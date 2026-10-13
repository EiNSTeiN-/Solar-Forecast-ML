# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _corrected_daily_energy_values,
    _get_db,
    _get_fixed_total_price,
    _get_power_kw,
    _get_power_w,
    _stable_api_error,
    _table_columns,
    _time_context,
    local_only,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today, ha_timezone
from ..core.corrected_energy import (
    component_day,
    dynamic_month_costs,
    energy_flows_changed,
    legacy_dynamic_month,
    load_hourly_component_days,
    own_supply_kwh,
)
from ..core.price_mode import effective_price_mode, is_fixed_like
from ..const import (
    DOMAIN,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    CONF_AMORTIZATION_INVESTMENT_EUR,
    CONF_AMORTIZATION_SUBSIDY_EUR,
    CONF_AMORTIZATION_COMMISSIONING_DATE,
    CONF_AMORTIZATION_ANNUAL_RUNNING_COSTS_EUR,
    CONF_AMORTIZATION_PRICE_INCREASE_PERCENT,
    CONF_AMORTIZATION_DEGRADATION_PERCENT,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_DAILY,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HP_HEATING_MODE,
    CONF_SENSOR_HP_DHW_MODE,
    CONF_SENSOR_HP_DHW_CHARGING,
    CONF_SENSOR_HP_PV_ACTIVE,
    CONF_SENSOR_HP_ELECTRIC_POWER,
    CONF_SENSOR_HP_THERMAL_POWER,
    CONF_SENSOR_HP_GRID_ENERGY_DAILY,
    CONF_SENSOR_HP_PV_ENERGY_DAILY,
    CONF_SENSOR_HP_JAZ,
    CONF_SENSOR_HP_COMPRESSOR_STARTS,
    CONF_SENSOR_HP_STORAGE_TEMP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    CONF_SENSOR_WB_CHARGE_MODE,
    CONF_SENSOR_WB_ENERGY_SESSION,
)
from ..core.amortization import (
    DEFAULT_AMORTIZATION_SETTINGS,
    calculate_amortization,
    sanitize_amortization_settings,
)
from ..core.consumer_atlas import (
    build_consumer_atlas_summary,
    calculate_billing_period,
    sanitize_consumer_config,
    valid_consumer_config,
)
from ..core.heatpump_today import (
    eai_electric_today_kwh,
    resolve_heatpump_today_kwh,
    split_heatpump_today_energy,
)


class ConsumerAtlasBackfillView(HomeAssistantView):

    async def _recorder_states(
        self, entity_id: str, start_time: datetime, end_time: datetime
    ) -> list[Any]:
        try:
            from homeassistant.components.recorder import get_instance as _var_var_38
            from homeassistant.components.recorder.history import (
                get_significant_states as _var_var_39,
                state_changes_during_period as _var_var_40,
            )

            _var_var_41 = _var_var_38(context.HASS)
            _var_var_42 = entity_id.lower()
            _var_var_26 = []
            try:
                _var_var_43 = await _var_var_41.async_add_executor_job(
                    _var_var_40,
                    context.HASS,
                    start_time,
                    end_time,
                    entity_id,
                    True,
                    False,
                    None,
                )
                _var_var_26 = _var_var_43.get(_var_var_42, []) or _var_var_43.get(
                    entity_id, []
                )
            except Exception as _var_var_12:
                _LOGGER.debug(
                    "Consumer atlas state_changes query failed for %s from %s to %s: %s",
                    entity_id,
                    start_time,
                    end_time,
                    _var_var_12,
                )
            if not _var_var_26:
                _var_var_43 = await _var_var_41.async_add_executor_job(
                    _var_var_39,
                    context.HASS,
                    start_time,
                    end_time,
                    [entity_id],
                    None,
                    True,
                )
                _var_var_26 = _var_var_43.get(_var_var_42, []) or _var_var_43.get(
                    entity_id, []
                )
            return sorted(
                _var_var_26, key=lambda state: self._range_time(state.last_changed)
            )
        except ImportError as _var_var_12:
            _LOGGER.debug(
                "Recorder not available for consumer atlas backfill: %s", _var_var_12
            )
        except Exception as _var_var_12:
            _LOGGER.debug(
                "Consumer atlas recorder query failed for %s from %s to %s: %s",
                entity_id,
                start_time,
                end_time,
                _var_var_12,
            )
        if True:
            return []


