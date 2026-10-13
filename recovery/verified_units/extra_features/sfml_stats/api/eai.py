# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Thin EAI capability and preview API."""
from __future__ import annotations
import json
from itertools import chain, islice
import logging
from math import isfinite
import re
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.eai_wp_sources import heatpump_setup_status
from ..mock.eai import SCENARIOS, scenario_payload
from .premium_dashboard import _eai_config, _eai_feature_enabled
from .views import local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
_SECTIONS = {
    "energy",
    "status",
    "weather",
    "building",
    "forecast",
    "mobility",
    "overview",
    "operation",
    "efficiency",
    "diagnostics",
}
_PRIVATE_KEYS = {
    "data_dir",
    "entity_id",
    "file_path",
    "entity_ids",
    "coordinator",
    "wallbox_name",
    "config_entry_id",
    "recommendation_explanation",
}
_SAFE_CODE = re.compile("^[a-z][a-z0-9_]{0,63}$")
_MOBILITY_PUBLIC_KEYS = {
    "available",
    "configured",
    "locked",
    "required_entitlement",
    "reason",
    "reason_codes",
    "missing_inputs",
    "telemetry_available",
    "demand_available",
    "plan_available",
    "pv_allocation_available",
    "connected",
    "charging",
    "current_power_kw",
    "energy_today_kwh",
    "current_soc_percent",
    "target_soc_percent",
    "battery_capacity_kwh",
    "demand_source",
    "demand_confidence_percent",
    "battery_energy_demand_kwh",
    "requested_energy_kwh",
    "planned_distance_km",
    "consumption_kwh_per_100km",
    "charging_efficiency_percent",
    "max_charging_power_kw",
    "required_energy_kwh",
    "departure_time",
    "planning_window_start",
    "planning_window_end",
    "planning_horizon_hours",
    "planning_covered_hours",
    "planning_intervals_contiguous",
    "planning_window_complete",
    "recommended_action",
    "recommendation_reason",
    "recommendation_confidence_percent",
    "forecast_uncertainty_percent",
    "recommended_start",
    "recommended_end",
    "recommended_energy_kwh",
    "expected_pv_energy_kwh",
    "expected_pv_energy_lower_kwh",
    "expected_pv_energy_upper_kwh",
    "expected_grid_energy_kwh",
    "household_grid_import_kwh",
    "heat_pump_grid_import_kwh",
    "expected_grid_export_kwh",
    "expected_pv_share_percent",
    "departure_readiness_percent",
    "departure_risk",
    "charging_window_active",
    "pv_charging_recommended",
    "residual_pv_window_active",
    "low_price_charging_recommended",
    "estimated_immediate_cost_eur",
    "estimated_advised_cost_eur",
    "estimated_cost_advantage_eur",
    "electricity_price_ct_per_kwh",
    "feed_in_tariff_ct_per_kwh",
    "data_quality_percent",
    "energy_context_quality_percent",
    "allocation_valid",
    "allocation_status",
    "advisory_only",
    "control_services_called",
    "hours",
}
_MOBILITY_HOUR_PUBLIC_KEYS = {
    "timestamp",
    "interval_start",
    "interval_end",
    "interval_hours",
    "source_interval_start",
    "source_interval_end",
    "source_interval_hours",
    "available_start",
    "available_end",
    "available_hours",
    "pv_forecast_kwh",
    "household_base_load_kwh",
    "household_pv_kwh",
    "household_grid_import_kwh",
    "heat_pump_kwh",
    "heat_pump_pv_kwh",
    "heat_pump_grid_import_kwh",
    "battery_reserve_kwh",
    "battery_pv_reserve_kwh",
    "battery_pv_reserved_kwh",
    "pv_calibration_reserve_kwh",
    "wallbox_pv_available_kwh",
    "unallocated_or_export_kwh",
    "expected_grid_export_kwh",
    "pv_balance_error_kwh",
    "pv_balance_tolerance_kwh",
    "energy_context_quality_percent",
    "allocation_valid",
    "allocation_status",
    "residual_pv_kwh",
    "residual_pv_lower_kwh",
    "residual_pv_upper_kwh",
    "forecast_confidence_percent",
    "forecast_uncertainty_percent",
    "wallbox_kwh",
    "pv_wallbox_kwh",
    "grid_wallbox_kwh",
}
_MOBILITY_CODE_VALUES = {
    "required_entitlement": {"forecast_enabled"},
    "reason": {
        "not_configured",
        "data_unavailable",
        "license_required",
        "planning_window_incomplete",
    },
    "demand_source": {"soc", "energy", "distance"},
    "recommended_action": {
        "defer",
        "charge",
        "connect",
        "complete",
        "data_unavailable",
    },
    "recommendation_reason": {
        "low_price",
        "pv_surplus",
        "deadline_plan",
        "target_reached",
        "data_unavailable",
        "departure_deadline",
        "pv_window_upcoming",
        "planning_window_incomplete",
    },
    "allocation_status": {"mixed", "valid", "invalid", "no_intervals"},
}
_MOBILITY_HOUR_ALLOCATION_STATUS = {
    "valid",
    "closed",
    "invalid",
    "balanced",
    "complete",
}
_WEATHER_PUBLIC_KEYS = frozenset(
    {
        "0_6h",
        "24_72h",
        "6_24h",
        "accuracy",
        "actual_available",
        "actual_history",
        "actual_sources",
        "aggregates",
        "available",
        "avg",
        "avg_mm_per_h",
        "bias",
        "by_horizon",
        "capabilities",
        "category",
        "cloud_cover",
        "cloud_cover_high",
        "cloud_cover_low",
        "cloud_cover_mid",
        "code",
        "cold_start",
        "complete_recorder_history",
        "condition",
        "consistent",
        "contract_version",
        "count",
        "coverage",
        "coverage_by_metric",
        "coverage_percent",
        "current",
        "daily",
        "data_quality",
        "data_since",
        "data_to",
        "date",
        "days",
        "dependencies",
        "dhi_wm2",
        "direction",
        "dni_wm2",
        "dry_spell_days",
        "dry_spell_max",
        "end",
        "events",
        "evidence",
        "evidence_kind",
        "expected_days",
        "expected_periods",
        "fallback_observation",
        "fog_type",
        "forecast_available",
        "forecast_stale",
        "forecast_vs_actual",
        "frost",
        "ghi_wm2",
        "heat",
        "history_truncated",
        "home_assistant_recorder",
        "horizon_hours",
        "hourly",
        "hourly_points",
        "hours",
        "hours_count",
        "humidity",
        "humidity_avg_percent",
        "humidity_max",
        "humidity_max_percent",
        "humidity_min",
        "humidity_min_percent",
        "icon_key",
        "impact_code",
        "issued_at",
        "is_complete",
        "is_partial",
        "kepler_input",
        "live_station_available",
        "lux",
        "lux_avg",
        "lux_max",
        "lux_min",
        "mae",
        "mae_delta",
        "max",
        "max_mm_per_h",
        "max_rate_mm_per_h",
        "merge_strategy",
        "message",
        "method",
        "min",
        "missing",
        "month",
        "monthly_series",
        "next_hours",
        "observation_date",
        "observation_source",
        "observations",
        "observed_at",
        "official_alert",
        "outlook",
        "pair_provenance",
        "paired_now",
        "paired_samples",
        "pairs",
        "partial_day",
        "precipitation",
        "precipitation_consistency",
        "precipitation_forecast_mm",
        "precipitation_quality",
        "precipitation_semantics",
        "precipitation_unit",
        "precipitation_max",
        "precipitation_min",
        "precipitation_probability",
        "precipitation_probability_max",
        "precipitation_total_mm",
        "pressure",
        "pressure_avg_hpa",
        "pressure_max",
        "pressure_max_hpa",
        "pressure_min",
        "pressure_min_hpa",
        "previous",
        "provenance",
        "rain",
        "rain_days",
        "ready",
        "reason",
        "recent_timeline",
        "recommended_action",
        "recorder_history_available",
        "records",
        "resets",
        "rmse",
        "same_day_last_year",
        "score_percent",
        "semantics",
        "severity",
        "sfml_database",
        "sfml_expert_blend",
        "sfml_recent_series",
        "sfml_station",
        "solar_radiation",
        "solar_radiation_avg_wm2",
        "solar_radiation_max",
        "solar_radiation_max_wm2",
        "solar_radiation_min",
        "solar_radiation_min_wm2",
        "solar_radiation_wm2",
        "source",
        "source_comparison",
        "source_status",
        "sources",
        "start",
        "station_independent",
        "station_metrics",
        "station_status",
        "status",
        "summary_code",
        "temperature",
        "temperature_avg_c",
        "temperature_c",
        "temperature_c_max",
        "temperature_c_min",
        "temperature_max_c",
        "temperature_min_c",
        "timeline",
        "title",
        "trend",
        "type",
        "time_context",
        "timezone",
        "uncertainty",
        "unit",
        "valid_at",
        "valid_days",
        "value",
        "values",
        "warnings",
        "weather_code",
        "weather_fusion_tracker",
        "wind_bearing",
        "wind_speed",
        "wind_speed_avg_kmh",
        "wind_speed_max",
        "wind_speed_max_kmh",
        "wind_speed_min",
        "wind_speed_min_kmh",
        "year",
        "year_comparison",
    }
)
_WEATHER_LIST_LIMITS = {
    "daily": 366,
    "events": 32,
    "hourly": 168,
    "kepler_input": 168,
    "monthly_series": 120,
    "next_hours": 72,
    "records": 32,
    "recent_timeline": 168,
    "sfml_expert_blend": 168,
    "timeline": 366,
    "warnings": 32,
}
_WEATHER_TAIL_LIST_FIELDS = frozenset(
    {"daily", "timeline", "monthly_series", "recent_timeline"}
)
_WEATHER_DICT_KEY_PRIORITY = {
    "source_comparison": ("sfml_expert_blend", "kepler_input"),
    "actual_history": (
        "available",
        "status",
        "source",
        "data_since",
        "data_to",
        "history_truncated",
        "source_comparison",
        "year_comparison",
        "current",
        "precipitation",
        "recent_timeline",
        "aggregates",
        "timeline",
    ),
    "precipitation": (
        "available",
        "source",
        "semantics",
        "unit",
        "max_rate_mm_per_h",
        "total_mm",
        "rain_days",
        "dry_spell_days",
        "resets",
        "coverage",
        "data_since",
        "data_to",
        "daily",
    ),
}
_WEATHER_MAX_DEPTH = 8
_WEATHER_MAX_RAW_DICT_ITEMS = 1024
_WEATHER_MAX_NODES = 20000
_WEATHER_MAX_BYTES = 524288
_WEATHER_CODE_FIELDS = {
    "code",
    "type",
    "method",
    "reason",
    "source",
    "status",
    "category",
    "fog_type",
    "icon_key",
    "severity",
    "condition",
    "direction",
    "semantics",
    "impact_code",
    "summary_code",
    "evidence_kind",
    "merge_strategy",
    "station_status",
    "observation_source",
}
_WEATHER_CONDITIONS = {
    "fog",
    "hail",
    "rainy",
    "snowy",
    "sunny",
    "windy",
    "cloudy",
    "pouring",
    "lightning",
    "exceptional",
    "partlycloudy",
    "clear-night",
    "snowy-rainy",
    "windy-variant",
    "lightning-rainy",
}


