# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Data Pipeline Manager V16.2.0 - Central Weather Data Pipeline

Central manager for weather data pipeline that coordinates:
- Open-Meteo data fetching
- Multi-source expert blending
- Precision tracking and correction
- Actual weather tracking

Runs background tasks for updates and ensures Single Source of Truth.

@zara
"""
import asyncio
import logging
from datetime import timedelta
from typing import Optional
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from .db_manager import DatabaseManager, WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
from .data_io import DataManagerIO
from .data_open_meteo_client import OpenMeteoClient, required_forecast_hours_for_date
from .data_weather_actual_tracker import WeatherActualTracker
from .data_weather_corrector import WeatherForecastCorrector
from .data_weather_expert_blender import WeatherExpertBlender, WeatherExpertLearner
from .data_weather_precision import WeatherPrecisionTracker
from .data_visibility_learner import VisibilityLearner
from ..const import CONF_WINTER_MODE, DEFAULT_WINTER_MODE
from ..core.core_helpers import SafeDateTimeUtil

_LOGGER = logging.getLogger(__name__)
RADIATION_SAMPLE_MINUTES = (5, 15, 25, 35, 45, 55)


def corrected_forecast_horizon_complete(
    rows, start_date, timezone_name: Optional[str], days: int = 3
) -> bool:
    """Return whether corrected weather covers the complete required horizon."""
    counts_by_date = {str(row[0]): int(row[1] or 0) for row in rows or []}
    for day_offset in range(days):
        forecast_day = start_date + timedelta(days=day_offset)
        if not counts_by_date.get(
            forecast_day.isoformat(), 0
        ) < required_forecast_hours_for_date(forecast_day, timezone_name):
            continue
        return False
    return True


class WeatherDataPipelineManager(DataManagerIO):
    """Central manager for weather data pipeline. @zara

    Manages all weather data operations from raw forecasts to corrected forecasts.
    Runs background tasks for updates and coordinates all weather components.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        config_entry=None,
        coordinator=None,
    ):
        "Initialize Weather Data Pipeline Manager. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager instance\n    config_entry: Config entry with settings\n    coordinator: Coordinator instance for accessing solar_capacity\n"
        super().__init__(hass, db_manager)
        self.config_entry = config_entry
        self.coordinator = coordinator
        self._background_tasks = []
        self._listeners = []
        self._running = False
        self._last_refresh_status = "idle"
        self._last_refresh_reason = None
        self._last_refresh_at = None
        self._last_corrected_forecast_status = "idle"
        self._last_corrected_forecast_reason = None
        self._last_corrected_forecast_at = None
        self.open_meteo_client = None
        self.weather_actual_tracker = None
        self.weather_precision = None
        self.weather_corrector = None
        self.weather_expert_blender = None
        self.weather_expert_learner = None
        self.visibility_learner = None
        _LOGGER.debug("Weather Data Pipeline Manager initialized")

    def _set_refresh_status(self, status: str, reason: Optional[str] = None) -> None:
        """Track the latest weather refresh state for diagnostics. @zara"""
        self._last_refresh_status = status
        self._last_refresh_reason = reason
        self._last_refresh_at = SafeDateTimeUtil.now().isoformat()

    def _set_corrected_forecast_status(
        self, status: str, reason: Optional[str] = None
    ) -> None:
        """Track the latest corrected-forecast state for diagnostics. @zara"""
        self._last_corrected_forecast_status = status
        self._last_corrected_forecast_reason = reason
        self._last_corrected_forecast_at = SafeDateTimeUtil.now().isoformat()

    async def async_setup(self) -> bool:
        """Setup weather data pipeline components. @zara"""
        try:
            _LOGGER.debug("Setting up Weather Data Pipeline Manager...")
            await self.ensure_initialized()
            latitude = self.hass.config.latitude
            longitude = self.hass.config.longitude
            self.open_meteo_client = OpenMeteoClient(
                hass=self.hass,
                db_manager=self.db,
                latitude=latitude,
                longitude=longitude,
            )
            self.weather_actual_tracker = WeatherActualTracker(
                hass=self.hass, db_manager=self.db, config_entry=self.config_entry
            )
            self.weather_precision = WeatherPrecisionTracker(
                hass=self.hass, db_manager=self.db, config_entry=self.config_entry
            )
            pirate_weather_key = None
            if self.config_entry and self.config_entry.options:
                pirate_weather_key = self.config_entry.options.get(
                    "pirate_weather_api_key"
                )
            self.weather_expert_blender = WeatherExpertBlender(
                hass=self.hass,
                db_manager=self.db,
                latitude=latitude,
                longitude=longitude,
                pirate_weather_api_key=pirate_weather_key,
            )
            await asyncio.gather(
                self.open_meteo_client.async_init(),
                self.weather_expert_blender.async_init(),
            )
            _LOGGER.debug(
                "Open-Meteo client initialized (lat=%.4f, lon=%.4f)",
                latitude,
                longitude,
            )
            active_expert_count = len(
                [
                    expert_or_error
                    for expert_or_error in self.weather_expert_blender._experts
                    if self.weather_expert_blender._is_expert_enabled(expert_or_error)
                ]
            )
            _LOGGER.debug(
                "Weather Expert Blender initialized with %d active experts",
                active_expert_count,
            )
            self.weather_expert_learner = WeatherExpertLearner(
                hass=self.hass,
                db_manager=self.db,
                blender=self.weather_expert_blender,
                config_entry=self.config_entry,
            )
            self.visibility_learner = VisibilityLearner(
                hass=self.hass, db_manager=self.db, config_entry=self.config_entry
            )
            await self.visibility_learner.initialize()
            self.weather_expert_blender.set_visibility_learner(self.visibility_learner)
            self.weather_expert_learner.set_visibility_learner(self.visibility_learner)
            _LOGGER.debug(
                "VisibilityLearner initialized (active=%s)",
                self.visibility_learner.is_learning_active,
            )
            winter_mode = DEFAULT_WINTER_MODE
            if self.config_entry:
                winter_mode = self.config_entry.options.get(
                    CONF_WINTER_MODE, DEFAULT_WINTER_MODE
                )
            self.weather_corrector = WeatherForecastCorrector(
                hass=self.hass,
                db_manager=self.db,
                open_meteo_client=self.open_meteo_client,
                expert_blender=self.weather_expert_blender,
                winter_mode=winter_mode,
            )
            await self.weather_corrector.async_init()
            _LOGGER.debug("Weather Data Pipeline Manager setup complete")
        except Exception as expert_or_error:
            _LOGGER.error(
                "Failed to setup Weather Data Pipeline Manager: %s",
                expert_or_error,
                exc_info=True,
            )
            return False
        return True

    async def start_pipeline(self) -> bool:
        """Start all background tasks for weather data pipeline. @zara"""
        try:
            if self._running:
                _LOGGER.warning("Pipeline already running")
                return True
            _LOGGER.debug("Starting Weather Data Pipeline...")
            self.hass.async_create_task(self._startup_open_meteo_refresh())
            self.hass.async_create_task(self._bootstrap_corrected_forecast())
            for update_hour in (6, 12, 18):
                unsubscribe = async_track_time_change(
                    self.hass,
                    self._scheduled_open_meteo_update,
                    hour=update_hour,
                    minute=0,
                    second=0,
                )
                self._listeners.append(unsubscribe)
            _LOGGER.debug(
                "Scheduled: Open-Meteo update (3x daily: 06:00, 12:00, 18:00)"
            )
            unsubscribe = async_track_time_change(
                self.hass, self._scheduled_actual_tracking, minute=0, second=0
            )
            self._listeners.append(unsubscribe)
            _LOGGER.debug("Scheduled: Hourly weather tracking (every hour at :00)")
            unsubscribe = async_track_time_change(
                self.hass,
                self._scheduled_radiation_sample,
                minute=list(RADIATION_SAMPLE_MINUTES),
                second=0,
            )
            self._listeners.append(unsubscribe)
            _LOGGER.debug(
                "Scheduled: Radiation sampling (minutes %s)",
                ",".join(
                    (str(sample_minute) for sample_minute in RADIATION_SAMPLE_MINUTES)
                ),
            )
            unsubscribe = async_track_time_change(
                self.hass,
                self._scheduled_corrected_forecast,
                hour=0,
                minute=20,
                second=0,
            )
            self._listeners.append(unsubscribe)
            _LOGGER.debug(
                "Scheduled: Daily corrected forecast (00:20 with weather refresh)"
            )
            self._running = True
            _LOGGER.debug(
                "Weather Data Pipeline started (%d scheduled listeners)",
                len(self._listeners),
            )
            return True
        except Exception as error:
            _LOGGER.error("Failed to start pipeline: %s", error, exc_info=True)
            return False

    async def stop_pipeline(self) -> None:
        """Stop all background tasks and listeners. @zara"""
        try:
            _LOGGER.info("Stopping Weather Data Pipeline...")
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._background_tasks.clear()
            for unsubscribe in self._listeners:
                unsubscribe()
            self._listeners.clear()
            self._running = False
            _LOGGER.info("Weather Data Pipeline stopped")
        except Exception as error:
            _LOGGER.error("Error stopping pipeline: %s", error, exc_info=True)
        return None if False else None

    async def _startup_open_meteo_refresh(self) -> None:
        """One-time Open-Meteo refresh after HA restart. @zara

        Returns immediately and schedules actual work for later to not block HA startup.
        """
        from homeassistant.helpers.event import (
            async_track_point_in_time as async_track_point_in_time,
        )
        from homeassistant.util import dt as dt_util
        from datetime import timedelta as timedelta_type

        async def _do_refresh(now):
            """Actual refresh work - runs 1 minute after HA starts."""
            try:
                if self.open_meteo_client:
                    _LOGGER.debug("Pipeline: Fetching weather data from Open-Meteo...")
                    hourly_forecast = await self.open_meteo_client.get_hourly_forecast()
                    if hourly_forecast:
                        _LOGGER.debug("Pipeline: Open-Meteo startup refresh SUCCESS")
                        self._set_refresh_status("ok", "startup_refresh")
                    else:
                        _LOGGER.warning(
                            "Pipeline: Open-Meteo startup refresh FAILED - will retry in 2min"
                        )
                        self._set_refresh_status(
                            "degraded", "startup_open_meteo_failed_retrying"
                        )

                        async def _retry(retry_now):
                            hourly_forecast = (
                                await self.open_meteo_client.get_hourly_forecast()
                            )
                            if hourly_forecast:
                                _LOGGER.info("Pipeline: Open-Meteo retry SUCCESS")
                                self._set_refresh_status("ok", "startup_retry")
                            else:
                                _LOGGER.warning(
                                    "Pipeline: Open-Meteo unavailable - using cached data"
                                )
                                self._set_refresh_status(
                                    "degraded", "startup_retry_cached_fallback"
                                )
                            await self._fetch_expert_weather_sources()

                        unsubscribe = async_track_point_in_time(
                            self.hass,
                            _retry,
                            dt_util.utcnow() + timedelta_type(seconds=120),
                        )
                        self._listeners.append(unsubscribe)
                        return
                await self._fetch_expert_weather_sources()
            except Exception as error:
                _LOGGER.error(
                    "Pipeline: Startup Open-Meteo refresh error: %s",
                    error,
                    exc_info=True,
                )
            return None if True else None

        _LOGGER.debug("Pipeline: Startup Open-Meteo refresh scheduled (1min delay)")
        unsubscribe = async_track_point_in_time(
            self.hass, _do_refresh, dt_util.utcnow() + timedelta_type(seconds=60)
        )
        self._listeners.append(unsubscribe)

    async def _bootstrap_corrected_forecast(self) -> None:
        """One-time bootstrap for corrected forecast after fresh install. @zara

        Returns immediately and schedules actual work for later to not block HA startup.
        Retries up to 3 times on failure (10min intervals). @zara V16.4.2
        """
        from homeassistant.helpers.event import (
            async_track_point_in_time as async_track_point_in_time,
        )
        from homeassistant.util import dt as dt_util
        from datetime import timedelta as timedelta_type

        bootstrap_attempts = [0]

        async def _do_bootstrap(now):
            """Actual bootstrap work - runs 5 minutes after HA starts."""
            bootstrap_attempts[0] += 1
            try:
                today = dt_util.now().date()
                horizon_end = today + timedelta_type(days=2)
                horizon_counts = await self.fetch_all(
                    "SELECT forecast_date, COUNT(DISTINCT hour)\n                       FROM weather_forecast\n                       WHERE forecast_date BETWEEN ? AND ?\n                       GROUP BY forecast_date",
                    (today.isoformat(), horizon_end.isoformat()),
                )
                horizon_complete = corrected_forecast_horizon_complete(
                    horizon_counts, today, self.hass.config.time_zone
                )
                contract_row = await self.fetch_one(
                    "SELECT contract_version\n                         FROM weather_forecast_correction_snapshots\n                        ORDER BY corrected_at DESC\n                        LIMIT 1"
                )
                contract_current = bool(
                    contract_row
                    and contract_row[0] == WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
                )
                if not horizon_complete or not contract_current:
                    _LOGGER.info(
                        "Pipeline: Bootstrap - Creating initial corrected forecast (attempt %d)...",
                        bootstrap_attempts[0],
                    )
                    if self.open_meteo_client:
                        await self.open_meteo_client.get_hourly_forecast()
                    if self.weather_corrector:
                        success = (
                            await self.weather_corrector.create_corrected_forecast(
                                min_confidence=0.0
                            )
                        )
                        if success:
                            _LOGGER.info(
                                "Pipeline: Bootstrap SUCCESS - corrected forecast created"
                            )
                            self._set_corrected_forecast_status("ok", "bootstrap")
                        elif bootstrap_attempts[0] < 3:
                            _LOGGER.warning(
                                "Pipeline: Bootstrap could not create corrected forecast — retry %d/3 in 10min",
                                bootstrap_attempts[0],
                            )
                            self._set_corrected_forecast_status(
                                "degraded", f"bootstrap_retry_{bootstrap_attempts[0]}"
                            )
                            unsubscribe = async_track_point_in_time(
                                self.hass,
                                _do_bootstrap,
                                dt_util.utcnow() + timedelta_type(seconds=600),
                            )
                            self._listeners.append(unsubscribe)
                        else:
                            _LOGGER.warning(
                                "Pipeline: Bootstrap failed after %d attempts",
                                bootstrap_attempts[0],
                            )
                            self._set_corrected_forecast_status(
                                "failed",
                                f"bootstrap_failed_after_{bootstrap_attempts[0]}",
                            )
                else:
                    _LOGGER.debug(
                        "Pipeline: Complete corrected forecast exists - skip bootstrap"
                    )
                    self._set_corrected_forecast_status(
                        "ok", "bootstrap_skipped_existing_data"
                    )
            except Exception as error:
                _LOGGER.error(
                    "Pipeline: Bootstrap corrected forecast error: %s",
                    error,
                    exc_info=True,
                )
                self._set_corrected_forecast_status("failed", "bootstrap_exception")
                if bootstrap_attempts[0] < 3:
                    unsubscribe = async_track_point_in_time(
                        self.hass,
                        _do_bootstrap,
                        dt_util.utcnow() + timedelta_type(seconds=600),
                    )
                    self._listeners.append(unsubscribe)
            return None if True else None

        _LOGGER.debug("Pipeline: Bootstrap corrected forecast scheduled (5min delay)")
        unsubscribe = async_track_point_in_time(
            self.hass, _do_bootstrap, dt_util.utcnow() + timedelta_type(seconds=300)
        )
        self._listeners.append(unsubscribe)

    async def _scheduled_open_meteo_update(self, now) -> None:
        """Scheduled task: Update weather forecast with 5-source blending. @zara"""
        try:
            _LOGGER.info("Pipeline: Scheduled weather update triggered")
            success = await self._refresh_all_weather_sources()
            if success:
                _LOGGER.info("Pipeline: Weather update SUCCESS")
            else:
                _LOGGER.warning("Pipeline: Weather update failed - using cached data")
                self._set_refresh_status("degraded", "scheduled_update_cached_fallback")
        except Exception as error:
            _LOGGER.error("Pipeline: Weather update error: %s", error, exc_info=True)
            self._set_refresh_status("failed", "scheduled_update_exception")
        return None if True else None

    async def _fetch_expert_weather_sources(self, force: bool = False) -> None:
        """Fetch data from all expert weather sources. @zara"""
        if not self.weather_expert_blender:
            return
        source_status = []
        try:
            wttr_expert = self.weather_expert_blender._wttr_expert
            success = await wttr_expert._fetch_forecast(force=force)
            source_status.append(f"wttr.in: {('OK' if success else 'FAILED')}")
        except Exception as error:
            source_status.append(f"wttr.in: ERROR ({error})")
        try:
            bright_sky_expert = self.weather_expert_blender._bright_sky_expert
            success = await bright_sky_expert._fetch_forecast(force=force)
            source_status.append(f"Bright Sky: {('OK' if success else 'FAILED')}")
        except Exception as error:
            source_status.append(f"Bright Sky: ERROR ({error})")
        try:
            pirate_expert = self.weather_expert_blender._pirate_weather_expert
            if pirate_expert._enabled:
                success = await pirate_expert._fetch_forecast(force=force)
                source_status.append(
                    f"Pirate Weather: {('OK' if success else 'FAILED')}"
                )
            else:
                source_status.append("Pirate Weather: disabled")
        except Exception as error:
            source_status.append(f"Pirate Weather: ERROR ({error})")
        if source_status:
            _LOGGER.info("Pipeline: Expert sources: %s", ", ".join(source_status))
        return None if True else None

    async def _refresh_all_weather_sources(self, force: bool = False) -> bool:
        """Refresh all 5 weather sources and create corrected forecast. @zara"""
        try:
            _LOGGER.info(
                "Pipeline: Refreshing all weather sources%s...",
                " (force)" if force else "",
            )
            degraded = False
            cache_info = {}
            if self.weather_expert_blender:
                self.weather_expert_blender.reset_stale_cache_reads()
            try:
                await self.db.prune_expired_weather_caches()
            except Exception as prune_error:
                _LOGGER.debug("Pipeline: Weather cache prune skipped: %s", prune_error)
            if self.open_meteo_client:
                hourly_forecast = await self.open_meteo_client.get_hourly_forecast(
                    force=force
                )
                cache_info = self.open_meteo_client._get_cache_source_info()
                if hourly_forecast:
                    cache_source = cache_info.get("source")
                    cache_status = cache_info.get("status")
                    _LOGGER.debug(
                        "Pipeline: Open-Meteo data available (source=%s, status=%s, age=%s)",
                        cache_source,
                        cache_status,
                        cache_info.get("age_hours"),
                    )
                    degraded = cache_status != "ok" or (force and cache_source != "api")
                else:
                    _LOGGER.warning(
                        "Pipeline: Open-Meteo fetch FAILED - using cached data"
                    )
                    degraded = True
            await self._fetch_expert_weather_sources(force=force)
            if self.weather_corrector:
                success = await self.weather_corrector.create_corrected_forecast()
                if success:
                    _LOGGER.info("Pipeline: Corrected forecast updated")
                    if degraded:
                        cache_source = cache_info.get("source", "unknown")
                        reason = cache_info.get("reason", "cached_fallback")
                        self._set_refresh_status(
                            "degraded", f"open_meteo_{cache_source}_{reason}"
                        )
                    elif (
                        self.weather_expert_blender
                        and self.weather_expert_blender.had_stale_cache_reads()
                    ):
                        self._set_refresh_status("degraded", "expert_cache_stale")
                    else:
                        reason = cache_info.get("reason", "fresh_refresh")
                        self._set_refresh_status("ok", f"open_meteo_{reason}")
                    self._set_corrected_forecast_status("ok", "refresh_success")
                    return True
                _LOGGER.warning("Pipeline: Corrected forecast creation failed")
                self._set_refresh_status(
                    "failed", "corrected_forecast_creation_failed_after_refresh"
                )
                self._set_corrected_forecast_status("failed", "refresh_creation_failed")
                return False
            self._set_refresh_status("failed", "weather_corrector_unavailable")
            self._set_corrected_forecast_status(
                "failed", "weather_corrector_unavailable"
            )
            return False
        except Exception as error:
            _LOGGER.error("Pipeline: Weather refresh error: %s", error, exc_info=True)
            self._set_refresh_status("failed", "refresh_exception")
            self._set_corrected_forecast_status("failed", "refresh_exception")
            return False

    async def _scheduled_radiation_sample(self, now) -> None:
        """Scheduled task: collect one radiation sample for the running hour. @zara"""
        try:
            if not self.weather_actual_tracker:
                return
            await self.weather_actual_tracker.sample_solar_radiation(now)
        except Exception as error:
            _LOGGER.debug("Pipeline: Radiation sample error: %s", error)
        return None if True else None

    async def _scheduled_actual_tracking(self, now) -> None:
        """Scheduled task: Track actual weather from local sensors. @zara"""
        try:
            if not self.weather_actual_tracker:
                return
            actual_weather = await self.weather_actual_tracker.track_current_weather()
            if actual_weather:
                _LOGGER.debug(
                    "Pipeline: Tracked actual weather - temp=%.1fC, clouds=%.0f%%",
                    actual_weather.get("temperature_c", 0),
                    actual_weather.get("cloud_cover_percent", 0),
                )
        except Exception as error:
            _LOGGER.error("Pipeline: Actual tracking error: %s", error, exc_info=True)
        return None if True else None

    async def _scheduled_corrected_forecast(self, now) -> None:
        """Scheduled task: Create daily corrected forecast with fresh weather. @zara"""
        try:
            _LOGGER.info("Pipeline: Scheduled corrected forecast triggered (00:20)")
            await self._refresh_all_weather_sources(force=True)
        except Exception as error:
            _LOGGER.error(
                "Pipeline: Corrected forecast error: %s", error, exc_info=True
            )
            self._set_corrected_forecast_status(
                "failed", "scheduled_corrected_forecast_exception"
            )
        return None if True else None

    async def trigger_precision_calculation(
        self, date_str: Optional[str] = None
    ) -> bool:
        "Trigger precision factor calculation. @zara\n\nArgs:\n    date_str: Date to calculate (default: today)\n\nReturns:\n    True if calculation was successful\n"
        try:
            if not self.weather_precision:
                return False
            if date_str is None:
                date_str = SafeDateTimeUtil.now().strftime("%Y-%m-%d")
            return await self.weather_precision.calculate_correction_factors_v2(
                weather_actual_tracker=self.weather_actual_tracker, date_str=date_str
            )
        except Exception as error:
            _LOGGER.error("Error triggering precision calculation: %s", error)
            return False

    async def trigger_expert_learning(self, date_str: Optional[str] = None) -> bool:
        "Trigger expert weight learning. @zara\n\nArgs:\n    date_str: Date to learn from (default: yesterday)\n\nReturns:\n    True if learning was successful\n"
        try:
            if not self.weather_expert_learner:
                return False
            if date_str is None:
                date_str = (SafeDateTimeUtil.now() - timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
            return await self.weather_expert_learner.learn_from_day(date_str)
        except Exception as error:
            _LOGGER.error("Error triggering expert learning: %s", error)
            return False

    async def update_weather_cache(self, force: bool = False) -> bool:
        "Public API: Refresh all weather sources and create corrected forecast. @zara\n\nThis is the main entry point for services to trigger a full weather refresh.\n\nArgs:\n    force: Force refresh even if data is still fresh\n\nReturns:\n    True if refresh was successful\n"
        return await self._refresh_all_weather_sources(force=force)

    def get_pipeline_status(self) -> dict:
        """Get current pipeline status. @zara"""
        cache_info = (
            self.open_meteo_client._get_cache_source_info()
            if self.open_meteo_client
            else None
        )
        return {
            "running": self._running,
            "listeners_count": len(self._listeners),
            "tasks_count": len(self._background_tasks),
            "open_meteo_status": cache_info,
            "refresh_status": {
                "status": self._last_refresh_status,
                "reason": self._last_refresh_reason,
                "at": self._last_refresh_at,
            },
            "corrected_forecast_status": {
                "status": self._last_corrected_forecast_status,
                "reason": self._last_corrected_forecast_reason,
                "at": self._last_corrected_forecast_at,
            },
            "components": {
                "open_meteo_client": self.open_meteo_client is not None,
                "weather_actual_tracker": self.weather_actual_tracker is not None,
                "weather_precision": self.weather_precision is not None,
                "weather_corrector": self.weather_corrector is not None,
                "weather_expert_blender": self.weather_expert_blender is not None,
                "weather_expert_learner": self.weather_expert_learner is not None,
            },
        }
