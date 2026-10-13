# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Forecast Corrector V16.2.0 - Creates Corrected Weather Forecasts

Creates corrected weather forecasts by applying precision factors to Open-Meteo data.
Combines multi-source blending with learned correction factors.

Architecture:
- Open-Meteo provides raw regional forecast data
- WeatherExpertBlender provides multi-source blended cloud cover
- Precision tracking calculates correction factors
- This class APPLIES those factors to create corrected forecasts

Database Tables: weather_forecast
@zara
"""
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from ..const import WINTER_MONTHS, WINTER_CLOUD_PENALTY_FACTOR, WINTER_LOW_SUN_THRESHOLD

_LOGGER = logging.getLogger(__name__)
DEFAULT_WEATHER: dict[str, Any] = {
    "temperature": 15.0,
    "solar_radiation_wm2": 150.0,
    "wind": 3.0,
    "humidity": 70.0,
    "rain": 0.0,
    "clouds": 50.0,
    "pressure": 1013.0,
    "source": "default_fallback",
    "confidence": 0.1,
}


def calculate_dni_dhi_ratio(
    cloud_cover: float, sun_elevation: float = 45.0
) -> tuple[float, float]:
    "Calculate dynamic DNI/DHI ratio based on cloud cover and sun elevation. @zara\n\nAt low sun angles, diffuse radiation dominates due to longer atmospheric path.\n\nArgs:\n    cloud_cover: Cloud cover percentage (0-100)\n    sun_elevation: Sun elevation in degrees (0-90)\n\nReturns:\n    Tuple of (dni_fraction, dhi_fraction)\n"
    cloud_percent = max(0.0, min(100.0, cloud_cover or 0.0))
    sun_elevation_deg = max(0.0, min(90.0, sun_elevation or 45.0))
    if sun_elevation_deg < 10:
        clear_direct_fraction, diffuse_reference_fraction = (0.45, 0.55)
        overcast_direct_fraction, diffuse_reference_fraction = (0.05, 0.95)
    elif sun_elevation_deg < 20:
        clear_direct_fraction, diffuse_reference_fraction = (0.6, 0.4)
        overcast_direct_fraction, diffuse_reference_fraction = (0.1, 0.9)
    elif sun_elevation_deg < 30:
        clear_direct_fraction, diffuse_reference_fraction = (0.72, 0.28)
        overcast_direct_fraction, diffuse_reference_fraction = (0.12, 0.88)
    else:
        clear_direct_fraction, diffuse_reference_fraction = (0.85, 0.15)
        overcast_direct_fraction, diffuse_reference_fraction = (0.15, 0.85)
    dni_fraction = clear_direct_fraction - (
        clear_direct_fraction - overcast_direct_fraction
    ) * (cloud_percent / 100.0)
    dhi_fraction = 1.0 - dni_fraction
    return (dni_fraction, dhi_fraction)


def calculate_effective_cloud_factor(
    cloud_cover: float,
    sun_elevation: float,
    month: int,
    winter_mode_enabled: bool = True,
) -> float:
    "Calculate effective cloud factor with winter and low-sun corrections. @zara\n\nAt low sun angles, clouds have disproportionately stronger effect\ndue to longer atmospheric path through clouds.\n\nArgs:\n    cloud_cover: Cloud cover percentage (0-100)\n    sun_elevation: Sun elevation in degrees (0-90)\n    month: Current month (1-12)\n    winter_mode_enabled: Whether winter cloud penalty is active (UI toggle)\n\nReturns:\n    Effective cloud factor (0.05 - 1.0)\n"
    cloud_percent = max(0.0, min(100.0, cloud_cover or 0.0))
    sun_elevation_deg = max(0.0, min(90.0, sun_elevation or 0.0))
    if sun_elevation_deg <= 0:
        return 0.05
    clear_fraction = (100.0 - cloud_percent) / 100.0
    if cloud_percent < 5.0:
        return min(1.0, clear_fraction)
    winter_active = winter_mode_enabled and month in WINTER_MONTHS
    winter_penalty = WINTER_CLOUD_PENALTY_FACTOR if winter_active else 1.0
    if sun_elevation_deg < WINTER_LOW_SUN_THRESHOLD:
        cloud_penalty_exponent = (
            1.0
            + (WINTER_LOW_SUN_THRESHOLD - sun_elevation_deg)
            / WINTER_LOW_SUN_THRESHOLD
            * winter_penalty
        )
        effective_cloud_factor = clear_fraction**cloud_penalty_exponent
    else:
        effective_cloud_factor = clear_fraction
    return max(0.05, min(1.0, effective_cloud_factor))


def detect_fog_from_meteorology(
    temperature: float,
    dewpoint: Optional[float],
    humidity: Optional[float],
    cloud_cover: float,
    hour: int,
) -> tuple[bool, float, str]:
    "Detect fog conditions using meteorological indicators. @zara\n\nArgs:\n    temperature: Air temperature in C\n    dewpoint: Dewpoint temperature in C (optional)\n    humidity: Relative humidity in % (optional)\n    cloud_cover: Cloud cover percentage\n    hour: Hour of day (0-23)\n\nReturns:\n    Tuple of (is_fog, confidence, fog_type)\n"
    fog_score = 0.0
    fog_type = "none"
    if temperature is None:
        return (
            (((False, 0.0, "none") if True else None) if True else None)
            if True
            else None
        )
    if dewpoint is not None:
        dewpoint_spread_c = temperature - dewpoint
        if dewpoint_spread_c < 1.0:
            fog_score += 0.45
            fog_type = "radiation" if 5 <= hour <= 10 else "advection"
        elif dewpoint_spread_c < 2.0:
            fog_score += 0.3
            fog_type = "radiation" if 5 <= hour <= 10 else "advection"
        elif dewpoint_spread_c < 3.0:
            fog_score += 0.15
    if humidity is not None:
        if humidity >= 98:
            fog_score += 0.25
        elif humidity >= 95:
            fog_score += 0.18
        elif humidity >= 90:
            fog_score += 0.1
    if cloud_cover >= 95:
        fog_score += 0.12
        if fog_type == "none":
            fog_type = "stratus_fog"
    elif cloud_cover >= 85:
        fog_score += 0.08
    if 5 <= hour <= 10:
        fog_score += 0.08
    elif 4 <= hour <= 11:
        fog_score += 0.04
    if -2 <= temperature <= 5:
        fog_score += 0.05
    fog_detected = fog_score >= 0.45
    return (
        (
            (
                (
                    fog_detected,
                    min(1.0, fog_score),
                    fog_type if fog_detected else "none",
                )
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


class WeatherForecastCorrector(DataManagerIO):
    """Creates corrected weather forecasts with precision factors applied. @zara

    All data stored in and retrieved from database via DatabaseManager.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        open_meteo_client=None,
        expert_blender=None,
        winter_mode: bool = True,
        astronomy_provider=None,
    ):
        "Initialize the weather corrector. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for data persistence\n    open_meteo_client: OpenMeteoClient instance\n    expert_blender: WeatherExpertBlender instance\n    winter_mode: Whether winter cloud penalty is active (UI toggle)\n"
        super().__init__(hass, db_manager)
        self._open_meteo_client = open_meteo_client
        self._expert_blender = expert_blender
        self._winter_mode = winter_mode
        self._astronomy_provider = astronomy_provider
        self._consecutive_failures = 0
        self._last_error = None
        self._weather_mlp = None
        self._kalman_tracker = None
        self.data_dir = Path(hass.config.path("solar_forecast_eai"))
        self.stats_dir = self.data_dir / "stats"
        self.integration_cache_file = self.stats_dir / "weather_integration_ml.json"
        self.integration_history_dir = self.stats_dir / "integration_cache_history"
        _LOGGER.info("WeatherForecastCorrector initialized")

    def set_expert_blender(self, expert_blender) -> None:
        """Set the expert blender for multi-source cloud cover. @zara"""
        self._expert_blender = expert_blender
        _LOGGER.info("WeatherForecastCorrector: Expert blender attached")

    def set_weather_mlp(self, mlp) -> None:
        """Set Weather MLP for GHI correction. @zara"""
        self._weather_mlp = mlp
        _LOGGER.info("WeatherForecastCorrector: Weather MLP attached")

    def set_kalman_tracker(self, tracker) -> None:
        """Set Kalman tracker for intraday bias correction. @zara"""
        self._kalman_tracker = tracker
        _LOGGER.info("WeatherForecastCorrector: Kalman tracker attached")

    async def async_init(self) -> bool:
        """Initialize and load data. @zara"""
        await self.ensure_initialized()
        return True

    async def create_corrected_forecast(self, min_confidence: float = 0.0) -> bool:
        "Create corrected weather forecast with precision factors applied. @zara\n\nV16.1: Now uses hourly factors and weather-specific factors.\n\nArgs:\n    min_confidence: Minimum confidence threshold\n\nReturns:\n    True if forecast created successfully\n"
        try:
            correction_data = await self._load_correction_factors()
            global_factors = correction_data["factors"]
            hourly_factors_data = correction_data.get("hourly_factors", {})
            weather_factors_data = correction_data.get("weather_factors", {})
            raw_forecasts = await self._load_raw_forecasts()
            if not raw_forecasts:
                _LOGGER.warning(
                    "No raw forecast data available from weather_cache_open_meteo"
                )
                return False
            _LOGGER.info(
                "Loaded %d dates from weather_cache_open_meteo (RAW)",
                len(raw_forecasts),
            )
            saved_hours = 0
            for forecast_date, day_forecast in raw_forecasts.items():
                daytime_cloud_values = []
                for hour_key, raw_hour in day_forecast.items():
                    daytime_hour = int(hour_key)
                    if 7 <= daytime_hour <= 17:
                        hour_cloud_percent = raw_hour.get("cloud_cover")
                        if hour_cloud_percent is not None:
                            daytime_cloud_values.append(hour_cloud_percent)
                weather_category = "mixed"
                if daytime_cloud_values:
                    average_cloud_percent = sum(daytime_cloud_values) / len(
                        daytime_cloud_values
                    )
                    if average_cloud_percent <= 30:
                        weather_category = "clear"
                    elif average_cloud_percent >= 50:
                        weather_category = "cloudy"
                for hour_key, raw_hour in day_forecast.items():
                    forecast_hour = int(hour_key)
                    weather_hour = await self._get_weather_for_hour_async(
                        forecast_date, forecast_hour, raw_hour
                    )
                    corrected_weather = self._apply_corrections(
                        weather_hour,
                        global_factors,
                        hourly_factors_data,
                        weather_factors_data,
                        weather_category,
                        forecast_hour,
                    )
                    forecast_saved = await self._save_corrected_forecast(
                        forecast_date, forecast_hour, corrected_weather
                    )
                    if forecast_saved:
                        saved_hours += 1
                continue
            if saved_hours == 0:
                _LOGGER.error("No forecast hours processed")
                return False
            _LOGGER.info(
                "Created corrected forecast: %d hours written to weather_forecast",
                saved_hours,
            )
        except Exception as error:
            _LOGGER.error("Error creating forecast: %s", error, exc_info=True)
            return False
        return True

    async def _load_raw_forecasts(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Load RAW forecast data from Open-Meteo cache (NOT corrected!). @zara

        V16.1 FIX: Reads from weather_cache_open_meteo to avoid circular dependency.
        The corrected forecasts will be written to weather_forecast.
        """
        try:
            today_iso = dt_util.now().date().isoformat()
            forecast_rows = await self.fetch_all(
                "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                          precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                          ghi, cloud_cover_low, cloud_cover_mid,\n                          cloud_cover_high, visibility_m, weather_code, snowfall, rain\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date >= ?\n                   ORDER BY forecast_date, hour",
                (today_iso,),
            )
            forecasts_by_date = {}
            for row in forecast_rows:
                forecast_date = str(row[0])
                hour_key = str(row[1])
                if forecast_date not in forecasts_by_date:
                    forecasts_by_date[forecast_date] = {}
                forecasts_by_date[forecast_date][hour_key] = {
                    "temperature": row[2],
                    "humidity": row[3],
                    "cloud_cover": row[4],
                    "precipitation": row[5],
                    "wind_speed": row[6],
                    "pressure": row[7],
                    "direct_radiation": row[8],
                    "diffuse_radiation": row[9],
                    "ghi": row[10],
                    "cloud_cover_low": row[11],
                    "cloud_cover_mid": row[12],
                    "cloud_cover_high": row[13],
                    "visibility_m": row[14],
                    "weather_code": row[15],
                    "snowfall": row[16],
                    "rain": row[17],
                }
            return forecasts_by_date
        except Exception as error:
            _LOGGER.error("Error loading raw forecasts: %s", error)
            return {}

    async def _load_correction_factors(self) -> dict[str, Any]:
        """Load precision correction factors including hourly + weather-specific. @zara

        V16.1: Now loads three types of factors:
        - Daily rolling averages (baseline)
        - Hourly factors (hours 7-17) for solar and clouds
        - Weather-specific factors (clear/cloudy days)
        """
        default_factors = {
            "temperature": 0.0,
            "solar_radiation_wm2": 1.0,
            "clouds": 1.0,
            "wind": 1.0,
            "humidity": 1.0,
            "rain": 1.0,
            "pressure": 0.0,
        }
        try:
            row = await self.fetch_one(
                "SELECT temperature_factor, solar_factor, cloud_factor,\n                          wind_factor, humidity_factor, rain_factor,\n                          pressure_factor, sample_days\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            global_factors = default_factors.copy()
            sample_days = 0
            if row:
                global_factors = {
                    "temperature": row[0] or 0.0,
                    "solar_radiation_wm2": row[1] or 1.0,
                    "clouds": row[2] or 1.0,
                    "wind": row[3] or 1.0,
                    "humidity": row[4] or 1.0,
                    "rain": row[5] or 1.0,
                    "pressure": row[6] or 0.0,
                }
                sample_days = row[7] or 0
            hourly_factors_data = {"solar_radiation_wm2": {}, "clouds": {}}
            hourly_factor_rows = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            for hourly_factor_row in hourly_factor_rows:
                hour_key, factor_type, factor_value, sample_count, confidence = (
                    hourly_factor_row
                )
                if factor_type in hourly_factors_data:
                    hourly_factors_data[factor_type][str(hour_key)] = {
                        "factor": factor_value,
                        "samples": sample_count,
                        "confidence": confidence or 0.0,
                    }
            weather_factors_data = {"clear": {}, "cloudy": {}}
            weather_factor_rows = await self.fetch_all(
                "SELECT weather_type, factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific"
            )
            for weather_factor_row in weather_factor_rows:
                weather_category, factor_type, factor_value, confidence = (
                    weather_factor_row
                )
                if (
                    weather_category in weather_factors_data
                    and confidence
                    and (confidence >= 0.3)
                ):
                    weather_factors_data[weather_category][factor_type] = factor_value
            return {
                "factors": global_factors,
                "hourly_factors": hourly_factors_data,
                "weather_factors": weather_factors_data,
                "sample_days": sample_days,
            }
        except Exception as error:
            _LOGGER.debug("Using default correction factors: %s", error)
            return {
                "factors": default_factors,
                "hourly_factors": {"solar_radiation_wm2": {}, "clouds": {}},
                "weather_factors": {"clear": {}, "cloudy": {}},
                "sample_days": 0,
            }

    async def _get_weather_for_hour_async(
        self, date_str: str, hour: int, raw_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Get weather for hour with blended cloud cover. @zara"""
        cloud_cover_percent = raw_data.get("cloud_cover") or 0.0
        cloud_source = "raw"
        blend_info = None
        if self._expert_blender:
            try:
                cloud_cover_percent, blend_info = (
                    await self._expert_blender.get_blended_cloud_cover(
                        date=date_str,
                        hour=hour,
                        cloud_low=raw_data.get("cloud_cover_low"),
                        cloud_mid=raw_data.get("cloud_cover_mid"),
                        cloud_high=raw_data.get("cloud_cover_high"),
                        visibility_m=raw_data.get("visibility_m"),
                    )
                )
                cloud_source = "expert_blend"
            except Exception as error:
                _LOGGER.debug("Expert blending failed: %s", error)
        sun_elevation_deg = await self._get_sun_elevation(date_str, hour)
        try:
            month = int(date_str.split("-")[1])
        except (IndexError, ValueError):
            month = 6
        weighted_poa_wm2 = await self._get_weighted_poa_radiation(date_str, hour)
        cloud_factor = calculate_effective_cloud_factor(
            cloud_cover=cloud_cover_percent,
            sun_elevation=sun_elevation_deg,
            month=month,
            winter_mode_enabled=self._winter_mode,
        )
        raw_ghi_wm2 = raw_data.get("ghi") or raw_data.get("solar_radiation_wm2")
        if raw_ghi_wm2 is not None and raw_ghi_wm2 > 0:
            corrected_radiation_wm2 = float(raw_ghi_wm2)
            if self._weather_mlp is not None and self._weather_mlp.is_trained:
                mlp_factor = self._weather_mlp.predict_correction(
                    raw_ghi=raw_ghi_wm2,
                    cloud_cover=cloud_cover_percent,
                    sun_elevation=sun_elevation_deg,
                    humidity=raw_data.get("humidity"),
                    pressure=raw_data.get("pressure"),
                    wind=raw_data.get("wind_speed"),
                    hour=hour,
                )
                mlp_factor = max(0.5, min(1.5, mlp_factor))
                corrected_radiation_wm2 = corrected_radiation_wm2 * mlp_factor
                _LOGGER.debug(
                    "MLP corrected GHI for %s hour %d: %.1f * %.3f = %.1f W/m²",
                    date_str,
                    hour,
                    raw_ghi_wm2,
                    mlp_factor,
                    corrected_radiation_wm2,
                )
            else:
                _LOGGER.debug(
                    "Using Open-Meteo GHI for %s hour %d: %.1f W/m² (cloud_cover=%.0f%%)",
                    date_str,
                    hour,
                    corrected_radiation_wm2,
                    cloud_cover_percent,
                )
            if self._kalman_tracker is not None and sun_elevation_deg > 5:
                kalman_offset_wm2 = self._kalman_tracker.get_correction()
                if kalman_offset_wm2 != 0.0:
                    corrected_radiation_wm2 = max(
                        0.0, corrected_radiation_wm2 + kalman_offset_wm2
                    )
                    _LOGGER.debug(
                        "Kalman bias for %s hour %d: %.1f W/m²",
                        date_str,
                        hour,
                        kalman_offset_wm2,
                    )
        else:
            corrected_radiation_wm2 = weighted_poa_wm2 * cloud_factor
            _LOGGER.debug(
                "Fallback calculation for %s hour %d: %.1f * %.3f = %.1f W/m²",
                date_str,
                hour,
                weighted_poa_wm2,
                cloud_factor,
                corrected_radiation_wm2,
            )
        dni_fraction, dhi_fraction = calculate_dni_dhi_ratio(
            cloud_cover_percent, sun_elevation_deg
        )
        visibility_m = raw_data.get("visibility_m")
        has_fog = False
        fog_type = None
        if visibility_m is not None and visibility_m < 1000:
            has_fog = True
            fog_type = "dense" if visibility_m < 200 else "light"
        elif visibility_m is None:
            temperature_c = raw_data.get("temperature")
            humidity_percent = raw_data.get("humidity")
            dewpoint_c = None
            if (
                temperature_c is not None
                and humidity_percent is not None
                and (humidity_percent > 50)
            ):
                dewpoint_c = temperature_c - (100 - humidity_percent) / 5.0
            has_fog, fog_confidence, fog_type = detect_fog_from_meteorology(
                temperature_c, dewpoint_c, humidity_percent, cloud_cover_percent, hour
            )
        return (
            (
                {
                    "temperature": raw_data.get("temperature"),
                    "solar_radiation_wm2": round(corrected_radiation_wm2, 1),
                    "wind": raw_data.get("wind_speed"),
                    "humidity": raw_data.get("humidity"),
                    "rain": raw_data.get("precipitation") or raw_data.get("rain") or 0,
                    "clouds": cloud_cover_percent,
                    "weather_code": raw_data.get("weather_code"),
                    "snowfall": raw_data.get("snowfall"),
                    "cloud_cover_low": raw_data.get("cloud_cover_low"),
                    "cloud_cover_mid": raw_data.get("cloud_cover_mid"),
                    "cloud_cover_high": raw_data.get("cloud_cover_high"),
                    "pressure": raw_data.get("pressure"),
                    "direct_radiation": round(
                        corrected_radiation_wm2 * dni_fraction, 1
                    ),
                    "diffuse_radiation": round(
                        corrected_radiation_wm2 * dhi_fraction, 1
                    ),
                    "clear_sky_poa_radiation": round(weighted_poa_wm2, 1),
                    "sun_elevation": round(sun_elevation_deg, 1),
                    "cloud_factor_effective": round(cloud_factor, 3),
                    "visibility_m": visibility_m,
                    "fog_detected": has_fog,
                    "fog_type": fog_type,
                    "source": cloud_source,
                    "confidence": 0.95 if cloud_source == "expert_blend" else 0.85,
                }
                if True
                else None
            )
            if True
            else None
        )

    async def _get_sun_elevation(self, date_str: str, hour: int) -> float:
        """Get sun elevation from the validated SFML provider."""
        try:
            astronomy_by_datetime = (
                await self._astronomy_provider.async_get_legacy_days(
                    datetime.fromisoformat(date_str).date(), days=1
                )
                if self._astronomy_provider
                else {}
            )
            sun_elevation = astronomy_by_datetime.get(f"{date_str}_{hour:02d}", {}).get(
                "sun_elevation_deg"
            )
            return float(sun_elevation) if sun_elevation is not None else 0.0
        except Exception:
            return 0.0

    async def _get_weighted_poa_radiation(self, date_str: str, hour: int) -> float:
        """Get capacity-weighted POA radiation from per-panel-group data. @zara

        SFML contract v1 has no panel-group POA; uses horizontal clear-sky GHI.
        """
        try:
            astronomy_by_datetime = (
                await self._astronomy_provider.async_get_legacy_days(
                    datetime.fromisoformat(date_str).date(), days=1
                )
                if self._astronomy_provider
                else {}
            )
            weighted_poa_radiation = astronomy_by_datetime.get(
                f"{date_str}_{hour:02d}", {}
            ).get("clear_sky_radiation_wm2")
            return (
                float(weighted_poa_radiation)
                if weighted_poa_radiation is not None
                else 0.0
            )
        except Exception:
            return 0.0

    def _apply_corrections(
        self,
        weather: dict[str, Any],
        factors: dict[str, float],
        hourly_factors: dict[str, dict[str, Any]] = None,
        weather_factors: dict[str, dict[str, float]] = None,
        weather_type: str = "mixed",
        hour: int = 12,
    ) -> dict[str, Any]:
        """Apply correction factors including hourly + weather-specific. @zara

        V16.1: Uses hourly factors for solar/clouds (hours 7-17) and
        weather-specific factors for clear/cloudy days.
        """
        corrected_weather = weather.copy()
        hourly_factors = hourly_factors or {}
        weather_factors = weather_factors or {}
        if corrected_weather.get("temperature") is not None:
            temperature_offset_c = factors.get("temperature", 0.0)
            if weather_type in weather_factors:
                temperature_offset_c = weather_factors[weather_type].get(
                    "temperature", temperature_offset_c
                )
            corrected_weather["temperature"] = round(
                corrected_weather["temperature"] + temperature_offset_c, 1
            )
        mlp_trained = self._weather_mlp is not None and self._weather_mlp.is_trained
        if corrected_weather.get("solar_radiation_wm2") is not None:
            if mlp_trained:
                radiation_factor = 1.0
            else:
                radiation_factor = factors.get("solar_radiation_wm2", 1.0)
            if not mlp_trained and 7 <= hour <= 17:
                hourly_radiation_factors = hourly_factors.get("solar_radiation_wm2", {})
                hour_factor = hourly_radiation_factors.get(str(hour))
                if hour_factor and "factor" in hour_factor:
                    radiation_factor = hour_factor["factor"]
                    _LOGGER.debug(
                        "Using hourly solar factor %.3f for hour %d",
                        radiation_factor,
                        hour,
                    )
            elif not mlp_trained:
                if weather_type in weather_factors:
                    weather_radiation_factor = weather_factors[weather_type].get(
                        "solar_radiation_wm2"
                    )
                    if weather_radiation_factor is not None:
                        radiation_factor = weather_radiation_factor
            radiation_factor = max(0.75, min(1.3, radiation_factor))
            corrected_weather["solar_radiation_wm2"] = round(
                max(0.0, corrected_weather["solar_radiation_wm2"] * radiation_factor), 1
            )
            if corrected_weather.get("direct_radiation") is not None:
                corrected_weather["direct_radiation"] = round(
                    max(0.0, corrected_weather["direct_radiation"] * radiation_factor),
                    1,
                )
            if corrected_weather.get("diffuse_radiation") is not None:
                corrected_weather["diffuse_radiation"] = round(
                    max(0.0, corrected_weather["diffuse_radiation"] * radiation_factor),
                    1,
                )
        for weather_field, factor_key in (
            ("wind", "wind"),
            ("humidity", "humidity"),
            ("rain", "rain"),
        ):
            if corrected_weather.get(weather_field) is not None:
                field_factor = factors.get(factor_key, 1.0)
                if weather_type in weather_factors:
                    weather_field_factor = weather_factors[weather_type].get(factor_key)
                    if weather_field_factor is not None:
                        field_factor = weather_field_factor
                corrected_weather[weather_field] = round(
                    corrected_weather[weather_field] * field_factor, 1
                )
                if weather_field == "humidity":
                    corrected_weather[weather_field] = max(
                        0.0, min(100.0, corrected_weather[weather_field])
                    )
                else:
                    corrected_weather[weather_field] = max(
                        0.0, corrected_weather[weather_field]
                    )
        if corrected_weather.get("pressure") is not None:
            pressure_offset = factors.get("pressure", 0.0)
            if weather_type in weather_factors:
                pressure_offset = weather_factors[weather_type].get(
                    "pressure", pressure_offset
                )
            corrected_weather["pressure"] = round(
                corrected_weather["pressure"] + pressure_offset, 1
            )
        return corrected_weather

    async def _save_corrected_forecast(
        self, date_str: str, hour: int, data: dict[str, Any]
    ) -> bool:
        """Save corrected forecast to database. @zara"""
        try:
            await self.execute_query(
                "INSERT INTO weather_forecast\n                   (forecast_date, hour, temperature, humidity, clouds, rain,\n                    wind, pressure, direct_radiation, diffuse_radiation,\n                    solar_radiation_wm2, cloud_cover_low, cloud_cover_mid,\n                    cloud_cover_high, visibility_m, fog_detected, fog_type,\n                    weather_code)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       temperature = excluded.temperature,\n                       humidity = excluded.humidity,\n                       clouds = excluded.clouds,\n                       rain = excluded.rain,\n                       wind = excluded.wind,\n                       pressure = excluded.pressure,\n                       direct_radiation = excluded.direct_radiation,\n                       diffuse_radiation = excluded.diffuse_radiation,\n                       solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                       cloud_cover_low = excluded.cloud_cover_low,\n                       cloud_cover_mid = excluded.cloud_cover_mid,\n                       cloud_cover_high = excluded.cloud_cover_high,\n                       visibility_m = excluded.visibility_m,\n                       fog_detected = excluded.fog_detected,\n                       fog_type = excluded.fog_type,\n                       weather_code = excluded.weather_code,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    date_str,
                    hour,
                    data.get("temperature"),
                    data.get("humidity"),
                    data.get("clouds"),
                    data.get("rain"),
                    data.get("wind"),
                    data.get("pressure"),
                    data.get("direct_radiation"),
                    data.get("diffuse_radiation"),
                    data.get("solar_radiation_wm2"),
                    data.get("cloud_cover_low"),
                    data.get("cloud_cover_mid"),
                    data.get("cloud_cover_high"),
                    data.get("visibility_m"),
                    data.get("fog_detected"),
                    data.get("fog_type"),
                    data.get("weather_code"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Error saving corrected forecast: %s", error)
            return False
        return True

    async def get_corrected_weather_for_hour(
        self, target_datetime: datetime
    ) -> Optional[dict[str, Any]]:
        """Get corrected weather for specific datetime. @zara"""
        forecast_date = target_datetime.date().isoformat()
        hour_key = target_datetime.hour
        try:
            row = await self.fetch_one(
                "SELECT temperature, humidity, clouds, rain, wind, pressure,\n                          direct_radiation, diffuse_radiation, solar_radiation_wm2,\n                          visibility_m, fog_detected, fog_type\n                   FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (forecast_date, hour_key),
            )
            if row:
                return {
                    "temperature": row[0],
                    "humidity": row[1],
                    "clouds": row[2],
                    "rain": row[3],
                    "wind": row[4],
                    "pressure": row[5],
                    "direct_radiation": row[6],
                    "diffuse_radiation": row[7],
                    "solar_radiation_wm2": row[8],
                    "visibility_m": row[9],
                    "fog_detected": row[10],
                    "fog_type": row[11],
                    "_source": "corrected_forecast",
                }
            return self._get_default_weather_for_hour(hour_key)
        except Exception as error:
            _LOGGER.error("Error getting weather for hour: %s", error)
            return self._get_default_weather_for_hour(hour_key)

    def _get_default_weather_for_hour(self, hour: int) -> dict[str, Any]:
        """Get default weather for hour. @zara"""
        daily_temperature_phase = (hour - 14) / 24.0 * 2 * math.pi
        temperature_c = DEFAULT_WEATHER["temperature"] + 5.0 * math.cos(
            daily_temperature_phase
        )
        default_weather = DEFAULT_WEATHER.copy()
        default_weather["temperature"] = round(temperature_c, 1)
        return default_weather

    async def get_correction_summary(self) -> dict[str, Any]:
        """Get summary of applied corrections. @zara"""
        try:
            correction_data = await self._load_correction_factors()
            return {
                "factors": correction_data["factors"],
                "sample_days": correction_data["sample_days"],
            }
        except Exception as error:
            _LOGGER.error("Error getting correction summary: %s", error)
            return {}

    async def update_integration_cache(self) -> bool:
        """Update weather integration cache. @zara

        For ML Weather integration - creates a fresh blended forecast.
        """
        try:
            return await self.create_corrected_forecast()
        except Exception as error:
            _LOGGER.error("Error updating integration cache: %s", error)
            return False

    async def bootstrap_integration_cache(self) -> bool:
        """Bootstrap integration cache if needed. @zara"""
        try:
            today_iso = dt_util.now().date().isoformat()
            row = await self.fetch_one(
                "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
                (today_iso,),
            )
            if row and row[0] > 0:
                return True
            return await self.create_corrected_forecast()
        except Exception as error:
            _LOGGER.error("Bootstrap integration cache error: %s", error)
            return False
