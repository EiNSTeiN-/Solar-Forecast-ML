# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Schema Validator for Solar Forecast Energy AI V16.2.0.
Validates and ensures database schema integrity on startup.
Replaces JSON file validation with database table validation.

@zara
"""
import logging
from datetime import datetime
from typing import Any, Dict, List
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)
REQUIRED_TABLES = {
    "ai_seasonal_factors": ["month", "factor", "sample_count"],
    "ai_feature_importance": ["feature_name", "importance", "category"],
    "ai_grid_search_results": ["success", "hidden_size", "accuracy"],
    "ai_dni_tracker": ["hour", "max_dni"],
    "ai_model_weights": ["weight_type", "weight_data"],
    "physics_learning_config": ["albedo", "system_efficiency"],
    "physics_calibration_groups": ["group_name", "global_factor"],
    "physics_calibration_hourly": ["group_name", "hour", "factor"],
    "physics_calibration_buckets": ["group_name", "bucket_name", "global_factor"],
    "physics_calibration_history": ["date", "group_name", "avg_ratio"],
    "weather_forecast": ["forecast_date", "hour", "temperature", "clouds"],
    "weather_expert_weights": ["cloud_type", "expert_name", "weight"],
    "weather_source_weights": ["source_name", "weight"],
    "hourly_predictions": ["prediction_id", "target_date", "prediction_kwh"],
    "daily_forecasts": ["forecast_type", "forecast_date", "prediction_kwh"],
    "daily_summaries": ["date", "predicted_total_kwh", "actual_total_kwh"],
    "coordinator_state": ["expected_daily_production", "last_set_date"],
    "production_time_state": ["date", "accumulated_hours"],
    "panel_group_sensor_state": ["group_name", "last_value"],
    "yield_cache": ["value", "time", "date"],
    "visibility_learning": ["visibility_threshold_m", "fog_visibility_threshold_m"],
}
DEFAULT_PHYSICS_LEARNING_CONFIG = {
    "albedo": 0.2,
    "system_efficiency": 0.9,
    "learned_efficiency_factor": 1.0,
    "rolling_window_days": 21,
    "min_samples": 1,
}
DEFAULT_VISIBILITY_LEARNING = {
    "visibility_threshold_m": 10000,
    "fog_visibility_threshold_m": 1000,
    "samples_below_threshold": 0,
    "samples_above_threshold": 0,
}


class DataSchemaValidator(DataManagerIO):
    """Validates database schema and ensures required tables exist. @zara

    Replaces the old JSON file validation with database table validation.
    On startup, ensures all required tables exist and have proper structure.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize the schema validator. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager instance for DB operations\n"
        super().__init__(hass, db_manager)
        self.migration_log = []
        self.healed_tables = []
        _LOGGER.debug("DataSchemaValidator initialized with DatabaseManager")

    async def validate_and_migrate_all(self) -> bool:
        """Validate all database tables and ensure schema integrity. @zara

        This method:
        1. Verifies all required tables exist
        2. Checks essential columns are present
        3. Initializes singleton config tables with defaults if empty
        4. Logs all validation actions

        Returns:
            True if all validations pass, False otherwise
        """
        try:
            _LOGGER.info("=== Database Schema Validation Starting ===")
            valid = True
            for table_name, required_columns in REQUIRED_TABLES.items():
                table_valid = await self._validate_table(table_name, required_columns)
                if not table_valid:
                    valid = False
                    self._log(f"Table validation failed: {table_name}")
            await self._ensure_physics_learning_config()
            await self._ensure_visibility_learning()
            await self._ensure_coordinator_state()
            if self.migration_log:
                _LOGGER.info("=== Schema Validation Summary ===")
                for log_message in self.migration_log:
                    _LOGGER.info("  %s", log_message)
            else:
                _LOGGER.info("All database tables valid - no changes needed")
            if self.healed_tables:
                _LOGGER.info(
                    "Initialized %d table(s): %s",
                    len(self.healed_tables),
                    ", ".join(self.healed_tables),
                )
            _LOGGER.info("=== Database Schema Validation Complete ===")
            return valid
        except Exception as error:
            _LOGGER.error("Schema validation failed: %s", error, exc_info=True)
            return False

    def _log(self, message: str) -> None:
        """Log a validation action. @zara"""
        self.migration_log.append(message)
        _LOGGER.info("SCHEMA: %s", message)

    async def _validate_table(
        self, table_name: str, required_columns: List[str]
    ) -> bool:
        "Validate that a table exists and has required columns. @zara\n\nArgs:\n    table_name: Name of the table to validate\n    required_columns: List of required column names\n\nReturns:\n    True if table is valid, False otherwise\n"
        try:
            table_row = await self.fetch_one(
                "SELECT name FROM sqlite_master\n                   WHERE type='table' AND name=?",
                (table_name,),
            )
            if not table_row:
                _LOGGER.warning("Table %s does not exist", table_name)
                return False
            column_rows = await self.fetch_all(f"PRAGMA table_info({table_name})")
            existing_columns = {column_row[1] for column_row in column_rows}
            missing_columns = set(required_columns) - existing_columns
            if missing_columns:
                _LOGGER.warning(
                    "Table %s missing columns: %s",
                    table_name,
                    ", ".join(missing_columns),
                )
                return False
        except Exception as error:
            _LOGGER.error("Failed to validate table %s: %s", table_name, error)
            return False
        return True

    async def _ensure_physics_learning_config(self) -> None:
        """Ensure physics_learning_config has default values. @zara"""
        try:
            config_row = await self.fetch_one(
                "SELECT id FROM physics_learning_config WHERE id = 1"
            )
            if not config_row:
                await self.execute_query(
                    "INSERT INTO physics_learning_config\n                       (id, albedo, system_efficiency, learned_efficiency_factor,\n                        rolling_window_days, min_samples, updated_at)\n                       VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (
                        DEFAULT_PHYSICS_LEARNING_CONFIG["albedo"],
                        DEFAULT_PHYSICS_LEARNING_CONFIG["system_efficiency"],
                        DEFAULT_PHYSICS_LEARNING_CONFIG["learned_efficiency_factor"],
                        DEFAULT_PHYSICS_LEARNING_CONFIG["rolling_window_days"],
                        DEFAULT_PHYSICS_LEARNING_CONFIG["min_samples"],
                        datetime.now(),
                    ),
                )
                self._log("Created default physics_learning_config")
                self.healed_tables.append("physics_learning_config")
        except Exception as error:
            _LOGGER.error("Failed to ensure physics_learning_config: %s", error)
        return None if True else None

    async def _ensure_visibility_learning(self) -> None:
        """Ensure visibility_learning has default values. @zara"""
        try:
            config_row = await self.fetch_one(
                "SELECT id FROM visibility_learning WHERE id = 1"
            )
            if not config_row:
                await self.execute_query(
                    "INSERT INTO visibility_learning\n                       (id, visibility_threshold_m, fog_visibility_threshold_m,\n                        samples_below_threshold, samples_above_threshold, last_updated)\n                       VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        DEFAULT_VISIBILITY_LEARNING["visibility_threshold_m"],
                        DEFAULT_VISIBILITY_LEARNING["fog_visibility_threshold_m"],
                        DEFAULT_VISIBILITY_LEARNING["samples_below_threshold"],
                        DEFAULT_VISIBILITY_LEARNING["samples_above_threshold"],
                        datetime.now(),
                    ),
                )
                self._log("Created default visibility_learning")
                self.healed_tables.append("visibility_learning")
        except Exception as error:
            _LOGGER.error("Failed to ensure visibility_learning: %s", error)
        return None if True else None

    async def _ensure_coordinator_state(self) -> None:
        """Ensure coordinator_state has default values. @zara"""
        try:
            state_row = await self.fetch_one(
                "SELECT id FROM coordinator_state WHERE id = 1"
            )
            if not state_row:
                await self.execute_query(
                    "INSERT INTO coordinator_state\n                       (id, expected_daily_production, last_set_date, last_updated)\n                       VALUES (1, 0, ?, ?)",
                    (datetime.now().date(), datetime.now()),
                )
                self._log("Created default coordinator_state")
                self.healed_tables.append("coordinator_state")
        except Exception as error:
            _LOGGER.error("Failed to ensure coordinator_state: %s", error)
        return None if True else None

    async def validate_table_integrity(self, table_name: str) -> Dict[str, Any]:
        """Check integrity of a specific table. @zara

        Args:
            table_name: Name of table to check

        Returns:
            Dictionary with integrity check results
        """
        integrity_report = {
            "table": table_name,
            "exists": False,
            "row_count": 0,
            "columns": [],
            "indexes": [],
            "issues": [],
        }
        try:
            table_row = await self.fetch_one(
                "SELECT name FROM sqlite_master\n                   WHERE type='table' AND name=?",
                (table_name,),
            )
            if not table_row:
                integrity_report["issues"].append("Table does not exist")
                return integrity_report
            integrity_report["exists"] = True
            count_row = await self.fetch_one(f"SELECT COUNT(*) FROM {table_name}")
            integrity_report["row_count"] = count_row[0] if count_row else 0
            column_rows = await self.fetch_all(f"PRAGMA table_info({table_name})")
            integrity_report["columns"] = [
                {
                    "name": column_row[1],
                    "type": column_row[2],
                    "notnull": bool(column_row[3]),
                    "default": column_row[4],
                    "pk": bool(column_row[5]),
                }
                for column_row in column_rows
            ]
            index_rows = await self.fetch_all(f"PRAGMA index_list({table_name})")
            integrity_report["indexes"] = [index_row[1] for index_row in index_rows]
            return integrity_report
        except Exception as error:
            _LOGGER.error("Failed to check integrity of %s: %s", table_name, error)
            integrity_report["issues"].append(f"Error: {str(error)}")
            return integrity_report

    async def get_schema_report(self) -> Dict[str, Any]:
        "Generate a comprehensive schema report. @zara\n\nReturns:\n    Dictionary with schema validation report\n"
        schema_report = {
            "timestamp": datetime.now().isoformat(),
            "tables_checked": 0,
            "tables_valid": 0,
            "tables_missing": [],
            "table_details": {},
        }
        for table_name in REQUIRED_TABLES.keys():
            schema_report["tables_checked"] += 1
            table_report = await self.validate_table_integrity(table_name)
            schema_report["table_details"][table_name] = table_report
            if table_report["exists"]:
                schema_report["tables_valid"] += 1
            else:
                schema_report["tables_missing"].append(table_name)
        return schema_report

    async def check_foreign_keys(self) -> Dict[str, Any]:
        "Check foreign key integrity. @zara\n\nReturns:\n    Dictionary with foreign key check results\n"
        try:
            await self.execute_query("PRAGMA foreign_keys = ON")
            violations = await self.fetch_all("PRAGMA foreign_key_check")
            return {
                "valid": len(violations) == 0,
                "violations_count": len(violations),
                "violations": [
                    {
                        "table": violation[0],
                        "rowid": violation[1],
                        "parent_table": violation[2],
                        "fk_index": violation[3],
                    }
                    for violation in violations
                ],
            }
            pass
        except Exception as error:
            _LOGGER.error("Failed to check foreign keys: %s", error)
            return {"valid": False, "error": str(error), "violations_count": -1}

    async def vacuum_database(self) -> bool:
        "Vacuum database to reclaim space. @zara\n\nReturns:\n    True if successful, False otherwise\n"
        try:
            await self.db.vacuum()
            self._log("Database vacuumed successfully")
        except Exception as error:
            _LOGGER.error("Failed to vacuum database: %s", error)
            return False
        return True

    async def get_database_stats(self) -> Dict[str, Any]:
        "Get database statistics. @zara\n\nReturns:\n    Dictionary with database statistics\n"
        try:
            database_statistics = await self.get_db_stats()
            row_counts = {}
            for table_name in (
                "hourly_predictions",
                "daily_summaries",
                "weather_forecast",
            ):
                count_row = await self.fetch_one(f"SELECT COUNT(*) FROM {table_name}")
                row_counts[table_name] = count_row[0] if count_row else 0
            database_statistics["table_row_counts"] = row_counts
            return database_statistics
        except Exception as error:
            _LOGGER.error("Failed to get database stats: %s", error)
            return {"error": str(error), "connected": False}
