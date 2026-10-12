# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""Forecast Corrector - Applies learned corrections to weather forecasts.

This module combines all learning components to correct raw forecasts:
1. Precision Tracker buckets (hourly, cloud type, seasonal)
2. Weather Learner expert weights
3. Pattern Detector corrections
4. Anomaly detection and handling

Correction Pipeline:
    Raw Forecast ──► Expert Blending ──► Bucket Corrections ──► Pattern Adjustments ──► Final Forecast

The corrector applies corrections in a layered approach:
- Base: Raw blended forecast from experts
- Layer 1: Hourly bucket corrections (most specific)
- Layer 2: Cloud type corrections
- Layer 3: Seasonal corrections
- Layer 4: Pattern-based adjustments
- Layer 5: Microclimate offset

@zara
"""
# fmt: on
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .precision_tracker import PrecisionTracker, WeatherField, Season
from .weather_learner import WeatherLearner
from .pattern_detector import PatternDetector
from ..blend_math import wrap_degrees
from ..cloud_type import classify_cloud_type, is_known_cloud_type
from ..const import VISIBILITY_MAX_M, VISIBILITY_MIN_M

_LOGGER = logging.getLogger(__name__)


@dataclass
class CorrectionResult:
    """Result of applying corrections to a forecast."""

    original_value: float
    corrected_value: float
    total_correction: float
    corrections_applied: Dict[str, float]
    confidence: float
    field: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CorrectedForecast:
    """A fully corrected weather forecast."""

    date: str
    hour: int
    cloud_type: str
    season: str
    temperature: Optional[float]
    humidity: Optional[float]
    cloud_cover: Optional[float]
    wind_speed: Optional[float]
    wind_direction: Optional[float]
    pressure: Optional[float]
    precipitation: Optional[float]
    precipitation_probability: Optional[float]
    visibility: Optional[float]
    uv_index: Optional[float]
    weather_code: int
    overall_confidence: float
    corrections_summary: Dict[str, CorrectionResult]
    experts_used: List[str]
    blend_info: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["corrections_summary"] = {
            correction_key: correction_value.to_dict()
            if isinstance(correction_value, CorrectionResult)
            else correction_value
            for correction_key, correction_value in self.corrections_summary.items()
        }
        return result


class ForecastCorrector:
    """Applies learned corrections to weather forecasts."""

    WEIGHT_HOURLY_BUCKET = 0.35
    WEIGHT_CLOUD_TYPE_BUCKET = 0.25
    WEIGHT_COMBINED_BUCKET = 0.2
    WEIGHT_PATTERN = 0.15
    WEIGHT_SEASONAL = 0.05
    MAX_CORRECTION = {
        "temperature": 5.0,
        "humidity": 20.0,
        "cloud_cover": 30.0,
        "wind_speed": 5.0,
        "wind_direction": 45.0,
        "pressure": 10.0,
        "precipitation": 5.0,
        "visibility": 10000.0,
        "uv_index": 2.0,
    }
    MIN_CONFIDENCE_TO_APPLY = 0.3

    def __init__(
        self,
        data_dir: Path,
        precision_tracker: PrecisionTracker,
        weather_learner: WeatherLearner,
        pattern_detector: PatternDetector,
    ):
        # fmt: off
        """Initialize the forecast corrector.

Args:
    data_dir: Base directory for weather_fusion_ai data
    precision_tracker: PrecisionTracker instance
    weather_learner: WeatherLearner instance
    pattern_detector: PatternDetector instance
"""
        # fmt: on
        self.data_dir = data_dir
        self._tracker = precision_tracker
        self._learner = weather_learner
        self._patterns = pattern_detector
        self._corrections_applied = 0
        self._total_correction_magnitude = 0.0
        _LOGGER.info("ForecastCorrector initialized")

    def correct_forecast(
        self,
        raw_forecast: Dict[str, Any],
        hour: int,
        cloud_type: str,
        experts_used: List[str],
        blend_info: Dict[str, Any],
    ) -> CorrectedForecast:
        # fmt: off
        """Apply all corrections to a raw forecast.

Args:
    raw_forecast: Raw blended forecast values
    hour: Hour of day (0-23)
    cloud_type: Detected cloud type
    experts_used: List of experts that contributed
    blend_info: Blending metadata

