"""Verified CSV-header function; its containing API module is incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from __future__ import annotations

from typing import Any


def _csv_headers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        header.strip() for header in value if isinstance(header, str) if header.strip()
    ]
