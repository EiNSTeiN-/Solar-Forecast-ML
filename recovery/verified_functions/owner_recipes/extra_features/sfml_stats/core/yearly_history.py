# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from ..api.gpm_monthly import iter_month_slices, prorate_base_fee
from .corrected_energy import projection_from_runtime
from .daily_costs import (
    SOURCE_DAILY_AVG,
    SOURCE_HOURLY,
    SOURCE_KWH_ONLY,
    daily_costs_from_hourly_rows,
    load_daily_costs,
)
from .invoice_archive import list_invoices


def fill_gap_from_daily_costs(
    gap_days: set[date],
    daily_rows: Sequence[Mapping[str, Any]],
    monthly_base_fee: float,
) -> dict[str, Any]:
    """Fill invoice complement days from daily_costs; base fee on (a)/(b) days."""
    _var_var_25 = set()
    _var_var_26 = set()
    _var_var_27 = 0
    _var_var_28 = 0
    _var_var_29 = 0
    _var_var_11 = 0.0
    _var_var_30 = 0.0
    _var_var_31 = 0
    for _var_var_32 in daily_rows:
        _var_var_8 = _parse_day(_var_var_32.get("date"))
        if _var_var_8 is None or _var_var_8 not in gap_days:
            continue
        _var_var_25.add(_var_var_8)
        _var_var_33 = str(_var_var_32.get("source") or "")
        if _var_var_33 == SOURCE_HOURLY:
            _var_var_27 += 1
            _var_var_26.add(_var_var_8)
        elif _var_var_33 == SOURCE_DAILY_AVG:
            _var_var_28 += 1
            _var_var_26.add(_var_var_8)
        elif _var_var_33 == SOURCE_KWH_ONLY:
            _var_var_29 += 1
        try:
            _var_var_11 += float(_var_var_32.get("grid_kwh") or 0.0)
        except (TypeError, ValueError):
            pass
        _var_var_34 = _var_var_32.get("energy_eur")
        if _var_var_34 is not None:
            try:
                _var_var_30 += float(_var_var_34)
            except (TypeError, ValueError):
                pass
        try:
            _var_var_31 += int(_var_var_32.get("hours_with_data") or 0)
        except (TypeError, ValueError):
            pass
    _var_var_35 = 0.0
    if _var_var_26 and monthly_base_fee > 0:
        _var_var_5 = min(gap_days) if gap_days else min(_var_var_26)
        _var_var_6 = max(gap_days) if gap_days else max(_var_var_26)
        _var_var_36 = _var_var_6 + timedelta(days=1)
        for _var_var_24, _var_var_23, _var_var_37 in iter_month_slices(
            _var_var_5.replace(day=1), _var_var_36
        ):
            _var_var_38 = sum(
                (
                    1
                    for _var_var_8 in _var_var_26
                    if _var_var_24 <= _var_var_8 < _var_var_23
                )
            )
            if _var_var_38 <= 0:
                continue
            _var_var_39 = int(_var_var_37[:4])
            _var_var_40 = int(_var_var_37[5:7])
            _var_var_35 += prorate_base_fee(
                monthly_base_fee, _var_var_39, _var_var_40, _var_var_38
            )
    return {
        "grid_kwh": _round_kwh(_var_var_11),
        "energy_eur": _round_money(_var_var_30),
        "base_fee_eur": _round_money(_var_var_35),
        "total_eur": _round_money(_var_var_30 + _var_var_35),
        "measured_days": len(_var_var_25),
        "measured_day_set": _var_var_25,
        "gap_days_without_data": len(gap_days - _var_var_25),
        "measured_hours": _var_var_31,
        "measured_hours_expected": _var_var_27 * 24,
        "days_hourly": _var_var_27,
        "days_daily_avg": _var_var_28,
        "days_kwh_only": _var_var_29,
        "priced_days": len(_var_var_26),
    }
