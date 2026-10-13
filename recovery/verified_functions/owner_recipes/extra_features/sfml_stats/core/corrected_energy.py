# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from typing import Any, Mapping
from .corrections import METRIC_REGISTRY, EnergyCorrectionStore


def _pragma_names(rows: list[Any]) -> set[str]:
    _var_var_25 = set()
    for _var_var_9 in rows:
        if isinstance(_var_var_9, dict) and _var_var_9.get("name") is not None:
            _var_var_25.add(str(_var_var_9["name"]))
            continue
        try:
            _var_var_25.add(str(_var_var_9["name"]))
            continue
        except (KeyError, TypeError, IndexError):
            pass
        try:
            _var_var_25.add(str(_var_var_9[1]))
        except (KeyError, TypeError, IndexError):
            pass
    return _var_var_25


def scale_field_hours(
    rows: list[Any],
    daily: dict[str, dict[str, float]],
    projection: dict[str, dict[str, float]] | None,
    fields: tuple[str, ...] | list[str],
    *,
    present_columns: set[str] | frozenset[str] | None = None,
) -> list[Any]:
    """Scale hourly copies the way billing does. Uncorrected rows stay as they are."""
    _var_var_37 = {}
    for _var_var_33, _var_var_9 in enumerate(rows):
        _var_var_10 = _row_get(_var_var_9, "date")
        if _var_var_10 is not None:
            _var_var_37.setdefault(str(_var_var_10), []).append(
                (_var_var_33, _var_var_9)
            )
    _var_var_38 = {}
    _var_var_39 = projection or {}
    for _var_var_10, _var_var_40 in _var_var_37.items():
        _var_var_19 = _var_var_39.get(_var_var_10) or {}
        if not _var_var_19:
            continue
        _var_var_12 = daily.get(_var_var_10, {})
        for _var_var_41 in fields:
            _var_var_14 = _HOURLY_METRIC_BY_COLUMN.get(_var_var_41)
            if _var_var_14 is None or _var_var_14 not in _var_var_19:
                continue
            if present_columns is not None:
                if _var_var_41 not in present_columns:
                    continue
            _var_var_16 = f"raw_{_var_var_41}"
            if _var_var_16 not in _var_var_12 and _var_var_14 not in _var_var_12:
                continue
            _var_var_0 = float(_var_var_12.get(_var_var_16, 0.0) or 0.0)
            _var_var_1 = float(_var_var_12.get(_var_var_14, _var_var_0) or 0.0)
            _var_var_11 = [_var_var_17 for _var_var_42, _var_var_17 in _var_var_40]
            _var_var_18 = [
                float(_row_get(_var_var_17, _var_var_41) or 0.0)
                for _var_var_17 in _var_var_11
            ]
            _var_var_20 = scaling_basis_kwh(
                _var_var_0, sum(_var_var_18), corrected=True
            )
            _var_var_21 = scale_hourly_amounts(_var_var_20, _var_var_1, _var_var_18)
            if _var_var_21 is None:
                continue
            for (_var_var_33, _var_var_17), _var_var_3 in zip(_var_var_40, _var_var_21):
                _var_var_43 = _row_get(_var_var_17, _var_var_41)
                try:
                    _var_var_44 = float(_var_var_43 or 0.0)
                except (TypeError, ValueError):
                    _var_var_44 = 0.0
                if abs(_var_var_3 - _var_var_44) <= 1e-09:
                    continue
                _var_var_45 = _var_var_38.get(_var_var_33)
                if _var_var_45 is None:
                    _var_var_45 = _copy_row(_var_var_17)
                    _var_var_38[_var_var_33] = _var_var_45
                _var_var_45[_var_var_41] = _var_var_3
            continue
        continue
    if not _var_var_38:
        return rows
    return [
        _var_var_38.get(_var_var_33, _var_var_9)
        for _var_var_33, _var_var_9 in enumerate(rows)
    ]
