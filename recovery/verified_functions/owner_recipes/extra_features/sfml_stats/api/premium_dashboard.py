# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..utils.time_utils import ha_now, ha_today
from .views import _build_live_today_clean_eval, _get_db, _table_columns, local_only


async def _live_payload(
    hass: HomeAssistant, snapshot: dict[str, Any]
) -> dict[str, Any]:
    _var_var_38 = ha_now(hass)
    _var_var_39 = ha_today(hass).isoformat()
    _var_var_40 = _var_var_38.hour - 1
    async with _get_db() as _var_var_41:
        async with _var_var_41.execute(
            "\n            SELECT hp.target_date,\n                   hp.target_hour,\n                   hp.prediction_kwh AS prediction_kwh,\n                   hp.physics_kwh,\n                   hp.ai_kwh,\n                   hp.tfs_kwh,\n                   hp.lstm_kwh,\n                   hp.ridge_kwh,\n                   hp.actual_kwh,\n                   hp.mppt_throttled,\n                   hp.mppt_throttle_reason,\n                   hp.inverter_clipped,\n                   hp.exclude_from_learning,\n                   hp.exclude_from_clean_evaluation,\n                   hp.has_weather_alert,\n                   hp.weather_alert_type\n            FROM hourly_predictions hp\n            WHERE hp.target_date = ?\n            ORDER BY hp.target_hour\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_43 = await _var_var_42.fetchall()
        async with _var_var_41.execute(
            "\n            SELECT timestamp, solar_power_w, house_consumption_w,\n                   battery_power_dc_w AS battery_power_w,\n                   battery_soc, solar_to_house_w,\n                   solar_to_battery_w, solar_to_grid_w, battery_to_house_w,\n                   grid_to_house_w, grid_to_battery_w\n            FROM stats_power_sources\n            ORDER BY timestamp DESC\n            LIMIT 1\n            "
        ) as _var_var_42:
            _var_var_44 = await _var_var_42.fetchone()
        async with _var_var_41.execute(
            "\n            SELECT total_actual_kwh, updated_at\n            FROM actual_live_state\n            WHERE date = ?\n            LIMIT 1\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_45 = await _var_var_42.fetchone()
        async with _var_var_41.execute(
            "\n            SELECT conservative_planning_forecast_kwh,\n                   conservative_planning_forecast_updated_at\n            FROM daily_forecast_tracking\n            WHERE date = ?\n            ORDER BY id DESC\n            LIMIT 1\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_46 = await _var_var_42.fetchone()
        async with _var_var_41.execute(
            "\n            SELECT solar_yield_kwh, home_consumption_kwh, autarkie_percent\n            FROM stats_daily_energy\n            WHERE date = ?\n            LIMIT 1\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_47 = await _var_var_42.fetchone()
        _var_var_48 = None
        _var_var_49 = await _table_columns(_var_var_41, "GPM_current_price")
        _var_var_50 = {
            "id",
            "is_cheap",
            "timestamp",
            "total_price",
            "last_updated",
            "average_today",
            "price_next_hour",
        }
        if _var_var_50 <= _var_var_49:
            async with _var_var_41.execute(
                "\n                SELECT timestamp, total_price, price_next_hour,\n                       average_today, is_cheap, last_updated\n                FROM GPM_current_price\n                WHERE id = 1\n                "
            ) as _var_var_42:
                _var_var_48 = await _var_var_42.fetchone()
        async with _var_var_41.execute(
            "\n            SELECT temperature_c, cloud_cover_percent, wind_speed_ms,\n                   solar_radiation_wm2, condition, timestamp\n            FROM hourly_weather_actual\n            WHERE date = ?\n            ORDER BY hour DESC\n            LIMIT 1\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_51 = await _var_var_42.fetchone()
        async with _var_var_41.execute(
            "\n            SELECT accuracy_percent\n            FROM daily_summaries\n            WHERE date < ?\n              AND accuracy_percent IS NOT NULL\n            ORDER BY date DESC\n            LIMIT 1\n            ",
            (_var_var_39,),
        ) as _var_var_42:
            _var_var_52 = await _var_var_42.fetchone()
    _var_var_53 = _build_live_today_clean_eval(
        _var_var_43, evaluation_cutoff_hour=_var_var_40
    )
    _var_var_54 = _number(
        _var_var_45["total_actual_kwh"] if _var_var_45 else _var_var_53["production"]
    )
    _var_var_55 = _number(_var_var_53["forecast"])
    _var_var_56 = (
        round(_var_var_54 - _var_var_55, 1)
        if _var_var_54 is not None and _var_var_55 is not None
        else None
    )
    _var_var_57 = _var_var_53.get("discarded_learning_reason_breakdown") or {}
    _var_var_58 = next(iter(_var_var_57), None)
    _var_var_59 = _number(
        _var_var_46["conservative_planning_forecast_kwh"] if _var_var_46 else None
    )
    _var_var_60 = (
        _var_var_59 / _var_var_55
        if _var_var_59 is not None and _var_var_55 is not None and (_var_var_55 > 0)
        else None
    )
    _var_var_61 = (
        snapshot.get("sections") if isinstance(snapshot.get("sections"), dict) else {}
    )
    _var_var_62 = (
        _var_var_61.get("overview")
        if isinstance(_var_var_61.get("overview"), dict)
        else {}
    )
    _var_var_63 = (
        _var_var_61.get("operation")
        if isinstance(_var_var_61.get("operation"), dict)
        else {}
    )
    _var_var_64 = (
        _var_var_61.get("mobility")
        if isinstance(_var_var_61.get("mobility"), dict)
        else {}
    )
    _var_var_65 = (
        _var_var_61.get("weather")
        if isinstance(_var_var_61.get("weather"), dict)
        else {}
    )
    _var_var_66 = _eai_config(hass)
    _var_var_67 = _first_entry_config(hass)
    _var_var_68 = _var_var_67.get("weather_entity")
    _var_var_69 = hass.states.get(_var_var_68) if _var_var_68 else None
    _var_var_70 = _var_var_69.attributes if _var_var_69 is not None else {}
    _var_var_71 = (
        _number(_var_var_51["temperature_c"], 1)
        if _var_var_51 and _var_var_51["temperature_c"] is not None
        else None
    )
    _var_var_72 = _number(_var_var_70.get("temperature"), 1)
    _var_var_73 = _var_var_71 if _var_var_71 is not None else _var_var_72
    _var_var_74 = (
        "sensor"
        if _var_var_71 is not None
        else "weather_entity" if _var_var_72 is not None else None
    )
    _var_var_75 = (
        _var_var_51["condition"]
        if _var_var_51 and _var_var_51["condition"]
        else _var_var_69.state if _var_var_69 is not None else None
    )
    _var_var_76 = _var_var_70.get("cloud_coverage")
    if _var_var_76 is None:
        _var_var_76 = _var_var_70.get("cloud_cover")
    _var_var_77 = (
        _number(_var_var_51["cloud_cover_percent"], 0)
        if _var_var_51 and _var_var_51["cloud_cover_percent"] is not None
        else _number(_var_var_76, 0)
    )
    _var_var_78 = bool(_var_var_66.get("heat_pump_enabled", False))
    _var_var_79 = bool(
        _var_var_64.get("configured", _var_var_66.get("wallbox_enabled", False))
    )
    _var_var_80 = bool(_var_var_66.get("weather_intelligence_enabled", False))
    _var_var_81 = _var_var_44["timestamp"] if _var_var_44 else None
    _var_var_82 = (
        _var_var_45["updated_at"]
        if _var_var_45 and _var_var_45["updated_at"]
        else _var_var_81
    )
    _var_var_83 = _var_var_65.get("warnings")
    if not isinstance(_var_var_83, list):
        _var_var_83 = _var_var_65.get("events")
    _var_var_83 = _public_warnings(_var_var_83)
    return {
        "schema_version": 1,
        "time_context": _time_context(hass),
        "generated_at": _var_var_38.isoformat(),
        "mode": "live",
        "is_demo": False,
        "sample_reason": "live",
        "premium": {
            "licensed": True,
            "license_status": snapshot.get("license_status"),
            "label": "Premium",
            "configuration_status": snapshot.get("configuration_status"),
            "capability_level": snapshot.get("capability_level"),
        },
        "freshness": {
            "energy": _var_var_82,
            "forecast": _var_var_82,
            "weather": (
                snapshot.get("data_freshness")
                if _var_var_80
                else _var_var_51["timestamp"] if _var_var_51 else None
            ),
            "eai": snapshot.get("data_freshness"),
        },
        "energy": {
            "solar_power_w": (
                _number(_var_var_44["solar_power_w"]) if _var_var_44 else None
            ),
            "home_consumption_w": (
                _number(_var_var_44["house_consumption_w"]) if _var_var_44 else None
            ),
            "grid_import_w": _sum_power(
                _var_var_44["grid_to_house_w"] if _var_var_44 else None,
                _var_var_44["grid_to_battery_w"] if _var_var_44 else None,
            ),
            "grid_export_w": (
                _number(_var_var_44["solar_to_grid_w"]) if _var_var_44 else None
            ),
            "battery_soc_percent": (
                _number(_var_var_44["battery_soc"]) if _var_var_44 else None
            ),
            "battery_power_w": (
                _number(_var_var_44["battery_power_w"]) if _var_var_44 else None
            ),
            "solar_to_house_w": (
                _number(_var_var_44["solar_to_house_w"]) if _var_var_44 else None
            ),
            "solar_to_battery_w": (
                _number(_var_var_44["solar_to_battery_w"]) if _var_var_44 else None
            ),
            "battery_to_house_w": (
                _number(_var_var_44["battery_to_house_w"]) if _var_var_44 else None
            ),
            "grid_to_house_w": (
                _number(_var_var_44["grid_to_house_w"]) if _var_var_44 else None
            ),
            "grid_to_battery_w": (
                _number(_var_var_44["grid_to_battery_w"]) if _var_var_44 else None
            ),
            "solar_yield_today_kwh": _number(
                _var_var_47["solar_yield_kwh"] if _var_var_47 else _var_var_54
            ),
            "home_consumption_today_kwh": _number(
                _var_var_47["home_consumption_kwh"] if _var_var_47 else None
            ),
            "autarky_percent": _number(
                _var_var_47["autarkie_percent"] if _var_var_47 else None, 1
            ),
        },
        "forecast": {
            "accuracy_percent": _number(
                _var_var_53.get("forecast_accuracy_vs_sot_percent"), 1
            ),
            "last_completed_accuracy_percent": (
                _number(_var_var_52["accuracy_percent"], 1) if _var_var_52 else None
            ),
            "forecast_error_percent": _number(
                _var_var_53.get("live_day_forecast_error_vs_sot_percent"), 1
            ),
            "actual_kwh": _var_var_54,
            "forecast_kwh": _var_var_55,
            "learning_hours": _var_var_53.get("learning_hours_count"),
            "learning_candidates": _var_var_53.get("learning_candidate_hours_count"),
            "discarded_hours": _var_var_53.get("discarded_learning_hours_count"),
            "discarded_reason": _var_var_58,
            "conservative_kwh": _var_var_59,
            "deviation_kwh": _var_var_56,
            "timeline": _forecast_timeline(
                _var_var_43,
                conservative_ratio=_var_var_60,
                actual_cutoff_hour=_var_var_40,
            ),
        },
        "price": {
            "current_ct_per_kwh": (
                _number(_var_var_48["total_price"]) if _var_var_48 else None
            ),
            "next_ct_per_kwh": (
                _number(_var_var_48["price_next_hour"]) if _var_var_48 else None
            ),
            "average_today_ct_per_kwh": (
                _number(_var_var_48["average_today"]) if _var_var_48 else None
            ),
            "is_cheap": bool(_var_var_48["is_cheap"]) if _var_var_48 else None,
        },
        "heat_pump": {
            "configured": _var_var_78,
            "is_demo": False,
            "sample_reason": "live",
            "power_kw": _number(
                _var_var_63.get("current_power_kw", _var_var_62.get("current_power_kw"))
            ),
            "mode": _var_var_63.get("current_mode"),
            "energy_today_kwh": _number(_var_var_62.get("energy_today_kwh")),
            "pv_coverage_percent": _number(_var_var_62.get("pv_coverage_percent"), 1),
            "recommendation": (
                _var_var_62.get("why_now", {}).get("headline")
                if isinstance(_var_var_62.get("why_now"), dict)
                else None
            ),
            "data_quality_percent": _number(_var_var_62.get("data_quality_percent"), 1),
        },
        "wallbox": {
            "configured": _var_var_79,
            "is_demo": False,
            "sample_reason": "live",
            "power_kw": _number(_var_var_64.get("current_power_kw")),
            "state": _var_var_64.get("state") or _wallbox_state(_var_var_64),
            "energy_today_kwh": _number(_var_var_64.get("energy_today_kwh")),
            "recommendation": _var_var_64.get("recommendation")
            or _var_var_64.get("recommended_action")
            or _var_var_64.get("reason"),
            "expected_pv_share_percent": _number(
                _var_var_64.get("expected_pv_share_percent"), 1
            ),
            "departure_readiness_percent": _number(
                _var_var_64.get("departure_readiness_percent"), 1
            ),
        },
        "weather": {
            "configured": _var_var_80,
            "available": bool(
                _var_var_51 or _var_var_69 is not None or _var_var_65.get("available")
            ),
            "temperature_c": _var_var_73,
            "temperature_source": _var_var_74,
            "condition": _var_var_75,
            "cloud_cover_percent": _var_var_77,
            "wind_speed_kmh": _number(
                (
                    float(_var_var_51["wind_speed_ms"]) * 3.6
                    if _var_var_51 and _var_var_51["wind_speed_ms"] is not None
                    else None
                ),
                1,
            ),
            "solar_radiation_wm2": (
                _number(_var_var_51["solar_radiation_wm2"], 0) if _var_var_51 else None
            ),
            "summary": _var_var_65.get("status") if _var_var_80 else None,
            "warnings": _var_var_83,
        },
    }
