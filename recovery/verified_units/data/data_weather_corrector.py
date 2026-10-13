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
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    RadiationSources,
    SNAPSHOT_KIND_ENSEMBLE_COMPOSE,
    align_provider_radiation_to_interval_start,
    apply_terminal_kalman_ghi,
    blend_static_with_mlp_residual,
    compose_ensemble_radiation,
    ensemble_compose_source_rows,
    open_meteo_base_source_identity,
    shrink_ensemble_solar_precision_factor,
    should_apply_coherence_cap,
    should_apply_kalman,
    should_apply_solar_precision_factor,
    snapshot_target_datetime,
)
from ..const import (
    CONF_SOLAR_RADIATION_SENSOR,
    DOMAIN,
    WINTER_MONTHS,
    WINTER_CLOUD_PENALTY_FACTOR,
    WINTER_LOW_SUN_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)
OVERCAST_FLOOR_MAX_RATIO = 0.15
OVERCAST_FLOOR_MIN_RATIO = 0.05
OVERCAST_CONSENSUS_CLOUD_THRESHOLD = 95.0


def _compute_dynamic_clear_sky_floor_ratio(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    sun_elevation: float,
    humidity: float,
    rain: float,
    blend_info: Optional[dict[str, Any]] = None,
    open_meteo_cloud_cover: Optional[float] = None,
) -> float:
    """Return a portable clear-sky floor ratio for dense-overcast regimes."""
    floor_ratio = OVERCAST_FLOOR_MAX_RATIO
    ghi_to_clear_sky_ratio = None
    if clear_sky > 50 and ghi >= 0:
        ghi_to_clear_sky_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_fraction = None
    if ghi > 5 and dhi >= 0:
        diffuse_fraction = min(max(dhi / ghi, 0.0), 1.5)
    direct_fraction = None
    if ghi > 5 and dni >= 0:
        direct_fraction = min(max(dni / ghi, 0.0), 1.5)
    overcast_expert_count = 0
    if (
        open_meteo_cloud_cover is not None
        and open_meteo_cloud_cover >= OVERCAST_CONSENSUS_CLOUD_THRESHOLD
    ):
        overcast_expert_count += 1
    if blend_info:
        for expert_cloud_percent in (blend_info.get("experts") or {}).values():
            if (
                isinstance(expert_cloud_percent, (int, float))
                and expert_cloud_percent >= OVERCAST_CONSENSUS_CLOUD_THRESHOLD
            ):
                overcast_expert_count += 1
    if clouds >= 85:
        floor_ratio = min(floor_ratio, 0.12)
    if clouds >= 92:
        floor_ratio = min(floor_ratio, 0.1)
    if overcast_expert_count >= 2:
        floor_ratio = min(floor_ratio, 0.1)
    if overcast_expert_count >= 3:
        floor_ratio = min(floor_ratio, 0.08)
    if overcast_expert_count >= 4:
        floor_ratio = min(floor_ratio, 0.06)
    if ghi_to_clear_sky_ratio is not None:
        if ghi_to_clear_sky_ratio <= 0.18:
            floor_ratio = min(floor_ratio, 0.06)
        elif ghi_to_clear_sky_ratio <= 0.24:
            floor_ratio = min(floor_ratio, 0.08)
        elif ghi_to_clear_sky_ratio <= 0.32:
            floor_ratio = min(floor_ratio, 0.1)
        elif ghi_to_clear_sky_ratio <= 0.4:
            floor_ratio = min(floor_ratio, 0.12)
    if diffuse_fraction is not None:
        if diffuse_fraction >= 0.82:
            floor_ratio = min(floor_ratio, 0.07)
        elif diffuse_fraction >= 0.74:
            floor_ratio = min(floor_ratio, 0.09)
    if direct_fraction is not None:
        if direct_fraction <= 0.18:
            floor_ratio = min(floor_ratio, 0.08)
        elif direct_fraction <= 0.28:
            floor_ratio = min(floor_ratio, 0.1)
    if rain >= 0.2:
        floor_ratio = min(floor_ratio, 0.09)
    if humidity >= 85:
        floor_ratio = min(floor_ratio, 0.09)
    if sun_elevation < 15:
        floor_ratio = max(floor_ratio, 0.07)
    return max(OVERCAST_FLOOR_MIN_RATIO, min(OVERCAST_FLOOR_MAX_RATIO, floor_ratio))


def calculate_dni_dhi_ratio(
    cloud_cover: float, sun_elevation: float = 45.0
) -> tuple[float, float]:
    "Calculate dynamic DNI/DHI ratio based on cloud cover and sun elevation. @zara\n\nAt low sun angles, diffuse radiation dominates due to longer atmospheric path.\n\nArgs:\n    cloud_cover: Cloud cover percentage (0-100)\n    sun_elevation: Sun elevation in degrees (0-90)\n\nReturns:\n    Tuple of (dni_fraction, dhi_fraction)\n"
    normalized_clouds = max(
        0.0,
        min(
            100.0,
            (
                cloud_cover
                if isinstance(cloud_cover, (int, float)) and math.isfinite(cloud_cover)
                else 0.0
            ),
        ),
    )
    normalized_sun_elevation = max(
        0.0,
        min(
            90.0,
            (
                sun_elevation
                if isinstance(sun_elevation, (int, float))
                and math.isfinite(sun_elevation)
                else 45.0
            ),
        ),
    )
    if normalized_sun_elevation < 10:
        clear_sky_dni_fraction = 0.45
        overcast_dni_fraction = 0.05
    elif normalized_sun_elevation < 20:
        clear_sky_dni_fraction = 0.6
        overcast_dni_fraction = 0.1
    elif normalized_sun_elevation < 30:
        clear_sky_dni_fraction = 0.72
        overcast_dni_fraction = 0.12
    else:
        clear_sky_dni_fraction = 0.85
        overcast_dni_fraction = 0.15
    dni_fraction = clear_sky_dni_fraction - (
        clear_sky_dni_fraction - overcast_dni_fraction
    ) * (normalized_clouds / 100.0)
    dhi_fraction = 1.0 - dni_fraction
    return (dni_fraction, dhi_fraction)


