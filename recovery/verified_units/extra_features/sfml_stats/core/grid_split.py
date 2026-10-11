# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Keep grid-to-house inside the import meter.

Customers often map ``sensor_grid_to_house`` to total grid import, which
already includes battery charging, and map ``sensor_grid_to_battery`` to a
second import channel. The two flows then overlap. The import meter stays
the billable quantity. House is what remains after battery.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
SPLIT_TOLERANCE_KWH = 0.02
SPLIT_TOLERANCE_W = SPLIT_TOLERANCE_KWH * 1000.0
META_GRID_SPLIT_BACKFILL = 'grid_split_backfill'
META_GRID_SPLIT_DAILY_BACKFILL = 'grid_split_daily_backfill'
META_GRID_SPLIT_DONE = 'done'

def _finite(value: Any) -> float | None:
    if value is None or value == '':
        return
    try:
        _var_var_0 = float(value)
    except (TypeError, ValueError):
        return
    if _var_var_0 != _var_var_0 or _var_var_0 in (float('inf'), float('-inf')):
        return
    return _var_var_0

def _non_negative(value: Any) -> float:
    _var_var_0 = _finite(value)
    if _var_var_0 is None or _var_var_0 < 0.0:
        return 0.0
    return _var_var_0

def split_tolerance_kwh(hours: float) -> float:
    """Scale the hourly band across a longer window.

    A day of 20 W jitter is 0.48 kWh. Using the one-hour band on a daily
    sum would treat that jitter as overlap.
    """
    try:
        _var_var_1 = float(hours)
    except (TypeError, ValueError):
        _var_var_1 = 0.0
    if _var_var_1 < 0.0:
        _var_var_1 = 0.0
    return SPLIT_TOLERANCE_KWH * _var_var_1

def reconcile_grid_split(import_kwh: Any, house_kwh: Any, battery_kwh: Any, *, tolerance: float=SPLIT_TOLERANCE_KWH) -> tuple[float, float]:
    """Return house and battery that fit a measured import.

    ``import_kwh is None`` means no import sample exists. The measured
    flows stay as they are. The config-flow text describes
    ``sensor_grid_to_house`` as power directly into the house, excluding
    battery charging, so a missing import is not evidence that the house
    sensor already contains the battery. A number, including 0, is a
    measurement: battery above the import is capped to the import (the
    meter is the ceiling; the excess is not billed as extra grid charge),
    and house becomes the non-negative remainder when the two flows
    exceed the import by more than ``tolerance``. ``tolerance`` uses the
    same unit as the three arguments (kWh or W).
    """
    _var_var_2 = _non_negative(house_kwh)
    _var_var_3 = _non_negative(battery_kwh)
    _var_var_4 = _finite(import_kwh)
    if _var_var_4 is None:
        return (round(_var_var_2, 4), round(_var_var_3, 4))
    if _var_var_4 < 0.0:
        _var_var_4 = 0.0
    if _var_var_2 + _var_var_3 <= _var_var_4 + tolerance:
        return (round(_var_var_2, 4), round(_var_var_3, 4))
    if _var_var_3 > _var_var_4:
        _var_var_3 = _var_var_4
    _var_var_2 = max(0.0, _var_var_4 - _var_var_3)
    return (round(_var_var_2, 4), round(_var_var_3, 4))

def energy_cost_ct(kwh: Any, price_ct: Any) -> float | None:
    """Cent for one flow. A missing price stays NULL, never a stored 0.

    Rounding matches SQLite ``ROUND`` (half away from zero), so the
    aggregator, reprice, and the backfill agree on the same cent.
    """
    _var_var_5 = _finite(kwh)
    _var_var_6 = _finite(price_ct)
    if _var_var_5 is None or _var_var_6 is None:
        return
    _var_var_7 = (Decimal(str(_var_var_5)) * Decimal(str(_var_var_6))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(_var_var_7)

def displayed_grid_import_kwh(stored_import: Any, house_kwh: Any, battery_kwh: Any, *, meter_configured: bool) -> float:
    """Import shown from collector daily rows.

    A configured smart-meter sensor stores an independent import. That
    value is the import, including a measured 0. Without the sensor the
    house channel is documented as house-only, so import is house plus
    battery. A stored 0 on that path is the column default, not a meter.
    """
    _var_var_4 = _finite(stored_import)
    if meter_configured and _var_var_4 is not None:
        if _var_var_4 < 0.0:
            return 0.0
        return _var_var_4
    return _non_negative(house_kwh) + _non_negative(battery_kwh)

def billed_import_kwh(meter_kwh: Any, house_kwh: Any, battery_kwh: Any, *, meter_configured: bool) -> float:
    """Bill the import meter. Do not add house and battery on top.

    Hourly rows are only written when ``sensor_smartmeter_import`` is
    set, so ``meter_configured=True`` uses ``grid_import_kwh`` even when
    that hour is 0. Without a meter and without a stored meter total,
    ``grid_to_house_kwh`` is the legacy Riemann stand-in for import.
    Adding ``grid_to_battery_kwh`` would count the typical overlapping
    house sensor a second time. Battery remains a split, not a purchase.
    """
    _var_var_4 = _finite(meter_kwh)
    if meter_configured:
        if _var_var_4 is None or _var_var_4 < 0.0:
            return 0.0
        return _var_var_4
    if _var_var_4 is not None and _var_var_4 > 0.0:
        return _var_var_4
    return _non_negative(house_kwh)

def battery_export_cost_sql(price_expr: str, *, column_present: bool, backfill_done: bool) -> str:
    """Export expression for grid-to-battery cents.

    Until the backfill marker is ``done``, a stored 0 with energy and a
    price is the legacy column default, not a calculated cost.
    """
    if not column_present:
        return f'COALESCE(grid_to_battery_kwh, 0) * {price_expr}'
    if backfill_done:
        return 'COALESCE(grid_to_battery_cost_ct, 0)'
    return f'CASE WHEN COALESCE(grid_to_battery_cost_ct, 0) = 0 AND COALESCE(grid_to_battery_kwh, 0) > 0 AND ({price_expr}) > 0 THEN COALESCE(grid_to_battery_kwh, 0) * ({price_expr}) ELSE COALESCE(grid_to_battery_cost_ct, 0) END'

def house_export_cost_sql(price_expr: str, *, column_present: bool, backfill_done: bool) -> str:
    """Export expression for grid-to-house cents.

    Same marker and NULL rules as ``battery_export_cost_sql``.
    """
    if not column_present:
        return f'COALESCE(grid_to_house_kwh, 0) * {price_expr}'
    if backfill_done:
        return 'COALESCE(grid_to_house_cost_ct, 0)'
    return f'CASE WHEN COALESCE(grid_to_house_cost_ct, 0) = 0 AND COALESCE(grid_to_house_kwh, 0) > 0 AND ({price_expr}) > 0 THEN COALESCE(grid_to_house_kwh, 0) * ({price_expr}) ELSE COALESCE(grid_to_house_cost_ct, 0) END'
