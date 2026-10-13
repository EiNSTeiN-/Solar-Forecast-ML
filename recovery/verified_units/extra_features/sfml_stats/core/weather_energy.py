# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Deterministic weather and energy analytics for the modern UI. @zara"""
from __future__ import annotations
import math
from collections import defaultdict
from statistics import fmean
from typing import Any, Iterable

MIN_DAYLIGHT_HOURS = 8
MIN_WEATHER_COVERAGE = 70.0
MIN_IMPACT_POINTS = 30
MIN_IMPACT_DAYS = 3
MIN_COMPARABLE_CANDIDATES = 14


def _number(
    value: Any, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    if value is None or isinstance(value, bool):
        return
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(numeric_value):
        return
    if minimum is not None and numeric_value < minimum:
        return
    if maximum is not None and numeric_value > maximum:
        return
    return numeric_value


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def is_valid_weather_energy_point(row: dict[str, Any]) -> bool:
    """Apply the authoritative hard-exclusion semantics to one comparison hour. @zara"""
    forecast_kwh = _number(row.get("forecast"), minimum=0)
    actual_kwh = _number(row.get("actual"), minimum=0)
    if (
        forecast_kwh is None
        or actual_kwh is None
        or max(forecast_kwh, actual_kwh) < 0.01
    ):
        return False
    if bool(row.get("mppt_throttled")) or bool(row.get("inverter_clipped")):
        return False
    if bool(row.get("exclude_from_learning")):
        return False
    if bool(row.get("exclude_from_clean_evaluation")):
        alert_type = str(row.get("weather_alert_type") or "").lower()
        if alert_type not in {"weather_miss", "weather_alert", "unexpected_clouds"}:
            return False
    return row.get("data_quarantine_reason") in {None, ""}


def calculate_solar_conditions(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calculate observed PV conditions from radiation, Clear Sky and clouds. @zara"""
    ordered_rows = sorted(rows, key=lambda item: int(item.get("hour", 0)))
    daylight_rows = [
        row
        for row in ordered_rows
        if (_number(row.get("clear_sky_wm2"), minimum=0) or 0) >= 20
    ]
    valid_rows = [
        row
        for row in daylight_rows
        if _number(row.get("solar_actual_wm2"), minimum=0) is not None
        if _number(row.get("cloud_actual_percent"), minimum=0, maximum=100) is not None
    ]
    coverage_percent = (
        100.0 * len(valid_rows) / len(daylight_rows) if daylight_rows else 0.0
    )
    unavailable_conditions = {
        "available": False,
        "reason": "insufficient_weather_coverage",
        "valid_hours": len(valid_rows),
        "daylight_hours": len(daylight_rows),
        "coverage_percent": round(coverage_percent, 1),
        "minimum_hours": MIN_DAYLIGHT_HOURS,
        "minimum_coverage_percent": MIN_WEATHER_COVERAGE,
    }
    if len(valid_rows) < MIN_DAYLIGHT_HOURS or coverage_percent < MIN_WEATHER_COVERAGE:
        return unavailable_conditions
    radiation_total_wm2 = sum(
        (_number(row.get("solar_actual_wm2"), minimum=0) or 0 for row in valid_rows)
    )
    clear_sky_total_wm2 = sum(
        (_number(row.get("clear_sky_wm2"), minimum=0) or 0 for row in valid_rows)
    )
    if clear_sky_total_wm2 <= 0:
        return unavailable_conditions
    radiation_potential_percent = _clamp(
        radiation_total_wm2 / clear_sky_total_wm2 * 100
    )
    strong_daylight_rows = [
        row
        for row in valid_rows
        if (_number(row.get("clear_sky_wm2"), minimum=0) or 0) >= 100
    ]
    usable_solar_window_percent = (
        100.0
        * sum(
            (
                (_number(row.get("solar_actual_wm2"), minimum=0) or 0) >= 100
                for row in strong_daylight_rows
            )
        )
        / len(strong_daylight_rows)
        if strong_daylight_rows
        else 0.0
    )
    radiation_ratios = [
        _clamp(
            (_number(row.get("solar_actual_wm2"), minimum=0) or 0)
            / (_number(row.get("clear_sky_wm2"), minimum=1) or 1),
            0,
            1.5,
        )
        for row in strong_daylight_rows
    ]
    ratio_changes = [
        abs(next_ratio - previous_ratio)
        for previous_ratio, next_ratio in zip(radiation_ratios, radiation_ratios[1:])
    ]
    radiation_volatility = fmean(ratio_changes) if ratio_changes else 0.0
    radiation_stability_percent = _clamp(
        100.0 * (1.0 - min(radiation_volatility / 0.75, 1.0))
    )
    mean_cloud_percent = fmean(
        (
            _number(row.get("cloud_actual_percent"), minimum=0, maximum=100) or 0
            for row in valid_rows
        )
    )
    cloud_openness_percent = 100.0 - mean_cloud_percent
    conditions_score = (
        radiation_potential_percent * 0.45
        + usable_solar_window_percent * 0.25
        + radiation_stability_percent * 0.2
        + cloud_openness_percent * 0.1
    )
    return {
        "available": True,
        "score": round(conditions_score, 1),
        "class": _conditions_class(conditions_score),
        "basis": "observed_weather",
        "valid_hours": len(valid_rows),
        "daylight_hours": len(daylight_rows),
        "coverage_percent": round(coverage_percent, 1),
        "daily_radiation_kwh_m2": round(radiation_total_wm2 / 1000.0, 2),
        "components": [
            {
                "id": "radiation_potential",
                "value": round(radiation_potential_percent, 1),
                "weight": 45,
            },
            {
                "id": "usable_solar_window",
                "value": round(usable_solar_window_percent, 1),
                "weight": 25,
            },
            {
                "id": "radiation_stability",
                "value": round(radiation_stability_percent, 1),
                "weight": 20,
            },
            {
                "id": "cloud_openness",
                "value": round(cloud_openness_percent, 1),
                "weight": 10,
            },
        ],
        "radiation_volatility": round(radiation_volatility, 3),
    }


def _conditions_class(score: float) -> str:
    if score >= 80:
        return "strong"
    if score >= 60:
        return "good"
    if score >= 40:
        return "mixed"
    return "weak"


def build_day_story(
    rows: Iterable[dict[str, Any]], *, target_date: str
) -> dict[str, Any]:
    """Build a gap-preserving 24-hour weather-to-energy timeline. @zara"""
    rows_by_hour = {}
    duplicate_hours = []
    for row in rows:
        hour = int(row.get("hour", -1))
        if not 0 <= hour <= 23:
            continue
        if hour in rows_by_hour:
            duplicate_hours.append(hour)
            continue
        rows_by_hour[hour] = row
    hour_story = []
    cumulative_actual_kwh = 0.0
    cumulative_forecast_kwh = 0.0
    valid_energy_hours = 0
    weather_hours = 0
    for hour in range(24):
        resolved_row = rows_by_hour.get(hour, {})
        forecast_kwh = _number(resolved_row.get("forecast"), minimum=0)
        actual_kwh = _number(resolved_row.get("actual"), minimum=0)
        actual_radiation_wm2 = _number(resolved_row.get("solar_actual_wm2"), minimum=0)
        forecast_radiation_wm2 = _number(
            resolved_row.get("solar_forecast_wm2"), minimum=0
        )
        clear_sky_wm2 = _number(resolved_row.get("clear_sky_wm2"), minimum=0)
        cloud_percent = _number(
            resolved_row.get("cloud_actual_percent"), minimum=0, maximum=100
        )
        if actual_kwh is not None:
            cumulative_actual_kwh += actual_kwh
        if forecast_kwh is not None:
            cumulative_forecast_kwh += forecast_kwh
        eligible = is_valid_weather_energy_point(
            {**resolved_row, "forecast": forecast_kwh, "actual": actual_kwh}
        )
        valid_energy_hours += int(eligible)
        weather_hours += int(
            actual_radiation_wm2 is not None or cloud_percent is not None
        )
        if (
            forecast_kwh is None
            and actual_kwh is None
            and (actual_radiation_wm2 is None)
            and (cloud_percent is None)
        ):
            point_state = "missing"
        elif forecast_kwh is None:
            point_state = "actual_without_forecast"
        elif actual_kwh is None:
            point_state = "forecast_without_actual"
        elif eligible:
            point_state = "valid"
        else:
            point_state = "excluded"
        hour_story.append(
            {
                "hour": hour,
                "forecast_kwh": _round(forecast_kwh, 3),
                "actual_kwh": _round(actual_kwh, 3),
                "physics_kwh": _round(
                    _number(resolved_row.get("physics"), minimum=0), 3
                ),
                "ai_kwh": _round(_number(resolved_row.get("ai"), minimum=0), 3),
                "solar_forecast_wm2": _round(forecast_radiation_wm2, 1),
                "solar_actual_wm2": _round(actual_radiation_wm2, 1),
                "clear_sky_wm2": _round(clear_sky_wm2, 1),
                "cloud_actual_percent": _round(cloud_percent, 1),
                "temperature_actual_c": _round(
                    _number(
                        resolved_row.get("temperature_actual_c"),
                        minimum=-70,
                        maximum=70,
                    ),
                    1,
                ),
                "sun_elevation_deg": _round(
                    _number(
                        resolved_row.get("sun_elevation_deg"), minimum=-90, maximum=90
                    ),
                    1,
                ),
                "cumulative_actual_kwh": round(cumulative_actual_kwh, 3),
                "cumulative_forecast_kwh": round(cumulative_forecast_kwh, 3),
                "error_kwh": (
                    _round(actual_kwh - forecast_kwh, 3)
                    if actual_kwh is not None and forecast_kwh is not None
                    else None
                ),
                "eligible": eligible,
                "state": point_state,
            }
        )
        continue
    eligible_hours = [row for row in hour_story if row["eligible"]]
    largest_deviation = max(
        eligible_hours, key=lambda item: abs(item["error_kwh"]), default=None
    )
    astronomy_row = next(
        (
            row
            for row in rows_by_hour.values()
            if row.get("sunrise") or row.get("sunset")
        ),
        {},
    )
    return {
        "date": target_date,
        "hours": hour_story,
        "valid_energy_hours": valid_energy_hours,
        "weather_hours": weather_hours,
        "energy_coverage_percent": round(
            100 * sum((row["actual_kwh"] is not None for row in hour_story)) / 24, 1
        ),
        "weather_coverage_percent": round(100 * weather_hours / 24, 1),
        "duplicate_hours": sorted(set(duplicate_hours)),
        "largest_deviation": (
            {
                "hour": largest_deviation["hour"],
                "error_kwh": largest_deviation["error_kwh"],
                "absolute_error_kwh": abs(largest_deviation["error_kwh"]),
            }
            if largest_deviation
            else None
        ),
        "astronomy": {
            "sunrise": astronomy_row.get("sunrise"),
            "sunset": astronomy_row.get("sunset"),
            "daylight_hours": _round(
                _number(astronomy_row.get("daylight_hours"), minimum=0), 2
            ),
        },
    }


def build_production_performance(
    energy: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    *,
    capacity_kwp: Any,
    month_yield_kwh: Any,
    year_yield_kwh: Any,
    solar_conditions: dict[str, Any],
) -> dict[str, Any]:
    """Calculate production metrics only from persisted SFML values. @zara"""
    energy = energy or {}
    summary = summary or {}
    yield_kwh = _number(energy.get("solar_yield_kwh"), minimum=0)
    predicted_kwh = _number(summary.get("predicted_total_kwh"), minimum=0)
    resolved_capacity_kwp = _number(capacity_kwp, minimum=0.01)
    radiation_kwh_m2 = _number(
        solar_conditions.get("daily_radiation_kwh_m2"), minimum=0.01
    )
    return {
        "yield_kwh": _round(yield_kwh, 2),
        "forecast_kwh": _round(predicted_kwh, 2),
        "forecast_fulfilment_percent": (
            _round(yield_kwh / predicted_kwh * 100, 1)
            if yield_kwh is not None and predicted_kwh
            else None
        ),
        "specific_yield_kwh_kwp": (
            _round(yield_kwh / resolved_capacity_kwp, 2)
            if yield_kwh is not None and resolved_capacity_kwp
            else None
        ),
        "installed_capacity_kwp": _round(resolved_capacity_kwp, 2),
        "peak_power_w": _round(_number(energy.get("peak_solar_w"), minimum=0), 0),
        "peak_time": energy.get("peak_solar_time") or summary.get("peak_power_time"),
        "month_yield_kwh": _round(_number(month_yield_kwh, minimum=0), 1),
        "year_yield_kwh": _round(_number(year_yield_kwh, minimum=0), 1),
        "radiation_to_energy_ratio": (
            _round(yield_kwh / radiation_kwh_m2, 2)
            if yield_kwh is not None and radiation_kwh_m2
            else None
        ),
        "radiation_to_energy_unit": "kWh_per_kWh_m2",
        "production_hours": int(summary.get("production_hours") or 0),
        "evaluation_coverage_percent": _round(
            _number(summary.get("evaluation_coverage_percent"), minimum=0, maximum=100),
            1,
        ),
    }


CLOUD_GROUPS = (
    ("clear", 0.0, 20.0),
    ("light_cloud", 20.0, 50.0),
    ("cloudy", 50.0, 80.0),
    ("overcast", 80.0, 100.0),
)


def _metric_group(group_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_rows = [row for row in rows if is_valid_weather_energy_point(row)]
    sample_dates = {str(row.get("date")) for row in valid_rows}
    group_summary = {
        "id": group_id,
        "points": len(valid_rows),
        "days": len(sample_dates),
        "minimum_points": MIN_IMPACT_POINTS,
        "minimum_days": MIN_IMPACT_DAYS,
    }
    if len(valid_rows) < MIN_IMPACT_POINTS or len(sample_dates) < MIN_IMPACT_DAYS:
        return {**group_summary, "available": False, "reason": "insufficient_sample"}
    forecast_errors_kwh = [
        float(row["forecast"]) - float(row["actual"]) for row in valid_rows
    ]
    model_metrics = {}
    for model_key in ("forecast", "physics", "ai"):
        prediction_actual_pairs = [
            (float(row[model_key]), float(row["actual"]))
            for row in valid_rows
            if _number(row.get(model_key), minimum=0) is not None
        ]
        if len(prediction_actual_pairs) >= MIN_IMPACT_POINTS:
            model_metrics[model_key] = {
                "mae_kwh": round(
                    fmean(
                        (
                            abs(prediction_kwh - actual_kwh)
                            for prediction_kwh, actual_kwh in prediction_actual_pairs
                        )
                    ),
                    3,
                ),
                "points": len(prediction_actual_pairs),
            }
    best_model = (
        min(model_metrics, key=lambda key: model_metrics[key]["mae_kwh"])
        if model_metrics
        else None
    )
    return {
        **group_summary,
        "available": True,
        "mae_kwh": round(
            fmean((abs(error_kwh) for error_kwh in forecast_errors_kwh)), 3
        ),
        "bias_kwh": round(fmean(forecast_errors_kwh), 3),
        "model_metrics": model_metrics,
        "best_model": best_model,
    }


def build_weather_impact(
    rows: Iterable[dict[str, Any]], *, period: dict[str, str]
) -> dict[str, Any]:
    """Aggregate forecast error by observed weather without causal claims. @zara"""
    rows_list = list(rows)
    cloud_groups = []
    for group_id, cloud_lower_bound, cloud_upper_bound in CLOUD_GROUPS:
        group_rows = []
        for row in rows_list:
            cloud_percent = _number(
                row.get("cloud_actual_percent"), minimum=0, maximum=100
            )
            if cloud_percent is None:
                continue
            if (
                cloud_lower_bound == 0
                and cloud_lower_bound <= cloud_percent <= cloud_upper_bound
                or cloud_lower_bound < cloud_percent <= cloud_upper_bound
            ):
                group_rows.append(row)
        cloud_groups.append(_metric_group(group_id, group_rows))
    rows_by_date = defaultdict(list)
    for row in rows_list:
        rows_by_date[str(row.get("date"))].append(row)
    rows_by_volatility = defaultdict(list)
    for day_rows in rows_by_date.values():
        radiation_ratios = []
        for row in sorted(day_rows, key=lambda item: int(item.get("hour", 0))):
            clear_sky_wm2 = _number(row.get("clear_sky_wm2"), minimum=100)
            actual_radiation_wm2 = _number(row.get("solar_actual_wm2"), minimum=0)
            if clear_sky_wm2 is not None:
                if actual_radiation_wm2 is not None:
                    radiation_ratios.append(
                        _clamp(actual_radiation_wm2 / clear_sky_wm2, 0, 1.5)
                    )
        ratio_changes = [
            abs(next_ratio - previous_ratio)
            for previous_ratio, next_ratio in zip(
                radiation_ratios, radiation_ratios[1:]
            )
        ]
        if len(ratio_changes) < 4:
            continue
        radiation_volatility = fmean(ratio_changes)
        group_id = (
            "stable"
            if radiation_volatility <= 0.15
            else "variable" if radiation_volatility <= 0.35 else "volatile"
        )
        rows_by_volatility[group_id].extend(day_rows)
        continue
    return {
        "period": period,
        "basis": "observed_weather_and_matched_forecast_actual",
        "minimum_points": MIN_IMPACT_POINTS,
        "minimum_days": MIN_IMPACT_DAYS,
        "cloud_groups": cloud_groups,
        "volatility_groups": [
            _metric_group(group_id, rows_by_volatility.get(group_id, []))
            for group_id in ("stable", "variable", "volatile")
        ],
    }


def build_day_features(
    rows: Iterable[dict[str, Any]], metadata: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Aggregate hourly weather into deterministic comparable-day features. @zara"""
    rows_by_date = defaultdict(list)
    for row in rows:
        rows_by_date[str(row["date"])].append(row)
    day_features = []
    for date_key, day_rows in rows_by_date.items():
        ordered_rows = sorted(day_rows, key=lambda item: int(item.get("hour", 0)))
        daylight_rows = [
            row
            for row in ordered_rows
            if (_number(row.get("clear_sky_wm2"), minimum=0) or 0) >= 20
        ]
        valid_rows = [
            row
            for row in daylight_rows
            if _number(row.get("solar_actual_wm2"), minimum=0) is not None
        ]
        coverage_percent = (
            100 * len(valid_rows) / len(daylight_rows) if daylight_rows else 0
        )
        if (
            len(valid_rows) < MIN_DAYLIGHT_HOURS
            or coverage_percent < MIN_WEATHER_COVERAGE
        ):
            continue
        solar_profile = [None] * 24
        energy_profile = [None] * 24
        for row in ordered_rows:
            hour = int(row["hour"])
            solar_profile[hour] = _round(
                _number(row.get("solar_actual_wm2"), minimum=0), 1
            )
            energy_profile[hour] = _round(_number(row.get("actual"), minimum=0), 3)
        cloud_values = [
            _number(row.get("cloud_actual_percent"), minimum=0, maximum=100)
            for row in valid_rows
        ]
        temperature_values = [
            _number(row.get("temperature_actual_c"), minimum=-70, maximum=70)
            for row in valid_rows
        ]
        day_metadata = metadata.get(date_key, {})
        day_features.append(
            {
                "date": date_key,
                "radiation_kwh_m2": round(
                    sum(
                        (
                            _number(row.get("solar_actual_wm2"), minimum=0) or 0
                            for row in valid_rows
                        )
                    )
                    / 1000,
                    2,
                ),
                "cloud_mean_percent": (
                    _round(
                        fmean(
                            (
                                metric_value
                                for metric_value in cloud_values
                                if metric_value is not None
                            )
                        ),
                        1,
                    )
                    if any((metric_value is not None for metric_value in cloud_values))
                    else None
                ),
                "temperature_mean_c": (
                    _round(
                        fmean(
                            (
                                metric_value
                                for metric_value in temperature_values
                                if metric_value is not None
                            )
                        ),
                        1,
                    )
                    if any(
                        (
                            metric_value is not None
                            for metric_value in temperature_values
                        )
                    )
                    else None
                ),
                "daylight_hours": _round(
                    _number(valid_rows[0].get("daylight_hours"), minimum=0), 2
                ),
                "weather_coverage_percent": round(coverage_percent, 1),
                "solar_profile": solar_profile,
                "energy_profile": energy_profile,
                "yield_kwh": _round(
                    _number(day_metadata.get("yield_kwh"), minimum=0), 2
                ),
                "forecast_accuracy_percent": _round(
                    _number(
                        day_metadata.get("forecast_accuracy_percent"),
                        minimum=0,
                        maximum=100,
                    ),
                    1,
                ),
                "conditions_score": _round(
                    _number(
                        day_metadata.get("conditions_score"), minimum=0, maximum=100
                    ),
                    1,
                ),
            }
        )
    return day_features


def _profile_distance(left: list[Any], right: list[Any]) -> float:
    prediction_actual_pairs = [
        (float(left_value), float(right_value))
        for left_value, right_value in zip(left, right)
        if _number(left_value, minimum=0) is not None
        if _number(right_value, minimum=0) is not None
    ]
    if len(prediction_actual_pairs) < MIN_DAYLIGHT_HOURS:
        return 1.0
    return min(
        fmean(
            (
                abs(left_value - right_value)
                for left_value, right_value in prediction_actual_pairs
            )
        )
        / 500.0,
        1.0,
    )


def build_comparable_days(
    features: Iterable[dict[str, Any]], *, target_date: str
) -> dict[str, Any]:
    """Rank comparable days using transparent weighted normalized distance. @zara"""
    day_features = list(features)
    target_features = next(
        (features for features in day_features if features["date"] == target_date), None
    )
    candidate_features = [
        resolved_features
        for resolved_features in day_features
        if resolved_features["date"] != target_date
    ]
    if target_features is None:
        return {
            "available": False,
            "reason": "target_weather_incomplete",
            "target_date": target_date,
            "days": [],
        }
    if len(candidate_features) < MIN_COMPARABLE_CANDIDATES:
        return {
            "available": False,
            "reason": "insufficient_comparable_days",
            "target_date": target_date,
            "candidate_days": len(candidate_features),
            "minimum_candidate_days": MIN_COMPARABLE_CANDIDATES,
            "days": [],
        }

    def normalized(left: Any, right: Any, span: float) -> float:
        left_value = _number(left)
        right_value = _number(right)
        return (
            min(abs(left_value - right_value) / span, 1.0)
            if left_value is not None and right_value is not None
            else 1.0
        )

    ranked_days = []
    for candidate in candidate_features:
        distance = (
            normalized(
                target_features["radiation_kwh_m2"], candidate["radiation_kwh_m2"], 8.0
            )
            * 0.4
            + _profile_distance(
                target_features["solar_profile"], candidate["solar_profile"]
            )
            * 0.25
            + normalized(
                target_features["cloud_mean_percent"],
                candidate["cloud_mean_percent"],
                100.0,
            )
            * 0.15
            + normalized(
                target_features["temperature_mean_c"],
                candidate["temperature_mean_c"],
                30.0,
            )
            * 0.1
            + normalized(
                target_features["daylight_hours"], candidate["daylight_hours"], 8.0
            )
            * 0.1
        )
        ranked_days.append(
            {
                **candidate,
                "distance": round(distance, 4),
                "similarity_percent": round((1 - distance) * 100, 1),
            }
        )
    ranked_days.sort(key=lambda item: (item["distance"], item["date"]))
    return {
        "available": True,
        "target_date": target_date,
        "target": target_features,
        "candidate_days": len(candidate_features),
        "method": "weighted_normalized_distance",
        "weights": {
            "radiation_total": 40,
            "radiation_profile": 25,
            "clouds": 15,
            "temperature": 10,
            "daylight": 10,
        },
        "days": ranked_days[:5],
    }


def build_weather_energy_insights(
    story: dict[str, Any],
    production: dict[str, Any],
    conditions: dict[str, Any],
    *,
    monthly_rank: int | None,
    monthly_days: int,
) -> list[dict[str, Any]]:
    """Create evidence-backed weather and energy statements. @zara"""
    insights = []
    largest_deviation = story.get("largest_deviation")
    if largest_deviation:
        hour = int(largest_deviation["hour"])
        current_hour = story["hours"][hour]
        previous_hour = story["hours"][max(0, hour - 1)]
        current_radiation_wm2 = _number(current_hour.get("solar_actual_wm2"), minimum=0)
        previous_radiation_wm2 = _number(
            previous_hour.get("solar_actual_wm2"), minimum=0
        )
        weather_coincidence = (
            current_radiation_wm2 is not None
            and previous_radiation_wm2 is not None
            and (abs(current_radiation_wm2 - previous_radiation_wm2) >= 150)
        )
        insights.append(
            {
                "id": "largest_deviation",
                "severity": "warning",
                "hour": hour,
                "absolute_error_kwh": round(
                    float(largest_deviation["absolute_error_kwh"]), 2
                ),
                "radiation_change_wm2": (
                    round(current_radiation_wm2 - previous_radiation_wm2, 1)
                    if weather_coincidence
                    else None
                ),
                "temporal_weather_coincidence": weather_coincidence,
            }
        )
    peak_time = production.get("peak_time")
    if peak_time and production.get("peak_power_w") is not None:
        insights.append(
            {
                "id": "production_peak",
                "severity": "information",
                "time": peak_time,
                "power_w": production["peak_power_w"],
            }
        )
    if monthly_rank is not None and monthly_days >= 5 and (monthly_rank <= 5):
        insights.append(
            {
                "id": "monthly_yield_rank",
                "severity": "positive",
                "rank": monthly_rank,
                "days": monthly_days,
            }
        )
    missing_actual_hours = sum(
        (row["state"] == "forecast_without_actual" for row in story.get("hours", []))
    )
    if missing_actual_hours:
        insights.append(
            {
                "id": "missing_actual_hours",
                "severity": "warning",
                "hours": missing_actual_hours,
            }
        )
    if conditions.get("available"):
        insights.append(
            {
                "id": "conditions_context",
                "severity": "information",
                "score": conditions["score"],
                "class": conditions["class"],
            }
        )
    return insights[:4]
