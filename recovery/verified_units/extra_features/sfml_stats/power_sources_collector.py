# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Power Sources Data Collector for SFML Stats. @zara"""
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from .storage.db_connection_manager import DatabaseConnectionManager
from .utils.time_utils import as_ha_local, ha_now, ha_today
from .core.power_integration import (
    integrate_power_from_recorder,
    integrate_power_states,
    integrate_signed_power_states,
    power_values_at_timestamps,
    power_states_from_recorder,
    power_state_w,
    signed_power_state_w,
)
from .core.energy_efficiency import (
    EFFICIENCY_CALCULATION_VERSION,
    aggregate_efficiency_rows,
)
from .sfml_data_reader import SFMLDataReader
from .core.consumer_atlas import integrate_power_to_kwh
from .core.grid_split import (
    SPLIT_TOLERANCE_W,
    displayed_grid_import_kwh,
    reconcile_grid_split,
    split_tolerance_kwh,
)
from .const import (
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_INVERTER_AC_OUTPUT,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_HP_PV_ENERGY_DAILY,
    CONF_SENSOR_WALLBOX_POWER,
)

_POWER_KEY_TO_DB_COLUMN: dict[str, str] = {
    CONF_SENSOR_SOLAR_TO_HOUSE: "solar_to_house_kwh",
    CONF_SENSOR_SOLAR_TO_BATTERY: "solar_to_battery_kwh",
    CONF_SENSOR_BATTERY_TO_HOUSE: "battery_to_house_kwh",
    CONF_SENSOR_GRID_TO_HOUSE: "grid_to_house_kwh",
    CONF_SENSOR_GRID_TO_BATTERY: "grid_to_battery_kwh",
    CONF_SENSOR_HOME_CONSUMPTION: "home_consumption_kwh",
    CONF_SENSOR_SMARTMETER_IMPORT: "smartmeter_import_kwh",
    CONF_SENSOR_SMARTMETER_EXPORT: "smartmeter_export_kwh",
    CONF_SENSOR_HEATPUMP_POWER: "consumer_heatpump_kwh",
    CONF_SENSOR_HEATINGROD_POWER: "consumer_heatingrod_kwh",
    CONF_SENSOR_WALLBOX_POWER: "consumer_wallbox_kwh",
}
_LOGGER = logging.getLogger(__name__)
COLLECTION_INTERVAL = 60
LIVE_MIN_INTERVAL = 5
MAX_LIVE_INTEGRATION_GAP_SECONDS = 300
MAX_DATA_AGE_DAYS = 7


