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

    async def calculate_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        **kwargs
    ) -> ForecastResult:
        """Calculate forecast using simple weather rules with learned corrections. @zara"""
        _LOGGER.debug("Calculating forecast using Rule-based (Iterative) strategy...")
        try:
            try:
                _var_var_0 = float(
                    sensor_data.get("solar_capacity", self.solar_capacity)
                )
                if _var_var_0 <= 0:
                    _LOGGER.warning(
                        "Solar capacity (%.2f kWp) is zero or negative. Using fallback 1.0 kWp",
                        _var_var_0,
                    )
                    _var_var_0 = 1.0
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Invalid solar_capacity in sensor_data, using default %.2f kWp",
                    self.solar_capacity,
                )
                _var_var_0 = self.solar_capacity
            _var_var_1 = 0.0
            _var_var_2 = 0.0
            _var_var_3 = dt_util.now()
            _var_var_4 = _var_var_3.date()
            _var_var_5 = _var_var_4 + timedelta(days=1)
            for _var_var_6 in hourly_weather_forecast:
                try:
                    _var_var_7 = _var_var_6.get("local_datetime")
                    if not _var_var_7:
                        _LOGGER.warning("Skipping hour, missing 'local_datetime'")
                        continue
                    if isinstance(_var_var_7, str):
                        _var_var_7 = dt_util.parse_datetime(_var_var_7)
                        if not _var_var_7:
                            _LOGGER.warning(
                                "Skipping hour, invalid 'local_datetime' format"
                            )
                            continue
                    _var_var_8 = _var_var_7.date()
                    _var_var_9 = _var_var_7.hour
                    _var_var_10 = self._get_hour_factor(_var_var_9)
                    _var_var_11 = (
                        self.weather_calculator.calculate_combined_weather_factor(
                            _var_var_6, include_seasonal=True
                        )
                    )
                    _var_var_12 = (
                        _var_var_0
                        * self.PEAK_KW_PER_KWP
                        * _var_var_10
                        * _var_var_11
                        * correction_factor
                    )
                    _var_var_12 = max(0.0, _var_var_12)
                    if _var_var_8 == _var_var_4:
                        _var_var_1 += _var_var_12
                    elif _var_var_8 == _var_var_5:
                        _var_var_2 += _var_var_12
                except Exception as _var_var_13:
                    _LOGGER.warning(
                        "Failed to process hour %s: %s",
                        _var_var_6.get("local_hour"),
                        _var_var_13,
                    )
                    continue
            _LOGGER.debug(
                "Rule-based iteration complete. Today (raw): %.2f kWh, Tomorrow (raw): %.2f kWh",
                _var_var_1,
                _var_var_2,
            )
            _var_var_14 = 0.0
            _var_var_15 = _var_var_0 * self.MAX_REALISTIC_DAILY_KWH_PER_KWP
            _var_var_16 = max(_var_var_14, min(_var_var_1, _var_var_15))
            _var_var_17 = max(_var_var_14, min(_var_var_2, _var_var_15))
            try:
                _var_var_18 = sensor_data.get("current_yield")
                if _var_var_18 is not None and _var_var_18 > 0:
                    _var_var_19 = float(_var_var_18)
                    if _var_var_19 > _var_var_16:
                        _var_var_20 = max(0, 21 - _var_var_3.hour)
                        _var_var_21 = 15
                        _var_var_22 = 0.0
                        if _var_var_21 > 0 and _var_var_20 > 0:
                            _var_var_23 = _var_var_20 / _var_var_21
                            _var_var_22 = _var_var_16 * _var_var_23
                        else:
                            _var_var_22 = _var_var_16 * 0.1
                        _var_var_24 = _var_var_19 + _var_var_22
                        _LOGGER.info(
                            "Minimum Forecast Adjustment (Rule): Current yield %.2f kWh > Original forecast %.2f kWh. Adjusted to %.2f kWh",
                            _var_var_19,
                            _var_var_16,
                            _var_var_24,
                        )
                        _var_var_25 = _var_var_16
                        _var_var_16 = _var_var_24
                        if _var_var_25 > 0:
                            _var_var_26 = _var_var_16 / _var_var_25
                            _var_var_17 = _var_var_17 * _var_var_26
            except Exception as _var_var_27:
                _LOGGER.debug(
                    "Minimum forecast check (Rule) could not be performed: %s",
                    _var_var_27,
                )
            _var_var_28 = abs(1.0 - correction_factor)
            _var_var_29 = max(0.0, 1.0 - _var_var_28 * 0.5)
            _var_var_30 = max(30.0, min(95.0, _var_var_29 * 85.0))
            _var_var_31 = _var_var_30 * 0.9
            _var_var_32 = ForecastResult(
                forecast_today=_var_var_16,
                forecast_tomorrow=_var_var_17,
                confidence_today=_var_var_30,
                confidence_tomorrow=_var_var_31,
                method="rule_based_iterative",
                calibrated=True,
                base_capacity=_var_var_0,
                correction_factor=correction_factor,
            )
            _LOGGER.info(
                "Rule-based (Iterative) Forecast successful: Today=%.2f kWh, Tomorrow=%.2f kWh, Confidence=%.1f%%, (CorrectionFactor=%.2f)",
                _var_var_32.forecast_today,
                _var_var_32.forecast_tomorrow,
                _var_var_32.confidence_today,
                correction_factor,
            )
            return _var_var_32
        except Exception as _var_var_27:
            _LOGGER.error(
                "Rule-based (Iterative) forecast calculation failed unexpectedly: %s",
                _var_var_27,
                exc_info=True,
            )
            _LOGGER.warning("Using emergency fallback for Rule-based forecast")
            _var_var_33 = self.solar_capacity if self.solar_capacity > 0 else 2.0
            _var_var_34 = _var_var_33 * 1.5
            return ForecastResult(
                forecast_today=_var_var_34,
                forecast_tomorrow=_var_var_34 * 0.9,
                confidence_today=20.0,
                confidence_tomorrow=15.0,
                method="emergency_fallback_rule",
                calibrated=False,
                base_capacity=_var_var_33,
            )

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
