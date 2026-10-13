# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Billing, GPM price, and monthly-tariff views."""
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

if TYPE_CHECKING:
    from aiohttp.web import Request, Response
_LOGGER = logging.getLogger(__name__)


class BillingDataView(HomeAssistantView):
    """API for billing and annual balance data. @zara"""

    url = "/api/sfml_stats/billing"
    name = "api:sfml_stats:billing"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return billing configuration and annual balance data. @zara"""
        if context.HASS is None:
            return web.json_response(
                _stable_api_error(
                    "home_assistant_not_initialized",
                    "Home Assistant not initialized",
                    status="unavailable",
                ),
                status=503,
            )
        billing_calculator = None
        domain_runtime = context.HASS.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if isinstance(runtime_data, dict):
                if "billing_calculator" in runtime_data:
                    billing_calculator = runtime_data["billing_calculator"]
                    break
        if billing_calculator is None:
            return web.json_response(
                _stable_api_error(
                    "billing_calculator_not_initialized",
                    "BillingCalculator not initialized",
                    status="unavailable",
                ),
                status=503,
            )
        try:
            billing_result = await billing_calculator.async_calculate_billing()
        except Exception as error:
            _LOGGER.error("Error in billing calculation: %s", error)
            return web.json_response(
                _stable_api_error(
                    "billing_calculation_failed",
                    "Internal server error",
                    status="error",
                ),
                status=500,
            )
        return web.json_response(billing_result)


class GPMPricesView(HomeAssistantView):
    """API for current electricity price data — DB-first. @zara"""

    url = "/api/sfml_stats/gpm_prices"
    name = "api:sfml_stats:gpm_prices"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return current electricity price. Dynamic: from DB. Fixed: from config. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            config = {}
            gpm_coordinator = None
            for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(runtime_data, dict):
                    if "config" in runtime_data:
                        config = runtime_data["config"]
                    if "gpm_coordinator" in runtime_data:
                        gpm_coordinator = runtime_data["gpm_coordinator"]
            price_mode = effective_price_mode(context.HASS, config)
            if price_mode == MODE_NONE:
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "time_context": _time_context(),
                        "price_mode": price_mode,
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
            if price_mode == MODE_LEGACY_FIXED:
                fixed_price_ct = _get_fixed_total_price(config)
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "time_context": _time_context(),
                        "price_mode": price_mode,
                        "hours_without_price": 0 if fixed_price_ct else 1,
                        "total_price": fixed_price_ct,
                        "is_cheap": True,
                        "price_trend": "stable",
                        "diagnostics": {
                            "price_source": "configured_fixed_tariff",
                            "gpm_read_skipped": True,
                        },
                    }
                )
            now = ha_now(context.HASS)
            next_hour = (now.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(
                ha_timezone(context.HASS)
            )
            current_hour_key = format_local_hour_key(now, context.HASS)
            next_hour_key = format_local_hour_key(next_hour, context.HASS)
            total_price_ct = None
            next_hour_price_ct = None
            spot_price_ct = None
            average_price_ct = None
            cheapest_hour = None
            most_expensive_hour = None
            price_source = "unavailable"
            try:
                today_str = ha_today(context.HASS).isoformat()
                next_date_str = next_hour.date().isoformat()
                range_start, range_end = _get_broad_timestamp_range_for_local_dates(
                    [today_str, next_date_str]
                )
                async with _get_db() as connection:
                    async with connection.execute(
                        "SELECT total_price, spot_price_net, price_next_hour,\n                                  average_today, cheapest_today, most_expensive_today\n                           FROM GPM_current_price WHERE id = 1"
                    ) as cursor:
                        price_row = await cursor.fetchone()
                    if price_row and price_row[0] is not None:
                        total_price_ct = price_row[0]
                        price_source = "GPM_current_price"
                        spot_price_ct = price_row[1]
                        next_hour_price_ct = price_row[2]
                        average_price_ct = price_row[3]
                        cheapest_hour = price_row[4]
                        most_expensive_hour = price_row[5]
                    async with connection.execute(
                        "SELECT timestamp, total_price, price_net FROM GPM_price_history\n                           WHERE timestamp >= ? AND timestamp < ?\n                           ORDER BY timestamp",
                        (range_start, range_end),
                    ) as cursor:
                        price_rows = await cursor.fetchall()
                    today_price_rows = []
                    prices_by_hour = {}
                    for history_row in price_rows:
                        local_hour_key = _gpm_local_hour_key(history_row[0])
                        if not local_hour_key:
                            continue
                        prices_by_hour[local_hour_key] = history_row
                        if local_hour_key[:10] == today_str:
                            today_price_rows.append((local_hour_key, history_row))
                    if total_price_ct is None or spot_price_ct is None:
                        price_row = prices_by_hour.get(current_hour_key)
                        if price_row:
                            total_price_ct = price_row[1]
                            spot_price_ct = price_row[2]
                            price_source = "GPM_price_history"
                    if next_hour_price_ct is None:
                        price_row = prices_by_hour.get(next_hour_key)
                        if price_row:
                            next_hour_price_ct = price_row[1]
                    if average_price_ct is None:
                        if today_price_rows:
                            present_prices = [
                                price_row[1]
                                for hour_key, price_row in today_price_rows
                                if price_row[1] is not None
                            ]
                            if present_prices:
                                average_price_ct = round(
                                    sum(present_prices) / len(present_prices), 2
                                )
                    if cheapest_hour is None and today_price_rows:
                        cheapest_row = min(
                            (
                                price_entry
                                for price_entry in today_price_rows
                                if price_entry[1][1] is not None
                            ),
                            key=lambda item: item[1][1],
                            default=None,
                        )
                        if cheapest_row:
                            cheapest_hour = int(cheapest_row[0][11:13])
                    if most_expensive_hour is None and today_price_rows:
                        most_expensive_row = max(
                            (
                                price_entry
                                for price_entry in today_price_rows
                                if price_entry[1][1] is not None
                            ),
                            key=lambda item: item[1][1],
                            default=None,
                        )
                        if most_expensive_row:
                            most_expensive_hour = int(most_expensive_row[0][11:13])
            except Exception as db_error:
                _LOGGER.debug("Error reading prices from DB: %s", db_error)
            price_trend = "unknown"
            if total_price_ct is not None and next_hour_price_ct is not None:
                price_delta = next_hour_price_ct - total_price_ct
                if price_delta > 1:
                    price_trend = "rising"
                elif price_delta < -1:
                    price_trend = "falling"
                else:
                    price_trend = "stable"
            smart_charging_status = {
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
            if gpm_coordinator and gpm_coordinator.data:
                smart_charging_status = {
                    "active": gpm_coordinator.data.get("smart_charging_active", False),
                    "target_soc": gpm_coordinator.data.get("smart_charging_target_soc"),
                    "current_soc": gpm_coordinator.data.get(
                        "smart_charging_current_soc"
                    ),
                    "reason": gpm_coordinator.data.get("smart_charging_reason", ""),
                    "decision": gpm_coordinator.data.get(
                        "smart_charging_decision", "not_load"
                    ),
                    "requested_grid_charge_kwh": gpm_coordinator.data.get(
                        "smart_charging_requested_grid_charge_kwh", 0.0
                    ),
                    "effective_storage_cost_ct_kwh": gpm_coordinator.data.get(
                        "smart_charging_effective_storage_cost_ct_kwh"
                    ),
                    "compared_future_price_ct_kwh": gpm_coordinator.data.get(
                        "smart_charging_compared_future_price_ct_kwh"
                    ),
                    "effective_roundtrip_efficiency": gpm_coordinator.data.get(
                        "smart_charging_effective_roundtrip_efficiency"
                    ),
                    "reserved_future_grid_charge_kwh": gpm_coordinator.data.get(
                        "smart_charging_reserved_future_grid_charge_kwh", 0.0
                    ),
                }
            is_cheap = False
            if gpm_coordinator and isinstance(
                getattr(gpm_coordinator, "data", None), dict
            ):
                is_cheap = bool(gpm_coordinator.data.get("is_cheap"))
            price_hours = []
            try:
                today_str = ha_today(context.HASS).isoformat()
                tomorrow_str = (ha_today(context.HASS) + timedelta(days=1)).isoformat()
                range_start, range_end = _get_broad_timestamp_range_for_local_dates(
                    [today_str, tomorrow_str]
                )
                async with _get_db() as connection:
                    async with connection.execute(
                        "SELECT timestamp, total_price, price_net\n                           FROM GPM_price_history\n                           WHERE timestamp >= ? AND timestamp < ?\n                           ORDER BY timestamp",
                        (range_start, range_end),
                    ) as cursor:
                        price_rows = await cursor.fetchall()
                    for price_row in price_rows:
                        local_hour_key = _gpm_local_hour_key(price_row[0])
                        if not local_hour_key:
                            continue
                        if local_hour_key[:10] not in (today_str, tomorrow_str):
                            continue
                        price_hours.append(
                            {
                                "hour": int(local_hour_key[11:13]),
                                "total_price": price_row[1],
                                "spot_price": price_row[2],
                                "is_tomorrow": local_hour_key[:10] == tomorrow_str,
                            }
                        )
            except Exception:
                pass
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "price_mode": price_mode,
                    "hours_without_price": 0 if total_price_ct is not None else 1,
                    "spot_price": spot_price_ct,
                    "total_price": total_price_ct,
                    "total_price_next_hour": next_hour_price_ct,
                    "cheapest_hour_today": cheapest_hour,
                    "most_expensive_hour_today": most_expensive_hour,
                    "average_price_today": average_price_ct,
                    "is_cheap": is_cheap,
                    "price_trend": price_trend,
                    "diagnostics": {
                        "price_source": price_source,
                        "gpm_read_skipped": False,
                    },
                    "smart_charging": smart_charging_status,
                    "price_hours": price_hours,
                }
            )
        except Exception as error:
            _LOGGER.error("Error fetching GPM price data: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class MonthlyTariffsView(HomeAssistantView):
    """API for monthly tariffs management (EEG/Energy Sharing support). @zara"""

    url = "/api/sfml_stats/monthly_tariffs"
    name = "api:sfml_stats:monthly_tariffs"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get monthly tariffs data. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = None
            domain_runtime = context.HASS.data.get(DOMAIN, {})
            for entry_id, runtime_data in domain_runtime.items():
                if isinstance(runtime_data, dict):
                    if "monthly_tariff_manager" in runtime_data:
                        tariff_manager = runtime_data["monthly_tariff_manager"]
                        break
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            year_number = max(
                2020,
                min(
                    int(request.query.get("year", ha_today(context.HASS).year)),
                    ha_today(context.HASS).year + 1,
                ),
            )
            year_summary = await tariff_manager.get_year_summary(year_number)
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    **year_summary,
                }
            )
        except Exception as error:
            _LOGGER.error("Error fetching monthly tariffs: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class MonthlyTariffDetailView(HomeAssistantView):
    """API for single month tariff details. @zara"""

    url = "/api/sfml_stats/monthly_tariffs/{year}/{month}"
    name = "api:sfml_stats:monthly_tariff_detail"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request, year: str, month: str) -> web.Response:
        """Get detailed data for a specific month. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            monthly_data = await tariff_manager.get_monthly_data(int(year), int(month))
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "data": monthly_data,
                }
            )
        except Exception as error:
            _LOGGER.error("Error fetching month detail: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def post(self, request: web.Request, year: str, month: str) -> web.Response:
        """Update overrides for a specific month. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            payload = await request.json()
            overrides = payload.get("overrides", {})
            saved = await tariff_manager.set_monthly_override(
                int(year), int(month), overrides
            )
            if saved:
                monthly_data = await tariff_manager.get_monthly_data(
                    int(year), int(month)
                )
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "data": monthly_data,
                    }
                )
            return web.json_response(
                {"success": False, "error": "Failed to save overrides"}, status=500
            )
        except Exception as error:
            _LOGGER.error("Error updating month overrides: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    def _get_tariff_manager(self):
        """Get tariff manager from HASS data. @zara"""
        if context.HASS is None:
            return
        domain_runtime = context.HASS.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if not isinstance(runtime_data, dict):
                continue
            if not "monthly_tariff_manager" in runtime_data:
                continue
            return runtime_data["monthly_tariff_manager"]


def _get_stats_config() -> dict[str, Any]:
    if context.HASS is None:
        return {}
    for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
        if not isinstance(runtime_data, dict):
            continue
        if not "config" in runtime_data:
            continue
        return runtime_data["config"] or {}
    return {}


def _get_monthly_tariff_manager():
    if context.HASS is None:
        return
    for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
        if not isinstance(runtime_data, dict):
            continue
        if not "monthly_tariff_manager" in runtime_data:
            continue
        return runtime_data["monthly_tariff_manager"]


async def _read_json_object(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise ValueError("Bitte geben Sie gültige Daten ein.") from None
    if not isinstance(payload, dict):
        raise ValueError("Bitte geben Sie gültige Daten ein.")
    return payload


def _parse_energy_month(year: str, month: str) -> tuple[int, int]:
    year_number = int(year)
    month_number = int(month)
    if year_number < 2020 or year_number > ha_today(context.HASS).year + 1:
        raise ValueError("Invalid year")
    if month_number < 1 or month_number > 12:
        raise ValueError("Invalid month")
    return (year_number, month_number)


def _parse_import_price_ct(value: Any) -> float:
    try:
        price_ct = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Bitte geben Sie einen gültigen Preis ein.") from None
    if price_ct <= 0 or price_ct > 200:
        raise ValueError("Der Preis muss größer als 0 und höchstens 200 ct/kWh sein.")
    return price_ct


async def _build_energy_monthly_price_payload(
    year: int, month: int, *, new_price_ct: float | None = None
) -> dict[str, Any]:
    config = _get_stats_config()
    tariff_manager = _get_monthly_tariff_manager()
    if tariff_manager is None:
        raise RuntimeError("MonthlyTariffManager not initialized")
    month_start = date(year, month, 1)
    month_end_exclusive = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    )
    calendar_days = calendar.monthrange(year, month)[1]
    async with _get_db() as connection:
        async with connection.execute(
            "SELECT SUM(home_consumption_kwh) as consumption,\n                      SUM(solar_yield_kwh) as solar,\n                      SUM(self_consumption_kwh) as self_cons,\n                      SUM(solar_to_house_kwh) as solar_to_house,\n                      SUM(battery_to_house_kwh) as bat_to_house,\n                      SUM(grid_to_house_kwh) as grid_to_house,\n                      SUM(smartmeter_import_kwh) as sm_import,\n                      SUM(grid_export_kwh) as grid_export,\n                      SUM(smartmeter_export_kwh) as sm_export,\n                      SUM(home_consumption_kwh) as total_consumption_for_autarkie,\n                      COUNT(*) as days,\n                      SUM(grid_to_battery_kwh) as grid_to_battery\n               FROM stats_daily_energy\n               WHERE date >= ? AND date < ?",
            (month_start.isoformat(), month_end_exclusive.isoformat()),
        ) as cursor:
            energy_row = await cursor.fetchone()
    if not energy_row or not energy_row[10]:
        days_with_data = min(energy_row[10] or 0, calendar_days) if energy_row else 0
    else:
        days_with_data = min(energy_row[10] or 0, calendar_days)
    try:
        default_import_price_ct = float(_get_fixed_total_price(config))
    except (TypeError, ValueError):
        default_import_price_ct = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    try:
        default_feed_in_tariff_ct = float(
            config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
        )
    except (TypeError, ValueError):
        default_feed_in_tariff_ct = DEFAULT_FEED_IN_TARIFF
    try:
        default_base_fee_eur = float(config.get(CONF_BILLING_BASE_FEE, 0) or 0)
    except (TypeError, ValueError):
        default_base_fee_eur = 0.0
    default_feed_in_tariff_ct = max(0.0, default_feed_in_tariff_ct)
    monthly_fallbacks = await tariff_manager.get_monthly_fallbacks(
        year,
        month,
        import_price_ct=default_import_price_ct,
        feed_in_tariff_ct=default_feed_in_tariff_ct,
        base_fee_eur=default_base_fee_eur,
    )
    import_price_fallback = monthly_fallbacks["import_price_ct"]
    feed_in_tariff_fallback = monthly_fallbacks["feed_in_tariff_ct"]
    base_fee_fallback = monthly_fallbacks["base_fee_eur"]
    current_price_ct = float(
        import_price_fallback["value"]
        if import_price_fallback["value"] is not None
        else default_import_price_ct
    )
    feed_in_tariff_ct = float(
        feed_in_tariff_fallback["value"] or default_feed_in_tariff_ct
    )
    monthly_base_fee_eur = float(base_fee_fallback["value"] or default_base_fee_eur)
    utility_import_kwh = 0.0
    utility_export_kwh = 0.0
    own_supply_kwh = 0.0
    consumption_kwh = 0.0
    solar_kwh = 0.0
    if energy_row:
        grid_to_battery_kwh = float(energy_row[11] or 0.0)
        utility_import_kwh = (
            round(float(energy_row[6] or 0.0), 1)
            if (energy_row[6] or 0.0) > 0
            else round(float(energy_row[5] or 0.0) + grid_to_battery_kwh, 1)
        )
        utility_export_kwh = (
            round(float(energy_row[8] or 0.0), 1)
            if (energy_row[8] or 0.0) > 0
            else round(float(energy_row[7] or 0.0), 1)
        )
        battery_own_supply_kwh = max(
            0.0, float(energy_row[4] or 0.0) - grid_to_battery_kwh
        )
        own_supply_kwh = float(energy_row[3] or 0.0) + battery_own_supply_kwh
        consumption_kwh = float(energy_row[0] or 0.0)
        solar_kwh = float(energy_row[1] or 0.0)
    corrected_daily_values = await _corrected_daily_energy_values(
        month_start.isoformat(), (month_end_exclusive - timedelta(days=1)).isoformat()
    )
    if corrected_daily_values:
        solar_kwh = sum(
            (
                daily_values["solar_yield_day_kwh"]
                for daily_values in corrected_daily_values.values()
            )
        )
        utility_import_kwh = sum(
            (
                daily_values["grid_import_day_kwh"]
                for daily_values in corrected_daily_values.values()
            )
        )
        utility_export_kwh = sum(
            (
                daily_values["grid_export_day_kwh"]
                for daily_values in corrected_daily_values.values()
            )
        )
    allocated_base_fee_eur = (
        monthly_base_fee_eur * days_with_data / calendar_days
        if calendar_days > 0
        else monthly_base_fee_eur
    )

    def calculate(price_ct: float) -> dict[str, Any]:
        energy_cost_eur = utility_import_kwh * price_ct / 100
        feed_in_revenue_eur = utility_export_kwh * feed_in_tariff_ct / 100
        self_savings_eur = own_supply_kwh * price_ct / 100
        return {
            "price_ct": price_ct,
            "cost_eur": round(energy_cost_eur + allocated_base_fee_eur, 2),
            "energy_cost_eur": round(energy_cost_eur, 2),
            "base_fee_eur": round(allocated_base_fee_eur, 2),
            "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
            "self_consumption_savings_eur": round(self_savings_eur, 2),
            "total_savings_eur": round(self_savings_eur + feed_in_revenue_eur, 2),
        }

    preview_price_ct = current_price_ct if new_price_ct is None else new_price_ct
    tariff_source = import_price_fallback.get("source", "fixed_month")
    return {
        "success": True,
        "timestamp": ha_now(context.HASS).isoformat(),
        "time_context": _time_context(),
        "month": f"{year}-{month:02d}",
        "year": year,
        "month_number": month,
        "price_mode": effective_price_mode(context.HASS, config),
        "current_price_ct": current_price_ct,
        "new_price_ct": new_price_ct,
        "has_manual_price": tariff_source == "manual",
        "can_reset": tariff_source == "manual",
        "tariff_source": tariff_source,
        "cost_source": "fixed",
        "grid_import_kwh": round(utility_import_kwh, 4),
        "grid_export_kwh": round(utility_export_kwh, 4),
        "consumption_kwh": round(consumption_kwh, 4),
        "solar_kwh": round(solar_kwh, 4),
        "days": days_with_data,
        "calendar_days": calendar_days,
        "before": calculate(current_price_ct),
        "after": calculate(preview_price_ct),
    }


class EnergyMonthlyPriceCorrectionView(HomeAssistantView):
    """Safe customer-facing fixed-price correction API. @zara"""

    url = "/api/sfml_stats/energy/monthly_price/{year}/{month}"
    name = "api:sfml_stats:energy_monthly_price"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request, year: str, month: str) -> web.Response:
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"},
                    status=500,
                )
            year_number, month_number = _parse_energy_month(year, month)
            return web.json_response(
                await _build_energy_monthly_price_payload(year_number, month_number)
            )
        except PermissionError as error:
            return web.json_response(
                {"success": False, "error": str(error)}, status=409
            )
        except ValueError as error:
            return web.json_response(
                {"success": False, "error": str(error)}, status=400
            )
        except Exception as error:
            _LOGGER.error(
                "Error fetching monthly energy price correction: %s",
                error,
                exc_info=True,
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def post(self, request: web.Request, year: str, month: str) -> web.Response:
        return web.json_response(
            {"success": False, "error": "moved_to_gpm"}, status=410
        )

    @local_only
    async def delete(self, request: web.Request, year: str, month: str) -> web.Response:
        return web.json_response(
            {"success": False, "error": "moved_to_gpm"}, status=410
        )


class EnergyMonthlyPricePreviewView(HomeAssistantView):
    """Preview fixed-price month corrections without saving. @zara"""

    url = "/api/sfml_stats/energy/monthly_price/{year}/{month}/preview"
    name = "api:sfml_stats:energy_monthly_price_preview"
    requires_auth = False

    @local_only
    async def post(self, request: web.Request, year: str, month: str) -> web.Response:
        return web.json_response(
            {"success": False, "error": "moved_to_gpm"}, status=410
        )


class MonthlyTariffFinalizeView(HomeAssistantView):
    """API to finalize a month (mark as billed). @zara"""

    url = "/api/sfml_stats/monthly_tariffs/{year}/{month}/finalize"
    name = "api:sfml_stats:monthly_tariff_finalize"
    requires_auth = False

    @local_only
    async def post(self, request: web.Request, year: str, month: str) -> web.Response:
        """Finalize a month and optionally recalculate history. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            payload = await request.json() if request.body_exists else {}
            recalculate_history = payload.get("recalculate_history", True)
            finalization_result = await tariff_manager.finalize_month(
                int(year), int(month), recalculate_history=recalculate_history
            )
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    **finalization_result,
                }
            )
        except Exception as error:
            _LOGGER.error("Error finalizing month: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def delete(self, request: web.Request, year: str, month: str) -> web.Response:
        """Remove finalization from a month. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            saved = await tariff_manager.unfinalize_month(int(year), int(month))
            return web.json_response(
                {"success": saved, "timestamp": ha_now(context.HASS).isoformat()}
            )
        except Exception as error:
            _LOGGER.error("Error unfinalizing month: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    def _get_tariff_manager(self):
        """Get tariff manager from HASS data. @zara"""
        if context.HASS is None:
            return
        domain_runtime = context.HASS.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if not isinstance(runtime_data, dict):
                continue
            if not "monthly_tariff_manager" in runtime_data:
                continue
            return runtime_data["monthly_tariff_manager"]


class MonthlyTariffsExportView(HomeAssistantView):
    """API to export monthly tariffs as CSV. @zara"""

    url = "/api/sfml_stats/monthly_tariffs/export"
    name = "api:sfml_stats:monthly_tariffs_export"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Export monthly tariffs as CSV. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            today = ha_today(context.HASS)
            start_month = request.query.get("start", f"{today.year}-01")
            end_month = request.query.get("end", f"{today.year}-{today.month:02d}")
            start_year, start_month_number = map(int, start_month.split("-"))
            end_year, end_month_number = map(int, end_month.split("-"))
            csv_text = await tariff_manager.export_csv(
                start_year, start_month_number, end_year, end_month_number
            )
            filename = f"monthly_tariffs_{start_month}_{end_month}.csv"
            return web.Response(
                text=csv_text,
                content_type="text/csv",
                charset="utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as error:
            _LOGGER.error("Error exporting monthly tariffs: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    def _get_tariff_manager(self):
        """Get tariff manager from HASS data. @zara"""
        if context.HASS is None:
            return
        domain_runtime = context.HASS.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if not isinstance(runtime_data, dict):
                continue
            if not "monthly_tariff_manager" in runtime_data:
                continue
            return runtime_data["monthly_tariff_manager"]


class MonthlyTariffsDefaultsView(HomeAssistantView):
    """API to manage default tariff settings. @zara"""

    url = "/api/sfml_stats/monthly_tariffs/defaults"
    name = "api:sfml_stats:monthly_tariffs_defaults"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Get current default tariff settings. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            defaults = tariff_manager._get_defaults()
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "defaults": defaults,
                }
            )
        except Exception as error:
            _LOGGER.error("Error fetching defaults: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        """Update default tariff settings. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "Home Assistant not initialized"}
                )
            tariff_manager = self._get_tariff_manager()
            if tariff_manager is None:
                return web.json_response(
                    {"success": False, "error": "MonthlyTariffManager not initialized"}
                )
            payload = await request.json()
            saved = await tariff_manager.update_defaults(payload)
            if saved:
                defaults = tariff_manager._get_defaults()
                return web.json_response(
                    {
                        "success": True,
                        "timestamp": ha_now(context.HASS).isoformat(),
                        "defaults": defaults,
                    }
                )
            return web.json_response(
                {"success": False, "error": "Failed to save defaults"}, status=500
            )
        except Exception as error:
            _LOGGER.error("Error updating defaults: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    def _get_tariff_manager(self):
        """Get tariff manager from HASS data. @zara"""
        if context.HASS is None:
            return
        domain_runtime = context.HASS.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if not isinstance(runtime_data, dict):
                continue
            if not "monthly_tariff_manager" in runtime_data:
                continue
            return runtime_data["monthly_tariff_manager"]
