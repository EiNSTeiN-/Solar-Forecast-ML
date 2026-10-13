# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .db_connector import GPMDatabaseConnector
_LOGGER = logging.getLogger(__name__)
HISTORY_RETENTION_DAYS = 730
MAX_HISTORY_ENTRIES = 18000


class HistoryManager:
    """Manages long-term price history storage in SQLite @zara"""

    def __init__(self, db: GPMDatabaseConnector) -> None:
        "Initialize the history manager @zara\n\nArgs:\n    db: GPM database connector instance\n"
        self._db = db
        self._loaded = False

    async def async_load(self) -> bool:
        """Check if history data exists in database @zara

        Returns:
            True if history is ready
        """
        try:
            count_row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_history WHERE timestamp LIKE '%+%' OR timestamp LIKE '%Z%'"
            )
            if count_row and count_row["cnt"] > 0:
                _LOGGER.info(
                    "Starting database migration for GPM_price_history to naive local timestamps..."
                )
                history_rows = await self._db.fetchall(
                    "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history"
                )
                deduplicated_rows = {}
                for history_row in history_rows:
                    stored_timestamp = history_row["timestamp"]
                    try:
                        parsed_timestamp = datetime.fromisoformat(
                            stored_timestamp.replace("Z", "+00:00")
                        )
                        if parsed_timestamp.tzinfo is not None:
                            local_timestamp = parsed_timestamp.astimezone()
                            naive_local_timestamp = local_timestamp.replace(tzinfo=None)
                        else:
                            naive_local_timestamp = parsed_timestamp
                        timestamp_text = naive_local_timestamp.isoformat()
                        price_net_ct_kwh = history_row["price_net"]
                        total_price_ct_kwh = history_row["total_price"]
                        hour = history_row["hour"]
                        if timestamp_text in deduplicated_rows:
                            existing_row = deduplicated_rows[timestamp_text]
                            if existing_row["total_price"] is None:
                                if total_price_ct_kwh is not None:
                                    deduplicated_rows[timestamp_text] = {
                                        "price_net": price_net_ct_kwh,
                                        "total_price": total_price_ct_kwh,
                                        "hour": hour,
                                    }
                                    continue
                                continue
                            continue
                        deduplicated_rows[timestamp_text] = {
                            "price_net": price_net_ct_kwh,
                            "total_price": total_price_ct_kwh,
                            "hour": hour,
                        }
                    except Exception as migration_error:
                        _LOGGER.warning(
                            "Migration skipping invalid timestamp %s: %s",
                            stored_timestamp,
                            migration_error,
                        )
                        continue
                await self._db.execute(
                    "DELETE FROM GPM_price_history", auto_commit=False
                )
                migration_rows = [
                    (
                        timestamp_text,
                        normalized_prices["price_net"],
                        normalized_prices["total_price"],
                        normalized_prices["hour"],
                    )
                    for timestamp_text, normalized_prices in deduplicated_rows.items()
                ]
                await self._db.executemany(
                    "INSERT INTO GPM_price_history (timestamp, price_net, total_price, hour)\n                       VALUES (?, ?, ?, ?)",
                    migration_rows,
                )
                await self._db.commit()
                _LOGGER.info(
                    "Successfully migrated %d price history entries to naive local timestamps",
                    len(migration_rows),
                )
            count_row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_history"
            )
            entry_count = count_row["cnt"] if count_row else 0
            if entry_count > 0:
                self._loaded = True
                _LOGGER.info("Loaded price history with %d entries", entry_count)
            await self._async_cleanup_old_entries()
            return True
        except Exception as error:
            _LOGGER.warning("Failed to load price history: %s", error)
            return True

    async def async_add_prices(self, prices: list[dict[str, Any]]) -> int:
        "Add new price entries to history @zara\n\nArgs:\n    prices: List of price entries to add\n\nReturns:\n    Number of new entries added\n"
        if not prices:
            return 0
        insert_rows = []
        for price_entry in prices:
            timestamp = price_entry.get("timestamp")
            if isinstance(timestamp, datetime):
                timestamp_text = timestamp.isoformat()
            else:
                timestamp_text = str(timestamp)
            insert_rows.append(
                (
                    timestamp_text,
                    price_entry.get("price", 0),
                    price_entry.get("total_price"),
                    price_entry.get("hour", 0),
                )
            )
        try:
            await self._db.executemany(
                "INSERT OR IGNORE INTO GPM_price_history\n                   (timestamp, price_net, total_price, hour) VALUES (?, ?, ?, ?)",
                insert_rows,
            )
            attempted_row_count = len(insert_rows)
            if attempted_row_count > 0:
                self._loaded = True
                _LOGGER.debug(
                    "Added up to %d price entries to history", attempted_row_count
                )
                await self._async_cleanup_old_entries()
            return attempted_row_count
        except Exception as error:
            _LOGGER.error("Failed to add prices to history: %s", error)
            return 0

    async def _async_cleanup_old_entries(self) -> int:
        "Remove entries older than retention period @zara\n\nReturns:\n    Number of entries removed\n"
        try:
            cutoff = datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)
            cutoff_timestamp = cutoff.replace(microsecond=0).isoformat()
            await self._db.execute(
                "DELETE FROM GPM_price_history WHERE timestamp < ?",
                (cutoff_timestamp,),
                auto_commit=False,
            )
            count_row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_history"
            )
            entry_count = count_row["cnt"] if count_row else 0
            if entry_count > MAX_HISTORY_ENTRIES:
                excess_entries = entry_count - MAX_HISTORY_ENTRIES
                await self._db.execute(
                    "DELETE FROM GPM_price_history WHERE id IN (\n                           SELECT id FROM GPM_price_history\n                           ORDER BY timestamp ASC LIMIT ?\n                       )",
                    (excess_entries,),
                )
                _LOGGER.info("Cleaned up %d old history entries", excess_entries)
            await self._db.commit()
        except Exception as error:
            _LOGGER.error("Failed to cleanup history: %s", error)
        return 0

    async def async_get_prices_for_date(self, date: datetime) -> list[dict[str, Any]]:
        "Get all prices for a specific date @zara\n\nArgs:\n    date: Date to get prices for\n\nReturns:\n    List of price entries for the date\n"
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_exclusive = day_start + timedelta(days=1)
        start_timestamp = day_start.replace(tzinfo=None).isoformat()
        end_timestamp = day_end_exclusive.replace(tzinfo=None).isoformat()
        try:
            rows = await self._db.fetchall(
                "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history\n                   WHERE timestamp >= ? AND timestamp < ?\n                   ORDER BY timestamp",
                (start_timestamp, end_timestamp),
            )
            return [
                {
                    "timestamp": row["timestamp"],
                    "price_net": row["price_net"],
                    "total_price": row["total_price"],
                    "hour": row["hour"],
                }
                for row in rows
            ]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get prices for date: %s", error)
            return []

    def get_prices_for_date(self, date: datetime) -> list[dict[str, Any]]:
        """Sync wrapper - returns empty, use async_get_prices_for_date() @zara"""
        return []

    async def async_get_prices_for_range(
        self, start_date: datetime, end_date: datetime
    ) -> list[dict[str, Any]]:
        "Get all prices for a date range @zara\n\nArgs:\n    start_date: Start of range\n    end_date: End of range\n\nReturns:\n    List of price entries in range\n"
        start_timestamp = start_date.replace(tzinfo=None).isoformat()
        end_timestamp = end_date.replace(tzinfo=None).isoformat()
        try:
            rows = await self._db.fetchall(
                "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history\n                   WHERE timestamp >= ? AND timestamp <= ?\n                   ORDER BY timestamp",
                (start_timestamp, end_timestamp),
            )
            return [
                {
                    "timestamp": row["timestamp"],
                    "price_net": row["price_net"],
                    "total_price": row["total_price"],
                    "hour": row["hour"],
                }
                for row in rows
            ]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get prices for range: %s", error)
            return []

    def get_prices_for_range(
        self, start_date: datetime, end_date: datetime
    ) -> list[dict[str, Any]]:
        """Sync wrapper - returns empty, use async_get_prices_for_range() @zara"""
        return []

    async def async_get_average_price_for_date(self, date: datetime) -> float | None:
        "Calculate average price for a specific date @zara\n\nArgs:\n    date: Date to calculate average for\n\nReturns:\n    Average price or None if no data\n"
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_exclusive = day_start + timedelta(days=1)
        day_start = day_start.astimezone()
        day_end_exclusive = day_end_exclusive.astimezone()
        try:
            average_row = await self._db.fetchone(
                "SELECT AVG(price_net) as avg_price FROM GPM_price_history\n                   WHERE timestamp >= ? AND timestamp < ?",
                (day_start.isoformat(), day_end_exclusive.isoformat()),
            )
            if average_row and average_row["avg_price"] is not None:
                return round(average_row["avg_price"], 4)
        except Exception as error:
            _LOGGER.error("Failed to get average price: %s", error)
        return None if True else None

    def get_average_price_for_date(self, date: datetime) -> float | None:
        """Sync wrapper - returns None, use async version @zara"""

    async def async_get_min_max_for_date(
        self, date: datetime
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        "Get minimum and maximum price entries for a date @zara\n\nArgs:\n    date: Date to check\n\nReturns:\n    Tuple of (min_entry, max_entry) or (None, None)\n"
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_exclusive = day_start + timedelta(days=1)
        day_start = day_start.astimezone()
        day_end_exclusive = day_end_exclusive.astimezone()
        try:
            minimum_row = await self._db.fetchone(
                "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history\n                   WHERE timestamp >= ? AND timestamp < ?\n                   ORDER BY price_net ASC LIMIT 1",
                (day_start.isoformat(), day_end_exclusive.isoformat()),
            )
            maximum_row = await self._db.fetchone(
                "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history\n                   WHERE timestamp >= ? AND timestamp < ?\n                   ORDER BY price_net DESC LIMIT 1",
                (day_start.isoformat(), day_end_exclusive.isoformat()),
            )
            minimum_entry = None
            maximum_entry = None
            if minimum_row:
                minimum_entry = {
                    "timestamp": minimum_row["timestamp"],
                    "price_net": minimum_row["price_net"],
                    "total_price": minimum_row["total_price"],
                    "hour": minimum_row["hour"],
                }
            if maximum_row:
                maximum_entry = {
                    "timestamp": maximum_row["timestamp"],
                    "price_net": maximum_row["price_net"],
                    "total_price": maximum_row["total_price"],
                    "hour": maximum_row["hour"],
                }
            return (minimum_entry, maximum_entry)
        except Exception as error:
            _LOGGER.error("Failed to get min/max prices: %s", error)
            return (None, None)

    def get_min_max_for_date(
        self, date: datetime
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Sync wrapper - returns (None, None) @zara"""
        return (None, None)

    async def async_get_history_stats(self) -> dict[str, Any]:
        "Get statistics about the stored history @zara\n\nReturns:\n    Dictionary with history statistics\n"
        try:
            statistics_row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt,\n                          MIN(timestamp) as oldest,\n                          MAX(timestamp) as newest\n                   FROM GPM_price_history"
            )
            if not statistics_row:
                return {"loaded": False, "entry_count": 0}
            return {
                "loaded": self._loaded,
                "entry_count": statistics_row["cnt"],
                "oldest_entry": statistics_row["oldest"],
                "newest_entry": statistics_row["newest"],
            }
        except Exception as error:
            _LOGGER.error("Failed to get history stats: %s", error)
            return {"loaded": False, "entry_count": 0}

    def get_history_stats(self) -> dict[str, Any]:
        """Sync wrapper - returns basic info @zara"""
        return {"loaded": self._loaded, "entry_count": 0}
