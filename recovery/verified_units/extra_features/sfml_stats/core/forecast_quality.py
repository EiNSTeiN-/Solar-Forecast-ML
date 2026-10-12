# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Deterministic forecast-quality definitions for the modern analytics UI. @zara"""
from __future__ import annotations
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import fmean
from typing import Any, Mapping, Sequence

PRODUCTION_CANDIDATE_KWH_THRESHOLD = 0.01
MIN_VALID_DAY_HOURS = 4
MIN_VALID_DAY_COVERAGE = 40.0
MIN_HEALTH_DAYS = 7
MIN_MODEL_POINTS = 24
MIN_MODEL_DAYS = 3
MODEL_TIE_MAE_KWH = 0.01
VISIBLE_WEATHER_MISS_TYPES = frozenset(
    {"unexpected_rain", "unexpected_snow", "unexpected_clouds"}
)
HARD_WEATHER_EXCLUSION_TYPES = frozenset({"snow_covered_panels", "manual_pause"})


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _number(value: Any) -> float | None:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    return number if math.isfinite(number) else None


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return min(upper, max(lower, value))


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def daily_evaluation_hours(row: Any) -> int:
    numeric_value = _number(_value(row, "evaluation_hours_count"))
    if numeric_value is None or numeric_value <= 0:
        numeric_value = _number(_value(row, "hours_with_actual_data"))
    return max(0, int(numeric_value or 0))


def daily_candidate_hours(row: Any) -> int:
    numeric_value = _number(_value(row, "production_candidate_hours_count"))
    if numeric_value is None or numeric_value <= 0:
        numeric_value = _number(_value(row, "total_hours_predicted"))
    return max(0, int(numeric_value or 0))


def daily_coverage(row: Any) -> float | None:
    numeric_value = _number(_value(row, "evaluation_coverage_percent"))
    if numeric_value is not None and numeric_value > 0:
        return _clamp(numeric_value)
    candidate_hours = daily_candidate_hours(row)
    if candidate_hours <= 0:
        return
    return _clamp(daily_evaluation_hours(row) / candidate_hours * 100.0)


def is_valid_day(row: Any) -> bool:
    accuracy = _number(_value(row, "accuracy_percent"))
    coverage = daily_coverage(row)
    return bool(
        accuracy is not None
        and coverage is not None
        and (daily_evaluation_hours(row) >= MIN_VALID_DAY_HOURS)
        and (coverage >= MIN_VALID_DAY_COVERAGE)
    )


def is_valid_hour(row: Any) -> bool:
    forecast_kwh = _number(_value(row, "final", _value(row, "prediction_kwh")))
    actual_kwh = _number(_value(row, "actual", _value(row, "actual_kwh")))
    if forecast_kwh is None or actual_kwh is None:
        return False
    if (
        forecast_kwh <= PRODUCTION_CANDIDATE_KWH_THRESHOLD
        and actual_kwh <= PRODUCTION_CANDIDATE_KWH_THRESHOLD
    ):
        return False
    weather_alert = str(_value(row, "weather_alert_type", "") or "").strip().lower()
    if bool(_value(row, "mppt_throttled", False)):
        return False
    if bool(_value(row, "inverter_clipped", False)):
        return False
    if bool(_value(row, "exclude_from_learning", False)):
        return False
    if weather_alert in HARD_WEATHER_EXCLUSION_TYPES:
        return False
    if bool(_value(row, "exclude_from_clean_evaluation", False)):
        return weather_alert in VISIBLE_WEATHER_MISS_TYPES
    return True


def health_class(score: float | None) -> str | None:
    if score is None:
        return
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "moderate"
    if score >= 40:
        return "weak"
    return "critical"


def harmonic_score(accuracy: float, completeness: float) -> float:
    accuracy = _clamp(accuracy)
    completeness = _clamp(completeness)
    if accuracy <= 0 or completeness <= 0:
        return 0.0
    return 2.0 * accuracy * completeness / (accuracy + completeness)


