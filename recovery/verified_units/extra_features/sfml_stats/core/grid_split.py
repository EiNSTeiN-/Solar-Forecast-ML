# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""Keep grid-to-house inside the import meter.

Customers often map ``sensor_grid_to_house`` to total grid import, which
already includes battery charging, and map ``sensor_grid_to_battery`` to a
second import channel. The two flows then overlap. The import meter stays
the billable quantity. House is what remains after battery.
"""
# fmt: on
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

SPLIT_TOLERANCE_KWH = 0.02
SPLIT_TOLERANCE_W = SPLIT_TOLERANCE_KWH * 1000.0
META_GRID_SPLIT_BACKFILL = "grid_split_backfill"
META_GRID_SPLIT_DAILY_BACKFILL = "grid_split_daily_backfill"
META_GRID_SPLIT_DONE = "done"


def _finite(value: Any) -> float | None:
    if value is None or value == "":
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number != number or number in (float("inf"), float("-inf")):
        return
    return number


def _non_negative(value: Any) -> float:
    number = _finite(value)
    if number is None or number < 0.0:
        return 0.0
    return number


def split_tolerance_kwh(hours: float) -> float:
    # fmt: off
    """Scale the hourly band across a longer window.

    A day of 20 W jitter is 0.48 kWh. Using the one-hour band on a daily
    sum would treat that jitter as overlap.
    """
    # fmt: on
    try:
        duration_hours = float(hours)
    except (TypeError, ValueError):
        duration_hours = 0.0
    if duration_hours < 0.0:
        duration_hours = 0.0
    return SPLIT_TOLERANCE_KWH * duration_hours


def reconcile_grid_split(
    import_kwh: Any,
    house_kwh: Any,
    battery_kwh: Any,
    *,
    tolerance: float = SPLIT_TOLERANCE_KWH,
) -> tuple[float, float]:
    # fmt: off
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
    # fmt: on
    house = _non_negative(house_kwh)
    battery = _non_negative(battery_kwh)
    measured_import = _finite(import_kwh)
    if measured_import is None:
        return (round(house, 4), round(battery, 4))
    if measured_import < 0.0:
        measured_import = 0.0
    if house + battery <= measured_import + tolerance:
        return (round(house, 4), round(battery, 4))
    if battery > measured_import:
        battery = measured_import
    house = max(0.0, measured_import - battery)
    return (round(house, 4), round(battery, 4))


def energy_cost_ct(kwh: Any, price_ct: Any) -> float | None:
    # fmt: off
    """Cent for one flow. A missing price stays NULL, never a stored 0.

    Rounding matches SQLite ``ROUND`` (half away from zero), so the
    aggregator, reprice, and the backfill agree on the same cent.
    """
    # fmt: on
    energy = _finite(kwh)
    price = _finite(price_ct)
    if energy is None or price is None:
        return
    cost = (Decimal(str(energy)) * Decimal(str(price))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(cost)


def displayed_grid_import_kwh(
    stored_import: Any, house_kwh: Any, battery_kwh: Any, *, meter_configured: bool
) -> float:
    # fmt: off
    """Import shown from collector daily rows.

    A configured smart-meter sensor stores an independent import. That
    value is the import, including a measured 0. Without the sensor the
    house channel is documented as house-only, so import is house plus
    battery. A stored 0 on that path is the column default, not a meter.
    """
    # fmt: on
    measured_import = _finite(stored_import)
    if meter_configured and measured_import is not None:
        if measured_import < 0.0:
            return 0.0
        return measured_import
    return _non_negative(house_kwh) + _non_negative(battery_kwh)


def billed_import_kwh(
    meter_kwh: Any, house_kwh: Any, battery_kwh: Any, *, meter_configured: bool
) -> float:
    # fmt: off
    """Bill the import meter. Do not add house and battery on top.

    Hourly rows are only written when ``sensor_smartmeter_import`` is
    set, so ``meter_configured=True`` uses ``grid_import_kwh`` even when
    that hour is 0. Without a meter and without a stored meter total,
    ``grid_to_house_kwh`` is the legacy Riemann stand-in for import.
    Adding ``grid_to_battery_kwh`` would count the typical overlapping
    house sensor a second time. Battery remains a split, not a purchase.
    """
    # fmt: on
    measured_import = _finite(meter_kwh)
    if meter_configured:
        if measured_import is None or measured_import < 0.0:
            return 0.0
        return measured_import
    if measured_import is not None and measured_import > 0.0:
        return measured_import
    return _non_negative(house_kwh)


def battery_export_cost_sql(
    price_expr: str, *, column_present: bool, backfill_done: bool
) -> str:
    # fmt: off
    """Export expression for grid-to-battery cents.

    Until the backfill marker is ``done``, a stored 0 with energy and a
    price is the legacy column default, not a calculated cost.
    """
    # fmt: on
    if not column_present:
        return f"COALESCE(grid_to_battery_kwh, 0) * {price_expr}"
    if backfill_done:
        return "COALESCE(grid_to_battery_cost_ct, 0)"
    return f"CASE WHEN COALESCE(grid_to_battery_cost_ct, 0) = 0 AND COALESCE(grid_to_battery_kwh, 0) > 0 AND ({price_expr}) > 0 THEN COALESCE(grid_to_battery_kwh, 0) * ({price_expr}) ELSE COALESCE(grid_to_battery_cost_ct, 0) END"


def house_export_cost_sql(
    price_expr: str, *, column_present: bool, backfill_done: bool
) -> str:
    # fmt: off
    """Export expression for grid-to-house cents.

    Same marker and NULL rules as ``battery_export_cost_sql``.
    """
    # fmt: on
    if not column_present:
        return f"COALESCE(grid_to_house_kwh, 0) * {price_expr}"
    if backfill_done:
        return "COALESCE(grid_to_house_cost_ct, 0)"
    return f"CASE WHEN COALESCE(grid_to_house_cost_ct, 0) = 0 AND COALESCE(grid_to_house_kwh, 0) > 0 AND ({price_expr}) > 0 THEN COALESCE(grid_to_house_kwh, 0) * ({price_expr}) ELSE COALESCE(grid_to_house_cost_ct, 0) END"