class AmortizationView(HomeAssistantView):

    async def _load_monthly_cashflows(self) -> list[dict[str, Any]]:
        _var_var_0, _var_var_58 = self._runtime_config_and_tariff_manager()
        try:
            _var_var_59 = float(_get_fixed_total_price(_var_var_0))
        except (TypeError, ValueError):
            _var_var_59 = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
        try:
            _var_var_60 = float(
                _var_var_0.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
            )
        except (TypeError, ValueError):
            _var_var_60 = DEFAULT_FEED_IN_TARIFF
        _var_var_61 = effective_price_mode(context.HASS, _var_var_0)
        _var_var_60 = max(0.0, _var_var_60)
        _var_var_62 = await _corrected_daily_energy_values("2025-01-01")
        async with _get_db() as _var_var_6:
            async with _var_var_6.execute(
                "SELECT strftime('%Y-%m', date) as month,\n                          SUM(solar_to_house_kwh) as solar_to_house,\n                          SUM(battery_to_house_kwh) as battery_to_house,\n                          SUM(grid_to_battery_kwh) as grid_to_battery,\n                          SUM(smartmeter_export_kwh) as smartmeter_export,\n                          SUM(grid_export_kwh) as grid_export,\n                          COUNT(*) as days\n                   FROM stats_daily_energy\n                   WHERE date >= '2025-01-01'\n                   GROUP BY month\n                   ORDER BY month"
            ) as _var_var_16:
                _var_var_63 = await _var_var_16.fetchall()
            _var_var_64 = {}
            try:
                _var_var_65 = await _table_columns(_var_var_6, "stats_hourly_billing")
                _var_var_66 = (
                    "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
                    if "price_ct_kwh" in _var_var_65
                    else "?"
                )
                async with _var_var_6.execute(
                    f"SELECT strftime('%Y-%m', date) as month,\n                              SUM(COALESCE(feed_in_revenue_ct, 0)) / 100.0 as feed_in_revenue_eur,\n                              SUM(\n                                  (\n                                      COALESCE(solar_to_house_kwh, 0)\n                                      + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                  )\n                                  * {_var_var_66}\n                              ) / 100.0 as self_savings_eur,\n                              SUM(\n                                  COALESCE(solar_to_house_kwh, 0)\n                                  + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                              ) as self_consumption_kwh,\n                              SUM(grid_export_kwh) as export_kwh,\n                              COUNT(*) as hours\n                       FROM stats_hourly_billing\n                       GROUP BY month\n                       ORDER BY month",
                    (_var_var_59,),
                ) as _var_var_16:
                    for _var_var_17 in await _var_var_16.fetchall():
                        if _var_var_17[0]:
                            if (_var_var_17[5] or 0) > 0:
                                _var_var_64[_var_var_17[0]] = {
                                    "feed_in_revenue_eur": float(_var_var_17[1] or 0),
                                    "self_savings_eur": float(_var_var_17[2] or 0),
                                    "self_consumption_kwh": float(_var_var_17[3] or 0),
                                    "export_kwh": float(_var_var_17[4] or 0),
                                    "hours": float(_var_var_17[5] or 0),
                                }
            except Exception as _var_var_12:
                _LOGGER.debug(
                    "Amortization hourly billing data unavailable: %s", _var_var_12
                )
            _var_var_67 = None
            if any(
                (
                    energy_flows_changed(_var_var_81)
                    for _var_var_81 in _var_var_62.values()
                )
            ):
                try:
                    _var_var_67 = await load_hourly_component_days(
                        _var_var_6, "2025-01-01", None, _var_var_59
                    )
                except Exception:
                    _LOGGER.debug("Amortization correction base unavailable")
                    _var_var_67 = None
        _var_var_46 = []
        for _var_var_17 in _var_var_63:
            _var_var_68 = _var_var_17[0]
            if not _var_var_68:
                continue
            _var_var_69, _var_var_70 = (
                int(_var_var_94) for _var_var_94 in _var_var_68.split("-")
            )
            _var_var_71 = _var_var_59
            _var_var_72 = _var_var_60
            _var_var_73 = "config_fallback"
            if _var_var_58 is not None:
                try:
                    _var_var_74 = await _var_var_58.get_monthly_fallbacks(
                        _var_var_69,
                        _var_var_70,
                        import_price_ct=_var_var_59,
                        feed_in_tariff_ct=_var_var_60,
                        base_fee_eur=0,
                    )
                    _var_var_75 = _var_var_74["import_price_ct"]
                    _var_var_76 = _var_var_74["feed_in_tariff_ct"]
                    _var_var_71 = float(
                        _var_var_75["value"]
                        if _var_var_75["value"] is not None
                        else _var_var_59
                    )
                    _var_var_72 = float(_var_var_76["value"] or _var_var_60)
                    _var_var_73 = _var_var_75.get("source", "fixed_month")
                except Exception as _var_var_12:
                    _LOGGER.debug(
                        "Amortization monthly tariff fallback failed for %s: %s",
                        _var_var_68,
                        _var_var_12,
                    )
            _var_var_77 = max(
                0.0, float(_var_var_17[2] or 0) - float(_var_var_17[3] or 0)
            )
            _var_var_78 = float(_var_var_17[1] or 0) + _var_var_77
            _var_var_79 = (
                float(_var_var_17[4] or 0)
                if (_var_var_17[4] or 0) > 0
                else float(_var_var_17[5] or 0)
            )
            _var_var_82 = [
                _var_var_81["grid_export_day_kwh"]
                for _var_var_80, _var_var_81 in _var_var_62.items()
                if _var_var_80.startswith(f"{_var_var_68}-")
            ]
            if _var_var_82:
                _var_var_79 = sum(_var_var_82)
            _var_var_83 = [
                (_var_var_80, _var_var_81)
                for _var_var_80, _var_var_81 in _var_var_62.items()
                if _var_var_80.startswith(f"{_var_var_68}-")
            ]
            _var_var_84 = any(
                (
                    energy_flows_changed(_var_var_81)
                    for _var_var_95, _var_var_81 in _var_var_83
                )
            )
            if _var_var_84:
                _var_var_78 = sum(
                    (
                        own_supply_kwh(
                            _var_var_81.get("solar_to_house_day_kwh", 0.0),
                            _var_var_81.get("battery_to_house_day_kwh", 0.0),
                            _var_var_81.get("grid_to_battery_day_kwh", 0.0),
                        )
                        for _var_var_95, _var_var_81 in _var_var_83
                    )
                )
            _var_var_85 = _var_var_64.get(_var_var_68)
            if is_fixed_like(_var_var_61):
                _var_var_86 = _var_var_78 * _var_var_71 / 100
                _var_var_87 = _var_var_79 * _var_var_72 / 100
                _var_var_88 = "fixed"
            elif _var_var_85:
                _var_var_89 = legacy_dynamic_month(
                    import_kwh=0.0,
                    covered_import_kwh=0.0,
                    covered_import_cost=0.0,
                    self_kwh=_var_var_78,
                    covered_self_kwh=_var_var_85["self_consumption_kwh"],
                    covered_self_savings=_var_var_85["self_savings_eur"],
                    export_kwh=_var_var_79,
                    covered_export_kwh=_var_var_85["export_kwh"],
                    covered_export_revenue=_var_var_85["feed_in_revenue_eur"],
                    month_price_ct=_var_var_71,
                    month_feed_in_ct=_var_var_72,
                )
                if _var_var_84 and _var_var_67 is not None:
                    _var_var_90 = dynamic_month_costs(
                        [
                            component_day(_var_var_81, _var_var_67.get(_var_var_80))
                            for _var_var_80, _var_var_81 in _var_var_83
                        ],
                        changed=True,
                        legacy=_var_var_89,
                        month_price_ct=_var_var_71,
                        month_feed_in_ct=_var_var_72,
                    )
                else:
                    _var_var_90 = _var_var_89
                _var_var_86 = _var_var_90["self_savings"]
                _var_var_87 = _var_var_90["feed_in_revenue"]
                _var_var_91 = max(
                    0.0, _var_var_78 - _var_var_85["self_consumption_kwh"]
                )
                _var_var_92 = max(0.0, _var_var_79 - _var_var_85["export_kwh"])
                _var_var_88 = (
                    "dynamic"
                    if _var_var_91 <= 0.001 and _var_var_92 <= 0.001
                    else "hybrid"
                )
            else:
                _var_var_86 = _var_var_78 * _var_var_71 / 100
                _var_var_87 = _var_var_79 * _var_var_72 / 100
                _var_var_88 = "estimated"
            _var_var_93 = _var_var_86 + _var_var_87
            _var_var_46.append(
                {
                    "month": _var_var_68,
                    "benefit_eur": round(_var_var_93, 2),
                    "self_consumption_savings_eur": round(_var_var_86, 2),
                    "feed_in_revenue_eur": round(_var_var_87, 2),
                    "self_consumption_kwh": round(_var_var_78, 2),
                    "grid_export_kwh": round(_var_var_79, 2),
                    "avg_price_ct": round(_var_var_71, 2),
                    "feed_in_tariff_ct": round(_var_var_72, 2),
                    "tariff_source": _var_var_73,
                    "source": _var_var_88,
                }
            )
            continue
        return _var_var_46


