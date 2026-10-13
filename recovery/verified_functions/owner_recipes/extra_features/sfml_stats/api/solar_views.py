# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from . import context
from .common import (
    PRODUCTION_CANDIDATE_KWH_THRESHOLD,
    _build_forecast_error_metrics,
    _build_live_today_clean_eval,
    _clock_time,
    _column_expr,
    _corrected_daily_energy_values,
    _energy_correction_projection,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_local_naive_range_for_local_date,
    _get_power_w,
    _get_sensor_value,
    _get_solar_reader,
    _get_stats_gpm_coordinator,
    _gpm_local_hour_key,
    _panel_group_exclusion_reason,
    _stats_visible_exclusion_reason,
    _table_columns,
    _time_context,
    local_only,
)
from .hubble import (
    _hubble_best_window,
    _hubble_build_memory,
    _hubble_decline_hour,
    _hubble_has_battery,
    _hubble_read_workflow_status,
    _hubble_recommendation_start_hour,
    _hubble_resolve_moment,
    _hubble_round,
    _hubble_time_to_minutes,
)
from ..core.shadow_context import build_shadow_signal
from ..core.heatpump_today import capped_hp_pv_kwh, hp_pv_share_percent
from ..core.grid_split import SPLIT_TOLERANCE_W, reconcile_grid_split
from ..core.corrected_energy import (
    _resolve_price,
    component_day,
    corrected_month_sum,
    corrected_value,
    dynamic_month_costs,
    energy_flows_changed,
    dynamic_month_average,
    legacy_dynamic_month,
    load_hourly_component_days,
    monthly_table_energy_cost,
    window_autarky_average,
    yesterday_figures,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today
from ..core.price_mode import (
    MODE_LEGACY_FIXED,
    effective_price_mode,
    feed_in_tariff_ct,
    is_fixed_like,
)
from ..const import (
    DOMAIN,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_INVERTER_AC_OUTPUT,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_WEATHER_ENTITY,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    CONF_BILLING_BASE_FEE,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_PANEL_GROUP_NAMES,
    CONF_SHOW_PANEL_GROUPS,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    DEFAULT_HEATPUMP_COP,
)
from ..readers.solar_reader import PanelGroupData
from ..readers.annual_forecast_reader import AnnualForecastReader
from ..readers.weather_reader import WeatherDataReader
from ..sfml_data_reader import SFMLDataReader
from ..core.consumer_atlas import calculate_billing_period
from ..core.energy_balance import build_daily_supply_balance
from ..core.energy_efficiency import build_energy_chain_summary


async def _build_hubble_summary(
    today: date, summary: dict[str, Any]
) -> dict[str, Any] | None:
    if False:
        __assert_armored__ = None
    A_recovered_capture_0008 = ha_now(context.HASS)
    _var_var_31 = today.isoformat()
    _var_var_47 = summary.get("today") or {}
    _var_var_48 = summary.get("daily_stats") or {}
    _var_var_49 = summary.get("kpis") or {}
    _var_var_50 = _get_config()
    _var_var_51 = None
    _var_var_52 = None
    _var_var_53 = None
    _var_var_54 = None
    _var_var_55 = None
    _var_var_56 = []
    try:
        async with _get_db() as _var_var_32:
            async with _var_var_32.execute(
                "PRAGMA table_info(hourly_predictions)"
            ) as _var_var_33:
                _var_var_57 = {
                    _var_var_30["name"] for _var_var_30 in await _var_var_33.fetchall()
                }
            _var_var_59 = [
                _var_var_58
                for _var_var_58 in (
                    "physics_kwh",
                    "ai_kwh",
                    "ai_confidence",
                    "confidence",
                    "ml_contribution_percent",
                    "clouds",
                    "solar_radiation",
                    "weather_alert_type",
                    "has_weather_alert",
                )
                if _var_var_58 in _var_var_57
            ]
            _var_var_60 = ["target_hour", "prediction_kwh", "actual_kwh", *_var_var_59]
            async with _var_var_32.execute(
                f"SELECT {', '.join(_var_var_60)}\n                    FROM hourly_predictions\n                    WHERE target_date = ?\n                    ORDER BY target_hour",
                (_var_var_31,),
            ) as _var_var_33:
                _var_var_61 = [
                    dict(_var_var_30) for _var_var_30 in await _var_var_33.fetchall()
                ]
            try:
                async with _var_var_32.execute(
                    "SELECT version FROM shadow_pattern_config WHERE id = 1"
                ) as _var_var_33:
                    _var_var_62 = await _var_var_33.fetchone()
                if _var_var_62:
                    if str(_var_var_62[0] or "") == "2.0":
                        async with _var_var_32.execute(
                            "SELECT h.group_name, h.hour, h.shadow_occurrence_rate,\n                                  h.avg_shadow_percent, h.pct_building_tree,\n                                  h.pattern_type, h.confidence, h.sample_count,\n                                  s.shadow_occurrence_rate AS seasonal_occurrence_rate,\n                                  s.avg_shadow_percent AS seasonal_avg_shadow_percent,\n                                  s.dominant_cause AS seasonal_dominant_cause,\n                                  s.sample_count AS seasonal_sample_count,\n                                  s.confidence AS seasonal_confidence\n                           FROM shadow_pattern_hourly h\n                           LEFT JOIN shadow_pattern_seasonal s\n                             ON s.group_name = h.group_name\n                            AND s.hour = h.hour\n                            AND s.month = ?\n                           ORDER BY h.group_name, h.hour",
                            (today.month,),
                        ) as _var_var_33:
                            _var_var_56 = [
                                dict(_var_var_30)
                                for _var_var_30 in await _var_var_33.fetchall()
                            ]
            except Exception as _var_var_5:
                _LOGGER.debug(
                    "Could not read learned shadow context for Hubble: %s", _var_var_5
                )
            async with _var_var_32.execute(
                "SELECT AVG(home_consumption_kwh) AS avg_home,\n                          AVG(solar_to_battery_kwh) AS avg_battery_charge,\n                          AVG(autarkie_percent) AS avg_autarky\n                   FROM stats_daily_energy\n                   WHERE date < ?\n                     AND date >= date(?, '-7 days')",
                (_var_var_31, _var_var_31),
            ) as _var_var_33:
                _var_var_63 = await _var_var_33.fetchone()
            try:
                A_recovered_capture_0007 = await _table_columns(
                    _var_var_32, "stats_daily_energy"
                )
                _var_var_64 = (
                    "date",
                    "autarkie_percent",
                    "home_consumption_kwh",
                    "solar_to_house_kwh",
                    "battery_to_house_kwh",
                    "grid_to_battery_kwh",
                )
                _var_var_65 = ", ".join(
                    (
                        (
                            "date"
                            if _var_var_155 == "date"
                            else _column_expr(A_recovered_capture_0007, _var_var_155)
                        )
                        for _var_var_155 in _var_var_64
                    )
                )
                async with _var_var_32.execute(
                    f"SELECT {_var_var_65}\n                        FROM stats_daily_energy\n                        WHERE date < ?\n                          AND date >= date(?, '-7 days')",
                    (_var_var_31, _var_var_31),
                ) as _var_var_33:
                    _var_var_66 = await _var_var_33.fetchall()
                _var_var_67 = await _energy_correction_projection(
                    (today - timedelta(days=7)).isoformat()
                )
                _var_var_68 = []
                for _var_var_30 in _var_var_66:
                    _var_var_69 = str(_var_var_30["date"])
                    _var_var_70 = float(_var_var_30["home_consumption_kwh"] or 0.0)
                    _var_var_71 = float(_var_var_30["solar_to_house_kwh"] or 0.0)
                    _var_var_72 = float(_var_var_30["battery_to_house_kwh"] or 0.0)
                    _var_var_73 = float(_var_var_30["grid_to_battery_kwh"] or 0.0)
                    _var_var_68.append(
                        {
                            "stored": _var_var_30["autarkie_percent"],
                            "raw_consumption": _var_var_70,
                            "consumption": corrected_value(
                                _var_var_67,
                                _var_var_69,
                                "home_consumption_day_kwh",
                                _var_var_70,
                            ),
                            "raw_solar_to_house": _var_var_71,
                            "solar_to_house": corrected_value(
                                _var_var_67,
                                _var_var_69,
                                "solar_to_house_day_kwh",
                                _var_var_71,
                            ),
                            "raw_battery_to_house": _var_var_72,
                            "battery_to_house": corrected_value(
                                _var_var_67,
                                _var_var_69,
                                "battery_to_house_day_kwh",
                                _var_var_72,
                            ),
                            "raw_grid_to_battery": _var_var_73,
                            "grid_to_battery": corrected_value(
                                _var_var_67,
                                _var_var_69,
                                "grid_to_battery_day_kwh",
                                _var_var_73,
                            ),
                        }
                    )
                _var_var_54 = window_autarky_average(_var_var_68)
            except Exception as _var_var_5:
                _LOGGER.debug("Could not project Hubble 7-day autarky: %s", _var_var_5)
            if _var_var_50.get(CONF_SENSOR_INVERTER_AC_OUTPUT):
                try:
                    async with _var_var_32.execute(
                        "SELECT *\n                           FROM stats_energy_efficiency_daily\n                           WHERE date = ?",
                        (_var_var_31,),
                    ) as _var_var_33:
                        _var_var_55 = await _var_var_33.fetchone()
                except Exception as _var_var_5:
                    _LOGGER.debug("Could not read Hubble energy chain: %s", _var_var_5)
            async with _var_var_32.execute(
                "SELECT MAX(solar_yield_kwh) AS record_yield\n                   FROM stats_daily_energy\n                   WHERE date < ?",
                (_var_var_31,),
            ) as _var_var_33:
                _var_var_74 = await _var_var_33.fetchone()
            A_recovered_capture_0010 = (today - timedelta(days=1)).isoformat()
            try:
                A_recovered_capture_0004 = await _table_columns(
                    _var_var_32, "stats_daily_energy"
                )
                _var_var_75 = (
                    "solar_yield_kwh",
                    "home_consumption_kwh",
                    "grid_import_kwh",
                    "grid_export_kwh",
                    "autarkie_percent",
                    "savings_eur",
                    "feed_in_revenue_eur",
                    "total_cost_eur",
                    "consumer_heatpump_kwh",
                    "consumer_wallbox_kwh",
                    "consumer_hp_pv_kwh",
                    "grid_to_battery_kwh",
                    "solar_to_battery_kwh",
                    "battery_to_house_kwh",
                    "solar_to_house_kwh",
                )
                _var_var_76 = ", ".join(
                    (
                        _column_expr(A_recovered_capture_0004, _var_var_155)
                        for _var_var_155 in _var_var_75
                    )
                )
                async with _var_var_32.execute(
                    f"SELECT {_var_var_76}\n                       FROM stats_daily_energy\n                       WHERE date = ?",
                    (A_recovered_capture_0010,),
                ) as _var_var_33:
                    _var_var_51 = await _var_var_33.fetchone()
                try:
                    async with _var_var_32.execute(
                        "SELECT average_total FROM GPM_daily_averages WHERE date = ?",
                        (A_recovered_capture_0010,),
                    ) as _var_var_33:
                        _var_var_77 = await _var_var_33.fetchone()
                        if _var_var_77 is not None:
                            _var_var_53 = _var_var_77[0]
                except Exception as _var_var_5:
                    _LOGGER.debug(
                        "Could not read yesterday price average: %s", _var_var_5
                    )
            except Exception as _var_var_5:
                _LOGGER.debug(
                    "Could not read yesterday daily energy for Hubble: %s", _var_var_5
                )
            try:
                async with _var_var_32.execute(
                    "SELECT accuracy_percent, error_kwh, error_percent, avg_temperature_diff, avg_cloud_cover_diff,\n                              forecast_dominant, actual_dominant, predicted_total_kwh, actual_total_kwh\n                       FROM daily_summaries\n                       WHERE date = ?",
                    (A_recovered_capture_0010,),
                ) as _var_var_33:
                    _var_var_52 = await _var_var_33.fetchone()
            except Exception as _var_var_5:
                _LOGGER.debug(
                    "Could not read yesterday summary for Hubble: %s", _var_var_5
                )
            _var_var_78 = None
            try:
                _var_var_78 = await _hubble_build_memory(
                    _var_var_32,
                    today,
                    _var_var_61,
                    _var_var_48,
                    _var_var_47,
                    _var_var_57,
                )
            except Exception as _var_var_5:
                _LOGGER.debug("Could not build Hubble memory: %s", _var_var_5)
    except Exception as _var_var_5:
        _LOGGER.debug("Could not build Hubble summary: %s", _var_var_5)
        return
    if not _var_var_61 and (not _var_var_48):
        return
    _var_var_79 = int(_var_var_47.get("evaluation_hours_count") or 0)
    _var_var_80 = [
        _var_var_30
        for _var_var_30 in _var_var_61
        if _var_var_30.get("actual_kwh") is not None
        if float(_var_var_30.get("actual_kwh") or 0.0)
        > PRODUCTION_CANDIDATE_KWH_THRESHOLD
        or float(_var_var_30.get("prediction_kwh") or 0.0)
        > PRODUCTION_CANDIDATE_KWH_THRESHOLD
    ]
    _var_var_81 = sum(
        (float(_var_var_30.get("prediction_kwh") or 0.0) for _var_var_30 in _var_var_61)
    )
    _var_var_82 = None
    _var_var_83 = today + timedelta(days=1)
    try:
        _var_var_84 = _get_solar_reader()
        _var_var_85 = await _var_var_84.async_get_hourly_predictions(_var_var_83)
        if _var_var_85:
            _var_var_82 = _hubble_round(
                sum((_var_var_30.prediction_kwh for _var_var_30 in _var_var_85)), 1
            )
        else:
            _var_var_86 = await _var_var_84.async_get_daily_forecasts()
            _var_var_87 = _var_var_86.get("tomorrow")
            if _var_var_87 and _var_var_87.prediction_kwh is not None:
                _var_var_82 = _hubble_round(_var_var_87.prediction_kwh, 1)
    except Exception as _var_var_5:
        _LOGGER.debug("Could not build Hubble tomorrow's forecast: %s", _var_var_5)
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT prediction_kwh\n                       FROM daily_forecasts\n                       WHERE forecast_date = ?\n                       ORDER BY forecast_type = 'tomorrow' DESC\n                       LIMIT 1",
                    (_var_var_83.isoformat(),),
                ) as _var_var_33:
                    _var_var_30 = await _var_var_33.fetchone()
                    if _var_var_30 and _var_var_30[0] is not None:
                        _var_var_82 = _hubble_round(_var_var_30[0], 1)
        except Exception as _var_var_88:
            _LOGGER.debug(
                "Could not load Hubble tomorrow daily fallback: %s", _var_var_88
            )
    _var_var_89 = _hubble_time_to_minutes(
        (summary.get("sun_times") or {}).get("sunset")
    )
    if _var_var_89 is None:
        try:
            _var_var_6 = context.HASS.states.get("sun.sun")
            if _var_var_6:
                _var_var_90 = _var_var_6.attributes.get(
                    "next_setting"
                ) or _var_var_6.attributes.get("sunset")
                if _var_var_90:
                    _var_var_91 = datetime.fromisoformat(_var_var_90)
                    _var_var_92 = as_ha_local(_var_var_91, context.HASS)
                    _var_var_89 = _var_var_92.hour * 60 + _var_var_92.minute
        except Exception as _var_var_5:
            _LOGGER.debug(
                "Hubble sunset fallback from HA sun.sun failed: %s", _var_var_5
            )
    if _var_var_89 is None:
        _var_var_89 = 1080
    _var_var_93, _var_var_94 = _hubble_resolve_moment(
        A_recovered_capture_0008, _var_var_89, _var_var_81, _var_var_79
    )
    _var_var_95 = _hubble_recommendation_start_hour(A_recovered_capture_0008)
    _var_var_96 = _hubble_best_window(_var_var_61, _var_var_95, _var_var_89)
    _var_var_97 = build_shadow_signal(_var_var_56, start_hour=_var_var_95)
    if _var_var_93 == "morning":
        _var_var_96 = (
            _hubble_best_window(_var_var_61, max(8, _var_var_95), _var_var_89)
            or _var_var_96
        )
    _var_var_98 = _hubble_decline_hour(_var_var_61, _var_var_96)
    _var_var_99 = _hubble_round(_var_var_63["avg_home"], 1) if _var_var_63 else None
    _var_var_100 = (
        _hubble_round(_var_var_63["avg_battery_charge"], 1) if _var_var_63 else None
    )
    if _var_var_54 is None:
        _var_var_101 = (
            _hubble_round(_var_var_63["avg_autarky"], 1) if _var_var_63 else None
        )
    else:
        _var_var_101 = _hubble_round(_var_var_54, 1)
    _var_var_102 = _hubble_round(_var_var_48.get("solar_to_battery_kwh"), 2)
    _var_var_103 = _hubble_has_battery(_var_var_50, _var_var_48, _var_var_100)
    _var_var_104 = build_daily_supply_balance(
        household_total_kwh=_var_var_48.get("home_consumption_kwh"),
        solar_to_house_kwh=_var_var_48.get("solar_to_house_kwh"),
        battery_to_house_kwh=_var_var_48.get("battery_to_house_kwh"),
        grid_to_house_kwh=_var_var_48.get("grid_to_house_kwh"),
        battery_available=_var_var_103,
    )
    _var_var_105 = None
    if _var_var_55 is not None:
        _var_var_106 = max(
            1.0,
            A_recovered_capture_0008.hour * 3600.0
            + A_recovered_capture_0008.minute * 60.0
            + A_recovered_capture_0008.second,
        )
        _var_var_105 = build_energy_chain_summary(
            _var_var_55,
            net_autarky_percent=_var_var_48.get("autarkie_percent"),
            elapsed_seconds=_var_var_106,
            battery_available=_var_var_103,
        )
    if not _var_var_103:
        _var_var_102 = None
        _var_var_100 = None
    _var_var_107 = (
        _var_var_103
        and _var_var_102 is not None
        and (_var_var_100 is not None)
        and (abs(_var_var_102 - _var_var_100) <= max(0.5, _var_var_100 * 0.15))
    )
    _var_var_108 = sum(
        (
            float(_var_var_30.get("prediction_kwh") or 0.0)
            for _var_var_30 in _var_var_61
            if int(_var_var_30.get("target_hour") or -1)
            >= A_recovered_capture_0008.hour
        )
    )
    _var_var_109 = None
    for _var_var_30 in _var_var_61:
        _var_var_110 = _var_var_30.get("physics_kwh")
        _var_var_111 = _var_var_30.get("ai_kwh")
        if _var_var_110 is None or _var_var_111 is None:
            continue
        _var_var_110 = float(_var_var_110 or 0.0)
        _var_var_111 = float(_var_var_111 or 0.0)
        if _var_var_110 <= 0.05:
            continue
        _var_var_112 = (_var_var_111 - _var_var_110) / _var_var_110 * 100.0
        _var_var_113 = {
            "hour": int(_var_var_30.get("target_hour") or 0),
            "delta_percent": round(_var_var_112, 1),
            "physics_kwh": round(_var_var_110, 2),
            "ai_kwh": round(_var_var_111, 2),
            "confidence": _hubble_round(
                _var_var_30.get("ai_confidence") or _var_var_30.get("confidence"), 2
            ),
            "ml_share_percent": _hubble_round(
                _var_var_30.get("ml_contribution_percent"), 0
            ),
        }
        if _var_var_109 is None or abs(_var_var_113["delta_percent"]) > abs(
            _var_var_109["delta_percent"]
        ):
            _var_var_109 = _var_var_113
    _var_var_114 = _hubble_round(
        _var_var_47.get(
            "forecast_accuracy_vs_sot_percent",
            _var_var_47.get("forecast_accuracy_vs_actual_percent"),
        ),
        1,
    )
    _var_var_115 = _hubble_round(
        _var_var_47.get(
            "forecast_error_vs_sot_percent",
            _var_var_47.get("forecast_error_vs_actual_percent"),
        ),
        1,
    )
    _var_var_116 = "flex_load_window" if _var_var_96 else "watch_day"
    if _var_var_93 == "evening":
        _var_var_116 = "evening_review"
    elif _var_var_107 and _var_var_102:
        _var_var_116 = "battery_served"
    elif (
        _var_var_96
        and _var_var_49.get("price_current") is not None
        and (_var_var_49.get("price_avg") is not None)
    ):
        try:
            if float(_var_var_49["price_current"]) < float(_var_var_49["price_avg"]):
                _var_var_116 = "cheap_price_plus_solar"
        except (TypeError, ValueError):
            pass
    _var_var_117 = None
    if _var_var_51:
        _var_var_118 = dict(_var_var_51)
        _var_var_119 = dict(_var_var_52) if _var_var_52 else {}
        A_recovered_capture_0009 = await _energy_correction_projection(
            A_recovered_capture_0010
        )

        def _keep_corrected(raw: Any, metric: str) -> Any:
            if raw is None:
                return
            return corrected_value(
                A_recovered_capture_0009, A_recovered_capture_0010, metric, raw
            )

        _var_var_70 = _var_var_118.get("home_consumption_kwh")
        _var_var_121 = _var_var_118.get("grid_export_kwh")
        _var_var_71 = _var_var_118.get("solar_to_house_kwh")
        _var_var_72 = _var_var_118.get("battery_to_house_kwh")
        _var_var_73 = _var_var_118.get("grid_to_battery_kwh")
        _var_var_122 = corrected_value(
            A_recovered_capture_0009,
            A_recovered_capture_0010,
            "home_consumption_day_kwh",
            _var_var_70,
        )
        _var_var_123 = corrected_value(
            A_recovered_capture_0009,
            A_recovered_capture_0010,
            "grid_export_day_kwh",
            _var_var_121,
        )
        _var_var_124 = _keep_corrected(_var_var_71, "solar_to_house_day_kwh")
        _var_var_125 = _keep_corrected(_var_var_72, "battery_to_house_day_kwh")
        _var_var_126 = _keep_corrected(_var_var_73, "grid_to_battery_day_kwh")
        _var_var_127 = _keep_corrected(
            _var_var_118.get("solar_to_battery_kwh"), "solar_to_battery_day_kwh"
        )
        _var_var_128 = _keep_corrected(
            _var_var_118.get("consumer_heatpump_kwh"), "consumer_heatpump_day_kwh"
        )
        _var_var_129 = _keep_corrected(
            _var_var_118.get("consumer_wallbox_kwh"), "consumer_wallbox_day_kwh"
        )
        _var_var_130 = _var_var_128 or 0.0
        _var_var_131 = capped_hp_pv_kwh(
            _var_var_118.get("consumer_hp_pv_kwh"), _var_var_128
        )
        _var_var_132 = hp_pv_share_percent(_var_var_130, _var_var_131)
        try:
            _var_var_133 = float(_get_fixed_total_price(_var_var_50))
        except (TypeError, ValueError):
            _var_var_133 = None
        try:
            _var_var_134 = float(
                _var_var_50.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
            )
        except (TypeError, ValueError):
            _var_var_134 = DEFAULT_FEED_IN_TARIFF
        _var_var_135, _var_var_136 = _resolve_price(
            daily_average=_var_var_53, tariff_fallback_ct=_var_var_133
        )
        _var_var_137 = yesterday_figures(
            raw_consumption=_var_var_70 or 0.0,
            consumption=_var_var_122,
            raw_solar_to_house=_var_var_71 or 0.0,
            solar_to_house=0.0 if _var_var_124 is None else _var_var_124,
            raw_battery_to_house=_var_var_72 or 0.0,
            battery_to_house=0.0 if _var_var_125 is None else _var_var_125,
            raw_grid_to_battery=_var_var_73 or 0.0,
            grid_to_battery=0.0 if _var_var_126 is None else _var_var_126,
            raw_export=_var_var_121 or 0.0,
            export_kwh=_var_var_123,
            stored_autarky=_var_var_118.get("autarkie_percent"),
            stored_savings=_var_var_118.get("savings_eur"),
            stored_revenue=_var_var_118.get("feed_in_revenue_eur"),
            import_fallback_ct=_var_var_135,
            feed_in_tariff_ct=_var_var_134,
        )
        _var_var_117 = {
            "solar_yield_kwh": _hubble_round(
                corrected_value(
                    A_recovered_capture_0009,
                    A_recovered_capture_0010,
                    "solar_yield_day_kwh",
                    _var_var_118.get("solar_yield_kwh"),
                ),
                2,
            ),
            "home_consumption_kwh": _hubble_round(_var_var_122, 2),
            "grid_import_kwh": _hubble_round(
                corrected_value(
                    A_recovered_capture_0009,
                    A_recovered_capture_0010,
                    "grid_import_day_kwh",
                    _var_var_118.get("grid_to_house_kwh")
                    or _var_var_118.get("grid_import_kwh"),
                ),
                2,
            ),
            "grid_export_kwh": _hubble_round(_var_var_123, 2),
            "autarky_percent": _hubble_round(_var_var_137["autarky_percent"], 1),
            "savings_eur": _hubble_round(_var_var_137["savings_eur"], 2),
            "feed_in_revenue_eur": _hubble_round(
                _var_var_137["feed_in_revenue_eur"], 2
            ),
            "grid_to_battery_kwh": _hubble_round(_var_var_126, 2),
            "solar_to_battery_kwh": _hubble_round(_var_var_127, 2),
            "battery_to_house_kwh": _hubble_round(_var_var_125, 2),
            "hp_kwh": _hubble_round(_var_var_130, 2),
            "hp_pv_kwh": _hubble_round(_var_var_131, 2),
            "hp_pv_percent": _var_var_132,
            "wallbox_kwh": _hubble_round(_var_var_129, 2),
            "accuracy_percent": _hubble_round(_var_var_119.get("accuracy_percent"), 1),
            "error_percent": _hubble_round(_var_var_119.get("error_percent"), 1),
            "avg_temperature_diff": _hubble_round(
                _var_var_119.get("avg_temperature_diff"), 1
            ),
            "avg_cloud_cover_diff": _hubble_round(
                _var_var_119.get("avg_cloud_cover_diff"), 1
            ),
            "forecast_dominant": _var_var_119.get("forecast_dominant"),
            "actual_dominant": _var_var_119.get("actual_dominant"),
            "predicted_total_kwh": _hubble_round(
                _var_var_119.get("predicted_total_kwh"), 2
            ),
            "actual_total_kwh": _hubble_round(_var_var_119.get("actual_total_kwh"), 2),
        }
    _var_var_138 = None
    _var_var_139 = _hubble_round(_var_var_48.get("solar_yield_kwh"), 2)
    _var_var_140 = (
        _hubble_round(_var_var_74["record_yield"], 2) if _var_var_74 else None
    )
    if _var_var_139 is not None and _var_var_140 is not None and (_var_var_140 > 0):
        if _var_var_139 >= _var_var_140:
            _var_var_138 = {
                "level": "new_record",
                "solar_kwh": _var_var_139,
                "record_kwh": _var_var_140,
            }
        elif _var_var_139 >= _var_var_140 * 0.9:
            _var_var_138 = {
                "level": "near_record",
                "solar_kwh": _var_var_139,
                "record_kwh": _var_var_140,
            }
    _var_var_141 = None
    if _var_var_115 is not None:
        if abs(float(_var_var_115)) >= 30.0:
            _var_var_142 = None
            for _var_var_30 in _var_var_61:
                if _var_var_30.get("weather_alert_type"):
                    _var_var_142 = _var_var_30.get("weather_alert_type")
                    break
                if _var_var_30.get("has_weather_alert"):
                    _var_var_142 = "weather alert"
            _var_var_141 = {"error_percent": _var_var_115, "reason": _var_var_142}
    _var_var_143 = None
    if (
        _var_var_117
        and _var_var_117.get("hp_kwh")
        and (_var_var_117.get("hp_pv_percent") is not None)
    ):
        _var_var_144 = float(_var_var_117["hp_pv_percent"])
        if _var_var_144 >= 70.0:
            _var_var_143 = {
                "type": "heatpump_praise",
                "hp_pv_percent": _hubble_round(_var_var_144, 0),
            }
        elif _var_var_144 < 25.0 and _var_var_96:
            _var_var_143 = {
                "type": "heatpump_tip",
                "hp_pv_percent": _hubble_round(_var_var_144, 0),
                "window": _var_var_96,
            }
    _var_var_145 = None
    try:
        _var_var_146 = float(_var_var_49.get("price_current"))
        if _var_var_146 < 0:
            _var_var_145 = {"price_ct": _hubble_round(_var_var_146, 2)}
    except (TypeError, ValueError):
        pass
    from ..core.dev_mode import is_dev_active as _var_var_147

    _var_var_148 = (
        _hubble_read_workflow_status() if _var_var_147(context.HASS) else None
    )
    if not _var_var_147(context.HASS):
        _var_var_109 = None
    _var_var_149 = []
    _var_var_150 = [
        (CONF_SENSOR_HEATPUMP_POWER, "heatpump"),
        (CONF_SENSOR_HEATINGROD_POWER, "heatingrod"),
        (CONF_SENSOR_WALLBOX_POWER, "wallbox"),
    ]
    from ..core.eai_wp_sources import resolved_heatpump_entity as _var_var_151

    for _var_var_152, _var_var_153 in _var_var_150:
        _var_var_154 = (
            _var_var_151(context.HASS, _var_var_50, _var_var_152)
            if _var_var_152 == CONF_SENSOR_HEATPUMP_POWER
            else _var_var_50.get(_var_var_152)
        )
        if _var_var_154:
            _var_var_149.append(
                {
                    "config_key": _var_var_153,
                    "power_entity_id": _var_var_154,
                    "daily_entity_id": None,
                    "mode": "integrated",
                }
            )
    return {
        "available": True,
        "agent": "Hubble",
        "moment": _var_var_93,
        "moment_gate": _var_var_94,
        "tip_type": _var_var_116,
        "generated_at": A_recovered_capture_0008.isoformat(timespec="seconds"),
        "actual_hours_count": len(_var_var_80),
        "best_window": _var_var_96,
        "variant_seed": (
            today.day + today.weekday() + A_recovered_capture_0008.hour // 4
        )
        % 3,
        "yesterday": _var_var_117,
        "configured_consumers": _var_var_149,
        "day_balance": _var_var_104,
        "energy_chain": _var_var_105,
        "metrics": {
            "solar_yield_kwh": _hubble_round(_var_var_48.get("solar_yield_kwh"), 2),
            "home_consumption_kwh": _hubble_round(
                _var_var_48.get("home_consumption_kwh"), 2
            ),
            "grid_import_kwh": _hubble_round(
                (
                    _var_var_48.get("grid_to_house_kwh")
                    if _var_var_48.get("grid_to_house_kwh") not in (None, 0)
                    else _var_var_48.get("grid_import_kwh")
                ),
                2,
            ),
            "autarky_percent": _hubble_round(_var_var_48.get("autarkie_percent"), 1),
            "battery_available": _var_var_103,
            "solar_to_battery_kwh": _var_var_102,
            "battery_to_house_kwh": (
                _hubble_round(_var_var_48.get("battery_to_house_kwh"), 2)
                if _var_var_103
                else None
            ),
            "peak_solar_w": _hubble_round(_var_var_48.get("peak_solar_w"), 0),
            "peak_solar_time": _var_var_48.get("peak_solar_time") or None,
            "forecast_today_kwh": _hubble_round(_var_var_81, 1),
            "forecast_tomorrow_kwh": _var_var_82,
            "forecast_remaining_kwh": _hubble_round(_var_var_108, 1),
            "evaluation_predicted_kwh": _hubble_round(
                _var_var_47.get("evaluation_predicted_kwh"), 2
            ),
            "evaluation_actual_kwh": _hubble_round(
                _var_var_47.get("evaluation_actual_kwh"), 2
            ),
            "forecast_quality_percent": _var_var_114,
            "forecast_error_percent": _var_var_115,
            "avg_home_consumption_7d_kwh": _var_var_99,
            "avg_battery_charge_7d_kwh": _var_var_100,
            "avg_autarky_7d_percent": _var_var_101,
            "price_current_ct": _hubble_round(_var_var_49.get("price_current"), 2),
            "price_avg_ct": _hubble_round(_var_var_49.get("price_avg"), 2),
            "price_min_ct": _hubble_round(_var_var_49.get("price_min"), 2),
            "price_max_ct": _hubble_round(_var_var_49.get("price_max"), 2),
            "decline_hour": _var_var_98,
        },
        "signals": {
            "battery_available": _var_var_103,
            "battery_near_average": _var_var_107,
            "strongest_ai_delta": _var_var_109,
            "record_signal": _var_var_138,
            "forecast_issue": _var_var_141,
            "helper_feedback": _var_var_143,
            "negative_price_signal": _var_var_145,
            "workflow_status": _var_var_148,
            "learned_shadow": _var_var_97,
        },
        "memory": _var_var_78,
    }


