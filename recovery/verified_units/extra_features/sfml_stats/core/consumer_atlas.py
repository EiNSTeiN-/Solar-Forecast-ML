# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Consumer atlas helpers for SFML Stats."""
from __future__ import annotations
import calendar
import re
from datetime import date, timedelta
from typing import Any
DEFAULT_CONSUMER_COLORS = ('#06b6d4', '#22c55e', '#f59e0b', '#ec4899', '#8b5cf6', '#ef4444', '#14b8a6', '#f97316')
VALID_CONSUMER_CATEGORIES = {'other', 'office', 'heating', 'kitchen', 'utility', 'mobility', 'entertainment'}

def _to_float(value: Any, fallback: float=0.0) -> float:
    try:
        _var_var_0 = float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return fallback
    if _var_var_0 != _var_var_0:
        return fallback
    return _var_var_0

def _to_int(value: Any, fallback: int=0) -> int:
    try:
        return int(float(str(value).replace(',', '.')))
    except (TypeError, ValueError):
        return fallback

def make_consumer_id(name: Any, entity_id: Any) -> str:
    _var_var_1 = str(entity_id or name or 'consumer').strip().lower()
    _var_var_1 = _var_var_1.replace('sensor.', '')
    _var_var_1 = re.sub('[^a-z0-9_]+', '_', _var_var_1)
    _var_var_1 = re.sub('_+', '_', _var_var_1).strip('_')
    return (_var_var_1 or 'consumer')[:64]

def sanitize_consumer_config(payload: dict[str, Any] | None, current: dict[str, Any] | None=None, *, default_sort_order: int=0) -> dict[str, Any]:
    _var_var_2 = {**(current or {}), **(payload or {})}
    _var_var_3 = str(_var_var_2.get('entity_id') or '').strip()
    _var_var_4 = str(_var_var_2.get('name') or '').strip()
    if not _var_var_4:
        if _var_var_3:
            _var_var_4 = _var_var_3.split('.', 1)[-1].replace('_', ' ').strip().title()
    _var_var_5 = str(_var_var_2.get('consumer_id') or _var_var_2.get('id') or '').strip()
    if not _var_var_5:
        _var_var_5 = make_consumer_id(_var_var_4, _var_var_3)
    _var_var_5 = make_consumer_id(_var_var_5, _var_var_5)
    _var_var_6 = str(_var_var_2.get('category') or 'other').strip().lower()
    if _var_var_6 not in VALID_CONSUMER_CATEGORIES:
        _var_var_6 = 'other'
    _var_var_7 = re.sub('[^a-z0-9_-]+', '', str(_var_var_2.get('icon') or 'plug').strip().lower())[:32]
    if not _var_var_7:
        _var_var_7 = 'plug'
    _var_var_8 = str(_var_var_2.get('color') or '').strip()
    if not re.fullmatch('#[0-9a-fA-F]{6}', _var_var_8):
        _var_var_8 = DEFAULT_CONSUMER_COLORS[abs(hash(_var_var_5)) % len(DEFAULT_CONSUMER_COLORS)]
    _var_var_9 = _var_var_2.get('enabled', True)
    _var_var_10 = bool(_var_var_9)
    if isinstance(_var_var_9, str):
        _var_var_10 = _var_var_9.strip().lower() not in {'0', 'no', 'off', 'false'}
    _var_var_11 = _to_int(_var_var_2.get('sort_order'), default_sort_order)
    _var_var_11 = max(0, min(9999, _var_var_11))
    _var_var_12 = _to_float(_var_var_2.get('start_kwh', _var_var_2.get('initial_kwh', 0.0)))
    _var_var_12 = max(0.0, min(999999.0, _var_var_12))
    return {'consumer_id': _var_var_5, 'name': _var_var_4[:80], 'entity_id': _var_var_3[:160], 'category': _var_var_6, 'icon': _var_var_7, 'color': _var_var_8.lower(), 'start_kwh': round(_var_var_12, 4), 'enabled': _var_var_10, 'sort_order': _var_var_11}

def valid_consumer_config(config: dict[str, Any]) -> bool:
    return bool(config.get('consumer_id') and config.get('name') and config.get('entity_id'))

def calculate_billing_period(today: date, start_month: Any, start_day: Any) -> tuple[date, date]:
    _var_var_13 = max(1, min(12, _to_int(start_month, 1) or 1))
    _var_var_14 = max(1, _to_int(start_day, 1) or 1)
    _var_var_15 = min(_var_var_14, calendar.monthrange(today.year, _var_var_13)[1])
    _var_var_16 = date(today.year, _var_var_13, _var_var_15)
    if _var_var_16 > today:
        _var_var_17 = min(_var_var_14, calendar.monthrange(today.year - 1, _var_var_13)[1])
        _var_var_16 = date(today.year - 1, _var_var_13, _var_var_17)
    _var_var_18 = min(_var_var_14, calendar.monthrange(_var_var_16.year + 1, _var_var_13)[1])
    _var_var_19 = date(_var_var_16.year + 1, _var_var_13, _var_var_18) - timedelta(days=1)
    return (_var_var_16, _var_var_19)

