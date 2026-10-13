# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class WeatherDataPipelineManager(DataManagerIO):

    async def stop_pipeline(self) -> None:
        """Stop all background tasks and listeners. @zara"""
        try:
            _LOGGER.info("Stopping Weather Data Pipeline...")
            for _var_var_8 in self._background_tasks:
                if not _var_var_8.done():
                    _var_var_8.cancel()
                    try:
                        await _var_var_8
                    except asyncio.CancelledError:
                        pass
            self._background_tasks.clear()
            for _var_var_7 in self._listeners:
                _var_var_7()
            self._listeners.clear()
            self._running = False
            _LOGGER.info("Weather Data Pipeline stopped")
        except Exception as _var_var_3:
            _LOGGER.error("Error stopping pipeline: %s", _var_var_3, exc_info=True)
        return None if True else None
