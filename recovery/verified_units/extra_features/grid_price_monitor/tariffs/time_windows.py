# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Named time windows with weekday filters. First matching window wins."""

from __future__ import annotations
from datetime import datetime, time
from typing import Any
from ..const import PRICE_SOURCE_TIME_WINDOWS, TARIFF_MODE_TIME_WINDOWS
from .base import PriceSlot, iter_local_hours
from .time_of_use import parse_clock


def _clock_minutes(value: time) -> int:
    return value.hour * 60 + value.minute + value.second // 60


def window_covers(local: datetime, window: dict[str, Any]) -> bool:
    start_time = parse_clock(window["start"])
    end_time = parse_clock(window["end"])
    weekdays = {int(day) for day in window.get("weekdays") or []}
    weekday = local.weekday()
    clock_time = local.time().replace(microsecond=0)
    if start_time == end_time:
        return weekday in weekdays
    if end_time <= start_time:
        if weekday in weekdays and clock_time >= start_time:
            return True
        previous_weekday = (weekday - 1) % 7
        return previous_weekday in weekdays and clock_time < end_time
    return weekday in weekdays and start_time <= clock_time < end_time


def windows_overlap(windows: list[dict[str, Any]]) -> bool:
    """True when two windows cover the same weekday minute."""
    minute_owners = {}
    for window_index, window in enumerate(windows):
        start_time = parse_clock(window["start"])
        end_time = parse_clock(window["end"])
        weekdays = {int(day) for day in window.get("weekdays") or []}
        for weekday in weekdays:
            segments = []
            if start_time == end_time:
                segments.append((weekday, 0, 1440))
            elif end_time <= start_time:
                segments.append((weekday, _clock_minutes(start_time), 1440))
                segments.append(((weekday + 1) % 7, 0, _clock_minutes(end_time)))
            else:
                segments.append(
                    (weekday, _clock_minutes(start_time), _clock_minutes(end_time))
                )
            for day, start_minute, end_minute in segments:
                for minute in range(start_minute, end_minute):
                    weekday_minute = (day, minute)
                    previous_owner = minute_owners.get(weekday_minute)
                    if previous_owner is not None and previous_owner != window_index:
                        return True
                    minute_owners[weekday_minute] = window_index
        continue
    return False


class TimeWindowSource:
    mode = TARIFF_MODE_TIME_WINDOWS
    has_spot_component = False
    needs_network = False

    def __init__(self, *, default_price: float, windows: list[dict[str, Any]]) -> None:
        self._default = float(default_price)
        self._windows = list(windows)
        window_names = [
            str(window.get("name") or "Fenster") for window in self._windows[:2]
        ]
        self.label = (
            " / ".join(window_names)
            if window_names
            else f"Zeitfenster {self._default:.2f} ct/kWh"
        )
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def price_for(self, local: datetime) -> float:
        for window in self._windows:
            if not window_covers(local, window):
                continue
            return float(window["price"])
        return self._default

    async def async_get_slots(
        self, window_start_local: datetime, window_end_local: datetime
    ) -> list[dict[str, Any]]:
        slots = []
        for timestamp, local_hour in iter_local_hours(
            window_start_local, window_end_local
        ):
            price = self.price_for(local_hour)
            slots.append(
                PriceSlot(
                    timestamp=timestamp,
                    price=price,
                    hour=timestamp.hour,
                    date=timestamp.date().isoformat(),
                    total_price=price,
                    price_source=PRICE_SOURCE_TIME_WINDOWS,
                ).as_dict()
            )
        return slots
