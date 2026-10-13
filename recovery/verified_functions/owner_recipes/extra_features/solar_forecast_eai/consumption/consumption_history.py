# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Any
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util


class ProductionCalculator:

    async def get_weather_correlation(self, days: int = 30) -> Dict[str, Any]:
        """Analyze correlation between weather and production. @zara"""
        try:
            _var_var_11 = (dt_util.now() - timedelta(days=days)).date()
            _var_var_2 = await self.db.fetchall(
                "SELECT\n                       hp.actual_kwh,\n                       pw.clouds,\n                       pw.temperature,\n                       pw.solar_radiation_wm2\n                   FROM hourly_predictions hp\n                   JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                   WHERE hp.target_date >= ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND pw.weather_type = 'actual'",
                (_var_var_11.isoformat(),),
            )
            if not _var_var_2:
                return {
                    "samples": 0,
                    "cloud_impact": None,
                    "temp_impact": None,
                    "radiation_correlation": None,
                }
            _var_var_15 = []
            _var_var_16 = []
            _var_var_17 = []
            _var_var_18 = []
            for _var_var_5 in _var_var_2:
                _var_var_19 = _var_var_5[0]
                _var_var_20 = _var_var_5[1]
                _var_var_21 = _var_var_5[2]
                if _var_var_20 is not None:
                    if _var_var_20 < 30:
                        _var_var_15.append(_var_var_19)
                    elif _var_var_20 > 70:
                        _var_var_16.append(_var_var_19)
                if _var_var_21 is not None:
                    if _var_var_21 > 25:
                        _var_var_17.append(_var_var_19)
                    elif _var_var_21 < 15:
                        _var_var_18.append(_var_var_19)
            _var_var_22 = None
            if _var_var_15 and _var_var_16:
                _var_var_23 = sum(_var_var_15) / len(_var_var_15)
                _var_var_24 = sum(_var_var_16) / len(_var_var_16)
                if _var_var_23 > 0:
                    _var_var_22 = round(
                        (_var_var_23 - _var_var_24) / _var_var_23 * 100, 1
                    )
            _var_var_25 = None
            if _var_var_17 and _var_var_18:
                _var_var_26 = sum(_var_var_17) / len(_var_var_17)
                _var_var_27 = sum(_var_var_18) / len(_var_var_18)
                if _var_var_27 > 0:
                    _var_var_25 = round(
                        (_var_var_26 - _var_var_27) / _var_var_27 * 100, 1
                    )
            return {
                "samples": len(_var_var_2),
                "cloud_impact": _var_var_22,
                "temp_impact": _var_var_25,
                "clear_sky_samples": len(_var_var_15),
                "cloudy_samples": len(_var_var_16),
            }
        except Exception as _var_var_8:
            _LOGGER.error("Error analyzing weather correlation: %s", _var_var_8)
            return {
                "samples": 0,
                "cloud_impact": None,
                "temp_impact": None,
                "radiation_correlation": None,
            }
