# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
from datetime import timedelta
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _column_expr,
    _energy_correction_projection,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_stats_gpm_coordinator,
    _table_columns,
    local_only,
)
from ..const import (
    CONF_FEED_IN_TARIFF,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_FEED_IN_TARIFF,
)
from ..core.corrected_energy import (
    autarky_percent,
    corrected_daily,
    corrected_day_cost,
    corrected_value,
    kwh_unchanged,
    own_supply_kwh,
    scale_hour_money,
)
from ..utils.time_utils import ha_now, ha_today
from ..core.dev_mode import is_dev_active, json_dev_required
from .statistics_views import (
    dataset_rows,
    license_gate,
    payload_for_request,
    statistics_export_map,
)


async def _export_legacy_daily_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    _var_var_27 = _export_cutoff(request)
    _var_var_19 = [
        "date",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "self_consumption_kwh",
        "autarkie_percent",
        "peak_solar_w",
        "peak_solar_time",
        "total_cost_eur",
        "feed_in_revenue_eur",
        "savings_eur",
    ]
    _var_var_20 = []
    _var_var_28 = await _energy_correction_projection(_var_var_27)
    async with _get_db() as _var_var_29:
        _var_var_30 = await _table_columns(_var_var_29, "stats_daily_energy")
        _var_var_32 = [
            (
                _column_expr(_var_var_30, _var_var_31, default="0")
                if _var_var_31 != "peak_solar_time"
                else _column_expr(_var_var_30, _var_var_31)
            )
            for _var_var_31 in _var_var_19
        ]
        async with _var_var_29.execute(
            f"SELECT {', '.join(_var_var_32)}\n                FROM stats_daily_energy\n                WHERE date >= ?\n                ORDER BY date",
            (_var_var_27,),
        ) as _var_var_2:
            for _var_var_3 in await _var_var_2.fetchall():
                _var_var_34 = [
                    _var_var_3[_var_var_33] for _var_var_33 in range(len(_var_var_19))
                ]
                _var_var_35 = _var_var_34[3]
                _var_var_36 = _var_var_34[4]
                _var_var_37 = _var_var_34[9]
                _var_var_38 = _var_var_34[10]
                _var_var_34[1] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "solar_yield_day_kwh",
                    _var_var_34[1],
                )
                _var_var_34[2] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "home_consumption_day_kwh",
                    _var_var_34[2],
                )
                _var_var_34[3] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "grid_import_day_kwh",
                    _var_var_34[3],
                )
                _var_var_34[4] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "grid_export_day_kwh",
                    _var_var_34[4],
                )
                _var_var_39 = _var_var_28.get(str(_var_var_34[0]), {})
                if "grid_import_day_kwh" in _var_var_39:
                    _var_var_34[9] = corrected_day_cost(
                        _var_var_35,
                        _var_var_34[3],
                        _var_var_37,
                        fallback_price_ct=_export_import_price_ct(),
                        corrected=True,
                    )
                if "grid_export_day_kwh" in _var_var_39:
                    _var_var_34[10] = corrected_day_cost(
                        _var_var_36,
                        _var_var_34[4],
                        _var_var_38,
                        fallback_price_ct=_export_feed_in_ct(),
                        corrected=True,
                    )
                _var_var_20.append(_var_var_34)
                continue
    if True:
        return (_var_var_19, _var_var_20)
    else:
        return (_var_var_19, _var_var_20)


async def _export_energy_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    _var_var_27 = _export_cutoff(request)
    _var_var_19 = [
        "date",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "smartmeter_import_kwh",
        "smartmeter_export_kwh",
        "self_consumption_kwh",
        "autarkie_percent",
        "solar_to_house_kwh",
        "solar_to_battery_kwh",
        "battery_to_house_kwh",
        "grid_to_house_kwh",
        "grid_to_battery_kwh",
        "battery_charge_solar_kwh",
        "battery_charge_grid_kwh",
        "consumer_heatpump_kwh",
        "consumer_heatingrod_kwh",
        "consumer_wallbox_kwh",
        "peak_solar_w",
        "peak_solar_time",
    ]
    _var_var_20 = []
    _var_var_28 = await _energy_correction_projection(_var_var_27)
    async with _get_db() as _var_var_29:
        _var_var_30 = await _table_columns(_var_var_29, "stats_daily_energy")
        _var_var_32 = [
            (
                _column_expr(_var_var_30, _var_var_31, default="0")
                if _var_var_31 != "peak_solar_time"
                else _column_expr(_var_var_30, _var_var_31)
            )
            for _var_var_31 in _var_var_19
        ]
        async with _var_var_29.execute(
            f"SELECT {', '.join(_var_var_32)}\n                FROM stats_daily_energy\n                WHERE date >= ?\n                ORDER BY date",
            (_var_var_27,),
        ) as _var_var_2:
            for _var_var_3 in await _var_var_2.fetchall():
                _var_var_34 = [_var_var_3[_var_var_31] for _var_var_31 in _var_var_19]
                _var_var_34[1] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "solar_yield_day_kwh",
                    _var_var_34[1],
                )
                _var_var_34[2] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "home_consumption_day_kwh",
                    _var_var_34[2],
                )
                _var_var_34[3] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "grid_import_day_kwh",
                    _var_var_34[3],
                )
                _var_var_34[4] = corrected_value(
                    _var_var_28,
                    str(_var_var_34[0]),
                    "grid_export_day_kwh",
                    _var_var_34[4],
                )
                _var_var_39 = _var_var_28.get(str(_var_var_34[0]), {})
                if "grid_import_day_kwh" in _var_var_39:
                    _var_var_34[5] = _var_var_34[3]
                if "grid_export_day_kwh" in _var_var_39:
                    _var_var_34[6] = _var_var_34[4]
                _project_energy_export_flows(_var_var_19, _var_var_34, _var_var_28)
                _var_var_20.append(_var_var_34)
                continue
    if True:
        return (_var_var_19, _var_var_20)
    else:
        return (_var_var_19, _var_var_20)