Returns:
    CorrectedForecast with all corrections applied
"""
        # fmt: on
        now = datetime.now()
        forecast_date = now.strftime("%Y-%m-%d")
        season = self._get_season(now.month)
        corrections_summary = {}
        corrected_values = {}
        for weather_field in WeatherField:
            field_name = weather_field.value
            raw_field_value = raw_forecast.get(field_name)
            if raw_field_value is None:
                corrected_values[field_name] = None
                continue
            result = self._correct_field(
                field_name=field_name,
                raw_value=float(raw_field_value),
                hour=hour,
                cloud_type=cloud_type,
                season=season,
            )
            corrected_values[field_name] = result.corrected_value
            corrections_summary[field_name] = result
        confidences = [
            correction.confidence
            for correction in corrections_summary.values()
            if correction.confidence > 0
        ]
        overall_confidence = sum(confidences) / len(confidences) if confidences else 0.5
        self._corrections_applied += 1
        self._total_correction_magnitude += sum(
            (
                abs(correction.total_correction)
                for correction in corrections_summary.values()
            )
        )
        return CorrectedForecast(
            date=forecast_date,
            hour=hour,
            cloud_type=cloud_type,
            season=season,
            temperature=corrected_values.get("temperature"),
            humidity=corrected_values.get("humidity"),
            cloud_cover=corrected_values.get("cloud_cover"),
            wind_speed=corrected_values.get("wind_speed"),
            wind_direction=corrected_values.get("wind_direction"),
            pressure=corrected_values.get("pressure"),
            precipitation=corrected_values.get("precipitation"),
            precipitation_probability=raw_forecast.get("precipitation_probability"),
            visibility=corrected_values.get("visibility"),
            uv_index=corrected_values.get("uv_index"),
            weather_code=raw_forecast.get("weather_code", 0),
            overall_confidence=round(overall_confidence, 3),
            corrections_summary=corrections_summary,
            experts_used=experts_used,
            blend_info=blend_info,
        )

    def _correct_field(
        self, field_name: str, raw_value: float, hour: int, cloud_type: str, season: str
    ) -> CorrectionResult:
        # fmt: off
        """Apply all correction layers to a single field.

Args:
    field_name: Name of the weather field
    raw_value: Raw forecast value
    hour: Hour of day
    cloud_type: Cloud type classification
    season: Season classification

Returns:
    CorrectionResult with all corrections applied
