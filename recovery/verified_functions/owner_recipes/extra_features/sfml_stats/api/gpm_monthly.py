# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence


def _demo_hourly_rows(through: date) -> list[dict[str, Any]]:
    _var_var_90 = []
    for _var_var_16 in (2024, 2025, 2026):
        for _var_var_17 in range(1, 13):
            _var_var_91 = calendar.monthrange(_var_var_16, _var_var_17)[1]
            if date(_var_var_16, _var_var_17, 1) > through:
                continue
            _var_var_92 = (
                min(_var_var_91, through.day)
                if _var_var_16 == through.year and _var_var_17 == through.month
                else _var_var_91
            )
            _var_var_43 = _DEMO_KWH[_var_var_17 - 1] / (_var_var_91 * 24)
            _var_var_44 = _DEMO_PRICE_CT[_var_var_17 - 1]
            for _var_var_93 in range(1, _var_var_92 + 1):
                _var_var_40 = date(_var_var_16, _var_var_17, _var_var_93)
                if _var_var_40 > through:
                    break
                for _var_var_41 in range(24):
                    _var_var_90.append(
                        {
                            "date": _var_var_40.isoformat(),
                            "hour": _var_var_41,
                            "grid_import_kwh": round(_var_var_43, 6),
                            "price_ct_kwh": _var_var_44,
                        }
                    )
    return _var_var_90