async def _corrected_finalized_solar_values(start_date: str) -> dict[str, float]:
    """Project PV yield for the exact finalized-day population used by annual reports."""
    _var_var_168 = await _energy_correction_projection(start_date)
    _var_var_169 = {}
    async with _get_db() as _var_var_32:
        async with _var_var_32.execute(
            "SELECT date, actual_total_kwh solar_yield_kwh\n               FROM daily_summaries\n               WHERE date >= ? AND actual_total_kwh > 0",
            (start_date,),
        ) as _var_var_33:
            for _var_var_30 in await _var_var_33.fetchall():
                _var_var_169[_var_var_30["date"]] = corrected_value(
                    _var_var_168,
                    _var_var_30["date"],
                    "solar_yield_day_kwh",
                    _var_var_30["solar_yield_kwh"],
                )
    if True:
        return _var_var_169


class SolarDataView(HomeAssistantView):

    @local_only
    async def get(self, request: Request) -> Response:
        """Return solar data. @zara"""
        _var_var_181 = max(1, min(int(request.query.get("days", 7)), 365))
        _var_var_182 = request.query.get("hourly", "true").lower() == "true"
        _var_var_157 = _query_local_date(request)
        _var_var_183 = _var_var_157 - timedelta(days=_var_var_181 - 1)
        _var_var_169 = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "selected_date": _var_var_157.isoformat(),
            "data": {},
        }
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_184 = await _var_var_84.async_get_daily_summaries(
                start_date=_var_var_183, end_date=_var_var_157
            )
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT target_date, target_hour, prediction_kwh, actual_kwh,\n                              exclude_from_learning, exclude_from_clean_evaluation,\n                              mppt_throttled, mppt_throttle_reason,\n                              inverter_clipped, has_weather_alert,\n                              weather_alert_type\n                       FROM hourly_predictions\n                       WHERE target_date BETWEEN ? AND ?\n                       ORDER BY target_date, target_hour",
                    (_var_var_183.isoformat(), _var_var_157.isoformat()),
                ) as _var_var_33:
                    _var_var_61 = await _var_var_33.fetchall()
            _var_var_185 = _build_learning_basis_by_day(_var_var_61)
            _var_var_186 = {
                (
                    _var_var_30["target_date"],
                    int(_var_var_30["target_hour"] or 0),
                ): _clean_evaluation_flags(_var_var_30)
                for _var_var_30 in _var_var_61
            }
            _var_var_169["data"]["daily"] = [
                {
                    "date": _var_var_187.date.isoformat(),
                    "overall": {
                        "predicted_total_kwh": _var_var_187.predicted_total_kwh,
                        "actual_total_kwh": _var_var_187.actual_total_kwh,
                        "accuracy_percent": _var_var_187.accuracy_percent,
                        **_build_daily_summary_forecast_metrics(
                            _var_var_187.predicted_total_kwh,
                            _var_var_187.actual_total_kwh,
                            _var_var_187.evaluation_predicted_kwh,
                            _var_var_187.evaluation_actual_kwh,
                            _var_var_187.accuracy_percent,
                        ),
                        "error_kwh": _var_var_187.error_kwh,
                        "production_hours": _var_var_187.production_hours,
                        "peak_hour": _var_var_187.peak_hour,
                        "peak_kwh": _var_var_187.peak_kwh,
                        "evaluation_predicted_kwh": _var_var_187.evaluation_predicted_kwh,
                        "evaluation_actual_kwh": _var_var_187.evaluation_actual_kwh,
                        "evaluation_coverage_percent": _var_var_187.evaluation_coverage_percent,
                        "excluded_hours_count": _var_var_187.excluded_hours_count,
                        "excluded_mppt_hours_count": _var_var_187.excluded_mppt_hours_count,
                        "evaluation_hours_count": _var_var_187.evaluation_hours_count,
                        "production_candidate_hours_count": _var_var_187.production_candidate_hours_count,
                        "missing_actual_hours_count": _var_var_187.missing_actual_hours_count,
                        "excluded_weather_alert_hours_count": _var_var_187.excluded_weather_alert_hours_count,
                        "excluded_inverter_clipped_hours_count": _var_var_187.excluded_inverter_clipped_hours_count,
                        "excluded_reason_breakdown_json": _var_var_187.excluded_reason_breakdown_json,
                        **_var_var_185.get(_var_var_187.date.isoformat(), {}),
                    },
                    "time_windows": {
                        "morning_accuracy": _var_var_187.morning_accuracy,
                        "midday_accuracy": _var_var_187.midday_accuracy,
                        "afternoon_accuracy": _var_var_187.afternoon_accuracy,
                    },
                    "ml_metrics": {
                        "mae": _var_var_187.ml_mae,
                        "rmse": _var_var_187.ml_rmse,
                        "r2_score": _var_var_187.ml_r2_score,
                    },
                }
                for _var_var_187 in _var_var_184
            ]
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading daily summaries from database: %s", _var_var_171
            )
            _var_var_169["data"]["daily"] = []
        if _var_var_182:
            try:
                _var_var_84 = _get_solar_reader()
                _var_var_188 = []
                _var_var_189 = _var_var_183
                while _var_var_189 <= _var_var_157:
                    _var_var_190 = await _var_var_84.async_get_hourly_predictions(
                        target_date=_var_var_189
                    )
                    for _var_var_191 in _var_var_190:
                        _var_var_192 = _var_var_186.get(
                            (
                                _var_var_191.target_date.isoformat(),
                                int(_var_var_191.target_hour or 0),
                            ),
                            {},
                        )
                        _var_var_188.append(
                            {
                                "target_datetime": _var_var_191.target_datetime.isoformat(),
                                "target_hour": _var_var_191.target_hour,
                                "target_date": _var_var_191.target_date.isoformat(),
                                "prediction_kwh": _var_var_191.prediction_kwh,
                                "actual_kwh": _var_var_191.actual_kwh,
                                "accuracy_percent": _var_var_191.accuracy_percent,
                                "error_kwh": _var_var_191.error_kwh,
                                "prediction_method": _var_var_191.prediction_method,
                                "ml_contribution_percent": _var_var_191.ml_contribution_percent,
                                "confidence": _var_var_191.confidence,
                                "temperature": _var_var_191.temperature,
                                "solar_radiation": _var_var_191.solar_radiation,
                                "clouds": _var_var_191.clouds,
                                "sun_elevation": _var_var_191.sun_elevation,
                                "theoretical_max_kwh": _var_var_191.theoretical_max_kwh,
                                **_var_var_192,
                            }
                        )
                    _var_var_189 += timedelta(days=1)
                _var_var_169["data"]["hourly"] = _var_var_188
            except Exception as _var_var_171:
                _LOGGER.error(
                    "Error loading hourly predictions from database: %s", _var_var_171
                )
                _var_var_169["data"]["hourly"] = []
            try:
                _var_var_84 = _get_solar_reader()
                _var_var_193 = await _var_var_84.async_get_operational_hybrid_forecast(
                    target_date=_var_var_157
                )
                if _var_var_193:
                    _var_var_169["data"]["hybrid"] = {
                        "available": True,
                        "target_date": _var_var_193.target_date.isoformat(),
                        "total_kwh": _var_var_193.total_kwh,
                        "ops_today_row_kwh": _var_var_193.ops_today_row_kwh,
                        "ops_today_row_scope": _var_var_193.ops_today_row_scope,
                        "hourly": [
                            {
                                "target_hour": _var_var_194.target_hour,
                                "target_date": _var_var_194.target_date.isoformat(),
                                "prediction_kwh": _var_var_194.prediction_kwh,
                                "snapshot_type": _var_var_194.snapshot_type,
                                "source_track": _var_var_194.source_track,
                                "snapshot_sequence": _var_var_194.snapshot_sequence,
                                "trigger_source": _var_var_194.trigger_source,
                                "reforecast_cutoff_hour": _var_var_194.reforecast_cutoff_hour,
                            }
                            for _var_var_194 in _var_var_193.hourly
                        ],
                    }
                else:
                    _var_var_169["data"]["hybrid"] = {"available": False, "hourly": []}
            except Exception as _var_var_171:
                _LOGGER.error(
                    "Error loading operational hybrid forecast from database: %s",
                    _var_var_171,
                )
                _var_var_169["data"]["hybrid"] = {"available": False, "hourly": []}
            try:
                _var_var_84 = _get_solar_reader()
                _var_var_195 = (
                    await _var_var_84.async_get_conservative_planning_forecast(
                        target_date=_var_var_157
                    )
                )
                if _var_var_195:
                    _var_var_169["data"]["conservative"] = {
                        "available": True,
                        "target_date": _var_var_195.target_date.isoformat(),
                        "total_kwh": _var_var_195.total_kwh,
                        "updated_at": _var_var_195.updated_at,
                        "source": "daily_forecast_tracking",
                        "hourly": [
                            {
                                "target_hour": _var_var_194.target_hour,
                                "target_date": _var_var_194.target_date.isoformat(),
                                "prediction_kwh": _var_var_194.prediction_kwh,
                            }
                            for _var_var_194 in _var_var_195.hourly
                        ],
                    }
                else:
                    _var_var_169["data"]["conservative"] = {
                        "available": False,
                        "hourly": [],
                    }
            except Exception as _var_var_171:
                _LOGGER.error(
                    "Error loading conservative planning forecast from database: %s",
                    _var_var_171,
                )
                _var_var_169["data"]["conservative"] = {
                    "available": False,
                    "hourly": [],
                }
        _var_var_196 = await _get_weather_from_db(
            days=_var_var_181, start_date=_var_var_183, end_date=_var_var_157
        )
        if _var_var_196:
            _var_var_169["data"]["weather"] = _var_var_196
        _var_var_197 = await _get_weather_forecast_from_db(
            days=_var_var_181, start_date=_var_var_183, end_date=_var_var_157
        )
        if _var_var_197:
            _var_var_169["data"]["weather_corrected"] = _var_var_197
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_198 = await _var_var_84.async_get_model_state()
            if _var_var_198:
                _var_var_169["data"]["ai_state"] = {
                    "model_loaded": _var_var_198.model_loaded,
                    "algorithm_used": _var_var_198.algorithm_used,
                    "training_samples": _var_var_198.training_samples,
                    "current_accuracy": _var_var_198.current_accuracy,
                    "last_training": (
                        _var_var_198.last_training.isoformat()
                        if _var_var_198.last_training
                        else None
                    ),
                    "peak_power_kw": _var_var_198.peak_power_kw,
                    "feature_weights": _var_var_198.feature_weights,
                    "feature_importance": _var_var_198.feature_importance,
                }
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading AI model state from database: %s", _var_var_171
            )
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_86 = await _var_var_84.async_get_daily_forecasts()
            _var_var_199 = _var_var_86.get("today")
            _var_var_87 = _var_var_86.get("tomorrow")
            _var_var_200 = _var_var_86.get("day_after_tomorrow")
            _var_var_169["data"]["forecasts"] = {
                "today": {
                    "prediction_kwh": (
                        _var_var_199.prediction_kwh if _var_var_199 else None
                    ),
                    "prediction_kwh_display": (
                        f"{_var_var_199.prediction_kwh:.2f}" if _var_var_199 else None
                    ),
                },
                "tomorrow": {
                    "date": (
                        _var_var_87.forecast_date.isoformat()
                        if _var_var_87 and _var_var_87.forecast_date
                        else None
                    ),
                    "prediction_kwh": (
                        _var_var_87.prediction_kwh if _var_var_87 else None
                    ),
                    "prediction_kwh_display": (
                        f"{_var_var_87.prediction_kwh:.2f}" if _var_var_87 else None
                    ),
                },
                "day_after_tomorrow": {
                    "date": (
                        _var_var_200.forecast_date.isoformat()
                        if _var_var_200 and _var_var_200.forecast_date
                        else None
                    ),
                    "prediction_kwh": (
                        _var_var_200.prediction_kwh if _var_var_200 else None
                    ),
                    "prediction_kwh_display": (
                        f"{_var_var_200.prediction_kwh:.2f}" if _var_var_200 else None
                    ),
                },
            }
        except Exception as _var_var_171:
            _LOGGER.error("Error loading forecasts from database: %s", _var_var_171)
            _var_var_169["data"]["forecasts"] = {
                "today": {"prediction_kwh": None, "prediction_kwh_display": None},
                "tomorrow": {
                    "date": None,
                    "prediction_kwh": None,
                    "prediction_kwh_display": None,
                },
                "day_after_tomorrow": {
                    "date": None,
                    "prediction_kwh": None,
                    "prediction_kwh_display": None,
                },
            }
        try:
            _var_var_201 = _var_var_183.isoformat()
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT cache_date, sunrise, sunset, daylight_hours\n                       FROM astronomy_cache\n                       WHERE cache_date >= ? AND cache_date <= ? AND hour = 12\n                       ORDER BY cache_date",
                    (_var_var_201, _var_var_157.isoformat()),
                ) as _var_var_33:
                    _var_var_202 = await _var_var_33.fetchall()
            if _var_var_202:
                _var_var_169["data"]["astronomy"] = {
                    _var_var_203["cache_date"]: {
                        "daylight_hours": _var_var_203["daylight_hours"],
                        "sunrise": _var_var_203["sunrise"],
                        "sunset": _var_var_203["sunset"],
                    }
                    for _var_var_203 in _var_var_202
                }
        except Exception as _var_var_171:
            _LOGGER.error("Error loading astronomy history from DB: %s", _var_var_171)
        try:
            _var_var_84 = _get_solar_reader()
            A_recovered_capture_0006 = ha_today(context.HASS)
            _var_var_83 = A_recovered_capture_0006 + timedelta(days=1)
            _var_var_204 = A_recovered_capture_0006 + timedelta(days=2)
            _var_var_205 = {}
            for _var_var_157 in (A_recovered_capture_0006, _var_var_83, _var_var_204):
                _var_var_206 = await _var_var_84.async_get_hourly_predictions(
                    target_date=_var_var_157
                )
                if _var_var_206:
                    _var_var_207 = []
                    for _var_var_191 in _var_var_206:
                        _var_var_207.append(
                            {
                                "hour": _var_var_191.target_hour,
                                "prediction_kwh": _var_var_191.prediction_kwh,
                                "actual_kwh": _var_var_191.actual_kwh,
                            }
                        )
                    _var_var_205[_var_var_157.isoformat()] = {
                        "date": _var_var_157.isoformat(),
                        "hourly": _var_var_207,
                        "total_kwh": sum(
                            (
                                _var_var_191.prediction_kwh
                                for _var_var_191 in _var_var_206
                            )
                        ),
                    }
            if _var_var_205:
                _var_var_169["data"]["multi_day_hourly"] = _var_var_205
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading multi-day hourly forecast from database: %s",
                _var_var_171,
            )
        return web.json_response(_var_var_169)


