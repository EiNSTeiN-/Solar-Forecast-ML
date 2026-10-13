# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Consumer atlas, consumer detail, and amortization views."""
from __future__ import annotations
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _corrected_daily_energy_values,
    _get_db,
    _get_fixed_total_price,
    _get_power_kw,
    _get_power_w,
    _stable_api_error,
    _table_columns,
    _time_context,
    local_only,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today, ha_timezone
from ..core.corrected_energy import (
    component_day,
    dynamic_month_costs,
    energy_flows_changed,
    legacy_dynamic_month,
    load_hourly_component_days,
    own_supply_kwh,
)
from ..core.price_mode import effective_price_mode, is_fixed_like
from ..const import (
    DOMAIN,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    CONF_AMORTIZATION_INVESTMENT_EUR,
    CONF_AMORTIZATION_SUBSIDY_EUR,
    CONF_AMORTIZATION_COMMISSIONING_DATE,
    CONF_AMORTIZATION_ANNUAL_RUNNING_COSTS_EUR,
    CONF_AMORTIZATION_PRICE_INCREASE_PERCENT,
    CONF_AMORTIZATION_DEGRADATION_PERCENT,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_DAILY,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HP_HEATING_MODE,
    CONF_SENSOR_HP_DHW_MODE,
    CONF_SENSOR_HP_DHW_CHARGING,
    CONF_SENSOR_HP_PV_ACTIVE,
    CONF_SENSOR_HP_ELECTRIC_POWER,
    CONF_SENSOR_HP_THERMAL_POWER,
    CONF_SENSOR_HP_GRID_ENERGY_DAILY,
    CONF_SENSOR_HP_PV_ENERGY_DAILY,
    CONF_SENSOR_HP_JAZ,
    CONF_SENSOR_HP_COMPRESSOR_STARTS,
    CONF_SENSOR_HP_STORAGE_TEMP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    CONF_SENSOR_WB_CHARGE_MODE,
    CONF_SENSOR_WB_ENERGY_SESSION,
)
from ..core.amortization import (
    DEFAULT_AMORTIZATION_SETTINGS,
    calculate_amortization,
    sanitize_amortization_settings,
)
from ..core.consumer_atlas import (
    build_consumer_atlas_summary,
    calculate_billing_period,
    sanitize_consumer_config,
    valid_consumer_config,
)
from ..core.heatpump_today import (
    eai_electric_today_kwh,
    resolve_heatpump_today_kwh,
    split_heatpump_today_energy,
)

_LOGGER = logging.getLogger(__name__)


def _display_watts(watts: float | None) -> float | None:
    return None if watts is None else round(watts, 2)


