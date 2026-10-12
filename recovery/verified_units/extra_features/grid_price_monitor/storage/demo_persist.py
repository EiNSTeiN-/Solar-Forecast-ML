# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Persist demo prices to cache and current_price only."""

from __future__ import annotations
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .db_connector import GPMDatabaseConnector
    from .price_cache import PriceCache


async def async_persist_demo_prices(
    db: "GPMDatabaseConnector",
    cache: "PriceCache",
    slots: list[dict[str, Any]],
    *,
    country: str,
    now_local: datetime,
    feed_in_tariff_ct: float | None,
    base_fee_eur_month: float | None,
) -> None:
    """Replace cache and current_price. Never touches history or statistics."""
    await db.require_price_write_schema()
    await cache.async_save(slots, country, tariff_mode="demo")
    current_slot = next(
        (
            slot
            for slot in slots
            if slot.get("hour") == now_local.hour
            and slot.get("date") == now_local.date().isoformat()
        ),
        slots[0] if slots else None,
    )
    next_slot = None
    if current_slot is not None:
        current_index = slots.index(current_slot)
        if current_index + 1 < len(slots):
            next_slot = slots[current_index + 1]
    today_prices = [
        float(slot["total_price"])
        for slot in slots
        if slot.get("date") == now_local.date().isoformat()
    ]
    average_today = (
        round(sum(today_prices) / len(today_prices), 2) if today_prices else None
    )
    hour_key = now_local.replace(minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:00:00"
    )
    await db.execute(
        """INSERT INTO GPM_current_price
           (id, timestamp, spot_price_net, spot_price_gross, total_price,
            price_next_hour, is_cheap, average_today,
            cheapest_today, most_expensive_today, country, last_updated,
            tariff_mode, price_revision, feed_in_tariff_ct, base_fee_eur_month, is_demo)
           VALUES (1, ?, NULL, NULL, ?, ?, 0, ?, NULL, NULL, ?, ?, 'demo', 0, ?, ?, 1)
           ON CONFLICT(id) DO UPDATE SET
               timestamp = excluded.timestamp,
               spot_price_net = NULL,
               spot_price_gross = NULL,
               total_price = excluded.total_price,
               price_next_hour = excluded.price_next_hour,
               is_cheap = 0,
               average_today = excluded.average_today,
               cheapest_today = NULL,
               most_expensive_today = NULL,
               country = excluded.country,
               last_updated = excluded.last_updated,
               tariff_mode = 'demo',
               price_revision = excluded.price_revision,
               feed_in_tariff_ct = excluded.feed_in_tariff_ct,
               base_fee_eur_month = excluded.base_fee_eur_month,
               is_demo = 1""",
        (
            hour_key,
            None if current_slot is None else current_slot.get("total_price"),
            None if next_slot is None else next_slot.get("total_price"),
            average_today,
            country,
            now_local.isoformat(),
            feed_in_tariff_ct,
            base_fee_eur_month,
        ),
    )
