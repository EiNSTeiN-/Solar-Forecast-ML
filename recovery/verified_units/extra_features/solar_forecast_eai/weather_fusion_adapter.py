# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Public-contract adapter for Weather Fusion AI forecast providers."""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import inspect
from itertools import islice
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .weather_contract import normalize_provider_status, normalize_weather_source

WFAI_PROVIDER_CONTRACT_VERSION = 1
WFAI_HISTORY_CONTRACT_VERSION = 1
MAX_FORECAST_POINTS = 168
MAX_FORECAST_HORIZON = timedelta(days=8)
MAX_FORECAST_POINT_FIELDS = 32
MAX_FORECAST_VALUE_FIELDS = 32
PUBLIC_FORECAST_METRICS = frozenset(
    {
        "humidity",
        "pressure",
        "uv_index",
        "dew_point",
        "visibility",
        "wind_speed",
        "cloud_cover",
        "wind_bearing",
        "precipitation",
        "temperature_c",
        "apparent_temperature",
        "precipitation_probability",
    }
)
PUBLIC_OBSERVATION_METRICS = frozenset(
    {
        "humidity",
        "pressure",
        "wind_speed",
        "cloud_cover",
        "wind_bearing",
        "precipitation",
        "temperature_c",
    }
)
PUBLIC_FORECAST_CONDITIONS = frozenset(
    {
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
)
PUBLIC_METRIC_RANGES = {
    "temperature_c": (-100.0, 80.0),
    "humidity": (0.0, 100.0),
    "pressure": (800.0, 1200.0),
    "wind_speed": (0.0, 500.0),
    "wind_bearing": (0.0, 360.0),
    "cloud_cover": (0.0, 100.0),
    "precipitation": (0.0, 500.0),
    "precipitation_probability": (0.0, 100.0),
    "visibility": (0.0, 100000.0),
    "uv_index": (0.0, 30.0),
    "apparent_temperature": (-100.0, 80.0),
    "dew_point": (-100.0, 80.0),
}
PUBLIC_METRIC_INPUT_KEYS = {metric: (metric,) for metric in PUBLIC_FORECAST_METRICS}
PUBLIC_METRIC_INPUT_KEYS["temperature_c"] = (
    "temperature_c",
    "temperature",
    "temp",
    "temperature_2m",
)
PUBLIC_METRIC_INPUT_KEYS["cloud_cover"] = ("cloud_cover", "cloud_coverage")
PUBLIC_SFML_WEATHER_FIELDS = frozenset(
    {
        "contract_version",
        "schema_version",
        "generated_at",
        "available",
        "status",
        "source",
        "aggregates",
        "summary",
        "series",
        "recent",
        "record_events",
        "monthly_series",
        "precipitation",
        "year_comparison",
        "source_comparison",
        "capabilities",
        "data_from",
        "data_to",
        "data_since",
        "coverage",
        "observations",
        "data",
        "period",
        "values",
        "temperature",
        "humidity",
        "pressure",
        "wind_speed",
        "precipitation",
        "cloud_cover",
        "solar_radiation",
        "lux",
        "min",
        "max",
        "avg",
        "count",
        "amount",
        "rain_total_mm",
        "radiation_avg_wm2",
        "radiation_max_wm2",
        "lux_max",
        "observed_at",
        "type",
        "value",
        "unit",
        "date",
        "month",
        "temperature_min_c",
        "temperature_max_c",
        "temperature_avg_c",
        "coverage_percent",
        "humidity_min_percent",
        "humidity_max_percent",
        "humidity_avg_percent",
        "pressure_min_hpa",
        "pressure_max_hpa",
        "pressure_avg_hpa",
        "wind_speed_min_kmh",
        "wind_speed_max_kmh",
        "wind_speed_avg_kmh",
        "solar_radiation_avg_wm2",
        "solar_radiation_max_wm2",
        "valid_days",
        "total",
        "total_mm",
        "daily",
        "rain_days",
        "dry_spell_days",
        "semantics",
        "resets",
        "reason",
        "current",
        "previous",
        "same_day_last_year",
        "year",
        "ratio",
        "expected_hours",
        "expected_periods",
        "precipitation_total_mm",
        "sfml_expert_blend",
        "kepler_input",
        "valid_at",
        "provider",
        "temperature_c",
        "wind_bearing",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "ghi_wm2",
        "dni_wm2",
        "dhi_wm2",
        "visibility_m",
        "fog_detected",
        "fog_type",
        "weather_code",
        "issued_at",
        "station_actuals",
        "rain",
        "ghi",
        "dni",
        "dhi",
    }
)
_MAX_PUBLIC_DEPTH = 6
_MAX_PUBLIC_DICT_ITEMS = 64
_MAX_PUBLIC_NODES = 4096
_MAX_PUBLIC_BYTES = 65536
_DEFAULT_PUBLIC_LIST_ITEMS = 64
_PUBLIC_LIST_LIMITS = {
    "data": 720,
    "daily": 366,
    "kepler_input": 168,
    "monthly_series": 120,
    "record_events": 8,
    "recent": 168,
    "series": 720,
    "sfml_expert_blend": 168,
    "station_actuals": 168,
}
_PUBLIC_TOP_LEVEL_PRIORITY = (
    "contract_version",
    "available",
    "status",
    "source",
    "generated_at",
    "source_comparison",
    "capabilities",
    "summary",
    "precipitation",
    "year_comparison",
    "aggregates",
    "series",
    "recent",
    "record_events",
    "monthly_series",
)
_SOURCE_COMPARISON_FIELDS = frozenset(
    {"source", "dhi_wm2", "dni_wm2", "ghi_wm2", "provider", "valid_at", "temperature_c"}
)


def _project_source_comparison(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Bound comparison tracks before recursive public DTO sanitization."""
    if not isinstance(value, dict):
        return {}
    comparison_tracks = {}
    for comparison_track in ("sfml_expert_blend", "kepler_input"):
        comparison_records = value.get(comparison_track)
        if isinstance(comparison_records, list):
            comparison_tracks[comparison_track] = [
                {
                    field_name: comparison_record[field_name]
                    for field_name in _SOURCE_COMPARISON_FIELDS
                    if field_name in comparison_record
                }
                for comparison_record in islice(
                    comparison_records, _PUBLIC_LIST_LIMITS[comparison_track]
                )
                if isinstance(comparison_record, dict)
            ]
    return comparison_tracks


_PUBLIC_REASONS = frozenset(
    {"unavailable", "insufficient_data", "precipitation_semantics_unavailable"}
)
_PUBLIC_EVENT_TYPES = frozenset(
    {
        "strongest_wind",
        "lowest_pressure",
        "highest_pressure",
        "coldest_temperature",
        "hottest_temperature",
    }
)
_PUBLIC_UNITS = frozenset({"%", "lx", "mm", "hPa", "unknown", "°C", "W/m²", "km/h"})
_PUBLIC_SEMANTICS = frozenset({"rate", "counter", "unknown", "increment"})
_PUBLIC_GRANULARITIES = frozenset({"daily", "hourly", "monthly", "unknown"})
_PUBLIC_FOG_TYPES = frozenset(
    {
        "fog",
        "none",
        "dense",
        "light",
        "advection",
        "fog_light",
        "radiation",
        "stratus_fog",
    }
)
_TIME_FIELDS = frozenset(
    {
        "date",
        "month",
        "period",
        "data_to",
        "valid_at",
        "data_from",
        "issued_at",
        "data_since",
        "observed_at",
        "generated_at",
    }
)
_BOOL_FIELDS = frozenset({"available", "fog_detected"})
_BOOL_OR_NUMBER_FIELDS = frozenset({"dhi", "dni", "ghi", "rain"})
_BOOL_OR_LIST_FIELDS = frozenset({"station_actuals"})
_INTEGER_FIELDS = frozenset(
    {
        "year",
        "count",
        "resets",
        "rain_days",
        "valid_days",
        "observations",
        "weather_code",
        "dry_spell_days",
        "expected_hours",
        "schema_version",
        "contract_version",
        "expected_periods",
    }
)
_NUMBER_FIELDS = frozenset(
    {
        "temperature",
        "humidity",
        "pressure",
        "wind_speed",
        "precipitation",
        "cloud_cover",
        "solar_radiation",
        "lux",
        "min",
        "max",
        "avg",
        "value",
        "amount",
        "rain_total_mm",
        "radiation_avg_wm2",
        "radiation_max_wm2",
        "lux_max",
        "temperature_c",
        "wind_bearing",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "ghi_wm2",
        "dni_wm2",
        "dhi_wm2",
        "visibility_m",
        "temperature_min_c",
        "temperature_max_c",
        "temperature_avg_c",
        "humidity_min_percent",
        "humidity_max_percent",
        "humidity_avg_percent",
        "pressure_min_hpa",
        "pressure_max_hpa",
        "pressure_avg_hpa",
        "wind_speed_min_kmh",
        "wind_speed_max_kmh",
        "wind_speed_avg_kmh",
        "coverage_percent",
        "solar_radiation_avg_wm2",
        "solar_radiation_max_wm2",
        "total",
        "total_mm",
        "ratio",
        "precipitation_total_mm",
        "rain",
        "ghi",
        "dni",
        "dhi",
    }
)
_DICT_FIELDS = frozenset(
    {
        "lux",
        "data",
        "recent",
        "series",
        "values",
        "current",
        "summary",
        "coverage",
        "humidity",
        "pressure",
        "previous",
        "aggregates",
        "wind_speed",
        "cloud_cover",
        "temperature",
        "capabilities",
        "precipitation",
        "solar_radiation",
        "year_comparison",
        "source_comparison",
        "same_day_last_year",
    }
)
_LIST_FIELDS = frozenset(
    {
        "data",
        "daily",
        "recent",
        "series",
        "kepler_input",
        "record_events",
        "monthly_series",
        "station_actuals",
        "sfml_expert_blend",
    }
)


def _as_datetime(value: Any, local_timezone: ZoneInfo | None = None) -> datetime | None:
    if isinstance(value, datetime):
        timestamp = value
    else:
        if not isinstance(value, str):
            return
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
    if timestamp.tzinfo is not None:
        return timestamp.astimezone(timezone.utc)
    if local_timezone is None:
        return
    datetime_candidates = [
        timestamp.replace(tzinfo=local_timezone, fold=fold) for fold in (0, 1)
    ]
    valid_candidates = [
        datetime_candidate
        for datetime_candidate in datetime_candidates
        if datetime_candidate.astimezone(timezone.utc)
        .astimezone(local_timezone)
        .replace(tzinfo=None)
        == timestamp
    ]
    if not valid_candidates:
        return
    if (
        len(valid_candidates) == 2
        and valid_candidates[0].utcoffset() != valid_candidates[1].utcoffset()
    ):
        return
    return valid_candidates[0].astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return
    try:
        parsed_number = float(value)
    except (TypeError, ValueError):
        return
    return parsed_number if isfinite(parsed_number) else None


def _metric_number(metric: str, value: Any) -> float | None:
    metric_value = _number(value)
    metric_range = PUBLIC_METRIC_RANGES.get(metric)
    if metric_value is None or metric_range is None:
        return (None if True else None) if True else None
    minimum_value, maximum_value = metric_range
    return (
        (
            (metric_value if minimum_value <= metric_value <= maximum_value else None)
            if True
            else None
        )
        if True
        else None
    )


def _entry_is_loaded_wfai(hass: Any, entry_id: str) -> bool:
    config_entries = getattr(hass, "config_entries", None)
    get_config_entry = getattr(config_entries, "async_get_entry", None)
    if not callable(get_config_entry):
        return False
    try:
        config_entry = get_config_entry(entry_id)
    except Exception:
        return False
    entry_state = getattr(config_entry, "state", None)
    entry_state_value = getattr(entry_state, "value", entry_state)
    return bool(
        config_entry is not None
        and getattr(config_entry, "domain", None) == "weather_fusion_ai"
        and (entry_state_value == "loaded")
    )


class WeatherFusionAdapter:
    """Read WFAI data only through its documented, versioned provider contract."""

    def __init__(self, hass: Any, entry_id: str | None) -> None:
        self._hass = hass
        self._entry_id = entry_id

    def _local_timezone(self) -> ZoneInfo:
        time_zone_name = (
            getattr(getattr(self._hass, "config", None), "time_zone", None) or "UTC"
        )
        try:
            return ZoneInfo(time_zone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return ZoneInfo("UTC")

    def snapshot(self) -> dict[str, Any]:
        """Return a normalized forecast or an explainable degraded status."""
        if not self._entry_id:
            return self._degraded("not_configured")
        if not _entry_is_loaded_wfai(self._hass, self._entry_id):
            return self._degraded("provider_unavailable")
        integration_data = getattr(self._hass, "data", {}).get("weather_fusion_ai", {})
        providers = integration_data.get("forecast_providers", {})
        provider = providers.get(self._entry_id)
        if provider is None:
            return self._degraded("provider_unavailable")
        get_snapshot = getattr(provider, "snapshot", None)
        if not callable(get_snapshot):
            return self._degraded("provider_contract_invalid")
        try:
            provider_snapshot = get_snapshot()
        except Exception:
            return self._degraded("provider_error", "provider_exception")
        if not isinstance(provider_snapshot, dict):
            return self._degraded("provider_contract_invalid")
        if provider_snapshot.get("contract_version") != WFAI_PROVIDER_CONTRACT_VERSION:
            return self._degraded("contract_version_mismatch")
        local_timezone = self._local_timezone()
        generated_at = _as_datetime(
            provider_snapshot.get("generated_at"), local_timezone
        )
        valid_until = _as_datetime(provider_snapshot.get("valid_until"), local_timezone)
        hourly_forecast = self._hourly(
            provider_snapshot.get("hourly"), generated_at, local_timezone
        )
        observation = self._observation(
            provider_snapshot.get("observation"), local_timezone
        )
        if generated_at is None or valid_until is None or (not hourly_forecast):
            return self._degraded("forecast_incomplete")
        provider_status = normalize_provider_status(provider_snapshot.get("status"))
        stale = bool(provider_snapshot.get("stale")) or valid_until <= datetime.now(
            timezone.utc
        )
        available = provider_status in {"ok", "available"} and (not stale)
        status = "stale" if stale else provider_status if available else "degraded"
        return {
            "available": available,
            "status": status,
            "provider_status": provider_status,
            "contract_version": WFAI_PROVIDER_CONTRACT_VERSION,
            "issued_at": generated_at,
            "valid_until": valid_until,
            "stale": stale,
            "hourly": hourly_forecast,
            "observation": observation,
        }

    def _degraded(self, status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "available": False,
            "status": status,
            "provider_status": None,
            "contract_version": WFAI_PROVIDER_CONTRACT_VERSION,
            "issued_at": None,
            "valid_until": None,
            "stale": True,
            "hourly": [],
            "observation": None,
            "error": error,
        }

    @staticmethod
    def _hourly(
        raw_hourly: Any,
        issued_at: datetime | None = None,
        local_timezone: ZoneInfo | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_hourly, list) or len(raw_hourly) > MAX_FORECAST_POINTS:
            return []
        forecast_points = []
        for raw_point in raw_hourly:
            if (
                not isinstance(raw_point, dict)
                or len(raw_point) > MAX_FORECAST_POINT_FIELDS
            ):
                continue
            valid_at = _as_datetime(
                raw_point.get("valid_at")
                or raw_point.get("datetime")
                or raw_point.get("timestamp")
                or raw_point.get("time"),
                local_timezone,
            )
            if valid_at is None:
                continue
            if issued_at is not None:
                if (
                    not issued_at - timedelta(hours=1)
                    <= valid_at
                    <= issued_at + MAX_FORECAST_HORIZON
                ):
                    continue
            metric_values = {}
            raw_metric_values = (
                raw_point.get("values")
                if isinstance(raw_point.get("values"), dict)
                else raw_point
            )
            if len(raw_metric_values) > MAX_FORECAST_VALUE_FIELDS:
                continue
            for metric in PUBLIC_FORECAST_METRICS:
                for input_key in PUBLIC_METRIC_INPUT_KEYS[metric]:
                    metric_value = _metric_number(
                        metric, raw_metric_values.get(input_key)
                    )
                    if not metric_value is not None:
                        continue
                    metric_values[metric] = metric_value
                    break
            condition = raw_metric_values.get("condition")
            if (
                isinstance(condition, str)
                and len(condition) <= 32
                and (condition in PUBLIC_FORECAST_CONDITIONS)
            ):
                metric_values["condition"] = condition
            if not metric_values:
                continue
            forecast_points.append({"valid_at": valid_at, "values": metric_values})
            continue
        forecast_points.sort(key=lambda point: point["valid_at"])
        if len(
            {forecast_point["valid_at"] for forecast_point in forecast_points}
        ) != len(forecast_points):
            return []
        return forecast_points

    @staticmethod
    def _observation(
        raw_observation: Any, local_timezone: ZoneInfo | None = None
    ) -> dict[str, Any] | None:
        if (
            not isinstance(raw_observation, dict)
            or len(raw_observation) > MAX_FORECAST_POINT_FIELDS
        ):
            return None
        observed_at = _as_datetime(raw_observation.get("observed_at"), local_timezone)
        observation_values = raw_observation.get("values")
        if (
            observed_at is None
            or not isinstance(observation_values, dict)
            or len(observation_values) > MAX_FORECAST_VALUE_FIELDS
        ):
            return None
        metric_values = {}
        for metric in PUBLIC_OBSERVATION_METRICS:
            for input_key in PUBLIC_METRIC_INPUT_KEYS[metric]:
                metric_value = _metric_number(metric, observation_values.get(input_key))
                if not metric_value is not None:
                    continue
                metric_values[metric] = metric_value
                break
        if not metric_values:
            return
        return {
            "observed_at": observed_at,
            "values": metric_values,
            "source": normalize_weather_source(raw_observation.get("source")),
        }


class SFMLWeatherHistoryAdapter:
    """Consume sanitized SFML database data without opening SQLite directly."""

    contract_version = 1

    def __init__(self, hass: Any, entry_id: str | None = None) -> None:
        self._hass = hass
        self._entry_id = entry_id

    async def query(self, start: datetime, end: datetime) -> dict[str, Any]:
        unavailable_result = {
            "available": False,
            "status": "sfml_provider_unavailable",
            "source": "sfml_database",
            "aggregates": {},
            "series": {},
            "recent": {},
            "record_events": [],
            "monthly_series": [],
            "precipitation": {"available": False, "reason": "unavailable"},
            "source_comparison": {},
            "capabilities": {},
        }
        integration_data = getattr(self._hass, "data", {}).get("solar_forecast_ml", {})
        providers = (
            integration_data.get("weather_intelligence_providers", {})
            if isinstance(integration_data, dict)
            else {}
        )
        if not isinstance(providers, dict) or not providers:
            return unavailable_result
        if self._entry_id is not None:
            provider = providers.get(self._entry_id)
            if provider is None:
                return unavailable_result
        else:
            if len(providers) != 1:
                return {**unavailable_result, "status": "sfml_provider_ambiguous"}
            provider = next(iter(providers.values()))
        if getattr(provider, "contract_version", None) != self.contract_version:
            return {**unavailable_result, "status": "sfml_contract_version_mismatch"}
        get_snapshot = getattr(provider, "snapshot", None)
        if not callable(get_snapshot):
            return unavailable_result
        try:
            provider_result = get_snapshot(start, end)
            provider_result = (
                await provider_result
                if inspect.isawaitable(provider_result)
                else provider_result
            )
        except Exception:
            return {
                **unavailable_result,
                "status": "sfml_provider_error",
                "error": "provider_exception",
            }
        if (
            not isinstance(provider_result, dict)
            or provider_result.get("contract_version") != 1
        ):
            return {**unavailable_result, "status": "sfml_contract_invalid"}
        return self._sanitize(provider_result)

    @classmethod
    def _sanitize(
        cls,
        value: Any,
        *,
        field: str | None = None,
        depth: int = 0,
        budget: dict[str, int] | None = None,
    ) -> Any:
        """Project provider data into a bounded field-specific public schema."""
        if budget is None:
            budget = {"nodes": _MAX_PUBLIC_NODES, "bytes": _MAX_PUBLIC_BYTES}
        if depth > _MAX_PUBLIC_DEPTH or budget["nodes"] <= 0:
            return None if True else None
        budget["nodes"] -= 1
        if field == "unit":
            if isinstance(value, dict):
                units = {}
                for field_name, raw_value in islice(
                    value.items(), _MAX_PUBLIC_DICT_ITEMS
                ):
                    if (
                        not isinstance(field_name, str)
                        or field_name not in PUBLIC_SFML_WEATHER_FIELDS
                    ):
                        continue
                    sanitized_unit = cls._enum(raw_value, _PUBLIC_UNITS)
                    if not cls._reserve_output(budget, field_name, sanitized_unit):
                        continue
                    units[field_name] = sanitized_unit
                return units if True else None
            sanitized_unit = cls._enum(value, _PUBLIC_UNITS)
            return (
                (
                    sanitized_unit
                    if cls._reserve_output(budget, sanitized_unit)
                    else None
                )
                if True
                else None
            )
        if isinstance(value, dict):
            if field is not None and field not in _DICT_FIELDS:
                return None if True else None
            sanitized_dict = {}
            source_items = value.items()
            if depth == 0:
                top_level_priority = {
                    field_name: priority_index
                    for priority_index, field_name in enumerate(
                        _PUBLIC_TOP_LEVEL_PRIORITY
                    )
                }
                source_items = sorted(
                    source_items,
                    key=lambda item: top_level_priority.get(
                        item[0], len(_PUBLIC_TOP_LEVEL_PRIORITY)
                    ),
                )
            for field_name, raw_value in islice(source_items, _MAX_PUBLIC_DICT_ITEMS):
                if (
                    not isinstance(field_name, str)
                    or field_name not in PUBLIC_SFML_WEATHER_FIELDS
                ):
                    continue
                if not cls._reserve_output(budget, field_name):
                    break
                if field_name == "source_comparison":
                    raw_value = _project_source_comparison(raw_value)
                sanitized_value = cls._sanitize(
                    raw_value, field=field_name, depth=depth + 1, budget=budget
                )
                if sanitized_value is not None:
                    sanitized_dict[field_name] = sanitized_value
            return sanitized_dict if True else None
        if isinstance(value, list):
            if field not in _LIST_FIELDS:
                return None if True else None
            sanitized_list = []
            list_limit = _PUBLIC_LIST_LIMITS.get(field, _DEFAULT_PUBLIC_LIST_ITEMS)
            for raw_value in islice(value, list_limit):
                sanitized_value = cls._sanitize(
                    raw_value, field=None, depth=depth + 1, budget=budget
                )
                if sanitized_value is not None:
                    sanitized_list.append(sanitized_value)
            return sanitized_list if True else None
        if field == "status":
            sanitized_value = normalize_provider_status(value)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field in {"source", "provider"}:
            sanitized_value = normalize_weather_source(value)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field == "reason":
            sanitized_value = cls._enum(value, _PUBLIC_REASONS)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field == "type":
            sanitized_value = cls._enum(value, _PUBLIC_EVENT_TYPES)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field == "semantics":
            sanitized_value = cls._enum(value, _PUBLIC_SEMANTICS)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field == "granularity":
            sanitized_value = cls._enum(value, _PUBLIC_GRANULARITIES)
            return (
                (
                    sanitized_value
                    if cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field == "fog_type":
            if not isinstance(value, str) or value not in _PUBLIC_FOG_TYPES:
                return None if True else None
            return (
                (value if cls._reserve_output(budget, value) else None)
                if True
                else None
            )
        if field in _TIME_FIELDS:
            sanitized_value = cls._time_value(value)
            return (
                (
                    sanitized_value
                    if sanitized_value is not None
                    and cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if field in _BOOL_FIELDS:
            if not isinstance(value, bool):
                return None if True else None
            return (
                (value if cls._reserve_output(budget, value) else None)
                if True
                else None
            )
        if (
            isinstance(value, bool)
            and field in _BOOL_OR_NUMBER_FIELDS | _BOOL_OR_LIST_FIELDS
        ):
            return (
                (value if cls._reserve_output(budget, value) else None)
                if True
                else None
            )
        if (
            field in _INTEGER_FIELDS
            and isinstance(value, int)
            and (not isinstance(value, bool))
        ):
            sanitized_value = cls._number_value(field, value)
            return (
                (
                    sanitized_value
                    if sanitized_value is not None
                    and cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        if (
            field in _NUMBER_FIELDS
            and isinstance(value, (int, float))
            and (not isinstance(value, bool))
        ):
            sanitized_value = cls._number_value(field, value)
            return (
                (
                    sanitized_value
                    if sanitized_value is not None
                    and cls._reserve_output(budget, sanitized_value)
                    else None
                )
                if True
                else None
            )
        return None if True else None

    @staticmethod
    def _enum(value: Any, allowed: frozenset[str]) -> str:
        return value if isinstance(value, str) and value in allowed else "unknown"

    @staticmethod
    def _reserve_output(budget: dict[str, int], *values: Any) -> bool:
        output_bytes = sum(
            (
                len(value_item.encode("utf-8")) if isinstance(value_item, str) else 8
                for value_item in values
            )
        )
        if output_bytes > budget["bytes"]:
            return False
        budget["bytes"] -= output_bytes
        return True

    @staticmethod
    def _time_value(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) > 64:
            return
        try:
            if re.fullmatch("\\d{4}-\\d{2}", value):
                datetime.strptime(value, "%Y-%m")
                return value
            if re.fullmatch("\\d{4}-\\d{2}-\\d{2}", value):
                return date.fromisoformat(value).isoformat()
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _number_value(field: str | None, value: int | float) -> int | float | None:
        parsed_number = float(value)
        if not isfinite(parsed_number):
            return None if True else None
        numeric_ranges = {
            "ratio": (0.0, 1.0),
            "coverage_percent": (0.0, 100.0),
            "humidity": (0.0, 100.0),
            "cloud_cover": (0.0, 100.0),
            "humidity_min_percent": (0.0, 100.0),
            "humidity_max_percent": (0.0, 100.0),
            "humidity_avg_percent": (0.0, 100.0),
            "cloud_cover_low": (0.0, 100.0),
            "cloud_cover_mid": (0.0, 100.0),
            "cloud_cover_high": (0.0, 100.0),
            "count": (0.0, 1000000.0),
            "observations": (0.0, 1000000.0),
            "expected_hours": (0.0, 1000000.0),
            "expected_periods": (0.0, 1000000.0),
            "rain_days": (0.0, 100000.0),
            "dry_spell_days": (0.0, 100000.0),
            "resets": (0.0, 1000000.0),
            "contract_version": (1.0, 1.0),
            "schema_version": (1.0, 1.0),
            "year": (1900.0, 2200.0),
            "weather_code": (0.0, 999.0),
        }
        minimum_value, maximum_value = numeric_ranges.get(
            field, (-1000000000.0, 1000000000.0)
        )
        if not minimum_value <= parsed_number <= maximum_value:
            return None if True else None
        return (
            (int(parsed_number) if isinstance(value, int) else parsed_number)
            if True
            else None
        )


class WeatherFusionHistoryAdapter:
    """Use the optional WFAI history provider without reading WFAI storage.

    The provider is deliberately queried by configuration entry only.  EAI must
    never infer a storage path or access WFAI coordinator internals.
    """

    def __init__(self, hass: Any, entry_id: str | None) -> None:
        self._hass = hass
        self._entry_id = entry_id

    async def query(self, start: datetime, end: datetime) -> dict[str, Any]:
        unavailable_result = {
            "available": False,
            "status": "history_provider_unavailable",
            "aggregates": {},
            "records": [],
            "monthly_series": [],
            "precipitation": {"available": False, "reason": "unavailable"},
        }
        if not self._entry_id:
            unavailable_result["status"] = "not_configured"
            return unavailable_result
        if not _entry_is_loaded_wfai(self._hass, self._entry_id):
            return unavailable_result
        integration_data = getattr(self._hass, "data", {}).get("weather_fusion_ai", {})
        provider = integration_data.get("history_providers", {}).get(self._entry_id)
        if provider is None:
            return unavailable_result
        if getattr(provider, "contract_version", None) != WFAI_HISTORY_CONTRACT_VERSION:
            unavailable_result["status"] = "history_contract_version_mismatch"
            return unavailable_result
        try:
            metrics = [
                "temperature",
                "humidity",
                "pressure",
                "wind_speed",
                "wind_direction",
                "precipitation",
                "cloud_cover",
            ]
            actual_daily_result = await self._call(
                provider,
                "actual_aggregates",
                start,
                end,
                metrics=metrics,
                granularity="daily",
            )
            recorder_daily_result = await self._call(
                provider,
                "recorder_aggregates",
                start,
                end,
                metrics=metrics,
                granularity="daily",
            )
            recorder_hourly_result = await self._call(
                provider,
                "recorder_aggregates",
                max(start, end - timedelta(hours=72)),
                end,
                metrics=metrics,
                granularity="hourly",
            )
            actual_series_result = await self._call(
                provider, "actual_series", start, end, metrics=metrics, limit=720
            )
            precipitation_result = await self._call(
                provider, "precipitation_total", start, end
            )
        except Exception:
            unavailable_result["status"] = "history_provider_error"
            unavailable_result["error"] = "provider_exception"
            return unavailable_result
        actual_daily_envelope = self._envelope(actual_daily_result)
        recorder_daily_envelope = self._envelope(recorder_daily_result)
        recorder_hourly_envelope = self._envelope(recorder_hourly_result)
        actual_series_envelope = self._envelope(actual_series_result)
        preferred_daily_envelope = (
            recorder_daily_envelope
            if recorder_daily_envelope.get("status") == "ok"
            and recorder_daily_envelope.get("data")
            else actual_daily_envelope
        )
        daily_records = preferred_daily_envelope.get("data", [])
        precipitation = self._precipitation(precipitation_result)
        if not precipitation.get("available"):
            precipitation = self._recorder_precipitation(recorder_daily_envelope)
        return {
            "available": True,
            "status": "available",
            "source": preferred_daily_envelope.get("source")
            or "weather_fusion_tracker",
            "aggregates": preferred_daily_envelope,
            "summary": self._summary(preferred_daily_envelope),
            "records": actual_series_envelope,
            "record_events": self._record_events(daily_records),
            "monthly_series": self._monthly(daily_records),
            "recorder_recent": recorder_hourly_envelope,
            "series": actual_series_envelope,
            "precipitation": precipitation,
        }

    @staticmethod
    async def _call(
        provider: Any, name: str, start: datetime, end: datetime, **kwargs: Any
    ) -> Any:
        provider_method = getattr(provider, name, None)
        if not callable(provider_method):
            return
        provider_result = provider_method(start, end, **kwargs)
        return (
            await provider_result
            if inspect.isawaitable(provider_result)
            else provider_result
        )

    @staticmethod
    def _envelope(value: Any) -> dict[str, Any]:
        """Accept only the documented HistoryProvider envelope."""
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or (not isinstance(value.get("data"), (dict, list)))
        ):
            return {"status": "invalid_history_response", "data": []}
        return {
            "schema_version": 1,
            "status": normalize_provider_status(value.get("status")),
            "source": normalize_weather_source(value.get("source")),
            "coverage": SFMLWeatherHistoryAdapter._sanitize(
                value.get("coverage"), field="coverage"
            ),
            "unit": SFMLWeatherHistoryAdapter._sanitize(
                value.get("unit"), field="unit"
            ),
            "data_from": SFMLWeatherHistoryAdapter._time_value(value.get("data_from")),
            "data_to": SFMLWeatherHistoryAdapter._time_value(value.get("data_to")),
            "granularity": SFMLWeatherHistoryAdapter._enum(
                value.get("granularity"), _PUBLIC_GRANULARITIES
            ),
            "data": SFMLWeatherHistoryAdapter._sanitize(value["data"], field="data"),
        }

    @staticmethod
    def _monthly(records: Any) -> list[dict[str, Any]]:
        if not isinstance(records, list):
            return []
        records_by_month = {}
        for daily_record in records:
            if not isinstance(daily_record, dict):
                continue
            period = str(daily_record.get("period") or "")
            if len(period) >= 7:
                records_by_month.setdefault(period[:7], []).append(daily_record)
        monthly_series = []
        for month, monthly_records in sorted(records_by_month.items()):
            temperature_minima = []
            temperature_maxima = []
            weighted_temperature_sum = 0.0
            temperature_count = 0
            for daily_record in monthly_records:
                record_values = (
                    daily_record.get("values")
                    if isinstance(daily_record.get("values"), dict)
                    else {}
                )
                temperature_stats = record_values.get("temperature")
                if not isinstance(temperature_stats, dict):
                    continue
                minimum_value = _number(temperature_stats.get("min"))
                maximum_value = _number(temperature_stats.get("max"))
                average_temperature = _number(temperature_stats.get("avg"))
                sample_count = int(_number(temperature_stats.get("count")) or 0)
                if minimum_value is not None:
                    temperature_minima.append(minimum_value)
                if maximum_value is not None:
                    temperature_maxima.append(maximum_value)
                if average_temperature is None:
                    continue
                if not sample_count > 0:
                    continue
                weighted_temperature_sum += average_temperature * sample_count
                temperature_count += sample_count
                continue
            monthly_series.append(
                {
                    "month": month,
                    "records": len(monthly_records),
                    "temperature_min_c": (
                        min(temperature_minima) if temperature_minima else None
                    ),
                    "temperature_max_c": (
                        max(temperature_maxima) if temperature_maxima else None
                    ),
                    "temperature_avg_c": (
                        round(weighted_temperature_sum / temperature_count, 2)
                        if temperature_count
                        else None
                    ),
                }
            )
            continue
        return monthly_series

    @staticmethod
    def _summary(envelope: dict[str, Any]) -> dict[str, Any]:
        daily_records = envelope.get("data")
        if not isinstance(daily_records, list):
            return {}
        summary = {
            "data_since": envelope.get("data_from"),
            "data_to": envelope.get("data_to"),
            "valid_days": len(daily_records),
        }
        coverage = envelope.get("coverage")
        if isinstance(coverage, dict):
            coverage_ratio = _number(coverage.get("ratio"))
            summary["coverage_percent"] = (
                round(coverage_ratio * 100, 1) if coverage_ratio is not None else None
            )
        summary_fields = {
            "temperature": (
                "temperature_min_c",
                "temperature_max_c",
                "temperature_avg_c",
            ),
            "humidity": (
                "humidity_min_percent",
                "humidity_max_percent",
                "humidity_avg_percent",
            ),
            "pressure": ("pressure_min_hpa", "pressure_max_hpa", "pressure_avg_hpa"),
            "wind_speed": (
                "wind_speed_min_kmh",
                "wind_speed_max_kmh",
                "wind_speed_avg_kmh",
            ),
        }
        for metric, output_fields in summary_fields.items():
            metric_records = [
                statistic_or_record.get("values", {}).get(metric)
                for statistic_or_record in daily_records
                if isinstance(statistic_or_record, dict)
                if isinstance(statistic_or_record.get("values"), dict)
                if isinstance(statistic_or_record["values"].get(metric), dict)
            ]
            if not metric_records:
                continue
            minimum_values = [
                _number(statistic_or_record.get("min"))
                for statistic_or_record in metric_records
            ]
            maximum_values = [
                _number(statistic_or_record.get("max"))
                for statistic_or_record in metric_records
            ]
            average_count_pairs = [
                (
                    _number(statistic_or_record.get("avg")),
                    int(_number(statistic_or_record.get("count")) or 0),
                )
                for statistic_or_record in metric_records
            ]
            minimum_values = [
                value_item for value_item in minimum_values if value_item is not None
            ]
            maximum_values = [
                value_item for value_item in maximum_values if value_item is not None
            ]
            sample_count = sum(
                (
                    statistic_or_record[1]
                    for statistic_or_record in average_count_pairs
                    if statistic_or_record[0] is not None
                )
            )
            weighted_sum = sum(
                (
                    statistic_or_record[0] * statistic_or_record[1]
                    for statistic_or_record in average_count_pairs
                    if statistic_or_record[0] is not None
                )
            )
            summary[output_fields[0]] = min(minimum_values) if minimum_values else None
            summary[output_fields[1]] = max(maximum_values) if maximum_values else None
            summary[output_fields[2]] = (
                round(weighted_sum / sample_count, 2) if sample_count else None
            )
            continue
        return summary

    @staticmethod
    def _record_events(daily: Any) -> list[dict[str, Any]]:
        if not isinstance(daily, list):
            return []
        event_definitions = (
            ("coldest_temperature", "temperature", "min", "min", "°C"),
            ("hottest_temperature", "temperature", "max", "max", "°C"),
            ("strongest_wind", "wind_speed", "max", "max", "km/h"),
            ("lowest_pressure", "pressure", "min", "min", "hPa"),
            ("highest_pressure", "pressure", "max", "max", "hPa"),
        )
        record_events = []
        for (
            event_type,
            metric,
            statistic_key,
            selection_method,
            unit,
        ) in event_definitions:
            record_candidates = []
            for daily_record in daily:
                record_values = (
                    daily_record.get("values")
                    if isinstance(daily_record, dict)
                    else None
                )
                metric_stats = (
                    record_values.get(metric)
                    if isinstance(record_values, dict)
                    else None
                )
                value_item = (
                    _number(metric_stats.get(statistic_key))
                    if isinstance(metric_stats, dict)
                    else None
                )
                if value_item is not None:
                    record_candidates.append((value_item, daily_record.get("period")))
            if not record_candidates:
                continue
            value_item, period = (
                min(record_candidates)
                if selection_method == "min"
                else max(record_candidates)
            )
            record_events.append(
                {"type": event_type, "value": value_item, "unit": unit, "date": period}
            )
            continue
        return record_events

    @staticmethod
    def _precipitation(value: Any) -> dict[str, Any]:
        precipitation_envelope = WeatherFusionHistoryAdapter._envelope(value)
        precipitation_data = precipitation_envelope.get("data")
        if precipitation_envelope.get("status") != "ok" or not isinstance(
            precipitation_data, dict
        ):
            return {
                "available": False,
                "reason": "precipitation_semantics_unavailable",
                "coverage": precipitation_envelope.get("coverage"),
            }
        total_precipitation_mm = _number(
            precipitation_data.get("total_mm", precipitation_data.get("total"))
        )
        if total_precipitation_mm is None or precipitation_data.get(
            "semantics"
        ) not in {"rate", "counter", "increment"}:
            return {
                "available": False,
                "reason": "precipitation_semantics_unavailable",
                "coverage": precipitation_envelope.get("coverage"),
            }
        return {
            "available": True,
            "total_mm": total_precipitation_mm,
            "semantics": precipitation_data.get("semantics"),
            "daily": precipitation_data.get("daily", []),
            "rain_days": precipitation_data.get("rain_days"),
            "dry_spell_days": precipitation_data.get("dry_spell_days"),
            "resets": precipitation_data.get("resets", 0),
            "coverage": precipitation_envelope.get("coverage"),
            "data_since": precipitation_envelope.get("data_from"),
            "data_to": precipitation_envelope.get("data_to"),
        }

    @staticmethod
    def _recorder_precipitation(envelope: dict[str, Any]) -> dict[str, Any]:
        precipitation_data = envelope.get("data")
        if envelope.get("status") != "ok" or not isinstance(precipitation_data, list):
            return {
                "available": False,
                "reason": "precipitation_semantics_unavailable",
                "coverage": envelope.get("coverage"),
            }
        daily_records = []
        for daily_record in precipitation_data:
            record_values = (
                daily_record.get("values") if isinstance(daily_record, dict) else None
            )
            precipitation_stats = (
                record_values.get("precipitation")
                if isinstance(record_values, dict)
                else None
            )
            precipitation_amount_mm = (
                _number(precipitation_stats.get("amount"))
                if isinstance(precipitation_stats, dict)
                else None
            )
            if precipitation_amount_mm is not None:
                daily_records.append(
                    {
                        "date": daily_record.get("period"),
                        "total_mm": precipitation_amount_mm,
                    }
                )
        if not daily_records:
            return {
                "available": False,
                "reason": "precipitation_semantics_unavailable",
                "coverage": envelope.get("coverage"),
            }
        return {
            "available": True,
            "source": "home_assistant_recorder",
            "total_mm": round(
                sum((daily_record["total_mm"] for daily_record in daily_records)), 3
            ),
            "semantics": "counter",
            "daily": daily_records,
            "rain_days": sum(
                (daily_record["total_mm"] >= 0.2 for daily_record in daily_records)
            ),
            "dry_spell_days": _longest_dry_spell(daily_records),
            "resets": 0,
            "coverage": envelope.get("coverage"),
            "data_since": envelope.get("data_from"),
            "data_to": envelope.get("data_to"),
        }


def _longest_dry_spell(daily: list[dict[str, Any]]) -> int:
    longest_dry_days = current_dry_days = 0
    for daily_record in sorted(daily, key=lambda row: str(row.get("date") or "")):
        if (_number(daily_record.get("total_mm")) or 0) < 0.2:
            current_dry_days += 1
            longest_dry_days = max(longest_dry_days, current_dry_days)
        else:
            current_dry_days = 0
    return longest_dry_days
