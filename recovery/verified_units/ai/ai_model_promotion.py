# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Pure paired evaluation gate for non-serving model candidates."""
from __future__ import annotations
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

_CI_LEVEL = 0.95
_V2_FORECAST_CYCLES = ("post_midnight", "pre_sunrise")
_V2_BOOTSTRAP_ITERATIONS = 10000


@dataclass(frozen=True)
class FrozenPairedTotalStackSample:
    """One frozen total-stack outcome for a champion and candidate pair."""

    target_date: str
    target_hour: int
    actual: float
    champion_prediction: float
    candidate_prediction: float
    clean_eligible: bool
    forecast_anchor: str
    input_fingerprint: str
    policy_fingerprint: str
    weather_regime: str | None = None
    horizon: str | None = None


@dataclass(frozen=True)
class CriticalSlice:
    """An observed context that must not regress when adequately powered."""

    name: str
    weather_regime: str | None = None
    horizon: str | None = None
    min_independent_days: int | None = None
    allowed_mae_regression: float = 0.0


@dataclass(frozen=True)
class ModelPromotionEvaluationConfig:
    """Stable, serializable configuration for paired promotion evaluation."""

    min_independent_eligible_days: int = 7
    bootstrap_iterations: int = 2000
    bootstrap_seed: int = 17290
    critical_slice_min_independent_days: int = 3
    critical_slices: tuple[CriticalSlice, ...] = ()


