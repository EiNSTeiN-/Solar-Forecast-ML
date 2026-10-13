# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite
from ..utils.time_utils import ha_now, ha_today
from ..const import (
    DOMAIN,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    CONF_LEGACY_FIXED_PRICE_CT,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_IMPORT_KWH,
    DEFAULT_BILLING_FIXED_PRICE,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    BILLING_CACHE_TTL_SECONDS,
)
from .energy_balance import calculate_balance_diagnostics
from .grid_split import billed_import_kwh
from .corrections import EnergyCorrectionStore
from .corrected_energy import (
    corrected_daily,
    corrected_day_cost,
    corrected_value,
    correction_remainder,
    kwh_unchanged,
    scale_billing_rows,
    statement_energy_cost,
    corrected_daily_report,
)


class BillingCalculator:

    async def _get_dynamic_costs_from_db(
        self, billing_start: date, config: dict[str, Any], price_mode: str
    ) -> dict[str, Any]:
        """Calculate costs and savings from hourly billing and daily energy data. @zara"""
        from collections import defaultdict as _var_var_111
        from .price_mode import (
            MODE_GPM_FIXED as _var_var_112,
            gpm_fixed_total_ct as _var_var_113,
            resolve_hour_price as _var_var_114,
        )

        _var_var_115 = {
            "grid_cost_eur": 0,
            "savings_eur": 0,
            "feed_in_revenue_eur": 0,
            "weighted_avg_price_ct": 0,
            "consumers": {},
            "hours_without_price": 0,
        }
        if not self.is_db_available:
            return _var_var_115
        _var_var_25 = billing_start.isoformat()
        _var_var_116 = ha_today(self._hass).isoformat()
        _var_var_117 = self._feed_in_tariff_ct(config)
        _var_var_118 = 0.0
        _var_var_119 = 0.0
        _var_var_120 = 0.0
        _var_var_121 = 0.0
        _var_var_122 = 0.0
        _var_var_67 = 0
        _var_var_123 = set()
        _var_var_124 = _var_var_111(float)
        _var_var_125 = _var_var_111(float)
        _var_var_126 = _var_var_111(float)
        _var_var_127 = _var_var_111(lambda: _var_var_111(float))
        _var_var_128 = _var_var_111(lambda: _var_var_111(float))
        try:
            await self._ensure_hourly_finance_columns()
            async with self._get_db() as _var_var_26:
                async with _var_var_26.execute(
                    "\n                    SELECT\n                        hb.date,\n                        hb.hour,\n                        COALESCE(hb.grid_import_kwh, 0) as grid_import_kwh,\n                        COALESCE(hb.grid_to_house_kwh, 0) as grid_to_house_kwh,\n                        COALESCE(hb.grid_to_battery_kwh, 0) as grid_to_battery_kwh,\n                        COALESCE(hb.solar_to_house_kwh, 0) as solar_to_house_kwh,\n                        COALESCE(hb.battery_to_house_kwh, 0) as battery_to_house_kwh,\n                        COALESCE(hb.grid_export_kwh, 0) as grid_export_kwh,\n                        COALESCE(hb.feed_in_revenue_ct, 0) as feed_in_revenue_ct,\n                        COALESCE(hb.feed_in_tariff_ct, 0) as feed_in_tariff_ct,\n                        COALESCE(hb.consumer_heatpump_kwh, 0) as consumer_heatpump_kwh,\n                        COALESCE(hb.consumer_heatpump_cost_ct, 0) as consumer_heatpump_cost_ct,\n                        COALESCE(hb.consumer_heatingrod_kwh, 0) as consumer_heatingrod_kwh,\n                        COALESCE(hb.consumer_heatingrod_cost_ct, 0) as consumer_heatingrod_cost_ct,\n                        COALESCE(hb.consumer_wallbox_kwh, 0) as consumer_wallbox_kwh,\n                        COALESCE(hb.consumer_wallbox_cost_ct, 0) as consumer_wallbox_cost_ct,\n                        gpm.total_price as gpm_total,\n                        hb.price_ct_kwh as stored_price\n                    FROM stats_hourly_billing hb\n                    LEFT JOIN GPM_price_history gpm\n                        ON gpm.hour = hb.hour\n                        AND gpm.timestamp >= hb.date || 'T00:00:00'\n                        AND gpm.timestamp < date(hb.date, '+1 day') || 'T00:00:00'\n                    WHERE hb.date >= ?\n                    ORDER BY hb.date, hb.hour\n                ",
                    (_var_var_25,),
                ) as _var_var_27:
                    _var_var_130 = [
                        {
                            _var_var_129: _var_var_30[_var_var_129]
                            for _var_var_129 in _var_var_30.keys()
                        }
                        async for _var_var_30 in _var_var_27
                    ]
                _var_var_24 = await self._correction_store.async_projection(_var_var_25)
                _var_var_131 = await corrected_daily(
                    _var_var_26, _var_var_25, _var_var_116, _var_var_24
                )
                scale_billing_rows(_var_var_130, _var_var_131, _var_var_24)
                _var_var_132 = (
                    _var_var_113(self._hass) if price_mode == _var_var_112 else None
                )
                for _var_var_30 in _var_var_130:
                    _var_var_133 = _var_var_114(
                        gpm_total=_var_var_30["gpm_total"],
                        stored_price=_var_var_30["stored_price"],
                        mode=price_mode,
                        config=config,
                        fixed_total_ct=_var_var_132,
                    )
                    if _var_var_133 is None:
                        _var_var_67 += 1
                        continue
                    _var_var_134 = billed_import_kwh(
                        _var_var_30["grid_import_kwh"],
                        _var_var_30["grid_to_house_kwh"],
                        _var_var_30["grid_to_battery_kwh"],
                        meter_configured=True,
                    )
                    _var_var_55 = max(
                        0.0,
                        (_var_var_30["battery_to_house_kwh"] or 0)
                        - (_var_var_30["grid_to_battery_kwh"] or 0),
                    )
                    _var_var_135 = (
                        _var_var_30["solar_to_house_kwh"] or 0
                    ) + _var_var_55
                    _var_var_118 += _var_var_134 * _var_var_133
                    _var_var_119 += _var_var_135 * _var_var_133
                    _var_var_121 += _var_var_134
                    _var_var_122 += _var_var_134 * _var_var_133
                    _var_var_123.add(_var_var_30["date"])
                    _var_var_136 = _var_var_30["grid_export_kwh"] or 0
                    _var_var_137 = self._price_or_fallback(
                        _var_var_30["feed_in_tariff_ct"], _var_var_117
                    )
                    _var_var_138 = (
                        _var_var_30["feed_in_revenue_ct"] or _var_var_136 * _var_var_137
                    )
                    _var_var_120 += _var_var_138
                    _var_var_126[_var_var_30["date"]] += _var_var_136
                    _var_var_124[_var_var_30["date"]] += (
                        _var_var_30["grid_to_battery_kwh"] or 0
                    )
                    _var_var_125[_var_var_30["date"]] += _var_var_135
                    for _var_var_129, _var_var_139, _var_var_140 in (
                        (
                            "heatpump",
                            "consumer_heatpump_kwh",
                            "consumer_heatpump_cost_ct",
                        ),
                        (
                            "heatingrod",
                            "consumer_heatingrod_kwh",
                            "consumer_heatingrod_cost_ct",
                        ),
                        ("wallbox", "consumer_wallbox_kwh", "consumer_wallbox_cost_ct"),
                    ):
                        _var_var_141 = _var_var_30[_var_var_139] or 0
                        _var_var_142 = (
                            _var_var_30[_var_var_140] or _var_var_141 * _var_var_133
                        )
                        _var_var_128[_var_var_129]["kwh"] += _var_var_141
                        _var_var_128[_var_var_129]["cost_ct"] += _var_var_142
                        if _var_var_30["date"] == _var_var_116:
                            _var_var_128[_var_var_129]["today_cost_ct"] += _var_var_142
                        _var_var_127[_var_var_30["date"]][_var_var_129] += _var_var_141
                    continue
                async with _var_var_26.execute(
                    "\n                    SELECT\n                        e.date,\n                        COALESCE(e.grid_import_kwh, 0) as grid_import_kwh,\n                        COALESCE(e.grid_to_house_kwh, 0) as grid_to_house_kwh,\n                        COALESCE(e.grid_to_battery_kwh, 0) as grid_to_battery_kwh,\n                        COALESCE(e.solar_to_house_kwh, 0) as solar_to_house_kwh,\n                        COALESCE(e.battery_to_house_kwh, 0) as battery_to_house_kwh,\n                        COALESCE(NULLIF(e.smartmeter_export_kwh, 0), e.grid_export_kwh, 0) as export_kwh,\n                        COALESCE(e.consumer_heatpump_kwh, 0) as consumer_heatpump_kwh,\n                        COALESCE(e.consumer_heatingrod_kwh, 0) as consumer_heatingrod_kwh,\n                        COALESCE(e.consumer_wallbox_kwh, 0) as consumer_wallbox_kwh,\n                        COALESCE(p.average_total, 0) as daily_avg_price_ct\n                    FROM stats_daily_energy e\n                    LEFT JOIN GPM_daily_averages p ON e.date = p.date\n                    WHERE e.date >= ?\n                    ORDER BY e.date\n                ",
                    (_var_var_25,),
                ) as _var_var_27:
                    async for _var_var_30 in _var_var_27:
                        _var_var_133 = _var_var_114(
                            daily_average=_var_var_30["daily_avg_price_ct"],
                            current_average=None,
                            mode=price_mode,
                            config=config,
                            fixed_total_ct=_var_var_132,
                        )
                        if _var_var_133 is None:
                            continue
                        if _var_var_30["date"] in _var_var_123:
                            _var_var_143 = _var_var_131.get(
                                str(_var_var_30["date"]), {}
                            )
                            _var_var_144 = _var_var_143.get(
                                "grid_to_battery_day_kwh",
                                _var_var_30["grid_to_battery_kwh"] or 0,
                            )
                            _var_var_145 = _var_var_124.get(_var_var_30["date"], 0)
                            _var_var_146 = not kwh_unchanged(
                                _var_var_143.get(
                                    "raw_grid_to_battery_kwh", _var_var_144
                                ),
                                _var_var_144,
                            )
                            _var_var_147 = correction_remainder(
                                _var_var_144, _var_var_145, corrected=_var_var_146
                            )
                            if _var_var_147 != 0:
                                _var_var_118 += _var_var_147 * _var_var_133
                                _var_var_121 += _var_var_147
                                _var_var_122 += _var_var_147 * _var_var_133
                            _var_var_148 = max(
                                0.0,
                                _var_var_143.get(
                                    "battery_to_house_day_kwh",
                                    _var_var_30["battery_to_house_kwh"] or 0,
                                )
                                - _var_var_143.get(
                                    "grid_to_battery_day_kwh",
                                    _var_var_30["grid_to_battery_kwh"] or 0,
                                ),
                            )
                            _var_var_149 = (
                                _var_var_143.get(
                                    "solar_to_house_day_kwh",
                                    _var_var_30["solar_to_house_kwh"] or 0,
                                )
                                + _var_var_148
                            )
                            _var_var_150 = _var_var_125.get(_var_var_30["date"], 0)
                            _var_var_151 = (
                                _var_var_30["solar_to_house_kwh"] or 0
                            ) + max(
                                0.0,
                                (_var_var_30["battery_to_house_kwh"] or 0)
                                - (_var_var_30["grid_to_battery_kwh"] or 0),
                            )
                            _var_var_152 = correction_remainder(
                                _var_var_149,
                                _var_var_150,
                                corrected=not kwh_unchanged(_var_var_151, _var_var_149),
                            )
                            if _var_var_152 != 0:
                                _var_var_119 += _var_var_152 * _var_var_133
                            _var_var_36 = _var_var_30["export_kwh"] or 0
                            _var_var_153 = _var_var_143.get(
                                "grid_export_day_kwh", _var_var_36
                            )
                            _var_var_154 = correction_remainder(
                                _var_var_153,
                                _var_var_126.get(_var_var_30["date"], 0),
                                corrected=not kwh_unchanged(_var_var_36, _var_var_153),
                            )
                            if _var_var_154 != 0:
                                _var_var_120 += _var_var_154 * _var_var_117
                            for _var_var_129, _var_var_155, _var_var_156 in (
                                (
                                    "heatpump",
                                    "consumer_heatpump_kwh",
                                    "consumer_heatpump_day_kwh",
                                ),
                                (
                                    "heatingrod",
                                    "consumer_heatingrod_kwh",
                                    "consumer_heatingrod_day_kwh",
                                ),
                                (
                                    "wallbox",
                                    "consumer_wallbox_kwh",
                                    "consumer_wallbox_day_kwh",
                                ),
                            ):
                                _var_var_157 = _var_var_30[_var_var_155] or 0
                                _var_var_158 = _var_var_143.get(
                                    _var_var_156, _var_var_157
                                )
                                _var_var_159 = correction_remainder(
                                    _var_var_158,
                                    _var_var_127[_var_var_30["date"]].get(
                                        _var_var_129, 0
                                    ),
                                    corrected=not kwh_unchanged(
                                        _var_var_157, _var_var_158
                                    ),
                                )
                                if _var_var_159 != 0:
                                    _var_var_142 = _var_var_159 * _var_var_133
                                    _var_var_128[_var_var_129][
                                        "cost_ct"
                                    ] += _var_var_142
                                    if _var_var_30["date"] == _var_var_116:
                                        _var_var_128[_var_var_129][
                                            "today_cost_ct"
                                        ] += _var_var_142
                        else:
                            _var_var_143 = _var_var_131.get(
                                str(_var_var_30["date"]), {}
                            )
                            _var_var_160 = _var_var_30["grid_import_kwh"]
                            if (
                                "raw_grid_import_kwh" in _var_var_143
                                or "grid_import_day_kwh" in _var_var_143
                            ):
                                _var_var_160 = _var_var_143.get(
                                    "grid_import_day_kwh",
                                    _var_var_143.get(
                                        "raw_grid_import_kwh", _var_var_160
                                    ),
                                )
                            _var_var_161 = bool(
                                config.get(CONF_SENSOR_SMARTMETER_IMPORT)
                                or config.get(CONF_SENSOR_SMARTMETER_IMPORT_KWH)
                            )
                            _var_var_134 = billed_import_kwh(
                                _var_var_160,
                                _var_var_30["grid_to_house_kwh"],
                                _var_var_30["grid_to_battery_kwh"],
                                meter_configured=_var_var_161,
                            )
                            _var_var_55 = max(
                                0.0,
                                _var_var_143.get(
                                    "battery_to_house_day_kwh",
                                    _var_var_30["battery_to_house_kwh"] or 0,
                                )
                                - _var_var_143.get(
                                    "grid_to_battery_day_kwh",
                                    _var_var_30["grid_to_battery_kwh"] or 0,
                                ),
                            )
                            _var_var_135 = (
                                _var_var_143.get(
                                    "solar_to_house_day_kwh",
                                    _var_var_30["solar_to_house_kwh"] or 0,
                                )
                                + _var_var_55
                            )
                            _var_var_118 += _var_var_134 * _var_var_133
                            _var_var_119 += _var_var_135 * _var_var_133
                            _var_var_36 = _var_var_30["export_kwh"] or 0
                            _var_var_153 = _var_var_143.get(
                                "grid_export_day_kwh", _var_var_36
                            )
                            _var_var_120 += corrected_day_cost(
                                _var_var_36,
                                _var_var_153,
                                _var_var_36 * _var_var_117,
                                fallback_price_ct=_var_var_117,
                                cost_in_cent=True,
                            )
                            _var_var_121 += _var_var_134
                            _var_var_122 += _var_var_134 * _var_var_133
                            for _var_var_129, _var_var_155, _var_var_156 in (
                                (
                                    "heatpump",
                                    "consumer_heatpump_kwh",
                                    "consumer_heatpump_day_kwh",
                                ),
                                (
                                    "heatingrod",
                                    "consumer_heatingrod_kwh",
                                    "consumer_heatingrod_day_kwh",
                                ),
                                (
                                    "wallbox",
                                    "consumer_wallbox_kwh",
                                    "consumer_wallbox_day_kwh",
                                ),
                            ):
                                _var_var_157 = _var_var_30[_var_var_155] or 0
                                _var_var_158 = _var_var_143.get(
                                    _var_var_156, _var_var_157
                                )
                                _var_var_142 = corrected_day_cost(
                                    _var_var_157,
                                    _var_var_158,
                                    _var_var_157 * _var_var_133,
                                    fallback_price_ct=_var_var_133,
                                    cost_in_cent=True,
                                )
                                _var_var_128[_var_var_129]["cost_ct"] += _var_var_142
                                if _var_var_30["date"] == _var_var_116:
                                    _var_var_128[_var_var_129][
                                        "today_cost_ct"
                                    ] += _var_var_142
        except Exception as _var_var_4:
            _LOGGER.error("Error calculating dynamic costs from DB: %s", _var_var_4)
            return {
                "grid_cost_eur": 0,
                "savings_eur": 0,
                "feed_in_revenue_eur": 0,
                "weighted_avg_price_ct": 0,
                "consumers": {},
                "hours_without_price": _var_var_67,
            }
        _var_var_162 = round(_var_var_122 / _var_var_121, 2) if _var_var_121 > 0 else 0
        _LOGGER.debug(
            "Dynamic billing: cost=%.2f EUR, savings=%.2f EUR, avg_price=%.2f ct, hourly_days=%d",
            _var_var_118 / 100,
            _var_var_119 / 100,
            _var_var_162,
            len(_var_var_123),
        )
        return {
            "grid_cost_eur": round(_var_var_118 / 100, 2),
            "savings_eur": round(_var_var_119 / 100, 2),
            "feed_in_revenue_eur": round(_var_var_120 / 100, 2),
            "weighted_avg_price_ct": _var_var_162,
            "hours_without_price": _var_var_67,
            "consumers": {
                _var_var_129: {
                    "cost_eur": round(data.get("cost_ct", 0) / 100, 2),
                    "today_cost_eur": round(data.get("today_cost_ct", 0) / 100, 2),
                }
                for _var_var_129, data in _var_var_128.items()
            },
        }
