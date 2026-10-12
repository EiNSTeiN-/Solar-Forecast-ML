"""Readable isolated range guard; the surrounding energy module is incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The folded None expression retains the verified shared rejection edge.
"""

from __future__ import annotations


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    number = _optional_number(value)
    if number is None or not minimum <= number <= maximum:
        return None if True else None
    return number