@dataclass(frozen=True)
class ModelPromotionDecision:
    """Immutable decision data; ``to_dict`` yields JSON-serializable primitives."""

    go_no_go: str
    reason_codes: tuple[str, ...]
    dataset_fingerprint: str
    evaluation_config_hash: str
    eligible_hour_count: int
    eligible_independent_day_count: int
    overall_hourly: Mapping[str, Mapping[str, float]]
    daily_energy_mae: Mapping[str, float]
    per_day_paired_mae_deltas: tuple[Mapping[str, float | str], ...]
    candidate_minus_champion_mae_ci_95: tuple[float, float] | None
    critical_slice_results: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-serializable representation."""
        return {
            "go_no_go": self.go_no_go,
            "reason_codes": list(self.reason_codes),
            "dataset_fingerprint": self.dataset_fingerprint,
            "evaluation_config_hash": self.evaluation_config_hash,
            "eligible_hour_count": self.eligible_hour_count,
            "eligible_independent_day_count": self.eligible_independent_day_count,
            "overall_hourly": _thaw(self.overall_hourly),
            "daily_energy_mae": _thaw(self.daily_energy_mae),
            "per_day_paired_mae_deltas": _thaw(self.per_day_paired_mae_deltas),
            "candidate_minus_champion_mae_ci_95": (
                list(self.candidate_minus_champion_mae_ci_95)
                if self.candidate_minus_champion_mae_ci_95 is not None
                else None
            ),
            "critical_slice_results": _thaw(self.critical_slice_results),
        }


def evaluate_model_promotion(
    samples: Sequence[FrozenPairedTotalStackSample],
    config: ModelPromotionEvaluationConfig = ModelPromotionEvaluationConfig(),
) -> ModelPromotionDecision:
    """Evaluate frozen paired samples without mutating state or calling services.

    Invalid input is rejected rather than silently excluded.  Explicitly clean-
    ineligible samples remain fingerprinted but do not contribute to metrics.
    """
    _validate_config(config)
    frozen_samples = tuple(samples)
    normalized_samples = tuple(
        (_validate_and_normalize(sample) for sample in frozen_samples)
    )
    _reject_duplicate_pairs(normalized_samples)
    dataset_fingerprint = _stable_hash(normalized_samples)
    evaluation_config_hash = _stable_hash(config)
    eligible_samples = tuple(
        (sample for sample in normalized_samples if sample["clean_eligible"])
    )
    by_day = _group_by_day(eligible_samples)
    daily_deltas = _daily_deltas(by_day)
    confidence_interval = _bootstrap_ci(daily_deltas, config) if daily_deltas else None
    critical_slice_results = tuple(
        (
            _evaluate_slice(slice_spec, eligible_samples, config)
            for slice_spec in config.critical_slices
        )
    )
    reason_codes = []
    if len(by_day) < config.min_independent_eligible_days:
        reason_codes.append("INSUFFICIENT_ELIGIBLE_DAYS")
    if confidence_interval is None or confidence_interval[1] >= 0.0:
        reason_codes.append("CANDIDATE_NOT_SUPERIOR")
    if any((slice_result["regresses"] for slice_result in critical_slice_results)):
        reason_codes.append("CRITICAL_SLICE_REGRESSION")
    if any(
        (
            not slice_result["adequately_powered"]
            for slice_result in critical_slice_results
        )
    ):
        reason_codes.append("CRITICAL_SLICE_UNDERPOWERED")
    return ModelPromotionDecision(
        go_no_go="GO" if not reason_codes else "NO_GO",
        reason_codes=tuple(reason_codes),
        dataset_fingerprint=dataset_fingerprint,
        evaluation_config_hash=evaluation_config_hash,
        eligible_hour_count=len(eligible_samples),
        eligible_independent_day_count=len(by_day),
        overall_hourly=_freeze(_hourly_metrics(eligible_samples)),
        daily_energy_mae=_freeze(_daily_energy_mae(by_day)),
        per_day_paired_mae_deltas=tuple(
            (_freeze(daily_delta) for daily_delta in daily_deltas)
        ),
        candidate_minus_champion_mae_ci_95=confidence_interval,
        critical_slice_results=tuple(
            (_freeze(slice_result) for slice_result in critical_slice_results)
        ),
    )


evaluate_paired_model_promotion = evaluate_model_promotion


def _validate_config(config: ModelPromotionEvaluationConfig) -> None:
    if not isinstance(config, ModelPromotionEvaluationConfig):
        raise TypeError("config must be ModelPromotionEvaluationConfig")
    if (
        isinstance(config.min_independent_eligible_days, bool)
        or not isinstance(config.min_independent_eligible_days, int)
        or config.min_independent_eligible_days < 1
    ):
        raise ValueError("min_independent_eligible_days must be positive")
    if (
        isinstance(config.bootstrap_iterations, bool)
        or not isinstance(config.bootstrap_iterations, int)
        or config.bootstrap_iterations < 1
    ):
        raise ValueError("bootstrap_iterations must be positive")
    if not isinstance(config.bootstrap_seed, int) or isinstance(
        config.bootstrap_seed, bool
    ):
        raise ValueError("bootstrap_seed must be an integer")
    if (
        isinstance(config.critical_slice_min_independent_days, bool)
        or not isinstance(config.critical_slice_min_independent_days, int)
        or config.critical_slice_min_independent_days < 1
    ):
        raise ValueError("critical_slice_min_independent_days must be positive")
    seen_slice_names = set()
    for slice_spec in config.critical_slices:
        if not isinstance(slice_spec, CriticalSlice) or not slice_spec.name:
            raise ValueError("critical slices require a non-empty name")
        if slice_spec.name in seen_slice_names:
            raise ValueError("duplicate critical slice name")
        seen_slice_names.add(slice_spec.name)
        minimum_days = slice_spec.min_independent_days
        if minimum_days is not None:
            if (
                isinstance(minimum_days, bool)
                or not isinstance(minimum_days, int)
                or minimum_days < 1
            ):
                raise ValueError("critical slice minimum days must be positive")
        _finite_non_negative(
            slice_spec.allowed_mae_regression, "allowed_mae_regression"
        )


def _validate_and_normalize(sample: FrozenPairedTotalStackSample) -> dict[str, Any]:
    if not isinstance(sample, FrozenPairedTotalStackSample):
        raise TypeError("samples must be FrozenPairedTotalStackSample instances")
    try:
        target_date = date.fromisoformat(sample.target_date).isoformat()
    except (TypeError, ValueError) as validation_error:
        raise ValueError(
            "target_date must be an ISO calendar date"
        ) from validation_error
    if isinstance(sample.target_hour, bool) or not isinstance(sample.target_hour, int):
        raise ValueError("target_hour must be an integer")
    if not 0 <= sample.target_hour <= 23:
        raise ValueError("target_hour must be between 0 and 23")
    if not isinstance(sample.clean_eligible, bool):
        raise ValueError("clean_eligible is unknown")
    try:
        forecast_anchor = datetime.fromisoformat(sample.forecast_anchor)
    except (TypeError, ValueError) as validation_error:
        raise ValueError(
            "forecast_anchor must be an ISO datetime"
        ) from validation_error
    if forecast_anchor.tzinfo is None or forecast_anchor.utcoffset() is None:
        raise ValueError("forecast_anchor must include a timezone")
    for field_name, field_value in (
        ("input_fingerprint", sample.input_fingerprint),
        ("policy_fingerprint", sample.policy_fingerprint),
    ):
        if isinstance(field_value, str) and (not len(field_value) != 64):
            if not any(
                (hex_char not in "0123456789abcdef" for hex_char in field_value)
            ):
                continue
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    for field_name, field_value in (
        ("actual", sample.actual),
        ("champion_prediction", sample.champion_prediction),
        ("candidate_prediction", sample.candidate_prediction),
    ):
        _finite_non_negative(field_value, field_name)
    for field_name, field_value in (
        ("weather_regime", sample.weather_regime),
        ("horizon", sample.horizon),
    ):
        if field_value is None:
            continue
        if isinstance(field_value, str):
            if field_value:
                continue
        raise ValueError(f"{field_name} must be a non-empty string or None")
    return {
        "target_date": target_date,
        "target_hour": sample.target_hour,
        "actual": float(sample.actual),
        "champion_prediction": float(sample.champion_prediction),
        "candidate_prediction": float(sample.candidate_prediction),
        "clean_eligible": sample.clean_eligible,
        "forecast_anchor": forecast_anchor.isoformat(),
        "input_fingerprint": sample.input_fingerprint,
        "policy_fingerprint": sample.policy_fingerprint,
        "weather_regime": sample.weather_regime,
        "horizon": sample.horizon,
    }


def _finite_non_negative(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite non-negative number")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")


def _reject_duplicate_pairs(samples: Sequence[Mapping[str, Any]]) -> None:
    pair_keys = [(sample["target_date"], sample["target_hour"]) for sample in samples]
    if len(pair_keys) != len(set(pair_keys)):
        raise ValueError("duplicate or unpaired target date/hour sample")


def _group_by_day(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    samples_by_date = {}
    for sample in samples:
        samples_by_date.setdefault(str(sample["target_date"]), []).append(sample)
    return {
        target_date: tuple(samples_by_date[target_date])
        for target_date in sorted(samples_by_date)
    }


def _hourly_metrics(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    return {
        "champion": _error_metrics(samples, "champion_prediction"),
        "candidate": _error_metrics(samples, "candidate_prediction"),
    }


def _error_metrics(
    samples: Sequence[Mapping[str, Any]], prediction_key: str
) -> dict[str, float]:
    if not samples:
        return {"mae": 0.0, "rmse": 0.0, "bias": 0.0}
    errors = [
        float(sample[prediction_key]) - float(sample["actual"]) for sample in samples
    ]
    return {
        "mae": sum((abs(error) for error in errors)) / len(errors),
        "rmse": math.sqrt(sum((error * error for error in errors)) / len(errors)),
        "bias": sum(errors) / len(errors),
    }


def _daily_energy_mae(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, float]:
    if not by_day:
        return {"champion": 0.0, "candidate": 0.0}
    model_losses = {}
    for model_name, prediction_key in (
        ("champion", "champion_prediction"),
        ("candidate", "candidate_prediction"),
    ):
        daily_losses = [
            abs(
                sum((float(sample[prediction_key]) for sample in day_samples))
                - sum((float(sample["actual"]) for sample in day_samples))
            )
            for day_samples in by_day.values()
        ]
        model_losses[model_name] = sum(daily_losses) / len(daily_losses)
    return model_losses


def _daily_deltas(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, float | str], ...]:
    per_day_deltas = []
    for target_date, day_samples in by_day.items():
        champion_mae = _error_metrics(day_samples, "champion_prediction")["mae"]
        candidate_mae = _error_metrics(day_samples, "candidate_prediction")["mae"]
        per_day_deltas.append(
            {
                "target_date": target_date,
                "champion_mae": champion_mae,
                "candidate_mae": candidate_mae,
                "candidate_minus_champion_mae": candidate_mae - champion_mae,
            }
        )
    return tuple(per_day_deltas)


def _bootstrap_ci(
    daily_deltas: Sequence[Mapping[str, float | str]],
    config: ModelPromotionEvaluationConfig,
) -> tuple[float, float]:
    paired_deltas = [
        float(daily_delta["candidate_minus_champion_mae"])
        for daily_delta in daily_deltas
    ]
    rng = random.Random(config.bootstrap_seed)
    bootstrap_means = sorted(
        (
            sum((rng.choice(paired_deltas) for draw_index in paired_deltas))
            / len(paired_deltas)
            for iteration in range(config.bootstrap_iterations)
        )
    )
    lower_index = int((1.0 - _CI_LEVEL) / 2.0 * (len(bootstrap_means) - 1))
    upper_index = math.ceil((1.0 + _CI_LEVEL) / 2.0 * (len(bootstrap_means) - 1))
    return (bootstrap_means[lower_index], bootstrap_means[upper_index])


def _evaluate_slice(
    critical_slice: CriticalSlice,
    eligible: Sequence[Mapping[str, Any]],
    config: ModelPromotionEvaluationConfig,
) -> dict[str, Any]:
    slice_samples = tuple(
        (
            sample
            for sample in eligible
            if (
                critical_slice.weather_regime is None
                or sample["weather_regime"] == critical_slice.weather_regime
            )
            and (
                critical_slice.horizon is None
                or sample["horizon"] == critical_slice.horizon
            )
        )
    )
    by_day = _group_by_day(slice_samples)
    hourly_metrics = _hourly_metrics(slice_samples)
    mae_delta = hourly_metrics["candidate"]["mae"] - hourly_metrics["champion"]["mae"]
    minimum_days = (
        critical_slice.min_independent_days
        or config.critical_slice_min_independent_days
    )
    adequately_powered = len(by_day) >= minimum_days
    return {
        "name": critical_slice.name,
        "weather_regime": critical_slice.weather_regime,
        "horizon": critical_slice.horizon,
        "eligible_hour_count": len(slice_samples),
        "eligible_independent_day_count": len(by_day),
        "minimum_independent_days": minimum_days,
        "adequately_powered": adequately_powered,
        "champion_mae": hourly_metrics["champion"]["mae"],
        "candidate_mae": hourly_metrics["candidate"]["mae"],
        "candidate_minus_champion_mae": mae_delta,
        "allowed_mae_regression": critical_slice.allowed_mae_regression,
        "regresses": adequately_powered
        and mae_delta > critical_slice.allowed_mae_regression,
    }


def _stable_hash(value: object) -> str:
    serialized = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_value(value: object) -> Any:
    if isinstance(
        value,
        (
            FrozenPairedTotalStackSample,
            CriticalSlice,
            ModelPromotionEvaluationConfig,
            FrozenCyclePairedTotalStackSample,
            CycleCriticalSlice,
            CycleAwareModelPromotionEvaluationConfig,
            CycleNoiseBand,
            CycleAwareModelPromotionEvaluationConfigV3,
        ),
    ):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item_value) for key, item_value in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item_value) for item_value in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                ((str(key), _freeze(item_value)) for key, item_value in value.items())
            )
        )
    if isinstance(value, (tuple, list)):
        return tuple((_freeze(item_value) for item_value in value))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            (
                isinstance(item_value, tuple)
                and len(item_value) == 2
                and isinstance(item_value[0], str)
                for item_value in value
            )
        ):
            return {key: _thaw(item_value) for key, item_value in value}
        return [_thaw(item_value) for item_value in value]
    return value


@dataclass(frozen=True)
class FrozenCyclePairedTotalStackSample:
    """One frozen paired hourly outcome within an intentional forecast cycle."""

    target_date: str
    target_hour: int
    forecast_cycle: str
    actual: float
    champion_prediction: float
    candidate_prediction: float
    clean_eligible: bool
    is_production_hour: bool
    forecast_anchor: str
    input_fingerprint: str
    policy_fingerprint: str
    weather_regime: str | None = None
    horizon: str | None = None


@dataclass(frozen=True)
class CycleCriticalSlice:
    """A cycle and weather context that must not regress when configured."""

    name: str
    forecast_cycle: str | None = None
    weather_regime: str | None = None
    min_independent_days: int | None = None
    allowed_daily_energy_regression: float = 0.0


@dataclass(frozen=True)
class CycleAwareModelPromotionEvaluationConfig:
    """Stable V2 configuration for two-cycle daily paired evaluation."""

    min_independent_eligible_days: int = 7
    min_common_clean_production_hours_per_cycle: int = 4
    bootstrap_seed: int = 17290
    critical_slice_min_independent_days: int = 3
    critical_slices: tuple[CycleCriticalSlice, ...] = ()


@dataclass(frozen=True)
class CycleAwareModelPromotionDecision:
    """Immutable V2 decision data for cycle-aware daily energy evaluation."""

    go_no_go: str
    reason_codes: tuple[str, ...]
    dataset_fingerprint: str
    evaluation_config_hash: str
    eligible_independent_day_count: int
    eligible_common_production_hour_count: int
    daily_energy_mae: Mapping[str, float]
    per_day_paired_daily_energy_deltas: tuple[Mapping[str, float | str], ...]
    cycle_mean_daily_energy_deltas: Mapping[str, float]
    candidate_minus_champion_daily_energy_mae_ci_95: tuple[float, float] | None
    critical_slice_results: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-serializable representation."""
        return {
            "go_no_go": self.go_no_go,
            "reason_codes": list(self.reason_codes),
            "dataset_fingerprint": self.dataset_fingerprint,
            "evaluation_config_hash": self.evaluation_config_hash,
            "eligible_independent_day_count": self.eligible_independent_day_count,
            "eligible_common_production_hour_count": self.eligible_common_production_hour_count,
            "daily_energy_mae": _thaw(self.daily_energy_mae),
            "per_day_paired_daily_energy_deltas": _thaw(
                self.per_day_paired_daily_energy_deltas
            ),
            "cycle_mean_daily_energy_deltas": _thaw(
                self.cycle_mean_daily_energy_deltas
            ),
            "candidate_minus_champion_daily_energy_mae_ci_95": (
                list(self.candidate_minus_champion_daily_energy_mae_ci_95)
                if self.candidate_minus_champion_daily_energy_mae_ci_95 is not None
                else None
            ),
            "critical_slice_results": _thaw(self.critical_slice_results),
        }


