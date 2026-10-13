# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Historical Production Calculator for Solar Forecast ML.

Provides methods to calculate historical production statistics
using database queries instead of Home Assistant recorder.

@author zara
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Any
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)


class ProductionCalculator:
    """Historical Production Calculator using database. @zara"""

    def __init__(self, hass: HomeAssistant, db_manager: "DatabaseManager"):
        """Initialize the Production Calculator. @zara"""
        self.hass = hass
        self.db = db_manager
        _LOGGER.debug("ProductionCalculator initialized (Database mode)")

    async def async_get_peak_production_time(
        self, power_entity: Optional[str] = None
    ) -> str:
        """Calculate peak production time from stored hourly data. @zara"""
        _LOGGER.debug("Peak time calculation: Using stored hourly production data")
        try:
            today = dt_util.now().date()
            today_iso = today.isoformat()
            rows = await self.db.fetchall(
                "SELECT target_hour, actual_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh IS NOT NULL\n                   ORDER BY target_hour",
                (today_iso,),
            )
            if not rows:
                return "12:00"
            peak_kwh = -1
            peak_hour = 12
            for row in rows:
                hour = row[0]
                actual_kwh = row[1]
                if actual_kwh is not None and actual_kwh > peak_kwh:
                    peak_kwh = actual_kwh
                    peak_hour = hour
            return f"{peak_hour:02d}:00"
        except Exception as error:
            _LOGGER.error(
                "Error calculating peak production time: %s", error, exc_info=True
            )
            return "12:00"

    async def calculate_yesterday_total_yield(self, yield_entity: str = "") -> float:
        """Calculate yesterday's total yield from database. @zara"""
        try:
            yesterday = (dt_util.now() - timedelta(days=1)).date()
            yesterday_iso = yesterday.isoformat()
            row = await self.db.fetchone(
                "SELECT actual_total_kwh FROM daily_summaries\n                   WHERE date = ?",
                (yesterday_iso,),
            )
            if row and row[0] is not None:
                return float(row[0])
            row = await self.db.fetchone(
                "SELECT SUM(actual_kwh) FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh IS NOT NULL",
                (yesterday_iso,),
            )
            if row and row[0] is not None:
                return float(row[0])
            _LOGGER.debug("No actual kWh found for yesterday")
        except Exception as error:
            _LOGGER.error("Error calculating yesterday's yield: %s", error)
        return 0.0

    async def get_last_7_days_average_yield(self, yield_entity: str = "") -> float:
        """Get average yield for the last 7 days from database. @zara"""
        try:
            today = dt_util.now().date()
            start_date = today - timedelta(days=7)
            start_iso = start_date.isoformat()
            end_iso = today.isoformat()
            row = await self.db.fetchone(
                "SELECT AVG(actual_total_kwh), COUNT(*)\n                   FROM daily_summaries\n                   WHERE date >= ? AND date < ?\n                     AND actual_total_kwh IS NOT NULL",
                (start_iso, end_iso),
            )
            if row and row[0] is not None and (row[1] > 0):
                return float(row[0])
            row = await self.db.fetchone(
                "SELECT AVG(daily_sum) FROM (\n                       SELECT target_date, SUM(actual_kwh) as daily_sum\n                       FROM hourly_predictions\n                       WHERE target_date >= ? AND target_date < ?\n                         AND actual_kwh IS NOT NULL\n                       GROUP BY target_date\n                   )",
                (start_iso, end_iso),
            )
            if row and row[0] is not None:
                return float(row[0])
        except Exception as error:
            _LOGGER.error("Error calculating 7-day average yield: %s", error)
        return 0.0

    async def get_monthly_production_statistics(
        self, yield_entity: str = ""
    ) -> Dict[str, Any]:
        """Get monthly production statistics from database. @zara"""
        try:
            now = dt_util.now()
            month_start = now.replace(day=1).date()
            start_iso = month_start.isoformat()
            row = await self.db.fetchone(
                "SELECT\n                       SUM(actual_total_kwh) as total,\n                       AVG(actual_total_kwh) as avg,\n                       MAX(actual_total_kwh) as best,\n                       MIN(actual_total_kwh) as worst,\n                       COUNT(*) as days\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (start_iso,),
            )
            if row and row[4] > 0:
                return {
                    "total_kwh": round(row[0] or 0.0, 2),
                    "average_daily_kwh": round(row[1] or 0.0, 2),
                    "best_day_kwh": round(row[2] or 0.0, 2),
                    "worst_day_kwh": round(row[3] or 0.0, 2),
                    "days_with_data": row[4],
                }
            row = await self.db.fetchone(
                "SELECT\n                       SUM(daily_sum) as total,\n                       AVG(daily_sum) as avg,\n                       MAX(daily_sum) as best,\n                       MIN(daily_sum) as worst,\n                       COUNT(*) as days\n                   FROM (\n                       SELECT target_date, SUM(actual_kwh) as daily_sum\n                       FROM hourly_predictions\n                       WHERE target_date >= ? AND actual_kwh IS NOT NULL\n                       GROUP BY target_date\n                   )",
                (start_iso,),
            )
            if row and row[4] > 0:
                return {
                    "total_kwh": round(row[0] or 0.0, 2),
                    "average_daily_kwh": round(row[1] or 0.0, 2),
                    "best_day_kwh": round(row[2] or 0.0, 2),
                    "worst_day_kwh": round(row[3] or 0.0, 2),
                    "days_with_data": row[4],
                }
            return {
                "total_kwh": 0.0,
                "average_daily_kwh": 0.0,
                "best_day_kwh": 0.0,
                "worst_day_kwh": 0.0,
                "days_with_data": 0,
            }
        except Exception as error:
            _LOGGER.error("Error calculating monthly production statistics: %s", error)
            return {
                "total_kwh": 0.0,
                "average_daily_kwh": 0.0,
                "best_day_kwh": 0.0,
                "worst_day_kwh": 0.0,
                "days_with_data": 0,
            }

    async def get_historical_average(self) -> Optional[float]:
        """Get historical average production. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT AVG(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE actual_total_kwh IS NOT NULL"
            )
            if row and row[0] is not None:
                return float(row[0])
        except Exception as error:
            _LOGGER.debug("Error getting historical average: %s", error)
            return
        return None if True else None

    async def get_production_for_date_range(
        self, start_date: date, end_date: date
    ) -> List[Dict[str, Any]]:
        """Get production data for a date range. @zara"""
        try:
            rows = await self.db.fetchall(
                "SELECT date, predicted_total_kwh, actual_total_kwh,\n                          accuracy_percent, peak_power_w, peak_hour,\n                          evaluation_coverage_percent, excluded_mppt_hours_count\n                   FROM daily_summaries\n                   WHERE date >= ? AND date <= ?\n                   ORDER BY date",
                (start_date.isoformat(), end_date.isoformat()),
            )
            return [
                {
                    "date": row[0],
                    "predicted_kwh": row[1],
                    "actual_kwh": row[2],
                    "accuracy_percent": row[3],
                    "peak_power_w": row[4],
                    "peak_hour": row[5],
                    "evaluation_coverage_percent": row[6],
                    "excluded_mppt_hours_count": row[7],
                }
                for row in rows
            ]
            pass
        except Exception as error:
            _LOGGER.error("Error getting production for date range: %s", error)
            return []

    async def get_seasonal_statistics(self, season: str) -> Dict[str, Any]:
        """Get production statistics for a specific season. @zara"""
        try:
            seasonal_stats = await self.db.get_daily_summary_statistics(season=season)
            if seasonal_stats["yield_days"]:
                return {
                    "season": season,
                    "days_with_data": seasonal_stats["yield_days"],
                    "evaluation_days": seasonal_stats["evaluation_days"],
                    "average_kwh": round(seasonal_stats["avg_yield_kwh"] or 0.0, 2),
                    "max_kwh": round(seasonal_stats["max_yield_kwh"] or 0.0, 2),
                    "min_kwh": round(seasonal_stats["min_yield_kwh"] or 0.0, 2),
                    "average_accuracy": round(seasonal_stats["avg_accuracy"] or 0.0, 1),
                }
            return {
                "season": season,
                "days_with_data": 0,
                "average_kwh": 0.0,
                "max_kwh": 0.0,
                "min_kwh": 0.0,
                "average_accuracy": 0.0,
            }
        except Exception as error:
            _LOGGER.error("Error getting seasonal statistics: %s", error)
            return {
                "season": season,
                "days_with_data": 0,
                "average_kwh": 0.0,
                "max_kwh": 0.0,
                "min_kwh": 0.0,
                "average_accuracy": 0.0,
            }

    async def get_hourly_production_pattern(self, days: int = 30) -> Dict[int, float]:
        """Get average hourly production pattern over specified days. @zara"""
        try:
            start_date = (dt_util.now() - timedelta(days=days)).date()
            rows = await self.db.fetchall(
                "SELECT target_hour, AVG(actual_kwh) as avg_kwh\n                   FROM hourly_predictions\n                   WHERE target_date >= ? AND actual_kwh IS NOT NULL\n                   GROUP BY target_hour\n                   ORDER BY target_hour",
                (start_date.isoformat(),),
            )
            return {row[0]: round(row[1], 3) for row in rows}
            pass
        except Exception as error:
            _LOGGER.error("Error getting hourly production pattern: %s", error)
            return {}

    async def get_weather_correlation(self, days: int = 30) -> Dict[str, Any]:
        """Analyze correlation between weather and production. @zara"""
        try:
            start_date = (dt_util.now() - timedelta(days=days)).date()
            rows = await self.db.fetchall(
                "SELECT\n                       hp.actual_kwh,\n                       pw.clouds,\n                       pw.temperature,\n                       pw.solar_radiation_wm2\n                   FROM hourly_predictions hp\n                   JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                   WHERE hp.target_date >= ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND pw.weather_type = 'actual'",
                (start_date.isoformat(),),
            )
            if not rows:
                return {
                    "samples": 0,
                    "cloud_impact": None,
                    "temp_impact": None,
                    "radiation_correlation": None,
                }
            clear_sky_yields = []
            cloudy_yields = []
            hot_weather_yields = []
            cold_weather_yields = []
            for row in rows:
                actual_kwh = row[0]
                cloud_percent = row[1]
                temperature_c = row[2]
                if cloud_percent is not None:
                    if cloud_percent < 30:
                        clear_sky_yields.append(actual_kwh)
                    elif cloud_percent > 70:
                        cloudy_yields.append(actual_kwh)
                if temperature_c is not None:
                    if temperature_c > 25:
                        hot_weather_yields.append(actual_kwh)
                    elif temperature_c < 15:
                        cold_weather_yields.append(actual_kwh)
            cloud_impact_percent = None
            if clear_sky_yields and cloudy_yields:
                clear_sky_average_kwh = sum(clear_sky_yields) / len(clear_sky_yields)
                cloudy_average_kwh = sum(cloudy_yields) / len(cloudy_yields)
                if clear_sky_average_kwh > 0:
                    cloud_impact_percent = round(
                        (clear_sky_average_kwh - cloudy_average_kwh)
                        / clear_sky_average_kwh
                        * 100,
                        1,
                    )
            temperature_impact_percent = None
            if hot_weather_yields and cold_weather_yields:
                hot_weather_average_kwh = sum(hot_weather_yields) / len(
                    hot_weather_yields
                )
                cold_weather_average_kwh = sum(cold_weather_yields) / len(
                    cold_weather_yields
                )
                if cold_weather_average_kwh > 0:
                    temperature_impact_percent = round(
                        (hot_weather_average_kwh - cold_weather_average_kwh)
                        / cold_weather_average_kwh
                        * 100,
                        1,
                    )
            return {
                "samples": len(rows),
                "cloud_impact": cloud_impact_percent,
                "temp_impact": temperature_impact_percent,
                "clear_sky_samples": len(clear_sky_yields),
                "cloudy_samples": len(cloudy_yields),
            }
        except Exception as error:
            _LOGGER.error("Error analyzing weather correlation: %s", error)
            return {
                "samples": 0,
                "cloud_impact": None,
                "temp_impact": None,
                "radiation_correlation": None,
            }
