# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Billing calculator using SFML database. @zara"""
from __future__ import annotations
import calendar
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
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

_LOGGER = logging.getLogger(__name__)


class BillingCalculator:
    """Calculate energy balance from SFML database. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        config_path: Path,
        entry_data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the calculator. @zara"""
        self._hass = hass
        self._config_path = config_path
        self._entry_data = entry_data
        self._db_path = config_path / "solar_forecast_ml" / "solar_forecast.db"
        self._billing_cache = None
        self._cache_timestamp = None
        self._cache_ttl_seconds = BILLING_CACHE_TTL_SECONDS
        self._correction_store = EnergyCorrectionStore(
            config_path / "sfml_stats" / "corrections.db",
            self._db_path,
            hass.config.time_zone,
        )

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get DB connection via manager with direct fallback. @zara"""
        from ..storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        connection = None
        db_manager = get_manager()
        if db_manager is not None:
            try:
                if not db_manager.is_connected:
                    await db_manager.connect()
                if db_manager.is_connected:
                    connection = await db_manager.get_connection()
            except Exception as error:
                _LOGGER.warning(
                    "Database connection manager auto-reconnect failed: %s", error
                )
        if connection is not None:
            yield connection
            return
        async with DatabaseConnectionManager.connect_direct_safe(
            self._db_path, self._hass
        ) as connection:
            yield connection
        if True:
            return

    @property
    def is_db_available(self) -> bool:
        """Check if SFML database is available. @zara"""
        return self._db_path.exists()

    def update_config(self, new_config: dict[str, Any]) -> None:
        """Update cached configuration and invalidate billing cache. @zara"""
        self._entry_data = new_config
        self._billing_cache = None
        self._cache_timestamp = None
        _LOGGER.debug("BillingCalculator config updated, cache invalidated")

    def invalidate_cache(self) -> None:
        """Invalidate cached billing totals after a correction event."""
        self._billing_cache = None
        self._cache_timestamp = None
        _LOGGER.debug("BillingCalculator cache invalidated")

    def _get_config(self) -> dict[str, Any]:
        """Get current configuration. @zara"""
        if self._entry_data:
            return self._entry_data
        domain_runtime = self._hass.data.get(DOMAIN, {})
        for entry_id, runtime_data in domain_runtime.items():
            if not isinstance(runtime_data, dict):
                continue
            if not "config" in runtime_data:
                continue
            return runtime_data["config"]
        config_entries = self._hass.config_entries.async_entries(DOMAIN)
        if config_entries:
            return dict(config_entries[0].data)
        return {}

    def _get_sensor_value(self, entity_id: str | None) -> float | None:
        """Read current value from a sensor. @zara"""
        if not entity_id:
            return
        sensor_state = self._hass.states.get(entity_id)
        if sensor_state is None or sensor_state.state in ("unknown", "unavailable"):
            return
        try:
            return float(sensor_state.state)
        except (ValueError, TypeError):
            return

    def _fixed_price_ct(self, config: dict[str, Any]) -> float:
        legacy_price_ct = config.get(CONF_LEGACY_FIXED_PRICE_CT)
        if legacy_price_ct not in (None, ""):
            try:
                return max(0.0, float(legacy_price_ct))
            except (TypeError, ValueError):
                pass
        try:
            work_price_ct = float(
                config.get(CONF_BILLING_WORK_PRICE, DEFAULT_BILLING_WORK_PRICE) or 0
            )
            grid_fees_ct = float(
                config.get(CONF_BILLING_GRID_FEES, DEFAULT_BILLING_GRID_FEES) or 0
            )
            fixed_price_ct = work_price_ct + grid_fees_ct
        except (TypeError, ValueError):
            fixed_price_ct = DEFAULT_BILLING_FIXED_PRICE
        if CONF_BILLING_WORK_PRICE not in config and CONF_BILLING_FIXED_PRICE in config:
            try:
                fixed_price_ct = float(config[CONF_BILLING_FIXED_PRICE] or 0)
            except (TypeError, ValueError):
                fixed_price_ct = DEFAULT_BILLING_FIXED_PRICE
        return max(0.0, fixed_price_ct)

    def _base_fee_eur(self, config: dict[str, Any]) -> float:
        from .price_mode import base_fee_eur_month as base_fee_eur_month

        return base_fee_eur_month(self._hass, config)

    def _feed_in_tariff_ct(self, config: dict[str, Any]) -> float:
        from .price_mode import feed_in_tariff_ct as feed_in_tariff_ct

        return feed_in_tariff_ct(self._hass, config)

    @staticmethod
    def _price_or_fallback(price: float | None, fallback: float) -> float:
        try:
            parsed_price_ct = float(price or 0)
        except (TypeError, ValueError):
            parsed_price_ct = 0.0
        return parsed_price_ct if parsed_price_ct > 0 else fallback

    def _get_billing_start_date(self, config: dict[str, Any]) -> date:
        """Calculate start date of current billing period. @zara"""
        billing_day = config.get(CONF_BILLING_START_DAY, 1)
        billing_month = config.get(CONF_BILLING_START_MONTH, 1)
        if isinstance(billing_day, str):
            billing_day = int(billing_day)
        if isinstance(billing_month, str):
            billing_month = int(billing_month)
        billing_day = max(1, billing_day or 1)
        billing_month = max(1, min(12, billing_month or 1))
        today = ha_today(self._hass)
        billing_year = today.year
        max_day = calendar.monthrange(billing_year, billing_month)[1]
        billing_day = min(billing_day, max_day)
        billing_start = date(billing_year, billing_month, billing_day)
        if billing_start > today:
            previous_max_day = calendar.monthrange(billing_year - 1, billing_month)[1]
            billing_day = min(billing_day, previous_max_day)
            billing_start = date(billing_year - 1, billing_month, billing_day)
        return billing_start

    async def _get_billing_data_from_db(self, billing_start: date) -> dict[str, float]:
        """Get summed energy data from stats_daily_energy since billing start. @zara"""
        if not self.is_db_available:
            _LOGGER.warning("SFML database not available at %s", self._db_path)
            return {}
        billing_start_str = billing_start.isoformat()
        corrections = await self._correction_store.async_projection(billing_start_str)
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        date,\n                        COALESCE(solar_yield_kwh, 0) as solar_yield_kwh,\n                        COALESCE(solar_to_house_kwh, 0) as solar_to_house_kwh,\n                        COALESCE(solar_to_battery_kwh, 0) as solar_to_battery_kwh,\n                        COALESCE(battery_to_house_kwh, 0) as battery_to_house_kwh,\n                        COALESCE(grid_to_house_kwh, 0) as grid_to_house_kwh,\n                        COALESCE(home_consumption_kwh, 0) as home_consumption_kwh\n                    FROM stats_daily_energy\n                    WHERE date >= ?\n                ",
                    (billing_start_str,),
                ) as cursor:
                    rows = await cursor.fetchall()
                if rows:
                    raw_solar_total_kwh = sum(
                        (row["solar_yield_kwh"] or 0.0 for row in rows)
                    )
                    return {
                        "solar_total_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "solar_yield_day_kwh",
                                    row["solar_yield_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "solar_to_house_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "solar_to_house_day_kwh",
                                    row["solar_to_house_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "solar_to_battery_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "solar_to_battery_day_kwh",
                                    row["solar_to_battery_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "battery_to_house_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "battery_to_house_day_kwh",
                                    row["battery_to_house_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "grid_to_house_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "grid_to_house_day_kwh",
                                    row["grid_to_house_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "home_consumption_kwh": sum(
                            (
                                corrected_value(
                                    corrections,
                                    row["date"],
                                    "home_consumption_day_kwh",
                                    row["home_consumption_kwh"],
                                )
                                for row in rows
                            )
                        ),
                        "days_with_data": len(rows),
                        "raw_solar_total_kwh": raw_solar_total_kwh,
                        "has_solar_corrections": any(
                            (
                                "solar_yield_day_kwh" in correction_values
                                for correction_values in corrections.values()
                            )
                        ),
                    }
                return {}
        except Exception as error:
            _LOGGER.error("Error reading billing data from DB: %s", error)
            return {}
        return None if True else None

    async def _get_smartmeter_data_from_db(
        self, billing_start: date
    ) -> dict[str, float]:
        """Get smartmeter data from stats_daily_energy if available. @zara"""
        if not self.is_db_available:
            return {}
        billing_start_str = billing_start.isoformat()
        corrections = await self._correction_store.async_projection(billing_start_str)
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        date, COALESCE(smartmeter_import_kwh, 0) smartmeter_import_kwh,\n                        COALESCE(smartmeter_export_kwh, 0) smartmeter_export_kwh,\n                        COALESCE(grid_import_kwh, 0) grid_import_kwh,\n                        COALESCE(grid_export_kwh, 0) grid_export_kwh,\n                        COALESCE(grid_to_house_kwh, 0) grid_to_house_kwh,\n                        COALESCE(grid_to_battery_kwh, 0) grid_to_battery_kwh\n                    FROM stats_daily_energy\n                    WHERE date >= ?\n                ",
                    (billing_start_str,),
                ) as cursor:
                    rows = await cursor.fetchall()
                total_import_kwh = 0.0
                total_export_kwh = 0.0
                total_grid_to_battery_kwh = 0.0
                for row in rows:
                    row_import_kwh = float(row["smartmeter_import_kwh"] or 0.0)
                    if row_import_kwh <= 0:
                        row_import_kwh = max(
                            float(row["grid_import_kwh"] or 0.0),
                            float(row["grid_to_house_kwh"] or 0.0)
                            + float(row["grid_to_battery_kwh"] or 0.0),
                        )
                    row_export_kwh = float(row["smartmeter_export_kwh"] or 0.0)
                    if row_export_kwh <= 0:
                        row_export_kwh = float(row["grid_export_kwh"] or 0.0)
                    total_import_kwh += corrected_value(
                        corrections, row["date"], "grid_import_day_kwh", row_import_kwh
                    )
                    total_export_kwh += corrected_value(
                        corrections, row["date"], "grid_export_day_kwh", row_export_kwh
                    )
                    total_grid_to_battery_kwh += float(
                        row["grid_to_battery_kwh"] or 0.0
                    )
                    continue
                return {
                    "smartmeter_import_kwh": total_import_kwh,
                    "smartmeter_export_kwh": total_export_kwh,
                    "grid_to_battery_kwh": total_grid_to_battery_kwh,
                    "has_import_corrections": any(
                        (
                            "grid_import_day_kwh" in correction_values
                            for correction_values in corrections.values()
                        )
                    ),
                    "has_export_corrections": any(
                        (
                            "grid_export_day_kwh" in correction_values
                            for correction_values in corrections.values()
                        )
                    ),
                }
        except Exception:
            return {}
        return None if True else None

    async def _get_consumer_data_from_db(self, billing_start: date) -> dict[str, float]:
        """Get consumer energy data from stats_daily_energy. @zara"""
        if not self.is_db_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        COALESCE(SUM(consumer_heatpump_kwh), 0) as consumer_heatpump_kwh,\n                        COALESCE(SUM(consumer_heatingrod_kwh), 0) as consumer_heatingrod_kwh,\n                        COALESCE(SUM(consumer_wallbox_kwh), 0) as consumer_wallbox_kwh\n                    FROM stats_daily_energy\n                    WHERE date >= ?\n                ",
                    (billing_start.isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return {
                        "consumer_heatpump_kwh": row["consumer_heatpump_kwh"] or 0.0,
                        "consumer_heatingrod_kwh": row["consumer_heatingrod_kwh"]
                        or 0.0,
                        "consumer_wallbox_kwh": row["consumer_wallbox_kwh"] or 0.0,
                    }
                return {}
        except Exception:
            return {}
        return None if True else None

    async def _get_consumer_data_today(self) -> dict[str, float]:
        """Get consumer energy data for today only. @zara"""
        if not self.is_db_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        COALESCE(consumer_heatpump_kwh, 0) as consumer_heatpump_kwh,\n                        COALESCE(consumer_heatingrod_kwh, 0) as consumer_heatingrod_kwh,\n                        COALESCE(consumer_wallbox_kwh, 0) as consumer_wallbox_kwh\n                    FROM stats_daily_energy\n                    WHERE date = ?\n                ",
                    (ha_today(self._hass).isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return {
                        "consumer_heatpump_kwh": row["consumer_heatpump_kwh"] or 0.0,
                        "consumer_heatingrod_kwh": row["consumer_heatingrod_kwh"]
                        or 0.0,
                        "consumer_wallbox_kwh": row["consumer_wallbox_kwh"] or 0.0,
                    }
                return {}
        except Exception:
            return {}
        return None if True else None

    async def async_ensure_baselines(self) -> dict[str, Any]:
        """Compatibility stub - no longer needed. @zara"""
        return {}

    async def async_calculate_billing(self) -> dict[str, Any]:
        """Calculate current energy balance from database. @zara"""
        if False:
            __assert_armored__ = None
        if self._billing_cache is not None and self._cache_timestamp is not None:
            cache_age_seconds = (
                ha_now(self._hass) - self._cache_timestamp
            ).total_seconds()
            if cache_age_seconds < self._cache_ttl_seconds:
                _LOGGER.debug(
                    "Returning cached billing result (age: %.1fs)", cache_age_seconds
                )
                return self._billing_cache
        _LOGGER.debug("Calculating billing data from database")
        config = self._get_config()
        billing_start = self._get_billing_start_date(config)
        today = ha_today(self._hass)
        energy_data = await self._get_billing_data_from_db(billing_start)
        smartmeter_data = await self._get_smartmeter_data_from_db(billing_start)
        consumer_period_data = await self._get_consumer_data_from_db(billing_start)
        consumer_today_data = await self._get_consumer_data_today()
        if not energy_data:
            _LOGGER.warning("No billing data available in database")
            return {
                "success": False,
                "error": "No data available in database",
                "data_available": False,
            }
        solar_total_kwh = energy_data.get("solar_total_kwh", 0.0)
        raw_solar_total_kwh = energy_data.get("raw_solar_total_kwh", solar_total_kwh)
        solar_to_house_kwh = energy_data.get("solar_to_house_kwh", 0.0)
        solar_to_battery_kwh = energy_data.get("solar_to_battery_kwh", 0.0)
        battery_to_house_kwh = energy_data.get("battery_to_house_kwh", 0.0)
        grid_to_house_kwh = energy_data.get("grid_to_house_kwh", 0.0)
        home_consumption_kwh = energy_data.get("home_consumption_kwh", 0.0)
        days_with_data = energy_data.get("days_with_data", 0)
        smartmeter_import_kwh = smartmeter_data.get("smartmeter_import_kwh", 0.0)
        smartmeter_export_kwh = smartmeter_data.get("smartmeter_export_kwh", 0.0)
        grid_to_battery_kwh = smartmeter_data.get("grid_to_battery_kwh", 0.0)
        battery_own_supply_kwh = max(0.0, battery_to_house_kwh - grid_to_battery_kwh)
        own_supply_kwh = solar_to_house_kwh + battery_own_supply_kwh
        total_battery_charge_kwh = solar_to_battery_kwh + grid_to_battery_kwh
        if (
            smartmeter_export_kwh == 0
            and raw_solar_total_kwh > 0
            and (not smartmeter_data.get("has_export_corrections", False))
        ):
            inferred_export_kwh = (
                raw_solar_total_kwh - solar_to_house_kwh - solar_to_battery_kwh
            )
            if inferred_export_kwh >= 0.05:
                smartmeter_export_kwh = inferred_export_kwh
            else:
                smartmeter_export_kwh = 0.0
        has_meter_corrections = bool(
            smartmeter_data.get("has_import_corrections")
            or smartmeter_data.get("has_export_corrections")
        )
        has_energy_corrections = bool(
            has_meter_corrections or energy_data.get("has_solar_corrections")
        )
        if home_consumption_kwh > 0:
            autarky_percent = min(100, own_supply_kwh / home_consumption_kwh * 100)
        else:
            autarky_percent = 0.0
        from .price_mode import (
            MODE_LEGACY_FIXED as MODE_LEGACY_FIXED,
            MODE_NONE as MODE_NONE,
            effective_price_mode as effective_price_mode,
            is_fixed_like as is_fixed_like,
        )

        price_mode = effective_price_mode(self._hass, config)
        hours_without_price = 0
        if price_mode == MODE_NONE:
            average_price_ct = 0.0
            energy_cost_eur = 0.0
            savings_eur = 0.0
            feed_in_revenue_eur = 0.0
            dynamic_consumer_costs = {}
        elif price_mode == MODE_LEGACY_FIXED:
            average_price_ct = self._fixed_price_ct(config)
            statement_corrections = await self._correction_store.async_projection(
                billing_start.isoformat()
            )
            async with self._get_db() as connection:
                statement_totals = await statement_energy_cost(
                    connection,
                    billing_start.isoformat(),
                    today.isoformat(),
                    statement_corrections,
                    tariff_fallback_ct=average_price_ct,
                    price_mode=price_mode,
                    config=config,
                )
            energy_cost_eur = statement_totals["energy_eur"]
            hours_without_price = int(statement_totals["hours_without_price"] or 0)
            if statement_totals["kwh"] > 0:
                average_price_ct = statement_totals["avg_ct"]
            savings_eur = own_supply_kwh * self._fixed_price_ct(config) / 100
            feed_in_revenue_eur = (
                smartmeter_export_kwh * self._feed_in_tariff_ct(config) / 100
            )
            dynamic_consumer_costs = {}
        else:
            dynamic_costs = await self._get_dynamic_costs_from_db(
                billing_start, config, price_mode
            )
            energy_cost_eur = dynamic_costs["grid_cost_eur"]
            savings_eur = dynamic_costs["savings_eur"]
            average_price_ct = dynamic_costs["weighted_avg_price_ct"]
            feed_in_revenue_eur = dynamic_costs.get("feed_in_revenue_eur", 0.0)
            dynamic_consumer_costs = dynamic_costs.get("consumers", {})
            hours_without_price = int(dynamic_costs.get("hours_without_price") or 0)
            fallback_price_ct = (
                self._fixed_price_ct(config) if is_fixed_like(price_mode) else None
            )
            statement_corrections = await self._correction_store.async_projection(
                billing_start.isoformat()
            )
            async with self._get_db() as connection:
                statement_totals = await statement_energy_cost(
                    connection,
                    billing_start.isoformat(),
                    today.isoformat(),
                    statement_corrections,
                    tariff_fallback_ct=fallback_price_ct,
                    price_mode=price_mode,
                    config=config,
                )
            energy_cost_eur = statement_totals["energy_eur"]
            hours_without_price = int(statement_totals["hours_without_price"] or 0)
            if statement_totals["kwh"] > 0:
                average_price_ct = statement_totals["avg_ct"]
        days_elapsed = (today - billing_start).days + 1
        next_billing_year = billing_start.year + 1
        billing_month = billing_start.month
        billing_day = billing_start.day
        max_billing_day = calendar.monthrange(next_billing_year, billing_month)[1]
        billing_day = min(billing_day, max_billing_day)
        theoretical_period_end = date(
            next_billing_year, billing_month, billing_day
        ) - timedelta(days=1)
        period_days = (theoretical_period_end - billing_start).days + 1
        monthly_base_fee_eur = self._base_fee_eur(config)
        allocated_base_fee_eur = (
            monthly_base_fee_eur / 30.436875 * days_elapsed
            if monthly_base_fee_eur > 0
            else 0.0
        )
        total_cost_eur = energy_cost_eur + allocated_base_fee_eur
        total_savings_eur = savings_eur + feed_in_revenue_eur
        balance_diagnostics = calculate_balance_diagnostics(
            household_total_kwh=home_consumption_kwh,
            solar_to_house_kwh=solar_to_house_kwh,
            battery_to_house_kwh=battery_to_house_kwh,
            grid_to_house_kwh=grid_to_house_kwh,
            smartmeter_import_kwh=smartmeter_import_kwh,
            grid_to_battery_kwh=grid_to_battery_kwh,
            solar_total_kwh=raw_solar_total_kwh,
            solar_to_battery_kwh=solar_to_battery_kwh,
            grid_export_kwh=smartmeter_export_kwh,
        )
        billing_result = {
            "success": True,
            "data_available": days_with_data > 0,
            "data_source": "stats_daily_energy",
            "period": {
                "start": billing_start.isoformat(),
                "end": today.isoformat(),
                "end_theoretical": theoretical_period_end.isoformat(),
                "days_elapsed": days_elapsed,
                "days_total": period_days,
                "days_with_data": days_with_data,
                "progress_percent": round(days_elapsed / period_days * 100, 1),
            },
            "household": {
                "total_kwh": round(home_consumption_kwh, 2),
                "from_solar_kwh": round(solar_to_house_kwh, 2),
                "from_battery_kwh": round(battery_to_house_kwh, 2),
                "from_own_battery_kwh": round(battery_own_supply_kwh, 2),
                "self_supplied_kwh": round(own_supply_kwh, 2),
                "from_grid_kwh": round(grid_to_house_kwh, 2),
                "assigned_kwh": balance_diagnostics["household_assigned_kwh"],
                "from_unknown_kwh": balance_diagnostics["household_unknown_kwh"],
            },
            "battery": {
                "total_charge_kwh": round(total_battery_charge_kwh, 2),
                "from_solar_kwh": round(solar_to_battery_kwh, 2),
                "from_grid_kwh": round(grid_to_battery_kwh, 2),
            },
            "solar": {
                "total_kwh": round(solar_total_kwh, 2),
                "to_house_kwh": round(solar_to_house_kwh, 2),
                "to_battery_kwh": round(solar_to_battery_kwh, 2),
                "correction_applied": bool(energy_data.get("has_solar_corrections")),
                "flow_contract": "raw_measured_flows",
            },
            "grid": {
                "total_import_kwh": round(smartmeter_import_kwh, 2),
                "to_house_kwh": round(grid_to_house_kwh, 2),
                "to_battery_kwh": round(grid_to_battery_kwh, 2),
                "export_kwh": round(smartmeter_export_kwh, 2),
            },
            "finance": {
                "grid_cost_eur": round(total_cost_eur, 2),
                "total_cost_eur": round(total_cost_eur, 2),
                "energy_cost_eur": round(energy_cost_eur, 2),
                "base_fee_eur": round(allocated_base_fee_eur, 2),
                "base_fee_monthly_eur": round(monthly_base_fee_eur, 2),
                "savings_eur": round(savings_eur, 2),
                "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
                "total_savings_eur": round(total_savings_eur, 2),
                "net_benefit_eur": round(total_savings_eur - total_cost_eur, 2),
                "avg_price_ct": (
                    round(average_price_ct, 2) if average_price_ct else None
                ),
                "hours_without_price": hours_without_price,
                "energy_corrections_applied": has_energy_corrections,
                "costs_include_energy_corrections": True,
                "correction_cost_contract": "costs_match_corrected_energy",
            },
            "autarkie_percent": round(autarky_percent, 1),
            "balance_diagnostics": balance_diagnostics,
            "config": {
                "billing_start_day": config.get(CONF_BILLING_START_DAY),
                "billing_start_month": config.get(CONF_BILLING_START_MONTH),
                "price_mode": price_mode,
            },
        }
        heatpump_period_kwh = consumer_period_data.get("consumer_heatpump_kwh", 0.0)
        heatingrod_period_kwh = consumer_period_data.get("consumer_heatingrod_kwh", 0.0)
        wallbox_period_kwh = consumer_period_data.get("consumer_wallbox_kwh", 0.0)
        heatpump_today_kwh = consumer_today_data.get("consumer_heatpump_kwh", 0.0)
        heatingrod_today_kwh = consumer_today_data.get("consumer_heatingrod_kwh", 0.0)
        wallbox_today_kwh = consumer_today_data.get("consumer_wallbox_kwh", 0.0)
        if price_mode == MODE_NONE:
            consumer_price_ct = 0.0
        else:
            consumer_price_ct = average_price_ct

        def consumer_cost(key: str, kwh: float) -> float:
            stored_cost_eur = (
                dynamic_consumer_costs.get(key, {}).get("cost_eur")
                if dynamic_consumer_costs
                else None
            )
            if stored_cost_eur is not None and stored_cost_eur > 0:
                return stored_cost_eur
            return kwh * consumer_price_ct / 100

        def consumer_daily_cost(key: str, kwh: float) -> float:
            stored_today_cost_eur = (
                dynamic_consumer_costs.get(key, {}).get("today_cost_eur")
                if dynamic_consumer_costs
                else None
            )
            if stored_today_cost_eur is not None and stored_today_cost_eur > 0:
                return stored_today_cost_eur
            return kwh * consumer_price_ct / 100

        heatpump_cost_eur = consumer_cost("heatpump", heatpump_period_kwh)
        heatingrod_cost_eur = consumer_cost("heatingrod", heatingrod_period_kwh)
        wallbox_cost_eur = consumer_cost("wallbox", wallbox_period_kwh)
        heatpump_today_cost_eur = consumer_daily_cost("heatpump", heatpump_today_kwh)
        heatingrod_today_cost_eur = consumer_daily_cost(
            "heatingrod", heatingrod_today_kwh
        )
        wallbox_today_cost_eur = consumer_daily_cost("wallbox", wallbox_today_kwh)
        billing_result["consumers"] = {
            "heatpump": {
                "total_kwh": round(heatpump_period_kwh, 2),
                "cost_eur": round(heatpump_cost_eur, 2),
                "daily_cost_eur": round(heatpump_today_cost_eur, 2),
                "avg_price_ct": (
                    round(heatpump_cost_eur * 100 / heatpump_period_kwh, 2)
                    if heatpump_period_kwh > 0
                    else 0.0
                ),
            },
            "heatingrod": {
                "total_kwh": round(heatingrod_period_kwh, 2),
                "cost_eur": round(heatingrod_cost_eur, 2),
                "daily_cost_eur": round(heatingrod_today_cost_eur, 2),
                "avg_price_ct": (
                    round(heatingrod_cost_eur * 100 / heatingrod_period_kwh, 2)
                    if heatingrod_period_kwh > 0
                    else 0.0
                ),
            },
            "wallbox": {
                "total_kwh": round(wallbox_period_kwh, 2),
                "cost_eur": round(wallbox_cost_eur, 2),
                "daily_cost_eur": round(wallbox_today_cost_eur, 2),
                "avg_price_ct": (
                    round(wallbox_cost_eur * 100 / wallbox_period_kwh, 2)
                    if wallbox_period_kwh > 0
                    else 0.0
                ),
            },
            "total_kwh": round(
                heatpump_period_kwh + heatingrod_period_kwh + wallbox_period_kwh, 2
            ),
            "total_cost_eur": round(
                heatpump_cost_eur + heatingrod_cost_eur + wallbox_cost_eur, 2
            ),
            "daily_cost_eur": round(
                heatpump_today_cost_eur
                + heatingrod_today_cost_eur
                + wallbox_today_cost_eur,
                2,
            ),
            "price_ct_kwh": round(consumer_price_ct, 2),
        }
        _LOGGER.debug(
            "Billing calculated: consumption=%.2f kWh, autarkie=%.1f%%, cost=%.2f EUR",
            home_consumption_kwh,
            autarky_percent,
            energy_cost_eur,
        )
        try:
            from .daily_costs import load_provenance as load_provenance

            async with self._get_db() as connection:
                corrections = {}
                if self._correction_store is not None:
                    corrections = await self._correction_store.async_projection(
                        billing_start.isoformat()
                    )
                billing_result["provenance"] = await load_provenance(
                    connection, billing_start, today + timedelta(days=1), corrections
                )
        except Exception as error:
            _LOGGER.debug("Billing provenance unavailable: %s", error)
        try:
            from .grid_import_split import (
                battery_is_configured as battery_is_configured,
                fixed_total_price_ct as fixed_total_price_ct,
                import_split_between as import_split_between,
            )

            if battery_is_configured(config):
                start_date_str = billing_start.isoformat()
                end_date_str = today.isoformat()
                grid_split_corrections = {}
                if self._correction_store is not None:
                    grid_split_corrections = (
                        await self._correction_store.async_projection(start_date_str)
                    )
                async with self._get_db() as connection:
                    resolved_corrected_daily_report = await corrected_daily_report(
                        connection, start_date_str, end_date_str, grid_split_corrections
                    )
                    billing_result["grid"]["import_split"] = await import_split_between(
                        connection,
                        start_date_str,
                        end_date_str,
                        resolved_corrected_daily_report,
                        grid_split_corrections,
                        fixed_total_price_ct(config),
                    )
        except Exception as error:
            _LOGGER.debug("Grid import split unavailable: %s", error)
        self._billing_cache = billing_result
        self._cache_timestamp = ha_now(self._hass)
        return billing_result

    async def _get_dynamic_costs_from_db(
        self, billing_start: date, config: dict[str, Any], price_mode: str
    ) -> dict[str, Any]:
        """Calculate costs and savings from hourly billing and daily energy data. @zara"""
        from collections import defaultdict as defaultdict
        from .price_mode import (
            MODE_GPM_FIXED as MODE_GPM_FIXED,
            gpm_fixed_total_ct as gpm_fixed_total_ct,
            resolve_hour_price as resolve_hour_price,
        )

        empty_result = {
            "grid_cost_eur": 0,
            "savings_eur": 0,
            "feed_in_revenue_eur": 0,
            "weighted_avg_price_ct": 0,
            "consumers": {},
            "hours_without_price": 0,
        }
        if not self.is_db_available:
            return empty_result
        billing_start_str = billing_start.isoformat()
        today_str = ha_today(self._hass).isoformat()
        fallback_feed_in_ct = self._feed_in_tariff_ct(config)
        grid_cost_ct = 0.0
        self_savings_ct = 0.0
        export_revenue_ct = 0.0
        priced_import_kwh = 0.0
        weighted_price_sum_ct = 0.0
        hours_without_price = 0
        hourly_dates = set()
        grid_charge_by_date = defaultdict(float)
        own_supply_by_date = defaultdict(float)
        export_by_date = defaultdict(float)
        consumer_kwh_by_date = defaultdict(lambda: defaultdict(float))
        consumer_cost_totals = defaultdict(lambda: defaultdict(float))
        try:
            await self._ensure_hourly_finance_columns()
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        hb.date,\n                        hb.hour,\n                        COALESCE(hb.grid_import_kwh, 0) as grid_import_kwh,\n                        COALESCE(hb.grid_to_house_kwh, 0) as grid_to_house_kwh,\n                        COALESCE(hb.grid_to_battery_kwh, 0) as grid_to_battery_kwh,\n                        COALESCE(hb.solar_to_house_kwh, 0) as solar_to_house_kwh,\n                        COALESCE(hb.battery_to_house_kwh, 0) as battery_to_house_kwh,\n                        COALESCE(hb.grid_export_kwh, 0) as grid_export_kwh,\n                        COALESCE(hb.feed_in_revenue_ct, 0) as feed_in_revenue_ct,\n                        COALESCE(hb.feed_in_tariff_ct, 0) as feed_in_tariff_ct,\n                        COALESCE(hb.consumer_heatpump_kwh, 0) as consumer_heatpump_kwh,\n                        COALESCE(hb.consumer_heatpump_cost_ct, 0) as consumer_heatpump_cost_ct,\n                        COALESCE(hb.consumer_heatingrod_kwh, 0) as consumer_heatingrod_kwh,\n                        COALESCE(hb.consumer_heatingrod_cost_ct, 0) as consumer_heatingrod_cost_ct,\n                        COALESCE(hb.consumer_wallbox_kwh, 0) as consumer_wallbox_kwh,\n                        COALESCE(hb.consumer_wallbox_cost_ct, 0) as consumer_wallbox_cost_ct,\n                        gpm.total_price as gpm_total,\n                        hb.price_ct_kwh as stored_price\n                    FROM stats_hourly_billing hb\n                    LEFT JOIN GPM_price_history gpm\n                        ON gpm.hour = hb.hour\n                        AND gpm.timestamp >= hb.date || 'T00:00:00'\n                        AND gpm.timestamp < date(hb.date, '+1 day') || 'T00:00:00'\n                    WHERE hb.date >= ?\n                    ORDER BY hb.date, hb.hour\n                ",
                    (billing_start_str,),
                ) as cursor:
                    billing_rows = [
                        {
                            column_or_consumer_key: row[column_or_consumer_key]
                            for column_or_consumer_key in row.keys()
                        }
                        async for row in cursor
                    ]
                corrections = await self._correction_store.async_projection(
                    billing_start_str
                )
                corrected_days = await corrected_daily(
                    connection, billing_start_str, today_str, corrections
                )
                scale_billing_rows(billing_rows, corrected_days, corrections)
                gpm_fixed_price_ct = (
                    gpm_fixed_total_ct(self._hass)
                    if price_mode == MODE_GPM_FIXED
                    else None
                )
                for row in billing_rows:
                    resolved_price_ct = resolve_hour_price(
                        gpm_total=row["gpm_total"],
                        stored_price=row["stored_price"],
                        mode=price_mode,
                        config=config,
                        fixed_total_ct=gpm_fixed_price_ct,
                    )
                    if resolved_price_ct is None:
                        hours_without_price += 1
                        continue
                    resolved_billed_import_kwh = billed_import_kwh(
                        row["grid_import_kwh"],
                        row["grid_to_house_kwh"],
                        row["grid_to_battery_kwh"],
                        meter_configured=True,
                    )
                    battery_own_supply_kwh = max(
                        0.0,
                        (row["battery_to_house_kwh"] or 0)
                        - (row["grid_to_battery_kwh"] or 0),
                    )
                    own_supply_kwh = (
                        row["solar_to_house_kwh"] or 0
                    ) + battery_own_supply_kwh
                    grid_cost_ct += resolved_billed_import_kwh * resolved_price_ct
                    self_savings_ct += own_supply_kwh * resolved_price_ct
                    priced_import_kwh += resolved_billed_import_kwh
                    weighted_price_sum_ct += (
                        resolved_billed_import_kwh * resolved_price_ct
                    )
                    hourly_dates.add(row["date"])
                    export_kwh = row["grid_export_kwh"] or 0
                    feed_in_tariff_ct = self._price_or_fallback(
                        row["feed_in_tariff_ct"], fallback_feed_in_ct
                    )
                    feed_in_revenue_ct = (
                        row["feed_in_revenue_ct"] or export_kwh * feed_in_tariff_ct
                    )
                    export_revenue_ct += feed_in_revenue_ct
                    export_by_date[row["date"]] += export_kwh
                    grid_charge_by_date[row["date"]] += row["grid_to_battery_kwh"] or 0
                    own_supply_by_date[row["date"]] += own_supply_kwh
                    for (
                        column_or_consumer_key,
                        consumer_energy_column,
                        consumer_cost_column,
                    ) in (
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
                        consumer_kwh = row[consumer_energy_column] or 0
                        consumer_cost_ct = (
                            row[consumer_cost_column]
                            or consumer_kwh * resolved_price_ct
                        )
                        consumer_cost_totals[column_or_consumer_key][
                            "kwh"
                        ] += consumer_kwh
                        consumer_cost_totals[column_or_consumer_key][
                            "cost_ct"
                        ] += consumer_cost_ct
                        if row["date"] == today_str:
                            consumer_cost_totals[column_or_consumer_key][
                                "today_cost_ct"
                            ] += consumer_cost_ct
                        consumer_kwh_by_date[row["date"]][
                            column_or_consumer_key
                        ] += consumer_kwh
                    continue
                async with connection.execute(
                    "\n                    SELECT\n                        e.date,\n                        COALESCE(e.grid_import_kwh, 0) as grid_import_kwh,\n                        COALESCE(e.grid_to_house_kwh, 0) as grid_to_house_kwh,\n                        COALESCE(e.grid_to_battery_kwh, 0) as grid_to_battery_kwh,\n                        COALESCE(e.solar_to_house_kwh, 0) as solar_to_house_kwh,\n                        COALESCE(e.battery_to_house_kwh, 0) as battery_to_house_kwh,\n                        COALESCE(NULLIF(e.smartmeter_export_kwh, 0), e.grid_export_kwh, 0) as export_kwh,\n                        COALESCE(e.consumer_heatpump_kwh, 0) as consumer_heatpump_kwh,\n                        COALESCE(e.consumer_heatingrod_kwh, 0) as consumer_heatingrod_kwh,\n                        COALESCE(e.consumer_wallbox_kwh, 0) as consumer_wallbox_kwh,\n                        COALESCE(p.average_total, 0) as daily_avg_price_ct\n                    FROM stats_daily_energy e\n                    LEFT JOIN GPM_daily_averages p ON e.date = p.date\n                    WHERE e.date >= ?\n                    ORDER BY e.date\n                ",
                    (billing_start_str,),
                ) as cursor:
                    async for row in cursor:
                        resolved_price_ct = resolve_hour_price(
                            daily_average=row["daily_avg_price_ct"],
                            current_average=None,
                            mode=price_mode,
                            config=config,
                            fixed_total_ct=gpm_fixed_price_ct,
                        )
                        if resolved_price_ct is None:
                            continue
                        if row["date"] in hourly_dates:
                            corrected_day = corrected_days.get(str(row["date"]), {})
                            day_grid_charge_kwh = corrected_day.get(
                                "grid_to_battery_day_kwh",
                                row["grid_to_battery_kwh"] or 0,
                            )
                            covered_grid_charge_kwh = grid_charge_by_date.get(
                                row["date"], 0
                            )
                            grid_charge_corrected = not kwh_unchanged(
                                corrected_day.get(
                                    "raw_grid_to_battery_kwh", day_grid_charge_kwh
                                ),
                                day_grid_charge_kwh,
                            )
                            grid_charge_remainder_kwh = correction_remainder(
                                day_grid_charge_kwh,
                                covered_grid_charge_kwh,
                                corrected=grid_charge_corrected,
                            )
                            if grid_charge_remainder_kwh != 0:
                                grid_cost_ct += (
                                    grid_charge_remainder_kwh * resolved_price_ct
                                )
                                priced_import_kwh += grid_charge_remainder_kwh
                                weighted_price_sum_ct += (
                                    grid_charge_remainder_kwh * resolved_price_ct
                                )
                            day_own_battery_kwh = max(
                                0.0,
                                corrected_day.get(
                                    "battery_to_house_day_kwh",
                                    row["battery_to_house_kwh"] or 0,
                                )
                                - corrected_day.get(
                                    "grid_to_battery_day_kwh",
                                    row["grid_to_battery_kwh"] or 0,
                                ),
                            )
                            day_own_supply_kwh = (
                                corrected_day.get(
                                    "solar_to_house_day_kwh",
                                    row["solar_to_house_kwh"] or 0,
                                )
                                + day_own_battery_kwh
                            )
                            covered_own_supply_kwh = own_supply_by_date.get(
                                row["date"], 0
                            )
                            raw_own_supply_kwh = (row["solar_to_house_kwh"] or 0) + max(
                                0.0,
                                (row["battery_to_house_kwh"] or 0)
                                - (row["grid_to_battery_kwh"] or 0),
                            )
                            own_supply_remainder_kwh = correction_remainder(
                                day_own_supply_kwh,
                                covered_own_supply_kwh,
                                corrected=not kwh_unchanged(
                                    raw_own_supply_kwh, day_own_supply_kwh
                                ),
                            )
                            if own_supply_remainder_kwh != 0:
                                self_savings_ct += (
                                    own_supply_remainder_kwh * resolved_price_ct
                                )
                            row_export_kwh = row["export_kwh"] or 0
                            corrected_export_kwh = corrected_day.get(
                                "grid_export_day_kwh", row_export_kwh
                            )
                            export_remainder_kwh = correction_remainder(
                                corrected_export_kwh,
                                export_by_date.get(row["date"], 0),
                                corrected=not kwh_unchanged(
                                    row_export_kwh, corrected_export_kwh
                                ),
                            )
                            if export_remainder_kwh != 0:
                                export_revenue_ct += (
                                    export_remainder_kwh * fallback_feed_in_ct
                                )
                            for (
                                column_or_consumer_key,
                                resolved_consumer_energy_column,
                                consumer_correction_key,
                            ) in (
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
                                raw_consumer_kwh = (
                                    row[resolved_consumer_energy_column] or 0
                                )
                                corrected_consumer_kwh = corrected_day.get(
                                    consumer_correction_key, raw_consumer_kwh
                                )
                                consumer_remainder_kwh = correction_remainder(
                                    corrected_consumer_kwh,
                                    consumer_kwh_by_date[row["date"]].get(
                                        column_or_consumer_key, 0
                                    ),
                                    corrected=not kwh_unchanged(
                                        raw_consumer_kwh, corrected_consumer_kwh
                                    ),
                                )
                                if consumer_remainder_kwh != 0:
                                    consumer_cost_ct = (
                                        consumer_remainder_kwh * resolved_price_ct
                                    )
                                    consumer_cost_totals[column_or_consumer_key][
                                        "cost_ct"
                                    ] += consumer_cost_ct
                                    if row["date"] == today_str:
                                        consumer_cost_totals[column_or_consumer_key][
                                            "today_cost_ct"
                                        ] += consumer_cost_ct
                        else:
                            corrected_day = corrected_days.get(str(row["date"]), {})
                            day_import_kwh = row["grid_import_kwh"]
                            if (
                                "raw_grid_import_kwh" in corrected_day
                                or "grid_import_day_kwh" in corrected_day
                            ):
                                day_import_kwh = corrected_day.get(
                                    "grid_import_day_kwh",
                                    corrected_day.get(
                                        "raw_grid_import_kwh", day_import_kwh
                                    ),
                                )
                            meter_configured = bool(
                                config.get(CONF_SENSOR_SMARTMETER_IMPORT)
                                or config.get(CONF_SENSOR_SMARTMETER_IMPORT_KWH)
                            )
                            resolved_billed_import_kwh = billed_import_kwh(
                                day_import_kwh,
                                row["grid_to_house_kwh"],
                                row["grid_to_battery_kwh"],
                                meter_configured=meter_configured,
                            )
                            battery_own_supply_kwh = max(
                                0.0,
                                corrected_day.get(
                                    "battery_to_house_day_kwh",
                                    row["battery_to_house_kwh"] or 0,
                                )
                                - corrected_day.get(
                                    "grid_to_battery_day_kwh",
                                    row["grid_to_battery_kwh"] or 0,
                                ),
                            )
                            own_supply_kwh = (
                                corrected_day.get(
                                    "solar_to_house_day_kwh",
                                    row["solar_to_house_kwh"] or 0,
                                )
                                + battery_own_supply_kwh
                            )
                            grid_cost_ct += (
                                resolved_billed_import_kwh * resolved_price_ct
                            )
                            self_savings_ct += own_supply_kwh * resolved_price_ct
                            row_export_kwh = row["export_kwh"] or 0
                            corrected_export_kwh = corrected_day.get(
                                "grid_export_day_kwh", row_export_kwh
                            )
                            export_revenue_ct += corrected_day_cost(
                                row_export_kwh,
                                corrected_export_kwh,
                                row_export_kwh * fallback_feed_in_ct,
                                fallback_price_ct=fallback_feed_in_ct,
                                cost_in_cent=True,
                            )
                            priced_import_kwh += resolved_billed_import_kwh
                            weighted_price_sum_ct += (
                                resolved_billed_import_kwh * resolved_price_ct
                            )
                            for (
                                column_or_consumer_key,
                                resolved_consumer_energy_column,
                                consumer_correction_key,
                            ) in (
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
                                raw_consumer_kwh = (
                                    row[resolved_consumer_energy_column] or 0
                                )
                                corrected_consumer_kwh = corrected_day.get(
                                    consumer_correction_key, raw_consumer_kwh
                                )
                                consumer_cost_ct = corrected_day_cost(
                                    raw_consumer_kwh,
                                    corrected_consumer_kwh,
                                    raw_consumer_kwh * resolved_price_ct,
                                    fallback_price_ct=resolved_price_ct,
                                    cost_in_cent=True,
                                )
                                consumer_cost_totals[column_or_consumer_key][
                                    "cost_ct"
                                ] += consumer_cost_ct
                                if row["date"] == today_str:
                                    consumer_cost_totals[column_or_consumer_key][
                                        "today_cost_ct"
                                    ] += consumer_cost_ct
        except Exception as error:
            _LOGGER.error("Error calculating dynamic costs from DB: %s", error)
            return {
                "grid_cost_eur": 0,
                "savings_eur": 0,
                "feed_in_revenue_eur": 0,
                "weighted_avg_price_ct": 0,
                "consumers": {},
                "hours_without_price": hours_without_price,
            }
        weighted_average_price_ct = (
            round(weighted_price_sum_ct / priced_import_kwh, 2)
            if priced_import_kwh > 0
            else 0
        )
        _LOGGER.debug(
            "Dynamic billing: cost=%.2f EUR, savings=%.2f EUR, avg_price=%.2f ct, hourly_days=%d",
            grid_cost_ct / 100,
            self_savings_ct / 100,
            weighted_average_price_ct,
            len(hourly_dates),
        )
        return {
            "grid_cost_eur": round(grid_cost_ct / 100, 2),
            "savings_eur": round(self_savings_ct / 100, 2),
            "feed_in_revenue_eur": round(export_revenue_ct / 100, 2),
            "weighted_avg_price_ct": weighted_average_price_ct,
            "hours_without_price": hours_without_price,
            "consumers": {
                column_or_consumer_key: {
                    "cost_eur": round(data.get("cost_ct", 0) / 100, 2),
                    "today_cost_eur": round(data.get("today_cost_ct", 0) / 100, 2),
                }
                for column_or_consumer_key, data in consumer_cost_totals.items()
            },
        }

    async def _ensure_hourly_finance_columns(self) -> None:
        """Retained for call compatibility; schema is gated during setup."""

    async def async_reset_baselines(self) -> bool:
        """Compatibility stub - no longer needed. @zara"""
        return True
