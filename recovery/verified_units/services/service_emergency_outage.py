# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Emergency outage quarantine service for production and sensor failures."""
from __future__ import annotations
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from homeassistant.core import HomeAssistant, ServiceCall
from ..core.core_helpers import SafeDateTimeUtil as dt_util

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
_LOGGER = logging.getLogger(__name__)
ACTION_QUARANTINE_ONLY = "quarantine_only"
ACTION_QUARANTINE_AND_INVALIDATE = "quarantine_and_invalidate_learning"
ACTION_QUARANTINE_INVALIDATE_RETRAIN = "quarantine_invalidate_and_retrain"
ACTUAL_POLICY_PRESERVE = "preserve_actuals"
ACTUAL_POLICY_NULL_INVALID = "null_invalid_actuals"
BACKFILL_POLICY_BLOCK = "block"
VALID_ACTIONS = {
    ACTION_QUARANTINE_ONLY,
    ACTION_QUARANTINE_AND_INVALIDATE,
    ACTION_QUARANTINE_INVALIDATE_RETRAIN,
}
VALID_ACTUAL_POLICIES = {ACTUAL_POLICY_PRESERVE, ACTUAL_POLICY_NULL_INVALID}
VALID_REASONS = {
    "custom",
    "maintenance",
    "meter_failure",
    "plant_offline",
    "sensor_failure",
    "inverter_failure",
    "lightning_damage",
}


