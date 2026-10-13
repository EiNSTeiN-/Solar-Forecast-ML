# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return
    try:
        _var_var_0 = float(value)
    except (TypeError, ValueError):
        return
    return (
        (_var_var_0 if True else None)
        if isfinite(_var_var_0) and minimum <= _var_var_0 <= maximum
        else None
    )
