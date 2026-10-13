# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Hourly billing aggregator for SFML Stats. @zara"""
from __future__ import annotations
import json
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, TYPE_CHECKING
import aiofiles
import asyncio
import aiosqlite
from homeassistant.core import HomeAssistant

MAX_INTEGRATED_HOURLY_KWH = 100.0


def clamp_integrated_hourly_kwh(value: float) -> float:
    """Integrated hour energy above 100 kWh, negative, or non-finite becomes 0."""
    if not math.isfinite(value) or value < 0.0 or value > MAX_INTEGRATED_HOURLY_KWH:
        return 0.0
    return float(value)


if TYPE_CHECKING:
    from ..storage.db_connection_manager import DatabaseConnectionManager
from .eai_wp_sources import resolved_heatpump_entity
from ..const import (
    DOMAIN,
    SFML_STATS_DATA,
    HOURLY_BILLING_HISTORY,
    HOURLY_CATCHUP_HOURS,
    SOLAR_FORECAST_DB,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_LEGACY_FIXED_PRICE_CT,
    CONF_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    DEFAULT_BILLING_FIXED_PRICE,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_FEED_IN_TARIFF,
)
from ..sfml_data_reader import SFMLDataReader
from ..utils.time_utils import (
    as_ha_local,
    format_local_hour_key,
    ha_now,
    ha_timezone,
    ha_today,
    local_date_range_strings,
    naive_ha_local,
    previous_ha_local_hour,
)
from .power_integration import integrate_power_from_recorder

_LOGGER = logging.getLogger(__name__)
_HOUR_KEY_FORMAT = "%Y-%m-%dT%H:%M"


def missing_hour_keys(existing: Iterable[str], now: datetime, hours: int) -> list[str]:
    """Complete hour keys in the last ``hours`` hours that are not in existing."""
    if hours <= 0:
        return []
    naive_now = now.replace(tzinfo=None)
    current_hour = naive_now.replace(minute=0, second=0, microsecond=0)
    existing_hour_keys = {str(hour_key) for hour_key in existing}
    missing_keys = []
    for hours_ago in range(hours, 0, -1):
        hour_start = current_hour - timedelta(hours=hours_ago)
        hour_key = hour_start.strftime("%Y-%m-%dT%H:00")
        if hour_key not in existing_hour_keys:
            missing_keys.append(hour_key)
    return missing_keys


