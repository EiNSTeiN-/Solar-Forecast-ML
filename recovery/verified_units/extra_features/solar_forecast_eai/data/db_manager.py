# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Database Manager for Solar Forecast Energy AI.
Handles all SQLite operations using aiosqlite.
"""
import aiofiles
import aiosqlite
import asyncio
import json
import logging
import random
from datetime import datetime, date
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)


def _safe_dt(value) -> datetime:
    """Safely parse ISO datetime, fallback to now() if None. @zara"""
    if not value:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _safe_date(value) -> date:
    """Safely parse ISO date, fallback to today() if None. @zara"""
    if not value:
        return date.today()
    if isinstance(value, date) and (not isinstance(value, datetime)):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(str(value)).date()


class DatabaseManager:
    """Manages SQLite database operations for Solar Forecast Energy AI."""

    def __init__(self, db_path: str):
        """Initialize the database manager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._db = None
        self._write_lock = None
        self._transaction_owner = None
        self._transaction_db = None
        self._transaction_depth = 0
        self._savepoint_counter = 0

    async def connect(self) -> None:
        """Establish database connection and create schema if needed."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        self._write_lock = asyncio.Lock()
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = DELETE")
        await self._db.execute("PRAGMA busy_timeout = 60000")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._initialize_schema()
        _LOGGER.info(
            "Database connected: DELETE mode, 60s busy_timeout, synchronous=NORMAL, write serialization enabled: %s",
            self.db_path,
        )

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            _LOGGER.info("Database closed")
        return None if True else None

    async def _initialize_schema(self) -> None:
        """Initialize database schema - only runs migrations if schema exists.

        Schema is created by StartupInitializer (sync) during initial setup.
        This method only runs migrations to avoid duplicate schema execution.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hourly_predictions'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if schema_row:
            _LOGGER.debug("Database schema already exists, running migrations only")
            await self._run_migrations()
            return
        _LOGGER.warning("Schema not found, initializing from scratch")
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            _LOGGER.error("Schema file not found: %s", schema_path)
            return
        async with aiofiles.open(schema_path, "r") as schema_file:
            schema_sql = await schema_file.read()
        await self._db.executescript(schema_sql)
        await self._db.commit()
        _LOGGER.info("Database schema initialized")
        await self._run_migrations()

    async def _run_migrations(self) -> None:
        """Run database migrations for schema updates. @zara"""
        await self._migrate_hourly_weather_actual_condition()
        await self._migrate_hourly_predictions_v16()
        await self._migrate_shadow_analysis_table_v16()
        await self._migrate_weather_precision_v161()
        await self._cleanup_ai_model_weights_blobs_v161()
        await self._migrate_frost_analysis_table_v161()
        await self._migrate_visibility_learning_table_v161()
        await self._migrate_snow_tracking_table_v161()
        await self._migrate_weather_forecast_weather_code_v161()
        await self._migrate_shadow_pattern_learning_v162()
        await self._migrate_sensor_storage_tables_v1600()
        await self._migrate_raw_prediction_values_v1600()
        await self._migrate_method_performance_learning_v1600()
        await self._migrate_hourly_weather_actual_snow_frost_v1600()
        await self._migrate_hourly_weather_actual_precipitation_semantics()
        await self._migrate_weather_cache_open_meteo_code_v1600()
        await self._migrate_missing_indexes_v1600()
        await self._migrate_peak_power_tracking_v1600()
        await self._migrate_dni_history_dedup_v1600()
        await self._migrate_grid_search_model_type_v1600()
        await self._migrate_prediction_weather_diffuse_v1600()
        await self._migrate_prediction_weather_direct_radiation_v1601()
        await self._migrate_ensemble_columns_v1602()
        await self._migrate_ensemble_group_weights_v1603()
        await self._migrate_ridge_meta_normalization_v1604()
        await self._migrate_per_group_v170()
        await self._migrate_gold_standard_v1640()
        await self._backfill_astronomy_enrichment_v1640()
        await self._cleanup_null_season_duplicates_v1640()
        await self._migrate_group_method_performance_v1640()
        await self._selfheal_stale_snow_v1642()
        await self._goldstandard_reset_v1642()
        await self._migrate_ai_v300()
        await self._migrate_weather_mlp()
        await self._migrate_weather_cache_snowfall_rain()
        await self._migrate_eod_duration_v1710()
        await self._migrate_daily_summaries_unique_date()
        await self._migrate_seasonal_archive_v1684()
        await self._migrate_forecast_calibration_v400()

    async def _migrate_forecast_calibration_v400(self) -> None:
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            column_names = {column[1] for column in await cursor.fetchall()}
        migration_statements = []
        if "prediction_kwh_raw" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN prediction_kwh_raw REAL"
            )
        if "correction_factor" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN correction_factor REAL NOT NULL DEFAULT 1.0"
            )
        if not migration_statements:
            return
        async with self.transaction():
            for statement in migration_statements:
                await self.execute(statement, auto_commit=False)
        return None if True else None

    async def _migrate_eod_duration_v1710(self) -> None:
        """Add eod_duration_seconds column to daily_summaries. @zara"""
        try:
            async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
            if "eod_duration_seconds" not in column_names:
                await self._db.execute(
                    "ALTER TABLE daily_summaries ADD COLUMN eod_duration_seconds REAL"
                )
                await self._db.commit()
                _LOGGER.info("Migration: Added eod_duration_seconds to daily_summaries")
        except Exception as error:
            _LOGGER.warning("Migration eod_duration failed: %s", error)
        return None if True else None

    async def _migrate_daily_summaries_unique_date(self) -> None:
        """Add missing UNIQUE index on daily_summaries.date for ON CONFLICT support. @zara"""
        try:
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_daily_summaries_date'"
            ) as cursor:
                schema_entry = await cursor.fetchone()
            if not schema_entry:
                await self._db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_summaries_date ON daily_summaries(date)"
                )
                await self._db.commit()
                _LOGGER.info("Migration: Added UNIQUE index on daily_summaries.date")
        except Exception as error:
            _LOGGER.warning("Migration daily_summaries_unique_date failed: %s", error)
        return None if True else None

    async def _migrate_seasonal_archive_v1684(self) -> None:
        """Create seasonal model archive tables. @zara V16.8.4"""
        try:
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_seasonal_archive_meta'"
            ) as cursor:
                schema_entry = await cursor.fetchone()
            if not schema_entry:
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS ai_seasonal_archive_meta (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        season TEXT NOT NULL CHECK(season IN ('winter', 'spring', 'summer', 'autumn')),\n                        archive_type TEXT NOT NULL CHECK(archive_type IN ('lstm', 'ridge')),\n                        training_samples INTEGER DEFAULT 0,\n                        accuracy REAL DEFAULT 0.0,\n                        rmse REAL DEFAULT 0.0,\n                        source_year INTEGER NOT NULL,\n                        input_size INTEGER,\n                        hidden_size INTEGER,\n                        hidden2 INTEGER DEFAULT 24,\n                        num_outputs INTEGER,\n                        num_heads INTEGER DEFAULT 4,\n                        sequence_length INTEGER DEFAULT 24,\n                        alpha REAL,\n                        flat_size INTEGER,\n                        loo_cv_score REAL,\n                        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(season, archive_type)\n                    )\n                "
                )
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS ai_seasonal_archive_weights (\n                        season TEXT NOT NULL,\n                        archive_type TEXT NOT NULL,\n                        weight_type TEXT NOT NULL,\n                        weight_index INTEGER NOT NULL,\n                        weight_value REAL NOT NULL,\n                        PRIMARY KEY(season, archive_type, weight_type, weight_index)\n                    ) WITHOUT ROWID\n                "
                )
                await self._db.commit()
                _LOGGER.info("Migration: Created seasonal model archive tables")
        except Exception as error:
            _LOGGER.warning("Migration seasonal_archive failed: %s", error)
        return None if True else None

    async def _migrate_weather_cache_snowfall_rain(self) -> None:
        """Add snowfall and rain columns to weather_cache_open_meteo. @zara"""
        try:
            async with self._db.execute(
                "PRAGMA table_info(weather_cache_open_meteo)"
            ) as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
            migration_statements = []
            if "snowfall" not in column_names:
                migration_statements.append(
                    "ALTER TABLE weather_cache_open_meteo ADD COLUMN snowfall REAL"
                )
            if "rain" not in column_names:
                migration_statements.append(
                    "ALTER TABLE weather_cache_open_meteo ADD COLUMN rain REAL"
                )
            if migration_statements:
                for statement in migration_statements:
                    await self._db.execute(statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration: Added %d columns (snowfall/rain) to weather_cache_open_meteo",
                    len(migration_statements),
                )
        except Exception as error:
            _LOGGER.warning("Migration weather_cache_snowfall_rain failed: %s", error)
        return None if True else None

    async def _migrate_hourly_weather_actual_condition(self) -> None:
        """Add condition column to hourly_weather_actual if missing."""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "condition" not in column_names:
            await self._db.execute(
                "ALTER TABLE hourly_weather_actual ADD COLUMN condition TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Added 'condition' column to hourly_weather_actual")
        return None if True else None

    async def _migrate_hourly_predictions_v16(self) -> None:
        """Add V16 fields to hourly_predictions if missing. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        migration_statements = []
        if "weather_alert_type" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN weather_alert_type TEXT"
            )
        if "exclude_from_learning" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN exclude_from_learning BOOLEAN DEFAULT FALSE"
            )
        if "mppt_throttled" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN mppt_throttled BOOLEAN DEFAULT FALSE"
            )
        if "mppt_throttle_reason" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN mppt_throttle_reason TEXT"
            )
        if "has_panel_group_actuals" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN has_panel_group_actuals BOOLEAN DEFAULT FALSE"
            )
        if "panel_group_predictions_backfilled" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN panel_group_predictions_backfilled BOOLEAN DEFAULT FALSE"
            )
        if "adaptive_corrected" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN adaptive_corrected BOOLEAN DEFAULT FALSE"
            )
        if "adaptive_correction_time" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN adaptive_correction_time TIMESTAMP"
            )
        if migration_statements:
            for statement in migration_statements:
                await self._db.execute(statement)
            await self._db.commit()
            _LOGGER.info(
                "Migration: Added %d V16 fields to hourly_predictions",
                len(migration_statements),
            )
        return None if True else None

    async def _migrate_shadow_analysis_table_v16(self) -> None:
        """Create daily_summary_shadow_analysis table if missing. @zara"""
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='daily_summary_shadow_analysis'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS daily_summary_shadow_analysis (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    date DATE NOT NULL UNIQUE,\n                    shadow_hours_count INTEGER DEFAULT 0,\n                    cumulative_loss_kwh REAL DEFAULT 0.0,\n                    FOREIGN KEY (date) REFERENCES daily_summaries(date) ON DELETE CASCADE\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Created daily_summary_shadow_analysis table")
        return None if True else None

    async def _migrate_weather_precision_v161(self) -> None:
        """Create weather precision tables for hourly and weather-specific factors. @zara

        V16.1 migration: Adds fine-grained precision learning with hourly factors
        and weather-specific (clear/cloudy) factors.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='weather_precision_hourly_factors'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS weather_precision_hourly_factors (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    factor_type TEXT NOT NULL CHECK(factor_type IN (\n                        'solar_radiation_wm2', 'clouds', 'temperature',\n                        'humidity', 'wind', 'rain', 'pressure'\n                    )),\n                    factor_value REAL NOT NULL DEFAULT 1.0,\n                    sample_count INTEGER DEFAULT 0,\n                    confidence REAL DEFAULT 0.0,\n                    std_dev REAL,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(hour, factor_type)\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Created weather_precision_hourly_factors table")
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='weather_precision_weather_specific'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS weather_precision_weather_specific (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    weather_type TEXT NOT NULL CHECK(weather_type IN ('clear', 'cloudy', 'mixed')),\n                    factor_type TEXT NOT NULL CHECK(factor_type IN (\n                        'solar_radiation_wm2', 'clouds', 'temperature',\n                        'humidity', 'wind', 'rain', 'pressure'\n                    )),\n                    factor_value REAL NOT NULL DEFAULT 1.0,\n                    sample_days INTEGER DEFAULT 0,\n                    confidence REAL DEFAULT 0.0,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(weather_type, factor_type)\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Created weather_precision_weather_specific table")
        return None if True else None

    async def _cleanup_ai_model_weights_blobs_v161(self) -> None:
        """Remove legacy JSON blobs from ai_model_weights table. @zara

        V16.1 cleanup: The ai_model_weights table was storing full weight dicts as JSON,
        which caused inconsistencies with has_attention flag. Now we use only
        structured tables (ai_lstm_weights, ai_ridge_weights, etc.).
        """
        async with self._db.execute("SELECT COUNT(*) FROM ai_model_weights") as cursor:
            legacy_blob_count_row = await cursor.fetchone()
        if legacy_blob_count_row and legacy_blob_count_row[0] > 0:
            await self._db.execute("DELETE FROM ai_model_weights")
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.1: Deleted %d legacy JSON blobs from ai_model_weights. Structured tables are now the single source of truth.",
                legacy_blob_count_row[0],
            )
        ridge_metadata_row = await self.fetchone(
            "SELECT flat_size FROM ai_ridge_meta WHERE id = 1"
        )
        if ridge_metadata_row and ridge_metadata_row[0]:
            flat_size = ridge_metadata_row[0]
            async with self._db.execute(
                "SELECT COUNT(*) FROM ai_ridge_weights WHERE col_index > ?",
                (flat_size,),
            ) as cursor:
                extra_weight_count_row = await cursor.fetchone()
            if extra_weight_count_row and extra_weight_count_row[0] > 0:
                await self._db.execute(
                    "DELETE FROM ai_ridge_weights WHERE col_index > ?", (flat_size,)
                )
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Removed %d extra Ridge weight entries (col_index > %d). This fixes the flat_size mismatch.",
                    extra_weight_count_row[0],
                    flat_size,
                )
        return None if True else None

    async def _migrate_frost_analysis_table_v161(self) -> None:
        """Create daily_summary_frost_analysis table if missing. @zara V16.1

        This table stores frost detection results for each day.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='daily_summary_frost_analysis'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS daily_summary_frost_analysis (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    date DATE NOT NULL UNIQUE,\n                    hours_analyzed INTEGER,\n                    frost_detected BOOLEAN,\n                    total_affected_hours INTEGER,\n                    heavy_frost_hours INTEGER,\n                    light_frost_hours INTEGER,\n                    FOREIGN KEY (date) REFERENCES daily_summaries(date) ON DELETE CASCADE\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created daily_summary_frost_analysis table")
        return None if True else None

    async def _migrate_visibility_learning_table_v161(self) -> None:
        """Create/update visibility_learning table. @zara V16.1

        This table stores fog detection learning data.
        V16.1 adds: visibility_threshold_m, fog_visibility_threshold_m,
                    samples_below_threshold, samples_above_threshold
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='visibility_learning'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS visibility_learning (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    version TEXT DEFAULT '1.0',\n                    has_solar_radiation_sensor BOOLEAN DEFAULT FALSE,\n                    last_learning_date DATE,\n                    total_fog_hours_learned INTEGER DEFAULT 0,\n                    total_fog_light_hours_learned INTEGER DEFAULT 0,\n                    bright_sky_fog_hits INTEGER DEFAULT 0,\n                    pirate_weather_fog_hits INTEGER DEFAULT 0,\n                    learning_sessions INTEGER DEFAULT 0,\n                    fog_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_pirate_weather_weight REAL DEFAULT 0.5,\n                    fog_light_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_light_pirate_weather_weight REAL DEFAULT 0.5,\n                    visibility_threshold_m REAL DEFAULT 5000,\n                    fog_visibility_threshold_m REAL DEFAULT 1000,\n                    samples_below_threshold INTEGER DEFAULT 0,\n                    samples_above_threshold INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created visibility_learning table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(visibility_learning)"
            ) as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
            migration_statements = []
            if "visibility_threshold_m" not in column_names:
                migration_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN visibility_threshold_m REAL DEFAULT 5000"
                )
            if "fog_visibility_threshold_m" not in column_names:
                migration_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN fog_visibility_threshold_m REAL DEFAULT 1000"
                )
            if "samples_below_threshold" not in column_names:
                migration_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_below_threshold INTEGER DEFAULT 0"
                )
            if "samples_above_threshold" not in column_names:
                migration_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_above_threshold INTEGER DEFAULT 0"
                )
            if migration_statements:
                for statement in migration_statements:
                    await self._db.execute(statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to visibility_learning",
                    len(migration_statements),
                )
        if True:
            return

    async def _migrate_snow_tracking_table_v161(self) -> None:
        """Create/update snow_tracking table. @zara V16.1

        This table stores snow coverage tracking data.
        V16.1 adds: melt_hours, detection_source, cleared_at
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='snow_tracking'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_started_at TIMESTAMP,\n                    melt_hours REAL DEFAULT 0,\n                    detection_source TEXT,\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created snow_tracking table")
        else:
            async with self._db.execute("PRAGMA table_info(snow_tracking)") as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
            migration_statements = []
            if "melt_hours" not in column_names:
                migration_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN melt_hours REAL DEFAULT 0"
                )
            if "detection_source" not in column_names:
                migration_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN detection_source TEXT"
                )
            if "cleared_at" not in column_names:
                migration_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN cleared_at TIMESTAMP"
                )
            if migration_statements:
                for statement in migration_statements:
                    await self._db.execute(statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to snow_tracking",
                    len(migration_statements),
                )
        if True:
            return

    async def _migrate_weather_forecast_weather_code_v161(self) -> None:
        """Add weather_code column to weather_forecast table. @zara V16.1

        Used for snow detection via Open-Meteo weather codes.
        """
        async with self._db.execute("PRAGMA table_info(weather_forecast)") as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "weather_code" not in column_names:
            await self._db.execute(
                "ALTER TABLE weather_forecast ADD COLUMN weather_code INTEGER"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.1: Added weather_code column to weather_forecast"
            )
        return None if True else None

    async def _migrate_shadow_pattern_learning_v162(self) -> None:
        """V16.2: Create shadow pattern learning tables. @zara"""
        created_tables = []
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_hourly'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_hourly (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        shadow_occurrence_rate REAL DEFAULT 0.0,\n                        avg_shadow_percent REAL DEFAULT 0.0,\n                        std_dev_shadow_percent REAL DEFAULT 0.0,\n                        pct_weather_clouds REAL DEFAULT 0.0,\n                        pct_building_tree REAL DEFAULT 0.0,\n                        pct_low_sun REAL DEFAULT 0.0,\n                        pct_other REAL DEFAULT 0.0,\n                        pattern_type TEXT DEFAULT 'unknown' CHECK(pattern_type IN (\n                            'no_shadow', 'occasional', 'frequent', 'fixed_obstruction', 'unknown'\n                        )),\n                        confidence REAL DEFAULT 0.0,\n                        sample_count INTEGER DEFAULT 0,\n                        shadow_days INTEGER DEFAULT 0,\n                        clear_days INTEGER DEFAULT 0,\n                        first_learned DATE,\n                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(hour)\n                    )\n                "
                )
                created_tables.append("shadow_pattern_hourly")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_seasonal'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_seasonal (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        month INTEGER NOT NULL CHECK(month >= 1 AND month <= 12),\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        shadow_occurrence_rate REAL DEFAULT 0.0,\n                        avg_shadow_percent REAL DEFAULT 0.0,\n                        std_dev_shadow_percent REAL DEFAULT 0.0,\n                        dominant_cause TEXT DEFAULT 'unknown',\n                        sample_count INTEGER DEFAULT 0,\n                        shadow_days INTEGER DEFAULT 0,\n                        confidence REAL DEFAULT 0.0,\n                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(month, hour)\n                    )\n                "
                )
                created_tables.append("shadow_pattern_seasonal")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_learning_history'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_learning_history (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        date DATE NOT NULL,\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        shadow_detected BOOLEAN NOT NULL,\n                        shadow_type TEXT,\n                        shadow_percent REAL,\n                        root_cause TEXT,\n                        confidence REAL,\n                        sun_elevation_deg REAL,\n                        cloud_cover_percent REAL,\n                        theoretical_max_kwh REAL,\n                        actual_kwh REAL,\n                        efficiency_ratio REAL,\n                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(date, hour)\n                    )\n                "
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_shadow_learning_history_date ON shadow_learning_history(date)"
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_shadow_learning_history_hour ON shadow_learning_history(hour)"
                )
                created_tables.append("shadow_learning_history")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_config'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_config (\n                        id INTEGER PRIMARY KEY CHECK (id = 1),\n                        rolling_window_days INTEGER DEFAULT 30,\n                        min_samples_for_pattern INTEGER DEFAULT 7,\n                        ema_alpha REAL DEFAULT 0.15,\n                        fixed_obstruction_threshold REAL DEFAULT 0.7,\n                        total_days_learned INTEGER DEFAULT 0,\n                        total_hours_learned INTEGER DEFAULT 0,\n                        last_learning_date DATE,\n                        patterns_detected INTEGER DEFAULT 0,\n                        fixed_obstructions_detected INTEGER DEFAULT 0,\n                        version TEXT DEFAULT '1.0',\n                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                    )\n                "
                )
                created_tables.append("shadow_pattern_config")
        if created_tables:
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.2: Created shadow pattern learning tables: %s",
                ", ".join(created_tables),
            )
        return None if True else None

    async def _migrate_sensor_storage_tables_v1600(self) -> None:
        """V16.0.0: Create sensor storage tables for live data. @zara

        Creates tables for:
        - sensor_power_live: Rolling 2-day power (Watt) data
        - sensor_energy_hourly: Permanent hourly energy (kWh) data
        - sensor_weather: Permanent 5-min weather sensor readings
        """
        created_tables = []
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_power_live'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS sensor_power_live (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,\n                        power_watt REAL,\n                        solar_to_battery_watt REAL\n                    )\n                "
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sensor_power_live_timestamp ON sensor_power_live(timestamp)"
                )
                created_tables.append("sensor_power_live")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_energy_hourly'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS sensor_energy_hourly (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,\n                        date DATE NOT NULL,\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        yield_total_kwh REAL,\n                        yield_gruppe1_kwh REAL,\n                        yield_gruppe2_kwh REAL,\n                        consumption_kwh REAL,\n                        UNIQUE(date, hour)\n                    )\n                "
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sensor_energy_hourly_date ON sensor_energy_hourly(date)"
                )
                created_tables.append("sensor_energy_hourly")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_weather'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS sensor_weather (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,\n                        temperature REAL,\n                        humidity REAL,\n                        wind_speed REAL,\n                        rain REAL,\n                        lux REAL,\n                        pressure REAL,\n                        solar_radiation REAL\n                    )\n                "
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sensor_weather_timestamp ON sensor_weather(timestamp)"
                )
                created_tables.append("sensor_weather")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sensor_monthly_stats'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS sensor_monthly_stats (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        year INTEGER NOT NULL,\n                        month INTEGER NOT NULL CHECK(month >= 1 AND month <= 12),\n                        yield_total_kwh REAL,\n                        consumption_total_kwh REAL,\n                        avg_autarky_percent REAL,\n                        avg_accuracy_percent REAL,\n                        peak_power_w REAL,\n                        peak_power_date DATE,\n                        production_days INTEGER,\n                        best_day_kwh REAL,\n                        best_day_date DATE,\n                        worst_day_kwh REAL,\n                        worst_day_date DATE,\n                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(year, month)\n                    )\n                "
                )
                await self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sensor_monthly_stats_year_month ON sensor_monthly_stats(year, month)"
                )
                created_tables.append("sensor_monthly_stats")
        if created_tables:
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Created sensor storage tables: %s",
                ", ".join(created_tables),
            )
        return None if True else None

    async def _migrate_raw_prediction_values_v1600(self) -> None:
        """V16.0.0: Add raw physics/AI prediction values to hourly_predictions and panel groups. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            table_info = await cursor.fetchall()
            prediction_columns = {column[1] for column in table_info}
        prediction_migrations = []
        if "physics_kwh" not in prediction_columns:
            prediction_migrations.append(
                "ALTER TABLE hourly_predictions ADD COLUMN physics_kwh REAL"
            )
        if "ai_kwh" not in prediction_columns:
            prediction_migrations.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ai_kwh REAL"
            )
        if "ai_confidence" not in prediction_columns:
            prediction_migrations.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ai_confidence REAL"
            )
        if prediction_migrations:
            for statement in prediction_migrations:
                await self._db.execute(statement)
            _LOGGER.info(
                "Migration V16.0.0: Added %d raw value columns to hourly_predictions",
                len(prediction_migrations),
            )
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            table_info = await cursor.fetchall()
            group_columns = {column[1] for column in table_info}
        group_migrations = []
        if "physics_kwh" not in group_columns:
            group_migrations.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN physics_kwh REAL"
            )
        if "ai_kwh" not in group_columns:
            group_migrations.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN ai_kwh REAL"
            )
        if group_migrations:
            for statement in group_migrations:
                await self._db.execute(statement)
            _LOGGER.info(
                "Migration V16.0.0: Added %d raw value columns to prediction_panel_groups",
                len(group_migrations),
            )
        if prediction_migrations or group_migrations:
            await self._db.commit()
        return None if True else None

    async def _migrate_method_performance_learning_v1600(self) -> None:
        """V16.0.0: Create method_performance_learning table for blending weight learning. @zara"""
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='method_performance_learning'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS method_performance_learning (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        cloud_bucket TEXT NOT NULL CHECK(cloud_bucket IN ('clear', 'partly_cloudy', 'overcast')),\n                        hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                        physics_mae REAL DEFAULT 0.0,\n                        ai_mae REAL DEFAULT 0.0,\n                        blend_mae REAL DEFAULT 0.0,\n                        ai_advantage_factor REAL DEFAULT 1.0,\n                        sample_count INTEGER DEFAULT 0,\n                        last_updated TIMESTAMP,\n                        UNIQUE(cloud_bucket, hour_bucket)\n                    )\n                "
                )
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.0.0: Created method_performance_learning table"
                )
        return None if True else None

    async def _migrate_hourly_weather_actual_snow_frost_v1600(self) -> None:
        """V16.0.0: Add frost/snow tracking columns to hourly_weather_actual. @zara

        These columns are used by data_weather_actual_tracker for frost notifications
        and snow event detection/clearing progress.
        """
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        migration_statements = []
        if "frost_notification_sent" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN frost_notification_sent BOOLEAN DEFAULT 0"
            )
        if "frost_type" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN frost_type TEXT"
            )
        if "snow_confidence" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_confidence REAL"
            )
        if "snow_event_detected" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_event_detected BOOLEAN DEFAULT 0"
            )
        if "snow_clearing_progress" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_clearing_progress REAL"
            )
        if migration_statements:
            for statement in migration_statements:
                await self._db.execute(statement)
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added %d frost/snow columns to hourly_weather_actual",
                len(migration_statements),
            )
        return None if True else None

    async def _migrate_hourly_weather_actual_precipitation_semantics(self) -> None:
        """Persist rain counter baselines and rate-only observations separately."""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        migration_statements = []
        if "precipitation_counter_mm" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_counter_mm REAL"
            )
        if "precipitation_rate_max_mm_h" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_rate_max_mm_h REAL"
            )
        if "precipitation_semantics" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_semantics TEXT"
            )
        if "precipitation_quality" not in column_names:
            migration_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_quality TEXT"
            )
        if migration_statements:
            for statement in migration_statements:
                await self._db.execute(statement)
            await self._db.commit()
            _LOGGER.info(
                "Migration: Added %d precipitation semantic columns to hourly_weather_actual",
                len(migration_statements),
            )
        cursor = await self._db.execute(
            "UPDATE hourly_weather_actual\n               SET precipitation_mm = NULL,\n                   precipitation_quality = COALESCE(precipitation_quality, ?)\n               WHERE precipitation_semantics IS NULL\n                 AND precipitation_mm IS NOT NULL",
            ("legacy_unclassified",),
        )
        if cursor.rowcount:
            await self._db.commit()
            _LOGGER.info(
                "Migration: invalidated %d unclassified legacy precipitation values",
                cursor.rowcount,
            )
        return None if True else None

    async def _migrate_weather_cache_open_meteo_code_v1600(self) -> None:
        """V16.0.0: Add weather_code column to weather_cache_open_meteo. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(weather_cache_open_meteo)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "weather_code" not in column_names:
            await self._db.execute(
                "ALTER TABLE weather_cache_open_meteo ADD COLUMN weather_code INTEGER"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added weather_code column to weather_cache_open_meteo"
            )
        return None if True else None

    async def _migrate_missing_indexes_v1600(self) -> None:
        """V16.0.0: Create missing indexes and clean up duplicates. @zara"""
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_weather_precision_hourly_hour ON weather_precision_hourly_factors(hour)"
        ]
        for index_statement in index_statements:
            try:
                await self._db.execute(index_statement)
            except Exception:
                continue
        await self._db.commit()

    async def _migrate_peak_power_tracking_v1600(self) -> None:
        """V16.0.0: Add today's peak + all-time peak record columns to production_time_state
        and peak_power_time to daily_summaries for AI learning. @zara"""
        added_columns = []
        async with self._db.execute(
            "PRAGMA table_info(production_time_state)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        for column, column_definition in (
            ("peak_power_w", "REAL DEFAULT 0"),
            ("peak_power_time", "TEXT"),
            ("peak_record_w", "REAL"),
            ("peak_record_date", "TEXT"),
            ("peak_record_time", "TEXT"),
        ):
            if column not in column_names:
                await self._db.execute(
                    f"ALTER TABLE production_time_state ADD COLUMN {column} {column_definition}"
                )
                added_columns.append(f"production_time_state.{column}")
        async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "peak_power_time" not in column_names:
            await self._db.execute(
                "ALTER TABLE daily_summaries ADD COLUMN peak_power_time TEXT"
            )
            added_columns.append("daily_summaries.peak_power_time")
        if added_columns:
            try:
                peak_row = await self._db.fetchone(
                    "SELECT kwh * 1000 as peak_w, date, hour\n                       FROM astronomy_hourly_peaks\n                       WHERE kwh IS NOT NULL AND kwh > 0\n                       ORDER BY kwh DESC LIMIT 1"
                )
                if peak_row and peak_row[0]:
                    await self._db.execute(
                        "UPDATE production_time_state\n                           SET peak_record_w = ?, peak_record_date = ?, peak_record_time = ?\n                           WHERE id = 1 AND (peak_record_w IS NULL OR peak_record_w < ?)",
                        (
                            float(peak_row[0]),
                            str(peak_row[1]),
                            f"{int(peak_row[2]):02d}:00",
                            float(peak_row[0]),
                        ),
                    )
                    _LOGGER.info(
                        "Migration V16.0.0: Seeded all-time peak: %.1f W on %s at %02d:00",
                        float(peak_row[0]),
                        peak_row[1],
                        int(peak_row[2]),
                    )
            except Exception as error:
                _LOGGER.debug("Could not seed all-time peak: %s", error)
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Peak power tracking: %s", ", ".join(added_columns)
            )
        return None if True else None

    async def _migrate_dni_history_dedup_v1600(self) -> None:
        """V16.0.0: Deduplicate ai_dni_history table. @zara"""
        try:
            async with self._db.execute(
                "SELECT COUNT(*) FROM ai_dni_history\n                   WHERE id NOT IN (\n                       SELECT MIN(id) FROM ai_dni_history\n                       GROUP BY hour, dni_value, date(recorded_at)\n                   )"
            ) as cursor:
                duplicate_count_row = await cursor.fetchone()
            if duplicate_count_row and duplicate_count_row[0] > 0:
                await self._db.execute(
                    "DELETE FROM ai_dni_history WHERE id NOT IN (\n                           SELECT MIN(id) FROM ai_dni_history\n                           GROUP BY hour, dni_value, date(recorded_at)\n                       )"
                )
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.0.0: Removed %d duplicate DNI history entries",
                    duplicate_count_row[0],
                )
        except Exception as error:
            _LOGGER.debug("Migration V16.0.0 DNI dedup skipped: %s", error)
        return None if True else None

    async def _migrate_grid_search_model_type_v1600(self) -> None:
        """V16.0.0: Add model_type column to ai_grid_search_results. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(ai_grid_search_results)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "model_type" not in column_names:
            await self._db.execute(
                "ALTER TABLE ai_grid_search_results ADD COLUMN model_type TEXT DEFAULT 'lstm'"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added 'model_type' to ai_grid_search_results"
            )
        return None if True else None

    async def _migrate_prediction_weather_diffuse_v1600(self) -> None:
        """V16.0.0: Add diffuse_radiation column to prediction_weather. @zara"""
        async with self._db.execute("PRAGMA table_info(prediction_weather)") as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "diffuse_radiation" not in column_names:
            await self._db.execute(
                "ALTER TABLE prediction_weather ADD COLUMN diffuse_radiation REAL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added 'diffuse_radiation' to prediction_weather"
            )
        return None if True else None

    async def _migrate_prediction_weather_direct_radiation_v1601(self) -> None:
        """V16.0.1: Add direct_radiation column to prediction_weather. @zara"""
        async with self._db.execute("PRAGMA table_info(prediction_weather)") as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "direct_radiation" not in column_names:
            await self._db.execute(
                "ALTER TABLE prediction_weather ADD COLUMN direct_radiation REAL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.1: Added 'direct_radiation' to prediction_weather"
            )
        return None if True else None

    async def _migrate_ensemble_columns_v1602(self) -> None:
        """V16.0.2: Add lstm_kwh and ridge_kwh columns for ensemble diagnostics. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            table_info = await cursor.fetchall()
            prediction_columns = {column[1] for column in table_info}
        prediction_migrations = []
        if "lstm_kwh" not in prediction_columns:
            prediction_migrations.append(
                "ALTER TABLE hourly_predictions ADD COLUMN lstm_kwh REAL"
            )
        if "ridge_kwh" not in prediction_columns:
            prediction_migrations.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ridge_kwh REAL"
            )
        if prediction_migrations:
            for statement in prediction_migrations:
                await self._db.execute(statement)
            _LOGGER.info(
                "Migration V16.0.2: Added %d ensemble columns to hourly_predictions",
                len(prediction_migrations),
            )
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            table_info = await cursor.fetchall()
            group_columns = {column[1] for column in table_info}
        group_migrations = []
        if "lstm_kwh" not in group_columns:
            group_migrations.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN lstm_kwh REAL"
            )
        if "ridge_kwh" not in group_columns:
            group_migrations.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN ridge_kwh REAL"
            )
        if group_migrations:
            for statement in group_migrations:
                await self._db.execute(statement)
            _LOGGER.info(
                "Migration V16.0.2: Added %d ensemble columns to prediction_panel_groups",
                len(group_migrations),
            )
        if prediction_migrations or group_migrations:
            await self._db.commit()
        return None if True else None

    async def _migrate_ensemble_group_weights_v1603(self) -> None:
        """V16.0.3: Create ensemble_group_weights table for per-group adaptive blending. @zara"""
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ensemble_group_weights'"
        ) as cursor:
            schema_row = await cursor.fetchone()
        if not schema_row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ensemble_group_weights (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    group_name TEXT NOT NULL,\n                    cloud_bucket TEXT NOT NULL CHECK(cloud_bucket IN ('clear', 'partly_cloudy', 'overcast')),\n                    hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                    lstm_weight REAL DEFAULT 0.85,\n                    ridge_weight REAL DEFAULT 0.15,\n                    lstm_mae REAL DEFAULT 0.0,\n                    ridge_mae REAL DEFAULT 0.0,\n                    sample_count INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP,\n                    UNIQUE(group_name, cloud_bucket, hour_bucket)\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.0.3: Created ensemble_group_weights table")
        return None if True else None

    async def _migrate_ridge_meta_normalization_v1604(self) -> None:
        """V16.0.4: Add feature_means_json/feature_stds_json to ai_ridge_meta. @zara"""
        try:
            async with self._db.execute("PRAGMA table_info(ai_ridge_meta)") as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
        except Exception:
            return
        if "feature_means_json" not in column_names:
            await self._db.execute(
                "ALTER TABLE ai_ridge_meta ADD COLUMN feature_means_json TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.0.4: Added feature_means_json to ai_ridge_meta")
        if "feature_stds_json" not in column_names:
            await self._db.execute(
                "ALTER TABLE ai_ridge_meta ADD COLUMN feature_stds_json TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.0.4: Added feature_stds_json to ai_ridge_meta")
        return None if True else None

    async def _migrate_per_group_v170(self) -> None:
        """V17.0.0: Per-group shadow/snow, drift detection, seasonal buckets."""
        migration_changes = []
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            table_info = await cursor.fetchall()
            prediction_group_columns = {column[1] for column in table_info}
        additional_columns = {
            "exclude_from_learning_group": "BOOLEAN DEFAULT FALSE",
            "exclusion_reason_group": "TEXT",
            "snow_covered_group": "BOOLEAN DEFAULT FALSE",
            "shadow_type_group": "TEXT",
        }
        for column_name, column_definition in additional_columns.items():
            if column_name not in prediction_group_columns:
                await self._db.execute(
                    f"ALTER TABLE prediction_panel_groups ADD COLUMN {column_name} {column_definition}"
                )
                migration_changes.append(f"ppg.{column_name}")
        needs_rebuild = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_pattern_hourly)"
        ) as cursor:
            table_rows = await cursor.fetchall()
            hourly_shadow_columns = {table_column[1] for table_column in table_rows}
            if "group_name" in hourly_shadow_columns:
                needs_rebuild = False
        if needs_rebuild and hourly_shadow_columns:
            await self._db.execute(
                "\n                CREATE TABLE IF NOT EXISTS shadow_pattern_hourly_new (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    group_name TEXT NOT NULL DEFAULT '_system_',\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    shadow_occurrence_rate REAL DEFAULT 0.0,\n                    avg_shadow_percent REAL DEFAULT 0.0,\n                    std_dev_shadow_percent REAL DEFAULT 0.0,\n                    pct_weather_clouds REAL DEFAULT 0.0,\n                    pct_building_tree REAL DEFAULT 0.0,\n                    pct_low_sun REAL DEFAULT 0.0,\n                    pct_other REAL DEFAULT 0.0,\n                    pattern_type TEXT DEFAULT 'unknown' CHECK(pattern_type IN (\n                        'no_shadow', 'occasional', 'frequent', 'fixed_obstruction', 'unknown'\n                    )),\n                    confidence REAL DEFAULT 0.0,\n                    sample_count INTEGER DEFAULT 0,\n                    shadow_days INTEGER DEFAULT 0,\n                    clear_days INTEGER DEFAULT 0,\n                    first_learned DATE,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(group_name, hour)\n                )\n            "
            )
            await self._db.execute(
                "\n                INSERT INTO shadow_pattern_hourly_new\n                    (hour, shadow_occurrence_rate, avg_shadow_percent, std_dev_shadow_percent,\n                     pct_weather_clouds, pct_building_tree, pct_low_sun, pct_other,\n                     pattern_type, confidence, sample_count, shadow_days, clear_days,\n                     first_learned, last_updated)\n                SELECT hour, shadow_occurrence_rate, avg_shadow_percent, std_dev_shadow_percent,\n                       pct_weather_clouds, pct_building_tree, pct_low_sun, pct_other,\n                       pattern_type, confidence, sample_count, shadow_days, clear_days,\n                       first_learned, last_updated\n                FROM shadow_pattern_hourly\n            "
            )
            await self._db.execute("DROP TABLE shadow_pattern_hourly")
            await self._db.execute(
                "ALTER TABLE shadow_pattern_hourly_new RENAME TO shadow_pattern_hourly"
            )
            migration_changes.append("rebuild:shadow_pattern_hourly")
        needs_rebuild = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_pattern_seasonal)"
        ) as cursor:
            table_rows = await cursor.fetchall()
            seasonal_shadow_columns = {table_column[1] for table_column in table_rows}
            if "group_name" in seasonal_shadow_columns:
                needs_rebuild = False
        if needs_rebuild and seasonal_shadow_columns:
            await self._db.execute(
                "\n                CREATE TABLE IF NOT EXISTS shadow_pattern_seasonal_new (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    group_name TEXT NOT NULL DEFAULT '_system_',\n                    month INTEGER NOT NULL CHECK(month >= 1 AND month <= 12),\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    shadow_occurrence_rate REAL DEFAULT 0.0,\n                    avg_shadow_percent REAL DEFAULT 0.0,\n                    std_dev_shadow_percent REAL DEFAULT 0.0,\n                    dominant_cause TEXT DEFAULT 'unknown',\n                    sample_count INTEGER DEFAULT 0,\n                    shadow_days INTEGER DEFAULT 0,\n                    confidence REAL DEFAULT 0.0,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(group_name, month, hour)\n                )\n            "
            )
            await self._db.execute(
                "\n                INSERT INTO shadow_pattern_seasonal_new\n                    (month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                     std_dev_shadow_percent, dominant_cause, sample_count,\n                     shadow_days, confidence, last_updated)\n                SELECT month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                       std_dev_shadow_percent, dominant_cause, sample_count,\n                       shadow_days, confidence, last_updated\n                FROM shadow_pattern_seasonal\n            "
            )
            await self._db.execute("DROP TABLE shadow_pattern_seasonal")
            await self._db.execute(
                "ALTER TABLE shadow_pattern_seasonal_new RENAME TO shadow_pattern_seasonal"
            )
            migration_changes.append("rebuild:shadow_pattern_seasonal")
        needs_rebuild = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_learning_history)"
        ) as cursor:
            table_rows = await cursor.fetchall()
            shadow_history_columns = {table_column[1] for table_column in table_rows}
            if "group_name" in shadow_history_columns:
                needs_rebuild = False
        if needs_rebuild and shadow_history_columns:
            await self._db.execute(
                "\n                CREATE TABLE IF NOT EXISTS shadow_learning_history_new (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    group_name TEXT NOT NULL DEFAULT '_system_',\n                    date DATE NOT NULL,\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    shadow_detected BOOLEAN NOT NULL,\n                    shadow_type TEXT,\n                    shadow_percent REAL,\n                    root_cause TEXT,\n                    confidence REAL,\n                    sun_elevation_deg REAL,\n                    cloud_cover_percent REAL,\n                    theoretical_max_kwh REAL,\n                    actual_kwh REAL,\n                    efficiency_ratio REAL,\n                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(group_name, date, hour)\n                )\n            "
            )
            await self._db.execute(
                "\n                INSERT INTO shadow_learning_history_new\n                    (date, hour, shadow_detected, shadow_type, shadow_percent,\n                     root_cause, confidence, sun_elevation_deg, cloud_cover_percent,\n                     theoretical_max_kwh, actual_kwh, efficiency_ratio, created_at)\n                SELECT date, hour, shadow_detected, shadow_type, shadow_percent,\n                       root_cause, confidence, sun_elevation_deg, cloud_cover_percent,\n                       theoretical_max_kwh, actual_kwh, efficiency_ratio, created_at\n                FROM shadow_learning_history\n            "
            )
            await self._db.execute("DROP TABLE shadow_learning_history")
            await self._db.execute(
                "ALTER TABLE shadow_learning_history_new RENAME TO shadow_learning_history"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_learning_history_date ON shadow_learning_history(date)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_learning_history_hour ON shadow_learning_history(hour)"
            )
            migration_changes.append("rebuild:shadow_learning_history")
        needs_rebuild = True
        async with self._db.execute(
            "PRAGMA table_info(method_performance_learning)"
        ) as cursor:
            table_rows = await cursor.fetchall()
            performance_columns = {table_column[1] for table_column in table_rows}
            if "season" in performance_columns:
                needs_rebuild = False
        if needs_rebuild and performance_columns:
            await self._db.execute(
                "\n                CREATE TABLE IF NOT EXISTS method_performance_learning_new (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    cloud_bucket TEXT NOT NULL CHECK(cloud_bucket IN ('clear', 'partly_cloudy', 'overcast')),\n                    hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                    physics_mae REAL DEFAULT 0.0,\n                    ai_mae REAL DEFAULT 0.0,\n                    blend_mae REAL DEFAULT 0.0,\n                    ai_advantage_factor REAL DEFAULT 1.0,\n                    sample_count INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP,\n                    season TEXT DEFAULT NULL,\n                    UNIQUE(cloud_bucket, hour_bucket, season)\n                )\n            "
            )
            await self._db.execute(
                "\n                INSERT INTO method_performance_learning_new\n                    (cloud_bucket, hour_bucket, physics_mae, ai_mae, blend_mae,\n                     ai_advantage_factor, sample_count, last_updated)\n                SELECT cloud_bucket, hour_bucket, physics_mae, ai_mae, blend_mae,\n                       ai_advantage_factor, sample_count, last_updated\n                FROM method_performance_learning\n            "
            )
            await self._db.execute("DROP TABLE method_performance_learning")
            await self._db.execute(
                "ALTER TABLE method_performance_learning_new RENAME TO method_performance_learning"
            )
            migration_changes.append("rebuild:method_performance_learning")
        needs_rebuild = True
        async with self._db.execute(
            "PRAGMA table_info(ensemble_group_weights)"
        ) as cursor:
            table_rows = await cursor.fetchall()
            ensemble_columns = {table_column[1] for table_column in table_rows}
            if "season" in ensemble_columns:
                needs_rebuild = False
        if needs_rebuild and ensemble_columns:
            await self._db.execute(
                "\n                CREATE TABLE IF NOT EXISTS ensemble_group_weights_new (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    group_name TEXT NOT NULL,\n                    cloud_bucket TEXT NOT NULL CHECK(cloud_bucket IN ('clear', 'partly_cloudy', 'overcast')),\n                    hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                    lstm_weight REAL DEFAULT 0.85,\n                    ridge_weight REAL DEFAULT 0.15,\n                    lstm_mae REAL DEFAULT 0.0,\n                    ridge_mae REAL DEFAULT 0.0,\n                    sample_count INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP,\n                    season TEXT DEFAULT NULL,\n                    UNIQUE(group_name, cloud_bucket, hour_bucket, season)\n                )\n            "
            )
            await self._db.execute(
                "\n                INSERT INTO ensemble_group_weights_new\n                    (group_name, cloud_bucket, hour_bucket, lstm_weight, ridge_weight,\n                     lstm_mae, ridge_mae, sample_count, last_updated)\n                SELECT group_name, cloud_bucket, hour_bucket, lstm_weight, ridge_weight,\n                       lstm_mae, ridge_mae, sample_count, last_updated\n                FROM ensemble_group_weights\n            "
            )
            await self._db.execute("DROP TABLE ensemble_group_weights")
            await self._db.execute(
                "ALTER TABLE ensemble_group_weights_new RENAME TO ensemble_group_weights"
            )
            migration_changes.append("rebuild:ensemble_group_weights")
        new_tables = {
            "snow_tracking_groups": "\n                CREATE TABLE IF NOT EXISTS snow_tracking_groups (\n                    group_name TEXT NOT NULL UNIQUE,\n                    tilt_deg REAL NOT NULL DEFAULT 30.0,\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_hours REAL DEFAULT 0,\n                    tilt_melt_factor REAL DEFAULT 1.0,\n                    detection_source TEXT DEFAULT 'unknown',\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )",
            "shadow_detection_groups": "\n                CREATE TABLE IF NOT EXISTS shadow_detection_groups (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    prediction_id TEXT NOT NULL,\n                    group_name TEXT NOT NULL,\n                    shadow_type TEXT,\n                    shadow_percent REAL,\n                    confidence REAL,\n                    root_cause TEXT,\n                    efficiency_ratio REAL,\n                    loss_kwh REAL,\n                    theoretical_max_kwh REAL,\n                    actual_kwh REAL,\n                    FOREIGN KEY (prediction_id) REFERENCES hourly_predictions(prediction_id) ON DELETE CASCADE,\n                    UNIQUE(prediction_id, group_name)\n                )",
            "drift_metrics_rolling": "\n                CREATE TABLE IF NOT EXISTS drift_metrics_rolling (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    scope TEXT NOT NULL,\n                    window_days INTEGER NOT NULL,\n                    season TEXT,\n                    mae REAL,\n                    rmse REAL,\n                    bias REAL,\n                    coverage_10 REAL,\n                    coverage_20 REAL,\n                    sample_count INTEGER,\n                    calculated_at TIMESTAMP,\n                    UNIQUE(scope, window_days, season)\n                )",
            "drift_metrics_bucket": "\n                CREATE TABLE IF NOT EXISTS drift_metrics_bucket (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    scope TEXT NOT NULL,\n                    cloud_bucket TEXT NOT NULL,\n                    hour_bucket TEXT NOT NULL,\n                    season TEXT,\n                    mae REAL,\n                    mae_baseline REAL,\n                    mae_ratio REAL,\n                    bias REAL,\n                    sample_count INTEGER,\n                    calculated_at TIMESTAMP,\n                    UNIQUE(scope, cloud_bucket, hour_bucket, season)\n                )",
            "drift_events": "\n                CREATE TABLE IF NOT EXISTS drift_events (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    event_date DATE NOT NULL,\n                    scope TEXT NOT NULL,\n                    drift_type TEXT NOT NULL,\n                    severity TEXT NOT NULL,\n                    bucket_detail TEXT,\n                    metric_value REAL,\n                    threshold_value REAL,\n                    description TEXT,\n                    response_action TEXT,\n                    response_executed BOOLEAN DEFAULT FALSE,\n                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )",
            "drift_cusum_state": "\n                CREATE TABLE IF NOT EXISTS drift_cusum_state (\n                    scope TEXT NOT NULL UNIQUE,\n                    cusum_pos REAL DEFAULT 0,\n                    cusum_neg REAL DEFAULT 0,\n                    target_mean REAL DEFAULT 0,\n                    last_reset DATE,\n                    alert_count INTEGER DEFAULT 0\n                )",
            "drift_response_config": "\n                CREATE TABLE IF NOT EXISTS drift_response_config (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    mae_ratio_warning REAL DEFAULT 1.15,\n                    mae_ratio_critical REAL DEFAULT 1.25,\n                    bias_threshold REAL DEFAULT 0.15,\n                    cusum_threshold REAL DEFAULT 5.0,\n                    coverage_20_min REAL DEFAULT 0.60,\n                    physics_boost_amount REAL DEFAULT 0.20,\n                    physics_boost_max_days INTEGER DEFAULT 7,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )",
        }
        for table_name, create_statement in new_tables.items():
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cursor:
                if not await cursor.fetchone():
                    await self._db.execute(create_statement)
                    migration_changes.append(f"table:{table_name}")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_detection_groups_prediction ON shadow_detection_groups(prediction_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_drift_events_date ON drift_events(event_date)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_drift_events_scope ON drift_events(scope)"
        )
        if migration_changes:
            await self._db.commit()
            _LOGGER.info(
                "Migration V17.0.0: Per-group + drift + seasonal — %s",
                ", ".join(migration_changes),
            )
        return None if True else None

    async def _migrate_gold_standard_v1640(self) -> None:
        """V16.4.0: outlier_severity on hourly_weather_actual. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            table_info = await cursor.fetchall()
            column_names = {column[1] for column in table_info}
        if "outlier_severity" not in column_names:
            await self._db.execute(
                "ALTER TABLE hourly_weather_actual ADD COLUMN outlier_severity REAL DEFAULT NULL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.0: Added outlier_severity to hourly_weather_actual"
            )
        return None if True else None

    async def _backfill_astronomy_enrichment_v1640(self) -> None:
        """V16.4.0: Backfill prediction_astronomy enrichment from stored sunrise/sunset/noon. @zara"""
        try:
            async with self._db.execute(
                "SELECT pa.prediction_id, pa.sunrise, pa.sunset, pa.solar_noon, hp.target_hour\n                   FROM prediction_astronomy pa\n                   JOIN hourly_predictions hp ON hp.prediction_id = pa.prediction_id\n                   WHERE (pa.day_progress_ratio IS NULL OR pa.hours_after_sunrise IS NULL\n                          OR pa.hours_before_sunset IS NULL OR pa.hours_since_solar_noon IS NULL)\n                     AND pa.sunrise IS NOT NULL AND pa.sunset IS NOT NULL\n                   LIMIT 5000"
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return
            backfilled_rows = 0
            for row in rows:
                prediction_id = row[0]
                sunrise_text = row[1]
                sunset_text = row[2]
                solar_noon_text = row[3]
                hour = row[4] if row[4] is not None else 12
                try:
                    sunrise_dt = self._parse_astronomy_ts(sunrise_text)
                    sunset_dt = self._parse_astronomy_ts(sunset_text)
                    if sunrise_dt is None or sunset_dt is None:
                        continue
                    sunrise_naive = (
                        sunrise_dt.replace(tzinfo=None)
                        if sunrise_dt.tzinfo
                        else sunrise_dt
                    )
                    sunset_naive = (
                        sunset_dt.replace(tzinfo=None)
                        if sunset_dt.tzinfo
                        else sunset_dt
                    )
                    hour_midpoint = sunrise_naive.replace(
                        hour=hour, minute=30, second=0, microsecond=0
                    )
                    duration_seconds = (sunset_naive - sunrise_naive).total_seconds()
                    elapsed_seconds = (hour_midpoint - sunrise_naive).total_seconds()
                    day_progress = (
                        max(0.0, min(1.0, elapsed_seconds / duration_seconds))
                        if duration_seconds > 0
                        else 0.5
                    )
                    after_sunrise = max(0.0, elapsed_seconds / 3600.0)
                    before_sunset = max(
                        0.0, (sunset_naive - hour_midpoint).total_seconds() / 3600.0
                    )
                    since_noon = None
                    if solar_noon_text:
                        noon_parsed = self._parse_astronomy_ts(solar_noon_text)
                        if noon_parsed:
                            noon_naive = (
                                noon_parsed.replace(tzinfo=None)
                                if noon_parsed.tzinfo
                                else noon_parsed
                            )
                            since_noon = (
                                hour_midpoint - noon_naive
                            ).total_seconds() / 3600.0
                    await self._db.execute(
                        "UPDATE prediction_astronomy\n                           SET day_progress_ratio = ?,\n                               hours_after_sunrise = ?,\n                               hours_before_sunset = ?,\n                               hours_since_solar_noon = ?\n                           WHERE prediction_id = ?\n                             AND day_progress_ratio IS NULL",
                        (
                            round(day_progress, 6),
                            round(after_sunrise, 4),
                            round(before_sunset, 4),
                            round(since_noon, 4) if since_noon is not None else None,
                            prediction_id,
                        ),
                    )
                    backfilled_rows += 1
                except Exception:
                    continue
            if backfilled_rows > 0:
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Backfilled %d prediction_astronomy enrichment rows",
                    backfilled_rows,
                )
        except Exception:
            pass
        return None if True else None

    async def _cleanup_null_season_duplicates_v1640(self) -> None:
        """V16.4.0: Remove NULL-season duplicates and reset poisoned learning data. @zara"""
        try:
            duplicate_bucket_count = 0
            for table_name, bucket_columns in (
                ("method_performance_learning", "cloud_bucket, hour_bucket"),
                ("ensemble_group_weights", "group_name, cloud_bucket, hour_bucket"),
            ):
                async with self._db.execute(
                    f"SELECT {bucket_columns}, COUNT(*) as cnt\n                        FROM {table_name}\n                        WHERE season IS NULL\n                        GROUP BY {bucket_columns}\n                        HAVING cnt > 1"
                ) as cursor:
                    duplicate_buckets = await cursor.fetchall()
                    duplicate_bucket_count += len(duplicate_buckets)
            if duplicate_bucket_count > 0:
                await self._db.execute("DELETE FROM method_performance_learning")
                await self._db.execute("DELETE FROM ensemble_group_weights")
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Reset method_performance_learning and ensemble_group_weights (%d duplicate buckets found)",
                    duplicate_bucket_count,
                )
        except Exception:
            pass
        return None if True else None

    async def _migrate_group_method_performance_v1640(self) -> None:
        """V16.4.0: Per-group method performance learning table. @zara

        Tracks Physics vs AI (LSTM/Ridge) accuracy per panel group,
        condition bucket, and season. Enables data-driven safeguard decisions.
        """
        try:
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='group_method_performance'"
            ) as cursor:
                schema_entry = await cursor.fetchone()
            if not schema_entry:
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS group_method_performance (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        group_name TEXT NOT NULL,\n                        cloud_bucket TEXT NOT NULL CHECK(cloud_bucket IN ('clear', 'partly_cloudy', 'overcast')),\n                        hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                        season TEXT DEFAULT NULL,\n                        physics_mae REAL DEFAULT 0.0,\n                        ai_mae REAL DEFAULT 0.0,\n                        lstm_mae REAL DEFAULT 0.0,\n                        ridge_mae REAL DEFAULT 0.0,\n                        blend_mae REAL DEFAULT 0.0,\n                        best_method TEXT DEFAULT 'physics',\n                        ai_advantage_factor REAL DEFAULT 1.0,\n                        sample_count INTEGER DEFAULT 0,\n                        last_updated TIMESTAMP,\n                        UNIQUE(group_name, cloud_bucket, hour_bucket, season)\n                    )\n                "
                )
                await self._db.execute(
                    "\n                    CREATE INDEX IF NOT EXISTS idx_group_method_perf_lookup\n                    ON group_method_performance(group_name, cloud_bucket, hour_bucket, season)\n                "
                )
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Created group_method_performance table"
                )
        except Exception as error:
            _LOGGER.debug("group_method_performance migration: %s", error)
        return None if True else None

    async def _selfheal_stale_snow_v1642(self) -> None:
        """V16.4.2: One-time migration to clear stale snow data. @zara

        Runs exactly ONCE per installation. Uses migration_applied table
        to track execution. Will not interfere with future snow detections.
        """
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS migration_applied (\n                       migration_id TEXT PRIMARY KEY,\n                       applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = 'selfheal_snow_v1642'"
            ) as cursor:
                migration_marker = await cursor.fetchone()
            if migration_marker:
                return
            await self._db.execute(
                "UPDATE snow_tracking\n                   SET panels_covered_since = NULL, melt_hours = 0,\n                       estimated_depth_mm = 0.0,\n                       cleared_at = CURRENT_TIMESTAMP\n                   WHERE id = 1"
            )
            async with self._db.execute(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE weather_alert_type = 'snow_covered_panels'\n                   AND target_date >= date('now', '-7 days')"
            ) as cursor:
                stale_count_row = await cursor.fetchone()
            stale_prediction_count = stale_count_row[0] if stale_count_row else 0
            if stale_prediction_count > 0:
                await self._db.execute(
                    "UPDATE hourly_predictions\n                       SET weather_alert_type = NULL,\n                           has_weather_alert = 0,\n                           exclude_from_learning = 0\n                       WHERE weather_alert_type = 'snow_covered_panels'\n                       AND target_date >= date('now', '-7 days')"
                )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES ('selfheal_snow_v1642')"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.2: Snow self-heal applied — cleared snow_tracking + %d stale hourly flags (one-time)",
                stale_prediction_count,
            )
        except Exception:
            pass
        return None if True else None

    async def _goldstandard_reset_v1642(self) -> None:
        """V16.4.2 Gold Standard: One-time reset of contaminated learning data. @zara"""
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS migration_applied (\n                       migration_id TEXT PRIMARY KEY,\n                       applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = 'goldstandard_reset_v1642'"
            ) as cursor:
                migration_marker = await cursor.fetchone()
            if migration_marker:
                return
            reset_components = []
            await self._db.execute(
                "UPDATE weather_precision_factors\n                   SET solar_factor = 1.0, confidence = 0.0,\n                       updated_at = CURRENT_TIMESTAMP\n                   WHERE id = 1"
            )
            reset_components.append("solar_factor")
            await self._db.execute(
                "DELETE FROM weather_precision_hourly_factors\n                   WHERE factor_type = 'solar_radiation_wm2'"
            )
            reset_components.append("hourly_solar")
            await self._db.execute(
                "DELETE FROM weather_precision_weather_specific\n                   WHERE factor_type = 'solar_radiation_wm2'"
            )
            reset_components.append("ws_solar")
            await self._db.execute(
                "UPDATE physics_calibration_groups\n                   SET global_factor = 1.0, sample_count = 0, confidence = 0.0"
            )
            await self._db.execute("DELETE FROM physics_calibration_hourly")
            await self._db.execute("DELETE FROM physics_calibration_buckets")
            await self._db.execute("DELETE FROM physics_calibration_history")
            reset_components.append("thermodynamics_calibrator")
            await self._db.execute("DELETE FROM method_performance_learning")
            await self._db.execute("DELETE FROM group_method_performance")
            await self._db.execute("DELETE FROM ensemble_group_weights")
            reset_components.append("learning_tables")
            await self._db.execute(
                "UPDATE weather_precision_daily_summary\n                   SET avg_solar_factor = 1.0\n                   WHERE date >= date('now', '-7 days')"
            )
            reset_components.append("daily_summary")
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES ('goldstandard_reset_v1642')"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.2 Gold Standard: Reset %s (one-time)",
                ", ".join(reset_components),
            )
        except Exception as error:
            _LOGGER.warning("Gold Standard reset: %s", error)
        return None if True else None

    async def _migrate_ai_v300(self) -> None:
        """AI V3.0 architecture migration. @zara"""
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS migration_applied (\n                       migration_id TEXT PRIMARY KEY,\n                       applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = 'ai_v300'"
            ) as cursor:
                migration_marker = await cursor.fetchone()
            if migration_marker:
                return
            async with self._db.execute("PRAGMA table_info(ai_lstm_meta)") as cursor:
                table_info = await cursor.fetchall()
                column_names = {column[1] for column in table_info}
            if "num_layers" not in column_names:
                await self._db.execute(
                    "ALTER TABLE ai_lstm_meta ADD COLUMN num_layers INTEGER DEFAULT 1"
                )
            if "num_heads" not in column_names:
                await self._db.execute(
                    "ALTER TABLE ai_lstm_meta ADD COLUMN num_heads INTEGER DEFAULT 1"
                )
            await self._db.execute("DROP TABLE IF EXISTS ai_lstm_weights")
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ai_lstm_weights (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       weight_type TEXT NOT NULL,\n                       weight_index INTEGER NOT NULL,\n                       weight_value REAL NOT NULL,\n                       UNIQUE(weight_type, weight_index)\n                   )"
            )
            await self._db.execute("DELETE FROM ai_learned_weights_meta WHERE id = 1")
            await self._db.execute("DELETE FROM ai_lstm_meta WHERE id = 1")
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES ('ai_v300')"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.8.0: AI V3.0 architecture upgrade")
        except Exception as error:
            _LOGGER.warning("AI V3.0 migration: %s", error)
        return None if True else None

    @staticmethod
    def _parse_astronomy_ts(ts_str):
        """Parse astronomy timestamp with timezone offset support. @zara V16.4.0"""
        if not ts_str:
            return
        try:
            timestamp_text = str(ts_str).strip()
            return datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            for timestamp_format in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%H:%M:%S",
                "%H:%M",
            ):
                try:
                    return datetime.strptime(timestamp_text, timestamp_format)
                except (ValueError, TypeError):
                    continue
        if True:
            return

    async def save_seasonal_factors(
        self, factors: Dict[str, float], sample_counts: Dict[str, int]
    ) -> None:
        """Save seasonal factors to database."""
        for month_key, factor in factors.items():
            month = int(month_key)
            sample_count = sample_counts.get(month_key, 0)
            await self._db.execute(
                "INSERT INTO ai_seasonal_factors (month, factor, sample_count, updated_at)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(month) DO UPDATE SET\n                       factor = excluded.factor,\n                       sample_count = excluded.sample_count,\n                       updated_at = excluded.updated_at",
                (month, factor, sample_count, datetime.now()),
            )
        await self._db.commit()

    async def get_seasonal_factors(self) -> Dict[int, Tuple[float, int]]:
        """Get all seasonal factors."""
        async with self._db.execute(
            "SELECT month, factor, sample_count FROM ai_seasonal_factors"
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: (row[1], row[2]) for row in rows}
        return None if True else None

    async def save_feature_importance(self, data: Dict[str, Any]) -> None:
        """Save feature importance analysis."""
        timestamp = _safe_dt(data.get("timestamp"))
        for feature_name, importance in data["feature_importance"].items():
            if feature_name in data["helpful_features"]:
                category = "helpful"
            elif feature_name in data["harmful_features"]:
                category = "harmful"
            else:
                category = "neutral"
            await self._db.execute(
                "INSERT INTO ai_feature_importance\n                   (feature_name, importance, category, baseline_rmse, num_samples,\n                    analysis_time_seconds, timestamp)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    feature_name,
                    importance,
                    category,
                    data.get("baseline_rmse"),
                    data.get("num_samples"),
                    data.get("analysis_time_seconds"),
                    timestamp,
                ),
            )
        await self._db.commit()

    async def get_latest_feature_importance(self) -> Optional[Dict[str, Any]]:
        """Get the latest feature importance analysis."""
        async with self._db.execute(
            "SELECT feature_name, importance, category\n               FROM ai_feature_importance\n               WHERE timestamp = (SELECT MAX(timestamp) FROM ai_feature_importance)"
        ) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                return
            return {
                "feature_importance": {row[0]: row[1] for row in rows},
                "helpful_features": [row[0] for row in rows if row[2] == "helpful"],
                "harmful_features": [row[0] for row in rows if row[2] == "harmful"],
                "neutral_features": [row[0] for row in rows if row[2] == "neutral"],
            }
        return None if True else None

    async def save_grid_search_result(self, result: Dict[str, Any]) -> None:
        """Save grid search result."""
        timestamp = _safe_dt(result.get("timestamp"))
        for trial_result in result.get("all_results", []):
            trial_params = trial_result["params"]
            await self._db.execute(
                "INSERT INTO ai_grid_search_results\n                   (success, hidden_size, batch_size, learning_rate, accuracy,\n                    epochs_trained, final_val_loss, duration_seconds, is_best_result,\n                    hardware_info, timestamp, model_type)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result["success"],
                    trial_params["hidden_size"],
                    trial_params["batch_size"],
                    trial_params["learning_rate"],
                    trial_result.get("accuracy", 0),
                    trial_result.get("epochs_trained", 0),
                    trial_result.get("final_val_loss", 0),
                    trial_result.get("duration_seconds", 0),
                    trial_params == result["best_params"],
                    json.dumps(result.get("hardware_info")),
                    timestamp,
                    result.get("model_type", "lstm"),
                ),
            )
        await self._db.commit()

    async def save_dni_tracker(self, data: Dict[str, Any]) -> None:
        """Save DNI tracker data."""
        updated_date = _safe_date(data.get("last_updated"))
        for hour_key, max_dni in data["max_dni"].items():
            hour = int(hour_key)
            await self._db.execute(
                "INSERT INTO ai_dni_tracker (hour, max_dni, last_updated)\n                   VALUES (?, ?, ?)\n                   ON CONFLICT(hour) DO UPDATE SET\n                       max_dni = excluded.max_dni,\n                       last_updated = excluded.last_updated",
                (hour, max_dni, updated_date),
            )
            if hour_key in data["history"]:
                await self._db.execute(
                    "DELETE FROM ai_dni_history WHERE hour = ?", (hour,)
                )
                for dni_value in data["history"][hour_key]:
                    await self._db.execute(
                        "INSERT INTO ai_dni_history (hour, dni_value) VALUES (?, ?)",
                        (hour, dni_value),
                    )
        await self._db.commit()

    async def save_model_weights(self, weights_data: Dict[str, Any]) -> None:
        """Save AI model weights to structured tables. @zara"""
        await self._db.execute(
            "INSERT INTO ai_learned_weights_meta\n               (id, version, active_model, training_samples, last_trained, accuracy, rmse, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   version = excluded.version,\n                   active_model = excluded.active_model,\n                   training_samples = excluded.training_samples,\n                   last_trained = excluded.last_trained,\n                   accuracy = excluded.accuracy,\n                   rmse = excluded.rmse,\n                   updated_at = excluded.updated_at",
            (
                weights_data.get("version", "2.0"),
                weights_data.get("active_model", "none"),
                weights_data.get("training_samples", 0),
                weights_data.get("last_trained"),
                weights_data.get("accuracy"),
                weights_data.get("rmse"),
                datetime.now(),
            ),
        )
        lstm_data = weights_data.get("lstm")
        if lstm_data:
            lstm_data["training_samples"] = weights_data.get("training_samples")
            lstm_data["accuracy"] = weights_data.get("accuracy")
            lstm_data["rmse"] = weights_data.get("rmse")
            await self._save_lstm_weights(lstm_data)
        ridge_data = weights_data.get("ridge")
        if ridge_data:
            ridge_data["training_samples"] = weights_data.get("training_samples")
            await self._save_ridge_weights(ridge_data)
        await self._db.commit()
        _LOGGER.debug("Model weights saved to structured tables")

    async def save_seasonal_archive(
        self,
        season: str,
        archive_type: str,
        weights_data: Dict[str, Any],
        training_samples: int,
        accuracy: float,
        rmse: float,
        source_year: int,
    ) -> None:
        """Save model weights to seasonal archive. @zara V16.8.4"""
        await self._db.execute(
            "INSERT INTO ai_seasonal_archive_meta\n               (season, archive_type, training_samples, accuracy, rmse, source_year,\n                input_size, hidden_size, hidden2, num_outputs, num_heads,\n                sequence_length, alpha, flat_size, loo_cv_score, archived_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(season, archive_type) DO UPDATE SET\n                   training_samples = excluded.training_samples,\n                   accuracy = excluded.accuracy,\n                   rmse = excluded.rmse,\n                   source_year = excluded.source_year,\n                   input_size = excluded.input_size,\n                   hidden_size = excluded.hidden_size,\n                   hidden2 = excluded.hidden2,\n                   num_outputs = excluded.num_outputs,\n                   num_heads = excluded.num_heads,\n                   sequence_length = excluded.sequence_length,\n                   alpha = excluded.alpha,\n                   flat_size = excluded.flat_size,\n                   loo_cv_score = excluded.loo_cv_score,\n                   archived_at = excluded.archived_at",
            (
                season,
                archive_type,
                training_samples,
                accuracy,
                rmse,
                source_year,
                weights_data.get("input_size"),
                weights_data.get("hidden_size"),
                weights_data.get("hidden2", 24),
                weights_data.get("num_outputs"),
                weights_data.get("num_heads", 4),
                weights_data.get("sequence_length", 24),
                weights_data.get("alpha"),
                weights_data.get("flat_size"),
                weights_data.get("loo_cv_score"),
                datetime.now(),
            ),
        )
        await self._db.execute(
            "DELETE FROM ai_seasonal_archive_weights WHERE season = ? AND archive_type = ?",
            (season, archive_type),
        )
        weight_records = []
        metadata_keys = {
            "rmse",
            "alpha",
            "hidden2",
            "accuracy",
            "flat_size",
            "num_heads",
            "input_size",
            "model_type",
            "num_layers",
            "hidden_size",
            "num_outputs",
            "source_year",
            "loo_cv_score",
            "has_attention",
            "sequence_length",
            "training_samples",
        }
        for weight_type, weight_data in weights_data.items():
            if weight_type in metadata_keys:
                continue
            if not (
                isinstance(weight_data, (list,)) or hasattr(weight_data, "flatten")
            ):
                continue
            weight_array = weight_data
            if (
                isinstance(weight_array, list)
                and len(weight_array) > 0
                and isinstance(weight_array[0], list)
            ):
                flat_weights = [
                    weight_value
                    for weight_row in weight_array
                    for weight_value in weight_row
                ]
            elif isinstance(weight_array, list):
                flat_weights = weight_array
            elif hasattr(weight_array, "flatten"):
                flat_weights = weight_array.flatten().tolist()
            else:
                flat_weights = list(weight_array)
            for weight_index, weight_value in enumerate(flat_weights):
                weight_records.append(
                    (
                        season,
                        archive_type,
                        weight_type,
                        weight_index,
                        float(weight_value),
                    )
                )
            continue
        if weight_records:
            await self._db.executemany(
                "INSERT INTO ai_seasonal_archive_weights\n                   (season, archive_type, weight_type, weight_index, weight_value)\n                   VALUES (?, ?, ?, ?, ?)",
                weight_records,
            )
        await self._db.commit()
        _LOGGER.debug(
            "Seasonal archive saved: %s/%s (%d weights)",
            season,
            archive_type,
            len(weight_records),
        )

    async def load_seasonal_archive(
        self, season: str, archive_type: str
    ) -> Optional[Dict[str, Any]]:
        """Load model weights from seasonal archive. @zara V16.8.4"""
        model_metadata = await self.fetchone(
            "SELECT training_samples, accuracy, rmse, source_year,\n                      input_size, hidden_size, hidden2, num_outputs, num_heads,\n                      sequence_length, alpha, flat_size, loo_cv_score\n               FROM ai_seasonal_archive_meta\n               WHERE season = ? AND archive_type = ?",
            (season, archive_type),
        )
        if not model_metadata:
            return
        archive_data = {
            "training_samples": model_metadata[0] or 0,
            "accuracy": model_metadata[1] or 0.0,
            "rmse": model_metadata[2] or 0.0,
            "source_year": model_metadata[3],
            "input_size": model_metadata[4],
            "hidden_size": model_metadata[5],
            "hidden2": model_metadata[6],
            "num_outputs": model_metadata[7],
            "num_heads": model_metadata[8] or 4,
            "sequence_length": model_metadata[9] or 24,
            "alpha": model_metadata[10],
            "flat_size": model_metadata[11],
            "loo_cv_score": model_metadata[12],
        }
        if archive_type == "lstm":
            archive_data["has_attention"] = True
            archive_data["num_layers"] = 2
        rows = await self.fetchall(
            "SELECT weight_type, weight_index, weight_value\n               FROM ai_seasonal_archive_weights\n               WHERE season = ? AND archive_type = ?\n               ORDER BY weight_type, weight_index",
            (season, archive_type),
        )
        if not rows:
            return
        previous_weight_type = None
        collected_weights = []
        for row in rows:
            stored_weight_type, weight_index, weight_value = (row[0], row[1], row[2])
            if stored_weight_type != previous_weight_type:
                if previous_weight_type is not None:
                    archive_data[previous_weight_type] = collected_weights
                previous_weight_type = stored_weight_type
                collected_weights = []
            collected_weights.append(weight_value)
        if previous_weight_type is not None:
            archive_data[previous_weight_type] = collected_weights
        if (
            archive_type == "ridge"
            and "weights" in archive_data
            and archive_data.get("num_outputs")
            and archive_data.get("flat_size")
        ):
            flat_weights = archive_data["weights"]
            matrix_width = archive_data["flat_size"] + 1
            output_count = archive_data["num_outputs"]
            if len(flat_weights) == output_count * matrix_width:
                archive_data["weights"] = [
                    flat_weights[
                        row_index * matrix_width : (row_index + 1) * matrix_width
                    ]
                    for row_index in range(output_count)
                ]
        if archive_type == "lstm":
            hidden_size = archive_data.get("hidden_size", 48)
            second_hidden_size = archive_data.get("hidden2", 24)
            input_size = archive_data.get("input_size", 31)
            output_size = archive_data.get("num_outputs", 2)
            weight_shapes = {
                "Wf": (hidden_size, input_size + hidden_size),
                "Wi": (hidden_size, input_size + hidden_size),
                "Wc": (hidden_size, input_size + hidden_size),
                "Wo": (hidden_size, input_size + hidden_size),
                "Wf2": (second_hidden_size, hidden_size + second_hidden_size),
                "Wi2": (second_hidden_size, hidden_size + second_hidden_size),
                "Wc2": (second_hidden_size, hidden_size + second_hidden_size),
                "Wo2": (second_hidden_size, hidden_size + second_hidden_size),
                "W_query": (second_hidden_size, second_hidden_size),
                "W_key": (second_hidden_size, second_hidden_size),
                "W_value": (second_hidden_size, second_hidden_size),
                "W_attn_out": (second_hidden_size, second_hidden_size),
                "Wy": (output_size, second_hidden_size),
            }
            for weight_type, (rows, matrix_width) in weight_shapes.items():
                if (
                    weight_type in archive_data
                    and isinstance(archive_data[weight_type], list)
                    and (len(archive_data[weight_type]) == rows * matrix_width)
                ):
                    flat_weights = archive_data[weight_type]
                    archive_data[weight_type] = [
                        flat_weights[
                            row_index * matrix_width : (row_index + 1) * matrix_width
                        ]
                        for row_index in range(rows)
                    ]
        return archive_data

    async def _save_lstm_weights(self, lstm_data: Dict[str, Any]) -> None:
        """Save LSTM weights to ai_lstm_weights table. @zara"""
        await self._db.execute(
            "INSERT INTO ai_lstm_meta\n               (id, input_size, hidden_size, sequence_length, num_outputs, has_attention,\n                num_layers, num_heads, training_samples, accuracy, rmse, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   input_size = excluded.input_size,\n                   hidden_size = excluded.hidden_size,\n                   sequence_length = excluded.sequence_length,\n                   num_outputs = excluded.num_outputs,\n                   has_attention = excluded.has_attention,\n                   num_layers = excluded.num_layers,\n                   num_heads = excluded.num_heads,\n                   training_samples = excluded.training_samples,\n                   accuracy = excluded.accuracy,\n                   rmse = excluded.rmse,\n                   updated_at = excluded.updated_at",
            (
                lstm_data.get("input_size"),
                lstm_data.get("hidden_size"),
                lstm_data.get("sequence_length", 24),
                lstm_data.get("num_outputs"),
                lstm_data.get("has_attention", True),
                lstm_data.get("num_layers", 2),
                lstm_data.get("num_heads", 4),
                lstm_data.get("training_samples"),
                lstm_data.get("accuracy"),
                lstm_data.get("rmse"),
                datetime.now(),
            ),
        )
        await self._db.execute("DELETE FROM ai_lstm_weights")
        for weight_type, weight_data in lstm_data.items():
            if not (
                isinstance(weight_data, (list,)) or hasattr(weight_data, "flatten")
            ):
                continue
            if weight_type in (
                "input_size",
                "hidden_size",
                "sequence_length",
                "num_outputs",
                "has_attention",
                "num_layers",
                "num_heads",
                "training_samples",
                "accuracy",
                "rmse",
                "model_type",
            ):
                continue
            weight_array = weight_data
            if (
                isinstance(weight_array, list)
                and len(weight_array) > 0
                and isinstance(weight_array[0], list)
            ):
                flat_weights = [
                    weight_value
                    for weight_row in weight_array
                    for weight_value in weight_row
                ]
            elif isinstance(weight_array, list):
                flat_weights = weight_array
            elif hasattr(weight_array, "flatten"):
                flat_weights = weight_array.flatten().tolist()
            else:
                flat_weights = list(weight_array)
            for weight_index, weight_value in enumerate(flat_weights):
                await self._db.execute(
                    "INSERT OR REPLACE INTO ai_lstm_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                    (weight_type, weight_index, float(weight_value)),
                )
            continue

    async def _save_ridge_weights(self, ridge_data: Dict[str, Any]) -> None:
        """Save Ridge weights to ai_ridge_weights table. @zara"""
        feature_means_json = None
        feature_stds_json = None
        if ridge_data.get("feature_means") is not None:
            feature_means_json = json.dumps(ridge_data["feature_means"])
        if ridge_data.get("feature_stds") is not None:
            feature_stds_json = json.dumps(ridge_data["feature_stds"])
        await self._db.execute(
            "INSERT INTO ai_ridge_meta\n               (id, model_type, alpha, input_size, hidden_size, sequence_length,\n                num_outputs, flat_size, trained_samples, loo_cv_score, accuracy, rmse,\n                feature_means_json, feature_stds_json, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   model_type = excluded.model_type,\n                   alpha = excluded.alpha,\n                   input_size = excluded.input_size,\n                   hidden_size = excluded.hidden_size,\n                   sequence_length = excluded.sequence_length,\n                   num_outputs = excluded.num_outputs,\n                   flat_size = excluded.flat_size,\n                   trained_samples = excluded.trained_samples,\n                   loo_cv_score = excluded.loo_cv_score,\n                   accuracy = excluded.accuracy,\n                   rmse = excluded.rmse,\n                   feature_means_json = excluded.feature_means_json,\n                   feature_stds_json = excluded.feature_stds_json,\n                   updated_at = excluded.updated_at",
            (
                ridge_data.get("model_type", "TinyRidge"),
                ridge_data.get("alpha"),
                ridge_data.get("input_size"),
                ridge_data.get("hidden_size"),
                ridge_data.get("sequence_length", 24),
                ridge_data.get("num_outputs"),
                ridge_data.get("flat_size"),
                ridge_data.get("trained_samples"),
                ridge_data.get("loo_cv_score"),
                ridge_data.get("accuracy"),
                ridge_data.get("rmse"),
                feature_means_json,
                feature_stds_json,
                datetime.now(),
            ),
        )
        await self._db.execute("DELETE FROM ai_ridge_weights")
        weights = ridge_data.get("weights")
        if not weights:
            return
        for row_index, weight_row in enumerate(weights):
            for column_index, weight_value in enumerate(weight_row):
                if weight_value != 0.0:
                    await self._db.execute(
                        "INSERT INTO ai_ridge_weights (row_index, col_index, weight_value) VALUES (?, ?, ?)",
                        (row_index, column_index, weight_value),
                    )

    async def get_model_weights(self) -> Optional[Dict[str, Any]]:
        """Get AI model weights from database. @zara

        V16.1 FIX: Now uses structured tables (ai_lstm_weights, ai_ridge_weights)
        as the primary source instead of JSON blobs in ai_model_weights.
        This ensures has_attention flag is consistent with actual attention weights.
        """
        model_metadata = await self.fetchone(
            "SELECT version, active_model, training_samples, last_trained,\n                      accuracy, rmse\n               FROM ai_learned_weights_meta WHERE id = 1"
        )
        if not model_metadata:
            return
        model_data = {
            "version": model_metadata[0] or "2.0",
            "active_model": model_metadata[1] or "none",
            "training_samples": model_metadata[2] or 0,
            "last_trained": str(model_metadata[3]) if model_metadata[3] else None,
            "accuracy": model_metadata[4],
            "rmse": model_metadata[5],
        }
        lstm_weights = await self._load_lstm_weights()
        if lstm_weights:
            model_data["lstm"] = lstm_weights
            model_data["has_attention"] = lstm_weights.get("has_attention", False)
        ridge_weights = await self._load_ridge_weights()
        if ridge_weights:
            model_data["ridge"] = ridge_weights
        return model_data

    async def _get_model_weights_legacy(self) -> Optional[Dict[str, Any]]:
        """Fallback to old ai_model_weights table. @zara"""
        rows = await self.fetchall(
            "SELECT weight_type, weight_data FROM ai_model_weights"
        )
        if not rows:
            return
        return {row[0]: json.loads(row[1]) for row in rows}

    async def _load_lstm_weights(self) -> Optional[Dict[str, Any]]:
        """Load LSTM weights from ai_lstm_weights table. @zara"""
        rows = await self.fetchall(
            "SELECT weight_type, weight_index, weight_value\n               FROM ai_lstm_weights ORDER BY weight_type, weight_index"
        )
        if not rows:
            return
        weights_by_type = {}
        for row in rows:
            stored_weight_type = row[0]
            if stored_weight_type not in weights_by_type:
                weights_by_type[stored_weight_type] = []
            weights_by_type[stored_weight_type].append(row[2])
        hidden_size = len(weights_by_type.get("bf", []))
        output_count = len(weights_by_type.get("by", []))
        if hidden_size == 0:
            return
        gate_weight_count = len(weights_by_type.get("Wf", []))
        gate_width = gate_weight_count // hidden_size if hidden_size > 0 else 0
        input_size = gate_width - hidden_size if gate_width > hidden_size else 0
        has_attention = False
        layer_count = 1
        head_count = 1
        training_samples = 0
        accuracy = None
        rmse = None
        metadata_row = await self.fetchone(
            "SELECT has_attention, training_samples, accuracy, rmse, num_layers, num_heads FROM ai_lstm_meta WHERE id = 1"
        )
        if metadata_row:
            has_attention = bool(metadata_row[0])
            training_samples = metadata_row[1] or 0
            accuracy = metadata_row[2]
            rmse = metadata_row[3]
            layer_count = metadata_row[4] or 1
            head_count = metadata_row[5] or 1
        model_data = {
            "input_size": input_size,
            "hidden_size": hidden_size,
            "num_outputs": output_count,
            "has_attention": has_attention,
            "num_layers": layer_count,
            "num_heads": head_count,
            "training_samples": training_samples,
            "accuracy": accuracy,
            "rmse": rmse,
        }
        for weight_type in ("Wf", "Wi", "Wc", "Wo"):
            if weight_type in weights_by_type:
                flat_weights = weights_by_type[weight_type]
                if gate_width > 0:
                    model_data[weight_type] = [
                        flat_weights[
                            row_index * gate_width : (row_index + 1) * gate_width
                        ]
                        for row_index in range(hidden_size)
                    ]
        if "Wy" in weights_by_type and output_count > 0:
            second_hidden_size = len(weights_by_type.get("bf2", [])) or hidden_size
            flat_weights = weights_by_type["Wy"]
            model_data["Wy"] = [
                flat_weights[
                    row_index
                    * second_hidden_size : (row_index + 1)
                    * second_hidden_size
                ]
                for row_index in range(output_count)
            ]
        for bias_type in ("bf", "bi", "bc", "bo", "by"):
            if bias_type in weights_by_type:
                model_data[bias_type] = weights_by_type[bias_type]
        if layer_count >= 2:
            second_hidden_size = len(weights_by_type.get("bf2", []))
            if second_hidden_size > 0:
                second_gate_width = (
                    len(weights_by_type.get("Wf2", [])) // second_hidden_size
                    if second_hidden_size
                    else 0
                )
                for weight_type in ("Wf2", "Wi2", "Wc2", "Wo2"):
                    if weight_type in weights_by_type and second_gate_width > 0:
                        flat_weights = weights_by_type[weight_type]
                        model_data[weight_type] = [
                            flat_weights[
                                row_index
                                * second_gate_width : (row_index + 1)
                                * second_gate_width
                            ]
                            for row_index in range(second_hidden_size)
                        ]
                for bias_type in ("bf2", "bi2", "bc2", "bo2"):
                    if bias_type in weights_by_type:
                        model_data[bias_type] = weights_by_type[bias_type]
            for normalization_type in ("ln_gamma", "ln_beta"):
                if normalization_type in weights_by_type:
                    model_data[normalization_type] = weights_by_type[normalization_type]
        attention_width = len(weights_by_type.get("bf2", [])) or hidden_size
        attention_weight_types = [
            "W_query",
            "W_key",
            "W_value",
            "W_attn_out",
            "b_attn_out",
        ]
        attention_complete = all(
            (
                attention_weight_type in weights_by_type
                for attention_weight_type in attention_weight_types
            )
        )
        if has_attention and attention_complete:
            for weight_type in ("W_query", "W_key", "W_value", "W_attn_out"):
                flat_weights = weights_by_type[weight_type]
                model_data[weight_type] = [
                    flat_weights[
                        row_index * attention_width : (row_index + 1) * attention_width
                    ]
                    for row_index in range(attention_width)
                ]
            model_data["b_attn_out"] = weights_by_type["b_attn_out"]
        elif has_attention and (not attention_complete):
            model_data["has_attention"] = False
            _LOGGER.warning(
                "ai_lstm_meta.has_attention=True but attention weights missing. Setting has_attention=False."
            )
        return model_data if True else None

    async def _load_ridge_weights(self) -> Optional[Dict[str, Any]]:
        """Load Ridge weights from ai_ridge_weights table. @zara"""
        rows = await self.fetchall(
            "SELECT row_index, col_index, weight_value\n               FROM ai_ridge_weights ORDER BY row_index, col_index"
        )
        if not rows:
            return
        max_row_index = max((row[0] for row in rows))
        max_column_index = max((row[1] for row in rows))
        output_count = max_row_index + 1
        stored_column_count = max_column_index + 1
        input_size = None
        sequence_length = 24
        flat_size = None
        alpha = None
        loo_cv_score = None
        trained_samples = 0
        accuracy = None
        rmse = None
        feature_means_json = None
        feature_stds_json = None
        metadata_row = await self.fetchone(
            "SELECT input_size, sequence_length, num_outputs, flat_size,\n                      alpha, loo_cv_score, trained_samples, accuracy, rmse,\n                      feature_means_json, feature_stds_json\n               FROM ai_ridge_meta WHERE id = 1"
        )
        if metadata_row:
            input_size = metadata_row[0]
            sequence_length = metadata_row[1] or 24
            flat_size = metadata_row[3]
            alpha = metadata_row[4]
            loo_cv_score = metadata_row[5]
            trained_samples = metadata_row[6] or 0
            accuracy = metadata_row[7]
            rmse = metadata_row[8]
            feature_means_json = metadata_row[9]
            feature_stds_json = metadata_row[10]
        if flat_size:
            matrix_width = flat_size + 1
        else:
            matrix_width = stored_column_count
        if matrix_width < stored_column_count:
            matrix_width = stored_column_count
        weights = [[0.0] * matrix_width for weight_index in range(output_count)]
        for row in rows:
            weights[row[0]][row[1]] = row[2]
        feature_means = None
        feature_stds = None
        if feature_means_json:
            try:
                feature_means = json.loads(feature_means_json)
            except (json.JSONDecodeError, TypeError):
                pass
        if feature_stds_json:
            try:
                feature_stds = json.loads(feature_stds_json)
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "model_type": "TinyRidge",
            "weights": weights,
            "num_outputs": output_count,
            "input_size": (
                input_size
                if input_size
                else flat_size // sequence_length if flat_size else stored_column_count
            ),
            "flat_size": flat_size if flat_size else stored_column_count,
            "sequence_length": sequence_length,
            "alpha": alpha,
            "loo_cv_score": loo_cv_score,
            "trained_samples": trained_samples,
            "accuracy": accuracy,
            "rmse": rmse,
            "feature_means": feature_means,
            "feature_stds": feature_stds,
        }

    async def _migrate_weather_mlp(self) -> None:
        """Weather MLP tables migration. @zara"""
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ai_weather_mlp_weights (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       weight_type TEXT NOT NULL,\n                       weight_index INTEGER NOT NULL,\n                       weight_value REAL NOT NULL,\n                       UNIQUE(weight_type, weight_index)\n                   )"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ai_weather_mlp_meta (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       input_size INTEGER DEFAULT 8,\n                       hidden1 INTEGER DEFAULT 16,\n                       hidden2 INTEGER DEFAULT 8,\n                       training_samples INTEGER DEFAULT 0,\n                       accuracy REAL DEFAULT 0.0,\n                       rmse REAL DEFAULT 0.0,\n                       last_trained TIMESTAMP,\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            await self._db.commit()
        except Exception as error:
            _LOGGER.debug("Weather MLP migration: %s", error)
        return None if True else None

    async def save_weather_mlp_weights(self, weights_data: Dict[str, Any]) -> None:
        """Save Weather MLP weights to database. @zara"""
        try:
            await self._db.execute(
                "INSERT INTO ai_weather_mlp_meta\n                   (id, input_size, hidden1, hidden2, training_samples, accuracy, rmse,\n                    last_trained, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       input_size = excluded.input_size,\n                       hidden1 = excluded.hidden1,\n                       hidden2 = excluded.hidden2,\n                       training_samples = excluded.training_samples,\n                       accuracy = excluded.accuracy,\n                       rmse = excluded.rmse,\n                       last_trained = excluded.last_trained,\n                       updated_at = excluded.updated_at",
                (
                    weights_data.get("input_size", 8),
                    weights_data.get("hidden1", 16),
                    weights_data.get("hidden2", 8),
                    weights_data.get("training_samples", 0),
                    weights_data.get("accuracy", 0.0),
                    weights_data.get("rmse", 0.0),
                    datetime.now(),
                    datetime.now(),
                ),
            )
            await self._db.execute("DELETE FROM ai_weather_mlp_weights")
            for weight_type, weight_data in weights_data.items():
                if weight_type in (
                    "input_size",
                    "hidden1",
                    "hidden2",
                    "training_samples",
                    "accuracy",
                    "rmse",
                ):
                    continue
                if not isinstance(weight_data, (list,)):
                    continue
                flat_weights = weight_data
                if len(flat_weights) > 0 and isinstance(flat_weights[0], list):
                    flat_weights = [
                        weight_component
                        for weight_row in weight_data
                        for weight_component in weight_row
                    ]
                for weight_index, weight_value in enumerate(flat_weights):
                    await self._db.execute(
                        "INSERT OR REPLACE INTO ai_weather_mlp_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                        (weight_type, weight_index, float(weight_value)),
                    )
            await self._db.commit()
            _LOGGER.info(
                "Weather MLP weights saved: %d samples, R²=%.3f",
                weights_data.get("training_samples", 0),
                weights_data.get("accuracy", 0.0),
            )
        except Exception as error:
            _LOGGER.error("Failed to save Weather MLP weights: %s", error)
        return None if True else None

    async def load_weather_mlp_weights(self) -> Optional[Dict[str, Any]]:
        """Load Weather MLP weights from database. @zara"""
        try:
            metadata_row = await self.fetchone(
                "SELECT input_size, hidden1, hidden2, training_samples,\n                          accuracy, rmse, last_trained\n                   FROM ai_weather_mlp_meta WHERE id = 1"
            )
            if not metadata_row or not metadata_row[3]:
                return None
            rows = await self.fetchall(
                "SELECT weight_type, weight_index, weight_value\n                   FROM ai_weather_mlp_weights ORDER BY weight_type, weight_index"
            )
            if not rows:
                return
            weights_by_type = {}
            for row in rows:
                weight_type = row[0]
                if weight_type not in weights_by_type:
                    weights_by_type[weight_type] = []
                weights_by_type[weight_type].append(row[2])
            input_size = metadata_row[0] or 8
            first_hidden_size = metadata_row[1] or 16
            second_hidden_size = metadata_row[2] or 8
            model_data = {
                "input_size": input_size,
                "hidden1": first_hidden_size,
                "hidden2": second_hidden_size,
                "training_samples": metadata_row[3] or 0,
                "accuracy": metadata_row[4],
                "rmse": metadata_row[5],
            }
            matrix_shapes = {
                "W1": (first_hidden_size, input_size),
                "W2": (second_hidden_size, first_hidden_size),
                "W3": (1, second_hidden_size),
            }
            for weight_type, flat_values in weights_by_type.items():
                if weight_type in matrix_shapes:
                    matrix_rows, matrix_columns = matrix_shapes[weight_type]
                    model_data[weight_type] = [
                        flat_values[
                            row_index
                            * matrix_columns : (row_index + 1)
                            * matrix_columns
                        ]
                        for row_index in range(matrix_rows)
                    ]
                else:
                    model_data[weight_type] = flat_values
            return model_data
        except Exception as error:
            _LOGGER.debug("Could not load Weather MLP weights: %s", error)
            return

    async def save_learning_config(self, config: Dict[str, Any]) -> None:
        """Save physics learning config."""
        physics_defaults = config["physics_defaults"]
        metadata = config["metadata"]
        await self._db.execute(
            "INSERT INTO physics_learning_config\n               (id, version, albedo, system_efficiency, learned_efficiency_factor,\n                rolling_window_days, min_samples, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   version = excluded.version,\n                   albedo = excluded.albedo,\n                   system_efficiency = excluded.system_efficiency,\n                   learned_efficiency_factor = excluded.learned_efficiency_factor,\n                   rolling_window_days = excluded.rolling_window_days,\n                   min_samples = excluded.min_samples,\n                   updated_at = excluded.updated_at",
            (
                config["version"],
                physics_defaults["albedo"],
                physics_defaults["system_efficiency"],
                physics_defaults["learned_efficiency_factor"],
                metadata["rolling_window_days"],
                metadata["min_samples"],
                _safe_dt(config.get("updated_at")),
            ),
        )
        for group_key, group_data in config["group_calibration"].items():
            await self._save_group_calibration(group_key, group_data)
        await self._db.commit()

    async def _save_group_calibration(
        self, group_name: str, group_data: Dict[str, Any]
    ) -> None:
        """Save calibration data for a panel group."""
        await self._db.execute(
            "INSERT INTO physics_calibration_groups\n               (group_name, global_factor, sample_count, confidence, last_updated)\n               VALUES (?, ?, ?, ?, ?)\n               ON CONFLICT(group_name) DO UPDATE SET\n                   global_factor = excluded.global_factor,\n                   sample_count = excluded.sample_count,\n                   confidence = excluded.confidence,\n                   last_updated = excluded.last_updated",
            (
                group_name,
                group_data["global_factor"],
                group_data["sample_count"],
                group_data["confidence"],
                _safe_dt(group_data.get("last_updated")),
            ),
        )
        await self._db.execute(
            "DELETE FROM physics_calibration_hourly WHERE group_name = ?", (group_name,)
        )
        for hour_key, factor in group_data["hourly_factors"].items():
            hour = int(hour_key)
            await self._db.execute(
                "INSERT INTO physics_calibration_hourly (group_name, hour, factor) VALUES (?, ?, ?)",
                (group_name, hour, factor),
            )
        await self._db.execute(
            "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
            (group_name,),
        )
        await self._db.execute(
            "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
            (group_name,),
        )
        for bucket_name, bucket_data in group_data["bucket_factors"].items():
            await self._db.execute(
                "INSERT INTO physics_calibration_buckets\n                   (group_name, bucket_name, global_factor, sample_count, confidence)\n                   VALUES (?, ?, ?, ?, ?)",
                (
                    group_name,
                    bucket_name,
                    bucket_data["global_factor"],
                    bucket_data["sample_count"],
                    bucket_data["confidence"],
                ),
            )
            for hour_key, factor in bucket_data["hourly_factors"].items():
                hour = int(hour_key)
                await self._db.execute(
                    "INSERT INTO physics_calibration_bucket_hourly\n                       (group_name, bucket_name, hour, factor) VALUES (?, ?, ?, ?)",
                    (group_name, bucket_name, hour, factor),
                )

    async def get_group_calibration(self, group_name: str) -> Optional[Dict[str, Any]]:
        """Get calibration data for a panel group."""
        async with self._db.execute(
            "SELECT global_factor, sample_count, confidence, last_updated\n               FROM physics_calibration_groups WHERE group_name = ?",
            (group_name,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return
        hourly_factors = {}
        async with self._db.execute(
            "SELECT hour, factor FROM physics_calibration_hourly WHERE group_name = ?",
            (group_name,),
        ) as cursor:
            async for row in cursor:
                hourly_factors[str(row[0])] = row[1]
        bucket_factors = {}
        async with self._db.execute(
            "SELECT bucket_name, global_factor, sample_count, confidence\n               FROM physics_calibration_buckets WHERE group_name = ?",
            (group_name,),
        ) as cursor:
            async for row in cursor:
                bucket_name = row[0]
                bucket_factors[bucket_name] = {
                    "global_factor": row[1],
                    "sample_count": row[2],
                    "confidence": row[3],
                    "hourly_factors": {},
                }
        async with self._db.execute(
            "SELECT bucket_name, hour, factor FROM physics_calibration_bucket_hourly WHERE group_name = ?",
            (group_name,),
        ) as cursor:
            async for row in cursor:
                bucket_factors[row[0]]["hourly_factors"][str(row[1])] = row[2]
        return {
            "global_factor": row[0],
            "sample_count": row[1],
            "confidence": row[2],
            "last_updated": row[3],
            "hourly_factors": hourly_factors,
            "bucket_factors": bucket_factors,
        }

    async def save_calibration_history(
        self, history_data: List[Dict[str, Any]]
    ) -> None:
        """Save calibration history."""
        for history_entry in history_data:
            record_date = _safe_date(history_entry.get("date"))
            for group_key, group_data in history_entry["groups"].items():
                for hour_key, ratio in group_data["hourly"].items():
                    hour = int(hour_key)
                    await self._db.execute(
                        "INSERT INTO physics_calibration_history\n                           (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                           VALUES (?, ?, NULL, ?, ?, ?, ?)\n                           ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                               avg_ratio = excluded.avg_ratio,\n                               sample_count = excluded.sample_count",
                        (
                            record_date,
                            group_key,
                            hour,
                            ratio,
                            group_data["sample_count"],
                            history_entry.get("source"),
                        ),
                    )
                for bucket_name, bucket_data in group_data.get("buckets", {}).items():
                    for hour_key, ratio in bucket_data["hourly"].items():
                        hour = int(hour_key)
                        await self._db.execute(
                            "INSERT INTO physics_calibration_history\n                               (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                               VALUES (?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                                   avg_ratio = excluded.avg_ratio,\n                                   sample_count = excluded.sample_count",
                            (
                                record_date,
                                group_key,
                                bucket_name,
                                hour,
                                ratio,
                                bucket_data["sample_count"],
                                history_entry.get("source"),
                            ),
                        )
                continue
            continue
        await self._db.commit()

    async def save_weather_expert_weights(self, data: Dict[str, Any]) -> None:
        """Save weather expert weights."""
        for cloud_type, expert_weights in data["weights"].items():
            for expert_name, weight in expert_weights.items():
                await self._db.execute(
                    "INSERT INTO weather_expert_weights\n                       (cloud_type, expert_name, weight, last_updated)\n                       VALUES (?, ?, ?, ?)\n                       ON CONFLICT(cloud_type, expert_name) DO UPDATE SET\n                           weight = excluded.weight,\n                           last_updated = excluded.last_updated",
                    (
                        cloud_type,
                        expert_name,
                        weight,
                        _safe_dt(data.get("metadata", {}).get("last_updated")),
                    ),
                )
        await self._db.commit()

    async def get_weather_expert_weights(self, cloud_type: str) -> Dict[str, float]:
        """Get weather expert weights for a cloud type."""
        async with self._db.execute(
            "SELECT expert_name, weight FROM weather_expert_weights WHERE cloud_type = ?",
            (cloud_type,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        return None if True else None

    async def save_weather_source_weights(self, data: Dict[str, Any]) -> None:
        """Save weather source weights."""
        for source_name, weight in data["weights"].items():
            last_mae = data["learning_metadata"]["last_mae"].get(source_name)
            await self._db.execute(
                "INSERT INTO weather_source_weights\n                   (source_name, weight, last_mae, version, last_updated)\n                   VALUES (?, ?, ?, ?, ?)\n                   ON CONFLICT(source_name) DO UPDATE SET\n                       weight = excluded.weight,\n                       last_mae = excluded.last_mae,\n                       last_updated = excluded.last_updated",
                (
                    source_name,
                    weight,
                    last_mae,
                    data.get("version"),
                    _safe_dt(data.get("learning_metadata", {}).get("last_updated")),
                ),
            )
        await self._db.commit()

    async def save_coordinator_state(self, state: Dict[str, Any]) -> None:
        """Save coordinator state."""
        await self._db.execute(
            "INSERT INTO coordinator_state\n               (id, expected_daily_production, last_set_date, last_updated)\n               VALUES (1, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   expected_daily_production = excluded.expected_daily_production,\n                   last_set_date = excluded.last_set_date,\n                   last_updated = excluded.last_updated",
            (
                state.get("expected_daily_production"),
                _safe_date(state.get("last_set_date")),
                _safe_dt(state.get("last_updated")),
            ),
        )
        await self._db.commit()

    async def get_coordinator_state(self) -> Optional[Dict[str, Any]]:
        """Get coordinator state."""
        async with self._db.execute(
            "SELECT expected_daily_production, last_set_date FROM coordinator_state WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return
            return {"expected_daily_production": row[0], "last_set_date": str(row[1])}
        return None if True else None

    async def save_production_time_state(self, state: Dict[str, Any]) -> None:
        """Save production time state."""
        await self._db.execute(
            "INSERT INTO production_time_state\n               (id, date, accumulated_hours, is_active, start_time, production_time_today, last_updated)\n               VALUES (1, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   date = excluded.date,\n                   accumulated_hours = excluded.accumulated_hours,\n                   is_active = excluded.is_active,\n                   start_time = excluded.start_time,\n                   production_time_today = excluded.production_time_today,\n                   last_updated = excluded.last_updated",
            (
                _safe_date(state.get("date")),
                state.get("accumulated_hours", 0.0),
                state.get("is_active", False),
                state.get("start_time"),
                state.get("production_time_today", "00:00:00"),
                _safe_dt(state.get("last_updated")),
            ),
        )
        await self._db.commit()

    async def save_panel_group_sensor_state(self, state: Dict[str, Any]) -> None:
        """Save panel group sensor state."""
        for group_key, sensor_value in state["last_values"].items():
            await self._db.execute(
                "INSERT INTO panel_group_sensor_state (group_name, last_value, last_updated)\n                   VALUES (?, ?, ?)\n                   ON CONFLICT(group_name) DO UPDATE SET\n                       last_value = excluded.last_value,\n                       last_updated = excluded.last_updated",
                (group_key, sensor_value, _safe_dt(state.get("last_updated"))),
            )
        await self._db.commit()

    async def save_yield_cache(self, cache: Dict[str, Any]) -> None:
        """Save yield cache."""
        await self._db.execute(
            "INSERT INTO yield_cache (id, value, time, date)\n               VALUES (1, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   value = excluded.value,\n                   time = excluded.time,\n                   date = excluded.date",
            (
                cache.get("value"),
                _safe_dt(cache.get("time")),
                _safe_date(cache.get("date")),
            ),
        )
        await self._db.commit()

    async def save_daily_forecast_tracking(self, data: Dict[str, Any]) -> None:
        """Save daily forecast tracking data."""
        await self._db.execute(
            "INSERT INTO daily_forecast_tracking\n               (id, date, forecast_best_hour, forecast_best_hour_kwh, forecast_best_hour_locked,\n                forecast_best_hour_locked_at, forecast_best_hour_source,\n                actual_best_hour, actual_best_hour_kwh, actual_best_hour_saved_at,\n                forecast_next_hour_period, forecast_next_hour_kwh, forecast_next_hour_updated_at,\n                forecast_next_hour_source, production_time_active, production_time_duration_seconds,\n                production_time_start, production_time_end, production_time_last_power_above_10w,\n                production_time_zero_power_since, peak_today_power_w, peak_today_at,\n                yield_today_kwh, yield_today_sensor, consumption_today_kwh, consumption_today_sensor,\n                autarky_percent, autarky_calculated_at, finalized_yield_kwh, finalized_consumption_kwh,\n                finalized_production_hours, finalized_accuracy_percent, finalized_excluded_hours_count,\n                finalized_excluded_hours_total, finalized_excluded_hours_ratio,\n                finalized_excluded_hours_reasons, finalized_at, last_updated)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   date = excluded.date,\n                   forecast_best_hour = excluded.forecast_best_hour,\n                   forecast_best_hour_kwh = excluded.forecast_best_hour_kwh,\n                   forecast_best_hour_locked = excluded.forecast_best_hour_locked,\n                   forecast_best_hour_locked_at = excluded.forecast_best_hour_locked_at,\n                   forecast_best_hour_source = excluded.forecast_best_hour_source,\n                   actual_best_hour = excluded.actual_best_hour,\n                   actual_best_hour_kwh = excluded.actual_best_hour_kwh,\n                   actual_best_hour_saved_at = excluded.actual_best_hour_saved_at,\n                   forecast_next_hour_period = excluded.forecast_next_hour_period,\n                   forecast_next_hour_kwh = excluded.forecast_next_hour_kwh,\n                   forecast_next_hour_updated_at = excluded.forecast_next_hour_updated_at,\n                   forecast_next_hour_source = excluded.forecast_next_hour_source,\n                   production_time_active = excluded.production_time_active,\n                   production_time_duration_seconds = excluded.production_time_duration_seconds,\n                   production_time_start = excluded.production_time_start,\n                   production_time_end = excluded.production_time_end,\n                   production_time_last_power_above_10w = excluded.production_time_last_power_above_10w,\n                   production_time_zero_power_since = excluded.production_time_zero_power_since,\n                   peak_today_power_w = excluded.peak_today_power_w,\n                   peak_today_at = excluded.peak_today_at,\n                   yield_today_kwh = excluded.yield_today_kwh,\n                   yield_today_sensor = excluded.yield_today_sensor,\n                   consumption_today_kwh = excluded.consumption_today_kwh,\n                   consumption_today_sensor = excluded.consumption_today_sensor,\n                   autarky_percent = excluded.autarky_percent,\n                   autarky_calculated_at = excluded.autarky_calculated_at,\n                   finalized_yield_kwh = excluded.finalized_yield_kwh,\n                   finalized_consumption_kwh = excluded.finalized_consumption_kwh,\n                   finalized_production_hours = excluded.finalized_production_hours,\n                   finalized_accuracy_percent = excluded.finalized_accuracy_percent,\n                   finalized_excluded_hours_count = excluded.finalized_excluded_hours_count,\n                   finalized_excluded_hours_total = excluded.finalized_excluded_hours_total,\n                   finalized_excluded_hours_ratio = excluded.finalized_excluded_hours_ratio,\n                   finalized_excluded_hours_reasons = excluded.finalized_excluded_hours_reasons,\n                   finalized_at = excluded.finalized_at,\n                   last_updated = excluded.last_updated",
            (
                _safe_date(data.get("date")),
                data.get("forecast_best_hour", {}).get("hour"),
                data.get("forecast_best_hour", {}).get("prediction_kwh"),
                data.get("forecast_best_hour", {}).get("locked", False),
                data.get("forecast_best_hour", {}).get("locked_at"),
                data.get("forecast_best_hour", {}).get("source"),
                data.get("actual_best_hour", {}).get("hour"),
                data.get("actual_best_hour", {}).get("actual_kwh"),
                data.get("actual_best_hour", {}).get("saved_at"),
                data.get("forecast_next_hour", {}).get("period"),
                data.get("forecast_next_hour", {}).get("prediction_kwh"),
                data.get("forecast_next_hour", {}).get("updated_at"),
                data.get("forecast_next_hour", {}).get("source"),
                data.get("production_time", {}).get("active", False),
                data.get("production_time", {}).get("duration_seconds"),
                data.get("production_time", {}).get("start_time"),
                data.get("production_time", {}).get("end_time"),
                data.get("production_time", {}).get("last_power_above_10w"),
                data.get("production_time", {}).get("zero_power_since"),
                data.get("peak_today", {}).get("power_w"),
                data.get("peak_today", {}).get("at"),
                data.get("yield_today", {}).get("kwh"),
                data.get("yield_today", {}).get("sensor"),
                data.get("consumption_today", {}).get("kwh"),
                data.get("consumption_today", {}).get("sensor"),
                data.get("autarky", {}).get("percent"),
                data.get("autarky", {}).get("calculated_at"),
                data.get("finalized", {}).get("yield_kwh"),
                data.get("finalized", {}).get("consumption_kwh"),
                data.get("finalized", {}).get("production_hours"),
                data.get("finalized", {}).get("accuracy_percent"),
                data.get("finalized", {}).get("excluded_hours", {}).get("count"),
                data.get("finalized", {}).get("excluded_hours", {}).get("total"),
                data.get("finalized", {}).get("excluded_hours", {}).get("ratio"),
                json.dumps(
                    data.get("finalized", {}).get("excluded_hours", {}).get("reasons")
                ),
                data.get("finalized", {}).get("at"),
                datetime.now(),
            ),
        )
        await self._db.commit()

    async def get_daily_forecast_tracking(self) -> Optional[Dict[str, Any]]:
        """Get daily forecast tracking data."""
        row = await self.fetchone("SELECT * FROM daily_forecast_tracking WHERE id = 1")
        return (
            None
            if not row
            else {
                "date": str(row["date"]),
                "forecast_best_hour": (
                    {
                        "hour": row["forecast_best_hour"],
                        "prediction_kwh": row["forecast_best_hour_kwh"],
                        "locked": bool(row["forecast_best_hour_locked"]),
                        "locked_at": (
                            str(row["forecast_best_hour_locked_at"])
                            if row["forecast_best_hour_locked_at"]
                            else None
                        ),
                        "source": row["forecast_best_hour_source"],
                    }
                    if row["forecast_best_hour"] is not None
                    else {}
                ),
                "actual_best_hour": (
                    {
                        "hour": row["actual_best_hour"],
                        "actual_kwh": row["actual_best_hour_kwh"],
                        "saved_at": (
                            str(row["actual_best_hour_saved_at"])
                            if row["actual_best_hour_saved_at"]
                            else None
                        ),
                    }
                    if row["actual_best_hour"] is not None
                    else {}
                ),
                "forecast_next_hour": (
                    {
                        "period": row["forecast_next_hour_period"],
                        "prediction_kwh": row["forecast_next_hour_kwh"],
                        "updated_at": (
                            str(row["forecast_next_hour_updated_at"])
                            if row["forecast_next_hour_updated_at"]
                            else None
                        ),
                        "source": row["forecast_next_hour_source"],
                    }
                    if row["forecast_next_hour_period"] is not None
                    else {}
                ),
                "production_time": {
                    "active": bool(row["production_time_active"]),
                    "duration_seconds": row["production_time_duration_seconds"],
                    "start_time": (
                        str(row["production_time_start"])
                        if row["production_time_start"]
                        else None
                    ),
                    "end_time": (
                        str(row["production_time_end"])
                        if row["production_time_end"]
                        else None
                    ),
                    "last_power_above_10w": (
                        str(row["production_time_last_power_above_10w"])
                        if row["production_time_last_power_above_10w"]
                        else None
                    ),
                    "zero_power_since": (
                        str(row["production_time_zero_power_since"])
                        if row["production_time_zero_power_since"]
                        else None
                    ),
                },
                "peak_today": (
                    {
                        "power_w": row["peak_today_power_w"],
                        "at": (
                            str(row["peak_today_at"]) if row["peak_today_at"] else None
                        ),
                    }
                    if row["peak_today_power_w"] is not None
                    else {}
                ),
                "yield_today": (
                    {"kwh": row["yield_today_kwh"], "sensor": row["yield_today_sensor"]}
                    if row["yield_today_kwh"] is not None
                    else {}
                ),
                "consumption_today": (
                    {
                        "kwh": row["consumption_today_kwh"],
                        "sensor": row["consumption_today_sensor"],
                    }
                    if row["consumption_today_kwh"] is not None
                    else {}
                ),
                "autarky": (
                    {
                        "percent": row["autarky_percent"],
                        "calculated_at": (
                            str(row["autarky_calculated_at"])
                            if row["autarky_calculated_at"]
                            else None
                        ),
                    }
                    if row["autarky_percent"] is not None
                    else {}
                ),
                "finalized": (
                    {
                        "yield_kwh": row["finalized_yield_kwh"],
                        "consumption_kwh": row["finalized_consumption_kwh"],
                        "production_hours": row["finalized_production_hours"],
                        "accuracy_percent": row["finalized_accuracy_percent"],
                        "excluded_hours": (
                            {
                                "count": row["finalized_excluded_hours_count"],
                                "total": row["finalized_excluded_hours_total"],
                                "ratio": row["finalized_excluded_hours_ratio"],
                                "reasons": (
                                    json.loads(row["finalized_excluded_hours_reasons"])
                                    if row["finalized_excluded_hours_reasons"]
                                    else {}
                                ),
                            }
                            if row["finalized_excluded_hours_count"] is not None
                            else {}
                        ),
                        "at": str(row["finalized_at"]) if row["finalized_at"] else None,
                    }
                    if row["finalized_yield_kwh"] is not None
                    else {}
                ),
            }
        )

    async def save_daily_statistics(self, data: Dict[str, Any]) -> None:
        """Save daily statistics data."""
        await self._db.execute(
            "INSERT INTO daily_statistics\n               (id, all_time_peak_power_w, all_time_peak_date, all_time_peak_at,\n                current_week_period, current_week_date_range, current_week_yield_kwh,\n                current_week_consumption_kwh, current_week_days, current_week_updated_at,\n                current_month_period, current_month_yield_kwh, current_month_consumption_kwh,\n                current_month_avg_autarky, current_month_days, current_month_updated_at,\n                last_7_days_avg_yield_kwh, last_7_days_avg_accuracy, last_7_days_total_yield_kwh,\n                last_7_days_calculated_at, last_30_days_avg_yield_kwh, last_30_days_avg_accuracy,\n                last_30_days_total_yield_kwh, last_30_days_calculated_at, last_365_days_avg_yield_kwh,\n                last_365_days_total_yield_kwh, last_365_days_calculated_at, last_updated)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   all_time_peak_power_w = excluded.all_time_peak_power_w,\n                   all_time_peak_date = excluded.all_time_peak_date,\n                   all_time_peak_at = excluded.all_time_peak_at,\n                   current_week_period = excluded.current_week_period,\n                   current_week_date_range = excluded.current_week_date_range,\n                   current_week_yield_kwh = excluded.current_week_yield_kwh,\n                   current_week_consumption_kwh = excluded.current_week_consumption_kwh,\n                   current_week_days = excluded.current_week_days,\n                   current_week_updated_at = excluded.current_week_updated_at,\n                   current_month_period = excluded.current_month_period,\n                   current_month_yield_kwh = excluded.current_month_yield_kwh,\n                   current_month_consumption_kwh = excluded.current_month_consumption_kwh,\n                   current_month_avg_autarky = excluded.current_month_avg_autarky,\n                   current_month_days = excluded.current_month_days,\n                   current_month_updated_at = excluded.current_month_updated_at,\n                   last_7_days_avg_yield_kwh = excluded.last_7_days_avg_yield_kwh,\n                   last_7_days_avg_accuracy = excluded.last_7_days_avg_accuracy,\n                   last_7_days_total_yield_kwh = excluded.last_7_days_total_yield_kwh,\n                   last_7_days_calculated_at = excluded.last_7_days_calculated_at,\n                   last_30_days_avg_yield_kwh = excluded.last_30_days_avg_yield_kwh,\n                   last_30_days_avg_accuracy = excluded.last_30_days_avg_accuracy,\n                   last_30_days_total_yield_kwh = excluded.last_30_days_total_yield_kwh,\n                   last_30_days_calculated_at = excluded.last_30_days_calculated_at,\n                   last_365_days_avg_yield_kwh = excluded.last_365_days_avg_yield_kwh,\n                   last_365_days_total_yield_kwh = excluded.last_365_days_total_yield_kwh,\n                   last_365_days_calculated_at = excluded.last_365_days_calculated_at,\n                   last_updated = excluded.last_updated",
            (
                data.get("all_time_peak", {}).get("power_w"),
                data.get("all_time_peak", {}).get("date"),
                data.get("all_time_peak", {}).get("at"),
                data.get("current_week", {}).get("period"),
                data.get("current_week", {}).get("date_range"),
                data.get("current_week", {}).get("yield_kwh"),
                data.get("current_week", {}).get("consumption_kwh"),
                data.get("current_week", {}).get("days"),
                data.get("current_week", {}).get("updated_at"),
                data.get("current_month", {}).get("period"),
                data.get("current_month", {}).get("yield_kwh"),
                data.get("current_month", {}).get("consumption_kwh"),
                data.get("current_month", {}).get("avg_autarky"),
                data.get("current_month", {}).get("days"),
                data.get("current_month", {}).get("updated_at"),
                data.get("last_7_days", {}).get("avg_yield_kwh"),
                data.get("last_7_days", {}).get("avg_accuracy"),
                data.get("last_7_days", {}).get("total_yield_kwh"),
                data.get("last_7_days", {}).get("calculated_at"),
                data.get("last_30_days", {}).get("avg_yield_kwh"),
                data.get("last_30_days", {}).get("avg_accuracy"),
                data.get("last_30_days", {}).get("total_yield_kwh"),
                data.get("last_30_days", {}).get("calculated_at"),
                data.get("last_365_days", {}).get("avg_yield_kwh"),
                data.get("last_365_days", {}).get("total_yield_kwh"),
                data.get("last_365_days", {}).get("calculated_at"),
                datetime.now(),
            ),
        )
        await self._db.commit()

    async def get_daily_statistics(self) -> Optional[Dict[str, Any]]:
        """Get daily statistics data."""
        row = await self.fetchone("SELECT * FROM daily_statistics WHERE id = 1")
        return (
            None
            if not row
            else {
                "all_time_peak": (
                    {
                        "power_w": row["all_time_peak_power_w"],
                        "date": (
                            str(row["all_time_peak_date"])
                            if row["all_time_peak_date"]
                            else None
                        ),
                        "at": (
                            str(row["all_time_peak_at"])
                            if row["all_time_peak_at"]
                            else None
                        ),
                    }
                    if row["all_time_peak_power_w"] is not None
                    else {}
                ),
                "current_week": (
                    {
                        "period": row["current_week_period"],
                        "date_range": row["current_week_date_range"],
                        "yield_kwh": row["current_week_yield_kwh"],
                        "consumption_kwh": row["current_week_consumption_kwh"],
                        "days": row["current_week_days"],
                        "updated_at": (
                            str(row["current_week_updated_at"])
                            if row["current_week_updated_at"]
                            else None
                        ),
                    }
                    if row["current_week_period"] is not None
                    else {}
                ),
                "current_month": (
                    {
                        "period": row["current_month_period"],
                        "yield_kwh": row["current_month_yield_kwh"],
                        "consumption_kwh": row["current_month_consumption_kwh"],
                        "avg_autarky": row["current_month_avg_autarky"],
                        "days": row["current_month_days"],
                        "updated_at": (
                            str(row["current_month_updated_at"])
                            if row["current_month_updated_at"]
                            else None
                        ),
                    }
                    if row["current_month_period"] is not None
                    else {}
                ),
                "last_7_days": (
                    {
                        "avg_yield_kwh": row["last_7_days_avg_yield_kwh"],
                        "avg_accuracy": row["last_7_days_avg_accuracy"],
                        "total_yield_kwh": row["last_7_days_total_yield_kwh"],
                        "calculated_at": (
                            str(row["last_7_days_calculated_at"])
                            if row["last_7_days_calculated_at"]
                            else None
                        ),
                    }
                    if row["last_7_days_avg_yield_kwh"] is not None
                    else {}
                ),
                "last_30_days": (
                    {
                        "avg_yield_kwh": row["last_30_days_avg_yield_kwh"],
                        "avg_accuracy": row["last_30_days_avg_accuracy"],
                        "total_yield_kwh": row["last_30_days_total_yield_kwh"],
                        "calculated_at": (
                            str(row["last_30_days_calculated_at"])
                            if row["last_30_days_calculated_at"]
                            else None
                        ),
                    }
                    if row["last_30_days_avg_yield_kwh"] is not None
                    else {}
                ),
                "last_365_days": (
                    {
                        "avg_yield_kwh": row["last_365_days_avg_yield_kwh"],
                        "total_yield_kwh": row["last_365_days_total_yield_kwh"],
                        "calculated_at": (
                            str(row["last_365_days_calculated_at"])
                            if row["last_365_days_calculated_at"]
                            else None
                        ),
                    }
                    if row["last_365_days_avg_yield_kwh"] is not None
                    else {}
                ),
            }
        )

    async def save_forecast_history_entry(self, entry: Dict[str, Any]) -> None:
        """Save a single forecast history entry."""
        record_date = _safe_date(entry.get("date"))
        await self._db.execute(
            "INSERT INTO forecast_history\n               (date, predicted_kwh, actual_kwh, consumption_kwh, autarky, accuracy,\n                production_hours, peak_power, source, excluded_hours_count, excluded_hours_total,\n                excluded_hours_ratio, excluded_hours_reasons, archived_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(date) DO UPDATE SET\n                   predicted_kwh = excluded.predicted_kwh,\n                   actual_kwh = excluded.actual_kwh,\n                   consumption_kwh = excluded.consumption_kwh,\n                   autarky = excluded.autarky,\n                   accuracy = excluded.accuracy,\n                   production_hours = excluded.production_hours,\n                   peak_power = excluded.peak_power,\n                   source = excluded.source,\n                   excluded_hours_count = excluded.excluded_hours_count,\n                   excluded_hours_total = excluded.excluded_hours_total,\n                   excluded_hours_ratio = excluded.excluded_hours_ratio,\n                   excluded_hours_reasons = excluded.excluded_hours_reasons,\n                   archived_at = excluded.archived_at",
            (
                record_date,
                entry.get("predicted_kwh"),
                entry.get("actual_kwh"),
                entry.get("consumption_kwh"),
                entry.get("autarky"),
                entry.get("accuracy"),
                entry.get("production_hours"),
                entry.get("peak_power"),
                entry.get("source"),
                entry.get("excluded_hours", {}).get("count"),
                entry.get("excluded_hours", {}).get("total"),
                entry.get("excluded_hours", {}).get("ratio"),
                json.dumps(entry.get("excluded_hours", {}).get("reasons")),
                _safe_dt(entry.get("archived_at")),
            ),
        )
        await self._db.commit()

    async def get_forecast_history(self, limit: int = 730) -> List[Dict[str, Any]]:
        """Get forecast history entries."""
        rows = await self.fetchall(
            f"SELECT * FROM forecast_history ORDER BY date DESC LIMIT {limit}"
        )
        return [
            {
                "date": str(row["date"]),
                "predicted_kwh": row["predicted_kwh"],
                "actual_kwh": row["actual_kwh"],
                "consumption_kwh": row["consumption_kwh"],
                "autarky": row["autarky"],
                "accuracy": row["accuracy"],
                "production_hours": row["production_hours"],
                "peak_power": row["peak_power"],
                "source": row["source"],
                "excluded_hours": (
                    {
                        "count": row["excluded_hours_count"],
                        "total": row["excluded_hours_total"],
                        "ratio": row["excluded_hours_ratio"],
                        "reasons": (
                            json.loads(row["excluded_hours_reasons"])
                            if row["excluded_hours_reasons"]
                            else {}
                        ),
                    }
                    if row["excluded_hours_count"] is not None
                    else {}
                ),
                "archived_at": str(row["archived_at"]) if row["archived_at"] else None,
            }
            for row in rows
        ]

    async def save_hourly_shadow_detection(
        self, prediction_id: str, shadow_data: Dict[str, Any]
    ) -> None:
        """Save shadow detection data for an hourly prediction."""
        await self._db.execute(
            "INSERT INTO hourly_shadow_detection\n               (prediction_id, method, ensemble_mode, shadow_type, shadow_percent, confidence,\n                root_cause, fusion_mode, efficiency_ratio, loss_kwh, theoretical_max_kwh,\n                interpretation, theory_ratio_shadow_type, theory_ratio_shadow_percent,\n                theory_ratio_confidence, theory_ratio_efficiency_ratio, theory_ratio_clear_sky_wm2,\n                theory_ratio_actual_wm2, theory_ratio_loss_kwh, theory_ratio_root_cause,\n                sensor_fusion_shadow_type, sensor_fusion_shadow_percent, sensor_fusion_confidence,\n                sensor_fusion_efficiency_ratio, sensor_fusion_loss_kwh, sensor_fusion_root_cause,\n                sensor_fusion_lux_factor, sensor_fusion_lux_shadow_percent,\n                sensor_fusion_irradiance_factor, sensor_fusion_irradiance_shadow_percent,\n                weight_theory_ratio, weight_sensor_fusion)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   method = excluded.method,\n                   ensemble_mode = excluded.ensemble_mode,\n                   shadow_type = excluded.shadow_type,\n                   shadow_percent = excluded.shadow_percent,\n                   confidence = excluded.confidence,\n                   root_cause = excluded.root_cause,\n                   fusion_mode = excluded.fusion_mode,\n                   efficiency_ratio = excluded.efficiency_ratio,\n                   loss_kwh = excluded.loss_kwh,\n                   theoretical_max_kwh = excluded.theoretical_max_kwh,\n                   interpretation = excluded.interpretation,\n                   theory_ratio_shadow_type = excluded.theory_ratio_shadow_type,\n                   theory_ratio_shadow_percent = excluded.theory_ratio_shadow_percent,\n                   theory_ratio_confidence = excluded.theory_ratio_confidence,\n                   theory_ratio_efficiency_ratio = excluded.theory_ratio_efficiency_ratio,\n                   theory_ratio_clear_sky_wm2 = excluded.theory_ratio_clear_sky_wm2,\n                   theory_ratio_actual_wm2 = excluded.theory_ratio_actual_wm2,\n                   theory_ratio_loss_kwh = excluded.theory_ratio_loss_kwh,\n                   theory_ratio_root_cause = excluded.theory_ratio_root_cause,\n                   sensor_fusion_shadow_type = excluded.sensor_fusion_shadow_type,\n                   sensor_fusion_shadow_percent = excluded.sensor_fusion_shadow_percent,\n                   sensor_fusion_confidence = excluded.sensor_fusion_confidence,\n                   sensor_fusion_efficiency_ratio = excluded.sensor_fusion_efficiency_ratio,\n                   sensor_fusion_loss_kwh = excluded.sensor_fusion_loss_kwh,\n                   sensor_fusion_root_cause = excluded.sensor_fusion_root_cause,\n                   sensor_fusion_lux_factor = excluded.sensor_fusion_lux_factor,\n                   sensor_fusion_lux_shadow_percent = excluded.sensor_fusion_lux_shadow_percent,\n                   sensor_fusion_irradiance_factor = excluded.sensor_fusion_irradiance_factor,\n                   sensor_fusion_irradiance_shadow_percent = excluded.sensor_fusion_irradiance_shadow_percent,\n                   weight_theory_ratio = excluded.weight_theory_ratio,\n                   weight_sensor_fusion = excluded.weight_sensor_fusion",
            (
                prediction_id,
                shadow_data.get("method"),
                shadow_data.get("ensemble_mode"),
                shadow_data.get("shadow_type"),
                shadow_data.get("shadow_percent"),
                shadow_data.get("confidence"),
                shadow_data.get("root_cause"),
                shadow_data.get("fusion_mode"),
                shadow_data.get("efficiency_ratio"),
                shadow_data.get("loss_kwh"),
                shadow_data.get("theoretical_max_kwh"),
                shadow_data.get("interpretation"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("shadow_type"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("shadow_percent"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("confidence"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("efficiency_ratio"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("clear_sky_wm2"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("actual_wm2"),
                shadow_data.get("methods", {}).get("theory_ratio", {}).get("loss_kwh"),
                shadow_data.get("methods", {})
                .get("theory_ratio", {})
                .get("root_cause"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("shadow_type"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("shadow_percent"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("confidence"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("efficiency_ratio"),
                shadow_data.get("methods", {}).get("sensor_fusion", {}).get("loss_kwh"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("root_cause"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("lux_analysis", {})
                .get("factor"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("lux_analysis", {})
                .get("shadow_percent"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("irradiance_factor"),
                shadow_data.get("methods", {})
                .get("sensor_fusion", {})
                .get("irradiance_shadow_percent"),
                shadow_data.get("weights", {}).get("theory_ratio"),
                shadow_data.get("weights", {}).get("sensor_fusion"),
            ),
        )
        await self._db.commit()

    async def save_hourly_production_metrics(
        self, prediction_id: str, metrics: Dict[str, Any]
    ) -> None:
        """Save production metrics for an hourly prediction."""
        await self._db.execute(
            "INSERT INTO hourly_production_metrics (prediction_id, peak_power_today_kwh, production_hours_today, cumulative_today_kwh)\n               VALUES (?, ?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   peak_power_today_kwh = excluded.peak_power_today_kwh,\n                   production_hours_today = excluded.production_hours_today,\n                   cumulative_today_kwh = excluded.cumulative_today_kwh",
            (
                prediction_id,
                metrics.get("peak_power_today_kwh"),
                metrics.get("production_hours_today"),
                metrics.get("cumulative_today_kwh"),
            ),
        )
        await self._db.commit()

    async def save_hourly_historical_context(
        self, prediction_id: str, context: Dict[str, Any]
    ) -> None:
        """Save historical context for an hourly prediction."""
        await self._db.execute(
            "INSERT INTO hourly_historical_context (prediction_id, yesterday_same_hour, same_hour_avg_7days)\n               VALUES (?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   yesterday_same_hour = excluded.yesterday_same_hour,\n                   same_hour_avg_7days = excluded.same_hour_avg_7days",
            (
                prediction_id,
                context.get("yesterday_same_hour"),
                context.get("same_hour_avg_7days"),
            ),
        )
        await self._db.commit()

    async def save_hourly_panel_group_accuracy(
        self, prediction_id: str, group_name: str, accuracy_data: Dict[str, Any]
    ) -> None:
        """Save panel group accuracy data for an hourly prediction."""
        await self._db.execute(
            "INSERT INTO hourly_panel_group_accuracy\n               (prediction_id, group_name, prediction_kwh, actual_kwh, error_kwh, error_percent, accuracy_percent)\n               VALUES (?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                   prediction_kwh = excluded.prediction_kwh,\n                   actual_kwh = excluded.actual_kwh,\n                   error_kwh = excluded.error_kwh,\n                   error_percent = excluded.error_percent,\n                   accuracy_percent = excluded.accuracy_percent",
            (
                prediction_id,
                group_name,
                accuracy_data.get("prediction_kwh"),
                accuracy_data.get("actual_kwh"),
                accuracy_data.get("error_kwh"),
                accuracy_data.get("error_percent"),
                accuracy_data.get("accuracy_percent"),
            ),
        )
        await self._db.commit()

    async def save_daily_patterns(
        self, date_val: date, patterns: List[Dict[str, Any]]
    ) -> None:
        """Save patterns for a daily summary."""
        await self._db.execute("DELETE FROM daily_patterns WHERE date = ?", (date_val,))
        for pattern in patterns:
            await self._db.execute(
                "INSERT INTO daily_patterns\n                   (date, pattern_type, hours, severity, avg_error_percent, confidence,\n                    first_detected, occurrence_count, seasonal)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    date_val,
                    pattern.get("type"),
                    json.dumps(pattern.get("hours", [])),
                    pattern.get("severity"),
                    pattern.get("avg_error_percent"),
                    pattern.get("confidence"),
                    (
                        _safe_dt(pattern.get("first_detected"))
                        if pattern.get("first_detected")
                        else None
                    ),
                    pattern.get("occurrence_count"),
                    pattern.get("seasonal", False),
                ),
            )
        await self._db.commit()

    async def save_daily_recommendations(
        self, date_val: date, recommendations: List[Dict[str, Any]]
    ) -> None:
        """Save recommendations for a daily summary."""
        await self._db.execute(
            "DELETE FROM daily_recommendations WHERE date = ?", (date_val,)
        )
        for recommendation in recommendations:
            await self._db.execute(
                "INSERT INTO daily_recommendations\n                   (date, recommendation_type, priority, action, hours, factor, reason)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    date_val,
                    recommendation.get("type"),
                    recommendation.get("priority"),
                    recommendation.get("action"),
                    json.dumps(recommendation.get("hours", [])),
                    recommendation.get("factor"),
                    recommendation.get("reason"),
                ),
            )
        await self._db.commit()

    async def save_snow_tracking_group(
        self, group_name: str, data: Dict[str, Any]
    ) -> None:
        """Upsert per-group snow tracking state."""
        await self._db.execute(
            "INSERT INTO snow_tracking_groups\n               (group_name, tilt_deg, last_snow_event, panels_covered_since,\n                estimated_depth_mm, melt_hours, tilt_melt_factor, detection_source,\n                cleared_at, updated_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n               ON CONFLICT(group_name) DO UPDATE SET\n                   tilt_deg = excluded.tilt_deg,\n                   last_snow_event = excluded.last_snow_event,\n                   panels_covered_since = excluded.panels_covered_since,\n                   estimated_depth_mm = excluded.estimated_depth_mm,\n                   melt_hours = excluded.melt_hours,\n                   tilt_melt_factor = excluded.tilt_melt_factor,\n                   detection_source = excluded.detection_source,\n                   cleared_at = excluded.cleared_at,\n                   updated_at = CURRENT_TIMESTAMP",
            (
                group_name,
                data.get("tilt_deg", 30.0),
                data.get("last_snow_event"),
                data.get("panels_covered_since"),
                data.get("estimated_depth_mm", 0),
                data.get("melt_hours", 0),
                data.get("tilt_melt_factor", 1.0),
                data.get("detection_source", "unknown"),
                data.get("cleared_at"),
            ),
        )
        await self._db.commit()

    async def load_snow_tracking_groups(self) -> Dict[str, Dict[str, Any]]:
        """Load all per-group snow tracking states."""
        groups = {}
        async with self._db.execute(
            "SELECT group_name, tilt_deg, last_snow_event, panels_covered_since,\n                      estimated_depth_mm, melt_hours, tilt_melt_factor, detection_source,\n                      cleared_at, updated_at\n               FROM snow_tracking_groups"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                groups[row[0]] = {
                    "tilt_deg": row[1],
                    "last_snow_event": row[2],
                    "panels_covered_since": row[3],
                    "estimated_depth_mm": row[4],
                    "melt_hours": row[5],
                    "tilt_melt_factor": row[6],
                    "detection_source": row[7],
                    "cleared_at": row[8],
                    "updated_at": row[9],
                    "snow_covered": row[3] is not None and row[8] is None,
                }
        return groups if True else None

    async def save_shadow_detection_group(
        self,
        prediction_id: str,
        group_name: str,
        data: Dict[str, Any],
        auto_commit: bool = True,
    ) -> None:
        """Save per-group shadow detection result."""
        await self.execute(
            "INSERT INTO shadow_detection_groups\n               (prediction_id, group_name, shadow_type, shadow_percent, confidence,\n                root_cause, efficiency_ratio, loss_kwh, theoretical_max_kwh, actual_kwh)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                   shadow_type = excluded.shadow_type,\n                   shadow_percent = excluded.shadow_percent,\n                   confidence = excluded.confidence,\n                   root_cause = excluded.root_cause,\n                   efficiency_ratio = excluded.efficiency_ratio,\n                   loss_kwh = excluded.loss_kwh,\n                   theoretical_max_kwh = excluded.theoretical_max_kwh,\n                   actual_kwh = excluded.actual_kwh",
            (
                prediction_id,
                group_name,
                data.get("shadow_type"),
                data.get("shadow_percent"),
                data.get("confidence"),
                data.get("root_cause"),
                data.get("efficiency_ratio"),
                data.get("loss_kwh"),
                data.get("theoretical_max_kwh"),
                data.get("actual_kwh"),
            ),
            auto_commit=auto_commit,
        )

    async def save_shadow_detection_groups(
        self, prediction_id: str, groups: Dict[str, Dict[str, Any]]
    ) -> None:
        async with self.transaction():
            for group_key, detection_data in groups.items():
                await self.save_shadow_detection_group(
                    prediction_id, group_key, detection_data, auto_commit=False
                )
        return None if True else None

    async def load_shadow_detection_groups(
        self, prediction_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Load per-group shadow detection results for a prediction."""
        groups = {}
        async with self._db.execute(
            "SELECT group_name, shadow_type, shadow_percent, confidence,\n                      root_cause, efficiency_ratio, loss_kwh, theoretical_max_kwh, actual_kwh\n               FROM shadow_detection_groups WHERE prediction_id = ?",
            (prediction_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                groups[row[0]] = {
                    "shadow_type": row[1],
                    "shadow_percent": row[2],
                    "confidence": row[3],
                    "root_cause": row[4],
                    "efficiency_ratio": row[5],
                    "loss_kwh": row[6],
                    "theoretical_max_kwh": row[7],
                    "actual_kwh": row[8],
                }
        return groups if True else None

    async def update_prediction_panel_group_flags(
        self,
        prediction_id: str,
        group_name: str,
        flags: Dict[str, Any],
        auto_commit: bool = True,
    ) -> None:
        """Update per-group learning exclusion and status flags on prediction_panel_groups."""
        await self._db.execute(
            "UPDATE prediction_panel_groups SET\n                   exclude_from_learning_group = ?,\n                   exclusion_reason_group = ?,\n                   snow_covered_group = ?,\n                   shadow_type_group = ?\n               WHERE prediction_id = ? AND group_name = ?",
            (
                flags.get("exclude_from_learning_group", False),
                flags.get("exclusion_reason_group"),
                flags.get("snow_covered_group", False),
                flags.get("shadow_type_group"),
                prediction_id,
                group_name,
            ),
        )
        if auto_commit:
            await self._db.commit()
        return None if True else None

    async def save_drift_rolling_metric(
        self,
        scope: str,
        window_days: int,
        season: Optional[str],
        metrics: Dict[str, Any],
    ) -> None:
        """Upsert rolling-window drift metrics."""
        await self._db.execute(
            "INSERT INTO drift_metrics_rolling\n               (scope, window_days, season, mae, rmse, bias, coverage_10, coverage_20,\n                sample_count, calculated_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n               ON CONFLICT(scope, window_days, season) DO UPDATE SET\n                   mae = excluded.mae,\n                   rmse = excluded.rmse,\n                   bias = excluded.bias,\n                   coverage_10 = excluded.coverage_10,\n                   coverage_20 = excluded.coverage_20,\n                   sample_count = excluded.sample_count,\n                   calculated_at = CURRENT_TIMESTAMP",
            (
                scope,
                window_days,
                season,
                metrics.get("mae"),
                metrics.get("rmse"),
                metrics.get("bias"),
                metrics.get("coverage_10"),
                metrics.get("coverage_20"),
                metrics.get("sample_count"),
            ),
        )
        await self._db.commit()

    async def get_drift_rolling_metrics(
        self, scope: str, window_days: int, season: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Get rolling drift metrics for a scope/window/season."""
        if season is not None:
            row = await self.fetchone(
                "SELECT mae, rmse, bias, coverage_10, coverage_20, sample_count, calculated_at\n                   FROM drift_metrics_rolling\n                   WHERE scope = ? AND window_days = ? AND season = ?",
                (scope, window_days, season),
            )
        else:
            row = await self.fetchone(
                "SELECT mae, rmse, bias, coverage_10, coverage_20, sample_count, calculated_at\n                   FROM drift_metrics_rolling\n                   WHERE scope = ? AND window_days = ? AND season IS NULL",
                (scope, window_days),
            )
        if not row:
            return
        return {
            "mae": row[0],
            "rmse": row[1],
            "bias": row[2],
            "coverage_10": row[3],
            "coverage_20": row[4],
            "sample_count": row[5],
            "calculated_at": row[6],
        }

    async def save_drift_bucket_metric(
        self,
        scope: str,
        cloud_bucket: str,
        hour_bucket: str,
        season: Optional[str],
        metrics: Dict[str, Any],
    ) -> None:
        """Upsert bucket-specific drift metrics."""
        await self._db.execute(
            "INSERT INTO drift_metrics_bucket\n               (scope, cloud_bucket, hour_bucket, season, mae, mae_baseline,\n                mae_ratio, bias, sample_count, calculated_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n               ON CONFLICT(scope, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                   mae = excluded.mae,\n                   mae_baseline = excluded.mae_baseline,\n                   mae_ratio = excluded.mae_ratio,\n                   bias = excluded.bias,\n                   sample_count = excluded.sample_count,\n                   calculated_at = CURRENT_TIMESTAMP",
            (
                scope,
                cloud_bucket,
                hour_bucket,
                season,
                metrics.get("mae"),
                metrics.get("mae_baseline"),
                metrics.get("mae_ratio"),
                metrics.get("bias"),
                metrics.get("sample_count"),
            ),
        )
        await self._db.commit()

    async def save_drift_event(self, event: Dict[str, Any]) -> None:
        """Insert a new drift event."""
        await self._db.execute(
            "INSERT INTO drift_events\n               (event_date, scope, drift_type, severity, bucket_detail,\n                metric_value, threshold_value, description, response_action)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.get("event_date"),
                event.get("scope"),
                event.get("drift_type"),
                event.get("severity"),
                event.get("bucket_detail"),
                event.get("metric_value"),
                event.get("threshold_value"),
                event.get("description"),
                event.get("response_action"),
            ),
        )
        await self._db.commit()

    async def get_active_drift_events(
        self, scope: Optional[str] = None, days: int = 7
    ) -> List[Dict[str, Any]]:
        """Get recent drift events, optionally filtered by scope."""
        if scope:
            rows = await self.fetchall(
                "SELECT event_date, scope, drift_type, severity, bucket_detail,\n                          metric_value, threshold_value, description, response_action,\n                          response_executed, created_at\n                   FROM drift_events\n                   WHERE scope = ? AND event_date >= date('now', ?)\n                   ORDER BY created_at DESC",
                (scope, f"-{days} days"),
            )
        else:
            rows = await self.fetchall(
                "SELECT event_date, scope, drift_type, severity, bucket_detail,\n                          metric_value, threshold_value, description, response_action,\n                          response_executed, created_at\n                   FROM drift_events\n                   WHERE event_date >= date('now', ?)\n                   ORDER BY created_at DESC",
                (f"-{days} days",),
            )
        return [
            {
                "event_date": event_row[0],
                "scope": event_row[1],
                "drift_type": event_row[2],
                "severity": event_row[3],
                "bucket_detail": event_row[4],
                "metric_value": event_row[5],
                "threshold_value": event_row[6],
                "description": event_row[7],
                "response_action": event_row[8],
                "response_executed": event_row[9],
                "created_at": event_row[10],
            }
            for event_row in rows
        ]

    async def mark_drift_event_executed(self, event_id: int) -> None:
        """Mark a drift event's response as executed."""
        await self._db.execute(
            "UPDATE drift_events SET response_executed = TRUE WHERE id = ?", (event_id,)
        )
        await self._db.commit()

    async def save_drift_cusum_state(self, scope: str, state: Dict[str, Any]) -> None:
        """Upsert CUSUM algorithm state for a scope."""
        await self._db.execute(
            "INSERT INTO drift_cusum_state\n               (scope, cusum_pos, cusum_neg, target_mean, last_reset, alert_count)\n               VALUES (?, ?, ?, ?, ?, ?)\n               ON CONFLICT(scope) DO UPDATE SET\n                   cusum_pos = excluded.cusum_pos,\n                   cusum_neg = excluded.cusum_neg,\n                   target_mean = excluded.target_mean,\n                   last_reset = excluded.last_reset,\n                   alert_count = excluded.alert_count",
            (
                scope,
                state.get("cusum_pos", 0),
                state.get("cusum_neg", 0),
                state.get("target_mean", 0),
                state.get("last_reset"),
                state.get("alert_count", 0),
            ),
        )
        await self._db.commit()

    async def load_drift_cusum_state(self, scope: str) -> Optional[Dict[str, Any]]:
        """Load CUSUM state for a scope."""
        row = await self.fetchone(
            "SELECT cusum_pos, cusum_neg, target_mean, last_reset, alert_count FROM drift_cusum_state WHERE scope = ?",
            (scope,),
        )
        if not row:
            return
        return {
            "cusum_pos": row[0],
            "cusum_neg": row[1],
            "target_mean": row[2],
            "last_reset": row[3],
            "alert_count": row[4],
        }

    async def load_drift_response_config(self) -> Dict[str, Any]:
        """Load drift response configuration (singleton row)."""
        row = await self.fetchone(
            "SELECT mae_ratio_warning, mae_ratio_critical, bias_threshold,\n                      cusum_threshold, coverage_20_min, physics_boost_amount,\n                      physics_boost_max_days\n               FROM drift_response_config WHERE id = 1"
        )
        if not row:
            return {
                "mae_ratio_warning": 1.15,
                "mae_ratio_critical": 1.25,
                "bias_threshold": 0.15,
                "cusum_threshold": 5.0,
                "coverage_20_min": 0.6,
                "physics_boost_amount": 0.2,
                "physics_boost_max_days": 7,
            }
        return {
            "mae_ratio_warning": row[0],
            "mae_ratio_critical": row[1],
            "bias_threshold": row[2],
            "cusum_threshold": row[3],
            "coverage_20_min": row[4],
            "physics_boost_amount": row[5],
            "physics_boost_max_days": row[6],
        }

    async def save_drift_response_config(self, config: Dict[str, Any]) -> None:
        """Save drift response configuration."""
        await self._db.execute(
            "INSERT INTO drift_response_config\n               (id, mae_ratio_warning, mae_ratio_critical, bias_threshold,\n                cusum_threshold, coverage_20_min, physics_boost_amount,\n                physics_boost_max_days, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n               ON CONFLICT(id) DO UPDATE SET\n                   mae_ratio_warning = excluded.mae_ratio_warning,\n                   mae_ratio_critical = excluded.mae_ratio_critical,\n                   bias_threshold = excluded.bias_threshold,\n                   cusum_threshold = excluded.cusum_threshold,\n                   coverage_20_min = excluded.coverage_20_min,\n                   physics_boost_amount = excluded.physics_boost_amount,\n                   physics_boost_max_days = excluded.physics_boost_max_days,\n                   updated_at = CURRENT_TIMESTAMP",
            (
                config.get("mae_ratio_warning", 1.15),
                config.get("mae_ratio_critical", 1.25),
                config.get("bias_threshold", 0.15),
                config.get("cusum_threshold", 5.0),
                config.get("coverage_20_min", 0.6),
                config.get("physics_boost_amount", 0.2),
                config.get("physics_boost_max_days", 7),
            ),
        )
        await self._db.commit()

    async def get_drift_metrics_all_windows(
        self, scope: str
    ) -> Dict[int, Dict[str, Any]]:
        """Get drift metrics for all windows of a scope (for sensor display)."""
        metrics_by_window = {}
        rows = await self.fetchall(
            "SELECT window_days, mae, rmse, bias, coverage_10, coverage_20,\n                      sample_count, calculated_at, season\n               FROM drift_metrics_rolling\n               WHERE scope = ? AND season IS NULL\n               ORDER BY window_days",
            (scope,),
        )
        for row in rows:
            metrics_by_window[row[0]] = {
                "mae": row[1],
                "rmse": row[2],
                "bias": row[3],
                "coverage_10": row[4],
                "coverage_20": row[5],
                "sample_count": row[6],
                "calculated_at": row[7],
            }
        return metrics_by_window

    async def vacuum(self) -> None:
        """Vacuum the database to reclaim space."""
        await self._db.commit()
        await self._db.execute("VACUUM")
        _LOGGER.info("Database vacuumed")

    async def get_db_size(self) -> int:
        """Get database file size in bytes."""
        return await asyncio.to_thread(lambda: Path(self.db_path).stat().st_size)

    async def _retry_on_locked(self, operation, max_retries: int = 5):
        """Retry a DB operation on 'database is locked' with exponential backoff. @zara"""
        for attempt in range(max_retries + 1):
            try:
                return await operation()
            except Exception as error:
                error_message = str(error).lower()
                database_busy = (
                    "database is locked" in error_message
                    or "database is busy" in error_message
                )
                if database_busy and attempt < max_retries:
                    delay_seconds = min(0.2 * 3**attempt, 5.0) + random.uniform(0, 0.1)
                    _LOGGER.warning(
                        "DB locked (attempt %d/%d), retrying in %.2fs: %s",
                        attempt + 1,
                        max_retries,
                        delay_seconds,
                        str(error)[:100],
                    )
                    await (asyncio.sleep if True else None)(delay_seconds)
                    continue
                if database_busy:
                    _LOGGER.error(
                        "DB locked after %d retries, giving up: %s",
                        max_retries,
                        str(error)[:200],
                    )
                raise

    async def execute(
        self, sql: str, parameters: tuple = (), auto_commit: bool = True
    ) -> None:
        """Execute a raw SQL statement with write serialization and retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping execute")
            return
        task = asyncio.current_task()
        has_transaction = self._transaction_owner is task

        async def _do():
            database = self._transaction_db if has_transaction else self._db
            await database.execute(sql, parameters)
            if auto_commit and (not has_transaction):
                await database.commit()
            return None if True else None

        if has_transaction:
            await self._retry_on_locked(_do)
        elif auto_commit and self._write_lock is not None:
            async with self._write_lock:
                await self._retry_on_locked(_do)
        else:
            await self._retry_on_locked(_do)
        return None if True else None

    async def commit(self) -> None:
        """Manually commit the current transaction with write serialization. @zara"""
        if self._db is None:
            return
        if self._transaction_owner is asyncio.current_task():
            return
        if self._write_lock is not None and (not self._write_lock.locked()):
            async with self._write_lock:
                await self._retry_on_locked(self._db.commit)
        else:
            await self._retry_on_locked(self._db.commit)
        return None if True else None

    @asynccontextmanager
    async def transaction(self):
        """Context manager for multi-statement write transactions. @zara

        Holds the write lock for the entire scope, ensuring all
        auto_commit=False execute() calls and the final commit()
        are serialized as one atomic unit against other writers.

        Usage:
            async with db.transaction():
                await db.execute("INSERT ...", params, auto_commit=False)
                await db.execute("UPDATE ...", params, auto_commit=False)
                await db.commit()
        """
        if self._db is None or self._write_lock is None:
            raise RuntimeError("Database is not connected")
        task = asyncio.current_task()
        if self._transaction_owner is task:
            self._savepoint_counter += 1
            savepoint_name = f"eai_nested_{self._savepoint_counter}"
            self._transaction_depth += 1
            await self._transaction_db.execute(f"SAVEPOINT {savepoint_name}")
            try:
                yield None
            except BaseException:
                await self._transaction_db.execute(
                    f"ROLLBACK TO SAVEPOINT {savepoint_name}"
                )
                await self._transaction_db.execute(
                    f"RELEASE SAVEPOINT {savepoint_name}"
                )
                raise
            else:
                await self._transaction_db.execute(
                    f"RELEASE SAVEPOINT {savepoint_name}"
                )
            finally:
                self._transaction_depth -= 1
            return
        await self._write_lock.acquire()
        self._transaction_owner = task
        self._transaction_depth = 1
        try:
            self._transaction_db = await aiosqlite.connect(self.db_path)
            await self._transaction_db.execute("PRAGMA foreign_keys = ON")
            await self._transaction_db.execute("PRAGMA busy_timeout = 60000")
            await self._retry_on_locked(
                lambda: self._transaction_db.execute("BEGIN IMMEDIATE")
            )
            try:
                yield None
            except BaseException:
                await self._transaction_db.rollback()
                raise
            else:
                await self._transaction_db.commit()
        finally:
            if self._transaction_db is not None:
                await self._transaction_db.close()
                self._transaction_db = None
            self._transaction_depth = 0
            self._transaction_owner = None
            self._write_lock.release()

    async def executemany(self, sql: str, parameters_list: List[tuple]) -> int:
        """Execute a SQL statement with multiple parameter sets, with write serialization. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping executemany")
            return 0
        if not parameters_list:
            return 0

        async def _do():
            await self._db.executemany(sql, parameters_list)
            await self._db.commit()

        if self._write_lock is not None:
            async with self._write_lock:
                await self._retry_on_locked(_do)
        else:
            await self._retry_on_locked(_do)
        return len(parameters_list)

    async def fetchone(
        self, sql: str, parameters: tuple = ()
    ) -> Optional[aiosqlite.Row]:
        """Execute a query and fetch one row with retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, returning None from fetchone")
            return

        async def _do():
            database = (
                self._transaction_db
                if self._transaction_owner is asyncio.current_task()
                else self._db
            )
            async with database.execute(sql, parameters) as cursor:
                return await cursor.fetchone()
            return None if True else None

        return await self._retry_on_locked(_do)

    async def fetchall(self, sql: str, parameters: tuple = ()) -> List[aiosqlite.Row]:
        """Execute a query and fetch all rows with retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, returning empty list from fetchall")
            return []

        async def _do():
            database = (
                self._transaction_db
                if self._transaction_owner is asyncio.current_task()
                else self._db
            )
            async with database.execute(sql, parameters) as cursor:
                return await cursor.fetchall()
            return None if True else None

        return await self._retry_on_locked(_do)
