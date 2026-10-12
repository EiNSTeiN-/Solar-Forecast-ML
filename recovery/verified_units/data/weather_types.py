# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Weather types and constants for Weather Expert System @zara"""

import math
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
VISIBILITY_CONTRADICTION_LOW_M = 1000.0
VISIBILITY_CONTRADICTION_HIGH_M = 5000.0
VISIBILITY_SOURCE_UNSUFFIXED = "open_meteo"
VISIBILITY_SOURCE_ICON_D2 = "icon_d2"
VISIBILITY_SOURCE_ICON_EU = "icon_eu"
VISIBILITY_SOURCE_ECMWF_IFS025 = "ecmwf_ifs025"


def _finite_nonneg_visibility(value: Any) -> Optional[float]:
    """Return a finite non-negative visibility in meters, else None."""
    if value is None:
        return
    try:
        visibility_m = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(visibility_m) or visibility_m < 0:
        return None
    return visibility_m


def _visibility_models_contradict(left: float, right: float) -> bool:
    """True when one model is fog-range and the other is clear-range."""
    return (
        left < VISIBILITY_CONTRADICTION_LOW_M
        and right > VISIBILITY_CONTRADICTION_HIGH_M
        or (
            right < VISIBILITY_CONTRADICTION_LOW_M
            and left > VISIBILITY_CONTRADICTION_HIGH_M
        )
    )


def resolve_open_meteo_visibility(
    *,
    unsuffixed: Any = None,
    icon_d2: Any = None,
    icon_eu: Any = None,
    ecmwf_ifs025: Any = None,
) -> tuple[Optional[float], Optional[str]]:
    # fmt: off
    """Resolve Open-Meteo visibility: unsuffixed, ICON-D2, ICON-EU, then IFS.

    When ICON-D2 and ICON-EU contradict (fog vs clear), keep the clearer
    (maximum) value instead of averaging or taking the first hop.
    """
    # fmt: on
    unsuffixed_visibility = _finite_nonneg_visibility(unsuffixed)
    if unsuffixed_visibility is not None:
        return (unsuffixed_visibility, VISIBILITY_SOURCE_UNSUFFIXED)
    icon_d2_visibility = _finite_nonneg_visibility(icon_d2)
    icon_eu_visibility = _finite_nonneg_visibility(icon_eu)
    ecmwf_visibility = _finite_nonneg_visibility(ecmwf_ifs025)
    if (
        icon_d2_visibility is not None
        and icon_eu_visibility is not None
        and _visibility_models_contradict(icon_d2_visibility, icon_eu_visibility)
    ):
        if icon_d2_visibility >= icon_eu_visibility:
            return (icon_d2_visibility, VISIBILITY_SOURCE_ICON_D2)
        return (icon_eu_visibility, VISIBILITY_SOURCE_ICON_EU)
    if icon_d2_visibility is not None:
        return (icon_d2_visibility, VISIBILITY_SOURCE_ICON_D2)
    if icon_eu_visibility is not None:
        return (icon_eu_visibility, VISIBILITY_SOURCE_ICON_EU)
    if ecmwf_visibility is not None:
        return (ecmwf_visibility, VISIBILITY_SOURCE_ECMWF_IFS025)
    return (None, None)


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
WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED = False
WEATHER_EXPERT_LEARNING_DISABLED_REASON = "immutable per-provider cloud forecast provenance is not available; static expert weights remain active"
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
    transmission_tau = TRANSMISSION_TAU_BY_CLOUD_TYPE.get(cloud_type.value, 0.35)
    transmission_percent = 100.0 * (
        1.0 - cloud_percent / 100.0 * (1.0 - transmission_tau)
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

    if True:
        expert_name: str
        cloud_cover: float
        confidence: float = 1.0
        raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlendedHourForecast:
    """Blended hourly forecast @zara"""

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
