# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _energy_correction_projection,
    _get_broad_timestamp_range_for_local_dates,
    _get_config,
    _get_db,
    _get_sensor_value,
    _table_columns,
    _time_context,
    local_only,
)
from .solar_views import _DAILY_ASTRONOMY_QUERY
from ..core.corrected_energy import corrected_value
from ..utils.time_utils import as_ha_local, format_local_hour_key, ha_now, ha_today
from ..const import (
    DOMAIN,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_WEATHER_ENTITY,
    SOLAR_FORECAST_DB,
    CONF_FORECAST_ENTITY_1_NAME,
    CONF_FORECAST_ENTITY_2_NAME,
    DEFAULT_FORECAST_ENTITY_1_NAME,
    DEFAULT_FORECAST_ENTITY_2_NAME,
)
from ..sfml_data_reader import SFMLDataReader


class PowerSourcesHistoryView(HomeAssistantView):

    async def _get_hourly_history_from_file(self) -> list[dict]:
        """Get hourly history from our own data file as alternative. @zara"""
        try:
            _var_var_26 = (
                Path(context.HASS.config.path())
                / "sfml_stats"
                / "data"
                / "hourly_billing_history.json"
            )
            if not _var_var_26.exists():
                return []
            import aiofiles as _var_var_27
            import json as _var_var_28

            async with _var_var_27.open(_var_var_26, "r") as _var_var_29:
                _var_var_30 = await _var_var_29.read()
                _var_var_19 = (_var_var_28.loads if True else None)(_var_var_30)
            _var_var_31 = _var_var_19.get("hours", {})
            _var_var_32 = []
            for _var_var_33, _var_var_34 in sorted(_var_var_31.items()):
                _var_var_32.append(
                    {
                        "timestamp": _var_var_33 + ":00",
                        "solar_to_house": _var_var_34.get("solar_to_house_kwh", 0)
                        * 1000,
                        "battery_to_house": _var_var_34.get("battery_to_house_kwh", 0)
                        * 1000,
                        "grid_to_house": _var_var_34.get("grid_to_house_kwh", 0) * 1000,
                        "home_consumption": _var_var_34.get("home_consumption_kwh", 0)
                        * 1000,
                        "battery_soc": None,
                    }
                )
            if True:
                return _var_var_32
        except Exception as _var_var_35:
            _LOGGER.error("Error reading hourly history file: %s", _var_var_35)
            return []

    def _process_history(
        self,
        history_data: dict[str, list],
        sensors: dict[str, str | None],
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """Process and align history data into time series. @zara"""
        _var_var_50 = 5
        _var_var_51 = []
        _var_var_52 = start_time
        while _var_var_52 <= end_time:
            _var_var_51.append(
                {
                    "timestamp": _var_var_52.isoformat(),
                    "solar_power": None,
                    "solar_to_house": None,
                    "solar_to_battery": None,
                    "battery_to_house": None,
                    "grid_to_house": None,
                    "home_consumption": None,
                    "battery_soc": None,
                }
            )
            _var_var_52 += timedelta(minutes=_var_var_50)
        for _var_var_53, _var_var_54 in sensors.items():
            if not _var_var_54 or _var_var_54 not in history_data:
                continue
            _var_var_55 = history_data[_var_var_54]
            if not _var_var_55:
                continue
            _var_var_56 = sorted(
                _var_var_55,
                key=lambda s: (
                    s.last_updated if hasattr(s, "last_updated") else s.last_changed
                ),
            )
            _var_var_57 = 0
            for _var_var_58 in _var_var_51:
                _var_var_59 = datetime.fromisoformat(_var_var_58["timestamp"])
                if _var_var_59.tzinfo is None:
                    _var_var_59 = _var_var_59.replace(tzinfo=timezone.utc)
                while _var_var_57 < len(_var_var_56) - 1:
                    _var_var_60 = _var_var_56[_var_var_57 + 1]
                    _var_var_61 = (
                        _var_var_60.last_updated
                        if hasattr(_var_var_60, "last_updated")
                        else _var_var_60.last_changed
                    )
                    if _var_var_61.tzinfo is None:
                        _var_var_61 = _var_var_61.replace(tzinfo=timezone.utc)
                    if _var_var_61 <= _var_var_59:
                        _var_var_57 += 1
                    else:
                        break
                if not _var_var_57 < len(_var_var_56):
                    continue
                _var_var_62 = _var_var_56[_var_var_57]
                try:
                    _var_var_63 = float(_var_var_62.state)
                    _var_var_58[_var_var_53] = round(_var_var_63, 3)
                except (ValueError, TypeError):
                    continue
        return _var_var_51


class EnergySourcesDailyStatsView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get daily energy sources statistics. @zara"""
        try:
            _var_var_11 = max(1, min(int(request.query.get("days", 7)), 365))
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            _var_var_2 = None
            _var_var_64 = context.HASS.data.get(DOMAIN, {})
            for _var_var_65, _var_var_66 in _var_var_64.items():
                if isinstance(_var_var_66, dict):
                    if "power_sources_collector" in _var_var_66:
                        _var_var_2 = _var_var_66["power_sources_collector"]
                        break
            if _var_var_2 is None:
                _var_var_1 = (
                    Path(context.HASS.config.path())
                    / "sfml_stats"
                    / "data"
                    / "energy_sources_daily_stats.json"
                )
                if _var_var_1.exists():
                    import aiofiles as _var_var_27

                    async with _var_var_27.open(_var_var_1, "r") as _var_var_29:
                        _var_var_30 = await _var_var_29.read()
                        _var_var_67 = json.loads(_var_var_30)
                else:
                    _var_var_67 = {"days": {}}
            else:
                _var_var_67 = await _var_var_2.get_daily_stats(_var_var_11)
            _var_var_68 = (
                Path(context.HASS.config.path())
                / "sfml_stats"
                / "data"
                / "daily_energy_history.json"
            )
            if _var_var_68.exists():
                import aiofiles as _var_var_27

                async with _var_var_27.open(_var_var_68, "r") as _var_var_29:
                    _var_var_30 = await _var_var_29.read()
                    _var_var_69 = json.loads(_var_var_30)
                    _var_var_70 = _var_var_69.get("days", {})
                    for _var_var_71, _var_var_72 in _var_var_70.items():
                        if _var_var_71 not in _var_var_67.get("days", {}):
                            _var_var_67.setdefault("days", {})[_var_var_71] = {
                                "date": _var_var_71,
                                "solar_to_house_kwh": _var_var_72.get(
                                    "solar_to_house_kwh", 0
                                ),
                                "solar_to_battery_kwh": _var_var_72.get(
                                    "battery_charge_solar_kwh", 0
                                ),
                                "battery_to_house_kwh": _var_var_72.get(
                                    "battery_to_house_kwh", 0
                                ),
                                "battery_charge_grid_kwh": _var_var_72.get(
                                    "battery_charge_grid_kwh", 0
                                ),
                                "grid_to_house_kwh": _var_var_72.get(
                                    "grid_import_kwh", 0
                                ),
                                "grid_export_kwh": _var_var_72.get(
                                    "grid_export_kwh", 0
                                ),
                                "home_consumption_kwh": _var_var_72.get(
                                    "home_consumption_kwh", 0
                                ),
                                "solar_yield_kwh": _var_var_72.get(
                                    "solar_yield_kwh", 0
                                ),
                                "price_ct_kwh": _var_var_72.get("price_ct_kwh", 0),
                                "autarky_percent": _var_var_72.get(
                                    "autarky_percent", 0
                                ),
                                "self_consumption_percent": _var_var_72.get(
                                    "self_consumption_percent", 0
                                ),
                                "avg_soc": _var_var_72.get("avg_soc", 0),
                                "min_soc": _var_var_72.get("min_soc", 0),
                                "max_soc": _var_var_72.get("max_soc", 0),
                                "peak_battery_power_w": _var_var_72.get(
                                    "peak_battery_power_w", 0
                                ),
                                "peak_consumption_w": _var_var_72.get(
                                    "peak_battery_power_w", 0
                                ),
                            }
                            continue
                        _var_var_73 = _var_var_67["days"][_var_var_71]
                        if (
                            _var_var_73.get("peak_battery_power_w") is None
                            or _var_var_73.get("peak_battery_power_w") == 0
                        ):
                            _var_var_73["peak_battery_power_w"] = _var_var_72.get(
                                "peak_battery_power_w", 0
                            )
                        if (
                            _var_var_73.get("home_consumption_kwh") is None
                            or _var_var_73.get("home_consumption_kwh") == 0
                        ):
                            _var_var_73["home_consumption_kwh"] = _var_var_72.get(
                                "home_consumption_kwh", 0
                            )
                        if (
                            _var_var_73.get("autarky_percent") is None
                            or _var_var_73.get("autarky_percent") == 0
                        ):
                            _var_var_73["autarky_percent"] = _var_var_72.get(
                                "autarky_percent", 0
                            )
                        if (
                            _var_var_73.get("self_consumption_percent") is None
                            or _var_var_73.get("self_consumption_percent") == 0
                        ):
                            _var_var_73["self_consumption_percent"] = _var_var_72.get(
                                "self_consumption_percent", 0
                            )
                        if (
                            _var_var_73.get("peak_consumption_w") is None
                            or _var_var_73.get("peak_consumption_w") == 0
                        ):
                            _var_var_73["peak_consumption_w"] = _var_var_72.get(
                                "peak_battery_power_w", 0
                            )
            _var_var_14 = _get_config()
            _var_var_74 = SFMLDataReader(context.HASS)
            _var_var_75 = await _var_var_74.get_live_power()
            _var_var_76 = _get_sensor_value(_var_var_14.get(CONF_SENSOR_SOLAR_TO_HOUSE))
            _var_var_77 = _get_sensor_value(
                _var_var_14.get(CONF_SENSOR_SOLAR_TO_BATTERY)
            )
            if _var_var_75 is not None and _var_var_75 < 0:
                _var_var_75 = 0.0
            if _var_var_76 is not None and _var_var_76 < 0:
                _var_var_76 = 0.0
            if _var_var_77 is not None and _var_var_77 < 0:
                _var_var_77 = 0.0
            if _var_var_75 is not None and _var_var_75 <= 0:
                _var_var_76 = 0.0
                _var_var_77 = 0.0
            elif _var_var_75 is not None:
                if _var_var_77 is not None:
                    _var_var_77 = min(_var_var_77, _var_var_75)
                if _var_var_76 is not None:
                    _var_var_76 = min(_var_var_76, _var_var_75)
            _var_var_78 = {
                "solar_yield_daily": await _var_var_74.get_live_yield(),
                "solar_to_house": _var_var_76,
                "solar_to_battery": _var_var_77,
                "battery_to_house": _get_sensor_value(
                    _var_var_14.get(CONF_SENSOR_BATTERY_TO_HOUSE)
                ),
                "grid_to_house": _get_sensor_value(
                    _var_var_14.get(CONF_SENSOR_GRID_TO_HOUSE)
                ),
                "home_consumption": _get_sensor_value(
                    _var_var_14.get(CONF_SENSOR_HOME_CONSUMPTION)
                ),
            }
            _var_var_79 = {}
            _var_var_80 = {}
            _var_var_81 = {}
            try:
                _var_var_82 = (
                    ha_now(context.HASS) - timedelta(days=_var_var_11)
                ).strftime("%Y-%m-%d")
                async with _get_db() as _var_var_36:
                    async with _var_var_36.execute(
                        "SELECT date, hour,\n                                  COALESCE(grid_to_house_kwh, 0) + COALESCE(solar_to_house_kwh, 0)\n                                  + COALESCE(battery_to_house_kwh, 0) AS consumption_kwh,\n                                  COALESCE(grid_to_house_kwh, 0) + COALESCE(grid_to_battery_kwh, 0) AS grid_import_kwh\n                           FROM stats_hourly_billing\n                           WHERE date >= ?\n                           ORDER BY date, hour",
                        (_var_var_82,),
                    ) as _var_var_46:
                        for _var_var_48 in await _var_var_46.fetchall():
                            _var_var_83 = _var_var_48["date"]
                            _var_var_84 = _var_var_48["hour"]
                            _var_var_79.setdefault(_var_var_83, {})[_var_var_84] = (
                                round(_var_var_48["consumption_kwh"], 4)
                            )
                            _var_var_80.setdefault(_var_var_83, {})[_var_var_84] = (
                                round(_var_var_48["grid_import_kwh"], 4)
                            )
                    async with _var_var_36.execute(
                        "SELECT date, average_total FROM GPM_daily_averages\n                           WHERE date >= ? ORDER BY date",
                        (_var_var_82,),
                    ) as _var_var_46:
                        for _var_var_48 in await _var_var_46.fetchall():
                            _var_var_81[_var_var_48["date"]] = round(
                                _var_var_48["average_total"], 2
                            )
            except Exception as _var_var_35:
                _LOGGER.debug("Could not load hourly/price data: %s", _var_var_35)
            _var_var_85 = await _energy_correction_projection(_var_var_82)
            for _var_var_86, _var_var_87 in _var_var_67.get("days", {}).items():
                _var_var_87["solar_yield_kwh"] = corrected_value(
                    _var_var_85,
                    _var_var_86,
                    "solar_yield_day_kwh",
                    _var_var_87.get("solar_yield_kwh", 0.0),
                )
                if "solar_yield_day_kwh" in _var_var_85.get(_var_var_86, {}):
                    _var_var_87["solar_total_kwh"] = _var_var_87["solar_yield_kwh"]
                _var_var_87["grid_import_kwh"] = corrected_value(
                    _var_var_85,
                    _var_var_86,
                    "grid_import_day_kwh",
                    _var_var_87.get(
                        "grid_import_kwh", _var_var_87.get("grid_to_house_kwh", 0.0)
                    ),
                )
                _var_var_87["grid_export_kwh"] = corrected_value(
                    _var_var_85,
                    _var_var_86,
                    "grid_export_day_kwh",
                    _var_var_87.get("grid_export_kwh", 0.0),
                )
                _var_var_87["home_consumption_kwh"] = corrected_value(
                    _var_var_85,
                    _var_var_86,
                    "home_consumption_day_kwh",
                    _var_var_87.get("home_consumption_kwh", 0.0),
                )
                _var_var_88 = _var_var_85.get(_var_var_86, {})
                if "grid_import_day_kwh" in _var_var_88:
                    _var_var_87["smartmeter_import_kwh"] = _var_var_87[
                        "grid_import_kwh"
                    ]
                if "grid_export_day_kwh" in _var_var_88:
                    _var_var_87["smartmeter_export_kwh"] = _var_var_87[
                        "grid_export_kwh"
                    ]
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "days_requested": _var_var_11,
                    "daily_stats": _var_var_67.get("days", {}),
                    "current_values": _var_var_78,
                    "hourly_consumption": _var_var_79,
                    "hourly_grid": _var_var_80,
                    "daily_prices": _var_var_81,
                }
            )
        except Exception as _var_var_8:
            _LOGGER.error(
                "Error fetching energy sources daily stats: %s",
                _var_var_8,
                exc_info=True,
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class WeatherDashboardView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return full weather dashboard payload. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            _var_var_14 = {}
            for _var_var_66 in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(_var_var_66, dict):
                    if "config" in _var_var_66:
                        _var_var_14 = _var_var_66["config"]
                        break
            _var_var_89 = await self._get_current(_var_var_14)
            _var_var_90 = await self._get_forecast_72h()
            _var_var_91 = await self._get_radiation_today()
            _var_var_92 = self._get_clothing_recommendation(_var_var_89)
            _var_var_93 = await self._get_astronomy()
            return web.json_response(
                {
                    "success": True,
                    "time_context": _time_context(),
                    "current": _var_var_89,
                    "forecast": _var_var_90,
                    "forecast_meta": {
                        "hours": len(_var_var_90),
                        "target_hours": 72,
                        "is_complete": len(_var_var_90) >= 72,
                    },
                    "radiation": _var_var_91,
                    "clothing": _var_var_92,
                    "astronomy": _var_var_93,
                }
            )
        except Exception as _var_var_8:
            _LOGGER.error("Error in weather dashboard: %s", _var_var_8, exc_info=True)
            return web.json_response({"error": str(_var_var_8)}, status=500)

    async def _get_astronomy(self) -> dict:
        """Sun/moon data from astronomy_cache + astronomy_system_info. @zara"""
        _var_var_143 = {
            "sunrise": None,
            "sunset": None,
            "day_length_min": None,
            "day_length_delta_min": None,
            "solar_noon": None,
            "moon_phase": None,
            "moon_illumination": None,
            "max_elevation_deg": None,
        }
        try:
            async with _get_db() as _var_var_97:
                _var_var_144 = ha_now(context.HASS).date().isoformat()
                try:
                    async with _var_var_97.execute(
                        _DAILY_ASTRONOMY_QUERY, (_var_var_144,)
                    ) as _var_var_46:
                        _var_var_48 = await _var_var_46.fetchone()
                        if _var_var_48:
                            _var_var_143["sunrise"] = _var_var_48[0]
                            _var_var_143["sunset"] = _var_var_48[1]
                            _var_var_143["solar_noon"] = _var_var_48[2]
                            if _var_var_48[3] is not None:
                                _var_var_143["day_length_min"] = round(
                                    _var_var_48[3] * 60, 1
                                )
                            _var_var_143["max_elevation_deg"] = (
                                round(_var_var_48[4], 1)
                                if _var_var_48[4] is not None
                                else None
                            )
                except Exception as _var_var_35:
                    _LOGGER.debug("astronomy_cache query failed: %s", _var_var_35)
                try:
                    async with _var_var_97.execute(
                        "SELECT daylight_hours FROM astronomy_cache\n                           WHERE cache_date = date(?, '-1 day') AND hour = 12 LIMIT 1",
                        (_var_var_144,),
                    ) as _var_var_46:
                        _var_var_145 = await _var_var_46.fetchone()
                        if (
                            _var_var_145
                            and _var_var_145[0] is not None
                            and (_var_var_143["day_length_min"] is not None)
                        ):
                            _var_var_143["day_length_delta_min"] = round(
                                _var_var_143["day_length_min"] - _var_var_145[0] * 60, 1
                            )
                except Exception:
                    pass
        except Exception as _var_var_35:
            _LOGGER.debug("Astronomy lookup failed: %s", _var_var_35)
        try:
            _var_var_146 = context.HASS.states.get("sun.sun")
            if _var_var_146:
                _var_var_95 = _var_var_146.attributes or {}
                if _var_var_143["sunrise"] is None:
                    _var_var_143["sunrise"] = _var_var_95.get("next_rising")
                if _var_var_143["sunset"] is None:
                    _var_var_143["sunset"] = _var_var_95.get("next_setting")
        except Exception:
            pass
        try:
            _var_var_147, _var_var_140 = self._calc_moon(ha_now(context.HASS))
            _var_var_143["moon_phase"] = _var_var_147
            _var_var_143["moon_illumination"] = _var_var_140
        except Exception:
            pass
        if True:
            return _var_var_143
        else:
            return _var_var_143
