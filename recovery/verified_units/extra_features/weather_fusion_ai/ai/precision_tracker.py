# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Precision Tracker - Tracks forecast deviations across multiple bucket dimensions.

This module implements a multi-dimensional bucket system for tracking forecast accuracy:

Bucket Dimensions:
1. HOURLY (0-23): Each hour has its own correction factors
2. CLOUD_TYPE (6 types): Different accuracy per cloud condition
3. SEASON (4 seasons): Seasonal variations in forecast accuracy
4. FIELD (temp, clouds, wind, etc.): Per-field tracking

The tracker stores deviations (forecast - actual) and calculates rolling statistics
that the WeatherLearner uses to generate correction factors.

@zara
"""
from __future__ import annotations
import asyncio
import json
import logging
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from ..cloud_type import classify_cloud_type, is_known_cloud_type
from ..blend_math import circular_delta
from ..const import LEARNING_ROLLING_DAYS
from ..file_io import async_write_json

_LOGGER = logging.getLogger(__name__)


def _aware_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return
    try:
        parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return
    if parsed_timestamp.tzinfo is None:
        return parsed_timestamp.replace(tzinfo=_aware_now().tzinfo)
    return parsed_timestamp


class CloudType(Enum):
    """Cloud type classification."""

    CLEAR = "clear"
    CIRRUS = "cirrus"
    FAIR = "fair"
    MIXED = "mixed"
    STRATUS = "stratus"
    OVERCAST = "overcast"


class Season(Enum):
    """Seasonal classification."""

    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"


class WeatherField(Enum):
    """Weather fields to track."""

    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    CLOUD_COVER = "cloud_cover"
    WIND_SPEED = "wind_speed"
    WIND_DIRECTION = "wind_direction"
    PRESSURE = "pressure"
    PRECIPITATION = "precipitation"
    VISIBILITY = "visibility"
    UV_INDEX = "uv_index"


@dataclass
class DeviationRecord:
    """Single deviation record between forecast and actual."""

    timestamp: str
    date: str
    hour: int
    cloud_type: str
    season: str
    expert: str
    deviations: Dict[str, float]
    forecast_values: Dict[str, float]
    actual_values: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviationRecord":
        return cls(**data)


@dataclass
class BucketStats:
    """Statistics for a single bucket."""

    count: int = 0
    sum_deviation: float = 0.0
    sum_squared: float = 0.0
    min_deviation: float = float("inf")
    max_deviation: float = float("-inf")
    last_updated: Optional[str] = None

    @property
    def mean(self) -> float:
        if self.count == 0:
            return 0.0
        return self.sum_deviation / self.count

    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        mean_deviation = self.mean
        return max(0.0, self.sum_squared / self.count - mean_deviation**2)

    @property
    def std_dev(self) -> float:
        return self.variance**0.5

    def add_sample(self, deviation: float) -> None:
        self.count += 1
        self.sum_deviation += deviation
        self.sum_squared += deviation**2
        self.min_deviation = min(self.min_deviation, deviation)
        self.max_deviation = max(self.max_deviation, deviation)
        self.last_updated = _aware_now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "sum_deviation": self.sum_deviation,
            "sum_squared": self.sum_squared,
            "min_deviation": (
                self.min_deviation if self.min_deviation != float("inf") else None
            ),
            "max_deviation": (
                self.max_deviation if self.max_deviation != float("-inf") else None
            ),
            "last_updated": self.last_updated,
            "mean": self.mean,
            "std_dev": self.std_dev,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BucketStats":
        return cls(
            count=data.get("count", 0),
            sum_deviation=data.get("sum_deviation", 0.0),
            sum_squared=data.get("sum_squared", 0.0),
            min_deviation=(
                data.get("min_deviation")
                if data.get("min_deviation") is not None
                else float("inf")
            ),
            max_deviation=(
                data.get("max_deviation")
                if data.get("max_deviation") is not None
                else float("-inf")
            ),
            last_updated=data.get("last_updated"),
        )


class PrecisionTracker:
    """Tracks forecast precision across multiple bucket dimensions."""

    MIN_SAMPLES_RELIABLE = 10
    MIN_SAMPLES_USABLE = 3
    ROLLING_WINDOW_DAYS = LEARNING_ROLLING_DAYS
    HISTORY_RETENTION_DAYS = 730

    def __init__(self, data_dir: Path):
        "Initialize the precision tracker.\n\nArgs:\n    data_dir: Base directory for weather_fusion_ai data\n              (e.g., /config/weather_fusion_ai)\n"
        self.data_dir = data_dir
        self.history_dir = data_dir / "history" / "deviations"
        self.learning_dir = data_dir / "learning"
        self._hourly_buckets = {}
        self._cloud_type_buckets = {}
        self._seasonal_buckets = {}
        self._expert_buckets = {}
        self._combined_buckets = {}
        self._rolling_records = []
        self._loaded = False
        self._dirty = False
        _LOGGER.info(f"PrecisionTracker initialized with data_dir: {data_dir}")

    def _month_history_files(self) -> List[Path]:
        """Return YYYY-MM deviation files. Must not run on the HA event loop."""
        if not self.history_dir.is_dir():
            return []
        return sorted(self.history_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].json"))

    async def async_init(self) -> bool:
        """Initialize and load existing data."""
        await asyncio.to_thread(
            lambda: (
                self.history_dir.mkdir(parents=True, exist_ok=True),
                self.learning_dir.mkdir(parents=True, exist_ok=True),
            )
        )
        await self._load_bucket_stats()
        await self._load_rolling_window()
        self._loaded = True
        _LOGGER.info(
            "PrecisionTracker loaded: %d hourly buckets, %d cloud buckets, %d rolling records",
            len(self._hourly_buckets),
            len(self._cloud_type_buckets),
            len(self._rolling_records),
        )
        return True

    async def record_deviation(
        self,
        forecast: Dict[str, Any],
        actual: Dict[str, Any],
        expert: str,
        cloud_type: Optional[str] = None,
    ) -> Optional[DeviationRecord]:
        "Record a deviation between forecast and actual values.\n\nArgs:\n    forecast: Forecast values dict\n    actual: Actual observed values dict\n    expert: Name of the expert/source that made the forecast\n    cloud_type: Optional cloud type override\n\nReturns:\n    The created DeviationRecord, or None if insufficient data\n"
        observed_at = _aware_now()
        record_date = observed_at.strftime("%Y-%m-%d")
        target_hour = observed_at.hour
        deviations = {}
        forecast_values = {}
        actual_values = {}
        for weather_field in WeatherField:
            field_name = weather_field.value
            forecast_value = forecast.get(field_name)
            actual_value = actual.get(field_name)
            if forecast_value is not None:
                if actual_value is not None:
                    try:
                        forecast_number = float(forecast_value)
                        actual_number = float(actual_value)
                        if field_name == "wind_direction":
                            deviations[field_name] = circular_delta(
                                forecast_number, actual_number
                            )
                        else:
                            deviations[field_name] = forecast_number - actual_number
                        forecast_values[field_name] = forecast_number
                        actual_values[field_name] = actual_number
                    except (ValueError, TypeError):
                        continue
        if not deviations:
            _LOGGER.debug("No valid deviations to record")
            return
        if not is_known_cloud_type(cloud_type):
            cloud_type = classify_cloud_type(
                cloud_total=actual.get("cloud_cover") or forecast.get("cloud_cover")
            )
        season = self._get_season(observed_at.month)
        record = DeviationRecord(
            timestamp=observed_at.isoformat(),
            date=record_date,
            hour=target_hour,
            cloud_type=cloud_type,
            season=season,
            expert=expert,
            deviations=deviations,
            forecast_values=forecast_values,
            actual_values=actual_values,
        )
        self._update_buckets(record)
        self._rolling_records.append(record)
        self._trim_rolling_window()
        await self._save_deviation_record(record)
        self._dirty = True
        _LOGGER.debug(
            "Recorded deviation for %s %02d:00 (%s, %s): %d fields",
            record_date,
            target_hour,
            cloud_type,
            expert,
            len(deviations),
        )
        return record

    def _update_buckets(self, record: DeviationRecord) -> None:
        """Update all bucket statistics with a new record."""
        target_hour = record.hour
        cloud_type = record.cloud_type
        season = record.season
        expert_name = record.expert
        for field_name, deviation in record.deviations.items():
            if target_hour not in self._hourly_buckets:
                self._hourly_buckets[target_hour] = {}
            if field_name not in self._hourly_buckets[target_hour]:
                self._hourly_buckets[target_hour][field_name] = BucketStats()
            self._hourly_buckets[target_hour][field_name].add_sample(deviation)
            if cloud_type not in self._cloud_type_buckets:
                self._cloud_type_buckets[cloud_type] = {}
            if field_name not in self._cloud_type_buckets[cloud_type]:
                self._cloud_type_buckets[cloud_type][field_name] = BucketStats()
            self._cloud_type_buckets[cloud_type][field_name].add_sample(deviation)
            if season not in self._seasonal_buckets:
                self._seasonal_buckets[season] = {}
            if field_name not in self._seasonal_buckets[season]:
                self._seasonal_buckets[season][field_name] = BucketStats()
            self._seasonal_buckets[season][field_name].add_sample(deviation)
            if expert_name not in self._expert_buckets:
                self._expert_buckets[expert_name] = {}
            if field_name not in self._expert_buckets[expert_name]:
                self._expert_buckets[expert_name][field_name] = BucketStats()
            self._expert_buckets[expert_name][field_name].add_sample(deviation)
            combined_key = f"{target_hour:02d}_{cloud_type}"
            if combined_key not in self._combined_buckets:
                self._combined_buckets[combined_key] = {}
            if field_name not in self._combined_buckets[combined_key]:
                self._combined_buckets[combined_key][field_name] = BucketStats()
            self._combined_buckets[combined_key][field_name].add_sample(deviation)
            continue

    def get_hourly_stats(self, hour: int, field: str) -> Optional[BucketStats]:
        """Get bucket stats for a specific hour and field."""
        if hour in self._hourly_buckets:
            return self._hourly_buckets[hour].get(field)

    def get_cloud_type_stats(
        self, cloud_type: str, field: str
    ) -> Optional[BucketStats]:
        """Get bucket stats for a specific cloud type and field."""
        if cloud_type in self._cloud_type_buckets:
            return self._cloud_type_buckets[cloud_type].get(field)

    def get_seasonal_stats(self, season: str, field: str) -> Optional[BucketStats]:
        """Get bucket stats for a specific season and field."""
        if season in self._seasonal_buckets:
            return self._seasonal_buckets[season].get(field)

    def get_expert_stats(self, expert: str, field: str) -> Optional[BucketStats]:
        """Get bucket stats for a specific expert and field."""
        if expert in self._expert_buckets:
            return self._expert_buckets[expert].get(field)

    def get_combined_stats(
        self, hour: int, cloud_type: str, field: str
    ) -> Optional[BucketStats]:
        """Get bucket stats for combined hour + cloud_type."""
        combined_key = f"{hour:02d}_{cloud_type}"
        if combined_key in self._combined_buckets:
            return self._combined_buckets[combined_key].get(field)

    def get_rolling_stats(self, field: str) -> Dict[str, Any]:
        """Calculate rolling statistics from recent records."""
        deviations = [
            rolling_record.deviations.get(field)
            for rolling_record in self._rolling_records
            if rolling_record.deviations.get(field) is not None
        ]
        if not deviations:
            return {"count": 0, "mean": 0.0, "std_dev": 0.0}
        return {
            "count": len(deviations),
            "mean": statistics.mean(deviations),
            "std_dev": statistics.stdev(deviations) if len(deviations) > 1 else 0.0,
            "min": min(deviations),
            "max": max(deviations),
            "median": statistics.median(deviations),
        }

    def iter_rolling_records(self) -> List[DeviationRecord]:
        """Return a copy of the rolling-window records."""
        return list(self._rolling_records)

    def _get_season(self, month: int) -> str:
        """Get season from month."""
        if month in (3, 4, 5):
            return Season.SPRING.value
        if month in (6, 7, 8):
            return Season.SUMMER.value
        if month in (9, 10, 11):
            return Season.AUTUMN.value
        return Season.WINTER.value

    def _trim_rolling_window(self) -> None:
        """Remove records older than rolling window."""
        cutoff = _aware_now() - timedelta(days=self.ROLLING_WINDOW_DAYS)
        retained_records = []
        for record in self._rolling_records:
            parsed_timestamp = _parse_timestamp(record.timestamp)
            if parsed_timestamp is not None and parsed_timestamp >= cutoff:
                retained_records.append(record)
        self._rolling_records = retained_records

    async def _save_deviation_record(self, record: DeviationRecord) -> None:
        """Save deviation record to monthly history file."""
        month_key = record.date[:7]
        history_path = self.history_dir / f"{month_key}.json"
        try:
            records = []
            if history_path.exists():
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(history_path.read_text())
                )
                records = file_payload.get("records", [])
            records.append(record.to_dict())
            history_payload = {
                "version": "1.0",
                "month": month_key,
                "record_count": len(records),
                "last_updated": _aware_now().isoformat(),
                "records": records,
            }
            await async_write_json(history_path, history_payload)
        except Exception as error:
            _LOGGER.warning(f"Error saving deviation record: {error}")
        return None if True else None

    async def prune_deviation_history(self) -> None:
        """Remove deviation records beyond the documented retention period."""
        cutoff = _aware_now() - timedelta(days=self.HISTORY_RETENTION_DAYS)

        def prune() -> None:
            from ..file_io import write_json_atomic as write_json_atomic

            for history_path in self._month_history_files():
                try:
                    file_payload = json.loads(history_path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                stored_records = file_payload.get("records", [])
                retained_records = []
                for stored_record in (
                    stored_records if isinstance(stored_records, list) else []
                ):
                    record_stamp = _parse_timestamp(
                        stored_record.get("timestamp")
                        if isinstance(stored_record, dict)
                        else None
                    )
                    if record_stamp is not None and record_stamp >= cutoff:
                        retained_records.append(stored_record)
                if not retained_records:
                    history_path.unlink(missing_ok=True)
                    continue
                if not len(retained_records) != len(stored_records):
                    continue
                file_payload["records"] = retained_records
                file_payload["record_count"] = len(retained_records)
                file_payload["last_updated"] = _aware_now().isoformat()
                write_json_atomic(history_path, file_payload)
                continue

        await asyncio.to_thread(prune)

    def _clear_buckets(self) -> None:
        self._hourly_buckets = {}
        self._cloud_type_buckets = {}
        self._seasonal_buckets = {}
        self._expert_buckets = {}
        self._combined_buckets = {}

    async def rebuild_from_history(self) -> None:
        """Rebuild bucket aggregates from retained deviation files."""
        self._clear_buckets()
        self._rolling_records = []

        def load_month_files() -> List[tuple[Path, Any]]:
            loaded_files = []
            for history_path in self._month_history_files():
                try:
                    loaded_files.append(
                        (history_path, json.loads(history_path.read_text()))
                    )
                except Exception as read_error:
                    loaded_files.append((history_path, read_error))
                    continue
            return loaded_files

        for history_path, payload in await asyncio.to_thread(load_month_files):
            if isinstance(payload, Exception):
                _LOGGER.warning("Error rebuilding from %s: %s", history_path, payload)
                continue
            for record_data in payload.get("records", []):
                try:
                    record = DeviationRecord.from_dict(record_data)
                except (TypeError, ValueError, KeyError):
                    continue
                self._update_buckets(record)
                self._rolling_records.append(record)
        self._trim_rolling_window()
        self._dirty = True

    async def _load_bucket_stats(self) -> None:
        """Load bucket statistics from files."""
        hourly_path = self.learning_dir / "hourly_buckets.json"
        if hourly_path.exists():
            try:
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(hourly_path.read_text())
                )
                for hour_key, field_stats in file_payload.get("buckets", {}).items():
                    target_hour = int(hour_key)
                    self._hourly_buckets[target_hour] = {
                        stats_field_name: BucketStats.from_dict(stats)
                        for stats_field_name, stats in field_stats.items()
                    }
            except Exception as error:
                _LOGGER.warning(f"Error loading hourly buckets: {error}")
        cloud_path = self.learning_dir / "cloud_type_buckets.json"
        if cloud_path.exists():
            try:
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(cloud_path.read_text())
                )
                for cloud_type, field_stats in file_payload.get("buckets", {}).items():
                    self._cloud_type_buckets[cloud_type] = {
                        stats_field_name: BucketStats.from_dict(stats)
                        for stats_field_name, stats in field_stats.items()
                    }
            except Exception as error:
                _LOGGER.warning(f"Error loading cloud type buckets: {error}")
        seasonal_path = self.learning_dir / "seasonal_buckets.json"
        if seasonal_path.exists():
            try:
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(seasonal_path.read_text())
                )
                for season, field_stats in file_payload.get("buckets", {}).items():
                    self._seasonal_buckets[season] = {
                        stats_field_name: BucketStats.from_dict(stats)
                        for stats_field_name, stats in field_stats.items()
                    }
            except Exception as error:
                _LOGGER.warning(f"Error loading seasonal buckets: {error}")
        expert_path = self.learning_dir / "expert_buckets.json"
        if expert_path.exists():
            try:
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(expert_path.read_text())
                )
                for expert_name, field_stats in file_payload.get("buckets", {}).items():
                    self._expert_buckets[expert_name] = {
                        stats_field_name: BucketStats.from_dict(stats)
                        for stats_field_name, stats in field_stats.items()
                    }
            except Exception as error:
                _LOGGER.warning(f"Error loading expert buckets: {error}")
        combined_path = self.learning_dir / "combined_buckets.json"
        if combined_path.exists():
            try:
                file_payload = await asyncio.to_thread(
                    lambda: json.loads(combined_path.read_text())
                )
                for combined_key, field_stats in file_payload.get(
                    "buckets", {}
                ).items():
                    self._combined_buckets[combined_key] = {
                        stats_field_name: BucketStats.from_dict(stats)
                        for stats_field_name, stats in field_stats.items()
                    }
            except Exception as error:
                _LOGGER.warning(f"Error loading combined buckets: {error}")
        return None if True else None

    async def _load_rolling_window(self) -> None:
        """Load recent records for rolling window calculations."""
        cutoff = _aware_now() - timedelta(days=self.ROLLING_WINDOW_DAYS)
        month_keys = {
            (_aware_now() - timedelta(days=day_offset)).strftime("%Y-%m")
            for day_offset in range(self.ROLLING_WINDOW_DAYS + 1)
        }
        seen_records = set()
        self._rolling_records = []
        for month_key in sorted(month_keys):
            history_path = self.history_dir / f"{month_key}.json"
            if not history_path.exists():
                continue
            try:
                file_payload = await asyncio.to_thread(
                    lambda fp=history_path: json.loads(fp.read_text())
                )
                for record_data in file_payload.get("records", []):
                    record = DeviationRecord.from_dict(record_data)
                    record_key = (record.timestamp, record.expert, record.hour)
                    if record_key in seen_records:
                        continue
                    parsed_timestamp = _parse_timestamp(record.timestamp)
                    if parsed_timestamp is None or parsed_timestamp < cutoff:
                        continue
                    seen_records.add(record_key)
                    self._rolling_records.append(record)
            except Exception as error:
                _LOGGER.warning(
                    f"Error loading rolling window from {history_path}: {error}"
                )
                continue
        self._rolling_records.sort(key=lambda r: r.timestamp)

    async def save_all(self) -> None:
        """Save all bucket statistics to files."""
        if not self._dirty:
            return
        try:
            updated_at = _aware_now().isoformat()
            pass
            pass
            pass
            pass
            bucket_payloads = (
                (
                    self.learning_dir / "hourly_buckets.json",
                    {
                        "version": "1.0",
                        "last_updated": updated_at,
                        "buckets": {
                            str(target_hour): {
                                stats_field_name: bucket_value.to_dict()
                                for stats_field_name, bucket_value in field_stats.items()
                            }
                            for target_hour, field_stats in self._hourly_buckets.items()
                        },
                    },
                ),
                (
                    self.learning_dir / "cloud_type_buckets.json",
                    {
                        "version": "1.0",
                        "last_updated": updated_at,
                        "buckets": {
                            cloud_key: {
                                stats_field_name: bucket_value.to_dict()
                                for stats_field_name, bucket_value in field_stats.items()
                            }
                            for cloud_key, field_stats in self._cloud_type_buckets.items()
                        },
                    },
                ),
                (
                    self.learning_dir / "seasonal_buckets.json",
                    {
                        "version": "1.0",
                        "last_updated": updated_at,
                        "buckets": {
                            bucket_value: {
                                stats_field_name: season_stats.to_dict()
                                for stats_field_name, season_stats in field_stats.items()
                            }
                            for bucket_value, field_stats in self._seasonal_buckets.items()
                        },
                    },
                ),
                (
                    self.learning_dir / "expert_buckets.json",
                    {
                        "version": "1.0",
                        "last_updated": updated_at,
                        "buckets": {
                            expert_key_or_error: {
                                stats_field_name: bucket_value.to_dict()
                                for stats_field_name, bucket_value in field_stats.items()
                            }
                            for expert_key_or_error, field_stats in self._expert_buckets.items()
                        },
                    },
                ),
                (
                    self.learning_dir / "combined_buckets.json",
                    {
                        "version": "1.0",
                        "last_updated": updated_at,
                        "buckets": {
                            combined_bucket_key: {
                                stats_field_name: bucket_value.to_dict()
                                for stats_field_name, bucket_value in field_stats.items()
                            }
                            for combined_bucket_key, field_stats in self._combined_buckets.items()
                        },
                    },
                ),
            )
            for history_path, payload in bucket_payloads:
                await async_write_json(history_path, payload)
            self._dirty = False
            _LOGGER.info("Saved all precision tracker buckets")
        except Exception as expert_key_or_error:
            _LOGGER.error(f"Error saving precision tracker: {expert_key_or_error}")
        return None if True else None

    def get_stats_summary(self) -> Dict[str, Any]:
        """Get summary statistics for diagnostics."""
        hourly_sample_count = sum(
            (
                sum((stats.count for stats in field_stats.values()))
                for field_stats in self._hourly_buckets.values()
            )
        )
        cloud_sample_count = sum(
            (
                sum((stats.count for stats in field_stats.values()))
                for field_stats in self._cloud_type_buckets.values()
            )
        )
        return {
            "hourly_buckets": len(self._hourly_buckets),
            "cloud_type_buckets": len(self._cloud_type_buckets),
            "seasonal_buckets": len(self._seasonal_buckets),
            "expert_buckets": len(self._expert_buckets),
            "combined_buckets": len(self._combined_buckets),
            "rolling_records": len(self._rolling_records),
            "total_hourly_samples": hourly_sample_count,
            "total_cloud_samples": cloud_sample_count,
        }
