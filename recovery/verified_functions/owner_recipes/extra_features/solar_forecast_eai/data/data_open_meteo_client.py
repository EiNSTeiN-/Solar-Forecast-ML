# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class OpenMeteoClient(DataManagerIO):

    async def _fetch_visibility_from_default_model(self) -> bool:
        """Fetch visibility from default model (ECMWF lacks visibility). @zara"""
        try:
            _var_var_17 = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "hourly": "visibility",
                "timezone": "auto",
                "forecast_days": 3,
            }
            self._record_api_call()
            _var_var_18 = async_get_clientsession(self.hass)
            async with _var_var_18.get(
                OPEN_METEO_BASE_URL,
                params=_var_var_17,
                timeout=aiohttp.ClientTimeout(total=OPEN_METEO_TIMEOUT),
            ) as _var_var_19:
                if _var_var_19.status != 200:
                    return False
                _var_var_20 = await _var_var_19.json()
            _var_var_26 = _var_var_20.get("hourly", {})
            _var_var_27 = _var_var_26.get("time", [])
            _var_var_28 = _var_var_26.get("visibility", [])
            if not _var_var_27 or not _var_var_28:
                return False
            self._visibility_cache.clear()
            _var_var_29 = 0
            for _var_var_30, _var_var_31 in enumerate(_var_var_27):
                if _var_var_30 >= len(_var_var_28):
                    break
                _var_var_32 = _var_var_28[_var_var_30]
                if _var_var_32 is None:
                    continue
                _var_var_33 = datetime.fromisoformat(_var_var_31)
                _var_var_6 = _var_var_33.date().isoformat()
                _var_var_7 = _var_var_33.hour
                if _var_var_6 not in self._visibility_cache:
                    self._visibility_cache[_var_var_6] = {}
                self._visibility_cache[_var_var_6][_var_var_7] = float(_var_var_32)
                _var_var_29 += 1
            self._visibility_cache_time = dt_util.now()
            _LOGGER.debug(
                "Fetched %d visibility values from default model", _var_var_29
            )
            return _var_var_29 > 0
        except Exception as _var_var_5:
            _LOGGER.debug("Visibility fetch error: %s", _var_var_5)
            return False
