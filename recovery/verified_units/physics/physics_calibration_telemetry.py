# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-only physics calibration telemetry. Does not change living factors. @zara"""
from __future__ import annotations
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple
from ..core.core_helpers import SafeDateTimeUtil
from ..data.data_shadow_observation import CEILING_MIN_BIN_SAMPLES

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)
PHYSICS_CALIBRATION_LEGACY_CONTRACT = "physics_calibration_legacy_mixed_v0"
PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT = "physics_calibration_sensor_ghi_v1"
PHYSICS_CALIBRATION_FORECAST_PHYSICS_POST_CAP_CONTRACT = (
    "physics_calibration_forecast_physics_post_cap_v1"
)
PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT = "physics_calibration_poa_dni_iam_v1"
PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT = (
    "physics_calibration_ghi_forecast_scale_v1"
)
PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT = "physics_calibration_ghi_tilt_poa_v1"
PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT = "physics_calibration_raw_forecast_v1"
PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT = "physics_calibration_raw_forecast_v2"
PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT = "physics_calibration_raw_forecast_v3"
_RAW_FORECAST_CONTRACTS = frozenset(
    {
        PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
    }
)
RAW_LEARN_DAYS = 3
FACTOR_STEP_CAP = 0.15
HOURLY_ABS_CEILING = 1.9
POA_DNI_IAM_FORMULA_VERSION = "poa_dni_iam_v1"
PHYSICS_CALIBRATION_POST_CAP_CONTRACTS = frozenset(
    {
        PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT,
        PHYSICS_CALIBRATION_FORECAST_PHYSICS_POST_CAP_CONTRACT,
        PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
        PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
        PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
    }
)
_FACTORS_HELD_UNTIL_REARM = frozenset(
    {
        PHYSICS_CALIBRATION_FORECAST_PHYSICS_POST_CAP_CONTRACT,
        PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
        PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
        PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
    }
)
_STAMP_GATED_CONTRACTS = frozenset(
    {
        PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
        PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
        PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
        PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
    }
)
PHYSICS_CALIBRATION_TELEMETRY_RETENTION_DAYS = 400
PHYSICS_REF_KIND_FORECAST = "forecast_physics"
PHYSICS_REF_KIND_GHI_SCALED = "ghi_scaled_clearsky"
SHAPE_PRODUCTION_HOURS = range(8, 19)
GHI_GEOMETRY_LOOKBACK_DAYS = 90
GHI_GEOMETRY_DIAGNOSIS_MIN_BIN_SAMPLES = 5
GHI_GEOMETRY_BEHAVIOR_MIN_BIN_SAMPLES = CEILING_MIN_BIN_SAMPLES
GHI_GEOMETRY_MIN_ELEVATION_DEG = 5.0
GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN = 20
GHI_ELEV_BIN_DEG = 10
GHI_AZ_BIN_DEG = 20
EAST_AZIMUTH_RANGE = (45.0, 135.0)
WEST_AZIMUTH_RANGE = (225.0, 315.0)
DIAGNOSIS_NO_LOCAL_GHI = "no_local_ghi"
DIAGNOSIS_INSUFFICIENT_OVERLAP = "insufficient_same_elevation_east_west"
DIAGNOSIS_BELOW_HEADLINE = "contrast_below_headline_elevation"
DIAGNOSIS_CONTRAST_RECORDED = "geometry_contrast_recorded"


def select_post_cap_learning_contract(
    forecast_physics_samples: int, ghi_scaled_samples: int
) -> tuple[str, bool]:
    """Choose the post-cap contract and whether live factors must be wiped.

    forecast_physics dominance means the learned factor absorbed the strategy
    cap. Sensor plants stay on the open-loop contract and keep their factors.
    A tie does not dominate, so those groups are not wiped.
    """
    try:
        forecast_physics_count = int(forecast_physics_samples or 0)
    except (TypeError, ValueError):
        forecast_physics_count = 0
    try:
        ghi_scaled_count = int(ghi_scaled_samples or 0)
    except (TypeError, ValueError):
        ghi_scaled_count = 0
    if forecast_physics_count > ghi_scaled_count:
        return (PHYSICS_CALIBRATION_FORECAST_PHYSICS_POST_CAP_CONTRACT, True)
    return (PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT, False)


