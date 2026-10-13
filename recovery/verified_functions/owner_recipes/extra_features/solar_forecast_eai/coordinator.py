# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class SolarForecastEAICoordinator(DataUpdateCoordinator):

    async def _initialize_services(self) -> bool:
        """Initialize all services (weather, ML, error handler). @zara"""
        try:
            if self.learning_enabled and self.dependencies_ok:
                try:
                    _var_var_9 = self.hass.data.get(DOMAIN, {}).get(
                        "notification_service"
                    )
                    self.ai_predictor = AIPredictor(
                        hass=self.hass,
                        db_manager=self.data_manager._db_manager,
                        error_handler=self.error_handler,
                        notification_service=_var_var_9,
                        config_entry=self.entry,
                        panel_groups=None,
                        solar_capacity=self.heating_capacity_kw,
                    )
                    self.ai_predictor.set_entities(
                        solar_capacity=self.heating_capacity_kw,
                        power_entity=self.wp_power_entity,
                        weather_entity=self.current_weather_entity,
                    )
                    _var_var_10 = await self.ai_predictor.initialize()
                    if _var_var_10:
                        self._ml_ready = True
                        self.best_hour_calculator.ai_predictor = self.ai_predictor
                    else:
                        _LOGGER.error("AIPredictor initialization failed")
                        self.ai_predictor = None
                except Exception as _var_var_8:
                    _LOGGER.error(f"Failed to initialize AIPredictor: {_var_var_8}")
                    self.ai_predictor = None
            try:
                self.thermodynamics_calibrator = ThermodynamicsCalibrator(
                    db_manager=self.data_manager._db_manager
                )
                await self.thermodynamics_calibrator.load_from_database()
                _LOGGER.info(
                    "ThermodynamicsCalibrator initialized for self-learning corrections"
                )
            except Exception as _var_var_8:
                _LOGGER.warning(
                    f"Failed to initialize ThermodynamicsCalibrator: {_var_var_8}"
                )
                self.thermodynamics_calibrator = None
            self._services_initialized = True
            return True
        except Exception as _var_var_8:
            _LOGGER.error(f"Failed to initialize services: {_var_var_8}")
            return False

    async def async_shutdown(self) -> None:
        """Cleanup coordinator resources. @zara

        Removes all event listeners to prevent accumulation on reload.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        try:
            for _var_var_36, _var_var_37 in (
                ("_unsub_startup_listener", "startup"),
                ("_unsub_power_peak_listener", "power peak tracking"),
                ("_unsub_weekly_retraining_listener", "weekly retraining"),
            ):
                _var_var_38 = getattr(self, _var_var_36)
                if _var_var_38 is None:
                    continue
                setattr(self, _var_var_36, None)
                try:
                    _var_var_38()
                    _LOGGER.debug("%s listener removed", _var_var_37.capitalize())
                except Exception as _var_var_8:
                    _LOGGER.warning(
                        "Error removing %s listener: %s", _var_var_37, _var_var_8
                    )
            await super().async_shutdown()
            _var_var_39 = asyncio.current_task()
            _var_var_40 = [
                _var_var_6
                for _var_var_6 in self._background_tasks
                if _var_var_6 is not _var_var_39
                if not _var_var_6.done()
            ]
            for _var_var_6 in _var_var_40:
                _var_var_6.cancel()
            if _var_var_40:
                await asyncio.gather(*_var_var_40, return_exceptions=True)
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
        except Exception as _var_var_8:
            _LOGGER.error(f"Error during coordinator shutdown: {_var_var_8}")
        return None if True else None
