# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
ML Data Handler for Solar Forecast ML V16.2.0.
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
from .db_manager import (
    DatabaseManager,
    artifact_crypto,
    loads_protected_json,
    note_unbound_artifact_key,
    seal_protected_text,
)

_LOGGER = logging.getLogger(__name__)


class DataMLHandler(DataManagerIO):
    """Handles ML-specific data with database backend. @zara

    Manages:
    - Learned weights (model parameters)
    - Model state (training status)
    - Feature importance analysis
    - Grid search results
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize ML data handler. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for DB operations\n"
        super().__init__(hass, db_manager)
        _LOGGER.debug("DataMLHandler initialized with database backend")

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
            best_params = result.get("best_params", {})
            for search_result in result.get("all_results", []):
                params = search_result.get("params", {})
                hidden_size = params.get("hidden_size")
                hidden_sizes = params.get("hidden_sizes")
                if hidden_size is None and hidden_sizes:
                    hidden_size = hidden_sizes[0]
                is_best_result = params == best_params
                await self.db.execute(
                    "INSERT INTO ai_grid_search_results\n                       (success, hidden_size, batch_size, learning_rate, accuracy,\n                        epochs_trained, final_val_loss, duration_seconds,\n                        is_best_result, hardware_info, timestamp)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.get("success", True),
                        hidden_size,
                        params.get("batch_size"),
                        params.get("learning_rate"),
                        search_result.get("accuracy"),
                        search_result.get("epochs_trained"),
                        search_result.get("final_val_loss"),
                        search_result.get("duration_seconds"),
                        is_best_result,
                        json.dumps(result.get("hardware_info")),
                        timestamp.isoformat(),
                    ),
                )
                continue
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

    async def save_model_weights(self, weights_data: Dict[str, Any]) -> bool:
        "Save AI model weights. @zara\n\nArgs:\n    weights_data: Dict mapping weight type to weight array\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            for weight_type, weight_data in weights_data.items():
                await self.db.execute(
                    "INSERT INTO ai_model_weights (weight_type, weight_data, updated_at)\n                       VALUES (?, ?, ?)\n                       ON CONFLICT(weight_type) DO UPDATE SET\n                           weight_data = excluded.weight_data,\n                           updated_at = excluded.updated_at",
                    (
                        weight_type,
                        seal_protected_text(
                            json.dumps(weight_data),
                            "ai_model_weights",
                            str(weight_type),
                        ),
                        dt_util.now().isoformat(),
                    ),
                )
            _LOGGER.info("Saved model weights for %d types", len(weights_data))
        except artifact_crypto.ArtifactCryptoError:
            note_unbound_artifact_key("model weights")
            return False
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
            weights = {}
            for row in rows:
                if str(row[0]).startswith("coeff:"):
                    continue
                weights[row[0]] = loads_protected_json(
                    row[1], "ai_model_weights", str(row[0]), "weights"
                )
            return weights or None
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
            query_params = []
            if start_date:
                query += " AND hp.target_date >= ?"
                query_params.append(start_date)
            if end_date:
                query += " AND hp.target_date <= ?"
                query_params.append(end_date)
            query += " ORDER BY hp.target_datetime"
            if limit:
                query += " LIMIT ?"
                query_params.append(limit)
            rows = await self.db.fetchall(query, tuple(query_params))
            samples = []
            for row in rows:
                sample = dict(row)
                samples.append(
                    {
                        "id": sample.get("prediction_id"),
                        "target_datetime": sample.get("target_datetime"),
                        "target_date": sample.get("target_date"),
                        "target_hour": sample.get("target_hour"),
                        "target_month": sample.get("target_month"),
                        "target_season": sample.get("target_season"),
                        "target_day_of_week": sample.get("target_day_of_week"),
                        "prediction_kwh": sample.get("prediction_kwh"),
                        "actual_kwh": sample.get("actual_kwh"),
                        "accuracy_percent": sample.get("accuracy_percent"),
                        "error_kwh": sample.get("error_kwh"),
                        "weather_forecast": {
                            "temperature": sample.get("weather_temperature"),
                            "solar_radiation_wm2": sample.get("weather_radiation"),
                            "clouds": sample.get("weather_clouds"),
                            "humidity": sample.get("weather_humidity"),
                        },
                        "astronomy": {
                            "sun_elevation_deg": sample.get("sun_elevation_deg"),
                            "theoretical_max_kwh": sample.get("theoretical_max_kwh"),
                            "clear_sky_radiation_wm2": sample.get(
                                "clear_sky_radiation_wm2"
                            ),
                        },
                        "flags": {
                            "is_production_hour": sample.get("is_production_hour"),
                            "is_peak_hour": sample.get("is_peak_hour"),
                            "is_outlier": sample.get("is_outlier"),
                            "inverter_clipped": sample.get("inverter_clipped"),
                            "has_weather_alert": sample.get("has_weather_alert"),
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
            _var_var_9 = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
        except Exception as _var_var_7:
            _LOGGER.error("Failed to get hourly samples count: %s", _var_var_7)
            return 0
        return _var_var_9[0] if _var_var_9 else 0

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
            season_rows = await self.db.fetchall(
                "SELECT target_season, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_season"
            )
            month_rows = await self.db.fetchall(
                "SELECT target_month, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_month"
            )
            return (
                (
                    {
                        "total_samples": total_samples,
                        "date_range": {
                            "start": date_range_row[0] if date_range_row else None,
                            "end": date_range_row[1] if date_range_row else None,
                        },
                        "samples_per_season": (
                            {row[0]: row[1] for row in season_rows}
                            if season_rows
                            else {}
                        ),
                        "samples_per_month": (
                            {row[0]: row[1] for row in month_rows} if month_rows else {}
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error("Failed to get training data stats: %s", error)
            return ({"total_samples": 0} if True else None) if True else None

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
