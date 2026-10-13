# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""SFML weather-station observations and Recorder history."""
from __future__ import annotations
import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import partial
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

SFML_DOMAIN = "solar_forecast_ml"
STATS_DOMAIN = "sfml_stats"
SENSOR_MAPPING_CONTRACT_VERSION = 1
SENSOR_MAPPING_REGISTRY = "sensor_mapping_providers"
WEATHER_SENSOR_SEMANTICS = {
    "environment.outdoor_temperature": "temperature_c",
    "weather.humidity": "humidity",
    "weather.pressure": "pressure",
    "weather.wind_speed": "wind_speed",
    "weather.precipitation": "precipitation",
    "weather.solar_radiation": "solar_radiation_wm2",
    "weather.illuminance": "lux",
}
_HISTORY_METRICS = {
    "temperature_c": "temperature",
    "humidity": "humidity",
    "pressure": "pressure",
    "wind_speed": "wind_speed",
    "precipitation": "precipitation",
    "solar_radiation_wm2": "solar_radiation",
    "lux": "lux",
}
_RECORDER_UNITS = {
    "temperature": "°C",
    "humidity": "%",
    "pressure": "hPa",
    "speed": "km/h",
    "precipitation": "mm",
    "irradiance": "W/m²",
    "illuminance": "lx",
}
_CURRENT_MAX_AGE = timedelta(hours=3)
_CURRENT_FUTURE_TOLERANCE = timedelta(minutes=5)
_RECORDER_HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
_MAX_DAILY_HISTORY_POINTS = 7305
_MAX_MONTHLY_HISTORY_POINTS = 240