def evaluate_cycle_aware_model_promotion(
    samples: Sequence[FrozenCyclePairedTotalStackSample],
    config: CycleAwareModelPromotionEvaluationConfig = CycleAwareModelPromotionEvaluationConfig(),
) -> CycleAwareModelPromotionDecision:
    """Evaluate two intentional forecast cycles using paired daily-energy loss."""
    _validate_cycle_aware_config(config)
    normalized_samples = tuple(
        (_validate_and_normalize_cycle_sample(sample) for sample in samples)
    )
    _reject_duplicate_cycle_pairs(normalized_samples)
    _reject_cycle_actual_mismatches(normalized_samples)
    by_day = _eligible_cycle_samples_by_day(normalized_samples, config)
    daily_deltas = _cycle_daily_deltas(by_day)
    confidence_interval = (
        _bootstrap_cycle_daily_ci(daily_deltas, config.bootstrap_seed)
        if daily_deltas
        else None
    )
    cycle_mean_deltas = _cycle_mean_deltas(daily_deltas)
    critical_slice_results = tuple(
        (
            _evaluate_cycle_slice(slice_spec, by_day, config)
            for slice_spec in config.critical_slices
        )
    )
    reason_codes = []
    if len(by_day) < config.min_independent_eligible_days:
        reason_codes.append("INSUFFICIENT_ELIGIBLE_DAYS")
    if confidence_interval is None or confidence_interval[1] >= 0.0:
        reason_codes.append("CANDIDATE_NOT_SUPERIOR")
    if any((cycle_mean_delta > 0.0 for cycle_mean_delta in cycle_mean_deltas.values())):
        reason_codes.append("REQUIRED_CYCLE_REGRESSION")
    if any((slice_result["regresses"] for slice_result in critical_slice_results)):
        reason_codes.append("CRITICAL_SLICE_REGRESSION")
    if any(
        (
            not slice_result["adequately_powered"]
            for slice_result in critical_slice_results
        )
    ):
        reason_codes.append("CRITICAL_SLICE_UNDERPOWERED")
    return CycleAwareModelPromotionDecision(
        go_no_go="GO" if not reason_codes else "NO_GO",
        reason_codes=tuple(reason_codes),
        dataset_fingerprint=_stable_hash(normalized_samples),
        evaluation_config_hash=_stable_hash(config),
        eligible_independent_day_count=len(by_day),
        eligible_common_production_hour_count=sum(
            (
                len(samples_by_cycle["post_midnight"])
                for samples_by_cycle in by_day.values()
            )
        )
        * 2,
        daily_energy_mae=_freeze(_cycle_daily_energy_mae(daily_deltas)),
        per_day_paired_daily_energy_deltas=tuple(
            (_freeze(daily_delta) for daily_delta in daily_deltas)
        ),
        cycle_mean_daily_energy_deltas=_freeze(cycle_mean_deltas),
        candidate_minus_champion_daily_energy_mae_ci_95=confidence_interval,
        critical_slice_results=tuple(
            (_freeze(slice_result) for slice_result in critical_slice_results)
        ),
    )


def _validate_cycle_aware_config(
    config: CycleAwareModelPromotionEvaluationConfig,
) -> None:
    if not isinstance(config, CycleAwareModelPromotionEvaluationConfig):
        raise TypeError("config must be CycleAwareModelPromotionEvaluationConfig")
    _validate_shared_cycle_aware_fields(config)


def _validate_shared_cycle_aware_fields(config: _CycleAwareConfig) -> None:
    for field_value, field_name in (
        (config.min_independent_eligible_days, "min_independent_eligible_days"),
        (
            config.min_common_clean_production_hours_per_cycle,
            "min_common_clean_production_hours_per_cycle",
        ),
        (
            config.critical_slice_min_independent_days,
            "critical_slice_min_independent_days",
        ),
    ):
        if not isinstance(field_value, bool) and isinstance(field_value, int):
            if not field_value < 1:
                continue
        raise ValueError(f"{field_name} must be positive")
    if isinstance(config.bootstrap_seed, bool) or not isinstance(
        config.bootstrap_seed, int
    ):
        raise ValueError("bootstrap_seed must be an integer")
    seen_slice_names = set()
    for slice_spec in config.critical_slices:
        if not isinstance(slice_spec, CycleCriticalSlice) or not slice_spec.name:
            raise ValueError("critical slices require a non-empty name")
        if slice_spec.name in seen_slice_names:
            raise ValueError("duplicate critical slice name")
        seen_slice_names.add(slice_spec.name)
        if slice_spec.forecast_cycle not in (None, *_V2_FORECAST_CYCLES):
            raise ValueError("critical slice forecast_cycle is unsupported")
        if slice_spec.weather_regime is not None:
            if (
                not isinstance(slice_spec.weather_regime, str)
                or not slice_spec.weather_regime
            ):
                raise ValueError(
                    "critical slice weather_regime must be non-empty or None"
                )
        if slice_spec.min_independent_days is not None:
            if (
                isinstance(slice_spec.min_independent_days, bool)
                or not isinstance(slice_spec.min_independent_days, int)
                or slice_spec.min_independent_days < 1
            ):
                raise ValueError("critical slice minimum days must be positive")
        _finite_non_negative(
            slice_spec.allowed_daily_energy_regression,
            "allowed_daily_energy_regression",
        )
        continue


