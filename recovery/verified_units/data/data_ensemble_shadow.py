# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Append-only ensemble-versus-physics shadow evaluation."""
import json
import logging
import hashlib
import math
import uuid
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
ENSEMBLE_SHADOW_TFS_WEIGHT_MIN = 0.38
ENSEMBLE_SHADOW_TIMEZONE = "Europe/Berlin"
ENSEMBLE_SHADOW_PROVENANCE_CONTRACT_VERSION = "ensemble_shadow_provenance_v2"
_FORECAST_CYCLES = frozenset({"manual", "catchup", "pre_sunrise", "post_midnight"})
_TRIGGER_KINDS = frozenset({"manual", "catchup", "scheduled"})
EVENING_CHALLENGER_RULE_VERSION = "ensemble_shadow_evening_v3"
ENSEMBLE_SHADOW_RULE_VERSION = EVENING_CHALLENGER_RULE_VERSION
CANONICAL_SHADOW_FORECAST_CYCLE = "pre_sunrise"
CANONICAL_SHADOW_TRIGGER_KIND = "scheduled"
EVENING_HOURS_BEFORE_SUNSET_MAX = 3.0
EVENING_DAY_PROGRESS_MIN = 0.5
EVENING_SUN_ELEVATION_MIN = 0.0
EVENING_SUN_ELEVATION_MAX = 50.0
DAILY_MAE_NON_INFERIORITY_EPS_KWH = 0.005
MIN_COMPLETE_DAY_HOURS = 8
COMPLETE_DAY_EDGE_INTERVAL_FACTOR = 1.25
COMPLETE_DAY_MAX_PROGRESS_STEP_FACTOR = 1.5
MIN_PERIOD_CLEAN_DAYS = 30
MIN_PERIOD_GATED_DAYS = 15
MIN_PERIOD_GATED_HOURS = 15