def is_raw_forecast_contract(version: Optional[str]) -> bool:
    """v1, v2 and v3 all learn on raw forecast physics."""
    return version in _RAW_FORECAST_CONTRACTS


def _later_contract(current_version: Optional[str]) -> Optional[str]:
    """A newer contract must not be rewritten by an older gate."""
    if current_version in _RAW_FORECAST_CONTRACTS:
        return current_version


def select_poa_dni_iam_contract(current_version: Optional[str]) -> tuple[str, bool]:
    """Stamp the POA contract. Any older version is wiped once.

    Groups already on this contract, or on a later reference contract,
    are left untouched so a restart cannot clear factors learned after the wipe.
    """
    newer_contract = _later_contract(current_version)
    if newer_contract is not None:
        return (newer_contract, False)
    if current_version == PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT:
        return (PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT, False)
    if current_version == PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT:
        return (PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT, False)
    if current_version == PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT:
        return (PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT, False)
    return (PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT, True)


def select_ghi_forecast_scale_contract(
    current_version: Optional[str], ghi_scaled_samples: int
) -> tuple[str, bool]:
    """Stamp the forecast-scale contract. Wipe only groups that learned on sensor GHI.

    A group already on this contract, or on a later contract, is not
    wiped again. Groups whose rolling window has no ghi_scaled_clearsky
    sample keep their factors.
    """
    newer_contract = _later_contract(current_version)
    if newer_contract is not None:
        return (newer_contract, False)
    if current_version == PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT:
        return (PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT, False)
    if current_version == PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT:
        return (PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT, False)
    try:
        ghi_scaled_count = int(ghi_scaled_samples or 0)
    except (TypeError, ValueError):
        ghi_scaled_count = 0
    return (PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT, ghi_scaled_count > 0)


def _parse_aware_datetime(value: Any) -> Optional[datetime]:
    """Parse an aware timestamp. Naive or unreadable values are unknown."""
    if isinstance(value, datetime):
        parsed_datetime = value
    elif isinstance(value, str):
        timestamp_text = value.strip()
        if not timestamp_text:
            return
        if timestamp_text.endswith("Z"):
            timestamp_text = timestamp_text[:-1] + "+00:00"
        try:
            parsed_datetime = datetime.fromisoformat(timestamp_text)
        except ValueError:
            return
    else:
        return
    if parsed_datetime.tzinfo is None:
        return
    return parsed_datetime


def poa_contract_hour_is_learnable(
    contract_version: Optional[str],
    stamped_at: Optional[str],
    prediction_created_at: Any,
) -> bool:
    """Stamp-gated contracts count an hour only after the contract stamp.

    Older contracts are not gated. A missing stamp or an unreadable creation
    time fails closed: the hour is not a sample.
    """
    if contract_version not in _STAMP_GATED_CONTRACTS:
        return True
    contract_stamp = _parse_aware_datetime(stamped_at)
    prediction_created = _parse_aware_datetime(prediction_created_at)
    if contract_stamp is None or prediction_created is None:
        return False
    return prediction_created > contract_stamp


def select_ghi_tilt_poa_contract(
    current_version: Optional[str], ghi_scaled_samples: int
) -> tuple[str, bool]:
    """Stamp the tilt POA contract. Wipe only groups that learned on sensor GHI.

    A group already on this contract, or on the raw-forecast contract, is
    not wiped again. Older gates must treat those versions as migrated.
    """
    newer_contract = _later_contract(current_version)
    if newer_contract is not None:
        return (newer_contract, False)
    if current_version == PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT:
        return (PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT, False)
    try:
        ghi_scaled_count = int(ghi_scaled_samples or 0)
    except (TypeError, ValueError):
        ghi_scaled_count = 0
    return (PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT, ghi_scaled_count > 0)