class SFMLWeatherStationAdapter:
    """Read only the versioned SFML mapping and Home Assistant public APIs."""

    def __init__(self, hass: Any, entry_id: str | None = None) -> None:
        self._hass = hass
        self._entry_id = entry_id

    def dependency_status(self) -> dict[str, Any]:
        """Return customer-facing prerequisites without exposing entity IDs."""
        missing_domains = []
        if not _domain_loaded(self._hass, SFML_DOMAIN):
            missing_domains.append(SFML_DOMAIN)
        if not _domain_loaded(self._hass, STATS_DOMAIN):
            missing_domains.append(STATS_DOMAIN)
        station_status, sensor_mapping = self._mapping()
        if station_status != "available":
            missing_domains.append("local_weather_station")
        return {
            "ready": not missing_domains,
            "missing": missing_domains,
            "station_status": station_status,
            "station_metrics": sorted(sensor_mapping),
        }

    def entity_ids(self) -> tuple[str, ...]:
        """Return the validated local weather-station entities for subscriptions."""
        station_status, sensor_mapping = self._mapping()
        if station_status != "available":
            return ()
        return tuple(sorted(set(sensor_mapping.values())))

    def observation(self, now: datetime) -> dict[str, Any]:
        """Read fresh live values from the sensors configured in SFML."""
        station_status, sensor_mapping = self._mapping()
        if station_status != "available":
            return self._empty_observation(station_status)
        states = getattr(self._hass, "states", None)
        values = {}
        observed_by_metric = {}
        for metric_name, entity_id in sensor_mapping.items():
            sensor_state = states.get(entity_id) if states is not None else None
            numeric_value = _normalized_state(metric_name, sensor_state)
            observed_at = _state_timestamp(sensor_state, now)
            if (
                numeric_value is None
                or observed_at > now + _CURRENT_FUTURE_TOLERANCE
                or now - observed_at > _CURRENT_MAX_AGE
            ):
                continue
            values[metric_name] = numeric_value
            observed_by_metric[metric_name] = observed_at
        if not values:
            return self._empty_observation("station_values_unavailable")
        observed_at = max(observed_by_metric.values())
        return {
            "available": True,
            "status": "available",
            "source": "sfml_station",
            "observed_at": observed_at,
            "observed_at_by_metric": observed_by_metric,
            "values": values,
        }

    async def async_history(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Read complete daily and recent hourly long-term statistics."""
        station_status, sensor_mapping = self._mapping()
        if station_status != "available":
            return self.empty_history(station_status)
        try:
            from homeassistant.components.recorder.statistics import (
                statistics_during_period as statistics_during_period,
            )
            from homeassistant.helpers.recorder import (
                get_instance as get_recorder_instance,
            )
        except ImportError:
            return self.empty_history("recorder_unavailable")
        station_timezone = _local_timezone(self._hass)
        end_utc = _as_utc(end)
        statistic_ids = set(sensor_mapping.values())
        rain_entity_id = sensor_mapping.get("precipitation")
        non_rain_ids = statistic_ids - ({rain_entity_id} if rain_entity_id else set())
        daily_start = _local_day_start(_RECORDER_HISTORY_START, station_timezone)
        hourly_start = max(daily_start, end_utc - timedelta(hours=72))
        rain_semantics = self._precipitation_semantics(sensor_mapping)
        daily_query = partial(
            statistics_during_period,
            self._hass,
            daily_start,
            end_utc,
            non_rain_ids,
            "day",
            _RECORDER_UNITS,
            {"max", "min", "sum", "mean", "state", "change"},
        )
        hourly_query = partial(
            statistics_during_period,
            self._hass,
            hourly_start,
            end_utc,
            non_rain_ids,
            "hour",
            _RECORDER_UNITS,
            {"max", "min", "sum", "mean", "state", "change"},
        )
        try:
            recorder = get_recorder_instance(self._hass)
            queries = [daily_query, hourly_query]
            if rain_entity_id:
                queries.extend(
                    (
                        partial(
                            statistics_during_period,
                            self._hass,
                            daily_start,
                            end_utc,
                            {rain_entity_id},
                            "day",
                            None,
                            {"max", "min", "sum", "mean", "state", "change"},
                        ),
                        partial(
                            statistics_during_period,
                            self._hass,
                            hourly_start,
                            end_utc,
                            {rain_entity_id},
                            "hour",
                            None,
                            {"max", "min", "sum", "mean", "state", "change"},
                        ),
                    )
                )
            query_results = await asyncio.gather(
                *(recorder.async_add_executor_job(query) for query in queries)
            )
            daily_raw, hourly_raw = query_results[:2]
            if rain_entity_id:
                rain_semantics = _resolve_precipitation_semantics(
                    rain_semantics, rain_entity_id, query_results[2], query_results[3]
                )
                daily_raw.update(query_results[2])
                hourly_raw.update(query_results[3])
        except Exception:
            return self.empty_history("recorder_unavailable")
        all_daily_rows = _statistics_rows(
            daily_raw,
            sensor_mapping,
            "daily",
            precipitation_semantics=rain_semantics,
            local_timezone=station_timezone,
        )
        history_truncated = len(all_daily_rows) > _MAX_DAILY_HISTORY_POINTS
        daily_rows = all_daily_rows[-_MAX_DAILY_HISTORY_POINTS:]
        hourly_rows = _statistics_rows(
            hourly_raw,
            sensor_mapping,
            "hourly",
            precipitation_semantics=rain_semantics,
            local_timezone=station_timezone,
        )
        series = _series_rows(hourly_rows)
        rain_summary = _precipitation_summary(daily_rows, rain_semantics, end=end_utc)
        monthly_series = _monthly_rows(daily_rows)[-_MAX_MONTHLY_HISTORY_POINTS:]
        history_summary = _history_summary(
            daily_rows,
            daily_start,
            end_utc,
            configured_metrics=set(sensor_mapping),
            history_truncated=history_truncated,
            local_timezone=station_timezone,
        )
        history_available = bool(daily_rows or hourly_rows)
        return {
            "contract_version": 1,
            "available": history_available,
            "status": (
                "available" if history_available else "recorder_statistics_unavailable"
            ),
            "source": "home_assistant_recorder",
            "aggregates": {
                "status": "ok" if daily_rows else "unavailable",
                "source": "home_assistant_recorder",
                "data_from": (
                    daily_rows[0]["period"] if daily_rows else daily_start.isoformat()
                ),
                "data_to": (
                    daily_rows[-1]["period"] if daily_rows else end_utc.isoformat()
                ),
                "coverage": {
                    "observations": history_summary.get("observations", 0),
                    "expected_periods": history_summary.get("expected_periods", 0),
                    "ratio": (
                        history_summary.get("coverage_percent", 0) / 100
                        if history_summary.get("coverage_percent") is not None
                        else 0
                    ),
                },
                "data": daily_rows,
            },
            "summary": history_summary,
            "series": {
                "status": "ok" if series else "unavailable",
                "source": "home_assistant_recorder",
                "data": series,
            },
            "recent": {
                "status": "ok" if hourly_rows else "unavailable",
                "source": "home_assistant_recorder",
                "data": hourly_rows,
            },
            "record_events": _record_events(daily_rows),
            "monthly_series": monthly_series,
            "precipitation": rain_summary,
            "year_comparison": _year_comparison(
                daily_rows, rain_summary, end_utc, local_timezone=station_timezone
            ),
            "source_comparison": {},
            "capabilities": {
                metric_name: metric_name in sensor_mapping
                for metric_name in WEATHER_SENSOR_SEMANTICS.values()
            },
        }

    def _precipitation_semantics(self, mapping: dict[str, str]) -> str | None:
        entity_id = mapping.get("precipitation")
        states = getattr(self._hass, "states", None)
        sensor_state = (
            states.get(entity_id) if states is not None and entity_id else None
        )
        attributes = getattr(sensor_state, "attributes", {}) or {}
        normalized_unit = (
            str(attributes.get("unit_of_measurement") or "").strip().lower()
        )
        if normalized_unit in {"cm/h", "in/h", "mm/h"} or "/" in normalized_unit:
            return "rate"
        if normalized_unit in {"cm", "in", "mm"}:
            return "counter"

    def _mapping(self) -> tuple[str, dict[str, str]]:
        domain_data = getattr(self._hass, "data", {}).get(SFML_DOMAIN)
        provider_registry = (
            domain_data.get(SENSOR_MAPPING_REGISTRY)
            if isinstance(domain_data, dict)
            else None
        )
        if not isinstance(provider_registry, dict) or not provider_registry:
            return ("sfml_sensor_provider_unavailable", {})
        if self._entry_id is not None:
            provider_entry_id = self._entry_id
            provider = provider_registry.get(provider_entry_id)
            if provider is None:
                return ("sfml_sensor_provider_unavailable", {})
        else:
            if len(provider_registry) != 1:
                return ("sfml_sensor_provider_ambiguous", {})
            provider_entry_id, provider = next(iter(provider_registry.items()))
        if not _entry_loaded(self._hass, provider_entry_id, SFML_DOMAIN):
            return ("sfml_sensor_provider_unavailable", {})
        snapshot_method = getattr(provider, "snapshot", None)
        if not callable(snapshot_method):
            return ("sfml_sensor_contract_invalid", {})
        try:
            snapshot = snapshot_method()
        except Exception:
            return ("sfml_sensor_provider_error", {})
        if not isinstance(snapshot, Mapping) or (
            set(snapshot)
            != {"entry_id", "mappings", "provider_domain", "contract_version"}
            or snapshot.get("contract_version") != SENSOR_MAPPING_CONTRACT_VERSION
            or snapshot.get("provider_domain") != SFML_DOMAIN
            or (snapshot.get("entry_id") != provider_entry_id)
            or (not isinstance(snapshot.get("mappings"), Mapping))
            or (len(snapshot["mappings"]) > 64)
        ):
            return ("sfml_sensor_contract_invalid", {})
        sensor_mapping = {
            WEATHER_SENSOR_SEMANTICS[semantic_key]: entity_id
            for semantic_key, entity_id in snapshot["mappings"].items()
            if semantic_key in WEATHER_SENSOR_SEMANTICS
            if isinstance(entity_id, str)
            if _valid_entity_id(entity_id)
        }
        if "temperature_c" not in sensor_mapping or len(sensor_mapping) < 2:
            return ("local_weather_station_required", {})
        return ("available", sensor_mapping)

    @staticmethod
    def _empty_observation(status: str) -> dict[str, Any]:
        return {
            "available": False,
            "status": status,
            "source": "sfml_station",
            "observed_at": None,
            "observed_at_by_metric": {},
            "values": {},
        }

    @staticmethod
    def empty_history(status: str) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "available": False,
            "status": status,
            "source": "home_assistant_recorder",
            "aggregates": {},
            "summary": {},
            "series": {},
            "recent": {},
            "record_events": [],
            "monthly_series": [],
            "precipitation": {
                "available": False,
                "reason": "precipitation_semantics_unavailable",
            },
            "year_comparison": {},
            "source_comparison": {},
            "capabilities": {},
        }


def _domain_loaded(hass: Any, domain: str) -> bool:
    config_entries = getattr(hass, "config_entries", None)
    entries_method = getattr(config_entries, "async_entries", None)
    if callable(entries_method):
        try:
            return any(
                (
                    _state_value(getattr(entry, "state", None)) == "loaded"
                    for entry in entries_method(domain)
                )
            )
        except Exception:
            return False
    domain_data = getattr(hass, "data", {}).get(domain)
    return isinstance(domain_data, dict) and bool(domain_data)


def _entry_loaded(hass: Any, entry_id: Any, domain: str) -> bool:
    if not isinstance(entry_id, str) or not entry_id:
        return False
    config_entries = getattr(hass, "config_entries", None)
    entry_method = getattr(config_entries, "async_get_entry", None)
    if not callable(entry_method):
        return False
    try:
        entry = entry_method(entry_id)
    except Exception:
        return False
    return bool(
        entry is not None
        and getattr(entry, "domain", None) == domain
        and (_state_value(getattr(entry, "state", None)) == "loaded")
    )


def _state_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _valid_entity_id(value: str) -> bool:
    if len(value) > 255 or value.count(".") != 1:
        return False
    entity_domain, object_id = value.split(".", 1)
    return bool(
        entity_domain
        and object_id
        and entity_domain.replace("_", "a").isalnum()
        and object_id.replace("_", "a").isalnum()
    )


def _state_timestamp(state: Any, fallback: datetime) -> datetime:
    for timestamp_attribute in ("last_reported", "last_updated", "last_changed"):
        reported_at = getattr(state, timestamp_attribute, None)
        if not isinstance(reported_at, datetime):
            continue
        return (
            # Keep this line boundary for the verified shared return block.
            (
                (
                    reported_at.replace(tzinfo=timezone.utc)
                    if reported_at.tzinfo is None
                    else reported_at.astimezone(timezone.utc) if True else None
                )
                if True
                else None
            )
            if True
            else None
        )
    return fallback


def _normalized_state(metric: str, state: Any) -> float | None:
    attributes = getattr(state, "attributes", {}) or {}
    normalized_unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
    accepted_units = {
        "temperature_c": {"c", "f", "°c", "°f"},
        "humidity": {"%", "percent"},
        "pressure": {"pa", "bar", "hpa", "kpa", "inhg", "mbar", "in hg"},
        "wind_speed": {
            "kn",
            "kt",
            "kph",
            "mph",
            "mps",
            "knot",
            "knots",
            "m/s",
            "km/h",
            "mi/h",
        },
        "precipitation": {"cm", "in", "mm", "cm/h", "in/h", "mm/h"},
        "solar_radiation_wm2": {"w/m²", "kw/m²", "w/m2", "kw/m2"},
        "lux": {"lx", "klx"},
    }
    if normalized_unit not in accepted_units[metric]:
        return
    try:
        numeric_value = float(getattr(state, "state", ""))
    except (TypeError, ValueError):
        return
    if not isfinite(numeric_value):
        return
    if metric == "temperature_c" and normalized_unit in {"f", "°f"}:
        numeric_value = (numeric_value - 32) * 5 / 9
    elif metric == "pressure":
        if normalized_unit == "pa":
            numeric_value /= 100
        elif normalized_unit == "kpa":
            numeric_value *= 10
        elif normalized_unit == "bar":
            numeric_value *= 1000
        elif normalized_unit in {"inhg", "in hg"}:
            numeric_value *= 33.8639
    elif metric == "wind_speed":
        if normalized_unit in {"mps", "m/s"}:
            numeric_value *= 3.6
        elif normalized_unit in {"mph", "mi/h"}:
            numeric_value *= 1.609344
        elif normalized_unit in {"kn", "kt", "knot", "knots"}:
            numeric_value *= 1.852
    elif metric == "precipitation":
        if normalized_unit.startswith("cm"):
            numeric_value *= 10
        elif normalized_unit.startswith("in"):
            numeric_value *= 25.4
    elif metric == "solar_radiation_wm2" and normalized_unit in {"kw/m²", "kw/m2"}:
        numeric_value *= 1000
    elif metric == "lux" and normalized_unit == "klx":
        numeric_value *= 1000
    physical_ranges = {
        "temperature_c": (-100, 80),
        "humidity": (0, 100),
        "pressure": (800, 1200),
        "wind_speed": (0, 500),
        "precipitation": (0, 1000),
        "solar_radiation_wm2": (0, 2000),
        "lux": (0, 250000),
    }
    minimum_value, maximum_value = physical_ranges[metric]
    return (
        round(numeric_value, 3)
        if minimum_value <= numeric_value <= maximum_value
        else None
    )


def _statistics_rows(
    raw: Any,
    mapping: dict[str, str],
    granularity: str,
    *,
    precipitation_semantics: str | None = None,
    local_timezone: Any = timezone.utc,
) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    metric_by_entity = {
        entity_id: metric_name for metric_name, entity_id in mapping.items()
    }
    rows_by_period = {}
    for entity_id, metric_name in metric_by_entity.items():
        statistic_rows = raw.get(entity_id, [])
        for statistic_row in statistic_rows if isinstance(statistic_rows, list) else []:
            if isinstance(statistic_row, Mapping):
                start_timestamp = _recorder_timestamp(statistic_row.get("start"))
                if start_timestamp is not None:
                    local_timestamp = start_timestamp.astimezone(local_timezone)
                    period_key = (
                        local_timestamp.date().isoformat()
                        if granularity == "daily"
                        else local_timestamp.replace(
                            minute=0, second=0, microsecond=0
                        ).isoformat()
                    )
                    metric_values = _statistic_value(
                        metric_name,
                        statistic_row,
                        precipitation_semantics=precipitation_semantics,
                    )
                    if metric_values is not None:
                        period_row = rows_by_period.setdefault(
                            period_key,
                            {
                                "period": period_key,
                                "values": {},
                                "_observed_at": start_timestamp,
                            },
                        )
                        period_row["values"][
                            _HISTORY_METRICS[metric_name]
                        ] = metric_values
        continue
    result = sorted(rows_by_period.values(), key=lambda row: row["_observed_at"])
    for statistic_row in result:
        statistic_row.pop("_observed_at", None)
    return result


def _resolve_precipitation_semantics(
    configured_semantics: str | None, entity_id: str, daily_raw: Any, hourly_raw: Any
) -> str | None:
    """Resolve rain semantics from the configured sensor's Recorder columns."""
    if configured_semantics in {"rate", "counter"}:
        return configured_semantics
    statistic_rows = []
    for raw_statistics in (daily_raw, hourly_raw):
        if isinstance(raw_statistics, dict):
            entity_rows = raw_statistics.get(entity_id, [])
            if isinstance(entity_rows, list):
                statistic_rows.extend(entity_rows)
    if any(
        (
            isinstance(statistic_row, Mapping)
            and (amount_change := _finite_number(statistic_row.get("change")))
            is not None
            and (amount_change >= 0)
            for statistic_row in statistic_rows
        )
    ):
        return "counter"
    if any(
        (
            isinstance(statistic_row, Mapping)
            and any(
                (
                    _finite_number(statistic_row.get(statistic_column)) is not None
                    for statistic_column in ("mean", "min", "max")
                )
            )
            for statistic_row in statistic_rows
        )
    ):
        return "rate"
    return configured_semantics


def _statistic_value(
    metric: str, row: Mapping[str, Any], *, precipitation_semantics: str | None = None
) -> dict[str, Any] | None:
    if metric == "precipitation":
        if precipitation_semantics == "counter":
            rain_amount = _finite_number(row.get("change"))
            return (
                {"amount": rain_amount}
                if rain_amount is not None and rain_amount >= 0
                else None
            )
        if precipitation_semantics != "rate":
            return
    average_value = _finite_number(row.get("mean"))
    if average_value is None:
        average_value = _finite_number(row.get("state"))
    minimum_value = _finite_number(row.get("min"))
    maximum_value = _finite_number(row.get("max"))
    if average_value is None and minimum_value is None and (maximum_value is None):
        return
    return {
        "min": minimum_value if minimum_value is not None else average_value,
        "max": maximum_value if maximum_value is not None else average_value,
        "avg": average_value,
        "count": 1,
    }


def _series_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series = []
    for daily_row in rows:
        values = {}
        for metric_name, metric_values in daily_row["values"].items():
            numeric_value = metric_values.get("amount")
            if not isinstance(numeric_value, (int, float)):
                numeric_value = metric_values.get("avg")
            if isinstance(numeric_value, (int, float)):
                values[metric_name] = numeric_value
        if values:
            series.append({"observed_at": daily_row["period"], "values": values})
    return series


def _history_summary(
    rows: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    configured_metrics: set[str],
    history_truncated: bool,
    local_timezone: Any = timezone.utc,
) -> dict[str, Any]:
    first_day = (
        datetime.fromisoformat(rows[0]["period"]).date()
        if rows
        else start.astimezone(local_timezone).date()
    )
    last_day = (
        datetime.fromisoformat(rows[-1]["period"]).date()
        if rows
        else end.astimezone(local_timezone).date()
    )
    expected_days = max(1, (last_day - first_day).days + 1)
    recorded_metrics = {
        _HISTORY_METRICS[metric_name]
        for metric_name in configured_metrics
        if metric_name in _HISTORY_METRICS
    }
    observations_by_metric = {
        metric_name: sum(
            (metric_name in daily_row.get("values", {}) for daily_row in rows)
        )
        for metric_name in sorted(recorded_metrics)
    }
    expected_periods = expected_days * len(recorded_metrics)
    observations = sum(observations_by_metric.values())
    coverage_percent = (
        round(min(100.0, observations / expected_periods * 100), 2)
        if expected_periods
        else 0.0
    )
    summary = {
        "data_since": rows[0]["period"] if rows else start.isoformat(),
        "data_to": rows[-1]["period"] if rows else end.isoformat(),
        "valid_days": len(rows),
        "expected_days": expected_days,
        "observations": observations,
        "expected_periods": expected_periods,
        "coverage_percent": coverage_percent,
        "coverage_by_metric": {
            metric_name: round(min(100.0, sample_count / expected_days * 100), 2)
            for metric_name, sample_count in observations_by_metric.items()
        },
        "complete_recorder_history": not history_truncated,
        "history_truncated": history_truncated,
    }
    for metric_name, summary_keys in {
        "temperature": ("temperature_min_c", "temperature_max_c", "temperature_avg_c"),
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
        "solar_radiation": (
            "solar_radiation_min_wm2",
            "solar_radiation_max_wm2",
            "solar_radiation_avg_wm2",
        ),
        "lux": ("lux_min", "lux_max", "lux_avg"),
    }.items():
        metric_rows = [
            daily_row["values"][metric_name]
            for daily_row in rows
            if metric_name in daily_row.get("values", {})
        ]
        if not metric_rows:
            continue
        minimum_values = [
            daily_row["min"]
            for daily_row in metric_rows
            if daily_row.get("min") is not None
        ]
        maximum_values = [
            daily_row["max"]
            for daily_row in metric_rows
            if daily_row.get("max") is not None
        ]
        average_values = [
            daily_row["avg"]
            for daily_row in metric_rows
            if daily_row.get("avg") is not None
        ]
        summary[summary_keys[0]] = min(minimum_values) if minimum_values else None
        summary[summary_keys[1]] = max(maximum_values) if maximum_values else None
        summary[summary_keys[2]] = (
            round(sum(average_values) / len(average_values), 2)
            if average_values
            else None
        )
        continue
    return summary


def _precipitation_summary(
    rows: list[dict[str, Any]], semantics: str | None, *, end: datetime | None = None
) -> dict[str, Any]:
    daily_precipitation = []
    for daily_row in rows:
        metric_values = daily_row.get("values", {}).get("precipitation")
        if not isinstance(metric_values, dict):
            continue
        if semantics == "rate":
            average_value = metric_values.get("avg")
            maximum_value = metric_values.get("max")
            if isinstance(average_value, (int, float)) or isinstance(
                maximum_value, (int, float)
            ):
                daily_precipitation.append(
                    {
                        "date": daily_row["period"],
                        "avg_mm_per_h": average_value,
                        "max_mm_per_h": maximum_value,
                    }
                )
                continue
            continue
        rain_amount = metric_values.get("amount")
        if not isinstance(rain_amount, (int, float)):
            continue
        daily_precipitation.append(
            {"date": daily_row["period"], "total_mm": rain_amount}
        )
        continue
    if not daily_precipitation:
        return {"available": False, "reason": "precipitation_semantics_unavailable"}
    longest_dry_spell = dry_run = 0
    rain_readings = []
    for daily_row in daily_precipitation:
        rain_reading = (
            daily_row.get("max_mm_per_h")
            if semantics == "rate"
            else daily_row.get("total_mm")
        )
        if isinstance(rain_reading, (int, float)):
            rain_readings.append(rain_reading)
        if not isinstance(rain_reading, (int, float)) or rain_reading < 0.2:
            dry_run += 1
            longest_dry_spell = max(longest_dry_spell, dry_run)
        else:
            dry_run = 0
    summary = {
        "available": True,
        "source": "home_assistant_recorder",
        "semantics": semantics,
        "daily": daily_precipitation,
        "rain_days": sum((numeric_value >= 0.2 for numeric_value in rain_readings)),
        "dry_spell_days": longest_dry_spell,
        "resets": 0,
    }
    rain_totals = [
        daily_row["total_mm"]
        for daily_row in daily_precipitation
        if isinstance(daily_row.get("total_mm"), (int, float))
    ]
    if semantics == "counter":
        summary["total_mm"] = round(sum(rain_totals), 3)
        summary["unit"] = "mm"
    else:
        maximum_values = [
            daily_row["max_mm_per_h"]
            for daily_row in daily_precipitation
            if isinstance(daily_row.get("max_mm_per_h"), (int, float))
        ]
        summary["unit"] = "mm/h"
        summary["max_rate_mm_per_h"] = max(maximum_values) if maximum_values else None
    return summary if True else None


def _monthly_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_month = {}
    for statistic_row in rows:
        rows_by_month.setdefault(str(statistic_row.get("period", ""))[:7], []).append(
            statistic_row
        )
    monthly_rows = []
    for month_key in sorted(rows_by_month):
        monthly_values = {}
        month_metrics = {
            metric_name
            for statistic_row in rows_by_month[month_key]
            for metric_name in statistic_row.get("values", {})
        }
        for metric_name in sorted(month_metrics):
            metric_rows = [
                statistic_row["values"][metric_name]
                for statistic_row in rows_by_month[month_key]
                if isinstance(statistic_row.get("values", {}).get(metric_name), dict)
            ]
            amounts = [
                daily_row["amount"]
                for daily_row in metric_rows
                if isinstance(daily_row.get("amount"), (int, float))
            ]
            if amounts:
                monthly_values[metric_name] = {
                    "amount": round(sum(amounts), 3),
                    "count": len(amounts),
                }
                continue
            weighted_averages = [
                (daily_row["avg"], int(daily_row.get("count") or 0))
                for daily_row in metric_rows
                if isinstance(daily_row.get("avg"), (int, float))
            ]
            minimum_values = [
                daily_row["min"]
                for daily_row in metric_rows
                if isinstance(daily_row.get("min"), (int, float))
            ]
            maximum_values = [
                daily_row["max"]
                for daily_row in metric_rows
                if isinstance(daily_row.get("max"), (int, float))
            ]
            total_count = sum(
                (sample_count for average_value, sample_count in weighted_averages)
            )
            if not weighted_averages and (not minimum_values) and (not maximum_values):
                continue
            monthly_values[metric_name] = {
                "min": min(minimum_values) if minimum_values else None,
                "max": max(maximum_values) if maximum_values else None,
                "avg": (
                    round(
                        sum(
                            (
                                numeric_value * sample_count
                                for numeric_value, sample_count in weighted_averages
                            )
                        )
                        / total_count,
                        3,
                    )
                    if total_count
                    else None
                ),
                "count": total_count,
            }
            continue
        if not monthly_values:
            continue
        monthly_rows.append({"month": month_key, "values": monthly_values})
        continue
    return monthly_rows


def _record_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    record_definitions = (
        ("coldest_temperature", "temperature", "min", min, "°C"),
        ("hottest_temperature", "temperature", "max", max, "°C"),
        ("strongest_wind", "wind_speed", "max", max, "km/h"),
        ("lowest_pressure", "pressure", "min", min, "hPa"),
        ("highest_pressure", "pressure", "max", max, "hPa"),
    )
    record_events = []
    for (
        event_kind,
        metric_name,
        value_column,
        extreme_function,
        event_unit,
    ) in record_definitions:
        event_candidates = [
            (metric_values[value_column], statistic_row["period"])
            for statistic_row in rows
            if isinstance(
                (metric_values := statistic_row.get("values", {}).get(metric_name)),
                dict,
            )
            if isinstance(metric_values.get(value_column), (int, float))
            if isinstance(statistic_row.get("period"), str)
        ]
        if event_candidates:
            numeric_value, event_date = extreme_function(
                event_candidates, key=lambda item: item[0]
            )
            record_events.append(
                {
                    "type": event_kind,
                    "value": round(numeric_value, 2),
                    "unit": event_unit,
                    "date": event_date,
                    "source": "home_assistant_recorder",
                }
            )
    return record_events


def _recorded_hours(
    period: str, end: datetime | None, local_timezone: Any = timezone.utc
) -> float:
    try:
        local_day = datetime.fromisoformat(period).date()
    except ValueError:
        return 24.0
    day_start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_timezone)
    next_day_start = datetime.combine(
        local_day + timedelta(days=1), datetime.min.time(), tzinfo=local_timezone
    )
    day_hours = (
        next_day_start.astimezone(timezone.utc) - day_start.astimezone(timezone.utc)
    ).total_seconds() / 3600
    if end is None or local_day != end.astimezone(local_timezone).date():
        return day_hours
    return round(
        max(
            0.0,
            min(
                day_hours,
                (
                    end.astimezone(timezone.utc) - day_start.astimezone(timezone.utc)
                ).total_seconds()
                / 3600,
            ),
        ),
        6,
    )


