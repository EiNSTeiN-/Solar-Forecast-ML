# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""One-shot repair of hourly grid split and split costs.

The suite database uses SQLite's delete journal. A single transaction
over every historical hour would rewrite the journal for the whole
table and block collectors. Each batch commits at most ``BATCH_ROWS``
primary-key updates, then releases the writer lock for ``BATCH_PAUSE_S``.

One year is about 8 800 hours: 44 batches. The pause alone is about
9 seconds; with the updates the pass stays well under a minute and
never runs on the startup await path. A restart continues after the
last committed ``hour_key``. The marker ``done`` makes a later start
a no-op. Applying the same split twice does not change the row.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, Awaitable, Callable
from .grid_split import (
    META_GRID_SPLIT_BACKFILL,
    META_GRID_SPLIT_DAILY_BACKFILL,
    META_GRID_SPLIT_DONE,
    energy_cost_ct,
    reconcile_grid_split,
    split_tolerance_kwh,
)
from .price_mode import MODE_NONE, _present_price, resolve_hour_price

_LOGGER = logging.getLogger(__name__)
BATCH_ROWS = 200
BATCH_PAUSE_S = 0.2
_ROW_SQL = "\nSELECT hb.hour_key, hb.date, hb.hour, hb.price_ct_kwh,\n       hb.grid_import_kwh, hb.grid_to_house_kwh, hb.grid_to_battery_kwh,\n       hb.grid_to_house_cost_ct, hb.grid_to_battery_cost_ct,\n       (\n           SELECT gpm.total_price FROM GPM_price_history gpm\n           WHERE gpm.hour = hb.hour\n             AND gpm.timestamp >= hb.date || 'T00:00:00'\n             AND gpm.timestamp < date(hb.date, '+1 day') || 'T00:00:00'\n           ORDER BY gpm.timestamp DESC\n           LIMIT 1\n       ) AS gpm_total\nFROM stats_hourly_billing hb\nWHERE hb.hour_key > ?\n  AND hb.hour_key != ?\nORDER BY hb.hour_key\nLIMIT ?\n"


def _col(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[name]
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _same_kwh(left: Any, right: float) -> bool:
    try:
        return abs(float(left) - right) <= 5e-05
    except (TypeError, ValueError):
        return False


def _same_cost(stored: Any, expected: float | None) -> bool:
    if expected is None:
        return stored is None
    if stored is None:
        return False
    try:
        return abs(float(stored) - expected) < 0.001
    except (TypeError, ValueError):
        return False


async def _meta_get(db: Any, key: str) -> str | None:
    try:
        async with db.execute(
            "SELECT value FROM stats_schema_meta WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
    except Exception:
        return
    if row is None:
        return
    return row["value"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]


async def _meta_set(db: Any, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO stats_schema_meta (key, value, updated_at)\n           VALUES (?, ?, CURRENT_TIMESTAMP)\n           ON CONFLICT(key) DO UPDATE SET\n             value = excluded.value,\n             updated_at = excluded.updated_at",
        (key, value),
    )


async def _load_averages(db: Any) -> dict[str, float]:
    daily_averages = {}
    try:
        async with db.execute(
            "SELECT date, average_total FROM GPM_daily_averages"
        ) as cursor:
            async for row in cursor:
                date_key = _col(row, "date", 0)
                price_ct = _present_price(_col(row, "average_total", 1))
                if price_ct is not None and date_key:
                    daily_averages[str(date_key)] = price_ct
    except Exception:
        return {}
    try:
        pass
    except Exception:
        return {}
    return daily_averages


async def _current_average(db: Any) -> float | None:
    try:
        async with db.execute(
            "SELECT average_today FROM GPM_current_price WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
    except Exception:
        return
    if row is None:
        return
    return _present_price(_col(row, "average_today", 0))


def _price_for_row(
    row: Any,
    *,
    mode: str,
    config: dict[str, Any],
    fixed_total_ct: float | None,
    daily_averages: dict[str, float],
    current_average: float | None,
    today_str: str,
) -> float | None:
    if mode == MODE_NONE:
        return
    row_date = str(_col(row, "date", 1) or "")
    return resolve_hour_price(
        gpm_total=_col(row, "gpm_total", 9),
        stored_price=_col(row, "price_ct_kwh", 3),
        daily_average=daily_averages.get(row_date),
        current_average=current_average if row_date == today_str else None,
        mode=mode,
        config=config,
        fixed_total_ct=fixed_total_ct,
    )


async def _apply_row(db: Any, row: Any, price: float | None) -> bool:
    grid_import_kwh = _col(row, "grid_import_kwh", 4)
    raw_house_kwh = _col(row, "grid_to_house_kwh", 5)
    raw_battery_kwh = _col(row, "grid_to_battery_kwh", 6)
    house_kwh, battery_kwh = reconcile_grid_split(
        grid_import_kwh, raw_house_kwh, raw_battery_kwh
    )
    house_cost_ct = energy_cost_ct(house_kwh, price)
    battery_cost_ct = energy_cost_ct(battery_kwh, price)
    if (
        _same_kwh(raw_house_kwh, house_kwh)
        and _same_kwh(raw_battery_kwh, battery_kwh)
        and _same_cost(_col(row, "grid_to_house_cost_ct", 7), house_cost_ct)
        and _same_cost(_col(row, "grid_to_battery_cost_ct", 8), battery_cost_ct)
    ):
        return False
    await db.execute(
        "UPDATE stats_hourly_billing SET\n             grid_to_house_kwh = ?,\n             grid_to_battery_kwh = ?,\n             grid_to_house_cost_ct = ?,\n             grid_to_battery_cost_ct = ?\n           WHERE hour_key = ?",
        (
            house_kwh,
            battery_kwh,
            house_cost_ct,
            battery_cost_ct,
            _col(row, "hour_key", 0),
        ),
    )
    return True


async def async_backfill_grid_split(
    hass: Any,
    *,
    config: dict[str, Any] | None = None,
    manager: Any | None = None,
    batch_size: int = BATCH_ROWS,
    pause_s: float = BATCH_PAUSE_S,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    hour_key_now: str | None = None,
    mode: str | None = None,
    fixed_total_ct: float | None = None,
) -> int:
    """Repair closed hours. Returns the number of rows actually updated."""
    config = config or {}
    from .price_mode import (
        effective_price_mode as effective_price_mode,
        gpm_fixed_total_ct as gpm_fixed_total_ct,
    )

    if mode is None:
        mode = effective_price_mode(hass, config)
    if fixed_total_ct is None and mode != MODE_NONE:
        fixed_total_ct = gpm_fixed_total_ct(hass)
    if manager is None:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
            get_manager as get_manager,
        )

        manager = get_manager()
        if manager is None:
            try:
                manager = await DatabaseConnectionManager.get_instance(hass)
            except Exception as error:
                _LOGGER.debug(
                    "Grid-split backfill skipped, no database manager: %s", error
                )
                return 0
    if hour_key_now is None:
        from ..utils.time_utils import (
            format_local_hour_key as format_local_hour_key,
            ha_now as ha_now,
            ha_today as ha_today,
        )

        now = ha_now(hass)
        hour_key_now = format_local_hour_key(now, hass)
        today_iso = ha_today(hass).isoformat()
    else:
        today_iso = str(hour_key_now)[:10]
    resolved_fixed_total_ct = fixed_total_ct
    pause = sleep or asyncio.sleep
    updated_rows = 0
    while True:
        async with manager.write_transaction() as connection:
            marker = await _meta_get(connection, META_GRID_SPLIT_BACKFILL)
            if marker == META_GRID_SPLIT_DONE:
                return updated_rows
            last_key = marker or ""
            daily_averages = await _load_averages(connection)
            current_average = await _current_average(connection)
            try:
                async with connection.execute(
                    _ROW_SQL, (last_key, hour_key_now, int(batch_size))
                ) as cursor:
                    rows = await cursor.fetchall()
            except Exception as error:
                _LOGGER.warning("Grid-split backfill read failed: %s", error)
                return updated_rows
            if not rows:
                await _meta_set(
                    connection, META_GRID_SPLIT_BACKFILL, META_GRID_SPLIT_DONE
                )
                _LOGGER.info(
                    "Grid-split backfill finished: %s rows updated", updated_rows
                )
                return updated_rows
            for row in rows:
                price_ct = _price_for_row(
                    row,
                    mode=mode,
                    config=config,
                    fixed_total_ct=resolved_fixed_total_ct,
                    daily_averages=daily_averages,
                    current_average=current_average,
                    today_str=today_iso,
                )
                if await _apply_row(connection, row, price_ct):
                    updated_rows += 1
            committed_key = str(_col(rows[-1], "hour_key", 0))
            await _meta_set(connection, META_GRID_SPLIT_BACKFILL, committed_key)
        await pause(pause_s)


_DAILY_TOLERANCE_KWH = split_tolerance_kwh(24.0)
_DAILY_SQL = "\nSELECT date, grid_to_house_kwh, grid_to_battery_kwh, smartmeter_import_kwh\nFROM stats_daily_energy\nWHERE date > ?\n  AND date < ?\n  AND smartmeter_import_kwh > 0\nORDER BY date\nLIMIT ?\n"


async def _apply_daily_row(db: Any, row: Any) -> bool:
    house_kwh, battery_kwh = reconcile_grid_split(
        _col(row, "smartmeter_import_kwh", 3),
        _col(row, "grid_to_house_kwh", 1),
        _col(row, "grid_to_battery_kwh", 2),
        tolerance=_DAILY_TOLERANCE_KWH,
    )
    if _same_kwh(_col(row, "grid_to_house_kwh", 1), house_kwh) and _same_kwh(
        _col(row, "grid_to_battery_kwh", 2), battery_kwh
    ):
        return False
    await db.execute(
        "UPDATE stats_daily_energy\n           SET grid_to_house_kwh = ?,\n               grid_to_battery_kwh = ?\n           WHERE date = ?",
        (house_kwh, battery_kwh, _col(row, "date", 0)),
    )
    return True


async def async_backfill_daily_grid_split(
    hass: Any,
    *,
    manager: Any | None = None,
    batch_size: int = BATCH_ROWS,
    pause_s: float = BATCH_PAUSE_S,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    today_str: str | None = None,
) -> int:
    """Repair closed days that stored an independent import meter.

    Returns the number of rows actually updated. Same batch and pause as
    the hourly backfill, with its own marker.
    """
    if manager is None:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
            get_manager as get_manager,
        )

        manager = get_manager()
        if manager is None:
            try:
                manager = await DatabaseConnectionManager.get_instance(hass)
            except Exception as error:
                _LOGGER.debug(
                    "Daily grid-split backfill skipped, no database manager: %s", error
                )
                return 0
    if today_str is None:
        from ..utils.time_utils import ha_today as ha_today

        today_str = ha_today(hass).isoformat()
    pause = sleep or asyncio.sleep
    updated_rows = 0
    while True:
        async with manager.write_transaction() as connection:
            marker = await _meta_get(connection, META_GRID_SPLIT_DAILY_BACKFILL)
            if marker == META_GRID_SPLIT_DONE:
                return updated_rows
            last_key = marker or ""
            try:
                async with connection.execute(
                    _DAILY_SQL, (last_key, today_str, int(batch_size))
                ) as cursor:
                    rows = await cursor.fetchall()
            except Exception as error:
                _LOGGER.warning("Daily grid-split backfill read failed: %s", error)
                return updated_rows
            if not rows:
                await _meta_set(
                    connection, META_GRID_SPLIT_DAILY_BACKFILL, META_GRID_SPLIT_DONE
                )
                _LOGGER.info(
                    "Daily grid-split backfill finished: %s rows updated", updated_rows
                )
                return updated_rows
            for row in rows:
                if await _apply_daily_row(connection, row):
                    updated_rows += 1
            committed_key = str(_col(rows[-1], "date", 0))
            await _meta_set(connection, META_GRID_SPLIT_DAILY_BACKFILL, committed_key)
        await pause(pause_s)
