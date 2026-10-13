# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Daily Summaries Handler for Solar Forecast ML V16.2.0.
Manages daily summaries using SQLite database.
All JSON operations replaced with database queries.

@zara
"""
import logging
import math
import statistics
from datetime import date, datetime, timedelta
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
    - Creating daily summaries at 23:30
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

        Called at 23:30 to create summary for the day.

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
                peak_row = await self.db.fetchone(
                    "SELECT peak_power_w, peak_power_time\n                       FROM production_time_state\n                       WHERE id = 1 AND date = ?",
                    (date,),
                )
                if peak_row and peak_row[0]:
                    overall_stats["peak_power_w"] = float(peak_row[0])
                    overall_stats["peak_power_time"] = (
                        str(peak_row[1]) if peak_row[1] else None
                    )
                else:
                    overall_stats["peak_power_time"] = None
            except Exception:
                overall_stats["peak_power_time"] = None
            day = datetime.fromisoformat(date)
            await self.db.execute(
                "INSERT INTO daily_summaries (\n                    date, day_of_week, day_of_year, month, season, week_of_year,\n                    predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                    evaluation_predicted_kwh, evaluation_actual_kwh, evaluation_coverage_percent,\n                    excluded_hours_count, excluded_mppt_hours_count,\n                    evaluation_hours_count, production_candidate_hours_count,\n                    missing_actual_hours_count, excluded_weather_alert_hours_count,\n                    excluded_inverter_clipped_hours_count, excluded_reason_breakdown_json,\n                    error_kwh, error_percent, production_hours, peak_power_w,\n                    peak_power_time, peak_hour, peak_kwh,\n                    total_hours_predicted, hours_with_actual_data,\n                    mean_hourly_accuracy, std_hourly_accuracy,\n                    forecast_accuracy, avg_temperature_diff, avg_cloud_cover_diff,\n                    forecast_dominant, actual_dominant,\n                    ml_mae, ml_rmse, ml_mape, ml_r2_score,\n                    created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(date) DO UPDATE SET\n                    predicted_total_kwh = excluded.predicted_total_kwh,\n                    actual_total_kwh = excluded.actual_total_kwh,\n                    accuracy_percent = excluded.accuracy_percent,\n                    evaluation_predicted_kwh = excluded.evaluation_predicted_kwh,\n                    evaluation_actual_kwh = excluded.evaluation_actual_kwh,\n                    evaluation_coverage_percent = excluded.evaluation_coverage_percent,\n                    excluded_hours_count = excluded.excluded_hours_count,\n                    excluded_mppt_hours_count = excluded.excluded_mppt_hours_count,\n                    evaluation_hours_count = excluded.evaluation_hours_count,\n                    production_candidate_hours_count = excluded.production_candidate_hours_count,\n                    missing_actual_hours_count = excluded.missing_actual_hours_count,\n                    excluded_weather_alert_hours_count = excluded.excluded_weather_alert_hours_count,\n                    excluded_inverter_clipped_hours_count = excluded.excluded_inverter_clipped_hours_count,\n                    excluded_reason_breakdown_json = excluded.excluded_reason_breakdown_json,\n                    error_kwh = excluded.error_kwh,\n                    error_percent = excluded.error_percent,\n                    production_hours = excluded.production_hours,\n                    peak_power_w = excluded.peak_power_w,\n                    peak_power_time = excluded.peak_power_time,\n                    peak_hour = excluded.peak_hour,\n                    peak_kwh = excluded.peak_kwh,\n                    total_hours_predicted = excluded.total_hours_predicted,\n                    hours_with_actual_data = excluded.hours_with_actual_data,\n                    mean_hourly_accuracy = excluded.mean_hourly_accuracy,\n                    std_hourly_accuracy = excluded.std_hourly_accuracy,\n                    forecast_accuracy = excluded.forecast_accuracy,\n                    avg_temperature_diff = excluded.avg_temperature_diff,\n                    avg_cloud_cover_diff = excluded.avg_cloud_cover_diff,\n                    forecast_dominant = excluded.forecast_dominant,\n                    actual_dominant = excluded.actual_dominant,\n                    ml_mae = excluded.ml_mae,\n                    ml_rmse = excluded.ml_rmse,\n                    ml_mape = excluded.ml_mape,\n                    ml_r2_score = excluded.ml_r2_score,\n                    created_at = excluded.created_at",
                (
                    date,
                    day.weekday(),
                    day.timetuple().tm_yday,
                    day.month,
                    get_season(day.month),
                    day.isocalendar()[1],
                    overall_stats["predicted_total_kwh"],
                    overall_stats["actual_total_kwh"],
                    overall_stats["accuracy_percent"],
                    overall_stats["evaluation_predicted_kwh"],
                    overall_stats["evaluation_actual_kwh"],
                    overall_stats["evaluation_coverage_percent"],
                    overall_stats["excluded_hours_count"],
                    overall_stats["excluded_mppt_hours_count"],
                    overall_stats["evaluation_hours_count"],
                    overall_stats["production_candidate_hours_count"],
                    overall_stats["missing_actual_hours_count"],
                    overall_stats["excluded_weather_alert_hours_count"],
                    overall_stats["excluded_inverter_clipped_hours_count"],
                    overall_stats["excluded_reason_breakdown_json"],
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
            month_start = date(year, month, 1)
            next_month_start = (
                date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            )
            aggregate_stats = await self.db.get_daily_summary_statistics(
                start_date=month_start.isoformat(),
                end_date=next_month_start.isoformat(),
            )
            if not aggregate_stats["yield_days"]:
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
                "days": aggregate_stats["yield_days"],
                "evaluation_days": aggregate_stats["evaluation_days"],
                "total_kwh": round(aggregate_stats["total_yield_kwh"] or 0, 2),
                "avg_kwh": round(aggregate_stats["avg_yield_kwh"] or 0, 2),
                "avg_accuracy": round(aggregate_stats["avg_accuracy"] or 0, 1),
                "best_day_kwh": round(aggregate_stats["max_yield_kwh"] or 0, 2),
                "worst_day_kwh": round(aggregate_stats["min_yield_kwh"] or 0, 2),
            }
        except Exception as error:
            _LOGGER.error("Failed to get monthly statistics: %s", error)
            return {}

    async def get_seasonal_statistics(self, season: str) -> Dict[str, Any]:
        "Get aggregated statistics for a season. @zara\n\nArgs:\n    season: Season name (spring, summer, autumn, winter)\n\nReturns:\n    Dict with seasonal statistics\n"
        try:
            await self.ensure_initialized()
            aggregate_stats = await self.db.get_daily_summary_statistics(season=season)
            if not aggregate_stats["yield_days"]:
                return {
                    "season": season,
                    "days": 0,
                    "total_kwh": 0.0,
                    "avg_kwh": 0.0,
                    "avg_accuracy": 0.0,
                }
            return {
                "season": season,
                "days": aggregate_stats["yield_days"],
                "evaluation_days": aggregate_stats["evaluation_days"],
                "total_kwh": round(aggregate_stats["total_yield_kwh"] or 0, 2),
                "avg_kwh": round(aggregate_stats["avg_yield_kwh"] or 0, 2),
                "avg_accuracy": round(aggregate_stats["avg_accuracy"] or 0, 1),
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
        for window_name, window_data in time_windows.items():
            await self.db.execute(
                "INSERT INTO daily_summary_time_windows (\n                    date, window_name, predicted_kwh, actual_kwh,\n                    accuracy, stable, hours_count\n                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(date, window_name) DO UPDATE SET\n                    predicted_kwh = excluded.predicted_kwh,\n                    actual_kwh = excluded.actual_kwh,\n                    accuracy = excluded.accuracy,\n                    stable = excluded.stable,\n                    hours_count = excluded.hours_count",
                (
                    date,
                    window_name,
                    window_data.get("predicted_kwh"),
                    window_data.get("actual_kwh"),
                    window_data.get("accuracy"),
                    window_data.get("stable"),
                    window_data.get("hours_count"),
                ),
            )

    async def _get_time_windows(self, date: str) -> Dict[str, Dict[str, Any]]:
        """Get time windows for a date. @zara"""
        rows = await self.db.fetchall(
            "SELECT window_name, predicted_kwh, actual_kwh,\n                      accuracy, stable, hours_count\n               FROM daily_summary_time_windows WHERE date = ?",
            (date,),
        )
        windows = {}
        for row in rows:
            windows[row[0]] = {
                "predicted_kwh": row[1],
                "actual_kwh": row[2],
                "accuracy": row[3],
                "stable": bool(row[4]),
                "hours_count": row[5],
            }
        return windows

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
            analyzer = get_performance_analyzer(self.hass, self.db)
            shadow_result = await analyzer.analyze_daily_shadow(date)
            return {
                "shadow_hours_count": shadow_result.get("shadow_hours_count", 0),
                "cumulative_loss_kwh": shadow_result.get("cumulative_loss_kwh", 0.0),
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
        predicted_total = sum(
            (prediction.get("prediction_kwh", 0) for prediction in predictions)
        )
        actual_total = sum(
            (
                prediction.get("actual_kwh", 0)
                for prediction in predictions
                if prediction.get("actual_kwh") is not None
            )
        )
        clean_evaluation = DatabaseManager.calculate_daily_clean_evaluation(predictions)
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
            "predicted_total_kwh": round(predicted_total, 2),
            "actual_total_kwh": round(actual_total, 2),
            "accuracy_percent": clean_evaluation["accuracy_percent"],
            "evaluation_predicted_kwh": clean_evaluation["evaluation_predicted_kwh"],
            "evaluation_actual_kwh": clean_evaluation["evaluation_actual_kwh"],
            "evaluation_coverage_percent": clean_evaluation[
                "evaluation_coverage_percent"
            ],
            "evaluation_hours_count": clean_evaluation["evaluation_hours_count"],
            "production_candidate_hours_count": clean_evaluation[
                "production_candidate_hours_count"
            ],
            "missing_actual_hours_count": clean_evaluation[
                "missing_actual_hours_count"
            ],
            "excluded_hours_count": clean_evaluation["excluded_hours_count"],
            "excluded_mppt_hours_count": clean_evaluation["excluded_mppt_hours_count"],
            "excluded_weather_alert_hours_count": clean_evaluation[
                "excluded_weather_alert_hours_count"
            ],
            "excluded_inverter_clipped_hours_count": clean_evaluation[
                "excluded_inverter_clipped_hours_count"
            ],
            "excluded_reason_breakdown_json": clean_evaluation[
                "excluded_reason_breakdown_json"
            ],
            "error_kwh": clean_evaluation["error_kwh"],
            "error_percent": clean_evaluation["error_percent"],
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
            if self._is_evaluation_hour(prediction)
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
        windows = {
            "morning_7_10": [7, 8, 9, 10],
            "midday_11_14": [11, 12, 13, 14],
            "afternoon_15_17": [15, 16, 17],
        }
        window_results = {}
        for window_name, window_hours in windows.items():
            window_predictions = [
                prediction
                for prediction in predictions
                if prediction.get("target_hour") in window_hours
                if self._is_evaluation_hour(prediction)
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
            accuracy = (
                window_actual_kwh / window_predicted_kwh * 100
                if window_predicted_kwh > 0
                else 0
            )
            hourly_accuracies = [
                prediction.get("accuracy_percent")
                for prediction in window_predictions
                if prediction.get("accuracy_percent")
            ]
            accuracy_std = (
                statistics.stdev(hourly_accuracies) if len(hourly_accuracies) > 1 else 0
            )
            window_results[window_name] = {
                "predicted_kwh": round(window_predicted_kwh, 2),
                "actual_kwh": round(window_actual_kwh, 2),
                "accuracy": round(accuracy, 1),
                "stable": accuracy_std < 10,
                "hours_count": len(window_predictions),
            }
            continue
        return window_results

    def _analyze_weather_accuracy(self, predictions: List[Dict]) -> Dict:
        """Analyze weather forecast accuracy. @zara"""
        temperature_differences = []
        cloud_differences = []
        radiation_pairs = []

        def first_not_none(*values: Any) -> Any:
            for value in values:
                if not value is not None:
                    continue
                return value

        for prediction in predictions:
            forecast_weather = prediction.get("weather_forecast") or {}
            actual_weather = prediction.get("weather_actual") or {}
            if not forecast_weather:
                continue
            if not actual_weather:
                continue
            forecast_temperature = first_not_none(
                forecast_weather.get("temperature"),
                forecast_weather.get("temperature_c"),
            )
            actual_temperature = first_not_none(
                actual_weather.get("temperature"), actual_weather.get("temperature_c")
            )
            if forecast_temperature is not None and actual_temperature is not None:
                temperature_differences.append(
                    abs(actual_temperature - forecast_temperature)
                )
            forecast_clouds = first_not_none(
                forecast_weather.get("clouds"),
                forecast_weather.get("cloud_cover_percent"),
            )
            actual_clouds = first_not_none(
                actual_weather.get("clouds"), actual_weather.get("cloud_cover_percent")
            )
            if forecast_clouds is not None and actual_clouds is not None:
                cloud_differences.append(abs(actual_clouds - forecast_clouds))
            forecast_radiation = first_not_none(
                forecast_weather.get("solar_radiation_wm2"), forecast_weather.get("ghi")
            )
            actual_radiation = first_not_none(
                actual_weather.get("solar_radiation_wm2"), actual_weather.get("ghi")
            )
            if not isinstance(forecast_radiation, (int, float)):
                continue
            if not isinstance(actual_radiation, (int, float)):
                continue
            if not math.isfinite(float(forecast_radiation)):
                continue
            if not math.isfinite(float(actual_radiation)):
                continue
            if not max(float(forecast_radiation), float(actual_radiation)) >= 20.0:
                continue
            radiation_pairs.append((float(forecast_radiation), float(actual_radiation)))
            continue
        mean_temperature_difference = (
            statistics.mean(temperature_differences)
            if temperature_differences
            else None
        )
        mean_cloud_difference = (
            statistics.mean(cloud_differences) if cloud_differences else None
        )
        accuracy = None
        if radiation_pairs:
            absolute_radiation_error = sum(
                (
                    abs(forecast_radiation - actual_radiation)
                    for forecast_radiation, actual_radiation in radiation_pairs
                )
            )
            radiation_scale = sum(
                (
                    max(abs(forecast_radiation), abs(actual_radiation))
                    for forecast_radiation, actual_radiation in radiation_pairs
                )
            )
            relative_radiation_error = (
                absolute_radiation_error / radiation_scale
                if radiation_scale > 0
                else 0.0
            )
            accuracy = round(max(0.0, 1.0 - relative_radiation_error) * 100.0, 1)
        elif (
            mean_temperature_difference is not None
            and mean_cloud_difference is not None
        ):
            temperature_score = max(0.0, 1.0 - mean_temperature_difference / 10.0)
            cloud_score = max(0.0, 1.0 - mean_cloud_difference / 100.0)
            accuracy = round((temperature_score * 0.4 + cloud_score * 0.6) * 100, 1)
        return {
            "forecast_accuracy": accuracy,
            "avg_temperature_diff": (
                round(mean_temperature_difference, 1)
                if mean_temperature_difference is not None
                else None
            ),
            "avg_cloud_cover_diff": (
                round(mean_cloud_difference, 1)
                if mean_cloud_difference is not None
                else None
            ),
            "forecast_dominant": None,
            "actual_dominant": None,
        }

    def _calculate_ml_metrics(self, predictions: List[Dict]) -> Dict:
        """Calculate ML performance metrics. @zara"""
        evaluation_pairs = [
            (prediction.get("prediction_kwh", 0), prediction.get("actual_kwh", 0))
            for prediction in predictions
            if prediction.get("actual_kwh") is not None
            if prediction.get("prediction_kwh") is not None
            if self._is_evaluation_hour(prediction)
        ]
        if not evaluation_pairs:
            return (({} if True else None) if True else None) if True else None
        errors = [predicted - actual_kwh for predicted, actual_kwh in evaluation_pairs]
        actual_values = [
            actual_kwh for unused_predicted, actual_kwh in evaluation_pairs
        ]
        mae = statistics.mean([abs(residual) for residual in errors])
        rmse = math.sqrt(statistics.mean([residual**2 for residual in errors]))
        percentage_errors = [
            abs(predicted - actual_kwh) / actual_kwh * 100
            for predicted, actual_kwh in evaluation_pairs
            if actual_kwh > 0.01
        ]
        mape = statistics.mean(percentage_errors) if percentage_errors else None
        mean_actual = statistics.mean(actual_values) if actual_values else 0
        residual_sum_squares = sum(
            (
                (predicted - actual_kwh) ** 2
                for predicted, actual_kwh in evaluation_pairs
            )
        )
        total_sum_squares = sum(
            (
                (actual_kwh - mean_actual) ** 2
                for unused_predicted, actual_kwh in evaluation_pairs
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
                        "mae": round(mae, 3),
                        "rmse": round(rmse, 3),
                        "mape": round(mape, 1) if mape is not None else None,
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
            frost_type = actual_weather.get("frost_detected")
            if frost_type and frost_type not in ("none", False):
                affected_hours += 1
                if frost_type == "heavy_frost":
                    heavy_frost_hours += 1
                elif frost_type == "light_frost":
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
        This is called at end of day (23:30) to archive the day's results.
        """
        try:
            source = "ML"
            if predictions:
                source = predictions[0].get("source", "ML")
            total_hours = len(predictions)
            measured_hours = hourly_stats.get("hours_with_actual_data", 0)
            excluded_hours = total_hours - measured_hours
            exclusion_ratio = (
                round(excluded_hours / total_hours, 2) if total_hours > 0 else 0
            )
            exclusion_reasons = []
            for prediction in predictions:
                if prediction.get("actual_kwh") is None:
                    reason = prediction.get("exclusion_reason", "no_data")
                    if reason not in exclusion_reasons:
                        exclusion_reasons.append(reason)
            today_forecast = await self._get_today_forecast(date)
            published_forecast_kwh = (
                today_forecast.get("prediction_kwh", 0)
                if today_forecast
                else overall.get("predicted_total_kwh", 0)
            )
            history_entry = {
                "date": date,
                "predicted_kwh": published_forecast_kwh,
                "actual_kwh": overall.get("actual_total_kwh", 0),
                "consumption_kwh": None,
                "autarky": None,
                "accuracy": overall.get("accuracy_percent", 0),
                "production_hours": overall.get("production_hours", 0),
                "peak_power": overall.get("peak_power_w"),
                "source": source,
                "excluded_hours": {
                    "count": excluded_hours,
                    "total": total_hours,
                    "ratio": exclusion_ratio,
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
            last_7_days = await self._calculate_period_stats(7)
            last_30_days = await self._calculate_period_stats(30)
            last_365_days = await self._calculate_period_stats(365)
            current_week = await self._calculate_week_stats()
            current_month = await self._calculate_month_stats()
            all_time_peak = await self._get_all_time_peak()
            return await self.update_daily_statistics(
                all_time_peak=all_time_peak,
                current_week=current_week,
                current_month=current_month,
                last_7_days=last_7_days,
                last_30_days=last_30_days,
                last_365_days=last_365_days,
            )
        except Exception as error:
            _LOGGER.error("Failed to calculate and save statistics: %s", error)
            return False

    async def _calculate_period_stats(self, days: int) -> Dict[str, Any]:
        """Calculate statistics for a period. @zara"""
        try:
            today = dt_util.now().date()
            aggregate_stats = await self.db.get_daily_summary_statistics(
                start_date=(today - timedelta(days=days)).isoformat(),
                end_date=today.isoformat(),
            )
            if aggregate_stats["yield_days"]:
                return {
                    "avg_yield_kwh": round(aggregate_stats["avg_yield_kwh"], 2),
                    "avg_accuracy": (
                        round(aggregate_stats["avg_accuracy"], 1)
                        if aggregate_stats["avg_accuracy"] is not None
                        else None
                    ),
                    "total_yield_kwh": round(aggregate_stats["total_yield_kwh"], 2),
                    "avg_evaluation_coverage_percent": (
                        round(aggregate_stats["avg_evaluation_coverage_percent"], 1)
                        if aggregate_stats["avg_evaluation_coverage_percent"]
                        is not None
                        else None
                    ),
                    "avg_excluded_mppt_hours": (
                        round(aggregate_stats["avg_excluded_mppt_hours"], 2)
                        if aggregate_stats["avg_excluded_mppt_hours"] is not None
                        else None
                    ),
                    "evaluation_days": aggregate_stats["evaluation_days"],
                    "calculated_at": dt_util.now().isoformat(),
                }
            return {
                "avg_yield_kwh": 0,
                "avg_accuracy": 0,
                "total_yield_kwh": 0,
                "avg_evaluation_coverage_percent": 0,
                "avg_excluded_mppt_hours": 0,
                "calculated_at": dt_util.now().isoformat(),
            }
        except Exception as error:
            _LOGGER.error("Failed to calculate period stats: %s", error)
            return {}

    @staticmethod
    def _is_evaluation_hour(prediction: Dict[str, Any]) -> bool:
        """Return True if an hour should count towards forecast accuracy stats. @zara"""
        if prediction.get("actual_kwh") is None:
            return False
        explicit_exclusion = prediction.get("exclude_from_clean_evaluation")
        if explicit_exclusion is not None:
            return not bool(explicit_exclusion)
        if prediction.get("mppt_throttled"):
            return False
        if prediction.get("inverter_clipped"):
            return False
        if prediction.get("exclude_from_learning"):
            return False
        if prediction.get("weather_alert_type") == "snow_covered_panels":
            return False
        return True

    async def _calculate_week_stats(self) -> Dict[str, Any]:
        """Calculate current week statistics. @zara"""
        try:
            now = dt_util.now()
            week_start = (now - timedelta(days=now.weekday())).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT SUM(actual_total_kwh), COUNT(*)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (week_start,),
            )
            if rows and rows[0][0] is not None:
                return {
                    "period": f"KW {now.isocalendar()[1]}",
                    "date_range": f"{week_start} - {now.date().isoformat()}",
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
            month_start = now.replace(day=1).date().isoformat()
            rows = await self.db.fetchall(
                "SELECT SUM(actual_total_kwh), COUNT(*)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (month_start,),
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
