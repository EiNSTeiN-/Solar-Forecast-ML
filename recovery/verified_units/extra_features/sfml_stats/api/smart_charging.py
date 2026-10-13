# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Smart charging settings API for SFML Stats."""
from __future__ import annotations
from typing import Any, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from .. import async_update_smart_charging_settings
from ..const import (
    CONF_BATTERY_CAPACITY,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_TARGET_SOC,
    CONF_SMART_CHARGING_ENABLED,
    CONF_SMART_CHARGING_MODE,
    CONF_SMART_CHARGING_SWITCH,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_SMART_CHARGING_MODE,
    DOMAIN,
    SMC_LIMITS,
    SMC_MODES,
    configured_battery_capacity_kwh,
)
from .gpm import _gpm_coordinator
from .views import local_only, local_or_trusted_cloud_only
from ..core.smart_charging import sfml_inverter_nominal_power_kw

GPM_KEYS = (
    "max_price",
    "force_charge_price",
    "threshold_mode",
    "mode",
    "below_average_pct",
    "cheapest_hours",
)
SMC_KEYS = (
    CONF_SMART_CHARGING_ENABLED,
    CONF_SMART_CHARGING_MODE,
    CONF_MIN_SOC,
    CONF_MAX_SOC,
    CONF_TARGET_SOC,
)
_SETTINGS_KEYS = (
    "enabled",
    "mode",
    "target_soc",
    "min_soc",
    "max_soc",
    "max_charge_power_kw",
    "inverter_nominal_power_kw",
)
_THRESHOLD_KEYS = (
    "mode",
    "max_price",
    "force_charge_price",
    "below_average_pct",
    "cheapest_hours",
)
_TOP_KEYS = ("success", "settings", "thresholds", "limits", "errors", "plant", "gpm")
_GPM_META_KEYS = ("available", "is_demo", "tariff_mode")
_PLANT_KEYS = (
    "battery_capacity_kwh",
    "max_charge_power_kw",
    "inverter_nominal_power_kw",
    "charge_switch_configured",
)


def live_is_cheap(data: Mapping[str, Any] | None) -> bool | None:
    """Map coordinator cheap flag; never None when a threshold is published."""
    if not isinstance(data, Mapping):
        return
    effective_threshold_ct = data.get("effective_threshold_ct")
    is_cheap = data.get("is_cheap")
    if effective_threshold_ct is not None:
        return bool(is_cheap)
    if is_cheap is None:
        return
    return bool(is_cheap)


def _iso_stamp(value: Any) -> str | None:
    if value is None:
        return
    isoformat_method = getattr(value, "isoformat", None)
    if callable(isoformat_method):
        timestamp_text = isoformat_method()
        return str(timestamp_text) if timestamp_text else None
    timestamp_text = str(value).strip()
    return timestamp_text or None


def live_switch_timestamps(
    switch_state: str | None, last_changed: Any, enabled: bool
) -> tuple[str | None, str | None]:
    """Return (charging_since, waiting_since) from the configured charge switch."""
    if switch_state is None:
        return (None, None)
    normalized_switch_state = str(switch_state).strip().lower()
    last_changed_stamp = _iso_stamp(last_changed)
    if normalized_switch_state == "on":
        return (last_changed_stamp, None)
    if normalized_switch_state == "off" and enabled:
        return (None, last_changed_stamp)
    return (None, None)


def _date_prefix(value: Any) -> str | None:
    if value is None:
        return
    date_text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    date_text = str(date_text).strip()
    if len(date_text) >= 10 and date_text[4] == "-" and (date_text[7] == "-"):
        return date_text[:10]
    return None if True else None


def _forecast_price_for_hour(rows: Any, hour: int) -> float | None:
    if not isinstance(rows, list):
        return
    for forecast_row in rows:
        if not isinstance(forecast_row, Mapping):
            continue
        try:
            row_hour = int(forecast_row.get("hour"))
        except (TypeError, ValueError):
            continue
        if row_hour != hour:
            continue
        price_ct = forecast_row.get("total_price")
        try:
            return (
                # Keep this line boundary for the verified shared return block.
                float(price_ct)
                if price_ct is not None
                else None
            )
        except (TypeError, ValueError):
            return


