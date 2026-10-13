# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from ..core.core_helpers import SafeDateTimeUtil

_LOGGER = logging.getLogger(__name__)


def _sql_value(val: Any) -> Union[None, str, int, float, bool]:
    """Convert a value to a SQLite-safe type (no BLOBs).

    Prevents lists, dicts, and other complex objects from being stored as BLOBs.
    """
    if val is None:
        return
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (datetime, date)):
        return val.isoformat() if hasattr(val, "isoformat") else str(val)
    if isinstance(val, (list, dict)):
        return json.dumps(val)
    return str(val)


MIGRATION_FLAG = ".storage/solar_forecast_ml_json_migrated_v3"
JSON_FILE_MAPPING = {
    "ai/feature_importance.json": "ai_feature_importance",
    "ai/grid_search_results.json": "ai_grid_search_results",
    "ai/learned_weights.json": "ai_learned_weights_meta",
    "physics/calibration_history.json": "physics_calibration_history",
    "physics/learning_config.json": "physics_learning_config",
    "data/coordinator_state.json": "coordinator_state",
    "data/production_time_state.json": "production_time_state",
    "data/weather_expert_weights.json": "weather_expert_weights",
    "data/weather_source_weights.json": "weather_source_weights",
    "data/wttr_in_cache.json": "weather_cache_wttr_in",
    "data/bright_sky_cache.json": "weather_cache_bright_sky",
    "data/pirate_weather_cache.json": "weather_cache_pirate_weather",
    "data/open_meteo_cache.json": "weather_cache_open_meteo",
    "stats/hourly_predictions.json": "hourly_predictions",
    "stats/daily_forecasts.json": "daily_forecasts",
    "stats/daily_summaries.json": "daily_summaries",
    "stats/weather_forecast_corrected.json": "weather_forecast",
    "stats/weather_integration_ml.json": "weather_forecast",
    "stats/astronomy_cache.json": "astronomy_cache",
    "stats/weather_expert_learning.json": "weather_expert_learning",
    "stats/weather_source_learning.json": "weather_source_learning",
    "stats/panel_group_sensor_state.json": "panel_group_sensor_state",
    "stats/yield_cache.json": "yield_cache",
    "stats/visibility_learning.json": "visibility_learning",
    "stats/forecast_drift_log.json": "forecast_drift_log",
    "stats/hourly_weather_actual.json": "hourly_weather_actual",
    "stats/weather_precision_daily.json": "weather_precision_daily",
    "stats/panel_group_today_cache.json": "panel_group_daily_cache",
    "stats/multi_day_hourly_forecast.json": "multi_day_hourly_forecast",
    "stats/retrospective_forecast.json": "retrospective_forecast",
}


class JsonMigrationStats:

    def __init__(self):
        self.imported = 0
        self.updated = 0
        self.skipped = 0
        self.errors = 0
        self.error_files = []

    def __str__(self) -> str:
        return f"Imported: {self.imported}, Updated: {self.updated}, Skipped: {self.skipped}, Errors: {self.errors}"


