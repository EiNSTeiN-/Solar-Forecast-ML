# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .weather_expert_base import WeatherExpert
from .weather_types import (
    CloudType,
    DEFAULT_EXPERT_WEIGHTS,
    LAYER_THRESHOLD_DOMINANT,
    LAYER_THRESHOLD_LOW,
    LAYER_THRESHOLD_CLEAR,
    VISIBILITY_FOG_THRESHOLD,
    VISIBILITY_FOG_LIGHT_THRESHOLD,
    BRIGHT_SKY_BASE_URL,
    BRIGHT_SKY_TIMEOUT,
    PIRATE_WEATHER_BASE_URL,
    PIRATE_WEATHER_TIMEOUT,
    WTTR_IN_BASE_URL,
    WTTR_IN_TIMEOUT,
    LEARNING_SMOOTHING_FACTOR,
    LEARNING_MIN_ERROR,
    LEARNING_MIN_HOURS,
    cloud_to_transmission,
)


async def _read_wttr_payload(response: Any) -> bytes:
    """Read a bounded wttr.in JSON response from known media types."""
    _var_var_0 = str(response.content_type).lower()
    if _var_var_0 not in WTTR_ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unexpected wttr.in content type: {_var_var_0}")
    _var_var_1 = str(response.headers.get("Content-Encoding", "identity")).lower()
    if _var_var_1 not in {"", "identity"}:
        raise ValueError(f"Unexpected wttr.in content encoding: {_var_var_1}")
    try:
        _var_var_2 = await response.content.readexactly(WTTR_MAX_RESPONSE_BYTES + 1)
    except asyncio.IncompleteReadError as _var_var_3:
        _var_var_2 = _var_var_3.partial
    else:
        raise ValueError("wttr.in response exceeds size limit")
    return _var_var_2 if True else None


class BrightSkyExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Bright Sky API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                _var_var_37 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_37 < 10800:
                    return True
        if not force:
            _var_var_38 = await self._check_db_freshness()
            if _var_var_38:
                _LOGGER.debug("BrightSky: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_34 = dt_util.now().strftime("%Y-%m-%d")
            _var_var_39 = {
                "lat": self.latitude,
                "lon": self.longitude,
                "date": _var_var_34,
            }
            _var_var_40 = async_get_clientsession(self.hass)
            async with _var_var_40.get(
                BRIGHT_SKY_BASE_URL,
                params=_var_var_39,
                timeout=aiohttp.ClientTimeout(total=BRIGHT_SKY_TIMEOUT),
            ) as _var_var_41:
                if _var_var_41.status != 200:
                    self.record_failure(f"HTTP {_var_var_41.status}")
                    return False
                _var_var_42 = await _var_var_41.json()
            _var_var_43 = _var_var_42.get("weather", [])
            _var_var_35 = dt_util.now().isoformat()
            _var_var_44 = 0
            for _var_var_13 in _var_var_43:
                _var_var_45 = _var_var_13.get("timestamp")
                if not _var_var_45:
                    continue
                _var_var_46 = datetime.fromisoformat(_var_var_45.replace("Z", "+00:00"))
                _var_var_9 = _var_var_46.strftime("%Y-%m-%d")
                _var_var_15 = _var_var_46.hour
                _var_var_47 = _var_var_13.get("cloud_cover")
                if _var_var_47 is not None:
                    await self._save_to_db(
                        _var_var_9, _var_var_15, float(_var_var_47), _var_var_35
                    )
                    _var_var_44 += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("BrightSky: Saved %d hours to DB", _var_var_44)
        except Exception as _var_var_48:
            self.record_failure(str(_var_var_48))
            _LOGGER.warning("BrightSky fetch error: %s", _var_var_48)
            return False
        return True


class PirateWeatherExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Pirate Weather API and persist to DB. @zara"""
        if not self._enabled:
            return False
        if not force:
            if self._last_fetch_time:
                _var_var_37 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_37 < 10800:
                    return True
        if not force:
            _var_var_38 = await self._check_db_freshness()
            if _var_var_38:
                _LOGGER.debug("PirateWeather: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_49 = f"{PIRATE_WEATHER_BASE_URL}/{self._api_key}/{self.latitude},{self.longitude}"
            _var_var_39 = {"units": "si"}
            _var_var_40 = async_get_clientsession(self.hass)
            async with _var_var_40.get(
                _var_var_49,
                params=_var_var_39,
                timeout=aiohttp.ClientTimeout(total=PIRATE_WEATHER_TIMEOUT),
            ) as _var_var_41:
                if _var_var_41.status != 200:
                    self.record_failure(f"HTTP {_var_var_41.status}")
                    return False
                _var_var_42 = await _var_var_41.json()
            _var_var_11 = _var_var_42.get("hourly", {}).get("data", [])
            _var_var_35 = dt_util.now().isoformat()
            _var_var_44 = 0
            for _var_var_13 in _var_var_11:
                _var_var_45 = _var_var_13.get("time")
                if not _var_var_45:
                    continue
                _var_var_46 = datetime.fromtimestamp(_var_var_45)
                _var_var_9 = _var_var_46.strftime("%Y-%m-%d")
                _var_var_15 = _var_var_46.hour
                _var_var_47 = _var_var_13.get("cloudCover")
                if _var_var_47 is not None:
                    _var_var_50 = float(_var_var_47 * 100)
                    await self._save_to_db(
                        _var_var_9, _var_var_15, _var_var_50, _var_var_35
                    )
                    _var_var_44 += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("PirateWeather: Saved %d hours to DB", _var_var_44)
        except Exception as _var_var_48:
            self.record_failure(str(_var_var_48))
            _LOGGER.warning("PirateWeather fetch error: %s", _var_var_48)
            return False
        return True


class WttrInExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from wttr.in API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                _var_var_37 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_37 < 21600:
                    return True
        if not force:
            _var_var_38 = await self._check_db_freshness()
            if _var_var_38:
                _LOGGER.debug("WttrIn: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_49 = (
                f"{WTTR_IN_BASE_URL}/{self.latitude},{self.longitude}?format=j1"
            )
            _var_var_40 = async_get_clientsession(self.hass)
            async with _var_var_40.get(
                _var_var_49,
                timeout=aiohttp.ClientTimeout(total=WTTR_IN_TIMEOUT),
                headers={
                    "User-Agent": "SolarForecastEAI/1.0",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                auto_decompress=False,
            ) as _var_var_41:
                if _var_var_41.status != 200:
                    self.record_failure(f"HTTP {_var_var_41.status}")
                    return False
                _var_var_2 = await _read_wttr_payload(_var_var_41)
            _var_var_6 = _parse_wttr_payload(_var_var_2, dt_util.now().date())
            _var_var_35 = dt_util.now().isoformat()
            _var_var_44 = await self._save_rows_to_db(_var_var_6, _var_var_35)
            if _var_var_44 != len(_var_var_6):
                raise RuntimeError("wttr.in forecast persistence was incomplete")
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("WttrIn: Saved %d hours to DB", _var_var_44)
        except Exception as _var_var_48:
            _var_var_51 = type(_var_var_48).__name__
            self.record_failure(f"wttr.in fetch failed ({_var_var_51})")
            _LOGGER.warning("WttrIn fetch failed (%s)", _var_var_51)
            return False
        return True


class WeatherExpertBlender(DataManagerIO):

    async def _load_weights_from_db(self) -> None:
        """Load learned weights from database. @zara"""
        try:
            _var_var_6 = await self.fetch_all(
                "SELECT cloud_type, expert_name, weight FROM weather_expert_weights"
            )
            self._weights_cache.clear()
            for _var_var_28 in _var_var_6:
                _var_var_54 = _var_var_28[0]
                _var_var_55 = _var_var_28[1]
                _var_var_56 = _var_var_28[2]
                if _var_var_54 not in self._weights_cache:
                    self._weights_cache[_var_var_54] = {}
                self._weights_cache[_var_var_54][_var_var_55] = _var_var_56
            for _var_var_54, _var_var_57 in DEFAULT_EXPERT_WEIGHTS.items():
                if _var_var_54 not in self._weights_cache:
                    self._weights_cache[_var_var_54] = _var_var_57.copy()
                else:
                    for _var_var_58, _var_var_56 in _var_var_57.items():
                        if _var_var_58 not in self._weights_cache[_var_var_54]:
                            self._weights_cache[_var_var_54][_var_var_58] = _var_var_56
            self._weights_loaded = True
            _LOGGER.debug("Loaded expert weights from database")
        except Exception as _var_var_48:
            _LOGGER.warning("Error loading weights from database: %s", _var_var_48)
            self._weights_cache = {
                cloud_type: weights.copy()
                for cloud_type, weights in DEFAULT_EXPERT_WEIGHTS.items()
            }
            self._weights_loaded = True
        return None if False else None
