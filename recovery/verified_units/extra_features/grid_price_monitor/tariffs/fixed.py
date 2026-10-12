# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Constant total-price tariff."""

from __future__ import annotations
from datetime import datetime
from typing import Any
from ..const import PRICE_SOURCE_FIXED, TARIFF_MODE_FIXED
from .base import PriceSlot, iter_local_hours


class FixedSource:
    mode = TARIFF_MODE_FIXED
    has_spot_component = False
    needs_network = False

    def __init__(self, total_price: float) -> None:
        self._total = float(total_price)
        self.label = f"Fixpreis {self._total:.2f} ct/kWh"

    async def async_get_slots(
        self, window_start_local: datetime, window_end_local: datetime
    ) -> list[dict[str, Any]]:
        slots = []
        for timestamp, local_hour in iter_local_hours(
            window_start_local, window_end_local
        ):
            slots.append(
                PriceSlot(
                    timestamp=timestamp,
                    price=self._total,
                    hour=timestamp.hour,
                    date=timestamp.date().isoformat(),
                    total_price=self._total,
                    price_source=PRICE_SOURCE_FIXED,
                ).as_dict()
            )
        return slots