def _json_response(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    """Return volatile EAI data without browser or proxy caching."""
    return web.json_response(
        payload,
        status=status,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


def _public_data(value: Any) -> Any:
    """Remove integration internals from the browser-facing capability DTO."""
    if isinstance(value, dict):
        return {
            key: _public_data(item_value)
            for key, item_value in value.items()
            if key not in _PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_public_data(item_value) for item_value in value]
    return value


def _public_mobility_data(data: dict[str, Any]) -> dict[str, Any]:
    """Expose only the current, enumerated Mobility DTO."""
    public_mobility = {
        key: field_value
        for key, field_value in data.items()
        if key in _MOBILITY_PUBLIC_KEYS
    }
    for key in ("reason_codes", "missing_inputs"):
        raw_codes = public_mobility.get(key)
        public_mobility[key] = (
            [
                field_value
                for field_value in raw_codes
                if isinstance(field_value, str)
                if _SAFE_CODE.fullmatch(field_value)
            ]
            if isinstance(raw_codes, list)
            else []
        )
    for key, allowed_codes in _MOBILITY_CODE_VALUES.items():
        if key in public_mobility:
            if public_mobility[key] is not None and (
                not isinstance(public_mobility[key], str)
                or public_mobility[key] not in allowed_codes
            ):
                public_mobility.pop(key)
    raw_hours = public_mobility.get("hours")
    public_mobility["hours"] = (
        [
            public_hour
            for hour in raw_hours
            if isinstance(hour, dict)
            if (
                public_hour := {
                    key: field_value
                    for key, field_value in hour.items()
                    if key in _MOBILITY_HOUR_PUBLIC_KEYS
                    if key != "allocation_status"
                    or (
                        isinstance(field_value, str)
                        and field_value in _MOBILITY_HOUR_ALLOCATION_STATUS
                    )
                }
            )
        ]
        if isinstance(raw_hours, list)
        else []
    )
    return _public_data(public_mobility)


def _public_weather_data(data: dict[str, Any]) -> dict[str, Any]:
    """Expose a bounded, explicit weather DTO at the browser boundary."""
    budget = {"nodes": _WEATHER_MAX_NODES, "bytes": _WEATHER_MAX_BYTES}

    def reserve(value: Any, overhead: int = 8) -> bool:
        encoded_bytes = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        required_bytes = encoded_bytes + overhead
        if required_bytes > budget["bytes"]:
            return False
        budget["bytes"] -= required_bytes
        return True

    def number_in_range(field: str | None, value: int | float) -> bool:
        try:
            numeric_value = float(value)
        except (OverflowError, TypeError, ValueError):
            return False
        if not isfinite(numeric_value):
            return False
        field_name = field or ""
        if "temperature" in field_name:
            allowed_range = (-100.0, 80.0)
        elif any(
            (
                field_fragment in field_name
                for field_fragment in (
                    "humidity",
                    "percent",
                    "probability",
                    "coverage",
                    "score",
                )
            )
        ):
            allowed_range = (0.0, 100.0)
        elif "pressure" in field_name:
            allowed_range = (800.0, 1200.0)
        elif "wind_bearing" in field_name:
            allowed_range = (0.0, 360.0)
        elif "wind_speed" in field_name:
            allowed_range = (0.0, 500.0)
        elif any(
            (
                field_fragment in field_name
                for field_fragment in ("radiation", "ghi", "dni", "dhi", "lux")
            )
        ):
            allowed_range = (0.0, 2000000.0)
        elif any(
            (
                field_fragment in field_name
                for field_fragment in ("precipitation", "rain", "_mm")
            )
        ):
            allowed_range = (0.0, 1000000.0)
        elif any(
            (
                field_fragment in field_name
                for field_fragment in (
                    "count",
                    "days",
                    "hours",
                    "observations",
                    "pairs",
                    "samples",
                    "resets",
                    "year",
                )
            )
        ):
            allowed_range = (0.0, 1000000000.0)
        elif field_name in {
            "mae",
            "bias",
            "rmse",
            "mae_delta",
            "recent_mae",
            "previous_mae",
        }:
            allowed_range = (-1000.0, 1000.0)
        else:
            allowed_range = (-1000000000.0, 1000000000.0)
        return allowed_range[0] <= numeric_value <= allowed_range[1]

    def project(value: Any, field: str | None = None, depth: int = 0) -> Any:
        if depth > _WEATHER_MAX_DEPTH or budget["nodes"] <= 0:
            return None if True else None
        budget["nodes"] -= 1
        if isinstance(value, dict):
            projected = {}
            priority_fields = _WEATHER_DICT_KEY_PRIORITY.get(field)
            priority_keys = set(priority_fields or ())
            priority_items = (
                ((key, value[key]) for key in priority_fields if key in value)
                if priority_fields
                else ()
            )
            remaining_items = (
                (entry for entry in value.items() if entry[0] not in priority_keys)
                if priority_fields
                else value.items()
            )
            accepted_fields = 0
            for key, item_value in chain(
                priority_items, islice(remaining_items, _WEATHER_MAX_RAW_DICT_ITEMS)
            ):
                if not isinstance(key, str) or key not in _WEATHER_PUBLIC_KEYS:
                    continue
                if accepted_fields >= 256:
                    break
                accepted_fields += 1
                if not reserve(key):
                    break
                projected_value = project(item_value, key, depth + 1)
                if projected_value is not None:
                    projected[key] = projected_value
            return projected if True else None
        if isinstance(value, list):
            list_limit = _WEATHER_LIST_LIMITS.get(field, 720)
            projected_items = []
            bounded_items = (
                value[-list_limit:]
                if field in _WEATHER_TAIL_LIST_FIELDS
                else value[:list_limit]
            )
            for item_value in bounded_items:
                if not reserve("[]", overhead=2):
                    break
                projected_value = project(item_value, None, depth + 1)
                if projected_value is not None:
                    projected_items.append(projected_value)
            return projected_items if True else None
        if value is None or isinstance(value, bool):
            return (value if reserve(value) else None) if True else None
        if isinstance(value, (int, float)) and (not isinstance(value, bool)):
            return (
                (value if number_in_range(field, value) and reserve(value) else None)
                if True
                else None
            )
        if isinstance(value, str):
            if len(value) > 256 or "<" in value or ">" in value:
                return None if True else None
            if field == "condition" and value not in _WEATHER_CONDITIONS:
                return None if True else None
            if (
                field in _WEATHER_CODE_FIELDS
                and field != "condition"
                and (not _SAFE_CODE.fullmatch(value))
            ):
                return None if True else None
            return (value if reserve(value) else None) if True else None

    projected = project(data)
    return projected if isinstance(projected, dict) else {}


def _weather_time_context(hass: HomeAssistant) -> dict[str, str]:
    """Expose the authoritative Home Assistant timezone to weather clients."""
    time_zone = getattr(getattr(hass, "config", None), "time_zone", None) or "UTC"
    try:
        ZoneInfo(time_zone)
    except (TypeError, ZoneInfoNotFoundError):
        time_zone = "UTC"
    return {"timezone": time_zone} if True else None


def _provider_snapshot(hass: HomeAssistant) -> dict[str, Any] | None:
    if "solar_forecast_eai" not in hass.data:
        return
    provider_data = hass.data["solar_forecast_eai"]
    if not isinstance(provider_data, dict):
        raise ValueError("incompatible_provider")
    provider = provider_data.get("capability_provider")
    if provider is None or not callable(getattr(provider, "snapshot", None)):
        raise ValueError("incompatible_provider")
    snapshot = provider.snapshot()
    if not isinstance(snapshot, dict) or snapshot.get("provider_version") != 8:
        raise ValueError("incompatible_provider")
    return snapshot


def _scenario_for(
    snapshot: dict[str, Any] | None, *, feature_enabled: bool | None = None
) -> str:
    if snapshot is None:
        return "demo_full_capability"
    if snapshot.get("license_status") != "valid":
        return "demo_license_required"
    if feature_enabled is False:
        return "demo_feature_unconfigured"
    if not snapshot.get("configured"):
        return "demo_learning_phase"
    return "demo_configured_no_history"


def _section_allows_live(
    hass: HomeAssistant, snapshot: dict[str, Any] | None, section: str
) -> bool:
    if snapshot is None or section == "status":
        return False
    config = _eai_config(hass)
    if section == "mobility":
        return _eai_feature_enabled(config, "wallbox_enabled")
    if section == "weather":
        return _eai_feature_enabled(config, "weather_intelligence_enabled")
    return _eai_feature_enabled(config, "heat_pump_enabled")


def _live_payload(
    hass: HomeAssistant, snapshot: dict[str, Any], section: str
) -> dict[str, Any] | None:
    if (
        snapshot.get("provider_version") != 8
        or snapshot.get("license_status") != "valid"
        or (not snapshot.get("configured"))
        or (snapshot.get("data_mode") not in {"live", "degraded"})
    ):
        return None
    sections = snapshot.get("sections")
    if not isinstance(sections, dict):
        return
    section_data = sections.get(section)
    if not isinstance(section_data, dict):
        section_data = {}
    metadata = {
        "data_mode": snapshot.get("data_mode"),
        "eai_installed": snapshot.get("installed", True),
        "license_status": snapshot.get("license_status"),
        "configuration_status": snapshot.get("configuration_status"),
        "capability_level": snapshot.get("capability_level"),
        "data_freshness": snapshot.get("data_freshness"),
        "provider_version": snapshot.get("provider_version"),
        "is_demo": False,
        "sample_reason": "live",
    }
    public_section = (
        _public_mobility_data(section_data)
        if section == "mobility"
        else (
            _public_weather_data(section_data)
            if section == "weather"
            else _public_data(section_data)
        )
    )
    if section == "weather":
        public_section["time_context"] = _weather_time_context(hass)
    return {**metadata, "section": section, "data": public_section}


class ModernEAIView(HomeAssistantView):
    url = "/api/sfml_stats/modern/eai/{section}"
    name = "api:sfml_stats:modern:eai"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request, section: str) -> web.Response:
        from ..core.dev_mode import (
            is_dev_active as is_dev_active,
            json_dev_required as json_dev_required,
        )

        if section in {"building", "diagnostics"} and (not is_dev_active(self.hass)):
            return json_dev_required()
        if section == "setup":
            return _json_response(
                {
                    "success": True,
                    "data": {
                        "data": heatpump_setup_status(self.hass),
                        "is_demo": False,
                    },
                }
            )
        if section not in _SECTIONS:
            return _json_response(
                {"success": False, "error": {"code": "not_found"}}, status=404
            )
        requested_scenario = request.query.get("scenario")
        if requested_scenario is not None and requested_scenario not in SCENARIOS:
            return _json_response(
                {"success": False, "error": {"code": "unknown_scenario"}}, status=404
            )
        try:
            snapshot = _provider_snapshot(self.hass)
            feature_enabled = (
                _section_allows_live(self.hass, snapshot, section)
                if section != "status" and snapshot is not None
                else None
            )
            live_payload = (
                _live_payload(self.hass, snapshot, section)
                if requested_scenario is None
                and snapshot is not None
                and feature_enabled
                else None
            )
            scenario = requested_scenario or _scenario_for(
                snapshot, feature_enabled=feature_enabled
            )
            if section == "status":
                status_payload = dict(SCENARIOS[scenario]["status"])
                if requested_scenario is None and snapshot is not None:
                    status_payload.update(
                        {
                            "eai_installed": snapshot["installed"],
                            "license_status": snapshot["license_status"],
                            "configuration_status": snapshot["configuration_status"],
                            "capability_level": snapshot["capability_level"],
                            "data_mode": snapshot["data_mode"],
                            "data_freshness": snapshot["data_freshness"],
                            "provider_version": snapshot["provider_version"],
                        }
                    )
                payload = status_payload
            else:
                payload = live_payload or scenario_payload(scenario, section)
            return _json_response({"success": True, "data": payload})
        except ValueError:
            return _json_response(
                {
                    "success": False,
                    "error": {
                        "code": "provider_incompatible",
                        "message": "EAI provider version is not supported.",
                    },
                },
                status=503,
            )
        except Exception as error:
            _LOGGER.error("EAI provider failed (%s)", type(error).__name__)
            return _json_response(
                {
                    "success": False,
                    "error": {
                        "code": "provider_unavailable",
                        "message": "EAI provider is unavailable.",
                    },
                },
                status=503,
            )


async def async_setup_eai_views(hass: HomeAssistant) -> None:
    hass.http.register_view(ModernEAIView(hass))
