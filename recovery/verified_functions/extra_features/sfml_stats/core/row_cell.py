"""Isolated row accessor shared by two incomplete statistics modules.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Each original function is proved independently before it is tested.
"""

from __future__ import annotations


def _cell(row: Any, index: int, key: str) -> Any:
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            return row[index]
    return row[index]
