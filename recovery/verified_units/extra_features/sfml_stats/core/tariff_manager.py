# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Monthly tariff manager for EEG and Energy Sharing support. @zara"""
from __future__ import annotations
import calendar
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator
import aiofiles
import aiosqlite
from homeassistant.core import HomeAssistant
from ..utils.time_utils import ha_now, ha_today
from ..const import (
    DOMAIN,
    SFML_STATS_DATA,
    SOLAR_FORECAST_DB,
    MONTHLY_TARIFFS_FILE,
    CONF_FEED_IN_TARIFF,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_REFERENCE_PRICE,
    CONF_EEG_IMPORT_PRICE,
    CONF_EEG_FEED_IN_PRICE,
    CONF_GRID_FEE_BASE,
    CONF_GRID_FEE_SCALING,
    DEFAULT_FEED_IN_TARIFF,
    DEFAULT_BILLING_FIXED_PRICE,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_REFERENCE_PRICE,
    DEFAULT_EEG_IMPORT_PRICE,
    DEFAULT_EEG_FEED_IN_PRICE,
    DEFAULT_GRID_FEE_BASE,
    DEFAULT_GRID_FEE_SCALING,
    GRID_FEE_THRESHOLD_HIGH,
    GRID_FEE_THRESHOLD_MEDIUM,
    GRID_FEE_THRESHOLD_LOW,
    GRID_FEE_FACTOR_HIGH,
    GRID_FEE_FACTOR_MEDIUM,
    GRID_FEE_FACTOR_LOW,
    GRID_FEE_FACTOR_VERY_LOW,
)

_LOGGER = logging.getLogger(__name__)


