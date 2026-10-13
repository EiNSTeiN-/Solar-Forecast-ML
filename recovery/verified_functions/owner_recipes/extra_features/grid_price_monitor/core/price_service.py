# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
import aiohttp
from ..const import API_TIMEOUT, AWATTAR_API_URL_AT, AWATTAR_API_URL_DE


class ElectricityPriceService:

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
            _var_var_0 = datetime.now().astimezone()
            start_date = _var_var_0.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_date is None:
            end_date = start_date + timedelta(days=2)
        _var_var_1 = int(start_date.timestamp() * 1000)
        _var_var_2 = int(end_date.timestamp() * 1000)
        _var_var_3 = f"{self.api_url}?start={_var_var_1}&end={_var_var_2}"
        _var_var_4 = None
        for _var_var_5 in range(MAX_RETRIES):
            try:
                _var_var_6 = aiohttp.ClientTimeout(total=API_TIMEOUT)
                async with aiohttp.ClientSession(timeout=_var_var_6) as _var_var_7:
                    async with _var_var_7.get(_var_var_3) as _var_var_8:
                        if _var_var_8.status != 200:
                            _LOGGER.warning(
                                "aWATTar API returned status %s for %s (attempt %d/%d)",
                                _var_var_8.status,
                                self.country,
                                _var_var_5 + 1,
                                MAX_RETRIES,
                            )
                            if _var_var_5 < MAX_RETRIES - 1:
                                await asyncio.sleep(RETRY_DELAY_SECONDS)
                                continue
                            return
                        _var_var_9 = await _var_var_8.json()
                        _var_var_10 = self._parse_awattar_response(_var_var_9)
                        if _var_var_10:
                            self._price_cache = _var_var_10
                            self._last_update = datetime.now(timezone.utc)
                            _LOGGER.debug(
                                "Fetched %d price entries for %s",
                                len(_var_var_10),
                                self.country,
                            )
                        return _var_var_10
            except asyncio.TimeoutError as _var_var_11:
                _var_var_4 = _var_var_11
                _LOGGER.warning(
                    "Timeout fetching prices (attempt %d/%d): %s",
                    _var_var_5 + 1,
                    MAX_RETRIES,
                    _var_var_11,
                )
            except aiohttp.ClientError as _var_var_11:
                _var_var_4 = _var_var_11
                _LOGGER.warning(
                    "Network error fetching prices (attempt %d/%d): %s",
                    _var_var_5 + 1,
                    MAX_RETRIES,
                    _var_var_11,
                )
            except Exception as _var_var_11:
                _var_var_4 = _var_var_11
                _LOGGER.error(
                    "Unexpected error fetching prices: %s", _var_var_11, exc_info=True
                )
                return
            if _var_var_5 < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        _LOGGER.error(
            "Failed to fetch prices after %d attempts. Last error: %s",
            MAX_RETRIES,
            _var_var_4,
        )
