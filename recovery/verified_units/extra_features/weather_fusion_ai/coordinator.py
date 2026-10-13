# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Data coordinator for Weather Fusion AI with full AI correction pipeline.

@zara
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    DOMAIN,
    NAME,
    CONF_LOCATION_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_WIND_SPEED_SENSOR,
    CONF_WIND_DIRECTION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_PIRATE_WEATHER_API_KEY,
    DEFAULT_UPDATE_INTERVAL,
    LEARNING_MIN_DAYS,
    WMO_TO_HA_CONDITION,
    DEFAULT_CONDITION,
    DAY_TO_NIGHT_CONDITION,
    CLOUD_CLEAR_MAX,
    CLOUD_FAIR_MAX,
    CLOUD_MIXED_MAX,
)

_LOGGER = logging.getLogger(__name__)
LEGACY_AI_DATA_DIR = "/config/weather_fusion_ai"
SUN_ENTITY_ID = "sun.sun"
SUN_STATE_BELOW_HORIZON = "below_horizon"


def _normalize_daily_condition(condition: str | None) -> str:
    """Normalize hourly condition values for a daytime daily summary."""
    if condition == "clear-night":
        return "sunny"
    return condition or DEFAULT_CONDITION


_FOG_WMO_CODES = frozenset({45, 48})
_FOG_VISIBILITY_M = 1000.0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _wmo_code(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _cloud_cover_wmo_code(cloud_cover: object) -> int:
    """Map total cover onto WMO codes 0-3 using the existing cover limits."""
    cover = _finite_number(cloud_cover)
    if cover is None:
        return 3
    if cover <= CLOUD_CLEAR_MAX:
        return 0
    if cover <= CLOUD_FAIR_MAX or cover <= CLOUD_MIXED_MAX:
        return 2
    return 3


def condition_from_wmo(
    wmo_code: object, *, visibility_m: object = None, cloud_cover: object = None
) -> str:
    """Map a WMO code to an HA condition.

    Codes 45 and 48 stay fog only when visibility is missing or below 1 km.
    Clearer hours reuse the code 0-3 table from total cloud cover.
    """
    code = _wmo_code(wmo_code)
    visibility = _finite_number(visibility_m)
    if (
        code in _FOG_WMO_CODES
        and visibility is not None
        and (visibility >= _FOG_VISIBILITY_M)
    ):
        code = _cloud_cover_wmo_code(cloud_cover)
    if code is None:
        return DEFAULT_CONDITION
    return WMO_TO_HA_CONDITION.get(code, DEFAULT_CONDITION)


class WeatherFusionCoordinator(DataUpdateCoordinator):
    """Coordinator for Weather Fusion AI with full AI correction pipeline."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, data_dir: Path) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.data_dir = data_dir
        self.ai_data_dir = data_dir
        self.location_name = entry.data.get(CONF_LOCATION_NAME, "Home")
        self.latitude, self.longitude = self._configured_coordinates(hass, entry)
        self.time_zone = self._home_assistant_time_zone(hass)
        update_minutes = self._entry_value(
            entry, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        self.sensors = {
            "temperature": self._entry_value(entry, CONF_TEMP_SENSOR),
            "humidity": self._entry_value(entry, CONF_HUMIDITY_SENSOR),
            "pressure": self._entry_value(entry, CONF_PRESSURE_SENSOR),
            "wind_speed": self._entry_value(entry, CONF_WIND_SPEED_SENSOR),
            "wind_direction": self._entry_value(entry, CONF_WIND_DIRECTION_SENSOR),
            "rain": self._entry_value(entry, CONF_RAIN_SENSOR),
        }
        self.pirate_weather_api_key = self._entry_value(
            entry, CONF_PIRATE_WEATHER_API_KEY
        )
        self._weather_blender = None
        self._ai_pipeline = None
        self._actual_tracker = None
        self._current_weather = {}
        self._hourly_forecast = []
        self._daily_forecast = []
        self._forecast_snapshot_generated_at = None
        self._ai_stats = {
            "days_learned": 0,
            "accuracy_percent": 0.0,
            "active_sources": 0,
            "patterns_detected": 0,
            "corrections_applied": 0,
            "last_learning": None,
        }
        self._last_learning_run = None
        self._learning_interval_hours = 6
        super().__init__(
            hass,
            _LOGGER,
            name=f"{NAME} ({self.location_name})",
            update_interval=timedelta(minutes=update_minutes),
        )
        _LOGGER.info(
            "WeatherFusionCoordinator created - update every %d min", update_minutes
        )

    @staticmethod
    def _entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
        """Prefer options, including explicit None that clears a setup value."""
        if key in entry.options:
            return entry.options.get(key)
        return entry.data.get(key, default)

    @staticmethod
    def _configured_coordinates(
        hass: HomeAssistant, entry: ConfigEntry
    ) -> tuple[float, float]:
        latitude = entry.data.get(CONF_LATITUDE, hass.config.latitude)
        longitude = entry.data.get(CONF_LONGITUDE, hass.config.longitude)
        return (float(latitude), float(longitude))

    @staticmethod
    def _home_assistant_time_zone(hass: HomeAssistant) -> ZoneInfo:
        """Return the configured Home Assistant timezone with a safe fallback."""
        time_zone_name = getattr(hass.config, "time_zone", None) or "UTC"
        try:
            return ZoneInfo(time_zone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            _LOGGER.warning(
                "Invalid Home Assistant timezone %r; falling back to UTC",
                time_zone_name,
            )
            return ZoneInfo("UTC")

    def _local_now(self) -> datetime:
        """Return an aware current time in the authoritative HA timezone."""
        return datetime.now(self.time_zone)

    def _sync_home_assistant_context(self) -> None:
        """Apply HA timezone changes; keep the entry's configured coordinates."""
        time_zone = self._home_assistant_time_zone(self.hass)
        latitude, longitude = self._configured_coordinates(self.hass, self.entry)
        context_changed = (
            latitude != self.latitude
            or longitude != self.longitude
            or time_zone.key != self.time_zone.key
        )
        self.latitude = latitude
        self.longitude = longitude
        self.time_zone = time_zone
        if context_changed and self._weather_blender is not None:
            self._weather_blender.set_home_context(
                latitude=latitude, longitude=longitude, time_zone=time_zone.key
            )
        return None if True else None

    async def async_initialize(self) -> None:
        """Initialize all components including AI pipeline."""
        _LOGGER.info("Initializing Weather Fusion AI components...")
        from .entry_storage import migrate_legacy_ai_data as migrate_legacy_ai_data

        entry_count = len(self.hass.config_entries.async_entries(DOMAIN))
        migrated_files = await self.hass.async_add_executor_job(
            migrate_legacy_ai_data,
            Path(LEGACY_AI_DATA_DIR),
            self.ai_data_dir,
            entry_count,
        )
        if migrated_files:
            _LOGGER.info(
                "Migrated %d legacy learning file(s) to config entry storage",
                migrated_files,
            )
        self.ai_data_dir.mkdir(parents=True, exist_ok=True)
        from .weather_experts import WeatherExpertBlender as WeatherExpertBlender
        from .actual_tracker import ActualWeatherTracker as ActualWeatherTracker
        from .ai.forecast_corrector import (
            ForecastCorrectionPipeline as ForecastCorrectionPipeline,
        )

        cache_dir = self.ai_data_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._weather_blender = WeatherExpertBlender(
            hass=self.hass,
            data_dir=cache_dir,
            latitude=self.latitude,
            longitude=self.longitude,
            time_zone=self.time_zone.key,
            pirate_weather_api_key=self.pirate_weather_api_key,
        )
        await self._weather_blender.async_init()
        self._ai_pipeline = ForecastCorrectionPipeline(self.ai_data_dir)
        await self._ai_pipeline.async_init()
        self._ai_pipeline.apply_weights_to_blender(self._weather_blender)
        actual_history_dir = self.ai_data_dir / "history" / "actual"
        actual_history_dir.mkdir(parents=True, exist_ok=True)
        self._actual_tracker = ActualWeatherTracker(
            hass=self.hass,
            data_dir=actual_history_dir,
            sensors=self.sensors,
            time_zone=getattr(self.hass.config, "time_zone", None),
        )
        _LOGGER.info("Weather Fusion AI components initialized with AI pipeline")

    async def async_shutdown(self) -> None:
        """Shutdown and save all data."""
        _LOGGER.info("Shutting down Weather Fusion AI...")
        if self._weather_blender:
            await self._weather_blender.async_save_caches()
        if self._ai_pipeline:
            await self._ai_pipeline.shutdown()
        return None if True else None

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch, correct, and process weather data."""
        try:
            _LOGGER.debug("Updating weather data with AI corrections...")
            self._sync_home_assistant_context()
            await self._weather_blender.fetch_all_forecasts()
            raw_weather = await self._get_raw_weather(self._local_now())
            corrected_weather = await self._apply_ai_corrections(raw_weather)
            actual_data = None
            if self._actual_tracker:
                actual_data = await self._actual_tracker.track_current_weather()
            current_weather = await self._get_sensor_based_current(
                corrected_weather, actual_data
            )
            hourly_forecast = await self._get_corrected_hourly_forecast()
            daily_forecast = await self._get_daily_forecast(hourly_forecast)
            await self._track_and_learn(actual_data, raw_weather)
            await self._maybe_run_learning()
            await self._update_ai_stats()
            self._current_weather = current_weather
            self._hourly_forecast = hourly_forecast
            self._daily_forecast = daily_forecast
            self._forecast_snapshot_generated_at = datetime.now(timezone.utc)
            return {
                "current": current_weather,
                "hourly_forecast": hourly_forecast,
                "daily_forecast": daily_forecast,
                "ai_stats": self._ai_stats,
                "last_update": self._local_now().isoformat(),
            }
        except Exception as error:
            _LOGGER.error("Error updating weather data: %s", error, exc_info=True)
            raise UpdateFailed(f"Error updating weather data: {error}") from error

    async def _get_raw_weather(self, target_dt: datetime) -> Dict[str, Any]:
        """Get raw blended weather for a specific time."""
        date_key = target_dt.strftime("%Y-%m-%d")
        hour = target_dt.hour
        try:
            blended_weather = await self._weather_blender.get_blended_weather(
                date_key, hour
            )
            if not blended_weather:
                _LOGGER.warning("No blended weather data available")
                return self._get_default_weather()
            return blended_weather
        except Exception as error:
            _LOGGER.error("Error getting raw weather: %s", error)
            return self._get_default_weather()

    async def _get_sensor_based_current(
        self, api_fallback: Dict[str, Any], sensor_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Get current weather prioritizing local sensor values.

        Local sensors provide the REAL current weather.
        API data is only used as fallback for missing sensor values.
        """
        weather_result = api_fallback.copy()
        rain_sensor_configured = bool(self.sensors.get("rain"))
        if not self._actual_tracker:
            return weather_result
        try:
            if not sensor_data:
                if rain_sensor_configured:
                    weather_result.pop("precipitation", None)
                return weather_result
            sensor_fields = {
                "temperature": "temperature",
                "humidity": "humidity",
                "pressure": "pressure",
                "wind_speed": "wind_speed",
                "wind_direction": "wind_bearing",
            }
            sensors_used = []
            for sensor_key, weather_key in sensor_fields.items():
                sensor_value = sensor_data.get(sensor_key)
                if sensor_value is not None:
                    weather_result[weather_key] = sensor_value
                    sensors_used.append(sensor_key)
            current_precipitation = sensor_data.get("current_precipitation")
            if current_precipitation is not None:
                weather_result["precipitation"] = current_precipitation
                sensors_used.append("rain")
            elif rain_sensor_configured:
                weather_result.pop("precipitation", None)
            if sensors_used:
                weather_result["sensor_based"] = True
                weather_result["sensors_used"] = sensors_used
                _LOGGER.debug(
                    "Current weather from sensors: %s",
                    {
                        sensor_name: sensor_data.get(sensor_name)
                        for sensor_name in sensors_used
                    },
                )
        except Exception as error:
            _LOGGER.warning("Error getting sensor data: %s", error)
        return weather_result if True else None

    async def _apply_ai_corrections(
        self, raw_weather: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply AI corrections to raw weather data."""
        if not self._ai_pipeline or not self._ai_pipeline._initialized:
            return self._convert_to_ha_format(raw_weather)
        try:
            now = self._local_now()
            hour = now.hour
            blend_info = raw_weather.get("blend_info", {})
            experts_used = blend_info.get("experts_used", [])
            corrected_forecast = await self._ai_pipeline.process_forecast(
                raw_forecast=raw_weather,
                hour=hour,
                experts_used=experts_used,
                blend_info=blend_info,
            )
            return self._corrected_to_ha_format(corrected_forecast, raw_weather)
        except Exception as error:
            _LOGGER.error("Error applying AI corrections: %s", error)
            return self._convert_to_ha_format(raw_weather)

    def _corrected_to_ha_format(
        self, corrected, raw_weather: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Convert corrected forecast to Home Assistant format."""
        weather_code = (
            corrected.weather_code if corrected else raw_weather.get("weather_code", 0)
        )
        visibility = (
            corrected.visibility if corrected else raw_weather.get("visibility")
        )
        cloud_cover = (
            corrected.cloud_cover if corrected else raw_weather.get("cloud_cover")
        )
        condition = condition_from_wmo(
            weather_code, visibility_m=visibility, cloud_cover=cloud_cover
        )
        condition = self._adjust_condition_for_time(condition)
        weather_result = {
            "condition": condition,
            "temperature": (
                corrected.temperature if corrected else raw_weather.get("temperature")
            ),
            "humidity": (
                corrected.humidity if corrected else raw_weather.get("humidity")
            ),
            "pressure": (
                corrected.pressure if corrected else raw_weather.get("pressure")
            ),
            "wind_speed": (
                corrected.wind_speed if corrected else raw_weather.get("wind_speed")
            ),
            "wind_bearing": (
                corrected.wind_direction
                if corrected
                else raw_weather.get("wind_direction")
            ),
            "cloud_coverage": (
                corrected.cloud_cover if corrected else raw_weather.get("cloud_cover")
            ),
            "precipitation": (
                corrected.precipitation
                if corrected
                else raw_weather.get("precipitation")
            ),
            "visibility": (
                corrected.visibility if corrected else raw_weather.get("visibility")
            ),
            "uv_index": (
                corrected.uv_index if corrected else raw_weather.get("uv_index")
            ),
            "weather_code": weather_code,
        }
        if corrected:
            weather_result["ai_corrected"] = True
            weather_result["ai_confidence"] = corrected.overall_confidence
            weather_result["cloud_type"] = corrected.cloud_type
            weather_result["corrections_applied"] = len(corrected.corrections_summary)
            weather_result["blend_info"] = {
                "experts_used": corrected.experts_used,
                "cloud_type": corrected.cloud_type,
                "confidence": corrected.overall_confidence,
            }
        else:
            weather_result["ai_corrected"] = False
            weather_result["blend_info"] = raw_weather.get("blend_info", {})
        return weather_result if True else None

    def _convert_to_ha_format(self, raw_weather: Dict[str, Any]) -> Dict[str, Any]:
        """Convert raw weather to Home Assistant format (no AI corrections)."""
        weather_code = raw_weather.get("weather_code", 0)
        condition = condition_from_wmo(
            weather_code,
            visibility_m=raw_weather.get("visibility"),
            cloud_cover=raw_weather.get("cloud_cover"),
        )
        condition = self._adjust_condition_for_time(condition)
        return {
            "condition": condition,
            "temperature": raw_weather.get("temperature"),
            "humidity": raw_weather.get("humidity"),
            "pressure": raw_weather.get("pressure"),
            "wind_speed": raw_weather.get("wind_speed"),
            "wind_bearing": raw_weather.get("wind_direction"),
            "cloud_coverage": raw_weather.get("cloud_cover"),
            "precipitation": raw_weather.get("precipitation"),
            "visibility": raw_weather.get("visibility"),
            "uv_index": raw_weather.get("uv_index"),
            "weather_code": weather_code,
            "ai_corrected": False,
            "blend_info": raw_weather.get("blend_info", {}),
        }

    async def _get_corrected_hourly_forecast(self) -> List[Dict[str, Any]]:
        """Get AI-corrected hourly forecast for next 7 days (168 hours)."""
        hourly_forecast = []
        utc_hour_start = (
            self._local_now()
            .astimezone(timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
        )
        for lead_hour in range(168):
            target_datetime = (utc_hour_start + timedelta(hours=lead_hour)).astimezone(
                self.time_zone
            )
            date_key = target_datetime.strftime("%Y-%m-%d")
            hour = target_datetime.hour
            if lead_hour <= 6:
                base_confidence = 95 - lead_hour
            elif lead_hour <= 24:
                base_confidence = 89 - (lead_hour - 6) * 0.5
            elif lead_hour <= 48:
                base_confidence = 80 - (lead_hour - 24) * 0.6
            elif lead_hour <= 72:
                base_confidence = 66 - (lead_hour - 48) * 0.5
            elif lead_hour <= 120:
                base_confidence = 54 - (lead_hour - 72) * 0.2
            else:
                base_confidence = max(40, 44 - (lead_hour - 120) * 0.1)
            try:
                raw_forecast = None
                raw_forecast = await self._weather_blender.get_blended_weather(
                    date_key, hour
                )
                if not raw_forecast:
                    continue
                if self._ai_pipeline and self._ai_pipeline._initialized:
                    blend_info = raw_forecast.get("blend_info", {})
                    experts_used = blend_info.get("experts_used", [])
                    corrected_forecast = await self._ai_pipeline.process_forecast(
                        raw_forecast=raw_forecast,
                        hour=hour,
                        experts_used=experts_used,
                        blend_info=blend_info,
                    )
                    weather_code = corrected_forecast.weather_code
                    condition = condition_from_wmo(
                        weather_code,
                        visibility_m=corrected_forecast.visibility,
                        cloud_cover=corrected_forecast.cloud_cover,
                    )
                    condition = self._adjust_condition_for_time(
                        condition, target_datetime
                    )
                    ai_confidence = corrected_forecast.overall_confidence or 0
                    confidence_bonus = min(8, ai_confidence * 0.08)
                    confidence = min(98, base_confidence + confidence_bonus)
                    hourly_forecast.append(
                        {
                            "datetime": target_datetime.isoformat(),
                            "condition": condition,
                            "temperature": corrected_forecast.temperature,
                            "humidity": corrected_forecast.humidity,
                            "pressure": corrected_forecast.pressure,
                            "wind_speed": corrected_forecast.wind_speed,
                            "wind_bearing": corrected_forecast.wind_direction,
                            "cloud_coverage": corrected_forecast.cloud_cover,
                            "precipitation": corrected_forecast.precipitation,
                            "precipitation_probability": corrected_forecast.precipitation_probability,
                            "visibility": corrected_forecast.visibility,
                            "ai_confidence": corrected_forecast.overall_confidence,
                            "confidence": round(confidence),
                        }
                    )
                    continue
                weather_code = raw_forecast.get("weather_code", 0)
                condition = condition_from_wmo(
                    weather_code,
                    visibility_m=raw_forecast.get("visibility"),
                    cloud_cover=raw_forecast.get("cloud_cover"),
                )
                condition = self._adjust_condition_for_time(condition, target_datetime)
                hourly_forecast.append(
                    {
                        "datetime": target_datetime.isoformat(),
                        "condition": condition,
                        "temperature": raw_forecast.get("temperature"),
                        "humidity": raw_forecast.get("humidity"),
                        "pressure": raw_forecast.get("pressure"),
                        "wind_speed": raw_forecast.get("wind_speed"),
                        "wind_bearing": raw_forecast.get("wind_direction"),
                        "cloud_coverage": raw_forecast.get("cloud_cover"),
                        "precipitation": raw_forecast.get("precipitation"),
                        "precipitation_probability": raw_forecast.get(
                            "precipitation_probability"
                        ),
                        "visibility": raw_forecast.get("visibility"),
                        "confidence": round(base_confidence),
                    }
                )
            except Exception as error:
                _LOGGER.debug(
                    "Error getting forecast for %s: %s", target_datetime, error
                )
                if raw_forecast:
                    weather_code = raw_forecast.get("weather_code", 0)
                    condition = condition_from_wmo(
                        weather_code,
                        visibility_m=raw_forecast.get("visibility"),
                        cloud_cover=raw_forecast.get("cloud_cover"),
                    )
                    condition = self._adjust_condition_for_time(
                        condition, target_datetime
                    )
                    hourly_forecast.append(
                        {
                            "datetime": target_datetime.isoformat(),
                            "condition": condition,
                            "temperature": raw_forecast.get("temperature"),
                            "humidity": raw_forecast.get("humidity"),
                            "pressure": raw_forecast.get("pressure"),
                            "wind_speed": raw_forecast.get("wind_speed"),
                            "wind_bearing": raw_forecast.get("wind_direction"),
                            "cloud_coverage": raw_forecast.get("cloud_cover"),
                            "precipitation": raw_forecast.get("precipitation"),
                            "precipitation_probability": raw_forecast.get(
                                "precipitation_probability"
                            ),
                            "visibility": raw_forecast.get("visibility"),
                            "confidence": round(base_confidence),
                        }
                    )
                continue
            continue
        return hourly_forecast

    async def _get_daily_forecast(
        self, hourly: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Generate daily forecast from hourly data."""
        daily_forecast = []
        if not hourly:
            return daily_forecast
        hours_by_date = {}
        for hour_forecast in hourly:
            timestamp_text = hour_forecast.get("datetime", "")
            if timestamp_text:
                try:
                    parsed_datetime = datetime.fromisoformat(
                        timestamp_text.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=self.time_zone)
                date_key = parsed_datetime.astimezone(self.time_zone).date().isoformat()
                if date_key not in hours_by_date:
                    hours_by_date[date_key] = []
                hours_by_date[date_key].append(hour_forecast)
        confidence_by_day = {0: 95, 1: 90, 2: 82, 3: 72, 4: 62, 5: 52, 6: 45}
        for day_offset, date_key in enumerate(sorted(hours_by_date.keys())[:7]):
            day_hours = hours_by_date[date_key]
            if not day_hours:
                continue
            temperatures = [
                hour_data.get("temperature")
                for hour_data in day_hours
                if hour_data.get("temperature") is not None
            ]
            precipitation_amounts = [
                hour_data.get("precipitation", 0) or 0 for hour_data in day_hours
            ]
            precipitation_probabilities = [
                hour_data.get("precipitation_probability", 0) or 0
                for hour_data in day_hours
            ]
            conditions = [
                _normalize_daily_condition(hour_data.get("condition"))
                for hour_data in day_hours
                if hour_data.get("datetime")
                if 6 <= int(hour_data["datetime"][11:13]) <= 18
            ]
            if not conditions:
                conditions = [
                    _normalize_daily_condition(hour_data.get("condition"))
                    for hour_data in day_hours
                    if hour_data.get("condition")
                ]
            dominant_condition = DEFAULT_CONDITION
            if conditions:
                dominant_condition = max(set(conditions), key=conditions.count)
            base_confidence = confidence_by_day.get(day_offset, 40)
            mean_ai_confidence = (
                sum((hour_data.get("ai_confidence", 0) for hour_data in day_hours))
                / len(day_hours)
                if day_hours
                else 0
            )
            if mean_ai_confidence > 0:
                confidence_bonus = min(10, mean_ai_confidence * 0.1)
                confidence = min(98, base_confidence + confidence_bonus)
            else:
                confidence = base_confidence
            daily_forecast.append(
                {
                    "datetime": datetime.fromisoformat(f"{date_key}T12:00:00")
                    .replace(tzinfo=self.time_zone)
                    .isoformat(),
                    "condition": dominant_condition,
                    "templow": round(min(temperatures), 1) if temperatures else None,
                    "native_temperature": (
                        round(max(temperatures), 1) if temperatures else None
                    ),
                    "precipitation": round(sum(precipitation_amounts), 1),
                    "precipitation_probability": round(
                        max(precipitation_probabilities)
                    ),
                    "wind_speed": (
                        day_hours[len(day_hours) // 2].get("wind_speed")
                        if day_hours
                        else None
                    ),
                    "wind_bearing": (
                        day_hours[len(day_hours) // 2].get("wind_bearing")
                        if day_hours
                        else None
                    ),
                    "confidence": round(confidence),
                }
            )
            continue
        return daily_forecast

    async def _track_and_learn(
        self,
        actual: Optional[Dict[str, Any]] = None,
        raw_forecast: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record actual weather against each expert's own forecast."""
        if not self._actual_tracker:
            return
        try:
            if actual is None:
                actual = await self._actual_tracker.track_current_weather()
            if not actual or not self._ai_pipeline:
                return None
            if not raw_forecast:
                now = self._local_now()
                raw_forecast = await self._weather_blender.get_blended_weather(
                    now.strftime("%Y-%m-%d"), now.hour
                )
            if not raw_forecast:
                return
            blend_info = raw_forecast.get("blend_info", {})
            expert_forecasts = blend_info.get("expert_forecasts") or {}
            cloud_type = blend_info.get("cloud_type") or raw_forecast.get("cloud_type")
            if not expert_forecasts:
                return
            for expert_name, expert_forecast in expert_forecasts.items():
                await self._ai_pipeline.record_actual(
                    actual=actual,
                    forecast=expert_forecast,
                    expert=expert_name,
                    cloud_type=cloud_type,
                )
        except Exception as error:
            _LOGGER.debug("Error in track_and_learn: %s", error)
        return None if True else None

    async def _maybe_run_learning(self) -> None:
        """Run learning cycle if enough time has passed."""
        if not self._ai_pipeline:
            return
        now = self._local_now()
        if self._last_learning_run:
            elapsed_hours = (now - self._last_learning_run).total_seconds() / 3600
            if elapsed_hours < self._learning_interval_hours:
                return
        try:
            _LOGGER.info("Running AI learning cycle...")
            learning_result = await self._ai_pipeline.run_learning_cycle()
            self._ai_pipeline.apply_weights_to_blender(self._weather_blender)
            self._last_learning_run = now
            _LOGGER.info(
                "Learning cycle complete: %s",
                learning_result.get("weight_learning", {}).get("reason", "unknown"),
            )
        except Exception as error:
            _LOGGER.error("Error in learning cycle: %s", error)
        return None if True else None

    async def _update_ai_stats(self) -> None:
        """Update AI statistics for display."""
        if not self._ai_pipeline:
            return
        try:
            pipeline_stats = self._ai_pipeline.get_pipeline_stats()
            tracker_stats = pipeline_stats.get("tracker", {})
            learner_stats = pipeline_stats.get("learner", {})
            active_sources = tracker_stats.get("expert_buckets", 0)
            if active_sources == 0:
                active_sources = len(
                    learner_stats.get("weights_summary", {}).get("mixed", {})
                )
            if active_sources == 0:
                active_sources = 4
            learning_hours = 0
            learning_days = 0
            if self._actual_tracker:
                learning_hours = self._actual_tracker.history_hours
                learning_days = self._actual_tracker.history_days
            pattern_stats = pipeline_stats.get("patterns", {})
            corrector_stats = pipeline_stats.get("corrector", {})
            stats_update = {
                "learning_hours": learning_hours,
                "learning_days": learning_days,
                "accuracy_percent": learner_stats.get("average_accuracy_7d"),
                "active_sources": active_sources,
                "last_learning": learner_stats.get("last_learning"),
                "calibration_complete": learning_days >= LEARNING_MIN_DAYS,
                "patterns_detected": pattern_stats.get(
                    "total_patterns", self._ai_stats.get("patterns_detected", 0)
                ),
                "corrections_applied": corrector_stats.get(
                    "corrections_applied", self._ai_stats.get("corrections_applied", 0)
                ),
            }
            self._ai_stats.update(stats_update)
            self._ai_stats.pop("error", None)
        except Exception as error:
            _LOGGER.debug("Error updating AI stats: %s", error)
            self._ai_stats["error"] = str(error)
        return None if True else None

    def _get_default_weather(self) -> Dict[str, Any]:
        """Return default weather data when no sources available."""
        return {
            "condition": DEFAULT_CONDITION,
            "temperature": None,
            "humidity": None,
            "pressure": None,
            "wind_speed": None,
            "wind_bearing": None,
            "cloud_coverage": 50,
            "precipitation": 0,
            "ai_corrected": False,
            "blend_info": {"source": "default"},
        }

    def _is_night(self, target_dt: Optional[datetime] = None) -> bool:
        """Check if it's night based on sun.sun entity or time calculation.

        Args:
            target_dt: Target datetime to check. If None, checks current time.

        Returns:
            True if sun is below horizon, False otherwise.
        """
        sun_state = self.hass.states.get(SUN_ENTITY_ID)
        if target_dt is None:
            if sun_state is None:
                hour = self._local_now().hour
                return hour < 6 or hour >= 20
            return sun_state.state == SUN_STATE_BELOW_HORIZON
        try:
            from astral.sun import sunrise as sunrise, sunset as sunset
            from homeassistant.helpers.sun import (
                get_astral_observer as get_astral_observer,
            )
            from homeassistant.util import dt as dt_util

            local_target = target_dt
            if local_target.tzinfo is None:
                local_target = local_target.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            local_target = dt_util.as_local(local_target)
            observer = get_astral_observer(self.hass)
            sunrise_datetime = dt_util.as_local(sunrise(observer, local_target.date()))
            sunset_datetime = dt_util.as_local(sunset(observer, local_target.date()))
            return local_target < sunrise_datetime or local_target >= sunset_datetime
        except Exception as error:
            _LOGGER.debug("Error calculating daily sun position: %s", error)
        if sun_state is not None:
            next_rising_iso = sun_state.attributes.get("next_rising")
            next_setting_iso = sun_state.attributes.get("next_setting")
            if next_rising_iso and next_setting_iso:
                try:
                    from homeassistant.util import dt as dt_util

                    local_target = target_dt
                    if local_target.tzinfo is None:
                        local_target = local_target.replace(
                            tzinfo=dt_util.DEFAULT_TIME_ZONE
                        )
                    local_target = dt_util.as_local(local_target)
                    next_rising = dt_util.parse_datetime(next_rising_iso)
                    next_setting = dt_util.parse_datetime(next_setting_iso)
                    if next_rising and next_setting:
                        next_rising = dt_util.as_local(next_rising)
                        next_setting = dt_util.as_local(next_setting)
                        day_sunrise = local_target.replace(
                            hour=next_rising.hour,
                            minute=next_rising.minute,
                            second=next_rising.second,
                            microsecond=0,
                        )
                        day_sunset = local_target.replace(
                            hour=next_setting.hour,
                            minute=next_setting.minute,
                            second=next_setting.second,
                            microsecond=0,
                        )
                        return local_target < day_sunrise or local_target >= day_sunset
                except Exception as error:
                    _LOGGER.debug("Error using sun entity fallback: %s", error)
        return target_dt.hour < 6 or target_dt.hour >= 20

    def _adjust_condition_for_time(
        self, condition: str, target_dt: Optional[datetime] = None
    ) -> str:
        "Adjust weather condition for time of day.\n\nConverts day conditions (like 'sunny') to night equivalents\n(like 'clear-night') after sunset.\n\nArgs:\n    condition: The weather condition to adjust.\n    target_dt: Target datetime. If None, uses current time.\n\nReturns:\n    Adjusted condition string.\n"
        if self._is_night(target_dt):
            return DAY_TO_NIGHT_CONDITION.get(condition, condition)
        return condition

    @property
    def current_weather(self) -> Dict[str, Any]:
        """Get current weather data."""
        return self._current_weather

    @property
    def hourly_forecast(self) -> List[Dict[str, Any]]:
        """Get hourly forecast."""
        return self._hourly_forecast

    @property
    def forecast_snapshot_generated_at(self) -> Optional[datetime]:
        """Return when the current public forecast snapshot was generated."""
        return self._forecast_snapshot_generated_at

    @property
    def daily_forecast(self) -> List[Dict[str, Any]]:
        """Get daily forecast."""
        return self._daily_forecast

    @property
    def ai_stats(self) -> Dict[str, Any]:
        """Get AI statistics."""
        return self._ai_stats

    @property
    def learning_stats(self) -> Dict[str, Any]:
        """Get learning statistics (alias for ai_stats)."""
        return self._ai_stats

    @property
    def weather_blender(self):
        """Get weather blender instance."""
        return self._weather_blender

    @property
    def actual_tracker(self):
        """Get the entry-scoped actual-weather tracker."""
        return self._actual_tracker

    @property
    def ai_pipeline(self):
        """Get AI pipeline instance."""
        return self._ai_pipeline