class SummaryDataView(HomeAssistantView):

    @local_only
    async def get(self, request: Request) -> Response:
        """Return a summary for the dashboard. @zara"""
        _var_var_215 = {
            "production": 0.0,
            "forecast": 0.0,
            "accuracy": 0.0,
            "forecast_accuracy_vs_sot_percent": 0.0,
            "forecast_error_vs_sot_percent": 0.0,
            "forecast_accuracy_vs_actual_percent": 0.0,
            "forecast_error_vs_actual_percent": 0.0,
            "forecast_absolute_error_kwh": 0.0,
            "forecast_relative_error_percent": 0.0,
            "evaluation_coverage_percent": 0.0,
            "excluded_hours_count": 0,
            "excluded_mppt_hours_count": 0,
            "evaluation_hours_count": 0,
            "production_candidate_hours_count": 0,
            "completed_candidate_hours_count": 0,
            "pending_candidate_hours_count": 0,
            "missing_actual_hours_count": 0,
            "excluded_weather_alert_hours_count": 0,
            "excluded_inverter_clipped_hours_count": 0,
            "excluded_reason_breakdown_json": "{}",
            "evaluation_actual_kwh": 0.0,
            "evaluation_predicted_kwh": 0.0,
            "evaluation_cutoff_hour": None,
            "evaluation_scope": None,
            "live_day_yield_delta_vs_forecast_percent": 0.0,
            "live_day_forecast_error_vs_sot_percent": 0.0,
            "live_day_forecast_accuracy_vs_sot_percent": 0.0,
            "peak_hour": None,
            "peak_kwh": 0.0,
            "forecast_tomorrow": 0.0,
        }
        _var_var_216 = {
            "total_production": 0.0,
            "total_forecast": 0.0,
            "avg_accuracy": 0.0,
            "avg_forecast_error_vs_sot_percent": 0.0,
            "avg_forecast_error_vs_actual_percent": 0.0,
            "avg_evaluation_coverage_percent": 0.0,
            "avg_excluded_mppt_hours": 0.0,
            "days_count": 0,
        }
        _var_var_169 = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "kpis": {},
            "today": _var_var_215.copy(),
            "week": _var_var_216.copy(),
        }
        A_recovered_capture_0006 = ha_today(context.HASS)
        _var_var_217 = A_recovered_capture_0006 - timedelta(days=7)
        _var_var_50 = _get_config()
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_184 = await _var_var_84.async_get_daily_summaries(
                start_date=_var_var_217, end_date=A_recovered_capture_0006
            )
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT hp.target_date,\n                              hp.target_hour,\n                              hp.prediction_kwh AS prediction_kwh,\n                              hp.actual_kwh,\n                              hp.mppt_throttled,\n                              hp.mppt_throttle_reason,\n                              hp.inverter_clipped,\n                              hp.exclude_from_learning,\n                              hp.exclude_from_clean_evaluation,\n                              hp.has_weather_alert,\n                              hp.weather_alert_type\n                       FROM hourly_predictions hp\n                       WHERE hp.target_date BETWEEN ? AND ?\n                       ORDER BY hp.target_date, hp.target_hour",
                    (_var_var_217.isoformat(), A_recovered_capture_0006.isoformat()),
                ) as _var_var_33:
                    _var_var_218 = await _var_var_33.fetchall()
            _var_var_219, _var_var_220 = _build_summary_week_from_hourly_rows(
                _var_var_218
            )
            _var_var_221 = next(
                (
                    _var_var_187
                    for _var_var_187 in _var_var_184
                    if _var_var_187.date == A_recovered_capture_0006
                ),
                None,
            )
            _var_var_222 = _var_var_219.get(A_recovered_capture_0006.isoformat())
            if _var_var_222:
                _var_var_169["today"] = _var_var_222
            elif _var_var_221:
                _var_var_169["today"] = {
                    "production": _var_var_221.actual_total_kwh,
                    "forecast": _var_var_221.predicted_total_kwh,
                    "accuracy": _var_var_221.accuracy_percent,
                    **_build_daily_summary_forecast_metrics(
                        _var_var_221.predicted_total_kwh,
                        _var_var_221.actual_total_kwh,
                        _var_var_221.evaluation_predicted_kwh,
                        _var_var_221.evaluation_actual_kwh,
                        _var_var_221.accuracy_percent,
                    ),
                    "evaluation_coverage_percent": _var_var_221.evaluation_coverage_percent,
                    "excluded_hours_count": _var_var_221.excluded_hours_count,
                    "excluded_mppt_hours_count": _var_var_221.excluded_mppt_hours_count,
                    "evaluation_hours_count": _var_var_221.evaluation_hours_count,
                    "production_candidate_hours_count": _var_var_221.production_candidate_hours_count,
                    "missing_actual_hours_count": _var_var_221.missing_actual_hours_count,
                    "excluded_weather_alert_hours_count": _var_var_221.excluded_weather_alert_hours_count,
                    "excluded_inverter_clipped_hours_count": _var_var_221.excluded_inverter_clipped_hours_count,
                    "excluded_reason_breakdown_json": _var_var_221.excluded_reason_breakdown_json,
                    "evaluation_actual_kwh": _var_var_221.evaluation_actual_kwh,
                    "evaluation_predicted_kwh": _var_var_221.evaluation_predicted_kwh,
                    "peak_hour": _var_var_221.peak_hour,
                    "peak_kwh": _var_var_221.peak_kwh,
                }
            if _var_var_220:
                _var_var_169["week"] = _var_var_220
            elif _var_var_184:
                _var_var_223 = [
                    _build_daily_summary_forecast_metrics(
                        _var_var_187.predicted_total_kwh,
                        _var_var_187.actual_total_kwh,
                        _var_var_187.evaluation_predicted_kwh,
                        _var_var_187.evaluation_actual_kwh,
                        _var_var_187.accuracy_percent,
                    )["forecast_accuracy_vs_sot_percent"]
                    for _var_var_187 in _var_var_184
                ]
                _var_var_224 = [
                    _build_daily_summary_forecast_metrics(
                        _var_var_187.predicted_total_kwh,
                        _var_var_187.actual_total_kwh,
                        _var_var_187.evaluation_predicted_kwh,
                        _var_var_187.evaluation_actual_kwh,
                        _var_var_187.accuracy_percent,
                    )["forecast_error_vs_sot_percent"]
                    for _var_var_187 in _var_var_184
                ]
                _var_var_225 = [
                    _build_daily_summary_forecast_metrics(
                        _var_var_187.predicted_total_kwh,
                        _var_var_187.actual_total_kwh,
                        _var_var_187.evaluation_predicted_kwh,
                        _var_var_187.evaluation_actual_kwh,
                        _var_var_187.accuracy_percent,
                    )["forecast_error_vs_actual_percent"]
                    for _var_var_187 in _var_var_184
                ]
                _var_var_226 = _average_present(_var_var_223)
                _var_var_227 = _average_present(
                    [
                        _var_var_187.evaluation_coverage_percent
                        for _var_var_187 in _var_var_184
                    ]
                )
                _var_var_169["week"] = {
                    "total_production": sum(
                        (_var_var_187.actual_total_kwh for _var_var_187 in _var_var_184)
                    ),
                    "total_forecast": sum(
                        (
                            _var_var_187.predicted_total_kwh
                            for _var_var_187 in _var_var_184
                        )
                    ),
                    "avg_accuracy": _var_var_226,
                    "avg_forecast_error_vs_sot_percent": _average_present(_var_var_224),
                    "avg_forecast_error_vs_actual_percent": _average_present(
                        _var_var_225
                    ),
                    "avg_evaluation_coverage_percent": _var_var_227,
                    "avg_excluded_mppt_hours": sum(
                        (
                            _var_var_187.excluded_mppt_hours_count
                            for _var_var_187 in _var_var_184
                        )
                    )
                    / len(_var_var_184),
                    "days_count": len(_var_var_184),
                }
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading daily summaries from database: %s", _var_var_171
            )
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT date, predicted_total_kwh, actual_total_kwh,\n                              accuracy_percent, evaluation_coverage_percent,\n                              excluded_mppt_hours_count\n                       FROM daily_summaries\n                       WHERE date < ?\n                       ORDER BY date DESC\n                       LIMIT 30",
                    (A_recovered_capture_0006.isoformat(),),
                ) as _var_var_33:
                    _var_var_228 = await _var_var_33.fetchall()
            _var_var_229 = [
                {
                    "date": _var_var_30["date"],
                    "predicted_kwh": _var_var_30["predicted_total_kwh"] or 0.0,
                    "actual_kwh": _var_var_30["actual_total_kwh"] or 0.0,
                    "accuracy": _var_var_30["accuracy_percent"],
                    "forecast_accuracy_vs_sot_percent": _var_var_30["accuracy_percent"],
                    "forecast_accuracy_vs_actual_percent": _var_var_30[
                        "accuracy_percent"
                    ],
                    "evaluation_coverage_percent": _var_var_30[
                        "evaluation_coverage_percent"
                    ],
                    "excluded_mppt_hours_count": _var_var_30[
                        "excluded_mppt_hours_count"
                    ]
                    or 0,
                }
                for _var_var_30 in _var_var_228
            ]
            _var_var_169["statistics"] = {}
            _var_var_230 = _build_statistics_window_from_history_rows(_var_var_229, 7)
            if _var_var_230 is not None:
                _var_var_169["statistics"]["last_7_days"] = _var_var_230
            _var_var_231 = _build_statistics_window_from_history_rows(_var_var_229, 30)
            if _var_var_231 is not None:
                _var_var_169["statistics"]["last_30_days"] = _var_var_231
        except Exception as _var_var_171:
            _LOGGER.debug("Could not load summary rolling statistics: %s", _var_var_171)
            _var_var_169["statistics"] = {}
        try:
            _var_var_169["panel_groups"] = (
                await StatisticsView()._get_panel_group_data()
            )
        except Exception as _var_var_171:
            _LOGGER.debug("Could not load summary panel groups: %s", _var_var_171)
            _var_var_169["panel_groups"] = {"available": False, "groups": {}}
        try:
            _var_var_169["outdoor_temperature"] = (
                await _get_summary_outdoor_temperature(_var_var_50)
            )
        except Exception as _var_var_171:
            _LOGGER.debug(
                "Could not load outdoor temperature summary: %s", _var_var_171
            )
            _var_var_169["outdoor_temperature"] = {
                "temperature_c": None,
                "source": None,
            }
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT total_price, average_today\n                       FROM GPM_current_price WHERE id = 1"
                ) as _var_var_33:
                    _var_var_232 = await _var_var_33.fetchone()
                if _var_var_232 and _var_var_232[0] is not None:
                    _var_var_169["kpis"]["price_current"] = _var_var_232[0]
                    if _var_var_232[1] is not None:
                        _var_var_169["kpis"]["price_avg"] = round(_var_var_232[1], 2)
                _var_var_233 = (
                    (ha_now(context.HASS) - timedelta(hours=48))
                    .replace(tzinfo=None)
                    .isoformat(timespec="seconds")
                )
                async with _var_var_32.execute(
                    "\n                    SELECT price_net, total_price\n                    FROM GPM_price_history\n                    WHERE timestamp >= ?\n                    ORDER BY timestamp\n                ",
                    (_var_var_233,),
                ) as _var_var_33:
                    _var_var_234 = await _var_var_33.fetchall()
            if _var_var_234:
                _var_var_235 = [
                    _var_var_203["total_price"]
                    for _var_var_203 in _var_var_234
                    if _var_var_203["total_price"]
                ]
                if _var_var_235:
                    if "price_current" not in _var_var_169["kpis"]:
                        _var_var_169["kpis"]["price_current"] = _var_var_235[-1]
                    if "price_avg" not in _var_var_169["kpis"]:
                        _var_var_169["kpis"]["price_avg"] = round(
                            sum(_var_var_235) / len(_var_var_235), 2
                        )
                    _var_var_169["kpis"]["price_min"] = min(_var_var_235)
                    _var_var_169["kpis"]["price_max"] = max(_var_var_235)
        except Exception as _var_var_171:
            _LOGGER.error("Error loading price KPIs from DB: %s", _var_var_171)
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_198 = await _var_var_84.async_get_model_state()
            if _var_var_198:
                _var_var_169["kpis"][
                    "ai_training_samples"
                ] = _var_var_198.training_samples
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading AI model state from database: %s", _var_var_171
            )

        def extract_time(iso_string: str | None) -> str | None:
            """Extract HH:MM from ISO string. @zara"""
            if not iso_string:
                return
            try:
                if "T" in iso_string:
                    _var_var_327 = iso_string.split("T")[1]
                    return _var_var_327[:5]
                if " " in iso_string:
                    _var_var_327 = iso_string.split(" ")[1]
                    return _var_var_327[:5]
                return iso_string[:5]
            except Exception:
                return

        _var_var_31 = ha_today(context.HASS).isoformat()
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT sunrise, sunset, daylight_hours\n                       FROM astronomy_cache\n                       WHERE cache_date = ? AND hour = 12",
                    (_var_var_31,),
                ) as _var_var_33:
                    _var_var_237 = await _var_var_33.fetchone()
            if _var_var_237:
                _var_var_10 = extract_time(_var_var_237["sunrise"])
                _var_var_11 = extract_time(_var_var_237["sunset"])
                _var_var_169["sun_times"] = {
                    "sunrise": _var_var_10,
                    "sunset": _var_var_11,
                }
                if _var_var_10 and _var_var_11:
                    _var_var_169["production_time"] = {
                        "active": True,
                        "start_time": _var_var_10,
                        "end_time": _var_var_11,
                        "duration_seconds": int(
                            (_var_var_237["daylight_hours"] or 0) * 3600
                        ),
                    }
            else:
                _var_var_238 = _today_sun_times_from_ha()
                if _var_var_238:
                    _var_var_169.update(_var_var_238)
        except Exception as _var_var_171:
            _LOGGER.error("Error loading astronomy data from DB: %s", _var_var_171)
            _var_var_238 = _today_sun_times_from_ha()
            if _var_var_238:
                _var_var_169.update(_var_var_238)
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_206 = await _var_var_84.async_get_hourly_predictions(
                target_date=A_recovered_capture_0006
            )
            if _var_var_206:
                _var_var_169["hourly_predictions"] = [
                    {
                        "hour": _var_var_191.target_hour,
                        "predicted": (
                            round(_var_var_191.prediction_kwh, 3)
                            if _var_var_191.prediction_kwh
                            else 0
                        ),
                        "actual": (
                            round(_var_var_191.actual_kwh, 3)
                            if _var_var_191.actual_kwh
                            else 0
                        ),
                    }
                    for _var_var_191 in _var_var_206
                ]
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading hourly predictions: %s", _var_var_171)
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT forecast_type, prediction_kwh, forecast_date\n                       FROM daily_forecasts\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date",
                    (ha_today(context.HASS).isoformat(),),
                ) as _var_var_33:
                    _var_var_239 = {}
                    for _var_var_30 in await _var_var_33.fetchall():
                        _var_var_239[_var_var_30[0]] = {
                            "kwh": round(_var_var_30[1], 1),
                            "date": _var_var_30[2],
                        }
                    _var_var_169["daily_forecasts"] = _var_var_239
                    if "tomorrow" in _var_var_239:
                        _var_var_169["today"]["forecast_tomorrow"] = _var_var_239[
                            "tomorrow"
                        ]["kwh"]
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading daily forecasts: %s", _var_var_171)
        try:
            _var_var_50 = {}
            for _var_var_240 in context.HASS.data.get(DOMAIN, {}).values():
                if not not isinstance(_var_var_240, dict) and (
                    not not "config" in _var_var_240
                ):
                    _var_var_50 = _var_var_240["config"]
                    break
            _var_var_241 = effective_price_mode(context.HASS, _var_var_50)
            if _var_var_241 == MODE_LEGACY_FIXED:
                _var_var_169["kpis"]["price_current"] = _get_fixed_total_price(
                    _var_var_50
                )
        except Exception:
            pass
        try:
            async with _get_db() as _var_var_32:
                A_recovered_capture_0004 = await _table_columns(
                    _var_var_32, "stats_daily_energy"
                )
                _var_var_242 = (
                    "solar_yield_kwh",
                    "home_consumption_kwh",
                    "grid_import_kwh",
                    "grid_export_kwh",
                    "battery_to_house_kwh",
                    "solar_to_house_kwh",
                    "self_consumption_kwh",
                    "autarkie_percent",
                    "peak_solar_w",
                    "peak_solar_time",
                    "total_cost_eur",
                    "feed_in_revenue_eur",
                    "savings_eur",
                    "solar_to_battery_kwh",
                    "grid_to_house_kwh",
                    "grid_to_battery_kwh",
                )
                _var_var_243 = ", ".join(
                    (
                        _column_expr(A_recovered_capture_0004, _var_var_155)
                        for _var_var_155 in _var_var_242
                    )
                )
                async with _var_var_32.execute(
                    f"SELECT {_var_var_243}\n                       FROM stats_daily_energy WHERE date = ?",
                    (_var_var_31,),
                ) as _var_var_33:
                    _var_var_30 = await _var_var_33.fetchone()
                    if _var_var_30:
                        _var_var_244 = await _energy_correction_projection(_var_var_31)
                        _var_var_169["daily_stats"] = {
                            "solar_yield_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "solar_yield_day_kwh",
                                    _var_var_30[0],
                                ),
                                2,
                            ),
                            "home_consumption_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "home_consumption_day_kwh",
                                    _var_var_30[1],
                                ),
                                2,
                            ),
                            "grid_import_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "grid_import_day_kwh",
                                    _var_var_30[2],
                                ),
                                2,
                            ),
                            "grid_export_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "grid_export_day_kwh",
                                    _var_var_30[3],
                                ),
                                2,
                            ),
                            "battery_to_house_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "battery_to_house_day_kwh",
                                    _var_var_30[4],
                                ),
                                2,
                            ),
                            "solar_to_house_kwh": round(
                                corrected_value(
                                    _var_var_244,
                                    _var_var_31,
                                    "solar_to_house_day_kwh",
                                    _var_var_30[5],
                                ),
                                2,
                            ),
                            "self_consumption_kwh": round(_var_var_30[6] or 0, 2),
                            "autarkie_percent": round(_var_var_30[7] or 0, 1),
                            "peak_solar_w": round(_var_var_30[8] or 0, 0),
                            "peak_solar_time": _var_var_30[9] or "",
                            "total_cost_eur": round(_var_var_30[10] or 0, 2),
                            "feed_in_revenue_eur": round(_var_var_30[11] or 0, 2),
                            "savings_eur": round(_var_var_30[12] or 0, 2),
                            "solar_to_battery_kwh": round(_var_var_30[13] or 0, 2),
                            "grid_to_house_kwh": round(_var_var_30[14] or 0, 2),
                            "grid_to_battery_kwh": round(_var_var_30[15] or 0, 2),
                        }
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading daily stats: %s", _var_var_171)
        try:
            _var_var_245 = SFMLDataReader(context.HASS)
            _var_var_246 = await _get_today_yield_from_db()
            if _var_var_246 is None:
                _var_var_246 = await _var_var_245.get_live_yield()
            if _var_var_246 is not None:
                _var_var_48 = _var_var_169.setdefault("daily_stats", {})
                _var_var_48["solar_yield_kwh"] = round(_var_var_246, 2)
                _var_var_48["solar_yield_source"] = "sfml_live"
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading SFML live yield for summary: %s", _var_var_171)
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT ppg.group_name,\n                              SUM(ppg.prediction_kwh) as pred,\n                              SUM(ppg.actual_kwh) as actual\n                       FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date = ?\n                       GROUP BY ppg.group_name",
                    (_var_var_31,),
                ) as _var_var_33:
                    _var_var_211 = await _var_var_33.fetchall()
                    if _var_var_211:
                        _var_var_169["panel_groups_daily"] = [
                            {
                                "name": _var_var_203[0],
                                "predicted_kwh": round(_var_var_203[1] or 0, 2),
                                "actual_kwh": round(_var_var_203[2] or 0, 2),
                            }
                            for _var_var_203 in _var_var_211
                        ]
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading panel group totals: %s", _var_var_171)
        try:
            from ..core.dev_mode import is_dev_active as _var_var_147

            if _var_var_147(context.HASS):
                async with _get_db() as _var_var_32:
                    async with _var_var_32.execute(
                        "SELECT active_model, training_samples, accuracy, rmse, last_trained FROM ai_learned_weights_meta LIMIT 1"
                    ) as _var_var_33:
                        _var_var_30 = await _var_var_33.fetchone()
                        if _var_var_30:
                            _var_var_169["ai_status"] = {
                                "model": _var_var_30[0],
                                "training_samples": _var_var_30[1],
                                "accuracy": round((_var_var_30[2] or 0) * 100, 1),
                                "rmse": round(_var_var_30[3] or 0, 4),
                                "last_trained": _var_var_30[4],
                            }
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading AI status: %s", _var_var_171)
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT SUM(theoretical_max_kwh) FROM astronomy_cache WHERE cache_date = ?",
                    (_var_var_31,),
                ) as _var_var_33:
                    _var_var_30 = await _var_var_33.fetchone()
                    if _var_var_30 and _var_var_30[0]:
                        _var_var_169["theoretical_max_kwh"] = round(_var_var_30[0], 2)
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading theoretical max: %s", _var_var_171)
        try:
            _var_var_247 = await _energy_correction_projection("2020-01-01")
            _var_var_248 = await _corrected_daily_energy_values("2020-01-01")
            _var_var_249 = {}
            for _var_var_69, _var_var_250 in _var_var_247.items():
                if (
                    "solar_yield_day_kwh" not in _var_var_250
                    or _var_var_69 not in _var_var_248
                ):
                    continue
                A_recovered_capture_0005 = _var_var_69[:7]
                _var_var_251 = _var_var_248[_var_var_69]
                _var_var_249[A_recovered_capture_0005] = (
                    _var_var_249.get(A_recovered_capture_0005, 0.0)
                    + _var_var_251["solar_yield_day_kwh"]
                    - _var_var_251["raw_solar_yield_kwh"]
                )
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT strftime('%Y', date) as year,\n                              strftime('%m', date) as month,\n                              SUM(actual_total_kwh) as total_kwh,\n                              COUNT(*) as days\n                       FROM daily_summaries\n                       GROUP BY year, month\n                       ORDER BY year, month"
                ) as _var_var_33:
                    _var_var_211 = await _var_var_33.fetchall()
                    if _var_var_211:
                        _var_var_169["monthly_by_year"] = [
                            {
                                "year": int(_var_var_203[0]),
                                "month": int(_var_var_203[1]),
                                "total_kwh": round(
                                    (_var_var_203[2] or 0)
                                    + _var_var_249.get(
                                        f"{int(_var_var_203[0]):04d}-{int(_var_var_203[1]):02d}",
                                        0.0,
                                    ),
                                    2,
                                ),
                                "days": _var_var_203[3],
                            }
                            for _var_var_203 in _var_var_211
                        ]
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading monthly by year: %s", _var_var_171)
        try:
            async with _get_db() as _var_var_32:
                _var_var_252 = {}
                _var_var_253 = None
                for _var_var_254 in context.HASS.data.get(DOMAIN, {}).values():
                    if not isinstance(_var_var_254, dict):
                        continue
                    if (
                        _var_var_253 is None
                        and "monthly_tariff_manager" in _var_var_254
                    ):
                        _var_var_253 = _var_var_254["monthly_tariff_manager"]
                    if not _var_var_252 and "config" in _var_var_254:
                        _var_var_252 = _var_var_254["config"]
                _var_var_255, _var_var_256 = calculate_billing_period(
                    ha_today(context.HASS),
                    _var_var_252.get(
                        CONF_BILLING_START_MONTH, DEFAULT_BILLING_START_MONTH
                    ),
                    _var_var_252.get(CONF_BILLING_START_DAY, DEFAULT_BILLING_START_DAY),
                )
                _var_var_257 = _var_var_255.isoformat()
                _var_var_258 = min(_var_var_256, ha_today(context.HASS)).isoformat()
                A_recovered_capture_0000 = await _corrected_daily_energy_values(
                    _var_var_257, _var_var_258
                )
                _var_var_260 = await _energy_correction_projection(_var_var_257)
                async with _var_var_32.execute(
                    "SELECT strftime('%Y-%m', date) as month,\n                              SUM(home_consumption_kwh) as consumption,\n                              SUM(solar_yield_kwh) as solar,\n                              SUM(self_consumption_kwh) as self_cons,\n                              SUM(solar_to_house_kwh) as solar_to_house,\n                              SUM(battery_to_house_kwh) as bat_to_house,\n                              SUM(grid_to_house_kwh) as grid_to_house,\n                              SUM(smartmeter_import_kwh) as sm_import,\n                              SUM(grid_export_kwh) as grid_export,\n                              SUM(smartmeter_export_kwh) as sm_export,\n                              SUM(home_consumption_kwh) as total_consumption_for_autarkie,\n                              COUNT(*) as days,\n                              SUM(grid_to_battery_kwh) as grid_to_battery\n                       FROM stats_daily_energy\n                       WHERE date >= ? AND date <= ?\n                       GROUP BY month\n                       ORDER BY month",
                    (_var_var_257, _var_var_258),
                ) as _var_var_33:
                    _var_var_261 = await _var_var_33.fetchall()
                try:
                    _var_var_262 = float(
                        _var_var_252.get(CONF_BILLING_BASE_FEE, 0) or 0
                    )
                except (TypeError, ValueError):
                    _var_var_262 = 0.0
                try:
                    _var_var_263 = float(
                        _var_var_252.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF)
                        or 0
                    )
                except (TypeError, ValueError):
                    _var_var_263 = DEFAULT_FEED_IN_TARIFF
                _var_var_263 = max(0.0, _var_var_263)
                try:
                    _var_var_264 = float(_get_fixed_total_price(_var_var_252))
                except (TypeError, ValueError):
                    _var_var_264 = (
                        DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
                    )
                _var_var_265 = effective_price_mode(context.HASS, _var_var_252)
                _var_var_266 = {}
                try:
                    _var_var_267 = await _table_columns(
                        _var_var_32, "stats_hourly_billing"
                    )
                    _var_var_268 = (
                        "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
                        if "price_ct_kwh" in _var_var_267
                        else "?"
                    )
                    if "price_ct_kwh" in _var_var_267:
                        _var_var_269 = "SUM(CASE WHEN price_ct_kwh IS NOT NULL AND CAST(price_ct_kwh AS REAL) != 0 THEN COALESCE(grid_import_cost_ct, COALESCE(grid_import_kwh, 0) * price_ct_kwh, 0) ELSE 0 END) / 100.0"
                        _var_var_270 = "SUM(CASE WHEN price_ct_kwh IS NOT NULL AND CAST(price_ct_kwh AS REAL) != 0 THEN COALESCE(grid_import_kwh, 0) ELSE 0 END)"
                    else:
                        _var_var_269 = "0"
                        _var_var_270 = "0"
                    async with _var_var_32.execute(
                        f"SELECT strftime('%Y-%m', date) as month,\n                                  SUM(grid_import_cost_ct) / 100.0 as cost_eur,\n                                  SUM(grid_import_kwh) as import_kwh,\n                                  SUM(grid_export_kwh) as export_kwh,\n                                  SUM(COALESCE(feed_in_revenue_ct, 0)) / 100.0 as feed_in_revenue_eur,\n                                  SUM(\n                                      (\n                                          COALESCE(solar_to_house_kwh, 0)\n                                          + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                      )\n                                      * {_var_var_268}\n                                  ) / 100.0 as self_savings_eur,\n                                  SUM(\n                                      COALESCE(solar_to_house_kwh, 0)\n                                      + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                  ) as self_consumption_kwh,\n                                  COUNT(*) as hours,\n                                  {_var_var_269} as priced_cost_eur,\n                                  {_var_var_270} as priced_import_kwh\n                           FROM stats_hourly_billing\n                           WHERE date >= ? AND date <= ?\n                           GROUP BY month\n                           ORDER BY month",
                        (_var_var_264, _var_var_257, _var_var_258),
                    ) as _var_var_33:
                        for _var_var_203 in await _var_var_33.fetchall():
                            if not not _var_var_203[0] and (
                                not not _var_var_203[7] > 0
                            ):
                                _var_var_266[_var_var_203[0]] = {
                                    "cost_eur": round(_var_var_203[1] or 0, 2),
                                    "import_kwh": round(_var_var_203[2] or 0, 4),
                                    "export_kwh": round(_var_var_203[3] or 0, 4),
                                    "feed_in_revenue_eur": round(
                                        _var_var_203[4] or 0, 2
                                    ),
                                    "self_savings_eur": round(_var_var_203[5] or 0, 2),
                                    "self_consumption_kwh": round(
                                        _var_var_203[6] or 0, 4
                                    ),
                                    "hours": _var_var_203[7],
                                    "priced_cost_eur": (
                                        0.0
                                        if _var_var_203[8] is None
                                        else _var_var_203[8]
                                    ),
                                    "priced_import_kwh": (
                                        0.0
                                        if _var_var_203[9] is None
                                        else _var_var_203[9]
                                    ),
                                }
                except Exception:
                    pass
                try:
                    pass
                except Exception:
                    pass
                _var_var_271 = None
                if any(
                    (
                        energy_flows_changed(_var_var_250)
                        for _var_var_250 in A_recovered_capture_0000.values()
                    )
                ):
                    try:
                        _var_var_271 = await load_hourly_component_days(
                            _var_var_32, _var_var_257, _var_var_258, _var_var_264
                        )
                    except Exception:
                        _LOGGER.debug("Hourly correction base unavailable")
                        _var_var_271 = None
                _var_var_272 = {}
                try:
                    from ..core.grid_import_split import (
                        battery_is_configured as _var_var_273,
                        load_split_hours as _var_var_274,
                        summarize_import_split as _var_var_275,
                    )

                    if _var_var_273(_var_var_252):
                        _var_var_276 = await _var_var_274(
                            _var_var_32, _var_var_257, _var_var_258
                        )
                        _var_var_277 = {}
                        for _var_var_278 in _var_var_276.hours:
                            _var_var_277.setdefault(
                                str(_var_var_278["date"])[:7], []
                            ).append(_var_var_278)
                        for _var_var_279, _var_var_280 in _var_var_277.items():
                            _var_var_272[_var_var_279] = _var_var_275(
                                _var_var_280,
                                A_recovered_capture_0000,
                                _var_var_260,
                                fallback_price_ct=_var_var_264,
                                house_column=_var_var_276.house_column,
                                battery_column=_var_var_276.battery_column,
                                backfill_done=_var_var_276.backfill_done,
                            )
                except Exception:
                    _LOGGER.debug("Monthly import split unavailable")
                if _var_var_261:
                    _var_var_169["monthly_energy"] = []
                    for _var_var_203 in _var_var_261:
                        A_recovered_capture_0005 = _var_var_203[0]
                        _var_var_281 = (
                            round(_var_var_203[7] or 0, 1)
                            if (_var_var_203[7] or 0) > 0
                            else round(
                                (_var_var_203[6] or 0) + (_var_var_203[12] or 0), 1
                            )
                        )
                        _var_var_282 = (
                            round(_var_var_203[9] or 0, 1)
                            if (_var_var_203[9] or 0) > 0
                            else round(_var_var_203[8] or 0, 1)
                        )
                        _var_var_283 = [
                            _var_var_250
                            for _var_var_69, _var_var_250 in A_recovered_capture_0000.items()
                            if _var_var_69.startswith(f"{A_recovered_capture_0005}-")
                            if _var_var_69 <= _var_var_258
                        ]
                        _var_var_284 = corrected_month_sum(
                            _var_var_283, "solar_yield_day_kwh", _var_var_203[2] or 0.0
                        )
                        _var_var_285 = corrected_month_sum(
                            _var_var_283,
                            "home_consumption_day_kwh",
                            _var_var_203[1] or 0.0,
                        )
                        _var_var_286 = corrected_month_sum(
                            _var_var_283,
                            "solar_to_house_day_kwh",
                            _var_var_203[4] or 0.0,
                        )
                        _var_var_287 = corrected_month_sum(
                            _var_var_283,
                            "battery_to_house_day_kwh",
                            _var_var_203[5] or 0.0,
                        )
                        _var_var_288 = corrected_month_sum(
                            _var_var_283,
                            "grid_to_house_day_kwh",
                            _var_var_203[6] or 0.0,
                        )
                        _var_var_289 = corrected_month_sum(
                            _var_var_283,
                            "grid_to_battery_day_kwh",
                            _var_var_203[12] or 0.0,
                        )
                        if _var_var_283 and "grid_import_day_kwh" in _var_var_283[0]:
                            _var_var_281 = sum(
                                (
                                    _var_var_250["grid_import_day_kwh"]
                                    for _var_var_250 in _var_var_283
                                )
                            )
                            _var_var_282 = sum(
                                (
                                    _var_var_250["grid_export_day_kwh"]
                                    for _var_var_250 in _var_var_283
                                )
                            )
                        _var_var_290 = any(
                            (
                                _var_var_69.startswith(f"{A_recovered_capture_0005}-")
                                and bool(_var_var_250)
                                for _var_var_69, _var_var_250 in _var_var_260.items()
                            )
                        )
                        _var_var_291 = any(
                            (
                                _var_var_69.startswith(f"{A_recovered_capture_0005}-")
                                and bool(
                                    {"grid_import_day_kwh", "grid_export_day_kwh"}
                                    & _var_var_250.keys()
                                )
                                for _var_var_69, _var_var_250 in _var_var_260.items()
                            )
                        )
                        _var_var_292 = _var_var_266.get(A_recovered_capture_0005)
                        _var_var_293, _var_var_294 = (
                            int(_var_var_328)
                            for _var_var_328 in A_recovered_capture_0005.split("-")
                        )
                        _var_var_295 = calendar.monthrange(_var_var_293, _var_var_294)[
                            1
                        ]
                        _var_var_296 = min(_var_var_203[11] or 0, _var_var_295)
                        _var_var_297 = _var_var_264
                        _var_var_298 = _var_var_263
                        _var_var_299 = _var_var_262
                        _var_var_300 = "config_fallback"
                        if _var_var_253 is not None:
                            try:
                                _var_var_301 = await _var_var_253.get_monthly_fallbacks(
                                    _var_var_293,
                                    _var_var_294,
                                    import_price_ct=_var_var_264,
                                    feed_in_tariff_ct=_var_var_263,
                                    base_fee_eur=_var_var_262,
                                )
                                _var_var_302 = _var_var_301["import_price_ct"]
                                _var_var_303 = _var_var_301["feed_in_tariff_ct"]
                                _var_var_304 = _var_var_301["base_fee_eur"]
                                _var_var_297 = float(
                                    _var_var_302["value"]
                                    if _var_var_302["value"] is not None
                                    else _var_var_264
                                )
                                _var_var_298 = float(
                                    _var_var_303["value"] or _var_var_263
                                )
                                _var_var_299 = float(
                                    _var_var_304["value"] or _var_var_262
                                )
                                _var_var_300 = _var_var_302.get("source", "fixed_month")
                            except Exception as _var_var_5:
                                _LOGGER.debug(
                                    "Monthly tariff fallback failed for %s: %s",
                                    A_recovered_capture_0005,
                                    _var_var_5,
                                )
                        _var_var_305 = max(0.0, _var_var_287 - _var_var_289)
                        _var_var_306 = _var_var_286 + _var_var_305
                        _var_var_307 = 0.0
                        if is_fixed_like(_var_var_265):
                            _var_var_308 = date(
                                _var_var_293, _var_var_294, _var_var_295
                            )
                            _var_var_309 = date.fromisoformat(_var_var_258)
                            _var_var_310 = min(_var_var_308, _var_var_309).isoformat()
                            _var_var_311 = await monthly_table_energy_cost(
                                _var_var_32,
                                f"{A_recovered_capture_0005}-01",
                                _var_var_310,
                                _var_var_260,
                                _var_var_297,
                                price_mode=_var_var_265,
                                config=_var_var_252,
                            )
                            _var_var_312 = _var_var_311["energy_eur"]
                            _var_var_313 = _var_var_311["avg_ct"] or _var_var_297
                            _var_var_307 = float(
                                _var_var_311.get("unpriced_kwh") or 0.0
                            )
                            _var_var_314 = _var_var_306 * _var_var_313 / 100
                            _var_var_315 = _var_var_282 * _var_var_298 / 100
                            _var_var_316 = _var_var_311["cost_source"]
                        elif _var_var_292:
                            _var_var_317 = [
                                (_var_var_69, _var_var_250)
                                for _var_var_69, _var_var_250 in A_recovered_capture_0000.items()
                                if _var_var_69.startswith(
                                    f"{A_recovered_capture_0005}-"
                                )
                                if _var_var_69 <= _var_var_258
                            ]
                            _var_var_318 = any(
                                (
                                    energy_flows_changed(_var_var_250)
                                    for _var_var_329, _var_var_250 in _var_var_317
                                )
                            )
                            _var_var_319 = legacy_dynamic_month(
                                import_kwh=_var_var_281,
                                covered_import_kwh=_var_var_292["import_kwh"],
                                covered_import_cost=_var_var_292["cost_eur"],
                                self_kwh=_var_var_306,
                                covered_self_kwh=_var_var_292["self_consumption_kwh"],
                                covered_self_savings=_var_var_292["self_savings_eur"],
                                export_kwh=_var_var_282,
                                covered_export_kwh=_var_var_292["export_kwh"],
                                covered_export_revenue=_var_var_292[
                                    "feed_in_revenue_eur"
                                ],
                                month_price_ct=_var_var_297,
                                month_feed_in_ct=_var_var_298,
                            )
                            if _var_var_318 and _var_var_271 is not None:
                                _var_var_320 = dynamic_month_costs(
                                    [
                                        component_day(
                                            _var_var_250, _var_var_271.get(_var_var_69)
                                        )
                                        for _var_var_69, _var_var_250 in _var_var_317
                                    ],
                                    changed=True,
                                    legacy=_var_var_319,
                                    month_price_ct=_var_var_297,
                                    month_feed_in_ct=_var_var_298,
                                )
                            else:
                                _var_var_320 = _var_var_319
                            _var_var_321, _var_var_322, _var_var_307 = (
                                dynamic_month_average(
                                    priced_cost_eur=_var_var_292.get("priced_cost_eur")
                                    or 0.0,
                                    priced_import_kwh=_var_var_292.get(
                                        "priced_import_kwh"
                                    )
                                    or 0.0,
                                    total_import_kwh=_var_var_281,
                                )
                            )
                            _var_var_312 = _var_var_322
                            _var_var_314 = _var_var_320["self_savings"]
                            _var_var_315 = _var_var_320["feed_in_revenue"]
                            _var_var_313 = _var_var_321
                            _var_var_316 = (
                                "dynamic" if _var_var_307 <= 0.001 else "hybrid"
                            )
                        else:
                            _var_var_313 = None
                            _var_var_312 = 0.0
                            _var_var_307 = float(_var_var_281)
                            _var_var_314 = 0.0
                            _var_var_315 = _var_var_282 * _var_var_298 / 100
                            _var_var_316 = "estimated"
                        _var_var_323 = (
                            _var_var_299 * _var_var_296 / _var_var_295
                            if _var_var_295 > 0
                            else _var_var_299
                        )
                        _var_var_324 = _var_var_314 + _var_var_315
                        _var_var_325 = round(_var_var_312 + _var_var_323, 2)
                        _var_var_169["monthly_energy"].append(
                            {
                                "month": A_recovered_capture_0005,
                                "consumption_kwh": round(_var_var_285, 1),
                                "solar_kwh": round(_var_var_284, 1),
                                "self_consumption_kwh": round(_var_var_306, 1),
                                "solar_to_house_kwh": round(_var_var_286, 1),
                                "battery_to_house_kwh": round(_var_var_287, 1),
                                "grid_to_house_kwh": round(_var_var_288, 1),
                                "grid_to_battery_kwh": round(_var_var_289, 1),
                                "grid_import_kwh": _var_var_281,
                                "grid_export_kwh": _var_var_282,
                                "autarkie_percent": (
                                    round(
                                        min(100, _var_var_306 / _var_var_285 * 100), 1
                                    )
                                    if _var_var_285 > 0
                                    else 0
                                ),
                                "days": _var_var_203[11],
                                "cost_eur": _var_var_325,
                                "energy_cost_eur": round(_var_var_312, 2),
                                "base_fee_eur": round(_var_var_323, 2),
                                "feed_in_revenue_eur": round(_var_var_315, 2),
                                "self_consumption_savings_eur": round(_var_var_314, 2),
                                "savings_eur": round(_var_var_324, 2),
                                "total_savings_eur": round(_var_var_324, 2),
                                "avg_price_ct": (
                                    None
                                    if _var_var_313 is None
                                    else round(float(_var_var_313), 2)
                                ),
                                "unpriced_import_kwh": round(float(_var_var_307), 4),
                                "cost_source": _var_var_316,
                                "energy_corrections_applied": _var_var_290
                                or _var_var_291,
                                "costs_include_energy_corrections": True,
                                "correction_cost_contract": "costs_match_corrected_energy",
                                "tariff_source": _var_var_300,
                                "price_mode": _var_var_265,
                                "hours_without_price": 0 if _var_var_297 else 1,
                                "fallback_price_ct": round(_var_var_297, 2),
                                "feed_in_tariff_ct": round(_var_var_298, 2),
                                "import_split": _var_var_272.get(
                                    A_recovered_capture_0005
                                ),
                            }
                        )
                        continue
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading monthly energy: %s", _var_var_171)
        try:
            async with _get_db() as _var_var_32:
                async with _var_var_32.execute(
                    "SELECT peak_solar_w, peak_solar_time, date\n                       FROM stats_daily_energy\n                       WHERE peak_solar_w = (SELECT MAX(peak_solar_w) FROM stats_daily_energy)\n                       LIMIT 1"
                ) as _var_var_33:
                    _var_var_30 = await _var_var_33.fetchone()
                    if _var_var_30 and _var_var_30[0]:
                        _var_var_169["alltime_peak"] = {
                            "watts": round(_var_var_30[0], 0),
                            "time": _var_var_30[1] or "",
                            "date": _var_var_30[2] or "",
                        }
        except Exception as _var_var_171:
            _LOGGER.debug("Error loading alltime peak: %s", _var_var_171)
        try:
            from ..core.daily_costs import load_provenance as _var_var_326

            async with _get_db() as _var_var_32:
                _var_var_169["provenance"] = await _var_var_326(
                    _var_var_32,
                    _var_var_217,
                    A_recovered_capture_0006,
                    await _energy_correction_projection(_var_var_217.isoformat()),
                )
        except Exception as _var_var_5:
            _LOGGER.debug("Summary provenance unavailable: %s", _var_var_5)
        _var_var_169["hubble"] = await _build_hubble_summary(
            A_recovered_capture_0006, _var_var_169
        )
        return web.json_response(_var_var_169)


