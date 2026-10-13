# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import date, datetime, time, timedelta
from math import isfinite, pi, sin
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..const import (
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_WALLBOX_POWER,
)
from ..core.corrected_energy import (
    corrected_daily,
    overlay_journal_row,
    projection_from_runtime,
)
from ..utils.time_utils import ha_now, ha_timezone, ha_today
from .premium_dashboard import (
    PremiumDashboardProviderError,
    _premium_is_live,
    _provider_snapshot,
)
from .views import _get_db, local_only


async def _selected_day_bundle(
    hass: HomeAssistant, selected_day: date, *, sensor_configured: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    _var_var_147 = _local_astronomy_profile(hass, selected_day)
    async with _get_db() as _var_var_148:
        async with _var_var_148.execute(
            "\n            SELECT hb.hour, hb.solar_yield_kwh, hb.home_consumption_kwh,\n                   hb.grid_import_kwh, hb.grid_export_kwh,\n                   hb.solar_to_house_kwh, hb.solar_to_battery_kwh,\n                   hb.battery_to_house_kwh, hb.grid_to_house_kwh,\n                   hb.grid_to_battery_kwh, hb.grid_import_cost_ct,\n                   hb.feed_in_revenue_ct, hb.price_ct_kwh,\n                   hb.consumer_heatingrod_kwh,\n                   hb.battery_soc_min, hb.battery_soc_avg,\n                   hb.battery_soc_max, hb.battery_soc_end,\n                   COALESCE(\n                       hb.battery_soc_end,\n                       (\n                           SELECT ps.battery_soc\n                           FROM stats_power_sources AS ps\n                           WHERE ps.date = hb.date\n                             AND ps.hour = hb.hour\n                             AND ps.battery_soc IS NOT NULL\n                             AND ps.battery_soc BETWEEN 0 AND 100\n                           ORDER BY ps.timestamp DESC\n                           LIMIT 1\n                       )\n                   ) AS battery_soc_percent\n            FROM stats_hourly_billing AS hb\n            WHERE hb.date = ?\n            ORDER BY hb.hour ASC\n            ",
            (selected_day.isoformat(),),
        ) as _var_var_141:
            _var_var_149 = await _var_var_141.fetchall()
        async with _var_var_148.execute(
            "\n            SELECT predicted_total_kwh, actual_total_kwh, accuracy_percent\n            FROM daily_summaries\n            WHERE date = ?\n            LIMIT 1\n            ",
            (selected_day.isoformat(),),
        ) as _var_var_141:
            _var_var_150 = await _var_var_141.fetchone()
        async with _var_var_148.execute(
            "\n            SELECT actual_kwh, sfml_forecast_kwh, external_1_kwh, external_2_kwh,\n                   best_source\n            FROM stats_forecast_comparison\n            WHERE date = ?\n            LIMIT 1\n            ",
            (selected_day.isoformat(),),
        ) as _var_var_141:
            _var_var_151 = await _var_var_141.fetchone()
    _var_var_152 = _day_timeline(
        selected_day,
        _var_var_149,
        _var_var_147.get("hour_phases", {}),
        sensor_configured=sensor_configured,
    )
    _var_var_152["daily_forecast"] = _daily_forecast_record(_var_var_150)
    if _var_var_151:
        _var_var_152["forecast_comparison"] = {
            "actual_kwh": _optional_number(_value(_var_var_151, "actual_kwh")),
            "sfml_forecast_kwh": _optional_number(
                _value(_var_var_151, "sfml_forecast_kwh")
            ),
            "external_1_kwh": _optional_number(_value(_var_var_151, "external_1_kwh")),
            "external_2_kwh": _optional_number(_value(_var_var_151, "external_2_kwh")),
            "best_source": _value(_var_var_151, "best_source"),
        }
    else:
        _var_var_152["forecast_comparison"] = None
    if True:
        return (_var_var_152, _var_var_147)
