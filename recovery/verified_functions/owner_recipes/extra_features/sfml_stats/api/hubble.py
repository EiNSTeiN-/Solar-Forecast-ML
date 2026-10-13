# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


def _hubble_read_workflow_status_inner() -> dict[str, Any] | None:
    if context.HASS is None:
        return
    _var_var_2 = [
        context.HASS.states.get("sensor.solar_forecast_ml_workflow_status"),
        context.HASS.states.get("sensor.workflow_status"),
    ]
    try:
        _var_var_2.extend(
            [
                _var_var_3
                for _var_var_3 in context.HASS.states.async_all("sensor")
                if "workflow_status" in _var_var_3.entity_id.lower()
                or (
                    _var_var_3.attributes.get("workflow") is not None
                    and (
                        "solar_forecast" in _var_var_3.entity_id.lower()
                        or "sfml" in _var_var_3.entity_id.lower()
                    )
                )
            ]
        )
    except Exception:
        pass
    for _var_var_3 in _var_var_2:
        if _var_var_3 is None:
            continue
        _var_var_4 = _var_var_3.attributes or {}
        _var_var_5 = _var_var_4.get("workflow") or _var_var_3.state
        if not _var_var_5 or str(_var_var_5).lower() in {"unknown", "unavailable"}:
            continue
        _var_var_6 = _var_var_4.get("is_running")
        if isinstance(_var_var_6, str):
            _var_var_6 = _var_var_6.lower() in {"1", "on", "yes", "true"}
        return {
            "entity_id": _var_var_3.entity_id,
            "is_running": bool(_var_var_6)
            or str(_var_var_3.state).lower() not in {"off", "idle"},
            "workflow": str(_var_var_5),
            "state": _var_var_3.state,
            "status": _var_var_4.get("status"),
            "current_step": _var_var_4.get("current_step"),
            "progress": _var_var_4.get("progress"),
            "steps_completed": _var_var_4.get("steps_completed"),
            "total_steps": _var_var_4.get("total_steps"),
            "started_at": _var_var_4.get("started_at"),
            "completed_at": _var_var_4.get("completed_at"),
            "duration_seconds": _hubble_round(_var_var_4.get("duration_seconds"), 1),
            "last_error": _var_var_4.get("last_error"),
        }


def _hubble_best_window(
    rows: list[dict[str, Any]], start_hour: int, sunset_minutes: int | None = None
) -> dict[str, Any] | None:
    _var_var_8 = None
    if sunset_minutes is not None:
        _var_var_8 = (
            max(0, sunset_minutes - HUBBLE_LOAD_WINDOW_SUNSET_BUFFER_MINUTES) // 60
        )
    _var_var_9 = []
    for _var_var_10 in rows:
        _var_var_0 = int(_var_var_10.get("target_hour") or -1)
        _var_var_11 = float(_var_var_10.get("prediction_kwh") or 0.0)
        if _var_var_0 < start_hour:
            continue
        if _var_var_8 is not None and _var_var_0 > _var_var_8:
            continue
        if not _var_var_11 >= HUBBLE_MIN_LOAD_WINDOW_HOUR_KWH:
            continue
        _var_var_9.append(_var_var_10)
    if not _var_var_9:
        return
    _var_var_12 = max(
        (float(_var_var_10.get("prediction_kwh") or 0.0) for _var_var_10 in _var_var_9)
    )
    _var_var_13 = max(HUBBLE_MIN_LOAD_WINDOW_HOUR_KWH, _var_var_12 * 0.5)
    _var_var_14 = []
    _var_var_15 = []
    _var_var_16 = None
    for _var_var_10 in _var_var_9:
        _var_var_0 = int(_var_var_10.get("target_hour") or 0)
        _var_var_11 = float(_var_var_10.get("prediction_kwh") or 0.0)
        if _var_var_11 >= _var_var_13 and (
            _var_var_16 is None or _var_var_0 == _var_var_16 + 1
        ):
            _var_var_15.append(_var_var_10)
        else:
            if _var_var_15:
                _var_var_14.append(_var_var_15)
            _var_var_15 = [_var_var_10] if _var_var_11 >= _var_var_13 else []
        _var_var_16 = _var_var_0
    if _var_var_15:
        _var_var_14.append(_var_var_15)
    if not _var_var_14:
        return
    _var_var_17 = max(
        _var_var_14,
        key=lambda segment: sum(
            (float(_var_var_10.get("prediction_kwh") or 0.0) for _var_var_10 in segment)
        ),
    )
    return {
        "start_hour": int(_var_var_17[0].get("target_hour") or start_hour),
        "end_hour": int(_var_var_17[-1].get("target_hour") or start_hour),
        "forecast_kwh": round(
            sum(
                (
                    float(_var_var_10.get("prediction_kwh") or 0.0)
                    for _var_var_10 in _var_var_17
                )
            ),
            2,
        ),
    }


