# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Weather types and constants for Weather Expert System @zara"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CloudType(Enum):
    """Cloud type classification @zara"""

    CLEAR = "clear"
    CIRRUS = "cirrus"
    FAIR = "fair"
    MIXED = "mixed"
    STRATUS = "stratus"
    OVERCAST = "overcast"
    SNOW = "snow"
    FOG = "fog"
    FOG_LIGHT = "fog_light"


LAYER_THRESHOLD_DOMINANT = 50.0
LAYER_THRESHOLD_LOW = 20.0
LAYER_THRESHOLD_CLEAR = 25.0
VISIBILITY_FOG_THRESHOLD = 1000
VISIBILITY_FOG_LIGHT_THRESHOLD = 5000
DEFAULT_EXPERT_WEIGHTS: dict[str, dict[str, float]] = {
    CloudType.CLEAR.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.15,
        "bright_sky": 0.35,
        "pirate_weather": 0.25,
    },
    CloudType.CIRRUS.value: {
        "open_meteo": 0.1,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.3,
        "bright_sky": 0.3,
        "pirate_weather": 0.2,
    },
    CloudType.FAIR.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.15,
        "bright_sky": 0.35,
        "pirate_weather": 0.25,
    },
    CloudType.MIXED.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.15,
        "bright_sky": 0.3,
        "pirate_weather": 0.3,
    },
    CloudType.STRATUS.value: {
        "open_meteo": 0.1,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.1,
        "bright_sky": 0.4,
        "pirate_weather": 0.3,
    },
    CloudType.OVERCAST.value: {
        "open_meteo": 0.1,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.1,
        "bright_sky": 0.4,
        "pirate_weather": 0.3,
    },
    CloudType.SNOW.value: {
        "open_meteo": 0.2,
        "wttr_in": 0.2,
        "ecmwf_layers": 0.2,
        "bright_sky": 0.2,
        "pirate_weather": 0.2,
    },
    CloudType.FOG.value: {
        "open_meteo": 0.1,
        "wttr_in": 0.05,
        "ecmwf_layers": 0.05,
        "bright_sky": 0.5,
        "pirate_weather": 0.3,
    },
    CloudType.FOG_LIGHT.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.1,
        "ecmwf_layers": 0.1,
        "bright_sky": 0.4,
        "pirate_weather": 0.25,
    },
}
TRANSMISSION_TAU_BY_CLOUD_TYPE: dict[str, float] = {
    CloudType.CLEAR.value: 1.0,
    CloudType.CIRRUS.value: 0.8,
    CloudType.FAIR.value: 0.5,
    CloudType.MIXED.value: 0.35,
    CloudType.STRATUS.value: 0.2,
    CloudType.OVERCAST.value: 0.1,
    CloudType.SNOW.value: 0.02,
    CloudType.FOG.value: 0.5,
    CloudType.FOG_LIGHT.value: 0.7,
}


def cloud_to_transmission(
    cloud_percent: float, cloud_type: CloudType = CloudType.MIXED
) -> float:
    """Convert cloud cover to solar transmission @zara"""
    base_transmission = TRANSMISSION_TAU_BY_CLOUD_TYPE.get(cloud_type.value, 0.35)
    transmission_percent = 100.0 * (
        1.0 - cloud_percent / 100.0 * (1.0 - base_transmission)
    )
    return round(max(0.0, min(100.0, transmission_percent)), 1)


BRIGHT_SKY_BASE_URL = "https://api.brightsky.dev/weather"
BRIGHT_SKY_TIMEOUT = 15
BRIGHT_SKY_CACHE_HOURS = 3
PIRATE_WEATHER_BASE_URL = "https://api.pirateweather.net/forecast"
PIRATE_WEATHER_TIMEOUT = 15
PIRATE_WEATHER_CACHE_HOURS = 3
WTTR_IN_BASE_URL = "https://wttr.in"
WTTR_IN_TIMEOUT = 15
WTTR_IN_CACHE_HOURS = 6
LEARNING_SMOOTHING_FACTOR = 0.3
LEARNING_MIN_ERROR = 5.0
LEARNING_MIN_HOURS = 4
LEARNING_ACCELERATED_THRESHOLD = 35.0
LEARNING_ACCELERATED_FACTOR = 0.5


@dataclass
class ExpertForecast:
    """Forecast from single expert @zara"""

    # Retain CPython 3.14's deferred annotation registration; no runtime condition.
    if True:
        expert_name: str
        cloud_cover: float
        confidence: float = 1.0
        raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlendedHourForecast:
    """Blended hourly forecast @zara"""

    # Retain CPython 3.14's deferred annotation registration; no runtime condition.
    if True:
        date: str
        hour: int
        cloud_cover: float
        cloud_type: CloudType
        expert_forecasts: dict[str, float]
        blend_weights: dict[str, float]
        cloud_cover_low: Optional[float] = None
        cloud_cover_mid: Optional[float] = None
        cloud_cover_high: Optional[float] = None
        temperature: Optional[float] = None
        humidity: Optional[float] = None
        precipitation: Optional[float] = None
        wind_speed: Optional[float] = None
        pressure: Optional[float] = None
        ghi: Optional[float] = None
        direct_radiation: Optional[float] = None
        diffuse_radiation: Optional[float] = None
        visibility_m: Optional[float] = None
        fog_detected: bool = False
        fog_type: Optional[str] = None
