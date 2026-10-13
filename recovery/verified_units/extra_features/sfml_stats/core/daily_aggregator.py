# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Daily energy aggregator for SFML Stats. @zara"""
from __future__ import annotations
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any
import aiofiles
from homeassistant.core import HomeAssistant
from ..const import (
    DOMAIN,
    SFML_STATS_DATA,
    DAILY_ENERGY_HISTORY,
    CONF_SENSOR_GRID_IMPORT_EXTRA,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_BILLING_FIXED_PRICE,
    CONF_LEGACY_FIXED_PRICE_CT,
    DEFAULT_BILLING_FIXED_PRICE,
)
from ..sfml_data_reader import SFMLDataReader
from ..utils.time_utils import ha_now, ha_today

_LOGGER = logging.getLogger(__name__)


class DailyEnergyAggregator:
    """Aggregate and store daily energy values. @zara"""

    def __init__(self, hass: HomeAssistant, config_path: Path) -> None:
        """Initialize the aggregator. @zara"""
        self._hass = hass
        self._config_path = config_path
        self._data_path = config_path / SFML_STATS_DATA
        self._history_file = self._data_path / DAILY_ENERGY_HISTORY

    def _get_config(self) -> dict[str, Any]:
        """Get current configuration. @zara"""
        domain_data = self._hass.data.get(DOMAIN, {})
        for entry_id, entry_data in domain_data.items():
            if not isinstance(entry_data, dict):
                continue
            if not "config" in entry_data:
                continue
            return entry_data["config"]
        config_entries = self._hass.config_entries.async_entries(DOMAIN)
        if config_entries:
            return dict(config_entries[0].data)
        return {}

    def _get_sensor_value(self, entity_id: str | None) -> float | None:
        """Read current value from a sensor. @zara"""
        if not entity_id:
            return
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return

    async def _load_history(self) -> dict[str, Any]:
        """Load existing history file. @zara"""
        if not self._history_file.exists():
            return {"days": {}, "last_updated": None}
        try:
            async with aiofiles.open(
                self._history_file, "r", encoding="utf-8"
            ) as history_file:
                history_text = await history_file.read()
                return json.loads(history_text)
        except Exception as error:
            _LOGGER.error("Error loading history: %s", error)
            return {"days": {}, "last_updated": None}
        return None if True else None

    async def _save_history(self, history: dict[str, Any]) -> bool:
        """Save history file. @zara"""
        self._data_path.mkdir(parents=True, exist_ok=True)
        try:
            async with aiofiles.open(
                self._history_file, "w", encoding="utf-8"
            ) as history_file:
                await history_file.write(
                    json.dumps(history, indent=2, ensure_ascii=False)
                )
            if True:
                return True
        except Exception as error:
            _LOGGER.error("Error saving history: %s", error)
            return False

    async def async_aggregate_daily(self) -> bool:
        """Aggregate daily values and save them. @zara"""
        config = self._get_config()
        today_iso = ha_today(self._hass).isoformat()
        _LOGGER.info("Starting daily energy aggregation for %s", today_iso)

        def energy_value(val: float | None) -> float | None:
            if val is None:
                return
            return max(0, val)

        solar_reader = SFMLDataReader(self._hass)
        hourly_yield = await solar_reader.get_daily_yield_from_hourly(today_iso)
        group_yields = {}
        if hourly_yield:
            raw_solar_yield_kwh = hourly_yield.get("yield_total_kwh", 0.0)
            group_yields = hourly_yield.get("groups", {})
            _LOGGER.debug(
                "Solar yield from SFML panel groups: %.4f kWh (%s)",
                raw_solar_yield_kwh,
                group_yields,
            )
        else:
            raw_solar_yield_kwh = await solar_reader.get_live_yield()
            _LOGGER.debug("Solar yield from SFML live SOT: %s kWh", raw_solar_yield_kwh)
        daily_data = {"solar_yield_kwh": energy_value(raw_solar_yield_kwh)}
        await self._merge_from_stats_database(today_iso, daily_data)
        if group_yields:
            daily_data["yield_groups"] = {
                group_name: energy_value(yield_kwh)
                for group_name, yield_kwh in group_yields.items()
            }
        solar_yield_kwh = daily_data.get("solar_yield_kwh")
        solar_to_battery_kwh = daily_data.get("solar_to_battery_kwh")
        daily_data["battery_charge_solar_kwh"] = solar_to_battery_kwh
        daily_data["battery_charge_grid_kwh"] = daily_data.get("grid_to_battery_kwh")
        if daily_data.get("solar_to_house_kwh") is None:
            grid_export_kwh = daily_data.get("grid_export_kwh")
            if (
                solar_yield_kwh is not None
                and solar_to_battery_kwh is not None
                and (grid_export_kwh is not None)
            ):
                daily_data["solar_to_house_kwh"] = max(
                    0, solar_yield_kwh - solar_to_battery_kwh - grid_export_kwh
                )
        daily_data["timestamp"] = ha_now(self._hass).isoformat()
        extra_grid_import_kwh = None
        if not config.get(CONF_SENSOR_GRID_TO_BATTERY):
            extra_grid_import_kwh = energy_value(
                self._get_sensor_value(config.get(CONF_SENSOR_GRID_IMPORT_EXTRA))
            )
        if extra_grid_import_kwh is not None:
            if extra_grid_import_kwh > 0:
                daily_data["grid_import_extra_kwh"] = extra_grid_import_kwh
                _LOGGER.info(
                    "Extra grid import: %.2f kWh (idempotent write to DB)",
                    extra_grid_import_kwh,
                )
                try:
                    from ..storage.db_connection_manager import (
                        DatabaseConnectionManager as DatabaseConnectionManager,
                        get_manager as get_manager,
                    )

                    connection_manager = get_manager()
                    if connection_manager is None:
                        connection_manager = (
                            await DatabaseConnectionManager.get_instance(self._hass)
                        )
                    elif not connection_manager.is_connected:
                        await connection_manager.connect()
                    async with connection_manager.write_transaction() as connection:
                        await connection.execute(
                            "\n                        UPDATE stats_daily_energy\n                        SET grid_import_extra_kwh = ?,\n                            grid_import_kwh = COALESCE(grid_to_house_kwh, 0)\n                                              + COALESCE(grid_to_battery_kwh, 0)\n                                              + ?,\n                            smartmeter_import_kwh = COALESCE(grid_to_house_kwh, 0)\n                                                  + COALESCE(grid_to_battery_kwh, 0)\n                                                  + ?\n                        WHERE date = ?\n                    ",
                            (
                                extra_grid_import_kwh,
                                extra_grid_import_kwh,
                                extra_grid_import_kwh,
                                today_iso,
                            ),
                        )
                    _LOGGER.info(
                        "Extra grid import %.2f kWh written idempotently for %s",
                        extra_grid_import_kwh,
                        today_iso,
                    )
                except Exception as error:
                    _LOGGER.error("Failed to write extra grid import to DB: %s", error)
        from .price_mode import (
            effective_price_mode as effective_price_mode,
            is_fixed_like as is_fixed_like,
        )

        price_mode = effective_price_mode(self._hass, config)
        fixed_price_ct = config.get(
            CONF_LEGACY_FIXED_PRICE_CT,
            config.get(CONF_BILLING_FIXED_PRICE, DEFAULT_BILLING_FIXED_PRICE),
        )
        daily_data["price_mode"] = price_mode
        if is_fixed_like(price_mode):
            daily_data["fixed_price_ct"] = fixed_price_ct
        history = await self._load_history()
        history["days"][today_iso] = daily_data
        history["last_updated"] = ha_now(self._hass).isoformat()
        history_saved = await self._save_history(history)
        if history_saved:
            _LOGGER.info(
                "Daily aggregation saved: Solar=%.2f kWh, Grid import=%.2f kWh, Battery discharged=%.2f kWh",
                solar_yield_kwh if solar_yield_kwh is not None else 0,
                daily_data.get("grid_import_kwh") or 0,
                daily_data.get("battery_to_house_kwh") or 0,
            )
        return history_saved

    async def _merge_from_stats_database(
        self, today_str: str, daily_data: dict[str, Any]
    ) -> None:
        try:
            from ..storage.db_connection_manager import (
                DatabaseConnectionManager as DatabaseConnectionManager,
                get_manager as get_manager,
            )

            connection_manager = get_manager()
            if connection_manager is None:
                connection_manager = await DatabaseConnectionManager.get_instance(
                    self._hass
                )
            elif not connection_manager.is_connected:
                await connection_manager.connect()
            connection = await connection_manager.get_connection()
            async with connection.execute(
                "\n                SELECT grid_import_kwh, grid_export_kwh,\n                       solar_to_house_kwh, solar_to_battery_kwh,\n                       battery_to_house_kwh, grid_to_house_kwh,\n                       grid_to_battery_kwh, home_consumption_kwh,\n                       smartmeter_import_kwh, smartmeter_export_kwh,\n                       autarkie_percent, self_consumption_kwh,\n                       consumer_heatpump_kwh, consumer_heatingrod_kwh,\n                       consumer_wallbox_kwh\n                FROM stats_daily_energy\n                WHERE date = ?\n                ",
                (today_str,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return
            measured_day = dict(row)
            measured_day["grid_import_kwh"] = (
                measured_day.get("smartmeter_import_kwh")
                or measured_day.get("grid_import_kwh")
                or measured_day.get("grid_to_house_kwh")
                or 0
            )
            measured_day["grid_export_kwh"] = (
                measured_day.get("smartmeter_export_kwh")
                or measured_day.get("grid_export_kwh")
                or 0
            )
            daily_data.update(measured_day)
        except Exception as error:
            _LOGGER.warning("Could not read calculated daily Watt values: %s", error)
        return None if True else None

    async def _merge_from_daily_stats(
        self, today_str: str, daily_data: dict[str, Any]
    ) -> None:
        """Merge data from energy_sources_daily_stats.json. @zara"""
        daily_stats_path = self._data_path / "energy_sources_daily_stats.json"
        if not daily_stats_path.exists():
            return
        try:
            async with aiofiles.open(
                daily_stats_path, "r", encoding="utf-8"
            ) as history_file:
                history_text = await history_file.read()
                daily_stats_history = json.loads(history_text)
            day_stats = daily_stats_history.get("days", {}).get(today_str, {})
            if day_stats.get("battery_to_house_kwh") is not None:
                daily_data["battery_to_house_kwh"] = day_stats["battery_to_house_kwh"]
            if day_stats.get("consumption_kwh") is not None:
                daily_data["home_consumption_kwh"] = day_stats["consumption_kwh"]
            if day_stats.get("autarky_percent") is not None:
                daily_data["autarky_percent"] = day_stats["autarky_percent"]
            if day_stats.get("self_consumption_percent") is not None:
                daily_data["self_consumption_percent"] = day_stats[
                    "self_consumption_percent"
                ]
            if day_stats.get("avg_soc") is not None:
                daily_data["avg_soc"] = day_stats["avg_soc"]
            if day_stats.get("min_soc") is not None:
                daily_data["min_soc"] = day_stats["min_soc"]
            if day_stats.get("max_soc") is not None:
                daily_data["max_soc"] = day_stats["max_soc"]
            if day_stats.get("peak_battery_power_w") is not None:
                daily_data["peak_battery_power_w"] = day_stats["peak_battery_power_w"]
        except Exception as error:
            _LOGGER.warning("Error merging from daily stats: %s", error)
        return None if True else None

    async def async_get_billing_period_data(
        self, start_date: date, end_date: date
    ) -> dict[str, Any]:
        """Get aggregated data for a billing period. @zara"""
        history = await self._load_history()
        period_totals = {
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
            "days_with_data": 0,
            "days_in_period": (end_date - start_date).days + 1,
            "total_solar_yield_kwh": 0,
            "total_grid_import_kwh": 0,
            "total_grid_export_kwh": 0,
            "total_battery_charge_solar_kwh": 0,
            "total_battery_charge_grid_kwh": 0,
            "total_solar_to_house_kwh": 0,
            "daily_data": [],
        }
        current_date = start_date
        while current_date <= end_date:
            date_key = current_date.isoformat()
            if date_key in history.get("days", {}):
                day_data = history["days"][date_key]
                period_totals["days_with_data"] += 1
                period_totals["total_solar_yield_kwh"] += (
                    day_data.get("solar_yield_kwh") or 0
                )
                period_totals["total_grid_import_kwh"] += (
                    day_data.get("grid_import_kwh") or 0
                )
                period_totals["total_grid_export_kwh"] += (
                    day_data.get("grid_export_kwh") or 0
                )
                period_totals["total_battery_charge_solar_kwh"] += (
                    day_data.get("battery_charge_solar_kwh") or 0
                )
                period_totals["total_battery_charge_grid_kwh"] += (
                    day_data.get("battery_charge_grid_kwh") or 0
                )
                period_totals["total_solar_to_house_kwh"] += (
                    day_data.get("solar_to_house_kwh") or 0
                )
                period_totals["daily_data"].append({"date": date_key, **day_data})
            current_date = current_date + timedelta(days=1)
        return period_totals
