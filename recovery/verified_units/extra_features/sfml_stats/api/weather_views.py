# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Weather history, comparison, power-source, and weather-dashboard views."""
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

_LOGGER = logging.getLogger(__name__)


class WeatherHistoryView(HomeAssistantView):
    """Get weather history data. @zara"""

    url = "/api/sfml_stats/weather_history"
    name = "api:sfml_stats:weather_history"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get weather history. @zara"""
        try:
            from ..weather_collector import WeatherDataCollector as WeatherDataCollector

            data_path = Path(context.HASS.config.path()) / "sfml_stats_weather"
            collector = WeatherDataCollector(context.HASS, data_path)
            requested_days = max(7, min(int(request.query.get("days", 30)), 365))
            include_partial = (
                request.query.get("include_partial", "false").lower() == "true"
            )
            history = await collector.get_history(
                days=365, include_partial=include_partial
            )
            returned_history = (
                history[-requested_days:] if len(history) > requested_days else history
            )
            statistics = await collector.get_statistics()
            return web.json_response(
                {
                    "success": True,
                    "data": returned_history,
                    "stats": statistics,
                    "requested_days": requested_days,
                    "returned_days": len(returned_history),
                    "available_days": len(history),
                    "time_context": _time_context(),
                }
            )
        except Exception as error:
            _LOGGER.error("Error fetching weather history: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class WeatherComparisonView(HomeAssistantView):
    """Get actual vs forecast weather comparison data. @zara"""

    url = "/api/sfml_stats/weather_comparison"
    name = "api:sfml_stats:weather_comparison"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get actual vs forecast weather comparison data. @zara"""
        from ..core.dev_mode import (
            is_dev_active as is_dev_active,
            json_dev_required as json_dev_required,
        )

        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            from ..weather_collector import WeatherDataCollector as WeatherDataCollector

            requested_days = max(1, min(int(request.query.get("days", 7)), 30))
            data_path = Path(context.HASS.config.path()) / "sfml_stats_weather"
            collector = WeatherDataCollector(context.HASS, data_path)
            comparison = await collector.get_comparison_data(days=requested_days)
            return web.json_response(comparison)
        except Exception as error:
            _LOGGER.error("Error fetching weather comparison: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class ForecastComparisonView(HomeAssistantView):
    """View to get forecast comparison data. @zara"""

    url = "/api/sfml_stats/forecast_comparison"
    name = "api:sfml_stats:forecast_comparison"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get forecast comparison data. @zara"""
        from ..core.dev_mode import (
            is_dev_active as is_dev_active,
            json_dev_required as json_dev_required,
        )

        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            from ..readers.forecast_comparison_reader import (
                ForecastComparisonReader as ForecastComparisonReader,
            )

            requested_days = max(1, min(int(request.query.get("days", 7)), 30))
            config = _get_config()
            external_forecast_1_name = (
                config.get(CONF_FORECAST_ENTITY_1_NAME)
                or DEFAULT_FORECAST_ENTITY_1_NAME
            )
            external_forecast_2_name = (
                config.get(CONF_FORECAST_ENTITY_2_NAME)
                or DEFAULT_FORECAST_ENTITY_2_NAME
            )
            database_path = Path(context.HASS.config.path()) / SOLAR_FORECAST_DB
            forecast_reader = ForecastComparisonReader(
                database_path,
                external_1_name=external_forecast_1_name,
                external_2_name=external_forecast_2_name,
            )
            chart_data = await forecast_reader.async_get_chart_data(days=requested_days)
            return web.json_response(chart_data)
        except Exception as error:
            _LOGGER.error(
                "Error fetching forecast comparison: %s", error, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class PowerSourcesHistoryView(HomeAssistantView):
    """View to get power sources history data from HA Recorder. @zara"""

    url = "/api/sfml_stats/power_sources_history"
    name = "api:sfml_stats:power_sources_history"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get power sources history from Home Assistant Recorder. @zara"""
        try:
            hours = max(1, min(int(request.query.get("hours", 24)), 168))
            use_today = str(request.query.get("today", "")).lower() in (
                "1",
                "true",
                "yes",
            )
            local_date = (
                ha_today(context.HASS).isoformat()
                if use_today
                else request.query.get("date")
            )
            config = _get_config()
            sensors = {
                "solar_power": "SFML stats_power_sources.solar_power_w",
                "solar_to_house": config.get(CONF_SENSOR_SOLAR_TO_HOUSE),
                "solar_to_battery": config.get(CONF_SENSOR_SOLAR_TO_BATTERY),
                "battery_to_house": config.get(CONF_SENSOR_BATTERY_TO_HOUSE),
                "grid_to_house": config.get(CONF_SENSOR_GRID_TO_HOUSE),
                "grid_to_battery": config.get(CONF_SENSOR_GRID_TO_BATTERY),
                "home_consumption": config.get(CONF_SENSOR_HOME_CONSUMPTION),
                "battery_soc": config.get(CONF_SENSOR_BATTERY_SOC),
            }
            power_history = await self._get_power_sources_collector_data(
                hours, local_date=local_date
            )
            data_source = "db"
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "hours": hours,
                    "local_date": local_date,
                    "sensors": sensors,
                    "data": power_history,
                    "data_source": data_source,
                }
            )
        except Exception as error:
            _LOGGER.error(
                "Error fetching power sources history: %s", error, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    async def _get_hourly_history_from_file(self) -> list[dict]:
        """Get hourly history from our own data file as alternative. @zara"""
        try:
            history_path = (
                Path(context.HASS.config.path())
                / "sfml_stats"
                / "data"
                / "hourly_billing_history.json"
            )
            if not history_path.exists():
                return []
            import aiofiles as aiofiles
            import json as json

            async with aiofiles.open(history_path, "r") as history_file:
                history_text = await history_file.read()
                history = (json.loads if True else None)(history_text)
            hourly_entries = history.get("hours", {})
            power_history = []
            for hour_key, hourly_data in sorted(hourly_entries.items()):
                power_history.append(
                    {
                        "timestamp": hour_key + ":00",
                        "solar_to_house": hourly_data.get("solar_to_house_kwh", 0)
                        * 1000,
                        "battery_to_house": hourly_data.get("battery_to_house_kwh", 0)
                        * 1000,
                        "grid_to_house": hourly_data.get("grid_to_house_kwh", 0) * 1000,
                        "home_consumption": hourly_data.get("home_consumption_kwh", 0)
                        * 1000,
                        "battery_soc": None,
                    }
                )
            if True:
                return power_history
        except Exception as error:
            _LOGGER.error("Error reading hourly history file: %s", error)
            return []

    async def _get_power_sources_collector_data(
        self, hours: int, local_date: str | None = None
    ) -> list[dict]:
        """Get data from stats_power_sources DB table. @zara"""
        try:
            async with _get_db() as connection:
                if local_date:
                    start_timestamp, end_timestamp = (
                        _get_broad_timestamp_range_for_local_dates([local_date])
                    )
                    timestamp_condition = "timestamp >= ? AND timestamp < ?"
                    query_parameters = (start_timestamp, end_timestamp)
                else:
                    cutoff_timestamp = (
                        ha_now(context.HASS).astimezone(timezone.utc)
                        - timedelta(hours=hours)
                    ).isoformat()
                    timestamp_condition = "timestamp >= ?"
                    query_parameters = (cutoff_timestamp,)
                available_columns = await _table_columns(
                    connection, "stats_power_sources"
                )
                grid_to_battery_expression = (
                    "grid_to_battery_w"
                    if "grid_to_battery_w" in available_columns
                    else "0 AS grid_to_battery_w"
                )
                solar_to_grid_expression = (
                    "solar_to_grid_w"
                    if "solar_to_grid_w" in available_columns
                    else "0 AS solar_to_grid_w"
                )
                battery_soc_expression = (
                    "battery_soc"
                    if "battery_soc" in available_columns
                    else "NULL AS battery_soc"
                )
                async with connection.execute(
                    f"\n                    SELECT timestamp, solar_power_w, solar_to_house_w,\n                           solar_to_battery_w, battery_to_house_w,\n                           grid_to_house_w, {grid_to_battery_expression},\n                           {solar_to_grid_expression}, house_consumption_w,\n                           {battery_soc_expression}\n                    FROM stats_power_sources\n                    WHERE {timestamp_condition}\n                    ORDER BY timestamp\n                ",
                    query_parameters,
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    return []
                power_history = []
                for row in rows:
                    local_timestamp = as_ha_local(row["timestamp"], context.HASS)
                    if local_date and local_timestamp.date().isoformat() != local_date:
                        continue
                    power_history.append(
                        {
                            "timestamp": row["timestamp"],
                            "local_timestamp": local_timestamp.isoformat(),
                            "local_date": local_timestamp.date().isoformat(),
                            "local_hour": local_timestamp.hour,
                            "hour_key": format_local_hour_key(
                                local_timestamp, context.HASS
                            ),
                            "solar_power": row["solar_power_w"] or 0,
                            "solar_to_house": row["solar_to_house_w"] or 0,
                            "solar_to_battery": row["solar_to_battery_w"] or 0,
                            "battery_to_house": row["battery_to_house_w"] or 0,
                            "grid_to_house": row["grid_to_house_w"] or 0,
                            "grid_to_battery": row["grid_to_battery_w"] or 0,
                            "house_to_grid": row["solar_to_grid_w"] or 0,
                            "home_consumption": row["house_consumption_w"] or 0,
                            "battery_soc": row["battery_soc"],
                        }
                    )
                    continue
                _LOGGER.debug("Power sources from DB: %d points", len(power_history))
                return power_history
        except Exception as error:
            _LOGGER.error("Error reading power sources from DB: %s", error)
            return []
        return None if True else None

    def _process_history(
        self,
        history_data: dict[str, list],
        sensors: dict[str, str | None],
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """Process and align history data into time series. @zara"""
        interval_minutes = 5
        time_series = []
        sample_time = start_time
        while sample_time <= end_time:
            time_series.append(
                {
                    "timestamp": sample_time.isoformat(),
                    "solar_power": None,
                    "solar_to_house": None,
                    "solar_to_battery": None,
                    "battery_to_house": None,
                    "grid_to_house": None,
                    "home_consumption": None,
                    "battery_soc": None,
                }
            )
            sample_time += timedelta(minutes=interval_minutes)
        for source_key, entity_id in sensors.items():
            if not entity_id or entity_id not in history_data:
                continue
            states = history_data[entity_id]
            if not states:
                continue
            ordered_states = sorted(
                states,
                key=lambda s: (
                    s.last_updated if hasattr(s, "last_updated") else s.last_changed
                ),
            )
            state_index = 0
            for point in time_series:
                point_time = datetime.fromisoformat(point["timestamp"])
                if point_time.tzinfo is None:
                    point_time = point_time.replace(tzinfo=timezone.utc)
                while state_index < len(ordered_states) - 1:
                    next_state = ordered_states[state_index + 1]
                    next_state_time = (
                        next_state.last_updated
                        if hasattr(next_state, "last_updated")
                        else next_state.last_changed
                    )
                    if next_state_time.tzinfo is None:
                        next_state_time = next_state_time.replace(tzinfo=timezone.utc)
                    if next_state_time <= point_time:
                        state_index += 1
                    else:
                        break
                if not state_index < len(ordered_states):
                    continue
                state = ordered_states[state_index]
                try:
                    numeric_state = float(state.state)
                    point[source_key] = round(numeric_state, 3)
                except (ValueError, TypeError):
                    continue
        return time_series


class EnergySourcesDailyStatsView(HomeAssistantView):
    """API for daily energy sources statistics. @zara"""

    url = "/api/sfml_stats/energy_sources_daily_stats"
    name = "api:sfml_stats:energy_sources_daily_stats"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get daily energy sources statistics. @zara"""
        try:
            requested_days = max(1, min(int(request.query.get("days", 7)), 365))
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            collector = None
            domain_data = context.HASS.data.get(DOMAIN, {})
            for entry_id, entry_data in domain_data.items():
                if isinstance(entry_data, dict):
                    if "power_sources_collector" in entry_data:
                        collector = entry_data["power_sources_collector"]
                        break
            if collector is None:
                data_path = (
                    Path(context.HASS.config.path())
                    / "sfml_stats"
                    / "data"
                    / "energy_sources_daily_stats.json"
                )
                if data_path.exists():
                    import aiofiles as aiofiles

                    async with aiofiles.open(data_path, "r") as history_file:
                        history_text = await history_file.read()
                        daily_stats = json.loads(history_text)
                else:
                    daily_stats = {"days": {}}
            else:
                daily_stats = await collector.get_daily_stats(requested_days)
            daily_history_path = (
                Path(context.HASS.config.path())
                / "sfml_stats"
                / "data"
                / "daily_energy_history.json"
            )
            if daily_history_path.exists():
                import aiofiles as aiofiles

                async with aiofiles.open(daily_history_path, "r") as history_file:
                    history_text = await history_file.read()
                    daily_history = json.loads(history_text)
                    historical_days = daily_history.get("days", {})
                    for historical_date, historical_day in historical_days.items():
                        if historical_date not in daily_stats.get("days", {}):
                            daily_stats.setdefault("days", {})[historical_date] = {
                                "date": historical_date,
                                "solar_to_house_kwh": historical_day.get(
                                    "solar_to_house_kwh", 0
                                ),
                                "solar_to_battery_kwh": historical_day.get(
                                    "battery_charge_solar_kwh", 0
                                ),
                                "battery_to_house_kwh": historical_day.get(
                                    "battery_to_house_kwh", 0
                                ),
                                "battery_charge_grid_kwh": historical_day.get(
                                    "battery_charge_grid_kwh", 0
                                ),
                                "grid_to_house_kwh": historical_day.get(
                                    "grid_import_kwh", 0
                                ),
                                "grid_export_kwh": historical_day.get(
                                    "grid_export_kwh", 0
                                ),
                                "home_consumption_kwh": historical_day.get(
                                    "home_consumption_kwh", 0
                                ),
                                "solar_yield_kwh": historical_day.get(
                                    "solar_yield_kwh", 0
                                ),
                                "price_ct_kwh": historical_day.get("price_ct_kwh", 0),
                                "autarky_percent": historical_day.get(
                                    "autarky_percent", 0
                                ),
                                "self_consumption_percent": historical_day.get(
                                    "self_consumption_percent", 0
                                ),
                                "avg_soc": historical_day.get("avg_soc", 0),
                                "min_soc": historical_day.get("min_soc", 0),
                                "max_soc": historical_day.get("max_soc", 0),
                                "peak_battery_power_w": historical_day.get(
                                    "peak_battery_power_w", 0
                                ),
                                "peak_consumption_w": historical_day.get(
                                    "peak_battery_power_w", 0
                                ),
                            }
                            continue
                        existing_day_stats = daily_stats["days"][historical_date]
                        if (
                            existing_day_stats.get("peak_battery_power_w") is None
                            or existing_day_stats.get("peak_battery_power_w") == 0
                        ):
                            existing_day_stats["peak_battery_power_w"] = (
                                historical_day.get("peak_battery_power_w", 0)
                            )
                        if (
                            existing_day_stats.get("home_consumption_kwh") is None
                            or existing_day_stats.get("home_consumption_kwh") == 0
                        ):
                            existing_day_stats["home_consumption_kwh"] = (
                                historical_day.get("home_consumption_kwh", 0)
                            )
                        if (
                            existing_day_stats.get("autarky_percent") is None
                            or existing_day_stats.get("autarky_percent") == 0
                        ):
                            existing_day_stats["autarky_percent"] = historical_day.get(
                                "autarky_percent", 0
                            )
                        if (
                            existing_day_stats.get("self_consumption_percent") is None
                            or existing_day_stats.get("self_consumption_percent") == 0
                        ):
                            existing_day_stats["self_consumption_percent"] = (
                                historical_day.get("self_consumption_percent", 0)
                            )
                        if (
                            existing_day_stats.get("peak_consumption_w") is None
                            or existing_day_stats.get("peak_consumption_w") == 0
                        ):
                            existing_day_stats["peak_consumption_w"] = (
                                historical_day.get("peak_battery_power_w", 0)
                            )
            config = _get_config()
            solar_data_reader = SFMLDataReader(context.HASS)
            solar_power_w = await solar_data_reader.get_live_power()
            solar_to_house_w = _get_sensor_value(config.get(CONF_SENSOR_SOLAR_TO_HOUSE))
            solar_to_battery_w = _get_sensor_value(
                config.get(CONF_SENSOR_SOLAR_TO_BATTERY)
            )
            if solar_power_w is not None and solar_power_w < 0:
                solar_power_w = 0.0
            if solar_to_house_w is not None and solar_to_house_w < 0:
                solar_to_house_w = 0.0
            if solar_to_battery_w is not None and solar_to_battery_w < 0:
                solar_to_battery_w = 0.0
            if solar_power_w is not None and solar_power_w <= 0:
                solar_to_house_w = 0.0
                solar_to_battery_w = 0.0
            elif solar_power_w is not None:
                if solar_to_battery_w is not None:
                    solar_to_battery_w = min(solar_to_battery_w, solar_power_w)
                if solar_to_house_w is not None:
                    solar_to_house_w = min(solar_to_house_w, solar_power_w)
            current_values = {
                "solar_yield_daily": await solar_data_reader.get_live_yield(),
                "solar_to_house": solar_to_house_w,
                "solar_to_battery": solar_to_battery_w,
                "battery_to_house": _get_sensor_value(
                    config.get(CONF_SENSOR_BATTERY_TO_HOUSE)
                ),
                "grid_to_house": _get_sensor_value(
                    config.get(CONF_SENSOR_GRID_TO_HOUSE)
                ),
                "home_consumption": _get_sensor_value(
                    config.get(CONF_SENSOR_HOME_CONSUMPTION)
                ),
            }
            hourly_consumption_by_date = {}
            hourly_grid_import_by_date = {}
            daily_prices = {}
            try:
                cutoff = (
                    ha_now(context.HASS) - timedelta(days=requested_days)
                ).strftime("%Y-%m-%d")
                async with _get_db() as connection:
                    async with connection.execute(
                        "SELECT date, hour,\n                                  COALESCE(grid_to_house_kwh, 0) + COALESCE(solar_to_house_kwh, 0)\n                                  + COALESCE(battery_to_house_kwh, 0) AS consumption_kwh,\n                                  COALESCE(grid_to_house_kwh, 0) + COALESCE(grid_to_battery_kwh, 0) AS grid_import_kwh\n                           FROM stats_hourly_billing\n                           WHERE date >= ?\n                           ORDER BY date, hour",
                        (cutoff,),
                    ) as cursor:
                        for row in await cursor.fetchall():
                            hourly_date = row["date"]
                            hour = row["hour"]
                            hourly_consumption_by_date.setdefault(hourly_date, {})[
                                hour
                            ] = round(row["consumption_kwh"], 4)
                            hourly_grid_import_by_date.setdefault(hourly_date, {})[
                                hour
                            ] = round(row["grid_import_kwh"], 4)
                    async with connection.execute(
                        "SELECT date, average_total FROM GPM_daily_averages\n                           WHERE date >= ? ORDER BY date",
                        (cutoff,),
                    ) as cursor:
                        for row in await cursor.fetchall():
                            daily_prices[row["date"]] = round(row["average_total"], 2)
            except Exception as lookup_error:
                _LOGGER.debug("Could not load hourly/price data: %s", lookup_error)
            projection = await _energy_correction_projection(cutoff)
            for corrected_date, corrected_day_stats in daily_stats.get(
                "days", {}
            ).items():
                corrected_day_stats["solar_yield_kwh"] = corrected_value(
                    projection,
                    corrected_date,
                    "solar_yield_day_kwh",
                    corrected_day_stats.get("solar_yield_kwh", 0.0),
                )
                if "solar_yield_day_kwh" in projection.get(corrected_date, {}):
                    corrected_day_stats["solar_total_kwh"] = corrected_day_stats[
                        "solar_yield_kwh"
                    ]
                corrected_day_stats["grid_import_kwh"] = corrected_value(
                    projection,
                    corrected_date,
                    "grid_import_day_kwh",
                    corrected_day_stats.get(
                        "grid_import_kwh",
                        corrected_day_stats.get("grid_to_house_kwh", 0.0),
                    ),
                )
                corrected_day_stats["grid_export_kwh"] = corrected_value(
                    projection,
                    corrected_date,
                    "grid_export_day_kwh",
                    corrected_day_stats.get("grid_export_kwh", 0.0),
                )
                corrected_day_stats["home_consumption_kwh"] = corrected_value(
                    projection,
                    corrected_date,
                    "home_consumption_day_kwh",
                    corrected_day_stats.get("home_consumption_kwh", 0.0),
                )
                day_projection = projection.get(corrected_date, {})
                if "grid_import_day_kwh" in day_projection:
                    corrected_day_stats["smartmeter_import_kwh"] = corrected_day_stats[
                        "grid_import_kwh"
                    ]
                if "grid_export_day_kwh" in day_projection:
                    corrected_day_stats["smartmeter_export_kwh"] = corrected_day_stats[
                        "grid_export_kwh"
                    ]
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "days_requested": requested_days,
                    "daily_stats": daily_stats.get("days", {}),
                    "current_values": current_values,
                    "hourly_consumption": hourly_consumption_by_date,
                    "hourly_grid": hourly_grid_import_by_date,
                    "daily_prices": daily_prices,
                }
            )
        except Exception as request_error:
            _LOGGER.error(
                "Error fetching energy sources daily stats: %s",
                request_error,
                exc_info=True,
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class WeatherDashboardView(HomeAssistantView):
    """Aggregated weather page data: current, forecast, radiation, sources, clothing, astronomy. @zara"""

    url = "/api/sfml_stats/weather/dashboard"
    name = "api:sfml_stats:weather:dashboard"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return full weather dashboard payload. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            config = {}
            for entry_data in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(entry_data, dict):
                    if "config" in entry_data:
                        config = entry_data["config"]
                        break
            current = await self._get_current(config)
            forecast = await self._get_forecast_72h()
            radiation = await self._get_radiation_today()
            clothing = self._get_clothing_recommendation(current)
            astronomy = await self._get_astronomy()
            return web.json_response(
                {
                    "success": True,
                    "time_context": _time_context(),
                    "current": current,
                    "forecast": forecast,
                    "forecast_meta": {
                        "hours": len(forecast),
                        "target_hours": 72,
                        "is_complete": len(forecast) >= 72,
                    },
                    "radiation": radiation,
                    "clothing": clothing,
                    "astronomy": astronomy,
                }
            )
        except Exception as error:
            _LOGGER.error("Error in weather dashboard: %s", error, exc_info=True)
            return web.json_response({"error": str(error)}, status=500)

    async def _get_current(self, config: dict) -> dict:
        """Latest weather snapshot from measured DB values with HA as fallback. @zara"""
        current = {
            "temperature": None,
            "feels_like": None,
            "humidity": None,
            "wind_speed": None,
            "wind_gust": None,
            "wind_bearing": None,
            "pressure": None,
            "pressure_trend": None,
            "visibility_km": None,
            "cloud_cover": None,
            "precipitation_mm": None,
            "precipitation_unit": None,
            "precipitation_semantics": None,
            "condition": None,
            "dewpoint": None,
            "uv_index": None,
            "solar_radiation_wm2": None,
            "timestamp": None,
            "solar_potential": None,
        }
        weather_entity = config.get(CONF_WEATHER_ENTITY)
        if weather_entity:
            state = context.HASS.states.get(weather_entity)
            if state is not None:
                attributes = state.attributes or {}
                current["temperature"] = attributes.get("temperature")
                current["humidity"] = attributes.get("humidity")
                current["wind_speed"] = attributes.get("wind_speed")
                current["wind_gust"] = attributes.get("wind_gust_speed")
                current["wind_bearing"] = attributes.get("wind_bearing")
                current["pressure"] = attributes.get("pressure")
                current["visibility_km"] = attributes.get("visibility")
                current["condition"] = state.state
                current["dewpoint"] = attributes.get("dew_point")
                current["uv_index"] = attributes.get("uv_index")
        try:
            today_iso = ha_today(context.HASS).isoformat()
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT temperature_c, humidity_percent, wind_speed_ms,\n                              precipitation_mm, pressure_hpa, solar_radiation_wm2,\n                              cloud_cover_percent, dewpoint_c, condition,\n                              date, hour, precipitation_unit, precipitation_semantics\n                       FROM hourly_weather_actual\n                       WHERE date = ?\n                       ORDER BY hour DESC LIMIT 1",
                    (today_iso,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        if row[0] is not None:
                            current["temperature"] = row[0]
                        if row[1] is not None:
                            current["humidity"] = row[1]
                        if row[2] is not None:
                            current["wind_speed"] = round(row[2] * 3.6, 1)
                        if row[3] is not None:
                            current["precipitation_mm"] = row[3]
                        if len(row) > 11 and row[11]:
                            current["precipitation_unit"] = row[11]
                        if len(row) > 12 and row[12]:
                            current["precipitation_semantics"] = row[12]
                        if row[4] is not None:
                            current["pressure"] = row[4]
                        if row[6] is not None:
                            current["cloud_cover"] = row[6]
                        if row[7] is not None:
                            current["dewpoint"] = row[7]
                        if row[8] is not None:
                            current["condition"] = row[8]
                        elif row[3] is not None and row[3] > 0.5:
                            current["condition"] = "rainy"
                        elif row[6] is not None and row[6] >= 85:
                            current["condition"] = "cloudy"
                        elif row[6] is not None and row[6] >= 35:
                            current["condition"] = "partlycloudy"
                        elif row[6] is not None:
                            current["condition"] = "sunny"
                        current["solar_radiation_wm2"] = row[5]
                        current["timestamp"] = f"{row[9]} {row[10]:02d}:00"
        except Exception as error:
            _LOGGER.debug("Current weather fallback failed: %s", error)
        try:
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT pressure_hpa FROM hourly_weather_actual\n                       WHERE pressure_hpa IS NOT NULL\n                       ORDER BY date DESC, hour DESC LIMIT 3"
                ) as cursor:
                    rows = await cursor.fetchall()
                    if len(rows) >= 2 and rows[0][0] and rows[-1][0]:
                        pressure_change_hpa = rows[0][0] - rows[-1][0]
                        if pressure_change_hpa > 0.5:
                            current["pressure_trend"] = "rising"
                        elif pressure_change_hpa < -0.5:
                            current["pressure_trend"] = "falling"
                        else:
                            current["pressure_trend"] = "steady"
        except Exception:
            pass
        temperature_c = current.get("temperature")
        raw_wind_speed = current.get("wind_speed")
        humidity_percent = current.get("humidity")
        if temperature_c is not None:
            try:
                feels_like_c = float(temperature_c)
                wind_speed_kmh = (
                    max(0.0, float(raw_wind_speed))
                    if raw_wind_speed is not None
                    else 0.0
                )
                if feels_like_c <= 10 and wind_speed_kmh > 4.8:
                    feels_like_c = (
                        13.12
                        + 0.6215 * feels_like_c
                        - 11.37 * wind_speed_kmh**0.16
                        + 0.3965 * feels_like_c * wind_speed_kmh**0.16
                    )
                elif feels_like_c >= 27 and humidity_percent is not None:
                    feels_like_c = feels_like_c + (float(humidity_percent) - 40) * 0.05
                current["feels_like"] = round(feels_like_c, 1)
            except (TypeError, ValueError):
                pass
        raw_cloud_cover = current.get("cloud_cover")
        if raw_cloud_cover is not None:
            try:
                cloud_cover_percent = float(raw_cloud_cover)
                if cloud_cover_percent < 30:
                    current["solar_potential"] = "good"
                elif cloud_cover_percent < 70:
                    current["solar_potential"] = "medium"
                else:
                    current["solar_potential"] = "poor"
            except (TypeError, ValueError):
                pass
        return current if True else None

    async def _get_forecast_72h(self) -> list[dict]:
        """Next 72h forecast: temp, clouds, rain, wind, radiation. @zara"""
        forecast = []
        try:
            import zoneinfo as zoneinfo
            from datetime import time as clock_time

            time_zone = (
                zoneinfo.ZoneInfo(context.HASS.config.time_zone)
                if context.HASS and context.HASS.config.time_zone
                else zoneinfo.ZoneInfo("UTC")
            )
            now = ha_now(context.HASS)
            cutoff = now - timedelta(hours=1)
            start_date_iso = cutoff.date().isoformat()
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT forecast_date, hour, temperature, clouds, rain,\n                              wind, solar_radiation_wm2, direct_radiation,\n                              diffuse_radiation, humidity, weather_code\n                       FROM weather_forecast\n                       WHERE forecast_date >= ? AND forecast_date <= date(?, '+5 days')\n                       ORDER BY forecast_date, hour",
                    (start_date_iso, start_date_iso),
                ) as cursor:
                    for row in await cursor.fetchall():
                        forecast_date = row[0]
                        forecast_hour = row[1]
                        try:
                            local_timestamp = datetime.combine(
                                date.fromisoformat(forecast_date),
                                clock_time(forecast_hour),
                                tzinfo=time_zone,
                            )
                        except Exception:
                            continue
                        if local_timestamp < cutoff:
                            continue
                        forecast.append(
                            {
                                "time": f"{row[0]} {row[1]:02d}:00",
                                "temperature": (
                                    round(row[2], 1) if row[2] is not None else None
                                ),
                                "cloud_cover": (
                                    round(row[3], 0) if row[3] is not None else None
                                ),
                                "rain": round(row[4], 1) if row[4] is not None else 0,
                                "wind": (
                                    round(row[5], 1) if row[5] is not None else None
                                ),
                                "ghi": round(row[6], 0) if row[6] is not None else 0,
                                "dni": round(row[7], 0) if row[7] is not None else 0,
                                "dhi": round(row[8], 0) if row[8] is not None else 0,
                                "humidity": (
                                    round(row[9], 0) if row[9] is not None else None
                                ),
                                "weather_code": row[10],
                            }
                        )
                        if len(forecast) >= 72:
                            break
        except Exception as error:
            _LOGGER.debug("Forecast 72h lookup failed: %s", error)
        try:
            pass
        except Exception as error:
            _LOGGER.debug("Forecast 72h lookup failed: %s", error)
        return forecast

    async def _get_radiation_today(self) -> dict:
        """Today's hourly radiation actual + forecast + clear-sky peaks. @zara"""
        today_iso = ha_now(context.HASS).date().isoformat()
        actual_radiation = []
        forecast_radiation = []
        clear_sky_radiation = []
        try:
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT hour, solar_radiation_wm2\n                       FROM hourly_weather_actual\n                       WHERE date = ? ORDER BY hour",
                    (today_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        actual_radiation.append(
                            {"hour": row[0], "ghi": round(row[1] or 0, 0)}
                        )
                async with connection.execute(
                    "SELECT hour, solar_radiation_wm2, direct_radiation, diffuse_radiation\n                       FROM weather_forecast\n                       WHERE forecast_date = ? ORDER BY hour",
                    (today_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        forecast_radiation.append(
                            {
                                "hour": row[0],
                                "ghi": round(row[1] or 0, 0),
                                "dni": round(row[2] or 0, 0),
                                "dhi": round(row[3] or 0, 0),
                            }
                        )
                async with connection.execute(
                    "SELECT hour, clear_sky_radiation_wm2\n                       FROM astronomy_cache\n                       WHERE cache_date = ?\n                         AND clear_sky_radiation_wm2 IS NOT NULL\n                       ORDER BY hour",
                    (today_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        clear_sky_radiation.append(
                            {"hour": row[0], "ghi": round(max(0, row[1]), 0)}
                        )
        except Exception as error:
            _LOGGER.debug("Radiation today lookup failed: %s", error)
        return {
            "actual": actual_radiation,
            "forecast": forecast_radiation,
            "clear_sky": clear_sky_radiation,
        }

    def _get_clothing_recommendation(self, current: dict) -> dict:
        """Rule-based clothing advice from current weather. @zara"""
        effective_temperature_c = current.get("feels_like") or current.get(
            "temperature"
        )
        wind_speed_kmh = current.get("wind_speed") or 0
        precipitation_mm = current.get("precipitation_mm") or 0
        humidity_percent = current.get("humidity") or 50
        if effective_temperature_c is None:
            return {"available": False}
        try:
            effective_temperature_c = float(effective_temperature_c)
            wind_speed_kmh = float(wind_speed_kmh)
            precipitation_mm = float(precipitation_mm)
        except (TypeError, ValueError):
            return {"available": False}
        if effective_temperature_c < -5:
            label, bottom, top, jacket, headwear = (
                "Eiskalt",
                "Thermohose",
                "Thermo-Pullover",
                "Winterjacke",
                "Muetze + Schal",
            )
            description = "Extreme Kaelte. Mehrschichtig kleiden, Gesicht schuetzen."
            message_keys = (
                "extreme_cold",
                "thermal_pants",
                "thermal_sweater",
                "winter_jacket",
                "hat_scarf",
                "extreme_cold",
            )
        elif effective_temperature_c < 5:
            label, bottom, top, jacket, headwear = (
                "Kalt",
                "Lange Hose",
                "Pullover",
                "Winterjacke",
                "Muetze",
            )
            description = "Kalt. Warme Schichten, Muetze empfohlen."
            message_keys = (
                "cold",
                "long_pants",
                "sweater",
                "winter_jacket",
                "beanie",
                "cold",
            )
        elif effective_temperature_c < 12:
            label, bottom, top, jacket, headwear = (
                "Kuehl",
                "Lange Hose",
                "Langarm-Shirt",
                "Uebergangsjacke",
                None,
            )
            description = "Kuehl. Jacke mitnehmen."
            message_keys = (
                "cool",
                "long_pants",
                "long_sleeve",
                "light_jacket",
                None,
                "cool",
            )
        elif effective_temperature_c < 18:
            label, bottom, top, jacket, headwear = (
                "Mild",
                "Lange Hose",
                "T-Shirt / Langarm",
                "Leichte Jacke",
                None,
            )
            description = "Angenehm mild. Leichte Jacke genuegt."
            message_keys = (
                "mild",
                "long_pants",
                "tshirt_long",
                "light_jacket",
                None,
                "mild",
            )
        elif effective_temperature_c < 25:
            label, bottom, top, jacket, headwear = (
                "Warm",
                "Kurze/lange Hose",
                "T-Shirt",
                None,
                None,
            )
            description = "Warm und angenehm."
            message_keys = ("warm", "shorts_or_pants", "tshirt", None, None, "warm")
        else:
            label, bottom, top, jacket, headwear = (
                "Heiss",
                "Kurze Hose",
                "T-Shirt / Tank",
                None,
                "Sonnenhut",
            )
            description = "Heiss. Leicht kleiden, Sonne meiden, hydriert bleiben."
            message_keys = ("hot", "shorts", "tshirt_tank", None, "sun_hat", "hot")
        label_key, bottom_key, top_key, jacket_key, headwear_key, description_key = (
            message_keys
        )
        extras = []
        extra_keys = []
        if precipitation_mm > 0.5:
            extras.append("Regenschirm")
            extra_keys.append("umbrella")
            if jacket and "Regen" not in jacket:
                jacket = (
                    jacket.replace("jacke", "jacke (wasserdicht)")
                    if "jacke" in jacket.lower()
                    else jacket
                )
        if wind_speed_kmh > 30:
            extras.append("Windschutz")
            extra_keys.append("wind_protection")
        if humidity_percent > 85 and effective_temperature_c > 20:
            extras.append("Atmungsaktive Kleidung")
            extra_keys.append("breathable")
        if effective_temperature_c < 5 and wind_speed_kmh > 15:
            extras.append("Handschuhe")
            extra_keys.append("gloves")
        return {
            "available": True,
            "label": label,
            "label_key": label_key,
            "description": description,
            "description_key": description_key,
            "bottom": bottom,
            "bottom_key": bottom_key,
            "top": top,
            "top_key": top_key,
            "jacket": jacket,
            "jacket_key": jacket_key,
            "headwear": headwear,
            "headwear_key": headwear_key,
            "extras": extras,
            "extras_keys": extra_keys,
        }

    @staticmethod
    def _calc_moon(dt: datetime) -> tuple[str, float]:
        """Moon phase name + illumination from synodic cycle. @zara"""
        import math as math

        new_moon_epoch = datetime(2024, 1, 11, 11, 57, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = as_ha_local(dt, context.HASS)
        elapsed_seconds = (dt.astimezone(timezone.utc) - new_moon_epoch).total_seconds()
        synodic_month_days = 29.53058867
        moon_age_days = elapsed_seconds / 86400 % synodic_month_days
        illumination_percent = round(
            (1 - math.cos(2 * math.pi * moon_age_days / synodic_month_days)) / 2 * 100,
            1,
        )
        phase_index = int(moon_age_days / synodic_month_days * 8 + 0.5) % 8
        phase_names = [
            "Neumond",
            "Zunehmende Sichel",
            "Erstes Viertel",
            "Zunehmender Mond",
            "Vollmond",
            "Abnehmender Mond",
            "Letztes Viertel",
            "Abnehmende Sichel",
        ]
        return (phase_names[phase_index], illumination_percent)

    async def _get_astronomy(self) -> dict:
        """Sun/moon data from astronomy_cache + astronomy_system_info. @zara"""
        astronomy = {
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
            async with _get_db() as connection:
                today_iso = ha_now(context.HASS).date().isoformat()
                try:
                    async with connection.execute(
                        _DAILY_ASTRONOMY_QUERY, (today_iso,)
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            astronomy["sunrise"] = row[0]
                            astronomy["sunset"] = row[1]
                            astronomy["solar_noon"] = row[2]
                            if row[3] is not None:
                                astronomy["day_length_min"] = round(row[3] * 60, 1)
                            astronomy["max_elevation_deg"] = (
                                round(row[4], 1) if row[4] is not None else None
                            )
                except Exception as error:
                    _LOGGER.debug("astronomy_cache query failed: %s", error)
                try:
                    async with connection.execute(
                        "SELECT daylight_hours FROM astronomy_cache\n                           WHERE cache_date = date(?, '-1 day') AND hour = 12 LIMIT 1",
                        (today_iso,),
                    ) as cursor:
                        yesterday_daylight_row = await cursor.fetchone()
                        if (
                            yesterday_daylight_row
                            and yesterday_daylight_row[0] is not None
                            and (astronomy["day_length_min"] is not None)
                        ):
                            astronomy["day_length_delta_min"] = round(
                                astronomy["day_length_min"]
                                - yesterday_daylight_row[0] * 60,
                                1,
                            )
                except Exception:
                    pass
        except Exception as error:
            _LOGGER.debug("Astronomy lookup failed: %s", error)
        try:
            sun_state = context.HASS.states.get("sun.sun")
            if sun_state:
                attributes = sun_state.attributes or {}
                if astronomy["sunrise"] is None:
                    astronomy["sunrise"] = attributes.get("next_rising")
                if astronomy["sunset"] is None:
                    astronomy["sunset"] = attributes.get("next_setting")
        except Exception:
            pass
        try:
            moon_phase, illumination_percent = self._calc_moon(ha_now(context.HASS))
            astronomy["moon_phase"] = moon_phase
            astronomy["moon_illumination"] = illumination_percent
        except Exception:
            pass
        if True:
            return astronomy
        else:
            return astronomy
