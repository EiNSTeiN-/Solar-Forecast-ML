# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence


def _validate_and_normalize(sample: FrozenPairedTotalStackSample) -> dict[str, Any]:
    if not isinstance(sample, FrozenPairedTotalStackSample):
        raise TypeError("samples must be FrozenPairedTotalStackSample instances")
    try:
        _var_var_17 = date.fromisoformat(sample.target_date).isoformat()
    except (TypeError, ValueError) as _var_var_18:
        raise ValueError("target_date must be an ISO calendar date") from _var_var_18
    if isinstance(sample.target_hour, bool) or not isinstance(sample.target_hour, int):
        raise ValueError("target_hour must be an integer")
    if not 0 <= sample.target_hour <= 23:
        raise ValueError("target_hour must be between 0 and 23")
    if not isinstance(sample.clean_eligible, bool):
        raise ValueError("clean_eligible is unknown")
    try:
        _var_var_19 = datetime.fromisoformat(sample.forecast_anchor)
    except (TypeError, ValueError) as _var_var_18:
        raise ValueError("forecast_anchor must be an ISO datetime") from _var_var_18
    if _var_var_19.tzinfo is None or _var_var_19.utcoffset() is None:
        raise ValueError("forecast_anchor must include a timezone")
    for _var_var_20, _var_var_13 in (
        ("input_fingerprint", sample.input_fingerprint),
        ("policy_fingerprint", sample.policy_fingerprint),
    ):
        if isinstance(_var_var_13, str) and (not len(_var_var_13) != 64):
            if not any(
                (_var_var_21 not in "0123456789abcdef" for _var_var_21 in _var_var_13)
            ):
                continue
        raise ValueError(f"{_var_var_20} must be a lowercase SHA-256 digest")
    for _var_var_20, _var_var_13 in (
        ("actual", sample.actual),
        ("champion_prediction", sample.champion_prediction),
        ("candidate_prediction", sample.candidate_prediction),
    ):
        _finite_non_negative(_var_var_13, _var_var_20)
    for _var_var_20, _var_var_13 in (
        ("weather_regime", sample.weather_regime),
        ("horizon", sample.horizon),
    ):
        if _var_var_13 is None:
            continue
        if isinstance(_var_var_13, str):
            if _var_var_13:
                continue
        raise ValueError(f"{_var_var_20} must be a non-empty string or None")
    return {
        "target_date": _var_var_17,
        "target_hour": sample.target_hour,
        "actual": float(sample.actual),
        "champion_prediction": float(sample.champion_prediction),
        "candidate_prediction": float(sample.candidate_prediction),
        "clean_eligible": sample.clean_eligible,
        "forecast_anchor": _var_var_19.isoformat(),
        "input_fingerprint": sample.input_fingerprint,
        "policy_fingerprint": sample.policy_fingerprint,
        "weather_regime": sample.weather_regime,
        "horizon": sample.horizon,
    }
