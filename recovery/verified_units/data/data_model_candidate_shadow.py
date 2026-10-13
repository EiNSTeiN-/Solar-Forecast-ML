# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Immutable shadow evidence collection for non-serving model candidates."""
from __future__ import annotations
import logging
import math
import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Mapping, Optional
from ..ai.ai_model_promotion import (
    CycleAwareModelPromotionEvaluationConfigV3,
    CycleCriticalSlice,
    CycleNoiseBand,
    FrozenCyclePairedTotalStackSample,
    evaluate_cycle_aware_model_promotion_v3,
)
from .data_model_promotion_policy import (
    MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS,
    MODEL_PROMOTION_FORECAST_CYCLES,
    MODEL_PROMOTION_WEATHER_REGIMES,
    build_cycle_aware_promotion_policy_config_v3,
)
from .db_manager import (
    SHADOW_JSON_CODEC_AESGCM,
    SHADOW_JSON_CODEC_ZLIB,
    artifact_crypto,
    canonical_stable_hash,
    canonical_stable_json,
    compress_shadow_json,
    decode_shadow_json,
    note_unbound_artifact_key,
    protected_identity,
    seal_shadow_snapshot,
    validate_hourly_actual_measurement_time,
)

_LOGGER = logging.getLogger(__name__)
_SHA256_LENGTH = 64
_PREDICTION_TOLERANCE = 1e-06
_PRODUCTION_THRESHOLD = 0.01
MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION = "model_candidate_shadow_v2"
_REQUIRED_PACKET_KEYS = frozenset(
    {
        "hours",
        "candidate_id",
        "input_snapshot",
        "forecast_anchor",
        "policy_snapshot",
        "contract_version",
        "input_snapshot_hash",
        "policy_snapshot_hash",
        "champion_artifact_hash",
        "candidate_artifact_hash",
    }
)
_V2_CONTEXT_KEYS = frozenset({"forecast_cycle", "trigger_kind"})
_REQUIRED_HOUR_KEYS = frozenset(
    {
        "horizon",
        "target_date",
        "target_hour",
        "weather_regime",
        "champion_result",
        "candidate_result",
        "input_fingerprint",
        "policy_fingerprint",
        "source_prediction_id",
    }
)


