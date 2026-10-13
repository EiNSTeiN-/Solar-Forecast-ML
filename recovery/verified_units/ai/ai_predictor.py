# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Main AI Predictor for Solar Forecast ML V16.2.0.

Multi-Output AI predictor with per-group predictions using database storage.
Combines TinyLSTM and TinyRidge models with DB-based weight persistence.

@zara
"""
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

_LOGGER = logging.getLogger(__name__)
MIN_RIDGE_SAMPLES = 10
MIN_TRAINING_SAMPLES = 50
TRAINING_DAYS = 60
TRAINING_VALIDATION_SPLIT = 0.2
TRAINING_RANDOM_SEED = 1729
TRAINING_SEQUENCE_LENGTH = 24
DEFAULT_LSTM_HYPERPARAMETERS = {
    "hidden_sizes": (48, 24),
    "batch_size": 16,
    "learning_rate": 0.005,
    "num_heads": 4,
    "epochs": 200,
    "early_stopping_patience": 20,
}
BASE_FEATURE_COUNT = 27
GROUP_FEATURE_COUNT = 3
DEFAULT_MAX_KWH_PER_HOUR = 2.0
MAX_RUNTIME_CANDIDATE_RECONCILIATION = 8


class CandidateRuntimeIncompatibleError(ValueError):
    """A candidate cannot run in the current serving architecture."""


def _artifact_architecture_summary(weights: Any) -> str:
    """Describe the saved model dimensions of one weight artifact for logs."""
    if not isinstance(weights, dict):
        return "artifact=unreadable"
    architecture_parts = [f"active={weights.get('active_model', 'none')}"]
    for model_type in ("ridge", "lstm"):
        model_weights = weights.get(model_type)
        if isinstance(model_weights, dict):
            architecture_parts.append(
                f"{model_type} input={model_weights.get('input_size', '?')}/outputs={model_weights.get('num_outputs', '?')}"
            )
    return ", ".join(architecture_parts)


def _hourly_governance_flags(
    *,
    is_outlier: Any = False,
    inverter_clipped: Any = False,
    exclude_from_learning: Any = False,
    exclude_from_clean_evaluation: Any = None,
    mppt_throttled: Any = False,
    has_weather_alert: Any = False,
    weather_alert_type: Any = None,
    mppt_throttle_reason: Any = None,
) -> Dict[str, Any]:
    """Copy hourly governance flags into the in-memory training record."""
    return {
        "is_outlier": bool(is_outlier),
        "inverter_clipped": bool(inverter_clipped),
        "exclude_from_learning": bool(exclude_from_learning),
        "exclude_from_clean_evaluation": (
            bool(exclude_from_clean_evaluation)
            if exclude_from_clean_evaluation is not None
            else None
        ),
        "mppt_throttled": bool(mppt_throttled),
        "has_weather_alert": bool(has_weather_alert),
        "weather_alert_type": weather_alert_type,
        "mppt_throttle_reason": mppt_throttle_reason,
    }


def _is_clean_context_prediction(prediction: Optional[Dict[str, Any]]) -> bool:
    if not prediction:
        return False
    governance_flags = prediction.get("flags") or {}
    if governance_flags.get("exclude_from_clean_evaluation") is not None:
        return not bool(governance_flags.get("exclude_from_clean_evaluation"))
    exclusion_flags = (
        "exclude_from_learning",
        "mppt_throttled",
        "inverter_clipped",
        "has_weather_alert",
        "is_outlier",
    )
    return not any(
        (bool(governance_flags.get(flag_name)) for flag_name in exclusion_flags)
    )


def _clean_actual_kwh(prediction: Optional[Dict[str, Any]]) -> float:
    if not _is_clean_context_prediction(prediction):
        return 0.0
    actual_kwh = prediction.get("actual_kwh")
    return float(actual_kwh or 0.0) if actual_kwh is not None else 0.0


def _sum_clean_actuals_for_date(
    predictions_by_id: Dict[str, Dict[str, Any]], date_str: str
) -> float:
    return sum(
        (
            _clean_actual_kwh(predictions_by_id.get(f"{date_str}_{hour:02d}"))
            for hour in range(24)
        )
    )


def calculate_feature_count(num_groups: int) -> int:
    """Calculate total feature count based on number of groups @zara"""
    if num_groups <= 0:
        return BASE_FEATURE_COUNT
    return BASE_FEATURE_COUNT + GROUP_FEATURE_COUNT * num_groups


def _get_group_azimuth(group) -> float:
    return float(group.get("azimuth", group.get("azimuth_deg", 180.0)) or 180.0)


def _get_group_tilt(group) -> float:
    return float(group.get("tilt", group.get("tilt_deg", 30.0)) or 30.0)


def _get_group_capacity(group) -> float:
    if "capacity_kwp" in group:
        return float(group.get("capacity_kwp", 0.0) or 0.0)
    if "kwp" in group:
        return float(group.get("kwp", 0.0) or 0.0)
    if "power_wp" in group:
        return float(group.get("power_wp", 0.0) or 0.0) / 1000.0
    if "power" in group:
        return float(group.get("power", 0.0) or 0.0) / 1000.0
    return 0.0


def _max_group_training_kwh(group, fallback: float) -> float:
    group_capacity_kwp = _get_group_capacity(group or {})
    if group_capacity_kwp > 0:
        return max(group_capacity_kwp * 1.3, 0.5)
    return max(float(fallback or DEFAULT_MAX_KWH_PER_HOUR), 0.5)


def _group_output_scale(group, fallback: float) -> float:
    return _max_group_training_kwh(group, fallback)


def _finite_positive_scale(value: Any) -> Optional[float]:
    try:
        positive_scale = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(positive_scale) or positive_scale <= 0:
        return None
    return positive_scale


def _group_orientation_match(group, other, tol: float = 0.5) -> bool:
    group_azimuth = _get_group_azimuth(group)
    other_azimuth = _get_group_azimuth(other)
    group_tilt = _get_group_tilt(group)
    other_tilt = _get_group_tilt(other)
    azimuth_difference = abs((group_azimuth - other_azimuth + 180.0) % 360.0 - 180.0)
    tilt_difference = abs(group_tilt - other_tilt)
    azimuth_tolerance = max(
        tol, max(abs(group_azimuth), abs(other_azimuth), 1.0) * 0.05
    )
    tilt_tolerance = max(tol, max(abs(group_tilt), abs(other_tilt), 1.0) * 0.05)
    return azimuth_difference <= azimuth_tolerance and tilt_difference <= tilt_tolerance


def _group_power_ratio(group, other) -> float | None:
    group_capacity = _get_group_capacity(group)
    other_capacity = _get_group_capacity(other)
    if group_capacity <= 0 or other_capacity <= 0:
        return None
    larger_capacity = max(group_capacity, other_capacity)
    smaller_capacity = min(group_capacity, other_capacity)
    return smaller_capacity / larger_capacity if larger_capacity > 0 else None


def _group_match_score(group, other) -> tuple[float, float, float, int]:
    azimuth_difference = abs(
        (_get_group_azimuth(group) - _get_group_azimuth(other) + 180.0) % 360.0 - 180.0
    )
    tilt_difference = abs(_get_group_tilt(group) - _get_group_tilt(other))
    capacity_ratio = _group_power_ratio(group, other)
    capacity_penalty = 1.0 - capacity_ratio if capacity_ratio is not None else 0.5
    return (
        azimuth_difference + tilt_difference,
        capacity_penalty,
        abs(_get_group_capacity(group) - _get_group_capacity(other)),
        0,
    )


def _find_training_donor(group, all_groups, actuals):
    best_donor, best_score = (None, None)
    for candidate_group in all_groups:
        if candidate_group.get("name", "") not in actuals:
            continue
        if not candidate_group.get("name", "") == group.get(
            "name", ""
        ) and _group_orientation_match(group, candidate_group):
            match_score = _group_match_score(group, candidate_group)
            if best_score is None or match_score < best_score:
                best_donor, best_score = (candidate_group, match_score)
    return best_donor


class ModelState(Enum):
    """State of AI model @zara"""

    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    TRAINING = "training"
    DEGRADED = "degraded"
    ERROR = "error"
    UNTRAINED = "untrained"


@dataclass
class TrainingResult:
    """Result of AI training @zara"""

    if True:
        success: bool
        accuracy: float = 0.0
        samples_used: int = 0
        feature_count: int = BASE_FEATURE_COUNT
        num_outputs: int = 1
        error_message: Optional[str] = None
        epochs_trained: int = 0
        has_attention: bool = False
        rmse: float = 0.0
        reason_code: Optional[str] = None


@dataclass
class GroupPrediction:
    """Prediction result for a single panel group @zara"""

    if True:
        group_name: str
        power_kwh: float
        azimuth: float
        tilt: float
        capacity_kwp: float


class AIPredictor:
    """Multi-Output AI predictor with per-group predictions @zara"""

    def __init__(
        self,
        hass: Any,
        db_manager: Any,
        error_handler: Optional[Any] = None,
        notification_service: Optional[Any] = None,
        config_entry: Optional[Any] = None,
        panel_groups: Optional[List[Dict[str, Any]]] = None,
        solar_capacity: float = 5.0,
        shadow_candidate_id: Optional[str] = None,
    ):
        """Initialize AI predictor @zara

        shadow_candidate_id marks an isolated probe instance for that
        candidate: it never serves, so its lifecycle lines stay at DEBUG and
        carry the candidate id instead of reading like the serving model.
        """
        self.shadow_candidate_id = shadow_candidate_id
        self._instance_log = _LOGGER.debug if shadow_candidate_id else _LOGGER.info
        self.hass = hass
        self.db_manager = db_manager
        self.error_handler = error_handler
        self.notification_service = notification_service
        self.config_entry = config_entry
        self.panel_groups = panel_groups or []
        self.num_groups = len(self.panel_groups)
        self.total_capacity = self._calculate_total_capacity(
            self.panel_groups, solar_capacity
        )
        self.lstm = None
        self.ridge = None
        self.active_model = "none"
        self.feature_engineer = None
        self.state = ModelState.INITIALIZING
        self.current_accuracy = None
        self.current_rmse = None
        self.last_training_time = None
        self.last_successful_training_time = None
        self.last_training_samples = 0
        self.training_samples = 0
        self.use_attention = True
        self._last_feature_importance = None
        self._historical_cache = {}
        self._last_reconcile_state = None
        self._last_panel_group_training_diagnostics = None
        self._candidate_training_lock = asyncio.Lock()
        self._latest_model_candidate_id = None
        self._frozen_prediction_history = None
        self._forecast_reference_date = None
        self.solar_capacity = self.total_capacity
        self.power_entity = None
        self.weather_entity = None
        self._artifact_group_scales = {}
        self._artifact_global_scale = DEFAULT_MAX_KWH_PER_HOUR
        self._max_kwh_per_hour = max(
            self.total_capacity * 1.2 / max(self.num_groups, 1), 0.5
        )
        self._instance_log(
            f"{self._instance_label()} initialized: {self.num_groups} groups, {self.total_capacity:.2f} kWp total, max_kwh_per_hour={self._max_kwh_per_hour:.2f}"
        )

    def _instance_label(self) -> str:
        """'AIPredictor' for the serving model, 'Shadow candidate <id>' for a probe."""
        if self.shadow_candidate_id:
            return f"Shadow candidate {str(self.shadow_candidate_id)[:8]}"
        return "AIPredictor"

    def _calculate_total_capacity(
        self, panel_groups: List[Dict[str, Any]], fallback_capacity: float
    ) -> float:
        """Calculate total capacity from panel groups @zara"""
        if not panel_groups:
            return fallback_capacity
        total_capacity_kwp = 0.0
        for panel_group in panel_groups:
            if "capacity_kwp" in panel_group:
                total_capacity_kwp += float(panel_group["capacity_kwp"])
            elif "kwp" in panel_group:
                total_capacity_kwp += float(panel_group["kwp"])
            elif "power_wp" in panel_group:
                total_capacity_kwp += float(panel_group["power_wp"]) / 1000.0
            else:
                total_capacity_kwp += 1.0
        return total_capacity_kwp if total_capacity_kwp > 0 else fallback_capacity

    def _build_output_scale_payload(self) -> Dict[str, Any]:
        group_scales = {}
        for panel_group in self.panel_groups:
            group_name = panel_group.get("name")
            if group_name:
                group_scales[str(group_name)] = round(
                    _group_output_scale(panel_group, self._max_kwh_per_hour), 6
                )
        return {
            "groups": group_scales,
            "global": round(float(self._max_kwh_per_hour), 6),
        }

    def _apply_artifact_output_scale(self, weights: Dict[str, Any]) -> None:
        scale_payload = (
            weights.get("output_scale") if isinstance(weights, dict) else None
        )
        group_scales = {}
        global_scale = None
        if isinstance(scale_payload, dict):
            saved_group_scales = scale_payload.get("groups")
            if isinstance(saved_group_scales, dict):
                for group_name, raw_scale in saved_group_scales.items():
                    positive_scale = _finite_positive_scale(raw_scale)
                    if positive_scale is not None:
                        group_scales[str(group_name)] = positive_scale
            global_scale = _finite_positive_scale(scale_payload.get("global"))
        if group_scales:
            self._artifact_group_scales = group_scales
            self._artifact_global_scale = global_scale
            return
        if global_scale is not None:
            self._artifact_group_scales = {}
            self._artifact_global_scale = global_scale
            return
        self._artifact_group_scales = {}
        self._artifact_global_scale = DEFAULT_MAX_KWH_PER_HOUR

    def _inference_output_scale(self, group: Optional[Dict[str, Any]]) -> float:
        group_name = (group or {}).get("name")
        if group_name and group_name in self._artifact_group_scales:
            return self._artifact_group_scales[group_name]
        if self._artifact_global_scale is not None:
            return float(self._artifact_global_scale)
        if group:
            return _group_output_scale(group, self._max_kwh_per_hour)
        return DEFAULT_MAX_KWH_PER_HOUR

    def set_entities(
        self,
        solar_capacity: float = 5.0,
        power_entity: Optional[str] = None,
        weather_entity: Optional[str] = None,
        **kwargs,
    ):
        """Set entity configuration @zara"""
        self.solar_capacity = solar_capacity
        self.power_entity = power_entity
        self.weather_entity = weather_entity

    def _reset_panel_group_training_diagnostics(self) -> None:
        if not self.panel_groups:
            self._last_panel_group_training_diagnostics = None
            return
        self._last_panel_group_training_diagnostics = {
            "samples_total": 0,
            "samples_with_group_input": 0,
            "samples_with_real_group_targets": 0,
            "samples_with_residual_group_targets": 0,
            "samples_with_mixed_fallback": 0,
            "samples_with_capacity_fallback_only": 0,
            "samples_with_legacy_topology_fallback": 0,
            "samples_with_legacy_partial_fallback": 0,
            "group_rejections_missing_topology": 0,
            "group_rejections_invalid_value": 0,
            "group_rejections_incompatible_lineage": 0,
            "group_rejections_legacy_unknown_topology": 0,
            "group_rejections_out_of_range": 0,
            "hour_rejections_sum_mismatch": 0,
            "hour_rejections_partial_sum_exceeds_total": 0,
            "training_dates": set(),
            "affected_dates": set(),
            "fallback_dates": set(),
            "legacy_fallback_dates": set(),
            "current_fallback_dates": set(),
        }

    def _record_panel_group_training_diagnostic(
        self,
        key: str,
        *,
        count: int = 1,
        date_str: Optional[str] = None,
        fallback: bool = False,
        legacy: bool = False,
    ) -> None:
        diagnostics = self._last_panel_group_training_diagnostics
        if not diagnostics:
            return
        diagnostics[key] = int(diagnostics.get(key, 0)) + count
        if not date_str:
            return
        if key == "samples_total":
            diagnostics["training_dates"].add(date_str)
            return
        sample_counter_keys = {
            "samples_with_group_input",
            "samples_with_real_group_targets",
        }
        if key not in sample_counter_keys:
            diagnostics["affected_dates"].add(date_str)
        if fallback:
            diagnostics["fallback_dates"].add(date_str)
            if legacy:
                diagnostics["legacy_fallback_dates"].add(date_str)
            else:
                diagnostics["current_fallback_dates"].add(date_str)
        return None if True else None

    @staticmethod
    def _format_date_range(dates: Any) -> str:
        sorted_dates = sorted(dates or [])
        if not sorted_dates:
            return "none"
        first_date = sorted_dates[0]
        last_date = sorted_dates[-1]
        return first_date if first_date == last_date else f"{first_date}..{last_date}"

    def _log_panel_group_training_diagnostics(self) -> None:
        diagnostics = self._last_panel_group_training_diagnostics
        if not diagnostics:
            return
        total_samples = int(diagnostics.get("samples_total", 0))
        if total_samples <= 0:
            return
        real_target_samples = int(diagnostics.get("samples_with_real_group_targets", 0))
        residual_target_samples = int(
            diagnostics.get("samples_with_residual_group_targets", 0)
        )
        measured_target_samples = real_target_samples + residual_target_samples
        mixed_fallback_samples = int(diagnostics.get("samples_with_mixed_fallback", 0))
        capacity_fallback_samples = int(
            diagnostics.get("samples_with_capacity_fallback_only", 0)
        )
        current_fallback_samples = mixed_fallback_samples + capacity_fallback_samples
        current_fallback_percent = (
            current_fallback_samples / total_samples * 100.0 if total_samples else 0.0
        )
        legacy_topology_samples = int(
            diagnostics.get("samples_with_legacy_topology_fallback", 0)
        )
        legacy_partial_samples = int(
            diagnostics.get("samples_with_legacy_partial_fallback", 0)
        )
        legacy_fallback_samples = legacy_topology_samples + legacy_partial_samples
        legacy_fallback_percent = (
            legacy_fallback_samples / total_samples * 100.0 if total_samples else 0.0
        )
        measured_target_percent = (
            measured_target_samples / total_samples * 100.0 if total_samples else 0.0
        )
        if current_fallback_percent >= 20.0:
            severity = "CRITICAL"
        elif current_fallback_percent >= 5.0:
            severity = "WARN"
        elif legacy_fallback_samples > 0:
            severity = "OK_LEGACY_IGNORED"
        else:
            severity = "OK"
        training_date_range = self._format_date_range(
            diagnostics.get("training_dates", set())
        )
        affected_date_range = self._format_date_range(
            diagnostics.get("affected_dates", set())
        )
        current_fallback_date_range = self._format_date_range(
            diagnostics.get("current_fallback_dates", set())
        )
        legacy_fallback_date_range = self._format_date_range(
            diagnostics.get("legacy_fallback_dates", set())
        )
        log_diagnostics = (
            _LOGGER.warning if severity in {"WARN", "CRITICAL"} else _LOGGER.info
        )
        log_diagnostics(
            "Panel-group training diagnostics: health=%s, samples=%d, real_targets=%d (%.1f%%), direct_real_targets=%d, residual_completed_targets=%d, current_mixed_fallback=%d, current_capacity_fallback=%d, current_fallback_ratio=%.1f%%, legacy_topology_fallback=%d, legacy_partial_fallback=%d, legacy_fallback_ratio=%.1f%%, sum_mismatch_hours=%d, partial_sum_exceeds_total_hours=%d, range_rejections=%d, topology_rejections=%d, legacy_unknown_topology_rejections=%d, invalid_value_rejections=%d, training_range=%s, current_fallback_range=%s, legacy_fallback_range=%s, affected_range=%s",
            severity,
            total_samples,
            measured_target_samples,
            measured_target_percent,
            real_target_samples,
            residual_target_samples,
            mixed_fallback_samples,
            capacity_fallback_samples,
            current_fallback_percent,
            legacy_topology_samples,
            legacy_partial_samples,
            legacy_fallback_percent,
            int(diagnostics.get("hour_rejections_sum_mismatch", 0)),
            int(diagnostics.get("hour_rejections_partial_sum_exceeds_total", 0)),
            int(diagnostics.get("group_rejections_out_of_range", 0)),
            int(
                diagnostics.get("group_rejections_missing_topology", 0)
                + diagnostics.get("group_rejections_incompatible_lineage", 0)
            ),
            int(diagnostics.get("group_rejections_legacy_unknown_topology", 0)),
            int(diagnostics.get("group_rejections_invalid_value", 0)),
            training_date_range,
            current_fallback_date_range,
            legacy_fallback_date_range,
            affected_date_range,
        )

    async def initialize(self) -> bool:
        """Initialize AI components with Multi-Output LSTM @zara"""
        try:
            import asyncio as asyncio_module

            (asyncio_module.get_running_loop if True else None)()
            self.feature_engineer = FeatureEngineer()
            output_count = self.num_groups if self.num_groups > 0 else 1
            feature_count = calculate_feature_count(self.num_groups)
            self.use_attention = True
            saved_weights = await self.db_manager.get_model_weights()
            self.lstm = self._create_lstm_for_weights(
                feature_count,
                output_count,
                saved_weights.get("lstm") if saved_weights else None,
                strict=bool(
                    saved_weights
                    and saved_weights.get("artifact_hash")
                    and (saved_weights.get("active_model") == "tiny_lstm")
                ),
            )
            self.ridge = TinyRidge(
                input_size=feature_count,
                hidden_size=48,
                sequence_length=24,
                num_outputs=output_count,
            )
            self._instance_log(
                f"Models initialized: {feature_count} features, {output_count} outputs, {self.num_groups} groups, layers=2, heads={self.lstm.num_heads}"
            )
            if saved_weights:
                try:
                    weights_version = saved_weights.get("version", "1.0")
                    if weights_version == "3.0":
                        await self._load_dual_model_weights(
                            saved_weights,
                            feature_count,
                            output_count,
                            strict_active_model=bool(
                                saved_weights.get("artifact_hash")
                            ),
                        )
                    else:
                        _LOGGER.warning(
                            f"Incompatible weight version {weights_version} - V3.0 architecture requires retraining"
                        )
                        self.state = ModelState.UNTRAINED
                except Exception as error:
                    _LOGGER.warning(f"Could not load weights: {error}")
                    self.state = ModelState.UNTRAINED
            else:
                self.state = ModelState.UNTRAINED
                self.active_model = "none"
                _LOGGER.info(
                    f"No weights found - model untrained ({output_count} outputs)"
                )
            await self._load_last_successful_training_time()
            await self.reconcile_pending_model_candidates()
            return True
        except Exception as error:
            _LOGGER.error(f"AI initialization failed: {error}")
            self.state = ModelState.ERROR
            return False

    async def _load_last_successful_training_time(self) -> None:
        """Load the newest governed training time without changing serving state."""
        self.last_successful_training_time = self.last_training_time
        load_training_time = getattr(
            self.db_manager, "get_last_successful_model_training_time", None
        )
        if not callable(load_training_time):
            return
        try:
            training_time = await load_training_time()
        except Exception as error:
            _LOGGER.warning(
                "Latest successful model training time could not be loaded: %s", error
            )
            return
        if training_time is not None:
            self.last_successful_training_time = training_time
        return None if True else None

    async def reconcile_pending_model_candidates(self) -> Optional[str]:
        """Verify every pending artifact before an atomic governance selection."""
        load_pending_ids = getattr(
            self.db_manager, "get_pending_model_candidate_ids", None
        )
        reconcile_candidates = getattr(
            self.db_manager, "reconcile_pending_model_candidates", None
        )
        if not (callable(load_pending_ids) and callable(reconcile_candidates)):
            _LOGGER.warning(
                "Model candidate readiness reconciliation is unavailable; pending candidates will not be selected"
            )
            return
        try:
            pending_ids = list(await load_pending_ids(limit=10000))
        except Exception as error:
            _LOGGER.warning(
                "Model candidate readiness could not be loaded; pending candidates remain unchanged: %s",
                error,
            )
            return
        incompatible_ids = []
        compatible_ids = []
        for candidate_id in pending_ids:
            try:
                await self.create_shadow_candidate_predictor(str(candidate_id))
            except CandidateRuntimeIncompatibleError as error:
                incompatible_ids.append(str(candidate_id))
                _LOGGER.info(
                    "Model candidate %s is runtime-incompatible: %s",
                    candidate_id,
                    error,
                )
                continue
            except ValueError as error:
                incompatible_ids.append(str(candidate_id))
                _LOGGER.info(
                    "Model candidate %s has an invalid runtime artifact: %s",
                    candidate_id,
                    error,
                )
                continue
            except (OSError, asyncio.TimeoutError) as error:
                _LOGGER.warning(
                    "Model candidate %s readiness is transiently unavailable; it remains pending: %s",
                    candidate_id,
                    error,
                )
                return
            except Exception as error:
                _LOGGER.warning(
                    "Model candidate %s readiness could not be verified; it remains pending: %s",
                    candidate_id,
                    error,
                )
                return
            compatible_ids.append(str(candidate_id))
        try:
            reconciliation_result = await reconcile_candidates(
                compatible_candidate_ids=compatible_ids,
                incompatible_candidate_ids=incompatible_ids,
            )
        except Exception as error:
            _LOGGER.warning(
                "Model candidate readiness reconciliation could not be persisted; pending candidates remain unchanged: %s",
                error,
            )
            return
        if isinstance(reconciliation_result, dict):
            pending_candidate_id = reconciliation_result.get("pending_candidate_id")
            readiness = reconciliation_result.get("readiness")
            if readiness:
                _LOGGER.info("Model candidate readiness: %s", readiness)
        else:
            pending_candidate_id = reconciliation_result
        reconsidered_any = False
        if pending_candidate_id is None or pending_candidate_id in compatible_ids:
            load_reconsideration_candidates = getattr(
                self.db_manager,
                "get_migration_superseded_reconsideration_candidates",
                None,
            )
            reconsider_candidate = getattr(
                self.db_manager, "reconsider_migration_superseded_model_candidate", None
            )
            if callable(load_reconsideration_candidates) and callable(
                reconsider_candidate
            ):
                close_incompatible_candidate = getattr(
                    self.db_manager, "close_runtime_incompatible_model_candidate", None
                )
                try:
                    reconsideration_pairs = list(
                        await load_reconsideration_candidates()
                    )
                except Exception as error:
                    _LOGGER.warning(
                        "Migration candidate reconsideration unavailable: %s", error
                    )
                    reconsideration_pairs = []
                for candidate_id, keeper_candidate_id in reconsideration_pairs:
                    runtime_compatible_ids = set(compatible_ids)
                    incompatible_pair = None
                    try:
                        for candidate_pair in (candidate_id, keeper_candidate_id):
                            if candidate_pair not in runtime_compatible_ids:
                                incompatible_pair = candidate_pair
                                await self.create_shadow_candidate_predictor(
                                    candidate_pair
                                )
                                runtime_compatible_ids.add(candidate_pair)
                        reconsidered_any = (
                            bool(
                                await reconsider_candidate(
                                    candidate_id,
                                    keeper_candidate_id,
                                    runtime_compatible_candidate_ids=tuple(
                                        sorted(runtime_compatible_ids)
                                    ),
                                )
                            )
                            or reconsidered_any
                        )
                    except CandidateRuntimeIncompatibleError as error:
                        outcome_note = "; it stays reviewable"
                        if callable(close_incompatible_candidate) and incompatible_pair:
                            try:
                                candidate_closed = bool(
                                    await close_incompatible_candidate(
                                        incompatible_pair,
                                        details={
                                            "source": "migration_reconsideration",
                                            "keeper_candidate_id": keeper_candidate_id,
                                            "reason": str(error),
                                        },
                                    )
                                )
                                outcome_note = (
                                    "" if candidate_closed else "; already terminal"
                                )
                            except Exception as close_error:
                                _LOGGER.warning(
                                    "Migration candidate %s could not be closed as runtime-incompatible: %s",
                                    incompatible_pair,
                                    close_error,
                                )
                        _LOGGER.info(
                            "Migration candidate reconsideration closed: %s is runtime-incompatible (%s)%s",
                            incompatible_pair,
                            error,
                            outcome_note,
                        )
                    except ValueError as error:
                        _LOGGER.info(
                            "Migration candidate reconsideration skipped safely: %s",
                            error,
                        )
                        continue
                    except (OSError, asyncio.TimeoutError) as error:
                        _LOGGER.info(
                            "Migration candidate reconsideration skipped safely: %s",
                            error,
                        )
                        break
                    except Exception as error:
                        _LOGGER.warning(
                            "Migration candidate reconsideration unavailable: %s", error
                        )
                        break
            if reconsidered_any:
                return await self.reconcile_pending_model_candidates()
            if pending_candidate_id:
                reconcile_state = (
                    str(pending_candidate_id),
                    len(compatible_ids),
                    len(incompatible_ids),
                )
                state_changed = reconcile_state != self._last_reconcile_state
                self._last_reconcile_state = reconcile_state
                (_LOGGER.info if state_changed else _LOGGER.debug)(
                    "Model candidate shadow acquisition reconciled: selected=%s compatible=%s incompatible=%s",
                    pending_candidate_id,
                    len(compatible_ids),
                    len(incompatible_ids),
                )
                return str(pending_candidate_id)
        if pending_candidate_id:
            _LOGGER.warning(
                "Model candidate readiness reconciliation returned an unverified candidate: %s",
                pending_candidate_id,
            )
        if incompatible_ids:
            _LOGGER.info(
                "Model candidate readiness reconciled: no compatible candidate; incompatible=%s",
                len(incompatible_ids),
            )
        elif pending_ids:
            _LOGGER.warning(
                "Pending model candidates exist but no runtime-compatible shadow acquisition candidate was selected"
            )
        return None if True else None

    def is_ready(self) -> bool:
        """Check if model is ready for predictions @zara"""
        if self.state != ModelState.READY:
            return False
        if self.active_model == "tiny_lstm" and self.lstm is not None:
            return True
        if (
            self.active_model == "ridge"
            and self.ridge is not None
            and self.ridge.is_trained
        ):
            return True
        return False

    def is_healthy(self) -> bool:
        """Check if predictor is in healthy state @zara"""
        models_present = self.lstm is not None or self.ridge is not None
        return self.state in (ModelState.READY, ModelState.UNTRAINED) and models_present

    def is_bootstrap_phase(self) -> bool:
        """Check if we're in bootstrap phase (Ridge active, 10-50 samples) @zara"""
        return (
            self.active_model == "ridge"
            and self.training_samples is not None
            and (self.training_samples >= MIN_RIDGE_SAMPLES)
            and (self.training_samples < MIN_TRAINING_SAMPLES)
        )

    def get_bootstrap_progress(self) -> float:
        """Get progress through bootstrap phase (0.0 to 1.0) @zara"""
        if not self.training_samples:
            return 0.0
        bootstrap_fraction = (self.training_samples - MIN_RIDGE_SAMPLES) / (
            MIN_TRAINING_SAMPLES - MIN_RIDGE_SAMPLES
        )
        return max(0.0, min(1.0, bootstrap_fraction))

    def get_base_ai_confidence(self) -> float:
        """Get base confidence level for AI predictions (0.0 - 1.0) @zara"""
        if not self.is_ready():
            return 0.0
        accuracy = self.current_accuracy or 0.0
        if accuracy <= 0:
            return 0.0
        if accuracy < 0.5:
            return accuracy
        if accuracy < 0.8:
            return 0.5 + (accuracy - 0.5) * 1.333
        return 0.9 + min(accuracy - 0.8, 0.2) * 0.5

    @property
    def base_ai_confidence(self) -> float:
        """Property accessor for base AI confidence @zara"""
        return self.get_base_ai_confidence()

    async def predict_hour(
        self, hour: int, weather_data: Dict[str, Any], astronomy_data: Dict[str, Any]
    ) -> float:
        """Predict total production for single hour (sum of all groups) @zara"""
        if not self.is_ready():
            return 0.0
        try:
            group_predictions = await self.predict_hour_per_group(
                hour, weather_data, astronomy_data
            )
            total_kwh = sum(
                (group_prediction.power_kwh for group_prediction in group_predictions)
            )
            return max(0.0, total_kwh)
        except Exception as error:
            _LOGGER.error(f"Prediction failed: {error}")
            return 0.0

    def _get_model_predictions(self, sequence: List[List[float]]) -> List[float]:
        """Get predictions from the active model (Ridge or LSTM) @zara"""
        try:
            if self.active_model == "tiny_lstm" and self.lstm:
                return self.lstm.predict(sequence)
            if self.active_model == "ridge" and self.ridge and self.ridge.is_trained:
                return self.ridge.predict(sequence)
            _LOGGER.debug(f"No active model available (active={self.active_model})")
            return []
        except Exception as error:
            _LOGGER.debug(f"Model prediction failed: {error}")
            return []

    def _get_ensemble_predictions(
        self, sequence: List[List[float]]
    ) -> Dict[str, List[float]]:
        """Get predictions from both LSTM and Ridge independently @zara V16.0.2"""
        model_predictions = {"lstm": [], "ridge": []}
        if self.lstm:
            try:
                model_predictions["lstm"] = self.lstm.predict(sequence)
            except Exception as error:
                _LOGGER.debug(f"LSTM ensemble prediction failed: {error}")
        if self.ridge and self.ridge.is_trained:
            try:
                model_predictions["ridge"] = self.ridge.predict(sequence)
            except Exception as error:
                _LOGGER.debug(f"Ridge ensemble prediction failed: {error}")
        return model_predictions if True else None

    async def predict_hour_per_group_ensemble(
        self, hour: int, weather_data: Dict[str, Any], astronomy_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Predict production per panel group using both LSTM and Ridge @zara V16.0.2

        Returns dict with lstm_predictions, ridge_predictions, lstm_total, ridge_total.
        """
        ensemble_result = {
            "lstm_predictions": [],
            "ridge_predictions": [],
            "lstm_total": 0.0,
            "ridge_total": 0.0,
        }
        if not self.is_ready():
            return ensemble_result
        try:
            theoretical_max_kwh = astronomy_data.get("theoretical_max_kwh", None)
            if theoretical_max_kwh is not None and theoretical_max_kwh <= 0:
                return ensemble_result
            historical_predictions = await self._load_predictions_for_sequence()
            target_date = (
                self._forecast_reference_date
                or SafeDateTimeUtil.now().strftime("%Y-%m-%d")
            )
            sequence, previous_day_actual_kwh, previous_hour_actual_kwh = (
                await self._build_temporal_sequence(
                    target_hour=hour,
                    target_date=target_date,
                    weather_data=weather_data,
                    astronomy_data=astronomy_data,
                    all_predictions=historical_predictions,
                )
            )
            ensemble_predictions = self._get_ensemble_predictions(sequence)

            def _build_group_predictions(
                predictions: List[float],
            ) -> List[GroupPrediction]:
                group_predictions = []
                if self.panel_groups:
                    for group_index, panel_group in enumerate(self.panel_groups):
                        if predictions and group_index < len(predictions):
                            group_kwh = predictions[
                                group_index
                            ] * self._inference_output_scale(panel_group)
                        else:
                            group_kwh = 0.0
                        group_predictions.append(
                            GroupPrediction(
                                group_name=panel_group.get(
                                    "name", f"Group_{group_index + 1}"
                                ),
                                power_kwh=max(0.0, group_kwh),
                                azimuth=panel_group.get("azimuth", 180.0),
                                tilt=panel_group.get("tilt", 30.0),
                                capacity_kwp=panel_group.get(
                                    "capacity_kwp", panel_group.get("kwp", 1.0)
                                ),
                            )
                        )
                else:
                    group_kwh = (
                        predictions[0] * self._inference_output_scale(None)
                        if predictions
                        else 0.0
                    )
                    group_predictions.append(
                        GroupPrediction(
                            group_name="Total",
                            power_kwh=max(0.0, group_kwh),
                            azimuth=180.0,
                            tilt=30.0,
                            capacity_kwp=self.solar_capacity,
                        )
                    )
                return group_predictions if True else None

            lstm_group_predictions = _build_group_predictions(
                ensemble_predictions["lstm"]
            )
            ridge_group_predictions = _build_group_predictions(
                ensemble_predictions["ridge"]
            )
            return {
                "lstm_predictions": lstm_group_predictions,
                "ridge_predictions": ridge_group_predictions,
                "lstm_total": sum(
                    (
                        group_prediction.power_kwh
                        for group_prediction in lstm_group_predictions
                    )
                ),
                "ridge_total": sum(
                    (
                        group_prediction.power_kwh
                        for group_prediction in ridge_group_predictions
                    )
                ),
            }
        except Exception as error:
            _LOGGER.error(f"Ensemble prediction failed: {error}")
            return ensemble_result

    async def predict_hour_per_group(
        self, hour: int, weather_data: Dict[str, Any], astronomy_data: Dict[str, Any]
    ) -> List[GroupPrediction]:
        """Predict production per panel group for single hour @zara"""
        if not self.is_ready():
            return [] if True else None
        try:
            theoretical_max_kwh = astronomy_data.get("theoretical_max_kwh", None)
            if theoretical_max_kwh is not None and theoretical_max_kwh <= 0:
                return [] if True else None
            historical_predictions = await self._load_predictions_for_sequence()
            target_date = (
                self._forecast_reference_date
                or SafeDateTimeUtil.now().strftime("%Y-%m-%d")
            )
            sequence, previous_day_actual_kwh, previous_hour_actual_kwh = (
                await self._build_temporal_sequence(
                    target_hour=hour,
                    target_date=target_date,
                    weather_data=weather_data,
                    astronomy_data=astronomy_data,
                    all_predictions=historical_predictions,
                )
            )
            group_predictions = []
            predictions = self._get_model_predictions(sequence)
            if self.panel_groups:
                if not predictions or len(predictions) == 0:
                    predictions = []
                for group_index, panel_group in enumerate(self.panel_groups):
                    if predictions and group_index < len(predictions):
                        group_kwh = predictions[
                            group_index
                        ] * self._inference_output_scale(panel_group)
                    else:
                        group_kwh = 0.0
                    group_predictions.append(
                        GroupPrediction(
                            group_name=panel_group.get(
                                "name", f"Group_{group_index + 1}"
                            ),
                            power_kwh=max(0.0, group_kwh),
                            azimuth=panel_group.get("azimuth", 180.0),
                            tilt=panel_group.get("tilt", 30.0),
                            capacity_kwp=panel_group.get(
                                "capacity_kwp", panel_group.get("kwp", 1.0)
                            ),
                        )
                    )
            else:
                group_kwh = (
                    predictions[0] * self._inference_output_scale(None)
                    if predictions
                    else 0.0
                )
                group_predictions.append(
                    GroupPrediction(
                        group_name="Total",
                        power_kwh=max(0.0, group_kwh),
                        azimuth=180.0,
                        tilt=30.0,
                        capacity_kwp=self.solar_capacity,
                    )
                )
            return group_predictions if True else None
        except Exception as error:
            _LOGGER.error(f"Per-group prediction failed: {error}")
            return [] if True else None

    async def train_model(
        self,
        run_feature_importance: bool = False,
        lstm_hyperparameters: Optional[Dict[str, Any]] = None,
        through_date: Optional[str] = None,
    ) -> TrainingResult:
        """Train AI models with historical data @zara"""
        async with self._candidate_training_lock:
            lease_owner = str(uuid.uuid4())
            if not await self.db_manager.acquire_model_training_lease(lease_owner):
                return TrainingResult(
                    success=False,
                    error_message="another model training run is already active",
                )
            stop_event, lease_lost, heartbeat_task = (
                self._start_training_lease_heartbeat(lease_owner)
            )
            try:
                pending_candidate_id = (
                    await self.db_manager.get_pending_model_candidate_id()
                )
                if pending_candidate_id:
                    skip_reason = f"candidate shadow evaluation pending for {pending_candidate_id}"
                    _LOGGER.info("AI model training skipped: %s", skip_reason)
                    return TrainingResult(success=False, error_message=skip_reason)
                return await self._train_model_candidate(
                    run_feature_importance,
                    lstm_hyperparameters=lstm_hyperparameters,
                    through_date=through_date,
                    lease_owner=lease_owner,
                )
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    return TrainingResult(
                        success=False, error_message="model training lease lost"
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(stop_event, heartbeat_task)
                await self.db_manager.release_model_training_lease(lease_owner)
        return None if True else None

    async def train_model_for_reaction(
        self, request_id: str, supersede_candidate_id: str, through_date: str
    ) -> TrainingResult:
        """Train one replacement candidate for a claimed drift reaction."""
        async with self._candidate_training_lock:
            lease_owner = str(uuid.uuid4())
            lease_acquired = await self.db_manager.acquire_model_training_lease(
                lease_owner, request_id
            )
            if not lease_acquired:
                return TrainingResult(
                    success=False,
                    error_message="another model training run is already active",
                )
            stop_event, lease_lost, heartbeat_task = (
                self._start_training_lease_heartbeat(lease_owner)
            )
            try:
                pending_candidate_id = (
                    await self.db_manager.get_pending_model_candidate_id()
                )
                if pending_candidate_id != supersede_candidate_id:
                    return TrainingResult(
                        success=False,
                        error_message="model reaction candidate changed before training",
                    )
                return await self._train_model_candidate(
                    False,
                    reaction_context={
                        "request_id": request_id,
                        "supersede_candidate_id": supersede_candidate_id,
                        "lease_owner": lease_owner,
                    },
                    through_date=through_date,
                    lease_owner=lease_owner,
                )
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    return TrainingResult(
                        success=False, error_message="model training lease lost"
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(stop_event, heartbeat_task)
                await self.db_manager.release_model_training_lease(lease_owner)
        return None if True else None

    async def run_grid_search_candidate(
        self,
        optimizer: Any,
        retrain_after: bool = True,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[Optional[Any], Optional[TrainingResult]]:
        """Select hyperparameters and optionally train one governed candidate."""
        async with self._candidate_training_lock:
            lease_owner = str(uuid.uuid4())
            if not await self.db_manager.acquire_model_training_lease(lease_owner):
                return (
                    None,
                    TrainingResult(
                        success=False,
                        error_message="another model training run is already active",
                    ),
                )
            stop_event, lease_lost, heartbeat_task = (
                self._start_training_lease_heartbeat(lease_owner)
            )
            last_search_result = None
            try:
                pending_candidate_id = (
                    await self.db_manager.get_pending_model_candidate_id()
                )
                if pending_candidate_id:
                    return (
                        None,
                        TrainingResult(
                            success=False,
                            error_message=f"candidate shadow evaluation pending for {pending_candidate_id}",
                        ),
                    )
                training_data = await self._prepare_training_data()
                X_sequences, y_targets, provenance_rows = training_data
                if len(X_sequences) < MIN_TRAINING_SAMPLES:
                    return (
                        None,
                        TrainingResult(
                            success=False,
                            samples_used=len(X_sequences),
                            error_message=f"Need {MIN_TRAINING_SAMPLES} samples for Grid-Search, got {len(X_sequences)}",
                        ),
                    )
                sample_dates, source_intervals = self._training_temporal_metadata(
                    provenance_rows, len(X_sequences)
                )
                feature_count = calculate_feature_count(self.num_groups)
                output_count = self.num_groups if self.num_groups > 0 else 1

                async def lease_progress_callback(*args: Any) -> None:
                    if not await self.db_manager.renew_model_training_lease(
                        lease_owner
                    ):
                        raise RuntimeError("model training lease expired")
                    if progress_callback is not None:
                        await progress_callback(*args)
                    return None if True else None

                search_result = await optimizer.run_grid_search(
                    lstm_class=TinyLSTM,
                    X_sequences=X_sequences,
                    y_targets=y_targets,
                    input_size=feature_count,
                    sequence_length=TRAINING_SEQUENCE_LENGTH,
                    num_outputs=output_count,
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    sample_dates=sample_dates,
                    source_intervals=source_intervals,
                    seed=TRAINING_RANDOM_SEED,
                    progress_callback=lease_progress_callback,
                )
                last_search_result = search_result
                if not search_result.success or not retrain_after:
                    return (search_result, None)
                if not await self.db_manager.renew_model_training_lease(lease_owner):
                    return (
                        search_result,
                        TrainingResult(
                            success=False, error_message="model training lease expired"
                        ),
                    )
                training_result = await self._train_model_candidate(
                    False,
                    lstm_hyperparameters=search_result.best_params,
                    training_data=training_data,
                    hyperparameter_selection={
                        "contract_version": "grid_candidate_v1",
                        "parameter_grid_hash": canonical_stable_hash(
                            optimizer.param_grid
                        ),
                        "selected_params": search_result.best_params,
                        "split_policy": "chronological_target_date_with_interval_purge",
                    },
                    lease_owner=lease_owner,
                )
                return (search_result, training_result)
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    return (
                        last_search_result,
                        TrainingResult(
                            success=False, error_message="model training lease lost"
                        ),
                    )
                raise
            finally:
                await self._stop_training_lease_heartbeat(stop_event, heartbeat_task)
                await self.db_manager.release_model_training_lease(lease_owner)
        return None if True else None

    def _start_training_lease_heartbeat(
        self, lease_owner: str, interval_seconds: float = 60.0
    ) -> Tuple[asyncio.Event, asyncio.Event, asyncio.Task]:
        """Renew the persistent lease throughout long in-process training."""
        stop_event = asyncio.Event()
        heartbeat_lost_event = asyncio.Event()
        heartbeat_owner_task = asyncio.current_task()
        if heartbeat_owner_task is None:
            raise RuntimeError("model training heartbeat requires an owning task")

        def abort_owner() -> None:
            if stop_event.is_set():
                return
            heartbeat_lost_event.set()
            heartbeat_owner_task.cancel()

        async def heartbeat() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                except asyncio.TimeoutError:
                    try:
                        lease_renewed = (
                            await self.db_manager.renew_model_training_lease(
                                lease_owner
                            )
                        )
                    except Exception as error:
                        _LOGGER.error(
                            "Model training lease heartbeat failed: %s", error
                        )
                        abort_owner()
                        return
                    if not lease_renewed:
                        _LOGGER.error("Model training lease heartbeat was lost")
                        abort_owner()
                        return

        return (stop_event, heartbeat_lost_event, asyncio.create_task(heartbeat()))

    @staticmethod
    async def _stop_training_lease_heartbeat(
        stop_event: asyncio.Event, heartbeat_task: asyncio.Task
    ) -> None:
        stop_event.set()
        await heartbeat_task

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
            X_sequences, y_targets, provenance_rows = training_data
            sample_count = len(X_sequences)
            sample_dates, source_intervals = self._training_temporal_metadata(
                provenance_rows, sample_count
            )
            self._log_panel_group_training_diagnostics()
            season = get_season(SafeDateTimeUtil.now().month)
            seasonal_archive = await self._load_seasonal_archive(season)
            if sample_count < MIN_RIDGE_SAMPLES:
                return TrainingResult(
                    success=False,
                    samples_used=sample_count,
                    error_message=f"Need {MIN_RIDGE_SAMPLES} samples for Ridge, got {sample_count}",
                )
            if not TinyRidge.has_viable_temporal_split(
                sample_count,
                TRAINING_VALIDATION_SPLIT,
                sample_dates=sample_dates,
                source_intervals=source_intervals,
            ):
                _LOGGER.info(
                    "AI model training deferred: independent training and holdout dates are not available yet"
                )
                return TrainingResult(
                    success=False,
                    samples_used=sample_count,
                    reason_code="insufficient_independent_training_days",
                )
            feature_count = calculate_feature_count(self.num_groups)
            output_count = self.num_groups if self.num_groups > 0 else 1
            candidate_ridge = TinyRidge(
                input_size=feature_count,
                hidden_size=48,
                sequence_length=TRAINING_SEQUENCE_LENGTH,
                num_outputs=output_count,
            )
            lstm_parameters = self._validated_lstm_hyperparameters(lstm_hyperparameters)
            candidate_lstm = TinyLSTM(
                input_size=feature_count,
                hidden_sizes=lstm_parameters["hidden_sizes"],
                sequence_length=TRAINING_SEQUENCE_LENGTH,
                num_outputs=output_count,
                learning_rate=lstm_parameters["learning_rate"],
                num_heads=lstm_parameters["num_heads"],
                seed=TRAINING_RANDOM_SEED,
            )
            ridge_result = None
            lstm_result = None
            trained_any = False
            if sample_count >= MIN_RIDGE_SAMPLES:
                _LOGGER.info(f"Training Ridge with {sample_count} samples...")
                ridge_result = await candidate_ridge.train(
                    X_sequences=X_sequences,
                    y_targets=y_targets,
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    sample_dates=sample_dates,
                    source_intervals=source_intervals,
                )
                if ridge_result.get("success"):
                    _LOGGER.info(
                        f"Ridge trained: R²={ridge_result.get('accuracy', 0):.3f}, alpha={ridge_result.get('alpha', 0):.4f}"
                    )
                    trained_any = True
            if sample_count >= MIN_TRAINING_SAMPLES:
                if seasonal_archive and "lstm" in seasonal_archive:
                    try:
                        archived_lstm = seasonal_archive["lstm"]
                        archive_dimensions = (
                            archived_lstm.get("input_size", 0),
                            archived_lstm.get("hidden_size", 0),
                            archived_lstm.get("hidden2", 0),
                            archived_lstm.get("num_outputs", 0),
                            archived_lstm.get("num_heads", 0),
                        )
                        runtime_dimensions = (
                            candidate_lstm.input_size,
                            candidate_lstm.hidden1,
                            candidate_lstm.hidden2,
                            candidate_lstm.num_outputs,
                            candidate_lstm.num_heads,
                        )
                        if archive_dimensions == runtime_dimensions:
                            candidate_lstm.set_weights(seasonal_archive["lstm"])
                            _LOGGER.info(
                                "Seasonal warm-start: %s, archive %d (%d samples, R²=%.3f)",
                                season,
                                seasonal_archive["lstm"]["source_year"],
                                seasonal_archive["lstm"]["training_samples"],
                                seasonal_archive["lstm"]["accuracy"],
                            )
                        else:
                            _LOGGER.info(
                                "Archive architecture mismatch: saved=%s vs needed=%s; training from scratch",
                                archive_dimensions,
                                runtime_dimensions,
                            )
                    except Exception as error:
                        _LOGGER.debug("Archive incompatible: %s", error)
                _LOGGER.info(f"Training LSTM with {sample_count} samples...")
                lstm_result = await candidate_lstm.train(
                    X_sequences=X_sequences,
                    y_targets=y_targets,
                    epochs=lstm_parameters["epochs"],
                    batch_size=lstm_parameters["batch_size"],
                    validation_split=TRAINING_VALIDATION_SPLIT,
                    early_stopping_patience=lstm_parameters["early_stopping_patience"],
                    sample_dates=sample_dates,
                    source_intervals=source_intervals,
                    seed=TRAINING_RANDOM_SEED,
                )
                if lstm_result.get("success"):
                    _LOGGER.info(
                        f"LSTM trained: R²={lstm_result.get('accuracy', 0):.3f}, epochs={lstm_result.get('epochs_trained', 0)}, layers=2, heads={lstm_parameters['num_heads']}"
                    )
                    trained_any = True
            if trained_any:
                candidate_model = self._select_candidate_model(
                    sample_count, ridge_result, lstm_result
                )
                if (
                    candidate_model == "tiny_lstm"
                    and lstm_result
                    and lstm_result.get("success")
                ):
                    selected_training_result = lstm_result
                elif ridge_result and ridge_result.get("success"):
                    selected_training_result = ridge_result
                else:
                    selected_training_result = None
                candidate_accuracy = (
                    selected_training_result.get("accuracy", 0.0)
                    if selected_training_result
                    else 0.0
                )
                candidate_rmse = (
                    selected_training_result.get("rmse", 0.0)
                    if selected_training_result
                    else 0.0
                )
                candidate_id = await self._save_candidate_async(
                    provenance_rows,
                    candidate_ridge,
                    candidate_lstm,
                    candidate_model,
                    sample_count,
                    candidate_accuracy,
                    candidate_rmse,
                    bool(lstm_result and lstm_result.get("success")),
                    reaction_context,
                    lstm_parameters,
                    hyperparameter_selection,
                    through_date,
                    lease_owner,
                )
                self._latest_model_candidate_id = candidate_id
                rmse_note = f", RMSE={candidate_rmse:.3f}kWh" if candidate_rmse else ""
                _LOGGER.info(
                    f"Training candidate stored: id={candidate_id}, R²={candidate_accuracy:.3f}{rmse_note}, samples={sample_count}, outputs={output_count}"
                )
                if run_feature_importance and sample_count >= MIN_TRAINING_SAMPLES:
                    _LOGGER.info(
                        "Candidate feature importance deferred until isolated candidate evaluation is available"
                    )
                return TrainingResult(
                    success=True,
                    accuracy=candidate_accuracy,
                    samples_used=sample_count,
                    feature_count=feature_count,
                    num_outputs=output_count,
                    epochs_trained=(
                        lstm_result.get("epochs_trained", 0) if lstm_result else 1
                    ),
                    has_attention=self.use_attention,
                    rmse=candidate_rmse,
                )
            return TrainingResult(
                success=False, error_message="Both Ridge and LSTM training failed"
            )
        except Exception as error:
            _LOGGER.error(f"Training failed: {error}")
            return TrainingResult(success=False, error_message=str(error))

    def _validated_lstm_hyperparameters(
        self, overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Return bounded, reproducible LSTM training parameters."""
        hyperparameters = dict(DEFAULT_LSTM_HYPERPARAMETERS)
        if overrides is None and self.lstm is not None:
            hyperparameters.update(
                hidden_sizes=(self.lstm.hidden1, self.lstm.hidden2),
                learning_rate=float(self.lstm.lr_max),
                num_heads=int(self.lstm.num_heads),
            )
        elif overrides is not None:
            unsupported_parameters = set(overrides) - {
                "num_heads",
                "batch_size",
                "hidden_sizes",
                "learning_rate",
            }
            if unsupported_parameters:
                raise ValueError(
                    "unsupported LSTM hyperparameters: "
                    + ", ".join(sorted(unsupported_parameters))
                )
            hyperparameters.update(overrides)
        hidden_sizes = hyperparameters["hidden_sizes"]
        if not isinstance(hidden_sizes, (list, tuple)) or len(hidden_sizes) != 2:
            raise ValueError("LSTM hidden_sizes must contain exactly two layers")
        hidden_sizes = tuple((int(hidden_size) for hidden_size in hidden_sizes))
        num_heads = int(hyperparameters["num_heads"])
        batch_size = int(hyperparameters["batch_size"])
        learning_rate = float(hyperparameters["learning_rate"])
        if not all((8 <= hidden_size <= 128 for hidden_size in hidden_sizes)):
            raise ValueError("LSTM hidden sizes must be between 8 and 128")
        if not 1 <= num_heads <= 16 or hidden_sizes[1] % num_heads:
            raise ValueError("LSTM attention heads must divide the second layer")
        if not 1 <= batch_size <= 256:
            raise ValueError("LSTM batch size must be between 1 and 256")
        if not 1e-05 <= learning_rate <= 0.05:
            raise ValueError("LSTM learning rate is outside the supported range")
        hyperparameters.update(
            hidden_sizes=hidden_sizes,
            num_heads=num_heads,
            batch_size=batch_size,
            learning_rate=learning_rate,
        )
        return hyperparameters

    def _create_lstm_for_weights(
        self,
        feature_count: int,
        num_outputs: int,
        weights: Optional[Dict[str, Any]],
        strict: bool = False,
    ) -> TinyLSTM:
        """Construct the exact bounded architecture encoded by an artifact."""
        saved_parameters = None
        if weights:
            saved_parameters = {
                "hidden_sizes": (
                    weights.get("hidden_size", 48),
                    weights.get("hidden2", 24),
                ),
                "learning_rate": weights.get("training_learning_rate", 0.005),
                "num_heads": weights.get("num_heads", 4),
                "batch_size": 16,
            }
        try:
            hyperparameters = self._validated_lstm_hyperparameters(saved_parameters)
        except (TypeError, ValueError) as error:
            if strict:
                raise ValueError("invalid active LSTM artifact architecture") from error
            _LOGGER.warning("Invalid LSTM artifact architecture: %s", error)
            hyperparameters = dict(DEFAULT_LSTM_HYPERPARAMETERS)
        return TinyLSTM(
            input_size=feature_count,
            hidden_sizes=hyperparameters["hidden_sizes"],
            sequence_length=TRAINING_SEQUENCE_LENGTH,
            num_outputs=num_outputs,
            learning_rate=hyperparameters["learning_rate"],
            num_heads=hyperparameters["num_heads"],
            seed=TRAINING_RANDOM_SEED,
        )

    @staticmethod
    def _select_candidate_model(
        n_samples: int,
        ridge_result: Optional[Dict[str, Any]],
        lstm_result: Optional[Dict[str, Any]],
    ) -> str:
        """Select the candidate model without changing the serving state."""
        ridge_success = ridge_result and ridge_result.get("success")
        lstm_success = lstm_result and lstm_result.get("success")
        if n_samples >= MIN_TRAINING_SAMPLES and lstm_success:
            _LOGGER.info(f"Selected candidate model: LSTM ({n_samples} samples)")
            return "tiny_lstm"
        if ridge_success:
            _LOGGER.info(
                f"Selected candidate model: Ridge ({n_samples} samples, bootstrap phase)"
            )
            return "ridge"
        _LOGGER.warning("No candidate model available")
        return "none"

    def _training_code_source_hash(self) -> str:
        source_paths = (
            Path(__file__),
            Path(__file__).with_name("ai_tiny_lstm.py"),
            Path(__file__).with_name("ai_tiny_ridge.py"),
        )
        return canonical_stable_hash(
            {
                source_path.name: source_path.read_text(encoding="utf-8")
                for source_path in source_paths
            }
        )

    @staticmethod
    def _training_temporal_metadata(
        provenance_rows: List[Dict[str, Any]], sample_count: int
    ) -> Tuple[List[str], List[Tuple[datetime, datetime]]]:
        anchors_by_sample = {}
        for row in provenance_rows:
            sample_index = int(row["sample_index"])
            target_anchor = datetime.fromisoformat(str(row["target_anchor"]))
            previous_anchor = anchors_by_sample.get(sample_index)
            if previous_anchor is not None and previous_anchor != target_anchor:
                raise ValueError("inconsistent temporal anchor within training sample")
            anchors_by_sample[sample_index] = target_anchor
        if set(anchors_by_sample) != set(range(sample_count)):
            raise ValueError("temporal metadata is incomplete")
        sample_anchors = [anchors_by_sample[index] for index in range(sample_count)]
        return (
            [target_anchor.date().isoformat() for target_anchor in sample_anchors],
            [
                (
                    target_anchor - timedelta(hours=TRAINING_SEQUENCE_LENGTH - 1),
                    target_anchor,
                )
                for target_anchor in sample_anchors
            ],
        )

    async def _save_candidate_async(
        self,
        provenance_rows: List[Dict[str, Any]],
        candidate_ridge: TinyRidge,
        candidate_lstm: TinyLSTM,
        candidate_active_model: str,
        training_samples: int,
        accuracy: float,
        rmse: float,
        lstm_trained: bool,
        reaction_context: Optional[Dict[str, str]] = None,
        lstm_hyperparameters: Optional[Dict[str, Any]] = None,
        hyperparameter_selection: Optional[Dict[str, Any]] = None,
        training_through_date: Optional[str] = None,
        lease_owner: Optional[str] = None,
    ) -> str:
        """Store trained weights as a non-serving candidate with complete provenance."""
        feature_names = list(self.feature_engineer.BASE_FEATURE_NAMES)
        for panel_group in self.panel_groups:
            group_name = panel_group.get("name", "")
            feature_names.extend(
                (
                    f"{group_feature_name}_{group_name}"
                    for group_feature_name in self.feature_engineer.GROUP_FEATURE_NAMES
                )
            )
        training_config = {
            "model_contract_version": "candidate-v2",
            "feature_count": calculate_feature_count(self.num_groups),
            "feature_names": feature_names,
            "feature_ranges": self.feature_engineer.FEATURE_RANGES,
            "num_outputs": self.num_groups if self.num_groups > 0 else 1,
            "output_names": [
                panel_group.get("name", f"Group_{group_index + 1}")
                for group_index, panel_group in enumerate(self.panel_groups)
            ]
            or ["total"],
            "training_days": TRAINING_DAYS,
            "min_ridge_samples": MIN_RIDGE_SAMPLES,
            "min_lstm_samples": MIN_TRAINING_SAMPLES,
            "sequence_length": TRAINING_SEQUENCE_LENGTH,
            "validation_split": TRAINING_VALIDATION_SPLIT,
            "split_policy": "chronological_target_date_with_interval_purge",
            "random_seed": TRAINING_RANDOM_SEED,
            "target_policy": "per_output_normalized_kwh_with_provenance",
            "training_through_date": training_through_date,
            "ridge": {"alphas": list(self.ridge.DEFAULT_ALPHAS) if self.ridge else []},
            "lstm": {
                parameter_name: (
                    list(parameter_value)
                    if isinstance(parameter_value, tuple)
                    else parameter_value
                )
                for parameter_name, parameter_value in (
                    lstm_hyperparameters or DEFAULT_LSTM_HYPERPARAMETERS
                ).items()
            },
        }
        dataset_fingerprint = canonical_stable_hash(provenance_rows)
        if hyperparameter_selection:
            training_config["hyperparameter_selection"] = {
                **hyperparameter_selection,
                "selection_dataset_fingerprint": dataset_fingerprint,
            }
        weight_artifact = {
            "version": "3.0",
            "active_model": candidate_active_model,
            "training_samples": training_samples,
            "accuracy": accuracy,
            "rmse": rmse,
            "output_scale": self._build_output_scale_payload(),
            "ridge": (
                candidate_ridge.get_weights() if candidate_ridge.is_trained else None
            ),
            "lstm": candidate_lstm.get_weights() if lstm_trained else None,
        }
        code_source_hash = await self.hass.async_add_executor_job(
            self._training_code_source_hash
        )
        candidate_id = await self.db_manager.save_model_candidate(
            {
                "dataset_fingerprint": dataset_fingerprint,
                "training_config_hash": canonical_stable_hash(training_config),
                "code_source_hash": code_source_hash,
                "config": training_config,
                "sample_count": training_samples,
            },
            provenance_rows,
            weight_artifact,
            reaction_context,
            lease_owner,
        )
        self.last_successful_training_time = SafeDateTimeUtil.now()
        return candidate_id

    async def promote_model_candidate(
        self, candidate_id: str, evaluation_id: str
    ) -> None:
        """Promote a verified candidate after validating its serving runtime."""
        candidate_predictor, artifact_hash = (
            await self.create_shadow_candidate_predictor(candidate_id)
        )
        await self.db_manager.promote_model_candidate(candidate_id, evaluation_id)
        self.ridge = candidate_predictor.ridge
        self.lstm = candidate_predictor.lstm
        self.training_samples = candidate_predictor.training_samples
        self.current_accuracy = candidate_predictor.current_accuracy
        self.current_rmse = candidate_predictor.current_rmse
        self.last_training_time = candidate_predictor.last_training_time
        self.active_model = candidate_predictor.active_model
        self.state = candidate_predictor.state
        self._artifact_group_scales = dict(candidate_predictor._artifact_group_scales)
        self._artifact_global_scale = candidate_predictor._artifact_global_scale

    async def create_shadow_candidate_predictor(
        self, candidate_id: str
    ) -> Tuple["AIPredictor", str]:
        """Load a candidate into an isolated predictor without serving mutation."""
        weight_artifact = await self.db_manager.get_model_candidate_weights(
            candidate_id
        )
        candidate_predictor = AIPredictor(
            hass=self.hass,
            db_manager=self.db_manager,
            error_handler=self.error_handler,
            notification_service=self.notification_service,
            config_entry=self.config_entry,
            panel_groups=self.panel_groups,
            solar_capacity=self.solar_capacity,
            shadow_candidate_id=candidate_id,
        )
        candidate_predictor.set_entities(
            solar_capacity=self.solar_capacity,
            power_entity=self.power_entity,
            weather_entity=self.weather_entity,
        )
        candidate_predictor.feature_engineer = self.feature_engineer
        candidate_predictor._historical_cache = self._historical_cache
        output_count = self.num_groups if self.num_groups > 0 else 1
        feature_count = calculate_feature_count(self.num_groups)
        try:
            candidate_predictor.lstm = candidate_predictor._create_lstm_for_weights(
                feature_count,
                output_count,
                weight_artifact.get("lstm"),
                strict=weight_artifact.get("active_model") == "tiny_lstm",
            )
            candidate_predictor.ridge = TinyRidge(
                input_size=feature_count,
                hidden_size=48,
                sequence_length=24,
                num_outputs=output_count,
            )
            await candidate_predictor._load_dual_model_weights(
                weight_artifact,
                feature_count,
                output_count,
                strict_active_model=True,
                quiet_mismatch=True,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateRuntimeIncompatibleError(
                f"candidate model is incompatible with the serving architecture ({_artifact_architecture_summary(weight_artifact)}, serving input={feature_count}/outputs={output_count})"
            ) from error
        if not candidate_predictor.is_ready():
            raise CandidateRuntimeIncompatibleError(
                f"candidate model is incompatible with the serving architecture ({_artifact_architecture_summary(weight_artifact)}, serving input={feature_count}/outputs={output_count})"
            )
        probe_sequence = [
            [0.0] * feature_count for step_index in range(TRAINING_SEQUENCE_LENGTH)
        ]
        try:
            probe_predictions = candidate_predictor._get_model_predictions(
                probe_sequence
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as error:
            raise CandidateRuntimeIncompatibleError(
                "candidate model failed finite probe inference"
            ) from error
        if len(probe_predictions) != output_count or not all(
            (
                math.isfinite(float(prediction_value))
                for prediction_value in probe_predictions
            )
        ):
            raise CandidateRuntimeIncompatibleError(
                "candidate model failed finite probe inference"
            )
        return (candidate_predictor, str(weight_artifact["artifact_hash"]))

    async def _prepare_training_data(
        self, through_date: Optional[str] = None
    ) -> Tuple[List[List[List[float]]], List[Any], List[Dict[str, Any]]]:
        """Load and prepare Multi-Output training data from database @zara"""
        X_sequences = []
        y_targets = []
        seen_prediction_ids = set()
        provenance_rows = []
        self._reset_panel_group_training_diagnostics()
        try:
            if through_date is None:
                training_end_date = SafeDateTimeUtil.now().date()
            else:
                training_end_date = date.fromisoformat(through_date) + timedelta(days=1)
            training_start_date = training_end_date - timedelta(days=TRAINING_DAYS)
            async with self.db_manager.transaction():
                prediction_rows = await self.db_manager.fetchall(
                    f"SELECT hp.target_date, hp.target_hour, hp.actual_kwh, hp.prediction_id,\n                          hp.is_outlier, hp.inverter_clipped,\n                          pw.temperature, pw.solar_radiation_wm2, pw.wind, pw.humidity,\n                          pw.rain, pw.clouds,\n                          pa.sun_elevation_deg, pa.theoretical_max_kwh, pa.clear_sky_radiation_wm2,\n                          hp.physics_kwh, hp.ai_kwh, hp.ai_confidence, hp.prediction_kwh,\n                          pw.pressure,\n                          pw.diffuse_radiation,\n                          pw.direct_radiation,\n                          pa.sun_azimuth_deg,\n                          pa.daylight_hours,\n                          pa.day_progress_ratio,\n                          pa.hours_after_sunrise,\n                          pa.hours_before_sunset,\n                          pa.hours_since_solar_noon,\n                          hp.exclude_from_learning,\n                          hp.exclude_from_clean_evaluation,\n                          hp.mppt_throttled,\n                          hp.inverter_clipped,\n                          hp.has_weather_alert,\n                          hp.is_outlier,\n                          hp.weather_alert_type,\n                          hp.mppt_throttle_reason\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date >= ? AND hp.target_date < ?\n                     {HOURLY_LEARNING_ELIGIBILITY_SQL}\n                   ORDER BY hp.target_date, hp.target_hour",
                    (training_start_date.isoformat(), training_end_date.isoformat()),
                )
                self._active_group_topology_by_name = (
                    await self._load_active_group_topology_by_name(strict=True)
                )
                panel_group_rows = await self.db_manager.fetchall(
                    f"SELECT ppg.prediction_id, ppg.group_name, ppg.actual_kwh,\n                          ppg.config_epoch_id, ppg.group_uid, ppg.group_lineage_uid,\n                          ppg.power_wp_at_prediction\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   WHERE hp.target_date >= ? AND hp.target_date < ?\n                       AND ppg.actual_kwh IS NOT NULL\n                       {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}",
                    (training_start_date.isoformat(), training_end_date.isoformat()),
                )
            group_actuals_by_prediction = {}
            for group_row in panel_group_rows:
                prediction_key = group_row[0]
                if prediction_key not in group_actuals_by_prediction:
                    group_actuals_by_prediction[prediction_key] = {}
                group_actuals_by_prediction[prediction_key][group_row[1]] = {
                    "actual_kwh": group_row[2],
                    "config_epoch_id": group_row[3],
                    "group_uid": group_row[4],
                    "group_lineage_uid": group_row[5],
                    "power_wp_at_prediction": group_row[6],
                }
            predictions_by_id = {}
            for row in prediction_rows:
                prediction_key = f"{row[0]}_{row[1]:02d}"
                prediction_id = row[3]
                predictions_by_id[prediction_key] = {
                    "target_date": row[0],
                    "target_hour": row[1],
                    "prediction_id": prediction_id,
                    "actual_kwh": row[2],
                    "panel_group_actuals": group_actuals_by_prediction.get(
                        prediction_id, {}
                    ),
                    "weather_corrected": (
                        {
                            "temperature": row[6],
                            "solar_radiation_wm2": row[7],
                            "wind": row[8],
                            "humidity": row[9],
                            "rain": row[10],
                            "clouds": row[11],
                            "pressure": row[19],
                            "diffuse_radiation": row[20],
                            "dni": row[21],
                        }
                        if row[6] is not None
                        else {}
                    ),
                    "astronomy": (
                        {
                            "sun_elevation_deg": row[12],
                            "theoretical_max_kwh": row[13],
                            "clear_sky_radiation_wm2": row[14],
                            "sun_azimuth_deg": row[22],
                            "daylight_hours": row[23],
                            "day_progress_ratio": row[24],
                            "hours_after_sunrise": row[25],
                            "hours_before_sunset": row[26],
                            "hours_since_solar_noon": row[27],
                        }
                        if row[12] is not None
                        else {}
                    ),
                    "physics_kwh": row[15],
                    "ai_kwh": row[16],
                    "ai_confidence_stored": row[17],
                    "prediction_kwh": row[18],
                    "flags": _hourly_governance_flags(
                        is_outlier=row[4] or row[33],
                        inverter_clipped=row[5] or row[31],
                        exclude_from_learning=row[28],
                        exclude_from_clean_evaluation=row[29],
                        mppt_throttled=row[30],
                        has_weather_alert=row[32],
                        weather_alert_type=row[34],
                        mppt_throttle_reason=row[35],
                    ),
                }
                continue
            for hour_record in predictions_by_id.values():
                self._process_hour_data_for_training(
                    hour_record,
                    training_start_date,
                    training_end_date,
                    X_sequences,
                    y_targets,
                    seen_prediction_ids,
                    predictions_by_id,
                    provenance_rows,
                )
            _LOGGER.info(
                f"Prepared {len(X_sequences)} training samples from {len(seen_prediction_ids)} days, {self.num_groups} outputs"
            )
        except Exception as error:
            _LOGGER.error(f"Error preparing training data: {error}")
            raise
        return (X_sequences, y_targets, provenance_rows)

    async def _load_active_group_topology_by_name(
        self, strict: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """Load current panel-group topology metadata for training compatibility checks."""
        try:
            topology_rows = await self.db_manager.fetchall(
                "SELECT eg.display_name, eg.group_uid, eg.group_lineage_uid, eg.power_wp, e.valid_from\n                   FROM panel_group_config_epochs e\n                   JOIN panel_group_config_epoch_groups eg ON eg.epoch_id = e.epoch_id\n                   WHERE e.valid_to IS NULL\n                   ORDER BY eg.display_name"
            )
        except Exception:
            if strict:
                raise
            return {}
        return {
            row[0]: {
                "group_uid": row[1],
                "group_lineage_uid": row[2],
                "power_wp": row[3],
                "valid_from": row[4],
            }
            for row in topology_rows
        }

    @staticmethod
    def _date_before_topology_valid_from(
        date_str: Optional[str], valid_from: Any
    ) -> bool:
        if not date_str or not valid_from:
            return False
        try:
            prediction_date = datetime.fromisoformat(str(date_str)[:10]).date()
            valid_from_text = str(valid_from).replace("Z", "+00:00")
            valid_from_date = datetime.fromisoformat(valid_from_text).date()
        except (TypeError, ValueError):
            return False
        return prediction_date < valid_from_date

    def _is_legacy_unknown_topology_target(
        self,
        meta: Dict[str, Any],
        current_topology: Dict[str, Any],
        date_str: Optional[str],
    ) -> bool:
        if not meta or not current_topology:
            return False
        topology_metadata_present = any(
            (
                meta.get(metadata_key)
                for metadata_key in (
                    "config_epoch_id",
                    "group_uid",
                    "group_lineage_uid",
                    "power_wp_at_prediction",
                )
            )
        )
        if topology_metadata_present:
            return False
        return self._date_before_topology_valid_from(
            date_str, current_topology.get("valid_from")
        )

    @staticmethod
    def _has_panel_group_topology_metadata(raw_value: Any) -> bool:
        if not isinstance(raw_value, dict):
            return False
        return any(
            (
                raw_value.get(metadata_key)
                for metadata_key in (
                    "config_epoch_id",
                    "group_uid",
                    "group_lineage_uid",
                    "power_wp_at_prediction",
                )
            )
        )

    def _is_before_active_panel_group_topology(self, date_str: Optional[str]) -> bool:
        active_topology_by_name = (
            getattr(self, "_active_group_topology_by_name", {}) or {}
        )
        return any(
            (
                self._date_before_topology_valid_from(
                    date_str, group_topology.get("valid_from")
                )
                for group_topology in active_topology_by_name.values()
            )
        )

    def _is_legacy_partial_group_training_context(
        self,
        panel_group_actuals: Any,
        valid_actuals: Dict[str, float],
        expected_group_names: List[str],
        date_str: Optional[str],
    ) -> bool:
        if not self._is_before_active_panel_group_topology(date_str):
            return False
        if not panel_group_actuals or not isinstance(panel_group_actuals, dict):
            return True
        if expected_group_names:
            if all(
                (group_name in valid_actuals for group_name in expected_group_names)
            ):
                return False
        return any(
            (
                not self._has_panel_group_topology_metadata(target_metadata)
                for target_metadata in panel_group_actuals.values()
            )
        )

    def _normalise_group_training_target(
        self,
        group_name: str,
        raw_value: Any,
        current_group: Dict[str, Any],
        date_str: Optional[str] = None,
    ) -> Optional[float]:
        if not current_group:
            self._record_panel_group_training_diagnostic(
                "group_rejections_missing_topology", date_str=date_str
            )
            _LOGGER.debug(
                "Skipping panel group training target '%s': group not in current topology",
                group_name,
            )
            return
        target_metadata = raw_value if isinstance(raw_value, dict) else {}
        actual_value = (
            target_metadata.get("actual_kwh") if target_metadata else raw_value
        )
        try:
            actual_kwh = float(actual_value)
        except (TypeError, ValueError):
            self._record_panel_group_training_diagnostic(
                "group_rejections_invalid_value", date_str=date_str
            )
            _LOGGER.warning(
                "Skipping panel group training target '%s': invalid value %r",
                group_name,
                actual_value,
            )
            return
        active_topology_by_name = (
            getattr(self, "_active_group_topology_by_name", {}) or {}
        )
        current_topology = active_topology_by_name.get(group_name, {})
        stored_lineage_uid = (
            target_metadata.get("group_lineage_uid") if target_metadata else None
        )
        current_lineage_uid = current_topology.get("group_lineage_uid")
        if (
            stored_lineage_uid
            and current_lineage_uid
            and (stored_lineage_uid != current_lineage_uid)
        ):
            self._record_panel_group_training_diagnostic(
                "group_rejections_incompatible_lineage", date_str=date_str
            )
            _LOGGER.debug(
                "Skipping panel group training target '%s': incompatible topology lineage",
                group_name,
            )
            return
        stored_power_wp = (
            target_metadata.get("power_wp_at_prediction") if target_metadata else None
        )
        current_power_wp = (
            current_topology.get("power_wp")
            or _get_group_capacity(current_group) * 1000.0
        )
        try:
            stored_capacity_wp = float(stored_power_wp) if stored_power_wp else None
            current_capacity_wp = float(current_power_wp) if current_power_wp else None
        except (TypeError, ValueError):
            stored_capacity_wp = None
            current_capacity_wp = None
        if (
            stored_lineage_uid
            and current_lineage_uid
            and (stored_lineage_uid == current_lineage_uid)
            and stored_capacity_wp
            and current_capacity_wp
            and (
                abs(stored_capacity_wp - current_capacity_wp)
                / max(current_capacity_wp, 1.0)
                > 0.05
            )
        ):
            actual_kwh *= current_capacity_wp / stored_capacity_wp
        return actual_kwh

    def _validate_panel_group_actuals_for_training(
        self,
        panel_group_actuals: Any,
        actual_kwh: float,
        date_str: Optional[str] = None,
    ) -> Dict[str, float]:
        """Return only group actuals that are safe as training targets."""
        validation_context = {
            "legacy_unknown_topology_rejections": 0,
            "legacy_partial_topology_fallback": False,
            "current_rejections": 0,
            "residual_completed_targets": False,
            "residual_completed_group_names": set(),
        }
        self._last_panel_group_validation_context = validation_context
        if not panel_group_actuals or not isinstance(panel_group_actuals, dict):
            validation_context["legacy_partial_topology_fallback"] = (
                self._is_legacy_partial_group_training_context(
                    panel_group_actuals,
                    {},
                    [
                        configured_group.get("name", "")
                        for configured_group in self.panel_groups
                        if configured_group.get("name")
                    ],
                    date_str,
                )
            )
            return {}
        groups_by_name = {
            configured_group.get("name", ""): configured_group
            for configured_group in self.panel_groups
        }
        validated_actuals = {}
        for actual_group_name, raw_target in panel_group_actuals.items():
            panel_group = groups_by_name.get(actual_group_name, {})
            normalized_actual_kwh = self._normalise_group_training_target(
                actual_group_name, raw_target, panel_group, date_str=date_str
            )
            if normalized_actual_kwh is None:
                validation_context["current_rejections"] += 1
                continue
            maximum_group_kwh = _max_group_training_kwh(
                panel_group, self._max_kwh_per_hour
            )
            if (
                not math.isfinite(normalized_actual_kwh)
                or normalized_actual_kwh < -0.001
                or normalized_actual_kwh > maximum_group_kwh
            ):
                target_metadata = raw_target if isinstance(raw_target, dict) else {}
                current_topology = (
                    getattr(self, "_active_group_topology_by_name", {}) or {}
                ).get(actual_group_name, {})
                if self._is_legacy_unknown_topology_target(
                    target_metadata, current_topology, date_str
                ):
                    validation_context["legacy_unknown_topology_rejections"] += 1
                    self._record_panel_group_training_diagnostic(
                        "group_rejections_legacy_unknown_topology", date_str=date_str
                    )
                    _LOGGER.debug(
                        "Skipping legacy panel group training target '%s' for %s: %.4f kWh exceeds current valid range 0..%.1f kWh; historical topology metadata is missing",
                        actual_group_name,
                        date_str or "unknown date",
                        normalized_actual_kwh,
                        maximum_group_kwh,
                    )
                    continue
                validation_context["current_rejections"] += 1
                self._record_panel_group_training_diagnostic(
                    "group_rejections_out_of_range", date_str=date_str
                )
                _LOGGER.info(
                    "Skipping panel group training target '%s': %.4f kWh exceeds valid range 0..%.1f kWh",
                    actual_group_name,
                    normalized_actual_kwh,
                    maximum_group_kwh,
                )
                continue
            validated_actuals[actual_group_name] = max(0.0, normalized_actual_kwh)
            continue
        if not validated_actuals:
            return {}
        actual_tolerance_kwh = max(0.25, actual_kwh * 0.5)
        validated_total_kwh = sum(validated_actuals.values())
        expected_group_names = [
            configured_group.get("name", "")
            for configured_group in self.panel_groups
            if configured_group.get("name")
        ]
        complete_targets = bool(expected_group_names) and all(
            (group_name in validated_actuals for group_name in expected_group_names)
        )
        validation_context["legacy_partial_topology_fallback"] = (
            self._is_legacy_partial_group_training_context(
                panel_group_actuals, validated_actuals, expected_group_names, date_str
            )
        )
        if (
            complete_targets
            and abs(validated_total_kwh - actual_kwh) > actual_tolerance_kwh
        ):
            validation_context["current_rejections"] += 1
            self._record_panel_group_training_diagnostic(
                "hour_rejections_sum_mismatch", date_str=date_str, fallback=True
            )
            _LOGGER.info(
                "Ignoring panel group training targets for this hour: group sum %.4f kWh mismatches total %.4f kWh; training continues with safe fallback targets",
                validated_total_kwh,
                actual_kwh,
            )
            return {}
        if validated_total_kwh > actual_kwh + actual_tolerance_kwh:
            validation_context["current_rejections"] += 1
            self._record_panel_group_training_diagnostic(
                "hour_rejections_partial_sum_exceeds_total",
                date_str=date_str,
                fallback=True,
            )
            _LOGGER.info(
                "Ignoring panel group training targets for this hour: partial group sum %.4f kWh exceeds total %.4f kWh; training continues with safe fallback targets",
                validated_total_kwh,
                actual_kwh,
            )
            return {}
        if not complete_targets:
            if expected_group_names:
                missing_groups = [
                    panel_group
                    for panel_group in self.panel_groups
                    if panel_group.get("name", "") in expected_group_names
                    if panel_group.get("name", "") not in validated_actuals
                ]
                if missing_groups:
                    residual_kwh = max(0.0, actual_kwh - validated_total_kwh)
                    missing_capacity_kwp = sum(
                        (
                            max(0.0, _get_group_capacity(panel_group))
                            for panel_group in missing_groups
                        )
                    )
                    if missing_capacity_kwp <= 0:
                        missing_capacity_kwp = float(len(missing_groups))
                    residual_targets = {}
                    for panel_group in missing_groups:
                        missing_group_name = panel_group.get("name", "")
                        group_capacity_kwp = max(0.0, _get_group_capacity(panel_group))
                        if missing_capacity_kwp > 0 and group_capacity_kwp > 0:
                            capacity_ratio = group_capacity_kwp / missing_capacity_kwp
                        else:
                            capacity_ratio = 1.0 / len(missing_groups)
                        residual_target_kwh = residual_kwh * capacity_ratio
                        maximum_group_kwh = _max_group_training_kwh(
                            panel_group, self._max_kwh_per_hour
                        )
                        if (
                            not math.isfinite(residual_target_kwh)
                            or residual_target_kwh < -0.001
                            or residual_target_kwh > maximum_group_kwh
                        ):
                            validation_context["current_rejections"] += 1
                            self._record_panel_group_training_diagnostic(
                                "group_rejections_out_of_range", date_str=date_str
                            )
                            return {}
                        residual_targets[missing_group_name] = max(
                            0.0, residual_target_kwh
                        )
                    validated_actuals.update(residual_targets)
                    validation_context["residual_completed_targets"] = True
                    validation_context["residual_completed_group_names"].update(
                        residual_targets
                    )
        return validated_actuals

    def _process_hour_data_for_training(
        self,
        hour_data: Dict[str, Any],
        cutoff: Any,
        today: Any,
        X_sequences: List,
        y_targets: List,
        training_days: set[str],
        predictions_by_id: Dict[str, Dict[str, Any]],
        provenance_rows: List[Dict[str, Any]],
    ) -> None:
        """Process single hour data for Multi-Output training @zara"""
        if False:
            __assert_armored__ = None
        date_str = hour_data.get("target_date")
        if not date_str:
            return
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if target_date < cutoff or target_date >= today:
                return
        except ValueError:
            return
        target_hour = hour_data.get("target_hour")
        if target_hour is None:
            return
        actual_kwh = hour_data.get("actual_kwh")
        panel_group_actuals = hour_data.get("panel_group_actuals")
        if actual_kwh is None or actual_kwh <= 0:
            return None
        self._last_panel_group_validation_context = {}
        validated_actuals = self._validate_panel_group_actuals_for_training(
            panel_group_actuals, actual_kwh, date_str=date_str
        )
        excluded, exclusion_reason = should_exclude_hour_from_learning(hour_data)
        if excluded:
            return
        target_datetime = datetime.strptime(date_str, "%Y-%m-%d")
        previous_date = (target_datetime - timedelta(days=1)).strftime("%Y-%m-%d")
        previous_day_kwh = _sum_clean_actuals_for_date(predictions_by_id, previous_date)
        previous_hour_key = f"{previous_date}_{target_hour:02d}"
        previous_hour_prediction = predictions_by_id.get(previous_hour_key)
        previous_hour_kwh = _clean_actual_kwh(previous_hour_prediction)
        current_cloud_values = []
        previous_cloud_values = []
        for daylight_hour in range(6, 20):
            current_hour_key = f"{date_str}_{daylight_hour:02d}"
            current_hour_prediction = predictions_by_id.get(current_hour_key)
            if current_hour_prediction:
                weather_record = (
                    current_hour_prediction.get("weather_actual")
                    or current_hour_prediction.get("weather_corrected")
                    or current_hour_prediction.get("weather_forecast")
                    or {}
                )
                cloud_percent = weather_record.get(
                    "clouds", weather_record.get("cloud_cover")
                )
                if cloud_percent is not None:
                    current_cloud_values.append(float(cloud_percent))
            previous_daylight_hour_key = f"{previous_date}_{daylight_hour:02d}"
            previous_daylight_prediction = predictions_by_id.get(
                previous_daylight_hour_key
            )
            if not previous_daylight_prediction:
                continue
            weather_record = (
                previous_daylight_prediction.get("weather_actual")
                or previous_daylight_prediction.get("weather_corrected")
                or previous_daylight_prediction.get("weather_forecast")
                or {}
            )
            cloud_percent = weather_record.get(
                "clouds", weather_record.get("cloud_cover")
            )
            if cloud_percent is None:
                continue
            previous_cloud_values.append(float(cloud_percent))
            continue
        if current_cloud_values and previous_cloud_values:
            cloud_trend = (
                sum(current_cloud_values) / len(current_cloud_values)
                - sum(previous_cloud_values) / len(previous_cloud_values)
            ) / 100.0
            cloud_trend = max(-1.0, min(1.0, cloud_trend))
        else:
            cloud_trend = 0.0
        current_pressure_values = []
        previous_pressure_values = []
        for daylight_hour in range(6, 20):
            for day_key, pressure_values in (
                (date_str, current_pressure_values),
                (previous_date, previous_pressure_values),
            ):
                lookup_key = f"{day_key}_{daylight_hour:02d}"
                lookup_prediction = predictions_by_id.get(lookup_key)
                if not lookup_prediction:
                    continue
                weather_record = (
                    lookup_prediction.get("weather_actual")
                    or lookup_prediction.get("weather_corrected")
                    or lookup_prediction.get("weather_forecast")
                    or {}
                )
                pressure_hpa = weather_record.get("pressure")
                if pressure_hpa is not None:
                    pressure_values.append(float(pressure_hpa))
        current_mean_pressure = (
            sum(current_pressure_values) / len(current_pressure_values)
            if current_pressure_values
            else 1013.0
        )
        previous_mean_pressure = (
            sum(previous_pressure_values) / len(previous_pressure_values)
            if previous_pressure_values
            else 1013.0
        )
        pressure_trend = current_mean_pressure - previous_mean_pressure
        sequence = []
        missing_sequence_hours = 0
        for hour_offset in range(-23, 1):
            sequence_moment = target_datetime.replace(hour=target_hour) + timedelta(
                hours=hour_offset
            )
            sequence_date = sequence_moment.strftime("%Y-%m-%d")
            sequence_hour = sequence_moment.hour
            prediction_key = f"{sequence_date}_{sequence_hour:02d}"
            sequence_prediction = predictions_by_id.get(prediction_key)
            if sequence_prediction and hour_offset < 0:
                sequence_weather = sequence_prediction.get("weather_corrected") or {}
                sequence_astronomy = sequence_prediction.get("astronomy") or {}
                sequence_previous_date = (sequence_moment - timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
                sequence_previous_day_kwh = _sum_clean_actuals_for_date(
                    predictions_by_id, sequence_previous_date
                )
                sequence_previous_hour_key = (
                    f"{sequence_previous_date}_{sequence_hour:02d}"
                )
                sequence_previous_hour_prediction = predictions_by_id.get(
                    sequence_previous_hour_key
                )
                sequence_previous_hour_kwh = _clean_actual_kwh(
                    sequence_previous_hour_prediction
                )
                feature_record = {
                    "target_hour": sequence_hour,
                    "target_day_of_year": sequence_moment.timetuple().tm_yday,
                    "target_month": sequence_moment.month,
                    "weather_corrected": {
                        "temperature": sequence_weather.get(
                            "temperature", sequence_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": sequence_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": sequence_weather.get(
                            "wind", sequence_weather.get("wind_speed", 3)
                        ),
                        "humidity": sequence_weather.get(
                            "humidity", sequence_weather.get("humidity_percent", 70)
                        ),
                        "rain": sequence_weather.get(
                            "rain", sequence_weather.get("precipitation", 0)
                        ),
                        "clouds": sequence_weather.get(
                            "clouds", sequence_weather.get("cloud_cover", 50)
                        ),
                        "dni": sequence_weather.get(
                            "dni", sequence_weather.get("direct_radiation", 0)
                        ),
                        "pressure": sequence_weather.get("pressure", 1013.0),
                        "diffuse_radiation": sequence_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": sequence_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": sequence_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": sequence_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": sequence_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": sequence_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": sequence_astronomy.get(
                            "daylight_hours", 12.0
                        ),
                        "day_progress_ratio": sequence_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": sequence_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": sequence_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": sequence_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": sequence_previous_day_kwh,
                    "production_same_hour_yesterday": sequence_previous_hour_kwh,
                    "cloud_delta": cloud_trend,
                    "pressure_tendency_24h": pressure_trend,
                }
            else:
                current_weather = hour_data.get("weather_corrected", {}) or {}
                current_astronomy = hour_data.get("astronomy", {}) or {}
                feature_record = {
                    "target_hour": target_hour,
                    "target_day_of_year": target_datetime.timetuple().tm_yday,
                    "target_month": target_datetime.month,
                    "weather_corrected": {
                        "temperature": current_weather.get(
                            "temperature", current_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": current_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": current_weather.get(
                            "wind", current_weather.get("wind_speed", 3)
                        ),
                        "humidity": current_weather.get(
                            "humidity", current_weather.get("humidity_percent", 70)
                        ),
                        "rain": current_weather.get(
                            "rain", current_weather.get("precipitation", 0)
                        ),
                        "clouds": current_weather.get(
                            "clouds", current_weather.get("cloud_cover", 50)
                        ),
                        "dni": current_weather.get(
                            "dni", current_weather.get("direct_radiation", 0)
                        ),
                        "pressure": current_weather.get("pressure", 1013.0),
                        "diffuse_radiation": current_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": current_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": current_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": current_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": current_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": current_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": current_astronomy.get("daylight_hours", 12.0),
                        "day_progress_ratio": current_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": current_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": current_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": current_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": previous_day_kwh,
                    "production_same_hour_yesterday": previous_hour_kwh,
                    "cloud_delta": cloud_trend,
                    "pressure_tendency_24h": pressure_trend,
                }
            if self.panel_groups:
                features = self.feature_engineer.extract_combined_for_multi_output(
                    feature_record, self.panel_groups
                )
            else:
                features = self.feature_engineer.extract_base_features(feature_record)
            if features is None:
                missing_sequence_hours += 1
                feature_count = calculate_feature_count(self.num_groups)
                features = [0.0] * feature_count
            sequence.append(features)
            continue
        if missing_sequence_hours > 12:
            _LOGGER.debug(
                "Skipping training sample %s_%02d: %d/24 timesteps had null features",
                date_str,
                target_hour,
                missing_sequence_hours,
            )
            return
        target_source = "single_total"
        validation_context = (
            getattr(self, "_last_panel_group_validation_context", {}) or {}
        )
        if self.panel_groups:
            if validated_actuals and isinstance(validated_actuals, dict):
                all_targets_available = all(
                    (
                        panel_group.get("name", "") in validated_actuals
                        for panel_group in self.panel_groups
                    )
                )
                if all_targets_available:
                    target_source = (
                        "residual_completed_groups"
                        if validation_context.get("residual_completed_targets")
                        else "real_groups"
                    )
                    normalized_targets = []
                    for panel_group in self.panel_groups:
                        measured_group_kwh = validated_actuals[
                            panel_group.get("name", "")
                        ]
                        normalized_targets.append(
                            float(measured_group_kwh)
                            / _group_output_scale(panel_group, self._max_kwh_per_hour)
                        )
                else:
                    target_source = "mixed_fallback"
                    normalized_targets = []
                    for panel_group in self.panel_groups:
                        group_name = panel_group.get("name", "")
                        if group_name in validated_actuals:
                            normalized_targets.append(
                                float(validated_actuals[group_name])
                                / _group_output_scale(
                                    panel_group, self._max_kwh_per_hour
                                )
                            )
                            continue
                        donor_group = _find_training_donor(
                            panel_group, self.panel_groups, validated_actuals
                        )
                        if donor_group:
                            donor_kwh = validated_actuals[donor_group.get("name", "")]
                            group_capacity_kwp = _get_group_capacity(panel_group)
                            donor_capacity_kwp = _get_group_capacity(donor_group)
                            donor_capacity_ratio = (
                                group_capacity_kwp / donor_capacity_kwp
                                if donor_capacity_kwp > 0
                                else 1.0
                            )
                            normalized_targets.append(
                                float(donor_kwh)
                                * donor_capacity_ratio
                                / _group_output_scale(
                                    panel_group, self._max_kwh_per_hour
                                )
                            )
                            continue
                        capacity_kwp = _get_group_capacity(panel_group)
                        capacity_ratio = (
                            capacity_kwp / self.total_capacity
                            if self.total_capacity > 0
                            else 1.0 / self.num_groups
                        )
                        normalized_targets.append(
                            actual_kwh
                            * capacity_ratio
                            / _group_output_scale(panel_group, self._max_kwh_per_hour)
                        )
                        continue
            else:
                target_source = "capacity_fallback"
                normalized_targets = []
                for panel_group in self.panel_groups:
                    capacity_kwp = _get_group_capacity(panel_group)
                    capacity_ratio = (
                        capacity_kwp / self.total_capacity
                        if self.total_capacity > 0
                        else 1.0 / self.num_groups
                    )
                    normalized_targets.append(
                        actual_kwh
                        * capacity_ratio
                        / _group_output_scale(panel_group, self._max_kwh_per_hour)
                    )
        else:
            normalized_targets = [actual_kwh / self._max_kwh_per_hour]
        sample_index = len(X_sequences)
        X_sequences.append(sequence)
        y_targets.append(normalized_targets)
        target_anchor = f"{date_str}T{int(target_hour):02d}:00:00"
        legacy_fallback = bool(
            validation_context.get("legacy_unknown_topology_rejections", 0)
            or validation_context.get("legacy_partial_topology_fallback")
        )
        residual_group_names = set(
            validation_context.get("residual_completed_group_names", set())
        )
        for target_index, output_target in enumerate(normalized_targets):
            panel_group = self.panel_groups[target_index] if self.panel_groups else {}
            output_group_name = panel_group.get("name") if panel_group else None
            target_topology = (
                panel_group_actuals.get(output_group_name, {})
                if isinstance(panel_group_actuals, dict) and output_group_name
                else {}
            )
            if target_source == "real_groups":
                target_provenance = "direct_measured"
            elif target_source == "residual_completed_groups":
                target_provenance = (
                    "residual_completed"
                    if output_group_name in residual_group_names
                    else "direct_measured"
                )
            elif target_source == "capacity_fallback":
                target_provenance = "capacity_imputed"
            elif target_source == "mixed_fallback":
                if output_group_name in validated_actuals:
                    target_provenance = "direct_measured"
                elif _find_training_donor(
                    panel_group, self.panel_groups, validated_actuals
                ):
                    target_provenance = "donor_imputed"
                else:
                    target_provenance = "capacity_imputed"
            else:
                target_provenance = "direct_measured"
            if legacy_fallback and target_provenance != "direct_measured":
                target_provenance = "legacy_fallback"
            provenance_rows.append(
                {
                    "sample_index": sample_index,
                    "output_index": target_index,
                    "forecast_prediction_id": hour_data.get("prediction_id")
                    or f"{date_str}_{int(target_hour):02d}",
                    "forecast_anchor": target_anchor,
                    "target_prediction_id": hour_data.get("prediction_id")
                    or f"{date_str}_{int(target_hour):02d}",
                    "target_anchor": target_anchor,
                    "prediction_lineage_id": hour_data.get("prediction_id"),
                    "group_lineage_id": (
                        target_topology.get("group_lineage_uid")
                        if isinstance(target_topology, dict)
                        else None
                    ),
                    "group_name": output_group_name,
                    "target_value": float(output_target),
                    "feature_digest": canonical_stable_hash(sequence),
                    "provenance_kind": target_provenance,
                }
            )
            continue
        if self.panel_groups:
            self._record_panel_group_training_diagnostic(
                "samples_total", date_str=date_str
            )
            if panel_group_actuals and isinstance(panel_group_actuals, dict):
                self._record_panel_group_training_diagnostic(
                    "samples_with_group_input", date_str=date_str
                )
            if target_source == "real_groups":
                self._record_panel_group_training_diagnostic(
                    "samples_with_real_group_targets", date_str=date_str
                )
            elif target_source == "residual_completed_groups":
                self._record_panel_group_training_diagnostic(
                    "samples_with_residual_group_targets", date_str=date_str
                )
            elif target_source == "mixed_fallback":
                if (
                    validation_context.get("legacy_unknown_topology_rejections", 0) > 0
                    and validation_context.get("current_rejections", 0) == 0
                ):
                    self._record_panel_group_training_diagnostic(
                        "samples_with_legacy_topology_fallback",
                        date_str=date_str,
                        fallback=True,
                        legacy=True,
                    )
                elif validation_context.get("legacy_partial_topology_fallback"):
                    self._record_panel_group_training_diagnostic(
                        "samples_with_legacy_partial_fallback",
                        date_str=date_str,
                        fallback=True,
                        legacy=True,
                    )
                else:
                    self._record_panel_group_training_diagnostic(
                        "samples_with_mixed_fallback", date_str=date_str, fallback=True
                    )
            elif target_source == "capacity_fallback":
                if (
                    validation_context.get("legacy_unknown_topology_rejections", 0) > 0
                    and validation_context.get("current_rejections", 0) == 0
                ):
                    self._record_panel_group_training_diagnostic(
                        "samples_with_legacy_topology_fallback",
                        date_str=date_str,
                        fallback=True,
                        legacy=True,
                    )
                elif validation_context.get("legacy_partial_topology_fallback"):
                    self._record_panel_group_training_diagnostic(
                        "samples_with_legacy_partial_fallback",
                        date_str=date_str,
                        fallback=True,
                        legacy=True,
                    )
                else:
                    self._record_panel_group_training_diagnostic(
                        "samples_with_capacity_fallback_only",
                        date_str=date_str,
                        fallback=True,
                    )
        training_days.add(date_str)

    async def _build_temporal_sequence(
        self,
        target_hour: int,
        target_date: str,
        weather_data: Dict[str, Any],
        astronomy_data: Dict[str, Any],
        all_predictions: List[Dict[str, Any]],
    ) -> Tuple[List[List[float]], float, float]:
        """Build a real 24-hour temporal sequence for LSTM prediction @zara"""
        try:
            target_datetime = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            target_datetime = SafeDateTimeUtil.now()
            target_date = target_datetime.strftime("%Y-%m-%d")
        predictions_by_id = {}
        for lookup_prediction in all_predictions:
            lookup_key = lookup_prediction.get("id")
            if lookup_key:
                predictions_by_id[lookup_key] = lookup_prediction
        previous_date = (target_datetime - timedelta(days=1)).strftime("%Y-%m-%d")
        previous_day_kwh = _sum_clean_actuals_for_date(predictions_by_id, previous_date)
        previous_hour_key = f"{previous_date}_{target_hour:02d}"
        previous_hour_prediction = predictions_by_id.get(previous_hour_key)
        previous_hour_kwh = _clean_actual_kwh(previous_hour_prediction)
        current_cloud_percent = float(weather_data.get("clouds", 50))
        current_pressure_hpa = float(weather_data.get("pressure", 1013.0))
        previous_cloud_values = []
        previous_pressure_values = []
        for daylight_hour in range(6, 20):
            lookup_key = f"{previous_date}_{daylight_hour:02d}"
            lookup_prediction = predictions_by_id.get(lookup_key)
            if lookup_prediction:
                weather_record = (
                    lookup_prediction.get("weather_actual")
                    or lookup_prediction.get("weather_corrected")
                    or lookup_prediction.get("weather_forecast")
                    or {}
                )
                cloud_percent = weather_record.get(
                    "clouds", weather_record.get("cloud_cover")
                )
                if cloud_percent is not None:
                    previous_cloud_values.append(float(cloud_percent))
                pressure_hpa = weather_record.get("pressure")
                if pressure_hpa is not None:
                    previous_pressure_values.append(float(pressure_hpa))
        previous_mean_pressure = (
            sum(previous_pressure_values) / len(previous_pressure_values)
            if previous_pressure_values
            else 1013.0
        )
        pressure_trend = current_pressure_hpa - previous_mean_pressure
        previous_mean_clouds = None
        if previous_cloud_values:
            previous_mean_clouds = sum(previous_cloud_values) / len(
                previous_cloud_values
            )
            cloud_delta = current_cloud_percent - previous_mean_clouds
            if cloud_delta > 20:
                weather_adjustment = max(0.3, 1.0 - (cloud_delta - 20) / 80.0)
                if abs(pressure_trend) < 3.0:
                    weather_adjustment = max(weather_adjustment, 0.7)
                previous_day_kwh *= weather_adjustment
                previous_hour_kwh *= weather_adjustment
        cloud_trend = (
            (current_cloud_percent - previous_mean_clouds) / 100.0
            if previous_mean_clouds is not None
            else 0.0
        )
        cloud_trend = max(-1.0, min(1.0, cloud_trend))
        sequence = []
        for hour_offset in range(-23, 1):
            sequence_moment = target_datetime.replace(hour=target_hour) + timedelta(
                hours=hour_offset
            )
            sequence_date = sequence_moment.strftime("%Y-%m-%d")
            sequence_hour = sequence_moment.hour
            prediction_key = f"{sequence_date}_{sequence_hour:02d}"
            sequence_prediction = predictions_by_id.get(prediction_key)
            if sequence_prediction and hour_offset < 0:
                sequence_weather = (
                    sequence_prediction.get("weather_corrected")
                    or sequence_prediction.get("weather_forecast")
                    or {}
                )
                sequence_astronomy = sequence_prediction.get("astronomy") or {}
                sequence_previous_date = (sequence_moment - timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
                sequence_previous_day_kwh = _sum_clean_actuals_for_date(
                    predictions_by_id, sequence_previous_date
                )
                sequence_previous_hour_key = (
                    f"{sequence_previous_date}_{sequence_hour:02d}"
                )
                sequence_previous_hour_prediction = predictions_by_id.get(
                    sequence_previous_hour_key
                )
                sequence_previous_hour_kwh = _clean_actual_kwh(
                    sequence_previous_hour_prediction
                )
                feature_record = {
                    "target_hour": sequence_hour,
                    "target_day_of_year": sequence_moment.timetuple().tm_yday,
                    "target_month": sequence_moment.month,
                    "weather_corrected": {
                        "temperature": sequence_weather.get(
                            "temperature", sequence_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": sequence_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": sequence_weather.get(
                            "wind", sequence_weather.get("wind_speed", 3)
                        ),
                        "humidity": sequence_weather.get(
                            "humidity", sequence_weather.get("humidity_percent", 70)
                        ),
                        "rain": sequence_weather.get(
                            "rain", sequence_weather.get("precipitation", 0)
                        ),
                        "clouds": sequence_weather.get(
                            "clouds", sequence_weather.get("cloud_cover", 50)
                        ),
                        "dni": sequence_weather.get(
                            "dni", sequence_weather.get("direct_radiation", 0)
                        ),
                        "pressure": sequence_weather.get("pressure", 1013.0),
                        "diffuse_radiation": sequence_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": sequence_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": sequence_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": sequence_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": sequence_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": sequence_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": sequence_astronomy.get(
                            "daylight_hours", 12.0
                        ),
                        "day_progress_ratio": sequence_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": sequence_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": sequence_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": sequence_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": sequence_previous_day_kwh,
                    "production_same_hour_yesterday": sequence_previous_hour_kwh,
                    "cloud_delta": cloud_trend,
                    "pressure_tendency_24h": pressure_trend,
                }
            else:
                feature_record = {
                    "target_hour": sequence_hour if hour_offset < 0 else target_hour,
                    "target_day_of_year": (
                        sequence_moment.timetuple().tm_yday
                        if hour_offset < 0
                        else target_datetime.timetuple().tm_yday
                    ),
                    "target_month": (
                        sequence_moment.month
                        if hour_offset < 0
                        else target_datetime.month
                    ),
                    "weather_corrected": weather_data,
                    "astronomy": astronomy_data,
                    "production_yesterday": previous_day_kwh,
                    "production_same_hour_yesterday": previous_hour_kwh,
                    "cloud_delta": cloud_trend,
                    "pressure_tendency_24h": pressure_trend,
                }
            if self.panel_groups:
                features = self.feature_engineer.extract_combined_for_multi_output(
                    feature_record, self.panel_groups
                )
            else:
                features = self.feature_engineer.extract_base_features(feature_record)
            if features is None:
                feature_count = calculate_feature_count(self.num_groups)
                features = [0.0] * feature_count
            sequence.append(features)
            continue
        return (sequence, previous_day_kwh, previous_hour_kwh)

    async def _load_predictions_for_sequence(self) -> List[Dict[str, Any]]:
        """Load recent predictions for building temporal sequences @zara"""
        if self._frozen_prediction_history is not None:
            return self._frozen_prediction_history
        try:
            prediction_rows = await self.db_manager.fetchall(
                "SELECT hp.target_date, hp.target_hour, hp.actual_kwh, hp.prediction_id,\n                          pw.temperature, pw.solar_radiation_wm2, pw.wind, pw.humidity,\n                          pw.rain, pw.clouds,\n                          pa.sun_elevation_deg, pa.theoretical_max_kwh, pa.clear_sky_radiation_wm2,\n                          pw.pressure,\n                          pw.diffuse_radiation,\n                          pw.direct_radiation,\n                          pa.sun_azimuth_deg,\n                          pa.daylight_hours,\n                          pa.day_progress_ratio,\n                          pa.hours_after_sunrise,\n                          pa.hours_before_sunset,\n                          pa.hours_since_solar_noon,\n                          hp.exclude_from_learning,\n                          hp.exclude_from_clean_evaluation,\n                          hp.mppt_throttled,\n                          hp.inverter_clipped,\n                          hp.has_weather_alert,\n                          hp.is_outlier,\n                          hp.weather_alert_type,\n                          hp.mppt_throttle_reason\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date >= date('now', '-7 days')\n                   ORDER BY hp.target_date, hp.target_hour"
            )
            predictions = []
            for row in prediction_rows:
                prediction_key = f"{row[0]}_{row[1]:02d}"
                predictions.append(
                    {
                        "id": prediction_key,
                        "target_date": row[0],
                        "target_hour": row[1],
                        "actual_kwh": row[2],
                        "weather_corrected": (
                            {
                                "temperature": row[4],
                                "solar_radiation_wm2": row[5],
                                "wind": row[6],
                                "humidity": row[7],
                                "rain": row[8],
                                "clouds": row[9],
                                "pressure": row[13],
                                "diffuse_radiation": row[14],
                                "dni": row[15],
                            }
                            if row[4] is not None
                            else None
                        ),
                        "astronomy": (
                            {
                                "sun_elevation_deg": row[10],
                                "theoretical_max_kwh": row[11],
                                "clear_sky_radiation_wm2": row[12],
                                "sun_azimuth_deg": row[16],
                                "daylight_hours": row[17],
                                "day_progress_ratio": row[18],
                                "hours_after_sunrise": row[19],
                                "hours_before_sunset": row[20],
                                "hours_since_solar_noon": row[21],
                            }
                            if row[10] is not None
                            else None
                        ),
                        "flags": _hourly_governance_flags(
                            exclude_from_learning=row[22],
                            exclude_from_clean_evaluation=row[23],
                            mppt_throttled=row[24],
                            inverter_clipped=row[25],
                            has_weather_alert=row[26],
                            is_outlier=row[27],
                            weather_alert_type=row[28],
                            mppt_throttle_reason=row[29],
                        ),
                    }
                )
                continue
            return predictions
        except Exception as error:
            _LOGGER.debug(f"Could not load predictions for sequence: {error}")
            return []

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
        mismatch_logger = _LOGGER.debug if quiet_mismatch else _LOGGER.warning
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
        ridge_loaded = False
        lstm_loaded = False
        saved_active_model = weights.get("active_model", "none")
        self._apply_artifact_output_scale(weights)
        ridge_artifact = weights.get("ridge")
        if ridge_artifact and self.ridge:
            try:
                saved_input_size = ridge_artifact.get("input_size", 17)
                saved_output_count = ridge_artifact.get("num_outputs", 1)
                saved_flat_size = ridge_artifact.get("flat_size", saved_input_size * 24)
                expected_flat_size = self.ridge.flat_size
                if (
                    saved_input_size == feature_count
                    and saved_output_count == num_outputs
                    and (saved_flat_size == expected_flat_size)
                ):
                    self.ridge.set_weights(
                        ridge_artifact,
                        strict=strict_active_model and saved_active_model == "ridge",
                    )
                    ridge_loaded = True
                    _LOGGER.debug(
                        f"Ridge weights loaded: {ridge_artifact.get('trained_samples', 0)} samples"
                    )
                else:
                    mismatch_logger(
                        f"Ridge architecture mismatch: saved input={saved_input_size}/flat_size={saved_flat_size}/outputs={saved_output_count}, needed input={feature_count}/flat_size={expected_flat_size}/outputs={num_outputs}"
                    )
            except Exception as error:
                _LOGGER.warning(f"Could not load Ridge weights: {error}")
        lstm_artifact = weights.get("lstm")
        if lstm_artifact and self.lstm:
            try:
                saved_input_size = lstm_artifact.get("input_size", 17)
                saved_output_count = lstm_artifact.get("num_outputs", 1)
                saved_hidden_size = lstm_artifact.get("hidden_size", 48)
                saved_second_hidden_size = lstm_artifact.get("hidden2", 24)
                saved_heads = lstm_artifact.get("num_heads", 4)
                if (
                    saved_input_size == feature_count
                    and saved_output_count == num_outputs
                    and (saved_hidden_size == self.lstm.hidden1)
                    and (saved_second_hidden_size == self.lstm.hidden2)
                    and (saved_heads == self.lstm.num_heads)
                ):
                    self.lstm.set_weights(lstm_artifact)
                    lstm_loaded = True
                    _LOGGER.debug(
                        f"LSTM weights loaded: {lstm_artifact.get('training_samples', 0)} samples"
                    )
                else:
                    mismatch_logger(
                        f"LSTM architecture mismatch: saved input={saved_input_size}/hidden=({saved_hidden_size},{saved_second_hidden_size})/heads={saved_heads}/outputs={saved_output_count}, needed input={feature_count}/hidden=({self.lstm.hidden1},{self.lstm.hidden2})/heads={self.lstm.num_heads}/outputs={num_outputs}. Retraining required."
                    )
            except Exception as error:
                _LOGGER.warning(f"Could not load LSTM weights: {error}")
        if strict_active_model and saved_active_model not in {"ridge", "tiny_lstm"}:
            raise ValueError("candidate declares an invalid active model")
        if strict_active_model:
            if (
                saved_active_model == "tiny_lstm"
                and (not lstm_loaded)
                or (saved_active_model == "ridge" and (not ridge_loaded))
            ):
                raise ValueError("candidate active model artifact could not be loaded")
        if saved_active_model == "tiny_lstm" and lstm_loaded:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif saved_active_model == "ridge" and ridge_loaded:
            self.active_model = "ridge"
            self.state = ModelState.READY
        elif not strict_active_model and lstm_loaded:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif not strict_active_model and ridge_loaded:
            self.active_model = "ridge"
            self.state = ModelState.READY
        else:
            self.active_model = "none"
            self.state = ModelState.UNTRAINED
            if not ridge_artifact and (not lstm_artifact):
                _LOGGER.info(
                    f"No promoted model artifacts available - serving physics until a candidate passes the promotion gate ({feature_count} features, {num_outputs} outputs)"
                )
            else:
                _LOGGER.warning(
                    f"No compatible weights found. Config has {feature_count} features, {num_outputs} outputs. Model needs retraining."
                )
        active_model_note = f"active={self.active_model}"
        rmse_note = f", RMSE={self.current_rmse:.3f}kWh" if self.current_rmse else ""
        if self.state == ModelState.READY:
            load_message = (
                "Serving model weights loaded"
                if not self.shadow_candidate_id
                else f"{self._instance_label()} weights loaded"
            )
            self._instance_log(
                f"{load_message}: {self.training_samples} samples, {active_model_note}, R²={self.current_accuracy:.3f}{rmse_note}"
                if self.current_accuracy
                else f"{load_message}: {self.training_samples} samples, {active_model_note}"
            )
            self._instance_log(
                "%s output scale: groups=%d global=%s",
                "Serving" if not self.shadow_candidate_id else self._instance_label(),
                len(self._artifact_group_scales),
                self._artifact_global_scale,
            )
        return None if True else None

    async def _save_weights_async(self):
        """Save model weights asynchronously to database @zara"""
        try:
            weight_artifact = {
                "version": "3.0",
                "active_model": self.active_model,
                "training_samples": self.training_samples,
                "last_trained": SafeDateTimeUtil.now().isoformat(),
                "accuracy": self.current_accuracy,
                "rmse": self.current_rmse,
                "output_scale": self._build_output_scale_payload(),
                "ridge": (
                    self.ridge.get_weights()
                    if self.ridge and self.ridge.is_trained
                    else None
                ),
                "lstm": self.lstm.get_weights() if self.lstm else None,
            }
            await self.db_manager.save_model_weights(weight_artifact)
            _LOGGER.info("Weights saved to database")
        except Exception as error:
            _LOGGER.error(f"Failed to save weights: {error}")
        return None if True else None

    async def _archive_current_model(self, season: str) -> None:
        """Archive current model weights for seasonal reuse. @zara V16.8.4"""
        source_year = SafeDateTimeUtil.now().year
        if self.lstm:
            try:
                weight_artifact = self.lstm.get_weights()
                await self.db_manager.save_seasonal_archive(
                    season=season,
                    archive_type="lstm",
                    weights_data=weight_artifact,
                    training_samples=self.training_samples,
                    accuracy=self.current_accuracy or 0.0,
                    rmse=self.current_rmse or 0.0,
                    source_year=source_year,
                )
            except Exception as error:
                _LOGGER.debug("LSTM seasonal archive failed: %s", error)
        if self.ridge and self.ridge.is_trained:
            try:
                weight_artifact = self.ridge.get_weights()
                await self.db_manager.save_seasonal_archive(
                    season=season,
                    archive_type="ridge",
                    weights_data=weight_artifact,
                    training_samples=self.training_samples,
                    accuracy=self.current_accuracy or 0.0,
                    rmse=self.current_rmse or 0.0,
                    source_year=source_year,
                )
            except Exception as error:
                _LOGGER.debug("Ridge seasonal archive failed: %s", error)
        _LOGGER.info("Model archived for season %s/%d", season, source_year)

    async def _load_seasonal_archive(self, season: str) -> Optional[Dict[str, Any]]:
        """Load archived model weights for seasonal warm-start. @zara V16.8.4"""
        archives = {}
        feature_count = calculate_feature_count(self.num_groups)
        output_count = self.num_groups if self.num_groups > 0 else 1
        for archive_type in ("lstm", "ridge"):
            try:
                archive_weights = await self.db_manager.load_seasonal_archive(
                    season, archive_type
                )
                if not archive_weights:
                    continue
                saved_input_size = archive_weights.get("input_size")
                saved_output_count = archive_weights.get("num_outputs")
                if (
                    saved_input_size != feature_count
                    or saved_output_count != output_count
                ):
                    _LOGGER.debug(
                        "Archive %s/%s architecture mismatch: input=%s/%s, outputs=%s/%s",
                        season,
                        archive_type,
                        saved_input_size,
                        feature_count,
                        saved_output_count,
                        output_count,
                    )
                    continue
                if archive_type == "lstm" and self.lstm:
                    archived_hidden_size = archive_weights.get("hidden_size", 48)
                    archived_second_hidden_size = archive_weights.get("hidden2", 24)
                    if (
                        archived_hidden_size != self.lstm.hidden1
                        or archived_second_hidden_size != self.lstm.hidden2
                    ):
                        _LOGGER.debug(
                            "Archive LSTM hidden mismatch: (%s,%s) vs (%s,%s)",
                            archived_hidden_size,
                            archived_second_hidden_size,
                            self.lstm.hidden1,
                            self.lstm.hidden2,
                        )
                        continue
                archives[archive_type] = archive_weights
            except Exception as error:
                _LOGGER.debug(
                    "Failed to load archive %s/%s: %s", season, archive_type, error
                )
                continue
            continue
        return archives if archives else None

    async def enable_attention(self, enable: bool = True) -> bool:
        """Attention is always active in V3.0 architecture @zara"""
        _LOGGER.info("Multi-head attention always enabled in V3.0")
        return True

    def get_model_info(self) -> Dict[str, Any]:
        """Get model information for diagnostics @zara"""
        model_info = {
            "state": self.state.value if self.state else "unknown",
            "training_samples": self.training_samples,
            "accuracy": self.current_accuracy,
            "rmse": self.current_rmse,
            "num_groups": self.num_groups,
            "total_capacity_kwp": self.total_capacity,
            "use_attention": self.use_attention,
            "last_training_time": (
                self.last_training_time.isoformat() if self.last_training_time else None
            ),
            "active_model": self.active_model,
        }
        if self.lstm:
            model_info["model_size_kb"] = self.lstm.get_model_size_kb()
            model_info["feature_count"] = self.lstm.input_size
            model_info["hidden_sizes"] = (self.lstm.hidden1, self.lstm.hidden2)
            model_info["num_layers"] = 2
            model_info["num_heads"] = self.lstm.num_heads
            model_info["num_outputs"] = self.lstm.num_outputs
        return model_info

    async def analyze_feature_importance(
        self, num_permutations: int = 5, progress_callback: Optional[callable] = None
    ) -> Optional[FeatureImportanceResult]:
        """Analyze which features help or hurt model performance @zara"""
        if not self.lstm or not self.feature_engineer:
            _LOGGER.warning("Cannot analyze features: model not initialized")
            return
        if self.state != ModelState.READY:
            _LOGGER.warning(
                f"Cannot analyze features: model state is {self.state.value}"
            )
            return
        _LOGGER.debug("Starting Feature Importance analysis...")
        try:
            X_sequences, y_targets, provenance_rows = (
                await self._prepare_training_data()
            )
            if len(X_sequences) < 20:
                _LOGGER.warning(
                    f"Not enough data for feature importance analysis: {len(X_sequences)} samples (need 20+)"
                )
                return
            import numpy as numpy_module

            sample_count = len(X_sequences)
            analysis_sample_count = max(20, int(sample_count * 0.2))
            sample_indices = numpy_module.random.permutation(sample_count)[
                :analysis_sample_count
            ]
            analysis_sequences = [
                X_sequences[sample_index] for sample_index in sample_indices
            ]
            analysis_targets = [
                y_targets[sample_index] for sample_index in sample_indices
            ]
            if self.panel_groups:
                feature_names = list(self.feature_engineer.BASE_FEATURE_NAMES)
                for sample_index, panel_group in enumerate(self.panel_groups):
                    group_name = panel_group.get("name", f"Group_{sample_index + 1}")
                    for group_feature_name in self.feature_engineer.GROUP_FEATURE_NAMES:
                        feature_names.append(f"{group_feature_name}_{group_name}")
            else:
                feature_names = list(self.feature_engineer.BASE_FEATURE_NAMES)
            analyzer = FeatureImportanceAnalyzer(
                self.db_manager, num_permutations=num_permutations
            )
            analysis_result = await analyzer.analyze(
                lstm=self.lstm,
                X_sequences=analysis_sequences,
                y_targets=analysis_targets,
                feature_names=feature_names,
                progress_callback=progress_callback,
            )
            if analysis_result.success:
                _LOGGER.debug(
                    f"Feature Importance complete: {len(analysis_result.helpful_features)} helpful, {len(analysis_result.harmful_features)} harmful, {len(analysis_result.neutral_features)} neutral"
                )
                self._last_feature_importance = analysis_result
            return analysis_result
        except Exception as error:
            _LOGGER.error(f"Feature importance analysis failed: {error}")
            return