def iter_backfill_dates(start: date, end: date, *, max_days: int=370) -> list[date]:
    if end < start:
        return []
    max_days = max(1, min(370, int(max_days or 370)))
    _var_var_20 = max(start, end - timedelta(days=max_days - 1))
    _var_var_21 = (end - _var_var_20).days + 1
    return [_var_var_20 + timedelta(days=_var_var_22) for _var_var_22 in range(_var_var_21)]

def build_consumer_atlas_summary(configs: list[dict[str, Any]], daily_rows: list[dict[str, Any]], *, home_consumption_kwh: float=0.0, today: date | None=None, live_power: dict[str, float] | None=None) -> dict[str, Any]:
    _var_var_23 = today.isoformat() if today else None
    live_power = live_power or {}
    _var_var_24 = {}
    for _var_var_25 in configs:
        _var_var_24[_var_var_25['consumer_id']] = {**_var_var_25, 'period_kwh': max(0.0, _to_float(_var_var_25.get('start_kwh'))), 'today_kwh': 0.0, 'peak_w': 0.0, 'peak_time': None, 'last_power_w': live_power.get(_var_var_25['consumer_id'], 0.0), 'samples': 0}
    _var_var_26 = {}
    for _var_var_27 in daily_rows:
        _var_var_5 = str(_var_var_27.get('consumer_id') or '')
        if _var_var_5 not in _var_var_24:
            continue
        _var_var_28 = str(_var_var_27.get('date') or '')
        _var_var_29 = max(0.0, _to_float(_var_var_27.get('kwh')))
        _var_var_30 = max(0.0, _to_float(_var_var_27.get('peak_w')))
        _var_var_31 = max(0, _to_int(_var_var_27.get('samples')))
        _var_var_24[_var_var_5]['period_kwh'] += _var_var_29
        _var_var_24[_var_var_5]['samples'] += _var_var_31
        if _var_var_23 and _var_var_28 == _var_var_23:
            _var_var_24[_var_var_5]['today_kwh'] += _var_var_29
            if _var_var_5 not in live_power:
                _var_var_24[_var_var_5]['last_power_w'] = max(0.0, _to_float(_var_var_27.get('last_power_w')))
        if _var_var_30 > _var_var_24[_var_var_5]['peak_w']:
            _var_var_24[_var_var_5]['peak_w'] = _var_var_30
            _var_var_24[_var_var_5]['peak_time'] = _var_var_27.get('peak_time')
        _var_var_14 = _var_var_26.setdefault(_var_var_28, {'date': _var_var_28, 'total_known_kwh': 0.0, 'consumers': {}})
        _var_var_14['consumers'][_var_var_5] = round(_var_var_14['consumers'].get(_var_var_5, 0.0) + _var_var_29, 4)
        _var_var_14['total_known_kwh'] += _var_var_29
        continue
    _var_var_32 = sum((_var_var_37['period_kwh'] for _var_var_37 in _var_var_24.values()))
    _var_var_33 = max(0.0, _to_float(home_consumption_kwh))
    _var_var_34 = max(0.0, _var_var_33 - _var_var_32)
    _var_var_35 = _var_var_33 if _var_var_33 > 0 else _var_var_32
    _var_var_36 = []
    for _var_var_37 in _var_var_24.values():
        _var_var_38 = _var_var_37['period_kwh'] / _var_var_35 * 100.0 if _var_var_35 > 0 else 0.0
        _var_var_36.append({**_var_var_37, 'period_kwh': round(_var_var_37['period_kwh'], 4), 'today_kwh': round(_var_var_37['today_kwh'], 4), 'peak_w': round(_var_var_37['peak_w'], 1), 'last_power_w': round(_var_var_37['last_power_w'], 1), 'share_percent': round(_var_var_38, 1)})
    _var_var_36.sort(key=lambda item: (-item['period_kwh'], item['sort_order'], item['name']))
    _var_var_39 = sum((max(0.0, _var_var_37['last_power_w']) for _var_var_37 in _var_var_36))
    _var_var_40 = _var_var_36[0] if _var_var_36 else None
    return {'summary': {'configured_count': len(configs), 'enabled_count': sum((1 for _var_var_37 in configs if _var_var_37.get('enabled'))), 'known_kwh': round(_var_var_32, 4), 'home_consumption_kwh': round(_var_var_33, 4), 'unknown_kwh': round(_var_var_34, 4), 'active_power_w': round(_var_var_39, 1), 'top_consumer': _var_var_40}, 'consumers': _var_var_36, 'daily': [{**_var_var_27, 'total_known_kwh': round(_var_var_27['total_known_kwh'], 4)} for _var_var_27 in sorted(_var_var_26.values(), key=lambda item: item['date'])]}

def integrate_power_to_kwh(power_w: Any, interval_seconds: int | float) -> float:
    _var_var_41 = max(0.0, _to_float(power_w))
    _var_var_42 = max(0.0, _to_float(interval_seconds))
    return _var_var_41 * _var_var_42 / 3600000