class PowerSourcesCollector:
    """Collects power sources data periodically and stores in SFML DB. @zara"""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], data_path: Path
    ) -> None:
        """Initialize the collector. @zara"""
        self.hass = hass
        self.config = config
        self.data_path = data_path
        self._db_path = (
            Path(hass.config.path()) / "solar_forecast_ml" / "solar_forecast.db"
        )
        self._task = None
        self._efficiency_backfill_task = None
        self._running = False
        self._live_columns_ensured = False
        self._power_source_columns_ensured = False
        self._efficiency_schema_ensured = False
        self._consumer_atlas_tables_ensured = False
        self._last_live_write = 0
        self._live_unsub = []
        self._sfml_reader = SFMLDataReader(hass)
        self._last_collection_at = None
        self._last_collection_date = None

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get DB connection via manager with direct fallback. @zara"""
        from .storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        db_manager = get_manager()
        if db_manager is not None:
            if db_manager.is_connected:
                yield (await db_manager.get_connection())
                return
        async with (DatabaseConnectionManager.connect_direct_safe if True else None)(
            self._db_path
        ) as direct_connection:
            yield direct_connection
        if True:
            return

    async def _get_db_manager(self) -> DatabaseConnectionManager:
        from .storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
            get_manager as get_manager,
        )

        db_manager = get_manager()
        if db_manager is None:
            db_manager = await (
                DatabaseConnectionManager.get_instance if True else None
            )(self.hass)
        elif not db_manager.is_connected:
            await db_manager.connect()
        return db_manager if True else None

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        db_manager = await self._get_db_manager()
        async with db_manager.write_transaction() as connection:
            yield connection
        if True:
            return

    @property
    def is_db_available(self) -> bool:
        """Check if SFML database is available. @zara"""
        return self._db_path.exists()

    @staticmethod
    def _is_locked_error(err: BaseException) -> bool:
        error_message = str(err).lower()
        return (
            "database is locked" in error_message or "database is busy" in error_message
        )

    async def start(self) -> None:
        """Start the data collection task. @zara"""
        if self._running:
            return
        if not self.is_db_available:
            _LOGGER.warning(
                "SFML database not found at %s - Power Sources Collector disabled",
                self._db_path,
            )
            return
        self._running = True
        async with self._write_transaction() as connection:
            await self._ensure_power_source_columns(connection)
            await self._ensure_db_columns(connection)
            await self._ensure_efficiency_schema(connection)
        startup_utc = datetime.now(timezone.utc)
        await self._rebuild_today_from_recorder(startup_utc)
        self._last_collection_at = startup_utc
        self._last_collection_date = (
            as_ha_local(startup_utc, self.hass).date().isoformat()
        )
        self._task = asyncio.create_task(self._collection_loop())
        self._schedule_efficiency_backfill()
        self._setup_live_listeners()
        _LOGGER.info("Power Sources Collector started (using SFML database)")

    async def stop(self) -> None:
        """Stop the data collection task. @zara"""
        self._running = False
        for unsubscribe in self._live_unsub:
            unsubscribe()
        self._live_unsub.clear()
        if self._efficiency_backfill_task:
            self._efficiency_backfill_task.cancel()
            try:
                await self._efficiency_backfill_task
            except asyncio.CancelledError:
                pass
            self._efficiency_backfill_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("Power Sources Collector stopped")

    def update_config(self, config: dict[str, Any]) -> None:
        previous_inverter_entity = self.config.get(CONF_SENSOR_INVERTER_AC_OUTPUT)
        previous_battery_entity = self.config.get(CONF_SENSOR_BATTERY_POWER)
        self.config = config
        if previous_inverter_entity != config.get(
            CONF_SENSOR_INVERTER_AC_OUTPUT
        ) or previous_battery_entity != config.get(CONF_SENSOR_BATTERY_POWER):
            self._setup_live_listeners()
            if not config.get(CONF_SENSOR_INVERTER_AC_OUTPUT):
                if self._efficiency_backfill_task:
                    self._efficiency_backfill_task.cancel()
                return
            self._schedule_efficiency_backfill(force=True)
        return None if True else None

    def _schedule_efficiency_backfill(self, *, force: bool = False) -> None:
        if not self._running or not self.config.get(CONF_SENSOR_INVERTER_AC_OUTPUT):
            return None
        if self._efficiency_backfill_task and (
            not self._efficiency_backfill_task.done()
        ):
            if not force:
                return
            previous_backfill_task = self._efficiency_backfill_task
            previous_backfill_task.cancel()
            self._efficiency_backfill_task = asyncio.create_task(
                self._restart_efficiency_backfill(previous_backfill_task)
            )
            return
        self._efficiency_backfill_task = asyncio.create_task(
            self._backfill_efficiency_history(force=force)
        )

    async def _restart_efficiency_backfill(self, previous_task: asyncio.Task) -> None:
        try:
            await previous_task
        except asyncio.CancelledError:
            pass
        if self._running:
            await self._backfill_efficiency_history(force=True)
        return None if True else None

    async def _collection_loop(self) -> None:
        """Main collection loop. @zara"""
        while self._running:
            try:
                await self._collect_data()
            except Exception as error:
                _LOGGER.error("Error collecting power sources data: %s", error)
            await asyncio.sleep(COLLECTION_INTERVAL)

    def _setup_live_listeners(self) -> None:
        """Register state change listeners for non-SFML flow sensors. @zara"""
        from homeassistant.helpers.event import (
            async_track_state_change_event as async_track_state_change_event,
        )

        for unsubscribe in self._live_unsub:
            unsubscribe()
        self._live_unsub.clear()
        tracked_entity_ids = []
        for config_key in (CONF_SENSOR_SOLAR_TO_BATTERY,):
            sensor_entity_id = self.config.get(config_key)
            if sensor_entity_id:
                tracked_entity_ids.append(sensor_entity_id)
        if not tracked_entity_ids:
            _LOGGER.debug(
                "No non-SFML live flow sensors configured - periodic tracking only"
            )
            return
        unsubscribe = async_track_state_change_event(
            self.hass, tracked_entity_ids, self._on_live_sensor_change
        )
        self._live_unsub.append(unsubscribe)
        _LOGGER.info(
            "Live flow tracking active for %d sensors", len(tracked_entity_ids)
        )

    async def _on_live_sensor_change(self, event) -> None:
        """Handle sensor state change - write to sensor_power_live. @zara"""
        import time as time_module

        monotonic_now = time_module.monotonic()
        if monotonic_now - self._last_live_write < LIVE_MIN_INTERVAL:
            return
        self._last_live_write = monotonic_now
        try:
            await self._write_live_power()
        except Exception as error:
            _LOGGER.error("Error writing live power data: %s", error)
        return None if True else None

    async def _write_live_power(self) -> None:
        """Write current power values to sensor_power_live. @zara"""
        local_now = ha_now(self.hass)
        solar_power_w = await self._sfml_reader.get_live_power() or 0
        solar_to_battery_w = self._get_sensor_value(CONF_SENSOR_SOLAR_TO_BATTERY) or 0
        panel_groups = await self._sfml_reader.get_live_panel_groups()
        panel_powers = [panel_group.get("power") for panel_group in panel_groups[:4]]
        while len(panel_powers) < 4:
            panel_powers.append(None)
        panel1_power_w, panel2_power_w, panel3_power_w, panel4_power_w = panel_powers
        async with self._write_transaction() as connection:
            if not self._live_columns_ensured:
                await self._ensure_power_live_columns(connection)
                self._live_columns_ensured = True
            await connection.execute(
                "\n                INSERT INTO sensor_power_live (\n                    timestamp, power_watt, solar_to_battery_watt,\n                    panel1_watt, panel2_watt, panel3_watt, panel4_watt\n                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n            ",
                (
                    local_now.strftime("%Y-%m-%d %H:%M:%S"),
                    solar_power_w,
                    solar_to_battery_w,
                    panel1_power_w,
                    panel2_power_w,
                    panel3_power_w,
                    panel4_power_w,
                ),
            )
            import random as random_module

            if random_module.random() < 0.01:
                cutoff_timestamp = (
                    local_now - timedelta(days=MAX_DATA_AGE_DAYS)
                ).strftime("%Y-%m-%d %H:%M:%S")
                await connection.execute(
                    "DELETE FROM sensor_power_live WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
        return None if True else None

    async def _collect_data(self) -> None:
        """Collect current power values and store in SFML database. @zara"""
        now_utc = datetime.now(timezone.utc)
        local_now = as_ha_local(now_utc, self.hass)
        solar_power_w = await self._sfml_reader.get_live_power()
        inverter_ac_output_w = self._get_power_sensor_value(
            CONF_SENSOR_INVERTER_AC_OUTPUT
        )
        battery_power_dc_w = self._get_signed_power_sensor_value(
            CONF_SENSOR_BATTERY_POWER
        )
        solar_to_house_w = self._get_power_sensor_value(CONF_SENSOR_SOLAR_TO_HOUSE)
        solar_to_battery_w = self._get_power_sensor_value(CONF_SENSOR_SOLAR_TO_BATTERY)
        battery_to_house_w = self._get_power_sensor_value(CONF_SENSOR_BATTERY_TO_HOUSE)
        grid_to_house_w = self._get_power_sensor_value(CONF_SENSOR_GRID_TO_HOUSE)
        grid_to_battery_w = self._get_power_sensor_value(CONF_SENSOR_GRID_TO_BATTERY)
        home_consumption_w = self._get_power_sensor_value(CONF_SENSOR_HOME_CONSUMPTION)
        solar_to_grid_w = self._get_power_sensor_value(CONF_SENSOR_HOUSE_TO_GRID)
        battery_soc = self._get_sensor_value(CONF_SENSOR_BATTERY_SOC)
        smartmeter_import_w = self._get_power_sensor_value(
            CONF_SENSOR_SMARTMETER_IMPORT
        )
        smartmeter_export_w = self._get_power_sensor_value(
            CONF_SENSOR_SMARTMETER_EXPORT
        )
        from .core.eai_wp_sources import (
            resolved_heatpump_entity as resolved_heatpump_entity,
        )

        heatpump_entity_id = resolved_heatpump_entity(
            self.hass, self.config, CONF_SENSOR_HEATPUMP_POWER
        )
        heatpump_power_w = (
            self._get_entity_power_value(heatpump_entity_id)
            if heatpump_entity_id
            else None
        )
        heatingrod_power_w = self._get_power_sensor_value(CONF_SENSOR_HEATINGROD_POWER)
        wallbox_power_w = self._get_power_sensor_value(CONF_SENSOR_WALLBOX_POWER)
        if solar_power_w is not None and solar_power_w < 0:
            solar_power_w = 0.0
        if solar_to_house_w is not None and solar_to_house_w < 0:
            solar_to_house_w = 0.0
        if solar_to_battery_w is not None and solar_to_battery_w < 0:
            solar_to_battery_w = 0.0
        if solar_to_grid_w is not None and solar_to_grid_w < 0:
            solar_to_grid_w = 0.0
        if solar_to_house_w is None and solar_power_w is not None:
            solar_to_battery_fallback_w = solar_to_battery_w or 0
            solar_to_house_w = max(0, solar_power_w - solar_to_battery_fallback_w)
            _LOGGER.debug(
                "Calculated solar_to_house: %.1f W (solar %.1f - battery %.1f)",
                solar_to_house_w,
                solar_power_w,
                solar_to_battery_fallback_w,
            )
        if grid_to_house_w is None and home_consumption_w is not None:
            solar_to_house_fallback_w = solar_to_house_w or 0
            battery_to_house_fallback_w = battery_to_house_w or 0
            grid_to_house_w = max(
                0,
                home_consumption_w
                - solar_to_house_fallback_w
                - battery_to_house_fallback_w,
            )
            _LOGGER.debug(
                "Calculated grid_to_house: %.1f W (consumption %.1f - solar %.1f - battery %.1f)",
                grid_to_house_w,
                home_consumption_w,
                solar_to_house_fallback_w,
                battery_to_house_fallback_w,
            )
        if smartmeter_import_w is not None:
            grid_to_house_w, grid_to_battery_w = reconcile_grid_split(
                smartmeter_import_w,
                grid_to_house_w,
                grid_to_battery_w,
                tolerance=SPLIT_TOLERANCE_W,
            )
        elif grid_to_house_w is not None:
            grid_to_battery_fallback_w = grid_to_battery_w or 0
            smartmeter_import_w = grid_to_house_w + grid_to_battery_fallback_w
            _LOGGER.debug("Calculated smartmeter_import: %.1f W", smartmeter_import_w)
        if solar_to_grid_w is None and solar_power_w is not None:
            solar_to_house_fallback_w = solar_to_house_w or 0
            solar_to_battery_fallback_w = solar_to_battery_w or 0
            solar_to_grid_w = max(
                0,
                solar_power_w - solar_to_house_fallback_w - solar_to_battery_fallback_w,
            )
            _LOGGER.debug("Calculated house_to_grid: %.1f W", solar_to_grid_w)
        if smartmeter_export_w is None:
            smartmeter_export_w = solar_to_grid_w
        if smartmeter_export_w is None and solar_power_w is not None:
            solar_to_house_fallback_w = solar_to_house_w or 0
            solar_to_battery_fallback_w = solar_to_battery_w or 0
            smartmeter_export_w = max(
                0,
                solar_power_w - solar_to_house_fallback_w - solar_to_battery_fallback_w,
            )
            _LOGGER.debug("Calculated smartmeter_export: %.1f W", smartmeter_export_w)
        try:
            async with self._write_transaction() as connection:
                await self._ensure_power_source_columns(connection)
                await connection.execute(
                    "\n                    INSERT OR REPLACE INTO stats_power_sources (\n                        timestamp, date, hour,\n                        solar_power_w, inverter_ac_output_w, battery_power_dc_w,\n                        house_consumption_w,\n                        solar_to_house_w, solar_to_battery_w,\n                        solar_to_grid_w, battery_to_house_w,\n                        grid_to_house_w, grid_to_battery_w, battery_soc\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ",
                    (
                        now_utc.isoformat(),
                        local_now.strftime("%Y-%m-%d"),
                        local_now.hour,
                        solar_power_w or 0,
                        inverter_ac_output_w,
                        battery_power_dc_w,
                        home_consumption_w or 0,
                        solar_to_house_w or 0,
                        solar_to_battery_w or 0,
                        solar_to_grid_w or 0,
                        battery_to_house_w or 0,
                        grid_to_house_w or 0,
                        grid_to_battery_w or 0,
                        battery_soc,
                    ),
                )
                cutoff_timestamp = (
                    now_utc - timedelta(days=MAX_DATA_AGE_DAYS)
                ).isoformat()
                await connection.execute(
                    "DELETE FROM stats_power_sources WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
        except Exception as error:
            if self._is_locked_error(error):
                _LOGGER.debug(
                    "Power sources data write skipped after DB lock retries: %s", error
                )
            else:
                _LOGGER.error("Error inserting power sources data into DB: %s", error)
        try:
            await self._write_live_power()
        except Exception as error:
            if self._is_locked_error(error):
                _LOGGER.debug(
                    "SFML live power snapshot skipped after DB lock retries: %s", error
                )
            else:
                _LOGGER.error("Error writing SFML live power snapshot: %s", error)
        _LOGGER.debug(
            "Collected power data: solar_power=%.1f, solar_to_house=%.1f, solar_to_battery=%.1f, battery=%.1f, grid=%.1f, consumption=%.1f, soc=%s",
            solar_power_w or 0,
            solar_to_house_w or 0,
            solar_to_battery_w or 0,
            battery_to_house_w or 0,
            grid_to_house_w or 0,
            home_consumption_w or 0,
            battery_soc,
        )
        data_point = {
            "solar_power": solar_power_w,
            "inverter_ac_output": inverter_ac_output_w,
            "battery_power_dc": battery_power_dc_w,
            "solar_to_house": solar_to_house_w,
            "solar_to_battery": solar_to_battery_w,
            "house_to_grid": solar_to_grid_w,
            "battery_to_house": battery_to_house_w,
            "grid_to_house": grid_to_house_w,
            "grid_to_battery": grid_to_battery_w,
            "home_consumption": home_consumption_w,
            "battery_soc": battery_soc,
            "smartmeter_import": smartmeter_import_w,
            "smartmeter_export": smartmeter_export_w,
            "heatpump_w": heatpump_power_w,
            "heatingrod_w": heatingrod_power_w,
            "wallbox_w": wallbox_power_w,
        }
        await self._update_daily_stats(now_utc, data_point)
        await self._update_consumer_atlas(now_utc)

    def _get_sensor_value(self, config_key: str) -> float | None:
        """Get sensor value from Home Assistant. @zara"""
        sensor_entity_id = self.config.get(config_key)
        if not sensor_entity_id:
            return
        sensor_state = self.hass.states.get(sensor_entity_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return
        try:
            return float(sensor_state.state)
        except (ValueError, TypeError):
            return

    def _get_power_sensor_value(self, config_key: str) -> float | None:
        sensor_entity_id = self.config.get(config_key)
        if not sensor_entity_id:
            return
        sensor_state = self.hass.states.get(sensor_entity_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return None
        return power_state_w(sensor_state)

    def _get_signed_power_sensor_value(self, config_key: str) -> float | None:
        sensor_entity_id = self.config.get(config_key)
        if not sensor_entity_id:
            return
        sensor_state = self.hass.states.get(sensor_entity_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return None
        return signed_power_state_w(sensor_state)

    def _hp_pv_reading_kwh(self) -> float | None:
        from .core.eai_wp_sources import (
            resolved_heatpump_entity as resolved_heatpump_entity,
        )
        from .core.heatpump_today import energy_reading_kwh as energy_reading_kwh

        sensor_entity_id = resolved_heatpump_entity(
            self.hass, self.config, CONF_SENSOR_HP_PV_ENERGY_DAILY
        )
        if not sensor_entity_id or self.hass is None:
            return None
        sensor_state = self.hass.states.get(sensor_entity_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return None
        sensor_attributes = getattr(sensor_state, "attributes", None) or {}
        return energy_reading_kwh(
            sensor_state.state, sensor_attributes.get("unit_of_measurement")
        )

    async def _store_hp_pv_daily(
        self, db: aiosqlite.Connection, today_str: str
    ) -> None:
        """Store today's PV-to-heat-pump meter beside the integrated heat-pump total."""
        from .core.heatpump_today import measured_hp_pv_kwh as measured_hp_pv_kwh

        async with db.execute(
            "SELECT consumer_heatpump_kwh FROM stats_daily_energy WHERE date = ?",
            (today_str,),
        ) as cursor:
            row = await cursor.fetchone()
        heatpump_kwh = None if row is None else row[0]
        heatpump_pv_kwh = measured_hp_pv_kwh(self._hp_pv_reading_kwh(), heatpump_kwh)
        await db.execute(
            "UPDATE stats_daily_energy SET consumer_hp_pv_kwh = ? WHERE date = ?",
            (heatpump_pv_kwh, today_str),
        )

    def _get_entity_power_value(self, entity_id: str) -> float | None:
        sensor_state = self.hass.states.get(entity_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return None
        return power_state_w(sensor_state)

    async def _ensure_consumer_atlas_tables(self, db: aiosqlite.Connection) -> None:
        """Retained for call compatibility; schema is gated during setup."""
        self._consumer_atlas_tables_ensured = True

    async def _update_consumer_atlas(self, now: datetime) -> None:
        local_now = as_ha_local(now, self.hass)
        date_str = local_now.strftime("%Y-%m-%d")
        time_str = local_now.strftime("%H:%M")
        interval_seconds = COLLECTION_INTERVAL
        try:
            async with self._write_transaction() as connection:
                await self._ensure_consumer_atlas_tables(connection)
                async with connection.execute(
                    "SELECT consumer_id, entity_id\n                       FROM stats_consumer_atlas_config\n                       WHERE enabled = 1\n                       ORDER BY sort_order, name"
                ) as cursor:
                    consumer_rows = await cursor.fetchall()
                if not consumer_rows:
                    return
                for consumer_row in consumer_rows:
                    consumer_power_w = self._get_entity_power_value(
                        consumer_row["entity_id"]
                    )
                    if consumer_power_w is None:
                        continue
                    consumer_kwh = integrate_power_to_kwh(
                        consumer_power_w, interval_seconds
                    )
                    await connection.execute(
                        "INSERT INTO stats_consumer_atlas_daily (\n                               date, consumer_id, kwh, peak_w, peak_time,\n                               last_power_w, samples, updated_at\n                           ) VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)\n                           ON CONFLICT(date, consumer_id) DO UPDATE SET\n                               kwh = kwh + excluded.kwh,\n                               peak_w = MAX(peak_w, excluded.peak_w),\n                               peak_time = CASE\n                                   WHEN excluded.peak_w > peak_w THEN excluded.peak_time\n                                   ELSE peak_time\n                               END,\n                               last_power_w = excluded.last_power_w,\n                               samples = samples + 1,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            date_str,
                            consumer_row["consumer_id"],
                            consumer_kwh,
                            consumer_power_w,
                            time_str,
                            consumer_power_w,
                        ),
                    )
        except Exception as error:
            if self._is_locked_error(error):
                _LOGGER.debug(
                    "Consumer atlas update skipped after DB lock retries: %s", error
                )
            else:
                _LOGGER.error("Error updating consumer atlas in DB: %s", error)
        if True:
            return

    async def get_history(self, hours: int = 24) -> list[dict[str, Any]]:
        """Get historical data for the specified number of hours from SFML database. @zara"""
        if not self.is_db_available:
            return []
        cutoff_timestamp = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        try:
            async with self._get_db() as connection:
                await self._ensure_power_source_columns(connection)
                async with connection.execute(
                    "\n                    SELECT timestamp, date, hour,\n                           solar_power_w, inverter_ac_output_w,\n                           battery_power_dc_w, house_consumption_w,\n                           solar_to_house_w, solar_to_battery_w,\n                           solar_to_grid_w, battery_to_house_w,\n                           grid_to_house_w, grid_to_battery_w, battery_soc\n                    FROM stats_power_sources\n                    WHERE timestamp > ?\n                    ORDER BY timestamp ASC\n                ",
                    (cutoff_timestamp,),
                ) as cursor:
                    rows = await cursor.fetchall()
            history = []
            for row in rows:
                history.append(
                    {
                        "timestamp": row["timestamp"],
                        "solar_power": row["solar_power_w"],
                        "inverter_ac_output": row["inverter_ac_output_w"],
                        "battery_power_dc": row["battery_power_dc_w"],
                        "solar_to_house": row["solar_to_house_w"],
                        "solar_to_battery": row["solar_to_battery_w"],
                        "house_to_grid": row["solar_to_grid_w"],
                        "battery_to_house": row["battery_to_house_w"],
                        "grid_to_house": row["grid_to_house_w"],
                        "grid_to_battery": row["grid_to_battery_w"],
                        "home_consumption": row["house_consumption_w"],
                        "battery_soc": row["battery_soc"],
                    }
                )
            return history
        except Exception as error:
            _LOGGER.error("Error reading power sources history from DB: %s", error)
            return []

    async def _ensure_power_source_columns(self, db: aiosqlite.Connection) -> None:
        """Retained for call compatibility; schema is gated during setup."""
        self._power_source_columns_ensured = True

    async def _ensure_power_live_columns(self, db: aiosqlite.Connection) -> None:
        """Ensure panel columns exist in sensor_power_live table. @zara"""
        async with db.execute("PRAGMA table_info(sensor_power_live)") as cursor:
            rows = await cursor.fetchall()
            existing_columns = {row[1] for row in rows}
        for column_name in ("panel1_watt", "panel2_watt", "panel3_watt", "panel4_watt"):
            if column_name not in existing_columns:
                try:
                    await db.execute(
                        f"ALTER TABLE sensor_power_live ADD COLUMN {column_name} REAL"
                    )
                    _LOGGER.info("Added column %s to sensor_power_live", column_name)
                except Exception:
                    continue

    async def _ensure_db_columns(self, db: aiosqlite.Connection) -> None:
        """Retained for call compatibility; schema is gated during setup."""

    async def _ensure_efficiency_schema(self, db: aiosqlite.Connection) -> None:
        """Retained for call compatibility; schema is gated during setup."""
        self._efficiency_schema_ensured = True

    async def _update_efficiency_live(
        self,
        db: aiosqlite.Connection,
        *,
        date_str: str,
        seconds: float,
        data_point: dict[str, Any],
    ) -> None:
        ac_output_w = data_point.get("inverter_ac_output")
        battery_dc_w = data_point.get("battery_power_dc")
        ac_output_kwh = (
            max(0.0, ac_output_w) * seconds / 3600000.0
            if ac_output_w is not None
            else None
        )
        battery_charge_dc_kwh = (
            max(0.0, battery_dc_w) * seconds / 3600000.0
            if battery_dc_w is not None
            else None
        )
        battery_discharge_dc_kwh = (
            max(0.0, -battery_dc_w) * seconds / 3600000.0
            if battery_dc_w is not None
            else None
        )
        if ac_output_kwh is not None:
            await db.execute(
                "UPDATE stats_daily_energy\n                   SET inverter_ac_output_kwh =\n                       COALESCE(inverter_ac_output_kwh, 0) + ?\n                   WHERE date = ?",
                (ac_output_kwh, date_str),
            )
        if battery_charge_dc_kwh is not None:
            await db.execute(
                "UPDATE stats_daily_energy\n                   SET battery_charge_dc_kwh =\n                           COALESCE(battery_charge_dc_kwh, 0) + ?,\n                       battery_discharge_dc_kwh =\n                           COALESCE(battery_discharge_dc_kwh, 0) + ?\n                   WHERE date = ?",
                (battery_charge_dc_kwh, battery_discharge_dc_kwh, date_str),
            )
        await self._ensure_efficiency_schema(db)
        await self._recalculate_efficiency_day(
            db, date_str=date_str, source="live_hourly"
        )

    async def _backfill_efficiency_history(self, *, force: bool = False) -> None:
        inverter_entity_id = self.config.get(CONF_SENSOR_INVERTER_AC_OUTPUT)
        battery_entity_id = self.config.get(CONF_SENSOR_BATTERY_POWER)
        if not inverter_entity_id:
            return
        sensor_signature = f"v{EFFICIENCY_CALCULATION_VERSION}|{inverter_entity_id}|{battery_entity_id or ''}"
        try:
            async with self._write_transaction() as connection:
                await self._ensure_power_source_columns(connection)
                await self._ensure_db_columns(connection)
                await self._ensure_efficiency_schema(connection)
                async with connection.execute(
                    "SELECT value FROM stats_energy_efficiency_meta\n                       WHERE key = 'backfill_sensor_signature'"
                ) as cursor:
                    row = await cursor.fetchone()
                if row and row[0] == sensor_signature:
                    if not force:
                        return
            keep_days = 10
            try:
                from homeassistant.components.recorder import (
                    get_instance as get_recorder_instance,
                )

                recorder = get_recorder_instance(self.hass)
                keep_days = int(getattr(recorder, "keep_days", keep_days))
            except (ImportError, RuntimeError, TypeError, ValueError):
                pass
            keep_days = min(370, max(1, keep_days))
            local_now = ha_now(self.hass)
            day_start_local = (local_now - timedelta(days=keep_days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            has_history = False
            backfilled_days = 0
            while day_start_local.date() <= local_now.date():
                if not self._running:
                    return
                next_day_local = (day_start_local + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                range_end_local = min(next_day_local, local_now)
                start_utc = day_start_local.astimezone(timezone.utc)
                period_end_utc = range_end_local.astimezone(timezone.utc)
                ac_states, battery_states = await asyncio.gather(
                    power_states_from_recorder(
                        self.hass, inverter_entity_id, start_utc, period_end_utc
                    ),
                    power_states_from_recorder(
                        self.hass, battery_entity_id, start_utc, period_end_utc
                    ),
                )
                if ac_states:
                    has_history = True
                    await self._backfill_efficiency_day(
                        date_str=day_start_local.date().isoformat(),
                        start_utc=start_utc,
                        end_utc=period_end_utc,
                        ac_states=ac_states,
                        battery_states=battery_states,
                    )
                    backfilled_days += 1
                day_start_local = next_day_local
                await asyncio.sleep(0)
            if has_history:
                async with self._write_transaction() as connection:
                    await self._ensure_efficiency_schema(connection)
                    await connection.execute(
                        "INSERT INTO stats_energy_efficiency_meta (\n                               key, value, updated_at\n                           ) VALUES ('backfill_sensor_signature', ?,\n                                     CURRENT_TIMESTAMP)\n                           ON CONFLICT(key) DO UPDATE SET\n                               value = excluded.value,\n                               updated_at = CURRENT_TIMESTAMP",
                        (sensor_signature,),
                    )
                _LOGGER.info(
                    "Energy efficiency recorder backfill completed for %d days",
                    backfilled_days,
                )
            else:
                _LOGGER.info(
                    "No recorder history available for inverter AC sensor %s",
                    inverter_entity_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error("Energy efficiency recorder backfill failed: %s", error)
        return None if True else None

    async def _backfill_efficiency_day(
        self,
        *,
        date_str: str,
        start_utc: datetime,
        end_utc: datetime,
        ac_states: list[Any],
        battery_states: list[Any],
    ) -> None:
        ac_output_kwh = integrate_power_states(ac_states, start_utc, end_utc)
        battery_energy_split = integrate_signed_power_states(
            battery_states, start_utc, end_utc
        )
        battery_charge_dc_kwh = (
            battery_energy_split[0] if battery_energy_split else None
        )
        battery_discharge_dc_kwh = (
            battery_energy_split[1] if battery_energy_split else None
        )
        async with self._write_transaction() as connection:
            await self._ensure_power_source_columns(connection)
            await self._ensure_db_columns(connection)
            await self._ensure_efficiency_schema(connection)
            await connection.execute(
                "INSERT OR IGNORE INTO stats_daily_energy (date) VALUES (?)",
                (date_str,),
            )
            await connection.execute(
                "UPDATE stats_daily_energy SET\n                       inverter_ac_output_kwh = ?,\n                       battery_charge_dc_kwh = ?,\n                       battery_discharge_dc_kwh = ?\n                   WHERE date = ?",
                (
                    ac_output_kwh,
                    battery_charge_dc_kwh,
                    battery_discharge_dc_kwh,
                    date_str,
                ),
            )
            async with connection.execute(
                "SELECT timestamp\n                   FROM stats_power_sources\n                   WHERE date = ?\n                   ORDER BY timestamp",
                (date_str,),
            ) as cursor:
                timestamp_rows = await cursor.fetchall()
            timestamp_strings = [row[0] for row in timestamp_rows]
            if timestamp_strings:
                sample_timestamps = [
                    datetime.fromisoformat(timestamp_string.replace("Z", "+00:00"))
                    for timestamp_string in timestamp_strings
                ]
                ac_values = power_values_at_timestamps(ac_states, sample_timestamps)
                battery_values = power_values_at_timestamps(
                    battery_states, sample_timestamps, signed=True
                )
                await connection.executemany(
                    "UPDATE stats_power_sources SET\n                           inverter_ac_output_w = ?,\n                           battery_power_dc_w = ?\n                       WHERE timestamp = ?",
                    [
                        (ac_value_w, battery_value_w, resolved_timestamp_string)
                        for resolved_timestamp_string, ac_value_w, battery_value_w in zip(
                            timestamp_strings, ac_values, battery_values
                        )
                    ],
                )
            await self._recalculate_efficiency_day(
                connection,
                date_str=date_str,
                source="recorder_aligned" if timestamp_strings else "recorder_daily",
            )
        return None if True else None

    async def _recalculate_efficiency_day(
        self, db: aiosqlite.Connection, *, date_str: str, source: str
    ) -> None:
        async with db.execute(
            "SELECT timestamp, solar_power_w, inverter_ac_output_w,\n                      battery_power_dc_w, grid_to_battery_w,\n                      solar_to_battery_w\n               FROM stats_power_sources\n               WHERE date = ?\n               ORDER BY timestamp",
            (date_str,),
        ) as cursor:
            rows = await cursor.fetchall()
        efficiency = aggregate_efficiency_rows(rows)
        efficiency_values = efficiency.as_dict()
        async with db.execute(
            "SELECT solar_yield_kwh, inverter_ac_output_kwh,\n                      battery_charge_dc_kwh, battery_discharge_dc_kwh\n               FROM stats_daily_energy\n               WHERE date = ?",
            (date_str,),
        ) as cursor:
            daily_energy_row = await cursor.fetchone()
        if daily_energy_row is None:
            return
        await db.execute(
            "INSERT INTO stats_energy_efficiency_daily (\n                   date, solar_dc_input_kwh, inverter_ac_output_kwh,\n                   battery_charge_dc_kwh, battery_discharge_dc_kwh,\n                   pv_dc_sample_kwh, pv_ac_sample_kwh,\n                   battery_dc_sample_kwh, battery_ac_sample_kwh,\n                   mixed_dc_sample_kwh, mixed_ac_sample_kwh,\n                   system_input_sample_kwh, system_output_sample_kwh,\n                   inverter_efficiency_percent,\n                   battery_discharge_efficiency_percent,\n                   mixed_efficiency_percent,\n                   system_efficiency_percent,\n                   ac_measurement_seconds, battery_measurement_seconds,\n                   pv_sample_seconds, battery_sample_seconds,\n                   mixed_sample_seconds, system_sample_seconds,\n                   source, updated_at\n               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n                         ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n               ON CONFLICT(date) DO UPDATE SET\n                   solar_dc_input_kwh = excluded.solar_dc_input_kwh,\n                   inverter_ac_output_kwh = excluded.inverter_ac_output_kwh,\n                   battery_charge_dc_kwh = excluded.battery_charge_dc_kwh,\n                   battery_discharge_dc_kwh = excluded.battery_discharge_dc_kwh,\n                   pv_dc_sample_kwh = excluded.pv_dc_sample_kwh,\n                   pv_ac_sample_kwh = excluded.pv_ac_sample_kwh,\n                   battery_dc_sample_kwh = excluded.battery_dc_sample_kwh,\n                   battery_ac_sample_kwh = excluded.battery_ac_sample_kwh,\n                   mixed_dc_sample_kwh = excluded.mixed_dc_sample_kwh,\n                   mixed_ac_sample_kwh = excluded.mixed_ac_sample_kwh,\n                   system_input_sample_kwh = excluded.system_input_sample_kwh,\n                   system_output_sample_kwh = excluded.system_output_sample_kwh,\n                   inverter_efficiency_percent =\n                       excluded.inverter_efficiency_percent,\n                   battery_discharge_efficiency_percent =\n                       excluded.battery_discharge_efficiency_percent,\n                   mixed_efficiency_percent = excluded.mixed_efficiency_percent,\n                   system_efficiency_percent =\n                       excluded.system_efficiency_percent,\n                   ac_measurement_seconds = excluded.ac_measurement_seconds,\n                   battery_measurement_seconds =\n                       excluded.battery_measurement_seconds,\n                   pv_sample_seconds = excluded.pv_sample_seconds,\n                   battery_sample_seconds = excluded.battery_sample_seconds,\n                   mixed_sample_seconds = excluded.mixed_sample_seconds,\n                   system_sample_seconds = excluded.system_sample_seconds,\n                   source = excluded.source,\n                   updated_at = CURRENT_TIMESTAMP",
            (
                date_str,
                daily_energy_row[0],
                daily_energy_row[1],
                daily_energy_row[2],
                daily_energy_row[3],
                efficiency_values["pv_dc_sample_kwh"],
                efficiency_values["pv_ac_sample_kwh"],
                efficiency_values["battery_dc_sample_kwh"],
                efficiency_values["battery_ac_sample_kwh"],
                efficiency_values["mixed_dc_sample_kwh"],
                efficiency_values["mixed_ac_sample_kwh"],
                efficiency_values["system_input_sample_kwh"],
                efficiency_values["system_output_sample_kwh"],
                efficiency_values["inverter_efficiency_percent"],
                efficiency_values["battery_discharge_efficiency_percent"],
                efficiency_values["mixed_efficiency_percent"],
                efficiency_values["system_efficiency_percent"],
                efficiency_values["ac_measurement_seconds"],
                efficiency_values["battery_measurement_seconds"],
                efficiency_values["pv_sample_seconds"],
                efficiency_values["battery_sample_seconds"],
                efficiency_values["mixed_sample_seconds"],
                efficiency_values["system_sample_seconds"],
                source,
            ),
        )

    async def _update_daily_stats(
        self, now: datetime, data_point: dict[str, Any]
    ) -> None:
        """Update daily statistics in stats_daily_energy table. @zara"""
        if not self.is_db_available:
            return
        local_now = as_ha_local(now, self.hass)
        date_str = local_now.strftime("%Y-%m-%d")
        if self._last_collection_date != date_str or self._last_collection_at is None:
            await self._rebuild_today_from_recorder(now)
            self._last_collection_at = now
            self._last_collection_date = date_str
            return
        elapsed_seconds = max(0.0, (now - self._last_collection_at).total_seconds())
        if elapsed_seconds > MAX_LIVE_INTEGRATION_GAP_SECONDS:
            await self._rebuild_today_from_recorder(now)
            self._last_collection_at = now
            return
        self._last_collection_at = now
        if elapsed_seconds <= 0:
            return
        elapsed_hours = elapsed_seconds / 3600.0
        solar_to_house_kwh = (
            (data_point.get("solar_to_house") or 0) * elapsed_hours / 1000
        )
        solar_to_battery_kwh = (
            (data_point.get("solar_to_battery") or 0) * elapsed_hours / 1000
        )
        battery_to_house_kwh = (
            (data_point.get("battery_to_house") or 0) * elapsed_hours / 1000
        )
        grid_to_house_kwh = (
            (data_point.get("grid_to_house") or 0) * elapsed_hours / 1000
        )
        grid_to_battery_kwh = (
            (data_point.get("grid_to_battery") or 0) * elapsed_hours / 1000
        )
        home_consumption_kwh = (
            (data_point.get("home_consumption") or 0) * elapsed_hours / 1000
        )
        smartmeter_import_kwh = (
            (data_point.get("smartmeter_import") or 0) * elapsed_hours / 1000
        )
        smartmeter_export_kwh = (
            (data_point.get("smartmeter_export") or 0) * elapsed_hours / 1000
        )
        integrated_solar_kwh = (
            solar_to_house_kwh + solar_to_battery_kwh + smartmeter_export_kwh
        )
        heatpump_kwh = (data_point.get("heatpump_w") or 0) * elapsed_hours / 1000
        heatingrod_kwh = (data_point.get("heatingrod_w") or 0) * elapsed_hours / 1000
        wallbox_kwh = (data_point.get("wallbox_w") or 0) * elapsed_hours / 1000
        solar_power_w = data_point.get("solar_power") or 0
        try:
            live_yield_kwh = await self._sfml_reader.get_live_yield()
            async with self._write_transaction() as connection:
                await self._ensure_db_columns(connection)
                async with connection.execute(
                    "SELECT * FROM stats_daily_energy WHERE date = ?", (date_str,)
                ) as cursor:
                    existing_daily_row = await cursor.fetchone()
                if existing_daily_row:
                    await connection.execute(
                        "\n                        UPDATE stats_daily_energy SET\n                            solar_yield_kwh = solar_yield_kwh + ?,\n                            solar_to_house_kwh = solar_to_house_kwh + ?,\n                            solar_to_battery_kwh = solar_to_battery_kwh + ?,\n                            battery_to_house_kwh = battery_to_house_kwh + ?,\n                            grid_to_house_kwh = grid_to_house_kwh + ?,\n                            grid_to_battery_kwh = COALESCE(grid_to_battery_kwh, 0) + ?,\n                            home_consumption_kwh = home_consumption_kwh + ?,\n                            grid_import_kwh = COALESCE(grid_import_kwh, 0) + ?,\n                            grid_export_kwh = COALESCE(grid_export_kwh, 0) + ?,\n                            smartmeter_import_kwh = COALESCE(smartmeter_import_kwh, 0) + ?,\n                            smartmeter_export_kwh = COALESCE(smartmeter_export_kwh, 0) + ?,\n                            consumer_heatpump_kwh = COALESCE(consumer_heatpump_kwh, 0) + ?,\n                            consumer_heatingrod_kwh = COALESCE(consumer_heatingrod_kwh, 0) + ?,\n                            consumer_wallbox_kwh = COALESCE(consumer_wallbox_kwh, 0) + ?,\n                            peak_solar_w = MAX(peak_solar_w, ?),\n                            peak_solar_time = CASE WHEN ? > peak_solar_w THEN ? ELSE peak_solar_time END\n                        WHERE date = ?\n                    ",
                        (
                            integrated_solar_kwh,
                            solar_to_house_kwh,
                            solar_to_battery_kwh,
                            battery_to_house_kwh,
                            grid_to_house_kwh,
                            grid_to_battery_kwh,
                            home_consumption_kwh,
                            smartmeter_import_kwh,
                            smartmeter_export_kwh,
                            smartmeter_import_kwh,
                            smartmeter_export_kwh,
                            heatpump_kwh,
                            heatingrod_kwh,
                            wallbox_kwh,
                            solar_power_w,
                            solar_power_w,
                            local_now.strftime("%H:%M"),
                            date_str,
                        ),
                    )
                else:
                    await connection.execute(
                        "\n                        INSERT INTO stats_daily_energy (\n                            date, solar_yield_kwh, solar_to_house_kwh, solar_to_battery_kwh,\n                            battery_to_house_kwh, grid_to_house_kwh, grid_to_battery_kwh,\n                            home_consumption_kwh, smartmeter_import_kwh, smartmeter_export_kwh,\n                            grid_import_kwh, grid_export_kwh,\n                            consumer_heatpump_kwh, consumer_heatingrod_kwh, consumer_wallbox_kwh,\n                            peak_solar_w, peak_solar_time\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ",
                        (
                            date_str,
                            integrated_solar_kwh,
                            solar_to_house_kwh,
                            solar_to_battery_kwh,
                            battery_to_house_kwh,
                            grid_to_house_kwh,
                            grid_to_battery_kwh,
                            home_consumption_kwh,
                            smartmeter_import_kwh,
                            smartmeter_export_kwh,
                            smartmeter_import_kwh,
                            smartmeter_export_kwh,
                            heatpump_kwh,
                            heatingrod_kwh,
                            wallbox_kwh,
                            solar_power_w,
                            local_now.strftime("%H:%M"),
                        ),
                    )
                if live_yield_kwh is not None:
                    await connection.execute(
                        "\n                        UPDATE stats_daily_energy SET\n                            solar_yield_kwh = ?,\n                            autarkie_percent = CASE\n                                WHEN home_consumption_kwh > 0\n                                THEN MIN(100, MAX(0, (solar_to_house_kwh + MAX(0, battery_to_house_kwh - grid_to_battery_kwh)) / home_consumption_kwh * 100))\n                                ELSE 0\n                            END,\n                            self_consumption_kwh = MIN(?, solar_to_house_kwh + solar_to_battery_kwh)\n                        WHERE date = ?\n                    ",
                        (round(live_yield_kwh, 4), round(live_yield_kwh, 4), date_str),
                    )
                else:
                    await connection.execute(
                        "\n                        UPDATE stats_daily_energy SET\n                            solar_yield_kwh = solar_to_house_kwh + solar_to_battery_kwh + COALESCE(smartmeter_export_kwh, 0),\n                            autarkie_percent = CASE\n                                WHEN home_consumption_kwh > 0\n                                THEN MIN(100, MAX(0, (solar_to_house_kwh + MAX(0, battery_to_house_kwh - grid_to_battery_kwh)) / home_consumption_kwh * 100))\n                                ELSE 0\n                            END,\n                            self_consumption_kwh = solar_to_house_kwh + solar_to_battery_kwh\n                        WHERE date = ?\n                    ",
                        (date_str,),
                    )
                await self._store_hp_pv_daily(connection, date_str)
                await self._update_efficiency_live(
                    connection,
                    date_str=date_str,
                    seconds=elapsed_seconds,
                    data_point=data_point,
                )
        except Exception as error:
            _LOGGER.error("Error updating daily stats in DB: %s", error)
        return None if True else None

    async def _rebuild_today_from_recorder(self, now: datetime) -> None:
        local_now = as_ha_local(now, self.hass)
        day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = day_start_local.astimezone(timezone.utc)
        period_end_utc = now.astimezone(timezone.utc)
        power_config_keys = list(_POWER_KEY_TO_DB_COLUMN)
        integrated_values = await asyncio.gather(
            *(
                integrate_power_from_recorder(
                    self.hass, self.config.get(config_key), start_utc, period_end_utc
                )
                for config_key in power_config_keys
            )
        )
        reconstructed_values = {
            _POWER_KEY_TO_DB_COLUMN[config_key]: value
            for config_key, value in zip(power_config_keys, integrated_values)
            if value is not None
        }
        ac_states, battery_states = await asyncio.gather(
            power_states_from_recorder(
                self.hass,
                self.config.get(CONF_SENSOR_INVERTER_AC_OUTPUT),
                start_utc,
                period_end_utc,
            ),
            power_states_from_recorder(
                self.hass,
                self.config.get(CONF_SENSOR_BATTERY_POWER),
                start_utc,
                period_end_utc,
            ),
        )
        ac_output_kwh = integrate_power_states(ac_states, start_utc, period_end_utc)
        if ac_output_kwh is not None:
            reconstructed_values["inverter_ac_output_kwh"] = ac_output_kwh
        battery_energy_split = integrate_signed_power_states(
            battery_states, start_utc, period_end_utc
        )
        if battery_energy_split is not None:
            reconstructed_values["battery_charge_dc_kwh"] = battery_energy_split[0]
            reconstructed_values["battery_discharge_dc_kwh"] = battery_energy_split[1]
        if not reconstructed_values:
            _LOGGER.info("No Watt recorder history available for daily reconstruction")
            return
        if "smartmeter_import_kwh" in reconstructed_values:
            duration_hours = max(
                (period_end_utc - start_utc).total_seconds() / 3600.0, 0.0
            )
            reconciled_grid_house_kwh, reconciled_grid_battery_kwh = (
                reconcile_grid_split(
                    reconstructed_values["smartmeter_import_kwh"],
                    reconstructed_values.get("grid_to_house_kwh", 0.0),
                    reconstructed_values.get("grid_to_battery_kwh", 0.0),
                    tolerance=split_tolerance_kwh(duration_hours),
                )
            )
            if "grid_to_house_kwh" in reconstructed_values:
                reconstructed_values["grid_to_house_kwh"] = reconciled_grid_house_kwh
            if "grid_to_battery_kwh" in reconstructed_values:
                reconstructed_values["grid_to_battery_kwh"] = (
                    reconciled_grid_battery_kwh
                )
        column_aliases = {
            "solar_to_battery_kwh": "battery_charge_solar_kwh",
            "grid_to_battery_kwh": "battery_charge_grid_kwh",
            "smartmeter_import_kwh": "grid_import_kwh",
            "smartmeter_export_kwh": "grid_export_kwh",
        }
        for source_column, target_column in column_aliases.items():
            if source_column in reconstructed_values:
                reconstructed_values[target_column] = reconstructed_values[
                    source_column
                ]
        date_str = local_now.date().isoformat()
        update_clause = ", ".join(
            (f"{column_name} = ?" for column_name in reconstructed_values)
        )
        update_params = [
            round(max(0.0, value), 6) for value in reconstructed_values.values()
        ]
        update_params.append(date_str)
        try:
            async with self._write_transaction() as connection:
                await self._ensure_db_columns(connection)
                await connection.execute(
                    "INSERT OR IGNORE INTO stats_daily_energy (date) VALUES (?)",
                    (date_str,),
                )
                await connection.execute(
                    f"UPDATE stats_daily_energy SET {update_clause} WHERE date = ?",
                    update_params,
                )
                await connection.execute(
                    "\n                    UPDATE stats_daily_energy SET\n                        autarkie_percent = CASE\n                            WHEN home_consumption_kwh > 0\n                            THEN MIN(100, MAX(0,\n                                (solar_to_house_kwh\n                                 + MAX(0, battery_to_house_kwh - COALESCE(grid_to_battery_kwh, 0)))\n                                / home_consumption_kwh * 100))\n                            ELSE 0\n                        END,\n                        self_consumption_kwh = solar_to_house_kwh + solar_to_battery_kwh\n                    WHERE date = ?\n                    ",
                    (date_str,),
                )
                await self._store_hp_pv_daily(connection, date_str)
                await self._recalculate_efficiency_day(
                    connection, date_str=date_str, source="recorder_today"
                )
            _LOGGER.info(
                "Rebuilt %d daily energy values from Watt recorder history for %s",
                len(reconstructed_values),
                date_str,
            )
        except Exception as error:
            _LOGGER.error("Daily Watt reconstruction failed: %s", error)
        return None if True else None

    async def get_daily_stats(self, days: int = 7) -> dict[str, Any]:
        """Get daily statistics for the specified number of days from SFML database. @zara"""
        if not self.is_db_available:
            return {"version": 1, "last_updated": None, "days": {}}
        cutoff_timestamp = (ha_today(self.hass) - timedelta(days=days)).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT date, solar_yield_kwh, solar_to_house_kwh, solar_to_battery_kwh,\n                           battery_to_house_kwh, battery_charge_solar_kwh, battery_charge_grid_kwh,\n                           grid_to_house_kwh, grid_to_battery_kwh,\n                           grid_export_kwh, smartmeter_import_kwh, smartmeter_export_kwh,\n                           home_consumption_kwh,\n                           autarkie_percent, self_consumption_kwh, peak_solar_w, peak_solar_time\n                    FROM stats_daily_energy\n                    WHERE date >= ?\n                    ORDER BY date DESC\n                ",
                    (cutoff_timestamp,),
                ) as cursor:
                    rows = await cursor.fetchall()
            daily_stats = {}
            for row in rows:
                solar_to_house_kwh = row["solar_to_house_kwh"] or 0
                solar_to_battery_kwh = row["solar_to_battery_kwh"] or 0
                solar_yield_kwh = row["solar_yield_kwh"] or 0
                self_consumption_percent = (
                    (solar_to_house_kwh + solar_to_battery_kwh) / solar_yield_kwh * 100
                    if solar_yield_kwh > 0
                    else 0
                )
                grid_to_house_kwh = row["grid_to_house_kwh"] or 0
                grid_to_battery_kwh = (
                    row["grid_to_battery_kwh"] or row["battery_charge_grid_kwh"] or 0
                )
                grid_import_kwh = displayed_grid_import_kwh(
                    row["smartmeter_import_kwh"],
                    grid_to_house_kwh,
                    grid_to_battery_kwh,
                    meter_configured=bool(
                        self.config.get(CONF_SENSOR_SMARTMETER_IMPORT)
                    ),
                )
                grid_export_kwh = (
                    row["grid_export_kwh"] or row["smartmeter_export_kwh"] or 0
                )
                battery_to_house_kwh = row["battery_to_house_kwh"] or 0
                battery_charge_solar_kwh = (
                    solar_to_battery_kwh or row["battery_charge_solar_kwh"] or 0
                )
                battery_total_charged_kwh = (
                    battery_charge_solar_kwh + grid_to_battery_kwh
                )
                daily_stats[row["date"]] = {
                    "date": row["date"],
                    "solar_total_kwh": solar_yield_kwh,
                    "solar_yield_kwh": solar_yield_kwh,
                    "solar_to_house_kwh": solar_to_house_kwh,
                    "solar_to_battery_kwh": solar_to_battery_kwh,
                    "battery_to_house_kwh": battery_to_house_kwh,
                    "battery_charge_solar_kwh": battery_charge_solar_kwh,
                    "battery_charge_grid_kwh": grid_to_battery_kwh,
                    "battery_total_charged_kwh": battery_total_charged_kwh,
                    "grid_to_house_kwh": grid_to_house_kwh,
                    "grid_to_battery_kwh": grid_to_battery_kwh,
                    "grid_import_kwh": grid_import_kwh,
                    "grid_export_kwh": grid_export_kwh,
                    "home_consumption_kwh": row["home_consumption_kwh"] or 0,
                    "autarky_percent": row["autarkie_percent"] or 0,
                    "self_consumption_percent": min(100, self_consumption_percent),
                    "peak_solar_w": row["peak_solar_w"] or 0,
                    "peak_solar_time": row["peak_solar_time"],
                }
                continue
            return {
                "version": 1,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "days": daily_stats,
            }
        except Exception as error:
            _LOGGER.error("Error reading daily stats from DB: %s", error)
            return {"version": 1, "last_updated": None, "days": {}}
