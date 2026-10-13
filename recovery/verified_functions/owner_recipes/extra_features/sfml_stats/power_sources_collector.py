# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite
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


class PowerSourcesCollector:

    async def _collect_data(self) -> None:
        """Collect current power values and store in SFML database. @zara"""
        _var_var_30 = datetime.now(timezone.utc)
        _var_var_18 = as_ha_local(_var_var_30, self.hass)
        _var_var_19 = await self._sfml_reader.get_live_power()
        _var_var_31 = self._get_power_sensor_value(CONF_SENSOR_INVERTER_AC_OUTPUT)
        _var_var_32 = self._get_signed_power_sensor_value(CONF_SENSOR_BATTERY_POWER)
        _var_var_33 = self._get_power_sensor_value(CONF_SENSOR_SOLAR_TO_HOUSE)
        _var_var_20 = self._get_power_sensor_value(CONF_SENSOR_SOLAR_TO_BATTERY)
        _var_var_34 = self._get_power_sensor_value(CONF_SENSOR_BATTERY_TO_HOUSE)
        _var_var_35 = self._get_power_sensor_value(CONF_SENSOR_GRID_TO_HOUSE)
        _var_var_36 = self._get_power_sensor_value(CONF_SENSOR_GRID_TO_BATTERY)
        _var_var_37 = self._get_power_sensor_value(CONF_SENSOR_HOME_CONSUMPTION)
        _var_var_38 = self._get_power_sensor_value(CONF_SENSOR_HOUSE_TO_GRID)
        _var_var_39 = self._get_sensor_value(CONF_SENSOR_BATTERY_SOC)
        _var_var_40 = self._get_power_sensor_value(CONF_SENSOR_SMARTMETER_IMPORT)
        _var_var_41 = self._get_power_sensor_value(CONF_SENSOR_SMARTMETER_EXPORT)
        from .core.eai_wp_sources import resolved_heatpump_entity as _var_var_42

        _var_var_43 = _var_var_42(self.hass, self.config, CONF_SENSOR_HEATPUMP_POWER)
        _var_var_44 = self._get_entity_power_value(_var_var_43) if _var_var_43 else None
        _var_var_45 = self._get_power_sensor_value(CONF_SENSOR_HEATINGROD_POWER)
        _var_var_46 = self._get_power_sensor_value(CONF_SENSOR_WALLBOX_POWER)
        if _var_var_19 is not None and _var_var_19 < 0:
            _var_var_19 = 0.0
        if _var_var_33 is not None and _var_var_33 < 0:
            _var_var_33 = 0.0
        if _var_var_20 is not None and _var_var_20 < 0:
            _var_var_20 = 0.0
        if _var_var_38 is not None and _var_var_38 < 0:
            _var_var_38 = 0.0
        if _var_var_33 is None and _var_var_19 is not None:
            _var_var_47 = _var_var_20 or 0
            _var_var_33 = max(0, _var_var_19 - _var_var_47)
            _LOGGER.debug(
                "Calculated solar_to_house: %.1f W (solar %.1f - battery %.1f)",
                _var_var_33,
                _var_var_19,
                _var_var_47,
            )
        if _var_var_35 is None and _var_var_37 is not None:
            _var_var_48 = _var_var_33 or 0
            _var_var_49 = _var_var_34 or 0
            _var_var_35 = max(0, _var_var_37 - _var_var_48 - _var_var_49)
            _LOGGER.debug(
                "Calculated grid_to_house: %.1f W (consumption %.1f - solar %.1f - battery %.1f)",
                _var_var_35,
                _var_var_37,
                _var_var_48,
                _var_var_49,
            )
        if _var_var_40 is not None:
            _var_var_35, _var_var_36 = reconcile_grid_split(
                _var_var_40, _var_var_35, _var_var_36, tolerance=SPLIT_TOLERANCE_W
            )
        elif _var_var_35 is not None:
            _var_var_50 = _var_var_36 or 0
            _var_var_40 = _var_var_35 + _var_var_50
            _LOGGER.debug("Calculated smartmeter_import: %.1f W", _var_var_40)
        if _var_var_38 is None and _var_var_19 is not None:
            _var_var_48 = _var_var_33 or 0
            _var_var_47 = _var_var_20 or 0
            _var_var_38 = max(0, _var_var_19 - _var_var_48 - _var_var_47)
            _LOGGER.debug("Calculated house_to_grid: %.1f W", _var_var_38)
        if _var_var_41 is None:
            _var_var_41 = _var_var_38
        if _var_var_41 is None and _var_var_19 is not None:
            _var_var_48 = _var_var_33 or 0
            _var_var_47 = _var_var_20 or 0
            _var_var_41 = max(0, _var_var_19 - _var_var_48 - _var_var_47)
            _LOGGER.debug("Calculated smartmeter_export: %.1f W", _var_var_41)
        try:
            async with self._write_transaction() as _var_var_4:
                await self._ensure_power_source_columns(_var_var_4)
                await _var_var_4.execute(
                    "\n                    INSERT OR REPLACE INTO stats_power_sources (\n                        timestamp, date, hour,\n                        solar_power_w, inverter_ac_output_w, battery_power_dc_w,\n                        house_consumption_w,\n                        solar_to_house_w, solar_to_battery_w,\n                        solar_to_grid_w, battery_to_house_w,\n                        grid_to_house_w, grid_to_battery_w, battery_soc\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ",
                    (
                        _var_var_30.isoformat(),
                        _var_var_18.strftime("%Y-%m-%d"),
                        _var_var_18.hour,
                        _var_var_19 or 0,
                        _var_var_31,
                        _var_var_32,
                        _var_var_37 or 0,
                        _var_var_33 or 0,
                        _var_var_20 or 0,
                        _var_var_38 or 0,
                        _var_var_34 or 0,
                        _var_var_35 or 0,
                        _var_var_36 or 0,
                        _var_var_39,
                    ),
                )
                _var_var_29 = (
                    _var_var_30 - timedelta(days=MAX_DATA_AGE_DAYS)
                ).isoformat()
                await _var_var_4.execute(
                    "DELETE FROM stats_power_sources WHERE timestamp < ?",
                    (_var_var_29,),
                )
        except Exception as _var_var_11:
            if self._is_locked_error(_var_var_11):
                _LOGGER.debug(
                    "Power sources data write skipped after DB lock retries: %s",
                    _var_var_11,
                )
            else:
                _LOGGER.error(
                    "Error inserting power sources data into DB: %s", _var_var_11
                )
        try:
            await self._write_live_power()
        except Exception as _var_var_11:
            if self._is_locked_error(_var_var_11):
                _LOGGER.debug(
                    "SFML live power snapshot skipped after DB lock retries: %s",
                    _var_var_11,
                )
            else:
                _LOGGER.error("Error writing SFML live power snapshot: %s", _var_var_11)
        _LOGGER.debug(
            "Collected power data: solar_power=%.1f, solar_to_house=%.1f, solar_to_battery=%.1f, battery=%.1f, grid=%.1f, consumption=%.1f, soc=%s",
            _var_var_19 or 0,
            _var_var_33 or 0,
            _var_var_20 or 0,
            _var_var_34 or 0,
            _var_var_35 or 0,
            _var_var_37 or 0,
            _var_var_39,
        )
        _var_var_51 = {
            "solar_power": _var_var_19,
            "inverter_ac_output": _var_var_31,
            "battery_power_dc": _var_var_32,
            "solar_to_house": _var_var_33,
            "solar_to_battery": _var_var_20,
            "house_to_grid": _var_var_38,
            "battery_to_house": _var_var_34,
            "grid_to_house": _var_var_35,
            "grid_to_battery": _var_var_36,
            "home_consumption": _var_var_37,
            "battery_soc": _var_var_39,
            "smartmeter_import": _var_var_40,
            "smartmeter_export": _var_var_41,
            "heatpump_w": _var_var_44,
            "heatingrod_w": _var_var_45,
            "wallbox_w": _var_var_46,
        }
        await self._update_daily_stats(_var_var_30, _var_var_51)
        await self._update_consumer_atlas(_var_var_30)

    async def _update_consumer_atlas(self, now: datetime) -> None:
        _var_var_18 = as_ha_local(now, self.hass)
        _var_var_60 = _var_var_18.strftime("%Y-%m-%d")
        _var_var_61 = _var_var_18.strftime("%H:%M")
        _var_var_62 = COLLECTION_INTERVAL
        try:
            async with self._write_transaction() as _var_var_4:
                await self._ensure_consumer_atlas_tables(_var_var_4)
                async with _var_var_4.execute(
                    "SELECT consumer_id, entity_id\n                       FROM stats_consumer_atlas_config\n                       WHERE enabled = 1\n                       ORDER BY sort_order, name"
                ) as _var_var_56:
                    _var_var_63 = await _var_var_56.fetchall()
                if not _var_var_63:
                    return
                for _var_var_64 in _var_var_63:
                    _var_var_65 = self._get_entity_power_value(_var_var_64["entity_id"])
                    if _var_var_65 is None:
                        continue
                    _var_var_66 = integrate_power_to_kwh(_var_var_65, _var_var_62)
                    await _var_var_4.execute(
                        "INSERT INTO stats_consumer_atlas_daily (\n                               date, consumer_id, kwh, peak_w, peak_time,\n                               last_power_w, samples, updated_at\n                           ) VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)\n                           ON CONFLICT(date, consumer_id) DO UPDATE SET\n                               kwh = kwh + excluded.kwh,\n                               peak_w = MAX(peak_w, excluded.peak_w),\n                               peak_time = CASE\n                                   WHEN excluded.peak_w > peak_w THEN excluded.peak_time\n                                   ELSE peak_time\n                               END,\n                               last_power_w = excluded.last_power_w,\n                               samples = samples + 1,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            _var_var_60,
                            _var_var_64["consumer_id"],
                            _var_var_66,
                            _var_var_65,
                            _var_var_61,
                            _var_var_65,
                        ),
                    )
        except Exception as _var_var_11:
            if self._is_locked_error(_var_var_11):
                _LOGGER.debug(
                    "Consumer atlas update skipped after DB lock retries: %s",
                    _var_var_11,
                )
            else:
                _LOGGER.error("Error updating consumer atlas in DB: %s", _var_var_11)
        if True:
            return

    async def _backfill_efficiency_history(self, *, force: bool = False) -> None:
        _var_var_76 = self.config.get(CONF_SENSOR_INVERTER_AC_OUTPUT)
        _var_var_77 = self.config.get(CONF_SENSOR_BATTERY_POWER)
        if not _var_var_76:
            return
        _var_var_78 = (
            f"v{EFFICIENCY_CALCULATION_VERSION}|{_var_var_76}|{_var_var_77 or ''}"
        )
        try:
            async with self._write_transaction() as _var_var_4:
                await self._ensure_power_source_columns(_var_var_4)
                await self._ensure_db_columns(_var_var_4)
                await self._ensure_efficiency_schema(_var_var_4)
                async with _var_var_4.execute(
                    "SELECT value FROM stats_energy_efficiency_meta\n                       WHERE key = 'backfill_sensor_signature'"
                ) as _var_var_56:
                    _var_var_57 = await _var_var_56.fetchone()
                if _var_var_57 and _var_var_57[0] == _var_var_78:
                    if not force:
                        return
            _var_var_79 = 10
            try:
                from homeassistant.components.recorder import (
                    get_instance as _var_var_80,
                )

                _var_var_81 = _var_var_80(self.hass)
                _var_var_79 = int(getattr(_var_var_81, "keep_days", _var_var_79))
            except (ImportError, RuntimeError, TypeError, ValueError):
                pass
            _var_var_79 = min(370, max(1, _var_var_79))
            _var_var_18 = ha_now(self.hass)
            _var_var_82 = (_var_var_18 - timedelta(days=_var_var_79)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            _var_var_83 = False
            _var_var_84 = 0
            while _var_var_82.date() <= _var_var_18.date():
                if not self._running:
                    return
                _var_var_85 = (_var_var_82 + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                _var_var_86 = min(_var_var_85, _var_var_18)
                self__recovered_capture_0003 = _var_var_82.astimezone(timezone.utc)
                A_recovered_capture_0000 = _var_var_86.astimezone(timezone.utc)
                _var_var_89, _var_var_90 = await asyncio.gather(
                    power_states_from_recorder(
                        self.hass,
                        _var_var_76,
                        self__recovered_capture_0003,
                        A_recovered_capture_0000,
                    ),
                    power_states_from_recorder(
                        self.hass,
                        _var_var_77,
                        self__recovered_capture_0003,
                        A_recovered_capture_0000,
                    ),
                )
                if _var_var_89:
                    _var_var_83 = True
                    await self._backfill_efficiency_day(
                        date_str=_var_var_82.date().isoformat(),
                        start_utc=self__recovered_capture_0003,
                        end_utc=A_recovered_capture_0000,
                        ac_states=_var_var_89,
                        battery_states=_var_var_90,
                    )
                    _var_var_84 += 1
                _var_var_82 = _var_var_85
                await asyncio.sleep(0)
            if _var_var_83:
                async with self._write_transaction() as _var_var_4:
                    await self._ensure_efficiency_schema(_var_var_4)
                    await _var_var_4.execute(
                        "INSERT INTO stats_energy_efficiency_meta (\n                               key, value, updated_at\n                           ) VALUES ('backfill_sensor_signature', ?,\n                                     CURRENT_TIMESTAMP)\n                           ON CONFLICT(key) DO UPDATE SET\n                               value = excluded.value,\n                               updated_at = CURRENT_TIMESTAMP",
                        (_var_var_78,),
                    )
                _LOGGER.info(
                    "Energy efficiency recorder backfill completed for %d days",
                    _var_var_84,
                )
            else:
                _LOGGER.info(
                    "No recorder history available for inverter AC sensor %s",
                    _var_var_76,
                )
        except asyncio.CancelledError:
            raise
        except Exception as _var_var_91:
            _LOGGER.error("Energy efficiency recorder backfill failed: %s", _var_var_91)
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
        _var_var_92 = integrate_power_states(ac_states, start_utc, end_utc)
        _var_var_93 = integrate_signed_power_states(battery_states, start_utc, end_utc)
        _var_var_94 = _var_var_93[0] if _var_var_93 else None
        _var_var_95 = _var_var_93[1] if _var_var_93 else None
        async with self._write_transaction() as _var_var_4:
            await self._ensure_power_source_columns(_var_var_4)
            await self._ensure_db_columns(_var_var_4)
            await self._ensure_efficiency_schema(_var_var_4)
            await _var_var_4.execute(
                "INSERT OR IGNORE INTO stats_daily_energy (date) VALUES (?)",
                (date_str,),
            )
            await _var_var_4.execute(
                "UPDATE stats_daily_energy SET\n                       inverter_ac_output_kwh = ?,\n                       battery_charge_dc_kwh = ?,\n                       battery_discharge_dc_kwh = ?\n                   WHERE date = ?",
                (_var_var_92, _var_var_94, _var_var_95, date_str),
            )
            async with _var_var_4.execute(
                "SELECT timestamp\n                   FROM stats_power_sources\n                   WHERE date = ?\n                   ORDER BY timestamp",
                (date_str,),
            ) as _var_var_56:
                _var_var_96 = await _var_var_56.fetchall()
            _var_var_97 = [_var_var_57[0] for _var_var_57 in _var_var_96]
            if _var_var_97:
                _var_var_99 = [
                    datetime.fromisoformat(_var_var_98.replace("Z", "+00:00"))
                    for _var_var_98 in _var_var_97
                ]
                _var_var_100 = power_values_at_timestamps(ac_states, _var_var_99)
                _var_var_101 = power_values_at_timestamps(
                    battery_states, _var_var_99, signed=True
                )
                await _var_var_4.executemany(
                    "UPDATE stats_power_sources SET\n                           inverter_ac_output_w = ?,\n                           battery_power_dc_w = ?\n                       WHERE timestamp = ?",
                    [
                        (_var_var_103, _var_var_104, _var_var_102)
                        for _var_var_102, _var_var_103, _var_var_104 in zip(
                            _var_var_97, _var_var_100, _var_var_101
                        )
                    ],
                )
            await self._recalculate_efficiency_day(
                _var_var_4,
                date_str=date_str,
                source="recorder_aligned" if _var_var_97 else "recorder_daily",
            )
        return None if True else None
