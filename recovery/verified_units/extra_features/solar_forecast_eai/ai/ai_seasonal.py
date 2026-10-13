# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Seasonal adjustment factors for solar production.
Monthly factors are learned from actual vs predicted ratios and stored in database.
"""
import logging
from typing import Any, Dict
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
DEFAULT_FACTORS = {
    1: 0.85,
    2: 0.9,
    3: 0.95,
    4: 1.0,
    5: 1.05,
    6: 1.1,
    7: 1.1,
    8: 1.05,
    9: 1.0,
    10: 0.95,
    11: 0.9,
    12: 0.85,
}


class SeasonalAdjuster:
    """Monthly adjustment factors for solar production @zara"""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize with database manager @zara"""
        self.db_manager = db_manager
        self.factors = DEFAULT_FACTORS.copy()
        self.sample_counts = {month: 0 for month in range(1, 13)}
        self._loaded = False
        _LOGGER.info("SeasonalAdjuster initialized")

    async def async_load(self):
        """Load factors from database @zara"""
        if self._loaded:
            return
        stored_factors = await self.db_manager.get_seasonal_factors()
        for month, (factor, sample_count) in stored_factors.items():
            self.factors[month] = factor
            self.sample_counts[month] = sample_count
        self._loaded = True
        _LOGGER.info("Seasonal factors loaded from database")

    def get_factor(self, month: int) -> float:
        """Get adjustment factor for month 1-12 @zara"""
        if month < 1 or month > 12:
            return 1.0
        return self.factors.get(month, 1.0)

    def update(self, month: int, actual: float, predicted: float):
        """Update factor based on actual vs predicted @zara"""
        if month < 1 or month > 12:
            return None
        if predicted <= 0 or actual < 0:
            return None
        observed_ratio = actual / predicted
        observed_ratio = max(0.5, min(1.5, observed_ratio))
        sample_count = self.sample_counts.get(month, 0)
        previous_factor = self.factors.get(month, 1.0)
        learning_rate = 0.1 if sample_count < 10 else 0.05
        self.factors[month] = (
            previous_factor * (1 - learning_rate) + observed_ratio * learning_rate
        )
        self.sample_counts[month] = sample_count + 1

    async def async_save(self):
        """Save factors to database @zara"""
        try:
            pass
            factors_payload = {
                str(month): month_value for month, month_value in self.factors.items()
            }
            sample_counts_payload = {
                str(month): month_value
                for month, month_value in self.sample_counts.items()
            }
            await self.db_manager.save_seasonal_factors(
                factors_payload, sample_counts_payload
            )
            _LOGGER.debug("Seasonal factors saved to database")
        except Exception as save_error:
            _LOGGER.error(f"Failed to save seasonal factors: {save_error}")
        return None if True else None

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics for diagnostics @zara"""
        return {
            "factors": self.factors.copy(),
            "sample_counts": self.sample_counts.copy(),
            "min_factor": min(self.factors.values()),
            "max_factor": max(self.factors.values()),
        }
