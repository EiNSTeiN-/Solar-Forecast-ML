# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Shadow Pattern Learning for Solar Forecast ML V16.2.0.
Self-learning system that detects and learns recurring shadow patterns.

Learns:
1. Hourly patterns - Which hours regularly have shadows?
2. Seasonal patterns - How do shadows change throughout the year?
3. Pattern classification - Fixed obstruction vs variable (clouds)?

@zara
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)
ROLLING_WINDOW_DAYS = 30
MIN_SAMPLES_FOR_PATTERN = 7
MIN_SAMPLES_FOR_SEASONAL = 3
EMA_ALPHA = 0.15
FIXED_OBSTRUCTION_THRESHOLD = 0.7
FREQUENT_SHADOW_THRESHOLD = 0.4
OCCASIONAL_SHADOW_THRESHOLD = 0.15
SHADOW_DETECTION_THRESHOLD = 0.05
MODERATE_SHADOW_THRESHOLD = 0.15
HEAVY_SHADOW_THRESHOLD = 0.3


class PatternType(Enum):
    """Classification of shadow patterns. @zara"""

    NO_SHADOW = "no_shadow"
    OCCASIONAL = "occasional"
    FREQUENT = "frequent"
    FIXED_OBSTRUCTION = "fixed_obstruction"
    UNKNOWN = "unknown"


@dataclass
class HourlyPattern:
    """Learned pattern for a specific hour. @zara"""

    group_name: str = "_system_"
    hour: int = 0
    shadow_occurrence_rate: float = 0.0
    avg_shadow_percent: float = 0.0
    std_dev_shadow_percent: float = 0.0
    pct_weather_clouds: float = 0.0
    pct_building_tree: float = 0.0
    pct_low_sun: float = 0.0
    pct_other: float = 0.0
    pattern_type: PatternType = PatternType.UNKNOWN
    confidence: float = 0.0
    sample_count: int = 0
    shadow_days: int = 0
    clear_days: int = 0


@dataclass
class SeasonalPattern:
    """Learned pattern for a specific month/hour combination. @zara"""

    group_name: str = "_system_"
    month: int = 0
    hour: int = 0
    shadow_occurrence_rate: float = 0.0
    avg_shadow_percent: float = 0.0
    std_dev_shadow_percent: float = 0.0
    dominant_cause: str = "unknown"
    sample_count: int = 0
    shadow_days: int = 0
    confidence: float = 0.0


@dataclass
class LearningResult:
    """Result of a learning session. @zara"""

    success: bool
    message: str
    hours_learned: int = 0
    patterns_updated: int = 0
    fixed_obstructions_found: int = 0
    date: Optional[str] = None


