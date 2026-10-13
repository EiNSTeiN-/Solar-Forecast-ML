# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
import aiohttp
from ..const import API_TIMEOUT, AWATTAR_API_URL_AT, AWATTAR_API_URL_DE

_LOGGER = logging.getLogger(__name__)
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


class ElectricityPriceService:
    """Service for fetching electricity spot prices from aWATTar API @zara"""

    def __init__(self, country: str = "DE") -> None:
        """Initialize the electricity price service @zara"""
        self.country = country.upper()
        self.api_url = (
            AWATTAR_API_URL_DE if self.country == "DE" else AWATTAR_API_URL_AT
        )
        self._price_cache = []
        self._last_update = None

    async def fetch_day_ahead_prices(
        self, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> list[dict[str, Any]] | None:
        """Fetch day-ahead prices from aWATTar API @zara

        Args:
            start_date: Start date for price data (default: today 00:00 UTC)
            end_date: End date for price data (default: 2 days from start)

        Returns:
            List of price dictionaries or None on error
        """
        if start_date is None:
            now = datetime.now().astimezone()
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_date is None:
            end_date = start_date + timedelta(days=2)
        start_timestamp_ms = int(start_date.timestamp() * 1000)
        end_timestamp_ms = int(end_date.timestamp() * 1000)
        request_url = (
            f"{self.api_url}?start={start_timestamp_ms}&end={end_timestamp_ms}"
        )
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(request_url) as response:
                        if response.status != 200:
                            _LOGGER.warning(
                                "aWATTar API returned status %s for %s (attempt %d/%d)",
                                response.status,
                                self.country,
                                attempt + 1,
                                MAX_RETRIES,
                            )
                            if attempt < MAX_RETRIES - 1:
                                await asyncio.sleep(RETRY_DELAY_SECONDS)
                                continue
                            return
                        payload = await response.json()
                        prices = self._parse_awattar_response(payload)
                        if prices:
                            self._price_cache = prices
                            self._last_update = datetime.now(timezone.utc)
                            _LOGGER.debug(
                                "Fetched %d price entries for %s",
                                len(prices),
                                self.country,
                            )
                        return prices
            except asyncio.TimeoutError as error:
                last_error = error
                _LOGGER.warning(
                    "Timeout fetching prices (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    error,
                )
            except aiohttp.ClientError as error:
                last_error = error
                _LOGGER.warning(
                    "Network error fetching prices (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    error,
                )
            except Exception as error:
                last_error = error
                _LOGGER.error(
                    "Unexpected error fetching prices: %s", error, exc_info=True
                )
                return
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        _LOGGER.error(
            "Failed to fetch prices after %d attempts. Last error: %s",
            MAX_RETRIES,
            last_error,
        )

    def _parse_awattar_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        "Parse aWATTar API response into standardized format @zara\n\nArgs:\n    response: Raw API response\n\nReturns:\n    List of parsed price entries\n"
        prices = []
        if "data" not in response:
            _LOGGER.error("Invalid aWATTar response: missing 'data' field")
            return prices
        for raw_entry in response["data"]:
            try:
                start_timestamp_ms = raw_entry.get("start_timestamp")
                market_price_eur_mwh = raw_entry.get("marketprice")
                if start_timestamp_ms is None or market_price_eur_mwh is None:
                    continue
                local_timestamp = datetime.fromtimestamp(
                    start_timestamp_ms / 1000
                ).astimezone()
                price_ct_kwh = round(market_price_eur_mwh / 10, 2)
                prices.append(
                    {
                        "timestamp": local_timestamp.replace(tzinfo=None),
                        "price": price_ct_kwh,
                        "hour": local_timestamp.hour,
                        "date": local_timestamp.date().isoformat(),
                    }
                )
            except (KeyError, TypeError, ValueError) as error:
                _LOGGER.warning("Error parsing price entry: %s", error)
                continue
        return prices

    def set_prices_from_cache(self, prices: list[dict[str, Any]]) -> None:
        "Set prices from external cache @zara\n\nArgs:\n    prices: List of price entries from cache\n"
        self._price_cache = prices
        _LOGGER.debug("Loaded %d prices from cache", len(prices))

    def get_current_price(self) -> float | None:
        """Get the current hour's spot price @zara"""
        now = datetime.now()
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        for entry in self._price_cache:
            timestamp = entry["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if not timestamp == current_hour:
                continue
            return entry["price"]

    def get_next_hour_price(self) -> float | None:
        """Get the next hour's spot price @zara"""
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        for entry in self._price_cache:
            timestamp = entry["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if not timestamp == next_hour:
                continue
            return entry["price"]

    def get_prices_for_date(self, date: datetime) -> list[dict[str, Any]]:
        """Get all prices for a specific date in LOCAL timezone @zara

        The date comparison is done in the local timezone to match user expectations.
        """
        if date.tzinfo is not None:
            local_date = date.astimezone().date()
        else:
            local_date = date.date()
        target_date_text = local_date.isoformat()
        date_prices = []
        for price_entry in self._price_cache:
            timestamp = price_entry["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is not None:
                entry_date_text = timestamp.astimezone().date().isoformat()
            else:
                entry_date_text = timestamp.date().isoformat()
            if entry_date_text == target_date_text:
                date_prices.append(price_entry)
        return date_prices

    def get_today_prices(self) -> list[dict[str, Any]]:
        """Get all prices for today (local timezone) @zara"""
        now = datetime.now()
        return self.get_prices_for_date(now)

    def get_tomorrow_prices(self) -> list[dict[str, Any]]:
        """Get all prices for tomorrow (local timezone) @zara"""
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        return self.get_prices_for_date(tomorrow)

    def get_cheapest_hour_today(self) -> dict[str, Any] | None:
        """Get the cheapest hour for today @zara"""
        today_prices = self.get_today_prices()
        if not today_prices:
            return
        return min(today_prices, key=lambda x: x["price"])

    def get_most_expensive_hour_today(self) -> dict[str, Any] | None:
        """Get the most expensive hour for today @zara"""
        today_prices = self.get_today_prices()
        if not today_prices:
            return
        return max(today_prices, key=lambda x: x["price"])

    def get_average_price_today(self) -> float | None:
        """Get the average price for today @zara"""
        today_prices = self.get_today_prices()
        if not today_prices:
            return
        return round(
            sum((price_entry["price"] for price_entry in today_prices))
            / len(today_prices),
            2,
        )

    def get_cheap_hours(
        self, max_price: float, date: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Get all hours where price is below max_price @zara"""
        if date is None:
            prices = self._price_cache
        else:
            prices = self.get_prices_for_date(date)
        return [
            price_entry for price_entry in prices if price_entry["price"] < max_price
        ]

    def get_next_cheap_hour(self, max_price: float) -> dict[str, Any] | None:
        """Get the next hour where price is below max_price @zara"""
        now = datetime.now()
        future_cheap_prices = []
        for price_entry in self._price_cache:
            timestamp = price_entry["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is not None:
                local_naive_timestamp = timestamp.astimezone().replace(tzinfo=None)
            else:
                local_naive_timestamp = timestamp
            if local_naive_timestamp >= now and price_entry["price"] < max_price:
                future_cheap_prices.append(price_entry)
        if not future_cheap_prices:
            return
        return min(
            future_cheap_prices,
            key=lambda x: (
                x["timestamp"]
                if isinstance(x["timestamp"], datetime)
                else datetime.fromisoformat(x["timestamp"])
            ),
        )

    def get_all_prices(self) -> list[dict[str, Any]]:
        """Get all cached prices @zara"""
        return self._price_cache.copy()

    @property
    def last_update(self) -> datetime | None:
        """Return the last update timestamp @zara"""
        return self._last_update

    @property
    def has_data(self) -> bool:
        """Check if price data is available @zara"""
        return len(self._price_cache) > 0
