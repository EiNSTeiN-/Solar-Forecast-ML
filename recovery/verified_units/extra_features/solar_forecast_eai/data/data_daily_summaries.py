# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Daily Summaries Handler for Solar Forecast Energy AI V16.2.0.
Manages daily summaries using SQLite database.
All JSON operations replaced with database queries.

@zara
"""
import logging
import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager
from .data_shadow_detection import get_performance_analyzer
from ..core.core_helpers import get_season

_LOGGER = logging.getLogger(__name__)


class DailySummariesHandler(DataManagerIO):
    """Manages daily summaries with database backend. @zara

    Handles:
    - Creating daily summaries at 23:40
    - Analyzing time windows (morning, midday, afternoon)
    - Calculating ML metrics
    - Frost analysis
    - Shadow analysis integration
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize daily summaries handler. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for DB operations\n"
        super().__init__(hass, db_manager)
        _LOGGER.debug("DailySummariesHandler initialized with database backend")

    async def create_daily_summary(
        self, date: str, hourly_predictions: List[Dict[str, Any]]
    ) -> bool:
        """Create daily summary from hourly predictions. @zara

        Called at 23:40 to create summary for the day.

        Analyzes:
        - Overall accuracy
        - Time window performance
        - Weather forecast accuracy
        - ML metrics

        Args:
            date: Date string YYYY-MM-DD
            hourly_predictions: List of hourly prediction dicts

        Returns:
            True if successful
        """
        try:
            if not await self.ensure_initialized():
                _LOGGER.error("Cannot create daily summary: database not initialized")
                return False
            day_predictions = [
                prediction
                for prediction in hourly_predictions
                if prediction.get("target_date") == date
            ]
            if not day_predictions:
                _LOGGER.warning("No predictions found for %s", date)
                return False
            overall_stats = self._calculate_overall_stats(day_predictions)
            hourly_stats = self._calculate_hourly_stats(day_predictions)
            ml_metrics = self._calculate_ml_metrics(day_predictions)
            weather_accuracy = self._analyze_weather_accuracy(day_predictions)
            try:
                peak_power_row = await self.db.fetchone(
                    "SELECT peak_power_w, peak_power_time\n                       FROM production_time_state\n                       WHERE id = 1 AND date = ?",
                    (date,),
                )
                if peak_power_row and peak_power_row[0]:
                    overall_stats["peak_power_w"] = float(peak_power_row[0])
                    overall_stats["peak_power_time"] = (
                        str(peak_power_row[1]) if peak_power_row[1] else None
                    )
                else:
                    overall_stats["peak_power_time"] = None
            except Exception:
                overall_stats["peak_power_time"] = None
            summary_date = datetime.fromisoformat(date)
            await self.db.execute(
                "INSERT INTO daily_summaries (\n                    date, day_of_week, day_of_year, month, season, week_of_year,\n                    predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                    error_kwh, error_percent, production_hours, peak_power_w,\n                    peak_power_time, peak_hour, peak_kwh,\n                    total_hours_predicted, hours_with_actual_data,\n                    mean_hourly_accuracy, std_hourly_accuracy,\n                    forecast_accuracy, avg_temperature_diff, avg_cloud_cover_diff,\n                    forecast_dominant, actual_dominant,\n                    ml_mae, ml_rmse, ml_mape, ml_r2_score,\n                    created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(date) DO UPDATE SET\n                    predicted_total_kwh = excluded.predicted_total_kwh,\n                    actual_total_kwh = excluded.actual_total_kwh,\n                    accuracy_percent = excluded.accuracy_percent,\n                    error_kwh = excluded.error_kwh,\n                    error_percent = excluded.error_percent,\n                    production_hours = excluded.production_hours,\n                    peak_power_w = excluded.peak_power_w,\n                    peak_power_time = excluded.peak_power_time,\n                    peak_hour = excluded.peak_hour,\n                    peak_kwh = excluded.peak_kwh,\n                    total_hours_predicted = excluded.total_hours_predicted,\n                    hours_with_actual_data = excluded.hours_with_actual_data,\n                    mean_hourly_accuracy = excluded.mean_hourly_accuracy,\n                    std_hourly_accuracy = excluded.std_hourly_accuracy,\n                    forecast_accuracy = excluded.forecast_accuracy,\n                    avg_temperature_diff = excluded.avg_temperature_diff,\n                    avg_cloud_cover_diff = excluded.avg_cloud_cover_diff,\n                    forecast_dominant = excluded.forecast_dominant,\n                    actual_dominant = excluded.actual_dominant,\n                    ml_mae = excluded.ml_mae,\n                    ml_rmse = excluded.ml_rmse,\n                    ml_mape = excluded.ml_mape,\n                    ml_r2_score = excluded.ml_r2_score,\n                    created_at = excluded.created_at",
                (
                    date,
                    summary_date.weekday(),
                    summary_date.timetuple().tm_yday,
                    summary_date.month,
                    get_season(summary_date.month),
                    summary_date.isocalendar()[1],
                    overall_stats["predicted_total_kwh"],
                    overall_stats["actual_total_kwh"],
                    overall_stats["accuracy_percent"],
                    overall_stats["error_kwh"],
                    overall_stats["error_percent"],
                    overall_stats["production_hours"],
                    overall_stats.get("peak_power_w"),
                    overall_stats.get("peak_power_time"),
                    overall_stats.get("peak_hour"),
                    overall_stats.get("peak_kwh"),
                    hourly_stats["total_hours_predicted"],
                    hourly_stats["hours_with_actual_data"],
                    hourly_stats["mean_hourly_accuracy"],
                    hourly_stats["std_hourly_accuracy"],
                    weather_accuracy.get("forecast_accuracy"),
                    weather_accuracy.get("avg_temperature_diff"),
                    weather_accuracy.get("avg_cloud_cover_diff"),
                    weather_accuracy.get("forecast_dominant"),
                    weather_accuracy.get("actual_dominant"),
                    ml_metrics.get("mae"),
                    ml_metrics.get("rmse"),
                    ml_metrics.get("mape"),
                    ml_metrics.get("r2_score"),
                    dt_util.now().isoformat(),
                ),
            )
            time_windows = self._analyze_time_windows(day_predictions)
            await self._save_time_windows(date, time_windows)
            frost_analysis = await self._analyze_frost_detection(day_predictions)
            await self._save_frost_analysis(date, frost_analysis)
            shadow_analysis = await self._analyze_shadow_detection(date)
            await self._save_shadow_analysis(date, shadow_analysis)
            _LOGGER.info("Created daily summary for %s", date)
            return True
        except Exception as error:
            _LOGGER.error("Failed to create daily summary: %s", error, exc_info=True)
            return False

    async def get_summary(self, date: str) -> Optional[Dict[str, Any]]:
        "Get summary for specific date. @zara\n\nArgs:\n    date: Date string YYYY-MM-DD\n\nReturns:\n    Summary dict or None if not found\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT * FROM daily_summaries WHERE date = ?", (date,)
            )
            if not row:
                return
            summary = dict(row)
            summary["time_windows"] = await self._get_time_windows(date)
            summary["frost_analysis"] = await self._get_frost_analysis(date)
            summary["shadow_analysis"] = await self._get_shadow_analysis(date)
            return summary
        except Exception as error:
            _LOGGER.error("Failed to get summary for %s: %s", date, error)
            return

    async def get_summaries_range(
        self, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        "Get summaries for a date range. @zara\n\nArgs:\n    start_date: Start date YYYY-MM-DD\n    end_date: End date YYYY-MM-DD\n\nReturns:\n    List of summary dicts\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT * FROM daily_summaries\n                   WHERE date BETWEEN ? AND ?\n                   ORDER BY date DESC",
                (start_date, end_date),
            )
            return [dict(row) for row in rows]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get summaries range: %s", error)
            return []

    async def get_recent_summaries(self, days: int = 30) -> List[Dict[str, Any]]:
        "Get most recent summaries. @zara\n\nArgs:\n    days: Number of days to retrieve\n\nReturns:\n    List of summary dicts\n"
        try:
            await self.ensure_initialized()
            cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT * FROM daily_summaries\n                   WHERE date >= ?\n                   ORDER BY date DESC",
                (cutoff_date,),
            )
            return [dict(row) for row in rows]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get recent summaries: %s", error)
            return []

    async def get_monthly_statistics(self, year: int, month: int) -> Dict[str, Any]:
        "Get aggregated statistics for a month. @zara\n\nArgs:\n    year: Year (e.g., 2026)\n    month: Month (1-12)\n\nReturns:\n    Dict with monthly statistics\n"
        try:
            await self.ensure_initialized()
            month_prefix = f"{year}-{month:02d}"
            rows = await self.db.fetchall(
                "SELECT\n                    COUNT(*) as days,\n                    SUM(actual_total_kwh) as total_kwh,\n                    AVG(actual_total_kwh) as avg_kwh,\n                    AVG(accuracy_percent) as avg_accuracy,\n                    MAX(actual_total_kwh) as best_day_kwh,\n                    MIN(actual_total_kwh) as worst_day_kwh\n                   FROM daily_summaries\n                   WHERE date LIKE ?",
                (f"{month_prefix}%",),
            )
            row = rows[0] if rows else None
            if not row or row[0] == 0:
                return {
                    "year": year,
                    "month": month,
                    "days": 0,
                    "total_kwh": 0.0,
                    "avg_kwh": 0.0,
                    "avg_accuracy": 0.0,
                }
            return {
                "year": year,
                "month": month,
                "days": row[0],
                "total_kwh": round(row[1] or 0, 2),
                "avg_kwh": round(row[2] or 0, 2),
                "avg_accuracy": round(row[3] or 0, 1),
                "best_day_kwh": round(row[4] or 0, 2),
                "worst_day_kwh": round(row[5] or 0, 2),
            }
        except Exception as error:
            _LOGGER.error("Failed to get monthly statistics: %s", error)
            return {}

    async def get_seasonal_statistics(self, season: str) -> Dict[str, Any]:
        "Get aggregated statistics for a season. @zara\n\nArgs:\n    season: Season name (spring, summer, autumn, winter)\n\nReturns:\n    Dict with seasonal statistics\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT\n                    COUNT(*) as days,\n                    SUM(actual_total_kwh) as total_kwh,\n                    AVG(actual_total_kwh) as avg_kwh,\n                    AVG(accuracy_percent) as avg_accuracy\n                   FROM daily_summaries\n                   WHERE season = ?",
                (season,),
            )
            row = rows[0] if rows else None
            if not row or row[0] == 0:
                return {
                    "season": season,
                    "days": 0,
                    "total_kwh": 0.0,
                    "avg_kwh": 0.0,
                    "avg_accuracy": 0.0,
                }
            return {
                "season": season,
                "days": row[0],
                "total_kwh": round(row[1] or 0, 2),
                "avg_kwh": round(row[2] or 0, 2),
                "avg_accuracy": round(row[3] or 0, 1),
            }
        except Exception as error:
            _LOGGER.error("Failed to get seasonal statistics: %s", error)
            return {}

    async def cleanup_old_summaries(self, days_to_keep: int = 365) -> int:
        "Remove summaries older than specified days. @zara\n\nArgs:\n    days_to_keep: Number of days of data to retain\n\nReturns:\n    Number of deleted records\n"
        try:
            await self.ensure_initialized()
            cutoff_date = (
                (dt_util.now() - timedelta(days=days_to_keep)).date().isoformat()
            )
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM daily_summaries WHERE date < ?", (cutoff_date,)
            )
            deleted_count = row[0] if row else 0
            if deleted_count > 0:
                await self.db.execute(
                    "DELETE FROM daily_summaries WHERE date < ?", (cutoff_date,)
                )
                _LOGGER.info("Cleaned up %d old summaries", deleted_count)
            return deleted_count
        except Exception as error:
            _LOGGER.error("Failed to cleanup old summaries: %s", error)
            return 0

    async def _save_time_windows(
        self, date: str, time_windows: Dict[str, Dict[str, Any]]
    ) -> None:
        """Save time window analysis to database. @zara"""
        for window_name, window_stats in time_windows.items():
            await self.db.execute(
                "INSERT INTO daily_summary_time_windows (\n                    date, window_name, predicted_kwh, actual_kwh,\n                    accuracy, stable, hours_count\n                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(date, window_name) DO UPDATE SET\n                    predicted_kwh = excluded.predicted_kwh,\n                    actual_kwh = excluded.actual_kwh,\n                    accuracy = excluded.accuracy,\n                    stable = excluded.stable,\n                    hours_count = excluded.hours_count",
                (
                    date,
                    window_name,
                    window_stats.get("predicted_kwh"),
                    window_stats.get("actual_kwh"),
                    window_stats.get("accuracy"),
                    window_stats.get("stable"),
                    window_stats.get("hours_count"),
                ),
            )

    async def _get_time_windows(self, date: str) -> Dict[str, Dict[str, Any]]:
        """Get time windows for a date. @zara"""
        rows = await self.db.fetchall(
            "SELECT window_name, predicted_kwh, actual_kwh,\n                      accuracy, stable, hours_count\n               FROM daily_summary_time_windows WHERE date = ?",
            (date,),
        )
        time_windows_by_name = {}
        for row in rows:
            time_windows_by_name[row[0]] = {
                "predicted_kwh": row[1],
                "actual_kwh": row[2],
                "accuracy": row[3],
                "stable": bool(row[4]),
                "hours_count": row[5],
            }
        return time_windows_by_name

    async def _save_frost_analysis(
        self, date: str, frost_analysis: Dict[str, Any]
    ) -> None:
        """Save frost analysis to database. @zara"""
        await self.db.execute(
            "INSERT INTO daily_summary_frost_analysis (\n                date, hours_analyzed, frost_detected, total_affected_hours,\n                heavy_frost_hours, light_frost_hours\n            ) VALUES (?, ?, ?, ?, ?, ?)\n            ON CONFLICT(date) DO UPDATE SET\n                hours_analyzed = excluded.hours_analyzed,\n                frost_detected = excluded.frost_detected,\n                total_affected_hours = excluded.total_affected_hours,\n                heavy_frost_hours = excluded.heavy_frost_hours,\n                light_frost_hours = excluded.light_frost_hours",
            (
                date,
                frost_analysis.get("hours_analyzed", 0),
                frost_analysis.get("frost_detected", False),
                frost_analysis.get("total_affected_hours", 0),
                frost_analysis.get("heavy_frost_hours", 0),
                frost_analysis.get("light_frost_hours", 0),
            ),
        )

    async def _get_frost_analysis(self, date: str) -> Optional[Dict[str, Any]]:
        """Get frost analysis for a date. @zara"""
        row = await self.db.fetchone(
            "SELECT hours_analyzed, frost_detected, total_affected_hours,\n                      heavy_frost_hours, light_frost_hours\n               FROM daily_summary_frost_analysis WHERE date = ?",
            (date,),
        )
        if not row:
            return
        return {
            "hours_analyzed": row[0],
            "frost_detected": bool(row[1]),
            "total_affected_hours": row[2],
            "heavy_frost_hours": row[3],
            "light_frost_hours": row[4],
        }

    async def _analyze_shadow_detection(self, date: str) -> Dict[str, Any]:
        """Analyze shadow detection for the day using PerformanceLossAnalyzer. @zara"""
        try:
            performance_analyzer = get_performance_analyzer(self.hass, self.db)
            shadow_analysis = await performance_analyzer.analyze_daily_shadow(date)
            return {
                "shadow_hours_count": shadow_analysis.get("shadow_hours_count", 0),
                "cumulative_loss_kwh": shadow_analysis.get("cumulative_loss_kwh", 0.0),
            }
        except Exception as error:
            _LOGGER.error("Shadow detection analysis failed for %s: %s", date, error)
            return {"shadow_hours_count": 0, "cumulative_loss_kwh": 0.0}

    async def _save_shadow_analysis(
        self, date: str, shadow_analysis: Dict[str, Any]
    ) -> None:
        """Save shadow analysis to database. @zara"""
        await self.db.execute(
            "INSERT INTO daily_summary_shadow_analysis (\n                date, shadow_hours_count, cumulative_loss_kwh\n            ) VALUES (?, ?, ?)\n            ON CONFLICT(date) DO UPDATE SET\n                shadow_hours_count = excluded.shadow_hours_count,\n                cumulative_loss_kwh = excluded.cumulative_loss_kwh",
            (
                date,
                shadow_analysis.get("shadow_hours_count", 0),
                shadow_analysis.get("cumulative_loss_kwh", 0.0),
            ),
        )

    async def _get_shadow_analysis(self, date: str) -> Optional[Dict[str, Any]]:
        """Get shadow analysis for a date. @zara"""
        row = await self.db.fetchone(
            "SELECT shadow_hours_count, cumulative_loss_kwh\n               FROM daily_summary_shadow_analysis WHERE date = ?",
            (date,),
        )
        if not row:
            return
        return {"shadow_hours_count": row[0], "cumulative_loss_kwh": row[1]}

    def _calculate_overall_stats(self, predictions: List[Dict]) -> Dict:
        """Calculate overall day statistics. @zara"""
        predicted_total_kwh = sum(
            (prediction.get("prediction_kwh", 0) for prediction in predictions)
        )
        actual_total_kwh = sum(
            (
                prediction.get("actual_kwh", 0)
                for prediction in predictions
                if prediction.get("actual_kwh") is not None
            )
        )
        if actual_total_kwh > 0.01 and predicted_total_kwh > 0:
            absolute_error_kwh = abs(predicted_total_kwh - actual_total_kwh)
            accuracy_percent = max(
                0.0, (1.0 - absolute_error_kwh / actual_total_kwh) * 100.0
            )
        else:
            accuracy_percent = 0.0
        signed_error_kwh = actual_total_kwh - predicted_total_kwh
        peak_prediction = max(predictions, key=lambda x: x.get("prediction_kwh", 0))
        production_hours = len(
            [
                prediction
                for prediction in predictions
                if prediction.get("actual_kwh") is not None
                if prediction.get("actual_kwh") > 0
            ]
        )
        return {
            "predicted_total_kwh": round(predicted_total_kwh, 2),
            "actual_total_kwh": round(actual_total_kwh, 2),
            "accuracy_percent": round(accuracy_percent, 1),
            "error_kwh": round(signed_error_kwh, 2),
            "error_percent": round(
                (
                    signed_error_kwh / predicted_total_kwh * 100
                    if predicted_total_kwh > 0
                    else 0
                ),
                1,
            ),
            "production_hours": production_hours,
            "peak_power_w": None,
            "peak_hour": peak_prediction.get("target_hour"),
            "peak_kwh": round(peak_prediction.get("prediction_kwh", 0), 3),
        }

    def _calculate_hourly_stats(self, predictions: List[Dict]) -> Dict:
        """Calculate hourly statistics. @zara"""
        hourly_accuracies = [
            prediction.get("accuracy_percent", 0)
            for prediction in predictions
            if prediction.get("accuracy_percent") is not None
        ]
        return (
            {
                "total_hours_predicted": len(predictions),
                "hours_with_actual_data": 0,
                "mean_hourly_accuracy": 0,
                "std_hourly_accuracy": 0,
            }
            if not hourly_accuracies
            else {
                "total_hours_predicted": len(predictions),
                "hours_with_actual_data": len(hourly_accuracies),
                "mean_hourly_accuracy": round(statistics.mean(hourly_accuracies), 1),
                "std_hourly_accuracy": (
                    round(statistics.stdev(hourly_accuracies), 1)
                    if len(hourly_accuracies) > 1
                    else 0
                ),
            }
        )

    def _analyze_time_windows(self, predictions: List[Dict]) -> Dict:
        """Analyze different time windows. @zara"""
        window_definitions = {
            "morning_7_10": [7, 8, 9, 10],
            "midday_11_14": [11, 12, 13, 14],
            "afternoon_15_17": [15, 16, 17],
        }
        window_analysis = {}
        for window_name, window_hours in window_definitions.items():
            window_predictions = [
                prediction
                for prediction in predictions
                if prediction.get("target_hour") in window_hours
            ]
            if not window_predictions:
                continue
            window_predicted_kwh = sum(
                (
                    prediction.get("prediction_kwh", 0)
                    for prediction in window_predictions
                )
            )
            window_actual_kwh = sum(
                (
                    prediction.get("actual_kwh", 0)
                    for prediction in window_predictions
                    if prediction.get("actual_kwh")
                )
            )
            accuracy_percent = (
                window_actual_kwh / window_predicted_kwh * 100
                if window_predicted_kwh > 0
                else 0
            )
            hourly_accuracies = [
                prediction.get("accuracy_percent")
                for prediction in window_predictions
                if prediction.get("accuracy_percent")
            ]
            accuracy_stddev = (
                statistics.stdev(hourly_accuracies) if len(hourly_accuracies) > 1 else 0
            )
            window_analysis[window_name] = {
                "predicted_kwh": round(window_predicted_kwh, 2),
                "actual_kwh": round(window_actual_kwh, 2),
                "accuracy": round(accuracy_percent, 1),
                "stable": accuracy_stddev < 10,
                "hours_count": len(window_predictions),
            }
            continue
        return window_analysis

    def _analyze_weather_accuracy(self, predictions: List[Dict]) -> Dict:
        """Analyze weather forecast accuracy. @zara"""
        temperature_differences = []
        cloud_cover_differences = []
        for prediction in predictions:
            forecast_weather = prediction.get("weather_forecast") or {}
            actual_weather = prediction.get("weather_actual") or {}
            if not forecast_weather:
                continue
            if not actual_weather:
                continue
            forecast_temperature_c = forecast_weather.get(
                "temperature"
            ) or forecast_weather.get("temperature_c")
            actual_temperature_c = actual_weather.get(
                "temperature"
            ) or actual_weather.get("temperature_c")
            if forecast_temperature_c is not None and actual_temperature_c is not None:
                temperature_differences.append(
                    abs(actual_temperature_c - forecast_temperature_c)
                )
            forecast_cloud_cover_percent = forecast_weather.get(
                "clouds"
            ) or forecast_weather.get("cloud_cover_percent")
            actual_cloud_cover_percent = actual_weather.get(
                "clouds"
            ) or actual_weather.get("cloud_cover_percent")
            if forecast_cloud_cover_percent is None:
                continue
            if actual_cloud_cover_percent is None:
                continue
            cloud_cover_differences.append(
                abs(actual_cloud_cover_percent - forecast_cloud_cover_percent)
            )
            continue
        mean_temperature_difference = (
            statistics.mean(temperature_differences)
            if temperature_differences
            else None
        )
        mean_cloud_cover_difference = (
            statistics.mean(cloud_cover_differences)
            if cloud_cover_differences
            else None
        )
        accuracy_percent = None
        if (
            mean_temperature_difference is not None
            and mean_cloud_cover_difference is not None
        ):
            temperature_accuracy = max(0.0, 1.0 - mean_temperature_difference / 10.0)
            cloud_cover_accuracy = max(0.0, 1.0 - mean_cloud_cover_difference / 100.0)
            accuracy_percent = round(
                (temperature_accuracy * 0.4 + cloud_cover_accuracy * 0.6) * 100, 1
            )
        return {
            "forecast_accuracy": accuracy_percent,
            "avg_temperature_diff": (
                round(mean_temperature_difference, 1)
                if mean_temperature_difference is not None
                else None
            ),
            "avg_cloud_cover_diff": (
                round(mean_cloud_cover_difference, 1)
                if mean_cloud_cover_difference is not None
                else None
            ),
            "forecast_dominant": None,
            "actual_dominant": None,
        }

    def _calculate_ml_metrics(self, predictions: List[Dict]) -> Dict:
        """Calculate ML performance metrics. @zara"""
        prediction_actual_pairs = [
            (prediction.get("prediction_kwh", 0), prediction.get("actual_kwh", 0))
            for prediction in predictions
            if prediction.get("actual_kwh") is not None
            if prediction.get("prediction_kwh") is not None
        ]
        if not prediction_actual_pairs:
            return (({} if True else None) if True else None) if True else None
        residuals_kwh = [
            predicted_kwh - actual_kwh
            for predicted_kwh, actual_kwh in prediction_actual_pairs
        ]
        actual_values_kwh = [
            actual_kwh for unused_predicted_kwh, actual_kwh in prediction_actual_pairs
        ]
        mean_absolute_error = statistics.mean(
            [abs(residual_kwh) for residual_kwh in residuals_kwh]
        )
        root_mean_squared_error = math.sqrt(
            statistics.mean([residual_kwh**2 for residual_kwh in residuals_kwh])
        )
        percentage_errors = [
            abs(predicted_kwh - actual_kwh) / actual_kwh * 100
            for predicted_kwh, actual_kwh in prediction_actual_pairs
            if actual_kwh > 0.01
        ]
        mean_absolute_percentage_error = (
            statistics.mean(percentage_errors) if percentage_errors else None
        )
        mean_actual_kwh = statistics.mean(actual_values_kwh) if actual_values_kwh else 0
        residual_sum_squares = sum(
            (
                (predicted_kwh - actual_kwh) ** 2
                for predicted_kwh, actual_kwh in prediction_actual_pairs
            )
        )
        total_sum_squares = sum(
            (
                (actual_kwh - mean_actual_kwh) ** 2
                for unused_predicted_kwh, actual_kwh in prediction_actual_pairs
            )
        )
        r2_score = (
            1.0 - residual_sum_squares / total_sum_squares
            if total_sum_squares > 0
            else None
        )
        return (
            (
                (
                    {
                        "mae": round(mean_absolute_error, 3),
                        "rmse": round(root_mean_squared_error, 3),
                        "mape": (
                            round(mean_absolute_percentage_error, 1)
                            if mean_absolute_percentage_error is not None
                            else None
                        ),
                        "r2_score": (
                            round(r2_score, 4) if r2_score is not None else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    async def _analyze_frost_detection(
        self, predictions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Analyze frost detection for the day. @zara"""
        affected_hours = 0
        heavy_frost_hours = 0
        light_frost_hours = 0
        for prediction in predictions:
            actual_weather = prediction.get("weather_actual") or {}
            frost_condition = actual_weather.get("frost_detected")
            if frost_condition:
                affected_hours += 1
                if frost_condition == "heavy_frost":
                    heavy_frost_hours += 1
                elif frost_condition == "light_frost":
                    light_frost_hours += 1
        return {
            "hours_analyzed": len(predictions),
            "frost_detected": affected_hours > 0,
            "total_affected_hours": affected_hours,
            "heavy_frost_hours": heavy_frost_hours,
            "light_frost_hours": light_frost_hours,
        }

    async def _save_daily_forecast_history(
        self,
        date: str,
        predictions: List[Dict[str, Any]],
        overall: Dict[str, Any],
        hourly_stats: Dict[str, Any],
    ) -> None:
        """Save daily forecast to history archive. @zara

        Creates a history entry with forecast performance for the day.
        This is called at end of day (23:40) to archive the day's results.
        """
        try:
            prediction_source = "ML"
            if predictions:
                prediction_source = predictions[0].get("source", "ML")
            prediction_count = len(predictions)
            actual_data_hours = hourly_stats.get("hours_with_actual_data", 0)
            excluded_hours = prediction_count - actual_data_hours
            excluded_ratio = (
                round(excluded_hours / prediction_count, 2)
                if prediction_count > 0
                else 0
            )
            exclusion_reasons = []
            for prediction in predictions:
                if prediction.get("actual_kwh") is None:
                    exclusion_reason = prediction.get("exclusion_reason", "no_data")
                    if exclusion_reason not in exclusion_reasons:
                        exclusion_reasons.append(exclusion_reason)
            saved_today_forecast = await self._get_today_forecast(date)
            archived_prediction_kwh = (
                saved_today_forecast.get("prediction_kwh", 0)
                if saved_today_forecast
                else overall.get("predicted_total_kwh", 0)
            )
            history_entry = {
                "date": date,
                "predicted_kwh": archived_prediction_kwh,
                "actual_kwh": overall.get("actual_total_kwh", 0),
                "consumption_kwh": None,
                "autarky": None,
                "accuracy": overall.get("accuracy_percent", 0),
                "production_hours": overall.get("production_hours", 0),
                "peak_power": overall.get("peak_power_w"),
                "source": prediction_source,
                "excluded_hours": {
                    "count": excluded_hours,
                    "total": prediction_count,
                    "ratio": excluded_ratio,
                    "reasons": exclusion_reasons,
                },
                "archived_at": dt_util.now().isoformat(),
            }
            await self.db.save_forecast_history_entry(history_entry)
            _LOGGER.debug("Saved forecast history entry for %s", date)
        except Exception as error:
            _LOGGER.error(
                "Failed to save forecast history for %s: %s", date, error, exc_info=True
            )
        return None if True else None

    async def _get_today_forecast(self, date: str) -> Optional[Dict[str, Any]]:
        """Get today's forecast from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT prediction_kwh, source\n                   FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
            if row:
                return {"prediction_kwh": row[0], "source": row[1]}
        except Exception as error:
            _LOGGER.error("Failed to get today's forecast: %s", error)
            return

    async def update_daily_statistics(
        self,
        all_time_peak: Optional[Dict[str, Any]] = None,
        current_week: Optional[Dict[str, Any]] = None,
        current_month: Optional[Dict[str, Any]] = None,
        last_7_days: Optional[Dict[str, Any]] = None,
        last_30_days: Optional[Dict[str, Any]] = None,
        last_365_days: Optional[Dict[str, Any]] = None,
    ) -> bool:
        'Update daily statistics. @zara\n\nThis method updates rolling statistics that are calculated\nperiodically (e.g., weekly, monthly aggregates).\n\nArgs:\n    all_time_peak: Dict with power_w, date, at\n    current_week: Dict with period, date_range, yield_kwh,\n                 consumption_kwh, days, updated_at\n    current_month: Dict with period, yield_kwh, consumption_kwh,\n                  avg_autarky, days, updated_at\n    last_7_days: Dict with avg_yield_kwh, avg_accuracy,\n                total_yield_kwh, calculated_at\n    last_30_days: Dict with avg_yield_kwh, avg_accuracy,\n                 total_yield_kwh, calculated_at\n    last_365_days: Dict with avg_yield_kwh, total_yield_kwh, calculated_at\n\nReturns:\n    True if successful\n\nExample:\n    await handler.update_daily_statistics(\n        all_time_peak={"power_w": 4200, "date": "2025-06-15", "at": "..."},\n        current_week={"period": "KW 05", "yield_kwh": 45.2, ...}\n    )\n'
        try:
            await self.ensure_initialized()
            daily_statistics = await self.db.get_daily_statistics()
            if not daily_statistics:
                daily_statistics = {}
            if all_time_peak is not None:
                daily_statistics["all_time_peak"] = all_time_peak
            if current_week is not None:
                daily_statistics["current_week"] = current_week
            if current_month is not None:
                daily_statistics["current_month"] = current_month
            if last_7_days is not None:
                daily_statistics["last_7_days"] = last_7_days
            if last_30_days is not None:
                daily_statistics["last_30_days"] = last_30_days
            if last_365_days is not None:
                daily_statistics["last_365_days"] = last_365_days
            await self.db.save_daily_statistics(daily_statistics)
            _LOGGER.debug("Daily statistics updated")
        except Exception as error:
            _LOGGER.error("Failed to update daily statistics: %s", error, exc_info=True)
            return False
        return True

    async def calculate_and_save_statistics(self) -> bool:
        "Calculate and save current statistics. @zara\n\nThis method calculates all rolling statistics from database\nand saves them using save_daily_statistics().\n\nShould be called periodically (e.g., daily at midnight or hourly).\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            seven_day_stats = await self._calculate_period_stats(7)
            thirty_day_stats = await self._calculate_period_stats(30)
            annual_stats = await self._calculate_period_stats(365)
            current_week_stats = await self._calculate_week_stats()
            current_month_stats = await self._calculate_month_stats()
            all_time_peak = await self._get_all_time_peak()
            return await self.update_daily_statistics(
                all_time_peak=all_time_peak,
                current_week=current_week_stats,
                current_month=current_month_stats,
                last_7_days=seven_day_stats,
                last_30_days=thirty_day_stats,
                last_365_days=annual_stats,
            )
        except Exception as error:
            _LOGGER.error("Failed to calculate and save statistics: %s", error)
            return False

    async def _calculate_period_stats(self, days: int) -> Dict[str, Any]:
        """Calculate statistics for a period. @zara"""
        try:
            cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT AVG(actual_total_kwh), AVG(accuracy_percent),\n                          SUM(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (cutoff_date,),
            )
            if rows and rows[0][0] is not None:
                return {
                    "avg_yield_kwh": round(rows[0][0], 2),
                    "avg_accuracy": round(rows[0][1], 1),
                    "total_yield_kwh": round(rows[0][2], 2),
                    "calculated_at": dt_util.now().isoformat(),
                }
            return {
                "avg_yield_kwh": 0,
                "avg_accuracy": 0,
                "total_yield_kwh": 0,
                "calculated_at": dt_util.now().isoformat(),
            }
        except Exception as error:
            _LOGGER.error("Failed to calculate period stats: %s", error)
            return {}

    async def _calculate_week_stats(self) -> Dict[str, Any]:
        """Calculate current week statistics. @zara"""
        try:
            now = dt_util.now()
            week_start_date = (now - timedelta(days=now.weekday())).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT SUM(actual_total_kwh), COUNT(*)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (week_start_date,),
            )
            if rows and rows[0][0] is not None:
                return {
                    "period": f"KW {now.isocalendar()[1]}",
                    "date_range": f"{week_start_date} - {now.date().isoformat()}",
                    "yield_kwh": round(rows[0][0], 2),
                    "consumption_kwh": None,
                    "days": rows[0][1],
                    "updated_at": now.isoformat(),
                }
            return {}
        except Exception as error:
            _LOGGER.error("Failed to calculate week stats: %s", error)
            return {}

    async def _calculate_month_stats(self) -> Dict[str, Any]:
        """Calculate current month statistics. @zara"""
        try:
            now = dt_util.now()
            month_start_date = now.replace(day=1).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT SUM(actual_total_kwh), COUNT(*)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (month_start_date,),
            )
            if rows and rows[0][0] is not None:
                return {
                    "period": now.strftime("%Y-%m"),
                    "yield_kwh": round(rows[0][0], 2),
                    "consumption_kwh": None,
                    "avg_autarky": None,
                    "days": rows[0][1],
                    "updated_at": now.isoformat(),
                }
            return {}
        except Exception as error:
            _LOGGER.error("Failed to calculate month stats: %s", error)
            return {}

    async def _get_all_time_peak(self) -> Dict[str, Any]:
        """Get all-time peak power. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT date, peak_power_w\n                   FROM daily_summaries\n                   WHERE peak_power_w IS NOT NULL\n                   ORDER BY peak_power_w DESC\n                   LIMIT 1"
            )
            if row and row[1] is not None:
                return {"power_w": row[1], "date": row[0], "at": None}
            return {}
        except Exception as error:
            _LOGGER.error("Failed to get all-time peak: %s", error)
            return {}
