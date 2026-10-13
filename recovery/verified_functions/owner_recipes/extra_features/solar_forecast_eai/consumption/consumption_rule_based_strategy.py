# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import timedelta
from math import isfinite
from typing import Any, Dict, List, Optional
from ..const import DEFAULT_COP_RATED, DEFAULT_WP_TYPE, SUPPORTED_WP_TYPES
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..forecast.forecast_strategy_base import ForecastResult, ForecastStrategy
from ..forecast.forecast_weather_calculator import WeatherCalculator
from ..setup_state import cop_sink_kwargs, dhw_forecast_kwargs
from ..thermodynamics.thermodynamics_engine import (
    ThermodynamicsEngine,
    WeatherConditions,
)


class RuleBasedForecastStrategy(ForecastStrategy):

    async def calculate_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        **kwargs
    ) -> ForecastResult:
        """Calculate consumption forecast using temperature-based rules. @zara"""
        _LOGGER.debug("Calculating forecast using Rule-based (Consumption) strategy...")
        try:
            _var_var_0 = 0.0
            _var_var_1 = 0.0
            _var_var_2 = dt_util.now()
            _var_var_3 = _var_var_2.date()
            _var_var_4 = _var_var_3 + timedelta(days=1)
            for _var_var_5 in hourly_weather_forecast:
                try:
                    _var_var_6 = _var_var_5.get("local_datetime")
                    if not _var_var_6:
                        _LOGGER.warning("Skipping hour, missing 'local_datetime'")
                        continue
                    if isinstance(_var_var_6, str):
                        _var_var_6 = dt_util.parse_datetime(_var_var_6)
                        if not _var_var_6:
                            continue
                    _var_var_7 = _var_var_6.date()
                    _var_var_8 = _var_var_6.hour
                    _var_var_9 = _var_var_5.get("temperature", 5)
                    _var_var_10 = _var_var_5.get("wind_speed", 3)
                    _var_var_11 = self._calculate_hourly_consumption(
                        _var_var_9, _var_var_10, _var_var_8, correction_factor
                    )
                    if _var_var_7 == _var_var_3:
                        _var_var_0 += _var_var_11
                    elif _var_var_7 == _var_var_4:
                        _var_var_1 += _var_var_11
                except Exception as _var_var_12:
                    _LOGGER.warning(
                        "Failed to process hour %s: %s",
                        _var_var_5.get("local_hour"),
                        _var_var_12,
                    )
                    continue
            _LOGGER.debug(
                "Rule-based iteration complete. Today (raw): %.2f kWh, Tomorrow (raw): %.2f kWh",
                _var_var_0,
                _var_var_1,
            )
            _var_var_13 = self.heating_capacity_kw * self.MAX_REALISTIC_DAILY_KWH_PER_KW
            _var_var_14 = max(0.0, min(_var_var_0, _var_var_13))
            _var_var_15 = max(0.0, min(_var_var_1, _var_var_13))
            try:
                _var_var_16 = sensor_data.get("current_yield")
                if _var_var_16 is not None and _var_var_16 > 0:
                    _var_var_17 = float(_var_var_16)
                    if _var_var_17 > _var_var_14:
                        _var_var_18 = max(0, 24 - _var_var_2.hour)
                        _var_var_19 = _var_var_14 / 24.0 if _var_var_14 > 0 else 0.5
                        _var_var_20 = _var_var_19 * _var_var_18
                        _var_var_14 = _var_var_17 + _var_var_20
                        _LOGGER.info(
                            "Consumption adjustment: Current %.2f kWh > Forecast %.2f kWh -> Adjusted %.2f kWh",
                            _var_var_17,
                            _var_var_0,
                            _var_var_14,
                        )
            except Exception as _var_var_21:
                _LOGGER.debug("Consumption adjustment check failed: %s", _var_var_21)
            _var_var_22 = abs(1.0 - correction_factor)
            _var_var_23 = max(0.0, 1.0 - _var_var_22 * 0.5)
            _var_var_24 = max(30.0, min(95.0, _var_var_23 * 85.0))
            _var_var_25 = _var_var_24 * 0.9
            _var_var_26 = ForecastResult(
                forecast_today=_var_var_14,
                forecast_tomorrow=_var_var_15,
                forecast_day_after_tomorrow=_var_var_15 * self.TOMORROW_DISCOUNT_FACTOR,
                confidence_today=_var_var_24,
                confidence_tomorrow=_var_var_25,
                confidence_day_after=_var_var_25 * 0.9,
                method="rule_based_consumption",
                calibrated=True,
                base_capacity=self.heating_capacity_kw,
                correction_factor=correction_factor,
            )
            _LOGGER.info(
                "Rule-based Consumption Forecast: Today=%.2f kWh, Tomorrow=%.2f kWh, Confidence=%.1f%% (CorrectionFactor=%.2f)",
                _var_var_26.forecast_today,
                _var_var_26.forecast_tomorrow,
                _var_var_26.confidence_today,
                correction_factor,
            )
            return _var_var_26
        except Exception as _var_var_21:
            _LOGGER.error(
                "Rule-based consumption forecast failed: %s", _var_var_21, exc_info=True
            )
            _var_var_27 = self.heating_capacity_kw * 5.0
            return ForecastResult(
                forecast_today=_var_var_27,
                forecast_tomorrow=_var_var_27 * 0.95,
                forecast_day_after_tomorrow=_var_var_27 * 0.9,
                confidence_today=20.0,
                confidence_tomorrow=15.0,
                confidence_day_after=10.0,
                method="emergency_fallback_consumption",
                calibrated=False,
                base_capacity=self.heating_capacity_kw,
            )
