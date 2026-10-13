# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta


def thermostat_demand(
    hvac_action: str | None,
    valve_pct: float | None,
    hvac_mode: str | None,
    setpoint_c: float | None,
    current_c: float | None,
) -> bool:
    """True when the valve is actually heating. Mode alone is only a last resort."""
    _var_var_9 = hvac_action.strip().lower() if isinstance(hvac_action, str) else ""
    _var_var_10 = _var_var_9 not in _MISSING_ACTION
    _var_var_11 = isinstance(valve_pct, (int, float)) and (
        not isinstance(valve_pct, bool)
    )
    if _var_var_10 or _var_var_11:
        return (
            _var_var_10
            and _var_var_9 == "heating"
            or (_var_var_11 and float(valve_pct) > 0)
        )
    if (hvac_mode or "").lower() != "heat":
        return False
    if setpoint_c is None or current_c is None:
        return False
    return float(setpoint_c) >= float(current_c) + HEAT_CALL_GAP
