# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from ..const import WINTER_MONTHS, WINTER_CLOUD_PENALTY_FACTOR, WINTER_LOW_SUN_THRESHOLD


class WeatherForecastCorrector(DataManagerIO):

    async def _load_correction_factors(self) -> dict[str, Any]:
        """Load precision correction factors including hourly + weather-specific. @zara

        V16.1: Now loads three types of factors:
        - Daily rolling averages (baseline)
        - Hourly factors (hours 7-17) for solar and clouds
        - Weather-specific factors (clear/cloudy days)
        """
        _var_var_41 = {
            "temperature": 0.0,
            "solar_radiation_wm2": 1.0,
            "clouds": 1.0,
            "wind": 1.0,
            "humidity": 1.0,
            "rain": 1.0,
            "pressure": 0.0,
        }
        try:
            _var_var_39 = await self.fetch_one(
                "SELECT temperature_factor, solar_factor, cloud_factor,\n                          wind_factor, humidity_factor, rain_factor,\n                          pressure_factor, sample_days\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            _var_var_17 = _var_var_41.copy()
            _var_var_42 = 0
            if _var_var_39:
                _var_var_17 = {
                    "temperature": _var_var_39[0] or 0.0,
                    "solar_radiation_wm2": _var_var_39[1] or 1.0,
                    "clouds": _var_var_39[2] or 1.0,
                    "wind": _var_var_39[3] or 1.0,
                    "humidity": _var_var_39[4] or 1.0,
                    "rain": _var_var_39[5] or 1.0,
                    "pressure": _var_var_39[6] or 0.0,
                }
                _var_var_42 = _var_var_39[7] or 0
            _var_var_18 = {"solar_radiation_wm2": {}, "clouds": {}}
            _var_var_43 = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            for _var_var_44 in _var_var_43:
                _var_var_25, _var_var_45, _var_var_46, _var_var_47, _var_var_12 = (
                    _var_var_44
                )
                if _var_var_45 in _var_var_18:
                    _var_var_18[_var_var_45][str(_var_var_25)] = {
                        "factor": _var_var_46,
                        "samples": _var_var_47,
                        "confidence": _var_var_12 or 0.0,
                    }
            _var_var_19 = {"clear": {}, "cloudy": {}}
            _var_var_48 = await self.fetch_all(
                "SELECT weather_type, factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific"
            )
            for _var_var_49 in _var_var_48:
                _var_var_29, _var_var_45, _var_var_46, _var_var_12 = _var_var_49
                if _var_var_29 in _var_var_19 and _var_var_12 and (_var_var_12 >= 0.3):
                    _var_var_19[_var_var_29][_var_var_45] = _var_var_46
            return {
                "factors": _var_var_17,
                "hourly_factors": _var_var_18,
                "weather_factors": _var_var_19,
                "sample_days": _var_var_42,
            }
        except Exception as _var_var_35:
            _LOGGER.debug("Using default correction factors: %s", _var_var_35)
            return {
                "factors": _var_var_41,
                "hourly_factors": {"solar_radiation_wm2": {}, "clouds": {}},
                "weather_factors": {"clear": {}, "cloudy": {}},
                "sample_days": 0,
            }
