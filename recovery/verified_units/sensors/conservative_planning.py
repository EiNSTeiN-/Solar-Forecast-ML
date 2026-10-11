# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
import math
from datetime import date, datetime
from typing import Optional
DEFAULT_SFML_WEIGHT = 0.65
DEFAULT_TFS_P10_WEIGHT = 0.35
P10_PERSISTENCE_MAX_AGE_HOURS = 30.0
P10_MORNING_RECOMPUTE_WINDOW_MINUTES = 45.0
P10_TRACKING_COLUMNS = ('conservative_planning_forecast_kwh', 'conservative_planning_forecast_updated_at', 'conservative_planning_forecast_hours_json', 'conservative_planning_forecast_panel_groups_json', 'conservative_planning_forecast_group_totals_json')

def _non_negative_float(value: object) -> Optional[float]:
    if value is None:
        return
    try:
        _var_var_0 = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(_var_var_0):
        return
    return max(0.0, _var_var_0)

def _parse_date(value: object) -> Optional[date]:
    if value is None:
        return
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return

def parse_p10_timestamp(value: object) -> Optional[datetime]:
    if value is None:
        return
    if isinstance(value, datetime):
        return value
    _var_var_1 = str(value).strip()
    if not _var_var_1:
        return
    if _var_var_1.endswith('Z'):
        _var_var_1 = f'{_var_var_1[:-1]}+00:00'
    try:
        return datetime.fromisoformat(_var_var_1)
    except ValueError:
        return

def p10_age_hours(updated_at: object, now: Optional[datetime]=None) -> Optional[float]:
    _var_var_2 = parse_p10_timestamp(updated_at)
    if _var_var_2 is None:
        return
    _var_var_3 = now or datetime.now(_var_var_2.tzinfo)
    if _var_var_2.tzinfo is None and _var_var_3.tzinfo is not None:
        _var_var_3 = _var_var_3.replace(tzinfo=None)
    elif _var_var_2.tzinfo is not None and _var_var_3.tzinfo is None:
        _var_var_3 = _var_var_3.replace(tzinfo=_var_var_2.tzinfo)
    return round((_var_var_3 - _var_var_2).total_seconds() / 3600.0, 3)

def persisted_p10_freshness(forecast_date: object, updated_at: object, *, now: Optional[datetime]=None, max_age_hours: float=P10_PERSISTENCE_MAX_AGE_HOURS) -> dict:
    _var_var_4 = _parse_date(forecast_date)
    _var_var_5 = parse_p10_timestamp(updated_at)
    _var_var_6 = p10_age_hours(_var_var_5, now)
    if _var_var_4 is None:
        return {'fresh': False, 'reason': 'missing_forecast_date', 'age_hours': _var_var_6}
    if _var_var_5 is None:
        return {'fresh': False, 'reason': 'missing_p10_updated_at', 'age_hours': _var_var_6}
    if _var_var_5.date() != _var_var_4:
        return {'fresh': False, 'reason': 'stale_persisted_p10', 'age_hours': _var_var_6}
    if _var_var_6 is not None and _var_var_6 > max_age_hours:
        return {'fresh': False, 'reason': 'expired_persisted_p10', 'age_hours': _var_var_6}
    return {'fresh': True, 'reason': 'fresh_persisted_p10', 'age_hours': _var_var_6}

def p10_morning_recompute_allowed(forecast_date: object, lock_source: object, locked_at: object, *, now: Optional[datetime]=None, window_minutes: float=P10_MORNING_RECOMPUTE_WINDOW_MINUTES) -> bool:
    if str(lock_source or '') != 'morning_routine':
        return False
    _var_var_4 = _parse_date(forecast_date)
    _var_var_7 = parse_p10_timestamp(locked_at)
    if _var_var_4 is None or _var_var_7 is None:
        return False
    if _var_var_7.date() != _var_var_4:
        return False
    _var_var_3 = now or datetime.now(_var_var_7.tzinfo)
    if _var_var_7.tzinfo is None and _var_var_3.tzinfo is not None:
        _var_var_3 = _var_var_3.replace(tzinfo=None)
    elif _var_var_7.tzinfo is not None and _var_var_3.tzinfo is None:
        _var_var_3 = _var_var_3.replace(tzinfo=_var_var_7.tzinfo)
    _var_var_8 = (_var_var_3 - _var_var_7).total_seconds() / 60.0
    return 0.0 <= _var_var_8 <= window_minutes