"""
        # fmt: on
        corrections = {}
        weights = {}
        hourly_stats = self._tracker.get_hourly_stats(hour, field_name)
        if hourly_stats and hourly_stats.count >= 3:
            hourly_correction = -hourly_stats.mean
            confidence = min(1.0, hourly_stats.count / 10)
            corrections["hourly_bucket"] = hourly_correction
            weights["hourly_bucket"] = self.WEIGHT_HOURLY_BUCKET * confidence
        cloud_stats = self._tracker.get_cloud_type_stats(cloud_type, field_name)
        if cloud_stats and cloud_stats.count >= 3:
            cloud_correction = -cloud_stats.mean
            confidence = min(1.0, cloud_stats.count / 10)
            corrections["cloud_type_bucket"] = cloud_correction
            weights["cloud_type_bucket"] = self.WEIGHT_CLOUD_TYPE_BUCKET * confidence
        combined_stats = self._tracker.get_combined_stats(hour, cloud_type, field_name)
        if combined_stats and combined_stats.count >= 3:
            combined_correction = -combined_stats.mean
            confidence = min(1.0, combined_stats.count / 10)
            corrections["combined_bucket"] = combined_correction
            weights["combined_bucket"] = self.WEIGHT_COMBINED_BUCKET * confidence
        seasonal_stats = self._tracker.get_seasonal_stats(season, field_name)
        if seasonal_stats and seasonal_stats.count >= 3:
            seasonal_correction = -seasonal_stats.mean
            confidence = min(1.0, seasonal_stats.count / 20)
            corrections["seasonal"] = seasonal_correction
            weights["seasonal"] = self.WEIGHT_SEASONAL * confidence
        pattern_correction, pattern_confidence = self._patterns.get_pattern_correction(
            hour, field_name
        )
        if pattern_confidence >= self.MIN_CONFIDENCE_TO_APPLY:
            corrections["pattern"] = pattern_correction
            weights["pattern"] = self.WEIGHT_PATTERN * pattern_confidence
        if not corrections:
            return CorrectionResult(
                original_value=raw_value,
                corrected_value=raw_value,
                total_correction=0.0,
                corrections_applied={},
                confidence=0.0,
                field=field_name,
            )
        total_weight = sum(weights.values())
        if total_weight > 0:
            total_correction = (
                sum(
                    (
                        corrections[correction_key] * weights[correction_key]
                        for correction_key in corrections
                    )
                )
                / total_weight
            )
        else:
            total_correction = 0.0
        max_correction = self.MAX_CORRECTION.get(field_name, 10.0)
        total_correction = max(-max_correction, min(max_correction, total_correction))
        if field_name == "wind_direction":
            corrected_value = wrap_degrees(raw_value + total_correction)
        else:
            corrected_value = raw_value + total_correction
        corrected_value = self._apply_field_bounds(field_name, corrected_value)
        confidence = min(1.0, total_weight / 0.5)
        return CorrectionResult(
            original_value=round(raw_value, 2),
            corrected_value=round(corrected_value, 2),
            total_correction=round(total_correction, 3),
            corrections_applied={
                correction_key: round(correction_value, 3)
                for correction_key, correction_value in corrections.items()
            },
            confidence=round(confidence, 3),
            field=field_name,
        )

    def _apply_field_bounds(self, field_name: str, value: float) -> float:
        """Apply physical bounds to corrected values."""
        bounds = {
            "temperature": (-50.0, 60.0),
            "humidity": (0.0, 100.0),
            "cloud_cover": (0.0, 100.0),
            "wind_speed": (0.0, 100.0),
            "wind_direction": (0.0, 360.0),
            "pressure": (900.0, 1100.0),
            "precipitation": (0.0, 100.0),
            "visibility": (VISIBILITY_MIN_M, VISIBILITY_MAX_M),
            "uv_index": (0.0, 15.0),
        }
        if field_name in bounds:
            minimum, maximum = bounds[field_name]
            return max(minimum, min(maximum, value))
        return value

    def _get_season(self, month: int) -> str:
        """Get season from month."""
        if month in (3, 4, 5):
            return Season.SPRING.value
        if month in (6, 7, 8):
            return Season.SUMMER.value
        if month in (9, 10, 11):
            return Season.AUTUMN.value
        return Season.WINTER.value

    def get_correction_stats(self) -> Dict[str, Any]:
        """Get correction statistics for diagnostics."""
        average_magnitude = (
            self._total_correction_magnitude / self._corrections_applied
            if self._corrections_applied > 0
            else 0.0
        )
        return {
            "corrections_applied": self._corrections_applied,
            "avg_correction_magnitude": round(average_magnitude, 3),
            "max_corrections": self.MAX_CORRECTION,
            "weights": {
                "hourly_bucket": self.WEIGHT_HOURLY_BUCKET,
                "cloud_type_bucket": self.WEIGHT_CLOUD_TYPE_BUCKET,
                "combined_bucket": self.WEIGHT_COMBINED_BUCKET,
                "pattern": self.WEIGHT_PATTERN,
                "seasonal": self.WEIGHT_SEASONAL,
            },
        }


class ForecastCorrectionPipeline:
    """Complete pipeline for forecast correction with all AI components."""

    def __init__(self, data_dir: Path):
        # fmt: off
        """Initialize the correction pipeline.

Args:
    data_dir: Base directory for weather_fusion_ai data
              (e.g., /config/weather_fusion_ai)