class ModelCandidateShadowHandler:
    """Write and evaluate immutable candidate-versus-champion evidence."""

    def __init__(self, db_manager: Any) -> None:
        self._db = db_manager

    async def capture_morning_run(
        self, morning_batch_id: str, packet: Mapping[str, Any]
    ) -> int:
        """Atomically capture one validated morning candidate run."""
        if not isinstance(morning_batch_id, str) or not morning_batch_id:
            raise ValueError("morning_batch_id is required")
        normalized_packet = self._normalize_packet(packet)
        packet_json = self._canonical_json(normalized_packet)
        async with self._db.transaction():
            terminal_row = await self._db.fetchone(
                "SELECT terminal_reason\n                     FROM ai_model_candidate_terminal_events\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT 'evaluated'\n                     FROM ai_model_evaluations_v2\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT 'evaluated'\n                     FROM ai_model_evaluations\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT disposition.disposition\n                     FROM ai_model_candidate_dispositions disposition\n                    WHERE disposition.candidate_id = ?\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                           WHERE reconsideration.candidate_id = disposition.candidate_id\n                             AND disposition.disposition = 'migration_superseded'\n                             AND disposition.replacement_candidate_id =\n                                 reconsideration.keeper_candidate_id\n                             AND reconsideration.candidate_id !=\n                                 reconsideration.keeper_candidate_id)\n                    UNION ALL\n                   SELECT 'reconsideration_closed'\n                     FROM ai_model_candidate_reconsideration_closures\n                    WHERE candidate_id = ?\n                    LIMIT 1",
                (normalized_packet["candidate_id"],) * 5,
            )
            if terminal_row:
                _LOGGER.info(
                    "Candidate shadow capture skipped for terminal candidate %s (%s)",
                    normalized_packet["candidate_id"],
                    terminal_row[0],
                )
                return 0
            existing_run = await self._db.fetchone(
                "SELECT shadow_run_id, run_date, candidate_id, forecast_anchor,\n                          champion_artifact_hash, candidate_artifact_hash,\n                          input_snapshot_json, input_snapshot_hash,\n                          policy_snapshot_json, policy_snapshot_hash, contract_version,\n                          input_snapshot_blob, policy_snapshot_blob, snapshot_codec\n                     FROM ai_model_shadow_runs WHERE morning_batch_id = ?",
                (morning_batch_id,),
            )
            if existing_run:
                persisted_evidence = await self._persisted_evidence(
                    existing_run[0], existing_run
                )
                if self._canonical_json(persisted_evidence) == packet_json:
                    return 0
                raise ValueError("morning batch already has different shadow evidence")
            batch_provenance = None
            if (
                normalized_packet["contract_version"]
                == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION
            ):
                batch_provenance = await self._db.fetchone(
                    "SELECT forecast_cycle, trigger_kind, promotion_eligible,\n                              contract_version, created_at\n                         FROM ensemble_shadow_batch_provenance\n                        WHERE morning_batch_id = ?",
                    (morning_batch_id,),
                )
                if not batch_provenance:
                    raise ValueError("V2 shadow evidence requires batch provenance")
            for hour_entry in normalized_packet["hours"]:
                source_prediction_row = await self._db.fetchone(
                    "SELECT target_date, target_hour, prediction_kwh\n                         FROM hourly_predictions WHERE prediction_id = ?",
                    (hour_entry["source_prediction_id"],),
                )
                if not source_prediction_row:
                    raise ValueError("source prediction is unavailable")
                if (
                    source_prediction_row[0] != hour_entry["target_date"]
                    or int(source_prediction_row[1]) != hour_entry["target_hour"]
                ):
                    raise ValueError(
                        "source prediction target does not match shadow evidence"
                    )
                try:
                    persisted_champion_prediction = float(source_prediction_row[2])
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "persisted champion prediction is invalid"
                    ) from error
                if (
                    not math.isfinite(persisted_champion_prediction)
                    or persisted_champion_prediction < 0
                    or abs(
                        persisted_champion_prediction
                        - hour_entry["champion_prediction"]
                    )
                    > _PREDICTION_TOLERANCE
                ):
                    raise ValueError(
                        "persisted champion prediction does not match shadow evidence"
                    )
            shadow_run_id = str(uuid.uuid4())
            try:
                sealed_input_snapshot = seal_shadow_snapshot(
                    normalized_packet["input_snapshot_json"],
                    shadow_run_id,
                    "input_snapshot",
                )
                sealed_policy_snapshot = seal_shadow_snapshot(
                    normalized_packet["policy_snapshot_json"],
                    shadow_run_id,
                    "policy_snapshot",
                )
            except artifact_crypto.ArtifactCryptoError:
                note_unbound_artifact_key("shadow snapshot")
                return 0
            compressed_results = [
                (
                    compress_shadow_json(hour_entry["champion_result_json"]),
                    compress_shadow_json(hour_entry["candidate_result_json"]),
                )
                for hour_entry in normalized_packet["hours"]
            ]
            payload_bytes = (
                len(sealed_input_snapshot)
                + len(sealed_policy_snapshot)
                + sum(
                    (
                        len(champion_blob) + len(candidate_blob)
                        for champion_blob, candidate_blob in compressed_results
                    )
                )
            )
            if not await self._db.enforce_model_candidate_evidence_budget(
                normalized_packet["candidate_id"],
                additional_runs=1,
                additional_predictions=len(normalized_packet["hours"]),
                additional_payload_bytes=payload_bytes,
            ):
                return 0
            await self._db.execute(
                "INSERT INTO ai_model_shadow_runs (\n                       shadow_run_id, morning_batch_id, run_date, candidate_id,\n                       forecast_anchor, champion_artifact_hash, candidate_artifact_hash,\n                       input_snapshot_json, input_snapshot_blob, input_snapshot_hash,\n                       policy_snapshot_json, policy_snapshot_blob, policy_snapshot_hash,\n                       snapshot_codec, contract_version, created_at\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?, ?, ?)",
                (
                    shadow_run_id,
                    morning_batch_id,
                    normalized_packet["run_date"],
                    normalized_packet["candidate_id"],
                    normalized_packet["forecast_anchor"],
                    normalized_packet["champion_artifact_hash"],
                    normalized_packet["candidate_artifact_hash"],
                    sealed_input_snapshot,
                    normalized_packet["input_snapshot_hash"],
                    sealed_policy_snapshot,
                    normalized_packet["policy_snapshot_hash"],
                    SHADOW_JSON_CODEC_AESGCM,
                    normalized_packet["contract_version"],
                    datetime.now().astimezone().isoformat(),
                ),
                auto_commit=False,
            )
            for hour_entry, (champion_blob, candidate_blob) in zip(
                normalized_packet["hours"], compressed_results
            ):
                await self._db.execute(
                    "INSERT INTO ai_model_shadow_predictions (\n                           shadow_prediction_id, shadow_run_id, source_prediction_id,\n                           target_date, target_hour, horizon, champion_prediction,\n                           candidate_prediction, weather_regime, input_fingerprint,\n                           policy_fingerprint, champion_result_json, champion_result_blob,\n                           champion_result_hash, candidate_result_json,\n                           candidate_result_blob, candidate_result_hash, result_codec\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        shadow_run_id,
                        hour_entry["source_prediction_id"],
                        hour_entry["target_date"],
                        hour_entry["target_hour"],
                        hour_entry["horizon"],
                        hour_entry["champion_prediction"],
                        hour_entry["candidate_prediction"],
                        hour_entry["weather_regime"],
                        hour_entry["input_fingerprint"],
                        hour_entry["policy_fingerprint"],
                        champion_blob,
                        hour_entry["champion_result_hash"],
                        candidate_blob,
                        hour_entry["candidate_result_hash"],
                        SHADOW_JSON_CODEC_ZLIB,
                    ),
                    auto_commit=False,
                )
            if batch_provenance:
                if (
                    normalized_packet["forecast_cycle"] != batch_provenance[0]
                    or normalized_packet["trigger_kind"] != batch_provenance[1]
                ):
                    raise ValueError(
                        "V2 packet context does not match batch provenance"
                    )
                await self._db.execute(
                    "INSERT INTO ai_model_shadow_run_provenance (\n                           shadow_run_id, morning_batch_id, forecast_cycle,\n                           trigger_kind, promotion_eligible, contract_version, created_at\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        shadow_run_id,
                        morning_batch_id,
                        batch_provenance[0],
                        batch_provenance[1],
                        batch_provenance[2],
                        MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION,
                        batch_provenance[4],
                    ),
                    auto_commit=False,
                )
        return len(normalized_packet["hours"])

    async def evaluate_prediction(self, prediction_id: str) -> int:
        """Append current actual evidence for every shadow row of a prediction."""
        if not isinstance(prediction_id, str) or not prediction_id:
            raise ValueError("prediction_id is required")
        actual_row = await self._db.fetchone(
            "SELECT actual_kwh, actual_measured_at, manual_override, inverter_clipped,\n                      exclude_from_learning, exclude_from_clean_evaluation, mppt_throttled,\n                      mppt_throttle_reason, actual_backfill_blocked, data_quarantine_reason,\n                      has_weather_alert, weather_alert_type, target_date, target_hour,\n                      target_datetime\n                 FROM hourly_predictions WHERE prediction_id = ?",
            (prediction_id,),
        )
        if not actual_row:
            return 0
        actual_kwh, invalid_actual_reason = self._valid_actual(actual_row[0])
        invalid_measurement_reason = self._invalid_actual_measurement_reason(
            actual_row[1], actual_row[12], actual_row[13], actual_row[14]
        )
        if not invalid_actual_reason:
            if invalid_measurement_reason:
                invalid_actual_reason = invalid_measurement_reason
        evaluation_inputs = {
            "actual_kwh": actual_kwh,
            "manual_override": actual_row[2],
            "inverter_clipped": actual_row[3],
            "exclude_from_learning": actual_row[4],
            "exclude_from_clean_evaluation": actual_row[5],
            "mppt_throttled": actual_row[6],
            "mppt_throttle_reason": actual_row[7],
            "actual_backfill_blocked": actual_row[8],
            "data_quarantine_reason": actual_row[9],
            "has_weather_alert": actual_row[10],
            "weather_alert_type": actual_row[11],
        }
        clean_eligible, exclusion_reason = self._db.classify_hourly_clean_evaluation(
            evaluation_inputs
        )
        if invalid_actual_reason:
            clean_eligible, exclusion_reason = (False, invalid_actual_reason)
        evaluation_status = (
            "evaluated_clean" if clean_eligible else "evaluated_excluded"
        )
        if invalid_actual_reason:
            evaluation_status = "invalid_actual"
        rows = await self._db.fetchall(
            "SELECT prediction.shadow_prediction_id, run.candidate_id\n                 FROM ai_model_shadow_predictions prediction\n                 JOIN ai_model_shadow_runs run\n                   ON run.shadow_run_id = prediction.shadow_run_id\n                WHERE prediction.source_prediction_id = ?\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                       WHERE terminal.candidate_id = run.candidate_id)",
            (prediction_id,),
        )
        versions_written = 0
        async with self._db.transaction():
            pending_versions = []
            for row in rows:
                actual_evidence = {
                    "actual_kwh": actual_kwh,
                    "actual_measured_at": actual_row[1],
                    "clean_eligible": clean_eligible,
                    "exclusion_reason": exclusion_reason,
                    "evaluation_status": evaluation_status,
                }
                latest_version = await self._db.fetchone(
                    "SELECT evaluation_version, actual_fingerprint, actual_kwh,\n                              actual_measured_at, clean_eligible, exclusion_reason,\n                              evaluation_status\n                         FROM ai_model_shadow_actual_versions\n                         WHERE shadow_prediction_id = ?\n                         ORDER BY evaluation_version DESC LIMIT 1",
                    (row[0],),
                )
                if (
                    latest_version
                    and latest_version[2] == actual_kwh
                    and (latest_version[3] == actual_row[1])
                    and (bool(latest_version[4]) is bool(clean_eligible))
                    and (latest_version[5] == exclusion_reason)
                    and (latest_version[6] == evaluation_status)
                ):
                    continue
                evaluation_version = int(latest_version[0]) + 1 if latest_version else 1
                actual_fingerprint = self._hash(
                    {
                        **actual_evidence,
                        "evaluation_version": evaluation_version,
                        "previous_actual_fingerprint": (
                            latest_version[1] if latest_version else None
                        ),
                    }
                )
                pending_versions.append(
                    (row[0], row[1], actual_fingerprint, evaluation_version)
                )
                continue
            versions_by_candidate = {}
            for (
                planned_shadow_prediction_id,
                candidate_id,
                planned_actual_fingerprint,
                planned_version,
            ) in pending_versions:
                versions_by_candidate[candidate_id] = (
                    versions_by_candidate.get(candidate_id, 0) + 1
                )
            allowed_candidates = set()
            for candidate_id, version_count in versions_by_candidate.items():
                if await self._db.enforce_model_candidate_evidence_budget(
                    candidate_id, additional_actual_versions=version_count
                ):
                    allowed_candidates.add(candidate_id)
            for (
                shadow_prediction_id,
                candidate_id,
                actual_fingerprint,
                evaluation_version,
            ) in pending_versions:
                if candidate_id not in allowed_candidates:
                    continue
                await self._db.execute(
                    "INSERT INTO ai_model_shadow_actual_versions (\n                           actual_version_id, shadow_prediction_id, evaluation_version,\n                           actual_fingerprint, evaluated_at, actual_kwh, actual_measured_at,\n                           clean_eligible, exclusion_reason, evaluation_status\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        shadow_prediction_id,
                        evaluation_version,
                        actual_fingerprint,
                        datetime.now().astimezone().isoformat(),
                        actual_kwh,
                        actual_row[1],
                        int(clean_eligible),
                        exclusion_reason,
                        evaluation_status,
                    ),
                    auto_commit=False,
                )
                versions_written += 1
        return versions_written if True else None

    async def refresh_actual_evidence(self, candidate_id: Optional[str] = None) -> int:
        """Idempotently mirror the current hourly truth into shadow actual versions."""
        if candidate_id is not None:
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("candidate_id must be a non-empty string")
        rows = await self._db.fetchall(
            "SELECT DISTINCT prediction.source_prediction_id\n                 FROM ai_model_shadow_predictions prediction\n                 JOIN ai_model_shadow_runs run\n                   ON run.shadow_run_id = prediction.shadow_run_id\n                 JOIN hourly_predictions hourly\n                   ON hourly.prediction_id = prediction.source_prediction_id\n                WHERE (? IS NULL OR run.candidate_id = ?)\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                       WHERE terminal.candidate_id = run.candidate_id)\n                ORDER BY prediction.source_prediction_id",
            (candidate_id, candidate_id),
        )
        versions_written = 0
        for row in rows:
            versions_written += await self.evaluate_prediction(str(row[0]))
        return versions_written

    async def evaluate_ready_candidate(
        self,
        min_days: int = 7,
        min_hours_per_day: int = 4,
        candidate_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Evaluate only complete, promotion-eligible V2 cycle evidence."""
        if isinstance(min_days, bool) or not isinstance(min_days, int) or min_days < 1:
            raise ValueError("min_days must be positive")
        if (
            isinstance(min_hours_per_day, bool)
            or not isinstance(min_hours_per_day, int)
            or min_hours_per_day < 1
        ):
            raise ValueError("min_hours_per_day must be positive")
        if candidate_id is not None:
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("candidate_id must be a non-empty string")
        pending_candidate = await self._fetch_pending_v2_candidate(candidate_id)
        if pending_candidate:
            return await self._evaluate_ready_candidate_v2(
                pending_candidate[0],
                pending_candidate[1],
                pending_candidate[2],
                min_days,
                min_hours_per_day,
            )

    async def gate_readiness(
        self,
        candidate_id: Optional[str] = None,
        min_days: int = 7,
        min_hours_per_day: int = 4,
    ) -> dict[str, Any]:
        """Return the WAIT/ready report without recording a promotion decision."""
        if isinstance(min_days, bool) or not isinstance(min_days, int) or min_days < 1:
            raise ValueError("min_days must be positive")
        if (
            isinstance(min_hours_per_day, bool)
            or not isinstance(min_hours_per_day, int)
            or min_hours_per_day < 1
        ):
            raise ValueError("min_hours_per_day must be positive")
        if candidate_id is not None:
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("candidate_id must be a non-empty string")
        pending_candidate = await self._fetch_pending_v2_candidate(candidate_id)
        if not pending_candidate:
            return self._empty_readiness(min_days, min_hours_per_day)
        rows = await self._fetch_v2_cycle_evidence_rows(
            pending_candidate[0], pending_candidate[1], pending_candidate[2]
        )
        selected_samples, readiness = self._readiness_from_rows(
            rows, min_days, min_hours_per_day
        )
        return readiness

    async def _fetch_pending_v2_candidate(
        self, candidate_id: Optional[str]
    ) -> Optional[tuple[Any, ...]]:
        return await self._db.fetchone(
            "SELECT run.candidate_id, run.champion_artifact_hash, run.candidate_artifact_hash\n                 FROM ai_model_shadow_runs run\n                 JOIN ai_model_candidates candidate\n                   ON candidate.candidate_id = run.candidate_id\n                 JOIN ensemble_shadow_batches batch ON batch.morning_batch_id = run.morning_batch_id\n                 JOIN ai_model_shadow_run_provenance provenance\n                   ON provenance.shadow_run_id = run.shadow_run_id\n                WHERE batch.status = 'completed'\n                  AND run.contract_version = 'model_candidate_shadow_v2'\n                  AND provenance.contract_version = 'model_candidate_shadow_v2'\n                  AND provenance.promotion_eligible = 1\n                  AND provenance.trigger_kind = 'scheduled'\n                  AND provenance.forecast_cycle IN ('post_midnight', 'pre_sunrise')\n                  AND (? IS NULL OR run.candidate_id = ?)\n                  AND candidate.status = 'candidate'\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_dispositions disposition\n                       WHERE disposition.candidate_id = run.candidate_id\n                         AND NOT EXISTS (\n                             SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                              WHERE reconsideration.candidate_id = disposition.candidate_id\n                                AND disposition.disposition = 'migration_superseded'\n                                AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                         )\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations evaluation\n                       WHERE evaluation.candidate_id = run.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = run.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                       WHERE closure.candidate_id = run.candidate_id\n                  )\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                       WHERE terminal.candidate_id = run.candidate_id\n                  )\n                ORDER BY run.created_at, run.shadow_run_id LIMIT 1",
            (candidate_id, candidate_id),
        )

    async def _fetch_v2_cycle_evidence_rows(
        self, candidate_id: str, champion_hash: str, candidate_hash: str
    ) -> list[Any]:
        return await self._db.fetchall(
            "WITH ranked_runs AS (\n                   SELECT run.shadow_run_id, run.forecast_anchor, provenance.forecast_cycle,\n                          ROW_NUMBER() OVER (\n                              PARTITION BY run.run_date, provenance.forecast_cycle\n                              ORDER BY COALESCE(batch.completed_at, run.created_at),\n                                       run.created_at, run.shadow_run_id\n                          ) AS cycle_rank\n                     FROM ai_model_shadow_runs run\n                     JOIN ensemble_shadow_batches batch\n                       ON batch.morning_batch_id = run.morning_batch_id\n                     JOIN ai_model_shadow_run_provenance provenance\n                       ON provenance.shadow_run_id = run.shadow_run_id\n                    WHERE batch.status = 'completed'\n                      AND run.contract_version = 'model_candidate_shadow_v2'\n                      AND provenance.contract_version = 'model_candidate_shadow_v2'\n                      AND provenance.promotion_eligible = 1\n                      AND provenance.trigger_kind = 'scheduled'\n                      AND provenance.forecast_cycle IN ('post_midnight', 'pre_sunrise')\n                      AND run.candidate_id = ?\n                      AND run.champion_artifact_hash = ?\n                      AND run.candidate_artifact_hash = ?\n                 )\n                 SELECT prediction.target_date, prediction.target_hour,\n                        ranked.forecast_cycle, actual.actual_kwh,\n                        prediction.champion_prediction, prediction.candidate_prediction,\n                        actual.clean_eligible, ranked.forecast_anchor,\n                        prediction.input_fingerprint, prediction.policy_fingerprint,\n                        prediction.weather_regime, prediction.horizon,\n                        ranked.shadow_run_id, prediction.shadow_prediction_id,\n                        actual.actual_version_id\n                   FROM ranked_runs ranked\n                   JOIN ai_model_shadow_predictions prediction\n                     ON prediction.shadow_run_id = ranked.shadow_run_id\n                   JOIN ai_model_shadow_actual_versions actual\n                     ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                    AND actual.evaluation_version = (\n                        SELECT MAX(version.evaluation_version)\n                          FROM ai_model_shadow_actual_versions version\n                         WHERE version.shadow_prediction_id = prediction.shadow_prediction_id\n                    )\n                  WHERE ranked.cycle_rank = 1\n                    AND prediction.horizon = 'day_0'\n                  ORDER BY prediction.target_date, prediction.target_hour,\n                           ranked.forecast_cycle",
            (candidate_id, champion_hash, candidate_hash),
        )

    @classmethod
    def _empty_readiness(cls, min_days: int, min_hours_per_day: int) -> dict[str, Any]:
        return {
            "eligible_days": 0,
            "policy_eligible_days": 0,
            "required_days": min_days,
            "wait_reason": "INSUFFICIENT_ELIGIBLE_DAYS",
            "unlabeled_hours": 0,
            "rare_regimes": {},
            "eligible_common_hours": 0,
            "minimum_common_hours_per_cycle": min_hours_per_day,
        }

    @classmethod
    def _readiness_from_rows(
        cls, rows: list[Any], min_days: int, min_hours_per_day: int
    ) -> tuple[
        list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]], dict[str, Any]
    ]:
        eligible_samples = cls._eligible_cycle_samples(rows, min_hours_per_day)
        eligible_day_count = len(
            {sample.target_date for sample, unused_identity in eligible_samples}
        )
        policy_samples, coverage_details = cls._policy_legal_cycle_samples(
            eligible_samples, min_days, min_hours_per_day
        )
        if eligible_day_count < min_days:
            wait_reason = "INSUFFICIENT_ELIGIBLE_DAYS"
        elif not policy_samples:
            wait_reason = "INSUFFICIENT_REGIME_COVERAGE"
        else:
            wait_reason = None
        readiness = {
            "eligible_days": eligible_day_count,
            "policy_eligible_days": coverage_details["eligible_days"],
            "required_days": min_days,
            "wait_reason": wait_reason,
            "unlabeled_hours": coverage_details["unlabeled_hours"],
            "rare_regimes": coverage_details["rare_regimes"],
            "eligible_common_hours": len(eligible_samples) // 2,
            "minimum_common_hours_per_cycle": min_hours_per_day,
        }
        return (policy_samples, readiness)

    async def _evaluate_ready_candidate_v2(
        self,
        candidate_id: str,
        champion_hash: str,
        candidate_hash: str,
        min_days: int,
        min_hours_per_day: int,
    ) -> Optional[dict[str, Any]]:
        rows = await self._fetch_v2_cycle_evidence_rows(
            candidate_id, champion_hash, candidate_hash
        )
        policy_samples, readiness = self._readiness_from_rows(
            rows, min_days, min_hours_per_day
        )
        wait_reason = readiness["wait_reason"]
        if wait_reason == "INSUFFICIENT_ELIGIBLE_DAYS":
            _LOGGER.info(
                "Model candidate shadow readiness: candidate=%s wait_reason=%s eligible_common_days=%d/%d eligible_common_hours=%d minimum_common_hours_per_cycle=%d",
                candidate_id,
                wait_reason,
                readiness["eligible_days"],
                readiness["required_days"],
                readiness["eligible_common_hours"],
                min_hours_per_day,
            )
            return
        if wait_reason == "INSUFFICIENT_REGIME_COVERAGE":
            _LOGGER.info(
                "Model candidate shadow readiness: candidate=%s wait_reason=%s eligible_policy_days=%d/%d unlabeled_hours=%d rare_regimes=%s minimum_common_hours_per_cycle=%d",
                candidate_id,
                wait_reason,
                readiness["policy_eligible_days"],
                readiness["required_days"],
                readiness["unlabeled_hours"],
                readiness["rare_regimes"],
                min_hours_per_day,
            )
            return
        evaluation_samples = [policy_entry[0] for policy_entry in policy_samples]
        policy_payload = build_cycle_aware_promotion_policy_config_v3(
            evaluation_samples,
            champion_trained_on=await self._db.get_active_model_trained_on(),
        )
        evaluation_config = self._cycle_aware_config_from_policy(policy_payload)
        decision = evaluate_cycle_aware_model_promotion_v3(
            evaluation_samples, evaluation_config
        ).to_dict()
        _LOGGER.info(
            "Model candidate shadow evaluation decided: candidate=%s decision=%s comparison=%s reason_codes=%s champion_age_days=%s non_inferiority_enabled=%s daily_energy_mae_delta=%.4f allowed_total_regression=%.4f",
            candidate_id,
            decision["go_no_go"],
            decision["comparison_outcome"],
            decision["reason_codes"],
            decision["champion_age_days"],
            decision["non_inferiority_enabled"],
            decision["candidate_minus_champion_daily_energy_mae"],
            decision["allowed_total_daily_energy_regression"],
        )
        evaluation_id = await self._db.record_model_candidate_evaluation_v2(
            candidate_id,
            champion_hash,
            policy_payload,
            [
                {**asdict(sample), **sample_identity}
                for sample, sample_identity in policy_samples
            ],
            decision,
        )
        return {
            "evaluation_id": evaluation_id,
            "decision": decision["go_no_go"],
            "candidate_id": candidate_id,
        }

    @staticmethod
    def _canonical_promotion_regime(value: Any) -> str | None:
        if isinstance(value, str) and value in MODEL_PROMOTION_WEATHER_REGIMES:
            return value
        return None if True else None

    @staticmethod
    def _cycle_aware_config_from_policy(
        config_payload: Mapping[str, Any],
    ) -> CycleAwareModelPromotionEvaluationConfigV3:
        return CycleAwareModelPromotionEvaluationConfigV3(
            min_independent_eligible_days=config_payload[
                "min_independent_eligible_days"
            ],
            min_common_clean_production_hours_per_cycle=config_payload[
                "min_common_clean_production_hours_per_cycle"
            ],
            bootstrap_seed=config_payload["bootstrap_seed"],
            critical_slice_min_independent_days=config_payload[
                "critical_slice_min_independent_days"
            ],
            critical_slices=tuple(
                (
                    CycleCriticalSlice(
                        name=critical_slice["name"],
                        forecast_cycle=critical_slice["forecast_cycle"],
                        weather_regime=critical_slice["weather_regime"],
                        min_independent_days=critical_slice["min_independent_days"],
                        allowed_daily_energy_regression=critical_slice[
                            "allowed_daily_energy_regression"
                        ],
                    )
                    for critical_slice in config_payload["critical_slices"]
                )
            ),
            policy_contract_version=config_payload["policy_contract_version"],
            cycle_noise_bands=tuple(
                (
                    CycleNoiseBand(
                        forecast_cycle=noise_band["forecast_cycle"],
                        allowed_daily_energy_regression=noise_band[
                            "allowed_daily_energy_regression"
                        ],
                    )
                    for noise_band in config_payload["cycle_noise_bands"]
                )
            ),
            allowed_total_daily_energy_regression=config_payload[
                "allowed_total_daily_energy_regression"
            ],
            champion_min_age_days_for_non_inferiority=config_payload[
                "champion_min_age_days_for_non_inferiority"
            ],
            champion_age_days=config_payload["champion_age_days"],
        )

    @classmethod
    def _paired_cycle_hours(
        cls, selected: list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]]
    ) -> dict[
        tuple[str, int],
        dict[str, tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]],
    ]:
        paired_by_date_hour = {}
        for sample, sample_identity in selected:
            paired_by_date_hour.setdefault(
                (sample.target_date, sample.target_hour), {}
            )[sample.forecast_cycle] = (sample, sample_identity)
        return paired_by_date_hour

    @classmethod
    def _samples_with_minimum_hours(
        cls,
        paired: Mapping[
            tuple[str, int],
            Mapping[str, tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]],
        ],
        min_hours_per_day: int,
    ) -> list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]]:
        hours_by_date = {}
        for (target_date, hour_entry), cycle_pairs in paired.items():
            if any(
                (cycle not in cycle_pairs for cycle in MODEL_PROMOTION_FORECAST_CYCLES)
            ):
                continue
            hours_by_date.setdefault(target_date, []).append(hour_entry)
        eligible_samples = []
        for target_date in sorted(hours_by_date):
            common_hours = sorted(hours_by_date[target_date])
            if len(common_hours) < min_hours_per_day:
                continue
            for hour_entry in common_hours:
                cycle_pairs = paired[target_date, hour_entry]
                eligible_samples.extend(
                    (cycle_pairs[cycle] for cycle in MODEL_PROMOTION_FORECAST_CYCLES)
                )
        return eligible_samples

    @classmethod
    def _policy_legal_cycle_samples(
        cls,
        selected: list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]],
        min_days: int,
        min_hours_per_day: int,
    ) -> tuple[
        list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]], dict[str, Any]
    ]:
        paired_by_date_hour = cls._paired_cycle_hours(selected)
        unlabeled_hour_count = 0
        labeled_pairs = {}
        for target_key, cycle_pairs in paired_by_date_hour.items():
            if any(
                (cycle not in cycle_pairs for cycle in MODEL_PROMOTION_FORECAST_CYCLES)
            ):
                continue
            if any(
                (
                    cls._canonical_promotion_regime(
                        cycle_pairs[cycle][0].weather_regime
                    )
                    is None
                    for cycle in MODEL_PROMOTION_FORECAST_CYCLES
                )
            ):
                unlabeled_hour_count += 1
                continue
            labeled_pairs[target_key] = cycle_pairs
        remaining_pairs = labeled_pairs
        rare_regimes = {}
        powered_regimes = set()
        policy_samples = []
        for iteration in range(len(MODEL_PROMOTION_WEATHER_REGIMES) + 1):
            policy_samples = cls._samples_with_minimum_hours(
                remaining_pairs, min_hours_per_day
            )
            regime_days = {}
            for sample, unused_identity in policy_samples:
                active_regime = cls._canonical_promotion_regime(sample.weather_regime)
                if active_regime is not None:
                    regime_days.setdefault(
                        (sample.forecast_cycle, active_regime), set()
                    ).add(sample.target_date)
            observed_regimes = {
                active_regime for forecast_cycle, active_regime in regime_days
            }
            powered_regimes = {
                active_regime
                for active_regime in observed_regimes
                if all(
                    (
                        len(regime_days.get((cycle, active_regime), set()))
                        >= MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS
                        for cycle in MODEL_PROMOTION_FORECAST_CYCLES
                    )
                )
            }
            for active_regime in sorted(observed_regimes - powered_regimes):
                rare_regimes[active_regime] = {
                    cycle: len(regime_days.get((cycle, active_regime), set()))
                    for cycle in MODEL_PROMOTION_FORECAST_CYCLES
                }
            filtered_pairs = {
                target_key: cycle_pairs
                for target_key, cycle_pairs in remaining_pairs.items()
                if all(
                    (
                        cls._canonical_promotion_regime(
                            cycle_pairs[cycle][0].weather_regime
                        )
                        in powered_regimes
                        for cycle in MODEL_PROMOTION_FORECAST_CYCLES
                    )
                )
            }
            if set(filtered_pairs) == set(remaining_pairs):
                break
            remaining_pairs = filtered_pairs
            continue
        policy_eligible_day_count = len(
            {sample.target_date for sample, unused_identity in policy_samples}
        )
        coverage_details = {
            "eligible_days": policy_eligible_day_count,
            "required_days": min_days,
            "unlabeled_hours": unlabeled_hour_count,
            "rare_regimes": rare_regimes,
            "powered_regimes": sorted(powered_regimes),
        }
        if policy_eligible_day_count < min_days:
            return ([], coverage_details)
        return (policy_samples, coverage_details)

    @staticmethod
    def _eligible_cycle_samples(
        rows: list[Any], min_hours_per_day: int
    ) -> list[tuple[FrozenCyclePairedTotalStackSample, dict[str, str]]]:
        samples_by_date_cycle = {}
        for row in rows:
            prediction_values = (row[3], row[4], row[5])
            if (
                not bool(row[6])
                or not row[3] is not None
                or float(row[3]) <= _PRODUCTION_THRESHOLD
                or any((value is None for value in prediction_values))
                or (
                    not all(
                        (math.isfinite(float(value)) for value in prediction_values)
                    )
                )
                or any((float(value) < 0 for value in prediction_values[1:]))
            ):
                continue
            sample = FrozenCyclePairedTotalStackSample(
                target_date=row[0],
                target_hour=int(row[1]),
                forecast_cycle=row[2],
                actual=float(row[3]),
                champion_prediction=float(row[4]),
                candidate_prediction=float(row[5]),
                clean_eligible=True,
                is_production_hour=True,
                forecast_anchor=row[7],
                input_fingerprint=row[8],
                policy_fingerprint=row[9],
                weather_regime=row[10],
                horizon=row[11],
            )
            sample_identity = {
                "shadow_run_id": row[12],
                "shadow_prediction_id": row[13],
                "actual_version_id": row[14],
            }
            samples_by_date_cycle.setdefault(sample.target_date, {}).setdefault(
                sample.forecast_cycle, {}
            )[sample.target_hour] = (sample, sample_identity)
            continue
        eligible_samples = []
        for target_date in sorted(samples_by_date_cycle):
            cycle_pairs = samples_by_date_cycle[target_date]
            if any(
                (cycle not in cycle_pairs for cycle in ("post_midnight", "pre_sunrise"))
            ):
                continue
            common_hours = set(cycle_pairs["post_midnight"]) & set(
                cycle_pairs["pre_sunrise"]
            )
            if len(common_hours) < min_hours_per_day:
                continue
            for cycle in ("post_midnight", "pre_sunrise"):
                eligible_samples.extend(
                    (cycle_pairs[cycle][hour] for hour in sorted(common_hours))
                )
        return eligible_samples

    def _normalize_packet(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, Mapping):
            raise ValueError("incomplete model candidate shadow packet")
        contract_version = self._required_text(
            packet.get("contract_version"), "contract_version"
        )
        if contract_version not in {
            "model_candidate_shadow_v1",
            MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION,
        }:
            raise ValueError("unsupported model candidate shadow contract")
        required_keys = _REQUIRED_PACKET_KEYS
        if contract_version == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION:
            required_keys |= _V2_CONTEXT_KEYS
        if set(packet) != required_keys:
            raise ValueError("incomplete model candidate shadow packet")
        candidate_id = self._required_text(packet["candidate_id"], "candidate_id")
        forecast_anchor = self._timezone_datetime(
            packet["forecast_anchor"], "forecast_anchor"
        )
        champion_artifact_hash = self._sha256(
            packet["champion_artifact_hash"], "champion_artifact_hash"
        )
        candidate_artifact_hash = self._sha256(
            packet["candidate_artifact_hash"], "candidate_artifact_hash"
        )
        input_snapshot_json = self._canonical_json(packet["input_snapshot"])
        policy_snapshot_json = self._canonical_json(packet["policy_snapshot"])
        input_snapshot_hash = self._sha256(
            packet["input_snapshot_hash"], "input_snapshot_hash"
        )
        policy_snapshot_hash = self._sha256(
            packet["policy_snapshot_hash"], "policy_snapshot_hash"
        )
        forecast_cycle = None
        trigger_kind = None
        if contract_version == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION:
            forecast_cycle = self._required_text(
                packet["forecast_cycle"], "forecast_cycle"
            )
            trigger_kind = self._required_text(packet["trigger_kind"], "trigger_kind")
            if forecast_cycle not in {
                "manual",
                "catchup",
                "pre_sunrise",
                "post_midnight",
            }:
                raise ValueError("invalid forecast_cycle")
            if trigger_kind not in {"manual", "catchup", "scheduled"}:
                raise ValueError("invalid trigger_kind")
        if (
            self._hash(packet["input_snapshot"]) != input_snapshot_hash
            or self._hash(packet["policy_snapshot"]) != policy_snapshot_hash
        ):
            raise ValueError("snapshot hash does not match canonical snapshot")
        if not isinstance(packet["hours"], list) or not packet["hours"]:
            raise ValueError("hours must be a non-empty list")
        normalized_hours = sorted(
            (self._normalize_hour(hour_packet) for hour_packet in packet["hours"]),
            key=lambda value: (value["target_date"], value["target_hour"]),
        )
        target_keys = [
            (hour_entry["target_date"], hour_entry["target_hour"])
            for hour_entry in normalized_hours
        ]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("duplicate target date/hour shadow evidence")
        source_prediction_ids = [
            hour_entry["source_prediction_id"] for hour_entry in normalized_hours
        ]
        if len(source_prediction_ids) != len(set(source_prediction_ids)):
            raise ValueError("duplicate source prediction shadow evidence")
        normalized_packet = {
            "run_date": forecast_anchor.date().isoformat(),
            "candidate_id": candidate_id,
            "forecast_anchor": forecast_anchor.isoformat(),
            "champion_artifact_hash": champion_artifact_hash,
            "candidate_artifact_hash": candidate_artifact_hash,
            "input_snapshot_json": input_snapshot_json,
            "input_snapshot_hash": input_snapshot_hash,
            "policy_snapshot_json": policy_snapshot_json,
            "policy_snapshot_hash": policy_snapshot_hash,
            "contract_version": contract_version,
            "hours": normalized_hours,
        }
        if contract_version == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION:
            normalized_packet.update(
                forecast_cycle=forecast_cycle, trigger_kind=trigger_kind
            )
        return normalized_packet

    def _normalize_hour(self, hour: Any) -> dict[str, Any]:
        if not isinstance(hour, Mapping) or set(hour) != _REQUIRED_HOUR_KEYS:
            raise ValueError("incomplete shadow hour")
        target_date = self._date(hour["target_date"])
        target_hour = hour["target_hour"]
        if (
            isinstance(target_hour, bool)
            or not isinstance(target_hour, int)
            or (not 0 <= target_hour <= 23)
        ):
            raise ValueError("target_hour must be between 0 and 23")
        champion_result_json = self._canonical_json(hour["champion_result"])
        candidate_result_json = self._canonical_json(hour["candidate_result"])
        champion_prediction = self._result_prediction(
            hour["champion_result"], "champion_result"
        )
        candidate_prediction = self._result_prediction(
            hour["candidate_result"], "candidate_result"
        )
        return {
            "source_prediction_id": self._required_text(
                hour["source_prediction_id"], "source_prediction_id"
            ),
            "target_date": target_date,
            "target_hour": target_hour,
            "horizon": self._required_text(hour["horizon"], "horizon"),
            "weather_regime": self._optional_text(
                hour["weather_regime"], "weather_regime"
            ),
            "input_fingerprint": self._sha256(
                hour["input_fingerprint"], "input_fingerprint"
            ),
            "policy_fingerprint": self._sha256(
                hour["policy_fingerprint"], "policy_fingerprint"
            ),
            "champion_prediction": champion_prediction,
            "candidate_prediction": candidate_prediction,
            "champion_result_json": champion_result_json,
            "champion_result_hash": self._hash(hour["champion_result"]),
            "candidate_result_json": candidate_result_json,
            "candidate_result_hash": self._hash(hour["candidate_result"]),
        }

    async def _persisted_evidence(self, shadow_run_id: str, run: Any) -> dict[str, Any]:
        rows = await self._db.fetchall(
            "SELECT source_prediction_id, target_date, target_hour, horizon,\n                      champion_prediction, candidate_prediction, weather_regime,\n                      input_fingerprint, policy_fingerprint, champion_result_json,\n                      champion_result_hash, candidate_result_json, candidate_result_hash,\n                      champion_result_blob, candidate_result_blob, result_codec\n                 FROM ai_model_shadow_predictions WHERE shadow_run_id = ?\n                 ORDER BY target_date, target_hour",
            (shadow_run_id,),
        )
        input_snapshot_json = decode_shadow_json(
            run[6],
            run[11],
            run[13],
            run[7],
            "ai_model_shadow_runs",
            protected_identity(run[0], "input_snapshot"),
        )
        policy_snapshot_json = decode_shadow_json(
            run[8],
            run[12],
            run[13],
            run[9],
            "ai_model_shadow_runs",
            protected_identity(run[0], "policy_snapshot"),
        )
        hours = []
        for row in rows:
            decoded_values = list(row[:13])
            decoded_values[9] = decode_shadow_json(row[9], row[13], row[15], row[10])
            decoded_values[11] = decode_shadow_json(row[11], row[14], row[15], row[12])
            hours.append(
                dict(
                    zip(
                        (
                            "source_prediction_id",
                            "target_date",
                            "target_hour",
                            "horizon",
                            "champion_prediction",
                            "candidate_prediction",
                            "weather_regime",
                            "input_fingerprint",
                            "policy_fingerprint",
                            "champion_result_json",
                            "champion_result_hash",
                            "candidate_result_json",
                            "candidate_result_hash",
                        ),
                        decoded_values,
                    )
                )
            )
        persisted_packet = {
            "run_date": run[1],
            "candidate_id": run[2],
            "forecast_anchor": run[3],
            "champion_artifact_hash": run[4],
            "candidate_artifact_hash": run[5],
            "input_snapshot_json": input_snapshot_json,
            "input_snapshot_hash": run[7],
            "policy_snapshot_json": policy_snapshot_json,
            "policy_snapshot_hash": run[9],
            "contract_version": run[10],
            "hours": hours,
        }
        if run[10] == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION:
            run_provenance = await self._db.fetchone(
                "SELECT forecast_cycle, trigger_kind\n                     FROM ai_model_shadow_run_provenance\n                    WHERE shadow_run_id = ?",
                (shadow_run_id,),
            )
            if not run_provenance:
                raise ValueError("V2 shadow evidence is missing run provenance")
            persisted_packet.update(
                forecast_cycle=run_provenance[0], trigger_kind=run_provenance[1]
            )
        return persisted_packet

    def _canonical_json(self, value: Any) -> str:
        canonicalize = getattr(self._db, "canonical_stable_json", canonical_stable_json)
        return canonicalize(value)

    def _hash(self, value: Any) -> str:
        hash_value = getattr(self._db, "canonical_stable_hash", canonical_stable_hash)
        return hash_value(value)

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} is required")
        return value

    @staticmethod
    def _optional_text(value: Any, label: str) -> Optional[str]:
        if value is None:
            return
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be non-empty text or null")
        return value

    def _sha256(self, value: Any, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != _SHA256_LENGTH
            or value != value.lower()
            or any((character not in "0123456789abcdef" for character in value))
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _date(value: Any) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as error:
            raise ValueError("target_date must be an ISO calendar date") from error

    @staticmethod
    def _timezone_datetime(value: Any, label: str) -> datetime:
        try:
            parsed_datetime = datetime.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be an ISO datetime") from error
        if parsed_datetime.tzinfo is None or parsed_datetime.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone")
        return parsed_datetime

    @staticmethod
    def _result_prediction(result: Any, label: str) -> float:
        if not isinstance(result, Mapping) or "production_kwh" not in result:
            raise ValueError(f"{label} requires production_kwh")
        production_kwh = result["production_kwh"]
        if (
            isinstance(production_kwh, bool)
            or not isinstance(production_kwh, (int, float))
            or (not math.isfinite(float(production_kwh)))
            or (production_kwh < 0)
        ):
            raise ValueError(f"{label}.production_kwh must be finite and non-negative")
        return float(production_kwh)

    @staticmethod
    def _valid_actual(value: Any) -> tuple[Optional[float], Optional[str]]:
        if value is None:
            return (None, "actual_missing")
        if isinstance(value, bool):
            return (None, "actual_not_numeric")
        try:
            actual_kwh = float(value)
        except (TypeError, ValueError):
            return (None, "actual_not_numeric")
        if not math.isfinite(actual_kwh):
            return (None, "actual_not_finite")
        if actual_kwh < 0:
            return (None, "actual_negative")
        return (actual_kwh, None)

    def _invalid_actual_measurement_reason(
        self, measured_at: Any, target_date: Any, target_hour: Any, target_datetime: Any
    ) -> Optional[str]:
        """Reject absent, pre-target, naive, malformed, or future measurements."""
        try:
            validate_hourly_actual_measurement_time(
                measured_at, target_datetime, target_date, target_hour
            )
        except ValueError as error:
            return str(error)
