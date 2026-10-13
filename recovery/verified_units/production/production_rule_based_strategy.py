# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Rule-Based Forecast Strategy for Solar Forecast ML.

A fallback forecast strategy that uses simple rule-based heuristics
combined with weather factors and solar curve calculations.

@author zara
"""
import logging
import math
from datetime import timedelta
from typing import Any, Dict, List
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..forecast.forecast_strategy_base import ForecastResult, ForecastStrategy
from ..forecast.forecast_weather_calculator import WeatherCalculator

_LOGGER = logging.getLogger(__name__)


class RuleBasedForecastStrategy(ForecastStrategy):
    """A fallback forecast strategy using simple rule-based heuristics. @zara"""

    def __init__(self, weather_calculator: WeatherCalculator, solar_capacity: float):
        """Initialize the Rule-Based Forecast Strategy. @zara"""
        super().__init__("rule_based")
        self.weather_calculator = weather_calculator
        self.solar_capacity = solar_capacity
        self.PEAK_KW_PER_KWP = 0.95
        self.TOMORROW_DISCOUNT_FACTOR = 0.92
        self.MAX_REALISTIC_DAILY_KWH_PER_KWP = 8.0
        _LOGGER.debug("RuleBasedForecastStrategy (Iterative) initialized")

    def is_available(self) -> bool:
        """This strategy is always available as a fallback. @zara"""
        return True

    def get_priority(self) -> int:
        """Returns the priority of this strategy (lower than ML strategy). @zara"""
        return 50

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
                capacity_kwp = float(
                    sensor_data.get("solar_capacity", self.solar_capacity)
                )
                if capacity_kwp <= 0:
                    _LOGGER.warning(
                        "Solar capacity (%.2f kWp) is zero or negative. Using fallback 1.0 kWp",
                        capacity_kwp,
                    )
                    capacity_kwp = 1.0
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Invalid solar_capacity in sensor_data, using default %.2f kWp",
                    self.solar_capacity,
                )
                capacity_kwp = self.solar_capacity
            today_sum_kwh = 0.0
            tomorrow_sum_kwh = 0.0
            current_time = dt_util.now()
            today = current_time.date()
            tomorrow = today + timedelta(days=1)
            for weather_hour in hourly_weather_forecast:
                try:
                    local_datetime = weather_hour.get("local_datetime")
                    if not local_datetime:
                        _LOGGER.warning("Skipping hour, missing 'local_datetime'")
                        continue
                    if isinstance(local_datetime, str):
                        local_datetime = dt_util.parse_datetime(local_datetime)
                        if not local_datetime:
                            _LOGGER.warning(
                                "Skipping hour, invalid 'local_datetime' format"
                            )
                            continue
                    forecast_date = local_datetime.date()
                    forecast_hour = local_datetime.hour
                    hour_factor = self._get_hour_factor(forecast_hour)
                    weather_factor = (
                        self.weather_calculator.calculate_combined_weather_factor(
                            weather_hour, include_seasonal=True
                        )
                    )
                    hourly_production_kwh = (
                        capacity_kwp
                        * self.PEAK_KW_PER_KWP
                        * hour_factor
                        * weather_factor
                        * correction_factor
                    )
                    hourly_production_kwh = max(0.0, hourly_production_kwh)
                    if forecast_date == today:
                        today_sum_kwh += hourly_production_kwh
                    elif forecast_date == tomorrow:
                        tomorrow_sum_kwh += hourly_production_kwh
                except Exception as error:
                    _LOGGER.warning(
                        "Failed to process hour %s: %s",
                        weather_hour.get("local_hour"),
                        error,
                    )
                    continue
            _LOGGER.debug(
                "Rule-based iteration complete. Today (raw): %.2f kWh, Tomorrow (raw): %.2f kWh",
                today_sum_kwh,
                tomorrow_sum_kwh,
            )
            minimum_daily_kwh = 0.0
            maximum_daily_kwh = capacity_kwp * self.MAX_REALISTIC_DAILY_KWH_PER_KWP
            forecast_today_kwh = max(
                minimum_daily_kwh, min(today_sum_kwh, maximum_daily_kwh)
            )
            forecast_tomorrow_kwh = max(
                minimum_daily_kwh, min(tomorrow_sum_kwh, maximum_daily_kwh)
            )
            try:
                current_yield_value = sensor_data.get("current_yield")
                if current_yield_value is not None and current_yield_value > 0:
                    current_yield_kwh = float(current_yield_value)
                    if current_yield_kwh > forecast_today_kwh:
                        remaining_hours = max(0, 21 - current_time.hour)
                        daylight_window_hours = 15
                        remaining_production_kwh = 0.0
                        if daylight_window_hours > 0 and remaining_hours > 0:
                            remaining_fraction = remaining_hours / daylight_window_hours
                            remaining_production_kwh = (
                                forecast_today_kwh * remaining_fraction
                            )
                        else:
                            remaining_production_kwh = forecast_today_kwh * 0.1
                        adjusted_today_kwh = (
                            current_yield_kwh + remaining_production_kwh
                        )
                        _LOGGER.info(
                            "Minimum Forecast Adjustment (Rule): Current yield %.2f kWh > Original forecast %.2f kWh. Adjusted to %.2f kWh",
                            current_yield_kwh,
                            forecast_today_kwh,
                            adjusted_today_kwh,
                        )
                        initial_today_kwh = forecast_today_kwh
                        forecast_today_kwh = adjusted_today_kwh
                        if initial_today_kwh > 0:
                            adjustment_ratio = forecast_today_kwh / initial_today_kwh
                            forecast_tomorrow_kwh = (
                                forecast_tomorrow_kwh * adjustment_ratio
                            )
            except Exception as adjustment_error:
                _LOGGER.debug(
                    "Minimum forecast check (Rule) could not be performed: %s",
                    adjustment_error,
                )
            correction_deviation = abs(1.0 - correction_factor)
            correction_confidence = max(0.0, 1.0 - correction_deviation * 0.5)
            confidence_today = max(30.0, min(95.0, correction_confidence * 85.0))
            confidence_tomorrow = confidence_today * 0.9
            result = ForecastResult(
                forecast_today=forecast_today_kwh,
                forecast_tomorrow=forecast_tomorrow_kwh,
                confidence_today=confidence_today,
                confidence_tomorrow=confidence_tomorrow,
                method="rule_based_iterative",
                calibrated=True,
                base_capacity=capacity_kwp,
                correction_factor=correction_factor,
            )
            _LOGGER.info(
                "Rule-based (Iterative) Forecast successful: Today=%.2f kWh, Tomorrow=%.2f kWh, Confidence=%.1f%%, (CorrectionFactor=%.2f)",
                result.forecast_today,
                result.forecast_tomorrow,
                result.confidence_today,
                correction_factor,
            )
            return result
        except Exception as adjustment_error:
            _LOGGER.error(
                "Rule-based (Iterative) forecast calculation failed unexpectedly: %s",
                adjustment_error,
                exc_info=True,
            )
            _LOGGER.warning("Using emergency fallback for Rule-based forecast")
            fallback_capacity_kwp = (
                self.solar_capacity if self.solar_capacity > 0 else 2.0
            )
            fallback_forecast_kwh = fallback_capacity_kwp * 1.5
            return ForecastResult(
                forecast_today=fallback_forecast_kwh,
                forecast_tomorrow=fallback_forecast_kwh * 0.9,
                confidence_today=20.0,
                confidence_tomorrow=15.0,
                method="emergency_fallback_rule",
                calibrated=False,
                base_capacity=fallback_capacity_kwp,
            )

    def _get_hour_factor(self, hour: int) -> float:
        """Calculate a factor (0.0 to 1.0) based on solar curve (sine function). @zara"""
        try:
            if hour >= 22 or hour <= 5:
                if True:
                    return 0.0
            sunrise_hour = 6
            sunset_hour = 21
            daylight_hours = sunset_hour - sunrise_hour
            if daylight_hours <= 0:
                if True:
                    return 0.0
            day_progress = (hour + 0.5 - sunrise_hour) / daylight_hours
            if not 0.0 <= day_progress <= 1.0:
                if True:
                    return 0.0
            factor = math.sin(day_progress * math.pi)
            if True:
                return max(0.0, factor)
        except Exception:
            if True:
                return 0.5

    def _get_cloud_factor(self, cloud_cover: float) -> float:
        """Calculate production factor based on cloud cover percentage. @zara"""
        if cloud_cover is None:
            return 0.7
        cloud_cover = max(0.0, min(100.0, cloud_cover))
        factor = 1.0 - cloud_cover / 100.0 * 0.75
        return max(0.15, factor)

    def _get_temperature_factor(self, temperature: float) -> float:
        """Calculate efficiency factor based on temperature. @zara"""
        if temperature is None:
            return 1.0
        reference_temperature_c = 25.0
        temperature_coefficient = -0.004
        if temperature > reference_temperature_c:
            factor = (
                1.0 + (temperature - reference_temperature_c) * temperature_coefficient
            )
        else:
            factor = 1.0
        return max(0.7, min(1.05, factor))

    def _get_rain_factor(self, rain_mm: float) -> float:
        """Calculate production factor based on rain amount. @zara"""
        if rain_mm is None or rain_mm <= 0:
            return 1.0
        if rain_mm < 0.5:
            return 0.95
        if rain_mm < 2.0:
            return 0.8
        if rain_mm < 5.0:
            return 0.6
        return 0.4
