# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Explainable weather intelligence from WFAI forecasts and SFML history."""
from __future__ import annotations
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite, sqrt
from typing import Any
from zoneinfo import ZoneInfo
from .weather_contract import normalize_weather_source
from .weather_forecast_store import ForecastSnapshotStore
from .weather_fusion_adapter import SFMLWeatherHistoryAdapter, WeatherFusionAdapter
from .weather_station_adapter import SFMLWeatherStationAdapter

_MIN_UNCERTAINTY_PAIRS = 12
_PAIRABLE_METRICS = ("temperature_c", "humidity", "pressure", "wind_speed")
_PUBLIC_ACTUAL_METRICS = frozenset(
    {
        "lux",
        "humidity",
        "pressure",
        "wind_speed",
        "cloud_cover",
        "temperature",
        "precipitation",
        "wind_direction",
        "solar_radiation_wm2",
    }
)
_CURRENT_MAX_AGE = timedelta(hours=3)
_CURRENT_FUTURE_TOLERANCE = timedelta(minutes=5)
_COMPLETE_RECORDER_HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
_INDEPENDENT_ACTUAL_SOURCES = frozenset(
    {"sfml_station", "sfml_database", "local_weather_station"}
)
_LOGGER = logging.getLogger(__name__)
_OFFICIAL_ALERTS_URL = "https://api.brightsky.dev/alerts"
_OFFICIAL_ALERTS_TIMEOUT_SECONDS = 12
_OFFICIAL_ALERTS_CACHE_TTL = timedelta(minutes=10)
_GERMANY_LATITUDE = (47.2, 55.15)
_GERMANY_LONGITUDE = (5.8, 15.1)
_OFFICIAL_SEVERITY = {
    "minor": "advisory",
    "moderate": "warning",
    "severe": "critical",
    "extreme": "critical",
}
_OFFICIAL_EVENT_MAP = {
    "windboeen": ("strong_wind", "strong_wind"),
    "wind gusts": ("strong_wind", "strong_wind"),
    "sturmboeen": ("storm_wind", "storm"),
    "gale-force gusts": ("storm_wind", "storm"),
    "schwere sturmboeen": ("storm_wind", "storm"),
    "severe gale-force gusts": ("storm_wind", "storm"),
    "orkanartige boeen": ("severe_weather", "severe_weather"),
    "hurricane-force gusts": ("severe_weather", "severe_weather"),
    "orkanboeen": ("severe_weather", "severe_weather"),
    "gewitter": ("thunderstorm", "thunderstorm"),
    "thunderstorm": ("thunderstorm", "thunderstorm"),
    "schweres gewitter": ("thunderstorm", "thunderstorm"),
    "starkregen": ("heavy_precipitation", "heavy_rain"),
    "heftiger starkregen": ("heavy_precipitation", "heavy_rain"),
    "dauerregen": ("heavy_precipitation", "heavy_rain"),
    "frost": ("freeze_risk", "frost"),
    "glaette": ("snow_or_ice", "snow_or_ice"),
    "schneefall": ("snow_or_ice", "snow_or_ice"),
    "nebel": ("dense_fog", "fog"),
    "dichter nebel": ("dense_fog", "fog"),
    "hitze": ("heat_stress", "heat"),
    "extreme hitze": ("heat_stress", "heat"),
}