def round_not_above(value: float, upper_bound: float, precision: int) -> float:
    _var_var_9 = max(0.0, float(upper_bound or 0.0))
    _var_var_10 = round(max(0.0, float(value or 0.0)), precision)
    if _var_var_10 <= _var_var_9:
        return _var_var_10
    _var_var_11 = 10 ** precision
    return round(math.floor(_var_var_9 * _var_var_11) / _var_var_11, precision)

def conservative_planning_value(sfml_value: object, tfs_p10_value: object, *, sfml_weight: float=DEFAULT_SFML_WEIGHT, tfs_weight: float=DEFAULT_TFS_P10_WEIGHT, precision: int=4) -> float:
    _var_var_12 = _non_negative_float(sfml_value) or 0.0
    _var_var_13 = _non_negative_float(tfs_p10_value)
    if _var_var_13 is None:
        _var_var_13 = _var_var_12
    _var_var_14 = _var_var_12 * sfml_weight + _var_var_13 * tfs_weight
    _var_var_15 = max(0.0, min(_var_var_12, _var_var_14))
    return round_not_above(_var_var_15, _var_var_12, precision)

def cap_persisted_conservative_value(persisted_value: object, full_day_sfml_cap: object, *, precision: int=2) -> Optional[float]:
    _var_var_16 = _non_negative_float(persisted_value)
    _var_var_17 = _non_negative_float(full_day_sfml_cap)
    if _var_var_16 is None:
        return
    if _var_var_17 is None:
        return round(_var_var_16, precision)
    return round_not_above(min(_var_var_16, _var_var_17), _var_var_17, precision)

def remaining_hourly_forecast_kwh(hourly_values: object, current_hour: int, *, precision: int=2) -> Optional[float]:
    """Sum forecast kWh for hours at or after the current local hour."""
    if not isinstance(hourly_values, dict) or not hourly_values:
        return
    try:
        _var_var_18 = int(current_hour)
    except (TypeError, ValueError):
        return
    if _var_var_18 < 0 or _var_var_18 > 23:
        return
    _var_var_19 = 0.0
    _var_var_20 = False
    for _var_var_21, _var_var_22 in hourly_values.items():
        _var_var_23 = None
        if isinstance(_var_var_21, int):
            _var_var_23 = _var_var_21
        else:
            _var_var_1 = str(_var_var_21).strip()
            if not _var_var_1:
                continue
            try:
                _var_var_23 = int(_var_var_1.split(':', 1)[0])
            except ValueError:
                continue
        if _var_var_23 < _var_var_18 or _var_var_23 > 23:
            continue
        _var_var_16 = _non_negative_float(_var_var_22)
        if _var_var_16 is not None:
            _var_var_20 = True
            _var_var_19 += _var_var_16
    if not _var_var_20:
        return
    return round(max(0.0, _var_var_19), precision)

def remaining_today_expected_kwh(*, current_hour: int, hourly_values: object=None, expected_day_kwh: object=None, actual_today_kwh: object=None, precision: int=2) -> float:
    """Rest-of-day expected energy for the Resttag sensor.

    Prefer remaining hourly forecast from now, and never exceed
    max(0, expected_day - actual_today) when that cap is known.
    """
    _var_var_24 = remaining_hourly_forecast_kwh(hourly_values, current_hour, precision=precision)
    _var_var_25 = _non_negative_float(actual_today_kwh) or 0.0
    _var_var_26 = _non_negative_float(expected_day_kwh)
    _var_var_27 = None
    if _var_var_26 is not None:
        _var_var_27 = round(max(0.0, _var_var_26 - _var_var_25), precision)
    if _var_var_24 is not None and _var_var_27 is not None:
        return round(min(_var_var_24, _var_var_27), precision)
    if _var_var_24 is not None:
        return _var_var_24
    if _var_var_27 is not None:
        return _var_var_27
    return 0.0

def remaining_conservative_value(conservative_full_day: object, actual_today: object, today_forecast_cap: object, *, precision: int=2) -> Optional[float]:
    _var_var_28 = _non_negative_float(conservative_full_day)
    if _var_var_28 is None:
        return
    _var_var_25 = _non_negative_float(actual_today) or 0.0
    _var_var_29 = max(0.0, _var_var_28 - _var_var_25)
    _var_var_17 = _non_negative_float(today_forecast_cap)
    if _var_var_17 is not None:
        _var_var_29 = min(_var_var_29, _var_var_17)
        return round_not_above(_var_var_29, _var_var_17, precision)
    return round(_var_var_29, precision)
