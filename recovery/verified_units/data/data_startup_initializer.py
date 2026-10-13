# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Startup Initializer for Solar Forecast ML V16.2.0.
Synchronous initializer - guarantees database and directories exist before async startup.
Initializes database schema and default data.

@zara
"""
import logging
import re
from datetime import datetime, timedelta, time, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
_DRIFT_NULL_SEASON_INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_drift_metrics_rolling_null_season\nON drift_metrics_rolling(scope, window_days)\nWHERE season IS NULL;\n",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_drift_metrics_bucket_null_season\nON drift_metrics_bucket(scope, cloud_bucket, hour_bucket)\nWHERE season IS NULL;\n",
)
_REQUIRED_FORECAST_PROVENANCE_TABLES = frozenset(
    {
        "ensemble_shadow_batches",
        "ensemble_shadow_evaluations",
        "weather_radiation_snapshots",
        "ensemble_shadow_batch_provenance",
        "weather_forecast_usage_snapshots",
        "ensemble_shadow_evaluation_results",
        "weather_radiation_snapshot_sources",
        "ensemble_shadow_evaluation_versions",
        "weather_forecast_correction_snapshots",
    }
)
_REQUIRED_FORECAST_PROVENANCE_COLUMNS = {
    "ensemble_shadow_batches": {"status", "run_date", "created_at", "morning_batch_id"},
    "ensemble_shadow_batch_provenance": {
        "created_at",
        "trigger_kind",
        "forecast_cycle",
        "contract_version",
        "morning_batch_id",
        "promotion_eligible",
    },
    "ensemble_shadow_evaluations": {
        "id",
        "target_date",
        "target_hour",
        "target_datetime",
        "morning_batch_id",
        "shadow_evaluable",
        "shadow_rule_version",
        "shadow_evaluation_id",
        "source_prediction_id",
        "shadow_gate_triggered",
        "production_prediction_kwh",
    },
    "ensemble_shadow_evaluation_results": {
        "id",
        "actual_kwh",
        "evaluated_at",
        "evaluation_status",
        "final_absolute_error",
        "shadow_evaluation_id",
        "clean_evaluation_status",
    },
    "ensemble_shadow_evaluation_versions": {
        "evaluated_at",
        "evaluation_status",
        "actual_fingerprint",
        "evaluation_version",
        "shadow_evaluation_id",
        "evaluation_version_id",
        "clean_evaluation_status",
    },
    "weather_radiation_snapshots": {
        "final_dhi",
        "final_dni",
        "final_ghi",
        "lead_hours",
        "kalman_bias",
        "snapshot_id",
        "target_date",
        "target_hour",
        "retrieved_at",
        "raw_blend_ghi",
        "snapshot_kind",
        "kalman_applied",
        "residual_scale",
        "provider_run_at",
        "target_datetime",
        "contract_version",
    },
    "weather_radiation_snapshot_sources": {
        "dhi",
        "dni",
        "ghi",
        "available",
        "snapshot_id",
        "source_name",
        "weight_used",
        "source_identity",
    },
    "weather_forecast_correction_snapshots": {
        "correction_snapshot_id",
        "contract_version",
        "radiation_snapshot_id",
        "corrected_at",
        "target_datetime",
        "target_date",
        "target_hour",
        "observation_mode",
        "local_observation_available",
        "raw_blend_ghi",
        "ensemble_ghi",
        "ensemble_dni",
        "ensemble_dhi",
        "learned_solar_factor",
        "static_solar_factor_applied",
        "mlp_factor",
        "mlp_confidence",
        "combined_solar_factor_applied",
        "coherence_cap_applied",
        "coherence_cap_scale",
        "kalman_applied",
        "kalman_bias",
        "ghi_coherence_cap_eligible",
        "final_used_ghi",
        "final_used_dni",
        "final_used_dhi",
        "temperature",
        "humidity",
        "clouds",
        "rain",
        "wind",
        "pressure",
        "sun_elevation",
    },
    "weather_forecast_usage_snapshots": {
        "locked_at",
        "target_date",
        "target_hour",
        "prediction_id",
        "target_datetime",
        "contract_version",
        "morning_batch_id",
        "usage_snapshot_id",
        "correction_snapshot_id",
    },
}
_REQUIRED_FORECAST_PROVENANCE_PRIMARY_KEYS = {
    "ensemble_shadow_batches": ("morning_batch_id",),
    "ensemble_shadow_batch_provenance": ("morning_batch_id",),
    "ensemble_shadow_evaluations": ("id",),
    "ensemble_shadow_evaluation_results": ("id",),
    "ensemble_shadow_evaluation_versions": ("evaluation_version_id",),
    "weather_radiation_snapshots": ("snapshot_id",),
    "weather_radiation_snapshot_sources": ("snapshot_id", "source_name"),
    "weather_forecast_correction_snapshots": ("correction_snapshot_id",),
    "weather_forecast_usage_snapshots": ("usage_snapshot_id",),
}
_REQUIRED_FORECAST_PROVENANCE_FOREIGN_KEYS = frozenset(
    {
        (
            "ensemble_shadow_batch_provenance",
            "morning_batch_id",
            "ensemble_shadow_batches",
            "morning_batch_id",
            "NO ACTION",
        ),
        (
            "weather_forecast_usage_snapshots",
            "correction_snapshot_id",
            "weather_forecast_correction_snapshots",
            "correction_snapshot_id",
            "RESTRICT",
        ),
        (
            "ensemble_shadow_evaluation_results",
            "shadow_evaluation_id",
            "ensemble_shadow_evaluations",
            "shadow_evaluation_id",
            "RESTRICT",
        ),
        (
            "weather_radiation_snapshot_sources",
            "snapshot_id",
            "weather_radiation_snapshots",
            "snapshot_id",
            "RESTRICT",
        ),
        (
            "ensemble_shadow_evaluation_versions",
            "shadow_evaluation_id",
            "ensemble_shadow_evaluations",
            "shadow_evaluation_id",
            "RESTRICT",
        ),
        (
            "weather_forecast_correction_snapshots",
            "radiation_snapshot_id",
            "weather_radiation_snapshots",
            "snapshot_id",
            "RESTRICT",
        ),
    }
)
_REQUIRED_FORECAST_PROVENANCE_TRIGGERS = frozenset(
    {
        "trg_weather_forecast_usage_no_delete",
        "trg_weather_forecast_usage_no_update",
        "trg_ensemble_shadow_results_no_delete",
        "trg_ensemble_shadow_results_no_update",
        "trg_ensemble_shadow_versions_no_delete",
        "trg_ensemble_shadow_versions_no_update",
        "trg_ensemble_shadow_evaluations_no_delete",
        "trg_ensemble_shadow_evaluations_no_update",
        "trg_weather_forecast_correction_no_delete",
        "trg_weather_forecast_correction_no_update",
        "trg_weather_radiation_snapshots_no_delete",
        "trg_weather_radiation_snapshots_no_update",
        "trg_weather_radiation_snapshot_sources_no_delete",
        "trg_weather_radiation_snapshot_sources_no_update",
        "ensemble_shadow_batch_provenance_append_only_delete",
        "ensemble_shadow_batch_provenance_append_only_update",
        "trg_weather_radiation_snapshots_no_duplicate_insert",
        "ensemble_shadow_batch_provenance_append_only_insert_guard",
        "trg_weather_radiation_snapshot_sources_no_duplicate_insert",
    }
)
_REQUIRED_FORECAST_PROVENANCE_SQL_FRAGMENTS = {
    "ensemble_shadow_evaluations": (
        "check(target_hour>=0andtarget_hour<=23)",
        "unique(morning_batch_id,source_prediction_id)",
    ),
    "weather_radiation_snapshots": (
        "check(snapshot_kindin('provider_fetch','ensemble_compose'))",
        "check(target_hour>=0andtarget_hour<=23)",
    ),
    "weather_radiation_snapshot_sources": ("primarykey(snapshot_id,source_name)",),
    "weather_forecast_correction_snapshots": (
        "check(target_hour>=0andtarget_hour<=23)",
        "check(observation_modein('measured_local','provider_only'))",
    ),
    "weather_forecast_usage_snapshots": (
        "check(target_hour>=0andtarget_hour<=23)",
        "unique(morning_batch_id,prediction_id)",
    ),
}
_CREATE_TABLE_RE = re.compile("CREATE TABLE IF NOT EXISTS (\\w+)\\s*\\(", re.IGNORECASE)
_TABLE_CONSTRAINT_RE = re.compile(
    "^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\\b", re.IGNORECASE
)
_COLUMN_NAME_RE = re.compile('^"?([A-Za-z_][A-Za-z0-9_]*)"?\\s+(.*)$', re.DOTALL)
_REFERENCES_CLAUSE_RE = re.compile(
    "\\bREFERENCES\\s+\\w+\\s*\\([^)]*\\)(?:\\s+ON\\s+DELETE\\s+(?:NO\\s+ACTION|RESTRICT|CASCADE|SET\\s+NULL|SET\\s+DEFAULT))?(?:\\s+ON\\s+UPDATE\\s+(?:NO\\s+ACTION|RESTRICT|CASCADE|SET\\s+NULL|SET\\s+DEFAULT))?",
    re.IGNORECASE,
)


def _strip_sql_line_comments(sql: str) -> str:
    """Remove `--` comments that are not inside quoted strings."""
    uncommented_lines = []
    for line in sql.splitlines():
        quote = None
        line_characters = []
        position = 0
        while position < len(line):
            character = line[position]
            if quote is not None:
                line_characters.append(character)
                if character == quote:
                    quote = None
                elif character == "'" and quote == "'":
                    pass
                position += 1
                continue
            if character in {'"', "'"}:
                quote = character
                line_characters.append(character)
                position += 1
                continue
            if (
                character == "-"
                and position + 1 < len(line)
                and (line[position + 1] == "-")
            ):
                break
            line_characters.append(character)
            position += 1
        uncommented_lines.append("".join(line_characters))
    return "\n".join(uncommented_lines)


def _split_top_level_clauses(body: str) -> list[str]:
    clauses = []
    clause_characters = []
    parenthesis_depth = 0
    for character in body:
        if character == "(":
            parenthesis_depth += 1
            clause_characters.append(character)
        elif character == ")":
            parenthesis_depth -= 1
            clause_characters.append(character)
        elif character == "," and parenthesis_depth == 0:
            clause = "".join(clause_characters).strip()
            if clause:
                clauses.append(clause)
            clause_characters = []
        else:
            clause_characters.append(character)
    last_clause = "".join(clause_characters).strip()
    if last_clause:
        clauses.append(last_clause)
    return clauses


def _parse_schema_table_columns(schema_sql: str) -> dict[str, dict[str, str]]:
    """Map CREATE TABLE names to `{column: remainder-of-definition}`."""
    columns_by_table = {}
    uncommented_sql = _strip_sql_line_comments(schema_sql)
    position = 0
    while True:
        table_match = _CREATE_TABLE_RE.search(uncommented_sql, position)
        if table_match is None:
            break
        table_name = table_match.group(1)
        body_start = table_match.end()
        parenthesis_depth = 1
        body_end = body_start
        while body_end < len(uncommented_sql) and parenthesis_depth:
            if uncommented_sql[body_end] == "(":
                parenthesis_depth += 1
            elif uncommented_sql[body_end] == ")":
                parenthesis_depth -= 1
            body_end += 1
        table_body = uncommented_sql[body_start : body_end - 1]
        columns = {}
        for clause in _split_top_level_clauses(table_body):
            if _TABLE_CONSTRAINT_RE.match(clause):
                continue
            column_match = _COLUMN_NAME_RE.match(clause)
            if column_match is not None:
                columns[column_match.group(1)] = " ".join(column_match.group(2).split())
        columns_by_table[table_name] = columns
        position = body_end
    return columns_by_table if True else None


def _additive_column_definition(definition: str) -> str | None:
    """Return an ALTER TABLE ADD COLUMN definition, or None if unsafe."""
    additive_definition = " ".join(definition.split())
    uppercase_definition = additive_definition.upper()
    if (
        "PRIMARY KEY" in uppercase_definition
        or "AUTOINCREMENT" in uppercase_definition
        or re.search("\\bUNIQUE\\b", additive_definition, flags=re.IGNORECASE)
    ):
        return None
    additive_definition = _REFERENCES_CLAUSE_RE.sub("", additive_definition)
    additive_definition = re.sub(
        "\\bDEFAULT\\s+CURRENT_(?:TIMESTAMP|TIME|DATE)\\b",
        "",
        additive_definition,
        flags=re.IGNORECASE,
    )
    additive_definition = re.sub(
        "\\bDEFAULT\\s*\\([^)]*\\)", "", additive_definition, flags=re.IGNORECASE
    )
    additive_definition = " ".join(additive_definition.split())
    if re.search("\\bNOT NULL\\b", additive_definition, re.IGNORECASE) and (
        not re.search("\\bDEFAULT\\b", additive_definition, re.IGNORECASE)
    ):
        additive_definition = re.sub(
            "\\bNOT NULL\\b", "", additive_definition, flags=re.IGNORECASE
        )
        additive_definition = " ".join(additive_definition.split())
    return additive_definition or None


def _load_schema_sql() -> str:
    """Return the canonical schema from the protected data module."""
    from .schema_sql import SCHEMA_SQL as schema_sql

    return schema_sql


class StartupInitializer:
    """Synchronous initializer - guarantees database exists before async startup. @zara

    Creates:
    1. Directory structure (backups/auto/)
    2. Database file with schema
    3. Default astronomy cache data
    4. Default weather forecast data

    This runs BEFORE any async operations to prevent race conditions.
    """

    def __init__(self, data_dir: Path, config: dict[str, Any]):
        "Initialize startup initializer. @zara\n\nArgs:\n    data_dir: Base data directory path\n    config: Configuration dictionary with location settings\n"
        self.data_dir = Path(data_dir)
        self.config = config
        self.latitude = config.get("latitude", 52.52)
        self.longitude = config.get("longitude", 13.4)
        self.solar_capacity_kwp = config.get("solar_capacity", 2.0)
        self.timezone_str = config.get("timezone", "Europe/Berlin")
        try:
            self.timezone = ZoneInfo(self.timezone_str)
        except Exception:
            _LOGGER.warning(
                "Invalid timezone '%s', using Europe/Berlin", self.timezone_str
            )
            self.timezone = ZoneInfo("Europe/Berlin")
            self.timezone_str = "Europe/Berlin"
        self.db_path = self.data_dir / "solar_forecast.db"

    def initialize_all(self) -> bool:
        "Initialize critical components synchronously. @zara\n\nMUST run BEFORE any async operations to prevent race conditions.\n\nReturns:\n    True if all critical components are ready\n"
        _LOGGER.debug("============================================================")
        _LOGGER.debug("STARTUP INITIALIZER - Creating critical pre-async components")
        _LOGGER.debug("============================================================")
        success = True
        if not self._ensure_directories():
            _LOGGER.error("Failed to create directory structure")
            success = False
        else:
            _LOGGER.debug("Directory structure ready")
        if not self._ensure_database():
            _LOGGER.error("Failed to create database")
            success = False
        else:
            _LOGGER.debug("Database ready")
        _LOGGER.debug("============================================================")
        if success:
            _LOGGER.debug("STARTUP INITIALIZER complete - database ready")
        else:
            _LOGGER.error("STARTUP INITIALIZER failed")
        _LOGGER.debug("============================================================")
        return success

    def _ensure_directories(self) -> bool:
        """Create all required directories. @zara"""
        directories = [
            self.data_dir,
            self.data_dir / "backups",
            self.data_dir / "backups" / "auto",
            self.data_dir / "backups" / "manual",
        ]
        try:
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
            _LOGGER.debug("Created/verified %d directories", len(directories))
            return True
        except Exception as error:
            _LOGGER.error("Failed to create directories: %s", error)
            return False

    def _ensure_database(self) -> bool:
        """Ensure database file exists. @zara"""
        try:
            import sqlite3 as sqlite_module

            connection = sqlite_module.connect(
                str(self.db_path), isolation_level="IMMEDIATE"
            )
            try:
                connection.execute("PRAGMA busy_timeout = 30000")
                schema_sql = _load_schema_sql()
                schema_columns = _parse_schema_table_columns(schema_sql)
                self._heal_schema_additive_columns(connection, schema_columns)
                existing_drift_table_count = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master\n                       WHERE type = 'table'\n                         AND name IN (\n                             'drift_metrics_rolling',\n                             'drift_metrics_bucket'\n                         )"
                ).fetchone()[0]
                if existing_drift_table_count:
                    for null_season_index_sql in _DRIFT_NULL_SEASON_INDEX_STATEMENTS:
                        schema_sql = schema_sql.replace(null_season_index_sql, "")
                connection.executescript(schema_sql)
                self._heal_schema_additive_columns(connection, schema_columns)
                self._verify_schema_tables_and_columns(connection, schema_columns)
                self._verify_forecast_provenance_schema(connection)
                _LOGGER.debug("Database schema initialized")
                self._init_default_data(connection)
            finally:
                connection.close()
            return True
        except Exception as error:
            _LOGGER.error("Failed to create database: %s", error)
            return False

    @staticmethod
    def _heal_schema_additive_columns(
        conn, schema_columns: dict[str, dict[str, str]]
    ) -> None:
        """Add missing CREATE TABLE columns onto existing tables."""
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table_name, columns in schema_columns.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')
            }
            for column_name, column_definition in columns.items():
                if column_name in existing_columns:
                    continue
                safe_definition = _additive_column_definition(column_definition)
                if safe_definition is None:
                    _LOGGER.warning(
                        "Startup schema skipped unsafe additive column %s.%s",
                        table_name,
                        column_name,
                    )
                    continue
                conn.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {safe_definition}'
                )
                _LOGGER.info(
                    "Startup schema migration added %s.%s", table_name, column_name
                )

    @staticmethod
    def _verify_schema_tables_and_columns(
        conn, schema_columns: dict[str, dict[str, str]]
    ) -> None:
        """Fail startup when any schema table or column is missing."""
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(set(schema_columns) - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "Required schema tables missing: " + ", ".join(missing_tables)
            )
        missing_column_reports = []
        for table_name, columns in schema_columns.items():
            existing_columns = {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')
            }
            missing_columns = sorted(set(columns) - existing_columns)
            if missing_columns:
                missing_column_reports.append(
                    f"{table_name}: {', '.join(missing_columns)}"
                )
        if missing_column_reports:
            raise RuntimeError(
                "Required schema columns missing: " + "; ".join(missing_column_reports)
            )

    @staticmethod
    def _verify_forecast_provenance_schema(conn) -> None:
        """Fail startup when the immutable forecast contract is incomplete."""
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(_REQUIRED_FORECAST_PROVENANCE_TABLES - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "Required forecast provenance tables missing: "
                + ", ".join(missing_tables)
            )
        for (
            table_name,
            required_columns,
        ) in _REQUIRED_FORECAST_PROVENANCE_COLUMNS.items():
            column_rows = list(conn.execute(f'PRAGMA table_info("{table_name}")'))
            columns = {row[1] for row in column_rows}
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise RuntimeError(
                    f"Required forecast provenance columns missing from {table_name}: {', '.join(missing_columns)}"
                )
            actual_primary_key = tuple(
                (
                    row[1]
                    for row in sorted(column_rows, key=lambda item: item[5])
                    if row[5] > 0
                )
            )
            required_primary_key = _REQUIRED_FORECAST_PROVENANCE_PRIMARY_KEYS[
                table_name
            ]
            if not actual_primary_key != required_primary_key:
                continue
            raise RuntimeError(
                f"Required forecast provenance primary key invalid for {table_name}: expected {required_primary_key}, got {actual_primary_key}"
            )
        for (
            table_name,
            required_fragments,
        ) in _REQUIRED_FORECAST_PROVENANCE_SQL_FRAGMENTS.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            normalized_table_sql = "".join(str(row[0]).lower().split()) if row else ""
            missing_fragments = [
                sql_fragment
                for sql_fragment in required_fragments
                if sql_fragment not in normalized_table_sql
            ]
            if not missing_fragments:
                continue
            raise RuntimeError(
                f"Required forecast provenance constraints missing from {table_name}: {', '.join(missing_fragments)}"
            )
        actual_foreign_keys = {
            (table_name, row[3], row[2], row[4], row[6].upper())
            for table_name in _REQUIRED_FORECAST_PROVENANCE_TABLES
            for row in conn.execute(f'PRAGMA foreign_key_list("{table_name}")')
        }
        missing_foreign_keys = sorted(
            _REQUIRED_FORECAST_PROVENANCE_FOREIGN_KEYS - actual_foreign_keys
        )
        if missing_foreign_keys:
            raise RuntimeError(
                "Required forecast provenance foreign keys missing: "
                + ", ".join((str(foreign_key) for foreign_key in missing_foreign_keys))
            )
        existing_triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        missing_triggers = sorted(
            _REQUIRED_FORECAST_PROVENANCE_TRIGGERS - existing_triggers
        )
        if missing_triggers:
            raise RuntimeError(
                "Required forecast provenance triggers missing: "
                + ", ".join(missing_triggers)
            )

    def _init_default_data(self, conn) -> None:
        """Initialize default data in database. @zara"""
        cursor = conn.cursor()
        today = date.today()
        cursor.execute("SELECT id FROM astronomy_system_info WHERE id = 1")
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO astronomy_system_info\n                   (id, latitude, longitude, timezone, installed_capacity_kwp)\n                   VALUES (1, ?, ?, ?, ?)",
                (
                    self.latitude,
                    self.longitude,
                    self.timezone_str,
                    self.solar_capacity_kwp,
                ),
            )
            _LOGGER.debug("Initialized astronomy system info")
        cursor.execute("SELECT COUNT(*) FROM astronomy_hourly_peaks")
        if cursor.fetchone()[0] == 0:
            hourly_peaks = [(hour, 0) for hour in range(24)]
            cursor.executemany(
                "INSERT INTO astronomy_hourly_peaks (hour, kwh) VALUES (?, ?)",
                hourly_peaks,
            )
            _LOGGER.debug("Initialized hourly peaks")
        cursor.execute(
            "SELECT COUNT(*) FROM astronomy_cache WHERE cache_date >= ?",
            (today.isoformat(),),
        )
        if cursor.fetchone()[0] == 0:
            self._init_astronomy_cache(cursor, today)
            _LOGGER.debug("Initialized astronomy cache")
        cursor.execute(
            "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
            (today.isoformat(),),
        )
        if cursor.fetchone()[0] == 0:
            self._init_weather_forecast(cursor, today)
            _LOGGER.debug("Initialized weather forecast")
        conn.commit()

    def _init_astronomy_cache(self, cursor, today: date) -> None:
        """Initialize astronomy cache with baseline data. @zara"""
        astronomy_rows = []
        for day_offset in range(7):
            forecast_day = today + timedelta(days=day_offset)
            date_text = forecast_day.isoformat()
            sunrise = datetime.combine(forecast_day, time(6, 0), tzinfo=self.timezone)
            sunset = datetime.combine(forecast_day, time(18, 0), tzinfo=self.timezone)
            solar_noon = datetime.combine(
                forecast_day, time(12, 0), tzinfo=self.timezone
            )
            for hour in range(24):
                if 6 <= hour <= 18:
                    sun_elevation_deg = max(0, 60 * (1 - abs(hour - 12) / 6))
                    sun_azimuth_deg = 90 + (hour - 6) * 15
                    clear_sky_radiation_wm2 = max(
                        0, 1000 * (sun_elevation_deg / 60) ** 1.5
                    )
                    theoretical_max_kwh = (
                        clear_sky_radiation_wm2 / 1000 * self.solar_capacity_kwp
                    )
                else:
                    sun_elevation_deg = sun_azimuth_deg = clear_sky_radiation_wm2 = (
                        theoretical_max_kwh
                    ) = 0
                astronomy_rows.append(
                    (
                        date_text,
                        hour,
                        round(sun_elevation_deg, 1),
                        round(sun_azimuth_deg, 1),
                        round(clear_sky_radiation_wm2, 0),
                        round(theoretical_max_kwh, 3),
                        sunrise.isoformat(),
                        sunset.isoformat(),
                        solar_noon.isoformat(),
                        12.0,
                    )
                )
                continue
            continue
        cursor.executemany(
            "INSERT OR IGNORE INTO astronomy_cache\n               (cache_date, hour, sun_elevation_deg, sun_azimuth_deg,\n                clear_sky_radiation_wm2, theoretical_max_kwh,\n                sunrise, sunset, solar_noon, daylight_hours)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            astronomy_rows,
        )

    def _init_weather_forecast(self, cursor, today: date) -> None:
        """Initialize weather forecast with baseline data. @zara"""
        weather_rows = []
        for day_offset in range(3):
            forecast_day = today + timedelta(days=day_offset)
            date_text = forecast_day.isoformat()
            for hour in range(24):
                if 6 <= hour <= 18:
                    sun_elevation_deg = max(0, 60 * (1 - abs(hour - 12) / 6))
                    clear_sky_radiation_wm2 = max(
                        0, 1000 * (sun_elevation_deg / 60) ** 1.5
                    )
                    direct_radiation_wm2 = int(clear_sky_radiation_wm2 * 0.35)
                    diffuse_radiation_wm2 = int(clear_sky_radiation_wm2 * 0.24)
                    solar_radiation_wm2 = int(clear_sky_radiation_wm2 * 0.5)
                else:
                    direct_radiation_wm2 = diffuse_radiation_wm2 = (
                        solar_radiation_wm2
                    ) = 0
                weather_rows.append(
                    (
                        date_text,
                        hour,
                        10.0,
                        70,
                        50,
                        0.0,
                        3.0,
                        1013.0,
                        direct_radiation_wm2,
                        diffuse_radiation_wm2,
                        solar_radiation_wm2,
                    )
                )
        cursor.executemany(
            "INSERT OR IGNORE INTO weather_forecast\n               (forecast_date, hour, temperature, humidity, clouds,\n                rain, wind, pressure, direct_radiation, diffuse_radiation,\n                solar_radiation_wm2)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            weather_rows,
        )

    def get_db_path(self) -> str:
        """Get the database file path. @zara"""
        return str(self.db_path)

    def get_config(self) -> dict[str, Any]:
        """Get the configuration. @zara"""
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "solar_capacity_kwp": self.solar_capacity_kwp,
            "timezone": self.timezone_str,
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
        }
