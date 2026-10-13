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


class BrightSkyExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Bright Sky API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                _var_var_17 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_17 < 10800:
                    return True
        if not force:
            _var_var_18 = await self._check_db_freshness()
            if _var_var_18:
                _LOGGER.debug("BrightSky: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_15 = dt_util.now().strftime("%Y-%m-%d")
            _var_var_19 = (dt_util.now() + timedelta(days=2)).strftime("%Y-%m-%d")
            _var_var_20 = {
                "lat": self.latitude,
                "lon": self.longitude,
                "date": _var_var_15,
                "last_date": _var_var_19,
            }
            async with aiohttp.ClientSession() as _var_var_21:
                async with _var_var_21.get(
                    BRIGHT_SKY_BASE_URL,
                    params=_var_var_20,
                    timeout=aiohttp.ClientTimeout(total=BRIGHT_SKY_TIMEOUT),
                ) as _var_var_22:
                    if _var_var_22.status != 200:
                        self.record_failure(f"HTTP {_var_var_22.status}")
                        return False
                    _var_var_23 = await _var_var_22.json()
            _var_var_24 = _var_var_23.get("weather", [])
            _var_var_16 = dt_util.now().isoformat()
            _var_var_25 = 0
            for _var_var_26 in _var_var_24:
                _var_var_27 = _var_var_26.get("timestamp")
                if not _var_var_27:
                    continue
                _var_var_28 = datetime.fromisoformat(_var_var_27.replace("Z", "+00:00"))
                _var_var_29 = (
                    dt_util.as_local(_var_var_28)
                    if _var_var_28.tzinfo is not None
                    else dt_util.ensure_local(_var_var_28)
                )
                _var_var_30 = _var_var_29.strftime("%Y-%m-%d")
                _var_var_31 = _var_var_29.hour
                _var_var_32 = _var_var_26.get("cloud_cover")
                _var_var_33 = _var_var_26.get("solar")
                _var_var_34 = _var_var_26.get("sunshine")
                if _var_var_32 is not None or _var_var_33 is not None:
                    await self._save_to_db(
                        _var_var_30,
                        _var_var_31,
                        float(_var_var_32) if _var_var_32 is not None else None,
                        self._solar_to_wm2(_var_var_33),
                        float(_var_var_34) if _var_var_34 is not None else None,
                        _var_var_16,
                    )
                    _var_var_25 += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("BrightSky: Saved %d hours to DB", _var_var_25)
            return True
        except Exception as _var_var_35:
            self.record_failure(str(_var_var_35))
            _LOGGER.warning("BrightSky fetch error: %s", _var_var_35)
            return False


class PirateWeatherExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Pirate Weather API and persist to DB. @zara"""
        if not self._enabled:
            return False
        if not force:
            if self._last_fetch_time:
                _var_var_17 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_17 < 10800:
                    return True
        if not force:
            _var_var_18 = await self._check_db_freshness()
            if _var_var_18:
                _LOGGER.debug("PirateWeather: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_40 = f"{PIRATE_WEATHER_BASE_URL}/{self._api_key}/{self.latitude},{self.longitude}"
            _var_var_20 = {"units": "si"}
            async with aiohttp.ClientSession() as _var_var_21:
                async with _var_var_21.get(
                    _var_var_40,
                    params=_var_var_20,
                    timeout=aiohttp.ClientTimeout(total=PIRATE_WEATHER_TIMEOUT),
                ) as _var_var_22:
                    if _var_var_22.status != 200:
                        self.record_failure(f"HTTP {_var_var_22.status}")
                        return False
                    _var_var_23 = await _var_var_22.json()
            _var_var_41 = _var_var_23.get("hourly", {}).get("data", [])
            _var_var_16 = dt_util.now().isoformat()
            _var_var_25 = 0
            for _var_var_26 in _var_var_41:
                _var_var_27 = _var_var_26.get("time")
                if not _var_var_27:
                    continue
                _var_var_42 = datetime.fromtimestamp(_var_var_27)
                _var_var_30 = _var_var_42.strftime("%Y-%m-%d")
                _var_var_31 = _var_var_42.hour
                _var_var_32 = _var_var_26.get("cloudCover")
                if _var_var_32 is not None:
                    _var_var_43 = float(_var_var_32 * 100)
                    await self._save_to_db(
                        _var_var_30, _var_var_31, _var_var_43, _var_var_16
                    )
                    _var_var_25 += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("PirateWeather: Saved %d hours to DB", _var_var_25)
            return True
        except Exception as _var_var_35:
            self.record_failure(str(_var_var_35))
            _LOGGER.warning("PirateWeather fetch error: %s", _var_var_35)
            return False


class WttrInExpert(WeatherExpert):

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from wttr.in API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                _var_var_17 = (dt_util.now() - self._last_fetch_time).total_seconds()
                if _var_var_17 < 21600:
                    return True
        if not force:
            _var_var_18 = await self._check_db_freshness()
            if _var_var_18:
                _LOGGER.debug("WttrIn: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            _var_var_40 = (
                f"{WTTR_IN_BASE_URL}/{self.latitude},{self.longitude}?format=j1"
            )
            async with aiohttp.ClientSession() as _var_var_21:
                async with _var_var_21.get(
                    _var_var_40,
                    timeout=aiohttp.ClientTimeout(total=WTTR_IN_TIMEOUT),
                    headers={"User-Agent": "SolarForecastML/1.0"},
                ) as _var_var_22:
                    if _var_var_22.status != 200:
                        self.record_failure(f"HTTP {_var_var_22.status}")
                        return False
                    _var_var_44 = await _var_var_22.text()
                    try:
                        _var_var_23 = json.loads(_var_var_44)
                    except json.JSONDecodeError:
                        _var_var_45 = _var_var_22.headers.get("Content-Type", "unknown")
                        self.record_failure(f"invalid_json content_type={_var_var_45}")
                        _LOGGER.debug(
                            "WttrIn returned non-JSON response: status=%s content_type=%s body_prefix=%r",
                            _var_var_22.status,
                            _var_var_45,
                            _var_var_44[:120],
                        )
                        return False
            _var_var_46 = _var_var_23.get("weather", [])
            _var_var_16 = dt_util.now().isoformat()
            _var_var_25 = 0
            for _var_var_47 in _var_var_46:
                _var_var_30 = _var_var_47.get("date")
                if not _var_var_30:
                    continue
                for _var_var_26 in _var_var_47.get("hourly", []):
                    _var_var_48 = _var_var_26.get("time", "0")
                    try:
                        _var_var_31 = int(_var_var_48) // 100
                    except (ValueError, TypeError):
                        continue
                    _var_var_32 = _var_var_26.get("cloudcover")
                    if _var_var_32 is not None:
                        _var_var_49 = {
                            "cloud_cover": float(_var_var_32),
                            "temperature": self._safe_float(_var_var_26.get("tempC")),
                            "humidity": self._safe_float(_var_var_26.get("humidity")),
                            "wind_speed": self._safe_float(
                                _var_var_26.get("windspeedKmph")
                            ),
                            "precipitation": self._safe_float(
                                _var_var_26.get("precipMM")
                            ),
                            "pressure": self._safe_float(_var_var_26.get("pressure")),
                        }
                        await self._save_to_db(
                            _var_var_30, _var_var_31, _var_var_49, _var_var_16
                        )
                        _var_var_25 += 1
                continue
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("WttrIn: Saved %d hours to DB", _var_var_25)
            return True
        except Exception as _var_var_35:
            self.record_failure(str(_var_var_35))
            _LOGGER.warning("WttrIn fetch error: %s", _var_var_35)
            return False


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


class WeatherExpertLearner(DataManagerIO):

    async def learn_from_day(self, date_str: str) -> bool:
        """Learn expert weights from a day's data - PER CLOUD TYPE. @zara V16.1

        Compares forecasted cloud cover vs actual measured cloud cover
        and updates expert weights based on accuracy FOR EACH CLOUD TYPE.

        FIX: Now learns separately for each cloud type bucket instead of
        only the dominant type for the day.

        Args:
            date_str: Date to learn from (YYYY-MM-DD)

        Returns:
            True if learning was successful
        """
        if not self.learning_enabled:
            _LOGGER.info(
                "Weather expert learning skipped: %s", self.learning_disabled_reason
            )
            return False
        _var_var_80 = deepcopy(self._blender._weights_cache)
        try:
            _var_var_87 = await self.fetch_all(
                "SELECT wa.hour, wa.cloud_cover_percent,\n                          COALESCE(hp.exclude_from_learning, 0),\n                          COALESCE(hp.mppt_throttled, 0),\n                          COALESCE(hp.inverter_clipped, 0),\n                          COALESCE(wa.snow_event_detected, 0)\n                   FROM hourly_weather_actual wa\n                   LEFT JOIN hourly_predictions hp ON hp.target_date = wa.date AND hp.target_hour = wa.hour\n                   WHERE wa.date = ? AND wa.cloud_cover_percent IS NOT NULL",
                (date_str,),
            )
            if len(_var_var_87) < LEARNING_MIN_HOURS:
                _LOGGER.debug(
                    "Not enough data for learning: %d hours (need %d)",
                    len(_var_var_87),
                    LEARNING_MIN_HOURS,
                )
                return False
            _var_var_88 = {}
            _var_var_89 = {}
            for _var_var_90 in _var_var_87:
                _var_var_31 = _var_var_90[0]
                _var_var_91 = _var_var_90[1]
                _var_var_92 = _var_var_90[2]
                _var_var_93 = _var_var_90[3]
                _var_var_94 = _var_var_90[4]
                _var_var_95 = _var_var_90[5]
                if _var_var_92 or _var_var_93 or _var_var_94:
                    _LOGGER.info(
                        "Skipping weather expert learning for %s hour %d due to curtailment/exclusion",
                        date_str,
                        _var_var_31,
                    )
                    continue
                _var_var_52 = cloud_type_from_actual(_var_var_91, bool(_var_var_95))
                _var_var_81 = _var_var_52.value
                _var_var_89[_var_var_81] = _var_var_89.get(_var_var_81, 0) + 1
                if _var_var_81 not in _var_var_88:
                    _var_var_88[_var_var_81] = {}
                for _var_var_50 in self._blender._experts:
                    if not self._blender._is_expert_enabled(_var_var_50):
                        continue
                    try:
                        _var_var_96 = await _var_var_50.get_cloud_cover(
                            date_str, _var_var_31
                        )
                        if _var_var_96 is not None:
                            _var_var_97 = abs(_var_var_96 - _var_var_91)
                            if _var_var_50.name not in _var_var_88[_var_var_81]:
                                _var_var_88[_var_var_81][_var_var_50.name] = []
                            _var_var_88[_var_var_81][_var_var_50.name].append(
                                _var_var_97
                            )
                    except Exception:
                        continue
                continue
            _var_var_98 = 0
            _var_var_99 = []
            for _var_var_81, _var_var_100 in _var_var_88.items():
                _var_var_101 = _var_var_89.get(_var_var_81, 0)
                if _var_var_101 < 2:
                    continue
                _var_var_52 = CloudType(_var_var_81)
                _var_var_102 = False
                for _var_var_53, _var_var_103 in _var_var_100.items():
                    if len(_var_var_103) < 2:
                        continue
                    _var_var_104 = sum(_var_var_103) / len(_var_var_103)
                    _var_var_105 = await self._blender.update_expert_weight(
                        _var_var_52, _var_var_53, _var_var_104, persist=False
                    )
                    if not _var_var_105:
                        continue
                    _var_var_102 = True
                    _var_var_106 = self._blender._weights_cache[_var_var_81][
                        _var_var_53
                    ]
                    _var_var_99.append(
                        (date_str, _var_var_81, _var_var_53, _var_var_104, _var_var_106)
                    )
                if _var_var_102:
                    _var_var_98 += 1
            if _var_var_99:
                await self._persist_learning_batch(_var_var_99)
            _LOGGER.info(
                "Learned from %s: %d hours across %d cloud types (buckets: %s)",
                date_str,
                len(_var_var_87),
                _var_var_98,
                ", ".join(
                    (
                        f"{_var_var_56}:{_var_var_57}h"
                        for _var_var_56, _var_var_57 in _var_var_89.items()
                    )
                ),
            )
            if self._visibility_learner:
                try:
                    _var_var_107 = await self._visibility_learner.learn_from_day(
                        date_str
                    )
                    if _var_var_107 and _var_var_107.get("success"):
                        _var_var_108 = _var_var_107.get("fog_hours_detected", 0)
                        _var_var_109 = _var_var_107.get("fog_light_hours_detected", 0)
                        if _var_var_108 > 0 or _var_var_109 > 0:
                            _LOGGER.info(
                                "Visibility learning for %s: %d fog, %d fog_light hours",
                                date_str,
                                _var_var_108,
                                _var_var_109,
                            )
                except Exception as _var_var_110:
                    _LOGGER.warning("Visibility learning error: %s", _var_var_110)
            if True:
                return True
            else:
                return True
        except BaseException:
            self._blender._weights_cache = _var_var_80
            _LOGGER.exception("Error learning from day %s", date_str)
            raise