def _validate_and_normalize_cycle_sample(
    sample: FrozenCyclePairedTotalStackSample,
) -> dict[str, Any]:
    if not isinstance(sample, FrozenCyclePairedTotalStackSample):
        raise TypeError("samples must be FrozenCyclePairedTotalStackSample instances")
    normalized_sample = _validate_and_normalize(
        FrozenPairedTotalStackSample(
            sample.target_date,
            sample.target_hour,
            sample.actual,
            sample.champion_prediction,
            sample.candidate_prediction,
            sample.clean_eligible,
            sample.forecast_anchor,
            sample.input_fingerprint,
            sample.policy_fingerprint,
            sample.weather_regime,
            sample.horizon,
        )
    )
    if sample.forecast_cycle not in _V2_FORECAST_CYCLES:
        raise ValueError("forecast_cycle must be post_midnight or pre_sunrise")
    if not isinstance(sample.is_production_hour, bool):
        raise ValueError("is_production_hour is unknown")
    return {
        **normalized_sample,
        "forecast_cycle": sample.forecast_cycle,
        "is_production_hour": sample.is_production_hour,
    }


def _reject_duplicate_cycle_pairs(samples: Sequence[Mapping[str, Any]]) -> None:
    pair_keys = [
        (sample["target_date"], sample["target_hour"], sample["forecast_cycle"])
        for sample in samples
    ]
    if len(pair_keys) != len(set(pair_keys)):
        raise ValueError("duplicate target date/hour/forecast cycle sample")


def _reject_cycle_actual_mismatches(samples: Sequence[Mapping[str, Any]]) -> None:
    actual_by_hour = {}
    for sample in samples:
        hour_key = (str(sample["target_date"]), int(sample["target_hour"]))
        actual = float(sample["actual"])
        if hour_key in actual_by_hour and actual_by_hour[hour_key] != actual:
            raise ValueError("forecast cycles must share the same actual")
        actual_by_hour[hour_key] = actual


def _eligible_cycle_samples_by_day(
    samples: Sequence[Mapping[str, Any]], config: _CycleAwareConfig
) -> dict[str, dict[str, tuple[Mapping[str, Any], ...]]]:
    production_samples_by_day = {}
    for sample in samples:
        if sample["clean_eligible"] and sample["is_production_hour"]:
            production_samples_by_day.setdefault(
                str(sample["target_date"]), {}
            ).setdefault(str(sample["forecast_cycle"]), {})[
                int(sample["target_hour"])
            ] = sample
    eligible_by_day = {}
    for target_date, samples_by_cycle in production_samples_by_day.items():
        if any(
            (
                forecast_cycle not in samples_by_cycle
                for forecast_cycle in _V2_FORECAST_CYCLES
            )
        ):
            continue
        common_hours = set(samples_by_cycle["post_midnight"]) & set(
            samples_by_cycle["pre_sunrise"]
        )
        if len(common_hours) < config.min_common_clean_production_hours_per_cycle:
            continue
        eligible_by_day[target_date] = {
            forecast_cycle: tuple(
                (
                    samples_by_cycle[forecast_cycle][target_hour]
                    for target_hour in sorted(common_hours)
                )
            )
            for forecast_cycle in _V2_FORECAST_CYCLES
        }
    return {
        target_date: eligible_by_day[target_date]
        for target_date in sorted(eligible_by_day)
    }


def _cycle_daily_deltas(
    by_day: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, float | str], ...]:
    per_day_deltas = []
    for target_date, samples_by_cycle in by_day.items():
        losses_by_cycle = {}
        for forecast_cycle, cycle_samples in samples_by_cycle.items():
            actual_total = sum((float(sample["actual"]) for sample in cycle_samples))
            champion_loss = abs(
                sum((float(sample["champion_prediction"]) for sample in cycle_samples))
                - actual_total
            )
            candidate_loss = abs(
                sum((float(sample["candidate_prediction"]) for sample in cycle_samples))
                - actual_total
            )
            losses_by_cycle[forecast_cycle] = (champion_loss, candidate_loss)
        champion_loss = sum(
            (cycle_losses[0] for cycle_losses in losses_by_cycle.values())
        ) / len(losses_by_cycle)
        candidate_loss = sum(
            (cycle_losses[1] for cycle_losses in losses_by_cycle.values())
        ) / len(losses_by_cycle)
        per_day_deltas.append(
            {
                "target_date": target_date,
                "champion_daily_energy_loss": champion_loss,
                "candidate_daily_energy_loss": candidate_loss,
                "candidate_minus_champion_daily_energy_loss": candidate_loss
                - champion_loss,
                **{
                    f"{forecast_cycle}_candidate_minus_champion_daily_energy_loss": cycle_losses[
                        1
                    ]
                    - cycle_losses[0]
                    for forecast_cycle, cycle_losses in losses_by_cycle.items()
                },
            }
        )
        continue
    return tuple(per_day_deltas)