def _year_comparison(
    rows: list[dict[str, Any]],
    precipitation: dict[str, Any],
    end: datetime,
    *,
    local_timezone: Any = timezone.utc,
) -> dict[str, Any]:
    local_end = end.astimezone(local_timezone)
    current_year = local_end.year
    precipitation_semantics = precipitation.get("semantics")
    precipitation_by_date = {
        daily_row["date"]: (
            daily_row.get("max_mm_per_h")
            if precipitation_semantics == "rate"
            else daily_row.get("total_mm")
        )
        for daily_row in precipitation.get("daily", [])
        if isinstance(daily_row, dict)
        if isinstance(daily_row.get("date"), str)
        if isinstance(
            (
                daily_row.get("max_mm_per_h")
                if precipitation_semantics == "rate"
                else daily_row.get("total_mm")
            ),
            (int, float),
        )
    }

    def year_summary(year: int) -> dict[str, Any] | None:
        year_rows = [
            statistic_row
            for statistic_row in rows
            if str(statistic_row.get("period", "")).startswith(f"{year:04d}-")
        ]
        if not year_rows:
            return ((None if True else None) if True else None) if True else None
        temperature_averages = [
            metric_values["avg"]
            for statistic_row in year_rows
            if isinstance(
                (metric_values := statistic_row.get("values", {}).get("temperature")),
                dict,
            )
            if isinstance(metric_values.get("avg"), (int, float))
        ]
        year_rain_values = [
            rain_amount
            for statistic_row in year_rows
            if isinstance(
                (rain_amount := precipitation_by_date.get(statistic_row.get("period"))),
                (int, float),
            )
        ]
        rain_totals = year_rain_values if precipitation_semantics == "counter" else []
        return (
            (
                (
                    {
                        "year": year,
                        "valid_days": len(year_rows),
                        "temperature_avg_c": (
                            round(
                                sum(temperature_averages) / len(temperature_averages), 2
                            )
                            if temperature_averages
                            else None
                        ),
                        "precipitation_total_mm": (
                            round(sum(rain_totals), 3) if rain_totals else None
                        ),
                        "rain_days": (
                            sum(
                                (rain_amount >= 0.2 for rain_amount in year_rain_values)
                            )
                            if year_rain_values
                            else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    try:
        same_day_last_year = local_end.date().replace(year=current_year - 1)
    except ValueError:
        same_day_last_year = local_end.date().replace(year=current_year - 1, day=28)
    previous_day_row = next(
        (
            statistic_row
            for statistic_row in rows
            if statistic_row.get("period") == same_day_last_year.isoformat()
        ),
        None,
    )
    previous_day_temperature = (
        previous_day_row.get("values", {}).get("temperature", {}).get("avg")
        if isinstance(previous_day_row, dict)
        else None
    )
    current_year_summary = year_summary(current_year)
    previous_year_summary = year_summary(current_year - 1)
    return (
        (
            (
                {
                    "available": bool(current_year_summary or previous_year_summary),
                    "current": current_year_summary,
                    "previous": previous_year_summary,
                    "same_day_last_year": (
                        {
                            "date": same_day_last_year.isoformat(),
                            "temperature_avg_c": previous_day_temperature,
                        }
                        if isinstance(previous_day_temperature, (int, float))
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


def _recorder_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
    if isinstance(value, (int, float)) and (not isinstance(value, bool)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return
    if isinstance(value, str):
        try:
            parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
        return (
            parsed_timestamp.replace(tzinfo=timezone.utc)
            if parsed_timestamp.tzinfo is None
            else parsed_timestamp.astimezone(timezone.utc)
        )


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _local_timezone(hass: Any) -> ZoneInfo | timezone:
    timezone_name = getattr(getattr(hass, "config", None), "time_zone", None)
    try:
        return ZoneInfo(timezone_name) if timezone_name else timezone.utc
    except (TypeError, ValueError):
        return timezone.utc


def _local_day_start(value: datetime, local_timezone: Any) -> datetime:
    local_day = _as_utc(value).astimezone(local_timezone).date()
    return datetime.combine(
        local_day, datetime.min.time(), tzinfo=local_timezone
    ).astimezone(timezone.utc)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None
