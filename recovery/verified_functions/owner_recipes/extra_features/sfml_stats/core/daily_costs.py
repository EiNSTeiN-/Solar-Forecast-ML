# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence


def merge_daily_costs(
    start: date,
    end_exclusive: date,
    hourly_by_day: Mapping[date, Mapping[str, Any]],
    daily_kwh: Mapping[date, float],
    daily_avg_ct: Mapping[date, float],
) -> list[dict[str, Any]]:
    """One row per calendar day in [start, end_exclusive) with the K1 fallback chain."""
    _var_var_1 = []
    for _var_var_2 in sorted(set(hourly_by_day) | set(daily_kwh)):
        if _var_var_2 < start or _var_var_2 >= end_exclusive:
            continue
        _var_var_3 = hourly_by_day.get(_var_var_2)
        _var_var_4 = 0
        if _var_var_3 is not None:
            try:
                _var_var_4 = int(_var_var_3.get("hours") or 0)
            except (TypeError, ValueError):
                _var_var_4 = 0
        if _var_var_4 >= 1:
            try:
                _var_var_5 = float(_var_var_3.get("kwh") or 0.0)
            except (TypeError, ValueError):
                _var_var_5 = 0.0
            try:
                _var_var_6 = float(_var_var_3.get("energy_eur") or 0.0)
            except (TypeError, ValueError):
                _var_var_6 = 0.0
            try:
                _var_var_7 = float(_var_var_3.get("priced_kwh") or 0.0)
            except (TypeError, ValueError):
                _var_var_7 = 0.0
            _var_var_1.append(
                {
                    "date": _var_var_2.isoformat(),
                    "grid_kwh": _var_var_5,
                    "energy_eur": _var_var_6,
                    "source": SOURCE_HOURLY,
                    "hours_with_data": _var_var_4,
                    "priced_kwh": _var_var_7,
                }
            )
            continue
        if _var_var_2 not in daily_kwh:
            continue
        try:
            _var_var_5 = float(daily_kwh[_var_var_2] or 0.0)
        except (TypeError, ValueError):
            _var_var_5 = 0.0
        _var_var_8 = daily_avg_ct.get(_var_var_2)
        if _var_var_8 is not None:
            if _priced(_var_var_8):
                _var_var_6 = _var_var_5 * float(_var_var_8) / 100.0
                _var_var_1.append(
                    {
                        "date": _var_var_2.isoformat(),
                        "grid_kwh": _var_var_5,
                        "energy_eur": _var_var_6,
                        "source": SOURCE_DAILY_AVG,
                        "hours_with_data": 0,
                        "priced_kwh": _var_var_5,
                    }
                )
                continue
        _var_var_1.append(
            {
                "date": _var_var_2.isoformat(),
                "grid_kwh": _var_var_5,
                "energy_eur": None,
                "source": SOURCE_KWH_ONLY,
                "hours_with_data": 0,
                "priced_kwh": 0.0,
            }
        )
    return _var_var_1


def daily_costs_from_hourly_rows(
    hourly_rows: Sequence[Mapping[str, Any]], start: date, end_exclusive: date
) -> list[dict[str, Any]]:
    """Group raw hourly billing rows into daily cost rows (source hourly only)."""
    _var_var_9 = {}
    _var_var_10 = set()
    for _var_var_11 in hourly_rows:
        _var_var_2 = _parse_day(_var_var_11.get("date"))
        if _var_var_2 is None or _var_var_2 < start or _var_var_2 >= end_exclusive:
            continue
        try:
            _var_var_12 = int(_var_var_11.get("hour"))
        except (TypeError, ValueError):
            _var_var_12 = 0
        _var_var_13 = (_var_var_2.isoformat(), _var_var_12)
        if _var_var_13 in _var_var_10:
            continue
        _var_var_10.add(_var_var_13)
        _var_var_14 = _var_var_9.setdefault(
            _var_var_2, {"kwh": 0.0, "energy_eur": 0.0, "hours": 0, "priced_kwh": 0.0}
        )
        _var_var_14["hours"] += 1
        try:
            _var_var_15 = float(_var_var_11.get("grid_import_kwh") or 0.0)
        except (TypeError, ValueError):
            _var_var_15 = 0.0
        _var_var_14["kwh"] += _var_var_15
        _var_var_16 = _var_var_11.get("price_ct_kwh")
        if not _priced(_var_var_16):
            continue
        _var_var_14["priced_kwh"] += _var_var_15
        _var_var_14["energy_eur"] += _var_var_15 * float(_var_var_16) / 100.0
    return merge_daily_costs(start, end_exclusive, _var_var_9, {}, {})


async def load_provenance(
    db: Any,
    start: date,
    end_exclusive: date,
    projection: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    """Cost source, hour source, and correction flag for one date window."""
    _var_var_50 = []
    try:
        _var_var_1 = await load_daily_costs(db, start, end_exclusive, projection)
        _var_var_50 = [
            str(_var_var_11.get("source") or "") for _var_var_11 in _var_var_1
        ]
    except Exception:
        _var_var_50 = []
    _var_var_51 = {}
    try:
        async with db.execute(
            "SELECT data_source, COUNT(*)\n               FROM stats_hourly_billing\n               WHERE date >= ? AND date < ?\n               GROUP BY data_source",
            (start.isoformat(), end_exclusive.isoformat()),
        ) as _var_var_17:
            for _var_var_11 in await _var_var_17.fetchall():
                _var_var_51[str(_var_var_11[0] or "")] = int(_var_var_11[1] or 0)
    except Exception:
        _var_var_51 = {}
    _var_var_28 = False
    _var_var_37 = start.isoformat()
    _var_var_38 = end_exclusive.isoformat()
    for _var_var_2, _var_var_52 in (projection or {}).items():
        _var_var_0 = str(_var_var_2)
        if _var_var_37 <= _var_var_0 < _var_var_38 and _var_var_52:
            _var_var_28 = True
            break
    return provenance_from_counts(_var_var_50, _var_var_51, corrected=_var_var_28)
