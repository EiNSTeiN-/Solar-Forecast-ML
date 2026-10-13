# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class ModelCandidateShadowHandler:

    async def capture_morning_run(
        self, morning_batch_id: str, packet: Mapping[str, Any]
    ) -> int:
        """Atomically capture one validated morning candidate run."""
        if not isinstance(morning_batch_id, str) or not morning_batch_id:
            raise ValueError("morning_batch_id is required")
        _var_var_0 = self._normalize_packet(packet)
        _var_var_1 = self._canonical_json(_var_var_0)
        async with self._db.transaction():
            _var_var_2 = await self._db.fetchone(
                "SELECT terminal_reason\n                     FROM ai_model_candidate_terminal_events\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT 'evaluated'\n                     FROM ai_model_evaluations_v2\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT 'evaluated'\n                     FROM ai_model_evaluations\n                    WHERE candidate_id = ?\n                    UNION ALL\n                   SELECT disposition.disposition\n                     FROM ai_model_candidate_dispositions disposition\n                    WHERE disposition.candidate_id = ?\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                           WHERE reconsideration.candidate_id = disposition.candidate_id\n                             AND disposition.disposition = 'migration_superseded'\n                             AND disposition.replacement_candidate_id =\n                                 reconsideration.keeper_candidate_id\n                             AND reconsideration.candidate_id !=\n                                 reconsideration.keeper_candidate_id)\n                    UNION ALL\n                   SELECT 'reconsideration_closed'\n                     FROM ai_model_candidate_reconsideration_closures\n                    WHERE candidate_id = ?\n                    LIMIT 1",
                (_var_var_0["candidate_id"],) * 5,
            )
            if _var_var_2:
                _LOGGER.info(
                    "Candidate shadow capture skipped for terminal candidate %s (%s)",
                    _var_var_0["candidate_id"],
                    _var_var_2[0],
                )
                return 0
            _var_var_3 = await self._db.fetchone(
                "SELECT shadow_run_id, run_date, candidate_id, forecast_anchor,\n                          champion_artifact_hash, candidate_artifact_hash,\n                          input_snapshot_json, input_snapshot_hash,\n                          policy_snapshot_json, policy_snapshot_hash, contract_version,\n                          input_snapshot_blob, policy_snapshot_blob, snapshot_codec\n                     FROM ai_model_shadow_runs WHERE morning_batch_id = ?",
                (morning_batch_id,),
            )
            if _var_var_3:
                _var_var_4 = await self._persisted_evidence(_var_var_3[0], _var_var_3)
                if self._canonical_json(_var_var_4) == _var_var_1:
                    return 0
                raise ValueError("morning batch already has different shadow evidence")
            _var_var_5 = None
            if (
                _var_var_0["contract_version"]
                == MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION
            ):
                _var_var_5 = await self._db.fetchone(
                    "SELECT forecast_cycle, trigger_kind, promotion_eligible,\n                              contract_version, created_at\n                         FROM ensemble_shadow_batch_provenance\n                        WHERE morning_batch_id = ?",
                    (morning_batch_id,),
                )
                if not _var_var_5:
                    raise ValueError("V2 shadow evidence requires batch provenance")
            for _var_var_6 in _var_var_0["hours"]:
                _var_var_7 = await self._db.fetchone(
                    "SELECT target_date, target_hour, prediction_kwh\n                         FROM hourly_predictions WHERE prediction_id = ?",
                    (_var_var_6["source_prediction_id"],),
                )
                if not _var_var_7:
                    raise ValueError("source prediction is unavailable")
                if (
                    _var_var_7[0] != _var_var_6["target_date"]
                    or int(_var_var_7[1]) != _var_var_6["target_hour"]
                ):
                    raise ValueError(
                        "source prediction target does not match shadow evidence"
                    )
                try:
                    _var_var_8 = float(_var_var_7[2])
                except (TypeError, ValueError) as _var_var_9:
                    raise ValueError(
                        "persisted champion prediction is invalid"
                    ) from _var_var_9
                if (
                    not math.isfinite(_var_var_8)
                    or _var_var_8 < 0
                    or abs(_var_var_8 - _var_var_6["champion_prediction"])
                    > _PREDICTION_TOLERANCE
                ):
                    raise ValueError(
                        "persisted champion prediction does not match shadow evidence"
                    )
            _var_var_10 = str(uuid.uuid4())
            try:
                _var_var_11 = seal_shadow_snapshot(
                    _var_var_0["input_snapshot_json"], _var_var_10, "input_snapshot"
                )
                _var_var_12 = seal_shadow_snapshot(
                    _var_var_0["policy_snapshot_json"], _var_var_10, "policy_snapshot"
                )
            except artifact_crypto.ArtifactCryptoError:
                note_unbound_artifact_key("shadow snapshot")
                return 0
            _var_var_13 = [
                (
                    compress_shadow_json(_var_var_6["champion_result_json"]),
                    compress_shadow_json(_var_var_6["candidate_result_json"]),
                )
                for _var_var_6 in _var_var_0["hours"]
            ]
            _var_var_14 = (
                len(_var_var_11)
                + len(_var_var_12)
                + sum(
                    (
                        len(_var_var_17) + len(_var_var_18)
                        for _var_var_17, _var_var_18 in _var_var_13
                    )
                )
            )
            if not await self._db.enforce_model_candidate_evidence_budget(
                _var_var_0["candidate_id"],
                additional_runs=1,
                additional_predictions=len(_var_var_0["hours"]),
                additional_payload_bytes=_var_var_14,
            ):
                return 0
            await self._db.execute(
                "INSERT INTO ai_model_shadow_runs (\n                       shadow_run_id, morning_batch_id, run_date, candidate_id,\n                       forecast_anchor, champion_artifact_hash, candidate_artifact_hash,\n                       input_snapshot_json, input_snapshot_blob, input_snapshot_hash,\n                       policy_snapshot_json, policy_snapshot_blob, policy_snapshot_hash,\n                       snapshot_codec, contract_version, created_at\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?, ?, ?)",
                (
                    _var_var_10,
                    morning_batch_id,
                    _var_var_0["run_date"],
                    _var_var_0["candidate_id"],
                    _var_var_0["forecast_anchor"],
                    _var_var_0["champion_artifact_hash"],
                    _var_var_0["candidate_artifact_hash"],
                    _var_var_11,
                    _var_var_0["input_snapshot_hash"],
                    _var_var_12,
                    _var_var_0["policy_snapshot_hash"],
                    SHADOW_JSON_CODEC_AESGCM,
                    _var_var_0["contract_version"],
                    datetime.now().astimezone().isoformat(),
                ),
                auto_commit=False,
            )
            for _var_var_6, (_var_var_15, _var_var_16) in zip(
                _var_var_0["hours"], _var_var_13
            ):
                await self._db.execute(
                    "INSERT INTO ai_model_shadow_predictions (\n                           shadow_prediction_id, shadow_run_id, source_prediction_id,\n                           target_date, target_hour, horizon, champion_prediction,\n                           candidate_prediction, weather_regime, input_fingerprint,\n                           policy_fingerprint, champion_result_json, champion_result_blob,\n                           champion_result_hash, candidate_result_json,\n                           candidate_result_blob, candidate_result_hash, result_codec\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        _var_var_10,
                        _var_var_6["source_prediction_id"],
                        _var_var_6["target_date"],
                        _var_var_6["target_hour"],
                        _var_var_6["horizon"],
                        _var_var_6["champion_prediction"],
                        _var_var_6["candidate_prediction"],
                        _var_var_6["weather_regime"],
                        _var_var_6["input_fingerprint"],
                        _var_var_6["policy_fingerprint"],
                        _var_var_15,
                        _var_var_6["champion_result_hash"],
                        _var_var_16,
                        _var_var_6["candidate_result_hash"],
                        SHADOW_JSON_CODEC_ZLIB,
                    ),
                    auto_commit=False,
                )
            if _var_var_5:
                if (
                    _var_var_0["forecast_cycle"] != _var_var_5[0]
                    or _var_var_0["trigger_kind"] != _var_var_5[1]
                ):
                    raise ValueError(
                        "V2 packet context does not match batch provenance"
                    )
                await self._db.execute(
                    "INSERT INTO ai_model_shadow_run_provenance (\n                           shadow_run_id, morning_batch_id, forecast_cycle,\n                           trigger_kind, promotion_eligible, contract_version, created_at\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_10,
                        morning_batch_id,
                        _var_var_5[0],
                        _var_var_5[1],
                        _var_var_5[2],
                        MODEL_CANDIDATE_SHADOW_V2_CONTRACT_VERSION,
                        _var_var_5[4],
                    ),
                    auto_commit=False,
                )
        return len(_var_var_0["hours"])
