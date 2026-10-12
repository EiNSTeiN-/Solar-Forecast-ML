# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING
from ..const import DAILY_AVERAGES_RETENTION_ROWS, MONTHLY_SUMMARIES_RETENTION_MONTHS

if TYPE_CHECKING:
    from .db_connector import GPMDatabaseConnector
_LOGGER = logging.getLogger(__name__)


class StatisticsStore:
    """Manages aggregated statistics storage in SQLite @zara"""

    def __init__(self, db: GPMDatabaseConnector) -> None:
        # fmt: off
        """Initialize the statistics store @zara

Args:
    db: GPM database connector instance
"""
        # fmt: on
        self._db = db
        self._loaded = False

    async def async_load(self) -> bool:
        # fmt: off
        """Check if statistics exist in database @zara

Returns:
    True if ready
"""
        # fmt: on
        try:
            row = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_daily_averages"
            )
            if row and row["cnt"] > 0:
                self._loaded = True
                _LOGGER.debug("Loaded statistics from database")
        except Exception as error:
            _LOGGER.warning("Failed to load statistics: %s", error)
        return True

    async def async_update_daily_average(
        self,
        date: str,
        average_net: float,
        average_total: float,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> None:
        # fmt: off
        """Update or add daily average statistics @zara

Args:
    date: Date string (YYYY-MM-DD)
    average_net: Average net price for the day
    average_total: Average total price for the day
    min_price: Minimum price for the day
    max_price: Maximum price for the day
"""
        # fmt: on
        try:
            await self._db.execute(
                "INSERT INTO GPM_daily_averages\n                   (date, average_net, average_total, min_price, max_price)\n                   VALUES (?, ?, ?, ?, ?)\n                   ON CONFLICT(date) DO UPDATE SET\n                       average_net = excluded.average_net,\n                       average_total = excluded.average_total,\n                       min_price = COALESCE(excluded.min_price, GPM_daily_averages.min_price),\n                       max_price = COALESCE(excluded.max_price, GPM_daily_averages.max_price)",
                (date, average_net, average_total, min_price, max_price),
            )
            if min_price is not None and max_price is not None:
                await self._async_update_extremes(min_price, max_price, date)
            await self._db.execute(
                "DELETE FROM GPM_daily_averages\n                   WHERE date NOT IN (\n                       SELECT date FROM GPM_daily_averages\n                       ORDER BY date DESC LIMIT ?\n                   )",
                (DAILY_AVERAGES_RETENTION_ROWS,),
            )
            self._loaded = True
        except Exception as error:
            _LOGGER.error("Failed to update daily average: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    async def _async_update_extremes(
        self, min_price: float, max_price: float, date: str
    ) -> None:
        # fmt: off
        """Update all-time price extremes @zara

Args:
    min_price: Today's minimum price
    max_price: Today's maximum price
    date: Date string (YYYY-MM-DD)
"""
        # fmt: on
        try:
            row = await self._db.fetchone(
                "SELECT all_time_low, all_time_low_date, all_time_high, all_time_high_date\n                   FROM GPM_price_extremes WHERE id = 1"
            )
            all_time_low = min_price
            all_time_low_date = date
            all_time_high = max_price
            all_time_high_date = date
            if row:
                if row["all_time_low"] is not None and row["all_time_low"] <= min_price:
                    all_time_low = row["all_time_low"]
                    all_time_low_date = row["all_time_low_date"] or date
                if (
                    row["all_time_high"] is not None
                    and row["all_time_high"] >= max_price
                ):
                    all_time_high = row["all_time_high"]
                    all_time_high_date = row["all_time_high_date"] or date
            await self._db.execute(
                "INSERT INTO GPM_price_extremes\n                   (id, all_time_low, all_time_low_date, all_time_high, all_time_high_date)\n                   VALUES (1, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       all_time_low = excluded.all_time_low,\n                       all_time_low_date = excluded.all_time_low_date,\n                       all_time_high = excluded.all_time_high,\n                       all_time_high_date = excluded.all_time_high_date",
                (all_time_low, all_time_low_date, all_time_high, all_time_high_date),
            )
        except Exception as error:
            _LOGGER.error("Failed to update price extremes: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    async def async_update_monthly_summary(
        self,
        year: int,
        month: int,
        average_price: float,
        total_cheap_hours: int,
        country: str,
    ) -> None:
        # fmt: off
        """Update or add monthly summary statistics @zara

Args:
    year: Year
    month: Month (1-12)
    average_price: Average price for the month
    total_cheap_hours: Total hours with cheap electricity
    country: Country code
"""
        # fmt: on
        month_key = f"{year}-{month:02d}"
        try:
            await self._db.execute(
                "INSERT INTO GPM_monthly_summaries\n                   (month, average_price, cheap_hours, country)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(month) DO UPDATE SET\n                       average_price = excluded.average_price,\n                       cheap_hours = excluded.cheap_hours,\n                       country = excluded.country",
                (month_key, average_price, total_cheap_hours, country),
            )
            await self._db.execute(
                "DELETE FROM GPM_monthly_summaries\n                   WHERE month NOT IN (\n                       SELECT month FROM GPM_monthly_summaries\n                       ORDER BY month DESC LIMIT ?\n                   )",
                (MONTHLY_SUMMARIES_RETENTION_MONTHS,),
            )
        except Exception as error:
            _LOGGER.error("Failed to update monthly summary: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    async def async_update_battery_totals(
        self, today_kwh: float, week_kwh: float, month_kwh: float
    ) -> None:
        # fmt: off
        """Update battery charging totals @zara

Args:
    today_kwh: Energy charged today in kWh
    week_kwh: Energy charged this week in kWh
    month_kwh: Energy charged this month in kWh
"""
        # fmt: on
        try:
            await self._db.execute(
                "INSERT INTO GPM_battery_totals\n                   (id, today_kwh, week_kwh, month_kwh)\n                   VALUES (1, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       today_kwh = excluded.today_kwh,\n                       week_kwh = excluded.week_kwh,\n                       month_kwh = excluded.month_kwh",
                (today_kwh, week_kwh, month_kwh),
            )
        except Exception as error:
            _LOGGER.error("Failed to update battery totals: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    async def async_get_daily_average(self, date: datetime) -> dict[str, Any] | None:
        # fmt: off
        """Get daily average for a specific date @zara

Args:
    date: Date to get average for

Returns:
    Daily average entry or None
"""
        # fmt: on
        date_key = date.strftime("%Y-%m-%d")
        try:
            row = await self._db.fetchone(
                "SELECT date, average_net, average_total, min_price, max_price\n                   FROM GPM_daily_averages WHERE date = ?",
                (date_key,),
            )
            if row:
                return {
                    "date": row["date"],
                    "average_net": row["average_net"],
                    "average_total": row["average_total"],
                    "min_price": row["min_price"],
                    "max_price": row["max_price"],
                }
        except Exception as error:
            _LOGGER.error("Failed to get daily average: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    def get_daily_average(self, date: datetime) -> dict[str, Any] | None:
        """Sync wrapper - returns None, use async version @zara"""

    async def async_get_monthly_summary(
        self, year: int, month: int
    ) -> dict[str, Any] | None:
        # fmt: off
        """Get monthly summary for a specific month @zara

Args:
    year: Year
    month: Month (1-12)

Returns:
    Monthly summary entry or None
"""
        # fmt: on
        month_key = f"{year}-{month:02d}"
        try:
            row = await self._db.fetchone(
                "SELECT month, average_price, cheap_hours, country\n                   FROM GPM_monthly_summaries WHERE month = ?",
                (month_key,),
            )
            if row:
                return {
                    "month": row["month"],
                    "average_price": row["average_price"],
                    "cheap_hours": row["cheap_hours"],
                    "country": row["country"],
                }
        except Exception as error:
            _LOGGER.error("Failed to get monthly summary: %s", error)
        # Preserve the verified shared-return edge.
        return None if True else None

    def get_monthly_summary(self, year: int, month: int) -> dict[str, Any] | None:
        """Sync wrapper - returns None, use async version @zara"""

    async def async_get_price_extremes(self) -> dict[str, Any]:
        # fmt: off
        """Get all-time price extremes @zara

Returns:
    Dictionary with price extremes
"""
        # fmt: on
        try:
            row = await self._db.fetchone(
                "SELECT all_time_low, all_time_low_date,\n                          all_time_high, all_time_high_date\n                   FROM GPM_price_extremes WHERE id = 1"
            )
            if row:
                return {
                    "all_time_low": row["all_time_low"],
                    "all_time_low_date": row["all_time_low_date"],
                    "all_time_high": row["all_time_high"],
                    "all_time_high_date": row["all_time_high_date"],
                }
        except Exception:
            pass
        return {} if True else None

    def get_price_extremes(self) -> dict[str, Any]:
        """Sync wrapper - returns empty dict @zara"""
        return {}

    async def async_get_battery_totals(self) -> dict[str, Any]:
        # fmt: off
        """Get battery charging totals @zara

Returns:
    Dictionary with battery totals
"""
        # fmt: on
        try:
            row = await self._db.fetchone(
                "SELECT today_kwh, week_kwh, month_kwh FROM GPM_battery_totals WHERE id = 1"
            )
            if row:
                return {
                    "today_kwh": row["today_kwh"],
                    "week_kwh": row["week_kwh"],
                    "month_kwh": row["month_kwh"],
                }
        except Exception:
            pass
        return {"today_kwh": 0.0, "week_kwh": 0.0, "month_kwh": 0.0}

    def get_battery_totals(self) -> dict[str, Any]:
        """Sync wrapper - returns defaults @zara"""
        return {"total_charged_kwh": 0.0, "total_cost_saved": 0.0}

    async def async_get_stats_summary(self) -> dict[str, Any]:
        # fmt: off
        """Get summary of all statistics @zara

Returns:
    Dictionary with statistics summary
"""
        # fmt: on
        try:
            daily_count = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_daily_averages"
            )
            monthly_count = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_monthly_summaries"
            )
            price_extremes = await self.async_get_price_extremes()
            battery_totals = await self.async_get_battery_totals()
            return {
                "loaded": self._loaded,
                "daily_average_count": daily_count["cnt"] if daily_count else 0,
                "monthly_summary_count": monthly_count["cnt"] if monthly_count else 0,
                "price_extremes": price_extremes,
                "battery_totals": battery_totals,
            }
        except Exception:
            return {"loaded": False}

    def get_stats_summary(self) -> dict[str, Any]:
        """Sync wrapper - returns basic info @zara"""
        return {"loaded": self._loaded}
