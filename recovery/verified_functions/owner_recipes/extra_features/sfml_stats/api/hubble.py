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
