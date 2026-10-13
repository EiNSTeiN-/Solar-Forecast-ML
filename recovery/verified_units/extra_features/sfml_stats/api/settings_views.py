# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Settings dashboard view."""
from __future__ import annotations
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import _get_db, _time_context, local_only, local_or_trusted_cloud_only
from ..utils.time_utils import ha_today
from ..core.price_mode import (
    MODE_GPM_DYNAMIC,
    MODE_GPM_FIXED,
    MODE_LEGACY_FIXED,
    MODE_NONE,
    effective_price_mode,
    is_fixed_like,
)
from ..const import (
    DOMAIN,
    VERSION,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_INVERTER_AC_OUTPUT,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_WEATHER_ENTITY,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_BILLING_BASE_FEE,
    CONF_SMART_CHARGING_ENABLED,
    CONF_BATTERY_CAPACITY,
    CONF_MIN_SOC,
    CONF_MAX_SOC,
    CONF_BATTERY_SOC_SENSOR,
    DEFAULT_MIN_SOC,
    DEFAULT_MAX_SOC,
    configured_battery_capacity_kwh,
    CONF_SENSOR_GRID_IMPORT_EXTRA,
    CONF_COUNTRY,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    SOLAR_FORECAST_DB,
)

_LOGGER = logging.getLogger(__name__)


