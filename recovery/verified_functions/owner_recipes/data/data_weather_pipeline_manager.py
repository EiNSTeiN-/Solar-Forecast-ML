# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class WeatherDataPipelineManager(DataManagerIO):

    async def stop_pipeline(self) -> None:
        """Stop all background tasks and listeners. @zara"""
        try:
            _LOGGER.info("Stopping Weather Data Pipeline...")
            for _var_var_13 in self._background_tasks:
                if not _var_var_13.done():
                    _var_var_13.cancel()
                    try:
                        await _var_var_13
                    except asyncio.CancelledError:
                        pass
            self._background_tasks.clear()
            for _var_var_11 in self._listeners:
                _var_var_11()
            self._listeners.clear()
            self._running = False
            _LOGGER.info("Weather Data Pipeline stopped")
        except Exception as _var_var_7:
            _LOGGER.error("Error stopping pipeline: %s", _var_var_7, exc_info=True)
        return None if False else None

    async def _refresh_all_weather_sources(self, force: bool = False) -> bool:
        """Refresh all 5 weather sources and create corrected forecast. @zara"""
        try:
            _LOGGER.info(
                "Pipeline: Refreshing all weather sources%s...",
                " (force)" if force else "",
            )
            _var_var_34 = False
            _var_var_35 = {}
            if self.weather_expert_blender:
                self.weather_expert_blender.reset_stale_cache_reads()
            try:
                await self.db.prune_expired_weather_caches()
            except Exception as _var_var_36:
                _LOGGER.debug("Pipeline: Weather cache prune skipped: %s", _var_var_36)
            if self.open_meteo_client:
                _var_var_19 = await self.open_meteo_client.get_hourly_forecast(
                    force=force
                )
                _var_var_35 = self.open_meteo_client._get_cache_source_info()
                if _var_var_19:
                    _var_var_37 = _var_var_35.get("source")
                    _var_var_38 = _var_var_35.get("status")
                    _LOGGER.debug(
                        "Pipeline: Open-Meteo data available (source=%s, status=%s, age=%s)",
                        _var_var_37,
                        _var_var_38,
                        _var_var_35.get("age_hours"),
                    )
                    _var_var_34 = _var_var_38 != "ok" or (
                        force and _var_var_37 != "api"
                    )
                else:
                    _LOGGER.warning(
                        "Pipeline: Open-Meteo fetch FAILED - using cached data"
                    )
                    _var_var_34 = True
            await self._fetch_expert_weather_sources(force=force)
            if self.weather_corrector:
                _var_var_29 = await self.weather_corrector.create_corrected_forecast()
                if _var_var_29:
                    _LOGGER.info("Pipeline: Corrected forecast updated")
                    if _var_var_34:
                        _var_var_37 = _var_var_35.get("source", "unknown")
                        _var_var_39 = _var_var_35.get("reason", "cached_fallback")
                        self._set_refresh_status(
                            "degraded", f"open_meteo_{_var_var_37}_{_var_var_39}"
                        )
                    elif (
                        self.weather_expert_blender
                        and self.weather_expert_blender.had_stale_cache_reads()
                    ):
                        self._set_refresh_status("degraded", "expert_cache_stale")
                    else:
                        _var_var_39 = _var_var_35.get("reason", "fresh_refresh")
                        self._set_refresh_status("ok", f"open_meteo_{_var_var_39}")
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
        except Exception as _var_var_7:
            _LOGGER.error(
                "Pipeline: Weather refresh error: %s", _var_var_7, exc_info=True
            )
            self._set_refresh_status("failed", "refresh_exception")
            self._set_corrected_forecast_status("failed", "refresh_exception")
            return False