def select_raw_forecast_contract(current_version: Optional[str]) -> tuple[str, bool]:
    """Move every group onto raw-forecast v3 once. v3 is not migrated again."""
    if current_version == PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT:
        return (PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT, False)
    return (PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT, True)


def union_hourly_bounds(
    global_factor: float, absolute_min: float, absolute_max: float
) -> tuple[float, float]:
    """Hourly and bucket band for the raw-forecast contract.

    The lower bound is the absolute correction minimum. The upper bound is
    max(1.90, 1.90 × global), then inside the absolute correction maximum.
    """
    try:
        normalized_global_factor = float(global_factor)
    except (TypeError, ValueError):
        normalized_global_factor = 1.0
    if not math.isfinite(normalized_global_factor) or normalized_global_factor <= 0.0:
        normalized_global_factor = 1.0
    try:
        lower_bound = float(absolute_min)
    except (TypeError, ValueError):
        lower_bound = 0.3
    if not math.isfinite(lower_bound):
        lower_bound = 0.3
    upper_bound = min(
        absolute_max,
        max(HOURLY_ABS_CEILING, HOURLY_ABS_CEILING * normalized_global_factor),
    )
    if lower_bound > upper_bound:
        return (upper_bound, lower_bound)
    return (lower_bound, upper_bound)


def clamp_union_hourly(
    value: float, global_factor: float, absolute_min: float, absolute_max: float
) -> float:
    lower_bound, upper_bound = union_hourly_bounds(
        global_factor, absolute_min, absolute_max
    )
    return min(upper_bound, max(lower_bound, value))


def step_limit_factor(previous: float, proposed: float) -> float:
    """Keep one learning day inside ±15% of the factor already in use."""
    if previous <= 0.0 or not math.isfinite(previous):
        return proposed
    return min(
        previous * (1.0 + FACTOR_STEP_CAP),
        max(previous * (1.0 - FACTOR_STEP_CAP), proposed),
    )


def raw_ema_alpha(level: float) -> float:
    """Same adaptive alpha the calibrator uses for a daily ratio."""
    return min(0.4, 0.22 + abs(level - 1.0) * 0.15)


def publish_raw_factor(means: Sequence[float], clamp) -> float:
    """First published value: EMA of the day-mean against 1.0, then the step cap."""
    if not means:
        return 1.0
    day_mean = sum(means) / len(means)
    alpha = raw_ema_alpha(day_mean)
    blended_factor = alpha * day_mean + (1.0 - alpha) * 1.0
    return clamp(step_limit_factor(1.0, blended_factor))


def ema_raw_factor(previous: float, today: float, clamp) -> float:
    alpha = raw_ema_alpha(today)
    blended_factor = alpha * today + (1.0 - alpha) * previous
    return clamp(step_limit_factor(previous, blended_factor))


def advantage_factor(physics_mae: Optional[float], ai_mae: Optional[float]) -> float:
    """physics_mae / ai_mae, clamped to [0.5, 2.0]. Same rule as the daily learner."""
    if physics_mae is None or ai_mae is None or physics_mae <= 0.0 or (ai_mae <= 0.0):
        return 1.0
    return max(0.5, min(2.0, physics_mae / ai_mae))


def _lowest_mae_method(candidates: Dict[str, Optional[float]]) -> str:
    """First name with the smallest MAE. None values are not candidates."""
    best_method = None
    best_mae = None
    for method, mae in candidates.items():
        if mae is not None and (best_mae is None or mae < best_mae):
            best_method = method
            best_mae = mae
    return best_method if best_method is not None else "physics"


def group_best_method(
    physics_mae: Optional[float],
    ai_mae: Optional[float],
    lstm_mae: Optional[float],
    ridge_mae: Optional[float],
) -> str:
    """Group-table choice. Same order as production_scheduled_tasks.py:5273."""
    return _lowest_mae_method(
        {"physics": physics_mae, "ai": ai_mae, "lstm": lstm_mae, "ridge": ridge_mae}
    )