class ConsumerDetailView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return live consumer detail data from configured sensors. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            A_recovered_capture_0000 = {}
            for _var_var_54 in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(_var_var_54, dict):
                    if "config" in _var_var_54:
                        A_recovered_capture_0000 = _var_var_54["config"]
                        break
            from ..core.eai_wp_sources import (
                resolved_heatpump_entity as read__recovered_capture_0002,
            )

            def read(key):
                _var_var_19 = read__recovered_capture_0002(
                    context.HASS, A_recovered_capture_0000, key
                )
                if not _var_var_19:
                    return
                _var_var_20 = context.HASS.states.get(_var_var_19)
                if _var_var_20 is None or _var_var_20.state in (
                    "unknown",
                    "unavailable",
                ):
                    return None
                return _var_var_20.state

            def read_float(key):
                _var_var_116 = read(key)
                if _var_var_116 is None:
                    return
                try:
                    return round(float(_var_var_116), 2)
                except (ValueError, TypeError):
                    return

            def read_bool(key):
                _var_var_116 = read(key)
                if _var_var_116 is None:
                    return
                return _var_var_116 in ("on", "true", "True", "1", "heat", "heating")

            _var_var_104 = {
                "consumer_heatpump_kwh": 0.0,
                "consumer_heatingrod_kwh": 0.0,
                "consumer_wallbox_kwh": 0.0,
            }
            async with _get_db() as _var_var_6:
                async with _var_var_6.execute(
                    "\n                    SELECT consumer_heatpump_kwh,\n                           consumer_heatingrod_kwh,\n                           consumer_wallbox_kwh\n                    FROM stats_daily_energy\n                    WHERE date = ?\n                    ",
                    (ha_today(context.HASS).isoformat(),),
                ) as _var_var_16:
                    _var_var_17 = await _var_var_16.fetchone()
            if _var_var_17 is not None:
                _var_var_104.update(dict(_var_var_17))
            _var_var_105 = any(
                (
                    read(_var_var_117) is not None
                    or read__recovered_capture_0002(
                        context.HASS, A_recovered_capture_0000, _var_var_117
                    )
                    for _var_var_117 in (
                        CONF_SENSOR_HEATPUMP_POWER,
                        CONF_SENSOR_HP_HEATING_MODE,
                        CONF_SENSOR_HP_STORAGE_TEMP,
                    )
                )
            )
            _var_var_106 = read_float(CONF_SENSOR_HP_GRID_ENERGY_DAILY)
            _var_var_107 = read_float(CONF_SENSOR_HP_PV_ENERGY_DAILY)
            _var_var_108, _var_var_109 = resolve_heatpump_today_kwh(
                eai_kwh=eai_electric_today_kwh(_eai_capability_snapshot(context.HASS)),
                daily_sensor_kwh=read_float(CONF_SENSOR_HEATPUMP_DAILY),
                watt_integrated_kwh=_var_var_104["consumer_heatpump_kwh"],
            )
            _var_var_110 = split_heatpump_today_energy(
                _var_var_108, _var_var_107, _var_var_106
            )
            _var_var_111 = {
                "configured": _var_var_105,
                "live": {
                    "heating_mode": read(CONF_SENSOR_HP_HEATING_MODE),
                    "dhw_mode": read(CONF_SENSOR_HP_DHW_MODE),
                    "dhw_charging": read_bool(CONF_SENSOR_HP_DHW_CHARGING),
                    "pv_active": read_bool(CONF_SENSOR_HP_PV_ACTIVE),
                    "electric_power": _get_power_kw(
                        read__recovered_capture_0002(
                            context.HASS,
                            A_recovered_capture_0000,
                            CONF_SENSOR_HP_ELECTRIC_POWER,
                        )
                    ),
                    "thermal_power": _get_power_kw(
                        read__recovered_capture_0002(
                            context.HASS,
                            A_recovered_capture_0000,
                            CONF_SENSOR_HP_THERMAL_POWER,
                        )
                    ),
                    "grid_energy_today": _var_var_110["grid_energy_today"],
                    "pv_energy_today": _var_var_110["pv_energy_today"],
                    "unassigned_energy_today": _var_var_110["unassigned_energy_today"],
                    "pv_share_percent": _var_var_110["pv_share_percent"],
                    "grid_share_percent": _var_var_110["grid_share_percent"],
                    "unassigned_share_percent": _var_var_110[
                        "unassigned_share_percent"
                    ],
                    "jaz": read_float(CONF_SENSOR_HP_JAZ),
                    "compressor_starts": read_float(CONF_SENSOR_HP_COMPRESSOR_STARTS),
                    "storage_temp": read_float(CONF_SENSOR_HP_STORAGE_TEMP),
                    "power": _display_watts(
                        _get_power_w(
                            read__recovered_capture_0002(
                                context.HASS,
                                A_recovered_capture_0000,
                                CONF_SENSOR_HEATPUMP_POWER,
                            )
                        )
                    ),
                    "daily_kwh": _var_var_110["daily_kwh"],
                    "daily_energy_source": _var_var_109,
                    "cop": read_float(CONF_SENSOR_HEATPUMP_COP),
                },
            }
            _var_var_112 = bool(
                A_recovered_capture_0000.get(CONF_SENSOR_HEATINGROD_POWER)
            )
            _var_var_113 = {
                "configured": _var_var_112,
                "live": {
                    "power": _display_watts(
                        _get_power_w(
                            A_recovered_capture_0000.get(CONF_SENSOR_HEATINGROD_POWER)
                        )
                    ),
                    "daily_kwh": round(_var_var_104["consumer_heatingrod_kwh"] or 0, 2),
                },
            }
            _var_var_114 = bool(A_recovered_capture_0000.get(CONF_SENSOR_WALLBOX_POWER))
            _var_var_115 = {
                "configured": _var_var_114,
                "live": {
                    "power": _display_watts(
                        _get_power_w(
                            A_recovered_capture_0000.get(CONF_SENSOR_WALLBOX_POWER)
                        )
                    ),
                    "daily_kwh": round(_var_var_104["consumer_wallbox_kwh"] or 0, 2),
                    "state": read(CONF_SENSOR_WALLBOX_STATE),
                    "charge_mode": read(CONF_SENSOR_WB_CHARGE_MODE),
                    "session_kwh": read_float(CONF_SENSOR_WB_ENERGY_SESSION),
                },
            }
            return web.json_response(
                {
                    "success": True,
                    "heatpump": _var_var_111,
                    "heatingrod": _var_var_113,
                    "wallbox": _var_var_115,
                }
            )
        except Exception as _var_var_12:
            _LOGGER.error("Error in consumer detail: %s", _var_var_12, exc_info=True)
            return web.json_response({"error": str(_var_var_12)}, status=500)