class SettingsDashboardView(HomeAssistantView):
    """API for Settings page — aggregated configuration + status. @zara"""

    url = "/api/sfml_stats/settings/dashboard"
    name = "api:sfml_stats:settings:dashboard"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return all settings/config data for the Settings page. @zara"""
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            config = {}
            for entry_data in context.HASS.data.get(DOMAIN, {}).values():
                if isinstance(entry_data, dict):
                    if "config" in entry_data:
                        config = entry_data["config"]
                        break
            sensor_definitions = [
                (CONF_SENSOR_HOME_CONSUMPTION, "Hausverbrauch (W)"),
                (CONF_SENSOR_SOLAR_TO_HOUSE, "Solar → Haus (W)"),
                (CONF_SENSOR_INVERTER_AC_OUTPUT, "Wechselrichter-Ausgang AC (W)"),
                (CONF_SENSOR_SOLAR_TO_BATTERY, "Solar → Batterie (W)"),
                (CONF_SENSOR_BATTERY_TO_HOUSE, "Batterie → Haus (W)"),
                (CONF_SENSOR_BATTERY_SOC, "Batterie SoC (%)"),
                (CONF_SENSOR_BATTERY_POWER, "Batterie Leistung (W)"),
                (CONF_SENSOR_GRID_TO_HOUSE, "Netz → Haus (W)"),
                (CONF_SENSOR_GRID_TO_BATTERY, "Netz → Batterie (W)"),
                (CONF_SENSOR_HOUSE_TO_GRID, "Haus → Netz (W)"),
                (CONF_SENSOR_SMARTMETER_IMPORT, "Smartmeter Bezug (W)"),
                (CONF_SENSOR_SMARTMETER_EXPORT, "Smartmeter Einspeisung (W)"),
                (CONF_SENSOR_GRID_IMPORT_EXTRA, "Zusätzlicher Netzbezug (kWh/Tag)"),
                (CONF_WEATHER_ENTITY, "Wetter"),
                (CONF_SENSOR_HEATPUMP_POWER, "Wärmepumpe Leistung"),
                (CONF_SENSOR_HEATINGROD_POWER, "Heizstab Leistung"),
                (CONF_SENSOR_WALLBOX_POWER, "Wallbox Leistung"),
                (CONF_SENSOR_WALLBOX_STATE, "Wallbox Status"),
            ]
            from ..core.eai_wp_sources import (
                resolved_heatpump_entity as resolved_heatpump_entity,
            )

            configured_sensors = []
            for config_key, friendly_name in sensor_definitions:
                entity_id = (
                    resolved_heatpump_entity(context.HASS, config, config_key)
                    if config_key == CONF_SENSOR_HEATPUMP_POWER
                    else config.get(config_key)
                )
                if not entity_id:
                    continue
                state = context.HASS.states.get(entity_id)
                available = state is not None and state.state not in (
                    "unknown",
                    "unavailable",
                )
                configured_sensors.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "state": state.state if available else None,
                        "available": available,
                    }
                )
            price_mode = effective_price_mode(context.HASS, config)
            price_mode_labels = {
                MODE_GPM_DYNAMIC: "Dynamisch (GPM)",
                MODE_GPM_FIXED: "Fest (GPM)",
                MODE_LEGACY_FIXED: "Fest",
                MODE_NONE: "Kein Tarif",
            }
            energy_price_ct = None
            grid_fees_ct = None
            if is_fixed_like(price_mode):
                raw_work_price_ct = config.get(CONF_BILLING_WORK_PRICE)
                raw_grid_fees_ct = config.get(CONF_BILLING_GRID_FEES)
                if raw_work_price_ct is not None:
                    energy_price_ct = round(raw_work_price_ct, 2)
                if raw_grid_fees_ct is not None:
                    grid_fees_ct = round(raw_grid_fees_ct, 2)
                if energy_price_ct is None and CONF_BILLING_FIXED_PRICE in config:
                    energy_price_ct = round(config[CONF_BILLING_FIXED_PRICE], 2)
            raw_base_fee_eur = config.get(CONF_BILLING_BASE_FEE)
            base_fee_eur = (
                round(raw_base_fee_eur, 2) if raw_base_fee_eur is not None else None
            )
            vat_percent = 19 if config.get(CONF_COUNTRY, "DE") == "DE" else 20
            price_settings = {
                "country": config.get(CONF_COUNTRY, "DE"),
                "vat": vat_percent,
                "mode": price_mode_labels.get(price_mode, price_mode),
                "energy_price": energy_price_ct,
                "grid_fees": grid_fees_ct,
                "base_fee": base_fee_eur,
                "feed_in_tariff": round(
                    config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF), 2
                ),
            }
            smart_charging_enabled = bool(
                config.get(CONF_SMART_CHARGING_ENABLED, False)
            )
            soc_entity = config.get(CONF_BATTERY_SOC_SENSOR) or config.get(
                CONF_SENSOR_BATTERY_SOC
            )
            smart_charging = {
                "enabled": smart_charging_enabled,
                "capacity": configured_battery_capacity_kwh(
                    config.get(CONF_BATTERY_CAPACITY)
                ),
                "min_soc": config.get(
                    CONF_MIN_SOC, DEFAULT_MIN_SOC if smart_charging_enabled else None
                ),
                "max_soc": config.get(
                    CONF_MAX_SOC, DEFAULT_MAX_SOC if smart_charging_enabled else None
                ),
                "soc_sensor": soc_entity,
                "soc_sensor_configured": bool(soc_entity),
                "current_soc": None,
                "target_soc": None,
                "active": None,
                "reason": None,
                "is_cheap": None,
                "current_price": None,
            }
            try:
                for coordinator_entry_data in context.HASS.data.get(
                    DOMAIN, {}
                ).values():
                    if isinstance(coordinator_entry_data, dict):
                        gpm_coordinator = coordinator_entry_data.get("gpm_coordinator")
                        if gpm_coordinator is not None:
                            if gpm_coordinator.data:
                                gpm_data = gpm_coordinator.data
                                smart_charging["is_cheap"] = gpm_data.get("is_cheap")
                                smart_charging["current_price"] = gpm_data.get(
                                    "total_price"
                                )
                                smart_charging["current_soc"] = gpm_data.get(
                                    "smart_charging_current_soc"
                                )
                                smart_charging["target_soc"] = gpm_data.get(
                                    "smart_charging_target_soc"
                                )
                                smart_charging["active"] = gpm_data.get(
                                    "smart_charging_active"
                                )
                                smart_charging["reason"] = gpm_data.get(
                                    "smart_charging_reason"
                                )
                                break
            except Exception as error:
                _LOGGER.debug("Smart charging live state unavailable: %s", error)
            if smart_charging["current_soc"] is None and soc_entity:
                try:
                    soc_state = context.HASS.states.get(soc_entity)
                    if soc_state is not None and soc_state.state not in (
                        "unknown",
                        "unavailable",
                        None,
                    ):
                        smart_charging["current_soc"] = float(soc_state.state)
                except (ValueError, TypeError):
                    pass
            fallback_soc_entity = config.get(CONF_SENSOR_BATTERY_SOC)
            if (
                smart_charging["current_soc"] is None
                or smart_charging["current_soc"] == 0.0
            ) and fallback_soc_entity:
                try:
                    fallback_soc_state = context.HASS.states.get(fallback_soc_entity)
                    if (
                        fallback_soc_state is not None
                        and fallback_soc_state.state
                        not in ("unknown", "unavailable", None)
                    ):
                        fallback_soc_percent = float(fallback_soc_state.state)
                        if fallback_soc_percent > 1.0:
                            smart_charging["current_soc"] = fallback_soc_percent
                except (ValueError, TypeError):
                    pass
            panel_groups = []
            try:
                async with _get_db() as connection:
                    async with connection.execute(
                        "\n                        SELECT group_name, power_kwp, azimuth_deg, tilt_deg\n                        FROM astronomy_cache_panel_groups\n                        WHERE cache_date = (SELECT MAX(cache_date) FROM astronomy_cache_panel_groups)\n                        AND hour = 12\n                        ORDER BY group_name\n                    "
                    ) as cursor:
                        for row in await cursor.fetchall():
                            panel_groups.append(
                                {
                                    "name": row[0],
                                    "kwp": (
                                        round(row[1], 3) if row[1] is not None else 0
                                    ),
                                    "azimuth": (
                                        round(row[2], 0) if row[2] is not None else 0
                                    ),
                                    "tilt": (
                                        round(row[3], 0) if row[3] is not None else 0
                                    ),
                                    "module_count": None,
                                }
                            )
                    calibrations_by_group = {}
                    try:
                        async with connection.execute(
                            "SELECT group_name, global_factor, confidence FROM physics_calibration_groups"
                        ) as cursor:
                            for row in await cursor.fetchall():
                                calibrations_by_group[row[0]] = {
                                    "factor": (
                                        round(row[1], 3) if row[1] is not None else None
                                    ),
                                    "confidence": (
                                        round(row[2], 2) if row[2] is not None else None
                                    ),
                                }
                    except Exception:
                        pass
                    for panel_group in panel_groups:
                        panel_group.update(
                            calibrations_by_group.get(panel_group["name"], {})
                        )
                    existing_group_names = {
                        panel_group["name"] for panel_group in panel_groups
                    }
                    for group_name, calibration in sorted(
                        calibrations_by_group.items()
                    ):
                        if group_name in existing_group_names:
                            continue
                        panel_groups.append(
                            {
                                "name": group_name,
                                "kwp": None,
                                "azimuth": None,
                                "tilt": None,
                                "module_count": None,
                                **calibration,
                            }
                        )
            except Exception as error:
                _LOGGER.debug("Panel groups from DB unavailable: %s", error)
            system_info = await self._collect_system_info()
            from ..core.dev_mode import is_dev_active as is_dev_active

            ai_info = (
                await self._collect_ai_info() if is_dev_active(context.HASS) else {}
            )
            configured_consumers = []
            consumer_definitions = [
                (CONF_SENSOR_HEATPUMP_POWER, "heatpump"),
                (CONF_SENSOR_HEATINGROD_POWER, "heatingrod"),
                (CONF_SENSOR_WALLBOX_POWER, "wallbox"),
            ]
            for consumer_config_key, consumer_name in consumer_definitions:
                consumer_entity = (
                    resolved_heatpump_entity(context.HASS, config, consumer_config_key)
                    if consumer_config_key == CONF_SENSOR_HEATPUMP_POWER
                    else config.get(consumer_config_key)
                )
                if consumer_entity:
                    configured_consumers.append(
                        {
                            "config_key": consumer_name,
                            "power_entity_id": consumer_entity,
                            "daily_entity_id": None,
                            "mode": "integrated",
                        }
                    )
            return web.json_response(
                {
                    "success": True,
                    "sensors": configured_sensors,
                    "price": price_settings,
                    "smart_charging": smart_charging,
                    "panel_groups": panel_groups,
                    "system": system_info,
                    "ai": ai_info,
                    "configured_consumers": configured_consumers,
                    "time_context": _time_context(),
                }
            )
        except Exception as request_error:
            _LOGGER.error(
                "Error in settings dashboard: %s", request_error, exc_info=True
            )
            return web.json_response({"error": str(request_error)}, status=500)

    async def _collect_system_info(self) -> dict:
        """Collect SFML Stats + ML versions, DB stats, aggregation status. @zara"""
        system_info = {
            "stats_version": VERSION,
            "ml_version": None,
            "db_path": None,
            "db_status": "offline",
            "db_ok": False,
            "db_size": None,
            "db_size_bytes": None,
            "last_aggregation": None,
            "data_points": None,
            "healthy": True,
        }
        try:
            manifest_path = (
                Path(context.HASS.config.path())
                / "custom_components"
                / "solar_forecast_ml"
                / "manifest.json"
            )
            if manifest_path.exists():
                import json as json
                import aiofiles as aiofiles

                async with aiofiles.open(
                    manifest_path, "r", encoding="utf-8"
                ) as manifest_file:
                    manifest_text = await manifest_file.read()
                system_info["ml_version"] = json.loads(manifest_text).get("version")
        except Exception as error:
            _LOGGER.debug("Could not read SFML ML manifest: %s", error)
        try:
            database_path = Path(context.HASS.config.path()) / SOLAR_FORECAST_DB
            system_info["db_path"] = str(database_path)
            if database_path.exists():
                database_size_bytes = database_path.stat().st_size
                system_info["db_size_bytes"] = database_size_bytes
                if database_size_bytes >= 1073741824:
                    system_info["db_size"] = (
                        f"{database_size_bytes / 1073741824:.2f} GB"
                    )
                elif database_size_bytes >= 1048576:
                    system_info["db_size"] = f"{database_size_bytes / 1048576:.1f} MB"
                else:
                    system_info["db_size"] = f"{database_size_bytes / 1024:.0f} KB"
                system_info["db_status"] = "online"
                system_info["db_ok"] = True
        except Exception as error:
            _LOGGER.debug("DB size lookup failed: %s", error)
            system_info["healthy"] = False
        try:
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT MAX(date) FROM stats_daily_energy"
                ) as cursor:
                    row = await cursor.fetchone()
                    system_info["last_aggregation"] = row[0] if row else None
                async with connection.execute(
                    "SELECT (SELECT COUNT(*) FROM stats_daily_energy) + (SELECT COUNT(*) FROM stats_hourly_billing) + (SELECT COUNT(*) FROM stats_power_sources)"
                ) as cursor:
                    row = await cursor.fetchone()
                    system_info["data_points"] = int(row[0]) if row and row[0] else 0
        except Exception as error:
            _LOGGER.debug("Aggregation lookup failed: %s", error)
        return system_info if True else None

    async def _collect_ai_info(self) -> dict:
        """Collect Hubble AI stack meta: active model, accuracy, drift. @zara"""
        ai_info = {
            "active_model": None,
            "active_model_display": None,
            "version": None,
            "accuracy_percent": None,
            "rmse": None,
            "training_samples": None,
            "last_trained": None,
            "drift": None,
            "coordinator": None,
        }
        try:
            async with _get_db() as connection:
                try:
                    async with connection.execute(
                        "SELECT version, active_model, training_samples, last_trained, accuracy, rmse FROM ai_learned_weights_meta WHERE id = 1"
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            ai_info["version"] = row[0]
                            ai_info["active_model"] = row[1]
                            ai_info["active_model_display"] = (
                                "Hubble AI Stack" if row[1] else None
                            )
                            ai_info["training_samples"] = row[2]
                            ai_info["last_trained"] = row[3]
                            ai_info["accuracy_percent"] = (
                                round((row[4] or 0) * 100, 1)
                                if row[4] is not None
                                else None
                            )
                            ai_info["rmse"] = (
                                round(row[5], 4) if row[5] is not None else None
                            )
                except Exception as error:
                    _LOGGER.debug("ai_learned_weights_meta missing: %s", error)
                try:
                    async with connection.execute(
                        "SELECT COUNT(*), MAX(created_at), SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END), SUM(CASE WHEN severity='warning' THEN 1 ELSE 0 END) FROM drift_events WHERE event_date >= ?",
                        ((ha_today(context.HASS) - timedelta(days=7)).isoformat(),),
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            drift_event_count = int(row[0] or 0)
                            critical_count = int(row[2] or 0)
                            warning_count = int(row[3] or 0)
                            if critical_count > 0:
                                drift_state = "critical"
                            elif warning_count > 0:
                                drift_state = "warning"
                            elif drift_event_count > 0:
                                drift_state = "recovering"
                            else:
                                drift_state = "stable"
                            ai_info["drift"] = {
                                "state": drift_state,
                                "active_events": drift_event_count,
                                "total_events": drift_event_count,
                                "last_event": row[1],
                                "critical_count": critical_count,
                                "warning_count": warning_count,
                            }
                except Exception as error:
                    _LOGGER.debug("drift_events missing: %s", error)
                try:
                    async with connection.execute(
                        "SELECT expected_daily_production, last_set_date, last_updated FROM coordinator_state WHERE id = 1"
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            ai_info["coordinator"] = {
                                "expected_kwh_today": (
                                    round(row[0], 2) if row[0] is not None else None
                                ),
                                "last_set_date": row[1],
                                "last_updated": row[2],
                            }
                except Exception as error:
                    _LOGGER.debug("coordinator_state missing: %s", error)
        except Exception as error:
            _LOGGER.debug("AI info collection failed: %s", error)
        if True:
            return ai_info
        else:
            return ai_info


_HYBRID_FORECAST_MODES = frozenset(
    {"standard", "custom_time", "standard_midday", "standard_midday_afternoon"}
)
_HYBRID_CUSTOM_TIME = re.compile("^(?:[01]\\d|2[0-3]):[0-5]\\d$")
_HYBRID_SETTING_KEYS = frozenset({"mode", "enabled", "custom_time"})
_SFML_DOMAIN = "solar_forecast_ml"
_SFML_SET_REFORECAST_MODE = "set_operational_reforecast_mode"
_NO_STORE = {"Cache-Control": "no-store"}
_ABSENT_HYBRID_SETTINGS = {
    "mode": "standard",
    "custom_time": None,
    "enabled": False,
    "updated_at": None,
    "updated_by": None,
}


def _blank_to_none(value: Any) -> str | None:
    if value is None:
        return
    text = str(value).strip()
    return text or None


def _enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return False


def _settings_from_row(row: Any) -> dict[str, Any]:
    return {
        "mode": row["mode"],
        "custom_time": _blank_to_none(row["custom_time"]),
        "enabled": _enabled_flag(row["enabled"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def _missing_table(err: BaseException) -> bool:
    return "no such table" in str(err).lower()


async def _fetch_hybrid_settings(db: Any) -> dict[str, Any] | None:
    """Return the newest settings row, or None when the customer never set one."""
    try:
        async with db.execute(
            "\n            SELECT mode, custom_time, enabled, updated_at, updated_by\n            FROM ops_reforecast_settings\n            ORDER BY settings_id DESC\n            LIMIT 1\n            "
        ) as cursor:
            row = await cursor.fetchone()
    except aiosqlite.OperationalError as error:
        if _missing_table(error):
            return
        raise
    if row is None:
        return
    return _settings_from_row(row)


async def _fetch_runs_today(db: Any, today: str) -> list[dict[str, Any]]:
    try:
        async with db.execute(
            "\n            SELECT snapshot_type, trigger_source, triggered_at, cutoff_hour, status\n            FROM ops_forecast_snapshots\n            WHERE snapshot_date = ?\n            ORDER BY snapshot_sequence ASC\n            ",
            (today,),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as error:
        if _missing_table(error):
            return []
        raise
    return [
        {
            "snapshot_type": row["snapshot_type"],
            "trigger_source": row["trigger_source"],
            "triggered_at": row["triggered_at"],
            "cutoff_hour": row["cutoff_hour"],
            "status": row["status"],
        }
        for row in rows
    ]


async def _sum_kwh(db: Any, sql: str, params: tuple[Any, ...]) -> float | None:
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
    if row is None or not row["hour_count"]:
        return
    return float(row["total_kwh"] or 0.0)


async def _fetch_morning_anchor_kwh(db: Any, today: str) -> float | None:
    try:
        async with db.execute(
            "\n            SELECT snapshot_sequence\n            FROM ops_forecast_snapshots\n            WHERE snapshot_date = ?\n              AND snapshot_type = 'morning_snapshot'\n              AND status = 'completed'\n            ORDER BY snapshot_sequence DESC\n            LIMIT 1\n            ",
            (today,),
        ) as cursor:
            morning_snapshot_row = await cursor.fetchone()
        if morning_snapshot_row is None:
            return
        return await _sum_kwh(
            db,
            "\n            SELECT SUM(prediction_kwh) AS total_kwh, COUNT(*) AS hour_count\n            FROM ops_hourly_forecasts\n            WHERE snapshot_sequence = ?\n              AND target_date = ?\n            ",
            (morning_snapshot_row["snapshot_sequence"], today),
        )
    except aiosqlite.OperationalError as error:
        if _missing_table(error):
            return
        raise


async def _fetch_current_kwh(db: Any, today: str) -> float | None:
    try:
        return await _sum_kwh(
            db,
            "\n            SELECT SUM(prediction_kwh) AS total_kwh, COUNT(*) AS hour_count\n            FROM ops_hourly_forecasts\n            WHERE target_date = ?\n              AND is_active = 1\n            ",
            (today,),
        )
    except aiosqlite.OperationalError as error:
        if _missing_table(error):
            return
        raise


async def _hybrid_forecast_payload(db: Any, today: str) -> dict[str, Any]:
    stored_settings = await _fetch_hybrid_settings(db)
    settings = (
        dict(_ABSENT_HYBRID_SETTINGS) if stored_settings is None else stored_settings
    )
    return {
        "settings": settings,
        "runs_today": await _fetch_runs_today(db, today),
        "morning_anchor_kwh": await _fetch_morning_anchor_kwh(db, today),
        "current_kwh": await _fetch_current_kwh(db, today),
        "date": today,
    }


def _validate_hybrid_payload(
    payload: Any,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    if not isinstance(payload, dict):
        return (None, {"body": "invalid_json"})
    errors = {}
    for unexpected_key in sorted(set(payload) - _HYBRID_SETTING_KEYS):
        errors[unexpected_key] = "unexpected_key"
    mode = payload.get("mode")
    if mode not in _HYBRID_FORECAST_MODES:
        errors["mode"] = "invalid_mode"
    enabled = payload.get("enabled")
    if enabled is not True and enabled is not False:
        errors["enabled"] = "invalid_enabled"
    custom_time = payload.get("custom_time")
    if mode == "custom_time":
        if (
            not isinstance(custom_time, str)
            or _HYBRID_CUSTOM_TIME.fullmatch(custom_time) is None
        ):
            errors["custom_time"] = "invalid_custom_time"
    elif "custom_time" in payload and custom_time is not None:
        errors["custom_time"] = "invalid_custom_time"
    if errors:
        return (None, errors)
    return (
        (
            {
                "mode": mode,
                "enabled": enabled,
                "custom_time": custom_time if mode == "custom_time" else None,
            },
            {},
        )
        if True
        else None
    )


def _wish_matches(stored: dict[str, Any] | None, wish: dict[str, Any]) -> bool:
    if stored is None:
        return False
    return (
        stored["mode"] == wish["mode"]
        and stored["enabled"] is wish["enabled"]
        and (stored["custom_time"] == wish["custom_time"])
    )


def _service_data(wish: dict[str, Any]) -> dict[str, Any]:
    service_data = {"mode": wish["mode"], "enabled": wish["enabled"]}
    if wish["mode"] == "custom_time":
        service_data["custom_time"] = wish["custom_time"]
    return service_data


class HybridForecastStateView(HomeAssistantView):
    """Read the hybrid-forecast setting and today's operational runs. @zara"""

    url = "/api/sfml_stats/hybrid_forecast"
    name = "api:sfml_stats:hybrid_forecast"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        today_iso = ha_today(context.HASS).isoformat()
        try:
            async with _get_db() as connection:
                payload = await _hybrid_forecast_payload(connection, today_iso)
        except Exception as error:
            _LOGGER.error("Hybrid forecast state failed: %s", error)
            return web.json_response(
                {"success": False, "error": "database_unavailable"},
                status=503,
                headers=_NO_STORE,
            )
        return web.json_response(payload, headers=_NO_STORE)


