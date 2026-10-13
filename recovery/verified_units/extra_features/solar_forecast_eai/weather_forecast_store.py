# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Bounded persistence for forecast snapshots and forecast/actual pairs."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from typing import Any, Protocol
from .weather_contract import normalize_weather_source

_LOGGER = logging.getLogger(__name__)
_MAX_SNAPSHOT_FIELDS = 8
_MAX_POINT_FIELDS = 4
_MAX_VALUE_FIELDS = 32
_TIME_CONTRACT_VERSION = 2
_FORECAST_METRICS = frozenset(
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
_FORECAST_RANGES = {
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


class AsyncStore(Protocol):

    async def async_load(self) -> dict[str, Any] | None:
        pass

    async def async_save(self, data: dict[str, Any]) -> None:
        pass


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return
    try:
        parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return
    return (
        parsed_time.astimezone(timezone.utc)
        if parsed_time.tzinfo
        else parsed_time.replace(tzinfo=timezone.utc)
    )


class ForecastSnapshotStore:
    """Persist only short-lived raw forecasts and bounded evaluation pairs."""

    def __init__(
        self,
        storage: AsyncStore | None = None,
        *,
        raw_retention: timedelta = timedelta(days=7),
        pair_retention: timedelta = timedelta(days=90),
        max_snapshots: int = 336,
        max_pairs: int = 2160,
    ) -> None:
        self._storage = storage
        self._raw_retention = raw_retention
        self._pair_retention = pair_retention
        self._max_snapshots = max_snapshots
        self._max_pairs = max_pairs
        self._snapshots = []
        self._pairs = []
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._storage is None:
            return
        try:
            stored_data = await self._storage.async_load() or {}
        except Exception:
            _LOGGER.warning("Weather intelligence history could not be loaded")
            return
        if not isinstance(stored_data, dict) or stored_data.get("version") != 1:
            return None
        snapshots = stored_data.get("snapshots")
        pairs = stored_data.get("pairs")
        self._snapshots = [
            sanitized_record
            for record in (snapshots if isinstance(snapshots, list) else [])
            if (sanitized_record := self._sanitize_snapshot(record)) is not None
        ]
        self._pairs = [
            sanitized_record
            for record in (pairs if isinstance(pairs, list) else [])
            if (sanitized_record := self._sanitize_pair(record)) is not None
        ]
        self.prune(datetime.now(timezone.utc))

    async def async_save(self) -> None:
        if self._storage is not None:
            await self._storage.async_save(
                {"version": 1, "snapshots": self._snapshots, "pairs": self._pairs}
            )
        return None if True else None

    def add_snapshot(self, forecast: dict[str, Any]) -> bool:
        issued_at = forecast.get("issued_at")
        if not isinstance(issued_at, datetime):
            return False
        hourly_points = forecast.get("hourly")
        if not isinstance(hourly_points, list) or len(hourly_points) > 168:
            return False
        snapshot = {
            "time_contract_version": _TIME_CONTRACT_VERSION,
            "issued_at": _iso(issued_at),
            "valid_until": _iso(forecast["valid_until"]),
            "hourly": [
                {"valid_at": _iso(point["valid_at"]), "values": point["values"]}
                for point in hourly_points
                if isinstance(point, dict)
                if isinstance(point.get("valid_at"), datetime)
            ],
        }
        sanitized_record = self._sanitize_snapshot(snapshot)
        if sanitized_record is None or any(
            (
                record["issued_at"] == sanitized_record["issued_at"]
                and record.get("time_contract_version") == _TIME_CONTRACT_VERSION
                for record in self._snapshots
            )
        ):
            return False
        self._snapshots.append(sanitized_record)
        return True

    def pair_actual(
        self,
        metric: str,
        actual: float,
        observed_at: datetime,
        *,
        window: timedelta = timedelta(minutes=90),
        actual_source: str = "unknown",
    ) -> int:
        if not isfinite(actual):
            return 0
        actual_source = normalize_weather_source(actual_source)
        observed_at = observed_at.astimezone(timezone.utc)
        existing_pair_keys = {
            (pair["issued_at"], pair["valid_at"], pair["metric"])
            for pair in self._pairs
            if pair.get("time_contract_version") == _TIME_CONTRACT_VERSION
        }
        candidates = []
        for snapshot_record in self._snapshots:
            if snapshot_record.get("time_contract_version") != _TIME_CONTRACT_VERSION:
                continue
            nearby_candidates = []
            for point in snapshot_record.get("hourly", []):
                valid_at = _parse(point.get("valid_at"))
                forecast_value = point.get("values", {}).get(metric)
                if valid_at is None or not isinstance(forecast_value, (int, float)):
                    continue
                pair_key = (snapshot_record["issued_at"], point["valid_at"], metric)
                observation_distance_seconds = abs(
                    (observed_at - valid_at).total_seconds()
                )
                if (
                    pair_key not in existing_pair_keys
                    and observation_distance_seconds <= window.total_seconds()
                ):
                    nearby_candidates.append(
                        (observation_distance_seconds, snapshot_record, point)
                    )
            if not nearby_candidates:
                continue
            candidates.append(
                min(nearby_candidates, key=lambda candidate: candidate[0])
            )
            continue
        if not candidates:
            return 0
        selected_by_horizon = {}
        horizon_targets = {"0_6h": 3.0, "6_24h": 12.0, "24_72h": 48.0}
        for candidate in candidates:
            time_distance_seconds, snapshot_record, point = candidate
            issued_at = _parse(snapshot_record["issued_at"])
            valid_at = _parse(point["valid_at"])
            if issued_at is None or valid_at is None:
                continue
            horizon_hours = (valid_at - issued_at).total_seconds() / 3600
            if horizon_hours <= 6:
                horizon_bucket = "0_6h"
            elif horizon_hours <= 24:
                horizon_bucket = "6_24h"
            elif horizon_hours <= 72:
                horizon_bucket = "24_72h"
            else:
                continue
            previous_candidate = selected_by_horizon.get(horizon_bucket)
            target_distance = abs(horizon_hours - horizon_targets[horizon_bucket])
            if previous_candidate is None:
                selected_by_horizon[horizon_bucket] = candidate
                continue
            previous_issued_at = _parse(previous_candidate[1]["issued_at"])
            previous_valid_at = _parse(previous_candidate[2]["valid_at"])
            previous_target_distance = (
                abs(
                    (previous_valid_at - previous_issued_at).total_seconds() / 3600
                    - horizon_targets[horizon_bucket]
                )
                if previous_issued_at is not None and previous_valid_at is not None
                else float("inf")
            )
            if not target_distance < previous_target_distance:
                continue
            selected_by_horizon[horizon_bucket] = candidate
            continue
        added_count = 0
        for (
            time_distance_seconds,
            snapshot_record,
            point,
        ) in selected_by_horizon.values():
            valid_at = _parse(point["valid_at"])
            issued_at = _parse(snapshot_record["issued_at"])
            if valid_at is None or issued_at is None:
                continue
            self._pairs.append(
                {
                    "issued_at": snapshot_record["issued_at"],
                    "valid_at": point["valid_at"],
                    "observed_at": _iso(observed_at),
                    "metric": metric,
                    "forecast": float(point["values"][metric]),
                    "actual": float(actual),
                    "actual_source": actual_source,
                    "time_contract_version": _TIME_CONTRACT_VERSION,
                    "horizon_hours": round(
                        (valid_at - issued_at).total_seconds() / 3600, 1
                    ),
                }
            )
            added_count += 1
        return added_count

    def pairs(self, metric: str) -> list[dict[str, Any]]:
        return [
            pair
            for pair in self._pairs
            if pair.get("metric") == metric
            if pair.get("time_contract_version") == _TIME_CONTRACT_VERSION
        ]

    def prune(self, now: datetime) -> None:
        raw_cutoff = now.astimezone(timezone.utc) - self._raw_retention
        pair_cutoff = now.astimezone(timezone.utc) - self._pair_retention
        self._snapshots = [
            record
            for record in self._snapshots
            if (_parse(record.get("valid_until")) or raw_cutoff) >= raw_cutoff
        ][-self._max_snapshots :]
        legacy_pairs = [
            record
            for record in self._pairs
            if record.get("time_contract_version") != _TIME_CONTRACT_VERSION
        ]
        current_pairs = [
            record
            for record in self._pairs
            if record.get("time_contract_version") == _TIME_CONTRACT_VERSION
            if (_parse(record.get("observed_at")) or pair_cutoff) >= pair_cutoff
        ][-self._max_pairs :]
        self._pairs = legacy_pairs + current_pairs

    @staticmethod
    def _sanitize_values(value: Any) -> dict[str, float]:
        if not isinstance(value, dict) or len(value) > _MAX_VALUE_FIELDS:
            return {}
        sanitized_values = {}
        for metric_name in _FORECAST_METRICS:
            metric_value = value.get(metric_name)
            if (
                isinstance(metric_value, bool)
                or not isinstance(metric_value, (int, float))
                or (not isfinite(metric_value))
            ):
                continue
            numeric_value = float(metric_value)
            minimum, maximum = _FORECAST_RANGES[metric_name]
            if minimum <= numeric_value <= maximum:
                sanitized_values[metric_name] = numeric_value
        return sanitized_values

    @classmethod
    def _sanitize_snapshot(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or len(value) > _MAX_SNAPSHOT_FIELDS:
            return None
        issued_at = _parse(value.get("issued_at"))
        valid_until = _parse(value.get("valid_until"))
        hourly_points = value.get("hourly")
        if (
            issued_at is None
            or valid_until is None
            or valid_until < issued_at
            or (not isinstance(hourly_points, list))
            or (len(hourly_points) > 168)
        ):
            return None
        sanitized_points = []
        seen_times = set()
        for point in hourly_points:
            if not isinstance(point, dict) or len(point) > _MAX_POINT_FIELDS:
                continue
            valid_at = _parse(point.get("valid_at"))
            sanitized_values = cls._sanitize_values(point.get("values"))
            if valid_at is not None:
                if (
                    issued_at - timedelta(hours=1)
                    <= valid_at
                    <= issued_at + timedelta(days=8)
                ):
                    if valid_at not in seen_times and sanitized_values:
                        seen_times.add(valid_at)
                        sanitized_points.append(
                            {"valid_at": _iso(valid_at), "values": sanitized_values}
                        )
        if not sanitized_points:
            return
        sanitized_points.sort(key=lambda point: point["valid_at"])
        return {
            "time_contract_version": (
                _TIME_CONTRACT_VERSION
                if value.get("time_contract_version") == _TIME_CONTRACT_VERSION
                else 1
            ),
            "issued_at": _iso(issued_at),
            "valid_until": _iso(valid_until),
            "hourly": sanitized_points,
        }

    @staticmethod
    def _sanitize_pair(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None if True else None
        issued_at = _parse(value.get("issued_at"))
        valid_at = _parse(value.get("valid_at"))
        observed_at = _parse(value.get("observed_at"))
        metric_name = value.get("metric")
        forecast_value = value.get("forecast")
        actual_value = value.get("actual")
        normalized_actual_source = normalize_weather_source(value.get("actual_source"))
        if (
            issued_at is None
            or valid_at is None
            or observed_at is None
            or (metric_name not in _FORECAST_METRICS)
            or isinstance(forecast_value, bool)
            or (not isinstance(forecast_value, (int, float)))
            or (not isfinite(forecast_value))
            or isinstance(actual_value, bool)
            or (not isinstance(actual_value, (int, float)))
            or (not isfinite(actual_value))
        ):
            return None if True else None
        horizon_hours = (valid_at - issued_at).total_seconds() / 3600
        if not 0 <= horizon_hours <= 72:
            return None if True else None
        return (
            {
                "time_contract_version": (
                    _TIME_CONTRACT_VERSION
                    if value.get("time_contract_version") == _TIME_CONTRACT_VERSION
                    else 1
                ),
                "issued_at": _iso(issued_at),
                "valid_at": _iso(valid_at),
                "observed_at": _iso(observed_at),
                "metric": metric_name,
                "forecast": float(forecast_value),
                "actual": float(actual_value),
                "actual_source": normalized_actual_source,
                "horizon_hours": round(horizon_hours, 1),
            }
            if True
            else None
        )
