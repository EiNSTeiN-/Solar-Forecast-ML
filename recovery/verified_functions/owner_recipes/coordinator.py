# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class SolarForecastMLCoordinator(DataUpdateCoordinator):

    async def _initialize_services(self) -> bool:
        """Initialize all services (weather, ML, error handler). @zara"""
        try:
            if self.learning_enabled and self.dependencies_ok:
                try:
                    _var_var_14 = self.hass.data.get(DOMAIN, {}).get(
                        "notification_service"
                    )
                    self.ai_predictor = AIPredictor(
                        hass=self.hass,
                        db_manager=self.data_manager._db_manager,
                        error_handler=self.error_handler,
                        notification_service=_var_var_14,
                        config_entry=self.entry,
                        panel_groups=self.panel_groups,
                        solar_capacity=self.solar_capacity,
                    )
                    self.ai_predictor.set_entities(
                        solar_capacity=self.solar_capacity,
                        power_entity=self.power_entity,
                        weather_entity=self.current_weather_entity,
                    )
                    _var_var_15 = await self.ai_predictor.initialize()
                    if _var_var_15:
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
                except Exception as _var_var_13:
                    _LOGGER.error(f"Failed to initialize AIPredictor: {_var_var_13}")
                    self.ai_predictor = None
            try:
                self.physics_calibrator = PhysicsCalibrator(
                    db_manager=self.data_manager._db_manager
                )
                await self.physics_calibrator.async_init()
                _var_var_16 = self.data_manager._db_manager
                if (
                    await _var_var_16.needs_physics_rebuild_after_learning_exclusion_repair()
                ):
                    _var_var_17 = await _var_var_16.count_physics_calibration_history()
                    if _var_var_17 > 0:
                        _LOGGER.info(
                            "Rebuilding physics calibration factors from %d cleaned history rows after learning-exclusion repair",
                            _var_var_17,
                        )
                        await self.physics_calibrator.recalculate_factors()
                    else:
                        _LOGGER.info(
                            "Physics learning-exclusion repair left no calibration history; starting with default physics factors"
                        )
                    await _var_var_16.mark_physics_rebuild_after_learning_exclusion_repair_done()
                _LOGGER.debug(
                    "PhysicsCalibrator initialized for self-learning corrections"
                )
            except Exception as _var_var_13:
                _LOGGER.warning(
                    f"Failed to initialize PhysicsCalibrator: {_var_var_13}"
                )
                self.physics_calibrator = None
            self._services_initialized = True
            return True
        except Exception as _var_var_13:
            _LOGGER.error(f"Failed to initialize services: {_var_var_13}")
            return False

    async def _background_initialization(self) -> None:
        """Initialize heavy components in background without blocking HA startup. @zara"""
        try:
            _LOGGER.debug("Background initialization started")
            try:
                from .data.data_shadow_pattern_learning import (
                    get_shadow_pattern_learner as _var_var_39,
                )

                _var_var_40 = _var_var_39(self.hass, self.data_manager._db_manager)
                if not _var_var_40._initialized:
                    _var_var_41 = await _var_var_40.async_init()
                    if not _var_var_41:
                        _LOGGER.warning("Shadow pattern model initialization failed")
                else:
                    _LOGGER.debug("Shadow pattern model already initialized")
            except Exception as _var_var_13:
                _LOGGER.warning("Shadow pattern model startup failed: %s", _var_var_13)
            try:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        _var_var_42 = await self._initialize_services()
                        if not _var_var_42:
                            _LOGGER.warning(
                                "Services initialization failed - will use rule-based forecasting"
                            )
            except Exception as _var_var_13:
                _LOGGER.warning(f"Failed to initialize services: {_var_var_13}")
            try:
                from .astronomy.astronomy_cache import AstronomyCache as _var_var_43
                from .data.data_weather_pipeline_manager import (
                    WeatherDataPipelineManager as _var_var_6,
                )

                _var_var_36 = _var_var_43(db_manager=self.data_manager._db_manager)
                if self.panel_groups:
                    _var_var_36.set_panel_groups(self.panel_groups)
                    _LOGGER.debug(
                        f"AstronomyCache: Panel groups set ({len(self.panel_groups)} groups)"
                    )
                _var_var_44 = self.hass.config.latitude
                _var_var_45 = self.hass.config.longitude
                _var_var_46 = str(self.hass.config.time_zone)
                _var_var_47 = self.hass.config.elevation or 0
                _var_var_36.initialize_location(
                    _var_var_44, _var_var_45, _var_var_46, _var_var_47
                )
                self.weather_pipeline_manager = _var_var_6(
                    hass=self.hass,
                    db_manager=self.data_manager._db_manager,
                    config_entry=self.entry,
                    coordinator=self,
                )
                from .astronomy.astronomy_cache_manager import (
                    get_cache_manager as _var_var_48,
                )

                _var_var_37 = _var_var_48(db_manager=self.data_manager._db_manager)
                _var_var_49 = await _var_var_37.initialize()
                if not _var_var_49:
                    _var_var_38 = self.entry.data.get("solar_capacity", 5.0)

                    async def _rebuild_cache_background():
                        try:
                            _LOGGER.info(
                                "Starting astronomy cache rebuild in background..."
                            )
                            await _var_var_36.rebuild_cache(
                                system_capacity_kwp=_var_var_38
                            )
                            await _var_var_37.initialize()
                            _LOGGER.info(
                                "Astronomy cache rebuild completed successfully"
                            )
                        except Exception as _var_var_63:
                            _LOGGER.error(
                                f"Failed to rebuild astronomy cache: {_var_var_63}"
                            )
                        return None if True else None

                    self.hass.async_create_task(
                        _rebuild_cache_background(),
                        name="solar_forecast_ml_cache_rebuild",
                    )
                    _LOGGER.info("Astronomy cache rebuild scheduled in background")
                _var_var_51 = await self.weather_pipeline_manager.async_setup()
                if not _var_var_51:
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
                            _var_var_64 = (
                                await self.weather_pipeline_manager.start_pipeline()
                            )
                            if not _var_var_64:
                                _LOGGER.warning(
                                    "Weather Data Pipeline failed to start - will retry on next update"
                                )
                            else:
                                _LOGGER.debug(
                                    "Weather Data Pipeline started successfully"
                                )
                        except Exception as _var_var_13:
                            _LOGGER.warning(
                                f"Weather Data Pipeline start failed: {_var_var_13} - will retry on next update"
                            )
                        return None if True else None

                    self.hass.async_create_task(
                        _start_pipeline_background(),
                        name="solar_forecast_ml_pipeline_start",
                    )
            except Exception as _var_var_13:
                _LOGGER.warning(
                    f"Weather pipeline initialization failed: {_var_var_13}"
                )
            try:
                await self._schedule_delayed_sensor_init()
            except Exception as _var_var_13:
                _LOGGER.warning(f"Delayed sensor init failed: {_var_var_13}")
            try:
                await self.scheduled_tasks.schedule_all_tasks()
            except Exception as _var_var_13:
                _LOGGER.warning(f"Task scheduling failed: {_var_var_13}")
            try:
                _var_var_7 = dt_util.now()
                _var_var_53 = _var_var_7.date().isoformat()
                _var_var_54 = (_var_var_7.date() + timedelta(days=1)).isoformat()
                _var_var_55 = (_var_var_7.date() + timedelta(days=2)).isoformat()
                _var_var_56 = await self.data_manager.get_hourly_predictions(
                    _var_var_53
                )
                _var_var_57 = await self.data_manager.get_hourly_predictions(
                    _var_var_54
                )
                _var_var_58 = await self.data_manager.get_hourly_predictions(
                    _var_var_55
                )
                _var_var_59 = None
                if _var_var_56:
                    _var_var_61 = [
                        _var_var_60
                        for _var_var_60 in _var_var_56
                        if (_var_var_60.get("prediction_kwh") or 0) > 0
                    ]
                    if _var_var_61:
                        _var_var_59 = max(
                            _var_var_61, key=lambda p: p.get("prediction_kwh", 0)
                        )
                self._hourly_predictions_cache = {
                    "predictions": _var_var_56 if _var_var_56 else [],
                    "predictions_tomorrow": _var_var_57 if _var_var_57 else [],
                    "predictions_day_after": _var_var_58 if _var_var_58 else [],
                    "best_hour_today": (
                        {
                            "hour": _var_var_59.get("target_hour"),
                            "kwh": _var_var_59.get("prediction_kwh"),
                        }
                        if _var_var_59
                        else {}
                    ),
                }
            except Exception as _var_var_13:
                _LOGGER.debug(f"Could not load hourly predictions cache: {_var_var_13}")
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
        except Exception as _var_var_13:
            _LOGGER.error(
                f"Background initialization failed: {_var_var_13}", exc_info=True
            )
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
                except Exception as _var_var_13:
                    _LOGGER.warning(
                        f"Error removing weekly retraining listener: {_var_var_13}"
                    )
            if self.panel_group_sensor_reader:
                try:
                    self.panel_group_sensor_reader.cleanup()
                except Exception as _var_var_65:
                    _LOGGER.warning(
                        f"Error cleaning up panel group sensor reader: {_var_var_65}"
                    )
            if self.data_manager:
                await self.data_manager.cleanup()
        except Exception as _var_var_13:
            _LOGGER.error(f"Error during coordinator shutdown: {_var_var_13}")
            raise
        return None if True else None

    async def _async_update_data(self):
        """Fetch data from API endpoint. @zara"""
        try:
            if not self._services_initialized:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        _var_var_42 = await self._initialize_services()
                        if not _var_var_42:
                            raise UpdateFailed("Failed to initialize services")
            if not self._forecast_orchestrator_initialized:
                await self._initialize_forecast_orchestrator()
                self._forecast_orchestrator_initialized = True
            from .core.core_coordinator_update_helpers import (
                CoordinatorUpdateHelpers as _var_var_80,
            )

            _var_var_81 = _var_var_80(self, self.data_manager._db_manager)
            await _var_var_81.handle_startup_recovery()
            _var_var_82, _var_var_83 = await _var_var_81.fetch_weather_data()
            _var_var_84 = self.sensor_collector.collect_all_sensor_data_dict()
            _var_var_85 = await _var_var_81.generate_forecast(
                _var_var_82, _var_var_83, _var_var_84
            )
            _var_var_86 = await _var_var_81.build_coordinator_result(
                _var_var_85, _var_var_82, _var_var_84
            )
            await self._update_sensor_properties(_var_var_86)
            await _var_var_81.save_forecasts(_var_var_85, _var_var_85.get("hourly", []))
            await self._refresh_hourly_predictions_cache()
            self._last_update_success_time = dt_util.now()
            _var_var_14 = self.hass.data.get(DOMAIN, {}).get("notification_service")
            _var_var_87 = _var_var_86.get(DATA_KEY_FORECAST_TODAY)
            if _var_var_14 is not None and isinstance(_var_var_87, (int, float)):
                try:
                    await _var_var_14.show_forecast_update(float(_var_var_87))
                except Exception:
                    _LOGGER.warning(
                        "Forecast update notification failed", exc_info=True
                    )
            if not self._startup_sensors_ready:
                self._startup_sensors_ready = True
            return _var_var_86
        except UpdateFailed:
            raise
        except Exception as _var_var_88:
            _LOGGER.error(f"Unexpected error updating data: {_var_var_88}")
            raise UpdateFailed(f"Error communicating with API: {_var_var_88}")

    async def _update_sensor_properties(self, data: Dict[str, Any]) -> None:
        """Update coordinator properties used by sensors. @zara"""
        try:
            if data.get("hourly_forecast"):
                _var_var_90 = data["hourly_forecast"]
                _var_var_91 = _var_var_90[0] if len(_var_var_90) > 0 else {}
                self.next_hour_pred = _var_var_91.get("production_kwh", 0.0)
            else:
                self.next_hour_pred = 0.0
            _var_var_92 = self.historical_calculator
            _var_var_93 = await _var_var_92.async_get_peak_production_time()
            self.peak_production_time_today = (
                _var_var_93 if _var_var_93 else "Calculating..."
            )
            _var_var_94 = self.production_time_calculator
            self.production_time_today = _var_var_94.get_production_time()
            _var_var_95 = data.get(DATA_KEY_EXTERNAL_SENSORS, {})
            _var_var_96 = getattr(self, "actual_live_state_manager", None)
            _var_var_97 = getattr(_var_var_96, "state", {}) if _var_var_96 else {}
            _var_var_98 = _var_var_97.get("total_actual_kwh")
            _var_var_99 = _var_var_95.get("total_consumption_today")
            if _var_var_98 is not None and _var_var_99 is not None:
                try:
                    if _var_var_99 > 0:
                        self.autarky_today = _var_var_98 / _var_var_99 * 100
                    else:
                        self.autarky_today = 0.0
                except (ValueError, TypeError, ZeroDivisionError):
                    self.autarky_today = None
            else:
                self.autarky_today = None
        except (ValueError, TypeError, AttributeError) as _var_var_13:
            _LOGGER.debug(f"Could not calculate autarky: {_var_var_13}")
            self.autarky_today = None
        _var_var_100 = self.ai_predictor
        if _var_var_100:
            await self.async_refresh_ai_diagnostic_snapshot()
            self.last_successful_learning = getattr(
                _var_var_100, "last_training_time", None
            )
            if self.model_accuracy is None:
                self.model_accuracy = getattr(_var_var_100, "current_accuracy", None)
            try:
                _var_var_101 = await self._get_training_ready_count()
                self._training_ready_count = _var_var_101
            except Exception:
                self._training_ready_count = 0
        else:
            self.ai_diagnostic_snapshot = {"source": "unavailable"}
            self.last_successful_learning = None
            self._training_ready_count = 0
        try:
            _var_var_7 = dt_util.now()
            _var_var_102 = _var_var_7.hour
            _var_var_103 = await self.data_manager._db_manager.fetchall(
                "SELECT hour, clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND clouds IS NOT NULL\n                   ORDER BY hour",
                (_var_var_7.date().isoformat(),),
            )
            if _var_var_103 and len(_var_var_103) >= 2:
                _var_var_105 = {
                    int(_var_var_104[0]): float(_var_var_104[1])
                    for _var_var_104 in _var_var_103
                }
                _var_var_106 = _var_var_105.get(_var_var_102)
                _var_var_107 = _var_var_105.get(_var_var_102 - 1)
                _var_var_108 = _var_var_105.get(_var_var_102 - 3)
                if _var_var_106 is not None and _var_var_107 is not None:
                    self.cloudiness_trend_1h = _var_var_106 - _var_var_107
                else:
                    self.cloudiness_trend_1h = 0.0
                if _var_var_106 is not None and _var_var_108 is not None:
                    self.cloudiness_trend_3h = _var_var_106 - _var_var_108
                else:
                    self.cloudiness_trend_3h = 0.0
                _var_var_110 = [
                    _var_var_105[_var_var_109]
                    for _var_var_109 in range(
                        max(0, _var_var_102 - 5), _var_var_102 + 1
                    )
                    if _var_var_109 in _var_var_105
                ]
                if len(_var_var_110) >= 3:
                    _var_var_89 = sum(_var_var_110) / len(_var_var_110)
                    _var_var_111 = sum(
                        (
                            (_var_var_118 - _var_var_89) ** 2
                            for _var_var_118 in _var_var_110
                        )
                    ) / len(_var_var_110)
                    self.cloudiness_volatility = round(_var_var_111**0.5, 1)
                else:
                    self.cloudiness_volatility = 0.0
            else:
                self.cloudiness_trend_1h = 0.0
                self.cloudiness_trend_3h = 0.0
                self.cloudiness_volatility = 0.0
        except Exception as _var_var_13:
            _LOGGER.debug(f"Could not compute cloudiness trends: {_var_var_13}")
            self.cloudiness_trend_1h = 0.0
            self.cloudiness_trend_3h = 0.0
            self.cloudiness_volatility = 0.0
        if self.last_day_error_kwh is None:
            try:
                _var_var_112 = (dt_util.now().date() - timedelta(days=1)).isoformat()
                _var_var_113 = await self.data_manager._db_manager.fetchone(
                    "SELECT predicted_kwh, actual_kwh FROM forecast_history WHERE date = ?",
                    (_var_var_112,),
                )
                if (
                    _var_var_113
                    and _var_var_113[0] is not None
                    and (_var_var_113[1] is not None)
                ):
                    _var_var_114 = float(_var_var_113[0] or 0.0)
                    _var_var_115 = float(_var_var_113[1] or 0.0)
                    _var_var_116 = abs(_var_var_114 - _var_var_115)
                    self.last_day_error_kwh = round(_var_var_116, 2)
                    if _var_var_115 > 0.1:
                        _var_var_32 = max(0.0, 1.0 - _var_var_116 / _var_var_115)
                        self.yesterday_accuracy = round(_var_var_32 * 100.0, 1)
                    elif _var_var_114 < 0.1:
                        self.yesterday_accuracy = 100.0
                    else:
                        self.yesterday_accuracy = 0.0
            except Exception as _var_var_13:
                _LOGGER.debug(f"Could not load yesterday deviation: {_var_var_13}")
        try:
            _var_var_117 = dt_util.now().date().replace(day=1).isoformat()
            _var_var_113 = await self.data_manager._db_manager.fetchone(
                "SELECT AVG(actual_total_kwh) FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (_var_var_117,),
            )
            if _var_var_113 and _var_var_113[0] is not None:
                self.avg_month_yield = round(float(_var_var_113[0]), 2)
        except Exception as _var_var_13:
            _LOGGER.debug(f"Could not load avg_month_yield: {_var_var_13}")
        try:
            _var_var_113 = await self.data_manager._db_manager.fetchone(
                "SELECT eod_duration_seconds FROM daily_summaries\n                   WHERE eod_duration_seconds IS NOT NULL\n                   ORDER BY date DESC LIMIT 1"
            )
            if _var_var_113 and _var_var_113[0] is not None:
                self.eod_duration_seconds = round(float(_var_var_113[0]), 1)
        except Exception as _var_var_13:
            _LOGGER.debug(f"Could not load eod_duration: {_var_var_13}")
        return None if True else None