class JsonMigrator:

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        self.hass = hass
        self.db = db_manager
        self.base_path = Path(hass.config.path("solar_forecast_ml"))
        self.stats = JsonMigrationStats()

    async def should_migrate(self) -> bool:
        flag_path = Path(self.hass.config.path(MIGRATION_FLAG))
        return not flag_path.exists()

    async def dry_run(self) -> Dict[str, Any]:
        dry_run_result = {
            "flag_exists": not await self.should_migrate(),
            "base_path": str(self.base_path),
            "base_path_exists": self.base_path.exists(),
            "files_found": [],
            "files_missing": [],
            "file_details": {},
            "total_entries": 0,
            "validation_errors": [],
        }
        for relative_file_path, table_name in JSON_FILE_MAPPING.items():
            file_path = self.base_path / relative_file_path
            if file_path.exists():
                dry_run_result["files_found"].append(relative_file_path)
                try:
                    loaded_data = await self._load_json(file_path)
                    if loaded_data is None:
                        dry_run_result["validation_errors"].append(
                            f"{relative_file_path}: Invalid JSON"
                        )
                        continue
                    entry_count = self._count_entries(relative_file_path, loaded_data)
                    dry_run_result["file_details"][relative_file_path] = {
                        "table": table_name,
                        "entries": entry_count,
                        "size_bytes": file_path.stat().st_size,
                    }
                    dry_run_result["total_entries"] += entry_count
                except Exception as migration_error:
                    dry_run_result["validation_errors"].append(
                        f"{relative_file_path}: {migration_error}"
                    )
                    continue
            else:
                dry_run_result["files_missing"].append(relative_file_path)
        return dry_run_result

    def _count_entries(self, file_path: str, data: Dict[str, Any]) -> int:
        if isinstance(data, list):
            return len(data)
        entry_counters = {
            "ai/feature_importance.json": lambda d: len(
                d.get("feature_importance", {})
            ),
            "ai/grid_search_results.json": lambda d: len(d.get("all_results", [])),
            "ai/learned_weights.json": lambda d: 2,
            "physics/calibration_history.json": lambda d: sum(
                (
                    len(history_record.get("groups", {}))
                    for history_record in d.get("history", [])
                )
            ),
            "physics/learning_config.json": lambda d: 1
            + len(d.get("group_calibration", {})),
            "data/coordinator_state.json": lambda d: 1,
            "data/production_time_state.json": lambda d: 1,
            "data/weather_expert_weights.json": lambda d: sum(
                (
                    len(expert_weights)
                    for expert_weights in d.get("weights", {}).values()
                )
            ),
            "data/weather_source_weights.json": lambda d: len(d.get("weights", {})),
            "data/wttr_in_cache.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "data/bright_sky_cache.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "data/pirate_weather_cache.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "data/open_meteo_cache.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "stats/hourly_predictions.json": lambda d: len(d.get("predictions", [])),
            "stats/daily_forecasts.json": lambda d: len(d.get("history", [])) + 3,
            "stats/daily_summaries.json": lambda d: len(d.get("summaries", [])),
            "stats/astronomy_cache.json": lambda d: sum(
                (
                    len(day_record.get("hourly", {}))
                    for day_record in d.get("days", {}).values()
                )
            ),
            "stats/weather_forecast_corrected.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "stats/weather_integration_ml.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("forecast", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "stats/weather_expert_learning.json": lambda d: sum(
                (
                    len(day_record.get("mae_by_expert", {}))
                    for day_record in d.get("daily_history", {}).values()
                )
            ),
            "stats/weather_source_learning.json": lambda d: sum(
                (
                    1
                    for day_record in d.get("daily_history", {}).values()
                    for metric_key in day_record
                    if metric_key.startswith("mae_")
                )
            ),
            "stats/hourly_weather_actual.json": lambda d: sum(
                (
                    len(hourly_entries)
                    for hourly_entries in d.get("hourly_data", {}).values()
                    if isinstance(hourly_entries, dict)
                )
            ),
            "stats/yield_cache.json": lambda d: 1,
            "stats/visibility_learning.json": lambda d: 1,
            "stats/retrospective_forecast.json": lambda d: 1
            + len(d.get("hourly_predictions", [])),
            "stats/forecast_drift_log.json": lambda d: len(d.get("entries", [])),
            "stats/weather_precision_daily.json": lambda d: sum(
                (
                    len(day_record.get("hourly_comparisons", []))
                    for day_record in d.get("daily_tracking", {}).values()
                )
            ),
            "stats/multi_day_hourly_forecast.json": lambda d: sum(
                (
                    len(day_record.get("hourly", []))
                    for day_record in d.get("days", {}).values()
                )
            ),
            "stats/panel_group_today_cache.json": lambda d: len(d.get("groups", {})),
        }
        count_entries_for_file = entry_counters.get(file_path)
        if count_entries_for_file:
            try:
                return count_entries_for_file(data)
            except Exception:
                return 0
        if isinstance(data, dict):
            for collection_key in (
                "entries",
                "hourly",
                "predictions",
                "forecasts",
                "summaries",
                "groups",
            ):
                if not collection_key in data:
                    continue
                fallback_entries = data[collection_key]
                return (
                    # Keep this line boundary for the verified shared return block.
                    (
                        (
                            len(fallback_entries)
                            if isinstance(fallback_entries, (list, dict))
                            else 0 if True else None
                        )
                        if True
                        else None
                    )
                    if True
                    else None
                )
        return 1

    async def run_migration(self) -> JsonMigrationStats:
        if not await self.should_migrate():
            _LOGGER.debug("JSON migration already completed - skipping")
            return self.stats
        _LOGGER.info("============================================================")
        _LOGGER.info("Starting JSON to SQLite migration...")
        _LOGGER.info("============================================================")
        json_files = await self._find_json_files()
        if not json_files:
            _LOGGER.info("No JSON files found for migration")
            await self._set_migration_flag()
            return self.stats
        file_count = len(json_files)
        _LOGGER.info(f"Found {file_count} JSON files to migrate")
        for file_index, json_path in enumerate(json_files, 1):
            relative_file_path = str(json_path.relative_to(self.base_path))
            _LOGGER.info(
                f"[{file_index}/{file_count}] Migrating {relative_file_path}..."
            )
            await self._migrate_file(json_path)
            _LOGGER.info(
                f"[{file_index}/{file_count}] Completed {relative_file_path} - Running total: {self.stats}"
            )
        await self._set_migration_flag()
        await self.db.protect_stored_artifacts()
        _LOGGER.info("============================================================")
        _LOGGER.info("JSON MIGRATION COMPLETED SUCCESSFULLY")
        _LOGGER.info(f"Final stats: {self.stats}")
        _LOGGER.info("============================================================")
        if self.stats.error_files:
            _LOGGER.warning(f"Failed files: {', '.join(self.stats.error_files)}")
        return self.stats

    async def _find_json_files(self) -> List[Path]:

        def _scan_files():
            found_files = []
            for relative_file_path in JSON_FILE_MAPPING.keys():
                file_path = self.base_path / relative_file_path
                if file_path.exists():
                    found_files.append(file_path)
            return found_files

        return await self.hass.async_add_executor_job(_scan_files)

    async def _migrate_file(self, json_path: Path) -> None:
        relative_file_path = str(json_path.relative_to(self.base_path))
        table_name = JSON_FILE_MAPPING.get(relative_file_path)
        if not table_name:
            self.stats.skipped += 1
            return
        try:
            loaded_data = await self._load_json(json_path)
            if loaded_data is None:
                self.stats.errors += 1
                self.stats.error_files.append(relative_file_path)
                return
            await self._migrate_data(relative_file_path, table_name, loaded_data)
        except Exception as migration_error:
            _LOGGER.error(
                f"Migration failed for {relative_file_path}: {migration_error}"
            )
            self.stats.errors += 1
            self.stats.error_files.append(relative_file_path)
        return None if True else None

    async def _load_json(self, path: Path) -> Optional[Dict[str, Any]]:

        def _read():
            try:
                with open(path, "r", encoding="utf-8") as json_file:
                    return json.load(json_file)
            except json.JSONDecodeError as migration_error:
                _LOGGER.error(f"Invalid JSON in {path}: {migration_error}")
                return
            except Exception as migration_error:
                _LOGGER.error(f"Failed to read {path}: {migration_error}")
                return
            return None if True else None

        return await self.hass.async_add_executor_job(_read)

    async def _migrate_data(
        self, file_path: str, table_name: str, data: Dict[str, Any]
    ) -> None:
        migration_handlers = {
            "ai/feature_importance.json": self._migrate_feature_importance,
            "ai/grid_search_results.json": self._migrate_grid_search,
            "ai/learned_weights.json": self._migrate_learned_weights,
            "physics/calibration_history.json": self._migrate_calibration_history,
            "physics/learning_config.json": self._migrate_learning_config,
            "data/coordinator_state.json": self._migrate_coordinator_state,
            "data/production_time_state.json": self._migrate_production_time_state,
            "data/weather_expert_weights.json": self._migrate_weather_expert_weights,
            "data/weather_source_weights.json": self._migrate_weather_source_weights,
            "data/wttr_in_cache.json": self._migrate_wttr_cache,
            "data/bright_sky_cache.json": self._migrate_bright_sky_cache,
            "data/pirate_weather_cache.json": self._migrate_pirate_weather_cache,
            "data/open_meteo_cache.json": self._migrate_open_meteo_cache,
            "stats/hourly_predictions.json": self._migrate_hourly_predictions,
            "stats/daily_forecasts.json": self._migrate_daily_forecasts,
            "stats/daily_summaries.json": self._migrate_daily_summaries,
            "stats/weather_forecast_corrected.json": self._migrate_weather_forecast,
            "stats/weather_integration_ml.json": self._migrate_weather_forecast,
            "stats/astronomy_cache.json": self._migrate_astronomy_cache,
            "stats/weather_expert_learning.json": self._migrate_weather_expert_learning,
            "stats/weather_source_learning.json": self._migrate_weather_source_learning,
            "stats/panel_group_sensor_state.json": self._migrate_panel_group_sensor_state,
            "stats/yield_cache.json": self._migrate_yield_cache,
            "stats/visibility_learning.json": self._migrate_visibility_learning,
            "stats/forecast_drift_log.json": self._migrate_forecast_drift_log,
            "stats/hourly_weather_actual.json": self._migrate_hourly_weather_actual,
            "stats/weather_precision_daily.json": self._migrate_weather_precision_daily,
            "stats/panel_group_today_cache.json": self._migrate_panel_group_daily_cache,
            "stats/multi_day_hourly_forecast.json": self._migrate_multi_day_forecast,
            "stats/retrospective_forecast.json": self._migrate_retrospective_forecast,
        }
        migration_handler = migration_handlers.get(file_path)
        if migration_handler:
            await migration_handler(data)
        else:
            self.stats.skipped += 1
        return None if True else None

    def _format_hardware_info(self, hw: Optional[Dict[str, Any]]) -> Optional[str]:
        if not hw or not isinstance(hw, dict):
            return None
        hardware_parts = []
        if hw.get("machine_type"):
            hardware_parts.append(hw["machine_type"])
        if hw.get("architecture"):
            hardware_parts.append(hw["architecture"])
        if hw.get("cpu_count"):
            hardware_parts.append(f"{hw['cpu_count']} CPUs")
        if hw.get("is_container"):
            hardware_parts.append("container")
        if hw.get("is_raspberry_pi"):
            hardware_parts.append("RPi")
        return " ".join(hardware_parts) if hardware_parts else None

    async def _migrate_feature_importance(self, data: Dict[str, Any]) -> None:
        timestamp_value = data.get("timestamp")
        if not timestamp_value:
            self.stats.skipped += 1
            return
        try:
            timestamp = datetime.fromisoformat(timestamp_value)
        except Exception:
            self.stats.skipped += 1
            return
        feature_importance = data.get("feature_importance", {})
        helpful_features = data.get("helpful_features", [])
        harmful_features = data.get("harmful_features", [])
        for feature_name, importance in feature_importance.items():
            if feature_name in helpful_features:
                feature_category = "helpful"
            elif feature_name in harmful_features:
                feature_category = "harmful"
            else:
                feature_category = "neutral"
            try:
                existing_row = await self.db.fetchone(
                    "SELECT id FROM ai_feature_importance WHERE feature_name = ? AND timestamp = ?",
                    (feature_name, timestamp),
                )
                if existing_row:
                    await self.db.execute(
                        "UPDATE ai_feature_importance SET importance = ?, category = ? WHERE feature_name = ? AND timestamp = ?",
                        (importance, feature_category, feature_name, timestamp),
                    )
                    self.stats.updated += 1
                    continue
                await self.db.execute(
                    "INSERT INTO ai_feature_importance (feature_name, importance, category, baseline_rmse, num_samples, analysis_time_seconds, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        feature_name,
                        importance,
                        feature_category,
                        data.get("baseline_rmse"),
                        data.get("num_samples"),
                        data.get("analysis_time_seconds"),
                        timestamp,
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip feature {feature_name}: {migration_error}")
                self.stats.skipped += 1
                continue
            continue

    async def _migrate_grid_search(self, data: Dict[str, Any]) -> None:
        grid_results = data.get("all_results", [])
        timestamp_value = data.get("timestamp")
        best_parameters = data.get("best_params", {})
        if not timestamp_value:
            self.stats.skipped += 1
            return
        try:
            timestamp = datetime.fromisoformat(timestamp_value)
        except Exception:
            self.stats.skipped += 1
            return
        for grid_result in grid_results:
            parameters = grid_result.get("params", {})
            is_best_result = parameters == best_parameters
            try:
                await self.db.execute(
                    "INSERT OR IGNORE INTO ai_grid_search_results (success, hidden_size, batch_size, learning_rate, accuracy, epochs_trained, final_val_loss, duration_seconds, is_best_result, hardware_info, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        data.get("success", False),
                        parameters.get("hidden_size"),
                        parameters.get("batch_size"),
                        parameters.get("learning_rate"),
                        grid_result.get("accuracy"),
                        grid_result.get("epochs_trained"),
                        grid_result.get("final_val_loss"),
                        grid_result.get("duration_seconds"),
                        is_best_result,
                        self._format_hardware_info(data.get("hardware_info")),
                        timestamp,
                    ),
                )
                self.stats.imported += 1
                continue
            except Exception as migration_error:
                _LOGGER.debug(f"Skip grid search result: {migration_error}")
                self.stats.skipped += 1

    async def _migrate_learned_weights(self, data: Dict[str, Any]) -> None:
        try:
            existing_row = await self.db.fetchone(
                "SELECT id FROM ai_learned_weights_meta WHERE id = 1"
            )
            learned_weight_metadata = (
                data.get("version", "2.0"),
                data.get("active_model", "tiny_lstm"),
                data.get("training_samples"),
                data.get("last_trained"),
                data.get("accuracy"),
                data.get("rmse"),
                SafeDateTimeUtil.now(),
            )
            if existing_row:
                await self.db.execute(
                    "UPDATE ai_learned_weights_meta SET version = ?, active_model = ?, training_samples = ?, last_trained = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                    learned_weight_metadata,
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO ai_learned_weights_meta (id, version, active_model, training_samples, last_trained, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    learned_weight_metadata,
                )
                self.stats.imported += 1
            ridge_model = data.get("ridge", {})
            if ridge_model:
                ridge_weights = ridge_model.get("weights", [])
                if ridge_weights and isinstance(ridge_weights, list):
                    for row_index, weight_row_or_value in enumerate(ridge_weights):
                        if isinstance(weight_row_or_value, list):
                            for column_index, weight_value in enumerate(
                                weight_row_or_value
                            ):
                                if weight_value is not None and weight_value != 0:
                                    await self.db.execute(
                                        "INSERT OR REPLACE INTO ai_ridge_weights (row_index, col_index, weight_value) VALUES (?, ?, ?)",
                                        (row_index, column_index, weight_value),
                                    )
                                    self.stats.imported += 1
                existing_ridge_metadata = await self.db.fetchone(
                    "SELECT id FROM ai_ridge_meta WHERE id = 1"
                )
                ridge_metadata = (
                    ridge_model.get("model_type", "TinyRidge"),
                    ridge_model.get("alpha"),
                    ridge_model.get("input_size"),
                    ridge_model.get("hidden_size"),
                    ridge_model.get("sequence_length"),
                    ridge_model.get("num_outputs"),
                    ridge_model.get("flat_size"),
                    ridge_model.get("trained_samples"),
                    ridge_model.get("loo_cv_score"),
                    ridge_model.get("accuracy"),
                    ridge_model.get("rmse"),
                    SafeDateTimeUtil.now(),
                )
                if existing_ridge_metadata:
                    await self.db.execute(
                        "UPDATE ai_ridge_meta SET model_type = ?, alpha = ?, input_size = ?, hidden_size = ?, sequence_length = ?, num_outputs = ?, flat_size = ?, trained_samples = ?, loo_cv_score = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                        ridge_metadata,
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO ai_ridge_meta (id, model_type, alpha, input_size, hidden_size, sequence_length, num_outputs, flat_size, trained_samples, loo_cv_score, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ridge_metadata,
                    )
                    self.stats.imported += 1
                feature_means = ridge_model.get("feature_means", [])
                feature_standard_deviations = ridge_model.get("feature_stds", [])
                if feature_means and feature_standard_deviations:
                    for feature_index, (
                        feature_mean,
                        feature_standard_deviation,
                    ) in enumerate(zip(feature_means, feature_standard_deviations)):
                        if feature_mean is not None:
                            if feature_standard_deviation is not None:
                                await self.db.execute(
                                    "INSERT OR REPLACE INTO ai_ridge_normalization (feature_index, feature_mean, feature_std) VALUES (?, ?, ?)",
                                    (
                                        feature_index,
                                        feature_mean,
                                        feature_standard_deviation,
                                    ),
                                )
                                self.stats.imported += 1
            lstm_model = data.get("lstm", {})
            if lstm_model:
                weight_types = [
                    "Wf",
                    "Wi",
                    "Wc",
                    "Wo",
                    "bf",
                    "bi",
                    "bc",
                    "bo",
                    "Wy",
                    "by",
                    "W_query",
                    "W_key",
                    "W_value",
                    "W_attn_out",
                    "b_attn_out",
                ]
                for weight_type in weight_types:
                    weight_values = lstm_model.get(weight_type, [])
                    if isinstance(weight_values, list):
                        weight_index = 0
                        for weight_row_or_value in weight_values:
                            if isinstance(weight_row_or_value, list):
                                for weight_value in weight_row_or_value:
                                    if isinstance(weight_value, (int, float)):
                                        await self.db.execute(
                                            "INSERT OR REPLACE INTO ai_lstm_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                                            (weight_type, weight_index, weight_value),
                                        )
                                        self.stats.imported += 1
                                        weight_index += 1
                            elif isinstance(weight_row_or_value, (int, float)):
                                await self.db.execute(
                                    "INSERT OR REPLACE INTO ai_lstm_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                                    (weight_type, weight_index, weight_row_or_value),
                                )
                                self.stats.imported += 1
                                weight_index += 1
                existing_lstm_metadata = await self.db.fetchone(
                    "SELECT id FROM ai_lstm_meta WHERE id = 1"
                )
                lstm_metadata = (
                    lstm_model.get("input_size"),
                    lstm_model.get("hidden_size"),
                    lstm_model.get("sequence_length"),
                    lstm_model.get("num_outputs"),
                    lstm_model.get("has_attention", False),
                    lstm_model.get("training_samples") or data.get("training_samples"),
                    lstm_model.get("accuracy") or data.get("accuracy"),
                    lstm_model.get("rmse") or data.get("rmse"),
                    SafeDateTimeUtil.now(),
                )
                if existing_lstm_metadata:
                    await self.db.execute(
                        "UPDATE ai_lstm_meta SET input_size = ?, hidden_size = ?, sequence_length = ?, num_outputs = ?, has_attention = ?, training_samples = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                        lstm_metadata,
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO ai_lstm_meta (id, input_size, hidden_size, sequence_length, num_outputs, has_attention, training_samples, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        lstm_metadata,
                    )
                    self.stats.imported += 1
        except Exception as migration_error:
            _LOGGER.debug(f"Skip learned weights: {migration_error}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_calibration_history(self, data: Dict[str, Any]) -> None:
        history_entries = (
            data
            if isinstance(data, list)
            else data.get("history", data.get("entries", []))
        )
        for history_entry in history_entries:
            date_value = history_entry.get("date")
            if not date_value:
                continue
            try:
                calibration_date = datetime.fromisoformat(date_value).date()
            except Exception:
                continue
            group_records = history_entry.get("groups", {})
            for group_name, group_record in group_records.items():
                hourly_records = group_record.get("hourly", {})
                group_sample_count = group_record.get("sample_count", 0)
                for hour_key, average_ratio in hourly_records.items():
                    try:
                        hour = int(hour_key)
                        existing_row = await self.db.fetchone(
                            "SELECT id FROM physics_calibration_history WHERE date = ? AND group_name = ? AND hour = ? AND bucket_name IS NULL",
                            (calibration_date, group_name, hour),
                        )
                        if existing_row:
                            await self.db.execute(
                                "UPDATE physics_calibration_history SET avg_ratio = ?, sample_count = ? WHERE id = ?",
                                (average_ratio, group_sample_count, existing_row[0]),
                            )
                            self.stats.updated += 1
                        else:
                            await self.db.execute(
                                "INSERT INTO physics_calibration_history (date, group_name, bucket_name, hour, avg_ratio, sample_count, source) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                                (
                                    calibration_date,
                                    group_name,
                                    hour,
                                    average_ratio,
                                    group_sample_count,
                                    history_entry.get("source"),
                                ),
                            )
                            self.stats.imported += 1
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip calibration entry: {migration_error}")
                        self.stats.skipped += 1
                        continue
                bucket_records = group_record.get("buckets", {})
                for bucket_name, bucket_record in bucket_records.items():
                    bucket_hourly_records = bucket_record.get("hourly", {})
                    bucket_sample_count = bucket_record.get("sample_count", 0)
                    for hour_key, average_ratio in bucket_hourly_records.items():
                        try:
                            hour = int(hour_key)
                            existing_row = await self.db.fetchone(
                                "SELECT id FROM physics_calibration_history WHERE date = ? AND group_name = ? AND hour = ? AND bucket_name = ?",
                                (calibration_date, group_name, hour, bucket_name),
                            )
                            if existing_row:
                                await self.db.execute(
                                    "UPDATE physics_calibration_history SET avg_ratio = ?, sample_count = ? WHERE id = ?",
                                    (
                                        average_ratio,
                                        bucket_sample_count,
                                        existing_row[0],
                                    ),
                                )
                                self.stats.updated += 1
                            else:
                                await self.db.execute(
                                    "INSERT INTO physics_calibration_history (date, group_name, bucket_name, hour, avg_ratio, sample_count, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        calibration_date,
                                        group_name,
                                        bucket_name,
                                        hour,
                                        average_ratio,
                                        bucket_sample_count,
                                        history_entry.get("source"),
                                    ),
                                )
                                self.stats.imported += 1
                        except Exception as migration_error:
                            _LOGGER.debug(
                                f"Skip bucket calibration entry: {migration_error}"
                            )
                            self.stats.skipped += 1
                            continue
                    continue
                continue
            continue

    async def _migrate_learning_config(self, data: Dict[str, Any]) -> None:
        try:
            physics_defaults = data.get("physics_defaults", {})
            metadata = data.get("metadata", {})
            updated_at = data.get("updated_at")
            if updated_at:
                try:
                    updated_at = datetime.fromisoformat(updated_at)
                except Exception:
                    updated_at = SafeDateTimeUtil.now()
            else:
                updated_at = SafeDateTimeUtil.now()
            existing_row = await self.db.fetchone(
                "SELECT id FROM physics_learning_config WHERE id = 1"
            )
            if existing_row:
                await self.db.execute(
                    "UPDATE physics_learning_config SET version = ?, albedo = ?, system_efficiency = ?, learned_efficiency_factor = ?, rolling_window_days = ?, min_samples = ?, updated_at = ? WHERE id = 1",
                    (
                        data.get("version", "3.0"),
                        physics_defaults.get("albedo", 0.2),
                        physics_defaults.get("system_efficiency", 0.9),
                        physics_defaults.get("learned_efficiency_factor", 1.0),
                        metadata.get("rolling_window_days", 21),
                        metadata.get("min_samples", 1),
                        updated_at,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO physics_learning_config (id, version, albedo, system_efficiency, learned_efficiency_factor, rolling_window_days, min_samples, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        data.get("version", "3.0"),
                        physics_defaults.get("albedo", 0.2),
                        physics_defaults.get("system_efficiency", 0.9),
                        physics_defaults.get("learned_efficiency_factor", 1.0),
                        metadata.get("rolling_window_days", 21),
                        metadata.get("min_samples", 1),
                        updated_at,
                    ),
                )
                self.stats.imported += 1
            for group_name, group_record in data.get("group_calibration", {}).items():
                await self._migrate_group_calibration(group_name, group_record)
        except Exception as migration_error:
            _LOGGER.debug(f"Skip learning config: {migration_error}")
            self.stats.skipped += 1
        if True:
            return

    async def _migrate_group_calibration(
        self, group_name: str, group_data: Dict[str, Any]
    ) -> None:
        try:
            last_updated = group_data.get("last_updated")
            if last_updated:
                try:
                    last_updated = datetime.fromisoformat(last_updated)
                except Exception:
                    last_updated = SafeDateTimeUtil.now()
            else:
                last_updated = SafeDateTimeUtil.now()
            existing_row = await self.db.fetchone(
                "SELECT id FROM physics_calibration_groups WHERE group_name = ?",
                (group_name,),
            )
            if existing_row:
                await self.db.execute(
                    "UPDATE physics_calibration_groups SET global_factor = ?, sample_count = ?, confidence = ?, last_updated = ? WHERE group_name = ?",
                    (
                        group_data.get("global_factor", 1.0),
                        group_data.get("sample_count", 0),
                        group_data.get("confidence", 0.0),
                        last_updated,
                        group_name,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO physics_calibration_groups (group_name, global_factor, sample_count, confidence, last_updated) VALUES (?, ?, ?, ?, ?)",
                    (
                        group_name,
                        group_data.get("global_factor", 1.0),
                        group_data.get("sample_count", 0),
                        group_data.get("confidence", 0.0),
                        last_updated,
                    ),
                )
                self.stats.imported += 1
            for hour_key, factor in group_data.get("hourly_factors", {}).items():
                try:
                    hour = int(hour_key)
                    await self.db.execute(
                        "INSERT OR REPLACE INTO physics_calibration_hourly (group_name, hour, factor) VALUES (?, ?, ?)",
                        (group_name, hour, factor),
                    )
                except Exception:
                    continue
            for bucket_name, bucket_record in group_data.get(
                "bucket_factors", {}
            ).items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO physics_calibration_buckets (group_name, bucket_name, global_factor, sample_count, confidence) VALUES (?, ?, ?, ?, ?)",
                    (
                        group_name,
                        bucket_name,
                        bucket_record.get("global_factor", 1.0),
                        bucket_record.get("sample_count", 0),
                        bucket_record.get("confidence", 0.0),
                    ),
                )
                for hour_key, factor in bucket_record.get("hourly_factors", {}).items():
                    try:
                        hour = int(hour_key)
                        await self.db.execute(
                            "INSERT OR REPLACE INTO physics_calibration_bucket_hourly (group_name, bucket_name, hour, factor) VALUES (?, ?, ?, ?)",
                            (group_name, bucket_name, hour, factor),
                        )
                    except Exception:
                        continue
        except Exception as migration_error:
            _LOGGER.debug(f"Skip group calibration {group_name}: {migration_error}")
        if True:
            return

    async def _migrate_coordinator_state(self, data: Dict[str, Any]) -> None:
        try:
            last_set_date = data.get("last_set_date")
            last_updated = data.get("last_updated")
            if last_set_date:
                try:
                    last_set_date = datetime.fromisoformat(last_set_date).date()
                except Exception:
                    last_set_date = None
            if last_updated:
                try:
                    last_updated = datetime.fromisoformat(last_updated)
                except Exception:
                    last_updated = SafeDateTimeUtil.now()
            else:
                last_updated = SafeDateTimeUtil.now()
            existing_row = await self.db.fetchone(
                "SELECT id FROM coordinator_state WHERE id = 1"
            )
            if existing_row:
                await self.db.execute(
                    "UPDATE coordinator_state SET expected_daily_production = ?, last_set_date = ?, last_updated = ? WHERE id = 1",
                    (
                        data.get("expected_daily_production"),
                        last_set_date,
                        last_updated,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO coordinator_state (id, expected_daily_production, last_set_date, last_updated) VALUES (1, ?, ?, ?)",
                    (
                        data.get("expected_daily_production"),
                        last_set_date,
                        last_updated,
                    ),
                )
                self.stats.imported += 1
        except Exception as migration_error:
            _LOGGER.debug(f"Skip coordinator state: {migration_error}")
            self.stats.skipped += 1
        if True:
            return

    async def _migrate_production_time_state(self, data: Dict[str, Any]) -> None:
        try:
            record_date = data.get("date")
            if record_date:
                try:
                    record_date = datetime.fromisoformat(record_date).date()
                except Exception:
                    record_date = SafeDateTimeUtil.now().date()
            else:
                record_date = SafeDateTimeUtil.now().date()
            last_updated = data.get("last_updated")
            if last_updated:
                try:
                    last_updated = datetime.fromisoformat(last_updated)
                except Exception:
                    last_updated = SafeDateTimeUtil.now()
            else:
                last_updated = SafeDateTimeUtil.now()
            existing_row = await self.db.fetchone(
                "SELECT id FROM production_time_state WHERE id = 1"
            )
            if existing_row:
                await self.db.execute(
                    "UPDATE production_time_state SET date = ?, accumulated_hours = ?, is_active = ?, start_time = ?, production_time_today = ?, last_updated = ? WHERE id = 1",
                    (
                        record_date,
                        data.get("accumulated_hours", 0),
                        data.get("is_active", False),
                        data.get("start_time"),
                        data.get("production_time_today"),
                        last_updated,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO production_time_state (id, date, accumulated_hours, is_active, start_time, production_time_today, last_updated) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (
                        record_date,
                        data.get("accumulated_hours", 0),
                        data.get("is_active", False),
                        data.get("start_time"),
                        data.get("production_time_today"),
                        last_updated,
                    ),
                )
                self.stats.imported += 1
        except Exception as migration_error:
            _LOGGER.debug(f"Skip production time state: {migration_error}")
            self.stats.skipped += 1
        if True:
            return

    async def _migrate_weather_expert_weights(self, data: Dict[str, Any]) -> None:
        weights = data.get("weights", {})
        metadata = data.get("metadata", {})
        last_updated = metadata.get("last_updated")
        if last_updated:
            try:
                last_updated = datetime.fromisoformat(last_updated)
            except Exception:
                last_updated = SafeDateTimeUtil.now()
        else:
            last_updated = SafeDateTimeUtil.now()
        for cloud_type, expert_weights in weights.items():
            for expert_name, weight in expert_weights.items():
                try:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO weather_expert_weights (cloud_type, expert_name, weight, last_updated) VALUES (?, ?, ?, ?)",
                        (cloud_type, expert_name, weight, last_updated),
                    )
                    self.stats.imported += 1
                except Exception as migration_error:
                    _LOGGER.debug(f"Skip weather expert weight: {migration_error}")
                    self.stats.skipped += 1
                    continue
        snow_prediction_stats = data.get("snow_prediction_stats", {})
        if snow_prediction_stats:
            try:
                snow_stats_updated_at = snow_prediction_stats.get("last_updated")
                if snow_stats_updated_at:
                    try:
                        snow_stats_updated_at = datetime.fromisoformat(
                            snow_stats_updated_at
                        )
                    except Exception:
                        snow_stats_updated_at = None
                existing_row = await self.db.fetchone(
                    "SELECT id FROM weather_expert_snow_stats WHERE id = 1"
                )
                if existing_row:
                    await self.db.execute(
                        "UPDATE weather_expert_snow_stats SET total_predictions = ?, correct_predictions = ?, accuracy = ?, last_updated = ? WHERE id = 1",
                        (
                            snow_prediction_stats.get("total_predictions", 0),
                            snow_prediction_stats.get("correct_predictions", 0),
                            snow_prediction_stats.get("accuracy", 0.0),
                            snow_stats_updated_at,
                        ),
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO weather_expert_snow_stats (id, total_predictions, correct_predictions, accuracy, last_updated) VALUES (1, ?, ?, ?, ?)",
                        (
                            snow_prediction_stats.get("total_predictions", 0),
                            snow_prediction_stats.get("correct_predictions", 0),
                            snow_prediction_stats.get("accuracy", 0.0),
                            snow_stats_updated_at,
                        ),
                    )
                    self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip snow prediction stats: {migration_error}")
        return None if True else None

    async def _migrate_weather_source_weights(self, data: Dict[str, Any]) -> None:
        weights = data.get("weights", {})
        learning_metadata = data.get("learning_metadata", {})
        last_mae_by_source = learning_metadata.get("last_mae", {})
        last_updated = learning_metadata.get("last_updated")
        if last_updated:
            try:
                last_updated = datetime.fromisoformat(last_updated)
            except Exception:
                last_updated = SafeDateTimeUtil.now()
        else:
            last_updated = SafeDateTimeUtil.now()
        last_learning_date = learning_metadata.get("last_learning_date")
        if last_learning_date:
            try:
                last_learning_date = datetime.fromisoformat(last_learning_date).date()
            except Exception:
                last_learning_date = None
        for source_name, weight in weights.items():
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO weather_source_weights\n                    (source_name, weight, last_mae, version, last_learning_date, comparison_hours,\n                     smoothing_factor_used, smoothing_factor_default, accelerated_learning, last_updated)\n                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source_name,
                        weight,
                        last_mae_by_source.get(source_name),
                        data.get("version", "1.1"),
                        last_learning_date,
                        learning_metadata.get("comparison_hours"),
                        learning_metadata.get("smoothing_factor_used"),
                        learning_metadata.get("smoothing_factor_default", 0.3),
                        learning_metadata.get("accelerated_learning", False),
                        last_updated,
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip weather source weight: {migration_error}")
                self.stats.skipped += 1
                continue

    async def _migrate_wttr_cache(self, data: Dict[str, Any]) -> None:
        await self._migrate_weather_cache_generic(
            data,
            "weather_cache_wttr_in",
            [
                "cloud_cover",
                "temperature",
                "humidity",
                "wind_speed",
                "precipitation",
                "pressure",
                "source",
            ],
        )

    async def _migrate_bright_sky_cache(self, data: Dict[str, Any]) -> None:
        await self._migrate_weather_cache_generic(
            data, "weather_cache_bright_sky", ["cloud_cover"]
        )

    async def _migrate_pirate_weather_cache(self, data: Dict[str, Any]) -> None:
        await self._migrate_weather_cache_generic(
            data, "weather_cache_pirate_weather", ["cloud_cover"]
        )

    async def _migrate_open_meteo_cache(self, data: Dict[str, Any]) -> None:
        await self._migrate_weather_cache_generic(
            data,
            "weather_cache_open_meteo",
            [
                "temperature",
                "cloud_cover",
                "cloud_cover_low",
                "cloud_cover_mid",
                "cloud_cover_high",
                "humidity",
                "wind_speed",
                "precipitation",
                "pressure",
                "direct_radiation",
                "diffuse_radiation",
                "ghi",
                "global_tilted_irradiance",
                "visibility_m",
                "source",
            ],
        )

    async def _migrate_weather_cache_generic(
        self, data: Dict[str, Any], table_name: str, fields: List[str]
    ) -> None:
        forecast_by_date = data.get("forecast", {})
        metadata = data.get("metadata", {})
        fetched_at = metadata.get("fetched_at") or metadata.get("last_updated")
        if fetched_at and isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at)
            except Exception:
                fetched_at = None
        batch_rows = []
        if isinstance(forecast_by_date, dict):
            for date_key, hourly_by_key in forecast_by_date.items():
                try:
                    forecast_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                if not isinstance(hourly_by_key, dict):
                    continue
                for hour_key, hourly_record in hourly_by_key.items():
                    try:
                        hour = int(hour_key)
                        if isinstance(hourly_record, dict):
                            field_values = [
                                _sql_value(hourly_record.get(field_name))
                                for field_name in fields
                            ]
                        elif (
                            isinstance(hourly_record, (int, float)) and len(fields) == 1
                        ):
                            field_values = [_sql_value(hourly_record)]
                        else:
                            continue
                        batch_rows.append(
                            (forecast_date, hour, *field_values, _sql_value(fetched_at))
                        )
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip weather cache entry: {migration_error}")
                        self.stats.skipped += 1
                        continue
                continue
        if batch_rows:
            try:
                placeholders = ", ".join(["?"] * (len(fields) + 3))
                columns = ", ".join(["forecast_date", "hour"] + fields + ["fetched_at"])
                inserted_count = await self.db.executemany(
                    f"INSERT OR REPLACE INTO {table_name} ({columns}) VALUES ({placeholders})",
                    batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(f"Batch insert {table_name} failed: {migration_error}")
                self.stats.errors += 1
        return None if True else None

    async def _migrate_hourly_predictions(self, data: Dict[str, Any]) -> None:
        predictions = data if isinstance(data, list) else data.get("predictions", [])
        prediction_count = len(predictions)
        if prediction_count > 0:
            _LOGGER.info(f"   → Migrating {prediction_count} hourly predictions...")
        processed_count = 0
        _batch_size = 100
        for _batch_index in range(0, prediction_count, _batch_size):
            _batch_rows = predictions[_batch_index : _batch_index + _batch_size]
            async with self.db.transaction():

                async def execute_no_commit(sql, params):
                    await self.db.execute(sql, params, auto_commit=False)

                for prediction_record in _batch_rows:
                    prediction_id = prediction_record.get(
                        "id"
                    ) or prediction_record.get("prediction_id")
                    if not prediction_id:
                        continue
                    try:
                        existing_prediction = await self.db.fetchone(
                            "SELECT id FROM hourly_predictions WHERE prediction_id = ?",
                            (prediction_id,),
                        )
                        already_exists = existing_prediction is not None
                        target_datetime = prediction_record.get("target_datetime")
                        if target_datetime and isinstance(target_datetime, str):
                            target_datetime = datetime.fromisoformat(target_datetime)
                        target_date = prediction_record.get("target_date")
                        if target_date and isinstance(target_date, str):
                            target_date = datetime.fromisoformat(target_date).date()
                        prediction_created_at = prediction_record.get(
                            "prediction_created_at"
                        )
                        if prediction_created_at and isinstance(
                            prediction_created_at, str
                        ):
                            prediction_created_at = datetime.fromisoformat(
                                prediction_created_at
                            )
                        actual_measured_at = prediction_record.get("actual_measured_at")
                        if actual_measured_at and isinstance(actual_measured_at, str):
                            actual_measured_at = datetime.fromisoformat(
                                actual_measured_at
                            )
                        flags = prediction_record.get("flags", {})
                        quality = prediction_record.get("quality", {})
                        if not already_exists:
                            await execute_no_commit(
                                "INSERT INTO hourly_predictions (prediction_id, prediction_created_at, prediction_created_hour, target_datetime, target_date, target_hour, target_day_of_week, target_day_of_year, target_month, target_season, prediction_kwh, prediction_kwh_uncapped, prediction_method, ml_contribution_percent, model_version, confidence, actual_kwh, actual_measured_at, accuracy_percent, error_kwh, error_percent, is_production_hour, is_peak_hour, is_outlier, has_weather_alert, has_sensor_data, sensor_data_complete, weather_forecast_updated, manual_override, inverter_clipped, has_panel_group_predictions, prediction_confidence, weather_forecast_age_hours, sensor_data_quality, data_completeness_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    prediction_id,
                                    prediction_created_at,
                                    prediction_record.get("prediction_created_hour"),
                                    target_datetime,
                                    target_date,
                                    prediction_record.get("target_hour"),
                                    prediction_record.get("target_day_of_week"),
                                    prediction_record.get("target_day_of_year"),
                                    prediction_record.get("target_month"),
                                    prediction_record.get("target_season"),
                                    prediction_record.get("prediction_kwh", 0),
                                    prediction_record.get("prediction_kwh_uncapped"),
                                    prediction_record.get("prediction_method"),
                                    prediction_record.get("ml_contribution_percent"),
                                    prediction_record.get("model_version"),
                                    prediction_record.get("confidence"),
                                    prediction_record.get("actual_kwh"),
                                    actual_measured_at,
                                    prediction_record.get("accuracy_percent"),
                                    prediction_record.get("error_kwh"),
                                    prediction_record.get("error_percent"),
                                    flags.get("is_production_hour", False),
                                    flags.get("is_peak_hour", False),
                                    flags.get("is_outlier", False),
                                    flags.get("has_weather_alert", False),
                                    flags.get("has_sensor_data", False),
                                    flags.get("sensor_data_complete", False),
                                    flags.get("weather_forecast_updated", False),
                                    flags.get("manual_override", False),
                                    flags.get("inverter_clipped", False),
                                    flags.get("has_panel_group_predictions", False),
                                    quality.get("prediction_confidence"),
                                    quality.get("weather_forecast_age_hours"),
                                    quality.get("sensor_data_quality"),
                                    quality.get("data_completeness_percent"),
                                ),
                            )
                            self.stats.imported += 1
                        else:
                            actual_kwh = prediction_record.get("actual_kwh")
                            if actual_kwh is not None:
                                await execute_no_commit(
                                    "UPDATE hourly_predictions SET\n                                       actual_kwh = ?,\n                                       actual_measured_at = ?,\n                                       accuracy_percent = ?,\n                                       error_kwh = ?,\n                                       error_percent = ?\n                                    WHERE prediction_id = ?\n                                      AND (actual_kwh IS NULL OR actual_kwh = 0)",
                                    (
                                        actual_kwh,
                                        actual_measured_at,
                                        prediction_record.get("accuracy_percent"),
                                        prediction_record.get("error_kwh"),
                                        prediction_record.get("error_percent"),
                                        prediction_id,
                                    ),
                                )
                                self.stats.updated += 1
                            else:
                                self.stats.skipped += 1
                        sensor_actual = prediction_record.get("sensor_actual", {})
                        if sensor_actual and isinstance(sensor_actual, dict):
                            try:
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO prediction_sensor_actual (prediction_id, temperature_c, humidity_percent, solar_radiation_wm2, rain_mm, uv_index, wind_speed_ms, current_yield_kwh, lux) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        prediction_id,
                                        sensor_actual.get("temperature_c"),
                                        sensor_actual.get("humidity_percent"),
                                        sensor_actual.get("solar_radiation_wm2"),
                                        sensor_actual.get("rain_mm"),
                                        sensor_actual.get("uv_index"),
                                        sensor_actual.get("wind_speed_ms"),
                                        sensor_actual.get("current_yield_kwh"),
                                        sensor_actual.get("lux"),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception:
                                pass
                        for weather_type, weather_key in (
                            ("forecast", "weather_forecast"),
                            ("corrected", "weather_corrected"),
                            ("actual", "weather_actual"),
                        ):
                            weather_record = prediction_record.get(weather_key)
                            if weather_record and isinstance(weather_record, dict):
                                try:
                                    if weather_type == "actual":
                                        temperature = weather_record.get(
                                            "temperature_c"
                                        )
                                        wind = weather_record.get("wind_speed_ms")
                                        humidity = weather_record.get(
                                            "humidity_percent"
                                        )
                                        rain = weather_record.get("precipitation_mm")
                                        clouds = weather_record.get(
                                            "cloud_cover_percent"
                                        )
                                        pressure = weather_record.get("pressure_hpa")
                                        lux = weather_record.get("lux")
                                        frost_detected = weather_record.get(
                                            "frost_detected"
                                        )
                                        frost_score = weather_record.get("frost_score")
                                        frost_confidence = weather_record.get(
                                            "frost_confidence"
                                        )
                                    else:
                                        temperature = weather_record.get("temperature")
                                        wind = weather_record.get("wind")
                                        humidity = weather_record.get("humidity")
                                        rain = weather_record.get("rain")
                                        clouds = weather_record.get("clouds")
                                        pressure = weather_record.get("pressure")
                                        lux = None
                                        frost_detected = None
                                        frost_score = None
                                        frost_confidence = None
                                    await execute_no_commit(
                                        "INSERT OR REPLACE INTO prediction_weather (prediction_id, weather_type, temperature, solar_radiation_wm2, wind, humidity, rain, clouds, pressure, source, lux, frost_detected, frost_score, frost_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            prediction_id,
                                            weather_type,
                                            _sql_value(temperature),
                                            _sql_value(
                                                weather_record.get(
                                                    "solar_radiation_wm2"
                                                )
                                            ),
                                            _sql_value(wind),
                                            _sql_value(humidity),
                                            _sql_value(rain),
                                            _sql_value(clouds),
                                            _sql_value(pressure),
                                            _sql_value(weather_record.get("source")),
                                            _sql_value(lux),
                                            _sql_value(frost_detected),
                                            _sql_value(frost_score),
                                            _sql_value(frost_confidence),
                                        ),
                                    )
                                    self.stats.imported += 1
                                except Exception:
                                    continue
                        astronomy = prediction_record.get("astronomy")
                        if astronomy and isinstance(astronomy, dict):
                            try:
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO prediction_astronomy (prediction_id, sunrise, sunset, solar_noon, daylight_hours, sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2, theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio, hours_after_sunrise, hours_before_sunset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        prediction_id,
                                        _sql_value(astronomy.get("sunrise")),
                                        _sql_value(astronomy.get("sunset")),
                                        _sql_value(astronomy.get("solar_noon")),
                                        _sql_value(astronomy.get("daylight_hours")),
                                        _sql_value(astronomy.get("sun_elevation_deg")),
                                        _sql_value(astronomy.get("sun_azimuth_deg")),
                                        _sql_value(
                                            astronomy.get("clear_sky_radiation_wm2")
                                        ),
                                        _sql_value(
                                            astronomy.get("theoretical_max_kwh")
                                        ),
                                        _sql_value(
                                            astronomy.get("hours_since_solar_noon")
                                        ),
                                        _sql_value(astronomy.get("day_progress_ratio")),
                                        _sql_value(
                                            astronomy.get("hours_after_sunrise")
                                        ),
                                        _sql_value(
                                            astronomy.get("hours_before_sunset")
                                        ),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception:
                                pass
                        group_predictions = (
                            prediction_record.get("panel_group_predictions") or {}
                        )
                        group_actuals = (
                            prediction_record.get("panel_group_actuals") or {}
                        )
                        group_names = set(group_predictions.keys()) | set(
                            group_actuals.keys()
                        )
                        for group_name in group_names:
                            group_prediction_kwh = group_predictions.get(group_name)
                            group_actual_kwh = group_actuals.get(group_name)
                            if (
                                group_prediction_kwh is not None
                                or group_actual_kwh is not None
                            ):
                                try:
                                    await execute_no_commit(
                                        "INSERT OR REPLACE INTO prediction_panel_groups (prediction_id, group_name, prediction_kwh, actual_kwh) VALUES (?, ?, ?, ?)",
                                        (
                                            prediction_id,
                                            group_name,
                                            group_prediction_kwh or 0,
                                            group_actual_kwh,
                                        ),
                                    )
                                    self.stats.imported += 1
                                except Exception:
                                    continue
                        shadow_detection = prediction_record.get("shadow_detection")
                        if shadow_detection and isinstance(shadow_detection, dict):
                            try:
                                shadow_methods = shadow_detection.get("methods", {})
                                theory_ratio = shadow_methods.get("theory_ratio", {})
                                sensor_fusion = shadow_methods.get("sensor_fusion", {})
                                shadow_weights = shadow_detection.get("weights", {})
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO hourly_shadow_detection (\n                                        prediction_id, method, ensemble_mode, shadow_type, shadow_percent,\n                                        confidence, root_cause, fusion_mode, efficiency_ratio, loss_kwh,\n                                        theoretical_max_kwh, interpretation,\n                                        theory_ratio_shadow_type, theory_ratio_shadow_percent, theory_ratio_confidence,\n                                        theory_ratio_efficiency_ratio, theory_ratio_clear_sky_wm2, theory_ratio_actual_wm2,\n                                        theory_ratio_loss_kwh, theory_ratio_root_cause,\n                                        sensor_fusion_shadow_type, sensor_fusion_shadow_percent, sensor_fusion_confidence,\n                                        sensor_fusion_efficiency_ratio, sensor_fusion_loss_kwh, sensor_fusion_root_cause,\n                                        sensor_fusion_lux_factor, sensor_fusion_lux_shadow_percent,\n                                        sensor_fusion_irradiance_factor, sensor_fusion_irradiance_shadow_percent,\n                                        weight_theory_ratio, weight_sensor_fusion\n                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        prediction_id,
                                        _sql_value(shadow_detection.get("method")),
                                        _sql_value(
                                            shadow_detection.get("ensemble_mode")
                                        ),
                                        _sql_value(shadow_detection.get("shadow_type")),
                                        _sql_value(
                                            shadow_detection.get("shadow_percent")
                                        ),
                                        _sql_value(shadow_detection.get("confidence")),
                                        _sql_value(shadow_detection.get("root_cause")),
                                        _sql_value(shadow_detection.get("fusion_mode")),
                                        _sql_value(
                                            shadow_detection.get("efficiency_ratio")
                                        ),
                                        _sql_value(shadow_detection.get("loss_kwh")),
                                        _sql_value(
                                            shadow_detection.get("theoretical_max_kwh")
                                        ),
                                        _sql_value(
                                            shadow_detection.get("interpretation")
                                        ),
                                        _sql_value(theory_ratio.get("shadow_type")),
                                        _sql_value(theory_ratio.get("shadow_percent")),
                                        _sql_value(theory_ratio.get("confidence")),
                                        _sql_value(
                                            theory_ratio.get("efficiency_ratio")
                                        ),
                                        _sql_value(theory_ratio.get("clear_sky_wm2")),
                                        _sql_value(theory_ratio.get("actual_wm2")),
                                        _sql_value(theory_ratio.get("loss_kwh")),
                                        _sql_value(theory_ratio.get("root_cause")),
                                        _sql_value(sensor_fusion.get("shadow_type")),
                                        _sql_value(sensor_fusion.get("shadow_percent")),
                                        _sql_value(sensor_fusion.get("confidence")),
                                        _sql_value(
                                            sensor_fusion.get("efficiency_ratio")
                                        ),
                                        _sql_value(sensor_fusion.get("loss_kwh")),
                                        _sql_value(sensor_fusion.get("root_cause")),
                                        _sql_value(sensor_fusion.get("lux_factor")),
                                        _sql_value(
                                            sensor_fusion.get("lux_shadow_percent")
                                        ),
                                        _sql_value(
                                            sensor_fusion.get("irradiance_factor")
                                        ),
                                        _sql_value(
                                            sensor_fusion.get(
                                                "irradiance_shadow_percent"
                                            )
                                        ),
                                        _sql_value(shadow_weights.get("theory_ratio")),
                                        _sql_value(shadow_weights.get("sensor_fusion")),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception as migration_error:
                                _LOGGER.debug(
                                    f"Skip shadow_detection: {migration_error}"
                                )
                        production_metrics = prediction_record.get("production_metrics")
                        if production_metrics and isinstance(production_metrics, dict):
                            try:
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO hourly_production_metrics (\n                                        prediction_id, peak_power_today_kwh, production_hours_today, cumulative_today_kwh\n                                    ) VALUES (?, ?, ?, ?)",
                                    (
                                        prediction_id,
                                        _sql_value(
                                            production_metrics.get(
                                                "peak_power_today_kwh"
                                            )
                                        ),
                                        _sql_value(
                                            production_metrics.get(
                                                "production_hours_today"
                                            )
                                        ),
                                        _sql_value(
                                            production_metrics.get(
                                                "cumulative_today_kwh"
                                            )
                                        ),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception as migration_error:
                                _LOGGER.debug(
                                    f"Skip production_metrics: {migration_error}"
                                )
                        historical_context = prediction_record.get("historical_context")
                        if historical_context and isinstance(historical_context, dict):
                            try:
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO hourly_historical_context (\n                                        prediction_id, yesterday_same_hour, same_hour_avg_7days\n                                    ) VALUES (?, ?, ?)",
                                    (
                                        prediction_id,
                                        _sql_value(
                                            historical_context.get(
                                                "yesterday_same_hour"
                                            )
                                        ),
                                        _sql_value(
                                            historical_context.get(
                                                "same_hour_avg_7days"
                                            )
                                        ),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception as migration_error:
                                _LOGGER.debug(
                                    f"Skip historical_context: {migration_error}"
                                )
                        group_accuracy = prediction_record.get("panel_group_accuracy")
                        if group_accuracy and isinstance(group_accuracy, dict):
                            for (
                                group_name,
                                group_accuracy_record,
                            ) in group_accuracy.items():
                                if group_accuracy_record and isinstance(
                                    group_accuracy_record, dict
                                ):
                                    try:
                                        await execute_no_commit(
                                            "INSERT OR REPLACE INTO hourly_panel_group_accuracy (\n                                                prediction_id, group_name, prediction_kwh, actual_kwh,\n                                                error_kwh, error_percent, accuracy_percent\n                                            ) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                            (
                                                prediction_id,
                                                group_name,
                                                _sql_value(
                                                    group_accuracy_record.get(
                                                        "prediction_kwh"
                                                    )
                                                ),
                                                _sql_value(
                                                    group_accuracy_record.get(
                                                        "actual_kwh"
                                                    )
                                                ),
                                                _sql_value(
                                                    group_accuracy_record.get(
                                                        "error_kwh"
                                                    )
                                                ),
                                                _sql_value(
                                                    group_accuracy_record.get(
                                                        "error_percent"
                                                    )
                                                ),
                                                _sql_value(
                                                    group_accuracy_record.get(
                                                        "accuracy_percent"
                                                    )
                                                ),
                                            ),
                                        )
                                        self.stats.imported += 1
                                    except Exception as migration_error:
                                        _LOGGER.debug(
                                            f"Skip panel_group_accuracy for {group_name}: {migration_error}"
                                        )
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip hourly prediction: {migration_error}")
                        self.stats.skipped += 1
                        continue
                    processed_count += 1
        if prediction_count > 500:
            progress_percent = processed_count / prediction_count * 100
            _LOGGER.info(
                f"   → Progress: {processed_count}/{prediction_count} ({progress_percent:.1f}%)"
            )
        return None if True else None

    async def _migrate_daily_forecasts(self, data: Dict[str, Any]) -> None:
        if False:
            __assert_armored__ = None
        forecast_history = data.get("history", [])
        today_forecasts = data.get("today", {})
        forecast_count = len(forecast_history) + (1 if today_forecasts else 0)
        if forecast_count > 0:
            _LOGGER.info(f"   → Migrating {forecast_count} daily forecasts...")
        if today_forecasts and today_forecasts.get("date"):
            try:
                today_date = today_forecasts.get("date")
                if isinstance(today_date, str):
                    today_date = datetime.fromisoformat(today_date).date()
                forecast_type_mapping = {
                    "forecast_day": "today",
                    "forecast_tomorrow": "tomorrow",
                    "forecast_day_after_tomorrow": "day_after_tomorrow",
                }
                for forecast_key, forecast_type in forecast_type_mapping.items():
                    forecast_record = today_forecasts.get(forecast_key)
                    if forecast_record and isinstance(forecast_record, dict):
                        prediction_kwh = forecast_record.get("prediction_kwh")
                        raw_prediction_kwh = forecast_record.get(
                            "prediction_kwh_raw", prediction_kwh
                        )
                        safeguard_applied = forecast_record.get(
                            "safeguard_applied", False
                        )
                        safeguard_reduction_kwh = forecast_record.get(
                            "safeguard_reduction_kwh"
                        )
                        locked = forecast_record.get("locked", False)
                        locked_at = forecast_record.get("locked_at")
                        forecast_source = forecast_record.get("source")
                        forecast_record_date = forecast_record.get("date")
                        if forecast_record_date and isinstance(
                            forecast_record_date, str
                        ):
                            forecast_record_date = datetime.fromisoformat(
                                forecast_record_date
                            ).date()
                        else:
                            forecast_record_date = today_date
                        if locked_at and isinstance(locked_at, str):
                            locked_at = datetime.fromisoformat(locked_at)
                        if prediction_kwh is not None:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_forecasts (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw, safeguard_applied, safeguard_reduction_kwh, locked, locked_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    forecast_type,
                                    forecast_record_date,
                                    prediction_kwh,
                                    raw_prediction_kwh,
                                    safeguard_applied,
                                    safeguard_reduction_kwh,
                                    locked,
                                    locked_at,
                                    forecast_source,
                                ),
                            )
                            self.stats.imported += 1
                        forecast_updates = forecast_record.get("updates", [])
                        for forecast_update in forecast_updates:
                            updated_prediction_kwh = forecast_update.get(
                                "prediction_kwh"
                            )
                            forecast_updated_at = forecast_update.get("updated_at")
                            update_source = forecast_update.get("source")
                            if forecast_updated_at and isinstance(
                                forecast_updated_at, str
                            ):
                                forecast_updated_at = datetime.fromisoformat(
                                    forecast_updated_at
                                )
                            if updated_prediction_kwh is not None:
                                await self.db.execute(
                                    "INSERT INTO daily_forecast_updates (forecast_type, forecast_date, prediction_kwh, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                                    (
                                        forecast_type,
                                        forecast_record_date,
                                        updated_prediction_kwh,
                                        update_source,
                                        forecast_updated_at,
                                    ),
                                )
                                self.stats.imported += 1
                        continue
            except Exception as migration_error:
                _LOGGER.debug(f"Skip today forecast: {migration_error}")
        for history_forecast in forecast_history:
            history_date_value = history_forecast.get("date")
            if not history_date_value:
                continue
            try:
                history_date = (
                    datetime.fromisoformat(history_date_value).date()
                    if isinstance(history_date_value, str)
                    else history_date_value
                )
                history_predicted_kwh = history_forecast.get("predicted_kwh")
                history_actual_kwh = history_forecast.get("actual_kwh")
                history_accuracy = history_forecast.get("accuracy")
                history_production_hours = history_forecast.get("production_hours")
                history_peak_power_w = history_forecast.get("peak_power_w")
                forecast_source = history_forecast.get("forecast_source")
                existing_summary = await self.db.fetchone(
                    "SELECT id FROM daily_summaries WHERE date = ?", (history_date,)
                )
                if existing_summary:
                    await self.db.execute(
                        "UPDATE daily_summaries SET predicted_total_kwh = COALESCE(predicted_total_kwh, ?), actual_total_kwh = COALESCE(actual_total_kwh, ?), accuracy_percent = COALESCE(accuracy_percent, ?), production_hours = COALESCE(production_hours, ?), peak_power_w = COALESCE(peak_power_w, ?) WHERE id = ?",
                        (
                            history_predicted_kwh,
                            history_actual_kwh,
                            history_accuracy,
                            history_production_hours,
                            history_peak_power_w,
                            existing_summary[0],
                        ),
                    )
                    self.stats.updated += 1
                    continue
                await self.db.execute(
                    "INSERT INTO daily_summaries (date, predicted_total_kwh, actual_total_kwh, accuracy_percent, production_hours, peak_power_w) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        history_date,
                        history_predicted_kwh,
                        history_actual_kwh,
                        history_accuracy,
                        history_production_hours,
                        history_peak_power_w,
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip daily forecast history: {migration_error}")
                self.stats.skipped += 1
                continue
        tracking_date = data.get("date")
        if tracking_date:
            try:
                if isinstance(tracking_date, str):
                    tracking_date = datetime.fromisoformat(tracking_date).date()
                forecast_best_hour = data.get("forecast_best_hour", {})
                actual_best_hour = data.get("actual_best_hour", {})
                forecast_next_hour = data.get("forecast_next_hour", {})
                production_time = data.get("production_time", {})
                peak_today = data.get("peak_today", {})
                yield_today = data.get("yield_today", {})
                consumption_today = data.get("consumption_today", {})
                autarky = data.get("autarky", {})
                finalized = data.get("finalized", {})
                excluded_hours = finalized.get("excluded_hours", {})
                forecast_best_hour_locked_at = forecast_best_hour.get("locked_at")
                if forecast_best_hour_locked_at and isinstance(
                    forecast_best_hour_locked_at, str
                ):
                    forecast_best_hour_locked_at = datetime.fromisoformat(
                        forecast_best_hour_locked_at
                    )
                actual_best_hour_saved_at = actual_best_hour.get("saved_at")
                if actual_best_hour_saved_at and isinstance(
                    actual_best_hour_saved_at, str
                ):
                    actual_best_hour_saved_at = datetime.fromisoformat(
                        actual_best_hour_saved_at
                    )
                next_hour_updated_at = forecast_next_hour.get("updated_at")
                if next_hour_updated_at and isinstance(next_hour_updated_at, str):
                    next_hour_updated_at = datetime.fromisoformat(next_hour_updated_at)
                production_start = production_time.get("start_time")
                if production_start and isinstance(production_start, str):
                    production_start = datetime.fromisoformat(production_start)
                production_end = production_time.get("end_time")
                if production_end and isinstance(production_end, str):
                    production_end = datetime.fromisoformat(production_end)
                last_power_above_10w = production_time.get("last_power_above_10w")
                if last_power_above_10w and isinstance(last_power_above_10w, str):
                    last_power_above_10w = datetime.fromisoformat(last_power_above_10w)
                zero_power_since = production_time.get("zero_power_since")
                if zero_power_since and isinstance(zero_power_since, str):
                    zero_power_since = datetime.fromisoformat(zero_power_since)
                peak_today_at = peak_today.get("at")
                if peak_today_at and isinstance(peak_today_at, str):
                    peak_today_at = datetime.fromisoformat(peak_today_at)
                autarky_calculated_at = autarky.get("calculated_at")
                if autarky_calculated_at and isinstance(autarky_calculated_at, str):
                    autarky_calculated_at = datetime.fromisoformat(
                        autarky_calculated_at
                    )
                finalized_at = finalized.get("at")
                if finalized_at and isinstance(finalized_at, str):
                    finalized_at = datetime.fromisoformat(finalized_at)
                await self.db.execute(
                    "INSERT OR REPLACE INTO daily_forecast_tracking (\n                        date, forecast_best_hour, forecast_best_hour_kwh, forecast_best_hour_locked,\n                        forecast_best_hour_locked_at, forecast_best_hour_source,\n                        actual_best_hour, actual_best_hour_kwh, actual_best_hour_saved_at,\n                        forecast_next_hour_period, forecast_next_hour_kwh, forecast_next_hour_updated_at,\n                        forecast_next_hour_source, production_time_active, production_time_duration_seconds,\n                        production_time_start, production_time_end, production_time_last_power_above_10w,\n                        production_time_zero_power_since, peak_today_power_w, peak_today_at,\n                        yield_today_kwh, yield_today_sensor, consumption_today_kwh, consumption_today_sensor,\n                        autarky_percent, autarky_calculated_at, finalized_yield_kwh, finalized_consumption_kwh,\n                        finalized_production_hours, finalized_accuracy_percent, finalized_excluded_hours_count,\n                        finalized_excluded_hours_total, finalized_excluded_hours_ratio,\n                        finalized_excluded_hours_reasons, finalized_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tracking_date,
                        _sql_value(forecast_best_hour.get("hour")),
                        _sql_value(forecast_best_hour.get("prediction_kwh")),
                        _sql_value(forecast_best_hour.get("locked")),
                        forecast_best_hour_locked_at,
                        _sql_value(forecast_best_hour.get("source")),
                        _sql_value(actual_best_hour.get("hour")),
                        _sql_value(actual_best_hour.get("actual_kwh")),
                        actual_best_hour_saved_at,
                        _sql_value(forecast_next_hour.get("period")),
                        _sql_value(forecast_next_hour.get("prediction_kwh")),
                        next_hour_updated_at,
                        _sql_value(forecast_next_hour.get("source")),
                        _sql_value(production_time.get("active")),
                        _sql_value(production_time.get("duration_seconds")),
                        production_start,
                        production_end,
                        last_power_above_10w,
                        zero_power_since,
                        _sql_value(peak_today.get("power_w")),
                        peak_today_at,
                        _sql_value(yield_today.get("kwh")),
                        _sql_value(yield_today.get("sensor")),
                        _sql_value(consumption_today.get("kwh")),
                        _sql_value(consumption_today.get("sensor")),
                        _sql_value(autarky.get("percent")),
                        autarky_calculated_at,
                        _sql_value(finalized.get("yield_kwh")),
                        _sql_value(finalized.get("consumption_kwh")),
                        _sql_value(finalized.get("production_hours")),
                        _sql_value(finalized.get("accuracy_percent")),
                        _sql_value(excluded_hours.get("count")),
                        _sql_value(excluded_hours.get("total")),
                        _sql_value(excluded_hours.get("ratio")),
                        _sql_value(excluded_hours.get("reasons")),
                        finalized_at,
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip daily_forecast_tracking: {migration_error}")
        statistics = data.get("statistics", {})
        if statistics:
            try:
                all_time_peak = statistics.get("all_time_peak", {})
                current_week = statistics.get("current_week", {})
                current_month = statistics.get("current_month", {})
                last_7_days = statistics.get("last_7_days", {})
                last_30_days = statistics.get("last_30_days", {})
                last_365_days = statistics.get("last_365_days", {})
                all_time_peak_date = all_time_peak.get("date")
                if all_time_peak_date and isinstance(all_time_peak_date, str):
                    all_time_peak_date = datetime.fromisoformat(
                        all_time_peak_date
                    ).date()
                all_time_peak_at = all_time_peak.get("at")
                if all_time_peak_at and isinstance(all_time_peak_at, str):
                    all_time_peak_at = datetime.fromisoformat(all_time_peak_at)
                current_week_updated_at = current_week.get("updated_at")
                if current_week_updated_at and isinstance(current_week_updated_at, str):
                    current_week_updated_at = datetime.fromisoformat(
                        current_week_updated_at
                    )
                current_month_updated_at = current_month.get("updated_at")
                if current_month_updated_at and isinstance(
                    current_month_updated_at, str
                ):
                    current_month_updated_at = datetime.fromisoformat(
                        current_month_updated_at
                    )
                last_7_days_calculated_at = last_7_days.get("calculated_at")
                if last_7_days_calculated_at and isinstance(
                    last_7_days_calculated_at, str
                ):
                    last_7_days_calculated_at = datetime.fromisoformat(
                        last_7_days_calculated_at
                    )
                last_30_days_calculated_at = last_30_days.get("calculated_at")
                if last_30_days_calculated_at and isinstance(
                    last_30_days_calculated_at, str
                ):
                    last_30_days_calculated_at = datetime.fromisoformat(
                        last_30_days_calculated_at
                    )
                last_365_days_calculated_at = last_365_days.get("calculated_at")
                if last_365_days_calculated_at and isinstance(
                    last_365_days_calculated_at, str
                ):
                    last_365_days_calculated_at = datetime.fromisoformat(
                        last_365_days_calculated_at
                    )
                await self.db.execute(
                    "INSERT OR REPLACE INTO daily_statistics (\n                        id, all_time_peak_power_w, all_time_peak_date, all_time_peak_at,\n                        current_week_period, current_week_date_range, current_week_yield_kwh,\n                        current_week_consumption_kwh, current_week_days, current_week_updated_at,\n                        current_month_period, current_month_yield_kwh, current_month_consumption_kwh,\n                        current_month_avg_autarky, current_month_days, current_month_updated_at,\n                        last_7_days_avg_yield_kwh, last_7_days_avg_accuracy, last_7_days_total_yield_kwh, last_7_days_calculated_at,\n                        last_30_days_avg_yield_kwh, last_30_days_avg_accuracy, last_30_days_total_yield_kwh, last_30_days_calculated_at,\n                        last_365_days_avg_yield_kwh, last_365_days_total_yield_kwh, last_365_days_calculated_at\n                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _sql_value(all_time_peak.get("power_w")),
                        all_time_peak_date,
                        all_time_peak_at,
                        _sql_value(current_week.get("period")),
                        _sql_value(current_week.get("date_range")),
                        _sql_value(current_week.get("yield_kwh")),
                        _sql_value(current_week.get("consumption_kwh")),
                        _sql_value(current_week.get("days")),
                        current_week_updated_at,
                        _sql_value(current_month.get("period")),
                        _sql_value(current_month.get("yield_kwh")),
                        _sql_value(current_month.get("consumption_kwh")),
                        _sql_value(current_month.get("avg_autarky")),
                        _sql_value(current_month.get("days")),
                        current_month_updated_at,
                        _sql_value(last_7_days.get("avg_yield_kwh")),
                        _sql_value(last_7_days.get("avg_accuracy")),
                        _sql_value(last_7_days.get("total_yield_kwh")),
                        last_7_days_calculated_at,
                        _sql_value(last_30_days.get("avg_yield_kwh")),
                        _sql_value(last_30_days.get("avg_accuracy")),
                        _sql_value(last_30_days.get("total_yield_kwh")),
                        last_30_days_calculated_at,
                        _sql_value(last_365_days.get("avg_yield_kwh")),
                        _sql_value(last_365_days.get("total_yield_kwh")),
                        last_365_days_calculated_at,
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip daily_statistics: {migration_error}")
        return None if True else None

    async def _migrate_daily_summaries(self, data: Dict[str, Any]) -> None:
        summaries = data if isinstance(data, list) else data.get("summaries", [])
        for summary_record in summaries:
            record_date = summary_record.get("date")
            if not record_date:
                continue
            try:
                if isinstance(record_date, str):
                    record_date = datetime.fromisoformat(record_date).date()
                overall_summary = summary_record.get("overall", {})
                if not overall_summary:
                    overall_summary = summary_record
                existing_row = await self.db.fetchone(
                    "SELECT id FROM daily_summaries WHERE date = ?", (record_date,)
                )
                predicted_total_kwh = overall_summary.get(
                    "predicted_total_kwh"
                ) or overall_summary.get("predicted_kwh")
                actual_total_kwh = overall_summary.get(
                    "actual_total_kwh"
                ) or overall_summary.get("actual_kwh")
                accuracy_percent = overall_summary.get(
                    "accuracy_percent"
                ) or overall_summary.get("accuracy")
                error_kwh = overall_summary.get("error_kwh") or overall_summary.get(
                    "absolute_error_kwh"
                )
                production_hours = overall_summary.get("production_hours")
                peak_power_w = overall_summary.get("peak_power_w")
                peak_hour = overall_summary.get("peak_hour")
                if existing_row:
                    await self.db.execute(
                        "UPDATE daily_summaries SET predicted_total_kwh = ?, actual_total_kwh = ?, accuracy_percent = ?, error_kwh = ?, production_hours = ?, peak_power_w = ?, peak_hour = ? WHERE id = ?",
                        (
                            predicted_total_kwh,
                            actual_total_kwh,
                            accuracy_percent,
                            error_kwh,
                            production_hours,
                            peak_power_w,
                            peak_hour,
                            existing_row[0],
                        ),
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO daily_summaries (date, predicted_total_kwh, actual_total_kwh, accuracy_percent, error_kwh, production_hours, peak_power_w, peak_hour, month, season) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record_date,
                            predicted_total_kwh,
                            actual_total_kwh,
                            accuracy_percent,
                            error_kwh,
                            production_hours,
                            peak_power_w,
                            peak_hour,
                            summary_record.get("month"),
                            summary_record.get("season"),
                        ),
                    )
                    self.stats.imported += 1
                time_windows = summary_record.get("time_windows", {})
                if time_windows and isinstance(time_windows, dict):
                    for window_name, window_record in time_windows.items():
                        if not isinstance(window_record, dict):
                            continue
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_summary_time_windows (date, window_name, predicted_kwh, actual_kwh, accuracy, stable, hours_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    record_date,
                                    window_name,
                                    window_record.get("predicted_kwh"),
                                    window_record.get("actual_kwh"),
                                    window_record.get("accuracy"),
                                    window_record.get("stable"),
                                    window_record.get("hours_count"),
                                ),
                            )
                            self.stats.imported += 1
                        except Exception:
                            pass
                frost_analysis = summary_record.get("frost_analysis", {})
                if frost_analysis:
                    if isinstance(frost_analysis, dict):
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_summary_frost_analysis (date, hours_analyzed, frost_detected, total_affected_hours, heavy_frost_hours, light_frost_hours) VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    record_date,
                                    frost_analysis.get("hours_analyzed"),
                                    frost_analysis.get("frost_detected"),
                                    frost_analysis.get("total_affected_hours"),
                                    frost_analysis.get("heavy_frost_hours"),
                                    frost_analysis.get("light_frost_hours"),
                                ),
                            )
                            self.stats.imported += 1
                        except Exception:
                            pass
            except Exception as migration_error:
                _LOGGER.debug(f"Skip daily summary: {migration_error}")
                self.stats.skipped += 1

    async def _migrate_weather_forecast(self, data: Dict[str, Any]) -> None:
        forecast_by_date = data.get("forecast", {})
        batch_rows = []
        if isinstance(forecast_by_date, dict):
            for date_key, hourly_by_key in forecast_by_date.items():
                try:
                    forecast_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                if not isinstance(hourly_by_key, dict):
                    continue
                for hour_key, hourly_forecast in hourly_by_key.items():
                    if not isinstance(hourly_forecast, dict):
                        continue
                    try:
                        hour = int(hour_key)
                        batch_rows.append(
                            (
                                forecast_date,
                                hour,
                                _sql_value(hourly_forecast.get("temperature")),
                                _sql_value(hourly_forecast.get("solar_radiation_wm2")),
                                _sql_value(hourly_forecast.get("wind")),
                                _sql_value(hourly_forecast.get("humidity")),
                                _sql_value(hourly_forecast.get("rain")),
                                _sql_value(hourly_forecast.get("clouds")),
                                _sql_value(hourly_forecast.get("cloud_cover_low")),
                                _sql_value(hourly_forecast.get("cloud_cover_mid")),
                                _sql_value(hourly_forecast.get("cloud_cover_high")),
                                _sql_value(hourly_forecast.get("pressure")),
                                _sql_value(hourly_forecast.get("direct_radiation")),
                                _sql_value(hourly_forecast.get("diffuse_radiation")),
                                _sql_value(hourly_forecast.get("visibility_m")),
                                _sql_value(hourly_forecast.get("fog_detected")),
                                _sql_value(hourly_forecast.get("fog_type")),
                            )
                        )
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip weather forecast: {migration_error}")
                        self.stats.skipped += 1
                        continue
                    continue
                continue
        if batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO weather_forecast (forecast_date, hour, temperature, solar_radiation_wm2, wind, humidity, rain, clouds, cloud_cover_low, cloud_cover_mid, cloud_cover_high, pressure, direct_radiation, diffuse_radiation, visibility_m, fog_detected, fog_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert weather forecast failed: {migration_error}"
                )
                self.stats.errors += 1
        return None if True else None

    async def _migrate_astronomy_cache(self, data: Dict[str, Any]) -> None:
        days = data.get("days", {})
        batch_rows = []
        if isinstance(days, dict):
            for date_key, day_record in days.items():
                if not isinstance(day_record, dict):
                    continue
                try:
                    cache_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                sunrise = _sql_value(day_record.get("sunrise_local"))
                sunset = _sql_value(day_record.get("sunset_local"))
                solar_noon = _sql_value(day_record.get("solar_noon_local"))
                daylight_hours = _sql_value(day_record.get("daylight_hours"))
                hourly_records = day_record.get("hourly", [])
                if isinstance(hourly_records, list):
                    for hour_index, hourly_record in enumerate(hourly_records):
                        if not isinstance(hourly_record, dict):
                            continue
                        try:
                            batch_rows.append(
                                (
                                    cache_date,
                                    hour_index,
                                    _sql_value(hourly_record.get("elevation_deg")),
                                    _sql_value(hourly_record.get("azimuth_deg")),
                                    _sql_value(
                                        hourly_record.get(
                                            "clear_sky_solar_radiation_wm2"
                                        )
                                    ),
                                    _sql_value(
                                        hourly_record.get("theoretical_max_pv_kwh")
                                    ),
                                    sunrise,
                                    sunset,
                                    solar_noon,
                                    daylight_hours,
                                )
                            )
                        except Exception as migration_error:
                            _LOGGER.debug(
                                f"Skip astronomy cache hour: {migration_error}"
                            )
                            self.stats.skipped += 1
                            continue
                elif isinstance(hourly_records, dict):
                    for hour_key, hourly_record in hourly_records.items():
                        if not isinstance(hourly_record, dict):
                            continue
                        try:
                            hour = int(hour_key)
                            batch_rows.append(
                                (
                                    cache_date,
                                    hour,
                                    _sql_value(hourly_record.get("elevation_deg")),
                                    _sql_value(hourly_record.get("azimuth_deg")),
                                    _sql_value(
                                        hourly_record.get(
                                            "clear_sky_solar_radiation_wm2"
                                        )
                                    ),
                                    _sql_value(
                                        hourly_record.get("theoretical_max_pv_kwh")
                                    ),
                                    sunrise,
                                    sunset,
                                    solar_noon,
                                    daylight_hours,
                                )
                            )
                        except Exception as migration_error:
                            _LOGGER.debug(
                                f"Skip astronomy cache hour: {migration_error}"
                            )
                            self.stats.skipped += 1
                            continue
        if batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO astronomy_cache (cache_date, hour, sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2, theoretical_max_kwh, sunrise, sunset, solar_noon, daylight_hours) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(f"Batch insert astronomy cache failed: {migration_error}")
                self.stats.errors += 1
        location = data.get("location", {})
        pv_system = data.get("pv_system", {})
        if location or pv_system:
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO astronomy_system_info (id, latitude, longitude, elevation_m, timezone, installed_capacity_kwp) VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        location.get("latitude"),
                        location.get("longitude"),
                        location.get("elevation_m"),
                        location.get("timezone"),
                        pv_system.get("installed_capacity_kwp"),
                    ),
                )
            except Exception:
                pass
        return None if True else None

    async def _migrate_weather_expert_learning(self, data: Dict[str, Any]) -> None:
        daily_history = data.get("daily_history", {})
        if isinstance(daily_history, dict):
            for date_key, day_record in daily_history.items():
                if not isinstance(day_record, dict):
                    continue
                try:
                    record_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    continue
                learned_at = day_record.get("learned_at")
                if learned_at and isinstance(learned_at, str):
                    try:
                        learned_at = datetime.fromisoformat(learned_at)
                    except Exception:
                        learned_at = SafeDateTimeUtil.now()
                else:
                    learned_at = SafeDateTimeUtil.now()
                comparison_hours = day_record.get("comparison_hours", 0)
                mae_by_cloud_type = day_record.get("mae_by_cloud_type", {})
                updated_weights = day_record.get("weights_updated", {})
                for cloud_type, expert_mae in mae_by_cloud_type.items():
                    if not isinstance(expert_mae, dict):
                        continue
                    weights_after_by_expert = updated_weights.get(cloud_type, {})
                    for expert_name, mae in expert_mae.items():
                        weight_after = (
                            weights_after_by_expert.get(expert_name, 0)
                            if isinstance(weights_after_by_expert, dict)
                            else 0
                        )
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO weather_expert_learning (date, cloud_type, expert_name, mae, weight_after, comparison_hours, learned_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    record_date,
                                    cloud_type,
                                    expert_name,
                                    mae,
                                    weight_after,
                                    comparison_hours,
                                    learned_at,
                                ),
                            )
                            self.stats.imported += 1
                        except Exception as migration_error:
                            _LOGGER.debug(
                                f"Skip weather expert learning: {migration_error}"
                            )
                            self.stats.skipped += 1
                            continue
                continue
        return None if True else None

    async def _migrate_weather_source_learning(self, data: Dict[str, Any]) -> None:
        daily_history = data.get("daily_history", {})
        if isinstance(daily_history, dict):
            for date_key, day_record in daily_history.items():
                if not isinstance(day_record, dict):
                    continue
                try:
                    record_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    continue
                learned_at = day_record.get("learned_at")
                if learned_at and isinstance(learned_at, str):
                    try:
                        learned_at = datetime.fromisoformat(learned_at)
                    except Exception:
                        learned_at = SafeDateTimeUtil.now()
                else:
                    learned_at = SafeDateTimeUtil.now()
                weights_after_by_source = day_record.get("weights_after", {})
                for metric_key, mae in day_record.items():
                    if metric_key.startswith("mae_"):
                        source_name = metric_key[4:]
                        weight_after = weights_after_by_source.get(source_name, 0)
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO weather_source_learning (date, source_name, mae, weight_after, learned_at) VALUES (?, ?, ?, ?, ?)",
                                (
                                    record_date,
                                    source_name,
                                    mae,
                                    weight_after,
                                    learned_at,
                                ),
                            )
                            self.stats.imported += 1
                        except Exception as migration_error:
                            _LOGGER.debug(
                                f"Skip weather source learning: {migration_error}"
                            )
                            self.stats.skipped += 1
                            continue
                continue
        return None if True else None

    async def _migrate_panel_group_sensor_state(self, data: Dict[str, Any]) -> None:
        last_values = data.get("last_values", {})
        last_updated = data.get("last_updated")
        if last_updated and isinstance(last_updated, str):
            try:
                last_updated = datetime.fromisoformat(last_updated)
            except Exception:
                last_updated = SafeDateTimeUtil.now()
        else:
            last_updated = SafeDateTimeUtil.now()
        for group_name, last_value in last_values.items():
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO panel_group_sensor_state (group_name, last_value, last_updated) VALUES (?, ?, ?)",
                    (group_name, last_value, last_updated),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip panel group sensor state: {migration_error}")
                self.stats.skipped += 1
                continue

    async def _migrate_yield_cache(self, data: Dict[str, Any]) -> None:
        try:
            yield_value = data.get("value")
            yield_time = data.get("time")
            record_date = data.get("date")
            if yield_time and isinstance(yield_time, str):
                yield_time = datetime.fromisoformat(yield_time)
            if record_date and isinstance(record_date, str):
                record_date = datetime.fromisoformat(record_date).date()
            await self.db.execute(
                "INSERT OR REPLACE INTO yield_cache (id, value, time, date) VALUES (1, ?, ?, ?)",
                (yield_value, yield_time, record_date),
            )
            self.stats.imported += 1
        except Exception as migration_error:
            _LOGGER.debug(f"Skip yield cache: {migration_error}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_visibility_learning(self, data: Dict[str, Any]) -> None:
        try:
            learning_stats = data.get("stats", {})
            weights = data.get("weights", {})
            last_learning_date = learning_stats.get("last_learning_date")
            if last_learning_date and isinstance(last_learning_date, str):
                try:
                    last_learning_date = datetime.fromisoformat(
                        last_learning_date
                    ).date()
                except Exception:
                    last_learning_date = None
            last_updated = data.get("updated_at")
            if last_updated and isinstance(last_updated, str):
                try:
                    last_updated = datetime.fromisoformat(last_updated)
                except Exception:
                    last_updated = SafeDateTimeUtil.now()
            else:
                last_updated = SafeDateTimeUtil.now()
            fog_weights = weights.get("fog", {})
            light_fog_weights = weights.get("fog_light", {})
            await self.db.execute(
                "INSERT OR REPLACE INTO visibility_learning (id, version, has_solar_radiation_sensor, last_learning_date, total_fog_hours_learned, total_fog_light_hours_learned, bright_sky_fog_hits, pirate_weather_fog_hits, learning_sessions, fog_bright_sky_weight, fog_pirate_weather_weight, fog_light_bright_sky_weight, fog_light_pirate_weather_weight, last_updated) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.get("version", "1.0"),
                    data.get("has_solar_radiation_sensor", False),
                    last_learning_date,
                    learning_stats.get("total_fog_hours_learned", 0),
                    learning_stats.get("total_fog_light_hours_learned", 0),
                    learning_stats.get("bright_sky_fog_hits", 0),
                    learning_stats.get("pirate_weather_fog_hits", 0),
                    learning_stats.get("learning_sessions", 0),
                    fog_weights.get("bright_sky", 0.5),
                    fog_weights.get("pirate_weather", 0.5),
                    light_fog_weights.get("bright_sky", 0.5),
                    light_fog_weights.get("pirate_weather", 0.5),
                    last_updated,
                ),
            )
            self.stats.imported += 1
        except Exception as migration_error:
            _LOGGER.debug(f"Skip visibility learning: {migration_error}")
            self.stats.skipped += 1
        if True:
            return

    async def _migrate_forecast_drift_log(self, data: Dict[str, Any]) -> None:
        history_entries = data if isinstance(data, list) else data.get("entries", [])
        for history_entry in history_entries:
            timestamp_value = history_entry.get("timestamp")
            if not timestamp_value:
                date_key = history_entry.get("date")
                entry_time = history_entry.get("time")
                if date_key and entry_time:
                    timestamp_value = f"{date_key}T{entry_time}:00"
                elif date_key:
                    timestamp_value = f"{date_key}T00:00:00"
            if not timestamp_value:
                self.stats.skipped += 1
                continue
            try:
                if isinstance(timestamp_value, str):
                    timestamp_value = datetime.fromisoformat(timestamp_value)
                if (
                    history_entry.get("morning_deviation_kwh") is not None
                    or history_entry.get("forecast_drift_percent") is not None
                ):
                    entry_type = "morning_correction"
                elif (
                    history_entry.get("sensor_cloud_percent") is not None
                    or history_entry.get("discrepancy_percent") is not None
                ):
                    entry_type = "cloud_discrepancy"
                else:
                    entry_type = "morning_correction"
                await self.db.execute(
                    "INSERT INTO forecast_drift_log (timestamp, entry_type, morning_deviation_kwh, forecast_drift_percent, correction_applied, sensor_cloud_percent, forecast_cloud_percent, discrepancy_percent, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        timestamp_value,
                        entry_type,
                        history_entry.get("morning_deviation_kwh"),
                        history_entry.get("forecast_drift_percent"),
                        history_entry.get("correction_applied"),
                        history_entry.get("sensor_cloud_percent"),
                        history_entry.get("forecast_cloud_percent"),
                        history_entry.get("discrepancy_percent"),
                        history_entry.get("action"),
                    ),
                )
                self.stats.imported += 1
                continue
            except Exception as migration_error:
                _LOGGER.debug(f"Skip forecast drift log: {migration_error}")
                self.stats.skipped += 1

    async def _migrate_hourly_weather_actual(self, data: Dict[str, Any]) -> None:
        hourly_data_by_date = data.get("hourly_data", {})
        batch_rows = []
        if isinstance(hourly_data_by_date, dict):
            for date_key, hourly_by_key in hourly_data_by_date.items():
                try:
                    record_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                if not isinstance(hourly_by_key, dict):
                    continue
                for hour_key, history_entry in hourly_by_key.items():
                    if not isinstance(history_entry, dict):
                        continue
                    try:
                        hour = int(hour_key)
                        frost_analysis = history_entry.get("frost_analysis", {})
                        batch_rows.append(
                            (
                                record_date,
                                hour,
                                _sql_value(history_entry.get("temperature_c")),
                                _sql_value(history_entry.get("humidity_percent")),
                                _sql_value(history_entry.get("wind_speed_ms")),
                                _sql_value(history_entry.get("precipitation_mm")),
                                _sql_value(history_entry.get("pressure_hpa")),
                                _sql_value(history_entry.get("solar_radiation_wm2")),
                                _sql_value(history_entry.get("lux")),
                                _sql_value(history_entry.get("timestamp")),
                                _sql_value(history_entry.get("source")),
                                _sql_value(history_entry.get("cloud_cover_percent")),
                                _sql_value(history_entry.get("cloud_cover_source")),
                                _sql_value(history_entry.get("frost_detected")),
                                _sql_value(history_entry.get("frost_score")),
                                _sql_value(history_entry.get("frost_confidence")),
                                _sql_value(frost_analysis.get("dewpoint_c")),
                                _sql_value(frost_analysis.get("frost_margin_c")),
                                _sql_value(frost_analysis.get("frost_probability")),
                                _sql_value(
                                    frost_analysis.get("correlation_diff_percent")
                                ),
                                _sql_value(frost_analysis.get("detection_method")),
                                _sql_value(frost_analysis.get("wind_frost_factor")),
                                _sql_value(
                                    frost_analysis.get("physical_frost_possible")
                                ),
                                _sql_value(history_entry.get("hours_after_sunrise")),
                                _sql_value(history_entry.get("hours_before_sunset")),
                                _sql_value(history_entry.get("snow_covered_panels")),
                                _sql_value(history_entry.get("snow_coverage_source")),
                            )
                        )
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip hourly weather actual: {migration_error}")
                        self.stats.skipped += 1
                        continue
                    continue
                continue
        if batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO hourly_weather_actual (date, hour, temperature_c, humidity_percent, wind_speed_ms, precipitation_mm, pressure_hpa, solar_radiation_wm2, lux, timestamp, source, cloud_cover_percent, cloud_cover_source, frost_detected, frost_score, frost_confidence, dewpoint_c, frost_margin_c, frost_probability, correlation_diff_percent, detection_method, wind_frost_factor, physical_frost_possible, hours_after_sunrise, hours_before_sunset, snow_covered_panels, snow_coverage_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert hourly weather actual failed: {migration_error}"
                )
                self.stats.errors += 1
        snow_tracking = data.get("snow_tracking", {})
        if snow_tracking and isinstance(snow_tracking, dict):
            try:
                last_snow_event = snow_tracking.get("last_snow_event")
                if last_snow_event and isinstance(last_snow_event, str):
                    last_snow_event = datetime.fromisoformat(last_snow_event)
                panels_covered_since = snow_tracking.get("panels_covered_since")
                if panels_covered_since and isinstance(panels_covered_since, str):
                    panels_covered_since = datetime.fromisoformat(panels_covered_since)
                melt_started_at = snow_tracking.get("melt_started_at")
                if melt_started_at and isinstance(melt_started_at, str):
                    melt_started_at = datetime.fromisoformat(melt_started_at)
                await self.db.execute(
                    "INSERT OR REPLACE INTO snow_tracking (id, last_snow_event, panels_covered_since, estimated_depth_mm, melt_started_at, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        last_snow_event,
                        panels_covered_since,
                        snow_tracking.get("estimated_depth_mm", 0),
                        melt_started_at,
                        SafeDateTimeUtil.now(),
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip snow tracking: {migration_error}")
        return None if True else None

    async def _migrate_weather_precision_daily(self, data: Dict[str, Any]) -> None:
        daily_tracking = data.get("daily_tracking", {})
        hourly_batch_rows = []
        daily_batch_rows = []
        if isinstance(daily_tracking, dict):
            for date_key, day_record in daily_tracking.items():
                if not isinstance(day_record, dict):
                    continue
                try:
                    record_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                for hourly_records in day_record.get("hourly_comparisons", []):
                    hour = hourly_records.get("hour")
                    if hour is None:
                        continue
                    field_comparisons = hourly_records.get("fields", {})
                    temperature_comparison = field_comparisons.get("temperature", {})
                    humidity_comparison = field_comparisons.get("humidity", {})
                    wind_comparison = field_comparisons.get("wind", {})
                    rain_comparison = field_comparisons.get("rain", {})
                    solar_comparison = field_comparisons.get(
                        "solar_radiation_wm2", field_comparisons.get("solar", {})
                    )
                    cloud_comparison = field_comparisons.get("clouds", {})
                    try:
                        hourly_batch_rows.append(
                            (
                                record_date,
                                hour,
                                _sql_value(temperature_comparison.get("forecast")),
                                _sql_value(temperature_comparison.get("actual")),
                                _sql_value(temperature_comparison.get("offset")),
                                _sql_value(humidity_comparison.get("forecast")),
                                _sql_value(humidity_comparison.get("actual")),
                                _sql_value(humidity_comparison.get("factor")),
                                _sql_value(wind_comparison.get("forecast")),
                                _sql_value(wind_comparison.get("actual")),
                                _sql_value(wind_comparison.get("factor")),
                                _sql_value(rain_comparison.get("forecast")),
                                _sql_value(rain_comparison.get("actual")),
                                _sql_value(rain_comparison.get("difference")),
                                _sql_value(solar_comparison.get("forecast")),
                                _sql_value(solar_comparison.get("actual")),
                                _sql_value(solar_comparison.get("factor")),
                                _sql_value(cloud_comparison.get("forecast")),
                                _sql_value(cloud_comparison.get("actual")),
                                _sql_value(cloud_comparison.get("factor")),
                            )
                        )
                    except Exception as migration_error:
                        _LOGGER.debug(
                            f"Skip weather precision hourly: {migration_error}"
                        )
                        self.stats.skipped += 1
                        continue
                    continue
                daily_factors = day_record.get("daily_factors", {})
                hours_tracked = day_record.get(
                    "hours_tracked", len(day_record.get("hourly_comparisons", []))
                )
                daily_batch_rows.append(
                    (
                        record_date,
                        hours_tracked,
                        _sql_value(daily_factors.get("temperature")),
                        _sql_value(daily_factors.get("pressure")),
                        _sql_value(daily_factors.get("solar_radiation_wm2")),
                        _sql_value(daily_factors.get("clouds")),
                        _sql_value(daily_factors.get("humidity")),
                        _sql_value(daily_factors.get("wind")),
                        _sql_value(daily_factors.get("rain")),
                    )
                )
                continue
        if hourly_batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO weather_precision_daily (date, hour, temp_forecast, temp_actual, temp_offset, humidity_forecast, humidity_actual, humidity_factor, wind_forecast, wind_actual, wind_factor, rain_forecast, rain_actual, rain_difference, solar_forecast, solar_actual, solar_factor, clouds_forecast, clouds_actual, clouds_factor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    hourly_batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert weather precision daily failed: {migration_error}"
                )
                self.stats.errors += 1
        if daily_batch_rows:
            try:
                await self.db.executemany(
                    "INSERT OR REPLACE INTO weather_precision_daily_summary (date, hours_tracked, avg_temp_offset, avg_pressure_offset, avg_solar_factor, avg_clouds_factor, avg_humidity_factor, avg_wind_factor, avg_rain_diff) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    daily_batch_rows,
                )
            except Exception:
                pass
        rolling_averages = data.get("rolling_averages", {})
        correction_factors = rolling_averages.get("correction_factors", {})
        confidence_by_field = rolling_averages.get("confidence", {})
        if correction_factors:
            try:
                pass
                numeric_confidences = [
                    confidence_value
                    for confidence_value in confidence_by_field.values()
                    if isinstance(confidence_value, (int, float))
                ]
                average_confidence = (
                    sum(numeric_confidences) / len(numeric_confidences)
                    if numeric_confidences
                    else 0.0
                )
                await self.db.execute(
                    "INSERT OR REPLACE INTO weather_precision_factors\n                       (id, temperature_factor, solar_factor, cloud_factor, wind_factor,\n                        humidity_factor, rain_factor, pressure_factor, sample_days, confidence, updated_at)\n                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (
                        _sql_value(correction_factors.get("temperature", 0.0)),
                        _sql_value(correction_factors.get("solar_radiation_wm2", 1.0)),
                        _sql_value(correction_factors.get("clouds", 1.0)),
                        _sql_value(correction_factors.get("wind", 1.0)),
                        _sql_value(correction_factors.get("humidity", 1.0)),
                        _sql_value(correction_factors.get("rain", 0.0)),
                        _sql_value(correction_factors.get("pressure", 0.0)),
                        _sql_value(rolling_averages.get("sample_days", 0)),
                        _sql_value(average_confidence),
                    ),
                )
                self.stats.imported += 1
            except Exception as migration_error:
                _LOGGER.debug(f"Skip weather precision factors: {migration_error}")
        return None if True else None

    async def _migrate_panel_group_daily_cache(self, data: Dict[str, Any]) -> None:
        cache_date = data.get("date")
        if not cache_date:
            self.stats.skipped += 1
            return
        try:
            if isinstance(cache_date, str):
                cache_date = datetime.fromisoformat(cache_date).date()
        except Exception:
            self.stats.skipped += 1
            return
        daily_group_batch_rows = []
        hourly_batch_rows = []
        group_records = data.get("groups", {})
        if isinstance(group_records, dict):
            for group_name, group_record in group_records.items():
                if not isinstance(group_record, dict):
                    continue
                try:
                    daily_group_batch_rows.append(
                        (
                            cache_date,
                            group_name,
                            _sql_value(group_record.get("prediction_total_kwh")),
                            _sql_value(group_record.get("actual_total_kwh")),
                        )
                    )
                    for hourly_record in group_record.get("hourly", []):
                        hour = hourly_record.get("hour")
                        if hour is not None:
                            hourly_batch_rows.append(
                                (
                                    cache_date,
                                    group_name,
                                    hour,
                                    _sql_value(hourly_record.get("prediction_kwh")),
                                    _sql_value(hourly_record.get("actual_kwh")),
                                )
                            )
                except Exception as migration_error:
                    _LOGGER.debug(f"Skip panel group {group_name}: {migration_error}")
                    self.stats.skipped += 1
                    continue
        if daily_group_batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO panel_group_daily_cache (cache_date, group_name, prediction_total_kwh, actual_total_kwh) VALUES (?, ?, ?, ?)",
                    daily_group_batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert panel group daily cache failed: {migration_error}"
                )
                self.stats.errors += 1
        if hourly_batch_rows:
            try:
                await self.db.executemany(
                    "INSERT OR REPLACE INTO panel_group_daily_hourly (cache_date, group_name, hour, prediction_kwh, actual_kwh) VALUES (?, ?, ?, ?, ?)",
                    hourly_batch_rows,
                )
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert panel group daily hourly failed: {migration_error}"
                )
                self.stats.errors += 1
        return None if True else None

    async def _migrate_multi_day_forecast(self, data: Dict[str, Any]) -> None:
        days = data.get("days", {})
        hourly_forecast_batch_rows = []
        panel_forecast_batch_rows = []
        if isinstance(days, dict):
            for date_key, day_record in days.items():
                if not isinstance(day_record, dict):
                    continue
                try:
                    forecast_date = datetime.fromisoformat(date_key).date()
                except Exception:
                    self.stats.skipped += 1
                    continue
                day_type = day_record.get("day_type", "today")
                for history_entry in day_record.get("hourly", []):
                    hour = history_entry.get("hour")
                    if hour is None:
                        continue
                    try:
                        hourly_forecast_batch_rows.append(
                            (
                                forecast_date,
                                day_type,
                                hour,
                                _sql_value(history_entry.get("prediction_kwh")),
                                _sql_value(history_entry.get("cloud_cover")),
                                _sql_value(history_entry.get("temperature")),
                                _sql_value(history_entry.get("solar_radiation_wm2")),
                                _sql_value(history_entry.get("weather_source")),
                            )
                        )
                        for panel_group_forecast in history_entry.get(
                            "panel_groups", []
                        ):
                            group_name = panel_group_forecast.get("name")
                            if group_name:
                                panel_forecast_batch_rows.append(
                                    (
                                        forecast_date,
                                        hour,
                                        group_name,
                                        _sql_value(
                                            panel_group_forecast.get("power_kwh")
                                        ),
                                        _sql_value(
                                            panel_group_forecast.get(
                                                "contribution_percent"
                                            )
                                        ),
                                        _sql_value(panel_group_forecast.get("poa_wm2")),
                                        _sql_value(panel_group_forecast.get("aoi_deg")),
                                        _sql_value(panel_group_forecast.get("source")),
                                    )
                                )
                    except Exception as migration_error:
                        _LOGGER.debug(f"Skip multi day forecast: {migration_error}")
                        self.stats.skipped += 1
                        continue
                    continue
                continue
        if hourly_forecast_batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO multi_day_hourly_forecast (forecast_date, day_type, hour, prediction_kwh, cloud_cover, temperature, solar_radiation_wm2, weather_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    hourly_forecast_batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert multi day forecast failed: {migration_error}"
                )
                self.stats.errors += 1
        if panel_forecast_batch_rows:
            try:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO multi_day_hourly_forecast_panels (forecast_date, hour, group_name, power_kwh, contribution_percent, poa_wm2, aoi_deg, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    panel_forecast_batch_rows,
                )
                self.stats.imported += inserted_count
            except Exception as migration_error:
                _LOGGER.error(
                    f"Batch insert multi day forecast panels failed: {migration_error}"
                )
                self.stats.errors += 1
        return None if True else None

    async def _migrate_retrospective_forecast(self, data: Dict[str, Any]) -> None:
        try:
            forecast_summary = data.get("forecast_summary", {})
            simulation_context = data.get("simulation_context", {})
            generated_at = data.get("generated_at")
            if generated_at and isinstance(generated_at, str):
                generated_at = datetime.fromisoformat(generated_at)
            target_date = simulation_context.get("target_date")
            if target_date and isinstance(target_date, str):
                try:
                    target_date = datetime.fromisoformat(target_date).date()
                except Exception:
                    target_date = None
            await self.db.execute(
                "INSERT OR REPLACE INTO retrospective_forecast (id, version, generated_at, simulated_forecast_time, sunrise_today, target_date, today_kwh, today_kwh_raw, safeguard_applied, tomorrow_kwh, day_after_tomorrow_kwh, method, confidence, best_hour, best_hour_kwh) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _sql_value(data.get("version", "1.0")),
                    _sql_value(generated_at),
                    _sql_value(simulation_context.get("simulated_forecast_time")),
                    _sql_value(simulation_context.get("sunrise_today")),
                    target_date,
                    _sql_value(forecast_summary.get("today_kwh")),
                    _sql_value(forecast_summary.get("today_kwh_raw")),
                    _sql_value(forecast_summary.get("safeguard_applied", False)),
                    _sql_value(forecast_summary.get("tomorrow_kwh")),
                    _sql_value(forecast_summary.get("day_after_tomorrow_kwh")),
                    _sql_value(forecast_summary.get("method")),
                    _sql_value(forecast_summary.get("confidence")),
                    _sql_value(forecast_summary.get("best_hour")),
                    _sql_value(forecast_summary.get("best_hour_kwh")),
                ),
            )
            self.stats.imported += 1
            await self.db.execute("DELETE FROM retrospective_forecast_hourly")
            hourly_batch_rows = []
            for hourly_record in data.get("hourly_predictions", []):
                hour = hourly_record.get("hour")
                if hour is not None:
                    weather = hourly_record.get("weather", {})
                    astronomy = hourly_record.get("astronomy", {})
                    hourly_batch_rows.append(
                        (
                            hour,
                            _sql_value(hourly_record.get("prediction_kwh")),
                            _sql_value(weather.get("temperature_c")),
                            _sql_value(weather.get("cloud_cover_percent")),
                            _sql_value(weather.get("humidity_percent")),
                            _sql_value(weather.get("wind_speed_ms")),
                            _sql_value(weather.get("precipitation_mm")),
                            _sql_value(weather.get("direct_radiation")),
                            _sql_value(weather.get("diffuse_radiation")),
                            _sql_value(weather.get("visibility_m")),
                            _sql_value(weather.get("fog_detected")),
                            _sql_value(weather.get("fog_type")),
                            _sql_value(astronomy.get("sun_elevation_deg")),
                            _sql_value(astronomy.get("sun_azimuth_deg")),
                            _sql_value(astronomy.get("theoretical_max_kwh")),
                            _sql_value(astronomy.get("clear_sky_radiation_wm2")),
                        )
                    )
            if hourly_batch_rows:
                inserted_count = await self.db.executemany(
                    "INSERT OR REPLACE INTO retrospective_forecast_hourly (hour, prediction_kwh, temperature_c, cloud_cover_percent, humidity_percent, wind_speed_ms, precipitation_mm, direct_radiation, diffuse_radiation, visibility_m, fog_detected, fog_type, sun_elevation_deg, sun_azimuth_deg, theoretical_max_kwh, clear_sky_radiation_wm2) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    hourly_batch_rows,
                )
                self.stats.imported += inserted_count
        except Exception as migration_error:
            _LOGGER.debug(f"Skip retrospective forecast: {migration_error}")
            self.stats.skipped += 1
        return None if True else None

    async def _set_migration_flag(self) -> None:

        def _write_flag():
            flag_path = Path(self.hass.config.path(MIGRATION_FLAG))
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text(
                f"JSON to SQLite migration completed\nTimestamp: {SafeDateTimeUtil.now().isoformat()}\nStats: {self.stats}\n"
            )

        await self.hass.async_add_executor_job(_write_flag)
        _LOGGER.info("Migration flag set")


async def run_json_migration(
    hass: HomeAssistant, db_manager: DatabaseManager
) -> JsonMigrationStats:
    migrator = JsonMigrator(hass, db_manager)
    return await migrator.run_migration()


async def dry_run_json_migration(
    hass: HomeAssistant, db_manager: DatabaseManager
) -> Dict[str, Any]:
    migrator = JsonMigrator(hass, db_manager)
    return await migrator.dry_run()
