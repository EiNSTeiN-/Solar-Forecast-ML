# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Rule-Based Forecast Strategy Module.
AI-based forecast strategy with physics fallback using database operations.
@zara
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from ..astronomy.astronomy_cache_manager import get_cache_manager
from ..core.core_helpers import SafeDateTimeUtil as dt_util_safe, get_season
from ..core.core_panel_group_forecast import (
    normalize_group_predictions_to_total,
    sync_panel_group_prediction_map,
)
from ..data.db_manager import DatabaseManager
from ..physics import (
    PhysicsEngine,
    PanelGroupCalculator,
    IrradianceData,
    SunPosition,
    PhysicsCalibrator,
)
from ..physics.poa_beam import poa_irradiance_components
from ..data.data_frost_detection import estimate_frost_from_forecast
from ..data.data_weather_radiation_ensemble import strategy_ghi_coherence_cap_allowed
from .forecast_strategy_base import (
    ForecastResult,
    ForecastStrategy,
    ForecastGenerationError,
)
from .forecast_sun_gate import should_zero_hourly_yield, zeroed_hour_production_result
from .forecast_weather_calculator import WeatherCalculator

_LOGGER = logging.getLogger(__name__)
MAX_AI_PHYSICS_DEVIATION = 0.5
BUCKET_CAL_THRESHOLD = 1.1
BUCKET_CAL_BOOST = 0.5
MAX_DYNAMIC_DEVIATION = 1.0
ENSEMBLE_LSTM_WEIGHT = 0.7
ENSEMBLE_RIDGE_WEIGHT = 0.3
DISAGREEMENT_THRESHOLD = 0.3
DISAGREEMENT_CONFIDENCE_PENALTY = 0.7
RIDGE_CONFIRMS_THRESHOLD = 0.5
TWILIGHT_MIN_ELEVATION = -2.0
TWILIGHT_MAX_ELEVATION = 5.0
LOW_SUN_MAX_ELEVATION = 15.0
TWILIGHT_MIN_DHI_WM2 = 15.0
TWILIGHT_MIN_GHI_WM2 = 8.0
LOW_SUN_CLOUD_THRESHOLD = 50
OVERCAST_AI_CAP_CLOUDS_THRESHOLD = 85.0
OVERCAST_AI_CAP_GHI_RATIO_THRESHOLD = 0.45
OVERCAST_AI_CAP_MIN_SUN_ELEVATION = 10.0
OVERCAST_AI_CAP_START_HOUR = 10
OVERCAST_AI_CAP_END_HOUR = 16
OVERCAST_AI_CAP_REFERENCE_MULTIPLIER = 1.1
OVERCAST_CLEAR_SKY_FLOOR_MAX_RATIO = 0.15
OVERCAST_CLEAR_SKY_FLOOR_MIN_RATIO = 0.05
BAD_WEATHER_AI_CLOUDS_THRESHOLD = 65.0
BAD_WEATHER_AI_MIN_SUN_ELEVATION = 5.0
BAD_WEATHER_AI_START_HOUR = 6
BAD_WEATHER_AI_END_HOUR = 18
BAD_WEATHER_AI_MODERATE_GHI_RATIO = 0.55
BAD_WEATHER_AI_STRONG_GHI_RATIO = 0.4
BAD_WEATHER_AI_SEVERE_GHI_RATIO = 0.25
BAD_WEATHER_AI_MODERATE_DIFFUSE_SHARE = 0.65
BAD_WEATHER_AI_STRONG_DIFFUSE_SHARE = 0.78
BAD_WEATHER_AI_SEVERE_DIFFUSE_SHARE = 0.88
BAD_WEATHER_AI_MODERATE_MAX_MULTIPLIER = 1.15
BAD_WEATHER_AI_STRONG_MAX_MULTIPLIER = 1.05
BAD_WEATHER_AI_SEVERE_MAX_MULTIPLIER = 0.95
BAD_WEATHER_AI_MODERATE_CONFIDENCE_MULTIPLIER = 0.7
BAD_WEATHER_AI_STRONG_CONFIDENCE_MULTIPLIER = 0.5
BAD_WEATHER_AI_SEVERE_CONFIDENCE_MULTIPLIER = 0.35
BLEND_REGIME_STABLE_GOOD = "stable_good"
BLEND_REGIME_MIXED = "mixed"
BLEND_REGIME_CLOUD_EDGE = "cloud_edge"
BLEND_REGIME_BAD_WEATHER = "bad_weather"
WEATHER_REGIME_RAIN_OVERCAST = "rain_overcast"
WEATHER_REGIME_LOW_GHI_OVERCAST = "low_ghi_overcast"
WEATHER_REGIME_DIFFUSE_OVERCAST = "diffuse_overcast"
WEATHER_REGIME_STABLE_GOOD = "stable_good"
WEATHER_REGIME_BRIGHT_MIXED = "bright_mixed"
WEATHER_REGIME_DRY_CLOUD_EDGE = "dry_cloud_edge"
WEATHER_REGIME_LOW_SUN_UNCERTAIN = "low_sun_uncertain"
WEATHER_REGIME_MIXED_UNCERTAIN = "mixed_uncertain"
GOOD_WEATHER_MIN_AI_FLOOR = 0.3
MIXED_WEATHER_MIN_AI_FLOOR = 0.3
CLOUD_EDGE_MIN_AI_FLOOR = 0.15
BAD_WEATHER_BLEND_SUPPORTED_AI_FLOOR = 0.0
MORNING_AI_UNDERBLEND_HOUR_START = 6
MORNING_AI_UNDERBLEND_HOUR_END = 11
MORNING_AI_UNDERBLEND_MIN_SUN_ELEVATION = 3.0
MORNING_AI_UNDERBLEND_MAX_SUN_ELEVATION = 55.0
MORNING_AI_UNDERBLEND_MIN_PHYSICS_KWH = 0.02
MORNING_AI_UNDERBLEND_MIN_AI_KWH = 0.05
MORNING_AI_UNDERBLEND_MIN_AI_PHYSICS_RATIO = 1.3
MORNING_AI_UNDERBLEND_CONFIDENCE_FLOOR = 0.55
MORNING_AI_UNDERBLEND_CONFIDENCE_CEILING = 0.78
MORNING_AI_UNDERBLEND_EARLY_HOUR_END = 7
MORNING_AI_UNDERBLEND_EARLY_CONFIDENCE_CAP = 0.48
MORNING_AI_UNDERBLEND_MAX_RAIN_MM = 0.5
SIMILAR_WEATHER_RAIN_GATE_MM = 0.3
BAD_WEATHER_RAIN_MIN_MM = 0.2
BAD_WEATHER_RAIN_CLOUDS_THRESHOLD = 55.0
BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD = 80.0
BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD = 0.45
BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD = 0.72
BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD = 80.0
BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD = 0.4
BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD = 70.0
BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD = 0.82
BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD = 70.0
BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD = 0.35
BAD_WEATHER_TFS_RAIN_OVERCAST_MAX_WEIGHT = 0.12
BAD_WEATHER_TFS_LOW_GHI_MAX_WEIGHT = 0.12
BAD_WEATHER_TFS_DIFFUSE_MAX_WEIGHT = 0.15
BAD_WEATHER_TFS_LOW_SUN_MAX_WEIGHT = 0.08
BAD_WEATHER_TFS_FALLBACK_MAX_WEIGHT = 0.18
REGIME_METHOD_SOFT_MIN_SAMPLES = 6
REGIME_METHOD_ACTIVE_MIN_SAMPLES = 30
REGIME_TFS_SOFT_MIN_SAMPLES = 8
REGIME_TFS_ACTIVE_MIN_SAMPLES = 30
REGIME_BIAS_SOFT_MIN_SAMPLES = 10
REGIME_BIAS_ACTIVE_MIN_SAMPLES = 40
REGIME_BIAS_MIN_KWH = 0.03
REGIME_TFS_BAD_WEATHER_MAX_WEIGHT = 0.2
REGIME_TFS_ADVANTAGE_RATIO = 0.9
REGIME_TFS_RISK_RATIO = 1.12
REGIME_TFS_MAX_POSITIVE_BIAS_KWH = 0.08
SIMILAR_WEATHER_MIN_SAMPLES = 3
SIMILAR_WEATHER_MIN_UNDERFORECAST_SHARE = 0.62
SIMILAR_WEATHER_MIN_PRED_FACTOR_Q25 = 1.05
SIMILAR_WEATHER_MIN_PHYSICS_FACTOR_Q25 = 1.1
SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_SHARE = 0.3
SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_PRED_MEDIAN = 0.85
SIMILAR_WEATHER_MAX_WEIGHT_SUM_FOR_THIN_HISTORY = 1.0
SIMILAR_WEATHER_MAX_FACTOR_THIN = 1.25
SIMILAR_WEATHER_MAX_FACTOR_MEDIUM = 1.45
SIMILAR_WEATHER_MAX_FACTOR_STRONG = 1.65
SIMILAR_WEATHER_MAX_PHYSICS_FACTOR = 2.8
SIMILAR_WEATHER_MAX_TFS_CAP = 0.45
SIMILAR_WEATHER_MAX_ANCHOR_CONFIDENCE = 0.55
BAD_WEATHER_CONSENSUS_MIN_AI_PHYSICS_RATIO = 1.2
BAD_WEATHER_CONSENSUS_MIN_TFS_PHYSICS_RATIO = 1.3
BAD_WEATHER_CONSENSUS_MIN_AGREEMENT = 0.5
BAD_WEATHER_CONSENSUS_MIN_SUN_ELEVATION = 8.0
BAD_WEATHER_CONSENSUS_NEUTRAL_TFS_CAP = 0.48
BAD_WEATHER_CONSENSUS_SUPPORTED_TFS_CAP = 0.6
BAD_WEATHER_CONSENSUS_MAX_TFS_CAP = 0.62
DAILY_BIAS_MIN_DAYS = 3
DAILY_BIAS_MIN_UNDERFORECAST_SHARE = 0.65
DAILY_BIAS_MIN_OVERFORECAST_SHARE = 0.65
DAILY_BIAS_MIN_FACTOR_Q25 = 1.08
DAILY_BIAS_MAX_FACTOR_Q75 = 0.92
DAILY_BIAS_MAX_UP_FACTOR_THIN = 1.6
DAILY_BIAS_MAX_UP_FACTOR_STRONG = 1.85
DAILY_BIAS_MIN_DOWN_FACTOR = 0.72
DAILY_BIAS_LOOKBACK_DAYS = 14
DAILY_BIAS_MAX_EXCLUDED_PRODUCTION_SHARE = 0.5
DAILY_BIAS_DISAGREEMENT_UNDERFORECAST_FACTOR = 1.1
DAILY_BIAS_MIN_SIMILARITY_WEIGHT = 0.25
DAILY_BIAS_HARD_DROP_REASONS = frozenset({"suspected_battery_curtailment"})
DAILY_BIAS_CURTAILMENT_REASONS = frozenset(
    {
        "transition_curtailment",
        "full_battery_zero_export",
        "full_battery_export_limit",
        "demand_limited_zero_export",
        "suspected_battery_curtailment",
    }
)
DAILY_FORECAST_CLEAR_SKY_CAP_BUFFER = 1.08
DAILY_FORECAST_CLEAR_SKY_MIN_KWH_PER_KWP = 2.5
DAILY_FORECAST_HARD_CAP_KWH_PER_KWP = 8.5
DAILY_BIAS_RECENT_GUARD_LOOKBACK_DAYS = 7
DAILY_BIAS_RECENT_GUARD_MIN_SIMILAR_DAYS = 2
DAILY_BIAS_RECENT_GUARD_MIN_BASE_RATIO = 0.55
DAILY_BIAS_RECENT_GUARD_MAX_BASE_RATIO = 1.75
DAILY_BIAS_RECENT_GUARD_MAX_MEDIAN_BASE_RATIO = 1.18
DAILY_BIAS_RECENT_GUARD_MIN_MEDIAN_UPLIFT = 1.08
DAILY_BIAS_RECENT_GUARD_MIN_DEGRADATION_KWH = 0.15
DAILY_BIAS_RECENT_GUARD_DEGRADATION_RATIO = 0.05
DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_KWH = 0.5
DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_RATIO = 0.15
MIXED_CLOUD_EDGE_TFS_UPSIDE_RATIO = 1.35
MIXED_CLOUD_EDGE_TFS_PHYSICS_RATIO = 1.6
MIXED_CLOUD_EDGE_TFS_BASE_TARGET_WEIGHT = 0.66
MIXED_CLOUD_EDGE_TFS_MEDIUM_TARGET_WEIGHT = 0.72
MIXED_CLOUD_EDGE_TFS_STRONG_TARGET_WEIGHT = 0.78
MIXED_CLOUD_EDGE_TFS_MEDIUM_STRENGTH = 1.2
MIXED_CLOUD_EDGE_TFS_STRONG_STRENGTH = 1.5
MIXED_CLOUD_EDGE_TFS_MAX_HUMIDITY = 78.0
MIXED_CLOUD_EDGE_TFS_MIN_SUN_ELEVATION = 12.0
MIXED_CLOUD_EDGE_TFS_MAX_RAIN = 0.05
TFS_BASE_WEIGHT = 0.35
TFS_MIN_WEIGHT = 0.1
TFS_MAX_WEIGHT = 0.55
TFS_MAE_LOOKBACK_DAYS = 7
TFS_MAE_MIN_SAMPLES = 5
TFS_MAE_CACHE_TTL_SECONDS = 21600
TFS_LOW_SUN_ENVELOPE_RATIO = 1.6
TFS_LOW_SUN_ENVELOPE_MIN_REFERENCE_KWH = 0.05
TFS_LOW_SUN_ENVELOPE_TINY_ALLOWANCE_KWH = 0.15
TFS_LOW_SUN_ENVELOPE_MIN_MULTIPLIER = 0.2
TFS_LOW_SUN_ENVELOPE_MIN_WEIGHT = 0.02
TFS_OVERCAST_BONUS_CLOUDS_THRESHOLD = 65.0
TFS_OVERCAST_BONUS_FULL_CLOUDS = 90.0
TFS_OVERCAST_BONUS_MULTIPLIER = 1.12
TFS_OVERCAST_MAX_WEIGHT = 0.62
TFS_CLOUD_MULTIPLIERS: Dict[str, float] = {
    "overcast": 1.4,
    "stratus": 1.3,
    "fog": 1.3,
    "fog_light": 1.2,
    "mixed": 1.0,
    "cirrus": 0.9,
    "fair": 0.8,
    "clear": 0.7,
    "snow": 1.0,
}
_tfs_mae_cache: Dict[str, Tuple[float, float]] = {}


async def _compute_tfs_mae_factor(db: DatabaseManager) -> float:
    """Return a multiplier based on 7-day TFS vs SFML accuracy.

    Queries the SFML-owned TFS comparison table written by the end-of-day
    workflow. Returns 1.0 (neutral) if not enough samples are present.
    """
    import time as time_module

    now_seconds = time_module.time()
    cached_factor = _tfs_mae_cache.get("factor")
    if cached_factor and now_seconds - cached_factor[1] < TFS_MAE_CACHE_TTL_SECONDS:
        return cached_factor[0]
    try:
        database_rows = await db.fetchall(
            "SELECT sfml_accuracy_percent, tfs_accuracy_percent FROM sfml_tfs_forecast_comparison WHERE date >= date('now', '-' || ? || ' days') AND tfs_accuracy_percent IS NOT NULL ORDER BY date DESC LIMIT ?",
            (TFS_MAE_LOOKBACK_DAYS, TFS_MAE_LOOKBACK_DAYS),
        )
    except Exception as error:
        _LOGGER.debug("TFS MAE factor fetch failed: %s", error)
        _tfs_mae_cache["factor"] = (1.0, now_seconds)
        return 1.0
    accuracy_pairs = [
        (row[0], row[1])
        for row in database_rows
        if row[0] is not None
        if row[1] is not None
    ]
    if len(accuracy_pairs) < TFS_MAE_MIN_SAMPLES:
        _tfs_mae_cache["factor"] = (1.0, now_seconds)
        return 1.0
    sfml_accuracy_percent = sum(
        (accuracy_pair[0] for accuracy_pair in accuracy_pairs)
    ) / len(accuracy_pairs)
    tfs_accuracy_percent = sum(
        (accuracy_pair[1] for accuracy_pair in accuracy_pairs)
    ) / len(accuracy_pairs)
    if sfml_accuracy_percent <= 1.0 or tfs_accuracy_percent <= 1.0:
        _tfs_mae_cache["factor"] = (1.0, now_seconds)
        return 1.0
    sfml_error_percent = max(0.1, 100.0 - sfml_accuracy_percent)
    tfs_error_percent = max(0.1, 100.0 - tfs_accuracy_percent)
    ratio = tfs_error_percent / sfml_error_percent
    factor = max(0.7, min(1.4, 1.5 - 0.5 * ratio))
    _tfs_mae_cache["factor"] = (factor, now_seconds)
    return factor


def _compute_tfs_cloud_factor(tfs_hour_data: Dict[str, Any]) -> float:
    """Return a multiplier based on the hour's cloud_type from TFS."""
    if not tfs_hour_data:
        return 1.0
    cloud_type = tfs_hour_data.get("cloud_type") or tfs_hour_data.get(
        "weather_condition"
    )
    if not cloud_type:
        return 1.0
    return TFS_CLOUD_MULTIPLIERS.get(str(cloud_type).lower(), 1.0)


async def _compute_tfs_weight(
    db: Optional[DatabaseManager],
    tfs_hour_data: Optional[Dict[str, Any]],
    local_clouds: Optional[float],
    group_name: Optional[str],
    hour: Optional[int],
    adaptive_enabled: bool = True,
) -> float:
    """Combine base weight with adaptive multipliers into an effective blend weight."""
    if not adaptive_enabled:
        return TFS_BASE_WEIGHT
    mae_factor = 1.0
    if db is not None:
        mae_factor = await _compute_tfs_mae_factor(db)
    cloud_factor = _compute_tfs_cloud_factor(tfs_hour_data or {})
    weight = TFS_BASE_WEIGHT * mae_factor * cloud_factor
    max_weight = TFS_MAX_WEIGHT
    if local_clouds is not None and local_clouds > TFS_OVERCAST_BONUS_CLOUDS_THRESHOLD:
        overcast_bonus_span = max(
            1.0, TFS_OVERCAST_BONUS_FULL_CLOUDS - TFS_OVERCAST_BONUS_CLOUDS_THRESHOLD
        )
        overcast_bonus_progress = min(
            1.0,
            max(
                0.0,
                (local_clouds - TFS_OVERCAST_BONUS_CLOUDS_THRESHOLD)
                / overcast_bonus_span,
            ),
        )
        if overcast_bonus_progress > 0:
            weight *= (
                1.0 + (TFS_OVERCAST_BONUS_MULTIPLIER - 1.0) * overcast_bonus_progress
            )
            max_weight = TFS_OVERCAST_MAX_WEIGHT
    return max(TFS_MIN_WEIGHT, min(max_weight, weight))


def _apply_tfs_low_sun_envelope(
    weight: float, tfs_kwh: float, reference_kwh: float, sun_elevation: Optional[float]
) -> float:
    if weight <= 0 or tfs_kwh <= 0:
        return weight
    if sun_elevation is None or sun_elevation > LOW_SUN_MAX_ELEVATION:
        return weight
    reference_prediction_kwh = max(0.0, reference_kwh or 0.0)
    if reference_prediction_kwh < TFS_LOW_SUN_ENVELOPE_MIN_REFERENCE_KWH:
        if tfs_kwh <= TFS_LOW_SUN_ENVELOPE_TINY_ALLOWANCE_KWH:
            return weight
        return min(weight, TFS_LOW_SUN_ENVELOPE_MIN_WEIGHT)
    tfs_reference_ratio = tfs_kwh / reference_prediction_kwh
    if tfs_reference_ratio <= TFS_LOW_SUN_ENVELOPE_RATIO:
        return weight
    envelope_multiplier = max(
        TFS_LOW_SUN_ENVELOPE_MIN_MULTIPLIER,
        TFS_LOW_SUN_ENVELOPE_RATIO / tfs_reference_ratio,
    )
    return max(TFS_LOW_SUN_ENVELOPE_MIN_WEIGHT, weight * envelope_multiplier)


def _hour_bucket(hour: int) -> str:
    return "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"


def _rain_band(rain: float) -> int:
    if rain < 0.05:
        return 0
    if rain < 0.2:
        return 1
    if rain < 1.0:
        return 2
    return 3


def daily_bias_history_start_date(
    target_date: str, lookback_days: int = DAILY_BIAS_LOOKBACK_DAYS
) -> str:
    """Return the inclusive start date for published-lock bias history."""
    target_day = datetime.strptime(str(target_date), "%Y-%m-%d").date()
    history_days = max(1, int(lookback_days))
    return (target_day - timedelta(days=history_days)).isoformat()


def daily_bias_day_should_drop(
    hours: List[Dict[str, Any]],
    *,
    max_excluded_share: float = DAILY_BIAS_MAX_EXCLUDED_PRODUCTION_SHARE,
) -> bool:
    """Drop majority-quarantined or hard-curtailment days from lock-vs-actual history."""
    production_hours = [
        hour_entry
        for hour_entry in hours
        if float(hour_entry.get("prediction_kwh") or 0.0) > 0.05
    ]
    if not production_hours:
        return True
    excluded_count = 0
    curtailment_count = 0
    for hour_entry in production_hours:
        exclusion_reason = str(
            hour_entry.get("data_quarantine_reason")
            or hour_entry.get("mppt_throttle_reason")
            or ""
        ).strip()
        if exclusion_reason in DAILY_BIAS_HARD_DROP_REASONS:
            return True
        if hour_entry.get("exclude_from_learning"):
            excluded_count += 1
        if exclusion_reason in DAILY_BIAS_CURTAILMENT_REASONS:
            curtailment_count += 1
    production_hours_count = len(production_hours)
    excluded_share_limit = float(max_excluded_share)
    return (
        excluded_count / production_hours_count >= excluded_share_limit
        or curtailment_count / production_hours_count >= excluded_share_limit
    )


def resolve_daily_overforecast_bias_factor(
    *,
    overforecast_share: float,
    factor_median: float,
    min_overforecast_share: float = DAILY_BIAS_MIN_OVERFORECAST_SHARE,
    min_down_factor: float = DAILY_BIAS_MIN_DOWN_FACTOR,
) -> Optional[float]:
    """Scale a published lock down from the similar-sample median."""
    if overforecast_share < min_overforecast_share or factor_median >= 0.88:
        return None
    return max(float(min_down_factor), min(1.0, float(factor_median)))


def daily_bias_hour_is_clean_sample(hour: Dict[str, Any]) -> bool:
    """Reject curtailment/quarantine hours from residual bias samples."""
    if hour.get("exclude_from_learning"):
        return False
    exclusion_reason = str(
        hour.get("data_quarantine_reason") or hour.get("mppt_throttle_reason") or ""
    ).strip()
    return exclusion_reason not in DAILY_BIAS_CURTAILMENT_REASONS


def daily_bias_hourly_lock_factors(
    hours: List[Dict[str, Any]], *, clean_only: bool = True
) -> Dict[int, float]:
    """Map production hours to published-lock vs actual factors."""
    hour_lock_factors = {}
    for hour_entry in hours:
        prediction_kwh = float(hour_entry.get("prediction_kwh") or 0.0)
        if prediction_kwh <= 0.05:
            continue
        if clean_only and (not daily_bias_hour_is_clean_sample(hour_entry)):
            continue
        hour_lock_factors[int(hour_entry.get("hour") or 0)] = (
            float(hour_entry.get("actual_kwh") or 0.0) / prediction_kwh
        )
    return hour_lock_factors


def _weighted_quantile(
    values: List[float], weights: List[float], quantile: float
) -> Optional[float]:
    if not values or not weights or len(values) != len(weights):
        return None
    weighted_pairs = sorted(zip(values, weights), key=lambda item: item[0])
    weight_sum = sum((max(0.0, weight) for sample_value, weight in weighted_pairs))
    if weight_sum <= 0:
        return
    cumulative_weight = 0.0
    for value, weight in weighted_pairs:
        cumulative_weight += max(0.0, weight)
        if not cumulative_weight / weight_sum >= quantile:
            continue
        return value
    return weighted_pairs[-1][0]


def resolve_hourly_overforecast_bias_factors(
    hour_samples: Dict[int, List[Tuple[float, float]]],
    *,
    min_days: int = DAILY_BIAS_MIN_DAYS,
    disagreement_underforecast_factor: float = DAILY_BIAS_DISAGREEMENT_UNDERFORECAST_FACTOR,
) -> Dict[int, float]:
    """Down-scale only hours where similar days agree the lock overshot."""
    hourly_bias_factors = {}
    for hour, samples in hour_samples.items():
        if len(samples) < min_days:
            continue
        factors = [float(factor_value) for factor_value, unused_sample_part in samples]
        weights = [float(weight) for unused_sample_part, weight in samples]
        weight_sum = sum((max(0.0, weight) for weight in weights))
        if not (
            weight_sum <= 0 or max(factors) >= float(disagreement_underforecast_factor)
        ):
            median_factor = _weighted_quantile(factors, weights, 0.5)
            if median_factor is not None:
                overforecast_share = (
                    sum(
                        (
                            weight
                            for factor_value, weight in samples
                            if float(factor_value) < 0.9
                        )
                    )
                    / weight_sum
                )
                factor = resolve_daily_overforecast_bias_factor(
                    overforecast_share=overforecast_share, factor_median=median_factor
                )
                if factor is not None:
                    hourly_bias_factors[int(hour)] = round(float(factor), 4)
    return hourly_bias_factors