class ConsumerAtlasView(HomeAssistantView):
    """API for arbitrary consumer tracking."""

    url = "/api/sfml_stats/consumer_atlas"
    name = "api:sfml_stats:consumer_atlas"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return consumer atlas configuration and period totals."""
        if context.HASS is None:
            return web.json_response(
                _stable_api_error(
                    "home_assistant_not_initialized",
                    "Home Assistant not initialized",
                    status="unavailable",
                ),
                status=503,
            )
        try:
            runtime_config = self._runtime_config()
            today = ha_today(context.HASS)
            period_start, period_end = calculate_billing_period(
                today,
                runtime_config.get(
                    CONF_BILLING_START_MONTH, DEFAULT_BILLING_START_MONTH
                ),
                runtime_config.get(CONF_BILLING_START_DAY, DEFAULT_BILLING_START_DAY),
            )
            period_start_iso = period_start.isoformat()
            today_iso = today.isoformat()
            async with _get_db() as connection:
                await self._ensure_tables(connection)
                consumer_configs = await self._load_configs(connection)
                daily_rows = await self._load_daily_rows(
                    connection, period_start_iso, today_iso
                )
                home_consumption_kwh = await self._load_home_consumption(
                    connection, period_start_iso, today_iso
                )
            live_power = self._live_power_by_consumer(consumer_configs)
            summary = build_consumer_atlas_summary(
                consumer_configs,
                daily_rows,
                home_consumption_kwh=home_consumption_kwh,
                today=today,
                live_power=live_power,
            )
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    "period": {
                        "start": period_start_iso,
                        "end": period_end.isoformat(),
                        "today": today_iso,
                    },
                    **summary,
                }
            )
        except Exception as error:
            _LOGGER.error("Error loading consumer atlas: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        """Create or update a consumer atlas entry."""
        try:
            payload = await request.json() if request.body_exists else {}
            async with _get_db() as connection:
                await self._ensure_tables(connection)
                max_sort_order = await self._max_sort_order(connection)
                consumer_config = sanitize_consumer_config(
                    payload, default_sort_order=max_sort_order + 1
                )
                if not valid_consumer_config(consumer_config):
                    return web.json_response(
                        {"success": False, "error": "name and entity_id are required"},
                        status=400,
                    )
                await connection.execute(
                    "INSERT INTO stats_consumer_atlas_config (\n                           consumer_id, name, entity_id, category, icon, color,\n                           start_kwh, enabled, sort_order, updated_at\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                       ON CONFLICT(consumer_id) DO UPDATE SET\n                           name = excluded.name,\n                           entity_id = excluded.entity_id,\n                           category = excluded.category,\n                           icon = excluded.icon,\n                           color = excluded.color,\n                           start_kwh = excluded.start_kwh,\n                           enabled = excluded.enabled,\n                           sort_order = excluded.sort_order,\n                           updated_at = CURRENT_TIMESTAMP",
                    (
                        consumer_config["consumer_id"],
                        consumer_config["name"],
                        consumer_config["entity_id"],
                        consumer_config["category"],
                        consumer_config["icon"],
                        consumer_config["color"],
                        consumer_config["start_kwh"],
                        1 if consumer_config["enabled"] else 0,
                        consumer_config["sort_order"],
                    ),
                )
                await connection.commit()
            return await self.get(request)
        except Exception as error:
            _LOGGER.error("Error saving consumer atlas entry: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        """Compatibility hook; the STATS startup gate owns this schema."""

    async def _ensure_config_columns(self, db: aiosqlite.Connection) -> None:
        """Compatibility hook; the STATS startup gate owns this schema."""

    async def _load_configs(self, db: aiosqlite.Connection) -> list[dict[str, Any]]:
        async with db.execute(
            "SELECT consumer_id, name, entity_id, category, icon, color,\n                      start_kwh, enabled, sort_order\n               FROM stats_consumer_atlas_config\n               ORDER BY sort_order, name"
        ) as cursor:
            config_rows = await cursor.fetchall()
        consumer_configs = []
        for row in config_rows:
            consumer_configs.append(
                {
                    "consumer_id": row["consumer_id"],
                    "name": row["name"],
                    "entity_id": row["entity_id"],
                    "category": row["category"],
                    "icon": row["icon"],
                    "color": row["color"],
                    "start_kwh": float(row["start_kwh"] or 0),
                    "enabled": bool(row["enabled"]),
                    "sort_order": row["sort_order"],
                }
            )
        return consumer_configs

    async def _load_daily_rows(
        self, db: aiosqlite.Connection, start: str, end: str
    ) -> list[dict[str, Any]]:
        async with db.execute(
            "SELECT date, consumer_id, kwh, peak_w, peak_time,\n                      last_power_w, samples\n               FROM stats_consumer_atlas_daily\n               WHERE date >= ? AND date <= ?\n               ORDER BY date, consumer_id",
            (start, end),
        ) as cursor:
            daily_rows = await cursor.fetchall()
        return [
            {
                "date": row["date"],
                "consumer_id": row["consumer_id"],
                "kwh": row["kwh"],
                "peak_w": row["peak_w"],
                "peak_time": row["peak_time"],
                "last_power_w": row["last_power_w"],
                "samples": row["samples"],
            }
            for row in daily_rows
        ]

    async def _load_home_consumption(
        self, db: aiosqlite.Connection, start: str, end: str
    ) -> float:
        try:
            async with db.execute(
                "SELECT COALESCE(SUM(home_consumption_kwh), 0)\n                   FROM stats_daily_energy\n                   WHERE date >= ? AND date <= ?",
                (start, end),
            ) as cursor:
                row = await cursor.fetchone()
        except Exception as error:
            _LOGGER.debug("Consumer atlas home consumption unavailable: %s", error)
            return 0.0
        return float(row[0] or 0) if row else 0.0

    async def _max_sort_order(self, db: aiosqlite.Connection) -> int:
        async with db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM stats_consumer_atlas_config"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def _live_power_by_consumer(
        self, configs: list[dict[str, Any]]
    ) -> dict[str, float]:
        power_by_consumer = {}
        for consumer_config in configs:
            sensor_entity_id = consumer_config.get("entity_id")
            sensor_state = (
                context.HASS.states.get(sensor_entity_id)
                if context.HASS and sensor_entity_id
                else None
            )
            if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
                power_by_consumer[consumer_config["consumer_id"]] = 0.0
            else:
                try:
                    power_by_consumer[consumer_config["consumer_id"]] = max(
                        0.0, float(sensor_state.state)
                    )
                except (TypeError, ValueError):
                    power_by_consumer[consumer_config["consumer_id"]] = 0.0
                    continue
        return power_by_consumer

    def _runtime_config(self) -> dict[str, Any]:
        for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
            if not isinstance(runtime_data, dict):
                continue
            if not "config" in runtime_data:
                continue
            return runtime_data["config"]
        return {}


class ConsumerAtlasBackfillView(HomeAssistantView):
    """Backfill consumer atlas energy from Home Assistant recorder."""

    url = "/api/sfml_stats/consumer_atlas/backfill"
    name = "api:sfml_stats:consumer_atlas_backfill"
    requires_auth = False

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        """Backfill selected or all consumer atlas entries."""
        return web.json_response(
            _stable_api_error(
                "consumer_atlas_backfill_disabled",
                "Consumer atlas recorder backfill has been replaced by per-consumer start_kwh values.",
                status="disabled",
            ),
            status=410,
        )

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        """Compatibility hook; the STATS startup gate owns this schema."""

    async def _ensure_config_columns(self, db: aiosqlite.Connection) -> None:
        """Compatibility hook; the STATS startup gate owns this schema."""

    async def _load_configs(
        self, db: aiosqlite.Connection, consumer_id: str | None
    ) -> list[dict[str, Any]]:
        where_clause = "WHERE enabled = 1"
        query_params = ()
        if consumer_id:
            where_clause += " AND consumer_id = ?"
            query_params = (consumer_id,)
        async with db.execute(
            f"SELECT consumer_id, name, entity_id, category, icon, color,\n                      enabled, sort_order\n               FROM stats_consumer_atlas_config\n               {where_clause}\n               ORDER BY sort_order, name",
            query_params,
        ) as cursor:
            config_rows = await cursor.fetchall()
        return [
            {
                "consumer_id": row["consumer_id"],
                "name": row["name"],
                "entity_id": row["entity_id"],
                "category": row["category"],
                "icon": row["icon"],
                "color": row["color"],
                "enabled": bool(row["enabled"]),
                "sort_order": row["sort_order"],
            }
            for row in config_rows
        ]

    async def _calculate_day_from_recorder(
        self, entity_id: str, day: date, today: date
    ) -> dict[str, Any] | None:
        day_start = datetime.combine(day, time.min, tzinfo=ha_timezone(context.HASS))
        if day == today:
            range_end = ha_now(context.HASS) - timedelta(minutes=10)
            if range_end <= day_start:
                return
        else:
            range_end = day_start + timedelta(days=1)
        recorded_states = await self._recorder_states(entity_id, day_start, range_end)
        if not recorded_states:
            return
        energy_wh = 0.0
        peak_watts = 0.0
        peak_time = None
        last_power_watts = 0.0
        sample_count = 0
        for state_index, sensor_state in enumerate(recorded_states):
            try:
                watts = max(0.0, float(sensor_state.state))
            except (TypeError, ValueError):
                continue
            state_time = self._range_time(sensor_state.last_changed)
            interval_start = max(state_time, day_start)
            if state_index + 1 < len(recorded_states):
                next_state_time = self._range_time(
                    recorded_states[state_index + 1].last_changed
                )
                interval_end = min(next_state_time, range_end)
            else:
                interval_end = range_end
            if interval_end <= interval_start:
                continue
            energy_wh += watts * (
                (interval_end - interval_start).total_seconds() / 3600
            )
            sample_count += 1
            last_power_watts = watts
            if watts > peak_watts:
                peak_watts = watts
                peak_time = state_time.strftime("%H:%M")
        if sample_count <= 0:
            return
        return {
            "kwh": round(energy_wh / 1000, 4),
            "peak_w": round(peak_watts, 1),
            "peak_time": peak_time,
            "last_power_w": round(last_power_watts, 1),
            "samples": sample_count,
        }

    async def _recorder_states(
        self, entity_id: str, start_time: datetime, end_time: datetime
    ) -> list[Any]:
        try:
            from homeassistant.components.recorder import (
                get_instance as get_recorder_instance,
            )
            from homeassistant.components.recorder.history import (
                get_significant_states as get_significant_states,
                state_changes_during_period as state_changes_during_period,
            )

            recorder = get_recorder_instance(context.HASS)
            normalized_entity_id = entity_id.lower()
            recorded_states = []
            try:
                states_by_entity = await recorder.async_add_executor_job(
                    state_changes_during_period,
                    context.HASS,
                    start_time,
                    end_time,
                    entity_id,
                    True,
                    False,
                    None,
                )
                recorded_states = states_by_entity.get(
                    normalized_entity_id, []
                ) or states_by_entity.get(entity_id, [])
            except Exception as error:
                _LOGGER.debug(
                    "Consumer atlas state_changes query failed for %s from %s to %s: %s",
                    entity_id,
                    start_time,
                    end_time,
                    error,
                )
            if not recorded_states:
                states_by_entity = await recorder.async_add_executor_job(
                    get_significant_states,
                    context.HASS,
                    start_time,
                    end_time,
                    [entity_id],
                    None,
                    True,
                )
                recorded_states = states_by_entity.get(
                    normalized_entity_id, []
                ) or states_by_entity.get(entity_id, [])
            return sorted(
                recorded_states, key=lambda state: self._range_time(state.last_changed)
            )
        except ImportError as error:
            _LOGGER.debug(
                "Recorder not available for consumer atlas backfill: %s", error
            )
        except Exception as error:
            _LOGGER.debug(
                "Consumer atlas recorder query failed for %s from %s to %s: %s",
                entity_id,
                start_time,
                end_time,
                error,
            )
        if True:
            return []

    def _range_time(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=ha_timezone(context.HASS))
        return as_ha_local(value, context.HASS)

    async def _replace_daily_row(
        self,
        db: aiosqlite.Connection,
        day: date,
        consumer_id: str,
        result: dict[str, Any],
    ) -> None:
        await db.execute(
            "DELETE FROM stats_consumer_atlas_daily WHERE date = ? AND consumer_id = ?",
            (day.isoformat(), consumer_id),
        )
        await db.execute(
            "INSERT INTO stats_consumer_atlas_daily (\n                   date, consumer_id, kwh, peak_w, peak_time,\n                   last_power_w, samples, updated_at\n               ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                day.isoformat(),
                consumer_id,
                result["kwh"],
                result["peak_w"],
                result["peak_time"],
                result["last_power_w"],
                result["samples"],
            ),
        )

    def _payload_date(self, value: Any, fallback: date) -> date:
        if not value:
            return fallback
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return fallback

    def _runtime_config(self) -> dict[str, Any]:
        for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
            if not isinstance(runtime_data, dict):
                continue
            if not "config" in runtime_data:
                continue
            return runtime_data["config"]
        return {}


class ConsumerAtlasItemView(HomeAssistantView):
    """API for a single consumer atlas entry."""

    url = "/api/sfml_stats/consumer_atlas/{consumer_id}"
    name = "api:sfml_stats:consumer_atlas_item"
    requires_auth = False

    @local_only
    async def delete(self, request: web.Request, consumer_id: str) -> web.Response:
        normalized_consumer_id = sanitize_consumer_config({"consumer_id": consumer_id})[
            "consumer_id"
        ]
        try:
            async with _get_db() as connection:
                await connection.execute(
                    "DELETE FROM stats_consumer_atlas_config WHERE consumer_id = ?",
                    (normalized_consumer_id,),
                )
                await connection.commit()
            return web.json_response(
                {"success": True, "consumer_id": normalized_consumer_id}
            )
        except Exception as error:
            _LOGGER.error(
                "Error deleting consumer atlas entry: %s", error, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


class AmortizationView(HomeAssistantView):
    """API for PV amortization and payback projection. @zara"""

    url = "/api/sfml_stats/amortization"
    name = "api:sfml_stats:amortization"
    requires_auth = False
    SETTINGS_KEY = "amortization"
    CONFIG_TO_SETTINGS = {
        CONF_AMORTIZATION_INVESTMENT_EUR: "investment_eur",
        CONF_AMORTIZATION_SUBSIDY_EUR: "subsidy_eur",
        CONF_AMORTIZATION_COMMISSIONING_DATE: "commissioning_date",
        CONF_AMORTIZATION_ANNUAL_RUNNING_COSTS_EUR: "annual_running_costs_eur",
        CONF_AMORTIZATION_PRICE_INCREASE_PERCENT: "electricity_price_increase_percent",
        CONF_AMORTIZATION_DEGRADATION_PERCENT: "degradation_percent",
    }

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return amortization settings, cashflow history and projection."""
        try:
            settings = await self._load_settings()
            monthly_cashflows = await self._load_monthly_cashflows()
            projection = calculate_amortization(
                monthly_cashflows, settings, today=ha_today(context.HASS)
            )
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    **projection,
                }
            )
        except Exception as error:
            _LOGGER.error("Error calculating amortization: %s", error, exc_info=True)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        """Save amortization settings and return the updated projection."""
        try:
            payload = await request.json() if request.body_exists else {}
            existing_settings = await self._load_settings()
            settings = sanitize_amortization_settings(payload, existing_settings)
            await self._save_settings(settings)
            monthly_cashflows = await self._load_monthly_cashflows()
            projection = calculate_amortization(
                monthly_cashflows, settings, today=ha_today(context.HASS)
            )
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "time_context": _time_context(),
                    **projection,
                }
            )
        except Exception as error:
            _LOGGER.error(
                "Error saving amortization settings: %s", error, exc_info=True
            )
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    async def _ensure_settings_table(self, db: aiosqlite.Connection) -> None:
        """Settings schema is created by the integration startup gate."""

    async def _load_settings(self) -> dict[str, Any]:
        runtime_config, tariff_manager = self._runtime_config_and_tariff_manager()
        config_settings = self._settings_from_config(runtime_config)
        async with _get_db() as connection:
            await self._ensure_settings_table(connection)
            async with connection.execute(
                "SELECT value FROM stats_settings WHERE key = ?", (self.SETTINGS_KEY,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row or not row[0]:
            return sanitize_amortization_settings(config_settings)
        try:
            stored_settings = json.loads(row[0])
        except (TypeError, ValueError):
            stored_settings = {}
        if config_settings.get("investment_eur", 0) > 0:
            return sanitize_amortization_settings(config_settings)
        return sanitize_amortization_settings(stored_settings, config_settings)

    async def _save_settings(self, settings: dict[str, Any]) -> None:
        self._save_settings_to_config(settings)
        async with _get_db() as connection:
            await self._ensure_settings_table(connection)
            await connection.execute(
                "INSERT INTO stats_settings (key, value, updated_at)\n                   VALUES (?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(key) DO UPDATE SET\n                       value = excluded.value,\n                       updated_at = CURRENT_TIMESTAMP",
                (self.SETTINGS_KEY, json.dumps(settings, sort_keys=True)),
            )
            await connection.commit()
        return None if True else None

    def _settings_from_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        settings = dict(DEFAULT_AMORTIZATION_SETTINGS)
        for config_key, setting_key in self.CONFIG_TO_SETTINGS.items():
            if config_key in cfg:
                settings[setting_key] = cfg.get(config_key)
        return sanitize_amortization_settings(settings)

    def _save_settings_to_config(self, settings: dict[str, Any]) -> None:
        if context.HASS is None:
            return
        entry_candidates = []
        for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
            if isinstance(runtime_data, dict) and "config_entry" in runtime_data:
                entry_candidates.append((runtime_data["config_entry"], runtime_data))
        if not entry_candidates:
            for config_entry in context.HASS.config_entries.async_entries(DOMAIN):
                entry_candidates.append((config_entry, None))
        for config_entry, runtime_data in entry_candidates:
            updated_config = {**config_entry.data}
            for config_key, setting_key in self.CONFIG_TO_SETTINGS.items():
                setting_value = settings.get(setting_key)
                if setting_value is None or setting_value == "":
                    updated_config.pop(config_key, None)
                else:
                    updated_config[config_key] = setting_value
            context.HASS.config_entries.async_update_entry(
                config_entry, data=updated_config
            )
            if isinstance(runtime_data, dict):
                runtime_data["config"] = updated_config
            break
        return None if True else None

    def _runtime_config_and_tariff_manager(self) -> tuple[dict[str, Any], Any]:
        runtime_config = {}
        tariff_manager = None
        for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
            if not isinstance(runtime_data, dict):
                continue
            if not runtime_config and "config" in runtime_data:
                runtime_config = runtime_data["config"]
            if tariff_manager is None and "monthly_tariff_manager" in runtime_data:
                tariff_manager = runtime_data["monthly_tariff_manager"]
        return (runtime_config, tariff_manager)

    async def _load_monthly_cashflows(self) -> list[dict[str, Any]]:
        runtime_config, tariff_manager = self._runtime_config_and_tariff_manager()
        try:
            default_import_price_ct = float(_get_fixed_total_price(runtime_config))
        except (TypeError, ValueError):
            default_import_price_ct = (
                DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
            )
        try:
            default_feed_in_tariff_ct = float(
                runtime_config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
            )
        except (TypeError, ValueError):
            default_feed_in_tariff_ct = DEFAULT_FEED_IN_TARIFF
        price_mode = effective_price_mode(context.HASS, runtime_config)
        default_feed_in_tariff_ct = max(0.0, default_feed_in_tariff_ct)
        corrected_daily_values = await _corrected_daily_energy_values("2025-01-01")
        async with _get_db() as connection:
            async with connection.execute(
                "SELECT strftime('%Y-%m', date) as month,\n                          SUM(solar_to_house_kwh) as solar_to_house,\n                          SUM(battery_to_house_kwh) as battery_to_house,\n                          SUM(grid_to_battery_kwh) as grid_to_battery,\n                          SUM(smartmeter_export_kwh) as smartmeter_export,\n                          SUM(grid_export_kwh) as grid_export,\n                          COUNT(*) as days\n                   FROM stats_daily_energy\n                   WHERE date >= '2025-01-01'\n                   GROUP BY month\n                   ORDER BY month"
            ) as cursor:
                monthly_rows = await cursor.fetchall()
            hourly_billing_by_month = {}
            try:
                billing_columns = await _table_columns(
                    connection, "stats_hourly_billing"
                )
                price_expression = (
                    "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
                    if "price_ct_kwh" in billing_columns
                    else "?"
                )
                async with connection.execute(
                    f"SELECT strftime('%Y-%m', date) as month,\n                              SUM(COALESCE(feed_in_revenue_ct, 0)) / 100.0 as feed_in_revenue_eur,\n                              SUM(\n                                  (\n                                      COALESCE(solar_to_house_kwh, 0)\n                                      + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                  )\n                                  * {price_expression}\n                              ) / 100.0 as self_savings_eur,\n                              SUM(\n                                  COALESCE(solar_to_house_kwh, 0)\n                                  + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                              ) as self_consumption_kwh,\n                              SUM(grid_export_kwh) as export_kwh,\n                              COUNT(*) as hours\n                       FROM stats_hourly_billing\n                       GROUP BY month\n                       ORDER BY month",
                    (default_import_price_ct,),
                ) as cursor:
                    for billing_or_energy_row in await cursor.fetchall():
                        if billing_or_energy_row[0]:
                            if (billing_or_energy_row[5] or 0) > 0:
                                hourly_billing_by_month[billing_or_energy_row[0]] = {
                                    "feed_in_revenue_eur": float(
                                        billing_or_energy_row[1] or 0
                                    ),
                                    "self_savings_eur": float(
                                        billing_or_energy_row[2] or 0
                                    ),
                                    "self_consumption_kwh": float(
                                        billing_or_energy_row[3] or 0
                                    ),
                                    "export_kwh": float(billing_or_energy_row[4] or 0),
                                    "hours": float(billing_or_energy_row[5] or 0),
                                }
            except Exception as error:
                _LOGGER.debug("Amortization hourly billing data unavailable: %s", error)
            hourly_component_days = None
            if any(
                (
                    energy_flows_changed(daily_values)
                    for daily_values in corrected_daily_values.values()
                )
            ):
                try:
                    hourly_component_days = await load_hourly_component_days(
                        connection, "2025-01-01", None, default_import_price_ct
                    )
                except Exception:
                    _LOGGER.debug("Amortization correction base unavailable")
                    hourly_component_days = None
        monthly_cashflows = []
        for billing_or_energy_row in monthly_rows:
            month_key = billing_or_energy_row[0]
            if not month_key:
                continue
            year, month_number = (int(date_part) for date_part in month_key.split("-"))
            month_import_price_ct = default_import_price_ct
            month_feed_in_tariff_ct = default_feed_in_tariff_ct
            tariff_source = "config_fallback"
            if tariff_manager is not None:
                try:
                    monthly_fallbacks = await tariff_manager.get_monthly_fallbacks(
                        year,
                        month_number,
                        import_price_ct=default_import_price_ct,
                        feed_in_tariff_ct=default_feed_in_tariff_ct,
                        base_fee_eur=0,
                    )
                    import_price_fallback = monthly_fallbacks["import_price_ct"]
                    feed_in_tariff_fallback = monthly_fallbacks["feed_in_tariff_ct"]
                    month_import_price_ct = float(
                        import_price_fallback["value"]
                        if import_price_fallback["value"] is not None
                        else default_import_price_ct
                    )
                    month_feed_in_tariff_ct = float(
                        feed_in_tariff_fallback["value"] or default_feed_in_tariff_ct
                    )
                    tariff_source = import_price_fallback.get("source", "fixed_month")
                except Exception as error:
                    _LOGGER.debug(
                        "Amortization monthly tariff fallback failed for %s: %s",
                        month_key,
                        error,
                    )
            battery_own_supply_kwh = max(
                0.0,
                float(billing_or_energy_row[2] or 0)
                - float(billing_or_energy_row[3] or 0),
            )
            self_consumption_kwh = (
                float(billing_or_energy_row[1] or 0) + battery_own_supply_kwh
            )
            export_kwh = (
                float(billing_or_energy_row[4] or 0)
                if (billing_or_energy_row[4] or 0) > 0
                else float(billing_or_energy_row[5] or 0)
            )
            corrected_export_values = [
                daily_values["grid_export_day_kwh"]
                for day_key, daily_values in corrected_daily_values.items()
                if day_key.startswith(f"{month_key}-")
            ]
            if corrected_export_values:
                export_kwh = sum(corrected_export_values)
            month_daily_values = [
                (day_key, daily_values)
                for day_key, daily_values in corrected_daily_values.items()
                if day_key.startswith(f"{month_key}-")
            ]
            flows_changed = any(
                (
                    energy_flows_changed(daily_values)
                    for day_key, daily_values in month_daily_values
                )
            )
            if flows_changed:
                self_consumption_kwh = sum(
                    (
                        own_supply_kwh(
                            daily_values.get("solar_to_house_day_kwh", 0.0),
                            daily_values.get("battery_to_house_day_kwh", 0.0),
                            daily_values.get("grid_to_battery_day_kwh", 0.0),
                        )
                        for day_key, daily_values in month_daily_values
                    )
                )
            hourly_billing = hourly_billing_by_month.get(month_key)
            if is_fixed_like(price_mode):
                self_savings_eur = self_consumption_kwh * month_import_price_ct / 100
                feed_in_revenue_eur = export_kwh * month_feed_in_tariff_ct / 100
                cashflow_source = "fixed"
            elif hourly_billing:
                legacy_month_costs = legacy_dynamic_month(
                    import_kwh=0.0,
                    covered_import_kwh=0.0,
                    covered_import_cost=0.0,
                    self_kwh=self_consumption_kwh,
                    covered_self_kwh=hourly_billing["self_consumption_kwh"],
                    covered_self_savings=hourly_billing["self_savings_eur"],
                    export_kwh=export_kwh,
                    covered_export_kwh=hourly_billing["export_kwh"],
                    covered_export_revenue=hourly_billing["feed_in_revenue_eur"],
                    month_price_ct=month_import_price_ct,
                    month_feed_in_ct=month_feed_in_tariff_ct,
                )
                if flows_changed and hourly_component_days is not None:
                    month_costs = dynamic_month_costs(
                        [
                            component_day(
                                daily_values, hourly_component_days.get(day_key)
                            )
                            for day_key, daily_values in month_daily_values
                        ],
                        changed=True,
                        legacy=legacy_month_costs,
                        month_price_ct=month_import_price_ct,
                        month_feed_in_ct=month_feed_in_tariff_ct,
                    )
                else:
                    month_costs = legacy_month_costs
                self_savings_eur = month_costs["self_savings"]
                feed_in_revenue_eur = month_costs["feed_in_revenue"]
                uncovered_self_kwh = max(
                    0.0, self_consumption_kwh - hourly_billing["self_consumption_kwh"]
                )
                uncovered_export_kwh = max(
                    0.0, export_kwh - hourly_billing["export_kwh"]
                )
                cashflow_source = (
                    "dynamic"
                    if uncovered_self_kwh <= 0.001 and uncovered_export_kwh <= 0.001
                    else "hybrid"
                )
            else:
                self_savings_eur = self_consumption_kwh * month_import_price_ct / 100
                feed_in_revenue_eur = export_kwh * month_feed_in_tariff_ct / 100
                cashflow_source = "estimated"
            benefit_eur = self_savings_eur + feed_in_revenue_eur
            monthly_cashflows.append(
                {
                    "month": month_key,
                    "benefit_eur": round(benefit_eur, 2),
                    "self_consumption_savings_eur": round(self_savings_eur, 2),
                    "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
                    "self_consumption_kwh": round(self_consumption_kwh, 2),
                    "grid_export_kwh": round(export_kwh, 2),
                    "avg_price_ct": round(month_import_price_ct, 2),
                    "feed_in_tariff_ct": round(month_feed_in_tariff_ct, 2),
                    "tariff_source": tariff_source,
                    "source": cashflow_source,
                }
            )
            continue
        return monthly_cashflows


def _eai_capability_snapshot(hass) -> dict | None:
    eai_runtime_data = hass.data.get("solar_forecast_eai")
    if not isinstance(eai_runtime_data, dict):
        return
    capability_provider = eai_runtime_data.get("capability_provider")
    snapshot_method = getattr(capability_provider, "snapshot", None)
    if capability_provider is None or not callable(snapshot_method):
        return
    try:
        snapshot = snapshot_method()
    except Exception:
        return
    return snapshot if isinstance(snapshot, dict) else None


class ConsumerDetailView(HomeAssistantView):
    """API for consumer detail data (WP, Heizstab, Wallbox) — live sensor values. @zara"""

    url = "/api/sfml_stats/consumers/detail"
    name = "api:sfml_stats:consumers:detail"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return live consumer detail data from configured sensors. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            consumer_config = {}
            for runtime_data in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(runtime_data, dict):
                    if "config" in runtime_data:
                        consumer_config = runtime_data["config"]
                        break
            from ..core.eai_wp_sources import (
                resolved_heatpump_entity as resolved_heatpump_entity,
            )

            def read(key):
                sensor_entity_id = resolved_heatpump_entity(
                    context.HASS, consumer_config, key
                )
                if not sensor_entity_id:
                    return
                sensor_state = context.HASS.states.get(sensor_entity_id)
                if sensor_state is None or sensor_state.state in (
                    "unknown",
                    "unavailable",
                ):
                    return None
                return sensor_state.state

            def read_float(key):
                sensor_value = read(key)
                if sensor_value is None:
                    return
                try:
                    return round(float(sensor_value), 2)
                except (ValueError, TypeError):
                    return

            def read_bool(key):
                sensor_value = read(key)
                if sensor_value is None:
                    return
                return sensor_value in ("on", "true", "True", "1", "heat", "heating")

            daily_consumer_energy = {
                "consumer_heatpump_kwh": 0.0,
                "consumer_heatingrod_kwh": 0.0,
                "consumer_wallbox_kwh": 0.0,
            }
            async with _get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT consumer_heatpump_kwh,\n                           consumer_heatingrod_kwh,\n                           consumer_wallbox_kwh\n                    FROM stats_daily_energy\n                    WHERE date = ?\n                    ",
                    (ha_today(context.HASS).isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
            if row is not None:
                daily_consumer_energy.update(dict(row))
            heatpump_configured = any(
                (
                    read(sensor_config_key) is not None
                    or resolved_heatpump_entity(
                        context.HASS, consumer_config, sensor_config_key
                    )
                    for sensor_config_key in (
                        CONF_SENSOR_HEATPUMP_POWER,
                        CONF_SENSOR_HP_HEATING_MODE,
                        CONF_SENSOR_HP_STORAGE_TEMP,
                    )
                )
            )
            heatpump_grid_kwh = read_float(CONF_SENSOR_HP_GRID_ENERGY_DAILY)
            heatpump_pv_kwh = read_float(CONF_SENSOR_HP_PV_ENERGY_DAILY)
            heatpump_today_kwh, daily_energy_source = resolve_heatpump_today_kwh(
                eai_kwh=eai_electric_today_kwh(_eai_capability_snapshot(context.HASS)),
                daily_sensor_kwh=read_float(CONF_SENSOR_HEATPUMP_DAILY),
                watt_integrated_kwh=daily_consumer_energy["consumer_heatpump_kwh"],
            )
            heatpump_energy_split = split_heatpump_today_energy(
                heatpump_today_kwh, heatpump_pv_kwh, heatpump_grid_kwh
            )
            heatpump_detail = {
                "configured": heatpump_configured,
                "live": {
                    "heating_mode": read(CONF_SENSOR_HP_HEATING_MODE),
                    "dhw_mode": read(CONF_SENSOR_HP_DHW_MODE),
                    "dhw_charging": read_bool(CONF_SENSOR_HP_DHW_CHARGING),
                    "pv_active": read_bool(CONF_SENSOR_HP_PV_ACTIVE),
                    "electric_power": _get_power_kw(
                        resolved_heatpump_entity(
                            context.HASS, consumer_config, CONF_SENSOR_HP_ELECTRIC_POWER
                        )
                    ),
                    "thermal_power": _get_power_kw(
                        resolved_heatpump_entity(
                            context.HASS, consumer_config, CONF_SENSOR_HP_THERMAL_POWER
                        )
                    ),
                    "grid_energy_today": heatpump_energy_split["grid_energy_today"],
                    "pv_energy_today": heatpump_energy_split["pv_energy_today"],
                    "unassigned_energy_today": heatpump_energy_split[
                        "unassigned_energy_today"
                    ],
                    "pv_share_percent": heatpump_energy_split["pv_share_percent"],
                    "grid_share_percent": heatpump_energy_split["grid_share_percent"],
                    "unassigned_share_percent": heatpump_energy_split[
                        "unassigned_share_percent"
                    ],
                    "jaz": read_float(CONF_SENSOR_HP_JAZ),
                    "compressor_starts": read_float(CONF_SENSOR_HP_COMPRESSOR_STARTS),
                    "storage_temp": read_float(CONF_SENSOR_HP_STORAGE_TEMP),
                    "power": _display_watts(
                        _get_power_w(
                            resolved_heatpump_entity(
                                context.HASS,
                                consumer_config,
                                CONF_SENSOR_HEATPUMP_POWER,
                            )
                        )
                    ),
                    "daily_kwh": heatpump_energy_split["daily_kwh"],
                    "daily_energy_source": daily_energy_source,
                    "cop": read_float(CONF_SENSOR_HEATPUMP_COP),
                },
            }
            heatingrod_configured = bool(
                consumer_config.get(CONF_SENSOR_HEATINGROD_POWER)
            )
            heatingrod_detail = {
                "configured": heatingrod_configured,
                "live": {
                    "power": _display_watts(
                        _get_power_w(consumer_config.get(CONF_SENSOR_HEATINGROD_POWER))
                    ),
                    "daily_kwh": round(
                        daily_consumer_energy["consumer_heatingrod_kwh"] or 0, 2
                    ),
                },
            }
            wallbox_configured = bool(consumer_config.get(CONF_SENSOR_WALLBOX_POWER))
            wallbox_detail = {
                "configured": wallbox_configured,
                "live": {
                    "power": _display_watts(
                        _get_power_w(consumer_config.get(CONF_SENSOR_WALLBOX_POWER))
                    ),
                    "daily_kwh": round(
                        daily_consumer_energy["consumer_wallbox_kwh"] or 0, 2
                    ),
                    "state": read(CONF_SENSOR_WALLBOX_STATE),
                    "charge_mode": read(CONF_SENSOR_WB_CHARGE_MODE),
                    "session_kwh": read_float(CONF_SENSOR_WB_ENERGY_SESSION),
                },
            }
            return web.json_response(
                {
                    "success": True,
                    "heatpump": heatpump_detail,
                    "heatingrod": heatingrod_detail,
                    "wallbox": wallbox_detail,
                }
            )
        except Exception as error:
            _LOGGER.error("Error in consumer detail: %s", error, exc_info=True)
            return web.json_response({"error": str(error)}, status=500)