"""
        # fmt: on
        self.data_dir = data_dir
        self._tracker = None
        self._learner = None
        self._patterns = None
        self._corrector = None
        self._initialized = False
        _LOGGER.info(f"ForecastCorrectionPipeline created for {data_dir}")

    async def async_init(self) -> bool:
        """Initialize all AI components."""
        try:
            (self.data_dir / "learning").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "history").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "models").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "cache").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
            self._tracker = PrecisionTracker(self.data_dir)
            await self._tracker.async_init()
            self._learner = WeatherLearner(self.data_dir, self._tracker)
            await self._learner.async_init()
            self._patterns = PatternDetector(self.data_dir, self._tracker)
            await self._patterns.async_init()
            self._corrector = ForecastCorrector(
                self.data_dir, self._tracker, self._learner, self._patterns
            )
            self._initialized = True
            _LOGGER.info("ForecastCorrectionPipeline initialized successfully")
        except Exception as error:
            _LOGGER.error(f"Error initializing correction pipeline: {error}")
            return False
        return True

    async def process_forecast(
        self,
        raw_forecast: Dict[str, Any],
        hour: int,
        experts_used: List[str],
        blend_info: Dict[str, Any],
    ) -> CorrectedForecast:
        # fmt: off
        """Process and correct a raw forecast.

Args:
    raw_forecast: Raw blended forecast from experts
    hour: Hour of day
    experts_used: List of contributing experts
    blend_info: Blending metadata

Returns:
    Corrected forecast
"""
        # fmt: on
        if not self._initialized:
            raise RuntimeError("Pipeline not initialized. Call async_init() first.")
        resolved_cloud_type = blend_info.get("cloud_type") or raw_forecast.get(
            "cloud_type"
        )
        if not is_known_cloud_type(resolved_cloud_type):
            resolved_cloud_type = classify_cloud_type(
                cloud_low=raw_forecast.get("cloud_cover_low"),
                cloud_mid=raw_forecast.get("cloud_cover_mid"),
                cloud_high=raw_forecast.get("cloud_cover_high"),
                cloud_total=raw_forecast.get("cloud_cover", 50),
            )
        corrected_forecast = self._corrector.correct_forecast(
            raw_forecast=raw_forecast,
            hour=hour,
            cloud_type=resolved_cloud_type,
            experts_used=experts_used,
            blend_info=blend_info,
        )
        return corrected_forecast

    async def record_actual(
        self,
        actual: Dict[str, Any],
        forecast: Dict[str, Any],
        expert: str,
        cloud_type: Optional[str] = None,
    ) -> None:
        """Record actual weather for learning."""
        if not self._initialized:
            return
        if not is_known_cloud_type(cloud_type):
            cloud_type = classify_cloud_type(
                cloud_total=actual.get("cloud_cover") or forecast.get("cloud_cover")
            )
        await self._tracker.record_deviation(
            forecast=forecast, actual=actual, expert=expert, cloud_type=cloud_type
        )

    def apply_weights_to_blender(self, blender: Any) -> None:
        """Copy learned weights into the blender's in-memory table."""
        if blender is None or self._learner is None:
            return None
        blender.apply_learned_weights(self._learner.get_all_weights())

    async def run_learning_cycle(self) -> Dict[str, Any]:
        """Run a complete learning cycle."""
        if not self._initialized:
            return {"error": "Pipeline not initialized"}
        results = {}
        weight_learning = await self._learner.learn()
        results["weight_learning"] = weight_learning
        patterns = await self._patterns.detect_patterns()
        results["patterns_detected"] = len(patterns)
        await self._tracker.prune_deviation_history()
        await self._tracker.rebuild_from_history()
        await self._tracker.save_all()
        return results

    async def shutdown(self) -> None:
        """Shutdown and save all data."""
        if self._tracker:
            await self._tracker.save_all()
        _LOGGER.info("ForecastCorrectionPipeline shutdown complete")

    def get_pipeline_stats(self) -> Dict[str, Any]:
        """Get pipeline statistics for diagnostics."""
        stats = {"initialized": self._initialized}
        if self._tracker:
            stats["tracker"] = self._tracker.get_stats_summary()
        if self._learner:
            stats["learner"] = self._learner.get_learning_stats()
        if self._patterns:
            stats["patterns"] = self._patterns.get_pattern_summary()
        if self._corrector:
            stats["corrector"] = self._corrector.get_correction_stats()
        return stats

    @property
    def tracker(self) -> Optional[PrecisionTracker]:
        return self._tracker

    @property
    def learner(self) -> Optional[WeatherLearner]:
        return self._learner

    @property
    def patterns(self) -> Optional[PatternDetector]:
        return self._patterns

    @property
    def corrector(self) -> Optional[ForecastCorrector]:
        return self._corrector
