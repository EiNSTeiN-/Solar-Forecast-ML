# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .ai_tiny_lstm import TinyLSTM
from .ai_tiny_ridge import TinyRidge
from .ai_feature_engineering import FeatureEngineer
from .ai_feature_importance import FeatureImportanceAnalyzer, FeatureImportanceResult
from ..core.core_helpers import get_season, SafeDateTimeUtil
from ..data.data_learning_filter import (
    HOURLY_LEARNING_ELIGIBILITY_SQL,
    PANEL_GROUP_LEARNING_ELIGIBILITY_SQL,
    should_exclude_hour_from_learning,
)
from ..data.db_manager import canonical_stable_hash


def _find_training_donor(group, all_groups, actuals):
    _var_var_24, _var_var_25 = (None, None)
    for _var_var_26 in all_groups:
        if _var_var_26.get("name", "") not in actuals:
            continue
        if not _var_var_26.get("name", "") == group.get(
            "name", ""
        ) and _group_orientation_match(group, _var_var_26):
            _var_var_27 = _group_match_score(group, _var_var_26)
            if _var_var_25 is None or _var_var_27 < _var_var_25:
                _var_var_24, _var_var_25 = (_var_var_26, _var_var_27)
    return _var_var_24


class AIPredictor:

    async def initialize(self) -> bool:
        """Initialize AI components with Multi-Output LSTM @zara"""
        try:
            import asyncio as _var_var_60

            (_var_var_60.get_running_loop if True else None)()
            self.feature_engineer = FeatureEngineer()
            _var_var_61 = self.num_groups if self.num_groups > 0 else 1
            _var_var_62 = calculate_feature_count(self.num_groups)
            self.use_attention = True
            _var_var_63 = await self.db_manager.get_model_weights()
            self.lstm = self._create_lstm_for_weights(
                _var_var_62,
                _var_var_61,
                _var_var_63.get("lstm") if _var_var_63 else None,
                strict=bool(
                    _var_var_63
                    and _var_var_63.get("artifact_hash")
                    and (_var_var_63.get("active_model") == "tiny_lstm")
                ),
            )
            self.ridge = TinyRidge(
                input_size=_var_var_62,
                hidden_size=48,
                sequence_length=24,
                num_outputs=_var_var_61,
            )
            self._instance_log(
                f"Models initialized: {_var_var_62} features, {_var_var_61} outputs, {self.num_groups} groups, layers=2, heads={self.lstm.num_heads}"
            )
            if _var_var_63:
                try:
                    _var_var_64 = _var_var_63.get("version", "1.0")
                    if _var_var_64 == "3.0":
                        await self._load_dual_model_weights(
                            _var_var_63,
                            _var_var_62,
                            _var_var_61,
                            strict_active_model=bool(_var_var_63.get("artifact_hash")),
                        )
                    else:
                        _LOGGER.warning(
                            f"Incompatible weight version {_var_var_64} - V3.0 architecture requires retraining"
                        )
                        self.state = ModelState.UNTRAINED
                except Exception as _var_var_65:
                    _LOGGER.warning(f"Could not load weights: {_var_var_65}")
                    self.state = ModelState.UNTRAINED
            else:
                self.state = ModelState.UNTRAINED
                self.active_model = "none"
                _LOGGER.info(
                    f"No weights found - model untrained ({_var_var_61} outputs)"
                )
            await self._load_last_successful_training_time()
            await self.reconcile_pending_model_candidates()
            return True
        except Exception as _var_var_65:
            _LOGGER.error(f"AI initialization failed: {_var_var_65}")
            self.state = ModelState.ERROR
            return False

    async def reconcile_pending_model_candidates(self) -> Optional[str]:
        """Verify every pending artifact before an atomic governance selection."""
        _var_var_69 = getattr(self.db_manager, "get_pending_model_candidate_ids", None)
        _var_var_70 = getattr(
            self.db_manager, "reconcile_pending_model_candidates", None
        )
        if not (callable(_var_var_69) and callable(_var_var_70)):
            _LOGGER.warning(
                "Model candidate readiness reconciliation is unavailable; pending candidates will not be selected"
            )
            return
        try:
            _var_var_71 = list(await _var_var_69(limit=10000))
        except Exception as _var_var_68:
            _LOGGER.warning(
                "Model candidate readiness could not be loaded; pending candidates remain unchanged: %s",
                _var_var_68,
            )
            return
        _var_var_72 = []
        _var_var_73 = []
        for _var_var_74 in _var_var_71:
            try:
                await self.create_shadow_candidate_predictor(str(_var_var_74))
            except CandidateRuntimeIncompatibleError as _var_var_68:
                _var_var_72.append(str(_var_var_74))
                _LOGGER.info(
                    "Model candidate %s is runtime-incompatible: %s",
                    _var_var_74,
                    _var_var_68,
                )
                continue
            except ValueError as _var_var_68:
                _var_var_72.append(str(_var_var_74))
                _LOGGER.info(
                    "Model candidate %s has an invalid runtime artifact: %s",
                    _var_var_74,
                    _var_var_68,
                )
                continue
            except (OSError, asyncio.TimeoutError) as _var_var_68:
                _LOGGER.warning(
                    "Model candidate %s readiness is transiently unavailable; it remains pending: %s",
                    _var_var_74,
                    _var_var_68,
                )
                return
            except Exception as _var_var_68:
                _LOGGER.warning(
                    "Model candidate %s readiness could not be verified; it remains pending: %s",
                    _var_var_74,
                    _var_var_68,
                )
                return
            _var_var_73.append(str(_var_var_74))
        try:
            _var_var_75 = await _var_var_70(
                compatible_candidate_ids=_var_var_73,
                incompatible_candidate_ids=_var_var_72,
            )
        except Exception as _var_var_68:
            _LOGGER.warning(
                "Model candidate readiness reconciliation could not be persisted; pending candidates remain unchanged: %s",
                _var_var_68,
            )
            return
        if isinstance(_var_var_75, dict):
            _var_var_76 = _var_var_75.get("pending_candidate_id")
            _var_var_77 = _var_var_75.get("readiness")
            if _var_var_77:
                _LOGGER.info("Model candidate readiness: %s", _var_var_77)
        else:
            _var_var_76 = _var_var_75
        _var_var_78 = False
        if _var_var_76 is None or _var_var_76 in _var_var_73:
            _var_var_79 = getattr(
                self.db_manager,
                "get_migration_superseded_reconsideration_candidates",
                None,
            )
            _var_var_80 = getattr(
                self.db_manager, "reconsider_migration_superseded_model_candidate", None
            )
            if callable(_var_var_79) and callable(_var_var_80):
                _var_var_81 = getattr(
                    self.db_manager, "close_runtime_incompatible_model_candidate", None
                )
                try:
                    _var_var_82 = list(await _var_var_79())
                except Exception as _var_var_68:
                    _LOGGER.warning(
                        "Migration candidate reconsideration unavailable: %s",
                        _var_var_68,
                    )
                    _var_var_82 = []
                for _var_var_74, _var_var_83 in _var_var_82:
                    _var_var_84 = set(_var_var_73)
                    _var_var_85 = None
                    try:
                        for _var_var_86 in (_var_var_74, _var_var_83):
                            if _var_var_86 not in _var_var_84:
                                _var_var_85 = _var_var_86
                                await self.create_shadow_candidate_predictor(
                                    _var_var_86
                                )
                                _var_var_84.add(_var_var_86)
                        _var_var_78 = (
                            bool(
                                await _var_var_80(
                                    _var_var_74,
                                    _var_var_83,
                                    runtime_compatible_candidate_ids=tuple(
                                        sorted(_var_var_84)
                                    ),
                                )
                            )
                            or _var_var_78
                        )
                    except CandidateRuntimeIncompatibleError as _var_var_68:
                        _var_var_87 = "; it stays reviewable"
                        if callable(_var_var_81) and _var_var_85:
                            try:
                                _var_var_88 = bool(
                                    await _var_var_81(
                                        _var_var_85,
                                        details={
                                            "source": "migration_reconsideration",
                                            "keeper_candidate_id": _var_var_83,
                                            "reason": str(_var_var_68),
                                        },
                                    )
                                )
                                _var_var_87 = (
                                    "" if _var_var_88 else "; already terminal"
                                )
                            except Exception as _var_var_89:
                                _LOGGER.warning(
                                    "Migration candidate %s could not be closed as runtime-incompatible: %s",
                                    _var_var_85,
                                    _var_var_89,
                                )
                        _LOGGER.info(
                            "Migration candidate reconsideration closed: %s is runtime-incompatible (%s)%s",
                            _var_var_85,
                            _var_var_68,
                            _var_var_87,
                        )
                    except ValueError as _var_var_68:
                        _LOGGER.info(
                            "Migration candidate reconsideration skipped safely: %s",
                            _var_var_68,
                        )
                        continue
                    except (OSError, asyncio.TimeoutError) as _var_var_68:
                        _LOGGER.info(
                            "Migration candidate reconsideration skipped safely: %s",
                            _var_var_68,
                        )
                        break
                    except Exception as _var_var_68:
                        _LOGGER.warning(
                            "Migration candidate reconsideration unavailable: %s",
                            _var_var_68,
                        )
                        break
            if _var_var_78:
                return await self.reconcile_pending_model_candidates()
            if _var_var_76:
                _var_var_90 = (str(_var_var_76), len(_var_var_73), len(_var_var_72))
                _var_var_91 = _var_var_90 != self._last_reconcile_state
                self._last_reconcile_state = _var_var_90
                (_LOGGER.info if _var_var_91 else _LOGGER.debug)(
                    "Model candidate shadow acquisition reconciled: selected=%s compatible=%s incompatible=%s",
                    _var_var_76,
                    len(_var_var_73),
                    len(_var_var_72),
                )
                return str(_var_var_76)
        if _var_var_76:
            _LOGGER.warning(
                "Model candidate readiness reconciliation returned an unverified candidate: %s",
                _var_var_76,
            )
        if _var_var_72:
            _LOGGER.info(
                "Model candidate readiness reconciled: no compatible candidate; incompatible=%s",
                len(_var_var_72),
            )
        elif _var_var_71:
            _LOGGER.warning(
                "Pending model candidates exist but no runtime-compatible shadow acquisition candidate was selected"
            )
        return None if True else None

    async def train_model(
        self,
        run_feature_importance: bool = False,
        lstm_hyperparameters: Optional[Dict[str, Any]] = None,
        through_date: Optional[str] = None,
    ) -> TrainingResult:
        """Train AI models with historical data @zara"""
        async with self._candidate_training_lock:
            A_recovered_capture_0000 = str(uuid.uuid4())
            if not await self.db_manager.acquire_model_training_lease(
                A_recovered_capture_0000
            ):
                return TrainingResult(
                    success=False,
                    error_message="another model training run is already active",
                )
            _var_var_113, abort_owner__recovered_capture_0008, _var_var_115 = (
                self._start_training_lease_heartbeat(A_recovered_capture_0000)
            )
            try:
                _var_var_116 = await self.db_manager.get_pending_model_candidate_id()
                if _var_var_116:
                    _var_var_117 = (
                        f"candidate shadow evaluation pending for {_var_var_116}"
                    )
                    _LOGGER.info("AI model training skipped: %s", _var_var_117)
                    return TrainingResult(success=False, error_message=_var_var_117)
                return await self._train_model_candidate(
                    run_feature_importance,
                    lstm_hyperparameters=lstm_hyperparameters,
                    through_date=through_date,
                    lease_owner=A_recovered_capture_0000,
                )
            except asyncio.CancelledError:
                if abort_owner__recovered_capture_0008.is_set():
                    return TrainingResult(
                        success=False, error_message="model training lease lost"
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(_var_var_113, _var_var_115)
                await self.db_manager.release_model_training_lease(
                    A_recovered_capture_0000
                )
        return None if True else None

    async def train_model_for_reaction(
        self, request_id: str, supersede_candidate_id: str, through_date: str
    ) -> TrainingResult:
        """Train one replacement candidate for a claimed drift reaction."""
        async with self._candidate_training_lock:
            A_recovered_capture_0000 = str(uuid.uuid4())
            _var_var_118 = await self.db_manager.acquire_model_training_lease(
                A_recovered_capture_0000, request_id
            )
            if not _var_var_118:
                return TrainingResult(
                    success=False,
                    error_message="another model training run is already active",
                )
            _var_var_113, abort_owner__recovered_capture_0008, _var_var_115 = (
                self._start_training_lease_heartbeat(A_recovered_capture_0000)
            )
            try:
                _var_var_116 = await self.db_manager.get_pending_model_candidate_id()
                if _var_var_116 != supersede_candidate_id:
                    return TrainingResult(
                        success=False,
                        error_message="model reaction candidate changed before training",
                    )
                return await self._train_model_candidate(
                    False,
                    reaction_context={
                        "request_id": request_id,
                        "supersede_candidate_id": supersede_candidate_id,
                        "lease_owner": A_recovered_capture_0000,
                    },
                    through_date=through_date,
                    lease_owner=A_recovered_capture_0000,
                )
            except asyncio.CancelledError:
                if abort_owner__recovered_capture_0008.is_set():
                    return TrainingResult(
                        success=False, error_message="model training lease lost"
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(_var_var_113, _var_var_115)
                await self.db_manager.release_model_training_lease(
                    A_recovered_capture_0000
                )
        return None if True else None

    async def run_grid_search_candidate(
        self,
        optimizer: Any,
        retrain_after: bool = True,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[Optional[Any], Optional[TrainingResult]]:
        """Select hyperparameters and optionally train one governed candidate."""
        async with self._candidate_training_lock:
            _var_var_112 = str(uuid.uuid4())
            if not await self.db_manager.acquire_model_training_lease(_var_var_112):
                return (
                    None,
                    TrainingResult(
                        success=False,
                        error_message="another model training run is already active",
                    ),
                )
            _var_var_113, abort_owner__recovered_capture_0008, _var_var_115 = (
                self._start_training_lease_heartbeat(_var_var_112)
            )
            _var_var_119 = None
            try:
                _var_var_116 = await self.db_manager.get_pending_model_candidate_id()
                if _var_var_116:
                    return (
                        None,
                        TrainingResult(
                            success=False,
                            error_message=f"candidate shadow evaluation pending for {_var_var_116}",
                        ),
                    )
                _var_var_120 = await self._prepare_training_data()
                _var_var_121, _var_var_122, _var_var_123 = _var_var_120
                if len(_var_var_121) < MIN_TRAINING_SAMPLES:
                    return (
                        None,
                        TrainingResult(
                            success=False,
                            samples_used=len(_var_var_121),
                            error_message=f"Need {MIN_TRAINING_SAMPLES} samples for Grid-Search, got {len(_var_var_121)}",
                        ),
                    )
                _var_var_124, _var_var_125 = self._training_temporal_metadata(
                    _var_var_123, len(_var_var_121)
                )
                _var_var_62 = calculate_feature_count(self.num_groups)
                _var_var_61 = self.num_groups if self.num_groups > 0 else 1

                async def lease_progress_callback(*args: Any) -> None:
                    if not await self.db_manager.renew_model_training_lease(
                        _var_var_112
                    ):
                        raise RuntimeError("model training lease expired")
                    if progress_callback is not None:
                        await progress_callback(*args)
                    return None if True else None

                _var_var_75 = await optimizer.run_grid_search(
                    lstm_class=TinyLSTM,
                    X_sequences=_var_var_121,
                    y_targets=_var_var_122,
                    input_size=_var_var_62,
                    sequence_length=TRAINING_SEQUENCE_LENGTH,
                    num_outputs=_var_var_61,
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    sample_dates=_var_var_124,
                    source_intervals=_var_var_125,
                    seed=TRAINING_RANDOM_SEED,
                    progress_callback=lease_progress_callback,
                )
                _var_var_119 = _var_var_75
                if not _var_var_75.success or not retrain_after:
                    return (_var_var_75, None)
                if not await self.db_manager.renew_model_training_lease(_var_var_112):
                    return (
                        _var_var_75,
                        TrainingResult(
                            success=False, error_message="model training lease expired"
                        ),
                    )
                _var_var_127 = await self._train_model_candidate(
                    False,
                    lstm_hyperparameters=_var_var_75.best_params,
                    training_data=_var_var_120,
                    hyperparameter_selection={
                        "contract_version": "grid_candidate_v1",
                        "parameter_grid_hash": canonical_stable_hash(
                            optimizer.param_grid
                        ),
                        "selected_params": _var_var_75.best_params,
                        "split_policy": "chronological_target_date_with_interval_purge",
                    },
                    lease_owner=_var_var_112,
                )
                return (_var_var_75, _var_var_127)
            except asyncio.CancelledError:
                if abort_owner__recovered_capture_0008.is_set():
                    return (
                        _var_var_119,
                        TrainingResult(
                            success=False, error_message="model training lease lost"
                        ),
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(_var_var_113, _var_var_115)
                await self.db_manager.release_model_training_lease(_var_var_112)
        return None if True else None

    def _start_training_lease_heartbeat(
        self, lease_owner: str, interval_seconds: float = 60.0
    ) -> Tuple[asyncio.Event, asyncio.Event, asyncio.Task]:
        """Renew the persistent lease throughout long in-process training."""
        self__recovered_capture_0018 = asyncio.Event()
        abort_owner__recovered_capture_0008 = asyncio.Event()
        abort_owner__recovered_capture_0009 = asyncio.current_task()
        if abort_owner__recovered_capture_0009 is None:
            raise RuntimeError("model training heartbeat requires an owning task")

        def abort_owner() -> None:
            if self__recovered_capture_0018.is_set():
                return
            abort_owner__recovered_capture_0008.set()
            abort_owner__recovered_capture_0009.cancel()

        async def heartbeat() -> None:
            while not self__recovered_capture_0018.is_set():
                try:
                    await asyncio.wait_for(
                        self__recovered_capture_0018.wait(), timeout=interval_seconds
                    )
                except asyncio.TimeoutError:
                    try:
                        _var_var_132 = await self.db_manager.renew_model_training_lease(
                            lease_owner
                        )
                    except Exception as _var_var_68:
                        _LOGGER.error(
                            "Model training lease heartbeat failed: %s", _var_var_68
                        )
                        abort_owner()
                        return
                    if not _var_var_132:
                        _LOGGER.error("Model training lease heartbeat was lost")
                        abort_owner()
                        return

        return (
            self__recovered_capture_0018,
            abort_owner__recovered_capture_0008,
            asyncio.create_task(heartbeat()),
        )

    async def _train_model_candidate(
        self,
        run_feature_importance: bool = False,
        reaction_context: Optional[Dict[str, str]] = None,
        lstm_hyperparameters: Optional[Dict[str, Any]] = None,
        training_data: Optional[Tuple[Any, Any, Any, Any, Any]] = None,
        hyperparameter_selection: Optional[Dict[str, Any]] = None,
        through_date: Optional[str] = None,
        lease_owner: Optional[str] = None,
    ) -> TrainingResult:
        """Train an isolated candidate without changing the serving models."""
        if not self.feature_engineer:
            return TrainingResult(
                success=False, error_message="FeatureEngineer not initialized"
            )
        _LOGGER.info(f"AI training started: {self.num_groups} outputs")
        try:
            if training_data is None:
                training_data = await self._prepare_training_data(through_date)
            _var_var_121, _var_var_122, _var_var_123 = training_data
            _var_var_133 = len(_var_var_121)
            _var_var_124, _var_var_125 = self._training_temporal_metadata(
                _var_var_123, _var_var_133
            )
            self._log_panel_group_training_diagnostics()
            _var_var_134 = get_season(SafeDateTimeUtil.now().month)
            _var_var_135 = await self._load_seasonal_archive(_var_var_134)
            if _var_var_133 < MIN_RIDGE_SAMPLES:
                return TrainingResult(
                    success=False,
                    samples_used=_var_var_133,
                    error_message=f"Need {MIN_RIDGE_SAMPLES} samples for Ridge, got {_var_var_133}",
                )
            if not TinyRidge.has_viable_temporal_split(
                _var_var_133,
                TRAINING_VALIDATION_SPLIT,
                sample_dates=_var_var_124,
                source_intervals=_var_var_125,
            ):
                _LOGGER.info(
                    "AI model training deferred: independent training and holdout dates are not available yet"
                )
                return TrainingResult(
                    success=False,
                    samples_used=_var_var_133,
                    reason_code="insufficient_independent_training_days",
                )
            _var_var_62 = calculate_feature_count(self.num_groups)
            _var_var_61 = self.num_groups if self.num_groups > 0 else 1
            _var_var_136 = TinyRidge(
                input_size=_var_var_62,
                hidden_size=48,
                sequence_length=TRAINING_SEQUENCE_LENGTH,
                num_outputs=_var_var_61,
            )
            _var_var_137 = self._validated_lstm_hyperparameters(lstm_hyperparameters)
            _var_var_138 = TinyLSTM(
                input_size=_var_var_62,
                hidden_sizes=_var_var_137["hidden_sizes"],
                sequence_length=TRAINING_SEQUENCE_LENGTH,
                num_outputs=_var_var_61,
                learning_rate=_var_var_137["learning_rate"],
                num_heads=_var_var_137["num_heads"],
                seed=TRAINING_RANDOM_SEED,
            )
            _var_var_139 = None
            _var_var_140 = None
            _var_var_141 = False
            if _var_var_133 >= MIN_RIDGE_SAMPLES:
                _LOGGER.info(f"Training Ridge with {_var_var_133} samples...")
                _var_var_139 = await _var_var_136.train(
                    X_sequences=_var_var_121,
                    y_targets=_var_var_122,
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    sample_dates=_var_var_124,
                    source_intervals=_var_var_125,
                )
                if _var_var_139.get("success"):
                    _LOGGER.info(
                        f"Ridge trained: R²={_var_var_139.get('accuracy', 0):.3f}, alpha={_var_var_139.get('alpha', 0):.4f}"
                    )
                    _var_var_141 = True
            if _var_var_133 >= MIN_TRAINING_SAMPLES:
                if _var_var_135 and "lstm" in _var_var_135:
                    try:
                        _var_var_142 = _var_var_135["lstm"]
                        _var_var_143 = (
                            _var_var_142.get("input_size", 0),
                            _var_var_142.get("hidden_size", 0),
                            _var_var_142.get("hidden2", 0),
                            _var_var_142.get("num_outputs", 0),
                            _var_var_142.get("num_heads", 0),
                        )
                        _var_var_144 = (
                            _var_var_138.input_size,
                            _var_var_138.hidden1,
                            _var_var_138.hidden2,
                            _var_var_138.num_outputs,
                            _var_var_138.num_heads,
                        )
                        if _var_var_143 == _var_var_144:
                            _var_var_138.set_weights(_var_var_135["lstm"])
                            _LOGGER.info(
                                "Seasonal warm-start: %s, archive %d (%d samples, R²=%.3f)",
                                _var_var_134,
                                _var_var_135["lstm"]["source_year"],
                                _var_var_135["lstm"]["training_samples"],
                                _var_var_135["lstm"]["accuracy"],
                            )
                        else:
                            _LOGGER.info(
                                "Archive architecture mismatch: saved=%s vs needed=%s; training from scratch",
                                _var_var_143,
                                _var_var_144,
                            )
                    except Exception as _var_var_65:
                        _LOGGER.debug("Archive incompatible: %s", _var_var_65)
                _LOGGER.info(f"Training LSTM with {_var_var_133} samples...")
                _var_var_140 = await _var_var_138.train(
                    X_sequences=_var_var_121,
                    y_targets=_var_var_122,
                    epochs=_var_var_137["epochs"],
                    batch_size=_var_var_137["batch_size"],
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    early_stopping_patience=_var_var_137["early_stopping_patience"],
                    sample_dates=_var_var_124,
                    source_intervals=_var_var_125,
                    seed=TRAINING_RANDOM_SEED,
                )
                if _var_var_140.get("success"):
                    _LOGGER.info(
                        f"LSTM trained: R²={_var_var_140.get('accuracy', 0):.3f}, epochs={_var_var_140.get('epochs_trained', 0)}, layers=2, heads={_var_var_137['num_heads']}"
                    )
                    _var_var_141 = True
            if _var_var_141:
                _var_var_145 = self._select_candidate_model(
                    _var_var_133, _var_var_139, _var_var_140
                )
                if (
                    _var_var_145 == "tiny_lstm"
                    and _var_var_140
                    and _var_var_140.get("success")
                ):
                    _var_var_146 = _var_var_140
                elif _var_var_139 and _var_var_139.get("success"):
                    _var_var_146 = _var_var_139
                else:
                    _var_var_146 = None
                _var_var_147 = (
                    _var_var_146.get("accuracy", 0.0) if _var_var_146 else 0.0
                )
                _var_var_148 = _var_var_146.get("rmse", 0.0) if _var_var_146 else 0.0
                _var_var_74 = await self._save_candidate_async(
                    _var_var_123,
                    _var_var_136,
                    _var_var_138,
                    _var_var_145,
                    _var_var_133,
                    _var_var_147,
                    _var_var_148,
                    bool(_var_var_140 and _var_var_140.get("success")),
                    reaction_context,
                    _var_var_137,
                    hyperparameter_selection,
                    through_date,
                    lease_owner,
                )
                self._latest_model_candidate_id = _var_var_74
                _var_var_149 = f", RMSE={_var_var_148:.3f}kWh" if _var_var_148 else ""
                _LOGGER.info(
                    f"Training candidate stored: id={_var_var_74}, R²={_var_var_147:.3f}{_var_var_149}, samples={_var_var_133}, outputs={_var_var_61}"
                )
                if run_feature_importance and _var_var_133 >= MIN_TRAINING_SAMPLES:
                    _LOGGER.info(
                        "Candidate feature importance deferred until isolated candidate evaluation is available"
                    )
                return TrainingResult(
                    success=True,
                    accuracy=_var_var_147,
                    samples_used=_var_var_133,
                    feature_count=_var_var_62,
                    num_outputs=_var_var_61,
                    epochs_trained=(
                        _var_var_140.get("epochs_trained", 0) if _var_var_140 else 1
                    ),
                    has_attention=self.use_attention,
                    rmse=_var_var_148,
                )
            return TrainingResult(
                success=False, error_message="Both Ridge and LSTM training failed"
            )
        except Exception as _var_var_65:
            _LOGGER.error(f"Training failed: {_var_var_65}")
            return TrainingResult(success=False, error_message=str(_var_var_65))

    async def _load_dual_model_weights(
        self,
        weights: Dict[str, Any],
        feature_count: int,
        num_outputs: int,
        strict_active_model: bool = False,
        quiet_mismatch: bool = False,
    ) -> None:
        """Load dual-model weights (v3.0 format) @zara

        quiet_mismatch: architecture mismatches are expected while probing
        shadow candidates; the caller reports the outcome, so log them at
        DEBUG instead of WARNING.
        """
        _var_var_292 = _LOGGER.debug if quiet_mismatch else _LOGGER.warning
        self.training_samples = weights.get("training_samples", 0)
        self.current_accuracy = weights.get("accuracy")
        self.current_rmse = weights.get("rmse")
        if weights.get("last_trained"):
            try:
                self.last_training_time = datetime.fromisoformat(
                    weights["last_trained"]
                )
            except (ValueError, TypeError):
                pass
        _var_var_293 = False
        _var_var_294 = False
        _var_var_295 = weights.get("active_model", "none")
        self._apply_artifact_output_scale(weights)
        _var_var_296 = weights.get("ridge")
        if _var_var_296 and self.ridge:
            try:
                _var_var_297 = _var_var_296.get("input_size", 17)
                _var_var_298 = _var_var_296.get("num_outputs", 1)
                _var_var_299 = _var_var_296.get("flat_size", _var_var_297 * 24)
                _var_var_300 = self.ridge.flat_size
                if (
                    _var_var_297 == feature_count
                    and _var_var_298 == num_outputs
                    and (_var_var_299 == _var_var_300)
                ):
                    self.ridge.set_weights(
                        _var_var_296,
                        strict=strict_active_model and _var_var_295 == "ridge",
                    )
                    _var_var_293 = True
                    _LOGGER.debug(
                        f"Ridge weights loaded: {_var_var_296.get('trained_samples', 0)} samples"
                    )
                else:
                    _var_var_292(
                        f"Ridge architecture mismatch: saved input={_var_var_297}/flat_size={_var_var_299}/outputs={_var_var_298}, needed input={feature_count}/flat_size={_var_var_300}/outputs={num_outputs}"
                    )
            except Exception as _var_var_65:
                _LOGGER.warning(f"Could not load Ridge weights: {_var_var_65}")
        _var_var_301 = weights.get("lstm")
        if _var_var_301 and self.lstm:
            try:
                _var_var_297 = _var_var_301.get("input_size", 17)
                _var_var_298 = _var_var_301.get("num_outputs", 1)
                _var_var_302 = _var_var_301.get("hidden_size", 48)
                _var_var_303 = _var_var_301.get("hidden2", 24)
                _var_var_304 = _var_var_301.get("num_heads", 4)
                if (
                    _var_var_297 == feature_count
                    and _var_var_298 == num_outputs
                    and (_var_var_302 == self.lstm.hidden1)
                    and (_var_var_303 == self.lstm.hidden2)
                    and (_var_var_304 == self.lstm.num_heads)
                ):
                    self.lstm.set_weights(_var_var_301)
                    _var_var_294 = True
                    _LOGGER.debug(
                        f"LSTM weights loaded: {_var_var_301.get('training_samples', 0)} samples"
                    )
                else:
                    _var_var_292(
                        f"LSTM architecture mismatch: saved input={_var_var_297}/hidden=({_var_var_302},{_var_var_303})/heads={_var_var_304}/outputs={_var_var_298}, needed input={feature_count}/hidden=({self.lstm.hidden1},{self.lstm.hidden2})/heads={self.lstm.num_heads}/outputs={num_outputs}. Retraining required."
                    )
            except Exception as _var_var_65:
                _LOGGER.warning(f"Could not load LSTM weights: {_var_var_65}")
        if strict_active_model and _var_var_295 not in {"ridge", "tiny_lstm"}:
            raise ValueError("candidate declares an invalid active model")
        if strict_active_model:
            if (
                _var_var_295 == "tiny_lstm"
                and (not _var_var_294)
                or (_var_var_295 == "ridge" and (not _var_var_293))
            ):
                raise ValueError("candidate active model artifact could not be loaded")
        if _var_var_295 == "tiny_lstm" and _var_var_294:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif _var_var_295 == "ridge" and _var_var_293:
            self.active_model = "ridge"
            self.state = ModelState.READY
        elif not strict_active_model and _var_var_294:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif not strict_active_model and _var_var_293:
            self.active_model = "ridge"
            self.state = ModelState.READY
        else:
            self.active_model = "none"
            self.state = ModelState.UNTRAINED
            if not _var_var_296 and (not _var_var_301):
                _LOGGER.info(
                    f"No promoted model artifacts available - serving physics until a candidate passes the promotion gate ({feature_count} features, {num_outputs} outputs)"
                )
            else:
                _LOGGER.warning(
                    f"No compatible weights found. Config has {feature_count} features, {num_outputs} outputs. Model needs retraining."
                )
        _var_var_305 = f"active={self.active_model}"
        _var_var_149 = f", RMSE={self.current_rmse:.3f}kWh" if self.current_rmse else ""
        if self.state == ModelState.READY:
            _var_var_306 = (
                "Serving model weights loaded"
                if not self.shadow_candidate_id
                else f"{self._instance_label()} weights loaded"
            )
            self._instance_log(
                f"{_var_var_306}: {self.training_samples} samples, {_var_var_305}, R²={self.current_accuracy:.3f}{_var_var_149}"
                if self.current_accuracy
                else f"{_var_var_306}: {self.training_samples} samples, {_var_var_305}"
            )
            self._instance_log(
                "%s output scale: groups=%d global=%s",
                "Serving" if not self.shadow_candidate_id else self._instance_label(),
                len(self._artifact_group_scales),
                self._artifact_global_scale,
            )
        return None if True else None
