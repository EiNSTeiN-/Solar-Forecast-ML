# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Smart-charging dashboard view."""
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

_LOGGER = logging.getLogger(__name__)


def _get_local_naive_range_for_local_dates(local_dates: list[str]) -> tuple[str, str]:
    """Return the local-naive DB range for Home Assistant local dates. @zara"""
    return local_dates_range_strings(local_dates)


class SmartChargingDashboardView(HomeAssistantView):
    """API for Smart Charging page — live status, KPIs, history and savings. @zara"""

    url = "/api/sfml_stats/smart_charging/dashboard"
    name = "api:sfml_stats:smart_charging:dashboard"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return smart charging live data and historical charts. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            config = {}
            gpm_coordinator = None
            billing_calculator = None
            for entry_data in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(entry_data, dict):
                    if "config" in entry_data:
                        config = entry_data["config"]
                    if "gpm_coordinator" in entry_data:
                        gpm_coordinator = entry_data["gpm_coordinator"]
                    if "billing_calculator" in entry_data:
                        billing_calculator = entry_data["billing_calculator"]
            smart_charging_enabled = bool(
                config.get(CONF_SMART_CHARGING_ENABLED, False)
            )
            soc_entity_id = config.get(CONF_BATTERY_SOC_SENSOR) or config.get(
                CONF_SENSOR_BATTERY_SOC
            )
            battery_configured = bool(
                config.get(CONF_SENSOR_BATTERY_SOC)
                or config.get(CONF_BATTERY_SOC_SENSOR)
            )
            live_status = {
                "enabled": smart_charging_enabled,
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
                "capacity_kwh": config.get(CONF_BATTERY_CAPACITY),
                "min_soc": config.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                "max_soc": config.get(CONF_MAX_SOC, DEFAULT_MAX_SOC),
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
            if gpm_coordinator and gpm_coordinator.data:
                coordinator_data = gpm_coordinator.data
                live_thresholds = coordinator_data.get("thresholds") or {}
                if isinstance(live_thresholds, dict):
                    live_status["max_price"] = live_thresholds.get("max_price")
                    live_status["force_charge_price"] = live_thresholds.get(
                        "force_charge_price"
                    )
                live_status["active"] = coordinator_data.get(
                    "smart_charging_active", False
                )
                live_status["target_soc"] = coordinator_data.get(
                    "smart_charging_target_soc"
                )
                live_status["current_soc"] = coordinator_data.get(
                    "smart_charging_current_soc"
                )
                live_status["reason"] = coordinator_data.get(
                    "smart_charging_reason", "unknown"
                )
                live_status["decision"] = coordinator_data.get(
                    "smart_charging_decision", "not_load"
                )
                live_status["requested_grid_charge_kwh"] = coordinator_data.get(
                    "smart_charging_requested_grid_charge_kwh", 0.0
                )
                live_status["effective_storage_cost_ct_kwh"] = coordinator_data.get(
                    "smart_charging_effective_storage_cost_ct_kwh"
                )
                live_status["compared_future_price_ct_kwh"] = coordinator_data.get(
                    "smart_charging_compared_future_price_ct_kwh"
                )
                live_status["effective_roundtrip_efficiency"] = coordinator_data.get(
                    "smart_charging_effective_roundtrip_efficiency"
                )
                live_status["reserved_future_grid_charge_kwh"] = coordinator_data.get(
                    "smart_charging_reserved_future_grid_charge_kwh", 0.0
                )
                live_status["solar_forecast_today_kwh"] = coordinator_data.get(
                    "solar_forecast_today"
                )
                live_status["solar_forecast_tomorrow_kwh"] = coordinator_data.get(
                    "solar_forecast_tomorrow"
                )
            price_mode = effective_price_mode(context.HASS, config)
            current_price_ct = None
            if price_mode == MODE_LEGACY_FIXED:
                current_price_ct = _get_fixed_total_price(config)
            else:
                try:
                    async with _get_db() as connection:
                        async with connection.execute(
                            "SELECT total_price FROM GPM_current_price WHERE id = 1"
                        ) as cursor:
                            row = await cursor.fetchone()
                        if row and row[0] is not None:
                            current_price_ct = row[0]
                        else:
                            current_datetime = dt_util.now()
                            current_hour = current_datetime.hour
                            today_iso = current_datetime.date().isoformat()
                            price_range_start, price_range_end = (
                                _get_local_naive_range_for_local_date(today_iso)
                            )
                            async with connection.execute(
                                "SELECT total_price FROM GPM_price_history\n                                   WHERE timestamp >= ? AND timestamp < ? AND hour = ?",
                                (price_range_start, price_range_end, current_hour),
                            ) as cursor:
                                row = await cursor.fetchone()
                            if row:
                                current_price_ct = row[0]
                except Exception as lookup_error:
                    _LOGGER.debug(
                        "Error reading current price for smart charging dashboard: %s",
                        lookup_error,
                    )
            if current_price_ct is None and gpm_coordinator and gpm_coordinator.data:
                current_price_ct = gpm_coordinator.data.get("total_price")
            live_status["current_price"] = current_price_ct
            if live_status["current_soc"] is None and soc_entity_id:
                try:
                    soc_state = context.HASS.states.get(soc_entity_id)
                    if soc_state is not None and soc_state.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        live_status["current_soc"] = float(soc_state.state)
                except (ValueError, TypeError):
                    pass
            fallback_soc_entity_id = config.get(CONF_SENSOR_BATTERY_SOC)
            if (
                live_status["current_soc"] is None or live_status["current_soc"] == 0.0
            ) and fallback_soc_entity_id:
                try:
                    fallback_soc_state = context.HASS.states.get(fallback_soc_entity_id)
                    if (
                        fallback_soc_state is not None
                        and fallback_soc_state.state
                        not in ("unknown", "unavailable", None)
                    ):
                        fallback_soc = float(fallback_soc_state.state)
                        if fallback_soc > 1.0:
                            live_status["current_soc"] = fallback_soc
                except (ValueError, TypeError):
                    pass
            today = dt_util.now().date()
            week_start = today - timedelta(days=7)
            billing_start = today.replace(day=1)
            if billing_calculator:
                try:
                    billing_start = billing_calculator._get_billing_start_date(config)
                except Exception:
                    pass
            kpis = {
                "today": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
                "week": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
                "period": {"charged_kwh": 0.0, "avg_price_ct": 0.0, "savings_eur": 0.0},
            }
            try:
                async with _get_db() as connection:
                    hourly_columns = await _table_columns(
                        connection, "stats_hourly_billing"
                    )
                    hourly_price_expression = (
                        "COALESCE(price_ct_kwh, 0.0)"
                        if "price_ct_kwh" in hourly_columns
                        else "0.0"
                    )
                    async with connection.execute(
                        f"\n                        SELECT \n                            date,\n                            COALESCE(grid_to_battery_kwh, 0.0),\n                            COALESCE(solar_to_battery_kwh, 0.0),\n                            COALESCE(battery_to_house_kwh, 0.0),\n                            {hourly_price_expression}\n                        FROM stats_hourly_billing\n                        ORDER BY hour_key ASC\n                        "
                    ) as cursor:
                        rows = await cursor.fetchall()
                stored_energy_kwh = 0.0
                stored_grid_fraction = 0.0
                stored_grid_price_ct = 0.0
                today_grid_charge_kwh = 0.0
                today_charge_cost_ct = 0.0
                today_savings_ct = 0.0
                today_iso = today.isoformat()
                week_grid_charge_kwh = 0.0
                week_charge_cost_ct = 0.0
                week_savings_ct = 0.0
                week_start_iso = week_start.isoformat()
                period_grid_charge_kwh = 0.0
                period_charge_cost_ct = 0.0
                period_savings_ct = 0.0
                billing_start_iso = billing_start.isoformat()
                hourly_grid_charge_by_date = {}
                hourly_charge_cost_by_date = {}
                for row in rows:
                    row_date = row[0]
                    grid_charge_kwh = row[1]
                    solar_charge_kwh = row[2]
                    battery_discharge_kwh = row[3]
                    price_ct = row[4]
                    if grid_charge_kwh + solar_charge_kwh > 0.0:
                        previous_stored_kwh = stored_energy_kwh
                        new_stored_kwh = (
                            previous_stored_kwh + grid_charge_kwh + solar_charge_kwh
                        )
                        new_grid_fraction = (
                            previous_stored_kwh * stored_grid_fraction + grid_charge_kwh
                        ) / new_stored_kwh
                        previous_grid_cost_ct = (
                            previous_stored_kwh
                            * stored_grid_fraction
                            * stored_grid_price_ct
                        )
                        added_grid_cost_ct = grid_charge_kwh * price_ct
                        new_grid_energy_kwh = (
                            previous_stored_kwh * stored_grid_fraction + grid_charge_kwh
                        )
                        if new_grid_energy_kwh > 0.0:
                            new_grid_price_ct = (
                                previous_grid_cost_ct + added_grid_cost_ct
                            ) / new_grid_energy_kwh
                        else:
                            new_grid_price_ct = 0.0
                        stored_energy_kwh = new_stored_kwh
                        stored_grid_fraction = new_grid_fraction
                        stored_grid_price_ct = new_grid_price_ct
                    avoided_cost_ct = 0.0
                    if battery_discharge_kwh > 0.0:
                        stored_grid_energy_kwh = (
                            stored_energy_kwh * stored_grid_fraction
                        )
                        discharged_grid_energy_kwh = min(
                            battery_discharge_kwh * stored_grid_fraction,
                            stored_grid_energy_kwh,
                        )
                        if price_ct > stored_grid_price_ct:
                            avoided_cost_ct = discharged_grid_energy_kwh * (
                                price_ct - stored_grid_price_ct
                            )
                        stored_energy_kwh = max(
                            0.0, stored_energy_kwh - battery_discharge_kwh
                        )
                        if stored_energy_kwh <= 0.0:
                            stored_grid_fraction = 0.0
                            stored_grid_price_ct = 0.0
                    if row_date >= today_iso:
                        today_grid_charge_kwh += grid_charge_kwh
                        today_charge_cost_ct += grid_charge_kwh * price_ct
                        today_savings_ct += avoided_cost_ct
                    if row_date >= week_start_iso:
                        week_grid_charge_kwh += grid_charge_kwh
                        week_charge_cost_ct += grid_charge_kwh * price_ct
                        week_savings_ct += avoided_cost_ct
                    if row_date >= billing_start_iso:
                        period_grid_charge_kwh += grid_charge_kwh
                        period_charge_cost_ct += grid_charge_kwh * price_ct
                        period_savings_ct += avoided_cost_ct
                    hourly_grid_charge_by_date[row_date] = (
                        hourly_grid_charge_by_date.get(row_date, 0.0) + grid_charge_kwh
                    )
                    hourly_charge_cost_by_date[row_date] = (
                        hourly_charge_cost_by_date.get(row_date, 0.0)
                        + grid_charge_kwh * price_ct
                    )
                    continue
                daily_fallback_kwh = {"today": 0.0, "week": 0.0, "period": 0.0}
                kpi_corrections = await _energy_correction_projection(billing_start_iso)
                async with _get_db() as connection:
                    daily_columns = await _table_columns(
                        connection, "stats_daily_energy"
                    )
                    daily_cost_expression = _column_expr(
                        daily_columns, "total_cost_eur", default="0.0"
                    )
                    async with connection.execute(
                        f"\n                        SELECT\n                            date,\n                            MAX(\n                                COALESCE(grid_to_battery_kwh, 0.0),\n                                COALESCE(battery_charge_grid_kwh, 0.0)\n                            ) AS grid_charge_kwh,\n                            {daily_cost_expression},\n                            COALESCE(\n                                NULLIF(grid_import_kwh, 0),\n                                NULLIF(smartmeter_import_kwh, 0),\n                                NULLIF(grid_to_house_kwh, 0),\n                                0.0\n                            ) AS import_kwh\n                        FROM stats_daily_energy\n                        WHERE date >= ?\n                        ORDER BY date\n                        ",
                        (billing_start_iso,),
                    ) as cursor:
                        daily_rows = await cursor.fetchall()
                for daily_row in daily_rows:
                    row_date = daily_row[0]
                    daily_grid_charge_kwh = float(daily_row[1] or 0.0)
                    hourly_grid_charge_kwh = hourly_grid_charge_by_date.get(
                        row_date, 0.0
                    )
                    missing_charge_kwh = daily_grid_charge_kwh - hourly_grid_charge_kwh
                    if missing_charge_kwh <= 0.01:
                        continue
                    fallback_charge_price_ct = 0.0
                    raw_import_kwh = float(daily_row[3] or 0.0)
                    raw_cost_eur = float(daily_row[2] or 0.0)
                    day_corrections = kpi_corrections.get(row_date, {})
                    corrected_import_kwh = float(
                        day_corrections.get("grid_import_day_kwh", raw_import_kwh)
                        or 0.0
                    )
                    corrected_cost_eur = corrected_day_cost(
                        raw_import_kwh, corrected_import_kwh, raw_cost_eur
                    )
                    cost_basis_import_kwh = corrected_import_kwh
                    if cost_basis_import_kwh > 0.0 and corrected_cost_eur > 0.0:
                        fallback_charge_price_ct = (
                            corrected_cost_eur * 100.0 / cost_basis_import_kwh
                        )
                    if fallback_charge_price_ct <= 0.0:
                        recorded_charge_kwh = hourly_grid_charge_by_date.get(
                            row_date, 0.0
                        )
                        if recorded_charge_kwh > 0.0:
                            fallback_charge_price_ct = (
                                hourly_charge_cost_by_date.get(row_date, 0.0)
                                / recorded_charge_kwh
                            )
                    if fallback_charge_price_ct <= 0.0:
                        fallback_charge_price_ct = (
                            current_price_ct or _get_fixed_total_price(config)
                        )
                    missing_charge_cost_ct = (
                        missing_charge_kwh * fallback_charge_price_ct
                    )
                    if row_date >= today_iso:
                        today_grid_charge_kwh += missing_charge_kwh
                        today_charge_cost_ct += missing_charge_cost_ct
                        daily_fallback_kwh["today"] += missing_charge_kwh
                    if row_date >= week_start_iso:
                        week_grid_charge_kwh += missing_charge_kwh
                        week_charge_cost_ct += missing_charge_cost_ct
                        daily_fallback_kwh["week"] += missing_charge_kwh
                    if row_date >= billing_start_iso:
                        period_grid_charge_kwh += missing_charge_kwh
                        period_charge_cost_ct += missing_charge_cost_ct
                        daily_fallback_kwh["period"] += missing_charge_kwh
                kpis["today"] = {
                    "charged_kwh": round(today_grid_charge_kwh, 2),
                    "avg_price_ct": (
                        round(today_charge_cost_ct / today_grid_charge_kwh, 2)
                        if today_grid_charge_kwh > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(today_savings_ct / 100.0, 2),
                    "daily_fallback_kwh": round(daily_fallback_kwh["today"], 2),
                }
                kpis["week"] = {
                    "charged_kwh": round(week_grid_charge_kwh, 2),
                    "avg_price_ct": (
                        round(week_charge_cost_ct / week_grid_charge_kwh, 2)
                        if week_grid_charge_kwh > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(week_savings_ct / 100.0, 2),
                    "daily_fallback_kwh": round(daily_fallback_kwh["week"], 2),
                }
                kpis["period"] = {
                    "charged_kwh": round(period_grid_charge_kwh, 2),
                    "avg_price_ct": (
                        round(period_charge_cost_ct / period_grid_charge_kwh, 2)
                        if period_grid_charge_kwh > 0.0
                        else 0.0
                    ),
                    "savings_eur": round(period_savings_ct / 100.0, 2),
                    "daily_fallback_kwh": round(daily_fallback_kwh["period"], 2),
                }
            except Exception as kpi_error:
                _LOGGER.error(
                    "Error calculating smart charging KPIs: %s",
                    kpi_error,
                    exc_info=True,
                )
            chart_history = []
            max_price_ct = None
            force_charge_price_ct = None
            if gpm_coordinator and isinstance(
                getattr(gpm_coordinator, "data", None), dict
            ):
                chart_thresholds = gpm_coordinator.data.get("thresholds") or {}
                if isinstance(chart_thresholds, dict):
                    max_price_ct = chart_thresholds.get("max_price")
                    force_charge_price_ct = chart_thresholds.get("force_charge_price")
                    live_status["cheapest_hours"] = chart_thresholds.get(
                        "cheapest_hours"
                    )
                    live_status["below_average_pct"] = chart_thresholds.get(
                        "below_average_pct"
                    )
                live_status["mode"] = gpm_coordinator.data.get("smart_charging_mode")
                live_status["is_force_price"] = bool(
                    gpm_coordinator.data.get("is_force_price")
                )
                live_status["effective_threshold_ct"] = gpm_coordinator.data.get(
                    "effective_threshold_ct"
                )
                live_status["threshold_mode"] = gpm_coordinator.data.get(
                    "threshold_mode"
                )
                from .smart_charging import live_is_cheap as live_is_cheap

                live_status["is_cheap"] = live_is_cheap(gpm_coordinator.data)
            from .smart_charging import (
                dashboard_gpm_meta as dashboard_gpm_meta,
                future_hour_is_gpm_cheap as future_hour_is_gpm_cheap,
                gpm_chart_source as gpm_chart_source,
                gpm_forecast_cheap_by_day as gpm_forecast_cheap_by_day,
                live_next_cheap_fields as live_next_cheap_fields,
                live_switch_timestamps as live_switch_timestamps,
            )

            gpm_meta = dashboard_gpm_meta(gpm_coordinator)
            chart_source = gpm_chart_source(gpm_meta)
            gpm_data = (
                gpm_coordinator.data
                if gpm_coordinator is not None
                and isinstance(getattr(gpm_coordinator, "data", None), dict)
                else None
            )
            cheap_forecasts_by_day = gpm_forecast_cheap_by_day(gpm_data)
            recommendation_threshold_ct = live_status.get("effective_threshold_ct")
            try:
                now = ha_now(context.HASS)
                chart_start = now - timedelta(hours=24)
                chart_end = now + timedelta(hours=24)
                chart_start_naive = chart_start.replace(tzinfo=None)
                chart_today = now.date()
                chart_tomorrow = chart_today + timedelta(days=1)
                billing_by_hour = {}
                async with _get_db() as connection:
                    hourly_columns = await _table_columns(
                        connection, "stats_hourly_billing"
                    )
                    hourly_price_expression = (
                        "hb.price_ct_kwh" if "price_ct_kwh" in hourly_columns else "0.0"
                    )
                    async with connection.execute(
                        f"\n                        SELECT \n                            hb.hour_key,\n                            hb.grid_to_battery_kwh,\n                            hb.solar_yield_kwh,\n                            {hourly_price_expression},\n                            hb.home_consumption_kwh\n                        FROM stats_hourly_billing hb\n                        WHERE hb.hour_key >= ?\n                        ORDER BY hb.hour_key\n                        ",
                        (chart_start_naive.strftime("%Y-%m-%dT%H:00"),),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for chart_row in rows:
                            billing_hour_key = chart_row[0][:16].replace(" ", "T")
                            billing_by_hour[billing_hour_key] = {
                                "grid_to_battery_kwh": chart_row[1],
                                "solar_yield_kwh": chart_row[2],
                                "price_ct_kwh": chart_row[3],
                                "home_consumption_kwh": chart_row[4],
                            }
                forecast_prices = []
                today_iso = today.isoformat()
                tomorrow_iso = (today + timedelta(days=1)).isoformat()
                price_range_start, price_range_end = (
                    _get_local_naive_range_for_local_dates([today_iso, tomorrow_iso])
                )
                async with _get_db() as connection:
                    async with connection.execute(
                        "\n                        SELECT \n                            timestamp,\n                            total_price\n                        FROM GPM_price_history\n                        WHERE timestamp >= ? AND timestamp < ?\n                        ORDER BY timestamp\n                        ",
                        (price_range_start, price_range_end),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for chart_row in rows:
                            price_hour_key = _gpm_local_hour_key(chart_row[0])
                            if price_hour_key:
                                forecast_prices.append(
                                    {
                                        "hour_key": price_hour_key,
                                        "total_price": chart_row[1],
                                    }
                                )
                for local_hour in iter_ha_local_hours(
                    chart_start, chart_end, context.HASS
                ):
                    naive_hour = local_hour.replace(tzinfo=None)
                    hour_key = format_local_hour_key(local_hour, context.HASS)
                    is_future = local_hour > now
                    hour_price_ct = None
                    hour_grid_charge_kwh = 0.0
                    hour_solar_yield_kwh = 0.0
                    hour_consumption_kwh = 0.0
                    if not is_future:
                        billing_hour = billing_by_hour.get(hour_key)
                        if billing_hour:
                            hour_price_ct = billing_hour["price_ct_kwh"]
                            hour_grid_charge_kwh = billing_hour["grid_to_battery_kwh"]
                            hour_solar_yield_kwh = billing_hour["solar_yield_kwh"]
                            hour_consumption_kwh = billing_hour["home_consumption_kwh"]
                    if hour_price_ct is None:
                        forecast_price = next(
                            (
                                forecast_price_entry
                                for forecast_price_entry in forecast_prices
                                if forecast_price_entry["hour_key"] == hour_key
                            ),
                            None,
                        )
                        if forecast_price:
                            hour_price_ct = forecast_price["total_price"]
                    charge_recommended = False
                    recommendation_reason = None
                    recommended_threshold_ct = None
                    recommendation_source = "unavailable"
                    if is_future and chart_source == "gpm_threshold":
                        recommendation_source = "gpm_threshold"
                        hour_date = local_hour.date()
                        if hour_date == chart_today:
                            forecast_day_key = "today"
                        elif hour_date == chart_tomorrow:
                            forecast_day_key = "tomorrow"
                        else:
                            forecast_day_key = None
                        if future_hour_is_gpm_cheap(
                            cheap_forecasts_by_day,
                            day_key=forecast_day_key,
                            hour=naive_hour.hour,
                        ):
                            charge_recommended = True
                            recommendation_reason = "gpm_cheap"
                            recommended_threshold_ct = recommendation_threshold_ct
                    chart_history.append(
                        {
                            "hour_key": hour_key,
                            "hour_key_utc": local_hour.astimezone(
                                timezone.utc
                            ).isoformat(),
                            "timezone_offset": local_hour.strftime("%z"),
                            "hour": naive_hour.hour,
                            "is_future": is_future,
                            "price_ct_kwh": (
                                round(hour_price_ct, 2)
                                if hour_price_ct is not None
                                else None
                            ),
                            "grid_to_battery_kwh": round(hour_grid_charge_kwh, 4),
                            "is_cheap": charge_recommended,
                            "grid_charge_recommended": charge_recommended,
                            "grid_charge_recommendation_reason": recommendation_reason,
                            "grid_charge_recommendation_threshold_ct": (
                                round(recommended_threshold_ct, 2)
                                if recommended_threshold_ct is not None
                                else None
                            ),
                            "grid_charge_recommendation_window": None,
                            "grid_charge_recommendation_source": recommendation_source,
                            "solar_yield_kwh": round(hour_solar_yield_kwh, 4),
                            "home_consumption_kwh": round(hour_consumption_kwh, 4),
                        }
                    )
                    continue
            except Exception as lookup_error:
                _LOGGER.error(
                    "Error building smart charging chart data: %s", lookup_error
                )
            sizing_advisor = None
            try:

                def _as_float(value: Any, fallback: float) -> float:
                    try:
                        if value is None or value == "":
                            return fallback
                        return float(value)
                    except (TypeError, ValueError):
                        return fallback

                has_battery_sensor = (
                    config.get(CONF_SENSOR_BATTERY_SOC) is not None
                    or config.get(CONF_BATTERY_SOC_SENSOR) is not None
                )
                configured_capacity_kwh = configured_battery_capacity_kwh(
                    config.get(CONF_BATTERY_CAPACITY)
                )
                battery_capacity_kwh = float(configured_capacity_kwh or 0.0)
                effective_import_price_ct = max(
                    _as_float(
                        config.get(CONF_BILLING_WORK_PRICE)
                        or config.get(CONF_BILLING_FIXED_PRICE),
                        35.0,
                    ),
                    0.0,
                )
                feed_in_tariff_ct = max(
                    _as_float(config.get(CONF_FEED_IN_TARIFF), DEFAULT_FEED_IN_TARIFF),
                    0.0,
                )
                roundtrip_efficiency = min(
                    max(
                        _as_float(config.get("battery_roundtrip_efficiency"), 0.9), 0.0
                    ),
                    1.0,
                )
                degradation_cost_ct = max(
                    _as_float(config.get("battery_degradation_ct_per_kwh"), 0.0), 0.0
                )
                gross_avoided_import_ct = (
                    effective_import_price_ct * roundtrip_efficiency
                )
                net_storage_value_ct = max(
                    0.0,
                    gross_avoided_import_ct - feed_in_tariff_ct - degradation_cost_ct,
                )
                total_days = 0
                capacity_days = 0
                potential_storage_kwh = 0.0
                export_at_capacity_kwh = 0.0
                potential_feed_in_revenue_eur = 0.0
                average_solar_yield_kwh = 0.0
                total_solar_yield_kwh = 0.0
                average_consumption_kwh = 0.0
                average_autarky_percent = 0.0
                total_solar_charge_kwh = 0.0
                total_grid_charge_kwh = 0.0
                total_battery_discharge_kwh = 0.0
                total_import_kwh = 0.0
                total_import_cost_eur = 0.0
                advisor_corrections = await _energy_correction_projection("2026-01-01")
                corrected_daily_values = await _corrected_daily_energy_values(
                    "2026-01-01"
                )
                async with _get_db() as connection:
                    daily_columns = await _table_columns(
                        connection, "stats_daily_energy"
                    )
                    daily_cost_expression = (
                        "COALESCE(SUM(total_cost_eur), 0)"
                        if "total_cost_eur" in daily_columns
                        else "0.0"
                    )
                    async with connection.execute(
                        f"\n                        SELECT \n                            COUNT(*),\n                            COALESCE(AVG(solar_yield_kwh), 0),\n                            COALESCE(SUM(solar_yield_kwh), 0),\n                            COALESCE(AVG(home_consumption_kwh), 0),\n                            COALESCE(AVG(autarkie_percent), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(solar_to_battery_kwh, 0), battery_charge_solar_kwh, 0)), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(grid_to_battery_kwh, 0), battery_charge_grid_kwh, 0)), 0),\n                            COALESCE(SUM(battery_to_house_kwh), 0),\n                            COALESCE(SUM(COALESCE(NULLIF(grid_import_kwh, 0), smartmeter_import_kwh, grid_to_house_kwh, 0)), 0),\n                            {daily_cost_expression}\n                        FROM stats_daily_energy\n                        WHERE date >= '2026-01-01'\n                        "
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row and row[0] > 0:
                            total_days = row[0]
                            average_solar_yield_kwh = row[1]
                            total_solar_yield_kwh = row[2]
                            average_consumption_kwh = row[3]
                            average_autarky_percent = row[4]
                            total_solar_charge_kwh = row[5]
                            total_grid_charge_kwh = row[6]
                            total_battery_discharge_kwh = row[7]
                            total_import_kwh = row[8]
                            total_import_cost_eur = row[9]
                            if total_import_kwh > 0 and total_import_cost_eur > 0:
                                effective_import_price_ct = (
                                    total_import_cost_eur / total_import_kwh * 100.0
                                )
                                gross_avoided_import_ct = (
                                    effective_import_price_ct * roundtrip_efficiency
                                )
                                net_storage_value_ct = max(
                                    0.0,
                                    gross_avoided_import_ct
                                    - feed_in_tariff_ct
                                    - degradation_cost_ct,
                                )
                    if advisor_corrections:
                        try:
                            provenance_cost_expression = _column_expr(
                                daily_columns, "total_cost_eur", default="0.0"
                            )
                            async with connection.execute(
                                f"\n                                SELECT date,\n                                    autarkie_percent,\n                                    COALESCE(home_consumption_kwh, 0),\n                                    COALESCE(solar_to_house_kwh, 0),\n                                    COALESCE(battery_to_house_kwh, 0),\n                                    COALESCE(grid_to_battery_kwh, 0),\n                                    {provenance_cost_expression},\n                                    COALESCE(\n                                        NULLIF(grid_import_kwh, 0),\n                                        NULLIF(smartmeter_import_kwh, 0),\n                                        NULLIF(grid_to_house_kwh, 0),\n                                        0\n                                    )\n                                FROM stats_daily_energy\n                                WHERE date >= '2026-01-01'\n                                "
                            ) as cursor:
                                provenance_rows = await cursor.fetchall()
                        except Exception as request_error:
                            _LOGGER.debug(
                                "Smart charging provenance rows unavailable: %s",
                                request_error,
                            )
                            provenance_rows = []
                        autarky_inputs = []
                        import_cost_inputs = []
                        for provenance_row in provenance_rows:
                            provenance_date = str(provenance_row[0])
                            provenance_corrections = advisor_corrections.get(
                                provenance_date, {}
                            )
                            raw_consumption_kwh = float(provenance_row[2] or 0.0)
                            raw_solar_to_house_kwh = float(provenance_row[3] or 0.0)
                            raw_battery_to_house_kwh = float(provenance_row[4] or 0.0)
                            raw_grid_to_battery_kwh = float(provenance_row[5] or 0.0)
                            raw_import_kwh = float(provenance_row[7] or 0.0)
                            autarky_inputs.append(
                                {
                                    "stored": provenance_row[1],
                                    "raw_consumption": raw_consumption_kwh,
                                    "consumption": corrected_value(
                                        advisor_corrections,
                                        provenance_date,
                                        "home_consumption_day_kwh",
                                        raw_consumption_kwh,
                                    ),
                                    "raw_solar_to_house": raw_solar_to_house_kwh,
                                    "solar_to_house": corrected_value(
                                        advisor_corrections,
                                        provenance_date,
                                        "solar_to_house_day_kwh",
                                        raw_solar_to_house_kwh,
                                    ),
                                    "raw_battery_to_house": raw_battery_to_house_kwh,
                                    "battery_to_house": corrected_value(
                                        advisor_corrections,
                                        provenance_date,
                                        "battery_to_house_day_kwh",
                                        raw_battery_to_house_kwh,
                                    ),
                                    "raw_grid_to_battery": raw_grid_to_battery_kwh,
                                    "grid_to_battery": corrected_value(
                                        advisor_corrections,
                                        provenance_date,
                                        "grid_to_battery_day_kwh",
                                        raw_grid_to_battery_kwh,
                                    ),
                                }
                            )
                            import_cost_inputs.append(
                                {
                                    "raw_import": raw_import_kwh,
                                    "corrected_import": float(
                                        provenance_corrections.get(
                                            "grid_import_day_kwh", raw_import_kwh
                                        )
                                        or 0.0
                                    ),
                                    "raw_cost": float(provenance_row[6] or 0.0),
                                }
                            )
                            continue
                        corrected_autarky_percent = window_autarky_average(
                            autarky_inputs
                        )
                        if corrected_autarky_percent is not None:
                            average_autarky_percent = corrected_autarky_percent
                        corrected_import_totals = projected_import_cost_totals(
                            import_cost_inputs
                        )
                        if corrected_import_totals is not None:
                            total_import_kwh, total_import_cost_eur = (
                                corrected_import_totals
                            )
                            if total_import_kwh > 0 and total_import_cost_eur > 0:
                                effective_import_price_ct = (
                                    total_import_cost_eur / total_import_kwh * 100.0
                                )
                                gross_avoided_import_ct = (
                                    effective_import_price_ct * roundtrip_efficiency
                                )
                                net_storage_value_ct = max(
                                    0.0,
                                    gross_avoided_import_ct
                                    - feed_in_tariff_ct
                                    - degradation_cost_ct,
                                )
                    if corrected_daily_values:
                        total_solar_yield_kwh = sum(
                            (
                                corrected_day["solar_yield_day_kwh"]
                                for corrected_day in corrected_daily_values.values()
                            )
                        )
                        average_solar_yield_kwh = (
                            total_solar_yield_kwh / total_days
                            if total_days > 0
                            else 0.0
                        )
                    if total_days > 0 and has_battery_sensor:
                        async with connection.execute(
                            "\n                            SELECT date,\n                                COALESCE(NULLIF(solar_to_battery_kwh, 0), battery_charge_solar_kwh, 0)\n                                    + COALESCE(NULLIF(grid_to_battery_kwh, 0), battery_charge_grid_kwh, 0) as total_charge,\n                                COALESCE(NULLIF(grid_export_kwh, 0), smartmeter_export_kwh, 0) as export,\n                                COALESCE(NULLIF(grid_import_kwh, 0), smartmeter_import_kwh, grid_to_house_kwh, 0) as import_val,\n                                COALESCE(solar_yield_kwh, 0) as solar\n                            FROM stats_daily_energy\n                            WHERE date >= '2026-01-01'\n                            "
                        ) as cursor:
                            rows = await cursor.fetchall()
                            for chart_row in rows:
                                daily_total_charge_kwh = max(chart_row[1] or 0.0, 0.0)
                                daily_export_kwh = max(
                                    corrected_value(
                                        advisor_corrections,
                                        chart_row[0],
                                        "grid_export_day_kwh",
                                        chart_row[2],
                                    ),
                                    0.0,
                                )
                                daily_import_kwh = max(
                                    corrected_value(
                                        advisor_corrections,
                                        chart_row[0],
                                        "grid_import_day_kwh",
                                        chart_row[3],
                                    ),
                                    0.0,
                                )
                                hour_solar_yield_kwh = max(
                                    corrected_value(
                                        advisor_corrections,
                                        chart_row[0],
                                        "solar_yield_day_kwh",
                                        chart_row[4],
                                    ),
                                    0.0,
                                )
                                at_capacity = battery_capacity_kwh > 0 and (
                                    daily_total_charge_kwh >= 0.8 * battery_capacity_kwh
                                    or (
                                        daily_export_kwh > 1.0
                                        and hour_solar_yield_kwh > 4.0
                                    )
                                )
                                if at_capacity:
                                    capacity_days += 1
                                    export_at_capacity_kwh += daily_export_kwh
                                    storable_export_kwh = min(
                                        daily_export_kwh, daily_import_kwh
                                    )
                                    potential_storage_kwh += storable_export_kwh
                                    potential_feed_in_revenue_eur += (
                                        storable_export_kwh
                                        * (feed_in_tariff_ct / 100.0)
                                    )
                total_charge_kwh = total_solar_charge_kwh + total_grid_charge_kwh
                battery_cycles = (
                    total_charge_kwh / battery_capacity_kwh
                    if battery_capacity_kwh > 0
                    else 0.0
                )
                capacity_days_percent = (
                    capacity_days / total_days * 100.0 if total_days > 0 else 0.0
                )
                potential_savings_eur = potential_storage_kwh * (
                    net_storage_value_ct / 100.0
                )
                sizing_advisor = {
                    "has_battery": has_battery_sensor,
                    "total_days": total_days,
                    "full_days": capacity_days,
                    "full_days_percent": capacity_days_percent,
                    "potential_kwh": potential_storage_kwh,
                    "potential_savings_eur": potential_savings_eur,
                    "export_at_capacity_kwh": export_at_capacity_kwh,
                    "feed_in_tariff_ct": feed_in_tariff_ct,
                    "effective_import_price_ct_kwh": effective_import_price_ct,
                    "gross_avoided_import_ct_kwh": gross_avoided_import_ct,
                    "battery_roundtrip_efficiency": roundtrip_efficiency,
                    "battery_degradation_ct_kwh": degradation_cost_ct,
                    "net_value_ct_kwh": net_storage_value_ct,
                    "potential_feed_in_revenue_eur": potential_feed_in_revenue_eur,
                    "total_solar_yield_kwh": total_solar_yield_kwh,
                    "avg_solar_yield_kwh": average_solar_yield_kwh,
                    "avg_house_consumption_kwh": average_consumption_kwh,
                    "avg_autarky_percent": average_autarky_percent,
                    "battery_charge_solar_kwh": total_solar_charge_kwh,
                    "battery_charge_grid_kwh": total_grid_charge_kwh,
                    "battery_discharge_house_kwh": total_battery_discharge_kwh,
                    "battery_cycles": battery_cycles,
                    "battery_capacity": configured_capacity_kwh,
                }
            except Exception as lookup_error:
                _LOGGER.error(
                    "Error calculating smart charging sizing advisor: %s", lookup_error
                )
            charging_switch_entity_id = config.get(CONF_SMART_CHARGING_SWITCH)
            switch_state = None
            switch_last_changed = None
            if charging_switch_entity_id and str(charging_switch_entity_id).strip():
                charging_switch = context.HASS.states.get(
                    str(charging_switch_entity_id).strip()
                )
                if charging_switch is not None:
                    switch_state = getattr(charging_switch, "state", None)
                    switch_last_changed = getattr(charging_switch, "last_changed", None)
            live_status["charging_since"], live_status["waiting_since"] = (
                live_switch_timestamps(
                    switch_state, switch_last_changed, smart_charging_enabled
                )
            )
            next_cheap_hour, next_cheap_price_ct = live_next_cheap_fields(
                gpm_data,
                gpm_available=bool(gpm_meta.get("available")),
                is_demo=bool(gpm_meta.get("is_demo")),
                today=today.isoformat(),
            )
            live_status["next_cheap_hour"] = next_cheap_hour
            live_status["next_cheap_price_ct"] = next_cheap_price_ct
            from ..core.charging_status import (
                grid_release_view as grid_release_view,
                status_facts as status_facts,
            )

            status_now = ha_now(context.HASS)
            grid_release = grid_release_view(
                gpm_data,
                status_now,
                gpm_coordinator,
                is_demo=bool(gpm_meta.get("is_demo")),
            )
            live_status["grid_allowed"] = grid_release["allowed"]
            live_status["grid_kind"] = grid_release["kind"]
            live_status["under_force"] = grid_release["under_force"]
            live_status["status"] = status_facts(
                active=bool(live_status.get("active")),
                reason=str(live_status.get("reason") or ""),
                grid_kind=str(grid_release["kind"]),
                mode=live_status.get("mode"),
                target_soc=live_status.get("target_soc"),
                max_soc=live_status.get("max_soc"),
                min_soc=live_status.get("min_soc"),
                next_cheap_hour=next_cheap_hour,
                under_force=bool(grid_release["under_force"]),
                now_hour=status_now.hour,
            )
            smart_charging_status = {
                "is_cheap": live_status.get("is_cheap"),
                "is_force_price": bool(live_status.get("is_force_price")),
                "decision": live_status.get("decision"),
                "reason": live_status.get("reason"),
                "enabled": bool(live_status.get("enabled")),
                "active": bool(live_status.get("active")),
                "reserved_future_grid_charge_kwh": live_status.get(
                    "reserved_future_grid_charge_kwh", 0.0
                ),
                "gpm_available": bool(gpm_meta.get("available")),
                "is_demo": bool(gpm_meta.get("is_demo")),
                "grid_allowed": live_status.get("grid_allowed"),
                "grid_kind": live_status.get("grid_kind"),
                "status": live_status.get("status"),
            }
            from ..core.grid_import_split import (
                battery_is_configured as battery_is_configured,
            )

            grid_charge_cards = None
            if battery_is_configured(config):
                try:
                    from ..core.grid_import_split import (
                        battery_card as battery_card,
                        charge_window_bounds as charge_window_bounds,
                        load_split_hours as load_split_hours,
                        summarize_import_split as summarize_import_split,
                    )

                    charge_window_today = ha_today(context.HASS)
                    charge_windows = charge_window_bounds(charge_window_today)
                    year_start_iso, year_end_iso = charge_windows["year"]
                    charge_card_corrections = await _energy_correction_projection(
                        year_start_iso
                    )
                    charge_card_daily_values = await _corrected_daily_energy_values(
                        year_start_iso, year_end_iso
                    )
                    try:
                        charge_card_fallback_price_ct = float(
                            _get_fixed_total_price(config)
                        )
                    except (TypeError, ValueError):
                        charge_card_fallback_price_ct = 0.0
                    async with _get_db() as connection:
                        split_hours = await load_split_hours(
                            connection, year_start_iso, year_end_iso
                        )
                    grid_charge_cards = {}
                    for window_name, (
                        window_start_iso,
                        window_end_iso,
                    ) in charge_windows.items():
                        window_hours = [
                            split_hour
                            for split_hour in split_hours.hours
                            if window_start_iso
                            <= str(split_hour["date"])
                            <= window_end_iso
                        ]
                        grid_charge_cards[window_name] = battery_card(
                            summarize_import_split(
                                window_hours,
                                charge_card_daily_values,
                                charge_card_corrections,
                                fallback_price_ct=charge_card_fallback_price_ct,
                                house_column=split_hours.house_column,
                                battery_column=split_hours.battery_column,
                                backfill_done=split_hours.backfill_done,
                            )
                        )
                except Exception as request_error:
                    _LOGGER.debug("Grid charge card unavailable: %s", request_error)
                    grid_charge_cards = None
            return web.json_response(
                {
                    "success": True,
                    "live": live_status,
                    "gpm": gpm_meta,
                    "smart_charging_status": smart_charging_status,
                    "kpis": kpis,
                    "history": chart_history,
                    "chart_plan": {
                        "enabled": bool(smart_charging_enabled and battery_configured),
                        "source": chart_source,
                        "recommendation_threshold": (
                            round(recommendation_threshold_ct, 2)
                            if recommendation_threshold_ct is not None
                            else None
                        ),
                        "max_price_ct": (
                            round(max_price_ct, 2) if max_price_ct is not None else None
                        ),
                        "force_charge_price_ct": (
                            round(force_charge_price_ct, 2)
                            if force_charge_price_ct is not None
                            else None
                        ),
                    },
                    "advisor": sizing_advisor,
                    "grid_charge": grid_charge_cards,
                }
            )
        except Exception as request_error:
            _LOGGER.error(
                "Error in SmartChargingDashboardView: %s", request_error, exc_info=True
            )
            return web.json_response({"error": str(request_error)}, status=500)
