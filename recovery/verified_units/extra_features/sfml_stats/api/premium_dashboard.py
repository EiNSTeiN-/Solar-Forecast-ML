# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Premium solar cockpit API."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..utils.time_utils import ha_now, ha_today
from .views import _build_live_today_clean_eval, _get_db, _table_columns, local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
_PROVIDER_VERSION = 8
_UNLICENSED_STATUSES = {
    "expired",
    "not_provided",
    "not_yet_valid",
    "wrong_product",
    "invalid_format",
    "unknown_key_id",
    "validation_error",
    "invalid_signature",
    "missing_entitlement",
    "unsupported_version",
}


class PremiumDashboardProviderError(RuntimeError):
    """Raised when an installed premium provider cannot satisfy its contract."""


def _number(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    return round(number, digits)


def _sum_power(*values: Any) -> float | None:
    total_power_w = 0.0
    has_numeric_value = False
    for value in values:
        number = _number(value)
        if number is not None:
            total_power_w += number
            has_numeric_value = True
    if not has_numeric_value:
        return
    return round(total_power_w, 2)


def _time_context(hass: HomeAssistant | None, fallback: str = "UTC") -> dict[str, str]:
    """Expose the authoritative Home Assistant timezone to the dashboard."""
    time_zone = (
        getattr(getattr(hass, "config", None), "time_zone", None)
        if hass is not None
        else fallback
    ) or fallback
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        time_zone = "UTC"
    return {"timezone": time_zone} if True else None


def _wallbox_state(mobility: dict[str, Any]) -> str | None:
    if mobility.get("charging") is True:
        return "charging"
    if mobility.get("connected") is True:
        return "connected"
    if mobility.get("connected") is False:
        return "idle"


def _provider_snapshot(hass: HomeAssistant) -> dict[str, Any] | None:
    if "solar_forecast_eai" not in hass.data:
        return
    provider_data = hass.data["solar_forecast_eai"]
    if not isinstance(provider_data, dict):
        raise PremiumDashboardProviderError("incompatible_provider")
    provider = provider_data.get("capability_provider")
    if provider is None:
        raise PremiumDashboardProviderError("provider_unavailable")
    if not callable(getattr(provider, "snapshot", None)):
        raise PremiumDashboardProviderError("incompatible_provider")
    try:
        snapshot = provider.snapshot()
    except Exception as error:
        _LOGGER.warning(
            "Premium dashboard provider snapshot unavailable (%s)", type(error).__name__
        )
        raise PremiumDashboardProviderError("provider_unavailable") from error
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("provider_version") != _PROVIDER_VERSION
    ):
        raise PremiumDashboardProviderError("incompatible_provider")
    return snapshot


def _premium_is_live(snapshot: dict[str, Any] | None) -> bool:
    return bool(
        snapshot
        and snapshot.get("license_status") == "valid"
        and (snapshot.get("premium_full_package") is True)
    )


def _premium_uses_mock(snapshot: dict[str, Any] | None) -> bool:
    """Return whether absence or an explicit non-valid licence permits demo data."""
    if snapshot is None:
        return True
    return snapshot.get("license_status") in _UNLICENSED_STATUSES


def _public_warnings(value: Any) -> list[dict[str, Any]]:
    """Return the small public warning contract used by the cockpit."""
    if not isinstance(value, list):
        return []
    field_limits = {
        "code": 64,
        "severity": 32,
        "label": 160,
        "title": 160,
        "category": 64,
        "icon_key": 64,
        "evidence_kind": 64,
        "recommended_action": 320,
        "message": 400,
        "start": 64,
        "end": 64,
    }
    allowed_field_values = {
        "code": {
            "hail",
            "dense_fog",
            "storm_wind",
            "freeze_risk",
            "heat_stress",
            "snow_or_ice",
            "strong_wind",
            "thunderstorm",
            "forecast_stale",
            "severe_weather",
            "heavy_precipitation",
            "high_precipitation_probability",
        },
        "severity": {"warning", "advisory", "critical"},
        "category": {"data_quality", "weather_hazard", "forecast_quality"},
        "icon_key": {
            "fog",
            "hail",
            "heat",
            "frost",
            "storm",
            "heavy_rain",
            "rain_likely",
            "snow_or_ice",
            "strong_wind",
            "data_quality",
            "thunderstorm",
            "severe_weather",
            "forecast_quality",
        },
        "evidence_kind": {"official_cap", "forecast_value", "condition_derived"},
    }
    public_warnings = []
    for warning in value[:4]:
        if not isinstance(warning, dict):
            continue
        public_warning = {}
        for field, length_limit in field_limits.items():
            raw_text = warning.get(field)
            if isinstance(raw_text, str) and raw_text.strip():
                text = raw_text.strip()
                if field in allowed_field_values:
                    if text not in allowed_field_values[field]:
                        continue
                public_warning[field] = text[:length_limit]
        contract_version = warning.get("contract_version")
        if type(contract_version) is int and contract_version == 1:
            public_warning["contract_version"] = contract_version
        official_alert = warning.get("official_alert")
        if isinstance(official_alert, bool):
            public_warning["official_alert"] = official_alert
        if not public_warning:
            continue
        public_warnings.append(public_warning)
        continue
    return public_warnings