class HourlyBillingAggregator:
    """Aggregate hourly energy values and calculate costs. @zara"""

    def __init__(self, hass: HomeAssistant, config_path: Path) -> None:
        """Initialize the aggregator. @zara"""
        self._hass = hass
        self._config_path = config_path
        self._data_path = config_path / SFML_STATS_DATA
        self._history_file = self._data_path / HOURLY_BILLING_HISTORY
        self._db_path = config_path / SOLAR_FORECAST_DB

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get DB connection via manager with direct fallback. @zara"""
        from ..storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        connection_manager = get_manager()
        if connection_manager is not None:
            try:
                if not connection_manager.is_connected:
                    await connection_manager.connect()
                if connection_manager.is_connected:
                    yield (await connection_manager.get_connection())
                    return
            except Exception as error:
                _LOGGER.warning(
                    "Database connection manager auto-reconnect failed: %s", error
                )
        async with (DatabaseConnectionManager.connect_direct_safe if True else None)(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    async def _get_db_manager(self) -> DatabaseConnectionManager:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
            get_manager as get_manager,
        )

        connection_manager = get_manager()
        if connection_manager is None:
            connection_manager = await (
                DatabaseConnectionManager.get_instance if True else None
            )(self._hass)
        elif not connection_manager.is_connected:
            await connection_manager.connect()
        return connection_manager if True else None

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        connection_manager = await self._get_db_manager()
        async with connection_manager.write_transaction() as connection:
            yield connection
        if True:
            return

    def _get_config(self) -> dict[str, Any]:
        """Get the current configuration. @zara"""
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
        """Read the current value of a sensor. @zara"""
        if not entity_id:
            return
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return

    def _fixed_price_ct(self, config: dict[str, Any]) -> float:
        legacy_fixed_price_ct = config.get(CONF_LEGACY_FIXED_PRICE_CT)
        if legacy_fixed_price_ct not in (None, ""):
            try:
                return max(0.0, float(legacy_fixed_price_ct))
            except (TypeError, ValueError):
                pass
        work_price_ct = config.get(CONF_BILLING_WORK_PRICE, DEFAULT_BILLING_WORK_PRICE)
        grid_fees_ct = config.get(CONF_BILLING_GRID_FEES, DEFAULT_BILLING_GRID_FEES)
        try:
            fixed_total_price_ct = float(work_price_ct or 0) + float(grid_fees_ct or 0)
        except (TypeError, ValueError):
            fixed_total_price_ct = DEFAULT_BILLING_FIXED_PRICE
        if CONF_BILLING_WORK_PRICE not in config and CONF_BILLING_FIXED_PRICE in config:
            try:
                fixed_total_price_ct = float(config[CONF_BILLING_FIXED_PRICE] or 0)
            except (TypeError, ValueError):
                fixed_total_price_ct = DEFAULT_BILLING_FIXED_PRICE
        return max(0.0, fixed_total_price_ct)

    def _feed_in_tariff_ct(self, config: dict[str, Any]) -> float:
        from .price_mode import feed_in_tariff_ct as feed_in_tariff_ct

        return feed_in_tariff_ct(self._hass, config)

    async def _calculate_kwh_from_recorder(
        self, entity_id: str, start_time: datetime, end_time: datetime
    ) -> float:
        """Calculate kWh from watt values in recorder. @zara"""
        if not entity_id:
            return 0.0
        duration_hours = (end_time - start_time).total_seconds() / 3600
        integrated_kwh = await integrate_power_from_recorder(
            self._hass, entity_id, start_time, end_time
        )
        if integrated_kwh is not None:
            return self._bounded_hourly_kwh(entity_id, start_time, integrated_kwh)
        current_power_w = self._get_sensor_value(entity_id)
        if current_power_w is not None and current_power_w > 0:
            fallback_kwh = current_power_w * duration_hours / 1000
            _LOGGER.info(
                "%s: Fallback to current value %.1f W x %.2fh = %.4f kWh",
                entity_id,
                current_power_w,
                duration_hours,
                fallback_kwh,
            )
            return self._bounded_hourly_kwh(entity_id, start_time, fallback_kwh)
        _LOGGER.debug("%s: No value available, returning 0", entity_id)
        return 0.0

    def _bounded_hourly_kwh(
        self, entity_id: str, hour: datetime, value: float
    ) -> float:
        """Drop non-finite, negative, or implausible integrated hour totals."""
        bounded_kwh = clamp_integrated_hourly_kwh(value)
        if bounded_kwh == 0.0 and value != 0.0:
            _LOGGER.warning(
                "%s: implausible hourly energy at %s: %s kWh stored as 0",
                entity_id,
                hour.isoformat() if hasattr(hour, "isoformat") else hour,
                value,
            )
        return bounded_kwh

    async def _resolve_grid_to_battery_kwh(
        self, config: dict[str, Any], start_time: datetime, end_time: datetime
    ) -> float:
        return await self._calculate_kwh_from_recorder(
            config.get(CONF_SENSOR_GRID_TO_BATTERY), start_time, end_time
        )

    async def _load_history(self) -> dict[str, Any]:
        """Load the existing history file. @zara"""
        if not self._history_file.exists():
            return {
                "billing_period": {},
                "hours": {},
                "totals": {
                    "grid_import_kwh": 0,
                    "grid_import_cost_eur": 0,
                    "grid_export_kwh": 0,
                    "solar_yield_kwh": 0,
                    "self_consumption_kwh": 0,
                },
                "last_updated": None,
            }
        try:
            async with aiofiles.open(
                self._history_file, "r", encoding="utf-8"
            ) as history_file:
                history_text = await history_file.read()
                return json.loads(history_text)
        except Exception as error:
            _LOGGER.error("Error loading billing history: %s", error)
            return {
                "billing_period": {},
                "hours": {},
                "totals": {},
                "last_updated": None,
            }
        return None if True else None

    async def _save_history(self, history: dict[str, Any]) -> bool:
        """Save the history file. @zara"""
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
            _LOGGER.error("Error saving billing history: %s", error)
            return False

    async def async_aggregate_hourly(
        self, start_local: datetime | None = None, end_local: datetime | None = None
    ) -> bool:
        """Aggregate one complete hour and calculate costs. @zara"""
        config = self._get_config()
        if start_local is None or end_local is None:
            start_local, end_local = previous_ha_local_hour(self._hass)
        start_time = start_local
        end_time = end_local
        hour_key = format_local_hour_key(start_local, self._hass)
        _LOGGER.info("Starting hourly billing aggregation for %s", hour_key)
        smartmeter_import_entity = config.get(CONF_SENSOR_SMARTMETER_IMPORT)
        smartmeter_export_entity = config.get(CONF_SENSOR_SMARTMETER_EXPORT)
        _LOGGER.debug(
            "Config: import_w=%s, export_w=%s",
            smartmeter_import_entity,
            smartmeter_export_entity,
        )
        if not smartmeter_import_entity:
            _LOGGER.warning(
                "No smartmeter import sensor configured - billing aggregation skipped"
            )
            return False
        grid_import_kwh = 0.0
        data_source = "none"
        grid_import_kwh = await self._calculate_kwh_from_recorder(
            smartmeter_import_entity, start_time, end_time
        )
        if grid_import_kwh > 0:
            data_source = "watt_recorder"
            _LOGGER.info("Grid import from watt recorder: %.4f kWh", grid_import_kwh)
        grid_export_kwh = 0.0
        if smartmeter_export_entity:
            grid_export_kwh = await self._calculate_kwh_from_recorder(
                smartmeter_export_entity, start_time, end_time
            )
        solar_data_reader = SFMLDataReader(self._hass)
        gathered_measurements = await asyncio.gather(
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_GRID_TO_HOUSE), start_time, end_time
            ),
            self._resolve_grid_to_battery_kwh(config, start_time, end_time),
            solar_data_reader.get_hourly_yield(
                date=start_time.strftime("%Y-%m-%d"), hour=start_time.hour
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_SOLAR_TO_HOUSE), start_time, end_time
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_SOLAR_TO_BATTERY), start_time, end_time
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_BATTERY_TO_HOUSE), start_time, end_time
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_HOME_CONSUMPTION), start_time, end_time
            ),
            self._calculate_kwh_from_recorder(
                resolved_heatpump_entity(
                    self._hass, config, CONF_SENSOR_HEATPUMP_POWER
                ),
                start_time,
                end_time,
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_HEATINGROD_POWER), start_time, end_time
            ),
            self._calculate_kwh_from_recorder(
                config.get(CONF_SENSOR_WALLBOX_POWER), start_time, end_time
            ),
        )
        from .grid_split import (
            energy_cost_ct as energy_cost_ct,
            reconcile_grid_split as reconcile_grid_split,
        )

        grid_to_house_kwh = gathered_measurements[0]
        grid_to_battery_kwh = gathered_measurements[1]
        hourly_yield_rows = gathered_measurements[2]
        solar_to_house_kwh = gathered_measurements[3]
        solar_to_battery_kwh = gathered_measurements[4]
        battery_to_house_kwh = gathered_measurements[5]
        home_consumption_kwh = gathered_measurements[6]
        heatpump_kwh = gathered_measurements[7]
        heatingrod_kwh = gathered_measurements[8]
        wallbox_kwh = gathered_measurements[9]
        if hourly_yield_rows:
            solar_yield_kwh = hourly_yield_rows[0].get("yield_total_kwh", 0.0)
            _LOGGER.debug(
                "Solar yield from SFML panel groups: %.4f kWh", solar_yield_kwh
            )
        else:
            solar_yield_kwh = 0.0
            _LOGGER.debug(
                "Solar yield unavailable in SFML panel-group SOT for this hour"
            )
        if solar_yield_kwh <= 0:
            resolved_solar_yield_kwh = await self._resolve_sfml_hourly_actual(
                start_time.strftime("%Y-%m-%d"), start_time.hour
            )
            if resolved_solar_yield_kwh > 0:
                solar_yield_kwh = resolved_solar_yield_kwh
                _LOGGER.info(
                    "Solar yield resolved from SFML hourly actual: %.4f kWh",
                    solar_yield_kwh,
                )
        from .price_mode import (
            MODE_GPM_FIXED as MODE_GPM_FIXED,
            MODE_NONE as MODE_NONE,
            effective_price_mode as effective_price_mode,
            gpm_fixed_total_ct as gpm_fixed_total_ct,
            lookup_hour_price_from_db as lookup_hour_price_from_db,
        )

        price_mode = effective_price_mode(self._hass, config)
        hour_price_ct = None
        if price_mode != MODE_NONE:
            stored_price_ct = None
            fixed_total_price_ct = (
                gpm_fixed_total_ct(self._hass) if price_mode == MODE_GPM_FIXED else None
            )
            try:
                async with self._get_db() as connection:
                    hour_price_ct = await lookup_hour_price_from_db(
                        connection,
                        date_str=start_time.strftime("%Y-%m-%d"),
                        hour=start_time.hour,
                        stored_price=stored_price_ct,
                        mode=price_mode,
                        config=config,
                        today_str=ha_today(self._hass).isoformat(),
                        fixed_total_ct=fixed_total_price_ct,
                    )
            except Exception as error:
                _LOGGER.debug("Hour price lookup failed: %s", error)
                hour_price_ct = None
        feed_in_tariff_ct = self._feed_in_tariff_ct(config)
        is_priced = hour_price_ct is not None
        raw_grid_to_battery_kwh = grid_to_battery_kwh
        grid_to_house_kwh, grid_to_battery_kwh = reconcile_grid_split(
            grid_import_kwh, grid_to_house_kwh, grid_to_battery_kwh
        )
        if (
            raw_grid_to_battery_kwh > grid_import_kwh + 1e-09
            and grid_to_battery_kwh < raw_grid_to_battery_kwh
        ):
            _LOGGER.info(
                "Grid-to-battery %.4f kWh capped to import %.4f kWh for %s",
                raw_grid_to_battery_kwh,
                grid_import_kwh,
                hour_key,
            )
        cost_price_ct = hour_price_ct if is_priced else None
        updated_at = ha_now(self._hass)
        hourly_data = {
            "grid_import_kwh": round(grid_import_kwh, 4),
            "grid_import_cost_ct": energy_cost_ct(grid_import_kwh, cost_price_ct),
            "grid_export_kwh": round(grid_export_kwh, 4),
            "feed_in_revenue_ct": round(grid_export_kwh * feed_in_tariff_ct, 2),
            "feed_in_tariff_ct": round(feed_in_tariff_ct, 2),
            "price_ct_kwh": round(hour_price_ct, 2) if is_priced else None,
            "grid_to_house_kwh": round(grid_to_house_kwh, 4),
            "grid_to_house_cost_ct": energy_cost_ct(grid_to_house_kwh, cost_price_ct),
            "grid_to_battery_kwh": round(grid_to_battery_kwh, 4),
            "grid_to_battery_cost_ct": energy_cost_ct(
                grid_to_battery_kwh, cost_price_ct
            ),
            "solar_yield_kwh": round(solar_yield_kwh, 4),
            "solar_to_house_kwh": round(solar_to_house_kwh, 4),
            "solar_to_battery_kwh": round(solar_to_battery_kwh, 4),
            "battery_to_house_kwh": round(battery_to_house_kwh, 4),
            "home_consumption_kwh": round(home_consumption_kwh, 4),
            "consumer_heatpump_kwh": round(heatpump_kwh, 4),
            "consumer_heatpump_cost_ct": (
                round(heatpump_kwh * hour_price_ct, 2) if is_priced else None
            ),
            "consumer_heatingrod_kwh": round(heatingrod_kwh, 4),
            "consumer_heatingrod_cost_ct": (
                round(heatingrod_kwh * hour_price_ct, 2) if is_priced else None
            ),
            "consumer_wallbox_kwh": round(wallbox_kwh, 4),
            "consumer_wallbox_cost_ct": (
                round(wallbox_kwh * hour_price_ct, 2) if is_priced else None
            ),
            "timestamp": updated_at.isoformat(),
            "data_source": data_source,
        }
        hourly_data.update(
            await self._soc_summary_for_hour(
                start_time.strftime("%Y-%m-%d"), start_time.hour
            )
        )
        history = await self._load_history()
        billing_start_day = config.get(CONF_BILLING_START_DAY, 1)
        billing_start_month = config.get(CONF_BILLING_START_MONTH, 1)
        history["billing_period"] = {
            "start_day": billing_start_day,
            "start_month": billing_start_month,
        }
        history["hours"][hour_key] = hourly_data
        history["last_updated"] = updated_at.isoformat()
        await self._recalculate_totals(history, config)
        history_saved = await self._save_history(history)
        await self._write_hourly_to_db(
            hour_key=hour_key,
            date_str=start_time.strftime("%Y-%m-%d"),
            hour=start_time.hour,
            hourly_data=hourly_data,
        )
        await self._repair_recent_solar_yield_from_sfml(start_time.strftime("%Y-%m-%d"))
        if history_saved:
            logged_price_ct = hour_price_ct if hour_price_ct is not None else 0.0
            _LOGGER.info(
                "Billing aggregation saved: Import=%.3f kWh (%.2f ct), Export=%.3f kWh, Price=%.2f ct/kWh",
                grid_import_kwh,
                grid_import_kwh * logged_price_ct,
                grid_export_kwh,
                logged_price_ct,
            )
        return history_saved

    def _window_for_hour_key(self, hour_key: str) -> tuple[datetime, datetime]:
        parsed_hour = datetime.strptime(hour_key, _HOUR_KEY_FORMAT)
        local_hour_start = as_ha_local(parsed_hour, self._hass)
        hour_end_utc = local_hour_start.astimezone(timezone.utc) + timedelta(hours=1)
        return (local_hour_start, hour_end_utc.astimezone(ha_timezone(self._hass)))

    async def _existing_hour_keys(self, start_key: str, end_key: str) -> set[str]:
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "SELECT hour_key FROM stats_hourly_billing WHERE hour_key >= ? AND hour_key < ?",
                    (start_key, end_key),
                ) as cursor:
                    rows = await cursor.fetchall()
        except Exception:
            return set()
        existing_keys = set()
        for row in rows:
            try:
                hour_key = row["hour_key"]
            except (KeyError, TypeError, IndexError):
                hour_key = row[0]
            if hour_key:
                existing_keys.add(str(hour_key))
        return existing_keys

    async def _aggregate_hour_with_lock_retry(
        self, start_local: datetime, end_local: datetime
    ) -> bool:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        last_error = None
        for attempt in range(3):
            try:
                return await self.async_aggregate_hourly(start_local, end_local)
            except Exception as error:
                last_error = error
                if (
                    DatabaseConnectionManager._is_locked_error
                    if True
                    else DatabaseConnectionManager._is_locked_error
                )(error) and attempt < 2:
                    retry_delay_seconds = (
                        DatabaseConnectionManager._retry_wait
                        if True
                        else DatabaseConnectionManager._retry_wait
                    )(attempt)
                    _LOGGER.warning(
                        "Hourly billing locked for %s (attempt %d/3), retrying in %.2fs",
                        format_local_hour_key(start_local, self._hass),
                        attempt + 1,
                        retry_delay_seconds,
                    )
                    await asyncio.sleep(retry_delay_seconds)
                    continue
                raise
        if last_error is not None:
            raise last_error
        return False

    async def async_catchup_missing_hours(self, hours: int | None = None) -> int:
        """Rewrite missing hour_keys of the last catch-up window from the recorder."""
        catchup_hours = HOURLY_CATCHUP_HOURS if hours is None else int(hours)
        local_now = naive_ha_local(ha_now(self._hass), self._hass)
        current_hour = local_now.replace(minute=0, second=0, microsecond=0)
        start_key = (current_hour - timedelta(hours=catchup_hours)).strftime(
            _HOUR_KEY_FORMAT
        )
        end_key = current_hour.strftime(_HOUR_KEY_FORMAT)
        existing_keys = await self._existing_hour_keys(start_key, end_key)
        missing_keys = missing_hour_keys(existing_keys, local_now, catchup_hours)
        written_hours = 0
        for hour_key in missing_keys:
            local_hour_start, local_hour_end = self._window_for_hour_key(hour_key)
            try:
                if await self._aggregate_hour_with_lock_retry(
                    local_hour_start, local_hour_end
                ):
                    written_hours += 1
            except Exception as error:
                _LOGGER.error(
                    "Hourly billing catch-up failed for %s: %s", hour_key, error
                )
                continue
        _LOGGER.info("Hourly billing catch-up wrote %s missing hour(s)", written_hours)
        return written_hours

    async def _recalculate_totals(
        self, history: dict[str, Any], config: dict[str, Any]
    ) -> None:
        """Recalculate totals for the billing period. @zara"""
        import calendar as calendar_module
        from datetime import date as date

        billing_start_day = config.get(CONF_BILLING_START_DAY, 1)
        billing_start_month = config.get(CONF_BILLING_START_MONTH, 1)
        if isinstance(billing_start_day, str):
            billing_start_day = int(billing_start_day)
        if isinstance(billing_start_month, str):
            billing_start_month = int(billing_start_month)
        billing_start_day = max(1, billing_start_day or 1)
        billing_start_month = max(1, min(12, billing_start_month or 1))
        today = ha_today(self._hass)
        billing_year = today.year
        days_in_start_month = calendar_module.monthrange(
            billing_year, billing_start_month
        )[1]
        billing_start_day = min(billing_start_day, days_in_start_month)
        billing_start_date = date(billing_year, billing_start_month, billing_start_day)
        if billing_start_date > today:
            days_in_previous_start_month = calendar_module.monthrange(
                billing_year - 1, billing_start_month
            )[1]
            billing_start_day = min(billing_start_day, days_in_previous_start_month)
            billing_start_date = date(
                billing_year - 1, billing_start_month, billing_start_day
            )
        billing_start_iso = billing_start_date.isoformat()
        totals = {
            "grid_import_kwh": 0,
            "grid_import_cost_eur": 0,
            "grid_export_kwh": 0,
            "solar_yield_kwh": 0,
            "solar_to_house_kwh": 0,
            "solar_to_battery_kwh": 0,
            "battery_to_house_kwh": 0,
            "grid_to_house_kwh": 0,
            "grid_to_battery_kwh": 0,
            "home_consumption_kwh": 0,
            "hours_count": 0,
            "avg_price_ct": 0,
        }
        sum_price_ct = 0
        for hour_key, hourly_data in history.get("hours", {}).items():
            date_key = hour_key[:10]
            if date_key >= billing_start_iso:
                totals["grid_import_kwh"] += hourly_data.get("grid_import_kwh", 0)
                totals["grid_import_cost_eur"] += (
                    hourly_data.get("grid_import_cost_ct", 0) / 100
                )
                totals["grid_export_kwh"] += hourly_data.get("grid_export_kwh", 0)
                totals["solar_yield_kwh"] += hourly_data.get("solar_yield_kwh", 0)
                totals["solar_to_house_kwh"] += hourly_data.get("solar_to_house_kwh", 0)
                totals["solar_to_battery_kwh"] += hourly_data.get(
                    "solar_to_battery_kwh", 0
                )
                totals["battery_to_house_kwh"] += hourly_data.get(
                    "battery_to_house_kwh", 0
                )
                totals["grid_to_house_kwh"] += hourly_data.get("grid_to_house_kwh", 0)
                totals["grid_to_battery_kwh"] += hourly_data.get(
                    "grid_to_battery_kwh", 0
                )
                totals["home_consumption_kwh"] += hourly_data.get(
                    "home_consumption_kwh", 0
                )
                totals["consumer_heatpump_kwh"] = totals.get(
                    "consumer_heatpump_kwh", 0
                ) + hourly_data.get("consumer_heatpump_kwh", 0)
                totals["consumer_heatingrod_kwh"] = totals.get(
                    "consumer_heatingrod_kwh", 0
                ) + hourly_data.get("consumer_heatingrod_kwh", 0)
                totals["consumer_wallbox_kwh"] = totals.get(
                    "consumer_wallbox_kwh", 0
                ) + hourly_data.get("consumer_wallbox_kwh", 0)
                totals["hours_count"] += 1
                sum_price_ct += hourly_data.get("price_ct_kwh", 0)
                continue
        if totals["hours_count"] > 0:
            totals["avg_price_ct"] = round(sum_price_ct / totals["hours_count"], 2)
        for total_key in totals:
            if isinstance(totals[total_key], float):
                totals[total_key] = round(totals[total_key], 4)
        totals["self_consumption_kwh"] = round(
            max(0, totals["solar_yield_kwh"] - totals["grid_export_kwh"]), 4
        )
        solar_battery_to_house_kwh = max(
            0.0, totals["battery_to_house_kwh"] - totals["grid_to_battery_kwh"]
        )
        own_supply_kwh = totals["solar_to_house_kwh"] + solar_battery_to_house_kwh
        total_consumption_kwh = totals["home_consumption_kwh"]
        if total_consumption_kwh <= 0:
            total_consumption_kwh = totals["grid_import_kwh"] + own_supply_kwh
        if total_consumption_kwh > 0:
            totals["autarkie_percent"] = round(
                min(100, own_supply_kwh / total_consumption_kwh * 100), 1
            )
        else:
            totals["autarkie_percent"] = 0
        savings_ct = 0.0
        for hour_key, hourly_data in history.get("hours", {}).items():
            date_key = hour_key[:10]
            if date_key >= billing_start_iso:
                hourly_own_supply_kwh = (
                    hourly_data.get("solar_to_house_kwh", 0) or 0
                ) + (hourly_data.get("battery_to_house_kwh", 0) or 0)
                price_ct = hourly_data.get("price_ct_kwh", 0) or 0
                savings_ct += hourly_own_supply_kwh * price_ct
        totals["savings_eur"] = round(savings_ct / 100, 2)
        history["totals"] = totals
        history["billing_period"]["start"] = billing_start_iso

    async def _get_gpm_price(self, date_str: str, hour: int) -> float | None:
        """Get exact price from GPM_price_history for a specific hour. @zara"""
        if not self._db_path.exists():
            return
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT total_price FROM GPM_price_history\n                    WHERE timestamp >= ? AND timestamp < ? AND hour = ?\n                    ORDER BY timestamp DESC LIMIT 1\n                    ",
                    (*local_date_range_strings(date_str), hour),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        if row["total_price"] is not None:
                            return float(row["total_price"])
        except Exception as error:
            _LOGGER.debug(
                "GPM price lookup failed for %s H%d: %s", date_str, hour, error
            )
        return None if True else None

    async def _resolve_sfml_hourly_actual(self, date_str: str, hour: int) -> float:
        """Resolve a completed SFML hourly actual for billing writes."""
        if not self._db_path.exists():
            return 0.0
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT COALESCE(SUM(ppg.actual_kwh), hp.actual_kwh, 0) AS actual_kwh\n                    FROM hourly_predictions hp\n                    LEFT JOIN prediction_panel_groups ppg\n                        ON ppg.prediction_id = hp.prediction_id\n                    WHERE hp.target_date = ?\n                      AND hp.target_hour = ?\n                      AND (ppg.actual_kwh IS NOT NULL OR hp.actual_kwh IS NOT NULL)\n                    GROUP BY hp.prediction_id, hp.actual_kwh\n                    ",
                    (date_str, hour),
                ) as cursor:
                    row = await cursor.fetchone()
            if row and row["actual_kwh"] is not None:
                return max(0.0, float(row["actual_kwh"]))
        except Exception as error:
            _LOGGER.debug(
                "SFML hourly actual lookup failed for %s H%d: %s", date_str, hour, error
            )
        return 0.0 if True else None

    async def _repair_recent_solar_yield_from_sfml(self, date_str: str) -> int:
        """Repair hourly billing solar yield after SFML actuals become available."""
        if not self._db_path.exists():
            return 0
        try:
            async with self._write_transaction() as connection:
                cursor = await connection.execute(
                    "\n                    WITH sfml_actuals AS (\n                        SELECT hp.target_date AS date,\n                               hp.target_hour AS hour,\n                               COALESCE(SUM(ppg.actual_kwh), hp.actual_kwh, 0) AS actual_kwh\n                        FROM hourly_predictions hp\n                        LEFT JOIN prediction_panel_groups ppg\n                            ON ppg.prediction_id = hp.prediction_id\n                        WHERE hp.target_date = ?\n                          AND (ppg.actual_kwh IS NOT NULL OR hp.actual_kwh IS NOT NULL)\n                        GROUP BY hp.prediction_id, hp.target_date, hp.target_hour, hp.actual_kwh\n                    )\n                    UPDATE stats_hourly_billing\n                    SET solar_yield_kwh = (\n                        SELECT ROUND(actual_kwh, 4)\n                        FROM sfml_actuals\n                        WHERE sfml_actuals.date = stats_hourly_billing.date\n                          AND sfml_actuals.hour = stats_hourly_billing.hour\n                    )\n                    WHERE date = ?\n                      AND COALESCE(solar_yield_kwh, 0) = 0\n                      AND EXISTS (\n                          SELECT 1\n                          FROM sfml_actuals\n                          WHERE sfml_actuals.date = stats_hourly_billing.date\n                            AND sfml_actuals.hour = stats_hourly_billing.hour\n                            AND sfml_actuals.actual_kwh > 0\n                      )\n                    ",
                    (date_str, date_str),
                )
                repaired_rows = cursor.rowcount if cursor.rowcount is not None else 0
            if repaired_rows > 0:
                _LOGGER.info(
                    "Repaired %d hourly billing solar-yield rows from SFML actuals for %s",
                    repaired_rows,
                    date_str,
                )
            return repaired_rows
        except Exception as error:
            _LOGGER.debug(
                "Hourly billing solar-yield repair failed for %s: %s", date_str, error
            )
            return 0

    async def _soc_summary_for_hour(
        self, date_str: str, hour: int
    ) -> dict[str, float | None]:
        """Condense measured battery SOC for one local hour before the 7-day raw cutoff."""
        empty_soc_summary = {
            "battery_soc_min": None,
            "battery_soc_avg": None,
            "battery_soc_max": None,
            "battery_soc_end": None,
        }
        if not self._db_path.exists():
            return empty_soc_summary
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT battery_soc\n                    FROM stats_power_sources\n                    WHERE date = ? AND hour = ?\n                      AND battery_soc IS NOT NULL\n                      AND battery_soc BETWEEN 0 AND 100\n                    ORDER BY timestamp ASC\n                    ",
                    (date_str, hour),
                ) as cursor:
                    rows = await cursor.fetchall()
        except Exception as error:
            _LOGGER.debug(
                "Hourly SOC summary unavailable for %s hour %s: %s",
                date_str,
                hour,
                error,
            )
            return empty_soc_summary
        soc_values = []
        for row in rows:
            try:
                raw_soc = row["battery_soc"]
            except (KeyError, TypeError, IndexError):
                raw_soc = row[0]
            try:
                soc_percent = float(raw_soc)
            except (TypeError, ValueError):
                continue
            if 0 <= soc_percent <= 100:
                soc_values.append(soc_percent)
        if not soc_values:
            return empty_soc_summary
        return {
            "battery_soc_min": round(min(soc_values), 1),
            "battery_soc_avg": round(sum(soc_values) / len(soc_values), 1),
            "battery_soc_max": round(max(soc_values), 1),
            "battery_soc_end": round(soc_values[-1], 1),
        }

    async def _write_hourly_to_db(
        self, hour_key: str, date_str: str, hour: int, hourly_data: dict[str, Any]
    ) -> bool:
        """Write hourly billing data to stats_hourly_billing table. @zara"""
        if not self._db_path.exists():
            _LOGGER.debug("DB not available, skipping hourly billing write")
            return False
        try:
            async with self._write_transaction() as connection:
                await self._ensure_hourly_billing_columns(connection)
                await connection.execute(
                    "\n                    INSERT OR REPLACE INTO stats_hourly_billing (\n                        hour_key, date, hour,\n                        grid_import_kwh, grid_import_cost_ct,\n                        grid_export_kwh, feed_in_revenue_ct, feed_in_tariff_ct,\n                        price_ct_kwh,\n                        grid_to_house_kwh, grid_to_house_cost_ct,\n                        grid_to_battery_kwh, grid_to_battery_cost_ct,\n                        solar_yield_kwh, solar_to_house_kwh, solar_to_battery_kwh,\n                        battery_to_house_kwh, home_consumption_kwh,\n                        consumer_heatpump_kwh, consumer_heatpump_cost_ct,\n                        consumer_heatingrod_kwh, consumer_heatingrod_cost_ct,\n                        consumer_wallbox_kwh, consumer_wallbox_cost_ct,\n                        battery_soc_min, battery_soc_avg, battery_soc_max, battery_soc_end,\n                        data_source\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ",
                    (
                        hour_key,
                        date_str,
                        hour,
                        hourly_data.get("grid_import_kwh", 0),
                        hourly_data.get("grid_import_cost_ct", 0),
                        hourly_data.get("grid_export_kwh", 0),
                        hourly_data.get("feed_in_revenue_ct", 0),
                        hourly_data.get("feed_in_tariff_ct", 0),
                        hourly_data.get("price_ct_kwh", 0),
                        hourly_data.get("grid_to_house_kwh", 0),
                        hourly_data.get("grid_to_house_cost_ct"),
                        hourly_data.get("grid_to_battery_kwh", 0),
                        hourly_data.get("grid_to_battery_cost_ct"),
                        hourly_data.get("solar_yield_kwh", 0),
                        hourly_data.get("solar_to_house_kwh", 0),
                        hourly_data.get("solar_to_battery_kwh", 0),
                        hourly_data.get("battery_to_house_kwh", 0),
                        hourly_data.get("home_consumption_kwh", 0),
                        hourly_data.get("consumer_heatpump_kwh", 0),
                        hourly_data.get("consumer_heatpump_cost_ct", 0),
                        hourly_data.get("consumer_heatingrod_kwh", 0),
                        hourly_data.get("consumer_heatingrod_cost_ct", 0),
                        hourly_data.get("consumer_wallbox_kwh", 0),
                        hourly_data.get("consumer_wallbox_cost_ct", 0),
                        hourly_data.get("battery_soc_min"),
                        hourly_data.get("battery_soc_avg"),
                        hourly_data.get("battery_soc_max"),
                        hourly_data.get("battery_soc_end"),
                        hourly_data.get("data_source", "aggregator"),
                    ),
                )
            _LOGGER.debug(
                "Hourly billing written to DB: %s (%.4f kWh @ %.2f ct)",
                hour_key,
                hourly_data.get("grid_import_kwh", 0),
                hourly_data.get("price_ct_kwh", 0),
            )
        except Exception as error:
            from ..storage.db_connection_manager import (
                DatabaseConnectionManager as DatabaseConnectionManager,
            )

            if (DatabaseConnectionManager._is_locked_error if True else None)(error):
                raise
            _LOGGER.error("Error writing hourly billing to DB: %s", error)
            return False
        return True

    async def _ensure_hourly_billing_columns(self, db: aiosqlite.Connection) -> None:
        """Retained for call compatibility; schema is gated during setup."""

    async def async_cleanup_old_data(self, keep_days: int = 400) -> int:
        """Remove old hourly data older than keep_days. @zara"""
        history = await self._load_history()
        cutoff = (ha_today(self._hass) - timedelta(days=keep_days)).isoformat()
        deleted_count = 0
        retained_hours = {}
        for hour_key, hourly_data in history.get("hours", {}).items():
            date_key = hour_key[:10]
            if date_key >= cutoff:
                retained_hours[hour_key] = hourly_data
            else:
                deleted_count += 1
        if deleted_count > 0:
            history["hours"] = retained_hours
            await self._save_history(history)
            _LOGGER.info("Billing history: %d old entries deleted", deleted_count)
        return deleted_count