def _cycle_daily_energy_mae(
    daily_deltas: Sequence[Mapping[str, float | str]],
) -> dict[str, float]:
    if not daily_deltas:
        return {"champion": 0.0, "candidate": 0.0}
    return {
        "champion": sum(
            (
                float(daily_delta["champion_daily_energy_loss"])
                for daily_delta in daily_deltas
            )
        )
        / len(daily_deltas),
        "candidate": sum(
            (
                float(daily_delta["candidate_daily_energy_loss"])
                for daily_delta in daily_deltas
            )
        )
        / len(daily_deltas),
    }


def _cycle_mean_deltas(
    daily_deltas: Sequence[Mapping[str, float | str]],
) -> dict[str, float]:
    if not daily_deltas:
        return {forecast_cycle: 0.0 for forecast_cycle in _V2_FORECAST_CYCLES}
    return {
        forecast_cycle: sum(
            (
                float(
                    daily_delta[
                        f"{forecast_cycle}_candidate_minus_champion_daily_energy_loss"
                    ]
                )
                for daily_delta in daily_deltas
            )
        )
        / len(daily_deltas)
        for forecast_cycle in _V2_FORECAST_CYCLES
    }


def _bootstrap_cycle_daily_ci(
    daily_deltas: Sequence[Mapping[str, float | str]], seed: int
) -> tuple[float, float]:
    paired_deltas = [
        float(daily_delta["candidate_minus_champion_daily_energy_loss"])
        for daily_delta in daily_deltas
    ]
    rng = random.Random(seed)
    bootstrap_means = sorted(
        (
            sum((rng.choice(paired_deltas) for draw_index in paired_deltas))
            / len(paired_deltas)
            for iteration in range(_V2_BOOTSTRAP_ITERATIONS)
        )
    )
    lower_index = int((1.0 - _CI_LEVEL) / 2.0 * (len(bootstrap_means) - 1))
    upper_index = math.ceil((1.0 + _CI_LEVEL) / 2.0 * (len(bootstrap_means) - 1))
    return (bootstrap_means[lower_index], bootstrap_means[upper_index])


def _cycle_sample_fields(sample: Any) -> dict[str, Any]:
    if isinstance(sample, Mapping):
        sample_fields = sample
        weather_regime = sample_fields.get("weather_regime")
    else:
        sample_fields = {
            "target_date": sample.target_date,
            "target_hour": sample.target_hour,
            "forecast_cycle": sample.forecast_cycle,
            "actual": sample.actual,
            "champion_prediction": sample.champion_prediction,
            "candidate_prediction": sample.candidate_prediction,
        }
        weather_regime = getattr(sample, "weather_regime", None)
    return {
        "target_date": str(sample_fields["target_date"]),
        "target_hour": int(sample_fields["target_hour"]),
        "forecast_cycle": sample_fields["forecast_cycle"],
        "actual": float(sample_fields["actual"]),
        "champion_prediction": float(sample_fields["champion_prediction"]),
        "candidate_prediction": float(sample_fields["candidate_prediction"]),
        "weather_regime": weather_regime,
    }


def cycle_slice_day_deltas(
    samples: Sequence[Any], forecast_cycle: str | None, weather_regime: str | None
) -> list[float]:
    """Return per-day slice loss deltas using the evaluator's grouping."""
    by_day = {}
    for sample in samples:
        normalized_sample = _cycle_sample_fields(sample)
        by_day.setdefault(normalized_sample["target_date"], {}).setdefault(
            str(normalized_sample["forecast_cycle"]), []
        ).append(normalized_sample)
    day_deltas = []
    for target_date in sorted(by_day):
        samples_by_cycle = by_day[target_date]
        cycle_deltas = []
        for resolved_forecast_cycle, cycle_samples in samples_by_cycle.items():
            if forecast_cycle is not None:
                if resolved_forecast_cycle != forecast_cycle:
                    continue
            slice_samples = tuple(
                (
                    sample
                    for sample in cycle_samples
                    if weather_regime is None
                    or sample["weather_regime"] == weather_regime
                )
            )
            if slice_samples:
                actual_total = sum((sample["actual"] for sample in slice_samples))
                champion_loss = abs(
                    sum((sample["champion_prediction"] for sample in slice_samples))
                    - actual_total
                )
                candidate_loss = abs(
                    sum((sample["candidate_prediction"] for sample in slice_samples))
                    - actual_total
                )
                cycle_deltas.append(candidate_loss - champion_loss)
        if not cycle_deltas:
            continue
        day_deltas.append(sum(cycle_deltas) / len(cycle_deltas))
        continue
    return day_deltas


def _evaluate_cycle_slice(
    critical_slice: CycleCriticalSlice,
    by_day: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    config: _CycleAwareConfig,
) -> dict[str, Any]:
    all_samples = tuple(
        (
            sample
            for samples_by_cycle in by_day.values()
            for cycle_samples in samples_by_cycle.values()
            for sample in cycle_samples
        )
    )
    day_deltas = cycle_slice_day_deltas(
        all_samples, critical_slice.forecast_cycle, critical_slice.weather_regime
    )
    minimum_days = (
        critical_slice.min_independent_days
        or config.critical_slice_min_independent_days
    )
    adequately_powered = len(day_deltas) >= minimum_days
    mean_daily_energy_delta = sum(day_deltas) / len(day_deltas) if day_deltas else 0.0
    return {
        "name": critical_slice.name,
        "forecast_cycle": critical_slice.forecast_cycle,
        "weather_regime": critical_slice.weather_regime,
        "eligible_independent_day_count": len(day_deltas),
        "minimum_independent_days": minimum_days,
        "adequately_powered": adequately_powered,
        "candidate_minus_champion_daily_energy_loss": mean_daily_energy_delta,
        "allowed_daily_energy_regression": critical_slice.allowed_daily_energy_regression,
        "regresses": adequately_powered
        and mean_daily_energy_delta > critical_slice.allowed_daily_energy_regression,
    }