def _eai_config(hass: HomeAssistant) -> dict[str, Any]:
    config_entries = hass.config_entries.async_entries("solar_forecast_eai")
    if not config_entries:
        return {}
    return {**dict(config_entries[0].data), **dict(config_entries[0].options)}


def _eai_feature_enabled(config: dict[str, Any], key: str) -> bool:
    return config.get(key) is True


def _overlay_unconfigured_features(
    payload: dict[str, Any], *, heat_pump: bool, wallbox: bool, mock: dict[str, Any]
) -> dict[str, Any]:
    """Keep mock tiles for premium devices that are licensed but not configured."""
    if not heat_pump:
        payload["heat_pump"] = {
            **_mapping(mock.get("heat_pump")),
            "configured": False,
            "is_demo": True,
            "sample_reason": "feature_off",
        }
    else:
        feature_payload = payload.setdefault("heat_pump", {})
        feature_payload["is_demo"] = False
        feature_payload["sample_reason"] = "live"
    if not wallbox:
        payload["wallbox"] = {
            **_mapping(mock.get("wallbox")),
            "configured": False,
            "is_demo": True,
            "sample_reason": "feature_off",
        }
    else:
        feature_payload = payload.setdefault("wallbox", {})
        feature_payload["is_demo"] = False
        feature_payload["sample_reason"] = "live"
    return payload if True else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _forecast_timeline(
    rows: list[Any], *, conservative_ratio: float | None, actual_cutoff_hour: int
) -> list[dict[str, float | int | None]]:
    """Return an honest hourly curve from SFML forecast and actual rows."""
    timeline = []
    for row in rows:
        try:
            hour = int(row["target_hour"])
        except (KeyError, TypeError, ValueError):
            continue
        forecast_kwh = _number(row["prediction_kwh"], 3)
        actual_kwh = (
            _number(row["actual_kwh"], 3) if hour <= actual_cutoff_hour else None
        )
        conservative_kwh = (
            _number(forecast_kwh * conservative_ratio, 3)
            if (forecast_kwh is not None and conservative_ratio is not None)
            and 0 <= conservative_ratio <= 1
            else None
        )
        model_predictions_kwh = []
        for field in ("physics_kwh", "ai_kwh", "tfs_kwh", "lstm_kwh", "ridge_kwh"):
            try:
                model_prediction_kwh = _number(row[field], 3)
            except (KeyError, TypeError):
                model_prediction_kwh = None
            if model_prediction_kwh is not None and model_prediction_kwh >= 0:
                model_predictions_kwh.append(model_prediction_kwh)
        corridor_low_kwh = (
            min(model_predictions_kwh) if len(model_predictions_kwh) >= 2 else None
        )
        corridor_high_kwh = (
            max(model_predictions_kwh) if len(model_predictions_kwh) >= 2 else None
        )
        timeline.append(
            {
                "hour": hour,
                "forecast_kwh": forecast_kwh,
                "p10_kwh": conservative_kwh,
                "actual_kwh": actual_kwh,
                "corridor_low_kwh": corridor_low_kwh,
                "corridor_high_kwh": corridor_high_kwh,
            }
        )
        continue
    return timeline


