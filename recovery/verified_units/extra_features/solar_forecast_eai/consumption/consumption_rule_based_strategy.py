# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Rule-Based Consumption Forecast Strategy.

A fallback forecast strategy that uses simple rule-based heuristics
combined with weather factors and heating demand profiles for heat pump
energy consumption prediction.

@zara
"""
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

_LOGGER = logging.getLogger(__name__)


class RuleBasedForecastStrategy(ForecastStrategy):
    """A fallback forecast strategy using the thermodynamics engine. @zara"""

    def __init__(
        self,
        weather_calculator: WeatherCalculator,
        heating_capacity_kw: float,
        wp_type: str = DEFAULT_WP_TYPE,
        cop_rated: float = DEFAULT_COP_RATED,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the Rule-Based Forecast Strategy. @zara"""
        super().__init__("rule_based")
        self.weather_calculator = weather_calculator
        self.heating_capacity_kw = heating_capacity_kw
        if wp_type not in SUPPORTED_WP_TYPES:
            raise ValueError(f"Unsupported heat pump type: {wp_type}")
        if not (isfinite(float(cop_rated)) and 1.0 <= float(cop_rated) <= 10.0):
            raise ValueError("Rated COP must be finite and between 1.0 and 10.0")
        self.wp_type = wp_type
        self.cop_rated = float(cop_rated)
        self._config = dict(config or {})
        self._engine = ThermodynamicsEngine(
            wp_type=self.wp_type,
            heating_capacity_kw=self.heating_capacity_kw,
            cop_rated=self.cop_rated,
        )
        self.TOMORROW_DISCOUNT_FACTOR = 0.95
        self.MAX_REALISTIC_DAILY_KWH_PER_KW = 20.0
        _LOGGER.debug("RuleBasedForecastStrategy (Consumption) initialized")

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
        **kwargs,
    ) -> ForecastResult:
        """Calculate consumption forecast using temperature-based rules. @zara"""
        _LOGGER.debug("Calculating forecast using Rule-based (Consumption) strategy...")
        try:
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
                            continue
                    forecast_date = local_datetime.date()
                    forecast_hour = local_datetime.hour
                    temperature_c = weather_hour.get("temperature", 5)
                    wind_speed_ms = weather_hour.get("wind_speed", 3)
                    hourly_consumption_kwh = self._calculate_hourly_consumption(
                        temperature_c, wind_speed_ms, forecast_hour, correction_factor
                    )
                    if forecast_date == today:
                        today_sum_kwh += hourly_consumption_kwh
                    elif forecast_date == tomorrow:
                        tomorrow_sum_kwh += hourly_consumption_kwh
                except Exception as hour_error:
                    _LOGGER.warning(
                        "Failed to process hour %s: %s",
                        weather_hour.get("local_hour"),
                        hour_error,
                    )
                    continue
            _LOGGER.debug(
                "Rule-based iteration complete. Today (raw): %.2f kWh, Tomorrow (raw): %.2f kWh",
                today_sum_kwh,
                tomorrow_sum_kwh,
            )
            maximum_daily_kwh = (
                self.heating_capacity_kw * self.MAX_REALISTIC_DAILY_KWH_PER_KW
            )
            forecast_today_kwh = max(0.0, min(today_sum_kwh, maximum_daily_kwh))
            forecast_tomorrow_kwh = max(0.0, min(tomorrow_sum_kwh, maximum_daily_kwh))
            try:
                current_consumption_value = sensor_data.get("current_yield")
                if (
                    current_consumption_value is not None
                    and current_consumption_value > 0
                ):
                    current_consumption_kwh = float(current_consumption_value)
                    if current_consumption_kwh > forecast_today_kwh:
                        remaining_hours = max(0, 24 - current_time.hour)
                        average_hourly_kwh = (
                            forecast_today_kwh / 24.0 if forecast_today_kwh > 0 else 0.5
                        )
                        remaining_consumption_kwh = average_hourly_kwh * remaining_hours
                        forecast_today_kwh = (
                            current_consumption_kwh + remaining_consumption_kwh
                        )
                        _LOGGER.info(
                            "Consumption adjustment: Current %.2f kWh > Forecast %.2f kWh -> Adjusted %.2f kWh",
                            current_consumption_kwh,
                            today_sum_kwh,
                            forecast_today_kwh,
                        )
            except Exception as forecast_error:
                _LOGGER.debug("Consumption adjustment check failed: %s", forecast_error)
            correction_deviation = abs(1.0 - correction_factor)
            correction_confidence = max(0.0, 1.0 - correction_deviation * 0.5)
            confidence_today = max(30.0, min(95.0, correction_confidence * 85.0))
            confidence_tomorrow = confidence_today * 0.9
            result = ForecastResult(
                forecast_today=forecast_today_kwh,
                forecast_tomorrow=forecast_tomorrow_kwh,
                forecast_day_after_tomorrow=forecast_tomorrow_kwh
                * self.TOMORROW_DISCOUNT_FACTOR,
                confidence_today=confidence_today,
                confidence_tomorrow=confidence_tomorrow,
                confidence_day_after=confidence_tomorrow * 0.9,
                method="rule_based_consumption",
                calibrated=True,
                base_capacity=self.heating_capacity_kw,
                correction_factor=correction_factor,
            )
            _LOGGER.info(
                "Rule-based Consumption Forecast: Today=%.2f kWh, Tomorrow=%.2f kWh, Confidence=%.1f%% (CorrectionFactor=%.2f)",
                result.forecast_today,
                result.forecast_tomorrow,
                result.confidence_today,
                correction_factor,
            )
            return result
        except Exception as forecast_error:
            _LOGGER.error(
                "Rule-based consumption forecast failed: %s",
                forecast_error,
                exc_info=True,
            )
            fallback_forecast_kwh = self.heating_capacity_kw * 5.0
            return ForecastResult(
                forecast_today=fallback_forecast_kwh,
                forecast_tomorrow=fallback_forecast_kwh * 0.95,
                forecast_day_after_tomorrow=fallback_forecast_kwh * 0.9,
                confidence_today=20.0,
                confidence_tomorrow=15.0,
                confidence_day_after=10.0,
                method="emergency_fallback_consumption",
                calibrated=False,
                base_capacity=self.heating_capacity_kw,
            )

    def _calculate_hourly_consumption(
        self, temp: float, wind_speed: float, hour: int, correction_factor: float
    ) -> float:
        """Calculate hourly heat pump consumption from the thermodynamics engine."""
        weather_conditions = WeatherConditions(
            outdoor_temp=float(temp), wind_speed=float(wind_speed)
        )
        thermal_result = self._engine.calculate_hourly_consumption(
            weather_conditions,
            indoor_temp=21.0,
            hour=hour,
            **cop_sink_kwargs(self._config),
            **dhw_forecast_kwargs(self._config),
        )
        corrected_consumption_kwh = thermal_result.consumption_kwh * correction_factor
        return max(0.0, min(corrected_consumption_kwh, self.heating_capacity_kw))
