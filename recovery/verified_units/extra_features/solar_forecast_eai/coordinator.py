# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Solar Forecast Energy AI - Main Coordinator.

SolarForecastEAICoordinator class - central update coordinator for heat pump
energy consumption forecasting. Manages weather data, thermodynamic calculations,
ML predictions, and sensor updates. Uses DatabaseManager for all data operations.

@zara
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    UPDATE_INTERVAL,
    VERSION,
    DATA_KEY_FORECAST_TODAY,
)
from .core.core_helpers import SafeDateTimeUtil as dt_util
from .core.core_startup_data_resolver import StartupDataResolver, StartupData
from .data.data_manager import DataManager
from .forecast.forecast_orchestrator import ForecastOrchestrator
from .forecast.forecast_weather import WeatherService
from .forecast.forecast_weather_calculator import WeatherCalculator
from .ai import AIPredictor, BestHourCalculator
from .thermodynamics.thermodynamics_calibrator import ThermodynamicsCalibrator
from .consumption.consumption_history import (
    ProductionCalculator as HistoricalConsumptionCalculator,
)
from .consumption.consumption_scheduled_tasks import ScheduledTasksManager
from .consumption.consumption_tracker import (
    ProductionTimeCalculator as ConsumptionTracker,
)
from .sensors.sensor_data_collector import SensorDataCollector
from .services.service_error_handler import ErrorHandlingService

_LOGGER = logging.getLogger(__name__)


