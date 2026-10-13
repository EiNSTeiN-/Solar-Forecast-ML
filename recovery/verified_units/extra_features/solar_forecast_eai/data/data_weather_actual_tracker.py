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
DATA_RETENTION_DAYS = 7


class WeatherActualTracker(DataManagerIO):
    """Tracks actual weather conditions from local sensors. @zara

    All data stored in hourly_weather_actual table.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        config_entry=None,
        astronomy_provider=None,
    ):
        "Initialize weather actual tracker. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for data persistence\n    config_entry: Config entry with sensor mappings\n"
        super().__init__(hass, db_manager)
        self._astronomy_provider = astronomy_provider
        self._last_frost_notification_hour = None
        self._last_snow_notification_key = None
        self._last_fog_notification_hour = None
        self._panel_groups = []
        self.has_solar_radiation_sensor = False
        if config_entry:
            from ..const import (
                CONF_TEMP_SENSOR as temperature_sensor_key,
                CONF_HUMIDITY_SENSOR as humidity_sensor_key,
                CONF_WIND_SENSOR as wind_sensor_key,
                CONF_RAIN_SENSOR as precipitation_sensor_key,
                CONF_PRESSURE_SENSOR as pressure_sensor_key,
                CONF_SOLAR_RADIATION_SENSOR as solar_radiation_sensor_key,
                CONF_WEATHER_ENTITY as weather_entity_key,
            )

            merged_config = {**config_entry.data, **config_entry.options}
            solar_radiation_entity_id = merged_config.get(solar_radiation_sensor_key)
            self.sensor_mapping = {
                "temperature_c": merged_config.get(temperature_sensor_key),
                "humidity_percent": merged_config.get(humidity_sensor_key),
                "wind_speed_ms": merged_config.get(wind_sensor_key),
                "precipitation_mm": merged_config.get(precipitation_sensor_key),
                "pressure_hpa": merged_config.get(pressure_sensor_key),
                "solar_radiation_wm2": solar_radiation_entity_id,
                "condition": merged_config.get(weather_entity_key),
            }
            self.has_solar_radiation_sensor = bool(solar_radiation_entity_id)
            self.weather_entity_id = merged_config.get(weather_entity_key)
        else:
            self.sensor_mapping = {}
            self.weather_entity_id = None
        _LOGGER.info("WeatherActualTracker initialized")

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
        now = dt_util.now()
        date_str = now.strftime("%Y-%m-%d")
        hour = now.hour
        weather_data = await self._read_sensors()
        if not weather_data:
            return {}
        weather_data["timestamp"] = now.isoformat()
        weather_data["source"] = "homeassistant_sensors"
        cloud_cover_percent = await self._estimate_cloud_cover(
            weather_data, date_str, hour
        )
        if cloud_cover_percent is not None:
            weather_data["cloud_cover_percent"] = cloud_cover_percent
        frost_result = await self._detect_frost(weather_data, date_str, hour)
        if frost_result:
            weather_data.update(frost_result)
        snow_result = await self._check_snow_conditions(weather_data, date_str, hour)
        if snow_result:
            weather_data.update(snow_result)
        if self._panel_groups and snow_result:
            group_snow_results = await self._check_snow_conditions_per_group(
                weather_data, snow_result, date_str, hour
            )
            weather_data["snow_per_group"] = group_snow_results
        fog_result = await self._detect_fog(weather_data, date_str, hour)
        if fog_result:
            weather_data.update(fog_result)
        await self._save_hourly_data(date_str, hour, weather_data)
        return weather_data

    async def _read_sensors(self) -> dict[str, Any]:
        """Read current values from all configured sensors. @zara"""
        weather_readings = {}
        available_sensor_count = 0
        for weather_field, entity_id in self.sensor_mapping.items():
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
                state_value = sensor_state.state
                if weather_field == "condition":
                    weather_readings[weather_field] = state_value
                    available_sensor_count += 1
                else:
                    try:
                        numeric_value = float(state_value)
                        if weather_field == "precipitation_mm":
                            precipitation_snapshot = self._precipitation_snapshot(
                                numeric_value,
                                getattr(sensor_state, "attributes", {}) or {},
                            )
                            if precipitation_snapshot is None:
                                _LOGGER.debug(
                                    "Ignoring ambiguous precipitation sensor %s",
                                    entity_id,
                                )
                                continue
                            weather_readings.update(precipitation_snapshot)
                        else:
                            weather_readings[weather_field] = round(numeric_value, 2)
                        available_sensor_count += 1
                    except (ValueError, TypeError):
                        continue
            except Exception:
                continue
        return weather_readings if available_sensor_count > 0 else {}

    @staticmethod
    def _precipitation_snapshot(
        value: float, attributes: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Classify a rain snapshot without inferring an amount from a rate.

        Home Assistant Recorder uses ``total``/``total_increasing`` for counters
        and ``measurement`` for instantaneous measurements. A plain ``mm``
        measurement is deliberately ambiguous, so it must not become an hourly
        amount.
        """
        if not math.isfinite(value) or value < 0:
            return None
        unit = (
            str(
                attributes.get("unit_of_measurement")
                or attributes.get("native_unit_of_measurement")
                or ""
            )
            .lower()
            .replace(" ", "")
        )
        state_class = str(attributes.get("state_class") or "").lower()
        millimeter_factors = {
            "mm": 1.0,
            "cm": 10.0,
            "m": 1000.0,
            "in": 25.4,
            "inch": 25.4,
            "inches": 25.4,
        }
        if state_class in {"total", "total_increasing"} and unit in millimeter_factors:
            return {
                "precipitation_semantics": (
                    "daily_total" if state_class == "total" else "counter"
                ),
                "precipitation_counter_mm": round(value * millimeter_factors[unit], 4),
            }
        for rate_suffix in ("/h", "/hr", "h-1", "hr-1"):
            if not unit.endswith(rate_suffix):
                continue
            amount_unit = unit[: -len(rate_suffix)]
            if not amount_unit in millimeter_factors:
                continue
            return {
                "precipitation_semantics": "rate",
                "precipitation_rate_max_mm_h": round(
                    value * millimeter_factors[amount_unit], 4
                ),
            }

    async def _estimate_cloud_cover(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[float]:
        """Estimate cloud cover from various sources. @zara

        Priority:
        1. Sensor-based calculation (when solar radiation sensor configured)
        2. Production-based estimation (when no sensor configured)
        3. Forecast fallback
        """
        cloud_cover_percent = None
        if self.has_solar_radiation_sensor:
            solar_radiation_wm2 = weather_data.get("solar_radiation_wm2")
            if solar_radiation_wm2 is not None:
                cloud_cover_percent = await self._calculate_cloud_from_radiation(
                    solar_radiation_wm2, date_str, hour
                )
                if cloud_cover_percent is not None:
                    weather_data["cloud_cover_source"] = "sensor_calculated"
                    return cloud_cover_percent
        if not self.has_solar_radiation_sensor:
            production_hour = hour - 1 if hour > 0 else hour
            cloud_cover_percent = await self._estimate_cloud_from_production(
                date_str, production_hour
            )
            if cloud_cover_percent is not None:
                weather_data["cloud_cover_source"] = "production_estimated"
                return cloud_cover_percent
        cloud_cover_percent = await self._get_forecast_cloud_cover(date_str, hour)
        if cloud_cover_percent is not None:
            weather_data["cloud_cover_source"] = "forecast_fallback"
            return cloud_cover_percent

    async def _calculate_cloud_from_radiation(
        self, measured_wm2: float, date_str: str, hour: int
    ) -> Optional[float]:
        """Calculate cloud cover from measured radiation vs theoretical max. @zara"""
        try:
            astronomy_days = (
                await self._astronomy_provider.async_get_legacy_days(
                    datetime.fromisoformat(date_str).date(), days=1
                )
                if self._astronomy_provider
                else {}
            )
            clear_sky_radiation_wm2 = astronomy_days.get(
                f"{date_str}_{hour:02d}", {}
            ).get("clear_sky_radiation_wm2")
            if not clear_sky_radiation_wm2 or clear_sky_radiation_wm2 <= 0:
                return None
            theoretical_max_wm2 = clear_sky_radiation_wm2
            radiation_ratio = measured_wm2 / theoretical_max_wm2
            radiation_ratio = max(0.0, min(1.0, radiation_ratio))
            cloud_cover_percent = (1.0 - radiation_ratio) * 100.0
            return round(cloud_cover_percent, 1)
        except Exception:
            return

    async def _estimate_cloud_from_production(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Estimate cloud cover from actual production vs theoretical. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT actual_kwh, pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date = ? AND hp.target_hour = ?",
                (date_str, hour),
            )
            if (
                not database_row
                or not database_row[0]
                or (not database_row[1])
                or (database_row[1] < 0.01)
            ):
                return None
            actual_kwh = database_row[0]
            theoretical_max_kwh = database_row[1]
            production_ratio = actual_kwh / theoretical_max_kwh
            production_ratio = max(0.0, min(1.0, production_ratio))
            cloud_cover_percent = (1.0 - production_ratio) * 100.0
            return round(cloud_cover_percent, 1)
        except Exception:
            return

    async def _get_forecast_cloud_cover(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Get cloud cover from forecast as fallback. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if database_row and database_row[0] is not None:
                return float(database_row[0])
        except Exception:
            return
        return None if True else None

    async def _detect_frost(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Detect frost conditions on solar panels. @zara"""
        try:
            temperature_c = weather_data.get("temperature_c")
            humidity_percent = weather_data.get("humidity_percent")
            if temperature_c is None:
                return
            freezing_detected = False
            frost_score = 0.0
            frost_confidence = 0.0
            if temperature_c <= 0:
                freezing_detected = True
                frost_score = min(1.0, abs(temperature_c) / 5.0)
                frost_confidence = 0.9
                if humidity_percent is not None and humidity_percent > 80:
                    frost_score = min(1.0, frost_score + 0.2)
            frost_type = "none"
            if frost_score > 0.7:
                frost_type = "heavy_frost"
            elif frost_score > 0.3:
                frost_type = "light_frost"
            elif freezing_detected:
                frost_type = "possible_frost"
            if frost_type == "heavy_frost":
                await self._send_frost_notification(
                    frost_score, temperature_c, humidity_percent, date_str, hour
                )
            return {
                "frost_detected": frost_type,
                "frost_score": round(frost_score, 2),
                "frost_confidence": round(frost_confidence, 2),
            }
        except Exception as tracker_error:
            _LOGGER.error("Error during frost detection: %s", tracker_error)
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
            hour_key = f"{date_str}_{hour}"
            if self._last_frost_notification_hour == hour_key:
                return
            from ..const import DOMAIN as integration_domain

            notification_service = self.hass.data.get(integration_domain, {}).get(
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
            self._last_frost_notification_hour = hour_key
            _LOGGER.info(
                "Frost warning notification sent for %s %02d:00", date_str, hour
            )
        except Exception as tracker_error:
            _LOGGER.error("Error sending frost notification: %s", tracker_error)
        return None if True else None

    async def _detect_fog(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Detect fog conditions from forecast data and meteorological indicators. @zara

        Two-source detection:
        1. Primary: Read fog_detected/fog_type from weather_forecast table
           (set by WeatherForecastCorrector from Open-Meteo visibility data)
        2. Fallback: Meteorological detection from local sensor data
           (temperature, humidity, dewpoint, cloud cover)

        Args:
            weather_data: Current sensor readings
            date_str: Current date (YYYY-MM-DD)
            hour: Current hour (0-23)

        Returns:
            Dict with fog detection results or None on error
        """
        try:
            fog_detected = False
            fog_type = "none"
            fog_confidence = 0.0
            visibility_m = None
            try:
                database_row = await self.fetch_one(
                    "SELECT visibility_m, fog_detected, fog_type\n                       FROM weather_forecast\n                       WHERE forecast_date = ? AND hour = ?",
                    (date_str, hour),
                )
                if database_row:
                    visibility_m = database_row[0]
                    if database_row[1]:
                        fog_detected = True
                        fog_type = database_row[2] or "dense"
                        fog_confidence = 0.85
            except Exception:
                pass
            if not fog_detected:
                temperature_c = weather_data.get("temperature_c")
                humidity_percent = weather_data.get("humidity_percent")
                if temperature_c is not None and humidity_percent is not None:
                    dewpoint_c = None
                    if humidity_percent > 50:
                        dewpoint_c = temperature_c - (100 - humidity_percent) / 5.0
                    cloud_cover_percent = weather_data.get("cloud_cover_percent", 50)
                    from .data_weather_corrector import (
                        detect_fog_from_meteorology as detect_fog_from_meteorology,
                    )

                    (
                        meteorological_fog_detected,
                        meteorological_fog_confidence,
                        meteorological_fog_type,
                    ) = detect_fog_from_meteorology(
                        temperature_c,
                        dewpoint_c,
                        humidity_percent,
                        cloud_cover_percent,
                        hour,
                    )
                    if meteorological_fog_detected:
                        fog_detected = True
                        fog_type = meteorological_fog_type
                        fog_confidence = meteorological_fog_confidence
            if not fog_detected:
                return
            dense_fog = False
            if visibility_m is not None:
                dense_fog = visibility_m < 1000
            if dense_fog:
                await self._send_fog_notification(
                    visibility_m=visibility_m,
                    temp_c=weather_data.get("temperature_c", 0.0),
                    humidity=weather_data.get("humidity_percent", 0.0),
                    fog_type=fog_type,
                    date_str=date_str,
                    hour=hour,
                )
            return {
                "fog_detected": True,
                "fog_type": fog_type,
                "fog_confidence": round(fog_confidence, 2),
            }
        except Exception as tracker_error:
            _LOGGER.error("Error during fog detection: %s", tracker_error)
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
            hour_key = f"{date_str}_{hour}"
            if self._last_fog_notification_hour == hour_key:
                return
            from ..const import DOMAIN as integration_domain

            notification_service = self.hass.data.get(integration_domain, {}).get(
                "notification_service"
            )
            if not notification_service:
                return
            notification_visibility_m = (
                visibility_m
                if visibility_m is not None
                else 800.0 if fog_type == "dense" else 3000.0
            )
            notification_fog_type = (
                fog_type if fog_type in ("dense", "light") else "dense"
            )
            await notification_service.show_fog_warning(
                visibility_m=notification_visibility_m,
                temperature_c=temp_c,
                humidity=humidity,
                hour=hour,
                fog_type=notification_fog_type,
            )
            self._last_fog_notification_hour = hour_key
            _LOGGER.info(
                "Fog warning notification sent for %s %02d:00 (type=%s)",
                date_str,
                hour,
                fog_type,
            )
        except Exception as tracker_error:
            _LOGGER.error("Error sending fog notification: %s", tracker_error)
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
            temperature_c = weather_data.get("temperature_c")
            precipitation_mm = weather_data.get("precipitation_mm", 0.0)
            if temperature_c is None:
                if True:
                    return snow_result
            snow_state = await self._load_snow_tracking()
            panels_covered_since = snow_state.get("panels_covered_since")
            if panels_covered_since:
                if 8 <= hour <= 16:
                    production_recovery_ratio = (
                        await self._check_production_recovery_theoretical(
                            date_str, hour
                        )
                    )
                    if production_recovery_ratio is not None:
                        if production_recovery_ratio > 0.15:
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
                                production_recovery_ratio * 100,
                            )
                            if True:
                                return snow_result
                if 8 <= hour <= 17:
                    warmth_cleared, degree_hours = await self._check_sustained_warmth(
                        snow_state, date_str, hour
                    )
                    if warmth_cleared:
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
                    panels_covered_since,
                    hour,
                )
            weather_code_snow = await self._check_weather_code_snow(
                date_str, hour, temperature_c
            )
            if weather_code_snow:
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
                    "snowfall", temperature_c, precipitation_mm, date_str, hour
                )
                if True:
                    return snow_result
            if 4 <= hour <= 9:
                overnight_snow = await self._check_overnight_snow(date_str)
                if overnight_snow and temperature_c < 1.0:
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
                            "overnight_snow", temperature_c, 0, date_str, hour
                        )
                    if True:
                        return snow_result
            if temperature_c < 0.5 and precipitation_mm > 0.5:
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
                        + precipitation_mm * 8,
                        "melt_hours": 0,
                        "detection_source": "heuristic",
                    }
                )
                await self._send_snow_notification(
                    "snowfall", temperature_c, precipitation_mm, date_str, hour
                )
                if True:
                    return snow_result
            humidity_percent = weather_data.get("humidity_percent")
            wind_speed_ms = weather_data.get("wind_speed_ms")
            if (
                temperature_c is not None
                and temperature_c <= -3
                and (humidity_percent is not None)
                and (humidity_percent > 85)
                and (wind_speed_ms is not None)
                and (wind_speed_ms < 3.0)
                and (not snow_result.get("snow_covered_panels"))
            ):
                snow_result["snow_covered_panels"] = True
                snow_result["snow_event_detected"] = True
                snow_result["snow_coverage_source"] = "rime_hoarfrost"
                snow_result["snow_confidence"] = 0.5
                _LOGGER.info(
                    "Rime/hoarfrost detected: temp=%.1f°C, humidity=%.0f%%, wind=%.1f m/s — marking current hour only",
                    temperature_c,
                    humidity_percent,
                    wind_speed_ms or 0.0,
                )
            freezing_rain = await self._check_weather_code_freezing_rain(
                date_str, hour, temperature_c
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
                    "snowfall", temperature_c, precipitation_mm, date_str, hour
                )
                if True:
                    return snow_result
            if panels_covered_since:
                melt_progress = await self._calculate_melt_progress(
                    snow_state, temperature_c, date_str, hour
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
                        "melting", temperature_c, 0, date_str, hour
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
        except Exception as tracker_error:
            _LOGGER.error("Error checking snow conditions: %s", tracker_error)
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
            database_row = await self.fetch_one(
                "SELECT weather_code, temperature FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if database_row and database_row[0]:
                weather_code = int(database_row[0])
                if weather_code in SNOW_WEATHER_CODES:
                    forecast_temperature_c = (
                        float(database_row[1]) if database_row[1] is not None else None
                    )
                    if not self._is_snow_plausible(
                        temp_c, forecast_temperature_c, weather_code
                    ):
                        return False
                    _LOGGER.info(
                        "Snow detected via weather code %d (%s) for %s %02d:00",
                        weather_code,
                        SNOW_WEATHER_CODES[weather_code],
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
            temperature
            for temperature in (temp_c, forecast_temp_c)
            if temperature is not None
        ]
        if not temperatures:
            return True
        warmest_temperature_c = max(temperatures)
        if weather_code in SNOW_WEATHER_CODES:
            if warmest_temperature_c > SNOW_TEMP_MAX_C:
                _LOGGER.info(
                    "Snow code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    SNOW_WEATHER_CODES.get(weather_code, "unknown"),
                    warmest_temperature_c,
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
            if warmest_temperature_c > FREEZING_RAIN_TEMP_MAX_C:
                _LOGGER.info(
                    "Freezing rain code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    FREEZING_RAIN_CODES.get(weather_code, "unknown"),
                    warmest_temperature_c,
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
            database_row = await self.fetch_one(
                "SELECT weather_code, temperature FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            if database_row and database_row[0]:
                weather_code = int(database_row[0])
                if weather_code in FREEZING_RAIN_CODES:
                    forecast_temperature_c = (
                        float(database_row[1]) if database_row[1] is not None else None
                    )
                    if not self._is_snow_plausible(
                        temp_c, forecast_temperature_c, weather_code
                    ):
                        return False
                    _LOGGER.info(
                        "Freezing rain detected via weather code %d (%s) for %s %02d:00",
                        weather_code,
                        FREEZING_RAIN_CODES[weather_code],
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
            current_date = datetime.strptime(date_str, "%Y-%m-%d")
            previous_date_str = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
            database_rows = await self.fetch_all(
                "SELECT hour, weather_code, temperature\n                   FROM weather_forecast\n                   WHERE (forecast_date = ? AND hour >= 22)\n                      OR (forecast_date = ? AND hour <= 6)\n                   ORDER BY forecast_date, hour",
                (previous_date_str, date_str),
            )
            snow_hours = []
            for database_row in database_rows:
                if database_row[1] and int(database_row[1]) in SNOW_WEATHER_CODES:
                    forecast_temperature_c = (
                        float(database_row[2]) if database_row[2] is not None else None
                    )
                    if (
                        forecast_temperature_c is not None
                        and forecast_temperature_c > SNOW_TEMP_MAX_C
                    ):
                        _LOGGER.debug(
                            "Overnight snow code %d skipped for hour %d: forecast temp %.1f°C > %.1f°C threshold",
                            int(database_row[1]),
                            database_row[0],
                            forecast_temperature_c,
                            SNOW_TEMP_MAX_C,
                        )
                        continue
                    snow_hours.append(
                        {
                            "hour": database_row[0],
                            "code": int(database_row[1]),
                            "temp": forecast_temperature_c,
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
            database_rows = await self.fetch_all(
                "SELECT hour, temperature_c, precipitation_mm,\n                        snow_event_detected, snow_covered_panels\n                   FROM hourly_weather_actual\n                   WHERE (date = ? AND hour >= 22)\n                      OR (date = ? AND hour <= 6)\n                   ORDER BY date, hour",
                (previous_date_str, date_str),
            )
            detected_snow_hours = 0
            cold_wet_hours = 0
            cold_precipitation_mm = 0.0
            for database_row in database_rows:
                if database_row[3]:
                    detected_snow_hours += 1
                elif database_row[4]:
                    detected_snow_hours += 1
                elif database_row[1] is not None and database_row[1] < 0.5:
                    if database_row[2] is not None and database_row[2] > 0.2:
                        cold_wet_hours += 1
                        cold_precipitation_mm += database_row[2] or 0
            if detected_snow_hours > 0:
                _LOGGER.info(
                    "Overnight snow detected (sensors): %d hours with snow events",
                    detected_snow_hours,
                )
                return {
                    "snow_hours": detected_snow_hours,
                    "estimated_mm": detected_snow_hours * 3,
                    "source": "actual_sensors",
                }
            if cold_wet_hours >= 2 and cold_precipitation_mm > 0.5:
                _LOGGER.info(
                    "Overnight snow suspected (sensors): %d cold hours with %.1fmm precip",
                    cold_wet_hours,
                    cold_precipitation_mm,
                )
                return {
                    "snow_hours": cold_wet_hours,
                    "estimated_mm": cold_precipitation_mm * 8,
                    "source": "actual_heuristic",
                }
            database_rows = await self.fetch_all(
                "SELECT hour, temperature, rain\n                   FROM weather_forecast\n                   WHERE (forecast_date = ? AND hour >= 22)\n                      OR (forecast_date = ? AND hour <= 6)",
                (previous_date_str, date_str),
            )
            forecast_cold_wet_hours = 0
            forecast_precipitation_mm = 0.0
            for database_row in database_rows:
                temperature_c = database_row[1]
                precipitation_mm = database_row[2] or 0
                if (
                    temperature_c is not None
                    and temperature_c < 0.5
                    and (precipitation_mm > 0.2)
                ):
                    forecast_cold_wet_hours += 1
                    forecast_precipitation_mm += precipitation_mm
            if forecast_cold_wet_hours >= 2 and forecast_precipitation_mm > 1.0:
                _LOGGER.info(
                    "Overnight snow suspected (forecast): %d cold hours with %.1fmm precip",
                    forecast_cold_wet_hours,
                    forecast_precipitation_mm,
                )
                return {
                    "snow_hours": forecast_cold_wet_hours,
                    "estimated_mm": forecast_precipitation_mm * 8,
                    "source": "forecast_heuristic",
                }
        except Exception as tracker_error:
            _LOGGER.debug("Error checking overnight snow: %s", tracker_error)
            return
        return None if True else None

    async def _calculate_melt_progress(
        self, snow_state: dict[str, Any], current_temp: float, date_str: str, hour: int
    ) -> float:
        "Calculate snow/ice melt progress using physics-based factors. @zara V16.2\n\nReplaces the old 48h force-clear with production-aware + temperature-aware system.\nSnow cannot melt when surface temperature stays below 0°C.\n\nFactors:\n- Factor 1: Melt hours (temp > 0°C during daylight) — max 0.35\n- Factor 2: Production recovery vs theoretical_max (astronomy) — max 0.40\n- Factor 3: Temperature intensity boost — max 0.15\n- Factor 4: Multi-hour temperature trend — max 0.10\n- VETO: Shadow detection feedback (panel_frost) — cap at 0.2\n- VETO: Freeze lock (12h all < 0°C) — cap at 0.15\n- Safety: 7-day gradual timer (replaces 48h force-clear)\n\nReturns:\n    Float 0.0-1.0 where 1.0 = panels fully cleared\n"
        try:
            melt_progress = 0.0
            melt_hours = snow_state.get("melt_hours", 0)
            estimated_depth_mm = snow_state.get("estimated_depth_mm", 5)
            detection_source = snow_state.get("detection_source", "")
            if detection_source == "freezing_rain":
                if estimated_depth_mm <= 3:
                    required_melt_hours = 5.0
                elif estimated_depth_mm <= 8:
                    required_melt_hours = 8.0
                else:
                    required_melt_hours = 12.0
            elif detection_source == "rime":
                if estimated_depth_mm <= 2:
                    required_melt_hours = 3.0
                else:
                    required_melt_hours = 5.0
            elif estimated_depth_mm <= 5:
                required_melt_hours = 2.5
            elif estimated_depth_mm <= 10:
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
                            "estimated_depth_mm": max(0, estimated_depth_mm - 1),
                        }
                    )
            melt_hours_progress = min(0.35, melt_hours / required_melt_hours * 0.35)
            if melt_hours > required_melt_hours:
                excess_melt_ratio = min(
                    1.0, (melt_hours - required_melt_hours) / required_melt_hours
                )
                melt_hours_progress += excess_melt_ratio * 0.2
            melt_progress += min(0.55, melt_hours_progress)
            production_recovery_ratio = (
                await self._check_production_recovery_theoretical(date_str, hour)
            )
            if production_recovery_ratio is not None:
                if production_recovery_ratio > 0.5:
                    melt_progress += 0.4
                    _LOGGER.info(
                        "Production recovery detected: %.0f%% of theoretical max",
                        production_recovery_ratio * 100,
                    )
                elif production_recovery_ratio > 0.3:
                    melt_progress += 0.25
                elif production_recovery_ratio > 0.15:
                    melt_progress += 0.1
            if current_temp > 5.0:
                melt_progress += 0.15
            elif current_temp > 3.0:
                melt_progress += 0.1
            elif current_temp > 1.0:
                melt_progress += 0.05
            temperature_trend_bonus = await self._check_temperature_trend(
                date_str, hour
            )
            melt_progress += min(0.1, temperature_trend_bonus)
            shadow_feedback = await self._check_shadow_detection_feedback(
                date_str, hour
            )
            if shadow_feedback == "reinforced":
                melt_progress = min(melt_progress, 0.2)
                _LOGGER.debug(
                    "Melt progress capped at 0.2 by shadow detection (panel_frost confirmed)"
                )
            freeze_lock = await self._check_freeze_lock(date_str, hour)
            if freeze_lock:
                melt_progress = min(melt_progress, 0.15)
                _LOGGER.debug(
                    "Melt progress capped at 0.15 by freeze lock (all temps < 0°C)"
                )
            panels_covered_since = snow_state.get("panels_covered_since")
            if panels_covered_since:
                try:
                    covered_since_datetime = datetime.fromisoformat(
                        panels_covered_since
                    )
                    hours_covered = (
                        dt_util.now() - covered_since_datetime
                    ).total_seconds() / 3600
                    if hours_covered > 48:
                        hours_after_grace = hours_covered - 48
                        safety_timer_bonus = min(0.7, hours_after_grace / 24 * 0.15)
                        melt_progress += safety_timer_bonus
                        _LOGGER.info(
                            "Snow safety timer: %dh covered, adding %.2f progress",
                            int(hours_covered),
                            safety_timer_bonus,
                        )
                except (ValueError, TypeError):
                    pass
            return min(1.0, melt_progress)
        except Exception as tracker_error:
            _LOGGER.debug("Error calculating melt progress: %s", tracker_error)
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
            database_row = await self.fetch_one(
                "SELECT actual_kwh, prediction_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND target_hour = ?\n                   AND actual_kwh IS NOT NULL",
                (date_str, hour),
            )
            if (
                not database_row
                or not database_row[0]
                or (not database_row[1])
                or (database_row[1] < 0.1)
            ):
                return None
            actual_kwh = database_row[0]
            predicted_kwh = database_row[1]
            production_ratio = actual_kwh / predicted_kwh
            return min(1.0, production_ratio)
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
            database_rows = await self.fetch_all(
                "SELECT hp.actual_kwh, pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date = ? AND hp.target_hour BETWEEN ? AND ?\n                   AND hp.actual_kwh IS NOT NULL\n                   AND pa.theoretical_max_kwh > 0.05\n                   ORDER BY hp.target_hour",
                (date_str, max(8, hour - 2), hour),
            )
            if not database_rows:
                return
            production_ratios = []
            for database_row in database_rows:
                actual_kwh = database_row[0] or 0.0
                theoretical_max_kwh = database_row[1]
                if theoretical_max_kwh > 0.05:
                    production_ratios.append(actual_kwh / theoretical_max_kwh)
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
            database_rows = await self.fetch_all(
                "SELECT temperature_c FROM hourly_weather_actual\n                   WHERE date = ? AND hour BETWEEN ? AND ?\n                   AND temperature_c IS NOT NULL\n                   ORDER BY hour",
                (date_str, start_hour, hour),
            )
            if not database_rows or len(database_rows) < 3:
                return 0.0
            temperatures = [database_row[0] for database_row in database_rows]
            above_freezing_hours = sum(
                (1 for temperature in temperatures if temperature > 0)
            )
            above_freezing_ratio = above_freezing_hours / len(temperatures)
            if above_freezing_ratio >= 1.0 and len(temperatures) >= 4:
                return 0.1
            if above_freezing_ratio >= 0.8:
                return 0.07
            if above_freezing_ratio >= 0.5:
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
            panels_covered_since = snow_state.get("panels_covered_since")
            if not panels_covered_since:
                return (False, 0.0)
            covered_since_datetime = datetime.fromisoformat(panels_covered_since)
            covered_since_date_str = covered_since_datetime.strftime("%Y-%m-%d")
            database_rows = await self.fetch_all(
                "SELECT date, hour, temperature_c FROM hourly_weather_actual\n                   WHERE (date > ? OR (date = ? AND hour >= ?))\n                   AND date <= ?\n                   AND temperature_c IS NOT NULL\n                   ORDER BY date, hour",
                (
                    covered_since_date_str,
                    covered_since_date_str,
                    covered_since_datetime.hour,
                    date_str,
                ),
            )
            if not database_rows:
                return (False, 0.0)
            degree_hours = 0.0
            consecutive_warm_hours = 0
            maximum_consecutive_warm_hours = 0
            for database_row in database_rows:
                temperature_c = database_row[2]
                if temperature_c > 0.0:
                    degree_hours += temperature_c
                if temperature_c > 2.0:
                    consecutive_warm_hours += 1
                    maximum_consecutive_warm_hours = max(
                        maximum_consecutive_warm_hours, consecutive_warm_hours
                    )
                else:
                    consecutive_warm_hours = 0
            detection_source = snow_state.get("detection_source", "")
            estimated_depth_mm = snow_state.get("estimated_depth_mm", 5)
            if detection_source == "freezing_rain":
                required_degree_hours = (
                    40.0
                    if estimated_depth_mm <= 3
                    else 60.0 if estimated_depth_mm <= 8 else 90.0
                )
            elif detection_source in ("rime", "rime_hoarfrost"):
                required_degree_hours = 25.0 if estimated_depth_mm <= 2 else 40.0
            else:
                required_degree_hours = (
                    20.0
                    if estimated_depth_mm <= 5
                    else 40.0 if estimated_depth_mm <= 10 else 70.0
                )
            required_consecutive_hours = 4
            warmth_cleared = (
                degree_hours >= required_degree_hours
                and maximum_consecutive_warm_hours >= required_consecutive_hours
            )
            if warmth_cleared:
                _LOGGER.info(
                    "Sustained warmth: %.1f degree-hours (threshold=%.1f), max consecutive warm=%d hours, detection=%s, depth=%.1fmm",
                    degree_hours,
                    required_degree_hours,
                    maximum_consecutive_warm_hours,
                    detection_source,
                    estimated_depth_mm,
                )
            return (warmth_cleared, degree_hours)
        except Exception as tracker_error:
            _LOGGER.debug("Error checking sustained warmth: %s", tracker_error)
            return (False, 0.0)

    async def _check_freeze_lock(self, date_str: str, hour: int) -> bool:
        """Check if conditions make panel clearing physically impossible. @zara V16.2

        Snow/ice cannot melt when surface temperature stays below 0°C.
        If ALL temperatures in the last 12 hours are below freezing,
        melting is physically impossible and clearing must be blocked.
        """
        try:
            current_date = datetime.strptime(date_str, "%Y-%m-%d")
            previous_date_str = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
            database_rows = await self.fetch_all(
                "SELECT temperature_c FROM hourly_weather_actual\n                   WHERE (date = ? AND hour > ?) OR (date = ? AND hour <= ?)\n                   AND temperature_c IS NOT NULL\n                   ORDER BY date, hour",
                (previous_date_str, hour + 12, date_str, hour),
            )
            if not database_rows or len(database_rows) < 4:
                return False
            temperatures = [database_row[0] for database_row in database_rows]
            maximum_temperature_c = max(temperatures)
            if maximum_temperature_c < 0.0:
                _LOGGER.debug(
                    "Freeze lock active: max temp in last 12h = %.1f°C (all below 0)",
                    maximum_temperature_c,
                )
                return True
        except Exception:
            return False
        return False

    async def _check_shadow_detection_feedback(self, date_str: str, hour: int) -> str:
        'Check shadow detection for panel_frost/snow reinforcement. @zara V16.2\n\nIf hourly_shadow_detection has root_cause = panel_frost with significant loss,\nthis is independent confirmation that panels are still covered.\nFeeds back into melt progress as a VETO signal.\n\nReturns:\n    "reinforced" if shadow detection confirms coverage\n    "clearing" if shadow detection suggests improvement\n    "none" if no relevant data\n'
        try:
            prediction_id = f"{date_str}_{hour:02d}"
            database_row = await self.fetch_one(
                "SELECT root_cause, shadow_percent, efficiency_ratio\n                   FROM hourly_shadow_detection\n                   WHERE prediction_id = ?",
                (prediction_id,),
            )
            if not database_row:
                return "none"
            root_cause = database_row[0] or ""
            shadow_percent = database_row[1] or 0.0
            efficiency_ratio = database_row[2] if database_row[2] is not None else 1.0
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
            await self._ensure_snow_tracking_schema()
            database_row = await self.fetch_one(
                "SELECT last_snow_event, panels_covered_since, estimated_depth_mm,\n                          melt_hours, detection_source, cleared_at\n                   FROM snow_tracking WHERE id = 1"
            )
            if database_row:
                return {
                    "last_snow_event": database_row[0],
                    "panels_covered_since": database_row[1],
                    "estimated_depth_mm": database_row[2] or 0.0,
                    "melt_hours": database_row[3] or 0,
                    "detection_source": database_row[4],
                    "cleared_at": database_row[5],
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

    async def _ensure_snow_tracking_schema(self) -> None:
        """Ensure snow_tracking table has all required columns. @zara V16.1"""
        try:
            await self.execute_query(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       last_snow_event TIMESTAMP,\n                       panels_covered_since TIMESTAMP,\n                       estimated_depth_mm REAL DEFAULT 0,\n                       melt_hours REAL DEFAULT 0,\n                       detection_source TEXT,\n                       cleared_at TIMESTAMP,\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            snow_tracking_columns = set()
            try:
                database_rows = await self.fetch_all("PRAGMA table_info(snow_tracking)")
                snow_tracking_columns = (
                    {database_row[1] for database_row in database_rows}
                    if database_rows
                    else set()
                )
            except Exception:
                pass
            for column_definition in (
                "melt_hours REAL DEFAULT 0",
                "detection_source TEXT",
                "cleared_at TIMESTAMP",
            ):
                column_name = column_definition.split()[0]
                if column_name not in snow_tracking_columns:
                    try:
                        await self.execute_query(
                            f"ALTER TABLE snow_tracking ADD COLUMN {column_definition}"
                        )
                        _LOGGER.info(
                            "Migration: Added column %s to snow_tracking", column_name
                        )
                    except Exception:
                        continue
            try:
                forecast_column_rows = await self.fetch_all(
                    "PRAGMA table_info(weather_forecast)"
                )
                forecast_columns = (
                    {database_row[1] for database_row in forecast_column_rows}
                    if forecast_column_rows
                    else set()
                )
                if "weather_code" not in forecast_columns:
                    await self.execute_query(
                        "ALTER TABLE weather_forecast ADD COLUMN weather_code INTEGER"
                    )
                    _LOGGER.info(
                        "Migration: Added weather_code column to weather_forecast"
                    )
            except Exception:
                pass
        except Exception as tracker_error:
            _LOGGER.debug("Error ensuring snow_tracking schema: %s", tracker_error)
        return None if False else None

    async def _update_snow_tracking(self, updates: dict[str, Any]) -> None:
        """Update snow tracking state in database. @zara V16.1"""
        try:
            await self._ensure_snow_tracking_schema()
            await self.execute_query(
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
        except Exception as tracker_error:
            _LOGGER.error("Error updating snow tracking: %s", tracker_error)
        return None if True else None

    async def _send_snow_notification(
        self, snow_type: str, temp_c: float, precip_mm: float, date_str: str, hour: int
    ) -> None:
        """Send notification for snow events. @zara V16.1"""
        try:
            notification_key = f"{date_str}_{snow_type}"
            if self._last_snow_notification_key == notification_key:
                return
            from ..const import DOMAIN as integration_domain

            notification_service = self.hass.data.get(integration_domain, {}).get(
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
        except Exception as tracker_error:
            _LOGGER.error("Error sending snow notification: %s", tracker_error)
        return None if True else None

    @staticmethod
    def _calculate_tilt_melt_factor(tilt_deg: float) -> float:
        """Calculate melt speed factor based on panel tilt angle. @zara V17.0.0

        Steep panels clear snow faster due to gravity and solar angle.
        """
        sin_tilt = math.sin(math.radians(tilt_deg))
        if tilt_deg >= 35:
            return 1.0 + sin_tilt * 1.5
        if tilt_deg >= 25:
            return 1.0 + sin_tilt * 0.8
        return 1.0 + sin_tilt * 0.3

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
        current_temperature_c = weather_data.get("temperature_c", 0.0)
        new_snow_event = system_snow_result.get("snow_event_detected", False)
        try:
            group_states = await self.db.load_snow_tracking_groups()
            for panel_group in self._panel_groups:
                group_name = panel_group.get(
                    "name", f"group_{self._panel_groups.index(panel_group)}"
                )
                tilt_deg = panel_group.get("tilt", 30.0)
                tilt_melt_factor = self._calculate_tilt_melt_factor(tilt_deg)
                group_state = group_states.get(group_name, {})
                was_covered = group_state.get("snow_covered", False)
                if new_snow_event:
                    detection_source = system_snow_result.get(
                        "snow_coverage_source", "unknown"
                    )
                    estimated_depth_mm = system_snow_result.get(
                        "estimated_depth_mm",
                        weather_data.get("estimated_depth_mm", 5.0),
                    )
                    system_snow_state = await self._load_snow_tracking()
                    if system_snow_state:
                        estimated_depth_mm = system_snow_state.get(
                            "estimated_depth_mm", estimated_depth_mm
                        )
                    await self.db.save_snow_tracking_group(
                        group_name,
                        {
                            "tilt_deg": tilt_deg,
                            "last_snow_event": dt_util.now().isoformat(),
                            "panels_covered_since": dt_util.now().isoformat(),
                            "estimated_depth_mm": estimated_depth_mm,
                            "melt_hours": 0,
                            "tilt_melt_factor": tilt_melt_factor,
                            "detection_source": detection_source,
                            "cleared_at": None,
                        },
                    )
                    group_results[group_name] = {
                        "snow_covered": True,
                        "melt_progress": 0.0,
                        "tilt_melt_factor": tilt_melt_factor,
                        "source": detection_source,
                    }
                    continue
                if was_covered:
                    melt_progress = await self._calculate_melt_progress_per_group(
                        group_state,
                        current_temperature_c,
                        date_str,
                        hour,
                        group_name,
                        tilt_melt_factor,
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
                                "tilt_melt_factor": tilt_melt_factor,
                                "detection_source": group_state.get("detection_source"),
                                "cleared_at": dt_util.now().isoformat(),
                            },
                        )
                        group_results[group_name] = {
                            "snow_covered": False,
                            "melt_progress": 1.0,
                            "tilt_melt_factor": tilt_melt_factor,
                            "source": "cleared_by_melt",
                        }
                        _LOGGER.info(
                            "Per-group snow cleared: %s (tilt=%.0f°, factor=%.2f)",
                            group_name,
                            tilt_deg,
                            tilt_melt_factor,
                        )
                        continue
                    group_results[group_name] = {
                        "snow_covered": True,
                        "melt_progress": melt_progress,
                        "tilt_melt_factor": tilt_melt_factor,
                        "source": "persistence",
                    }
                    continue
                if system_snow_result.get("snow_covered_panels", False):
                    group_results[group_name] = {
                        "snow_covered": False,
                        "melt_progress": 1.0,
                        "tilt_melt_factor": tilt_melt_factor,
                        "source": "already_cleared",
                    }
                    continue
                group_results[group_name] = {
                    "snow_covered": False,
                    "melt_progress": 0.0,
                    "tilt_melt_factor": tilt_melt_factor,
                    "source": "none",
                }
                continue
        except Exception as tracker_error:
            _LOGGER.error("Error in per-group snow tracking: %s", tracker_error)
            system_panels_covered = system_snow_result.get("snow_covered_panels", False)
            for panel_group in self._panel_groups:
                group_results[
                    panel_group.get(
                        "name", f"group_{self._panel_groups.index(panel_group)}"
                    )
                ] = {
                    "snow_covered": system_panels_covered,
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
            melt_progress = 0.0
            melt_hours = group_state.get("melt_hours", 0)
            estimated_depth_mm = group_state.get("estimated_depth_mm", 5)
            detection_source = group_state.get("detection_source", "")
            if detection_source == "freezing_rain":
                required_melt_hours = (
                    5.0
                    if estimated_depth_mm <= 3
                    else 8.0 if estimated_depth_mm <= 8 else 12.0
                )
            elif detection_source == "rime":
                required_melt_hours = 3.0 if estimated_depth_mm <= 2 else 5.0
            else:
                required_melt_hours = (
                    2.5
                    if estimated_depth_mm <= 5
                    else 4.0 if estimated_depth_mm <= 10 else 6.0
                )
            if current_temp > 0:
                if 7 <= hour <= 17:
                    hourly_melt_increment = 1.0
                    if current_temp > 3.0:
                        hourly_melt_increment += 0.5
                    if current_temp > 5.0:
                        hourly_melt_increment += 0.5
                    melt_hours += hourly_melt_increment * tilt_melt_factor
                    await self.db.save_snow_tracking_group(
                        group_name,
                        {
                            "tilt_deg": group_state.get("tilt_deg", 30.0),
                            "melt_hours": melt_hours,
                            "last_snow_event": group_state.get("last_snow_event"),
                            "panels_covered_since": group_state.get(
                                "panels_covered_since"
                            ),
                            "estimated_depth_mm": max(0, estimated_depth_mm - 1),
                            "tilt_melt_factor": tilt_melt_factor,
                            "detection_source": detection_source,
                            "cleared_at": None,
                        },
                    )
            melt_hours_progress = min(0.35, melt_hours / required_melt_hours * 0.35)
            if melt_hours > required_melt_hours:
                excess_melt_ratio = min(
                    1.0, (melt_hours - required_melt_hours) / required_melt_hours
                )
                melt_hours_progress += excess_melt_ratio * 0.2
            melt_progress += min(0.55, melt_hours_progress)
            production_recovery_ratio = (
                await self._check_production_recovery_theoretical(date_str, hour)
            )
            if production_recovery_ratio is not None:
                if production_recovery_ratio > 0.5:
                    melt_progress += 0.4
                elif production_recovery_ratio > 0.3:
                    melt_progress += 0.25
                elif production_recovery_ratio > 0.15:
                    melt_progress += 0.1
            if (
                production_recovery_ratio is not None
                and production_recovery_ratio > 0.15
            ):
                _LOGGER.info(
                    "Per-group production override %s: ratio=%.0f%% — panels clear despite freeze",
                    group_name,
                    production_recovery_ratio * 100,
                )
                return 1.0
            warmth_cleared, degree_hours = await self._check_sustained_warmth(
                group_state, date_str, hour
            )
            if warmth_cleared:
                _LOGGER.info(
                    "Per-group warm override %s: %.1f degree-hours — panels clear",
                    group_name,
                    degree_hours,
                )
                return 1.0
            if current_temp > 5.0:
                melt_progress += 0.15
            elif current_temp > 3.0:
                melt_progress += 0.1
            elif current_temp > 1.0:
                melt_progress += 0.05
            temperature_trend_bonus = await self._check_temperature_trend(
                date_str, hour
            )
            melt_progress += min(
                0.1, temperature_trend_bonus * min(1.3, tilt_melt_factor)
            )
            freeze_lock = await self._check_freeze_lock(date_str, hour)
            if freeze_lock:
                melt_progress = min(melt_progress, 0.15)
            if (
                melt_progress >= 0.5
                and production_recovery_ratio is not None
                and (production_recovery_ratio > 0.5)
            ):
                _LOGGER.info(
                    "Snow melt early release for %s: progress=%.2f, production=%.2f",
                    group_name,
                    melt_progress,
                    production_recovery_ratio,
                )
                return 1.0
            panels_covered_since = group_state.get("panels_covered_since")
            if panels_covered_since:
                try:
                    covered_since_datetime = datetime.fromisoformat(
                        panels_covered_since
                    )
                    hours_covered = (
                        dt_util.now() - covered_since_datetime
                    ).total_seconds() / 3600
                    if hours_covered > 48:
                        hours_after_grace = hours_covered - 48
                        safety_timer_bonus = min(0.7, hours_after_grace / 24 * 0.15)
                        melt_progress += safety_timer_bonus
                        _LOGGER.info(
                            "Snow safety timer %s: %dh covered, adding %.2f progress",
                            group_name,
                            int(hours_covered),
                            safety_timer_bonus,
                        )
                except (ValueError, TypeError):
                    pass
            return min(1.0, melt_progress)
        except Exception as tracker_error:
            _LOGGER.debug(
                "Error calculating per-group melt for %s: %s", group_name, tracker_error
            )
            return 0.0

    async def _save_hourly_data(
        self, date_str: str, hour: int, weather_data: dict[str, Any]
    ) -> None:
        """Save hourly weather data to database. @zara"""
        try:
            await self.execute_query(
                "CREATE TABLE IF NOT EXISTS hourly_weather_actual (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       date DATE NOT NULL,\n                       hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                       temperature_c REAL,\n                       humidity_percent REAL,\n                       wind_speed_ms REAL,\n                       precipitation_mm REAL,\n                       precipitation_counter_mm REAL,\n                       precipitation_rate_max_mm_h REAL,\n                       precipitation_semantics TEXT,\n                       precipitation_quality TEXT,\n                       pressure_hpa REAL,\n                       solar_radiation_wm2 REAL,\n                       cloud_cover_percent REAL,\n                       cloud_cover_source TEXT,\n                       frost_detected TEXT,\n                       frost_score REAL,\n                       snow_covered_panels BOOLEAN,\n                       condition TEXT,\n                       source TEXT,\n                       timestamp TIMESTAMP,\n                       UNIQUE(date, hour)\n                   )"
            )
            await self._normalise_hourly_precipitation(date_str, hour, weather_data)
            await self.execute_query(
                "INSERT INTO hourly_weather_actual\n                   (date, hour, temperature_c, humidity_percent, wind_speed_ms,\n                    precipitation_mm, pressure_hpa, solar_radiation_wm2, lux,\n                    precipitation_counter_mm, precipitation_rate_max_mm_h,\n                    precipitation_semantics, precipitation_quality,\n                    cloud_cover_percent, cloud_cover_source, frost_detected,\n                    frost_score, frost_confidence, snow_covered_panels,\n                    snow_event_detected, snow_coverage_source, snow_confidence,\n                    snow_clearing_progress, condition, source, timestamp)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date, hour) DO UPDATE SET\n                       temperature_c = excluded.temperature_c,\n                       humidity_percent = excluded.humidity_percent,\n                       wind_speed_ms = excluded.wind_speed_ms,\n                       precipitation_mm = excluded.precipitation_mm,\n                       precipitation_counter_mm = excluded.precipitation_counter_mm,\n                       precipitation_rate_max_mm_h = excluded.precipitation_rate_max_mm_h,\n                       precipitation_semantics = excluded.precipitation_semantics,\n                       precipitation_quality = excluded.precipitation_quality,\n                       pressure_hpa = excluded.pressure_hpa,\n                       solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                       lux = excluded.lux,\n                       cloud_cover_percent = excluded.cloud_cover_percent,\n                       cloud_cover_source = excluded.cloud_cover_source,\n                       frost_detected = excluded.frost_detected,\n                       frost_score = excluded.frost_score,\n                       frost_confidence = excluded.frost_confidence,\n                       snow_covered_panels = excluded.snow_covered_panels,\n                       snow_event_detected = excluded.snow_event_detected,\n                       snow_coverage_source = excluded.snow_coverage_source,\n                       snow_confidence = excluded.snow_confidence,\n                       snow_clearing_progress = excluded.snow_clearing_progress,\n                       condition = excluded.condition,\n                       source = excluded.source,\n                       timestamp = excluded.timestamp",
                (
                    date_str,
                    hour,
                    weather_data.get("temperature_c"),
                    weather_data.get("humidity_percent"),
                    weather_data.get("wind_speed_ms"),
                    weather_data.get("precipitation_mm"),
                    weather_data.get("pressure_hpa"),
                    weather_data.get("solar_radiation_wm2"),
                    weather_data.get("lux"),
                    weather_data.get("precipitation_counter_mm"),
                    weather_data.get("precipitation_rate_max_mm_h"),
                    weather_data.get("precipitation_semantics"),
                    weather_data.get("precipitation_quality"),
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
            )
            await self._cleanup_old_data()
        except Exception as tracker_error:
            _LOGGER.error("Error saving hourly weather data: %s", tracker_error)
        return None if True else None

    async def _normalise_hourly_precipitation(
        self, date_str: str, hour: int, weather_data: dict[str, Any]
    ) -> None:
        """Turn Recorder-compatible rain snapshots into safe hourly values."""
        precipitation_semantics = weather_data.get("precipitation_semantics")
        if precipitation_semantics not in {"rate", "counter", "daily_total"}:
            return
        current_hour_row = await self.fetch_one(
            "SELECT precipitation_mm, precipitation_counter_mm,\n                      precipitation_rate_max_mm_h\n               FROM hourly_weather_actual WHERE date = ? AND hour = ?",
            (date_str, hour),
        )
        prior_hourly_precipitation_mm = (
            current_hour_row[0] if current_hour_row else None
        )
        if precipitation_semantics == "rate":
            current_rate_max_mm_h = weather_data.get("precipitation_rate_max_mm_h")
            prior_rate_max_mm_h = current_hour_row[2] if current_hour_row else None
            if prior_rate_max_mm_h is not None:
                current_rate_max_mm_h = max(current_rate_max_mm_h, prior_rate_max_mm_h)
            weather_data["precipitation_rate_max_mm_h"] = current_rate_max_mm_h
            weather_data["precipitation_mm"] = prior_hourly_precipitation_mm
            weather_data["precipitation_quality"] = "rate_only"
            return
        current_counter_mm = weather_data.get("precipitation_counter_mm")
        prior_counter_mm = current_hour_row[1] if current_hour_row else None
        if prior_counter_mm is None:
            previous_counter_row = await self.fetch_one(
                "SELECT date, precipitation_counter_mm FROM hourly_weather_actual\n                   WHERE precipitation_counter_mm IS NOT NULL\n                     AND (date < ? OR (date = ? AND hour < ?))\n                   ORDER BY date DESC, hour DESC LIMIT 1",
                (date_str, date_str, hour),
            )
            prior_counter_mm = previous_counter_row[1] if previous_counter_row else None
            prior_counter_date = (
                previous_counter_row[0] if previous_counter_row else None
            )
        else:
            prior_counter_date = date_str
        if prior_counter_mm is None:
            weather_data["precipitation_mm"] = prior_hourly_precipitation_mm
            weather_data["precipitation_quality"] = "counter_baseline"
            return
        if current_counter_mm < prior_counter_mm:
            if (
                precipitation_semantics == "daily_total"
                and prior_counter_date != date_str
            ):
                weather_data["precipitation_mm"] = current_counter_mm
                weather_data["precipitation_quality"] = "daily_total"
                return
            weather_data["precipitation_mm"] = prior_hourly_precipitation_mm
            weather_data["precipitation_quality"] = "counter_reset"
            return
        counter_delta_mm = current_counter_mm - prior_counter_mm
        weather_data["precipitation_mm"] = round(
            (prior_hourly_precipitation_mm or 0.0) + counter_delta_mm, 4
        )
        weather_data["precipitation_quality"] = "counter_delta"

    async def _cleanup_old_data(self) -> None:
        """Remove data older than retention period. @zara"""
        try:
            cutoff_date_str = (
                dt_util.now() - timedelta(days=DATA_RETENTION_DAYS)
            ).strftime("%Y-%m-%d")
            await self.execute_query(
                "DELETE FROM hourly_weather_actual WHERE date < ?", (cutoff_date_str,)
            )
        except Exception:
            pass
        return None if True else None

    async def get_actual_weather(
        self, date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Get actual weather data for specific date and hour. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT temperature_c, humidity_percent, wind_speed_ms,\n                          precipitation_mm, pressure_hpa, solar_radiation_wm2,\n                          cloud_cover_percent, cloud_cover_source, frost_detected,\n                          frost_score, frost_confidence, lux, snow_covered_panels,\n                          condition, precipitation_rate_max_mm_h,\n                          precipitation_semantics, precipitation_quality\n                   FROM hourly_weather_actual\n                   WHERE date = ? AND hour = ?",
                (date_str, hour),
            )
            if not database_row:
                return
            return {
                "temperature_c": database_row[0],
                "humidity_percent": database_row[1],
                "wind_speed_ms": database_row[2],
                "precipitation_mm": database_row[3],
                "pressure_hpa": database_row[4],
                "solar_radiation_wm2": database_row[5],
                "cloud_cover_percent": database_row[6],
                "cloud_cover_source": database_row[7],
                "frost_detected": database_row[8],
                "frost_score": database_row[9],
                "frost_confidence": database_row[10],
                "lux": database_row[11],
                "snow_covered_panels": database_row[12],
                "condition": database_row[13],
                "precipitation_rate_max_mm_h": database_row[14],
                "precipitation_semantics": database_row[15],
                "precipitation_quality": database_row[16],
            }
        except Exception:
            return

    async def get_daily_actual_weather(
        self, date_str: str
    ) -> dict[int, dict[str, Any]]:
        """Get all actual weather data for a specific day. @zara V16.2"""
        try:
            database_rows = await self.fetch_all(
                "SELECT hour, temperature_c, humidity_percent, wind_speed_ms,\n                          precipitation_mm, pressure_hpa, solar_radiation_wm2,\n                          cloud_cover_percent, cloud_cover_source,\n                          frost_detected, frost_score, frost_confidence,\n                          snow_covered_panels, precipitation_rate_max_mm_h,\n                          precipitation_semantics, precipitation_quality\n                   FROM hourly_weather_actual\n                   WHERE date = ?\n                   ORDER BY hour",
                (date_str,),
            )
            weather_by_hour = {}
            for database_row in database_rows:
                weather_by_hour[database_row[0]] = {
                    "temperature_c": database_row[1],
                    "humidity_percent": database_row[2],
                    "wind_speed_ms": database_row[3],
                    "precipitation_mm": database_row[4],
                    "pressure_hpa": database_row[5],
                    "solar_radiation_wm2": database_row[6],
                    "cloud_cover_percent": database_row[7],
                    "cloud_cover_source": database_row[8],
                    "frost_detected": database_row[9],
                    "frost_score": database_row[10],
                    "frost_confidence": database_row[11],
                    "snow_covered_panels": (
                        bool(database_row[12]) if database_row[12] else False
                    ),
                    "precipitation_rate_max_mm_h": database_row[13],
                    "precipitation_semantics": database_row[14],
                    "precipitation_quality": database_row[15],
                }
            return weather_by_hour
        except Exception:
            return {}

    async def get_snow_tracking_status(self) -> dict[str, Any]:
        """Get current snow tracking status with type-specific diagnostics. @zara V16.2"""
        snow_state = await self._load_snow_tracking()
        panels_covered = snow_state.get("panels_covered_since") is not None
        melt_hours = snow_state.get("melt_hours", 0)
        estimated_depth_mm = snow_state.get("estimated_depth_mm", 0.0)
        detection_source = snow_state.get("detection_source", "")
        clearing_progress = 0.0
        if panels_covered and estimated_depth_mm > 0:
            if detection_source == "freezing_rain":
                required_melt_hours = 8.0
            elif detection_source == "rime":
                required_melt_hours = 3.0
            else:
                required_melt_hours = 3.0 if estimated_depth_mm <= 5 else 5.0
            clearing_progress = min(1.0, melt_hours / required_melt_hours)
        return {
            "panels_covered": panels_covered,
            "snow_covered_panels": panels_covered,
            "last_snow_event": snow_state.get("last_snow_event"),
            "estimated_depth_mm": estimated_depth_mm,
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
        except Exception as tracker_error:
            _LOGGER.debug("Could not load per-group snow status: %s", tracker_error)
            return {}
