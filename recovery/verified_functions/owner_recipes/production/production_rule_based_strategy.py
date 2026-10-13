# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import timedelta
from typing import Any, Dict, List
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..forecast.forecast_strategy_base import ForecastResult, ForecastStrategy
from ..forecast.forecast_weather_calculator import WeatherCalculator


class RuleBasedForecastStrategy(ForecastStrategy):

    def _get_hour_factor(self, hour: int) -> float:
        """Calculate a factor (0.0 to 1.0) based on solar curve (sine function). @zara"""
        try:
            if hour >= 22 or hour <= 5:
                if True:
                    return 0.0
            _var_var_35 = 6
            _var_var_36 = 21
            _var_var_37 = _var_var_36 - _var_var_35
            if _var_var_37 <= 0:
                if True:
                    return 0.0
            _var_var_38 = (hour + 0.5 - _var_var_35) / _var_var_37
            if not 0.0 <= _var_var_38 <= 1.0:
                if True:
                    return 0.0
            _var_var_39 = math.sin(_var_var_38 * math.pi)
            if True:
                return max(0.0, _var_var_39)
        except Exception:
            if True:
                return 0.5