class EmergencyOutageService:
    """Emergency service for quarantining bad PV actual periods."""

    def __init__(
        self, hass: HomeAssistant, coordinator: "SolarForecastMLCoordinator"
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator

    @property
    def db_manager(self):
        data_manager = getattr(self.coordinator, "data_manager", None)
        if not data_manager:
            return
        return getattr(data_manager, "_db_manager", None)

    async def handle_emergency_outage_quarantine(self, call: ServiceCall) -> None:
        try:
            result = await self._run(call.data)
            self._log_result(result)
        except Exception as error:
            _LOGGER.error(
                "SFML emergency outage quarantine failed: %s", error, exc_info=True
            )
        return None if True else None

    async def _run(self, data: dict[str, Any]) -> dict[str, Any]:
        db = self.db_manager
        if db is None:
            raise RuntimeError("database manager unavailable")
        payload = self._parse_payload(data)
        dry_run = payload["dry_run"]
        if not dry_run and (not payload["acknowledge_emergency_risk"]):
            raise ValueError(
                "acknowledge_emergency_risk must be true when dry_run is false"
            )
        await self._ensure_tables()
        result = await self._build_preview(payload)
        result["dry_run"] = dry_run
        if dry_run:
            await self._write_audit(payload, result, dry_run=True)
            return result
        if payload["create_backup"]:
            result["backup_path"] = self._create_backup(payload)
        quarantine_result = await self._apply_quarantine(payload)
        result.update(quarantine_result)
        if payload["action"] in {
            ACTION_QUARANTINE_AND_INVALIDATE,
            ACTION_QUARANTINE_INVALIDATE_RETRAIN,
        }:
            result["invalidated"] = await self._invalidate_learning(payload)
        if payload["action"] == ACTION_QUARANTINE_INVALIDATE_RETRAIN:
            result["retrain"] = await self._retrain_ai_model()
        await self._write_audit(payload, result, dry_run=False)
        if hasattr(self.coordinator, "_refresh_hourly_predictions_cache"):
            await self.coordinator._refresh_hourly_predictions_cache()
            self.coordinator.async_update_listeners()
        return result

    def _parse_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        start_date = self._parse_date(data.get("start_date"), "start_date")
        end_date = self._parse_date(
            data.get("end_date") or data.get("start_date"), "end_date"
        )
        if end_date < start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        start_hour = self._parse_hour(data.get("start_hour"), 0)
        end_hour = self._parse_hour(data.get("end_hour"), 23)
        if start_date == end_date and end_hour < start_hour:
            raise ValueError(
                "end_hour must be greater than or equal to start_hour for single-day ranges"
            )
        action = data.get("action", ACTION_QUARANTINE_ONLY)
        if action not in VALID_ACTIONS:
            raise ValueError(f"unsupported action: {action}")
        actual_policy = data.get("actual_policy", ACTUAL_POLICY_PRESERVE)
        if actual_policy not in VALID_ACTUAL_POLICIES:
            raise ValueError(f"unsupported actual_policy: {actual_policy}")
        reason = str(data.get("reason") or "custom").strip()
        if reason not in VALID_REASONS:
            reason = "custom"
        custom_reason = str(data.get("custom_reason") or "").strip()
        quarantine_reason = f"plant_outage_{reason}"
        if reason == "custom" and custom_reason:
            reason_slug = "".join(
                (
                    character if character.isalnum() or character == "_" else "_"
                    for character in custom_reason.lower()
                )
            )[:48]
            quarantine_reason = f"plant_outage_{reason_slug or 'custom'}"
        return {
            "start_date": start_date,
            "end_date": end_date,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "reason": reason,
            "custom_reason": custom_reason,
            "quarantine_reason": quarantine_reason,
            "action": action,
            "actual_policy": actual_policy,
            "backfill_policy": BACKFILL_POLICY_BLOCK,
            "dry_run": bool(data.get("dry_run", True)),
            "create_backup": bool(data.get("create_backup", True)),
            "acknowledge_emergency_risk": bool(
                data.get("acknowledge_emergency_risk", False)
            ),
        }

    def _parse_date(self, value: Any, field_name: str) -> str:
        if not value:
            raise ValueError(f"{field_name} is required")
        return datetime.fromisoformat(str(value)).date().isoformat()

    def _parse_hour(self, value: Any, default: int) -> int:
        if value is None:
            return default
        hour = int(value)
        if hour < 0 or hour > 23:
            raise ValueError("hour must be between 0 and 23")
        return hour

    def _range_where(self, alias: str = "hp") -> str:
        return f"{alias}.target_date BETWEEN ? AND ? AND ({alias}.target_date > ? OR {alias}.target_hour >= ?) AND ({alias}.target_date < ? OR {alias}.target_hour <= ?)"

    def _range_params(self, payload: dict[str, Any]) -> tuple[Any, ...]:
        return (
            payload["start_date"],
            payload["end_date"],
            payload["start_date"],
            payload["start_hour"],
            payload["end_date"],
            payload["end_hour"],
        )

    async def _build_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        db = self.db_manager
        range_where = self._range_where("hp")
        range_params = self._range_params(payload)
        hourly_count_row = await db.fetchone(
            f"SELECT COUNT(*) AS hourly_rows,\n                      SUM(CASE WHEN hp.actual_kwh IS NULL THEN 1 ELSE 0 END) AS missing_actuals,\n                      SUM(CASE WHEN hp.actual_kwh IS NOT NULL AND hp.actual_kwh <= 0.01 THEN 1 ELSE 0 END) AS zero_actuals,\n                      SUM(CASE WHEN COALESCE(hp.actual_backfill_blocked, 0) = 1 THEN 1 ELSE 0 END) AS already_blocked,\n                      SUM(CASE WHEN COALESCE(hp.exclude_from_learning, 0) = 1 THEN 1 ELSE 0 END) AS already_excluded\n               FROM hourly_predictions hp\n               WHERE {range_where}",
            range_params,
        )
        panel_group_count_row = await db.fetchone(
            f"SELECT COUNT(*)\n               FROM prediction_panel_groups ppg\n               JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n               WHERE {range_where}",
            range_params,
        )
        learning_artifacts = await self._count_learning_artifacts(payload)
        return {
            "operation": "emergency_outage_quarantine",
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "start_hour": payload["start_hour"],
            "end_hour": payload["end_hour"],
            "reason": payload["quarantine_reason"],
            "action": payload["action"],
            "actual_policy": payload["actual_policy"],
            "backfill_policy": payload["backfill_policy"],
            "hourly_rows": (
                int(hourly_count_row["hourly_rows"] or 0) if hourly_count_row else 0
            ),
            "panel_group_rows": (
                int(panel_group_count_row[0] or 0) if panel_group_count_row else 0
            ),
            "missing_actuals": (
                int(hourly_count_row["missing_actuals"] or 0) if hourly_count_row else 0
            ),
            "zero_actuals": (
                int(hourly_count_row["zero_actuals"] or 0) if hourly_count_row else 0
            ),
            "already_backfill_blocked": (
                int(hourly_count_row["already_blocked"] or 0) if hourly_count_row else 0
            ),
            "already_excluded_from_learning": (
                int(hourly_count_row["already_excluded"] or 0)
                if hourly_count_row
                else 0
            ),
            "learning_artifacts": learning_artifacts,
        }

    async def _count_learning_artifacts(
        self, payload: dict[str, Any]
    ) -> dict[str, int]:
        db = self.db_manager
        counts_by_table = {}
        date_range = (payload["start_date"], payload["end_date"])
        for table_name, date_column in (
            ("physics_calibration_history", "date"),
            ("shadow_learning_history", "date"),
            ("drift_events", "event_date"),
            ("daily_summaries", "date"),
        ):
            try:
                count_row = await db.fetchone(
                    f"SELECT COUNT(*) FROM {table_name} WHERE {date_column} BETWEEN ? AND ?",
                    date_range,
                )
                counts_by_table[table_name] = int(count_row[0] or 0) if count_row else 0
            except Exception:
                counts_by_table[table_name] = 0
                continue
        return counts_by_table

    async def _apply_quarantine(self, payload: dict[str, Any]) -> dict[str, Any]:
        db = self.db_manager
        quarantine_at = dt_util.now().isoformat()
        range_where = self._range_where("hp")
        range_params = self._range_params(payload)
        quarantine_reason = payload["quarantine_reason"]
        async with db.transaction():
            await db.execute(
                "INSERT INTO emergency_outage_periods\n                   (created_at, start_date, end_date, start_hour, end_hour, reason,\n                    action, actual_policy, backfill_policy, source, is_active)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'emergency_service', 1)",
                (
                    quarantine_at,
                    payload["start_date"],
                    payload["end_date"],
                    payload["start_hour"],
                    payload["end_hour"],
                    quarantine_reason,
                    payload["action"],
                    payload["actual_policy"],
                    payload["backfill_policy"],
                ),
                auto_commit=False,
            )
            last_id_row = await db.fetchone("SELECT last_insert_rowid()")
            period_id = int(last_id_row[0]) if last_id_row else None
            actual_null_clause = ""
            if payload["actual_policy"] == ACTUAL_POLICY_NULL_INVALID:
                actual_null_clause = ", actual_kwh = CASE WHEN actual_kwh IS NOT NULL AND (actual_kwh <= 0.01 OR COALESCE(sensor_data_complete, 0) = 0 OR COALESCE(has_sensor_data, 0) = 0) THEN NULL ELSE actual_kwh END"
            await db.execute(
                f"UPDATE hourly_predictions AS hp\n                   SET exclude_from_learning = 1,\n                       exclude_from_clean_evaluation = 1,\n                       actual_backfill_blocked = 1,\n                       data_quarantine_reason = ?,\n                       data_quarantine_at = ?,\n                       mppt_throttle_reason = COALESCE(NULLIF(mppt_throttle_reason, ''), ?)\n                       {actual_null_clause}\n                   WHERE {range_where}",
                (quarantine_reason, quarantine_at, quarantine_reason, *range_params),
                auto_commit=False,
            )
            await db.execute(
                f"UPDATE prediction_panel_groups\n                   SET exclude_from_learning_group = 1,\n                       exclusion_reason_group = COALESCE(exclusion_reason_group, ?)\n                   WHERE prediction_id IN (\n                       SELECT hp.prediction_id FROM hourly_predictions hp WHERE {range_where}\n                   )",
                (quarantine_reason, *range_params),
                auto_commit=False,
            )
            affected_hourly_rows = await self._count_matching_hourly(payload)
            affected_panel_group_rows = await self._count_matching_panel_groups(payload)
            if period_id is not None:
                await db.execute(
                    "UPDATE emergency_outage_periods\n                       SET affected_hourly_rows = ?, affected_panel_group_rows = ?\n                       WHERE id = ?",
                    (affected_hourly_rows, affected_panel_group_rows, period_id),
                    auto_commit=False,
                )
        return {
            "written": True,
            "period_id": period_id,
            "affected_hourly_rows": affected_hourly_rows,
            "affected_panel_group_rows": affected_panel_group_rows,
        }

    async def _count_matching_hourly(self, payload: dict[str, Any]) -> int:
        count_row = await self.db_manager.fetchone(
            f"SELECT COUNT(*) FROM hourly_predictions hp WHERE {self._range_where('hp')}",
            self._range_params(payload),
        )
        return int(count_row[0] or 0) if count_row else 0

    async def _count_matching_panel_groups(self, payload: dict[str, Any]) -> int:
        count_row = await self.db_manager.fetchone(
            f"SELECT COUNT(*)\n                FROM prediction_panel_groups ppg\n                JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                WHERE {self._range_where('hp')}",
            self._range_params(payload),
        )
        return int(count_row[0] or 0) if count_row else 0

    async def _invalidate_learning(self, payload: dict[str, Any]) -> dict[str, int]:
        db = self.db_manager
        invalidated_counts = {}
        start_date = payload["start_date"]
        end_date = payload["end_date"]
        deletion_operations = [
            (
                "physics_calibration_history",
                "DELETE FROM physics_calibration_history WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ),
            (
                "shadow_learning_history",
                "DELETE FROM shadow_learning_history WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ),
            (
                "drift_events",
                "DELETE FROM drift_events WHERE event_date BETWEEN ? AND ?",
                (start_date, end_date),
            ),
            ("drift_metrics_rolling", "DELETE FROM drift_metrics_rolling", ()),
            ("drift_metrics_bucket", "DELETE FROM drift_metrics_bucket", ()),
            ("drift_cusum_state", "DELETE FROM drift_cusum_state", ()),
            (
                "method_performance_learning",
                "DELETE FROM method_performance_learning",
                (),
            ),
            ("group_method_performance", "DELETE FROM group_method_performance", ()),
            (
                "weather_regime_performance_learning",
                "DELETE FROM weather_regime_performance_learning",
                (),
            ),
            (
                "physics_calibration_hourly",
                "DELETE FROM physics_calibration_hourly",
                (),
            ),
            (
                "physics_calibration_buckets",
                "DELETE FROM physics_calibration_buckets",
                (),
            ),
            (
                "physics_calibration_bucket_hourly",
                "DELETE FROM physics_calibration_bucket_hourly",
                (),
            ),
            ("ai_lstm_weights", "DELETE FROM ai_lstm_weights", ()),
            ("ai_lstm_meta", "DELETE FROM ai_lstm_meta", ()),
            ("ai_ridge_weights", "DELETE FROM ai_ridge_weights", ()),
            ("ai_ridge_meta", "DELETE FROM ai_ridge_meta", ()),
            ("ai_ridge_normalization", "DELETE FROM ai_ridge_normalization", ()),
            ("ai_learned_weights_meta", "DELETE FROM ai_learned_weights_meta", ()),
        ]
        async with db.transaction():
            for table_name, delete_sql, delete_params in deletion_operations:
                try:
                    count_before = await self._table_count(table_name)
                    await db.execute(delete_sql, delete_params, auto_commit=False)
                    count_after = await self._table_count(table_name)
                    invalidated_counts[table_name] = max(0, count_before - count_after)
                except Exception as error:
                    _LOGGER.debug(
                        "Emergency invalidation skipped for %s: %s", table_name, error
                    )
                    invalidated_counts[table_name] = 0
                    continue
        return invalidated_counts if True else None

    async def _table_count(self, table: str) -> int:
        count_row = await self.db_manager.fetchone(f"SELECT COUNT(*) FROM {table}")
        return int(count_row[0] or 0) if count_row else 0

    async def _retrain_ai_model(self) -> dict[str, Any]:
        predictor = getattr(self.coordinator, "ai_predictor", None)
        if predictor is None:
            return {"started": False, "reason": "ai_predictor_unavailable"}
        training_result = await predictor.train_model()
        return {
            "started": True,
            "success": bool(getattr(training_result, "success", False)),
            "samples_used": getattr(training_result, "samples_used", None),
            "accuracy": getattr(training_result, "accuracy", None),
            "error_message": getattr(training_result, "error_message", None),
        }

    def _create_backup(self, payload: dict[str, Any]) -> str:
        database_path = Path(self.db_manager.db_path)
        backup_directory = database_path.parent / "emergency_backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        backup_timestamp = dt_util.now().strftime("%Y%m%d_%H%M%S")
        backup_path = (
            backup_directory
            / f"solar_forecast_emergency_outage_{payload['start_date']}_{payload['end_date']}_{backup_timestamp}.db"
        )
        shutil.copy2(database_path, backup_path)
        return str(backup_path)

    async def _ensure_tables(self) -> None:
        migrate_quarantine_tables = getattr(
            self.db_manager, "_migrate_emergency_outage_quarantine_v20260627", None
        )
        if migrate_quarantine_tables:
            await migrate_quarantine_tables()
        return None if True else None

    async def _write_audit(
        self, payload: dict[str, Any], result: dict[str, Any], dry_run: bool
    ) -> None:
        await self.db_manager.execute(
            "INSERT INTO emergency_outage_audit\n               (created_at, operation, dry_run, payload_json, result_json)\n               VALUES (?, ?, ?, ?, ?)",
            (
                dt_util.now().isoformat(),
                "emergency_outage_quarantine",
                1 if dry_run else 0,
                json.dumps(payload, sort_keys=True),
                json.dumps(result, sort_keys=True),
            ),
        )

    def _log_result(self, result: dict[str, Any]) -> None:
        _LOGGER.warning(
            "SFML emergency outage quarantine %s: range=%s..%s hours=%s..%s action=%s hourly=%s panel_groups=%s reason=%s",
            "DRY-RUN" if result.get("dry_run") else "APPLIED",
            result.get("start_date"),
            result.get("end_date"),
            result.get("start_hour"),
            result.get("end_hour"),
            result.get("action"),
            result.get("affected_hourly_rows", result.get("hourly_rows")),
            result.get("affected_panel_group_rows", result.get("panel_group_rows")),
            result.get("reason"),
        )
        _LOGGER.info(
            "SFML emergency outage quarantine result: %s",
            json.dumps(result, sort_keys=True),
        )
