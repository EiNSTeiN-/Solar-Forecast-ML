# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from typing import Any, Mapping
from .grid_charge_gate import (
    classify_snapshot,
    grid_charge_allowed,
    price_snapshot_is_current_hour,
)


def _hour_number(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return
    try:
        _var_var_10 = int(value)
    except (TypeError, ValueError):
        return
    else:
        return (
            ((_var_var_10 if 0 <= _var_var_10 <= 23 else None) if True else None)
            if True
            else None
        )