async def _export_billing_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    _var_var_27 = _export_cutoff(request)
    _var_var_19 = [
        "date",
        "hour",
        "hour_key",
        "price_ct_kwh",
        "grid_import_kwh",
        "grid_import_cost_eur",
        "grid_export_kwh",
        "feed_in_tariff_ct",
        "feed_in_revenue_eur",
        "self_consumption_kwh",
        "self_consumption_savings_eur",
        "solar_to_house_kwh",
        "battery_to_house_kwh",
        "grid_to_battery_kwh",
        "grid_to_house_kwh",
        "grid_to_house_cost_eur",
        "grid_to_battery_cost_eur",
        "consumer_heatpump_kwh",
        "consumer_heatpump_cost_eur",
        "consumer_heatingrod_kwh",
        "consumer_heatingrod_cost_eur",
        "consumer_wallbox_kwh",
        "consumer_wallbox_cost_eur",
        "data_source",
    ]
    _var_var_20 = []
    _var_var_41 = _get_config()
    try:
        _var_var_42 = float(_get_fixed_total_price(_var_var_41))
    except (TypeError, ValueError):
        _var_var_42 = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    async with _get_db() as _var_var_29:
        if not await _table_exists(_var_var_29, "stats_hourly_billing"):
            return (_var_var_19, _var_var_20)
        _var_var_30 = await _table_columns(_var_var_29, "stats_hourly_billing")
        _var_var_43 = (
            "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
            if "price_ct_kwh" in _var_var_30
            else "?"
        )
        _var_var_44 = "COALESCE(solar_to_house_kwh, 0) + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))"
        from ..core.grid_split import (
            META_GRID_SPLIT_BACKFILL as _var_var_45,
            META_GRID_SPLIT_DONE as _var_var_46,
            battery_export_cost_sql as _var_var_47,
            house_export_cost_sql as _var_var_48,
        )

        _var_var_49 = False
        try:
            async with _var_var_29.execute(
                "SELECT value FROM stats_schema_meta WHERE key = ?", (_var_var_45,)
            ) as _var_var_50:
                _var_var_51 = await _var_var_50.fetchone()
            if _var_var_51 is not None:
                _var_var_52 = (
                    _var_var_51["value"]
                    if hasattr(_var_var_51, "keys")
                    else _var_var_51[0]
                )
                _var_var_49 = _var_var_52 == _var_var_46
        except Exception:
            _var_var_49 = False
        _var_var_53 = _var_var_47(
            _var_var_43,
            column_present="grid_to_battery_cost_ct" in _var_var_30,
            backfill_done=_var_var_49,
        )
        _var_var_54 = _var_var_48(
            _var_var_43,
            column_present="grid_to_house_cost_ct" in _var_var_30,
            backfill_done=_var_var_49,
        )
        _var_var_55 = f"SELECT\n                    date,\n                    hour,\n                    hour_key,\n                    {_var_var_43} AS price_ct_kwh,\n                    COALESCE(grid_import_kwh, 0) AS grid_import_kwh,\n                    ROUND(COALESCE(grid_import_cost_ct, COALESCE(grid_import_kwh, 0) * {_var_var_43}) / 100.0, 4) AS grid_import_cost_eur,\n                    COALESCE(grid_export_kwh, 0) AS grid_export_kwh,\n                    COALESCE(feed_in_tariff_ct, 0) AS feed_in_tariff_ct,\n                    ROUND(COALESCE(feed_in_revenue_ct, 0) / 100.0, 4) AS feed_in_revenue_eur,\n                    ROUND({_var_var_44}, 4) AS self_consumption_kwh,\n                    ROUND(({_var_var_44}) * {_var_var_43} / 100.0, 4) AS self_consumption_savings_eur,\n                    COALESCE(solar_to_house_kwh, 0) AS solar_to_house_kwh,\n                    COALESCE(battery_to_house_kwh, 0) AS battery_to_house_kwh,\n                    COALESCE(grid_to_battery_kwh, 0) AS grid_to_battery_kwh,\n                    COALESCE(grid_to_house_kwh, 0) AS grid_to_house_kwh,\n                    ROUND(({_var_var_54}) / 100.0, 4) AS grid_to_house_cost_eur,\n                    ROUND(({_var_var_53}) / 100.0, 4) AS grid_to_battery_cost_eur,\n                    COALESCE(consumer_heatpump_kwh, 0) AS consumer_heatpump_kwh,\n                    ROUND(COALESCE(consumer_heatpump_cost_ct, 0) / 100.0, 4) AS consumer_heatpump_cost_eur,\n                    COALESCE(consumer_heatingrod_kwh, 0) AS consumer_heatingrod_kwh,\n                    ROUND(COALESCE(consumer_heatingrod_cost_ct, 0) / 100.0, 4) AS consumer_heatingrod_cost_eur,\n                    COALESCE(consumer_wallbox_kwh, 0) AS consumer_wallbox_kwh,\n                    ROUND(COALESCE(consumer_wallbox_cost_ct, 0) / 100.0, 4) AS consumer_wallbox_cost_eur,\n                    data_source\n                FROM stats_hourly_billing\n                WHERE date >= ?\n                ORDER BY date, hour"
        _var_var_56 = _var_var_55.count("?") - 1
        async with _var_var_29.execute(
            _var_var_55, (_var_var_42,) * _var_var_56 + (_var_var_27,)
        ) as _var_var_2:
            for _var_var_3 in await _var_var_2.fetchall():
                _var_var_20.append(
                    [_var_var_3[_var_var_31] for _var_var_31 in _var_var_19]
                )
        await _project_billing_export_rows(
            _var_var_20, _var_var_19, _var_var_29, _var_var_27, _var_var_42
        )
    if True:
        return (_var_var_19, _var_var_20)
    else:
        return (_var_var_19, _var_var_20)


