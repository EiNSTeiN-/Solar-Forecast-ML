# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Data Processor Module.
Processes and transforms weather data for ML features.
@zara
"""
import logging
from typing import Any, Dict, List

_LOGGER = logging.getLogger(__name__)


class WeatherDataProcessor:
    """Processes and transforms weather data for ML features. @zara"""

    @staticmethod
    def process_forecast_data(
        forecast_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        "Process raw forecast data into standardized format. @zara\n\nArgs:\n    forecast_list: List of raw forecast entries\n\nReturns:\n    List of processed forecast entries\n"
        processed_forecast = []
        for raw_entry in forecast_list:
            try:
                processed_entry = {
                    "datetime": raw_entry.get("datetime"),
                    "temperature": WeatherDataProcessor._safe_float(
                        raw_entry.get("temperature"), 15.0
                    ),
                    "humidity": WeatherDataProcessor._safe_float(
                        raw_entry.get("humidity"), 60.0
                    ),
                    "cloud_coverage": WeatherDataProcessor._safe_float(
                        raw_entry.get("cloud_coverage"), 50.0
                    ),
                    "wind_speed": WeatherDataProcessor._safe_float(
                        raw_entry.get("wind_speed"), 5.0
                    ),
                    "pressure": WeatherDataProcessor._safe_float(
                        raw_entry.get("pressure"), 1013.0
                    ),
                    "condition": raw_entry.get("condition", "unknown"),
                    "precipitation": WeatherDataProcessor._safe_float(
                        raw_entry.get("precipitation"), 0.0
                    ),
                }
                processed_forecast.append(processed_entry)
                continue
            except Exception as error:
                _LOGGER.warning(f"Error processing forecast entry: {error}")
        return processed_forecast

    @staticmethod
    def calculate_solar_radiation(
        cloud_cover: float, hour: int, latitude: float = 50.0
    ) -> float:
        "Estimate solar radiation based on cloud cover and time. @zara\n\nArgs:\n    cloud_cover: Cloud coverage percentage (0-100)\n    hour: Hour of day (0-23)\n    latitude: Geographic latitude\n\nReturns:\n    Estimated solar radiation in W/m2\n"
        if hour < 6 or hour > 20:
            return 0.0
        hour_angle_deg = abs(12 - hour) * 15
        sun_elevation_deg = 90 - abs(latitude) - hour_angle_deg
        if sun_elevation_deg < 0:
            return 0.0
        clear_sky_peak_wm2 = 1000.0
        elevation_factor = sun_elevation_deg / 90.0
        cloud_factor = 1.0 - cloud_cover / 100.0 * 0.75
        radiation_wm2 = clear_sky_peak_wm2 * elevation_factor * cloud_factor
        return max(0.0, radiation_wm2)

    @staticmethod
    def normalize_weather_features(weather_data: Dict[str, Any]) -> Dict[str, float]:
        "Normalize weather features for ML input. @zara\n\nArgs:\n    weather_data: Raw weather data dictionary\n\nReturns:\n    Dictionary of normalized features (0-1 range)\n"
        return {
            "temperature_norm": WeatherDataProcessor._normalize(
                weather_data.get("temperature", 15.0), min_val=-20.0, max_val=40.0
            ),
            "humidity_norm": WeatherDataProcessor._normalize(
                weather_data.get("humidity", 60.0), min_val=0.0, max_val=100.0
            ),
            "cloud_coverage_norm": WeatherDataProcessor._normalize(
                weather_data.get("cloud_coverage", 50.0), min_val=0.0, max_val=100.0
            ),
            "wind_speed_norm": WeatherDataProcessor._normalize(
                weather_data.get("wind_speed", 5.0), min_val=0.0, max_val=30.0
            ),
            "pressure_norm": WeatherDataProcessor._normalize(
                weather_data.get("pressure", 1013.0), min_val=950.0, max_val=1050.0
            ),
        }

    @staticmethod
    def extract_radiation_components(weather_data: Dict[str, Any]) -> Dict[str, float]:
        "Extract radiation components from weather data. @zara\n\nArgs:\n    weather_data: Weather data dictionary\n\nReturns:\n    Dictionary with ghi, dni, dhi values\n"
        ghi_wm2 = WeatherDataProcessor._safe_float(
            weather_data.get("ghi", weather_data.get("solar_radiation", 0)), 0.0
        )
        dni_wm2 = WeatherDataProcessor._safe_float(
            weather_data.get("dni", weather_data.get("direct_radiation", 0)), 0.0
        )
        dhi_wm2 = WeatherDataProcessor._safe_float(
            weather_data.get("dhi", weather_data.get("diffuse_radiation", 0)), 0.0
        )
        if dhi_wm2 <= 0 and ghi_wm2 > 0:
            cloud_cover_percent = weather_data.get(
                "cloud_coverage", weather_data.get("clouds", 50)
            )
            if cloud_cover_percent is not None:
                diffuse_fraction = 0.3 + cloud_cover_percent / 100.0 * 0.4
                dhi_wm2 = ghi_wm2 * diffuse_fraction
            else:
                dhi_wm2 = ghi_wm2 * 0.3
        if dni_wm2 <= 0 and ghi_wm2 > 0:
            dni_wm2 = ghi_wm2 - dhi_wm2 if dhi_wm2 > 0 else ghi_wm2 * 0.7
        return {"ghi": ghi_wm2, "dni": dni_wm2, "dhi": dhi_wm2}

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        "Safely convert value to float with default. @zara\n\nArgs:\n    value: Value to convert\n    default: Default if conversion fails\n\nReturns:\n    Float value\n"
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _normalize(value: float, min_val: float, max_val: float) -> float:
        "Normalize value to range 0-1. @zara\n\nArgs:\n    value: Value to normalize\n    min_val: Minimum expected value\n    max_val: Maximum expected value\n\nReturns:\n    Normalized value (0-1)\n"
        if max_val == min_val:
            return 0.5
        return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))

    @staticmethod
    def validate_weather_data(weather_data: Dict[str, Any]) -> bool:
        "Validate weather data has required fields. @zara\n\nArgs:\n    weather_data: Weather data dictionary\n\nReturns:\n    True if data is valid\n"
        required_fields = ["temperature"]
        for field_name in required_fields:
            if not weather_data.get(field_name) is None:
                continue
            return False
        return True

    @staticmethod
    def fill_missing_values(weather_data: Dict[str, Any]) -> Dict[str, Any]:
        "Fill missing values with sensible defaults. @zara\n\nArgs:\n    weather_data: Weather data dictionary\n\nReturns:\n    Dictionary with filled values\n"
        defaults = {
            "temperature": 15.0,
            "humidity": 60.0,
            "cloud_coverage": 50.0,
            "clouds": 50.0,
            "wind_speed": 5.0,
            "pressure": 1013.0,
            "precipitation": 0.0,
            "ghi": 0.0,
            "dni": 0.0,
            "dhi": 0.0,
        }
        filled_weather = weather_data.copy()
        for field_name, default_value in defaults.items():
            if filled_weather.get(field_name) is None:
                filled_weather[field_name] = default_value
        return filled_weather