def _mock_payload(
    now: datetime, license_status: str, time_zone: str = "UTC"
) -> dict[str, Any]:
    generated_at_iso = now.isoformat()
    mock_forecast_curve = [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.044,
        0.159,
        0.335,
        0.547,
        0.741,
        0.899,
        1.041,
        1.129,
        1.188,
        1.129,
        0.988,
        0.794,
        0.6,
        0.388,
        0.212,
        0.088,
        0.018,
        0.0,
        0.0,
    ]
    mock_actual_curve = [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.04,
        0.14,
        0.31,
        0.55,
        0.72,
        0.79,
        0.7,
        0.75,
    ]
    mock_hourly_rows = [
        {
            "target_hour": hour,
            "prediction_kwh": hour_forecast_kwh,
            "actual_kwh": (
                mock_actual_curve[hour] if hour < len(mock_actual_curve) else None
            ),
            "physics_kwh": round(hour_forecast_kwh * 0.84, 3),
            "ai_kwh": round(hour_forecast_kwh * 1.13, 3),
            "tfs_kwh": round(hour_forecast_kwh * 0.94, 3),
        }
        for hour, hour_forecast_kwh in enumerate(mock_forecast_curve)
    ]
    actual_today_kwh = round(
        sum(
            (
                hour_actual_kwh
                for hour, hour_actual_kwh in enumerate(mock_actual_curve)
                if hour <= now.hour
            )
        ),
        2,
    )
    forecast_error_percent = round((actual_today_kwh / 10.3 - 1) * 100, 1)
    return {
        "schema_version": 1,
        "time_context": _time_context(None, time_zone),
        "generated_at": generated_at_iso,
        "mode": "mock",
        "is_demo": True,
        "sample_reason": "no_license",
        "premium": {
            "licensed": False,
            "license_status": license_status,
            "label": "Premium-Demo",
        },
        "freshness": {
            "energy": generated_at_iso,
            "forecast": generated_at_iso,
            "weather": generated_at_iso,
            "eai": generated_at_iso,
        },
        "energy": {
            "solar_power_w": 4820.0,
            "home_consumption_w": 1360.0,
            "grid_import_w": 0.0,
            "grid_export_w": 410.0,
            "battery_soc_percent": 68.0,
            "battery_power_w": 3050.0,
            "solar_to_house_w": 1360.0,
            "solar_to_battery_w": 3050.0,
            "battery_to_house_w": 0.0,
            "grid_to_house_w": 0.0,
            "grid_to_battery_w": 0.0,
            "solar_yield_today_kwh": actual_today_kwh,
            "home_consumption_today_kwh": 8.6,
            "autarky_percent": 94.0,
        },
        "forecast": {
            "accuracy_percent": 82.4,
            "last_completed_accuracy_percent": 86.7,
            "forecast_error_percent": forecast_error_percent,
            "actual_kwh": actual_today_kwh,
            "forecast_kwh": 10.3,
            "learning_hours": 6,
            "learning_candidates": 17,
            "discarded_hours": 1,
            "discarded_reason": "Nullexport/Basislast",
            "conservative_kwh": 9.0,
            "deviation_kwh": round(actual_today_kwh - 10.3, 1),
            "timeline": _forecast_timeline(
                mock_hourly_rows,
                conservative_ratio=0.8737864077669902,
                actual_cutoff_hour=now.hour,
            ),
        },
        "price": {
            "current_ct_per_kwh": 21.84,
            "next_ct_per_kwh": 19.42,
            "average_today_ct_per_kwh": 30.12,
            "is_cheap": True,
        },
        "heat_pump": {
            "configured": False,
            "is_demo": True,
            "sample_reason": "no_license",
            "power_kw": 1.8,
            "mode": "heating",
            "energy_today_kwh": 5.4,
            "pv_coverage_percent": 63.0,
            "recommendation": "PV-Fenster bis 15:30 Uhr nutzen",
            "data_quality_percent": 91.0,
        },
        "wallbox": {
            "configured": False,
            "is_demo": True,
            "sample_reason": "no_license",
            "power_kw": 0.0,
            "state": "connected",
            "energy_today_kwh": 0.0,
            "recommendation": "Ladestart ab 13:30 Uhr",
            "expected_pv_share_percent": 78.0,
            "departure_readiness_percent": 82.0,
        },
        "weather": {
            "configured": True,
            "is_demo": True,
            "available": True,
            "temperature_c": 18.6,
            "temperature_source": "mock",
            "condition": "partlycloudy",
            "cloud_cover_percent": 32.0,
            "wind_speed_kmh": 14.0,
            "solar_radiation_wm2": 612.0,
            "summary": "Gute Solarbedingungen, später zunehmende Bewölkung",
            "warnings": [
                {
                    "code": "high_precipitation_probability",
                    "severity": "advisory",
                    "label": "Morgen 78 % Regenrisiko",
                    "start": now.replace(hour=18, minute=0, second=0).isoformat(),
                }
            ],
        },
    }


