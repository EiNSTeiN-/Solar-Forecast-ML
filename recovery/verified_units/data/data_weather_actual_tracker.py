# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Actual Tracker V16.2.0 - Track Actual Weather from Local Sensors

Tracks actual weather conditions from local sensors and stores them in DB.
Used for comparing forecasts vs actuals and detecting frost/snow conditions.

Features:
- Hourly weather tracking from local sensors
- Cloud cover estimation from radiation or production
- Frost detection with notification support
- Advanced snow tracking with overnight detection and production-based melt learning

Database Table: hourly_weather_actual
@zara V16.1 - Enhanced snow detection
"""
import logging
import math
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, Optional, List
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)
SNOW_WEATHER_CODES = {
    71: "light_snow",
    73: "moderate_snow",
    75: "heavy_snow",
    77: "snow_grains",
    85: "light_showers",
    86: "heavy_showers",
}
FREEZING_RAIN_CODES = {66: "light_freezing_rain", 67: "heavy_freezing_rain"}
SNOW_TEMP_MAX_C = 2.0
FREEZING_RAIN_TEMP_MAX_C = 0.0
DATA_RETENTION_DAYS = 365
WEATHER_DB_WRITE_TIMEOUT_SECONDS = 10.0
RAIN_RATE_HOURS_PER_SAMPLE = 1.0


def classify_rain_sensor_state(
    state: Any,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return precipitation semantics from a Home Assistant rain entity."""
    if state is None:
        return (None, None, None)
    attributes = getattr(state, "attributes", None) or {}
    normalized_unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
    normalized_unit = normalized_unit.replace(" ", "")
    if normalized_unit in {"mmh", "mm/h", "mm/hr"}:
        return ("rate", "mm/h", "rate_only")
    if normalized_unit == "mm":
        state_class = str(attributes.get("state_class") or "").strip().lower()
        if state_class in {"total", "total_increasing"}:
            return (None, None, None)
        return ("amount", "mm", "direct_amount")
    return (None, None, None)


def integrate_rain_rate_to_hourly_mm(
    rate_mm_h: Any, hours: float = RAIN_RATE_HOURS_PER_SAMPLE
) -> Optional[float]:
    """Convert a rain-rate snapshot into an hourly precipitation amount."""
    try:
        rain_rate = float(rate_mm_h)
    except (TypeError, ValueError):
        return
    try:
        sample_hours = float(hours)
    except (TypeError, ValueError):
        sample_hours = RAIN_RATE_HOURS_PER_SAMPLE
    sample_hours = min(max(sample_hours, 0.0), RAIN_RATE_HOURS_PER_SAMPLE)
    return round(max(0.0, rain_rate) * sample_hours, 2)


def persistable_rain_observation(
    value: Any, semantics: Optional[str], unit: Optional[str], quality: Optional[str]
) -> tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    """Store rain-rate snapshots as hourly millimetres for daily history."""
    if semantics == "rate" and unit == "mm/h" and (quality == "rate_only"):
        rain_amount = integrate_rain_rate_to_hourly_mm(value)
        if rain_amount is None:
            return (None, semantics, unit, quality)
        return (rain_amount, "amount", "mm", "rate_integrated")
    try:
        numeric_amount = round(float(value), 2)
    except (TypeError, ValueError):
        return (None, semantics, unit, quality)
    return (numeric_amount, semantics, unit, quality)


