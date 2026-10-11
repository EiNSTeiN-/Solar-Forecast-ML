# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
Feature engineering for heat pump energy consumption AI model.
Extracts 17 normalized features from weather/temperature/consumption data.

Replaces the 28-feature solar model (removed 8 astronomy + 4 radiation + 3 panel-group features,
added 6 heat-pump-specific features).

@zara
"""
# fmt: on
import logging
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)


class FeatureEngineer:
    """Extract normalized features for heat pump consumption AI. @zara"""

    FEATURE_NAMES = [
        "hour_of_day",
        "day_of_year",
        "season_encoded",
        "weather_temp",
        "weather_wind",
        "weather_wind_chill",
        "weather_humidity",
        "weather_pressure",
        "weather_clouds",
        "weather_rain",
        "temp_delta",
        "heating_degree_hours",
        "temp_trend_3h",
        "is_dhw_mode",
        "consumption_yesterday",
        "consumption_same_hour",
        "thermal_mass_indicator",
    ]
    FEATURE_COUNT = len(FEATURE_NAMES)
    FEATURE_RANGES = {
        "hour_of_day": (0, 23),
        "day_of_year": (1, 365),
        "season_encoded": (0, 3),
        "weather_temp": (-25, 40),
        "weather_wind": (0, 30),
        "weather_wind_chill": (0, 10),
        "weather_humidity": (0, 100),
        "weather_pressure": (950, 1050),
        "weather_clouds": (0, 100),
        "weather_rain": (0, 50),
        "temp_delta": (0, 50),
        "heating_degree_hours": (0, 40),
        "temp_trend_3h": (-10, 10),
        "is_dhw_mode": (0, 1),
        "consumption_yesterday": (0, 100),
        "consumption_same_hour": (0, 10),
        "thermal_mass_indicator": (0, 1),
    }

    def __init__(self, dni_tracker=None) -> None:
        """Initialize feature engineer. @zara"""
        self.dni_tracker = dni_tracker
        self._thermal_mass_cache = 0.5

    @classmethod
    def get_feature_count(cls) -> int:
        """Return total number of features. @zara"""
        return cls.FEATURE_COUNT

    def extract_features(
        self,
        hour: int,
        day_of_year: int,
        weather: Dict[str, Any],
        historical: Dict[str, Any],
        indoor_temp: float = 21.0,
        is_dhw_mode: bool = False,
        temp_history_3h: Optional[List[float]] = None,
    ) -> List[float]:
        # fmt: off
        """Extract normalized features for one hour. @zara

Args:
    hour: Hour of day (0-23)
    day_of_year: Day of year (1-365)
    weather: Dict with keys: temperature, wind_speed, humidity, pressure,
             cloud_cover, precipitation
    historical: Dict with keys: consumption_yesterday, consumption_same_hour
    indoor_temp: Indoor temperature (°C)
    is_dhw_mode: Whether DHW mode is active
    temp_history_3h: Last 3 outdoor temps for trend calculation

Returns:
    List of 17 normalized feature values
