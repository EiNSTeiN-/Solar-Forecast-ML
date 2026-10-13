# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


def daily_summary_evaluation_unchanged(
    stored: Sequence[Any], stats: Mapping[str, Any]
) -> bool:
    """True when a stored summary row (in field order) already carries these stats."""
    for _var_var_49, _var_var_50 in enumerate(DAILY_SUMMARY_EVALUATION_FIELDS):
        _var_var_51, _var_var_52 = (stored[_var_var_49], stats[_var_var_50])
        if _var_var_51 is None or _var_var_52 is None:
            if _var_var_51 is not _var_var_52:
                return False
            continue
        if isinstance(_var_var_52, float) or isinstance(_var_var_51, float):
            try:
                if not math.isclose(
                    float(_var_var_51), float(_var_var_52), rel_tol=0.0, abs_tol=1e-09
                ):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if _var_var_51 != _var_var_52:
            return False
    return True


class DatabaseManager:

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
        _var_var_98 = " OR ".join(
            (
                f"substr({_var_var_106}, 1, 1) IN ('{{', '[')"
                for _var_var_106 in payload_columns
            )
        )
        _var_var_99 = ", ".join((*id_columns, *payload_columns))
        while True:
            async with self.transaction():
                await self.execute(
                    f"DROP TRIGGER IF EXISTS {update_trigger}", auto_commit=False
                )
                _var_var_100 = await self.fetchall(
                    f"SELECT {_var_var_99} FROM {table}\n                         WHERE {_var_var_98}\n                         LIMIT {ARTIFACT_SEAL_BATCH}"
                )
                for _var_var_101 in _var_var_100:
                    _var_var_102 = [
                        _var_var_101[_var_var_49]
                        for _var_var_49 in range(len(id_columns))
                    ]
                    _var_var_103 = []
                    _var_var_104 = []
                    for _var_var_105, _var_var_106 in enumerate(payload_columns):
                        _var_var_107 = _var_var_101[len(id_columns) + _var_var_105]
                        if (
                            not isinstance(_var_var_107, str)
                            or _var_var_107[:1] not in "{["
                        ):
                            continue
                        _var_var_103.append(f"{_var_var_106} = ?")
                        _var_var_108 = list(_var_var_102)
                        if include_column_in_identity:
                            _var_var_108.append(_var_var_106)
                        _var_var_104.append(
                            seal_protected_text(
                                _var_var_107, table, protected_identity(*_var_var_108)
                            )
                        )
                        if (
                            table == "ai_model_candidate_artifacts"
                            and _var_var_106 == "weights_json"
                        ):
                            _var_var_103.append("input_size = ?")
                            _var_var_104.append(
                                artifact_cleartext_input_size(_var_var_107)
                            )
                    if not _var_var_103:
                        continue
                    _var_var_104.extend(_var_var_102)
                    _var_var_109 = " AND ".join(
                        (f"{_var_var_106} = ?" for _var_var_106 in id_columns)
                    )
                    await self.execute(
                        f"UPDATE {table} SET {', '.join(_var_var_103)} WHERE {_var_var_109}",
                        tuple(_var_var_104),
                        auto_commit=False,
                    )
                    continue
                await self._restore_schema_trigger(update_trigger, auto_commit=False)
            if len(_var_var_100) < ARTIFACT_SEAL_BATCH:
                if table == "ai_model_candidate_artifacts":
                    await self._copy_candidate_model_input_size(update_trigger)
                return

    async def _seal_model_weight_rows(self) -> None:
        """Seal leftover plaintext in ai_model_weights.weight_data."""
        if not await self._table_exists("ai_model_weights"):
            return
        while True:
            _var_var_100 = await self.fetchall(
                f"SELECT weight_type, weight_data FROM ai_model_weights\n                    WHERE substr(weight_data, 1, 1) IN ('{{', '[')\n                    LIMIT {ARTIFACT_SEAL_BATCH}"
            )
            if not _var_var_100:
                return
            async with self.transaction():
                for _var_var_110, _var_var_111 in _var_var_100:
                    if (
                        not isinstance(_var_var_111, str)
                        or _var_var_111[:1] not in "{["
                    ):
                        continue
                    _var_var_112 = seal_protected_text(
                        _var_var_111, "ai_model_weights", str(_var_var_110)
                    )
                    await self.execute(
                        "UPDATE ai_model_weights\n                              SET weight_data = ?, updated_at = ?\n                            WHERE weight_type = ?",
                        (_var_var_112, SafeDateTimeUtil.now(), _var_var_110),
                        auto_commit=False,
                    )
            if len(_var_var_100) < ARTIFACT_SEAL_BATCH:
                return

    async def _seal_shadow_runs_in_place(self) -> None:
        """Same batch shape as shadow compaction: drop trigger, rewrite, restore."""
        _var_var_122 = set()
        while True:
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_runs_append_only_update",
                    auto_commit=False,
                )
                _var_var_123 = ""
                _var_var_124 = [SHADOW_JSON_CODEC_AESGCM]
                if _var_var_122:
                    _var_var_123 = (
                        " AND shadow_run_id NOT IN ("
                        + ", ".join(("?" for _var_var_127 in _var_var_122))
                        + ")"
                    )
                    _var_var_124.extend(sorted(_var_var_122))
                _var_var_100 = await self.fetchall(
                    f"SELECT shadow_run_id, input_snapshot_json, input_snapshot_blob,\n                              input_snapshot_hash, policy_snapshot_json,\n                              policy_snapshot_blob, policy_snapshot_hash, snapshot_codec\n                         FROM ai_model_shadow_runs\n                        WHERE (snapshot_codec != ?\n                           OR length(input_snapshot_json) > 0\n                           OR length(policy_snapshot_json) > 0)\n                          {_var_var_123}\n                        ORDER BY shadow_run_id\n                        LIMIT {ARTIFACT_SEAL_BATCH}",
                    tuple(_var_var_124),
                )
                if not _var_var_100:
                    await self._restore_schema_trigger(
                        "ai_model_shadow_runs_append_only_update", auto_commit=False
                    )
                    return
                for _var_var_101 in _var_var_100:
                    _var_var_112 = self._sealed_shadow_payloads(_var_var_101)
                    if _var_var_112 is None:
                        _var_var_122.add(str(_var_var_101[0]))
                        continue
                    _var_var_125, _var_var_126 = _var_var_112
                    await self.execute(
                        "UPDATE ai_model_shadow_runs\n                              SET input_snapshot_json = '',\n                                  policy_snapshot_json = '',\n                                  input_snapshot_blob = ?,\n                                  policy_snapshot_blob = ?,\n                                  snapshot_codec = ?\n                            WHERE shadow_run_id = ?",
                        (
                            _var_var_125,
                            _var_var_126,
                            SHADOW_JSON_CODEC_AESGCM,
                            _var_var_101[0],
                        ),
                        auto_commit=False,
                    )
                await self._restore_schema_trigger(
                    "ai_model_shadow_runs_append_only_update", auto_commit=False
                )
            if len(_var_var_100) < ARTIFACT_SEAL_BATCH:
                return

    async def _rebuild_shadow_run_table(self) -> None:
        """Recreate ai_model_shadow_runs so its codec CHECK accepts aesgcm-v1."""
        _var_var_131 = self._read_schema_sql()
        _var_var_132 = self._schema_create_table(_var_var_131, "ai_model_shadow_runs")
        _var_var_132 = _var_var_132.replace(
            "CREATE TABLE IF NOT EXISTS ai_model_shadow_runs",
            "CREATE TABLE ai_model_shadow_runs_new",
            1,
        )
        await self._db.execute("PRAGMA foreign_keys = OFF")
        await self.execute("DROP TABLE IF EXISTS ai_model_shadow_runs_new")
        await self.execute(_var_var_132)
        _var_var_133 = await self._table_column_names("ai_model_shadow_runs_new")
        _var_var_134 = await self._table_column_names("ai_model_shadow_runs")
        _var_var_135 = ""
        while True:
            _var_var_100 = await self.fetchall(
                f"SELECT * FROM ai_model_shadow_runs\n                    WHERE shadow_run_id > ?\n                    ORDER BY shadow_run_id\n                    LIMIT {ARTIFACT_SEAL_BATCH}",
                (_var_var_135,),
            )
            if not _var_var_100:
                break
            async with self.transaction():
                for _var_var_101 in _var_var_100:
                    A_recovered_capture_0000 = {
                        _var_var_134[_var_var_49]: _var_var_101[_var_var_49]
                        for _var_var_49 in range(
                            min(len(_var_var_134), len(_var_var_101))
                        )
                    }
                    _var_var_112 = self._sealed_shadow_payloads(
                        (
                            A_recovered_capture_0000.get("shadow_run_id"),
                            A_recovered_capture_0000.get("input_snapshot_json"),
                            A_recovered_capture_0000.get("input_snapshot_blob"),
                            A_recovered_capture_0000.get("input_snapshot_hash"),
                            A_recovered_capture_0000.get("policy_snapshot_json"),
                            A_recovered_capture_0000.get("policy_snapshot_blob"),
                            A_recovered_capture_0000.get("policy_snapshot_hash"),
                            A_recovered_capture_0000.get("snapshot_codec"),
                        )
                    )
                    if _var_var_112 is not None:
                        A_recovered_capture_0000["input_snapshot_json"] = ""
                        A_recovered_capture_0000["policy_snapshot_json"] = ""
                        A_recovered_capture_0000["input_snapshot_blob"] = _var_var_112[
                            0
                        ]
                        A_recovered_capture_0000["policy_snapshot_blob"] = _var_var_112[
                            1
                        ]
                        A_recovered_capture_0000["snapshot_codec"] = (
                            SHADOW_JSON_CODEC_AESGCM
                        )
                    _var_var_136 = [
                        _var_var_0
                        for _var_var_0 in _var_var_133
                        if _var_var_0 in A_recovered_capture_0000
                    ]
                    _var_var_137 = ", ".join(("?" for _var_var_127 in _var_var_136))
                    await self.execute(
                        f"INSERT INTO ai_model_shadow_runs_new\n                            ({', '.join(_var_var_136)})\n                            VALUES ({_var_var_137})",
                        tuple(
                            (
                                A_recovered_capture_0000[_var_var_0]
                                for _var_var_0 in _var_var_136
                            )
                        ),
                        auto_commit=False,
                    )
                    continue
            _var_var_135 = str(_var_var_100[-1][_var_var_134.index("shadow_run_id")])
        await self._drop_triggers_referencing("ai_model_shadow_runs")
        async with self.transaction():
            await self.execute("DROP TABLE ai_model_shadow_runs", auto_commit=False)
            await self.execute(
                "ALTER TABLE ai_model_shadow_runs_new RENAME TO ai_model_shadow_runs",
                auto_commit=False,
            )
        await self._restore_triggers_referencing("ai_model_shadow_runs")
        await self._db.execute("PRAGMA foreign_keys = ON")

    async def _restore_schema_indexes(self, table_name: str) -> None:
        _var_var_131 = self._read_schema_sql()
        _var_var_17 = 0
        async with self.transaction():
            while True:
                _var_var_18 = _var_var_131.find("CREATE INDEX", _var_var_17)
                if _var_var_18 < 0:
                    return
                _var_var_19 = _var_var_131.find(";", _var_var_18)
                _var_var_21 = _var_var_131[_var_var_18 : _var_var_19 + 1].strip()
                if re.search(f"\\bON {re.escape(table_name)}\\b", _var_var_21):
                    await self.execute(_var_var_21, auto_commit=False)
                _var_var_17 = _var_var_19 + 1
        return None if True else None

    async def _migrate_raw_physics_kwh_v20260826(self) -> None:
        """Persist uncalibrated physics kWh beside calibrated physics_kwh."""
        for _var_var_138 in ("prediction_panel_groups", "ops_prediction_panel_groups"):
            async with self._db.execute(
                f"PRAGMA table_info({_var_var_138})"
            ) as _var_var_17:
                _var_var_94 = {
                    _var_var_101[1] for _var_var_101 in await _var_var_17.fetchall()
                }
            if "raw_physics_kwh" not in _var_var_94:
                await self._db.execute(
                    f"ALTER TABLE {_var_var_138} ADD COLUMN raw_physics_kwh REAL"
                )
        await self._db.commit()

    async def _migrate_weather_visibility_source_v20260827(self) -> None:
        """Persist visibility provenance on cache and corrected forecast rows."""
        for _var_var_138 in ("weather_forecast", "weather_cache_open_meteo"):
            async with self._db.execute(
                f"PRAGMA table_info({_var_var_138})"
            ) as _var_var_17:
                _var_var_94 = {
                    _var_var_101[1] for _var_var_101 in await _var_var_17.fetchall()
                }
            if "visibility_source" not in _var_var_94:
                await self._db.execute(
                    f"ALTER TABLE {_var_var_138} ADD COLUMN visibility_source TEXT"
                )
        await self._db.commit()

    async def reconcile_pending_model_candidates(
        self,
        *,
        compatible_candidate_ids: Sequence[str],
        incompatible_candidate_ids: Sequence[str],
    ) -> Optional[str]:
        """Select one verified candidate for shadow evidence collection."""
        _var_var_157 = tuple(compatible_candidate_ids)
        _var_var_158 = tuple(incompatible_candidate_ids)
        if (
            len(set(_var_var_157)) != len(_var_var_157)
            or len(set(_var_var_158)) != len(_var_var_158)
            or set(_var_var_157) & set(_var_var_158)
            or any(
                (
                    not isinstance(_var_var_164, str) or not _var_var_164
                    for _var_var_164 in [*_var_var_157, *_var_var_158]
                )
            )
        ):
            raise ValueError("runtime candidate ids must be unique and valid")
        async with self.transaction():
            _var_var_159 = await self.fetchall(
                "SELECT candidate.candidate_id\n                     FROM ai_model_candidates candidate\n                    WHERE candidate.status = 'candidate'\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_terminal_events terminal\n                           WHERE terminal.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_dispositions disposition\n                           WHERE disposition.candidate_id = candidate.candidate_id\n                             AND NOT EXISTS (\n                                 SELECT 1 FROM ai_model_candidate_reconsiderations reconsideration\n                                  WHERE reconsideration.candidate_id = disposition.candidate_id\n                                    AND disposition.disposition = 'migration_superseded'\n                                    AND disposition.replacement_candidate_id = reconsideration.keeper_candidate_id\n                                    AND reconsideration.candidate_id != reconsideration.keeper_candidate_id\n                             )\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_evaluations evaluation\n                           WHERE evaluation.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                           WHERE evaluation.candidate_id = candidate.candidate_id\n                      )\n                      AND NOT EXISTS (\n                          SELECT 1 FROM ai_model_candidate_reconsideration_closures closure\n                           WHERE closure.candidate_id = candidate.candidate_id\n                      )\n                    ORDER BY candidate.created_at, candidate.candidate_id"
            )
            _var_var_160 = {str(_var_var_101[0]) for _var_var_101 in _var_var_159}
            _var_var_161 = set(_var_var_158) & _var_var_160
            _var_var_162 = set(_var_var_158) - _var_var_161
            if _var_var_162:
                _var_var_163 = await self.fetchall(
                    "SELECT candidate_id, disposition\n                         FROM ai_model_candidate_dispositions\n                        WHERE candidate_id IN ({})".format(
                        ", ".join(("?" for _var_var_127 in _var_var_162))
                    ),
                    tuple(_var_var_162),
                )
                if set(_var_var_163) != {
                    (_var_var_164, "incompatible_runtime")
                    for _var_var_164 in _var_var_162
                }:
                    raise ValueError("incompatible candidate is not pending")
            if _var_var_160 != set(_var_var_157) | _var_var_161:
                raise ValueError("every pending candidate must be runtime-verified")
            _var_var_165 = None
            if _var_var_157:
                _var_var_137 = ", ".join(("?" for _var_var_127 in _var_var_157))
                _var_var_99 = await self.fetchone(
                    f"SELECT candidate.candidate_id\n                           FROM ai_model_candidates candidate\n                          WHERE candidate.candidate_id IN ({_var_var_137})\n                          ORDER BY candidate.accuracy DESC NULLS LAST,\n                                   candidate.training_samples DESC,\n                                   candidate.rmse ASC NULLS LAST,\n                                   candidate.created_at ASC, candidate.candidate_id ASC\n                          LIMIT 1",
                    _var_var_157,
                )
                _var_var_165 = str(_var_var_99[0]) if _var_var_99 else None
            _var_var_166 = SafeDateTimeUtil.now().isoformat()
            if _var_var_161:
                await self.executemany(
                    "INSERT INTO ai_model_candidate_dispositions\n                       (candidate_id, disposition, replacement_candidate_id,\n                        retrain_request_id, created_at)\n                       VALUES (?, ?, ?, NULL, ?)",
                    [
                        (
                            _var_var_164,
                            "incompatible_runtime",
                            _var_var_165,
                            _var_var_166,
                        )
                        for _var_var_164 in sorted(_var_var_161)
                    ],
                )
        if True:
            if True:
                if True:
                    return _var_var_165

    async def _migrate_model_candidate_shadow_compression_v20260813(self) -> None:
        """Compact immutable shadow JSON payloads without discarding evidence."""
        _var_var_151 = {
            _var_var_101[1]
            for _var_var_101 in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_runs)")
            ).fetchall()
        }
        _var_var_140 = {
            _var_var_101[1]
            for _var_var_101 in await (
                await self._db.execute("PRAGMA table_info(ai_model_shadow_predictions)")
            ).fetchall()
        }
        _var_var_167 = {
            "input_snapshot_blob": "BLOB",
            "policy_snapshot_blob": "BLOB",
            "payload_state": "TEXT NOT NULL DEFAULT 'hot' CHECK(payload_state IN ('hot', 'compacted'))",
            "snapshot_codec": "TEXT NOT NULL DEFAULT 'plain-json-v1' CHECK(snapshot_codec IN ('plain-json-v1', 'zlib-json-v1', 'aesgcm-v1'))",
        }
        _var_var_168 = {
            "champion_result_blob": "BLOB",
            "candidate_result_blob": "BLOB",
            "payload_state": "TEXT NOT NULL DEFAULT 'hot' CHECK(payload_state IN ('hot', 'compacted'))",
            "result_codec": "TEXT NOT NULL DEFAULT 'plain-json-v1' CHECK(result_codec IN ('plain-json-v1', 'zlib-json-v1'))",
        }
        _var_var_169 = 0
        _var_var_170 = 0
        async with self.transaction():
            for _var_var_0, _var_var_121 in _var_var_167.items():
                if _var_var_0 not in _var_var_151:
                    await self.execute(
                        f"ALTER TABLE ai_model_shadow_runs ADD COLUMN {_var_var_0} {_var_var_121}",
                        auto_commit=False,
                    )
            for _var_var_0, _var_var_121 in _var_var_168.items():
                if _var_var_0 not in _var_var_140:
                    await self.execute(
                        f"ALTER TABLE ai_model_shadow_predictions ADD COLUMN {_var_var_0} {_var_var_121}",
                        auto_commit=False,
                    )
        while True:
            _var_var_171 = 0
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_runs_append_only_update",
                    auto_commit=False,
                )
                _var_var_172 = await self.fetchall(
                    "SELECT shadow_run_id, input_snapshot_json, input_snapshot_hash,\n                              policy_snapshot_json, policy_snapshot_hash\n                         FROM ai_model_shadow_runs\n                        WHERE payload_state = 'hot' AND (\n                              snapshot_codec = 'plain-json-v1'\n                           OR input_snapshot_blob IS NULL\n                           OR policy_snapshot_blob IS NULL)\n                        ORDER BY shadow_run_id LIMIT 8"
                )
                _var_var_173 = []
                for _var_var_101 in _var_var_172:
                    decode_shadow_json(
                        _var_var_101[1], None, SHADOW_JSON_CODEC_PLAIN, _var_var_101[2]
                    )
                    decode_shadow_json(
                        _var_var_101[3], None, SHADOW_JSON_CODEC_PLAIN, _var_var_101[4]
                    )
                    _var_var_173.append(
                        (
                            compress_shadow_json(_var_var_101[1]),
                            compress_shadow_json(_var_var_101[3]),
                            SHADOW_JSON_CODEC_ZLIB,
                            _var_var_101[0],
                        )
                    )
                if _var_var_173:
                    await self.executemany(
                        "UPDATE ai_model_shadow_runs\n                              SET input_snapshot_json = '', policy_snapshot_json = '',\n                                  input_snapshot_blob = ?, policy_snapshot_blob = ?,\n                                  snapshot_codec = ?\n                            WHERE shadow_run_id = ?",
                        _var_var_173,
                    )
                    _var_var_171 = len(_var_var_173)
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_runs_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_runs\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_runs is append-only'); END",
                    auto_commit=False,
                )
            _var_var_169 += _var_var_171
            if not _var_var_171:
                break
        while True:
            _var_var_174 = 0
            async with self.transaction():
                await self.execute(
                    "DROP TRIGGER IF EXISTS ai_model_shadow_predictions_append_only_update",
                    auto_commit=False,
                )
                _var_var_175 = await self.fetchall(
                    "SELECT shadow_prediction_id, champion_result_json,\n                              champion_result_hash, candidate_result_json,\n                              candidate_result_hash\n                         FROM ai_model_shadow_predictions\n                        WHERE payload_state = 'hot' AND (\n                              result_codec = 'plain-json-v1'\n                           OR champion_result_blob IS NULL\n                           OR candidate_result_blob IS NULL)\n                        ORDER BY shadow_prediction_id LIMIT 128"
                )
                _var_var_176 = []
                for _var_var_101 in _var_var_175:
                    decode_shadow_json(
                        _var_var_101[1], None, SHADOW_JSON_CODEC_PLAIN, _var_var_101[2]
                    )
                    decode_shadow_json(
                        _var_var_101[3], None, SHADOW_JSON_CODEC_PLAIN, _var_var_101[4]
                    )
                    _var_var_176.append(
                        (
                            compress_shadow_json(_var_var_101[1]),
                            compress_shadow_json(_var_var_101[3]),
                            SHADOW_JSON_CODEC_ZLIB,
                            _var_var_101[0],
                        )
                    )
                if _var_var_176:
                    await self.executemany(
                        "UPDATE ai_model_shadow_predictions\n                              SET champion_result_json = '', candidate_result_json = '',\n                                  champion_result_blob = ?, candidate_result_blob = ?,\n                                  result_codec = ?\n                            WHERE shadow_prediction_id = ?",
                        _var_var_176,
                    )
                    _var_var_174 = len(_var_var_176)
                await self.execute(
                    "CREATE TRIGGER ai_model_shadow_predictions_append_only_update\n                       BEFORE UPDATE ON ai_model_shadow_predictions\n                       BEGIN SELECT RAISE(ABORT,\n                           'ai_model_shadow_predictions is append-only'); END",
                    auto_commit=False,
                )
            _var_var_170 += _var_var_174
            if not _var_var_174:
                break
        if _var_var_169 or _var_var_170:
            _LOGGER.info(
                "Model candidate shadow storage compacted: runs=%d predictions=%d",
                _var_var_169,
                _var_var_170,
            )
        if True:
            return None

    async def _migrate_ai_model_cycle_evaluation_v42(self) -> None:
        """Create V42 cycle provenance and evaluation storage without legacy rewrites."""
        _var_var_16 = (
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
        _var_var_178 = (
            "ensemble_shadow_batch_provenance",
            "ai_model_shadow_run_provenance",
            "ai_model_evaluations_v2",
            "ai_model_evaluation_samples_v2",
            "ai_model_evaluation_invalidations",
            "ai_model_promotion_events",
        )
        _var_var_177 = {
            "ensemble_shadow_batch_provenance": "morning_batch_id = NEW.morning_batch_id",
            "ai_model_shadow_run_provenance": "shadow_run_id = NEW.shadow_run_id OR morning_batch_id = NEW.morning_batch_id",
            "ai_model_evaluations_v2": "evaluation_id = NEW.evaluation_id",
            "ai_model_evaluation_invalidations": "evaluation_id = NEW.evaluation_id",
            "ai_model_promotion_events": "promotion_event_id = NEW.promotion_event_id",
            "ai_model_evaluation_samples_v2": "(evaluation_id = NEW.evaluation_id AND sample_index = NEW.sample_index) OR (evaluation_id = NEW.evaluation_id AND target_date = NEW.target_date AND target_hour = NEW.target_hour AND forecast_cycle = NEW.forecast_cycle)",
        }
        async with self.transaction():
            for _var_var_21 in _var_var_16:
                await self.execute(_var_var_21, auto_commit=False)
            for _var_var_96 in _var_var_178:
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {_var_var_96}_append_only_update\n                         BEFORE UPDATE ON {_var_var_96}\n                         BEGIN SELECT RAISE(ABORT, '{_var_var_96} is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {_var_var_96}_append_only_delete\n                         BEFORE DELETE ON {_var_var_96}\n                         BEGIN SELECT RAISE(ABORT, '{_var_var_96} is append-only'); END",
                    auto_commit=False,
                )
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {_var_var_96}_append_only_insert_guard\n                         BEFORE INSERT ON {_var_var_96}\n                         WHEN EXISTS (SELECT 1 FROM {_var_var_96} WHERE {_var_var_177[_var_var_96]})\n                         BEGIN SELECT RAISE(ABORT, '{_var_var_96} is append-only'); END",
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
            for _var_var_179 in ("INSERT", "UPDATE"):
                await self.execute(
                    f"CREATE TRIGGER IF NOT EXISTS\n                         ai_active_model_pointer_promotion_guard_{_var_var_179.lower()}\n                         BEFORE {_var_var_179} ON ai_active_model_pointer\n                         WHEN (NEW.active_candidate_id IS NOT NULL AND NOT EXISTS (\n                                   SELECT 1 FROM ai_model_promotion_events event\n                                    WHERE event.candidate_id = NEW.active_candidate_id\n                               )) OR (NEW.last_known_good_candidate_id IS NOT NULL AND NOT EXISTS (\n                                   SELECT 1 FROM ai_model_promotion_events event\n                                    WHERE event.candidate_id = NEW.last_known_good_candidate_id\n                               ))\n                         BEGIN SELECT RAISE(ABORT,\n                             'active model pointer requires promotion provenance'); END",
                    auto_commit=False,
                )
        if True:
            return

    async def _migrate_visibility_learning_table_v161(self) -> None:
        """Create/update visibility_learning table. @zara V16.1

        This table stores fog detection learning data.
        V16.1 adds: visibility_threshold_m, fog_visibility_threshold_m,
                    samples_below_threshold, samples_above_threshold
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='visibility_learning'"
        ) as _var_var_17:
            _var_var_87 = await _var_var_17.fetchone()
        if not _var_var_87:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS visibility_learning (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    version TEXT DEFAULT '1.0',\n                    has_solar_radiation_sensor BOOLEAN DEFAULT FALSE,\n                    last_learning_date DATE,\n                    total_fog_hours_learned INTEGER DEFAULT 0,\n                    total_fog_light_hours_learned INTEGER DEFAULT 0,\n                    bright_sky_fog_hits INTEGER DEFAULT 0,\n                    pirate_weather_fog_hits INTEGER DEFAULT 0,\n                    learning_sessions INTEGER DEFAULT 0,\n                    fog_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_pirate_weather_weight REAL DEFAULT 0.5,\n                    fog_light_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_light_pirate_weather_weight REAL DEFAULT 0.5,\n                    visibility_threshold_m REAL DEFAULT 5000,\n                    fog_visibility_threshold_m REAL DEFAULT 1000,\n                    samples_below_threshold INTEGER DEFAULT 0,\n                    samples_above_threshold INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created visibility_learning table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(visibility_learning)"
            ) as _var_var_17:
                _var_var_94 = await _var_var_17.fetchall()
                _var_var_189 = {_var_var_199[1] for _var_var_199 in _var_var_94}
            _var_var_190 = []
            if "visibility_threshold_m" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE visibility_learning ADD COLUMN visibility_threshold_m REAL DEFAULT 5000"
                )
            if "fog_visibility_threshold_m" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE visibility_learning ADD COLUMN fog_visibility_threshold_m REAL DEFAULT 1000"
                )
            if "samples_below_threshold" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_below_threshold INTEGER DEFAULT 0"
                )
            if "samples_above_threshold" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_above_threshold INTEGER DEFAULT 0"
                )
            if _var_var_190:
                for _var_var_279 in _var_var_190:
                    await self._db.execute(_var_var_279)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to visibility_learning",
                    len(_var_var_190),
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
        ) as _var_var_17:
            _var_var_87 = await _var_var_17.fetchone()
        if not _var_var_87:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_started_at TIMESTAMP,\n                    melt_hours REAL DEFAULT 0,\n                    detection_source TEXT,\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created snow_tracking table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(snow_tracking)"
            ) as _var_var_17:
                _var_var_94 = await _var_var_17.fetchall()
                _var_var_189 = {_var_var_199[1] for _var_var_199 in _var_var_94}
            _var_var_190 = []
            if "melt_hours" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE snow_tracking ADD COLUMN melt_hours REAL DEFAULT 0"
                )
            if "detection_source" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE snow_tracking ADD COLUMN detection_source TEXT"
                )
            if "cleared_at" not in _var_var_189:
                _var_var_190.append(
                    "ALTER TABLE snow_tracking ADD COLUMN cleared_at TIMESTAMP"
                )
            if _var_var_190:
                for _var_var_279 in _var_var_190:
                    await self._db.execute(_var_var_279)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to snow_tracking",
                    len(_var_var_190),
                )
        if True:
            return None

    async def _backfill_astronomy_enrichment_v1640(self) -> None:
        """V16.4.0: Backfill prediction_astronomy enrichment from stored sunrise/sunset/noon. @zara"""
        try:
            async with self._db.execute(
                "SELECT pa.prediction_id, pa.sunrise, pa.sunset, pa.solar_noon, hp.target_hour\n                   FROM prediction_astronomy pa\n                   JOIN hourly_predictions hp ON hp.prediction_id = pa.prediction_id\n                   WHERE (pa.day_progress_ratio IS NULL OR pa.hours_after_sunrise IS NULL\n                          OR pa.hours_before_sunset IS NULL OR pa.hours_since_solar_noon IS NULL)\n                     AND pa.sunrise IS NOT NULL AND pa.sunset IS NOT NULL\n                   LIMIT 5000"
            ) as _var_var_17:
                _var_var_100 = await _var_var_17.fetchall()
            if not _var_var_100:
                return
            _var_var_233 = 0
            for _var_var_101 in _var_var_100:
                _var_var_268 = _var_var_101[0]
                _var_var_335 = _var_var_101[1]
                _var_var_336 = _var_var_101[2]
                _var_var_337 = _var_var_101[3]
                _var_var_338 = _var_var_101[4] if _var_var_101[4] is not None else 12
                try:
                    _var_var_339 = self._parse_astronomy_ts(_var_var_335)
                    _var_var_340 = self._parse_astronomy_ts(_var_var_336)
                    if _var_var_339 is None or _var_var_340 is None:
                        continue
                    _var_var_341 = (
                        _var_var_339.replace(tzinfo=None)
                        if _var_var_339.tzinfo
                        else _var_var_339
                    )
                    _var_var_342 = (
                        _var_var_340.replace(tzinfo=None)
                        if _var_var_340.tzinfo
                        else _var_var_340
                    )
                    _var_var_343 = _var_var_341.replace(
                        hour=_var_var_338, minute=30, second=0, microsecond=0
                    )
                    _var_var_344 = (_var_var_342 - _var_var_341).total_seconds()
                    _var_var_345 = (_var_var_343 - _var_var_341).total_seconds()
                    _var_var_346 = (
                        max(0.0, min(1.0, _var_var_345 / _var_var_344))
                        if _var_var_344 > 0
                        else 0.5
                    )
                    _var_var_347 = max(0.0, _var_var_345 / 3600.0)
                    _var_var_348 = max(
                        0.0, (_var_var_342 - _var_var_343).total_seconds() / 3600.0
                    )
                    _var_var_349 = None
                    if _var_var_337:
                        _var_var_350 = self._parse_astronomy_ts(_var_var_337)
                        if _var_var_350:
                            _var_var_351 = (
                                _var_var_350.replace(tzinfo=None)
                                if _var_var_350.tzinfo
                                else _var_var_350
                            )
                            _var_var_349 = (
                                _var_var_343 - _var_var_351
                            ).total_seconds() / 3600.0
                    await self._db.execute(
                        "UPDATE prediction_astronomy\n                           SET day_progress_ratio = ?,\n                               hours_after_sunrise = ?,\n                               hours_before_sunset = ?,\n                               hours_since_solar_noon = ?\n                           WHERE prediction_id = ?\n                             AND day_progress_ratio IS NULL",
                        (
                            round(_var_var_346, 6),
                            round(_var_var_347, 4),
                            round(_var_var_348, 4),
                            (
                                round(_var_var_349, 4)
                                if _var_var_349 is not None
                                else None
                            ),
                            _var_var_268,
                        ),
                    )
                    _var_var_233 += 1
                except Exception:
                    continue
            if _var_var_233 > 0:
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Backfilled %d prediction_astronomy enrichment rows",
                    _var_var_233,
                )
        except Exception:
            pass
        if True:
            return None

    @staticmethod
    def _parse_astronomy_ts(ts_str):
        """Parse astronomy timestamp with timezone offset support. @zara V16.4.0"""
        if not ts_str:
            return
        try:
            _var_var_357 = str(ts_str).strip()
            return datetime.fromisoformat(_var_var_357.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            for _var_var_358 in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%H:%M:%S",
                "%H:%M",
            ):
                try:
                    return datetime.strptime(_var_var_357, _var_var_358)
                except (ValueError, TypeError):
                    continue
        return None if True else None

    async def reconsider_migration_superseded_model_candidate(
        self,
        candidate_id: str,
        keeper_candidate_id: str,
        *,
        runtime_compatible_candidate_ids: Sequence[str],
    ) -> bool:
        """Append one guarded reconsideration without changing disposition history."""
        _var_var_441 = set(runtime_compatible_candidate_ids)
        if (
            not candidate_id
            or not keeper_candidate_id
            or {candidate_id, keeper_candidate_id} - _var_var_441
        ):
            raise ValueError("reconsideration requires verified runtime compatibility")
        async with self.transaction():
            _var_var_442 = await self.fetchone(
                "SELECT disposition, replacement_candidate_id\n                     FROM ai_model_candidate_dispositions WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not _var_var_442 or _var_var_442[0] != "migration_superseded":
                raise ValueError(
                    "only migration-superseded candidates are reconsiderable"
                )
            if _var_var_442[1] != keeper_candidate_id:
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
            for _var_var_443 in (candidate_id, keeper_candidate_id):
                A_recovered_capture_0001 = await self.fetchone(
                    "SELECT artifact_hash FROM ai_model_candidates\n                         WHERE candidate_id = ? AND status = 'candidate'",
                    (_var_var_443,),
                )
                _var_var_394 = await self.fetchall(
                    "SELECT model_type, weights_json, weights_hash\n                         FROM ai_model_candidate_artifacts WHERE candidate_id = ?",
                    (_var_var_443,),
                )
                _var_var_46 = {}
                for _var_var_393, _var_var_405, _var_var_395 in _var_var_394:
                    _var_var_404 = loads_protected_json(
                        _var_var_405,
                        "ai_model_candidate_artifacts",
                        protected_identity(_var_var_443, _var_var_393),
                        "weights",
                    )
                    if canonical_stable_hash(_var_var_404) != _var_var_395:
                        raise ValueError("candidate artifact hash mismatch")
                    _var_var_46[_var_var_393] = _var_var_404
                if (
                    not A_recovered_capture_0001
                    or not _var_var_46
                    or canonical_stable_hash(model_weight_artifacts(_var_var_46))
                    != A_recovered_capture_0001[0]
                ):
                    raise ValueError(
                        "candidate artifact set is incomplete or mismatched"
                    )
            _var_var_431 = await self.fetchall(
                "WITH shadow_samples AS (\n                       SELECT run.candidate_id, prediction.target_date, prediction.target_hour,\n                              provenance.forecast_cycle\n                         FROM ai_model_shadow_runs run\n                         JOIN ai_model_shadow_run_provenance provenance\n                           ON provenance.shadow_run_id = run.shadow_run_id\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_run_id = run.shadow_run_id\n                         JOIN ai_model_shadow_actual_versions actual\n                           ON actual.shadow_prediction_id = prediction.shadow_prediction_id\n                        WHERE provenance.promotion_eligible = 1\n                          AND provenance.forecast_cycle IN ('post_midnight', 'pre_sunrise')\n                          AND actual.clean_eligible = 1 AND actual.actual_kwh IS NOT NULL\n                    ), paired AS (\n                       SELECT candidate_id, target_date, target_hour FROM shadow_samples\n                        GROUP BY candidate_id, target_date, target_hour\n                       HAVING COUNT(DISTINCT forecast_cycle) = 2\n                    ), evidence AS (\n                       SELECT candidate_id, COUNT(*) AS days FROM (\n                           SELECT candidate_id, target_date FROM paired\n                            GROUP BY candidate_id, target_date HAVING COUNT(*) >= 4\n                       ) GROUP BY candidate_id\n                    )\n                    SELECT candidate.candidate_id, COALESCE(evidence.days, 0),\n                           candidate.accuracy, candidate.training_samples, candidate.rmse\n                      FROM ai_model_candidates candidate LEFT JOIN evidence\n                        ON evidence.candidate_id = candidate.candidate_id\n                     WHERE candidate.candidate_id IN (?, ?)",
                (candidate_id, keeper_candidate_id),
            )
            _var_var_444 = {
                str(_var_var_101[0]): _var_var_101[1:] for _var_var_101 in _var_var_431
            }
            _var_var_445 = _var_var_444.get(candidate_id)
            _var_var_446 = _var_var_444.get(keeper_candidate_id)
            if not _var_var_445 or not _var_var_446:
                raise ValueError("reconsideration evidence is unavailable")
            _var_var_447, _var_var_448, _var_var_449, _var_var_450 = _var_var_445
            _var_var_451, _var_var_127, _var_var_127, _var_var_127 = _var_var_446
            if _var_var_451 >= MODEL_PROMOTION_MIN_INDEPENDENT_DAYS:
                raise ValueError(
                    "keeper already has promotion-eligible shadow evidence"
                )
            _var_var_452 = (
                int(_var_var_447),
                float(_var_var_448) if _var_var_448 is not None else float("-inf"),
                int(_var_var_449),
                -(float(_var_var_450) if _var_var_450 is not None else float("inf")),
            )
            _var_var_453 = (
                int(_var_var_446[0]),
                (
                    float(_var_var_446[1])
                    if _var_var_446[1] is not None
                    else float("-inf")
                ),
                int(_var_var_446[2]),
                -(
                    float(_var_var_446[3])
                    if _var_var_446[3] is not None
                    else float("inf")
                ),
            )
            if _var_var_452 <= _var_var_453:
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

    async def get_or_create_confirmed_model_retrain_request(
        self, through_date: str
    ) -> Optional[Dict[str, Any]]:
        """Coalesce repeated rolling-MAE drift into one restart-safe request."""
        _var_var_299 = date.fromisoformat(through_date).isoformat()
        _var_var_457 = await self.get_open_model_retrain_request()
        _var_var_458 = (("critical", 3, 2), ("warning", 5, 3))
        _var_var_99 = None
        for _var_var_459, _var_var_460, _var_var_461 in _var_var_458:
            _var_var_100 = await self.fetchall(
                "SELECT event.id, event.event_date\n                     FROM drift_events event\n                    WHERE event.scope = 'global'\n                      AND event.drift_type = 'mae_above_baseline'\n                      AND event.severity = ?\n                      AND event.response_action = 'light_retrain'\n                      AND COALESCE(event.response_executed, FALSE) = FALSE\n                      AND event.event_date BETWEEN date(?, ?) AND date(?)\n                      AND (\n                          SELECT COUNT(*) FROM hourly_predictions prediction\n                           WHERE prediction.target_date = event.event_date\n                             AND prediction.actual_kwh IS NOT NULL\n                             AND prediction.actual_kwh > 0.01\n                             AND prediction.prediction_kwh IS NOT NULL\n                             AND COALESCE(prediction.exclude_from_learning, FALSE) = FALSE\n                             AND COALESCE(prediction.exclude_from_clean_evaluation, FALSE) = FALSE\n                             AND COALESCE(prediction.mppt_throttled, FALSE) = FALSE\n                             AND COALESCE(prediction.inverter_clipped, FALSE) = FALSE\n                      ) >= 4\n                    ORDER BY event.event_date, event.id",
                (_var_var_459, _var_var_299, f"-{_var_var_460 - 1} days", _var_var_299),
            )
            if (
                len({str(_var_var_101[1]) for _var_var_101 in _var_var_100})
                >= _var_var_461
            ):
                _var_var_99 = (_var_var_459, _var_var_100)
                break
        if _var_var_99 is None:
            return _var_var_457
        _var_var_462, _var_var_100 = _var_var_99
        _var_var_463 = sorted({str(_var_var_101[1]) for _var_var_101 in _var_var_100})
        _var_var_156 = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            _var_var_33 = await self.fetchone(
                "SELECT request_id, status, severity, first_event_date,\n                          last_event_date, confirmation_days, bound_candidate_id,\n                          superseded_candidate_id\n                     FROM ai_model_retrain_requests\n                    WHERE scope = 'global'\n                      AND drift_type = 'mae_above_baseline'\n                      AND status IN ('pending', 'bound')\n                    ORDER BY created_at LIMIT 1"
            )
            if _var_var_33:
                _var_var_399 = str(_var_var_33[0])
                _var_var_464 = str(_var_var_33[1])
                _var_var_459 = str(_var_var_33[2])
                _var_var_465 = str(_var_var_33[3])
                _var_var_466 = str(_var_var_33[4])
                _var_var_467 = int(_var_var_33[5])
                _var_var_468 = _var_var_33[6]
                _var_var_469 = _var_var_33[7]
                _var_var_470 = []
                if _var_var_464 == "pending":
                    _var_var_471 = {
                        str(_var_var_101[0])
                        for _var_var_101 in await self.fetchall(
                            "SELECT event.event_date\n                                 FROM ai_model_retrain_request_events link\n                                 JOIN drift_events event\n                                   ON event.id = link.drift_event_id\n                                WHERE link.request_id = ?",
                            (_var_var_399,),
                        )
                    }
                    _var_var_472 = sorted(_var_var_471 | set(_var_var_463))
                    _var_var_465 = _var_var_472[0]
                    _var_var_466 = _var_var_472[-1]
                    _var_var_467 = len(_var_var_472)
                    if _var_var_462 == "critical":
                        _var_var_459 = "critical"
                    await self.execute(
                        "UPDATE ai_model_retrain_requests\n                              SET updated_at = ?, severity = ?, first_event_date = ?,\n                                  last_event_date = ?, confirmation_days = ?\n                            WHERE request_id = ?",
                        (
                            _var_var_156,
                            _var_var_459,
                            _var_var_465,
                            _var_var_466,
                            _var_var_467,
                            _var_var_399,
                        ),
                        auto_commit=False,
                    )
                    _var_var_470 = _var_var_100
            else:
                _var_var_399 = str(uuid.uuid4())
                _var_var_459 = _var_var_462
                _var_var_465 = _var_var_463[0]
                _var_var_466 = _var_var_463[-1]
                _var_var_467 = len(_var_var_463)
                _var_var_464 = "pending"
                _var_var_468 = None
                _var_var_469 = None
                _var_var_470 = _var_var_100
                await self.execute(
                    "INSERT INTO ai_model_retrain_requests\n                       (request_id, created_at, updated_at, status, scope,\n                        drift_type, severity, first_event_date, last_event_date,\n                        confirmation_days)\n                       VALUES (?, ?, ?, 'pending', 'global',\n                               'mae_above_baseline', ?, ?, ?, ?)",
                    (
                        _var_var_399,
                        _var_var_156,
                        _var_var_156,
                        _var_var_459,
                        _var_var_465,
                        _var_var_466,
                        _var_var_467,
                    ),
                    auto_commit=False,
                )
            await self.executemany(
                "INSERT OR IGNORE INTO ai_model_retrain_request_events\n                   (request_id, drift_event_id, linked_at) VALUES (?, ?, ?)",
                [
                    (_var_var_399, int(_var_var_101[0]), _var_var_156)
                    for _var_var_101 in _var_var_470
                ],
            )
        return (
            None
            if False
            else {
                "request_id": _var_var_399,
                "status": _var_var_464,
                "severity": _var_var_459,
                "first_event_date": _var_var_465,
                "last_event_date": _var_var_466,
                "confirmation_days": _var_var_467,
                "bound_candidate_id": str(_var_var_468) if _var_var_468 else None,
                "superseded_candidate_id": str(_var_var_469) if _var_var_469 else None,
            }
        )

    async def model_candidate_covers_retrain_request(
        self, request_id: str, candidate_id: str, minimum_samples_per_day: int = 4
    ) -> bool:
        """Verify that every confirmed drift day is present in candidate provenance."""
        _var_var_473 = {
            str(_var_var_101[0])
            for _var_var_101 in await self.fetchall(
                "SELECT event.event_date\n                     FROM ai_model_retrain_request_events link\n                     JOIN drift_events event ON event.id = link.drift_event_id\n                    WHERE link.request_id = ?",
                (request_id,),
            )
        }
        if not _var_var_473:
            return False
        for _var_var_56 in _var_var_473:
            _var_var_187 = await self.fetchone(
                "SELECT COUNT(DISTINCT provenance.sample_index)\n                     FROM ai_model_candidates candidate\n                     JOIN ai_training_sample_provenance provenance\n                       ON provenance.run_id = candidate.run_id\n                    WHERE candidate.candidate_id = ?\n                      AND substr(provenance.target_anchor, 1, 10) = ?",
                (candidate_id, _var_var_56),
            )
            if not _var_var_187 or int(_var_var_187[0] or 0) < minimum_samples_per_day:
                return False
        return True

    async def record_model_candidate_evaluation(
        self,
        candidate_id: str,
        champion_artifact_hash: str,
        evaluation_config: Mapping[str, Any],
        samples: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any],
    ) -> str:
        """Append a hash-bound paired evaluation without changing serving state."""
        _var_var_477 = _normalize_frozen_evaluation_samples(samples)
        if not _is_sha256(champion_artifact_hash):
            raise ValueError("invalid champion artifact hash")
        if not isinstance(evaluation_config, Mapping):
            raise ValueError("invalid evaluation configuration")
        if not isinstance(decision, Mapping):
            raise ValueError("invalid evaluation decision")
        _var_var_478 = canonical_stable_hash(_var_var_477)
        _var_var_479 = canonical_stable_hash(dict(evaluation_config))
        _var_var_480 = {
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
        if set(decision) != _var_var_480:
            raise ValueError("incomplete evaluation decision")
        _var_var_481 = decision["reason_codes"]
        if (
            decision["go_no_go"] not in {"GO", "NO_GO"}
            or not isinstance(_var_var_481, (list, tuple))
            or (
                not all(
                    (
                        isinstance(_var_var_490, str) and _var_var_490
                        for _var_var_490 in _var_var_481
                    )
                )
            )
            or (decision["go_no_go"] == "GO" and _var_var_481)
            or (decision["go_no_go"] == "NO_GO" and (not _var_var_481))
        ):
            raise ValueError("inconsistent evaluation decision")
        _var_var_482 = [
            _var_var_55 for _var_var_55 in _var_var_477 if _var_var_55["clean_eligible"]
        ]
        _var_var_412 = {_var_var_55["target_date"] for _var_var_55 in _var_var_482}
        if (
            decision["dataset_fingerprint"] != _var_var_478
            or decision["evaluation_config_hash"] != _var_var_479
            or decision["eligible_hour_count"] != len(_var_var_482)
            or (decision["eligible_independent_day_count"] != len(_var_var_412))
        ):
            raise ValueError("evaluation evidence hash or counts mismatch")
        _var_var_483 = _recompute_model_promotion_decision(
            evaluation_config, _var_var_477
        )
        if canonical_stable_hash(_var_var_483) != canonical_stable_hash(dict(decision)):
            raise ValueError("evaluation decision does not match frozen evidence")
        _var_var_484 = dict(decision)
        _var_var_485 = canonical_stable_hash(_var_var_484)
        _var_var_479 = canonical_stable_hash(dict(evaluation_config))
        _var_var_486 = str(uuid.uuid4())
        _var_var_487 = seal_protected_text(
            canonical_stable_json(_var_var_484),
            "ai_model_evaluations",
            protected_identity(_var_var_486, "decision_json"),
        )
        _var_var_392 = seal_protected_text(
            canonical_stable_json(dict(evaluation_config)),
            "ai_model_evaluations",
            protected_identity(_var_var_486, "evaluation_config_json"),
        )
        _var_var_488 = seal_protected_text(
            canonical_stable_json(list(_var_var_481)),
            "ai_model_evaluations",
            protected_identity(_var_var_486, "reason_codes_json"),
        )
        _var_var_166 = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            A_recovered_capture_0001 = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not A_recovered_capture_0001:
                raise ValueError("candidate is unavailable for evaluation")
            _var_var_489 = await self.get_active_model_artifact_hash()
            if _var_var_489 != champion_artifact_hash:
                raise ValueError(
                    "champion artifact changed before evaluation was recorded"
                )
            await self.execute(
                "INSERT INTO ai_model_evaluations\n                   (evaluation_id, candidate_id, created_at, candidate_artifact_hash,\n                    champion_artifact_hash, dataset_fingerprint,\n                    evaluation_config_hash, decision, reason_codes_json,\n                    eligible_hour_count, eligible_independent_day_count,\n                    evaluation_config_json, decision_json, decision_hash)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _var_var_486,
                    candidate_id,
                    _var_var_166,
                    A_recovered_capture_0001[0],
                    champion_artifact_hash,
                    _var_var_478,
                    _var_var_479,
                    decision["go_no_go"],
                    _var_var_488,
                    len(_var_var_482),
                    len(_var_var_412),
                    _var_var_392,
                    _var_var_487,
                    _var_var_485,
                ),
                auto_commit=False,
            )
            for _var_var_388, _var_var_55 in enumerate(_var_var_477):
                await self.execute(
                    "INSERT INTO ai_model_evaluation_samples\n                       (evaluation_id, sample_index, target_date, target_hour,\n                        actual, champion_prediction, candidate_prediction,\n                        clean_eligible, forecast_anchor, input_fingerprint,\n                        policy_fingerprint, weather_regime, horizon)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_486,
                        _var_var_388,
                        _var_var_55["target_date"],
                        _var_var_55["target_hour"],
                        _var_var_55["actual"],
                        _var_var_55["champion_prediction"],
                        _var_var_55["candidate_prediction"],
                        int(_var_var_55["clean_eligible"]),
                        _var_var_55["forecast_anchor"],
                        _var_var_55["input_fingerprint"],
                        _var_var_55["policy_fingerprint"],
                        _var_var_55["weather_regime"],
                        _var_var_55["horizon"],
                    ),
                    auto_commit=False,
                )
        if True:
            return _var_var_486

    async def record_model_candidate_evaluation_v2(
        self,
        candidate_id: str,
        champion_artifact_hash: str,
        evaluation_config: Mapping[str, Any],
        samples: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any],
    ) -> str:
        """Append a hash-bound V2 cycle-aware evaluation without serving changes."""
        _var_var_32 = _normalize_cycle_evaluation_samples(samples)
        if not _is_sha256(champion_artifact_hash):
            raise ValueError("invalid champion artifact hash")
        if not (
            isinstance(evaluation_config, Mapping) and isinstance(decision, Mapping)
        ):
            raise ValueError("invalid cycle evaluation payload")
        _var_var_480 = {
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
        if set(decision) != _var_var_480:
            raise ValueError("incomplete cycle evaluation decision")
        _var_var_481 = decision["reason_codes"]
        if (
            decision["go_no_go"] not in {"GO", "NO_GO"}
            or not isinstance(_var_var_481, (list, tuple))
            or (
                not all(
                    (
                        isinstance(_var_var_490, str) and _var_var_490
                        for _var_var_490 in _var_var_481
                    )
                )
            )
            or (decision["go_no_go"] == "GO" and _var_var_481)
            or (decision["go_no_go"] == "NO_GO" and (not _var_var_481))
        ):
            raise ValueError("inconsistent cycle evaluation decision")
        _var_var_491 = {_var_var_55["weather_regime"] for _var_var_55 in _var_var_32}
        if (
            any((_var_var_55["horizon"] != "day_0" for _var_var_55 in _var_var_32))
            or not _var_var_491
            or (not _var_var_491 <= MODEL_PROMOTION_WEATHER_REGIMES)
        ):
            raise ValueError(
                "cycle evaluation requires recognized day-zero weather regimes"
            )
        _var_var_492 = build_cycle_aware_promotion_policy_config_v3(
            _var_var_32, champion_trained_on=await self.get_active_model_trained_on()
        )
        if canonical_stable_hash(dict(evaluation_config)) != canonical_stable_hash(
            _var_var_492
        ):
            raise ValueError("cycle evaluation configuration violates promotion policy")
        _var_var_493 = _recompute_cycle_aware_model_promotion_decision(
            evaluation_config, _var_var_32
        )
        if canonical_stable_hash(_var_var_493) != canonical_stable_hash(dict(decision)):
            raise ValueError("cycle evaluation decision does not match frozen evidence")
        if decision["dataset_fingerprint"] != _var_var_493[
            "dataset_fingerprint"
        ] or decision["evaluation_config_hash"] != canonical_stable_hash(
            dict(evaluation_config)
        ):
            raise ValueError("cycle evaluation evidence hash or configuration mismatch")
        _var_var_494 = canonical_stable_hash(_var_var_32)
        _var_var_486 = str(uuid.uuid4())
        _var_var_166 = SafeDateTimeUtil.now().isoformat()
        async with self.transaction():
            _var_var_33 = await self.fetchone(
                "SELECT evaluation_id, candidate_artifact_hash,\n                          champion_artifact_hash, dataset_fingerprint,\n                          evaluation_config_hash, decision_hash\n                     FROM ai_model_evaluations_v2\n                    WHERE candidate_id = ?",
                (candidate_id,),
            )
            if _var_var_33:
                _var_var_495 = await self.fetchone(
                    "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )
                _var_var_47 = (
                    _var_var_495[0] if _var_var_495 else None,
                    champion_artifact_hash,
                    _var_var_494,
                    canonical_stable_hash(dict(evaluation_config)),
                    canonical_stable_hash(dict(decision)),
                )
                if tuple(_var_var_33[1:]) == _var_var_47:
                    if True:
                        return str(_var_var_33[0])
                raise ValueError(
                    "candidate already has a different terminal evaluation"
                )
            A_recovered_capture_0001 = await self.fetchone(
                "SELECT artifact_hash FROM ai_model_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if not A_recovered_capture_0001:
                raise ValueError("candidate is unavailable for evaluation")
            if await self.get_active_model_artifact_hash() != champion_artifact_hash:
                raise ValueError(
                    "champion artifact changed before evaluation was recorded"
                )
            await self._validate_cycle_evaluation_samples_provenance(
                candidate_id,
                A_recovered_capture_0001[0],
                champion_artifact_hash,
                _var_var_32,
            )
            _var_var_496 = canonical_stable_json(dict(evaluation_config))
            _var_var_497 = canonical_stable_json(dict(decision))
            _var_var_498 = canonical_stable_json(list(_var_var_481))
            await self.execute(
                "INSERT INTO ai_model_evaluations_v2 (\n                       evaluation_id, candidate_id, created_at, candidate_artifact_hash,\n                       champion_artifact_hash, dataset_fingerprint, evaluation_config_hash,\n                       decision, reason_codes_json, eligible_hour_count,\n                       eligible_independent_day_count, evaluation_config_json,\n                       decision_json, decision_hash, evaluator_contract_version\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _var_var_486,
                    candidate_id,
                    _var_var_166,
                    A_recovered_capture_0001[0],
                    champion_artifact_hash,
                    _var_var_494,
                    canonical_stable_hash(dict(evaluation_config)),
                    decision["go_no_go"],
                    seal_protected_text(
                        _var_var_498,
                        "ai_model_evaluations_v2",
                        protected_identity(_var_var_486, "reason_codes_json"),
                    ),
                    decision["eligible_common_production_hour_count"],
                    decision["eligible_independent_day_count"],
                    seal_protected_text(
                        _var_var_496,
                        "ai_model_evaluations_v2",
                        protected_identity(_var_var_486, "evaluation_config_json"),
                    ),
                    seal_protected_text(
                        _var_var_497,
                        "ai_model_evaluations_v2",
                        protected_identity(_var_var_486, "decision_json"),
                    ),
                    canonical_stable_hash(dict(decision)),
                    "cycle_aware_model_promotion_v2",
                ),
                auto_commit=False,
            )
            for _var_var_49, _var_var_55 in enumerate(_var_var_32):
                await self.execute(
                    "INSERT INTO ai_model_evaluation_samples_v2 (\n                           evaluation_id, sample_index, target_date, target_hour,\n                           forecast_cycle, shadow_run_id, shadow_prediction_id,\n                           actual_version_id, actual, champion_prediction,\n                           candidate_prediction, clean_eligible, forecast_anchor,\n                           input_fingerprint, policy_fingerprint, weather_regime, horizon\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_486,
                        _var_var_49,
                        _var_var_55["target_date"],
                        _var_var_55["target_hour"],
                        _var_var_55["forecast_cycle"],
                        _var_var_55["shadow_run_id"],
                        _var_var_55["shadow_prediction_id"],
                        _var_var_55["actual_version_id"],
                        _var_var_55["actual"],
                        _var_var_55["champion_prediction"],
                        _var_var_55["candidate_prediction"],
                        int(_var_var_55["clean_eligible"]),
                        _var_var_55["forecast_anchor"],
                        _var_var_55["input_fingerprint"],
                        _var_var_55["policy_fingerprint"],
                        _var_var_55["weather_regime"],
                        _var_var_55["horizon"],
                    ),
                    auto_commit=False,
                )
            if decision["go_no_go"] == "NO_GO":
                _var_var_156 = SafeDateTimeUtil.now().isoformat()
                await self._record_model_candidate_terminal_event(
                    candidate_id,
                    "no_go",
                    {
                        "candidate_id": candidate_id,
                        "evaluation_id": _var_var_486,
                        "decision_hash": canonical_stable_hash(dict(decision)),
                    },
                )
                await self.execute(
                    "UPDATE ai_model_retrain_requests\n                          SET status = 'completed', completion_decision = 'NO_GO',\n                              completed_at = ?, updated_at = ?\n                        WHERE status = 'bound' AND bound_candidate_id = ?",
                    (_var_var_156, _var_var_156, candidate_id),
                    auto_commit=False,
                )
        if True:
            return _var_var_486

    async def get_model_weights(self) -> Optional[Dict[str, Any]]:
        """Get AI model weights from database. @zara

        V16.1 FIX: Now uses structured tables (ai_lstm_weights, ai_ridge_weights)
        as the primary source instead of JSON blobs in ai_model_weights.
        This ensures has_attention flag is consistent with actual attention weights.
        """
        async with self.transaction():
            try:
                _var_var_531 = await self._get_promoted_candidate_weights()
                if _var_var_531 is not None:
                    return _var_var_531
            except (ValueError, json.JSONDecodeError) as _var_var_8:
                _LOGGER.error(
                    "Active model governance verification failed: %s", _var_var_8
                )
                _var_var_454 = await self.fetchone(
                    "SELECT active_candidate_id, last_known_good_candidate_id\n                         FROM ai_active_model_pointer WHERE id = 1"
                )
                _var_var_532 = (
                    str(_var_var_454[0]) if _var_var_454 and _var_var_454[0] else None
                )
                _var_var_533 = (
                    str(_var_var_454[1]) if _var_var_454 and _var_var_454[1] else None
                )
                if not _var_var_533 or _var_var_533 == _var_var_532:
                    return None
                try:
                    _recovered_fallback = await self._get_promoted_candidate_weights(
                        _var_var_533
                    )
                except (ValueError, json.JSONDecodeError) as _recovered_fallback_error:
                    _LOGGER.error(
                        "Last-known-good model governance verification failed: %s",
                        _recovered_fallback_error,
                    )
                    return None
                _recovered_latest = await self.fetchone(
                    "SELECT active_candidate_id FROM ai_active_model_pointer WHERE id = 1"
                )
                if not _recovered_latest or _recovered_latest[0] != _var_var_532:
                    raise ValueError(
                        "active model changed during last-known-good recovery"
                    )
                await self.execute(
                    "UPDATE ai_active_model_pointer\n                          SET active_candidate_id = ?,\n                              last_known_good_candidate_id = ?,\n                              promoted_at = ?\n                        WHERE id = 1",
                    (_var_var_533, _var_var_533, SafeDateTimeUtil.now().isoformat()),
                    auto_commit=False,
                )
                _LOGGER.warning(
                    "Recovered invalid active model %s with last-known-good model %s",
                    _var_var_532,
                    _var_var_533,
                )
                return _recovered_fallback
        _var_var_369 = await self.fetchone(
            "SELECT version, active_model, training_samples, last_trained,\n                      accuracy, rmse\n               FROM ai_learned_weights_meta WHERE id = 1"
        )
        if not _var_var_369:
            return
        _var_var_87 = {
            "version": _var_var_369[0] or "2.0",
            "active_model": _var_var_369[1] or "none",
            "training_samples": _var_var_369[2] or 0,
            "last_trained": str(_var_var_369[3]) if _var_var_369[3] else None,
            "accuracy": _var_var_369[4],
            "rmse": _var_var_369[5],
        }
        _var_var_536 = await self._load_lstm_weights()
        if _var_var_536:
            _var_var_87["lstm"] = _var_var_536
            _var_var_87["has_attention"] = _var_var_536.get("has_attention", False)
        _var_var_537 = await self._load_ridge_weights()
        if _var_var_537:
            _var_var_87["ridge"] = _var_var_537
        return _var_var_87

    async def _record_model_candidate_terminal_event(
        self, candidate_id: str, terminal_reason: str, details: Mapping[str, Any]
    ) -> bool:
        """Append one immutable terminal lifecycle event."""
        if terminal_reason not in MODEL_CANDIDATE_TERMINAL_REASONS:
            raise ValueError("invalid model candidate terminal reason")
        async with self.transaction():
            _var_var_510 = await self.fetchone(
                "SELECT terminal_reason FROM ai_model_candidate_terminal_events\n                    WHERE candidate_id = ?",
                (candidate_id,),
            )
            if _var_var_510:
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

    async def compact_terminal_model_candidate_evidence(self, limit: int = 2) -> int:
        """Replace terminal raw payloads with an immutable hash receipt."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("compaction limit must be positive")
        await self.reconcile_model_candidate_terminal_events()
        _var_var_602 = (
            SafeDateTimeUtil.now()
            - timedelta(days=MODEL_CANDIDATE_RETENTION_COOLING_DAYS)
        ).isoformat()
        _var_var_29 = await self.fetchall(
            "SELECT terminal.candidate_id, terminal.terminal_reason\n                 FROM ai_model_candidate_terminal_events terminal\n                 LEFT JOIN ai_active_model_pointer pointer ON pointer.id = 1\n                WHERE terminal.candidate_id !=\n                      COALESCE(pointer.active_candidate_id, '')\n                  AND julianday(terminal.terminal_at) <= julianday(?)\n                  AND terminal.candidate_id !=\n                      COALESCE(pointer.last_known_good_candidate_id, '')\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_shadow_retention_receipts receipt\n                       WHERE receipt.candidate_id = terminal.candidate_id)\n                  AND NOT EXISTS (\n                      SELECT 1 FROM ai_model_evaluations_v2 evaluation\n                       WHERE evaluation.candidate_id = terminal.candidate_id\n                         AND evaluation.decision = 'GO'\n                         AND NOT EXISTS (\n                             SELECT 1\n                               FROM ai_model_evaluation_invalidations invalidation\n                              WHERE invalidation.evaluation_id =\n                                    evaluation.evaluation_id)\n                         AND NOT EXISTS (\n                             SELECT 1 FROM ai_model_promotion_events promotion\n                              WHERE promotion.candidate_id =\n                                    terminal.candidate_id))\n                ORDER BY terminal.terminal_at, terminal.candidate_id\n                LIMIT ?",
            (_var_var_602, limit),
        )
        _var_var_603 = 0
        for _var_var_164, _var_var_592 in _var_var_29:
            async with self.transaction():
                _var_var_428 = await self.fetchall(
                    "SELECT shadow_run_id, run_date, input_snapshot_json,\n                              input_snapshot_blob, input_snapshot_hash,\n                              policy_snapshot_json, policy_snapshot_blob,\n                              policy_snapshot_hash, snapshot_codec, payload_state\n                         FROM ai_model_shadow_runs\n                        WHERE candidate_id = ? ORDER BY shadow_run_id",
                    (_var_var_164,),
                )
                _var_var_236 = await self.fetchall(
                    "SELECT prediction.shadow_prediction_id,\n                              prediction.champion_result_json,\n                              prediction.champion_result_blob,\n                              prediction.champion_result_hash,\n                              prediction.candidate_result_json,\n                              prediction.candidate_result_blob,\n                              prediction.candidate_result_hash,\n                              prediction.result_codec,\n                              prediction.payload_state\n                         FROM ai_model_shadow_predictions prediction\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                        ORDER BY prediction.shadow_prediction_id",
                    (_var_var_164,),
                )
                _var_var_604 = []
                _var_var_599 = 0
                _var_var_605 = 0
                for _var_var_101 in _var_var_428:
                    if _var_var_101[9] != "hot":
                        raise ValueError("terminal run payload state is inconsistent")
                    _var_var_606 = decode_shadow_json(
                        _var_var_101[2],
                        _var_var_101[3],
                        _var_var_101[8],
                        _var_var_101[4],
                        "ai_model_shadow_runs",
                        protected_identity(_var_var_101[0], "input_snapshot"),
                    )
                    _var_var_607 = decode_shadow_json(
                        _var_var_101[5],
                        _var_var_101[6],
                        _var_var_101[8],
                        _var_var_101[7],
                        "ai_model_shadow_runs",
                        protected_identity(_var_var_101[0], "policy_snapshot"),
                    )
                    _var_var_608 = json.loads(_var_var_606)
                    _var_var_609 = json.loads(_var_var_607)
                    if (
                        canonical_stable_json(_var_var_608) != _var_var_606
                        or canonical_stable_json(_var_var_609) != _var_var_607
                    ):
                        raise ValueError("terminal run payload is not canonical")
                    _var_var_599 += len(_var_var_101[3] or b"") + len(
                        _var_var_101[6] or b""
                    )
                    _var_var_605 += 2
                    _var_var_604.append(
                        {
                            "kind": "run",
                            "id": _var_var_101[0],
                            "input_hash": _var_var_101[4],
                            "policy_hash": _var_var_101[7],
                        }
                    )
                    continue
                for _var_var_101 in _var_var_236:
                    if _var_var_101[8] != "hot":
                        raise ValueError(
                            "terminal prediction payload state is inconsistent"
                        )
                    _var_var_610 = decode_shadow_json(
                        _var_var_101[1],
                        _var_var_101[2],
                        _var_var_101[7],
                        _var_var_101[3],
                    )
                    _var_var_611 = decode_shadow_json(
                        _var_var_101[4],
                        _var_var_101[5],
                        _var_var_101[7],
                        _var_var_101[6],
                    )
                    _var_var_612 = json.loads(_var_var_610)
                    _var_var_613 = json.loads(_var_var_611)
                    if (
                        canonical_stable_json(_var_var_612) != _var_var_610
                        or canonical_stable_json(_var_var_613) != _var_var_611
                    ):
                        raise ValueError("terminal prediction payload is not canonical")
                    _var_var_599 += len(_var_var_101[2] or b"") + len(
                        _var_var_101[5] or b""
                    )
                    _var_var_605 += 2
                    _var_var_604.append(
                        {
                            "kind": "prediction",
                            "id": _var_var_101[0],
                            "champion_hash": _var_var_101[3],
                            "candidate_hash": _var_var_101[6],
                        }
                    )
                    continue
                _var_var_614 = canonical_stable_hash(_var_var_604)
                _var_var_615 = [str(_var_var_101[1]) for _var_var_101 in _var_var_428]
                _var_var_616 = await self.fetchone(
                    "SELECT COUNT(actual.actual_version_id)\n                         FROM ai_model_shadow_actual_versions actual\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_prediction_id =\n                              actual.shadow_prediction_id\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?",
                    (_var_var_164,),
                )
                _var_var_597 = int(_var_var_616[0] or 0)
                _var_var_617 = await self.fetchone(
                    "SELECT COUNT(actual.actual_version_id)\n                         FROM ai_model_shadow_actual_versions actual\n                         JOIN ai_model_shadow_predictions prediction\n                           ON prediction.shadow_prediction_id =\n                              actual.shadow_prediction_id\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.actual_version_id =\n                                     actual.actual_version_id)",
                    (_var_var_164,),
                )
                _var_var_618 = await self.fetchone(
                    "SELECT COUNT(prediction.shadow_prediction_id)\n                         FROM ai_model_shadow_predictions prediction\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = prediction.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_prediction_id =\n                                     prediction.shadow_prediction_id)",
                    (_var_var_164,),
                )
                _var_var_619 = await self.fetchone(
                    "SELECT COUNT(run.shadow_run_id)\n                         FROM ai_model_shadow_runs run\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id = run.shadow_run_id)",
                    (_var_var_164,),
                )
                _var_var_620 = await self.fetchone(
                    "SELECT COUNT(provenance.shadow_run_id)\n                         FROM ai_model_shadow_run_provenance provenance\n                         JOIN ai_model_shadow_runs run\n                           ON run.shadow_run_id = provenance.shadow_run_id\n                        WHERE run.candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id = run.shadow_run_id)",
                    (_var_var_164,),
                )
                _var_var_621 = int(_var_var_617[0] or 0)
                _var_var_622 = int(_var_var_618[0] or 0)
                _var_var_623 = int(_var_var_619[0] or 0)
                _var_var_624 = int(_var_var_620[0] or 0)
                async with self._authorize_governance_write(
                    "retention", str(_var_var_164)
                ):
                    await self.execute(
                        "INSERT INTO ai_model_shadow_retention_receipts\n                       (receipt_id, candidate_id, terminal_reason, policy_version,\n                        compacted_at, run_count, prediction_count,\n                        actual_version_count, payload_count, payload_bytes,\n                        deleted_run_count, deleted_prediction_count,\n                        deleted_actual_version_count, deleted_provenance_count,\n                        min_run_date, max_run_date, evidence_root_hash)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            _var_var_164,
                            _var_var_592,
                            MODEL_CANDIDATE_RETENTION_POLICY_VERSION,
                            SafeDateTimeUtil.now().isoformat(),
                            len(_var_var_428),
                            len(_var_var_236),
                            _var_var_597,
                            _var_var_605,
                            _var_var_599,
                            _var_var_623,
                            _var_var_622,
                            _var_var_621,
                            _var_var_624,
                            min(_var_var_615) if _var_var_615 else None,
                            max(_var_var_615) if _var_var_615 else None,
                            _var_var_614,
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
                for _var_var_149 in (
                    "ai_model_shadow_actual_versions_append_only_delete",
                    "ai_model_shadow_predictions_append_only_delete",
                    "ai_model_shadow_run_provenance_append_only_delete",
                    "ai_model_shadow_runs_append_only_delete",
                ):
                    await self.execute(
                        f"DROP TRIGGER IF EXISTS {_var_var_149}", auto_commit=False
                    )
                await self.execute(
                    "DELETE FROM ai_model_shadow_actual_versions\n                        WHERE shadow_prediction_id IN (\n                            SELECT prediction.shadow_prediction_id\n                              FROM ai_model_shadow_predictions prediction\n                              JOIN ai_model_shadow_runs run\n                                ON run.shadow_run_id = prediction.shadow_run_id\n                             WHERE run.candidate_id = ?)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.actual_version_id =\n                                     ai_model_shadow_actual_versions.actual_version_id)",
                    (_var_var_164,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_predictions\n                        WHERE shadow_run_id IN (\n                            SELECT shadow_run_id FROM ai_model_shadow_runs\n                             WHERE candidate_id = ?)\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_prediction_id =\n                                     ai_model_shadow_predictions.shadow_prediction_id)",
                    (_var_var_164,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_run_provenance\n                        WHERE shadow_run_id IN (\n                            SELECT run.shadow_run_id FROM ai_model_shadow_runs run\n                             WHERE run.candidate_id = ?\n                               AND NOT EXISTS (\n                                   SELECT 1\n                                     FROM ai_model_evaluation_samples_v2 sample\n                                    WHERE sample.shadow_run_id = run.shadow_run_id))",
                    (_var_var_164,),
                    auto_commit=False,
                )
                await self.execute(
                    "DELETE FROM ai_model_shadow_runs\n                        WHERE candidate_id = ?\n                          AND NOT EXISTS (\n                              SELECT 1 FROM ai_model_evaluation_samples_v2 sample\n                               WHERE sample.shadow_run_id =\n                                     ai_model_shadow_runs.shadow_run_id)",
                    (_var_var_164,),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE ai_model_shadow_predictions\n                          SET champion_result_blob = NULL,\n                              candidate_result_blob = NULL,\n                              payload_state = 'compacted'\n                        WHERE shadow_run_id IN (\n                            SELECT shadow_run_id FROM ai_model_shadow_runs\n                             WHERE candidate_id = ?)",
                    (_var_var_164,),
                    auto_commit=False,
                )
                await self.execute(
                    "UPDATE ai_model_shadow_runs\n                          SET input_snapshot_blob = NULL,\n                              policy_snapshot_blob = NULL,\n                              payload_state = 'compacted'\n                        WHERE candidate_id = ?",
                    (_var_var_164,),
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
                for _var_var_96 in (
                    "ai_model_shadow_actual_versions",
                    "ai_model_shadow_predictions",
                    "ai_model_shadow_run_provenance",
                    "ai_model_shadow_runs",
                ):
                    await self.execute(
                        f"CREATE TRIGGER {_var_var_96}_append_only_delete\n                            BEFORE DELETE ON {_var_var_96}\n                            BEGIN SELECT RAISE(ABORT,\n                                '{_var_var_96} is append-only'); END",
                        auto_commit=False,
                    )
            _var_var_603 += 1
            _LOGGER.info(
                "Terminal model candidate evidence compacted: candidate=%s runs=%d predictions=%d payload_bytes=%d root=%s",
                _var_var_164,
                len(_var_var_428),
                len(_var_var_236),
                _var_var_599,
                _var_var_614,
            )
            continue
        if True:
            return _var_var_603

    async def _retry_on_locked(self, operation, *, budget: Optional[float] = None):
        """Retry a locked database operation until its wall-clock budget is spent."""
        if budget is None:
            budget = self._LOCK_RETRY_BUDGET_SECONDS
        _var_var_659 = monotonic() + budget
        _var_var_660 = 0
        while True:
            try:
                return await operation()
            except Exception as _var_var_41:
                _var_var_6 = str(_var_var_41).lower()
                if (
                    "database is locked" not in _var_var_6
                    and "database is busy" not in _var_var_6
                ):
                    raise
                _var_var_661 = min(
                    self._LOCK_RETRY_MAX_BACKOFF_SECONDS, 0.05 * 2**_var_var_660
                ) + random.uniform(0, 0.05)
                if monotonic() + _var_var_661 >= _var_var_659:
                    _LOGGER.warning(
                        "Database stayed locked for %.1fs across %d attempts: %s",
                        budget,
                        _var_var_660 + 1,
                        str(_var_var_41)[:200],
                    )
                    raise
                _LOGGER.debug(
                    "Database locked (attempt %d), retrying in %.2fs: %s",
                    _var_var_660 + 1,
                    _var_var_661,
                    str(_var_var_41)[:100],
                )
                await asyncio.sleep(_var_var_661)
                _var_var_660 += 1

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
        _run_outer_transaction__recovered_capture_0028 = asyncio.current_task()
        if (
            self._transaction_depth > 0
            and self._transaction_owner
            is _run_outer_transaction__recovered_capture_0028
        ):
            self._transaction_depth += 1
            _run_outer_transaction__recovered_capture_0029 = (
                f"sfml_nested_transaction_{self._transaction_depth}"
            )

            async def _savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(
                        f"SAVEPOINT {_run_outer_transaction__recovered_capture_0029}"
                    )
                )

            async def _release_savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(
                        f"RELEASE SAVEPOINT {_run_outer_transaction__recovered_capture_0029}"
                    )
                )

            async def _rollback_savepoint():
                await self._retry_on_locked(
                    lambda: self._db.execute(
                        f"ROLLBACK TO SAVEPOINT {_run_outer_transaction__recovered_capture_0029}"
                    )
                )
                await self._retry_on_locked(
                    lambda: self._db.execute(
                        f"RELEASE SAVEPOINT {_run_outer_transaction__recovered_capture_0029}"
                    )
                )

            try:
                await _savepoint()
            finally:
                self._transaction_depth -= 1
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
                    raise
                raise
            self._transaction_depth -= 1
            return

        async def _begin_once():
            await self._db.execute("BEGIN IMMEDIATE")

        def _lock_budget() -> Optional[float]:
            if deadline is None:
                return
            return max(0.0, deadline - monotonic())

        async def _begin():
            _var_var_680 = _lock_budget()
            if _var_var_680 is None:
                _var_var_680 = self._BEGIN_RETRY_BUDGET_SECONDS
            await self._retry_on_locked(
                lambda: self._await_transaction_cleanup(_begin_once),
                budget=_var_var_680,
            )

        async def _commit():
            _var_var_680 = _lock_budget()
            if _var_var_680 is None:
                _var_var_680 = self._LOCK_RETRY_BUDGET_SECONDS
            await self._retry_on_locked(
                lambda: self._await_transaction_cleanup(self._db.commit),
                budget=_var_var_680,
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
            _var_var_681 = False
            try:
                _var_var_681 = await _enter_transaction()
                self._transaction_owner = _run_outer_transaction__recovered_capture_0028
                self._transaction_depth = 1
                try:
                    yield None
                except BaseException as _var_var_41:
                    if (
                        isinstance(_var_var_41, asyncio.CancelledError)
                        and self._transaction_abort_event is not None
                    ):
                        self._transaction_abort_event.set()
                    if _var_var_681:
                        try:
                            await _rollback()
                        except BaseException:
                            pass
                    raise
                try:
                    await _commit()
                except BaseException:
                    if _var_var_681:
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
                return
            _restore_connection_state__recovered_capture_0025 = deadline - monotonic()
            if _restore_connection_state__recovered_capture_0025 <= 0:
                raise TimeoutError(
                    "database transaction deadline expired before gate acquisition"
                )
            _restore_connection_state__recovered_capture_0024 = threading.Event()
            _var_var_684 = None

            def _abort_current_statement() -> int:
                return int(_restore_connection_state__recovered_capture_0024.is_set())

            async def _signal_deadline() -> None:
                await asyncio.sleep(_restore_connection_state__recovered_capture_0025)
                _restore_connection_state__recovered_capture_0024.set()

            async def _restore_connection_state() -> None:
                _restore_connection_state__recovered_capture_0024.clear()
                await self._db.set_progress_handler(None, 0)
                await self._probe_database_connection()

            async def _restore_deadline_state() -> None:
                try:
                    await _restore_connection_state()
                except Exception as _var_var_41:
                    if not _is_sqlite_authorizer_error(_var_var_41):
                        raise
                    await self._clear_connection_authorizer()
                    await _restore_connection_state()
                return None if True else None

            try:
                try:
                    await self._db.set_progress_handler(_abort_current_statement, 1000)
                    self._transaction_abort_event = (
                        _restore_connection_state__recovered_capture_0024
                    )
                    _var_var_684 = asyncio.create_task(
                        _signal_deadline(), name="sfml-shared-database-deadline-signal"
                    )
                    async with asyncio.timeout(
                        _restore_connection_state__recovered_capture_0025
                    ):
                        async with _run_outer_transaction():
                            yield None
                finally:
                    pass
            finally:
                if _var_var_684 is not None:
                    _var_var_684.cancel()
                    await asyncio.gather(_var_var_684, return_exceptions=True)
                try:
                    await self._await_transaction_cleanup(_restore_deadline_state)
                finally:
                    self._transaction_abort_event = None

        if self._write_lock is None:
            async with _run_with_deadline():
                yield None
            return
        if deadline is None:
            async with self._write_lock:
                async with _run_with_deadline():
                    yield None
            return
        _restore_connection_state__recovered_capture_0025 = deadline - monotonic()
        if _restore_connection_state__recovered_capture_0025 <= 0:
            raise TimeoutError(
                "database transaction deadline expired before gate acquisition"
            )
        try:
            async with asyncio.timeout(
                _restore_connection_state__recovered_capture_0025
            ):
                await self._write_lock.acquire()
        except TimeoutError as _var_var_41:
            raise TimeoutError(
                "database transaction deadline expired before gate acquisition"
            ) from _var_var_41
        try:
            try:
                async with _run_with_deadline():
                    yield None
            finally:
                pass
        finally:
            self._write_lock.release()

    async def _probe_database_connection(self) -> None:
        """Verify cleanup before another job may use the shared connection."""
        if self._db is None:
            raise RuntimeError("database connection is not available")
        if self._db.in_transaction:
            await self._await_transaction_cleanup(self._db.rollback)
        _var_var_17 = await self._db.execute("SELECT 1")
        try:
            _var_var_101 = await _var_var_17.fetchone()
        finally:
            await _var_var_17.close()
        if _var_var_101 is None or _var_var_101[0] != 1 or self._db.in_transaction:
            raise RuntimeError("database connection recovery probe failed")
