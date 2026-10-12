# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Deterministic demo prices. Never written to history."""

from __future__ import annotations
import random
from datetime import datetime, timedelta
from typing import Any
from .base import PriceSlot, iter_local_hours

_DEMO_SHAPE = [
    22,
    21,
    20,
    20,
    21,
    23,
    27,
    31,
    30,
    28,
    26,
    25,
    24,
    24,
    25,
    27,
    31,
    36,
    38,
    35,
    31,
    28,
    25,
    23,
]


class DemoSource:
    mode = "demo"
    label = "Demo"
    has_spot_component = False
    needs_network = False

    async def async_get_slots(
        self, window_start_local: datetime, window_end_local: datetime
    ) -> list[dict[str, Any]]:
        slots = []
        for timestamp, local_hour in iter_local_hours(
            window_start_local, window_end_local
        ):
            day = timestamp.date()
            generator = random.Random(day.toordinal())
            jitter = 0.0
            for draw in range(timestamp.hour + 1):
                jitter = generator.uniform(-2.0, 2.0)
            total_price = round(_DEMO_SHAPE[timestamp.hour % 24] + jitter, 2)
            spot_net = round((total_price - 14.0) / 1.19, 2)
            slots.append(
                PriceSlot(
                    timestamp=timestamp,
                    price=spot_net,
                    hour=timestamp.hour,
                    date=day.isoformat(),
                    total_price=total_price,
                    price_source="demo",
                ).as_dict()
            )
            continue
        return slots


def demo_window(now_local: datetime) -> tuple[datetime, datetime]:
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start, start + timedelta(days=2))
