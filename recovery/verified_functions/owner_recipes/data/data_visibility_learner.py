# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from typing import Any, Dict, List, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .weather_types import (
    WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED,
    WEATHER_EXPERT_LEARNING_DISABLED_REASON,
)
from ..const import CONF_SOLAR_RADIATION_SENSOR
from ..core.core_helpers import SafeDateTimeUtil


class VisibilityLearner(DataManagerIO):

    async def learn_from_day(self, date_str: str) -> Dict[str, Any]:
        """Learn visibility weights from a day's actual observations. @zara

        Compares visibility predictions from Bright Sky and Pirate Weather
        with actual transmittance calculated from solar radiation measurements.

        Args:
            date_str: Date to learn from (YYYY-MM-DD format)

        Returns:
            Dict with learning results and statistics
        """
        _var_var_4 = {
            "success": False,
            "date": date_str,
            "reason": None,
            "fog_hours_detected": 0,
            "fog_light_hours_detected": 0,
            "bright_sky_accuracy": None,
            "pirate_weather_accuracy": None,
            "weights_updated": False,
        }
        if not WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED:
            _var_var_4["reason"] = WEATHER_EXPERT_LEARNING_DISABLED_REASON
            _var_var_4["success"] = True
            return _var_var_4
        if not self.has_solar_radiation_sensor:
            _var_var_4["reason"] = "No solar radiation sensor - using static weights"
            _var_var_4["success"] = True
            return _var_var_4
        try:
            _var_var_5 = await self._load_actual_weather_from_db(date_str)
            if not _var_var_5:
                _var_var_4["reason"] = "No actual weather data available"
                return _var_var_4
            _var_var_6 = await self._load_astronomy_from_db(date_str)
            if not _var_var_6:
                _var_var_4["reason"] = "No astronomy data for clear-sky reference"
                return _var_var_4
            _var_var_7 = await self._load_forecast_visibility(date_str)
            _var_var_8 = []
            _var_var_9 = []
            _var_var_10 = 0
            _var_var_11 = 0
            _var_var_12 = 0
            _var_var_13 = 0
            for _var_var_14, _var_var_15 in _var_var_5.items():
                _var_var_16 = _var_var_15.get("solar_radiation_wm2")
                if _var_var_16 is None or _var_var_16 < VISIBILITY_MIN_GHI:
                    continue
                _var_var_17 = _var_var_6.get(_var_var_14, {}).get(
                    "clear_sky_radiation_wm2"
                )
                if _var_var_17 is None or _var_var_17 < VISIBILITY_MIN_GHI:
                    continue
                _var_var_18 = min(1.0, _var_var_16 / _var_var_17)
                _var_var_19 = _var_var_15.get("cloud_cover_percent", 50)
                _var_var_20 = self._get_expected_transmittance(_var_var_19)
                _var_var_21 = _var_var_20 - _var_var_18
                _var_var_2 = None
                if (
                    _var_var_18 < TRANSMITTANCE_DENSE_FOG
                    and _var_var_21 > FOG_TRANSMITTANCE_DEFICIT
                ):
                    _var_var_2 = "fog"
                elif (
                    _var_var_18 < TRANSMITTANCE_LIGHT_FOG
                    and _var_var_21 > FOG_TRANSMITTANCE_DEFICIT
                ):
                    _var_var_2 = "fog_light"
                if _var_var_2 is None:
                    continue
                _var_var_22 = _var_var_7.get(_var_var_14, {})
                _var_var_23 = _var_var_22.get("bright_sky_visibility_m")
                _var_var_24 = _var_var_22.get("pirate_weather_visibility_m")
                _var_var_25 = {
                    "hour": _var_var_14,
                    "actual_ghi": round(_var_var_16, 1),
                    "clear_sky_ghi": round(_var_var_17, 1),
                    "transmittance": round(_var_var_18 * 100, 1),
                    "expected_transmittance": round(_var_var_20 * 100, 1),
                    "fog_type": _var_var_2,
                }
                if _var_var_2 == "fog":
                    _var_var_8.append(_var_var_25)
                else:
                    _var_var_9.append(_var_var_25)
                if _var_var_23 is not None:
                    _var_var_11 += 1
                    if _var_var_2 == "fog" and _var_var_23 < 1000:
                        _var_var_10 += 1
                    elif _var_var_2 == "fog_light" and _var_var_23 < 5000:
                        _var_var_10 += 1
                if _var_var_24 is not None:
                    _var_var_13 += 1
                    if _var_var_2 == "fog" and _var_var_24 < 1000:
                        _var_var_12 += 1
                    elif _var_var_2 == "fog_light" and _var_var_24 < 5000:
                        _var_var_12 += 1
            _var_var_4["fog_hours_detected"] = len(_var_var_8)
            _var_var_4["fog_light_hours_detected"] = len(_var_var_9)
            if _var_var_11 > 0:
                _var_var_4["bright_sky_accuracy"] = round(
                    _var_var_10 / _var_var_11 * 100, 1
                )
            if _var_var_13 > 0:
                _var_var_4["pirate_weather_accuracy"] = round(
                    _var_var_12 / _var_var_13 * 100, 1
                )
            _var_var_26 = len(_var_var_8) + len(_var_var_9)
            if _var_var_26 >= VISIBILITY_MIN_HOURS:
                await self._update_weights_from_accuracy(
                    _var_var_10,
                    _var_var_11,
                    _var_var_12,
                    _var_var_13,
                    len(_var_var_8),
                    len(_var_var_9),
                )
                _var_var_4["weights_updated"] = True
                self._learning_stats["last_learning_date"] = date_str
                self._learning_stats["total_fog_hours_learned"] += len(_var_var_8)
                self._learning_stats["total_fog_light_hours_learned"] += len(_var_var_9)
                self._learning_stats["bright_sky_fog_hits"] += _var_var_10
                self._learning_stats["pirate_weather_fog_hits"] += _var_var_12
                self._learning_stats["learning_sessions"] += 1
                await self._save_learning_data()
            _var_var_4["success"] = True
            _var_var_4["reason"] = f"Analyzed {_var_var_26} fog hours"
            if _var_var_26 > 0:
                _LOGGER.info(
                    "VisibilityLearner for %s: %d fog, %d fog_light hours. Accuracy: BrightSky=%.1f%%, PirateWeather=%.1f%%",
                    date_str,
                    len(_var_var_8),
                    len(_var_var_9),
                    _var_var_4["bright_sky_accuracy"] or 0,
                    _var_var_4["pirate_weather_accuracy"] or 0,
                )
            else:
                _LOGGER.debug(
                    "VisibilityLearner for %s: No fog conditions detected", date_str
                )
            return _var_var_4 if True else _var_var_4
        except Exception as _var_var_1:
            _var_var_4["reason"] = f"Error: {str(_var_var_1)}"
            _LOGGER.error("Error in visibility learning: %s", _var_var_1, exc_info=True)
            return _var_var_4