def live_next_cheap_fields(
    data: Mapping[str, Any] | None,
    *,
    gpm_available: bool,
    is_demo: bool,
    today: str | None = None,
) -> tuple[int | None, float | None]:
    """Return (next_cheap_hour, next_cheap_price_ct) from GPM coordinator data."""
    if not gpm_available or is_demo or (not isinstance(data, Mapping)):
        return (None, None)
    next_cheap_hour_value = data.get("next_cheap_hour")
    next_cheap_timestamp = data.get("next_cheap_timestamp")
    next_cheap = data.get("next_cheap")
    if isinstance(next_cheap, Mapping):
        if next_cheap_hour_value is None:
            next_cheap_hour_value = next_cheap.get("hour")
        if next_cheap_timestamp is None:
            next_cheap_timestamp = next_cheap.get("timestamp")
    try:
        next_cheap_hour = (
            int(next_cheap_hour_value) if next_cheap_hour_value is not None else None
        )
    except (TypeError, ValueError):
        next_cheap_hour = None
    if next_cheap_hour is None:
        return (None, None)
    next_cheap_date = _date_prefix(next_cheap_timestamp)
    price_ct = None
    if next_cheap_date and today:
        if next_cheap_date == today:
            price_ct = _forecast_price_for_hour(
                data.get("forecast_today"), next_cheap_hour
            )
        else:
            price_ct = _forecast_price_for_hour(
                data.get("forecast_tomorrow"), next_cheap_hour
            )
    if price_ct is None:
        price_ct = _forecast_price_for_hour(data.get("forecast_today"), next_cheap_hour)
    if price_ct is None:
        price_ct = _forecast_price_for_hour(
            data.get("forecast_tomorrow"), next_cheap_hour
        )
    return (next_cheap_hour, price_ct)