def _resolve_entity_id_by_unique_suffix(
    entity_domain: str,
    platform: str,
    suffixes: tuple[str, ...],
    candidates: tuple[str, ...] = (),
) -> str | None:
    if context.HASS is None:
        return
    for _var_var_344 in candidates:
        _var_var_38 = context.HASS.states.get(_var_var_344)
        if _var_var_38 is None:
            continue
        if not _var_var_38.state not in ("unknown", "unavailable", None):
            continue
        return _var_var_344
    try:
        _var_var_345 = er.async_get(context.HASS)
        for _var_var_346 in _var_var_345.entities.values():
            if (
                _var_var_346.domain != entity_domain
                or _var_var_346.platform != platform
            ):
                continue
            A_recovered_capture_0001 = str(_var_var_346.unique_id or "").lower()
            if not any(
                (
                    A_recovered_capture_0001.endswith(_var_var_349.lower())
                    for _var_var_349 in suffixes
                )
            ):
                continue
            if _var_var_346.disabled_by is not None:
                continue
            _var_var_38 = context.HASS.states.get(_var_var_346.entity_id)
            if _var_var_38 is not None:
                if _var_var_38.state not in ("unknown", "unavailable", None):
                    return _var_var_346.entity_id
    except Exception as _var_var_5:
        _LOGGER.debug("Entity registry status-chip lookup failed: %s", _var_var_5)
    _var_var_347 = tuple((_var_var_349.lower() for _var_var_349 in suffixes))
    for _var_var_38 in context.HASS.states.async_all(entity_domain):
        _var_var_348 = _var_var_38.entity_id.split(".", 1)[-1].lower()
        if not _var_var_348.endswith(_var_var_347):
            continue
        if not _var_var_38.state not in ("unknown", "unavailable", None):
            continue
        return _var_var_38.entity_id


