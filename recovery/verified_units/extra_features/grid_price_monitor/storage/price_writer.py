# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Apply tariff slots to cache, history, and current_price."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from ..const import TARIFF_MODE_DEMO


def slot_timestamp_iso(value: Any) -> str:
    if isinstance(value, datetime):
        hour_timestamp = value.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        return hour_timestamp.strftime("%Y-%m-%dT%H:00:00")
    timestamp_text = str(value)
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp_text)
        return parsed_timestamp.replace(
            tzinfo=None, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:00:00")
    except ValueError:
        return timestamp_text[:19]


def _round_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value), 4)


@dataclass(frozen=True, slots=True)
class WriteResult:
    changed_rows: int
    changed_from: str | None
    changed_to: str | None
    revision: int
    signal_reason: str | None


class PriceWriter:

    def __init__(
        self,
        db,
        cache,
        *,
        lock: asyncio.Lock,
        dispatcher: Callable[..., None] | None = None,
        signal: str | None = None,
    ) -> None:
        self._db = db
        self._cache = cache
        self._lock = lock
        self._dispatcher = dispatcher
        self._signal = signal

    async def current_revision(self) -> int:
        revision_row = await self._db.fetchone(
            "SELECT price_revision FROM GPM_price_cache_meta WHERE id = 1"
        )
        if not revision_row or revision_row["price_revision"] is None:
            return 0
        return int(revision_row["price_revision"])

    async def _set_revision(self, revision: int) -> None:
        await self._db.execute(
            "INSERT INTO GPM_price_cache_meta (id, price_revision)\n               VALUES (1, ?)\n               ON CONFLICT(id) DO UPDATE SET price_revision = excluded.price_revision",
            (revision,),
            auto_commit=False,
        )

    async def write_slots(
        self,
        slots: list[dict[str, Any]],
        *,
        mode: str,
        country: str,
        is_demo: bool,
        now_local: datetime,
        feed_in_tariff_ct: float | None = None,
        base_fee_eur_month: float | None = None,
        reason: str = "config",
        persist_history: bool = True,
        current_price: dict[str, Any] | None = None,
    ) -> WriteResult:
        async with self._lock:
            return await self._write_locked(
                slots,
                mode=mode,
                country=country,
                is_demo=is_demo,
                now_local=now_local,
                feed_in_tariff_ct=feed_in_tariff_ct,
                base_fee_eur_month=base_fee_eur_month,
                reason=reason,
                persist_history=persist_history,
                current_price=current_price,
            )
        return None if True else None

    async def _write_locked(
        self,
        slots: list[dict[str, Any]],
        *,
        mode: str,
        country: str,
        is_demo: bool,
        now_local: datetime,
        feed_in_tariff_ct: float | None,
        base_fee_eur_month: float | None,
        reason: str,
        persist_history: bool,
        current_price: dict[str, Any] | None,
    ) -> WriteResult:
        await self._db.require_price_write_schema()
        revision = await self.current_revision()
        changed_rows = 0
        changed_from = None
        changed_to = None
        signal_reason = None
        if persist_history:
            await self._cache.async_save(slots, country, tariff_mode=mode)
            if not is_demo:
                changed_rows, changed_from, changed_to = await self._write_history(
                    slots, now_local
                )
                if changed_rows > 0:
                    revision += 1
                    await self._set_revision(revision)
                    signal_reason = reason
                    await self._db.commit()
                    if self._dispatcher and self._signal:
                        self._dispatcher(
                            {
                                "revision": revision,
                                "from": changed_from,
                                "to": changed_to,
                                "reason": reason,
                            }
                        )
                else:
                    await self._db.commit()
        await self._write_current_price(
            slots,
            mode=TARIFF_MODE_DEMO if is_demo else mode,
            country=country,
            is_demo=is_demo,
            now_local=now_local,
            revision=0 if is_demo else revision,
            feed_in_tariff_ct=feed_in_tariff_ct,
            base_fee_eur_month=base_fee_eur_month,
            current_price=current_price,
        )
        return (
            WriteResult(
                changed_rows=changed_rows,
                changed_from=changed_from,
                changed_to=changed_to,
                revision=0 if is_demo else revision,
                signal_reason=signal_reason,
            )
            if True
            else None
        )

    async def backfill_missing(
        self, slots: list[dict[str, Any]], *, now_local: datetime
    ) -> WriteResult:
        async with self._lock:
            await self._db.require_price_write_schema()
            revision = await self.current_revision()
            inserted_count, changed_from, changed_to = await self._insert_ignore(slots)
            signal_reason = None
            if inserted_count > 0:
                revision += 1
                await self._set_revision(revision)
                signal_reason = "backfill"
                if self._dispatcher and self._signal:
                    self._dispatcher(
                        {
                            "revision": revision,
                            "from": changed_from,
                            "to": changed_to,
                            "reason": "backfill",
                        }
                    )
            await self._db.commit()
            return WriteResult(
                changed_rows=inserted_count,
                changed_from=changed_from,
                changed_to=changed_to,
                revision=revision,
                signal_reason=signal_reason,
            )
        return None if True else None

    async def _write_history(
        self, slots: list[dict[str, Any]], now_local: datetime
    ) -> tuple[int, str | None, str | None]:
        current_slot_timestamp = slot_timestamp_iso(now_local)
        slot_timestamps = [slot_timestamp_iso(slot.get("timestamp")) for slot in slots]
        existing_rows = []
        if slot_timestamps:
            existing_rows = await self._db.fetchall(
                f"SELECT timestamp, total_price, correction_id\n                    FROM GPM_price_history\n                    WHERE timestamp IN ({','.join(('?' for unused_timestamp in slot_timestamps))})",
                tuple(slot_timestamps),
            )
        existing_prices = {
            row["timestamp"]: (_round_price(row["total_price"]), row["correction_id"])
            for row in existing_rows or []
        }
        changed_timestamps = []
        rows_by_date = {}
        for slot in slots:
            timestamp = slot_timestamp_iso(slot.get("timestamp"))
            hour = int(slot.get("hour", timestamp[11:13]))
            price_net_ct_kwh = float(slot.get("price", slot.get("total_price") or 0))
            total_price_ct_kwh = float(slot.get("total_price", price_net_ct_kwh))
            price_source = slot.get("price_source")
            slot_date = timestamp[:10]
            rows_by_date.setdefault(slot_date, []).append(
                (
                    timestamp,
                    price_net_ct_kwh,
                    total_price_ct_kwh,
                    hour,
                    price_source,
                    current_slot_timestamp,
                )
            )
            previous_price = existing_prices.get(timestamp)
            if previous_price is None:
                if timestamp < current_slot_timestamp:
                    changed_timestamps.append(timestamp)
                continue
            previous_total_price_ct_kwh, correction_id = previous_price
            if correction_id is not None:
                continue
            if timestamp < current_slot_timestamp:
                continue
            if _round_price(total_price_ct_kwh) != previous_total_price_ct_kwh:
                changed_timestamps.append(timestamp)
                continue
        for slot_date, date_rows in sorted(rows_by_date.items()):
            await self._db.executemany(
                "INSERT INTO GPM_price_history\n                   (timestamp, price_net, total_price, hour, price_source)\n                   VALUES (?, ?, ?, ?, ?)\n                   ON CONFLICT(timestamp) DO UPDATE SET\n                     price_net = excluded.price_net,\n                     total_price = excluded.total_price,\n                     hour = excluded.hour,\n                     price_source = excluded.price_source\n                   WHERE GPM_price_history.correction_id IS NULL\n                     AND GPM_price_history.timestamp >= ?",
                [
                    (
                        timestamp,
                        price_net_ct_kwh,
                        total_price_ct_kwh,
                        hour,
                        price_source,
                        current_slot_timestamp,
                    )
                    for timestamp, price_net_ct_kwh, total_price_ct_kwh, hour, price_source, current_slot_timestamp in date_rows
                ],
            )
        if not changed_timestamps:
            return (0, None, None)
        changed_timestamps.sort()
        return (
            len(changed_timestamps),
            changed_timestamps[0][:10],
            changed_timestamps[-1][:10],
        )

    async def _insert_ignore(
        self, slots: list[dict[str, Any]]
    ) -> tuple[int, str | None, str | None]:
        if not slots:
            return (0, None, None)
        slot_timestamps = [slot_timestamp_iso(slot.get("timestamp")) for slot in slots]
        existing_timestamps = {
            row["timestamp"]
            for row in await self._db.fetchall(
                f"SELECT timestamp FROM GPM_price_history WHERE timestamp IN ({','.join(('?' for unused_timestamp in slot_timestamps))})",
                tuple(slot_timestamps),
            )
            or []
        }
        insert_rows = []
        inserted_timestamps = []
        for slot, timestamp in zip(slots, slot_timestamps, strict=True):
            if timestamp in existing_timestamps:
                continue
            hour = int(slot.get("hour", timestamp[11:13]))
            price_net_ct_kwh = float(slot.get("price", slot.get("total_price") or 0))
            total_price_ct_kwh = float(slot.get("total_price", price_net_ct_kwh))
            insert_rows.append(
                (
                    timestamp,
                    price_net_ct_kwh,
                    total_price_ct_kwh,
                    hour,
                    slot.get("price_source"),
                )
            )
            inserted_timestamps.append(timestamp)
        if not insert_rows:
            return (0, None, None)
        await self._db.executemany(
            "INSERT OR IGNORE INTO GPM_price_history\n               (timestamp, price_net, total_price, hour, price_source)\n               VALUES (?, ?, ?, ?, ?)",
            insert_rows,
        )
        inserted_timestamps.sort()
        return (
            len(insert_rows),
            inserted_timestamps[0][:10],
            inserted_timestamps[-1][:10],
        )

    async def _write_current_price(
        self,
        slots: list[dict[str, Any]],
        *,
        mode: str,
        country: str,
        is_demo: bool,
        now_local: datetime,
        revision: int,
        feed_in_tariff_ct: float | None,
        base_fee_eur_month: float | None,
        current_price: dict[str, Any] | None,
    ) -> None:
        price_context = current_price or {}
        current_slot_timestamp = slot_timestamp_iso(now_local)
        spot_price_net_ct_kwh = price_context.get("spot_price_net")
        spot_price_gross_ct_kwh = price_context.get("spot_price")
        total_price_ct_kwh = price_context.get("total_price")
        next_hour_total_price_ct_kwh = price_context.get("total_price_next_hour")
        is_cheap = price_context.get("is_cheap")
        if is_demo:
            spot_price_net_ct_kwh = None
            spot_price_gross_ct_kwh = None
            is_cheap = False
        await self._db.execute(
            "INSERT INTO GPM_current_price\n               (id, timestamp, spot_price_net, spot_price_gross, total_price,\n                price_next_hour, is_cheap, average_today,\n                cheapest_today, most_expensive_today, country, last_updated,\n                tariff_mode, price_revision, feed_in_tariff_ct, base_fee_eur_month, is_demo)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   timestamp = excluded.timestamp,\n                   spot_price_net = excluded.spot_price_net,\n                   spot_price_gross = excluded.spot_price_gross,\n                   total_price = excluded.total_price,\n                   price_next_hour = excluded.price_next_hour,\n                   is_cheap = excluded.is_cheap,\n                   average_today = excluded.average_today,\n                   cheapest_today = excluded.cheapest_today,\n                   most_expensive_today = excluded.most_expensive_today,\n                   country = excluded.country,\n                   last_updated = excluded.last_updated,\n                   tariff_mode = excluded.tariff_mode,\n                   price_revision = excluded.price_revision,\n                   feed_in_tariff_ct = excluded.feed_in_tariff_ct,\n                   base_fee_eur_month = excluded.base_fee_eur_month,\n                   is_demo = excluded.is_demo",
            (
                current_slot_timestamp,
                None if is_demo else spot_price_net_ct_kwh,
                None if is_demo else spot_price_gross_ct_kwh,
                total_price_ct_kwh,
                next_hour_total_price_ct_kwh,
                0 if is_demo else 1 if is_cheap else 0,
                price_context.get("average_today"),
                None if is_demo else price_context.get("cheapest_today"),
                None if is_demo else price_context.get("most_expensive_today"),
                country,
                now_local.isoformat(),
                mode,
                0 if is_demo else revision,
                feed_in_tariff_ct,
                base_fee_eur_month,
                1 if is_demo else 0,
            ),
        )
