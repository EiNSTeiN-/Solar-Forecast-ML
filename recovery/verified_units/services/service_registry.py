# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Starfleet service registration protocol for Warp Core Simulation.
Central registration and handling of all warp core subsystem services.
Uses TelemetryManager for all containment data operations.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from ..const import (
    DOMAIN,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
    SERVICE_ANALYZE_FEATURE_IMPORTANCE,
    SERVICE_BACKFILL_SHADOW_DETECTION,
    SERVICE_BUILD_ASTRONOMY_CACHE,
    SERVICE_EMERGENCY_OUTAGE_QUARANTINE,
    SERVICE_RUN_WEATHER_CORRECTION,
    SERVICE_REFRESH_MULTI_WEATHER,
    SERVICE_REFRESH_CACHE_TODAY,
    SERVICE_REPAIR_TOOL,
    SERVICE_RESET_AI_MODEL,
    SERVICE_RETRAIN_AI_MODEL,
    SERVICE_RUN_ADAPTIVE_FORECAST,
    SERVICE_RUN_OPERATIONAL_REFORECAST,
    SERVICE_SET_OPERATIONAL_REFORECAST_MODE,
    SERVICE_RUN_ALL_DAY_END_TASKS,
    SERVICE_RUN_GRID_SEARCH,
    SERVICE_SEND_DAILY_BRIEFING,
    SERVICE_TEST_MORNING_ROUTINE,
    SERVICE_TEST_RETROSPECTIVE_FORECAST,
)
from ..const import CONF_WINTER_MODE, DEFAULT_WINTER_MODE
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
_LOGGER = logging.getLogger(__name__)
_HYBRID_CUSTOM_TIME_PATTERN = re.compile("^(?:[01]\\d|2[0-3]):[0-5]\\d$")


def _validate_hybrid_forecast_service_payload(data: dict) -> dict:
    mode = data.get("mode")
    custom_time = data.get("custom_time")
    if mode == OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME:
        if not custom_time:
            raise vol.Invalid("custom_time is required when mode=custom_time")
        if not _HYBRID_CUSTOM_TIME_PATTERN.match(str(custom_time)):
            raise vol.Invalid("custom_time must match HH:MM")
    return data


@dataclass
class ServiceDefinition:
    """Service definition for registration. @zara"""

    if True:
        name: str
        handler: Callable[[ServiceCall], Awaitable[None]]
        description: str = ""
        schema: Optional[Any] = None