class StatisticsView(HomeAssistantView):

    async def _get_panel_group_data(
        self, target_date: date | None = None
    ) -> dict[str, Any]:
        """Extract panel group predictions and actuals for a local day. @zara"""
        _var_var_50 = _get_config()
        if _var_var_50.get(CONF_SHOW_PANEL_GROUPS) is False:
            return {"available": False, "groups": {}}
        _var_var_384 = target_date or ha_today(context.HASS)
        _var_var_394 = _var_var_384 == ha_today(context.HASS)
        _var_var_330 = ha_now(context.HASS).hour if _var_var_394 else 23
        _var_var_395 = {}
        if _var_var_394:
            try:
                for _var_var_396 in await SFMLDataReader(
                    context.HASS
                ).get_live_panel_groups():
                    _var_var_395[normalized_group_key(_var_var_396.get("name"))] = (
                        _var_var_396
                    )
            except Exception as _var_var_171:
                _LOGGER.debug("Could not load live panel group SOT: %s", _var_var_171)
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_397 = await _var_var_84.async_get_panel_group_data(
                target_date=_var_var_384
            )
            if not _var_var_397:
                _var_var_397 = await self._get_panel_group_forecast_fallback(
                    _var_var_384
                )
                if not _var_var_397 and (not _var_var_395):
                    return {
                        "available": False,
                        "target_date": _var_var_384.isoformat(),
                        "groups": {},
                    }
                if not _var_var_397:
                    return {
                        "available": True,
                        "target_date": _var_var_384.isoformat(),
                        "forecast_available": False,
                        "live_only": True,
                        "groups": self._build_live_panel_group_fallback(_var_var_395),
                    }
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading panel group data from database: %s", _var_var_171
            )
            _var_var_397 = await self._get_panel_group_forecast_fallback(_var_var_384)
            if not _var_var_397 and (not _var_var_395):
                return {
                    "available": False,
                    "target_date": _var_var_384.isoformat(),
                    "groups": {},
                }
            if not _var_var_397:
                return {
                    "available": True,
                    "target_date": _var_var_384.isoformat(),
                    "forecast_available": False,
                    "live_only": True,
                    "groups": self._build_live_panel_group_fallback(_var_var_395),
                }
        _var_var_50 = _get_config()
        _var_var_398 = _var_var_50.get(CONF_PANEL_GROUP_NAMES, {})
        if not isinstance(_var_var_398, dict):
            _var_var_398 = {}
        _var_var_399 = {}
        for _var_var_400, _var_var_207 in _var_var_397.items():
            _var_var_401 = _var_var_398.get(_var_var_400, _var_var_400)
            _var_var_396 = _var_var_395.get(
                normalized_group_key(_var_var_400)
            ) or _var_var_395.get(normalized_group_key(_var_var_401))
            _var_var_402 = None
            if _var_var_396 and _var_var_396.get("actual_today_kwh") is not None:
                try:
                    _var_var_402 = max(
                        0.0, float(_var_var_396.get("actual_today_kwh") or 0.0)
                    )
                except (TypeError, ValueError):
                    _var_var_402 = None
            _var_var_403 = {
                "name": _var_var_401,
                "original_name": _var_var_400,
                "prediction_day_kwh": 0.0,
                "prediction_until_now_kwh": 0.0,
                "actual_until_now_kwh": 0.0,
                "clean_prediction_until_now_kwh": 0.0,
                "clean_actual_until_now_kwh": 0.0,
                "raw_prediction_until_now_kwh": 0.0,
                "raw_actual_until_now_kwh": 0.0,
                "day_progress_percent": None,
                "forecast_until_now_accuracy_percent": None,
                "clean_accuracy_percent": None,
                "elapsed_hours_count": 0,
                "clean_hours_count": 0,
                "excluded_hours_count": 0,
                "missing_actual_hours_count": 0,
                "exclusion_reason_breakdown": {},
                "hourly": [],
                "forecast_available": True,
                "live_only": False,
            }
            _var_var_404 = 0.0
            _var_var_405 = None
            for _var_var_406 in _var_var_207:
                _var_var_407 = _var_var_406.prediction_kwh
                _var_var_17 = _var_var_406.actual_kwh
                _var_var_194 = (
                    _var_var_406.target_hour
                    if _var_var_406.target_hour is not None
                    else 0
                )
                _var_var_408 = _panel_group_exclusion_reason(_var_var_406)
                _var_var_409 = _var_var_408 is not None
                if _var_var_407 is not None:
                    _var_var_403["prediction_day_kwh"] += _var_var_407
                if _var_var_17 is not None:
                    _var_var_404 += _var_var_17
                if _var_var_194 <= _var_var_330:
                    _var_var_403["elapsed_hours_count"] += 1
                    if _var_var_407 is not None:
                        _var_var_403["prediction_until_now_kwh"] += _var_var_407
                        _var_var_403["raw_prediction_until_now_kwh"] += _var_var_407
                    if _var_var_17 is not None:
                        _var_var_403["actual_until_now_kwh"] += _var_var_17
                        _var_var_403["raw_actual_until_now_kwh"] += _var_var_17
                    else:
                        _var_var_403["missing_actual_hours_count"] += 1
                    if _var_var_409:
                        _var_var_403["excluded_hours_count"] += 1
                        _var_var_158 = _var_var_403["exclusion_reason_breakdown"]
                        _var_var_158[_var_var_408] = (
                            _var_var_158.get(_var_var_408, 0) + 1
                        )
                    elif _var_var_17 is not None:
                        _var_var_403["clean_hours_count"] += 1
                        if _var_var_407 is not None:
                            _var_var_403[
                                "clean_prediction_until_now_kwh"
                            ] += _var_var_407
                        _var_var_403["clean_actual_until_now_kwh"] += _var_var_17
                _var_var_410 = {
                    "hour": _var_var_194,
                    "prediction_kwh": _var_var_407,
                    "actual_kwh": _var_var_17,
                    "excluded": _var_var_409,
                    "exclusion_reason": _var_var_408,
                }
                _var_var_403["hourly"].append(_var_var_410)
                if _var_var_194 == _var_var_330:
                    _var_var_405 = _var_var_410
            _var_var_403["actual_source"] = "db_hourly_sum"
            _var_var_403["prediction_total_kwh"] = _var_var_403["prediction_day_kwh"]
            if _var_var_394 and _var_var_402 is not None:
                _var_var_411 = _var_var_402 - _var_var_404
                if _var_var_411 > 0.01 and _var_var_403["hourly"]:
                    _var_var_412 = _var_var_405
                    if _var_var_412 is None:
                        _var_var_413 = [
                            _var_var_30
                            for _var_var_30 in _var_var_403["hourly"]
                            if int(_var_var_30.get("hour") or -1) <= _var_var_330
                        ]
                        _var_var_412 = (
                            _var_var_413[-1]
                            if _var_var_413
                            else _var_var_403["hourly"][-1]
                        )
                    _var_var_412["actual_kwh"] = (
                        _var_var_412.get("actual_kwh") or 0.0
                    ) + _var_var_411
                    if int(_var_var_412.get("hour") or -1) <= _var_var_330:
                        _var_var_403["raw_actual_until_now_kwh"] += _var_var_411
                        if not _var_var_412.get("excluded"):
                            if (_var_var_412.get("actual_kwh") or 0.0) == _var_var_411:
                                _var_var_403["clean_hours_count"] += 1
                                _var_var_403["missing_actual_hours_count"] = max(
                                    0, _var_var_403["missing_actual_hours_count"] - 1
                                )
                                _var_var_403["clean_prediction_until_now_kwh"] += (
                                    _var_var_412.get("prediction_kwh") or 0.0
                                )
                            _var_var_403["clean_actual_until_now_kwh"] += _var_var_411
                _var_var_403["actual_until_now_kwh"] = _var_var_402
                _var_var_403["actual_total_kwh"] = _var_var_402
                _var_var_403["raw_actual_until_now_kwh"] = _var_var_402
                _var_var_403["actual_source"] = "actual_live_panel_groups"
                _var_var_403["live_power_w"] = (
                    _var_var_396.get("power") if _var_var_396 else None
                )
                _var_var_403["live_quality"] = (
                    _var_var_396.get("quality") if _var_var_396 else None
                )
                _var_var_403["live_updated_at"] = (
                    _var_var_396.get("updated_at") if _var_var_396 else None
                )
            else:
                _var_var_403["actual_total_kwh"] = _var_var_403["actual_until_now_kwh"]
            if _var_var_403["prediction_day_kwh"] > 0:
                _var_var_403["day_progress_percent"] = (
                    _var_var_403["actual_total_kwh"]
                    / _var_var_403["prediction_day_kwh"]
                    * 100
                )
            if (
                _var_var_403["raw_prediction_until_now_kwh"] > 0
                and _var_var_403["raw_actual_until_now_kwh"] > 0
            ):
                _var_var_414 = (
                    abs(
                        (
                            _var_var_403["raw_actual_until_now_kwh"]
                            - _var_var_403["raw_prediction_until_now_kwh"]
                        )
                        / _var_var_403["raw_prediction_until_now_kwh"]
                    )
                    * 100
                )
                _var_var_403["raw_accuracy_percent"] = max(
                    0, min(100, 100 - _var_var_414)
                )
                _var_var_403["forecast_until_now_accuracy_percent"] = _var_var_403[
                    "raw_accuracy_percent"
                ]
            else:
                _var_var_403["raw_accuracy_percent"] = None
            if (
                _var_var_403["clean_prediction_until_now_kwh"] > 0
                and _var_var_403["clean_actual_until_now_kwh"] > 0
            ):
                _var_var_415 = (
                    abs(
                        (
                            _var_var_403["clean_actual_until_now_kwh"]
                            - _var_var_403["clean_prediction_until_now_kwh"]
                        )
                        / _var_var_403["clean_prediction_until_now_kwh"]
                    )
                    * 100
                )
                _var_var_403["accuracy_percent"] = max(0, min(100, 100 - _var_var_415))
                _var_var_403["clean_accuracy_percent"] = _var_var_403[
                    "accuracy_percent"
                ]
            else:
                _var_var_403["accuracy_percent"] = None
            _var_var_399[_var_var_401] = _var_var_403
            continue
        return {
            "available": True,
            "target_date": _var_var_384.isoformat(),
            "forecast_available": True,
            "live_only": False,
            "groups": _var_var_399,
        }