async def _hubble_build_memory(
    db: aiosqlite.Connection,
    today: date,
    hourly_rows: list[dict[str, Any]],
    daily_stats: dict[str, Any],
    today_metrics: dict[str, Any],
    hourly_columns: set[str],
) -> dict[str, Any] | None:
    _var_var_21 = today.isoformat()
    _var_var_22 = sum(
        (float(_var_var_10.get("prediction_kwh") or 0.0) for _var_var_10 in hourly_rows)
    )
    _var_var_23 = daily_stats.get("solar_yield_kwh")
    if _var_var_23 is None:
        _var_var_23 = today_metrics.get("evaluation_actual_kwh")
    _var_var_15 = {
        "forecast_kwh": _var_var_22 if _var_var_22 > 0.2 else None,
        "yield_kwh": _hubble_round(_var_var_23, 2),
        "autarky_percent": _hubble_round(daily_stats.get("autarkie_percent"), 1),
        "home_consumption_kwh": _hubble_round(
            daily_stats.get("home_consumption_kwh"), 2
        ),
        "cloud_avg": _hubble_average(
            [
                float(_var_var_10.get("clouds"))
                for _var_var_10 in hourly_rows
                if _var_var_10.get("clouds") is not None
            ]
        ),
        "peak_hour": _hubble_peak_hour(hourly_rows),
    }
    if _var_var_15["forecast_kwh"] is None and _var_var_15["yield_kwh"] is None:
        return
    _var_var_24 = "hp.clouds" if "clouds" in hourly_columns else "NULL AS clouds"
    async with db.execute(
        "SELECT hp.target_date,\n                  hp.target_hour,\n                  hp.prediction_kwh,\n                  hp.actual_kwh,\n                  "
        + _var_var_24
        + ",\n                  s.solar_yield_kwh,\n                  s.autarkie_percent,\n                  s.home_consumption_kwh\n           FROM hourly_predictions hp\n           LEFT JOIN stats_daily_energy s ON s.date = hp.target_date\n           WHERE hp.target_date < ?\n             AND hp.target_date >= date(?, '-' || ? || ' days')\n           ORDER BY hp.target_date, hp.target_hour",
        (_var_var_21, _var_var_21, HUBBLE_MEMORY_LOOKBACK_DAYS),
    ) as _var_var_25:
        _var_var_26 = [
            dict(_var_var_10) for _var_var_10 in await _var_var_25.fetchall()
        ]
    _var_var_27 = {}
    for _var_var_10 in _var_var_26:
        _var_var_28 = _var_var_10.get("target_date")
        if _var_var_28:
            _var_var_27.setdefault(str(_var_var_28), []).append(_var_var_10)
    _var_var_29 = []
    for _var_var_28, _var_var_30 in _var_var_27.items():
        _var_var_31 = [
            float(_var_var_10.get("prediction_kwh") or 0.0)
            for _var_var_10 in _var_var_30
        ]
        _var_var_32 = [
            float(_var_var_10.get("actual_kwh") or 0.0)
            for _var_var_10 in _var_var_30
            if _var_var_10.get("actual_kwh") is not None
        ]
        _var_var_33 = _var_var_30[0]
        _var_var_34 = sum(_var_var_31)
        _var_var_35 = _var_var_33.get("solar_yield_kwh")
        if _var_var_35 is None and _var_var_32:
            _var_var_35 = sum(_var_var_32)
        if (
            _var_var_34 <= 0.2
            or _var_var_35 is None
            or float(_var_var_35 or 0.0) <= 0.2
        ):
            continue
        _var_var_36 = [
            float(_var_var_10.get("clouds"))
            for _var_var_10 in _var_var_30
            if _var_var_10.get("clouds") is not None
        ]
        _var_var_37 = _hubble_best_window(_var_var_30, 8)
        _var_var_38 = {
            "date": _var_var_28,
            "forecast_kwh": _var_var_34,
            "yield_kwh": float(_var_var_35 or 0.0),
            "autarky_percent": _hubble_round(_var_var_33.get("autarkie_percent"), 1),
            "home_consumption_kwh": _hubble_round(
                _var_var_33.get("home_consumption_kwh"), 2
            ),
            "cloud_avg": _hubble_average(_var_var_36),
            "peak_hour": _hubble_peak_hour(_var_var_30),
            "window": _var_var_37,
        }
        _var_var_39 = 0.0
        _var_var_39 += _hubble_score_part(
            _var_var_15["forecast_kwh"],
            _var_var_38["forecast_kwh"],
            max(_var_var_15["forecast_kwh"] or 5.0, 5.0),
            1.4,
        )
        _var_var_39 += _hubble_score_part(
            _var_var_15["yield_kwh"],
            _var_var_38["yield_kwh"],
            max(_var_var_15["yield_kwh"] or 5.0, 5.0),
            1.2,
        )
        _var_var_39 += _hubble_score_part(
            _var_var_15["cloud_avg"], _var_var_38["cloud_avg"], 100.0, 0.8
        )
        _var_var_39 += _hubble_score_part(
            _var_var_15["autarky_percent"], _var_var_38["autarky_percent"], 100.0, 0.6
        )
        _var_var_39 += _hubble_score_part(
            _var_var_15["home_consumption_kwh"],
            _var_var_38["home_consumption_kwh"],
            max(_var_var_15["home_consumption_kwh"] or 5.0, 5.0),
            0.5,
        )
        _var_var_39 += _hubble_score_part(
            _var_var_15["peak_hour"], _var_var_38["peak_hour"], 8.0, 0.5
        )
        _var_var_40 = max(0.0, min(100.0, 100.0 - _var_var_39 / 5.0 * 100.0))
        if _var_var_40 < 45.0:
            continue
        _var_var_29.append(
            {
                "date": _var_var_28,
                "similarity_percent": round(_var_var_40, 0),
                "yield_kwh": _hubble_round(_var_var_38["yield_kwh"], 1),
                "forecast_kwh": _hubble_round(_var_var_38["forecast_kwh"], 1),
                "autarky_percent": _var_var_38["autarky_percent"],
                "peak_hour": _var_var_38["peak_hour"],
                "window": _var_var_37,
            }
        )
        continue
    _var_var_29.sort(key=lambda item: item["similarity_percent"], reverse=True)
    _var_var_41 = _var_var_29[:3]
    if len(_var_var_41) < HUBBLE_MEMORY_MIN_MATCHES:
        return
    _var_var_43 = [
        int(_var_var_42["window"].get("start_hour"))
        for _var_var_42 in _var_var_41
        if _var_var_42.get("window")
        if _var_var_42["window"].get("start_hour") is not None
    ]
    _var_var_44 = None
    if _var_var_43:
        _var_var_45 = round(sum(_var_var_43) / len(_var_var_43))
        _var_var_44 = {"start_hour": _var_var_45, "end_hour": min(_var_var_45 + 2, 23)}
    _var_var_46 = _hubble_average(
        [
            _var_var_42["yield_kwh"]
            for _var_var_42 in _var_var_41
            if _var_var_42.get("yield_kwh") is not None
        ]
    )
    _var_var_47 = _hubble_average(
        [
            _var_var_42["forecast_kwh"]
            for _var_var_42 in _var_var_41
            if _var_var_42.get("forecast_kwh") is not None
        ]
    )
    _var_var_48 = _hubble_average(
        [_var_var_42["similarity_percent"] for _var_var_42 in _var_var_41]
    )
    _var_var_49 = _hubble_average(
        [
            _var_var_42["autarky_percent"]
            for _var_var_42 in _var_var_41
            if _var_var_42.get("autarky_percent") is not None
        ]
    )
    return {
        "available": True,
        "lookback_days": HUBBLE_MEMORY_LOOKBACK_DAYS,
        "match_count": len(_var_var_41),
        "matches": _var_var_41,
        "avg_similarity_percent": _hubble_round(_var_var_48, 0),
        "avg_yield_kwh": _hubble_round(_var_var_46, 1),
        "avg_forecast_kwh": _hubble_round(_var_var_47, 1),
        "avg_autarky_percent": _hubble_round(_var_var_49, 0),
        "common_window": _var_var_44,
    }
