"""Isolated feature conversion; its containing module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The class provides compilation context, not a replacement feature engineer.
"""


class FeatureEngineer:
    def _safe_float(self, value: Any, default: float) -> float:
        """Convert value to float with fallback @zara"""
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default