def regime_best_method(
    physics_mae: Optional[float],
    blend_mae: Optional[float],
    ai_mae: Optional[float],
    tfs_mae: Optional[float],
) -> str:
    """Regime-table choice. Same order as production_scheduled_tasks.py:4855."""
    return _lowest_mae_method(
        {"physics": physics_mae, "blend": blend_mae, "ai": ai_mae, "tfs": tfs_mae}
    )


def recommended_tfs_weight(
    best_method: Optional[str], tfs_advantage: Optional[float]
) -> Optional[float]:
    """Weight the daily regime learner stores when the chosen method is TFS.

    production_scheduled_tasks.py:4865. The advantage itself is blend/tfs and
    does not use physics_mae; the choice of method does.
    """
    if best_method != "tfs" or tfs_advantage is None or tfs_advantage <= 1.1:
        return None
    return max(0.35, min(0.7, 0.35 + (tfs_advantage - 1.0) * 0.25))


def _smooth_value(new_value, old_value, smoothing: float):
    if new_value is None:
        return old_value
    if old_value is None:
        return new_value
    return smoothing * new_value + (1.0 - smoothing) * old_value


def fold_mae_day(
    table: Dict[tuple, Dict[str, Any]],
    key: tuple,
    physics_errors: Sequence[float],
    ai_errors: Sequence[float],
    smoothing: float = 0.3,
    physics_bias_values: Optional[Sequence[float]] = None,
) -> None:
    """One day of absolute errors into an EMA table. Empty days do nothing."""
    if not physics_errors:
        return
    daily_physics_mae = sum(physics_errors) / len(physics_errors)
    daily_ai_mae = sum(ai_errors) / len(ai_errors) if ai_errors else None
    daily_physics_bias = (
        sum(physics_bias_values) / len(physics_bias_values)
        if physics_bias_values
        else None
    )
    mae_state = table.get(key)
    if mae_state is None:
        table[key] = {
            "physics_mae": daily_physics_mae,
            "ai_mae": daily_ai_mae,
            "physics_bias": daily_physics_bias,
            "sample_count": len(physics_errors),
        }
        return
    if mae_state["sample_count"] > 0 and mae_state["physics_mae"] is not None:
        mae_state["physics_mae"] = _smooth_value(
            daily_physics_mae, mae_state["physics_mae"], smoothing
        )
        mae_state["ai_mae"] = _smooth_value(
            daily_ai_mae, mae_state.get("ai_mae"), smoothing
        )
        mae_state["physics_bias"] = _smooth_value(
            daily_physics_bias, mae_state.get("physics_bias"), smoothing
        )
    else:
        mae_state["physics_mae"] = daily_physics_mae
        mae_state["ai_mae"] = daily_ai_mae
        mae_state["physics_bias"] = daily_physics_bias
    mae_state["sample_count"] += len(physics_errors)


def smoothed_mae_rows(day_folds: Sequence[tuple]) -> list:
    """Chronological day folds into final MAE rows. Same input, same rows."""
    mae_by_key = {}
    for day_fold in day_folds:
        bias_values = day_fold[3] if len(day_fold) > 3 else None
        fold_mae_day(
            mae_by_key,
            tuple(day_fold[0]),
            list(day_fold[1]),
            list(day_fold[2] or []),
            physics_bias_values=None if bias_values is None else list(bias_values),
        )
    rows = []
    for fold_key, mae_state in mae_by_key.items():
        physics_mae = mae_state["physics_mae"]
        ai_mae = mae_state["ai_mae"]
        rows.append(
            {
                "key": fold_key,
                "physics_mae": physics_mae,
                "ai_mae": ai_mae,
                "physics_bias": mae_state.get("physics_bias"),
                "sample_count": mae_state["sample_count"],
                "advantage": advantage_factor(physics_mae, ai_mae),
                "best_method": group_best_method(physics_mae, ai_mae, None, None),
            }
        )
    return rows


def should_rebuild_factors_from_history(contract_version: Optional[str]) -> bool:
    """Wiped contracts must not be rebuilt from pre-change history ratios.

    The raw-forecast contracts replay prediction rows, never this history.
    """
    if is_raw_forecast_contract(contract_version):
        return False
    return contract_version not in _FACTORS_HELD_UNTIL_REARM