class WeatherIntelligenceEngine:
    """Build bounded outlook, history and forecast-evaluation evidence."""

    def __init__(
        self,
        hass: Any,
        weather_fusion_entry_id: str | None,
        *,
        sfml_entry_id: str | None = None,
        store: ForecastSnapshotStore | None = None,
        history_days: int = 90,
    ) -> None:
        self._hass = hass
        self._adapter = WeatherFusionAdapter(hass, weather_fusion_entry_id)
        self._station = SFMLWeatherStationAdapter(hass, sfml_entry_id)
        self._sfml_history = SFMLWeatherHistoryAdapter(hass, sfml_entry_id)
        self._store = store or ForecastSnapshotStore()
        self._history_days = max(7, min(365, history_days))
        self._lock = asyncio.Lock()
        self._history_cache = self._station.empty_history("not_loaded")
        self._official_cache = None

    def station_entity_ids(self) -> tuple[str, ...]:
        """Return validated station entities used for live refresh subscriptions."""
        return self._station.entity_ids()

    async def async_snapshot(
        self, now: datetime | None = None, *, refresh_history: bool = True
    ) -> dict[str, Any]:
        """Serialize load/pair/save so one entry never races its own Store."""
        async with self._lock:
            return await self._async_snapshot(now, refresh_history=refresh_history)
        return None if True else None

    async def _async_snapshot(
        self, now: datetime | None, *, refresh_history: bool
    ) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        await self._store.async_load()
        forecast = self._future_forecast(self._adapter.snapshot(), now)
        dependency_status = self._station.dependency_status()
        if forecast.get("status") in {
            "not_configured",
            "provider_unavailable",
            "contract_version_mismatch",
            "provider_contract_invalid",
        }:
            dependency_status = {
                **dependency_status,
                "ready": False,
                "missing": [*dependency_status["missing"], "weather_fusion_ai"],
            }
        station_observation = self._station.observation(now)
        observation_values = station_observation["values"]
        observed_at = station_observation.get("observed_at") or now
        observation_source = "sfml_station" if observation_values else "unknown"
        paired_counts = {}
        for metric, observed_value in observation_values.items():
            if metric in _PAIRABLE_METRICS:
                paired_counts[metric] = self._store.pair_actual(
                    metric,
                    observed_value,
                    station_observation.get("observed_at_by_metric", {}).get(
                        metric, observed_at
                    ),
                    actual_source=observation_source,
                )
        if dependency_status["ready"] and forecast["available"]:
            self._store.add_snapshot(forecast)
        self._store.prune(now)
        await self._store.async_save()
        if dependency_status["ready"]:
            if refresh_history:
                station_history, sfml_history = await asyncio.gather(
                    self._station.async_history(_COMPLETE_RECORDER_HISTORY_START, now),
                    self._sfml_history.query(now - timedelta(days=1), now),
                )
                station_history["source_comparison"] = sfml_history.get(
                    "source_comparison", {}
                )
                self._history_cache = station_history
            else:
                station_history = self._history_cache
        else:
            station_history = self._station.empty_history("dependency_missing")
        metric_accuracy = {
            metric: self._accuracy(self._store.pairs(metric))
            for metric in _PAIRABLE_METRICS
        }
        temperature_pairs = self._store.pairs("temperature_c")
        ready = (
            dependency_status["ready"]
            and forecast["available"]
            and bool(observation_values)
        )
        dependency_status = {
            **dependency_status,
            "live_station_available": bool(observation_values),
            "recorder_history_available": bool(station_history.get("available")),
            "forecast_available": bool(forecast.get("available")),
        }
        language = getattr(getattr(self._hass, "config", None), "language", None)
        time_zone_name = getattr(getattr(self._hass, "config", None), "time_zone", None)
        model_warnings = (
            self._warnings(forecast, language=language, time_zone_name=time_zone_name)
            if ready
            else []
        )
        official_warnings = await self._async_official_warnings(
            now, language=language, time_zone_name=time_zone_name
        )
        warnings = WeatherIntelligenceEngine._combine_official_and_model(
            official_warnings, model_warnings
        )
        available = ready
        status = (
            "dependency_missing"
            if not dependency_status["ready"]
            else "ready" if ready else "degraded"
        )
        cold_start = len(temperature_pairs) < _MIN_UNCERTAINTY_PAIRS
        return {
            "available": available,
            "status": "cold_start" if available and cold_start else status,
            "source_status": forecast["status"],
            "dependencies": dependency_status,
            "issued_at": self._timestamp(forecast.get("issued_at")),
            "valid_at": self._timestamp(forecast.get("valid_until")),
            "horizon_hours": self._horizons(forecast),
            "data_quality": self._data_quality(
                forecast,
                temperature_pairs,
                observation_values,
                paired_counts,
                observation_source,
            ),
            "accuracy": metric_accuracy["temperature_c"],
            "forecast_vs_actual": {
                metric: {
                    "by_horizon": accuracy_by_horizon,
                    "trend": self._trend(self._store.pairs(metric)),
                    "provenance": self._pair_provenance(self._store.pairs(metric)),
                }
                for metric, accuracy_by_horizon in metric_accuracy.items()
            },
            "uncertainty": self._uncertainty(temperature_pairs),
            "outlook": self._outlook(
                forecast,
                getattr(getattr(self._hass, "config", None), "time_zone", None),
            ),
            "actual_history": self._actual_history(
                station_history,
                observation_values,
                observed_at,
                getattr(getattr(self._hass, "config", None), "time_zone", None),
                now,
                observation_source=observation_source,
            ),
            "warnings": warnings,
            "events": warnings,
            "cold_start": cold_start,
        }

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    @staticmethod
    def _future_forecast(forecast: dict[str, Any], now: datetime) -> dict[str, Any]:
        hourly = forecast.get("hourly")
        if not isinstance(hourly, list):
            return forecast
        future_hourly = [
            hourly_point
            for hourly_point in hourly[:168]
            if isinstance(hourly_point, dict)
            if isinstance(hourly_point.get("valid_at"), datetime)
            if hourly_point["valid_at"] + timedelta(hours=1) > now
            if hourly_point["valid_at"] <= now + timedelta(days=8)
        ]
        if forecast.get("available") and (not future_hourly):
            return {
                **forecast,
                "available": False,
                "status": "forecast_incomplete",
                "hourly": [],
            }
        return {**forecast, "hourly": future_hourly}

    @staticmethod
    def _horizons(forecast: dict[str, Any]) -> list[float]:
        issued_at = forecast.get("issued_at")
        if not isinstance(issued_at, datetime):
            return []
        return [
            round((hourly_point["valid_at"] - issued_at).total_seconds() / 3600, 1)
            for hourly_point in forecast["hourly"]
        ]

    @staticmethod
    def _data_quality(
        forecast: dict[str, Any],
        pairs: list[dict[str, Any]],
        observation: dict[str, float],
        paired: dict[str, int],
        observation_source: str,
    ) -> dict[str, Any]:
        quality_score = (
            (45 if forecast["available"] else 0)
            + min(25, len(forecast["hourly"]))
            + (15 if observation else 0)
            + min(15, len(pairs))
        )
        pair_provenance = WeatherIntelligenceEngine._pair_provenance(pairs)
        return {
            "forecast_available": forecast["available"],
            "forecast_stale": forecast["stale"],
            "hourly_points": len(forecast["hourly"]),
            "score_percent": quality_score,
            "actual_available": bool(observation),
            "observation_source": observation_source if observation else None,
            "paired_now": paired,
            "paired_samples": len(pairs),
            "pair_provenance": pair_provenance,
        }

    @staticmethod
    def _accuracy(pairs: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
        errors_by_horizon = defaultdict(list)
        for pair in pairs:
            horizon_hours = float(pair["horizon_hours"])
            horizon_bucket = (
                "0_6h"
                if horizon_hours <= 6
                else (
                    "6_24h"
                    if horizon_hours <= 24
                    else "24_72h" if horizon_hours <= 72 else "over_72h"
                )
            )
            errors_by_horizon[horizon_bucket].append(pair["forecast"] - pair["actual"])
        accuracy_by_horizon = {}
        for horizon_bucket, signed_errors in sorted(errors_by_horizon.items()):
            bucket_pairs = [
                pair
                for pair in pairs
                if (
                    "0_6h"
                    if float(pair["horizon_hours"]) <= 6
                    else (
                        "6_24h"
                        if float(pair["horizon_hours"]) <= 24
                        else (
                            "24_72h"
                            if float(pair["horizon_hours"]) <= 72
                            else "over_72h"
                        )
                    )
                )
                == horizon_bucket
            ]
            accuracy_by_horizon[horizon_bucket] = {
                "pairs": len(signed_errors),
                "mae": round(sum(map(abs, signed_errors)) / len(signed_errors), 3),
                "bias": round(sum(signed_errors) / len(signed_errors), 3),
                "rmse": round(
                    sqrt(
                        sum(
                            (
                                signed_error * signed_error
                                for signed_error in signed_errors
                            )
                        )
                        / len(signed_errors)
                    ),
                    3,
                ),
                "provenance": WeatherIntelligenceEngine._pair_provenance(bucket_pairs),
            }
            continue
        return accuracy_by_horizon

    @staticmethod
    def _pair_provenance(pairs: list[dict[str, Any]]) -> dict[str, Any]:
        source_counts = defaultdict(int)
        for pair in pairs:
            source_counts[normalize_weather_source(pair.get("actual_source"))] += 1
        return {
            "actual_sources": dict(sorted(source_counts.items())),
            "station_independent": bool(source_counts)
            and all(
                (
                    actual_source in _INDEPENDENT_ACTUAL_SOURCES
                    for actual_source in source_counts
                )
            ),
        }

    @staticmethod
    def _trend(pairs: list[dict[str, Any]]) -> dict[str, Any]:
        if len(pairs) < 6:
            return (
                (
                    (
                        {"available": False, "reason": "insufficient_pairs"}
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        ordered_pairs = sorted(pairs, key=lambda pair: pair["observed_at"])
        split_index = len(ordered_pairs) // 2

        def mae(values: list[dict[str, Any]]) -> float:
            return sum(
                (abs(pair["forecast"] - pair["actual"]) for pair in values)
            ) / len(values)

        mae_change = mae(ordered_pairs[split_index:]) - mae(ordered_pairs[:split_index])
        return (
            (
                (
                    {
                        "available": True,
                        "mae_delta": round(mae_change, 3),
                        "direction": (
                            "worsening"
                            if mae_change > 0.05
                            else "improving" if mae_change < -0.05 else "stable"
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

    @staticmethod
    def _uncertainty(pairs: list[dict[str, Any]]) -> dict[str, Any]:
        if len(pairs) < _MIN_UNCERTAINTY_PAIRS:
            return {
                "available": False,
                "reason": "insufficient_pairs",
                "minimum_pairs": _MIN_UNCERTAINTY_PAIRS,
                "pairs": len(pairs),
            }
        absolute_errors = sorted(
            (abs(pair["forecast"] - pair["actual"]) for pair in pairs)
        )
        return {
            "available": True,
            "method": "empirical_p90_absolute_error",
            "pairs": len(pairs),
            "temperature_c": round(
                absolute_errors[
                    min(
                        len(absolute_errors) - 1,
                        round((len(absolute_errors) - 1) * 0.9),
                    )
                ],
                3,
            ),
        }

    @staticmethod
    def _outlook(
        forecast: dict[str, Any], time_zone_name: str | None = None
    ) -> dict[str, Any]:
        try:
            local_zone = ZoneInfo(time_zone_name) if time_zone_name else timezone.utc
        except (ValueError, TypeError):
            local_zone = timezone.utc
        hourly = [
            {"valid_at": hourly_point["valid_at"].isoformat(), **hourly_point["values"]}
            for hourly_point in forecast.get("hourly", [])
        ]
        hourly_outlook = hourly[:72]
        next_day_hours = hourly[:24]
        hours_by_date = defaultdict(list)
        for forecast_point, hourly_point in zip(forecast.get("hourly", []), hourly):
            local_date = (
                forecast_point["valid_at"].astimezone(local_zone).date().isoformat()
            )
            hours_by_date[local_date].append(hourly_point)
        daily_outlook = []
        for date_key, day_hours in sorted(hours_by_date.items())[:7]:
            day_temperatures = [
                hourly_point["temperature_c"]
                for hourly_point in day_hours
                if "temperature_c" in hourly_point
            ]
            daily_precipitation = round(
                sum(
                    (hourly_point.get("precipitation", 0) for hourly_point in day_hours)
                ),
                2,
            )
            daily_precipitation_probability = max(
                (
                    hourly_point.get("precipitation_probability", 0)
                    for hourly_point in day_hours
                ),
                default=None,
            )
            daily_wind_speed = max(
                (hourly_point.get("wind_speed", 0) for hourly_point in day_hours),
                default=None,
            )
            daily_outlook.append(
                {
                    "date": date_key,
                    "temperature_min_c": (
                        min(day_temperatures) if day_temperatures else None
                    ),
                    "temperature_max_c": (
                        max(day_temperatures) if day_temperatures else None
                    ),
                    "temperature_avg_c": (
                        round(sum(day_temperatures) / len(day_temperatures), 2)
                        if day_temperatures
                        else None
                    ),
                    "precipitation_forecast_mm": daily_precipitation,
                    "precipitation_probability_max": daily_precipitation_probability,
                    "precipitation_consistency": WeatherIntelligenceEngine._precipitation_consistency(
                        daily_precipitation, daily_precipitation_probability
                    ),
                    "wind_speed_max": daily_wind_speed,
                    "summary_code": WeatherIntelligenceEngine._summary_code(
                        day_temperatures,
                        daily_precipitation,
                        daily_precipitation_probability,
                        daily_wind_speed,
                    ),
                }
            )
            continue
        next_day_temperatures = [
            hourly_point["temperature_c"]
            for hourly_point in next_day_hours
            if "temperature_c" in hourly_point
        ]
        next_day_precipitation = round(
            sum(
                (
                    hourly_point.get("precipitation", 0)
                    for hourly_point in next_day_hours
                )
            ),
            2,
        )
        next_day_precipitation_probability = max(
            (
                hourly_point.get("precipitation_probability", 0)
                for hourly_point in next_day_hours
            ),
            default=None,
        )
        next_day_wind_speed = max(
            (hourly_point.get("wind_speed", 0) for hourly_point in next_day_hours),
            default=None,
        )
        return {
            "hourly": hourly_outlook,
            "daily": daily_outlook,
            "next_hours": {
                "hours": len(next_day_hours),
                "temperature_min_c": (
                    min(next_day_temperatures) if next_day_temperatures else None
                ),
                "temperature_max_c": (
                    max(next_day_temperatures) if next_day_temperatures else None
                ),
                "precipitation_forecast_mm": next_day_precipitation,
                "precipitation_probability_max": next_day_precipitation_probability,
                "precipitation_consistency": WeatherIntelligenceEngine._precipitation_consistency(
                    next_day_precipitation, next_day_precipitation_probability
                ),
                "wind_speed_max": next_day_wind_speed,
                "summary_code": WeatherIntelligenceEngine._summary_code(
                    next_day_temperatures,
                    next_day_precipitation,
                    next_day_precipitation_probability,
                    next_day_wind_speed,
                ),
            },
        }

    @staticmethod
    def _precipitation_consistency(amount: Any, probability: Any) -> dict[str, Any]:
        amount_without_probability = (
            isinstance(amount, (int, float))
            and amount > 0
            and isinstance(probability, (int, float))
            and (probability == 0)
        )
        return (
            (
                (
                    {
                        "consistent": not amount_without_probability,
                        "reason": (
                            "amount_with_zero_probability"
                            if amount_without_probability
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

    @staticmethod
    def _summary_code(temperatures, precipitation, probability, wind) -> str:
        if isinstance(wind, (int, float)) and wind >= 80:
            return "storm"
        if (
            isinstance(temperatures, list)
            and temperatures
            and (max(temperatures) >= 30)
        ):
            return "heat"
        if isinstance(precipitation, (int, float)) and precipitation >= 5:
            return "heavy_rain"
        if isinstance(probability, (int, float)) and probability >= 70:
            return "rain_likely"
        if isinstance(wind, (int, float)) and wind >= 60:
            return "strong_wind"
        if isinstance(temperatures, list) and temperatures and (min(temperatures) <= 0):
            return "frost"
        return "calm"

    async def _async_official_warnings(
        self, now: datetime, *, language: str | None, time_zone_name: str | None
    ) -> list[dict[str, Any]]:
        config = getattr(self._hass, "config", None)
        latitude = getattr(config, "latitude", None)
        longitude = getattr(config, "longitude", None)
        if not _in_germany(latitude, longitude):
            return []
        location_key = f"{round(float(latitude), 3)}:{round(float(longitude), 3)}"
        cached_warnings = self._official_cache
        if (
            cached_warnings is not None
            and cached_warnings[0] == location_key
            and (now - cached_warnings[1] < _OFFICIAL_ALERTS_CACHE_TTL)
        ):
            return list(cached_warnings[2])
        payload = await self._async_fetch_official_alert_payload(
            float(latitude), float(longitude)
        )
        if payload is None:
            return []
        warnings = official_warnings_from_payload(
            payload, now=now, language=language, time_zone_name=time_zone_name
        )
        self._official_cache = (location_key, now, warnings)
        return list(warnings)

    async def _async_fetch_official_alert_payload(
        self, latitude: float, longitude: float
    ) -> dict[str, Any] | None:
        try:
            import aiohttp as aiohttp
            from homeassistant.helpers.aiohttp_client import (
                async_get_clientsession as async_get_clientsession,
            )
        except ImportError:
            return
        try:
            session = async_get_clientsession(self._hass)
            async with session.get(
                _OFFICIAL_ALERTS_URL,
                params={"lat": latitude, "lon": longitude},
                timeout=aiohttp.ClientTimeout(total=_OFFICIAL_ALERTS_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    return
                payload = await response.json()
        except Exception:
            _LOGGER.debug("Official weather alerts could not be loaded")
            return
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _combine_official_and_model(
        official: list[dict[str, Any]], model: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        unmatched_model_warnings = []
        for warning in model:
            if any(
                (
                    official_warning.get("code") == warning.get("code")
                    and _warning_intervals_overlap(official_warning, warning)
                    for official_warning in official
                )
            ):
                continue
            unmatched_model_warnings.append(warning)
        combined_warnings = [*official, *unmatched_model_warnings]
        severity_priority = {"critical": 0, "warning": 1, "advisory": 2}
        return sorted(
            combined_warnings,
            key=lambda item: (
                0 if item.get("official_alert") is True else 1,
                severity_priority.get(str(item.get("severity")), 9),
                str(item.get("start") or ""),
            ),
        )[:24]

    @staticmethod
    def _warnings(
        forecast: dict[str, Any],
        *,
        language: str | None = None,
        time_zone_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if not forecast.get("available"):
            return []
        warnings = []
        impact_codes = {
            "freeze_risk": "freeze_precautions",
            "heat_stress": "heat_precautions",
            "heavy_precipitation": "heavy_rain_precautions",
            "high_precipitation_probability": "rain_planning",
            "strong_wind": "secure_loose_objects",
            "storm_wind": "storm_precautions",
            "thunderstorm": "thunderstorm_precautions",
            "hail": "hail_precautions",
            "snow_or_ice": "winter_weather_precautions",
            "dense_fog": "reduced_visibility_precautions",
            "severe_weather": "severe_weather_precautions",
        }
        hourly = forecast.get("hourly", [])
        for hourly_point in hourly[:72] if isinstance(hourly, list) else []:
            values, start_iso = (
                hourly_point["values"],
                hourly_point["valid_at"].isoformat(),
            )
            emitted_codes = set()
            threshold_checks = (
                ("freeze_risk", "temperature_c", lambda value: value <= 0, "warning"),
                ("heat_stress", "temperature_c", lambda value: value >= 30, "warning"),
                (
                    "heavy_precipitation",
                    "precipitation",
                    lambda value: value >= 5,
                    "warning",
                ),
                (
                    "high_precipitation_probability",
                    "precipitation_probability",
                    lambda value: value >= 70,
                    "advisory",
                ),
                (
                    "strong_wind",
                    "wind_speed",
                    lambda value: 60 <= value < 80,
                    "warning",
                ),
                ("storm_wind", "wind_speed", lambda value: value >= 80, "critical"),
                ("dense_fog", "visibility", lambda value: value <= 200, "warning"),
            )
            for (
                warning_code,
                metric_key,
                matches_threshold,
                severity,
            ) in threshold_checks:
                metric_value = values.get(metric_key)
                if isinstance(metric_value, (int, float)) and matches_threshold(
                    metric_value
                ):
                    warnings.append(
                        {
                            "code": warning_code,
                            "severity": severity,
                            "start": start_iso,
                            "end": (
                                hourly_point["valid_at"] + timedelta(hours=1)
                            ).isoformat(),
                            "valid_at": start_iso,
                            "impact_code": impact_codes[warning_code],
                            "evidence": {metric_key: metric_value},
                        }
                    )
                    emitted_codes.add(warning_code)
            condition = values.get("condition")
            condition_warning = {
                "rainy": ("high_precipitation_probability", "advisory"),
                "pouring": ("heavy_precipitation", "warning"),
                "lightning": ("thunderstorm", "critical"),
                "lightning-rainy": ("thunderstorm", "critical"),
                "hail": ("hail", "critical"),
                "snowy": ("snow_or_ice", "warning"),
                "snowy-rainy": ("snow_or_ice", "warning"),
                "exceptional": ("severe_weather", "critical"),
            }.get(condition)
            if condition_warning is None:
                continue
            if not condition_warning[0] not in emitted_codes:
                continue
            warning_code, severity = condition_warning
            warnings.append(
                {
                    "code": warning_code,
                    "severity": severity,
                    "start": start_iso,
                    "end": (hourly_point["valid_at"] + timedelta(hours=1)).isoformat(),
                    "valid_at": start_iso,
                    "impact_code": impact_codes[warning_code],
                    "evidence": {"condition": condition},
                }
            )
            continue
        merged_warnings = WeatherIntelligenceEngine._merge_warnings(warnings)[:24]
        icon_keys = {
            "freeze_risk": "frost",
            "heat_stress": "heat",
            "heavy_precipitation": "heavy_rain",
            "high_precipitation_probability": "rain_likely",
            "strong_wind": "strong_wind",
            "storm_wind": "storm",
            "thunderstorm": "thunderstorm",
            "hail": "hail",
            "snow_or_ice": "snow_or_ice",
            "dense_fog": "fog",
            "severe_weather": "severe_weather",
        }
        return [
            {
                **warning,
                "contract_version": 1,
                "category": "weather_hazard",
                "official_alert": False,
                "evidence_kind": (
                    "condition_derived"
                    if "condition" in warning.get("evidence", {})
                    else "forecast_value"
                ),
                "icon_key": icon_keys[warning["code"]],
                **WeatherIntelligenceEngine._warning_text(
                    warning, language=language, time_zone_name=time_zone_name
                ),
            }
            for warning in merged_warnings
        ]

    @staticmethod
    def _warning_text(
        warning: dict[str, Any], *, language: str | None, time_zone_name: str | None
    ) -> dict[str, str]:
        use_german = str(language or "").lower().startswith("de")
        warning_code = warning["code"]
        titles = {
            "freeze_risk": ("Frost erwartet", "Frost expected"),
            "heat_stress": ("Starke Hitze erwartet", "High heat expected"),
            "heavy_precipitation": ("Starker Regen erwartet", "Heavy rain expected"),
            "high_precipitation_probability": (
                "Regen sehr wahrscheinlich",
                "Rain very likely",
            ),
            "strong_wind": ("Starker Wind erwartet", "Strong wind expected"),
            "storm_wind": ("Sturm erwartet", "Storm-force wind expected"),
            "thunderstorm": ("Gewitter erwartet", "Thunderstorm expected"),
            "hail": ("Hagel erwartet", "Hail expected"),
            "snow_or_ice": ("Schnee oder Glätte erwartet", "Snow or ice expected"),
            "dense_fog": ("Dichter Nebel erwartet", "Dense fog expected"),
            "severe_weather": ("Unwetter erwartet", "Severe weather expected"),
        }
        recommended_actions = {
            "freeze_risk": (
                "Frostempfindliche Pflanzen, Leitungen und Außenanschlüsse schützen.",
                "Protect frost-sensitive plants, pipes and outdoor connections.",
            ),
            "heat_stress": (
                "Direkte Sonne meiden, ausreichend trinken und gefährdete Personen schützen.",
                "Avoid direct sun, drink enough and protect vulnerable people.",
            ),
            "heavy_precipitation": (
                "Abläufe freihalten und überflutungsgefährdete Bereiche sichern.",
                "Keep drains clear and secure areas at risk of flooding.",
            ),
            "high_precipitation_probability": (
                "Aktivitäten im Freien und offene Fenster entsprechend planen.",
                "Plan outdoor activities and open windows accordingly.",
            ),
            "strong_wind": (
                "Lose Gegenstände im Außenbereich sichern.",
                "Secure loose outdoor objects.",
            ),
            "storm_wind": (
                "Im Gebäude bleiben, lose Gegenstände sichern und lokale Warnmeldungen beachten.",
                "Stay indoors, secure loose objects and follow local alerts.",
            ),
            "thunderstorm": (
                "Geschützte Räume aufsuchen und offene Flächen sowie hohe Bäume meiden.",
                "Seek shelter and avoid open areas and tall trees.",
            ),
            "hail": (
                "Fahrzeuge, Pflanzen und empfindliche Außenanlagen schützen.",
                "Protect vehicles, plants and vulnerable outdoor equipment.",
            ),
            "snow_or_ice": (
                "Mit glatten Wegen und erschwerten Verkehrsbedingungen rechnen.",
                "Expect slippery surfaces and difficult travel conditions.",
            ),
            "dense_fog": (
                "Geschwindigkeit und Sichtabstand anpassen.",
                "Reduce speed and increase following distance.",
            ),
            "severe_weather": (
                "Lokale amtliche Warnmeldungen verfolgen und Schutzmaßnahmen vorbereiten.",
                "Monitor official local alerts and prepare protective measures.",
            ),
        }
        evidence = warning.get("evidence", {})
        metric_labels = {
            "temperature_c": ("Temperatur", "temperature", "°C"),
            "precipitation": ("Niederschlag", "precipitation", "mm/h"),
            "precipitation_probability": ("Wahrscheinlichkeit", "probability", "%"),
            "wind_speed": ("Windgeschwindigkeit", "wind speed", "km/h"),
            "visibility": ("Sichtweite", "visibility", "m"),
        }
        evidence_text = ""
        for metric_key, (german_label, english_label, unit) in metric_labels.items():
            metric_value = evidence.get(metric_key)
            if not isinstance(metric_value, (int, float)):
                continue
            label = german_label if use_german else english_label
            evidence_text = f"{label}: {metric_value:g} {unit}. "
            break
        window_text = WeatherIntelligenceEngine._warning_window(
            warning, time_zone_name=time_zone_name
        )
        if use_german:
            message = (
                f"{window_text}. {evidence_text}{recommended_actions[warning_code][0]}"
            )
        else:
            message = (
                f"{window_text}. {evidence_text}{recommended_actions[warning_code][1]}"
            )
        return (
            (
                {
                    "title": titles[warning_code][0 if use_german else 1],
                    "message": message,
                    "recommended_action": recommended_actions[warning_code][
                        0 if use_german else 1
                    ],
                }
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def _warning_window(warning: dict[str, Any], *, time_zone_name: str | None) -> str:
        local_zone = _safe_zone(time_zone_name)
        local_start = _as_utc(warning["start"]).astimezone(local_zone)
        local_end = _as_utc(warning["end"]).astimezone(local_zone)
        if local_start.date() == local_end.date():
            return f"{local_start.strftime('%d.%m.%Y, %H:%M')}–{local_end.strftime('%H:%M')}"
        return f"{local_start.strftime('%d.%m.%Y, %H:%M')}–{local_end.strftime('%d.%m.%Y, %H:%M')}"

    @staticmethod
    def _merge_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        warnings_by_code = defaultdict(list)
        for warning in warnings:
            warnings_by_code[warning["code"]].append(warning)
        merged_warnings = []
        for code_warnings in warnings_by_code.values():
            for warning in sorted(code_warnings, key=lambda item: item["start"]):
                previous_warning = merged_warnings[-1] if merged_warnings else None
                if (
                    previous_warning
                    and previous_warning["code"] == warning["code"]
                    and (_as_utc(previous_warning["end"]) == _as_utc(warning["start"]))
                ):
                    previous_warning["end"] = warning["end"]
                    for evidence_key, metric_value in warning["evidence"].items():
                        previous_value = previous_warning["evidence"].get(evidence_key)
                        if isinstance(metric_value, (int, float)) and (
                            not isinstance(previous_value, (int, float))
                            or abs(metric_value) > abs(previous_value)
                        ):
                            previous_warning["evidence"][evidence_key] = metric_value
                    continue
                merged_warnings.append(dict(warning))
                continue
            continue
        return sorted(merged_warnings, key=lambda item: item["start"])

    @staticmethod
    def _actual_history(
        history: dict[str, Any],
        observation: dict[str, float] | None = None,
        observed_at: datetime | None = None,
        time_zone: str | None = None,
        reference_now: datetime | None = None,
        *,
        observation_source: str = "unknown",
    ) -> dict[str, Any]:
        precipitation = history.get("precipitation", {})
        aggregate_envelope = history.get("aggregates", {})
        aggregate_rows = (
            aggregate_envelope.get("data", [])
            if isinstance(aggregate_envelope, dict)
            else []
        )
        frost_days = 0
        heat_days = 0
        for row in aggregate_rows if isinstance(aggregate_rows, list) else []:
            values = row.get("values") if isinstance(row, dict) else None
            temperature_stats = (
                values.get("temperature") if isinstance(values, dict) else None
            )
            if not isinstance(temperature_stats, dict):
                continue
            if (
                isinstance(temperature_stats.get("min"), (int, float))
                and temperature_stats["min"] <= 0
            ):
                frost_days += 1
            if (
                isinstance(temperature_stats.get("max"), (int, float))
                and temperature_stats["max"] >= 30
            ):
                heat_days += 1
        precipitation_available = bool(precipitation.get("available"))
        history_summary = history.get("summary", {})
        recent_envelope = history.get("recent", history.get("recorder_recent", {}))
        current_values = dict(observation or {})
        current_observed_at = observed_at
        current_source = (
            normalize_weather_source(observation_source) if current_values else None
        )
        fallback_observation = None
        latest_actual = WeatherIntelligenceEngine._latest_actual(
            history.get("series"), reference_now or datetime.now(timezone.utc)
        )
        if latest_actual is not None:
            if current_values:
                fallback_observation = {
                    "available": True,
                    "observed_at": latest_actual["observed_at"].isoformat(),
                    "source": normalize_weather_source(history.get("source")),
                    "values": dict(latest_actual["values"]),
                }
            else:
                current_values = dict(latest_actual["values"])
                current_observed_at = latest_actual["observed_at"]
                current_source = normalize_weather_source(history.get("source"))
        return (
            (
                {
                    "available": history.get("available", False),
                    "status": history.get("status"),
                    "source": history.get("source"),
                    "sources": {
                        "sfml_database": False,
                        "weather_fusion_tracker": False,
                        "home_assistant_recorder": history.get("source")
                        == "home_assistant_recorder",
                        "sfml_recent_series": isinstance(recent_envelope, dict)
                        and recent_envelope.get("status") == "ok",
                    },
                    "data_since": history_summary.get("data_since"),
                    "data_to": history_summary.get("data_to"),
                    "coverage": history_summary.get("coverage_percent"),
                    "coverage_by_metric": history_summary.get("coverage_by_metric", {}),
                    "complete_recorder_history": history_summary.get(
                        "complete_recorder_history", False
                    ),
                    "history_truncated": history_summary.get(
                        "history_truncated", False
                    ),
                    "aggregates": history_summary,
                    "current": {
                        "available": bool(current_values),
                        "observed_at": (
                            current_observed_at.astimezone(timezone.utc).isoformat()
                            if isinstance(current_observed_at, datetime)
                            else None
                        ),
                        "observation_date": (
                            current_observed_at.astimezone(_safe_zone(time_zone))
                            .date()
                            .isoformat()
                            if isinstance(current_observed_at, datetime)
                            else None
                        ),
                        "source": current_source,
                        "merge_strategy": "live_station_primary_no_cross_timestamp_merge",
                        "fallback_observation": fallback_observation,
                        **current_values,
                    },
                    "timeline": WeatherIntelligenceEngine._recorder_timeline(
                        aggregate_envelope, include_ranges=True
                    ),
                    "recent_timeline": WeatherIntelligenceEngine._recorder_timeline(
                        recent_envelope
                    ),
                    "records": history.get("record_events", []),
                    "monthly_series": history.get("monthly_series", []),
                    "year_comparison": history.get("year_comparison", {}),
                    "source_comparison": history.get("source_comparison", {}),
                    "capabilities": history.get("capabilities", {}),
                    "days": {
                        "frost": frost_days,
                        "heat": heat_days,
                        "rain": (
                            precipitation.get("rain_days")
                            if precipitation_available
                            else None
                        ),
                        "dry_spell_max": (
                            precipitation.get("dry_spell_days")
                            if precipitation_available
                            else None
                        ),
                    },
                    "precipitation": (
                        precipitation
                        if precipitation_available
                        else {
                            "available": False,
                            "reason": "precipitation_semantics_unavailable",
                        }
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def _latest_actual(envelope: Any, reference_now: datetime) -> dict[str, Any] | None:
        if not isinstance(envelope, dict) or envelope.get("status") not in {
            "ok",
            "partial",
            "available",
        }:
            return None
        rows = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(rows, list):
            return
        metric_aliases = {
            "temperature": "temperature_c",
            "wind_direction": "wind_bearing",
        }
        for row in reversed(rows):
            if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
                continue
            observed_at = row.get("observed_at")
            if isinstance(observed_at, str):
                try:
                    observed_at = _as_utc(observed_at)
                except ValueError:
                    continue
            if not isinstance(observed_at, datetime):
                continue
            observed_at = observed_at.astimezone(timezone.utc)
            reference_utc = reference_now.astimezone(timezone.utc)
            if (
                observed_at > reference_utc + _CURRENT_FUTURE_TOLERANCE
                or reference_utc - observed_at > _CURRENT_MAX_AGE
            ):
                continue
            values = {
                metric_aliases.get(metric, metric): metric_value
                for metric, metric_value in row["values"].items()
                if metric in _PUBLIC_ACTUAL_METRICS
                if isinstance(metric_value, (int, float))
                if not isinstance(metric_value, bool)
                if isfinite(metric_value)
            }
            if not values:
                continue
            return {"observed_at": observed_at, "values": values}

    @staticmethod
    def _recorder_timeline(
        envelope: Any, *, include_ranges: bool = False
    ) -> list[dict[str, Any]]:
        rows = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(rows, list):
            return []
        metric_aliases = {
            "temperature": "temperature_c",
            "wind_direction": "wind_bearing",
        }
        timeline = []
        for row in rows:
            values = row.get("values") if isinstance(row, dict) else None
            if not isinstance(values, dict):
                continue
            timeline_point = {"observed_at": row.get("period")}
            for metric, metric_stats in values.items():
                output_metric = metric_aliases.get(metric, metric)
                if isinstance(metric_stats, dict):
                    metric_value = metric_stats.get("amount")
                    if not isinstance(metric_value, (int, float)):
                        metric_value = metric_stats.get("avg")
                else:
                    metric_value = metric_stats
                if isinstance(metric_value, (int, float)):
                    timeline_point[output_metric] = metric_value
                if not include_ranges:
                    continue
                if not isinstance(metric_stats, dict):
                    continue
                minimum = metric_stats.get("min")
                maximum = metric_stats.get("max")
                if isinstance(minimum, (int, float)):
                    timeline_point[f"{output_metric}_min"] = minimum
                if not isinstance(maximum, (int, float)):
                    continue
                timeline_point[f"{output_metric}_max"] = maximum
                continue
            if not len(timeline_point) > 1:
                continue
            timeline.append(timeline_point)
            continue
        return timeline


def _in_germany(latitude: Any, longitude: Any) -> bool:
    if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
        return False
    if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
        return False
    return (
        _GERMANY_LATITUDE[0] <= float(latitude) <= _GERMANY_LATITUDE[1]
        and _GERMANY_LONGITUDE[0] <= float(longitude) <= _GERMANY_LONGITUDE[1]
    )


def _normalize_official_event(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _official_event_mapping(event_de: Any, event_en: Any) -> tuple[str, str]:
    normalized_events = [
        _normalize_official_event(event_text)
        for event_text in (event_de, event_en)
        if isinstance(event_text, str)
        if event_text.strip()
    ]
    for event_name in normalized_events:
        event_mapping = _OFFICIAL_EVENT_MAP.get(event_name)
        if not event_mapping is not None:
            continue
        return event_mapping
    for event_name in normalized_events:
        for known_event, event_mapping in sorted(
            _OFFICIAL_EVENT_MAP.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if not known_event in event_name:
                continue
            return event_mapping
    return ("severe_weather", "severe_weather")


def _parse_alert_time(value: Any) -> datetime | None:
    if not (isinstance(value, str) and value.strip()):
        return
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return
    if parsed_datetime.tzinfo is None:
        parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
    return parsed_datetime.astimezone(timezone.utc)


def _clip_alert_text(value: Any, limit: int) -> str | None:
    if not (isinstance(value, str) and value.strip()):
        return None
    return " ".join(value.split())[:limit]


def official_warnings_from_payload(
    payload: dict[str, Any] | None,
    *,
    now: datetime,
    language: str | None,
    time_zone_name: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return []
    use_german = str(language or "").lower().startswith("de")
    warnings = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        status = str(alert.get("status") or "actual").strip().lower()
        if status not in {"", "actual"}:
            continue
        onset = _parse_alert_time(alert.get("onset"))
        expires = _parse_alert_time(alert.get("expires"))
        if onset is None or expires is None or expires <= onset or (expires <= now):
            continue
        severity = _OFFICIAL_SEVERITY.get(str(alert.get("severity") or "").lower())
        if severity is None:
            continue
        warning_code, icon_key = _official_event_mapping(
            alert.get("event_de"), alert.get("event_en")
        )
        headline = _clip_alert_text(
            alert.get("headline_de" if use_german else "headline_en")
            or alert.get("headline_de")
            or alert.get("event_de")
            or alert.get("event_en"),
            160,
        )
        message = _clip_alert_text(
            alert.get("description_de" if use_german else "description_en")
            or alert.get("description_de"),
            400,
        )
        instruction = _clip_alert_text(
            alert.get("instruction_de" if use_german else "instruction_en")
            or alert.get("instruction_de"),
            320,
        )
        if headline is None or instruction is None:
            continue
        event = (
            alert.get("event_de") if use_german else alert.get("event_en")
        ) or alert.get("event_de")
        warning = {
            "code": warning_code,
            "severity": severity,
            "start": onset.isoformat(),
            "end": expires.isoformat(),
            "valid_at": onset.isoformat(),
            "impact_code": "official_alert",
            "evidence": {"event": event if isinstance(event, str) else warning_code},
            "contract_version": 1,
            "category": "weather_hazard",
            "official_alert": True,
            "evidence_kind": "official_cap",
            "icon_key": icon_key,
            "title": headline,
            "message": message or headline,
            "recommended_action": instruction,
        }
        warning["message"] = (
            f"{WeatherIntelligenceEngine._warning_window(warning, time_zone_name=time_zone_name)}. {warning['message']}"
        )
        warnings.append(warning)
        continue
    return warnings[:24]


def _warning_intervals_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = _as_utc(str(left["start"]))
        left_end = _as_utc(str(left["end"]))
        right_start = _as_utc(str(right["start"]))
        right_end = _as_utc(str(right["end"]))
    except (KeyError, TypeError, ValueError):
        return False
    return left_start < right_end and right_start < left_end


def _as_utc(value: str) -> datetime:
    parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed_datetime.astimezone(timezone.utc)


def _safe_zone(time_zone_name: str | None) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(time_zone_name) if time_zone_name else timezone.utc
    except (ValueError, TypeError):
        return timezone.utc
