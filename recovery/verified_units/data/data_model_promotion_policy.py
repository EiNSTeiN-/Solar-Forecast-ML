# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Canonical cycle-aware model promotion policy."""

from __future__ import annotations
import math
import statistics
from datetime import date, datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

MODEL_PROMOTION_MIN_INDEPENDENT_DAYS = 7
MODEL_PROMOTION_MIN_COMMON_HOURS_PER_CYCLE = 4
MODEL_PROMOTION_BOOTSTRAP_SEED = 17290
MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS = 3
MODEL_PROMOTION_FORECAST_CYCLES = ("post_midnight", "pre_sunrise")
MODEL_PROMOTION_WEATHER_REGIMES = frozenset(
    {
        "stable_good",
        "bright_mixed",
        "rain_overcast",
        "dry_cloud_edge",
        "mixed_uncertain",
        "diffuse_overcast",
        "low_ghi_overcast",
        "low_sun_uncertain",
    }
)
MODEL_PROMOTION_SLICE_REGRESSION_FLOOR_KWH = 0.005
MODEL_PROMOTION_POLICY_CONTRACT_V3 = "cycle_aware_model_promotion_policy_v3"
MODEL_PROMOTION_CHAMPION_MIN_AGE_DAYS_FOR_NON_INFERIORITY = 28
MODEL_PROMOTION_SLICE_T95 = {
    2: 6.314,
    3: 2.92,
    4: 2.353,
    5: 2.132,
    6: 2.015,
    7: 1.943,
    8: 1.895,
    9: 1.86,
    10: 1.833,
    11: 1.812,
    12: 1.796,
    13: 1.782,
    14: 1.771,
    15: 1.761,
}


def _sample_weather_regime(sample: Any) -> Any:
    if isinstance(sample, Mapping):
        return sample.get("weather_regime")
    return getattr(sample, "weather_regime", None)


def _sample_target_date(sample: Any) -> str:
    if isinstance(sample, Mapping):
        return str(sample["target_date"])
    return str(sample.target_date)


def _observed_regimes(samples: Iterable[Any]) -> set[Any]:
    if isinstance(samples, (str, bytes)):
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        )
    try:
        sample_list = list(samples)
    except TypeError as error:
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        ) from error
    return {_sample_weather_regime(sample) for sample in sample_list}


def _daily_delta_noise_band(deltas: Sequence[float]) -> float:
    sample_count = len(deltas)
    if sample_count < 2:
        return MODEL_PROMOTION_SLICE_REGRESSION_FLOOR_KWH
    standard_deviation = statistics.stdev(deltas)
    if standard_deviation == 0:
        return MODEL_PROMOTION_SLICE_REGRESSION_FLOOR_KWH
    t95 = MODEL_PROMOTION_SLICE_T95.get(sample_count, MODEL_PROMOTION_SLICE_T95[15])
    noise_band = t95 * standard_deviation / math.sqrt(sample_count)
    return round(max(MODEL_PROMOTION_SLICE_REGRESSION_FLOOR_KWH, noise_band), 6)


def slice_allowed_daily_energy_regression(
    samples: Iterable[Any], forecast_cycle: Optional[str], weather_regime: Optional[str]
) -> float:
    # fmt: off
    """Freeze the one-sided 95% noise band for one cycle/regime slice.

    ``None`` widens the slice: one cycle over all regimes, or both cycles as
    the paired daily total.
    """
    # fmt: on
    from ..ai.ai_model_promotion import cycle_slice_day_deltas as calculate_deltas

    return _daily_delta_noise_band(
        calculate_deltas(list(samples), forecast_cycle, weather_regime)
    )


def champion_age_days(
    samples: Iterable[Any], champion_trained_on: Optional[date]
) -> Optional[int]:
    """Return the champion's calendar age on the last evaluated day."""
    if champion_trained_on is None:
        return
    if isinstance(champion_trained_on, datetime):
        champion_trained_on = champion_trained_on.date()
    latest_date = max(
        (date.fromisoformat(_sample_target_date(sample)) for sample in samples)
    )
    return max(0, (latest_date - champion_trained_on).days)


def build_cycle_aware_promotion_policy_config(samples: Iterable[Any]) -> Dict[str, Any]:
    """Return the persistable V2 evaluation config with frozen slice bands."""
    if isinstance(samples, (str, bytes)):
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        )
    try:
        sample_list = list(samples)
    except TypeError as error:
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        ) from error
    regimes = _observed_regimes(sample_list)
    if not regimes or not regimes <= MODEL_PROMOTION_WEATHER_REGIMES:
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        )
    return {
        "min_independent_eligible_days": MODEL_PROMOTION_MIN_INDEPENDENT_DAYS,
        "min_common_clean_production_hours_per_cycle": MODEL_PROMOTION_MIN_COMMON_HOURS_PER_CYCLE,
        "bootstrap_seed": MODEL_PROMOTION_BOOTSTRAP_SEED,
        "critical_slice_min_independent_days": MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS,
        "critical_slices": tuple(
            (
                {
                    "name": f"{forecast_cycle}:weather_regime:{weather_regime}",
                    "forecast_cycle": forecast_cycle,
                    "weather_regime": weather_regime,
                    "min_independent_days": MODEL_PROMOTION_CRITICAL_SLICE_MIN_DAYS,
                    "allowed_daily_energy_regression": slice_allowed_daily_energy_regression(
                        sample_list, forecast_cycle, weather_regime
                    ),
                }
                for forecast_cycle in MODEL_PROMOTION_FORECAST_CYCLES
                for weather_regime in sorted(regimes)
            )
        ),
    }


def build_cycle_aware_promotion_policy_config_v3(
    samples: Iterable[Any], *, champion_trained_on: Optional[date]
) -> Dict[str, Any]:
    """Return the persistable V3 config: V2 slices plus frozen cycle/total bands."""
    if isinstance(samples, (str, bytes)):
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        )
    try:
        sample_list = list(samples)
    except TypeError as error:
        raise ValueError(
            "cycle evaluation requires recognized day-zero weather regimes"
        ) from error
    return {
        **build_cycle_aware_promotion_policy_config(sample_list),
        "policy_contract_version": MODEL_PROMOTION_POLICY_CONTRACT_V3,
        "cycle_noise_bands": tuple(
            (
                {
                    "forecast_cycle": forecast_cycle,
                    "allowed_daily_energy_regression": slice_allowed_daily_energy_regression(
                        sample_list, forecast_cycle, None
                    ),
                }
                for forecast_cycle in MODEL_PROMOTION_FORECAST_CYCLES
            )
        ),
        "allowed_total_daily_energy_regression": slice_allowed_daily_energy_regression(
            sample_list, None, None
        ),
        "champion_min_age_days_for_non_inferiority": MODEL_PROMOTION_CHAMPION_MIN_AGE_DAYS_FOR_NON_INFERIORITY,
        "champion_age_days": champion_age_days(sample_list, champion_trained_on),
    }