"""
        # fmt: on
        temperature = float(weather.get("temperature", 5.0))
        wind_speed = float(weather.get("wind_speed", 0.0))
        humidity = float(weather.get("humidity", 50.0))
        pressure = float(weather.get("pressure", 1013.0))
        cloud_cover = float(weather.get("cloud_cover", 50.0))
        precipitation = float(weather.get("precipitation", 0.0))
        wind_chill = self._calculate_wind_chill(wind_speed)
        temp_delta = max(0.0, indoor_temp - (temperature - wind_chill))
        heating_degree_hours = max(0.0, 20.0 - temperature)
        temp_trend = self._calculate_temp_trend(temperature, temp_history_3h)
        consumption_yesterday = float(historical.get("consumption_yesterday", 0.0))
        consumption_same_hour = float(historical.get("consumption_same_hour", 0.0))
        season = self._encode_season(day_of_year)
        raw_features = {
            "hour_of_day": hour,
            "day_of_year": day_of_year,
            "season_encoded": season,
            "weather_temp": temperature,
            "weather_wind": wind_speed,
            "weather_wind_chill": wind_chill,
            "weather_humidity": humidity,
            "weather_pressure": pressure,
            "weather_clouds": cloud_cover,
            "weather_rain": precipitation,
            "temp_delta": temp_delta,
            "heating_degree_hours": heating_degree_hours,
            "temp_trend_3h": temp_trend,
            "is_dhw_mode": 1.0 if is_dhw_mode else 0.0,
            "consumption_yesterday": consumption_yesterday,
            "consumption_same_hour": consumption_same_hour,
            "thermal_mass_indicator": self._thermal_mass_cache,
        }
        features = []
        for feature_name in self.FEATURE_NAMES:
            value = raw_features.get(feature_name, 0.0)
            minimum, maximum = self.FEATURE_RANGES.get(feature_name, (0, 1))
            if maximum == minimum:
                features.append(0.5)
            else:
                normalized = (value - minimum) / (maximum - minimum)
                features.append(max(0.0, min(1.0, normalized)))
        return features

    def extract_record_features(self, record: Dict[str, Any]) -> Optional[List[float]]:
        """Adapt a persisted EAI prediction record to the consumption feature contract."""
        if not isinstance(record, dict):
            return
        weather = record.get("weather_corrected") or record.get("weather") or {}
        if not isinstance(weather, dict):
            return

        def first_value(*names: str, default: Any) -> Any:
            for key in names:
                value = weather.get(key)
                if value is None:
                    continue
                return value
            return default

        try:
            historical = {
                "consumption_yesterday": record.get(
                    "consumption_yesterday", record.get("production_yesterday", 0.0)
                ),
                "consumption_same_hour": record.get(
                    "consumption_same_hour",
                    record.get("production_same_hour_yesterday", 0.0),
                ),
            }
            weather_inputs = {
                "temperature": first_value(
                    "temperature", "temperature_c", "temp", default=5.0
                ),
                "wind_speed": first_value(
                    "wind_speed", "wind", "wind_speed_ms", default=0.0
                ),
                "humidity": first_value("humidity", "humidity_percent", default=50.0),
                "pressure": first_value("pressure", "pressure_hpa", default=1013.0),
                "cloud_cover": first_value("cloud_cover", "clouds", default=50.0),
                "precipitation": first_value(
                    "precipitation", "rain", "rain_mm", default=0.0
                ),
            }
            return self.extract_features(
                hour=int(record.get("target_hour", record.get("hour", 0))),
                day_of_year=int(
                    record.get("target_day_of_year", record.get("day_of_year", 1))
                ),
                weather=weather_inputs,
                historical=historical,
                indoor_temp=float(record.get("indoor_temperature", 21.0)),
                is_dhw_mode=bool(record.get("is_dhw_mode", False)),
                temp_history_3h=record.get("temp_history_3h"),
            )
        except (TypeError, ValueError):
            _LOGGER.debug("Prediction record contains invalid consumption features")
            return

    def extract_sequence(
        self, hours_data: List[Dict[str, Any]], indoor_temp: float = 21.0
    ) -> List[List[float]]:
        # fmt: off
        """Extract feature sequence for 24 hours (for LSTM input). @zara

Args:
    hours_data: List of 24 dicts, each containing:
        - hour, day_of_year, weather, historical, is_dhw_mode
    indoor_temp: Indoor temperature

Returns:
    List of 24 feature vectors (24 x 17)
"""
        # fmt: on
        sequence = []
        temperatures = []
        for hour_data in hours_data:
            hour = hour_data.get("hour", 0)
            day_of_year = hour_data.get("day_of_year", 1)
            weather = hour_data.get("weather", {})
            historical = hour_data.get("historical", {})
            is_dhw_mode = hour_data.get("is_dhw_mode", False)
            temperature = float(weather.get("temperature", 5.0))
            temperatures.append(temperature)
            history = temperatures[-4:] if len(temperatures) >= 4 else None
            features = self.extract_features(
                hour=hour,
                day_of_year=day_of_year,
                weather=weather,
                historical=historical,
                indoor_temp=indoor_temp,
                is_dhw_mode=is_dhw_mode,
                temp_history_3h=history,
            )
            sequence.append(features)
        return sequence

    @staticmethod
    def _calculate_wind_chill(wind_speed: float) -> float:
        # fmt: off
        """Calculate wind chill reduction in °C. @zara

        Simplified model: 0.5°C per m/s, capped at 5°C.
        Only relevant for air-source heat pumps.
        """
        # fmt: on
        if wind_speed <= 0:
            return 0.0
        return min(wind_speed * 0.5, 5.0)

    @staticmethod
    def _calculate_temp_trend(
        current_temp: float, history: Optional[List[float]]
    ) -> float:
        # fmt: off
        """Calculate temperature trend over last 3 hours. @zara

        Positive = warming, negative = cooling.
        """
        # fmt: on
        if not history or len(history) < 2:
            return 0.0
        oldest_temp = history[0]
        return current_temp - oldest_temp

    @staticmethod
    def _encode_season(day_of_year: int) -> int:
        # fmt: off
        """Encode season from day of year. @zara

        0 = Spring (Mar-May), 1 = Summer (Jun-Aug),
        2 = Autumn (Sep-Nov), 3 = Winter (Dec-Feb)
        """
        # fmt: on
        if day_of_year < 60 or day_of_year >= 335:
            return 3
        if day_of_year < 152:
            return 0
        if day_of_year < 244:
            return 1
        return 2

    def update_thermal_mass(self, value: float) -> None:
        """Update thermal mass indicator (learned from data). @zara"""
        self._thermal_mass_cache = max(0.0, min(1.0, value))