class HybridForecastSettingsPostView(HomeAssistantView):
    """Change the hybrid-forecast setting through the SFML service. @zara"""

    url = "/api/sfml_stats/hybrid_forecast"
    name = "api:sfml_stats:hybrid_forecast:write"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None:
            return web.json_response(
                {"success": False, "error": "unauthorized"},
                status=401,
                headers=_NO_STORE,
            )
        if not bool(getattr(user, "is_admin", False)):
            return web.json_response(
                {"success": False, "error": "admin_required"},
                status=403,
                headers=_NO_STORE,
            )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "errors": {"body": "invalid_json"}},
                status=400,
                headers=_NO_STORE,
            )
        wish, errors = _validate_hybrid_payload(payload)
        if wish is None:
            return web.json_response(
                {"success": False, "errors": errors}, status=400, headers=_NO_STORE
            )
        services = getattr(self.hass, "services", None)
        has_service = getattr(services, "has_service", None)
        if services is None or not has_service(_SFML_DOMAIN, _SFML_SET_REFORECAST_MODE):
            return web.json_response(
                {"success": False, "error": "service_unavailable"},
                status=503,
                headers=_NO_STORE,
            )
        try:
            await services.async_call(
                _SFML_DOMAIN,
                _SFML_SET_REFORECAST_MODE,
                _service_data(wish),
                blocking=True,
            )
        except Exception as error:
            _LOGGER.error("Hybrid forecast service call failed: %s", error)
            return web.json_response(
                {"success": False, "error": "service_call_failed"},
                status=502,
                headers=_NO_STORE,
            )
        try:
            async with _get_db() as connection:
                stored_settings = await _fetch_hybrid_settings(connection)
        except Exception as error:
            _LOGGER.error("Hybrid forecast read-back failed: %s", error)
            stored_settings = None
        if not _wish_matches(stored_settings, wish):
            return web.json_response(
                {
                    "success": False,
                    "error": "not_applied",
                    "message": "Die Änderung wurde nicht übernommen.",
                },
                status=409,
                headers=_NO_STORE,
            )
        return web.json_response(
            {"success": True, "settings": stored_settings}, headers=_NO_STORE
        )