def similar_weather_underforecast_is_rain_gated(
    *, rain: Optional[float], weather_regime: str
) -> bool:
    """Gate underforecast lift on meaningful rain only — not regime label alone."""
    del weather_regime
    try:
        rain_mm = float(rain) if rain is not None else 0.0
    except (TypeError, ValueError):
        rain_mm = 0.0
    return rain_mm > float(SIMILAR_WEATHER_RAIN_GATE_MM)


def resolve_morning_ai_underblend_confidence(
    *,
    hour: int,
    sun_elevation: float,
    physics_kwh: float,
    ai_kwh: float,
    blend_regime: str,
    rain: float,
    current_confidence: float,
) -> Optional[float]:
    """Raise morning AI weight when physics under-blends a clearly higher AI.

    Portable ratio gate for all plants: only acts when AI/physics is decisive,
    weather is not a true bad-weather blend, and rain is not material.
    Early low-sun hours stay capped to avoid dawn overshoot.
    """
    if (
        int(hour) < MORNING_AI_UNDERBLEND_HOUR_START
        or int(hour) > MORNING_AI_UNDERBLEND_HOUR_END
    ):
        return None
    if float(sun_elevation) < MORNING_AI_UNDERBLEND_MIN_SUN_ELEVATION:
        return
    if float(sun_elevation) > MORNING_AI_UNDERBLEND_MAX_SUN_ELEVATION:
        return
    if blend_regime == BLEND_REGIME_BAD_WEATHER:
        return
    try:
        rain_mm = float(rain) if rain is not None else 0.0
    except (TypeError, ValueError):
        rain_mm = 0.0
    if rain_mm > float(MORNING_AI_UNDERBLEND_MAX_RAIN_MM):
        return
    physics_value_kwh = float(physics_kwh or 0.0)
    ai_value_kwh = float(ai_kwh or 0.0)
    if physics_value_kwh < float(MORNING_AI_UNDERBLEND_MIN_PHYSICS_KWH):
        return
    if ai_value_kwh < float(MORNING_AI_UNDERBLEND_MIN_AI_KWH):
        return
    ai_physics_ratio = ai_value_kwh / physics_value_kwh
    if ai_physics_ratio < float(MORNING_AI_UNDERBLEND_MIN_AI_PHYSICS_RATIO):
        return
    confidence_progress = min(
        1.0, (ai_physics_ratio - MORNING_AI_UNDERBLEND_MIN_AI_PHYSICS_RATIO) / 0.7
    )
    confidence_target = MORNING_AI_UNDERBLEND_CONFIDENCE_FLOOR + confidence_progress * (
        MORNING_AI_UNDERBLEND_CONFIDENCE_CEILING
        - MORNING_AI_UNDERBLEND_CONFIDENCE_FLOOR
    )
    if int(hour) <= MORNING_AI_UNDERBLEND_EARLY_HOUR_END or float(sun_elevation) < 15.0:
        confidence_target = min(
            confidence_target, float(MORNING_AI_UNDERBLEND_EARLY_CONFIDENCE_CAP)
        )
    current_confidence_value = max(0.0, float(current_confidence or 0.0))
    if current_confidence_value >= confidence_target - 0.01:
        return
    return round(float(confidence_target), 4)


def daily_bias_similarity_weight(
    current: Dict[str, Any], historic: Dict[str, Any]
) -> float:
    """Weight similar published-lock days by season, weather regime, and daypart shape."""
    if current.get("season") != historic.get("season"):
        return 0.0
    similarity_weight = 1.0
    if current.get("primary_regime") != historic.get("primary_regime"):
        if current.get("primary_blend") == historic.get("primary_blend"):
            similarity_weight *= 0.4
        else:
            return 0.0
    for field_name, similarity_tolerance, similarity_scale in (
        ("bad_weather_share", 1.0, 0.2),
        ("rain_share", 1.2, 0.35),
        ("low_ghi_share", 1.2, 0.35),
        ("diffuse_share", 1.0, 0.45),
        ("morning_share", 1.1, 0.4),
        ("midday_share", 1.1, 0.4),
        ("afternoon_share", 1.1, 0.4),
    ):
        distance = abs(
            float(current.get(field_name) or 0.0)
            - float(historic.get(field_name) or 0.0)
        )
        similarity_weight *= max(
            similarity_scale, 1.0 - distance * similarity_tolerance
        )
    return similarity_weight


def _learning_strength(sample_count: int, *, soft_min: int, full_samples: int) -> float:
    if sample_count < soft_min:
        return 0.0
    if full_samples <= soft_min:
        return 1.0
    return max(
        0.0, min(1.0, (sample_count - soft_min + 1) / (full_samples - soft_min + 1))
    )


def _similar_weather_weight(
    *, current: Dict[str, Any], historic: Dict[str, Any]
) -> float:
    if current.get("weather_regime") != historic.get("weather_regime"):
        return 0.0
    if current.get("season") != historic.get("season"):
        return 0.0
    if current.get("hour_bucket") != historic.get("hour_bucket"):
        return 0.0
    hour_distance = abs(float(current["hour"]) - float(historic["hour"])) / 3.0
    hour_distance += (
        abs(float(current["sun_elevation"]) - float(historic["sun_elevation"])) / 18.0
    )
    hour_distance += abs(float(current["clouds"]) - float(historic["clouds"])) / 25.0
    hour_distance += (
        abs(_rain_band(float(current["rain"])) - _rain_band(float(historic["rain"])))
        * 0.65
    )
    current_ghi_ratio = current.get("ghi_ratio")
    historic_ghi_ratio = historic.get("ghi_ratio")
    if current_ghi_ratio is not None and historic_ghi_ratio is not None:
        hour_distance += (
            abs(float(current_ghi_ratio) - float(historic_ghi_ratio)) / 0.18
        )
    current_diffuse_share = current.get("diffuse_share")
    historic_diffuse_share = historic.get("diffuse_share")
    if current_diffuse_share is not None and historic_diffuse_share is not None:
        hour_distance += (
            abs(float(current_diffuse_share) - float(historic_diffuse_share)) / 0.2
        )
    return math.exp(-hour_distance)


def _scale_group_predictions(
    groups: Optional[List[Dict[str, Any]]], ratio: float
) -> Optional[List[Dict[str, Any]]]:
    if not groups or not math.isfinite(ratio) or ratio < 0:
        return groups
    scaled_groups = []
    for group in groups:
        scaled_group = dict(group)
        group_power_kwh = scaled_group.get("power_kwh")
        if group_power_kwh is not None:
            scaled_group["power_kwh"] = round(float(group_power_kwh) * ratio, 4)
        scaled_groups.append(scaled_group)
    return scaled_groups


def _cap_ai_for_dense_overcast(
    ai_pred: float,
    physics_pred: float,
    tfs_pred: float,
    clouds: float,
    ghi: float,
    clear_sky: float,
    sun_elevation: float,
    hour: int,
) -> tuple[float, Optional[float]]:
    """Cap AI output in dense overcast peak-solar situations."""
    if (
        ai_pred <= 0
        or physics_pred <= 0
        or clouds < OVERCAST_AI_CAP_CLOUDS_THRESHOLD
        or (sun_elevation < OVERCAST_AI_CAP_MIN_SUN_ELEVATION)
        or (hour < OVERCAST_AI_CAP_START_HOUR)
        or (hour > OVERCAST_AI_CAP_END_HOUR)
    ):
        return (ai_pred, None)
    ghi_ratio = None
    if clear_sky > 50 and ghi > 0:
        ghi_ratio = min(ghi / clear_sky, 1.05)
    if ghi_ratio is not None and ghi_ratio > OVERCAST_AI_CAP_GHI_RATIO_THRESHOLD:
        return (ai_pred, ghi_ratio)
    reference_prediction_kwh = max(physics_pred, tfs_pred)
    if reference_prediction_kwh <= 0:
        return (ai_pred, ghi_ratio)
    ai_cap_kwh = reference_prediction_kwh * OVERCAST_AI_CAP_REFERENCE_MULTIPLIER
    if ai_pred <= ai_cap_kwh:
        return (ai_pred, ghi_ratio)
    return (ai_cap_kwh, ghi_ratio)


