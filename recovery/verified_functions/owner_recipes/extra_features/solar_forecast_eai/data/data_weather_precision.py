# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class WeatherPrecisionTracker(DataManagerIO):

    async def get_hourly_correction_factors(self) -> dict[str, dict[str, Any]]:
        """Get hourly correction factors for solar_radiation_wm2 and clouds. @zara

        V16.1: Returns hourly factors that override daily factors for specific hours.

        Returns:
            Dict with structure:
            {
                "solar_radiation_wm2": {
                    "9": {"factor": 1.282, "samples": 10, "confidence": 0.65},
                    ...
                },
                "clouds": {...},
                "min_samples": 3
            }
        """
        try:
            _var_var_17 = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence, std_dev\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            _var_var_18 = {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
            for _var_var_19 in _var_var_17:
                (
                    _var_var_12,
                    _var_var_113,
                    _var_var_114,
                    _var_var_115,
                    _var_var_110,
                    _var_var_108,
                ) = _var_var_19
                if _var_var_113 in _var_var_18:
                    _var_var_18[_var_var_113][str(_var_var_12)] = {
                        "factor": _var_var_114,
                        "samples": _var_var_115,
                        "confidence": _var_var_110 or 0.0,
                        "std_dev": _var_var_108,
                    }
            return _var_var_18
        except Exception as _var_var_8:
            _LOGGER.error("Error getting hourly correction factors: %s", _var_var_8)
            return {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
