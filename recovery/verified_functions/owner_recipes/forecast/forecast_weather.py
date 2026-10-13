# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager
from ..data.data_open_meteo_client import OpenMeteoClient


class WeatherService:

    async def _background_forecast_update(self) -> None:
        """Background task to periodically update forecast. @zara"""
        _var_var_18 = 3600
        while True:
            try:
                await asyncio.sleep(_var_var_18)
                _LOGGER.debug("Background forecast update starting...")
                _var_var_19 = await self.force_update()
                if _var_var_19:
                    _LOGGER.debug(
                        f"Background update complete: {len(self._cached_forecast)} hours"
                    )
                else:
                    _LOGGER.debug("Background update: No new data")
            except asyncio.CancelledError:
                _LOGGER.info("Background forecast update task cancelled")
                break
            except Exception as _var_var_2:
                _LOGGER.error(
                    f"Background forecast update error: {_var_var_2}", exc_info=True
                )
        return None if True else None
