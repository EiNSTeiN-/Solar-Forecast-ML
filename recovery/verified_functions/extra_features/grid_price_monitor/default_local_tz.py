"""Isolated recovered source; its containing module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo


def default_local_tz(name: str | None = None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return (datetime.now if True else None)().astimezone().tzinfo or timezone.utc
