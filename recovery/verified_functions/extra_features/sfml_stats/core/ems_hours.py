"""Verified EMS hour selector; the class is compilation context only.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from __future__ import annotations

from typing import Any


class EMSManager:
    def _hour_points_from(
        self, hours: list[dict[str, Any]], start_hour: int
    ) -> list[dict[str, Any]]:
        return [
            point
            for point in hours
            if isinstance(point.get("hour"), int)
            if point["hour"] >= start_hour
        ]