class ShadowPatternLearner(DataManagerIO):
    """Self-learning system for shadow detection patterns. @zara

    Analyzes historical shadow detection results to learn:
    - Which hours typically have shadows
    - Whether shadows are from fixed obstructions or variable sources
    - Seasonal variations in shadow patterns

    Uses EMA (Exponential Moving Average) for smooth updates.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize shadow pattern learner. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager instance for DB operations\n"
        super().__init__(hass, db_manager)
        self._hourly_patterns = {}
        self._seasonal_patterns = {}
        self._config = {}
        self._initialized = False
        _LOGGER.info("ShadowPatternLearner initialized")

    async def async_init(self) -> bool:
        """Async initialization - load existing patterns from database. @zara"""
        try:
            await self._load_config()
            await self._load_hourly_patterns()
            await self._load_seasonal_patterns()
            self._initialized = True
            detected_pattern_count = len(
                [
                    learned_pattern
                    for learned_pattern in self._hourly_patterns.values()
                    if learned_pattern.pattern_type != PatternType.UNKNOWN
                ]
            )
            fixed_obstruction_count = len(
                [
                    learned_pattern
                    for learned_pattern in self._hourly_patterns.values()
                    if learned_pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
                ]
            )
            _LOGGER.info(
                "ShadowPatternLearner loaded: %d hourly patterns, %d seasonal patterns, %d fixed obstructions detected",
                detected_pattern_count,
                len(self._seasonal_patterns),
                fixed_obstruction_count,
            )
        except Exception as error:
            _LOGGER.error("ShadowPatternLearner initialization failed: %s", error)
            return False
        return True

    async def _load_config(self) -> None:
        """Load learning config from database. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT * FROM shadow_pattern_config WHERE id = 1"
            )
            if database_row:
                self._config = {
                    "rolling_window_days": database_row[1] or ROLLING_WINDOW_DAYS,
                    "min_samples_for_pattern": database_row[2]
                    or MIN_SAMPLES_FOR_PATTERN,
                    "ema_alpha": database_row[3] or EMA_ALPHA,
                    "fixed_obstruction_threshold": database_row[4]
                    or FIXED_OBSTRUCTION_THRESHOLD,
                    "total_days_learned": database_row[5] or 0,
                    "total_hours_learned": database_row[6] or 0,
                    "last_learning_date": database_row[7],
                    "patterns_detected": database_row[8] or 0,
                    "fixed_obstructions_detected": database_row[9] or 0,
                }
            else:
                self._config = {
                    "rolling_window_days": ROLLING_WINDOW_DAYS,
                    "min_samples_for_pattern": MIN_SAMPLES_FOR_PATTERN,
                    "ema_alpha": EMA_ALPHA,
                    "fixed_obstruction_threshold": FIXED_OBSTRUCTION_THRESHOLD,
                    "total_days_learned": 0,
                    "total_hours_learned": 0,
                    "last_learning_date": None,
                    "patterns_detected": 0,
                    "fixed_obstructions_detected": 0,
                }
        except Exception as error:
            _LOGGER.warning("Could not load shadow pattern config: %s", error)
            self._config = {}
        return None if True else None

    async def _load_hourly_patterns(self) -> None:
        """Load hourly patterns from database. @zara V17.0.0: group_name dimension"""
        try:
            database_rows = await self.fetch_all(
                "SELECT * FROM shadow_pattern_hourly ORDER BY group_name, hour"
            )
            for database_row in database_rows:
                panel_group_name = database_row[1] or "_system_"
                learned_hour = database_row[2]
                pattern_key = (panel_group_name, learned_hour)
                self._hourly_patterns[pattern_key] = HourlyPattern(
                    group_name=panel_group_name,
                    hour=learned_hour,
                    shadow_occurrence_rate=database_row[3] or 0.0,
                    avg_shadow_percent=database_row[4] or 0.0,
                    std_dev_shadow_percent=database_row[5] or 0.0,
                    pct_weather_clouds=database_row[6] or 0.0,
                    pct_building_tree=database_row[7] or 0.0,
                    pct_low_sun=database_row[8] or 0.0,
                    pct_other=database_row[9] or 0.0,
                    pattern_type=(
                        PatternType(database_row[10])
                        if database_row[10]
                        else PatternType.UNKNOWN
                    ),
                    confidence=database_row[11] or 0.0,
                    sample_count=database_row[12] or 0,
                    shadow_days=database_row[13] or 0,
                    clear_days=database_row[14] or 0,
                )
                continue
        except Exception as error:
            _LOGGER.warning("Could not load hourly patterns: %s", error)
        return None if True else None

    async def _load_seasonal_patterns(self) -> None:
        """Load seasonal patterns from database. @zara V17.0.0: group_name dimension"""
        try:
            database_rows = await self.fetch_all(
                "SELECT * FROM shadow_pattern_seasonal ORDER BY group_name, month, hour"
            )
            for database_row in database_rows:
                panel_group_name = database_row[1] or "_system_"
                learned_month = database_row[2]
                learned_hour = database_row[3]
                pattern_key = (panel_group_name, learned_month, learned_hour)
                self._seasonal_patterns[pattern_key] = SeasonalPattern(
                    group_name=panel_group_name,
                    month=learned_month,
                    hour=learned_hour,
                    shadow_occurrence_rate=database_row[4] or 0.0,
                    avg_shadow_percent=database_row[5] or 0.0,
                    std_dev_shadow_percent=database_row[6] or 0.0,
                    dominant_cause=database_row[7] or "unknown",
                    sample_count=database_row[8] or 0,
                    shadow_days=database_row[9] or 0,
                    confidence=database_row[10] or 0.0,
                )
        except Exception as error:
            _LOGGER.warning("Could not load seasonal patterns: %s", error)
        return None if True else None

    async def learn_from_day(
        self, date_str: str, group_name: str = "_system_"
    ) -> LearningResult:
        """Learn shadow patterns from a day's shadow detection data. @zara V17.0.0

        This is the main learning entry point, called from EOD workflow.

        Args:
            date_str: Date to learn from (YYYY-MM-DD)
            group_name: Panel group name ('_system_' for system-wide)

        Returns:
            LearningResult with success status and details
        """
        try:
            daily_shadow_data = await self._get_shadow_data_for_date(
                date_str, group_name
            )
            if not daily_shadow_data:
                _LOGGER.debug(
                    "No shadow data for %s/%s - skipping learning", date_str, group_name
                )
                return LearningResult(
                    success=False,
                    message="No shadow detection data available",
                    date=date_str,
                )
            learning_date = datetime.strptime(date_str, "%Y-%m-%d")
            learned_month = learning_date.month
            learned_hours = 0
            updated_patterns = 0
            for hour_shadow_data in daily_shadow_data:
                learned_hour = hour_shadow_data.get("hour")
                if learned_hour is None:
                    continue
                await self._save_learning_history(
                    date_str, hour_shadow_data, group_name
                )
                pattern_changed = await self._update_hourly_pattern(
                    learned_hour, hour_shadow_data, group_name
                )
                if pattern_changed:
                    updated_patterns += 1
                await self._update_seasonal_pattern(
                    learned_month, learned_hour, hour_shadow_data, group_name
                )
                learned_hours += 1
            fixed_obstructions = await self._classify_all_patterns(group_name)
            await self._update_config_stats(date_str, learned_hours, fixed_obstructions)
            _LOGGER.info(
                "Shadow pattern learning for %s [%s]: %d hours, %d patterns updated, %d fixed obstructions",
                date_str,
                group_name,
                learned_hours,
                updated_patterns,
                fixed_obstructions,
            )
            return LearningResult(
                success=True,
                message=f"Learned from {learned_hours} hours",
                hours_learned=learned_hours,
                patterns_updated=updated_patterns,
                fixed_obstructions_found=fixed_obstructions,
                date=date_str,
            )
        except Exception as error:
            _LOGGER.error("Shadow pattern learning failed for %s: %s", date_str, error)
            return LearningResult(success=False, message=str(error), date=date_str)

    async def _get_shadow_data_for_date(
        self, date_str: str, group_name: str = "_system_"
    ) -> List[Dict[str, Any]]:
        """Get shadow detection data for a specific date. @zara V17.0.0"""
        try:
            if group_name != "_system_":
                database_rows = await self.fetch_all(
                    "SELECT hp.target_hour, sdg.shadow_type, sdg.shadow_percent,\n                              sdg.root_cause, sdg.confidence, sdg.efficiency_ratio,\n                              sdg.theoretical_max_kwh, sdg.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover\n                       FROM hourly_predictions hp\n                       JOIN shadow_detection_groups sdg ON hp.prediction_id = sdg.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND sdg.group_name = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                         AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                       ORDER BY hp.target_hour",
                    (date_str, group_name),
                )
            else:
                database_rows = await self.fetch_all(
                    "SELECT hp.target_hour, hsd.shadow_type, hsd.shadow_percent,\n                              hsd.root_cause, hsd.confidence, hsd.efficiency_ratio,\n                              hsd.theoretical_max_kwh, hp.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover\n                       FROM hourly_predictions hp\n                       JOIN hourly_shadow_detection hsd ON hp.prediction_id = hsd.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                         AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                       ORDER BY hp.target_hour",
                    (date_str,),
                )
            shadow_data = []
            for database_row in database_rows:
                shadow_data.append(
                    {
                        "hour": database_row[0],
                        "shadow_type": database_row[1],
                        "shadow_percent": database_row[2] or 0.0,
                        "root_cause": database_row[3],
                        "confidence": database_row[4] or 0.0,
                        "efficiency_ratio": database_row[5] or 0.0,
                        "theoretical_max_kwh": database_row[6] or 0.0,
                        "actual_kwh": database_row[7] or 0.0,
                        "sun_elevation_deg": database_row[8],
                        "cloud_cover_percent": database_row[9],
                    }
                )
            return shadow_data
        except Exception as error:
            _LOGGER.error(
                "Failed to get shadow data for %s [%s]: %s", date_str, group_name, error
            )
            return []

    async def _save_learning_history(
        self, date_str: str, hour_data: Dict[str, Any], group_name: str = "_system_"
    ) -> None:
        """Save hour data to learning history. @zara V17.0.0: group_name"""
        try:
            learned_hour = hour_data["hour"]
            shadow_percent = hour_data.get("shadow_percent", 0.0)
            shadow_detected = shadow_percent >= SHADOW_DETECTION_THRESHOLD * 100
            await self.execute_query(
                "INSERT INTO shadow_learning_history\n                   (group_name, date, hour, shadow_detected, shadow_type, shadow_percent,\n                    root_cause, confidence, sun_elevation_deg, cloud_cover_percent,\n                    theoretical_max_kwh, actual_kwh, efficiency_ratio)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, date, hour) DO UPDATE SET\n                       shadow_detected = excluded.shadow_detected,\n                       shadow_type = excluded.shadow_type,\n                       shadow_percent = excluded.shadow_percent,\n                       root_cause = excluded.root_cause,\n                       confidence = excluded.confidence,\n                       sun_elevation_deg = excluded.sun_elevation_deg,\n                       cloud_cover_percent = excluded.cloud_cover_percent,\n                       theoretical_max_kwh = excluded.theoretical_max_kwh,\n                       actual_kwh = excluded.actual_kwh,\n                       efficiency_ratio = excluded.efficiency_ratio",
                (
                    group_name,
                    date_str,
                    learned_hour,
                    shadow_detected,
                    hour_data.get("shadow_type"),
                    shadow_percent,
                    hour_data.get("root_cause"),
                    hour_data.get("confidence"),
                    hour_data.get("sun_elevation_deg"),
                    hour_data.get("cloud_cover_percent"),
                    hour_data.get("theoretical_max_kwh"),
                    hour_data.get("actual_kwh"),
                    hour_data.get("efficiency_ratio"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save learning history: %s", error)
        return None if True else None

    async def _update_hourly_pattern(
        self, hour: int, hour_data: Dict[str, Any], group_name: str = "_system_"
    ) -> bool:
        """Update hourly pattern with new data using EMA. @zara V17.0.0

        Returns True if pattern classification changed.
        """
        try:
            shadow_percent = hour_data.get("shadow_percent", 0.0)
            shadow_detected = shadow_percent >= SHADOW_DETECTION_THRESHOLD * 100
            root_cause = hour_data.get("root_cause", "unknown")
            pattern_key = (group_name, hour)
            if pattern_key not in self._hourly_patterns:
                self._hourly_patterns[pattern_key] = HourlyPattern(
                    group_name=group_name, hour=hour
                )
            learned_pattern = self._hourly_patterns[pattern_key]
            previous_pattern_type = learned_pattern.pattern_type
            ema_alpha = self._config.get("ema_alpha", EMA_ALPHA)
            learned_pattern.sample_count += 1
            if shadow_detected:
                learned_pattern.shadow_days += 1
            else:
                learned_pattern.clear_days += 1
            observed_occurrence_rate = (
                learned_pattern.shadow_days / learned_pattern.sample_count
            )
            if learned_pattern.sample_count >= 3:
                learned_pattern.shadow_occurrence_rate = (
                    ema_alpha * observed_occurrence_rate
                    + (1 - ema_alpha) * learned_pattern.shadow_occurrence_rate
                )
            else:
                learned_pattern.shadow_occurrence_rate = observed_occurrence_rate
            if shadow_detected:
                if learned_pattern.shadow_days == 1:
                    learned_pattern.avg_shadow_percent = shadow_percent
                    learned_pattern.std_dev_shadow_percent = 0.0
                else:
                    previous_shadow_mean = learned_pattern.avg_shadow_percent
                    learned_pattern.avg_shadow_percent = (
                        ema_alpha * shadow_percent
                        + (1 - ema_alpha) * previous_shadow_mean
                    )
                    shadow_mean_delta = shadow_percent - previous_shadow_mean
                    learned_pattern.std_dev_shadow_percent = math.sqrt(
                        (1 - ema_alpha)
                        * (
                            learned_pattern.std_dev_shadow_percent**2
                            + ema_alpha * shadow_mean_delta**2
                        )
                    )
            if shadow_detected:
                new_cause_weight = (
                    1.0 / learned_pattern.shadow_days
                    if learned_pattern.shadow_days > 0
                    else 0
                )
                retained_cause_weight = 1.0 - new_cause_weight
                learned_pattern.pct_weather_clouds *= retained_cause_weight
                learned_pattern.pct_building_tree *= retained_cause_weight
                learned_pattern.pct_low_sun *= retained_cause_weight
                learned_pattern.pct_other *= retained_cause_weight
                if root_cause == "weather_clouds":
                    learned_pattern.pct_weather_clouds += new_cause_weight
                elif root_cause == "building_tree_obstruction":
                    learned_pattern.pct_building_tree += new_cause_weight
                elif root_cause == "low_sun_angle":
                    learned_pattern.pct_low_sun += new_cause_weight
                else:
                    learned_pattern.pct_other += new_cause_weight
                cause_weight_sum = (
                    learned_pattern.pct_weather_clouds
                    + learned_pattern.pct_building_tree
                    + learned_pattern.pct_low_sun
                    + learned_pattern.pct_other
                )
                if cause_weight_sum > 0:
                    learned_pattern.pct_weather_clouds /= cause_weight_sum
                    learned_pattern.pct_building_tree /= cause_weight_sum
                    learned_pattern.pct_low_sun /= cause_weight_sum
                    learned_pattern.pct_other /= cause_weight_sum
            minimum_samples = self._config.get(
                "min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN
            )
            learned_pattern.confidence = min(
                1.0, learned_pattern.sample_count / (minimum_samples * 2)
            )
            await self._save_hourly_pattern(learned_pattern)
            return learned_pattern.pattern_type != previous_pattern_type
        except Exception as error:
            _LOGGER.error(
                "Failed to update hourly pattern for hour %d: %s", hour, error
            )
            return False

    async def _save_hourly_pattern(self, pattern: HourlyPattern) -> None:
        """Save hourly pattern to database. @zara V17.0.0: group_name"""
        try:
            await self.execute_query(
                "INSERT INTO shadow_pattern_hourly\n                   (group_name, hour, shadow_occurrence_rate, avg_shadow_percent,\n                    std_dev_shadow_percent, pct_weather_clouds, pct_building_tree,\n                    pct_low_sun, pct_other, pattern_type, confidence, sample_count,\n                    shadow_days, clear_days, first_learned, last_updated)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, hour) DO UPDATE SET\n                       shadow_occurrence_rate = excluded.shadow_occurrence_rate,\n                       avg_shadow_percent = excluded.avg_shadow_percent,\n                       std_dev_shadow_percent = excluded.std_dev_shadow_percent,\n                       pct_weather_clouds = excluded.pct_weather_clouds,\n                       pct_building_tree = excluded.pct_building_tree,\n                       pct_low_sun = excluded.pct_low_sun,\n                       pct_other = excluded.pct_other,\n                       pattern_type = excluded.pattern_type,\n                       confidence = excluded.confidence,\n                       sample_count = excluded.sample_count,\n                       shadow_days = excluded.shadow_days,\n                       clear_days = excluded.clear_days,\n                       last_updated = excluded.last_updated",
                (
                    pattern.group_name,
                    pattern.hour,
                    round(pattern.shadow_occurrence_rate, 4),
                    round(pattern.avg_shadow_percent, 2),
                    round(pattern.std_dev_shadow_percent, 2),
                    round(pattern.pct_weather_clouds, 4),
                    round(pattern.pct_building_tree, 4),
                    round(pattern.pct_low_sun, 4),
                    round(pattern.pct_other, 4),
                    pattern.pattern_type.value,
                    round(pattern.confidence, 3),
                    pattern.sample_count,
                    pattern.shadow_days,
                    pattern.clear_days,
                    datetime.now().date() if pattern.sample_count == 1 else None,
                    datetime.now(),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save hourly pattern: %s", error)
        return None if True else None

    async def _update_seasonal_pattern(
        self,
        month: int,
        hour: int,
        hour_data: Dict[str, Any],
        group_name: str = "_system_",
    ) -> None:
        """Update seasonal pattern with new data. @zara V17.0.0: group_name"""
        try:
            shadow_percent = hour_data.get("shadow_percent", 0.0)
            shadow_detected = shadow_percent >= SHADOW_DETECTION_THRESHOLD * 100
            root_cause = hour_data.get("root_cause", "unknown")
            pattern_key = (group_name, month, hour)
            ema_alpha = self._config.get("ema_alpha", EMA_ALPHA)
            if pattern_key not in self._seasonal_patterns:
                self._seasonal_patterns[pattern_key] = SeasonalPattern(
                    group_name=group_name, month=month, hour=hour
                )
            learned_pattern = self._seasonal_patterns[pattern_key]
            learned_pattern.sample_count += 1
            if shadow_detected:
                learned_pattern.shadow_days += 1
            observed_occurrence_rate = (
                learned_pattern.shadow_days / learned_pattern.sample_count
            )
            if learned_pattern.sample_count >= 2:
                learned_pattern.shadow_occurrence_rate = (
                    ema_alpha * observed_occurrence_rate
                    + (1 - ema_alpha) * learned_pattern.shadow_occurrence_rate
                )
            else:
                learned_pattern.shadow_occurrence_rate = observed_occurrence_rate
            if shadow_detected:
                if learned_pattern.shadow_days == 1:
                    learned_pattern.avg_shadow_percent = shadow_percent
                else:
                    learned_pattern.avg_shadow_percent = (
                        ema_alpha * shadow_percent
                        + (1 - ema_alpha) * learned_pattern.avg_shadow_percent
                    )
            if shadow_detected and root_cause != "unknown":
                learned_pattern.dominant_cause = root_cause
            learned_pattern.confidence = min(
                1.0, learned_pattern.sample_count / (MIN_SAMPLES_FOR_SEASONAL * 2)
            )
            await self._save_seasonal_pattern(learned_pattern)
        except Exception as error:
            _LOGGER.error(
                "Failed to update seasonal pattern for %d/%d: %s", month, hour, error
            )
        return None if True else None

    async def _save_seasonal_pattern(self, pattern: SeasonalPattern) -> None:
        """Save seasonal pattern to database. @zara V17.0.0: group_name"""
        try:
            await self.execute_query(
                "INSERT INTO shadow_pattern_seasonal\n                   (group_name, month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                    std_dev_shadow_percent, dominant_cause, sample_count,\n                    shadow_days, confidence, last_updated)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, month, hour) DO UPDATE SET\n                       shadow_occurrence_rate = excluded.shadow_occurrence_rate,\n                       avg_shadow_percent = excluded.avg_shadow_percent,\n                       std_dev_shadow_percent = excluded.std_dev_shadow_percent,\n                       dominant_cause = excluded.dominant_cause,\n                       sample_count = excluded.sample_count,\n                       shadow_days = excluded.shadow_days,\n                       confidence = excluded.confidence,\n                       last_updated = excluded.last_updated",
                (
                    pattern.group_name,
                    pattern.month,
                    pattern.hour,
                    round(pattern.shadow_occurrence_rate, 4),
                    round(pattern.avg_shadow_percent, 2),
                    round(pattern.std_dev_shadow_percent, 2),
                    pattern.dominant_cause,
                    pattern.sample_count,
                    pattern.shadow_days,
                    round(pattern.confidence, 3),
                    datetime.now(),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save seasonal pattern: %s", error)
        return None if True else None

    async def _classify_all_patterns(self, group_name: Optional[str] = None) -> int:
        """Classify hourly patterns and return count of fixed obstructions. @zara V17.0.0"""
        fixed_obstruction_count = 0
        minimum_samples = self._config.get(
            "min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN
        )
        obstruction_threshold = self._config.get(
            "fixed_obstruction_threshold", FIXED_OBSTRUCTION_THRESHOLD
        )
        for pattern_key, learned_pattern in self._hourly_patterns.items():
            if group_name is not None:
                if learned_pattern.group_name != group_name:
                    continue
            if learned_pattern.sample_count < minimum_samples:
                learned_pattern.pattern_type = PatternType.UNKNOWN
                continue
            occurrence_rate = learned_pattern.shadow_occurrence_rate
            if occurrence_rate >= obstruction_threshold:
                if learned_pattern.std_dev_shadow_percent < 15.0:
                    learned_pattern.pattern_type = PatternType.FIXED_OBSTRUCTION
                    fixed_obstruction_count += 1
                else:
                    learned_pattern.pattern_type = PatternType.FREQUENT
            elif occurrence_rate >= FREQUENT_SHADOW_THRESHOLD:
                learned_pattern.pattern_type = PatternType.FREQUENT
            elif occurrence_rate >= OCCASIONAL_SHADOW_THRESHOLD:
                learned_pattern.pattern_type = PatternType.OCCASIONAL
            else:
                learned_pattern.pattern_type = PatternType.NO_SHADOW
            await self._save_hourly_pattern(learned_pattern)
            continue
        return fixed_obstruction_count

    async def _update_config_stats(
        self, date_str: str, hours_learned: int, fixed_obstructions: int
    ) -> None:
        """Update config statistics. @zara"""
        try:
            self._config["total_days_learned"] = (
                self._config.get("total_days_learned", 0) + 1
            )
            self._config["total_hours_learned"] = (
                self._config.get("total_hours_learned", 0) + hours_learned
            )
            self._config["last_learning_date"] = date_str
            self._config["fixed_obstructions_detected"] = fixed_obstructions
            detected_pattern_count = len(
                [
                    learned_pattern
                    for learned_pattern in self._hourly_patterns.values()
                    if learned_pattern.pattern_type != PatternType.UNKNOWN
                ]
            )
            self._config["patterns_detected"] = detected_pattern_count
            await self.execute_query(
                "INSERT INTO shadow_pattern_config\n                   (id, rolling_window_days, min_samples_for_pattern, ema_alpha,\n                    fixed_obstruction_threshold, total_days_learned, total_hours_learned,\n                    last_learning_date, patterns_detected, fixed_obstructions_detected,\n                    updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       total_days_learned = excluded.total_days_learned,\n                       total_hours_learned = excluded.total_hours_learned,\n                       last_learning_date = excluded.last_learning_date,\n                       patterns_detected = excluded.patterns_detected,\n                       fixed_obstructions_detected = excluded.fixed_obstructions_detected,\n                       updated_at = excluded.updated_at",
                (
                    self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS),
                    self._config.get(
                        "min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN
                    ),
                    self._config.get("ema_alpha", EMA_ALPHA),
                    self._config.get(
                        "fixed_obstruction_threshold", FIXED_OBSTRUCTION_THRESHOLD
                    ),
                    self._config["total_days_learned"],
                    self._config["total_hours_learned"],
                    date_str,
                    detected_pattern_count,
                    fixed_obstructions,
                    datetime.now(),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to update config stats: %s", error)
        return None if True else None

    def get_hourly_pattern(
        self, hour: int, group_name: str = "_system_"
    ) -> Optional[HourlyPattern]:
        "Get learned pattern for a specific hour. @zara V17.0.0\n\nUsed by shadow detection to adjust confidence or add context.\n\nArgs:\n    hour: Hour (0-23)\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    HourlyPattern if available, None otherwise\n"
        return self._hourly_patterns.get((group_name, hour))

    def get_seasonal_pattern(
        self, month: int, hour: int, group_name: str = "_system_"
    ) -> Optional[SeasonalPattern]:
        "Get seasonal pattern for a specific month/hour. @zara V17.0.0\n\nArgs:\n    month: Month (1-12)\n    hour: Hour (0-23)\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    SeasonalPattern if available, None otherwise\n"
        return self._seasonal_patterns.get((group_name, month, hour))

    def get_expected_shadow(
        self, hour: int, month: Optional[int] = None, group_name: str = "_system_"
    ) -> Dict[str, Any]:
        "Get expected shadow for an hour based on learned patterns. @zara V17.0.0\n\nThis is the main interface for shadow detection to use learned patterns.\n\nArgs:\n    hour: Hour to check\n    month: Optional month for seasonal adjustment\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    Dictionary with expected shadow info\n"
        expected_shadow = {
            "hour": hour,
            "has_pattern": False,
            "expected_shadow": False,
            "expected_shadow_percent": 0.0,
            "pattern_type": "unknown",
            "is_fixed_obstruction": False,
            "dominant_cause": "unknown",
            "confidence": 0.0,
        }
        hourly_pattern = self._hourly_patterns.get((group_name, hour))
        if not hourly_pattern or hourly_pattern.pattern_type == PatternType.UNKNOWN:
            return expected_shadow
        expected_shadow["has_pattern"] = True
        expected_shadow["pattern_type"] = hourly_pattern.pattern_type.value
        expected_shadow["expected_shadow"] = (
            hourly_pattern.shadow_occurrence_rate >= OCCASIONAL_SHADOW_THRESHOLD
        )
        expected_shadow["expected_shadow_percent"] = hourly_pattern.avg_shadow_percent
        expected_shadow["is_fixed_obstruction"] = (
            hourly_pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        )
        expected_shadow["confidence"] = hourly_pattern.confidence
        cause_weights = {
            "weather_clouds": hourly_pattern.pct_weather_clouds,
            "building_tree_obstruction": hourly_pattern.pct_building_tree,
            "low_sun_angle": hourly_pattern.pct_low_sun,
            "other": hourly_pattern.pct_other,
        }
        expected_shadow["dominant_cause"] = max(cause_weights, key=cause_weights.get)
        if month:
            seasonal_pattern = self._seasonal_patterns.get((group_name, month, hour))
            if seasonal_pattern and seasonal_pattern.confidence > 0.3:
                seasonal_blend_weight = min(0.4, seasonal_pattern.confidence * 0.5)
                expected_shadow["expected_shadow_percent"] = (
                    (1 - seasonal_blend_weight) * hourly_pattern.avg_shadow_percent
                    + seasonal_blend_weight * seasonal_pattern.avg_shadow_percent
                )
                if seasonal_pattern.dominant_cause != "unknown":
                    expected_shadow["dominant_cause"] = seasonal_pattern.dominant_cause
        return expected_shadow

    def is_fixed_obstruction_hour(
        self, hour: int, group_name: str = "_system_"
    ) -> bool:
        "Check if an hour is classified as having a fixed obstruction. @zara V17.0.0\n\nArgs:\n    hour: Hour to check\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    True if hour has a learned fixed obstruction pattern\n"
        learned_pattern = self._hourly_patterns.get((group_name, hour))
        return (
            learned_pattern is not None
            and learned_pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        )

    def get_all_fixed_obstruction_hours(
        self, group_name: str = "_system_"
    ) -> List[int]:
        "Get all hours classified as having fixed obstructions. @zara V17.0.0\n\nArgs:\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    List of hours with fixed obstruction patterns\n"
        return [
            learned_pattern.hour
            for pattern_key, learned_pattern in self._hourly_patterns.items()
            if pattern_key[0] == group_name
            if learned_pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        ]

    def get_learning_summary(self) -> Dict[str, Any]:
        "Get summary of learned patterns. @zara\n\nReturns:\n    Dictionary with learning statistics\n"
        counts_by_type = {pattern_type.value: 0 for pattern_type in PatternType}
        for learned_pattern in self._hourly_patterns.values():
            counts_by_type[learned_pattern.pattern_type.value] += 1
        fixed_obstruction_hours = self.get_all_fixed_obstruction_hours()
        return {
            "initialized": self._initialized,
            "total_days_learned": self._config.get("total_days_learned", 0),
            "total_hours_learned": self._config.get("total_hours_learned", 0),
            "last_learning_date": self._config.get("last_learning_date"),
            "hourly_patterns_count": len(self._hourly_patterns),
            "seasonal_patterns_count": len(self._seasonal_patterns),
            "patterns_by_type": counts_by_type,
            "fixed_obstruction_hours": fixed_obstruction_hours,
            "config": {
                "rolling_window_days": self._config.get("rolling_window_days"),
                "min_samples_for_pattern": self._config.get("min_samples_for_pattern"),
                "ema_alpha": self._config.get("ema_alpha"),
                "fixed_obstruction_threshold": self._config.get(
                    "fixed_obstruction_threshold"
                ),
            },
        }

    async def recalculate_from_history(self, days: int = 30) -> LearningResult:
        "Recalculate all patterns from learning history. @zara V17.0.0\n\nUseful after parameter changes or data corrections.\nNow includes group_name dimension.\n\nArgs:\n    days: Number of days of history to use\n\nReturns:\n    LearningResult with recalculation status\n"
        try:
            self._hourly_patterns = {}
            self._seasonal_patterns = {}
            history_cutoff_date = (
                (datetime.now() - timedelta(days=days)).date().isoformat()
            )
            database_rows = await self.fetch_all(
                "SELECT group_name, date, hour, shadow_detected, shadow_type,\n                          shadow_percent, root_cause, confidence, sun_elevation_deg,\n                          cloud_cover_percent\n                   FROM shadow_learning_history\n                   WHERE date >= ?\n                   ORDER BY group_name, date, hour",
                (history_cutoff_date,),
            )
            if not database_rows:
                return LearningResult(
                    success=False, message="No history data available for recalculation"
                )
            history_dates = set()
            for database_row in database_rows:
                panel_group_name = database_row[0] or "_system_"
                history_date = str(database_row[1])
                history_dates.add(history_date)
                hour_shadow_data = {
                    "hour": database_row[2],
                    "shadow_detected": database_row[3],
                    "shadow_type": database_row[4],
                    "shadow_percent": database_row[5] or 0.0,
                    "root_cause": database_row[6],
                    "confidence": database_row[7],
                    "sun_elevation_deg": database_row[8],
                    "cloud_cover_percent": database_row[9],
                }
                learning_date = datetime.strptime(history_date, "%Y-%m-%d")
                learned_month = learning_date.month
                learned_hour = hour_shadow_data["hour"]
                await self._update_hourly_pattern(
                    learned_hour, hour_shadow_data, panel_group_name
                )
                await self._update_seasonal_pattern(
                    learned_month, learned_hour, hour_shadow_data, panel_group_name
                )
            fixed_obstruction_count = await self._classify_all_patterns()
            return LearningResult(
                success=True,
                message=f"Recalculated from {len(history_dates)} days of history",
                hours_learned=len(database_rows),
                patterns_updated=len(self._hourly_patterns),
                fixed_obstructions_found=fixed_obstruction_count,
            )
        except Exception as error:
            _LOGGER.error("Recalculation failed: %s", error)
            return LearningResult(success=False, message=str(error))


_shadow_pattern_learner: Optional[ShadowPatternLearner] = None


def get_shadow_pattern_learner(
    hass: HomeAssistant, db_manager: DatabaseManager
) -> ShadowPatternLearner:
    """Get or create shadow pattern learner instance. @zara"""
    global _shadow_pattern_learner
    if _shadow_pattern_learner is None:
        _shadow_pattern_learner = ShadowPatternLearner(hass, db_manager)
    return _shadow_pattern_learner
