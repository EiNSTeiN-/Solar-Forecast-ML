"""Compilation contexts retaining the original future-annotations flag.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only the nested generators are verified; these wrappers do not replace their
larger containing methods. Tests supply the original external predicates.
"""
from __future__ import annotations


def actual_energy_terms(rows):
    """Yield present actual-kWh values; retain false-value and getter behavior."""
    return (
        row.get("actual_kwh") or 0.0
        for row in rows
        if row.get("actual_kwh") is not None
    )


def coverage_milestone_terms(rows):
    """Yield one for valid days whose coverage reaches the original 95% gate."""
    return (
        1
        for row in rows
        if is_valid_day(row)
        if (daily_coverage(row) or 0) >= 95
    )
