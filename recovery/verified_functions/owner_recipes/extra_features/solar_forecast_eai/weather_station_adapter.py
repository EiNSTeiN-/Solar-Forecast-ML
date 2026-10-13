# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import partial
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo


def _year_comparison(
    rows: list[dict[str, Any]],
    precipitation: dict[str, Any],
    end: datetime,
    *,
    local_timezone: Any = timezone.utc,
) -> dict[str, Any]:
    _var_var_113 = end.astimezone(local_timezone)
    _var_var_114 = _var_var_113.year
    _var_var_111_after_0001 = precipitation.get("semantics")
    _var_var_111 = {
        _var_var_74["date"]: (
            _var_var_74.get("max_mm_per_h")
            if _var_var_111_after_0001 == "rate"
            else _var_var_74.get("total_mm")
        )
        for _var_var_74 in precipitation.get("daily", [])
        if isinstance(_var_var_74, dict)
        if isinstance(_var_var_74.get("date"), str)
        if isinstance(
            (
                _var_var_74.get("max_mm_per_h")
                if _var_var_111_after_0001 == "rate"
                else _var_var_74.get("total_mm")
            ),
            (int, float),
        )
    }

    def year_summary(year: int) -> dict[str, Any] | None:
        _var_var_119 = [
            _var_var_61
            for _var_var_61 in rows
            if str(_var_var_61.get("period", "")).startswith(f"{year:04d}-")
        ]
        if not _var_var_119:
            return ((None if True else None) if True else None) if True else None
        _var_var_120 = [
            _var_var_65["avg"]
            for _var_var_61 in _var_var_119
            if isinstance(
                (_var_var_65 := _var_var_61.get("values", {}).get("temperature")), dict
            )
            if isinstance(_var_var_65.get("avg"), (int, float))
        ]
        _var_var_121 = [
            _var_var_72
            for _var_var_61 in _var_var_119
            if isinstance(
                (_var_var_72 := _var_var_111.get(_var_var_61.get("period"))),
                (int, float),
            )
        ]
        _var_var_90 = _var_var_121 if _var_var_111_after_0001 == "counter" else []
        return (
            (
                (
                    {
                        "year": year,
                        "valid_days": len(_var_var_119),
                        "temperature_avg_c": (
                            round(sum(_var_var_120) / len(_var_var_120), 2)
                            if _var_var_120
                            else None
                        ),
                        "precipitation_total_mm": (
                            round(sum(_var_var_90), 3) if _var_var_90 else None
                        ),
                        "rain_days": (
                            sum((_var_var_72 >= 0.2 for _var_var_72 in _var_var_121))
                            if _var_var_121
                            else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    try:
        _var_var_112 = _var_var_113.date().replace(year=_var_var_114 - 1)
    except ValueError:
        _var_var_112 = _var_var_113.date().replace(year=_var_var_114 - 1, day=28)
    _var_var_116 = next(
        (
            _var_var_61
            for _var_var_61 in rows
            if _var_var_61.get("period") == _var_var_112.isoformat()
        ),
        None,
    )
    _var_var_117 = (
        _var_var_116.get("values", {}).get("temperature", {}).get("avg")
        if isinstance(_var_var_116, dict)
        else None
    )
    _var_var_87 = year_summary(_var_var_114)
    _var_var_118 = year_summary(_var_var_114 - 1)
    return (
        (
            (
                {
                    "available": bool(_var_var_87 or _var_var_118),
                    "current": _var_var_87,
                    "previous": _var_var_118,
                    "same_day_last_year": (
                        {
                            "date": _var_var_112.isoformat(),
                            "temperature_avg_c": _var_var_117,
                        }
                        if isinstance(_var_var_117, (int, float))
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )
