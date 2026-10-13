# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Re-price STATS hourly billing from GPM history. Never writes GPM tables."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any
from ..utils.time_utils import format_local_hour_key, ha_now, ha_today
from .grid_split import energy_cost_ct, reconcile_grid_split
from .price_mode import (
    MODE_NONE,
    _present_price,
    effective_price_mode,
    gpm_fixed_total_ct,
    resolve_hour_price,
)

_LOGGER = logging.getLogger(__name__)
META_REVISION_SEEN = "gpm_price_revision_seen"
CATCH_UP_DAYS_UNKNOWN = 400


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
    return row["value"] if isinstance(row, dict) else row[0]


async def _meta_set(db: Any, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO stats_schema_meta (key, value, updated_at)\n           VALUES (?, ?, CURRENT_TIMESTAMP)\n           ON CONFLICT(key) DO UPDATE SET\n             value = excluded.value,\n             updated_at = excluded.updated_at",
        (key, value),
    )


async def gpm_current_revision(db: Any) -> int:
    try:
        async with db.execute(
            "SELECT price_revision FROM GPM_price_cache_meta WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
    except Exception:
        return 0
    if row is None:
        return 0
    revision_value = row["price_revision"] if isinstance(row, dict) else row[0]
    try:
        return int(revision_value or 0)
    except (TypeError, ValueError):
        return 0


def _col(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[name]
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _same_cost(stored: Any, expected: float | None) -> bool:
    if expected is None:
        return stored is None
    if stored is None:
        return False
    try:
        return abs(float(stored) - expected) < 0.001
    except (TypeError, ValueError):
        return False


def _same_kwh(stored: Any, expected: float) -> bool:
    try:
        return abs(float(stored) - expected) <= 5e-05
    except (TypeError, ValueError):
        return False


async def reprice_range(
    db: Any,
    *,
    start_date: str,
    end_date: str,
    current_hour_key: str,
    mode: str,
    config: dict[str, Any],
    fixed_total_ct: float | None = None,
) -> int:
    """Update hourly costs from GPM history. Returns changed row count."""
    if mode == MODE_NONE:
        return 0
    changed_rows = 0
    daily_averages = {}
    try:
        async with db.execute(
            "SELECT date, average_total FROM GPM_daily_averages\n               WHERE date >= ? AND date <= ?",
            (start_date, end_date),
        ) as cursor:
            async for row in cursor:
                date_value = row["date"] if isinstance(row, dict) else row[0]
                average_value = (
                    row["average_total"] if isinstance(row, dict) else row[1]
                )
                average_price_ct_kwh = _present_price(average_value)
                if average_price_ct_kwh is not None and date_value:
                    daily_averages[str(date_value)] = average_price_ct_kwh
    except Exception:
        daily_averages = {}
    current_average_ct_kwh = None
    try:
        async with db.execute(
            "SELECT average_today FROM GPM_current_price WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                current_average_value = (
                    row["average_today"] if isinstance(row, dict) else row[0]
                )
                current_average_ct_kwh = _present_price(current_average_value)
    except Exception:
        current_average_ct_kwh = None
    current_date = end_date
    async with db.execute(
        "SELECT hb.hour_key, hb.date, hb.hour, hb.price_ct_kwh,\n                  hb.grid_import_kwh, hb.consumer_heatpump_kwh,\n                  hb.consumer_heatingrod_kwh, hb.consumer_wallbox_kwh,\n                  gpm.total_price AS gpm_total,\n                  hb.grid_to_house_kwh, hb.grid_to_battery_kwh,\n                  hb.grid_to_house_cost_ct, hb.grid_to_battery_cost_ct\n           FROM stats_hourly_billing hb\n           LEFT JOIN GPM_price_history gpm\n             ON gpm.hour = hb.hour\n            AND gpm.timestamp >= hb.date || 'T00:00:00'\n            AND gpm.timestamp < date(hb.date, '+1 day') || 'T00:00:00'\n           WHERE hb.date >= ? AND hb.date <= ?\n             AND hb.hour_key != ?\n           ORDER BY hb.date, hb.hour",
        (start_date, end_date, current_hour_key),
    ) as cursor:
        billing_rows = await cursor.fetchall()
    for row in billing_rows:
        hour_key = _col(row, "hour_key", 0)
        billing_date = _col(row, "date", 1)
        stored_price_ct_kwh = _col(row, "price_ct_kwh", 3)
        gpm_total_ct_kwh = _col(row, "gpm_total", 8)
        grid_house_kwh, grid_battery_kwh = reconcile_grid_split(
            _col(row, "grid_import_kwh", 4),
            _col(row, "grid_to_house_kwh", 9),
            _col(row, "grid_to_battery_kwh", 10),
        )
        resolved_price_ct_kwh = (
            None
            if mode == MODE_NONE
            else resolve_hour_price(
                gpm_total=gpm_total_ct_kwh,
                stored_price=stored_price_ct_kwh,
                daily_average=daily_averages.get(str(billing_date)),
                current_average=(
                    current_average_ct_kwh
                    if str(billing_date) == current_date
                    else None
                ),
                mode=mode,
                config=config,
                fixed_total_ct=fixed_total_ct,
            )
        )
        grid_house_cost_ct = energy_cost_ct(grid_house_kwh, resolved_price_ct_kwh)
        grid_battery_cost_ct = energy_cost_ct(grid_battery_kwh, resolved_price_ct_kwh)
        grid_import_cost_ct = energy_cost_ct(
            _col(row, "grid_import_kwh", 4), resolved_price_ct_kwh
        )
        split_unchanged = (
            _same_kwh(_col(row, "grid_to_house_kwh", 9), grid_house_kwh)
            and _same_kwh(_col(row, "grid_to_battery_kwh", 10), grid_battery_kwh)
            and _same_cost(_col(row, "grid_to_house_cost_ct", 11), grid_house_cost_ct)
            and _same_cost(
                _col(row, "grid_to_battery_cost_ct", 12), grid_battery_cost_ct
            )
        )
        if resolved_price_ct_kwh is None:
            if split_unchanged:
                continue
            await db.execute(
                "UPDATE stats_hourly_billing SET\n                     grid_to_house_kwh = ?,\n                     grid_to_battery_kwh = ?,\n                     grid_to_house_cost_ct = NULL,\n                     grid_to_battery_cost_ct = NULL\n                   WHERE hour_key = ?",
                (grid_house_kwh, grid_battery_kwh, hour_key),
            )
            changed_rows += 1
            continue
        try:
            stored_price_number = (
                float(stored_price_ct_kwh)
                if stored_price_ct_kwh not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            stored_price_number = None
        price_unchanged = (
            stored_price_number is not None
            and abs(stored_price_number - resolved_price_ct_kwh) < 0.0001
        )
        if price_unchanged and split_unchanged:
            continue
        await db.execute(
            "UPDATE stats_hourly_billing SET\n                 price_ct_kwh = ?,\n                 grid_import_cost_ct = ?,\n                 consumer_heatpump_cost_ct = ROUND(consumer_heatpump_kwh * ?, 2),\n                 consumer_heatingrod_cost_ct = ROUND(consumer_heatingrod_kwh * ?, 2),\n                 consumer_wallbox_cost_ct = ROUND(consumer_wallbox_kwh * ?, 2),\n                 grid_to_house_kwh = ?,\n                 grid_to_battery_kwh = ?,\n                 grid_to_house_cost_ct = ?,\n                 grid_to_battery_cost_ct = ?\n               WHERE hour_key = ?",
            (
                resolved_price_ct_kwh,
                grid_import_cost_ct,
                resolved_price_ct_kwh,
                resolved_price_ct_kwh,
                resolved_price_ct_kwh,
                grid_house_kwh,
                grid_battery_kwh,
                grid_house_cost_ct,
                grid_battery_cost_ct,
                hour_key,
            ),
        )
        changed_rows += 1
        continue
    if changed_rows:
        await db.execute(
            "UPDATE stats_daily_energy\n               SET price_ct_kwh = (\n                   SELECT CASE\n                     WHEN SUM(CASE WHEN price_ct_kwh IS NOT NULL AND price_ct_kwh != 0\n                                   THEN grid_import_kwh ELSE 0 END) > 0\n                     THEN SUM(CASE WHEN price_ct_kwh IS NOT NULL AND price_ct_kwh != 0\n                                   THEN grid_import_kwh * price_ct_kwh ELSE 0 END)\n                          / SUM(CASE WHEN price_ct_kwh IS NOT NULL AND price_ct_kwh != 0\n                                     THEN grid_import_kwh ELSE 0 END)\n                     ELSE AVG(CASE WHEN price_ct_kwh IS NOT NULL AND price_ct_kwh != 0\n                                   THEN price_ct_kwh END)\n                   END\n                   FROM stats_hourly_billing\n                   WHERE date = stats_daily_energy.date\n               )\n               WHERE date >= ? AND date <= ?",
            (start_date, end_date),
        )
    return changed_rows


async def async_reprice_from_revision(
    hass: Any,
    *,
    config: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Catch-up or signal-driven reprice. Writes only STATS tables."""
    config = config or {}
    price_mode = effective_price_mode(hass, config)
    if price_mode == MODE_NONE:
        return 0
    from ..storage.db_connection_manager import (
        DatabaseConnectionManager as database_manager_type,
        get_manager as get_manager,
    )

    manager = get_manager()
    if manager is None:
        try:
            manager = await database_manager_type.get_instance(hass)
        except Exception as error:
            _LOGGER.debug("Reprice skipped, no database manager: %s", error)
            return 0
    now = ha_now(hass)
    current_hour_key = format_local_hour_key(now)
    today = ha_today(hass)
    changed_rows = 0
    current_revision = 0
    async with manager.write_transaction() as db:
        seen_revision_text = await _meta_get(db, META_REVISION_SEEN)
        current_revision = await gpm_current_revision(db)
        if seen_revision_text is None:
            start_date = today - timedelta(days=CATCH_UP_DAYS_UNKNOWN)
        else:
            try:
                seen_revision = int(seen_revision_text)
            except (TypeError, ValueError):
                seen_revision = -1
            if seen_revision >= current_revision:
                return 0
            changed_from = (payload or {}).get("from")
            if changed_from:
                try:
                    start_date = datetime.fromisoformat(str(changed_from)[:10]).date()
                except (TypeError, ValueError):
                    start_date = today - timedelta(days=CATCH_UP_DAYS_UNKNOWN)
            else:
                start_date = today - timedelta(days=CATCH_UP_DAYS_UNKNOWN)
        changed_rows = await reprice_range(
            db,
            start_date=start_date.isoformat(),
            end_date=today.isoformat(),
            current_hour_key=current_hour_key,
            mode=price_mode,
            config=config,
            fixed_total_ct=gpm_fixed_total_ct(hass),
        )
        await _meta_set(db, META_REVISION_SEEN, str(current_revision))
    _LOGGER.info(
        "STATS reprice finished: %s rows, revision %s", changed_rows, current_revision
    )
    return changed_rows
