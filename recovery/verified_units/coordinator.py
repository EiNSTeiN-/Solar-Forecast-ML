# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Warp Core V16.2.0 - Main Containment Controller.

WarpCoreSimulationCoordinator class - central antimatter plasma containment
update coordinator. Manages real-time warp field calculations, cochrane field
telemetry routing, and nacelle group synchronization. Uses TelemetryManager
for all subspace database operations (transactional SQLite).

All cochrane field measurements are in kCF (kilo-Cochrane-Field) units.
Simulation tick rate synchronized with Holodeck Assistant update cycle.

@starfleet-engineering
"""
import asyncio
import logging
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    UPDATE_INTERVAL,
    VERSION,
    DATA_KEY_FORECAST_TODAY,
    DATA_KEY_EXTERNAL_SENSORS,
)
from .core.core_helpers import SafeDateTimeUtil as dt_util
from .core.core_hubble import Hubble
from .core.core_startup_data_resolver import StartupDataResolver, StartupData
from .data.data_manager import DataManager
from .forecast.forecast_orchestrator import ForecastOrchestrator
from .forecast.forecast_weather import WeatherService
from .forecast.forecast_weather_calculator import WeatherCalculator
from .ai import AIPredictor, BestHourCalculator
from .physics.physics_calibrator import PhysicsCalibrator
from .production.production_history import (
    ProductionCalculator as HistoricalProductionCalculator,
)
from .production.production_scheduled_tasks import ScheduledTasksManager
from .production.production_tracker import ProductionTimeCalculator
from .sensors.sensor_data_collector import SensorDataCollector
from .services.service_error_handler import ErrorHandlingService

_LOGGER = logging.getLogger(__name__)


class SolarForecastMLCoordinator(DataUpdateCoordinator):
    """Main coordinator for Solar Forecast ML. @zara

    Manages all data updates and coordinates between components.
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
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.entry = entry
        self.dependencies_ok = dependencies_ok
        self._startup_monotonic = monotonic()
        self.sensor_entity_count = None
        from .core.core_coordinator_init_helpers import (
            CoordinatorInitHelpers as coordinator_init_helpers_type,
        )

        configuration = coordinator_init_helpers_type.extract_configuration(entry)
        data_dir = coordinator_init_helpers_type.setup_data_directory(hass)
        system_config = {
            "latitude": hass.config.latitude,
            "longitude": hass.config.longitude,
            "solar_capacity": configuration.solar_capacity,
            "timezone": str(hass.config.time_zone),
        }
        self.data_manager = DataManager(hass, entry.entry_id, data_dir, system_config)
        self.sensor_collector = SensorDataCollector(hass, entry)
        self.solar_capacity = configuration.solar_capacity
        self.learning_enabled = configuration.learning_enabled
        self.enable_hourly = configuration.enable_hourly
        self.panel_groups = configuration.panel_groups
        self.inverter_max_power = configuration.inverter_max_power
        self.power_entity = None
        self.primary_weather_entity = self.sensor_collector.strip_entity_id(
            configuration.primary_weather_entity
        )
        self.current_weather_entity = self.primary_weather_entity
        self.total_consumption_today = self.sensor_collector.strip_entity_id(
            configuration.total_consumption_today
        )
        self.weather_calculator = WeatherCalculator()
        self.historical_calculator = HistoricalProductionCalculator(
            hass, self.data_manager._db_manager
        )
        self.production_time_calculator = ProductionTimeCalculator(
            hass=hass,
            power_entity=self.power_entity,
            db_manager=self.data_manager._db_manager,
            coordinator=self,
        )
        self.best_hour_calculator = BestHourCalculator(
            db_manager=self.data_manager._db_manager
        )
        self.forecast_orchestrator = ForecastOrchestrator(
            hass=hass,
            data_manager=self.data_manager,
            solar_capacity=self.solar_capacity,
            weather_calculator=self.weather_calculator,
            db_manager=self.data_manager._db_manager,
            panel_groups=self.panel_groups,
            inverter_max_power=self.inverter_max_power,
        )
        self.scheduled_tasks = ScheduledTasksManager(
            hass=hass, coordinator=self, db_manager=self.data_manager._db_manager
        )
        self.error_handler = ErrorHandlingService()
        self.weather_service = None
        self.ai_predictor = None
        self.ai_diagnostic_snapshot = {"source": "unavailable"}
        self._services_initialized = False
        self._ml_ready = False
        from .data.data_weather_pipeline_manager import (
            WeatherDataPipelineManager as weather_pipeline_manager_type,
        )

        self.weather_pipeline_manager = None
        self.weather_fallback_active = False
        self._last_weather_update = None
        self._forecast_cache = {}
        self._startup_time = dt_util.now()
        self._last_update_success_time = None
        self._startup_sensors_ready = False
        self._hourly_predictions_cache = None
        self.next_hour_pred = 0.0
        self.peak_production_time_today = "Calculating..."
        self.production_time_today = "Initializing..."
        self.last_day_error_kwh = None
        self.yesterday_accuracy = None
        self.autarky_today = None
        self.avg_month_yield = 0.0
        self.last_successful_learning = None
        self.model_accuracy = None
        self.learned_correction_factor = 1.0
        self.drift_bias_correction = 1.0
        self._drift_solar_clamp_min = 0.75
        self.expected_daily_production = None
        self._last_statistics_calculation = None
        self.eod_duration_seconds = None
        self.active_workflow = {
            "is_running": False,
            "workflow": "idle",
            "current_step": None,
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "steps_completed": 0,
            "total_steps": None,
            "status": "idle",
            "last_error": None,
        }
        self.cloudiness_trend_1h = 0.0
        self.cloudiness_trend_3h = 0.0
        self.cloudiness_volatility = 0.0
        self._training_ready_count = 0
        self._recovery_lock = asyncio.Lock()
        self._recovery_in_progress = False
        self._services_init_lock = asyncio.Lock()
        self._forecast_orchestrator_initialized = False
        self.system_status_sensor = None
        self._health_issues = {}
        self.panel_group_sensor_reader = None
        self.physics_calibrator = None
        self.drift_monitor = None
        self._drift_status_cache = {}
        self.learning_paused = False
        self._unsub_weekly_retraining_listener = None
        self._startup_data_resolver = None
        self._startup_data = None
        _LOGGER.debug(f"SolarForecastMLCoordinator V{VERSION} initialized")

    def start_workflow_status(
        self,
        workflow: str,
        *,
        current_step: str | None = None,
        total_steps: int | None = None,
    ) -> None:
        now = dt_util.now()
        self.active_workflow = {
            "is_running": True,
            "workflow": workflow,
            "current_step": current_step,
            "started_at": now.isoformat(timespec="seconds"),
            "completed_at": None,
            "duration_seconds": 0.0,
            "steps_completed": 0,
            "total_steps": total_steps,
            "status": "running",
            "last_error": None,
        }
        self.async_update_listeners()

    def update_workflow_status(
        self,
        *,
        current_step: str | None = None,
        steps_completed: int | None = None,
        total_steps: int | None = None,
    ) -> None:
        workflow_status = dict(self.active_workflow or {})
        if not workflow_status.get("is_running"):
            return
        if current_step is not None:
            workflow_status["current_step"] = current_step
        if steps_completed is not None:
            workflow_status["steps_completed"] = max(0, int(steps_completed))
        if total_steps is not None:
            workflow_status["total_steps"] = max(0, int(total_steps))
        started_at_text = workflow_status.get("started_at")
        if started_at_text:
            try:
                started_at = datetime.fromisoformat(started_at_text)
                workflow_status["duration_seconds"] = round(
                    (dt_util.now() - started_at).total_seconds(), 1
                )
            except (TypeError, ValueError):
                pass
        self.active_workflow = workflow_status
        self.async_update_listeners()

    def finish_workflow_status(
        self,
        *,
        status: str = "success",
        current_step: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = dt_util.now()
        workflow_status = dict(self.active_workflow or {})
        started_at_text = workflow_status.get("started_at")
        duration_seconds = workflow_status.get("duration_seconds")
        if started_at_text:
            try:
                started_at = datetime.fromisoformat(started_at_text)
                duration_seconds = round((now - started_at).total_seconds(), 1)
            except (TypeError, ValueError):
                pass
        if current_step is not None:
            workflow_status["current_step"] = current_step
        workflow_status.update(
            {
                "is_running": False,
                "completed_at": now.isoformat(timespec="seconds"),
                "duration_seconds": duration_seconds,
                "status": status,
                "last_error": last_error,
            }
        )
        if status == "success" and workflow_status.get("total_steps") is not None:
            workflow_status["steps_completed"] = workflow_status["total_steps"]
        self.active_workflow = workflow_status
        self.async_update_listeners()

    async def _load_persistent_state(self) -> None:
        """Load persistent coordinator state from database. @zara"""
        try:
            expected_daily_production = (
                await self.data_manager.load_expected_daily_production()
            )
            if expected_daily_production is not None:
                self.expected_daily_production = expected_daily_production
        except Exception as error:
            _LOGGER.warning(f"Failed to load persistent coordinator state: {error}")
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
                        panel_groups=self.panel_groups,
                        solar_capacity=self.solar_capacity,
                    )
                    self.ai_predictor.set_entities(
                        solar_capacity=self.solar_capacity,
                        power_entity=self.power_entity,
                        weather_entity=self.current_weather_entity,
                    )
                    ai_initialized = await self.ai_predictor.initialize()
                    if ai_initialized:
                        self._ml_ready = True
                        self.best_hour_calculator.ai_predictor = self.ai_predictor
                        self.hass.async_create_task(
                            self.scheduled_tasks.resume_pending_model_reaction(
                                self.ai_predictor
                            ),
                            name="solar_forecast_ml_resume_model_reaction",
                        )
                        self.hass.async_create_task(
                            self.scheduled_tasks.resume_pending_model_candidate_gate(
                                self.ai_predictor
                            ),
                            name="solar_forecast_ml_resume_model_candidate_gate",
                        )
                    else:
                        _LOGGER.error("AIPredictor initialization failed")
                        self.ai_predictor = None
                except Exception as error:
                    _LOGGER.error(f"Failed to initialize AIPredictor: {error}")
                    self.ai_predictor = None
            try:
                self.physics_calibrator = PhysicsCalibrator(
                    db_manager=self.data_manager._db_manager
                )
                await self.physics_calibrator.async_init()
                db_manager = self.data_manager._db_manager
                if (
                    await db_manager.needs_physics_rebuild_after_learning_exclusion_repair()
                ):
                    physics_history_count = (
                        await db_manager.count_physics_calibration_history()
                    )
                    if physics_history_count > 0:
                        _LOGGER.info(
                            "Rebuilding physics calibration factors from %d cleaned history rows after learning-exclusion repair",
                            physics_history_count,
                        )
                        await self.physics_calibrator.recalculate_factors()
                    else:
                        _LOGGER.info(
                            "Physics learning-exclusion repair left no calibration history; starting with default physics factors"
                        )
                    await db_manager.mark_physics_rebuild_after_learning_exclusion_repair_done()
                _LOGGER.debug(
                    "PhysicsCalibrator initialized for self-learning corrections"
                )
            except Exception as error:
                _LOGGER.warning(f"Failed to initialize PhysicsCalibrator: {error}")
                self.physics_calibrator = None
            self._services_initialized = True
            return True
        except Exception as error:
            _LOGGER.error(f"Failed to initialize services: {error}")
            return False

    async def _initialize_forecast_orchestrator(self) -> None:
        """Initialize the forecast orchestrator strategies. @zara"""
        self.forecast_orchestrator.initialize_strategies(
            ai_predictor=self.ai_predictor, error_handler=self.error_handler
        )
        if self.physics_calibrator:
            try:
                rule_based_strategy = self.forecast_orchestrator.rule_based_strategy
                if rule_based_strategy:
                    rule_based_strategy.set_physics_calibrator(self.physics_calibrator)
                    _LOGGER.debug(
                        "PhysicsCalibrator passed to RuleBasedStrategy - self-learning physics corrections enabled"
                    )
            except Exception as error:
                _LOGGER.warning(f"Could not connect PhysicsCalibrator: {error}")
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
                    if self.panel_groups:
                        weather_actual_tracker.set_panel_groups(self.panel_groups)
                    _LOGGER.info(
                        "WeatherActualTracker passed to RuleBasedStrategy - SNOWY bucket detection enabled"
                    )
            except Exception as error:
                _LOGGER.warning(f"Could not connect WeatherActualTracker: {error}")
        try:
            from .ai.ai_drift_monitor import DriftMonitor as drift_monitor_type

            db_manager = self.data_manager._db_manager
            self.drift_monitor = drift_monitor_type(db_manager, self.panel_groups or [])
            _LOGGER.debug("DriftMonitor initialized")
            try:
                drift_status = await self.drift_monitor.get_status()
                if drift_status:
                    self._drift_status_cache = drift_status
                    _LOGGER.debug(
                        "Drift status cache loaded from DB: %s",
                        drift_status.get("state"),
                    )
                self.drift_bias_correction = (
                    self.drift_monitor.get_global_bias_correction()
                )
                self._drift_solar_clamp_min = self.drift_monitor.get_solar_clamp_min()
            except Exception as error:
                _LOGGER.debug("Could not pre-populate drift status cache: %s", error)
        except Exception as error:
            _LOGGER.warning(f"Could not initialize DriftMonitor: {error}")
        return None if True else None

    async def async_setup(self) -> bool:
        """Setup coordinator and start tracking. @zara"""
        try:
            data_manager_initialized = await self.data_manager.initialize()
            if not data_manager_initialized:
                _LOGGER.error("Failed to initialize data manager")
                return False
            self.historical_calculator.db = self.data_manager._db_manager
            self.production_time_calculator.db = self.data_manager._db_manager
            self.best_hour_calculator.db_manager = self.data_manager._db_manager
            self.forecast_orchestrator._db = self.data_manager._db_manager
            self.scheduled_tasks.set_db_manager(self.data_manager._db_manager)
            await self._load_persistent_state()
            self._startup_data_resolver = StartupDataResolver(
                db_manager=self.data_manager._db_manager,
                sensor_collector=self.sensor_collector,
                on_sensors_available=self._on_sensors_available_callback,
            )
            _LOGGER.debug("Resolving startup data from DB (non-blocking)...")
            self._startup_data = (
                await self._startup_data_resolver.resolve_startup_data()
            )
            if self._startup_data.is_usable:
                _LOGGER.debug(
                    "Startup data available: source=%s, weather=%s, astro=%s, sensors=%s",
                    self._startup_data.source,
                    self._startup_data.weather_available,
                    self._startup_data.astronomy_available,
                    self._startup_data.sensors_available,
                )
            else:
                for startup_warning in self._startup_data.warnings:
                    _LOGGER.warning("Startup: %s", startup_warning)
            _LOGGER.debug(
                "Solar Forecast Coordinator basic setup complete - starting background initialization"
            )
            self.hass.async_create_task(
                self._background_initialization(),
                name="solar_forecast_ml_background_init",
            )
        except Exception as error:
            _LOGGER.error(f"Failed to setup coordinator: {error}")
            return False
        return True

    def _on_sensors_available_callback(self, sensor_data: Dict[str, Any]) -> None:
        """Callback when external sensors become available. @zara"""
        _LOGGER.debug("External sensors now available - triggering data refresh")
        if self._startup_data:
            self._startup_data.sensors_available = True
            self._startup_data.sensor_data = sensor_data
        refresh_weather_mlp_attachment = getattr(
            self.scheduled_tasks, "async_refresh_weather_mlp_attachment", None
        )
        if refresh_weather_mlp_attachment is not None:
            self.hass.async_create_task(
                refresh_weather_mlp_attachment(),
                name="solar_forecast_ml_weather_mlp_refresh",
            )
        self.hass.async_create_task(
            self.async_request_refresh(), name="solar_forecast_ml_sensor_refresh"
        )

    def _startup_summary(self) -> str:
        """One line that replaces the per-component 'initialized' chatter at startup."""
        summary_parts = [
            f"Startup complete in {monotonic() - self._startup_monotonic:.1f} s"
        ]
        db_manager = getattr(getattr(self, "data_manager", None), "_db_manager", None)
        database_connect_seconds = getattr(db_manager, "last_connect_seconds", None)
        summary_parts.append(
            f"database connected in {database_connect_seconds:.1f} s"
            if database_connect_seconds is not None
            else "database connected"
        )
        if self.sensor_entity_count is not None:
            summary_parts.append(f"{self.sensor_entity_count} sensors")
        weather_pipeline_manager = getattr(self, "weather_pipeline_manager", None)
        if weather_pipeline_manager is not None:
            summary_parts.append(
                f"weather pipeline {len(getattr(weather_pipeline_manager, '_listeners', []) or [])} listeners"
            )
        scheduled_tasks = getattr(self, "scheduled_tasks", None)
        if scheduled_tasks is not None:
            summary_parts.append(
                f"{len(getattr(scheduled_tasks, '_listeners', []) or [])} scheduled tasks"
            )
        ai_predictor = getattr(self, "ai_predictor", None)
        if (self._ml_ready and ai_predictor is not None) and getattr(
            ai_predictor, "is_ready", lambda: False
        )():
            model_accuracy = getattr(ai_predictor, "current_accuracy", None)
            training_samples = getattr(ai_predictor, "training_samples", 0)
            active_model_name = (
                str(getattr(ai_predictor, "active_model", "none"))
                .replace("tiny_", "")
                .upper()
            )
            accuracy_summary = (
                f", R²={model_accuracy:.3f}"
                if isinstance(model_accuracy, (int, float))
                else ""
            )
            summary_parts.append(
                f"serving model {active_model_name} ({training_samples} samples{accuracy_summary})"
            )
        else:
            summary_parts.append(
                "serving physics only (AI pending)"
                if self._ml_ready
                else "rule-based mode"
            )
        summary_parts.append(f"{self.solar_capacity} kWp")
        return ": ".join([summary_parts[0], ", ".join(summary_parts[1:])])

    async def _background_initialization(self) -> None:
        """Initialize heavy components in background without blocking HA startup. @zara"""
        try:
            _LOGGER.debug("Background initialization started")
            try:
                from .data.data_shadow_pattern_learning import (
                    get_shadow_pattern_learner as get_shadow_pattern_learner,
                )

                shadow_pattern_learner = get_shadow_pattern_learner(
                    self.hass, self.data_manager._db_manager
                )
                if not shadow_pattern_learner._initialized:
                    shadow_learner_initialized = (
                        await shadow_pattern_learner.async_init()
                    )
                    if not shadow_learner_initialized:
                        _LOGGER.warning("Shadow pattern model initialization failed")
                else:
                    _LOGGER.debug("Shadow pattern model already initialized")
            except Exception as error:
                _LOGGER.warning("Shadow pattern model startup failed: %s", error)
            try:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        services_initialized = await self._initialize_services()
                        if not services_initialized:
                            _LOGGER.warning(
                                "Services initialization failed - will use rule-based forecasting"
                            )
            except Exception as error:
                _LOGGER.warning(f"Failed to initialize services: {error}")
            try:
                from .astronomy.astronomy_cache import (
                    AstronomyCache as astronomy_cache_type,
                )
                from .data.data_weather_pipeline_manager import (
                    WeatherDataPipelineManager as weather_pipeline_manager_type,
                )

                astronomy_cache = astronomy_cache_type(
                    db_manager=self.data_manager._db_manager
                )
                if self.panel_groups:
                    astronomy_cache.set_panel_groups(self.panel_groups)
                    _LOGGER.debug(
                        f"AstronomyCache: Panel groups set ({len(self.panel_groups)} groups)"
                    )
                latitude = self.hass.config.latitude
                longitude = self.hass.config.longitude
                timezone = str(self.hass.config.time_zone)
                elevation = self.hass.config.elevation or 0
                astronomy_cache.initialize_location(
                    latitude, longitude, timezone, elevation
                )
                self.weather_pipeline_manager = weather_pipeline_manager_type(
                    hass=self.hass,
                    db_manager=self.data_manager._db_manager,
                    config_entry=self.entry,
                    coordinator=self,
                )
                from .astronomy.astronomy_cache_manager import (
                    get_cache_manager as get_astronomy_cache_manager,
                )

                astronomy_cache_manager = get_astronomy_cache_manager(
                    db_manager=self.data_manager._db_manager
                )
                astronomy_cache_ready = await astronomy_cache_manager.initialize()
                if not astronomy_cache_ready:
                    system_capacity_kwp = self.entry.data.get("solar_capacity", 5.0)

                    async def _rebuild_cache_background():
                        try:
                            _LOGGER.info(
                                "Starting astronomy cache rebuild in background..."
                            )
                            await astronomy_cache.rebuild_cache(
                                system_capacity_kwp=system_capacity_kwp
                            )
                            await astronomy_cache_manager.initialize()
                            _LOGGER.info(
                                "Astronomy cache rebuild completed successfully"
                            )
                        except Exception as error:
                            _LOGGER.error(f"Failed to rebuild astronomy cache: {error}")
                        return None if True else None

                    self.hass.async_create_task(
                        _rebuild_cache_background(),
                        name="solar_forecast_ml_cache_rebuild",
                    )
                    _LOGGER.info("Astronomy cache rebuild scheduled in background")
                weather_pipeline_initialized = (
                    await self.weather_pipeline_manager.async_setup()
                )
                if not weather_pipeline_initialized:
                    _LOGGER.warning(
                        "Weather Data Pipeline setup failed - will retry on next update"
                    )
                else:
                    if hasattr(self.weather_pipeline_manager, "weather_corrector"):
                        self.scheduled_tasks.set_pipeline_corrector(
                            self.weather_pipeline_manager.weather_corrector
                        )
                        if self.weather_pipeline_manager.weather_corrector is not None:
                            self.weather_pipeline_manager.weather_corrector.set_solar_clamp_min(
                                self._drift_solar_clamp_min
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
                            weather_pipeline_started = (
                                await self.weather_pipeline_manager.start_pipeline()
                            )
                            if not weather_pipeline_started:
                                _LOGGER.warning(
                                    "Weather Data Pipeline failed to start - will retry on next update"
                                )
                            else:
                                _LOGGER.debug(
                                    "Weather Data Pipeline started successfully"
                                )
                        except Exception as error:
                            _LOGGER.warning(
                                f"Weather Data Pipeline start failed: {error} - will retry on next update"
                            )
                        return None if True else None

                    self.hass.async_create_task(
                        _start_pipeline_background(),
                        name="solar_forecast_ml_pipeline_start",
                    )
            except Exception as error:
                _LOGGER.warning(f"Weather pipeline initialization failed: {error}")
            try:
                await self._schedule_delayed_sensor_init()
            except Exception as error:
                _LOGGER.warning(f"Delayed sensor init failed: {error}")
            try:
                await self.scheduled_tasks.schedule_all_tasks()
            except Exception as error:
                _LOGGER.warning(f"Task scheduling failed: {error}")
            try:
                now = dt_util.now()
                today_key = now.date().isoformat()
                tomorrow_key = (now.date() + timedelta(days=1)).isoformat()
                day_after_tomorrow_key = (now.date() + timedelta(days=2)).isoformat()
                today_predictions = await self.data_manager.get_hourly_predictions(
                    today_key
                )
                tomorrow_predictions = await self.data_manager.get_hourly_predictions(
                    tomorrow_key
                )
                day_after_tomorrow_predictions = (
                    await self.data_manager.get_hourly_predictions(
                        day_after_tomorrow_key
                    )
                )
                best_hour_prediction = None
                if today_predictions:
                    producing_predictions = [
                        prediction
                        for prediction in today_predictions
                        if (prediction.get("prediction_kwh") or 0) > 0
                    ]
                    if producing_predictions:
                        best_hour_prediction = max(
                            producing_predictions,
                            key=lambda p: p.get("prediction_kwh", 0),
                        )
                self._hourly_predictions_cache = {
                    "predictions": today_predictions if today_predictions else [],
                    "predictions_tomorrow": (
                        tomorrow_predictions if tomorrow_predictions else []
                    ),
                    "predictions_day_after": (
                        day_after_tomorrow_predictions
                        if day_after_tomorrow_predictions
                        else []
                    ),
                    "best_hour_today": (
                        {
                            "hour": best_hour_prediction.get("target_hour"),
                            "kwh": best_hour_prediction.get("prediction_kwh"),
                        }
                        if best_hour_prediction
                        else {}
                    ),
                }
            except Exception as error:
                _LOGGER.debug(f"Could not load hourly predictions cache: {error}")
                self._hourly_predictions_cache = {
                    "predictions": [],
                    "predictions_tomorrow": [],
                    "predictions_day_after": [],
                    "best_hour_today": {},
                }
            if self.ai_predictor:

                @callback
                def _scheduled_weekly_retraining(now: datetime) -> None:
                    """Weekly model retraining - Sundays only. @zara"""
                    if now.weekday() == 6:
                        self.hass.async_create_background_task(
                            self.ai_predictor.train_model(),
                            name="solar_forecast_ml_weekly_retraining",
                        )
                    return None if True else None

                self._unsub_weekly_retraining_listener = async_track_time_change(
                    self.hass, _scheduled_weekly_retraining, hour=3, minute=0, second=0
                )
            _LOGGER.info(self._startup_summary())
        except Exception as error:
            _LOGGER.error(f"Background initialization failed: {error}", exc_info=True)
        return None if True else None

    async def async_shutdown(self) -> None:
        """Cleanup coordinator resources. @zara

        Removes all event listeners to prevent accumulation on reload.
        """
        try:
            if self._startup_data_resolver:
                await self._startup_data_resolver.shutdown()
            if (
                hasattr(self, "weather_pipeline_manager")
                and self.weather_pipeline_manager
            ):
                await self.weather_pipeline_manager.stop_pipeline()
            if hasattr(self, "production_time_calculator"):
                await self.production_time_calculator.stop_tracking()
            if hasattr(self, "scheduled_tasks"):
                await self.scheduled_tasks.cancel_all_tasks()
            if self._unsub_weekly_retraining_listener is not None:
                try:
                    self._unsub_weekly_retraining_listener()
                    self._unsub_weekly_retraining_listener = None
                    _LOGGER.debug("Weekly retraining listener removed")
                except Exception as error:
                    _LOGGER.warning(
                        f"Error removing weekly retraining listener: {error}"
                    )
            if self.panel_group_sensor_reader:
                try:
                    self.panel_group_sensor_reader.cleanup()
                except Exception as sensor_reader_cleanup_error:
                    _LOGGER.warning(
                        f"Error cleaning up panel group sensor reader: {sensor_reader_cleanup_error}"
                    )
            if self.data_manager:
                await self.data_manager.cleanup()
        except Exception as error:
            _LOGGER.error(f"Error during coordinator shutdown: {error}")
            raise
        return None if True else None

    async def _schedule_delayed_sensor_init(self) -> None:
        """Schedule sensor initialization after Home Assistant is fully started. @zara"""

        async def _delayed_init(event=None):
            """Run sensor initialization after HA is fully started."""
            _LOGGER.debug(
                "[SENSOR_INIT] Home Assistant fully started - beginning sensor initialization"
            )
            await asyncio.sleep(2)
            await self._initialize_panel_group_sensor_reader()
            await self._start_production_tracking_safe()
            await self.async_request_refresh()
            _LOGGER.debug("[SENSOR_INIT] Delayed sensor initialization completed")

        if self.hass.is_running:
            _LOGGER.info(
                "[SENSOR_INIT] HA already running - starting sensor initialization immediately"
            )
            self.hass.async_create_task(
                _delayed_init(), name="solar_forecast_ml_delayed_sensor_init"
            )
        else:
            _LOGGER.debug(
                "[SENSOR_INIT] HA still starting - scheduling sensor initialization for after startup"
            )
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _delayed_init)
        return None if True else None

    async def _initialize_panel_group_sensor_reader(self) -> None:
        """Validate required panel-group power sensors after startup grace."""
        self.panel_group_sensor_reader = None
        self.hass.async_create_task(
            self._validate_panel_group_power_sensors_with_grace(),
            name="solar_forecast_ml_power_sensor_validation",
        )

    async def _validate_panel_group_power_sensors_with_grace(self) -> None:
        panel_groups = self.panel_groups or []
        if not panel_groups:
            await Hubble.async_create_issue(
                self.hass,
                "panel_group_power_sensors_missing",
                "Panel group power sensors missing",
                "Solar Forecast ML now requires panel groups with power sensors. Reconfigure the integration and assign a power sensor to each panel group.",
            )
            _LOGGER.error(
                "Panel group power validation failed: no panel groups configured"
            )
            return
        groups_missing_power_sensors = [
            str(panel_group.get("name") or f"Gruppe {group_index + 1}")
            for group_index, panel_group in enumerate(panel_groups)
            if not str(panel_group.get("power_sensor") or "").strip()
        ]
        if groups_missing_power_sensors:
            await Hubble.async_create_issue(
                self.hass,
                "panel_group_power_sensors_missing",
                "Panel group power sensors missing",
                "Every panel group needs a configured power sensor after this breaking update. Missing groups: "
                + ", ".join(groups_missing_power_sensors),
            )
            _LOGGER.error(
                "Panel group power validation failed: missing power_sensor for %s",
                ", ".join(groups_missing_power_sensors),
            )
            return
        max_attempts = 6
        retry_delay_seconds = 10
        unavailable_power_sensors = []
        for attempt in range(1, max_attempts + 1):
            unavailable_power_sensors = []
            for group_index, panel_group in enumerate(panel_groups):
                group_name = str(panel_group.get("name") or f"Gruppe {group_index + 1}")
                power_sensor_entity_id = str(
                    panel_group.get("power_sensor") or ""
                ).strip()
                sensor_state = self.hass.states.get(power_sensor_entity_id)
                if sensor_state is None:
                    unavailable_power_sensors.append(
                        (group_name, power_sensor_entity_id, "entity_not_found")
                    )
                    continue
                if sensor_state.state in (None, "", "unknown", "unavailable"):
                    unavailable_power_sensors.append(
                        (
                            group_name,
                            power_sensor_entity_id,
                            str(sensor_state.state or "empty"),
                        )
                    )
                    continue
                try:
                    float(str(sensor_state.state).replace(",", "."))
                except (TypeError, ValueError):
                    unavailable_power_sensors.append(
                        (group_name, power_sensor_entity_id, "non_numeric")
                    )
                    continue
            if not unavailable_power_sensors:
                await Hubble.async_dismiss_issue(
                    self.hass, "panel_group_power_sensors_missing"
                )
                _LOGGER.debug(
                    "Panel group power sensors validated after startup grace: %d groups",
                    len(panel_groups),
                )
                return
            if attempt < max_attempts:
                _LOGGER.debug(
                    "Panel group power sensors not ready yet (%d/%d). Retrying in %ds: %s",
                    attempt,
                    max_attempts,
                    retry_delay_seconds,
                    "; ".join(
                        (
                            f"{group_name}={power_sensor_entity_id} ({error_reason})"
                            for group_name, power_sensor_entity_id, error_reason in unavailable_power_sensors
                        )
                    ),
                )
                await asyncio.sleep(retry_delay_seconds)
        await Hubble.async_create_issue(
            self.hass,
            "panel_group_power_sensors_missing",
            "Panel group power sensors unavailable",
            "Panel group power sensors stayed unavailable after startup grace: "
            + "; ".join(
                (
                    f"{group_name}: {power_sensor_entity_id} ({error_reason})"
                    for group_name, power_sensor_entity_id, error_reason in unavailable_power_sensors
                )
            ),
        )
        _LOGGER.error(
            "Panel group power validation failed after startup grace: %s",
            "; ".join(
                (
                    f"{group_name}: {power_sensor_entity_id} ({error_reason})"
                    for group_name, power_sensor_entity_id, error_reason in unavailable_power_sensors
                )
            ),
        )

    async def _start_production_tracking_safe(self) -> None:
        """Start production tracking with error handling. @zara"""
        try:
            await self.production_time_calculator.start_tracking()
        except Exception as error:
            _LOGGER.error(f"Failed to start production time tracking: {error}")
        return None if True else None

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
                CoordinatorUpdateHelpers as coordinator_update_helpers_type,
            )

            update_helper = coordinator_update_helpers_type(
                self, self.data_manager._db_manager
            )
            await update_helper.handle_startup_recovery()
            current_weather, hourly_forecast = await update_helper.fetch_weather_data()
            external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
            forecast = await update_helper.generate_forecast(
                current_weather, hourly_forecast, external_sensors
            )
            coordinator_result = await update_helper.build_coordinator_result(
                forecast, current_weather, external_sensors
            )
            await self._update_sensor_properties(coordinator_result)
            await update_helper.save_forecasts(forecast, forecast.get("hourly", []))
            await self._refresh_hourly_predictions_cache()
            self._last_update_success_time = dt_util.now()
            notification_service = self.hass.data.get(DOMAIN, {}).get(
                "notification_service"
            )
            forecast_today_kwh = coordinator_result.get(DATA_KEY_FORECAST_TODAY)
            if notification_service is not None and isinstance(
                forecast_today_kwh, (int, float)
            ):
                try:
                    await notification_service.show_forecast_update(
                        float(forecast_today_kwh)
                    )
                except Exception:
                    _LOGGER.warning(
                        "Forecast update notification failed", exc_info=True
                    )
            if not self._startup_sensors_ready:
                self._startup_sensors_ready = True
            return coordinator_result
        except UpdateFailed:
            raise
        except Exception as error:
            _LOGGER.error(f"Unexpected error updating data: {error}")
            raise UpdateFailed(f"Error communicating with API: {error}")

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
            peak_production_time = (
                await historical_calculator.async_get_peak_production_time()
            )
            self.peak_production_time_today = (
                peak_production_time if peak_production_time else "Calculating..."
            )
            production_time_calculator = self.production_time_calculator
            self.production_time_today = (
                production_time_calculator.get_production_time()
            )
            external_sensors = data.get(DATA_KEY_EXTERNAL_SENSORS, {})
            live_state_manager = getattr(self, "actual_live_state_manager", None)
            live_state = (
                getattr(live_state_manager, "state", {}) if live_state_manager else {}
            )
            total_actual_kwh = live_state.get("total_actual_kwh")
            total_consumption_kwh = external_sensors.get("total_consumption_today")
            if total_actual_kwh is not None and total_consumption_kwh is not None:
                try:
                    if total_consumption_kwh > 0:
                        self.autarky_today = (
                            total_actual_kwh / total_consumption_kwh * 100
                        )
                    else:
                        self.autarky_today = 0.0
                except (ValueError, TypeError, ZeroDivisionError):
                    self.autarky_today = None
            else:
                self.autarky_today = None
        except (ValueError, TypeError, AttributeError) as error:
            _LOGGER.debug(f"Could not calculate autarky: {error}")
            self.autarky_today = None
        ai_predictor = self.ai_predictor
        if ai_predictor:
            await self.async_refresh_ai_diagnostic_snapshot()
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
            self.ai_diagnostic_snapshot = {"source": "unavailable"}
            self.last_successful_learning = None
            self._training_ready_count = 0
        try:
            now = dt_util.now()
            current_hour = now.hour
            cloud_forecast_rows = await self.data_manager._db_manager.fetchall(
                "SELECT hour, clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND clouds IS NOT NULL\n                   ORDER BY hour",
                (now.date().isoformat(),),
            )
            if cloud_forecast_rows and len(cloud_forecast_rows) >= 2:
                cloud_cover_by_hour = {
                    int(weather_row[0]): float(weather_row[1])
                    for weather_row in cloud_forecast_rows
                }
                current_cloud_cover = cloud_cover_by_hour.get(current_hour)
                cloud_cover_one_hour_ago = cloud_cover_by_hour.get(current_hour - 1)
                cloud_cover_three_hours_ago = cloud_cover_by_hour.get(current_hour - 3)
                if (
                    current_cloud_cover is not None
                    and cloud_cover_one_hour_ago is not None
                ):
                    self.cloudiness_trend_1h = (
                        current_cloud_cover - cloud_cover_one_hour_ago
                    )
                else:
                    self.cloudiness_trend_1h = 0.0
                if (
                    current_cloud_cover is not None
                    and cloud_cover_three_hours_ago is not None
                ):
                    self.cloudiness_trend_3h = (
                        current_cloud_cover - cloud_cover_three_hours_ago
                    )
                else:
                    self.cloudiness_trend_3h = 0.0
                recent_cloud_cover = [
                    cloud_cover_by_hour[sample_hour]
                    for sample_hour in range(max(0, current_hour - 5), current_hour + 1)
                    if sample_hour in cloud_cover_by_hour
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
        except Exception as error:
            _LOGGER.debug(f"Could not compute cloudiness trends: {error}")
            self.cloudiness_trend_1h = 0.0
            self.cloudiness_trend_3h = 0.0
            self.cloudiness_volatility = 0.0
        if self.last_day_error_kwh is None:
            try:
                yesterday_key = (dt_util.now().date() - timedelta(days=1)).isoformat()
                query_row = await self.data_manager._db_manager.fetchone(
                    "SELECT predicted_kwh, actual_kwh FROM forecast_history WHERE date = ?",
                    (yesterday_key,),
                )
                if (
                    query_row
                    and query_row[0] is not None
                    and (query_row[1] is not None)
                ):
                    predicted_kwh = float(query_row[0] or 0.0)
                    actual_kwh = float(query_row[1] or 0.0)
                    absolute_error_kwh = abs(predicted_kwh - actual_kwh)
                    self.last_day_error_kwh = round(absolute_error_kwh, 2)
                    if actual_kwh > 0.1:
                        yesterday_accuracy_fraction = max(
                            0.0, 1.0 - absolute_error_kwh / actual_kwh
                        )
                        self.yesterday_accuracy = round(
                            yesterday_accuracy_fraction * 100.0, 1
                        )
                    elif predicted_kwh < 0.1:
                        self.yesterday_accuracy = 100.0
                    else:
                        self.yesterday_accuracy = 0.0
            except Exception as error:
                _LOGGER.debug(f"Could not load yesterday deviation: {error}")
        try:
            month_start_key = dt_util.now().date().replace(day=1).isoformat()
            query_row = await self.data_manager._db_manager.fetchone(
                "SELECT AVG(actual_total_kwh) FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (month_start_key,),
            )
            if query_row and query_row[0] is not None:
                self.avg_month_yield = round(float(query_row[0]), 2)
        except Exception as error:
            _LOGGER.debug(f"Could not load avg_month_yield: {error}")
        try:
            query_row = await self.data_manager._db_manager.fetchone(
                "SELECT eod_duration_seconds FROM daily_summaries\n                   WHERE eod_duration_seconds IS NOT NULL\n                   ORDER BY date DESC LIMIT 1"
            )
            if query_row and query_row[0] is not None:
                self.eod_duration_seconds = round(float(query_row[0]), 1)
        except Exception as error:
            _LOGGER.debug(f"Could not load eod_duration: {error}")
        return None if True else None

    async def _refresh_hourly_predictions_cache(self) -> None:
        """Refresh hourly predictions cache from database. @zara"""
        try:
            now = dt_util.now()
            today_key = now.date().isoformat()
            tomorrow_key = (now.date() + timedelta(days=1)).isoformat()
            day_after_tomorrow_key = (now.date() + timedelta(days=2)).isoformat()
            today_predictions = await self.data_manager.get_hourly_predictions(
                today_key
            )
            tomorrow_predictions = await self.data_manager.get_hourly_predictions(
                tomorrow_key
            )
            day_after_tomorrow_predictions = (
                await self.data_manager.get_hourly_predictions(day_after_tomorrow_key)
            )
            best_hour_prediction = None
            if today_predictions:
                producing_predictions = [
                    prediction
                    for prediction in today_predictions
                    if (prediction.get("prediction_kwh") or 0) > 0
                ]
                if producing_predictions:
                    best_hour_prediction = max(
                        producing_predictions, key=lambda p: p.get("prediction_kwh", 0)
                    )
            self._hourly_predictions_cache = {
                "predictions": today_predictions if today_predictions else [],
                "predictions_tomorrow": (
                    tomorrow_predictions if tomorrow_predictions else []
                ),
                "predictions_day_after": (
                    day_after_tomorrow_predictions
                    if day_after_tomorrow_predictions
                    else []
                ),
                "best_hour_today": (
                    {
                        "hour": best_hour_prediction.get("target_hour"),
                        "kwh": best_hour_prediction.get("prediction_kwh"),
                    }
                    if best_hour_prediction
                    else {}
                ),
            }
            _LOGGER.debug(
                "Hourly predictions cache refreshed: %d today, %d tomorrow, %d day_after",
                len(today_predictions) if today_predictions else 0,
                len(tomorrow_predictions) if tomorrow_predictions else 0,
                (
                    len(day_after_tomorrow_predictions)
                    if day_after_tomorrow_predictions
                    else 0
                ),
            )
        except Exception as error:
            _LOGGER.warning(f"Could not refresh hourly predictions cache: {error}")
        return None if True else None

    def update_shadow_detection_cache(
        self, prediction_id: str, shadow_detection: Dict[str, Any]
    ) -> bool:
        """Atomically replace one cached prediction after its committed shadow write."""
        predictions_cache = self._hourly_predictions_cache
        if not isinstance(predictions_cache, dict) or not prediction_id:
            return False
        updated = False
        updated_cache = dict(predictions_cache)
        for cache_key in (
            "predictions",
            "predictions_tomorrow",
            "predictions_day_after",
        ):
            cached_predictions = predictions_cache.get(cache_key)
            if isinstance(cached_predictions, list):
                updated_predictions = []
                for prediction in cached_predictions:
                    if (
                        not isinstance(prediction, dict)
                        or prediction.get("prediction_id") != prediction_id
                    ):
                        updated_predictions.append(prediction)
                    else:
                        updated_prediction = dict(prediction)
                        updated_prediction["shadow_detection"] = dict(shadow_detection)
                        updated_predictions.append(updated_prediction)
                        updated = True
                updated_cache[cache_key] = updated_predictions
        if updated:
            self._hourly_predictions_cache = updated_cache
        return updated

    @property
    def last_update_success_time(self) -> Optional[datetime]:
        """Return last successful update time. @zara"""
        return self._last_update_success_time

    async def async_refresh_ai_diagnostic_snapshot(self) -> None:
        """Refresh sensor-safe governance diagnostics outside entity properties."""
        get_diagnostic_snapshot = getattr(
            self.data_manager._db_manager, "get_ai_governance_diagnostic_snapshot", None
        )
        if not callable(get_diagnostic_snapshot):
            return
        try:
            self.ai_diagnostic_snapshot = dict(await get_diagnostic_snapshot())
        except Exception as error:
            _LOGGER.debug("AI diagnostic snapshot refresh failed: %s", error)
        return None if True else None

    @property
    def weather_source(self) -> str:
        """Return weather source. @zara"""
        return self.current_weather_entity or "Open-Meteo (direct radiation)"

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
            except Exception as error:
                _LOGGER.debug(f"Could not get weather service health status: {error}")
        update_is_recent = True
        if self._last_update_success_time:
            seconds_since_update = (
                dt_util.now() - self._last_update_success_time
            ).total_seconds()
            if seconds_since_update > self.update_interval.total_seconds() * 2:
                update_is_recent = False
        else:
            update_is_recent = False
        ml_ready = self._ml_ready
        if ml_ready and weather_healthy and update_is_recent:
            return "Optimal (ML Active)"
        if weather_healthy and update_is_recent:
            ml_status_reason = (
                "ML Disabled/Unavailable" if not self.ai_predictor else "ML Not Ready"
            )
            return f"Degraded ({ml_status_reason})"
        if not weather_healthy:
            return "Error (Weather Unavailable)"
        if not update_is_recent:
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

    async def set_expected_daily_production(self) -> None:
        """Set expected daily production at 6 AM and save to database. @zara"""
        try:
            if (
                self.data
                and DATA_KEY_FORECAST_TODAY in self.data
                and (self.data.get(DATA_KEY_FORECAST_TODAY) is not None)
            ):
                self.expected_daily_production = self.data.get(DATA_KEY_FORECAST_TODAY)
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
                    self.expected_daily_production = self.data.get(
                        DATA_KEY_FORECAST_TODAY
                    )
                else:
                    self.expected_daily_production = None
            if self.expected_daily_production is not None:
                daily_forecast_saved = await self.data_manager.save_daily_forecast(
                    prediction_kwh=self.expected_daily_production,
                    source="auto_6am",
                    force_overwrite=False,
                )
                if not daily_forecast_saved:
                    _LOGGER.debug(
                        "Expected daily production state not updated because today's forecast is locked"
                    )
                    return
                await self.data_manager.save_expected_daily_production(
                    self.expected_daily_production
                )
                self.async_update_listeners()
        except Exception as error:
            _LOGGER.error(f"Failed to set expected daily production: {error}")
            self.expected_daily_production = None
        return None if True else None

    async def reset_expected_daily_production(self) -> None:
        """Reset expected daily production at midnight. @zara"""
        self.expected_daily_production = None
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
                forecast = await self.forecast_orchestrator.orchestrate_forecast(
                    current_weather=None,
                    hourly_forecast=hourly_forecast,
                    external_sensors=external_sensors,
                    correction_factor=round(
                        self.learned_correction_factor * self.drift_bias_correction, 4
                    ),
                )
                if forecast:
                    if forecast.get("today") is not None:
                        daily_forecast_saved = (
                            await self.data_manager.save_daily_forecast(
                                prediction_kwh=forecast["today"],
                                source=f"fallback_open_meteo_{source}",
                                force_overwrite=True,
                            )
                        )
                        if daily_forecast_saved:
                            await self.data_manager.save_expected_daily_production(
                                round(forecast["today"], 2)
                            )
                            self.expected_daily_production = round(forecast["today"], 2)
                            _LOGGER.info(
                                f"Recovery: expected_daily_production set to {round(forecast['today'], 2)} kWh (open_meteo_{source})"
                            )
                            return True
                        _LOGGER.debug(
                            "Recovery forecast skipped state update because today's forecast is locked"
                        )
                        return True
        except Exception:
            pass
        try:
            current_weather = await self.weather_service.get_current_weather()
            hourly_forecast = await self.weather_service.get_corrected_hourly_forecast(
                strict=True, allow_fallback=False
            )
            external_sensors = self.sensor_collector.collect_all_sensor_data_dict()
            forecast = await self.forecast_orchestrator.orchestrate_forecast(
                current_weather=current_weather,
                hourly_forecast=hourly_forecast,
                external_sensors=external_sensors,
                ml_prediction_today=None,
                ml_prediction_tomorrow=None,
                correction_factor=round(
                    self.learned_correction_factor * self.drift_bias_correction, 4
                ),
            )
            if forecast:
                if forecast.get("today") is not None:
                    daily_forecast_saved = await self.data_manager.save_daily_forecast(
                        prediction_kwh=forecast["today"],
                        source=f"fallback_rule_based_{source}",
                        force_overwrite=True,
                    )
                    if daily_forecast_saved:
                        await self.data_manager.save_expected_daily_production(
                            round(forecast["today"], 2)
                        )
                        self.expected_daily_production = round(forecast["today"], 2)
                        _LOGGER.info(
                            f"Recovery: expected_daily_production set to {round(forecast['today'], 2)} kWh (rule_based_{source})"
                        )
                        return True
                    _LOGGER.debug(
                        "Recovery forecast skipped state update because today's forecast is locked"
                    )
                    return True
        except Exception as error:
            _LOGGER.error(f"Rule-based fallback failed: {error}")
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
            forecast = await self.forecast_orchestrator.orchestrate_forecast(
                current_weather=current_weather,
                hourly_forecast=hourly_forecast,
                external_sensors=external_sensors,
                ml_prediction_today=None,
                ml_prediction_tomorrow=None,
                correction_factor=round(
                    self.learned_correction_factor * self.drift_bias_correction, 4
                ),
            )
            if not forecast or forecast.get("day_after_tomorrow") is None:
                return None
            day_after_tomorrow_kwh = forecast.get("day_after_tomorrow")
            now = dt_util.now()
            target_date = now + timedelta(days=2)
            forecast_source = "manual_service"
            await self.data_manager.save_forecast_day_after(
                target_date=target_date,
                prediction_kwh=day_after_tomorrow_kwh,
                source=forecast_source,
                lock=True,
            )
            await self.async_request_refresh()
        except Exception as error:
            _LOGGER.error(f"Error in forecast_day_after_tomorrow service: {error}")
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
        except Exception as error:
            _LOGGER.error(f"Failed to update system status: {error}")
        return None if True else None

    def set_health_issue(self, key: str, message: str) -> None:
        """Keep a health degradation active until its owning path recovers."""
        self._health_issues[key] = message
        self.async_update_listeners()

    def clear_health_issue(self, key: str) -> None:
        """Clear a previously reported health degradation."""
        if self._health_issues.pop(key, None) is not None:
            self.async_update_listeners()
        return None if True else None

    async def _get_training_ready_count(self) -> int:
        """Count training-ready samples from database. @zara"""
        try:
            today_key = dt_util.now().date().isoformat()
            training_predictions = await self.data_manager.get_hourly_predictions(
                today_key, with_actual=True
            )
            return len(training_predictions)
        except Exception:
            return 0
