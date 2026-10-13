# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Hubble summary helpers (Home / Summary dashboard)."""
from __future__ import annotations
from datetime import date, datetime
from typing import Any
import aiosqlite
from ..const import (
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_SOLAR_TO_BATTERY,
)
from . import context
from .common import PRODUCTION_CANDIDATE_KWH_THRESHOLD

HUBBLE_REFRESH_SECONDS = 60
HUBBLE_MIDDAY_MIN_EVALUATED_HOURS = 4
HUBBLE_MIDDAY_NOT_BEFORE_HOUR = 11
HUBBLE_EVENING_AFTER_SUNSET_MINUTES = 45
HUBBLE_MIN_LOAD_WINDOW_HOUR_KWH = 0.5
HUBBLE_LOAD_WINDOW_SUNSET_BUFFER_MINUTES = 60
HUBBLE_MEMORY_LOOKBACK_DAYS = 120
HUBBLE_MEMORY_MIN_MATCHES = 2
HUBBLE_BATTERY_CONFIG_KEYS = (
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
)


def _hubble_time_to_minutes(value: str | None) -> int | None:
    if not value or ":" not in value:
        return
    try:
        hour, minute = value[:5].split(":")
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return


def _hubble_round(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return


def _hubble_read_workflow_status() -> dict[str, Any] | None:
    """Read the SFML workflow status sensor from Home Assistant state."""
    try:
        return _hubble_read_workflow_status_inner()
    except Exception:
        return


def _hubble_read_workflow_status_inner() -> dict[str, Any] | None:
    if context.HASS is None:
        return
    candidate_states = [
        context.HASS.states.get("sensor.solar_forecast_ml_workflow_status"),
        context.HASS.states.get("sensor.workflow_status"),
    ]
    try:
        candidate_states.extend(
            [
                state
                for state in context.HASS.states.async_all("sensor")
                if "workflow_status" in state.entity_id.lower()
                or (
                    state.attributes.get("workflow") is not None
                    and (
                        "solar_forecast" in state.entity_id.lower()
                        or "sfml" in state.entity_id.lower()
                    )
                )
            ]
        )
    except Exception:
        pass
    for state in candidate_states:
        if state is None:
            continue
        attributes = state.attributes or {}
        workflow = attributes.get("workflow") or state.state
        if not workflow or str(workflow).lower() in {"unknown", "unavailable"}:
            continue
        is_running = attributes.get("is_running")
        if isinstance(is_running, str):
            is_running = is_running.lower() in {"1", "on", "yes", "true"}
        return {
            "entity_id": state.entity_id,
            "is_running": bool(is_running)
            or str(state.state).lower() not in {"off", "idle"},
            "workflow": str(workflow),
            "state": state.state,
            "status": attributes.get("status"),
            "current_step": attributes.get("current_step"),
            "progress": attributes.get("progress"),
            "steps_completed": attributes.get("steps_completed"),
            "total_steps": attributes.get("total_steps"),
            "started_at": attributes.get("started_at"),
            "completed_at": attributes.get("completed_at"),
            "duration_seconds": _hubble_round(attributes.get("duration_seconds"), 1),
            "last_error": attributes.get("last_error"),
        }


def _hubble_has_positive_value(value: Any, threshold: float = 0.01) -> bool:
    try:
        return abs(float(value or 0.0)) > threshold
    except (TypeError, ValueError):
        return False


def _hubble_has_battery(
    config: dict[str, Any],
    daily_stats: dict[str, Any],
    avg_battery_charge: float | None,
) -> bool:
    if any((config.get(battery_key) for battery_key in HUBBLE_BATTERY_CONFIG_KEYS)):
        return True
    if any(
        (
            _hubble_has_positive_value(daily_stats.get(energy_key))
            for energy_key in (
                "solar_to_battery_kwh",
                "battery_to_house_kwh",
                "battery_to_grid_kwh",
                "grid_to_battery_kwh",
                "battery_charge_solar_kwh",
                "battery_charge_grid_kwh",
                "battery_discharge_kwh",
            )
        )
    ):
        return True
    return _hubble_has_positive_value(avg_battery_charge)


def _hubble_best_window(
    rows: list[dict[str, Any]], start_hour: int, sunset_minutes: int | None = None
) -> dict[str, Any] | None:
    latest_window_hour = None
    if sunset_minutes is not None:
        latest_window_hour = (
            max(0, sunset_minutes - HUBBLE_LOAD_WINDOW_SUNSET_BUFFER_MINUTES) // 60
        )
    candidate_rows = []
    for row in rows:
        hour = int(row.get("target_hour") or -1)
        prediction_kwh = float(row.get("prediction_kwh") or 0.0)
        if hour < start_hour:
            continue
        if latest_window_hour is not None and hour > latest_window_hour:
            continue
        if not prediction_kwh >= HUBBLE_MIN_LOAD_WINDOW_HOUR_KWH:
            continue
        candidate_rows.append(row)
    if not candidate_rows:
        return
    peak_kwh = max((float(row.get("prediction_kwh") or 0.0) for row in candidate_rows))
    threshold_kwh = max(HUBBLE_MIN_LOAD_WINDOW_HOUR_KWH, peak_kwh * 0.5)
    segments = []
    current_segment = []
    previous_hour = None
    for row in candidate_rows:
        hour = int(row.get("target_hour") or 0)
        prediction_kwh = float(row.get("prediction_kwh") or 0.0)
        if prediction_kwh >= threshold_kwh and (
            previous_hour is None or hour == previous_hour + 1
        ):
            current_segment.append(row)
        else:
            if current_segment:
                segments.append(current_segment)
            current_segment = [row] if prediction_kwh >= threshold_kwh else []
        previous_hour = hour
    if current_segment:
        segments.append(current_segment)
    if not segments:
        return
    best_segment = max(
        segments,
        key=lambda segment: sum(
            (float(row.get("prediction_kwh") or 0.0) for row in segment)
        ),
    )
    return {
        "start_hour": int(best_segment[0].get("target_hour") or start_hour),
        "end_hour": int(best_segment[-1].get("target_hour") or start_hour),
        "forecast_kwh": round(
            sum((float(row.get("prediction_kwh") or 0.0) for row in best_segment)), 2
        ),
    }


def _hubble_average(values: list[float]) -> float | None:
    available_values = [value for value in values if value is not None]
    if not available_values:
        return
    return sum(available_values) / len(available_values)


def _hubble_peak_hour(rows: list[dict[str, Any]]) -> int | None:
    peak_row = max(
        rows, key=lambda row: float(row.get("prediction_kwh") or 0.0), default=None
    )
    if not peak_row:
        return
    try:
        return int(peak_row.get("target_hour") or 0)
    except (TypeError, ValueError):
        return


def _hubble_recommendation_start_hour(now: datetime) -> int:
    return min(23, now.hour + 1) if now.minute >= 15 else now.hour


def _hubble_score_part(
    a: float | None, b: float | None, scale: float, weight: float
) -> float:
    if a is None or b is None or scale <= 0:
        return 0.0
    return min(abs(a - b) / scale, 1.0) * weight


async def _hubble_build_memory(
    db: aiosqlite.Connection,
    today: date,
    hourly_rows: list[dict[str, Any]],
    daily_stats: dict[str, Any],
    today_metrics: dict[str, Any],
    hourly_columns: set[str],
) -> dict[str, Any] | None:
    today_iso = today.isoformat()
    forecast_today_kwh = sum(
        (float(row.get("prediction_kwh") or 0.0) for row in hourly_rows)
    )
    actual_today_kwh = daily_stats.get("solar_yield_kwh")
    if actual_today_kwh is None:
        actual_today_kwh = today_metrics.get("evaluation_actual_kwh")
    today_metrics_summary = {
        "forecast_kwh": forecast_today_kwh if forecast_today_kwh > 0.2 else None,
        "yield_kwh": _hubble_round(actual_today_kwh, 2),
        "autarky_percent": _hubble_round(daily_stats.get("autarkie_percent"), 1),
        "home_consumption_kwh": _hubble_round(
            daily_stats.get("home_consumption_kwh"), 2
        ),
        "cloud_avg": _hubble_average(
            [
                float(row.get("clouds"))
                for row in hourly_rows
                if row.get("clouds") is not None
            ]
        ),
        "peak_hour": _hubble_peak_hour(hourly_rows),
    }
    if (
        today_metrics_summary["forecast_kwh"] is None
        and today_metrics_summary["yield_kwh"] is None
    ):
        return
    cloud_column_expression = (
        "hp.clouds" if "clouds" in hourly_columns else "NULL AS clouds"
    )
    async with db.execute(
        "SELECT hp.target_date,\n                  hp.target_hour,\n                  hp.prediction_kwh,\n                  hp.actual_kwh,\n                  "
        + cloud_column_expression
        + ",\n                  s.solar_yield_kwh,\n                  s.autarkie_percent,\n                  s.home_consumption_kwh\n           FROM hourly_predictions hp\n           LEFT JOIN stats_daily_energy s ON s.date = hp.target_date\n           WHERE hp.target_date < ?\n             AND hp.target_date >= date(?, '-' || ? || ' days')\n           ORDER BY hp.target_date, hp.target_hour",
        (today_iso, today_iso, HUBBLE_MEMORY_LOOKBACK_DAYS),
    ) as cursor:
        historical_rows = [dict(row) for row in await cursor.fetchall()]
    rows_by_date = {}
    for row in historical_rows:
        date_key = row.get("target_date")
        if date_key:
            rows_by_date.setdefault(str(date_key), []).append(row)
    matches = []
    for date_key, day_rows in rows_by_date.items():
        hourly_forecasts_kwh = [
            float(row.get("prediction_kwh") or 0.0) for row in day_rows
        ]
        hourly_actuals_kwh = [
            float(row.get("actual_kwh") or 0.0)
            for row in day_rows
            if row.get("actual_kwh") is not None
        ]
        first_day_row = day_rows[0]
        daily_forecast_kwh = sum(hourly_forecasts_kwh)
        daily_yield_kwh = first_day_row.get("solar_yield_kwh")
        if daily_yield_kwh is None and hourly_actuals_kwh:
            daily_yield_kwh = sum(hourly_actuals_kwh)
        if (
            daily_forecast_kwh <= 0.2
            or daily_yield_kwh is None
            or float(daily_yield_kwh or 0.0) <= 0.2
        ):
            continue
        cloud_samples = [
            float(row.get("clouds"))
            for row in day_rows
            if row.get("clouds") is not None
        ]
        load_window = _hubble_best_window(day_rows, 8)
        day_metrics = {
            "date": date_key,
            "forecast_kwh": daily_forecast_kwh,
            "yield_kwh": float(daily_yield_kwh or 0.0),
            "autarky_percent": _hubble_round(first_day_row.get("autarkie_percent"), 1),
            "home_consumption_kwh": _hubble_round(
                first_day_row.get("home_consumption_kwh"), 2
            ),
            "cloud_avg": _hubble_average(cloud_samples),
            "peak_hour": _hubble_peak_hour(day_rows),
            "window": load_window,
        }
        distance_score = 0.0
        distance_score += _hubble_score_part(
            today_metrics_summary["forecast_kwh"],
            day_metrics["forecast_kwh"],
            max(today_metrics_summary["forecast_kwh"] or 5.0, 5.0),
            1.4,
        )
        distance_score += _hubble_score_part(
            today_metrics_summary["yield_kwh"],
            day_metrics["yield_kwh"],
            max(today_metrics_summary["yield_kwh"] or 5.0, 5.0),
            1.2,
        )
        distance_score += _hubble_score_part(
            today_metrics_summary["cloud_avg"], day_metrics["cloud_avg"], 100.0, 0.8
        )
        distance_score += _hubble_score_part(
            today_metrics_summary["autarky_percent"],
            day_metrics["autarky_percent"],
            100.0,
            0.6,
        )
        distance_score += _hubble_score_part(
            today_metrics_summary["home_consumption_kwh"],
            day_metrics["home_consumption_kwh"],
            max(today_metrics_summary["home_consumption_kwh"] or 5.0, 5.0),
            0.5,
        )
        distance_score += _hubble_score_part(
            today_metrics_summary["peak_hour"], day_metrics["peak_hour"], 8.0, 0.5
        )
        similarity_percent = max(0.0, min(100.0, 100.0 - distance_score / 5.0 * 100.0))
        if similarity_percent < 45.0:
            continue
        matches.append(
            {
                "date": date_key,
                "similarity_percent": round(similarity_percent, 0),
                "yield_kwh": _hubble_round(day_metrics["yield_kwh"], 1),
                "forecast_kwh": _hubble_round(day_metrics["forecast_kwh"], 1),
                "autarky_percent": day_metrics["autarky_percent"],
                "peak_hour": day_metrics["peak_hour"],
                "window": load_window,
            }
        )
        continue
    matches.sort(key=lambda item: item["similarity_percent"], reverse=True)
    top_matches = matches[:3]
    if len(top_matches) < HUBBLE_MEMORY_MIN_MATCHES:
        return
    window_start_hours = [
        int(match["window"].get("start_hour"))
        for match in top_matches
        if match.get("window")
        if match["window"].get("start_hour") is not None
    ]
    common_window = None
    if window_start_hours:
        mean_start_hour = round(sum(window_start_hours) / len(window_start_hours))
        common_window = {
            "start_hour": mean_start_hour,
            "end_hour": min(mean_start_hour + 2, 23),
        }
    mean_yield_kwh = _hubble_average(
        [
            match["yield_kwh"]
            for match in top_matches
            if match.get("yield_kwh") is not None
        ]
    )
    mean_forecast_kwh = _hubble_average(
        [
            match["forecast_kwh"]
            for match in top_matches
            if match.get("forecast_kwh") is not None
        ]
    )
    mean_similarity_percent = _hubble_average(
        [match["similarity_percent"] for match in top_matches]
    )
    mean_autarky_percent = _hubble_average(
        [
            match["autarky_percent"]
            for match in top_matches
            if match.get("autarky_percent") is not None
        ]
    )
    return {
        "available": True,
        "lookback_days": HUBBLE_MEMORY_LOOKBACK_DAYS,
        "match_count": len(top_matches),
        "matches": top_matches,
        "avg_similarity_percent": _hubble_round(mean_similarity_percent, 0),
        "avg_yield_kwh": _hubble_round(mean_yield_kwh, 1),
        "avg_forecast_kwh": _hubble_round(mean_forecast_kwh, 1),
        "avg_autarky_percent": _hubble_round(mean_autarky_percent, 0),
        "common_window": common_window,
    }


def _hubble_decline_hour(
    rows: list[dict[str, Any]], best_window: dict[str, Any] | None
) -> int | None:
    if not best_window:
        return
    window_end_hour = int(best_window.get("end_hour") or 0)
    window_rows = [
        row
        for row in rows
        if int(row.get("target_hour") or -1) >= int(best_window.get("start_hour") or 0)
        if int(row.get("target_hour") or -1) <= window_end_hour
    ]
    if not window_rows:
        return
    mean_window_kwh = sum(
        (float(row.get("prediction_kwh") or 0.0) for row in window_rows)
    ) / len(window_rows)
    threshold_kwh = max(0.25, mean_window_kwh * 0.55)
    for row in rows:
        hour = int(row.get("target_hour") or -1)
        if hour <= window_end_hour:
            continue
        if not float(row.get("prediction_kwh") or 0.0) <= threshold_kwh:
            continue
        return hour


def _hubble_resolve_moment(
    now: datetime,
    sunset_minutes: int | None,
    forecast_today_kwh: float,
    evaluated_hours: int,
) -> tuple[str, dict[str, Any]]:
    now_minutes = now.hour * 60 + now.minute
    morning_ready = forecast_today_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
    midday_ready = (
        morning_ready
        and now.hour >= HUBBLE_MIDDAY_NOT_BEFORE_HOUR
        and (evaluated_hours >= HUBBLE_MIDDAY_MIN_EVALUATED_HOURS)
    )
    midday_time_ready = now.hour >= HUBBLE_MIDDAY_NOT_BEFORE_HOUR
    evening_ready = (
        sunset_minutes is not None
        and now_minutes >= sunset_minutes + HUBBLE_EVENING_AFTER_SUNSET_MINUTES
    )
    if evening_ready:
        moment = "evening"
        next_moment = None
    elif midday_time_ready:
        moment = "midday"
        next_moment = "evening"
    else:
        moment = "morning"
        next_moment = "midday"
    return (
        moment,
        {
            "refresh_seconds": HUBBLE_REFRESH_SECONDS,
            "morning_ready": morning_ready,
            "midday_ready": midday_ready,
            "midday_time_ready": midday_time_ready,
            "evening_ready": evening_ready,
            "evaluated_hours": evaluated_hours,
            "midday_min_evaluated_hours": HUBBLE_MIDDAY_MIN_EVALUATED_HOURS,
            "midday_not_before_hour": HUBBLE_MIDDAY_NOT_BEFORE_HOUR,
            "evening_after_sunset_minutes": HUBBLE_EVENING_AFTER_SUNSET_MINUTES,
            "next_moment": next_moment,
        },
    )
