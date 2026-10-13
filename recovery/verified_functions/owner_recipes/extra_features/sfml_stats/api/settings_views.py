# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import _get_db, _time_context, local_only, local_or_trusted_cloud_only
from ..utils.time_utils import ha_today
from ..core.price_mode import (
    MODE_GPM_DYNAMIC,
    MODE_GPM_FIXED,
    MODE_LEGACY_FIXED,
    MODE_NONE,
    effective_price_mode,
    is_fixed_like,
)
from ..const import (
    DOMAIN,
    VERSION,
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
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_BILLING_BASE_FEE,
    CONF_SMART_CHARGING_ENABLED,
    CONF_BATTERY_CAPACITY,
    CONF_MIN_SOC,
    CONF_MAX_SOC,
    CONF_BATTERY_SOC_SENSOR,
    DEFAULT_MIN_SOC,
    DEFAULT_MAX_SOC,
    configured_battery_capacity_kwh,
    CONF_SENSOR_GRID_IMPORT_EXTRA,
    CONF_COUNTRY,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    SOLAR_FORECAST_DB,
)


class SettingsDashboardView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return all settings/config data for the Settings page. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            _var_var_0 = {}
            for _var_var_1 in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(_var_var_1, dict):
                    if "config" in _var_var_1:
                        _var_var_0 = _var_var_1["config"]
                        break
            _var_var_2 = [
                (CONF_SENSOR_HOME_CONSUMPTION, "Hausverbrauch (W)"),
                (CONF_SENSOR_SOLAR_TO_HOUSE, "Solar → Haus (W)"),
                (CONF_SENSOR_INVERTER_AC_OUTPUT, "Wechselrichter-Ausgang AC (W)"),
                (CONF_SENSOR_SOLAR_TO_BATTERY, "Solar → Batterie (W)"),
                (CONF_SENSOR_BATTERY_TO_HOUSE, "Batterie → Haus (W)"),
                (CONF_SENSOR_BATTERY_SOC, "Batterie SoC (%)"),
                (CONF_SENSOR_BATTERY_POWER, "Batterie Leistung (W)"),
                (CONF_SENSOR_GRID_TO_HOUSE, "Netz → Haus (W)"),
                (CONF_SENSOR_GRID_TO_BATTERY, "Netz → Batterie (W)"),
                (CONF_SENSOR_HOUSE_TO_GRID, "Haus → Netz (W)"),
                (CONF_SENSOR_SMARTMETER_IMPORT, "Smartmeter Bezug (W)"),
                (CONF_SENSOR_SMARTMETER_EXPORT, "Smartmeter Einspeisung (W)"),
                (CONF_SENSOR_GRID_IMPORT_EXTRA, "Zusätzlicher Netzbezug (kWh/Tag)"),
                (CONF_WEATHER_ENTITY, "Wetter"),
                (CONF_SENSOR_HEATPUMP_POWER, "Wärmepumpe Leistung"),
                (CONF_SENSOR_HEATINGROD_POWER, "Heizstab Leistung"),
                (CONF_SENSOR_WALLBOX_POWER, "Wallbox Leistung"),
                (CONF_SENSOR_WALLBOX_STATE, "Wallbox Status"),
            ]
            from ..core.eai_wp_sources import resolved_heatpump_entity as _var_var_3

            _var_var_4 = []
            for _var_var_5, _var_var_6 in _var_var_2:
                _var_var_7 = (
                    _var_var_3(context.HASS, _var_var_0, _var_var_5)
                    if _var_var_5 == CONF_SENSOR_HEATPUMP_POWER
                    else _var_var_0.get(_var_var_5)
                )
                if not _var_var_7:
                    continue
                _var_var_8 = context.HASS.states.get(_var_var_7)
                _var_var_9 = _var_var_8 is not None and _var_var_8.state not in (
                    "unknown",
                    "unavailable",
                )
                _var_var_4.append(
                    {
                        "entity_id": _var_var_7,
                        "friendly_name": _var_var_6,
                        "state": _var_var_8.state if _var_var_9 else None,
                        "available": _var_var_9,
                    }
                )
            _var_var_10 = effective_price_mode(context.HASS, _var_var_0)
            _var_var_11 = {
                MODE_GPM_DYNAMIC: "Dynamisch (GPM)",
                MODE_GPM_FIXED: "Fest (GPM)",
                MODE_LEGACY_FIXED: "Fest",
                MODE_NONE: "Kein Tarif",
            }
            _var_var_12 = None
            _var_var_13 = None
            if is_fixed_like(_var_var_10):
                _var_var_14 = _var_var_0.get(CONF_BILLING_WORK_PRICE)
                _var_var_15 = _var_var_0.get(CONF_BILLING_GRID_FEES)
                if _var_var_14 is not None:
                    _var_var_12 = round(_var_var_14, 2)
                if _var_var_15 is not None:
                    _var_var_13 = round(_var_var_15, 2)
                if _var_var_12 is None and CONF_BILLING_FIXED_PRICE in _var_var_0:
                    _var_var_12 = round(_var_var_0[CONF_BILLING_FIXED_PRICE], 2)
            _var_var_16 = _var_var_0.get(CONF_BILLING_BASE_FEE)
            _var_var_17 = round(_var_var_16, 2) if _var_var_16 is not None else None
            _var_var_18 = 19 if _var_var_0.get(CONF_COUNTRY, "DE") == "DE" else 20
            _var_var_19 = {
                "country": _var_var_0.get(CONF_COUNTRY, "DE"),
                "vat": _var_var_18,
                "mode": _var_var_11.get(_var_var_10, _var_var_10),
                "energy_price": _var_var_12,
                "grid_fees": _var_var_13,
                "base_fee": _var_var_17,
                "feed_in_tariff": round(
                    _var_var_0.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF), 2
                ),
            }
            _var_var_20 = bool(_var_var_0.get(CONF_SMART_CHARGING_ENABLED, False))
            _var_var_21 = _var_var_0.get(CONF_BATTERY_SOC_SENSOR) or _var_var_0.get(
                CONF_SENSOR_BATTERY_SOC
            )
            _var_var_22 = {
                "enabled": _var_var_20,
                "capacity": configured_battery_capacity_kwh(
                    _var_var_0.get(CONF_BATTERY_CAPACITY)
                ),
                "min_soc": _var_var_0.get(
                    CONF_MIN_SOC, DEFAULT_MIN_SOC if _var_var_20 else None
                ),
                "max_soc": _var_var_0.get(
                    CONF_MAX_SOC, DEFAULT_MAX_SOC if _var_var_20 else None
                ),
                "soc_sensor": _var_var_21,
                "soc_sensor_configured": bool(_var_var_21),
                "current_soc": None,
                "target_soc": None,
                "active": None,
                "reason": None,
                "is_cheap": None,
                "current_price": None,
            }
            try:
                for _var_var_23 in context.HASS.data.get(DOMAIN, {}).values():
                    if isinstance(_var_var_23, dict):
                        _var_var_24 = _var_var_23.get("gpm_coordinator")
                        if _var_var_24 is not None:
                            if _var_var_24.data:
                                _var_var_25 = _var_var_24.data
                                _var_var_22["is_cheap"] = _var_var_25.get("is_cheap")
                                _var_var_22["current_price"] = _var_var_25.get(
                                    "total_price"
                                )
                                _var_var_22["current_soc"] = _var_var_25.get(
                                    "smart_charging_current_soc"
                                )
                                _var_var_22["target_soc"] = _var_var_25.get(
                                    "smart_charging_target_soc"
                                )
                                _var_var_22["active"] = _var_var_25.get(
                                    "smart_charging_active"
                                )
                                _var_var_22["reason"] = _var_var_25.get(
                                    "smart_charging_reason"
                                )
                                break
            except Exception as _var_var_26:
                _LOGGER.debug("Smart charging live state unavailable: %s", _var_var_26)
            if _var_var_22["current_soc"] is None and _var_var_21:
                try:
                    _var_var_27 = context.HASS.states.get(_var_var_21)
                    if _var_var_27 is not None and _var_var_27.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        _var_var_22["current_soc"] = float(_var_var_27.state)
                except (ValueError, TypeError):
                    pass
            _var_var_28 = _var_var_0.get(CONF_SENSOR_BATTERY_SOC)
            if (
                _var_var_22["current_soc"] is None or _var_var_22["current_soc"] == 0.0
            ) and _var_var_28:
                try:
                    _var_var_29 = context.HASS.states.get(_var_var_28)
                    if _var_var_29 is not None and _var_var_29.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        _var_var_30 = float(_var_var_29.state)
                        if _var_var_30 > 1.0:
                            _var_var_22["current_soc"] = _var_var_30
                except (ValueError, TypeError):
                    pass
            _var_var_31 = []
            try:
                async with _get_db() as _var_var_32:
                    async with _var_var_32.execute(
                        "\n                        SELECT group_name, power_kwp, azimuth_deg, tilt_deg\n                        FROM astronomy_cache_panel_groups\n                        WHERE cache_date = (SELECT MAX(cache_date) FROM astronomy_cache_panel_groups)\n                        AND hour = 12\n                        ORDER BY group_name\n                    "
                    ) as _var_var_33:
                        for _var_var_34 in await _var_var_33.fetchall():
                            _var_var_31.append(
                                {
                                    "name": _var_var_34[0],
                                    "kwp": (
                                        round(_var_var_34[1], 3)
                                        if _var_var_34[1] is not None
                                        else 0
                                    ),
                                    "azimuth": (
                                        round(_var_var_34[2], 0)
                                        if _var_var_34[2] is not None
                                        else 0
                                    ),
                                    "tilt": (
                                        round(_var_var_34[3], 0)
                                        if _var_var_34[3] is not None
                                        else 0
                                    ),
                                    "module_count": None,
                                }
                            )
                    _var_var_35 = {}
                    try:
                        async with _var_var_32.execute(
                            "SELECT group_name, global_factor, confidence FROM physics_calibration_groups"
                        ) as _var_var_33:
                            for _var_var_34 in await _var_var_33.fetchall():
                                _var_var_35[_var_var_34[0]] = {
                                    "factor": (
                                        round(_var_var_34[1], 3)
                                        if _var_var_34[1] is not None
                                        else None
                                    ),
                                    "confidence": (
                                        round(_var_var_34[2], 2)
                                        if _var_var_34[2] is not None
                                        else None
                                    ),
                                }
                    except Exception:
                        pass
                    for _var_var_36 in _var_var_31:
                        _var_var_36.update(_var_var_35.get(_var_var_36["name"], {}))
                    _var_var_37 = {_var_var_36["name"] for _var_var_36 in _var_var_31}
                    for _var_var_38, _var_var_39 in sorted(_var_var_35.items()):
                        if _var_var_38 in _var_var_37:
                            continue
                        _var_var_31.append(
                            {
                                "name": _var_var_38,
                                "kwp": None,
                                "azimuth": None,
                                "tilt": None,
                                "module_count": None,
                                **_var_var_39,
                            }
                        )
            except Exception as _var_var_26:
                _LOGGER.debug("Panel groups from DB unavailable: %s", _var_var_26)
            _var_var_40 = await self._collect_system_info()
            from ..core.dev_mode import is_dev_active as _var_var_41

            _var_var_42 = (
                await self._collect_ai_info() if _var_var_41(context.HASS) else {}
            )
            _var_var_43 = []
            _var_var_44 = [
                (CONF_SENSOR_HEATPUMP_POWER, "heatpump"),
                (CONF_SENSOR_HEATINGROD_POWER, "heatingrod"),
                (CONF_SENSOR_WALLBOX_POWER, "wallbox"),
            ]
            for _var_var_45, _var_var_46 in _var_var_44:
                _var_var_47 = (
                    _var_var_3(context.HASS, _var_var_0, _var_var_45)
                    if _var_var_45 == CONF_SENSOR_HEATPUMP_POWER
                    else _var_var_0.get(_var_var_45)
                )
                if _var_var_47:
                    _var_var_43.append(
                        {
                            "config_key": _var_var_46,
                            "power_entity_id": _var_var_47,
                            "daily_entity_id": None,
                            "mode": "integrated",
                        }
                    )
            return web.json_response(
                {
                    "success": True,
                    "sensors": _var_var_4,
                    "price": _var_var_19,
                    "smart_charging": _var_var_22,
                    "panel_groups": _var_var_31,
                    "system": _var_var_40,
                    "ai": _var_var_42,
                    "configured_consumers": _var_var_43,
                    "time_context": _time_context(),
                }
            )
        except Exception as _var_var_48:
            _LOGGER.error("Error in settings dashboard: %s", _var_var_48, exc_info=True)
            return web.json_response({"error": str(_var_var_48)}, status=500)

    async def _collect_ai_info(self) -> dict:
        """Collect Hubble AI stack meta: active model, accuracy, drift. @zara"""
        _var_var_49 = {
            "active_model": None,
            "active_model_display": None,
            "version": None,
            "accuracy_percent": None,
            "rmse": None,
            "training_samples": None,
            "last_trained": None,
            "drift": None,
            "coordinator": None,
        }
        try:
            async with _get_db() as _var_var_32:
                try:
                    async with _var_var_32.execute(
                        "SELECT version, active_model, training_samples, last_trained, accuracy, rmse FROM ai_learned_weights_meta WHERE id = 1"
                    ) as _var_var_33:
                        _var_var_57 = await _var_var_33.fetchone()
                        if _var_var_57:
                            _var_var_49["version"] = _var_var_57[0]
                            _var_var_49["active_model"] = _var_var_57[1]
                            _var_var_49["active_model_display"] = (
                                "Hubble AI Stack" if _var_var_57[1] else None
                            )
                            _var_var_49["training_samples"] = _var_var_57[2]
                            _var_var_49["last_trained"] = _var_var_57[3]
                            _var_var_49["accuracy_percent"] = (
                                round((_var_var_57[4] or 0) * 100, 1)
                                if _var_var_57[4] is not None
                                else None
                            )
                            _var_var_49["rmse"] = (
                                round(_var_var_57[5], 4)
                                if _var_var_57[5] is not None
                                else None
                            )
                except Exception as _var_var_26:
                    _LOGGER.debug("ai_learned_weights_meta missing: %s", _var_var_26)
                try:
                    async with _var_var_32.execute(
                        "SELECT COUNT(*), MAX(created_at), SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END), SUM(CASE WHEN severity='warning' THEN 1 ELSE 0 END) FROM drift_events WHERE event_date >= ?",
                        ((ha_today(context.HASS) - timedelta(days=7)).isoformat(),),
                    ) as _var_var_33:
                        _var_var_57 = await _var_var_33.fetchone()
                        if _var_var_57:
                            _var_var_58 = int(_var_var_57[0] or 0)
                            _var_var_59 = int(_var_var_57[2] or 0)
                            _var_var_60 = int(_var_var_57[3] or 0)
                            if _var_var_59 > 0:
                                _var_var_8 = "critical"
                            elif _var_var_60 > 0:
                                _var_var_8 = "warning"
                            elif _var_var_58 > 0:
                                _var_var_8 = "recovering"
                            else:
                                _var_var_8 = "stable"
                            _var_var_49["drift"] = {
                                "state": _var_var_8,
                                "active_events": _var_var_58,
                                "total_events": _var_var_58,
                                "last_event": _var_var_57[1],
                                "critical_count": _var_var_59,
                                "warning_count": _var_var_60,
                            }
                except Exception as _var_var_26:
                    _LOGGER.debug("drift_events missing: %s", _var_var_26)
                try:
                    async with _var_var_32.execute(
                        "SELECT expected_daily_production, last_set_date, last_updated FROM coordinator_state WHERE id = 1"
                    ) as _var_var_33:
                        _var_var_57 = await _var_var_33.fetchone()
                        if _var_var_57:
                            _var_var_49["coordinator"] = {
                                "expected_kwh_today": (
                                    round(_var_var_57[0], 2)
                                    if _var_var_57[0] is not None
                                    else None
                                ),
                                "last_set_date": _var_var_57[1],
                                "last_updated": _var_var_57[2],
                            }
                except Exception as _var_var_26:
                    _LOGGER.debug("coordinator_state missing: %s", _var_var_26)
        except Exception as _var_var_26:
            _LOGGER.debug("AI info collection failed: %s", _var_var_26)
        if True:
            return _var_var_49
        else:
            return _var_var_49