def _first_entry_config(hass: HomeAssistant) -> dict[str, Any]:
    for entry_data in hass.data.get("sfml_stats", {}).values():
        if not isinstance(entry_data, dict):
            continue
        if not isinstance(entry_data.get("config"), dict):
            continue
        return entry_data["config"]
    return {}


async def _live_payload(
    hass: HomeAssistant, snapshot: dict[str, Any]
) -> dict[str, Any]:
    now = ha_now(hass)
    today_iso = ha_today(hass).isoformat()
    last_completed_hour = now.hour - 1
    async with _get_db() as connection:
        async with connection.execute(
            "\n            SELECT hp.target_date,\n                   hp.target_hour,\n                   hp.prediction_kwh AS prediction_kwh,\n                   hp.physics_kwh,\n                   hp.ai_kwh,\n                   hp.tfs_kwh,\n                   hp.lstm_kwh,\n                   hp.ridge_kwh,\n                   hp.actual_kwh,\n                   hp.mppt_throttled,\n                   hp.mppt_throttle_reason,\n                   hp.inverter_clipped,\n                   hp.exclude_from_learning,\n                   hp.exclude_from_clean_evaluation,\n                   hp.has_weather_alert,\n                   hp.weather_alert_type\n            FROM hourly_predictions hp\n            WHERE hp.target_date = ?\n            ORDER BY hp.target_hour\n            ",
            (today_iso,),
        ) as cursor:
            hourly_rows = await cursor.fetchall()
        async with connection.execute(
            "\n            SELECT timestamp, solar_power_w, house_consumption_w,\n                   battery_power_dc_w AS battery_power_w,\n                   battery_soc, solar_to_house_w,\n                   solar_to_battery_w, solar_to_grid_w, battery_to_house_w,\n                   grid_to_house_w, grid_to_battery_w\n            FROM stats_power_sources\n            ORDER BY timestamp DESC\n            LIMIT 1\n            "
        ) as cursor:
            power_source_row = await cursor.fetchone()
        async with connection.execute(
            "\n            SELECT total_actual_kwh, updated_at\n            FROM actual_live_state\n            WHERE date = ?\n            LIMIT 1\n            ",
            (today_iso,),
        ) as cursor:
            live_actual_row = await cursor.fetchone()
        async with connection.execute(
            "\n            SELECT conservative_planning_forecast_kwh,\n                   conservative_planning_forecast_updated_at\n            FROM daily_forecast_tracking\n            WHERE date = ?\n            ORDER BY id DESC\n            LIMIT 1\n            ",
            (today_iso,),
        ) as cursor:
            conservative_forecast_row = await cursor.fetchone()
        async with connection.execute(
            "\n            SELECT solar_yield_kwh, home_consumption_kwh, autarkie_percent\n            FROM stats_daily_energy\n            WHERE date = ?\n            LIMIT 1\n            ",
            (today_iso,),
        ) as cursor:
            daily_energy_row = await cursor.fetchone()
        price_row = None
        price_table_columns = await _table_columns(connection, "GPM_current_price")
        required_price_columns = {
            "id",
            "is_cheap",
            "timestamp",
            "total_price",
            "last_updated",
            "average_today",
            "price_next_hour",
        }
        if required_price_columns <= price_table_columns:
            async with connection.execute(
                "\n                SELECT timestamp, total_price, price_next_hour,\n                       average_today, is_cheap, last_updated\n                FROM GPM_current_price\n                WHERE id = 1\n                "
            ) as cursor:
                price_row = await cursor.fetchone()
        async with connection.execute(
            "\n            SELECT temperature_c, cloud_cover_percent, wind_speed_ms,\n                   solar_radiation_wm2, condition, timestamp\n            FROM hourly_weather_actual\n            WHERE date = ?\n            ORDER BY hour DESC\n            LIMIT 1\n            ",
            (today_iso,),
        ) as cursor:
            weather_row = await cursor.fetchone()
        async with connection.execute(
            "\n            SELECT accuracy_percent\n            FROM daily_summaries\n            WHERE date < ?\n              AND accuracy_percent IS NOT NULL\n            ORDER BY date DESC\n            LIMIT 1\n            ",
            (today_iso,),
        ) as cursor:
            completed_accuracy_row = await cursor.fetchone()
    clean_evaluation = _build_live_today_clean_eval(
        hourly_rows, evaluation_cutoff_hour=last_completed_hour
    )
    actual_today_kwh = _number(
        live_actual_row["total_actual_kwh"]
        if live_actual_row
        else clean_evaluation["production"]
    )
    forecast_today_kwh = _number(clean_evaluation["forecast"])
    deviation_kwh = (
        round(actual_today_kwh - forecast_today_kwh, 1)
        if actual_today_kwh is not None and forecast_today_kwh is not None
        else None
    )
    discarded_reason_counts = (
        clean_evaluation.get("discarded_learning_reason_breakdown") or {}
    )
    discarded_reason = next(iter(discarded_reason_counts), None)
    conservative_forecast_kwh = _number(
        conservative_forecast_row["conservative_planning_forecast_kwh"]
        if conservative_forecast_row
        else None
    )
    conservative_ratio = (
        conservative_forecast_kwh / forecast_today_kwh
        if conservative_forecast_kwh is not None
        and forecast_today_kwh is not None
        and (forecast_today_kwh > 0)
        else None
    )
    provider_sections = (
        snapshot.get("sections") if isinstance(snapshot.get("sections"), dict) else {}
    )
    overview = (
        provider_sections.get("overview")
        if isinstance(provider_sections.get("overview"), dict)
        else {}
    )
    operation = (
        provider_sections.get("operation")
        if isinstance(provider_sections.get("operation"), dict)
        else {}
    )
    mobility = (
        provider_sections.get("mobility")
        if isinstance(provider_sections.get("mobility"), dict)
        else {}
    )
    weather = (
        provider_sections.get("weather")
        if isinstance(provider_sections.get("weather"), dict)
        else {}
    )
    eai_config = _eai_config(hass)
    stats_config = _first_entry_config(hass)
    weather_entity = stats_config.get("weather_entity")
    weather_state = hass.states.get(weather_entity) if weather_entity else None
    weather_attributes = weather_state.attributes if weather_state is not None else {}
    sensor_temperature_c = (
        _number(weather_row["temperature_c"], 1)
        if weather_row and weather_row["temperature_c"] is not None
        else None
    )
    entity_temperature_c = _number(weather_attributes.get("temperature"), 1)
    temperature_c = (
        sensor_temperature_c
        if sensor_temperature_c is not None
        else entity_temperature_c
    )
    temperature_source = (
        "sensor"
        if sensor_temperature_c is not None
        else "weather_entity" if entity_temperature_c is not None else None
    )
    condition = (
        weather_row["condition"]
        if weather_row and weather_row["condition"]
        else weather_state.state if weather_state is not None else None
    )
    entity_cloud_cover = weather_attributes.get("cloud_coverage")
    if entity_cloud_cover is None:
        entity_cloud_cover = weather_attributes.get("cloud_cover")
    cloud_cover_percent = (
        _number(weather_row["cloud_cover_percent"], 0)
        if weather_row and weather_row["cloud_cover_percent"] is not None
        else _number(entity_cloud_cover, 0)
    )
    heat_pump_configured = bool(eai_config.get("heat_pump_enabled", False))
    wallbox_configured = bool(
        mobility.get("configured", eai_config.get("wallbox_enabled", False))
    )
    weather_intelligence_configured = bool(
        eai_config.get("weather_intelligence_enabled", False)
    )
    power_updated_at = power_source_row["timestamp"] if power_source_row else None
    energy_updated_at = (
        live_actual_row["updated_at"]
        if live_actual_row and live_actual_row["updated_at"]
        else power_updated_at
    )
    weather_warnings = weather.get("warnings")
    if not isinstance(weather_warnings, list):
        weather_warnings = weather.get("events")
    weather_warnings = _public_warnings(weather_warnings)
    return {
        "schema_version": 1,
        "time_context": _time_context(hass),
        "generated_at": now.isoformat(),
        "mode": "live",
        "is_demo": False,
        "sample_reason": "live",
        "premium": {
            "licensed": True,
            "license_status": snapshot.get("license_status"),
            "label": "Premium",
            "configuration_status": snapshot.get("configuration_status"),
            "capability_level": snapshot.get("capability_level"),
        },
        "freshness": {
            "energy": energy_updated_at,
            "forecast": energy_updated_at,
            "weather": (
                snapshot.get("data_freshness")
                if weather_intelligence_configured
                else weather_row["timestamp"] if weather_row else None
            ),
            "eai": snapshot.get("data_freshness"),
        },
        "energy": {
            "solar_power_w": (
                _number(power_source_row["solar_power_w"]) if power_source_row else None
            ),
            "home_consumption_w": (
                _number(power_source_row["house_consumption_w"])
                if power_source_row
                else None
            ),
            "grid_import_w": _sum_power(
                power_source_row["grid_to_house_w"] if power_source_row else None,
                power_source_row["grid_to_battery_w"] if power_source_row else None,
            ),
            "grid_export_w": (
                _number(power_source_row["solar_to_grid_w"])
                if power_source_row
                else None
            ),
            "battery_soc_percent": (
                _number(power_source_row["battery_soc"]) if power_source_row else None
            ),
            "battery_power_w": (
                _number(power_source_row["battery_power_w"])
                if power_source_row
                else None
            ),
            "solar_to_house_w": (
                _number(power_source_row["solar_to_house_w"])
                if power_source_row
                else None
            ),
            "solar_to_battery_w": (
                _number(power_source_row["solar_to_battery_w"])
                if power_source_row
                else None
            ),
            "battery_to_house_w": (
                _number(power_source_row["battery_to_house_w"])
                if power_source_row
                else None
            ),
            "grid_to_house_w": (
                _number(power_source_row["grid_to_house_w"])
                if power_source_row
                else None
            ),
            "grid_to_battery_w": (
                _number(power_source_row["grid_to_battery_w"])
                if power_source_row
                else None
            ),
            "solar_yield_today_kwh": _number(
                daily_energy_row["solar_yield_kwh"]
                if daily_energy_row
                else actual_today_kwh
            ),
            "home_consumption_today_kwh": _number(
                daily_energy_row["home_consumption_kwh"] if daily_energy_row else None
            ),
            "autarky_percent": _number(
                daily_energy_row["autarkie_percent"] if daily_energy_row else None, 1
            ),
        },
        "forecast": {
            "accuracy_percent": _number(
                clean_evaluation.get("forecast_accuracy_vs_sot_percent"), 1
            ),
            "last_completed_accuracy_percent": (
                _number(completed_accuracy_row["accuracy_percent"], 1)
                if completed_accuracy_row
                else None
            ),
            "forecast_error_percent": _number(
                clean_evaluation.get("live_day_forecast_error_vs_sot_percent"), 1
            ),
            "actual_kwh": actual_today_kwh,
            "forecast_kwh": forecast_today_kwh,
            "learning_hours": clean_evaluation.get("learning_hours_count"),
            "learning_candidates": clean_evaluation.get(
                "learning_candidate_hours_count"
            ),
            "discarded_hours": clean_evaluation.get("discarded_learning_hours_count"),
            "discarded_reason": discarded_reason,
            "conservative_kwh": conservative_forecast_kwh,
            "deviation_kwh": deviation_kwh,
            "timeline": _forecast_timeline(
                hourly_rows,
                conservative_ratio=conservative_ratio,
                actual_cutoff_hour=last_completed_hour,
            ),
        },
        "price": {
            "current_ct_per_kwh": (
                _number(price_row["total_price"]) if price_row else None
            ),
            "next_ct_per_kwh": (
                _number(price_row["price_next_hour"]) if price_row else None
            ),
            "average_today_ct_per_kwh": (
                _number(price_row["average_today"]) if price_row else None
            ),
            "is_cheap": bool(price_row["is_cheap"]) if price_row else None,
        },
        "heat_pump": {
            "configured": heat_pump_configured,
            "is_demo": False,
            "sample_reason": "live",
            "power_kw": _number(
                operation.get("current_power_kw", overview.get("current_power_kw"))
            ),
            "mode": operation.get("current_mode"),
            "energy_today_kwh": _number(overview.get("energy_today_kwh")),
            "pv_coverage_percent": _number(overview.get("pv_coverage_percent"), 1),
            "recommendation": (
                overview.get("why_now", {}).get("headline")
                if isinstance(overview.get("why_now"), dict)
                else None
            ),
            "data_quality_percent": _number(overview.get("data_quality_percent"), 1),
        },
        "wallbox": {
            "configured": wallbox_configured,
            "is_demo": False,
            "sample_reason": "live",
            "power_kw": _number(mobility.get("current_power_kw")),
            "state": mobility.get("state") or _wallbox_state(mobility),
            "energy_today_kwh": _number(mobility.get("energy_today_kwh")),
            "recommendation": mobility.get("recommendation")
            or mobility.get("recommended_action")
            or mobility.get("reason"),
            "expected_pv_share_percent": _number(
                mobility.get("expected_pv_share_percent"), 1
            ),
            "departure_readiness_percent": _number(
                mobility.get("departure_readiness_percent"), 1
            ),
        },
        "weather": {
            "configured": weather_intelligence_configured,
            "available": bool(
                weather_row or weather_state is not None or weather.get("available")
            ),
            "temperature_c": temperature_c,
            "temperature_source": temperature_source,
            "condition": condition,
            "cloud_cover_percent": cloud_cover_percent,
            "wind_speed_kmh": _number(
                (
                    float(weather_row["wind_speed_ms"]) * 3.6
                    if weather_row and weather_row["wind_speed_ms"] is not None
                    else None
                ),
                1,
            ),
            "solar_radiation_wm2": (
                _number(weather_row["solar_radiation_wm2"], 0) if weather_row else None
            ),
            "summary": (
                weather.get("status") if weather_intelligence_configured else None
            ),
            "warnings": weather_warnings,
        },
    }


