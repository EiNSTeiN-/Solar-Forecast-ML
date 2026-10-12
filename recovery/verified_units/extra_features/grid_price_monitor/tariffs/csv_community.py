# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""CSV-community mode uses a base tariff for live slots."""

from __future__ import annotations
from datetime import datetime
from typing import Any
from ..const import TARIFF_MODE_CSV_COMMUNITY
from .base import PriceSource


class CsvCommunitySource:
    mode = TARIFF_MODE_CSV_COMMUNITY
    needs_network = False

    def __init__(self, base: PriceSource) -> None:
        self._base = base
        self.has_spot_component = base.has_spot_component
        self.needs_network = base.needs_network
        self.label = f"CSV + {base.label}"

    async def async_get_slots(
        self, window_start_local: datetime, window_end_local: datetime
    ) -> list[dict[str, Any]] | None:
        return await self._base.async_get_slots(window_start_local, window_end_local)
