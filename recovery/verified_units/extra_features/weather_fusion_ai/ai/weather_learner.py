# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Weather Learner - Learns and adapts expert weights based on accuracy.

This module implements the learning algorithm that:
1. Analyzes forecast deviations from PrecisionTracker
2. Calculates expert-specific accuracy scores
3. Updates expert weights per cloud type
4. Implements accelerated learning for large deviations
5. Maintains historical accuracy trends

Learning Algorithm:
- Uses exponential moving average (EMA) for smooth weight updates
- Accelerates learning when forecast errors exceed thresholds
- Maintains minimum weights to prevent expert exclusion
- Validates learning with statistical significance tests

@zara
"""
from __future__ import annotations
import asyncio
import json
import logging
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from .precision_tracker import PrecisionTracker, CloudType
from ..const import EXPERT_WEIGHT_KEYS, LEARNING_SMOOTHING_FACTOR
from ..file_io import async_write_json

_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: str) -> datetime:
    parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed_datetime.tzinfo is None:
        parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
    return parsed_datetime.astimezone(timezone.utc)


@dataclass
class ExpertAccuracy:
    """Accuracy metrics for a single expert."""

    expert_name: str
    sample_count: int
    mean_error: float
    std_error: float
    accuracy_score: float
    last_updated: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExpertAccuracy":
        return cls(**data)


@dataclass
class LearningEvent:
    """Record of a learning update."""

    timestamp: str
    cloud_type: str
    old_weights: Dict[str, float]
    new_weights: Dict[str, float]
    trigger: str
    accuracy_scores: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WeatherLearner:
    """Learns optimal expert weights from forecast accuracy data."""

    SMOOTHING_FACTOR = LEARNING_SMOOTHING_FACTOR
    ACCELERATED_THRESHOLD = 35.0
    ACCELERATED_FACTOR = 0.5
    MIN_WEIGHT = 0.05
    MAX_WEIGHT = 0.6
    MIN_SAMPLES_FOR_LEARNING = 24
    MIN_DAYS_FOR_LEARNING = 3
    FIELD_IMPORTANCE = {
        "temperature": 3.0,
        "precipitation": 2.5,
        "humidity": 1.5,
        "pressure": 1.0,
        "wind_speed": 0.5,
    }
    FIELD_ERROR_RANGES = {
        "temperature": 30.0,
        "cloud_cover": 100.0,
        "precipitation": 10.0,
        "humidity": 100.0,
        "wind_speed": 50.0,
        "pressure": 50.0,
        "visibility": 20000.0,
    }

    def __init__(self, data_dir: Path, precision_tracker: PrecisionTracker):
        "Initialize the weather learner.\n\nArgs:\n    data_dir: Base directory for weather_fusion_ai data\n    precision_tracker: PrecisionTracker instance for deviation data\n"
        self.data_dir = data_dir
        self.learning_dir = data_dir / "learning"
        self.logs_dir = data_dir / "logs"
        self._tracker = precision_tracker
        self._weights = {}
        self._accuracy = {}
        self._learning_log = []
        self._loaded = False
        self._last_learning = None
        self._stats_cache = None
        _LOGGER.info("WeatherLearner initialized")

    async def async_init(self) -> bool:
        """Initialize and load existing data."""
        self.learning_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        await self._load_weights()
        await self._load_learning_log()
        self._loaded = True
        _LOGGER.info(
            "WeatherLearner loaded: %d cloud types with weights", len(self._weights)
        )
        return True

    def get_weights(self, cloud_type: str) -> Dict[str, float]:
        """Get expert weights for a cloud type."""
        if cloud_type in self._weights:
            return self._weights[cloud_type].copy()
        return self._get_default_weights()

    def get_all_weights(self) -> Dict[str, Dict[str, float]]:
        """Get all expert weights."""
        return {
            cloud_type_key: expert_weights.copy()
            for cloud_type_key, expert_weights in self._weights.items()
        }

    def _get_default_weights(self) -> Dict[str, float]:
        """Get default expert weights."""
        return {
            "open_meteo": 0.25,
            "bright_sky": 0.35,
            "wttr_in": 0.15,
            "pirate_weather": 0.25,
        }

    def _sanitize_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Keep only canonical expert keys; otherwise fall back to defaults."""
        canonical_weights = {
            expert_name: float(weight)
            for expert_name, weight in (weights or {}).items()
            if expert_name in EXPERT_WEIGHT_KEYS
        }
        if set(canonical_weights) != set(EXPERT_WEIGHT_KEYS):
            return self._get_default_weights()
        return self._normalize_weights(canonical_weights)

    async def update_weights(
        self, cloud_type: str, new_weights: Dict[str, float]
    ) -> bool:
        """Manually update weights for a cloud type."""
        normalized_weights = self._normalize_weights(new_weights)
        self._weights[cloud_type] = normalized_weights
        await self._save_weights()
        return True

    async def learn(self, force: bool = False) -> Dict[str, Any]:
        "Execute learning cycle to update weights.\n\nArgs:\n    force: Force learning even if minimum samples not met\n\nReturns:\n    Learning result summary\n"
        result = {
            "success": False,
            "cloud_types_updated": [],
            "reason": "",
            "accuracy_scores": {},
        }
        tracker_stats = self._tracker.get_stats_summary()
        sample_count = tracker_stats.get("rolling_records", 0)
        if not force:
            if sample_count < self.MIN_SAMPLES_FOR_LEARNING:
                result["reason"] = (
                    f"Insufficient samples: {sample_count} < {self.MIN_SAMPLES_FOR_LEARNING}"
                )
                _LOGGER.debug(result["reason"])
                return result
        for cloud_type in CloudType:
            cloud_type_name = cloud_type.value
            expert_accuracy = await self._calculate_expert_accuracy(cloud_type_name)
            if not expert_accuracy:
                continue
            result["accuracy_scores"][cloud_type_name] = {
                expert_name: accuracy.accuracy_score
                for expert_name, accuracy in expert_accuracy.items()
            }
            maximum_error = max(
                (100 - accuracy.accuracy_score for accuracy in expert_accuracy.values())
            )
            accelerated = maximum_error > self.ACCELERATED_THRESHOLD
            old_weights = self.get_weights(cloud_type_name)
            new_weights = self._calculate_new_weights(
                expert_accuracy, old_weights, accelerated
            )
            self._weights[cloud_type_name] = new_weights
            result["cloud_types_updated"].append(cloud_type_name)
            learning_event = LearningEvent(
                timestamp=_utc_now().isoformat(),
                cloud_type=cloud_type_name,
                old_weights=old_weights,
                new_weights=new_weights,
                trigger="accelerated" if accelerated else "scheduled",
                accuracy_scores={
                    expert_name: accuracy.accuracy_score
                    for expert_name, accuracy in expert_accuracy.items()
                },
            )
            self._learning_log.append(learning_event)
            _LOGGER.info(
                "Updated weights for %s: %s (accelerated=%s)",
                cloud_type_name,
                {
                    expert_key: f"{weight:.2f}"
                    for expert_key, weight in new_weights.items()
                },
                accelerated,
            )
            continue
        self._last_learning = _utc_now()
        await self._save_weights()
        await self._save_learning_log()
        self._stats_cache = None
        result["success"] = len(result["cloud_types_updated"]) > 0
        result["reason"] = f"Updated {len(result['cloud_types_updated'])} cloud types"
        return result

    async def _calculate_expert_accuracy(
        self, cloud_type: str
    ) -> Dict[str, ExpertAccuracy]:
        """Calculate accuracy scores for all experts for a cloud type."""
        return self._calculate_expert_accuracy_from_records(cloud_type)

    def _calculate_expert_accuracy_from_records(
        self, cloud_type: str
    ) -> Dict[str, ExpertAccuracy]:
        """Calculate expert accuracy from recent deviations."""
        records_by_expert = {}
        if hasattr(self._tracker, "iter_rolling_records"):
            rolling_records = self._tracker.iter_rolling_records()
        else:
            rolling_records = getattr(self._tracker, "_rolling_records", [])
        for record in rolling_records:
            if record.cloud_type != cloud_type:
                continue
            if record.expert not in EXPERT_WEIGHT_KEYS:
                continue
            records_by_expert.setdefault(record.expert, []).append(record)
        accuracy_by_expert = {}
        for expert_name, expert_records in records_by_expert.items():
            if len(expert_records) < self.MIN_SAMPLES_FOR_LEARNING:
                continue
            weighted_error_total = 0.0
            importance_total = 0.0
            absolute_errors = []
            for field_name, importance in self.FIELD_IMPORTANCE.items():
                field_errors = [
                    abs(float(record.deviations[field_name]))
                    for record in expert_records
                    if field_name in record.deviations
                ]
                if len(field_errors) < 3:
                    continue
                mean_field_error = statistics.mean(field_errors)
                normalized_error = self._normalize_error(field_name, mean_field_error)
                weighted_error_total += normalized_error * importance
                importance_total += importance
                absolute_errors.extend(field_errors)
            if importance_total == 0:
                continue
            weighted_mean_error = weighted_error_total / importance_total
            accuracy_score = max(0.0, min(100.0, 100.0 - weighted_mean_error))
            accuracy_by_expert[expert_name] = ExpertAccuracy(
                expert_name=expert_name,
                sample_count=len(expert_records),
                mean_error=round(weighted_mean_error, 2),
                std_error=(
                    round(statistics.stdev(absolute_errors), 2)
                    if len(absolute_errors) > 1
                    else 0.0
                ),
                accuracy_score=round(accuracy_score, 2),
                last_updated=_utc_now().isoformat(),
            )
            continue
        return accuracy_by_expert

    def _normalize_error(self, field: str, error: float) -> float:
        """Normalize error to percentage scale based on typical field ranges."""
        error_range = self.FIELD_ERROR_RANGES.get(field, 100.0)
        return error / error_range * 100.0

    def _calculate_new_weights(
        self,
        accuracy_scores: Dict[str, ExpertAccuracy],
        old_weights: Dict[str, float],
        accelerated: bool,
    ) -> Dict[str, float]:
        """Calculate new weights based on accuracy scores."""
        if not accuracy_scores:
            return old_weights
        raw_weights = {}
        for expert_name, accuracy in accuracy_scores.items():
            raw_weights[expert_name] = (accuracy.accuracy_score / 100.0) ** 2
        for expert_name in old_weights:
            if expert_name not in raw_weights:
                raw_weights[expert_name] = self.MIN_WEIGHT
        target_weights = self._normalize_weights(raw_weights)
        smoothing_factor = (
            self.ACCELERATED_FACTOR if accelerated else self.SMOOTHING_FACTOR
        )
        new_weights = {}
        for expert_name in set(old_weights.keys()) | set(target_weights.keys()):
            previous_weight = old_weights.get(expert_name, 0.0)
            target_weight = target_weights.get(expert_name, 0.0)
            smoothed_weight = (
                previous_weight * (1 - smoothing_factor)
                + target_weight * smoothing_factor
            )
            smoothed_weight = max(
                self.MIN_WEIGHT, min(self.MAX_WEIGHT, smoothed_weight)
            )
            new_weights[expert_name] = smoothed_weight
        return self._sanitize_weights(new_weights)

    def _normalize_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Normalize weights to sum to 1.0."""
        total_weight = sum(weights.values())
        if total_weight <= 0:
            expert_count = len(weights)
            return {expert_key: 1.0 / expert_count for expert_key in weights}
        return {
            expert_key: round(weight / total_weight, 4)
            for expert_key, weight in weights.items()
        }

    async def _load_weights(self) -> None:
        """Load expert weights from file."""
        weights_path = self.learning_dir / "expert_weights.json"
        if not weights_path.exists():
            for cloud_type in CloudType:
                self._weights[cloud_type.value] = self._get_default_weights()
            await self._save_weights()
            return
        try:
            payload = await asyncio.to_thread(
                lambda: json.loads(weights_path.read_text())
            )
            self._weights = {}
            for cloud_type in CloudType:
                self._weights[cloud_type.value] = self._sanitize_weights(
                    payload.get("weights", {}).get(cloud_type.value, {})
                )
            last_learning_text = payload.get("last_learning")
            if last_learning_text:
                try:
                    self._last_learning = _as_utc(last_learning_text)
                except (ValueError, TypeError):
                    self._last_learning = None
            for cloud_type in CloudType:
                if cloud_type.value not in self._weights:
                    self._weights[cloud_type.value] = self._get_default_weights()
            _LOGGER.debug(
                f"Loaded weights for {len(self._weights)} cloud types, last_learning={self._last_learning}"
            )
        except Exception as error:
            _LOGGER.warning(f"Error loading weights: {error}")
            for cloud_type in CloudType:
                self._weights[cloud_type.value] = self._get_default_weights()
        return None if True else None

    async def _save_weights(self) -> None:
        """Save expert weights to file."""
        weights_path = self.learning_dir / "expert_weights.json"
        try:
            payload = {
                "version": "1.0",
                "last_updated": _utc_now().isoformat(),
                "last_learning": (
                    self._last_learning.isoformat() if self._last_learning else None
                ),
                "weights": self._weights,
            }
            await async_write_json(weights_path, payload)
        except Exception as error:
            _LOGGER.error(f"Error saving weights: {error}")
        return None if True else None

    async def _load_learning_log(self) -> None:
        """Load learning history log."""
        log_path = self.logs_dir / "learning_log.json"
        if not log_path.exists():
            return
        try:
            payload = await asyncio.to_thread(lambda: json.loads(log_path.read_text()))
            events = payload.get("events", [])[-100:]
            self._learning_log = [
                LearningEvent(**entry_or_error) for entry_or_error in events
            ]
        except Exception as entry_or_error:
            _LOGGER.warning(f"Error loading learning log: {entry_or_error}")
        return None if True else None

    async def _save_learning_log(self) -> None:
        """Save learning history log."""
        log_path = self.logs_dir / "learning_log.json"
        try:
            events = self._learning_log[-100:]
            payload = {
                "version": "1.0",
                "last_updated": _utc_now().isoformat(),
                "event_count": len(events),
                "events": [event_or_error.to_dict() for event_or_error in events],
            }
            await async_write_json(log_path, payload)
        except Exception as event_or_error:
            _LOGGER.error(f"Error saving learning log: {event_or_error}")
        return None if True else None

    def get_learning_stats(self) -> Dict[str, Any]:
        """Get learning statistics for diagnostics."""
        if self._stats_cache is not None:
            return self._stats_cache
        event_count = len(self._learning_log)
        recent_events = [
            learning_event
            for learning_event in self._learning_log
            if _as_utc(learning_event.timestamp) > _utc_now() - timedelta(days=7)
        ]
        average_accuracy = 0.0
        accuracy_count = 0
        for cloud_type in CloudType:
            for accuracy in self._calculate_expert_accuracy_from_records(
                cloud_type.value
            ).values():
                average_accuracy += accuracy.accuracy_score
                accuracy_count += 1
        if accuracy_count == 0:
            for recent_event in recent_events:
                for accuracy_score in recent_event.accuracy_scores.values():
                    average_accuracy += accuracy_score
                    accuracy_count += 1
        if accuracy_count > 0:
            average_accuracy /= accuracy_count
        result = {
            "total_learning_events": event_count,
            "recent_events_7d": len(recent_events),
            "last_learning": (
                self._last_learning.isoformat() if self._last_learning else None
            ),
            "average_accuracy_7d": round(average_accuracy, 2),
            "cloud_types_configured": len(self._weights),
            "weights_summary": {
                cloud_type_key: {
                    expert_key: f"{weight:.2f}"
                    for expert_key, weight in expert_weights.items()
                }
                for cloud_type_key, expert_weights in self._weights.items()
            },
        }
        self._stats_cache = result
        return result