def _daily_forecast_actual(row: Any) -> tuple[float | None, float | None]:
    forecast_kwh = _number(_value(row, "evaluation_predicted_kwh"))
    actual_kwh = _number(_value(row, "evaluation_actual_kwh"))
    if forecast_kwh is None or actual_kwh is None:
        forecast_kwh = _number(_value(row, "predicted_total_kwh"))
        actual_kwh = _number(_value(row, "actual_total_kwh"))
    return (forecast_kwh, actual_kwh)


def aggregate_daily_period(rows: Sequence[Any]) -> dict[str, Any]:
    valid_rows = [row for row in rows if is_valid_day(row)]
    if not valid_rows:
        return {
            "available": False,
            "reason": "insufficient_valid_days",
            "valid_days": 0,
            "calendar_days": len(rows),
        }
    hour_counts = [daily_evaluation_hours(row) for row in valid_rows]
    evaluation_hours = sum(hour_counts)
    accuracy = (
        sum(
            (
                float(_value(row, "accuracy_percent")) * hours
                for row, hours in zip(valid_rows, hour_counts, strict=True)
            )
        )
        / evaluation_hours
    )
    candidate_hours = sum((daily_candidate_hours(row) for row in valid_rows))
    if candidate_hours:
        completeness = evaluation_hours / candidate_hours * 100.0
    else:
        completeness = fmean((daily_coverage(row) or 0.0 for row in valid_rows))
    forecast_actual_pairs = [_daily_forecast_actual(row) for row in valid_rows]
    forecast_actual_pairs = [
        (forecast_kwh, actual_kwh)
        for forecast_kwh, actual_kwh in forecast_actual_pairs
        if forecast_kwh is not None
        if actual_kwh is not None
    ]
    absolute_error = sum(
        (
            abs(forecast_kwh - actual_kwh)
            for forecast_kwh, actual_kwh in forecast_actual_pairs
        )
    )
    bias_total = sum(
        (
            forecast_kwh - actual_kwh
            for forecast_kwh, actual_kwh in forecast_actual_pairs
        )
    )
    actual_total = sum(
        (actual_kwh for unused_forecast, actual_kwh in forecast_actual_pairs)
    )
    return (
        (
            (
                {
                    "available": len(valid_rows) >= MIN_HEALTH_DAYS,
                    "reason": (
                        None
                        if len(valid_rows) >= MIN_HEALTH_DAYS
                        else "insufficient_valid_days"
                    ),
                    "valid_days": len(valid_rows),
                    "calendar_days": len(rows),
                    "evaluation_hours": evaluation_hours,
                    "candidate_hours": candidate_hours,
                    "accuracy": _round(_clamp(accuracy)),
                    "completeness": _round(_clamp(completeness)),
                    "score": _round(harmonic_score(accuracy, completeness)),
                    "absolute_error_kwh": _round(absolute_error, 3),
                    "bias_kwh": (
                        _round(bias_total / len(forecast_actual_pairs), 3)
                        if forecast_actual_pairs
                        else None
                    ),
                    "bias_percent": (
                        _round(bias_total / actual_total * 100.0)
                        if actual_total > 0
                        else None
                    ),
                    "wape_percent": (
                        _round(absolute_error / actual_total * 100.0)
                        if actual_total > 0
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


def build_health_dashboard(
    rows: Sequence[Any],
    *,
    period_days: int,
    calculated_at: str,
    model_battle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ordered_rows = sorted(
        (row for row in rows if _value(row, "date")),
        key=lambda row: str(_value(row, "date")),
    )
    if not ordered_rows:
        return {
            "health": {"available": False, "reason": "no_daily_data"},
            "executive_summary": [],
            "insights": [],
            "calculated_at": calculated_at,
        }
    latest_date = date.fromisoformat(str(_value(ordered_rows[-1], "date")))
    period_start = latest_date - timedelta(days=period_days - 1)
    previous_start = period_start - timedelta(days=period_days)
    previous_end = period_start - timedelta(days=1)
    current_rows = [
        row
        for row in ordered_rows
        if period_start <= date.fromisoformat(str(_value(row, "date"))) <= latest_date
    ]
    previous_rows = [
        row
        for row in ordered_rows
        if previous_start
        <= date.fromisoformat(str(_value(row, "date")))
        <= previous_end
    ]
    current = aggregate_daily_period(current_rows)
    previous = aggregate_daily_period(previous_rows)
    if not current.get("available"):
        current["score"] = None
    score = current.get("score")
    previous_score = previous.get("score") if previous.get("available") else None
    change = (
        score - previous_score
        if score is not None and previous_score is not None
        else None
    )
    current["class"] = health_class(score)
    current["period"] = {
        "start": period_start.isoformat(),
        "end": latest_date.isoformat(),
    }
    current["calculated_at"] = calculated_at
    current["change"] = _round(change)
    current["previous_score"] = previous_score
    current["minimum_valid_days"] = MIN_HEALTH_DAYS
    current["formula"] = "harmonic_mean_accuracy_completeness"
    components = [
        {
            "id": "accuracy",
            "value": current.get("accuracy"),
            "weight": "equal_harmonic",
        },
        {
            "id": "completeness",
            "value": current.get("completeness"),
            "weight": "equal_harmonic",
        },
    ]
    current["components"] = components
    available_components = [
        component for component in components if component["value"] is not None
    ]
    if available_components:
        current["positive_influence"] = max(
            available_components, key=lambda item: item["value"]
        )["id"]
        current["negative_influence"] = min(
            available_components, key=lambda item: item["value"]
        )["id"]
    valid_current_rows = [row for row in current_rows if is_valid_day(row)]
    biggest_deviation = None
    for row in valid_current_rows:
        forecast_kwh, actual_kwh = _daily_forecast_actual(row)
        if forecast_kwh is None or actual_kwh is None:
            continue
        day_error = {
            "date": str(_value(row, "date")),
            "absolute_error_kwh": abs(forecast_kwh - actual_kwh),
            "forecast_kwh": forecast_kwh,
            "actual_kwh": actual_kwh,
        }
        if (
            biggest_deviation is None
            or day_error["absolute_error_kwh"] > biggest_deviation["absolute_error_kwh"]
        ):
            biggest_deviation = day_error
    executive_summary = []
    if current.get("available"):
        executive_summary.append(
            {"code": "health", "score": score, "class": current["class"]}
        )
    if change is not None and abs(change) >= 1:
        executive_summary.append(
            {
                "code": "trend",
                "change": _round(change),
                "direction": "up" if change > 0 else "down",
            }
        )
    if biggest_deviation is not None:
        executive_summary.append(
            {
                "code": "largest_day_error",
                "date": biggest_deviation["date"],
                "absolute_error_kwh": _round(
                    biggest_deviation["absolute_error_kwh"], 2
                ),
            }
        )
    bias_percent = current.get("bias_percent")
    if bias_percent is not None:
        executive_summary.append(
            {
                "code": "bias",
                "bias_percent": bias_percent,
                "direction": (
                    "over"
                    if bias_percent > 3
                    else "under" if bias_percent < -3 else "balanced"
                ),
            }
        )
    if model_battle and model_battle.get("available"):
        executive_summary.append(
            {
                "code": "model_winner",
                "winner": model_battle.get("winner"),
                "tie": model_battle.get("tie", False),
                "points": model_battle.get("common_points"),
            }
        )
    insights = _build_insights(current, change, biggest_deviation, model_battle)
    return {
        "health": current,
        "executive_summary": executive_summary[:5],
        "insights": insights,
        "calculated_at": calculated_at,
        "data_source": "sfml_database",
    }


def _build_insights(
    current: Mapping[str, Any],
    change: float | None,
    biggest: Mapping[str, Any] | None,
    model_battle: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    items = []
    health_classification = current.get("class")
    if health_classification in {"weak", "critical"}:
        items.append(
            {
                "id": "health_low",
                "severity": (
                    "critical" if health_classification == "critical" else "warning"
                ),
                "value": current.get("score"),
                "period": current.get("period"),
                "target": "overview",
            }
        )
    completeness = current.get("completeness")
    if completeness is not None and completeness < 75:
        items.append(
            {
                "id": "coverage_low",
                "severity": "warning",
                "value": completeness,
                "period": current.get("period"),
                "target": "calendar",
            }
        )
    if change is not None and abs(change) >= 3:
        items.append(
            {
                "id": "trend_change",
                "severity": "positive" if change > 0 else "warning",
                "value": _round(change),
                "direction": "up" if change > 0 else "down",
                "period": current.get("period"),
                "target": "trends",
            }
        )
    current_bias = current.get("bias_percent")
    if current_bias is not None and abs(current_bias) >= 10:
        items.append(
            {
                "id": "systematic_bias",
                "severity": "warning",
                "value": current_bias,
                "direction": "over" if current_bias > 0 else "under",
                "period": current.get("period"),
                "target": "trends",
            }
        )
    if model_battle and model_battle.get("available") and (not model_battle.get("tie")):
        items.append(
            {
                "id": "model_winner",
                "severity": "information",
                "winner": model_battle.get("winner"),
                "value": model_battle.get("winner_margin_mae_kwh"),
                "period": model_battle.get("period"),
                "target": "models",
            }
        )
    if (
        health_classification in {"good", "excellent"}
        and completeness is not None
        and (completeness >= 90)
    ):
        items.append(
            {
                "id": "quality_strong",
                "severity": "positive",
                "value": current.get("score"),
                "period": current.get("period"),
                "target": "overview",
            }
        )
    if biggest is not None:
        items.append(
            {
                "id": "largest_day_error",
                "severity": "information",
                "value": _round(float(biggest["absolute_error_kwh"]), 2),
                "date": biggest["date"],
                "period": current.get("period"),
                "target": "calendar",
            }
        )
    severity_priority = {"critical": 0, "warning": 1, "positive": 2, "information": 3}
    items.sort(key=lambda item: (severity_priority[item["severity"]], item["id"]))
    return items[:4]


def build_heatmap(rows: Sequence[Any], *, start: date, end: date) -> dict[str, Any]:
    rows_by_date = {
        str(_value(row, "date")): row for row in rows if _value(row, "date")
    }
    valid_rows = [row for row in rows if is_valid_day(row)]
    ranked_rows = sorted(
        valid_rows, key=lambda row: float(_value(row, "accuracy_percent")), reverse=True
    )
    ranks = {}
    previous_accuracy = None
    previous_rank = 0
    for ordinal_rank, row in enumerate(ranked_rows, 1):
        accuracy = float(_value(row, "accuracy_percent"))
        rank = (
            previous_rank
            if previous_accuracy is not None and accuracy == previous_accuracy
            else ordinal_rank
        )
        ranks[str(_value(row, "date"))] = rank
        previous_accuracy = accuracy
        previous_rank = rank
    days = []
    current_date = start
    while current_date <= end:
        key = current_date.isoformat()
        row = rows_by_date.get(key)
        if row is None:
            days.append({"date": key, "state": "missing"})
            current_date += timedelta(days=1)
            continue
        forecast_kwh, actual_kwh = _daily_forecast_actual(row)
        accuracy = _number(_value(row, "accuracy_percent"))
        coverage = daily_coverage(row)
        valid = is_valid_day(row)
        absolute_error = (
            abs(forecast_kwh - actual_kwh)
            if forecast_kwh is not None and actual_kwh is not None
            else None
        )
        relative_error = _number(_value(row, "error_percent"))
        if (
            relative_error is None
            and forecast_kwh not in (None, 0)
            and (actual_kwh is not None)
        ):
            relative_error = (actual_kwh - forecast_kwh) / forecast_kwh * 100.0
        days.append(
            {
                "date": key,
                "state": "valid" if valid else "insufficient",
                "quality": _round(accuracy),
                "class": health_class(accuracy),
                "forecast_kwh": _round(forecast_kwh, 2),
                "actual_kwh": _round(actual_kwh, 2),
                "absolute_error_kwh": _round(absolute_error, 2),
                "relative_error_percent": _round(relative_error),
                "evaluation_hours": daily_evaluation_hours(row),
                "candidate_hours": daily_candidate_hours(row),
                "completeness": _round(coverage),
                "rank": ranks.get(key),
                "rank_basis": len(valid_rows),
            }
        )
        current_date += timedelta(days=1)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "metric": "daily_summaries_accuracy_percent",
        "valid_days": len(valid_rows),
        "days": days,
    }


def _longest_streak(rows: Sequence[Any], predicate) -> dict[str, Any]:
    longest_streak = []
    current_streak = []
    previous_date = None
    for row in sorted(rows, key=lambda item: str(_value(item, "date"))):
        row_date = date.fromisoformat(str(_value(row, "date")))
        consecutive = (
            previous_date is not None and row_date == previous_date + timedelta(days=1)
        )
        if predicate(row):
            current_streak = current_streak + [row] if consecutive else [row]
            if len(current_streak) > len(longest_streak):
                longest_streak = list(current_streak)
        else:
            current_streak = []
        previous_date = row_date
    return (
        (
            (
                {
                    "length": len(longest_streak),
                    "start": (
                        str(_value(longest_streak[0], "date"))
                        if longest_streak
                        else None
                    ),
                    "end": (
                        str(_value(longest_streak[-1], "date"))
                        if longest_streak
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


def build_milestones(rows: Sequence[Any]) -> dict[str, Any]:
    valid_rows = [row for row in rows if is_valid_day(row)]
    milestones = []
    if valid_rows:
        best_day = max(
            valid_rows, key=lambda row: float(_value(row, "accuracy_percent"))
        )
        milestones.append(
            {
                "id": "best_day",
                "status": "achieved",
                "date": str(_value(best_day, "date")),
                "value": _round(_number(_value(best_day, "accuracy_percent"))),
                "basis": len(valid_rows),
            }
        )
    complete_streak = _longest_streak(
        rows, lambda row: is_valid_day(row) and (daily_coverage(row) or 0) >= 95
    )
    milestones.append(
        {
            "id": "complete_streak",
            "status": "achieved" if complete_streak["length"] else "pending",
            **complete_streak,
        }
    )
    accurate_streak = _longest_streak(
        rows,
        lambda row: is_valid_day(row) and float(_value(row, "accuracy_percent")) >= 90,
    )
    milestones.append(
        {
            "id": "accurate_streak",
            "status": "achieved" if accurate_streak["length"] else "pending",
            **accurate_streak,
        }
    )
    complete_days = sum(
        (1 for row in rows if is_valid_day(row) if (daily_coverage(row) or 0) >= 95)
    )
    for target in (30, 100, 365):
        milestones.append(
            {
                "id": f"complete_days_{target}",
                "status": "achieved" if complete_days >= target else "in_progress",
                "value": min(complete_days, target),
                "target": target,
                "total_complete_days": complete_days,
            }
        )
    rows_by_week = defaultdict(list)
    for row in valid_rows:
        row_date = date.fromisoformat(str(_value(row, "date")))
        iso_calendar = row_date.isocalendar()
        rows_by_week[iso_calendar.year, iso_calendar.week].append(row)
    weekly_summaries = {
        week_key: aggregate_daily_period(week_rows)
        for week_key, week_rows in rows_by_week.items()
        if len(week_rows) >= 5
    }
    if weekly_summaries:
        week_key, summary = max(
            weekly_summaries.items(), key=lambda item: item[1].get("accuracy") or -1
        )
        milestones.append(
            {
                "id": "best_week",
                "status": "achieved",
                "year": week_key[0],
                "week": week_key[1],
                "value": summary.get("accuracy"),
                "valid_days": summary.get("valid_days"),
                "basis": len(weekly_summaries),
            }
        )
    return {
        "milestones": milestones,
        "valid_days": len(valid_rows),
        "period_days": len(rows),
    }


def build_trends(rows: Sequence[Any]) -> dict[str, Any]:
    weekly_rows = defaultdict(list)
    for row in rows:
        if is_valid_day(row):
            row_date = date.fromisoformat(str(_value(row, "date")))
            iso_calendar = row_date.isocalendar()
            weekly_rows[iso_calendar.year, iso_calendar.week].append(row)
    points = []
    for (iso_year, iso_week), bucket_rows in sorted(weekly_rows.items()):
        if len(bucket_rows) < 3:
            continue
        summary = aggregate_daily_period(bucket_rows)
        week_start = date.fromisocalendar(iso_year, iso_week, 1)
        points.append(
            {
                "period": f"{iso_year}-W{iso_week:02d}",
                "start": week_start.isoformat(),
                "end": (week_start + timedelta(days=6)).isoformat(),
                "accuracy": summary.get("accuracy"),
                "completeness": summary.get("completeness"),
                "health_score": summary.get("score"),
                "mae_kwh": _round(
                    (summary.get("absolute_error_kwh") or 0)
                    / max(1, summary.get("valid_days", 0)),
                    3,
                ),
                "wape_percent": summary.get("wape_percent"),
                "bias_kwh": summary.get("bias_kwh"),
                "bias_percent": summary.get("bias_percent"),
                "valid_days": summary.get("valid_days"),
            }
        )
        continue
    return {"bucket": "iso_week", "points": points}


def calculate_model_battle(
    rows: Sequence[Mapping[str, Any]],
    *,
    period: Mapping[str, str] | None = None,
    mode: str = "morning",
) -> dict[str, Any]:
    model_names = ("final", "physics", "ai")
    eligible_rows = [
        row
        for row in rows
        if row.get("eligible")
        if _number(row.get("actual")) is not None
    ]
    eligible_models = [
        model_name
        for model_name in model_names
        if sum((_number(row.get(model_name)) is not None for row in eligible_rows))
        >= MIN_MODEL_POINTS
    ]
    common_rows = (
        [
            row
            for row in eligible_rows
            if all(
                (
                    _number(row.get(model_name)) is not None
                    for model_name in eligible_models
                )
            )
        ]
        if len(eligible_models) >= 2
        else []
    )
    common_dates = sorted({str(row.get("date")) for row in common_rows})
    if len(common_rows) < MIN_MODEL_POINTS or len(common_dates) < MIN_MODEL_DAYS:
        return {
            "available": False,
            "reason": "insufficient_common_points",
            "common_points": len(common_rows),
            "common_days": len(common_dates),
            "minimum_points": MIN_MODEL_POINTS,
            "minimum_days": MIN_MODEL_DAYS,
            "models": eligible_models,
            "mode": mode,
            "period": period,
        }
    model_metrics = {}
    hour_wins = {model_name: 0 for model_name in eligible_models}
    errors_by_day = defaultdict(lambda: defaultdict(list))
    for row in common_rows:
        actual_kwh = float(row["actual"])
        errors = {
            model_name: float(row[model_name]) - actual_kwh
            for model_name in eligible_models
        }
        absolute_errors = {
            model_name: abs(error) for model_name, error in errors.items()
        }
        best_error = min(absolute_errors.values())
        winners = [
            model_name
            for model_name, absolute_error in absolute_errors.items()
            if abs(absolute_error - best_error) < 1e-09
        ]
        if len(winners) == 1:
            hour_wins[winners[0]] += 1
        for model_name, error in errors.items():
            errors_by_day[str(row["date"])][model_name].append(abs(error))
        continue
    day_wins = {model_name: 0 for model_name in eligible_models}
    tied_days = 0
    daily_results = []
    for day, daily_errors in sorted(errors_by_day.items()):
        daily_mae = {
            model_name: fmean(daily_errors[model_name])
            for model_name in eligible_models
        }
        best_error = min(daily_mae.values())
        winners = [
            model_name
            for model_name, absolute_error in daily_mae.items()
            if abs(absolute_error - best_error) < 1e-09
        ]
        if len(winners) == 1:
            day_wins[winners[0]] += 1
        else:
            tied_days += 1
        daily_results.append(
            {
                "date": day,
                "winner": winners[0] if len(winners) == 1 else None,
                "tie": len(winners) > 1,
                "mae_kwh": {
                    model_name: _round(absolute_error, 3)
                    for model_name, absolute_error in daily_mae.items()
                },
                "points": len(next(iter(daily_errors.values()))),
            }
        )
        continue
    actual_total = sum((float(row["actual"]) for row in common_rows))
    for model_name in eligible_models:
        errors = [float(row[model_name]) - float(row["actual"]) for row in common_rows]
        absolute_errors = [abs(error) for error in errors]
        model_metrics[model_name] = {
            "mae_kwh": _round(fmean(absolute_errors), 3),
            "rmse_kwh": _round(
                math.sqrt(fmean((error * error for error in errors))), 3
            ),
            "bias_kwh": _round(fmean(errors), 3),
            "wape_percent": (
                _round(sum(absolute_errors) / actual_total * 100.0)
                if actual_total > 0
                else None
            ),
            "overforecast_share": _round(
                sum((error > 0 for error in errors)) / len(errors) * 100.0
            ),
            "underforecast_share": _round(
                sum((error < 0 for error in errors)) / len(errors) * 100.0
            ),
            "hour_wins": hour_wins[model_name],
            "day_wins": day_wins[model_name],
        }
        continue
    ranking = sorted(eligible_models, key=lambda key: model_metrics[key]["mae_kwh"])
    winner_margin = (
        model_metrics[ranking[1]]["mae_kwh"] - model_metrics[ranking[0]]["mae_kwh"]
    )
    tie = winner_margin < MODEL_TIE_MAE_KWH
    return {
        "available": True,
        "mode": mode,
        "method": "lowest_mae_on_common_valid_hours",
        "winner": None if tie else ranking[0],
        "tie": tie,
        "winner_margin_mae_kwh": _round(winner_margin, 3),
        "ranking": ranking,
        "metrics": model_metrics,
        "common_points": len(common_rows),
        "common_days": len(common_dates),
        "excluded_points": len(rows) - len(common_rows),
        "tied_days": tied_days,
        "daily_results": daily_results,
        "period": period,
        "minimum_points": MIN_MODEL_POINTS,
        "minimum_days": MIN_MODEL_DAYS,
    }


def build_replay(
    rows: Sequence[Mapping[str, Any]], *, replay_date: str
) -> dict[str, Any]:
    totals = {"actual": 0.0, "final": 0.0, "physics": 0.0, "ai": 0.0, "reforecast": 0.0}
    replay_rows = []
    biggest_deviation = None
    for input_row in sorted(rows, key=lambda item: int(item["hour"])):
        replay_row = dict(input_row)
        actual_kwh = _number(replay_row.get("actual"))
        for series_name in totals:
            series_value = (
                actual_kwh
                if series_name == "actual"
                else _number(replay_row.get(series_name))
            )
            if series_value is not None:
                totals[series_name] += series_value
            replay_row[f"cumulative_{series_name}_kwh"] = _round(totals[series_name], 3)
        final_forecast = _number(replay_row.get("final"))
        eligible = bool(replay_row.get("eligible"))
        absolute_error = (
            abs(final_forecast - actual_kwh)
            if eligible and final_forecast is not None and (actual_kwh is not None)
            else None
        )
        replay_row["absolute_error_kwh"] = _round(absolute_error, 3)
        replay_row["cumulative_error_kwh"] = _round(
            totals["final"] - totals["actual"], 3
        )
        if absolute_error is not None:
            if (
                biggest_deviation is None
                or absolute_error > biggest_deviation["absolute_error_kwh"]
            ):
                biggest_deviation = {
                    "hour": int(replay_row["hour"]),
                    "absolute_error_kwh": absolute_error,
                }
        replay_rows.append(replay_row)
        continue
    series_available = {
        series_name: any(
            (_number(row.get(series_name)) is not None for row in replay_rows)
        )
        for series_name in ("actual", "final", "physics", "ai", "reforecast")
    }
    series_available["p10"] = False
    return {
        "date": replay_date,
        "hours": replay_rows,
        "series_available": series_available,
        "biggest_deviation": (
            {
                "hour": biggest_deviation["hour"],
                "absolute_error_kwh": _round(
                    biggest_deviation["absolute_error_kwh"], 3
                ),
            }
            if biggest_deviation
            else None
        ),
        "p10": {"available": False, "reason": "no_historical_hourly_series"},
    }
