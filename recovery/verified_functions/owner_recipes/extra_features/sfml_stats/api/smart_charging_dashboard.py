# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import timedelta, timezone
from typing import Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.util import dt as dt_util
from . import context
from .common import (
    _column_expr,
    _corrected_daily_energy_values,
    _energy_correction_projection,
    _get_db,
    _get_fixed_total_price,
    _get_local_naive_range_for_local_date,
    _gpm_local_hour_key,
    _table_columns,
    local_only,
)
from ..core.corrected_energy import (
    corrected_day_cost,
    corrected_value,
    projected_import_cost_totals,
    window_autarky_average,
)
from ..utils.time_utils import (
    format_local_hour_key,
    ha_now,
    iter_ha_local_hours,
    local_dates_range_strings,
)
from ..core.price_mode import MODE_LEGACY_FIXED, effective_price_mode
from ..const import (
    DOMAIN,
    CONF_SENSOR_BATTERY_SOC,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_SMART_CHARGING_ENABLED,
    CONF_SMART_CHARGING_SWITCH,
    CONF_BATTERY_CAPACITY,
    CONF_MIN_SOC,
    CONF_MAX_SOC,
    CONF_BATTERY_SOC_SENSOR,
    DEFAULT_MIN_SOC,
    DEFAULT_MAX_SOC,
    configured_battery_capacity_kwh,
)