def should_hold_post_cap_rearm(
    contract_version: Optional[str], sample_count: int, min_samples: int
) -> bool:
    """Keep factor 1.0 until the wiped group has enough new samples.

    The raw-forecast contracts use their own three-day rearm, not this hold.
    """
    if is_raw_forecast_contract(contract_version):
        return False
    if contract_version not in _FACTORS_HELD_UNTIL_REARM:
        return False
    try:
        samples = int(sample_count or 0)
    except (TypeError, ValueError):
        samples = 0
    try:
        required_samples = int(min_samples)
    except (TypeError, ValueError):
        required_samples = 0
    return samples < required_samples if True else None


_TELEMETRY_DATE_TABLES = (
    ("physics_calibration_samples", "date"),
    ("physics_calibration_clamp_hits", "date"),
    ("physics_calibration_hourly_shape", "date"),
    ("ghi_sensor_geometry_bins", "computed_on"),
    ("ghi_sensor_geometry_sectors", "computed_on"),
    ("ghi_sensor_geometry_daily", "computed_on"),
)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile; q in (0, 1]."""
    if not values:
        return
    sorted_values = sorted((float(value) for value in values))
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = min(len(sorted_values), max(1, int(math.ceil(q * len(sorted_values)))))
    return sorted_values[rank - 1]


def elevation_bin(elevation_deg: float) -> int:
    return int(math.floor(float(elevation_deg) / GHI_ELEV_BIN_DEG) * GHI_ELEV_BIN_DEG)


def azimuth_bin(azimuth_deg: float) -> int:
    normalized_azimuth = float(azimuth_deg) % 360.0
    return int(math.floor(normalized_azimuth / GHI_AZ_BIN_DEG) * GHI_AZ_BIN_DEG)


def azimuth_sector(azimuth_deg: float) -> str:
    """v1: east 45-135 and west 225-315 only; all other azimuths are other.

    Uses the sample azimuth, not the 20° bin midpoint. Persisted az bins
    stay on the 20° partition; pooled sectors are the headline contract.
    """
    normalized_azimuth = float(azimuth_deg) % 360.0
    east_min, east_max = EAST_AZIMUTH_RANGE
    west_min, west_max = WEST_AZIMUTH_RANGE
    if east_min <= normalized_azimuth < east_max:
        return "east" if True else None
    if west_min <= normalized_azimuth < west_max:
        return "west" if True else None
    return "other" if True else None


def format_headline_exclusion_note(
    elev_bin: int, east_k_p90: float, west_k_p90: float
) -> str:
    note = f"elev {elev_bin} excluded from headline (elev < {GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN}; east k_p90={east_k_p90:.3f} west k_p90={west_k_p90:.3f}"
    if east_k_p90 > 1.0 or west_k_p90 > 1.0:
        note += "; k_p90 > 1"
    return note + ")"


def headline_exclusion_notes_from_sectors(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Rebuild headline-exclusion notes from persisted sector aggregates."""
    sector_p90_by_elevation = defaultdict(dict)
    for row in rows:
        sector = row.get("sector")
        if sector not in ("east", "west"):
            continue
        if int(row.get("sample_count") or 0) < GHI_GEOMETRY_DIAGNOSIS_MIN_BIN_SAMPLES:
            continue
        k_p90 = row.get("k_p90")
        if k_p90 is None or float(k_p90) <= 0:
            continue
        sector_p90_by_elevation[int(row["elev_bin"])][str(sector)] = float(k_p90)
    exclusion_notes = []
    for elevation in sorted(sector_p90_by_elevation):
        east_k_p90 = sector_p90_by_elevation[elevation].get("east")
        west_k_p90 = sector_p90_by_elevation[elevation].get("west")
        if east_k_p90 is None or west_k_p90 is None:
            continue
        if elevation >= GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN:
            continue
        exclusion_notes.append(
            format_headline_exclusion_note(elevation, east_k_p90, west_k_p90)
        )
    return tuple(exclusion_notes)


