# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
import logging
from datetime import date, timedelta, timezone
from typing import TYPE_CHECKING, Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _corrected_daily_energy_values,
    _get_broad_timestamp_range_for_local_dates,
    _get_db,
    _get_fixed_total_price,
    _gpm_local_hour_key,
    _stable_api_error,
    _time_context,
    local_only,
)
from ..utils.time_utils import format_local_hour_key, ha_now, ha_today, ha_timezone
from ..core.price_mode import MODE_LEGACY_FIXED, MODE_NONE, effective_price_mode
from ..const import (
    DOMAIN,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_BASE_FEE,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
)


class GPMPricesView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return current electricity price. Dynamic: from DB. Fixed: from config. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            _var_var_6 = {}
            _var_var_7 = None
            for _var_var_3 in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(_var_var_3, dict):
                    if "config" in _var_var_3:
                        _var_var_6 = _var_var_3["config"]
                    if "gpm_coordinator" in _var_var_3:
                        _var_var_7 = _var_var_3["gpm_coordinator"]
            _var_var_8 = effective_price_mode(context.HASS, _var_var_6)
            if _var_var_8 == MODE_NONE:
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "time_context": _time_context(),
                        "price_mode": _var_var_8,
                        "hours_without_price": 0,
                        "total_price": None,
                        "is_cheap": False,
                        "price_trend": "stable",
                        "diagnostics": {
                            "price_source": "cost_tracking_disabled",
                            "gpm_read_skipped": True,
                        },
                    }
                )
            if _var_var_8 == MODE_LEGACY_FIXED:
                _var_var_9 = _get_fixed_total_price(_var_var_6)
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "time_context": _time_context(),
                        "price_mode": _var_var_8,
                        "hours_without_price": 0 if _var_var_9 else 1,
                        "total_price": _var_var_9,
                        "is_cheap": True,
                        "price_trend": "stable",
                        "diagnostics": {
                            "price_source": "configured_fixed_tariff",
                            "gpm_read_skipped": True,
                        },
                    }
                )
            _var_var_10 = ha_now(context.HASS)
            _var_var_11 = (
                _var_var_10.astimezone(timezone.utc) + timedelta(hours=1)
            ).astimezone(ha_timezone(context.HASS))
            _var_var_12 = format_local_hour_key(_var_var_10, context.HASS)
            _var_var_13 = format_local_hour_key(_var_var_11, context.HASS)
            _var_var_14 = None
            _var_var_15 = None
            _var_var_16 = None
            _var_var_17 = None
            _var_var_18 = None
            _var_var_19 = None
            _var_var_20 = "unavailable"
            try:
                _var_var_21 = ha_today(context.HASS).isoformat()
                _var_var_22 = _var_var_11.date().isoformat()
                _var_var_23, _var_var_24 = _get_broad_timestamp_range_for_local_dates(
                    [_var_var_21, _var_var_22]
                )
                async with _get_db() as _var_var_25:
                    async with _var_var_25.execute(
                        "SELECT total_price, spot_price_net, price_next_hour,\n                                  average_today, cheapest_today, most_expensive_today\n                           FROM GPM_current_price WHERE id = 1"
                    ) as _var_var_26:
                        _var_var_27 = await _var_var_26.fetchone()
                    if _var_var_27 and _var_var_27[0] is not None:
                        _var_var_14 = _var_var_27[0]
                        _var_var_20 = "GPM_current_price"
                        _var_var_16 = _var_var_27[1]
                        _var_var_15 = _var_var_27[2]
                        _var_var_17 = _var_var_27[3]
                        _var_var_18 = _var_var_27[4]
                        _var_var_19 = _var_var_27[5]
                    async with _var_var_25.execute(
                        "SELECT timestamp, total_price, price_net FROM GPM_price_history\n                           WHERE timestamp >= ? AND timestamp < ?\n                           ORDER BY timestamp",
                        (_var_var_23, _var_var_24),
                    ) as _var_var_26:
                        _var_var_28 = await _var_var_26.fetchall()
                    _var_var_29 = []
                    _var_var_30 = {}
                    for _var_var_31 in _var_var_28:
                        _var_var_32 = _gpm_local_hour_key(_var_var_31[0])
                        if not _var_var_32:
                            continue
                        _var_var_30[_var_var_32] = _var_var_31
                        if _var_var_32[:10] == _var_var_21:
                            _var_var_29.append((_var_var_32, _var_var_31))
                    if _var_var_14 is None or _var_var_16 is None:
                        _var_var_27 = _var_var_30.get(_var_var_12)
                        if _var_var_27:
                            _var_var_14 = _var_var_27[1]
                            _var_var_16 = _var_var_27[2]
                            _var_var_20 = "GPM_price_history"
                    if _var_var_15 is None:
                        _var_var_27 = _var_var_30.get(_var_var_13)
                        if _var_var_27:
                            _var_var_15 = _var_var_27[1]
                    if _var_var_17 is None:
                        if _var_var_29:
                            _var_var_34 = [
                                _var_var_27[1]
                                for _var_var_33, _var_var_27 in _var_var_29
                                if _var_var_27[1] is not None
                            ]
                            if _var_var_34:
                                _var_var_17 = round(
                                    sum(_var_var_34) / len(_var_var_34), 2
                                )
                    if _var_var_18 is None and _var_var_29:
                        _var_var_35 = min(
                            (
                                _var_var_44
                                for _var_var_44 in _var_var_29
                                if _var_var_44[1][1] is not None
                            ),
                            key=lambda item: item[1][1],
                            default=None,
                        )
                        if _var_var_35:
                            _var_var_18 = int(_var_var_35[0][11:13])
                    if _var_var_19 is None and _var_var_29:
                        _var_var_36 = max(
                            (
                                _var_var_44
                                for _var_var_44 in _var_var_29
                                if _var_var_44[1][1] is not None
                            ),
                            key=lambda item: item[1][1],
                            default=None,
                        )
                        if _var_var_36:
                            _var_var_19 = int(_var_var_36[0][11:13])
            except Exception as _var_var_37:
                _LOGGER.debug("Error reading prices from DB: %s", _var_var_37)
            _var_var_38 = "unknown"
            if _var_var_14 is not None and _var_var_15 is not None:
                _var_var_39 = _var_var_15 - _var_var_14
                if _var_var_39 > 1:
                    _var_var_38 = "rising"
                elif _var_var_39 < -1:
                    _var_var_38 = "falling"
                else:
                    _var_var_38 = "stable"
            _var_var_40 = {
                "active": False,
                "target_soc": None,
                "current_soc": None,
                "reason": "",
                "decision": "not_load",
                "requested_grid_charge_kwh": 0.0,
                "effective_storage_cost_ct_kwh": None,
                "compared_future_price_ct_kwh": None,
                "effective_roundtrip_efficiency": None,
                "reserved_future_grid_charge_kwh": 0.0,
            }
            if _var_var_7 and _var_var_7.data:
                _var_var_40 = {
                    "active": _var_var_7.data.get("smart_charging_active", False),
                    "target_soc": _var_var_7.data.get("smart_charging_target_soc"),
                    "current_soc": _var_var_7.data.get("smart_charging_current_soc"),
                    "reason": _var_var_7.data.get("smart_charging_reason", ""),
                    "decision": _var_var_7.data.get(
                        "smart_charging_decision", "not_load"
                    ),
                    "requested_grid_charge_kwh": _var_var_7.data.get(
                        "smart_charging_requested_grid_charge_kwh", 0.0
                    ),
                    "effective_storage_cost_ct_kwh": _var_var_7.data.get(
                        "smart_charging_effective_storage_cost_ct_kwh"
                    ),
                    "compared_future_price_ct_kwh": _var_var_7.data.get(
                        "smart_charging_compared_future_price_ct_kwh"
                    ),
                    "effective_roundtrip_efficiency": _var_var_7.data.get(
                        "smart_charging_effective_roundtrip_efficiency"
                    ),
                    "reserved_future_grid_charge_kwh": _var_var_7.data.get(
                        "smart_charging_reserved_future_grid_charge_kwh", 0.0
                    ),
                }
            _var_var_41 = False
            if _var_var_7 and isinstance(getattr(_var_var_7, "data", None), dict):
                _var_var_41 = bool(_var_var_7.data.get("is_cheap"))
            _var_var_42 = []
            try:
                _var_var_21 = ha_today(context.HASS).isoformat()
                _var_var_43 = (ha_today(context.HASS) + timedelta(days=1)).isoformat()
                _var_var_23, _var_var_24 = _get_broad_timestamp_range_for_local_dates(
                    [_var_var_21, _var_var_43]
                )
                async with _get_db() as _var_var_25:
                    async with _var_var_25.execute(
                        "SELECT timestamp, total_price, price_net\n                           FROM GPM_price_history\n                           WHERE timestamp >= ? AND timestamp < ?\n                           ORDER BY timestamp",
                        (_var_var_23, _var_var_24),
                    ) as _var_var_26:
                        _var_var_28 = await _var_var_26.fetchall()
                    for _var_var_27 in _var_var_28:
                        _var_var_32 = _gpm_local_hour_key(_var_var_27[0])
                        if not _var_var_32:
                            continue
                        if _var_var_32[:10] not in (_var_var_21, _var_var_43):
                            continue
                        _var_var_42.append(
                            {
                                "hour": int(_var_var_32[11:13]),
                                "total_price": _var_var_27[1],
                                "spot_price": _var_var_27[2],
                                "is_tomorrow": _var_var_32[:10] == _var_var_43,
                            }
                        )
            except Exception:
                pass
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "price_mode": _var_var_8,
                    "hours_without_price": 0 if _var_var_14 is not None else 1,
                    "spot_price": _var_var_16,
                    "total_price": _var_var_14,
                    "total_price_next_hour": _var_var_15,
                    "cheapest_hour_today": _var_var_18,
                    "most_expensive_hour_today": _var_var_19,
                    "average_price_today": _var_var_17,
                    "is_cheap": _var_var_41,
                    "price_trend": _var_var_38,
                    "diagnostics": {
                        "price_source": _var_var_20,
                        "gpm_read_skipped": False,
                    },
                    "smart_charging": _var_var_40,
                    "price_hours": _var_var_42,
                }
            )
        except Exception as _var_var_5:
            _LOGGER.error(
                "Error fetching GPM price data: %s", _var_var_5, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class MonthlyTariffsView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get monthly tariffs data. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            _var_var_45 = None
            _var_var_1 = context.HASS.data.get(DOMAIN, {})
            for _var_var_2, _var_var_3 in _var_var_1.items():
                if isinstance(_var_var_3, dict):
                    if "monthly_tariff_manager" in _var_var_3:
                        _var_var_45 = _var_var_3["monthly_tariff_manager"]
                        break
            if _var_var_45 is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            _var_var_46 = max(
                2020,
                min(
                    int(request.query.get("year", ha_today(context.HASS).year)),
                    ha_today(context.HASS).year + 1,
                ),
            )
            _var_var_47 = await _var_var_45.get_year_summary(_var_var_46)
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    **_var_var_47,
                }
            )
        except Exception as _var_var_5:
            _LOGGER.error(
                "Error fetching monthly tariffs: %s", _var_var_5, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )
