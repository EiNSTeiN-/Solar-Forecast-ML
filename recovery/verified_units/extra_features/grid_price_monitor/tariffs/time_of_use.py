# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""High/low tariff with optional weekend and nationwide holiday low periods."""

from __future__ import annotations
import logging
from datetime import datetime, time
from typing import Any
from ..const import PRICE_SOURCE_TIME_OF_USE, TARIFF_MODE_TIME_OF_USE
from .base import PriceSlot, iter_local_hours

_LOGGER = logging.getLogger(__name__)
_HOLIDAYS_WARNED = False


def parse_clock(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(microsecond=0)
    text = str(value)
    parts = text.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(float(parts[2])) if len(parts) > 2 else 0
    return time(hour, minute, second)


def _in_high_window(local_time: time, high_start: time, high_end: time) -> bool:
    if high_start == high_end:
        return True
    if high_end <= high_start:
        return local_time >= high_start or local_time < high_end
    return high_start <= local_time < high_end


def nationwide_holiday(country: str, day) -> bool:
    global _HOLIDAYS_WARNED
    try:
        import holidays as holidays
    except ImportError:
        if not _HOLIDAYS_WARNED:
            _LOGGER.warning(
                "Python package holidays is missing; holiday-low rule is inactive"
            )
            _HOLIDAYS_WARNED = True
        return False
    try:
        calendar = holidays.country_holidays(country)
    except Exception as error:
        if not _HOLIDAYS_WARNED:
            _LOGGER.warning("Could not load holidays for %s: %s", country, error)
            _HOLIDAYS_WARNED = True
        return False
    return day in calendar


class TimeOfUseSource:
    mode = TARIFF_MODE_TIME_OF_USE
    has_spot_component = False
    needs_network = False

    def __init__(
        self,
        *,
        high_price: float,
        low_price: float,
        high_start: Any,
        high_end: Any,
        weekend_low: bool,
        holiday_low: bool,
        country: str,
    ) -> None:
        self._high = float(high_price)
        self._low = float(low_price)
        self._high_start = parse_clock(high_start)
        self._high_end = parse_clock(high_end)
        self._weekend_low = bool(weekend_low)
        self._holiday_low = bool(holiday_low)
        self._country = country
        self.label = f"HT/NT {self._high_start.strftime('%H:%M')}–{self._high_end.strftime('%H:%M')}"

    def price_for(self, local: datetime) -> float:
        if self._weekend_low and local.weekday() >= 5:
            return self._low
        if self._holiday_low and nationwide_holiday(self._country, local.date()):
            return self._low
        if _in_high_window(
            local.time().replace(microsecond=0), self._high_start, self._high_end
        ):
            return self._high
        return self._low

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
                    price_source=PRICE_SOURCE_TIME_OF_USE,
                ).as_dict()
            )
        return slots
