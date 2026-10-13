# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Pattern Detector - Detects weather patterns and anomalies.

This module implements pattern detection algorithms that:
1. Identify recurring weather patterns (morning fog, afternoon thermals, etc.)
2. Detect anomalies in forecast vs actual deviations
3. Learn location-specific weather behaviors
4. Provide pattern-based forecast adjustments

Pattern Types:
- Temporal: Daily patterns (morning, midday, evening, night)
- Seasonal: Monthly/seasonal patterns
- Weather-state: Patterns during specific conditions (rain, clear, etc.)
- Transition: Weather change patterns (clearing, deteriorating)
- Microclimate: Location-specific persistent patterns

@zara
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import statistics
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .precision_tracker import PrecisionTracker, WeatherField
from ..file_io import async_write_json

_LOGGER = logging.getLogger(__name__)


class PatternType(Enum):
    """Types of weather patterns."""

    MORNING_BIAS = "morning_bias"
    AFTERNOON_BIAS = "afternoon_bias"
    EVENING_TRANSITION = "evening_transition"
    NIGHT_STABILITY = "night_stability"
    CLOUD_BUILDUP = "cloud_buildup"
    TEMPERATURE_LAG = "temperature_lag"
    PRECIPITATION_FALSE_POSITIVE = "precip_false_positive"
    PRECIPITATION_MISS = "precip_miss"
    MICROCLIMATE_WARM = "microclimate_warm"
    MICROCLIMATE_COLD = "microclimate_cold"
    WIND_CHANNELING = "wind_channeling"


@dataclass
class DetectedPattern:
    """A detected weather pattern."""

    pattern_type: str
    confidence: float
    strength: float
    description: str
    hours_affected: List[int]
    fields_affected: List[str]
    correction_factor: float
    sample_count: int
    last_detected: str
    field_corrections: Dict[str, float] = None

    def __post_init__(self) -> None:
        if self.field_corrections is None:
            self.field_corrections = {}
        return None if True else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetectedPattern":
        field_names = {dataclass_field.name for dataclass_field in fields(cls)}
        known_fields = {key: value for key, value in data.items() if key in field_names}
        known_fields.setdefault("field_corrections", {})
        return cls(**known_fields)