class SolarForecastEAICoordinator(DataUpdateCoordinator):
    """Main coordinator for Solar Forecast Energy AI. @zara

    Manages all data updates and coordinates between components
    for heat pump energy consumption forecasting.
    All data operations use DatabaseManager via DataManager.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, dependencies_ok: bool = False
    ):
        """Initialize coordinator. @zara"""
        update_interval_seconds = entry.options.get(
            CONF_UPDATE_INTERVAL, UPDATE_INTERVAL.total_seconds()
        )
        update_interval = timedelta(seconds=update_interval_seconds)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.entry = entry
        self.dependencies_ok = dependencies_ok
        from .core.core_coordinator_init_helpers import (
            CoordinatorInitHelpers as coordinator_init_helpers,
        )

        config = coordinator_init_helpers.extract_configuration(entry)
        data_directory = coordinator_init_helpers.setup_data_directory(hass)
        self.data_manager = DataManager(
            hass, entry.entry_id, data_directory, str(hass.config.time_zone)
        )
        self.sensor_collector = SensorDataCollector(hass, entry)
        self.panel_group_sensor_reader = None
        self.inverter_max_power = 0.0
        self.heating_capacity_kw = config.heating_capacity_kw
        self.wp_type = config.wp_type
        self.cop_rated = config.cop_rated
        self.learning_enabled = config.learning_enabled
        self.enable_hourly = config.enable_hourly
        self.wp_power_entity = self.sensor_collector.strip_entity_id(
            config.wp_power_entity
        )
        self.wp_energy_today = self.sensor_collector.strip_entity_id(
            config.wp_energy_today
        )
        self.primary_weather_entity = self.sensor_collector.strip_entity_id(
            config.primary_weather_entity
        )
        self.current_weather_entity = self.primary_weather_entity
        self.weather_calculator = WeatherCalculator()
        self.historical_calculator = HistoricalConsumptionCalculator(
            hass, self.data_manager._db_manager
        )
        self.consumption_tracker = ConsumptionTracker(
            hass=hass,
            power_entity=self.wp_power_entity,
            db_manager=self.data_manager._db_manager,
            coordinator=self,
        )
        self.best_hour_calculator = BestHourCalculator(
            db_manager=self.data_manager._db_manager
        )
        self.forecast_orchestrator = ForecastOrchestrator(
            hass=hass,
            data_manager=self.data_manager,
            solar_capacity=self.heating_capacity_kw,
            weather_calculator=self.weather_calculator,
            db_manager=self.data_manager._db_manager,
            wp_type=self.wp_type,
            cop_rated=self.cop_rated,
            config_entry=self.entry,
        )
        self.scheduled_tasks = ScheduledTasksManager(
            hass=hass,
            coordinator=self,
            wp_energy_today_entity_id=self.wp_energy_today,
            db_manager=self.data_manager._db_manager,
        )
        self.error_handler = ErrorHandlingService()
        self.weather_service = None
        self.ai_predictor = None
        self._services_initialized = False
        self._ml_ready = False
        from .data.data_weather_pipeline_manager import (
            WeatherDataPipelineManager as weather_pipeline_type,
        )

        self.weather_pipeline_manager = None
        self.weather_fallback_active = False
        self._last_weather_update = None
        self._forecast_cache = {}
        self._startup_time = dt_util.now()
        self._last_update_success_time = None
        self._startup_sensors_ready = False
        self._hourly_predictions_cache = None
        self._eai_hourly_weather_cache = {"generated_at": None, "hourly": []}
        self.next_hour_pred = 0.0
        self.peak_consumption_hour = "Calculating..."
        self.heating_time_today = "Initializing..."
        self.last_day_error_kwh = None
        self.yesterday_accuracy = None
        self.avg_month_consumption = 0.0
        self.last_successful_learning = None
        self.model_accuracy = None
        self.learned_correction_factor = 1.0
        self.expected_daily_consumption = None
        self._last_statistics_calculation = None
        self.eod_duration_seconds = None
        self.cloudiness_trend_1h = 0.0
        self.cloudiness_trend_3h = 0.0
        self.cloudiness_volatility = 0.0
        self._training_ready_count = 0
        self._recovery_lock = asyncio.Lock()
        self._recovery_in_progress = False
        self._services_init_lock = asyncio.Lock()
        self._forecast_orchestrator_initialized = False
        self.system_status_sensor = None
        self.thermodynamics_calibrator = None
        self.drift_monitor = None
        self._drift_status_cache = {}
        self.learning_paused = False
        self._unsub_power_peak_listener = None
        self._unsub_weekly_retraining_listener = None
        self._unsub_startup_listener = None
        self._background_tasks = set()
        self._shutdown_started = False
        self._startup_data_resolver = None
        self._startup_data = None
        _LOGGER.debug(f"SolarForecastEAICoordinator V{VERSION} initialized")

    def _create_background_task(
        self, coroutine: Any, *, name: str
    ) -> Optional[asyncio.Task[Any]]:
        if self._shutdown_started:
            if asyncio.iscoroutine(coroutine):
                coroutine.close()
            return
        task = self.hass.async_create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _load_persistent_state(self) -> None:
        """Load persistent coordinator state from database. @zara"""
        try:
            expected_daily_consumption = (
                await self.data_manager.load_expected_daily_production()
            )
            if expected_daily_consumption is not None:
                self.expected_daily_consumption = expected_daily_consumption
        except Exception as coordinator_error:
            _LOGGER.warning(
                f"Failed to load persistent coordinator state: {coordinator_error}"
            )
        return None if True else None

    async def _initialize_services(self) -> bool:
        """Initialize all services (weather, ML, error handler). @zara"""
        try:
            if self.learning_enabled and self.dependencies_ok:
                try:
                    notification_service = self.hass.data.get(DOMAIN, {}).get(
                        "notification_service"
                    )
                    self.ai_predictor = AIPredictor(
                        hass=self.hass,
                        db_manager=self.data_manager._db_manager,
                        error_handler=self.error_handler,
                        notification_service=notification_service,
                        config_entry=self.entry,
                        panel_groups=None,
                        solar_capacity=self.heating_capacity_kw,
                    )
                    self.ai_predictor.set_entities(
                        solar_capacity=self.heating_capacity_kw,
                        power_entity=self.wp_power_entity,
                        weather_entity=self.current_weather_entity,
                    )
                    predictor_initialized = await self.ai_predictor.initialize()
                    if predictor_initialized:
                        self._ml_ready = True
                        self.best_hour_calculator.ai_predictor = self.ai_predictor
                    else:
                        _LOGGER.error("AIPredictor initialization failed")
                        self.ai_predictor = None
                except Exception as coordinator_error:
                    _LOGGER.error(
                        f"Failed to initialize AIPredictor: {coordinator_error}"
                    )
                    self.ai_predictor = None
            try:
                self.thermodynamics_calibrator = ThermodynamicsCalibrator(
                    db_manager=self.data_manager._db_manager
                )
                await self.thermodynamics_calibrator.load_from_database()
                _LOGGER.info(
                    "ThermodynamicsCalibrator initialized for self-learning corrections"
                )
            except Exception as coordinator_error:
                _LOGGER.warning(
                    f"Failed to initialize ThermodynamicsCalibrator: {coordinator_error}"
                )
                self.thermodynamics_calibrator = None
            self._services_initialized = True
            return True
        except Exception as coordinator_error:
            _LOGGER.error(f"Failed to initialize services: {coordinator_error}")
            return False

    def _schedule_daily_ratio_migration(self) -> None:
        """Relearn day ratios once, without holding startup.

        The task is stored so it is not garbage-collected. A second start
        returns as soon as the marker is present.
        """
        task = asyncio.create_task(self._run_daily_ratio_migration())
        self._daily_ratio_migration_task = task

    async def _run_daily_ratio_migration(self) -> None:
        try:
            migration_result = (
                await self.scheduled_tasks.migrate_daily_consumption_model()
            )
            _LOGGER.info(
                "Daily consumption migration %s in %.3fs",
                migration_result.get("reason"),
                float(migration_result.get("elapsed_s") or 0.0),
            )
        except Exception as migration_error:
            _LOGGER.warning("Daily consumption migration skipped: %s", migration_error)
        return None if True else None

    async def _initialize_forecast_orchestrator(self) -> None:
        """Initialize the forecast orchestrator strategies. @zara"""
        self.forecast_orchestrator.initialize_strategies(
            ai_predictor=self.ai_predictor, error_handler=self.error_handler
        )
        if self.thermodynamics_calibrator:
            try:
                rule_based_strategy = self.forecast_orchestrator.rule_based_strategy
                if rule_based_strategy:
                    rule_based_strategy.set_thermodynamics_calibrator(
                        self.thermodynamics_calibrator
                    )
                    _LOGGER.info(
                        "ThermodynamicsCalibrator passed to RuleBasedStrategy - self-learning thermodynamic corrections enabled"
                    )
            except Exception as coordinator_error:
                _LOGGER.warning(
                    f"Could not connect ThermodynamicsCalibrator: {coordinator_error}"
                )
            else:
                self._schedule_daily_ratio_migration()
        if self.weather_pipeline_manager:
            try:
                rule_based_strategy = self.forecast_orchestrator.rule_based_strategy
                weather_actual_tracker = getattr(
                    self.weather_pipeline_manager, "weather_actual_tracker", None
                )
                if rule_based_strategy and weather_actual_tracker:
                    rule_based_strategy.set_weather_actual_tracker(
                        weather_actual_tracker
                    )
                    _LOGGER.info("WeatherActualTracker passed to RuleBasedStrategy")
            except Exception as coordinator_error:
                _LOGGER.warning(
                    f"Could not connect WeatherActualTracker: {coordinator_error}"
                )
        try:
            from .ai.ai_drift_monitor import DriftMonitor as drift_monitor_type

            database_manager = self.data_manager._db_manager
            self.drift_monitor = drift_monitor_type(database_manager, [])
            _LOGGER.info("DriftMonitor initialized")
            try:
                drift_status = await self.drift_monitor.get_status()
                if drift_status:
                    self._drift_status_cache = drift_status
                    _LOGGER.debug(
                        "Drift status cache loaded from DB: %s",
                        drift_status.get("state"),
                    )
            except Exception as coordinator_error:
                _LOGGER.debug(
                    "Could not pre-populate drift status cache: %s", coordinator_error
                )
        except Exception as coordinator_error:
            _LOGGER.warning(f"Could not initialize DriftMonitor: {coordinator_error}")
        return None if True else None

    async def async_setup(self) -> bool:
        """Setup coordinator and start tracking. @zara"""
        try:
            data_initialized = await self.data_manager.initialize()
            if not data_initialized:
                _LOGGER.error("Failed to initialize data manager")
                return False
            self.historical_calculator.db = self.data_manager._db_manager
            self.consumption_tracker.db = self.data_manager._db_manager
            self.best_hour_calculator.db_manager = self.data_manager._db_manager
            self.forecast_orchestrator._db = self.data_manager._db_manager
            self.scheduled_tasks.db = self.data_manager._db_manager
            self.scheduled_tasks.morning_routine_handler.db = (
                self.data_manager._db_manager
            )
            self.scheduled_tasks.adaptive_forecast_engine.db = (
                self.data_manager._db_manager
            )
            self.scheduled_tasks.daily_summaries_handler.db = (
                self.data_manager._db_manager
            )
            await self._load_persistent_state()
            self._startup_data_resolver = StartupDataResolver(
                db_manager=self.data_manager._db_manager,
                sensor_collector=self.sensor_collector,
                on_sensors_available=self._on_sensors_available_callback,
            )
            _LOGGER.info("Resolving startup data from DB (non-blocking)...")
            self._startup_data = (
                await self._startup_data_resolver.resolve_startup_data()
            )
            if self._startup_data.is_usable:
                _LOGGER.info(
                    "Startup data available: source=%s, weather=%s, sensors=%s",
                    self._startup_data.source,
                    self._startup_data.weather_available,
                    self._startup_data.sensors_available,
                )
            else:
                for startup_warning in self._startup_data.warnings:
                    _LOGGER.warning("Startup: %s", startup_warning)
            _LOGGER.info(
                "Forecast Energy AI Coordinator basic setup complete - starting background initialization"
            )
            self._create_background_task(
                self._background_initialization(),
                name="solar_forecast_eai_background_init",
            )
        except Exception as coordinator_error:
            _LOGGER.error(f"Failed to setup coordinator: {coordinator_error}")
            return False
        return True

    def _on_sensors_available_callback(self, sensor_data: Dict[str, Any]) -> None:
        """Callback when external sensors become available. @zara"""
        _LOGGER.info("External sensors now available - triggering data refresh")
        if self._startup_data:
            self._startup_data.sensors_available = True
            self._startup_data.sensor_data = sensor_data
        self._create_background_task(
            self.async_request_refresh(), name="solar_forecast_eai_sensor_refresh"
        )

    async def _background_initialization(self) -> None:
        """Initialize heavy components in background without blocking HA startup. @zara"""
        try:
            _LOGGER.info("Background initialization started")
            try:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        services_ready = await self._initialize_services()
                        if not services_ready:
                            _LOGGER.warning(
                                "Services initialization failed - will use rule-based forecasting"
                            )
            except Exception as error:
                _LOGGER.warning(f"Failed to initialize services: {error}")
            try:
                from .data.data_weather_pipeline_manager import (
                    WeatherDataPipelineManager,
                )

                self.weather_pipeline_manager = WeatherDataPipelineManager(
                    hass=self.hass,
                    db_manager=self.data_manager._db_manager,
                    config_entry=self.entry,
                    coordinator=self,
                )
                pipeline_ready = await self.weather_pipeline_manager.async_setup()
                if not pipeline_ready:
                    _LOGGER.warning(
                        "Weather Data Pipeline setup failed - will retry on next update"
                    )
                else:
                    if hasattr(self.weather_pipeline_manager, "weather_corrector"):
                        self.scheduled_tasks.set_pipeline_corrector(
                            self.weather_pipeline_manager.weather_corrector
                        )
                    self.weather_service = WeatherService(
                        hass=self.hass,
                        latitude=self.hass.config.latitude,
                        longitude=self.hass.config.longitude,
                        data_dir=self.data_manager.data_dir,
                        db_manager=self.data_manager._db_manager,
                        data_manager=self.data_manager,
                        error_handler=self.error_handler,
                    )
                    await self.weather_service.initialize()

                    async def _start_pipeline_background():
                        try:
                            started = (
                                await self.weather_pipeline_manager.start_pipeline()
                            )
                            if not started:
                                _LOGGER.warning(
                                    "Weather Data Pipeline failed to start - will retry on next update"
                                )
                            else:
                                _LOGGER.info(
                                    "Weather Data Pipeline started successfully"
                                )
                        except Exception as error:
                            _LOGGER.warning(
                                f"Weather Data Pipeline start failed: {error} - will retry on next update"
                            )
                        return None if True else None

                    self._create_background_task(
                        _start_pipeline_background(),
                        name="solar_forecast_eai_pipeline_start",
                    )
            except Exception as error:
                _LOGGER.warning(f"Weather pipeline initialization failed: {error}")
            try:
                await self._schedule_delayed_sensor_init()
            except Exception as error:
                _LOGGER.warning(f"Delayed sensor init failed: {error}")
            try:
                await self._setup_power_peak_tracking()
            except Exception as error:
                _LOGGER.warning(f"Power peak tracking setup failed: {error}")
            try:
                await self.scheduled_tasks.schedule_all_tasks()
            except Exception as error:
                _LOGGER.warning(f"Task scheduling failed: {error}")
            try:
                now = (dt_util.now if True else None)()
                today_iso = now.date().isoformat()
                tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
                day_after_iso = (now.date() + timedelta(days=2)).isoformat()
                today_predictions = await self.data_manager.get_hourly_predictions(
                    today_iso
                )
                tomorrow_predictions = await self.data_manager.get_hourly_predictions(
                    tomorrow_iso
                )
                day_after_predictions = await self.data_manager.get_hourly_predictions(
                    day_after_iso
                )
                peak_prediction = None
                if today_predictions:
                    positive_predictions = [
                        prediction
                        for prediction in today_predictions
                        if (prediction.get("prediction_kwh") or 0) > 0
                    ]
                    if positive_predictions:
                        peak_prediction = max(
                            positive_predictions,
                            key=lambda p: p.get("prediction_kwh", 0),
                        )
                self._hourly_predictions_cache = {
                    "predictions": today_predictions if today_predictions else [],
                    "predictions_tomorrow": (
                        tomorrow_predictions if tomorrow_predictions else []
                    ),
                    "predictions_day_after": (
                        day_after_predictions if day_after_predictions else []
                    ),
                    "peak_hour_today": (
                        {
                            "hour": peak_prediction.get("target_hour"),
                            "kwh": peak_prediction.get("prediction_kwh"),
                        }
                        if peak_prediction
                        else {}
                    ),
                }
            except Exception as error:
                _LOGGER.debug(f"Could not load hourly predictions cache: {error}")
                self._hourly_predictions_cache = {
                    "predictions": [],
                    "predictions_tomorrow": [],
                    "predictions_day_after": [],
                    "peak_hour_today": {},
                }
            if self.ai_predictor:

                @callback
                def _scheduled_weekly_retraining(now: datetime) -> None:
                    """Weekly model retraining - Sundays only. @zara"""
                    if now.weekday() == 6:
                        self._create_background_task(
                            self.ai_predictor.train_model(),
                            name="solar_forecast_eai_weekly_retraining",
                        )
                    return None if True else None

                self._unsub_weekly_retraining_listener = async_track_time_change(
                    self.hass, _scheduled_weekly_retraining, hour=3, minute=0, second=0
                )
            forecast_mode = "AI-Ready" if self._ml_ready else "Rule-Based"
            _LOGGER.info(
                f"Forecast Energy AI Coordinator fully initialized ({forecast_mode}, {self.heating_capacity_kw} kW)"
            )
        except Exception as error:
            _LOGGER.error(f"Background initialization failed: {error}", exc_info=True)
        return None if True else None

    async def async_shutdown(self) -> None:
        """Cleanup coordinator resources. @zara

        Removes all event listeners to prevent accumulation on reload.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        try:
            for unsubscribe_attribute, listener_description in (
                ("_unsub_startup_listener", "startup"),
                ("_unsub_power_peak_listener", "power peak tracking"),
                ("_unsub_weekly_retraining_listener", "weekly retraining"),
            ):
                unsubscribe = getattr(self, unsubscribe_attribute)
                if unsubscribe is None:
                    continue
                setattr(self, unsubscribe_attribute, None)
                try:
                    unsubscribe()
                    _LOGGER.debug(
                        "%s listener removed", listener_description.capitalize()
                    )
                except Exception as coordinator_error:
                    _LOGGER.warning(
                        "Error removing %s listener: %s",
                        listener_description,
                        coordinator_error,
                    )
            await super().async_shutdown()
            current_task = asyncio.current_task()
            pending_tasks = [
                task
                for task in self._background_tasks
                if task is not current_task
                if not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            self._background_tasks.clear()
            if self._startup_data_resolver:
                await self._startup_data_resolver.shutdown()
            if (
                hasattr(self, "weather_pipeline_manager")
                and self.weather_pipeline_manager
            ):
                await self.weather_pipeline_manager.stop_pipeline()
            if hasattr(self, "consumption_tracker"):
                await self.consumption_tracker.stop_tracking()
            if hasattr(self, "scheduled_tasks"):
                await self.scheduled_tasks.cancel_all_tasks()
            if self.data_manager:
                await self.data_manager.cleanup()
        except Exception as coordinator_error:
            _LOGGER.error(f"Error during coordinator shutdown: {coordinator_error}")
        return None if True else None

    async def _schedule_delayed_sensor_init(self) -> None:
        """Schedule sensor initialization after Home Assistant is fully started. @zara"""

        async def _delayed_init(event=None):
            """Run sensor initialization after HA is fully started."""
            _LOGGER.info(
                "[SENSOR_INIT] Home Assistant fully started - beginning sensor initialization"
            )
            await asyncio.sleep(2)
            await self._start_consumption_tracking_safe()
            await self.async_request_refresh()
            _LOGGER.info("[SENSOR_INIT] Delayed sensor initialization completed")

        @callback
        def _handle_homeassistant_started(event=None) -> None:
            """Move one-shot event work into a coordinator-owned task."""
            self._unsub_startup_listener = None
            self._create_background_task(
                _delayed_init(event), name="solar_forecast_eai_delayed_sensor_init"
            )

        if self.hass.is_running:
            _LOGGER.info(
                "[SENSOR_INIT] HA already running - starting sensor initialization immediately"
            )
            self._create_background_task(
                _delayed_init(), name="solar_forecast_eai_delayed_sensor_init"
            )
        else:
            _LOGGER.info(
                "[SENSOR_INIT] HA still starting - scheduling sensor initialization for after startup"
            )
            self._unsub_startup_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _handle_homeassistant_started
            )
        return None if True else None

    async def _start_consumption_tracking_safe(self) -> None:
        """Start consumption tracking with error handling. @zara"""
        try:
            await self.consumption_tracker.start_tracking()
        except Exception as tracking_error:
            _LOGGER.error(
                f"Failed to start consumption time tracking: {tracking_error}"
            )
        return None if True else None

    async def _setup_power_peak_tracking(self) -> None:
        """Setup event listener for power peak tracking. @zara"""
        if not self.wp_power_entity:
            return
        self._peak_power_today = 0.0
        self._peak_time_today = None
        try:
            peak_date = dt_util.now().date().isoformat()
            database_row = await self.data_manager._db_manager.fetchone(
                "SELECT peak_power_w, peak_power_time\n                   FROM production_time_state\n                   WHERE id = 1 AND date = ?",
                (peak_date,),
            )
            if database_row and database_row[0]:
                self._peak_power_today = float(database_row[0])
                self._peak_time_today = (
                    str(database_row[1]) if database_row[1] else None
                )
        except Exception as coordinator_error:
            _LOGGER.debug(f"Could not load today's peak: {coordinator_error}")
        last_peak_persisted_at = None

        async def power_state_changed(event):
            nonlocal last_peak_persisted_at
            new_state = event.data.get("new_state")
            if not new_state or new_state.state in (None, "unavailable", "unknown"):
                return
            try:
                power_w = float(new_state.state)
                if power_w > self._peak_power_today:
                    self._peak_power_today = power_w
                    now = dt_util.now()
                    self._peak_time_today = now.strftime("%H:%M")
                    if (
                        last_peak_persisted_at is None
                        or (now - last_peak_persisted_at).total_seconds() > 60
                    ):
                        peak_date = now.date().isoformat()
                        peak_time = now.strftime("%H:%M")
                        await self.data_manager._db_manager.execute(
                            "INSERT INTO production_time_state\n                               (id, date, peak_power_w, peak_power_time, last_updated)\n                               VALUES (1, ?, ?, ?, ?)\n                               ON CONFLICT(id) DO UPDATE SET\n                                   peak_power_w = excluded.peak_power_w,\n                                   peak_power_time = excluded.peak_power_time,\n                                   date = excluded.date,\n                                   last_updated = excluded.last_updated",
                            (peak_date, power_w, peak_time, now.isoformat()),
                        )
                        all_time_peak_w = await self.data_manager.get_all_time_peak()
                        if all_time_peak_w is None or power_w > all_time_peak_w:
                            await self.data_manager._db_manager.execute(
                                "UPDATE production_time_state\n                                   SET peak_record_w = ?, peak_record_date = ?, peak_record_time = ?\n                                   WHERE id = 1",
                                (power_w, peak_date, peak_time),
                            )
                            _LOGGER.info(
                                "New all-time peak consumption: %.1f W at %s",
                                power_w,
                                peak_time,
                            )
                        last_peak_persisted_at = now
            except (ValueError, TypeError):
                pass
            return None if True else None

        self._unsub_power_peak_listener = async_track_state_change_event(
            self.hass, [self.wp_power_entity], power_state_changed
        )

    async def _async_update_data(self):
        """Fetch data from API endpoint. @zara"""
        try:
            if not self._services_initialized:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        services_initialized = await self._initialize_services()
                        if not services_initialized:
                            raise UpdateFailed("Failed to initialize services")
            if not self._forecast_orchestrator_initialized:
                await self._initialize_forecast_orchestrator()
                self._forecast_orchestrator_initialized = True
            from .core.core_coordinator_update_helpers import (
                CoordinatorUpdateHelpers as coordinator_update_helpers,
            )

            update_helpers = coordinator_update_helpers(
                self, self.data_manager._db_manager
            )
            await update_helpers.handle_startup_recovery()
            current_weather, hourly_forecast = await update_helpers.fetch_weather_data()
            hourly_weather_points = [
                dict(weather_point)
                for weather_point in (
                    hourly_forecast if isinstance(hourly_forecast, list) else []
                )
                if isinstance(weather_point, dict)
            ]
            issued_times = []
            for weather_point in hourly_weather_points:
                issued_at_value = weather_point.get("_issued_at")
                if isinstance(issued_at_value, datetime):
                    issued_at_datetime = issued_at_value
                elif isinstance(issued_at_value, str):
                    try:
                        issued_at_datetime = datetime.fromisoformat(
                            issued_at_value.replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                else:
                    continue
                if issued_at_datetime.tzinfo is None:
                    issued_at_datetime = issued_at_datetime.replace(tzinfo=timezone.utc)
                issued_times.append(issued_at_datetime.astimezone(timezone.utc))
            self._eai_hourly_weather_cache = {
                "generated_at": min(issued_times) if issued_times else None,
                "hourly": hourly_weather_points,
            }
            external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
            forecast_result = await update_helpers.generate_forecast(
                current_weather, hourly_forecast, external_sensors
            )
            coordinator_result = await update_helpers.build_coordinator_result(
                forecast_result, current_weather, external_sensors
            )
            await self._update_sensor_properties(coordinator_result)
            await update_helpers.save_forecasts(
                forecast_result, forecast_result.get("hourly", [])
            )
            await self._refresh_hourly_predictions_cache()
            self._last_update_success_time = dt_util.now()
            if not self._startup_sensors_ready:
                self._startup_sensors_ready = True
            return coordinator_result
        except UpdateFailed:
            raise
        except Exception as update_error:
            _LOGGER.error(f"Unexpected error updating data: {update_error}")
            raise UpdateFailed(f"Error communicating with API: {update_error}")

    async def _update_sensor_properties(self, data: Dict[str, Any]) -> None:
        """Update coordinator properties used by sensors. @zara"""
        try:
            if data.get("hourly_forecast"):
                hourly_forecast = data["hourly_forecast"]
                next_hour_forecast = (
                    hourly_forecast[0] if len(hourly_forecast) > 0 else {}
                )
                self.next_hour_pred = next_hour_forecast.get("production_kwh", 0.0)
            else:
                self.next_hour_pred = 0.0
            historical_calculator = self.historical_calculator
            peak_consumption_time = (
                await historical_calculator.async_get_peak_production_time()
            )
            self.peak_consumption_hour = (
                peak_consumption_time if peak_consumption_time else "Calculating..."
            )
            self.heating_time_today = self.consumption_tracker.get_production_time()
        except (ValueError, TypeError, AttributeError) as coordinator_error:
            _LOGGER.debug(f"Could not update sensor properties: {coordinator_error}")
        ai_predictor = self.ai_predictor
        if ai_predictor:
            self.last_successful_learning = getattr(
                ai_predictor, "last_training_time", None
            )
            if self.model_accuracy is None:
                self.model_accuracy = getattr(ai_predictor, "current_accuracy", None)
            try:
                training_ready_count = await self._get_training_ready_count()
                self._training_ready_count = training_ready_count
            except Exception:
                self._training_ready_count = 0
        else:
            self.last_successful_learning = None
            self._training_ready_count = 0
        try:
            now = dt_util.now()
            current_hour = now.hour
            cloud_rows = await self.data_manager._db_manager.fetchall(
                "SELECT hour, clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND clouds IS NOT NULL\n                   ORDER BY hour",
                (now.date().isoformat(),),
            )
            if cloud_rows and len(cloud_rows) >= 2:
                clouds_by_hour = {
                    int(cloud_row[0]): float(cloud_row[1]) for cloud_row in cloud_rows
                }
                current_cloud_cover = clouds_by_hour.get(current_hour)
                cloud_cover_1h_ago = clouds_by_hour.get(current_hour - 1)
                cloud_cover_3h_ago = clouds_by_hour.get(current_hour - 3)
                if current_cloud_cover is not None and cloud_cover_1h_ago is not None:
                    self.cloudiness_trend_1h = current_cloud_cover - cloud_cover_1h_ago
                else:
                    self.cloudiness_trend_1h = 0.0
                if current_cloud_cover is not None and cloud_cover_3h_ago is not None:
                    self.cloudiness_trend_3h = current_cloud_cover - cloud_cover_3h_ago
                else:
                    self.cloudiness_trend_3h = 0.0
                recent_cloud_cover = [
                    clouds_by_hour[recent_hour]
                    for recent_hour in range(max(0, current_hour - 5), current_hour + 1)
                    if recent_hour in clouds_by_hour
                ]
                if len(recent_cloud_cover) >= 3:
                    mean_cloud_cover = sum(recent_cloud_cover) / len(recent_cloud_cover)
                    cloud_cover_variance = sum(
                        (
                            (cloud_cover - mean_cloud_cover) ** 2
                            for cloud_cover in recent_cloud_cover
                        )
                    ) / len(recent_cloud_cover)
                    self.cloudiness_volatility = round(cloud_cover_variance**0.5, 1)
                else:
                    self.cloudiness_volatility = 0.0
            else:
                self.cloudiness_trend_1h = 0.0
                self.cloudiness_trend_3h = 0.0
                self.cloudiness_volatility = 0.0
        except Exception as coordinator_error:
            _LOGGER.debug(f"Could not compute cloudiness trends: {coordinator_error}")
            self.cloudiness_trend_1h = 0.0
            self.cloudiness_trend_3h = 0.0
            self.cloudiness_volatility = 0.0
        if self.last_day_error_kwh is None:
            try:
                yesterday_date = (dt_util.now().date() - timedelta(days=1)).isoformat()
                database_row = await self.data_manager._db_manager.fetchone(
                    "SELECT predicted_kwh, actual_kwh FROM forecast_history WHERE date = ?",
                    (yesterday_date,),
                )
                if (
                    database_row
                    and database_row[0] is not None
                    and (database_row[1] is not None)
                ):
                    predicted_kwh = float(database_row[0] or 0.0)
                    actual_kwh = float(database_row[1] or 0.0)
                    absolute_error_kwh = abs(predicted_kwh - actual_kwh)
                    self.last_day_error_kwh = round(absolute_error_kwh, 2)
                    if actual_kwh > 0.1:
                        accuracy_ratio = max(0.0, 1.0 - absolute_error_kwh / actual_kwh)
                        self.yesterday_accuracy = round(accuracy_ratio * 100.0, 1)
                    elif predicted_kwh < 0.1:
                        self.yesterday_accuracy = 100.0
                    else:
                        self.yesterday_accuracy = 0.0
            except Exception as coordinator_error:
                _LOGGER.debug(
                    f"Could not load yesterday deviation: {coordinator_error}"
                )
        try:
            month_start_date = dt_util.now().date().replace(day=1).isoformat()
            database_row = await self.data_manager._db_manager.fetchone(
                "SELECT AVG(actual_total_kwh) FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (month_start_date,),
            )
            if database_row and database_row[0] is not None:
                self.avg_month_consumption = round(float(database_row[0]), 2)
        except Exception as coordinator_error:
            _LOGGER.debug(f"Could not load avg_month_consumption: {coordinator_error}")
        try:
            database_row = await self.data_manager._db_manager.fetchone(
                "SELECT eod_duration_seconds FROM daily_summaries\n                   WHERE eod_duration_seconds IS NOT NULL\n                   ORDER BY date DESC LIMIT 1"
            )
            if database_row and database_row[0] is not None:
                self.eod_duration_seconds = round(float(database_row[0]), 1)
        except Exception as coordinator_error:
            _LOGGER.debug(f"Could not load eod_duration: {coordinator_error}")
        return None if True else None

    async def _refresh_hourly_predictions_cache(self) -> None:
        """Refresh hourly predictions cache from database. @zara"""
        try:
            now = dt_util.now()
            today_date = now.date().isoformat()
            tomorrow_date = (now.date() + timedelta(days=1)).isoformat()
            day_after_date = (now.date() + timedelta(days=2)).isoformat()
            today_predictions = await self.data_manager.get_hourly_predictions(
                today_date
            )
            tomorrow_predictions = await self.data_manager.get_hourly_predictions(
                tomorrow_date
            )
            day_after_predictions = await self.data_manager.get_hourly_predictions(
                day_after_date
            )
            peak_prediction = None
            if today_predictions:
                positive_predictions = [
                    prediction
                    for prediction in today_predictions
                    if (prediction.get("prediction_kwh") or 0) > 0
                ]
                if positive_predictions:
                    peak_prediction = max(
                        positive_predictions, key=lambda p: p.get("prediction_kwh", 0)
                    )
            self._hourly_predictions_cache = {
                "predictions": today_predictions if today_predictions else [],
                "predictions_tomorrow": (
                    tomorrow_predictions if tomorrow_predictions else []
                ),
                "predictions_day_after": (
                    day_after_predictions if day_after_predictions else []
                ),
                "peak_hour_today": (
                    {
                        "hour": peak_prediction.get("target_hour"),
                        "kwh": peak_prediction.get("prediction_kwh"),
                    }
                    if peak_prediction
                    else {}
                ),
            }
            _LOGGER.debug(
                "Hourly predictions cache refreshed: %d today, %d tomorrow, %d day_after",
                len(today_predictions) if today_predictions else 0,
                len(tomorrow_predictions) if tomorrow_predictions else 0,
                len(day_after_predictions) if day_after_predictions else 0,
            )
        except Exception as coordinator_error:
            _LOGGER.warning(
                f"Could not refresh hourly predictions cache: {coordinator_error}"
            )
        return None if True else None

    @property
    def last_update_success_time(self) -> Optional[datetime]:
        """Return last successful update time. @zara"""
        return self._last_update_success_time

    @property
    def weather_source(self) -> str:
        """Return weather source. @zara"""
        return self.current_weather_entity or "Open-Meteo"

    @property
    def diagnostic_status(self) -> str:
        """Return diagnostic status string. @zara"""
        if not self._startup_sensors_ready:
            return "Initializing (Waiting for sensors)"
        if not self.last_update_success and self._last_update_success_time is None:
            return "Error Initializing"
        if not self.last_update_success:
            return "Update Failed"
        weather_healthy = False
        if self.weather_service:
            try:
                weather_healthy = self.weather_service.get_health_status().get(
                    "healthy", False
                )
            except Exception as coordinator_error:
                _LOGGER.debug(
                    f"Could not get weather service health status: {coordinator_error}"
                )
        update_recent = True
        if self._last_update_success_time:
            update_age_seconds = (
                dt_util.now() - self._last_update_success_time
            ).total_seconds()
            if update_age_seconds > self.update_interval.total_seconds() * 2:
                update_recent = False
        else:
            update_recent = False
        ml_ready = self._ml_ready
        if ml_ready and weather_healthy and update_recent:
            return "Optimal (ML Active)"
        if weather_healthy and update_recent:
            ml_status = (
                "ML Disabled/Unavailable" if not self.ai_predictor else "ML Not Ready"
            )
            return f"Degraded ({ml_status})"
        if not weather_healthy:
            return "Error (Weather Unavailable)"
        if not update_recent:
            return "Stale (No Recent Update)"
        return "Initializing"

    def on_ai_training_complete(
        self, timestamp: datetime, accuracy: Optional[float] = None
    ) -> None:
        """Callback when AI training completes. @zara"""
        _LOGGER.info(
            f"Coordinator notified of AI Training completion at {timestamp}. Accuracy: {accuracy}"
        )
        self.last_successful_learning = timestamp
        if accuracy is not None:
            self.model_accuracy = accuracy
        self.async_update_listeners()
        if accuracy is not None:
            training_samples = (
                self.ai_predictor.training_samples if self.ai_predictor else 0
            )
            self.update_system_status(
                event_type="ai_training",
                event_status="success",
                event_summary=f"AI Training successful - Accuracy: {accuracy * 100:.1f}%",
                event_details={
                    "accuracy_percent": round(accuracy * 100, 1),
                    "samples_used": training_samples,
                    "training_time": timestamp.isoformat(),
                },
            )
        else:
            self.update_system_status(
                event_type="ai_training",
                event_status="failed",
                event_summary="AI Training failed",
                event_details={},
            )
        return None if True else None

    async def set_expected_daily_consumption(self) -> None:
        """Set expected daily consumption at 6 AM and save to database. @zara"""
        try:
            if (
                self.data
                and DATA_KEY_FORECAST_TODAY in self.data
                and (self.data.get(DATA_KEY_FORECAST_TODAY) is not None)
            ):
                self.expected_daily_consumption = self.data.get(DATA_KEY_FORECAST_TODAY)
            else:
                await self.async_request_refresh()
                for refresh_attempt in range(10):
                    if (
                        self.data
                        and DATA_KEY_FORECAST_TODAY in self.data
                        and (self.data.get(DATA_KEY_FORECAST_TODAY) is not None)
                    ):
                        break
                    await asyncio.sleep(1.0)
                if (
                    self.data
                    and DATA_KEY_FORECAST_TODAY in self.data
                    and (self.data.get(DATA_KEY_FORECAST_TODAY) is not None)
                ):
                    self.expected_daily_consumption = self.data.get(
                        DATA_KEY_FORECAST_TODAY
                    )
                else:
                    self.expected_daily_consumption = None
            if self.expected_daily_consumption is not None:
                await self.data_manager.save_expected_daily_production(
                    self.expected_daily_consumption
                )
                forecast_saved = await self.data_manager.save_daily_forecast(
                    prediction_kwh=self.expected_daily_consumption,
                    source="auto_6am",
                    force_overwrite=False,
                )
                if not forecast_saved:
                    _LOGGER.error("CRITICAL: daily_forecast NOT saved to database!")
                self.async_update_listeners()
        except Exception as update_error:
            _LOGGER.error(f"Failed to set expected daily consumption: {update_error}")
            self.expected_daily_consumption = None
        return None if True else None

    async def reset_expected_daily_consumption(self) -> None:
        """Reset expected daily consumption at midnight. @zara"""
        self.expected_daily_consumption = None
        await self.data_manager.clear_expected_daily_production()
        self.async_update_listeners()

    async def _recovery_forecast_process(self, source: str) -> bool:
        """Fallback process for missing forecasts. @zara"""
        async with self._recovery_lock:
            if self._recovery_in_progress:
                return False
            self._recovery_in_progress = True
            try:
                return await self._execute_recovery(source)
            finally:
                self._recovery_in_progress = False
        return None if True else None

    async def _execute_recovery(self, source: str) -> bool:
        """Internal method to execute the recovery process. @zara"""
        if not self.weather_service:
            _LOGGER.error("Weather service not initialized for recovery")
            return False
        current_day_forecast = await self.data_manager.get_current_day_forecast()
        if current_day_forecast and current_day_forecast.get("locked"):
            return True
        try:
            hourly_forecast = await self.weather_service.get_hourly_forecast()
            if hourly_forecast:
                external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
                forecast_result = await self.forecast_orchestrator.orchestrate_forecast(
                    current_weather=None,
                    hourly_forecast=hourly_forecast,
                    external_sensors=external_sensors,
                    correction_factor=self.learned_correction_factor,
                )
                if forecast_result:
                    if forecast_result.get("today") is not None:
                        forecast_saved = await self.data_manager.save_daily_forecast(
                            prediction_kwh=forecast_result["today"],
                            source=f"fallback_open_meteo_{source}",
                            force_overwrite=True,
                        )
                        if forecast_saved:
                            await self.data_manager.save_expected_daily_production(
                                round(forecast_result["today"], 2)
                            )
                            self.expected_daily_consumption = round(
                                forecast_result["today"], 2
                            )
                            _LOGGER.info(
                                f"Recovery: expected_daily_consumption set to {round(forecast_result['today'], 2)} kWh (open_meteo_{source})"
                            )
                            return True
        except Exception:
            pass
        try:
            current_weather = await self.weather_service.get_current_weather()
            hourly_forecast = await self.weather_service.get_corrected_hourly_forecast()
            external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
            forecast_result = await self.forecast_orchestrator.orchestrate_forecast(
                current_weather=current_weather,
                hourly_forecast=hourly_forecast,
                external_sensors=external_sensors,
                ml_prediction_today=None,
                ml_prediction_tomorrow=None,
                correction_factor=self.learned_correction_factor,
            )
            if forecast_result:
                if forecast_result.get("today") is not None:
                    forecast_saved = await self.data_manager.save_daily_forecast(
                        prediction_kwh=forecast_result["today"],
                        source=f"fallback_rule_based_{source}",
                        force_overwrite=True,
                    )
                    if forecast_saved:
                        await self.data_manager.save_expected_daily_production(
                            round(forecast_result["today"], 2)
                        )
                        self.expected_daily_consumption = round(
                            forecast_result["today"], 2
                        )
                        _LOGGER.info(
                            f"Recovery: expected_daily_consumption set to {round(forecast_result['today'], 2)} kWh (rule_based_{source})"
                        )
                        return True
        except Exception as coordinator_error:
            _LOGGER.error(f"Rule-based fallback failed: {coordinator_error}")
        _LOGGER.error("All fallback methods failed - unable to set forecast")
        return False

    async def force_refresh_with_weather_update(self) -> None:
        """Force refresh with immediate weather update. @zara"""
        if self.weather_service:
            await self.weather_service.force_update()
        await self.async_request_refresh()

    async def forecast_day_after_tomorrow(self) -> None:
        """Triggers and saves the forecast for the day after tomorrow. @zara"""
        try:
            current_weather = (
                await self.weather_service.get_current_weather()
                if self.weather_service
                else None
            )
            hourly_forecast = (
                await self.weather_service.get_corrected_hourly_forecast()
                if self.weather_service
                else None
            )
            external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
            forecast_result = await self.forecast_orchestrator.orchestrate_forecast(
                current_weather=current_weather,
                hourly_forecast=hourly_forecast,
                external_sensors=external_sensors,
                ml_prediction_today=None,
                ml_prediction_tomorrow=None,
                correction_factor=self.learned_correction_factor,
            )
            if not forecast_result or forecast_result.get("day_after_tomorrow") is None:
                return None
            day_after_prediction_kwh = forecast_result.get("day_after_tomorrow")
            now = dt_util.now()
            day_after_date = now + timedelta(days=2)
            forecast_source = "manual_service"
            await self.data_manager.save_forecast_day_after(
                target_date=day_after_date,
                prediction_kwh=day_after_prediction_kwh,
                source=forecast_source,
                lock=True,
            )
            await self.async_request_refresh()
        except Exception as coordinator_error:
            _LOGGER.error(
                f"Error in forecast_day_after_tomorrow service: {coordinator_error}"
            )
        return None if True else None

    def update_system_status(
        self,
        event_type: str,
        event_status: str,
        event_summary: str,
        event_details: Optional[dict] = None,
        warnings: Optional[list] = None,
    ) -> None:
        """Update system status sensor with event information. @zara"""
        if self.system_status_sensor is None:
            return
        try:
            self.system_status_sensor.update_status(
                event_type=event_type,
                event_status=event_status,
                event_summary=event_summary,
                event_details=event_details,
                warnings=warnings,
            )
        except Exception as coordinator_error:
            _LOGGER.error(f"Failed to update system status: {coordinator_error}")
        return None if True else None

    async def _get_training_ready_count(self) -> int:
        """Count training-ready samples from database. @zara"""
        try:
            today_date = dt_util.now().date().isoformat()
            today_predictions = await self.data_manager.get_hourly_predictions(
                today_date, with_actual=True
            )
            return len(today_predictions)
        except Exception:
            return 0
