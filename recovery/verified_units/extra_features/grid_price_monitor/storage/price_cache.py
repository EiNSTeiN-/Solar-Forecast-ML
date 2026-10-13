# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .db_connector import GPMDatabaseConnector
_LOGGER = logging.getLogger(__name__)
CACHE_VALIDITY_HOURS = 48


class PriceCache:
    """Manages cached electricity price data in SQLite @zara"""

    def __init__(self, db: GPMDatabaseConnector) -> None:
        "Initialize the price cache @zara\n\nArgs:\n    db: GPM database connector instance\n"
        self._db = db
        self._loaded = False

    async def async_load(self) -> bool:
        "Load cache metadata from database @zara\n\nReturns:\n    True if cache was loaded successfully\n"
        try:
            await self._db.execute(
                "DELETE FROM GPM_price_cache WHERE timestamp LIKE '%+%' OR timestamp LIKE '%Z%'"
            )
            metadata_row = await self._db.fetchone(
                "SELECT last_fetch, valid_until, country FROM GPM_price_cache_meta WHERE id = 1"
            )
            if metadata_row:
                self._loaded = True
                count_row = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM GPM_price_cache"
                )
                entry_count = count_row["cnt"] if count_row else 0
                _LOGGER.debug("Loaded price cache with %d entries", entry_count)
                return True
        except Exception as cache_error:
            _LOGGER.warning("Failed to load price cache: %s", cache_error)
        return False

    async def async_save(
        self,
        prices: list[dict[str, Any]],
        country: str,
        *,
        tariff_mode: str | None = None,
    ) -> bool:
        "Save prices to cache in database @zara\n\nArgs:\n    prices: List of price entries\n    country: Country code (DE/AT)\n\nReturns:\n    True if saved successfully\n"
        try:
            await self._db.require_price_write_schema()
            fetched_at = datetime.now().astimezone()
            valid_until = fetched_at + timedelta(hours=CACHE_VALIDITY_HOURS)
            await self._db.execute(
                "INSERT INTO GPM_price_cache_meta\n                   (id, last_fetch, valid_until, country, tariff_mode)\n                   VALUES (1, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       last_fetch = excluded.last_fetch,\n                       valid_until = excluded.valid_until,\n                       country = excluded.country,\n                       tariff_mode = COALESCE(excluded.tariff_mode, GPM_price_cache_meta.tariff_mode)",
                (fetched_at.isoformat(), valid_until.isoformat(), country, tariff_mode),
                auto_commit=False,
            )
            await self._db.execute("DELETE FROM GPM_price_cache", auto_commit=False)
            cache_rows = []
            for price_entry in prices:
                timestamp = price_entry.get("timestamp")
                if isinstance(timestamp, datetime):
                    if timestamp.tzinfo is not None:
                        timestamp = timestamp.astimezone().replace(tzinfo=None)
                    timestamp = timestamp.isoformat()
                elif isinstance(timestamp, str):
                    if "+" in timestamp or "Z" in timestamp:
                        try:
                            parsed_timestamp = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                            timestamp = (
                                parsed_timestamp.astimezone()
                                .replace(tzinfo=None)
                                .isoformat()
                            )
                        except Exception:
                            pass
                cache_rows.append(
                    (
                        str(timestamp),
                        price_entry.get("price", 0),
                        price_entry.get("total_price"),
                        price_entry.get("hour", 0),
                        price_entry.get("price_source"),
                    )
                )
                continue
            await self._db.executemany(
                "INSERT OR REPLACE INTO GPM_price_cache\n                   (timestamp, price, total_price, hour, price_source)\n                   VALUES (?, ?, ?, ?, ?)",
                cache_rows,
            )
            _LOGGER.debug(
                "Saved %d price entries to cache, valid until %s",
                len(prices),
                valid_until.isoformat(),
            )
        except RuntimeError:
            raise
        except Exception as cache_error:
            _LOGGER.error("Failed to save price cache: %s", cache_error)
            return False
        return True

    def is_valid(self) -> bool:
        "Check if cache is valid and not expired @zara\n\nNote: Uses in-memory check based on last loaded metadata.\nFor async validation, call async_is_valid().\n\nReturns:\n    True if cache was loaded (actual validity checked async)\n"
        return self._loaded

    async def async_is_valid(self) -> bool:
        "Check if cache is valid and not expired (async) @zara\n\nReturns:\n    True if cache is valid\n"
        try:
            metadata_row = await self._db.fetchone(
                "SELECT valid_until FROM GPM_price_cache_meta WHERE id = 1"
            )
            if not metadata_row or not metadata_row["valid_until"]:
                return False
            valid_until_datetime = datetime.fromisoformat(metadata_row["valid_until"])
            now = datetime.now().astimezone()
            if valid_until_datetime.tzinfo is None:
                valid_until_datetime = valid_until_datetime.astimezone()
            return now < valid_until_datetime
        except (ValueError, TypeError, Exception):
            return False

    def get_prices(self) -> list[dict[str, Any]]:
        """Sync wrapper - returns empty, use async_get_prices() @zara"""
        return []

    async def async_get_prices(self) -> list[dict[str, Any]]:
        "Get cached prices from database @zara\n\nReturns:\n    List of price entries or empty list\n"
        try:
            price_rows = await self._db.fetchall(
                "SELECT timestamp, price, total_price, hour FROM GPM_price_cache ORDER BY timestamp"
            )
            prices = []
            for price_row in price_rows:
                price_entry = {
                    "timestamp": datetime.fromisoformat(price_row["timestamp"]),
                    "price": price_row["price"],
                    "hour": price_row["hour"],
                }
                if price_row["total_price"] is not None:
                    price_entry["total_price"] = price_row["total_price"]
                prices.append(price_entry)
            return prices
        except Exception as cache_error:
            _LOGGER.warning("Failed to get cached prices: %s", cache_error)
            return []

    async def async_get_country(self) -> str | None:
        "Get the country code for cached prices @zara\n\nReturns:\n    Country code or None\n"
        try:
            country_row = await self._db.fetchone(
                "SELECT country FROM GPM_price_cache_meta WHERE id = 1"
            )
            return country_row["country"] if country_row else None
        except Exception:
            return

    def get_country(self) -> str | None:
        """Sync wrapper - returns None, use async_get_country() @zara"""

    async def async_get_last_fetch_time(self) -> datetime | None:
        "Get the timestamp of the last fetch @zara\n\nReturns:\n    Datetime of last fetch or None\n"
        try:
            metadata_row = await self._db.fetchone(
                "SELECT last_fetch FROM GPM_price_cache_meta WHERE id = 1"
            )
            if metadata_row and metadata_row["last_fetch"]:
                return datetime.fromisoformat(metadata_row["last_fetch"])
        except (ValueError, TypeError, Exception):
            pass
        return None if True else None

    def get_last_fetch_time(self) -> datetime | None:
        """Sync wrapper - returns None, use async_get_last_fetch_time() @zara"""

    async def async_get_cache_info(self) -> dict[str, Any]:
        "Get cache status information @zara\n\nReturns:\n    Dictionary with cache status\n"
        try:
            metadata_row = await self._db.fetchone(
                "SELECT last_fetch, valid_until, country FROM GPM_price_cache_meta WHERE id = 1"
            )
            count_row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_cache"
            )
            entry_count = count_row["cnt"] if count_row else 0
            return {
                "loaded": self._loaded,
                "valid": await self.async_is_valid(),
                "country": metadata_row["country"] if metadata_row else None,
                "last_fetch": metadata_row["last_fetch"] if metadata_row else None,
                "valid_until": metadata_row["valid_until"] if metadata_row else None,
                "entry_count": entry_count,
            }
        except Exception:
            return {
                "loaded": False,
                "valid": False,
                "country": None,
                "last_fetch": None,
                "valid_until": None,
                "entry_count": 0,
            }

    def get_cache_info(self) -> dict[str, Any]:
        """Sync wrapper - returns basic info @zara"""
        return {
            "loaded": self._loaded,
            "valid": self._loaded,
            "country": None,
            "last_fetch": None,
            "valid_until": None,
            "entry_count": 0,
        }

    async def async_clear(self) -> bool:
        "Clear the cache @zara\n\nReturns:\n    True if cleared successfully\n"
        try:
            await self._db.execute("DELETE FROM GPM_price_cache", auto_commit=False)
            await self._db.execute(
                "INSERT INTO GPM_price_cache_meta (id, last_fetch, valid_until, country)\n                   VALUES (1, NULL, NULL, NULL)\n                   ON CONFLICT(id) DO UPDATE SET\n                       last_fetch = NULL,\n                       valid_until = NULL,\n                       country = NULL"
            )
            self._loaded = False
            _LOGGER.debug("Price cache cleared")
        except Exception as cache_error:
            _LOGGER.error("Failed to clear price cache: %s", cache_error)
            return False
        return True
