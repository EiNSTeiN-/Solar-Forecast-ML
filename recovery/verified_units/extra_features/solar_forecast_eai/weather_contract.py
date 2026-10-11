# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Canonical public values for weather provenance and provider state."""
from __future__ import annotations
from enum import StrEnum
from typing import Any

class WeatherSource(StrEnum):
    """Bounded observation and comparison provenance values."""
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    UNKNOWN = 'unknown'
    WEATHER_FUSION_AI = 'weather_fusion_ai'
    WEATHER_FUSION_TRACKER = 'weather_fusion_tracker'
    SFML_DATABASE = 'sfml_database'
    SFML_STATION = 'sfml_station'
    LOCAL_WEATHER_STATION = 'local_weather_station'
    HOME_ASSISTANT_ENTITY = 'home_assistant_entity'
    HOME_ASSISTANT_RECORDER = 'home_assistant_recorder'

class WeatherProviderStatus(StrEnum):
    """Bounded status values accepted from optional weather providers."""
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    UNKNOWN = 'unknown'
    OK = 'ok'
    AVAILABLE = 'available'
    PARTIAL = 'partial'
    STALE = 'stale'
    UNAVAILABLE = 'unavailable'
    DEGRADED = 'degraded'
    ERROR = 'error'
_SOURCE_VALUES = frozenset((_var_var_0.value for _var_var_0 in WeatherSource))
_STATUS_VALUES = frozenset((_var_var_0.value for _var_var_0 in WeatherProviderStatus))

def normalize_weather_source(value: Any) -> str:
    """Return a canonical provenance value without reflecting provider strings."""
    return value if isinstance(value, str) and value in _SOURCE_VALUES else 'unknown'

def normalize_provider_status(value: Any) -> str:
    """Return a canonical provider status without reflecting provider strings."""
    return value if isinstance(value, str) and value in _STATUS_VALUES else 'unknown'