def calculate_effective_cloud_factor(
    cloud_cover: float,
    sun_elevation: float,
    month: int,
    winter_mode_enabled: bool = True,
) -> float:
    "Calculate effective cloud factor with winter and low-sun corrections. @zara\n\nAt low sun angles, clouds have disproportionately stronger effect\ndue to longer atmospheric path through clouds.\n\nArgs:\n    cloud_cover: Cloud cover percentage (0-100)\n    sun_elevation: Sun elevation in degrees (0-90)\n    month: Current month (1-12)\n    winter_mode_enabled: Whether winter cloud penalty is active (UI toggle)\n\nReturns:\n    Effective cloud factor (0.05 - 1.0)\n"
    normalized_clouds = max(
        0.0,
        min(
            100.0,
            (
                cloud_cover
                if isinstance(cloud_cover, (int, float)) and math.isfinite(cloud_cover)
                else 0.0
            ),
        ),
    )
    normalized_sun_elevation = max(
        0.0,
        min(
            90.0,
            (
                sun_elevation
                if isinstance(sun_elevation, (int, float))
                and math.isfinite(sun_elevation)
                else 0.0
            ),
        ),
    )
    if normalized_sun_elevation <= 0:
        return 0.05
    clear_fraction = (100.0 - normalized_clouds) / 100.0
    if normalized_clouds < 5.0:
        return min(1.0, clear_fraction)
    is_winter = winter_mode_enabled and month in WINTER_MONTHS
    winter_penalty = WINTER_CLOUD_PENALTY_FACTOR if is_winter else 1.0
    if normalized_sun_elevation < WINTER_LOW_SUN_THRESHOLD:
        cloud_exponent = (
            1.0
            + (WINTER_LOW_SUN_THRESHOLD - normalized_sun_elevation)
            / WINTER_LOW_SUN_THRESHOLD
            * winter_penalty
        )
        effective_cloud_factor = clear_fraction**cloud_exponent
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
    fog_confidence = 0.0
    fog_type = "none"
    if temperature is None:
        return (
            (((False, 0.0, "none") if True else None) if True else None)
            if True
            else None
        )
    if dewpoint is not None:
        dewpoint_spread = temperature - dewpoint
        if dewpoint_spread < 1.0:
            fog_confidence += 0.45
            fog_type = "radiation" if 5 <= hour <= 10 else "advection"
        elif dewpoint_spread < 2.0:
            fog_confidence += 0.3
            fog_type = "radiation" if 5 <= hour <= 10 else "advection"
        elif dewpoint_spread < 3.0:
            fog_confidence += 0.15
    if humidity is not None:
        if humidity >= 98:
            fog_confidence += 0.25
        elif humidity >= 95:
            fog_confidence += 0.18
        elif humidity >= 90:
            fog_confidence += 0.1
    if cloud_cover >= 95:
        fog_confidence += 0.12
        if fog_type == "none":
            fog_type = "stratus_fog"
    elif cloud_cover >= 85:
        fog_confidence += 0.08
    if 5 <= hour <= 10:
        fog_confidence += 0.08
    elif 4 <= hour <= 11:
        fog_confidence += 0.04
    if -2 <= temperature <= 5:
        fog_confidence += 0.05
    fog_detected = fog_confidence >= 0.45
    return (
        (
            (
                (
                    fog_detected,
                    min(1.0, fog_confidence),
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
    ):
        "Initialize the weather corrector. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for data persistence\n    open_meteo_client: OpenMeteoClient instance\n    expert_blender: WeatherExpertBlender instance\n    winter_mode: Whether winter cloud penalty is active (UI toggle)\n"
        super().__init__(hass, db_manager)
        self._open_meteo_client = open_meteo_client
        self._expert_blender = expert_blender
        self._winter_mode = winter_mode
        self._consecutive_failures = 0
        self._last_error = None
        self._weather_mlp = None
        self._kalman_tracker = None
        self._solar_clamp_min = 0.75
        self._correction_log_stats = {}
        self.data_dir = Path(hass.config.path("solar_forecast_ml"))
        self.stats_dir = self.data_dir / "stats"
        self.integration_cache_file = self.stats_dir / "weather_integration_ml.json"
        self.integration_history_dir = self.stats_dir / "integration_cache_history"
        _LOGGER.debug("WeatherForecastCorrector initialized")

    def set_weather_mlp(self, mlp) -> None:
        """Set Weather MLP for GHI correction. @zara"""
        previous_mlp = self._weather_mlp
        self._weather_mlp = mlp
        if mlp is not None and mlp is not previous_mlp:
            _LOGGER.info("WeatherForecastCorrector: Weather MLP attached")
        elif mlp is None and previous_mlp is not None:
            _LOGGER.info("WeatherForecastCorrector: Weather MLP detached")
        return None if True else None

    def set_kalman_tracker(self, tracker) -> None:
        """Set Kalman tracker for intraday bias correction. @zara"""
        self._kalman_tracker = tracker
        _LOGGER.debug("WeatherForecastCorrector: Kalman tracker attached")

    def set_solar_clamp_min(self, value: float) -> None:
        """Set adaptive solar clamp minimum. @zara V17.2.0"""
        self._solar_clamp_min = max(0.65, min(0.75, value))

    def _has_available_local_solar_observation(self) -> bool:
        """Return whether a configured physical radiation sensor is usable."""
        try:
            config_entries = getattr(self.hass, "config_entries", None)
            get_entries = getattr(config_entries, "async_entries", None)
            states = getattr(self.hass, "states", None)
            get_state = getattr(states, "get", None)
            if not (callable(get_entries) and callable(get_state)):
                return False
            for entry in get_entries(DOMAIN):
                entry_data = getattr(entry, "data", {}) or {}
                radiation_entity_id = entry_data.get(CONF_SOLAR_RADIATION_SENSOR)
                if not isinstance(radiation_entity_id, str) or not radiation_entity_id:
                    continue
                radiation_state = get_state(radiation_entity_id)
                state_value = getattr(radiation_state, "state", None)
                if state_value is None or str(state_value).strip().lower() in {
                    "",
                    "none",
                    "unknown",
                    "unavailable",
                }:
                    continue
                radiation_value = float(state_value)
                if math.isfinite(radiation_value):
                    if radiation_value >= 0.0:
                        return True
        except (TypeError, ValueError, AttributeError):
            return False
        try:
            pass
        except (TypeError, ValueError, AttributeError):
            pass
        return False

    def _can_use_weather_mlp(self) -> bool:
        """Gate MLP inference on a live measured-GHI capability check."""
        return bool(
            self._has_available_local_solar_observation()
            and self._weather_mlp is not None
            and getattr(self._weather_mlp, "is_usable", False)
        )

    def _reset_correction_log_stats(self) -> None:
        self._correction_log_stats = {
            "open_meteo_ghi": 0,
            "ensemble_ghi": 0,
            "icon_d2_ghi": 0,
            "mlp_available": 0,
            "mlp_corrected": 0,
            "clear_sky_fallback": 0,
            "hourly_solar_factor": 0,
            "solar_factor_skipped": 0,
            "ensemble_solar_calibration": 0,
            "coherence_cap": 0,
            "coherence_cap_skipped": 0,
            "kalman_today": 0,
            "cloud_residual": 0,
        }

    def _record_correction_stat(self, key: str) -> None:
        if not self._correction_log_stats:
            return
        self._correction_log_stats[key] = self._correction_log_stats.get(key, 0) + 1

    def _log_correction_summary(self, hours_processed: int) -> None:
        correction_stats = self._correction_log_stats
        if not correction_stats:
            return
        _LOGGER.info(
            "Weather correction summary: hours=%d, open_meteo_ghi=%d, ensemble_ghi=%d, icon_d2=%d, mlp_available=%d, mlp_corrected=%d, clear_sky_fallback=%d, hourly_solar_factors=%d, solar_factor_skipped=%d, ensemble_solar_calibration=%d, coherence_caps=%d, coherence_cap_skipped=%d, kalman_today=%d, cloud_residual=%d",
            hours_processed,
            correction_stats.get("open_meteo_ghi", 0),
            correction_stats.get("ensemble_ghi", 0),
            correction_stats.get("icon_d2_ghi", 0),
            correction_stats.get("mlp_available", 0),
            correction_stats.get("mlp_corrected", 0),
            correction_stats.get("clear_sky_fallback", 0),
            correction_stats.get("hourly_solar_factor", 0),
            correction_stats.get("solar_factor_skipped", 0),
            correction_stats.get("ensemble_solar_calibration", 0),
            correction_stats.get("coherence_cap", 0),
            correction_stats.get("coherence_cap_skipped", 0),
            correction_stats.get("kalman_today", 0),
            correction_stats.get("cloud_residual", 0),
        )

    async def async_init(self) -> bool:
        """Initialize and load data. @zara"""
        await self.ensure_initialized()
        return True

    async def create_corrected_forecast(self, min_confidence: float = 0.0) -> bool:
        "Create corrected weather forecast with precision factors applied. @zara\n\nV16.1: Now uses hourly factors and weather-specific factors.\n\nArgs:\n    min_confidence: Minimum confidence threshold\n\nReturns:\n    True if forecast created successfully\n"
        try:
            self._reset_correction_log_stats()
            correction_bundle = await self._load_correction_factors()
            factors = correction_bundle["factors"]
            hourly_factors = correction_bundle.get("hourly_factors", {})
            weather_factors = correction_bundle.get("weather_factors", {})
            solar_calibration = correction_bundle.get("solar_calibration", {})
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
            hours_processed = 0
            for forecast_date, forecasts_by_hour in raw_forecasts.items():
                for hour_key, raw_hour in forecasts_by_hour.items():
                    forecast_hour = int(hour_key)
                    weather_hour = await self._get_weather_for_hour_async(
                        forecast_date, forecast_hour, raw_hour
                    )
                    weather_type = self._classify_weather_type_for_hour(weather_hour)
                    corrected_weather = self._apply_corrections(
                        weather_hour,
                        factors,
                        hourly_factors,
                        weather_factors,
                        weather_type,
                        forecast_hour,
                        solar_calibration,
                    )
                    corrected_weather = self._apply_terminal_kalman(
                        corrected_weather, forecast_date, forecast_hour, dt_util.now()
                    )
                    try:
                        correction_snapshot_id = (
                            await self.db.insert_weather_forecast_correction_snapshot(
                                target_datetime=snapshot_target_datetime(
                                    forecast_date, forecast_hour, dt_util.now()
                                ),
                                target_date=forecast_date,
                                target_hour=forecast_hour,
                                data=corrected_weather,
                                weather_type=weather_type,
                            )
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Corrected-weather provenance persist failed for %s hour %s",
                            forecast_date,
                            forecast_hour,
                        )
                        continue
                    corrected_weather["_sfml_correction_snapshot_id"] = (
                        correction_snapshot_id
                    )
                    forecast_saved = await self._save_corrected_forecast(
                        forecast_date, forecast_hour, corrected_weather
                    )
                    if forecast_saved:
                        hours_processed += 1
                continue
            if hours_processed == 0:
                _LOGGER.error("No forecast hours processed")
                return False
            _LOGGER.info(
                "Created corrected forecast: %d hours written to weather_forecast",
                hours_processed,
            )
            self._log_correction_summary(hours_processed)
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
            today = dt_util.now().date().isoformat()
            try:
                cached_rows = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              ghi_icon_d2, direct_radiation_icon_d2, diffuse_radiation_icon_d2,\n                              ghi_icon_eu, direct_radiation_icon_eu, diffuse_radiation_icon_eu,\n                              source, ghi_source_identity, visibility_source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (today,),
                )
            except Exception:
                cached_rows = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (today,),
                )
            raw_rows = []
            for database_row in cached_rows:
                raw_rows.append(
                    {
                        "date": str(database_row[0]),
                        "hour": int(database_row[1]),
                        "temperature": database_row[2],
                        "humidity": database_row[3],
                        "cloud_cover": database_row[4],
                        "precipitation": database_row[5],
                        "wind_speed": database_row[6],
                        "pressure": database_row[7],
                        "direct_radiation": database_row[8],
                        "diffuse_radiation": database_row[9],
                        "ghi": database_row[10],
                        "cloud_cover_low": database_row[11],
                        "cloud_cover_mid": database_row[12],
                        "cloud_cover_high": database_row[13],
                        "visibility_m": database_row[14],
                        "weather_code": database_row[15],
                        "snowfall": database_row[16],
                        "rain": database_row[17],
                        "ghi_icon_d2": (
                            database_row[18] if len(database_row) > 18 else None
                        ),
                        "direct_radiation_icon_d2": (
                            database_row[19] if len(database_row) > 19 else None
                        ),
                        "diffuse_radiation_icon_d2": (
                            database_row[20] if len(database_row) > 20 else None
                        ),
                        "ghi_icon_eu": (
                            database_row[21] if len(database_row) > 21 else None
                        ),
                        "direct_radiation_icon_eu": (
                            database_row[22] if len(database_row) > 22 else None
                        ),
                        "diffuse_radiation_icon_eu": (
                            database_row[23] if len(database_row) > 23 else None
                        ),
                        "source": database_row[24] if len(database_row) > 24 else None,
                        "ghi_source_identity": (
                            database_row[25]
                            if len(database_row) > 25 and database_row[25]
                            else open_meteo_base_source_identity(
                                database_row[24] if len(database_row) > 24 else None
                            )
                        ),
                        "visibility_source": (
                            database_row[26] if len(database_row) > 26 else None
                        ),
                    }
                )
                continue
            forecasts_by_date = {}
            for aligned_record in align_provider_radiation_to_interval_start(raw_rows):
                forecast_date = str(aligned_record.pop("date"))
                raw_hour_key = str(aligned_record.pop("hour"))
                forecasts_by_date.setdefault(forecast_date, {})[
                    raw_hour_key
                ] = aligned_record
            return forecasts_by_date
        except Exception as error:
            _LOGGER.error("Error loading raw forecasts: %s", error)
            return {}

    @staticmethod
    def _classify_weather_type_for_hour(weather: dict[str, Any]) -> str:
        """Classify weather type per hour to avoid day-level front smearing. @zara"""
        normalized_clouds = weather.get("clouds")
        if normalized_clouds is None:
            return "mixed"
        if normalized_clouds <= 30:
            return "clear"
        if normalized_clouds >= 50:
            return "cloudy"
        return "mixed"

    @staticmethod
    def _scale_mlp_confidence_for_weather(
        *,
        base_confidence: float,
        static_factor: float,
        mlp_factor: float,
        solar_radiation: Optional[float],
        clear_sky_radiation: Optional[float],
        clouds: Optional[float],
        rain: Optional[float],
    ) -> float:
        """Scale MLP confidence by meteorological support for the correction direction. @zara"""
        bounded_mlp_confidence = max(0.0, min(0.8, base_confidence))
        if bounded_mlp_confidence <= 0:
            return 0.0
        if mlp_factor >= static_factor:
            return bounded_mlp_confidence * 0.45
        weather_support = 0.05
        solar_clear_sky_ratio = None
        if (
            isinstance(solar_radiation, (int, float))
            and isinstance(clear_sky_radiation, (int, float))
            and math.isfinite(float(solar_radiation))
            and math.isfinite(float(clear_sky_radiation))
            and (clear_sky_radiation > 50)
        ):
            solar_clear_sky_ratio = max(
                0.0, min(1.5, float(solar_radiation) / float(clear_sky_radiation))
            )
        if solar_clear_sky_ratio is None:
            if isinstance(clouds, (int, float)) and clouds >= 85:
                weather_support += 0.2
        else:
            if solar_clear_sky_ratio <= 0.25:
                weather_support += 0.45
            elif solar_clear_sky_ratio <= 0.4:
                weather_support += 0.35
            elif solar_clear_sky_ratio <= 0.55:
                weather_support += 0.25
            elif solar_clear_sky_ratio <= 0.7:
                weather_support += 0.15
            elif solar_clear_sky_ratio <= 0.85:
                weather_support += 0.05
            if solar_clear_sky_ratio <= 0.75 and isinstance(clouds, (int, float)):
                if clouds >= 90:
                    weather_support += 0.2
                elif clouds >= 75:
                    weather_support += 0.12
                elif clouds >= 60:
                    weather_support += 0.07
        if isinstance(rain, (int, float)):
            if rain >= 0.5:
                weather_support += 0.18
            elif rain >= 0.1:
                weather_support += 0.1
        return bounded_mlp_confidence * max(0.08, min(1.0, weather_support))

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
            database_row = await self.fetch_one(
                "SELECT temperature_factor, solar_factor, cloud_factor,\n                          wind_factor, humidity_factor, rain_factor,\n                          pressure_factor, sample_days, confidence\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            factors = default_factors.copy()
            sample_days = 0
            calibration_confidence = 0.0
            if database_row:
                factors = {
                    "temperature": database_row[0] or 0.0,
                    "solar_radiation_wm2": database_row[1] or 1.0,
                    "clouds": database_row[2] or 1.0,
                    "wind": database_row[3] or 1.0,
                    "humidity": database_row[4] or 1.0,
                    "rain": database_row[5] or 1.0,
                    "pressure": database_row[6] or 0.0,
                }
                sample_days = database_row[7] or 0
                calibration_confidence = database_row[8] or 0.0
            hourly_factors = {"solar_radiation_wm2": {}, "clouds": {}}
            hourly_factor_rows = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            for hourly_factor_row in hourly_factor_rows:
                (
                    factor_hour,
                    factor_type,
                    factor_value,
                    sample_count,
                    factor_confidence,
                ) = hourly_factor_row
                if factor_type in hourly_factors:
                    hourly_factors[factor_type][str(factor_hour)] = {
                        "factor": factor_value,
                        "samples": sample_count,
                        "confidence": factor_confidence or 0.0,
                    }
            weather_factors = {"clear": {}, "cloudy": {}}
            weather_factor_rows = await self.fetch_all(
                "SELECT weather_type, factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific"
            )
            for weather_factor_row in weather_factor_rows:
                weather_type, factor_type, factor_value, weather_confidence = (
                    weather_factor_row
                )
                if (
                    weather_type in weather_factors
                    and weather_confidence
                    and (weather_confidence >= 0.3)
                ):
                    weather_factors[weather_type][factor_type] = factor_value
            return {
                "factors": factors,
                "hourly_factors": hourly_factors,
                "weather_factors": weather_factors,
                "sample_days": sample_days,
                "solar_calibration": {
                    "local_observation_available": self._has_available_local_solar_observation(),
                    "sample_days": sample_days,
                    "confidence": calibration_confidence,
                },
            }
        except Exception as error:
            _LOGGER.debug("Using default correction factors: %s", error)
            return {
                "factors": default_factors,
                "hourly_factors": {"solar_radiation_wm2": {}, "clouds": {}},
                "weather_factors": {"clear": {}, "cloudy": {}},
                "sample_days": 0,
                "solar_calibration": {
                    "local_observation_available": False,
                    "sample_days": 0,
                    "confidence": 0.0,
                },
            }

    async def _get_weather_for_hour_async(
        self, date_str: str, hour: int, raw_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Get weather for hour with blended cloud cover. @zara"""
        blended_clouds = raw_data.get("cloud_cover") or 0.0
        weather_source = "raw"
        blend_info = None
        if self._expert_blender:
            try:
                blended_clouds, blend_info = (
                    await self._expert_blender.get_blended_cloud_cover(
                        date=date_str,
                        hour=hour,
                        cloud_low=raw_data.get("cloud_cover_low"),
                        cloud_mid=raw_data.get("cloud_cover_mid"),
                        cloud_high=raw_data.get("cloud_cover_high"),
                        visibility_m=raw_data.get("visibility_m"),
                        fallback_cloud=raw_data.get("cloud_cover"),
                    )
                )
                if blend_info and blend_info.get("fallback"):
                    weather_source = "raw_fallback"
                else:
                    weather_source = "expert_blend"
            except Exception as error:
                _LOGGER.debug("Expert blending failed: %s", error)
        sun_elevation = await self._get_sun_elevation(date_str, hour)
        try:
            forecast_month = int(date_str.split("-")[1])
        except (IndexError, ValueError):
            forecast_month = 6
        clear_sky_poa_wm2 = await self._get_weighted_poa_radiation(date_str, hour)
        effective_cloud_factor = calculate_effective_cloud_factor(
            cloud_cover=blended_clouds,
            sun_elevation=sun_elevation,
            month=forecast_month,
            winter_mode_enabled=self._winter_mode,
        )
        raw_ghi = raw_data.get("ghi")
        mlp_factor = None
        mlp_confidence = 0.0
        issued_at = dt_util.now()
        bright_sky_ghi = None
        if self._expert_blender is not None:
            try:
                bright_sky_ghi = await self._expert_blender.get_bright_sky_solar_wm2(
                    date_str, hour
                )
            except Exception:
                bright_sky_ghi = None
        radiation_sources = RadiationSources(
            ghi_ifs=raw_ghi,
            dni_ifs=raw_data.get("direct_radiation"),
            dhi_ifs=raw_data.get("diffuse_radiation"),
            ghi_icon_d2=raw_data.get("ghi_icon_d2"),
            dni_icon_d2=raw_data.get("direct_radiation_icon_d2"),
            dhi_icon_d2=raw_data.get("diffuse_radiation_icon_d2"),
            ghi_icon_eu=raw_data.get("ghi_icon_eu"),
            dni_icon_eu=raw_data.get("direct_radiation_icon_eu"),
            dhi_icon_eu=raw_data.get("diffuse_radiation_icon_eu"),
            ghi_bright_sky=bright_sky_ghi,
            ifs_cloud=raw_data.get("cloud_cover"),
            blended_cloud=blended_clouds,
            ifs_source_identity=raw_data.get("ghi_source_identity"),
        )
        ensemble_result = compose_ensemble_radiation(
            radiation_sources,
            forecast_date=date_str,
            hour=hour,
            now=issued_at,
            sun_elevation=sun_elevation,
        )
        radiation_snapshot_id = None
        try:
            radiation_snapshot_id = await self.db.insert_weather_radiation_snapshot(
                snapshot_kind=SNAPSHOT_KIND_ENSEMBLE_COMPOSE,
                retrieved_at=issued_at.isoformat(),
                provider_run_at=None,
                target_datetime=snapshot_target_datetime(date_str, hour, issued_at),
                target_date=date_str,
                target_hour=hour,
                lead_hours=ensemble_result.lead_hours,
                raw_blend_ghi=ensemble_result.raw_blend_ghi,
                residual_scale=ensemble_result.cloud_residual_scale,
                kalman_applied=ensemble_result.kalman_applied,
                kalman_bias=ensemble_result.kalman_bias,
                final_ghi=ensemble_result.ghi,
                final_dni=ensemble_result.dni,
                final_dhi=ensemble_result.dhi,
                sources=ensemble_compose_source_rows(
                    radiation_sources, ensemble_result
                ),
            )
        except Exception:
            _LOGGER.warning(
                "Radiation ensemble snapshot persist failed for %s hour %s",
                date_str,
                hour,
            )
            raise RuntimeError(
                f"radiation provenance unavailable for {date_str} hour {hour}"
            )
        ghi_from_ensemble = (
            bool(ensemble_result.used_sources) and ensemble_result.ghi > 0
        )
        if ghi_from_ensemble:
            ghi_wm2 = ensemble_result.ghi
            self._record_correction_stat("open_meteo_ghi")
            self._record_correction_stat("ensemble_ghi")
            if ensemble_result.icon_d2_available:
                self._record_correction_stat("icon_d2_ghi")
            if ensemble_result.cloud_residual_scale != 1.0:
                self._record_correction_stat("cloud_residual")
            if self._can_use_weather_mlp():
                mlp_factor = self._weather_mlp.predict_correction(
                    raw_ghi=ghi_wm2,
                    cloud_cover=blended_clouds,
                    sun_elevation=sun_elevation,
                    humidity=raw_data.get("humidity"),
                    pressure=raw_data.get("pressure"),
                    wind=raw_data.get("wind_speed"),
                    hour=hour,
                    rain=raw_data.get("precipitation") or raw_data.get("rain"),
                    cloud_low=raw_data.get("cloud_cover_low"),
                    cloud_mid=raw_data.get("cloud_cover_mid"),
                    cloud_high=raw_data.get("cloud_cover_high"),
                )
                if mlp_factor is None:
                    mlp_confidence = 0.0
                else:
                    mlp_factor = max(0.5, min(1.5, mlp_factor))
                    mlp_confidence = max(
                        0.0,
                        min(
                            0.8,
                            getattr(self._weather_mlp, "application_confidence", 0.0),
                        ),
                    )
                    if mlp_confidence > 0:
                        self._record_correction_stat("mlp_available")
            dni_wm2 = ensemble_result.dni
            dhi_wm2 = ensemble_result.dhi
        else:
            if sun_elevation > 0:
                ghi_wm2 = clear_sky_poa_wm2 * effective_cloud_factor
                self._record_correction_stat("clear_sky_fallback")
            else:
                ghi_wm2 = 0.0
            dni_fraction, dhi_fraction = calculate_dni_dhi_ratio(
                blended_clouds, sun_elevation
            )
            dni_wm2 = ghi_wm2 * dni_fraction
            dhi_wm2 = ghi_wm2 * dhi_fraction
        visibility_m = raw_data.get("visibility_m")
        fog_detected = False
        fog_type = None
        if visibility_m is not None and visibility_m < 1000:
            fog_detected = True
            fog_type = "dense" if visibility_m < 200 else "light"
        elif visibility_m is None:
            temperature_c = raw_data.get("temperature")
            humidity_percent = raw_data.get("humidity")
            inferred_dewpoint_c = None
            if (
                temperature_c is not None
                and humidity_percent is not None
                and (humidity_percent > 50)
            ):
                inferred_dewpoint_c = temperature_c - (100 - humidity_percent) / 5.0
            fog_detected, fog_confidence, fog_type = detect_fog_from_meteorology(
                temperature_c,
                inferred_dewpoint_c,
                humidity_percent,
                blended_clouds,
                hour,
            )
        return (
            (
                {
                    "temperature": raw_data.get("temperature"),
                    "solar_radiation_wm2": round(ghi_wm2, 1),
                    "wind": raw_data.get("wind_speed"),
                    "humidity": raw_data.get("humidity"),
                    "rain": raw_data.get("precipitation") or raw_data.get("rain") or 0,
                    "clouds": blended_clouds,
                    "weather_code": raw_data.get("weather_code"),
                    "snowfall": raw_data.get("snowfall"),
                    "cloud_cover_low": raw_data.get("cloud_cover_low"),
                    "cloud_cover_mid": raw_data.get("cloud_cover_mid"),
                    "cloud_cover_high": raw_data.get("cloud_cover_high"),
                    "pressure": raw_data.get("pressure"),
                    "direct_radiation": round(dni_wm2, 1),
                    "diffuse_radiation": round(dhi_wm2, 1),
                    "clear_sky_poa_radiation": round(clear_sky_poa_wm2, 1),
                    "sun_elevation": round(sun_elevation, 1),
                    "cloud_factor_effective": round(effective_cloud_factor, 3),
                    "visibility_m": visibility_m,
                    "visibility_source": raw_data.get("visibility_source"),
                    "fog_detected": fog_detected,
                    "fog_type": fog_type,
                    "source": weather_source,
                    "confidence": 0.95 if weather_source == "expert_blend" else 0.85,
                    "blend_info": blend_info,
                    "open_meteo_cloud_cover": raw_data.get("cloud_cover"),
                    "mlp_correction_factor": mlp_factor,
                    "mlp_confidence": mlp_confidence,
                    "ghi_from_ensemble": ghi_from_ensemble,
                    "ensemble_used_sources": (
                        ensemble_result.used_sources if ghi_from_ensemble else ()
                    ),
                    "_sfml_radiation_snapshot_id": radiation_snapshot_id,
                    "_sfml_raw_blend_ghi": ensemble_result.raw_blend_ghi,
                    "_sfml_ensemble_ghi": (
                        ensemble_result.ghi if ghi_from_ensemble else ghi_wm2
                    ),
                    "_sfml_ensemble_dni": (
                        ensemble_result.dni if ghi_from_ensemble else dni_wm2
                    ),
                    "_sfml_ensemble_dhi": (
                        ensemble_result.dhi if ghi_from_ensemble else dhi_wm2
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )

    async def _get_sun_elevation(self, date_str: str, hour: int) -> float:
        """Get sun elevation from astronomy cache. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT sun_elevation_deg FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = ?",
                (date_str, hour),
            )
            if database_row and database_row[0] is not None:
                return float(database_row[0])
        except Exception:
            pass
        return 45.0

    async def _get_weighted_poa_radiation(self, date_str: str, hour: int) -> float:
        """Get capacity-weighted POA radiation from per-panel-group data. @zara

        V16.1: Uses astronomy_cache_panel_groups for accurate tilted irradiance
        weighted by panel capacity. Falls back to horizontal GHI if no group data.
        """
        try:
            panel_radiation_rows = await self.fetch_all(
                "SELECT power_kwp, poa_wm2 FROM astronomy_cache_panel_groups\n                   WHERE cache_date = ? AND hour = ?",
                (date_str, hour),
            )
            if panel_radiation_rows:
                total_capacity_kwp = 0.0
                weighted_radiation_sum = 0.0
                for panel_capacity_kwp, panel_poa_wm2 in panel_radiation_rows:
                    if panel_capacity_kwp:
                        if panel_poa_wm2 is not None:
                            total_capacity_kwp += panel_capacity_kwp
                            weighted_radiation_sum += panel_capacity_kwp * panel_poa_wm2
                if total_capacity_kwp > 0:
                    return weighted_radiation_sum / total_capacity_kwp
            database_row = await self.fetch_one(
                "SELECT clear_sky_radiation_wm2 FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = ?",
                (date_str, hour),
            )
            if database_row and database_row[0] is not None:
                return float(database_row[0])
        except Exception:
            pass
        return 0.0

    def _apply_corrections(
        self,
        weather: dict[str, Any],
        factors: dict[str, float],
        hourly_factors: dict[str, dict[str, Any]] = None,
        weather_factors: dict[str, dict[str, float]] = None,
        weather_type: str = "mixed",
        hour: int = 12,
        solar_calibration: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Apply correction factors including hourly + weather-specific. @zara

        V16.1: Uses hourly factors for solar/clouds (hours 7-17) and
        weather-specific factors for clear/cloudy days.
        """
        corrected_weather = weather.copy()
        hourly_factors = hourly_factors or {}
        weather_factors = weather_factors or {}
        solar_calibration = solar_calibration or {}
        for weather_key in (
            "temperature",
            "humidity",
            "clouds",
            "rain",
            "wind",
            "pressure",
        ):
            corrected_weather[f"_sfml_input_{weather_key}"] = weather.get(weather_key)
        local_observation_available = bool(
            solar_calibration.get("local_observation_available")
        )
        corrected_weather["_sfml_local_observation_available"] = (
            local_observation_available
        )
        corrected_weather["_sfml_observation_mode"] = (
            "measured_local" if local_observation_available else "provider_only"
        )
        corrected_weather["_sfml_learned_solar_factor"] = factors.get(
            "solar_radiation_wm2", 1.0
        )
        corrected_weather["_sfml_static_solar_factor_applied"] = 1.0
        corrected_weather["_sfml_mlp_confidence_applied"] = 0.0
        corrected_weather["_sfml_combined_solar_factor_applied"] = 1.0
        corrected_weather["_sfml_coherence_cap_applied"] = False
        corrected_weather["_sfml_coherence_cap_scale"] = 1.0
        corrected_weather["_sfml_kalman_applied"] = False
        corrected_weather["_sfml_kalman_bias"] = 0.0
        if corrected_weather.get("temperature") is not None:
            temperature_offset = factors.get("temperature", 0.0)
            if weather_type in weather_factors:
                temperature_offset = weather_factors[weather_type].get(
                    "temperature", temperature_offset
                )
            corrected_weather["temperature"] = round(
                corrected_weather["temperature"] + temperature_offset, 1
            )
        ensemble_sources = tuple(corrected_weather.get("ensemble_used_sources") or ())
        ghi_from_ensemble = bool(corrected_weather.get("ghi_from_ensemble"))
        legacy_solar_precision_eligible = should_apply_solar_precision_factor(
            ensemble_sources,
            ghi_from_ensemble=ghi_from_ensemble,
            local_observation_available=local_observation_available,
        )
        solar_precision_eligible = should_apply_solar_precision_factor(
            ensemble_sources,
            ghi_from_ensemble=ghi_from_ensemble,
            local_observation_available=local_observation_available,
            sample_days=solar_calibration.get("sample_days", 0),
            confidence=solar_calibration.get("confidence", 0.0),
        )
        coherence_cap_eligible = should_apply_coherence_cap(
            ensemble_sources, ghi_from_ensemble=ghi_from_ensemble
        )
        corrected_weather["_sfml_ghi_coherence_cap_eligible"] = int(
            bool(coherence_cap_eligible)
        )
        if corrected_weather.get("solar_radiation_wm2") is not None:
            if solar_precision_eligible:
                solar_factor = factors.get("solar_radiation_wm2", 1.0)
                hourly_solar_factor_used = False
                if legacy_solar_precision_eligible:
                    if 7 <= hour <= 17:
                        hourly_solar_factors = hourly_factors.get(
                            "solar_radiation_wm2", {}
                        )
                        hourly_factor_entry = hourly_solar_factors.get(str(hour))
                        if hourly_factor_entry and "factor" in hourly_factor_entry:
                            solar_factor = hourly_factor_entry["factor"]
                            hourly_solar_factor_used = True
                            self._record_correction_stat("hourly_solar_factor")
                if (
                    legacy_solar_precision_eligible
                    and (not hourly_solar_factor_used)
                    and (weather_type in weather_factors)
                ):
                    weather_solar_factor = weather_factors[weather_type].get(
                        "solar_radiation_wm2"
                    )
                    if weather_solar_factor is not None:
                        solar_factor = weather_solar_factor
                try:
                    solar_factor = float(solar_factor)
                except (TypeError, ValueError):
                    solar_factor = 1.0
                if not math.isfinite(solar_factor):
                    solar_factor = 1.0
                solar_factor = max(self._solar_clamp_min, min(1.3, solar_factor))
                if not legacy_solar_precision_eligible:
                    solar_factor = shrink_ensemble_solar_precision_factor(
                        solar_factor,
                        sample_days=solar_calibration.get("sample_days", 0),
                        confidence=solar_calibration.get("confidence", 0.0),
                    )
                    self._record_correction_stat("ensemble_solar_calibration")
                corrected_weather["_sfml_static_solar_factor_applied"] = solar_factor
                mlp_factor = corrected_weather.get("mlp_correction_factor")
                mlp_confidence = corrected_weather.get("mlp_confidence") or 0.0
                if (
                    local_observation_available
                    and isinstance(mlp_factor, (int, float))
                    and math.isfinite(float(mlp_factor))
                    and isinstance(mlp_confidence, (int, float))
                    and (float(mlp_confidence) > 0)
                ):
                    bounded_mlp_factor = max(0.5, min(1.5, float(mlp_factor)))
                    mlp_confidence = max(0.0, min(0.8, float(mlp_confidence)))
                    mlp_confidence = self._scale_mlp_confidence_for_weather(
                        base_confidence=mlp_confidence,
                        static_factor=solar_factor,
                        mlp_factor=solar_factor * bounded_mlp_factor,
                        solar_radiation=corrected_weather.get("solar_radiation_wm2"),
                        clear_sky_radiation=corrected_weather.get(
                            "clear_sky_poa_radiation"
                        ),
                        clouds=corrected_weather.get("clouds"),
                        rain=corrected_weather.get("rain"),
                    )
                    solar_factor = blend_static_with_mlp_residual(
                        solar_factor, bounded_mlp_factor, mlp_confidence
                    )
                    solar_factor = max(0.5, min(1.3, solar_factor))
                    corrected_weather["_sfml_mlp_confidence_applied"] = mlp_confidence
                    self._record_correction_stat("mlp_corrected")
                corrected_weather["_sfml_combined_solar_factor_applied"] = solar_factor
                corrected_weather["solar_radiation_wm2"] = round(
                    max(0.0, corrected_weather["solar_radiation_wm2"] * solar_factor), 1
                )
                if corrected_weather.get("direct_radiation") is not None:
                    corrected_weather["direct_radiation"] = round(
                        max(0.0, corrected_weather["direct_radiation"] * solar_factor),
                        1,
                    )
                if corrected_weather.get("diffuse_radiation") is not None:
                    corrected_weather["diffuse_radiation"] = round(
                        max(0.0, corrected_weather["diffuse_radiation"] * solar_factor),
                        1,
                    )
            else:
                self._record_correction_stat("solar_factor_skipped")
        cloud_correction_factor = factors.get("clouds", 1.0)
        if weather_type in weather_factors:
            weather_cloud_factor = weather_factors[weather_type].get("clouds")
            if weather_cloud_factor is not None:
                cloud_correction_factor = weather_cloud_factor
        if 7 <= hour <= 17:
            hourly_cloud_factors = hourly_factors.get("clouds", {})
            hourly_factor_entry = hourly_cloud_factors.get(str(hour))
            if hourly_factor_entry and "factor" in hourly_factor_entry:
                cloud_correction_factor = hourly_factor_entry["factor"]
        if corrected_weather.get("clouds") is not None:
            if corrected_weather.get("source") == "expert_blend":
                cloud_correction_factor = max(1.0, cloud_correction_factor)
            cloud_correction_factor = max(0.4, min(2.5, cloud_correction_factor))
            corrected_weather["clouds"] = round(
                max(
                    0.0,
                    min(100.0, corrected_weather["clouds"] * cloud_correction_factor),
                ),
                1,
            )
        clear_sky_poa_wm2 = corrected_weather.get("clear_sky_poa_radiation")
        normalized_clouds = corrected_weather.get("clouds")
        corrected_ghi_wm2 = corrected_weather.get("solar_radiation_wm2")
        cap_inputs_available = (
            isinstance(clear_sky_poa_wm2, (int, float))
            and isinstance(normalized_clouds, (int, float))
            and isinstance(corrected_ghi_wm2, (int, float))
            and (clear_sky_poa_wm2 > 0)
            and (corrected_ghi_wm2 > 0)
            and (normalized_clouds > 20)
        )
        if cap_inputs_available and coherence_cap_eligible:
            cloud_fraction = max(0.0, min(1.0, normalized_clouds / 100.0))
            coherence_ceiling_wm2 = clear_sky_poa_wm2 * math.exp(-2.0 * cloud_fraction)
            dynamic_floor_ratio = _compute_dynamic_clear_sky_floor_ratio(
                clouds=float(normalized_clouds),
                ghi=float(corrected_ghi_wm2),
                dni=float(corrected_weather.get("direct_radiation") or 0.0),
                dhi=float(corrected_weather.get("diffuse_radiation") or 0.0),
                clear_sky=float(clear_sky_poa_wm2),
                sun_elevation=float(corrected_weather.get("sun_elevation") or 0.0),
                humidity=float(corrected_weather.get("humidity") or 0.0),
                rain=float(corrected_weather.get("rain") or 0.0),
                blend_info=corrected_weather.get("blend_info"),
                open_meteo_cloud_cover=corrected_weather.get("open_meteo_cloud_cover"),
            )
            coherence_ceiling_wm2 = max(
                coherence_ceiling_wm2, clear_sky_poa_wm2 * dynamic_floor_ratio
            )
            corrected_weather["_sfml_ghi_cap_ceiling"] = round(coherence_ceiling_wm2, 1)
            if corrected_ghi_wm2 > coherence_ceiling_wm2 * 1.15:
                capped_ghi_wm2 = round(coherence_ceiling_wm2, 1)
                radiation_scale = (
                    capped_ghi_wm2 / corrected_ghi_wm2 if corrected_ghi_wm2 > 0 else 1.0
                )
                corrected_weather["solar_radiation_wm2"] = capped_ghi_wm2
                if corrected_weather.get("direct_radiation") is not None:
                    corrected_weather["direct_radiation"] = round(
                        max(
                            0.0, corrected_weather["direct_radiation"] * radiation_scale
                        ),
                        1,
                    )
                if corrected_weather.get("diffuse_radiation") is not None:
                    corrected_weather["diffuse_radiation"] = round(
                        max(
                            0.0,
                            corrected_weather["diffuse_radiation"] * radiation_scale,
                        ),
                        1,
                    )
                corrected_weather["_sfml_coherence_cap_applied"] = True
                corrected_weather["_sfml_coherence_cap_scale"] = radiation_scale
                self._record_correction_stat("coherence_cap")
        elif cap_inputs_available:
            self._record_correction_stat("coherence_cap_skipped")
        for weather_key, factor_key in (
            ("wind", "wind"),
            ("humidity", "humidity"),
            ("rain", "rain"),
        ):
            if corrected_weather.get(weather_key) is not None:
                correction_factor = factors.get(factor_key, 1.0)
                if weather_type in weather_factors:
                    weather_specific_factor = weather_factors[weather_type].get(
                        factor_key
                    )
                    if weather_specific_factor is not None:
                        correction_factor = weather_specific_factor
                corrected_weather[weather_key] = round(
                    corrected_weather[weather_key] * correction_factor, 1
                )
                if weather_key == "humidity":
                    corrected_weather[weather_key] = max(
                        0.0, min(100.0, corrected_weather[weather_key])
                    )
                else:
                    corrected_weather[weather_key] = max(
                        0.0, corrected_weather[weather_key]
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

    def _apply_terminal_kalman(
        self, corrected: dict[str, Any], date_str: str, hour: int, now
    ) -> dict[str, Any]:
        """Add Kalman after the solar factor/cap and record the delivered delta."""
        raw_corrected_ghi = corrected.get("solar_radiation_wm2")
        if raw_corrected_ghi is None:
            corrected["_sfml_kalman_applied"] = False
            corrected["_sfml_kalman_bias"] = 0.0
            return corrected
        try:
            initial_ghi_wm2 = float(raw_corrected_ghi)
        except (TypeError, ValueError):
            corrected["_sfml_kalman_applied"] = False
            corrected["_sfml_kalman_bias"] = 0.0
            return corrected
        if not math.isfinite(initial_ghi_wm2):
            corrected["_sfml_kalman_applied"] = False
            corrected["_sfml_kalman_bias"] = 0.0
            return corrected
        kalman_bias = 0.0
        live_observation_available = (
            bool(corrected.get("_sfml_local_observation_available"))
            and self._has_available_local_solar_observation()
        )
        if live_observation_available and self._kalman_tracker is not None:
            try:
                kalman_bias = float(self._kalman_tracker.get_correction() or 0.0)
            except (TypeError, ValueError, AttributeError):
                kalman_bias = 0.0
            if not math.isfinite(kalman_bias):
                kalman_bias = 0.0
            if not should_apply_kalman(
                date_str, hour, now, float(corrected.get("sun_elevation") or 0.0)
            ):
                kalman_bias = 0.0
        delivered_ghi_wm2, delivered_kalman_bias = apply_terminal_kalman_ghi(
            initial_ghi_wm2,
            kalman_bias,
            cap_ceiling=corrected.get("_sfml_ghi_cap_ceiling"),
        )
        if abs(delivered_kalman_bias) > 0:
            radiation_scale = (
                delivered_ghi_wm2 / initial_ghi_wm2 if initial_ghi_wm2 > 0 else 0.0
            )
            if corrected.get("direct_radiation") is not None:
                corrected["direct_radiation"] = round(
                    max(0.0, float(corrected["direct_radiation"]) * radiation_scale), 1
                )
            if corrected.get("diffuse_radiation") is not None:
                corrected["diffuse_radiation"] = round(
                    max(0.0, float(corrected["diffuse_radiation"]) * radiation_scale), 1
                )
            self._record_correction_stat("kalman_today")
        corrected["solar_radiation_wm2"] = delivered_ghi_wm2
        corrected["_sfml_kalman_applied"] = abs(delivered_kalman_bias) > 0
        corrected["_sfml_kalman_bias"] = delivered_kalman_bias
        return corrected

    async def _save_corrected_forecast(
        self, date_str: str, hour: int, data: dict[str, Any]
    ) -> bool:
        """Save corrected forecast to database. @zara"""
        try:
            await self.execute_query(
                "INSERT INTO weather_forecast\n                   (forecast_date, hour, temperature, humidity, clouds, rain,\n                    wind, pressure, direct_radiation, diffuse_radiation,\n                    solar_radiation_wm2, cloud_cover_low, cloud_cover_mid,\n                    cloud_cover_high, visibility_m, visibility_source,\n                    fog_detected, fog_type, weather_code, correction_snapshot_id)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       temperature = excluded.temperature,\n                       humidity = excluded.humidity,\n                       clouds = excluded.clouds,\n                       rain = excluded.rain,\n                       wind = excluded.wind,\n                       pressure = excluded.pressure,\n                       direct_radiation = excluded.direct_radiation,\n                       diffuse_radiation = excluded.diffuse_radiation,\n                       solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                       cloud_cover_low = excluded.cloud_cover_low,\n                       cloud_cover_mid = excluded.cloud_cover_mid,\n                       cloud_cover_high = excluded.cloud_cover_high,\n                       visibility_m = excluded.visibility_m,\n                       visibility_source = excluded.visibility_source,\n                       fog_detected = excluded.fog_detected,\n                       fog_type = excluded.fog_type,\n                       weather_code = excluded.weather_code,\n                       correction_snapshot_id = excluded.correction_snapshot_id,\n                       updated_at = CURRENT_TIMESTAMP",
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
                    data.get("visibility_source"),
                    data.get("fog_detected"),
                    data.get("fog_type"),
                    data.get("weather_code"),
                    data.get("_sfml_correction_snapshot_id"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Error saving corrected forecast: %s", error)
            return False
        return True

    async def get_correction_summary(self) -> dict[str, Any]:
        """Get summary of applied corrections. @zara"""
        try:
            correction_bundle = await self._load_correction_factors()
            return {
                "factors": correction_bundle["factors"],
                "sample_days": correction_bundle["sample_days"],
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
            today = dt_util.now().date().isoformat()
            database_row = await self.fetch_one(
                "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
                (today,),
            )
            if database_row and database_row[0] > 0:
                return True
            return await self.create_corrected_forecast()
        except Exception as error:
            _LOGGER.error("Bootstrap integration cache error: %s", error)
            return False