class SmartChargingDashboardView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return smart charging live data and historical charts. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            _var_var_1 = {}
            _var_var_2 = None
            _var_var_3 = None
            for _var_var_4 in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(_var_var_4, dict):
                    if "config" in _var_var_4:
                        _var_var_1 = _var_var_4["config"]
                    if "gpm_coordinator" in _var_var_4:
                        _var_var_2 = _var_var_4["gpm_coordinator"]
                    if "billing_calculator" in _var_var_4:
                        _var_var_3 = _var_var_4["billing_calculator"]
            _var_var_5 = bool(_var_var_1.get(CONF_SMART_CHARGING_ENABLED, False))
            _var_var_6 = _var_var_1.get(CONF_BATTERY_SOC_SENSOR) or _var_var_1.get(
                CONF_SENSOR_BATTERY_SOC
            )
            _var_var_7 = bool(
                _var_var_1.get(CONF_SENSOR_BATTERY_SOC)
                or _var_var_1.get(CONF_BATTERY_SOC_SENSOR)
            )
            _var_var_8 = {
                "enabled": _var_var_5,
                "active": False,
                "target_soc": None,
                "current_soc": None,
                "reason": "disabled",
                "decision": "not_load",
                "requested_grid_charge_kwh": 0.0,
                "effective_storage_cost_ct_kwh": None,
                "compared_future_price_ct_kwh": None,
                "effective_roundtrip_efficiency": None,
                "reserved_future_grid_charge_kwh": 0.0,
                "capacity_kwh": _var_var_1.get(CONF_BATTERY_CAPACITY),
                "min_soc": _var_var_1.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                "max_soc": _var_var_1.get(CONF_MAX_SOC, DEFAULT_MAX_SOC),
                "current_price": None,
                "force_charge_price": None,
                "max_price": None,
                "solar_forecast_today_kwh": None,
                "solar_forecast_tomorrow_kwh": None,
                "is_cheap": None,
                "is_force_price": False,
                "charging_since": None,
                "waiting_since": None,
                "next_cheap_hour": None,
                "next_cheap_price_ct": None,
            }
            if _var_var_2 and _var_var_2.data:
                _var_var_9 = _var_var_2.data
                _var_var_10 = _var_var_9.get("thresholds") or {}
                if isinstance(_var_var_10, dict):
                    _var_var_8["max_price"] = _var_var_10.get("max_price")
                    _var_var_8["force_charge_price"] = _var_var_10.get(
                        "force_charge_price"
                    )
                _var_var_8["active"] = _var_var_9.get("smart_charging_active", False)
                _var_var_8["target_soc"] = _var_var_9.get("smart_charging_target_soc")
                _var_var_8["current_soc"] = _var_var_9.get("smart_charging_current_soc")
                _var_var_8["reason"] = _var_var_9.get(
                    "smart_charging_reason", "unknown"
                )
                _var_var_8["decision"] = _var_var_9.get(
                    "smart_charging_decision", "not_load"
                )
                _var_var_8["requested_grid_charge_kwh"] = _var_var_9.get(
                    "smart_charging_requested_grid_charge_kwh", 0.0
                )
                _var_var_8["effective_storage_cost_ct_kwh"] = _var_var_9.get(
                    "smart_charging_effective_storage_cost_ct_kwh"
                )
                _var_var_8["compared_future_price_ct_kwh"] = _var_var_9.get(
                    "smart_charging_compared_future_price_ct_kwh"
                )
                _var_var_8["effective_roundtrip_efficiency"] = _var_var_9.get(
                    "smart_charging_effective_roundtrip_efficiency"
                )
                _var_var_8["reserved_future_grid_charge_kwh"] = _var_var_9.get(
                    "smart_charging_reserved_future_grid_charge_kwh", 0.0
                )
                _var_var_8["solar_forecast_today_kwh"] = _var_var_9.get(
                    "solar_forecast_today"
                )
                _var_var_8["solar_forecast_tomorrow_kwh"] = _var_var_9.get(
                    "solar_forecast_tomorrow"
                )
            _var_var_11 = effective_price_mode(context.HASS, _var_var_1)
            _var_var_12 = None
            if _var_var_11 == MODE_LEGACY_FIXED:
                _var_var_12 = _get_fixed_total_price(_var_var_1)
            else:
                try:
                    async with _get_db() as _var_var_13:
                        async with _var_var_13.execute(
                            "SELECT total_price FROM GPM_current_price WHERE id = 1"
                        ) as _var_var_14:
                            _var_var_15 = await _var_var_14.fetchone()
                        if _var_var_15 and _var_var_15[0] is not None:
                            _var_var_12 = _var_var_15[0]
                        else:
                            _var_var_16 = dt_util.now()
                            _var_var_17 = _var_var_16.hour
                            _var_var_18 = _var_var_16.date().isoformat()
                            _var_var_19, _var_var_20 = (
                                _get_local_naive_range_for_local_date(_var_var_18)
                            )
                            async with _var_var_13.execute(
                                "SELECT total_price FROM GPM_price_history\n                                   WHERE timestamp >= ? AND timestamp < ? AND hour = ?",
                                (_var_var_19, _var_var_20, _var_var_17),
                            ) as _var_var_14:
                                _var_var_15 = await _var_var_14.fetchone()
                            if _var_var_15:
                                _var_var_12 = _var_var_15[0]
                except Exception as _var_var_21:
                    _LOGGER.debug(
                        "Error reading current price for smart charging dashboard: %s",
                        _var_var_21,
                    )
            if _var_var_12 is None and _var_var_2 and _var_var_2.data:
                _var_var_12 = _var_var_2.data.get("total_price")
            _var_var_8["current_price"] = _var_var_12
            if _var_var_8["current_soc"] is None and _var_var_6:
                try:
                    _var_var_22 = context.HASS.states.get(_var_var_6)
                    if _var_var_22 is not None and _var_var_22.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        _var_var_8["current_soc"] = float(_var_var_22.state)
                except (ValueError, TypeError):
                    pass
            _var_var_23 = _var_var_1.get(CONF_SENSOR_BATTERY_SOC)
            if (
                _var_var_8["current_soc"] is None or _var_var_8["current_soc"] == 0.0
            ) and _var_var_23:
                try:
                    _var_var_24 = context.HASS.states.get(_var_var_23)
                    if _var_var_24 is not None and _var_var_24.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        _var_var_25 = float(_var_var_24.state)
                        if _var_var_25 > 1.0:
                            _var_var_8["current_soc"] = _var_var_25
                except (ValueError, TypeError):
                    pass
            _var_var_26 = dt_util.now().date()
            _var_var_27 = _var_var_26 - timedelta(days=7)
            _var_var_28 = _var_var_26.replace(day=1)
            if _var_var_3:
                try:
                    _var_var_28 = _var_var_3._get_billing_start_date(_var_var_1)
                except Exception:
                    pass
            _var_var_29 = {
                "today": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
                "week": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
                "period": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
            }
            try:
                async with _get_db() as _var_var_13:
                    _var_var_30 = await _table_columns(
                        _var_var_13, "stats_hourly_billing"
                    )
                    _var_var_31 = (
                        "COALESCE(price_ct_kwh, 0.0)"
                        if "price_ct_kwh" in _var_var_30
                        else "0.0"
                    )
                    async with _var_var_13.execute(
                        f"\n                        SELECT \n                            date,\n                            COALESCE(grid_to_battery_kwh, 0.0),\n                            COALESCE(solar_to_battery_kwh, 0.0),\n                            COALESCE(battery_to_house_kwh, 0.0),\n                            {_var_var_31}\n                        FROM stats_hourly_billing\n                        ORDER BY hour_key ASC\n                        "
                    ) as _var_var_14:
                        _var_var_32 = await _var_var_14.fetchall()
                _var_var_33 = 0.0
                _var_var_34 = 0.0
                _var_var_35 = 0.0
                _var_var_36 = 0.0
                _var_var_37 = 0.0
                _var_var_38 = 0.0
                _var_var_18 = _var_var_26.isoformat()
                _var_var_39 = 0.0
                _var_var_40 = 0.0
                _var_var_41 = 0.0
                _var_var_42 = _var_var_27.isoformat()
                _var_var_43 = 0.0
                _var_var_44 = 0.0
                _var_var_45 = 0.0
                _var_var_46 = _var_var_28.isoformat()
                _var_var_47 = {}
                _var_var_48 = {}
                for _var_var_15 in _var_var_32:
                    _var_var_49 = _var_var_15[0]
                    _var_var_50 = _var_var_15[1]
                    _var_var_51 = _var_var_15[2]
                    _var_var_52 = _var_var_15[3]
                    _var_var_53 = _var_var_15[4]
                    if _var_var_50 + _var_var_51 > 0.0:
                        _var_var_54 = _var_var_33
                        _var_var_55 = _var_var_54 + _var_var_50 + _var_var_51
                        _var_var_56 = (
                            _var_var_54 * _var_var_34 + _var_var_50
                        ) / _var_var_55
                        _var_var_57 = _var_var_54 * _var_var_34 * _var_var_35
                        _var_var_58 = _var_var_50 * _var_var_53
                        _var_var_59 = _var_var_54 * _var_var_34 + _var_var_50
                        if _var_var_59 > 0.0:
                            _var_var_60 = (_var_var_57 + _var_var_58) / _var_var_59
                        else:
                            _var_var_60 = 0.0
                        _var_var_33 = _var_var_55
                        _var_var_34 = _var_var_56
                        _var_var_35 = _var_var_60
                    _var_var_61 = 0.0
                    if _var_var_52 > 0.0:
                        _var_var_62 = _var_var_33 * _var_var_34
                        _var_var_63 = min(_var_var_52 * _var_var_34, _var_var_62)
                        if _var_var_53 > _var_var_35:
                            _var_var_61 = _var_var_63 * (_var_var_53 - _var_var_35)
                        _var_var_33 = max(0.0, _var_var_33 - _var_var_52)
                        if _var_var_33 <= 0.0:
                            _var_var_34 = 0.0
                            _var_var_35 = 0.0
                    if _var_var_49 >= _var_var_18:
                        _var_var_36 += _var_var_50
                        _var_var_37 += _var_var_50 * _var_var_53
                        _var_var_38 += _var_var_61
                    if _var_var_49 >= _var_var_42:
                        _var_var_39 += _var_var_50
                        _var_var_40 += _var_var_50 * _var_var_53
                        _var_var_41 += _var_var_61
                    if _var_var_49 >= _var_var_46:
                        _var_var_43 += _var_var_50
                        _var_var_44 += _var_var_50 * _var_var_53
                        _var_var_45 += _var_var_61
                    _var_var_47[_var_var_49] = (
                        _var_var_47.get(_var_var_49, 0.0) + _var_var_50
                    )
                    _var_var_48[_var_var_49] = (
                        _var_var_48.get(_var_var_49, 0.0) + _var_var_50 * _var_var_53
                    )
                    continue
                _var_var_64 = {"today": 0.0, "week": 0.0, "period": 0.0}
                _var_var_65 = await _energy_correction_projection(_var_var_46)
                async with _get_db() as _var_var_13:
                    _var_var_66 = await _table_columns(
                        _var_var_13, "stats_daily_energy"
                    )
                    _var_var_67 = _column_expr(
                        _var_var_66, "total_cost_eur", default="0.0"
                    )
                    async with _var_var_13.execute(
                        f"\n                        SELECT\n                            date,\n                            MAX(\n                                COALESCE(grid_to_battery_kwh, 0.0),\n                                COALESCE(battery_charge_grid_kwh, 0.0)\n                            ) AS grid_charge_kwh,\n                            {_var_var_67},\n                            COALESCE(\n                                NULLIF(grid_import_kwh, 0),\n                                NULLIF(smartmeter_import_kwh, 0),\n                                NULLIF(grid_to_house_kwh, 0),\n                                0.0\n                            ) AS import_kwh\n                        FROM stats_daily_energy\n                        WHERE date >= ?\n                        ORDER BY date\n                        ",
                        (_var_var_46,),
                    ) as _var_var_14:
                        _var_var_68 = await _var_var_14.fetchall()
                for _var_var_69 in _var_var_68:
                    _var_var_49 = _var_var_69[0]
                    _var_var_70 = float(_var_var_69[1] or 0.0)
                    _var_var_71 = _var_var_47.get(_var_var_49, 0.0)
                    _var_var_72 = _var_var_70 - _var_var_71
                    if _var_var_72 <= 0.01:
                        continue
                    _var_var_73 = 0.0
                    _var_var_74 = float(_var_var_69[3] or 0.0)
                    _var_var_75 = float(_var_var_69[2] or 0.0)
                    _var_var_76 = _var_var_65.get(_var_var_49, {})
                    _var_var_77 = float(
                        _var_var_76.get("grid_import_day_kwh", _var_var_74) or 0.0
                    )
                    _var_var_78 = corrected_day_cost(
                        _var_var_74, _var_var_77, _var_var_75
                    )
                    _var_var_79 = _var_var_77
                    if _var_var_79 > 0.0 and _var_var_78 > 0.0:
                        _var_var_73 = _var_var_78 * 100.0 / _var_var_79
                    if _var_var_73 <= 0.0:
                        _var_var_80 = _var_var_47.get(_var_var_49, 0.0)
                        if _var_var_80 > 0.0:
                            _var_var_73 = (
                                _var_var_48.get(_var_var_49, 0.0) / _var_var_80
                            )
                    if _var_var_73 <= 0.0:
                        _var_var_73 = _var_var_12 or _get_fixed_total_price(_var_var_1)
                    _var_var_81 = _var_var_72 * _var_var_73
                    if _var_var_49 >= _var_var_18:
                        _var_var_36 += _var_var_72
                        _var_var_37 += _var_var_81
                        _var_var_64["today"] += _var_var_72
                    if _var_var_49 >= _var_var_42:
                        _var_var_39 += _var_var_72
                        _var_var_40 += _var_var_81
                        _var_var_64["week"] += _var_var_72
                    if _var_var_49 >= _var_var_46:
                        _var_var_43 += _var_var_72
                        _var_var_44 += _var_var_81
                        _var_var_64["period"] += _var_var_72
                _var_var_29["today"] = {
                    "charged_kwh": round(_var_var_36, 2),
                    "avg_price_ct": (
                        round(_var_var_37 / _var_var_36, 2)
                        if _var_var_36 > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(_var_var_38 / 100.0, 2),
                    "daily_fallback_kwh": round(_var_var_64["today"], 2),
                }
                _var_var_29["week"] = {
                    "charged_kwh": round(_var_var_39, 2),
                    "avg_price_ct": (
                        round(_var_var_40 / _var_var_39, 2)
                        if _var_var_39 > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(_var_var_41 / 100.0, 2),
                    "daily_fallback_kwh": round(_var_var_64["week"], 2),
                }
                _var_var_29["period"] = {
                    "charged_kwh": round(_var_var_43, 2),
                    "avg_price_ct": (
                        round(_var_var_44 / _var_var_43, 2)
                        if _var_var_43 > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(_var_var_45 / 100.0, 2),
                    "daily_fallback_kwh": round(_var_var_64["period"], 2),
                }
            except Exception as _var_var_82:
                _LOGGER.error(
                    "Error calculating smart charging KPIs: %s",
                    _var_var_82,
                    exc_info=True,
                )
            _var_var_83 = []
            _var_var_84 = None
            _var_var_85 = None
            if _var_var_2 and isinstance(getattr(_var_var_2, "data", None), dict):
                _var_var_86 = _var_var_2.data.get("thresholds") or {}
                if isinstance(_var_var_86, dict):
                    _var_var_84 = _var_var_86.get("max_price")
                    _var_var_85 = _var_var_86.get("force_charge_price")
                    _var_var_8["cheapest_hours"] = _var_var_86.get("cheapest_hours")
                    _var_var_8["below_average_pct"] = _var_var_86.get(
                        "below_average_pct"
                    )
                _var_var_8["mode"] = _var_var_2.data.get("smart_charging_mode")
                _var_var_8["is_force_price"] = bool(
                    _var_var_2.data.get("is_force_price")
                )
                _var_var_8["effective_threshold_ct"] = _var_var_2.data.get(
                    "effective_threshold_ct"
                )
                _var_var_8["threshold_mode"] = _var_var_2.data.get("threshold_mode")
                from .smart_charging import live_is_cheap as _var_var_87

                _var_var_8["is_cheap"] = _var_var_87(_var_var_2.data)
            from .smart_charging import (
                dashboard_gpm_meta as _var_var_88,
                future_hour_is_gpm_cheap as _var_var_89,
                gpm_chart_source as _var_var_90,
                gpm_forecast_cheap_by_day as _var_var_91,
                live_next_cheap_fields as _var_var_92,
                live_switch_timestamps as _var_var_93,
            )

            _var_var_94 = _var_var_88(_var_var_2)
            _var_var_95 = _var_var_90(_var_var_94)
            _var_var_96 = (
                _var_var_2.data
                if _var_var_2 is not None
                and isinstance(getattr(_var_var_2, "data", None), dict)
                else None
            )
            _var_var_97 = _var_var_91(_var_var_96)
            _var_var_98 = _var_var_8.get("effective_threshold_ct")
            try:
                _var_var_99 = ha_now(context.HASS)
                _var_var_100 = _var_var_99 - timedelta(hours=24)
                _var_var_101 = _var_var_99 + timedelta(hours=24)
                _var_var_102 = _var_var_100.replace(tzinfo=None)
                _var_var_103 = _var_var_99.date()
                _var_var_104 = _var_var_103 + timedelta(days=1)
                _var_var_105 = {}
                async with _get_db() as _var_var_13:
                    _var_var_30 = await _table_columns(
                        _var_var_13, "stats_hourly_billing"
                    )
                    _var_var_31 = (
                        "hb.price_ct_kwh" if "price_ct_kwh" in _var_var_30 else "0.0"
                    )
                    async with _var_var_13.execute(
                        f"\n                        SELECT \n                            hb.hour_key,\n                            hb.grid_to_battery_kwh,\n                            hb.solar_yield_kwh,\n                            {_var_var_31},\n                            hb.home_consumption_kwh\n                        FROM stats_hourly_billing hb\n                        WHERE hb.hour_key >= ?\n                        ORDER BY hb.hour_key\n                        ",
                        (_var_var_102.strftime("%Y-%m-%dT%H:00"),),
                    ) as _var_var_14:
                        _var_var_32 = await _var_var_14.fetchall()
                        for _var_var_106 in _var_var_32:
                            _var_var_107 = _var_var_106[0][:16].replace(" ", "T")
                            _var_var_105[_var_var_107] = {
                                "grid_to_battery_kwh": _var_var_106[1],
                                "solar_yield_kwh": _var_var_106[2],
                                "price_ct_kwh": _var_var_106[3],
                                "home_consumption_kwh": _var_var_106[4],
                            }
                _var_var_108 = []
                _var_var_18 = _var_var_26.isoformat()
                _var_var_109 = (_var_var_26 + timedelta(days=1)).isoformat()
                _var_var_19, _var_var_20 = _get_local_naive_range_for_local_dates(
                    [_var_var_18, _var_var_109]
                )
                async with _get_db() as _var_var_13:
                    async with _var_var_13.execute(
                        "\n                        SELECT \n                            timestamp,\n                            total_price\n                        FROM GPM_price_history\n                        WHERE timestamp >= ? AND timestamp < ?\n                        ORDER BY timestamp\n                        ",
                        (_var_var_19, _var_var_20),
                    ) as _var_var_14:
                        _var_var_32 = await _var_var_14.fetchall()
                        for _var_var_106 in _var_var_32:
                            _var_var_110 = _gpm_local_hour_key(_var_var_106[0])
                            if _var_var_110:
                                _var_var_108.append(
                                    {
                                        "hour_key": _var_var_110,
                                        "total_price": _var_var_106[1],
                                    }
                                )
                for _var_var_111 in iter_ha_local_hours(
                    _var_var_100, _var_var_101, context.HASS
                ):
                    _var_var_112 = _var_var_111.replace(tzinfo=None)
                    _var_var_0 = format_local_hour_key(_var_var_111, context.HASS)
                    _var_var_113 = _var_var_111 > _var_var_99
                    _var_var_114 = None
                    _var_var_115 = 0.0
                    _var_var_116 = 0.0
                    _var_var_117 = 0.0
                    if not _var_var_113:
                        _var_var_118 = _var_var_105.get(_var_var_0)
                        if _var_var_118:
                            _var_var_114 = _var_var_118["price_ct_kwh"]
                            _var_var_115 = _var_var_118["grid_to_battery_kwh"]
                            _var_var_116 = _var_var_118["solar_yield_kwh"]
                            _var_var_117 = _var_var_118["home_consumption_kwh"]
                    if _var_var_114 is None:
                        _var_var_119 = next(
                            (
                                _var_var_53
                                for _var_var_53 in _var_var_108
                                if _var_var_53["hour_key"] == _var_var_0
                            ),
                            None,
                        )
                        if _var_var_119:
                            _var_var_114 = _var_var_119["total_price"]
                    _var_var_120 = False
                    _var_var_121 = None
                    _var_var_122 = None
                    _var_var_123 = "unavailable"
                    if _var_var_113 and _var_var_95 == "gpm_threshold":
                        _var_var_123 = "gpm_threshold"
                        _var_var_124 = _var_var_111.date()
                        if _var_var_124 == _var_var_103:
                            _var_var_125 = "today"
                        elif _var_var_124 == _var_var_104:
                            _var_var_125 = "tomorrow"
                        else:
                            _var_var_125 = None
                        if _var_var_89(
                            _var_var_97, day_key=_var_var_125, hour=_var_var_112.hour
                        ):
                            _var_var_120 = True
                            _var_var_121 = "gpm_cheap"
                            _var_var_122 = _var_var_98
                    _var_var_83.append(
                        {
                            "hour_key": _var_var_0,
                            "hour_key_utc": _var_var_111.astimezone(
                                timezone.utc
                            ).isoformat(),
                            "timezone_offset": _var_var_111.strftime("%z"),
                            "hour": _var_var_112.hour,
                            "is_future": _var_var_113,
                            "price_ct_kwh": (
                                round(_var_var_114, 2)
                                if _var_var_114 is not None
                                else None
                            ),
                            "grid_to_battery_kwh": round(_var_var_115, 4),
                            "is_cheap": _var_var_120,
                            "grid_charge_recommended": _var_var_120,
                            "grid_charge_recommendation_reason": _var_var_121,
                            "grid_charge_recommendation_threshold_ct": (
                                round(_var_var_122, 2)
                                if _var_var_122 is not None
                                else None
                            ),
                            "grid_charge_recommendation_window": None,
                            "grid_charge_recommendation_source": _var_var_123,
                            "solar_yield_kwh": round(_var_var_116, 4),
                            "home_consumption_kwh": round(_var_var_117, 4),
                        }
                    )
                    continue
            except Exception as _var_var_21:
                _LOGGER.error(
                    "Error building smart charging chart data: %s", _var_var_21
                )
            _var_var_126 = None
            try:

                def _as_float(value: Any, fallback: float) -> float:
                    try:
                        if value is None or value == "":
                            return fallback
                        return float(value)
                    except (TypeError, ValueError):
                        return fallback

                _var_var_128 = (
                    _var_var_1.get(CONF_SENSOR_BATTERY_SOC) is not None
                    or _var_var_1.get(CONF_BATTERY_SOC_SENSOR) is not None
                )
                _var_var_129 = configured_battery_capacity_kwh(
                    _var_var_1.get(CONF_BATTERY_CAPACITY)
                )
                _var_var_130 = float(_var_var_129 or 0.0)
                _var_var_131 = max(
                    _as_float(
                        _var_var_1.get(CONF_BILLING_WORK_PRICE)
                        or _var_var_1.get(CONF_BILLING_FIXED_PRICE),
                        35.0,
                    ),
                    0.0,
                )
                _var_var_132 = max(
                    _as_float(
                        _var_var_1.get(CONF_FEED_IN_TARIFF), DEFAULT_FEED_IN_TARIFF
                    ),
                    0.0,
                )
                _var_var_133 = min(
                    max(
                        _as_float(_var_var_1.get("battery_roundtrip_efficiency"), 0.9),
                        0.0,
                    ),
                    1.0,
                )
                _var_var_134 = max(
                    _as_float(_var_var_1.get("battery_degradation_ct_per_kwh"), 0.0),
                    0.0,
                )
                _var_var_135 = _var_var_131 * _var_var_133
                _var_var_136 = max(0.0, _var_var_135 - _var_var_132 - _var_var_134)
                _var_var_137 = 0
                _var_var_138 = 0
                _var_var_139 = 0.0
                _var_var_140 = 0.0
                _var_var_141 = 0.0
                _var_var_142 = 0.0
                _var_var_143 = 0.0
                _var_var_144 = 0.0
                _var_var_145 = 0.0
                _var_var_146 = 0.0
                _var_var_147 = 0.0
                _var_var_148 = 0.0
                _var_var_149 = 0.0
                _var_var_150 = 0.0
                _var_var_151 = await _energy_correction_projection("2026-01-01")
                _var_var_152 = await _corrected_daily_energy_values("2026-01-01")
                async with _get_db() as _var_var_13:
                    _var_var_66 = await _table_columns(
                        _var_var_13, "stats_daily_energy"
                    )
                    _var_var_67 = (
                        "COALESCE(SUM(total_cost_eur), 0)"
                        if "total_cost_eur" in _var_var_66
                        else "0.0"
                    )
                    async with _var_var_13.execute(
                        f"\n                        SELECT \n                            COUNT(*),\n                            COALESCE(AVG(solar_yield_kwh), 0),\n                            COALESCE(SUM(solar_yield_kwh), 0),\n                            COALESCE(AVG(home_consumption_kwh), 0),\n                            COALESCE(AVG(autarkie_percent), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(solar_to_battery_kwh, 0), battery_charge_solar_kwh, 0)), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(grid_to_battery_kwh, 0), battery_charge_grid_kwh, 0)), 0),\n                            COALESCE(SUM(battery_to_house_kwh), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(grid_import_kwh, 0), smartmeter_import_kwh, grid_to_house_kwh, 0)), 0),\n                            {_var_var_67}\n                        FROM stats_daily_energy\n                        WHERE date >= '2026-01-01'\n                        "
                    ) as _var_var_14:
                        _var_var_15 = await _var_var_14.fetchone()
                        if _var_var_15 and _var_var_15[0] > 0:
                            _var_var_137 = _var_var_15[0]
                            _var_var_142 = _var_var_15[1]
                            _var_var_143 = _var_var_15[2]
                            _var_var_144 = _var_var_15[3]
                            _var_var_145 = _var_var_15[4]
                            _var_var_146 = _var_var_15[5]
                            _var_var_147 = _var_var_15[6]
                            _var_var_148 = _var_var_15[7]
                            _var_var_149 = _var_var_15[8]
                            _var_var_150 = _var_var_15[9]
                            if _var_var_149 > 0 and _var_var_150 > 0:
                                _var_var_131 = _var_var_150 / _var_var_149 * 100.0
                                _var_var_135 = _var_var_131 * _var_var_133
                                _var_var_136 = max(
                                    0.0, _var_var_135 - _var_var_132 - _var_var_134
                                )
                    if _var_var_151:
                        try:
                            _var_var_153 = _column_expr(
                                _var_var_66, "total_cost_eur", default="0.0"
                            )
                            async with _var_var_13.execute(
                                f"\n                                SELECT date,\n                                    autarkie_percent,\n                                    COALESCE(home_consumption_kwh, 0),\n                                    COALESCE(solar_to_house_kwh, 0),\n                                    COALESCE(battery_to_house_kwh, 0),\n                                    COALESCE(grid_to_battery_kwh, 0),\n                                    {_var_var_153},\n                                    COALESCE(\n                                        NULLIF(grid_import_kwh, 0),\n                                        NULLIF(smartmeter_import_kwh, 0),\n                                        NULLIF(grid_to_house_kwh, 0),\n                                        0\n                                    )\n                                FROM stats_daily_energy\n                                WHERE date >= '2026-01-01'\n                                "
                            ) as _var_var_14:
                                _var_var_154 = await _var_var_14.fetchall()
                        except Exception as _var_var_155:
                            _LOGGER.debug(
                                "Smart charging provenance rows unavailable: %s",
                                _var_var_155,
                            )
                            _var_var_154 = []
                        _var_var_156 = []
                        _var_var_157 = []
                        for _var_var_158 in _var_var_154:
                            _var_var_159 = str(_var_var_158[0])
                            _var_var_160 = _var_var_151.get(_var_var_159, {})
                            _var_var_161 = float(_var_var_158[2] or 0.0)
                            _var_var_162 = float(_var_var_158[3] or 0.0)
                            _var_var_163 = float(_var_var_158[4] or 0.0)
                            _var_var_164 = float(_var_var_158[5] or 0.0)
                            _var_var_74 = float(_var_var_158[7] or 0.0)
                            _var_var_156.append(
                                {
                                    "stored": _var_var_158[1],
                                    "raw_consumption": _var_var_161,
                                    "consumption": corrected_value(
                                        _var_var_151,
                                        _var_var_159,
                                        "home_consumption_day_kwh",
                                        _var_var_161,
                                    ),
                                    "raw_solar_to_house": _var_var_162,
                                    "solar_to_house": corrected_value(
                                        _var_var_151,
                                        _var_var_159,
                                        "solar_to_house_day_kwh",
                                        _var_var_162,
                                    ),
                                    "raw_battery_to_house": _var_var_163,
                                    "battery_to_house": corrected_value(
                                        _var_var_151,
                                        _var_var_159,
                                        "battery_to_house_day_kwh",
                                        _var_var_163,
                                    ),
                                    "raw_grid_to_battery": _var_var_164,
                                    "grid_to_battery": corrected_value(
                                        _var_var_151,
                                        _var_var_159,
                                        "grid_to_battery_day_kwh",
                                        _var_var_164,
                                    ),
                                }
                            )
                            _var_var_157.append(
                                {
                                    "raw_import": _var_var_74,
                                    "corrected_import": float(
                                        _var_var_160.get(
                                            "grid_import_day_kwh", _var_var_74
                                        )
                                        or 0.0
                                    ),
                                    "raw_cost": float(_var_var_158[6] or 0.0),
                                }
                            )
                            continue
                        _var_var_165 = window_autarky_average(_var_var_156)
                        if _var_var_165 is not None:
                            _var_var_145 = _var_var_165
                        _var_var_166 = projected_import_cost_totals(_var_var_157)
                        if _var_var_166 is not None:
                            _var_var_149, _var_var_150 = _var_var_166
                            if _var_var_149 > 0 and _var_var_150 > 0:
                                _var_var_131 = _var_var_150 / _var_var_149 * 100.0
                                _var_var_135 = _var_var_131 * _var_var_133
                                _var_var_136 = max(
                                    0.0, _var_var_135 - _var_var_132 - _var_var_134
                                )
                    if _var_var_152:
                        _var_var_143 = sum(
                            (
                                _var_var_206["solar_yield_day_kwh"]
                                for _var_var_206 in _var_var_152.values()
                            )
                        )
                        _var_var_142 = (
                            _var_var_143 / _var_var_137 if _var_var_137 > 0 else 0.0
                        )
                    if _var_var_137 > 0 and _var_var_128:
                        async with _var_var_13.execute(
                            "\n                            SELECT date,\n                                COALESCE(NULLIF(solar_to_battery_kwh, 0), battery_charge_solar_kwh, 0)\n                                    + COALESCE(NULLIF(grid_to_battery_kwh, 0), battery_charge_grid_kwh, 0) as total_charge,\n                                COALESCE(NULLIF(grid_export_kwh, 0), smartmeter_export_kwh, 0) as export,\n                                COALESCE(NULLIF(grid_import_kwh, 0), smartmeter_import_kwh, grid_to_house_kwh, 0) as import_val,\n                                COALESCE(solar_yield_kwh, 0) as solar\n                            FROM stats_daily_energy\n                            WHERE date >= '2026-01-01'\n                            "
                        ) as _var_var_14:
                            _var_var_32 = await _var_var_14.fetchall()
                            for _var_var_106 in _var_var_32:
                                _var_var_167 = max(_var_var_106[1] or 0.0, 0.0)
                                _var_var_168 = max(
                                    corrected_value(
                                        _var_var_151,
                                        _var_var_106[0],
                                        "grid_export_day_kwh",
                                        _var_var_106[2],
                                    ),
                                    0.0,
                                )
                                _var_var_169 = max(
                                    corrected_value(
                                        _var_var_151,
                                        _var_var_106[0],
                                        "grid_import_day_kwh",
                                        _var_var_106[3],
                                    ),
                                    0.0,
                                )
                                _var_var_116 = max(
                                    corrected_value(
                                        _var_var_151,
                                        _var_var_106[0],
                                        "solar_yield_day_kwh",
                                        _var_var_106[4],
                                    ),
                                    0.0,
                                )
                                _var_var_170 = _var_var_130 > 0 and (
                                    _var_var_167 >= 0.8 * _var_var_130
                                    or (_var_var_168 > 1.0 and _var_var_116 > 4.0)
                                )
                                if _var_var_170:
                                    _var_var_138 += 1
                                    _var_var_140 += _var_var_168
                                    _var_var_171 = min(_var_var_168, _var_var_169)
                                    _var_var_139 += _var_var_171
                                    _var_var_141 += _var_var_171 * (
                                        _var_var_132 / 100.0
                                    )
                _var_var_172 = _var_var_146 + _var_var_147
                _var_var_173 = _var_var_172 / _var_var_130 if _var_var_130 > 0 else 0.0
                _var_var_174 = (
                    _var_var_138 / _var_var_137 * 100.0 if _var_var_137 > 0 else 0.0
                )
                _var_var_175 = _var_var_139 * (_var_var_136 / 100.0)
                _var_var_126 = {
                    "has_battery": _var_var_128,
                    "total_days": _var_var_137,
                    "full_days": _var_var_138,
                    "full_days_percent": _var_var_174,
                    "potential_kwh": _var_var_139,
                    "potential_savings_eur": _var_var_175,
                    "export_at_capacity_kwh": _var_var_140,
                    "feed_in_tariff_ct": _var_var_132,
                    "effective_import_price_ct_kwh": _var_var_131,
                    "gross_avoided_import_ct_kwh": _var_var_135,
                    "battery_roundtrip_efficiency": _var_var_133,
                    "battery_degradation_ct_kwh": _var_var_134,
                    "net_value_ct_kwh": _var_var_136,
                    "potential_feed_in_revenue_eur": _var_var_141,
                    "total_solar_yield_kwh": _var_var_143,
                    "avg_solar_yield_kwh": _var_var_142,
                    "avg_house_consumption_kwh": _var_var_144,
                    "avg_autarky_percent": _var_var_145,
                    "battery_charge_solar_kwh": _var_var_146,
                    "battery_charge_grid_kwh": _var_var_147,
                    "battery_discharge_house_kwh": _var_var_148,
                    "battery_cycles": _var_var_173,
                    "battery_capacity": _var_var_129,
                }
            except Exception as _var_var_21:
                _LOGGER.error(
                    "Error calculating smart charging sizing advisor: %s", _var_var_21
                )
            _var_var_176 = _var_var_1.get(CONF_SMART_CHARGING_SWITCH)
            _var_var_177 = None
            _var_var_178 = None
            if _var_var_176 and str(_var_var_176).strip():
                _var_var_179 = context.HASS.states.get(str(_var_var_176).strip())
                if _var_var_179 is not None:
                    _var_var_177 = getattr(_var_var_179, "state", None)
                    _var_var_178 = getattr(_var_var_179, "last_changed", None)
            _var_var_8["charging_since"], _var_var_8["waiting_since"] = _var_var_93(
                _var_var_177, _var_var_178, _var_var_5
            )
            _var_var_180, _var_var_181 = _var_var_92(
                _var_var_96,
                gpm_available=bool(_var_var_94.get("available")),
                is_demo=bool(_var_var_94.get("is_demo")),
                today=_var_var_26.isoformat(),
            )
            _var_var_8["next_cheap_hour"] = _var_var_180
            _var_var_8["next_cheap_price_ct"] = _var_var_181
            from ..core.charging_status import (
                grid_release_view as _var_var_182,
                status_facts as _var_var_183,
            )

            _var_var_184 = ha_now(context.HASS)
            _var_var_185 = _var_var_182(
                _var_var_96,
                _var_var_184,
                _var_var_2,
                is_demo=bool(_var_var_94.get("is_demo")),
            )
            _var_var_8["grid_allowed"] = _var_var_185["allowed"]
            _var_var_8["grid_kind"] = _var_var_185["kind"]
            _var_var_8["under_force"] = _var_var_185["under_force"]
            _var_var_8["status"] = _var_var_183(
                active=bool(_var_var_8.get("active")),
                reason=str(_var_var_8.get("reason") or ""),
                grid_kind=str(_var_var_185["kind"]),
                mode=_var_var_8.get("mode"),
                target_soc=_var_var_8.get("target_soc"),
                max_soc=_var_var_8.get("max_soc"),
                min_soc=_var_var_8.get("min_soc"),
                next_cheap_hour=_var_var_180,
                under_force=bool(_var_var_185["under_force"]),
                now_hour=_var_var_184.hour,
            )
            _var_var_186 = {
                "is_cheap": _var_var_8.get("is_cheap"),
                "is_force_price": bool(_var_var_8.get("is_force_price")),
                "decision": _var_var_8.get("decision"),
                "reason": _var_var_8.get("reason"),
                "enabled": bool(_var_var_8.get("enabled")),
                "active": bool(_var_var_8.get("active")),
                "reserved_future_grid_charge_kwh": _var_var_8.get(
                    "reserved_future_grid_charge_kwh", 0.0
                ),
                "gpm_available": bool(_var_var_94.get("available")),
                "is_demo": bool(_var_var_94.get("is_demo")),
                "grid_allowed": _var_var_8.get("grid_allowed"),
                "grid_kind": _var_var_8.get("grid_kind"),
                "status": _var_var_8.get("status"),
            }
            from ..core.grid_import_split import battery_is_configured as _var_var_187

            _var_var_188 = None
            if _var_var_187(_var_var_1):
                try:
                    from ..core.grid_import_split import (
                        battery_card as _var_var_189,
                        charge_window_bounds as _var_var_190,
                        load_split_hours as _var_var_191,
                        summarize_import_split as _var_var_192,
                    )

                    _var_var_193 = ha_today(context.HASS)
                    _var_var_194 = _var_var_190(_var_var_193)
                    _var_var_195, _var_var_196 = _var_var_194["year"]
                    _var_var_197 = await _energy_correction_projection(_var_var_195)
                    _var_var_198 = await _corrected_daily_energy_values(
                        _var_var_195, _var_var_196
                    )
                    try:
                        _var_var_199 = float(_get_fixed_total_price(_var_var_1))
                    except (TypeError, ValueError):
                        _var_var_199 = 0.0
                    async with _get_db() as _var_var_13:
                        _var_var_200 = await _var_var_191(
                            _var_var_13, _var_var_195, _var_var_196
                        )
                    _var_var_188 = {}
                    for _var_var_201, (
                        _var_var_202,
                        _var_var_203,
                    ) in _var_var_194.items():
                        _var_var_205 = [
                            _var_var_204
                            for _var_var_204 in _var_var_200.hours
                            if _var_var_202 <= str(_var_var_204["date"]) <= _var_var_203
                        ]
                        _var_var_188[_var_var_201] = _var_var_189(
                            _var_var_192(
                                _var_var_205,
                                _var_var_198,
                                _var_var_197,
                                fallback_price_ct=_var_var_199,
                                house_column=_var_var_200.house_column,
                                battery_column=_var_var_200.battery_column,
                                backfill_done=_var_var_200.backfill_done,
                            )
                        )
                except Exception as _var_var_155:
                    _LOGGER.debug("Grid charge card unavailable: %s", _var_var_155)
                    _var_var_188 = None
            return web.json_response(
                {
                    "success": True,
                    "live": _var_var_8,
                    "gpm": _var_var_94,
                    "smart_charging_status": _var_var_186,
                    "kpis": _var_var_29,
                    "history": _var_var_83,
                    "chart_plan": {
                        "enabled": bool(_var_var_5 and _var_var_7),
                        "source": _var_var_95,
                        "recommendation_threshold": (
                            round(_var_var_98, 2) if _var_var_98 is not None else None
                        ),
                        "max_price_ct": (
                            round(_var_var_84, 2) if _var_var_84 is not None else None
                        ),
                        "force_charge_price_ct": (
                            round(_var_var_85, 2) if _var_var_85 is not None else None
                        ),
                    },
                    "advisor": _var_var_126,
                    "grid_charge": _var_var_188,
                }
            )
        except Exception as _var_var_155:
            _LOGGER.error(
                "Error in SmartChargingDashboardView: %s", _var_var_155, exc_info=True
            )
            return web.json_response({"error": str(_var_var_155)}, status=500)