async def _export_smart_charging_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    _var_var_27 = _export_cutoff(request)
    _var_var_19 = [
        "date",
        "hour",
        "hour_key",
        "price_ct_kwh",
        "max_price_ct",
        "force_charge_price_ct",
        "grid_to_battery_kwh",
        "solar_to_battery_kwh",
        "battery_to_house_kwh",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "is_cheap",
        "is_force_price",
        "is_night_window",
        "data_source",
    ]
    _var_var_20 = []
    _var_var_41 = _get_config()
    _var_var_89 = _get_stats_gpm_coordinator()
    _var_var_90 = {}
    if _var_var_89 is not None and isinstance(getattr(_var_var_89, "data", None), dict):
        _var_var_91 = _var_var_89.data.get("thresholds") or {}
        if isinstance(_var_var_91, dict):
            _var_var_90 = _var_var_91
    _var_var_92 = _var_var_90.get("max_price")
    _var_var_93 = _var_var_90.get("force_charge_price")
    try:
        _var_var_42 = float(_get_fixed_total_price(_var_var_41))
    except (TypeError, ValueError):
        _var_var_42 = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    async with _get_db() as _var_var_29:
        if not await _table_exists(_var_var_29, "stats_hourly_billing"):
            return (_var_var_19, _var_var_20)
        _var_var_30 = await _table_columns(_var_var_29, "stats_hourly_billing")
        _var_var_43 = (
            "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
            if "price_ct_kwh" in _var_var_30
            else "?"
        )
        async with _var_var_29.execute(
            f"SELECT\n                    date,\n                    hour,\n                    hour_key,\n                    {_var_var_43} AS price_ct_kwh,\n                    COALESCE(grid_to_battery_kwh, 0) AS grid_to_battery_kwh,\n                    COALESCE(solar_to_battery_kwh, 0) AS solar_to_battery_kwh,\n                    COALESCE(battery_to_house_kwh, 0) AS battery_to_house_kwh,\n                    COALESCE(solar_yield_kwh, 0) AS solar_yield_kwh,\n                    COALESCE(home_consumption_kwh, 0) AS home_consumption_kwh,\n                    data_source\n                FROM stats_hourly_billing\n                WHERE date >= ?\n                ORDER BY date, hour",
            (_var_var_42, _var_var_27),
        ) as _var_var_2:
            for _var_var_3 in await _var_var_2.fetchall():
                _var_var_94 = int(_var_var_3["hour"] or 0)
                _var_var_95 = float(_var_var_3["price_ct_kwh"] or 0.0)
                _var_var_96 = _var_var_94 < 6 or _var_var_94 >= 21
                _var_var_20.append(
                    [
                        _var_var_3["date"],
                        _var_var_94,
                        _var_var_3["hour_key"],
                        _var_var_95,
                        _var_var_92,
                        _var_var_93,
                        _var_var_3["grid_to_battery_kwh"],
                        _var_var_3["solar_to_battery_kwh"],
                        _var_var_3["battery_to_house_kwh"],
                        _var_var_3["solar_yield_kwh"],
                        _var_var_3["home_consumption_kwh"],
                        int(
                            _var_var_92 is not None and _var_var_95 < float(_var_var_92)
                        ),
                        int(
                            _var_var_93 is not None and _var_var_95 < float(_var_var_93)
                        ),
                        int(_var_var_96),
                        _var_var_3["data_source"],
                    ]
                )
    if True:
        return (_var_var_19, _var_var_20)
    else:
        return (_var_var_19, _var_var_20)
