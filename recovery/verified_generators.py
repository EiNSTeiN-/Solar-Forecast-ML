"""Readable compilation contexts for independently recovered generator units.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only the nested generator units are claimed verified. These wrappers are not
replacement integration APIs; the containing application methods remain
incomplete. External predicates and numeric dependencies are supplied by tests.
"""


def daily_amount_terms(rows):
    """Yield column-one amounts using the original truth filter and fallback."""
    return (row[1] or 0 for row in rows if row[1])


def finite_sample_terms(values, numeric_library):
    """Yield finite checks lazily with the original captured callable lookup."""
    # The folded condition preserves LOAD_ATTR plus PUSH_NULL in the verified
    # generator; it executes no runtime condition and evaluates the lookup once.
    return (
        (numeric_library.isfinite if True else None)(value)
        for value in values
    )
