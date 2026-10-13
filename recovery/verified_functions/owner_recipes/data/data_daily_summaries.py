# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class DailySummariesHandler(DataManagerIO):

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
            _var_var_1 = [
                _var_var_0
                for _var_var_0 in hourly_predictions
                if _var_var_0.get("target_date") == date
            ]
            if not _var_var_1:
                _LOGGER.warning("No predictions found for %s", date)
                return False
            _var_var_2 = self._calculate_overall_stats(_var_var_1)
            _var_var_3 = self._calculate_hourly_stats(_var_var_1)
            _var_var_4 = self._calculate_ml_metrics(_var_var_1)
            _var_var_5 = self._analyze_weather_accuracy(_var_var_1)
            try:
                _var_var_6 = await self.db.fetchone(
                    "SELECT peak_power_w, peak_power_time\n                       FROM production_time_state\n                       WHERE id = 1 AND date = ?",
                    (date,),
                )
                if _var_var_6 and _var_var_6[0]:
                    _var_var_2["peak_power_w"] = float(_var_var_6[0])
                    _var_var_2["peak_power_time"] = (
                        str(_var_var_6[1]) if _var_var_6[1] else None
                    )
                else:
                    _var_var_2["peak_power_time"] = None
            except Exception:
                _var_var_2["peak_power_time"] = None
            _var_var_7 = datetime.fromisoformat(date)
            await self.db.execute(
                "INSERT INTO daily_summaries (\n                    date, day_of_week, day_of_year, month, season, week_of_year,\n                    predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                    evaluation_predicted_kwh, evaluation_actual_kwh, evaluation_coverage_percent,\n                    excluded_hours_count, excluded_mppt_hours_count,\n                    evaluation_hours_count, production_candidate_hours_count,\n                    missing_actual_hours_count, excluded_weather_alert_hours_count,\n                    excluded_inverter_clipped_hours_count, excluded_reason_breakdown_json,\n                    error_kwh, error_percent, production_hours, peak_power_w,\n                    peak_power_time, peak_hour, peak_kwh,\n                    total_hours_predicted, hours_with_actual_data,\n                    mean_hourly_accuracy, std_hourly_accuracy,\n                    forecast_accuracy, avg_temperature_diff, avg_cloud_cover_diff,\n                    forecast_dominant, actual_dominant,\n                    ml_mae, ml_rmse, ml_mape, ml_r2_score,\n                    created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(date) DO UPDATE SET\n                    predicted_total_kwh = excluded.predicted_total_kwh,\n                    actual_total_kwh = excluded.actual_total_kwh,\n                    accuracy_percent = excluded.accuracy_percent,\n                    evaluation_predicted_kwh = excluded.evaluation_predicted_kwh,\n                    evaluation_actual_kwh = excluded.evaluation_actual_kwh,\n                    evaluation_coverage_percent = excluded.evaluation_coverage_percent,\n                    excluded_hours_count = excluded.excluded_hours_count,\n                    excluded_mppt_hours_count = excluded.excluded_mppt_hours_count,\n                    evaluation_hours_count = excluded.evaluation_hours_count,\n                    production_candidate_hours_count = excluded.production_candidate_hours_count,\n                    missing_actual_hours_count = excluded.missing_actual_hours_count,\n                    excluded_weather_alert_hours_count = excluded.excluded_weather_alert_hours_count,\n                    excluded_inverter_clipped_hours_count = excluded.excluded_inverter_clipped_hours_count,\n                    excluded_reason_breakdown_json = excluded.excluded_reason_breakdown_json,\n                    error_kwh = excluded.error_kwh,\n                    error_percent = excluded.error_percent,\n                    production_hours = excluded.production_hours,\n                    peak_power_w = excluded.peak_power_w,\n                    peak_power_time = excluded.peak_power_time,\n                    peak_hour = excluded.peak_hour,\n                    peak_kwh = excluded.peak_kwh,\n                    total_hours_predicted = excluded.total_hours_predicted,\n                    hours_with_actual_data = excluded.hours_with_actual_data,\n                    mean_hourly_accuracy = excluded.mean_hourly_accuracy,\n                    std_hourly_accuracy = excluded.std_hourly_accuracy,\n                    forecast_accuracy = excluded.forecast_accuracy,\n                    avg_temperature_diff = excluded.avg_temperature_diff,\n                    avg_cloud_cover_diff = excluded.avg_cloud_cover_diff,\n                    forecast_dominant = excluded.forecast_dominant,\n                    actual_dominant = excluded.actual_dominant,\n                    ml_mae = excluded.ml_mae,\n                    ml_rmse = excluded.ml_rmse,\n                    ml_mape = excluded.ml_mape,\n                    ml_r2_score = excluded.ml_r2_score,\n                    created_at = excluded.created_at",
                (
                    date,
                    _var_var_7.weekday(),
                    _var_var_7.timetuple().tm_yday,
                    _var_var_7.month,
                    get_season(_var_var_7.month),
                    _var_var_7.isocalendar()[1],
                    _var_var_2["predicted_total_kwh"],
                    _var_var_2["actual_total_kwh"],
                    _var_var_2["accuracy_percent"],
                    _var_var_2["evaluation_predicted_kwh"],
                    _var_var_2["evaluation_actual_kwh"],
                    _var_var_2["evaluation_coverage_percent"],
                    _var_var_2["excluded_hours_count"],
                    _var_var_2["excluded_mppt_hours_count"],
                    _var_var_2["evaluation_hours_count"],
                    _var_var_2["production_candidate_hours_count"],
                    _var_var_2["missing_actual_hours_count"],
                    _var_var_2["excluded_weather_alert_hours_count"],
                    _var_var_2["excluded_inverter_clipped_hours_count"],
                    _var_var_2["excluded_reason_breakdown_json"],
                    _var_var_2["error_kwh"],
                    _var_var_2["error_percent"],
                    _var_var_2["production_hours"],
                    _var_var_2.get("peak_power_w"),
                    _var_var_2.get("peak_power_time"),
                    _var_var_2.get("peak_hour"),
                    _var_var_2.get("peak_kwh"),
                    _var_var_3["total_hours_predicted"],
                    _var_var_3["hours_with_actual_data"],
                    _var_var_3["mean_hourly_accuracy"],
                    _var_var_3["std_hourly_accuracy"],
                    _var_var_5.get("forecast_accuracy"),
                    _var_var_5.get("avg_temperature_diff"),
                    _var_var_5.get("avg_cloud_cover_diff"),
                    _var_var_5.get("forecast_dominant"),
                    _var_var_5.get("actual_dominant"),
                    _var_var_4.get("mae"),
                    _var_var_4.get("rmse"),
                    _var_var_4.get("mape"),
                    _var_var_4.get("r2_score"),
                    dt_util.now().isoformat(),
                ),
            )
            _var_var_8 = self._analyze_time_windows(_var_var_1)
            await self._save_time_windows(date, _var_var_8)
            _var_var_9 = await self._analyze_frost_detection(_var_var_1)
            await self._save_frost_analysis(date, _var_var_9)
            _var_var_10 = await self._analyze_shadow_detection(date)
            await self._save_shadow_analysis(date, _var_var_10)
            _LOGGER.info("Created daily summary for %s", date)
            return True
        except Exception as _var_var_11:
            _LOGGER.error(
                "Failed to create daily summary: %s", _var_var_11, exc_info=True
            )
            return False