@dataclass
class AnomalyRecord:
    """Record of a detected anomaly."""

    timestamp: str
    date: str
    hour: int
    field: str
    expected_value: float
    actual_value: float
    deviation: float
    z_score: float
    is_anomaly: bool
    possible_cause: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PatternDetector:
    """Detects and learns weather patterns from historical data."""

    ANOMALY_Z_SCORE_THRESHOLD = 2.5
    MIN_SAMPLES_FOR_PATTERN = 14
    PATTERN_CONFIDENCE_THRESHOLD = 0.6
    MORNING_HOURS = [5, 6, 7, 8, 9, 10]
    MIDDAY_HOURS = [11, 12, 13, 14]
    AFTERNOON_HOURS = [15, 16, 17, 18]
    EVENING_HOURS = [19, 20, 21, 22]
    NIGHT_HOURS = [23, 0, 1, 2, 3, 4]

    def __init__(self, data_dir: Path, precision_tracker: PrecisionTracker):
        "Initialize pattern detector.\n\nArgs:\n    data_dir: Base directory for weather_fusion_ai data\n    precision_tracker: PrecisionTracker instance for deviation data\n"
        self.data_dir = data_dir
        self.models_dir = data_dir / "models"
        self._tracker = precision_tracker
        self._patterns = {}
        self._anomalies = []
        self._pattern_weights = {}
        self._loaded = False
        _LOGGER.info("PatternDetector initialized")

    async def async_init(self) -> bool:
        """Initialize and load existing data."""
        self.models_dir.mkdir(parents=True, exist_ok=True)
        await self._load_patterns()
        await self._load_anomaly_thresholds()
        self._loaded = True
        _LOGGER.info(f"PatternDetector loaded: {len(self._patterns)} patterns")
        return True

    async def detect_patterns(self) -> Dict[str, DetectedPattern]:
        "Run full pattern detection on accumulated data.\n\nReturns:\n    Dict of detected patterns\n"
        detected_patterns = {}
        morning_pattern = await self._detect_time_period_bias(
            self.MORNING_HOURS,
            PatternType.MORNING_BIAS.value,
            "Systematic morning forecast bias",
        )
        if morning_pattern:
            detected_patterns[morning_pattern.pattern_type] = morning_pattern
        afternoon_pattern = await self._detect_time_period_bias(
            self.AFTERNOON_HOURS,
            PatternType.AFTERNOON_BIAS.value,
            "Systematic afternoon forecast bias",
        )
        if afternoon_pattern:
            detected_patterns[afternoon_pattern.pattern_type] = afternoon_pattern
        temperature_lag = await self._detect_temperature_lag()
        if temperature_lag:
            detected_patterns[temperature_lag.pattern_type] = temperature_lag
        microclimate = await self._detect_microclimate()
        if microclimate:
            detected_patterns[microclimate.pattern_type] = microclimate
        cloud_buildup = await self._detect_cloud_buildup()
        if cloud_buildup:
            detected_patterns[cloud_buildup.pattern_type] = cloud_buildup
        self._patterns = detected_patterns
        await self._save_patterns()
        _LOGGER.info(
            f"Pattern detection complete: {len(detected_patterns)} patterns found"
        )
        return detected_patterns

    async def _detect_time_period_bias(
        self, hours: List[int], pattern_type: str, description: str
    ) -> Optional[DetectedPattern]:
        """Detect systematic bias in a time period."""
        means_by_field = {}
        for hour in hours:
            for weather_field in WeatherField:
                field_name = weather_field.value
                hourly_stats = self._tracker.get_hourly_stats(hour, field_name)
                if not hourly_stats or hourly_stats.count < 3:
                    continue
                if field_name not in means_by_field:
                    means_by_field[field_name] = []
                means_by_field[field_name].append(hourly_stats.mean)
        if not means_by_field:
            return
        field_corrections = {}
        sample_count = 0
        for field_name, hourly_means in means_by_field.items():
            if len(hourly_means) < 3:
                continue
            mean_bias = statistics.mean(hourly_means)
            bias_std = statistics.stdev(hourly_means) if len(hourly_means) > 1 else 1.0
            if bias_std > 0 and abs(mean_bias / bias_std) > 1.0:
                field_corrections[field_name] = round(-mean_bias, 3)
                sample_count += len(hourly_means)
        if not field_corrections or sample_count < self.MIN_SAMPLES_FOR_PATTERN:
            return None
        strength = statistics.mean(
            (abs(correction) for correction in field_corrections.values())
        )
        confidence = min(1.0, sample_count / (self.MIN_SAMPLES_FOR_PATTERN * 2))
        return DetectedPattern(
            pattern_type=pattern_type,
            confidence=round(confidence, 3),
            strength=round(strength, 3),
            description=f"{description}: {len(field_corrections)} field(s)",
            hours_affected=hours,
            fields_affected=list(field_corrections),
            correction_factor=0.0,
            sample_count=sample_count,
            last_detected=datetime.now().astimezone().isoformat(),
            field_corrections=field_corrections,
        )

    async def _detect_temperature_lag(self) -> Optional[DetectedPattern]:
        """Detect if forecast temperature lags behind actual."""
        morning_biases = []
        afternoon_biases = []
        for hour in self.MORNING_HOURS:
            hourly_stats = self._tracker.get_hourly_stats(hour, "temperature")
            if hourly_stats and hourly_stats.count >= 3:
                morning_biases.append(hourly_stats.mean)
        for hour in self.AFTERNOON_HOURS:
            hourly_stats = self._tracker.get_hourly_stats(hour, "temperature")
            if hourly_stats and hourly_stats.count >= 3:
                afternoon_biases.append(hourly_stats.mean)
        if len(morning_biases) < 3 or len(afternoon_biases) < 3:
            return None
        morning_mean = statistics.mean(morning_biases)
        afternoon_mean = statistics.mean(afternoon_biases)
        if morning_mean > 0.5 and afternoon_mean < -0.5:
            lag_strength = (morning_mean - afternoon_mean) / 2
            confidence = 0.7
            return DetectedPattern(
                pattern_type=PatternType.TEMPERATURE_LAG.value,
                confidence=confidence,
                strength=round(lag_strength, 3),
                description=f"Forecast temperature lags actual by ~{lag_strength:.1f}°C",
                hours_affected=self.MORNING_HOURS + self.AFTERNOON_HOURS,
                fields_affected=["temperature"],
                correction_factor=0.0,
                sample_count=len(morning_biases) + len(afternoon_biases),
                last_detected=datetime.now().isoformat(),
            )
        return None if True else None

    async def _detect_microclimate(self) -> Optional[DetectedPattern]:
        """Detect persistent location-specific temperature bias."""
        temperature_stats = []
        for hour in range(24):
            hourly_stats = self._tracker.get_hourly_stats(hour, "temperature")
            if hourly_stats and hourly_stats.count >= 3:
                temperature_stats.append((hourly_stats.mean, hourly_stats.count))
        if len(temperature_stats) < 12:
            return
        total_samples = sum(
            (sample_count for unused_mean, sample_count in temperature_stats)
        )
        weighted_bias = (
            sum(
                (
                    mean_bias * sample_count
                    for mean_bias, sample_count in temperature_stats
                )
            )
            / total_samples
        )
        if abs(weighted_bias) > 1.0:
            if weighted_bias > 0:
                pattern_type = PatternType.MICROCLIMATE_COLD.value
                description = (
                    f"Location is {abs(weighted_bias):.1f}°C colder than forecasts"
                )
            else:
                pattern_type = PatternType.MICROCLIMATE_WARM.value
                description = (
                    f"Location is {abs(weighted_bias):.1f}°C warmer than forecasts"
                )
            return DetectedPattern(
                pattern_type=pattern_type,
                confidence=min(0.9, total_samples / 200),
                strength=round(abs(weighted_bias), 3),
                description=description,
                hours_affected=list(range(24)),
                fields_affected=["temperature"],
                correction_factor=round(-weighted_bias, 3),
                sample_count=total_samples,
                last_detected=datetime.now().isoformat(),
            )

    async def _detect_cloud_buildup(self) -> Optional[DetectedPattern]:
        """Detect daily cloud development pattern."""
        morning_cloud_biases = []
        afternoon_cloud_biases = []
        for hour in self.MORNING_HOURS:
            hourly_stats = self._tracker.get_hourly_stats(hour, "cloud_cover")
            if hourly_stats and hourly_stats.count >= 3:
                morning_cloud_biases.append(hourly_stats.mean)
        for hour in self.AFTERNOON_HOURS:
            hourly_stats = self._tracker.get_hourly_stats(hour, "cloud_cover")
            if hourly_stats and hourly_stats.count >= 3:
                afternoon_cloud_biases.append(hourly_stats.mean)
        if len(morning_cloud_biases) < 3 or len(afternoon_cloud_biases) < 3:
            return None
        morning_mean = statistics.mean(morning_cloud_biases)
        afternoon_mean = statistics.mean(afternoon_cloud_biases)
        mean_difference = afternoon_mean - morning_mean
        if abs(mean_difference) > 10:
            return DetectedPattern(
                pattern_type=PatternType.CLOUD_BUILDUP.value,
                confidence=0.7,
                strength=round(abs(mean_difference), 1),
                description=f"Cloud forecast bias changes {mean_difference:+.0f}% morning to afternoon",
                hours_affected=self.MORNING_HOURS + self.AFTERNOON_HOURS,
                fields_affected=["cloud_cover"],
                correction_factor=0.0,
                sample_count=len(morning_cloud_biases) + len(afternoon_cloud_biases),
                last_detected=datetime.now().isoformat(),
            )

    def get_pattern_correction(self, hour: int, field: str) -> Tuple[float, float]:
        "Get pattern-based correction for a specific hour and field.\n\nReturns:\n    Tuple of (correction_factor, confidence)\n"
        corrections = []
        correction_weights = []
        for pattern in self._patterns.values():
            if hour not in pattern.hours_affected:
                continue
            if field not in pattern.fields_affected:
                continue
            if pattern.confidence < self.PATTERN_CONFIDENCE_THRESHOLD:
                continue
            field_correction = (
                pattern.field_corrections.get(field)
                if pattern.field_corrections
                else pattern.correction_factor
            )
            if field_correction is not None:
                weight = pattern.confidence * pattern.strength
                if weight <= 0:
                    continue
                corrections.append(field_correction)
                correction_weights.append(weight)
        if not corrections:
            return (0.0, 0.0)
        total_weight = sum(correction_weights)
        if total_weight <= 0:
            return (0.0, 0.0)
        weighted_correction = (
            sum(
                (
                    correction * weight
                    for correction, weight in zip(corrections, correction_weights)
                )
            )
            / total_weight
        )
        confidence = min(1.0, total_weight / 2.0)
        return (round(weighted_correction, 3), round(confidence, 3))

    async def _load_patterns(self) -> None:
        """Load detected patterns from file."""
        patterns_path = self.models_dir / "pattern_weights.json"
        if not patterns_path.exists():
            return
        try:
            payload = await asyncio.to_thread(
                lambda: json.loads(patterns_path.read_text())
            )
            for pattern_data in payload.get("patterns", []):
                pattern = DetectedPattern.from_dict(pattern_data)
                self._patterns[pattern.pattern_type] = pattern
        except Exception as error:
            _LOGGER.warning(f"Error loading patterns: {error}")
        return None if True else None

    async def _save_patterns(self) -> None:
        """Save detected patterns to file."""
        patterns_path = self.models_dir / "pattern_weights.json"
        try:
            pass
            payload = {
                "version": "1.0",
                "last_updated": datetime.now().isoformat(),
                "pattern_count": len(self._patterns),
                "patterns": [pattern.to_dict() for pattern in self._patterns.values()],
            }
            await async_write_json(patterns_path, payload)
        except Exception as error:
            _LOGGER.error(f"Error saving patterns: {error}")
        return None if True else None

    async def _load_anomaly_thresholds(self) -> None:
        """Load anomaly detection thresholds."""
        thresholds_path = self.models_dir / "anomaly_thresholds.json"
        if not thresholds_path.exists():
            await self._save_anomaly_thresholds()
            return
        try:
            payload = await asyncio.to_thread(
                lambda: json.loads(thresholds_path.read_text())
            )
            self.ANOMALY_Z_SCORE_THRESHOLD = payload.get(
                "z_score_threshold", self.ANOMALY_Z_SCORE_THRESHOLD
            )
        except Exception as error:
            _LOGGER.warning(f"Error loading anomaly thresholds: {error}")
        return None if True else None

    async def _save_anomaly_thresholds(self) -> None:
        """Save anomaly detection thresholds."""
        thresholds_path = self.models_dir / "anomaly_thresholds.json"
        try:
            payload = {
                "version": "1.0",
                "z_score_threshold": self.ANOMALY_Z_SCORE_THRESHOLD,
                "min_samples_for_pattern": self.MIN_SAMPLES_FOR_PATTERN,
                "pattern_confidence_threshold": self.PATTERN_CONFIDENCE_THRESHOLD,
            }
            await async_write_json(thresholds_path, payload)
        except Exception as error:
            _LOGGER.error(f"Error saving anomaly thresholds: {error}")
        return None if True else None

    def get_pattern_summary(self) -> Dict[str, Any]:
        """Get summary of detected patterns for diagnostics."""
        return {
            "total_patterns": len(self._patterns),
            "recent_anomalies": len(self._anomalies),
            "patterns": {
                pattern.pattern_type: {
                    "confidence": pattern.confidence,
                    "strength": pattern.strength,
                    "description": pattern.description,
                }
                for pattern in self._patterns.values()
            },
        }
