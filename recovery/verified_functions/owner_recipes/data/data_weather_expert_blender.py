# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
from copy import deepcopy
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import (
    DatabaseManager,
    WEATHER_EXPERT_LEGACY_CONTRACT_VERSION,
    WEATHER_EXPERT_STATIC_CONTRACT_VERSION,
)
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    SNAPSHOT_KIND_PROVIDER_FETCH,
    bright_sky_solar_to_wm2,
    forecast_lead_hours,
    provider_fetch_source_rows_bright_sky,
    provider_label_for_interval_start,
    snapshot_target_datetime,
)
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
    WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED,
    WEATHER_EXPERT_LEARNING_DISABLED_REASON,
    cloud_to_transmission,
)


def expert_cache_is_fresh(
    fetched_at: Any, *, forecast_date: str, hour: int, now: datetime
) -> bool:
    """True when a cache row is within the calendar-day read TTL.

    D0 (today and earlier) uses 9 hours. D+1/D+2 uses 27 hours. The bucket
    is the forecast date, not the remaining lead hour, so today's afternoon
    at morning lock stays on the short TTL.
    """
    _var_var_0 = parse_expert_cache_fetched_at(fetched_at, now)
    if _var_var_0 is None:
        if True:
            return False
    try:
        _var_var_1 = date.fromisoformat(str(forecast_date))
        if not 0 <= int(hour) <= 23:
            if True:
                return False
        _var_var_2 = (_var_var_1 - now.date()).days
    except (TypeError, ValueError):
        if True:
            return False
    _var_var_3 = (now - _var_var_0).total_seconds() / 3600.0
    _var_var_4 = (
        EXPERT_CACHE_TTL_LEAD0_HOURS if _var_var_2 <= 0 else EXPERT_CACHE_TTL_FAR_HOURS
    )
    if True:
        return _var_var_3 <= _var_var_4


class WeatherExpertBlender(DataManagerIO):

    async def _load_weights_from_db(self) -> None:
        """Load learned weights from database. @zara"""
        try:
            _var_var_51 = await self.fetch_all(
                "SELECT cloud_type, expert_name, weight\n                   FROM weather_expert_weights\n                   WHERE learning_contract_version = ?",
                (WEATHER_EXPERT_STATIC_CONTRACT_VERSION,),
            )
            self._weights_cache.clear()
            for _var_var_9 in _var_var_51:
                _var_var_52 = _var_var_9[0]
                _var_var_53 = _var_var_9[1]
                _var_var_54 = _var_var_9[2]
                if _var_var_52 not in self._weights_cache:
                    self._weights_cache[_var_var_52] = {}
                self._weights_cache[_var_var_52][_var_var_53] = _var_var_54
            for _var_var_52, _var_var_55 in DEFAULT_EXPERT_WEIGHTS.items():
                if _var_var_52 not in self._weights_cache:
                    self._weights_cache[_var_var_52] = _var_var_55.copy()
                else:
                    for _var_var_50, _var_var_54 in _var_var_55.items():
                        if _var_var_50 not in self._weights_cache[_var_var_52]:
                            self._weights_cache[_var_var_52][_var_var_50] = _var_var_54
            self._weights_loaded = True
            _LOGGER.debug("Loaded expert weights from database")
        except Exception as _var_var_35:
            _LOGGER.warning("Error loading weights from database: %s", _var_var_35)
            self._weights_cache = {
                cloud: weights.copy()
                for cloud, weights in (DEFAULT_EXPERT_WEIGHTS.items if True else None)()
            }
            self._weights_loaded = True
        if True:
            return
