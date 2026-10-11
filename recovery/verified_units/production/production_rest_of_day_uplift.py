# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Rest-of-day operational uplift: GHI evidence gate and conservative cap. @zara"""

import math
from typing import Optional

REST_OF_DAY_UPLIFT_MAX = 1.15
GHI_CONFIRM_MIN_RATIO = 1.15


def cap_rest_of_day_uplift(uplift: Optional[float]) -> float:
    """Clamp a rest-of-day scalar. Never reduces. Never exceeds REST_OF_DAY_UPLIFT_MAX."""
    try:
        scalar = float(uplift)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(scalar) or scalar <= 1.0:
        return 1.0
    return min(scalar, REST_OF_DAY_UPLIFT_MAX)


def rest_of_day_uplift_from_weather_evidence(
    production_uplift: Optional[float],
    *,
    has_local_ghi_sensor: bool,
    actual_ghi_wm2: Optional[float],
    forecast_ghi_wm2: Optional[float],
) -> float:
    # fmt: off
    """Return a rest-of-day scalar only when local GHI confirms the production miss.

    Without a local GHI sensor, or without both actual and forecast GHI, the
    scalar is fail-closed at 1.0. Weather regeneration may still run.
    """
    # fmt: on
    if not has_local_ghi_sensor:
        return 1.0
    if actual_ghi_wm2 is None or forecast_ghi_wm2 is None:
        return 1.0
    try:
        actual_ghi = float(actual_ghi_wm2)
        forecast_ghi = float(forecast_ghi_wm2)
    except (TypeError, ValueError):
        return 1.0
    if (
        not math.isfinite(actual_ghi)
        or not math.isfinite(forecast_ghi)
        or forecast_ghi <= 0.0
    ):
        return 1.0
    if actual_ghi / forecast_ghi < GHI_CONFIRM_MIN_RATIO:
        return 1.0
    return cap_rest_of_day_uplift(production_uplift)