_V3_POLICY_CONTRACT_VERSION = "cycle_aware_model_promotion_policy_v3"


@dataclass(frozen=True)
class CycleNoiseBand:
    """Frozen one-sided noise band for one forecast cycle's daily energy delta."""

    forecast_cycle: str
    allowed_daily_energy_regression: float = 0.0


@dataclass(frozen=True)
class CycleAwareModelPromotionEvaluationConfigV3:
    "Stable V3 configuration: V2 paired evaluation plus non-inferiority refresh.\n\nA champion younger than ``champion_min_age_days_for_non_inferiority`` must be\nbeaten (upper CI bound below zero).  An older champion is replaced by any\ncandidate whose paired daily-energy delta stays inside the frozen noise band.\n"

    min_independent_eligible_days: int = 7
    min_common_clean_production_hours_per_cycle: int = 4
    bootstrap_seed: int = 17290
    critical_slice_min_independent_days: int = 3
    critical_slices: tuple[CycleCriticalSlice, ...] = ()
    policy_contract_version: str = _V3_POLICY_CONTRACT_VERSION
    cycle_noise_bands: tuple[CycleNoiseBand, ...] = ()
    allowed_total_daily_energy_regression: float = 0.0
    champion_min_age_days_for_non_inferiority: int = 28
    champion_age_days: int | None = None


_CycleAwareConfig = (
    CycleAwareModelPromotionEvaluationConfig
    | CycleAwareModelPromotionEvaluationConfigV3
)


