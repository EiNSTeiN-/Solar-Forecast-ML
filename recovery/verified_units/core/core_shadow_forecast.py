# Powered by Solar Forecast ML by Zara-Toorox; subject to the repository LICENSE.
# Entire module verified against recovered bytecode.
# Folded return edges, where retained, preserve verified shared compiler blocks.
from __future__ import annotations
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

MIN_PATTERN_SAMPLES = 7
MIN_PATTERN_CONFIDENCE = 0.5
MIN_LOCAL_OBSTRUCTION_PROBABILITY = 0.6
MIN_SEASONAL_SAMPLES = 3
MIN_SEASONAL_CONFIDENCE = 0.5
MAX_PHYSICS_SHADOW_LOSS_PERCENT = 45.0
MAX_SENSORLESS_PHYSICS_SHADOW_LOSS_PERCENT = 25.0
ACTIVE_PATTERN_TYPES = frozenset({"frequent", "occasional", "fixed_obstruction"})
LOCAL_ROOT_CAUSES = frozenset({"building_tree_obstruction", "possible_obstruction"})


@dataclass(frozen=True)
class ShadowForecastContext:
    scope: str
    pattern_type: str
    occurrence_probability: float
    conditional_loss_percent: float
    expected_loss_percent: float
    applied_loss_percent: float
    local_obstruction_probability: float
    confidence: float
    sample_count: int
    seasonal_match: bool
    eligible: bool
    reason: str

    @property
    def physics_factor(self) -> float:
        return max(0.0, 1.0 - self.applied_loss_percent / 100.0)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "physics_factor": self.physics_factor}


def build_shadow_forecast_context(
    hourly: Mapping[str, Any] | None, seasonal: Mapping[str, Any] | None = None, *, scope: str
) -> ShadowForecastContext:
    if not hourly:
        return _inactive_context(scope, "missing_hourly_pattern")
    pattern_type = str(hourly.get("pattern_type") or "unknown")
    sample_count = int(hourly.get("sample_count") or 0)
    clear_days = int(hourly.get("clear_days") or 0)
    confidence = _fraction(hourly.get("confidence"))
    local_obstruction_probability = _fraction(hourly.get("pct_building_tree"))
    occurrence_probability = _fraction(hourly.get("shadow_occurrence_rate"))
    conditional_loss_percent = _percent(hourly.get("avg_shadow_percent"))
    if pattern_type not in ACTIVE_PATTERN_TYPES:
        return _inactive_context(scope, "inactive_pattern_type", pattern_type=pattern_type)
    if sample_count < MIN_PATTERN_SAMPLES:
        return _inactive_context(scope, "insufficient_samples", pattern_type=pattern_type)
    if clear_days < 1:
        return _inactive_context(scope, "insufficient_clear_days", pattern_type=pattern_type)
    if confidence < MIN_PATTERN_CONFIDENCE:
        return _inactive_context(scope, "insufficient_confidence", pattern_type=pattern_type)
    if local_obstruction_probability < MIN_LOCAL_OBSTRUCTION_PROBABILITY:
        return _inactive_context(scope, "non_local_cause", pattern_type=pattern_type)
    seasonal_match = False
    if seasonal:
        dominant_cause = str(seasonal.get("dominant_cause") or "unknown")
        seasonal_samples = int(seasonal.get("sample_count") or 0)
        seasonal_confidence = _fraction(seasonal.get("confidence"))
        if (
            dominant_cause in LOCAL_ROOT_CAUSES
            and seasonal_samples >= MIN_SEASONAL_SAMPLES
            and (seasonal_confidence >= MIN_SEASONAL_CONFIDENCE)
        ):
            seasonal_weight = min(0.4, seasonal_confidence * 0.4)
            occurrence_probability = (
                occurrence_probability * (1.0 - seasonal_weight)
                + _fraction(seasonal.get("shadow_occurrence_rate")) * seasonal_weight
            )
            conditional_loss_percent = (
                conditional_loss_percent * (1.0 - seasonal_weight)
                + _percent(seasonal.get("avg_shadow_percent")) * seasonal_weight
            )
            seasonal_match = True
    expected_loss_percent = occurrence_probability * conditional_loss_percent
    loss_cap = MAX_PHYSICS_SHADOW_LOSS_PERCENT
    if str(hourly.get("observation_class") or "") == "sensorless_candidate":
        loss_cap = MAX_SENSORLESS_PHYSICS_SHADOW_LOSS_PERCENT
    applied_loss_percent = min(loss_cap, expected_loss_percent) * confidence
    return (
        (
            ShadowForecastContext(
                scope=scope,
                pattern_type=pattern_type,
                occurrence_probability=round(occurrence_probability, 4),
                conditional_loss_percent=round(conditional_loss_percent, 2),
                expected_loss_percent=round(expected_loss_percent, 2),
                applied_loss_percent=round(applied_loss_percent, 2),
                local_obstruction_probability=round(local_obstruction_probability, 4),
                confidence=round(confidence, 4),
                sample_count=sample_count,
                seasonal_match=seasonal_match,
                eligible=applied_loss_percent > 0.0,
                reason="eligible" if applied_loss_percent > 0.0 else "zero_expected_loss",
            )
            if True
            else None
        )
        if True
        else None
    )


def apply_shadow_to_calibrated_physics(
    *,
    calibrated_kwh: float,
    raw_kwh: float,
    global_calibration_factor: float,
    context: ShadowForecastContext,
) -> float:
    calibrated = max(0.0, float(calibrated_kwh))
    if not context.eligible:
        return calibrated
    raw = max(0.0, float(raw_kwh))
    calibration = max(0.0, min(2.0, float(global_calibration_factor)))
    shadow_adjusted = raw * calibration * context.physics_factor
    return min(calibrated, shadow_adjusted)


def _inactive_context(
    scope: str, reason: str, *, pattern_type: str = "unknown"
) -> ShadowForecastContext:
    return ShadowForecastContext(
        scope=scope,
        pattern_type=pattern_type,
        occurrence_probability=0.0,
        conditional_loss_percent=0.0,
        expected_loss_percent=0.0,
        applied_loss_percent=0.0,
        local_obstruction_probability=0.0,
        confidence=0.0,
        sample_count=0,
        seasonal_match=False,
        eligible=False,
        reason=reason,
    )


def _fraction(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _percent(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(100.0, number))