def headline_eligible_elevations(rows: Sequence[Mapping[str, Any]]) -> Tuple[int, ...]:
    """Elevations that enter the headline: >= 20, both sectors, diagnosis floor."""
    sector_p90_by_elevation = defaultdict(dict)
    for row in rows:
        sector = row.get("sector")
        if sector not in ("east", "west"):
            continue
        if int(row.get("sample_count") or 0) < GHI_GEOMETRY_DIAGNOSIS_MIN_BIN_SAMPLES:
            continue
        k_p90 = row.get("k_p90")
        if k_p90 is None or float(k_p90) <= 0:
            continue
        elevation = int(row["elev_bin"])
        if elevation < GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN:
            continue
        sector_p90_by_elevation[elevation][str(sector)] = float(k_p90)
    return tuple(
        (
            elevation
            for elevation in sorted(sector_p90_by_elevation)
            if "east" in sector_p90_by_elevation[elevation]
            and "west" in sector_p90_by_elevation[elevation]
        )
    )


def _pooled_sector_k_p90(samples: Sequence[float]) -> Optional[float]:
    if len(samples) < GHI_GEOMETRY_DIAGNOSIS_MIN_BIN_SAMPLES:
        return
    k_p90 = percentile(samples, 0.9)
    if k_p90 is None or float(k_p90) <= 0:
        return None
    return float(k_p90)


def max_east_west_k_p90_ratio(
    samples_by_elev_sector: Mapping[Tuple[int, str], Sequence[float]],
) -> Tuple[Optional[float], int, Tuple[str, ...]]:
    """Diagnosis (a): pooled east vs west p90 at the same elevation.

    Not a mask (b). Overlapping counts every elevation with both sectors.
    Headline ratio uses only elev_bin >= GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN.
    Lower pairs are protocolled, not used as a silent k_p90 filter.
    """
    pooled_samples = defaultdict(lambda: {"east": [], "west": []})
    for (elevation, sector), sector_samples in samples_by_elev_sector.items():
        if sector not in ("east", "west"):
            continue
        pooled_samples[int(elevation)][sector].extend(
            (float(sample) for sample in sector_samples)
        )
    overlapping_elevations = 0
    max_contrast = None
    exclusion_notes = []
    for elevation in sorted(pooled_samples):
        samples_by_sector = pooled_samples[elevation]
        east_k_p90 = _pooled_sector_k_p90(samples_by_sector["east"])
        west_k_p90 = _pooled_sector_k_p90(samples_by_sector["west"])
        if east_k_p90 is None or west_k_p90 is None:
            continue
        overlapping_elevations += 1
        if elevation < GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN:
            exclusion_notes.append(
                format_headline_exclusion_note(elevation, east_k_p90, west_k_p90)
            )
            continue
        contrast = max(east_k_p90, west_k_p90) / min(east_k_p90, west_k_p90)
        if max_contrast is None or contrast > max_contrast:
            max_contrast = contrast
    return (max_contrast, overlapping_elevations, tuple(exclusion_notes))


def ghi_geometry_diagnosis(overlapping: int, contrast: Optional[float]) -> str:
    """Daily diagnosis label. Not a mask (b)."""
    if overlapping <= 0:
        return DIAGNOSIS_INSUFFICIENT_OVERLAP
    if contrast is None:
        return DIAGNOSIS_BELOW_HEADLINE
    return DIAGNOSIS_CONTRAST_RECORDED


def hourly_factor_shape(hourly_factors: Dict[int, float]) -> Optional[Dict[str, Any]]:
    hours = sorted((hour for hour in SHAPE_PRODUCTION_HOURS if hour in hourly_factors))
    if len(hours) < 2:
        return
    factors = [float(hourly_factors[hour]) for hour in hours]
    min_factor = min(factors)
    max_factor = max(factors)
    return {
        "min_hour": hours[0],
        "max_hour": hours[-1],
        "hour_count": len(hours),
        "min_factor": round(min_factor, 4),
        "max_factor": round(max_factor, 4),
        "spread": round(max_factor - min_factor, 4),
    }


