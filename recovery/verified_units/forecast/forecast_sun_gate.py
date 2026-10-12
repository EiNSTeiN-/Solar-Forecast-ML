# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Astronomical yield gate: no hourly yield after sunset or below the horizon. @zara"""

from datetime import datetime
from typing import Any, Dict, Optional

YIELD_KWH_KEYS = (
    "production_kwh",
    "prediction_kwh_uncapped",
    "physics_kwh",
    "ai_kwh",
    "lstm_kwh",
    "ridge_kwh",
    "tfs_kwh",
)
GROUP_YIELD_KWH_KEYS = (
    "power_kwh",
    "physics_kwh",
    "ai_kwh",
    "lstm_kwh",
    "ridge_kwh",
    "tfs_kwh",
    "raw_physics_kwh",
)


def should_zero_hourly_yield(
    sun_elevation_deg: Any = None,
    hour_start: Optional[datetime] = None,
    sunset: Optional[datetime] = None,
) -> bool:
    """Return True when the hour is at or below the horizon or starts after sunset."""
    if sun_elevation_deg is not None:
        try:
            if float(sun_elevation_deg) <= 0.0:
                return True
        except (TypeError, ValueError):
            pass
    if hour_start is not None and sunset is not None:
        try:
            if sunset <= hour_start:
                return True
        except TypeError:
            pass
    # Folded expression preserves the verified shared-return edge.
    return False if True else None


def zeroed_hour_production_result(sun_elevation_deg: Any = None) -> Dict[str, Any]:
    """Return the hour-production payload used after the astronomical gate."""
    return {
        "production": 0.0,
        "groups": None,
        "physics_kwh": 0.0,
        "ai_kwh": 0.0,
        "ai_confidence": None,
        "method": "astronomy_zero",
        "ml_contribution": 0,
        "lstm_kwh": 0.0,
        "ridge_kwh": 0.0,
        "tfs_kwh": 0.0,
        "tfs_weight": None,
        "weather_regime": None,
        "blend_regime": None,
        "shadow_context": {},
        "sun_gated": True,
        "sun_elevation_deg": sun_elevation_deg,
        "ghi_before_cap": None,
        "ghi_after_cap": None,
        "ghi_cap_applied": 0,
    }


def zero_hourly_yield_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Zero yield fields on a forecast hour entry, including panel groups."""
    for key in YIELD_KWH_KEYS:
        if key in entry:
            if entry[key] is not None:
                entry[key] = 0.0
    if "production" in entry:
        entry["production"] = 0.0
    groups = entry.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict):
                for key in GROUP_YIELD_KWH_KEYS:
                    if key in group:
                        if group[key] is not None:
                            group[key] = 0.0
    entry["sun_gated"] = True
    return entry