def _classify_blend_weather_regime(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify the forecast hour for blend policy, not for display."""
    ghi_ratio = None
    if clear_sky > 50 and ghi >= 0:
        ghi_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_share = None
    if ghi > 5 and dhi >= 0:
        diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
    rainy_overcast = rain >= BAD_WEATHER_RAIN_MIN_MM and (
        clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
        or humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD
        or (ghi_ratio is not None and ghi_ratio <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD)
        or (
            diffuse_share is not None
            and diffuse_share >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
        )
    )
    low_ghi_overcast = clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD and (
        ghi_ratio is None or ghi_ratio <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD
    )
    diffuse_overcast = (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and diffuse_share is not None
        and (diffuse_share >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    )
    low_sun_overcast = (
        sun_elevation <= 8.0
        and clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD
        and (ghi_ratio is None or ghi_ratio <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD)
    )
    if rainy_overcast or low_ghi_overcast or diffuse_overcast or low_sun_overcast:
        return BLEND_REGIME_BAD_WEATHER
    if clouds < 35.0:
        if ghi_ratio is None or ghi_ratio >= 0.45:
            return BLEND_REGIME_STABLE_GOOD
    if clouds >= 65.0:
        return BLEND_REGIME_CLOUD_EDGE
    return BLEND_REGIME_MIXED


def _classify_static_forecast_weather_regime(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify the static forecast hour for persisted learning."""
    ghi_ratio = None
    if clear_sky > 50 and ghi >= 0:
        ghi_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_share = None
    if ghi > 5 and dhi >= 0:
        diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
    if sun_elevation <= 8.0:
        if not not clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD or (
            ghi_ratio is not None
            and ghi_ratio <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD
        ):
            return WEATHER_REGIME_LOW_SUN_UNCERTAIN
    if rain >= BAD_WEATHER_RAIN_MIN_MM:
        if not (
            not clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
            and (not humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD)
            and (
                ghi_ratio is None
                or not ghi_ratio <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD
            )
        ) or (
            diffuse_share is not None
            and diffuse_share >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
        ):
            return WEATHER_REGIME_RAIN_OVERCAST
    if clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD:
        if ghi_ratio is None or ghi_ratio <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD:
            return WEATHER_REGIME_LOW_GHI_OVERCAST
    if (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and diffuse_share is not None
        and (diffuse_share >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    ):
        return WEATHER_REGIME_DIFFUSE_OVERCAST
    if clouds < 35.0:
        if ghi_ratio is None or ghi_ratio >= 0.45:
            return WEATHER_REGIME_STABLE_GOOD
    if rain <= 0.05 and clouds >= 60.0:
        if ghi_ratio is None or ghi_ratio <= 0.5:
            return WEATHER_REGIME_DRY_CLOUD_EDGE
    if rain <= 0.05 and ghi_ratio is not None and (ghi_ratio >= 0.5):
        return WEATHER_REGIME_BRIGHT_MIXED
    return WEATHER_REGIME_MIXED_UNCERTAIN


class StaticForecastBlendPolicy:
    """Central policy for the static morning forecast blend."""

    @staticmethod
    def _bad_weather_tfs_basis_cap(
        *, blend_regime: str, weather_regime: Optional[str]
    ) -> Optional[float]:
        if blend_regime != BLEND_REGIME_BAD_WEATHER:
            return
        if weather_regime == WEATHER_REGIME_LOW_SUN_UNCERTAIN:
            return BAD_WEATHER_TFS_LOW_SUN_MAX_WEIGHT
        if weather_regime == WEATHER_REGIME_RAIN_OVERCAST:
            return BAD_WEATHER_TFS_RAIN_OVERCAST_MAX_WEIGHT
        if weather_regime == WEATHER_REGIME_LOW_GHI_OVERCAST:
            return BAD_WEATHER_TFS_LOW_GHI_MAX_WEIGHT
        if weather_regime == WEATHER_REGIME_DIFFUSE_OVERCAST:
            return BAD_WEATHER_TFS_DIFFUSE_MAX_WEIGHT
        return BAD_WEATHER_TFS_FALLBACK_MAX_WEIGHT

    @staticmethod
    def _bad_weather_consensus_tfs_cap(
        *,
        blend_regime: str,
        similar_weather: Optional[Dict[str, Any]],
        physics_pred: float,
        ai_pred: float,
        tfs_pred: float,
        sun_elevation: float,
    ) -> Optional[float]:
        if blend_regime != BLEND_REGIME_BAD_WEATHER:
            return
        if physics_pred <= 0 or ai_pred <= 0 or tfs_pred <= 0:
            return None
        if sun_elevation < BAD_WEATHER_CONSENSUS_MIN_SUN_ELEVATION:
            return
        if similar_weather and similar_weather.get("tfs_risk_signal"):
            return
        ai_physics_ratio = ai_pred / physics_pred
        tfs_physics_ratio = tfs_pred / physics_pred
        if ai_physics_ratio < BAD_WEATHER_CONSENSUS_MIN_AI_PHYSICS_RATIO:
            return
        if tfs_physics_ratio < BAD_WEATHER_CONSENSUS_MIN_TFS_PHYSICS_RATIO:
            return
        model_agreement = min(ai_pred, tfs_pred) / max(ai_pred, tfs_pred)
        if model_agreement < BAD_WEATHER_CONSENSUS_MIN_AGREEMENT:
            return
        consensus_weight_cap = BAD_WEATHER_CONSENSUS_NEUTRAL_TFS_CAP
        if similar_weather:
            if similar_weather.get("tfs_support_signal"):
                consensus_weight_cap = BAD_WEATHER_CONSENSUS_SUPPORTED_TFS_CAP
            if similar_weather.get("underforecast_signal"):
                try:
                    confidence = float(similar_weather.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                consensus_weight_cap = max(
                    consensus_weight_cap,
                    min(BAD_WEATHER_CONSENSUS_MAX_TFS_CAP, 0.48 + confidence * 0.25),
                )
        if (
            ai_physics_ratio >= 1.7
            and tfs_physics_ratio >= 1.8
            and (model_agreement >= 0.7)
        ):
            consensus_weight_cap = max(consensus_weight_cap, 0.6)
        return min(BAD_WEATHER_CONSENSUS_MAX_TFS_CAP, consensus_weight_cap)

    @staticmethod
    def should_apply_bad_weather_ai_guard(blend_regime: str) -> bool:
        return blend_regime == BLEND_REGIME_BAD_WEATHER

    @staticmethod
    def calculate_ai_floor(learned: Dict[str, Any], blend_regime: str) -> float:
        if not learned or learned.get("sample_count", 0) < 10:
            return 0.0
        try:
            ai_advantage_factor = float(
                learned.get("advantage_factor", learned.get("ai_advantage_factor", 1.0))
                or 1.0
            )
        except (TypeError, ValueError):
            ai_advantage_factor = 1.0
        advantage_floor = min(0.92, max(0.0, 0.5 + (ai_advantage_factor - 1.0) * 0.4))
        physics_mae = learned.get("physics_mae")
        ai_mae = learned.get("ai_mae")
        blend_mae = learned.get("blend_mae")
        if physics_mae is None or ai_mae is None:
            return advantage_floor
        try:
            physics_mae = float(physics_mae)
            ai_mae = float(ai_mae)
            blend_mae = float(blend_mae) if blend_mae is not None else None
        except (TypeError, ValueError):
            return advantage_floor
        if (
            not math.isfinite(physics_mae)
            or not math.isfinite(ai_mae)
            or physics_mae <= 0
            or (ai_mae <= 0)
        ):
            return advantage_floor
        blend_better_than_physics = (
            blend_mae is not None
            and math.isfinite(blend_mae)
            and (blend_mae > 0)
            and (blend_mae < physics_mae)
        )
        if blend_regime == BLEND_REGIME_STABLE_GOOD:
            return max(GOOD_WEATHER_MIN_AI_FLOOR, min(advantage_floor, 0.5))
        if blend_regime == BLEND_REGIME_BAD_WEATHER:
            if ai_mae > physics_mae:
                return 0.0
            inverse_error_sum = 1.0 / physics_mae + 1.0 / ai_mae
            mae_based_ai_weight = 1.0 / ai_mae / inverse_error_sum
            return min(advantage_floor, mae_based_ai_weight, 0.2)
        if blend_regime == BLEND_REGIME_MIXED:
            if blend_better_than_physics:
                return max(MIXED_WEATHER_MIN_AI_FLOOR, min(advantage_floor, 0.5))
            if ai_mae > physics_mae:
                return 0.0
        if blend_regime == BLEND_REGIME_CLOUD_EDGE:
            if blend_better_than_physics:
                return max(CLOUD_EDGE_MIN_AI_FLOOR, min(advantage_floor, 0.4))
            if ai_mae > physics_mae:
                return 0.0
        if ai_mae > physics_mae:
            return 0.0
        inverse_error_sum = 1.0 / physics_mae + 1.0 / ai_mae
        mae_based_ai_weight = 1.0 / ai_mae / inverse_error_sum
        return min(advantage_floor, mae_based_ai_weight)

    @staticmethod
    def merge_regime_learning(
        learned: Dict[str, Any], regime_learning: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        sample_count = (
            int(regime_learning.get("sample_count", 0)) if regime_learning else 0
        )
        learning_strength = _learning_strength(
            sample_count,
            soft_min=REGIME_METHOD_SOFT_MIN_SAMPLES,
            full_samples=REGIME_METHOD_ACTIVE_MIN_SAMPLES,
        )
        if not regime_learning or learning_strength <= 0:
            return learned
        physics_mae = regime_learning.get("physics_mae")
        ai_mae = regime_learning.get("ai_mae")
        blend_mae = regime_learning.get("blend_mae")
        if physics_mae is None or ai_mae is None:
            return learned
        merged_learning = dict(learned or {})
        merged_learning["physics_mae"] = physics_mae
        merged_learning["ai_mae"] = ai_mae
        merged_learning["blend_mae"] = blend_mae
        merged_learning["sample_count"] = sample_count
        merged_learning["regime_learning_strength"] = round(learning_strength, 4)
        try:
            parsed_physics_mae = float(physics_mae)
            parsed_ai_mae = float(ai_mae)
            if parsed_physics_mae > 0 and parsed_ai_mae > 0:
                current_advantage_factor = float(
                    merged_learning.get("advantage_factor", 1.0) or 1.0
                )
                regime_advantage_factor = max(
                    0.5, min(2.0, parsed_physics_mae / parsed_ai_mae)
                )
                merged_learning["advantage_factor"] = max(
                    0.5,
                    min(
                        2.0,
                        current_advantage_factor * (1.0 - learning_strength)
                        + regime_advantage_factor * learning_strength,
                    ),
                )
        except (TypeError, ValueError):
            pass
        return merged_learning if True else None

    @staticmethod
    def adjust_tfs_weight(
        weight: float,
        *,
        blend_regime: str,
        weather_regime: Optional[str] = None,
        regime_learning: Optional[Dict[str, Any]] = None,
        similar_weather: Optional[Dict[str, Any]] = None,
        physics_pred: float,
        ai_pred: float,
        tfs_pred: float,
        rain: float,
        humidity: float,
        sun_elevation: float,
    ) -> Tuple[float, Optional[str]]:
        if weight <= 0 or tfs_pred <= 0 or physics_pred <= 0:
            return (
                (((weight, None) if True else None) if True else None) if True else None
            )
        adjustment_reason = None
        tfs_weight_cap = StaticForecastBlendPolicy._bad_weather_tfs_basis_cap(
            blend_regime=blend_regime, weather_regime=weather_regime
        )
        initial_weight_cap = tfs_weight_cap
        cap_reason = "bad_weather_tfs_basis_cap"
        similar_weather_cap = None
        if similar_weather and similar_weather.get("underforecast_signal"):
            try:
                confidence = float(similar_weather.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence > 0 and tfs_pred > max(physics_pred, ai_pred * 0.75, 0.0):
                similar_weather_cap = min(
                    SIMILAR_WEATHER_MAX_TFS_CAP, max(weight, 0.18 + confidence * 0.35)
                )
                if tfs_weight_cap is not None:
                    tfs_weight_cap = max(tfs_weight_cap, similar_weather_cap)
                    cap_reason = "similar_weather_tfs_cap_relaxation"
        consensus_tfs_cap = StaticForecastBlendPolicy._bad_weather_consensus_tfs_cap(
            blend_regime=blend_regime,
            similar_weather=similar_weather,
            physics_pred=physics_pred,
            ai_pred=ai_pred,
            tfs_pred=tfs_pred,
            sun_elevation=sun_elevation,
        )
        if consensus_tfs_cap is not None:
            if tfs_weight_cap is not None and consensus_tfs_cap > tfs_weight_cap:
                if initial_weight_cap is not None and weight > initial_weight_cap:
                    adjustment_reason = "bad_weather_consensus_tfs_escape"
                tfs_weight_cap = consensus_tfs_cap
                cap_reason = "bad_weather_consensus_tfs_escape"
            elif tfs_weight_cap is None and weight < consensus_tfs_cap:
                weight = consensus_tfs_cap
                adjustment_reason = "bad_weather_consensus_tfs_escape"
        if tfs_weight_cap is not None and weight > tfs_weight_cap:
            weight = tfs_weight_cap
            adjustment_reason = cap_reason
        elif consensus_tfs_cap is not None and weight < consensus_tfs_cap:
            weight = consensus_tfs_cap
            adjustment_reason = "bad_weather_consensus_tfs_escape"
        elif similar_weather_cap is not None and weight < similar_weather_cap:
            weight = similar_weather_cap
            adjustment_reason = "similar_weather_tfs_cap_relaxation"
        if ai_pred <= 0:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        tfs_sample_count = (
            int(regime_learning.get("sample_count", 0)) if regime_learning else 0
        )
        tfs_learning_strength = _learning_strength(
            tfs_sample_count,
            soft_min=REGIME_TFS_SOFT_MIN_SAMPLES,
            full_samples=REGIME_TFS_ACTIVE_MIN_SAMPLES,
        )
        if regime_learning and tfs_learning_strength > 0:
            blend_mae = regime_learning.get("blend_mae")
            tfs_mae = regime_learning.get("tfs_mae")
            tfs_bias = regime_learning.get("tfs_bias")
            recommended_tfs_weight = regime_learning.get("recommended_tfs_weight")
            try:
                blend_mae = float(blend_mae) if blend_mae is not None else None
                tfs_mae = float(tfs_mae) if tfs_mae is not None else None
                tfs_bias = float(tfs_bias) if tfs_bias is not None else 0.0
                recommended_tfs_weight = (
                    float(recommended_tfs_weight)
                    if recommended_tfs_weight is not None
                    else None
                )
            except (TypeError, ValueError):
                blend_mae = tfs_mae = recommended_tfs_weight = None
                tfs_bias = 0.0
            if (
                weather_regime
                in (
                    WEATHER_REGIME_RAIN_OVERCAST,
                    WEATHER_REGIME_LOW_GHI_OVERCAST,
                    WEATHER_REGIME_DIFFUSE_OVERCAST,
                    WEATHER_REGIME_LOW_SUN_UNCERTAIN,
                )
                and blend_mae is not None
                and (tfs_mae is not None)
            ):
                if (
                    tfs_mae > blend_mae * REGIME_TFS_RISK_RATIO
                    or tfs_bias > REGIME_TFS_MAX_POSITIVE_BIAS_KWH
                ):
                    capped_tfs_weight = min(weight, REGIME_TFS_BAD_WEATHER_MAX_WEIGHT)
                    adjusted_tfs_weight = (
                        weight - (weight - capped_tfs_weight) * tfs_learning_strength
                    )
                    return (
                        (
                            (
                                (
                                    adjusted_tfs_weight,
                                    (
                                        "regime_tfs_risk_cap"
                                        if adjusted_tfs_weight < weight
                                        or adjustment_reason
                                        else None
                                    ),
                                )
                                if True
                                else None
                            )
                            if True
                            else None
                        )
                        if True
                        else None
                    )
            if (
                recommended_tfs_weight is not None
                and blend_mae is not None
                and (tfs_mae is not None)
                and (tfs_mae < blend_mae * REGIME_TFS_ADVANTAGE_RATIO)
                and (tfs_bias <= REGIME_TFS_MAX_POSITIVE_BIAS_KWH)
            ):
                boosted_target_weight = max(
                    weight, min(recommended_tfs_weight, TFS_OVERCAST_MAX_WEIGHT)
                )
                boosted_tfs_weight = (
                    weight + (boosted_target_weight - weight) * tfs_learning_strength
                )
                if boosted_tfs_weight > weight:
                    weight = boosted_tfs_weight
                    adjustment_reason = "regime_tfs_advantage"
        if blend_regime not in (BLEND_REGIME_MIXED, BLEND_REGIME_CLOUD_EDGE):
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        if rain > MIXED_CLOUD_EDGE_TFS_MAX_RAIN:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        if humidity > MIXED_CLOUD_EDGE_TFS_MAX_HUMIDITY:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        if sun_elevation < MIXED_CLOUD_EDGE_TFS_MIN_SUN_ELEVATION:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        if tfs_pred < ai_pred * MIXED_CLOUD_EDGE_TFS_UPSIDE_RATIO:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        if tfs_pred < physics_pred * MIXED_CLOUD_EDGE_TFS_PHYSICS_RATIO:
            return (
                (((weight, adjustment_reason) if True else None) if True else None)
                if True
                else None
            )
        tfs_ai_upside_ratio = tfs_pred / max(
            ai_pred * MIXED_CLOUD_EDGE_TFS_UPSIDE_RATIO, 0.001
        )
        tfs_physics_upside_ratio = tfs_pred / max(
            physics_pred * MIXED_CLOUD_EDGE_TFS_PHYSICS_RATIO, 0.001
        )
        tfs_upside_strength = min(tfs_ai_upside_ratio, tfs_physics_upside_ratio)
        target_tfs_weight = MIXED_CLOUD_EDGE_TFS_BASE_TARGET_WEIGHT
        if tfs_upside_strength >= MIXED_CLOUD_EDGE_TFS_STRONG_STRENGTH:
            target_tfs_weight = MIXED_CLOUD_EDGE_TFS_STRONG_TARGET_WEIGHT
        elif tfs_upside_strength >= MIXED_CLOUD_EDGE_TFS_MEDIUM_STRENGTH:
            target_tfs_weight = MIXED_CLOUD_EDGE_TFS_MEDIUM_TARGET_WEIGHT
        boosted_tfs_weight = max(weight, target_tfs_weight)
        if boosted_tfs_weight > weight:
            return (
                (
                    (
                        (boosted_tfs_weight, "mixed_cloud_edge_tfs_upside")
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        return (
            (
                ((boosted_tfs_weight, adjustment_reason) if True else None)
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def adjust_prediction_for_regime_bias(
        prediction: float,
        regime_learning: Optional[Dict[str, Any]],
        *,
        blend_regime: str,
        capacity: float,
    ) -> Tuple[float, Optional[str]]:
        if prediction <= 0 or not regime_learning:
            return (prediction, None)
        if blend_regime != BLEND_REGIME_BAD_WEATHER:
            return (prediction, None)
        try:
            blend_bias = float(regime_learning.get("blend_bias") or 0.0)
            sample_count = int(regime_learning.get("sample_count") or 0)
        except (TypeError, ValueError):
            return (prediction, None)
        learning_strength = _learning_strength(
            sample_count,
            soft_min=REGIME_BIAS_SOFT_MIN_SAMPLES,
            full_samples=REGIME_BIAS_ACTIVE_MIN_SAMPLES,
        )
        if learning_strength <= 0:
            return (prediction, None)
        if not math.isfinite(blend_bias) or abs(blend_bias) < REGIME_BIAS_MIN_KWH:
            return (prediction, None)
        maximum_adjustment_share = 0.12
        if blend_regime == BLEND_REGIME_BAD_WEATHER:
            maximum_adjustment_share = 0.12 + 0.23 * learning_strength
        sample_confidence = min(0.7, max(0.2, sample_count / 50.0))
        bias_adjustment_kwh = blend_bias * sample_confidence * learning_strength
        maximum_adjustment_kwh = max(0.03, prediction * maximum_adjustment_share)
        bias_adjustment_kwh = max(
            -maximum_adjustment_kwh, min(maximum_adjustment_kwh, bias_adjustment_kwh)
        )
        adjusted_prediction_kwh = max(
            0.0, min(capacity, prediction - bias_adjustment_kwh)
        )
        if abs(adjusted_prediction_kwh - prediction) < 0.001:
            return (prediction, None)
        return (adjusted_prediction_kwh, "regime_bias_correction")

    @staticmethod
    def adjust_prediction_for_similar_weather(
        prediction: float,
        similar_weather: Optional[Dict[str, Any]],
        *,
        physics_pred: float,
        ai_pred: float,
        tfs_pred: float,
        capacity: float,
    ) -> Tuple[float, Optional[str]]:
        if (
            prediction <= 0
            or not similar_weather
            or (not similar_weather.get("underforecast_signal"))
        ):
            return (prediction, None)
        try:
            confidence = float(similar_weather.get("confidence") or 0.0)
            prediction_factor = float(similar_weather.get("pred_factor") or 1.0)
            physics_factor = float(similar_weather.get("physics_factor") or 1.0)
        except (TypeError, ValueError):
            return (prediction, None)
        if confidence <= 0:
            return (prediction, None)
        prediction_candidates = [prediction]
        if prediction_factor > 1.0:
            prediction_candidates.append(prediction * prediction_factor)
        if (
            physics_pred > 0
            and physics_factor > 1.0
            and similar_weather.get("physics_signal")
        ):
            prediction_candidates.append(physics_pred * physics_factor)
        model_anchor_kwh = max(ai_pred or 0.0, tfs_pred or 0.0)
        if model_anchor_kwh > prediction:
            anchor_confidence = min(SIMILAR_WEATHER_MAX_ANCHOR_CONFIDENCE, confidence)
            prediction_candidates.append(
                prediction * (1 - anchor_confidence)
                + model_anchor_kwh * anchor_confidence
            )
        candidate_prediction_kwh = max(prediction_candidates)
        if candidate_prediction_kwh <= prediction:
            return (prediction, None)
        maximum_factor = similar_weather.get(
            "max_factor", SIMILAR_WEATHER_MAX_FACTOR_MEDIUM
        )
        try:
            maximum_factor = float(maximum_factor)
        except (TypeError, ValueError):
            maximum_factor = SIMILAR_WEATHER_MAX_FACTOR_MEDIUM
        candidate_prediction_kwh = min(
            candidate_prediction_kwh, prediction * maximum_factor
        )
        candidate_prediction_kwh = min(candidate_prediction_kwh, max(0.0, capacity))
        if candidate_prediction_kwh <= prediction + 0.001:
            return (prediction, None)
        return (candidate_prediction_kwh, "similar_weather_underforecast_relaxation")


def _should_apply_bad_weather_ai_guard(blend_regime: str) -> bool:
    """Only true bad-weather regimes may aggressively suppress AI."""
    return StaticForecastBlendPolicy.should_apply_bad_weather_ai_guard(blend_regime)


def _compute_dynamic_clear_sky_floor_ratio(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    sun_elevation: float,
    humidity: float,
    rain: float,
) -> float:
    """Return a portable clear-sky floor ratio for overcast forecast guards."""
    floor_ratio = OVERCAST_CLEAR_SKY_FLOOR_MAX_RATIO
    ghi_ratio = None
    if clear_sky > 50 and ghi >= 0:
        ghi_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_share = None
    if ghi > 5 and dhi >= 0:
        diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
    direct_share = None
    if ghi > 5 and dni >= 0:
        direct_share = min(max(dni / ghi, 0.0), 1.5)
    if clouds >= 85:
        floor_ratio = min(floor_ratio, 0.12)
    if clouds >= 92:
        floor_ratio = min(floor_ratio, 0.1)
    if ghi_ratio is not None:
        if ghi_ratio <= 0.18:
            floor_ratio = min(floor_ratio, 0.06)
        elif ghi_ratio <= 0.24:
            floor_ratio = min(floor_ratio, 0.08)
        elif ghi_ratio <= 0.32:
            floor_ratio = min(floor_ratio, 0.1)
        elif ghi_ratio <= 0.4:
            floor_ratio = min(floor_ratio, 0.12)
    if diffuse_share is not None:
        if diffuse_share >= 0.82:
            floor_ratio = min(floor_ratio, 0.07)
        elif diffuse_share >= 0.74:
            floor_ratio = min(floor_ratio, 0.09)
    if direct_share is not None:
        if direct_share <= 0.18:
            floor_ratio = min(floor_ratio, 0.08)
        elif direct_share <= 0.28:
            floor_ratio = min(floor_ratio, 0.1)
    if rain >= 0.2:
        floor_ratio = min(floor_ratio, 0.09)
    if humidity >= 85:
        floor_ratio = min(floor_ratio, 0.09)
    if sun_elevation < 15:
        floor_ratio = max(floor_ratio, 0.07)
    return max(
        OVERCAST_CLEAR_SKY_FLOOR_MIN_RATIO,
        min(OVERCAST_CLEAR_SKY_FLOOR_MAX_RATIO, floor_ratio),
    )


def _get_bad_weather_ai_regime(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
    season: str,
) -> Optional[Dict[str, Any]]:
    """Classify low-confidence bad-weather regimes for AI predictions."""
    if (
        clouds < BAD_WEATHER_AI_CLOUDS_THRESHOLD
        or sun_elevation < BAD_WEATHER_AI_MIN_SUN_ELEVATION
        or hour < BAD_WEATHER_AI_START_HOUR
        or (hour > BAD_WEATHER_AI_END_HOUR)
    ):
        return None
    ghi_ratio = None
    if clear_sky > 50 and ghi >= 0:
        ghi_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_share = None
    if ghi > 5 and dhi >= 0:
        diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
    very_dense_clouds = clouds >= 90.0
    dense_clouds = clouds >= 80.0
    raining = rain >= 0.2
    very_humid = humidity >= 88.0
    severity_level = 0
    if very_dense_clouds:
        severity_level = max(severity_level, 3)
    elif dense_clouds:
        severity_level = max(severity_level, 2)
    if ghi_ratio is not None:
        if ghi_ratio <= BAD_WEATHER_AI_SEVERE_GHI_RATIO:
            severity_level = max(severity_level, 3)
        elif ghi_ratio <= BAD_WEATHER_AI_STRONG_GHI_RATIO:
            severity_level = max(severity_level, 2)
        elif ghi_ratio <= BAD_WEATHER_AI_MODERATE_GHI_RATIO:
            severity_level = max(severity_level, 1)
    if diffuse_share is not None:
        if diffuse_share >= BAD_WEATHER_AI_SEVERE_DIFFUSE_SHARE:
            severity_level = max(severity_level, 3)
        elif diffuse_share >= BAD_WEATHER_AI_STRONG_DIFFUSE_SHARE:
            severity_level = max(severity_level, 2)
        elif diffuse_share >= BAD_WEATHER_AI_MODERATE_DIFFUSE_SHARE:
            severity_level = max(severity_level, 1)
    if raining and very_humid:
        severity_level = max(severity_level, 2)
    elif raining:
        severity_level = max(severity_level, 1)
    if clouds >= BAD_WEATHER_AI_CLOUDS_THRESHOLD and severity_level == 0:
        if ghi_ratio is None or ghi_ratio <= BAD_WEATHER_AI_MODERATE_GHI_RATIO:
            severity_level = 1
        elif (
            diffuse_share is not None
            and diffuse_share >= BAD_WEATHER_AI_MODERATE_DIFFUSE_SHARE
        ):
            severity_level = 1
        elif rain > 0:
            severity_level = 1
        else:
            severity_level = 0
    if severity_level <= 0:
        return
    if season == "spring":
        if severity_level == 1 and hour < 10:
            severity_level = 2
        elif (
            severity_level == 1
            and hour >= 15
            and (ghi_ratio is not None)
            and (ghi_ratio <= 0.55)
        ):
            severity_level = 2
    if severity_level >= 3:
        regime = "severe"
        maximum_multiplier = BAD_WEATHER_AI_SEVERE_MAX_MULTIPLIER
        confidence_multiplier = BAD_WEATHER_AI_SEVERE_CONFIDENCE_MULTIPLIER
    elif severity_level == 2:
        regime = "strong"
        maximum_multiplier = BAD_WEATHER_AI_STRONG_MAX_MULTIPLIER
        confidence_multiplier = BAD_WEATHER_AI_STRONG_CONFIDENCE_MULTIPLIER
    else:
        regime = "moderate"
        maximum_multiplier = BAD_WEATHER_AI_MODERATE_MAX_MULTIPLIER
        confidence_multiplier = BAD_WEATHER_AI_MODERATE_CONFIDENCE_MULTIPLIER
    return {
        "regime": regime,
        "ghi_ratio": ghi_ratio,
        "diffuse_share": diffuse_share,
        "max_multiplier": maximum_multiplier,
        "confidence_multiplier": confidence_multiplier,
    }


def _apply_bad_weather_ai_regime(
    *,
    ai_pred: float,
    physics_pred: float,
    tfs_pred: float,
    regime: Optional[Dict[str, Any]],
) -> tuple[float, Optional[str], Optional[float], Optional[float], Optional[float]]:
    """Apply a regime-specific cap to raw AI output before blending."""
    if not regime or ai_pred <= 0 or physics_pred <= 0:
        return (ai_pred, None, None, None, None)
    reference_prediction_kwh = max(physics_pred, tfs_pred)
    if reference_prediction_kwh <= 0:
        return (
            ai_pred,
            None,
            regime.get("ghi_ratio"),
            regime.get("diffuse_share"),
            None,
        )
    ai_cap_kwh = reference_prediction_kwh * float(regime["max_multiplier"])
    if ai_pred <= ai_cap_kwh:
        return (
            ai_pred,
            None,
            regime.get("ghi_ratio"),
            regime.get("diffuse_share"),
            ai_cap_kwh,
        )
    return (
        ai_cap_kwh,
        str(regime["regime"]),
        regime.get("ghi_ratio"),
        regime.get("diffuse_share"),
        ai_cap_kwh,
    )


class RuleBasedForecastStrategy(ForecastStrategy):
    """AI-based forecast strategy with physics fallback. @zara"""

    def __init__(
        self,
        weather_calculator: WeatherCalculator,
        solar_capacity: float,
        db_manager: DatabaseManager,
        orchestrator: Optional[Any] = None,
        panel_groups: Optional[List[Dict[str, Any]]] = None,
        ai_predictor: Optional[Any] = None,
        inverter_max_power: float = 0.0,
    ):
        "Initialize strategy. @zara\n\nArgs:\n    weather_calculator: Weather calculation utilities\n    solar_capacity: System capacity in kWp\n    db_manager: Database manager for DB operations\n    orchestrator: Parent orchestrator reference\n    panel_groups: Panel group configurations\n    ai_predictor: AI predictor instance\n    inverter_max_power: Optional AC inverter limit in kW\n"
        super().__init__("rule_based")
        self.weather_calculator = weather_calculator
        self.solar_capacity = solar_capacity
        self._db = db_manager
        self.orchestrator = orchestrator
        self.ai_predictor = ai_predictor
        self.inverter_max_power = max(0.0, float(inverter_max_power or 0.0))
        self.physics_engine = None
        self._physics_init_attempted = False
        self.panel_group_calculator = None
        self._panel_groups_config = panel_groups or []
        self._panel_groups_initialized = False
        self._panel_groups_config_hash = (
            hash(
                str(
                    sorted(
                        (
                            str(physics_group)
                            for physics_group in self._panel_groups_config
                        )
                    )
                )
            )
            if self._panel_groups_config
            else 0
        )
        self._physics_calibrator = None
        self._weather_actual_tracker = None
        self._use_ai = False
        self._similar_weather_history_cache = {}
        self._daily_bias_history_cache = {}
        self._forecast_debug_stats = {}
        self._run_is_morning_cycle = False
        self._run_rain_gated = {}
        self._last_component_mode = None
        self._forecast_reference_time = None
        self._forecast_policy_read_cache = None
        if panel_groups:
            _LOGGER.debug(
                f"Physics+ML Strategy: {len(panel_groups)} panel groups configured"
            )
        if ai_predictor:
            _LOGGER.debug("Physics+ML Strategy: Local AI available")
        return None if True else None

    def _cycle_log(self):
        """INFO in a morning cycle, DEBUG on a coordinator refresh."""
        return _LOGGER.info if self._run_is_morning_cycle else _LOGGER.debug

    def _reset_forecast_debug_stats(self) -> None:
        self._forecast_debug_stats = {
            "bad_weather_confidence": 0,
            "bad_weather_guard_skipped": 0,
            "bad_weather_cap": 0,
            "dense_overcast_ai_cap": 0,
            "tfs_low_sun_envelope": 0,
            "tfs_policy_adjustment": 0,
            "tfs_adaptive_blend": 0,
            "similar_weather_adjustment": 0,
            "static_regime_bias": 0,
        }

    def _policy_now(self) -> datetime:
        return self._forecast_reference_time or dt_util_safe.now()

    def _record_forecast_debug(self, key: str) -> None:
        if not self._forecast_debug_stats:
            return
        self._forecast_debug_stats[key] = self._forecast_debug_stats.get(key, 0) + 1

    def _log_forecast_debug_summary(self, hours_processed: int) -> None:
        debug_stats = self._forecast_debug_stats
        if not debug_stats:
            return
        summary = {
            counter_key: count for counter_key, count in debug_stats.items() if count
        }
        if self._run_rain_gated:
            rain_gated_regimes = sorted(
                {regime for regime, rain_gated_hour in self._run_rain_gated.values()}
            )
            summary["rain_gated_days"] = (
                f"{len(self._run_rain_gated)} ({', '.join(rain_gated_regimes)})"
            )
        if not summary:
            return
        self._cycle_log()(
            "Forecast strategy summary: hours=%d, %s",
            hours_processed,
            ", ".join(
                (
                    f"{counter_key}={count}"
                    for counter_key, count in sorted(summary.items())
                )
            ),
        )

    def set_physics_calibrator(self, calibrator: PhysicsCalibrator) -> None:
        """Set the physics calibrator for self-learning corrections. @zara"""
        self._physics_calibrator = calibrator
        if self.panel_group_calculator:
            self.panel_group_calculator.set_calibrator(calibrator)
            _LOGGER.info("PhysicsCalibrator connected to existing PanelGroupCalculator")
        else:
            _LOGGER.debug(
                "PhysicsCalibrator stored, will connect when PanelGroupCalculator is created"
            )
        return None if True else None

    def set_weather_actual_tracker(self, tracker: Any) -> None:
        """Set the weather actual tracker for snow status detection. @zara"""
        self._weather_actual_tracker = tracker
        _LOGGER.debug("WeatherActualTracker connected for SNOWY bucket detection")

    async def _get_snow_covered_status(self) -> Tuple[bool, Dict[str, bool]]:
        """Get current snow coverage status — system-wide + per-group. @zara V17.0.0"""
        if not self._weather_actual_tracker:
            return (False, {})
        try:
            snow_status = await self._weather_actual_tracker.get_snow_tracking_status()
            snow_covered = snow_status.get("snow_covered_panels", False)
            snow_per_group = (
                await self._weather_actual_tracker.get_snow_tracking_per_group()
            )
            if snow_covered:
                _LOGGER.debug(
                    "Snow-covered panels detected - using SNOWY bucket for calibration"
                )
            if any(snow_per_group.values()):
                snow_covered_groups = [
                    group_name
                    for group_name, group_snow_covered in snow_per_group.items()
                    if group_snow_covered
                ]
                _LOGGER.log(5, "Per-group snow: covered=%s", snow_covered_groups)
            return (snow_covered, snow_per_group)
        except Exception as error:
            _LOGGER.debug(f"Could not get snow status: {error}")
            return (False, {})

    def is_available(self) -> bool:
        """Always available as fallback. @zara"""
        return True

    def get_priority(self) -> int:
        """Priority lower than pure AI strategy. @zara"""
        return 50

    async def calculate_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        **kwargs,
    ) -> ForecastResult:
        """Calculate forecast using physics engine. @zara"""
        try:
            self._run_is_morning_cycle = bool(kwargs.get("forecast_cycle"))
            self._run_rain_gated = {}
            self._reset_forecast_debug_stats()
            capacity_kwp = float(sensor_data.get("solar_capacity", self.solar_capacity))
            if capacity_kwp <= 0:
                capacity_kwp = self.solar_capacity
            await self._ensure_components_initialized()
            if not hourly_weather_forecast:
                raise ForecastGenerationError("no hourly weather forecast")
            frozen_snow_status = kwargs.get("frozen_snow_status")
            if frozen_snow_status is None:
                snow_covered_panels, snow_per_group = (
                    await self._get_snow_covered_status()
                )
            else:
                snow_covered_panels, snow_per_group = frozen_snow_status
            tfs_hourly = kwargs.get("tfs_hourly")
            persist = kwargs.get("persist", True)
            reference_time = kwargs.get("forecast_reference_time") or dt_util_safe.now()
            self._forecast_reference_time = reference_time
            today = reference_time.date()
            tomorrow = today + timedelta(days=1)
            day_after_tomorrow = today + timedelta(days=2)
            today_kwh = 0.0
            tomorrow_kwh = 0.0
            day_after_tomorrow_kwh = 0.0
            hourly_values = []
            ensemble_group_weights = kwargs.get("frozen_ensemble_group_weights")
            if ensemble_group_weights is None:
                ensemble_group_weights = await self._load_ensemble_group_weights()
            for hour_weather in hourly_weather_forecast:
                hour_datetime = self._parse_datetime(hour_weather)
                if not hour_datetime:
                    continue
                forecast_day = hour_datetime.date()
                hour = hour_datetime.hour
                hour_key = hour_datetime.strftime("%Y-%m-%dT%H:00:00")
                tfs_hour_data = tfs_hourly.get(hour_key) if tfs_hourly else None
                hour_result = await self._calculate_hour_production(
                    hour_weather,
                    hour_datetime,
                    capacity_kwp,
                    snow_covered_panels,
                    ensemble_group_weights=ensemble_group_weights,
                    snow_per_group=snow_per_group,
                    tfs_hour_data=tfs_hour_data,
                )
                production_kwh = hour_result["production"]
                group_results = hour_result["groups"]
                hourly_entry = {
                    "hour": hour,
                    "datetime": hour_datetime.isoformat(),
                    "date": forecast_day.isoformat(),
                    "production_kwh": round(production_kwh, 3),
                    "prediction_kwh_uncapped": round(production_kwh, 4),
                    "method": hour_result["method"],
                    "physics_kwh": hour_result["physics_kwh"],
                    "ai_kwh": hour_result["ai_kwh"],
                    "ai_confidence": hour_result["ai_confidence"],
                    "ml_contribution": hour_result["ml_contribution"],
                    "confidence": 75.0,
                    "lstm_kwh": hour_result.get("lstm_kwh"),
                    "ridge_kwh": hour_result.get("ridge_kwh"),
                    "tfs_kwh": hour_result.get("tfs_kwh"),
                    "tfs_weight": hour_result.get("tfs_weight"),
                    "weather_regime": hour_result.get("weather_regime"),
                    "blend_regime": hour_result.get("blend_regime"),
                    "sun_elevation_deg": hour_result.get("sun_elevation_deg"),
                    "sun_gated": bool(hour_result.get("sun_gated")),
                    "ghi_before_cap": hour_result.get("ghi_before_cap"),
                    "ghi_after_cap": hour_result.get("ghi_after_cap"),
                    "ghi_cap_applied": hour_result.get("ghi_cap_applied"),
                }
                if group_results:
                    hourly_entry["groups"] = group_results
                    hourly_entry["panel_group_predictions"] = (
                        sync_panel_group_prediction_map(group_results)
                    )
                hourly_values.append(hourly_entry)
                continue
            if not hourly_values:
                raise ForecastGenerationError(
                    "no hourly slots produced from weather forecast"
                )
            target_dates = [
                today.isoformat(),
                tomorrow.isoformat(),
                day_after_tomorrow.isoformat(),
            ]
            daily_totals, uncapped_daily_totals, daily_cap_details = (
                self._normalize_hourly_forecast_values(
                    hourly_values=hourly_values,
                    target_dates=target_dates,
                    capacity=capacity_kwp,
                    correction_factor=correction_factor,
                )
            )
            today_kwh = daily_totals.get(today.isoformat(), 0.0)
            tomorrow_kwh = daily_totals.get(tomorrow.isoformat(), 0.0)
            day_after_tomorrow_kwh = daily_totals.get(
                day_after_tomorrow.isoformat(), 0.0
            )
            daily_bias_applied = await self._apply_final_morning_daily_bias(
                hourly_values=hourly_values,
                target_dates=target_dates,
                totals=daily_totals,
                capacity=capacity_kwp,
            )
            self._apply_inverter_hour_cap(hourly_values, daily_totals)
            if daily_bias_applied:
                today_kwh = daily_totals.get(today.isoformat(), 0.0)
                tomorrow_kwh = daily_totals.get(tomorrow.isoformat(), 0.0)
                day_after_tomorrow_kwh = daily_totals.get(
                    day_after_tomorrow.isoformat(), 0.0
                )
            best_hour = None
            best_hour_kwh = 0.0
            for entry in hourly_values:
                if entry.get("date") != today.isoformat():
                    continue
                production_kwh = float(entry.get("production_kwh") or 0.0)
                if production_kwh > best_hour_kwh:
                    best_hour_kwh = production_kwh
                    best_hour = entry.get("hour")
            if persist:
                await self._store_hourly_predictions(hourly_values)
            else:
                _LOGGER.debug(
                    "Skipping hourly_predictions persistence for compute-only forecast"
                )
            self._log_forecast_debug_summary(len(hourly_values))
            return ForecastResult(
                forecast_today=round(today_kwh, 2),
                forecast_tomorrow=round(tomorrow_kwh, 2),
                forecast_day_after_tomorrow=round(day_after_tomorrow_kwh, 2),
                confidence_today=70.0,
                confidence_tomorrow=60.0,
                confidence_day_after=50.0,
                method="physics" if not self._use_ai else "AI + Physics",
                calibrated=bool(
                    self.panel_group_calculator
                    and self.panel_group_calculator.has_groups
                ),
                best_hour_today=best_hour,
                best_hour_production_kwh=round(best_hour_kwh, 3),
                hourly_values=hourly_values,
                model_accuracy=None,
                forecast_today_raw=uncapped_daily_totals.get(
                    today.isoformat(), today_kwh
                ),
                forecast_tomorrow_raw=uncapped_daily_totals.get(
                    tomorrow.isoformat(), tomorrow_kwh
                ),
                forecast_day_after_raw=uncapped_daily_totals.get(
                    day_after_tomorrow.isoformat(), day_after_tomorrow_kwh
                ),
                safeguard_applied_today=daily_cap_details.get(
                    today.isoformat(), {}
                ).get("applied", False),
                safeguard_applied_tomorrow=daily_cap_details.get(
                    tomorrow.isoformat(), {}
                ).get("applied", False),
                safeguard_applied_day_after=daily_cap_details.get(
                    day_after_tomorrow.isoformat(), {}
                ).get("applied", False),
            )
        except ForecastGenerationError:
            raise
        except Exception as error:
            _LOGGER.error(f"Forecast calculation failed: {error}")
            raise ForecastGenerationError(
                f"Forecast calculation failed: {error}"
            ) from error

    def _normalize_hourly_forecast_values(
        self,
        hourly_values: List[Dict[str, Any]],
        target_dates: List[str],
        capacity: float,
        correction_factor: float,
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, Dict[str, Any]]]:
        """Apply correction, inverter limits, and daily caps to one hourly truth."""
        uncapped_daily_totals = {target_date: 0.0 for target_date in target_dates}
        for entry in hourly_values:
            clamped_prediction_kwh = max(0.0, float(entry.get("production_kwh") or 0.0))
            entry["prediction_kwh_uncapped"] = round(clamped_prediction_kwh, 4)
            if entry.get("date") in uncapped_daily_totals:
                uncapped_daily_totals[entry["date"]] += clamped_prediction_kwh
            corrected_prediction_kwh = clamped_prediction_kwh * correction_factor
            if self.inverter_max_power > 0:
                corrected_prediction_kwh = min(
                    corrected_prediction_kwh, self.inverter_max_power
                )
            scale = (
                corrected_prediction_kwh / clamped_prediction_kwh
                if clamped_prediction_kwh > 0
                else 0.0
            )
            self._scale_hourly_entry(entry, scale, corrected_prediction_kwh)
        daily_totals = {target_date: 0.0 for target_date in target_dates}
        daily_cap_details = {}
        for target_date in target_dates:
            day_entries = [
                entry for entry in hourly_values if entry.get("date") == target_date
            ]
            daily_total_kwh = sum(
                (float(entry.get("production_kwh") or 0.0) for entry in day_entries)
            )
            daily_cap = self._get_daily_forecast_cap(day_entries, capacity)
            daily_limit_kwh = float(daily_cap["limit_kwh"])
            cap_applied = False
            if daily_limit_kwh > 0 and daily_total_kwh > daily_limit_kwh:
                daily_scale = daily_limit_kwh / daily_total_kwh
                for entry in day_entries:
                    original_prediction_kwh = float(entry.get("production_kwh") or 0.0)
                    self._scale_hourly_entry(
                        entry, daily_scale, original_prediction_kwh * daily_scale
                    )
                    entry["daily_cap_applied"] = True
                    entry["daily_cap_kwh"] = round(daily_limit_kwh, 4)
                    entry["daily_cap_source"] = daily_cap["source"]
                daily_total_kwh = daily_limit_kwh
                cap_applied = True
            daily_totals[target_date] = round(daily_total_kwh, 4)
            uncapped_daily_totals[target_date] = round(
                uncapped_daily_totals[target_date], 4
            )
            daily_cap_details[target_date] = {
                "applied": cap_applied,
                "limit_kwh": round(daily_limit_kwh, 4),
                "source": daily_cap["source"],
            }
            continue
        return (daily_totals, uncapped_daily_totals, daily_cap_details)

    def _get_daily_forecast_cap(
        self, day_entries: List[Dict[str, Any]], capacity: float
    ) -> Dict[str, Any]:
        hard_limit_kwh = max(0.0, capacity * DAILY_FORECAST_HARD_CAP_KWH_PER_KWP)
        if not day_entries or capacity <= 0:
            return (
                {"limit_kwh": hard_limit_kwh, "source": "hard_capacity_cap"}
                if True
                else None
            )
        clear_sky_potential_kwh = self._get_daily_clear_sky_potential(day_entries)
        minimum_clear_sky_potential_kwh = (
            capacity * DAILY_FORECAST_CLEAR_SKY_MIN_KWH_PER_KWP
        )
        if clear_sky_potential_kwh >= minimum_clear_sky_potential_kwh:
            clear_sky_limit_kwh = (
                clear_sky_potential_kwh * DAILY_FORECAST_CLEAR_SKY_CAP_BUFFER
            )
            return (
                {
                    "limit_kwh": (
                        min(clear_sky_limit_kwh, hard_limit_kwh)
                        if hard_limit_kwh > 0
                        else clear_sky_limit_kwh
                    ),
                    "source": "clear_sky_dynamic_cap",
                }
                if True
                else None
            )
        return (
            {"limit_kwh": hard_limit_kwh, "source": "hard_capacity_cap"}
            if True
            else None
        )

    def _get_daily_clear_sky_potential(
        self, day_entries: List[Dict[str, Any]]
    ) -> float:
        astronomy_cache_manager = get_cache_manager()
        if not astronomy_cache_manager or not astronomy_cache_manager.is_loaded():
            return 0.0
        potential_kwh = 0.0
        for entry in day_entries:
            try:
                datetime_text = entry.get("datetime")
                if not datetime_text:
                    continue
                hour_datetime = datetime.fromisoformat(str(datetime_text))
                astronomy = self._get_astronomy_for_hour(hour_datetime)
                potential_kwh += max(
                    0.0, float(astronomy.get("theoretical_max_kwh") or 0.0)
                )
            except Exception:
                continue
        return potential_kwh

    @staticmethod
    def _scale_hourly_entry(
        entry: Dict[str, Any], scale: float, production: float
    ) -> None:
        """Scale one hourly prediction and its diagnostic breakdown consistently."""
        entry["production_kwh"] = round(max(0.0, production), 3)
        for field_name in ("physics_kwh", "ai_kwh", "lstm_kwh", "ridge_kwh", "tfs_kwh"):
            component_value = entry.get(field_name)
            if component_value is not None:
                entry[field_name] = round(max(0.0, float(component_value) * scale), 4)
        groups = entry.get("groups") or []
        for group in groups:
            for field_name in (
                "power_kwh",
                "physics_kwh",
                "ai_kwh",
                "lstm_kwh",
                "ridge_kwh",
                "tfs_kwh",
            ):
                component_value = group.get(field_name)
                if component_value is not None:
                    group[field_name] = round(
                        max(0.0, float(component_value) * scale), 4
                    )
        if groups:
            normalized_groups = normalize_group_predictions_to_total(
                groups, entry["production_kwh"]
            )
            entry["groups"] = normalized_groups
            entry["panel_group_predictions"] = sync_panel_group_prediction_map(
                normalized_groups or []
            )
        return None if True else None

    def _apply_inverter_hour_cap(
        self, hourly_values: List[Dict[str, Any]], totals: Dict[str, float]
    ) -> None:
        """Re-apply the hourly inverter limit after daily bias scaling."""
        if self.inverter_max_power <= 0:
            return
        inverter_cap_applied = False
        for entry in hourly_values:
            original_prediction_kwh = max(
                0.0, float(entry.get("production_kwh") or 0.0)
            )
            capped_prediction_kwh = min(
                original_prediction_kwh, self.inverter_max_power
            )
            if abs(capped_prediction_kwh - original_prediction_kwh) < 1e-09:
                continue
            scale = (
                capped_prediction_kwh / original_prediction_kwh
                if original_prediction_kwh > 0
                else 0.0
            )
            self._scale_hourly_entry(entry, scale, capped_prediction_kwh)
            inverter_cap_applied = True
        if not inverter_cap_applied:
            return
        for target_date in list(totals):
            daily_total_kwh = sum(
                (
                    float(entry.get("production_kwh") or 0.0)
                    for entry in hourly_values
                    if entry.get("date") == target_date
                )
            )
            totals[target_date] = round(daily_total_kwh, 4)

    def _scale_daily_bias_entry(self, entry: Dict[str, Any], scale: float) -> None:
        original_prediction_kwh = max(0.0, float(entry.get("production_kwh") or 0.0))
        scaled_prediction_kwh = original_prediction_kwh * scale
        if self.inverter_max_power > 0:
            scaled_prediction_kwh = min(scaled_prediction_kwh, self.inverter_max_power)
        effective_scale = (
            scaled_prediction_kwh / original_prediction_kwh
            if original_prediction_kwh > 0
            else 0.0
        )
        self._scale_hourly_entry(entry, effective_scale, scaled_prediction_kwh)
        groups = entry.get("groups") or []
        if groups:
            normalized_groups = normalize_group_predictions_to_total(
                groups, entry["production_kwh"]
            )
            entry["groups"] = normalized_groups
            entry["panel_group_predictions"] = sync_panel_group_prediction_map(
                normalized_groups or []
            )
        return None if True else None

    async def _apply_final_morning_daily_bias(
        self,
        *,
        hourly_values: List[Dict[str, Any]],
        target_dates: List[str],
        totals: Dict[str, float],
        capacity: float,
    ) -> bool:
        bias_applied = False
        for target_date in target_dates:
            day_entries = [
                entry for entry in hourly_values if entry.get("date") == target_date
            ]
            if not day_entries:
                continue
            current_total_kwh = sum(
                (float(entry.get("production_kwh") or 0.0) for entry in day_entries)
            )
            if current_total_kwh <= 0.01:
                continue
            bias_signal = await self._get_final_morning_daily_bias_signal(
                target_date, day_entries
            )
            hourly_factors = {
                int(hour): float(scale)
                for hour, scale in (bias_signal.get("hour_factors") or {}).items()
                if abs(float(scale) - 1.0) >= 0.01
            }
            daily_factor = float(bias_signal.get("factor") or 1.0)
            daily_limit_kwh = float(
                self._get_daily_forecast_cap(day_entries, capacity)["limit_kwh"]
            )
            if hourly_factors:
                for entry in day_entries:
                    scale = hourly_factors.get(int(entry.get("hour") or 0))
                    if not scale is not None:
                        continue
                    self._scale_daily_bias_entry(entry, scale)
                    entry["daily_bias_factor"] = round(scale, 4)
                    entry["daily_bias_reason"] = bias_signal.get("reason")
            elif abs(daily_factor - 1.0) >= 0.01:
                adjusted_total_kwh = current_total_kwh * daily_factor
                if daily_limit_kwh > 0:
                    adjusted_total_kwh = min(adjusted_total_kwh, daily_limit_kwh)
                adjusted_total_kwh = max(0.0, adjusted_total_kwh)
                if abs(adjusted_total_kwh - current_total_kwh) < 0.01:
                    continue
                scale = adjusted_total_kwh / current_total_kwh
                for entry in day_entries:
                    self._scale_daily_bias_entry(entry, scale)
                    entry["daily_bias_factor"] = round(scale, 4)
                    entry["daily_bias_reason"] = bias_signal.get("reason")
            else:
                continue
            scaled_total_kwh = sum(
                (float(entry.get("production_kwh") or 0.0) for entry in day_entries)
            )
            if (
                daily_limit_kwh > 0
                and scaled_total_kwh > daily_limit_kwh
                and (scaled_total_kwh > 0.01)
            ):
                cap_scale = daily_limit_kwh / scaled_total_kwh
                for entry in day_entries:
                    self._scale_daily_bias_entry(entry, cap_scale)
                scaled_total_kwh = sum(
                    (float(entry.get("production_kwh") or 0.0) for entry in day_entries)
                )
            if abs(scaled_total_kwh - current_total_kwh) < 0.01:
                continue
            totals[target_date] = round(scaled_total_kwh, 4)
            bias_applied = True
            self._cycle_log()(
                "Daily overforecast bias applied: date=%s, reason=%s, total=%.3f->%.3f kWh, factor=%.3f, hours=%s, samples=%s, weight_sum=%s, median=%s, q25=%s, q75=%s",
                target_date,
                bias_signal.get("reason"),
                current_total_kwh,
                totals[target_date],
                daily_factor if not hourly_factors else 1.0,
                ",".join((str(hour) for hour in sorted(hourly_factors))) or "-",
                bias_signal.get("sample_count"),
                bias_signal.get("weight_sum"),
                bias_signal.get("factor_median"),
                bias_signal.get("factor_q25"),
                bias_signal.get("factor_q75"),
            )
            continue
        return bias_applied

    async def _store_hourly_predictions(
        self, hourly_values: List[Dict[str, Any]]
    ) -> None:
        "Store hourly predictions in database.\n        Locked dates are skipped entirely - only morning routine may write predictions. @zara"
        try:
            locked_dates = set()
            forecast_dates = {entry["date"] for entry in hourly_values}
            for target_date in forecast_dates:
                locked_forecast_row = await self._db.fetchone(
                    "SELECT locked FROM daily_forecasts\n                       WHERE forecast_date = ? AND locked = 1\n                       LIMIT 1",
                    (target_date,),
                )
                if locked_forecast_row and locked_forecast_row[0]:
                    locked_dates.add(target_date)
            if locked_dates:
                _LOGGER.debug(
                    "Skipping hourly predictions for locked dates: %s",
                    ", ".join(sorted(locked_dates)),
                )
            async with self._db.transaction():
                for entry in hourly_values:
                    if entry["date"] in locked_dates:
                        continue
                    prediction_id = f"{entry['date']}_{entry['hour']:02d}"
                    target_datetime = datetime.fromisoformat(entry["datetime"])
                    method = entry.get("method", "physics")
                    await self._db.execute(
                        "INSERT INTO hourly_predictions\n                           (prediction_id, prediction_created_at, prediction_created_hour,\n                            target_datetime, target_date, target_hour, prediction_kwh,\n                            prediction_kwh_uncapped, prediction_method, is_production_hour,\n                            physics_kwh, ai_kwh, ai_confidence,\n                            ml_contribution_percent, confidence,\n                            lstm_kwh, ridge_kwh,\n                            ghi_before_cap, ghi_after_cap, ghi_cap_applied)\n                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                           ON CONFLICT(prediction_id) DO UPDATE SET\n                               prediction_kwh = excluded.prediction_kwh,\n                               prediction_kwh_uncapped = excluded.prediction_kwh_uncapped,\n                               prediction_method = excluded.prediction_method,\n                               prediction_created_at = excluded.prediction_created_at,\n                               physics_kwh = excluded.physics_kwh,\n                               ai_kwh = excluded.ai_kwh,\n                               ai_confidence = excluded.ai_confidence,\n                               ml_contribution_percent = excluded.ml_contribution_percent,\n                               confidence = excluded.confidence,\n                               lstm_kwh = excluded.lstm_kwh,\n                               ridge_kwh = excluded.ridge_kwh,\n                               ghi_before_cap = excluded.ghi_before_cap,\n                               ghi_after_cap = excluded.ghi_after_cap,\n                               ghi_cap_applied = excluded.ghi_cap_applied",
                        (
                            prediction_id,
                            dt_util_safe.now(),
                            dt_util_safe.now().hour,
                            target_datetime,
                            entry["date"],
                            entry["hour"],
                            entry["production_kwh"],
                            entry.get("prediction_kwh_uncapped"),
                            method,
                            entry["production_kwh"] > 0,
                            entry.get("physics_kwh"),
                            entry.get("ai_kwh"),
                            entry.get("ai_confidence"),
                            entry.get("ml_contribution", 0),
                            entry.get("confidence", 75.0),
                            entry.get("lstm_kwh"),
                            entry.get("ridge_kwh"),
                            entry.get("ghi_before_cap"),
                            entry.get("ghi_after_cap"),
                            entry.get("ghi_cap_applied"),
                        ),
                        auto_commit=False,
                    )
                    groups = entry.get("groups")
                    if groups:
                        groups = (
                            normalize_group_predictions_to_total(
                                groups, entry.get("production_kwh", 0.0)
                            )
                            or []
                        )
                        entry["groups"] = groups
                        entry["panel_group_predictions"] = (
                            sync_panel_group_prediction_map(groups)
                        )
                        for group in groups:
                            group_key = group.get("name")
                            group_power_kwh = group.get("power_kwh", 0.0)
                            group_physics_kwh = group.get("physics_kwh")
                            group_ai_kwh = group.get("ai_kwh")
                            group_lstm_kwh = group.get("lstm_kwh")
                            group_ridge_kwh = group.get("ridge_kwh")
                            topology_meta = (
                                await self._db.get_active_panel_group_topology_meta(
                                    group_key
                                )
                            )
                            await self._db.execute(
                                "INSERT INTO prediction_panel_groups\n                                   (prediction_id, group_name, prediction_kwh, physics_kwh, ai_kwh,\n                                    lstm_kwh, ridge_kwh, raw_physics_kwh,\n                                    config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                                       prediction_kwh = excluded.prediction_kwh,\n                                       physics_kwh = excluded.physics_kwh,\n                                       ai_kwh = excluded.ai_kwh,\n                                       lstm_kwh = excluded.lstm_kwh,\n                                       ridge_kwh = excluded.ridge_kwh,\n                                       raw_physics_kwh = excluded.raw_physics_kwh,\n                                       config_epoch_id = excluded.config_epoch_id,\n                                       group_uid = excluded.group_uid,\n                                       group_lineage_uid = excluded.group_lineage_uid,\n                                       power_wp_at_prediction = excluded.power_wp_at_prediction",
                                (
                                    prediction_id,
                                    group_key,
                                    round(group_power_kwh, 4),
                                    (
                                        round(group_physics_kwh, 4)
                                        if group_physics_kwh
                                        else None
                                    ),
                                    round(group_ai_kwh, 4) if group_ai_kwh else None,
                                    (
                                        round(group_lstm_kwh, 4)
                                        if group_lstm_kwh
                                        else None
                                    ),
                                    (
                                        round(group_ridge_kwh, 4)
                                        if group_ridge_kwh
                                        else None
                                    ),
                                    (
                                        round(group.get("raw_physics_kwh"), 4)
                                        if group.get("raw_physics_kwh") is not None
                                        else None
                                    ),
                                    topology_meta.get("config_epoch_id"),
                                    topology_meta.get("group_uid"),
                                    topology_meta.get("group_lineage_uid"),
                                    topology_meta.get("power_wp_at_prediction"),
                                ),
                                auto_commit=False,
                            )
                            continue
                        continue
        except Exception as error:
            _LOGGER.error(
                "Could not store hourly predictions: %s", error, exc_info=True
            )
        try:
            return
        except Exception as error:
            _LOGGER.error(
                "Could not store hourly predictions: %s", error, exc_info=True
            )

    async def _ensure_components_initialized(self) -> None:
        """Initialize AI and physics components. @zara"""
        if self.ai_predictor and self.ai_predictor.is_ready():
            self._use_ai = True
            if self.ai_predictor.is_bootstrap_phase():
                training_samples = self.ai_predictor.training_samples or 0
                predictor_mode = f"ridge:{training_samples}"
                if predictor_mode != self._last_component_mode:
                    _LOGGER.debug(
                        "Physics+Ridge blend active (%d samples)", training_samples
                    )
                    self._last_component_mode = predictor_mode
            else:
                predictor_mode = "lstm"
                if predictor_mode != self._last_component_mode:
                    _LOGGER.debug("Physics+LSTM blend active")
                    self._last_component_mode = predictor_mode
        else:
            self._use_ai = False
            self._last_component_mode = None
        if not self._physics_init_attempted:
            self._physics_init_attempted = True
            try:
                self.physics_engine = PhysicsEngine(
                    system_capacity_kwp=self.solar_capacity
                )
                _LOGGER.debug("Physics engine initialized")
            except Exception as error:
                _LOGGER.warning(f"Physics engine init failed: {error}")
        panel_groups_hash = (
            hash(
                str(
                    sorted(
                        (str(panel_group) for panel_group in self._panel_groups_config)
                    )
                )
            )
            if self._panel_groups_config
            else 0
        )
        if panel_groups_hash != self._panel_groups_config_hash:
            self._panel_groups_initialized = False
            self._panel_groups_config_hash = panel_groups_hash
        if not self._panel_groups_initialized:
            if self._panel_groups_config:
                self._panel_groups_initialized = True
                try:
                    self.panel_group_calculator = PanelGroupCalculator(
                        panel_groups=self._panel_groups_config, db_manager=self._db
                    )
                    _LOGGER.debug(
                        f"PanelGroupCalculator initialized: {self.panel_group_calculator.group_count} groups, {self.panel_group_calculator.total_capacity_kwp:.2f} kWp"
                    )
                    if self._physics_calibrator:
                        self.panel_group_calculator.set_calibrator(
                            self._physics_calibrator
                        )
                        _LOGGER.debug(
                            "PhysicsCalibrator connected to PanelGroupCalculator for self-learning"
                        )
                except Exception as error:
                    _LOGGER.warning(f"PanelGroupCalculator init failed: {error}")
        return None if True else None

    async def _get_ghi_coherence_cap_eligible(
        self, snapshot_id: Optional[str]
    ) -> Optional[int]:
        """Read the corrector's cap-eligibility flag; NULL means legacy cap-on."""
        if not snapshot_id:
            return
        cache_key = ("ghi_coherence_cap_eligible", snapshot_id)
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            return self._forecast_policy_read_cache[cache_key]
        database_row = None
        try:
            database_row = await self._db.fetchone(
                "SELECT ghi_coherence_cap_eligible\n                   FROM weather_forecast_correction_snapshots\n                   WHERE correction_snapshot_id = ?",
                (snapshot_id,),
            )
        except Exception:
            cap_eligible = None
        else:
            cap_eligible = None if not database_row else database_row[0]
        if self._forecast_policy_read_cache is not None:
            self._forecast_policy_read_cache[cache_key] = cap_eligible
        return cap_eligible

    async def _calculate_hour_production(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        capacity: float,
        snow_covered_panels: bool = False,
        ensemble_group_weights: Optional[Dict] = None,
        snow_per_group: Optional[Dict[str, bool]] = None,
        tfs_hour_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Hybrid Physics + ML prediction. @zara"""
        if False:
            __assert_armored__ = None
        try:
            hour = hour_dt.hour
            cloud_cover = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            astronomy = self._get_astronomy_for_hour(hour_dt)
            sun_elevation = astronomy.get("sun_elevation_deg", 30)
            hour_start = hour_dt.replace(minute=0, second=0, microsecond=0)
            if should_zero_hourly_yield(
                sun_elevation_deg=sun_elevation,
                hour_start=hour_start,
                sunset=astronomy.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=sun_elevation)
            clear_sky_ghi_wm2 = astronomy.get("clear_sky_radiation_wm2", 0)
            ghi_wm2 = float(
                hour_data.get(
                    "solar_radiation",
                    hour_data.get("ghi", hour_data.get("solar_radiation_wm2", 0)),
                )
            )
            original_ghi_wm2 = ghi_wm2
            coherence_ghi_wm2 = ghi_wm2
            coherence_cap_applied = 0
            corrected_cloud_cover = self._correct_cloud_cover_from_ghi(
                cloud_cover, ghi_wm2, clear_sky_ghi_wm2, sun_elevation
            )
            if corrected_cloud_cover != cloud_cover:
                hour_data = dict(hour_data)
                hour_data["cloud_cover"] = corrected_cloud_cover
                hour_data["clouds"] = corrected_cloud_cover
                cloud_cover = corrected_cloud_cover
            coherence_cap_eligible = await self._get_ghi_coherence_cap_eligible(
                hour_data.get("correction_snapshot_id")
            )
            if (
                strategy_ghi_coherence_cap_allowed(coherence_cap_eligible)
                and clear_sky_ghi_wm2 > 0
                and (cloud_cover > 20)
            ):
                cloud_fraction = cloud_cover / 100.0
                coherent_ghi_wm2 = clear_sky_ghi_wm2 * math.exp(-2.0 * cloud_fraction)
                clear_sky_floor_ratio = _compute_dynamic_clear_sky_floor_ratio(
                    clouds=cloud_cover,
                    ghi=ghi_wm2,
                    dni=float(
                        hour_data.get("direct_radiation", hour_data.get("dni", 0.0))
                        or 0.0
                    ),
                    dhi=float(
                        hour_data.get("diffuse_radiation", hour_data.get("dhi", 0.0))
                        or 0.0
                    ),
                    clear_sky=clear_sky_ghi_wm2,
                    sun_elevation=sun_elevation,
                    humidity=float(hour_data.get("humidity", 0.0) or 0.0),
                    rain=float(
                        hour_data.get("rain", hour_data.get("precipitation", 0.0))
                        or 0.0
                    ),
                )
                coherent_ghi_wm2 = max(
                    coherent_ghi_wm2, clear_sky_ghi_wm2 * clear_sky_floor_ratio
                )
                if ghi_wm2 > coherent_ghi_wm2 * 1.15:
                    corrected_ghi_wm2 = round(coherent_ghi_wm2, 1)
                    hour_data = dict(hour_data)
                    hour_data["solar_radiation"] = corrected_ghi_wm2
                    hour_data["ghi"] = corrected_ghi_wm2
                    hour_data["solar_radiation_wm2"] = corrected_ghi_wm2
                    ratio = corrected_ghi_wm2 / ghi_wm2 if ghi_wm2 > 0 else 1.0
                    for radiation_key in ("direct_radiation", "dni"):
                        if radiation_key in hour_data and hour_data[radiation_key]:
                            hour_data[radiation_key] = round(
                                float(hour_data[radiation_key]) * ratio, 1
                            )
                    for radiation_key in ("diffuse_radiation", "dhi"):
                        if radiation_key in hour_data and hour_data[radiation_key]:
                            hour_data[radiation_key] = round(
                                float(hour_data[radiation_key]) * ratio, 1
                            )
                    coherence_ghi_wm2 = corrected_ghi_wm2
                    coherence_cap_applied = 1
                    _LOGGER.debug(
                        "GHI plausibility: clouds=%.0f%%, raw=%.0f, max_plausible=%.0f, corrected=%.0f W/m², floor=%.3f",
                        cloud_cover,
                        ghi_wm2,
                        coherent_ghi_wm2,
                        corrected_ghi_wm2,
                        clear_sky_floor_ratio,
                    )
            regime_ghi_wm2 = float(
                hour_data.get(
                    "solar_radiation",
                    hour_data.get("ghi", hour_data.get("solar_radiation_wm2", 0)),
                )
            )
            rain_mm = float(
                hour_data.get("rain", hour_data.get("precipitation", 0.0)) or 0.0
            )
            humidity_percent = float(hour_data.get("humidity", 70.0) or 70.0)
            dni_wm2 = float(
                hour_data.get("direct_radiation", hour_data.get("dni", 0.0)) or 0.0
            )
            dhi_wm2 = float(
                hour_data.get("diffuse_radiation", hour_data.get("dhi", 0.0)) or 0.0
            )
            blend_regime = _classify_blend_weather_regime(
                clouds=cloud_cover,
                ghi=regime_ghi_wm2,
                dni=dni_wm2,
                dhi=dhi_wm2,
                clear_sky=clear_sky_ghi_wm2,
                rain=rain_mm,
                humidity=humidity_percent,
                sun_elevation=sun_elevation,
                hour=hour,
            )
            weather_regime = _classify_static_forecast_weather_regime(
                clouds=cloud_cover,
                ghi=regime_ghi_wm2,
                dni=dni_wm2,
                dhi=dhi_wm2,
                clear_sky=clear_sky_ghi_wm2,
                rain=rain_mm,
                humidity=humidity_percent,
                sun_elevation=sun_elevation,
                hour=hour,
            )
            regime_learning = await self._get_weather_regime_learning_adjustment(
                weather_regime, hour
            )
            if regime_learning.get("sample_count", 0) >= REGIME_METHOD_SOFT_MIN_SAMPLES:
                self._record_forecast_debug("regime_learning_available")
                if float(regime_learning.get("learning_strength") or 0.0) < 1.0:
                    self._record_forecast_debug("regime_learning_shrinkage")
            similar_weather = await self._get_similar_weather_underforecast_signal(
                target_date=hour_dt.date().isoformat(),
                weather_regime=weather_regime,
                hour=hour,
                clouds=cloud_cover,
                ghi=regime_ghi_wm2,
                dhi=dhi_wm2,
                clear_sky=clear_sky_ghi_wm2,
                rain=rain_mm,
                sun_elevation=sun_elevation,
            )
            physics_kwh, physics_groups = self._calculate_physics_production(
                hour_data, hour_dt, snow_covered_panels, snow_per_group=snow_per_group
            )
            shadow_by_group = {}
            sun_azimuth = astronomy.get("sun_azimuth_deg")
            total_radiation_wm2 = dni_wm2 + dhi_wm2
            direct_share = (
                dni_wm2 / total_radiation_wm2 if total_radiation_wm2 > 0.0 else 0.0
            )
            if physics_kwh > 0 and physics_groups:
                if self._physics_calibrator is not None:
                    for group in physics_groups:
                        group_key = str(group.get("name") or "")
                        if not group_key:
                            continue
                        group_original_kwh = float(group.get("power_kwh") or 0.0)
                        shadow_adjusted_kwh = self._physics_calibrator.apply_sun_shadow(
                            group_key,
                            group_original_kwh,
                            float(group.get("raw_physics_kwh") or 0.0),
                            sun_azimuth,
                            sun_elevation,
                            direct_share,
                        )
                        shadow_by_group[group_key] = {
                            "model": "sun_position",
                            "azimuth_deg": sun_azimuth,
                            "elevation_deg": sun_elevation,
                            "direct_share": round(direct_share, 4),
                            "adjusted_kwh": round(shadow_adjusted_kwh, 4),
                        }
                        if abs(shadow_adjusted_kwh - group_original_kwh) < 1e-09:
                            continue
                        group["power_kwh"] = round(shadow_adjusted_kwh, 4)
                        if group.get("physics_kwh") is not None:
                            group["physics_kwh"] = round(shadow_adjusted_kwh, 4)
                            continue
                    physics_kwh = sum(
                        (
                            float(group.get("power_kwh") or 0.0)
                            for group in physics_groups
                        )
                    )
            ai_kwh = 0.0
            ai_groups = None
            ai_confidence = 0.0
            learned_ai_floor = 0.0
            bootstrap_phase = False
            lstm_kwh = 0.0
            ridge_kwh = 0.0
            lstm_groups = None
            ridge_groups = None
            lstm_available = False
            ridge_available = False
            guard_tfs_kwh = 0.0
            bad_weather_regime = None
            if tfs_hour_data:
                guard_tfs_kwh = float(tfs_hour_data.get("p50", 0.0) or 0.0)
            if self._use_ai and self.ai_predictor:
                model_r2 = self.ai_predictor.current_accuracy or 0.0
                bootstrap_phase = self.ai_predictor.is_bootstrap_phase()
                if bootstrap_phase:
                    training_samples = self.ai_predictor.training_samples or 10
                    ai_confidence = self._calculate_bootstrap_confidence(
                        model_r2, training_samples, cloud_cover, sun_elevation
                    )
                else:
                    ai_confidence = self._calculate_hourly_ai_confidence(
                        model_r2, cloud_cover, sun_elevation, hour
                    )
                learned_adjustment = await self._get_learned_confidence_adjustment(
                    cloud_cover, hour
                )
                learned_adjustment = StaticForecastBlendPolicy.merge_regime_learning(
                    learned_adjustment, regime_learning
                )
                ai_confidence = ai_confidence * learned_adjustment["advantage_factor"]
                if (
                    learned_adjustment["physics_mae"]
                    and learned_adjustment["ai_mae"]
                    and (learned_adjustment["sample_count"] >= 10)
                ):
                    physics_mae = max(0.001, learned_adjustment["physics_mae"])
                    ai_mae = max(0.001, learned_adjustment["ai_mae"])
                    inverse_error_sum = 1.0 / physics_mae + 1.0 / ai_mae
                    mae_based_ai_weight = 1.0 / ai_mae / inverse_error_sum
                    ai_confidence = 0.5 * ai_confidence + 0.5 * mae_based_ai_weight
                ai_confidence = min(0.96, max(0.0, ai_confidence))
                learned_ai_floor = self._calculate_learned_ai_confidence_floor(
                    learned_adjustment, blend_regime
                )
                if ai_confidence > 0:
                    bad_weather_regime = _get_bad_weather_ai_regime(
                        clouds=cloud_cover,
                        ghi=regime_ghi_wm2,
                        dni=dni_wm2,
                        dhi=dhi_wm2,
                        clear_sky=clear_sky_ghi_wm2,
                        rain=rain_mm,
                        humidity=humidity_percent,
                        sun_elevation=sun_elevation,
                        hour=hour,
                        season=get_season(hour_dt.month),
                    )
                    if bad_weather_regime and _should_apply_bad_weather_ai_guard(
                        blend_regime
                    ):
                        bad_weather_confidence_multiplier = float(
                            bad_weather_regime["confidence_multiplier"]
                        )
                        if learned_adjustment.get("sample_count", 0) >= 10:
                            physics_mae = learned_adjustment.get("physics_mae")
                            ai_mae = learned_adjustment.get("ai_mae")
                            if physics_mae and ai_mae and (ai_mae > physics_mae):
                                physics_ai_error_ratio = min(
                                    3.0, ai_mae / max(physics_mae, 0.001)
                                )
                                bad_weather_confidence_multiplier *= max(
                                    0.35,
                                    min(
                                        1.0, 1.0 - (physics_ai_error_ratio - 1.0) * 0.18
                                    ),
                                )
                        ai_confidence *= bad_weather_confidence_multiplier
                        self._record_forecast_debug("bad_weather_confidence")
                    elif bad_weather_regime:
                        self._record_forecast_debug("bad_weather_guard_skipped")
                    model_weather = {
                        "temperature": hour_data.get("temperature", 20),
                        "solar_radiation_wm2": hour_data.get(
                            "solar_radiation", hour_data.get("ghi", 0)
                        ),
                        "wind": hour_data.get("wind_speed", 3),
                        "humidity": humidity_percent,
                        "rain": rain_mm,
                        "clouds": cloud_cover,
                        "dni": dni_wm2,
                        "pressure": hour_data.get("pressure", 1013.0),
                        "diffuse_radiation": dhi_wm2,
                    }
                    ensemble_prediction = (
                        await self.ai_predictor.predict_hour_per_group_ensemble(
                            hour=hour,
                            weather_data=model_weather,
                            astronomy_data=astronomy,
                        )
                    )
                    lstm_kwh = ensemble_prediction["lstm_total"]
                    ridge_kwh = ensemble_prediction["ridge_total"]
                    lstm_available = (
                        lstm_kwh > 0
                        and len(ensemble_prediction["lstm_predictions"]) > 0
                    )
                    ridge_available = (
                        ridge_kwh > 0
                        and len(ensemble_prediction["ridge_predictions"]) > 0
                    )

                    def _groups_from_predictions(preds):
                        total_group_kwh = sum(
                            (prediction.power_kwh for prediction in preds)
                        )
                        groups = []
                        for prediction in preds:
                            group_share_percent = (
                                prediction.power_kwh / total_group_kwh * 100
                                if total_group_kwh > 0
                                else 0
                            )
                            groups.append(
                                {
                                    "name": prediction.group_name,
                                    "power_kwh": round(prediction.power_kwh, 4),
                                    "contribution_percent": round(
                                        group_share_percent, 1
                                    ),
                                    "azimuth_deg": prediction.azimuth,
                                    "tilt_deg": prediction.tilt,
                                    "capacity_kwp": prediction.capacity_kwp,
                                }
                            )
                        return groups

                    if lstm_available:
                        lstm_groups = _groups_from_predictions(
                            ensemble_prediction["lstm_predictions"]
                        )
                    if ridge_available:
                        ridge_groups = _groups_from_predictions(
                            ensemble_prediction["ridge_predictions"]
                        )
                    if lstm_available and ridge_available:
                        cloud_bucket = (
                            "clear"
                            if cloud_cover < 30
                            else "partly_cloudy" if cloud_cover < 70 else "overcast"
                        )
                        hour_bucket = (
                            "morning"
                            if hour < 10
                            else "midday" if hour < 15 else "afternoon"
                        )
                        ai_groups = self._blend_ensemble_groups_adaptive(
                            lstm_groups,
                            ridge_groups,
                            (cloud_bucket, hour_bucket),
                            ensemble_group_weights,
                        )
                        ai_kwh = sum(
                            (group_result["power_kwh"] for group_result in ai_groups)
                        )
                    elif lstm_available:
                        ai_kwh = lstm_kwh
                        ai_groups = lstm_groups
                    elif ridge_available:
                        ai_kwh = ridge_kwh
                        ai_groups = ridge_groups
                    active_ai_guard_regime = (
                        bad_weather_regime
                        if _should_apply_bad_weather_ai_guard(blend_regime)
                        else None
                    )
                    (
                        guarded_ai_kwh,
                        guard_reason,
                        guard_cap_kwh,
                        guard_ratio,
                        guard_reference_kwh,
                    ) = _apply_bad_weather_ai_regime(
                        ai_pred=ai_kwh,
                        physics_pred=physics_kwh,
                        tfs_pred=guard_tfs_kwh,
                        regime=active_ai_guard_regime,
                    )
                    if guarded_ai_kwh < ai_kwh:
                        guard_scale = guarded_ai_kwh / ai_kwh if ai_kwh > 0 else 1.0
                        ai_kwh = guarded_ai_kwh
                        lstm_kwh *= guard_scale
                        ridge_kwh *= guard_scale
                        ai_groups = _scale_group_predictions(ai_groups, guard_scale)
                        lstm_groups = _scale_group_predictions(lstm_groups, guard_scale)
                        ridge_groups = _scale_group_predictions(
                            ridge_groups, guard_scale
                        )
                        self._record_forecast_debug("bad_weather_cap")
                    guarded_ai_kwh, overcast_cap_kwh = _cap_ai_for_dense_overcast(
                        ai_pred=ai_kwh,
                        physics_pred=physics_kwh,
                        tfs_pred=guard_tfs_kwh,
                        clouds=cloud_cover,
                        ghi=regime_ghi_wm2,
                        clear_sky=clear_sky_ghi_wm2,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
                    if guarded_ai_kwh < ai_kwh:
                        guard_scale = guarded_ai_kwh / ai_kwh if ai_kwh > 0 else 1.0
                        ai_kwh = guarded_ai_kwh
                        lstm_kwh *= guard_scale
                        ridge_kwh *= guard_scale
                        ai_groups = _scale_group_predictions(ai_groups, guard_scale)
                        lstm_groups = _scale_group_predictions(lstm_groups, guard_scale)
                        ridge_groups = _scale_group_predictions(
                            ridge_groups, guard_scale
                        )
                        self._record_forecast_debug("dense_overcast_ai_cap")
                    if lstm_available and ridge_available:
                        ensemble_mean_kwh = (lstm_kwh + ridge_kwh) / 2.0
                        if ensemble_mean_kwh > 0.005:
                            ensemble_disagreement = (
                                abs(lstm_kwh - ridge_kwh) / ensemble_mean_kwh
                            )
                        else:
                            ensemble_disagreement = 0.0
                        if ensemble_disagreement > DISAGREEMENT_THRESHOLD:
                            disagreement_factor = max(
                                0.5, 1.0 - ensemble_disagreement * 0.5
                            )
                            ai_confidence *= disagreement_factor
                            _LOGGER.log(
                                5,
                                "Ensemble disagreement %.1f%% > %.0f%%: penalty=%.2f, confidence=%.2f (LSTM=%.3f, Ridge=%.3f)",
                                ensemble_disagreement * 100,
                                DISAGREEMENT_THRESHOLD * 100,
                                disagreement_factor,
                                ai_confidence,
                                lstm_kwh,
                                ridge_kwh,
                            )
            deviation_guard_applied = False
            effective_ai_weight = ai_confidence
            if ai_confidence > 0 and ai_kwh > 0 and (physics_kwh > 0):
                ai_physics_deviation = abs(ai_kwh - physics_kwh) / physics_kwh
                ai_above_physics = ai_kwh > physics_kwh
                adjusted_deviation = ai_physics_deviation
                mean_calibration_factor = 1.0
                mean_bucket_factor = 1.0
                allowed_deviation = MAX_AI_PHYSICS_DEVIATION
                if self._physics_calibrator and self._physics_calibrator._factors:
                    calibration_factors = []
                    for (
                        group_key,
                        calibration_factor,
                    ) in self._physics_calibrator._factors.items():
                        if calibration_factor.sample_count >= 3:
                            calibration_factors.append(calibration_factor.global_factor)
                    if calibration_factors:
                        mean_calibration_factor = sum(calibration_factors) / len(
                            calibration_factors
                        )
                    if ai_above_physics:
                        bucket_factors = []
                        for group_key in self._physics_calibrator._factors:
                            group_correction = (
                                self._physics_calibrator.get_correction_factor(
                                    group_name=group_key,
                                    hour=hour,
                                    cloud_cover=cloud_cover,
                                    sun_elevation=sun_elevation,
                                )
                            )
                            bucket_factors.append(group_correction)
                        if bucket_factors:
                            mean_bucket_factor = sum(bucket_factors) / len(
                                bucket_factors
                            )
                        if mean_bucket_factor > BUCKET_CAL_THRESHOLD:
                            bucket_extra = mean_bucket_factor - 1.0
                            adjusted_deviation = max(
                                0.0, ai_physics_deviation - bucket_extra
                            )
                            allowed_deviation = min(
                                MAX_DYNAMIC_DEVIATION,
                                MAX_AI_PHYSICS_DEVIATION
                                + bucket_extra * BUCKET_CAL_BOOST,
                            )
                            _LOGGER.log(
                                5,
                                "Asymmetric safeguard [upward]: bucket_cal=%.3f, eff_dev=%.3f, dynamic_max=%.3f (dev=%.3f)",
                                mean_bucket_factor,
                                adjusted_deviation,
                                allowed_deviation,
                                ai_physics_deviation,
                            )
                        else:
                            calibration_delta = abs(mean_calibration_factor - 1.0)
                            if (
                                mean_calibration_factor > 1.0
                                and calibration_delta > 0.05
                            ):
                                adjusted_deviation = max(
                                    0.0, ai_physics_deviation - calibration_delta * 0.7
                                )
                                allowed_deviation = min(
                                    MAX_DYNAMIC_DEVIATION,
                                    MAX_AI_PHYSICS_DEVIATION
                                    + calibration_delta * BUCKET_CAL_BOOST,
                                )
                    else:
                        calibration_delta = abs(mean_calibration_factor - 1.0)
                        calibration_is_upward = mean_calibration_factor > 1.0
                        if (
                            ai_above_physics == calibration_is_upward
                            and calibration_delta > 0.05
                        ):
                            adjusted_deviation = max(
                                0.0, ai_physics_deviation - calibration_delta * 0.7
                            )
                if adjusted_deviation <= allowed_deviation:
                    deviation_guard_applied = True
                else:
                    ridge_confirms_lstm = False
                    models_agree_on_direction = False
                    if lstm_available and ridge_available:
                        comparison_mean_kwh = (lstm_kwh + ridge_kwh) / 2.0
                        if comparison_mean_kwh > 0.005:
                            model_direction_disagreement = (
                                abs(lstm_kwh - ridge_kwh) / comparison_mean_kwh
                            )
                            ridge_confirms_lstm = (
                                model_direction_disagreement <= RIDGE_CONFIRMS_THRESHOLD
                            )
                        if physics_kwh > 0.005:
                            lstm_physics_ratio = lstm_kwh / physics_kwh
                            ridge_physics_ratio = ridge_kwh / physics_kwh
                            both_models_above_physics = (
                                lstm_physics_ratio > 1.1 and ridge_physics_ratio > 1.1
                            )
                            both_models_below_physics = (
                                lstm_physics_ratio < 0.9 and ridge_physics_ratio < 0.9
                            )
                            models_agree_on_direction = (
                                both_models_above_physics or both_models_below_physics
                            )
                    significant_calibration = abs(mean_calibration_factor - 1.0) > 0.15
                    group_learning_supported = await self._check_group_learning_support(
                        cloud_cover, hour, physics_groups, ai_groups
                    )
                    ensemble_direction_supported = (
                        ridge_confirms_lstm or models_agree_on_direction
                    )
                    if ensemble_direction_supported and (
                        significant_calibration or group_learning_supported
                    ):
                        deviation_guard_applied = True
                        excessive_deviation = max(
                            0.0, adjusted_deviation - allowed_deviation
                        )
                        deviation_penalty = max(0.4, 1.0 - excessive_deviation * 0.4)
                        effective_ai_weight = max(
                            learned_ai_floor, ai_confidence * deviation_penalty
                        )
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [strong]: AI agree + calibrator/learning, dev=%.0f%%, taper=%.2f, conf=%.2f->%.2f",
                            ai_physics_deviation * 100,
                            deviation_penalty,
                            ai_confidence,
                            effective_ai_weight,
                        )
                    elif ensemble_direction_supported:
                        deviation_guard_applied = True
                        excessive_deviation = max(
                            0.0, adjusted_deviation - allowed_deviation
                        )
                        deviation_penalty = max(0.2, 1.0 - excessive_deviation * 0.6)
                        effective_ai_weight = max(
                            learned_ai_floor, ai_confidence * deviation_penalty
                        )
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [moderate]: AI agree, dev=%.0f%%, taper=%.2f, conf=%.2f->%.2f",
                            ai_physics_deviation * 100,
                            deviation_penalty,
                            ai_confidence,
                            effective_ai_weight,
                        )
                    elif significant_calibration:
                        deviation_guard_applied = True
                        calibration_distance = abs(mean_calibration_factor - 1.0)
                        calibration_support_weight = min(
                            0.6, 0.25 + calibration_distance * 0.5
                        )
                        effective_ai_weight = max(
                            learned_ai_floor, ai_confidence * calibration_support_weight
                        )
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [calibrator]: cal supports deviation, dev=%.0f%%, conf=%.2f->%.2f",
                            ai_physics_deviation * 100,
                            ai_confidence,
                            effective_ai_weight,
                        )
                    elif ai_physics_deviation <= 1.0:
                        deviation_guard_applied = True
                        effective_ai_weight = max(learned_ai_floor, ai_confidence * 0.2)
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [minimal]: dev=%.0f%%, conf=%.2f->%.2f",
                            ai_physics_deviation * 100,
                            ai_confidence,
                            effective_ai_weight,
                        )
            morning_confidence_override = resolve_morning_ai_underblend_confidence(
                hour=hour,
                sun_elevation=sun_elevation,
                physics_kwh=physics_kwh,
                ai_kwh=ai_kwh,
                blend_regime=blend_regime,
                rain=rain_mm,
                current_confidence=(
                    effective_ai_weight
                    if deviation_guard_applied
                    else float(ai_confidence or 0.0)
                ),
            )
            if morning_confidence_override is not None:
                deviation_guard_applied = True
                effective_ai_weight = float(morning_confidence_override)
                self._record_forecast_debug("morning_ai_underblend_lift")
            if deviation_guard_applied:
                blended_kwh = (
                    physics_kwh * (1 - effective_ai_weight)
                    + ai_kwh * effective_ai_weight
                )
                if ai_groups and physics_groups:
                    blended_groups = self._blend_group_details(
                        physics_groups, ai_groups, effective_ai_weight
                    )
                else:
                    blended_groups = ai_groups or physics_groups
                model_name = "ridge" if bootstrap_phase else "lstm"
                blend_method = f"physics_{model_name}{int(effective_ai_weight * 100)}"
                if blended_groups:
                    for group_result in blended_groups:
                        group_result["source"] = blend_method
            else:
                blended_kwh = physics_kwh
                blended_groups = physics_groups
                if blended_groups:
                    for group_result in blended_groups:
                        group_result["source"] = "physics"
            if blended_groups and physics_groups:
                physics_by_group = {
                    group_result["name"]: group_result.get(
                        "physics_kwh", group_result.get("power_kwh")
                    )
                    for group_result in physics_groups
                }
                raw_physics_by_group = {
                    group_result["name"]: group_result.get("raw_physics_kwh")
                    for group_result in physics_groups
                }
                for group_result in blended_groups:
                    if "physics_kwh" not in group_result:
                        group_result["physics_kwh"] = physics_by_group.get(
                            group_result.get("name")
                        )
                    if "raw_physics_kwh" not in group_result:
                        group_result["raw_physics_kwh"] = raw_physics_by_group.get(
                            group_result.get("name")
                        )
            if blended_groups and ai_groups:
                ai_by_group = {
                    group_result["name"]: group_result["power_kwh"]
                    for group_result in ai_groups
                }
                for group_result in blended_groups:
                    if "ai_kwh" not in group_result:
                        group_result["ai_kwh"] = (
                            round(ai_by_group[group_result["name"]], 4)
                            if group_result.get("name") in ai_by_group
                            else None
                        )
            if blended_groups and lstm_available and lstm_groups:
                lstm_by_group = {
                    group_result["name"]: group_result["power_kwh"]
                    for group_result in lstm_groups
                }
                for group_result in blended_groups:
                    if "lstm_kwh" not in group_result:
                        group_result["lstm_kwh"] = (
                            round(lstm_by_group[group_result["name"]], 4)
                            if group_result.get("name") in lstm_by_group
                            else None
                        )
            if blended_groups and ridge_available and ridge_groups:
                ridge_by_group = {
                    group_result["name"]: group_result["power_kwh"]
                    for group_result in ridge_groups
                }
                for group_result in blended_groups:
                    if "ridge_kwh" not in group_result:
                        group_result["ridge_kwh"] = (
                            round(ridge_by_group[group_result["name"]], 4)
                            if group_result.get("name") in ridge_by_group
                            else None
                        )
            tfs_kwh = 0.0
            tfs_by_group = None
            tfs_weight = None
            if tfs_hour_data:
                tfs_kwh = float(tfs_hour_data.get("p50", 0.0))
                if tfs_kwh > 0:
                    tfs_groups = tfs_hour_data.get("groups")
                    if tfs_groups and blended_groups:
                        tfs_by_group = self._map_tfs_groups(tfs_groups, blended_groups)
                    tfs_weight = await _compute_tfs_weight(
                        db=self._db,
                        tfs_hour_data=tfs_hour_data,
                        local_clouds=cloud_cover,
                        group_name=None,
                        hour=hour_dt.hour,
                        adaptive_enabled=True,
                    )
                    base_tfs_weight = tfs_weight
                    tfs_reference_kwh = max(
                        blended_kwh, physics_kwh, ai_kwh if ai_kwh > 0 else 0.0
                    )
                    tfs_weight = _apply_tfs_low_sun_envelope(
                        tfs_weight, tfs_kwh, tfs_reference_kwh, sun_elevation
                    )
                    if tfs_weight != base_tfs_weight:
                        self._record_forecast_debug("tfs_low_sun_envelope")
                    tfs_weight, tfs_adjustment_reason = (
                        StaticForecastBlendPolicy.adjust_tfs_weight(
                            tfs_weight,
                            blend_regime=blend_regime,
                            weather_regime=weather_regime,
                            regime_learning=regime_learning,
                            similar_weather=similar_weather,
                            physics_pred=physics_kwh,
                            ai_pred=ai_kwh,
                            tfs_pred=tfs_kwh,
                            rain=rain_mm,
                            humidity=humidity_percent,
                            sun_elevation=sun_elevation,
                        )
                    )
                    if tfs_adjustment_reason:
                        self._record_forecast_debug("tfs_policy_adjustment")
                        self._record_forecast_debug(tfs_adjustment_reason)
                    blended_kwh = blended_kwh * (1 - tfs_weight) + tfs_kwh * tfs_weight
                    if tfs_by_group and blended_groups:
                        for group_result in blended_groups:
                            group_tfs_kwh = tfs_by_group.get(group_result["name"])
                            if group_tfs_kwh is not None:
                                group_tfs_weight = await _compute_tfs_weight(
                                    db=self._db,
                                    tfs_hour_data=tfs_hour_data,
                                    local_clouds=cloud_cover,
                                    group_name=group_result.get("name"),
                                    hour=hour_dt.hour,
                                    adaptive_enabled=True,
                                )
                                group_reference_kwh = max(
                                    float(group_result.get("power_kwh") or 0.0),
                                    float(group_result.get("physics_kwh") or 0.0),
                                    float(group_result.get("ai_kwh") or 0.0),
                                )
                                group_tfs_weight = _apply_tfs_low_sun_envelope(
                                    group_tfs_weight,
                                    group_tfs_kwh,
                                    group_reference_kwh,
                                    sun_elevation,
                                )
                                group_tfs_weight, details = (
                                    StaticForecastBlendPolicy.adjust_tfs_weight(
                                        group_tfs_weight,
                                        blend_regime=blend_regime,
                                        weather_regime=weather_regime,
                                        regime_learning=regime_learning,
                                        similar_weather=similar_weather,
                                        physics_pred=float(
                                            group_result.get("physics_kwh") or 0.0
                                        ),
                                        ai_pred=float(
                                            group_result.get("ai_kwh") or 0.0
                                        ),
                                        tfs_pred=group_tfs_kwh,
                                        rain=rain_mm,
                                        humidity=humidity_percent,
                                        sun_elevation=sun_elevation,
                                    )
                                )
                                group_result["power_kwh"] = round(
                                    group_result["power_kwh"] * (1 - group_tfs_weight)
                                    + group_tfs_kwh * group_tfs_weight,
                                    4,
                                )
                                group_result["tfs_weight"] = round(group_tfs_weight, 3)
                            group_result["tfs_kwh"] = (
                                round(group_tfs_kwh, 4)
                                if group_tfs_kwh is not None
                                else None
                            )
                            continue
                    self._record_forecast_debug("tfs_adaptive_blend")
            if (not tfs_hour_data or tfs_kwh <= 0) and blended_groups:
                for group_result in blended_groups:
                    group_result["tfs_kwh"] = None
                    group_result["tfs_weight"] = None
            similar_weather_adjusted_kwh, similar_weather_reason = (
                StaticForecastBlendPolicy.adjust_prediction_for_similar_weather(
                    blended_kwh,
                    similar_weather,
                    physics_pred=physics_kwh,
                    ai_pred=ai_kwh,
                    tfs_pred=tfs_kwh,
                    capacity=capacity,
                )
            )
            if similar_weather_reason:
                if blended_groups and blended_kwh > 0:
                    blended_groups = _scale_group_predictions(
                        blended_groups, similar_weather_adjusted_kwh / blended_kwh
                    )
                self._record_forecast_debug("similar_weather_adjustment")
                self._record_forecast_debug(similar_weather_reason)
                blended_kwh = similar_weather_adjusted_kwh
            regime_adjusted_kwh, regime_bias_reason = (
                StaticForecastBlendPolicy.adjust_prediction_for_regime_bias(
                    blended_kwh,
                    regime_learning,
                    blend_regime=blend_regime,
                    capacity=capacity,
                )
            )
            if regime_bias_reason:
                if blended_groups and blended_kwh > 0:
                    blended_groups = _scale_group_predictions(
                        blended_groups, regime_adjusted_kwh / blended_kwh
                    )
                self._record_forecast_debug("static_regime_bias")
                self._record_forecast_debug(regime_bias_reason)
                blended_kwh = regime_adjusted_kwh
            production_kwh = max(0.0, min(blended_kwh, capacity * 1.0))
            blended_groups = normalize_group_predictions_to_total(
                blended_groups, production_kwh
            )
            if blended_groups:
                for group in blended_groups:
                    group_shadow = shadow_by_group.get(str(group.get("name") or ""))
                    if group_shadow:
                        group["shadow_context"] = group_shadow
            if should_zero_hourly_yield(
                sun_elevation_deg=sun_elevation,
                hour_start=hour_start,
                sunset=astronomy.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=sun_elevation)
            return {
                "production": production_kwh,
                "groups": blended_groups,
                "physics_kwh": round(physics_kwh, 4),
                "ai_kwh": round(ai_kwh, 4) if ai_kwh > 0 and physics_kwh > 0 else None,
                "ai_confidence": (
                    round(effective_ai_weight, 4) if effective_ai_weight > 0 else None
                ),
                "method": "AI + Physics" if deviation_guard_applied else "physics",
                "ml_contribution": (
                    int(effective_ai_weight * 100) if deviation_guard_applied else 0
                ),
                "lstm_kwh": round(lstm_kwh, 4) if lstm_available else None,
                "ridge_kwh": round(ridge_kwh, 4) if ridge_available else None,
                "tfs_kwh": round(tfs_kwh, 4) if tfs_kwh > 0 else None,
                "tfs_weight": round(tfs_weight, 3) if tfs_weight is not None else None,
                "weather_regime": weather_regime,
                "blend_regime": blend_regime,
                "shadow_context": shadow_by_group,
                "sun_elevation_deg": sun_elevation,
                "ghi_before_cap": original_ghi_wm2,
                "ghi_after_cap": coherence_ghi_wm2,
                "ghi_cap_applied": coherence_cap_applied,
            }
        except Exception as error:
            _LOGGER.warning(
                "Hour calculation failed for %s hour %02d: %s; using physics fallback",
                hour_dt.date().isoformat(),
                hour_dt.hour,
                error,
            )
            _LOGGER.debug("Hour calculation traceback", exc_info=True)
            astronomy = self._get_astronomy_for_hour(hour_dt)
            sun_elevation = astronomy.get("sun_elevation_deg", 30)
            hour_start = hour_dt.replace(minute=0, second=0, microsecond=0)
            if should_zero_hourly_yield(
                sun_elevation_deg=sun_elevation,
                hour_start=hour_start,
                sunset=astronomy.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=sun_elevation)
            production_kwh, details = self._calculate_physics_production(
                hour_data, hour_dt, snow_covered_panels
            )
            return {
                "production": production_kwh,
                "groups": None,
                "physics_kwh": round(production_kwh, 4),
                "ai_kwh": None,
                "ai_confidence": None,
                "method": "physics",
                "ml_contribution": 0,
                "lstm_kwh": None,
                "ridge_kwh": None,
                "tfs_kwh": None,
                "tfs_weight": None,
                "weather_regime": None,
                "blend_regime": None,
                "sun_elevation_deg": sun_elevation,
                "ghi_before_cap": None,
                "ghi_after_cap": None,
                "ghi_cap_applied": 0,
            }

    def _calculate_physics_production(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        snow_covered_panels: bool = False,
        snow_per_group: Optional[Dict[str, bool]] = None,
    ) -> Tuple[float, Optional[List[Dict[str, Any]]]]:
        """Physics-based calculation with panel groups and calibration. @zara"""
        try:
            ghi_wm2 = float(
                hour_data.get(
                    "solar_radiation",
                    hour_data.get("ghi", hour_data.get("solar_radiation_wm2", 0)),
                )
            )
            dni_wm2 = float(hour_data.get("direct_radiation", hour_data.get("dni", 0)))
            dhi_wm2 = float(hour_data.get("diffuse_radiation", hour_data.get("dhi", 0)))
            temperature_c = float(hour_data.get("temperature", 15))
            cloud_cover = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            visibility_m = hour_data.get("visibility_m")
            precipitation_mm = hour_data.get(
                "rain", hour_data.get("precipitation", None)
            )
            if precipitation_mm is not None:
                precipitation_mm = float(precipitation_mm)
            raw_weather_code = hour_data.get("weather_code")
            if raw_weather_code is not None:
                try:
                    weather_code = int(raw_weather_code)
                    if weather_code in (71, 73, 75, 77, 85, 86, 66, 67):
                        if precipitation_mm is None or precipitation_mm <= 0.5:
                            precipitation_mm = 1.0
                except (ValueError, TypeError):
                    pass
            humidity_percent = hour_data.get("humidity")
            wind_speed = hour_data.get("wind_speed")
            frost_detected, frost_score = estimate_frost_from_forecast(
                temperature_c, humidity_percent, wind_speed, hour_dt.hour
            )
            astronomy = self._get_astronomy_for_hour(hour_dt)
            sun_elevation = astronomy.get("sun_elevation_deg", 0)
            sun_azimuth = astronomy.get("sun_azimuth_deg", 180)
            clear_sky_ghi_wm2 = astronomy.get("clear_sky_radiation_wm2", 0)
            hour_start = hour_dt.replace(minute=0, second=0, microsecond=0)
            if should_zero_hourly_yield(
                sun_elevation_deg=sun_elevation,
                hour_start=hour_start,
                sunset=astronomy.get("sunset"),
            ):
                return (0.0, None)
            twilight = TWILIGHT_MIN_ELEVATION <= sun_elevation <= TWILIGHT_MAX_ELEVATION
            low_sun = (
                TWILIGHT_MAX_ELEVATION < sun_elevation <= LOW_SUN_MAX_ELEVATION
                and cloud_cover < LOW_SUN_CLOUD_THRESHOLD
            )
            if ghi_wm2 <= 0:
                if twilight and sun_elevation > 0:
                    ghi_wm2, dhi_wm2 = self._estimate_twilight_radiation(
                        sun_elevation, cloud_cover
                    )
                    dni_wm2 = 0.0
                else:
                    return (0.0, None)
            elif low_sun:
                ghi_wm2, dhi_wm2, dni_wm2 = self._correct_low_sun_radiation(
                    sun_elevation,
                    cloud_cover,
                    ghi_wm2,
                    dhi_wm2,
                    dni_wm2,
                    clear_sky_ghi_wm2,
                )
            hour = hour_dt.hour
            if self.panel_group_calculator and self.panel_group_calculator.has_groups:
                effective_ghi_wm2, effective_dni_wm2, effective_dhi_wm2 = (
                    poa_irradiance_components(ghi_wm2, dni_wm2, dhi_wm2, sun_elevation)
                )
                irradiance = IrradianceData(
                    ghi=effective_ghi_wm2, dni=effective_dni_wm2, dhi=effective_dhi_wm2
                )
                sun_position = SunPosition(
                    elevation_deg=sun_elevation, azimuth_deg=sun_azimuth
                )
                weather_ghi_ratio = (
                    min(ghi_wm2 / clear_sky_ghi_wm2, 1.05)
                    if clear_sky_ghi_wm2 > 50 and ghi_wm2 > 0
                    else None
                )
                production_result = self.panel_group_calculator.calculate_total_power(
                    irradiance=irradiance,
                    sun=sun_position,
                    ambient_temp_c=temperature_c,
                    hour=hour,
                    cloud_cover=cloud_cover,
                    visibility_m=visibility_m,
                    precipitation_mm=precipitation_mm,
                    temperature_c=temperature_c,
                    snow_covered_panels=snow_covered_panels,
                    frost_detected=frost_detected,
                    frost_score=frost_score,
                    snow_per_group=snow_per_group,
                    ghi_ratio=weather_ghi_ratio,
                )
                group_results = []
                for group_result in production_result.group_results:
                    group_results.append(
                        {
                            "name": group_result.group.name,
                            "power_kwh": round(group_result.power_kwh, 4),
                            "physics_kwh": round(group_result.power_kwh, 4),
                            "raw_physics_kwh": round(group_result.raw_power_kwh, 4),
                            "calibration_factor": round(
                                group_result.calibration_factor, 4
                            ),
                            "contribution_percent": round(
                                group_result.contribution_percent, 1
                            ),
                            "poa_wm2": round(group_result.poa_result.poa_total, 1),
                            "aoi_deg": round(group_result.poa_result.aoi_deg, 1),
                        }
                    )
                return (production_result.total_power_kwh, group_results)
            efficiency = self._calculate_efficiency(cloud_cover, temperature_c)
            fallback_production_kwh = (
                ghi_wm2 / 1000.0 * self.solar_capacity * efficiency
            )
            return (
                max(0.0, min(fallback_production_kwh, self.solar_capacity * 1.0)),
                None,
            )
        except Exception as error:
            _LOGGER.debug(f"Physics production calc failed: {error}")
            return (0.0, None)

    def _get_astronomy_for_hour(self, hour_dt: datetime) -> Dict[str, Any]:
        """Get astronomy data for specific hour with enrichment. @zara V16.0.0"""
        try:
            astronomy_cache_manager = get_cache_manager()
            if not astronomy_cache_manager or not astronomy_cache_manager.is_loaded():
                return self._default_astronomy(hour_dt.hour)
            day_astronomy = astronomy_cache_manager.get_day_data(hour_dt.date())
            if not day_astronomy:
                return self._default_astronomy(hour_dt.hour)
            hourly_astronomy = day_astronomy.get("hourly", {})
            hour_key = str(hour_dt.hour)
            hour_astronomy = hourly_astronomy.get(hour_key, {})
            astronomy_data = {
                "sun_elevation_deg": hour_astronomy.get("elevation_deg", 30),
                "sun_azimuth_deg": hour_astronomy.get("azimuth_deg", 180),
                "theoretical_max_kwh": hour_astronomy.get(
                    "theoretical_max_pv_kwh", 0.5
                ),
                "clear_sky_radiation_wm2": hour_astronomy.get(
                    "clear_sky_solar_radiation_wm2", 500
                ),
                "max_elevation_today": day_astronomy.get("max_elevation_deg", 60),
                "sunset": None,
                "hour_start": hour_dt.replace(minute=0, second=0, microsecond=0),
            }
            daylight_hours = day_astronomy.get(
                "daylight_hours", day_astronomy.get("day_length_hours", 12.0)
            )
            astronomy_data["daylight_hours"] = daylight_hours
            sunrise_value = day_astronomy.get(
                "sunrise_local", day_astronomy.get("sunrise_time")
            )
            sunset_value = day_astronomy.get(
                "sunset_local", day_astronomy.get("sunset_time")
            )
            solar_noon_value = day_astronomy.get(
                "solar_noon_local", day_astronomy.get("solar_noon")
            )
            hour_midpoint = hour_dt.replace(minute=30, second=0, microsecond=0)
            hour = hour_dt.hour
            day_progress = max(0.0, min(1.0, (hour - 6) / 12.0))
            hours_since_sunrise = max(0.0, hour - 6.0)
            hours_until_sunset = max(0.0, 18.0 - hour)
            hours_from_noon = float(hour - 12)
            if sunrise_value and sunset_value:
                try:
                    sunrise = self._parse_time_to_datetime(sunrise_value, hour_dt)
                    sunset = self._parse_time_to_datetime(sunset_value, hour_dt)
                    astronomy_data["sunset"] = sunset
                    daylight_seconds = (sunset - sunrise).total_seconds()
                    elapsed_daylight_seconds = (hour_midpoint - sunrise).total_seconds()
                    if daylight_seconds > 0:
                        day_progress = max(
                            0.0, min(1.0, elapsed_daylight_seconds / daylight_seconds)
                        )
                    hours_since_sunrise = max(0.0, elapsed_daylight_seconds / 3600.0)
                    hours_until_sunset = max(
                        0.0, (sunset - hour_midpoint).total_seconds() / 3600.0
                    )
                except Exception:
                    pass
            if solar_noon_value:
                try:
                    solar_noon = self._parse_time_to_datetime(solar_noon_value, hour_dt)
                    hours_from_noon = (
                        hour_midpoint - solar_noon
                    ).total_seconds() / 3600.0
                except Exception:
                    pass
            astronomy_data["day_progress_ratio"] = day_progress
            astronomy_data["hours_after_sunrise"] = hours_since_sunrise
            astronomy_data["hours_before_sunset"] = hours_until_sunset
            astronomy_data["hours_since_solar_noon"] = hours_from_noon
            return astronomy_data
        except Exception:
            return self._default_astronomy(hour_dt.hour)

    @staticmethod
    def _parse_time_to_datetime(time_str: Any, reference_dt: datetime) -> datetime:
        """Parse time string/timestamp to datetime on reference date. @zara V16.0.0"""
        if isinstance(time_str, datetime):
            return time_str.replace(
                year=reference_dt.year, month=reference_dt.month, day=reference_dt.day
            )
        time_text = str(time_str).strip()
        if "T" in time_text or len(time_text) > 10:
            try:
                parsed_datetime = datetime.fromisoformat(
                    time_text.replace("Z", "+00:00")
                )
                return parsed_datetime.replace(
                    year=reference_dt.year,
                    month=reference_dt.month,
                    day=reference_dt.day,
                )
            except (ValueError, TypeError):
                pass
        for time_format in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(time_text, time_format).time()
                return reference_dt.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute,
                    second=parsed_time.second,
                    microsecond=0,
                )
            except (ValueError, TypeError):
                continue
        raise ValueError(f"Cannot parse time: {time_str}")

    def _default_astronomy(self, hour: int) -> Dict[str, Any]:
        """Default astronomy values with enrichment. @zara V16.0.0"""
        sun_elevation = max(0, 45 - abs(hour - 12) * 7)
        sun_azimuth = 90 + (hour - 6) * 15
        sun_azimuth = max(90, min(270, sun_azimuth))
        return {
            "sun_elevation_deg": sun_elevation,
            "sun_azimuth_deg": sun_azimuth,
            "theoretical_max_kwh": 0.5 if 6 <= hour <= 18 else 0.0,
            "clear_sky_radiation_wm2": max(0, 500 - abs(hour - 12) * 50),
            "max_elevation_today": 60,
            "daylight_hours": 12.0,
            "day_progress_ratio": max(0.0, min(1.0, (hour - 6) / 12.0)),
            "hours_after_sunrise": max(0.0, hour - 6.0),
            "hours_before_sunset": max(0.0, 18.0 - hour),
            "hours_since_solar_noon": float(hour - 12),
        }

    def _correct_cloud_cover_from_ghi(
        self, clouds: float, raw_ghi: float, clear_sky: float, sun_elevation: float
    ) -> float:
        """Correct cloud_cover when GHI contradicts reported clouds. @zara"""
        if clear_sky < 50 or raw_ghi <= 0 or sun_elevation <= 5:
            return clouds
        ghi_ratio = min(raw_ghi / clear_sky, 1.05)
        ghi_cloud_cover = max(0.0, (1.0 - ghi_ratio) * 100.0)
        if clouds > ghi_cloud_cover + 30:
            corrected_cloud_cover = round(clouds * 0.2 + ghi_cloud_cover * 0.8, 1)
            _LOGGER.log(
                5,
                "Cloud plausibility: API=%.0f%%, GHI-implied=%.0f%%, corrected=%.0f%%",
                clouds,
                ghi_cloud_cover,
                corrected_cloud_cover,
            )
            return corrected_cloud_cover
        return clouds

    def _calculate_efficiency(self, clouds: float, temp: float) -> float:
        """Calculate efficiency based on conditions. @zara"""
        cloud_factor = 1.0 - clouds / 100.0 * 0.75
        cloud_factor = max(0.1, min(1.0, cloud_factor))
        temperature_factor = 1.0
        if temp > 25:
            temperature_factor = 1.0 - (temp - 25) * 0.004
        elif temp < 10:
            temperature_factor = 1.0 + (10 - temp) * 0.002
        temperature_factor = max(0.85, min(1.05, temperature_factor))
        return cloud_factor * temperature_factor * 0.92

    def _estimate_twilight_radiation(
        self, sun_elevation: float, cloud_cover: float
    ) -> Tuple[float, float]:
        """Estimate minimum radiation during twilight hours. @zara"""
        twilight_elevation_span = TWILIGHT_MAX_ELEVATION - TWILIGHT_MIN_ELEVATION
        twilight_progress = (
            sun_elevation - TWILIGHT_MIN_ELEVATION
        ) / twilight_elevation_span
        twilight_progress = max(0.0, min(1.0, twilight_progress))
        cloud_factor = 1.0 - cloud_cover / 100.0 * 0.15
        twilight_dhi_wm2 = (
            TWILIGHT_MIN_DHI_WM2 * (0.3 + 0.7 * twilight_progress) * cloud_factor
        )
        twilight_ghi_wm2 = twilight_dhi_wm2 * 1.1
        return (max(TWILIGHT_MIN_GHI_WM2, twilight_ghi_wm2), max(3.0, twilight_dhi_wm2))

    def _correct_low_sun_radiation(
        self,
        sun_elevation: float,
        cloud_cover: float,
        api_ghi: float,
        api_dhi: float,
        api_dni: float,
        clear_sky_ghi: float,
    ) -> Tuple[float, float, float]:
        """Correct radiation estimates for low sun angles with clear/fair sky. @zara"""
        if cloud_cover >= LOW_SUN_CLOUD_THRESHOLD:
            return (api_ghi, api_dhi, api_dni)
        learned_factor_available = self._has_learned_low_sun_factor(
            sun_elevation, cloud_cover
        )
        if learned_factor_available:
            learned_calibration_factor = self._get_low_sun_calibration_factor(
                sun_elevation, cloud_cover
            )
            return (
                api_ghi * learned_calibration_factor,
                api_dhi * learned_calibration_factor,
                api_dni * learned_calibration_factor,
            )
        low_sun_progress = (sun_elevation - TWILIGHT_MAX_ELEVATION) / (
            LOW_SUN_MAX_ELEVATION - TWILIGHT_MAX_ELEVATION
        )
        low_sun_progress = max(0.0, min(1.0, low_sun_progress))
        low_sun_boost = 1.0 + 1.5 * (1.0 - low_sun_progress) ** 2
        clear_sky_weight = 1.0 - cloud_cover / LOW_SUN_CLOUD_THRESHOLD
        clear_sky_weight = max(0.0, clear_sky_weight)
        correction_multiplier = 1.0 + (low_sun_boost - 1.0) * clear_sky_weight
        corrected_ghi_wm2 = api_ghi * correction_multiplier
        if clear_sky_ghi > 0:
            expected_ghi_wm2 = clear_sky_ghi * (1.0 - cloud_cover / 100.0 * 0.7)
            corrected_ghi_wm2 = min(corrected_ghi_wm2, max(api_ghi, expected_ghi_wm2))
        diffuse_fraction = 0.85 - (sun_elevation - 5) * 0.015
        diffuse_fraction = max(0.7, min(0.85, diffuse_fraction))
        corrected_dhi_wm2 = max(api_dhi, corrected_ghi_wm2 * diffuse_fraction)
        corrected_dni_wm2 = min(api_dni, corrected_ghi_wm2 * (1.0 - diffuse_fraction))
        return (corrected_ghi_wm2, corrected_dhi_wm2, corrected_dni_wm2)

    def _has_learned_low_sun_factor(
        self, sun_elevation: float, cloud_cover: float
    ) -> bool:
        """Check if calibrator has learned LOW_SUN bucket factors. @zara"""
        if (
            not self.panel_group_calculator
            or not self.panel_group_calculator._calibrator
        ):
            return False
        calibrator = self.panel_group_calculator._calibrator
        if cloud_cover <= 25:
            low_sun_bucket = "clear_low_sun"
        elif cloud_cover <= 50:
            low_sun_bucket = "fair_low_sun"
        else:
            return True
        for group_key, group_calibration in calibrator._factors.items():
            bucket_factor = group_calibration.bucket_factors.get(low_sun_bucket)
            if not bucket_factor:
                continue
            if not bucket_factor.sample_count >= 1:
                continue
            return True
        return False

    def _get_low_sun_calibration_factor(
        self, sun_elevation: float, cloud_cover: float
    ) -> float:
        """Get learned calibration factor for low sun bucket. @zara V16.4.0"""
        if (
            not self.panel_group_calculator
            or not self.panel_group_calculator._calibrator
        ):
            return 1.0
        calibrator = self.panel_group_calculator._calibrator
        if cloud_cover <= 25:
            low_sun_bucket = "clear_low_sun"
        elif cloud_cover <= 50:
            low_sun_bucket = "fair_low_sun"
        else:
            return 1.0
        candidate_factors = []
        for group_key, group_calibration in calibrator._factors.items():
            bucket_factor = group_calibration.bucket_factors.get(low_sun_bucket)
            if bucket_factor and bucket_factor.sample_count >= 1:
                candidate_factors.append(bucket_factor.global_factor)
        if candidate_factors:
            return sum(candidate_factors) / len(candidate_factors)
        return 1.0

    def _calculate_hourly_ai_confidence(
        self, base_r2: float, clouds: float, sun_elevation: float, hour: int
    ) -> float:
        """Calculate dynamic AI confidence for specific hour. @zara"""
        if base_r2 <= 0:
            return 0.0
        if base_r2 < 0.5:
            r2_confidence = base_r2
        elif base_r2 < 0.8:
            r2_confidence = 0.5 + (base_r2 - 0.5) * 1.333
        else:
            r2_confidence = 0.9 + min(base_r2 - 0.8, 0.2) * 0.5
        if clouds < 30:
            cloud_factor = 1.0
        elif clouds < 70:
            cloud_factor = 0.9
        else:
            cloud_factor = 0.7
        if sun_elevation < 10:
            elevation_factor = 0.7
        elif sun_elevation < 30:
            elevation_factor = 0.85
        else:
            elevation_factor = 1.0
        if 10 <= hour <= 14:
            hour_factor = 1.0
        else:
            hour_factor = 0.85
        return r2_confidence * cloud_factor * elevation_factor * hour_factor

    def _calculate_bootstrap_confidence(
        self, base_r2: float, n_samples: int, cloud_cover: float, sun_elevation: float
    ) -> float:
        """Calculate confidence for Ridge in bootstrap phase (10-50 samples). @zara"""
        sample_progress = (n_samples - 10) / 40
        bootstrap_base_confidence = 0.15 + sample_progress * 0.35
        r2_factor = min(1.0, base_r2 / 0.6)
        cloud_factor = 1.0 if cloud_cover < 50 else 0.8
        elevation_factor = 1.0 if sun_elevation > 15 else 0.7
        bootstrap_confidence = (
            bootstrap_base_confidence * r2_factor * cloud_factor * elevation_factor
        )
        return min(0.5, max(0.0, bootstrap_confidence))

    def _calculate_learned_ai_confidence_floor(
        self, learned: Dict[str, Any], blend_regime: str = BLEND_REGIME_MIXED
    ) -> float:
        """Calculate the minimum AI blend through the static blend policy."""
        return StaticForecastBlendPolicy.calculate_ai_floor(learned, blend_regime)

    async def _get_learned_confidence_adjustment(
        self, clouds: float, hour: int
    ) -> Dict[str, Any]:
        """Learned confidence adjustment with MAE data. @zara V16.0.0

        Returns dict with advantage_factor, physics_mae, ai_mae, blend_mae, sample_count.
        """
        cloud_bucket = (
            "clear" if clouds < 30 else "partly_cloudy" if clouds < 70 else "overcast"
        )
        hour_bucket = "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
        season = get_season(self._policy_now().month)
        cache_key = ("method_performance", cloud_bucket, hour_bucket, season)
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            return dict(self._forecast_policy_read_cache[cache_key])
        learned_adjustment = {
            "advantage_factor": 1.0,
            "physics_mae": None,
            "ai_mae": None,
            "blend_mae": None,
            "sample_count": 0,
        }
        try:
            database_row = await self._db.fetchone(
                "SELECT ai_advantage_factor, sample_count, physics_mae, ai_mae, blend_mae\n                   FROM method_performance_learning\n                   WHERE cloud_bucket = ? AND hour_bucket = ? AND season = ?",
                (cloud_bucket, hour_bucket, season),
            )
            if not database_row or not database_row[1] or database_row[1] < 5:
                database_row = await self._db.fetchone(
                    "SELECT ai_advantage_factor, sample_count, physics_mae, ai_mae, blend_mae\n                       FROM method_performance_learning\n                       WHERE cloud_bucket = ? AND hour_bucket = ? AND season IS NULL",
                    (cloud_bucket, hour_bucket),
                )
            if database_row and database_row[1] and (database_row[1] >= 5):
                learned_adjustment["advantage_factor"] = database_row[0]
                learned_adjustment["sample_count"] = database_row[1]
                learned_adjustment["physics_mae"] = database_row[2]
                learned_adjustment["ai_mae"] = database_row[3]
                learned_adjustment["blend_mae"] = (
                    database_row[4] if len(database_row) > 4 else None
                )
        except Exception:
            pass
        if self._forecast_policy_read_cache is not None:
            self._forecast_policy_read_cache[cache_key] = dict(learned_adjustment)
        return learned_adjustment

    async def _get_weather_regime_learning_adjustment(
        self, weather_regime: str, hour: int
    ) -> Dict[str, Any]:
        """Load learned performance for the exact static weather regime."""
        hour_bucket = "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
        season = get_season(self._policy_now().month)
        cache_key = ("weather_regime_performance", weather_regime, hour_bucket, season)
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            return dict(self._forecast_policy_read_cache[cache_key])
        regime_adjustment = {
            "weather_regime": weather_regime,
            "physics_mae": None,
            "ai_mae": None,
            "tfs_mae": None,
            "blend_mae": None,
            "physics_bias": None,
            "ai_bias": None,
            "tfs_bias": None,
            "blend_bias": None,
            "best_method": None,
            "recommended_tfs_weight": None,
            "ai_advantage_factor": 1.0,
            "tfs_advantage_factor": 1.0,
            "sample_count": 0,
            "learning_strength": 0.0,
        }
        try:
            database_row = await self._db.fetchone(
                "SELECT physics_mae, ai_mae, tfs_mae, blend_mae,\n                          physics_bias, ai_bias, tfs_bias, blend_bias,\n                          best_method, recommended_tfs_weight,\n                          ai_advantage_factor, tfs_advantage_factor, sample_count\n                   FROM weather_regime_performance_learning\n                   WHERE weather_regime = ? AND hour_bucket = ? AND season = ?",
                (weather_regime, hour_bucket, season),
            )
            if (
                not database_row
                or not database_row[12]
                or database_row[12] < REGIME_METHOD_SOFT_MIN_SAMPLES
            ):
                database_row = await self._db.fetchone(
                    "SELECT physics_mae, ai_mae, tfs_mae, blend_mae,\n                              physics_bias, ai_bias, tfs_bias, blend_bias,\n                              best_method, recommended_tfs_weight,\n                              ai_advantage_factor, tfs_advantage_factor, sample_count\n                       FROM weather_regime_performance_learning\n                       WHERE weather_regime = ? AND hour_bucket = ? AND season IS NULL",
                    (weather_regime, hour_bucket),
                )
            if (
                database_row
                and database_row[12]
                and (database_row[12] >= REGIME_METHOD_SOFT_MIN_SAMPLES)
            ):
                learning_strength = _learning_strength(
                    int(database_row[12]),
                    soft_min=REGIME_METHOD_SOFT_MIN_SAMPLES,
                    full_samples=REGIME_METHOD_ACTIVE_MIN_SAMPLES,
                )
                regime_adjustment.update(
                    {
                        "physics_mae": database_row[0],
                        "ai_mae": database_row[1],
                        "tfs_mae": database_row[2],
                        "blend_mae": database_row[3],
                        "physics_bias": database_row[4],
                        "ai_bias": database_row[5],
                        "tfs_bias": database_row[6],
                        "blend_bias": database_row[7],
                        "best_method": database_row[8],
                        "recommended_tfs_weight": database_row[9],
                        "ai_advantage_factor": database_row[10] or 1.0,
                        "tfs_advantage_factor": database_row[11] or 1.0,
                        "sample_count": database_row[12],
                        "learning_strength": round(learning_strength, 4),
                    }
                )
        except Exception:
            pass
        if self._forecast_policy_read_cache is not None:
            self._forecast_policy_read_cache[cache_key] = dict(regime_adjustment)
        return regime_adjustment

    @staticmethod
    def _daily_regime_profile_from_hours(hours: List[Dict[str, Any]]) -> Dict[str, Any]:
        weather_regime_weights = {}
        blend_regime_weights = {}
        hour_bucket_weights = {"morning": 0.0, "midday": 0.0, "afternoon": 0.0}
        profile_weight_sum = 0.0
        for hour_entry in hours:
            profile_weight = max(
                0.01,
                float(
                    hour_entry.get("prediction_kwh")
                    or hour_entry.get("production_kwh")
                    or 0.0
                ),
                float(hour_entry.get("ai_kwh") or 0.0) * 0.5,
                float(hour_entry.get("tfs_kwh") or 0.0) * 0.5,
                float(hour_entry.get("physics_kwh") or 0.0) * 0.5,
            )
            regime = hour_entry.get("weather_regime") or WEATHER_REGIME_MIXED_UNCERTAIN
            blend_regime = hour_entry.get("blend_regime") or BLEND_REGIME_MIXED
            weather_regime_weights[regime] = (
                weather_regime_weights.get(regime, 0.0) + profile_weight
            )
            blend_regime_weights[blend_regime] = (
                blend_regime_weights.get(blend_regime, 0.0) + profile_weight
            )
            hour_bucket = _hour_bucket(int(hour_entry.get("hour") or 0))
            hour_bucket_weights[hour_bucket] = (
                hour_bucket_weights.get(hour_bucket, 0.0) + profile_weight
            )
            profile_weight_sum += profile_weight
            continue
        if profile_weight_sum <= 0:
            profile_weight_sum = 1.0
        dominant_weather_regime = (
            max(weather_regime_weights, key=weather_regime_weights.get)
            if weather_regime_weights
            else WEATHER_REGIME_MIXED_UNCERTAIN
        )
        dominant_blend_regime = (
            max(blend_regime_weights, key=blend_regime_weights.get)
            if blend_regime_weights
            else BLEND_REGIME_MIXED
        )
        return {
            "primary_regime": dominant_weather_regime,
            "primary_blend": dominant_blend_regime,
            "bad_weather_share": blend_regime_weights.get(BLEND_REGIME_BAD_WEATHER, 0.0)
            / profile_weight_sum,
            "rain_share": weather_regime_weights.get(WEATHER_REGIME_RAIN_OVERCAST, 0.0)
            / profile_weight_sum,
            "low_ghi_share": weather_regime_weights.get(
                WEATHER_REGIME_LOW_GHI_OVERCAST, 0.0
            )
            / profile_weight_sum,
            "diffuse_share": weather_regime_weights.get(
                WEATHER_REGIME_DIFFUSE_OVERCAST, 0.0
            )
            / profile_weight_sum,
            "morning_share": hour_bucket_weights["morning"] / profile_weight_sum,
            "midday_share": hour_bucket_weights["midday"] / profile_weight_sum,
            "afternoon_share": hour_bucket_weights["afternoon"] / profile_weight_sum,
        }

    async def _get_daily_bias_history(self, target_date: str) -> List[Dict[str, Any]]:
        """Load final Morning daily forecast factors from immutable OPS snapshots."""
        if target_date in self._daily_bias_history_cache:
            return self._daily_bias_history_cache[target_date]
        history = []
        try:
            history_start_date = daily_bias_history_start_date(target_date)
            database_rows = await self._db.fetchall(
                "SELECT od.forecast_date,\n                          oh.target_hour,\n                          oh.prediction_kwh,\n                          oh.physics_kwh,\n                          oh.ai_kwh,\n                          oh.tfs_kwh,\n                          hp.actual_kwh,\n                          ow.clouds,\n                          ow.rain,\n                          ow.humidity,\n                          ow.solar_radiation_wm2,\n                          ow.diffuse_radiation,\n                          ow.direct_radiation,\n                          ac.clear_sky_radiation_wm2,\n                          ac.sun_elevation_deg,\n                          hp.exclude_from_learning,\n                          hp.data_quarantine_reason,\n                          hp.mppt_throttle_reason\n                   FROM ops_daily_forecasts od\n                   JOIN ops_hourly_forecasts oh\n                       ON oh.snapshot_sequence = od.snapshot_sequence\n                       AND oh.target_date = od.forecast_date\n                   LEFT JOIN ops_prediction_weather ow\n                       ON ow.forecast_row_id = oh.forecast_row_id\n                   LEFT JOIN hourly_predictions hp\n                       ON hp.prediction_id = oh.prediction_id\n                   LEFT JOIN astronomy_cache ac\n                       ON ac.cache_date = oh.target_date AND ac.hour = oh.target_hour\n                   WHERE od.forecast_type = 'today'\n                     AND od.snapshot_type = 'morning_snapshot'\n                     AND od.forecast_date >= ?\n                     AND od.forecast_date < ?\n                     AND od.snapshot_sequence = (\n                         SELECT MAX(od2.snapshot_sequence)\n                         FROM ops_daily_forecasts od2\n                         WHERE od2.forecast_type = 'today'\n                           AND od2.snapshot_type = 'morning_snapshot'\n                           AND od2.forecast_date = od.forecast_date\n                     )\n                     AND oh.prediction_kwh IS NOT NULL\n                     AND hp.actual_kwh IS NOT NULL\n                     AND ow.clouds IS NOT NULL\n                     AND ow.solar_radiation_wm2 IS NOT NULL\n                     AND ac.clear_sky_radiation_wm2 IS NOT NULL\n                     AND ac.sun_elevation_deg IS NOT NULL\n                   ORDER BY od.forecast_date, oh.target_hour",
                (history_start_date, target_date),
            )
            hours_by_date = {}
            for database_row in database_rows:
                forecast_date = str(database_row[0])
                hour = int(database_row[1])
                prediction_kwh = max(0.0, float(database_row[2] or 0.0))
                actual_kwh = max(0.0, float(database_row[6] or 0.0))
                cloud_cover = float(database_row[7] or 50.0)
                rain_mm = float(database_row[8] or 0.0)
                humidity_percent = float(database_row[9] or 70.0)
                ghi_wm2 = float(database_row[10] or 0.0)
                dhi_wm2 = float(database_row[11] or 0.0)
                dni_wm2 = float(database_row[12] or 0.0)
                clear_sky_ghi_wm2 = float(database_row[13] or 0.0)
                sun_elevation = float(database_row[14] or 0.0)
                weather_regime = _classify_static_forecast_weather_regime(
                    clouds=cloud_cover,
                    ghi=ghi_wm2,
                    dni=dni_wm2,
                    dhi=dhi_wm2,
                    clear_sky=clear_sky_ghi_wm2,
                    rain=rain_mm,
                    humidity=humidity_percent,
                    sun_elevation=sun_elevation,
                    hour=hour,
                )
                blend_regime = _classify_blend_weather_regime(
                    clouds=cloud_cover,
                    ghi=ghi_wm2,
                    dni=dni_wm2,
                    dhi=dhi_wm2,
                    clear_sky=clear_sky_ghi_wm2,
                    rain=rain_mm,
                    humidity=humidity_percent,
                    sun_elevation=sun_elevation,
                    hour=hour,
                )
                hours_by_date.setdefault(forecast_date, []).append(
                    {
                        "hour": hour,
                        "prediction_kwh": prediction_kwh,
                        "physics_kwh": max(0.0, float(database_row[3] or 0.0)),
                        "ai_kwh": max(0.0, float(database_row[4] or 0.0)),
                        "tfs_kwh": max(0.0, float(database_row[5] or 0.0)),
                        "actual_kwh": actual_kwh,
                        "weather_regime": weather_regime,
                        "blend_regime": blend_regime,
                        "exclude_from_learning": bool(database_row[15]),
                        "data_quarantine_reason": database_row[16],
                        "mppt_throttle_reason": database_row[17],
                    }
                )
                continue
            for forecast_date, historical_hours in hours_by_date.items():
                if daily_bias_day_should_drop(historical_hours):
                    continue
                predicted_total_kwh = sum(
                    (
                        float(hour.get("prediction_kwh") or 0.0)
                        for hour in historical_hours
                    )
                )
                actual_total_kwh = sum(
                    (float(hour.get("actual_kwh") or 0.0) for hour in historical_hours)
                )
                if predicted_total_kwh <= 0.05 or actual_total_kwh <= 0:
                    continue
                regime_profile = self._daily_regime_profile_from_hours(historical_hours)
                history.append(
                    {
                        "forecast_date": forecast_date,
                        "season": get_season(int(forecast_date[5:7])),
                        "factor": actual_total_kwh / predicted_total_kwh,
                        "prediction_total": predicted_total_kwh,
                        "actual_total": actual_total_kwh,
                        "hourly_factors": daily_bias_hourly_lock_factors(
                            historical_hours, clean_only=True
                        ),
                        **regime_profile,
                    }
                )
        except Exception as error:
            _LOGGER.debug(
                "Daily bias history unavailable for %s: %s", target_date, error
            )
            return history
        self._daily_bias_history_cache[target_date] = history
        return history

    @staticmethod
    def _daily_bias_similarity(
        current: Dict[str, Any], historic: Dict[str, Any]
    ) -> float:
        return daily_bias_similarity_weight(current, historic)

    async def _get_final_morning_daily_bias_signal(
        self, target_date: str, day_entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        current_profile = self._daily_regime_profile_from_hours(day_entries)
        current_profile["season"] = get_season(int(target_date[5:7]))
        history = await self._get_daily_bias_history(target_date)
        weighted_history = []
        for historic_sample in history:
            weight = self._daily_bias_similarity(current_profile, historic_sample)
            if weight > DAILY_BIAS_MIN_SIMILARITY_WEIGHT:
                weighted_history.append((historic_sample, weight))
        weighted_history.sort(
            key=lambda item: (
                str(item[0].get("forecast_date") or item[0].get("date") or ""),
                -item[1],
            )
        )
        sample_count = len(weighted_history)
        weight_sum = sum((weight for unused_sample_part, weight in weighted_history))
        result = {
            "factor": 1.0,
            "reason": None,
            "sample_count": sample_count,
            "weight_sum": round(weight_sum, 4),
        }
        if sample_count < DAILY_BIAS_MIN_DAYS or weight_sum <= 0:
            self._cycle_log()(
                "Daily overforecast bias skipped: date=%s, reason=insufficient_similar_days, samples=%s, weight_sum=%s, history=%s",
                target_date,
                sample_count,
                round(weight_sum, 4),
                len(history),
            )
            return result
        weights = [weight for unused_sample_part, weight in weighted_history]
        daily_factors = [
            float(historic_sample["factor"])
            for historic_sample, unused_sample_part in weighted_history
        ]
        median_factor = _weighted_quantile(daily_factors, weights, 0.5) or 1.0
        factor_q25 = _weighted_quantile(daily_factors, weights, 0.25) or 1.0
        factor_q75 = _weighted_quantile(daily_factors, weights, 0.75) or 1.0
        underforecast_share = (
            sum(
                (
                    weight
                    for historic_sample, weight in weighted_history
                    if float(historic_sample["factor"]) > 1.1
                )
            )
            / weight_sum
        )
        overforecast_share = (
            sum(
                (
                    weight
                    for historic_sample, weight in weighted_history
                    if float(historic_sample["factor"]) < 0.9
                )
            )
            / weight_sum
        )
        result.update(
            {
                "factor_median": round(median_factor, 4),
                "factor_q25": round(factor_q25, 4),
                "factor_q75": round(factor_q75, 4),
                "underforecast_share": round(underforecast_share, 4),
                "overforecast_share": round(overforecast_share, 4),
            }
        )
        if (
            underforecast_share >= DAILY_BIAS_MIN_UNDERFORECAST_SHARE
            and factor_q25 >= DAILY_BIAS_MIN_FACTOR_Q25
            and (median_factor > 1.12)
        ):
            current_total_kwh = sum(
                (float(entry.get("production_kwh") or 0.0) for entry in day_entries)
            )
            maximum_factor = (
                DAILY_BIAS_MAX_UP_FACTOR_STRONG
                if weight_sum >= 3.0
                else DAILY_BIAS_MAX_UP_FACTOR_THIN
            )
            daily_factor = min(maximum_factor, max(factor_q25, median_factor * 0.92))
            recent_up_bias_guard = await self._get_recent_daily_up_bias_guard_signal(
                target_date=target_date, current_total=current_total_kwh
            )
            if recent_up_bias_guard.get("veto"):
                result.update(
                    {
                        "recent_guard_reason": recent_up_bias_guard.get("reason"),
                        "recent_guard_similar_days": recent_up_bias_guard.get(
                            "similar_days"
                        ),
                        "recent_guard_harmful_days": recent_up_bias_guard.get(
                            "harmful_days"
                        ),
                        "recent_guard_median_base_ratio": recent_up_bias_guard.get(
                            "median_base_ratio"
                        ),
                        "recent_guard_median_uplift": recent_up_bias_guard.get(
                            "median_uplift"
                        ),
                    }
                )
                _LOGGER.debug(
                    "Final Morning daily up-bias vetoed: date=%s, reason=%s, candidate_factor=%.3f, similar_days=%s, harmful_days=%s, median_base_ratio=%s, median_uplift=%s",
                    target_date,
                    recent_up_bias_guard.get("reason"),
                    daily_factor,
                    recent_up_bias_guard.get("similar_days"),
                    recent_up_bias_guard.get("harmful_days"),
                    recent_up_bias_guard.get("median_base_ratio"),
                    recent_up_bias_guard.get("median_uplift"),
                )
                return result
            result.update(
                {
                    "factor": round(max(1.0, daily_factor), 4),
                    "reason": "final_morning_daily_underforecast_bias",
                }
            )
            return result
        samples_by_hour = {}
        for historic_sample, weight in weighted_history:
            historic_hour_factors = historic_sample.get("hourly_factors") or {}
            for hour, historic_factor in historic_hour_factors.items():
                samples_by_hour.setdefault(int(hour), []).append(
                    (float(historic_factor), weight)
                )
        hourly_factors = resolve_hourly_overforecast_bias_factors(samples_by_hour)
        if hourly_factors:
            result.update(
                {
                    "factor": 1.0,
                    "hour_factors": hourly_factors,
                    "reason": "final_morning_hourly_overforecast_bias",
                    "hourly_scaled": len(hourly_factors),
                }
            )
            return result
        self._cycle_log()(
            "Daily overforecast bias skipped: date=%s, reason=gates_not_met, samples=%s, weight_sum=%s, overforecast_share=%.3f, underforecast_share=%.3f, median=%.3f, q25=%.3f, q75=%.3f",
            target_date,
            sample_count,
            round(weight_sum, 4),
            overforecast_share,
            underforecast_share,
            median_factor,
            factor_q25,
            factor_q75,
        )
        return result

    async def _get_recent_daily_up_bias_guard_signal(
        self, *, target_date: str, current_total: float
    ) -> Dict[str, Any]:
        if current_total <= 0.05:
            return {"veto": False, "reason": None}
        cache_key = ("daily_up_bias_rows", target_date)
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            database_rows = self._forecast_policy_read_cache[cache_key]
        else:
            try:
                database_rows = await self._db.fetchall(
                    "SELECT hp.target_date,\n                              SUM(hp.prediction_kwh) AS final_total,\n                              SUM(hp.prediction_kwh_uncapped) AS base_total,\n                              ds.actual_total_kwh\n                       FROM hourly_predictions hp\n                       JOIN daily_summaries ds ON ds.date = hp.target_date\n                       WHERE hp.target_date < ?\n                         AND hp.target_date >= date(?, ?)\n                         AND hp.prediction_kwh_uncapped IS NOT NULL\n                       GROUP BY hp.target_date\n                       HAVING base_total > 0.05\n                          AND ds.actual_total_kwh IS NOT NULL\n                          AND ds.actual_total_kwh > 0\n                       ORDER BY hp.target_date DESC",
                    (
                        target_date,
                        target_date,
                        f"-{DAILY_BIAS_RECENT_GUARD_LOOKBACK_DAYS} day",
                    ),
                )
                if self._forecast_policy_read_cache is not None:
                    self._forecast_policy_read_cache[cache_key] = database_rows
            except Exception as error:
                _LOGGER.debug(
                    "Daily up-bias recent guard unavailable for %s: %s",
                    target_date,
                    error,
                )
                return {"veto": False, "reason": None}
        recent_days = []
        for database_row in database_rows:
            try:
                final_total_kwh = max(0.0, float(database_row[1] or 0.0))
                base_total_kwh = max(0.0, float(database_row[2] or 0.0))
                actual_total_kwh = max(0.0, float(database_row[3] or 0.0))
            except (TypeError, ValueError):
                continue
            if base_total_kwh <= 0.05 or actual_total_kwh <= 0:
                continue
            base_ratio = base_total_kwh / current_total
            if (
                not DAILY_BIAS_RECENT_GUARD_MIN_BASE_RATIO
                <= base_ratio
                <= DAILY_BIAS_RECENT_GUARD_MAX_BASE_RATIO
            ):
                continue
            recent_days.append(
                {
                    "final_total": final_total_kwh,
                    "base_total": base_total_kwh,
                    "actual_total": actual_total_kwh,
                    "uplift": (
                        final_total_kwh / base_total_kwh if base_total_kwh > 0 else 1.0
                    ),
                    "base_ratio": (
                        actual_total_kwh / base_total_kwh if base_total_kwh > 0 else 1.0
                    ),
                    "final_error": abs(final_total_kwh - actual_total_kwh),
                    "base_error": abs(base_total_kwh - actual_total_kwh),
                }
            )
        if len(recent_days) < DAILY_BIAS_RECENT_GUARD_MIN_SIMILAR_DAYS:
            return {"veto": False, "reason": None, "similar_days": len(recent_days)}
        degraded_days = []
        overforecast_days = []
        severely_degraded_days = []
        for historic_record in recent_days:
            error_degradation_kwh = (
                historic_record["final_error"] - historic_record["base_error"]
            )
            degradation_threshold_kwh = max(
                DAILY_BIAS_RECENT_GUARD_MIN_DEGRADATION_KWH,
                historic_record["actual_total"]
                * DAILY_BIAS_RECENT_GUARD_DEGRADATION_RATIO,
            )
            if (
                historic_record["uplift"] > 1.05
                and error_degradation_kwh > degradation_threshold_kwh
            ):
                degraded_days.append(historic_record)
            overforecast_tolerance_kwh = max(
                0.1, historic_record["actual_total"] * 0.03
            )
            if (
                historic_record["base_error"]
                <= historic_record["final_error"] + overforecast_tolerance_kwh
            ):
                overforecast_days.append(historic_record)
            severe_degradation_threshold_kwh = max(
                DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_KWH,
                historic_record["actual_total"]
                * DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_RATIO,
            )
            if error_degradation_kwh >= severe_degradation_threshold_kwh:
                severely_degraded_days.append(historic_record)
        base_ratios = [historic_record["base_ratio"] for historic_record in recent_days]
        uplift_ratios = [historic_record["uplift"] for historic_record in recent_days]
        median_base_ratio = (
            _weighted_quantile(base_ratios, [1.0] * len(base_ratios), 0.5) or 1.0
        )
        median_uplift = (
            _weighted_quantile(uplift_ratios, [1.0] * len(uplift_ratios), 0.5) or 1.0
        )
        persistent_degradation = len(degraded_days) >= 2
        severe_degradation = (
            len(severely_degraded_days) >= 1 and len(overforecast_days) >= 2
        )
        if (
            (persistent_degradation or severe_degradation)
            and len(overforecast_days) >= 2
            and (median_base_ratio <= DAILY_BIAS_RECENT_GUARD_MAX_MEDIAN_BASE_RATIO)
            and (median_uplift > DAILY_BIAS_RECENT_GUARD_MIN_MEDIAN_UPLIFT)
        ):
            return {
                "veto": True,
                "reason": "similar_recent_harmful_upbias",
                "similar_days": len(recent_days),
                "harmful_days": len(degraded_days),
                "median_base_ratio": round(median_base_ratio, 4),
                "median_uplift": round(median_uplift, 4),
            }
        return {
            "veto": False,
            "reason": None,
            "similar_days": len(recent_days),
            "harmful_days": len(degraded_days),
            "median_base_ratio": round(median_base_ratio, 4),
            "median_uplift": round(median_uplift, 4),
        }

    async def _get_similar_weather_history(
        self, target_date: str
    ) -> List[Dict[str, Any]]:
        """Load immutable final Morning snapshots with later actuals before target_date."""
        if target_date in self._similar_weather_history_cache:
            return self._similar_weather_history_cache[target_date]
        history = []
        try:
            database_rows = await self._db.fetchall(
                "SELECT od.forecast_date,\n                          oh.target_hour,\n                          oh.prediction_kwh,\n                          oh.physics_kwh,\n                          oh.tfs_kwh,\n                          hp.actual_kwh,\n                          ow.clouds,\n                          ow.rain,\n                          ow.humidity,\n                          ow.solar_radiation_wm2,\n                          ow.diffuse_radiation,\n                          ac.clear_sky_radiation_wm2,\n                          ac.sun_elevation_deg\n                   FROM ops_daily_forecasts od\n                   JOIN ops_hourly_forecasts oh\n                       ON oh.snapshot_sequence = od.snapshot_sequence\n                       AND oh.target_date = od.forecast_date\n                   LEFT JOIN ops_prediction_weather ow\n                       ON ow.forecast_row_id = oh.forecast_row_id\n                   LEFT JOIN hourly_predictions hp\n                       ON hp.prediction_id = oh.prediction_id\n                   LEFT JOIN astronomy_cache ac\n                       ON ac.cache_date = oh.target_date AND ac.hour = oh.target_hour\n                   WHERE od.forecast_type = 'today'\n                     AND od.snapshot_type = 'morning_snapshot'\n                     AND od.forecast_date < ?\n                     AND od.snapshot_sequence = (\n                         SELECT MAX(od2.snapshot_sequence)\n                         FROM ops_daily_forecasts od2\n                         WHERE od2.forecast_type = 'today'\n                           AND od2.snapshot_type = 'morning_snapshot'\n                           AND od2.forecast_date = od.forecast_date\n                     )\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (hp.exclude_from_clean_evaluation = FALSE OR hp.exclude_from_clean_evaluation IS NULL)\n                     AND oh.prediction_kwh IS NOT NULL\n                     AND oh.prediction_kwh > 0.002\n                     AND oh.physics_kwh IS NOT NULL\n                     AND oh.physics_kwh > 0.001\n                     AND ow.clouds IS NOT NULL\n                     AND ow.solar_radiation_wm2 IS NOT NULL\n                     AND ac.clear_sky_radiation_wm2 IS NOT NULL\n                     AND ac.sun_elevation_deg IS NOT NULL\n                   ORDER BY od.forecast_date DESC, oh.target_hour",
                (target_date,),
            )
            for database_row in database_rows:
                forecast_date = str(database_row[0])
                hour = int(database_row[1])
                predicted_kwh = float(database_row[2])
                physics_kwh = float(database_row[3])
                tfs_kwh = float(database_row[4] or 0.0)
                actual_kwh = float(database_row[5])
                cloud_cover = float(database_row[6] or 50.0)
                rain_mm = float(database_row[7] or 0.0)
                humidity_percent = float(database_row[8] or 70.0)
                ghi_wm2 = float(database_row[9] or 0.0)
                dhi_wm2 = float(database_row[10] or 0.0)
                clear_sky_ghi_wm2 = float(database_row[11] or 0.0)
                sun_elevation = float(database_row[12] or 0.0)
                ghi_ratio = None
                if clear_sky_ghi_wm2 > 50 and ghi_wm2 >= 0:
                    ghi_ratio = min(max(ghi_wm2 / clear_sky_ghi_wm2, 0.0), 1.05)
                diffuse_share = None
                if ghi_wm2 > 5 and dhi_wm2 >= 0:
                    diffuse_share = min(max(dhi_wm2 / ghi_wm2, 0.0), 1.5)
                weather_regime = _classify_static_forecast_weather_regime(
                    clouds=cloud_cover,
                    ghi=ghi_wm2,
                    dni=0.0,
                    dhi=dhi_wm2,
                    clear_sky=clear_sky_ghi_wm2,
                    rain=rain_mm,
                    humidity=humidity_percent,
                    sun_elevation=sun_elevation,
                    hour=hour,
                )
                historic_record = {
                    "weather_regime": weather_regime,
                    "hour_bucket": _hour_bucket(hour),
                    "season": get_season(int(forecast_date[5:7])),
                    "hour": hour,
                    "sun_elevation": sun_elevation,
                    "clouds": cloud_cover,
                    "rain": rain_mm,
                    "ghi_ratio": ghi_ratio,
                    "diffuse_share": diffuse_share,
                    "pred_factor": actual_kwh / predicted_kwh,
                    "physics_factor": actual_kwh / physics_kwh,
                }
                if tfs_kwh > 0.002:
                    historic_record["tfs_factor"] = actual_kwh / tfs_kwh
                    historic_record["tfs_better_than_blend"] = abs(
                        tfs_kwh - actual_kwh
                    ) < abs(predicted_kwh - actual_kwh)
                history.append(historic_record)
                continue
        except Exception as error:
            _LOGGER.debug(
                "Similar-weather history unavailable for %s: %s", target_date, error
            )
        self._similar_weather_history_cache[target_date] = history
        return history

    async def _get_similar_weather_underforecast_signal(
        self,
        *,
        target_date: str,
        weather_regime: str,
        hour: int,
        clouds: float,
        ghi: float,
        dhi: float,
        clear_sky: float,
        rain: float,
        sun_elevation: float,
    ) -> Dict[str, Any]:
        """Return local underforecast evidence from similar final Morning snapshots."""
        if similar_weather_underforecast_is_rain_gated(
            rain=rain, weather_regime=weather_regime
        ):
            self._run_rain_gated[target_date] = (
                str(weather_regime),
                float(rain or 0.0),
            )
            _LOGGER.debug(
                "Similar-weather underforecast signal for %s suppressed by rain gating (regime=%s, rain=%s)",
                target_date,
                weather_regime,
                rain,
            )
            return {
                "underforecast_signal": False,
                "sample_count": 0,
                "weight_sum": 0.0,
                "confidence": 0.0,
                "pred_factor": 1.0,
                "physics_factor": 1.0,
                "max_factor": 1.0,
                "tfs_support_signal": False,
                "tfs_risk_signal": False,
                "pred_signal": False,
                "physics_signal": False,
            }
        if clear_sky > 50 and ghi >= 0:
            ghi_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
        else:
            ghi_ratio = None
        diffuse_share = None
        if ghi > 5 and dhi >= 0:
            diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
        current_weather = {
            "weather_regime": weather_regime,
            "hour_bucket": _hour_bucket(hour),
            "season": get_season(int(target_date[5:7])),
            "hour": hour,
            "sun_elevation": sun_elevation,
            "clouds": clouds,
            "rain": rain,
            "ghi_ratio": ghi_ratio,
            "diffuse_share": diffuse_share,
        }
        history = await self._get_similar_weather_history(target_date)
        weighted_history = []
        for historic_sample in history:
            weight = _similar_weather_weight(
                current=current_weather, historic=historic_sample
            )
            if weight > 0.025:
                weighted_history.append((historic_sample, weight))
        if len(weighted_history) < SIMILAR_WEATHER_MIN_SAMPLES:
            selected_sample_ids = {
                id(selected_pair[0]) for selected_pair in weighted_history
            }
            for historic_sample in history:
                if id(historic_sample) in selected_sample_ids:
                    continue
                if (
                    historic_sample.get("weather_regime") == weather_regime
                    and historic_sample.get("hour_bucket")
                    == current_weather["hour_bucket"]
                    and (historic_sample.get("season") == current_weather["season"])
                ):
                    weighted_history.append(
                        (
                            historic_sample,
                            max(
                                0.015,
                                _similar_weather_weight(
                                    current=current_weather, historic=historic_sample
                                ),
                            ),
                        )
                    )
        sample_count = len(weighted_history)
        weight_sum = sum((weight for unused_sample_part, weight in weighted_history))
        signal_result = {
            "underforecast_signal": False,
            "sample_count": sample_count,
            "weight_sum": round(weight_sum, 4),
            "confidence": 0.0,
            "pred_factor": 1.0,
            "physics_factor": 1.0,
            "max_factor": 1.0,
            "tfs_support_signal": False,
            "tfs_risk_signal": False,
            "pred_signal": False,
            "physics_signal": False,
        }
        if sample_count < SIMILAR_WEATHER_MIN_SAMPLES or weight_sum <= 0:
            return signal_result
        weights = [weight for unused_sample_part, weight in weighted_history]
        prediction_factors = [
            float(historic_sample["pred_factor"])
            for historic_sample, unused_sample_part in weighted_history
        ]
        physics_factors = [
            float(historic_sample["physics_factor"])
            for historic_sample, unused_sample_part in weighted_history
        ]
        median_prediction_factor = (
            _weighted_quantile(prediction_factors, weights, 0.5) or 1.0
        )
        prediction_factor_q25 = (
            _weighted_quantile(prediction_factors, weights, 0.25) or 1.0
        )
        median_physics_factor = _weighted_quantile(physics_factors, weights, 0.5) or 1.0
        physics_factor_q25 = _weighted_quantile(physics_factors, weights, 0.25) or 1.0
        underforecast_share = (
            sum(
                (
                    weight
                    for historic_sample, weight in weighted_history
                    if float(historic_sample["pred_factor"]) > 1.1
                )
            )
            / weight_sum
        )
        tfs_weighted_history = [
            (historic_sample, weight)
            for historic_sample, weight in weighted_history
            if historic_sample.get("tfs_factor") is not None
        ]
        if tfs_weighted_history:
            tfs_weights = [
                weight for unused_sample_part, weight in tfs_weighted_history
            ]
            tfs_weight_sum = sum(tfs_weights)
            tfs_factors = [
                float(historic_sample["tfs_factor"])
                for historic_sample, unused_sample_part in tfs_weighted_history
            ]
            median_tfs_factor = _weighted_quantile(tfs_factors, tfs_weights, 0.5) or 1.0
            tfs_factor_q25 = _weighted_quantile(tfs_factors, tfs_weights, 0.25) or 1.0
            tfs_better_share = (
                sum(
                    (
                        weight
                        for historic_sample, weight in tfs_weighted_history
                        if historic_sample.get("tfs_better_than_blend")
                    )
                )
                / tfs_weight_sum
                if tfs_weight_sum > 0
                else 0.0
            )
            tfs_support_signal = (
                tfs_weight_sum >= 0.3
                and tfs_better_share >= 0.45
                and (0.65 <= median_tfs_factor <= 1.6)
            )
            tfs_risk_signal = (
                tfs_weight_sum >= 1.0
                and tfs_better_share <= 0.25
                and (median_tfs_factor < 0.85)
            )
            signal_result.update(
                {
                    "tfs_sample_count": len(tfs_weighted_history),
                    "tfs_weight_sum": round(tfs_weight_sum, 4),
                    "tfs_factor_q25": round(tfs_factor_q25, 4),
                    "tfs_factor_median": round(median_tfs_factor, 4),
                    "tfs_better_share": round(tfs_better_share, 4),
                    "tfs_support_signal": tfs_support_signal,
                    "tfs_risk_signal": tfs_risk_signal,
                }
            )
        prediction_underforecast_signal = (
            underforecast_share >= SIMILAR_WEATHER_MIN_UNDERFORECAST_SHARE
            and prediction_factor_q25 >= SIMILAR_WEATHER_MIN_PRED_FACTOR_Q25
            and (median_prediction_factor > 1.1)
        )
        physics_underforecast_signal = (
            physics_factor_q25 >= SIMILAR_WEATHER_MIN_PHYSICS_FACTOR_Q25
            and median_physics_factor > 1.2
            and (
                median_prediction_factor
                >= SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_PRED_MEDIAN
            )
            and (underforecast_share >= SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_SHARE)
        )
        if not prediction_underforecast_signal and (not physics_underforecast_signal):
            return signal_result
        confidence = min(0.7, max(0.15, weight_sum / (weight_sum + 2.0)))
        if weight_sum < SIMILAR_WEATHER_MAX_WEIGHT_SUM_FOR_THIN_HISTORY:
            maximum_factor = SIMILAR_WEATHER_MAX_FACTOR_THIN
        elif weight_sum < 2.0:
            maximum_factor = SIMILAR_WEATHER_MAX_FACTOR_MEDIUM
        else:
            maximum_factor = SIMILAR_WEATHER_MAX_FACTOR_STRONG
        prediction_factor = 1.0
        if prediction_underforecast_signal:
            prediction_factor = min(
                maximum_factor, 1.0 + (median_prediction_factor - 1.0) * confidence
            )
        physics_factor = 1.0
        if physics_underforecast_signal:
            physics_factor = min(
                SIMILAR_WEATHER_MAX_PHYSICS_FACTOR,
                1.0 + (median_physics_factor - 1.0) * confidence,
            )
        signal_result.update(
            {
                "underforecast_signal": True,
                "pred_signal": prediction_underforecast_signal,
                "physics_signal": physics_underforecast_signal,
                "confidence": round(confidence, 4),
                "pred_factor": round(max(1.0, prediction_factor), 4),
                "physics_factor": round(max(1.0, physics_factor), 4),
                "max_factor": round(maximum_factor, 4),
                "underforecast_share": round(underforecast_share, 4),
                "pred_factor_q25": round(prediction_factor_q25, 4),
                "pred_factor_median": round(median_prediction_factor, 4),
                "physics_factor_q25": round(physics_factor_q25, 4),
                "physics_factor_median": round(median_physics_factor, 4),
            }
        )
        return signal_result

    async def _check_group_learning_support(
        self,
        clouds: float,
        hour: int,
        physics_groups: Optional[List[Dict[str, Any]]],
        ai_groups: Optional[List[Dict[str, Any]]],
    ) -> bool:
        """Check if per-group method learning supports AI over Physics. @zara V16.4.0

        Queries group_method_performance for each panel group under current conditions.
        Returns True if the majority of groups with learned data show AI outperforming Physics.
        Silently returns False when no learned data exists (new installations).
        """
        if not physics_groups or not ai_groups:
            return False
        cloud_bucket = (
            "clear" if clouds < 30 else "partly_cloudy" if clouds < 70 else "overcast"
        )
        hour_bucket = "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
        season = get_season(self._policy_now().month)
        group_names = tuple((str(group.get("name") or "") for group in physics_groups))
        cache_key = (
            "group_method_performance",
            cloud_bucket,
            hour_bucket,
            season,
            group_names,
        )
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            return bool(self._forecast_policy_read_cache[cache_key])
        supported_groups = 0
        considered_groups = 0
        try:
            for physics_group in physics_groups:
                group_key = physics_group.get("name")
                if not group_key:
                    continue
                database_row = await self._db.fetchone(
                    "SELECT physics_mae, ai_mae, sample_count, best_method\n                       FROM group_method_performance\n                       WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                           AND season = ?",
                    (group_key, cloud_bucket, hour_bucket, season),
                )
                if not database_row or not database_row[2] or database_row[2] < 5:
                    database_row = await self._db.fetchone(
                        "SELECT physics_mae, ai_mae, sample_count, best_method\n                           FROM group_method_performance\n                           WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS NULL",
                        (group_key, cloud_bucket, hour_bucket),
                    )
                if database_row and database_row[2] and (database_row[2] >= 5):
                    considered_groups += 1
                    physics_mae = database_row[0]
                    ai_mae = database_row[1]
                    if ai_mae is not None:
                        if physics_mae is not None and ai_mae < physics_mae:
                            supported_groups += 1
        except Exception:
            if self._forecast_policy_read_cache is not None:
                self._forecast_policy_read_cache[cache_key] = False
            return False
        if considered_groups == 0:
            if self._forecast_policy_read_cache is not None:
                self._forecast_policy_read_cache[cache_key] = False
            return False
        majority_supported = supported_groups > considered_groups / 2
        if self._forecast_policy_read_cache is not None:
            self._forecast_policy_read_cache[cache_key] = majority_supported
        return majority_supported

    async def _load_ensemble_group_weights(self) -> Dict[str, Dict]:
        """Load all learned ensemble group weights from DB. @zara V17.0.0

        Returns nested dict: {group_name: {(cloud_bucket, hour_bucket): (lstm_w, ridge_w)}}
        Called ONCE per forecast cycle and cached.
        Prefers season-specific weights, fallback to overall (season IS NULL).
        """
        ensemble_weights = {}
        season = get_season(self._policy_now().month)
        cache_key = ("ensemble_group_weights", season)
        if (
            self._forecast_policy_read_cache is not None
            and cache_key in self._forecast_policy_read_cache
        ):
            return self._forecast_policy_read_cache[cache_key]
        try:
            seasonal_weight_rows = await self._db.fetchall(
                "SELECT group_name, cloud_bucket, hour_bucket,\n                          lstm_weight, ridge_weight, sample_count\n                   FROM ensemble_group_weights\n                   WHERE sample_count >= 5 AND season = ?",
                (season,),
            )
            for database_row in seasonal_weight_rows:
                group_key = database_row[0]
                if group_key not in ensemble_weights:
                    ensemble_weights[group_key] = {}
                ensemble_weights[group_key][database_row[1], database_row[2]] = (
                    database_row[3],
                    database_row[4],
                )
            fallback_weight_rows = await self._db.fetchall(
                "SELECT group_name, cloud_bucket, hour_bucket,\n                          lstm_weight, ridge_weight, sample_count\n                   FROM ensemble_group_weights\n                   WHERE sample_count >= 5 AND season IS NULL"
            )
            for database_row in fallback_weight_rows:
                group_key = database_row[0]
                if group_key not in ensemble_weights:
                    ensemble_weights[group_key] = {}
                bucket_key = (database_row[1], database_row[2])
                if bucket_key not in ensemble_weights[group_key]:
                    ensemble_weights[group_key][bucket_key] = (
                        database_row[3],
                        database_row[4],
                    )
        except Exception:
            pass
        if self._forecast_policy_read_cache is not None:
            self._forecast_policy_read_cache[cache_key] = ensemble_weights
        return ensemble_weights

    def _blend_group_details(
        self,
        physics_groups: List[Dict[str, Any]],
        ai_groups: List[Dict[str, Any]],
        ai_weight: float,
    ) -> List[Dict[str, Any]]:
        """Blend physics and AI group predictions. @zara V16.0.0"""
        if len(physics_groups) != len(ai_groups):
            _LOGGER.warning(
                "Group count mismatch: physics=%d, ai=%d. Using physics only.",
                len(physics_groups),
                len(ai_groups),
            )
            return physics_groups
        blended_groups = []
        physics_weight = 1 - ai_weight
        for physics_group, ai_group in zip(physics_groups, ai_groups):
            blended_group_kwh = (
                physics_group["power_kwh"] * physics_weight
                + ai_group["power_kwh"] * ai_weight
            )
            raw_physics_kwh = physics_group.get("raw_physics_kwh")
            blended_groups.append(
                {
                    "name": physics_group.get("name", ai_group.get("name")),
                    "power_kwh": round(blended_group_kwh, 4),
                    "contribution_percent": 0,
                    "physics_kwh": round(physics_group["power_kwh"], 4),
                    "raw_physics_kwh": (
                        round(float(raw_physics_kwh), 4)
                        if raw_physics_kwh is not None
                        else None
                    ),
                    "ai_kwh": round(ai_group["power_kwh"], 4),
                    "ai_weight": round(ai_weight, 2),
                }
            )
        total_group_kwh = sum(
            (group_result["power_kwh"] for group_result in blended_groups)
        )
        for group_result in blended_groups:
            group_result["contribution_percent"] = (
                round(group_result["power_kwh"] / total_group_kwh * 100, 1)
                if total_group_kwh > 0
                else 0
            )
        return blended_groups

    def _blend_ensemble_groups(
        self,
        lstm_groups: List[Dict[str, Any]],
        ridge_groups: List[Dict[str, Any]],
        lstm_w: float,
        ridge_w: float,
    ) -> List[Dict[str, Any]]:
        """Blend LSTM and Ridge group predictions with per-model storage. @zara V16.0.2"""
        if len(lstm_groups) != len(ridge_groups):
            return lstm_groups
        blended_groups = []
        for lstm_group, ridge_group in zip(lstm_groups, ridge_groups):
            blended_group_kwh = (
                lstm_group["power_kwh"] * lstm_w + ridge_group["power_kwh"] * ridge_w
            )
            blended_groups.append(
                {
                    "name": lstm_group.get("name", ridge_group.get("name")),
                    "power_kwh": round(blended_group_kwh, 4),
                    "contribution_percent": 0,
                    "lstm_kwh": round(lstm_group["power_kwh"], 4),
                    "ridge_kwh": round(ridge_group["power_kwh"], 4),
                    "azimuth_deg": lstm_group.get("azimuth_deg"),
                    "tilt_deg": lstm_group.get("tilt_deg"),
                    "capacity_kwp": lstm_group.get("capacity_kwp"),
                }
            )
        total_group_kwh = sum(
            (group_result["power_kwh"] for group_result in blended_groups)
        )
        for group_result in blended_groups:
            group_result["contribution_percent"] = (
                round(group_result["power_kwh"] / total_group_kwh * 100, 1)
                if total_group_kwh > 0
                else 0
            )
        return blended_groups

    def _blend_ensemble_groups_adaptive(
        self,
        lstm_groups: List[Dict[str, Any]],
        ridge_groups: List[Dict[str, Any]],
        bucket_key: Tuple[str, str],
        learned_weights: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Blend LSTM and Ridge per group with adaptive per-group weights. @zara V16.0.3

        For each group, looks up learned (lstm_w, ridge_w) from ensemble_group_weights.
        Falls back to global ENSEMBLE_LSTM_WEIGHT / ENSEMBLE_RIDGE_WEIGHT if not found.
        """
        if len(lstm_groups) != len(ridge_groups):
            return lstm_groups
        blended_groups = []
        for lstm_group, ridge_group in zip(lstm_groups, ridge_groups):
            group_key = lstm_group.get("name", ridge_group.get("name"))
            lstm_weight = ENSEMBLE_LSTM_WEIGHT
            ridge_weight = ENSEMBLE_RIDGE_WEIGHT
            if learned_weights and group_key in learned_weights:
                group_weights = learned_weights[group_key]
                if bucket_key in group_weights:
                    lstm_weight, ridge_weight = group_weights[bucket_key]
                elif (
                    learned_weights.get("_overall_")
                    and bucket_key in learned_weights["_overall_"]
                ):
                    lstm_weight, ridge_weight = learned_weights["_overall_"][bucket_key]
            elif (
                learned_weights
                and "_overall_" in learned_weights
                and (bucket_key in learned_weights["_overall_"])
            ):
                lstm_weight, ridge_weight = learned_weights["_overall_"][bucket_key]
            blended_group_kwh = (
                lstm_group["power_kwh"] * lstm_weight
                + ridge_group["power_kwh"] * ridge_weight
            )
            blended_groups.append(
                {
                    "name": group_key,
                    "power_kwh": round(blended_group_kwh, 4),
                    "contribution_percent": 0,
                    "lstm_kwh": round(lstm_group["power_kwh"], 4),
                    "ridge_kwh": round(ridge_group["power_kwh"], 4),
                    "lstm_weight": round(lstm_weight, 4),
                    "ridge_weight": round(ridge_weight, 4),
                    "azimuth_deg": lstm_group.get("azimuth_deg"),
                    "tilt_deg": lstm_group.get("tilt_deg"),
                    "capacity_kwp": lstm_group.get("capacity_kwp"),
                }
            )
            continue
        total_group_kwh = sum(
            (group_result["power_kwh"] for group_result in blended_groups)
        )
        for group_result in blended_groups:
            group_result["contribution_percent"] = (
                round(group_result["power_kwh"] / total_group_kwh * 100, 1)
                if total_group_kwh > 0
                else 0
            )
        return blended_groups

    def _map_tfs_groups(
        self, tfs_group_list: list, sfml_groups: list
    ) -> Dict[str, float]:
        tfs_predictions_by_name = {
            tfs_group["name"]: tfs_group.get("p50", 0.0) for tfs_group in tfs_group_list
        }
        mapped_tfs_predictions = {}
        for group_index, sfml_group in enumerate(sfml_groups):
            group_name = sfml_group.get("name", "")
            if group_name in tfs_predictions_by_name:
                mapped_tfs_predictions[group_name] = tfs_predictions_by_name[group_name]
            elif group_index < len(tfs_group_list):
                mapped_tfs_predictions[group_name] = tfs_group_list[group_index].get(
                    "p50", 0.0
                )
        return mapped_tfs_predictions

    def _parse_datetime(self, hour_data: Dict[str, Any]) -> Optional[datetime]:
        """Parse datetime from hour data. @zara"""
        try:
            datetime_text = hour_data.get("datetime") or hour_data.get("time")
            if datetime_text:
                if isinstance(datetime_text, datetime):
                    return datetime_text
                return datetime.fromisoformat(datetime_text.replace("Z", "+00:00"))
        except Exception:
            pass
        return None if True else None