class PremiumDashboardView(HomeAssistantView):
    """Serve the licensed solar cockpit or a complete premium demo."""

    url = "/api/sfml_stats/modern/premium-dashboard"
    name = "api:sfml_stats:modern:premium_dashboard"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        try:
            snapshot = _provider_snapshot(self.hass)
            if _premium_is_live(snapshot):
                payload = await _live_payload(self.hass, snapshot)
                eai_config = _eai_config(self.hass)
                payload = _overlay_unconfigured_features(
                    payload,
                    heat_pump=_eai_feature_enabled(eai_config, "heat_pump_enabled"),
                    wallbox=_eai_feature_enabled(eai_config, "wallbox_enabled"),
                    mock=_mock_payload(
                        ha_now(self.hass),
                        "valid",
                        getattr(self.hass.config, "time_zone", None) or "UTC",
                    ),
                )
            else:
                license_status = (
                    str(snapshot.get("license_status"))
                    if snapshot is not None
                    else "not_provided"
                )
                payload = _mock_payload(
                    ha_now(self.hass),
                    license_status,
                    getattr(self.hass.config, "time_zone", None) or "UTC",
                )
            return web.json_response(
                {"success": True, "data": payload},
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                },
            )
        except Exception as error:
            _LOGGER.error(
                "Premium dashboard request failed (%s)",
                type(error).__name__,
                exc_info=True,
            )
            return web.json_response(
                {
                    "success": False,
                    "error": {
                        "code": "premium_dashboard_unavailable",
                        "message": "Premium dashboard data is temporarily unavailable.",
                    },
                },
                status=503,
                headers={"Cache-Control": "no-store"},
            )


async def async_setup_premium_dashboard_view(hass: HomeAssistant) -> None:
    hass.http.register_view(PremiumDashboardView(hass))
