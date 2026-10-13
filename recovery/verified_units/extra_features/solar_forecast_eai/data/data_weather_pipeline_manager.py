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
from datetime import datetime, timedelta
from typing import Optional
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_open_meteo_client import OpenMeteoClient
from .data_weather_actual_tracker import WeatherActualTracker
from .data_weather_corrector import WeatherForecastCorrector
from .data_weather_expert_blender import WeatherExpertBlender, WeatherExpertLearner
from .data_weather_precision import WeatherPrecisionTracker
from .data_visibility_learner import VisibilityLearner
from ..const import CONF_WINTER_MODE, DEFAULT_WINTER_MODE

_LOGGER = logging.getLogger(__name__)


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
        self.astronomy_provider = (
            getattr(coordinator, "scheduled_tasks", None)
            and coordinator.scheduled_tasks.astronomy_provider
        )
        self._background_tasks = []
        self._listeners = []
        self._running = False
        self.open_meteo_client = None
        self.weather_actual_tracker = None
        self.weather_precision = None
        self.weather_corrector = None
        self.weather_expert_blender = None
        self.weather_expert_learner = None
        self.visibility_learner = None
        _LOGGER.info("Weather Data Pipeline Manager initialized")

    async def async_setup(self) -> bool:
        """Setup weather data pipeline components. @zara"""
        try:
            _LOGGER.info("Setting up Weather Data Pipeline Manager...")
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
                hass=self.hass,
                db_manager=self.db,
                config_entry=self.config_entry,
                astronomy_provider=self.astronomy_provider,
            )
            self.weather_precision = WeatherPrecisionTracker(
                hass=self.hass, db_manager=self.db, config_entry=self.config_entry
            )
            pirate_api_key = None
            if self.config_entry and self.config_entry.options:
                pirate_api_key = self.config_entry.options.get("pirate_weather_api_key")
            self.weather_expert_blender = WeatherExpertBlender(
                hass=self.hass,
                db_manager=self.db,
                latitude=latitude,
                longitude=longitude,
                pirate_weather_api_key=pirate_api_key,
            )
            await asyncio.gather(
                self.open_meteo_client.async_init(),
                self.weather_expert_blender.async_init(),
            )
            _LOGGER.info(
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
            _LOGGER.info(
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
                hass=self.hass,
                db_manager=self.db,
                config_entry=self.config_entry,
                astronomy_provider=self.astronomy_provider,
            )
            await self.visibility_learner.initialize()
            self.weather_expert_blender.set_visibility_learner(self.visibility_learner)
            self.weather_expert_learner.set_visibility_learner(self.visibility_learner)
            _LOGGER.info(
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
                astronomy_provider=self.astronomy_provider,
            )
            await self.weather_corrector.async_init()
            _LOGGER.info("Weather Data Pipeline Manager setup complete")
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
            _LOGGER.info("Starting Weather Data Pipeline...")
            self._background_tasks.extend(
                [
                    self.hass.async_create_task(self._startup_open_meteo_refresh()),
                    self.hass.async_create_task(self._bootstrap_corrected_forecast()),
                ]
            )
            for scheduled_hour in (6, 12, 18):
                remove_listener = async_track_time_change(
                    self.hass,
                    self._scheduled_open_meteo_update,
                    hour=scheduled_hour,
                    minute=0,
                    second=0,
                )
                self._listeners.append(remove_listener)
            _LOGGER.info("Scheduled: Open-Meteo update (3x daily: 06:00, 12:00, 18:00)")
            remove_listener = async_track_time_change(
                self.hass, self._scheduled_actual_tracking, minute=0, second=0
            )
            self._listeners.append(remove_listener)
            _LOGGER.info("Scheduled: Hourly weather tracking (every hour at :00)")
            remove_listener = async_track_time_change(
                self.hass,
                self._scheduled_corrected_forecast,
                hour=0,
                minute=20,
                second=0,
            )
            self._listeners.append(remove_listener)
            _LOGGER.info(
                "Scheduled: Daily corrected forecast (00:20 with weather refresh)"
            )
            self._running = True
            _LOGGER.info(
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
            for background_task in self._background_tasks:
                if not background_task.done():
                    background_task.cancel()
                    try:
                        await background_task
                    except asyncio.CancelledError:
                        pass
            self._background_tasks.clear()
            for remove_listener in self._listeners:
                remove_listener()
            self._listeners.clear()
            self._running = False
            _LOGGER.info("Weather Data Pipeline stopped")
        except Exception as error:
            _LOGGER.error("Error stopping pipeline: %s", error, exc_info=True)
        return None if True else None

    async def _startup_open_meteo_refresh(self) -> None:
        """One-time Open-Meteo refresh after HA restart. @zara

        Returns immediately and schedules actual work for later to not block HA startup.
        """
        from homeassistant.helpers.event import (
            async_track_point_in_time as async_track_point_in_time,
        )
        from homeassistant.util import dt as dt_util
        from datetime import timedelta as timedelta_factory

        async def _do_refresh(now):
            """Actual refresh work - runs 1 minute after HA starts."""
            try:
                if self.open_meteo_client:
                    _LOGGER.info("Pipeline: Fetching weather data from Open-Meteo...")
                    open_meteo_forecast = (
                        await self.open_meteo_client.get_hourly_forecast()
                    )
                    if open_meteo_forecast:
                        _LOGGER.info("Pipeline: Open-Meteo startup refresh SUCCESS")
                    else:
                        _LOGGER.warning(
                            "Pipeline: Open-Meteo startup refresh FAILED - will retry in 2min"
                        )

                        async def _retry(retry_now):
                            open_meteo_forecast = (
                                await self.open_meteo_client.get_hourly_forecast()
                            )
                            if open_meteo_forecast:
                                _LOGGER.info("Pipeline: Open-Meteo retry SUCCESS")
                            else:
                                _LOGGER.warning(
                                    "Pipeline: Open-Meteo unavailable - using cached data"
                                )
                            await self._fetch_expert_weather_sources()

                        self._listeners.append(
                            async_track_point_in_time(
                                self.hass,
                                _retry,
                                dt_util.utcnow() + timedelta_factory(seconds=120),
                            )
                        )
                        return
                await self._fetch_expert_weather_sources()
            except Exception as error:
                _LOGGER.error(
                    "Pipeline: Startup Open-Meteo refresh error: %s",
                    error,
                    exc_info=True,
                )
            return None if True else None

        _LOGGER.info("Pipeline: Startup Open-Meteo refresh scheduled (1min delay)")
        self._listeners.append(
            async_track_point_in_time(
                self.hass, _do_refresh, dt_util.utcnow() + timedelta_factory(seconds=60)
            )
        )

    async def _bootstrap_corrected_forecast(self) -> None:
        """One-time bootstrap for corrected forecast after fresh install. @zara

        Returns immediately and schedules actual work for later to not block HA startup.
        Retries up to 3 times on failure (10min intervals). @zara V16.4.2
        """
        from homeassistant.helpers.event import (
            async_track_point_in_time as async_track_point_in_time,
        )
        from homeassistant.util import dt as dt_util
        from datetime import timedelta as timedelta_factory

        bootstrap_attempts = [0]

        async def _do_bootstrap(now):
            """Actual bootstrap work - runs 5 minutes after HA starts."""
            bootstrap_attempts[0] += 1
            try:
                today = dt_util.now().date().isoformat()
                forecast_count_row = await self.fetch_one(
                    "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
                    (today,),
                )
                forecast_exists = forecast_count_row and forecast_count_row[0] > 0
                if not forecast_exists:
                    _LOGGER.info(
                        "Pipeline: Bootstrap - Creating initial corrected forecast (attempt %d)...",
                        bootstrap_attempts[0],
                    )
                    if self.open_meteo_client:
                        await self.open_meteo_client.get_hourly_forecast()
                    if self.weather_corrector:
                        fetch_succeeded = (
                            await self.weather_corrector.create_corrected_forecast(
                                min_confidence=0.0
                            )
                        )
                        if fetch_succeeded:
                            _LOGGER.info(
                                "Pipeline: Bootstrap SUCCESS - corrected forecast created"
                            )
                        elif bootstrap_attempts[0] < 3:
                            _LOGGER.warning(
                                "Pipeline: Bootstrap could not create corrected forecast — retry %d/3 in 10min",
                                bootstrap_attempts[0],
                            )
                            self._listeners.append(
                                async_track_point_in_time(
                                    self.hass,
                                    _do_bootstrap,
                                    dt_util.utcnow() + timedelta_factory(seconds=600),
                                )
                            )
                        else:
                            _LOGGER.warning(
                                "Pipeline: Bootstrap failed after %d attempts",
                                bootstrap_attempts[0],
                            )
                else:
                    _LOGGER.info("Pipeline: Corrected forecast exists - skip bootstrap")
            except Exception as error:
                _LOGGER.error(
                    "Pipeline: Bootstrap corrected forecast error: %s",
                    error,
                    exc_info=True,
                )
                if bootstrap_attempts[0] < 3:
                    self._listeners.append(
                        async_track_point_in_time(
                            self.hass,
                            _do_bootstrap,
                            dt_util.utcnow() + timedelta_factory(seconds=600),
                        )
                    )
            return None if True else None

        _LOGGER.info("Pipeline: Bootstrap corrected forecast scheduled (5min delay)")
        self._listeners.append(
            async_track_point_in_time(
                self.hass,
                _do_bootstrap,
                dt_util.utcnow() + timedelta_factory(seconds=300),
            )
        )

    async def _scheduled_open_meteo_update(self, now) -> None:
        """Scheduled task: Update weather forecast with 5-source blending. @zara"""
        try:
            _LOGGER.info("Pipeline: Scheduled weather update triggered")
            fetch_succeeded = await self._refresh_all_weather_sources()
            if fetch_succeeded:
                _LOGGER.info("Pipeline: Weather update SUCCESS")
            else:
                _LOGGER.warning("Pipeline: Weather update failed - using cached data")
        except Exception as error:
            _LOGGER.error("Pipeline: Weather update error: %s", error, exc_info=True)
        return None if True else None

    async def _fetch_expert_weather_sources(self, force: bool = False) -> None:
        """Fetch data from all expert weather sources. @zara"""
        if not self.weather_expert_blender:
            return
        source_status = []
        try:
            wttr_expert = self.weather_expert_blender._wttr_expert
            fetch_succeeded = await wttr_expert._fetch_forecast(force=force)
            source_status.append(f"wttr.in: {('OK' if fetch_succeeded else 'FAILED')}")
        except Exception as error:
            source_status.append(f"wttr.in: ERROR ({error})")
        try:
            bright_sky_expert = self.weather_expert_blender._bright_sky_expert
            fetch_succeeded = await bright_sky_expert._fetch_forecast(force=force)
            source_status.append(
                f"Bright Sky: {('OK' if fetch_succeeded else 'FAILED')}"
            )
        except Exception as error:
            source_status.append(f"Bright Sky: ERROR ({error})")
        try:
            pirate_weather_expert = self.weather_expert_blender._pirate_weather_expert
            if pirate_weather_expert._enabled:
                fetch_succeeded = await pirate_weather_expert._fetch_forecast(
                    force=force
                )
                source_status.append(
                    f"Pirate Weather: {('OK' if fetch_succeeded else 'FAILED')}"
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
            if self.open_meteo_client:
                open_meteo_forecast = await self.open_meteo_client.get_hourly_forecast(
                    force=force
                )
                if open_meteo_forecast:
                    _LOGGER.debug("Pipeline: Open-Meteo fetch SUCCESS")
                else:
                    _LOGGER.warning(
                        "Pipeline: Open-Meteo fetch FAILED - using cached data"
                    )
            await self._fetch_expert_weather_sources(force=force)
            if self.weather_corrector:
                fetch_succeeded = (
                    await self.weather_corrector.create_corrected_forecast()
                )
                if fetch_succeeded:
                    _LOGGER.info("Pipeline: Corrected forecast updated")
                    return True
                _LOGGER.warning("Pipeline: Corrected forecast creation failed")
                return False
        except Exception as error:
            _LOGGER.error("Pipeline: Weather refresh error: %s", error, exc_info=True)
        return False

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
            _LOGGER.info(
                "Pipeline: Fetching fresh weather data for next day forecast..."
            )
            refresh_succeeded = await self._refresh_all_weather_sources(force=True)
            if refresh_succeeded:
                _LOGGER.info("Pipeline: Fresh weather data fetched successfully")
            else:
                _LOGGER.warning("Pipeline: Weather refresh failed, using cached data")
            if self.weather_corrector:
                fetch_succeeded = (
                    await self.weather_corrector.create_corrected_forecast()
                )
                if fetch_succeeded:
                    _LOGGER.info(
                        "Pipeline: Daily corrected forecast created with fresh data"
                    )
                else:
                    _LOGGER.warning("Pipeline: Daily corrected forecast failed")
        except Exception as error:
            _LOGGER.error(
                "Pipeline: Corrected forecast error: %s", error, exc_info=True
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
                date_str = datetime.now().strftime("%Y-%m-%d")
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
                date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            return await self.weather_expert_learner.learn_from_day(date_str)
        except Exception as error:
            _LOGGER.error("Error triggering expert learning: %s", error)
            return False

    async def update_weather_cache(self, force: bool = False) -> bool:
        "Public API: Refresh all weather sources and create corrected forecast. @zara\n\nThis is the main entry point for services to trigger a full weather refresh.\n\nArgs:\n    force: Force refresh even if data is still fresh\n\nReturns:\n    True if refresh was successful\n"
        return await self._refresh_all_weather_sources(force=force)

    def get_pipeline_status(self) -> dict:
        """Get current pipeline status. @zara"""
        return {
            "running": self._running,
            "listeners_count": len(self._listeners),
            "tasks_count": len(self._background_tasks),
            "components": {
                "open_meteo_client": self.open_meteo_client is not None,
                "weather_actual_tracker": self.weather_actual_tracker is not None,
                "weather_precision": self.weather_precision is not None,
                "weather_corrector": self.weather_corrector is not None,
                "weather_expert_blender": self.weather_expert_blender is not None,
                "weather_expert_learner": self.weather_expert_learner is not None,
            },
        }