@dataclass(frozen=True)
class CycleAwareModelPromotionDecisionV3:
    """Immutable V3 decision data for cycle-aware daily energy evaluation."""

    go_no_go: str
    reason_codes: tuple[str, ...]
    comparison_outcome: str
    dataset_fingerprint: str
    evaluation_config_hash: str
    eligible_independent_day_count: int
    eligible_common_production_hour_count: int
    daily_energy_mae: Mapping[str, float]
    candidate_minus_champion_daily_energy_mae: float
    allowed_total_daily_energy_regression: float
    non_inferiority_enabled: bool
    champion_age_days: int | None
    per_day_paired_daily_energy_deltas: tuple[Mapping[str, float | str], ...]
    cycle_mean_daily_energy_deltas: Mapping[str, float]
    cycle_noise_band_results: tuple[Mapping[str, Any], ...]
    candidate_minus_champion_daily_energy_mae_ci_95: tuple[float, float] | None
    critical_slice_results: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-serializable representation."""
        return {
            "go_no_go": self.go_no_go,
            "reason_codes": list(self.reason_codes),
            "comparison_outcome": self.comparison_outcome,
            "dataset_fingerprint": self.dataset_fingerprint,
            "evaluation_config_hash": self.evaluation_config_hash,
            "eligible_independent_day_count": self.eligible_independent_day_count,
            "eligible_common_production_hour_count": self.eligible_common_production_hour_count,
            "daily_energy_mae": _thaw(self.daily_energy_mae),
            "candidate_minus_champion_daily_energy_mae": self.candidate_minus_champion_daily_energy_mae,
            "allowed_total_daily_energy_regression": self.allowed_total_daily_energy_regression,
            "non_inferiority_enabled": self.non_inferiority_enabled,
            "champion_age_days": self.champion_age_days,
            "per_day_paired_daily_energy_deltas": _thaw(
                self.per_day_paired_daily_energy_deltas
            ),
            "cycle_mean_daily_energy_deltas": _thaw(
                self.cycle_mean_daily_energy_deltas
            ),
            "cycle_noise_band_results": _thaw(self.cycle_noise_band_results),
            "candidate_minus_champion_daily_energy_mae_ci_95": (
                list(self.candidate_minus_champion_daily_energy_mae_ci_95)
                if self.candidate_minus_champion_daily_energy_mae_ci_95 is not None
                else None
            ),
            "critical_slice_results": _thaw(self.critical_slice_results),
        }


def evaluate_cycle_aware_model_promotion_v3(
    samples: Sequence[FrozenCyclePairedTotalStackSample],
    config: CycleAwareModelPromotionEvaluationConfigV3,
) -> CycleAwareModelPromotionDecisionV3:
    """Evaluate two forecast cycles with superiority or aged-champion non-inferiority."""
    _validate_cycle_aware_config_v3(config)
    normalized_samples = tuple(
        (_validate_and_normalize_cycle_sample(sample) for sample in samples)
    )
    _reject_duplicate_cycle_pairs(normalized_samples)
    _reject_cycle_actual_mismatches(normalized_samples)
    by_day = _eligible_cycle_samples_by_day(normalized_samples, config)
    daily_deltas = _cycle_daily_deltas(by_day)
    confidence_interval = (
        _bootstrap_cycle_daily_ci(daily_deltas, config.bootstrap_seed)
        if daily_deltas
        else None
    )
    cycle_mean_deltas = _cycle_mean_deltas(daily_deltas)
    mean_daily_energy_delta = (
        sum(
            (
                float(daily_delta["candidate_minus_champion_daily_energy_loss"])
                for daily_delta in daily_deltas
            )
        )
        / len(daily_deltas)
        if daily_deltas
        else 0.0
    )
    non_inferiority_enabled = (
        config.champion_age_days is not None
        and config.champion_age_days >= config.champion_min_age_days_for_non_inferiority
    )
    is_superior = confidence_interval is not None and confidence_interval[1] < 0.0
    within_total_noise_band = (
        mean_daily_energy_delta <= config.allowed_total_daily_energy_regression
    )
    if is_superior:
        comparison_outcome = "superior"
    elif confidence_interval is None or not non_inferiority_enabled:
        comparison_outcome = "not_superior"
    elif within_total_noise_band:
        comparison_outcome = "non_inferior"
    else:
        comparison_outcome = "inferior"
    allowed_cycle_regressions = {
        noise_band.forecast_cycle: noise_band.allowed_daily_energy_regression
        for noise_band in config.cycle_noise_bands
    }
    noise_band_results = tuple(
        (
            {
                "forecast_cycle": forecast_cycle,
                "candidate_minus_champion_daily_energy_loss": cycle_mean_deltas[
                    forecast_cycle
                ],
                "allowed_daily_energy_regression": allowed_cycle_regressions[
                    forecast_cycle
                ],
                "regresses": cycle_mean_deltas[forecast_cycle]
                > allowed_cycle_regressions[forecast_cycle],
            }
            for forecast_cycle in _V2_FORECAST_CYCLES
        )
    )
    critical_slice_results = tuple(
        (
            _evaluate_cycle_slice(slice_spec, by_day, config)
            for slice_spec in config.critical_slices
        )
    )
    reason_codes = []
    if len(by_day) < config.min_independent_eligible_days:
        reason_codes.append("INSUFFICIENT_ELIGIBLE_DAYS")
    if comparison_outcome == "not_superior":
        reason_codes.append("CANDIDATE_NOT_SUPERIOR")
    if comparison_outcome == "inferior":
        reason_codes.append("CANDIDATE_INFERIOR")
    if any(
        (noise_band_result["regresses"] for noise_band_result in noise_band_results)
    ):
        reason_codes.append("REQUIRED_CYCLE_REGRESSION")
    if any((slice_result["regresses"] for slice_result in critical_slice_results)):
        reason_codes.append("CRITICAL_SLICE_REGRESSION")
    if any(
        (
            not slice_result["adequately_powered"]
            for slice_result in critical_slice_results
        )
    ):
        reason_codes.append("CRITICAL_SLICE_UNDERPOWERED")
    return CycleAwareModelPromotionDecisionV3(
        go_no_go="GO" if not reason_codes else "NO_GO",
        reason_codes=tuple(reason_codes),
        comparison_outcome=comparison_outcome,
        dataset_fingerprint=_stable_hash(normalized_samples),
        evaluation_config_hash=_stable_hash(config),
        eligible_independent_day_count=len(by_day),
        eligible_common_production_hour_count=sum(
            (
                len(samples_by_cycle["post_midnight"])
                for samples_by_cycle in by_day.values()
            )
        )
        * 2,
        daily_energy_mae=_freeze(_cycle_daily_energy_mae(daily_deltas)),
        candidate_minus_champion_daily_energy_mae=mean_daily_energy_delta,
        allowed_total_daily_energy_regression=config.allowed_total_daily_energy_regression,
        non_inferiority_enabled=non_inferiority_enabled,
        champion_age_days=config.champion_age_days,
        per_day_paired_daily_energy_deltas=tuple(
            (_freeze(daily_delta) for daily_delta in daily_deltas)
        ),
        cycle_mean_daily_energy_deltas=_freeze(cycle_mean_deltas),
        cycle_noise_band_results=tuple(
            (_freeze(noise_band_result) for noise_band_result in noise_band_results)
        ),
        candidate_minus_champion_daily_energy_mae_ci_95=confidence_interval,
        critical_slice_results=tuple(
            (_freeze(slice_result) for slice_result in critical_slice_results)
        ),
    )


def _validate_cycle_aware_config_v3(
    config: CycleAwareModelPromotionEvaluationConfigV3,
) -> None:
    if not isinstance(config, CycleAwareModelPromotionEvaluationConfigV3):
        raise TypeError("config must be CycleAwareModelPromotionEvaluationConfigV3")
    _validate_shared_cycle_aware_fields(config)
    if config.policy_contract_version != _V3_POLICY_CONTRACT_VERSION:
        raise ValueError("policy_contract_version is unsupported")
    seen_cycles = set()
    for noise_band in config.cycle_noise_bands:
        if not isinstance(noise_band, CycleNoiseBand):
            raise ValueError("cycle noise bands must be CycleNoiseBand instances")
        if noise_band.forecast_cycle not in _V2_FORECAST_CYCLES:
            raise ValueError("cycle noise band forecast_cycle is unsupported")
        if noise_band.forecast_cycle in seen_cycles:
            raise ValueError("duplicate cycle noise band")
        seen_cycles.add(noise_band.forecast_cycle)
        _finite_non_negative(
            noise_band.allowed_daily_energy_regression,
            "allowed_daily_energy_regression",
        )
    if seen_cycles != set(_V2_FORECAST_CYCLES):
        raise ValueError("cycle noise bands must cover every forecast cycle")
    _finite_non_negative(
        config.allowed_total_daily_energy_regression,
        "allowed_total_daily_energy_regression",
    )
    champion_minimum_age = config.champion_min_age_days_for_non_inferiority
    if (
        isinstance(champion_minimum_age, bool)
        or not isinstance(champion_minimum_age, int)
        or champion_minimum_age < 1
    ):
        raise ValueError("champion_min_age_days_for_non_inferiority must be positive")
    champion_age = config.champion_age_days
    if champion_age is not None:
        if (
            isinstance(champion_age, bool)
            or not isinstance(champion_age, int)
            or champion_age < 0
        ):
            raise ValueError("champion_age_days must be a non-negative integer or None")
    return None if True else None
