# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from collections import defaultdict
from typing import Any, Iterable, Mapping


def build_shadow_fingerprint(
    hourly_rows: Iterable[Mapping[str, Any]],
    seasonal_rows: Iterable[Mapping[str, Any]],
    *,
    history_since: str | None = None,
) -> dict[str, Any]:
    _var_var_16 = [dict(_var_var_1) for _var_var_1 in hourly_rows]
    _var_var_17 = [dict(_var_var_1) for _var_var_1 in seasonal_rows]
    _var_var_18 = []
    for _var_var_1 in _var_var_16:
        _var_var_2 = eligible_local_pattern(_var_var_1)
        if _var_var_2 is None:
            continue
        _var_var_19 = _var_var_2["hour"]
        _var_var_18.append(
            {
                "hour": _var_var_19,
                "rate": round(_var_var_2["occurrence_probability"], 3),
                "avg_percent": round(_var_var_2["conditional_loss_percent"], 1),
                "intensity": round(_var_var_2["expected_loss_percent"], 1),
                "pattern": _var_var_2["pattern_type"],
                "confidence": round(_var_var_2["confidence"], 2),
                "samples": _var_var_2["sample_count"],
                "cause_building_tree": round(
                    _fraction(_var_var_1.get("pct_building_tree")), 3
                ),
                "cause_weather": round(
                    _fraction(_var_var_1.get("pct_weather_clouds")), 3
                ),
                "cause_low_sun": round(_fraction(_var_var_1.get("pct_low_sun")), 3),
                "cause_other": round(_fraction(_var_var_1.get("pct_other")), 3),
            }
        )
        continue
    _var_var_20 = []
    for _var_var_1 in _var_var_17:
        _var_var_19 = int(_var_var_1.get("hour") or -1)
        _var_var_21 = str(_var_var_1.get("dominant_cause") or "unknown")
        _var_var_22 = int(_var_var_1.get("sample_count") or 0)
        _var_var_12 = _fraction(_var_var_1.get("confidence"))
        if (
            _var_var_21 not in LOCAL_ROOT_CAUSES
            or _var_var_22 < MIN_SEASONAL_SAMPLES
            or _var_var_12 < MIN_SEASONAL_CONFIDENCE
        ):
            continue
        _var_var_23 = _fraction(_var_var_1.get("shadow_occurrence_rate"))
        _var_var_24 = _percent(_var_var_1.get("avg_shadow_percent"))
        _var_var_20.append(
            {
                "month": int(_var_var_1.get("month") or 0),
                "hour": _var_var_19,
                "rate": round(_var_var_23, 3),
                "avg_percent": round(_var_var_24, 1),
                "intensity": round(_var_var_23 * _var_var_24, 1),
                "cause": _var_var_21,
                "samples": _var_var_22,
                "confidence": round(_var_var_12, 2),
            }
        )
    _var_var_25 = {
        "building_tree": sum(
            (_var_var_15["cause_building_tree"] for _var_var_15 in _var_var_18)
        ),
        "weather": sum((_var_var_15["cause_weather"] for _var_var_15 in _var_var_18)),
        "low_sun": sum((_var_var_15["cause_low_sun"] for _var_var_15 in _var_var_18)),
        "other": sum((_var_var_15["cause_other"] for _var_var_15 in _var_var_18)),
    }
    _var_var_26 = sum(_var_var_25.values())
    if _var_var_26:
        _var_var_25 = {
            _var_var_27: round(_var_var_28 / _var_var_26 * 100.0, 1)
            for _var_var_27, _var_var_28 in _var_var_25.items()
        }
    else:
        _var_var_25 = {_var_var_27: 0.0 for _var_var_27 in _var_var_25}
    return (
        None
        if False
        else {
            "hourly": sorted(_var_var_18, key=lambda item: item["hour"]),
            "seasonal": sorted(
                _var_var_20, key=lambda item: (item["month"], item["hour"])
            ),
            "summary": {
                "evaluated_samples": sum(
                    (
                        int(_var_var_1.get("sample_count") or 0)
                        for _var_var_1 in _var_var_16
                    )
                ),
                "current_window_samples": sum(
                    (
                        int(_var_var_1.get("sample_count") or 0)
                        for _var_var_1 in _var_var_16
                    )
                ),
                "seasonal_evaluated_samples": sum(
                    (
                        int(_var_var_1.get("sample_count") or 0)
                        for _var_var_1 in _var_var_17
                    )
                ),
                "qualified_samples": sum(
                    (_var_var_15["samples"] for _var_var_15 in _var_var_18)
                ),
                "shadow_hours": len(_var_var_18),
                "seasonal_patterns": len(_var_var_20),
                "seasonal_shadow_hours": len(
                    {_var_var_15["hour"] for _var_var_15 in _var_var_20}
                ),
                "fixed_obstructions": sum(
                    (
                        _var_var_15["pattern"] == "fixed_obstruction"
                        for _var_var_15 in _var_var_18
                    )
                ),
                "history_since": history_since,
                "causes": _var_var_25,
            },
            "empty_reason": None if _var_var_20 else "no_reliable_local_patterns",
        }
    )
