# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Database Manager for Solar Forecast ML.
Handles all SQLite operations using aiosqlite.
"""
import aiofiles
import aiosqlite
import asyncio
import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import threading
import uuid
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from contextlib import asynccontextmanager
from functools import wraps
from time import monotonic, time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from ..const import CURTAILMENT_THROTTLE_REASON_SQL, VERSION
from ..core.core_helpers import SafeDateTimeUtil
from .data_model_promotion_policy import (
    MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS,
    MODEL_PROMOTION_MIN_COMMON_HOURS_PER_CYCLE,
    MODEL_PROMOTION_MIN_INDEPENDENT_DAYS,
    MODEL_PROMOTION_WEATHER_REGIMES,
    build_cycle_aware_promotion_policy_config_v3,
)
from .data_shadow_group_math import compare_panel_group_performance
from .weather_types import DEFAULT_EXPERT_WEIGHTS
from .data_weather_radiation_ensemble import (
    GHI_UPDATE_PRIOR_HOUR_MAX,
    GHI_UPDATE_PRIOR_HOUR_MIN,
    GHI_UPDATE_PRIOR_MIN_HOURS,
    GHI_UPDATE_PRIOR_WINDOW_DAYS,
    WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID,
    compute_ghi_update_prior,
    solar_precision_window_is_apply_ready,
)

try:
    from ..core import core_artifact_crypto as artifact_crypto
except ImportError:
    import importlib.util as _artifact_import_util

    _artifact_crypto_path = (
        Path(__file__).resolve().parents[1] / "core" / "core_artifact_crypto.py"
    )
    _artifact_spec = _artifact_import_util.spec_from_file_location(
        "sfml_core_artifact_crypto", _artifact_crypto_path
    )
    if _artifact_spec is None or _artifact_spec.loader is None:
        raise ImportError(f"artifact crypto module is missing: {_artifact_crypto_path}")
    artifact_crypto = _artifact_import_util.module_from_spec(_artifact_spec)
    _artifact_spec.loader.exec_module(artifact_crypto)
_LOGGER = logging.getLogger(__name__)
PRODUCTION_CANDIDATE_KWH_THRESHOLD = 0.01
LEARNABLE_WEATHER_MISS_ALERT_TYPES = frozenset(
    {"unexpected_rain", "unexpected_snow", "unexpected_clouds"}
)
WEATHER_MISS_CLASS_LEGACY_FORECAST_MISS = "legacy_forecast_miss"
MODEL_GOVERNANCE_SCHEMA_VERSION = "1"
MODEL_GOVERNANCE_ELIGIBILITY_VERSION = "1"
MODEL_BOOTSTRAP_CHAMPION_CONTRACT_VERSION = "physics_baseline_champion_v1"
WEATHER_RADIATION_SNAPSHOT_CONTRACT_VERSION = "weather_radiation_snapshot_v3"
WEATHER_FORECAST_USAGE_CONTRACT_VERSION = "weather_forecast_usage_v1"
WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION = (
    "weather_forecast_correction_interval_start_v3"
)
WEATHER_MLP_TRAINING_CONTRACT_VERSION = "weather_mlp_residual_v2"
WEATHER_EXPERT_STATIC_CONTRACT_VERSION = "weather_expert_static_v1"
WEATHER_EXPERT_LEGACY_CONTRACT_VERSION = "weather_expert_legacy_mutable_cache_v0"
MODEL_PROVENANCE_KINDS = frozenset(
    {
        "donor_imputed",
        "direct_measured",
        "legacy_fallback",
        "capacity_imputed",
        "residual_completed",
    }
)
MODEL_EVALUATION_SAMPLE_KEYS = frozenset(
    {
        "actual",
        "horizon",
        "target_date",
        "target_hour",
        "clean_eligible",
        "weather_regime",
        "forecast_anchor",
        "input_fingerprint",
        "policy_fingerprint",
        "champion_prediction",
        "candidate_prediction",
    }
)
SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID = (
    "shadow_group_relative_percent_repair_v20260805"
)
SHADOW_SYSTEM_HISTORY_REPAIR_MIGRATION_ID = "shadow_system_history_purge_v20260827"
SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID = (
    "shadow_curtailment_observation_repair_v20260923"
)
_CURTAILMENT_SHADOW_NEUTRAL_VALUES = (
    ("method", "ensemble"),
    ("ensemble_mode", "not_evaluable"),
    ("shadow_type", "not_evaluable"),
    ("shadow_percent", 0),
    ("confidence", 0),
    ("root_cause", "insufficient_reference"),
    ("fusion_mode", "production_limited"),
    ("sensor_fusion_shadow_type", "not_evaluable"),
    ("sensor_fusion_shadow_percent", 0),
    ("sensor_fusion_confidence", 0),
    ("sensor_fusion_root_cause", "insufficient_reference"),
    ("evaluable", 0),
    ("learning_eligible", 0),
    ("observation_class", "not_evaluable"),
    ("interpretation", "Production is limited or panel-group actuals are incomplete"),
    ("shadow_detected", 0),
)
_SHADOW_VERDICT_TYPES_LEFT_ALONE = (
    "not_evaluable",
    "none",
    "error",
    "unknown",
    "night",
)


def _neutral_shadow_assignment(columns: set) -> Tuple[str, List[Any]]:
    selected_fields = [
        (column_name, field_value)
        for column_name, field_value in _CURTAILMENT_SHADOW_NEUTRAL_VALUES
        if column_name in columns
    ]
    if not selected_fields:
        return ("", [])
    update_clause = ", ".join(
        (f"{column_name} = ?" for column_name, unused_value in selected_fields)
    )
    return (
        update_clause,
        [field_value for unused_column, field_value in selected_fields],
    )


def _is_sqlite_authorizer_error(error: BaseException) -> bool:
    error_message = str(error).lower()
    return "not authorized" in error_message or "is prohibited" in error_message


def _validated_shadow_percentage(
    value: Any, field_name: str, *, required: bool = False
) -> Optional[float]:
    """Return a finite shadow percentage or reject the persistence payload."""
    if value is None and (not required):
        return
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number from 0 to 100")
    try:
        percentage = float(value)
    except (TypeError, ValueError) as validation_error:
        raise ValueError(
            f"{field_name} must be a finite number from 0 to 100"
        ) from validation_error
    if not (math.isfinite(percentage) and 0.0 <= percentage <= 100.0):
        raise ValueError(f"{field_name} must be a finite number from 0 to 100")
    return percentage


def canonical_stable_json(value: Any) -> str:
    """Serialize governance inputs deterministically and reject ambiguous values."""

    def normalize(item: Any) -> Any:
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise ValueError("non-finite value is not canonical")
            return item
        if isinstance(item, (list, tuple)):
            return [normalize(element) for element in item]
        if isinstance(item, dict):
            if not all((isinstance(field_name, str) for field_name in item)):
                raise ValueError("canonical mappings require string keys")
            return {
                field_name: normalize(item[field_name]) for field_name in sorted(item)
            }
        raise ValueError(f"unsupported canonical value: {type(item).__name__}")

    return json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def canonical_stable_hash(value: Any) -> str:
    """Return the SHA-256 digest for canonical governance data."""
    return hashlib.sha256(canonical_stable_json(value).encode("utf-8")).hexdigest()


def protected_identity(*parts: str) -> str:
    """Row identity bound into the AES-GCM additional authenticated data."""
    return "\x1f".join((str(identity_part) for identity_part in parts))


def json_ready(value: Any) -> Any:
    """Convert model objects into values canonical JSON can encode."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        raise ValueError("artifact plaintext contains raw bytes")
    if hasattr(value, "tolist"):
        return json_ready(value.tolist())
    if hasattr(value, "item") and (not isinstance(value, (str, bytes))):
        try:
            return json_ready(value.item())
        except (AttributeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {
            str(field_name): json_ready(element)
            for field_name, element in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_ready(element) for element in value]
    raise ValueError(f"unsupported artifact value: {type(value).__name__}")


def note_unbound_artifact_key(action: str) -> None:
    """Log once that a protected write was refused."""
    artifact_crypto.note_unbound_key(action)


def seal_protected_text(canonical_text: str, table: str, identity: str) -> str:
    """Seal one canonical JSON document for a TEXT column."""
    try:
        return artifact_crypto.seal_text(canonical_text, table, identity)
    except artifact_crypto.ArtifactCryptoError:
        note_unbound_artifact_key("seal")
        raise


def open_protected_text(
    stored: Any, table: str, identity: str, artifact_class: str
) -> Optional[str]:
    """Return canonical JSON text. Legacy plaintext JSON passes through."""
    if isinstance(stored, str) and stored[:1] in "{[":
        return stored
    if not isinstance(stored, str) or not stored:
        return None
    return artifact_crypto.open_text(stored, table, identity, artifact_class)


def loads_protected_json(
    stored: Any, table: str, identity: str, artifact_class: str
) -> Any:
    """Parse one protected JSON column or raise the existing missing-artifact error."""
    cleartext_json = open_protected_text(stored, table, identity, artifact_class)
    if cleartext_json is None:
        raise ValueError("protected artifact is unavailable")
    return json.loads(cleartext_json)


def seal_shadow_snapshot(canonical_text: str, shadow_run_id: str, kind: str) -> bytes:
    """Seal one shadow snapshot. The hash stays over the canonical text."""
    try:
        return artifact_crypto.seal_bytes(
            canonical_text.encode("utf-8"),
            "ai_model_shadow_runs",
            protected_identity(shadow_run_id, kind),
        )
    except artifact_crypto.ArtifactCryptoError:
        note_unbound_artifact_key("shadow snapshot")
        raise


def _load_schema_sql() -> str:
    """Return the canonical schema from the protected data module."""
    from .schema_sql import SCHEMA_SQL as schema_sql

    return schema_sql


def schema_trigger_statements(schema_sql: str, table_name: str) -> list[str]:
    """Return CREATE TRIGGER statements whose target table is ``table_name``."""
    trigger_statements = []
    scan_offset = 0
    while True:
        statement_start = schema_sql.find("CREATE TRIGGER", scan_offset)
        if statement_start < 0:
            return trigger_statements
        statement_end = schema_sql.find(";", statement_start)
        if statement_end < 0:
            return trigger_statements
        trigger_end = schema_sql.find("END;", statement_start)
        if trigger_end < 0:
            return trigger_statements
        schema_statement = schema_sql[statement_start : trigger_end + len("END;")]
        trigger_header = schema_statement.split("BEGIN", 1)[0]
        if re.search(f"\\bON {re.escape(table_name)}\\b", trigger_header):
            trigger_statements.append(schema_statement.strip())
        scan_offset = trigger_end + len("END;")


def schema_triggers_referencing(schema_sql: str, table_name: str) -> list[str]:
    """Return every CREATE TRIGGER whose text names ``table_name``."""
    trigger_statements = []
    scan_offset = 0
    table_reference_pattern = re.compile(f"\\b{re.escape(table_name)}\\b")
    while True:
        statement_start = schema_sql.find("CREATE TRIGGER", scan_offset)
        if statement_start < 0:
            return trigger_statements
        trigger_end = schema_sql.find("END;", statement_start)
        if trigger_end < 0:
            return trigger_statements
        schema_statement = schema_sql[
            statement_start : trigger_end + len("END;")
        ].strip()
        if table_reference_pattern.search(schema_statement):
            trigger_statements.append(schema_statement)
        scan_offset = trigger_end + len("END;")


MODEL_WEIGHT_ARTIFACT_TYPES = ("ridge", "lstm")


def artifact_cleartext_input_size(payload: Any) -> Optional[int]:
    """Return input_size from a plaintext artifact. Sealed payloads yield None."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return
    if not isinstance(payload, dict):
        return
    input_size = payload.get("input_size")
    if isinstance(input_size, bool) or not isinstance(input_size, int):
        return None
    return input_size


def model_weight_artifacts(artifacts: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the hash-bound ridge/lstm set, ignoring sidecar metadata artifacts."""
    return {
        artifact_type: artifacts[artifact_type]
        for artifact_type in MODEL_WEIGHT_ARTIFACT_TYPES
        if artifacts.get(artifact_type) is not None
    }


MODEL_BOOTSTRAP_CHAMPION_ARTIFACT_HASH = canonical_stable_hash(
    {
        "artifact_type": "rule_based_physics_champion",
        "contract_version": MODEL_BOOTSTRAP_CHAMPION_CONTRACT_VERSION,
    }
)
MODEL_PROMOTION_ACTUAL_MEASUREMENT_MAX_LAG = timedelta(hours=36)
MODEL_PROMOTION_ACTUAL_MEASUREMENT_CLOCK_SKEW = timedelta(minutes=5)
SHADOW_JSON_CODEC_PLAIN = "plain-json-v1"
SHADOW_JSON_CODEC_ZLIB = "zlib-json-v1"
SHADOW_JSON_CODEC_AESGCM = artifact_crypto.CODEC
SHADOW_JSON_MAX_BYTES = 16777216
ARTIFACT_PROTECTION_MIGRATION_ID = "artifact_aesgcm_v1"
THRESHOLD_CODE_MIGRATION_ID = "threshold_code_v1"
_SHADOW_THRESHOLD_COLUMNS = (
    "rolling_window_days",
    "min_samples_for_pattern",
    "ema_alpha",
    "fixed_obstruction_threshold",
)
ARTIFACT_SEAL_BATCH = 8
ARTIFACT_COEFFICIENT_SENTINEL = "__artifact__"
_NO_COEFFICIENT_BLOB = object()
MODEL_CANDIDATE_EVIDENCE_MAX_AGE_DAYS = 45
MODEL_CANDIDATE_EVIDENCE_MAX_RUNS = 90
MODEL_CANDIDATE_EVALUATION_TIMEOUT_CALENDAR_DAYS = 10
MODEL_CANDIDATE_EVALUATION_TIMEOUT_SOURCES = frozenset({"eod", "startup"})
MODEL_CANDIDATE_TIMEOUT_CLOSE_CALENDAR_CAP = "CALENDAR_CAP"
MODEL_CANDIDATE_TIMEOUT_CLOSE_REGIME_COVERAGE = "INSUFFICIENT_REGIME_COVERAGE"
MODEL_CANDIDATE_EVIDENCE_MAX_PREDICTIONS = 6480
MODEL_CANDIDATE_EVIDENCE_MAX_ACTUAL_VERSIONS = 25920
MODEL_CANDIDATE_EVIDENCE_MAX_BYTES = 67108864
MODEL_CANDIDATE_RETENTION_COOLING_DAYS = 7
MODEL_CANDIDATE_RETENTION_POLICY_VERSION = "model-candidate-retention-v1"


def normalize_hourly_target_datetime(
    value: Any,
    target_date: Any,
    target_hour: Any,
    *,
    local_timezone: Optional[tzinfo] = None,
) -> datetime:
    """Return the canonical aware local target time for hourly predictions.

    Historical production rows store a naive local wall-clock value while new
    rows must carry an offset.  Naive values are therefore interpreted in the
    configured Home Assistant timezone, but nonexistent and ambiguous DST wall
    times fail closed instead of silently selecting the wrong instant.
    """
    try:
        target_day = date.fromisoformat(str(target_date))
        if isinstance(target_hour, bool):
            raise ValueError
        target_hour_value = int(target_hour)
    except (TypeError, ValueError) as validation_error:
        raise ValueError("hourly target date/hour is invalid") from validation_error
    if not 0 <= target_hour_value <= 23:
        raise ValueError("hourly target hour is invalid")
    if isinstance(value, datetime):
        parsed_datetime = value
    else:
        try:
            parsed_datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as validation_error:
            raise ValueError("hourly target datetime is invalid") from validation_error
    local_zone = local_timezone or SafeDateTimeUtil.get_default_time_zone()
    if local_zone is None:
        raise ValueError("hourly target timezone is unavailable")
    if parsed_datetime.tzinfo is None or parsed_datetime.utcoffset() is None:
        valid_local_candidates = []
        for fold in (0, 1):
            candidate_datetime = parsed_datetime.replace(tzinfo=local_zone, fold=fold)
            roundtrip_datetime = candidate_datetime.astimezone(timezone.utc).astimezone(
                local_zone
            )
            if (
                roundtrip_datetime.replace(tzinfo=None) != parsed_datetime
                or roundtrip_datetime.fold != fold
            ):
                continue
            if any(
                (
                    previous_candidate.utcoffset() == candidate_datetime.utcoffset()
                    for previous_candidate in valid_local_candidates
                )
            ):
                continue
            valid_local_candidates.append(candidate_datetime)
        if not valid_local_candidates:
            raise ValueError("hourly target datetime is nonexistent in local timezone")
        if len(valid_local_candidates) != 1:
            raise ValueError("hourly target datetime is ambiguous in local timezone")
        normalized_datetime = valid_local_candidates[0]
    else:
        normalized_datetime = parsed_datetime.astimezone(local_zone)
        roundtrip_datetime = normalized_datetime.astimezone(timezone.utc).astimezone(
            local_zone
        )
        if (
            roundtrip_datetime.replace(tzinfo=None)
            != normalized_datetime.replace(tzinfo=None)
            or roundtrip_datetime.utcoffset() != normalized_datetime.utcoffset()
        ):
            raise ValueError("hourly target datetime is nonexistent in local timezone")
    if (
        normalized_datetime.date() != target_day
        or normalized_datetime.hour != target_hour_value
    ):
        raise ValueError("hourly target datetime does not match target date/hour")
    return normalized_datetime


def validate_hourly_actual_measurement_time(
    measured_at: Any,
    target_datetime: Any,
    target_date: Any,
    target_hour: Any,
    *,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Validate one actual timestamp against the canonical target instant."""
    if not (isinstance(measured_at, str) and measured_at.strip()):
        raise ValueError("actual_measurement_missing")
    try:
        measured_datetime = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    except ValueError as validation_error:
        raise ValueError("actual_measurement_invalid") from validation_error
    if measured_datetime.tzinfo is None or measured_datetime.utcoffset() is None:
        raise ValueError("actual_measurement_timezone_missing")
    try:
        target_datetime_value = normalize_hourly_target_datetime(
            target_datetime, target_date, target_hour
        )
    except ValueError as validation_error:
        raise ValueError("actual_measurement_target_invalid") from validation_error
    measured_utc = measured_datetime.astimezone(timezone.utc)
    target_utc = target_datetime_value.astimezone(timezone.utc)
    if measured_utc < target_utc:
        raise ValueError("actual_measurement_before_target")
    if measured_utc - target_utc > MODEL_PROMOTION_ACTUAL_MEASUREMENT_MAX_LAG:
        raise ValueError("actual_measurement_too_late")
    current_time = now or SafeDateTimeUtil.now()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        current_time = SafeDateTimeUtil.ensure_local(current_time)
    if (
        measured_utc
        > current_time.astimezone(timezone.utc)
        + MODEL_PROMOTION_ACTUAL_MEASUREMENT_CLOCK_SKEW
    ):
        raise ValueError("actual_measurement_in_future")
    return (measured_datetime, target_datetime_value)


MODEL_CANDIDATE_TERMINAL_REASONS = frozenset(
    {
        "no_go",
        "promoted",
        "superseded",
        "invalidated",
        "incompatible",
        "evidence_timeout",
    }
)


def compress_shadow_json(value: str) -> bytes:
    """Compress one canonical shadow JSON payload without changing its hash basis."""
    if not isinstance(value, str):
        raise ValueError("shadow JSON payload must be text")
    cleartext_bytes = value.encode("utf-8")
    if len(cleartext_bytes) > SHADOW_JSON_MAX_BYTES:
        raise ValueError("shadow JSON payload exceeds the storage limit")
    return zlib.compress(cleartext_bytes, level=9)


def _shadow_json_bytes(plain_value: str, compressed_value: Any, codec: str) -> bytes:
    """Stored UTF-8 bytes of one shadow JSON payload, bounds-checked per codec."""
    if codec == SHADOW_JSON_CODEC_PLAIN:
        if not isinstance(plain_value, str):
            raise ValueError("plain shadow JSON payload is invalid")
        return plain_value.encode("utf-8")
    if codec == SHADOW_JSON_CODEC_ZLIB:
        if not isinstance(compressed_value, (bytes, bytearray, memoryview)):
            raise ValueError("compressed shadow JSON payload is unavailable")
        decompressor = zlib.decompressobj()
        try:
            cleartext_bytes = decompressor.decompress(
                bytes(compressed_value), SHADOW_JSON_MAX_BYTES + 1
            )
        except zlib.error as decoding_error:
            raise ValueError(
                "compressed shadow JSON payload is invalid"
            ) from decoding_error
        if (
            len(cleartext_bytes) > SHADOW_JSON_MAX_BYTES
            or decompressor.unconsumed_tail
            or (not decompressor.eof)
            or decompressor.unused_data
        ):
            raise ValueError("compressed shadow JSON payload is invalid")
        return cleartext_bytes
    raise ValueError("unsupported shadow JSON codec")


def _shadow_cleartext_bytes(
    plain_value: str,
    compressed_value: Any,
    codec: str,
    table: Optional[str] = None,
    identity: Optional[str] = None,
) -> bytes:
    """Recover the hashed canonical bytes for a plain, zlib, or sealed snapshot."""
    if codec == SHADOW_JSON_CODEC_AESGCM:
        if not table or not identity:
            raise ValueError("shadow JSON payload is unavailable")
        cleartext_bytes = artifact_crypto.open_bytes(
            compressed_value, table, identity, "shadow"
        )
        if cleartext_bytes is None:
            raise ValueError("shadow JSON payload is unavailable")
        return cleartext_bytes
    return _shadow_json_bytes(plain_value, compressed_value, codec)


def decode_shadow_json(
    plain_value: str,
    compressed_value: Any,
    codec: str,
    expected_hash: str,
    table: Optional[str] = None,
    identity: Optional[str] = None,
) -> str:
    """Decode and hash-verify one stored shadow JSON payload."""
    cleartext_bytes = _shadow_cleartext_bytes(
        plain_value, compressed_value, codec, table, identity
    )
    try:
        json_text = cleartext_bytes.decode("utf-8")
    except UnicodeDecodeError as decoding_error:
        raise ValueError(
            "compressed shadow JSON payload is not UTF-8"
        ) from decoding_error
    if hashlib.sha256(cleartext_bytes).hexdigest() != expected_hash:
        raise ValueError("shadow JSON payload hash mismatch")
    return json_text


def verify_shadow_json_hash(
    plain_value: str,
    compressed_value: Any,
    codec: str,
    expected_hash: str,
    table: Optional[str] = None,
    identity: Optional[str] = None,
) -> None:
    """Prove a stored shadow payload is byte-identical to what was hashed at write time.

    zlib and SHA-256 both release the GIL, so unlike a JSON decode this keeps
    the event loop free even for the multi-megabyte policy snapshots.
    """
    cleartext_bytes = _shadow_cleartext_bytes(
        plain_value, compressed_value, codec, table, identity
    )
    if hashlib.sha256(cleartext_bytes).hexdigest() != expected_hash:
        raise ValueError("shadow JSON payload hash mismatch")


def _reject_non_finite_json_constant(_name: str) -> Any:
    raise ValueError("promotion raw evidence is not canonical JSON")


def _decode_canonical_shadow_payload(
    plain_value: str,
    compressed_value: Any,
    codec: str,
    expected_hash: str,
    table: Optional[str] = None,
    identity: Optional[str] = None,
) -> Any:
    """Decode one shadow payload and require it to be canonical JSON.

    Parsed JSON only holds str/int/float/bool/None, lists and string-keyed
    dicts, and NaN/Infinity are rejected while parsing, so a sorted dump is
    exactly canonical_stable_json() without its Python-level walk — that
    matters for the multi-megabyte policy snapshots.
    """
    decoded_text = decode_shadow_json(
        plain_value, compressed_value, codec, expected_hash, table, identity
    )
    try:
        decoded_payload = json.loads(
            decoded_text, parse_constant=_reject_non_finite_json_constant
        )
    except (TypeError, json.JSONDecodeError) as decoding_error:
        raise ValueError("promotion raw evidence is not valid JSON") from decoding_error
    canonical_text = json.dumps(
        decoded_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if canonical_text != decoded_text:
        raise ValueError("promotion raw evidence is not canonical JSON")
    return decoded_payload


def _decode_canonical_shadow_run_snapshots(row: Sequence[Any]) -> None:
    """Prove both run snapshots of one shadow run are canonical JSON (CPU-bound)."""
    run_id = str(row[8])
    _decode_canonical_shadow_payload(
        row[0],
        row[1],
        row[6],
        row[2],
        "ai_model_shadow_runs",
        protected_identity(run_id, "input_snapshot"),
    )
    _decode_canonical_shadow_payload(
        row[3],
        row[4],
        row[6],
        row[5],
        "ai_model_shadow_runs",
        protected_identity(run_id, "policy_snapshot"),
    )


def _check_shadow_result_payloads(
    row: Sequence[Any], champion_prediction: float, candidate_prediction: float
) -> None:
    """Both hour result payloads must be canonical and carry the frozen production."""
    decoded_results = (
        _decode_canonical_shadow_payload(row[0], row[1], row[6], row[2]),
        _decode_canonical_shadow_payload(row[3], row[4], row[6], row[5]),
    )
    for result_payload, expected_prediction in (
        (decoded_results[0], champion_prediction),
        (decoded_results[1], candidate_prediction),
    ):
        if not isinstance(result_payload, Mapping) or isinstance(
            result_payload.get("production_kwh"), bool
        ):
            raise ValueError("promotion raw result payload is incomplete")
        try:
            production_kwh = float(result_payload["production_kwh"])
        except (KeyError, TypeError, ValueError) as decoding_error:
            raise ValueError(
                "promotion raw result production is invalid"
            ) from decoding_error
        if math.isfinite(production_kwh) and (not production_kwh < 0):
            if math.isclose(
                production_kwh, expected_prediction, rel_tol=0.0, abs_tol=1e-09
            ):
                continue
        raise ValueError("promotion raw result does not match relational evidence")


def _verify_shadow_run_snapshot_hashes(row: Sequence[Any]) -> None:
    """Prove both run snapshots of one shadow run are byte-identical to their hashes."""
    run_id = str(row[8])
    verify_shadow_json_hash(
        row[0],
        row[1],
        row[6],
        row[2],
        "ai_model_shadow_runs",
        protected_identity(run_id, "input_snapshot"),
    )
    verify_shadow_json_hash(
        row[3],
        row[4],
        row[6],
        row[5],
        "ai_model_shadow_runs",
        protected_identity(run_id, "policy_snapshot"),
    )


DAILY_SUMMARY_EVALUATION_FIELDS = (
    "accuracy_percent",
    "evaluation_predicted_kwh",
    "evaluation_actual_kwh",
    "evaluation_coverage_percent",
    "evaluation_hours_count",
    "production_candidate_hours_count",
    "missing_actual_hours_count",
    "excluded_hours_count",
    "excluded_mppt_hours_count",
    "excluded_weather_alert_hours_count",
    "excluded_inverter_clipped_hours_count",
    "excluded_reason_breakdown_json",
    "error_kwh",
    "error_percent",
)


def daily_summary_evaluation_unchanged(
    stored: Sequence[Any], stats: Mapping[str, Any]
) -> bool:
    """True when a stored summary row (in field order) already carries these stats."""
    for field_index, field_name in enumerate(DAILY_SUMMARY_EVALUATION_FIELDS):
        stored_value, current_value = (stored[field_index], stats[field_name])
        if stored_value is None or current_value is None:
            if stored_value is not current_value:
                return False
            continue
        if isinstance(current_value, float) or isinstance(stored_value, float):
            try:
                if not math.isclose(
                    float(stored_value),
                    float(current_value),
                    rel_tol=0.0,
                    abs_tol=1e-09,
                ):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if stored_value != current_value:
            return False
    return True


def _stamp_row(row: Any) -> Optional[list[Any]]:
    """Plain, hashable copy of one database row for an evidence stamp."""
    if row is None:
        return
    return [
        (
            field_value.hex()
            if isinstance(field_value, (bytes, bytearray, memoryview))
            else field_value
        )
        for field_value in tuple(row)
    ]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all((character in "0123456789abcdef" for character in value))
    )


def _normalize_frozen_evaluation_samples(
    samples: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    normalized_samples = []
    seen_targets = set()
    for sample in samples:
        if set(sample) != MODEL_EVALUATION_SAMPLE_KEYS:
            raise ValueError("incomplete frozen evaluation sample")
        try:
            target_date_value = date.fromisoformat(sample["target_date"]).isoformat()
            forecast_anchor = datetime.fromisoformat(sample["forecast_anchor"])
        except (TypeError, ValueError) as validation_error:
            raise ValueError(
                "invalid frozen evaluation timestamp"
            ) from validation_error
        if forecast_anchor.tzinfo is None or forecast_anchor.utcoffset() is None:
            raise ValueError("frozen forecast anchor must include a timezone")
        target_hour_value = sample["target_hour"]
        if (
            isinstance(target_hour_value, bool)
            or not isinstance(target_hour_value, int)
            or (not 0 <= target_hour_value <= 23)
        ):
            raise ValueError("invalid frozen evaluation target hour")
        target_identity = (target_date_value, target_hour_value)
        if target_identity in seen_targets:
            raise ValueError("duplicate frozen evaluation target")
        seen_targets.add(target_identity)
        if not isinstance(sample["clean_eligible"], bool):
            raise ValueError("unknown frozen evaluation eligibility")
        for field_name in ("actual", "champion_prediction", "candidate_prediction"):
            field_value = sample[field_name]
            if (
                not isinstance(field_value, bool)
                and isinstance(field_value, (int, float))
                and math.isfinite(float(field_value))
            ):
                if not float(field_value) < 0.0:
                    continue
            raise ValueError(f"invalid frozen evaluation {field_name}")
        for field_name in ("input_fingerprint", "policy_fingerprint"):
            if _is_sha256(sample[field_name]):
                continue
            raise ValueError(f"invalid frozen evaluation {field_name}")
        for field_name in ("weather_regime", "horizon"):
            if sample[field_name] is None:
                continue
            if isinstance(sample[field_name], str):
                if sample[field_name]:
                    continue
            raise ValueError(f"invalid frozen evaluation {field_name}")
        normalized_samples.append(
            {
                "target_date": target_date_value,
                "target_hour": target_hour_value,
                "actual": float(sample["actual"]),
                "champion_prediction": float(sample["champion_prediction"]),
                "candidate_prediction": float(sample["candidate_prediction"]),
                "clean_eligible": sample["clean_eligible"],
                "forecast_anchor": forecast_anchor.isoformat(),
                "input_fingerprint": sample["input_fingerprint"],
                "policy_fingerprint": sample["policy_fingerprint"],
                "weather_regime": sample["weather_regime"],
                "horizon": sample["horizon"],
            }
        )
        continue
    if not normalized_samples:
        raise ValueError("frozen evaluation dataset is empty")
    return normalized_samples


def _recompute_model_promotion_decision(
    evaluation_config: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    from ..ai.ai_model_promotion import (
        CriticalSlice as CriticalSlice,
        FrozenPairedTotalStackSample as FrozenPairedTotalStackSample,
        ModelPromotionEvaluationConfig as ModelPromotionEvaluationConfig,
        evaluate_model_promotion as evaluate_model_promotion,
    )

    config_fields = dict(evaluation_config)
    critical_slice_specs = config_fields.pop("critical_slices", ())
    if not isinstance(critical_slice_specs, (list, tuple)):
        raise ValueError("invalid critical slice configuration")
    try:
        promotion_config = ModelPromotionEvaluationConfig(
            **config_fields,
            **{
                "critical_slices": tuple(
                    (
                        CriticalSlice(**dict(slice_spec))
                        for slice_spec in critical_slice_specs
                    )
                )
            },
        )
        return evaluate_model_promotion(
            tuple((FrozenPairedTotalStackSample(**sample) for sample in samples)),
            promotion_config,
        ).to_dict()
    except (TypeError, ValueError) as validation_error:
        raise ValueError("evaluation could not be reproduced") from validation_error


def _normalize_cycle_evaluation_samples(
    samples: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    required_fields = MODEL_EVALUATION_SAMPLE_KEYS | {
        "shadow_run_id",
        "forecast_cycle",
        "actual_version_id",
        "is_production_hour",
        "shadow_prediction_id",
    }
    normalized_samples = []
    seen_targets = set()
    for sample in samples:
        if set(sample) != required_fields:
            raise ValueError("incomplete frozen cycle evaluation sample")
        normalized_sample = _normalize_frozen_evaluation_samples(
            [
                {
                    field_name: sample[field_name]
                    for field_name in MODEL_EVALUATION_SAMPLE_KEYS
                }
            ]
        )[0]
        forecast_cycle = sample["forecast_cycle"]
        if forecast_cycle not in {"pre_sunrise", "post_midnight"}:
            raise ValueError("invalid frozen cycle evaluation forecast_cycle")
        if not isinstance(sample["is_production_hour"], bool):
            raise ValueError("invalid frozen cycle production-hour flag")
        for field_name in (
            "shadow_run_id",
            "shadow_prediction_id",
            "actual_version_id",
        ):
            if isinstance(sample[field_name], str):
                if sample[field_name]:
                    continue
            raise ValueError(f"invalid frozen cycle evaluation {field_name}")
        target_identity = (
            normalized_sample["target_date"],
            normalized_sample["target_hour"],
            forecast_cycle,
        )
        if target_identity in seen_targets:
            raise ValueError("duplicate frozen cycle evaluation target")
        seen_targets.add(target_identity)
        normalized_samples.append(
            {
                **normalized_sample,
                "forecast_cycle": forecast_cycle,
                "is_production_hour": sample["is_production_hour"],
                "shadow_run_id": sample["shadow_run_id"],
                "shadow_prediction_id": sample["shadow_prediction_id"],
                "actual_version_id": sample["actual_version_id"],
            }
        )
        continue
    if not normalized_samples:
        raise ValueError("frozen cycle evaluation dataset is empty")
    return normalized_samples


def _recompute_cycle_aware_model_promotion_decision(
    evaluation_config: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Replay a frozen evaluation with the evaluator its persisted config names.

    Configs without ``policy_contract_version`` are V2 and must keep replaying
    byte-identically because promoted champions are re-verified on every load.
    """
    from ..ai.ai_model_promotion import (
        CycleAwareModelPromotionEvaluationConfig as CycleAwareModelPromotionEvaluationConfig,
        CycleAwareModelPromotionEvaluationConfigV3 as CycleAwareModelPromotionEvaluationConfigV3,
        CycleCriticalSlice as CycleCriticalSlice,
        CycleNoiseBand as CycleNoiseBand,
        FrozenCyclePairedTotalStackSample as FrozenCyclePairedTotalStackSample,
        evaluate_cycle_aware_model_promotion as evaluate_cycle_aware_model_promotion,
        evaluate_cycle_aware_model_promotion_v3 as evaluate_cycle_aware_model_promotion_v3,
    )

    config_fields = dict(evaluation_config)
    critical_slice_specs = config_fields.pop("critical_slices", ())
    if not isinstance(critical_slice_specs, (list, tuple)):
        raise ValueError("invalid critical slice configuration")
    try:
        critical_slices = tuple(
            (
                CycleCriticalSlice(**dict(slice_spec))
                for slice_spec in critical_slice_specs
            )
        )
        if "policy_contract_version" in config_fields:
            noise_band_specs = config_fields.pop("cycle_noise_bands", ())
            if not isinstance(noise_band_specs, (list, tuple)):
                raise ValueError("invalid cycle noise band configuration")
            promotion_config = CycleAwareModelPromotionEvaluationConfigV3(
                **config_fields,
                **{
                    "critical_slices": critical_slices,
                    "cycle_noise_bands": tuple(
                        (
                            CycleNoiseBand(**dict(noise_band_spec))
                            for noise_band_spec in noise_band_specs
                        )
                    ),
                },
            )
            evaluate_promotion = evaluate_cycle_aware_model_promotion_v3
        else:
            promotion_config = CycleAwareModelPromotionEvaluationConfig(
                **config_fields, **{"critical_slices": critical_slices}
            )
            evaluate_promotion = evaluate_cycle_aware_model_promotion
        return evaluate_promotion(
            tuple(
                (
                    FrozenCyclePairedTotalStackSample(
                        **{
                            field_name: sample[field_name]
                            for field_name in (
                                "target_date",
                                "target_hour",
                                "forecast_cycle",
                                "actual",
                                "champion_prediction",
                                "candidate_prediction",
                                "clean_eligible",
                                "is_production_hour",
                                "forecast_anchor",
                                "input_fingerprint",
                                "policy_fingerprint",
                                "weather_regime",
                                "horizon",
                            )
                        }
                    )
                    for sample in samples
                )
            ),
            promotion_config,
        ).to_dict()
    except (TypeError, ValueError) as validation_error:
        raise ValueError(
            "cycle evaluation could not be reproduced"
        ) from validation_error


def _safe_dt(value) -> datetime:
    """Safely parse ISO datetime, fallback to now() if None. @zara"""
    if not value:
        return SafeDateTimeUtil.now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return SafeDateTimeUtil.ensure_local(value)
        return value
    parsed_datetime = datetime.fromisoformat(str(value))
    if parsed_datetime.tzinfo is None:
        return SafeDateTimeUtil.ensure_local(parsed_datetime)
    return parsed_datetime


def _safe_date(value) -> date:
    """Safely parse ISO date, fallback to today() if None. @zara"""
    if not value:
        return date.today()
    if isinstance(value, date) and (not isinstance(value, datetime)):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(str(value)).date()


def _atomic_database_write(method):
    """Serialize a complete runtime persistence operation."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.transaction():
            return await method(self, *args, **kwargs)
        return None if True else None

    return wrapped


@dataclass(frozen=True)
class SharedDatabaseConnectionLease:
    """Lease granting the central service access to the managed connection."""

    if True:
        connection: aiosqlite.Connection
        lease_id: str


class DatabaseManager:
    """Manages SQLite database operations for Solar Forecast ML."""

    _MIGRATION_BUSY_TIMEOUT_MS = 60000
    _DEFAULT_BUSY_TIMEOUT_MS = 500
    _LOCK_RETRY_BUDGET_SECONDS = 35.0
    _BEGIN_RETRY_BUDGET_SECONDS = _LOCK_RETRY_BUDGET_SECONDS
    _LOCK_RETRY_MAX_BACKOFF_SECONDS = 1.0

    def __init__(self, db_path: str):
        """Initialize the database manager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._db = None
        self._write_lock = None
        self._transaction_depth = 0
        self._transaction_owner = None
        self._transaction_abort_event = None
        self._shared_connection_lease = None
        self._governance_write_authorizations = set()
        self._verified_promotion_evidence = {}
        self.last_connect_seconds = None
        self.artifact_instance_id = None

    def _is_governance_write_authorized(self, kind: Any, candidate_id: Any) -> int:
        """Expose only transaction-scoped lifecycle write authority to SQLite."""
        field_name = (str(kind), str(candidate_id))
        return int(
            field_name in self._governance_write_authorizations
            or (str(kind), "*") in self._governance_write_authorizations
        )

    @asynccontextmanager
    async def _authorize_governance_write(self, kind: str, candidate_id: str):
        field_name = (kind, candidate_id)
        self._governance_write_authorizations.add(field_name)
        try:
            yield None
        finally:
            self._governance_write_authorizations.discard(field_name)

    def _bind_artifact_instance(self) -> None:
        """Bind the configured Home Assistant instance id, or leave the key unset."""
        artifact_instance_id = self.artifact_instance_id
        if isinstance(artifact_instance_id, str) and artifact_instance_id:
            artifact_crypto.bind_instance(artifact_instance_id)
        return None if True else None

    async def connect(self) -> None:
        """Establish database connection and create schema if needed."""
        connect_started_at = monotonic()
        self._db = await aiosqlite.connect(
            self.db_path,
            timeout=self._MIGRATION_BUSY_TIMEOUT_MS / 1000,
            isolation_level="IMMEDIATE",
        )
        self._db.row_factory = aiosqlite.Row
        self._write_lock = asyncio.Lock()
        await self._db.create_function(
            "sfml_governance_write_authorized",
            2,
            self._is_governance_write_authorized,
            deterministic=False,
        )
        self._bind_artifact_instance()
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = DELETE")
        await self._db.execute("PRAGMA secure_delete = ON")
        await self._db.execute(
            f"PRAGMA busy_timeout = {self._MIGRATION_BUSY_TIMEOUT_MS}"
        )
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._initialize_schema()
        await self._db.execute(f"PRAGMA busy_timeout = {self._DEFAULT_BUSY_TIMEOUT_MS}")
        self.last_connect_seconds = monotonic() - connect_started_at
        _LOGGER.info(
            "Database connected in %.1f s (schema + migrations): DELETE mode, %dms busy_timeout, %.0fs lock retry budget: %s",
            self.last_connect_seconds,
            self._DEFAULT_BUSY_TIMEOUT_MS,
            self._LOCK_RETRY_BUDGET_SECONDS,
            self.db_path,
        )

    async def close(self) -> None:
        """Close database connection."""
        if self._shared_connection_lease is not None:
            _LOGGER.debug(
                "Database close deferred to the shared connection lease owner"
            )
            return
        await self._close_connection()

    async def _close_connection(self) -> None:
        """Close the managed connection without evaluating shared ownership."""
        if self._db:
            await self._db.close()
            self._db = None
            _LOGGER.info("Database closed")
        return None if True else None

    async def acquire_shared_connection_lease(self) -> SharedDatabaseConnectionLease:
        """Return the single connection lease for the installation database service."""
        if self._db is None:
            raise RuntimeError("database connection is not available")
        if self._shared_connection_lease is None:
            self._shared_connection_lease = SharedDatabaseConnectionLease(
                connection=self._db, lease_id=uuid.uuid4().hex
            )
        return self._shared_connection_lease

    async def release_shared_connection_lease(
        self, lease: SharedDatabaseConnectionLease, *, close: bool = False
    ) -> None:
        """Release the service lease and optionally close its managed connection."""
        if lease is not self._shared_connection_lease:
            raise RuntimeError("invalid shared database connection lease")
        self._shared_connection_lease = None
        if close:
            await self._close_connection()
        return None if True else None

    async def async_run_serialized_transaction(
        self, runner, *, deadline: Optional[float] = None
    ):
        """Run a service callback through the same write gate and transaction owner."""
        if self._db is None:
            raise RuntimeError("database connection is not available")
        async with self.transaction(deadline=deadline):
            return await runner(self._db)
        return None if True else None

    async def _initialize_schema(self) -> None:
        """Initialize database schema - only runs migrations if schema exists.

        Schema is created by StartupInitializer (sync) during initial setup.
        This method only runs migrations to avoid duplicate schema execution.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='astronomy_cache'"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            _LOGGER.debug("Database schema already exists, running migrations only")
            await self._run_migrations_for_build()
            return
        _LOGGER.warning("Schema not found, initializing from scratch")
        await self._db.executescript(_load_schema_sql())
        await self._db.commit()
        _LOGGER.info("Database schema initialized")
        await self._run_migrations_for_build()

    _MIGRATION_CHAIN_MARKER_PREFIX = "startup_migration_chain:"

    @staticmethod
    def _build_fingerprint() -> str:
        """Identify the deployed build the migration chain was last run for.

        The chain is idempotent, but a dozen of its steps open write
        transactions even when there is nothing to do, and each of those
        waits for whoever else writes the shared database at Home Assistant
        startup. Migrations only need to run once per deployed build, so the
        fingerprint covers the version and the deployed files whose change
        can bring new migrations or schema.
        """
        fingerprint_parts = [f"version={VERSION}"]
        module_path = Path(__file__)
        for source_path in (module_path, module_path.parent / "schema_sql.py"):
            try:
                source_stat = source_path.stat()
                fingerprint_parts.append(
                    f"{source_path.name}={source_stat.st_size}:{source_stat.st_mtime_ns}"
                )
            except OSError:
                fingerprint_parts.append(f"{source_path.name}=missing")
                continue
        return hashlib.sha256("|".join(fingerprint_parts).encode("utf-8")).hexdigest()[
            :16
        ]

    async def _run_migrations_for_build(self) -> None:
        """Run the migration chain once per deployed build, then skip it.

        A restart without a new build takes no write lock here at all, so
        startup no longer waits behind other database writers.
        """
        build_fingerprint = self._build_fingerprint()
        schema_marker = f"{self._MIGRATION_CHAIN_MARKER_PREFIX}{build_fingerprint}"
        if await self._table_exists("migration_applied"):
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = ?",
                (schema_marker,),
            ) as cursor:
                if await cursor.fetchone():
                    _LOGGER.debug(
                        "Migration chain skipped: build %s already migrated",
                        build_fingerprint,
                    )
                    return
        _LOGGER.info("Running database migrations for build %s", build_fingerprint)
        await self._run_migrations()
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        await self._db.execute(
            "DELETE FROM migration_applied WHERE migration_id LIKE ? AND migration_id != ?",
            (f"{self._MIGRATION_CHAIN_MARKER_PREFIX}%", schema_marker),
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO migration_applied (migration_id) VALUES (?)",
            (schema_marker,),
        )
        await self._db.commit()
        _LOGGER.info("Database migrations complete for build %s", build_fingerprint)

    async def _run_migrations(self) -> None:
        """Run database migrations for schema updates. @zara"""
        await self._prepare_shadow_payload_schema_upgrade()
        await self._migrate_hourly_weather_actual_condition()
        await self._migrate_hourly_predictions_v16()
        await self._migrate_shadow_analysis_table_v16()
        await self._migrate_weather_precision_v161()
        await self._migrate_astronomy_panel_groups_v161()
        await self._cleanup_ai_model_weights_blobs_v161()
        await self._migrate_frost_analysis_table_v161()
        await self._migrate_visibility_learning_table_v161()
        await self._migrate_snow_tracking_table_v161()
        await self._migrate_weather_forecast_weather_code_v161()
        await self._migrate_sfml_tfs_forecast_comparison()
        await self._migrate_shadow_pattern_learning_v162()
        await self._migrate_sensor_storage_tables_v1600()
        await self._migrate_raw_prediction_values_v1600()
        await self._migrate_method_performance_learning_v1600()
        await self._migrate_weather_regime_performance_learning_v2026()
        await self._migrate_hourly_weather_actual_snow_frost_v1600()
        await self._migrate_weather_cache_open_meteo_code_v1600()
        await self._migrate_missing_indexes_v1600()
        await self._migrate_peak_power_tracking_v1600()
        await self._migrate_grid_search_model_type_v1600()
        await self._migrate_prediction_weather_diffuse_v1600()
        await self._migrate_prediction_weather_direct_radiation_v1601()
        await self._migrate_ensemble_columns_v1602()
        await self._migrate_ensemble_group_weights_v1603()
        await self._migrate_ridge_meta_normalization_v1604()
        await self._migrate_per_group_v170()
        await self._migrate_shadow_group_relative_percent_v20260805()
        await self._migrate_shadow_observation_columns_v20260827()
        await self._migrate_shadow_system_history_purge_v20260827()
        await self._migrate_shadow_curtailment_observation_repair_v20260923()
        await self._migrate_gold_standard_v1640()
        await self._backfill_astronomy_enrichment_v1640()
        await self._cleanup_null_season_duplicates_v1640()
        await self._migrate_group_method_performance_v1640()
        await self._selfheal_stale_snow_v1642()
        await self._goldstandard_reset_v1642()
        await self._migrate_ai_v300()
        await self._migrate_weather_mlp()
        await self._migrate_weather_cache_snowfall_rain()
        await self._migrate_weather_radiation_ensemble_v20260816()
        await self._migrate_eod_duration_v1710()
        await self._migrate_eod_completed_at()
        await self._migrate_daily_summary_evaluation_fields_v1730()
        await self._migrate_daily_statistics_evaluation_fields_v1730()
        await self._migrate_daily_summaries_unique_date()
        await self._migrate_daily_summary_evaluation_diagnostics_v2000()
        await self._backfill_daily_summary_clean_evaluation_v2000()
        await self._migrate_seasonal_archive_v1684()
        await self._migrate_cusum_bias_correction_v1720()
        await self._migrate_drift_null_season_uniqueness_v20260805()
        await self._migrate_tfs_columns()
        await self._migrate_operational_reforecast_tables_v2010()
        await self._migrate_operational_reforecast_settings_v2011()
        await self._migrate_panel_group_topology_epochs_v20260511()
        await self._migrate_actual_live_state_v20260619()
        await self._migrate_panel_group_learning_exclusion_repair_v20260626()
        await self._migrate_emergency_outage_quarantine_v20260627()
        await self._migrate_learning_quarantine_consistency_v20260715()
        await self._migrate_scheduled_task_state_v20260715()
        await self._migrate_ensemble_shadow_evaluation_v20260714()
        await self._migrate_ai_model_governance_v20260727()
        await self._migrate_ai_model_cycle_evaluation_v42()
        await self._migrate_ai_model_reaction_v20260804()
        await self._migrate_runtime_schema_ownership_v20260802()
        await self._migrate_model_candidate_shadow_compression_v20260813()
        await self._migrate_model_candidate_retention_v20260813()
        await self._migrate_hourly_weather_actual_precipitation_semantics_v20260819()
        await self._migrate_weather_radiation_snapshots_v20260822()
        await self._migrate_weather_forecast_usage_v20260823()
        await self._migrate_weather_expert_learning_quarantine_v20260824()
        await self._migrate_weather_ghi_local_prior_v20260824()
        await self._ensure_ghi_local_prior_until_rolling_ready()
        await self._migrate_raw_physics_kwh_v20260826()
        await self._migrate_weather_visibility_source_v20260827()
        await self._migrate_weather_forecast_correction_terminal_kalman_v20260827()
        await self._migrate_physics_calibration_telemetry_v20260830()
        await self._migrate_poa_contract_stamp_v20260927()
        await self._migrate_rearm_learning_days_v20261004()
        await self._migrate_raw_damp_pending_v20261006()
        await self._migrate_ghi_coherence_cap_gate_v20260904()
        await self._migrate_ai_gate_status_v20260905()
        await self._migrate_drop_dni_seasonal_tables_v20260917()
        await self._migrate_artifact_aesgcm_v1()
        await self._migrate_threshold_code_v1()

    async def _migrate_threshold_code_v1(self) -> None:
        """Move drift and shadow thresholds into code and drop their columns."""
        await self.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        migration_row = await self.fetchone(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?",
            (THRESHOLD_CODE_MIGRATION_ID,),
        )
        if migration_row:
            return
        await self.execute("PRAGMA secure_delete = ON")
        await self._rebuild_shadow_pattern_config_without_thresholds()
        await self._drop_drift_response_config()
        await self.execute(
            "INSERT OR IGNORE INTO migration_applied (migration_id) VALUES (?)",
            (THRESHOLD_CODE_MIGRATION_ID,),
        )

    async def _rebuild_shadow_pattern_config_without_thresholds(self) -> None:
        if not await self._table_exists("shadow_pattern_config"):
            return
        column_names = set(await self._table_column_names("shadow_pattern_config"))
        if not column_names.intersection(_SHADOW_THRESHOLD_COLUMNS):
            return
        async with self.transaction():
            await self.execute(
                "CREATE TABLE shadow_pattern_config_new (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       total_days_learned INTEGER DEFAULT 0,\n                       total_hours_learned INTEGER DEFAULT 0,\n                       last_learning_date DATE,\n                       patterns_detected INTEGER DEFAULT 0,\n                       fixed_obstructions_detected INTEGER DEFAULT 0,\n                       version TEXT DEFAULT '1.0',\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )",
                auto_commit=False,
            )
            await self.execute(
                "INSERT INTO shadow_pattern_config_new\n                       (id, total_days_learned, total_hours_learned, last_learning_date,\n                        patterns_detected, fixed_obstructions_detected, version, updated_at)\n                   SELECT id, total_days_learned, total_hours_learned, last_learning_date,\n                          patterns_detected, fixed_obstructions_detected, version, updated_at\n                   FROM shadow_pattern_config",
                auto_commit=False,
            )
            await self.execute("DROP TABLE shadow_pattern_config", auto_commit=False)
            await self.execute(
                "ALTER TABLE shadow_pattern_config_new RENAME TO shadow_pattern_config",
                auto_commit=False,
            )
        return None if True else None

    async def _drop_drift_response_config(self) -> None:
        if not await self._table_exists("drift_response_config"):
            return
        async with self.transaction():
            await self.execute(
                "DROP TABLE IF EXISTS drift_response_config", auto_commit=False
            )
        return None if True else None

    async def protect_stored_artifacts(self) -> None:
        """Seal any remaining plaintext artifacts. Safe to repeat."""
        if self._db is None:
            return
        if not artifact_crypto.is_bound():
            note_unbound_artifact_key("artifact protection")
            return
        await self._db.execute("PRAGMA secure_delete = ON")
        await self._protect_artifact_rows()

    async def _ensure_candidate_artifact_input_size_column(self) -> None:
        """Add the cleartext input_size column on databases created before it."""
        if not await self._table_exists("ai_model_candidate_artifacts"):
            return
        if "input_size" in await self._table_column_names(
            "ai_model_candidate_artifacts"
        ):
            return
        alter_statement = (
            "ALTER TABLE ai_model_candidate_artifacts ADD COLUMN input_size INTEGER"
        )
        await self.execute(alter_statement)

    async def _migrate_artifact_aesgcm_v1(self) -> None:
        """One-time seal of model artifacts, feature contract, and policies."""
        await self._ensure_candidate_artifact_input_size_column()
        if not artifact_crypto.is_bound():
            note_unbound_artifact_key("artifact protection migration")
            return
        await self.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        migration_row = await self.fetchone(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?",
            (ARTIFACT_PROTECTION_MIGRATION_ID,),
        )
        if migration_row:
            return
        await self.protect_stored_artifacts()
        await self.execute(
            "INSERT OR IGNORE INTO migration_applied (migration_id) VALUES (?)",
            (ARTIFACT_PROTECTION_MIGRATION_ID,),
        )

    async def _protect_artifact_rows(self) -> None:
        """Rewrite protected columns in batches and restore schema triggers."""
        await self._collapse_legacy_coefficients()
        await self._seal_json_column(
            "ai_model_candidate_artifacts",
            ("candidate_id", "model_type"),
            ("weights_json",),
            "ai_model_candidate_artifacts_append_only_update",
            include_column_in_identity=False,
        )
        await self._seal_json_column(
            "ai_training_runs",
            ("run_id",),
            ("config_json",),
            "ai_training_runs_append_only_update",
        )
        for table_name, update_trigger in (
            ("ai_model_evaluations", "ai_model_evaluations_append_only_update"),
            ("ai_model_evaluations_v2", "ai_model_evaluations_v2_append_only_update"),
        ):
            await self._seal_json_column(
                table_name,
                ("evaluation_id",),
                ("evaluation_config_json", "decision_json", "reason_codes_json"),
                update_trigger,
            )
        await self._seal_model_weight_rows()
        await self._seal_shadow_run_snapshots()

    async def _seal_json_column(
        self,
        table: str,
        id_columns: Sequence[str],
        payload_columns: Sequence[str],
        update_trigger: str,
        *,
        include_column_in_identity: bool = True,
    ) -> None:
        """Drop the append-only update trigger, seal a batch, then restore it."""
        if not await self._table_exists(table):
            return
        unsealed_payload_filter = " OR ".join(
            (
                f"substr({column_name}, 1, 1) IN ('{{', '[')"
                for column_name in payload_columns
            )
        )
        selected_columns = ", ".join((*id_columns, *payload_columns))
        while True:
            async with self.transaction():
                await self.execute(
                    f"DROP TRIGGER IF EXISTS {update_trigger}", auto_commit=False
                )
                rows = await self.fetchall(
                    f"SELECT {selected_columns} FROM {table}\n                         WHERE {unsealed_payload_filter}\n                         LIMIT {ARTIFACT_SEAL_BATCH}"
                )
                for row in rows:
                    identity_values = [
                        row[field_index] for field_index in range(len(id_columns))
                    ]
                    assignments = []
                    sealed_parameters = []
                    for column_index, column_name in enumerate(payload_columns):
                        stored_payload = row[len(id_columns) + column_index]
                        if (
                            not isinstance(stored_payload, str)
                            or stored_payload[:1] not in "{["
                        ):
                            continue
                        assignments.append(f"{column_name} = ?")
                        payload_identity_parts = list(identity_values)
                        if include_column_in_identity:
                            payload_identity_parts.append(column_name)
                        sealed_parameters.append(
                            seal_protected_text(
                                stored_payload,
                                table,
                                protected_identity(*payload_identity_parts),
                            )
                        )
                        if (
                            table == "ai_model_candidate_artifacts"
                            and column_name == "weights_json"
                        ):
                            assignments.append("input_size = ?")
                            sealed_parameters.append(
                                artifact_cleartext_input_size(stored_payload)
                            )
                    if not assignments:
                        continue
                    sealed_parameters.extend(identity_values)
                    identity_filter = " AND ".join(
                        (f"{column_name} = ?" for column_name in id_columns)
                    )
                    await self.execute(
                        f"UPDATE {table} SET {', '.join(assignments)} WHERE {identity_filter}",
                        tuple(sealed_parameters),
                        auto_commit=False,
                    )
                    continue
                await self._restore_schema_trigger(update_trigger, auto_commit=False)
            if len(rows) < ARTIFACT_SEAL_BATCH:
                if table == "ai_model_candidate_artifacts":
                    await self._copy_candidate_model_input_size(update_trigger)
                return

    async def _copy_candidate_model_input_size(self, update_trigger: str) -> None:
        """Give sidecar rows the model input_size already read from plaintext."""
        async with self.transaction():
            await self.execute(
                f"DROP TRIGGER IF EXISTS {update_trigger}", auto_commit=False
            )
            await self.execute(
                "UPDATE ai_model_candidate_artifacts\n                      SET input_size = (\n                          SELECT sibling.input_size\n                            FROM ai_model_candidate_artifacts AS sibling\n                           WHERE sibling.candidate_id =\n                                 ai_model_candidate_artifacts.candidate_id\n                             AND sibling.model_type IN ('ridge', 'lstm')\n                             AND sibling.input_size IS NOT NULL\n                           ORDER BY sibling.model_type\n                           LIMIT 1\n                      )\n                    WHERE input_size IS NULL\n                      AND EXISTS (\n                          SELECT 1\n                            FROM ai_model_candidate_artifacts AS sibling\n                           WHERE sibling.candidate_id =\n                                 ai_model_candidate_artifacts.candidate_id\n                             AND sibling.model_type IN ('ridge', 'lstm')\n                             AND sibling.input_size IS NOT NULL\n                      )",
                auto_commit=False,
            )
            await self._restore_schema_trigger(update_trigger, auto_commit=False)
        return None if True else None

    async def _seal_model_weight_rows(self) -> None:
        """Seal leftover plaintext in ai_model_weights.weight_data."""
        if not await self._table_exists("ai_model_weights"):
            return
        while True:
            rows = await self.fetchall(
                f"SELECT weight_type, weight_data FROM ai_model_weights\n                    WHERE substr(weight_data, 1, 1) IN ('{{', '[')\n                    LIMIT {ARTIFACT_SEAL_BATCH}"
            )
            if not rows:
                return
            async with self.transaction():
                for weight_type, cleartext_weights in rows:
                    if (
                        not isinstance(cleartext_weights, str)
                        or cleartext_weights[:1] not in "{["
                    ):
                        continue
                    sealed_payload = seal_protected_text(
                        cleartext_weights, "ai_model_weights", str(weight_type)
                    )
                    await self.execute(
                        "UPDATE ai_model_weights\n                              SET weight_data = ?, updated_at = ?\n                            WHERE weight_type = ?",
                        (sealed_payload, SafeDateTimeUtil.now(), weight_type),
                        auto_commit=False,
                    )
            if len(rows) < ARTIFACT_SEAL_BATCH:
                return

    async def _collapse_legacy_coefficients(self) -> None:
        """Replace per-coefficient REAL rows with one sealed blob per artifact."""
        if await self._table_exists("ai_lstm_weights"):
            legacy_weights_row = await self.fetchone(
                "SELECT 1 FROM ai_lstm_weights\n                    WHERE weight_type != ? LIMIT 1",
                (ARTIFACT_COEFFICIENT_SENTINEL,),
            )
            if legacy_weights_row:
                if await self._open_coefficient_blob("lstm") is _NO_COEFFICIENT_BLOB:
                    legacy_weights = await self._load_lstm_weights(legacy_only=True)
                    if legacy_weights is None:
                        legacy_weights = await self._grouped_named_weights(
                            "ai_lstm_weights"
                        )
                    if legacy_weights:
                        await self._store_coefficient_blob("lstm", legacy_weights)
                    await self.execute("DELETE FROM ai_lstm_weights")
                    await self.execute(
                        "INSERT INTO ai_lstm_weights\n                       (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                        (ARTIFACT_COEFFICIENT_SENTINEL, 0, 0.0),
                    )
        if await self._table_exists("ai_ridge_weights"):
            if await self._open_coefficient_blob("ridge") is _NO_COEFFICIENT_BLOB:
                legacy_weights = await self._load_ridge_weights(legacy_only=True)
                if legacy_weights:
                    await self._store_coefficient_blob("ridge", legacy_weights)
                    await self.execute("DELETE FROM ai_ridge_weights")
                    if await self._table_exists("ai_ridge_meta"):
                        await self.execute(
                            "UPDATE ai_ridge_meta\n                                  SET feature_means_json = NULL,\n                                      feature_stds_json = NULL\n                                WHERE id = 1"
                        )
        if await self._table_exists("ai_weather_mlp_weights"):
            legacy_weights_row = await self.fetchone(
                "SELECT 1 FROM ai_weather_mlp_weights LIMIT 1"
            )
            if legacy_weights_row:
                if (
                    await self._open_coefficient_blob("weather_mlp")
                    is _NO_COEFFICIENT_BLOB
                ):
                    legacy_weights = await self.load_weather_mlp_weights()
                    if legacy_weights is None:
                        legacy_weights = await self._grouped_named_weights(
                            "ai_weather_mlp_weights"
                        )
                    if legacy_weights:
                        await self._store_coefficient_blob(
                            "weather_mlp", legacy_weights
                        )
                    await self.execute("DELETE FROM ai_weather_mlp_weights")
        if await self._table_exists("ai_seasonal_archive_weights"):
            archive_rows = await self.fetchall(
                "SELECT DISTINCT season, archive_type\n                     FROM ai_seasonal_archive_weights"
            )
            for season, archive_type in archive_rows:
                artifact_kind = f"seasonal:{season}:{archive_type}"
                if (
                    await self._open_coefficient_blob(artifact_kind)
                    is _NO_COEFFICIENT_BLOB
                ):
                    legacy_weights = await self.load_seasonal_archive(
                        season, archive_type
                    )
                    if legacy_weights:
                        await self._store_coefficient_blob(
                            artifact_kind, legacy_weights
                        )
                await self.execute(
                    "DELETE FROM ai_seasonal_archive_weights\n                        WHERE season = ? AND archive_type = ?",
                    (season, archive_type),
                )
        return None if True else None

    async def _grouped_named_weights(self, table: str) -> Optional[Dict[str, Any]]:
        """Keep unreconstructable coefficient rows inside the sealed blob."""
        rows = await self.fetchall(
            f"SELECT weight_type, weight_index, weight_value\n                  FROM {table}\n                 WHERE weight_type != ?\n                 ORDER BY weight_type, weight_index",
            (ARTIFACT_COEFFICIENT_SENTINEL,),
        )
        if not rows:
            return
        weights_by_type = {}
        for weight_type, flat_values, field_value in rows:
            weights_by_type.setdefault(str(weight_type), []).append(float(field_value))
        return weights_by_type

    async def _seal_shadow_run_snapshots(self) -> None:
        """Seal shadow policy and input snapshots, widening the codec check."""
        if not await self._table_exists("ai_model_shadow_runs"):
            return
        schema_sql_row = await self.fetchone(
            "SELECT sql FROM sqlite_master\n                WHERE type = 'table' AND name = 'ai_model_shadow_runs'"
        )
        if (
            schema_sql_row
            and schema_sql_row[0]
            and ("aesgcm-v1" not in schema_sql_row[0])
        ):
            await self._rebuild_shadow_run_table()
        else:
            await self._seal_shadow_runs_in_place()
        await self._restore_schema_triggers("ai_model_shadow_runs")
        await self._restore_schema_indexes("ai_model_shadow_runs")

    async def _seal_shadow_runs_in_place(self) -> None:
        """Same batch shape as shadow compaction: drop trigger, rewrite, restore."""
        skipped_run_ids = set()
        while True:
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_runs_append_only_update",
                    auto_commit=False,
                )
                exclusion_filter = ""
                query_parameters = [SHADOW_JSON_CODEC_AESGCM]
                if skipped_run_ids:
                    exclusion_filter = (
                        " AND shadow_run_id NOT IN ("
                        + ", ".join(("?" for unused_item in skipped_run_ids))
                        + ")"
                    )
                    query_parameters.extend(sorted(skipped_run_ids))
                rows = await self.fetchall(
                    f"SELECT shadow_run_id, input_snapshot_json, input_snapshot_blob,\n                              input_snapshot_hash, policy_snapshot_json,\n                              policy_snapshot_blob, policy_snapshot_hash, snapshot_codec\n                         FROM ai_model_shadow_runs\n                        WHERE (snapshot_codec != ?\n                           OR length(input_snapshot_json) > 0\n                           OR length(policy_snapshot_json) > 0)\n                          {exclusion_filter}\n                        ORDER BY shadow_run_id\n                        LIMIT {ARTIFACT_SEAL_BATCH}",
                    tuple(query_parameters),
                )
                if not rows:
                    await self._restore_schema_trigger(
                        "ai_model_shadow_runs_append_only_update", auto_commit=False
                    )
                    return
                for row in rows:
                    sealed_payload = self._sealed_shadow_payloads(row)
                    if sealed_payload is None:
                        skipped_run_ids.add(str(row[0]))
                        continue
                    input_blob, policy_blob = sealed_payload
                    await self.execute(
                        "UPDATE ai_model_shadow_runs\n                              SET input_snapshot_json = '',\n                                  policy_snapshot_json = '',\n                                  input_snapshot_blob = ?,\n                                  policy_snapshot_blob = ?,\n                                  snapshot_codec = ?\n                            WHERE shadow_run_id = ?",
                        (input_blob, policy_blob, SHADOW_JSON_CODEC_AESGCM, row[0]),
                        auto_commit=False,
                    )
                await self._restore_schema_trigger(
                    "ai_model_shadow_runs_append_only_update", auto_commit=False
                )
            if len(rows) < ARTIFACT_SEAL_BATCH:
                return

    def _sealed_shadow_payloads(self, row: Sequence[Any]) -> Optional[tuple]:
        """Return sealed input and policy blobs, or None when a row cannot be read."""
        run_id = str(row[0])
        try:
            input_text = self._shadow_source_text(
                row[1], row[2], row[7], run_id, "input_snapshot"
            )
            policy_text = self._shadow_source_text(
                row[4], row[5], row[7], run_id, "policy_snapshot"
            )
        except (UnicodeDecodeError, ValueError, zlib.error):
            _LOGGER.warning(
                "Shadow snapshot %s could not be sealed and was left unchanged", run_id
            )
            return
        return (
            seal_shadow_snapshot(input_text, run_id, "input_snapshot"),
            seal_shadow_snapshot(policy_text, run_id, "policy_snapshot"),
        )

    @staticmethod
    def _shadow_source_text(
        plain_value: Any, blob: Any, codec: Any, run_id: str, kind: str
    ) -> str:
        """Recover the canonical snapshot text before it is sealed."""
        if codec == SHADOW_JSON_CODEC_AESGCM or artifact_crypto.is_envelope(blob):
            cleartext_bytes = artifact_crypto.open_bytes(
                blob, "ai_model_shadow_runs", protected_identity(run_id, kind), "shadow"
            )
            if cleartext_bytes is None:
                raise ValueError("sealed shadow snapshot is unreadable")
            return cleartext_bytes.decode("utf-8")
        if codec == SHADOW_JSON_CODEC_ZLIB and blob is not None:
            return zlib.decompress(bytes(blob)).decode("utf-8")
        if isinstance(plain_value, str):
            return plain_value
        raise ValueError("shadow snapshot text is missing")

    async def _rebuild_shadow_run_table(self) -> None:
        """Recreate ai_model_shadow_runs so its codec CHECK accepts aesgcm-v1."""
        schema_sql = self._read_schema_sql()
        create_table_sql = self._schema_create_table(schema_sql, "ai_model_shadow_runs")
        create_table_sql = create_table_sql.replace(
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_runs",
            "CREATE TABLE ai_model_shadow_runs_new",
            1,
        )
        await self._db.execute("PRAGMA foreign_keys = OFF")
        await self.execute("DROP TABLE IF EXISTS ai_model_shadow_runs_new")
        await self.execute(create_table_sql)
        new_column_names = await self._table_column_names("ai_model_shadow_runs_new")
        source_column_names = await self._table_column_names("ai_model_shadow_runs")
        run_id = ""
        while True:
            rows = await self.fetchall(
                f"SELECT * FROM ai_model_shadow_runs\n                    WHERE shadow_run_id > ?\n                    ORDER BY shadow_run_id\n                    LIMIT {ARTIFACT_SEAL_BATCH}",
                (run_id,),
            )
            if not rows:
                break
            async with self.transaction():
                for row in rows:
                    source_values = {
                        source_column_names[field_index]: row[field_index]
                        for field_index in range(
                            min(len(source_column_names), len(row))
                        )
                    }
                    sealed_payload = self._sealed_shadow_payloads(
                        (
                            source_values.get("shadow_run_id"),
                            source_values.get("input_snapshot_json"),
                            source_values.get("input_snapshot_blob"),
                            source_values.get("input_snapshot_hash"),
                            source_values.get("policy_snapshot_json"),
                            source_values.get("policy_snapshot_blob"),
                            source_values.get("policy_snapshot_hash"),
                            source_values.get("snapshot_codec"),
                        )
                    )
                    if sealed_payload is not None:
                        source_values["input_snapshot_json"] = ""
                        source_values["policy_snapshot_json"] = ""
                        source_values["input_snapshot_blob"] = sealed_payload[0]
                        source_values["policy_snapshot_blob"] = sealed_payload[1]
                        source_values["snapshot_codec"] = SHADOW_JSON_CODEC_AESGCM
                    insertion_columns = [
                        column_name
                        for column_name in new_column_names
                        if column_name in source_values
                    ]
                    placeholders = ", ".join(("?" for unused_item in insertion_columns))
                    await self.execute(
                        f"INSERT INTO ai_model_shadow_runs_new\n                            ({', '.join(insertion_columns)})\n                            VALUES ({placeholders})",
                        tuple(
                            (
                                source_values[column_name]
                                for column_name in insertion_columns
                            )
                        ),
                        auto_commit=False,
                    )
                    continue
            run_id = str(rows[-1][source_column_names.index("shadow_run_id")])
        await self._drop_triggers_referencing("ai_model_shadow_runs")
        async with self.transaction():
            await self.execute("DROP TABLE ai_model_shadow_runs", auto_commit=False)
            await self.execute(
                "ALTER TABLE ai_model_shadow_runs_new RENAME TO ai_model_shadow_runs",
                auto_commit=False,
            )
        await self._restore_triggers_referencing("ai_model_shadow_runs")
        await self._db.execute("PRAGMA foreign_keys = ON")

    async def _table_column_names(self, table: str) -> list[str]:
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        return [str(row[1]) for row in rows]

    async def _drop_triggers_referencing(self, table_name: str) -> None:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master\n                WHERE type = 'trigger' AND sql LIKE ?",
            (f"%{table_name}%",),
        )
        for (trigger_name,) in rows:
            await self.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

    async def _restore_triggers_referencing(self, table_name: str) -> None:
        schema_sql = self._read_schema_sql()
        async with self.transaction():
            for schema_statement in schema_triggers_referencing(schema_sql, table_name):
                trigger_name = schema_statement.split("BEFORE", 1)[0].split()[-1]
                await self.execute(
                    f"DROP TRIGGER IF EXISTS {trigger_name}", auto_commit=False
                )
                await self.execute(schema_statement, auto_commit=False)
        return None if True else None

    async def _restore_schema_triggers(self, table_name: str) -> None:
        schema_sql = self._read_schema_sql()
        trigger_statements = schema_trigger_statements(schema_sql, table_name)
        async with self.transaction():
            for schema_statement in trigger_statements:
                trigger_name = schema_statement.split("BEFORE", 1)[0].split()[-1]
                await self.execute(
                    f"DROP TRIGGER IF EXISTS {trigger_name}", auto_commit=False
                )
            for schema_statement in trigger_statements:
                await self.execute(schema_statement, auto_commit=False)
        return None if True else None

    async def _restore_schema_indexes(self, table_name: str) -> None:
        schema_sql = self._read_schema_sql()
        scan_offset = 0
        async with self.transaction():
            while True:
                statement_start = schema_sql.find("CREATE INDEX", scan_offset)
                if statement_start < 0:
                    return
                statement_end = schema_sql.find(";", statement_start)
                schema_statement = schema_sql[
                    statement_start : statement_end + 1
                ].strip()
                if re.search(f"\\bON {re.escape(table_name)}\\b", schema_statement):
                    await self.execute(schema_statement, auto_commit=False)
                scan_offset = statement_end + 1
        return None if True else None

    async def _restore_schema_trigger(
        self, trigger_name: str, *, auto_commit: bool
    ) -> None:
        schema_sql = self._read_schema_sql()
        trigger_prefix = f"CREATE TRIGGER IF NOT EXISTS {trigger_name}"
        statement_start = schema_sql.find(trigger_prefix)
        if statement_start < 0:
            trigger_prefix = f"CREATE TRIGGER {trigger_name}"
            statement_start = schema_sql.find(trigger_prefix)
        if statement_start < 0:
            raise RuntimeError(f"schema trigger missing: {trigger_name}")
        trigger_end = schema_sql.find("END;", statement_start)
        if trigger_end < 0:
            raise RuntimeError(f"schema trigger is incomplete: {trigger_name}")
        schema_statement = schema_sql[
            statement_start : trigger_end + len("END;")
        ].strip()
        await self.execute(schema_statement, auto_commit=auto_commit)

    @staticmethod
    def _read_schema_sql() -> str:
        return _load_schema_sql()

    @staticmethod
    def _schema_create_table(schema_sql: str, table_name: str) -> str:
        table_prefix = f"CREATE TABLE IF NOT EXISTS {table_name}"
        statement_start = schema_sql.find(table_prefix)
        if statement_start < 0:
            raise RuntimeError(f"schema table missing: {table_name}")
        return schema_sql[
            statement_start : schema_sql.find(";", statement_start) + 1
        ].strip()

    async def _migrate_raw_physics_kwh_v20260826(self) -> None:
        """Persist uncalibrated physics kWh beside calibrated physics_kwh."""
        for table_name in ("prediction_panel_groups", "ops_prediction_panel_groups"):
            async with self._db.execute(f"PRAGMA table_info({table_name})") as cursor:
                column_names = {row[1] for row in await cursor.fetchall()}
            if "raw_physics_kwh" not in column_names:
                await self._db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN raw_physics_kwh REAL"
                )
        await self._db.commit()

    async def _migrate_weather_visibility_source_v20260827(self) -> None:
        """Persist visibility provenance on cache and corrected forecast rows."""
        for table_name in ("weather_forecast", "weather_cache_open_meteo"):
            async with self._db.execute(f"PRAGMA table_info({table_name})") as cursor:
                column_names = {row[1] for row in await cursor.fetchall()}
            if "visibility_source" not in column_names:
                await self._db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN visibility_source TEXT"
                )
        await self._db.commit()

    async def _migrate_weather_forecast_correction_terminal_kalman_v20260827(
        self,
    ) -> None:
        """Persist the delivered Kalman delta on correction snapshots."""
        async with self._db.execute(
            "PRAGMA table_info(weather_forecast_correction_snapshots)"
        ) as cursor:
            column_names = {row[1] for row in await cursor.fetchall()}
        if "kalman_applied" not in column_names:
            await self._db.execute(
                "ALTER TABLE weather_forecast_correction_snapshots ADD COLUMN kalman_applied BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "kalman_bias" not in column_names:
            await self._db.execute(
                "ALTER TABLE weather_forecast_correction_snapshots ADD COLUMN kalman_bias REAL NOT NULL DEFAULT 0.0"
            )
        await self._db.commit()
        return

    async def _migrate_ghi_coherence_cap_gate_v20260904(self) -> None:
        """Persist cap eligibility on snapshots and strategy-cap observability."""
        async with self._db.execute(
            "PRAGMA table_info(weather_forecast_correction_snapshots)"
        ) as cursor:
            prediction_columns = {row[1] for row in await cursor.fetchall()}
        if (
            prediction_columns
            and "ghi_coherence_cap_eligible" not in prediction_columns
        ):
            await self._db.execute(
                "ALTER TABLE weather_forecast_correction_snapshots ADD COLUMN ghi_coherence_cap_eligible INTEGER"
            )
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            shadow_prediction_columns = {row[1] for row in await cursor.fetchall()}
        if shadow_prediction_columns:
            if "ghi_before_cap" not in shadow_prediction_columns:
                await self._db.execute(
                    "ALTER TABLE hourly_predictions ADD COLUMN ghi_before_cap REAL"
                )
            if "ghi_after_cap" not in shadow_prediction_columns:
                await self._db.execute(
                    "ALTER TABLE hourly_predictions ADD COLUMN ghi_after_cap REAL"
                )
            if "ghi_cap_applied" not in shadow_prediction_columns:
                await self._db.execute(
                    "ALTER TABLE hourly_predictions ADD COLUMN ghi_cap_applied INTEGER"
                )
        await self._db.commit()
        return

    async def _migrate_drop_dni_seasonal_tables_v20260917(self) -> None:
        """Drop the legacy DNI tracker and seasonal adjuster tables.

        Both learners lost their post-training hooks in the v42 candidate
        flow and never had a consumer since; nothing reads these tables.
        Read-only when the tables are already gone, so it costs no lock.
        """
        obsolete_tables = ("ai_dni_history", "ai_dni_tracker", "ai_seasonal_factors")
        placeholders = ",".join(("?" for unused_item in obsolete_tables))
        async with self._db.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
            obsolete_tables,
        ) as cursor:
            existing_tables = [row[0] for row in await cursor.fetchall()]
        if not existing_tables:
            return
        for table_name in obsolete_tables:
            if table_name in existing_tables:
                await self._db.execute(f"DROP TABLE IF EXISTS {table_name}")
        await self._db.commit()
        _LOGGER.info(
            "Migration: dropped legacy DNI/seasonal tables %s",
            ", ".join(existing_tables),
        )
        return

    async def _migrate_ai_gate_status_v20260905(self) -> None:
        """Create the one-row last-known gate readiness status table."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS ai_gate_status (\n                   id INTEGER PRIMARY KEY CHECK(id = 1),\n                   candidate_id TEXT,\n                   eligible_days INTEGER,\n                   required_days INTEGER,\n                   policy_eligible_days INTEGER,\n                   eligible_common_hours INTEGER,\n                   wait_reason TEXT,\n                   candidate_created_at TIMESTAMP,\n                   calendar_day_limit INTEGER,\n                   computed_at TIMESTAMP NOT NULL\n               )"
        )
        await self._db.commit()

    async def _migrate_physics_calibration_telemetry_v20260830(self) -> None:
        """Add physics sample ledger, clamp, shape, and GHI geometry diagnosis tables."""
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'physics_calibration_groups'"
        ) as cursor:
            migration_row = await cursor.fetchone()
        if migration_row:
            async with self._db.execute(
                "PRAGMA table_info(physics_calibration_groups)"
            ) as cursor:
                shadow_group_columns = {row[1] for row in await cursor.fetchall()}
            if "learning_contract_version" not in shadow_group_columns:
                await self._db.execute(
                    "ALTER TABLE physics_calibration_groups ADD COLUMN learning_contract_version TEXT DEFAULT 'physics_calibration_legacy_mixed_v0'"
                )
        schema_statements = (
            "CREATE TABLE IF NOT EXISTS physics_calibration_samples (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT,\n                   date DATE NOT NULL,\n                   group_name TEXT NOT NULL,\n                   hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                   physics_ref_kind TEXT NOT NULL CHECK(\n                       physics_ref_kind IN ('forecast_physics', 'ghi_scaled_clearsky')\n                   ),\n                   physics_ref_kwh REAL NOT NULL,\n                   actual_kwh REAL NOT NULL,\n                   unclamped_ratio REAL NOT NULL,\n                   clamped_ratio REAL NOT NULL,\n                   ratio_clamp_hit INTEGER NOT NULL DEFAULT 0\n                       CHECK(ratio_clamp_hit IN (0, 1)),\n                   bucket_name TEXT,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                   UNIQUE(date, group_name, hour)\n               )",
            "CREATE INDEX IF NOT EXISTS idx_physics_calibration_samples_date\n               ON physics_calibration_samples(date)",
            "CREATE TABLE IF NOT EXISTS physics_calibration_clamp_hits (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT,\n                   date DATE NOT NULL,\n                   group_name TEXT NOT NULL,\n                   hour INTEGER CHECK(hour IS NULL OR (hour >= 0 AND hour <= 23)),\n                   bucket_name TEXT,\n                   clamp_kind TEXT NOT NULL CHECK(\n                       clamp_kind IN (\n                           'ratio',\n                           'global_factor',\n                           'hourly_factor',\n                           'bucket_global',\n                           'bucket_hourly'\n                       )\n                   ),\n                   unclamped_value REAL NOT NULL,\n                   clamped_value REAL NOT NULL,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )",
            "CREATE INDEX IF NOT EXISTS idx_physics_calibration_clamp_hits_date\n               ON physics_calibration_clamp_hits(date)",
            "CREATE TABLE IF NOT EXISTS physics_calibration_hourly_shape (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT,\n                   date DATE NOT NULL,\n                   group_name TEXT NOT NULL,\n                   min_hour INTEGER NOT NULL,\n                   max_hour INTEGER NOT NULL,\n                   hour_count INTEGER NOT NULL,\n                   min_factor REAL NOT NULL,\n                   max_factor REAL NOT NULL,\n                   spread REAL NOT NULL,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                   UNIQUE(date, group_name)\n               )",
            "CREATE INDEX IF NOT EXISTS idx_physics_calibration_hourly_shape_date\n               ON physics_calibration_hourly_shape(date)",
            "CREATE TABLE IF NOT EXISTS ghi_sensor_geometry_bins (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT,\n                   computed_on DATE NOT NULL,\n                   elev_bin INTEGER NOT NULL,\n                   az_bin INTEGER NOT NULL,\n                   sample_count INTEGER NOT NULL,\n                   k_avg REAL,\n                   k_p90 REAL,\n                   k_max REAL,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                   UNIQUE(computed_on, elev_bin, az_bin)\n               )",
            "CREATE INDEX IF NOT EXISTS idx_ghi_sensor_geometry_bins_computed\n               ON ghi_sensor_geometry_bins(computed_on)",
            "CREATE TABLE IF NOT EXISTS ghi_sensor_geometry_sectors (\n                   computed_on DATE NOT NULL,\n                   elev_bin INTEGER NOT NULL,\n                   sector TEXT NOT NULL,\n                   sample_count INTEGER NOT NULL,\n                   k_avg REAL,\n                   k_p90 REAL,\n                   k_max REAL,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                   PRIMARY KEY (computed_on, elev_bin, sector)\n               )",
            "CREATE INDEX IF NOT EXISTS idx_ghi_sensor_geometry_sectors_computed\n               ON ghi_sensor_geometry_sectors(computed_on)",
            "CREATE TABLE IF NOT EXISTS ghi_sensor_geometry_daily (\n                   computed_on DATE PRIMARY KEY,\n                   lookback_days INTEGER NOT NULL,\n                   sample_hours INTEGER NOT NULL,\n                   bin_count INTEGER NOT NULL,\n                   overlapping_elev_bins INTEGER NOT NULL,\n                   max_east_west_k_p90_ratio REAL,\n                   diagnosis TEXT NOT NULL,\n                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )",
        )
        for schema_statement in schema_statements:
            await self._db.execute(schema_statement)
        await self._db.commit()

    async def _migrate_poa_contract_stamp_v20260927(self) -> None:
        """Remember when a group was moved onto the POA DNI/IAM contract."""
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'physics_calibration_groups'"
        ) as cursor:
            migration_row = await cursor.fetchone()
        if not migration_row:
            return
        async with self._db.execute(
            "PRAGMA table_info(physics_calibration_groups)"
        ) as cursor:
            column_names = {row[1] for row in await cursor.fetchall()}
        if "poa_contract_stamped_at" in column_names:
            return
        await self._db.execute(
            "ALTER TABLE physics_calibration_groups ADD COLUMN poa_contract_stamped_at TIMESTAMP"
        )
        await self._db.commit()
        return

    async def _migrate_rearm_learning_days_v20261004(self) -> None:
        """Add the post-wipe rearm counter. NULL means no rearm is open."""
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'physics_calibration_groups'"
        ) as cursor:
            migration_row = await cursor.fetchone()
        if not migration_row:
            return
        async with self._db.execute(
            "PRAGMA table_info(physics_calibration_groups)"
        ) as cursor:
            column_names = {row[1] for row in await cursor.fetchall()}
        if "rearm_learning_days" not in column_names:
            await self._db.execute(
                "ALTER TABLE physics_calibration_groups ADD COLUMN rearm_learning_days INTEGER"
            )
        if "rearm_pending_factor" not in column_names:
            await self._db.execute(
                "ALTER TABLE physics_calibration_groups ADD COLUMN rearm_pending_factor REAL"
            )
        await self._db.commit()
        return

    async def _migrate_raw_damp_pending_v20261006(self) -> None:
        """Unpublished raw-forecast day means. NULL once that series is published."""
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'physics_calibration_groups'"
        ) as cursor:
            migration_row = await cursor.fetchone()
        if not migration_row:
            return
        async with self._db.execute(
            "PRAGMA table_info(physics_calibration_groups)"
        ) as cursor:
            column_names = {row[1] for row in await cursor.fetchall()}
        if "raw_damp_pending" in column_names:
            return
        await self._db.execute(
            "ALTER TABLE physics_calibration_groups ADD COLUMN raw_damp_pending TEXT"
        )
        await self._db.commit()
        return

    async def prune_expired_weather_caches(self, today: Optional[date] = None) -> int:
        """Delete weather_cache_* rows older than seven days before today."""
        timestamp_iso = (
            (today or SafeDateTimeUtil.now().date()) - timedelta(days=7)
        ).isoformat()
        deleted_row_count = 0
        async with self.transaction():
            for table_name in (
                "weather_cache_open_meteo",
                "weather_cache_bright_sky",
                "weather_cache_pirate_weather",
                "weather_cache_wttr_in",
            ):
                await self.execute(
                    f"DELETE FROM {table_name} WHERE forecast_date < ?",
                    (timestamp_iso,),
                    auto_commit=False,
                )
                row = await self.fetchone("SELECT changes()")
                deleted_row_count += int((row[0] if row else 0) or 0)
        return deleted_row_count if True else None

    async def _prepare_shadow_payload_schema_upgrade(self) -> None:
        """Remove payload guards that reference columns not migrated yet."""
        payload_guard_specs = (
            (
                "ai_model_shadow_runs",
                "ai_model_shadow_runs_payload_insert_guard",
                {
                    "payload_state",
                    "snapshot_codec",
                    "input_snapshot_blob",
                    "policy_snapshot_blob",
                },
            ),
            (
                "ai_model_shadow_predictions",
                "ai_model_shadow_predictions_payload_insert_guard",
                {
                    "result_codec",
                    "payload_state",
                    "champion_result_blob",
                    "candidate_result_blob",
                },
            ),
        )
        missing_tables = []
        for table_name, trigger_name, required_guard_columns in payload_guard_specs:
            column_names = {
                row[1]
                for row in await (
                    await self._db.execute(f"PRAGMA table_info({table_name})")
                ).fetchall()
            }
            if required_guard_columns.issubset(column_names):
                continue
            trigger_exists_row = await self.fetchone(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger_name,),
            )
            if trigger_exists_row:
                await self._db.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                missing_tables.append(trigger_name)
        if missing_tables:
            await self._db.commit()
            _LOGGER.info(
                "Removed incompatible pre-migration payload guards: %s",
                ", ".join(missing_tables),
            )
        return None if True else None

    async def _migrate_model_candidate_retention_v20260813(self) -> None:
        """Create the complete bounded-evidence lifecycle on clean and upgraded DBs."""
        schema_statements = (
            "CREATE TABLE IF NOT EXISTS ai_model_candidate_terminal_events (\n                   candidate_id TEXT PRIMARY KEY,\n                   terminal_reason TEXT NOT NULL CHECK(terminal_reason IN (\n                       'promoted', 'no_go', 'invalidated', 'superseded',\n                       'incompatible', 'evidence_timeout')),\n                   terminal_at TIMESTAMP NOT NULL,\n                   policy_version TEXT NOT NULL,\n                   details_hash TEXT NOT NULL CHECK(length(details_hash) = 64),\n                   FOREIGN KEY(candidate_id)\n                       REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_retention_receipts (\n                   receipt_id TEXT PRIMARY KEY,\n                   candidate_id TEXT NOT NULL UNIQUE,\n                   terminal_reason TEXT NOT NULL,\n                   policy_version TEXT NOT NULL,\n                   compacted_at TIMESTAMP NOT NULL,\n                   run_count INTEGER NOT NULL CHECK(run_count >= 0),\n                   prediction_count INTEGER NOT NULL CHECK(prediction_count >= 0),\n                   actual_version_count INTEGER NOT NULL\n                       CHECK(actual_version_count >= 0),\n                   payload_count INTEGER NOT NULL CHECK(payload_count >= 0),\n                   payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0),\n                   deleted_run_count INTEGER NOT NULL\n                       CHECK(deleted_run_count >= 0),\n                   deleted_prediction_count INTEGER NOT NULL\n                       CHECK(deleted_prediction_count >= 0),\n                   deleted_actual_version_count INTEGER NOT NULL\n                       CHECK(deleted_actual_version_count >= 0),\n                   deleted_provenance_count INTEGER NOT NULL\n                       CHECK(deleted_provenance_count >= 0),\n                   min_run_date TEXT, max_run_date TEXT,\n                   evidence_root_hash TEXT NOT NULL\n                       CHECK(length(evidence_root_hash) = 64),\n                   FOREIGN KEY(candidate_id)\n                       REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(candidate_id)\n                       REFERENCES ai_model_candidate_terminal_events(candidate_id))",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_candidate_terminal_events_append_only_update\n                   BEFORE UPDATE ON ai_model_candidate_terminal_events\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_terminal_events is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_candidate_terminal_events_append_only_delete\n                   BEFORE DELETE ON ai_model_candidate_terminal_events\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_terminal_events is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_candidate_terminal_events_authorization_guard\n                   BEFORE INSERT ON ai_model_candidate_terminal_events\n                   WHEN sfml_governance_write_authorized(\n                       'terminal', NEW.candidate_id) != 1\n                   BEGIN SELECT RAISE(ABORT,\n                       'unauthorized candidate terminal event'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_evaluations_terminal_event_guard\n                   BEFORE INSERT ON ai_model_evaluations\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                        WHERE terminal.candidate_id = NEW.candidate_id)\n                   BEGIN SELECT RAISE(ABORT,\n                       'candidate lifecycle is terminal'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_evaluations_v2_terminal_event_guard\n                   BEFORE INSERT ON ai_model_evaluations_v2\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                        WHERE terminal.candidate_id = NEW.candidate_id)\n                   BEGIN SELECT RAISE(ABORT,\n                       'candidate lifecycle is terminal'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_shadow_retention_receipts_append_only_update\n                   BEFORE UPDATE ON ai_model_shadow_retention_receipts\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_shadow_retention_receipts is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_shadow_retention_receipts_append_only_delete\n                   BEFORE DELETE ON ai_model_shadow_retention_receipts\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_shadow_retention_receipts is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_shadow_retention_receipts_authorization_guard\n                   BEFORE INSERT ON ai_model_shadow_retention_receipts\n                   WHEN sfml_governance_write_authorized(\n                       'retention', NEW.candidate_id) != 1\n                   BEGIN SELECT RAISE(ABORT,\n                       'unauthorized retention receipt'); END",
        )
        shadow_run_columns = {
            row[1]
            for row in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_runs)")
            ).fetchall()
        }
        shadow_prediction_columns = {
            row[1]
            for row in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_predictions)")
            ).fetchall()
        }
        async with self.transaction():
            for schema_statement in schema_statements:
                await self.execute(schema_statement, auto_commit=False)
            receipt_columns = {
                row[1]
                for row in await (
                    await self._db.execute(
                        "PRAGMA table_info(ai_model_shadow_retention_receipts)"
                    )
                ).fetchall()
            }
            if "payload_state" not in shadow_run_columns:
                await self.execute(
                    "ALTER TABLE ai_model_shadow_runs ADD COLUMN payload_state\n                       TEXT NOT NULL DEFAULT 'hot'\n                       CHECK(payload_state IN ('hot', 'compacted'))",
                    auto_commit=False,
                )
            if "payload_state" not in shadow_prediction_columns:
                await self.execute(
                    "ALTER TABLE ai_model_shadow_predictions\n                       ADD COLUMN payload_state TEXT NOT NULL DEFAULT 'hot'\n                       CHECK(payload_state IN ('hot', 'compacted'))",
                    auto_commit=False,
                )
            for column_name in (
                "actual_version_count",
                "deleted_run_count",
                "deleted_prediction_count",
                "deleted_actual_version_count",
                "deleted_provenance_count",
            ):
                if column_name not in receipt_columns:
                    await self.execute(
                        f"ALTER TABLE ai_model_shadow_retention_receipts\n                            ADD COLUMN {column_name} INTEGER NOT NULL DEFAULT 0\n                            CHECK({column_name} >= 0)",
                        auto_commit=False,
                    )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                       ai_model_shadow_runs_payload_insert_guard\n                   BEFORE INSERT ON ai_model_shadow_runs\n                   WHEN NOT (\n                       NEW.payload_state = 'hot' AND (\n                           (NEW.snapshot_codec = 'plain-json-v1'\n                            AND NEW.input_snapshot_blob IS NULL\n                            AND NEW.policy_snapshot_blob IS NULL)\n                           OR\n                           (NEW.snapshot_codec = 'zlib-json-v1'\n                            AND length(NEW.input_snapshot_json) = 0\n                            AND length(NEW.policy_snapshot_json) = 0\n                            AND NEW.input_snapshot_blob IS NOT NULL\n                            AND NEW.policy_snapshot_blob IS NOT NULL)\n                           OR\n                           (NEW.snapshot_codec = 'aesgcm-v1'\n                            AND length(NEW.input_snapshot_json) = 0\n                            AND length(NEW.policy_snapshot_json) = 0\n                            AND NEW.input_snapshot_blob IS NOT NULL\n                            AND NEW.policy_snapshot_blob IS NOT NULL)))\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid shadow run payload storage'); END",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                       ai_model_shadow_predictions_payload_insert_guard\n                   BEFORE INSERT ON ai_model_shadow_predictions\n                   WHEN NOT (\n                       NEW.payload_state = 'hot' AND (\n                           (NEW.result_codec = 'plain-json-v1'\n                            AND NEW.champion_result_blob IS NULL\n                            AND NEW.candidate_result_blob IS NULL)\n                           OR\n                           (NEW.result_codec = 'zlib-json-v1'\n                            AND length(NEW.champion_result_json) = 0\n                            AND length(NEW.candidate_result_json) = 0\n                            AND NEW.champion_result_blob IS NOT NULL\n                            AND NEW.candidate_result_blob IS NOT NULL)))\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid shadow prediction payload storage'); END",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                       ai_model_shadow_run_provenance_pair_guard\n                   BEFORE INSERT ON ai_model_shadow_run_provenance\n                   WHEN NOT EXISTS (\n                       SELECT 1 FROM ai_model_shadow_runs run\n                        WHERE run.shadow_run_id = NEW.shadow_run_id\n                          AND run.morning_batch_id = NEW.morning_batch_id)\n                   BEGIN SELECT RAISE(ABORT,\n                       'shadow run provenance pair mismatch'); END",
                auto_commit=False,
            )
            await self.execute(
                "DROP TRIGGER IF EXISTS ai_model_candidates_single_unevaluated_guard",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER ai_model_candidates_single_unevaluated_guard\n                   BEFORE INSERT ON ai_model_candidates\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_candidates candidate\n                        WHERE candidate.status = 'candidate'\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                               WHERE terminal.candidate_id = candidate.candidate_id)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_dispositions disposition\n                               WHERE disposition.candidate_id = candidate.candidate_id\n                                 AND NOT EXISTS (\n                                     SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                      WHERE reconsideration.candidate_id =\n                                            disposition.candidate_id\n                                        AND disposition.disposition =\n                                            'migration_superseded'\n                                        AND disposition.replacement_candidate_id =\n                                            reconsideration.keeper_candidate_id\n                                        AND reconsideration.candidate_id !=\n                                            reconsideration.keeper_candidate_id))\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                               WHERE closure.candidate_id = candidate.candidate_id))\n                   BEGIN SELECT RAISE(ABORT,\n                       'unevaluated model candidate already exists'); END",
                auto_commit=False,
            )
        await self.reconcile_model_candidate_terminal_events()
        return

    async def _migrate_ai_model_reaction_v20260804(self) -> None:
        """Create restart-safe drift reaction and candidate disposition storage."""
        evaluation_table_row = await self.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_model_evaluations_v2'"
        )
        schema_statements = (
            "CREATE TABLE IF NOT EXISTS ai_model_retrain_requests (\n                   request_id TEXT PRIMARY KEY, created_at TIMESTAMP NOT NULL,\n                   updated_at TIMESTAMP NOT NULL,\n                   status TEXT NOT NULL CHECK(status IN\n                       ('pending', 'bound', 'completed')),\n                   scope TEXT NOT NULL, drift_type TEXT NOT NULL,\n                   severity TEXT NOT NULL CHECK(severity IN ('warning', 'critical')),\n                   first_event_date TEXT NOT NULL, last_event_date TEXT NOT NULL,\n                   confirmation_days INTEGER NOT NULL, bound_candidate_id TEXT,\n                   superseded_candidate_id TEXT, completion_decision TEXT,\n                   completed_at TIMESTAMP,\n                   FOREIGN KEY(bound_candidate_id)\n                       REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(superseded_candidate_id)\n                       REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_candidate_reconsiderations (\n                   reconsideration_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,\n                   keeper_candidate_id TEXT NOT NULL, created_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(keeper_candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_candidate_reconsideration_closures (\n                   candidate_id TEXT PRIMARY KEY,\n                   replacement_candidate_id TEXT NOT NULL,\n                   created_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(replacement_candidate_id)\n                       REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_retrain_request_events (\n                   request_id TEXT NOT NULL, drift_event_id INTEGER NOT NULL UNIQUE,\n                   linked_at TIMESTAMP NOT NULL,\n                   PRIMARY KEY(request_id, drift_event_id),\n                   FOREIGN KEY(request_id)\n                       REFERENCES ai_model_retrain_requests(request_id),\n                   FOREIGN KEY(drift_event_id) REFERENCES drift_events(id))",
            "CREATE TABLE IF NOT EXISTS ai_model_training_lease (\n                   id INTEGER PRIMARY KEY CHECK(id = 1), owner TEXT NOT NULL,\n                   retrain_request_id TEXT, acquired_at_epoch REAL NOT NULL,\n                   expires_at_epoch REAL NOT NULL,\n                   FOREIGN KEY(retrain_request_id)\n                       REFERENCES ai_model_retrain_requests(request_id))",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_model_retrain_request_open\n                   ON ai_model_retrain_requests(scope, drift_type)\n                   WHERE status IN ('pending', 'bound')",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_dispositions_append_only_update\n                   BEFORE UPDATE ON ai_model_candidate_dispositions\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_dispositions is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_dispositions_append_only_delete\n                   BEFORE DELETE ON ai_model_candidate_dispositions\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_dispositions is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_dispositions_evaluation_guard\n                   BEFORE INSERT ON ai_model_candidate_dispositions\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_evaluations WHERE candidate_id = NEW.candidate_id\n                       UNION ALL\n                       SELECT 1 FROM ai_model_evaluations_v2 WHERE candidate_id = NEW.candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'candidate evaluation already exists; disposition is forbidden'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_dispositions_promotion_guard\n                   BEFORE INSERT ON ai_model_candidate_dispositions\n                   WHEN NEW.disposition = 'superseded_by_promotion' AND (\n                       NEW.replacement_candidate_id IS NULL\n                       OR NEW.replacement_candidate_id = NEW.candidate_id\n                       OR NOT EXISTS (\n                           SELECT 1 FROM ai_model_promotion_events event\n                            WHERE event.candidate_id = NEW.replacement_candidate_id\n                       )\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'promotion disposition requires promotion provenance'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsiderations_append_only_update\n                   BEFORE UPDATE ON ai_model_candidate_reconsiderations\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_reconsiderations is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsiderations_append_only_delete\n                   BEFORE DELETE ON ai_model_candidate_reconsiderations\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_reconsiderations is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsiderations_insert_guard\n                   BEFORE INSERT ON ai_model_candidate_reconsiderations\n                   WHEN NEW.candidate_id = NEW.keeper_candidate_id OR NOT EXISTS (\n                       SELECT 1 FROM ai_model_candidate_dispositions disposition\n                        WHERE disposition.candidate_id = NEW.candidate_id\n                          AND disposition.disposition = 'migration_superseded'\n                          AND disposition.replacement_candidate_id = NEW.keeper_candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid migration candidate reconsideration'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsideration_closures_append_only_update\n                   BEFORE UPDATE ON ai_model_candidate_reconsideration_closures\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_reconsideration_closures is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsideration_closures_append_only_delete\n                   BEFORE DELETE ON ai_model_candidate_reconsideration_closures\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_reconsideration_closures is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_candidate_reconsideration_closures_insert_guard\n                   BEFORE INSERT ON ai_model_candidate_reconsideration_closures\n                   WHEN NEW.candidate_id = NEW.replacement_candidate_id OR NOT EXISTS (\n                       SELECT 1\n                         FROM ai_model_candidate_reconsiderations reconsideration\n                         JOIN ai_model_candidate_dispositions disposition\n                           ON disposition.candidate_id = reconsideration.candidate_id\n                          AND disposition.disposition = 'migration_superseded'\n                          AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                         JOIN ai_model_promotion_events event\n                           ON event.candidate_id = NEW.replacement_candidate_id\n                        WHERE reconsideration.candidate_id = NEW.candidate_id\n                          AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid candidate reconsideration closure'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_evaluations_candidate_terminal_guard\n                   BEFORE INSERT ON ai_model_evaluations\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_candidate_dispositions WHERE candidate_id = NEW.candidate_id\n                       UNION ALL\n                       SELECT 1 FROM ai_model_evaluations WHERE candidate_id = NEW.candidate_id\n                       UNION ALL\n                       SELECT 1 FROM ai_model_evaluations_v2 WHERE candidate_id = NEW.candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'candidate is dispositioned or already evaluated'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_evaluations_v2_candidate_terminal_guard\n                   BEFORE INSERT ON ai_model_evaluations_v2\n                   WHEN EXISTS (\n                       SELECT 1 FROM ai_model_candidate_dispositions WHERE candidate_id = NEW.candidate_id\n                       UNION ALL\n                       SELECT 1 FROM ai_model_evaluations WHERE candidate_id = NEW.candidate_id\n                       UNION ALL\n                       SELECT 1 FROM ai_model_evaluations_v2 WHERE candidate_id = NEW.candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'candidate is dispositioned or already evaluated'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_retrain_request_events_append_only_update\n                   BEFORE UPDATE ON ai_model_retrain_request_events\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_retrain_request_events is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS ai_model_retrain_request_events_append_only_delete\n                   BEFORE DELETE ON ai_model_retrain_request_events\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_retrain_request_events is append-only'); END",
        )
        async with self.transaction():
            disposition_schema_row = await self.fetchone(
                "SELECT sql FROM sqlite_master\n                   WHERE type = 'table'\n                     AND name = 'ai_model_candidate_dispositions'"
            )
            disposition_table_sql = (
                disposition_schema_row[0] if disposition_schema_row else ""
            )
            if not disposition_schema_row:
                await self.execute(
                    "CREATE TABLE ai_model_candidate_dispositions (\n                           candidate_id TEXT PRIMARY KEY,\n                           disposition TEXT NOT NULL CHECK(disposition IN (\n                               'superseded_by_confirmed_drift',\n                               'incompatible_runtime',\n                               'migration_superseded',\n                               'superseded_by_promotion'\n                           )),\n                           replacement_candidate_id TEXT,\n                           retrain_request_id TEXT,\n                           created_at TIMESTAMP NOT NULL,\n                           FOREIGN KEY(candidate_id)\n                               REFERENCES ai_model_candidates(candidate_id))",
                    auto_commit=False,
                )
            elif (
                "incompatible_runtime" not in disposition_table_sql
                or "migration_superseded" not in disposition_table_sql
                or "superseded_by_promotion" not in disposition_table_sql
            ):
                for trigger_name in (
                    "ai_model_candidate_dispositions_append_only_update",
                    "ai_model_candidate_dispositions_append_only_delete",
                    "ai_model_candidate_dispositions_evaluation_guard",
                    "ai_model_candidates_single_unevaluated_guard",
                    "ai_model_evaluations_candidate_terminal_guard",
                    "ai_model_evaluations_v2_candidate_terminal_guard",
                    "ai_model_candidate_reconsiderations_insert_guard",
                    "ai_model_candidate_reconsideration_closures_insert_guard",
                ):
                    await self.execute(
                        f"DROP TRIGGER IF EXISTS {trigger_name}", auto_commit=False
                    )
                await self.execute(
                    "ALTER TABLE ai_model_candidate_dispositions\n                       RENAME TO ai_model_candidate_dispositions_legacy_v20260804",
                    auto_commit=False,
                )
                await self.execute(
                    "CREATE TABLE ai_model_candidate_dispositions (\n                           candidate_id TEXT PRIMARY KEY,\n                           disposition TEXT NOT NULL CHECK(disposition IN (\n                               'superseded_by_confirmed_drift',\n                               'incompatible_runtime',\n                               'migration_superseded',\n                               'superseded_by_promotion'\n                           )),\n                           replacement_candidate_id TEXT,\n                           retrain_request_id TEXT,\n                           created_at TIMESTAMP NOT NULL,\n                           FOREIGN KEY(candidate_id)\n                               REFERENCES ai_model_candidates(candidate_id))",
                    auto_commit=False,
                )
                await self.execute(
                    "INSERT INTO ai_model_candidate_dispositions\n                       (candidate_id, disposition, replacement_candidate_id,\n                        retrain_request_id, created_at)\n                       SELECT candidate_id, disposition, replacement_candidate_id,\n                              retrain_request_id, created_at\n                         FROM ai_model_candidate_dispositions_legacy_v20260804",
                    auto_commit=False,
                )
                await self.execute(
                    "DROP TABLE ai_model_candidate_dispositions_legacy_v20260804",
                    auto_commit=False,
                )
            for schema_statement in schema_statements:
                if not evaluation_table_row:
                    if "ai_model_evaluations_v2" in schema_statement:
                        continue
                await self.execute(schema_statement, auto_commit=False)
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_candidate_dispositions_append_only_update\n                   BEFORE UPDATE ON ai_model_candidate_dispositions\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_dispositions is append-only'); END",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                   ai_model_candidate_dispositions_append_only_delete\n                   BEFORE DELETE ON ai_model_candidate_dispositions\n                   BEGIN SELECT RAISE(ABORT,\n                       'ai_model_candidate_dispositions is append-only'); END",
                auto_commit=False,
            )
            await self.execute(
                "DROP TRIGGER IF EXISTS ai_model_candidates_single_unevaluated_guard",
                auto_commit=False,
            )
            await self.execute(
                "DROP TRIGGER IF EXISTS ai_model_evaluations_candidate_terminal_guard",
                auto_commit=False,
            )
            if evaluation_table_row:
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_evaluations_v2_candidate_terminal_guard",
                    auto_commit=False,
                )
            for table_name in (
                ("ai_model_evaluations", "ai_model_evaluations_v2")
                if evaluation_table_row
                else ("ai_model_evaluations",)
            ):
                await self.execute(
                    f"CREATE TRIGGER {table_name}_candidate_terminal_guard\n                       BEFORE INSERT ON {table_name}\n                       WHEN EXISTS (\n                           SELECT 1 FROM ai_model_candidate_dispositions disposition\n                            WHERE disposition.candidate_id = NEW.candidate_id\n                              AND NOT EXISTS (\n                                  SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                   WHERE reconsideration.candidate_id = disposition.candidate_id\n                                     AND disposition.disposition = 'migration_superseded'\n                                     AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                     AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                              )\n                           UNION ALL\n                           SELECT 1 FROM ai_model_evaluations\n                            WHERE candidate_id = NEW.candidate_id\n                           UNION ALL\n                           SELECT 1 FROM ai_model_evaluations_v2\n                            WHERE candidate_id = NEW.candidate_id\n                           UNION ALL\n                           SELECT 1 FROM ai_model_candidate_reconsideration_closures\n                            WHERE candidate_id = NEW.candidate_id\n                       )\n                       BEGIN SELECT RAISE(ABORT,\n                           'candidate is dispositioned or already evaluated'); END",
                    auto_commit=False,
                )
            await self.execute(
                "CREATE TRIGGER ai_model_candidates_single_unevaluated_guard\n                   BEFORE INSERT ON ai_model_candidates\n                   WHEN EXISTS (\n                       SELECT 1\n                         FROM ai_model_candidates candidate\n                        WHERE candidate.status = 'candidate'\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                               WHERE terminal.candidate_id = candidate.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_dispositions disposition\n                               WHERE disposition.candidate_id = candidate.candidate_id\n                                 AND NOT EXISTS (\n                                     SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                      WHERE reconsideration.candidate_id = disposition.candidate_id\n                                        AND disposition.disposition = 'migration_superseded'\n                                        AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                        AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                                 )\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                               WHERE closure.candidate_id = candidate.candidate_id\n                          )\n                   )\n                   BEGIN\n                       SELECT RAISE(ABORT, 'unevaluated model candidate already exists');\n                   END",
                auto_commit=False,
            )
            await self.execute(
                "DROP TRIGGER IF EXISTS ai_model_candidate_reconsiderations_insert_guard",
                auto_commit=False,
            )
            await self.execute(
                "DROP TRIGGER IF EXISTS ai_model_candidate_reconsideration_closures_insert_guard",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER ai_model_candidate_reconsiderations_insert_guard\n                   BEFORE INSERT ON ai_model_candidate_reconsiderations\n                   WHEN NEW.candidate_id = NEW.keeper_candidate_id OR NOT EXISTS (\n                       SELECT 1 FROM ai_model_candidate_dispositions disposition\n                        WHERE disposition.candidate_id = NEW.candidate_id\n                          AND disposition.disposition = 'migration_superseded'\n                          AND disposition.replacement_candidate_id = NEW.keeper_candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid migration candidate reconsideration'); END",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER ai_model_candidate_reconsideration_closures_insert_guard\n                   BEFORE INSERT ON ai_model_candidate_reconsideration_closures\n                   WHEN NEW.candidate_id = NEW.replacement_candidate_id OR NOT EXISTS (\n                       SELECT 1\n                         FROM ai_model_candidate_reconsiderations reconsideration\n                         JOIN ai_model_candidate_dispositions disposition\n                           ON disposition.candidate_id = reconsideration.candidate_id\n                          AND disposition.disposition = 'migration_superseded'\n                          AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                         JOIN ai_model_promotion_events event\n                           ON event.candidate_id = NEW.replacement_candidate_id\n                        WHERE reconsideration.candidate_id = NEW.candidate_id\n                          AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                   )\n                   BEGIN SELECT RAISE(ABORT,\n                       'invalid candidate reconsideration closure'); END",
                auto_commit=False,
            )
        await self._repair_terminal_bound_model_retrain_requests()

    async def _repair_terminal_bound_model_retrain_requests(self) -> None:
        """Complete stranded bound requests only when immutable terminal facts exist."""
        current_time = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            await self.execute(
                "UPDATE ai_model_retrain_requests AS request\n                  SET status = 'completed', completion_decision = 'NO_GO',\n                      completed_at = ?, updated_at = ?\n                WHERE request.status = 'bound'\n                  AND EXISTS (\n                      SELECT 1 FROM ai_model_evaluations evaluation\n                       WHERE evaluation.candidate_id = request.bound_candidate_id\n                         AND evaluation.decision = 'NO_GO'\n                      UNION ALL\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = request.bound_candidate_id\n                         AND evaluation.decision = 'NO_GO'\n                  )",
                (current_time, current_time),
                auto_commit=False,
            )
            await self.execute(
                "UPDATE ai_model_retrain_requests AS request\n                  SET status = 'completed', completion_decision = 'GO',\n                      completed_at = ?, updated_at = ?\n                WHERE request.status = 'bound'\n                  AND EXISTS (\n                      SELECT 1 FROM ai_active_model_pointer pointer\n                       WHERE pointer.id = 1\n                         AND pointer.active_candidate_id = request.bound_candidate_id\n                  )\n                  AND EXISTS (\n                      SELECT 1 FROM ai_model_evaluations evaluation\n                       WHERE evaluation.candidate_id = request.bound_candidate_id\n                         AND evaluation.decision = 'GO'\n                      UNION ALL\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = request.bound_candidate_id\n                         AND evaluation.decision = 'GO'\n                  )",
                (current_time, current_time),
                auto_commit=False,
            )
        return None if True else None

    async def reconcile_pending_model_candidates(
        self,
        *,
        compatible_candidate_ids: Sequence[str],
        incompatible_candidate_ids: Sequence[str],
    ) -> Optional[str]:
        """Select one verified candidate for shadow evidence collection."""
        compatible_ids = tuple(compatible_candidate_ids)
        incompatible_ids = tuple(incompatible_candidate_ids)
        if (
            len(set(compatible_ids)) != len(compatible_ids)
            or len(set(incompatible_ids)) != len(incompatible_ids)
            or set(compatible_ids) & set(incompatible_ids)
            or any(
                (
                    not isinstance(candidate_id, str) or not candidate_id
                    for candidate_id in [*compatible_ids, *incompatible_ids]
                )
            )
        ):
            raise ValueError("runtime candidate ids must be unique and valid")
        async with self.transaction():
            pending_candidate_rows = await self.fetchall(
                "SELECT candidate.candidate_id\n                     FROM ai_model_candidates candidate\n                    WHERE candidate.status = 'candidate'\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                           WHERE terminal.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_dispositions disposition\n                           WHERE disposition.candidate_id = candidate.candidate_id\n                             AND NOT EXISTS (\n                                 SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                  WHERE reconsideration.candidate_id = disposition.candidate_id\n                                    AND disposition.disposition = 'migration_superseded'\n                                    AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                    AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                             )\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_evaluations evaluation\n                           WHERE evaluation.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                           WHERE evaluation.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                           WHERE closure.candidate_id = candidate.candidate_id\n                      )\n                    ORDER BY candidate.created_at, candidate.candidate_id"
            )
            pending_candidate_ids = {str(row[0]) for row in pending_candidate_rows}
            incompatible_pending_ids = set(incompatible_ids) & pending_candidate_ids
            other_incompatible_ids = set(incompatible_ids) - incompatible_pending_ids
            if other_incompatible_ids:
                disposition_rows = await self.fetchall(
                    "SELECT candidate_id, disposition\n                         FROM ai_model_candidate_dispositions\n                        WHERE candidate_id IN ({})".format(
                        ", ".join(("?" for unused_item in other_incompatible_ids))
                    ),
                    tuple(other_incompatible_ids),
                )
                if set(disposition_rows) != {
                    (candidate_id, "incompatible_runtime")
                    for candidate_id in other_incompatible_ids
                }:
                    raise ValueError("incompatible candidate is not pending")
            if pending_candidate_ids != set(compatible_ids) | incompatible_pending_ids:
                raise ValueError("every pending candidate must be runtime-verified")
            keeper_candidate_id = None
            if compatible_ids:
                placeholders = ", ".join(("?" for unused_item in compatible_ids))
                selected_columns = await self.fetchone(
                    f"SELECT candidate.candidate_id\n                           FROM ai_model_candidates candidate\n                          WHERE candidate.candidate_id IN ({placeholders})\n                          ORDER BY candidate.accuracy DESC NULLS LAST,\n                                   candidate.training_samples DESC,\n                                   candidate.rmse ASC NULLS LAST,\n                                   candidate.created_at ASC, candidate.candidate_id ASC\n                          LIMIT 1",
                    compatible_ids,
                )
                keeper_candidate_id = (
                    str(selected_columns[0]) if selected_columns else None
                )
            created_at = SafeDateTimeUtil.now().isoformat()
            if incompatible_pending_ids:
                await self.executemany(
                    "INSERT INTO ai_model_candidate_dispositions\n                       (candidate_id, disposition, replacement_candidate_id,\n                        retrain_request_id, created_at)\n                       VALUES (?, ?, ?, NULL, ?)",
                    [
                        (
                            candidate_id,
                            "incompatible_runtime",
                            keeper_candidate_id,
                            created_at,
                        )
                        for candidate_id in sorted(incompatible_pending_ids)
                    ],
                )
        if True:
            if True:
                if True:
                    return keeper_candidate_id

    async def _migrate_runtime_schema_ownership_v20260802(self) -> None:
        """Install legacy tables and columns formerly created by runtime callers."""
        async with self._db.execute(
            "PRAGMA table_info(daily_forecast_tracking)"
        ) as cursor:
            column_names = {row[1] for row in await cursor.fetchall()}
        column_definitions = {
            "conservative_planning_forecast_kwh": "REAL",
            "conservative_planning_forecast_updated_at": "TIMESTAMP",
            "conservative_planning_forecast_hours_json": "TEXT",
            "conservative_planning_forecast_panel_groups_json": "TEXT",
            "conservative_planning_forecast_group_totals_json": "TEXT",
        }
        for column_name, column_definition in column_definitions.items():
            if column_name not in column_names:
                await self._db.execute(
                    f"ALTER TABLE daily_forecast_tracking ADD COLUMN {column_name} {column_definition}"
                )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS panel_group_config_snapshot (\n                   group_name TEXT PRIMARY KEY, power_wp REAL NOT NULL,\n                   azimuth REAL NOT NULL, tilt REAL NOT NULL, energy_sensor TEXT)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS repair_tool_audit (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TIMESTAMP NOT NULL,\n                   operation TEXT NOT NULL, dry_run BOOLEAN NOT NULL DEFAULT TRUE,\n                   payload_json TEXT NOT NULL)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS error_log (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TIMESTAMP NOT NULL,\n                   source TEXT NOT NULL, error_type TEXT NOT NULL,\n                   classification TEXT NOT NULL, message TEXT NOT NULL, context TEXT)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_log_timestamp\n                   ON error_log(timestamp DESC)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS retrospective_forecasts (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL,\n                   hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                   predicted_kwh REAL NOT NULL, simulation_time TIMESTAMP NOT NULL,\n                   created_at TIMESTAMP NOT NULL,\n                   UNIQUE(date, hour, simulation_time))"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_retrospective_forecasts_date_hour\n                   ON retrospective_forecasts(date, hour)"
        )
        await self._db.commit()
        return

    async def _migrate_model_candidate_shadow_compression_v20260813(self) -> None:
        """Compact immutable shadow JSON payloads without discarding evidence."""
        shadow_run_columns = {
            row[1]
            for row in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_runs)")
            ).fetchall()
        }
        shadow_prediction_columns = {
            row[1]
            for row in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_predictions)")
            ).fetchall()
        }
        run_payload_columns = {
            "input_snapshot_blob": "BLOB",
            "policy_snapshot_blob": "BLOB",
            "payload_state": "TEXT NOT NULL DEFAULT 'hot' CHECK(payload_state IN ('hot', 'compacted'))",
            "snapshot_codec": "TEXT NOT NULL DEFAULT 'plain-json-v1' CHECK(snapshot_codec IN ('plain-json-v1', 'zlib-json-v1', 'aesgcm-v1'))",
        }
        prediction_payload_columns = {
            "champion_result_blob": "BLOB",
            "candidate_result_blob": "BLOB",
            "payload_state": "TEXT NOT NULL DEFAULT 'hot' CHECK(payload_state IN ('hot', 'compacted'))",
            "result_codec": "TEXT NOT NULL DEFAULT 'plain-json-v1' CHECK(result_codec IN ('plain-json-v1', 'zlib-json-v1'))",
        }
        compressed_run_count = 0
        compressed_run_bytes = 0
        async with self.transaction():
            for column_name, column_definition in run_payload_columns.items():
                if column_name not in shadow_run_columns:
                    await self.execute(
                        f"ALTER TABLE ai_model_shadow_runs ADD COLUMN {column_name} {column_definition}",
                        auto_commit=False,
                    )
            for column_name, column_definition in prediction_payload_columns.items():
                if column_name not in shadow_prediction_columns:
                    await self.execute(
                        f"ALTER TABLE ai_model_shadow_predictions ADD COLUMN {column_name} {column_definition}",
                        auto_commit=False,
                    )
        while True:
            run_batch_count = 0
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_runs_append_only_update",
                    auto_commit=False,
                )
                shadow_run_rows = await self.fetchall(
                    "SELECT shadow_run_id, input_snapshot_json, input_snapshot_hash,\n                              policy_snapshot_json, policy_snapshot_hash\n                         FROM ai_model_shadow_runs\n                        WHERE payload_state = 'hot' AND (\n                              snapshot_codec = 'plain-json-v1'\n                           OR input_snapshot_blob IS NULL\n                           OR policy_snapshot_blob IS NULL)\n                        ORDER BY shadow_run_id LIMIT 8"
                )
                run_updates = []
                for row in shadow_run_rows:
                    decode_shadow_json(row[1], None, SHADOW_JSON_CODEC_PLAIN, row[2])
                    decode_shadow_json(row[3], None, SHADOW_JSON_CODEC_PLAIN, row[4])
                    run_updates.append(
                        (
                            compress_shadow_json(row[1]),
                            compress_shadow_json(row[3]),
                            SHADOW_JSON_CODEC_ZLIB,
                            row[0],
                        )
                    )
                if run_updates:
                    await self.executemany(
                        "UPDATE ai_model_shadow_runs\n                              SET input_snapshot_json = '', policy_snapshot_json = '',\n                                  input_snapshot_blob = ?, policy_snapshot_blob = ?,\n                                  snapshot_codec = ?\n                            WHERE shadow_run_id = ?",
                        run_updates,
                    )
                    run_batch_count = len(run_updates)
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_runs_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_runs\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_runs is append-only'); END",
                    auto_commit=False,
                )
            compressed_run_count += run_batch_count
            if not run_batch_count:
                break
        while True:
            prediction_batch_count = 0
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_predictions_append_only_update",
                    auto_commit=False,
                )
                prediction_rows = await self.fetchall(
                    "SELECT shadow_prediction_id, champion_result_json,\n                              champion_result_hash, candidate_result_json,\n                              candidate_result_hash\n                         FROM ai_model_shadow_predictions\n                        WHERE payload_state = 'hot' AND (\n                              result_codec = 'plain-json-v1'\n                           OR champion_result_blob IS NULL\n                           OR candidate_result_blob IS NULL)\n                        ORDER BY shadow_prediction_id LIMIT 128"
                )
                prediction_updates = []
                for row in prediction_rows:
                    decode_shadow_json(row[1], None, SHADOW_JSON_CODEC_PLAIN, row[2])
                    decode_shadow_json(row[3], None, SHADOW_JSON_CODEC_PLAIN, row[4])
                    prediction_updates.append(
                        (
                            compress_shadow_json(row[1]),
                            compress_shadow_json(row[3]),
                            SHADOW_JSON_CODEC_ZLIB,
                            row[0],
                        )
                    )
                if prediction_updates:
                    await self.executemany(
                        "UPDATE ai_model_shadow_predictions\n                              SET champion_result_json = '', candidate_result_json = '',\n                                  champion_result_blob = ?, candidate_result_blob = ?,\n                                  result_codec = ?\n                            WHERE shadow_prediction_id = ?",
                        prediction_updates,
                    )
                    prediction_batch_count = len(prediction_updates)
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_predictions_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_predictions\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_predictions is append-only'); END",
                    auto_commit=False,
                )
            compressed_run_bytes += prediction_batch_count
            if not prediction_batch_count:
                break
        if compressed_run_count or compressed_run_bytes:
            _LOGGER.info(
                "Model candidate shadow storage compacted: runs=%d predictions=%d",
                compressed_run_count,
                compressed_run_bytes,
            )
        if True:
            return None

    async def _migrate_ai_model_governance_v20260727(self) -> None:
        """Create append-only model-governance storage without touching legacy weights."""
        schema_statements = (
            "CREATE TABLE IF NOT EXISTS ai_training_runs (\n                   run_id TEXT PRIMARY KEY, created_at TIMESTAMP NOT NULL,\n                   dataset_fingerprint TEXT NOT NULL, training_config_hash TEXT NOT NULL,\n                   code_source_hash TEXT NOT NULL, sample_count INTEGER NOT NULL,\n                   config_json TEXT NOT NULL, schema_version TEXT NOT NULL,\n                   eligibility_version TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ai_training_sample_provenance (\n                   id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,\n                   sample_index INTEGER NOT NULL, output_index INTEGER NOT NULL,\n                   forecast_prediction_id TEXT NOT NULL, forecast_anchor TEXT NOT NULL,\n                   target_prediction_id TEXT NOT NULL, target_anchor TEXT NOT NULL,\n                   prediction_lineage_id TEXT, group_lineage_id TEXT, group_name TEXT,\n                   target_value REAL NOT NULL, feature_digest TEXT NOT NULL,\n                   provenance_kind TEXT NOT NULL, eligibility_version TEXT NOT NULL,\n                   schema_version TEXT NOT NULL,\n                   FOREIGN KEY(run_id) REFERENCES ai_training_runs(run_id),\n                   UNIQUE(run_id, sample_index, output_index))",
            "CREATE TABLE IF NOT EXISTS ai_model_candidates (\n                   candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,\n                   created_at TIMESTAMP NOT NULL, active_model TEXT NOT NULL,\n                   training_samples INTEGER NOT NULL, accuracy REAL, rmse REAL,\n                   artifact_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',\n                   FOREIGN KEY(run_id) REFERENCES ai_training_runs(run_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_candidate_artifacts (\n                   candidate_id TEXT NOT NULL, model_type TEXT NOT NULL,\n                   weights_json TEXT NOT NULL, weights_hash TEXT NOT NULL,\n                   input_size INTEGER,\n                   PRIMARY KEY(candidate_id, model_type),\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_active_model_pointer (\n                   id INTEGER PRIMARY KEY CHECK(id = 1), active_candidate_id TEXT,\n                   last_known_good_candidate_id TEXT, promoted_at TIMESTAMP,\n                   FOREIGN KEY(active_candidate_id) REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(last_known_good_candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_evaluations (\n                   evaluation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,\n                   created_at TIMESTAMP NOT NULL, candidate_artifact_hash TEXT NOT NULL,\n                   champion_artifact_hash TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,\n                   evaluation_config_hash TEXT NOT NULL,\n                   decision TEXT NOT NULL CHECK(decision IN ('GO', 'NO_GO')),\n                   reason_codes_json TEXT NOT NULL, eligible_hour_count INTEGER NOT NULL,\n                   eligible_independent_day_count INTEGER NOT NULL,\n                   evaluation_config_json TEXT NOT NULL, decision_json TEXT NOT NULL,\n                   decision_hash TEXT NOT NULL,\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_evaluation_samples (\n                   evaluation_id TEXT NOT NULL, sample_index INTEGER NOT NULL,\n                   target_date TEXT NOT NULL, target_hour INTEGER NOT NULL\n                       CHECK(target_hour BETWEEN 0 AND 23),\n                   actual REAL NOT NULL, champion_prediction REAL NOT NULL,\n                   candidate_prediction REAL NOT NULL, clean_eligible INTEGER NOT NULL\n                       CHECK(clean_eligible IN (0, 1)),\n                   forecast_anchor TEXT NOT NULL, input_fingerprint TEXT NOT NULL,\n                   policy_fingerprint TEXT NOT NULL, weather_regime TEXT, horizon TEXT,\n                   PRIMARY KEY(evaluation_id, sample_index),\n                   UNIQUE(evaluation_id, target_date, target_hour),\n                   FOREIGN KEY(evaluation_id) REFERENCES ai_model_evaluations(evaluation_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_runs (\n                   shadow_run_id TEXT PRIMARY KEY, morning_batch_id TEXT NOT NULL UNIQUE,\n                   run_date TEXT NOT NULL, candidate_id TEXT NOT NULL,\n                   forecast_anchor TEXT NOT NULL, champion_artifact_hash TEXT NOT NULL,\n                   candidate_artifact_hash TEXT NOT NULL, input_snapshot_json TEXT NOT NULL,\n                   input_snapshot_hash TEXT NOT NULL, policy_snapshot_json TEXT NOT NULL,\n                   policy_snapshot_hash TEXT NOT NULL, contract_version TEXT NOT NULL,\n                   created_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(morning_batch_id)\n                       REFERENCES ensemble_shadow_batches(morning_batch_id),\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_predictions (\n                   shadow_prediction_id TEXT PRIMARY KEY, shadow_run_id TEXT NOT NULL,\n                   source_prediction_id TEXT NOT NULL, target_date TEXT NOT NULL,\n                   target_hour INTEGER NOT NULL CHECK(target_hour BETWEEN 0 AND 23),\n                   horizon TEXT NOT NULL, champion_prediction REAL NOT NULL,\n                   candidate_prediction REAL NOT NULL, weather_regime TEXT,\n                   input_fingerprint TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,\n                   champion_result_json TEXT NOT NULL, champion_result_hash TEXT NOT NULL,\n                   candidate_result_json TEXT NOT NULL, candidate_result_hash TEXT NOT NULL,\n                   UNIQUE(shadow_run_id, target_date, target_hour),\n                   FOREIGN KEY(shadow_run_id) REFERENCES ai_model_shadow_runs(shadow_run_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_actual_versions (\n                   actual_version_id TEXT PRIMARY KEY, shadow_prediction_id TEXT NOT NULL,\n                   evaluation_version INTEGER NOT NULL, actual_fingerprint TEXT NOT NULL,\n                   evaluated_at TIMESTAMP NOT NULL, actual_kwh REAL,\n                   actual_measured_at TIMESTAMP, clean_eligible INTEGER NOT NULL\n                       CHECK(clean_eligible IN (0, 1)),\n                   exclusion_reason TEXT, evaluation_status TEXT NOT NULL,\n                   UNIQUE(shadow_prediction_id, actual_fingerprint),\n                   UNIQUE(shadow_prediction_id, evaluation_version),\n                   FOREIGN KEY(shadow_prediction_id)\n                       REFERENCES ai_model_shadow_predictions(shadow_prediction_id))",
            "CREATE INDEX IF NOT EXISTS idx_ai_training_provenance_run ON ai_training_sample_provenance(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_evaluations_candidate\n               ON ai_model_evaluations(candidate_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_shadow_candidate\n               ON ai_model_shadow_runs(candidate_id, run_date)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_shadow_source\n               ON ai_model_shadow_predictions(source_prediction_id)",
        )
        async with self.transaction():
            for schema_statement in schema_statements:
                await self.execute(schema_statement, auto_commit=False)
            for table_name in (
                "ai_training_runs",
                "ai_training_sample_provenance",
                "ai_model_candidates",
                "ai_model_candidate_artifacts",
                "ai_model_evaluations",
                "ai_model_evaluation_samples",
                "ai_model_shadow_runs",
                "ai_model_shadow_predictions",
                "ai_model_shadow_actual_versions",
            ):
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_update\n                         BEFORE UPDATE ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
            table_identity_filters = {
                "ai_training_runs": "run_id = NEW.run_id",
                "ai_training_sample_provenance": "id = NEW.id OR (run_id = NEW.run_id AND sample_index = NEW.sample_index AND output_index = NEW.output_index)",
                "ai_model_candidates": "candidate_id = NEW.candidate_id",
                "ai_model_candidate_artifacts": "candidate_id = NEW.candidate_id AND model_type = NEW.model_type",
                "ai_model_evaluations": "evaluation_id = NEW.evaluation_id",
                "ai_model_evaluation_samples": "(evaluation_id = NEW.evaluation_id AND sample_index = NEW.sample_index) OR (evaluation_id = NEW.evaluation_id AND target_date = NEW.target_date AND target_hour = NEW.target_hour)",
                "ai_model_shadow_runs": "shadow_run_id = NEW.shadow_run_id OR morning_batch_id = NEW.morning_batch_id",
                "ai_model_shadow_predictions": "shadow_prediction_id = NEW.shadow_prediction_id OR (shadow_run_id = NEW.shadow_run_id AND target_date = NEW.target_date AND target_hour = NEW.target_hour)",
                "ai_model_shadow_actual_versions": "actual_version_id = NEW.actual_version_id OR (shadow_prediction_id = NEW.shadow_prediction_id AND actual_fingerprint = NEW.actual_fingerprint) OR (shadow_prediction_id = NEW.shadow_prediction_id AND evaluation_version = NEW.evaluation_version)",
            }
            for table_name, unsealed_payload_filter in table_identity_filters.items():
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_insert_guard\n                         BEFORE INSERT ON {table_name}\n                         WHEN EXISTS (SELECT 1 FROM {table_name} WHERE {unsealed_payload_filter})\n                         BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_delete\n                         BEFORE DELETE ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS ai_model_candidates_single_unevaluated_guard\n                   BEFORE INSERT ON ai_model_candidates\n                   WHEN EXISTS (\n                       SELECT 1\n                         FROM ai_model_candidates candidate\n                        WHERE candidate.status = 'candidate'\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                               WHERE terminal.candidate_id = candidate.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1\n                                FROM ai_model_evaluations evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1\n                                FROM ai_model_evaluations_v2 evaluation\n                               WHERE evaluation.candidate_id = candidate.candidate_id\n                          )\n                   )\n                   BEGIN\n                       SELECT RAISE(ABORT, 'unevaluated model candidate already exists');\n                   END",
                auto_commit=False,
            )
        return None if True else None

    async def _migrate_ai_model_cycle_evaluation_v42(self) -> None:
        """Create V42 cycle provenance and evaluation storage without legacy rewrites."""
        schema_statements = (
            "CREATE TABLE IF NOT EXISTS ensemble_shadow_batch_provenance (\n                   morning_batch_id TEXT PRIMARY KEY,\n                   forecast_cycle TEXT NOT NULL CHECK(forecast_cycle IN\n                       ('post_midnight', 'pre_sunrise', 'catchup', 'manual')),\n                   trigger_kind TEXT NOT NULL CHECK(trigger_kind IN\n                       ('scheduled', 'catchup', 'manual')),\n                   promotion_eligible INTEGER NOT NULL CHECK(promotion_eligible IN (0, 1)),\n                   contract_version TEXT NOT NULL CHECK(\n                       contract_version = 'ensemble_shadow_provenance_v2'),\n                   created_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(morning_batch_id)\n                       REFERENCES ensemble_shadow_batches(morning_batch_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_run_provenance (\n                   shadow_run_id TEXT PRIMARY KEY, morning_batch_id TEXT NOT NULL UNIQUE,\n                   forecast_cycle TEXT NOT NULL CHECK(forecast_cycle IN\n                       ('post_midnight', 'pre_sunrise', 'catchup', 'manual')),\n                   trigger_kind TEXT NOT NULL CHECK(trigger_kind IN\n                       ('scheduled', 'catchup', 'manual')),\n                   promotion_eligible INTEGER NOT NULL CHECK(promotion_eligible IN (0, 1)),\n                   contract_version TEXT NOT NULL CHECK(\n                       contract_version = 'model_candidate_shadow_v2'),\n                   created_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(shadow_run_id) REFERENCES ai_model_shadow_runs(shadow_run_id),\n                   FOREIGN KEY(morning_batch_id)\n                       REFERENCES ensemble_shadow_batches(morning_batch_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_evaluations_v2 (\n                   evaluation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,\n                   created_at TIMESTAMP NOT NULL, candidate_artifact_hash TEXT NOT NULL,\n                   champion_artifact_hash TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,\n                   evaluation_config_hash TEXT NOT NULL,\n                   decision TEXT NOT NULL CHECK(decision IN ('GO', 'NO_GO')),\n                   reason_codes_json TEXT NOT NULL, eligible_hour_count INTEGER NOT NULL,\n                   eligible_independent_day_count INTEGER NOT NULL,\n                   evaluation_config_json TEXT NOT NULL, decision_json TEXT NOT NULL,\n                   decision_hash TEXT NOT NULL, evaluator_contract_version TEXT NOT NULL\n                       CHECK(evaluator_contract_version = 'cycle_aware_model_promotion_v2'),\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_evaluation_samples_v2 (\n                   evaluation_id TEXT NOT NULL, sample_index INTEGER NOT NULL,\n                   target_date TEXT NOT NULL,\n                   target_hour INTEGER NOT NULL CHECK(target_hour BETWEEN 0 AND 23),\n                   forecast_cycle TEXT NOT NULL CHECK(forecast_cycle IN\n                       ('post_midnight', 'pre_sunrise')),\n                   shadow_run_id TEXT NOT NULL, shadow_prediction_id TEXT NOT NULL,\n                   actual_version_id TEXT NOT NULL, actual REAL NOT NULL,\n                   champion_prediction REAL NOT NULL, candidate_prediction REAL NOT NULL,\n                   clean_eligible INTEGER NOT NULL CHECK(clean_eligible IN (0, 1)),\n                   forecast_anchor TEXT NOT NULL, input_fingerprint TEXT NOT NULL,\n                   policy_fingerprint TEXT NOT NULL, weather_regime TEXT, horizon TEXT,\n                   PRIMARY KEY(evaluation_id, sample_index),\n                   UNIQUE(evaluation_id, target_date, target_hour, forecast_cycle),\n                   FOREIGN KEY(evaluation_id) REFERENCES ai_model_evaluations_v2(evaluation_id),\n                   FOREIGN KEY(shadow_run_id) REFERENCES ai_model_shadow_runs(shadow_run_id),\n                   FOREIGN KEY(shadow_prediction_id)\n                       REFERENCES ai_model_shadow_predictions(shadow_prediction_id),\n                   FOREIGN KEY(actual_version_id)\n                       REFERENCES ai_model_shadow_actual_versions(actual_version_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_evaluation_invalidations (\n                   evaluation_id TEXT PRIMARY KEY,\n                   invalidation_reason TEXT NOT NULL CHECK(\n                       invalidation_reason = 'actual_evidence_superseded'),\n                   invalidated_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(evaluation_id)\n                       REFERENCES ai_model_evaluations_v2(evaluation_id))",
            "CREATE TABLE IF NOT EXISTS ai_model_promotion_events (\n                   promotion_event_id TEXT PRIMARY KEY,\n                   candidate_id TEXT NOT NULL UNIQUE,\n                   evaluation_id TEXT NOT NULL UNIQUE,\n                   candidate_artifact_hash TEXT NOT NULL,\n                   champion_artifact_hash TEXT NOT NULL,\n                   promoted_at TIMESTAMP NOT NULL,\n                   FOREIGN KEY(candidate_id) REFERENCES ai_model_candidates(candidate_id),\n                   FOREIGN KEY(evaluation_id) REFERENCES ai_model_evaluations_v2(evaluation_id))",
            "CREATE INDEX IF NOT EXISTS idx_ensemble_shadow_batch_provenance_context\n               ON ensemble_shadow_batch_provenance(forecast_cycle, trigger_kind, promotion_eligible)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_shadow_run_provenance_context\n               ON ai_model_shadow_run_provenance(forecast_cycle, trigger_kind, promotion_eligible)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_evaluations_v2_candidate\n               ON ai_model_evaluations_v2(candidate_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ai_model_evaluation_samples_v2_provenance\n               ON ai_model_evaluation_samples_v2(shadow_run_id, shadow_prediction_id, actual_version_id)",
        )
        governed_tables = (
            "ensemble_shadow_batch_provenance",
            "ai_model_shadow_run_provenance",
            "ai_model_evaluations_v2",
            "ai_model_evaluation_samples_v2",
            "ai_model_evaluation_invalidations",
            "ai_model_promotion_events",
        )
        table_identity_filters = {
            "ensemble_shadow_batch_provenance": "morning_batch_id = NEW.morning_batch_id",
            "ai_model_shadow_run_provenance": "shadow_run_id = NEW.shadow_run_id OR morning_batch_id = NEW.morning_batch_id",
            "ai_model_evaluations_v2": "evaluation_id = NEW.evaluation_id",
            "ai_model_evaluation_invalidations": "evaluation_id = NEW.evaluation_id",
            "ai_model_promotion_events": "promotion_event_id = NEW.promotion_event_id",
            "ai_model_evaluation_samples_v2": "(evaluation_id = NEW.evaluation_id AND sample_index = NEW.sample_index) OR (evaluation_id = NEW.evaluation_id AND target_date = NEW.target_date AND target_hour = NEW.target_hour AND forecast_cycle = NEW.forecast_cycle)",
        }
        async with self.transaction():
            for schema_statement in schema_statements:
                await self.execute(schema_statement, auto_commit=False)
            for table_name in governed_tables:
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_update\n                         BEFORE UPDATE ON {table_name}\n                         BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_delete\n                         BEFORE DELETE ON {table_name}\n                         BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_append_only_insert_guard\n                         BEFORE INSERT ON {table_name}\n                         WHEN EXISTS (SELECT 1 FROM {table_name} WHERE {table_identity_filters[table_name]})\n                         BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END",
                    auto_commit=False,
                )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS ai_model_promotion_events_insert_guard\n                     BEFORE INSERT ON ai_model_promotion_events\n                     WHEN NOT EXISTS (\n                         SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                         JOIN ai_model_candidates candidate\n                           ON candidate.candidate_id = evaluation.candidate_id\n                          WHERE evaluation.evaluation_id = NEW.evaluation_id\n                            AND evaluation.candidate_id = NEW.candidate_id\n                            AND evaluation.decision = 'GO'\n                            AND evaluation.candidate_artifact_hash = NEW.candidate_artifact_hash\n                            AND evaluation.champion_artifact_hash = NEW.champion_artifact_hash\n                            AND candidate.artifact_hash = NEW.candidate_artifact_hash\n                            AND NOT EXISTS (\n                                SELECT 1 FROM ai_model_evaluation_invalidations invalidation\n                                 WHERE invalidation.evaluation_id = evaluation.evaluation_id\n                            )\n                     )\n                     BEGIN SELECT RAISE(ABORT, 'invalid model promotion event'); END",
                auto_commit=False,
            )
            await self.execute(
                "CREATE TRIGGER IF NOT EXISTS\n                       ai_model_promotion_events_authorization_guard\n                   BEFORE INSERT ON ai_model_promotion_events\n                   WHEN sfml_governance_write_authorized(\n                       'promotion', NEW.candidate_id) != 1\n                   BEGIN SELECT RAISE(ABORT,\n                       'unauthorized model promotion event'); END",
                auto_commit=False,
            )
            async with self._authorize_governance_write("promotion", "*"):
                await self.execute(
                    "INSERT OR IGNORE INTO ai_model_promotion_events\n                       (promotion_event_id, candidate_id, evaluation_id,\n                        candidate_artifact_hash, champion_artifact_hash, promoted_at)\n                   SELECT 'migration:' || evaluation.evaluation_id,\n                          evaluation.candidate_id, evaluation.evaluation_id,\n                          evaluation.candidate_artifact_hash,\n                          evaluation.champion_artifact_hash,\n                          COALESCE(pointer.promoted_at, evaluation.created_at)\n                     FROM ai_active_model_pointer pointer\n                     JOIN ai_model_evaluations_v2 evaluation\n                       ON evaluation.candidate_id IN (\n                           pointer.active_candidate_id,\n                           pointer.last_known_good_candidate_id\n                       )\n                      AND evaluation.decision = 'GO'\n                      AND evaluation.created_at = (\n                          SELECT MAX(candidate_evaluation.created_at)\n                            FROM ai_model_evaluations_v2 candidate_evaluation\n                           WHERE candidate_evaluation.candidate_id = evaluation.candidate_id\n                             AND candidate_evaluation.decision = 'GO'\n                      )\n                     JOIN ai_model_candidates candidate\n                       ON candidate.candidate_id = evaluation.candidate_id\n                      AND candidate.artifact_hash = evaluation.candidate_artifact_hash\n                    WHERE pointer.id = 1\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_evaluation_invalidations invalidation\n                           WHERE invalidation.evaluation_id = evaluation.evaluation_id\n                      )",
                    auto_commit=False,
                )
            for operation_name in ("INSERT", "UPDATE"):
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS\n                         ai_active_model_pointer_promotion_guard_{operation_name.lower()}\n                         BEFORE {operation_name} ON ai_active_model_pointer\n                         WHEN (NEW.active_candidate_id IS NOT NULL AND NOT EXISTS (\n                                   SELECT 1 FROM ai_model_promotion_events event\n                                    WHERE event.candidate_id = NEW.active_candidate_id\n                               )) OR (NEW.last_known_good_candidate_id IS NOT NULL AND NOT EXISTS (\n                                   SELECT 1 FROM ai_model_promotion_events event\n                                    WHERE event.candidate_id = NEW.last_known_good_candidate_id\n                               ))\n                         BEGIN SELECT RAISE(ABORT,\n                             'active model pointer requires promotion provenance'); END",
                    auto_commit=False,
                )
        if True:
            return

    async def _migrate_learning_quarantine_consistency_v20260715(self) -> None:
        """Enforce learning exclusion for every quarantined historical hour."""
        migration_started = False
        try:
            await self._db.execute("SAVEPOINT learning_quarantine_consistency")
            migration_started = True
            hourly_update_cursor = await self._db.execute(
                "UPDATE hourly_predictions\n                   SET exclude_from_learning = 1,\n                       exclude_from_clean_evaluation = 1\n                   WHERE (COALESCE(actual_backfill_blocked, 0) = 1\n                          OR data_quarantine_reason IS NOT NULL)\n                     AND (COALESCE(exclude_from_learning, 0) = 0\n                          OR COALESCE(exclude_from_clean_evaluation, 0) = 0)"
            )
            group_update_cursor = await self._db.execute(
                "UPDATE prediction_panel_groups\n                   SET exclude_from_learning_group = 1,\n                       exclusion_reason_group = COALESCE(\n                           NULLIF(exclusion_reason_group, ?),\n                           (SELECT COALESCE(\n                               NULLIF(hp.data_quarantine_reason, ?),\n                               ?\n                            )\n                            FROM hourly_predictions hp\n                            WHERE hp.prediction_id = prediction_panel_groups.prediction_id)\n                       )\n                   WHERE COALESCE(exclude_from_learning_group, 0) = 0\n                     AND prediction_id IN (\n                         SELECT hp.prediction_id\n                         FROM hourly_predictions hp\n                         WHERE COALESCE(hp.actual_backfill_blocked, 0) = 1\n                            OR hp.data_quarantine_reason IS NOT NULL\n                     )",
                ("", "", "actual_backfill_blocked"),
            )
            await self._db.execute("RELEASE SAVEPOINT learning_quarantine_consistency")
            migration_started = False
            await self._db.commit()
            updated_hour_count = max(hourly_update_cursor.rowcount, 0)
            updated_group_count = max(group_update_cursor.rowcount, 0)
            if updated_hour_count or updated_group_count:
                _LOGGER.info(
                    "Repaired learning quarantine consistency: hourly=%d, groups=%d",
                    updated_hour_count,
                    updated_group_count,
                )
        except Exception:
            if migration_started:
                await self._db.execute(
                    "ROLLBACK TO SAVEPOINT learning_quarantine_consistency"
                )
                await self._db.execute(
                    "RELEASE SAVEPOINT learning_quarantine_consistency"
                )
            raise
        return None if True else None

    async def _migrate_scheduled_task_state_v20260715(self) -> None:
        """Create persistent completion state for catch-up capable tasks."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_task_state (\n                   task_key TEXT PRIMARY KEY,\n                   completed_period TEXT,\n                   completed_at TIMESTAMP,\n                   last_attempt_at TIMESTAMP\n               )"
        )
        await self._db.commit()

    async def _migrate_ensemble_shadow_evaluation_v20260714(self) -> None:
        """Add append-only provenance for ensemble-versus-physics shadow runs."""
        try:
            await self._db.execute("SAVEPOINT ensemble_shadow_migration")
            async with self._db.execute(
                "PRAGMA table_info(hourly_predictions)"
            ) as cursor:
                column_names = {schema_row[1] for schema_row in await cursor.fetchall()}
            if "morning_batch_id" not in column_names:
                await self._db.execute(
                    "ALTER TABLE hourly_predictions ADD COLUMN morning_batch_id TEXT"
                )
            async with self._db.execute(
                "SELECT name FROM sqlite_master\n                   WHERE type = 'table' AND name = 'ensemble_shadow_evaluations'"
            ) as cursor:
                evaluation_table_row = await cursor.fetchone()
            if evaluation_table_row:
                async with self._db.execute(
                    "PRAGMA table_info(ensemble_shadow_evaluations)"
                ) as cursor:
                    provenance_columns = {
                        schema_row[1] for schema_row in await cursor.fetchall()
                    }
                required_fields = {
                    "target_date",
                    "target_hour",
                    "prediction_kwh",
                    "target_datetime",
                    "morning_batch_id",
                    "shadow_evaluable",
                    "forecast_created_at",
                    "decision_reason_json",
                    "shadow_evaluation_id",
                    "source_prediction_id",
                    "shadow_gate_triggered",
                    "production_prediction_kwh",
                }
                missing_provenance_columns = required_fields - provenance_columns
                if missing_provenance_columns:
                    raise RuntimeError(
                        "incompatible ensemble_shadow_evaluations schema: "
                        + ", ".join(sorted(missing_provenance_columns))
                    )
                if "target_utc_epoch" not in provenance_columns:
                    async with self._db.execute(
                        "SELECT COUNT(*) FROM ensemble_shadow_evaluations"
                    ) as cursor:
                        evaluation_count_row = await cursor.fetchone()
                    if evaluation_count_row and evaluation_count_row[0]:
                        raise RuntimeError(
                            "cannot add target_utc_epoch to populated shadow provenance"
                        )
                    await self._db.execute(
                        "ALTER TABLE ensemble_shadow_evaluations ADD COLUMN target_utc_epoch INTEGER"
                    )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ensemble_shadow_batches (\n                       morning_batch_id TEXT PRIMARY KEY,\n                       run_date DATE NOT NULL,\n                       status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),\n                       created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       completed_at TIMESTAMP,\n                       failed_at TIMESTAMP,\n                       last_resumed_at TIMESTAMP\n                   )"
            )
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ensemble_shadow_batches_one_running\n                   ON ensemble_shadow_batches(run_date) WHERE status = 'running'"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_hourly_predictions_morning_batch\n                   ON hourly_predictions(morning_batch_id)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ensemble_shadow_evaluations (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       shadow_evaluation_id TEXT NOT NULL UNIQUE,\n                       morning_batch_id TEXT NOT NULL,\n                       source_prediction_id TEXT NOT NULL,\n                       forecast_created_at TIMESTAMP NOT NULL,\n                       target_datetime TIMESTAMP NOT NULL,\n                       target_utc_epoch INTEGER NOT NULL,\n                       target_date DATE NOT NULL,\n                       target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                       forecast_timezone TEXT NOT NULL,\n                       prediction_method TEXT NOT NULL,\n                       shadow_rule_version TEXT NOT NULL,\n                       physics_kwh REAL,\n                       ai_kwh REAL,\n                       prediction_kwh REAL NOT NULL,\n                       tfs_kwh REAL,\n                       tfs_weight REAL,\n                       sun_elevation_deg REAL,\n                       sun_azimuth_deg REAL,\n                       hours_before_sunset REAL,\n                       day_progress_ratio REAL,\n                       forecast_ghi_wm2 REAL,\n                       direct_radiation_wm2 REAL,\n                       diffuse_radiation_wm2 REAL,\n                       forecast_clouds_pct REAL,\n                       theoretical_max_kwh REAL,\n                       shadow_evaluable BOOLEAN NOT NULL,\n                       shadow_not_evaluable_reason TEXT,\n                       shadow_gate_triggered BOOLEAN NOT NULL,\n                       shadow_prediction_kwh REAL,\n                       production_prediction_kwh REAL NOT NULL,\n                       decision_reason_json TEXT NOT NULL,\n                       created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       UNIQUE(morning_batch_id, source_prediction_id)\n                   )"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ensemble_shadow_evaluations_target\n                   ON ensemble_shadow_evaluations(target_date, target_hour)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ensemble_shadow_evaluations_source\n                   ON ensemble_shadow_evaluations(source_prediction_id)"
            )
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ensemble_shadow_evaluations_batch_target\n                   ON ensemble_shadow_evaluations(morning_batch_id, target_utc_epoch)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ensemble_shadow_evaluation_results (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       shadow_evaluation_id TEXT NOT NULL UNIQUE,\n                       evaluated_at TIMESTAMP NOT NULL,\n                       actual_kwh REAL NOT NULL,\n                       actual_measured_at TIMESTAMP,\n                       clean_evaluation_status BOOLEAN NOT NULL,\n                       clean_exclusion_reason TEXT,\n                       evaluation_status TEXT NOT NULL,\n                       final_absolute_error REAL NOT NULL,\n                       physics_absolute_error REAL,\n                       shadow_absolute_error REAL,\n                       avoided_error REAL,\n                       lost_ensemble_improvement REAL,\n                       created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       FOREIGN KEY (shadow_evaluation_id)\n                           REFERENCES ensemble_shadow_evaluations(shadow_evaluation_id)\n                           ON DELETE RESTRICT\n                   )"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ensemble_shadow_results_status\n                   ON ensemble_shadow_evaluation_results(evaluation_status, evaluated_at)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS ensemble_shadow_evaluation_versions (\n                       evaluation_version_id TEXT PRIMARY KEY,\n                       shadow_evaluation_id TEXT NOT NULL,\n                       evaluation_version INTEGER NOT NULL,\n                       supersedes_evaluation_version_id TEXT,\n                       actual_fingerprint TEXT NOT NULL,\n                       evaluated_at TIMESTAMP NOT NULL,\n                       actual_kwh REAL,\n                       actual_measured_at TIMESTAMP,\n                       clean_evaluation_status BOOLEAN NOT NULL,\n                       clean_exclusion_reason TEXT,\n                       evaluation_status TEXT NOT NULL,\n                       evaluation_reason TEXT,\n                       final_absolute_error REAL,\n                       physics_absolute_error REAL,\n                       shadow_absolute_error REAL,\n                       avoided_error REAL,\n                       lost_ensemble_improvement REAL,\n                       created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       FOREIGN KEY (shadow_evaluation_id)\n                           REFERENCES ensemble_shadow_evaluations(shadow_evaluation_id)\n                           ON DELETE RESTRICT,\n                       UNIQUE(shadow_evaluation_id, evaluation_version),\n                       UNIQUE(shadow_evaluation_id, actual_fingerprint)\n                   )"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ensemble_shadow_versions_current\n                   ON ensemble_shadow_evaluation_versions\n                   (shadow_evaluation_id, evaluation_version DESC)"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_evaluations_no_update\n                   BEFORE UPDATE ON ensemble_shadow_evaluations\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow provenance is append-only');\n                   END"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_evaluations_no_delete\n                   BEFORE DELETE ON ensemble_shadow_evaluations\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow provenance is append-only');\n                   END"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_results_no_update\n                   BEFORE UPDATE ON ensemble_shadow_evaluation_results\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow evaluation result is append-only');\n                   END"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_results_no_delete\n                   BEFORE DELETE ON ensemble_shadow_evaluation_results\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow evaluation result is append-only');\n                   END"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_versions_no_update\n                   BEFORE UPDATE ON ensemble_shadow_evaluation_versions\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow evaluation version is append-only');\n                   END"
            )
            await self._db.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_ensemble_shadow_versions_no_delete\n                   BEFORE DELETE ON ensemble_shadow_evaluation_versions\n                   BEGIN\n                       SELECT RAISE(ABORT, 'ensemble shadow evaluation version is append-only');\n                   END"
            )
            await self._db.execute("RELEASE SAVEPOINT ensemble_shadow_migration")
        except Exception as migration_error:
            await self._db.execute("ROLLBACK TO SAVEPOINT ensemble_shadow_migration")
            await self._db.execute("RELEASE SAVEPOINT ensemble_shadow_migration")
            _LOGGER.error(
                "Ensemble shadow evaluation migration failed: %s", migration_error
            )
            raise

    async def _migrate_emergency_outage_quarantine_v20260627(self) -> None:
        try:
            async with self._db.execute(
                "PRAGMA table_info(hourly_predictions)"
            ) as cursor:
                rows = await cursor.fetchall()
            existing_columns = {row[1] for row in rows}
            alter_statements = []
            if "actual_backfill_blocked" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE hourly_predictions ADD COLUMN actual_backfill_blocked BOOLEAN DEFAULT FALSE"
                )
            if "data_quarantine_reason" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE hourly_predictions ADD COLUMN data_quarantine_reason TEXT"
                )
            if "data_quarantine_at" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE hourly_predictions ADD COLUMN data_quarantine_at TIMESTAMP"
                )
            for update_clause in alter_statements:
                await self._db.execute(update_clause)
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS emergency_outage_periods (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       created_at TIMESTAMP NOT NULL,\n                       start_date DATE NOT NULL,\n                       end_date DATE NOT NULL,\n                       start_hour INTEGER NOT NULL DEFAULT 0,\n                       end_hour INTEGER NOT NULL DEFAULT 23,\n                       reason TEXT NOT NULL,\n                       action TEXT NOT NULL,\n                       actual_policy TEXT NOT NULL,\n                       backfill_policy TEXT NOT NULL DEFAULT 'block',\n                       source TEXT NOT NULL DEFAULT 'emergency_service',\n                       is_active BOOLEAN DEFAULT TRUE,\n                       affected_hourly_rows INTEGER DEFAULT 0,\n                       affected_panel_group_rows INTEGER DEFAULT 0\n                   )"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_emergency_outage_periods_range\n                   ON emergency_outage_periods(start_date, end_date, is_active)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS emergency_outage_audit (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       created_at TIMESTAMP NOT NULL,\n                       operation TEXT NOT NULL,\n                       dry_run BOOLEAN NOT NULL,\n                       payload_json TEXT NOT NULL,\n                       result_json TEXT NOT NULL\n                   )"
            )
            await self._db.commit()
            if alter_statements:
                _LOGGER.info(
                    "Migration: Added emergency outage quarantine fields and tables"
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Emergency outage quarantine migration failed: %s", migration_error
            )
        return None if True else None

    async def _migrate_panel_group_learning_exclusion_repair_v20260626(self) -> None:
        """Repair historical panel-group learning exclusions inherited from parent rows."""
        migration_id = "panel_group_learning_exclusion_repair_v20260626"
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS migration_applied (\n                       migration_id TEXT PRIMARY KEY,\n                       applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = ?",
                (migration_id,),
            ) as cursor:
                migration_row = await cursor.fetchone()
            if migration_row:
                return
            await self._db.execute(
                "DROP TABLE IF EXISTS temp_pg_learning_repair_groups"
            )
            await self._db.execute(
                "CREATE TEMP TABLE temp_pg_learning_repair_groups AS\n                   SELECT DISTINCT ppg.group_name\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp\n                     ON hp.prediction_id = ppg.prediction_id\n                   WHERE COALESCE(ppg.exclude_from_learning_group, 0) = 0\n                     AND (\n                         COALESCE(hp.exclude_from_learning, 0) = 1\n                         OR COALESCE(hp.mppt_throttled, 0) = 1\n                     )"
            )
            async with self._db.execute(
                "SELECT COUNT(*)\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp\n                     ON hp.prediction_id = ppg.prediction_id\n                   WHERE COALESCE(ppg.exclude_from_learning_group, 0) = 0\n                     AND (\n                         COALESCE(hp.exclude_from_learning, 0) = 1\n                         OR COALESCE(hp.mppt_throttled, 0) = 1\n                     )"
            ) as cursor:
                row = await cursor.fetchone()
                repaired_group_count = int(row[0] or 0) if row else 0
            async with self._db.execute(
                "SELECT COUNT(*)\n                   FROM physics_calibration_history pch\n                   WHERE EXISTS (\n                       SELECT 1\n                       FROM hourly_predictions hp\n                       JOIN prediction_panel_groups ppg\n                         ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date = pch.date\n                         AND hp.target_hour = pch.hour\n                         AND ppg.group_name = pch.group_name\n                         AND (\n                             COALESCE(hp.exclude_from_learning, 0) = 1\n                             OR COALESCE(hp.mppt_throttled, 0) = 1\n                         )\n                   )"
            ) as cursor:
                row = await cursor.fetchone()
                deleted_calibration_count = int(row[0] or 0) if row else 0
            await self._db.execute(
                "UPDATE prediction_panel_groups\n                   SET exclude_from_learning_group = 1,\n                       exclusion_reason_group = COALESCE(\n                           NULLIF(exclusion_reason_group, ''),\n                           (\n                               SELECT COALESCE(\n                                   NULLIF(hp.mppt_throttle_reason, ''),\n                                   CASE\n                                       WHEN COALESCE(hp.mppt_throttled, 0) = 1 THEN 'mppt_throttled'\n                                       WHEN NULLIF(hp.weather_alert_type, '') IS NOT NULL THEN hp.weather_alert_type\n                                       ELSE 'parent_excluded_from_learning'\n                                   END\n                               )\n                               FROM hourly_predictions hp\n                               WHERE hp.prediction_id = prediction_panel_groups.prediction_id\n                               LIMIT 1\n                           )\n                       )\n                   WHERE COALESCE(exclude_from_learning_group, 0) = 0\n                     AND EXISTS (\n                         SELECT 1\n                         FROM hourly_predictions hp\n                         WHERE hp.prediction_id = prediction_panel_groups.prediction_id\n                           AND (\n                               COALESCE(hp.exclude_from_learning, 0) = 1\n                               OR COALESCE(hp.mppt_throttled, 0) = 1\n                           )\n                     )"
            )
            await self._db.execute(
                "DELETE FROM physics_calibration_history\n                   WHERE EXISTS (\n                       SELECT 1\n                       FROM hourly_predictions hp\n                       JOIN prediction_panel_groups ppg\n                         ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date = physics_calibration_history.date\n                         AND hp.target_hour = physics_calibration_history.hour\n                         AND ppg.group_name = physics_calibration_history.group_name\n                         AND (\n                             COALESCE(hp.exclude_from_learning, 0) = 1\n                             OR COALESCE(hp.mppt_throttled, 0) = 1\n                         )\n                   )"
            )
            for table_name in (
                "physics_calibration_groups",
                "physics_calibration_hourly",
                "physics_calibration_buckets",
                "physics_calibration_bucket_hourly",
            ):
                await self._db.execute(
                    f"DELETE FROM {table_name}\n                        WHERE group_name IN (\n                            SELECT group_name FROM temp_pg_learning_repair_groups\n                        )"
                )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES (?)",
                (migration_id,),
            )
            await self._db.execute(
                "DROP TABLE IF EXISTS temp_pg_learning_repair_groups"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration %s applied: repaired %d panel-group rows, removed %d contaminated physics calibration history rows",
                migration_id,
                repaired_group_count,
                deleted_calibration_count,
            )
        except Exception as migration_error:
            await self._db.rollback()
            _LOGGER.warning(
                "Panel-group learning exclusion repair failed: %s", migration_error
            )
        return None if True else None

    async def needs_physics_rebuild_after_learning_exclusion_repair(self) -> bool:
        """Return whether physics factors need a one-time rebuild after the repair migration."""
        repair_migration_id = "panel_group_learning_exclusion_repair_v20260626"
        rebuild_migration_id = (
            "physics_rebuild_after_learning_exclusion_repair_v20260627"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        async with self._db.execute(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?",
            (repair_migration_id,),
        ) as cursor:
            repair_row = await cursor.fetchone()
        if not repair_row:
            return False
        async with self._db.execute(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?",
            (rebuild_migration_id,),
        ) as cursor:
            rebuild_row = await cursor.fetchone()
        return rebuild_row is None if True else None

    async def mark_physics_rebuild_after_learning_exclusion_repair_done(self) -> None:
        """Mark the one-time physics rebuild after learning-exclusion repair as complete."""
        rebuild_migration_id = (
            "physics_rebuild_after_learning_exclusion_repair_v20260627"
        )
        await self.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        async with self.transaction():
            await self.execute(
                "INSERT OR IGNORE INTO migration_applied (migration_id) VALUES (?)",
                (rebuild_migration_id,),
                auto_commit=False,
            )
        return None if True else None

    async def count_physics_calibration_history(self) -> int:
        """Return the number of persisted physics calibration history rows."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM physics_calibration_history"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def _migrate_actual_live_state_v20260619(self) -> None:
        """Create SFML-owned live actual/power state tables."""
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS actual_live_state (\n                date DATE PRIMARY KEY,\n                total_power_w REAL,\n                total_actual_kwh REAL NOT NULL DEFAULT 0,\n                power_source TEXT,\n                actual_source TEXT NOT NULL,\n                groups_expected INTEGER NOT NULL DEFAULT 0,\n                groups_available INTEGER NOT NULL DEFAULT 0,\n                quality TEXT NOT NULL,\n                updated_at TIMESTAMP NOT NULL\n            )\n            "
        )
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS actual_live_panel_groups (\n                date DATE NOT NULL,\n                group_name TEXT NOT NULL,\n                power_w REAL,\n                actual_today_kwh REAL NOT NULL DEFAULT 0,\n                power_sensor TEXT,\n                quality TEXT NOT NULL,\n                updated_at TIMESTAMP NOT NULL,\n                PRIMARY KEY(date, group_name)\n            )\n            "
        )
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_actual_live_panel_groups_date\n            ON actual_live_panel_groups(date)\n            "
        )
        await self._db.commit()

    async def _migrate_panel_group_topology_epochs_v20260511(self) -> None:
        """Create panel-group topology epochs and row-level lineage metadata."""
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS panel_group_config_epochs (\n                epoch_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                topology_hash TEXT NOT NULL,\n                valid_from TIMESTAMP NOT NULL,\n                valid_to TIMESTAMP,\n                reason TEXT,\n                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n            )\n            "
        )
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS panel_group_config_epoch_groups (\n                epoch_group_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                epoch_id INTEGER NOT NULL,\n                group_uid TEXT NOT NULL,\n                group_lineage_uid TEXT NOT NULL,\n                display_name TEXT NOT NULL,\n                power_wp REAL NOT NULL,\n                azimuth REAL NOT NULL,\n                tilt REAL NOT NULL,\n                energy_sensor TEXT,\n                group_signature TEXT NOT NULL,\n                is_active BOOLEAN NOT NULL DEFAULT TRUE,\n                FOREIGN KEY (epoch_id) REFERENCES panel_group_config_epochs(epoch_id) ON DELETE CASCADE,\n                UNIQUE(epoch_id, display_name)\n            )\n            "
        )
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
        existing_columns = {schema_row[1] for schema_row in schema_rows}
        new_column_names = {
            "config_epoch_id": "INTEGER",
            "group_uid": "TEXT",
            "group_lineage_uid": "TEXT",
            "power_wp_at_prediction": "REAL",
            "topology_compatible": "BOOLEAN DEFAULT TRUE",
            "topology_exclusion_reason": "TEXT",
        }
        alter_statements = []
        for column_name, column_type in new_column_names.items():
            if column_name not in existing_columns:
                await self._db.execute(
                    f"ALTER TABLE prediction_panel_groups ADD COLUMN {column_name} {column_type}"
                )
                alter_statements.append(column_name)
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_panel_group_epochs_active\n            ON panel_group_config_epochs(valid_to, valid_from)\n            "
        )
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_panel_group_epoch_groups_lookup\n            ON panel_group_config_epoch_groups(epoch_id, display_name)\n            "
        )
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_prediction_panel_groups_topology\n            ON prediction_panel_groups(config_epoch_id, group_uid, group_name)\n            "
        )
        await self._db.commit()
        if alter_statements:
            _LOGGER.info(
                "Migration 2026.05.11: Added panel-group topology columns: %s",
                ", ".join(alter_statements),
            )
        return None if True else None

    async def _migrate_operational_reforecast_tables_v2010(self) -> None:
        """Create operational reforecast sidecar tables. @zara V20.1.0

        These tables are intentionally separate from the core learning truth.
        They exist only for operational/user-facing forecast evolution.
        """
        added_columns = []
        table_definitions = {
            "ops_forecast_snapshots": "\n                CREATE TABLE IF NOT EXISTS ops_forecast_snapshots (\n                    snapshot_sequence INTEGER PRIMARY KEY AUTOINCREMENT,\n                    snapshot_date DATE NOT NULL,\n                    snapshot_type TEXT NOT NULL,\n                    trigger_source TEXT NOT NULL,\n                    triggered_at TIMESTAMP NOT NULL,\n                    cutoff_hour INTEGER,\n                    weather_refresh_started_at TIMESTAMP,\n                    weather_refresh_finished_at TIMESTAMP,\n                    forecast_generation_started_at TIMESTAMP,\n                    forecast_generation_finished_at TIMESTAMP,\n                    status TEXT NOT NULL,\n                    source_method TEXT,\n                    notes TEXT\n                )",
            "ops_hourly_forecasts": "\n                CREATE TABLE IF NOT EXISTS ops_hourly_forecasts (\n                    forecast_row_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    ops_track_date DATE NOT NULL,\n                    target_date DATE NOT NULL,\n                    target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                    prediction_id TEXT NOT NULL,\n                    snapshot_type TEXT NOT NULL,\n                    snapshot_sequence INTEGER NOT NULL,\n                    snapshot_created_at TIMESTAMP NOT NULL,\n                    trigger_source TEXT NOT NULL,\n                    is_active BOOLEAN NOT NULL DEFAULT TRUE,\n                    prediction_kwh REAL NOT NULL,\n                    prediction_kwh_uncapped REAL,\n                    prediction_method TEXT,\n                    physics_kwh REAL,\n                    ai_kwh REAL,\n                    ai_confidence REAL,\n                    ml_contribution_percent REAL,\n                    lstm_kwh REAL,\n                    ridge_kwh REAL,\n                    tfs_kwh REAL,\n                    tfs_weight REAL,\n                    is_production_hour BOOLEAN,\n                    actual_kwh REAL,\n                    source_track TEXT NOT NULL,\n                    reforecast_cutoff_hour INTEGER,\n                    superseded_at TIMESTAMP,\n                    FOREIGN KEY (snapshot_sequence) REFERENCES ops_forecast_snapshots(snapshot_sequence) ON DELETE CASCADE,\n                    UNIQUE(target_date, target_hour, snapshot_sequence)\n                )",
            "ops_prediction_weather": "\n                CREATE TABLE IF NOT EXISTS ops_prediction_weather (\n                    ops_weather_row_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    forecast_row_id INTEGER NOT NULL,\n                    target_date DATE NOT NULL,\n                    target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                    prediction_id TEXT NOT NULL,\n                    snapshot_sequence INTEGER NOT NULL,\n                    temperature REAL,\n                    solar_radiation_wm2 REAL,\n                    wind REAL,\n                    humidity REAL,\n                    rain REAL,\n                    clouds REAL,\n                    pressure REAL,\n                    source TEXT,\n                    lux REAL,\n                    frost_detected TEXT,\n                    frost_score REAL,\n                    frost_confidence REAL,\n                    diffuse_radiation REAL,\n                    direct_radiation REAL,\n                    is_active BOOLEAN NOT NULL DEFAULT TRUE,\n                    superseded_at TIMESTAMP,\n                    FOREIGN KEY (forecast_row_id) REFERENCES ops_hourly_forecasts(forecast_row_id) ON DELETE CASCADE,\n                    FOREIGN KEY (snapshot_sequence) REFERENCES ops_forecast_snapshots(snapshot_sequence) ON DELETE CASCADE,\n                    UNIQUE(forecast_row_id)\n                )",
            "ops_prediction_panel_groups": "\n                CREATE TABLE IF NOT EXISTS ops_prediction_panel_groups (\n                    ops_group_row_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    forecast_row_id INTEGER NOT NULL,\n                    target_date DATE NOT NULL,\n                    target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                    prediction_id TEXT NOT NULL,\n                    snapshot_sequence INTEGER NOT NULL,\n                    group_name TEXT NOT NULL,\n                    prediction_kwh REAL NOT NULL,\n                    physics_kwh REAL,\n                    ai_kwh REAL,\n                    lstm_kwh REAL,\n                    ridge_kwh REAL,\n                    tfs_kwh REAL,\n                    actual_kwh REAL,\n                    raw_physics_kwh REAL,\n                    is_active BOOLEAN NOT NULL DEFAULT TRUE,\n                    superseded_at TIMESTAMP,\n                    FOREIGN KEY (forecast_row_id) REFERENCES ops_hourly_forecasts(forecast_row_id) ON DELETE CASCADE,\n                    FOREIGN KEY (snapshot_sequence) REFERENCES ops_forecast_snapshots(snapshot_sequence) ON DELETE CASCADE,\n                    UNIQUE(prediction_id, snapshot_sequence, group_name)\n                )",
            "ops_daily_forecasts": "\n                CREATE TABLE IF NOT EXISTS ops_daily_forecasts (\n                    ops_daily_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    forecast_type TEXT NOT NULL CHECK(forecast_type IN ('today', 'tomorrow', 'day_after_tomorrow')),\n                    forecast_date DATE NOT NULL,\n                    snapshot_type TEXT NOT NULL,\n                    snapshot_sequence INTEGER NOT NULL,\n                    snapshot_created_at TIMESTAMP NOT NULL,\n                    trigger_source TEXT NOT NULL,\n                    prediction_kwh REAL NOT NULL,\n                    prediction_kwh_raw REAL,\n                    source_method TEXT,\n                    actual_kwh REAL,\n                    is_active BOOLEAN NOT NULL DEFAULT TRUE,\n                    superseded_at TIMESTAMP,\n                    FOREIGN KEY (snapshot_sequence) REFERENCES ops_forecast_snapshots(snapshot_sequence) ON DELETE CASCADE,\n                    UNIQUE(forecast_type, forecast_date, snapshot_sequence)\n                )",
        }
        index_definitions = {
            "idx_ops_forecast_snapshots_date": "\n                CREATE INDEX IF NOT EXISTS idx_ops_forecast_snapshots_date\n                ON ops_forecast_snapshots(snapshot_date, snapshot_sequence)\n            ",
            "idx_ops_hourly_forecasts_active": "\n                CREATE INDEX IF NOT EXISTS idx_ops_hourly_forecasts_active\n                ON ops_hourly_forecasts(target_date, target_hour, is_active)\n            ",
            "idx_ops_hourly_forecasts_snapshot": "\n                CREATE INDEX IF NOT EXISTS idx_ops_hourly_forecasts_snapshot\n                ON ops_hourly_forecasts(ops_track_date, snapshot_sequence)\n            ",
            "idx_ops_hourly_forecasts_prediction": "\n                CREATE INDEX IF NOT EXISTS idx_ops_hourly_forecasts_prediction\n                ON ops_hourly_forecasts(prediction_id, is_active)\n            ",
            "idx_ops_prediction_weather_active": "\n                CREATE INDEX IF NOT EXISTS idx_ops_prediction_weather_active\n                ON ops_prediction_weather(target_date, target_hour, is_active)\n            ",
            "idx_ops_prediction_weather_prediction": "\n                CREATE INDEX IF NOT EXISTS idx_ops_prediction_weather_prediction\n                ON ops_prediction_weather(prediction_id, is_active)\n            ",
            "idx_ops_prediction_panel_groups_active": "\n                CREATE INDEX IF NOT EXISTS idx_ops_prediction_panel_groups_active\n                ON ops_prediction_panel_groups(target_date, target_hour, is_active)\n            ",
            "idx_ops_prediction_panel_groups_forecast_row": "\n                CREATE INDEX IF NOT EXISTS idx_ops_prediction_panel_groups_forecast_row\n                ON ops_prediction_panel_groups(forecast_row_id)\n            ",
            "idx_ops_daily_forecasts_active": "\n                CREATE INDEX IF NOT EXISTS idx_ops_daily_forecasts_active\n                ON ops_daily_forecasts(forecast_type, forecast_date, is_active)\n            ",
            "idx_ops_hourly_forecasts_one_active": "\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_hourly_forecasts_one_active\n                ON ops_hourly_forecasts(target_date, target_hour)\n                WHERE is_active = TRUE\n            ",
            "idx_ops_prediction_weather_one_active": "\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_prediction_weather_one_active\n                ON ops_prediction_weather(target_date, target_hour)\n                WHERE is_active = TRUE\n            ",
            "idx_ops_prediction_panel_groups_one_active": "\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_prediction_panel_groups_one_active\n                ON ops_prediction_panel_groups(target_date, target_hour, group_name)\n                WHERE is_active = TRUE\n            ",
            "idx_ops_daily_forecasts_one_active": "\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_daily_forecasts_one_active\n                ON ops_daily_forecasts(forecast_type, forecast_date)\n                WHERE is_active = TRUE\n            ",
        }
        for table_name, create_table_sql in table_definitions.items():
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cursor:
                if not await cursor.fetchone():
                    await self._db.execute(create_table_sql)
                    added_columns.append(table_name)
        migrated_at = datetime.now().isoformat()
        await self._db.execute(
            "UPDATE ops_hourly_forecasts\n               SET is_active = FALSE,\n                   superseded_at = COALESCE(superseded_at, ?)\n               WHERE forecast_row_id IN (\n                   SELECT forecast_row_id FROM (\n                       SELECT forecast_row_id,\n                              ROW_NUMBER() OVER (\n                                  PARTITION BY target_date, target_hour\n                                  ORDER BY snapshot_sequence DESC, forecast_row_id DESC\n                              ) AS row_rank\n                       FROM ops_hourly_forecasts\n                       WHERE is_active = TRUE\n                   )\n                   WHERE row_rank > 1\n               )",
            (migrated_at,),
        )
        await self._db.execute(
            "UPDATE ops_prediction_weather\n               SET is_active = FALSE,\n                   superseded_at = COALESCE(superseded_at, ?)\n               WHERE ops_weather_row_id IN (\n                   SELECT ops_weather_row_id FROM (\n                       SELECT ops_weather_row_id,\n                              ROW_NUMBER() OVER (\n                                  PARTITION BY target_date, target_hour\n                                  ORDER BY snapshot_sequence DESC, ops_weather_row_id DESC\n                              ) AS row_rank\n                       FROM ops_prediction_weather\n                       WHERE is_active = TRUE\n                   )\n                   WHERE row_rank > 1\n               )",
            (migrated_at,),
        )
        await self._db.execute(
            "UPDATE ops_prediction_panel_groups\n               SET is_active = FALSE,\n                   superseded_at = COALESCE(superseded_at, ?)\n               WHERE ops_group_row_id IN (\n                   SELECT ops_group_row_id FROM (\n                       SELECT ops_group_row_id,\n                              ROW_NUMBER() OVER (\n                                  PARTITION BY target_date, target_hour, group_name\n                                  ORDER BY snapshot_sequence DESC, ops_group_row_id DESC\n                              ) AS row_rank\n                       FROM ops_prediction_panel_groups\n                       WHERE is_active = TRUE\n                   )\n                   WHERE row_rank > 1\n               )",
            (migrated_at,),
        )
        await self._db.execute(
            "UPDATE ops_daily_forecasts\n               SET is_active = FALSE,\n                   superseded_at = COALESCE(superseded_at, ?)\n               WHERE ops_daily_id IN (\n                   SELECT ops_daily_id FROM (\n                       SELECT ops_daily_id,\n                              ROW_NUMBER() OVER (\n                                  PARTITION BY forecast_type, forecast_date\n                                  ORDER BY snapshot_sequence DESC, ops_daily_id DESC\n                              ) AS row_rank\n                       FROM ops_daily_forecasts\n                       WHERE is_active = TRUE\n                   )\n                   WHERE row_rank > 1\n               )",
            (migrated_at,),
        )
        for index_statement in index_definitions.values():
            await self._db.execute(index_statement)
        if added_columns:
            await self._db.commit()
            _LOGGER.info(
                "Migration V20.1.0: Created operational reforecast tables: %s",
                ", ".join(added_columns),
            )
        else:
            await self._db.commit()
        return None if True else None

    async def _migrate_operational_reforecast_settings_v2011(self) -> None:
        """Create persistent operational reforecast settings table. @zara V20.1.1"""
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS ops_reforecast_settings (\n                settings_id INTEGER PRIMARY KEY AUTOINCREMENT,\n                mode TEXT NOT NULL,\n                custom_time TEXT,\n                enabled BOOLEAN NOT NULL DEFAULT FALSE,\n                updated_at TIMESTAMP NOT NULL,\n                updated_by TEXT,\n                notes TEXT,\n                CHECK (mode IN ('standard', 'standard_midday',\n                                'standard_midday_afternoon', 'custom_time'))\n            )\n            "
        )
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_ops_reforecast_settings_updated\n            ON ops_reforecast_settings(updated_at DESC)\n            "
        )
        await self._db.commit()

    async def _migrate_tfs_columns(self) -> None:
        for table_name in ("hourly_predictions", "prediction_panel_groups"):
            async with self._db.execute(f"PRAGMA table_info({table_name})") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
                if "tfs_kwh" not in existing_columns:
                    await self._db.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN tfs_kwh REAL"
                    )
                    _LOGGER.info("Migration: Added tfs_kwh to %s", table_name)
                if "tfs_weight" not in existing_columns:
                    await self._db.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN tfs_weight REAL"
                    )
                    _LOGGER.info("Migration: Added tfs_weight to %s", table_name)
                continue
        await self._db.commit()

    async def _migrate_eod_duration_v1710(self) -> None:
        """Add eod_duration_seconds column to daily_summaries. @zara"""
        try:
            async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            if "eod_duration_seconds" not in existing_columns:
                await self._db.execute(
                    "ALTER TABLE daily_summaries ADD COLUMN eod_duration_seconds REAL"
                )
                await self._db.commit()
                _LOGGER.info("Migration: Added eod_duration_seconds to daily_summaries")
        except Exception as migration_error:
            _LOGGER.warning("Migration eod_duration failed: %s", migration_error)
        return None if True else None

    async def _migrate_eod_completed_at(self) -> None:
        """Remember a finished end-of-day run on daily_summaries.

        A stored duration means that run already reached the end. Its
        created_at is the completion time, so a later scheduled trigger
        can skip the day after a restart.
        """
        try:
            async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            if "eod_completed_at" not in existing_columns:
                await self._db.execute(
                    "ALTER TABLE daily_summaries ADD COLUMN eod_completed_at TEXT"
                )
                await self._db.commit()
                _LOGGER.info("Migration: Added eod_completed_at to daily_summaries")
            await self._db.execute(
                "UPDATE daily_summaries\n                   SET eod_completed_at = COALESCE(created_at, date || 'T23:30:00')\n                   WHERE eod_duration_seconds IS NOT NULL\n                     AND (eod_completed_at IS NULL OR eod_completed_at = '')"
            )
            await self._db.commit()
        except Exception as migration_error:
            _LOGGER.warning("Migration eod_completed_at failed: %s", migration_error)
        return None if True else None

    async def _migrate_daily_summaries_unique_date(self) -> None:
        """Add missing UNIQUE index on daily_summaries.date for ON CONFLICT support. @zara"""
        try:
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_daily_summaries_date'"
            ) as cursor:
                summary_row = await cursor.fetchone()
            if not summary_row:
                await self._db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_summaries_date ON daily_summaries(date)"
                )
                await self._db.commit()
                _LOGGER.info("Migration: Added UNIQUE index on daily_summaries.date")
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration daily_summaries_unique_date failed: %s", migration_error
            )
        return None if True else None

    async def _migrate_daily_summary_evaluation_fields_v1730(self) -> None:
        """Add clean-evaluation columns to daily_summaries for MPPT-aware stats. @zara"""
        try:
            async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            for schema_row, column_type in (
                ("evaluation_predicted_kwh", "REAL"),
                ("evaluation_actual_kwh", "REAL"),
                ("evaluation_coverage_percent", "REAL"),
                ("excluded_hours_count", "INTEGER DEFAULT 0"),
                ("excluded_mppt_hours_count", "INTEGER DEFAULT 0"),
            ):
                if schema_row not in existing_columns:
                    alter_statements.append(
                        f"ALTER TABLE daily_summaries ADD COLUMN {schema_row} {column_type}"
                    )
            for update_clause in alter_statements:
                await self._db.execute(update_clause)
            if alter_statements:
                await self._db.commit()
                _LOGGER.info(
                    "Migration: Added clean evaluation fields to daily_summaries"
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration daily_summary_evaluation_fields failed: %s", migration_error
            )
        return None if True else None

    async def _migrate_daily_statistics_evaluation_fields_v1730(self) -> None:
        """Add rolling evaluation transparency fields to daily_statistics. @zara"""
        try:
            async with self._db.execute(
                "PRAGMA table_info(daily_statistics)"
            ) as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            for schema_row, column_type in (
                ("last_7_days_avg_evaluation_coverage_percent", "REAL"),
                ("last_7_days_avg_excluded_mppt_hours", "REAL"),
                ("last_30_days_avg_evaluation_coverage_percent", "REAL"),
                ("last_30_days_avg_excluded_mppt_hours", "REAL"),
            ):
                if schema_row not in existing_columns:
                    alter_statements.append(
                        f"ALTER TABLE daily_statistics ADD COLUMN {schema_row} {column_type}"
                    )
            for update_clause in alter_statements:
                await self._db.execute(update_clause)
            if alter_statements:
                await self._db.commit()
                _LOGGER.info(
                    "Migration: Added rolling evaluation fields to daily_statistics"
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration daily_statistics_evaluation_fields failed: %s",
                migration_error,
            )
        return None if True else None

    async def _migrate_daily_summary_evaluation_diagnostics_v2000(self) -> None:
        """Add explicit clean-evaluation diagnostic counters to daily_summaries."""
        try:
            async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            for schema_row, column_type in (
                ("evaluation_hours_count", "INTEGER DEFAULT 0"),
                ("production_candidate_hours_count", "INTEGER DEFAULT 0"),
                ("missing_actual_hours_count", "INTEGER DEFAULT 0"),
                ("excluded_weather_alert_hours_count", "INTEGER DEFAULT 0"),
                ("excluded_inverter_clipped_hours_count", "INTEGER DEFAULT 0"),
                ("excluded_reason_breakdown_json", "TEXT"),
            ):
                if schema_row not in existing_columns:
                    alter_statements.append(
                        f"ALTER TABLE daily_summaries ADD COLUMN {schema_row} {column_type}"
                    )
            for update_clause in alter_statements:
                await self._db.execute(update_clause)
            if alter_statements:
                await self._db.commit()
                _LOGGER.info(
                    "Migration: Added clean evaluation diagnostic fields to daily_summaries"
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration daily_summary_evaluation_diagnostics failed: %s",
                migration_error,
            )
        return None if True else None

    @staticmethod
    def classify_hourly_clean_evaluation(
        prediction: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Classify one actual hour with the shared clean-evaluation policy."""
        if prediction.get("manual_override"):
            return (False, "manual_override")
        if prediction.get("actual_backfill_blocked"):
            return (False, "actual_backfill_blocked")
        if prediction.get("data_quarantine_reason"):
            return (False, f"data_quarantine:{prediction['data_quarantine_reason']}")
        if prediction.get("inverter_clipped"):
            return (False, "inverter_clipped")
        if prediction.get("mppt_throttled"):
            return (False, "mppt_throttled")
        if prediction.get("mppt_throttle_reason"):
            return (False, f"mppt_throttle_reason:{prediction['mppt_throttle_reason']}")
        if prediction.get("exclude_from_learning"):
            return (False, "exclude_from_learning")
        weather_alert_type = prediction.get("weather_alert_type")
        if weather_alert_type == "snow_covered_panels":
            return (False, "snow_covered_panels")
        explicit_exclusion = prediction.get("exclude_from_clean_evaluation")
        if explicit_exclusion is not None and bool(explicit_exclusion):
            return (False, "clean_evaluation_excluded")
        return (True, None)

    @staticmethod
    def calculate_daily_clean_evaluation(
        predictions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calculate daily forecast quality from production-relevant clean hours."""
        predicted_kwh_total = 0.0
        actual_kwh_total = 0.0
        evaluation_hours_count = 0
        production_candidate_hours_count = 0
        missing_actual_hours_count = 0
        excluded_hours_count = 0
        excluded_mppt_hours_count = 0
        excluded_weather_alert_hours_count = 0
        excluded_inverter_clipped_hours_count = 0
        exclusion_reason_counts = {}
        for prediction in predictions:
            predicted_kwh = float(prediction.get("prediction_kwh") or 0.0)
            raw_actual_kwh = prediction.get("actual_kwh")
            actual_kwh = float(raw_actual_kwh) if raw_actual_kwh is not None else None
            production_relevant = (
                predicted_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
                or (
                    actual_kwh is not None
                    and actual_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
                )
            )
            if not production_relevant:
                continue
            production_candidate_hours_count += 1
            if actual_kwh is None:
                missing_actual_hours_count += 1
                continue
            clean_eligible, exclusion_reason = (
                DatabaseManager.classify_hourly_clean_evaluation(prediction)
            )
            if not clean_eligible:
                excluded_hours_count += 1
                if prediction.get("mppt_throttled"):
                    excluded_mppt_hours_count += 1
                if prediction.get("has_weather_alert"):
                    excluded_weather_alert_hours_count += 1
                if prediction.get("inverter_clipped"):
                    excluded_inverter_clipped_hours_count += 1
                field_name = exclusion_reason or "other"
                exclusion_reason_counts[field_name] = (
                    exclusion_reason_counts.get(field_name, 0) + 1
                )
                continue
            evaluation_hours_count += 1
            predicted_kwh_total += predicted_kwh
            actual_kwh_total += actual_kwh
            continue
        if actual_kwh_total > 0.01 and predicted_kwh_total > 0:
            absolute_error_kwh = abs(predicted_kwh_total - actual_kwh_total)
            accuracy_percent = max(
                0.0, (1.0 - absolute_error_kwh / predicted_kwh_total) * 100.0
            )
        else:
            accuracy_percent = 0.0
        signed_error_kwh = actual_kwh_total - predicted_kwh_total
        evaluation_coverage_percent = (
            evaluation_hours_count / production_candidate_hours_count * 100.0
            if production_candidate_hours_count > 0
            else 0.0
        )
        return (
            {
                "accuracy_percent": round(accuracy_percent, 1),
                "evaluation_predicted_kwh": round(predicted_kwh_total, 2),
                "evaluation_actual_kwh": round(actual_kwh_total, 2),
                "evaluation_coverage_percent": round(evaluation_coverage_percent, 1),
                "evaluation_hours_count": evaluation_hours_count,
                "production_candidate_hours_count": production_candidate_hours_count,
                "missing_actual_hours_count": missing_actual_hours_count,
                "excluded_hours_count": excluded_hours_count,
                "excluded_mppt_hours_count": excluded_mppt_hours_count,
                "excluded_weather_alert_hours_count": excluded_weather_alert_hours_count,
                "excluded_inverter_clipped_hours_count": excluded_inverter_clipped_hours_count,
                "excluded_reason_breakdown_json": (
                    json.dumps(exclusion_reason_counts, sort_keys=True)
                    if exclusion_reason_counts
                    else None
                ),
                "error_kwh": round(signed_error_kwh, 2),
                "error_percent": round(
                    (
                        signed_error_kwh / predicted_kwh_total * 100.0
                        if predicted_kwh_total > 0
                        else 0.0
                    ),
                    1,
                ),
            }
            if True
            else None
        )

    async def _backfill_daily_summary_clean_evaluation_v2000(self) -> None:
        """Backfill clean-evaluation summary fields from hourly_predictions.

        Runs on every startup; a day is only written when its recomputed
        stats differ, so a consistent database costs reads but no exclusive
        lock while other readers are busy at Home Assistant startup.
        """
        try:
            summary_rows = await self.fetchall(
                f"SELECT ds.date, {', '.join((f'ds.{evaluation_field}' for evaluation_field in DAILY_SUMMARY_EVALUATION_FIELDS))}\n                   FROM daily_summaries ds\n                   WHERE EXISTS (\n                       SELECT 1 FROM hourly_predictions hp\n                       WHERE hp.target_date = ds.date\n                   )\n                   ORDER BY ds.date"
            )
            if not summary_rows:
                return
            updated_summary_count = 0
            async with self.transaction():
                for row in summary_rows:
                    summary_date = str(row[0])
                    prediction_rows = await self.fetchall(
                        "SELECT prediction_kwh, actual_kwh, exclude_from_learning,\n                                  mppt_throttled, mppt_throttle_reason, has_weather_alert,\n                                  inverter_clipped, weather_alert_type,\n                                  exclude_from_clean_evaluation\n                           FROM hourly_predictions\n                           WHERE target_date = ?\n                           ORDER BY target_hour",
                        (summary_date,),
                    )
                    predictions = [
                        {
                            "prediction_kwh": row_index["prediction_kwh"],
                            "actual_kwh": row_index["actual_kwh"],
                            "exclude_from_learning": row_index["exclude_from_learning"],
                            "mppt_throttled": row_index["mppt_throttled"],
                            "mppt_throttle_reason": row_index["mppt_throttle_reason"],
                            "has_weather_alert": row_index["has_weather_alert"],
                            "inverter_clipped": row_index["inverter_clipped"],
                            "weather_alert_type": row_index["weather_alert_type"],
                            "exclude_from_clean_evaluation": row_index[
                                "exclude_from_clean_evaluation"
                            ],
                        }
                        for row_index in prediction_rows
                    ]
                    evaluation_statistics = self.calculate_daily_clean_evaluation(
                        predictions
                    )
                    if daily_summary_evaluation_unchanged(
                        tuple(row)[1:], evaluation_statistics
                    ):
                        continue
                    await self.execute(
                        "UPDATE daily_summaries\n                           SET accuracy_percent = ?,\n                               evaluation_predicted_kwh = ?,\n                               evaluation_actual_kwh = ?,\n                               evaluation_coverage_percent = ?,\n                               evaluation_hours_count = ?,\n                               production_candidate_hours_count = ?,\n                               missing_actual_hours_count = ?,\n                               excluded_hours_count = ?,\n                               excluded_mppt_hours_count = ?,\n                               excluded_weather_alert_hours_count = ?,\n                               excluded_inverter_clipped_hours_count = ?,\n                               excluded_reason_breakdown_json = ?,\n                               error_kwh = ?,\n                               error_percent = ?\n                           WHERE date = ?",
                        (
                            evaluation_statistics["accuracy_percent"],
                            evaluation_statistics["evaluation_predicted_kwh"],
                            evaluation_statistics["evaluation_actual_kwh"],
                            evaluation_statistics["evaluation_coverage_percent"],
                            evaluation_statistics["evaluation_hours_count"],
                            evaluation_statistics["production_candidate_hours_count"],
                            evaluation_statistics["missing_actual_hours_count"],
                            evaluation_statistics["excluded_hours_count"],
                            evaluation_statistics["excluded_mppt_hours_count"],
                            evaluation_statistics["excluded_weather_alert_hours_count"],
                            evaluation_statistics[
                                "excluded_inverter_clipped_hours_count"
                            ],
                            evaluation_statistics["excluded_reason_breakdown_json"],
                            evaluation_statistics["error_kwh"],
                            evaluation_statistics["error_percent"],
                            summary_date,
                        ),
                        auto_commit=False,
                    )
                    updated_summary_count += 1
                    continue
            if updated_summary_count:
                _LOGGER.info(
                    "Backfilled clean daily summary evaluation for %d of %d days",
                    updated_summary_count,
                    len(summary_rows),
                )
            else:
                _LOGGER.debug(
                    "Clean daily summary evaluation already current for %d days",
                    len(summary_rows),
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Backfill daily summary clean evaluation failed: %s", migration_error
            )
        return None if True else None

    async def _migrate_seasonal_archive_v1684(self) -> None:
        """Create seasonal model archive tables. @zara V16.8.4"""
        try:
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_seasonal_archive_meta'"
            ) as cursor:
                summary_row = await cursor.fetchone()
            if not summary_row:
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS ai_seasonal_archive_meta (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        season TEXT NOT NULL CHECK(season IN ('winter', 'spring', 'summer', 'autumn')),\n                        archive_type TEXT NOT NULL CHECK(archive_type IN ('lstm', 'ridge')),\n                        training_samples INTEGER DEFAULT 0,\n                        accuracy REAL DEFAULT 0.0,\n                        rmse REAL DEFAULT 0.0,\n                        source_year INTEGER NOT NULL,\n                        input_size INTEGER,\n                        hidden_size INTEGER,\n                        hidden2 INTEGER DEFAULT 24,\n                        num_outputs INTEGER,\n                        num_heads INTEGER DEFAULT 4,\n                        sequence_length INTEGER DEFAULT 24,\n                        alpha REAL,\n                        flat_size INTEGER,\n                        loo_cv_score REAL,\n                        archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(season, archive_type)\n                    )\n                "
                )
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS ai_seasonal_archive_weights (\n                        season TEXT NOT NULL,\n                        archive_type TEXT NOT NULL,\n                        weight_type TEXT NOT NULL,\n                        weight_index INTEGER NOT NULL,\n                        weight_value REAL NOT NULL,\n                        PRIMARY KEY(season, archive_type, weight_type, weight_index)\n                    ) WITHOUT ROWID\n                "
                )
                await self._db.commit()
                _LOGGER.info("Migration: Created seasonal model archive tables")
        except Exception as migration_error:
            _LOGGER.warning("Migration seasonal_archive failed: %s", migration_error)
        return None if True else None

    async def _migrate_cusum_bias_correction_v1720(self) -> None:
        """V17.2.0: Add bias_correction_factor, solar_clamp_min to drift_cusum_state. @zara"""
        try:
            async with self._db.execute(
                "PRAGMA table_info(drift_cusum_state)"
            ) as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            if "bias_correction_factor" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE drift_cusum_state ADD COLUMN bias_correction_factor REAL DEFAULT 1.0"
                )
            if "bias_correction_updated" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE drift_cusum_state ADD COLUMN bias_correction_updated TIMESTAMP"
                )
            if "solar_clamp_min" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE drift_cusum_state ADD COLUMN solar_clamp_min REAL DEFAULT 0.75"
                )
            if alter_statements:
                for update_clause in alter_statements:
                    await self._db.execute(update_clause)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V17.2.0: Added bias correction columns to drift_cusum_state (%d cols)",
                    len(alter_statements),
                )
        except Exception as migration_error:
            _LOGGER.warning("Migration V17.2.0 failed (non-fatal): %s", migration_error)
        return None if True else None

    async def _migrate_drift_null_season_uniqueness_v20260805(self) -> None:
        """Deduplicate global drift metrics and enforce one NULL-season row."""
        async with self.transaction():
            await self.execute(
                "DELETE FROM drift_metrics_rolling AS stale\n                   WHERE stale.season IS NULL\n                     AND EXISTS (\n                         SELECT 1 FROM drift_metrics_rolling AS newer\n                         WHERE newer.season IS NULL\n                           AND newer.scope = stale.scope\n                           AND newer.window_days = stale.window_days\n                           AND (\n                               COALESCE(newer.calculated_at, '') >\n                                   COALESCE(stale.calculated_at, '')\n                               OR (\n                                   COALESCE(newer.calculated_at, '') =\n                                       COALESCE(stale.calculated_at, '')\n                                   AND newer.id > stale.id\n                               )\n                           )\n                     )"
            )
            await self.execute(
                "DELETE FROM drift_metrics_bucket AS stale\n                   WHERE stale.season IS NULL\n                     AND EXISTS (\n                         SELECT 1 FROM drift_metrics_bucket AS newer\n                         WHERE newer.season IS NULL\n                           AND newer.scope = stale.scope\n                           AND newer.cloud_bucket = stale.cloud_bucket\n                           AND newer.hour_bucket = stale.hour_bucket\n                           AND (\n                               COALESCE(newer.calculated_at, '') >\n                                   COALESCE(stale.calculated_at, '')\n                               OR (\n                                   COALESCE(newer.calculated_at, '') =\n                                       COALESCE(stale.calculated_at, '')\n                                   AND newer.id > stale.id\n                               )\n                           )\n                     )"
            )
            await self.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS\n                       uq_drift_metrics_rolling_null_season\n                   ON drift_metrics_rolling(scope, window_days)\n                   WHERE season IS NULL"
            )
            await self.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS\n                       uq_drift_metrics_bucket_null_season\n                   ON drift_metrics_bucket(scope, cloud_bucket, hour_bucket)\n                   WHERE season IS NULL"
            )
        return None if True else None

    async def _migrate_shadow_group_relative_percent_v20260805(self) -> None:
        """Recalculate historical group percentages and reset derived group models."""
        async with self.transaction():
            await self.execute(
                "CREATE TABLE IF NOT EXISTS shadow_history_repair_runs (\n                       repair_version TEXT PRIMARY KEY,\n                       repaired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       patterns_rebuilt_at TIMESTAMP,\n                       source_rows INTEGER NOT NULL,\n                       recalculated_rows INTEGER NOT NULL,\n                       not_evaluable_rows INTEGER NOT NULL,\n                       learning_history_rows_removed INTEGER NOT NULL,\n                       hourly_pattern_rows_removed INTEGER NOT NULL,\n                       seasonal_pattern_rows_removed INTEGER NOT NULL\n                   )"
            )
            repair_row = await self.fetchone(
                "SELECT 1 FROM shadow_history_repair_runs\n                   WHERE repair_version = ?",
                (SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID,),
            )
            if repair_row:
                return
            rows = await self.fetchall(
                "SELECT sdg.id, sdg.prediction_id, sdg.group_name,\n                          sdg.actual_kwh, sdg.theoretical_max_kwh,\n                          COALESCE(hp.has_panel_group_actuals, 0),\n                          COALESCE(hp.inverter_clipped, 0),\n                          COALESCE(hp.mppt_throttled, 0),\n                          hsd.fusion_mode,\n                          hsd.sensor_fusion_shadow_type,\n                          hsd.shadow_type\n                   FROM shadow_detection_groups sdg\n              LEFT JOIN hourly_predictions hp\n                     ON hp.prediction_id = sdg.prediction_id\n              LEFT JOIN hourly_shadow_detection hsd\n                     ON hsd.prediction_id = sdg.prediction_id\n               ORDER BY sdg.prediction_id, sdg.id"
            )
            rows_by_prediction = {}
            for row in rows:
                rows_by_prediction.setdefault(str(row[1]), []).append(row)
            repair_updates = []
            not_evaluable_count = 0
            for prediction_rows in rows_by_prediction.values():
                reference_row = prediction_rows[0]
                reference_valid = (
                    bool(reference_row[5])
                    and (not bool(reference_row[6]))
                    and (not bool(reference_row[7]))
                    and (reference_row[8] == "solar_radiation_sensor")
                    and (reference_row[9] not in (None, "not_evaluable", "error"))
                    and (reference_row[10] not in (None, "not_evaluable", "error"))
                )
                group_performance = {
                    str(row[2]): {"actual_kwh": row[3], "theoretical_max_kwh": row[4]}
                    for row in prediction_rows
                }
                relative_group_results = compare_panel_group_performance(
                    group_performance, reference_is_valid=reference_valid
                )
                group_record_ids = {str(row[2]): int(row[0]) for row in prediction_rows}
                for group_name, relative_group_result in relative_group_results.items():
                    if not relative_group_result["evaluable"]:
                        not_evaluable_count += 1
                    repair_updates.append(
                        (
                            relative_group_result["shadow_type"],
                            relative_group_result["shadow_percent"],
                            relative_group_result["confidence"],
                            relative_group_result["root_cause"],
                            relative_group_result["efficiency_ratio"],
                            relative_group_result["loss_kwh"],
                            group_record_ids[group_name],
                        )
                    )
                continue
            if repair_updates:
                await self.executemany(
                    "UPDATE shadow_detection_groups\n                       SET shadow_type = ?, shadow_percent = ?, confidence = ?,\n                           root_cause = ?, efficiency_ratio = ?, loss_kwh = ?\n                       WHERE id = ?",
                    repair_updates,
                )
            history_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_learning_history\n                   WHERE group_name <> '_system_'"
            )
            hourly_pattern_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_pattern_hourly\n                   WHERE group_name <> '_system_'"
            )
            seasonal_pattern_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_pattern_seasonal\n                   WHERE group_name <> '_system_'"
            )
            await self.execute(
                "DELETE FROM shadow_learning_history WHERE group_name <> '_system_'"
            )
            await self.execute(
                "DELETE FROM shadow_pattern_hourly WHERE group_name <> '_system_'"
            )
            await self.execute(
                "DELETE FROM shadow_pattern_seasonal WHERE group_name <> '_system_'"
            )
            await self.execute(
                "INSERT INTO shadow_history_repair_runs\n                   (repair_version, source_rows, recalculated_rows,\n                    not_evaluable_rows, learning_history_rows_removed,\n                    hourly_pattern_rows_removed, seasonal_pattern_rows_removed)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID,
                    len(rows),
                    len(repair_updates),
                    not_evaluable_count,
                    int(history_count_row[0] or 0) if history_count_row else 0,
                    (
                        int(hourly_pattern_count_row[0] or 0)
                        if hourly_pattern_count_row
                        else 0
                    ),
                    (
                        int(seasonal_pattern_count_row[0] or 0)
                        if seasonal_pattern_count_row
                        else 0
                    ),
                ),
            )
        _LOGGER.info(
            "Shadow group history repair completed: source=%d, recalculated=%d, not_evaluable=%d",
            len(rows),
            len(repair_updates),
            not_evaluable_count,
        )
        return

    async def _migrate_shadow_observation_columns_v20260827(self) -> None:
        """Persist observation provenance that previously lived only in memory."""
        observation_column_specs = {
            "hourly_shadow_detection": (
                ("evaluable", "BOOLEAN"),
                ("learning_eligible", "BOOLEAN"),
                ("observation_class", "TEXT"),
            ),
            "shadow_learning_history": (
                ("fusion_mode", "TEXT"),
                ("observation_class", "TEXT"),
                ("evaluable", "BOOLEAN"),
                ("learning_eligible", "BOOLEAN"),
            ),
        }
        added_columns = []
        for table_name, column_specs in observation_column_specs.items():
            table_exists_row = await self.fetchone(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            if not table_exists_row:
                continue
            existing_columns = {
                row[1]
                for row in await (
                    await self._db.execute(f"PRAGMA table_info({table_name})")
                ).fetchall()
            }
            for column_name, column_definition in column_specs:
                if column_name in existing_columns:
                    continue
                await self._db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
                )
                added_columns.append(f"{table_name}.{column_name}")
        if added_columns:
            await self._db.commit()
            _LOGGER.info(
                "Shadow observation columns added: %s", ", ".join(added_columns)
            )
        return None if True else None

    async def _migrate_shadow_system_history_purge_v20260827(self) -> None:
        """Drop poisoned _system_ learning history and derived patterns."""
        async with self.transaction():
            await self.execute(
                "CREATE TABLE IF NOT EXISTS shadow_history_repair_runs (\n                       repair_version TEXT PRIMARY KEY,\n                       repaired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                       patterns_rebuilt_at TIMESTAMP,\n                       source_rows INTEGER NOT NULL,\n                       recalculated_rows INTEGER NOT NULL,\n                       not_evaluable_rows INTEGER NOT NULL,\n                       learning_history_rows_removed INTEGER NOT NULL,\n                       hourly_pattern_rows_removed INTEGER NOT NULL,\n                       seasonal_pattern_rows_removed INTEGER NOT NULL\n                   )"
            )
            repair_row = await self.fetchone(
                "SELECT 1 FROM shadow_history_repair_runs\n                   WHERE repair_version = ?",
                (SHADOW_SYSTEM_HISTORY_REPAIR_MIGRATION_ID,),
            )
            if repair_row:
                return
            history_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_learning_history\n                   WHERE group_name = '_system_'"
            )
            hourly_pattern_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_pattern_hourly\n                   WHERE group_name = '_system_'"
            )
            seasonal_pattern_count_row = await self.fetchone(
                "SELECT COUNT(*) FROM shadow_pattern_seasonal\n                   WHERE group_name = '_system_'"
            )
            history_count = int(history_count_row[0] or 0) if history_count_row else 0
            hourly_pattern_count = (
                int(hourly_pattern_count_row[0] or 0) if hourly_pattern_count_row else 0
            )
            seasonal_pattern_count = (
                int(seasonal_pattern_count_row[0] or 0)
                if seasonal_pattern_count_row
                else 0
            )
            await self.execute(
                "DELETE FROM shadow_learning_history WHERE group_name = '_system_'"
            )
            await self.execute(
                "DELETE FROM shadow_pattern_hourly WHERE group_name = '_system_'"
            )
            await self.execute(
                "DELETE FROM shadow_pattern_seasonal WHERE group_name = '_system_'"
            )
            await self.execute(
                "INSERT INTO shadow_history_repair_runs\n                   (repair_version, repaired_at, patterns_rebuilt_at, source_rows,\n                    recalculated_rows, not_evaluable_rows,\n                    learning_history_rows_removed, hourly_pattern_rows_removed,\n                    seasonal_pattern_rows_removed)\n                   VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 0, 0, ?, ?, ?)",
                (
                    SHADOW_SYSTEM_HISTORY_REPAIR_MIGRATION_ID,
                    history_count,
                    history_count,
                    hourly_pattern_count,
                    seasonal_pattern_count,
                ),
            )
        _LOGGER.info(
            "Shadow system history purge completed: history=%d, hourly=%d, seasonal=%d",
            history_count,
            hourly_pattern_count,
            seasonal_pattern_count,
        )

    async def _migrate_shadow_curtailment_observation_repair_v20260923(self) -> None:
        """Reclassify stored shadow hours that were production-limited."""
        try:
            await self._repair_curtailed_shadow_observations()
        except Exception as repair_error:
            _LOGGER.warning(
                "Curtailment shadow observation repair skipped: %s", repair_error
            )
        return None if True else None

    async def _repair_curtailed_shadow_observations(self) -> None:
        if not await self._table_exists("hourly_predictions"):
            return
        shadow_prediction_columns = await self._sqlite_column_names(
            "hourly_predictions"
        )
        if "mppt_throttle_reason" not in shadow_prediction_columns:
            return
        if (
            "target_date" not in shadow_prediction_columns
            or "target_hour" not in shadow_prediction_columns
        ):
            return None
        await self._ensure_shadow_history_repair_table()
        repair_state = await self.fetchone(
            "SELECT patterns_rebuilt_at FROM shadow_history_repair_runs\n               WHERE repair_version = ?",
            (SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,),
        )
        if repair_state is not None:
            if repair_state[0]:
                return
            await self._finish_curtailment_shadow_pattern_rebuild()
            return
        repair_targets = await self._curtailed_shadow_repair_targets()
        if not repair_targets:
            await self._record_curtailment_shadow_repair(0, rebuilt=True)
            return
        detection_columns = await self._sqlite_column_names("hourly_shadow_detection")
        shadow_group_columns = await self._sqlite_column_names(
            "shadow_detection_groups"
        )
        history_columns = await self._sqlite_column_names("shadow_learning_history")
        detection_assignments, detection_parameters = _neutral_shadow_assignment(
            detection_columns
        )
        group_assignments, group_parameters = _neutral_shadow_assignment(
            shadow_group_columns
        )
        history_assignments, history_parameters = _neutral_shadow_assignment(
            history_columns
        )
        preserved_verdicts_sql = ", ".join(
            (f"'{column_name}'" for column_name in _SHADOW_VERDICT_TYPES_LEFT_ALONE)
        )
        async with self.transaction():
            for prediction_id, target_date_value, target_hour_value in repair_targets:
                if detection_assignments:
                    await self.execute(
                        f"UPDATE hourly_shadow_detection\n                            SET {detection_assignments}\n                            WHERE prediction_id = ?",
                        tuple(detection_parameters) + (prediction_id,),
                        auto_commit=False,
                    )
                if group_assignments:
                    await self.execute(
                        f"UPDATE shadow_detection_groups\n                            SET {group_assignments}\n                            WHERE prediction_id = ?\n                              AND COALESCE(shadow_type, '') NOT IN ({preserved_verdicts_sql})",
                        tuple(group_parameters) + (prediction_id,),
                        auto_commit=False,
                    )
                if (
                    history_assignments
                    and "date" in history_columns
                    and ("hour" in history_columns)
                ):
                    await self.execute(
                        f"UPDATE shadow_learning_history\n                            SET {history_assignments}\n                            WHERE date = ? AND hour = ?",
                        tuple(history_parameters)
                        + (target_date_value, int(target_hour_value)),
                        auto_commit=False,
                    )
            await self._record_curtailment_shadow_repair(
                len(repair_targets), rebuilt=False, auto_commit=False
            )
        await self._finish_curtailment_shadow_pattern_rebuild()

    async def _curtailed_shadow_repair_targets(self) -> List[Tuple[Any, Any, Any]]:
        detection_columns = await self._sqlite_column_names("hourly_shadow_detection")
        shadow_group_columns = await self._sqlite_column_names(
            "shadow_detection_groups"
        )
        history_columns = await self._sqlite_column_names("shadow_learning_history")
        detection_filters = []
        if detection_columns:
            group_filters = ["COALESCE(hsd.shadow_type, '') != 'not_evaluable'"]
            if "learning_eligible" in detection_columns:
                group_filters.append("COALESCE(hsd.learning_eligible, 1) != 0")
            if "fusion_mode" in detection_columns:
                group_filters.append(
                    "COALESCE(hsd.fusion_mode, '') != 'production_limited'"
                )
            detection_filters.append(
                f"EXISTS (\n                       SELECT 1 FROM hourly_shadow_detection hsd\n                       WHERE hsd.prediction_id = hp.prediction_id\n                         AND ({' OR '.join(group_filters)})\n                   )"
            )
        if shadow_group_columns:
            preserved_verdicts_sql = ", ".join(
                (f"'{column_name}'" for column_name in _SHADOW_VERDICT_TYPES_LEFT_ALONE)
            )
            detection_filters.append(
                f"EXISTS (\n                        SELECT 1 FROM shadow_detection_groups sdg\n                        WHERE sdg.prediction_id = hp.prediction_id\n                          AND COALESCE(sdg.shadow_type, '') NOT IN ({preserved_verdicts_sql})\n                    )"
            )
        if {"date", "hour"}.issubset(history_columns):
            history_filters = []
            if "shadow_detected" in history_columns:
                history_filters.append("COALESCE(slh.shadow_detected, 0) != 0")
            if "learning_eligible" in history_columns:
                history_filters.append("COALESCE(slh.learning_eligible, 0) != 0")
            if "root_cause" in history_columns:
                history_filters.append(
                    "COALESCE(slh.root_cause, '') IN (\n                           'building_tree_obstruction', 'possible_obstruction'\n                       )"
                )
            if history_filters:
                detection_filters.append(
                    f"EXISTS (\n                           SELECT 1 FROM shadow_learning_history slh\n                           WHERE slh.date = hp.target_date\n                             AND slh.hour = hp.target_hour\n                             AND ({' OR '.join(history_filters)})\n                       )"
                )
        repair_targets = []
        seen_targets = set()
        if detection_filters:
            rows = await self.fetchall(
                f"SELECT hp.prediction_id, hp.target_date, hp.target_hour\n                    FROM hourly_predictions hp\n                    WHERE COALESCE(hp.mppt_throttle_reason, '') IN (\n                        {CURTAILMENT_THROTTLE_REASON_SQL}\n                    )\n                      AND ({' OR '.join(detection_filters)})"
            )
            for row in rows:
                field_name = (row[0], row[1], int(row[2]))
                if field_name not in seen_targets:
                    seen_targets.add(field_name)
                    repair_targets.append(field_name)
        flow_targets = await self._flow_curtailed_shadow_targets(seen_targets)
        repair_targets.extend(flow_targets)
        return repair_targets

    async def _flow_curtailed_shadow_targets(
        self, already_selected: set
    ) -> List[Tuple[Any, Any, Any]]:
        required_fields = {
            "date",
            "hour",
            "battery_soc",
            "solar_to_grid_w",
            "solar_to_battery_w",
            "house_consumption_w",
        }
        power_source_columns = await self._sqlite_column_names("stats_power_sources")
        detection_columns = await self._sqlite_column_names("hourly_shadow_detection")
        if not required_fields.issubset(power_source_columns) or not detection_columns:
            return []
        try:
            from ..production.production_morning_routine import (
                MorningRoutineHandler as power_source_column,
            )
        except ImportError as repair_error:
            _LOGGER.warning(
                "Flow curtailment shadow repair unavailable: %s", repair_error
            )
            return []
        rows = await self.fetchall(
            f"SELECT hp.prediction_id, hp.target_date, hp.target_hour,\n                       COUNT(*) AS samples,\n                       AVG(sps.solar_to_battery_w) AS avg_battery_w,\n                       AVG(sps.solar_to_grid_w) AS avg_solar_to_grid_w,\n                       AVG(sps.house_consumption_w) AS avg_house_w,\n                       MAX(sps.battery_soc) AS max_battery_soc,\n                       AVG(sps.battery_soc) AS avg_battery_soc\n                FROM hourly_predictions hp\n                JOIN hourly_shadow_detection hsd\n                  ON hsd.prediction_id = hp.prediction_id\n                JOIN stats_power_sources sps\n                  ON sps.date = hp.target_date AND sps.hour = hp.target_hour\n                WHERE COALESCE(hp.mppt_throttle_reason, '') NOT IN (\n                    {CURTAILMENT_THROTTLE_REASON_SQL}\n                )\n                  AND COALESCE(hsd.shadow_type, '') NOT IN (\n                    'not_evaluable', 'none', 'error', 'unknown', 'night'\n                  )\n                GROUP BY hp.prediction_id, hp.target_date, hp.target_hour"
        )
        selected_columns = []
        for row in rows:
            field_name = (row[0], row[1], int(row[2]))
            if field_name in already_selected:
                continue
            power_statistics = {
                "samples": int(row[3] or 0),
                "avg_battery_w": row[4],
                "avg_solar_to_grid_w": row[5],
                "avg_house_w": row[6],
                "max_battery_soc": row[7],
                "avg_battery_soc": row[8],
            }
            if power_source_column.shadow_curtailment_suspected(power_statistics):
                selected_columns.append(field_name)
        return selected_columns

    async def _finish_curtailment_shadow_pattern_rebuild(self) -> None:
        patterns_rebuilt = True
        if await self._table_exists("shadow_learning_history"):
            row = await self.fetchone("SELECT COUNT(*) FROM shadow_learning_history")
            if row:
                if int(row[0] or 0) > 0:
                    patterns_rebuilt = (
                        await self._recalculate_shadow_patterns_from_history()
                    )
        if not patterns_rebuilt:
            return
        await self.execute(
            "UPDATE shadow_history_repair_runs\n               SET patterns_rebuilt_at = CURRENT_TIMESTAMP\n               WHERE repair_version = ?\n                 AND patterns_rebuilt_at IS NULL",
            (SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,),
        )

    async def _recalculate_shadow_patterns_from_history(self) -> bool:
        try:
            from .data_shadow_pattern_learning import (
                ShadowPatternLearner as ShadowPatternLearner,
            )
        except ImportError as error:
            _LOGGER.warning(
                "Shadow pattern learner unavailable for curtailment repair: %s", error
            )
            return False
        pattern_learner = ShadowPatternLearner(None, self)
        await pattern_learner._load_config()
        row = await pattern_learner.recalculate_from_history(
            int(pattern_learner._config.get("rolling_window_days", 30))
        )
        if not not row.success or "No history" in (row.message or ""):
            return True
        _LOGGER.warning("Curtailment shadow pattern rebuild failed: %s", row.message)
        return False

    async def _ensure_shadow_history_repair_table(self) -> None:
        await self.execute(
            "CREATE TABLE IF NOT EXISTS shadow_history_repair_runs (\n                   repair_version TEXT PRIMARY KEY,\n                   repaired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                   patterns_rebuilt_at TIMESTAMP,\n                   source_rows INTEGER NOT NULL,\n                   recalculated_rows INTEGER NOT NULL,\n                   not_evaluable_rows INTEGER NOT NULL,\n                   learning_history_rows_removed INTEGER NOT NULL,\n                   hourly_pattern_rows_removed INTEGER NOT NULL,\n                   seasonal_pattern_rows_removed INTEGER NOT NULL\n               )"
        )

    async def _record_curtailment_shadow_repair(
        self, source_rows: int, *, rebuilt: bool, auto_commit: bool = True
    ) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO shadow_history_repair_runs\n               (repair_version, repaired_at, patterns_rebuilt_at, source_rows,\n                recalculated_rows, not_evaluable_rows,\n                learning_history_rows_removed, hourly_pattern_rows_removed,\n                seasonal_pattern_rows_removed)\n               VALUES (?, CURRENT_TIMESTAMP, CASE WHEN ? THEN CURRENT_TIMESTAMP END,\n                       ?, ?, ?, 0, 0, 0)",
            (
                SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,
                1 if rebuilt else 0,
                int(source_rows),
                int(source_rows),
                int(source_rows),
            ),
            auto_commit=auto_commit,
        )

    async def _sqlite_column_names(self, table_name: str) -> set:
        if not table_name.replace("_", "").isalnum():
            return set()
        if not await self._table_exists(table_name):
            return set()
        rows = await self.fetchall(f"PRAGMA table_info({table_name})")
        return {row[1] for row in rows}

    async def _migrate_weather_cache_snowfall_rain(self) -> None:
        """Add snowfall and rain columns to weather_cache_open_meteo. @zara"""
        try:
            async with self._db.execute(
                "PRAGMA table_info(weather_cache_open_meteo)"
            ) as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            if "snowfall" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE weather_cache_open_meteo ADD COLUMN snowfall REAL"
                )
            if "rain" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE weather_cache_open_meteo ADD COLUMN rain REAL"
                )
            if alter_statements:
                for alter_statement in alter_statements:
                    await self._db.execute(alter_statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration: Added %d columns (snowfall/rain) to weather_cache_open_meteo",
                    len(alter_statements),
                )
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration weather_cache_snowfall_rain failed: %s", migration_error
            )
        return None if True else None

    async def _migrate_weather_radiation_ensemble_v20260816(self) -> None:
        """Add ICON radiation and Bright Sky solar columns."""
        try:
            async with self._db.execute(
                "PRAGMA table_info(weather_cache_open_meteo)"
            ) as cursor:
                weather_forecast_columns = {
                    schema_row[1] for schema_row in await cursor.fetchall()
                }
            radiation_column_definitions = {
                "ghi_icon_d2": "REAL",
                "direct_radiation_icon_d2": "REAL",
                "diffuse_radiation_icon_d2": "REAL",
                "ghi_icon_eu": "REAL",
                "direct_radiation_icon_eu": "REAL",
                "diffuse_radiation_icon_eu": "REAL",
                "ghi_source_identity": "TEXT",
            }
            for column_name, column_definition in radiation_column_definitions.items():
                if column_name not in weather_forecast_columns:
                    await self._db.execute(
                        f"ALTER TABLE weather_cache_open_meteo ADD COLUMN {column_name} {column_definition}"
                    )
            async with self._db.execute(
                "PRAGMA table_info(weather_cache_bright_sky)"
            ) as cursor:
                weather_cache_columns = {
                    schema_row[1] for schema_row in await cursor.fetchall()
                }
            source_column_definitions = {"solar_wm2": "REAL", "sunshine_min": "REAL"}
            for column_name, column_definition in source_column_definitions.items():
                if column_name not in weather_cache_columns:
                    await self._db.execute(
                        f"ALTER TABLE weather_cache_bright_sky ADD COLUMN {column_name} {column_definition}"
                    )
            await self._db.commit()
            _LOGGER.debug("Migration: weather radiation ensemble columns ready")
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration weather_radiation_ensemble_v20260816 failed: %s",
                migration_error,
            )
        return None if True else None

    async def _migrate_weather_radiation_snapshots_v20260822(self) -> None:
        """Append-only GHI retrieval and ensemble provenance sidecar."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS weather_radiation_snapshots (\n                   snapshot_id TEXT PRIMARY KEY,\n                   contract_version TEXT,\n                   snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('provider_fetch', 'ensemble_compose')),\n                   retrieved_at TIMESTAMP NOT NULL,\n                   provider_run_at TIMESTAMP,\n                   target_datetime TIMESTAMP NOT NULL,\n                   target_date DATE NOT NULL,\n                   target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                   lead_hours INTEGER,\n                   raw_blend_ghi REAL,\n                   residual_scale REAL,\n                   kalman_applied BOOLEAN,\n                   kalman_bias REAL,\n                   final_ghi REAL,\n                   final_dni REAL,\n                   final_dhi REAL,\n                   created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        async with self._db.execute(
            "PRAGMA table_info(weather_radiation_snapshots)"
        ) as cursor:
            prediction_columns = {
                schema_row[1] for schema_row in await cursor.fetchall()
            }
        if "contract_version" not in prediction_columns:
            await self._db.execute(
                "ALTER TABLE weather_radiation_snapshots ADD COLUMN contract_version TEXT"
            )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weather_radiation_snapshots_target\n               ON weather_radiation_snapshots(target_date, target_hour, retrieved_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weather_radiation_snapshots_kind\n               ON weather_radiation_snapshots(snapshot_kind, retrieved_at)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS weather_radiation_snapshot_sources (\n                   snapshot_id TEXT NOT NULL,\n                   source_name TEXT NOT NULL CHECK(source_name IN ('ifs', 'icon_d2', 'icon_eu', 'bright_sky')),\n                   source_identity TEXT,\n                   available BOOLEAN NOT NULL,\n                   ghi REAL,\n                   dni REAL,\n                   dhi REAL,\n                   weight_used REAL,\n                   PRIMARY KEY (snapshot_id, source_name),\n                   FOREIGN KEY (snapshot_id) REFERENCES weather_radiation_snapshots(snapshot_id)\n                       ON DELETE RESTRICT\n               )"
        )
        async with self._db.execute(
            "PRAGMA table_info(weather_radiation_snapshot_sources)"
        ) as cursor:
            radiation_snapshot_columns = {
                schema_row[1] for schema_row in await cursor.fetchall()
            }
        if "source_identity" not in radiation_snapshot_columns:
            await self._db.execute(
                "ALTER TABLE weather_radiation_snapshot_sources ADD COLUMN source_identity TEXT"
            )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weather_radiation_snapshot_sources_source\n               ON weather_radiation_snapshot_sources(source_name, available)"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshots_no_update\n               BEFORE UPDATE ON weather_radiation_snapshots\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshots are append-only');\n               END"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshots_no_duplicate_insert\n               BEFORE INSERT ON weather_radiation_snapshots\n               WHEN EXISTS (\n                   SELECT 1 FROM weather_radiation_snapshots\n                    WHERE snapshot_id = NEW.snapshot_id\n               )\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshots are append-only');\n               END"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshots_no_delete\n               BEFORE DELETE ON weather_radiation_snapshots\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshots are append-only');\n               END"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshot_sources_no_update\n               BEFORE UPDATE ON weather_radiation_snapshot_sources\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshot sources are append-only');\n               END"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshot_sources_no_duplicate_insert\n               BEFORE INSERT ON weather_radiation_snapshot_sources\n               WHEN EXISTS (\n                   SELECT 1 FROM weather_radiation_snapshot_sources\n                    WHERE snapshot_id = NEW.snapshot_id\n                      AND source_name = NEW.source_name\n               )\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshot sources are append-only');\n               END"
        )
        await self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_weather_radiation_snapshot_sources_no_delete\n               BEFORE DELETE ON weather_radiation_snapshot_sources\n               BEGIN\n                   SELECT RAISE(ABORT, 'weather radiation snapshot sources are append-only');\n               END"
        )
        await self._db.commit()
        return

    async def insert_weather_radiation_snapshot(
        self,
        *,
        snapshot_kind: str,
        retrieved_at: str,
        target_datetime: str,
        target_date: str,
        target_hour: int,
        sources: Sequence[Mapping[str, Any]],
        provider_run_at: Optional[str] = None,
        lead_hours: Optional[int] = None,
        raw_blend_ghi: Optional[float] = None,
        residual_scale: Optional[float] = None,
        kalman_applied: Optional[bool] = None,
        kalman_bias: Optional[float] = None,
        final_ghi: Optional[float] = None,
        final_dni: Optional[float] = None,
        final_dhi: Optional[float] = None,
        snapshot_id: Optional[str] = None,
    ) -> str:
        """Insert one immutable retrieval or ensemble radiation snapshot."""
        snapshot_identifier = snapshot_id or str(uuid.uuid4())
        source_list = tuple(sources)
        if not source_list:
            raise ValueError("radiation snapshot requires source rows")
        async with self.transaction():
            await self.execute(
                "INSERT INTO weather_radiation_snapshots (\n                       snapshot_id, contract_version, snapshot_kind,\n                       retrieved_at, provider_run_at,\n                       target_datetime, target_date, target_hour, lead_hours,\n                       raw_blend_ghi, residual_scale, kalman_applied, kalman_bias,\n                       final_ghi, final_dni, final_dhi\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_identifier,
                    WEATHER_RADIATION_SNAPSHOT_CONTRACT_VERSION,
                    snapshot_kind,
                    retrieved_at,
                    provider_run_at,
                    target_datetime,
                    target_date,
                    int(target_hour),
                    lead_hours,
                    raw_blend_ghi,
                    residual_scale,
                    None if kalman_applied is None else int(bool(kalman_applied)),
                    kalman_bias,
                    final_ghi,
                    final_dni,
                    final_dhi,
                ),
                auto_commit=False,
            )
            for source in source_list:
                await self.execute(
                    "INSERT INTO weather_radiation_snapshot_sources (\n                           snapshot_id, source_name, source_identity,\n                           available, ghi, dni, dhi, weight_used\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_identifier,
                        source["source_name"],
                        source.get("source_identity"),
                        int(bool(source.get("available"))),
                        source.get("ghi"),
                        source.get("dni"),
                        source.get("dhi"),
                        source.get("weight_used"),
                    ),
                    auto_commit=False,
                )
        return snapshot_identifier if True else None

    async def _migrate_weather_forecast_usage_v20260823(self) -> None:
        """Create immutable corrected-weather and morning-usage provenance."""
        async with self._db.execute("PRAGMA table_info(weather_forecast)") as cursor:
            correction_snapshot_columns = {row[1] for row in await cursor.fetchall()}
        if "correction_snapshot_id" not in correction_snapshot_columns:
            await self._db.execute(
                "ALTER TABLE weather_forecast ADD COLUMN correction_snapshot_id TEXT"
            )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS weather_forecast_correction_snapshots (\n                   correction_snapshot_id TEXT PRIMARY KEY,\n                   contract_version TEXT NOT NULL,\n                   radiation_snapshot_id TEXT NOT NULL,\n                   corrected_at TIMESTAMP NOT NULL,\n                   target_datetime TIMESTAMP NOT NULL,\n                   target_date DATE NOT NULL,\n                   target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                   weather_type TEXT,\n                   observation_mode TEXT NOT NULL CHECK(observation_mode IN ('measured_local', 'provider_only')),\n                   local_observation_available BOOLEAN NOT NULL,\n                   raw_blend_ghi REAL,\n                   ensemble_ghi REAL,\n                   ensemble_dni REAL,\n                   ensemble_dhi REAL,\n                   learned_solar_factor REAL,\n                   static_solar_factor_applied REAL NOT NULL DEFAULT 1.0,\n                   mlp_factor REAL,\n                   mlp_confidence REAL NOT NULL DEFAULT 0.0,\n                   combined_solar_factor_applied REAL NOT NULL DEFAULT 1.0,\n                   coherence_cap_applied BOOLEAN NOT NULL DEFAULT FALSE,\n                   coherence_cap_scale REAL NOT NULL DEFAULT 1.0,\n                   kalman_applied BOOLEAN NOT NULL DEFAULT FALSE,\n                   kalman_bias REAL NOT NULL DEFAULT 0.0,\n                   ghi_coherence_cap_eligible INTEGER\n                       CHECK(\n                           ghi_coherence_cap_eligible IS NULL\n                           OR ghi_coherence_cap_eligible IN (0, 1)\n                       ),\n                   final_used_ghi REAL,\n                   final_used_dni REAL,\n                   final_used_dhi REAL,\n                   final_used_temperature REAL,\n                   final_used_humidity REAL,\n                   final_used_clouds REAL,\n                   final_used_rain REAL,\n                   final_used_wind REAL,\n                   final_used_pressure REAL,\n                   temperature REAL,\n                   humidity REAL,\n                   clouds REAL,\n                   rain REAL,\n                   wind REAL,\n                   pressure REAL,\n                   cloud_cover_low REAL,\n                   cloud_cover_mid REAL,\n                   cloud_cover_high REAL,\n                   visibility_m REAL,\n                   sun_elevation REAL,\n                   created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                   FOREIGN KEY (radiation_snapshot_id)\n                       REFERENCES weather_radiation_snapshots(snapshot_id) ON DELETE RESTRICT\n               )"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weather_forecast_correction_target\n               ON weather_forecast_correction_snapshots(target_date, target_hour, corrected_at)"
        )
        async with self._db.execute(
            "PRAGMA table_info(weather_forecast_correction_snapshots)"
        ) as cursor:
            usage_snapshot_columns = {row[1] for row in await cursor.fetchall()}
        for column_name in (
            "final_used_temperature",
            "final_used_humidity",
            "final_used_clouds",
            "final_used_rain",
            "final_used_wind",
            "final_used_pressure",
        ):
            if column_name not in usage_snapshot_columns:
                await self._db.execute(
                    f"ALTER TABLE weather_forecast_correction_snapshots ADD COLUMN {column_name} REAL"
                )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS weather_forecast_usage_snapshots (\n                   usage_snapshot_id TEXT PRIMARY KEY,\n                   contract_version TEXT NOT NULL,\n                   prediction_id TEXT NOT NULL,\n                   morning_batch_id TEXT NOT NULL,\n                   correction_snapshot_id TEXT NOT NULL,\n                   locked_at TIMESTAMP NOT NULL,\n                   target_datetime TIMESTAMP NOT NULL,\n                   target_date DATE NOT NULL,\n                   target_hour INTEGER NOT NULL CHECK(target_hour >= 0 AND target_hour <= 23),\n                   created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                   UNIQUE(morning_batch_id, prediction_id),\n                   FOREIGN KEY (correction_snapshot_id)\n                       REFERENCES weather_forecast_correction_snapshots(correction_snapshot_id)\n                       ON DELETE RESTRICT\n               )"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_weather_forecast_usage_target\n               ON weather_forecast_usage_snapshots(target_date, target_hour, locked_at)"
        )
        for trigger_name, table_name, operation_name, error_message in (
            (
                "trg_weather_forecast_correction_no_update",
                "weather_forecast_correction_snapshots",
                "UPDATE",
                "weather forecast correction snapshots are append-only",
            ),
            (
                "trg_weather_forecast_correction_no_delete",
                "weather_forecast_correction_snapshots",
                "DELETE",
                "weather forecast correction snapshots are append-only",
            ),
            (
                "trg_weather_forecast_usage_no_update",
                "weather_forecast_usage_snapshots",
                "UPDATE",
                "weather forecast usage snapshots are append-only",
            ),
            (
                "trg_weather_forecast_usage_no_delete",
                "weather_forecast_usage_snapshots",
                "DELETE",
                "weather forecast usage snapshots are append-only",
            ),
        ):
            await self._db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger_name}\n                    BEFORE {operation_name} ON {table_name}\n                    BEGIN SELECT RAISE(ABORT, '{error_message}'); END"
            )
        for table_name, precision_column_definitions in (
            (
                "weather_precision_daily",
                {
                    "usage_snapshot_id": "TEXT",
                    "forecast_contract_version": "TEXT",
                    "solar_sample_eligible": "BOOLEAN NOT NULL DEFAULT FALSE",
                    "solar_exclusion_reason": "TEXT",
                },
            ),
            (
                "weather_precision_daily_summary",
                {
                    "solar_sample_hours": "INTEGER NOT NULL DEFAULT 0",
                    "ghi_mae_wm2": "REAL",
                    "ghi_rmse_wm2": "REAL",
                    "ghi_bias_wm2": "REAL",
                    "ghi_nmae_percent": "REAL",
                    "weather_metric_basis": "TEXT",
                    "forecast_contract_version": "TEXT",
                },
            ),
            ("ai_weather_mlp_meta", {"training_contract_version": "TEXT"}),
        ):
            async with self._db.execute(f"PRAGMA table_info({table_name})") as cursor:
                column_names = {row[1] for row in await cursor.fetchall()}
            for column_name, column_definition in precision_column_definitions.items():
                if column_name not in column_names:
                    await self._db.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
                    )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        migration_id = "weather_forecast_usage_learning_reset_v20260823"
        async with self._db.execute(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?", (migration_id,)
        ) as cursor:
            precision_row = await cursor.fetchone()
        if not precision_row:
            local_ghi_prior = None
            try:
                local_ghi_prior = await self._compute_ghi_local_update_prior()
            except Exception as repair_error:
                _LOGGER.warning(
                    "Local GHI update prior unavailable during learning reset: %s",
                    repair_error,
                )
            await self._db.execute(
                "UPDATE weather_precision_factors\n                   SET temperature_factor = 0.0,\n                       cloud_factor = 1.0,\n                       wind_factor = 1.0,\n                       humidity_factor = 1.0,\n                       rain_factor = 1.0,\n                       pressure_factor = 0.0,\n                       updated_at = CURRENT_TIMESTAMP"
            )
            if local_ghi_prior is not None:
                await self._db.execute(
                    "UPDATE weather_precision_factors\n                       SET solar_factor = ?,\n                           sample_days = ?,\n                           confidence = ?,\n                           updated_at = CURRENT_TIMESTAMP",
                    (
                        float(local_ghi_prior["solar_factor"]),
                        int(local_ghi_prior["sample_days"]),
                        float(local_ghi_prior["confidence"]),
                    ),
                )
            await self._db.execute("DELETE FROM weather_precision_hourly_factors")
            await self._db.execute("DELETE FROM weather_precision_weather_specific")
            await self._db.execute(
                "UPDATE ai_weather_mlp_meta SET training_contract_version = NULL WHERE id = 1"
            )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES (?)",
                (migration_id,),
            )
            _LOGGER.info(
                "Migration: quarantined mutable-cache weather learning; immutable warm-up started"
            )
        await self._db.commit()

    async def _migrate_weather_expert_learning_quarantine_v20260824(self) -> None:
        """Quarantine mutable-cache expert learning and restore static weights once."""
        for table_name in ("weather_expert_weights", "weather_expert_learning"):
            async with self._db.execute(f"PRAGMA table_info({table_name})") as cursor:
                column_names = {row[1] for row in await cursor.fetchall()}
            if "learning_contract_version" not in column_names:
                await self._db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN learning_contract_version TEXT NOT NULL DEFAULT '{WEATHER_EXPERT_LEGACY_CONTRACT_VERSION}'"
                )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        await self._db.commit()
        migration_id = "weather_expert_learning_quarantine_v20260824"
        async with self._db.execute(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?", (migration_id,)
        ) as cursor:
            precision_row = await cursor.fetchone()
        if precision_row:
            return
        default_expert_weights = tuple(
            (
                (
                    cloud_type,
                    expert_name,
                    float(weight),
                    WEATHER_EXPERT_STATIC_CONTRACT_VERSION,
                )
                for cloud_type, expert_weights in sorted(DEFAULT_EXPERT_WEIGHTS.items())
                for expert_name, weight in sorted(expert_weights.items())
            )
        )
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            await self._db.execute(
                "UPDATE weather_expert_learning\n                   SET learning_contract_version = ?",
                (WEATHER_EXPERT_LEGACY_CONTRACT_VERSION,),
            )
            await self._db.executemany(
                "INSERT INTO weather_expert_weights (\n                       cloud_type, expert_name, weight,\n                       learning_contract_version, last_updated\n                   ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(cloud_type, expert_name) DO UPDATE SET\n                       weight = excluded.weight,\n                       learning_contract_version = excluded.learning_contract_version,\n                       last_updated = CURRENT_TIMESTAMP",
                default_expert_weights,
            )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES (?)",
                (migration_id,),
            )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        _LOGGER.info(
            "Migration: quarantined mutable-cache weather expert learning; restored %d static expert weights",
            len(default_expert_weights),
        )

    async def _table_exists(self, table_name: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ) as cursor:
            return await cursor.fetchone() is not None
        return None if True else None

    async def _ghi_window_bounds(self) -> tuple[str, str]:
        current_time = SafeDateTimeUtil.now()
        current_date = (current_time - timedelta(days=1)).strftime("%Y-%m-%d")
        window_start_date = (
            current_time - timedelta(days=GHI_UPDATE_PRIOR_WINDOW_DAYS)
        ).strftime("%Y-%m-%d")
        return (window_start_date, current_date)

    async def _has_current_local_ghi_actuals(self, end_date: str) -> bool:
        async with self._db.execute(
            "SELECT COUNT(*)\n                 FROM hourly_weather_actual\n                WHERE date = ?\n                  AND hour BETWEEN ? AND ?\n                  AND solar_radiation_wm2 IS NOT NULL",
            (end_date, GHI_UPDATE_PRIOR_HOUR_MIN, GHI_UPDATE_PRIOR_HOUR_MAX),
        ) as cursor:
            row = await cursor.fetchone()
        return int((row[0] if row else 0) or 0) >= GHI_UPDATE_PRIOR_MIN_HOURS

    async def _fetch_ghi_prior_rows(self, forecast_expr: str, joins: str) -> tuple:
        window_start_date, current_date = await self._ghi_window_bounds()
        async with self._db.execute(
            f"SELECT h.date, h.hour, {forecast_expr}, h.solar_radiation_wm2\n                  FROM hourly_weather_actual h\n                  {joins}\n                 WHERE h.date >= ?\n                   AND h.date <= ?\n                   AND h.hour BETWEEN ? AND ?\n                   AND h.solar_radiation_wm2 IS NOT NULL",
            (
                window_start_date,
                current_date,
                GHI_UPDATE_PRIOR_HOUR_MIN,
                GHI_UPDATE_PRIOR_HOUR_MAX,
            ),
        ) as cursor:
            return tuple(await cursor.fetchall())
        return None if True else None

    async def _compute_ghi_local_update_prior(self) -> Optional[Dict[str, Any]]:
        """Build a one-shot GHI prior from local actuals in the last 7 days."""
        if not await self._table_exists("hourly_weather_actual"):
            return
        window_start_date, current_date = await self._ghi_window_bounds()
        if not await self._has_current_local_ghi_actuals(current_date):
            return
        correction_snapshots_exist = await self._table_exists(
            "weather_forecast_correction_snapshots"
        )
        usage_snapshots_exist = await self._table_exists(
            "weather_forecast_usage_snapshots"
        )
        baseline_ghi_sql = None
        if usage_snapshots_exist and correction_snapshots_exist:
            baseline_ghi_sql = "COALESCE(\n                (SELECT COALESCE(c_used.raw_blend_ghi, c_used.ensemble_ghi)\n                   FROM weather_forecast_usage_snapshots u\n                   JOIN weather_forecast_correction_snapshots c_used\n                     ON c_used.correction_snapshot_id = u.correction_snapshot_id\n                  WHERE u.target_date = h.date AND u.target_hour = h.hour\n                    AND COALESCE(c_used.raw_blend_ghi, c_used.ensemble_ghi) IS NOT NULL\n                  LIMIT 1),\n                (SELECT COALESCE(c_first.raw_blend_ghi, c_first.ensemble_ghi)\n                   FROM weather_forecast_correction_snapshots c_first\n                  WHERE c_first.target_date = h.date\n                    AND c_first.target_hour = h.hour\n                    AND COALESCE(c_first.raw_blend_ghi, c_first.ensemble_ghi) IS NOT NULL\n                  ORDER BY c_first.corrected_at ASC\n                  LIMIT 1)\n            )"
        elif correction_snapshots_exist:
            baseline_ghi_sql = "(\n                SELECT COALESCE(c_first.raw_blend_ghi, c_first.ensemble_ghi)\n                  FROM weather_forecast_correction_snapshots c_first\n                 WHERE c_first.target_date = h.date\n                   AND c_first.target_hour = h.hour\n                   AND COALESCE(c_first.raw_blend_ghi, c_first.ensemble_ghi) IS NOT NULL\n                 ORDER BY c_first.corrected_at ASC\n                 LIMIT 1\n            )"
        if baseline_ghi_sql is not None:
            computed_prior = compute_ghi_update_prior(
                await self._fetch_ghi_prior_rows(baseline_ghi_sql, "")
            )
            if computed_prior is not None:
                return computed_prior
        solar_factor = 1.0
        if await self._table_exists("weather_precision_factors"):
            async with self._db.execute(
                "SELECT solar_factor FROM weather_precision_factors WHERE id = 1"
            ) as cursor:
                stored_payload = await cursor.fetchone()
            if stored_payload is not None and stored_payload[0] is not None:
                try:
                    solar_factor = float(stored_payload[0])
                except (TypeError, ValueError):
                    solar_factor = 1.0
        if math.isfinite(solar_factor) and abs(solar_factor - 1.0) > 0.001:
            return
        if not await self._table_exists("weather_forecast"):
            return
        return compute_ghi_update_prior(
            await self._fetch_ghi_prior_rows(
                "wf.solar_radiation_wm2",
                "LEFT JOIN weather_forecast wf\n                     ON wf.forecast_date = h.date AND wf.hour = h.hour",
            )
        )

    async def _migrate_weather_ghi_local_prior_v20260824(self) -> None:
        """Replace a neutral solar-factor reset with a local 7-day GHI prior once."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS migration_applied (\n                   migration_id TEXT PRIMARY KEY,\n                   applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS weather_precision_factors (\n                   id INTEGER PRIMARY KEY CHECK (id = 1),\n                   temperature_factor REAL DEFAULT 0.0,\n                   solar_factor REAL DEFAULT 1.0,\n                   cloud_factor REAL DEFAULT 1.0,\n                   wind_factor REAL DEFAULT 1.0,\n                   humidity_factor REAL DEFAULT 1.0,\n                   rain_factor REAL DEFAULT 1.0,\n                   pressure_factor REAL DEFAULT 0.0,\n                   sample_days INTEGER DEFAULT 0,\n                   confidence REAL DEFAULT 0.0,\n                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n               )"
        )
        async with self._db.execute(
            "SELECT 1 FROM migration_applied WHERE migration_id = ?",
            (WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID,),
        ) as cursor:
            if await cursor.fetchone():
                return
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                "SELECT 1 FROM migration_applied WHERE migration_id = ?",
                (WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID,),
            ) as cursor:
                if await cursor.fetchone():
                    await self._db.commit()
                    return
            local_ghi_prior = await self._compute_ghi_local_update_prior()
            if local_ghi_prior is not None:
                await self._db.execute(
                    "INSERT INTO weather_precision_factors (\n                           id, temperature_factor, solar_factor, cloud_factor,\n                           wind_factor, humidity_factor, rain_factor,\n                           pressure_factor, sample_days, confidence, updated_at\n                       ) VALUES (1, 0.0, ?, 1.0, 1.0, 1.0, 1.0, 0.0, ?, ?,\n                                 CURRENT_TIMESTAMP)\n                       ON CONFLICT(id) DO UPDATE SET\n                           solar_factor = excluded.solar_factor,\n                           sample_days = excluded.sample_days,\n                           confidence = excluded.confidence,\n                           updated_at = CURRENT_TIMESTAMP",
                    (
                        float(local_ghi_prior["solar_factor"]),
                        int(local_ghi_prior["sample_days"]),
                        float(local_ghi_prior["confidence"]),
                    ),
                )
                _LOGGER.info(
                    "Migration: applied local GHI update prior solar_factor=%.3f sample_days=%d confidence=%.2f",
                    float(local_ghi_prior["solar_factor"]),
                    int(local_ghi_prior["sample_days"]),
                    float(local_ghi_prior["confidence"]),
                )
            else:
                _LOGGER.info(
                    "Migration: no local GHI actuals for a 7-day update prior; solar factor left unchanged"
                )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES (?)",
                (WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID,),
            )
            await self._db.commit()
        except Exception as repair_error:
            await self._db.rollback()
            _LOGGER.warning(
                "Migration %s deferred after error: %s",
                WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID,
                repair_error,
            )
        return None if True else None

    async def _ensure_ghi_local_prior_until_rolling_ready(self) -> None:
        """Restore a local GHI prior while the new contract window is too thin."""
        if not await self._table_exists("weather_precision_factors"):
            return
        async with self._db.execute(
            "SELECT solar_factor, sample_days, confidence\n               FROM weather_precision_factors\n               WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        sample_days = int((row[1] if row else 0) or 0)
        confidence = float((row[2] if row else 0.0) or 0.0)
        if solar_precision_window_is_apply_ready(sample_days, confidence):
            return
        try:
            local_ghi_prior = await self._compute_ghi_local_update_prior()
        except Exception as repair_error:
            _LOGGER.warning("Local GHI prior refresh skipped: %s", repair_error)
            return
        if local_ghi_prior is None:
            return
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                "SELECT sample_days, confidence\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            ) as cursor:
                current_precision_row = await cursor.fetchone()
            current_sample_days = int(
                (current_precision_row[0] if current_precision_row else 0) or 0
            )
            current_confidence = float(
                (current_precision_row[1] if current_precision_row else 0.0) or 0.0
            )
            if solar_precision_window_is_apply_ready(
                current_sample_days, current_confidence
            ):
                await self._db.commit()
                return
            await self._db.execute(
                "INSERT INTO weather_precision_factors (\n                       id, temperature_factor, solar_factor, cloud_factor,\n                       wind_factor, humidity_factor, rain_factor,\n                       pressure_factor, sample_days, confidence, updated_at\n                   ) VALUES (1, 0.0, ?, 1.0, 1.0, 1.0, 1.0, 0.0, ?, ?,\n                             CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       solar_factor = excluded.solar_factor,\n                       sample_days = excluded.sample_days,\n                       confidence = excluded.confidence,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    float(local_ghi_prior["solar_factor"]),
                    int(local_ghi_prior["sample_days"]),
                    float(local_ghi_prior["confidence"]),
                ),
            )
            await self._db.commit()
        except Exception as repair_error:
            await self._db.rollback()
            _LOGGER.warning(
                "Local GHI prior restore deferred after error: %s", repair_error
            )
            return
        _LOGGER.info(
            "Restored local GHI prior solar_factor=%.3f sample_days=%d confidence=%.2f until rolling window is apply-ready",
            float(local_ghi_prior["solar_factor"]),
            int(local_ghi_prior["sample_days"]),
            float(local_ghi_prior["confidence"]),
        )

    async def insert_weather_forecast_correction_snapshot(
        self,
        *,
        target_datetime: str,
        target_date: str,
        target_hour: int,
        data: Mapping[str, Any],
        weather_type: str,
    ) -> str:
        """Persist the complete immutable correction chain for one forecast hour."""
        correction_snapshot_id = str(uuid.uuid4())
        timestamp_iso = SafeDateTimeUtil.now().isoformat()
        await self.execute(
            "INSERT INTO weather_forecast_correction_snapshots (\n                   correction_snapshot_id, contract_version, radiation_snapshot_id,\n                   corrected_at, target_datetime, target_date, target_hour,\n                   weather_type, observation_mode, local_observation_available,\n                   raw_blend_ghi, ensemble_ghi, ensemble_dni, ensemble_dhi,\n                   learned_solar_factor, static_solar_factor_applied,\n                   mlp_factor, mlp_confidence, combined_solar_factor_applied,\n                   coherence_cap_applied, coherence_cap_scale,\n                   kalman_applied, kalman_bias, ghi_coherence_cap_eligible,\n                   final_used_ghi, final_used_dni, final_used_dhi,\n                   final_used_temperature, final_used_humidity,\n                   final_used_clouds, final_used_rain, final_used_wind,\n                   final_used_pressure,\n                   temperature, humidity, clouds, rain, wind, pressure,\n                   cloud_cover_low, cloud_cover_mid, cloud_cover_high,\n                   visibility_m, sun_elevation\n               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n                         ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                correction_snapshot_id,
                WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
                data.get("_sfml_radiation_snapshot_id"),
                timestamp_iso,
                target_datetime,
                target_date,
                int(target_hour),
                weather_type,
                data.get("_sfml_observation_mode", "provider_only"),
                int(bool(data.get("_sfml_local_observation_available"))),
                data.get("_sfml_raw_blend_ghi"),
                data.get("_sfml_ensemble_ghi"),
                data.get("_sfml_ensemble_dni"),
                data.get("_sfml_ensemble_dhi"),
                data.get("_sfml_learned_solar_factor"),
                data.get("_sfml_static_solar_factor_applied", 1.0),
                data.get("mlp_correction_factor"),
                data.get("_sfml_mlp_confidence_applied", 0.0),
                data.get("_sfml_combined_solar_factor_applied", 1.0),
                int(bool(data.get("_sfml_coherence_cap_applied"))),
                data.get("_sfml_coherence_cap_scale", 1.0),
                int(bool(data.get("_sfml_kalman_applied"))),
                data.get("_sfml_kalman_bias", 0.0),
                (
                    int(data["_sfml_ghi_coherence_cap_eligible"])
                    if data.get("_sfml_ghi_coherence_cap_eligible") is not None
                    else None
                ),
                data.get("solar_radiation_wm2"),
                data.get("direct_radiation"),
                data.get("diffuse_radiation"),
                data.get("temperature"),
                data.get("humidity"),
                data.get("clouds"),
                data.get("rain"),
                data.get("wind"),
                data.get("pressure"),
                data.get("_sfml_input_temperature"),
                data.get("_sfml_input_humidity"),
                data.get("_sfml_input_clouds"),
                data.get("_sfml_input_rain"),
                data.get("_sfml_input_wind"),
                data.get("_sfml_input_pressure"),
                data.get("cloud_cover_low"),
                data.get("cloud_cover_mid"),
                data.get("cloud_cover_high"),
                data.get("visibility_m"),
                data.get("sun_elevation"),
            ),
        )
        return correction_snapshot_id

    async def insert_weather_forecast_usage_snapshot(
        self,
        *,
        prediction_id: str,
        morning_batch_id: str,
        correction_snapshot_id: str,
        locked_at: Any,
        target_datetime: str,
        target_date: str,
        target_hour: int,
    ) -> str:
        """Lock one morning prediction to the exact corrected-weather snapshot."""
        if not correction_snapshot_id:
            raise ValueError("morning weather usage requires correction_snapshot_id")
        usage_snapshot_id = str(uuid.uuid4())
        await self.execute(
            "INSERT INTO weather_forecast_usage_snapshots (\n                   usage_snapshot_id, contract_version, prediction_id,\n                   morning_batch_id, correction_snapshot_id, locked_at,\n                   target_datetime, target_date, target_hour\n               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                usage_snapshot_id,
                WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                prediction_id,
                morning_batch_id,
                correction_snapshot_id,
                locked_at,
                target_datetime,
                target_date,
                int(target_hour),
            ),
        )
        return usage_snapshot_id

    async def _migrate_hourly_weather_actual_condition(self) -> None:
        """Add condition column to hourly_weather_actual if missing."""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "condition" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE hourly_weather_actual ADD COLUMN condition TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Added 'condition' column to hourly_weather_actual")
        return

    async def _migrate_hourly_predictions_v16(self) -> None:
        """Add V16 fields to hourly_predictions if missing. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        alter_statements = []
        if "weather_alert_type" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN weather_alert_type TEXT"
            )
        if "exclude_from_learning" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN exclude_from_learning BOOLEAN DEFAULT FALSE"
            )
        if "exclude_from_clean_evaluation" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN exclude_from_clean_evaluation BOOLEAN DEFAULT FALSE"
            )
        if "mppt_throttled" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN mppt_throttled BOOLEAN DEFAULT FALSE"
            )
        if "mppt_throttle_reason" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN mppt_throttle_reason TEXT"
            )
        if "weather_miss_class" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN weather_miss_class TEXT"
            )
        if "reforecast_trigger_eligible" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN reforecast_trigger_eligible BOOLEAN DEFAULT FALSE"
            )
        if "has_panel_group_actuals" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN has_panel_group_actuals BOOLEAN DEFAULT FALSE"
            )
        if "panel_group_predictions_backfilled" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN panel_group_predictions_backfilled BOOLEAN DEFAULT FALSE"
            )
        if "adaptive_corrected" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN adaptive_corrected BOOLEAN DEFAULT FALSE"
            )
        if "adaptive_correction_time" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN adaptive_correction_time TIMESTAMP"
            )
        if alter_statements:
            for alter_statement in alter_statements:
                await self._db.execute(alter_statement)
            await self._db.commit()
            _LOGGER.info(
                "Migration: Added %d V16 fields to hourly_predictions",
                len(alter_statements),
            )
        await self._backfill_hourly_prediction_exclusion_semantics()
        return

    async def _backfill_hourly_prediction_exclusion_semantics(self) -> None:
        """Separate learning exclusion from clean-evaluation exclusion.

        Runs on every startup, so it must leave quarantined hours alone: a
        learnable weather miss on a curtailed, backfill-blocked or otherwise
        quarantined hour is still not learnable. It must also stay write-free
        once the flags are consistent: every written row makes the commit wait
        for the exclusive lock behind whatever else reads the database at
        Home Assistant startup.
        """
        placeholders = ",".join(
            ("?" for unused_item in LEARNABLE_WEATHER_MISS_ALERT_TYPES)
        )
        quarantine_expression = "CASE\n                           WHEN COALESCE(mppt_throttled, 0) = 1\n                             OR COALESCE(inverter_clipped, 0) = 1\n                             OR COALESCE(exclude_from_learning, 0) = 1\n                             OR COALESCE(mppt_throttle_reason, '') != ''\n                             OR weather_alert_type = 'snow_covered_panels'\n                           THEN 1 ELSE 0\n                       END"
        try:
            await self._db.execute(
                f"UPDATE hourly_predictions\n                    SET exclude_from_learning = 0,\n                        weather_miss_class = COALESCE(weather_miss_class, ?),\n                        reforecast_trigger_eligible = 0\n                    WHERE weather_alert_type IN ({placeholders})\n                      AND COALESCE(mppt_throttled, 0) = 0\n                      AND COALESCE(inverter_clipped, 0) = 0\n                      AND COALESCE(mppt_throttle_reason, '')\n                          NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                      AND COALESCE(actual_backfill_blocked, 0) = 0\n                      AND data_quarantine_reason IS NULL\n                      AND (COALESCE(exclude_from_learning, 0) != 0\n                           OR weather_miss_class IS NULL\n                           OR COALESCE(reforecast_trigger_eligible, 0) != 0)",
                (
                    WEATHER_MISS_CLASS_LEGACY_FORECAST_MISS,
                    *tuple(LEARNABLE_WEATHER_MISS_ALERT_TYPES),
                ),
            )
            await self._db.execute(
                f"UPDATE hourly_predictions\n                   SET exclude_from_clean_evaluation = {quarantine_expression}\n                   WHERE actual_kwh IS NOT NULL\n                     AND COALESCE(exclude_from_clean_evaluation, -1)\n                         != {quarantine_expression}"
            )
            await self._db.commit()
        except Exception as migration_error:
            _LOGGER.warning(
                "Migration hourly prediction exclusion semantics failed: %s",
                migration_error,
            )
        return None if True else None

    async def _migrate_shadow_analysis_table_v16(self) -> None:
        """Create daily_summary_shadow_analysis table if missing. @zara"""
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='daily_summary_shadow_analysis'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
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
            row = await cursor.fetchone()
        if not row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS weather_precision_hourly_factors (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    factor_type TEXT NOT NULL CHECK(factor_type IN (\n                        'solar_radiation_wm2', 'clouds', 'temperature',\n                        'humidity', 'wind', 'rain', 'pressure'\n                    )),\n                    factor_value REAL NOT NULL DEFAULT 1.0,\n                    sample_count INTEGER DEFAULT 0,\n                    confidence REAL DEFAULT 0.0,\n                    std_dev REAL,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(hour, factor_type)\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Created weather_precision_hourly_factors table")
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='weather_precision_weather_specific'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS weather_precision_weather_specific (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    weather_type TEXT NOT NULL CHECK(weather_type IN ('clear', 'cloudy', 'mixed')),\n                    factor_type TEXT NOT NULL CHECK(factor_type IN (\n                        'solar_radiation_wm2', 'clouds', 'temperature',\n                        'humidity', 'wind', 'rain', 'pressure'\n                    )),\n                    factor_value REAL NOT NULL DEFAULT 1.0,\n                    sample_days INTEGER DEFAULT 0,\n                    confidence REAL DEFAULT 0.0,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                    UNIQUE(weather_type, factor_type)\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration: Created weather_precision_weather_specific table")
        return None if True else None

    async def _migrate_astronomy_panel_groups_v161(self) -> None:
        """Create astronomy_cache_panel_groups table for per-group POA radiation. @zara

        V16.1 migration: Adds per-panel-group POA (Plane-of-Array) radiation data
        for accurate tilted irradiance calculations instead of horizontal GHI only.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='astronomy_cache_panel_groups'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS astronomy_cache_panel_groups (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    cache_date DATE NOT NULL,\n                    hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                    group_name TEXT NOT NULL,\n                    power_kwp REAL NOT NULL,\n                    azimuth_deg REAL NOT NULL,\n                    tilt_deg REAL NOT NULL,\n                    theoretical_kwh REAL,\n                    poa_wm2 REAL,\n                    aoi_deg REAL,\n                    UNIQUE(cache_date, hour, group_name)\n                )"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_astronomy_panel_groups_date\n                   ON astronomy_cache_panel_groups(cache_date)"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration: Created astronomy_cache_panel_groups table with index"
            )
        return None if True else None

    async def _cleanup_ai_model_weights_blobs_v161(self) -> None:
        """Remove legacy JSON blobs from ai_model_weights table. @zara

        V16.1 cleanup: The ai_model_weights table was storing full weight dicts as JSON,
        which caused inconsistencies with has_attention flag. Now we use only
        structured tables (ai_lstm_weights, ai_ridge_weights, etc.).
        """
        async with self._db.execute("SELECT COUNT(*) FROM ai_model_weights") as cursor:
            row = await cursor.fetchone()
        if row and row[0] > 0:
            await self._db.execute("DELETE FROM ai_model_weights")
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.1: Deleted %d legacy JSON blobs from ai_model_weights. Structured tables are now the single source of truth.",
                row[0],
            )
        metadata_row = await self.fetchone(
            "SELECT flat_size FROM ai_ridge_meta WHERE id = 1"
        )
        if metadata_row and metadata_row[0]:
            stored_json = metadata_row[0]
            async with self._db.execute(
                "SELECT COUNT(*) FROM ai_ridge_weights WHERE col_index > ?",
                (stored_json,),
            ) as cursor:
                shadow_analysis_row = await cursor.fetchone()
            if shadow_analysis_row and shadow_analysis_row[0] > 0:
                await self._db.execute(
                    "DELETE FROM ai_ridge_weights WHERE col_index > ?", (stored_json,)
                )
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Removed %d extra Ridge weight entries (col_index > %d). This fixes the flat_size mismatch.",
                    shadow_analysis_row[0],
                    stored_json,
                )
        return None if True else None

    async def _migrate_frost_analysis_table_v161(self) -> None:
        """Create daily_summary_frost_analysis table if missing. @zara V16.1

        This table stores frost detection results for each day.
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='daily_summary_frost_analysis'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
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
            row = await cursor.fetchone()
        if not row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS visibility_learning (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    version TEXT DEFAULT '1.0',\n                    has_solar_radiation_sensor BOOLEAN DEFAULT FALSE,\n                    last_learning_date DATE,\n                    total_fog_hours_learned INTEGER DEFAULT 0,\n                    total_fog_light_hours_learned INTEGER DEFAULT 0,\n                    bright_sky_fog_hits INTEGER DEFAULT 0,\n                    pirate_weather_fog_hits INTEGER DEFAULT 0,\n                    learning_sessions INTEGER DEFAULT 0,\n                    fog_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_pirate_weather_weight REAL DEFAULT 0.5,\n                    fog_light_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_light_pirate_weather_weight REAL DEFAULT 0.5,\n                    visibility_threshold_m REAL DEFAULT 5000,\n                    fog_visibility_threshold_m REAL DEFAULT 1000,\n                    samples_below_threshold INTEGER DEFAULT 0,\n                    samples_above_threshold INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created visibility_learning table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(visibility_learning)"
            ) as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            if "visibility_threshold_m" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN visibility_threshold_m REAL DEFAULT 5000"
                )
            if "fog_visibility_threshold_m" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN fog_visibility_threshold_m REAL DEFAULT 1000"
                )
            if "samples_below_threshold" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_below_threshold INTEGER DEFAULT 0"
                )
            if "samples_above_threshold" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_above_threshold INTEGER DEFAULT 0"
                )
            if alter_statements:
                for alter_statement in alter_statements:
                    await self._db.execute(alter_statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to visibility_learning",
                    len(alter_statements),
                )
        if True:
            return None

    async def _migrate_snow_tracking_table_v161(self) -> None:
        """Create/update snow_tracking table. @zara V16.1

        This table stores snow coverage tracking data.
        V16.1 adds: melt_hours, detection_source, cleared_at
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='snow_tracking'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_started_at TIMESTAMP,\n                    melt_hours REAL DEFAULT 0,\n                    detection_source TEXT,\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created snow_tracking table")
        else:
            async with self._db.execute("PRAGMA table_info(snow_tracking)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            alter_statements = []
            if "melt_hours" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN melt_hours REAL DEFAULT 0"
                )
            if "detection_source" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN detection_source TEXT"
                )
            if "cleared_at" not in existing_columns:
                alter_statements.append(
                    "ALTER TABLE snow_tracking ADD COLUMN cleared_at TIMESTAMP"
                )
            if alter_statements:
                for alter_statement in alter_statements:
                    await self._db.execute(alter_statement)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to snow_tracking",
                    len(alter_statements),
                )
        if True:
            return None

    async def _migrate_weather_forecast_weather_code_v161(self) -> None:
        """Add weather_code column to weather_forecast table. @zara V16.1

        Used for snow detection via Open-Meteo weather codes.
        """
        async with self._db.execute("PRAGMA table_info(weather_forecast)") as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "weather_code" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE weather_forecast ADD COLUMN weather_code INTEGER"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.1: Added weather_code column to weather_forecast"
            )
        return

    async def _migrate_sfml_tfs_forecast_comparison(self) -> None:
        """Create the SFML-owned comparison table without importing legacy data."""
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS sfml_tfs_forecast_comparison (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                date TEXT NOT NULL UNIQUE,\n                actual_kwh REAL,\n                sfml_forecast_kwh REAL,\n                sfml_accuracy_percent REAL,\n                tfs_forecast_kwh REAL,\n                tfs_accuracy_percent REAL,\n                best_source TEXT,\n                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n            )\n        "
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sfml_tfs_forecast_comparison_date ON sfml_tfs_forecast_comparison(date)"
        )
        await self._db.commit()

    async def _migrate_shadow_pattern_learning_v162(self) -> None:
        """V16.2: Create shadow pattern learning tables. @zara"""
        added_columns = []
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_hourly'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_hourly (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        shadow_occurrence_rate REAL DEFAULT 0.0,\n                        avg_shadow_percent REAL DEFAULT 0.0,\n                        std_dev_shadow_percent REAL DEFAULT 0.0,\n                        pct_weather_clouds REAL DEFAULT 0.0,\n                        pct_building_tree REAL DEFAULT 0.0,\n                        pct_low_sun REAL DEFAULT 0.0,\n                        pct_other REAL DEFAULT 0.0,\n                        pattern_type TEXT DEFAULT 'unknown' CHECK(pattern_type IN (\n                            'no_shadow', 'occasional', 'frequent', 'fixed_obstruction', 'unknown'\n                        )),\n                        confidence REAL DEFAULT 0.0,\n                        sample_count INTEGER DEFAULT 0,\n                        shadow_days INTEGER DEFAULT 0,\n                        clear_days INTEGER DEFAULT 0,\n                        first_learned DATE,\n                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(hour)\n                    )\n                "
                )
                added_columns.append("shadow_pattern_hourly")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_seasonal'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_seasonal (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        month INTEGER NOT NULL CHECK(month >= 1 AND month <= 12),\n                        hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),\n                        shadow_occurrence_rate REAL DEFAULT 0.0,\n                        avg_shadow_percent REAL DEFAULT 0.0,\n                        std_dev_shadow_percent REAL DEFAULT 0.0,\n                        dominant_cause TEXT DEFAULT 'unknown',\n                        sample_count INTEGER DEFAULT 0,\n                        shadow_days INTEGER DEFAULT 0,\n                        confidence REAL DEFAULT 0.0,\n                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n                        UNIQUE(month, hour)\n                    )\n                "
                )
                added_columns.append("shadow_pattern_seasonal")
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
                added_columns.append("shadow_learning_history")
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_pattern_config'"
        ) as cursor:
            if not await cursor.fetchone():
                await self._db.execute(
                    "\n                    CREATE TABLE IF NOT EXISTS shadow_pattern_config (\n                        id INTEGER PRIMARY KEY CHECK (id = 1),\n                        total_days_learned INTEGER DEFAULT 0,\n                        total_hours_learned INTEGER DEFAULT 0,\n                        last_learning_date DATE,\n                        patterns_detected INTEGER DEFAULT 0,\n                        fixed_obstructions_detected INTEGER DEFAULT 0,\n                        version TEXT DEFAULT '1.0',\n                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                    )\n                "
                )
                added_columns.append("shadow_pattern_config")
        if added_columns:
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.2: Created shadow pattern learning tables: %s",
                ", ".join(added_columns),
            )
        return None if True else None

    async def _migrate_sensor_storage_tables_v1600(self) -> None:
        """V16.0.0: Create sensor storage tables for live data. @zara

        Creates tables for:
        - sensor_power_live: Rolling 2-day power (Watt) data
        - sensor_energy_hourly: Permanent hourly energy (kWh) data
        - sensor_weather: Permanent 5-min weather sensor readings
        """
        added_columns = []
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
                added_columns.append("sensor_power_live")
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
                added_columns.append("sensor_energy_hourly")
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
                added_columns.append("sensor_weather")
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
                added_columns.append("sensor_monthly_stats")
        if added_columns:
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Created sensor storage tables: %s",
                ", ".join(added_columns),
            )
        return None if True else None

    async def _migrate_raw_prediction_values_v1600(self) -> None:
        """V16.0.0: Add raw physics/AI prediction values to hourly_predictions and panel groups. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            schema_rows = await cursor.fetchall()
            prediction_columns = {schema_row[1] for schema_row in schema_rows}
        prediction_alter_statements = []
        if "physics_kwh" not in prediction_columns:
            prediction_alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN physics_kwh REAL"
            )
        if "ai_kwh" not in prediction_columns:
            prediction_alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ai_kwh REAL"
            )
        if "ai_confidence" not in prediction_columns:
            prediction_alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ai_confidence REAL"
            )
        if prediction_alter_statements:
            for alter_statement in prediction_alter_statements:
                await self._db.execute(alter_statement)
            _LOGGER.info(
                "Migration V16.0.0: Added %d raw value columns to hourly_predictions",
                len(prediction_alter_statements),
            )
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            panel_group_columns = {schema_row[1] for schema_row in schema_rows}
        group_alter_statements = []
        if "physics_kwh" not in panel_group_columns:
            group_alter_statements.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN physics_kwh REAL"
            )
        if "ai_kwh" not in panel_group_columns:
            group_alter_statements.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN ai_kwh REAL"
            )
        if group_alter_statements:
            for alter_statement in group_alter_statements:
                await self._db.execute(alter_statement)
            _LOGGER.info(
                "Migration V16.0.0: Added %d raw value columns to prediction_panel_groups",
                len(group_alter_statements),
            )
        if prediction_alter_statements or group_alter_statements:
            await self._db.commit()
        return

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

    async def _migrate_weather_regime_performance_learning_v2026(self) -> None:
        """Create weather-regime performance learning for static forecast blending."""
        await self._db.execute(
            "\n            CREATE TABLE IF NOT EXISTS weather_regime_performance_learning (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                weather_regime TEXT NOT NULL,\n                hour_bucket TEXT NOT NULL CHECK(hour_bucket IN ('morning', 'midday', 'afternoon')),\n                season TEXT DEFAULT NULL,\n                physics_mae REAL DEFAULT 0.0,\n                ai_mae REAL DEFAULT NULL,\n                tfs_mae REAL DEFAULT NULL,\n                blend_mae REAL DEFAULT 0.0,\n                physics_bias REAL DEFAULT 0.0,\n                ai_bias REAL DEFAULT NULL,\n                tfs_bias REAL DEFAULT NULL,\n                blend_bias REAL DEFAULT 0.0,\n                best_method TEXT DEFAULT 'blend',\n                recommended_tfs_weight REAL DEFAULT NULL,\n                ai_advantage_factor REAL DEFAULT 1.0,\n                tfs_advantage_factor REAL DEFAULT 1.0,\n                sample_count INTEGER DEFAULT 0,\n                last_updated TIMESTAMP,\n                UNIQUE(weather_regime, hour_bucket, season)\n            )\n        "
        )
        await self._db.execute(
            "\n            CREATE INDEX IF NOT EXISTS idx_weather_regime_perf_lookup\n            ON weather_regime_performance_learning(weather_regime, hour_bucket, season)\n        "
        )
        await self._db.commit()

    async def _migrate_hourly_weather_actual_snow_frost_v1600(self) -> None:
        """V16.0.0: Add frost/snow tracking columns to hourly_weather_actual. @zara

        These columns are used by data_weather_actual_tracker for frost notifications
        and snow event detection/clearing progress.
        """
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        alter_statements = []
        if "frost_notification_sent" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN frost_notification_sent BOOLEAN DEFAULT 0"
            )
        if "frost_type" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN frost_type TEXT"
            )
        if "snow_confidence" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_confidence REAL"
            )
        if "snow_event_detected" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_event_detected BOOLEAN DEFAULT 0"
            )
        if "snow_clearing_progress" not in existing_columns:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN snow_clearing_progress REAL"
            )
        if alter_statements:
            for alter_statement in alter_statements:
                await self._db.execute(alter_statement)
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added %d frost/snow columns to hourly_weather_actual",
                len(alter_statements),
            )
        return

    async def _migrate_weather_cache_open_meteo_code_v1600(self) -> None:
        """V16.0.0: Add weather_code column to weather_cache_open_meteo. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(weather_cache_open_meteo)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "weather_code" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE weather_cache_open_meteo ADD COLUMN weather_code INTEGER"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added weather_code column to weather_cache_open_meteo"
            )
        return

    async def _migrate_missing_indexes_v1600(self) -> None:
        """V16.0.0: Create missing indexes and clean up duplicates. @zara"""
        try:
            await self._db.execute(
                "DROP INDEX IF EXISTS idx_astronomy_panel_groups_date"
            )
        except Exception:
            pass
        index_definitions = [
            "CREATE INDEX IF NOT EXISTS idx_astronomy_cache_panel_groups_date ON astronomy_cache_panel_groups(cache_date)",
            "CREATE INDEX IF NOT EXISTS idx_astronomy_cache_panel_groups_date_hour ON astronomy_cache_panel_groups(cache_date, hour)",
            "CREATE INDEX IF NOT EXISTS idx_weather_precision_hourly_hour ON weather_precision_hourly_factors(hour)",
        ]
        for index_statement in index_definitions:
            try:
                await self._db.execute(index_statement)
            except Exception:
                continue
        await self._db.commit()

    async def _migrate_peak_power_tracking_v1600(self) -> None:
        """V16.0.0: Add today's peak + all-time peak record columns to production_time_state
        and peak_power_time to daily_summaries for AI learning. @zara"""
        new_tables = []
        async with self._db.execute(
            "PRAGMA table_info(production_time_state)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        for schema_row, column_type in (
            ("peak_power_w", "REAL DEFAULT 0"),
            ("peak_power_time", "TEXT"),
            ("peak_record_w", "REAL"),
            ("peak_record_date", "TEXT"),
            ("peak_record_time", "TEXT"),
        ):
            if schema_row not in existing_columns:
                await self._db.execute(
                    f"ALTER TABLE production_time_state ADD COLUMN {schema_row} {column_type}"
                )
                new_tables.append(f"production_time_state.{schema_row}")
        async with self._db.execute("PRAGMA table_info(daily_summaries)") as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "peak_power_time" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE daily_summaries ADD COLUMN peak_power_time TEXT"
            )
            new_tables.append("daily_summaries.peak_power_time")
        if new_tables:
            try:
                row = await self._db.fetchone(
                    "SELECT kwh * 1000 as peak_w, date, hour\n                       FROM astronomy_hourly_peaks\n                       WHERE kwh IS NOT NULL AND kwh > 0\n                       ORDER BY kwh DESC LIMIT 1"
                )
                if row and row[0]:
                    await self._db.execute(
                        "UPDATE production_time_state\n                           SET peak_record_w = ?, peak_record_date = ?, peak_record_time = ?\n                           WHERE id = 1 AND (peak_record_w IS NULL OR peak_record_w < ?)",
                        (
                            float(row[0]),
                            str(row[1]),
                            f"{int(row[2]):02d}:00",
                            float(row[0]),
                        ),
                    )
                    _LOGGER.info(
                        "Migration V16.0.0: Seeded all-time peak: %.1f W on %s at %02d:00",
                        float(row[0]),
                        row[1],
                        int(row[2]),
                    )
            except Exception as migration_error:
                _LOGGER.debug("Could not seed all-time peak: %s", migration_error)
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Peak power tracking: %s", ", ".join(new_tables)
            )
        return None if True else None

    async def _migrate_grid_search_model_type_v1600(self) -> None:
        """V16.0.0: Add model_type column to ai_grid_search_results. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(ai_grid_search_results)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "model_type" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE ai_grid_search_results ADD COLUMN model_type TEXT DEFAULT 'lstm'"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added 'model_type' to ai_grid_search_results"
            )
        return

    async def _migrate_prediction_weather_diffuse_v1600(self) -> None:
        """V16.0.0: Add diffuse_radiation column to prediction_weather. @zara"""
        async with self._db.execute("PRAGMA table_info(prediction_weather)") as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "diffuse_radiation" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE prediction_weather ADD COLUMN diffuse_radiation REAL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.0: Added 'diffuse_radiation' to prediction_weather"
            )
        return

    async def _migrate_prediction_weather_direct_radiation_v1601(self) -> None:
        """V16.0.1: Add direct_radiation column to prediction_weather. @zara"""
        async with self._db.execute("PRAGMA table_info(prediction_weather)") as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "direct_radiation" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE prediction_weather ADD COLUMN direct_radiation REAL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.0.1: Added 'direct_radiation' to prediction_weather"
            )
        return

    async def _migrate_ensemble_columns_v1602(self) -> None:
        """V16.0.2: Add lstm_kwh and ridge_kwh columns for ensemble diagnostics. @zara"""
        async with self._db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
            schema_rows = await cursor.fetchall()
            prediction_columns = {schema_row[1] for schema_row in schema_rows}
        prediction_alter_statements = []
        if "lstm_kwh" not in prediction_columns:
            prediction_alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN lstm_kwh REAL"
            )
        if "ridge_kwh" not in prediction_columns:
            prediction_alter_statements.append(
                "ALTER TABLE hourly_predictions ADD COLUMN ridge_kwh REAL"
            )
        if prediction_alter_statements:
            for alter_statement in prediction_alter_statements:
                await self._db.execute(alter_statement)
            _LOGGER.info(
                "Migration V16.0.2: Added %d ensemble columns to hourly_predictions",
                len(prediction_alter_statements),
            )
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            panel_group_columns = {schema_row[1] for schema_row in schema_rows}
        group_alter_statements = []
        if "lstm_kwh" not in panel_group_columns:
            group_alter_statements.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN lstm_kwh REAL"
            )
        if "ridge_kwh" not in panel_group_columns:
            group_alter_statements.append(
                "ALTER TABLE prediction_panel_groups ADD COLUMN ridge_kwh REAL"
            )
        if group_alter_statements:
            for alter_statement in group_alter_statements:
                await self._db.execute(alter_statement)
            _LOGGER.info(
                "Migration V16.0.2: Added %d ensemble columns to prediction_panel_groups",
                len(group_alter_statements),
            )
        if prediction_alter_statements or group_alter_statements:
            await self._db.commit()
        return

    async def _migrate_ensemble_group_weights_v1603(self) -> None:
        """V16.0.3: Create ensemble_group_weights table for per-group adaptive blending. @zara"""
        async with self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ensemble_group_weights'"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
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
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
        except Exception:
            return
        if "feature_means_json" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE ai_ridge_meta ADD COLUMN feature_means_json TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.0.4: Added feature_means_json to ai_ridge_meta")
        if "feature_stds_json" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE ai_ridge_meta ADD COLUMN feature_stds_json TEXT"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.0.4: Added feature_stds_json to ai_ridge_meta")
        return None if True else None

    async def _migrate_per_group_v170(self) -> None:
        """V17.0.0: Per-group shadow/snow, drift detection, seasonal buckets."""
        added_columns = []
        async with self._db.execute(
            "PRAGMA table_info(prediction_panel_groups)"
        ) as cursor:
            prediction_group_schema_rows = await cursor.fetchall()
            prediction_group_columns = {
                prediction_group_schema_row[1]
                for prediction_group_schema_row in prediction_group_schema_rows
            }
        group_flag_column_definitions = {
            "exclude_from_learning_group": "BOOLEAN DEFAULT FALSE",
            "exclusion_reason_group": "TEXT",
            "snow_covered_group": "BOOLEAN DEFAULT FALSE",
            "shadow_type_group": "TEXT",
        }
        for column_name, column_type in group_flag_column_definitions.items():
            if column_name not in prediction_group_columns:
                await self._db.execute(
                    f"ALTER TABLE prediction_panel_groups ADD COLUMN {column_name} {column_type}"
                )
                added_columns.append(f"ppg.{column_name}")
        migration_needed = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_pattern_hourly)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            hourly_pattern_columns = {schema_row[1] for schema_row in schema_rows}
            if "group_name" in hourly_pattern_columns:
                migration_needed = False
        if migration_needed and hourly_pattern_columns:
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
            added_columns.append("rebuild:shadow_pattern_hourly")
        migration_needed = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_pattern_seasonal)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            seasonal_pattern_columns = {schema_row[1] for schema_row in schema_rows}
            if "group_name" in seasonal_pattern_columns:
                migration_needed = False
        if migration_needed and seasonal_pattern_columns:
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
            added_columns.append("rebuild:shadow_pattern_seasonal")
        migration_needed = True
        async with self._db.execute(
            "PRAGMA table_info(shadow_learning_history)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            learning_history_columns = {schema_row[1] for schema_row in schema_rows}
            if "group_name" in learning_history_columns:
                migration_needed = False
        if migration_needed and learning_history_columns:
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
            added_columns.append("rebuild:shadow_learning_history")
        migration_needed = True
        async with self._db.execute(
            "PRAGMA table_info(method_performance_learning)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            method_performance_columns = {schema_row[1] for schema_row in schema_rows}
            if "season" in method_performance_columns:
                migration_needed = False
        if migration_needed and method_performance_columns:
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
            added_columns.append("rebuild:method_performance_learning")
        migration_needed = True
        async with self._db.execute(
            "PRAGMA table_info(ensemble_group_weights)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            group_weight_columns = {schema_row[1] for schema_row in schema_rows}
            if "season" in group_weight_columns:
                migration_needed = False
        if migration_needed and group_weight_columns:
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
            added_columns.append("rebuild:ensemble_group_weights")
        table_definitions = {
            "snow_tracking_groups": "\n                CREATE TABLE IF NOT EXISTS snow_tracking_groups (\n                    group_name TEXT NOT NULL UNIQUE,\n                    tilt_deg REAL NOT NULL DEFAULT 30.0,\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_hours REAL DEFAULT 0,\n                    tilt_melt_factor REAL DEFAULT 1.0,\n                    detection_source TEXT DEFAULT 'unknown',\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )",
            "shadow_detection_groups": "\n                CREATE TABLE IF NOT EXISTS shadow_detection_groups (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    prediction_id TEXT NOT NULL,\n                    group_name TEXT NOT NULL,\n                    shadow_type TEXT,\n                    shadow_percent REAL,\n                    confidence REAL,\n                    root_cause TEXT,\n                    efficiency_ratio REAL,\n                    loss_kwh REAL,\n                    theoretical_max_kwh REAL,\n                    actual_kwh REAL,\n                    FOREIGN KEY (prediction_id) REFERENCES hourly_predictions(prediction_id) ON DELETE CASCADE,\n                    UNIQUE(prediction_id, group_name)\n                )",
            "drift_metrics_rolling": "\n                CREATE TABLE IF NOT EXISTS drift_metrics_rolling (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    scope TEXT NOT NULL,\n                    window_days INTEGER NOT NULL,\n                    season TEXT,\n                    mae REAL,\n                    rmse REAL,\n                    bias REAL,\n                    coverage_10 REAL,\n                    coverage_20 REAL,\n                    sample_count INTEGER,\n                    calculated_at TIMESTAMP,\n                    UNIQUE(scope, window_days, season)\n                )",
            "drift_metrics_bucket": "\n                CREATE TABLE IF NOT EXISTS drift_metrics_bucket (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    scope TEXT NOT NULL,\n                    cloud_bucket TEXT NOT NULL,\n                    hour_bucket TEXT NOT NULL,\n                    season TEXT,\n                    mae REAL,\n                    mae_baseline REAL,\n                    mae_ratio REAL,\n                    bias REAL,\n                    sample_count INTEGER,\n                    calculated_at TIMESTAMP,\n                    UNIQUE(scope, cloud_bucket, hour_bucket, season)\n                )",
            "drift_events": "\n                CREATE TABLE IF NOT EXISTS drift_events (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    event_date DATE NOT NULL,\n                    scope TEXT NOT NULL,\n                    drift_type TEXT NOT NULL,\n                    severity TEXT NOT NULL,\n                    bucket_detail TEXT,\n                    metric_value REAL,\n                    threshold_value REAL,\n                    description TEXT,\n                    response_action TEXT,\n                    response_executed BOOLEAN DEFAULT FALSE,\n                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )",
            "drift_cusum_state": "\n                CREATE TABLE IF NOT EXISTS drift_cusum_state (\n                    scope TEXT NOT NULL UNIQUE,\n                    cusum_pos REAL DEFAULT 0,\n                    cusum_neg REAL DEFAULT 0,\n                    target_mean REAL DEFAULT 0,\n                    last_reset DATE,\n                    alert_count INTEGER DEFAULT 0\n                )",
        }
        for table_name, create_table_sql in table_definitions.items():
            async with self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cursor:
                if not await cursor.fetchone():
                    await self._db.execute(create_table_sql)
                    added_columns.append(f"table:{table_name}")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_detection_groups_prediction ON shadow_detection_groups(prediction_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_drift_events_date ON drift_events(event_date)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_drift_events_scope ON drift_events(scope)"
        )
        if added_columns:
            await self._db.commit()
            _LOGGER.info(
                "Migration V17.0.0: Per-group + drift + seasonal — %s",
                ", ".join(added_columns),
            )
        return None if True else None

    async def _migrate_gold_standard_v1640(self) -> None:
        """V16.4.0: outlier_severity on hourly_weather_actual. @zara"""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            schema_rows = await cursor.fetchall()
            existing_columns = {schema_row[1] for schema_row in schema_rows}
        if "outlier_severity" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE hourly_weather_actual ADD COLUMN outlier_severity REAL DEFAULT NULL"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.0: Added outlier_severity to hourly_weather_actual"
            )
        return

    async def _migrate_hourly_weather_actual_precipitation_semantics_v20260819(
        self,
    ) -> None:
        """Persist rain unit and semantics so STATS can separate rate from amount."""
        async with self._db.execute(
            "PRAGMA table_info(hourly_weather_actual)"
        ) as cursor:
            column_names = {schema_row[1] for schema_row in await cursor.fetchall()}
        alter_statements = []
        if "precipitation_semantics" not in column_names:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_semantics TEXT"
            )
        if "precipitation_unit" not in column_names:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_unit TEXT"
            )
        if "precipitation_quality" not in column_names:
            alter_statements.append(
                "ALTER TABLE hourly_weather_actual ADD COLUMN precipitation_quality TEXT"
            )
        if not alter_statements:
            return
        for alter_statement in alter_statements:
            await self._db.execute(alter_statement)
        await self._db.commit()
        _LOGGER.info(
            "Migration: Added %d precipitation semantics columns to hourly_weather_actual",
            len(alter_statements),
        )
        return

    async def _backfill_astronomy_enrichment_v1640(self) -> None:
        """V16.4.0: Backfill prediction_astronomy enrichment from stored sunrise/sunset/noon. @zara"""
        try:
            async with self._db.execute(
                "SELECT pa.prediction_id, pa.sunrise, pa.sunset, pa.solar_noon, hp.target_hour\n                   FROM prediction_astronomy pa\n                   JOIN hourly_predictions hp ON hp.prediction_id = pa.prediction_id\n                   WHERE (pa.day_progress_ratio IS NULL OR pa.hours_after_sunrise IS NULL\n                          OR pa.hours_before_sunset IS NULL OR pa.hours_since_solar_noon IS NULL)\n                     AND pa.sunrise IS NOT NULL AND pa.sunset IS NOT NULL\n                   LIMIT 5000"
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return
            updated_summary_count = 0
            for row in rows:
                prediction_id = row[0]
                sunrise_text = row[1]
                sunset_text = row[2]
                solar_noon_text = row[3]
                target_hour = row[4] if row[4] is not None else 12
                try:
                    sunrise_datetime = self._parse_astronomy_ts(sunrise_text)
                    sunset_datetime = self._parse_astronomy_ts(sunset_text)
                    if sunrise_datetime is None or sunset_datetime is None:
                        continue
                    sunrise_naive = (
                        sunrise_datetime.replace(tzinfo=None)
                        if sunrise_datetime.tzinfo
                        else sunrise_datetime
                    )
                    sunset_naive = (
                        sunset_datetime.replace(tzinfo=None)
                        if sunset_datetime.tzinfo
                        else sunset_datetime
                    )
                    target_midpoint = sunrise_naive.replace(
                        hour=target_hour, minute=30, second=0, microsecond=0
                    )
                    daylight_seconds = (sunset_naive - sunrise_naive).total_seconds()
                    elapsed_daylight_seconds = (
                        target_midpoint - sunrise_naive
                    ).total_seconds()
                    daylight_position = (
                        max(0.0, min(1.0, elapsed_daylight_seconds / daylight_seconds))
                        if daylight_seconds > 0
                        else 0.5
                    )
                    hours_since_sunrise = max(0.0, elapsed_daylight_seconds / 3600.0)
                    hours_until_sunset = max(
                        0.0, (sunset_naive - target_midpoint).total_seconds() / 3600.0
                    )
                    hours_from_solar_noon = None
                    if solar_noon_text:
                        solar_noon_datetime = self._parse_astronomy_ts(solar_noon_text)
                        if solar_noon_datetime:
                            solar_noon_naive = (
                                solar_noon_datetime.replace(tzinfo=None)
                                if solar_noon_datetime.tzinfo
                                else solar_noon_datetime
                            )
                            hours_from_solar_noon = (
                                target_midpoint - solar_noon_naive
                            ).total_seconds() / 3600.0
                    await self._db.execute(
                        "UPDATE prediction_astronomy\n                           SET day_progress_ratio = ?,\n                               hours_after_sunrise = ?,\n                               hours_before_sunset = ?,\n                               hours_since_solar_noon = ?\n                           WHERE prediction_id = ?\n                             AND day_progress_ratio IS NULL",
                        (
                            round(daylight_position, 6),
                            round(hours_since_sunrise, 4),
                            round(hours_until_sunset, 4),
                            (
                                round(hours_from_solar_noon, 4)
                                if hours_from_solar_noon is not None
                                else None
                            ),
                            prediction_id,
                        ),
                    )
                    updated_summary_count += 1
                except Exception:
                    continue
            if updated_summary_count > 0:
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Backfilled %d prediction_astronomy enrichment rows",
                    updated_summary_count,
                )
        except Exception:
            pass
        if True:
            return None

    async def _cleanup_null_season_duplicates_v1640(self) -> None:
        """V16.4.0: Remove NULL-season duplicates and reset poisoned learning data. @zara"""
        try:
            duplicate_count = 0
            for table_name, grouping_columns in (
                ("method_performance_learning", "cloud_bucket, hour_bucket"),
                ("ensemble_group_weights", "group_name, cloud_bucket, hour_bucket"),
            ):
                async with self._db.execute(
                    f"SELECT {grouping_columns}, COUNT(*) as cnt\n                        FROM {table_name}\n                        WHERE season IS NULL\n                        GROUP BY {grouping_columns}\n                        HAVING cnt > 1"
                ) as cursor:
                    duplicate_rows = await cursor.fetchall()
                    duplicate_count += len(duplicate_rows)
            if duplicate_count > 0:
                await self._db.execute("DELETE FROM method_performance_learning")
                await self._db.execute("DELETE FROM ensemble_group_weights")
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Reset method_performance_learning and ensemble_group_weights (%d duplicate buckets found)",
                    duplicate_count,
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
                summary_row = await cursor.fetchone()
            if not summary_row:
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
        except Exception as migration_error:
            _LOGGER.debug("group_method_performance migration: %s", migration_error)
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
                migration_row = await cursor.fetchone()
            if migration_row:
                return
            await self._db.execute(
                "UPDATE snow_tracking\n                   SET panels_covered_since = NULL, melt_hours = 0,\n                       estimated_depth_mm = 0.0,\n                       cleared_at = CURRENT_TIMESTAMP\n                   WHERE id = 1"
            )
            async with self._db.execute(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE weather_alert_type = 'snow_covered_panels'\n                   AND target_date >= date('now', '-7 days')"
            ) as cursor:
                row = await cursor.fetchone()
            snow_group_count = row[0] if row else 0
            if snow_group_count > 0:
                await self._db.execute(
                    "UPDATE hourly_predictions\n                       SET weather_alert_type = NULL,\n                           has_weather_alert = 0,\n                           exclude_from_learning = 0,\n                           exclude_from_clean_evaluation = 0\n                       WHERE weather_alert_type = 'snow_covered_panels'\n                       AND target_date >= date('now', '-7 days')"
                )
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES ('selfheal_snow_v1642')"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.2: Snow self-heal applied — cleared snow_tracking + %d stale hourly flags (one-time)",
                snow_group_count,
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
                migration_row = await cursor.fetchone()
            if migration_row:
                return
            reset_statements = []
            await self._db.execute(
                "UPDATE weather_precision_factors\n                   SET solar_factor = 1.0, confidence = 0.0,\n                       updated_at = CURRENT_TIMESTAMP\n                   WHERE id = 1"
            )
            reset_statements.append("solar_factor")
            await self._db.execute(
                "DELETE FROM weather_precision_hourly_factors\n                   WHERE factor_type = 'solar_radiation_wm2'"
            )
            reset_statements.append("hourly_solar")
            await self._db.execute(
                "DELETE FROM weather_precision_weather_specific\n                   WHERE factor_type = 'solar_radiation_wm2'"
            )
            reset_statements.append("ws_solar")
            await self._db.execute(
                "UPDATE physics_calibration_groups\n                   SET global_factor = 1.0, sample_count = 0, confidence = 0.0"
            )
            await self._db.execute("DELETE FROM physics_calibration_hourly")
            await self._db.execute("DELETE FROM physics_calibration_buckets")
            await self._db.execute("DELETE FROM physics_calibration_history")
            reset_statements.append("physics_calibrator")
            await self._db.execute("DELETE FROM method_performance_learning")
            await self._db.execute("DELETE FROM group_method_performance")
            await self._db.execute("DELETE FROM ensemble_group_weights")
            reset_statements.append("learning_tables")
            await self._db.execute(
                "UPDATE weather_precision_daily_summary\n                   SET avg_solar_factor = 1.0\n                   WHERE date >= date('now', '-7 days')"
            )
            reset_statements.append("daily_summary")
            await self._db.execute(
                "INSERT INTO migration_applied (migration_id) VALUES ('goldstandard_reset_v1642')"
            )
            await self._db.commit()
            _LOGGER.info(
                "Migration V16.4.2 Gold Standard: Reset %s (one-time)",
                ", ".join(reset_statements),
            )
        except Exception as migration_error:
            _LOGGER.warning("Gold Standard reset: %s", migration_error)
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
                migration_row = await cursor.fetchone()
            if migration_row:
                return
            async with self._db.execute("PRAGMA table_info(ai_lstm_meta)") as cursor:
                schema_rows = await cursor.fetchall()
                existing_columns = {schema_row[1] for schema_row in schema_rows}
            if "num_layers" not in existing_columns:
                await self._db.execute(
                    "ALTER TABLE ai_lstm_meta ADD COLUMN num_layers INTEGER DEFAULT 1"
                )
            if "num_heads" not in existing_columns:
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
        except Exception as migration_error:
            _LOGGER.warning("AI V3.0 migration: %s", migration_error)
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
        return None if True else None

    @_atomic_database_write
    async def save_feature_importance(self, data: Dict[str, Any]) -> None:
        """Save feature importance analysis."""
        analysis_timestamp = _safe_dt(data.get("timestamp"))
        for feature_name, importance in data["feature_importance"].items():
            if feature_name in data["helpful_features"]:
                category = "helpful"
            elif feature_name in data["harmful_features"]:
                category = "harmful"
            else:
                category = "neutral"
            await self.execute(
                "INSERT INTO ai_feature_importance\n                   (feature_name, importance, category, baseline_rmse, num_samples,\n                    analysis_time_seconds, timestamp)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    feature_name,
                    importance,
                    category,
                    data.get("baseline_rmse"),
                    data.get("num_samples"),
                    data.get("analysis_time_seconds"),
                    analysis_timestamp,
                ),
            )
        await self.commit()

    async def get_latest_feature_importance(self) -> Optional[Dict[str, Any]]:
        """Get the latest feature importance analysis."""
        rows = await self.fetchall(
            "SELECT feature_name, importance, category\n               FROM ai_feature_importance\n               WHERE timestamp = (SELECT MAX(timestamp) FROM ai_feature_importance)"
        )
        if not rows:
            return
        return {
            "feature_importance": {row[0]: row[1] for row in rows},
            "helpful_features": [row[0] for row in rows if row[2] == "helpful"],
            "harmful_features": [row[0] for row in rows if row[2] == "harmful"],
            "neutral_features": [row[0] for row in rows if row[2] == "neutral"],
        }

    @_atomic_database_write
    async def save_grid_search_result(self, result: Dict[str, Any]) -> None:
        """Save grid search result."""
        search_timestamp = _safe_dt(result.get("timestamp"))
        for search_result in result.get("all_results", []):
            search_parameters = search_result["params"]
            hidden_size = search_parameters.get("hidden_size")
            hidden_sizes = search_parameters.get("hidden_sizes")
            if hidden_size is None and hidden_sizes:
                hidden_size = hidden_sizes[0]
            await self.execute(
                "INSERT INTO ai_grid_search_results\n                   (success, hidden_size, batch_size, learning_rate, accuracy,\n                    epochs_trained, final_val_loss, duration_seconds, is_best_result,\n                    hardware_info, timestamp, model_type)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result["success"],
                    hidden_size,
                    search_parameters.get("batch_size"),
                    search_parameters.get("learning_rate"),
                    search_result.get("accuracy", 0),
                    search_result.get("epochs_trained", 0),
                    search_result.get("final_val_loss", 0),
                    search_result.get("duration_seconds", 0),
                    search_parameters == result["best_params"],
                    json.dumps(result.get("hardware_info")),
                    search_timestamp,
                    result.get("model_type", "lstm"),
                ),
            )
            continue
        await self.commit()

    @_atomic_database_write
    async def save_model_weights(self, weights_data: Dict[str, Any]) -> None:
        """Save AI model weights to structured tables. @zara"""
        await self.execute(
            "INSERT INTO ai_learned_weights_meta\n               (id, version, active_model, training_samples, last_trained, accuracy, rmse, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   version = excluded.version,\n                   active_model = excluded.active_model,\n                   training_samples = excluded.training_samples,\n                   last_trained = excluded.last_trained,\n                   accuracy = excluded.accuracy,\n                   rmse = excluded.rmse,\n                   updated_at = excluded.updated_at",
            (
                weights_data.get("version", "2.0"),
                weights_data.get("active_model", "none"),
                weights_data.get("training_samples", 0),
                weights_data.get("last_trained"),
                weights_data.get("accuracy"),
                weights_data.get("rmse"),
                SafeDateTimeUtil.now(),
            ),
        )
        lstm_weights = weights_data.get("lstm")
        if lstm_weights:
            lstm_weights["training_samples"] = weights_data.get("training_samples")
            lstm_weights["accuracy"] = weights_data.get("accuracy")
            lstm_weights["rmse"] = weights_data.get("rmse")
            await self._save_lstm_weights(lstm_weights)
        ridge_weights = weights_data.get("ridge")
        if ridge_weights:
            ridge_weights["training_samples"] = weights_data.get("training_samples")
            await self._save_ridge_weights(ridge_weights)
        await self.commit()
        _LOGGER.debug("Model weights saved to structured tables")

    @_atomic_database_write
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
        await self.execute(
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
                SafeDateTimeUtil.now(),
            ),
        )
        await self._store_coefficient_blob(
            f"seasonal:{season}:{archive_type}", weights_data
        )
        await self.execute(
            "DELETE FROM ai_seasonal_archive_weights WHERE season = ? AND archive_type = ?",
            (season, archive_type),
        )
        await self.commit()
        _LOGGER.debug("Seasonal archive saved: %s/%s", season, archive_type)

    async def load_seasonal_archive(
        self, season: str, archive_type: str
    ) -> Optional[Dict[str, Any]]:
        """Load model weights from seasonal archive. @zara V16.8.4"""
        metadata_row = await self.fetchone(
            "SELECT training_samples, accuracy, rmse, source_year,\n                      input_size, hidden_size, hidden2, num_outputs, num_heads,\n                      sequence_length, alpha, flat_size, loo_cv_score\n               FROM ai_seasonal_archive_meta\n               WHERE season = ? AND archive_type = ?",
            (season, archive_type),
        )
        if not metadata_row:
            return
        sealed_payload = await self._open_coefficient_blob(
            f"seasonal:{season}:{archive_type}"
        )
        if sealed_payload is not _NO_COEFFICIENT_BLOB:
            return sealed_payload
        weights = {
            "training_samples": metadata_row[0] or 0,
            "accuracy": metadata_row[1] or 0.0,
            "rmse": metadata_row[2] or 0.0,
            "source_year": metadata_row[3],
            "input_size": metadata_row[4],
            "hidden_size": metadata_row[5],
            "hidden2": metadata_row[6],
            "num_outputs": metadata_row[7],
            "num_heads": metadata_row[8] or 4,
            "sequence_length": metadata_row[9] or 24,
            "alpha": metadata_row[10],
            "flat_size": metadata_row[11],
            "loo_cv_score": metadata_row[12],
        }
        if archive_type == "lstm":
            weights["has_attention"] = True
            weights["num_layers"] = 2
        rows = await self.fetchall(
            "SELECT weight_type, weight_index, weight_value\n               FROM ai_seasonal_archive_weights\n               WHERE season = ? AND archive_type = ?\n               ORDER BY weight_type, weight_index",
            (season, archive_type),
        )
        if not rows:
            return
        current_weight_type = None
        current_weight_values = []
        for row in rows:
            weight_type, weight_index, weight_value = (row[0], row[1], row[2])
            if weight_type != current_weight_type:
                if current_weight_type is not None:
                    weights[current_weight_type] = current_weight_values
                current_weight_type = weight_type
                current_weight_values = []
            current_weight_values.append(weight_value)
        if current_weight_type is not None:
            weights[current_weight_type] = current_weight_values
        if (
            archive_type == "ridge"
            and "weights" in weights
            and weights.get("num_outputs")
            and weights.get("flat_size")
        ):
            flat_values = weights["weights"]
            matrix_columns = weights["flat_size"] + 1
            ridge_output_count = weights["num_outputs"]
            if len(flat_values) == ridge_output_count * matrix_columns:
                weights["weights"] = [
                    flat_values[
                        row_index * matrix_columns : (row_index + 1) * matrix_columns
                    ]
                    for row_index in range(ridge_output_count)
                ]
        if archive_type == "lstm":
            first_hidden_size = weights.get("hidden_size", 48)
            second_hidden_size = weights.get("hidden2", 24)
            input_size = weights.get("input_size", 31)
            lstm_output_count = weights.get("num_outputs", 2)
            matrix_shapes = {
                "Wf": (first_hidden_size, input_size + first_hidden_size),
                "Wi": (first_hidden_size, input_size + first_hidden_size),
                "Wc": (first_hidden_size, input_size + first_hidden_size),
                "Wo": (first_hidden_size, input_size + first_hidden_size),
                "Wf2": (second_hidden_size, first_hidden_size + second_hidden_size),
                "Wi2": (second_hidden_size, first_hidden_size + second_hidden_size),
                "Wc2": (second_hidden_size, first_hidden_size + second_hidden_size),
                "Wo2": (second_hidden_size, first_hidden_size + second_hidden_size),
                "W_query": (second_hidden_size, second_hidden_size),
                "W_key": (second_hidden_size, second_hidden_size),
                "W_value": (second_hidden_size, second_hidden_size),
                "W_attn_out": (second_hidden_size, second_hidden_size),
                "Wy": (lstm_output_count, second_hidden_size),
            }
            for matrix_type, (rows, matrix_columns) in matrix_shapes.items():
                if (
                    matrix_type in weights
                    and isinstance(weights[matrix_type], list)
                    and (len(weights[matrix_type]) == rows * matrix_columns)
                ):
                    flat_values = weights[matrix_type]
                    weights[matrix_type] = [
                        flat_values[
                            row_index
                            * matrix_columns : (row_index + 1)
                            * matrix_columns
                        ]
                        for row_index in range(rows)
                    ]
        return weights

    async def _store_coefficient_blob(
        self, kind: str, payload: Mapping[str, Any]
    ) -> None:
        """Store one model artifact as a single sealed blob."""
        weight_type = f"coeff:{kind}"
        sealed_weights_blob = seal_protected_text(
            canonical_stable_json(json_ready(dict(payload))),
            "ai_model_weights",
            weight_type,
        )
        await self.execute(
            "INSERT INTO ai_model_weights (weight_type, weight_data, updated_at)\n               VALUES (?, ?, ?)\n               ON CONFLICT(weight_type) DO UPDATE SET\n                   weight_data = excluded.weight_data,\n                   updated_at = excluded.updated_at",
            (weight_type, sealed_weights_blob, SafeDateTimeUtil.now()),
        )

    async def _open_coefficient_blob(self, kind: str) -> Any:
        """Return the sealed artifact, None when it cannot be opened, or a sentinel."""
        weight_type = f"coeff:{kind}"
        weights_row = await self.fetchone(
            "SELECT weight_data FROM ai_model_weights WHERE weight_type = ?",
            (weight_type,),
        )
        if weights_row is None:
            return _NO_COEFFICIENT_BLOB
        try:
            return loads_protected_json(
                weights_row[0], "ai_model_weights", weight_type, "weights"
            )
        except ValueError:
            return

    @_atomic_database_write
    async def _save_lstm_weights(self, lstm_data: Dict[str, Any]) -> None:
        """Save LSTM weights to ai_lstm_weights table. @zara"""
        await self.execute(
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
                SafeDateTimeUtil.now(),
            ),
        )
        await self._store_coefficient_blob("lstm", lstm_data)
        await self.execute("DELETE FROM ai_lstm_weights")
        await self.execute(
            "INSERT INTO ai_lstm_weights (weight_type, weight_index, weight_value)\n               VALUES (?, ?, ?)",
            (ARTIFACT_COEFFICIENT_SENTINEL, 0, 0.0),
        )

    @_atomic_database_write
    async def _save_ridge_weights(self, ridge_data: Dict[str, Any]) -> None:
        """Save Ridge weights to ai_ridge_weights table. @zara"""
        feature_means_json = None
        feature_stds_json = None
        await self.execute(
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
                SafeDateTimeUtil.now(),
            ),
        )
        await self._store_coefficient_blob("ridge", ridge_data)
        await self.execute("DELETE FROM ai_ridge_weights")

    async def save_model_candidate(
        self,
        training_run: Dict[str, Any],
        provenance_rows: List[Dict[str, Any]],
        weights_data: Dict[str, Any],
        reaction_context: Optional[Mapping[str, str]] = None,
        lease_owner: Optional[str] = None,
    ) -> str:
        """Persist a fully auditable candidate without mutating active model storage."""
        required_training_fields = {
            "config",
            "sample_count",
            "code_source_hash",
            "dataset_fingerprint",
            "training_config_hash",
        }
        if not required_training_fields.issubset(training_run):
            raise ValueError("incomplete training-run governance metadata")
        if training_run["dataset_fingerprint"] != canonical_stable_hash(
            provenance_rows
        ):
            raise ValueError("dataset fingerprint mismatch")
        if training_run["training_config_hash"] != canonical_stable_hash(
            training_run["config"]
        ):
            raise ValueError("training configuration hash mismatch")
        code_source_hash = training_run["code_source_hash"]
        if not isinstance(code_source_hash, str) or len(code_source_hash) != 64:
            raise ValueError("invalid code source hash")
        sample_count = int(training_run["sample_count"])
        output_count = int(training_run["config"].get("num_outputs", 0))
        if sample_count < 1 or output_count < 1:
            raise ValueError("invalid training sample/output count")
        expected_provenance_indices = {
            (sample_index, output_index)
            for sample_index in range(sample_count)
            for output_index in range(output_count)
        }
        actual_provenance_indices = {
            (provenance_row.get("sample_index"), provenance_row.get("output_index"))
            for provenance_row in provenance_rows
        }
        if (
            len(provenance_rows) != len(actual_provenance_indices)
            or actual_provenance_indices != expected_provenance_indices
        ):
            raise ValueError("training provenance is incomplete")
        for provenance_row in provenance_rows:
            if provenance_row.get("provenance_kind") not in MODEL_PROVENANCE_KINDS:
                raise ValueError("unknown training provenance")
            if not provenance_row.get("feature_digest") or not provenance_row.get(
                "forecast_prediction_id"
            ):
                raise ValueError("incomplete training sample provenance")
        training_run_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        created_at = SafeDateTimeUtil.now().isoformat()
        sealed_configuration = seal_protected_text(
            canonical_stable_json(training_run["config"]),
            "ai_training_runs",
            protected_identity(training_run_id, "config_json"),
        )
        model_artifacts = {
            artifact_type: weights_data[artifact_type]
            for artifact_type in MODEL_WEIGHT_ARTIFACT_TYPES
            if weights_data.get(artifact_type) is not None
        }
        if not model_artifacts:
            raise ValueError("candidate has no model artifacts")
        artifact_hash = canonical_stable_hash(model_artifacts)
        if weights_data.get("output_scale") is not None:
            model_artifacts["output_scale"] = weights_data["output_scale"]
        if reaction_context is not None:
            required_reaction_fields = {
                "request_id",
                "lease_owner",
                "supersede_candidate_id",
            }
            if set(reaction_context) != required_reaction_fields or not all(
                (
                    isinstance(reaction_context[reaction_field], str)
                    and reaction_context[reaction_field]
                    for reaction_field in required_reaction_fields
                )
            ):
                raise ValueError("invalid model reaction context")
        is_candidate_v2 = (
            training_run["config"].get("model_contract_version") == "candidate-v2"
        )
        if is_candidate_v2 and (not lease_owner):
            raise ValueError("candidate-v2 training lease is required")
        async with self.transaction():
            if lease_owner is not None:
                training_lease_row = await self.fetchone(
                    "SELECT owner, expires_at_epoch\n                         FROM ai_model_training_lease WHERE id = 1"
                )
                if (
                    not training_lease_row
                    or training_lease_row[0] != lease_owner
                    or float(training_lease_row[1]) <= time()
                ):
                    raise ValueError("model training lease is no longer valid")
            if reaction_context is not None:
                retrain_request_id = reaction_context["request_id"]
                superseded_candidate_id = reaction_context["supersede_candidate_id"]
                lease_owner = reaction_context["lease_owner"]
                reaction_lease_row = await self.fetchone(
                    "SELECT owner, retrain_request_id, expires_at_epoch\n                         FROM ai_model_training_lease WHERE id = 1"
                )
                retrain_request_row = await self.fetchone(
                    "SELECT status FROM ai_model_retrain_requests\n                         WHERE request_id = ?",
                    (retrain_request_id,),
                )
                superseded_candidate_row = await self.fetchone(
                    "SELECT 1 FROM ai_model_candidates candidate\n                         WHERE candidate.candidate_id = ?\n                           AND candidate.status = 'candidate'\n                           AND NOT EXISTS (\n                               SELECT 1 FROM ai_model_candidate_dispositions disposition\n                                WHERE disposition.candidate_id = candidate.candidate_id\n                           )\n                           AND NOT EXISTS (\n                               SELECT 1 FROM ai_model_evaluations evaluation\n                                WHERE evaluation.candidate_id = candidate.candidate_id\n                           )\n                           AND NOT EXISTS (\n                               SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                                WHERE evaluation.candidate_id = candidate.candidate_id\n                           )",
                    (superseded_candidate_id,),
                )
                if (
                    not reaction_lease_row
                    or reaction_lease_row[0] != lease_owner
                    or reaction_lease_row[1] != retrain_request_id
                    or (float(reaction_lease_row[2]) <= time())
                    or (not retrain_request_row)
                    or (retrain_request_row[0] != "pending")
                    or (not superseded_candidate_row)
                ):
                    raise ValueError("model reaction claim is no longer valid")
                await self.execute(
                    "INSERT INTO ai_model_candidate_dispositions\n                       (candidate_id, disposition, replacement_candidate_id,\n                        retrain_request_id, created_at)\n                       VALUES (?, 'superseded_by_confirmed_drift', ?, ?, ?)",
                    (
                        superseded_candidate_id,
                        candidate_id,
                        retrain_request_id,
                        created_at,
                    ),
                    auto_commit=False,
                )
            await self.execute(
                "INSERT INTO ai_training_runs\n                   (run_id, created_at, dataset_fingerprint, training_config_hash,\n                    code_source_hash, sample_count, config_json, schema_version,\n                    eligibility_version)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    training_run_id,
                    created_at,
                    training_run["dataset_fingerprint"],
                    training_run["training_config_hash"],
                    training_run["code_source_hash"],
                    int(training_run["sample_count"]),
                    sealed_configuration,
                    MODEL_GOVERNANCE_SCHEMA_VERSION,
                    MODEL_GOVERNANCE_ELIGIBILITY_VERSION,
                ),
                auto_commit=False,
            )
            await self.execute(
                "INSERT INTO ai_model_candidates\n                   (candidate_id, run_id, created_at, active_model, training_samples,\n                    accuracy, rmse, artifact_hash)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    training_run_id,
                    created_at,
                    weights_data.get("active_model", "none"),
                    int(weights_data.get("training_samples", 0)),
                    weights_data.get("accuracy"),
                    weights_data.get("rmse"),
                    artifact_hash,
                ),
                auto_commit=False,
            )
            await self.executemany(
                "INSERT INTO ai_training_sample_provenance\n                   (run_id, sample_index, output_index, forecast_prediction_id, forecast_anchor,\n                    target_prediction_id, target_anchor, prediction_lineage_id, group_lineage_id,\n                    group_name, target_value, feature_digest, provenance_kind,\n                    eligibility_version, schema_version)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        training_run_id,
                        provenance_row["sample_index"],
                        provenance_row["output_index"],
                        provenance_row["forecast_prediction_id"],
                        provenance_row["forecast_anchor"],
                        provenance_row["target_prediction_id"],
                        provenance_row["target_anchor"],
                        provenance_row.get("prediction_lineage_id"),
                        provenance_row.get("group_lineage_id"),
                        provenance_row.get("group_name"),
                        provenance_row["target_value"],
                        provenance_row["feature_digest"],
                        provenance_row["provenance_kind"],
                        MODEL_GOVERNANCE_ELIGIBILITY_VERSION,
                        MODEL_GOVERNANCE_SCHEMA_VERSION,
                    )
                    for provenance_row in provenance_rows
                ],
            )
            default_input_size = None
            for artifact_type in MODEL_WEIGHT_ARTIFACT_TYPES:
                if not model_artifacts.get(artifact_type) is not None:
                    continue
                default_input_size = artifact_cleartext_input_size(
                    model_artifacts[artifact_type]
                )
                if default_input_size is not None:
                    break
            for artifact_type, artifact_value in model_artifacts.items():
                sealed_artifact = seal_protected_text(
                    canonical_stable_json(artifact_value),
                    "ai_model_candidate_artifacts",
                    protected_identity(candidate_id, artifact_type),
                )
                artifact_input_size = artifact_cleartext_input_size(artifact_value)
                if artifact_input_size is None:
                    artifact_input_size = default_input_size
                await self.execute(
                    "INSERT INTO ai_model_candidate_artifacts\n                       (candidate_id, model_type, weights_json, weights_hash, input_size)\n                       VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        artifact_type,
                        sealed_artifact,
                        canonical_stable_hash(artifact_value),
                        artifact_input_size,
                    ),
                    auto_commit=False,
                )
            if reaction_context is not None:
                if not await self.model_candidate_covers_retrain_request(
                    reaction_context["request_id"], candidate_id
                ):
                    raise ValueError(
                        "replacement candidate does not cover drift evidence"
                    )
                await self.execute(
                    "UPDATE ai_model_retrain_requests\n                          SET status = 'bound', bound_candidate_id = ?,\n                              superseded_candidate_id = ?, updated_at = ?\n                        WHERE request_id = ? AND status = 'pending'",
                    (
                        candidate_id,
                        reaction_context["supersede_candidate_id"],
                        created_at,
                        reaction_context["request_id"],
                    ),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE drift_events SET response_executed = TRUE\n                         WHERE id IN (\n                             SELECT drift_event_id\n                               FROM ai_model_retrain_request_events\n                              WHERE request_id = ?\n                         )",
                    (reaction_context["request_id"],),
                    auto_commit=False,
                )
        if True:
            return candidate_id

    async def get_pending_model_candidate_id(self) -> Optional[str]:
        """Return the canonical unevaluated non-serving candidate."""
        row = await self.fetchone(
            "SELECT candidate.candidate_id\n                 FROM ai_model_candidates candidate\n                 LEFT JOIN ai_active_model_pointer pointer ON pointer.id = 1\n                WHERE candidate.status = 'candidate'\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                       WHERE terminal.candidate_id = candidate.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_dispositions disposition\n                       WHERE disposition.candidate_id = candidate.candidate_id\n                         AND NOT EXISTS (\n                             SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                              WHERE reconsideration.candidate_id = disposition.candidate_id\n                                AND disposition.disposition = 'migration_superseded'\n                                AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                         )\n                  )\n                  AND (pointer.active_candidate_id IS NULL\n                       OR candidate.candidate_id != pointer.active_candidate_id)\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations evaluation\n                       WHERE evaluation.candidate_id = candidate.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = candidate.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                       WHERE closure.candidate_id = candidate.candidate_id\n                  )\n                ORDER BY candidate.accuracy DESC NULLS LAST,\n                         candidate.training_samples DESC,\n                         candidate.rmse ASC NULLS LAST,\n                         candidate.created_at, candidate.candidate_id\n                LIMIT 1"
        )
        return str(row[0]) if row else None

    async def end_of_day_completion_is_missing(
        self, now: Optional[datetime] = None
    ) -> bool:
        """Return True when yesterday has no completed end-of-day summary."""
        current_time = now or SafeDateTimeUtil.now()
        if not isinstance(current_time, datetime):
            raise ValueError("end-of-day clock is invalid")
        if current_time.tzinfo is None:
            local_zone = SafeDateTimeUtil.get_default_time_zone()
            if local_zone is None:
                raise ValueError("end-of-day timezone is unavailable")
            current_time = current_time.replace(tzinfo=local_zone)
        timestamp_iso = (current_time.date() - timedelta(days=1)).isoformat()
        row = await self.fetchone(
            "SELECT date FROM daily_summaries ORDER BY date DESC LIMIT 1"
        )
        if not row or row[0] is None:
            return True
        target_date_prefix = str(row[0])[:10]
        try:
            date.fromisoformat(target_date_prefix)
        except ValueError as error:
            raise ValueError("end-of-day summary date is invalid") from error
        return target_date_prefix < timestamp_iso

    @staticmethod
    def _candidate_age_calendar_days(
        created_at: Any, now_value: datetime
    ) -> Optional[int]:
        parsed_datetime = created_at
        if not isinstance(parsed_datetime, datetime):
            try:
                parsed_datetime = datetime.fromisoformat(str(parsed_datetime))
            except (TypeError, ValueError):
                return
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=now_value.tzinfo)
        return max(0, (now_value.date() - parsed_datetime.date()).days)

    async def timeout_unevaluated_model_candidate(
        self, *, source: str = "eod", readiness: Optional[Mapping[str, Any]] = None
    ) -> Optional[str]:
        """Close a pending candidate after 10 calendar days, without promote.

        Evaluation uses eligible paired days. This closer is only the calendar
        cap. Startup may apply it only when yesterday's end-of-day is missing,
        so a restart cannot kill a candidate the night before EOD can judge it.
        """
        if source not in MODEL_CANDIDATE_EVALUATION_TIMEOUT_SOURCES:
            raise ValueError("timeout source must be eod or startup")
        candidate_id = await self.get_pending_model_candidate_id()
        if not candidate_id:
            return
        candidate_row = await self.fetchone(
            "SELECT created_at FROM ai_model_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        if not candidate_row or candidate_row[0] is None:
            return None
        current_time = SafeDateTimeUtil.now()
        age_days = self._candidate_age_calendar_days(candidate_row[0], current_time)
        if age_days is None:
            return
        if age_days < MODEL_CANDIDATE_EVALUATION_TIMEOUT_CALENDAR_DAYS:
            return
        if source == "startup":
            if not await self.end_of_day_completion_is_missing(current_time):
                return
        wait_reason = None
        eligible_days = None
        policy_eligible_days = None
        if isinstance(readiness, Mapping):
            wait_reason = readiness.get("wait_reason")
            eligible_days = readiness.get("eligible_days")
            policy_eligible_days = readiness.get("policy_eligible_days")
        close_reason = MODEL_CANDIDATE_TIMEOUT_CLOSE_CALENDAR_CAP
        if wait_reason == "INSUFFICIENT_REGIME_COVERAGE" or (
            isinstance(eligible_days, int)
            and (not isinstance(eligible_days, bool))
            and (eligible_days >= MODEL_PROMOTION_MIN_INDEPENDENT_DAYS)
        ):
            close_reason = MODEL_CANDIDATE_TIMEOUT_CLOSE_REGIME_COVERAGE
        terminal_payload = {
            "age_days": age_days,
            "calendar_day_limit": MODEL_CANDIDATE_EVALUATION_TIMEOUT_CALENDAR_DAYS,
            "source": source,
            "close_reason": close_reason,
            "wait_reason": wait_reason,
            "eligible_days": eligible_days,
            "policy_eligible_days": policy_eligible_days,
        }
        repair_state = await self._record_model_candidate_terminal_event(
            candidate_id, "evidence_timeout", terminal_payload
        )
        if repair_state:
            _LOGGER.info(
                "Model candidate evaluation timed out without promotion: candidate=%s age_days=%d close_reason=%s wait_reason=%s eligible_days=%s source=%s",
                candidate_id,
                age_days,
                close_reason,
                wait_reason,
                eligible_days,
                source,
            )
            return candidate_id

    async def persist_ai_gate_status(
        self, candidate_id: Optional[str], readiness: Mapping[str, Any]
    ) -> None:
        """Replace the one-row last-known gate readiness without recomputing it."""
        if not isinstance(readiness, Mapping):
            raise ValueError("readiness must be a mapping")
        created_at = None
        if candidate_id:
            row = await self.fetchone(
                "SELECT created_at FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if row:
                created_at = row[0]
        await self.execute(
            "INSERT INTO ai_gate_status (\n                   id, candidate_id, eligible_days, required_days,\n                   policy_eligible_days, eligible_common_hours, wait_reason,\n                   candidate_created_at, calendar_day_limit, computed_at\n               ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   candidate_id=excluded.candidate_id,\n                   eligible_days=excluded.eligible_days,\n                   required_days=excluded.required_days,\n                   policy_eligible_days=excluded.policy_eligible_days,\n                   eligible_common_hours=excluded.eligible_common_hours,\n                   wait_reason=excluded.wait_reason,\n                   candidate_created_at=excluded.candidate_created_at,\n                   calendar_day_limit=excluded.calendar_day_limit,\n                   computed_at=excluded.computed_at",
            (
                candidate_id,
                readiness.get("eligible_days"),
                readiness.get("required_days"),
                readiness.get("policy_eligible_days"),
                readiness.get("eligible_common_hours"),
                readiness.get("wait_reason"),
                created_at,
                MODEL_CANDIDATE_EVALUATION_TIMEOUT_CALENDAR_DAYS,
                SafeDateTimeUtil.now(),
            ),
        )

    async def get_last_successful_model_training_time(self) -> Optional[datetime]:
        """Return the newest successfully persisted governed training run."""
        row = await self.fetchone(
            "SELECT created_at FROM ai_training_runs\n               ORDER BY julianday(created_at) DESC, created_at DESC\n               LIMIT 1"
        )
        if not row or row[0] is None:
            return None
        if isinstance(row[0], datetime):
            return row[0]
        try:
            return datetime.fromisoformat(str(row[0]))
        except (TypeError, ValueError) as error:
            raise ValueError("latest model training time is invalid") from error

    async def get_ai_governance_diagnostic_snapshot(self) -> Dict[str, Any]:
        """Return one DB-derived snapshot for coordinator-owned diagnostics."""

        async def cleartext_input_size(model_type: str | None) -> Any:
            if model_type == "tiny_lstm":
                table_name = "ai_lstm_meta"
            elif model_type == "ridge":
                table_name = "ai_ridge_meta"
            else:
                return
            input_size_row = await self.fetchone(
                f"SELECT input_size FROM {table_name} WHERE id = 1"
            )
            return input_size_row[0] if input_size_row else None

        serving_candidate_row = await self.fetchone(
            "SELECT candidate.training_samples, candidate.accuracy, candidate.rmse,\n                      candidate.created_at, candidate.active_model,\n                      candidate.candidate_id\n                 FROM ai_active_model_pointer pointer\n                 JOIN ai_model_candidates candidate\n                   ON candidate.candidate_id = pointer.active_candidate_id\n                WHERE pointer.id = 1"
        )
        legacy_model_row = None
        if not serving_candidate_row:
            legacy_model_row = await self.fetchone(
                "SELECT training_samples, accuracy, rmse, last_trained, active_model\n                     FROM ai_learned_weights_meta\n                    WHERE id = 1 AND active_model IN ('tiny_lstm', 'ridge')\n                      AND training_samples > 0"
            )
        pending_candidate_rows = await self.fetchall(
            "SELECT candidate.candidate_id, candidate.training_samples, candidate.accuracy,\n                      candidate.rmse, candidate.created_at, candidate.active_model\n                 FROM ai_model_candidates candidate\n                 WHERE candidate.status = 'candidate'\n                   AND NOT EXISTS (\n                       SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                        WHERE terminal.candidate_id = candidate.candidate_id\n                   )\n                   AND NOT EXISTS (SELECT 1 FROM ai_model_evaluations evaluation\n                                    WHERE evaluation.candidate_id = candidate.candidate_id)\n                   AND NOT EXISTS (SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                                    WHERE evaluation.candidate_id = candidate.candidate_id)\n                   AND NOT EXISTS (\n                       SELECT 1 FROM ai_model_candidate_dispositions disposition\n                        WHERE disposition.candidate_id = candidate.candidate_id\n                          AND NOT EXISTS (SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                           WHERE reconsideration.candidate_id = disposition.candidate_id\n                                             AND disposition.disposition = 'migration_superseded'\n                                             AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                             AND reconsideration.candidate_id != reconsideration.keeper_candidate_id)\n                   )\n                   AND NOT EXISTS (\n                       SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                        WHERE closure.candidate_id = candidate.candidate_id\n                   )\n                 ORDER BY candidate.accuracy DESC NULLS LAST, candidate.training_samples DESC,\n                          candidate.rmse ASC NULLS LAST, candidate.created_at, candidate.candidate_id"
        )

        async def candidate_artifact_input_size(
            candidate_id: str, model_type: str
        ) -> Any:
            artifact_type = "lstm" if model_type == "tiny_lstm" else model_type
            input_size_row = await self.fetchone(
                "SELECT input_size FROM ai_model_candidate_artifacts\n                    WHERE candidate_id = ? AND model_type = ?",
                (candidate_id, artifact_type),
            )
            return input_size_row[0] if input_size_row else None

        selected_candidate_row = (
            pending_candidate_rows[0] if pending_candidate_rows else None
        )
        if serving_candidate_row:
            serving_input_size = await candidate_artifact_input_size(
                serving_candidate_row[5], serving_candidate_row[4]
            )
        elif legacy_model_row:
            serving_input_size = await cleartext_input_size(legacy_model_row[4])
        else:
            serving_input_size = None
        pending_input_size = (
            await candidate_artifact_input_size(
                selected_candidate_row[0], selected_candidate_row[5]
            )
            if selected_candidate_row
            else None
        )
        eligible_independent_days = 0
        critical_slices_ready = False
        missing_weather_regime = False
        post_midnight_run_count = 0
        pre_sunrise_run_count = 0
        if selected_candidate_row:
            cycle_count_row = await self.fetchone(
                'SELECT SUM(CASE WHEN forecast_cycle = "post_midnight" THEN 1 ELSE 0 END),\n                          SUM(CASE WHEN forecast_cycle = "pre_sunrise" THEN 1 ELSE 0 END)\n                     FROM ai_model_shadow_runs run JOIN ai_model_shadow_run_provenance provenance\n                       ON provenance.shadow_run_id = run.shadow_run_id WHERE run.candidate_id = ?',
                (selected_candidate_row[0],),
            )
            post_midnight_run_count = (
                int(cycle_count_row[0] or 0) if cycle_count_row else 0
            )
            pre_sunrise_run_count = (
                int(cycle_count_row[1] or 0) if cycle_count_row else 0
            )
            candidate_hash_row = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (selected_candidate_row[0],),
            )
            try:
                champion_artifact_hash = await self.get_active_model_artifact_hash()
            except (ValueError, json.JSONDecodeError):
                champion_artifact_hash = None
            paired_evidence_rows = []
            if candidate_hash_row and champion_artifact_hash:
                paired_evidence_rows = await self.fetchall(
                    "WITH ranked_runs AS (\n                           SELECT run.shadow_run_id, run.forecast_anchor,\n                                  provenance.forecast_cycle,\n                                  ROW_NUMBER() OVER (\n                                      PARTITION BY run.run_date, provenance.forecast_cycle\n                                      ORDER BY COALESCE(batch.completed_at, run.created_at),\n                                               run.created_at, run.shadow_run_id\n                                  ) AS cycle_rank\n                             FROM ai_model_shadow_runs run\n                             JOIN ensemble_shadow_batches batch\n                               ON batch.morning_batch_id = run.morning_batch_id\n                             JOIN ai_model_shadow_run_provenance provenance\n                               ON provenance.shadow_run_id = run.shadow_run_id\n                            WHERE batch.status = 'completed'\n                              AND run.contract_version = 'model_candidate_shadow_v2'\n                              AND provenance.contract_version = 'model_candidate_shadow_v2'\n                              AND provenance.promotion_eligible = 1\n                              AND provenance.trigger_kind = 'scheduled'\n                              AND provenance.forecast_cycle IN\n                                  ('post_midnight', 'pre_sunrise')\n                              AND run.candidate_id = ?\n                              AND run.champion_artifact_hash = ?\n                              AND run.candidate_artifact_hash = ?\n                         )\n                         SELECT prediction.target_date, prediction.target_hour,\n                                ranked.forecast_cycle, actual.actual_kwh,\n                                prediction.champion_prediction,\n                                prediction.candidate_prediction,\n                                actual.clean_eligible, ranked.forecast_anchor,\n                                prediction.input_fingerprint,\n                                prediction.policy_fingerprint,\n                                prediction.weather_regime, prediction.horizon,\n                                ranked.shadow_run_id,\n                                prediction.shadow_prediction_id,\n                                actual.actual_version_id\n                           FROM ranked_runs ranked\n                           JOIN ai_model_shadow_predictions prediction\n                             ON prediction.shadow_run_id = ranked.shadow_run_id\n                           JOIN ai_model_shadow_actual_versions actual\n                             ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                            AND actual.evaluation_version = (\n                                SELECT MAX(version.evaluation_version)\n                                  FROM ai_model_shadow_actual_versions version\n                                 WHERE version.shadow_prediction_id =\n                                       prediction.shadow_prediction_id\n                            )\n                          WHERE ranked.cycle_rank = 1\n                            AND prediction.horizon = 'day_0'\n                          ORDER BY prediction.target_date, prediction.target_hour,\n                                   ranked.forecast_cycle",
                    (
                        selected_candidate_row[0],
                        champion_artifact_hash,
                        candidate_hash_row[0],
                    ),
                )
            evidence_by_date_and_cycle = {}
            for evidence_row in paired_evidence_rows:
                prediction_values = (evidence_row[3], evidence_row[4], evidence_row[5])
                if (
                    not bool(evidence_row[6])
                    or not evidence_row[3] is not None
                    or float(evidence_row[3]) <= PRODUCTION_CANDIDATE_KWH_THRESHOLD
                    or any(
                        (
                            prediction_value is None
                            for prediction_value in prediction_values
                        )
                    )
                    or (
                        not all(
                            (
                                math.isfinite(float(prediction_value))
                                for prediction_value in prediction_values
                            )
                        )
                    )
                    or any(
                        (
                            float(prediction_value) < 0
                            for prediction_value in prediction_values[1:]
                        )
                    )
                ):
                    continue
                evidence_by_date_and_cycle.setdefault(
                    str(evidence_row[0]), {}
                ).setdefault(str(evidence_row[2]), {})[
                    int(evidence_row[1])
                ] = evidence_row
                continue
            eligible_evidence_rows = []
            for target_date in sorted(evidence_by_date_and_cycle):
                cycle_evidence = evidence_by_date_and_cycle[target_date]
                if any(
                    (
                        required_cycle not in cycle_evidence
                        for required_cycle in ("post_midnight", "pre_sunrise")
                    )
                ):
                    continue
                common_hours = set(cycle_evidence["post_midnight"]) & set(
                    cycle_evidence["pre_sunrise"]
                )
                if len(common_hours) < MODEL_PROMOTION_MIN_COMMON_HOURS_PER_CYCLE:
                    continue
                for current_cycle in ("post_midnight", "pre_sunrise"):
                    eligible_evidence_rows.extend(
                        (
                            cycle_evidence[current_cycle][hour]
                            for hour in sorted(common_hours)
                        )
                    )
            eligible_independent_days = len(
                {str(evidence_row[0]) for evidence_row in eligible_evidence_rows}
            )
            missing_weather_regime = any(
                (
                    not isinstance(evidence_row[10], str)
                    or not evidence_row[10].strip()
                    for evidence_row in eligible_evidence_rows
                )
            )
            critical_slice_dates = {}
            if not missing_weather_regime:
                for evidence_row in eligible_evidence_rows:
                    critical_slice_key = (
                        str(evidence_row[2]),
                        evidence_row[10].strip(),
                    )
                    critical_slice_dates.setdefault(critical_slice_key, set()).add(
                        str(evidence_row[0])
                    )
            critical_slices_ready = bool(critical_slice_dates) and all(
                (
                    len(slice_dates) >= MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS
                    for slice_dates in critical_slice_dates.values()
                )
            )
        bootstrap_active = (
            serving_candidate_row is None and selected_candidate_row is not None
        )
        snapshot = {
            "source": "governance_db",
            "serving": (
                {
                    "training_samples": int(
                        (serving_candidate_row or legacy_model_row)[0]
                    ),
                    "r2_score": (serving_candidate_row or legacy_model_row)[1],
                    "rmse": (serving_candidate_row or legacy_model_row)[2],
                    "trained_at": (serving_candidate_row or legacy_model_row)[3],
                    "model_type": (serving_candidate_row or legacy_model_row)[4],
                    "input_size": serving_input_size,
                    "serving_source": (
                        "governance_pointer"
                        if serving_candidate_row
                        else "legacy_fallback"
                    ),
                }
                if serving_candidate_row or legacy_model_row
                else None
            ),
            "pending_candidate_count": len(pending_candidate_rows),
            "pending": (
                {
                    "candidate_id": selected_candidate_row[0],
                    "training_samples": int(selected_candidate_row[1]),
                    "r2_score": selected_candidate_row[2],
                    "rmse": selected_candidate_row[3],
                    "trained_at": selected_candidate_row[4],
                    "model_type": selected_candidate_row[5],
                    "input_size": pending_input_size,
                }
                if selected_candidate_row
                else None
            ),
            "bootstrap": {
                "active": bootstrap_active,
                "selected_candidate_id": (
                    selected_candidate_row[0] if selected_candidate_row else None
                ),
                "eligible_independent_days": eligible_independent_days,
                "required_independent_days": MODEL_PROMOTION_MIN_INDEPENDENT_DAYS,
                "critical_slices_ready": critical_slices_ready,
                "missing_weather_regime": missing_weather_regime,
                "post_midnight_runs": post_midnight_run_count,
                "pre_sunrise_runs": pre_sunrise_run_count,
                "next_eligible_shadow_cycle": (
                    "post_midnight"
                    if post_midnight_run_count <= pre_sunrise_run_count
                    else "pre_sunrise"
                ),
                "waiting_message": (
                    f"Waiting for eligible paired shadow evidence: {eligible_independent_days}/{MODEL_PROMOTION_MIN_INDEPENDENT_DAYS} independent days; no promotion before evaluation."
                    + (
                        " Waiting for complete weather-regime evidence."
                        if missing_weather_regime
                        else (
                            " Waiting for adequately powered critical weather regimes."
                            if eligible_independent_days
                            >= MODEL_PROMOTION_MIN_INDEPENDENT_DAYS
                            and (not critical_slices_ready)
                            else ""
                        )
                    )
                    if bootstrap_active
                    else None
                ),
            },
        }
        gate_status_row = await self.fetchone(
            "SELECT candidate_id, eligible_days, required_days,\n                      policy_eligible_days, eligible_common_hours, wait_reason,\n                      candidate_created_at, calendar_day_limit, computed_at\n                 FROM ai_gate_status WHERE id = 1"
        )
        if gate_status_row:
            snapshot["gate"] = {
                "candidate_id": gate_status_row[0],
                "eligible_days": gate_status_row[1],
                "required_days": gate_status_row[2],
                "policy_eligible_days": gate_status_row[3],
                "eligible_common_hours": gate_status_row[4],
                "wait_reason": gate_status_row[5],
                "candidate_created_at": gate_status_row[6],
                "calendar_day_limit": gate_status_row[7],
                "computed_at": gate_status_row[8],
            }
        return snapshot

    async def reconsider_migration_superseded_model_candidate(
        self,
        candidate_id: str,
        keeper_candidate_id: str,
        *,
        runtime_compatible_candidate_ids: Sequence[str],
    ) -> bool:
        """Append one guarded reconsideration without changing disposition history."""
        runtime_compatible_ids = set(runtime_compatible_candidate_ids)
        if (
            not candidate_id
            or not keeper_candidate_id
            or {candidate_id, keeper_candidate_id} - runtime_compatible_ids
        ):
            raise ValueError("reconsideration requires verified runtime compatibility")
        async with self.transaction():
            disposition_row = await self.fetchone(
                "SELECT disposition, replacement_candidate_id\n                     FROM ai_model_candidate_dispositions WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not disposition_row or disposition_row[0] != "migration_superseded":
                raise ValueError(
                    "only migration-superseded candidates are reconsiderable"
                )
            if disposition_row[1] != keeper_candidate_id:
                raise ValueError("reconsideration keeper does not match disposition")
            if await self.fetchone(
                "SELECT 1 FROM ai_model_candidate_reconsideration_closures\n                    WHERE candidate_id = ?",
                (candidate_id,),
            ):
                raise ValueError("candidate reconsideration is already terminal")
            if await self.fetchone(
                "SELECT 1 FROM ai_model_candidate_reconsiderations WHERE candidate_id = ?",
                (candidate_id,),
            ):
                return False
            for checked_candidate_id in (candidate_id, keeper_candidate_id):
                artifact_hash_row = await self.fetchone(
                    "SELECT artifact_hash FROM ai_model_candidates\n                         WHERE candidate_id = ? AND status = 'candidate'",
                    (checked_candidate_id,),
                )
                model_artifacts = await self.fetchall(
                    "SELECT model_type, weights_json, weights_hash\n                         FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
                    (checked_candidate_id,),
                )
                decoded_results = {}
                for artifact_type, sealed_artifact, artifact_hash in model_artifacts:
                    artifact_value = loads_protected_json(
                        sealed_artifact,
                        "ai_model_candidate_artifacts",
                        protected_identity(checked_candidate_id, artifact_type),
                        "weights",
                    )
                    if canonical_stable_hash(artifact_value) != artifact_hash:
                        raise ValueError("candidate artifact hash mismatch")
                    decoded_results[artifact_type] = artifact_value
                if (
                    not artifact_hash_row
                    or not decoded_results
                    or canonical_stable_hash(model_weight_artifacts(decoded_results))
                    != artifact_hash_row[0]
                ):
                    raise ValueError(
                        "candidate artifact set is incomplete or mismatched"
                    )
            candidate_rows = await self.fetchall(
                "WITH shadow_samples AS (\n                       SELECT run.candidate_id, prediction.target_date, prediction.target_hour,\n                              provenance.forecast_cycle\n                         FROM ai_model_shadow_runs run\n                         JOIN ai_model_shadow_run_provenance provenance\n                           ON provenance.shadow_run_id = run.shadow_run_id\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_run_id = run.shadow_run_id\n                         JOIN ai_model_shadow_actual_versions actual\n                           ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                        WHERE provenance.promotion_eligible = 1\n                          AND provenance.forecast_cycle IN ('post_midnight', 'pre_sunrise')\n                          AND actual.clean_eligible = 1 AND actual.actual_kwh IS NOT NULL\n                    ), paired AS (\n                       SELECT candidate_id, target_date, target_hour FROM shadow_samples\n                        GROUP BY candidate_id, target_date, target_hour\n                       HAVING COUNT(DISTINCT forecast_cycle) = 2\n                    ), evidence AS (\n                       SELECT candidate_id, COUNT(*) AS days FROM (\n                           SELECT candidate_id, target_date FROM paired\n                            GROUP BY candidate_id, target_date HAVING COUNT(*) >= 4\n                       ) GROUP BY candidate_id\n                    )\n                    SELECT candidate.candidate_id, COALESCE(evidence.days, 0),\n                           candidate.accuracy, candidate.training_samples, candidate.rmse\n                      FROM ai_model_candidates candidate LEFT JOIN evidence\n                        ON evidence.candidate_id = candidate.candidate_id\n                     WHERE candidate.candidate_id IN (?, ?)",
                (candidate_id, keeper_candidate_id),
            )
            candidate_metadata_by_id = {str(row[0]): row[1:] for row in candidate_rows}
            candidate_metadata = candidate_metadata_by_id.get(candidate_id)
            keeper_metadata = candidate_metadata_by_id.get(keeper_candidate_id)
            if not candidate_metadata or not keeper_metadata:
                raise ValueError("reconsideration evidence is unavailable")
            training_samples, accuracy, eligible_days, rmse = candidate_metadata
            keeper_training_samples, unused_item, unused_item, unused_item = (
                keeper_metadata
            )
            if keeper_training_samples >= MODEL_PROMOTION_MIN_INDEPENDENT_DAYS:
                raise ValueError(
                    "keeper already has promotion-eligible shadow evidence"
                )
            candidate_rank = (
                int(training_samples),
                float(accuracy) if accuracy is not None else float("-inf"),
                int(eligible_days),
                -(float(rmse) if rmse is not None else float("inf")),
            )
            keeper_rank = (
                int(keeper_metadata[0]),
                (
                    float(keeper_metadata[1])
                    if keeper_metadata[1] is not None
                    else float("-inf")
                ),
                int(keeper_metadata[2]),
                -(
                    float(keeper_metadata[3])
                    if keeper_metadata[3] is not None
                    else float("inf")
                ),
            )
            if candidate_rank <= keeper_rank:
                raise ValueError("reconsidered candidate is not uniquely better")
            await self.execute(
                "INSERT INTO ai_model_candidate_reconsiderations\n                   (reconsideration_id, candidate_id, keeper_candidate_id, created_at)\n                   VALUES (?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    candidate_id,
                    keeper_candidate_id,
                    SafeDateTimeUtil.now().isoformat(),
                ),
                auto_commit=False,
            )
        if True:
            return True

    async def get_migration_superseded_reconsideration_candidates(
        self,
    ) -> List[Tuple[str, str]]:
        """Return unreconsidered migration dispositions for guarded runtime review.

        A superseded candidate with a terminal lifecycle event can never become
        pending again (see get_pending_model_candidate_ids), so it is not
        offered for review either.
        """
        rows = await self.fetchall(
            "SELECT disposition.candidate_id, disposition.replacement_candidate_id\n                 FROM ai_model_candidate_dispositions disposition\n                WHERE disposition.disposition = 'migration_superseded'\n                  AND disposition.replacement_candidate_id IS NOT NULL\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                       WHERE reconsideration.candidate_id = disposition.candidate_id\n                         AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                         AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                       WHERE terminal.candidate_id = disposition.candidate_id\n                  )\n                ORDER BY disposition.created_at, disposition.candidate_id"
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    async def close_runtime_incompatible_model_candidate(
        self, candidate_id: str, *, details: Mapping[str, Any]
    ) -> bool:
        """Terminalize one non-serving candidate that cannot run in the serving architecture.

        Dispositions are append-only, so a migration-superseded candidate keeps
        its disposition; the terminal event is the immutable fact that stops
        further runtime review. Returns False when the candidate is already
        terminal.
        """
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate id is required")
        row = await self.fetchone(
            "SELECT 1 FROM ai_model_candidates WHERE candidate_id = ?", (candidate_id,)
        )
        if not row:
            raise ValueError("unknown model candidate")
        row_2 = await self.fetchone(
            "SELECT active_candidate_id, last_known_good_candidate_id\n                 FROM ai_active_model_pointer WHERE id = 1"
        )
        if row_2 and candidate_id in {row_2[0], row_2[1]}:
            raise ValueError("serving candidates cannot be closed as incompatible")
        return await self._record_model_candidate_terminal_event(
            candidate_id,
            "incompatible",
            {"candidate_id": candidate_id, **dict(details)},
        )

    async def get_pending_model_candidate_ids(self, limit: int = 8) -> List[str]:
        """Return oldest-first unevaluated non-serving candidate IDs."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("pending candidate limit must be positive")
        rows = await self.fetchall(
            "SELECT c.candidate_id\n               FROM ai_model_candidates c\n               LEFT JOIN ai_active_model_pointer p ON p.id = 1\n               WHERE c.status = 'candidate'\n                 AND NOT EXISTS (\n                     SELECT 1 FROM ai_model_candidate_dispositions disposition\n                      WHERE disposition.candidate_id = c.candidate_id\n                        AND NOT EXISTS (\n                            SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                             WHERE reconsideration.candidate_id = disposition.candidate_id\n                               AND disposition.disposition = 'migration_superseded'\n                               AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                               AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                        )\n                 )\n                 AND (p.active_candidate_id IS NULL\n                      OR c.candidate_id != p.active_candidate_id)\n                 AND NOT EXISTS (\n                     SELECT 1 FROM ai_model_evaluations e\n                     WHERE e.candidate_id = c.candidate_id\n                 )\n                 AND NOT EXISTS (\n                     SELECT 1 FROM ai_model_evaluations_v2 e2\n                     WHERE e2.candidate_id = c.candidate_id\n                 )\n                 AND NOT EXISTS (\n                     SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                      WHERE closure.candidate_id = c.candidate_id\n                 )\n                 AND NOT EXISTS (\n                     SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                      WHERE terminal.candidate_id = c.candidate_id\n                 )\n               ORDER BY c.created_at, c.candidate_id\n               LIMIT ?",
            (limit,),
        )
        return [str(row[0]) for row in rows]

    async def acquire_model_training_lease(
        self,
        owner: str,
        retrain_request_id: Optional[str] = None,
        lease_seconds: int = 3600,
    ) -> bool:
        """Acquire the singleton cross-caller training lease."""
        if not owner or lease_seconds < 60:
            raise ValueError("invalid model training lease")
        lease_started_at = time()
        lease_expires_at = lease_started_at + lease_seconds
        async with self.transaction():
            await self.execute(
                "INSERT INTO ai_model_training_lease\n                   (id, owner, retrain_request_id, acquired_at_epoch, expires_at_epoch)\n                   VALUES (1, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       owner = excluded.owner,\n                       retrain_request_id = excluded.retrain_request_id,\n                       acquired_at_epoch = excluded.acquired_at_epoch,\n                       expires_at_epoch = excluded.expires_at_epoch\n                   WHERE ai_model_training_lease.expires_at_epoch <= ?",
                (
                    owner,
                    retrain_request_id,
                    lease_started_at,
                    lease_expires_at,
                    lease_started_at,
                ),
                auto_commit=False,
            )
            row = await self.fetchone(
                "SELECT owner, retrain_request_id, expires_at_epoch\n                     FROM ai_model_training_lease WHERE id = 1"
            )
        return bool(
            row
            and row[0] == owner
            and (row[1] == retrain_request_id)
            and (float(row[2]) > lease_started_at)
        )

    async def renew_model_training_lease(
        self, owner: str, lease_seconds: int = 3600
    ) -> bool:
        """Extend an unexpired training lease only for its current owner."""
        if not owner or lease_seconds < 60:
            raise ValueError("invalid model training lease renewal")
        lease_started_at = time()
        lease_expires_at = lease_started_at + lease_seconds
        async with self.transaction():
            await self.execute(
                "UPDATE ai_model_training_lease\n                      SET expires_at_epoch = ?\n                    WHERE id = 1 AND owner = ? AND expires_at_epoch > ?",
                (lease_expires_at, owner, lease_started_at),
                auto_commit=False,
            )
            row = await self.fetchone(
                "SELECT owner, expires_at_epoch\n                     FROM ai_model_training_lease WHERE id = 1"
            )
        return bool(row and row[0] == owner and (float(row[1]) >= lease_expires_at))

    async def release_model_training_lease(self, owner: str) -> None:
        """Release a training lease only when owned by the caller."""
        await self.execute(
            "DELETE FROM ai_model_training_lease WHERE id = 1 AND owner = ?", (owner,)
        )

    async def get_or_create_confirmed_model_retrain_request(
        self, through_date: str
    ) -> Optional[Dict[str, Any]]:
        """Coalesce repeated rolling-MAE drift into one restart-safe request."""
        through_date_iso = date.fromisoformat(through_date).isoformat()
        open_request = await self.get_open_model_retrain_request()
        confirmation_rules = (("critical", 3, 2), ("warning", 5, 3))
        confirmed_evidence = None
        for severity, window_days, required_confirmation_days in confirmation_rules:
            drift_event_rows = await self.fetchall(
                "SELECT event.id, event.event_date\n                     FROM drift_events event\n                    WHERE event.scope = 'global'\n                      AND event.drift_type = 'mae_above_baseline'\n                      AND event.severity = ?\n                      AND event.response_action = 'light_retrain'\n                      AND COALESCE(event.response_executed, FALSE) = FALSE\n                      AND event.event_date BETWEEN date(?, ?) AND date(?)\n                      AND (\n                          SELECT COUNT(*) FROM hourly_predictions prediction\n                           WHERE prediction.target_date = event.event_date\n                             AND prediction.actual_kwh IS NOT NULL\n                             AND prediction.actual_kwh > 0.01\n                             AND prediction.prediction_kwh IS NOT NULL\n                             AND COALESCE(prediction.exclude_from_learning, FALSE) = FALSE\n                             AND COALESCE(prediction.exclude_from_clean_evaluation, FALSE) = FALSE\n                             AND COALESCE(prediction.mppt_throttled, FALSE) = FALSE\n                             AND COALESCE(prediction.inverter_clipped, FALSE) = FALSE\n                      ) >= 4\n                    ORDER BY event.event_date, event.id",
                (
                    severity,
                    through_date_iso,
                    f"-{window_days - 1} days",
                    through_date_iso,
                ),
            )
            if (
                len({str(event_row[1]) for event_row in drift_event_rows})
                >= required_confirmation_days
            ):
                confirmed_evidence = (severity, drift_event_rows)
                break
        if confirmed_evidence is None:
            return open_request
        confirmed_severity, drift_event_rows = confirmed_evidence
        confirmed_event_dates = sorted(
            {str(event_row[1]) for event_row in drift_event_rows}
        )
        updated_at = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            request_row = await self.fetchone(
                "SELECT request_id, status, severity, first_event_date,\n                          last_event_date, confirmation_days, bound_candidate_id,\n                          superseded_candidate_id\n                     FROM ai_model_retrain_requests\n                    WHERE scope = 'global'\n                      AND drift_type = 'mae_above_baseline'\n                      AND status IN ('pending', 'bound')\n                    ORDER BY created_at LIMIT 1"
            )
            if request_row:
                request_id = str(request_row[0])
                status = str(request_row[1])
                severity = str(request_row[2])
                first_event_date = str(request_row[3])
                last_event_date = str(request_row[4])
                confirmation_days = int(request_row[5])
                bound_candidate_id = request_row[6]
                superseded_candidate_id = request_row[7]
                events_to_link = []
                if status == "pending":
                    linked_event_dates = {
                        str(event_row[0])
                        for event_row in await self.fetchall(
                            "SELECT event.event_date\n                                 FROM ai_model_retrain_request_events link\n                                 JOIN drift_events event\n                                   ON event.id = link.drift_event_id\n                                WHERE link.request_id = ?",
                            (request_id,),
                        )
                    }
                    combined_event_dates = sorted(
                        linked_event_dates | set(confirmed_event_dates)
                    )
                    first_event_date = combined_event_dates[0]
                    last_event_date = combined_event_dates[-1]
                    confirmation_days = len(combined_event_dates)
                    if confirmed_severity == "critical":
                        severity = "critical"
                    await self.execute(
                        "UPDATE ai_model_retrain_requests\n                              SET updated_at = ?, severity = ?, first_event_date = ?,\n                                  last_event_date = ?, confirmation_days = ?\n                            WHERE request_id = ?",
                        (
                            updated_at,
                            severity,
                            first_event_date,
                            last_event_date,
                            confirmation_days,
                            request_id,
                        ),
                        auto_commit=False,
                    )
                    events_to_link = drift_event_rows
            else:
                request_id = str(uuid.uuid4())
                severity = confirmed_severity
                first_event_date = confirmed_event_dates[0]
                last_event_date = confirmed_event_dates[-1]
                confirmation_days = len(confirmed_event_dates)
                status = "pending"
                bound_candidate_id = None
                superseded_candidate_id = None
                events_to_link = drift_event_rows
                await self.execute(
                    "INSERT INTO ai_model_retrain_requests\n                       (request_id, created_at, updated_at, status, scope,\n                        drift_type, severity, first_event_date, last_event_date,\n                        confirmation_days)\n                       VALUES (?, ?, ?, 'pending', 'global',\n                               'mae_above_baseline', ?, ?, ?, ?)",
                    (
                        request_id,
                        updated_at,
                        updated_at,
                        severity,
                        first_event_date,
                        last_event_date,
                        confirmation_days,
                    ),
                    auto_commit=False,
                )
            await self.executemany(
                "INSERT OR IGNORE INTO ai_model_retrain_request_events\n                   (request_id, drift_event_id, linked_at) VALUES (?, ?, ?)",
                [
                    (request_id, int(event_row[0]), updated_at)
                    for event_row in events_to_link
                ],
            )
        return (
            None
            if False
            else {
                "request_id": request_id,
                "status": status,
                "severity": severity,
                "first_event_date": first_event_date,
                "last_event_date": last_event_date,
                "confirmation_days": confirmation_days,
                "bound_candidate_id": (
                    str(bound_candidate_id) if bound_candidate_id else None
                ),
                "superseded_candidate_id": (
                    str(superseded_candidate_id) if superseded_candidate_id else None
                ),
            }
        )

    async def get_open_model_retrain_request(self) -> Optional[Dict[str, Any]]:
        """Return the oldest restart-safe model reaction awaiting completion."""
        row = await self.fetchone(
            "SELECT request_id, status, severity, first_event_date,\n                      last_event_date, confirmation_days, bound_candidate_id,\n                      superseded_candidate_id\n                 FROM ai_model_retrain_requests\n                WHERE scope = 'global'\n                  AND drift_type = 'mae_above_baseline'\n                  AND status IN ('pending', 'bound')\n                ORDER BY created_at LIMIT 1"
        )
        return (
            None
            if not row
            else {
                "request_id": str(row[0]),
                "status": str(row[1]),
                "severity": str(row[2]),
                "first_event_date": str(row[3]),
                "last_event_date": str(row[4]),
                "confirmation_days": int(row[5]),
                "bound_candidate_id": str(row[6]) if row[6] else None,
                "superseded_candidate_id": str(row[7]) if row[7] else None,
            }
        )

    async def model_candidate_covers_retrain_request(
        self, request_id: str, candidate_id: str, minimum_samples_per_day: int = 4
    ) -> bool:
        """Verify that every confirmed drift day is present in candidate provenance."""
        request_event_dates = {
            str(event_date_row[0])
            for event_date_row in await self.fetchall(
                "SELECT event.event_date\n                     FROM ai_model_retrain_request_events link\n                     JOIN drift_events event ON event.id = link.drift_event_id\n                    WHERE link.request_id = ?",
                (request_id,),
            )
        }
        if not request_event_dates:
            return False
        for target_date_value in request_event_dates:
            provenance_count_row = await self.fetchone(
                "SELECT COUNT(DISTINCT provenance.sample_index)\n                     FROM ai_model_candidates candidate\n                     JOIN ai_training_sample_provenance provenance\n                       ON provenance.run_id = candidate.run_id\n                    WHERE candidate.candidate_id = ?\n                      AND substr(provenance.target_anchor, 1, 10) = ?",
                (candidate_id, target_date_value),
            )
            if (
                not provenance_count_row
                or int(provenance_count_row[0] or 0) < minimum_samples_per_day
            ):
                return False
        return True

    async def get_model_candidate_latest_target_date(
        self, candidate_id: str
    ) -> Optional[str]:
        """Return the newest target date included in candidate training."""
        latest_date_row = await self.fetchone(
            "SELECT MAX(substr(provenance.target_anchor, 1, 10))\n                 FROM ai_model_candidates candidate\n                 JOIN ai_training_sample_provenance provenance\n                   ON provenance.run_id = candidate.run_id\n                WHERE candidate.candidate_id = ?",
            (candidate_id,),
        )
        return (
            str(latest_date_row[0]) if latest_date_row and latest_date_row[0] else None
        )

    async def bind_model_retrain_request(
        self, request_id: str, candidate_id: str
    ) -> None:
        """Bind a confirmed request to a candidate that already contains its data."""
        timestamp_iso = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            row = await self.fetchone(
                "SELECT status FROM ai_model_retrain_requests\n                     WHERE request_id = ?",
                (request_id,),
            )
            row_2 = await self.fetchone(
                "SELECT 1 FROM ai_model_candidates candidate\n                     WHERE candidate.candidate_id = ?\n                       AND candidate.status = 'candidate'\n                       AND NOT EXISTS (\n                           SELECT 1 FROM ai_model_candidate_dispositions disposition\n                            WHERE disposition.candidate_id = candidate.candidate_id\n                       )\n                       AND NOT EXISTS (\n                           SELECT 1 FROM ai_model_evaluations evaluation\n                            WHERE evaluation.candidate_id = candidate.candidate_id\n                       )\n                       AND NOT EXISTS (\n                           SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                            WHERE evaluation.candidate_id = candidate.candidate_id\n                       )",
                (candidate_id,),
            )
            if not row or row[0] not in {"bound", "pending"}:
                raise ValueError("model retrain request is unavailable")
            if row[0] == "bound":
                row_3 = await self.fetchone(
                    "SELECT bound_candidate_id FROM ai_model_retrain_requests\n                         WHERE request_id = ?",
                    (request_id,),
                )
                if row_3 and row_3[0] == candidate_id:
                    return
                raise ValueError("model retrain request is already bound")
            if not row_2:
                raise ValueError("model candidate is no longer pending")
            if not await self.model_candidate_covers_retrain_request(
                request_id, candidate_id
            ):
                raise ValueError("model candidate does not cover drift evidence")
            await self.execute(
                "UPDATE ai_model_retrain_requests\n                      SET status = 'bound', bound_candidate_id = ?, updated_at = ?\n                    WHERE request_id = ? AND status = 'pending'",
                (candidate_id, timestamp_iso, request_id),
                auto_commit=False,
            )
            await self.execute(
                "UPDATE drift_events SET response_executed = TRUE\n                     WHERE id IN (\n                         SELECT drift_event_id\n                           FROM ai_model_retrain_request_events\n                          WHERE request_id = ?\n                     )",
                (request_id,),
                auto_commit=False,
            )
        return None if True else None

    async def complete_model_retrain_requests(
        self, candidate_id: str, decision: str
    ) -> None:
        """Complete every open request bound to an evaluated candidate."""
        if decision not in {"GO", "NO_GO"}:
            raise ValueError("invalid model reaction completion decision")
        timestamp_iso = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            await self.execute(
                "UPDATE ai_model_retrain_requests\n                  SET status = 'completed', completion_decision = ?,\n                      completed_at = ?, updated_at = ?\n                WHERE status = 'bound' AND bound_candidate_id = ?",
                (decision, timestamp_iso, timestamp_iso, candidate_id),
                auto_commit=False,
            )
        return None if True else None

    async def get_retryable_model_candidate_promotion(self) -> Optional[Dict[str, str]]:
        """Return a recorded GO that still targets the current serving champion."""
        artifact_hash = await self.get_active_model_artifact_hash()
        if not artifact_hash:
            return
        row = await self.fetchone(
            "SELECT evaluation.candidate_id, evaluation.evaluation_id,\n                      evaluation.decision\n                 FROM ai_model_evaluations_v2 evaluation\n                 JOIN ai_model_candidates candidate\n                   ON candidate.candidate_id = evaluation.candidate_id\n                 LEFT JOIN ai_active_model_pointer pointer ON pointer.id = 1\n                WHERE evaluation.decision = 'GO'\n                  AND evaluation.champion_artifact_hash = ?\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluation_invalidations invalidation\n                       WHERE invalidation.evaluation_id = evaluation.evaluation_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1\n                        FROM ai_model_evaluation_samples_v2 sample\n                        JOIN ai_model_shadow_actual_versions actual\n                          ON actual.actual_version_id = sample.actual_version_id\n                       WHERE sample.evaluation_id = evaluation.evaluation_id\n                         AND (\n                             actual.clean_eligible != 1\n                             OR actual.evaluation_version != (\n                                 SELECT MAX(latest.evaluation_version)\n                                   FROM ai_model_shadow_actual_versions latest\n                                  WHERE latest.shadow_prediction_id = actual.shadow_prediction_id\n                             )\n                         )\n                  )\n                  AND candidate.status = 'candidate'\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_dispositions disposition\n                       WHERE disposition.candidate_id = candidate.candidate_id\n                         AND NOT EXISTS (\n                             SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                              WHERE reconsideration.candidate_id = disposition.candidate_id\n                                AND disposition.disposition = 'migration_superseded'\n                                AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                         )\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                       WHERE closure.candidate_id = candidate.candidate_id\n                  )\n                  AND (pointer.active_candidate_id IS NULL\n                       OR pointer.active_candidate_id != evaluation.candidate_id)\n                ORDER BY evaluation.created_at, evaluation.evaluation_id\n                LIMIT 1",
            (artifact_hash,),
        )
        if not row:
            return
        return {
            "candidate_id": str(row[0]),
            "evaluation_id": str(row[1]),
            "decision": str(row[2]),
        }

    async def get_model_candidate_weights(self, candidate_id: str) -> Dict[str, Any]:
        """Load and verify one non-serving candidate artifact set."""
        candidate_row = await self.fetchone(
            "SELECT active_model, training_samples, accuracy, rmse,\n                      artifact_hash, created_at\n               FROM ai_model_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        if not candidate_row:
            raise ValueError("model candidate is unavailable")
        artifact_rows = await self.fetchall(
            "SELECT model_type, weights_json, weights_hash\n               FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
            (candidate_id,),
        )
        model_artifacts = {}
        for artifact_row in artifact_rows:
            artifact_value = loads_protected_json(
                artifact_row[1],
                "ai_model_candidate_artifacts",
                protected_identity(candidate_id, artifact_row[0]),
                "weights",
            )
            if canonical_stable_hash(artifact_value) != artifact_row[2]:
                raise ValueError("candidate artifact hash mismatch")
            model_artifacts[artifact_row[0]] = artifact_value
        if (
            not model_artifacts
            or canonical_stable_hash(model_weight_artifacts(model_artifacts))
            != candidate_row[4]
        ):
            raise ValueError("candidate artifact set is incomplete or mismatched")
        return {
            "version": "3.0",
            "candidate_id": candidate_id,
            "artifact_hash": candidate_row[4],
            "active_model": candidate_row[0],
            "training_samples": candidate_row[1],
            "accuracy": candidate_row[2],
            "rmse": candidate_row[3],
            "last_trained": candidate_row[5],
            **model_artifacts,
        }

    async def get_active_model_artifact_hash(self) -> Optional[str]:
        """Return the verified serving model hash, including the legacy fallback."""
        active_pointer_row = await self.fetchone(
            "SELECT active_candidate_id FROM ai_active_model_pointer WHERE id = 1"
        )
        if active_pointer_row and active_pointer_row[0]:
            candidate_row = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (active_pointer_row[0],),
            )
            artifact_rows = await self.fetchall(
                "SELECT model_type, weights_json, weights_hash\n                   FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
                (active_pointer_row[0],),
            )
            model_artifacts = {}
            for artifact_row in artifact_rows:
                artifact_value = loads_protected_json(
                    artifact_row[1],
                    "ai_model_candidate_artifacts",
                    protected_identity(active_pointer_row[0], artifact_row[0]),
                    "weights",
                )
                if canonical_stable_hash(artifact_value) != artifact_row[2]:
                    raise ValueError("active candidate artifact hash mismatch")
                model_artifacts[artifact_row[0]] = artifact_value
            if (
                not candidate_row
                or not model_artifacts
                or canonical_stable_hash(model_weight_artifacts(model_artifacts))
                != candidate_row[0]
            ):
                raise ValueError("active candidate artifact set hash mismatch")
            return candidate_row[0]
        active_weights = await self.get_model_weights()
        if not active_weights:
            return MODEL_BOOTSTRAP_CHAMPION_ARTIFACT_HASH
        legacy_artifacts = {
            artifact_type: active_weights[artifact_type]
            for artifact_type in ("ridge", "lstm")
            if active_weights.get(artifact_type) is not None
        }
        if not legacy_artifacts:
            return MODEL_BOOTSTRAP_CHAMPION_ARTIFACT_HASH
        return canonical_stable_hash(legacy_artifacts)

    async def get_active_model_trained_on(self) -> Optional[date]:
        """Return the serving model's training date, or None for the bootstrap champion."""
        row = await self.fetchone(
            "SELECT active_candidate_id FROM ai_active_model_pointer WHERE id = 1"
        )
        if row and row[0]:
            row_3 = await self.fetchone(
                "SELECT created_at FROM ai_model_candidates WHERE candidate_id = ?",
                (row[0],),
            )
            return self._calendar_date(row_3[0]) if row_3 else None
        if (
            await self.get_active_model_artifact_hash()
            == MODEL_BOOTSTRAP_CHAMPION_ARTIFACT_HASH
        ):
            return
        row_2 = await self.fetchone(
            "SELECT last_trained, updated_at FROM ai_learned_weights_meta WHERE id = 1"
        )
        if not row_2:
            return
        return self._calendar_date(row_2[0]) or self._calendar_date(row_2[1])

    @staticmethod
    def _calendar_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value is None:
            return
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return

    async def record_model_candidate_evaluation(
        self,
        candidate_id: str,
        champion_artifact_hash: str,
        evaluation_config: Mapping[str, Any],
        samples: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any],
    ) -> str:
        """Append a hash-bound paired evaluation without changing serving state."""
        frozen_samples = _normalize_frozen_evaluation_samples(samples)
        if not _is_sha256(champion_artifact_hash):
            raise ValueError("invalid champion artifact hash")
        if not isinstance(evaluation_config, Mapping):
            raise ValueError("invalid evaluation configuration")
        if not isinstance(decision, Mapping):
            raise ValueError("invalid evaluation decision")
        dataset_fingerprint = canonical_stable_hash(frozen_samples)
        evaluation_config_hash = canonical_stable_hash(dict(evaluation_config))
        required_decision_fields = {
            "go_no_go",
            "reason_codes",
            "overall_hourly",
            "daily_energy_mae",
            "dataset_fingerprint",
            "eligible_hour_count",
            "critical_slice_results",
            "evaluation_config_hash",
            "per_day_paired_mae_deltas",
            "eligible_independent_day_count",
            "candidate_minus_champion_mae_ci_95",
        }
        if set(decision) != required_decision_fields:
            raise ValueError("incomplete evaluation decision")
        reason_codes = decision["reason_codes"]
        if (
            decision["go_no_go"] not in {"GO", "NO_GO"}
            or not isinstance(reason_codes, (list, tuple))
            or (
                not all(
                    (
                        isinstance(reason_code, str) and reason_code
                        for reason_code in reason_codes
                    )
                )
            )
            or (decision["go_no_go"] == "GO" and reason_codes)
            or (decision["go_no_go"] == "NO_GO" and (not reason_codes))
        ):
            raise ValueError("inconsistent evaluation decision")
        eligible_samples = [
            sample for sample in frozen_samples if sample["clean_eligible"]
        ]
        eligible_dates = {sample["target_date"] for sample in eligible_samples}
        if (
            decision["dataset_fingerprint"] != dataset_fingerprint
            or decision["evaluation_config_hash"] != evaluation_config_hash
            or decision["eligible_hour_count"] != len(eligible_samples)
            or (decision["eligible_independent_day_count"] != len(eligible_dates))
        ):
            raise ValueError("evaluation evidence hash or counts mismatch")
        recomputed_decision = _recompute_model_promotion_decision(
            evaluation_config, frozen_samples
        )
        if canonical_stable_hash(recomputed_decision) != canonical_stable_hash(
            dict(decision)
        ):
            raise ValueError("evaluation decision does not match frozen evidence")
        decision_payload = dict(decision)
        decision_hash = canonical_stable_hash(decision_payload)
        evaluation_config_hash = canonical_stable_hash(dict(evaluation_config))
        evaluation_id = str(uuid.uuid4())
        sealed_decision = seal_protected_text(
            canonical_stable_json(decision_payload),
            "ai_model_evaluations",
            protected_identity(evaluation_id, "decision_json"),
        )
        sealed_evaluation_config = seal_protected_text(
            canonical_stable_json(dict(evaluation_config)),
            "ai_model_evaluations",
            protected_identity(evaluation_id, "evaluation_config_json"),
        )
        sealed_reason_codes = seal_protected_text(
            canonical_stable_json(list(reason_codes)),
            "ai_model_evaluations",
            protected_identity(evaluation_id, "reason_codes_json"),
        )
        created_at = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            candidate_row = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not candidate_row:
                raise ValueError("candidate is unavailable for evaluation")
            current_champion_hash = await self.get_active_model_artifact_hash()
            if current_champion_hash != champion_artifact_hash:
                raise ValueError(
                    "champion artifact changed before evaluation was recorded"
                )
            await self.execute(
                "INSERT INTO ai_model_evaluations\n                   (evaluation_id, candidate_id, created_at, candidate_artifact_hash,\n                    champion_artifact_hash, dataset_fingerprint,\n                    evaluation_config_hash, decision, reason_codes_json,\n                    eligible_hour_count, eligible_independent_day_count,\n                    evaluation_config_json, decision_json, decision_hash)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    candidate_id,
                    created_at,
                    candidate_row[0],
                    champion_artifact_hash,
                    dataset_fingerprint,
                    evaluation_config_hash,
                    decision["go_no_go"],
                    sealed_reason_codes,
                    len(eligible_samples),
                    len(eligible_dates),
                    sealed_evaluation_config,
                    sealed_decision,
                    decision_hash,
                ),
                auto_commit=False,
            )
            for sample_index, sample in enumerate(frozen_samples):
                await self.execute(
                    "INSERT INTO ai_model_evaluation_samples\n                       (evaluation_id, sample_index, target_date, target_hour,\n                        actual, champion_prediction, candidate_prediction,\n                        clean_eligible, forecast_anchor, input_fingerprint,\n                        policy_fingerprint, weather_regime, horizon)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evaluation_id,
                        sample_index,
                        sample["target_date"],
                        sample["target_hour"],
                        sample["actual"],
                        sample["champion_prediction"],
                        sample["candidate_prediction"],
                        int(sample["clean_eligible"]),
                        sample["forecast_anchor"],
                        sample["input_fingerprint"],
                        sample["policy_fingerprint"],
                        sample["weather_regime"],
                        sample["horizon"],
                    ),
                    auto_commit=False,
                )
        if True:
            return evaluation_id

    async def record_model_candidate_evaluation_v2(
        self,
        candidate_id: str,
        champion_artifact_hash: str,
        evaluation_config: Mapping[str, Any],
        samples: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any],
    ) -> str:
        """Append a hash-bound V2 cycle-aware evaluation without serving changes."""
        cycle_samples = _normalize_cycle_evaluation_samples(samples)
        if not _is_sha256(champion_artifact_hash):
            raise ValueError("invalid champion artifact hash")
        if not (
            isinstance(evaluation_config, Mapping) and isinstance(decision, Mapping)
        ):
            raise ValueError("invalid cycle evaluation payload")
        required_decision_fields = {
            "go_no_go",
            "reason_codes",
            "daily_energy_mae",
            "champion_age_days",
            "comparison_outcome",
            "dataset_fingerprint",
            "critical_slice_results",
            "evaluation_config_hash",
            "non_inferiority_enabled",
            "cycle_noise_band_results",
            "cycle_mean_daily_energy_deltas",
            "eligible_independent_day_count",
            "per_day_paired_daily_energy_deltas",
            "allowed_total_daily_energy_regression",
            "eligible_common_production_hour_count",
            "candidate_minus_champion_daily_energy_mae",
            "candidate_minus_champion_daily_energy_mae_ci_95",
        }
        if set(decision) != required_decision_fields:
            raise ValueError("incomplete cycle evaluation decision")
        reason_codes = decision["reason_codes"]
        if (
            decision["go_no_go"] not in {"GO", "NO_GO"}
            or not isinstance(reason_codes, (list, tuple))
            or (
                not all(
                    (
                        isinstance(reason_code, str) and reason_code
                        for reason_code in reason_codes
                    )
                )
            )
            or (decision["go_no_go"] == "GO" and reason_codes)
            or (decision["go_no_go"] == "NO_GO" and (not reason_codes))
        ):
            raise ValueError("inconsistent cycle evaluation decision")
        weather_regimes = {sample["weather_regime"] for sample in cycle_samples}
        if (
            any((sample["horizon"] != "day_0" for sample in cycle_samples))
            or not weather_regimes
            or (not weather_regimes <= MODEL_PROMOTION_WEATHER_REGIMES)
        ):
            raise ValueError(
                "cycle evaluation requires recognized day-zero weather regimes"
            )
        required_policy_config = build_cycle_aware_promotion_policy_config_v3(
            cycle_samples, champion_trained_on=await self.get_active_model_trained_on()
        )
        if canonical_stable_hash(dict(evaluation_config)) != canonical_stable_hash(
            required_policy_config
        ):
            raise ValueError("cycle evaluation configuration violates promotion policy")
        recomputed_decision = _recompute_cycle_aware_model_promotion_decision(
            evaluation_config, cycle_samples
        )
        if canonical_stable_hash(recomputed_decision) != canonical_stable_hash(
            dict(decision)
        ):
            raise ValueError("cycle evaluation decision does not match frozen evidence")
        if decision["dataset_fingerprint"] != recomputed_decision[
            "dataset_fingerprint"
        ] or decision["evaluation_config_hash"] != canonical_stable_hash(
            dict(evaluation_config)
        ):
            raise ValueError("cycle evaluation evidence hash or configuration mismatch")
        dataset_fingerprint = canonical_stable_hash(cycle_samples)
        evaluation_id = str(uuid.uuid4())
        created_at = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            existing_evaluation_row = await self.fetchone(
                "SELECT evaluation_id, candidate_artifact_hash,\n                          champion_artifact_hash, dataset_fingerprint,\n                          evaluation_config_hash, decision_hash\n                     FROM ai_model_evaluations_v2\n                    WHERE candidate_id = ?",
                (candidate_id,),
            )
            if existing_evaluation_row:
                candidate_hash_row = await self.fetchone(
                    "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )
                expected_evidence_stamp = (
                    candidate_hash_row[0] if candidate_hash_row else None,
                    champion_artifact_hash,
                    dataset_fingerprint,
                    canonical_stable_hash(dict(evaluation_config)),
                    canonical_stable_hash(dict(decision)),
                )
                if tuple(existing_evaluation_row[1:]) == expected_evidence_stamp:
                    if True:
                        return str(existing_evaluation_row[0])
                raise ValueError(
                    "candidate already has a different terminal evaluation"
                )
            candidate_row = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not candidate_row:
                raise ValueError("candidate is unavailable for evaluation")
            if await self.get_active_model_artifact_hash() != champion_artifact_hash:
                raise ValueError(
                    "champion artifact changed before evaluation was recorded"
                )
            await self._validate_cycle_evaluation_samples_provenance(
                candidate_id, candidate_row[0], champion_artifact_hash, cycle_samples
            )
            evaluation_config_json = canonical_stable_json(dict(evaluation_config))
            decision_json = canonical_stable_json(dict(decision))
            reason_codes_json = canonical_stable_json(list(reason_codes))
            await self.execute(
                "INSERT INTO ai_model_evaluations_v2 (\n                       evaluation_id, candidate_id, created_at, candidate_artifact_hash,\n                       champion_artifact_hash, dataset_fingerprint, evaluation_config_hash,\n                       decision, reason_codes_json, eligible_hour_count,\n                       eligible_independent_day_count, evaluation_config_json,\n                       decision_json, decision_hash, evaluator_contract_version\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    candidate_id,
                    created_at,
                    candidate_row[0],
                    champion_artifact_hash,
                    dataset_fingerprint,
                    canonical_stable_hash(dict(evaluation_config)),
                    decision["go_no_go"],
                    seal_protected_text(
                        reason_codes_json,
                        "ai_model_evaluations_v2",
                        protected_identity(evaluation_id, "reason_codes_json"),
                    ),
                    decision["eligible_common_production_hour_count"],
                    decision["eligible_independent_day_count"],
                    seal_protected_text(
                        evaluation_config_json,
                        "ai_model_evaluations_v2",
                        protected_identity(evaluation_id, "evaluation_config_json"),
                    ),
                    seal_protected_text(
                        decision_json,
                        "ai_model_evaluations_v2",
                        protected_identity(evaluation_id, "decision_json"),
                    ),
                    canonical_stable_hash(dict(decision)),
                    "cycle_aware_model_promotion_v2",
                ),
                auto_commit=False,
            )
            for sample_index, sample in enumerate(cycle_samples):
                await self.execute(
                    "INSERT INTO ai_model_evaluation_samples_v2 (\n                           evaluation_id, sample_index, target_date, target_hour,\n                           forecast_cycle, shadow_run_id, shadow_prediction_id,\n                           actual_version_id, actual, champion_prediction,\n                           candidate_prediction, clean_eligible, forecast_anchor,\n                           input_fingerprint, policy_fingerprint, weather_regime, horizon\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evaluation_id,
                        sample_index,
                        sample["target_date"],
                        sample["target_hour"],
                        sample["forecast_cycle"],
                        sample["shadow_run_id"],
                        sample["shadow_prediction_id"],
                        sample["actual_version_id"],
                        sample["actual"],
                        sample["champion_prediction"],
                        sample["candidate_prediction"],
                        int(sample["clean_eligible"]),
                        sample["forecast_anchor"],
                        sample["input_fingerprint"],
                        sample["policy_fingerprint"],
                        sample["weather_regime"],
                        sample["horizon"],
                    ),
                    auto_commit=False,
                )
            if decision["go_no_go"] == "NO_GO":
                completed_at = SafeDateTimeUtil.now().isoformat()
                await self._record_model_candidate_terminal_event(
                    candidate_id,
                    "no_go",
                    {
                        "candidate_id": candidate_id,
                        "evaluation_id": evaluation_id,
                        "decision_hash": canonical_stable_hash(dict(decision)),
                    },
                )
                await self.execute(
                    "UPDATE ai_model_retrain_requests\n                          SET status = 'completed', completion_decision = 'NO_GO',\n                              completed_at = ?, updated_at = ?\n                        WHERE status = 'bound' AND bound_candidate_id = ?",
                    (completed_at, completed_at, candidate_id),
                    auto_commit=False,
                )
        if True:
            return evaluation_id

    _PROVENANCE_QUERY_CHUNK = 250

    async def _validate_cycle_evaluation_samples_provenance(
        self,
        candidate_id: str,
        candidate_artifact_hash: str,
        champion_artifact_hash: str,
        samples: Sequence[Mapping[str, Any]],
        *,
        canonical_runs: bool = True,
    ) -> None:
        """Require every frozen sample to belong to one current eligible shadow row.

        Set-based on purpose: the proof runs at startup and in the morning
        cycles, and hundreds of small awaits against a busy event loop cost
        seconds regardless of how cheap each query is. One relational query
        per chunk of samples, one query for the hour payloads and one thread
        call for the run snapshots replace the former per-sample round trips.
        The checks and their error messages are unchanged.
        """
        if not samples:
            return
        provenance_id_triples = [
            (
                str(sample["shadow_run_id"]),
                str(sample["shadow_prediction_id"]),
                str(sample["actual_version_id"]),
            )
            for sample in samples
        ]
        provenance_rows_by_ids = {}
        for chunk_start in range(
            0, len(provenance_id_triples), self._PROVENANCE_QUERY_CHUNK
        ):
            provenance_chunk = provenance_id_triples[
                chunk_start : chunk_start + self._PROVENANCE_QUERY_CHUNK
            ]
            value_placeholders = ", ".join(
                ("(?, ?, ?)" for placeholder_row in provenance_chunk)
            )
            provenance_rows = await self.fetchall(
                f"WITH wanted(shadow_run_id, shadow_prediction_id, actual_version_id) AS (\n                       VALUES {value_placeholders}\n                   ),\n                   ranked_runs AS (\n                       SELECT candidate_run.shadow_run_id,\n                              ROW_NUMBER() OVER (\n                                  PARTITION BY candidate_run.run_date,\n                                               candidate_provenance.forecast_cycle\n                                  ORDER BY COALESCE(candidate_batch.completed_at,\n                                                    candidate_run.created_at),\n                                           candidate_run.created_at,\n                                           candidate_run.shadow_run_id\n                              ) AS cycle_rank\n                         FROM ai_model_shadow_runs candidate_run\n                         JOIN ensemble_shadow_batches candidate_batch\n                           ON candidate_batch.morning_batch_id =\n                              candidate_run.morning_batch_id\n                         JOIN ai_model_shadow_run_provenance candidate_provenance\n                           ON candidate_provenance.shadow_run_id =\n                              candidate_run.shadow_run_id\n                        WHERE candidate_batch.status = 'completed'\n                          AND candidate_run.contract_version =\n                              'model_candidate_shadow_v2'\n                          AND candidate_provenance.contract_version =\n                              'model_candidate_shadow_v2'\n                          AND candidate_provenance.promotion_eligible = 1\n                          AND candidate_provenance.trigger_kind = 'scheduled'\n                          AND candidate_provenance.forecast_cycle IN\n                              ('post_midnight', 'pre_sunrise')\n                          AND candidate_run.candidate_id = ?\n                          AND candidate_run.champion_artifact_hash = ?\n                          AND candidate_run.candidate_artifact_hash = ?\n                   )\n                   SELECT run.candidate_id, run.champion_artifact_hash,\n                          run.candidate_artifact_hash, run.forecast_anchor,\n                          run.contract_version, batch.status,\n                          provenance.forecast_cycle, provenance.trigger_kind,\n                          provenance.promotion_eligible, provenance.contract_version,\n                          prediction.target_date, prediction.target_hour,\n                          prediction.champion_prediction, prediction.candidate_prediction,\n                          prediction.input_fingerprint, prediction.policy_fingerprint,\n                          prediction.weather_regime, prediction.horizon,\n                          actual.actual_kwh, actual.clean_eligible,\n                          actual.evaluation_version = (\n                              SELECT MAX(latest.evaluation_version)\n                                FROM ai_model_shadow_actual_versions latest\n                               WHERE latest.shadow_prediction_id = actual.shadow_prediction_id\n                          ),\n                          wanted.shadow_run_id, wanted.shadow_prediction_id,\n                          wanted.actual_version_id\n                     FROM wanted\n                     JOIN ai_model_shadow_runs run\n                       ON run.shadow_run_id = wanted.shadow_run_id\n                     JOIN ranked_runs ranked\n                       ON ranked.shadow_run_id = run.shadow_run_id\n                     JOIN ensemble_shadow_batches batch\n                       ON batch.morning_batch_id = run.morning_batch_id\n                     JOIN ai_model_shadow_run_provenance provenance\n                       ON provenance.shadow_run_id = run.shadow_run_id\n                     JOIN ai_model_shadow_predictions prediction\n                       ON prediction.shadow_run_id = run.shadow_run_id\n                      AND prediction.shadow_prediction_id = wanted.shadow_prediction_id\n                     JOIN ai_model_shadow_actual_versions actual\n                       ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                      AND actual.actual_version_id = wanted.actual_version_id\n                    WHERE ranked.cycle_rank = 1",
                (
                    *[
                        identifier
                        for provenance_ids in provenance_chunk
                        for identifier in provenance_ids
                    ],
                    candidate_id,
                    champion_artifact_hash,
                    candidate_artifact_hash,
                ),
            )
            for provenance_row in provenance_rows:
                provenance_rows_by_ids[
                    str(provenance_row[21]),
                    str(provenance_row[22]),
                    str(provenance_row[23]),
                ] = provenance_row
        for sample, provenance_ids in zip(samples, provenance_id_triples):
            self._check_cycle_evaluation_sample_row(
                candidate_id,
                candidate_artifact_hash,
                champion_artifact_hash,
                sample,
                provenance_rows_by_ids.get(provenance_ids),
            )
        await self._validate_shadow_runs_raw_payloads(
            sorted({provenance_ids[0] for provenance_ids in provenance_id_triples}),
            canonical=canonical_runs,
        )
        await self._validate_shadow_result_payloads(samples)

    @staticmethod
    def _check_cycle_evaluation_sample_row(
        candidate_id: str,
        candidate_artifact_hash: str,
        champion_artifact_hash: str,
        sample: Mapping[str, Any],
        row: Any,
    ) -> None:
        expected_identity_fields = (
            candidate_id,
            champion_artifact_hash,
            candidate_artifact_hash,
            sample["forecast_anchor"],
            "model_candidate_shadow_v2",
            "completed",
            sample["forecast_cycle"],
            "scheduled",
            1,
            "model_candidate_shadow_v2",
            sample["target_date"],
            sample["target_hour"],
        )
        if not row or tuple(row[:12]) != expected_identity_fields:
            raise ValueError(
                "frozen cycle evaluation provenance is not relationally bound"
            )
        value_pairs = (
            (row[12], sample["champion_prediction"]),
            (row[13], sample["candidate_prediction"]),
            (row[18], sample["actual"]),
        )
        if any(
            (
                not math.isclose(
                    float(stored_payload),
                    float(actual_value),
                    rel_tol=0.0,
                    abs_tol=1e-09,
                )
                for stored_payload, actual_value in value_pairs
            )
        ):
            raise ValueError("frozen cycle evaluation values do not match provenance")
        if (
            row[14] != sample["input_fingerprint"]
            or row[15] != sample["policy_fingerprint"]
            or row[16] != sample["weather_regime"]
            or (row[17] != sample["horizon"])
            or (not bool(row[19]))
            or (not bool(row[20]))
            or (not sample["clean_eligible"])
            or (not sample["is_production_hour"])
        ):
            raise ValueError("frozen cycle evaluation evidence is stale or mismatched")

    async def _validate_shadow_runs_raw_payloads(
        self, shadow_run_ids: Sequence[str], *, canonical: bool = True
    ) -> None:
        """Require hash-valid raw run snapshots (input and policy) for every run.

        The policy snapshot is megabytes of JSON and shared by every hour of
        the run, so a pass proves each run once. Canonical form is enforced
        when the run is written and proven again when an evaluation is
        recorded and when it is promoted; a later load of the promoted
        champion only needs the bytes to be the hashed ones
        (``canonical=False``), which costs zlib + SHA-256 instead of JSON.
        """
        if not shadow_run_ids:
            return
        rows = await self.fetchall(
            f"SELECT input_snapshot_json, input_snapshot_blob, input_snapshot_hash,\n                       policy_snapshot_json, policy_snapshot_blob, policy_snapshot_hash,\n                       snapshot_codec, payload_state, shadow_run_id\n                  FROM ai_model_shadow_runs\n                 WHERE shadow_run_id IN ({', '.join(('?' for unused_item in shadow_run_ids))})",
            tuple(shadow_run_ids),
        )
        evidence_rows_by_id = {str(row[8]): tuple(row) for row in rows}
        if any(
            (
                evidence_rows_by_id.get(run_id) is None
                or evidence_rows_by_id[run_id][7] != "hot"
                for run_id in shadow_run_ids
            )
        ):
            raise ValueError("promotion raw evidence is unavailable")
        payload_verifier = (
            _decode_canonical_shadow_run_snapshots
            if canonical
            else _verify_shadow_run_snapshot_hashes
        )

        def prove_all() -> None:
            for run_id in shadow_run_ids:
                payload_verifier(evidence_rows_by_id[run_id])

        await asyncio.to_thread(prove_all)

    async def _validate_shadow_result_payloads(
        self, samples: Sequence[Mapping[str, Any]]
    ) -> None:
        """Require canonical, hash-valid hour result payloads matching the frozen predictions."""
        if not samples:
            return
        prediction_ids = sorted(
            {str(sample["shadow_prediction_id"]) for sample in samples}
        )
        result_rows_by_ids = {}
        for chunk_start in range(0, len(prediction_ids), self._PROVENANCE_QUERY_CHUNK):
            prediction_id_chunk = prediction_ids[
                chunk_start : chunk_start + self._PROVENANCE_QUERY_CHUNK
            ]
            result_rows = await self.fetchall(
                f"SELECT champion_result_json, champion_result_blob, champion_result_hash,\n                           candidate_result_json, candidate_result_blob,\n                           candidate_result_hash, result_codec, payload_state,\n                           shadow_run_id, shadow_prediction_id\n                      FROM ai_model_shadow_predictions\n                     WHERE shadow_prediction_id IN ({', '.join(('?' for placeholder_id in prediction_id_chunk))})",
                tuple(prediction_id_chunk),
            )
            for result_row in result_rows:
                result_rows_by_ids[str(result_row[8]), str(result_row[9])] = result_row
        for sample in samples:
            result_row = result_rows_by_ids.get(
                (str(sample["shadow_run_id"]), str(sample["shadow_prediction_id"]))
            )
            if not result_row or result_row[7] != "hot":
                raise ValueError("promotion raw evidence is unavailable")
            _check_shadow_result_payloads(
                result_row,
                float(sample["champion_prediction"]),
                float(sample["candidate_prediction"]),
            )

    async def invalidate_stale_model_candidate_evaluations(self) -> int:
        """Terminalize recorded decisions whose referenced actual version is obsolete."""
        async with self.transaction():
            row = await self.fetchone(
                "SELECT COUNT(*) FROM ai_model_evaluation_invalidations"
            )
            timestamp_iso = SafeDateTimeUtil.now().isoformat()
            await self.execute(
                "INSERT INTO ai_model_evaluation_invalidations\n                       (evaluation_id, invalidation_reason, invalidated_at)\n                   SELECT evaluation.evaluation_id,\n                          'actual_evidence_superseded', ?\n                     FROM ai_model_evaluations_v2 evaluation\n                    WHERE NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_invalidations existing\n                               WHERE existing.evaluation_id = evaluation.evaluation_id\n                          )\n                      AND EXISTS (\n                              SELECT 1\n                                FROM ai_model_evaluation_samples_v2 sample\n                                JOIN ai_model_shadow_actual_versions actual\n                                  ON actual.actual_version_id = sample.actual_version_id\n                               WHERE sample.evaluation_id = evaluation.evaluation_id\n                                 AND (\n                                     actual.clean_eligible != 1\n                                     OR actual.evaluation_version != (\n                                         SELECT MAX(latest.evaluation_version)\n                                           FROM ai_model_shadow_actual_versions latest\n                                          WHERE latest.shadow_prediction_id = actual.shadow_prediction_id\n                                     )\n                                 )\n                          )",
                (timestamp_iso,),
                auto_commit=False,
            )
            await self.execute(
                "UPDATE ai_model_retrain_requests AS request\n                      SET status = 'completed', completion_decision = 'NO_GO',\n                          completed_at = ?, updated_at = ?\n                    WHERE request.status = 'bound'\n                      AND EXISTS (\n                          SELECT 1\n                            FROM ai_model_evaluations_v2 evaluation\n                            JOIN ai_model_evaluation_invalidations invalidation\n                              ON invalidation.evaluation_id = evaluation.evaluation_id\n                           WHERE evaluation.candidate_id = request.bound_candidate_id\n                      )",
                (timestamp_iso, timestamp_iso),
                auto_commit=False,
            )
            row_2 = await self.fetchone(
                "SELECT COUNT(*) FROM ai_model_evaluation_invalidations"
            )
        return int(row_2[0] or 0) - int(row[0] or 0)

    async def promote_model_candidate(
        self, candidate_id: str, evaluation_id: str
    ) -> None:
        """Promote only a candidate with a current, intact paired GO decision."""
        async with self.transaction():
            candidate_row = await self.fetchone(
                "SELECT artifact_hash, status FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not candidate_row or candidate_row[1] != "candidate":
                raise ValueError("candidate is unavailable for promotion")
            disposition_row = await self.fetchone(
                "SELECT disposition FROM ai_model_candidate_dispositions disposition\n                     WHERE candidate_id = ? AND NOT EXISTS (\n                         SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                          WHERE reconsideration.candidate_id = disposition.candidate_id\n                            AND disposition.disposition = 'migration_superseded'\n                            AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                            AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                     )",
                (candidate_id,),
            )
            if disposition_row:
                raise ValueError("candidate was dispositioned before promotion")
            if await self.fetchone(
                "SELECT 1 FROM ai_model_candidate_reconsideration_closures\n                    WHERE candidate_id = ?",
                (candidate_id,),
            ):
                raise ValueError(
                    "candidate reconsideration was closed before promotion"
                )
            artifact_rows = await self.fetchall(
                "SELECT model_type, weights_json, weights_hash FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
                (candidate_id,),
            )
            model_artifacts = {}
            for artifact_row in artifact_rows:
                artifact_value = loads_protected_json(
                    artifact_row[1],
                    "ai_model_candidate_artifacts",
                    protected_identity(candidate_id, artifact_row[0]),
                    "weights",
                )
                if canonical_stable_hash(artifact_value) != artifact_row[2]:
                    raise ValueError("candidate artifact hash mismatch")
                model_artifacts[artifact_row[0]] = artifact_value
            if (
                not model_artifacts
                or canonical_stable_hash(model_weight_artifacts(model_artifacts))
                != candidate_row[0]
            ):
                raise ValueError("candidate artifact set is incomplete or mismatched")
            evaluation_row = await self.fetchone(
                "SELECT candidate_artifact_hash, champion_artifact_hash,\n                          dataset_fingerprint, evaluation_config_hash, decision,\n                          reason_codes_json, eligible_hour_count,\n                          eligible_independent_day_count, evaluation_config_json,\n                          decision_json, decision_hash, evaluator_contract_version\n                   FROM ai_model_evaluations_v2\n                   WHERE evaluation_id = ? AND candidate_id = ?",
                (evaluation_id, candidate_id),
            )
            if evaluation_row:
                rollback_candidate_id = (
                    await self._validate_model_candidate_promotion_v2(
                        candidate_id, candidate_row[0], evaluation_id, evaluation_row
                    )
                )
                promoted_at = SafeDateTimeUtil.now().isoformat()
                async with self._authorize_governance_write("promotion", candidate_id):
                    await self.execute(
                        "INSERT INTO ai_model_promotion_events\n                       (promotion_event_id, candidate_id, evaluation_id,\n                        candidate_artifact_hash, champion_artifact_hash, promoted_at)\n                       VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            candidate_id,
                            evaluation_id,
                            candidate_row[0],
                            evaluation_row[1],
                            promoted_at,
                        ),
                        auto_commit=False,
                    )
                await self.execute(
                    "INSERT INTO ai_active_model_pointer\n                       (id, active_candidate_id, last_known_good_candidate_id, promoted_at)\n                       VALUES (1, ?, ?, ?)\n                       ON CONFLICT(id) DO UPDATE SET active_candidate_id = excluded.active_candidate_id,\n                           last_known_good_candidate_id = excluded.last_known_good_candidate_id,\n                           promoted_at = excluded.promoted_at",
                    (candidate_id, rollback_candidate_id, promoted_at),
                    auto_commit=False,
                )
                completed_at = SafeDateTimeUtil.now().isoformat()
                await self.execute(
                    "INSERT INTO ai_model_candidate_reconsideration_closures\n                           (candidate_id, replacement_candidate_id, created_at)\n                       SELECT other.candidate_id, ?, ?\n                         FROM ai_model_candidates other\n                         JOIN ai_model_candidate_dispositions disposition\n                           ON disposition.candidate_id = other.candidate_id\n                         JOIN ai_model_candidate_reconsiderations reconsideration\n                           ON reconsideration.candidate_id = other.candidate_id\n                          AND disposition.disposition = 'migration_superseded'\n                          AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                          AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                        WHERE other.status = 'candidate'\n                          AND other.candidate_id != ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations evaluation\n                               WHERE evaluation.candidate_id = other.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                               WHERE evaluation.candidate_id = other.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                               WHERE closure.candidate_id = other.candidate_id\n                          )",
                    (candidate_id, completed_at, candidate_id),
                    auto_commit=False,
                )
                await self.execute(
                    "INSERT INTO ai_model_candidate_dispositions\n                           (candidate_id, disposition, replacement_candidate_id,\n                            retrain_request_id, created_at)\n                       SELECT other.candidate_id, 'superseded_by_promotion', ?, NULL, ?\n                         FROM ai_model_candidates other\n                        WHERE other.status = 'candidate'\n                          AND other.candidate_id != ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations evaluation\n                               WHERE evaluation.candidate_id = other.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                               WHERE evaluation.candidate_id = other.candidate_id\n                          )\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_candidate_dispositions disposition\n                               WHERE disposition.candidate_id = other.candidate_id\n                          )",
                    (candidate_id, completed_at, candidate_id),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE ai_model_retrain_requests\n                          SET status = 'completed', completion_decision = 'GO',\n                              completed_at = ?, updated_at = ?\n                        WHERE status = 'bound' AND bound_candidate_id = ?",
                    (completed_at, completed_at, candidate_id),
                    auto_commit=False,
                )
                await self._record_model_candidate_terminal_event(
                    candidate_id,
                    "promoted",
                    {
                        "candidate_id": candidate_id,
                        "evaluation_id": evaluation_id,
                        "promotion_event_candidate_hash": candidate_row[0],
                    },
                )
                return
            raise ValueError("matching V2 candidate evaluation is required")
        return None if True else None

    async def _validate_model_candidate_promotion_v2(
        self,
        candidate_id: str,
        candidate_artifact_hash: str,
        evaluation_id: str,
        evaluation: Sequence[Any],
        *,
        require_current_champion: bool = True,
        canonical_runs: bool = True,
    ) -> str:
        """Validate an intact V2 GO evaluation and return its rollback pointer.

        ``canonical_runs=False`` is the load-time proof of an already promoted
        champion: run snapshots are proven byte-identical to their hashes, the
        canonical-form proof having been made at recording and promotion time.
        """
        try:
            reason_codes = loads_protected_json(
                evaluation[5],
                "ai_model_evaluations_v2",
                protected_identity(evaluation_id, "reason_codes_json"),
                "evaluation",
            )
            evaluation_config_payload = loads_protected_json(
                evaluation[8],
                "ai_model_evaluations_v2",
                protected_identity(evaluation_id, "evaluation_config_json"),
                "evaluation",
            )
            decision_payload = loads_protected_json(
                evaluation[9],
                "ai_model_evaluations_v2",
                protected_identity(evaluation_id, "decision_json"),
                "evaluation",
            )
        except (TypeError, json.JSONDecodeError, ValueError) as payload_error:
            raise ValueError(
                "candidate evaluation payload is invalid"
            ) from payload_error
        if (
            evaluation[0] != candidate_artifact_hash
            or evaluation[4] != "GO"
            or reason_codes
            or (evaluation[11] != "cycle_aware_model_promotion_v2")
            or (decision_payload.get("go_no_go") != "GO")
            or (decision_payload.get("reason_codes") != [])
            or (decision_payload.get("evaluation_config_hash") != evaluation[3])
            or (decision_payload.get("eligible_independent_day_count") != evaluation[7])
            or (
                decision_payload.get("eligible_common_production_hour_count")
                != evaluation[6]
            )
            or (canonical_stable_hash(evaluation_config_payload) != evaluation[3])
            or (canonical_stable_hash(decision_payload) != evaluation[10])
        ):
            raise ValueError("candidate evaluation is not an intact GO decision")
        invalidation_row = await self.fetchone(
            "SELECT invalidation_reason\n                 FROM ai_model_evaluation_invalidations\n                WHERE evaluation_id = ?",
            (evaluation_id,),
        )
        if invalidation_row:
            raise ValueError(
                "candidate evaluation was invalidated by newer actual evidence"
            )
        sample_rows = await self.fetchall(
            "SELECT target_date, target_hour, forecast_cycle, shadow_run_id,\n                      shadow_prediction_id, actual_version_id, actual,\n                      champion_prediction, candidate_prediction, clean_eligible,\n                      forecast_anchor, input_fingerprint, policy_fingerprint,\n                      weather_regime, horizon\n                 FROM ai_model_evaluation_samples_v2\n                WHERE evaluation_id = ? ORDER BY sample_index",
            (evaluation_id,),
        )
        sample_payloads = [
            {
                "target_date": sample_row[0],
                "target_hour": sample_row[1],
                "forecast_cycle": sample_row[2],
                "shadow_run_id": sample_row[3],
                "shadow_prediction_id": sample_row[4],
                "actual_version_id": sample_row[5],
                "actual": sample_row[6],
                "champion_prediction": sample_row[7],
                "candidate_prediction": sample_row[8],
                "clean_eligible": bool(sample_row[9]),
                "is_production_hour": True,
                "forecast_anchor": sample_row[10],
                "input_fingerprint": sample_row[11],
                "policy_fingerprint": sample_row[12],
                "weather_regime": sample_row[13],
                "horizon": sample_row[14],
            }
            for sample_row in sample_rows
        ]
        normalized_samples = _normalize_cycle_evaluation_samples(sample_payloads)
        if (
            not normalized_samples
            or canonical_stable_hash(normalized_samples) != evaluation[2]
        ):
            raise ValueError(
                "candidate evaluation evidence is incomplete or mismatched"
            )
        await self._validate_cycle_evaluation_samples_provenance(
            candidate_id,
            candidate_artifact_hash,
            evaluation[1],
            normalized_samples,
            canonical_runs=canonical_runs,
        )
        stale_actual_row = await self.fetchone(
            "SELECT 1\n                 FROM ai_model_evaluation_samples_v2 sample\n                 JOIN ai_model_shadow_actual_versions actual\n                   ON actual.actual_version_id = sample.actual_version_id\n                WHERE sample.evaluation_id = ?\n                  AND (\n                      actual.clean_eligible != 1\n                      OR actual.evaluation_version != (\n                          SELECT MAX(latest.evaluation_version)\n                            FROM ai_model_shadow_actual_versions latest\n                           WHERE latest.shadow_prediction_id = actual.shadow_prediction_id\n                      )\n                  )\n                LIMIT 1",
            (evaluation_id,),
        )
        if stale_actual_row:
            raise ValueError("candidate evaluation actual evidence is stale")
        await self._validate_current_evaluation_actual_truth(evaluation_id)
        recomputed_decision = _recompute_cycle_aware_model_promotion_decision(
            evaluation_config_payload, normalized_samples
        )
        if canonical_stable_hash(recomputed_decision) != evaluation[10]:
            raise ValueError("candidate evaluation GO does not match frozen evidence")
        if require_current_champion:
            if await self.get_active_model_artifact_hash() != evaluation[1]:
                raise ValueError(
                    "candidate evaluation is stale for the active champion"
                )
        active_pointer_row = await self.fetchone(
            "SELECT active_candidate_id, last_known_good_candidate_id\n               FROM ai_active_model_pointer WHERE id = 1"
        )
        active_candidate_id = active_pointer_row[0] if active_pointer_row else None
        last_known_good_candidate_id = (
            active_pointer_row[1] if active_pointer_row else None
        )
        return active_candidate_id or last_known_good_candidate_id or candidate_id

    async def _fetch_current_evaluation_actual_truth_rows(
        self, evaluation_id: str
    ) -> list[Any]:
        """Frozen sample actuals next to the live production records they came from."""
        return await self.fetchall(
            "SELECT sample.actual, actual.actual_measured_at,\n                      prediction.target_date, prediction.target_hour,\n                      live.target_date, live.target_hour, live.actual_kwh,\n                      live.actual_measured_at, live.manual_override,\n                      live.inverter_clipped, live.exclude_from_learning,\n                      live.exclude_from_clean_evaluation, live.mppt_throttled,\n                      live.mppt_throttle_reason, live.actual_backfill_blocked,\n                      live.data_quarantine_reason, live.has_weather_alert,\n                      live.weather_alert_type, live.target_datetime\n                 FROM ai_model_evaluation_samples_v2 sample\n                 JOIN ai_model_shadow_actual_versions actual\n                   ON actual.actual_version_id = sample.actual_version_id\n                 JOIN ai_model_shadow_predictions prediction\n                   ON prediction.shadow_prediction_id = sample.shadow_prediction_id\n                 LEFT JOIN hourly_predictions live\n                   ON live.prediction_id = prediction.source_prediction_id\n                WHERE sample.evaluation_id = ?\n                ORDER BY sample.sample_index",
            (evaluation_id,),
        )

    async def _validate_current_evaluation_actual_truth(
        self, evaluation_id: str
    ) -> None:
        """Bind promotion atomically to the current production actual records."""
        actual_truth_rows = await self._fetch_current_evaluation_actual_truth_rows(
            evaluation_id
        )
        sample_count_row = await self.fetchone(
            "SELECT COUNT(*) FROM ai_model_evaluation_samples_v2\n                WHERE evaluation_id = ?",
            (evaluation_id,),
        )
        if not sample_count_row or len(actual_truth_rows) != int(sample_count_row[0]):
            raise ValueError(
                "candidate evaluation current actual evidence is incomplete"
            )
        now = datetime.now().astimezone()
        for actual_truth_row in actual_truth_rows:
            try:
                frozen_actual_kwh = float(actual_truth_row[0])
                current_actual_kwh = float(actual_truth_row[6])
            except (TypeError, ValueError) as evidence_error:
                raise ValueError(
                    "candidate evaluation current actual is invalid"
                ) from evidence_error
            if (
                not math.isfinite(current_actual_kwh)
                or current_actual_kwh < 0
                or abs(frozen_actual_kwh - current_actual_kwh) > 1e-09
                or (str(actual_truth_row[2]) != str(actual_truth_row[4]))
                or (int(actual_truth_row[3]) != int(actual_truth_row[5]))
            ):
                raise ValueError("candidate evaluation current actual evidence changed")
            try:
                frozen_measurement_time = datetime.fromisoformat(
                    str(actual_truth_row[1])
                )
                current_measurement_time, normalized_target_datetime = (
                    validate_hourly_actual_measurement_time(
                        str(actual_truth_row[7]),
                        actual_truth_row[18],
                        actual_truth_row[4],
                        actual_truth_row[5],
                        now=now,
                    )
                )
            except (TypeError, ValueError) as evidence_error:
                raise ValueError(
                    "candidate evaluation current measurement time is invalid"
                ) from evidence_error
            if (
                frozen_measurement_time.tzinfo is None
                or frozen_measurement_time.utcoffset() is None
                or current_measurement_time.tzinfo is None
                or (current_measurement_time.utcoffset() is None)
                or (frozen_measurement_time != current_measurement_time)
            ):
                raise ValueError(
                    "candidate evaluation current measurement time changed"
                )
            clean_eligible, exclusion_reason = self.classify_hourly_clean_evaluation(
                {
                    "actual_kwh": current_actual_kwh,
                    "manual_override": actual_truth_row[8],
                    "inverter_clipped": actual_truth_row[9],
                    "exclude_from_learning": actual_truth_row[10],
                    "exclude_from_clean_evaluation": actual_truth_row[11],
                    "mppt_throttled": actual_truth_row[12],
                    "mppt_throttle_reason": actual_truth_row[13],
                    "actual_backfill_blocked": actual_truth_row[14],
                    "data_quarantine_reason": actual_truth_row[15],
                    "has_weather_alert": actual_truth_row[16],
                    "weather_alert_type": actual_truth_row[17],
                }
            )
            if not clean_eligible:
                raise ValueError("candidate evaluation current actual is excluded")

    async def _promotion_evidence_stamp(
        self, candidate_id: str, candidate: Any, evaluation_id: str, promotion: Any
    ) -> str:
        """Digest of every relational fact the promotion proof reads.

        Hash columns and version rows stand in for the raw snapshot bytes:
        an unchanged stamp means the byte-level proof already done in this
        process would repeat identically; a changed one forces it again.
        """
        row = await self.fetchone(
            "SELECT active_candidate_id, last_known_good_candidate_id, promoted_at\n                 FROM ai_active_model_pointer WHERE id = 1"
        )
        row_2 = await self.fetchone(
            "SELECT invalidation_reason\n                 FROM ai_model_evaluation_invalidations\n                WHERE evaluation_id = ?",
            (evaluation_id,),
        )
        rows = await self.fetchall(
            "SELECT sample_index, target_date, target_hour, forecast_cycle,\n                      shadow_run_id, shadow_prediction_id, actual_version_id, actual,\n                      champion_prediction, candidate_prediction, clean_eligible,\n                      forecast_anchor, input_fingerprint, policy_fingerprint,\n                      weather_regime, horizon\n                 FROM ai_model_evaluation_samples_v2\n                WHERE evaluation_id = ? ORDER BY sample_index",
            (evaluation_id,),
        )
        rows_2 = await self.fetchall(
            "SELECT run.shadow_run_id, run.run_date, run.created_at,\n                      run.forecast_anchor, run.contract_version,\n                      run.champion_artifact_hash, run.candidate_artifact_hash,\n                      run.input_snapshot_hash, run.policy_snapshot_hash,\n                      run.snapshot_codec, run.payload_state,\n                      batch.status, batch.completed_at,\n                      provenance.forecast_cycle, provenance.trigger_kind,\n                      provenance.promotion_eligible, provenance.contract_version\n                 FROM ai_model_shadow_runs run\n                 LEFT JOIN ensemble_shadow_batches batch\n                   ON batch.morning_batch_id = run.morning_batch_id\n                 LEFT JOIN ai_model_shadow_run_provenance provenance\n                   ON provenance.shadow_run_id = run.shadow_run_id\n                WHERE run.candidate_id = ?\n                ORDER BY run.shadow_run_id, provenance.forecast_cycle",
            (candidate_id,),
        )
        rows_3 = await self.fetchall(
            "SELECT prediction.shadow_prediction_id, prediction.shadow_run_id,\n                      prediction.source_prediction_id, prediction.target_date,\n                      prediction.target_hour, prediction.horizon,\n                      prediction.weather_regime, prediction.input_fingerprint,\n                      prediction.policy_fingerprint, prediction.champion_prediction,\n                      prediction.candidate_prediction,\n                      prediction.champion_result_hash, prediction.candidate_result_hash,\n                      prediction.result_codec, prediction.payload_state,\n                      actual.actual_version_id, actual.evaluation_version,\n                      actual.actual_kwh, actual.clean_eligible, actual.actual_measured_at\n                 FROM ai_model_evaluation_samples_v2 sample\n                 JOIN ai_model_shadow_predictions prediction\n                   ON prediction.shadow_prediction_id = sample.shadow_prediction_id\n                 LEFT JOIN ai_model_shadow_actual_versions actual\n                   ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                WHERE sample.evaluation_id = ?\n                ORDER BY sample.sample_index, actual.actual_version_id",
            (evaluation_id,),
        )
        current_actual_truth_rows = (
            await self._fetch_current_evaluation_actual_truth_rows(evaluation_id)
        )
        return canonical_stable_hash(
            {
                "candidate_id": candidate_id,
                "candidate": _stamp_row(candidate),
                "evaluation_id": evaluation_id,
                "promotion": _stamp_row(promotion),
                "pointer": _stamp_row(row),
                "invalidation": _stamp_row(row_2),
                "samples": [_stamp_row(row_3) for row_3 in rows],
                "runs": [_stamp_row(row_3) for row_3 in rows_2],
                "predictions": [_stamp_row(row_3) for row_3 in rows_3],
                "live": [_stamp_row(row_3) for row_3 in current_actual_truth_rows],
            }
        )

    async def _get_promoted_candidate_weights(
        self, candidate_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if candidate_id is None:
            active_pointer_row = await self.fetchone(
                "SELECT active_candidate_id FROM ai_active_model_pointer WHERE id = 1"
            )
            candidate_id = (
                str(active_pointer_row[0])
                if active_pointer_row and active_pointer_row[0]
                else None
            )
        if not candidate_id:
            return
        candidate_row = await self.fetchone(
            "SELECT active_model, training_samples, accuracy, rmse, artifact_hash\n               FROM ai_model_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        if not candidate_row:
            raise ValueError("active candidate pointer is invalid")
        promotion_evaluation_row = await self.fetchone(
            "SELECT evaluation.candidate_artifact_hash,\n                      evaluation.champion_artifact_hash,\n                      evaluation.dataset_fingerprint,\n                      evaluation.evaluation_config_hash, evaluation.decision,\n                      evaluation.reason_codes_json, evaluation.eligible_hour_count,\n                      evaluation.eligible_independent_day_count,\n                      evaluation.evaluation_config_json, evaluation.decision_json,\n                      evaluation.decision_hash, evaluation.evaluator_contract_version,\n                      evaluation.evaluation_id\n                 FROM ai_model_promotion_events event\n                 JOIN ai_model_evaluations_v2 evaluation\n                   ON evaluation.evaluation_id = event.evaluation_id\n                  AND evaluation.candidate_id = event.candidate_id\n                  AND evaluation.decision = 'GO'\n                  AND evaluation.candidate_artifact_hash = event.candidate_artifact_hash\n                 LEFT JOIN ai_model_evaluation_invalidations invalidation\n                   ON invalidation.evaluation_id = evaluation.evaluation_id\n                WHERE event.candidate_id = ?\n                  AND event.candidate_artifact_hash = ?\n                  AND invalidation.evaluation_id IS NULL",
            (candidate_id, candidate_row[4]),
        )
        if not promotion_evaluation_row:
            raise ValueError("active candidate has no intact promotion provenance")
        evaluation_id = str(promotion_evaluation_row[12])
        evidence_stamp = await self._promotion_evidence_stamp(
            candidate_id, candidate_row, evaluation_id, promotion_evaluation_row
        )
        if self._verified_promotion_evidence.get(candidate_id) != evidence_stamp:
            self._verified_promotion_evidence.pop(candidate_id, None)
            await self._validate_model_candidate_promotion_v2(
                candidate_id,
                candidate_row[4],
                evaluation_id,
                promotion_evaluation_row[:12],
                require_current_champion=False,
                canonical_runs=False,
            )
            self._verified_promotion_evidence[candidate_id] = evidence_stamp
        artifact_rows = await self.fetchall(
            "SELECT model_type, weights_json, weights_hash FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
            (candidate_id,),
        )
        model_artifacts = {}
        for artifact_row in artifact_rows:
            artifact_value = loads_protected_json(
                artifact_row[1],
                "ai_model_candidate_artifacts",
                protected_identity(candidate_id, artifact_row[0]),
                "weights",
            )
            if canonical_stable_hash(artifact_value) != artifact_row[2]:
                raise ValueError("active candidate artifact hash mismatch")
            model_artifacts[artifact_row[0]] = artifact_value
        if (
            not model_artifacts
            or canonical_stable_hash(model_weight_artifacts(model_artifacts))
            != candidate_row[4]
        ):
            raise ValueError("active candidate artifact set hash mismatch")
        return {
            "version": "3.0",
            "active_model": candidate_row[0],
            "training_samples": candidate_row[1],
            "accuracy": candidate_row[2],
            "rmse": candidate_row[3],
            "artifact_hash": candidate_row[4],
            **model_artifacts,
        }

    async def get_model_weights(self) -> Optional[Dict[str, Any]]:
        """Get AI model weights from database. @zara

        V16.1 FIX: Now uses structured tables (ai_lstm_weights, ai_ridge_weights)
        as the primary source instead of JSON blobs in ai_model_weights.
        This ensures has_attention flag is consistent with actual attention weights.
        """
        async with self.transaction():
            try:
                promoted_weights = await self._get_promoted_candidate_weights()
                if promoted_weights is not None:
                    return promoted_weights
            except (ValueError, json.JSONDecodeError) as governance_error:
                _LOGGER.error(
                    "Active model governance verification failed: %s", governance_error
                )
                active_pointer_row = await self.fetchone(
                    "SELECT active_candidate_id, last_known_good_candidate_id\n                         FROM ai_active_model_pointer WHERE id = 1"
                )
                active_candidate_id = (
                    str(active_pointer_row[0])
                    if active_pointer_row and active_pointer_row[0]
                    else None
                )
                last_known_good_candidate_id = (
                    str(active_pointer_row[1])
                    if active_pointer_row and active_pointer_row[1]
                    else None
                )
                if (
                    not last_known_good_candidate_id
                    or last_known_good_candidate_id == active_candidate_id
                ):
                    return None
                try:
                    last_known_good_weights = (
                        await self._get_promoted_candidate_weights(
                            last_known_good_candidate_id
                        )
                    )
                except (
                    ValueError,
                    json.JSONDecodeError,
                ) as fallback_verification_error:
                    _LOGGER.error(
                        "Last-known-good model governance verification failed: %s",
                        fallback_verification_error,
                    )
                    return None
                latest_active_pointer_row = await self.fetchone(
                    "SELECT active_candidate_id FROM ai_active_model_pointer WHERE id = 1"
                )
                if (
                    not latest_active_pointer_row
                    or latest_active_pointer_row[0] != active_candidate_id
                ):
                    raise ValueError(
                        "active model changed during last-known-good recovery"
                    )
                await self.execute(
                    "UPDATE ai_active_model_pointer\n                          SET active_candidate_id = ?,\n                              last_known_good_candidate_id = ?,\n                              promoted_at = ?\n                        WHERE id = 1",
                    (
                        last_known_good_candidate_id,
                        last_known_good_candidate_id,
                        SafeDateTimeUtil.now().isoformat(),
                    ),
                    auto_commit=False,
                )
                _LOGGER.warning(
                    "Recovered invalid active model %s with last-known-good model %s",
                    active_candidate_id,
                    last_known_good_candidate_id,
                )
                return last_known_good_weights
        metadata_row = await self.fetchone(
            "SELECT version, active_model, training_samples, last_trained,\n                      accuracy, rmse\n               FROM ai_learned_weights_meta WHERE id = 1"
        )
        if not metadata_row:
            return
        weights = {
            "version": metadata_row[0] or "2.0",
            "active_model": metadata_row[1] or "none",
            "training_samples": metadata_row[2] or 0,
            "last_trained": str(metadata_row[3]) if metadata_row[3] else None,
            "accuracy": metadata_row[4],
            "rmse": metadata_row[5],
        }
        lstm_weights = await self._load_lstm_weights()
        if lstm_weights:
            weights["lstm"] = lstm_weights
            weights["has_attention"] = lstm_weights.get("has_attention", False)
        ridge_weights = await self._load_ridge_weights()
        if ridge_weights:
            weights["ridge"] = ridge_weights
        return weights

    async def _get_model_weights_legacy(self) -> Optional[Dict[str, Any]]:
        """Fallback to old ai_model_weights table. @zara"""
        weight_rows = await self.fetchall(
            "SELECT weight_type, weight_data FROM ai_model_weights"
        )
        if not weight_rows:
            return
        weights_by_type = {}
        for weight_row in weight_rows:
            if str(weight_row[0]).startswith("coeff:"):
                continue
            try:
                weights_by_type[weight_row[0]] = loads_protected_json(
                    weight_row[1], "ai_model_weights", str(weight_row[0]), "weights"
                )
            except ValueError:
                return
        return weights_by_type or None

    async def _load_lstm_weights(
        self, *, legacy_only: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Load LSTM weights from the sealed blob, or from legacy rows."""
        if not legacy_only:
            coefficient_weights = await self._open_coefficient_blob("lstm")
            if coefficient_weights is not _NO_COEFFICIENT_BLOB:
                return coefficient_weights
        weight_rows = await self.fetchall(
            "SELECT weight_type, weight_index, weight_value\n               FROM ai_lstm_weights ORDER BY weight_type, weight_index"
        )
        if not weight_rows:
            return
        weights_by_type = {}
        for weight_row in weight_rows:
            weight_type = weight_row[0]
            if weight_type not in weights_by_type:
                weights_by_type[weight_type] = []
            weights_by_type[weight_type].append(weight_row[2])
        hidden_size = len(weights_by_type.get("bf", []))
        output_count = len(weights_by_type.get("by", []))
        if hidden_size == 0:
            return
        forget_gate_element_count = len(weights_by_type.get("Wf", []))
        combined_input_width = (
            forget_gate_element_count // hidden_size if hidden_size > 0 else 0
        )
        input_size = (
            combined_input_width - hidden_size
            if combined_input_width > hidden_size
            else 0
        )
        has_attention = False
        layer_count = 1
        attention_head_count = 1
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
            attention_head_count = metadata_row[5] or 1
        weights = {
            "input_size": input_size,
            "hidden_size": hidden_size,
            "num_outputs": output_count,
            "has_attention": has_attention,
            "num_layers": layer_count,
            "num_heads": attention_head_count,
            "training_samples": training_samples,
            "accuracy": accuracy,
            "rmse": rmse,
        }
        for matrix_type in ("Wf", "Wi", "Wc", "Wo"):
            if matrix_type in weights_by_type:
                flat_values = weights_by_type[matrix_type]
                if combined_input_width > 0:
                    weights[matrix_type] = [
                        flat_values[
                            matrix_row_index
                            * combined_input_width : (matrix_row_index + 1)
                            * combined_input_width
                        ]
                        for matrix_row_index in range(hidden_size)
                    ]
        if "Wy" in weights_by_type and output_count > 0:
            secondary_hidden_size = len(weights_by_type.get("bf2", [])) or hidden_size
            flat_values = weights_by_type["Wy"]
            weights["Wy"] = [
                flat_values[
                    matrix_row_index
                    * secondary_hidden_size : (matrix_row_index + 1)
                    * secondary_hidden_size
                ]
                for matrix_row_index in range(output_count)
            ]
        for bias_type in ("bf", "bi", "bc", "bo", "by"):
            if bias_type in weights_by_type:
                weights[bias_type] = weights_by_type[bias_type]
        if layer_count >= 2:
            secondary_hidden_size = len(weights_by_type.get("bf2", []))
            if secondary_hidden_size > 0:
                second_layer_input_width = (
                    len(weights_by_type.get("Wf2", [])) // secondary_hidden_size
                    if secondary_hidden_size
                    else 0
                )
                for matrix_type in ("Wf2", "Wi2", "Wc2", "Wo2"):
                    if matrix_type in weights_by_type and second_layer_input_width > 0:
                        flat_values = weights_by_type[matrix_type]
                        weights[matrix_type] = [
                            flat_values[
                                matrix_row_index
                                * second_layer_input_width : (matrix_row_index + 1)
                                * second_layer_input_width
                            ]
                            for matrix_row_index in range(secondary_hidden_size)
                        ]
                for bias_type in ("bf2", "bi2", "bc2", "bo2"):
                    if bias_type in weights_by_type:
                        weights[bias_type] = weights_by_type[bias_type]
            for normalization_parameter in ("ln_gamma", "ln_beta"):
                if normalization_parameter in weights_by_type:
                    weights[normalization_parameter] = weights_by_type[
                        normalization_parameter
                    ]
        attention_width = len(weights_by_type.get("bf2", [])) or hidden_size
        required_attention_weights = [
            "W_query",
            "W_key",
            "W_value",
            "W_attn_out",
            "b_attn_out",
        ]
        attention_weights_complete = all(
            (
                attention_weight_type in weights_by_type
                for attention_weight_type in required_attention_weights
            )
        )
        if has_attention and attention_weights_complete:
            for matrix_type in ("W_query", "W_key", "W_value", "W_attn_out"):
                flat_values = weights_by_type[matrix_type]
                weights[matrix_type] = [
                    flat_values[
                        matrix_row_index
                        * attention_width : (matrix_row_index + 1)
                        * attention_width
                    ]
                    for matrix_row_index in range(attention_width)
                ]
            weights["b_attn_out"] = weights_by_type["b_attn_out"]
        elif has_attention and (not attention_weights_complete):
            weights["has_attention"] = False
            _LOGGER.warning(
                "ai_lstm_meta.has_attention=True but attention weights missing. Setting has_attention=False."
            )
        return weights if True else None

    async def _load_ridge_weights(
        self, *, legacy_only: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Load Ridge weights from the sealed blob, or from legacy rows."""
        if not legacy_only:
            coefficient_weights = await self._open_coefficient_blob("ridge")
            if coefficient_weights is not _NO_COEFFICIENT_BLOB:
                return coefficient_weights
        weight_rows = await self.fetchall(
            "SELECT row_index, col_index, weight_value\n               FROM ai_ridge_weights ORDER BY row_index, col_index"
        )
        if not weight_rows:
            return
        maximum_output_index = max((weight_row[0] for weight_row in weight_rows))
        maximum_column_index = max((weight_row[1] for weight_row in weight_rows))
        output_count = maximum_output_index + 1
        stored_column_count = maximum_column_index + 1
        input_size = None
        sequence_length = 24
        flat_size = None
        alpha = None
        loo_cv_score = None
        training_samples = 0
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
            training_samples = metadata_row[6] or 0
            accuracy = metadata_row[7]
            rmse = metadata_row[8]
            feature_means_json = metadata_row[9]
            feature_stds_json = metadata_row[10]
        if flat_size:
            matrix_column_count = flat_size + 1
        else:
            matrix_column_count = stored_column_count
        if matrix_column_count < stored_column_count:
            matrix_column_count = stored_column_count
        weights = [[0.0] * matrix_column_count for output_index in range(output_count)]
        for weight_row in weight_rows:
            weights[weight_row[0]][weight_row[1]] = weight_row[2]
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
            "trained_samples": training_samples,
            "accuracy": accuracy,
            "rmse": rmse,
            "feature_means": feature_means,
            "feature_stds": feature_stds,
        }

    async def _migrate_weather_mlp(self) -> None:
        """Weather MLP tables migration. @zara"""
        try:
            await self.execute(
                "CREATE TABLE IF NOT EXISTS ai_weather_mlp_weights (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       weight_type TEXT NOT NULL,\n                       weight_index INTEGER NOT NULL,\n                       weight_value REAL NOT NULL,\n                       UNIQUE(weight_type, weight_index)\n                   )"
            )
            await self.execute(
                "CREATE TABLE IF NOT EXISTS ai_weather_mlp_meta (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       input_size INTEGER DEFAULT 8,\n                       hidden1 INTEGER DEFAULT 16,\n                       hidden2 INTEGER DEFAULT 8,\n                       training_samples INTEGER DEFAULT 0,\n                       accuracy REAL DEFAULT 0.0,\n                       rmse REAL DEFAULT 0.0,\n                       training_contract_version TEXT,\n                       last_trained TIMESTAMP,\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            await self.commit()
        except Exception as migration_error:
            _LOGGER.debug("Weather MLP migration: %s", migration_error)
        return None if True else None

    async def save_weather_mlp_weights(
        self,
        weights_data: Dict[str, Any],
        *,
        training_contract_version: Optional[str] = None,
    ) -> None:
        """Save Weather MLP weights to database. @zara"""
        try:
            async with self.transaction():
                await self.execute(
                    "INSERT INTO ai_weather_mlp_meta\n                       (id, input_size, hidden1, hidden2, training_samples, accuracy, rmse,\n                        training_contract_version, last_trained, updated_at)\n                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(id) DO UPDATE SET\n                           input_size = excluded.input_size,\n                           hidden1 = excluded.hidden1,\n                           hidden2 = excluded.hidden2,\n                           training_samples = excluded.training_samples,\n                           accuracy = excluded.accuracy,\n                           rmse = excluded.rmse,\n                           training_contract_version = excluded.training_contract_version,\n                           last_trained = excluded.last_trained,\n                           updated_at = excluded.updated_at",
                    (
                        weights_data.get("input_size", 8),
                        weights_data.get("hidden1", 16),
                        weights_data.get("hidden2", 8),
                        weights_data.get("training_samples", 0),
                        weights_data.get("accuracy", 0.0),
                        weights_data.get("rmse", 0.0),
                        training_contract_version,
                        SafeDateTimeUtil.now(),
                        SafeDateTimeUtil.now(),
                    ),
                )
                await self._store_coefficient_blob("weather_mlp", weights_data)
                await self.execute("DELETE FROM ai_weather_mlp_weights")
                await self.commit()
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
                "SELECT input_size, hidden1, hidden2, training_samples,\n                          accuracy, rmse, last_trained, training_contract_version\n                   FROM ai_weather_mlp_meta WHERE id = 1"
            )
            if not metadata_row or not metadata_row[3]:
                return None
            if metadata_row[7] != WEATHER_MLP_TRAINING_CONTRACT_VERSION:
                _LOGGER.info(
                    "Weather MLP: stored weights predate contract %s (found %r) - ignored until the MLP is retrained",
                    WEATHER_MLP_TRAINING_CONTRACT_VERSION,
                    metadata_row[7],
                )
                return
            coefficient_weights = await self._open_coefficient_blob("weather_mlp")
            if coefficient_weights is not _NO_COEFFICIENT_BLOB:
                return coefficient_weights
            weight_rows = await self.fetchall(
                "SELECT weight_type, weight_index, weight_value\n                   FROM ai_weather_mlp_weights ORDER BY weight_type, weight_index"
            )
            if not weight_rows:
                return
            weights_by_type = {}
            for weight_row in weight_rows:
                weight_type = weight_row[0]
                if weight_type not in weights_by_type:
                    weights_by_type[weight_type] = []
                weights_by_type[weight_type].append(weight_row[2])
            input_size = metadata_row[0] or 8
            first_hidden_size = metadata_row[1] or 16
            second_hidden_size = metadata_row[2] or 8
            weights = {
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
                    weights[weight_type] = [
                        flat_values[
                            matrix_row_index
                            * matrix_columns : (matrix_row_index + 1)
                            * matrix_columns
                        ]
                        for matrix_row_index in range(matrix_rows)
                    ]
                else:
                    weights[weight_type] = flat_values
            return weights
        except Exception as load_error:
            _LOGGER.debug("Could not load Weather MLP weights: %s", load_error)
            return

    @_atomic_database_write
    async def save_learning_config(self, config: Dict[str, Any]) -> None:
        """Save physics learning config."""
        physics_defaults = config["physics_defaults"]
        learning_metadata = config["metadata"]
        await self.execute(
            "INSERT INTO physics_learning_config\n               (id, version, albedo, system_efficiency, learned_efficiency_factor,\n                rolling_window_days, min_samples, updated_at)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   version = excluded.version,\n                   albedo = excluded.albedo,\n                   system_efficiency = excluded.system_efficiency,\n                   learned_efficiency_factor = excluded.learned_efficiency_factor,\n                   rolling_window_days = excluded.rolling_window_days,\n                   min_samples = excluded.min_samples,\n                   updated_at = excluded.updated_at",
            (
                config["version"],
                physics_defaults["albedo"],
                physics_defaults["system_efficiency"],
                physics_defaults["learned_efficiency_factor"],
                learning_metadata["rolling_window_days"],
                learning_metadata["min_samples"],
                _safe_dt(config.get("updated_at")),
            ),
        )
        for group_name, group_calibration in config["group_calibration"].items():
            await self._save_group_calibration(group_name, group_calibration)
        await self.commit()

    @_atomic_database_write
    async def _save_group_calibration(
        self, group_name: str, group_data: Dict[str, Any]
    ) -> None:
        """Save calibration data for a panel group."""
        await self.execute(
            "INSERT INTO physics_calibration_groups\n               (group_name, global_factor, sample_count, confidence, last_updated)\n               VALUES (?, ?, ?, ?, ?)\n               ON CONFLICT(group_name) DO UPDATE SET\n                   global_factor = excluded.global_factor,\n                   sample_count = excluded.sample_count,\n                   confidence = excluded.confidence,\n                   last_updated = excluded.last_updated",
            (
                group_name,
                group_data["global_factor"],
                group_data["sample_count"],
                group_data["confidence"],
                _safe_dt(group_data.get("last_updated")),
            ),
        )
        await self.execute(
            "DELETE FROM physics_calibration_hourly WHERE group_name = ?", (group_name,)
        )
        for hour_key, hourly_factor in group_data["hourly_factors"].items():
            hour = int(hour_key)
            await self.execute(
                "INSERT INTO physics_calibration_hourly (group_name, hour, factor) VALUES (?, ?, ?)",
                (group_name, hour, hourly_factor),
            )
        await self.execute(
            "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
            (group_name,),
        )
        await self.execute(
            "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
            (group_name,),
        )
        for bucket_name, bucket_calibration in group_data["bucket_factors"].items():
            await self.execute(
                "INSERT INTO physics_calibration_buckets\n                   (group_name, bucket_name, global_factor, sample_count, confidence)\n                   VALUES (?, ?, ?, ?, ?)",
                (
                    group_name,
                    bucket_name,
                    bucket_calibration["global_factor"],
                    bucket_calibration["sample_count"],
                    bucket_calibration["confidence"],
                ),
            )
            for hour_key, hourly_factor in bucket_calibration["hourly_factors"].items():
                hour = int(hour_key)
                await self.execute(
                    "INSERT INTO physics_calibration_bucket_hourly\n                       (group_name, bucket_name, hour, factor) VALUES (?, ?, ?, ?)",
                    (group_name, bucket_name, hour, hourly_factor),
                )

    async def get_group_calibration(self, group_name: str) -> Optional[Dict[str, Any]]:
        """Get calibration data for a panel group."""
        calibration_row = await self.fetchone(
            "SELECT global_factor, sample_count, confidence, last_updated\n               FROM physics_calibration_groups WHERE group_name = ?",
            (group_name,),
        )
        if not calibration_row:
            return
        hourly_factor_rows = await self.fetchall(
            "SELECT hour, factor FROM physics_calibration_hourly WHERE group_name = ?",
            (group_name,),
        )
        hourly_factors = {
            str(hourly_factor_row[0]): hourly_factor_row[1]
            for hourly_factor_row in hourly_factor_rows
        }
        bucket_factors = {}
        bucket_factor_rows = await self.fetchall(
            "SELECT bucket_name, global_factor, sample_count, confidence\n               FROM physics_calibration_buckets WHERE group_name = ?",
            (group_name,),
        )
        for bucket_factor_row in bucket_factor_rows:
            bucket_name = bucket_factor_row[0]
            bucket_factors[bucket_name] = {
                "global_factor": bucket_factor_row[1],
                "sample_count": bucket_factor_row[2],
                "confidence": bucket_factor_row[3],
                "hourly_factors": {},
            }
        bucket_hourly_factor_rows = await self.fetchall(
            "SELECT bucket_name, hour, factor FROM physics_calibration_bucket_hourly WHERE group_name = ?",
            (group_name,),
        )
        for bucket_hourly_factor_row in bucket_hourly_factor_rows:
            bucket_factors[bucket_hourly_factor_row[0]]["hourly_factors"][
                str(bucket_hourly_factor_row[1])
            ] = bucket_hourly_factor_row[2]
        return {
            "global_factor": calibration_row[0],
            "sample_count": calibration_row[1],
            "confidence": calibration_row[2],
            "last_updated": calibration_row[3],
            "hourly_factors": hourly_factors,
            "bucket_factors": bucket_factors,
        }

    @_atomic_database_write
    async def save_calibration_history(
        self, history_data: List[Dict[str, Any]]
    ) -> None:
        """Save calibration history."""
        for item_value in history_data:
            calibration_date = _safe_date(item_value.get("date"))
            for group_name, group_calibration in item_value["groups"].items():
                for hour_key, hourly_sample in group_calibration["hourly"].items():
                    target_hour = int(hour_key)
                    await self.execute(
                        "INSERT INTO physics_calibration_history\n                           (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                           VALUES (?, ?, NULL, ?, ?, ?, ?)\n                           ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                               avg_ratio = excluded.avg_ratio,\n                               sample_count = excluded.sample_count",
                        (
                            calibration_date,
                            group_name,
                            target_hour,
                            hourly_sample,
                            group_calibration["sample_count"],
                            item_value.get("source"),
                        ),
                    )
                for bucket_name, bucket_calibration in group_calibration.get(
                    "buckets", {}
                ).items():
                    for hour_key, hourly_sample in bucket_calibration["hourly"].items():
                        target_hour = int(hour_key)
                        await self.execute(
                            "INSERT INTO physics_calibration_history\n                               (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                               VALUES (?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                                   avg_ratio = excluded.avg_ratio,\n                                   sample_count = excluded.sample_count",
                            (
                                calibration_date,
                                group_name,
                                bucket_name,
                                target_hour,
                                hourly_sample,
                                bucket_calibration["sample_count"],
                                item_value.get("source"),
                            ),
                        )
            continue
        await self.commit()

    @_atomic_database_write
    async def save_weather_expert_weights(self, data: Dict[str, Any]) -> None:
        """Save weather expert weights."""
        for cloud_type, expert_weights in data["weights"].items():
            for expert_name, weight in expert_weights.items():
                await self.execute(
                    "INSERT INTO weather_expert_weights\n                       (cloud_type, expert_name, weight,\n                        learning_contract_version, last_updated)\n                       VALUES (?, ?, ?, ?, ?)\n                       ON CONFLICT(cloud_type, expert_name) DO UPDATE SET\n                           weight = excluded.weight,\n                           learning_contract_version = excluded.learning_contract_version,\n                           last_updated = excluded.last_updated",
                    (
                        cloud_type,
                        expert_name,
                        weight,
                        WEATHER_EXPERT_LEGACY_CONTRACT_VERSION,
                        _safe_dt(data.get("metadata", {}).get("last_updated")),
                    ),
                )
        await self.commit()

    async def get_weather_expert_weights(self, cloud_type: str) -> Dict[str, float]:
        """Get weather expert weights for a cloud type."""
        rows = await self.fetchall(
            "SELECT expert_name, weight\n               FROM weather_expert_weights\n               WHERE cloud_type = ? AND learning_contract_version = ?",
            (cloud_type, WEATHER_EXPERT_STATIC_CONTRACT_VERSION),
        )
        return {row[0]: row[1] for row in rows}

    @_atomic_database_write
    async def save_weather_source_weights(self, data: Dict[str, Any]) -> None:
        """Save weather source weights."""
        for source_name, weight in data["weights"].items():
            last_mae = data["learning_metadata"]["last_mae"].get(source_name)
            await self.execute(
                "INSERT INTO weather_source_weights\n                   (source_name, weight, last_mae, version, last_updated)\n                   VALUES (?, ?, ?, ?, ?)\n                   ON CONFLICT(source_name) DO UPDATE SET\n                       weight = excluded.weight,\n                       last_mae = excluded.last_mae,\n                       last_updated = excluded.last_updated",
                (
                    source_name,
                    weight,
                    last_mae,
                    data.get("version"),
                    _safe_dt(data.get("learning_metadata", {}).get("last_updated")),
                ),
            )
        await self.commit()

    @_atomic_database_write
    async def save_coordinator_state(self, state: Dict[str, Any]) -> None:
        """Save coordinator state."""
        await self.execute(
            "INSERT INTO coordinator_state\n               (id, expected_daily_production, last_set_date, last_updated)\n               VALUES (1, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   expected_daily_production = excluded.expected_daily_production,\n                   last_set_date = excluded.last_set_date,\n                   last_updated = excluded.last_updated",
            (
                state.get("expected_daily_production"),
                _safe_date(state.get("last_set_date")),
                _safe_dt(state.get("last_updated")),
            ),
        )
        await self.commit()

    async def get_coordinator_state(self) -> Optional[Dict[str, Any]]:
        """Get coordinator state."""
        row = await self.fetchone(
            "SELECT expected_daily_production, last_set_date FROM coordinator_state WHERE id = 1"
        )
        if not row:
            return
        return {"expected_daily_production": row[0], "last_set_date": str(row[1])}

    async def get_scheduled_task_completed_period(self, task_key: str) -> Optional[str]:
        """Return the last successfully completed period for a scheduled task."""
        row = await self.fetchone(
            "SELECT completed_period FROM scheduled_task_state WHERE task_key = ?",
            (task_key,),
        )
        return str(row[0]) if row and row[0] else None

    async def _record_model_candidate_terminal_event(
        self, candidate_id: str, terminal_reason: str, details: Mapping[str, Any]
    ) -> bool:
        """Append one immutable terminal lifecycle event."""
        if terminal_reason not in MODEL_CANDIDATE_TERMINAL_REASONS:
            raise ValueError("invalid model candidate terminal reason")
        async with self.transaction():
            row = await self.fetchone(
                "SELECT terminal_reason FROM ai_model_candidate_terminal_events\n                    WHERE candidate_id = ?",
                (candidate_id,),
            )
            if row:
                if True:
                    return False
            async with self._authorize_governance_write("terminal", candidate_id):
                await self.execute(
                    "INSERT INTO ai_model_candidate_terminal_events\n                   (candidate_id, terminal_reason, terminal_at, policy_version,\n                    details_hash)\n                   VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        terminal_reason,
                        SafeDateTimeUtil.now().isoformat(),
                        MODEL_CANDIDATE_RETENTION_POLICY_VERSION,
                        canonical_stable_hash(dict(details)),
                    ),
                    auto_commit=False,
                )
        if True:
            return True

    async def reconcile_model_candidate_terminal_events(self) -> int:
        """Backfill explicit lifecycle events from immutable governance facts."""
        rows = await self.fetchall(
            "SELECT candidate.candidate_id,\n                      CASE\n                        WHEN EXISTS (\n                            SELECT 1 FROM ai_model_promotion_events promotion\n                             WHERE promotion.candidate_id = candidate.candidate_id)\n                          THEN 'promoted'\n                        WHEN EXISTS (\n                            SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                             WHERE evaluation.candidate_id = candidate.candidate_id\n                               AND evaluation.decision = 'NO_GO')\n                          THEN 'no_go'\n                        WHEN EXISTS (\n                            SELECT 1\n                              FROM ai_model_evaluations_v2 evaluation\n                              JOIN ai_model_evaluation_invalidations invalidation\n                                ON invalidation.evaluation_id =\n                                   evaluation.evaluation_id\n                             WHERE evaluation.candidate_id = candidate.candidate_id)\n                          THEN 'invalidated'\n                        WHEN EXISTS (\n                            SELECT 1 FROM ai_model_candidate_dispositions disposition\n                             WHERE disposition.candidate_id = candidate.candidate_id\n                               AND disposition.disposition =\n                                   'incompatible_runtime')\n                          THEN 'incompatible'\n                        WHEN EXISTS (\n                            SELECT 1 FROM ai_model_candidate_dispositions disposition\n                             WHERE disposition.candidate_id = candidate.candidate_id)\n                          THEN 'superseded'\n                        ELSE NULL\n                      END AS terminal_reason\n                 FROM ai_model_candidates candidate\n                WHERE NOT EXISTS (\n                    SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                     WHERE terminal.candidate_id = candidate.candidate_id)"
        )
        reconciled_count = 0
        async with self.transaction():
            for candidate_id, terminal_reason in rows:
                if terminal_reason:
                    reconciled_count += int(
                        await self._record_model_candidate_terminal_event(
                            str(candidate_id),
                            str(terminal_reason),
                            {
                                "candidate_id": str(candidate_id),
                                "terminal_reason": str(terminal_reason),
                                "source": "governance_reconciliation",
                            },
                        )
                    )
        return reconciled_count if True else None

    async def enforce_model_candidate_evidence_budget(
        self,
        candidate_id: str,
        *,
        additional_runs: int = 0,
        additional_predictions: int = 0,
        additional_actual_versions: int = 0,
        additional_payload_bytes: int = 0,
    ) -> bool:
        """Terminalize an inconclusive candidate before its hot evidence is unbounded."""
        additional_evidence = (
            additional_runs,
            additional_predictions,
            additional_actual_versions,
            additional_payload_bytes,
        )
        if any(
            (
                isinstance(field_value, bool)
                or not isinstance(field_value, int)
                or field_value < 0
                for field_value in additional_evidence
            )
        ):
            raise ValueError("evidence budget additions must be non-negative integers")
        terminal_event_row = await self.fetchone(
            "SELECT terminal_reason FROM ai_model_candidate_terminal_events\n                WHERE candidate_id = ?",
            (candidate_id,),
        )
        if terminal_event_row:
            return False
        artifact_hash_row = await self.fetchone(
            "SELECT created_at FROM ai_model_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        if not artifact_hash_row:
            raise ValueError("model candidate is unavailable")
        evidence_counts_row = await self.fetchone(
            "SELECT COUNT(DISTINCT run.shadow_run_id),\n                      COUNT(prediction.shadow_prediction_id),\n                      COALESCE(SUM(\n                          CASE WHEN run.payload_state = 'hot'\n                               THEN length(run.input_snapshot_blob)\n                                  + length(run.policy_snapshot_blob)\n                               ELSE 0 END), 0),\n                      COALESCE(SUM(\n                          CASE WHEN prediction.payload_state = 'hot'\n                               THEN length(prediction.champion_result_blob)\n                                  + length(prediction.candidate_result_blob)\n                               ELSE 0 END), 0)\n                 FROM ai_model_shadow_runs run\n                 LEFT JOIN ai_model_shadow_predictions prediction\n                   ON prediction.shadow_run_id = run.shadow_run_id\n                WHERE run.candidate_id = ?",
            (candidate_id,),
        )
        run_count = int(evidence_counts_row[0] or 0)
        prediction_count = int(evidence_counts_row[1] or 0)
        actual_version_count_row = await self.fetchone(
            "SELECT COUNT(actual.actual_version_id)\n                 FROM ai_model_shadow_runs run\n                 JOIN ai_model_shadow_predictions prediction\n                   ON prediction.shadow_run_id = run.shadow_run_id\n                 JOIN ai_model_shadow_actual_versions actual\n                   ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                WHERE run.candidate_id = ?",
            (candidate_id,),
        )
        actual_version_count = int(actual_version_count_row[0] or 0)
        snapshot_bytes_row = await self.fetchone(
            "SELECT COALESCE(SUM(length(input_snapshot_blob)\n                                     + length(policy_snapshot_blob)), 0)\n                 FROM ai_model_shadow_runs\n                WHERE candidate_id = ? AND payload_state = 'hot'",
            (candidate_id,),
        )
        payload_bytes = (
            int(snapshot_bytes_row[0] or 0)
            + int(evidence_counts_row[3] or 0)
            + int(additional_payload_bytes)
        )
        try:
            created_at = datetime.fromisoformat(str(artifact_hash_row[0]))
            current_time = SafeDateTimeUtil.now()
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=current_time.tzinfo)
            candidate_age = current_time - created_at
            age_days = (
                MODEL_CANDIDATE_EVIDENCE_MAX_AGE_DAYS
                if candidate_age < -MODEL_PROMOTION_ACTUAL_MEASUREMENT_CLOCK_SKEW
                else max(0, candidate_age.days)
            )
        except (TypeError, ValueError):
            age_days = MODEL_CANDIDATE_EVIDENCE_MAX_AGE_DAYS
        evidence_budget_exceeded = (
            age_days >= MODEL_CANDIDATE_EVIDENCE_MAX_AGE_DAYS
            or run_count + additional_runs > MODEL_CANDIDATE_EVIDENCE_MAX_RUNS
            or prediction_count + additional_predictions
            > MODEL_CANDIDATE_EVIDENCE_MAX_PREDICTIONS
            or (
                actual_version_count + additional_actual_versions
                > MODEL_CANDIDATE_EVIDENCE_MAX_ACTUAL_VERSIONS
            )
            or (payload_bytes > MODEL_CANDIDATE_EVIDENCE_MAX_BYTES)
        )
        if not evidence_budget_exceeded:
            return True
        async with self.transaction():
            await self._record_model_candidate_terminal_event(
                candidate_id,
                "evidence_timeout",
                {
                    "age_days": age_days,
                    "run_count": run_count,
                    "prediction_count": prediction_count,
                    "actual_version_count": actual_version_count,
                    "payload_bytes": payload_bytes,
                    "limits": {
                        "age_days": MODEL_CANDIDATE_EVIDENCE_MAX_AGE_DAYS,
                        "runs": MODEL_CANDIDATE_EVIDENCE_MAX_RUNS,
                        "predictions": MODEL_CANDIDATE_EVIDENCE_MAX_PREDICTIONS,
                        "actual_versions": MODEL_CANDIDATE_EVIDENCE_MAX_ACTUAL_VERSIONS,
                        "payload_bytes": MODEL_CANDIDATE_EVIDENCE_MAX_BYTES,
                    },
                },
            )
        _LOGGER.info(
            "Model candidate evidence window expired safely: candidate=%s age_days=%d runs=%d predictions=%d payload_bytes=%d",
            candidate_id,
            age_days,
            run_count,
            prediction_count,
            payload_bytes,
        )
        return False

    async def compact_terminal_model_candidate_evidence(self, limit: int = 2) -> int:
        """Replace terminal raw payloads with an immutable hash receipt."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("compaction limit must be positive")
        await self.reconcile_model_candidate_terminal_events()
        cooling_cutoff = (
            SafeDateTimeUtil.now()
            - timedelta(days=MODEL_CANDIDATE_RETENTION_COOLING_DAYS)
        ).isoformat()
        candidate_rows = await self.fetchall(
            "SELECT terminal.candidate_id, terminal.terminal_reason\n                 FROM ai_model_candidate_terminal_events terminal\n                 LEFT JOIN ai_active_model_pointer pointer ON pointer.id = 1\n                WHERE terminal.candidate_id !=\n                      COALESCE(pointer.active_candidate_id, '')\n                  AND julianday(terminal.terminal_at) <= julianday(?)\n                  AND terminal.candidate_id !=\n                      COALESCE(pointer.last_known_good_candidate_id, '')\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_shadow_retention_receipts receipt\n                       WHERE receipt.candidate_id = terminal.candidate_id)\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = terminal.candidate_id\n                         AND evaluation.decision = 'GO'\n                         AND NOT EXISTS (\n                             SELECT 1\n                               FROM ai_model_evaluation_invalidations invalidation\n                              WHERE invalidation.evaluation_id =\n                                    evaluation.evaluation_id)\n                         AND NOT EXISTS (\n                             SELECT 1 FROM ai_model_promotion_events promotion\n                              WHERE promotion.candidate_id =\n                                    terminal.candidate_id))\n                ORDER BY terminal.terminal_at, terminal.candidate_id\n                LIMIT ?",
            (cooling_cutoff, limit),
        )
        compacted_candidate_count = 0
        for candidate_id, terminal_reason in candidate_rows:
            async with self.transaction():
                shadow_run_rows = await self.fetchall(
                    "SELECT shadow_run_id, run_date, input_snapshot_json,\n                              input_snapshot_blob, input_snapshot_hash,\n                              policy_snapshot_json, policy_snapshot_blob,\n                              policy_snapshot_hash, snapshot_codec, payload_state\n                         FROM ai_model_shadow_runs\n                        WHERE candidate_id = ? ORDER BY shadow_run_id",
                    (candidate_id,),
                )
                predictions = await self.fetchall(
                    "SELECT prediction.shadow_prediction_id,\n                              prediction.champion_result_json,\n                              prediction.champion_result_blob,\n                              prediction.champion_result_hash,\n                              prediction.candidate_result_json,\n                              prediction.candidate_result_blob,\n                              prediction.candidate_result_hash,\n                              prediction.result_codec,\n                              prediction.payload_state\n                         FROM ai_model_shadow_predictions prediction\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                        ORDER BY prediction.shadow_prediction_id",
                    (candidate_id,),
                )
                hash_records = []
                payload_bytes = 0
                reclaimed_bytes = 0
                for row in shadow_run_rows:
                    if row[9] != "hot":
                        raise ValueError("terminal run payload state is inconsistent")
                    input_snapshot_text = decode_shadow_json(
                        row[2],
                        row[3],
                        row[8],
                        row[4],
                        "ai_model_shadow_runs",
                        protected_identity(row[0], "input_snapshot"),
                    )
                    policy_snapshot_text = decode_shadow_json(
                        row[5],
                        row[6],
                        row[8],
                        row[7],
                        "ai_model_shadow_runs",
                        protected_identity(row[0], "policy_snapshot"),
                    )
                    input_snapshot_payload = json.loads(input_snapshot_text)
                    policy_snapshot_payload = json.loads(policy_snapshot_text)
                    if (
                        canonical_stable_json(input_snapshot_payload)
                        != input_snapshot_text
                        or canonical_stable_json(policy_snapshot_payload)
                        != policy_snapshot_text
                    ):
                        raise ValueError("terminal run payload is not canonical")
                    payload_bytes += len(row[3] or b"") + len(row[6] or b"")
                    reclaimed_bytes += 2
                    hash_records.append(
                        {
                            "kind": "run",
                            "id": row[0],
                            "input_hash": row[4],
                            "policy_hash": row[7],
                        }
                    )
                    continue
                for row in predictions:
                    if row[8] != "hot":
                        raise ValueError(
                            "terminal prediction payload state is inconsistent"
                        )
                    champion_result_text = decode_shadow_json(
                        row[1], row[2], row[7], row[3]
                    )
                    candidate_result_text = decode_shadow_json(
                        row[4], row[5], row[7], row[6]
                    )
                    champion_result_payload = json.loads(champion_result_text)
                    candidate_result_payload = json.loads(candidate_result_text)
                    if (
                        canonical_stable_json(champion_result_payload)
                        != champion_result_text
                        or canonical_stable_json(candidate_result_payload)
                        != candidate_result_text
                    ):
                        raise ValueError("terminal prediction payload is not canonical")
                    payload_bytes += len(row[2] or b"") + len(row[5] or b"")
                    reclaimed_bytes += 2
                    hash_records.append(
                        {
                            "kind": "prediction",
                            "id": row[0],
                            "champion_hash": row[3],
                            "candidate_hash": row[6],
                        }
                    )
                    continue
                evidence_hash = canonical_stable_hash(hash_records)
                run_dates = [str(row[1]) for row in shadow_run_rows]
                actual_version_count_row = await self.fetchone(
                    "SELECT COUNT(actual.actual_version_id)\n                         FROM ai_model_shadow_actual_versions actual\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_prediction_id =\n                              actual.shadow_prediction_id\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?",
                    (candidate_id,),
                )
                actual_version_count = int(actual_version_count_row[0] or 0)
                retained_actual_count_row = await self.fetchone(
                    "SELECT COUNT(actual.actual_version_id)\n                         FROM ai_model_shadow_actual_versions actual\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_prediction_id =\n                              actual.shadow_prediction_id\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.actual_version_id =\n                                     actual.actual_version_id)",
                    (candidate_id,),
                )
                retained_prediction_count_row = await self.fetchone(
                    "SELECT COUNT(prediction.shadow_prediction_id)\n                         FROM ai_model_shadow_predictions prediction\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_prediction_id =\n                                     prediction.shadow_prediction_id)",
                    (candidate_id,),
                )
                retained_run_count_row = await self.fetchone(
                    "SELECT COUNT(run.shadow_run_id)\n                         FROM ai_model_shadow_runs run\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id = run.shadow_run_id)",
                    (candidate_id,),
                )
                retained_provenance_count_row = await self.fetchone(
                    "SELECT COUNT(provenance.shadow_run_id)\n                         FROM ai_model_shadow_run_provenance provenance\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = provenance.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id = run.shadow_run_id)",
                    (candidate_id,),
                )
                retained_actual_count = int(retained_actual_count_row[0] or 0)
                retained_prediction_count = int(retained_prediction_count_row[0] or 0)
                retained_run_count = int(retained_run_count_row[0] or 0)
                retained_provenance_count = int(retained_provenance_count_row[0] or 0)
                async with self._authorize_governance_write(
                    "retention", str(candidate_id)
                ):
                    await self.execute(
                        "INSERT INTO ai_model_shadow_retention_receipts\n                       (receipt_id, candidate_id, terminal_reason, policy_version,\n                        compacted_at, run_count, prediction_count,\n                        actual_version_count, payload_count, payload_bytes,\n                        deleted_run_count, deleted_prediction_count,\n                        deleted_actual_version_count, deleted_provenance_count,\n                        min_run_date, max_run_date, evidence_root_hash)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            candidate_id,
                            terminal_reason,
                            MODEL_CANDIDATE_RETENTION_POLICY_VERSION,
                            SafeDateTimeUtil.now().isoformat(),
                            len(shadow_run_rows),
                            len(predictions),
                            actual_version_count,
                            reclaimed_bytes,
                            payload_bytes,
                            retained_run_count,
                            retained_prediction_count,
                            retained_actual_count,
                            retained_provenance_count,
                            min(run_dates) if run_dates else None,
                            max(run_dates) if run_dates else None,
                            evidence_hash,
                        ),
                        auto_commit=False,
                    )
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_runs_append_only_update",
                    auto_commit=False,
                )
                await self.execute(
                    "DROP TRIGGER IF EXISTS\n                       ai_model_shadow_predictions_append_only_update",
                    auto_commit=False,
                )
                for trigger_name in (
                    "ai_model_shadow_actual_versions_append_only_delete",
                    "ai_model_shadow_predictions_append_only_delete",
                    "ai_model_shadow_run_provenance_append_only_delete",
                    "ai_model_shadow_runs_append_only_delete",
                ):
                    await self.execute(
                        f"DROP TRIGGER IF EXISTS {trigger_name}", auto_commit=False
                    )
                await self.execute(
                    "DELETE FROM ai_model_shadow_actual_versions\n                        WHERE shadow_prediction_id IN (\n                            SELECT prediction.shadow_prediction_id\n                              FROM ai_model_shadow_predictions prediction\n                              JOIN ai_model_shadow_runs run\n                                ON run.shadow_run_id = prediction.shadow_run_id\n                             WHERE run.candidate_id = ?)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.actual_version_id =\n                                     ai_model_shadow_actual_versions.actual_version_id)",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_predictions\n                        WHERE shadow_run_id IN (\n                            SELECT shadow_run_id FROM ai_model_shadow_runs\n                             WHERE candidate_id = ?)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_prediction_id =\n                                     ai_model_shadow_predictions.shadow_prediction_id)",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_run_provenance\n                        WHERE shadow_run_id IN (\n                            SELECT run.shadow_run_id FROM ai_model_shadow_runs run\n                             WHERE run.candidate_id = ?\n                               AND NOT EXISTS (\n                                   SELECT 1\n                                     FROM ai_model_evaluation_samples_v2 sample\n                                    WHERE sample.shadow_run_id = run.shadow_run_id))",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_runs\n                        WHERE candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id =\n                                     ai_model_shadow_runs.shadow_run_id)",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE ai_model_shadow_predictions\n                          SET champion_result_blob = NULL,\n                              candidate_result_blob = NULL,\n                              payload_state = 'compacted'\n                        WHERE shadow_run_id IN (\n                            SELECT shadow_run_id FROM ai_model_shadow_runs\n                             WHERE candidate_id = ?)",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE ai_model_shadow_runs\n                          SET input_snapshot_blob = NULL,\n                              policy_snapshot_blob = NULL,\n                              payload_state = 'compacted'\n                        WHERE candidate_id = ?",
                    (candidate_id,),
                    auto_commit=False,
                )
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_runs_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_runs\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_runs is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_predictions_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_predictions\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_predictions is append-only'); END",
                    auto_commit=False,
                )
                for table_name in (
                    "ai_model_shadow_actual_versions",
                    "ai_model_shadow_predictions",
                    "ai_model_shadow_run_provenance",
                    "ai_model_shadow_runs",
                ):
                    await self.execute(
                        f"CREATE TRIGGER {table_name}_append_only_delete\n                            BEFORE DELETE ON {table_name}\n                            BEGIN SELECT RAISE(ABORT,\n                                '{table_name} is append-only'); END",
                        auto_commit=False,
                    )
            compacted_candidate_count += 1
            _LOGGER.info(
                "Terminal model candidate evidence compacted: candidate=%s runs=%d predictions=%d payload_bytes=%d root=%s",
                candidate_id,
                len(shadow_run_rows),
                len(predictions),
                payload_bytes,
                evidence_hash,
            )
            continue
        if True:
            return compacted_candidate_count

    async def mark_scheduled_task_attempt(
        self, task_key: str, attempted_at: str
    ) -> None:
        """Persist the latest attempt without changing completion state."""
        await self.execute(
            "INSERT INTO scheduled_task_state (task_key, last_attempt_at)\n               VALUES (?, ?)\n               ON CONFLICT(task_key) DO UPDATE SET\n                   last_attempt_at = excluded.last_attempt_at",
            (task_key, attempted_at),
        )

    async def mark_scheduled_task_completed(
        self, task_key: str, period: str, completed_at: str
    ) -> None:
        """Persist successful completion of a scheduled task period."""
        await self.execute(
            "INSERT INTO scheduled_task_state\n                   (task_key, completed_period, completed_at, last_attempt_at)\n               VALUES (?, ?, ?, ?)\n               ON CONFLICT(task_key) DO UPDATE SET\n                   completed_period = excluded.completed_period,\n                   completed_at = excluded.completed_at,\n                   last_attempt_at = excluded.last_attempt_at",
            (task_key, period, completed_at, completed_at),
        )

    @_atomic_database_write
    async def save_production_time_state(self, state: Dict[str, Any]) -> None:
        """Save production time state."""
        await self.execute(
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
        await self.commit()

    @_atomic_database_write
    async def save_panel_group_sensor_state(self, state: Dict[str, Any]) -> None:
        """Save panel group sensor state."""
        for group_name, last_value in state["last_values"].items():
            await self.execute(
                "INSERT INTO panel_group_sensor_state (group_name, last_value, last_updated)\n                   VALUES (?, ?, ?)\n                   ON CONFLICT(group_name) DO UPDATE SET\n                       last_value = excluded.last_value,\n                       last_updated = excluded.last_updated",
                (group_name, last_value, _safe_dt(state.get("last_updated"))),
            )
        await self.commit()

    async def save_actual_live_state(
        self, state: Dict[str, Any], panel_groups: Dict[str, Dict[str, Any]]
    ) -> None:
        """Save SFML-owned live actual and group power state."""
        async with self.transaction():
            await self.execute(
                "INSERT INTO actual_live_state\n                   (date, total_power_w, total_actual_kwh, power_source, actual_source,\n                    groups_expected, groups_available, quality, updated_at)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date) DO UPDATE SET\n                       total_power_w = excluded.total_power_w,\n                       total_actual_kwh = excluded.total_actual_kwh,\n                       power_source = excluded.power_source,\n                       actual_source = excluded.actual_source,\n                       groups_expected = excluded.groups_expected,\n                       groups_available = excluded.groups_available,\n                       quality = excluded.quality,\n                       updated_at = excluded.updated_at",
                (
                    _safe_date(state.get("date")),
                    state.get("total_power_w"),
                    state.get("total_actual_kwh", 0.0),
                    state.get("power_source"),
                    state.get("actual_source", "hourly_predictions"),
                    state.get("groups_expected", 0),
                    state.get("groups_available", 0),
                    state.get("quality", "unknown"),
                    _safe_dt(state.get("updated_at")),
                ),
                auto_commit=False,
            )
            await self.execute(
                "DELETE FROM actual_live_panel_groups WHERE date = ?",
                (_safe_date(state.get("date")),),
                auto_commit=False,
            )
            for group_name, group_state in panel_groups.items():
                await self.execute(
                    "INSERT INTO actual_live_panel_groups\n                       (date, group_name, power_w, actual_today_kwh, power_sensor,\n                        quality, updated_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _safe_date(state.get("date")),
                        group_name,
                        group_state.get("power_w"),
                        group_state.get("actual_today_kwh", 0.0),
                        group_state.get("power_sensor"),
                        group_state.get("quality", "unknown"),
                        _safe_dt(state.get("updated_at")),
                    ),
                    auto_commit=False,
                )
        return None if True else None

    async def get_actual_live_state(self, date_str: str) -> Optional[Dict[str, Any]]:
        """Get SFML-owned live actual state for a date."""
        state_row = await self.fetchone(
            "SELECT date, total_power_w, total_actual_kwh, power_source,\n                      actual_source, groups_expected, groups_available, quality, updated_at\n               FROM actual_live_state\n               WHERE date = ?",
            (date_str,),
        )
        return (
            None
            if not state_row
            else {
                "date": str(state_row["date"]),
                "total_power_w": state_row["total_power_w"],
                "total_actual_kwh": state_row["total_actual_kwh"],
                "power_source": state_row["power_source"],
                "actual_source": state_row["actual_source"],
                "groups_expected": state_row["groups_expected"],
                "groups_available": state_row["groups_available"],
                "quality": state_row["quality"],
                "updated_at": (
                    str(state_row["updated_at"]) if state_row["updated_at"] else None
                ),
            }
        )

    async def get_actual_live_panel_groups(
        self, date_str: str
    ) -> Dict[str, Dict[str, Any]]:
        """Get SFML-owned live panel group state for a date."""
        group_state_rows = await self.fetchall(
            "SELECT group_name, power_w, actual_today_kwh, power_sensor, quality, updated_at\n               FROM actual_live_panel_groups\n               WHERE date = ?\n               ORDER BY group_name",
            (date_str,),
        )
        return {
            str(group_row["group_name"]): {
                "power_w": group_row["power_w"],
                "actual_today_kwh": group_row["actual_today_kwh"],
                "power_sensor": group_row["power_sensor"],
                "quality": group_row["quality"],
                "updated_at": (
                    str(group_row["updated_at"]) if group_row["updated_at"] else None
                ),
            }
            for group_row in group_state_rows
        }

    async def build_actual_live_state_from_hourly(
        self, date_str: str
    ) -> Dict[str, Any]:
        """Build current SFML actual totals from persisted hourly truth."""
        async with self.transaction():
            total_actual_row = await self.fetchone(
                "SELECT COALESCE(SUM(actual_kwh), 0.0) AS total_actual_kwh,\n                          COUNT(actual_kwh) AS actual_hours\n                   FROM hourly_predictions\n                   WHERE target_date = ?",
                (date_str,),
            )
            group_actual_rows = await self.fetchall(
                "SELECT ppg.group_name,\n                          COALESCE(SUM(ppg.actual_kwh), 0.0) AS actual_today_kwh,\n                          COUNT(ppg.actual_kwh) AS actual_hours\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                   WHERE hp.target_date = ?\n                   GROUP BY ppg.group_name\n                   ORDER BY ppg.group_name",
                (date_str,),
            )
            return {
                "total_actual_kwh": (
                    float(total_actual_row["total_actual_kwh"] or 0.0)
                    if total_actual_row
                    else 0.0
                ),
                "actual_hours": (
                    int(total_actual_row["actual_hours"] or 0)
                    if total_actual_row
                    else 0
                ),
                "panel_groups": {
                    str(group_row["group_name"]): {
                        "actual_today_kwh": float(group_row["actual_today_kwh"] or 0.0),
                        "actual_hours": int(group_row["actual_hours"] or 0),
                    }
                    for group_row in group_actual_rows
                },
            }
        return None if True else None

    async def clear_conservative_planning_tracking(
        self, forecast_date: Optional[str] = None, *, reason: str = "date_rollover"
    ) -> None:
        rows = await self.fetchall("PRAGMA table_info(daily_forecast_tracking)")
        existing_columns = {row[1] for row in rows}
        column_definitions = {
            "conservative_planning_forecast_kwh",
            "conservative_planning_forecast_hours_json",
            "conservative_planning_forecast_updated_at",
            "conservative_planning_forecast_group_totals_json",
            "conservative_planning_forecast_panel_groups_json",
        }
        if not column_definitions.issubset(existing_columns):
            return
        tracking_where_clause = "WHERE id = 1"
        search_parameters = ()
        if forecast_date:
            tracking_where_clause += " AND date = ?"
            search_parameters = (forecast_date,)
        await self.execute(
            f"UPDATE daily_forecast_tracking\n                SET conservative_planning_forecast_kwh = NULL,\n                    conservative_planning_forecast_updated_at = NULL,\n                    conservative_planning_forecast_hours_json = NULL,\n                    conservative_planning_forecast_panel_groups_json = NULL,\n                    conservative_planning_forecast_group_totals_json = NULL\n                {tracking_where_clause}",
            search_parameters,
        )
        _LOGGER.info(
            "Cleared conservative planning tracking fields: date=%s reason=%s",
            forecast_date,
            reason,
        )

    async def _merge_daily_forecast_tracking_patch(
        self, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        existing_tracking = await self.get_daily_forecast_tracking()
        if not existing_tracking:
            return data
        patch_date = data.get("date")
        existing_date = existing_tracking.get("date")
        if patch_date and existing_date and (str(patch_date) != str(existing_date)):
            return data
        merged_tracking = dict(existing_tracking)
        merged_tracking["date"] = patch_date or existing_date
        for field_name, field_value in data.items():
            if field_name == "date":
                continue
            if isinstance(field_value, dict):
                if field_value:
                    existing_field_value = (
                        existing_tracking.get(field_name)
                        if isinstance(existing_tracking.get(field_name), dict)
                        else {}
                    )
                    merged_tracking[field_name] = {
                        **existing_field_value,
                        **field_value,
                    }
                elif field_name not in merged_tracking:
                    merged_tracking[field_name] = field_value
            elif field_value is not None:
                merged_tracking[field_name] = field_value
        return merged_tracking

    @_atomic_database_write
    async def save_daily_forecast_tracking(self, data: Dict[str, Any]) -> None:
        """Save daily forecast tracking data."""
        existing_tracking = await self.get_daily_forecast_tracking()
        requested_date = data.get("date")
        existing_date = existing_tracking.get("date") if existing_tracking else None
        date_rolled_over = bool(
            requested_date
            and existing_date
            and (str(requested_date) != str(existing_date))
        )
        data = await self._merge_daily_forecast_tracking_patch(data)
        await self.execute(
            "INSERT INTO daily_forecast_tracking\n               (id, date, forecast_best_hour, forecast_best_hour_kwh, forecast_best_hour_locked,\n                forecast_best_hour_locked_at, forecast_best_hour_source,\n                actual_best_hour, actual_best_hour_kwh, actual_best_hour_saved_at,\n                forecast_next_hour_period, forecast_next_hour_kwh, forecast_next_hour_updated_at,\n                forecast_next_hour_source, production_time_active, production_time_duration_seconds,\n                production_time_start, production_time_end, production_time_last_power_above_10w,\n                production_time_zero_power_since, peak_today_power_w, peak_today_at,\n                yield_today_kwh, yield_today_sensor, consumption_today_kwh, consumption_today_sensor,\n                autarky_percent, autarky_calculated_at, finalized_yield_kwh, finalized_consumption_kwh,\n                finalized_production_hours, finalized_accuracy_percent, finalized_excluded_hours_count,\n                finalized_excluded_hours_total, finalized_excluded_hours_ratio,\n                finalized_excluded_hours_reasons, finalized_at, last_updated)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                    date = excluded.date,\n                    forecast_best_hour = COALESCE(excluded.forecast_best_hour, daily_forecast_tracking.forecast_best_hour),\n                    forecast_best_hour_kwh = COALESCE(excluded.forecast_best_hour_kwh, daily_forecast_tracking.forecast_best_hour_kwh),\n                    forecast_best_hour_locked = COALESCE(excluded.forecast_best_hour_locked, daily_forecast_tracking.forecast_best_hour_locked),\n                    forecast_best_hour_locked_at = COALESCE(excluded.forecast_best_hour_locked_at, daily_forecast_tracking.forecast_best_hour_locked_at),\n                    forecast_best_hour_source = COALESCE(excluded.forecast_best_hour_source, daily_forecast_tracking.forecast_best_hour_source),\n                    actual_best_hour = COALESCE(excluded.actual_best_hour, daily_forecast_tracking.actual_best_hour),\n                    actual_best_hour_kwh = COALESCE(excluded.actual_best_hour_kwh, daily_forecast_tracking.actual_best_hour_kwh),\n                    actual_best_hour_saved_at = COALESCE(excluded.actual_best_hour_saved_at, daily_forecast_tracking.actual_best_hour_saved_at),\n                    forecast_next_hour_period = COALESCE(excluded.forecast_next_hour_period, daily_forecast_tracking.forecast_next_hour_period),\n                    forecast_next_hour_kwh = COALESCE(excluded.forecast_next_hour_kwh, daily_forecast_tracking.forecast_next_hour_kwh),\n                    forecast_next_hour_updated_at = COALESCE(excluded.forecast_next_hour_updated_at, daily_forecast_tracking.forecast_next_hour_updated_at),\n                    forecast_next_hour_source = COALESCE(excluded.forecast_next_hour_source, daily_forecast_tracking.forecast_next_hour_source),\n                    production_time_active = COALESCE(excluded.production_time_active, daily_forecast_tracking.production_time_active),\n                    production_time_duration_seconds = COALESCE(excluded.production_time_duration_seconds, daily_forecast_tracking.production_time_duration_seconds),\n                    production_time_start = COALESCE(excluded.production_time_start, daily_forecast_tracking.production_time_start),\n                    production_time_end = COALESCE(excluded.production_time_end, daily_forecast_tracking.production_time_end),\n                    production_time_last_power_above_10w = COALESCE(excluded.production_time_last_power_above_10w, daily_forecast_tracking.production_time_last_power_above_10w),\n                    production_time_zero_power_since = COALESCE(excluded.production_time_zero_power_since, daily_forecast_tracking.production_time_zero_power_since),\n                    peak_today_power_w = COALESCE(excluded.peak_today_power_w, daily_forecast_tracking.peak_today_power_w),\n                    peak_today_at = COALESCE(excluded.peak_today_at, daily_forecast_tracking.peak_today_at),\n                    yield_today_kwh = COALESCE(excluded.yield_today_kwh, daily_forecast_tracking.yield_today_kwh),\n                    yield_today_sensor = COALESCE(excluded.yield_today_sensor, daily_forecast_tracking.yield_today_sensor),\n                    consumption_today_kwh = COALESCE(excluded.consumption_today_kwh, daily_forecast_tracking.consumption_today_kwh),\n                    consumption_today_sensor = COALESCE(excluded.consumption_today_sensor, daily_forecast_tracking.consumption_today_sensor),\n                    autarky_percent = COALESCE(excluded.autarky_percent, daily_forecast_tracking.autarky_percent),\n                    autarky_calculated_at = COALESCE(excluded.autarky_calculated_at, daily_forecast_tracking.autarky_calculated_at),\n                    finalized_yield_kwh = COALESCE(excluded.finalized_yield_kwh, daily_forecast_tracking.finalized_yield_kwh),\n                    finalized_consumption_kwh = COALESCE(excluded.finalized_consumption_kwh, daily_forecast_tracking.finalized_consumption_kwh),\n                    finalized_production_hours = COALESCE(excluded.finalized_production_hours, daily_forecast_tracking.finalized_production_hours),\n                    finalized_accuracy_percent = COALESCE(excluded.finalized_accuracy_percent, daily_forecast_tracking.finalized_accuracy_percent),\n                    finalized_excluded_hours_count = COALESCE(excluded.finalized_excluded_hours_count, daily_forecast_tracking.finalized_excluded_hours_count),\n                    finalized_excluded_hours_total = COALESCE(excluded.finalized_excluded_hours_total, daily_forecast_tracking.finalized_excluded_hours_total),\n                    finalized_excluded_hours_ratio = COALESCE(excluded.finalized_excluded_hours_ratio, daily_forecast_tracking.finalized_excluded_hours_ratio),\n                    finalized_excluded_hours_reasons = COALESCE(excluded.finalized_excluded_hours_reasons, daily_forecast_tracking.finalized_excluded_hours_reasons),\n                    finalized_at = COALESCE(excluded.finalized_at, daily_forecast_tracking.finalized_at),\n                    last_updated = excluded.last_updated",
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
                SafeDateTimeUtil.now(),
            ),
        )
        await self.commit()
        if date_rolled_over:
            await self.clear_conservative_planning_tracking(
                str(requested_date), reason="daily_forecast_tracking_date_rollover"
            )
        return None if True else None

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

    @_atomic_database_write
    async def save_daily_statistics(self, data: Dict[str, Any]) -> None:
        """Save daily statistics data."""
        await self.execute(
            "INSERT INTO daily_statistics\n               (id, all_time_peak_power_w, all_time_peak_date, all_time_peak_at,\n                current_week_period, current_week_date_range, current_week_yield_kwh,\n                current_week_consumption_kwh, current_week_days, current_week_updated_at,\n                current_month_period, current_month_yield_kwh, current_month_consumption_kwh,\n                current_month_avg_autarky, current_month_days, current_month_updated_at,\n                last_7_days_avg_yield_kwh, last_7_days_avg_accuracy,\n                last_7_days_avg_evaluation_coverage_percent, last_7_days_avg_excluded_mppt_hours,\n                last_7_days_total_yield_kwh, last_7_days_calculated_at,\n                last_30_days_avg_yield_kwh, last_30_days_avg_accuracy,\n                last_30_days_avg_evaluation_coverage_percent, last_30_days_avg_excluded_mppt_hours,\n                last_30_days_total_yield_kwh, last_30_days_calculated_at, last_365_days_avg_yield_kwh,\n                last_365_days_total_yield_kwh, last_365_days_calculated_at, last_updated)\n               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   all_time_peak_power_w = excluded.all_time_peak_power_w,\n                   all_time_peak_date = excluded.all_time_peak_date,\n                   all_time_peak_at = excluded.all_time_peak_at,\n                   current_week_period = excluded.current_week_period,\n                   current_week_date_range = excluded.current_week_date_range,\n                   current_week_yield_kwh = excluded.current_week_yield_kwh,\n                   current_week_consumption_kwh = excluded.current_week_consumption_kwh,\n                   current_week_days = excluded.current_week_days,\n                   current_week_updated_at = excluded.current_week_updated_at,\n                   current_month_period = excluded.current_month_period,\n                   current_month_yield_kwh = excluded.current_month_yield_kwh,\n                   current_month_consumption_kwh = excluded.current_month_consumption_kwh,\n                   current_month_avg_autarky = excluded.current_month_avg_autarky,\n                   current_month_days = excluded.current_month_days,\n                   current_month_updated_at = excluded.current_month_updated_at,\n                   last_7_days_avg_yield_kwh = excluded.last_7_days_avg_yield_kwh,\n                   last_7_days_avg_accuracy = excluded.last_7_days_avg_accuracy,\n                   last_7_days_avg_evaluation_coverage_percent = excluded.last_7_days_avg_evaluation_coverage_percent,\n                   last_7_days_avg_excluded_mppt_hours = excluded.last_7_days_avg_excluded_mppt_hours,\n                   last_7_days_total_yield_kwh = excluded.last_7_days_total_yield_kwh,\n                   last_7_days_calculated_at = excluded.last_7_days_calculated_at,\n                   last_30_days_avg_yield_kwh = excluded.last_30_days_avg_yield_kwh,\n                   last_30_days_avg_accuracy = excluded.last_30_days_avg_accuracy,\n                   last_30_days_avg_evaluation_coverage_percent = excluded.last_30_days_avg_evaluation_coverage_percent,\n                   last_30_days_avg_excluded_mppt_hours = excluded.last_30_days_avg_excluded_mppt_hours,\n                   last_30_days_total_yield_kwh = excluded.last_30_days_total_yield_kwh,\n                   last_30_days_calculated_at = excluded.last_30_days_calculated_at,\n                   last_365_days_avg_yield_kwh = excluded.last_365_days_avg_yield_kwh,\n                   last_365_days_total_yield_kwh = excluded.last_365_days_total_yield_kwh,\n                   last_365_days_calculated_at = excluded.last_365_days_calculated_at,\n                   last_updated = excluded.last_updated",
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
                data.get("last_7_days", {}).get("avg_evaluation_coverage_percent"),
                data.get("last_7_days", {}).get("avg_excluded_mppt_hours"),
                data.get("last_7_days", {}).get("total_yield_kwh"),
                data.get("last_7_days", {}).get("calculated_at"),
                data.get("last_30_days", {}).get("avg_yield_kwh"),
                data.get("last_30_days", {}).get("avg_accuracy"),
                data.get("last_30_days", {}).get("avg_evaluation_coverage_percent"),
                data.get("last_30_days", {}).get("avg_excluded_mppt_hours"),
                data.get("last_30_days", {}).get("total_yield_kwh"),
                data.get("last_30_days", {}).get("calculated_at"),
                data.get("last_365_days", {}).get("avg_yield_kwh"),
                data.get("last_365_days", {}).get("total_yield_kwh"),
                data.get("last_365_days", {}).get("calculated_at"),
                SafeDateTimeUtil.now(),
            ),
        )
        await self.commit()

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
                        "avg_evaluation_coverage_percent": row[
                            "last_7_days_avg_evaluation_coverage_percent"
                        ],
                        "avg_excluded_mppt_hours": row[
                            "last_7_days_avg_excluded_mppt_hours"
                        ],
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
                        "avg_evaluation_coverage_percent": row[
                            "last_30_days_avg_evaluation_coverage_percent"
                        ],
                        "avg_excluded_mppt_hours": row[
                            "last_30_days_avg_excluded_mppt_hours"
                        ],
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

    @_atomic_database_write
    async def save_forecast_history_entry(self, entry: Dict[str, Any]) -> None:
        """Save a single forecast history entry."""
        calibration_date = _safe_date(entry.get("date"))
        await self.execute(
            "INSERT INTO forecast_history\n               (date, predicted_kwh, actual_kwh, consumption_kwh, autarky, accuracy,\n                production_hours, peak_power, source, excluded_hours_count, excluded_hours_total,\n                excluded_hours_ratio, excluded_hours_reasons, archived_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(date) DO UPDATE SET\n                   predicted_kwh = excluded.predicted_kwh,\n                   actual_kwh = excluded.actual_kwh,\n                   consumption_kwh = excluded.consumption_kwh,\n                   autarky = excluded.autarky,\n                   accuracy = excluded.accuracy,\n                   production_hours = excluded.production_hours,\n                   peak_power = excluded.peak_power,\n                   source = excluded.source,\n                   excluded_hours_count = excluded.excluded_hours_count,\n                   excluded_hours_total = excluded.excluded_hours_total,\n                   excluded_hours_ratio = excluded.excluded_hours_ratio,\n                   excluded_hours_reasons = excluded.excluded_hours_reasons,\n                   archived_at = excluded.archived_at",
            (
                calibration_date,
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
        await self.commit()

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

    @_atomic_database_write
    async def save_hourly_shadow_detection(
        self, prediction_id: str, shadow_data: Dict[str, Any]
    ) -> None:
        """Save shadow detection data for an hourly prediction."""
        method_results = shadow_data.get("methods") or {}
        theory_ratio_result = method_results.get("theory_ratio") or {}
        sensor_fusion_result = method_results.get("sensor_fusion") or {}
        lux_analysis = sensor_fusion_result.get("lux_analysis") or {}
        shadow_percent = _validated_shadow_percentage(
            shadow_data.get("shadow_percent"), "shadow_percent", required=True
        )
        theory_shadow_percent = _validated_shadow_percentage(
            theory_ratio_result.get("shadow_percent"), "theory_ratio.shadow_percent"
        )
        fusion_shadow_percent = _validated_shadow_percentage(
            sensor_fusion_result.get("shadow_percent"), "sensor_fusion.shadow_percent"
        )
        lux_shadow_percent = _validated_shadow_percentage(
            lux_analysis.get("shadow_percent"),
            "sensor_fusion.lux_analysis.shadow_percent",
        )
        irradiance_shadow_percent = _validated_shadow_percentage(
            sensor_fusion_result.get("irradiance_shadow_percent"),
            "sensor_fusion.irradiance_shadow_percent",
        )
        await self.execute(
            "INSERT INTO hourly_shadow_detection\n               (prediction_id, method, ensemble_mode, shadow_type, shadow_percent, confidence,\n                root_cause, fusion_mode, efficiency_ratio, loss_kwh, theoretical_max_kwh,\n                interpretation, theory_ratio_shadow_type, theory_ratio_shadow_percent,\n                theory_ratio_confidence, theory_ratio_efficiency_ratio, theory_ratio_clear_sky_wm2,\n                theory_ratio_actual_wm2, theory_ratio_loss_kwh, theory_ratio_root_cause,\n                sensor_fusion_shadow_type, sensor_fusion_shadow_percent, sensor_fusion_confidence,\n                sensor_fusion_efficiency_ratio, sensor_fusion_loss_kwh, sensor_fusion_root_cause,\n                sensor_fusion_lux_factor, sensor_fusion_lux_shadow_percent,\n                sensor_fusion_irradiance_factor, sensor_fusion_irradiance_shadow_percent,\n                weight_theory_ratio, weight_sensor_fusion, evaluable,\n                learning_eligible, observation_class)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   method = excluded.method,\n                   ensemble_mode = excluded.ensemble_mode,\n                   shadow_type = excluded.shadow_type,\n                   shadow_percent = excluded.shadow_percent,\n                   confidence = excluded.confidence,\n                   root_cause = excluded.root_cause,\n                   fusion_mode = excluded.fusion_mode,\n                   efficiency_ratio = excluded.efficiency_ratio,\n                   loss_kwh = excluded.loss_kwh,\n                   theoretical_max_kwh = excluded.theoretical_max_kwh,\n                   interpretation = excluded.interpretation,\n                   theory_ratio_shadow_type = excluded.theory_ratio_shadow_type,\n                   theory_ratio_shadow_percent = excluded.theory_ratio_shadow_percent,\n                   theory_ratio_confidence = excluded.theory_ratio_confidence,\n                   theory_ratio_efficiency_ratio = excluded.theory_ratio_efficiency_ratio,\n                   theory_ratio_clear_sky_wm2 = excluded.theory_ratio_clear_sky_wm2,\n                   theory_ratio_actual_wm2 = excluded.theory_ratio_actual_wm2,\n                   theory_ratio_loss_kwh = excluded.theory_ratio_loss_kwh,\n                   theory_ratio_root_cause = excluded.theory_ratio_root_cause,\n                   sensor_fusion_shadow_type = excluded.sensor_fusion_shadow_type,\n                   sensor_fusion_shadow_percent = excluded.sensor_fusion_shadow_percent,\n                   sensor_fusion_confidence = excluded.sensor_fusion_confidence,\n                   sensor_fusion_efficiency_ratio = excluded.sensor_fusion_efficiency_ratio,\n                   sensor_fusion_loss_kwh = excluded.sensor_fusion_loss_kwh,\n                   sensor_fusion_root_cause = excluded.sensor_fusion_root_cause,\n                   sensor_fusion_lux_factor = excluded.sensor_fusion_lux_factor,\n                   sensor_fusion_lux_shadow_percent = excluded.sensor_fusion_lux_shadow_percent,\n                   sensor_fusion_irradiance_factor = excluded.sensor_fusion_irradiance_factor,\n                   sensor_fusion_irradiance_shadow_percent = excluded.sensor_fusion_irradiance_shadow_percent,\n                   weight_theory_ratio = excluded.weight_theory_ratio,\n                   weight_sensor_fusion = excluded.weight_sensor_fusion,\n                   evaluable = excluded.evaluable,\n                   learning_eligible = excluded.learning_eligible,\n                   observation_class = excluded.observation_class",
            (
                prediction_id,
                shadow_data.get("method"),
                shadow_data.get("ensemble_mode"),
                shadow_data.get("shadow_type"),
                shadow_percent,
                shadow_data.get("confidence"),
                shadow_data.get("root_cause"),
                shadow_data.get("fusion_mode"),
                shadow_data.get("efficiency_ratio"),
                shadow_data.get("loss_kwh"),
                shadow_data.get("theoretical_max_kwh"),
                shadow_data.get("interpretation"),
                theory_ratio_result.get("shadow_type"),
                theory_shadow_percent,
                theory_ratio_result.get("confidence"),
                theory_ratio_result.get("efficiency_ratio"),
                theory_ratio_result.get("clear_sky_wm2"),
                theory_ratio_result.get("actual_wm2"),
                theory_ratio_result.get("loss_kwh"),
                theory_ratio_result.get("root_cause"),
                sensor_fusion_result.get("shadow_type"),
                fusion_shadow_percent,
                sensor_fusion_result.get("confidence"),
                sensor_fusion_result.get("efficiency_ratio"),
                sensor_fusion_result.get("loss_kwh"),
                sensor_fusion_result.get("root_cause"),
                lux_analysis.get("factor"),
                lux_shadow_percent,
                sensor_fusion_result.get("irradiance_factor"),
                irradiance_shadow_percent,
                shadow_data.get("weights", {}).get("theory_ratio"),
                shadow_data.get("weights", {}).get("sensor_fusion"),
                1 if shadow_data.get("evaluable", False) else 0,
                1 if shadow_data.get("learning_eligible", False) else 0,
                shadow_data.get("observation_class"),
            ),
        )
        await self.commit()

    @_atomic_database_write
    async def save_hourly_production_metrics(
        self, prediction_id: str, metrics: Dict[str, Any]
    ) -> None:
        """Save production metrics for an hourly prediction."""
        await self.execute(
            "INSERT INTO hourly_production_metrics (prediction_id, peak_power_today_kwh, production_hours_today, cumulative_today_kwh)\n               VALUES (?, ?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   peak_power_today_kwh = excluded.peak_power_today_kwh,\n                   production_hours_today = excluded.production_hours_today,\n                   cumulative_today_kwh = excluded.cumulative_today_kwh",
            (
                prediction_id,
                metrics.get("peak_power_today_kwh"),
                metrics.get("production_hours_today"),
                metrics.get("cumulative_today_kwh"),
            ),
        )
        await self.commit()

    @_atomic_database_write
    async def save_hourly_historical_context(
        self, prediction_id: str, context: Dict[str, Any]
    ) -> None:
        """Save historical context for an hourly prediction."""
        await self.execute(
            "INSERT INTO hourly_historical_context (prediction_id, yesterday_same_hour, same_hour_avg_7days)\n               VALUES (?, ?, ?)\n               ON CONFLICT(prediction_id) DO UPDATE SET\n                   yesterday_same_hour = excluded.yesterday_same_hour,\n                   same_hour_avg_7days = excluded.same_hour_avg_7days",
            (
                prediction_id,
                context.get("yesterday_same_hour"),
                context.get("same_hour_avg_7days"),
            ),
        )
        await self.commit()

    @_atomic_database_write
    async def save_hourly_panel_group_accuracy(
        self, prediction_id: str, group_name: str, accuracy_data: Dict[str, Any]
    ) -> None:
        """Save panel group accuracy data for an hourly prediction."""
        await self.execute(
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
        await self.commit()

    @_atomic_database_write
    async def save_daily_patterns(
        self, date_val: date, patterns: List[Dict[str, Any]]
    ) -> None:
        """Save patterns for a daily summary."""
        await self.execute("DELETE FROM daily_patterns WHERE date = ?", (date_val,))
        for table_reference_pattern in patterns:
            await self.execute(
                "INSERT INTO daily_patterns\n                   (date, pattern_type, hours, severity, avg_error_percent, confidence,\n                    first_detected, occurrence_count, seasonal)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    date_val,
                    table_reference_pattern.get("type"),
                    json.dumps(table_reference_pattern.get("hours", [])),
                    table_reference_pattern.get("severity"),
                    table_reference_pattern.get("avg_error_percent"),
                    table_reference_pattern.get("confidence"),
                    (
                        _safe_dt(table_reference_pattern.get("first_detected"))
                        if table_reference_pattern.get("first_detected")
                        else None
                    ),
                    table_reference_pattern.get("occurrence_count"),
                    table_reference_pattern.get("seasonal", False),
                ),
            )
        await self.commit()

    @_atomic_database_write
    async def save_daily_recommendations(
        self, date_val: date, recommendations: List[Dict[str, Any]]
    ) -> None:
        """Save recommendations for a daily summary."""
        await self.execute(
            "DELETE FROM daily_recommendations WHERE date = ?", (date_val,)
        )
        for recommendation in recommendations:
            await self.execute(
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
        await self.commit()

    @_atomic_database_write
    async def save_snow_tracking_group(
        self, group_name: str, data: Dict[str, Any]
    ) -> None:
        """Upsert per-group snow tracking state."""
        await self.execute(
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
        await self.commit()

    async def load_snow_tracking_groups(self) -> Dict[str, Dict[str, Any]]:
        """Load all per-group snow tracking states."""
        states_by_group = {}
        rows = await self.fetchall(
            "SELECT group_name, tilt_deg, last_snow_event, panels_covered_since,\n                      estimated_depth_mm, melt_hours, tilt_melt_factor, detection_source,\n                      cleared_at, updated_at\n               FROM snow_tracking_groups"
        )
        for group_row in rows:
            states_by_group[group_row[0]] = {
                "tilt_deg": group_row[1],
                "last_snow_event": group_row[2],
                "panels_covered_since": group_row[3],
                "estimated_depth_mm": group_row[4],
                "melt_hours": group_row[5],
                "tilt_melt_factor": group_row[6],
                "detection_source": group_row[7],
                "cleared_at": group_row[8],
                "updated_at": group_row[9],
                "snow_covered": group_row[3] is not None and group_row[8] is None,
            }
        return states_by_group

    async def save_shadow_detection_group(
        self,
        prediction_id: str,
        group_name: str,
        data: Dict[str, Any],
        auto_commit: bool = True,
    ) -> None:
        """Save per-group shadow detection result."""
        shadow_percent = _validated_shadow_percentage(
            data.get("shadow_percent"), "shadow_percent", required=True
        )
        await self.execute(
            "INSERT INTO shadow_detection_groups\n               (prediction_id, group_name, shadow_type, shadow_percent, confidence,\n                root_cause, efficiency_ratio, loss_kwh, theoretical_max_kwh, actual_kwh)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                   shadow_type = excluded.shadow_type,\n                   shadow_percent = excluded.shadow_percent,\n                   confidence = excluded.confidence,\n                   root_cause = excluded.root_cause,\n                   efficiency_ratio = excluded.efficiency_ratio,\n                   loss_kwh = excluded.loss_kwh,\n                   theoretical_max_kwh = excluded.theoretical_max_kwh,\n                   actual_kwh = excluded.actual_kwh",
            (
                prediction_id,
                group_name,
                data.get("shadow_type"),
                shadow_percent,
                data.get("confidence"),
                data.get("root_cause"),
                data.get("efficiency_ratio"),
                data.get("loss_kwh"),
                data.get("theoretical_max_kwh"),
                data.get("actual_kwh"),
            ),
            auto_commit=auto_commit,
        )

    async def load_shadow_detection_groups(
        self, prediction_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Load per-group shadow detection results for a prediction."""
        shadow_by_group = {}
        rows = await self.fetchall(
            "SELECT group_name, shadow_type, shadow_percent, confidence,\n                      root_cause, efficiency_ratio, loss_kwh, theoretical_max_kwh, actual_kwh\n               FROM shadow_detection_groups WHERE prediction_id = ?",
            (prediction_id,),
        )
        for group_row in rows:
            shadow_by_group[group_row[0]] = {
                "shadow_type": group_row[1],
                "shadow_percent": group_row[2],
                "confidence": group_row[3],
                "root_cause": group_row[4],
                "efficiency_ratio": group_row[5],
                "loss_kwh": group_row[6],
                "theoretical_max_kwh": group_row[7],
                "actual_kwh": group_row[8],
            }
        return shadow_by_group

    async def is_actual_backfill_blocked(self, date_str: str, hour: int) -> bool:
        row = await self.fetchone(
            "SELECT 1\n               FROM emergency_outage_periods\n               WHERE is_active = 1\n                 AND backfill_policy = 'block'\n                 AND ? BETWEEN start_date AND end_date\n                 AND (? > start_date OR ? >= start_hour)\n                 AND (? < end_date OR ? <= end_hour)\n               LIMIT 1",
            (date_str, date_str, hour, date_str, hour),
        )
        if row:
            return True
        row = await self.fetchone(
            "SELECT actual_backfill_blocked\n               FROM hourly_predictions\n               WHERE target_date = ? AND target_hour = ?",
            (date_str, hour),
        )
        return bool(row and row[0])

    async def count_actual_backfill_blocked_hours(
        self, date_str: str, hours: Optional[List[int]] = None
    ) -> int:
        if hours:
            placeholders = ",".join(("?" for unused_item in hours))
            row = await self.fetchone(
                f"SELECT COUNT(*)\n                    FROM hourly_predictions\n                    WHERE target_date = ?\n                      AND target_hour IN ({placeholders})\n                      AND COALESCE(actual_backfill_blocked, 0) = 1",
                (date_str, *hours),
            )
        else:
            row = await self.fetchone(
                "SELECT COUNT(*)\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                     AND COALESCE(actual_backfill_blocked, 0) = 1",
                (date_str,),
            )
        return int(row[0] or 0) if row else 0

    @_atomic_database_write
    async def update_prediction_panel_group_flags(
        self,
        prediction_id: str,
        group_name: str,
        flags: Dict[str, Any],
        auto_commit: bool = True,
    ) -> None:
        """Update per-group learning exclusion and status flags on prediction_panel_groups."""
        reason_priorities = {
            "missing_total_actual": 50,
            "incomplete_group_data": 40,
            "consistency_mismatch": 30,
            "manual_actual_repair": 25,
            "unplausible_daily_yield_spike": 20,
            "reconstructed_gap_interpolation": 20,
            "snow_covered_group": 10,
            "shadow_loss": 10,
            "frost": 10,
        }

        def get_priority(reason: Optional[str]) -> int:
            if not reason:
                return 0
            return reason_priorities.get(reason, 1)

        existing_flags_row = await self.fetchone(
            "SELECT exclusion_reason_group, exclude_from_learning_group\n               FROM prediction_panel_groups\n               WHERE prediction_id = ? AND group_name = ?",
            (prediction_id, group_name),
        )
        existing_exclusion_reason = None
        existing_learning_excluded = False
        if existing_flags_row:
            existing_exclusion_reason = existing_flags_row[0]
            existing_learning_excluded = bool(existing_flags_row[1])
        requested_exclusion_reason = flags.get("exclusion_reason_group")
        requested_learning_excluded = flags.get("exclude_from_learning_group", False)
        existing_reason_priority = get_priority(existing_exclusion_reason)
        requested_reason_priority = get_priority(requested_exclusion_reason)
        if requested_learning_excluded is False and requested_exclusion_reason is None:
            selected_exclusion_reason = None
            selected_learning_excluded = False
        elif existing_reason_priority > requested_reason_priority:
            selected_exclusion_reason = existing_exclusion_reason
            selected_learning_excluded = existing_learning_excluded
        else:
            selected_exclusion_reason = requested_exclusion_reason
            selected_learning_excluded = requested_learning_excluded
        await self.execute(
            "UPDATE prediction_panel_groups SET\n                   exclude_from_learning_group = ?,\n                   exclusion_reason_group = ?,\n                   snow_covered_group = ?,\n                   shadow_type_group = ?\n               WHERE prediction_id = ? AND group_name = ?",
            (
                selected_learning_excluded,
                selected_exclusion_reason,
                flags.get("snow_covered_group", False),
                flags.get("shadow_type_group"),
                prediction_id,
                group_name,
            ),
        )
        if auto_commit:
            await self.commit()
        return None if True else None

    async def get_active_panel_group_topology_meta(
        self, group_name: str
    ) -> Dict[str, Any]:
        """Return active topology metadata for a panel group, if epochs exist."""
        try:
            row = await self.fetchone(
                "SELECT e.epoch_id, eg.group_uid, eg.group_lineage_uid, eg.power_wp\n                   FROM panel_group_config_epochs e\n                   JOIN panel_group_config_epoch_groups eg ON eg.epoch_id = e.epoch_id\n                   WHERE e.valid_to IS NULL AND eg.display_name = ?\n                   ORDER BY e.valid_from DESC, e.epoch_id DESC\n                   LIMIT 1",
                (group_name,),
            )
        except Exception:
            return {}
        if not row:
            return {}
        return {
            "config_epoch_id": row[0],
            "group_uid": row[1],
            "group_lineage_uid": row[2],
            "power_wp_at_prediction": row[3],
        }

    @_atomic_database_write
    async def save_drift_rolling_metric(
        self,
        scope: str,
        window_days: int,
        season: Optional[str],
        metrics: Dict[str, Any],
    ) -> None:
        """Upsert rolling-window drift metrics."""
        if season is None:
            row = await self.fetchone(
                "SELECT 1 FROM drift_metrics_rolling\n                   WHERE scope = ? AND window_days = ? AND season IS NULL\n                   LIMIT 1",
                (scope, window_days),
            )
            if row:
                await self.execute(
                    "UPDATE drift_metrics_rolling\n                       SET mae = ?,\n                           rmse = ?,\n                           bias = ?,\n                           coverage_10 = ?,\n                           coverage_20 = ?,\n                           sample_count = ?,\n                           calculated_at = CURRENT_TIMESTAMP\n                       WHERE scope = ? AND window_days = ? AND season IS NULL",
                    (
                        metrics.get("mae"),
                        metrics.get("rmse"),
                        metrics.get("bias"),
                        metrics.get("coverage_10"),
                        metrics.get("coverage_20"),
                        metrics.get("sample_count"),
                        scope,
                        window_days,
                    ),
                )
                return
            await self.execute(
                "INSERT INTO drift_metrics_rolling\n                   (scope, window_days, season, mae, rmse, bias, coverage_10,\n                    coverage_20, sample_count, calculated_at)\n                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    scope,
                    window_days,
                    metrics.get("mae"),
                    metrics.get("rmse"),
                    metrics.get("bias"),
                    metrics.get("coverage_10"),
                    metrics.get("coverage_20"),
                    metrics.get("sample_count"),
                ),
            )
            return
        await self.execute(
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

    async def get_drift_rolling_metrics(
        self, scope: str, window_days: int, season: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Get rolling drift metrics for a scope/window/season."""
        if season is not None:
            row = await self.fetchone(
                "SELECT mae, rmse, bias, coverage_10, coverage_20, sample_count, calculated_at\n                   FROM drift_metrics_rolling\n                   WHERE scope = ? AND window_days = ? AND season = ?\n                   ORDER BY calculated_at DESC, id DESC LIMIT 1",
                (scope, window_days, season),
            )
        else:
            row = await self.fetchone(
                "SELECT mae, rmse, bias, coverage_10, coverage_20, sample_count, calculated_at\n                   FROM drift_metrics_rolling\n                   WHERE scope = ? AND window_days = ? AND season IS NULL\n                   ORDER BY calculated_at DESC, id DESC LIMIT 1",
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

    @_atomic_database_write
    async def save_drift_bucket_metric(
        self,
        scope: str,
        cloud_bucket: str,
        hour_bucket: str,
        season: Optional[str],
        metrics: Dict[str, Any],
    ) -> None:
        """Upsert bucket-specific drift metrics."""
        if season is None:
            row = await self.fetchone(
                "SELECT 1 FROM drift_metrics_bucket\n                   WHERE scope = ? AND cloud_bucket = ? AND hour_bucket = ?\n                       AND season IS NULL\n                   LIMIT 1",
                (scope, cloud_bucket, hour_bucket),
            )
            if row:
                await self.execute(
                    "UPDATE drift_metrics_bucket\n                       SET mae = ?,\n                           mae_baseline = ?,\n                           mae_ratio = ?,\n                           bias = ?,\n                           sample_count = ?,\n                           calculated_at = CURRENT_TIMESTAMP\n                       WHERE scope = ? AND cloud_bucket = ? AND hour_bucket = ?\n                           AND season IS NULL",
                    (
                        metrics.get("mae"),
                        metrics.get("mae_baseline"),
                        metrics.get("mae_ratio"),
                        metrics.get("bias"),
                        metrics.get("sample_count"),
                        scope,
                        cloud_bucket,
                        hour_bucket,
                    ),
                )
                return
            await self.execute(
                "INSERT INTO drift_metrics_bucket\n                   (scope, cloud_bucket, hour_bucket, season, mae, mae_baseline,\n                    mae_ratio, bias, sample_count, calculated_at)\n                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    scope,
                    cloud_bucket,
                    hour_bucket,
                    metrics.get("mae"),
                    metrics.get("mae_baseline"),
                    metrics.get("mae_ratio"),
                    metrics.get("bias"),
                    metrics.get("sample_count"),
                ),
            )
            return
        await self.execute(
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

    @_atomic_database_write
    async def save_drift_event(self, event: Dict[str, Any]) -> None:
        """Insert one audit event per date/scope/type/severity/bucket."""
        event_date = event.get("event_date")
        scope = event.get("scope")
        drift_type = event.get("drift_type")
        severity = event.get("severity")
        bucket_detail = event.get("bucket_detail")
        await self.execute(
            "INSERT INTO drift_events\n               (event_date, scope, drift_type, severity, bucket_detail,\n                metric_value, threshold_value, description, response_action)\n               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?\n               WHERE NOT EXISTS (\n                   SELECT 1\n                   FROM drift_events\n                   WHERE event_date = ?\n                     AND scope = ?\n                     AND drift_type = ?\n                     AND severity = ?\n                     AND COALESCE(bucket_detail, CHAR(0)) = COALESCE(?, CHAR(0))\n               )",
            (
                event_date,
                scope,
                drift_type,
                severity,
                bucket_detail,
                event.get("metric_value"),
                event.get("threshold_value"),
                event.get("description"),
                event.get("response_action"),
                event_date,
                scope,
                drift_type,
                severity,
                bucket_detail,
            ),
        )
        await self.commit()

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
                "event_date": row[0],
                "scope": row[1],
                "drift_type": row[2],
                "severity": row[3],
                "bucket_detail": row[4],
                "metric_value": row[5],
                "threshold_value": row[6],
                "description": row[7],
                "response_action": row[8],
                "response_executed": row[9],
                "created_at": row[10],
            }
            for row in rows
        ]

    @_atomic_database_write
    async def mark_drift_event_executed(self, event_id: int) -> None:
        """Mark a drift event's response as executed."""
        await self.execute(
            "UPDATE drift_events SET response_executed = TRUE WHERE id = ?", (event_id,)
        )
        await self.commit()

    @_atomic_database_write
    async def save_drift_cusum_state(self, scope: str, state: Dict[str, Any]) -> None:
        """Upsert CUSUM algorithm state for a scope."""
        await self.execute(
            "INSERT INTO drift_cusum_state\n               (scope, cusum_pos, cusum_neg, target_mean, last_reset, alert_count,\n                bias_correction_factor, bias_correction_updated, solar_clamp_min)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(scope) DO UPDATE SET\n                   cusum_pos = excluded.cusum_pos,\n                   cusum_neg = excluded.cusum_neg,\n                   target_mean = excluded.target_mean,\n                   last_reset = excluded.last_reset,\n                   alert_count = excluded.alert_count,\n                   bias_correction_factor = excluded.bias_correction_factor,\n                   bias_correction_updated = excluded.bias_correction_updated,\n                   solar_clamp_min = excluded.solar_clamp_min",
            (
                scope,
                state.get("cusum_pos", 0),
                state.get("cusum_neg", 0),
                state.get("target_mean", 0),
                state.get("last_reset"),
                state.get("alert_count", 0),
                state.get("bias_correction_factor", 1.0),
                state.get("bias_correction_updated"),
                state.get("solar_clamp_min", 0.75),
            ),
        )
        await self.commit()

    async def load_drift_cusum_state(self, scope: str) -> Optional[Dict[str, Any]]:
        """Load CUSUM state for a scope."""
        row = await self.fetchone(
            "SELECT cusum_pos, cusum_neg, target_mean, last_reset, alert_count, bias_correction_factor, bias_correction_updated, solar_clamp_min FROM drift_cusum_state WHERE scope = ?",
            (scope,),
        )
        return (
            None
            if not row
            else {
                "cusum_pos": row[0],
                "cusum_neg": row[1],
                "target_mean": row[2],
                "last_reset": row[3],
                "alert_count": row[4],
                "bias_correction_factor": row[5] if row[5] is not None else 1.0,
                "bias_correction_updated": row[6],
                "solar_clamp_min": row[7] if row[7] is not None else 0.75,
            }
        )

    async def get_drift_metrics_all_windows(
        self, scope: str
    ) -> Dict[int, Dict[str, Any]]:
        """Get drift metrics for all windows of a scope (for sensor display)."""
        metrics_by_window = {}
        rows = await self.fetchall(
            "SELECT window_days, mae, rmse, bias, coverage_10, coverage_20,\n                      sample_count, calculated_at, season\n               FROM drift_metrics_rolling\n               WHERE scope = ? AND season IS NULL\n               ORDER BY window_days, calculated_at DESC, id DESC",
            (scope,),
        )
        for metrics_row in rows:
            if metrics_row[0] in metrics_by_window:
                continue
            metrics_by_window[metrics_row[0]] = {
                "mae": metrics_row[1],
                "rmse": metrics_row[2],
                "bias": metrics_row[3],
                "coverage_10": metrics_row[4],
                "coverage_20": metrics_row[5],
                "sample_count": metrics_row[6],
                "calculated_at": metrics_row[7],
            }
        return metrics_by_window

    async def vacuum(self) -> None:
        """Vacuum the database to reclaim space."""
        if self._db is None:
            return
        if self._transaction_owner is asyncio.current_task():
            raise RuntimeError("VACUUM cannot run inside a managed transaction")

        async def _vacuum():
            if self.has_active_unmanaged_transaction():
                raise RuntimeError("VACUUM refused while another transaction is active")
            await self._retry_on_locked(lambda: self._db.execute("VACUUM"))

        if self._write_lock is not None:
            async with self._write_lock:
                await _vacuum()
        else:
            await _vacuum()
        _LOGGER.info("Database vacuumed")

    async def get_db_size(self) -> int:
        """Get database file size in bytes."""
        return Path(self.db_path).stat().st_size

    async def _retry_on_locked(self, operation, *, budget: Optional[float] = None):
        """Retry a locked database operation until its wall-clock budget is spent."""
        if budget is None:
            budget = self._LOCK_RETRY_BUDGET_SECONDS
        retry_deadline = monotonic() + budget
        attempt_count = 0
        while True:
            try:
                return await operation()
            except Exception as operation_error:
                error_message = str(operation_error).lower()
                if (
                    "database is locked" not in error_message
                    and "database is busy" not in error_message
                ):
                    raise
                backoff_seconds = min(
                    self._LOCK_RETRY_MAX_BACKOFF_SECONDS, 0.05 * 2**attempt_count
                ) + random.uniform(0, 0.05)
                if monotonic() + backoff_seconds >= retry_deadline:
                    _LOGGER.warning(
                        "Database stayed locked for %.1fs across %d attempts: %s",
                        budget,
                        attempt_count + 1,
                        str(operation_error)[:200],
                    )
                    raise
                _LOGGER.debug(
                    "Database locked (attempt %d), retrying in %.2fs: %s",
                    attempt_count + 1,
                    backoff_seconds,
                    str(operation_error)[:100],
                )
                await asyncio.sleep(backoff_seconds)
                attempt_count += 1

    async def execute(
        self, sql: str, parameters: tuple = (), auto_commit: bool = True
    ) -> None:
        """Execute a raw SQL statement with write serialization and retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping execute")
            return
        in_owned_transaction = self._transaction_owner is asyncio.current_task()
        if not auto_commit and (not in_owned_transaction):
            raise RuntimeError(
                "auto_commit=False requires the current managed transaction"
            )

        async def _do():
            try:
                await self._db.execute(sql, parameters)
                if auto_commit and (not in_owned_transaction):
                    await self._db.commit()
            except BaseException:
                if auto_commit and (not in_owned_transaction):
                    await self._await_transaction_cleanup(self._db.rollback)
                raise
            return None if True else None

        if in_owned_transaction:
            await self._retry_on_locked(_do)
            return
        if auto_commit and self._write_lock is not None:
            async with self._write_lock:
                await self._retry_on_locked(_do)
        else:
            await self._retry_on_locked(_do)
        return None if True else None

    async def commit(self) -> None:
        """Manually commit the current transaction. @zara"""
        if self._db is None:
            return None if True else None
        if self._transaction_owner is asyncio.current_task():
            return None if True else None
        if self._write_lock is not None:
            async with self._write_lock:
                await self._retry_on_locked(self._db.commit)
            return None if True else None
        await self._retry_on_locked(self._db.commit)

    async def enable_foreign_keys(self) -> None:
        """Enable SQLite foreign-key enforcement outside a transaction."""
        if self._db is None:
            raise RuntimeError("database connection is not available")
        if self._write_lock is not None:
            async with self._write_lock:
                if self._db.in_transaction:
                    raise RuntimeError(
                        "foreign-key PRAGMA requires no active transaction"
                    )
                cursor = await self._db.execute("PRAGMA foreign_keys = ON")
                await cursor.close()
            return None if True else None
        if self._db.in_transaction:
            raise RuntimeError("foreign-key PRAGMA requires no active transaction")
        cursor = await self._db.execute("PRAGMA foreign_keys = ON")
        await cursor.close()

    def has_active_unmanaged_transaction(self) -> bool:
        """Return whether the shared connection is inside a non-owned transaction."""
        if self._db is None or not self._db.in_transaction:
            return False
        return self._transaction_owner is not asyncio.current_task()

    async def _await_transaction_cleanup(self, operation) -> None:
        """Finish transaction cleanup even when the calling task is cancelled."""
        cleanup_task = asyncio.create_task(operation())
        caller_cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                caller_cancelled = True
                continue
        await cleanup_task
        if caller_cancelled:
            raise asyncio.CancelledError

    @asynccontextmanager
    async def transaction(self, *, deadline: Optional[float] = None):
        """Context manager for multi-statement write transactions. @zara

        Acquires write lock and opens BEGIN IMMEDIATE transaction.
        BEGIN IMMEDIATE retries within the begin budget or remaining deadline;
        COMMIT retries within the lock-retry budget or remaining deadline.

        Usage:
            async with db.transaction():
                await db.execute("INSERT ...", params, auto_commit=False)
                await db.execute("UPDATE ...", params, auto_commit=False)
        """
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping transaction")
            yield None
            return
        owner_task = asyncio.current_task()
        if self._transaction_depth > 0 and self._transaction_owner is owner_task:
            self._transaction_depth += 1
            savepoint_name = f"sfml_nested_transaction_{self._transaction_depth}"

            async def _savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(f"SAVEPOINT {savepoint_name}")
                )

            async def _release_savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                )

            async def _rollback_savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                )
                await self._retry_on_locked(
                    lambda: self._db.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                )

            try:
                await _savepoint()
                try:
                    yield None
                except BaseException:
                    await self._await_transaction_cleanup(_rollback_savepoint)
                    raise
                try:
                    await self._await_transaction_cleanup(_release_savepoint)
                except BaseException:
                    try:
                        await self._await_transaction_cleanup(_rollback_savepoint)
                    except BaseException:
                        pass
                    raise
            finally:
                self._transaction_depth -= 1
            return

        async def _begin_once():
            await self._db.execute("BEGIN IMMEDIATE")

        def _lock_budget() -> Optional[float]:
            if deadline is None:
                return
            return max(0.0, deadline - monotonic())

        async def _begin():
            retry_budget_seconds = _lock_budget()
            if retry_budget_seconds is None:
                retry_budget_seconds = self._BEGIN_RETRY_BUDGET_SECONDS
            await self._retry_on_locked(
                lambda: self._await_transaction_cleanup(_begin_once),
                budget=retry_budget_seconds,
            )

        async def _commit():
            retry_budget_seconds = _lock_budget()
            if retry_budget_seconds is None:
                retry_budget_seconds = self._LOCK_RETRY_BUDGET_SECONDS
            await self._retry_on_locked(
                lambda: self._await_transaction_cleanup(self._db.commit),
                budget=retry_budget_seconds,
            )

        async def _rollback():
            await self._await_transaction_cleanup(self._db.rollback)

        async def _enter_transaction() -> bool:
            if self.has_active_unmanaged_transaction():
                raise RuntimeError(
                    "Database connection already has an active unmanaged transaction"
                )
            try:
                await _begin()
            except BaseException:
                if self._db.in_transaction:
                    try:
                        await _rollback()
                    except BaseException:
                        pass
                raise
            return True

        @asynccontextmanager
        async def _run_outer_transaction():
            transaction_entered = False
            try:
                transaction_entered = await _enter_transaction()
                self._transaction_owner = owner_task
                self._transaction_depth = 1
                try:
                    yield None
                except BaseException as transaction_error:
                    if (
                        isinstance(transaction_error, asyncio.CancelledError)
                        and self._transaction_abort_event is not None
                    ):
                        self._transaction_abort_event.set()
                    if transaction_entered:
                        try:
                            await _rollback()
                        except BaseException:
                            pass
                    raise
                try:
                    await _commit()
                except BaseException:
                    if transaction_entered:
                        try:
                            await _rollback()
                        except BaseException:
                            pass
                    raise
            finally:
                self._transaction_depth = 0
                self._transaction_owner = None

        @asynccontextmanager
        async def _run_with_deadline():
            if deadline is None:
                async with _run_outer_transaction():
                    yield None
                if True:
                    return
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    "database transaction deadline expired before gate acquisition"
                )
            abort_event = threading.Event()
            deadline_signal_task = None

            def _abort_current_statement() -> int:
                return int(abort_event.is_set())

            async def _signal_deadline() -> None:
                await asyncio.sleep(remaining_seconds)
                abort_event.set()

            async def _restore_connection_state() -> None:
                abort_event.clear()
                await self._db.set_progress_handler(None, 0)
                await self._probe_database_connection()

            async def _restore_deadline_state() -> None:
                try:
                    await _restore_connection_state()
                except Exception as restore_error:
                    if not _is_sqlite_authorizer_error(restore_error):
                        raise
                    await self._clear_connection_authorizer()
                    await _restore_connection_state()
                return None if True else None

            try:
                await self._db.set_progress_handler(_abort_current_statement, 1000)
                self._transaction_abort_event = abort_event
                deadline_signal_task = asyncio.create_task(
                    _signal_deadline(), name="sfml-shared-database-deadline-signal"
                )
                async with asyncio.timeout(remaining_seconds):
                    async with _run_outer_transaction():
                        yield None
            finally:
                if deadline_signal_task is not None:
                    deadline_signal_task.cancel()
                    await asyncio.gather(deadline_signal_task, return_exceptions=True)
                try:
                    await self._await_transaction_cleanup(_restore_deadline_state)
                finally:
                    self._transaction_abort_event = None

        if self._write_lock is None:
            async with _run_with_deadline():
                yield None
            if True:
                return
        if deadline is None:
            async with self._write_lock:
                async with _run_with_deadline():
                    yield None
            if True:
                return
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError(
                "database transaction deadline expired before gate acquisition"
            )
        try:
            async with asyncio.timeout(remaining_seconds):
                await self._write_lock.acquire()
        except TimeoutError as timeout_error:
            raise TimeoutError(
                "database transaction deadline expired before gate acquisition"
            ) from timeout_error
        try:
            async with _run_with_deadline():
                yield None
        finally:
            self._write_lock.release()

    async def clear_stale_authorizer(self) -> None:
        """Remove a leaked SQLite authorizer from the shared writer connection."""
        await self._clear_connection_authorizer()
        if self._db is None:
            return
        cursor = await self._db.execute(
            f"PRAGMA busy_timeout = {self._DEFAULT_BUSY_TIMEOUT_MS}"
        )
        await cursor.close()

    async def _clear_connection_authorizer(self) -> None:
        if self._db is None:
            return
        _LOGGER.error("Clearing a stale SQLite authorizer on the shared connection")
        set_authorizer = getattr(self._db, "set_authorizer", None)
        if callable(set_authorizer):
            await self._await_transaction_cleanup(lambda: set_authorizer(None))
            return
        connection = getattr(self._db, "_conn", None)
        thread_execute = getattr(self._db, "_execute", None)
        if not (
            callable(thread_execute) and isinstance(connection, sqlite3.Connection)
        ):
            raise RuntimeError("cannot clear the SQLite authorizer on this connection")
        await self._await_transaction_cleanup(
            lambda: thread_execute(sqlite3.Connection.set_authorizer, connection, None)
        )

    async def _probe_database_connection(self) -> None:
        """Verify cleanup before another job may use the shared connection."""
        if self._db is None:
            raise RuntimeError("database connection is not available")
        if self._db.in_transaction:
            await self._await_transaction_cleanup(self._db.rollback)
        cursor = await self._db.execute("SELECT 1")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None or row[0] != 1 or self._db.in_transaction:
            raise RuntimeError("database connection recovery probe failed")

    async def executemany(self, sql: str, parameters_list: List[tuple]) -> int:
        """Execute a SQL statement with multiple parameter sets, with write serialization. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping executemany")
            return 0
        if not parameters_list:
            return 0
        in_owned_transaction = self._transaction_owner is asyncio.current_task()

        async def _do():
            await self._db.executemany(sql, parameters_list)
            if not in_owned_transaction:
                await self._db.commit()
            return None if True else None

        if in_owned_transaction:
            await self._retry_on_locked(_do)
        elif self._write_lock is not None:
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
            async with self._db.execute(sql, parameters) as cursor:
                return await cursor.fetchone()
            return None if True else None

        if self._transaction_owner is asyncio.current_task():
            return await self._retry_on_locked(_do)
        if self._write_lock is not None:
            async with self._write_lock:
                return await self._retry_on_locked(_do)
        return await self._retry_on_locked(_do)

    async def fetchall(self, sql: str, parameters: tuple = ()) -> List[aiosqlite.Row]:
        """Execute a query and fetch all rows with retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, returning empty list from fetchall")
            return []

        async def _do():
            async with self._db.execute(sql, parameters) as cursor:
                return await cursor.fetchall()
            return None if True else None

        if self._transaction_owner is asyncio.current_task():
            return await self._retry_on_locked(_do)
        if self._write_lock is not None:
            async with self._write_lock:
                return await self._retry_on_locked(_do)
        return await self._retry_on_locked(_do)

    async def get_daily_summary_statistics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        season: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate yield and clean forecast quality from daily summaries."""
        where_clauses = ["actual_total_kwh IS NOT NULL"]
        query_parameters = []
        if start_date is not None:
            where_clauses.append("date >= ?")
            query_parameters.append(start_date)
        if end_date is not None:
            where_clauses.append("date < ?")
            query_parameters.append(end_date)
        if season is not None:
            where_clauses.append("season = ?")
            query_parameters.append(season)
        summary_row = await self.fetchone(
            f"SELECT COUNT(*),\n                       SUM(actual_total_kwh),\n                       AVG(actual_total_kwh),\n                       MAX(actual_total_kwh),\n                       MIN(actual_total_kwh),\n                       SUM(CASE WHEN evaluation_hours_count > 0 THEN 1 ELSE 0 END),\n                       AVG(CASE WHEN evaluation_hours_count > 0\n                                THEN accuracy_percent END),\n                       AVG(CASE WHEN evaluation_hours_count > 0\n                                THEN evaluation_coverage_percent END),\n                       AVG(CASE WHEN evaluation_hours_count > 0\n                                THEN excluded_mppt_hours_count END)\n                FROM daily_summaries\n                WHERE {' AND '.join(where_clauses)}",
            tuple(query_parameters),
        )
        return (
            {
                "yield_days": 0,
                "evaluation_days": 0,
                "total_yield_kwh": None,
                "avg_yield_kwh": None,
                "max_yield_kwh": None,
                "min_yield_kwh": None,
                "avg_accuracy": None,
                "avg_evaluation_coverage_percent": None,
                "avg_excluded_mppt_hours": None,
            }
            if not summary_row
            else {
                "yield_days": int(summary_row[0] or 0),
                "total_yield_kwh": (
                    float(summary_row[1]) if summary_row[1] is not None else None
                ),
                "avg_yield_kwh": (
                    float(summary_row[2]) if summary_row[2] is not None else None
                ),
                "max_yield_kwh": (
                    float(summary_row[3]) if summary_row[3] is not None else None
                ),
                "min_yield_kwh": (
                    float(summary_row[4]) if summary_row[4] is not None else None
                ),
                "evaluation_days": int(summary_row[5] or 0),
                "avg_accuracy": (
                    float(summary_row[6]) if summary_row[6] is not None else None
                ),
                "avg_evaluation_coverage_percent": (
                    float(summary_row[7]) if summary_row[7] is not None else None
                ),
                "avg_excluded_mppt_hours": (
                    float(summary_row[8]) if summary_row[8] is not None else None
                ),
            }
        )
