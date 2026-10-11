# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Rest-of-day operational uplift: GHI evidence gate and conservative cap. @zara"""
''
''
''
''
''
''
''
''
''
import math
from typing import Optional
REST_OF_DAY_UPLIFT_MAX = 1.15
GHI_CONFIRM_MIN_RATIO = 1.15

def cap_rest_of_day_uplift(uplift: Optional[float]) -> float:
    """Clamp a rest-of-day scalar. Never reduces. Never exceeds REST_OF_DAY_UPLIFT_MAX."""
    try:
        _var_var_0 = float(uplift)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(_var_var_0) or _var_var_0 <= 1.0:
        return 1.0
    return min(_var_var_0, REST_OF_DAY_UPLIFT_MAX)

def rest_of_day_uplift_from_weather_evidence(production_uplift: Optional[float], *, has_local_ghi_sensor: bool, actual_ghi_wm2: Optional[float], forecast_ghi_wm2: Optional[float]) -> float:
    """Return a rest-of-day scalar only when local GHI confirms the production miss.

    Without a local GHI sensor, or without both actual and forecast GHI, the
    scalar is fail-closed at 1.0. Weather regeneration may still run.
    """
    if not has_local_ghi_sensor:
        return 1.0
    if actual_ghi_wm2 is None or forecast_ghi_wm2 is None:
        return 1.0
    try:
        _var_var_1 = float(actual_ghi_wm2)
        _var_var_2 = float(forecast_ghi_wm2)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(_var_var_1) or not math.isfinite(_var_var_2) or _var_var_2 <= 0.0:
        return 1.0
    if _var_var_1 / _var_var_2 < GHI_CONFIRM_MIN_RATIO:
        return 1.0
    return cap_rest_of_day_uplift(production_uplift)
