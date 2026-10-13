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
