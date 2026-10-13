# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import math
from collections import defaultdict
from statistics import fmean
from typing import Any, Iterable


def build_weather_impact(
    rows: Iterable[dict[str, Any]], *, period: dict[str, str]
) -> dict[str, Any]:
    """Aggregate forecast error by observed weather without causal claims. @zara"""
    _var_var_31 = list(rows)
    _var_var_53 = []
    for _var_var_54, _var_var_55, _var_var_56 in CLOUD_GROUPS:
        _var_var_57 = []
        for _var_var_5 in _var_var_31:
            _var_var_58 = _number(
                _var_var_5.get("cloud_actual_percent"), minimum=0, maximum=100
            )
            if _var_var_58 is None:
                continue
            if (
                _var_var_55 == 0
                and _var_var_55 <= _var_var_58 <= _var_var_56
                or _var_var_55 < _var_var_58 <= _var_var_56
            ):
                _var_var_57.append(_var_var_5)
        _var_var_53.append(_metric_group(_var_var_54, _var_var_57))
    _var_var_59 = defaultdict(list)
    for _var_var_5 in _var_var_31:
        _var_var_59[str(_var_var_5.get("date"))].append(_var_var_5)
    _var_var_60 = defaultdict(list)
    for _var_var_61 in _var_var_59.values():
        _var_var_15 = []
        for _var_var_5 in sorted(
            _var_var_61, key=lambda item: int(item.get("hour", 0))
        ):
            _var_var_62 = _number(_var_var_5.get("clear_sky_wm2"), minimum=100)
            _var_var_63 = _number(_var_var_5.get("solar_actual_wm2"), minimum=0)
            if _var_var_62 is not None:
                if _var_var_63 is not None:
                    _var_var_15.append(_clamp(_var_var_63 / _var_var_62, 0, 1.5))
        _var_var_64 = [
            abs(_var_var_17 - _var_var_16)
            for _var_var_16, _var_var_17 in zip(_var_var_15, _var_var_15[1:])
        ]
        if len(_var_var_64) < 4:
            continue
        _var_var_19 = fmean(_var_var_64)
        _var_var_54 = (
            "stable"
            if _var_var_19 <= 0.15
            else "variable" if _var_var_19 <= 0.35 else "volatile"
        )
        _var_var_60[_var_var_54].extend(_var_var_61)
        continue
    return {
        "period": period,
        "basis": "observed_weather_and_matched_forecast_actual",
        "minimum_points": MIN_IMPACT_POINTS,
        "minimum_days": MIN_IMPACT_DAYS,
        "cloud_groups": _var_var_53,
        "volatility_groups": [
            _metric_group(_var_var_54, _var_var_60.get(_var_var_54, []))
            for _var_var_54 in ("stable", "variable", "volatile")
        ],
    }


def build_day_features(
    rows: Iterable[dict[str, Any]], metadata: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Aggregate hourly weather into deterministic comparable-day features. @zara"""
    _var_var_65 = defaultdict(list)
    for _var_var_5 in rows:
        _var_var_65[str(_var_var_5["date"])].append(_var_var_5)
    _var_var_66 = []
    for _var_var_67, _var_var_61 in _var_var_65.items():
        _var_var_4 = sorted(_var_var_61, key=lambda item: int(item.get("hour", 0)))
        _var_var_6 = [
            _var_var_5
            for _var_var_5 in _var_var_4
            if (_number(_var_var_5.get("clear_sky_wm2"), minimum=0) or 0) >= 20
        ]
        _var_var_7 = [
            _var_var_5
            for _var_var_5 in _var_var_6
            if _number(_var_var_5.get("solar_actual_wm2"), minimum=0) is not None
        ]
        _var_var_8 = 100 * len(_var_var_7) / len(_var_var_6) if _var_var_6 else 0
        if len(_var_var_7) < MIN_DAYLIGHT_HOURS or _var_var_8 < MIN_WEATHER_COVERAGE:
            continue
        _var_var_68 = [None] * 24
        _var_var_69 = [None] * 24
        for _var_var_5 in _var_var_4:
            _var_var_26 = int(_var_var_5["hour"])
            _var_var_68[_var_var_26] = _round(
                _number(_var_var_5.get("solar_actual_wm2"), minimum=0), 1
            )
            _var_var_69[_var_var_26] = _round(
                _number(_var_var_5.get("actual"), minimum=0), 3
            )
        _var_var_35 = [
            _number(_var_var_5.get("cloud_actual_percent"), minimum=0, maximum=100)
            for _var_var_5 in _var_var_7
        ]
        _var_var_70 = [
            _number(_var_var_5.get("temperature_actual_c"), minimum=-70, maximum=70)
            for _var_var_5 in _var_var_7
        ]
        _var_var_71 = metadata.get(_var_var_67, {})
        _var_var_66.append(
            {
                "date": _var_var_67,
                "radiation_kwh_m2": round(
                    sum(
                        (
                            _number(_var_var_5.get("solar_actual_wm2"), minimum=0) or 0
                            for _var_var_5 in _var_var_7
                        )
                    )
                    / 1000,
                    2,
                ),
                "cloud_mean_percent": (
                    _round(
                        fmean(
                            (
                                _var_var_52
                                for _var_var_52 in _var_var_35
                                if _var_var_52 is not None
                            )
                        ),
                        1,
                    )
                    if any((_var_var_52 is not None for _var_var_52 in _var_var_35))
                    else None
                ),
                "temperature_mean_c": (
                    _round(
                        fmean(
                            (
                                _var_var_52
                                for _var_var_52 in _var_var_70
                                if _var_var_52 is not None
                            )
                        ),
                        1,
                    )
                    if any((_var_var_52 is not None for _var_var_52 in _var_var_70))
                    else None
                ),
                "daylight_hours": _round(
                    _number(_var_var_7[0].get("daylight_hours"), minimum=0), 2
                ),
                "weather_coverage_percent": round(_var_var_8, 1),
                "solar_profile": _var_var_68,
                "energy_profile": _var_var_69,
                "yield_kwh": _round(
                    _number(_var_var_71.get("yield_kwh"), minimum=0), 2
                ),
                "forecast_accuracy_percent": _round(
                    _number(
                        _var_var_71.get("forecast_accuracy_percent"),
                        minimum=0,
                        maximum=100,
                    ),
                    1,
                ),
                "conditions_score": _round(
                    _number(
                        _var_var_71.get("conditions_score"), minimum=0, maximum=100
                    ),
                    1,
                ),
            }
        )
    return _var_var_66
