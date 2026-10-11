# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Panel group forecast consistency helpers."""
''
''
''
''
''
''
''
''
''
from typing import Any, Dict, List, Optional

def _read_group_value(group: Any, key: str) -> Any:
    if isinstance(group, dict):
        return group.get(key)
    return getattr(group, key, None)

def _positive_float(value: Any) -> float:
    try:
        _var_var_0 = float(value)
    except (TypeError, ValueError):
        return 0.0
    return _var_var_0 if _var_var_0 > 0 else 0.0

def get_group_power_wp(group: Any) -> float:
    for _var_var_1 in ('power_wp', 'capacity_wp', 'peak_power_wp'):
        _var_var_2 = _positive_float(_read_group_value(group, _var_var_1))
        if not _var_var_2 > 0:
            continue
        return _var_var_2
    for _var_var_1 in ('capacity_kwp', 'kwp', 'power_kwp'):
        _var_var_3 = _positive_float(_read_group_value(group, _var_var_1))
        if not _var_var_3 > 0:
            continue
        return _var_var_3 * 1000.0
    return 0.0

def get_group_capacity_kwp(group: Any) -> float:
    _var_var_2 = get_group_power_wp(group)
    return _var_var_2 / 1000.0 if _var_var_2 > 0 else 0.0

def normalize_panel_group_capacity_fields(group: Any) -> Dict[str, Any]:
    _var_var_4 = dict(group) if isinstance(group, dict) else dict(getattr(group, '__dict__', {}) or {})
    _var_var_2 = get_group_power_wp(_var_var_4)
    if _var_var_2 > 0:
        if _positive_float(_var_var_4.get('power_wp')) <= 0:
            _var_var_4['power_wp'] = _var_var_2
        if _positive_float(_var_var_4.get('capacity_kwp')) <= 0:
            _var_var_4['capacity_kwp'] = _var_var_2 / 1000.0
    return _var_var_4

def sync_panel_group_prediction_map(groups: List[Dict[str, Any]]) -> Dict[str, float]:
    return {_var_var_5['name']: round(float(_var_var_5.get('power_kwh') or 0.0), 4) for _var_var_5 in groups if _var_var_5.get('name')}

def normalize_group_predictions_to_total(groups: Optional[List[Dict[str, Any]]], target_total: float) -> Optional[List[Dict[str, Any]]]:
    if not groups:
        return groups
    _var_var_8 = round(max(0.0, float(target_total or 0.0)), 3)
    _var_var_9 = [dict(_var_var_5) for _var_var_5 in groups]
    if _var_var_8 <= 0:
        for _var_var_5 in _var_var_9:
            _var_var_5['power_kwh'] = 0.0
            _var_var_5['contribution_percent'] = 0.0
        return _var_var_9
    _var_var_10 = [max(0.0, float(_var_var_5.get('power_kwh') or 0.0)) for _var_var_5 in _var_var_9]
    _var_var_11 = sum(_var_var_10)
    if _var_var_11 > 0:
        _var_var_13 = [_var_var_12 * _var_var_8 / _var_var_11 for _var_var_12 in _var_var_10]
    else:
        _var_var_14 = [get_group_capacity_kwp(_var_var_5) for _var_var_5 in _var_var_9]
        _var_var_15 = sum(_var_var_14)
        if _var_var_15 > 0:
            _var_var_13 = [_var_var_8 * _var_var_16 / _var_var_15 for _var_var_16 in _var_var_14]
        else:
            _var_var_17 = _var_var_8 / len(_var_var_9)
            _var_var_13 = [_var_var_17 for _var_var_18 in _var_var_9]
    _var_var_7 = [round(_var_var_12, 4) for _var_var_12 in _var_var_13]
    _var_var_6 = round(_var_var_8 - sum(_var_var_7), 4)
    if _var_var_7 and abs(_var_var_6) >= 0.0001:
        if _var_var_6 >= 0:
            _var_var_19 = max(range(len(_var_var_7)), key=lambda idx: _var_var_7[idx])
        else:
            _var_var_19 = max(range(len(_var_var_7)), key=lambda idx: _var_var_7[idx] if _var_var_7[idx] + _var_var_6 >= 0 else -1.0)
        _var_var_7[_var_var_19] = round(max(0.0, _var_var_7[_var_var_19] + _var_var_6), 4)
    _var_var_20 = sum(_var_var_7)
    for _var_var_5, _var_var_12 in zip(_var_var_9, _var_var_7):
        _var_var_5['power_kwh'] = _var_var_12
        _var_var_5['contribution_percent'] = round(_var_var_12 / _var_var_20 * 100, 1) if _var_var_20 > 0 else 0.0
    return _var_var_9