def _gpm_int_hours(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    hours = []
    for hour_value in value:
        try:
            hours.append(int(hour_value))
        except (TypeError, ValueError):
            continue
    return hours


def dashboard_gpm_meta(coordinator: Any) -> dict[str, Any]:
    empty_cheap_hours = {"today": [], "tomorrow": []}
    if coordinator is None:
        return {
            "available": False,
            "is_demo": False,
            "cheap_hours": empty_cheap_hours,
            "tomorrow_available": False,
        }
    is_demo = bool(getattr(coordinator, "is_demo", False))
    coordinator_data = getattr(coordinator, "data", None) or {}
    gpm_data = coordinator_data if isinstance(coordinator_data, dict) else {}
    threshold_catalog = _gpm_threshold_catalog(gpm_data)
    tomorrow_forecast = gpm_data.get("forecast_tomorrow")
    return {
        "available": threshold_catalog is not None and (not is_demo),
        "is_demo": is_demo,
        "cheap_hours": {
            "today": _gpm_int_hours(gpm_data.get("cheap_hours_today")),
            "tomorrow": _gpm_int_hours(gpm_data.get("cheap_hours_tomorrow")),
        },
        "tomorrow_available": isinstance(tomorrow_forecast, list)
        and bool(tomorrow_forecast),
    }


def gpm_chart_source(gpm_meta: Mapping[str, Any] | None) -> str:
    if not isinstance(gpm_meta, Mapping):
        return "unavailable"
    if gpm_meta.get("available") and (not gpm_meta.get("is_demo")):
        return "gpm_threshold"
    return "unavailable"


def gpm_forecast_cheap_by_day(
    data: Mapping[str, Any] | None,
) -> dict[str, dict[int, bool]]:
    """Hour cheap flags from GPM forecast_today / forecast_tomorrow."""
    cheap_by_day = {"today": {}, "tomorrow": {}}
    if not isinstance(data, Mapping):
        return cheap_by_day
    for day_key, forecast_key in (
        ("today", "forecast_today"),
        ("tomorrow", "forecast_tomorrow"),
    ):
        forecast_rows = data.get(forecast_key)
        if isinstance(forecast_rows, list):
            for forecast_row in forecast_rows:
                if not isinstance(forecast_row, dict):
                    continue
                try:
                    next_cheap_hour = int(forecast_row.get("hour"))
                except (TypeError, ValueError):
                    continue
                cheap_by_day[day_key][next_cheap_hour] = bool(
                    forecast_row.get("is_cheap")
                ) or bool(forecast_row.get("is_force_price"))
    return cheap_by_day


def future_hour_is_gpm_cheap(
    cheap_by_day: Mapping[str, Mapping[int, bool]] | None,
    *,
    day_key: str | None,
    hour: int,
) -> bool:
    if not day_key or not isinstance(cheap_by_day, Mapping):
        return False
    day_cheap_flags = cheap_by_day.get(day_key) or {}
    return bool(day_cheap_flags.get(hour))


def plant_charge_switch_configured(config: Mapping[str, Any] | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    switch_entity_id = config.get(CONF_SMART_CHARGING_SWITCH)
    if switch_entity_id is None:
        return False
    return bool(str(switch_entity_id).strip())


def setup_check_items(
    plant: Mapping[str, Any] | None, gpm: Mapping[str, Any] | None, enabled: bool
) -> list[dict[str, str]]:
    """Ampel items for the Smart Charge start check. Levels: red, yellow, gray."""
    plant = plant or {}
    gpm = gpm or {}
    check_items = []
    capacity_value = plant.get("battery_capacity_kwh")
    try:
        capacity_kwh = float(capacity_value) if capacity_value is not None else 0.0
    except (TypeError, ValueError):
        capacity_kwh = 0.0
    if capacity_value is None or capacity_kwh <= 0.0:
        check_items.append({"id": "capacity", "level": "red"})
    if not plant.get("charge_switch_configured"):
        check_items.append({"id": "switch", "level": "yellow"})
    if gpm.get("is_demo"):
        check_items.append({"id": "gpm_demo", "level": "yellow"})
    elif not gpm.get("available"):
        check_items.append({"id": "gpm", "level": "yellow"})
    if not enabled:
        check_items.append({"id": "enabled", "level": "gray"})
    return check_items


def _published_threshold_limits(
    data: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if isinstance(data, Mapping):
        threshold_limits = data.get("threshold_limits")
        if isinstance(threshold_limits, dict) and threshold_limits:
            return threshold_limits
    return None if True else None


def _published_threshold_modes(
    data: Mapping[str, Any] | None,
) -> tuple[str, ...] | None:
    if isinstance(data, Mapping):
        threshold_modes = data.get("threshold_modes")
        if isinstance(threshold_modes, (list, tuple)) and threshold_modes:
            return tuple((str(mode_value) for mode_value in threshold_modes))
    return None if True else None


def _gpm_threshold_catalog(
    data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    threshold_limits = _published_threshold_limits(data)
    threshold_modes = _published_threshold_modes(data)
    if threshold_limits is None or threshold_modes is None:
        return None
    return (threshold_limits, threshold_modes)


def _gpm_unavailable_response() -> web.Response:
    return web.json_response({"success": False, "error": "gpm_unavailable"}, status=409)


def _in_limit(limits: Mapping[str, float], value: float) -> bool:
    return limits["min"] <= value <= limits["max"]


def _split_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    gpm_changes = {}
    charging_changes = {}
    errors = {}
    for key, value in payload.items():
        if key in GPM_KEYS:
            gpm_changes[key] = value
        elif key in SMC_KEYS:
            charging_changes[key] = value
        else:
            errors[key] = "unexpected_key"
    return (gpm_changes, charging_changes, errors)


def _validate_gpm(
    current: Mapping[str, Any],
    changes: Mapping[str, Any],
    published: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    errors = {}
    threshold_catalog = _gpm_threshold_catalog(published)
    if threshold_catalog is None:
        return {"thresholds": "gpm_unavailable"}
    threshold_limits, threshold_modes = threshold_catalog
    threshold_mode = current.get("mode") or current.get("threshold_mode") or "absolute"
    if "threshold_mode" in changes or "mode" in changes:
        requested_mode = changes.get("threshold_mode", changes.get("mode"))
        if requested_mode not in threshold_modes:
            errors["threshold_mode"] = "invalid_mode"
        else:
            threshold_mode = str(requested_mode)
    max_price_ct = current.get("max_price", 0)
    force_charge_price_ct = current.get("force_charge_price", 0)
    if "max_price" in changes:
        try:
            max_price_ct = float(changes["max_price"])
        except (TypeError, ValueError):
            errors["max_price"] = "range"
        else:
            if not _in_limit(threshold_limits["max_price"], max_price_ct):
                errors["max_price"] = "range"
    if "force_charge_price" in changes:
        try:
            force_charge_price_ct = float(changes["force_charge_price"])
        except (TypeError, ValueError):
            errors["force_charge_price"] = "range"
        else:
            if not _in_limit(
                threshold_limits["force_charge_price"], force_charge_price_ct
            ):
                errors["force_charge_price"] = "range"
    if "below_average_pct" in changes:
        try:
            value = float(changes["below_average_pct"])
        except (TypeError, ValueError):
            errors["below_average_pct"] = "range"
        else:
            if not _in_limit(threshold_limits["below_average_pct"], value):
                errors["below_average_pct"] = "range"
    if "cheapest_hours" in changes:
        try:
            value = float(changes["cheapest_hours"])
        except (TypeError, ValueError):
            errors["cheapest_hours"] = "range"
        else:
            if value != int(value) or not _in_limit(
                threshold_limits["cheapest_hours"], value
            ):
                errors["cheapest_hours"] = "range"
    if errors:
        return errors
    if threshold_mode == "absolute" and float(force_charge_price_ct) > float(
        max_price_ct
    ):
        return {"force_charge_price": "force_above_cheap"}
    return {}


def _validate_smc(changes: Mapping[str, Any]) -> dict[str, str]:
    errors = {}
    if (
        CONF_SMART_CHARGING_MODE in changes
        and changes[CONF_SMART_CHARGING_MODE] not in SMC_MODES
    ):
        errors[CONF_SMART_CHARGING_MODE] = "invalid_mode"
    for config_key, limit_key in (
        (CONF_MIN_SOC, "min_soc"),
        (CONF_MAX_SOC, "max_soc"),
        ("target_soc", "target_soc"),
    ):
        if config_key not in changes:
            continue
        try:
            value = float(changes[config_key])
        except (TypeError, ValueError):
            errors[config_key] = "range"
            continue
        if _in_limit(SMC_LIMITS[limit_key], value):
            continue
        errors[config_key] = "range"
    return errors


def public_smart_charging_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    thresholds = (
        raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
    )
    limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    plant = raw.get("plant") if isinstance(raw.get("plant"), dict) else {}
    gpm_metadata = raw.get("gpm") if isinstance(raw.get("gpm"), dict) else {}
    public_settings = {
        "success": True,
        "settings": {key: settings.get(key) for key in _SETTINGS_KEYS},
        "thresholds": {key: thresholds.get(key) for key in _THRESHOLD_KEYS},
        "limits": limits,
        "errors": raw.get("errors") if isinstance(raw.get("errors"), dict) else {},
        "plant": {key: plant.get(key) for key in _PLANT_KEYS},
        "gpm": {key: gpm_metadata.get(key) for key in _GPM_META_KEYS},
    }
    assert set(public_settings) == set(_TOP_KEYS)
    assert set(public_settings["settings"]) == set(_SETTINGS_KEYS)
    assert set(public_settings["thresholds"]) == set(_THRESHOLD_KEYS)
    assert set(public_settings["plant"]) == set(_PLANT_KEYS)
    assert set(public_settings["gpm"]) == set(_GPM_META_KEYS)
    return public_settings


def _current_settings(hass: Any) -> dict[str, Any]:
    config = {}
    for runtime_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(runtime_data, dict):
            continue
        if not "config" in runtime_data:
            continue
        config = runtime_data["config"]
        break
    gpm_coordinator = _gpm_coordinator(hass)
    gpm_data = getattr(gpm_coordinator, "data", None) or {}
    if not isinstance(gpm_data, dict):
        gpm_data = {}
    thresholds = (
        gpm_data.get("thresholds")
        if isinstance(gpm_data.get("thresholds"), dict)
        else {}
    )
    is_demo = (
        bool(getattr(gpm_coordinator, "is_demo", False))
        if gpm_coordinator is not None
        else False
    )
    threshold_catalog = _gpm_threshold_catalog(gpm_data)
    if threshold_catalog is None:
        gpm_limits = {}
        public_thresholds = {key: None for key in _THRESHOLD_KEYS}
        gpm_available = False
    else:
        gpm_limits = threshold_catalog[0]
        public_thresholds = {
            "mode": thresholds.get("mode") or thresholds.get("threshold_mode"),
            "max_price": thresholds.get("max_price"),
            "force_charge_price": thresholds.get("force_charge_price"),
            "below_average_pct": thresholds.get("below_average_pct"),
            "cheapest_hours": thresholds.get("cheapest_hours"),
        }
        gpm_available = gpm_coordinator is not None and (not is_demo)
    return public_smart_charging_settings(
        {
            "settings": {
                "enabled": bool(config.get(CONF_SMART_CHARGING_ENABLED, False)),
                "mode": config.get(
                    CONF_SMART_CHARGING_MODE, DEFAULT_SMART_CHARGING_MODE
                ),
                "target_soc": config.get(CONF_TARGET_SOC, DEFAULT_MAX_SOC),
                "min_soc": config.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                "max_soc": config.get(CONF_MAX_SOC, DEFAULT_MAX_SOC),
                "max_charge_power_kw": config.get(CONF_MAX_CHARGE_POWER_KW, 0.0),
                "inverter_nominal_power_kw": sfml_inverter_nominal_power_kw(hass),
            },
            "thresholds": public_thresholds,
            "limits": {**SMC_LIMITS, **gpm_limits},
            "plant": {
                "battery_capacity_kwh": configured_battery_capacity_kwh(
                    config.get(CONF_BATTERY_CAPACITY)
                ),
                "max_charge_power_kw": config.get(CONF_MAX_CHARGE_POWER_KW, 0.0),
                "inverter_nominal_power_kw": sfml_inverter_nominal_power_kw(hass),
                "charge_switch_configured": plant_charge_switch_configured(config),
            },
            "gpm": {
                "available": gpm_available,
                "is_demo": is_demo,
                "tariff_mode": gpm_data.get("tariff_mode"),
            },
        }
    )


class SmartChargingSettingsGetView(HomeAssistantView):
    url = "/api/sfml_stats/smart_charging/settings"
    name = "api:sfml_stats:smart_charging:settings"
    requires_auth = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(
            _current_settings(self.hass), headers={"Cache-Control": "no-store"}
        )


class SmartChargingSettingsPostView(HomeAssistantView):
    url = "/api/sfml_stats/smart_charging/settings"
    name = "api:sfml_stats:smart_charging:settings_write"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None:
            return web.json_response(
                {"success": False, "error": "unauthorized"}, status=401
            )
        if not bool(getattr(user, "is_admin", False)):
            return web.json_response(
                {"success": False, "error": "admin_required"}, status=403
            )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "errors": {"body": "invalid_json"}}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"success": False, "errors": {"body": "invalid_json"}}, status=400
            )
        gpm_changes, charging_changes, payload_errors = _split_payload(payload)
        if payload_errors:
            return web.json_response(
                {"success": False, "errors": payload_errors}, status=400
            )
        gpm_coordinator = _gpm_coordinator(self.hass)
        current_thresholds = {}
        gpm_data = {}
        if gpm_coordinator is not None:
            gpm_data = getattr(gpm_coordinator, "data", None) or {}
            current_thresholds = (
                gpm_data.get("thresholds") if isinstance(gpm_data, dict) else {}
            )
            if not isinstance(current_thresholds, dict):
                current_thresholds = {}
            if not isinstance(gpm_data, dict):
                gpm_data = {}
        errors = {}
        if gpm_changes:
            if (
                gpm_coordinator is None
                or not hasattr(gpm_coordinator, "async_update_thresholds")
                or _gpm_threshold_catalog(gpm_data) is None
            ):
                return _gpm_unavailable_response()
            if getattr(gpm_coordinator, "is_demo", False):
                return web.json_response(
                    {"success": False, "error": "demo_mode"}, status=409
                )
            errors.update(_validate_gpm(current_thresholds, gpm_changes, gpm_data))
        if charging_changes:
            errors.update(_validate_smc(charging_changes))
        if errors:
            return web.json_response({"success": False, "errors": errors}, status=400)
        if gpm_changes:
            try:
                update_result = await gpm_coordinator.async_update_thresholds(
                    **gpm_changes
                )
            except ValueError as error:
                if str(error) == "demo_mode":
                    return web.json_response(
                        {"success": False, "error": "demo_mode"}, status=409
                    )
                return web.json_response(
                    {"success": False, "error": "gpm_unavailable"}, status=409
                )
            gpm_errors = (
                update_result.get("errors") if isinstance(update_result, dict) else None
            )
            if gpm_errors:
                return web.json_response(
                    {"success": False, "errors": gpm_errors}, status=400
                )
        if charging_changes:
            update_result = await async_update_smart_charging_settings(
                self.hass, **charging_changes
            )
            charging_errors = (
                update_result.get("errors") if isinstance(update_result, dict) else None
            )
            if charging_errors:
                return web.json_response(
                    {
                        "success": False,
                        "errors": charging_errors,
                        "error": next(iter(charging_errors.values())),
                    },
                    status=400,
                )
        return web.json_response(
            {
                **_current_settings(self.hass),
                "applied": {
                    "thresholds": bool(gpm_changes),
                    "charging": bool(charging_changes),
                },
            },
            headers={"Cache-Control": "no-store"},
        )


async def async_setup_smart_charging_views(hass: Any) -> None:
    hass.http.register_view(SmartChargingSettingsGetView(hass))
    hass.http.register_view(SmartChargingSettingsPostView(hass))
