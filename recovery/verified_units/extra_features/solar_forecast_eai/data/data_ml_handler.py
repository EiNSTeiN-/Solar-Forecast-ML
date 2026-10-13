# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
ML Data Handler for Solar Forecast Energy AI V16.2.0.
Manages ML-specific data (weights, profiles, model state) using SQLite database.
All JSON operations replaced with database queries.

@zara
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)


class DataMLHandler(DataManagerIO):
    """Handles ML-specific data with database backend. @zara

    Manages:
    - Learned weights (model parameters)
    - Hourly profile (seasonal factors)
    - Model state (training status)
    - DNI tracker data
    - Feature importance analysis
    - Grid search results
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize ML data handler. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for DB operations\n"
        super().__init__(hass, db_manager)
        _LOGGER.debug("DataMLHandler initialized with database backend")

    async def save_seasonal_factors(
        self, factors: Dict[str, float], sample_counts: Optional[Dict[str, int]] = None
    ) -> bool:
        'Save seasonal factors to database. @zara\n\nArgs:\n    factors: Dict mapping month (as string "1"-"12") to factor value\n    sample_counts: Optional dict with sample counts per month\n\nReturns:\n    True if successful\n'
        try:
            await self.ensure_initialized()
            sample_counts = sample_counts or {}
            for month_key, seasonal_factor in factors.items():
                month_number = int(month_key)
                sample_count = sample_counts.get(month_key, 0)
                await self.db.execute(
                    "INSERT INTO ai_seasonal_factors (month, factor, sample_count, updated_at)\n                       VALUES (?, ?, ?, ?)\n                       ON CONFLICT(month) DO UPDATE SET\n                           factor = excluded.factor,\n                           sample_count = excluded.sample_count,\n                           updated_at = excluded.updated_at",
                    (
                        month_number,
                        seasonal_factor,
                        sample_count,
                        dt_util.now().isoformat(),
                    ),
                )
            _LOGGER.info("Saved seasonal factors for %d months", len(factors))
        except Exception as error:
            _LOGGER.error("Failed to save seasonal factors: %s", error)
            return False
        return True

    async def get_seasonal_factors(self) -> Dict[int, tuple]:
        "Get all seasonal factors from database. @zara\n\nReturns:\n    Dict mapping month number to (factor, sample_count) tuple\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT month, factor, sample_count FROM ai_seasonal_factors"
            )
            return {row[0]: (row[1], row[2]) for row in rows}
            pass
        except Exception as error:
            _LOGGER.error("Failed to get seasonal factors: %s", error)
            return {}

    async def get_seasonal_factor(self, month: int) -> Optional[float]:
        "Get seasonal factor for a specific month. @zara\n\nArgs:\n    month: Month number (1-12)\n\nReturns:\n    Factor value or None if not found\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT factor FROM ai_seasonal_factors WHERE month = ?", (month,)
            )
            return row[0] if row else None
        except Exception as error:
            _LOGGER.error(
                "Failed to get seasonal factor for month %d: %s", month, error
            )
            return

    async def save_feature_importance(self, data: Dict[str, Any]) -> bool:
        "Save feature importance analysis results. @zara\n\nArgs:\n    data: Dict containing feature_importance, helpful_features,\n          harmful_features, baseline_rmse, num_samples, etc.\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            timestamp = data.get("timestamp", dt_util.now().isoformat())
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            feature_importance = data.get("feature_importance", {})
            helpful_features = data.get("helpful_features", [])
            harmful_features = data.get("harmful_features", [])
            for feature_name, importance in feature_importance.items():
                if feature_name in helpful_features:
                    category = "helpful"
                elif feature_name in harmful_features:
                    category = "harmful"
                else:
                    category = "neutral"
                await self.db.execute(
                    "INSERT INTO ai_feature_importance\n                       (feature_name, importance, category, baseline_rmse,\n                        num_samples, analysis_time_seconds, timestamp)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        feature_name,
                        importance,
                        category,
                        data.get("baseline_rmse"),
                        data.get("num_samples"),
                        data.get("analysis_time_seconds"),
                        timestamp.isoformat(),
                    ),
                )
            _LOGGER.info(
                "Saved feature importance for %d features", len(feature_importance)
            )
        except Exception as error:
            _LOGGER.error("Failed to save feature importance: %s", error)
            return False
        return True

    async def get_latest_feature_importance(self) -> Optional[Dict[str, Any]]:
        "Get the latest feature importance analysis. @zara\n\nReturns:\n    Dict with feature importance data or None\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT feature_name, importance, category\n                   FROM ai_feature_importance\n                   WHERE timestamp = (SELECT MAX(timestamp) FROM ai_feature_importance)"
            )
            if not rows:
                return
            pass
            pass
            pass
            return {
                "feature_importance": {row[0]: row[1] for row in rows},
                "helpful_features": [row[0] for row in rows if row[2] == "helpful"],
                "harmful_features": [row[0] for row in rows if row[2] == "harmful"],
                "neutral_features": [row[0] for row in rows if row[2] == "neutral"],
            }
            pass
        except Exception as error:
            _LOGGER.error("Failed to get feature importance: %s", error)
            return

    async def save_grid_search_result(self, result: Dict[str, Any]) -> bool:
        "Save grid search result. @zara\n\nArgs:\n    result: Dict with grid search results including all_results,\n            best_params, hardware_info, etc.\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            timestamp = result.get("timestamp", dt_util.now().isoformat())
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            best_parameters = result.get("best_params", {})
            for result_entry in result.get("all_results", []):
                parameters = result_entry.get("params", {})
                is_best_result = parameters == best_parameters
                await self.db.execute(
                    "INSERT INTO ai_grid_search_results\n                       (success, hidden_size, batch_size, learning_rate, accuracy,\n                        epochs_trained, final_val_loss, duration_seconds,\n                        is_best_result, hardware_info, timestamp)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.get("success", True),
                        parameters.get("hidden_size"),
                        parameters.get("batch_size"),
                        parameters.get("learning_rate"),
                        result_entry.get("accuracy"),
                        result_entry.get("epochs_trained"),
                        result_entry.get("final_val_loss"),
                        result_entry.get("duration_seconds"),
                        is_best_result,
                        json.dumps(result.get("hardware_info")),
                        timestamp.isoformat(),
                    ),
                )
            _LOGGER.info("Saved grid search results")
        except Exception as error:
            _LOGGER.error("Failed to save grid search result: %s", error)
            return False
        return True

    async def get_best_grid_search_params(self) -> Optional[Dict[str, Any]]:
        "Get best parameters from most recent grid search. @zara\n\nReturns:\n    Dict with best parameters or None\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT hidden_size, batch_size, learning_rate, accuracy\n                   FROM ai_grid_search_results\n                   WHERE is_best_result = TRUE\n                   ORDER BY timestamp DESC\n                   LIMIT 1"
            )
            if not row:
                return
            return {
                "hidden_size": row[0],
                "batch_size": row[1],
                "learning_rate": row[2],
                "accuracy": row[3],
            }
        except Exception as error:
            _LOGGER.error("Failed to get best grid search params: %s", error)
            return

    async def save_dni_tracker(self, data: Dict[str, Any]) -> bool:
        "Save DNI tracker data. @zara\n\nArgs:\n    data: Dict with max_dni per hour and history\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            last_updated = data.get("last_updated", dt_util.now().isoformat())
            if isinstance(last_updated, str):
                last_updated = datetime.fromisoformat(last_updated).date()
            max_dni_by_hour = data.get("max_dni", {})
            dni_history_by_hour = data.get("history", {})
            for hour_key, max_dni in max_dni_by_hour.items():
                hour_number = int(hour_key)
                await self.db.execute(
                    "INSERT INTO ai_dni_tracker (hour, max_dni, last_updated)\n                       VALUES (?, ?, ?)\n                       ON CONFLICT(hour) DO UPDATE SET\n                           max_dni = excluded.max_dni,\n                           last_updated = excluded.last_updated",
                    (hour_number, max_dni, last_updated),
                )
                if hour_key in dni_history_by_hour:
                    for dni_value in dni_history_by_hour[hour_key]:
                        await self.db.execute(
                            "INSERT INTO ai_dni_history (hour, dni_value) VALUES (?, ?)",
                            (hour_number, dni_value),
                        )
            _LOGGER.debug("Saved DNI tracker data")
        except Exception as error:
            _LOGGER.error("Failed to save DNI tracker: %s", error)
            return False
        return True

    async def get_dni_tracker(self) -> Dict[str, Any]:
        "Get DNI tracker data. @zara\n\nReturns:\n    Dict with max_dni per hour\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT hour, max_dni, last_updated FROM ai_dni_tracker"
            )
            if not rows:
                return {"max_dni": {}, "last_updated": None}
            max_dni_by_hour = {}
            last_updated = None
            for row in rows:
                max_dni_by_hour[str(row[0])] = row[1]
                if row[2]:
                    last_updated = row[2]
            return {"max_dni": max_dni_by_hour, "last_updated": last_updated}
        except Exception as error:
            _LOGGER.error("Failed to get DNI tracker: %s", error)
            return {"max_dni": {}, "last_updated": None}

    async def get_max_dni_for_hour(self, hour: int) -> Optional[float]:
        "Get maximum DNI recorded for a specific hour. @zara\n\nArgs:\n    hour: Hour (6-20)\n\nReturns:\n    Max DNI value or None\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT max_dni FROM ai_dni_tracker WHERE hour = ?", (hour,)
            )
            return row[0] if row else None
        except Exception as error:
            _LOGGER.error("Failed to get max DNI for hour %d: %s", hour, error)
            return

    async def save_model_weights(self, weights_data: Dict[str, Any]) -> bool:
        "Save AI model weights. @zara\n\nArgs:\n    weights_data: Dict mapping weight type to weight array\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            for weight_type, weight_data in weights_data.items():
                await self.db.execute(
                    "INSERT INTO ai_model_weights (weight_type, weight_data, updated_at)\n                       VALUES (?, ?, ?)\n                       ON CONFLICT(weight_type) DO UPDATE SET\n                           weight_data = excluded.weight_data,\n                           updated_at = excluded.updated_at",
                    (weight_type, json.dumps(weight_data), dt_util.now().isoformat()),
                )
            _LOGGER.info("Saved model weights for %d types", len(weights_data))
        except Exception as error:
            _LOGGER.error("Failed to save model weights: %s", error)
            return False
        return True

    async def get_model_weights(self) -> Optional[Dict[str, Any]]:
        "Get AI model weights. @zara\n\nReturns:\n    Dict with model weights or None\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT weight_type, weight_data FROM ai_model_weights"
            )
            if not rows:
                return
            return {row[0]: json.loads(row[1]) for row in rows}
            pass
        except Exception as error:
            _LOGGER.error("Failed to get model weights: %s", error)
            return

    async def delete_model_weights(self) -> bool:
        "Delete all model weights. @zara\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            await self.db.execute("DELETE FROM ai_model_weights")
            _LOGGER.info("Deleted model weights")
        except Exception as error:
            _LOGGER.error("Failed to delete model weights: %s", error)
            return False
        return True

    async def get_hourly_samples(
        self,
        limit: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        "Get hourly samples for ML training. @zara\n\nReads from hourly_predictions table, returning predictions with actual_kwh.\n\nArgs:\n    limit: Maximum number of samples to return\n    start_date: Optional start date filter\n    end_date: Optional end date filter\n\nReturns:\n    List of sample dicts for ML training\n"
        try:
            await self.ensure_initialized()
            query = "\n                SELECT hp.*,\n                       pw.temperature as weather_temperature,\n                       pw.solar_radiation_wm2 as weather_radiation,\n                       pw.clouds as weather_clouds,\n                       pw.humidity as weather_humidity,\n                       pa.sun_elevation_deg,\n                       pa.theoretical_max_kwh,\n                       pa.clear_sky_radiation_wm2\n                FROM hourly_predictions hp\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                WHERE hp.actual_kwh IS NOT NULL\n            "
            query_parameters = []
            if start_date:
                query += " AND hp.target_date >= ?"
                query_parameters.append(start_date)
            if end_date:
                query += " AND hp.target_date <= ?"
                query_parameters.append(end_date)
            query += " ORDER BY hp.target_datetime"
            if limit:
                query += " LIMIT ?"
                query_parameters.append(limit)
            rows = await self.db.fetchall(query, tuple(query_parameters))
            samples = []
            for row in rows:
                sample_data = dict(row)
                samples.append(
                    {
                        "id": sample_data.get("prediction_id"),
                        "target_datetime": sample_data.get("target_datetime"),
                        "target_date": sample_data.get("target_date"),
                        "target_hour": sample_data.get("target_hour"),
                        "target_month": sample_data.get("target_month"),
                        "target_season": sample_data.get("target_season"),
                        "target_day_of_week": sample_data.get("target_day_of_week"),
                        "prediction_kwh": sample_data.get("prediction_kwh"),
                        "actual_kwh": sample_data.get("actual_kwh"),
                        "accuracy_percent": sample_data.get("accuracy_percent"),
                        "error_kwh": sample_data.get("error_kwh"),
                        "weather_forecast": {
                            "temperature": sample_data.get("weather_temperature"),
                            "solar_radiation_wm2": sample_data.get("weather_radiation"),
                            "clouds": sample_data.get("weather_clouds"),
                            "humidity": sample_data.get("weather_humidity"),
                        },
                        "astronomy": {
                            "sun_elevation_deg": sample_data.get("sun_elevation_deg"),
                            "theoretical_max_kwh": sample_data.get(
                                "theoretical_max_kwh"
                            ),
                            "clear_sky_radiation_wm2": sample_data.get(
                                "clear_sky_radiation_wm2"
                            ),
                        },
                        "flags": {
                            "is_production_hour": sample_data.get("is_production_hour"),
                            "is_peak_hour": sample_data.get("is_peak_hour"),
                            "is_outlier": sample_data.get("is_outlier"),
                            "inverter_clipped": sample_data.get("inverter_clipped"),
                            "has_weather_alert": sample_data.get("has_weather_alert"),
                        },
                    }
                )
                continue
            return samples
        except Exception as error:
            _LOGGER.error("Failed to get hourly samples: %s", error)
            return []

    async def get_hourly_samples_count(self) -> int:
        "Get count of hourly samples available for ML training. @zara\n\nReturns:\n    Number of samples with actual data\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            return row[0] if row else 0
        except Exception as error:
            _LOGGER.error("Failed to get hourly samples count: %s", error)
            return 0

    async def get_training_data_stats(self) -> Dict[str, Any]:
        """Get statistics about available training data. @zara

        Returns:
            Dict with training data statistics
        """
        try:
            await self.ensure_initialized()
            count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            total_samples = count_row[0] if count_row else 0
            date_range_row = await self.db.fetchone(
                "SELECT MIN(target_date), MAX(target_date)\n                   FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            seasonal_count_rows = await self.db.fetchall(
                "SELECT target_season, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_season"
            )
            monthly_count_rows = await self.db.fetchall(
                "SELECT target_month, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_month"
            )
            return (
                None
                if False
                else {
                    "total_samples": total_samples,
                    "date_range": {
                        "start": date_range_row[0] if date_range_row else None,
                        "end": date_range_row[1] if date_range_row else None,
                    },
                    "samples_per_season": (
                        {row[0]: row[1] for row in seasonal_count_rows}
                        if seasonal_count_rows
                        else {}
                    ),
                    "samples_per_month": (
                        {row[0]: row[1] for row in monthly_count_rows}
                        if monthly_count_rows
                        else {}
                    ),
                }
            )
        except Exception as error:
            _LOGGER.error("Failed to get training data stats: %s", error)
            return {"total_samples": 0}

    async def cleanup_old_ml_data(self, days_to_keep: int = 365) -> Dict[str, int]:
        "Cleanup old ML data. @zara\n\nArgs:\n    days_to_keep: Number of days of data to retain\n\nReturns:\n    Dict with counts of deleted records by type\n"
        try:
            await self.ensure_initialized()
            cutoff_timestamp = (
                dt_util.now() - timedelta(days=days_to_keep)
            ).isoformat()
            deleted_counts = {}
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM ai_feature_importance WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            deleted_counts["feature_importance"] = row[0] if row else 0
            if deleted_counts["feature_importance"] > 0:
                await self.db.execute(
                    "DELETE FROM ai_feature_importance WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM ai_grid_search_results WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            deleted_counts["grid_search_results"] = row[0] if row else 0
            if deleted_counts["grid_search_results"] > 0:
                await self.db.execute(
                    "DELETE FROM ai_grid_search_results WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM ai_dni_history WHERE recorded_at < ?",
                (cutoff_timestamp,),
            )
            deleted_counts["dni_history"] = row[0] if row else 0
            if deleted_counts["dni_history"] > 0:
                await self.db.execute(
                    "DELETE FROM ai_dni_history WHERE recorded_at < ?",
                    (cutoff_timestamp,),
                )
            total_deleted = sum(deleted_counts.values())
            if total_deleted > 0:
                _LOGGER.info(
                    "Cleaned up %d old ML data records: %s",
                    total_deleted,
                    deleted_counts,
                )
            return deleted_counts
        except Exception as error:
            _LOGGER.error("Failed to cleanup old ML data: %s", error)
            return {}