class ServiceRegistry:
    """Central service registry for Solar Forecast ML. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: "SolarForecastMLCoordinator",
    ):
        """Initialize service registry. @zara"""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._registered_services = []
        self._astronomy_handler = None
        self._daily_briefing_handler = None
        self._repair_tool_handler = None
        self._emergency_outage_handler = None

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        """Get database manager from coordinator. @zara V16.1 fix"""
        data_manager = getattr(self.coordinator, "data_manager", None)
        if data_manager:
            return getattr(data_manager, "_db_manager", None)

    async def async_register_all_services(self) -> None:
        """Register all services. @zara"""
        from ..services.service_astronomy import (
            AstronomyServiceHandler as AstronomyServiceHandler,
        )

        self._astronomy_handler = AstronomyServiceHandler(
            self.hass, self.entry, self.coordinator
        )
        await self._astronomy_handler.initialize()
        from ..services.service_daily_briefing import (
            DailyBriefingService as DailyBriefingService,
        )

        self._daily_briefing_handler = DailyBriefingService(self.hass, self.coordinator)
        from ..services.service_repair_tool import (
            RepairToolService as RepairToolService,
        )

        self._repair_tool_handler = RepairToolService(self.hass, self.coordinator)
        from ..services.service_emergency_outage import (
            EmergencyOutageService as EmergencyOutageService,
        )

        self._emergency_outage_handler = EmergencyOutageService(
            self.hass, self.coordinator
        )
        service_definitions = self._build_service_definitions()
        from ..core.core_dev_mode import get_dev_mode as get_dev_mode

        if not bool(get_dev_mode(self.hass).get("active")):
            service_definitions = [
                definition
                for definition in service_definitions
                if "DEVELOPER ONLY" not in definition.description
                if "NUR FUER ENTWICKLER" not in definition.description
            ]
        for definition in service_definitions:
            self.hass.services.async_register(
                DOMAIN, definition.name, definition.handler, schema=definition.schema
            )
            self._registered_services.append(definition.name)
        _LOGGER.debug(f"Registered {len(service_definitions)} services")

    def unregister_all_services(self) -> None:
        """Unregister all services. @zara"""
        for service_name in self._registered_services:
            if self.hass.services.has_service(DOMAIN, service_name):
                self.hass.services.async_remove(DOMAIN, service_name)
        self._registered_services.clear()

    def _build_service_definitions(self) -> List[ServiceDefinition]:
        """Build all service definitions. @zara"""
        developer_warning = "DEVELOPER ONLY - potentially destructive! / NUR FUER ENTWICKLER - potenziell destruktiv! — "
        destructive_warning = (
            "DEVELOPER ONLY - DESTRUCTIVE! / NUR FUER ENTWICKLER - DESTRUKTIV! — "
        )
        return [
            ServiceDefinition(
                name=SERVICE_RETRAIN_AI_MODEL,
                handler=self._handle_retrain_ai_model,
                description=developer_warning
                + "Retrain TinyLSTM AI model with current data",
            ),
            ServiceDefinition(
                name=SERVICE_RESET_AI_MODEL,
                handler=self._handle_reset_ai_model,
                description="DEVELOPER ONLY / NUR FUER ENTWICKLER — Reinitialize AI model from persisted weights",
            ),
            ServiceDefinition(
                name=SERVICE_RUN_GRID_SEARCH,
                handler=self._handle_run_grid_search,
                description=destructive_warning
                + "Run Grid-Search hyperparameter optimization. Resets LSTM model",
            ),
            ServiceDefinition(
                name=SERVICE_ANALYZE_FEATURE_IMPORTANCE,
                handler=self._handle_analyze_feature_importance,
                description=developer_warning
                + "Analyze feature importance using Permutation Importance",
            ),
            ServiceDefinition(
                name=SERVICE_EMERGENCY_OUTAGE_QUARANTINE,
                handler=self._handle_emergency_outage_quarantine,
                description="EMERGENCY SERVICE - quarantines outage periods, blocks actual backfill, and can invalidate contaminated learning layers",
            ),
            ServiceDefinition(
                name=SERVICE_RUN_ALL_DAY_END_TASKS,
                handler=self._handle_run_all_day_end_tasks,
                description=destructive_warning
                + "Run ALL day-end tasks (EOD workflow)",
            ),
            ServiceDefinition(
                name=SERVICE_TEST_MORNING_ROUTINE,
                handler=self._handle_test_morning_routine,
                description=developer_warning
                + "Execute complete morning routine (overwrites forecasts)",
            ),
            ServiceDefinition(
                name=SERVICE_TEST_RETROSPECTIVE_FORECAST,
                handler=self._handle_test_retrospective_forecast,
                description=developer_warning
                + "Create retrospective forecast for today",
            ),
            ServiceDefinition(
                name=SERVICE_RUN_ADAPTIVE_FORECAST,
                handler=self._handle_run_adaptive_forecast,
                description=developer_warning
                + "Manually trigger adaptive midday forecast correction",
            ),
            ServiceDefinition(
                name=SERVICE_RUN_OPERATIONAL_REFORECAST,
                handler=self._handle_run_operational_reforecast,
                description=developer_warning
                + "Manually trigger operational intraday reforecast",
                schema=vol.Schema(
                    {
                        vol.Optional("cutoff_hour"): vol.All(
                            vol.Coerce(int), vol.Range(min=0, max=23)
                        )
                    }
                ),
            ),
            ServiceDefinition(
                name=SERVICE_SET_OPERATIONAL_REFORECAST_MODE,
                handler=self._handle_set_operational_reforecast_mode,
                description="Set the scheduled hybrid / operational reforecast mode. Standard is sufficient for most users and hardware-efficient. Every additional forecast triggers the AI stack again.",
                schema=vol.All(
                    vol.Schema(
                        {
                            vol.Required("mode"): vol.In(
                                [
                                    OPERATIONAL_REFORECAST_MODE_STANDARD,
                                    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
                                    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
                                    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
                                ]
                            ),
                            vol.Optional("custom_time"): str,
                            vol.Optional("enabled", default=False): bool,
                        }
                    ),
                    _validate_hybrid_forecast_service_payload,
                ),
            ),
            ServiceDefinition(
                name=SERVICE_RUN_WEATHER_CORRECTION,
                handler=self._handle_run_weather_correction,
                description=developer_warning
                + "Trigger corrected forecast generation (overwrites weather_forecast)",
            ),
            ServiceDefinition(
                name=SERVICE_REFRESH_MULTI_WEATHER,
                handler=self._handle_refresh_multi_weather,
                description=developer_warning
                + "Refresh Multi-Weather cache (5-source blending)",
            ),
            ServiceDefinition(
                name=SERVICE_BUILD_ASTRONOMY_CACHE,
                handler=self._handle_build_astronomy_cache,
                description=developer_warning + "Build astronomy cache for date range",
            ),
            ServiceDefinition(
                name=SERVICE_REFRESH_CACHE_TODAY,
                handler=self._handle_refresh_cache_today,
                description=developer_warning + "Refresh today's astronomy cache",
            ),
            ServiceDefinition(
                name=SERVICE_SEND_DAILY_BRIEFING,
                handler=self._handle_send_daily_briefing,
                description="Send daily solar weather briefing notification",
            ),
            ServiceDefinition(
                name=SERVICE_BACKFILL_SHADOW_DETECTION,
                handler=self._handle_backfill_shadow_detection,
                description=developer_warning
                + "Backfill shadow detection for missing hours",
            ),
            ServiceDefinition(
                name=SERVICE_REPAIR_TOOL,
                handler=self._handle_repair_tool,
                description=destructive_warning
                + "Controlled live database repair tool",
            ),
        ]

    async def _handle_retrain_ai_model(self, call: ServiceCall) -> None:
        """Handle retrain_ai_model service. @zara"""
        try:
            if self.coordinator.ai_predictor:
                _LOGGER.info("Service: retrain_ai_model - Starting AI training")
                training_result = await self.coordinator.ai_predictor.train_model()
                if training_result.success:
                    _LOGGER.info(
                        f"AI model training complete: R2={training_result.accuracy:.3f}, samples={training_result.samples_used}"
                    )
                else:
                    _LOGGER.error(
                        f"AI model training failed: {training_result.error_message}"
                    )
            else:
                _LOGGER.warning("AI predictor not available")
        except Exception as error:
            _LOGGER.error(f"Error in retrain_ai_model: {error}")
        return None if True else None

    async def _handle_reset_ai_model(self, call: ServiceCall) -> None:
        """Handle reset_ai_model service. @zara"""
        try:
            if self.coordinator.ai_predictor:
                _LOGGER.info(
                    "Service: reset_ai_model - reinitializing AI model from persisted weights"
                )
                succeeded = await self.coordinator.ai_predictor.initialize()
                if succeeded:
                    _LOGGER.info(
                        "AI model reinitialized; persisted model artifacts were retained"
                    )
                else:
                    _LOGGER.error("AI model reinitialization failed")
            else:
                _LOGGER.warning("AI predictor not available")
        except Exception as error:
            _LOGGER.error(f"Error in reset_ai_model: {error}")
        return None if True else None

    async def _handle_run_grid_search(self, call: ServiceCall) -> None:
        """Handle run_grid_search service - Run hyperparameter optimization. @zara

        The search never mutates the serving model. An optional retraining run
        creates a governed shadow candidate with the winning parameters.
        """
        from ..ai import GridSearchOptimizer as GridSearchOptimizer
        from ..ai.ai_grid_search import detect_hardware as detect_hardware

        _LOGGER.info(
            "SERVICE: run_grid_search - starting governed hyperparameter search"
        )
        _LOGGER.info("SERVICE: run_grid_search - Starting in background")

        async def _run_grid_search_background():
            try:
                hardware_info = await self.hass.async_add_executor_job(detect_hardware)
                _LOGGER.info(
                    f"Hardware: {hardware_info.architecture}, {hardware_info.cpu_count} CPUs"
                )
                if not hardware_info.grid_search_allowed:
                    _LOGGER.warning(
                        f"Grid-Search not available: {hardware_info.reason}"
                    )
                    return
                if not self.coordinator.ai_predictor:
                    _LOGGER.error("AI predictor not available")
                    return
                predictor = self.coordinator.ai_predictor
                optimizer = GridSearchOptimizer(
                    db_manager=predictor.db_manager, hardware_info=hardware_info
                )

                async def progress_callback(current, total, params, accuracy):
                    _LOGGER.info(
                        f"Grid-Search progress: {current}/{total} - hidden={params.get('hidden_sizes', params.get('hidden_size'))}, R2={accuracy:.4f}"
                    )

                retrain_after = call.data.get("retrain_after", True)
                search_result, candidate_result = (
                    await predictor.run_grid_search_candidate(
                        optimizer,
                        retrain_after=retrain_after,
                        progress_callback=progress_callback,
                    )
                )
                if search_result is None:
                    search_failure_reason = (
                        candidate_result.error_message
                        if candidate_result is not None
                        else "grid search did not start"
                    )
                    _LOGGER.info("Grid-Search not started: %s", search_failure_reason)
                    return
                if not search_result.success:
                    _LOGGER.error(f"Grid-Search failed: {search_result.error_message}")
                    return
                _LOGGER.info(
                    f"GRID-SEARCH COMPLETE: Best R2={search_result.best_accuracy:.4f}"
                )
                if retrain_after and search_result.best_params:
                    if candidate_result is not None and candidate_result.success:
                        _LOGGER.info(
                            "Optimized model candidate created: R2=%.4f",
                            candidate_result.accuracy,
                        )
                    else:
                        _LOGGER.info(
                            "Optimized candidate not created: %s",
                            (
                                candidate_result.error_message
                                if candidate_result is not None
                                else "candidate training was not requested"
                            ),
                        )
            except Exception as error:
                _LOGGER.error(f"Error in run_grid_search: {error}", exc_info=True)
            return None if True else None

        self.hass.async_create_task(
            _run_grid_search_background(), name="solar_forecast_ml_grid_search"
        )
        _LOGGER.info("Grid-Search started in background")

    async def _handle_analyze_feature_importance(self, call: ServiceCall) -> None:
        """Handle analyze_feature_importance service. @zara"""
        _LOGGER.info("SERVICE: analyze_feature_importance")
        try:
            if not self.coordinator.ai_predictor:
                _LOGGER.error("AI predictor not available")
                return
            predictor = self.coordinator.ai_predictor
            permutation_count = call.data.get("num_permutations", 5)

            async def progress_callback(current, total, feature_name):
                _LOGGER.debug(f"Feature Importance: {current}/{total} - {feature_name}")

            analysis_result = await predictor.analyze_feature_importance(
                num_permutations=permutation_count, progress_callback=progress_callback
            )
            if analysis_result is None or not analysis_result.success:
                analysis_error = (
                    analysis_result.error_message
                    if analysis_result
                    else "Unknown error"
                )
                _LOGGER.error(f"Feature Importance analysis failed: {analysis_error}")
                return
            _LOGGER.info(
                f"FEATURE IMPORTANCE COMPLETE: Baseline RMSE={analysis_result.baseline_rmse:.4f}"
            )
        except Exception as error:
            _LOGGER.error(
                f"Error in analyze_feature_importance: {error}", exc_info=True
            )
        return None if True else None

    async def _handle_run_all_day_end_tasks(self, call: ServiceCall) -> None:
        """Handle run_all_day_end_tasks service. @zara"""
        try:
            if hasattr(self.coordinator, "scheduled_tasks"):
                await self.coordinator.scheduled_tasks.end_of_day_workflow(None)
        except Exception as error:
            _LOGGER.error(f"Error in run_all_day_end_tasks: {error}")
        return None if True else None

    async def _handle_test_morning_routine(self, call: ServiceCall) -> None:
        """Handle test_morning_routine service. @zara"""
        _LOGGER.info("SERVICE: test_morning_routine called")
        try:
            if not hasattr(self.coordinator, "scheduled_tasks"):
                _LOGGER.error(
                    "SERVICE: test_morning_routine FAILED - scheduled_tasks not available on coordinator"
                )
                return
            if self.coordinator.scheduled_tasks is None:
                _LOGGER.error(
                    "SERVICE: test_morning_routine FAILED - scheduled_tasks is None"
                )
                return
            _LOGGER.info(
                "SERVICE: test_morning_routine - executing _execute_morning_routine()..."
            )
            succeeded = await self.coordinator.scheduled_tasks._execute_morning_routine(
                date=dt_util.now().date().isoformat(),
                forecast_cycle="manual",
                trigger_kind="manual",
            )
            if succeeded:
                _LOGGER.info("SERVICE: test_morning_routine COMPLETED SUCCESSFULLY")
            else:
                _LOGGER.error(
                    "SERVICE: test_morning_routine COMPLETED but returned False (check logs above)"
                )
        except Exception as error:
            _LOGGER.error(
                f"SERVICE: test_morning_routine EXCEPTION: {error}", exc_info=True
            )
        return None if True else None

    async def _handle_test_retrospective_forecast(self, call: ServiceCall) -> None:
        """Handle test_retrospective_forecast service - Create retrospective forecast. @zara"""
        _LOGGER.info("SERVICE: test_retrospective_forecast")
        try:
            sunrise = await self._get_sunrise_for_today()
            if not sunrise:
                _LOGGER.error("Could not determine sunrise time - using fallback 08:00")
                now = dt_util.now()
                sunrise = datetime.combine(
                    now.date(), datetime.min.time().replace(hour=8)
                )
                if now.tzinfo:
                    sunrise = sunrise.replace(tzinfo=now.tzinfo)
            simulation_time = sunrise - timedelta(hours=1)
            today = dt_util.now().date().isoformat()
            _LOGGER.info(f"Sunrise today: {sunrise.strftime('%H:%M')}")
            _LOGGER.info(f"Simulation time: {simulation_time.strftime('%H:%M')}")
            weather_service = self.coordinator.weather_service
            if not weather_service:
                _LOGGER.error("Weather service not available")
                return
            corrected_hourly_forecast = (
                await weather_service.get_corrected_hourly_forecast()
            )
            if not corrected_hourly_forecast:
                _LOGGER.error("No corrected weather forecast available")
                return
            current_weather = await weather_service.get_current_weather()
            external_sensor_data = (
                self.coordinator.sensor_collector.collect_all_sensor_data_dict()
            )
            forecast_result = (
                await self.coordinator.forecast_orchestrator.orchestrate_forecast(
                    current_weather=current_weather,
                    hourly_forecast=corrected_hourly_forecast,
                    external_sensors=external_sensor_data,
                    ml_prediction_today=None,
                    ml_prediction_tomorrow=None,
                    correction_factor=self.coordinator.learned_correction_factor,
                )
            )
            if not forecast_result or not forecast_result.get("hourly"):
                _LOGGER.error("Forecast generation failed")
                return
            hourly_predictions = forecast_result.get("hourly", [])
            predictions_for_today = [
                hourly_prediction
                for hourly_prediction in hourly_predictions
                if hourly_prediction.get("date") == today
            ]
            _LOGGER.info(
                f"Generated {len(predictions_for_today)} hourly predictions for today"
            )
            database = self.db_manager
            if database:
                for prediction in predictions_for_today:
                    await database.execute(
                        "INSERT OR REPLACE INTO retrospective_forecasts\n                           (date, hour, predicted_kwh, simulation_time, created_at)\n                           VALUES (?, ?, ?, ?, ?)",
                        (
                            today,
                            prediction.get("hour"),
                            prediction.get("production_kwh", 0.0),
                            simulation_time.isoformat(),
                            dt_util.now().isoformat(),
                        ),
                    )
            total_kwh = sum(
                (
                    hourly_prediction.get("production_kwh", 0.0) or 0.0
                    for hourly_prediction in predictions_for_today
                )
            )
            _LOGGER.info(f"RETROSPECTIVE FORECAST COMPLETE: {total_kwh:.2f} kWh")
        except Exception as error:
            _LOGGER.error(
                f"Error in test_retrospective_forecast: {error}", exc_info=True
            )
        return None if True else None

    async def _handle_run_adaptive_forecast(self, call: ServiceCall) -> None:
        """Handle run_adaptive_forecast service — manual trigger. @zara"""
        _LOGGER.info("SERVICE: run_adaptive_forecast — manual trigger")
        try:
            if (
                not hasattr(self.coordinator, "scheduled_tasks")
                or not self.coordinator.scheduled_tasks
            ):
                _LOGGER.error("Scheduled tasks not available")
                return
            adaptive_forecast_engine = getattr(
                self.coordinator.scheduled_tasks, "adaptive_forecast_engine", None
            )
            if not adaptive_forecast_engine:
                _LOGGER.error("Adaptive forecast engine not available")
                return
            await adaptive_forecast_engine.run_midday_check(manual=True)
            _LOGGER.info("SERVICE: run_adaptive_forecast — completed")
        except Exception as error:
            _LOGGER.error("Error in run_adaptive_forecast: %s", error, exc_info=True)
        return None if True else None

    async def _handle_run_operational_reforecast(self, call: ServiceCall) -> None:
        """Handle run_operational_reforecast service — manual trigger. @zara"""
        _LOGGER.info("SERVICE: run_operational_reforecast — manual trigger")
        try:
            if (
                not hasattr(self.coordinator, "scheduled_tasks")
                or not self.coordinator.scheduled_tasks
            ):
                _LOGGER.error("Scheduled tasks not available")
                return
            operational_reforecast_engine = getattr(
                self.coordinator.scheduled_tasks, "operational_reforecast_engine", None
            )
            if not operational_reforecast_engine:
                _LOGGER.error("Operational reforecast engine not available")
                return
            cutoff_hour = call.data.get("cutoff_hour")
            if cutoff_hour is not None:
                cutoff_hour = int(cutoff_hour)
            succeeded = await operational_reforecast_engine.run_reforecast(
                trigger_source="manual_service", cutoff_hour=cutoff_hour
            )
            if succeeded:
                _LOGGER.info("SERVICE: run_operational_reforecast — completed")
            else:
                _LOGGER.warning(
                    "SERVICE: run_operational_reforecast — no operational rewrite applied"
                )
        except Exception as error:
            _LOGGER.error(
                "Error in run_operational_reforecast: %s", error, exc_info=True
            )
        return None if True else None

    async def _handle_set_operational_reforecast_mode(self, call: ServiceCall) -> None:
        """Handle set_operational_reforecast_mode service. @zara"""
        _LOGGER.info("SERVICE: set_operational_reforecast_mode — manual trigger")
        try:
            data_manager = getattr(self.coordinator, "data_manager", None)
            operational_forecast_handler = getattr(
                data_manager, "operational_forecast_handler", None
            )
            if operational_forecast_handler is None:
                _LOGGER.error(
                    "Operational reforecast mode unavailable: no operational forecast handler"
                )
                return
            mode = call.data.get("mode", "standard")
            custom_time = call.data.get("custom_time")
            enabled = call.data.get("enabled", False)
            if mode == OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME and (not custom_time):
                _LOGGER.error(
                    "SERVICE: set_operational_reforecast_mode — custom_time required"
                )
                return
            settings_saved = (
                await operational_forecast_handler.save_reforecast_settings(
                    mode=mode,
                    custom_time=custom_time,
                    enabled=enabled,
                    updated_by="manual_service",
                    notes="Configured via Home Assistant service call",
                )
            )
            if not settings_saved:
                _LOGGER.error("SERVICE: set_operational_reforecast_mode — save failed")
                return
            saved_settings = (
                await operational_forecast_handler.get_reforecast_settings()
            )
            _LOGGER.info(
                "SERVICE: set_operational_reforecast_mode — saved mode=%s custom_time=%s enabled=%s",
                saved_settings.get("mode"),
                saved_settings.get("custom_time"),
                saved_settings.get("enabled"),
            )
            update_listeners = getattr(self.coordinator, "async_update_listeners", None)
            if callable(update_listeners):
                update_listeners()
        except Exception as error:
            _LOGGER.error(
                "Error in set_operational_reforecast_mode: %s", error, exc_info=True
            )
        return None if True else None

    async def _get_sunrise_for_today(self) -> Optional[datetime]:
        """Get sunrise time for today from astronomy cache. @zara"""
        try:
            from ..astronomy.astronomy_cache_manager import (
                get_cache_manager as get_cache_manager,
            )

            astronomy_cache = get_cache_manager()
            today = dt_util.now().date().isoformat()
            day_data = astronomy_cache.get_day_data(today)
            if not day_data:
                return
            sunrise_iso = day_data.get("sunrise_local")
            if not sunrise_iso:
                return
            sunrise = datetime.fromisoformat(sunrise_iso)
            if sunrise.tzinfo is None:
                local_timezone = dt_util.now().tzinfo
                if local_timezone:
                    sunrise = sunrise.replace(tzinfo=local_timezone)
            return sunrise
        except Exception as error:
            _LOGGER.error(f"Error getting sunrise: {error}")
            return

    async def _handle_run_weather_correction(self, call: ServiceCall) -> None:
        """Handle run_weather_correction service. @zara"""
        try:
            from ..data.data_weather_corrector import (
                WeatherForecastCorrector as WeatherForecastCorrector,
            )

            winter_mode = DEFAULT_WINTER_MODE
            if self.coordinator.config_entry:
                winter_mode = self.coordinator.config_entry.options.get(
                    CONF_WINTER_MODE, DEFAULT_WINTER_MODE
                )
            weather_corrector = WeatherForecastCorrector(
                self.hass,
                self.coordinator.data_manager._db_manager,
                winter_mode=winter_mode,
            )
            succeeded = await weather_corrector.create_corrected_forecast()
            if not succeeded:
                _LOGGER.warning("Corrected forecast generation failed")
        except Exception as error:
            _LOGGER.error(f"Error in run_weather_correction: {error}")
        return None if True else None

    async def _handle_refresh_multi_weather(self, call: ServiceCall) -> None:
        """Handle refresh_multi_weather service. @zara"""
        try:
            if not hasattr(self.coordinator, "weather_pipeline_manager"):
                _LOGGER.warning("Weather pipeline manager not available")
                return
            weather_pipeline = self.coordinator.weather_pipeline_manager
            force_update = call.data.get("force_update", False) or call.data.get(
                "force_wttr_refresh", False
            )
            _LOGGER.info(f"Service: refresh_multi_weather (force={force_update})")
            succeeded = await weather_pipeline.update_weather_cache(force=force_update)
            if succeeded:
                blend_stats = {}
                if weather_pipeline.weather_expert_blender:
                    blend_stats = (
                        weather_pipeline.weather_expert_blender.get_blend_stats()
                    )
                _LOGGER.info(
                    f"5-source weather refresh complete: {blend_stats.get('active_sources', 0)} sources active"
                )
            else:
                _LOGGER.warning("Weather refresh failed")
        except Exception as error:
            _LOGGER.error(f"Error in refresh_multi_weather: {error}", exc_info=True)
        return None if True else None

    async def _handle_build_astronomy_cache(self, call: ServiceCall) -> None:
        """Handle build_astronomy_cache service. @zara"""
        if self._astronomy_handler:
            await self._astronomy_handler.handle_build_astronomy_cache(call)
        return None if True else None

    async def _handle_refresh_cache_today(self, call: ServiceCall) -> None:
        """Handle refresh_cache_today service. @zara"""
        if self._astronomy_handler:
            await self._astronomy_handler.handle_refresh_cache_today(call)
        return None if True else None

    async def _handle_send_daily_briefing(self, call: ServiceCall) -> None:
        """Handle send_daily_briefing service. @zara"""
        _LOGGER.info("Service: send_daily_briefing")
        try:
            if not self._daily_briefing_handler:
                _LOGGER.error("Daily briefing handler not initialized")
                return
            notify_service = call.data.get("notify_service", "persistent_notification")
            language = call.data.get("language", "de")
            briefing_result = await self._daily_briefing_handler.send_daily_briefing(
                notify_service=notify_service, language=language
            )
            if briefing_result.get("success"):
                _LOGGER.info(
                    f"Daily briefing sent successfully: {briefing_result.get('title')}"
                )
            elif briefing_result.get("skipped"):
                _LOGGER.debug(
                    f"Daily briefing not sent: {briefing_result.get('error')}"
                )
            else:
                _LOGGER.error(
                    f"Failed to send daily briefing: {briefing_result.get('error')}"
                )
        except Exception as error:
            _LOGGER.error(
                f"Error in send_daily_briefing service: {error}", exc_info=True
            )
        return None if True else None

    async def _handle_emergency_outage_quarantine(self, call: ServiceCall) -> None:
        """Handle emergency_outage_quarantine service."""
        if not self._emergency_outage_handler:
            _LOGGER.error("Emergency outage handler not initialized")
            return
        await self._emergency_outage_handler.handle_emergency_outage_quarantine(call)

    async def _handle_repair_tool(self, call: ServiceCall) -> None:
        """Handle repair_tool service."""
        if not self._repair_tool_handler:
            _LOGGER.error("Repair tool handler not initialized")
            return
        await self._repair_tool_handler.handle_repair_tool(call)

    async def _handle_backfill_shadow_detection(self, call: ServiceCall) -> None:
        """Handle backfill_shadow_detection service. @zara V16.2

        Runs shadow detection for hours that have actual production data
        but no shadow detection results.
        """
        _LOGGER.info("Service: backfill_shadow_detection")
        try:
            target_date = call.data.get("target_date")
            if not target_date:
                target_date = dt_util.now().date().isoformat()
            _LOGGER.info(f"Backfilling shadow detection for {target_date}")
            if (
                not hasattr(self.coordinator, "scheduled_tasks")
                or not self.coordinator.scheduled_tasks
                or (not self.coordinator.scheduled_tasks.morning_routine_handler)
            ):
                _LOGGER.error("Morning routine handler not available")
                return
            morning_routine = self.coordinator.scheduled_tasks.morning_routine_handler
            missing_shadow_rows = await self.db_manager.fetchall(
                "SELECT hp.target_hour, hp.prediction_id, hp.actual_kwh,\n                          COALESCE(hp.inverter_clipped, 0), COALESCE(hp.mppt_throttled, 0)\n                   FROM hourly_predictions hp\n                   LEFT JOIN hourly_shadow_detection hsd ON hp.prediction_id = hsd.prediction_id\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n                     AND hsd.prediction_id IS NULL\n                     AND hp.target_hour >= 6\n                     AND hp.target_hour <= 20\n                   ORDER BY hp.target_hour",
                (target_date,),
            )
            if not missing_shadow_rows:
                _LOGGER.info(f"No missing shadow detection for {target_date}")
                return
            _LOGGER.info(
                f"Found {len(missing_shadow_rows)} hours missing shadow detection"
            )
            shadow_detector = morning_routine._shadow_detector
            if not shadow_detector:
                _LOGGER.error("Shadow detector not available")
                return
            processed_count = 0
            error_count = 0
            for prediction_row in missing_shadow_rows:
                target_hour = prediction_row[0]
                prediction_id = prediction_row[1]
                actual_kwh = prediction_row[2]
                inverter_clipped = bool(prediction_row[3])
                mppt_throttled = bool(prediction_row[4])
                try:
                    if hasattr(self.db_manager, "is_actual_backfill_blocked"):
                        if await self.db_manager.is_actual_backfill_blocked(
                            target_date, target_hour
                        ):
                            _LOGGER.warning(
                                "Shadow backfill skipped for emergency-quarantined hour: %s_%02d",
                                target_date,
                                target_hour,
                            )
                            continue
                    astronomy = await morning_routine._get_astronomy_for_hour(
                        target_date, target_hour
                    )
                    if not astronomy:
                        _LOGGER.debug(f"No astronomy data for hour {target_hour}")
                        continue
                    weather_row = await self.db_manager.fetchone(
                        "SELECT temperature_c, humidity_percent, cloud_cover_percent,\n                                  precipitation_mm, wind_speed_ms, snow_covered_panels,\n                                  frost_detected\n                           FROM hourly_weather_actual\n                           WHERE date = ? AND hour = ?",
                        (target_date, target_hour),
                    )
                    actual_weather = {}
                    if weather_row:
                        actual_weather = {
                            "temperature_c": weather_row[0],
                            "humidity_percent": weather_row[1],
                            "cloud_cover_percent": weather_row[2],
                            "precipitation_mm": weather_row[3],
                            "wind_speed_ms": weather_row[4],
                            "snow_covered_panels": weather_row[5],
                            "frost_detected": weather_row[6],
                        }
                    if not actual_weather.get("snow_covered_panels"):
                        hour_iso = f"{target_date}T{target_hour:02d}:00:00"
                        snow_tracking_row = await self.db_manager.fetchone(
                            "SELECT panels_covered_since, cleared_at\n                               FROM snow_tracking\n                               WHERE id = 1",
                            (),
                        )
                        if snow_tracking_row and snow_tracking_row[0]:
                            covered_since = snow_tracking_row[0]
                            cleared_at = snow_tracking_row[1]
                            if covered_since <= hour_iso and (
                                cleared_at is None or cleared_at > hour_iso
                            ):
                                actual_weather["snow_covered_panels"] = True
                                _LOGGER.debug(
                                    "Backfill: Snow detected from snow_tracking for %s hour %d",
                                    target_date,
                                    target_hour,
                                )
                    detection_context = {
                        "actual_kwh": actual_kwh,
                        "astronomy": astronomy,
                        "target_hour": target_hour,
                        "target_datetime": f"{target_date}T{target_hour:02d}:00:00",
                        "weather_actual": actual_weather,
                        "sensor_actual": {},
                        "flags": {
                            "inverter_clipped": inverter_clipped,
                            "mppt_throttled": mppt_throttled,
                        },
                    }
                    shadow_result = await shadow_detector.detect_shadow_ensemble(
                        detection_context
                    )
                    if shadow_result:
                        await self.db_manager.save_hourly_shadow_detection(
                            prediction_id, shadow_result
                        )
                        if self.coordinator.update_shadow_detection_cache(
                            prediction_id, shadow_result
                        ):
                            self.coordinator.async_update_listeners()
                        processed_count += 1
                        _LOGGER.debug(
                            "Shadow detection for %s hour %d: %s (%.1f%%)",
                            target_date,
                            target_hour,
                            shadow_result.get("shadow_type", "unknown"),
                            shadow_result.get("shadow_percent", 0),
                        )
                except Exception as hour_error:
                    error_count += 1
                    _LOGGER.warning(
                        f"Failed shadow detection for hour {target_hour}: {hour_error}"
                    )
            _LOGGER.info(
                f"Shadow detection backfill complete for {target_date}: {processed_count} processed, {error_count} errors"
            )
        except Exception as backfill_error:
            _LOGGER.error(
                f"Error in backfill_shadow_detection service: {backfill_error}",
                exc_info=True,
            )
        return None if True else None
