# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    DOMAIN,
    NAME,
    CONF_LOCATION_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_WIND_SPEED_SENSOR,
    CONF_WIND_DIRECTION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_PIRATE_WEATHER_API_KEY,
    DEFAULT_UPDATE_INTERVAL,
    LEARNING_MIN_DAYS,
    WMO_TO_HA_CONDITION,
    DEFAULT_CONDITION,
    DAY_TO_NIGHT_CONDITION,
    CLOUD_CLEAR_MAX,
    CLOUD_FAIR_MAX,
    CLOUD_MIXED_MAX,
)


class WeatherFusionCoordinator(DataUpdateCoordinator):

    async def _get_daily_forecast(
        self, hourly: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Generate daily forecast from hourly data."""
        _var_var_54 = []
        if not hourly:
            return _var_var_54
        _var_var_55 = {}
        for _var_var_56 in hourly:
            _var_var_57 = _var_var_56.get("datetime", "")
            if _var_var_57:
                try:
                    _var_var_58 = datetime.fromisoformat(
                        _var_var_57.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if _var_var_58.tzinfo is None:
                    _var_var_58 = _var_var_58.replace(tzinfo=self.time_zone)
                _var_var_25 = _var_var_58.astimezone(self.time_zone).date().isoformat()
                if _var_var_25 not in _var_var_55:
                    _var_var_55[_var_var_25] = []
                _var_var_55[_var_var_25].append(_var_var_56)
        _var_var_59 = {0: 95, 1: 90, 2: 82, 3: 72, 4: 62, 5: 52, 6: 45}
        for _var_var_60, _var_var_25 in enumerate(sorted(_var_var_55.keys())[:7]):
            _var_var_61 = _var_var_55[_var_var_25]
            if not _var_var_61:
                continue
            _var_var_63 = [
                _var_var_62.get("temperature")
                for _var_var_62 in _var_var_61
                if _var_var_62.get("temperature") is not None
            ]
            _var_var_64 = [
                _var_var_62.get("precipitation", 0) or 0 for _var_var_62 in _var_var_61
            ]
            _var_var_65 = [
                _var_var_62.get("precipitation_probability", 0) or 0
                for _var_var_62 in _var_var_61
            ]
            _var_var_66 = [
                _normalize_daily_condition(_var_var_62.get("condition"))
                for _var_var_62 in _var_var_61
                if _var_var_62.get("datetime")
                if 6 <= int(_var_var_62["datetime"][11:13]) <= 18
            ]
            if not _var_var_66:
                _var_var_66 = [
                    _normalize_daily_condition(_var_var_62.get("condition"))
                    for _var_var_62 in _var_var_61
                    if _var_var_62.get("condition")
                ]
            _var_var_67 = DEFAULT_CONDITION
            if _var_var_66:
                _var_var_67 = max(set(_var_var_66), key=_var_var_66.count)
            _var_var_49 = _var_var_59.get(_var_var_60, 40)
            _var_var_68 = (
                sum(
                    (_var_var_62.get("ai_confidence", 0) for _var_var_62 in _var_var_61)
                )
                / len(_var_var_61)
                if _var_var_61
                else 0
            )
            if _var_var_68 > 0:
                _var_var_52 = min(10, _var_var_68 * 0.1)
                _var_var_53 = min(98, _var_var_49 + _var_var_52)
            else:
                _var_var_53 = _var_var_49
            _var_var_54.append(
                {
                    "datetime": datetime.fromisoformat(f"{_var_var_25}T12:00:00")
                    .replace(tzinfo=self.time_zone)
                    .isoformat(),
                    "condition": _var_var_67,
                    "templow": round(min(_var_var_63), 1) if _var_var_63 else None,
                    "native_temperature": (
                        round(max(_var_var_63), 1) if _var_var_63 else None
                    ),
                    "precipitation": round(sum(_var_var_64), 1),
                    "precipitation_probability": round(max(_var_var_65)),
                    "wind_speed": (
                        _var_var_61[len(_var_var_61) // 2].get("wind_speed")
                        if _var_var_61
                        else None
                    ),
                    "wind_bearing": (
                        _var_var_61[len(_var_var_61) // 2].get("wind_bearing")
                        if _var_var_61
                        else None
                    ),
                    "confidence": round(_var_var_53),
                }
            )
            continue
        return _var_var_54

    def _is_night(self, target_dt: Optional[datetime] = None) -> bool:
        """Check if it's night based on sun.sun entity or time calculation.

        Args:
            target_dt: Target datetime to check. If None, checks current time.

        Returns:
            True if sun is below horizon, False otherwise.
        """
        _var_var_84 = self.hass.states.get(SUN_ENTITY_ID)
        if target_dt is None:
            if _var_var_84 is None:
                _var_var_26 = self._local_now().hour
                return _var_var_26 < 6 or _var_var_26 >= 20
            return _var_var_84.state == SUN_STATE_BELOW_HORIZON
        try:
            from astral.sun import sunrise as _var_var_85, sunset as _var_var_86
            from homeassistant.helpers.sun import get_astral_observer as _var_var_87
            from homeassistant.util import dt as _var_var_88

            _var_var_89 = target_dt
            if _var_var_89.tzinfo is None:
                _var_var_89 = _var_var_89.replace(tzinfo=_var_var_88.DEFAULT_TIME_ZONE)
            _var_var_89 = _var_var_88.as_local(_var_var_89)
            _var_var_90 = _var_var_87(self.hass)
            _var_var_91 = _var_var_88.as_local(
                _var_var_85(_var_var_90, _var_var_89.date())
            )
            _var_var_92 = _var_var_88.as_local(
                _var_var_86(_var_var_90, _var_var_89.date())
            )
            return _var_var_89 < _var_var_91 or _var_var_89 >= _var_var_92
        except Exception as _var_var_28:
            _LOGGER.debug("Error calculating daily sun position: %s", _var_var_28)
        if _var_var_84 is not None:
            _var_var_93 = _var_var_84.attributes.get("next_rising")
            _var_var_94 = _var_var_84.attributes.get("next_setting")
            if _var_var_93 and _var_var_94:
                try:
                    from homeassistant.util import dt as _var_var_88

                    _var_var_89 = target_dt
                    if _var_var_89.tzinfo is None:
                        _var_var_89 = _var_var_89.replace(
                            tzinfo=_var_var_88.DEFAULT_TIME_ZONE
                        )
                    _var_var_89 = _var_var_88.as_local(_var_var_89)
                    _var_var_95 = _var_var_88.parse_datetime(_var_var_93)
                    _var_var_96 = _var_var_88.parse_datetime(_var_var_94)
                    if _var_var_95 and _var_var_96:
                        _var_var_95 = _var_var_88.as_local(_var_var_95)
                        _var_var_96 = _var_var_88.as_local(_var_var_96)
                        _var_var_97 = _var_var_89.replace(
                            hour=_var_var_95.hour,
                            minute=_var_var_95.minute,
                            second=_var_var_95.second,
                            microsecond=0,
                        )
                        _var_var_98 = _var_var_89.replace(
                            hour=_var_var_96.hour,
                            minute=_var_var_96.minute,
                            second=_var_var_96.second,
                            microsecond=0,
                        )
                        return _var_var_89 < _var_var_97 or _var_var_89 >= _var_var_98
                except Exception as _var_var_28:
                    _LOGGER.debug("Error using sun entity fallback: %s", _var_var_28)
        return target_dt.hour < 6 or target_dt.hour >= 20