class MonthlyTariffManager:
    """Manage monthly tariffs with smart defaults and manual overrides. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        config_path: Path,
        entry_data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the tariff manager. @zara"""
        self._hass = hass
        self._config_path = config_path
        self._entry_data = entry_data
        self._data_path = config_path / SFML_STATS_DATA
        self._tariff_file = self._data_path / MONTHLY_TARIFFS_FILE
        self._db_path = config_path / SOLAR_FORECAST_DB
        self._cache = None

    def update_config(self, new_config: dict[str, Any]) -> None:
        """Update cached configuration. @zara"""
        self._entry_data = new_config
        _LOGGER.debug("MonthlyTariffManager config updated")

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

    @staticmethod
    def _positive_float(value: Any, fallback: float = 0.0) -> float:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            numeric_value = fallback
        return max(0.0, numeric_value)

    def _configured_fixed_price_ct(self, config: dict[str, Any]) -> float:
        from ..const import CONF_LEGACY_FIXED_PRICE_CT as CONF_LEGACY_FIXED_PRICE_CT

        legacy_price_ct = config.get(CONF_LEGACY_FIXED_PRICE_CT)
        if legacy_price_ct not in (None, ""):
            return self._positive_float(legacy_price_ct, 0.0)
        if CONF_BILLING_WORK_PRICE in config or CONF_BILLING_GRID_FEES in config:
            work_price_ct = self._positive_float(
                config.get(CONF_BILLING_WORK_PRICE), DEFAULT_BILLING_WORK_PRICE
            )
            grid_fees_ct = self._positive_float(
                config.get(CONF_BILLING_GRID_FEES), DEFAULT_BILLING_GRID_FEES
            )
            return work_price_ct + grid_fees_ct
        return self._positive_float(
            config.get(CONF_BILLING_FIXED_PRICE), DEFAULT_BILLING_FIXED_PRICE
        )

    def _get_defaults(self) -> dict[str, Any]:
        """Get default tariff values from configuration. @zara"""
        config = self._get_config()
        return {
            "reference_price_ct": config.get(
                CONF_REFERENCE_PRICE, DEFAULT_REFERENCE_PRICE
            ),
            "feed_in_tariff_ct": config.get(
                CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF
            ),
            "fixed_price_ct": self._configured_fixed_price_ct(config),
            "eeg_import_price_ct": config.get(
                CONF_EEG_IMPORT_PRICE, DEFAULT_EEG_IMPORT_PRICE
            ),
            "eeg_feed_in_ct": config.get(
                CONF_EEG_FEED_IN_PRICE, DEFAULT_EEG_FEED_IN_PRICE
            ),
            "grid_fee_base_ct": config.get(CONF_GRID_FEE_BASE, DEFAULT_GRID_FEE_BASE),
            "grid_fee_scaling_enabled": config.get(
                CONF_GRID_FEE_SCALING, DEFAULT_GRID_FEE_SCALING
            ),
        }

    async def _load_data(self) -> dict[str, Any]:
        """Load tariff data from file. @zara"""
        if self._cache is not None:
            return self._cache
        if not self._tariff_file.exists():
            self._cache = {"version": 1, "defaults": self._get_defaults(), "months": {}}
            return self._cache
        try:
            async with aiofiles.open(
                self._tariff_file, "r", encoding="utf-8"
            ) as tariff_file:
                json_text = await tariff_file.read()
                self._cache = json.loads(json_text)
                return self._cache
        except Exception as error:
            _LOGGER.error("Error loading tariff data: %s", error)
            self._cache = {"version": 1, "defaults": self._get_defaults(), "months": {}}
            return self._cache
        return None if True else None

    async def _save_data(self, data: dict[str, Any]) -> bool:
        """Save tariff data to file. @zara"""
        try:
            self._data_path.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(
                self._tariff_file, "w", encoding="utf-8"
            ) as tariff_file:
                await tariff_file.write(json.dumps(data, indent=2, ensure_ascii=False))
            self._cache = data
        except Exception as error:
            _LOGGER.error("Error saving tariff data: %s", error)
            return False
        return True

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
            get_manager as get_manager,
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

    def _get_month_key(self, year: int, month: int) -> str:
        """Generate month key in YYYY-MM format. @zara"""
        return f"{year:04d}-{month:02d}"

    def _calculate_grid_fee(self, annual_import_kwh: float) -> float:
        """Calculate scaled grid fee based on annual consumption. @zara"""
        defaults = self._get_defaults()
        base_grid_fee_ct = defaults["grid_fee_base_ct"]
        if not defaults["grid_fee_scaling_enabled"]:
            return base_grid_fee_ct
        if annual_import_kwh > GRID_FEE_THRESHOLD_HIGH:
            return base_grid_fee_ct * GRID_FEE_FACTOR_HIGH
        if annual_import_kwh > GRID_FEE_THRESHOLD_MEDIUM:
            return base_grid_fee_ct * GRID_FEE_FACTOR_MEDIUM
        if annual_import_kwh > GRID_FEE_THRESHOLD_LOW:
            return base_grid_fee_ct * GRID_FEE_FACTOR_LOW
        return base_grid_fee_ct * GRID_FEE_FACTOR_VERY_LOW

    def _estimate_eeg_share(
        self, weighted_price: float, standard_price: float, eeg_price: float
    ) -> float:
        """Estimate EEG share percentage from price difference. @zara"""
        if standard_price <= eeg_price:
            return 0.0
        eeg_share_percent = (
            (standard_price - weighted_price) / (standard_price - eeg_price) * 100
        )
        return max(0.0, min(100.0, eeg_share_percent))

    def _usable_price(self, value: Any) -> float | None:
        if value is None or value == "":
            return
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return
        if numeric_value == 0.0:
            return
        return numeric_value

    async def calculate_weighted_average_price(
        self, year: int, month: int
    ) -> dict[str, float]:
        """Calculate consumption-weighted average price from stats_hourly_billing. @zara"""
        weighted_price_sum_ct = 0.0
        priced_import_kwh = 0.0
        total_import_kwh = 0.0
        total_export_kwh = 0.0
        total_own_supply_kwh = 0.0
        priced_hours = 0
        price_sum_ct = 0.0
        if self._db_path.exists():
            month_start_str = f"{year:04d}-{month:02d}-01"
            month_end = (
                f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
            )
            try:
                async with self._get_db() as connection:
                    async with connection.execute(
                        "\n                        SELECT grid_import_kwh, grid_export_kwh, price_ct_kwh,\n                               solar_to_house_kwh, battery_to_house_kwh,\n                               grid_to_battery_kwh\n                        FROM stats_hourly_billing\n                        WHERE date >= ? AND date <= ?\n                        ",
                        (month_start_str, month_end),
                    ) as cursor:
                        rows = await cursor.fetchall()
            except Exception as error:
                _LOGGER.error(
                    "Error loading hourly billing for %s-%02d: %s", year, month, error
                )
                rows = []
            for row in rows:
                if isinstance(row, dict) or hasattr(row, "keys"):
                    import_kwh = float(row["grid_import_kwh"] or 0)
                    export_kwh = float(row["grid_export_kwh"] or 0)
                    price_ct = self._usable_price(row["price_ct_kwh"])
                    solar_to_house_kwh = float(row["solar_to_house_kwh"] or 0)
                    battery_to_house_kwh = float(row["battery_to_house_kwh"] or 0)
                    grid_to_battery_kwh = float(row["grid_to_battery_kwh"] or 0)
                else:
                    import_kwh = float(row[0] or 0)
                    export_kwh = float(row[1] or 0)
                    price_ct = self._usable_price(row[2])
                    solar_to_house_kwh = float(row[3] or 0)
                    battery_to_house_kwh = float(row[4] or 0)
                    grid_to_battery_kwh = float(row[5] or 0)
                total_import_kwh += import_kwh
                total_export_kwh += export_kwh
                total_own_supply_kwh += solar_to_house_kwh + max(
                    0.0, battery_to_house_kwh - grid_to_battery_kwh
                )
                if price_ct is None:
                    continue
                price_sum_ct += price_ct
                priced_hours += 1
                priced_import_kwh += import_kwh
                weighted_price_sum_ct += import_kwh * price_ct
                continue
        if priced_import_kwh > 0:
            weighted_average_price_ct = weighted_price_sum_ct / priced_import_kwh
        elif priced_hours > 0:
            weighted_average_price_ct = price_sum_ct / priced_hours
        else:
            weighted_average_price_ct = self._get_defaults()["fixed_price_ct"]
        return {
            "weighted_avg_price_ct": round(weighted_average_price_ct, 2),
            "import_kwh": round(total_import_kwh, 2),
            "export_kwh": round(total_export_kwh, 2),
            "self_consumption_kwh": round(total_own_supply_kwh, 2),
            "hours_with_data": priced_hours,
        }

    async def get_monthly_data(self, year: int, month: int) -> dict[str, Any]:
        """Get complete monthly data with auto-calculated and override values. @zara"""
        month_key = self._get_month_key(year, month)
        tariff_data = await self._load_data()
        defaults = self._get_defaults()
        calculated = await self.calculate_weighted_average_price(year, month)
        eeg_share_percent = None
        if calculated["weighted_avg_price_ct"] > 0:
            eeg_share_percent = self._estimate_eeg_share(
                calculated["weighted_avg_price_ct"],
                defaults["reference_price_ct"],
                defaults["eeg_import_price_ct"],
            )
        month_import_kwh = calculated["import_kwh"]
        months_elapsed = month - 1 + (12 if year < ha_today(self._hass).year else 0) + 1
        if months_elapsed > 0 and month_import_kwh > 0:
            estimated_annual_import_kwh = month_import_kwh / months_elapsed * 12
        else:
            estimated_annual_import_kwh = 3000
        scaled_grid_fee_ct = self._calculate_grid_fee(estimated_annual_import_kwh)
        month_data = tariff_data.get("months", {}).get(month_key, {})
        overrides = month_data.get("overrides", {})
        is_finalized = month_data.get("is_finalized", False)
        finalized_at = month_data.get("finalized_at")

        def get_effective(key: str, auto_value: float, default_value: float) -> dict:
            if key in overrides and overrides[key] is not None:
                return {"value": overrides[key], "source": "manual"}
            if auto_value is not None and auto_value > 0:
                return {"value": auto_value, "source": "auto"}
            return {"value": default_value, "source": "default"}

        monthly_payload = {
            "month_key": month_key,
            "year": year,
            "month": month,
            "is_finalized": is_finalized,
            "finalized_at": finalized_at,
            "auto_calculated": {
                "import_kwh": calculated["import_kwh"],
                "export_kwh": calculated["export_kwh"],
                "self_consumption_kwh": calculated["self_consumption_kwh"],
                "weighted_avg_price_ct": calculated["weighted_avg_price_ct"],
                "eeg_share_percent": (
                    round(eeg_share_percent, 1) if eeg_share_percent else None
                ),
                "estimated_grid_fee_ct": round(scaled_grid_fee_ct, 2),
                "hours_with_data": calculated["hours_with_data"],
            },
            "overrides": overrides,
            "effective": {
                "import_price_ct": get_effective(
                    "import_price_ct",
                    calculated["weighted_avg_price_ct"],
                    defaults["fixed_price_ct"],
                ),
                "export_price_ct": get_effective(
                    "export_price_ct", None, defaults["feed_in_tariff_ct"]
                ),
                "reference_price_ct": get_effective(
                    "reference_price_ct", None, defaults["reference_price_ct"]
                ),
                "grid_fee_ct": get_effective(
                    "grid_fee_ct", scaled_grid_fee_ct, defaults["grid_fee_base_ct"]
                ),
                "eeg_share_percent": get_effective(
                    "eeg_share_percent", eeg_share_percent, 0.0
                ),
            },
            "defaults": defaults,
        }
        return monthly_payload

    async def get_monthly_fallbacks(
        self,
        year: int,
        month: int,
        *,
        import_price_ct: float | None = None,
        feed_in_tariff_ct: float | None = None,
        base_fee_eur: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return stable fallback tariffs for a month."""
        month_key = self._get_month_key(year, month)
        tariff_data = await self._load_data()
        defaults = self._get_defaults()
        months = tariff_data.setdefault("months", {})
        month_data = months.setdefault(month_key, {"overrides": {}})
        overrides = month_data.get("overrides", {})
        fixed_fallbacks = month_data.setdefault("fixed_fallbacks", {})
        default_import_price_ct = self._positive_float(
            import_price_ct, defaults["fixed_price_ct"]
        )
        default_feed_in_tariff_ct = self._positive_float(
            feed_in_tariff_ct, defaults["feed_in_tariff_ct"]
        )
        default_base_fee_eur = self._positive_float(base_fee_eur, 0.0)
        fallbacks_changed = False
        for key, value in (
            ("import_price_ct", default_import_price_ct),
            ("feed_in_tariff_ct", default_feed_in_tariff_ct),
            ("base_fee_eur", default_base_fee_eur),
        ):
            if fixed_fallbacks.get(key) is None:
                fixed_fallbacks[key] = value
                fallbacks_changed = True
        if fallbacks_changed:
            month_data["fixed_fallbacks"] = fixed_fallbacks
            month_data["fixed_at"] = (
                month_data.get("fixed_at") or ha_now(self._hass).isoformat()
            )
            await self._save_data(tariff_data)

        def value_for(key: str, fallback: float) -> dict[str, Any]:
            if key in overrides and overrides[key] is not None:
                return {"value": overrides[key], "source": "manual"}
            return {
                "value": self._positive_float(fixed_fallbacks.get(key), fallback),
                "source": "fixed_month",
            }

        return {
            "import_price_ct": value_for("import_price_ct", default_import_price_ct),
            "feed_in_tariff_ct": value_for(
                "feed_in_tariff_ct", default_feed_in_tariff_ct
            ),
            "base_fee_eur": value_for("base_fee_eur", default_base_fee_eur),
        }

    async def set_monthly_override(
        self, year: int, month: int, overrides: dict[str, float | None]
    ) -> bool:
        """Set manual override values for a month. @zara"""
        month_key = self._get_month_key(year, month)
        tariff_data = await self._load_data()
        if "months" not in tariff_data:
            tariff_data["months"] = {}
        if month_key not in tariff_data["months"]:
            tariff_data["months"][month_key] = {"overrides": {}, "is_finalized": False}
        resolved_overrides = tariff_data["months"][month_key].get("overrides", {})
        for key, value in overrides.items():
            if value is None:
                resolved_overrides.pop(key, None)
            else:
                resolved_overrides[key] = value
        tariff_data["months"][month_key]["overrides"] = resolved_overrides
        tariff_data["months"][month_key]["updated_at"] = ha_now(self._hass).isoformat()
        return await self._save_data(tariff_data)

    async def finalize_month(
        self, year: int, month: int, recalculate_history: bool = True
    ) -> dict[str, Any]:
        """Mark a month as finalized after billing. @zara"""
        del recalculate_history
        month_key = self._get_month_key(year, month)
        tariff_data = await self._load_data()
        if "months" not in tariff_data:
            tariff_data["months"] = {}
        if month_key not in tariff_data["months"]:
            tariff_data["months"][month_key] = {"overrides": {}}
        tariff_data["months"][month_key]["is_finalized"] = True
        tariff_data["months"][month_key]["finalized_at"] = ha_now(
            self._hass
        ).isoformat()
        await self._save_data(tariff_data)
        return {
            "success": True,
            "month_key": month_key,
            "finalized_at": tariff_data["months"][month_key]["finalized_at"],
            "recalculated": False,
        }

    async def unfinalize_month(self, year: int, month: int) -> bool:
        """Remove finalization status from a month. @zara"""
        month_key = self._get_month_key(year, month)
        tariff_data = await self._load_data()
        if month_key in tariff_data.get("months", {}):
            tariff_data["months"][month_key]["is_finalized"] = False
            tariff_data["months"][month_key].pop("finalized_at", None)
            return await self._save_data(tariff_data)
        return True

    async def get_all_months(
        self, year: int | None = None, include_empty: bool = False
    ) -> list[dict[str, Any]]:
        """Get data for all months, optionally filtered by year. @zara"""
        if year is None:
            year = ha_today(self._hass).year
        month_summaries = []
        for month_number in range(1, 13):
            if year == ha_today(self._hass).year and (
                not (not month_number > ha_today(self._hass).month or include_empty)
            ):
                continue
            month_data = await self.get_monthly_data(year, month_number)
            month_summaries.append(month_data)
        return month_summaries

    async def get_year_summary(self, year: int) -> dict[str, Any]:
        """Get yearly summary with totals and averages. @zara"""
        month_summaries = await self.get_all_months(year)
        total_import_kwh = 0.0
        total_export_kwh = 0.0
        total_own_supply_kwh = 0.0
        grid_cost_eur = 0.0
        feed_in_revenue_eur = 0.0
        savings_eur = 0.0
        finalized_months = 0
        price_sum_ct = 0.0
        months_with_price = 0
        for month_data in month_summaries:
            auto_values = month_data["auto_calculated"]
            effective_values = month_data["effective"]
            total_import_kwh += auto_values["import_kwh"]
            total_export_kwh += auto_values["export_kwh"]
            total_own_supply_kwh += auto_values["self_consumption_kwh"]
            import_price_ct = effective_values["import_price_ct"]["value"]
            export_price_ct = effective_values["export_price_ct"]["value"]
            reference_price_ct = effective_values["reference_price_ct"]["value"]
            grid_cost_eur += auto_values["import_kwh"] * import_price_ct / 100
            feed_in_revenue_eur += auto_values["export_kwh"] * export_price_ct / 100
            savings_eur += (
                auto_values["self_consumption_kwh"] * reference_price_ct / 100
            )
            if month_data["is_finalized"]:
                finalized_months += 1
            if import_price_ct > 0:
                price_sum_ct += import_price_ct
                months_with_price += 1
        average_import_price_ct = (
            price_sum_ct / months_with_price if months_with_price > 0 else 0
        )
        return {
            "year": year,
            "months_with_data": len(
                [
                    month_data
                    for month_data in month_summaries
                    if month_data["auto_calculated"]["hours_with_data"] > 0
                ]
            ),
            "finalized_months": finalized_months,
            "totals": {
                "import_kwh": round(total_import_kwh, 2),
                "export_kwh": round(total_export_kwh, 2),
                "self_consumption_kwh": round(total_own_supply_kwh, 2),
                "grid_cost_eur": round(grid_cost_eur, 2),
                "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
                "savings_eur": round(savings_eur, 2),
                "net_benefit_eur": round(
                    savings_eur + feed_in_revenue_eur - grid_cost_eur, 2
                ),
            },
            "averages": {"import_price_ct": round(average_import_price_ct, 2)},
            "months": month_summaries,
        }

    async def export_csv(
        self, start_year: int, start_month: int, end_year: int, end_month: int
    ) -> str:
        """Export monthly data as CSV. @zara"""
        csv_lines = [
            "Monat;Bezug (kWh);Bezugspreis (ct/kWh);Quelle Bezugspreis;Einspeisung (kWh);Vergütung (ct/kWh);Quelle Vergütung;Eigenverbrauch (kWh);Referenzpreis (ct/kWh);Netzgebühren (ct/kWh);EEG-Anteil (%);Stromkosten (EUR);Einspeise-Erlös (EUR);Einsparung (EUR);Status"
        ]
        current_month = date(start_year, start_month, 1)
        month_end = date(end_year, end_month, 1)
        while current_month <= month_end:
            month_data = await self.get_monthly_data(
                current_month.year, current_month.month
            )
            auto_values = month_data["auto_calculated"]
            effective_values = month_data["effective"]
            import_price_ct = effective_values["import_price_ct"]["value"]
            export_price_ct = effective_values["export_price_ct"]["value"]
            reference_price_ct = effective_values["reference_price_ct"]["value"]
            grid_fee_ct = effective_values["grid_fee_ct"]["value"]
            eeg_share_percent = effective_values["eeg_share_percent"]["value"]
            grid_cost_eur = auto_values["import_kwh"] * import_price_ct / 100
            feed_in_revenue_eur = auto_values["export_kwh"] * export_price_ct / 100
            savings_eur = auto_values["self_consumption_kwh"] * reference_price_ct / 100
            status_text = "Finalisiert" if month_data["is_finalized"] else "Offen"
            csv_row = f"{month_data['month_key']};{auto_values['import_kwh']:.2f};{import_price_ct:.2f};{effective_values['import_price_ct']['source']};{auto_values['export_kwh']:.2f};{export_price_ct:.2f};{effective_values['export_price_ct']['source']};{auto_values['self_consumption_kwh']:.2f};{reference_price_ct:.2f};{grid_fee_ct:.2f};{eeg_share_percent:.1f};{grid_cost_eur:.2f};{feed_in_revenue_eur:.2f};{savings_eur:.2f};{status_text}"
            csv_lines.append(csv_row)
            if current_month.month == 12:
                current_month = date(current_month.year + 1, 1, 1)
            else:
                current_month = date(current_month.year, current_month.month + 1, 1)
        return "\n".join(csv_lines)

    async def update_defaults(self, defaults: dict[str, Any]) -> bool:
        """Update default tariff values. @zara"""
        tariff_data = await self._load_data()
        tariff_data["defaults"] = {**tariff_data.get("defaults", {}), **defaults}
        return await self._save_data(tariff_data)

    def invalidate_cache(self) -> None:
        """Invalidate the data cache. @zara"""
        self._cache = None
