# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Best Hour Calculator for Solar Forecast Energy AI V16.2.0.

Calculates the hour with highest predicted production.
Uses database instead of JSON files.

@zara
"""
import logging
from datetime import datetime
from typing import Any, Optional, Tuple

_LOGGER = logging.getLogger(__name__)


class BestHourCalculator:
    """Calculate best production hour based on forecasts @zara"""

    def __init__(self, db_manager: Any):
        """Initialize calculator @zara"""
        self.db_manager = db_manager
        self.ai_predictor = None

    async def calculate_best_hour_today(self) -> Tuple[Optional[int], Optional[float]]:
        """Find hour with highest predicted production @zara"""
        try:
            today_date = datetime.now().date().isoformat()
            rows = await self.db_manager.fetchall(
                "SELECT target_hour, prediction_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND prediction_kwh > 0\n                   ORDER BY prediction_kwh DESC\n                   LIMIT 1",
                (today_date,),
            )
            if rows:
                return (rows[0][0], rows[0][1])
            return self._get_solar_noon_fallback()
        except Exception as error:
            _LOGGER.debug(f"Best hour calculation failed: {error}")
            return self._get_solar_noon_fallback()

    def _get_solar_noon_fallback(self) -> Tuple[int, float]:
        """Return solar noon as fallback @zara"""
        return (12, 0.0)
