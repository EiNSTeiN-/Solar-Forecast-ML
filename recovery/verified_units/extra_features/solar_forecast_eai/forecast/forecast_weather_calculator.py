# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Weather Calculator Module.
Calculates various rule-based weather adjustment factors.
@zara
"""
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season

_LOGGER = logging.getLogger(__name__)


class WeatherCalculator:
    """Calculates various rule-based weather adjustment factors. @zara"""

    def __init__(self):
        """Initialize the WeatherCalculator with predefined factor mappings. @zara"""
        self.SEASONAL_FACTORS = {
            "winter": 0.35,
            "spring": 0.75,
            "summer": 1.0,
            "autumn": 0.65,
        }
        self.OPTIMAL_TEMPERATURE_C = 25.0
        self.TEMP_EFFICIENCY_LOSS_PER_C = 0.004
        self.CONDITION_FACTORS = {
            "rainy": 0.4,
            "pouring": 0.2,
            "snowy": 0.3,
            "snowy-rainy": 0.25,
            "hail": 0.2,
            "lightning": 0.5,
            "lightning-rainy": 0.35,
            "fog": 0.45,
            "windy": 0.95,
            "windy-variant": 0.95,
            "exceptional": 0.5,
        }

    def get_temperature_factor(self, temperature_c: Optional[float]) -> float:
        """Calculate efficiency factor based on ambient temperature. @zara

        Args:
            temperature_c: Temperature in Celsius

        Returns:
            Temperature factor (0.7-1.0)
        """
        if temperature_c is None:
            return 0.9
        try:
            if temperature_c < 0:
                return 0.85
            if temperature_c <= self.OPTIMAL_TEMPERATURE_C:
                return 0.85 + temperature_c / self.OPTIMAL_TEMPERATURE_C * 0.15
            temperature_loss = (
                temperature_c - self.OPTIMAL_TEMPERATURE_C
            ) * self.TEMP_EFFICIENCY_LOSS_PER_C
            temperature_factor = 1.0 - temperature_loss
            return max(0.7, temperature_factor)
        except (ValueError, TypeError) as error:
            _LOGGER.warning(
                f"Temperature factor calculation failed for value '{temperature_c}': {error}. Using default 0.9."
            )
            return 0.9

    def get_cloud_factor(
        self, cloud_coverage_percent: Optional[float], condition: Optional[str] = None
    ) -> float:
        """Calculate factor based on cloud coverage percentage. @zara

        Args:
            cloud_coverage_percent: Cloud coverage 0-100%
            condition: Optional weather condition for fallback

        Returns:
            Cloud factor (0.35-1.0)
        """
        if cloud_coverage_percent is None:
            if condition:
                normalized_condition = condition.lower()
                if normalized_condition in (
                    "rainy",
                    "pouring",
                    "snowy",
                    "snowy-rainy",
                    "hail",
                ):
                    return 0.15
                if normalized_condition in ("lightning", "lightning-rainy", "fog"):
                    return 0.25
                if normalized_condition in ("cloudy",):
                    return 0.35
                if normalized_condition in ("partlycloudy",):
                    return 0.65
                if normalized_condition in ("sunny", "clear", "clear-night"):
                    return 0.95
            return 0.6
        try:
            cloud_percent = max(0.0, min(100.0, float(cloud_coverage_percent)))
            cloud_attenuation = 0.002
            minimum_factor = 0.35
            cloud_factor = max(
                minimum_factor, math.exp(-cloud_attenuation * cloud_percent)
            )
            return cloud_factor
        except (ValueError, TypeError) as error:
            _LOGGER.warning(
                f"Cloud factor calculation failed for value '{cloud_coverage_percent}': {error}. Using condition-aware default."
            )
            return self.get_cloud_factor(None, condition)

    def calculate_effective_radiation(
        self,
        clear_sky_radiation_wm2: float,
        cloud_coverage_percent: Optional[float],
        condition: Optional[str] = None,
    ) -> float:
        """Calculate effective solar radiation based on clear-sky radiation and cloud coverage. @zara

        Args:
            clear_sky_radiation_wm2: Theoretical maximum radiation (W/m2) from astronomy
            cloud_coverage_percent: Cloud coverage 0-100%
            condition: Optional weather condition string for fallback

        Returns:
            Effective solar radiation in W/m2
        """
        if clear_sky_radiation_wm2 <= 0:
            return 0.0
        cloud_factor = self.get_cloud_factor(cloud_coverage_percent, condition)
        effective_radiation = clear_sky_radiation_wm2 * cloud_factor
        return min(effective_radiation, clear_sky_radiation_wm2)

    def get_condition_factor(self, condition: Optional[str]) -> float:
        """Get reduction factor based on specific adverse weather conditions. @zara

        Args:
            condition: Weather condition string

        Returns:
            Condition factor (0.2-1.0)
        """
        if not condition or not isinstance(condition, str):
            return 1.0
        try:
            normalized_condition = condition.lower()
            return self.CONDITION_FACTORS.get(normalized_condition, 1.0)
        except Exception as error:
            _LOGGER.warning(
                f"Condition factor calculation failed for condition '{condition}': {error}. Using default 1.0."
            )
            return 1.0

    def get_seasonal_adjustment(self, now: Optional[datetime] = None) -> float:
        """Calculate seasonal adjustment factor based on the month. @zara

        Args:
            now: Optional datetime, uses current time if not provided

        Returns:
            Seasonal factor (0.2-1.2)
        """
        try:
            if now is None:
                local_now = dt_util.now()
            elif now.tzinfo is None:
                local_now = dt_util.ensure_local(now)
            else:
                local_now = dt_util.as_local(now)
            month = local_now.month
            season = get_season(month)
            seasonal_factor = self.SEASONAL_FACTORS.get(season, 0.65)
            if month in (12, 1):
                seasonal_factor *= 0.85
            elif month in (6, 7):
                seasonal_factor *= 1.05
            return max(0.2, min(1.2, seasonal_factor))
        except Exception as error:
            _LOGGER.warning(
                f"Seasonal adjustment calculation failed: {error}. Using default 0.65 (Autumn).",
                exc_info=True,
            )
            return 0.65

    def get_current_season(self, now: Optional[datetime] = None) -> str:
        """Return the current season name. @zara

        Args:
            now: Optional datetime

        Returns:
            Season name (winter, spring, summer, autumn)
        """
        try:
            if now is None:
                now = dt_util.now()
            elif now.tzinfo is None:
                now = dt_util.ensure_local(now)
            else:
                now = dt_util.as_local(now)
            month = now.month
            return get_season(month)
        except Exception as error:
            _LOGGER.warning(
                f"Failed to determine current season: {error}. Defaulting to 'autumn'."
            )
            return "autumn"

    def estimate_cloud_factor_from_sensors(
        self,
        actual_radiation_wm2: Optional[float] = None,
        theoretical_max_radiation_wm2: Optional[float] = None,
        actual_lux: Optional[float] = None,
        theoretical_max_lux: Optional[float] = None,
    ) -> Optional[float]:
        """Estimate cloud factor from real sensor data (LUX or W/m2). @zara

        Args:
            actual_radiation_wm2: Measured radiation
            theoretical_max_radiation_wm2: Clear sky max radiation
            actual_lux: Measured light level
            theoretical_max_lux: Clear sky max light level

        Returns:
            Cloud factor (0.0-1.0) or None if insufficient data
        """
        try:
            cloud_factor = None
            if (
                actual_radiation_wm2 is not None
                and theoretical_max_radiation_wm2 is not None
                and (theoretical_max_radiation_wm2 > 0)
            ):
                radiation_ratio = actual_radiation_wm2 / theoretical_max_radiation_wm2
                cloud_factor = max(0.0, min(1.0, radiation_ratio))
                _LOGGER.debug(
                    f"Cloud factor from W/m2: {cloud_factor:.2f} (actual={actual_radiation_wm2:.1f}, max={theoretical_max_radiation_wm2:.1f})"
                )
            elif (
                actual_lux is not None
                and theoretical_max_lux is not None
                and (theoretical_max_lux > 0)
            ):
                radiation_ratio = actual_lux / theoretical_max_lux
                cloud_factor = max(0.0, min(1.0, radiation_ratio))
                _LOGGER.debug(
                    f"Cloud factor from LUX: {cloud_factor:.2f} (actual={actual_lux:.1f}, max={theoretical_max_lux:.1f})"
                )
            return cloud_factor if True else None
        except Exception as error:
            _LOGGER.warning(f"Cloud factor estimation from sensors failed: {error}")
            return

    def blend_cloud_factors(
        self,
        weather_cloud_factor: Optional[float],
        sensor_cloud_factor: Optional[float],
        condition: Optional[str] = None,
        sensor_weight: float = 0.7,
    ) -> float:
        """Blend cloud factors from multiple sources with smart weighting. @zara

        Args:
            weather_cloud_factor: Cloud factor from weather forecast
            sensor_cloud_factor: Cloud factor from real sensors
            condition: Weather condition string for fallback
            sensor_weight: Weight for sensor data (0.0-1.0)

        Returns:
            Blended cloud factor (0.0-1.0)
        """
        try:
            if sensor_cloud_factor is not None and weather_cloud_factor is not None:
                blended_factor = (
                    sensor_weight * sensor_cloud_factor
                    + (1 - sensor_weight) * weather_cloud_factor
                )
                _LOGGER.debug(
                    f"Blended cloud factor: {blended_factor:.2f} (sensor={sensor_cloud_factor:.2f} @ {sensor_weight:.0%}, weather={weather_cloud_factor:.2f} @ {1 - sensor_weight:.0%})"
                )
                return blended_factor
            if sensor_cloud_factor is not None:
                _LOGGER.debug(f"Using sensor cloud factor: {sensor_cloud_factor:.2f}")
                return sensor_cloud_factor
            if weather_cloud_factor is not None:
                _LOGGER.debug(f"Using weather cloud factor: {weather_cloud_factor:.2f}")
                return weather_cloud_factor
            fallback_factor = self.get_cloud_factor(None, condition)
            _LOGGER.debug(
                f"Using condition-aware default cloud factor: {fallback_factor:.2f} (condition={condition})"
            )
            return fallback_factor
        except Exception as error:
            _LOGGER.error(f"Cloud factor blending failed: {error}", exc_info=True)
            return self.get_cloud_factor(None, condition)

    def calculate_combined_weather_factor(
        self, weather_data: Dict[str, Any], include_seasonal: bool = True
    ) -> float:
        """Combine temperature, cloud, condition and optionally seasonal factors. @zara

        Args:
            weather_data: Weather data dictionary
            include_seasonal: Whether to include seasonal factor

        Returns:
            Combined weather factor
        """
        try:
            temperature_c = weather_data.get("temperature")
            cloud_percent = weather_data.get("cloud_cover", weather_data.get("clouds"))
            condition = weather_data.get("condition")
            temperature_factor = self.get_temperature_factor(temperature_c)
            cloud_factor = self.get_cloud_factor(cloud_percent, condition)
            condition_factor = self.get_condition_factor(condition)
            combined_factor = temperature_factor * cloud_factor * condition_factor
            if include_seasonal:
                seasonal_factor = self.get_seasonal_adjustment()
                combined_factor *= seasonal_factor
            return max(0.0, combined_factor)
        except Exception as error:
            _LOGGER.error(
                f"Combined weather factor calculation failed: {error}", exc_info=True
            )
            return 0.5
