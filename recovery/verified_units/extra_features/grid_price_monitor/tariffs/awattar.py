# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""aWATTar day-ahead source. Totals match the existing enrich formula."""

from __future__ import annotations
from datetime import datetime
from typing import Any
from ..const import PRICE_SOURCE_AWATTAR, TARIFF_MODE_DYNAMIC
from ..core.calculator import PriceCalculator
from ..core.price_service import ElectricityPriceService
from .base import PriceSlot


class AwattarSource:
    mode = TARIFF_MODE_DYNAMIC
    has_spot_component = True
    needs_network = True

    def __init__(
        self,
        country: str,
        calculator: PriceCalculator,
        price_service: ElectricityPriceService | None = None,
    ) -> None:
        self._country = country
        self._calculator = calculator
        self._price_service = price_service or ElectricityPriceService(country)
        self.label = f"Dynamisch (aWATTar {country})"

    def enrich_parsed(self, prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add total_price, naive-local timestamp, and price_source=awattar."""
        enriched = []
        for price in prices:
            entry = price.copy()
            spot_net = price.get("price", 0)
            entry["total_price"] = self._calculator.calculate_total_price(spot_net)
            timestamp = price.get("timestamp")
            if timestamp is not None:
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp)
                local_timestamp = timestamp.astimezone()
                entry["timestamp"] = local_timestamp.replace(tzinfo=None).isoformat()
            entry["price_source"] = PRICE_SOURCE_AWATTAR
            enriched.append(entry)
        return enriched

    def slots_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        parsed = self._price_service._parse_awattar_response(response)
        return self.enrich_parsed(parsed)

    async def async_get_slots(
        self, window_start_local: datetime, window_end_local: datetime
    ) -> list[dict[str, Any]] | None:
        prices = await self._price_service.fetch_day_ahead_prices(
            start_date=window_start_local, end_date=window_end_local
        )
        if prices is None:
            return
        enriched = self.enrich_parsed(prices)
        slots = []
        for price in enriched:
            timestamp = price.get("timestamp")
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            hour_start = timestamp.replace(
                tzinfo=None, minute=0, second=0, microsecond=0
            )
            slots.append(
                PriceSlot(
                    timestamp=hour_start,
                    price=float(price.get("price", 0)),
                    hour=int(price.get("hour", hour_start.hour)),
                    date=str(price.get("date") or hour_start.date().isoformat()),
                    total_price=float(price.get("total_price", 0)),
                    price_source=PRICE_SOURCE_AWATTAR,
                ).as_dict()
            )
            continue
        return slots
