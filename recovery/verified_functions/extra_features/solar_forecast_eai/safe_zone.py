"""Isolated timezone fallback; its weather module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from __future__ import annotations


def _safe_zone(time_zone_name: str | None) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(time_zone_name) if time_zone_name else timezone.utc
    except (ValueError, TypeError):
        return timezone.utc