async def persist_ghi_sensor_geometry_diagnosis(
    db: "DatabaseManager", computed_on: str
) -> Dict[str, Any]:
    """EOD diagnosis only: bin measured GHI / clear-sky. No forecast change.

    v1 scores east vs west only. Does not apply a geometry mask.
    """
    start_date = (
        date.fromisoformat(computed_on) - timedelta(days=GHI_GEOMETRY_LOOKBACK_DAYS - 1)
    ).isoformat()
    rows = await db.fetchall(
        "SELECT ac.sun_elevation_deg, ac.sun_azimuth_deg,\n                  ac.clear_sky_radiation_wm2, hwa.solar_radiation_wm2\n           FROM hourly_weather_actual hwa\n           JOIN astronomy_cache ac\n               ON hwa.date = ac.cache_date AND hwa.hour = ac.hour\n           WHERE hwa.date >= ?\n             AND hwa.date <= ?\n             AND hwa.solar_radiation_wm2 IS NOT NULL\n             AND ac.clear_sky_radiation_wm2 IS NOT NULL\n             AND ac.clear_sky_radiation_wm2 > 0\n             AND ac.sun_elevation_deg IS NOT NULL\n             AND ac.sun_azimuth_deg IS NOT NULL\n             AND ac.sun_elevation_deg >= ?",
        (start_date, computed_on, GHI_GEOMETRY_MIN_ELEVATION_DEG),
    )
    if not rows:
        await db.execute(
            "INSERT INTO ghi_sensor_geometry_daily\n               (computed_on, lookback_days, sample_hours, bin_count,\n                overlapping_elev_bins, max_east_west_k_p90_ratio, diagnosis)\n               VALUES (?, ?, 0, 0, 0, NULL, ?)\n               ON CONFLICT(computed_on) DO UPDATE SET\n                   lookback_days = excluded.lookback_days,\n                   sample_hours = excluded.sample_hours,\n                   bin_count = excluded.bin_count,\n                   overlapping_elev_bins = excluded.overlapping_elev_bins,\n                   max_east_west_k_p90_ratio = excluded.max_east_west_k_p90_ratio,\n                   diagnosis = excluded.diagnosis",
            (computed_on, GHI_GEOMETRY_LOOKBACK_DAYS, DIAGNOSIS_NO_LOCAL_GHI),
        )
        return {"status": "skipped", "reason": DIAGNOSIS_NO_LOCAL_GHI}
    samples_by_bin = defaultdict(list)
    samples_by_elev_sector = defaultdict(list)
    for row in rows:
        elevation_deg = float(row[0])
        azimuth_deg = float(row[1])
        clear_sky_ghi = float(row[2])
        measured_ghi = float(row[3])
        if clear_sky_ghi <= 0:
            continue
        k_ratio = measured_ghi / clear_sky_ghi
        elevation = elevation_bin(elevation_deg)
        samples_by_bin[elevation, azimuth_bin(azimuth_deg)].append(k_ratio)
        sector = azimuth_sector(azimuth_deg)
        if sector != "other":
            samples_by_elev_sector[elevation, sector].append(k_ratio)
    await db.execute(
        "DELETE FROM ghi_sensor_geometry_bins WHERE computed_on = ?", (computed_on,)
    )
    bin_rows = []
    for (elevation, azimuth_bin_key), bin_samples in samples_by_bin.items():
        if not bin_samples:
            continue
        bin_row = {
            "elev_bin": elevation,
            "az_bin": azimuth_bin_key,
            "sample_count": len(bin_samples),
            "k_avg": round(sum(bin_samples) / len(bin_samples), 4),
            "k_p90": round(percentile(bin_samples, 0.9) or 0.0, 4),
            "k_max": round(max(bin_samples), 4),
        }
        bin_rows.append(bin_row)
        await db.execute(
            "INSERT INTO ghi_sensor_geometry_bins\n               (computed_on, elev_bin, az_bin, sample_count, k_avg, k_p90, k_max)\n               VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                computed_on,
                bin_row["elev_bin"],
                bin_row["az_bin"],
                bin_row["sample_count"],
                bin_row["k_avg"],
                bin_row["k_p90"],
                bin_row["k_max"],
            ),
        )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS ghi_sensor_geometry_sectors (\n               computed_on DATE NOT NULL,\n               elev_bin INTEGER NOT NULL,\n               sector TEXT NOT NULL,\n               sample_count INTEGER NOT NULL,\n               k_avg REAL,\n               k_p90 REAL,\n               k_max REAL,\n               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n               PRIMARY KEY (computed_on, elev_bin, sector)\n           )"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ghi_sensor_geometry_sectors_computed\n           ON ghi_sensor_geometry_sectors(computed_on)"
    )
    await db.execute(
        "DELETE FROM ghi_sensor_geometry_sectors WHERE computed_on = ?", (computed_on,)
    )
    for (elevation, sector), bin_samples in sorted(samples_by_elev_sector.items()):
        if not bin_samples:
            continue
        await db.execute(
            "INSERT INTO ghi_sensor_geometry_sectors\n               (computed_on, elev_bin, sector, sample_count, k_avg, k_p90, k_max)\n               VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                computed_on,
                elevation,
                sector,
                len(bin_samples),
                round(sum(bin_samples) / len(bin_samples), 4),
                round(percentile(bin_samples, 0.9) or 0.0, 4),
                round(max(bin_samples), 4),
            ),
        )
    headline_contrast, overlapping_elevations, exclusion_notes = (
        max_east_west_k_p90_ratio(samples_by_elev_sector)
    )
    for note in exclusion_notes:
        _LOGGER.info("GHI sensor geometry headline: %s", note)
    diagnosis = ghi_geometry_diagnosis(overlapping_elevations, headline_contrast)
    await db.execute(
        "INSERT INTO ghi_sensor_geometry_daily\n           (computed_on, lookback_days, sample_hours, bin_count,\n            overlapping_elev_bins, max_east_west_k_p90_ratio, diagnosis)\n           VALUES (?, ?, ?, ?, ?, ?, ?)\n           ON CONFLICT(computed_on) DO UPDATE SET\n               lookback_days = excluded.lookback_days,\n               sample_hours = excluded.sample_hours,\n               bin_count = excluded.bin_count,\n               overlapping_elev_bins = excluded.overlapping_elev_bins,\n               max_east_west_k_p90_ratio = excluded.max_east_west_k_p90_ratio,\n               diagnosis = excluded.diagnosis",
        (
            computed_on,
            GHI_GEOMETRY_LOOKBACK_DAYS,
            len(rows),
            len(bin_rows),
            overlapping_elevations,
            round(headline_contrast, 4) if headline_contrast is not None else None,
            diagnosis,
        ),
    )
    _LOGGER.info(
        "GHI sensor geometry diagnosis for %s: hours=%d bins=%d overlap=%d headline_contrast=%s discarded=%d",
        computed_on,
        len(rows),
        len(bin_rows),
        overlapping_elevations,
        f"{headline_contrast:.2f}" if headline_contrast is not None else "n/a",
        len(exclusion_notes),
    )
    return {"status": "succeeded", "reason": ""}


async def prune_physics_calibration_telemetry(
    db: "DatabaseManager", today: Optional[str] = None
) -> int:
    cutoff_date = date.fromisoformat(
        today or SafeDateTimeUtil.now().date().isoformat()
    ) - timedelta(days=PHYSICS_CALIBRATION_TELEMETRY_RETENTION_DAYS)
    cutoff_iso = cutoff_date.isoformat()
    deleted_count = 0
    for table_name, date_column in _TELEMETRY_DATE_TABLES:
        table_exists = await db.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        if not table_exists:
            continue
        await db.execute(
            f'DELETE FROM "{table_name}" WHERE "{date_column}" < ?', (cutoff_iso,)
        )
        changes_row = await db.fetchone("SELECT changes()")
        deleted_count += int(changes_row[0] if changes_row else 0)
    return deleted_count
