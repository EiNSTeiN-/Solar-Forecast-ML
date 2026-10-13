# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Append-only corrections for completed-day energy metrics."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo
import aiosqlite

SCHEMA_VERSION = 3
TOKEN_TTL = timedelta(minutes=5)
MAX_TARGET_KWH = 1000000.0
METRIC_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "id": "home_consumption_day_kwh",
        "daily_column": "home_consumption_kwh",
        "hourly_column": "home_consumption_kwh",
        "class": "consumer",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.homeConsumption",
    },
    {
        "id": "grid_import_day_kwh",
        "daily_column": "grid_import_kwh",
        "hourly_column": "grid_import_kwh",
        "class": "meter",
        "limit_rule": "import_limit_kw",
        "i18n_key": "corrections.metric.gridImport",
    },
    {
        "id": "smartmeter_import_day_kwh",
        "daily_column": "smartmeter_import_kwh",
        "hourly_column": None,
        "class": "meter",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.smartmeterImport",
    },
    {
        "id": "grid_export_day_kwh",
        "daily_column": "grid_export_kwh",
        "hourly_column": "grid_export_kwh",
        "class": "meter",
        "limit_rule": "export_limit_kw",
        "i18n_key": "corrections.metric.gridExport",
    },
    {
        "id": "smartmeter_export_day_kwh",
        "daily_column": "smartmeter_export_kwh",
        "hourly_column": None,
        "class": "meter",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.smartmeterExport",
    },
    {
        "id": "solar_yield_day_kwh",
        "daily_column": "solar_yield_kwh",
        "hourly_column": "solar_yield_kwh",
        "class": "meter",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.solarYield",
    },
    {
        "id": "solar_to_house_day_kwh",
        "daily_column": "solar_to_house_kwh",
        "hourly_column": "solar_to_house_kwh",
        "class": "flow",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.solarToHouse",
    },
    {
        "id": "solar_to_battery_day_kwh",
        "daily_column": "solar_to_battery_kwh",
        "hourly_column": "solar_to_battery_kwh",
        "class": "flow",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.solarToBattery",
    },
    {
        "id": "battery_to_house_day_kwh",
        "daily_column": "battery_to_house_kwh",
        "hourly_column": "battery_to_house_kwh",
        "class": "flow",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.batteryToHouse",
    },
    {
        "id": "grid_to_house_day_kwh",
        "daily_column": "grid_to_house_kwh",
        "hourly_column": "grid_to_house_kwh",
        "class": "flow",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.gridToHouse",
    },
    {
        "id": "grid_to_battery_day_kwh",
        "daily_column": "grid_to_battery_kwh",
        "hourly_column": "grid_to_battery_kwh",
        "class": "flow",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.gridToBattery",
    },
    {
        "id": "consumer_heatpump_day_kwh",
        "daily_column": "consumer_heatpump_kwh",
        "hourly_column": "consumer_heatpump_kwh",
        "class": "consumer",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.heatpump",
    },
    {
        "id": "consumer_heatingrod_day_kwh",
        "daily_column": "consumer_heatingrod_kwh",
        "hourly_column": "consumer_heatingrod_kwh",
        "class": "consumer",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.heatingrod",
    },
    {
        "id": "consumer_wallbox_day_kwh",
        "daily_column": "consumer_wallbox_kwh",
        "hourly_column": "consumer_wallbox_kwh",
        "class": "consumer",
        "limit_rule": "max_target_kwh",
        "i18n_key": "corrections.metric.wallbox",
    },
)
METRIC_BY_ID = {item["id"]: item for item in METRIC_REGISTRY}
METRICS = {
    item["id"]: (
        item["daily_column"],
        (
            item["limit_rule"]
            if item["limit_rule"] in {"export_limit_kw", "import_limit_kw"}
            else None
        ),
    )
    for item in METRIC_REGISTRY
}
HOURLY_METRICS = {
    item["id"]: item["hourly_column"]
    for item in METRIC_REGISTRY
    if item["hourly_column"] is not None
}
_SOLAR_FLOW = (
    "solar_flow",
    ("solar_to_house_day_kwh", "solar_to_battery_day_kwh"),
    "solar_yield_day_kwh",
)
_GRID_FLOW = (
    "grid_flow",
    ("grid_to_house_day_kwh", "grid_to_battery_day_kwh"),
    "grid_import_day_kwh",
)
_HOME_BALANCE = (
    "home_balance",
    ("solar_to_house_day_kwh", "battery_to_house_day_kwh", "grid_to_house_day_kwh"),
    "home_consumption_day_kwh",
)
_BALANCE_LIMIT_PERCENT = 5.0
DEFAULT_REASON_CODE = "user_confirmed"
DEFAULT_EVIDENCE_TYPE = "user_confirmation"
BACKFILL_REASON_CODE = "historical_backfill"
DEFAULT_MODE = "correct"
MODES = frozenset({DEFAULT_MODE, "backfill"})
REASON_CODES = frozenset(
    {
        DEFAULT_REASON_CODE,
        "outlier_verified",
        "gap_exact_daily_value",
        BACKFILL_REASON_CODE,
    }
)
EVIDENCE_TYPES = frozenset(
    {
        DEFAULT_EVIDENCE_TYPE,
        "utility_invoice",
        "meter_portal_export",
        "signed_daily_report",
    }
)
BACKFILL_EVIDENCE_TYPES = frozenset(
    {"utility_invoice", "meter_portal_export", "signed_daily_report"}
)
_COLUMN = re.compile("^[a-z][a-z0-9_]*$")
_DECIMAL = re.compile("^(?:0|[1-9][0-9]*)(?:[.][0-9]{1,3})?$")
_IDEMPOTENCY_KEY = re.compile("^[A-Za-z0-9_-]{16,128}$")
_EVIDENCE_REFERENCE = re.compile("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_BATCH_ID = re.compile("^[a-f0-9]{32}$")
CSV_MAX_BYTES = 5242880
CSV_MAX_ROWS = 20000
CSV_MIN_DATE = date(2015, 1, 1)
CSV_TEMPLATE = "date;metric;value_kwh\n2026-01-15;grid_import_day_kwh;12.5\n"
_CSV_HEADER = ("date", "metric", "value_kwh")
_RANGE_LIMIT_DAYS = 5000
_WEIGHTINGS = frozenset({"measured", "uniform"})


class CorrectionError(RuntimeError):
    """Stable customer-facing correction error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EnergyCorrectionStore:
    """Keep corrections separate from the read-only SFML database."""

    def __init__(
        self, corrections_path: Path, solar_db_path: Path, timezone_name: str
    ) -> None:
        self._path = corrections_path
        self._solar_path = solar_db_path
        self._timezone = ZoneInfo(timezone_name)
        self._initialized = False

    async def async_initialize(self) -> None:
        if self._initialized:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, 448)
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA journal_mode=DELETE")
            await connection.execute("PRAGMA busy_timeout=30000")
            await connection.executescript(
                "\n                CREATE TABLE IF NOT EXISTS correction_schema (\n                    schema_version INTEGER PRIMARY KEY, created_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS correction_events (\n                    event_id TEXT PRIMARY KEY, audit_sequence INTEGER NOT NULL UNIQUE,\n                    created_at TEXT NOT NULL,\n                    event_type TEXT NOT NULL CHECK(event_type IN ('correction','undo')),\n                    target_date TEXT NOT NULL, metric TEXT NOT NULL, mode TEXT,\n                    batch_id TEXT,\n                    absolute_value_kwh REAL NOT NULL, raw_source_kwh REAL NOT NULL,\n                    previous_effective_kwh REAL NOT NULL, delta_kwh REAL NOT NULL,\n                    reason_code TEXT NOT NULL, evidence_type TEXT NOT NULL,\n                    evidence_reference TEXT NOT NULL, reason_note TEXT,\n                    actor_user_id TEXT NOT NULL, reverses_event_id TEXT,\n                    preview_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE,\n                    previous_audit_hash TEXT NOT NULL, audit_hash TEXT NOT NULL UNIQUE\n                );\n                CREATE INDEX IF NOT EXISTS correction_events_target_idx\n                    ON correction_events(target_date, metric, created_at);\n                CREATE TABLE IF NOT EXISTS preview_tokens (\n                    preview_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,\n                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,\n                    target_date TEXT NOT NULL, metric TEXT NOT NULL, mode TEXT,\n                    raw_source_kwh REAL NOT NULL, expected_effective_kwh REAL NOT NULL,\n                    target_value_kwh REAL NOT NULL, reason_code TEXT NOT NULL,\n                    evidence_type TEXT NOT NULL, evidence_reference TEXT NOT NULL,\n                    reason_note TEXT, actor_user_id TEXT NOT NULL,\n                    idempotency_key TEXT NOT NULL UNIQUE\n                );\n                CREATE TABLE IF NOT EXISTS preview_consumptions (\n                    preview_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL,\n                    FOREIGN KEY(preview_id) REFERENCES preview_tokens(preview_id)\n                );\n                CREATE TABLE IF NOT EXISTS correction_config_events (\n                    event_id TEXT PRIMARY KEY, audit_sequence INTEGER NOT NULL UNIQUE,\n                    created_at TEXT NOT NULL,\n                    import_limit_kw REAL NOT NULL, export_limit_kw REAL NOT NULL,\n                    actor_user_id TEXT NOT NULL, previous_audit_hash TEXT NOT NULL,\n                    audit_hash TEXT NOT NULL UNIQUE\n                );\n                CREATE TABLE IF NOT EXISTS correction_audit_head (\n                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),\n                    last_sequence INTEGER NOT NULL, last_hash TEXT NOT NULL\n                );\n                INSERT OR IGNORE INTO correction_audit_head VALUES (1,0,'0000000000000000000000000000000000000000000000000000000000000000');\n                CREATE TRIGGER IF NOT EXISTS correction_events_no_update\n                    BEFORE UPDATE ON correction_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;\n                CREATE TRIGGER IF NOT EXISTS correction_events_no_delete\n                    BEFORE DELETE ON correction_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;\n                CREATE TRIGGER IF NOT EXISTS correction_config_no_update\n                    BEFORE UPDATE ON correction_config_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;\n                CREATE TRIGGER IF NOT EXISTS correction_config_no_delete\n                    BEFORE DELETE ON correction_config_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;\n            "
            )
            await self._ensure_mode_columns(connection)
            await self._ensure_batch_column(connection)
            schema_row = await self._one(
                connection, "SELECT MAX(schema_version) version FROM correction_schema"
            )
            stored_schema_version = schema_row["version"] if schema_row else None
            if stored_schema_version is None or stored_schema_version == 2:
                await connection.execute(
                    "INSERT INTO correction_schema VALUES (?, ?)",
                    (SCHEMA_VERSION, self._now().isoformat()),
                )
            elif stored_schema_version != SCHEMA_VERSION:
                raise CorrectionError(
                    "unsupported_schema", "Correction database schema is incompatible."
                )
            await self._verify_audit_chain(connection)
            await connection.commit()
        os.chmod(self._path, 384)
        self._initialized = True

    async def async_status(self) -> dict[str, Any]:
        await self.async_initialize()
        return {
            "schema_version": SCHEMA_VERSION,
            "metrics": sorted(METRICS),
            "registry": [
                {
                    "id": metric["id"],
                    "class": metric["class"],
                    "i18n_key": metric["i18n_key"],
                    "has_hourly_profile": metric["hourly_column"] is not None,
                }
                for metric in METRIC_REGISTRY
            ],
            "setup_required": False,
        }

    async def async_context(
        self, target_date: Any, metric: Any, today: date, mode: Any = DEFAULT_MODE
    ) -> dict[str, Any]:
        """Return the effective daily value and its diagnostic hourly profile."""
        selected_day, metric_id = self._validated_selection(target_date, metric, today)
        selected_mode = self._mode(mode)
        await self.async_initialize()
        source_kwh = await self._source_value(selected_day, metric_id, selected_mode)
        projection = await self.async_projection(selected_day.isoformat())
        day_projection = projection.get(selected_day.isoformat(), {})
        effective_kwh = self.project_value(
            projection, selected_day.isoformat(), metric_id, source_kwh
        )
        hourly_values = await self._hourly_values(selected_day, metric_id)
        hourly_sum_kwh = sum((hourly_row["value_kwh"] for hourly_row in hourly_values))
        expected_hours = int(self._day_hours(selected_day))
        return {
            "target_date": selected_day.isoformat(),
            "metric": metric_id,
            "source_value_kwh": round(source_kwh, 3),
            "effective_value_kwh": round(effective_kwh, 3),
            "is_corrected": metric_id in day_projection,
            "hourly_values": hourly_values,
            "hourly_sum_kwh": round(hourly_sum_kwh, 3),
            "hourly_delta_kwh": round(source_kwh - hourly_sum_kwh, 3),
            "expected_hours": expected_hours,
            "available_hours": len(hourly_values),
        }

    async def async_configure_limits(
        self, import_limit_kw: Any, export_limit_kw: Any, actor_user_id: str
    ) -> dict[str, Any]:
        validated_import_limit = self._positive(import_limit_kw, "import_limit_kw")
        validated_export_limit = self._positive(export_limit_kw, "export_limit_kw")
        await self.async_initialize()
        new_event_id = secrets.token_hex(16)
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            audit_sequence, previous_audit_hash = await self._audit_head(connection)
            created_at = self._now().isoformat()
            event_payload = {
                "event_id": new_event_id,
                "audit_sequence": audit_sequence,
                "created_at": created_at,
                "import_limit_kw": validated_import_limit,
                "export_limit_kw": validated_export_limit,
                "actor_user_id": actor_user_id,
                "previous_audit_hash": previous_audit_hash,
            }
            audit_hash = self._audit_hash(event_payload)
            await connection.execute(
                "INSERT INTO correction_config_events\n                   (event_id,audit_sequence,created_at,import_limit_kw,export_limit_kw,actor_user_id,\n                    previous_audit_hash,audit_hash) VALUES (?,?,?,?,?,?,?,?)",
                (
                    new_event_id,
                    audit_sequence,
                    created_at,
                    validated_import_limit,
                    validated_export_limit,
                    actor_user_id,
                    previous_audit_hash,
                    audit_hash,
                ),
            )
            await self._advance_audit_head(connection, audit_sequence, audit_hash)
            await connection.commit()
        return await self.async_status()

    async def async_preview(
        self,
        target_date: Any,
        metric: Any,
        target_value_kwh: Any,
        reason_code: Any,
        evidence_type: Any,
        evidence_reference: Any,
        reason_note: Any,
        idempotency_key: Any,
        actor_user_id: str,
        today: date,
        mode: Any = DEFAULT_MODE,
    ) -> dict[str, Any]:
        (
            selected_day,
            metric_id,
            target_kwh,
            validated_reason,
            validated_evidence_type,
            validated_evidence_reference,
            validated_reason_note,
            selected_mode,
        ) = self._validated(
            target_date,
            metric,
            target_value_kwh,
            reason_code,
            evidence_type,
            evidence_reference,
            reason_note,
            today,
            mode,
        )
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        await self._reject_over_cap(metric_id, selected_day, target_kwh)
        source_kwh = await self._source_value(selected_day, metric_id, selected_mode)
        projection = await self.async_projection(selected_day.isoformat())
        before_kwh = self.project_value(
            projection, selected_day.isoformat(), metric_id, source_kwh
        )
        delta_kwh = target_kwh - before_kwh
        balance_warnings = await self._balance_warnings(
            selected_day, selected_mode, projection, {metric_id: target_kwh}
        )
        requires_confirmation = abs(delta_kwh) > max(
            2.0, abs(before_kwh) * 0.25
        ) or bool(balance_warnings)
        token = secrets.token_urlsafe(32)
        preview_id = secrets.token_hex(16)
        now = self._now()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            await connection.execute(
                "DELETE FROM preview_tokens WHERE expires_at<? AND preview_id NOT IN\n                   (SELECT preview_id FROM preview_consumptions)",
                (now.isoformat(),),
            )
            existing_preview = await self._one(
                connection,
                "SELECT 1 present FROM preview_tokens WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if existing_preview is not None:
                await connection.rollback()
                raise CorrectionError(
                    "idempotency_conflict", "Preview idempotency key is already in use."
                )
            await connection.execute(
                "INSERT INTO preview_tokens\n                (preview_id,token_hash,created_at,expires_at,target_date,metric,mode,\n                 raw_source_kwh,expected_effective_kwh,target_value_kwh,reason_code,\n                 evidence_type,evidence_reference,reason_note,actor_user_id,idempotency_key)\n                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    preview_id,
                    self._token_hash(token),
                    now.isoformat(),
                    (now + TOKEN_TTL).isoformat(),
                    selected_day.isoformat(),
                    metric_id,
                    selected_mode,
                    source_kwh,
                    before_kwh,
                    target_kwh,
                    validated_reason,
                    validated_evidence_type,
                    validated_evidence_reference,
                    validated_reason_note,
                    actor_user_id,
                    idempotency_key,
                ),
            )
            await connection.commit()
        return {
            "preview_token": token,
            "target_date": selected_day.isoformat(),
            "metric": metric_id,
            "mode": selected_mode,
            "reason_code": validated_reason,
            "evidence_type": validated_evidence_type,
            "evidence_reference": validated_evidence_reference,
            "source_value_kwh": round(source_kwh, 3),
            "before_kwh": round(before_kwh, 3),
            "after_kwh": round(target_kwh, 3),
            "delta_kwh": round(delta_kwh, 3),
            "requires_second_confirmation": requires_confirmation,
            "balance_warnings": balance_warnings,
            "expires_at": (now + TOKEN_TTL).isoformat(),
        }

    async def async_commit(
        self,
        preview_token: Any,
        idempotency_key: Any,
        confirmed_large_change: Any,
        actor_user_id: str,
    ) -> dict[str, Any]:
        if not isinstance(preview_token, str) or not preview_token:
            raise CorrectionError(
                "invalid_preview_token", "A preview token is required."
            )
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA busy_timeout=30000")
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            now = self._now()
            existing_event = await self._one(
                connection,
                "SELECT e.*,p.token_hash preview_token_hash FROM correction_events e\n                    LEFT JOIN preview_tokens p ON p.preview_id=e.preview_id\n                    WHERE e.idempotency_key=?",
                (idempotency_key,),
            )
            if existing_event is not None:
                if (
                    existing_event["actor_user_id"] != actor_user_id
                    or existing_event["event_type"] != "correction"
                    or existing_event["preview_token_hash"]
                    != self._token_hash(preview_token)
                ):
                    await connection.rollback()
                    raise CorrectionError(
                        "idempotency_conflict", "Idempotency key is already in use."
                    )
                await connection.rollback()
                return {
                    "event_id": existing_event["event_id"],
                    "effective_value_kwh": round(
                        existing_event["absolute_value_kwh"], 3
                    ),
                    "idempotent_replay": True,
                }
            preview_row = await self._one(
                connection,
                "SELECT p.* FROM preview_tokens p\n                LEFT JOIN preview_consumptions c ON c.preview_id=p.preview_id\n                WHERE p.token_hash=? AND c.preview_id IS NULL",
                (self._token_hash(preview_token),),
            )
            if preview_row is None or preview_row["actor_user_id"] != actor_user_id:
                await connection.rollback()
                raise CorrectionError(
                    "invalid_preview_token", "Preview is invalid or already used."
                )
            if str(preview_row["evidence_reference"] or "").startswith("batch:"):
                await connection.rollback()
                raise CorrectionError(
                    "invalid_preview_token", "Preview is invalid or already used."
                )
            if preview_row["idempotency_key"] != idempotency_key:
                await connection.rollback()
                raise CorrectionError(
                    "idempotency_conflict", "Idempotency key does not match preview."
                )
            if datetime.fromisoformat(preview_row["expires_at"]) <= now:
                await connection.rollback()
                raise CorrectionError("preview_expired", "Preview has expired.")
            selected_mode = preview_row["mode"] or DEFAULT_MODE
            source_kwh = await self._source_value(
                date.fromisoformat(preview_row["target_date"]),
                preview_row["metric"],
                selected_mode,
            )
            before_kwh = await self._effective_value(
                connection,
                preview_row["target_date"],
                preview_row["metric"],
                source_kwh,
            )
            if not (
                math.isclose(source_kwh, preview_row["raw_source_kwh"], abs_tol=0.0005)
                and math.isclose(
                    before_kwh, preview_row["expected_effective_kwh"], abs_tol=0.0005
                )
            ):
                await connection.rollback()
                raise CorrectionError(
                    "correction_conflict", "Source value changed after preview."
                )
            try:
                await self._reject_over_cap(
                    preview_row["metric"],
                    date.fromisoformat(preview_row["target_date"]),
                    float(preview_row["target_value_kwh"]),
                    connection,
                )
            except CorrectionError:
                await connection.rollback()
                raise
            delta_kwh = float(preview_row["target_value_kwh"]) - before_kwh
            balance_warnings = await self._balance_warnings(
                date.fromisoformat(preview_row["target_date"]),
                selected_mode,
                await self._projection_on(connection, preview_row["target_date"]),
                {preview_row["metric"]: float(preview_row["target_value_kwh"])},
            )
            requires_confirmation = abs(delta_kwh) > max(
                2.0, abs(before_kwh) * 0.25
            ) or bool(balance_warnings)
            if requires_confirmation and confirmed_large_change is not True:
                await connection.rollback()
                raise CorrectionError(
                    "second_confirmation_required",
                    "Confirm the unusually large correction.",
                )
            await connection.execute(
                "INSERT INTO preview_consumptions VALUES (?, ?)",
                (preview_row["preview_id"], now.isoformat()),
            )
            new_event_id = secrets.token_hex(16)
            audit_sequence, previous_audit_hash = await self._audit_head(connection)
            event_payload = {
                "event_id": new_event_id,
                "audit_sequence": audit_sequence,
                "created_at": now.isoformat(),
                "event_type": "correction",
                "target_date": preview_row["target_date"],
                "metric": preview_row["metric"],
                "mode": selected_mode,
                "absolute_value_kwh": float(preview_row["target_value_kwh"]),
                "raw_source_kwh": source_kwh,
                "previous_effective_kwh": before_kwh,
                "delta_kwh": delta_kwh,
                "reason_code": preview_row["reason_code"],
                "evidence_type": preview_row["evidence_type"],
                "evidence_reference": preview_row["evidence_reference"],
                "reason_note": preview_row["reason_note"],
                "actor_user_id": actor_user_id,
                "reverses_event_id": None,
                "preview_id": preview_row["preview_id"],
                "idempotency_key": idempotency_key,
                "previous_audit_hash": previous_audit_hash,
            }
            audit_hash = self._audit_hash(event_payload)
            await self._insert_event(connection, event_payload, audit_hash)
            await self._advance_audit_head(connection, audit_sequence, audit_hash)
            await connection.commit()
        return {
            "event_id": new_event_id,
            "effective_value_kwh": round(before_kwh + delta_kwh, 3),
        }

    async def async_undo(
        self, event_id: Any, idempotency_key: Any, actor_user_id: str
    ) -> dict[str, Any]:
        if not isinstance(event_id, str) or not event_id:
            raise CorrectionError("invalid_event", "A correction event is required.")
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            now = self._now()
            existing_event = await self._one(
                connection,
                "SELECT * FROM correction_events WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if existing_event is not None:
                if (
                    existing_event["actor_user_id"] != actor_user_id
                    or existing_event["event_type"] != "undo"
                    or existing_event["reverses_event_id"] != event_id
                ):
                    await connection.rollback()
                    raise CorrectionError(
                        "idempotency_conflict", "Idempotency key is already in use."
                    )
                await connection.rollback()
                return {
                    "event_id": existing_event["event_id"],
                    "effective_value_kwh": round(
                        existing_event["absolute_value_kwh"], 3
                    ),
                    "idempotent_replay": True,
                }
            active_event = await self._one(
                connection,
                "SELECT e.* FROM correction_events e\n                WHERE e.event_id=? AND e.event_type='correction'\n                AND NOT EXISTS(SELECT 1 FROM correction_events u\n                    WHERE u.reverses_event_id=e.event_id)\n                AND e.audit_sequence=(\n                    SELECT MAX(active.audit_sequence) FROM correction_events active\n                    WHERE active.event_type='correction'\n                    AND active.target_date=e.target_date AND active.metric=e.metric\n                    AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                        WHERE undone.reverses_event_id=active.event_id)\n                )",
                (event_id,),
            )
            if active_event is None:
                await connection.rollback()
                raise CorrectionError(
                    "event_not_undoable", "Correction is unknown or already undone."
                )
            source_kwh = await self._source_value(
                date.fromisoformat(active_event["target_date"]),
                active_event["metric"],
                active_event["mode"] or DEFAULT_MODE,
            )
            before_kwh = await self._effective_value(
                connection,
                active_event["target_date"],
                active_event["metric"],
                source_kwh,
            )
            previous_event = await self._one(
                connection,
                "SELECT active.absolute_value_kwh FROM correction_events active\n                    WHERE active.event_type='correction'\n                    AND active.target_date=? AND active.metric=?\n                    AND active.audit_sequence<?\n                    AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                        WHERE undone.reverses_event_id=active.event_id)\n                    ORDER BY active.audit_sequence DESC LIMIT 1",
                (
                    active_event["target_date"],
                    active_event["metric"],
                    active_event["audit_sequence"],
                ),
            )
            restored_kwh = (
                float(previous_event["absolute_value_kwh"])
                if previous_event is not None
                else source_kwh
            )
            delta_kwh = restored_kwh - before_kwh
            undo_event_id = secrets.token_hex(16)
            audit_sequence, previous_audit_hash = await self._audit_head(connection)
            event_payload = {
                "event_id": undo_event_id,
                "audit_sequence": audit_sequence,
                "created_at": now.isoformat(),
                "event_type": "undo",
                "target_date": active_event["target_date"],
                "metric": active_event["metric"],
                "mode": active_event["mode"] or DEFAULT_MODE,
                "absolute_value_kwh": restored_kwh,
                "raw_source_kwh": source_kwh,
                "previous_effective_kwh": before_kwh,
                "delta_kwh": delta_kwh,
                "reason_code": active_event["reason_code"],
                "reason_note": None,
                "evidence_type": active_event["evidence_type"],
                "evidence_reference": active_event["evidence_reference"],
                "actor_user_id": actor_user_id,
                "reverses_event_id": event_id,
                "preview_id": f"undo:{undo_event_id}",
                "idempotency_key": idempotency_key,
                "previous_audit_hash": previous_audit_hash,
            }
            if active_event["batch_id"] is not None:
                event_payload["batch_id"] = active_event["batch_id"]
            audit_hash = self._audit_hash(event_payload)
            await self._insert_event(connection, event_payload, audit_hash)
            await self._advance_audit_head(connection, audit_sequence, audit_hash)
            await connection.commit()
        return {
            "event_id": undo_event_id,
            "effective_value_kwh": round(restored_kwh, 3),
        }

    async def async_preview_range(
        self,
        metric: Any,
        start_date: Any,
        end_date: Any,
        target_sum: Any,
        weighting: Any,
        idempotency_key: Any,
        actor_user_id: str,
        today: date,
    ) -> dict[str, Any]:
        first_day, last_day = self._validated_range(start_date, end_date, today)
        metric_id = self._validated_selection(first_day.isoformat(), metric, today)[1]
        target_kwh = self._decimal(target_sum, "target_sum_kwh")
        if weighting not in _WEIGHTINGS:
            raise CorrectionError(
                "invalid_weighting", "Weighting must be measured or uniform."
            )
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        selected_days = [
            first_day + timedelta(days=day_offset)
            for day_offset in range((last_day - first_day).days + 1)
        ]
        prepared_days = []
        source_values = []
        for selected_day in selected_days:
            try:
                source_kwh, selected_mode = await self._source_for_bulk(
                    selected_day, metric_id
                )
            except CorrectionError as error:
                raise CorrectionError(
                    error.code, f"{selected_day.isoformat()}: {error}"
                ) from error
            source_values.append(source_kwh)
            prepared_days.append(
                {
                    "day": selected_day,
                    "metric": metric_id,
                    "source": source_kwh,
                    "mode": selected_mode,
                }
            )
        allocated_targets, fallback_reason, rounded_target_sum = _allocate_targets(
            source_values, target_kwh, str(weighting)
        )
        for prepared_day, allocated_kwh in zip(prepared_days, allocated_targets):
            await self._reject_over_cap(metric_id, prepared_day["day"], allocated_kwh)
            prepared_day["after"] = allocated_kwh
        return await self._store_bulk_preview(
            prepared_days,
            metric_id,
            idempotency_key,
            actor_user_id,
            "range",
            {
                "metric": metric_id,
                "start_date": first_day.isoformat(),
                "end_date": last_day.isoformat(),
                "weighting": weighting,
                "applied_weighting": "uniform" if fallback_reason else weighting,
                "fallback_reason": fallback_reason,
                "target_sum_kwh": rounded_target_sum,
            },
        )

    async def async_commit_range(
        self,
        preview_token: Any,
        idempotency_key: Any,
        confirmed_large_change: Any,
        actor_user_id: str,
    ) -> dict[str, Any]:
        return await self._commit_bulk(
            preview_token,
            idempotency_key,
            confirmed_large_change,
            actor_user_id,
            "range",
        )

    async def async_preview_csv(
        self, content: Any, idempotency_key: Any, actor_user_id: str, today: date
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        csv_result = _parse_csv(content, today)
        prepared_days = []
        seen_keys = set()
        rejections = list(csv_result["rejections"])
        for csv_row in csv_result["rows"]:
            row_key = (csv_row["date"], csv_row["metric"])
            if row_key in seen_keys:
                rejections.append({"line": csv_row["line"], "reason": "duplicate"})
                continue
            seen_keys.add(row_key)
            selected_day = date.fromisoformat(csv_row["date"])
            try:
                source_kwh, selected_mode = await self._source_for_bulk(
                    selected_day, csv_row["metric"]
                )
                await self._reject_over_cap(
                    csv_row["metric"], selected_day, csv_row["value_kwh"]
                )
            except CorrectionError as error:
                rejections.append(
                    {
                        "line": csv_row["line"],
                        "reason": error.code,
                        "message": f"{csv_row['date']}: {error}",
                    }
                )
                continue
            prepared_days.append(
                {
                    "day": selected_day,
                    "metric": csv_row["metric"],
                    "source": source_kwh,
                    "mode": selected_mode,
                    "after": csv_row["value_kwh"],
                    "line": csv_row["line"],
                }
            )
        rejections.sort(key=lambda item: item["line"])
        preview_result = (
            await self._store_bulk_preview(
                prepared_days,
                prepared_days[0]["metric"] if prepared_days else "grid_import_day_kwh",
                idempotency_key,
                actor_user_id,
                "csv",
                {},
            )
            if prepared_days
            else {
                "preview_token": None,
                "days": [],
                "requires_second_confirmation": False,
                "balance_warnings": [],
                "expires_at": None,
            }
        )
        delta_by_metric = {}
        covered_dates = []
        for preview_day in preview_result["days"]:
            covered_dates.append(preview_day["date"])
            delta_by_metric[preview_day["metric"]] = round(
                delta_by_metric.get(preview_day["metric"], 0.0)
                + preview_day["after_kwh"]
                - preview_day["before_kwh"],
                2,
            )
        return {
            "rows_total": csv_result["rows_total"],
            "accepted": len(prepared_days),
            "rejected": len(rejections),
            "rejections": rejections,
            "days_covered": sorted(set(covered_dates)),
            "sum_delta_by_metric": delta_by_metric,
            "preview_token": preview_result["preview_token"],
            "requires_second_confirmation": preview_result[
                "requires_second_confirmation"
            ],
            "balance_warnings": preview_result["balance_warnings"],
            "expires_at": preview_result["expires_at"],
        }

    async def async_commit_csv(
        self,
        preview_token: Any,
        idempotency_key: Any,
        confirmed_large_change: Any,
        actor_user_id: str,
    ) -> dict[str, Any]:
        return await self._commit_bulk(
            preview_token, idempotency_key, confirmed_large_change, actor_user_id, "csv"
        )

    async def async_undo_batch(
        self, batch_id: Any, idempotency_key: Any, actor_user_id: str
    ) -> dict[str, Any]:
        if not (isinstance(batch_id, str) and _BATCH_ID.fullmatch(batch_id)):
            raise CorrectionError("invalid_batch", "A correction batch is required.")
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            existing_event = await self._one(
                connection,
                "SELECT * FROM correction_events WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if existing_event is not None:
                if (
                    existing_event["actor_user_id"] != actor_user_id
                    or existing_event["event_type"] != "undo"
                    or existing_event["batch_id"] != batch_id
                ):
                    await connection.rollback()
                    raise CorrectionError(
                        "idempotency_conflict", "Idempotency key is already in use."
                    )
                await connection.rollback()
                return {
                    "batch_id": batch_id,
                    "event_id": existing_event["event_id"],
                    "idempotent_replay": True,
                }
            rows = await self._all(
                connection,
                "SELECT e.* FROM correction_events e\n                WHERE e.batch_id=? AND e.event_type='correction'\n                AND NOT EXISTS(SELECT 1 FROM correction_events u\n                    WHERE u.reverses_event_id=e.event_id)\n                AND e.audit_sequence=(\n                    SELECT MAX(active.audit_sequence) FROM correction_events active\n                    WHERE active.event_type='correction'\n                    AND active.target_date=e.target_date AND active.metric=e.metric\n                    AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                        WHERE undone.reverses_event_id=active.event_id)\n                )\n                ORDER BY e.audit_sequence DESC",
                (batch_id,),
            )
            if not rows:
                await connection.rollback()
                raise CorrectionError(
                    "batch_not_undoable", "Batch is unknown or already undone."
                )
            undo_event_ids = []
            for index, active_event in enumerate(rows):
                source_kwh = await self._source_value(
                    date.fromisoformat(active_event["target_date"]),
                    active_event["metric"],
                    active_event["mode"] or DEFAULT_MODE,
                )
                before_kwh = await self._effective_value(
                    connection,
                    active_event["target_date"],
                    active_event["metric"],
                    source_kwh,
                )
                previous_event = await self._one(
                    connection,
                    "SELECT active.absolute_value_kwh FROM correction_events active\n                        WHERE active.event_type='correction'\n                        AND active.target_date=? AND active.metric=?\n                        AND active.audit_sequence<?\n                        AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                            WHERE undone.reverses_event_id=active.event_id)\n                        ORDER BY active.audit_sequence DESC LIMIT 1",
                    (
                        active_event["target_date"],
                        active_event["metric"],
                        active_event["audit_sequence"],
                    ),
                )
                restored_kwh = (
                    float(previous_event["absolute_value_kwh"])
                    if previous_event is not None
                    else source_kwh
                )
                undo_event_id = secrets.token_hex(16)
                audit_sequence, previous_audit_hash = await self._audit_head(connection)
                event_payload = {
                    "event_id": undo_event_id,
                    "audit_sequence": audit_sequence,
                    "created_at": self._now().isoformat(),
                    "event_type": "undo",
                    "target_date": active_event["target_date"],
                    "metric": active_event["metric"],
                    "mode": active_event["mode"] or DEFAULT_MODE,
                    "batch_id": batch_id,
                    "absolute_value_kwh": restored_kwh,
                    "raw_source_kwh": source_kwh,
                    "previous_effective_kwh": before_kwh,
                    "delta_kwh": restored_kwh - before_kwh,
                    "reason_code": active_event["reason_code"],
                    "reason_note": None,
                    "evidence_type": active_event["evidence_type"],
                    "evidence_reference": active_event["evidence_reference"],
                    "actor_user_id": actor_user_id,
                    "reverses_event_id": active_event["event_id"],
                    "preview_id": f"undo:{undo_event_id}",
                    "idempotency_key": (
                        idempotency_key
                        if index == 0
                        else f"{idempotency_key}.{index:04d}"
                    ),
                    "previous_audit_hash": previous_audit_hash,
                }
                audit_hash = self._audit_hash(event_payload)
                await self._insert_event(connection, event_payload, audit_hash)
                await self._advance_audit_head(connection, audit_sequence, audit_hash)
                undo_event_ids.append(undo_event_id)
                continue
            await connection.commit()
        return {
            "batch_id": batch_id,
            "event_ids": undo_event_ids,
            "event_id": undo_event_ids[0],
        }

    async def async_history(self, limit: int = 100) -> list[dict[str, Any]]:
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            rows = await self._all(
                connection,
                "SELECT e.*,\n                EXISTS(SELECT 1 FROM correction_events u WHERE u.reverses_event_id=e.event_id) undone,\n                (e.event_type='correction'\n                 AND NOT EXISTS(SELECT 1 FROM correction_events u WHERE u.reverses_event_id=e.event_id)\n                 AND e.audit_sequence=(SELECT MAX(active.audit_sequence)\n                    FROM correction_events active WHERE active.event_type='correction'\n                    AND active.target_date=e.target_date AND active.metric=e.metric\n                    AND NOT EXISTS(SELECT 1 FROM correction_events undone_event\n                        WHERE undone_event.reverses_event_id=active.event_id))) undoable\n                FROM correction_events e ORDER BY audit_sequence DESC LIMIT ?",
                (max(1, min(int(limit), 250)),),
            )
        history_fields = (
            "event_id",
            "audit_sequence",
            "created_at",
            "event_type",
            "target_date",
            "metric",
            "mode",
            "absolute_value_kwh",
            "raw_source_kwh",
            "previous_effective_kwh",
            "delta_kwh",
            "reason_code",
            "evidence_type",
            "evidence_reference",
            "reason_note",
            "reverses_event_id",
            "batch_id",
            "undone",
            "undoable",
        )
        history_entries = []
        for event_row in rows:
            history_entry = {
                field_name: event_row[field_name] for field_name in history_fields
            }
            if history_entry.get("mode") is None:
                history_entry["mode"] = DEFAULT_MODE
            history_entries.append(history_entry)
        return history_entries

    async def async_projection(
        self, start_date: str | None = None
    ) -> dict[str, dict[str, float]]:
        """Return the latest absolute effective value for every corrected day."""
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            return await self._projection_on(connection, start_date)
        return None if True else None

    @staticmethod
    def project_value(
        projection: dict[str, dict[str, float]],
        target_date: str,
        metric: str,
        raw_value: Any,
    ) -> float:
        """Apply the sole correction projection contract to a daily raw value."""
        return projection.get(target_date, {}).get(metric, float(raw_value or 0.0))

    async def _limits(self) -> dict[str, float | None]:
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            return await self._limits_on(connection)
        return None if True else None

    async def _limits_on(self, db: aiosqlite.Connection) -> dict[str, float | None]:
        limits_row = await self._one(
            db,
            "SELECT import_limit_kw,export_limit_kw\n            FROM correction_config_events ORDER BY audit_sequence DESC LIMIT 1",
        )
        return (
            (
                (
                    {
                        "import_limit_kw": (
                            float(limits_row["import_limit_kw"]) if limits_row else None
                        ),
                        "export_limit_kw": (
                            float(limits_row["export_limit_kw"]) if limits_row else None
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

    async def _ensure_mode_columns(self, db: aiosqlite.Connection) -> None:
        for table_name in ("correction_events", "preview_tokens"):
            table_info = await self._all(db, f"PRAGMA table_info({table_name})")
            column_names = {column["name"] for column in table_info}
            if column_names and "mode" not in column_names:
                await db.execute(f"ALTER TABLE {table_name} ADD COLUMN mode TEXT")

    async def _ensure_batch_column(self, db: aiosqlite.Connection) -> None:
        table_info = await self._all(db, "PRAGMA table_info(correction_events)")
        column_names = {column["name"] for column in table_info}
        if column_names and "batch_id" not in column_names:
            await db.execute("ALTER TABLE correction_events ADD COLUMN batch_id TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS correction_events_batch_idx ON correction_events(batch_id)"
        )

    async def _reject_over_cap(
        self,
        metric: str,
        day: date,
        target: float,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        if target > await self._target_cap_kwh(metric, day, db):
            raise CorrectionError(
                "target_out_of_range", "Target exceeds the supported technical range."
            )

    async def _target_cap_kwh(
        self, metric: str, day: date, db: aiosqlite.Connection | None = None
    ) -> float:
        day_hours = self._day_hours(day)
        if not math.isfinite(day_hours) or day_hours < 23 or day_hours > 25:
            raise CorrectionError(
                "target_out_of_range", "Target exceeds the supported technical range."
            )
        limit_rule = METRIC_BY_ID[metric]["limit_rule"]
        if limit_rule in {"export_limit_kw", "import_limit_kw"}:
            limits = (
                await self._limits_on(db) if db is not None else await self._limits()
            )
            limit_kw = limits[limit_rule]
            if limit_kw is not None:
                return min(MAX_TARGET_KWH, limit_kw * day_hours)
        return MAX_TARGET_KWH

    async def _projection_on(
        self, db: aiosqlite.Connection, start_date: str | None = None
    ) -> dict[str, dict[str, float]]:
        query = "SELECT e.target_date,e.metric,e.absolute_value_kwh\n            FROM correction_events e\n            WHERE e.event_type='correction'\n            AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                WHERE undone.reverses_event_id=e.event_id)\n            AND e.audit_sequence=(SELECT MAX(latest.audit_sequence) FROM correction_events latest\n                WHERE latest.event_type='correction'\n                AND latest.target_date=e.target_date AND latest.metric=e.metric\n                AND NOT EXISTS(SELECT 1 FROM correction_events latest_undo\n                    WHERE latest_undo.reverses_event_id=latest.event_id))"
        query_parameters = ()
        if start_date is not None:
            query += " AND e.target_date>=?"
            query_parameters = (start_date,)
        rows = await self._all(db, query, query_parameters)
        projection = {}
        for event_row in rows:
            projection.setdefault(event_row["target_date"], {})[event_row["metric"]] = (
                float(event_row["absolute_value_kwh"])
            )
        return projection

    def _validated_range(
        self, start_date: Any, end_date: Any, today: date
    ) -> tuple[date, date]:
        try:
            first_day = date.fromisoformat(str(start_date))
            last_day = date.fromisoformat(str(end_date))
        except ValueError as error:
            raise CorrectionError("invalid_date", "Expected YYYY-MM-DD.") from error
        if last_day < first_day or (last_day - first_day).days + 1 > _RANGE_LIMIT_DAYS:
            raise CorrectionError("invalid_range", "The date range is invalid.")
        if first_day >= today or last_day >= today:
            raise CorrectionError(
                "completed_day_required", "Only completed past days are supported."
            )
        return (first_day, last_day)

    async def _source_for_bulk(self, day: date, metric: str) -> tuple[float, str]:
        try:
            return (await self._source_value(day, metric, DEFAULT_MODE), DEFAULT_MODE)
        except CorrectionError as error:
            if error.code not in {"eod_not_finalized", "source_value_unavailable"}:
                raise
        return (await self._source_value(day, metric, "backfill"), "backfill")

    async def _store_bulk_preview(
        self,
        prepared: list[dict[str, Any]],
        metric_name: str,
        idempotency_key: str,
        actor_user_id: str,
        kind: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        await self.async_initialize()
        preview_days = []
        warnings = []
        requires_confirmation = False
        for prepared_day in prepared:
            selected_day = prepared_day["day"]
            metric_id = prepared_day["metric"]
            projection = await self.async_projection(selected_day.isoformat())
            before_kwh = self.project_value(
                projection, selected_day.isoformat(), metric_id, prepared_day["source"]
            )
            prepared_day["before"] = before_kwh
            delta_kwh = prepared_day["after"] - before_kwh
            day_warnings = await self._balance_warnings(
                selected_day,
                prepared_day["mode"],
                projection,
                {metric_id: prepared_day["after"]},
            )
            warnings.extend(day_warnings)
            if abs(delta_kwh) > max(2.0, abs(before_kwh) * 0.25) or day_warnings:
                requires_confirmation = True
            preview_days.append(
                {
                    "date": selected_day.isoformat(),
                    "metric": metric_id,
                    "before_kwh": round(before_kwh, 2),
                    "after_kwh": round(prepared_day["after"], 2),
                }
            )
            continue
        token = secrets.token_urlsafe(32)
        batch_id = secrets.token_hex(16)
        now = self._now()
        validated_evidence_reference = f"batch:{kind}:{batch_id}"
        validated_reason_note = extra.get("fallback_reason")
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            await connection.execute(
                "DELETE FROM preview_tokens WHERE expires_at<? AND preview_id NOT IN\n                   (SELECT preview_id FROM preview_consumptions)",
                (now.isoformat(),),
            )
            existing_event = await self._one(
                connection,
                "SELECT 1 present FROM preview_tokens WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if existing_event is not None:
                await connection.rollback()
                raise CorrectionError(
                    "idempotency_conflict", "Preview idempotency key is already in use."
                )
            await connection.execute(
                "INSERT INTO preview_tokens\n                (preview_id,token_hash,created_at,expires_at,target_date,metric,mode,\n                 raw_source_kwh,expected_effective_kwh,target_value_kwh,reason_code,\n                 evidence_type,evidence_reference,reason_note,actor_user_id,idempotency_key)\n                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    self._token_hash(token),
                    now.isoformat(),
                    (now + TOKEN_TTL).isoformat(),
                    preview_days[0]["date"],
                    metric_name,
                    DEFAULT_MODE,
                    0.0,
                    0.0,
                    extra.get("target_sum_kwh", 0.0),
                    DEFAULT_REASON_CODE,
                    DEFAULT_EVIDENCE_TYPE,
                    validated_evidence_reference,
                    validated_reason_note,
                    actor_user_id,
                    idempotency_key,
                ),
            )
            for index, prepared_day in enumerate(prepared):
                reason_code, validated_evidence_type = (
                    (BACKFILL_REASON_CODE, "meter_portal_export")
                    if prepared_day["mode"] == "backfill"
                    else (DEFAULT_REASON_CODE, DEFAULT_EVIDENCE_TYPE)
                )
                preview_id = f"{batch_id}:{prepared_day['day'].isoformat()}:{prepared_day['metric']}"
                await connection.execute(
                    "INSERT INTO preview_tokens\n                    (preview_id,token_hash,created_at,expires_at,target_date,metric,mode,\n                     raw_source_kwh,expected_effective_kwh,target_value_kwh,reason_code,\n                     evidence_type,evidence_reference,reason_note,actor_user_id,idempotency_key)\n                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        preview_id,
                        self._token_hash(f"{token}:{preview_id}"),
                        now.isoformat(),
                        (now + TOKEN_TTL).isoformat(),
                        prepared_day["day"].isoformat(),
                        prepared_day["metric"],
                        prepared_day["mode"],
                        prepared_day["source"],
                        prepared_day["before"],
                        prepared_day["after"],
                        reason_code,
                        validated_evidence_type,
                        f"{kind}:{prepared_day['day'].isoformat()}:{prepared_day['metric']}",
                        None,
                        actor_user_id,
                        f"{idempotency_key}.{index:04d}",
                    ),
                )
                continue
            await connection.commit()
        preview_result = {
            "preview_token": token,
            "batch_id": batch_id,
            "days": preview_days,
            "requires_second_confirmation": requires_confirmation,
            "balance_warnings": warnings,
            "expires_at": (now + TOKEN_TTL).isoformat(),
        }
        preview_result.update(extra)
        if "target_sum_kwh" in extra:
            preview_result["after_sum_kwh"] = round(
                sum((preview_day["after_kwh"] for preview_day in preview_days)), 2
            )
        return preview_result

    async def _commit_bulk(
        self,
        preview_token: Any,
        idempotency_key: Any,
        confirmed_large_change: Any,
        actor_user_id: str,
        kind: str,
    ) -> dict[str, Any]:
        if not isinstance(preview_token, str) or not preview_token:
            raise CorrectionError(
                "invalid_preview_token", "A preview token is required."
            )
        self._validate_idempotency_key(idempotency_key)
        await self.async_initialize()
        async with aiosqlite.connect(self._path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA busy_timeout=30000")
            await connection.execute("BEGIN IMMEDIATE")
            await self._verify_audit_chain(connection)
            now = self._now()
            batch_preview = await self._one(
                connection,
                "SELECT p.* FROM preview_tokens p\n                LEFT JOIN preview_consumptions c ON c.preview_id=p.preview_id\n                WHERE p.token_hash=? AND c.preview_id IS NULL",
                (self._token_hash(preview_token),),
            )
            if batch_preview is None or batch_preview["actor_user_id"] != actor_user_id:
                await connection.rollback()
                raise CorrectionError(
                    "invalid_preview_token", "Preview is invalid or already used."
                )
            if not str(batch_preview["evidence_reference"]).startswith(
                f"batch:{kind}:"
            ):
                await connection.rollback()
                raise CorrectionError(
                    "invalid_preview_token", "Preview is invalid or already used."
                )
            if batch_preview["idempotency_key"] != idempotency_key:
                await connection.rollback()
                raise CorrectionError(
                    "idempotency_conflict", "Idempotency key does not match preview."
                )
            if datetime.fromisoformat(batch_preview["expires_at"]) <= now:
                await connection.rollback()
                raise CorrectionError("preview_expired", "Preview has expired.")
            batch_id = batch_preview["preview_id"]
            existing_event = await self._one(
                connection,
                "SELECT * FROM correction_events WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if existing_event is not None:
                if (
                    existing_event["actor_user_id"] != actor_user_id
                    or existing_event["event_type"] != "correction"
                    or existing_event["batch_id"] != batch_id
                ):
                    await connection.rollback()
                    raise CorrectionError(
                        "idempotency_conflict", "Idempotency key is already in use."
                    )
                await connection.rollback()
                return {
                    "batch_id": batch_id,
                    "event_id": existing_event["event_id"],
                    "idempotent_replay": True,
                }
            rows = await self._all(
                connection,
                "SELECT * FROM preview_tokens WHERE preview_id LIKE ?\n                    ORDER BY target_date, metric",
                (f"{batch_id}:%",),
            )
            if not rows:
                await connection.rollback()
                raise CorrectionError(
                    "invalid_preview_token", "Preview is invalid or already used."
                )
            requires_confirmation = False
            validated_changes = []
            for preview_row in rows:
                selected_mode = preview_row["mode"] or DEFAULT_MODE
                try:
                    source_kwh = await self._source_value(
                        date.fromisoformat(preview_row["target_date"]),
                        preview_row["metric"],
                        selected_mode,
                    )
                except CorrectionError as error:
                    await connection.rollback()
                    raise CorrectionError(
                        error.code, f"{preview_row['target_date']}: {error}"
                    ) from error
                before_kwh = await self._effective_value(
                    connection,
                    preview_row["target_date"],
                    preview_row["metric"],
                    source_kwh,
                )
                if not (
                    math.isclose(
                        source_kwh, preview_row["raw_source_kwh"], abs_tol=0.0005
                    )
                    and math.isclose(
                        before_kwh,
                        preview_row["expected_effective_kwh"],
                        abs_tol=0.0005,
                    )
                ):
                    await connection.rollback()
                    raise CorrectionError(
                        "correction_conflict", "Source value changed after preview."
                    )
                target_kwh = float(preview_row["target_value_kwh"])
                try:
                    await self._reject_over_cap(
                        preview_row["metric"],
                        date.fromisoformat(preview_row["target_date"]),
                        target_kwh,
                        connection,
                    )
                except CorrectionError:
                    await connection.rollback()
                    raise
                delta_kwh = target_kwh - before_kwh
                balance_warnings = await self._balance_warnings(
                    date.fromisoformat(preview_row["target_date"]),
                    selected_mode,
                    await self._projection_on(connection, preview_row["target_date"]),
                    {preview_row["metric"]: target_kwh},
                )
                if (
                    abs(delta_kwh) > max(2.0, abs(before_kwh) * 0.25)
                    or balance_warnings
                ):
                    requires_confirmation = True
                validated_changes.append(
                    (preview_row, source_kwh, before_kwh, delta_kwh)
                )
                continue
            if requires_confirmation and confirmed_large_change is not True:
                await connection.rollback()
                raise CorrectionError(
                    "second_confirmation_required",
                    "Confirm the unusually large correction.",
                )
            event_ids = []
            for index, (preview_row, source_kwh, before_kwh, delta_kwh) in enumerate(
                validated_changes
            ):
                new_event_id = secrets.token_hex(16)
                audit_sequence, previous_audit_hash = await self._audit_head(connection)
                event_payload = {
                    "event_id": new_event_id,
                    "audit_sequence": audit_sequence,
                    "created_at": now.isoformat(),
                    "event_type": "correction",
                    "target_date": preview_row["target_date"],
                    "metric": preview_row["metric"],
                    "mode": preview_row["mode"] or DEFAULT_MODE,
                    "batch_id": batch_id,
                    "absolute_value_kwh": float(preview_row["target_value_kwh"]),
                    "raw_source_kwh": source_kwh,
                    "previous_effective_kwh": before_kwh,
                    "delta_kwh": delta_kwh,
                    "reason_code": preview_row["reason_code"],
                    "evidence_type": preview_row["evidence_type"],
                    "evidence_reference": preview_row["evidence_reference"],
                    "reason_note": preview_row["reason_note"],
                    "actor_user_id": actor_user_id,
                    "reverses_event_id": None,
                    "preview_id": preview_row["preview_id"],
                    "idempotency_key": (
                        idempotency_key
                        if index == 0
                        else f"{idempotency_key}.{index:04d}"
                    ),
                    "previous_audit_hash": previous_audit_hash,
                }
                audit_hash = self._audit_hash(event_payload)
                await self._insert_event(connection, event_payload, audit_hash)
                await self._advance_audit_head(connection, audit_sequence, audit_hash)
                await connection.execute(
                    "INSERT INTO preview_consumptions VALUES (?, ?)",
                    (preview_row["preview_id"], now.isoformat()),
                )
                event_ids.append(new_event_id)
                continue
            await connection.execute(
                "INSERT INTO preview_consumptions VALUES (?, ?)",
                (batch_id, now.isoformat()),
            )
            await connection.commit()
        return {"batch_id": batch_id, "event_ids": event_ids, "event_id": event_ids[0]}

    async def _balance_warnings(
        self,
        day: date,
        mode: str,
        projection: dict[str, dict[str, float]],
        overrides: dict[str, float],
    ) -> list[dict[str, Any]]:
        raw_values, available_metrics = await self._raw_day(day, mode)
        effective_values = {
            metric["id"]: self.project_value(
                projection, day.isoformat(), metric["id"], raw_values[metric["id"]]
            )
            for metric in METRIC_REGISTRY
        }
        effective_values.update(overrides)
        edited_metric = next(iter(overrides), "")
        warnings = []
        for rule_name, rule_parts, anchor_metric, is_upper_bound in (
            (_SOLAR_FLOW[0], _SOLAR_FLOW[1], _SOLAR_FLOW[2], True),
            (_GRID_FLOW[0], _GRID_FLOW[1], _GRID_FLOW[2], False),
            (_HOME_BALANCE[0], _HOME_BALANCE[1], _HOME_BALANCE[2], False),
        ):
            required_metrics = rule_parts + (anchor_metric,)
            if any(
                (metric_id not in available_metrics for metric_id in required_metrics)
            ):
                continue
            warning = self._balance_warning(
                rule_name,
                rule_parts,
                anchor_metric,
                effective_values,
                edited_metric,
                is_upper_bound,
            )
            if warning is not None:
                warnings.append(warning)
        return warnings

    @staticmethod
    def _balance_warning(
        rule_name: str,
        parts: tuple[str, ...],
        anchor: str,
        values: dict[str, float],
        edited: str,
        upper_bound: bool,
    ) -> dict[str, Any] | None:
        parts_total = sum((values[metric_id] for metric_id in parts))
        anchor_value = values[anchor]
        if upper_bound:
            delta_kwh = parts_total - anchor_value
            if delta_kwh <= 1e-09:
                return
            deviation_percent = EnergyCorrectionStore._percent(
                delta_kwh, anchor_value if anchor_value > 0 else parts_total
            )
        else:
            delta_kwh = abs(parts_total - anchor_value)
            deviation_percent = EnergyCorrectionStore._percent(
                delta_kwh, max(abs(parts_total), abs(anchor_value))
            )
        if deviation_percent <= _BALANCE_LIMIT_PERCENT:
            return
        required_metrics = parts + (anchor,)
        return (
            {
                "rule": rule_name,
                "metric": edited if edited in required_metrics else anchor,
                "deviation_percent": deviation_percent,
            }
            if True
            else None
        )

    @staticmethod
    def _percent(delta: float, base: float) -> float:
        denominator = abs(base) if abs(base) > 1e-09 else abs(delta)
        if denominator <= 1e-09:
            return 0.0
        return round(abs(delta) / denominator * 100.0, 3)

    async def _raw_day(self, day: date, mode: str) -> tuple[dict[str, float], set[str]]:
        zero_values = {metric["id"]: 0.0 for metric in METRIC_REGISTRY}
        daily_columns = []
        for metric in METRIC_REGISTRY:
            column_name = metric["daily_column"]
            if not _COLUMN.fullmatch(column_name):
                raise CorrectionError(
                    "unsupported_metric", "This daily energy metric is not supported."
                )
            daily_columns.append(column_name)
        readonly_uri = f"file:{quote(str(self._solar_path), safe='/')}?mode=ro"
        try:
            async with aiosqlite.connect(readonly_uri, uri=True) as connection:
                connection.row_factory = aiosqlite.Row
                if mode != "backfill":
                    summary_row = await self._one(
                        connection,
                        "SELECT 1 present FROM daily_summaries WHERE date=? LIMIT 1",
                        (day.isoformat(),),
                    )
                    if summary_row is None:
                        raise CorrectionError(
                            "eod_not_finalized", "No finalized daily summary exists."
                        )
                table_info = await self._all(
                    connection, "PRAGMA table_info(stats_daily_energy)"
                )
                existing_columns = {row["name"] for row in table_info}
                selected_columns = [
                    column_name
                    for column_name in daily_columns
                    if column_name in existing_columns
                ]
                if not selected_columns:
                    if mode == "backfill":
                        return (zero_values, set())
                    raise CorrectionError(
                        "source_value_unavailable", "Daily metric is unavailable."
                    )
                row = await self._one(
                    connection,
                    f"SELECT {', '.join(selected_columns)} FROM stats_daily_energy WHERE date=?",
                    (day.isoformat(),),
                )
        except CorrectionError:
            raise
        except (OSError, aiosqlite.Error) as error:
            raise CorrectionError(
                "source_unavailable", "Read-only energy source is unavailable."
            ) from error
        if row is None:
            available_metric_ids = {
                metric["id"]
                for metric in METRIC_REGISTRY
                if metric["daily_column"] in existing_columns
            }
            if mode == "backfill":
                return (zero_values, available_metric_ids)
            raise CorrectionError(
                "source_value_unavailable", "Daily metric is unavailable."
            )
        metric_values = dict(zero_values)
        available_metrics = set()
        for metric in METRIC_REGISTRY:
            column_name = metric["daily_column"]
            if column_name not in existing_columns:
                continue
            available_metrics.add(metric["id"])
            raw_values = row[column_name]
            if raw_values is None:
                continue
            numeric_value = float(raw_values)
            if math.isfinite(numeric_value):
                metric_values[metric["id"]] = numeric_value
        return (metric_values, available_metrics)

    async def _source_value(
        self, day: date, metric: str, mode: str = DEFAULT_MODE
    ) -> float:
        column_name = self._daily_column(metric)
        readonly_uri = f"file:{quote(str(self._solar_path), safe='/')}?mode=ro"
        try:
            async with aiosqlite.connect(readonly_uri, uri=True) as connection:
                connection.row_factory = aiosqlite.Row
                if mode != "backfill":
                    summary_row = await self._one(
                        connection,
                        "SELECT 1 present FROM daily_summaries WHERE date=? LIMIT 1",
                        (day.isoformat(),),
                    )
                    if summary_row is None:
                        raise CorrectionError(
                            "eod_not_finalized", "No finalized daily summary exists."
                        )
                row = await self._one(
                    connection,
                    f"SELECT {column_name} value FROM stats_daily_energy WHERE date=?",
                    (day.isoformat(),),
                )
        except CorrectionError:
            raise
        except (OSError, aiosqlite.Error) as error:
            raise CorrectionError(
                "source_unavailable", "Read-only energy source is unavailable."
            ) from error
        if row is None or row["value"] is None:
            if mode == "backfill":
                return 0.0
            raise CorrectionError(
                "source_value_unavailable", "Daily metric is unavailable."
            )
        value_kwh = float(row["value"])
        if not math.isfinite(value_kwh):
            raise CorrectionError("source_value_invalid", "Daily metric is invalid.")
        return value_kwh

    async def _hourly_values(self, day: date, metric: str) -> list[dict[str, Any]]:
        hourly_column = HOURLY_METRICS.get(metric)
        if hourly_column is None:
            return []
        readonly_uri = f"file:{quote(str(self._solar_path), safe='/')}?mode=ro"
        try:
            async with aiosqlite.connect(readonly_uri, uri=True) as connection:
                connection.row_factory = aiosqlite.Row
                rows = await self._all(
                    connection,
                    f"SELECT hour_key, hour, {hourly_column} value\n                        FROM stats_hourly_billing\n                        WHERE date=? ORDER BY hour_key",
                    (day.isoformat(),),
                )
        except (OSError, aiosqlite.Error) as error:
            raise CorrectionError(
                "source_unavailable", "Read-only hourly energy source is unavailable."
            ) from error
        hourly_values = []
        for row in rows:
            if not row["value"] is not None:
                continue
            value_kwh = float(row["value"])
            if not math.isfinite(value_kwh) or value_kwh < 0:
                continue
            hourly_values.append(
                {
                    "hour_key": str(row["hour_key"]),
                    "hour": int(row["hour"]),
                    "value_kwh": round(value_kwh, 3),
                }
            )
        return hourly_values

    async def _effective_value(
        self, db: aiosqlite.Connection, target_date: str, metric: str, raw_value: float
    ) -> float:
        correction_row = await self._one(
            db,
            "SELECT correction.absolute_value_kwh FROM correction_events correction\n               WHERE correction.event_type='correction'\n               AND correction.target_date=? AND correction.metric=?\n               AND NOT EXISTS(SELECT 1 FROM correction_events undone\n                   WHERE undone.reverses_event_id=correction.event_id)\n               ORDER BY correction.audit_sequence DESC LIMIT 1",
            (target_date, metric),
        )
        return (
            float(correction_row["absolute_value_kwh"]) if correction_row else raw_value
        )

    def _validated(
        self,
        target_date: Any,
        metric: Any,
        value: Any,
        reason_code: Any,
        evidence_type: Any,
        evidence_reference: Any,
        reason_note: Any,
        today: date,
        mode: Any = DEFAULT_MODE,
    ) -> tuple[date, str, float, str, str, str, str | None, str]:
        selected_day, metric = self._validated_selection(target_date, metric, today)
        selected_mode = self._mode(mode)
        target_kwh = self._decimal(value, "target_value_kwh")
        if target_kwh > MAX_TARGET_KWH:
            raise CorrectionError(
                "target_out_of_range", "Target exceeds the supported technical range."
            )
        if reason_code not in REASON_CODES:
            raise CorrectionError(
                "invalid_reason_code", "Select an approved correction reason."
            )
        if evidence_type not in EVIDENCE_TYPES:
            raise CorrectionError(
                "invalid_evidence_type", "Select an approved evidence type."
            )
        if selected_mode == "backfill":
            if reason_code != BACKFILL_REASON_CODE:
                raise CorrectionError(
                    "invalid_reason_code", "Select an approved correction reason."
                )
            if evidence_type not in BACKFILL_EVIDENCE_TYPES:
                raise CorrectionError(
                    "invalid_evidence_type", "Select an approved evidence type."
                )
        elif reason_code == BACKFILL_REASON_CODE:
            raise CorrectionError(
                "invalid_reason_code", "Select an approved correction reason."
            )
        if not (
            isinstance(evidence_reference, str)
            and _EVIDENCE_REFERENCE.fullmatch(evidence_reference)
        ):
            raise CorrectionError(
                "invalid_evidence_reference", "Provide a structured evidence reference."
            )
        if reason_note is not None and (not isinstance(reason_note, str)):
            raise CorrectionError("invalid_reason_note", "Reason note must be text.")
        validated_reason_note = (reason_note or "").strip() or None
        if validated_reason_note is not None and len(validated_reason_note) > 160:
            raise CorrectionError(
                "invalid_reason_note", "Reason note must not exceed 160 characters."
            )
        return (
            selected_day,
            metric,
            target_kwh,
            reason_code,
            evidence_type,
            evidence_reference,
            validated_reason_note,
            selected_mode,
        )

    @staticmethod
    def _mode(mode: Any) -> str:
        if mode is None or mode == "":
            return DEFAULT_MODE
        if mode not in MODES:
            raise CorrectionError("invalid_mode", "Select correction or backfill.")
        return str(mode)

    @staticmethod
    def _validated_selection(
        target_date: Any, metric: Any, today: date
    ) -> tuple[date, str]:
        try:
            selected_day = date.fromisoformat(str(target_date))
        except ValueError as error:
            raise CorrectionError("invalid_date", "Expected YYYY-MM-DD.") from error
        if selected_day >= today:
            raise CorrectionError(
                "completed_day_required", "Only completed past days are supported."
            )
        if metric not in METRICS:
            raise CorrectionError(
                "unsupported_metric", "This daily energy metric is not supported."
            )
        return (selected_day, str(metric))

    @staticmethod
    def _daily_column(metric: str) -> str:
        column_name = METRICS[metric][0]
        if not _COLUMN.fullmatch(column_name):
            raise CorrectionError(
                "unsupported_metric", "This daily energy metric is not supported."
            )
        return column_name

    def _day_hours(self, day: date) -> float:
        start_midnight = datetime.combine(day, datetime.min.time(), self._timezone)
        end_midnight = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), self._timezone
        )
        return (
            end_midnight.astimezone(timezone.utc)
            - start_midnight.astimezone(timezone.utc)
        ).total_seconds() / 3600

    async def _insert_event(
        self, db: aiosqlite.Connection, payload: dict[str, Any], audit_hash: str
    ) -> None:
        await db.execute(
            "INSERT INTO correction_events\n            (event_id,audit_sequence,created_at,event_type,target_date,metric,mode,batch_id,\n             absolute_value_kwh,raw_source_kwh,previous_effective_kwh,delta_kwh,reason_code,\n             evidence_type,evidence_reference,reason_note,\n             actor_user_id,reverses_event_id,preview_id,idempotency_key,\n             previous_audit_hash,audit_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["event_id"],
                payload["audit_sequence"],
                payload["created_at"],
                payload["event_type"],
                payload["target_date"],
                payload["metric"],
                payload.get("mode") or DEFAULT_MODE,
                payload.get("batch_id"),
                payload["absolute_value_kwh"],
                payload["raw_source_kwh"],
                payload["previous_effective_kwh"],
                payload["delta_kwh"],
                payload["reason_code"],
                payload["evidence_type"],
                payload["evidence_reference"],
                payload["reason_note"],
                payload["actor_user_id"],
                payload["reverses_event_id"],
                payload["preview_id"],
                payload["idempotency_key"],
                payload["previous_audit_hash"],
                audit_hash,
            ),
        )

    async def _audit_head(self, db: aiosqlite.Connection) -> tuple[int, str]:
        head_row = await self._one(
            db,
            "SELECT last_sequence,last_hash FROM correction_audit_head WHERE singleton=1",
        )
        return (int(head_row["last_sequence"]) + 1, str(head_row["last_hash"]))

    async def _advance_audit_head(
        self, db: aiosqlite.Connection, sequence: int, audit_hash: str
    ) -> None:
        cursor = await db.execute(
            "UPDATE correction_audit_head SET last_sequence=?,last_hash=?\n               WHERE singleton=1 AND last_sequence=?",
            (sequence, audit_hash, sequence - 1),
        )
        if cursor.rowcount != 1:
            raise CorrectionError(
                "audit_head_conflict", "Correction audit head changed."
            )

    async def _verify_audit_chain(self, db: aiosqlite.Connection) -> None:
        """Fail closed if the append-only audit chain is incomplete or altered."""
        config_events = await self._all(db, "SELECT * FROM correction_config_events")
        correction_events = await self._all(db, "SELECT * FROM correction_events")
        audit_entries = []
        for event_row in config_events:
            event_payload = {
                "event_id": event_row["event_id"],
                "audit_sequence": event_row["audit_sequence"],
                "created_at": event_row["created_at"],
                "import_limit_kw": event_row["import_limit_kw"],
                "export_limit_kw": event_row["export_limit_kw"],
                "actor_user_id": event_row["actor_user_id"],
                "previous_audit_hash": event_row["previous_audit_hash"],
            }
            audit_entries.append(
                (event_row["audit_sequence"], event_row["audit_hash"], event_payload)
            )
        for event_row in correction_events:
            event_payload = {
                field_name: event_row[field_name]
                for field_name in (
                    "event_id",
                    "audit_sequence",
                    "created_at",
                    "event_type",
                    "target_date",
                    "metric",
                    "absolute_value_kwh",
                    "raw_source_kwh",
                    "previous_effective_kwh",
                    "delta_kwh",
                    "reason_code",
                    "evidence_type",
                    "evidence_reference",
                    "reason_note",
                    "actor_user_id",
                    "reverses_event_id",
                    "preview_id",
                    "idempotency_key",
                    "previous_audit_hash",
                )
            }
            if event_row["mode"] is not None:
                event_payload["mode"] = event_row["mode"]
            if event_row["batch_id"] is not None:
                event_payload["batch_id"] = event_row["batch_id"]
            audit_entries.append(
                (event_row["audit_sequence"], event_row["audit_hash"], event_payload)
            )
        previous_hash = (
            "0000000000000000000000000000000000000000000000000000000000000000"
        )
        for stored_hash, (audit_sequence, audit_hash, event_payload) in enumerate(
            sorted(audit_entries, key=lambda item: item[0]), start=1
        ):
            if (
                audit_sequence != stored_hash
                or event_payload["previous_audit_hash"] != previous_hash
            ):
                raise CorrectionError(
                    "audit_chain_invalid", "Correction audit chain is incomplete."
                )
            if self._audit_hash(event_payload) != audit_hash:
                raise CorrectionError(
                    "audit_chain_invalid", "Correction audit chain was altered."
                )
            previous_hash = audit_hash
        audit_head = await self._one(
            db,
            "SELECT last_sequence,last_hash FROM correction_audit_head WHERE singleton=1",
        )
        if (
            audit_head is None
            or audit_head["last_sequence"] != len(audit_entries)
            or audit_head["last_hash"] != previous_hash
        ):
            raise CorrectionError(
                "audit_chain_invalid", "Correction audit head is inconsistent."
            )

    @staticmethod
    def _audit_hash(payload: dict[str, Any]) -> str:
        canonical_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical_json.encode()).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _validate_idempotency_key(value: Any) -> None:
        if not (isinstance(value, str) and _IDEMPOTENCY_KEY.fullmatch(value)):
            raise CorrectionError(
                "invalid_idempotency_key", "A valid idempotency key is required."
            )

    @staticmethod
    def _positive(value: Any, field: str) -> float:
        numeric_value = EnergyCorrectionStore._decimal(value, field)
        if numeric_value <= 0:
            raise CorrectionError(
                "invalid_limit", f"{field} must be greater than zero."
            )
        return numeric_value

    @staticmethod
    def _decimal(value: Any, field: str) -> float:
        if isinstance(value, bool) or isinstance(value, float):
            raise CorrectionError("invalid_number", f"{field} must be a plain decimal.")
        text = str(value)
        if not _DECIMAL.fullmatch(text):
            raise CorrectionError(
                "invalid_number",
                f"{field} must be non-negative with at most 3 decimals.",
            )
        numeric_value = float(text)
        if not math.isfinite(numeric_value):
            raise CorrectionError("invalid_number", f"{field} must be finite.")
        return numeric_value

    @staticmethod
    async def _one(
        db: aiosqlite.Connection, query: str, parameters: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        async with db.execute(query, parameters) as cursor:
            return await cursor.fetchone()
        return None if True else None

    @staticmethod
    async def _all(
        db: aiosqlite.Connection, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with db.execute(query, parameters) as cursor:
            return await cursor.fetchall()
        return None if True else None


def _allocate_targets(
    raws: list[float], target: float, weighting: str
) -> tuple[list[float], str | None, float]:
    """Split a target sum so the cent values add up exactly."""
    applied_weighting = weighting
    fallback_reason = None
    nonnegative_raws = [max(0.0, raw_value) for raw_value in raws]
    if applied_weighting == "measured" and sum(nonnegative_raws) <= 0:
        applied_weighting = "uniform"
        fallback_reason = "raw_sum_zero"
    weights = (
        [Decimal(f"{raw_value:.6f}") for raw_value in nonnegative_raws]
        if applied_weighting == "measured"
        else [Decimal(1)] * len(raws)
    )
    weight_sum = sum(weights, Decimal(0))
    if weight_sum <= 0:
        weights = [Decimal(1)] * len(raws)
        weight_sum = Decimal(len(raws))
        fallback_reason = "raw_sum_zero"
    rounded_target_sum = Decimal(f"{target:.3f}").quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    target_cents = int(rounded_target_sum * 100)
    decimal_allocations = [
        rounded_target_sum * weight / weight_sum for weight in weights
    ]
    integer_cents = [
        int((allocation * 100).to_integral_value(rounding=ROUND_DOWN))
        for allocation in decimal_allocations
    ]
    remaining_cents = target_cents - sum(integer_cents)
    if remaining_cents < 0:
        ranked_indices = sorted(
            range(len(integer_cents)),
            key=lambda index: (
                decimal_allocations[index] * 100 - integer_cents[index],
                index,
            ),
        )
        for adjustment_index in range(-remaining_cents):
            integer_cents[ranked_indices[adjustment_index % len(integer_cents)]] -= 1
    else:
        ranked_indices = sorted(
            range(len(integer_cents)),
            key=lambda index: (
                decimal_allocations[index] * 100 - integer_cents[index],
                -index,
            ),
            reverse=True,
        )
        for adjustment_index in range(remaining_cents):
            integer_cents[ranked_indices[adjustment_index % len(integer_cents)]] += 1
    allocated_values = [float(Decimal(cents) / Decimal(100)) for cents in integer_cents]
    return (allocated_values, fallback_reason, float(rounded_target_sum))


def _parse_csv(content: Any, today: date) -> dict[str, Any]:
    if not isinstance(content, (bytes, bytearray)):
        raise CorrectionError("invalid_csv", "CSV content is required.")
    if len(content) > CSV_MAX_BYTES:
        raise CorrectionError("csv_too_large", "CSV exceeds 5 MB.")
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CorrectionError("invalid_csv", "CSV must be UTF-8.") from error
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise CorrectionError("csv_missing_header", "CSV header is required.")
    delimiter = _csv_header_delimiter(lines[0])
    data_lines = lines[1:]
    if len(data_lines) > CSV_MAX_ROWS:
        raise CorrectionError("csv_too_many_rows", "CSV exceeds 20000 rows.")
    accepted_rows = []
    rejections = []
    for line_number, line in enumerate(data_lines, start=2):
        if not line.strip():
            rejections.append({"line": line_number, "reason": "invalid_row"})
            continue
        fields = _split_csv_line(line, delimiter)
        if len(fields) != 3:
            rejections.append({"line": line_number, "reason": "invalid_row"})
            continue
        date_text, metric_text, value_text = (field.strip() for field in fields)
        try:
            parsed_date = date.fromisoformat(date_text)
        except ValueError:
            rejections.append({"line": line_number, "reason": "invalid_date"})
            continue
        if parsed_date < CSV_MIN_DATE:
            rejections.append({"line": line_number, "reason": "date_before_2015"})
            continue
        if parsed_date >= today:
            rejections.append({"line": line_number, "reason": "completed_day_required"})
            continue
        if metric_text not in METRIC_BY_ID:
            rejections.append({"line": line_number, "reason": "unsupported_metric"})
            continue
        try:
            value_kwh = _csv_decimal(value_text)
        except CorrectionError:
            rejections.append({"line": line_number, "reason": "invalid_number"})
            continue
        accepted_rows.append(
            {
                "line": line_number,
                "date": parsed_date.isoformat(),
                "metric": metric_text,
                "value_kwh": value_kwh,
            }
        )
        continue
    return {
        "rows_total": len(data_lines),
        "rows": accepted_rows,
        "rejections": rejections,
    }


def _csv_header_delimiter(line: str) -> str:
    for delimiter in (";", ","):
        fields = tuple((field.strip().lower() for field in line.split(delimiter)))
        if not fields == _CSV_HEADER:
            continue
        return delimiter
    raise CorrectionError(
        "csv_missing_header", "CSV header must be date, metric, value_kwh."
    )


def _split_csv_line(line: str, delimiter: str) -> list[str]:
    fields = line.split(delimiter)
    if len(fields) == 3:
        return fields
    other_delimiter = "," if delimiter == ";" else ";"
    other_fields = line.split(other_delimiter)
    return other_fields if len(other_fields) == 3 else fields


def _csv_decimal(raw: str) -> float:
    text = raw.strip().replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    if not _DECIMAL.fullmatch(text):
        raise CorrectionError("invalid_number", "Value must be a non-negative decimal.")
    return float(text)
