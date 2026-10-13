# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence


def _exact_rounded_partition(total: float, first: float) -> tuple[float, float]:
    _var_var_10 = (first, math.nextafter(first, total), math.nextafter(first, 0.0))
    for _var_var_11 in _var_var_10:
        _var_var_12 = total - _var_var_11
        if 0.0 <= _var_var_11 <= total:
            if 0.0 <= _var_var_12 <= total:
                if _var_var_11 + _var_var_12 == total:
                    return (_var_var_11, _var_var_12)
    return (0.0, total)