class WeatherActualTracker(DataManagerIO):
    """Tracks actual weather conditions from local sensors. @zara

    All data stored in hourly_weather_actual table.
    """

    def __init__(
        self, hass: HomeAssistant, db_manager: DatabaseManager, config_entry=None
    ):
        "Initialize weather actual tracker. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for data persistence\n    config_entry: Config entry with sensor mappings\n"
        super().__init__(hass, db_manager)
        self._last_frost_notification_hour = None
        self._last_snow_notification_key = None
        self._last_fog_notification_hour = None
        self._last_retention_cleanup_cutoff = None
        self._radiation_samples = {}
        self._panel_groups = []
        if config_entry:
            from ..const import (
                CONF_TEMP_SENSOR as CONF_TEMP_SENSOR,
                CONF_HUMIDITY_SENSOR as CONF_HUMIDITY_SENSOR,
                CONF_WIND_SENSOR as CONF_WIND_SENSOR,
                CONF_RAIN_SENSOR as CONF_RAIN_SENSOR,
                CONF_PRESSURE_SENSOR as CONF_PRESSURE_SENSOR,
                CONF_SOLAR_RADIATION_SENSOR as CONF_SOLAR_RADIATION_SENSOR,
                CONF_LUX_SENSOR as CONF_LUX_SENSOR,
                CONF_WEATHER_ENTITY as CONF_WEATHER_ENTITY,
            )

            config_data = config_entry.data
            self.sensor_mapping = {
                "temperature_c": config_data.get(CONF_TEMP_SENSOR),
                "humidity_percent": config_data.get(CONF_HUMIDITY_SENSOR),
                "wind_speed_ms": config_data.get(CONF_WIND_SENSOR),
                "precipitation_mm": config_data.get(CONF_RAIN_SENSOR),
                "pressure_hpa": config_data.get(CONF_PRESSURE_SENSOR),
                "solar_radiation_wm2": config_data.get(CONF_SOLAR_RADIATION_SENSOR),
                "lux": config_data.get(CONF_LUX_SENSOR),
                "condition": config_data.get(CONF_WEATHER_ENTITY),
            }
            self.weather_entity_id = config_data.get(CONF_WEATHER_ENTITY)
            self.has_solar_radiation_sensor = (
                config_data.get(CONF_SOLAR_RADIATION_SENSOR) is not None
            )
        else:
            self.sensor_mapping = {}
            self.weather_entity_id = None
            self.has_solar_radiation_sensor = False
        _LOGGER.debug("WeatherActualTracker initialized")

    def set_panel_groups(self, panel_groups: List[Any]) -> None:
        """Set panel groups for per-group snow tracking. @zara V17.0.0"""
        self._panel_groups = panel_groups or []
        if self._panel_groups:
            _LOGGER.info(
                "Per-group snow tracking enabled for %d groups: %s",
                len(self._panel_groups),
                [panel_group.get("name", "?") for panel_group in self._panel_groups],
            )
        return None if True else None

    async def track_current_weather(self) -> dict[str, Any]:
        "Track current weather conditions from local sensors. @zara\n\nReturns:\n    Dictionary with weather data\n"
        await self.ensure_initialized()
        observed_at = dt_util.now()
        current_date = observed_at.strftime("%Y-%m-%d")
        current_hour = observed_at.hour
        previous_hour = observed_at - timedelta(hours=1)
        await self.finalize_hour_radiation(
            previous_hour.strftime("%Y-%m-%d"), previous_hour.hour
        )
        weather_readings = await self._read_sensors()
        if not weather_readings:
            return {}
        for data_field, database_field in (
            ("temperature_c", "temperature_c"),
            ("solar_radiation_wm2", "solar_radiation_wm2"),
            ("lux", "lux"),
        ):
            current_reading = weather_readings.get(data_field)
            if current_reading is not None:
                sensor_frozen = await self._detect_sensor_freeze(
                    current_date, current_hour, current_reading, database_field
                )
                if sensor_frozen:
                    weather_readings.pop(data_field, None)
        weather_readings["timestamp"] = observed_at.isoformat()
        weather_readings["source"] = "homeassistant_sensors"
        cloud_cover = await self._estimate_cloud_cover(
            weather_readings, current_date, current_hour
        )
        if cloud_cover is not None:
            weather_readings["cloud_cover_percent"] = cloud_cover
        frost_result = await self._detect_frost(
            weather_readings, current_date, current_hour
        )
        if frost_result:
            weather_readings.update(frost_result)
        snow_result = await self._check_snow_conditions(
            weather_readings, current_date, current_hour
        )
        if snow_result:
            weather_readings.update(snow_result)
        if self._panel_groups and snow_result:
            snow_by_group = await self._check_snow_conditions_per_group(
                weather_readings, snow_result, current_date, current_hour
            )
            weather_readings["snow_per_group"] = snow_by_group
        fog_result = await self._detect_fog(
            weather_readings, current_date, current_hour
        )
        if fog_result:
            weather_readings.update(fog_result)
        await self._save_hourly_data(current_date, current_hour, weather_readings)
        return weather_readings

    def _read_numeric_sensor(self, field: str) -> Optional[float]:
        """Read one configured numeric sensor, None when unmapped or unavailable."""
        entity_id = self.sensor_mapping.get(field)
        if not entity_id:
            return
        try:
            sensor_state = self.hass.states.get(entity_id)
            if sensor_state is None or sensor_state.state in (
                "unavailable",
                "unknown",
                None,
            ):
                return None
            sensor_value = float(sensor_state.state)
        except (ValueError, TypeError, AttributeError):
            return
        if not math.isfinite(sensor_value):
            return
        return round(sensor_value, 2)

    async def sample_solar_radiation(
        self, now: Optional[datetime] = None
    ) -> Optional[float]:
        """Collect one in-hour radiation sample; no-op without a radiation sensor. @zara"""
        if not self.has_solar_radiation_sensor:
            return
        sensor_value = self._read_numeric_sensor("solar_radiation_wm2")
        if sensor_value is None:
            return
        now = now or dt_util.now()
        hour_key = (now.strftime("%Y-%m-%d"), now.hour)
        self._radiation_samples.setdefault(hour_key, []).append(sensor_value)
        previous_hour = now - timedelta(hours=1)
        retained_hours = {
            hour_key,
            (previous_hour.strftime("%Y-%m-%d"), previous_hour.hour),
        }
        for expired_hour in [
            sample_key
            for sample_key in self._radiation_samples
            if sample_key not in retained_hours
        ]:
            self._radiation_samples.pop(expired_hour, None)
        return sensor_value

    async def finalize_hour_radiation(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Store the hour's radiation as the mean of its in-hour samples. @zara

        The snapshot written at h:00 stays in place until the hour is over;
        the sensor-derived cloud cover is re-derived from the mean.
        """
        radiation_samples = self._radiation_samples.pop((date_str, int(hour)), None)
        if not radiation_samples:
            return
        mean_radiation = round(sum(radiation_samples) / len(radiation_samples), 2)
        cloud_cover = await self._calculate_cloud_from_radiation(
            mean_radiation, date_str, int(hour)
        )
        try:
            async with self.db.transaction(
                deadline=monotonic() + WEATHER_DB_WRITE_TIMEOUT_SECONDS
            ):
                await self.db.execute(
                    "INSERT INTO hourly_weather_actual\n                       (date, hour, solar_radiation_wm2, cloud_cover_percent,\n                        cloud_cover_source, source, timestamp)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(date, hour) DO UPDATE SET\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           cloud_cover_percent = CASE\n                               WHEN excluded.cloud_cover_percent IS NOT NULL\n                                    AND hourly_weather_actual.cloud_cover_source = 'sensor_calculated'\n                               THEN excluded.cloud_cover_percent\n                               ELSE hourly_weather_actual.cloud_cover_percent\n                           END",
                    (
                        date_str,
                        int(hour),
                        mean_radiation,
                        cloud_cover,
                        "sensor_calculated" if cloud_cover is not None else None,
                        "homeassistant_sensors",
                        dt_util.now().isoformat(),
                    ),
                    auto_commit=False,
                )
        except Exception as error:
            _LOGGER.error("Error storing hourly radiation mean: %s", error)
            return
        _LOGGER.debug(
            "Hourly radiation %s %02d: mean=%.1f W/m² from %d samples",
            date_str,
            int(hour),
            mean_radiation,
            len(radiation_samples),
        )
        return mean_radiation

    async def _read_sensors(self) -> dict[str, Any]:
        """Read current values from all configured sensors. @zara"""
        readings = {}
        valid_sensor_count = 0
        for data_field, entity_id in self.sensor_mapping.items():
            if not entity_id:
                continue
            try:
                sensor_state = self.hass.states.get(entity_id)
                if sensor_state is None or sensor_state.state in (
                    "unavailable",
                    "unknown",
                    None,
                ):
                    continue
                sensor_value = sensor_state.state
                if data_field == "condition":
                    readings[data_field] = sensor_value
                    valid_sensor_count += 1
                else:
                    try:
                        numeric_value = float(sensor_value)
                        readings[data_field] = round(numeric_value, 2)
                        valid_sensor_count += 1
                        if data_field == "precipitation_mm":
                            rain_semantics, rain_unit, rain_quality = (
                                classify_rain_sensor_state(sensor_state)
                            )
                            rain_amount, rain_semantics, rain_unit, rain_quality = (
                                persistable_rain_observation(
                                    numeric_value,
                                    rain_semantics,
                                    rain_unit,
                                    rain_quality,
                                )
                            )
                            if rain_amount is not None:
                                readings[data_field] = rain_amount
                            readings["precipitation_semantics"] = rain_semantics
                            readings["precipitation_unit"] = rain_unit
                            readings["precipitation_quality"] = rain_quality
                    except (ValueError, TypeError):
                        continue
            except Exception:
                continue
        return readings if valid_sensor_count > 0 else {}

    async def _estimate_cloud_cover(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[float]:
        """Estimate cloud cover from various sources. @zara

        Priority:
        1. Sensor-based calculation (when solar radiation sensor configured)
        2. Production-based estimation (when no sensor configured)
        3. Forecast fallback
        """
        cloud_cover = None
        if self.has_solar_radiation_sensor:
            radiation_wm2 = weather_data.get("solar_radiation_wm2")
            if radiation_wm2 is not None:
                cloud_cover = await self._calculate_cloud_from_radiation(
                    radiation_wm2, date_str, hour
                )
                if cloud_cover is not None:
                    weather_data["cloud_cover_source"] = "sensor_calculated"
                    return cloud_cover
        if not self.has_solar_radiation_sensor:
            production_hour = hour - 1 if hour > 0 else hour
            cloud_cover = await self._estimate_cloud_from_production(
                date_str, production_hour
            )
            if cloud_cover is not None:
                weather_data["cloud_cover_source"] = "production_estimated"
                return cloud_cover
        cloud_cover = await self._get_forecast_cloud_cover(date_str, hour)
        if cloud_cover is not None:
            weather_data["cloud_cover_source"] = "forecast_fallback"
            return cloud_cover

    async def _calculate_cloud_from_radiation(
        self, measured_wm2: float, date_str: str, hour: int
    ) -> Optional[float]:
        """Calculate cloud cover from measured radiation vs theoretical max. @zara"""
        try:
            row = await self.fetch_one(
                "SELECT clear_sky_radiation_wm2\n                   FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = ?",
                (date_str, hour),
            )
            if not row or not row[0] or row[0] <= 0:
                return None
            clear_sky_radiation = row[0]
            observed_ratio = measured_wm2 / clear_sky_radiation
            observed_ratio = max(0.0, min(1.0, observed_ratio))
            cloud_cover = (1.0 - observed_ratio) * 100.0
            return round(cloud_cover, 1)
        except Exception:
            return

    async def _estimate_cloud_from_production(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Estimate cloud cover from actual production vs theoretical. @zara"""
        try:
            row = await self.fetch_one(
                "SELECT actual_kwh, pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date = ? AND hp.target_hour = ?",
                (date_str, hour),
            )
            if not row or not row[0] or (not row[1]) or (row[1] < 0.01):
                return None
            actual_kwh = row[0]
            theoretical_kwh = row[1]
            observed_ratio = actual_kwh / theoretical_kwh
            observed_ratio = max(0.0, min(1.0, observed_ratio))
            cloud_cover = (1.0 - observed_ratio) * 100.0
            return round(cloud_cover, 1)
        except Exception:
            return

    async def _get_forecast_cloud_cover(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Get cloud cover from forecast as fallback. @zara"""
        try:
            row = await self.fetch_one(
                "SELECT clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if row and row[0] is not None:
                return float(row[0])
        except Exception:
            return
        return None if True else None

    async def _detect_frost(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Detect frost conditions on solar panels. @zara"""
        try:
            temperature = weather_data.get("temperature_c")
            relative_humidity = weather_data.get("humidity_percent")
            if temperature is None:
                return
            below_freezing = False
            frost_score = 0.0
            confidence = 0.0
            if temperature <= 0:
                below_freezing = True
                frost_score = min(1.0, abs(temperature) / 5.0)
                confidence = 0.9
                if relative_humidity is not None and relative_humidity > 80:
                    frost_score = min(1.0, frost_score + 0.2)
            frost_type = None
            if frost_score > 0.7:
                frost_type = "heavy_frost"
            elif frost_score > 0.3:
                frost_type = "light_frost"
            elif below_freezing:
                frost_type = "possible_frost"
            if frost_type == "heavy_frost":
                await self._send_frost_notification(
                    frost_score, temperature, relative_humidity, date_str, hour
                )
            return {
                "frost_detected": frost_type,
                "frost_score": round(frost_score, 2),
                "frost_confidence": round(confidence, 2),
            }
        except Exception as error:
            _LOGGER.error("Error during frost detection: %s", error)
            return

    async def _send_frost_notification(
        self,
        frost_score: float,
        temp_c: float,
        humidity: Optional[float],
        date_str: str,
        hour: int,
    ) -> None:
        """Send notification for heavy frost. @zara"""
        try:
            notification_hour = f"{date_str}_{hour}"
            if self._last_frost_notification_hour == notification_hour:
                return
            from ..const import DOMAIN as DOMAIN

            notification_service = self.hass.data.get(DOMAIN, {}).get(
                "notification_service"
            )
            if not notification_service:
                return
            await notification_service.show_frost_warning(
                frost_score=frost_score,
                temperature_c=temp_c,
                dewpoint_c=None,
                frost_margin_c=temp_c,
                hour=hour,
                confidence=0.9,
            )
            self._last_frost_notification_hour = notification_hour
            _LOGGER.info(
                "Frost warning notification sent for %s %02d:00", date_str, hour
            )
        except Exception as error:
            _LOGGER.error("Error sending frost notification: %s", error)
        return None if True else None

    async def _detect_fog(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        "Detect fog from local temperature and humidity only. @zara\n\n``weather_forecast.fog_detected`` is a forecast label, not a\nmeasurement. Without both sensors there is no detection and no\nnotification.\n\nArgs:\n    weather_data: Current sensor readings\n    date_str: Current date (YYYY-MM-DD)\n    hour: Current hour (0-23)\n\nReturns:\n    Dict with fog detection results, or None when not detected\n"
        try:
            temperature = weather_data.get("temperature_c")
            relative_humidity = weather_data.get("humidity_percent")
            if temperature is None or relative_humidity is None:
                return None
            dewpoint = None
            if relative_humidity > 50:
                dewpoint = temperature - (100 - relative_humidity) / 5.0
            cloud_cover = weather_data.get("cloud_cover_percent")
            if cloud_cover is None:
                cloud_cover = 0.0
            from .data_weather_corrector import (
                detect_fog_from_meteorology as detect_fog_from_meteorology,
            )

            fog_detected, fog_confidence, detected_fog_type = (
                detect_fog_from_meteorology(
                    temperature, dewpoint, relative_humidity, cloud_cover, hour
                )
            )
            if not fog_detected:
                return
            await self._send_fog_notification(
                visibility_m=None,
                temp_c=temperature,
                humidity=relative_humidity,
                fog_type=detected_fog_type,
                date_str=date_str,
                hour=hour,
            )
            return {
                "fog_detected": True,
                "fog_type": detected_fog_type,
                "fog_confidence": round(fog_confidence, 2),
            }
        except Exception as error:
            _LOGGER.error("Error during fog detection: %s", error)
            return

    async def _send_fog_notification(
        self,
        visibility_m: Optional[float],
        temp_c: float,
        humidity: float,
        fog_type: str,
        date_str: str,
        hour: int,
    ) -> None:
        """Send notification for fog conditions. @zara

        Follows the same pattern as _send_frost_notification():
        - Deduplication via hour_key
        - NotificationService from hass.data
        - fog_type normalization for show_fog_warning()
        """
        try:
            notification_hour = f"{date_str}_{hour}"
            if self._last_fog_notification_hour == notification_hour:
                return
            from ..const import DOMAIN as DOMAIN

            notification_service = self.hass.data.get(DOMAIN, {}).get(
                "notification_service"
            )
            if not notification_service:
                return
            normalized_fog_type = (
                fog_type if fog_type in ("dense", "light") else "dense"
            )
            await notification_service.show_fog_warning(
                visibility_m=visibility_m,
                temperature_c=temp_c,
                humidity=humidity,
                hour=hour,
                fog_type=normalized_fog_type,
            )
            self._last_fog_notification_hour = notification_hour
            _LOGGER.info(
                "Fog warning notification sent for %s %02d:00 (type=%s)",
                date_str,
                hour,
                fog_type,
            )
        except Exception as error:
            _LOGGER.error("Error sending fog notification: %s", error)
        return None if True else None

    async def _check_snow_conditions(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Check for snow conditions and panel coverage. @zara V16.1

        Enhanced detection with:
        - Weather code based detection (most reliable)
        - Overnight snowfall check (22:00-06:00)
        - Temperature + precipitation heuristic (fallback)
        - Production-based melt detection (smart clearing)
        - Confidence levels for each detection method
        """
        try:
            snow_result = {
                "snow_covered_panels": False,
                "snow_event_detected": False,
                "snow_coverage_source": "none",
                "snow_confidence": 0.0,
                "snow_clearing_progress": 0.0,
            }
            temperature = weather_data.get("temperature_c")
            precipitation = weather_data.get("precipitation_mm", 0.0)
            if temperature is None:
                if True:
                    return snow_result
            snow_state = await self._load_snow_tracking()
            covered_since = snow_state.get("panels_covered_since")
            if covered_since:
                if 8 <= hour <= 16:
                    production_recovery = (
                        await self._check_production_recovery_theoretical(
                            date_str, hour
                        )
                    )
                    if production_recovery is not None:
                        if production_recovery > 0.15:
                            snow_result["snow_covered_panels"] = False
                            snow_result["snow_coverage_source"] = "production_override"
                            snow_result["snow_confidence"] = 0.0
                            await self._update_snow_tracking(
                                {
                                    "panels_covered_since": None,
                                    "estimated_depth_mm": 0.0,
                                    "melt_hours": 0,
                                    "cleared_at": dt_util.now().isoformat(),
                                }
                            )
                            if self._panel_groups:
                                for panel_group in self._panel_groups:
                                    group_name = panel_group.get(
                                        "name",
                                        f"group_{self._panel_groups.index(panel_group)}",
                                    )
                                    await self.db.save_snow_tracking_group(
                                        group_name,
                                        {
                                            "tilt_deg": panel_group.get("tilt", 30.0),
                                            "panels_covered_since": None,
                                            "estimated_depth_mm": 0.0,
                                            "melt_hours": 0,
                                            "tilt_melt_factor": self._calculate_tilt_melt_factor(
                                                panel_group.get("tilt", 30.0)
                                            ),
                                            "detection_source": "production_override",
                                            "cleared_at": dt_util.now().isoformat(),
                                        },
                                    )
                                _LOGGER.info(
                                    "Snow override: cleared %d per-group entries",
                                    len(self._panel_groups),
                                )
                            _LOGGER.info(
                                "Snow override: production %.0f%% of theoretical — panels NOT covered",
                                production_recovery * 100,
                            )
                            if True:
                                return snow_result
                if 8 <= hour <= 17:
                    warm_override, degree_hours = await self._check_sustained_warmth(
                        snow_state, date_str, hour
                    )
                    if warm_override:
                        snow_result["snow_covered_panels"] = False
                        snow_result["snow_coverage_source"] = "warm_override"
                        snow_result["snow_confidence"] = 0.0
                        await self._update_snow_tracking(
                            {
                                "panels_covered_since": None,
                                "estimated_depth_mm": 0.0,
                                "melt_hours": 0,
                                "cleared_at": dt_util.now().isoformat(),
                            }
                        )
                        if self._panel_groups:
                            for panel_group in self._panel_groups:
                                group_name = panel_group.get(
                                    "name",
                                    f"group_{self._panel_groups.index(panel_group)}",
                                )
                                await self.db.save_snow_tracking_group(
                                    group_name,
                                    {
                                        "tilt_deg": panel_group.get("tilt", 30.0),
                                        "panels_covered_since": None,
                                        "estimated_depth_mm": 0.0,
                                        "melt_hours": 0,
                                        "tilt_melt_factor": self._calculate_tilt_melt_factor(
                                            panel_group.get("tilt", 30.0)
                                        ),
                                        "detection_source": "warm_override",
                                        "cleared_at": dt_util.now().isoformat(),
                                    },
                                )
                            _LOGGER.info(
                                "Warm override: cleared %d per-group entries (%.1f degree-hours)",
                                len(self._panel_groups),
                                degree_hours,
                            )
                        _LOGGER.info(
                            "Warm override: %.1f degree-hours — panels cleared without production",
                            degree_hours,
                        )
                        if True:
                            return snow_result
                snow_result["snow_covered_panels"] = True
                snow_result["snow_coverage_source"] = "persistence"
                snow_result["snow_confidence"] = 0.9
                _LOGGER.debug(
                    "Snow persistence: panels covered since %s, marking hour %02d as covered",
                    covered_since,
                    hour,
                )
            forecast_snow = await self._check_weather_code_snow(
                date_str, hour, temperature
            )
            if forecast_snow:
                snow_result["snow_event_detected"] = True
                snow_result["snow_covered_panels"] = True
                snow_result["snow_coverage_source"] = "weather_code"
                snow_result["snow_confidence"] = 0.95
                await self._update_snow_tracking(
                    {
                        "last_snow_event": dt_util.now().isoformat(),
                        "panels_covered_since": dt_util.now().isoformat(),
                        "estimated_depth_mm": (
                            snow_state.get("estimated_depth_mm") or 0
                        )
                        + 10,
                        "melt_hours": 0,
                        "detection_source": "weather_code",
                    }
                )
                await self._send_snow_notification(
                    "snowfall", temperature, precipitation, date_str, hour
                )
                if True:
                    return snow_result
            if 4 <= hour <= 9:
                overnight_snow = await self._check_overnight_snow(date_str)
                if overnight_snow and temperature < 1.0:
                    snow_result["snow_covered_panels"] = True
                    snow_result["snow_coverage_source"] = "overnight_snowfall"
                    snow_result["snow_confidence"] = 0.85
                    if not snow_state.get("panels_covered_since"):
                        await self._update_snow_tracking(
                            {
                                "last_snow_event": dt_util.now().isoformat(),
                                "panels_covered_since": dt_util.now().isoformat(),
                                "estimated_depth_mm": overnight_snow.get(
                                    "estimated_mm", 5
                                ),
                                "melt_hours": 0,
                                "detection_source": "overnight",
                            }
                        )
                        await self._send_snow_notification(
                            "overnight_snow", temperature, 0, date_str, hour
                        )
                    if True:
                        return snow_result
            if temperature < 0.5 and precipitation > 0.5:
                snow_result["snow_event_detected"] = True
                snow_result["snow_covered_panels"] = True
                snow_result["snow_coverage_source"] = "active_snowfall_heuristic"
                snow_result["snow_confidence"] = 0.75
                await self._update_snow_tracking(
                    {
                        "last_snow_event": dt_util.now().isoformat(),
                        "panels_covered_since": dt_util.now().isoformat(),
                        "estimated_depth_mm": (
                            snow_state.get("estimated_depth_mm") or 0
                        )
                        + precipitation * 8,
                        "melt_hours": 0,
                        "detection_source": "heuristic",
                    }
                )
                await self._send_snow_notification(
                    "snowfall", temperature, precipitation, date_str, hour
                )
                if True:
                    return snow_result
            relative_humidity = weather_data.get("humidity_percent")
            wind_speed = weather_data.get("wind_speed_ms")
            if (
                temperature is not None
                and temperature <= -3
                and (relative_humidity is not None)
                and (relative_humidity > 85)
                and (wind_speed is not None)
                and (wind_speed < 3.0)
                and (not snow_result.get("snow_covered_panels"))
            ):
                snow_result["snow_covered_panels"] = True
                snow_result["snow_event_detected"] = True
                snow_result["snow_coverage_source"] = "rime_hoarfrost"
                snow_result["snow_confidence"] = 0.5
                _LOGGER.info(
                    "Rime/hoarfrost detected: temp=%.1f°C, humidity=%.0f%%, wind=%.1f m/s — marking current hour only",
                    temperature,
                    relative_humidity,
                    wind_speed or 0.0,
                )
            freezing_rain = await self._check_weather_code_freezing_rain(
                date_str, hour, temperature
            )
            if freezing_rain:
                snow_result["snow_event_detected"] = True
                snow_result["snow_covered_panels"] = True
                snow_result["snow_coverage_source"] = "freezing_rain"
                snow_result["snow_confidence"] = 0.95
                await self._update_snow_tracking(
                    {
                        "last_snow_event": dt_util.now().isoformat(),
                        "panels_covered_since": dt_util.now().isoformat(),
                        "estimated_depth_mm": (
                            snow_state.get("estimated_depth_mm") or 0
                        )
                        + 5,
                        "melt_hours": 0,
                        "detection_source": "freezing_rain",
                    }
                )
                await self._send_snow_notification(
                    "snowfall", temperature, precipitation, date_str, hour
                )
                if True:
                    return snow_result
            if covered_since:
                melt_progress = await self._calculate_melt_progress(
                    snow_state, temperature, date_str, hour
                )
                if melt_progress >= 1.0:
                    snow_result["snow_covered_panels"] = False
                    snow_result["snow_coverage_source"] = "cleared_by_melt"
                    snow_result["snow_clearing_progress"] = 1.0
                    snow_result["snow_confidence"] = 0.0
                    await self._update_snow_tracking(
                        {
                            "panels_covered_since": None,
                            "estimated_depth_mm": 0.0,
                            "melt_hours": 0,
                            "cleared_at": dt_util.now().isoformat(),
                        }
                    )
                    await self._send_snow_notification(
                        "melting", temperature, 0, date_str, hour
                    )
                    _LOGGER.info(
                        "Snow cleared: panels no longer covered (melt_progress=%.2f)",
                        melt_progress,
                    )
                else:
                    snow_result["snow_clearing_progress"] = melt_progress
                    snow_result["snow_confidence"] = max(0.5, 0.9 - melt_progress * 0.4)
                    _LOGGER.debug(
                        "Snow persistence: melt_progress=%.2f, still covered",
                        melt_progress,
                    )
            if True:
                return snow_result
        except Exception as error:
            _LOGGER.error("Error checking snow conditions: %s", error)
            if True:
                return

    async def _check_weather_code_snow(
        self, date_str: str, hour: int, temp_c: Optional[float] = None
    ) -> bool:
        """Check if weather forecast indicates snow via weather codes. @zara V16.8.3

        Now includes temperature plausibility check to prevent false positives
        when Open-Meteo sends snow codes at warm temperatures (model altitude mismatch).
        """
        try:
            row = await self.fetch_one(
                "SELECT weather_code, temperature FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if row and row[0]:
                forecast_code = int(row[0])
                if forecast_code in SNOW_WEATHER_CODES:
                    forecast_temperature = float(row[1]) if row[1] is not None else None
                    if not self._is_snow_plausible(
                        temp_c, forecast_temperature, forecast_code
                    ):
                        return False
                    _LOGGER.info(
                        "Snow detected via weather code %d (%s) for %s %02d:00",
                        forecast_code,
                        SNOW_WEATHER_CODES[forecast_code],
                        date_str,
                        hour,
                    )
                    return True
        except Exception:
            pass
        return False

    def _is_snow_plausible(
        self,
        temp_c: Optional[float],
        forecast_temp_c: Optional[float],
        weather_code: int,
    ) -> bool:
        """Check if snow/ice is physically plausible given temperature data. @zara V16.8.3

        Uses the WARMER of actual sensor temp and forecast temp for validation.
        If either source says it's warm, the snow code is implausible.
        If no temperature data is available at all, trusts the weather code.
        """
        temperatures = [
            temperature_reading
            for temperature_reading in (temp_c, forecast_temp_c)
            if temperature_reading is not None
        ]
        if not temperatures:
            return True
        warmest_temperature = max(temperatures)
        if weather_code in SNOW_WEATHER_CODES:
            if warmest_temperature > SNOW_TEMP_MAX_C:
                _LOGGER.info(
                    "Snow code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    SNOW_WEATHER_CODES.get(weather_code, "unknown"),
                    warmest_temperature,
                    SNOW_TEMP_MAX_C,
                    f"{temp_c:.1f}°C" if temp_c is not None else "n/a",
                    (
                        f"{forecast_temp_c:.1f}°C"
                        if forecast_temp_c is not None
                        else "n/a"
                    ),
                )
                return False
            return True
        if weather_code in FREEZING_RAIN_CODES:
            if warmest_temperature > FREEZING_RAIN_TEMP_MAX_C:
                _LOGGER.info(
                    "Freezing rain code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    FREEZING_RAIN_CODES.get(weather_code, "unknown"),
                    warmest_temperature,
                    FREEZING_RAIN_TEMP_MAX_C,
                    f"{temp_c:.1f}°C" if temp_c is not None else "n/a",
                    (
                        f"{forecast_temp_c:.1f}°C"
                        if forecast_temp_c is not None
                        else "n/a"
                    ),
                )
                return False
            return True
        return True

    async def _check_weather_code_freezing_rain(
        self, date_str: str, hour: int, temp_c: Optional[float] = None
    ) -> bool:
        """Check if weather forecast indicates freezing rain via WMO codes 66/67. @zara V16.8.3

        Freezing rain creates a dense ice layer on panels that is harder to
        remove than snow (ice density ~917 kg/m³ vs snow ~100-300 kg/m³).
        Now includes temperature plausibility check (freezing rain requires T < 0°C).
        """
        try:
            row = await self.fetch_one(
                "SELECT weather_code, temperature FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if row and row[0]:
                forecast_code = int(row[0])
                if forecast_code in FREEZING_RAIN_CODES:
                    forecast_temperature = float(row[1]) if row[1] is not None else None
                    if not self._is_snow_plausible(
                        temp_c, forecast_temperature, forecast_code
                    ):
                        return False
                    _LOGGER.info(
                        "Freezing rain detected via weather code %d (%s) for %s %02d:00",
                        forecast_code,
                        FREEZING_RAIN_CODES[forecast_code],
                        date_str,
                        hour,
                    )
                    return True
        except Exception:
            pass
        return False

    async def _check_overnight_snow(self, date_str: str) -> Optional[dict[str, Any]]:
        """Check if snow fell overnight (22:00 previous day to 06:00 today). @zara V16.1

        Used for morning snow detection when panels may be covered from night snowfall.
        Checks 3 sources: forecast weather_codes, actual sensor data, forecast heuristic.
        """
        try:
            local_date = datetime.strptime(date_str, "%Y-%m-%d")
            previous_date = (local_date - timedelta(days=1)).strftime("%Y-%m-%d")
            rows = await self.fetch_all(
                "SELECT hour, weather_code, temperature\n                   FROM weather_forecast\n                   WHERE (forecast_date = ? AND hour >= 22)\n                      OR (forecast_date = ? AND hour <= 6)\n                   ORDER BY forecast_date, hour",
                (previous_date, date_str),
            )
            snow_hours = []
            for row in rows:
                if row[1] and int(row[1]) in SNOW_WEATHER_CODES:
                    forecast_temperature = float(row[2]) if row[2] is not None else None
                    if (
                        forecast_temperature is not None
                        and forecast_temperature > SNOW_TEMP_MAX_C
                    ):
                        _LOGGER.debug(
                            "Overnight snow code %d skipped for hour %d: forecast temp %.1f°C > %.1f°C threshold",
                            int(row[1]),
                            row[0],
                            forecast_temperature,
                            SNOW_TEMP_MAX_C,
                        )
                        continue
                    snow_hours.append(
                        {
                            "hour": row[0],
                            "code": int(row[1]),
                            "temp": forecast_temperature,
                        }
                    )
            if snow_hours:
                _LOGGER.info(
                    "Overnight snow detected (forecast): %d hours with snow codes",
                    len(snow_hours),
                )
                return {
                    "snow_hours": len(snow_hours),
                    "estimated_mm": len(snow_hours) * 2,
                    "source": "forecast_weather_code",
                }
            rows = await self.fetch_all(
                "SELECT hour, temperature_c, precipitation_mm,\n                        snow_event_detected, snow_covered_panels\n                   FROM hourly_weather_actual\n                   WHERE (date = ? AND hour >= 22)\n                      OR (date = ? AND hour <= 6)\n                   ORDER BY date, hour",
                (previous_date, date_str),
            )
            sensor_snow_hours = 0
            cold_wet_hours = 0
            cold_precipitation = 0.0
            for row in rows:
                if row[3]:
                    sensor_snow_hours += 1
                elif row[4]:
                    sensor_snow_hours += 1
                elif row[1] is not None and row[1] < 0.5:
                    if row[2] is not None and row[2] > 0.2:
                        cold_wet_hours += 1
                        cold_precipitation += row[2] or 0
            if sensor_snow_hours > 0:
                _LOGGER.info(
                    "Overnight snow detected (sensors): %d hours with snow events",
                    sensor_snow_hours,
                )
                return {
                    "snow_hours": sensor_snow_hours,
                    "estimated_mm": sensor_snow_hours * 3,
                    "source": "actual_sensors",
                }
            if cold_wet_hours >= 2 and cold_precipitation > 0.5:
                _LOGGER.info(
                    "Overnight snow suspected (sensors): %d cold hours with %.1fmm precip",
                    cold_wet_hours,
                    cold_precipitation,
                )
                return {
                    "snow_hours": cold_wet_hours,
                    "estimated_mm": cold_precipitation * 8,
                    "source": "actual_heuristic",
                }
            rows = await self.fetch_all(
                "SELECT hour, temperature, rain\n                   FROM weather_forecast\n                   WHERE (forecast_date = ? AND hour >= 22)\n                      OR (forecast_date = ? AND hour <= 6)",
                (previous_date, date_str),
            )
            forecast_snow_hours = 0
            forecast_precipitation = 0.0
            for row in rows:
                row_temperature = row[1]
                row_precipitation = row[2] or 0
                if (
                    row_temperature is not None
                    and row_temperature < 0.5
                    and (row_precipitation > 0.2)
                ):
                    forecast_snow_hours += 1
                    forecast_precipitation += row_precipitation
            if forecast_snow_hours >= 2 and forecast_precipitation > 1.0:
                _LOGGER.info(
                    "Overnight snow suspected (forecast): %d cold hours with %.1fmm precip",
                    forecast_snow_hours,
                    forecast_precipitation,
                )
                return {
                    "snow_hours": forecast_snow_hours,
                    "estimated_mm": forecast_precipitation * 8,
                    "source": "forecast_heuristic",
                }
        except Exception as error:
            _LOGGER.debug("Error checking overnight snow: %s", error)
            return
        return None if True else None

    async def _calculate_melt_progress(
        self, snow_state: dict[str, Any], current_temp: float, date_str: str, hour: int
    ) -> float:
        "Calculate snow/ice melt progress using physics-based factors. @zara V16.2\n\nReplaces the old 48h force-clear with production-aware + temperature-aware system.\nSnow cannot melt when surface temperature stays below 0°C.\n\nFactors:\n- Factor 1: Melt hours (temp > 0°C during daylight) — max 0.35\n- Factor 2: Production recovery vs theoretical_max (astronomy) — max 0.40\n- Factor 3: Temperature intensity boost — max 0.15\n- Factor 4: Multi-hour temperature trend — max 0.10\n- VETO: Shadow detection feedback (panel_frost) — cap at 0.2\n- VETO: Freeze lock (12h all < 0°C) — cap at 0.15\n- Safety: 7-day gradual timer (replaces 48h force-clear)\n\nReturns:\n    Float 0.0-1.0 where 1.0 = panels fully cleared\n"
        try:
            progress = 0.0
            melt_hours = snow_state.get("melt_hours", 0)
            depth_mm = snow_state.get("estimated_depth_mm", 5)
            detection_source = snow_state.get("detection_source", "")
            if detection_source == "freezing_rain":
                if depth_mm <= 3:
                    required_melt_hours = 5.0
                elif depth_mm <= 8:
                    required_melt_hours = 8.0
                else:
                    required_melt_hours = 12.0
            elif detection_source == "rime":
                if depth_mm <= 2:
                    required_melt_hours = 3.0
                else:
                    required_melt_hours = 5.0
            elif depth_mm <= 5:
                required_melt_hours = 2.5
            elif depth_mm <= 10:
                required_melt_hours = 4.0
            else:
                required_melt_hours = 6.0
            if current_temp > 0:
                if 7 <= hour <= 17:
                    melt_hours += 1
                    if current_temp > 3.0:
                        melt_hours += 0.5
                    if current_temp > 5.0:
                        melt_hours += 0.5
                    await self._update_snow_tracking(
                        {
                            "melt_hours": melt_hours,
                            "last_snow_event": snow_state.get("last_snow_event"),
                            "panels_covered_since": snow_state.get(
                                "panels_covered_since"
                            ),
                            "estimated_depth_mm": max(0, depth_mm - 1),
                        }
                    )
            hours_progress = min(0.35, melt_hours / required_melt_hours * 0.35)
            if melt_hours > required_melt_hours:
                excess_melt_fraction = min(
                    1.0, (melt_hours - required_melt_hours) / required_melt_hours
                )
                hours_progress += excess_melt_fraction * 0.2
            progress += min(0.55, hours_progress)
            production_ratio = await self._check_production_recovery_theoretical(
                date_str, hour
            )
            if production_ratio is not None:
                if production_ratio > 0.5:
                    progress += 0.4
                    _LOGGER.info(
                        "Production recovery detected: %.0f%% of theoretical max",
                        production_ratio * 100,
                    )
                elif production_ratio > 0.3:
                    progress += 0.25
                elif production_ratio > 0.15:
                    progress += 0.1
            if current_temp > 5.0:
                progress += 0.15
            elif current_temp > 3.0:
                progress += 0.1
            elif current_temp > 1.0:
                progress += 0.05
            temperature_bonus = await self._check_temperature_trend(date_str, hour)
            progress += min(0.1, temperature_bonus)
            shadow_feedback = await self._check_shadow_detection_feedback(
                date_str, hour
            )
            if shadow_feedback == "reinforced":
                progress = min(progress, 0.2)
                _LOGGER.debug(
                    "Melt progress capped at 0.2 by shadow detection (panel_frost confirmed)"
                )
            freeze_locked = await self._check_freeze_lock(date_str, hour)
            if freeze_locked:
                progress = min(progress, 0.15)
                _LOGGER.debug(
                    "Melt progress capped at 0.15 by freeze lock (all temps < 0°C)"
                )
            covered_since = snow_state.get("panels_covered_since")
            if covered_since:
                try:
                    covered_at = dt_util.ensure_local(
                        datetime.fromisoformat(covered_since)
                    )
                    covered_hours = (dt_util.now() - covered_at).total_seconds() / 3600
                    if covered_hours > 48:
                        timer_excess_hours = covered_hours - 48
                        timer_bonus = min(0.7, timer_excess_hours / 24 * 0.15)
                        progress += timer_bonus
                        _LOGGER.info(
                            "Snow safety timer: %dh covered, adding %.2f progress",
                            int(covered_hours),
                            timer_bonus,
                        )
                except (ValueError, TypeError):
                    pass
            return min(1.0, progress)
        except Exception as error:
            _LOGGER.debug("Error calculating melt progress: %s", error)
            return 0.0

    async def _check_production_recovery(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Check if production is recovering (indicates panels clearing). @zara V16.1

        Compares actual production vs expected production.
        If ratio > 0.5, panels are likely partially cleared.
        If ratio > 0.7, panels are likely mostly cleared.
        """
        try:
            if hour < 8 or hour > 16:
                return None
            row = await self.fetch_one(
                "SELECT actual_kwh, prediction_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND target_hour = ?\n                   AND actual_kwh IS NOT NULL",
                (date_str, hour),
            )
            if not row or not row[0] or (not row[1]) or (row[1] < 0.1):
                return None
            actual_kwh = row[0]
            predicted_kwh = row[1]
            observed_ratio = actual_kwh / predicted_kwh
            return min(1.0, observed_ratio)
        except Exception:
            return

    async def _check_production_recovery_theoretical(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Check production recovery against THEORETICAL maximum (astronomy-based). @zara V16.2

        Uses theoretical_max_kwh from prediction_astronomy instead of prediction_kwh.
        This prevents false positives on cloudy winter days where prediction_kwh is
        already low (e.g. actual=0.01, pred=0.03 → ratio=0.33 looks like recovery).

        Checks a 3-hour window for trend stability.
        """
        try:
            if hour < 8 or hour > 16:
                return None
            rows = await self.fetch_all(
                "SELECT hp.actual_kwh, pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date = ? AND hp.target_hour BETWEEN ? AND ?\n                   AND hp.actual_kwh IS NOT NULL\n                   AND pa.theoretical_max_kwh > 0.05\n                   ORDER BY hp.target_hour",
                (date_str, max(8, hour - 2), hour),
            )
            if not rows:
                return
            production_ratios = []
            for row in rows:
                actual_kwh = row[0] or 0.0
                theoretical_kwh = row[1]
                if theoretical_kwh > 0.05:
                    production_ratios.append(actual_kwh / theoretical_kwh)
            if not production_ratios:
                return
            return min(1.0, sum(production_ratios) / len(production_ratios))
        except Exception:
            return

    async def _check_temperature_trend(self, date_str: str, hour: int) -> float:
        """Check multi-hour temperature trend for sustained above-freezing. @zara V16.2

        Returns bonus progress (0.0-0.10) if temperatures have been consistently
        above 0°C for multiple hours, indicating real melting conditions.
        """
        try:
            start_hour = max(0, hour - 6)
            rows = await self.fetch_all(
                "SELECT temperature_c FROM hourly_weather_actual\n                   WHERE date = ? AND hour BETWEEN ? AND ?\n                   AND temperature_c IS NOT NULL\n                   ORDER BY hour",
                (date_str, start_hour, hour),
            )
            if not rows or len(rows) < 3:
                return 0.0
            temperatures = [row[0] for row in rows]
            above_freezing_count = sum(
                (1 for temperature_reading in temperatures if temperature_reading > 0)
            )
            above_freezing_fraction = above_freezing_count / len(temperatures)
            if above_freezing_fraction >= 1.0 and len(temperatures) >= 4:
                return 0.1
            if above_freezing_fraction >= 0.8:
                return 0.07
            if above_freezing_fraction >= 0.5:
                return 0.03
        except Exception:
            return 0.0
        return 0.0

    async def _check_sustained_warmth(
        self, snow_state: dict[str, Any], date_str: str, hour: int
    ) -> tuple[bool, float]:
        """Check if cumulative warmth since snow makes coverage physically impossible. @zara V16.8.4

        Uses degree-hours (sum of temp for each hour where temp > 0°C) as the standard
        hydrological snow-melt metric. Solar panels at typical tilts (25-45°) shed snow
        faster than ground due to gravity and lower albedo.

        Thresholds:
        - 5mm snow on tilted panel: ~20 degree-hours
        - 10mm snow: ~40 degree-hours
        - Ice (freezing rain, denser): ~60 degree-hours for 5mm equivalent

        Requires minimum 4 consecutive hours > 2°C to prevent false clearing
        from brief warm spells.
        """
        try:
            covered_since = snow_state.get("panels_covered_since")
            if not covered_since:
                return (False, 0.0)
            covered_at = datetime.fromisoformat(covered_since)
            covered_date = covered_at.strftime("%Y-%m-%d")
            rows = await self.fetch_all(
                "SELECT date, hour, temperature_c FROM hourly_weather_actual\n                   WHERE (date > ? OR (date = ? AND hour >= ?))\n                   AND date <= ?\n                   AND temperature_c IS NOT NULL\n                   ORDER BY date, hour",
                (covered_date, covered_date, covered_at.hour, date_str),
            )
            if not rows:
                return (False, 0.0)
            degree_hours = 0.0
            warm_run = 0
            longest_warm_run = 0
            for row in rows:
                row_temperature = row[2]
                if row_temperature > 0.0:
                    degree_hours += row_temperature
                if row_temperature > 2.0:
                    warm_run += 1
                    longest_warm_run = max(longest_warm_run, warm_run)
                else:
                    warm_run = 0
            detection_source = snow_state.get("detection_source", "")
            depth_mm = snow_state.get("estimated_depth_mm", 5)
            if detection_source == "freezing_rain":
                degree_hour_threshold = (
                    40.0 if depth_mm <= 3 else 60.0 if depth_mm <= 8 else 90.0
                )
            elif detection_source in ("rime", "rime_hoarfrost"):
                degree_hour_threshold = 25.0 if depth_mm <= 2 else 40.0
            else:
                degree_hour_threshold = (
                    20.0 if depth_mm <= 5 else 40.0 if depth_mm <= 10 else 70.0
                )
            minimum_warm_run = 4
            warmth_sufficient = (
                degree_hours >= degree_hour_threshold
                and longest_warm_run >= minimum_warm_run
            )
            if warmth_sufficient:
                _LOGGER.info(
                    "Sustained warmth: %.1f degree-hours (threshold=%.1f), max consecutive warm=%d hours, detection=%s, depth=%.1fmm",
                    degree_hours,
                    degree_hour_threshold,
                    longest_warm_run,
                    detection_source,
                    depth_mm,
                )
            return (warmth_sufficient, degree_hours)
        except Exception as error:
            _LOGGER.debug("Error checking sustained warmth: %s", error)
            return (False, 0.0)

    async def _check_freeze_lock(self, date_str: str, hour: int) -> bool:
        """Check if conditions make panel clearing physically impossible. @zara V16.2

        Snow/ice cannot melt when surface temperature stays below 0°C.
        If ALL temperatures in the last 12 hours are below freezing,
        melting is physically impossible and clearing must be blocked.
        """
        try:
            local_date = datetime.strptime(date_str, "%Y-%m-%d")
            previous_date = (local_date - timedelta(days=1)).strftime("%Y-%m-%d")
            rows = await self.fetch_all(
                "SELECT temperature_c FROM hourly_weather_actual\n                   WHERE (date = ? AND hour > ?) OR (date = ? AND hour <= ?)\n                   AND temperature_c IS NOT NULL\n                   ORDER BY date, hour",
                (previous_date, hour + 12, date_str, hour),
            )
            if not rows or len(rows) < 4:
                return False
            temperatures = [row[0] for row in rows]
            maximum_temperature = max(temperatures)
            if maximum_temperature < 0.0:
                _LOGGER.debug(
                    "Freeze lock active: max temp in last 12h = %.1f°C (all below 0)",
                    maximum_temperature,
                )
                return True
        except Exception:
            return False
        return False

    async def _check_shadow_detection_feedback(self, date_str: str, hour: int) -> str:
        'Check shadow detection for panel_frost/snow reinforcement. @zara V16.2\n\nIf hourly_shadow_detection has root_cause = panel_frost with significant loss,\nthis is independent confirmation that panels are still covered.\nFeeds back into melt progress as a VETO signal.\n\nReturns:\n    "reinforced" if shadow detection confirms coverage\n    "clearing" if shadow detection suggests improvement\n    "none" if no relevant data\n'
        try:
            prediction_id = f"{date_str}_{hour:02d}"
            row = await self.fetch_one(
                "SELECT root_cause, shadow_percent, efficiency_ratio\n                   FROM hourly_shadow_detection\n                   WHERE prediction_id = ?",
                (prediction_id,),
            )
            if not row:
                return "none"
            root_cause = row[0] or ""
            shadow_percent = row[1] or 0.0
            efficiency_ratio = row[2] if row[2] is not None else 1.0
            if root_cause == "panel_frost":
                if shadow_percent > 40.0 or efficiency_ratio < 0.6:
                    _LOGGER.debug(
                        "Shadow detection reinforces coverage: root_cause=%s, shadow_pct=%.1f, efficiency=%.2f",
                        root_cause,
                        shadow_percent,
                        efficiency_ratio,
                    )
                    return "reinforced"
            if root_cause not in ("panel_frost", "night") and efficiency_ratio > 0.7:
                return "clearing"
        except Exception:
            return "none"
        return "none"

    async def _load_snow_tracking(self) -> dict[str, Any]:
        """Load snow tracking state from database. @zara V16.1"""
        try:
            row = await self.fetch_one(
                "SELECT last_snow_event, panels_covered_since, estimated_depth_mm,\n                          melt_hours, detection_source, cleared_at\n                   FROM snow_tracking WHERE id = 1"
            )
            if row:
                return {
                    "last_snow_event": row[0],
                    "panels_covered_since": row[1],
                    "estimated_depth_mm": row[2] or 0.0,
                    "melt_hours": row[3] or 0,
                    "detection_source": row[4],
                    "cleared_at": row[5],
                }
            return {
                "last_snow_event": None,
                "panels_covered_since": None,
                "estimated_depth_mm": 0.0,
                "melt_hours": 0,
                "detection_source": None,
                "cleared_at": None,
            }
        except Exception:
            return {
                "last_snow_event": None,
                "panels_covered_since": None,
                "estimated_depth_mm": 0.0,
                "melt_hours": 0,
                "detection_source": None,
                "cleared_at": None,
            }

    async def _update_snow_tracking(self, updates: dict[str, Any]) -> None:
        """Update snow tracking state in database. @zara V16.1"""
        try:
            await self.db.execute(
                "INSERT INTO snow_tracking\n                   (id, last_snow_event, panels_covered_since, estimated_depth_mm,\n                    melt_hours, detection_source, cleared_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       last_snow_event = COALESCE(excluded.last_snow_event, snow_tracking.last_snow_event),\n                       panels_covered_since = excluded.panels_covered_since,\n                       estimated_depth_mm = excluded.estimated_depth_mm,\n                       melt_hours = COALESCE(excluded.melt_hours, snow_tracking.melt_hours),\n                       detection_source = COALESCE(excluded.detection_source, snow_tracking.detection_source),\n                       cleared_at = excluded.cleared_at,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    updates.get("last_snow_event"),
                    updates.get("panels_covered_since"),
                    updates.get("estimated_depth_mm", 0.0),
                    updates.get("melt_hours", 0),
                    updates.get("detection_source"),
                    updates.get("cleared_at"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Error updating snow tracking: %s", error)
        return None if True else None

    async def _send_snow_notification(
        self, snow_type: str, temp_c: float, precip_mm: float, date_str: str, hour: int
    ) -> None:
        """Send notification for snow events. @zara V16.1"""
        try:
            notification_key = f"{date_str}_{snow_type}"
            if self._last_snow_notification_key == notification_key:
                return
            from ..const import DOMAIN as DOMAIN

            notification_service = self.hass.data.get(DOMAIN, {}).get(
                "notification_service"
            )
            if not notification_service:
                return
            if snow_type == "snowfall":
                await notification_service.show_snow_covered_warning(
                    temperature_c=temp_c, precipitation_mm=precip_mm, hour=hour
                )
            elif snow_type == "overnight_snow":
                await notification_service.show_snow_covered_warning(
                    temperature_c=temp_c,
                    precipitation_mm=0,
                    hour=hour,
                    message="Nächtlicher Schneefall erkannt - Panels vermutlich bedeckt",
                )
            elif snow_type == "melting":
                await notification_service.show_snow_melting_info(
                    temperature_c=temp_c, hour=hour
                )
            self._last_snow_notification_key = notification_key
            _LOGGER.info(
                "Snow notification sent: %s for %s %02d:00", snow_type, date_str, hour
            )
        except Exception as error:
            _LOGGER.error("Error sending snow notification: %s", error)
        return None if True else None

    @staticmethod
    def _calculate_tilt_melt_factor(tilt_deg: float) -> float:
        """Calculate melt speed factor based on panel tilt angle. @zara V17.0.0

        Steep panels clear snow faster due to gravity and solar angle.
        """
        tilt_sine = math.sin(math.radians(tilt_deg))
        if tilt_deg >= 35:
            return 1.0 + tilt_sine * 1.5
        if tilt_deg >= 25:
            return 1.0 + tilt_sine * 0.8
        return 1.0 + tilt_sine * 0.3

    async def _check_snow_conditions_per_group(
        self,
        weather_data: dict[str, Any],
        system_snow_result: dict[str, Any],
        date_str: str,
        hour: int,
    ) -> Dict[str, Dict[str, Any]]:
        """Per-group snow tracking with tilt-based melt physics. @zara V17.0.0

        Detection is system-wide (weather codes apply to entire location).
        Melt tracking is per-group (different tilt → different clearing speed).
        """
        group_results = {}
        current_temperature = weather_data.get("temperature_c", 0.0)
        new_snow_event = system_snow_result.get("snow_event_detected", False)
        try:
            group_states = await self.db.load_snow_tracking_groups()
            for panel_group in self._panel_groups:
                group_name = panel_group.get(
                    "name", f"group_{self._panel_groups.index(panel_group)}"
                )
                tilt_deg = panel_group.get("tilt", 30.0)
                tilt_factor = self._calculate_tilt_melt_factor(tilt_deg)
                group_state = group_states.get(group_name, {})
                was_covered = group_state.get("snow_covered", False)
                if new_snow_event:
                    coverage_source = system_snow_result.get(
                        "snow_coverage_source", "unknown"
                    )
                    depth_mm = system_snow_result.get(
                        "estimated_depth_mm",
                        weather_data.get("estimated_depth_mm", 5.0),
                    )
                    system_snow_state = await self._load_snow_tracking()
                    if system_snow_state:
                        depth_mm = system_snow_state.get("estimated_depth_mm", depth_mm)
                    await self.db.save_snow_tracking_group(
                        group_name,
                        {
                            "tilt_deg": tilt_deg,
                            "last_snow_event": dt_util.now().isoformat(),
                            "panels_covered_since": dt_util.now().isoformat(),
                            "estimated_depth_mm": depth_mm,
                            "melt_hours": 0,
                            "tilt_melt_factor": tilt_factor,
                            "detection_source": coverage_source,
                            "cleared_at": None,
                        },
                    )
                    group_results[group_name] = {
                        "snow_covered": True,
                        "melt_progress": 0.0,
                        "tilt_melt_factor": tilt_factor,
                        "source": coverage_source,
                    }
                    continue
                if was_covered:
                    melt_progress = await self._calculate_melt_progress_per_group(
                        group_state,
                        current_temperature,
                        date_str,
                        hour,
                        group_name,
                        tilt_factor,
                    )
                    if melt_progress >= 1.0:
                        await self.db.save_snow_tracking_group(
                            group_name,
                            {
                                "tilt_deg": tilt_deg,
                                "last_snow_event": group_state.get("last_snow_event"),
                                "panels_covered_since": None,
                                "estimated_depth_mm": 0.0,
                                "melt_hours": 0,
                                "tilt_melt_factor": tilt_factor,
                                "detection_source": group_state.get("detection_source"),
                                "cleared_at": dt_util.now().isoformat(),
                            },
                        )
                        group_results[group_name] = {
                            "snow_covered": False,
                            "melt_progress": 1.0,
                            "tilt_melt_factor": tilt_factor,
                            "source": "cleared_by_melt",
                        }
                        _LOGGER.info(
                            "Per-group snow cleared: %s (tilt=%.0f°, factor=%.2f)",
                            group_name,
                            tilt_deg,
                            tilt_factor,
                        )
                        continue
                    group_results[group_name] = {
                        "snow_covered": True,
                        "melt_progress": melt_progress,
                        "tilt_melt_factor": tilt_factor,
                        "source": "persistence",
                    }
                    continue
                if system_snow_result.get("snow_covered_panels", False):
                    group_results[group_name] = {
                        "snow_covered": False,
                        "melt_progress": 1.0,
                        "tilt_melt_factor": tilt_factor,
                        "source": "already_cleared",
                    }
                    continue
                group_results[group_name] = {
                    "snow_covered": False,
                    "melt_progress": 0.0,
                    "tilt_melt_factor": tilt_factor,
                    "source": "none",
                }
                continue
        except Exception as error:
            _LOGGER.error("Error in per-group snow tracking: %s", error)
            system_covered = system_snow_result.get("snow_covered_panels", False)
            for panel_group in self._panel_groups:
                group_results[
                    panel_group.get(
                        "name", f"group_{self._panel_groups.index(panel_group)}"
                    )
                ] = {
                    "snow_covered": system_covered,
                    "melt_progress": system_snow_result.get(
                        "snow_clearing_progress", 0.0
                    ),
                    "tilt_melt_factor": 1.0,
                    "source": "system_fallback",
                }
        return group_results if True else None

    async def _calculate_melt_progress_per_group(
        self,
        group_state: Dict[str, Any],
        current_temp: float,
        date_str: str,
        hour: int,
        group_name: str,
        tilt_melt_factor: float,
    ) -> float:
        """Calculate per-group melt progress with tilt factor. @zara V17.0.0

        Delegates to the system melt logic but applies tilt_melt_factor
        to the effective melt hours — steeper panels accumulate melt faster.
        """
        try:
            progress = 0.0
            melt_hours = group_state.get("melt_hours", 0)
            depth_mm = group_state.get("estimated_depth_mm", 5)
            detection_source = group_state.get("detection_source", "")
            if detection_source == "freezing_rain":
                required_melt_hours = (
                    5.0 if depth_mm <= 3 else 8.0 if depth_mm <= 8 else 12.0
                )
            elif detection_source == "rime":
                required_melt_hours = 3.0 if depth_mm <= 2 else 5.0
            else:
                required_melt_hours = (
                    2.5 if depth_mm <= 5 else 4.0 if depth_mm <= 10 else 6.0
                )
            if current_temp > 0:
                if 7 <= hour <= 17:
                    melt_increment = 1.0
                    if current_temp > 3.0:
                        melt_increment += 0.5
                    if current_temp > 5.0:
                        melt_increment += 0.5
                    melt_hours += melt_increment * tilt_melt_factor
                    await self.db.save_snow_tracking_group(
                        group_name,
                        {
                            "tilt_deg": group_state.get("tilt_deg", 30.0),
                            "melt_hours": melt_hours,
                            "last_snow_event": group_state.get("last_snow_event"),
                            "panels_covered_since": group_state.get(
                                "panels_covered_since"
                            ),
                            "estimated_depth_mm": max(0, depth_mm - 1),
                            "tilt_melt_factor": tilt_melt_factor,
                            "detection_source": detection_source,
                            "cleared_at": None,
                        },
                    )
            hours_progress = min(0.35, melt_hours / required_melt_hours * 0.35)
            if melt_hours > required_melt_hours:
                excess_melt_fraction = min(
                    1.0, (melt_hours - required_melt_hours) / required_melt_hours
                )
                hours_progress += excess_melt_fraction * 0.2
            progress += min(0.55, hours_progress)
            production_ratio = await self._check_production_recovery_theoretical(
                date_str, hour
            )
            if production_ratio is not None:
                if production_ratio > 0.5:
                    progress += 0.4
                elif production_ratio > 0.3:
                    progress += 0.25
                elif production_ratio > 0.15:
                    progress += 0.1
            if production_ratio is not None and production_ratio > 0.15:
                _LOGGER.info(
                    "Per-group production override %s: ratio=%.0f%% — panels clear despite freeze",
                    group_name,
                    production_ratio * 100,
                )
                return 1.0
            warm_override, degree_hours = await self._check_sustained_warmth(
                group_state, date_str, hour
            )
            if warm_override:
                _LOGGER.info(
                    "Per-group warm override %s: %.1f degree-hours — panels clear",
                    group_name,
                    degree_hours,
                )
                return 1.0
            if current_temp > 5.0:
                progress += 0.15
            elif current_temp > 3.0:
                progress += 0.1
            elif current_temp > 1.0:
                progress += 0.05
            temperature_bonus = await self._check_temperature_trend(date_str, hour)
            progress += min(0.1, temperature_bonus * min(1.3, tilt_melt_factor))
            freeze_locked = await self._check_freeze_lock(date_str, hour)
            if freeze_locked:
                progress = min(progress, 0.15)
            if (
                progress >= 0.5
                and production_ratio is not None
                and (production_ratio > 0.5)
            ):
                _LOGGER.info(
                    "Snow melt early release for %s: progress=%.2f, production=%.2f",
                    group_name,
                    progress,
                    production_ratio,
                )
                return 1.0
            covered_since = group_state.get("panels_covered_since")
            if covered_since:
                try:
                    covered_at = dt_util.ensure_local(
                        datetime.fromisoformat(covered_since)
                    )
                    covered_hours = (dt_util.now() - covered_at).total_seconds() / 3600
                    if covered_hours > 48:
                        timer_excess_hours = covered_hours - 48
                        timer_bonus = min(0.7, timer_excess_hours / 24 * 0.15)
                        progress += timer_bonus
                        _LOGGER.info(
                            "Snow safety timer %s: %dh covered, adding %.2f progress",
                            group_name,
                            int(covered_hours),
                            timer_bonus,
                        )
                except (ValueError, TypeError):
                    pass
            return min(1.0, progress)
        except Exception as error:
            _LOGGER.debug(
                "Error calculating per-group melt for %s: %s", group_name, error
            )
            return 0.0

    async def _save_hourly_data(
        self, date_str: str, hour: int, weather_data: dict[str, Any]
    ) -> None:
        """Save hourly weather data to database. @zara"""
        try:
            cutoff_date = self._retention_cutoff_date()
            cleanup_required = cutoff_date != self._last_retention_cleanup_cutoff
            async with self.db.transaction(
                deadline=monotonic() + WEATHER_DB_WRITE_TIMEOUT_SECONDS
            ):
                await self.db.execute(
                    "INSERT INTO hourly_weather_actual\n                   (date, hour, temperature_c, humidity_percent, wind_speed_ms,\n                    precipitation_mm, precipitation_semantics, precipitation_unit,\n                    precipitation_quality, pressure_hpa, solar_radiation_wm2, lux,\n                    cloud_cover_percent, cloud_cover_source, frost_detected,\n                    frost_score, frost_confidence, snow_covered_panels,\n                    snow_event_detected, snow_coverage_source, snow_confidence,\n                    snow_clearing_progress, condition, source, timestamp)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date, hour) DO UPDATE SET\n                       temperature_c = excluded.temperature_c,\n                       humidity_percent = excluded.humidity_percent,\n                       wind_speed_ms = excluded.wind_speed_ms,\n                       precipitation_mm = excluded.precipitation_mm,\n                       precipitation_semantics = excluded.precipitation_semantics,\n                       precipitation_unit = excluded.precipitation_unit,\n                       precipitation_quality = excluded.precipitation_quality,\n                       pressure_hpa = excluded.pressure_hpa,\n                       solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                       lux = excluded.lux,\n                       cloud_cover_percent = excluded.cloud_cover_percent,\n                       cloud_cover_source = excluded.cloud_cover_source,\n                       frost_detected = excluded.frost_detected,\n                       frost_score = excluded.frost_score,\n                       frost_confidence = excluded.frost_confidence,\n                       snow_covered_panels = excluded.snow_covered_panels,\n                       snow_event_detected = excluded.snow_event_detected,\n                       snow_coverage_source = excluded.snow_coverage_source,\n                       snow_confidence = excluded.snow_confidence,\n                       snow_clearing_progress = excluded.snow_clearing_progress,\n                       condition = excluded.condition,\n                       source = excluded.source,\n                       timestamp = excluded.timestamp",
                    (
                        date_str,
                        hour,
                        weather_data.get("temperature_c"),
                        weather_data.get("humidity_percent"),
                        weather_data.get("wind_speed_ms"),
                        weather_data.get("precipitation_mm"),
                        weather_data.get("precipitation_semantics"),
                        weather_data.get("precipitation_unit"),
                        weather_data.get("precipitation_quality"),
                        weather_data.get("pressure_hpa"),
                        weather_data.get("solar_radiation_wm2"),
                        weather_data.get("lux"),
                        weather_data.get("cloud_cover_percent"),
                        weather_data.get("cloud_cover_source"),
                        weather_data.get("frost_detected"),
                        weather_data.get("frost_score"),
                        weather_data.get("frost_confidence"),
                        weather_data.get("snow_covered_panels"),
                        weather_data.get("snow_event_detected"),
                        weather_data.get("snow_coverage_source"),
                        weather_data.get("snow_confidence"),
                        weather_data.get("snow_clearing_progress"),
                        weather_data.get("condition"),
                        weather_data.get("source"),
                        weather_data.get("timestamp"),
                    ),
                    auto_commit=False,
                )
                if cleanup_required:
                    await self.db.execute(
                        "DELETE FROM hourly_weather_actual WHERE date < ?",
                        (cutoff_date,),
                        auto_commit=False,
                    )
            if cleanup_required:
                self._last_retention_cleanup_cutoff = cutoff_date
        except Exception as error:
            _LOGGER.error("Error saving hourly weather data: %s", error)
        return None if True else None

    async def _cleanup_old_data(self) -> None:
        """Remove data older than retention period. @zara"""
        try:
            cutoff_date = self._retention_cutoff_date()
            if cutoff_date == self._last_retention_cleanup_cutoff:
                return
            async with self.db.transaction(
                deadline=monotonic() + WEATHER_DB_WRITE_TIMEOUT_SECONDS
            ):
                await self.db.execute(
                    "DELETE FROM hourly_weather_actual WHERE date < ?",
                    (cutoff_date,),
                    auto_commit=False,
                )
            self._last_retention_cleanup_cutoff = cutoff_date
        except Exception:
            pass
        return None if True else None

    @staticmethod
    def _retention_cutoff_date() -> str:
        """Return the current hourly-weather retention cutoff."""
        return (dt_util.now() - timedelta(days=DATA_RETENTION_DAYS)).strftime(
            "%Y-%m-%d"
        )

    async def get_actual_weather(
        self, date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Get actual weather data for specific date and hour. @zara"""
        try:
            row = await self.fetch_one(
                "SELECT temperature_c, humidity_percent, wind_speed_ms,\n                          precipitation_mm, pressure_hpa, solar_radiation_wm2,\n                          cloud_cover_percent, cloud_cover_source, frost_detected,\n                          frost_score, frost_confidence, lux, snow_covered_panels,\n                          condition\n                   FROM hourly_weather_actual\n                   WHERE date = ? AND hour = ?",
                (date_str, hour),
            )
            if not row:
                return
            return {
                "temperature_c": row[0],
                "humidity_percent": row[1],
                "wind_speed_ms": row[2],
                "precipitation_mm": row[3],
                "pressure_hpa": row[4],
                "solar_radiation_wm2": row[5],
                "cloud_cover_percent": row[6],
                "cloud_cover_source": row[7],
                "frost_detected": row[8],
                "frost_score": row[9],
                "frost_confidence": row[10],
                "lux": row[11],
                "snow_covered_panels": row[12],
                "condition": row[13],
            }
        except Exception:
            return

    async def get_daily_actual_weather(
        self, date_str: str
    ) -> dict[int, dict[str, Any]]:
        """Get all actual weather data for a specific day. @zara V16.2"""
        try:
            rows = await self.fetch_all(
                "SELECT hour, temperature_c, humidity_percent, wind_speed_ms,\n                          precipitation_mm, pressure_hpa, solar_radiation_wm2,\n                          cloud_cover_percent, cloud_cover_source,\n                          frost_detected, frost_score, frost_confidence,\n                          snow_covered_panels\n                   FROM hourly_weather_actual\n                   WHERE date = ?\n                   ORDER BY hour",
                (date_str,),
            )
            weather_by_hour = {}
            for row in rows:
                weather_by_hour[row[0]] = {
                    "temperature_c": row[1],
                    "humidity_percent": row[2],
                    "wind_speed_ms": row[3],
                    "precipitation_mm": row[4],
                    "pressure_hpa": row[5],
                    "solar_radiation_wm2": row[6],
                    "cloud_cover_percent": row[7],
                    "cloud_cover_source": row[8],
                    "frost_detected": row[9],
                    "frost_score": row[10],
                    "frost_confidence": row[11],
                    "snow_covered_panels": bool(row[12]) if row[12] else False,
                }
            return weather_by_hour
        except Exception:
            return {}

    async def get_snow_tracking_status(self) -> dict[str, Any]:
        """Get current snow tracking status with type-specific diagnostics. @zara V16.2"""
        snow_state = await self._load_snow_tracking()
        covered = snow_state.get("panels_covered_since") is not None
        melt_hours = snow_state.get("melt_hours", 0)
        depth_mm = snow_state.get("estimated_depth_mm", 0.0)
        detection_source = snow_state.get("detection_source", "")
        clearing_progress = 0.0
        if covered and depth_mm > 0:
            if detection_source == "freezing_rain":
                required_melt_hours = 8.0
            elif detection_source == "rime":
                required_melt_hours = 3.0
            else:
                required_melt_hours = 3.0 if depth_mm <= 5 else 5.0
            clearing_progress = min(1.0, melt_hours / required_melt_hours)
        return {
            "panels_covered": covered,
            "snow_covered_panels": covered,
            "last_snow_event": snow_state.get("last_snow_event"),
            "estimated_depth_mm": depth_mm,
            "panels_covered_since": snow_state.get("panels_covered_since"),
            "melt_hours": melt_hours,
            "detection_source": detection_source,
            "clearing_progress": round(clearing_progress, 2),
            "cleared_at": snow_state.get("cleared_at"),
        }

    async def get_snow_tracking_per_group(self) -> Dict[str, bool]:
        "Get per-group snow coverage status. @zara V17.0.0\n\nReturns:\n    Dict mapping group_name → snow_covered (bool).\n    Empty dict if no panel groups configured.\n"
        try:
            group_states = await self.db.load_snow_tracking_groups()
            return {
                group_name: group_state.get("snow_covered", False)
                for group_name, group_state in group_states.items()
            }
            pass
        except Exception as error:
            _LOGGER.debug("Could not load per-group snow status: %s", error)
            return {}

    async def _detect_sensor_freeze(
        self, date_str: str, hour: int, current_val: float, field: str
    ) -> bool:
        """Detect if sensor values are frozen (identical for 3+ consecutive hours). @zara V16.2"""
        if current_val <= 0.0 and field in ("solar_radiation_wm2", "lux"):
            return False
        try:
            rows = await self.fetch_all(
                "SELECT {field} FROM hourly_weather_actual\n                   WHERE (date = ? AND hour < ?) OR (date < ?)\n                   ORDER BY date DESC, hour DESC\n                   LIMIT 2".format(
                    field=field
                ),
                (date_str, hour, date_str),
            )
            if not rows or len(rows) < 2:
                return False
            recent_values = [current_val]
            for history_row in rows:
                if history_row[0] is not None:
                    recent_values.append(float(history_row[0]))
            if len(recent_values) < 3:
                return False
            if all((reading == recent_values[0] for reading in recent_values)):
                _LOGGER.warning(
                    "Sensor freeze detected for field '%s': 3 identical values (%s)",
                    field,
                    recent_values,
                )
                return True
            return False
        except Exception as error:
            _LOGGER.debug("Error in freeze detection: %s", error)
            return False