class EnsembleShadowHandler:
    """Persist immutable morning inputs and append later evaluation results."""

    def __init__(self, db_manager: Any) -> None:
        self._db = db_manager

    async def acquire_morning_batch(
        self, run_date: str, forecast_cycle: str, trigger_kind: str
    ) -> tuple[str, bool]:
        """Resume only the unfinished batch for one explicit forecast context."""
        self._validate_context(forecast_cycle, trigger_kind)
        resumed_at = datetime.now().astimezone().isoformat()
        async with self._db.transaction():
            resumable_batch = await self._db.fetchone(
                "SELECT batch.morning_batch_id FROM ensemble_shadow_batches batch\n                   JOIN ensemble_shadow_batch_provenance provenance\n                     ON provenance.morning_batch_id = batch.morning_batch_id\n                  WHERE batch.run_date = ? AND batch.status = 'running'\n                    AND provenance.forecast_cycle = ?\n                    AND provenance.trigger_kind = ?",
                (run_date, forecast_cycle, trigger_kind),
            )
            if resumable_batch:
                await self._db.execute(
                    "UPDATE ensemble_shadow_batches SET last_resumed_at = ?\n                       WHERE morning_batch_id = ?",
                    (resumed_at, resumable_batch[0]),
                    auto_commit=False,
                )
                return (resumable_batch[0], True)
            other_running_batch = await self._db.fetchone(
                "SELECT morning_batch_id FROM ensemble_shadow_batches\n                   WHERE run_date = ? AND status = 'running'",
                (run_date,),
            )
            if other_running_batch:
                raise RuntimeError(
                    "unfinished morning batch belongs to a different or legacy forecast context"
                )
            batch_id = str(uuid.uuid4())
            await self._db.execute(
                "INSERT INTO ensemble_shadow_batches\n                   (morning_batch_id, run_date, status, last_resumed_at)\n                   VALUES (?, ?, 'running', ?)",
                (batch_id, run_date, resumed_at),
                auto_commit=False,
            )
            await self._db.execute(
                "INSERT INTO ensemble_shadow_batch_provenance (\n                       morning_batch_id, forecast_cycle, trigger_kind,\n                       promotion_eligible, contract_version, created_at\n                   ) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    forecast_cycle,
                    trigger_kind,
                    int(
                        forecast_cycle in {"pre_sunrise", "post_midnight"}
                        and trigger_kind == "scheduled"
                    ),
                    ENSEMBLE_SHADOW_PROVENANCE_CONTRACT_VERSION,
                    resumed_at,
                ),
                auto_commit=False,
            )
            return (batch_id, False)
        return None if True else None

    @staticmethod
    def _validate_context(forecast_cycle: str, trigger_kind: str) -> None:
        if forecast_cycle not in _FORECAST_CYCLES:
            raise ValueError("unsupported forecast_cycle")
        if trigger_kind not in _TRIGGER_KINDS:
            raise ValueError("unsupported trigger_kind")
        if forecast_cycle == "catchup" and trigger_kind != "catchup":
            raise ValueError("catchup forecast_cycle requires catchup trigger_kind")
        if forecast_cycle == "manual" and trigger_kind != "manual":
            raise ValueError("manual forecast_cycle requires manual trigger_kind")
        return None if True else None

    async def finish_morning_batch(
        self, morning_batch_id: str, succeeded: bool
    ) -> None:
        status = "completed" if succeeded else "failed"
        status_time_column = "completed_at" if succeeded else "failed_at"
        await self._db.execute(
            f"UPDATE ensemble_shadow_batches\n                SET status = ?, {status_time_column} = ?\n                WHERE morning_batch_id = ? AND status = 'running'",
            (status, datetime.now().astimezone().isoformat(), morning_batch_id),
        )

    async def capture_morning_batch(
        self, morning_batch_id: str, forecast_created_at: datetime
    ) -> int:
        """Capture one immutable shadow row for every hour in a morning batch."""
        prediction_rows = await self._db.fetchall(
            "SELECT hp.prediction_id, hp.target_datetime, hp.target_date, hp.target_hour,\n                      hp.prediction_method, hp.physics_kwh, hp.ai_kwh, hp.prediction_kwh,\n                      hp.tfs_kwh, hp.tfs_weight, pa.sun_elevation_deg,\n                      pa.sun_azimuth_deg, pa.hours_before_sunset, pa.day_progress_ratio,\n                      pa.theoretical_max_kwh, pw.solar_radiation_wm2,\n                      pw.direct_radiation, pw.diffuse_radiation, pw.clouds\n                 FROM hourly_predictions hp\n                 LEFT JOIN prediction_astronomy pa ON pa.prediction_id = hp.prediction_id\n                 LEFT JOIN prediction_weather pw\n                   ON pw.prediction_id = hp.prediction_id AND pw.weather_type = 'forecast'\n                WHERE hp.morning_batch_id = ?\n                  AND hp.prediction_method = 'AI + Physics'\n                ORDER BY hp.target_datetime",
            (morning_batch_id,),
        )
        if not prediction_rows:
            count_row = await self._db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE morning_batch_id = ?",
                (morning_batch_id,),
            )
            persisted_count = int(count_row[0]) if count_row and count_row[0] else 0
            if persisted_count:
                _LOGGER.info(
                    "Ensemble shadow batch %s skipped: %d persisted forecast rows, none with method 'AI + Physics'",
                    morning_batch_id,
                    persisted_count,
                )
            else:
                _LOGGER.warning(
                    "Ensemble shadow batch %s has no persisted forecast rows",
                    morning_batch_id,
                )
            return 0 if True else None
        captured_count = 0
        not_evaluable_count = 0
        async with self._db.transaction():
            for row in prediction_rows:
                decision = self._build_decision(row)
                if not decision["evaluable"]:
                    not_evaluable_count += 1
                await self._db.execute(
                    "INSERT OR IGNORE INTO ensemble_shadow_evaluations (\n                           shadow_evaluation_id, morning_batch_id, source_prediction_id,\n                           forecast_created_at, target_datetime, target_utc_epoch, target_date, target_hour,\n                           forecast_timezone, prediction_method, shadow_rule_version,\n                           physics_kwh, ai_kwh, prediction_kwh, tfs_kwh, tfs_weight,\n                           sun_elevation_deg, sun_azimuth_deg, hours_before_sunset,\n                           day_progress_ratio, forecast_ghi_wm2, direct_radiation_wm2,\n                           diffuse_radiation_wm2, forecast_clouds_pct,\n                           theoretical_max_kwh, shadow_evaluable,\n                           shadow_not_evaluable_reason, shadow_gate_triggered,\n                           shadow_prediction_kwh, production_prediction_kwh,\n                           decision_reason_json\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n                                 ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        morning_batch_id,
                        row[0],
                        forecast_created_at.isoformat(),
                        row[1],
                        self._target_utc_epoch(row[1]),
                        row[2],
                        row[3],
                        ENSEMBLE_SHADOW_TIMEZONE,
                        row[4],
                        ENSEMBLE_SHADOW_RULE_VERSION,
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        row[11],
                        row[12],
                        row[13],
                        row[14],
                        row[15],
                        row[16],
                        row[17],
                        row[18],
                        decision["evaluable"],
                        decision["not_evaluable_reason"],
                        decision["triggered"],
                        decision["shadow_prediction_kwh"],
                        row[7],
                        json.dumps(
                            decision["reason"], separators=(",", ":"), sort_keys=True
                        ),
                    ),
                    auto_commit=False,
                )
                captured_count += 1
                continue
        _LOGGER.info(
            "Ensemble shadow batch %s captured: %d hours, %d not evaluable",
            morning_batch_id,
            captured_count,
            not_evaluable_count,
        )
        return captured_count if True else None

    async def evaluate_prediction(self, prediction_id: str) -> int:
        """Append evaluation rows once actuals and quality flags are available."""
        actual_row = await self._db.fetchone(
            "SELECT actual_kwh, actual_measured_at, has_sensor_data,\n                      sensor_data_complete, manual_override, inverter_clipped,\n                      exclude_from_learning, exclude_from_clean_evaluation,\n                      mppt_throttled, mppt_throttle_reason,\n                      actual_backfill_blocked, data_quarantine_reason\n                 FROM hourly_predictions\n                WHERE prediction_id = ?",
            (prediction_id,),
        )
        if not actual_row:
            return 0
        shadow_rows = await self._db.fetchall(
            "SELECT shadow_evaluation_id, physics_kwh, production_prediction_kwh,\n                      shadow_prediction_kwh, shadow_evaluable\n                 FROM ensemble_shadow_evaluations\n                WHERE source_prediction_id = ?",
            (prediction_id,),
        )
        if not shadow_rows:
            return 0
        actual_kwh, invalid_actual_reason = self._valid_actual(actual_row[0])
        quality_flags = {
            "actual_kwh": actual_kwh,
            "manual_override": actual_row[4],
            "inverter_clipped": actual_row[5],
            "exclude_from_learning": actual_row[6],
            "exclude_from_clean_evaluation": actual_row[7],
            "mppt_throttled": actual_row[8],
            "mppt_throttle_reason": actual_row[9],
            "actual_backfill_blocked": actual_row[10],
            "data_quarantine_reason": actual_row[11],
        }
        clean_status, clean_exclusion_reason = (
            self._db.classify_hourly_clean_evaluation(quality_flags)
        )
        evaluated_at = datetime.now().astimezone().isoformat()
        actual_measured_at = actual_row[1]
        evaluated_count = 0
        async with self._db.transaction():
            for shadow_row in shadow_rows:
                actual_fingerprint = self._actual_fingerprint(
                    actual_row[0],
                    actual_row[1],
                    clean_status,
                    clean_exclusion_reason,
                    invalid_actual_reason,
                )
                existing_version = await self._db.fetchone(
                    "SELECT 1 FROM ensemble_shadow_evaluation_versions\n                       WHERE shadow_evaluation_id = ? AND actual_fingerprint = ?",
                    (shadow_row[0], actual_fingerprint),
                )
                if existing_version:
                    continue
                previous_version = await self._db.fetchone(
                    "SELECT evaluation_version_id, evaluation_version\n                       FROM ensemble_shadow_evaluation_versions\n                       WHERE shadow_evaluation_id = ?\n                       ORDER BY evaluation_version DESC LIMIT 1",
                    (shadow_row[0],),
                )
                version_number = int(previous_version[1]) + 1 if previous_version else 1
                status = (
                    "not_evaluable_invalid_actual"
                    if invalid_actual_reason
                    else (
                        "evaluated_shadow_not_evaluable"
                        if not shadow_row[4]
                        else "evaluated_clean" if clean_status else "evaluated_excluded"
                    )
                )
                production_absolute_error = (
                    abs(float(shadow_row[2]) - actual_kwh)
                    if actual_kwh is not None
                    else None
                )
                physics_absolute_error = (
                    abs(float(shadow_row[1]) - actual_kwh)
                    if shadow_row[1] is not None and actual_kwh is not None
                    else None
                )
                shadow_absolute_error = (
                    abs(float(shadow_row[3]) - actual_kwh)
                    if shadow_row[3] is not None and actual_kwh is not None
                    else None
                )
                avoided_error = (
                    production_absolute_error - shadow_absolute_error
                    if shadow_absolute_error is not None
                    else None
                )
                lost_ensemble_improvement = (
                    max(physics_absolute_error - production_absolute_error, 0.0)
                    if physics_absolute_error is not None
                    else None
                )
                await self._db.execute(
                    "INSERT INTO ensemble_shadow_evaluation_versions (\n                           evaluation_version_id, shadow_evaluation_id, evaluation_version,\n                           supersedes_evaluation_version_id, actual_fingerprint, evaluated_at, actual_kwh,\n                           actual_measured_at, clean_evaluation_status,\n                           clean_exclusion_reason, evaluation_status, evaluation_reason,\n                           final_absolute_error, physics_absolute_error,\n                           shadow_absolute_error, avoided_error,\n                           lost_ensemble_improvement\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        shadow_row[0],
                        version_number,
                        previous_version[0] if previous_version else None,
                        actual_fingerprint,
                        evaluated_at,
                        actual_kwh,
                        actual_measured_at,
                        clean_status,
                        clean_exclusion_reason,
                        status,
                        invalid_actual_reason,
                        production_absolute_error,
                        physics_absolute_error,
                        shadow_absolute_error,
                        avoided_error,
                        lost_ensemble_improvement,
                    ),
                    auto_commit=False,
                )
                evaluated_count += 1
                continue
        if evaluated_count:
            _LOGGER.debug(
                "Ensemble shadow evaluation completed for %s: %d batch rows",
                prediction_id,
                evaluated_count,
            )
        return evaluated_count

    @classmethod
    def _build_decision(cls, row: Any) -> dict[str, Any]:
        return cls.build_evening_challenger_decision(
            physics_kwh=row[5],
            prediction_kwh=row[7],
            tfs_weight=row[9],
            sun_elevation_deg=row[10],
            hours_before_sunset=row[12],
            day_progress_ratio=row[13],
        )

    @staticmethod
    def _finite_number(value: Any) -> Optional[float]:
        if value is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(number):
            return
        return number

    @classmethod
    def build_evening_challenger_decision(
        cls,
        *,
        physics_kwh: Any,
        prediction_kwh: Any,
        tfs_weight: Any,
        sun_elevation_deg: Any,
        hours_before_sunset: Any,
        day_progress_ratio: Any,
    ) -> dict[str, Any]:
        """Replay the evening-only challenger on frozen shadow inputs."""
        missing_reasons = []
        physics_value_kwh = cls._finite_number(physics_kwh)
        production_value_kwh = cls._finite_number(prediction_kwh)
        tfs_weight_value = cls._finite_number(tfs_weight)
        sun_elevation_value = cls._finite_number(sun_elevation_deg)
        if production_value_kwh is None:
            missing_reasons.append("prediction_kwh_missing")
        if sun_elevation_value is None:
            missing_reasons.append("sun_elevation_missing")
        hours_before_sunset_value = cls._finite_number(hours_before_sunset)
        day_progress_value = cls._finite_number(day_progress_ratio)
        if hours_before_sunset_value is None:
            missing_reasons.append("hours_before_sunset_missing")
        if day_progress_value is None:
            missing_reasons.append("day_progress_ratio_missing")
        evening_phase = (
            hours_before_sunset_value is not None
            and day_progress_value is not None
            and (0.0 <= hours_before_sunset_value <= EVENING_HOURS_BEFORE_SUNSET_MAX)
            and (EVENING_DAY_PROGRESS_MIN <= day_progress_value <= 1.0)
        )
        sun_geometry_eligible = (
            sun_elevation_value is not None
            and sun_elevation_value > EVENING_SUN_ELEVATION_MIN
            and (sun_elevation_value <= EVENING_SUN_ELEVATION_MAX)
        )
        gate_geometry_eligible = evening_phase and sun_geometry_eligible
        triggered = False
        shadow_prediction_kwh = None
        if not missing_reasons:
            if not gate_geometry_eligible:
                shadow_prediction_kwh = production_value_kwh
            elif tfs_weight_value is None:
                missing_reasons.append("tfs_weight_missing")
            elif tfs_weight_value < ENSEMBLE_SHADOW_TFS_WEIGHT_MIN:
                shadow_prediction_kwh = production_value_kwh
            elif physics_value_kwh is None:
                missing_reasons.append("physics_kwh_missing")
            else:
                triggered = True
                shadow_prediction_kwh = physics_value_kwh
        evaluable = not missing_reasons
        outcome = (
            "not_evaluable"
            if not evaluable
            else "triggered" if triggered else "not_triggered"
        )
        return {
            "evaluable": evaluable,
            "not_evaluable_reason": (
                ",".join(missing_reasons) if missing_reasons else None
            ),
            "triggered": triggered,
            "shadow_prediction_kwh": shadow_prediction_kwh,
            "reason": {
                "rule_version": EVENING_CHALLENGER_RULE_VERSION,
                "tfs_weight": tfs_weight_value,
                "tfs_weight_min": ENSEMBLE_SHADOW_TFS_WEIGHT_MIN,
                "sun_elevation_deg": sun_elevation_value,
                "sun_elevation_min": EVENING_SUN_ELEVATION_MIN,
                "sun_elevation_max": EVENING_SUN_ELEVATION_MAX,
                "hours_before_sunset": hours_before_sunset_value,
                "hours_before_sunset_max": EVENING_HOURS_BEFORE_SUNSET_MAX,
                "day_progress_ratio": day_progress_value,
                "day_progress_min": EVENING_DAY_PROGRESS_MIN,
                "evening_phase": evening_phase,
                "gate_geometry_eligible": gate_geometry_eligible,
                "outcome": outcome,
                "missing": missing_reasons,
            },
        }

    @classmethod
    def select_canonical_batch_rows(
        cls, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Keep one pre-sunrise scheduled batch row per target date/hour."""
        selected_rows = {}
        for candidate_row in rows:
            forecast_cycle = str(candidate_row.get("forecast_cycle") or "")
            trigger_kind = str(candidate_row.get("trigger_kind") or "")
            if (
                forecast_cycle != CANONICAL_SHADOW_FORECAST_CYCLE
                or trigger_kind != CANONICAL_SHADOW_TRIGGER_KIND
            ):
                continue
            target_date = str(candidate_row.get("target_date") or "")
            try:
                target_hour = int(candidate_row.get("target_hour"))
            except (TypeError, ValueError):
                continue
            if not target_date or not 0 <= target_hour <= 23:
                continue
            hour_key = (target_date, target_hour)
            normalized_row = dict(candidate_row)
            previous_row = selected_rows.get(hour_key)
            if previous_row is None:
                selected_rows[hour_key] = normalized_row
                continue
            previous_created_at = str(previous_row.get("forecast_created_at") or "")
            candidate_created_at = str(normalized_row.get("forecast_created_at") or "")
            previous_batch_id = str(previous_row.get("morning_batch_id") or "")
            candidate_batch_id = str(normalized_row.get("morning_batch_id") or "")
            if not (candidate_created_at, candidate_batch_id) < (
                previous_created_at,
                previous_batch_id,
            ):
                continue
            selected_rows[hour_key] = normalized_row
        return [selected_rows[hour_key] for hour_key in sorted(selected_rows)]

    @classmethod
    def _has_complete_day_coverage(cls, rows: Sequence[Mapping[str, Any]]) -> bool:
        if len(rows) < MIN_COMPLETE_DAY_HOURS:
            return False
        try:
            pass
        except (TypeError, ValueError):
            return False
        try:
            target_hours = [int(row.get("target_hour")) for row in rows]
        except (TypeError, ValueError):
            return False
        if target_hours != list(range(target_hours[0], target_hours[-1] + 1)):
            return False
        progress_values = [
            cls._finite_number(row.get("day_progress_ratio")) for row in rows
        ]
        if any(
            (
                progress is None or not 0.0 <= progress <= 1.0
                for progress in progress_values
            )
        ):
            return False
        valid_progress_values = [
            float(progress) for progress in progress_values if progress is not None
        ]
        progress_steps = [
            next_progress - previous_progress
            for previous_progress, next_progress in zip(
                valid_progress_values, valid_progress_values[1:]
            )
        ]
        if not progress_steps or any(
            (progress_step <= 0.0 for progress_step in progress_steps)
        ):
            return False
        median_progress_step = sorted(progress_steps)[len(progress_steps) // 2]
        if any(
            (
                progress_step
                > median_progress_step * COMPLETE_DAY_MAX_PROGRESS_STEP_FACTOR
                for progress_step in progress_steps
            )
        ):
            return False
        edge_tolerance = min(
            0.2, median_progress_step * COMPLETE_DAY_EDGE_INTERVAL_FACTOR
        )
        return (
            valid_progress_values[0] <= edge_tolerance
            and 1.0 - valid_progress_values[-1] <= edge_tolerance
        )

    @classmethod
    def score_evening_challenger(
        cls, rows: Sequence[Mapping[str, Any]], *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Pairwise production vs evening challenger on canonical clean hours."""
        canonical_rows = [
            row
            for row in cls.select_canonical_batch_rows(rows)
            if start_date <= str(row.get("target_date") or "") <= end_date
        ]
        rows_by_date = {}
        evening_production_errors = []
        evening_challenger_errors = []
        scored_hours = 0
        gated_hours = 0
        gated_days = 0
        for row in canonical_rows:
            rows_by_date.setdefault(str(row.get("target_date") or ""), []).append(row)
        clean_dates = []
        daily_production_errors = []
        daily_challenger_errors = []
        for target_date in sorted(rows_by_date):
            day_rows = sorted(
                rows_by_date[target_date], key=lambda row: int(row.get("target_hour"))
            )
            if not cls._has_complete_day_coverage(day_rows):
                continue
            paired_hours = []
            for row in day_rows:
                if not row.get("clean_evaluation_status"):
                    paired_hours = []
                    break
                actual_value_kwh = cls._finite_number(row.get("actual_kwh"))
                production_value_kwh = cls._finite_number(
                    row.get("production_prediction_kwh")
                )
                if actual_value_kwh is None or production_value_kwh is None:
                    paired_hours = []
                    break
                decision = cls.build_evening_challenger_decision(
                    physics_kwh=row.get("physics_kwh"),
                    prediction_kwh=production_value_kwh,
                    tfs_weight=row.get("tfs_weight"),
                    sun_elevation_deg=row.get("sun_elevation_deg"),
                    hours_before_sunset=row.get("hours_before_sunset"),
                    day_progress_ratio=row.get("day_progress_ratio"),
                )
                if not decision["evaluable"]:
                    paired_hours = []
                    break
                challenger_value_kwh = cls._finite_number(
                    decision["shadow_prediction_kwh"]
                )
                if challenger_value_kwh is None:
                    paired_hours = []
                    break
                paired_hours.append(
                    (
                        actual_value_kwh,
                        production_value_kwh,
                        challenger_value_kwh,
                        decision,
                    )
                )
                continue
            if len(paired_hours) != len(day_rows):
                continue
            clean_dates.append(target_date)
            daily_production_kwh = sum((paired_hour[1] for paired_hour in paired_hours))
            daily_challenger_kwh = sum((paired_hour[2] for paired_hour in paired_hours))
            daily_actual_kwh = sum((paired_hour[0] for paired_hour in paired_hours))
            daily_production_errors.append(abs(daily_production_kwh - daily_actual_kwh))
            daily_challenger_errors.append(abs(daily_challenger_kwh - daily_actual_kwh))
            scored_hours += len(paired_hours)
            day_has_gated_hour = False
            for (
                actual_value_kwh,
                production_value_kwh,
                challenger_value_kwh,
                decision,
            ) in paired_hours:
                if decision["triggered"]:
                    gated_hours += 1
                    day_has_gated_hour = True
                    evening_production_errors.append(
                        abs(production_value_kwh - actual_value_kwh)
                    )
                    evening_challenger_errors.append(
                        abs(challenger_value_kwh - actual_value_kwh)
                    )
            if not day_has_gated_hour:
                continue
            gated_days += 1
            continue
        evening_mae_production = (
            sum(evening_production_errors) / len(evening_production_errors)
            if evening_production_errors
            else None
        )
        evening_mae_challenger = (
            sum(evening_challenger_errors) / len(evening_challenger_errors)
            if evening_challenger_errors
            else None
        )
        daily_mae_production = (
            sum(daily_production_errors) / len(daily_production_errors)
            if daily_production_errors
            else None
        )
        daily_mae_challenger = (
            sum(daily_challenger_errors) / len(daily_challenger_errors)
            if daily_challenger_errors
            else None
        )
        eligible = (
            evening_mae_production is not None
            and evening_mae_challenger is not None
            and (daily_mae_production is not None)
            and (daily_mae_challenger is not None)
            and (gated_hours >= MIN_PERIOD_GATED_HOURS)
            and (gated_days >= MIN_PERIOD_GATED_DAYS)
            and (len(clean_dates) >= MIN_PERIOD_CLEAN_DAYS)
        )
        evening_better = eligible and evening_mae_challenger < evening_mae_production
        daily_non_inferior = (
            eligible
            and daily_mae_challenger
            <= daily_mae_production + DAILY_MAE_NON_INFERIORITY_EPS_KWH
        )
        return {
            "rule_version": EVENING_CHALLENGER_RULE_VERSION,
            "start_date": start_date,
            "end_date": end_date,
            "canonical_cycle": CANONICAL_SHADOW_FORECAST_CYCLE,
            "canonical_trigger": CANONICAL_SHADOW_TRIGGER_KIND,
            "scored_hours": scored_hours,
            "gated_hours": gated_hours,
            "gated_days": gated_days,
            "clean_days": len(clean_dates),
            "min_complete_day_hours": MIN_COMPLETE_DAY_HOURS,
            "complete_day_edge_interval_factor": COMPLETE_DAY_EDGE_INTERVAL_FACTOR,
            "complete_day_max_progress_step_factor": COMPLETE_DAY_MAX_PROGRESS_STEP_FACTOR,
            "min_period_clean_days": MIN_PERIOD_CLEAN_DAYS,
            "min_period_gated_days": MIN_PERIOD_GATED_DAYS,
            "min_period_gated_hours": MIN_PERIOD_GATED_HOURS,
            "evening_mae_production": evening_mae_production,
            "evening_mae_challenger": evening_mae_challenger,
            "daily_mae_production": daily_mae_production,
            "daily_mae_challenger": daily_mae_challenger,
            "daily_mae_eps_kwh": DAILY_MAE_NON_INFERIORITY_EPS_KWH,
            "eligible": eligible,
            "evening_better": evening_better,
            "daily_non_inferior": daily_non_inferior,
            "go": bool(evening_better and daily_non_inferior),
        }

    @classmethod
    def decide_evening_challenger_go(
        cls, period_scores: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """GO only when every frozen period is evening-better and daily non-inferior."""
        reason_codes = []
        if len(period_scores) < 2:
            reason_codes.append("second_period_missing")
        valid_periods = []
        for period_score in period_scores:
            period_label = (
                f"{period_score.get('start_date')}_{period_score.get('end_date')}"
            )
            start_date_text = str(period_score.get("start_date") or "")
            end_date_text = str(period_score.get("end_date") or "")
            try:
                period_start = datetime.fromisoformat(start_date_text).date()
                period_end = datetime.fromisoformat(end_date_text).date()
            except ValueError:
                reason_codes.append(f"invalid_period:{period_label}")
                continue
            if period_start > period_end:
                reason_codes.append(f"invalid_period:{period_label}")
                continue
            valid_periods.append((period_start.isoformat(), period_end.isoformat()))
            if not period_score.get("eligible"):
                reason_codes.append(f"ineligible:{period_label}")
                continue
            if not period_score.get("evening_better"):
                reason_codes.append(f"evening_not_better:{period_label}")
            if period_score.get("daily_non_inferior"):
                continue
            reason_codes.append(f"daily_inferior:{period_label}")
            continue
        for period_index, current_period in enumerate(sorted(valid_periods)):
            for previous_period in sorted(valid_periods)[:period_index]:
                if current_period[0] <= previous_period[1]:
                    reason_codes.append(
                        f"periods_overlap:{previous_period[0]}_{previous_period[1]}:{current_period[0]}_{current_period[1]}"
                    )
        return {
            "go_no_go": "GO" if not reason_codes else "NO_GO",
            "reason_codes": reason_codes,
            "rule_version": EVENING_CHALLENGER_RULE_VERSION,
            "daily_mae_eps_kwh": DAILY_MAE_NON_INFERIORITY_EPS_KWH,
        }

    CANONICAL_EVENING_CHALLENGER_SQL = "\n        SELECT ev.target_date, ev.target_hour, ev.physics_kwh,\n               ev.production_prediction_kwh, ev.tfs_weight, ev.sun_elevation_deg,\n               ev.hours_before_sunset, ev.day_progress_ratio, ev.forecast_created_at,\n               ev.morning_batch_id, provenance.forecast_cycle, provenance.trigger_kind,\n               versions.actual_kwh, versions.clean_evaluation_status\n          FROM ensemble_shadow_evaluations ev\n          JOIN ensemble_shadow_batch_provenance provenance\n            ON provenance.morning_batch_id = ev.morning_batch_id\n          JOIN ensemble_shadow_evaluation_versions versions\n            ON versions.shadow_evaluation_id = ev.shadow_evaluation_id\n          JOIN (\n                SELECT shadow_evaluation_id, MAX(evaluation_version) AS evaluation_version\n                  FROM ensemble_shadow_evaluation_versions\n                 GROUP BY shadow_evaluation_id\n               ) latest\n            ON latest.shadow_evaluation_id = versions.shadow_evaluation_id\n           AND latest.evaluation_version = versions.evaluation_version\n    "

    @classmethod
    def load_evening_challenger_rows(cls, rows: Sequence[Any]) -> list[dict[str, Any]]:
        challenger_rows = []
        for row in rows:
            challenger_rows.append(
                {
                    "target_date": row[0],
                    "target_hour": row[1],
                    "physics_kwh": row[2],
                    "production_prediction_kwh": row[3],
                    "tfs_weight": row[4],
                    "sun_elevation_deg": row[5],
                    "hours_before_sunset": row[6],
                    "day_progress_ratio": row[7],
                    "forecast_created_at": row[8],
                    "morning_batch_id": row[9],
                    "forecast_cycle": row[10],
                    "trigger_kind": row[11],
                    "actual_kwh": row[12],
                    "clean_evaluation_status": bool(row[13]),
                }
            )
        return challenger_rows

    @staticmethod
    def _target_utc_epoch(value: Any) -> int:
        target_datetime = datetime.fromisoformat(str(value))
        if target_datetime.tzinfo is None:
            target_datetime = target_datetime.replace(
                tzinfo=ZoneInfo(ENSEMBLE_SHADOW_TIMEZONE)
            )
        return int(target_datetime.timestamp())

    @staticmethod
    def _valid_actual(value: Any) -> tuple[Optional[float], Optional[str]]:
        if value is None:
            return (None, "actual_missing")
        try:
            actual_value_kwh = float(value)
        except (TypeError, ValueError):
            return (None, "actual_not_numeric")
        if not math.isfinite(actual_value_kwh):
            return (None, "actual_not_finite")
        if actual_value_kwh < 0:
            return (None, "actual_negative")
        return (actual_value_kwh, None)

    @staticmethod
    def _actual_fingerprint(*values: Any) -> str:
        serialized_values = json.dumps(
            values, default=str, separators=(",", ":"), sort_keys=False
        )
        return hashlib.sha256(serialized_values.encode()).hexdigest()
