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
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from .db_manager import (
    SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,
    SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID,
    SHADOW_SYSTEM_HISTORY_REPAIR_MIGRATION_ID,
    DatabaseManager,
)
from .data_io import DataManagerIO
from ..core.core_helpers import SafeDateTimeUtil
from .data_shadow_observation import (
    OBSERVATION_CONFIRMED_SENSOR,
    OBSERVATION_NOT_EVALUABLE,
    OBSERVATION_PEER_CONFIRMED,
    classify_observation_class,
    is_local_learning_cause,
    shadow_detected_from_observation,
)

_LOGGER = logging.getLogger(__name__)
ROLLING_WINDOW_DAYS = 30
SEASONAL_WINDOW_DAYS = 730
MIN_SAMPLES_FOR_PATTERN = 7
MIN_SAMPLES_FOR_SEASONAL = 3
EMA_ALPHA = 0.15
FIXED_OBSTRUCTION_THRESHOLD = 0.7
FREQUENT_SHADOW_THRESHOLD = 0.4
OCCASIONAL_SHADOW_THRESHOLD = 0.15
LOCAL_OBSTRUCTION_MIN_SHARE = 0.6
SHADOW_PATTERN_MODEL_VERSION = "2.0"
SHADOW_DETECTION_THRESHOLD = 0.05
SHADOW_LEARNING_LOCAL_ROOT_CAUSES = {"building_tree_obstruction"}
SHADOW_PATTERN_MIN_OBSERVATION_CONFIDENCE = 0.5
LEGACY_LOCAL_SENSOR_FUSION_MODES = ("solar_radiation_sensor", "lux_sensor")
LEGACY_LOCAL_SENSOR_MIN_CONFIDENCE = 0.8


def _valid_shadow_percent(value: Any) -> Optional[float]:
    """Return a semantically valid shadow percentage or reject the value."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return
    try:
        parsed_percent = float(value)
    except (TypeError, ValueError):
        return
    if not (math.isfinite(parsed_percent) and 0.0 <= parsed_percent <= 100.0):
        return None if True else None
    return parsed_percent


def _bounded_shadow_percent(value: Any) -> float:
    """Bound a consumer-facing percentage without repairing stored history."""
    try:
        parsed_percent = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed_percent):
        return 0.0
    return min(100.0, max(0.0, parsed_percent))


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
    reason_code: Optional[str] = None


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
        _LOGGER.debug("ShadowPatternLearner initialized")

    async def async_init(self) -> bool:
        """Async initialization - load existing patterns from database. @zara"""
        try:
            await self._load_config()
            await self._load_hourly_patterns()
            await self._load_seasonal_patterns()
            current_model = self._config.get("version") == SHADOW_PATTERN_MODEL_VERSION
            group_repair_pending = await self._group_repair_patterns_pending()
            system_repair_recorded = await self._system_history_repair_recorded()
            async with self.db.transaction():
                legacy_migrated_count = await self._migrate_legacy_detection_history(
                    include_system=not group_repair_pending
                    and (not system_repair_recorded)
                )
                await self._upgrade_pattern_model_if_needed()
                if group_repair_pending:
                    if current_model:
                        result = await self._recalculate_repaired_group_patterns(
                            int(
                                self._config.get(
                                    "rolling_window_days", ROLLING_WINDOW_DAYS
                                )
                            )
                        )
                        if not result.success:
                            raise RuntimeError(
                                f"Repaired group shadow rebuild failed: {result.message}"
                            )
                    if not await self.execute_query(
                        "UPDATE shadow_history_repair_runs\n                           SET patterns_rebuilt_at = ?\n                           WHERE repair_version = ?\n                             AND patterns_rebuilt_at IS NULL",
                        (
                            SafeDateTimeUtil.now(),
                            SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID,
                        ),
                    ):
                        raise RuntimeError(
                            "Failed to complete shadow group repair audit"
                        )
                elif legacy_migrated_count and current_model:
                    result = await self.recalculate_from_history(
                        int(
                            self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS)
                        )
                    )
                    if not result.success:
                        raise RuntimeError(
                            f"Legacy shadow history rebuild failed: {result.message}"
                        )
            if legacy_migrated_count:
                _LOGGER.info(
                    "Shadow legacy replay migrated %d historical observations",
                    legacy_migrated_count,
                )
            if await self._curtailment_shadow_repair_pending():
                result = await self.recalculate_from_history(
                    int(self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS))
                )
                if not result.success:
                    if "No history" not in (result.message or ""):
                        raise RuntimeError(
                            f"Curtailment shadow pattern rebuild failed: {result.message}"
                        )
                if not await self.execute_query(
                    "UPDATE shadow_history_repair_runs\n                       SET patterns_rebuilt_at = ?\n                       WHERE repair_version = ?\n                         AND patterns_rebuilt_at IS NULL",
                    (
                        SafeDateTimeUtil.now(),
                        SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,
                    ),
                ):
                    raise RuntimeError(
                        "Failed to complete curtailment shadow repair audit"
                    )
            self._initialized = True
            known_pattern_count = len(
                [
                    pattern
                    for pattern in self._hourly_patterns.values()
                    if pattern.pattern_type != PatternType.UNKNOWN
                ]
            )
            fixed_obstruction_count = len(
                [
                    pattern
                    for pattern in self._hourly_patterns.values()
                    if pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
                ]
            )
            _LOGGER.debug(
                "ShadowPatternLearner loaded: %d hourly patterns, %d seasonal patterns, %d fixed obstructions detected",
                known_pattern_count,
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
            row = await self.fetch_one(
                "SELECT total_days_learned, total_hours_learned, last_learning_date,\n                          patterns_detected, fixed_obstructions_detected, version\n                   FROM shadow_pattern_config WHERE id = 1"
            )
            if row:
                self._config = {
                    "rolling_window_days": ROLLING_WINDOW_DAYS,
                    "min_samples_for_pattern": MIN_SAMPLES_FOR_PATTERN,
                    "ema_alpha": EMA_ALPHA,
                    "fixed_obstruction_threshold": FIXED_OBSTRUCTION_THRESHOLD,
                    "total_days_learned": row[0] or 0,
                    "total_hours_learned": row[1] or 0,
                    "last_learning_date": row[2],
                    "patterns_detected": row[3] or 0,
                    "fixed_obstructions_detected": row[4] or 0,
                    "version": str(row[5] or "1.0"),
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
                    "version": SHADOW_PATTERN_MODEL_VERSION,
                }
        except Exception as error:
            _LOGGER.error("Could not load shadow pattern config: %s", error)
            raise
        return None if True else None

    async def _group_repair_patterns_pending(self) -> bool:
        repair_table_exists = await self.fetch_one(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'shadow_history_repair_runs'"
        )
        if not repair_table_exists:
            return False
        row = await self.fetch_one(
            "SELECT 1 FROM shadow_history_repair_runs\n               WHERE repair_version = ? AND patterns_rebuilt_at IS NULL",
            (SHADOW_GROUP_PERCENT_REPAIR_MIGRATION_ID,),
        )
        return row is not None

    async def _curtailment_shadow_repair_pending(self) -> bool:
        repair_table_exists = await self.fetch_one(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'shadow_history_repair_runs'"
        )
        if not repair_table_exists:
            return False
        row = await self.fetch_one(
            "SELECT 1 FROM shadow_history_repair_runs\n               WHERE repair_version = ? AND patterns_rebuilt_at IS NULL",
            (SHADOW_CURTAILMENT_OBSERVATION_REPAIR_MIGRATION_ID,),
        )
        return row is not None

    async def _system_history_repair_recorded(self) -> bool:
        repair_table_exists = await self.fetch_one(
            "SELECT 1 FROM sqlite_master\n               WHERE type = 'table' AND name = 'shadow_history_repair_runs'"
        )
        if not repair_table_exists:
            return False
        row = await self.fetch_one(
            "SELECT 1 FROM shadow_history_repair_runs\n               WHERE repair_version = ?",
            (SHADOW_SYSTEM_HISTORY_REPAIR_MIGRATION_ID,),
        )
        return row is not None

    async def _recalculate_repaired_group_patterns(self, days: int) -> LearningResult:
        previous_window_days = self._config.get(
            "rolling_window_days", ROLLING_WINDOW_DAYS
        )
        try:
            rows = await self.fetch_all(
                "SELECT group_name, MAX(date), COUNT(*)\n                   FROM shadow_learning_history\n                   WHERE group_name <> '_system_'\n                   GROUP BY group_name\n                   ORDER BY group_name"
            )
            self._config["rolling_window_days"] = max(1, int(days))
            patterns_updated = 0
            fixed_obstruction_count = 0
            hours_learned = 0
            for row in rows:
                reference_date = datetime.strptime(str(row[1]), "%Y-%m-%d").date()
                group_patterns_updated, group_fixed_obstructions = (
                    await self._rebuild_group_patterns(str(row[0]), reference_date)
                )
                patterns_updated += group_patterns_updated
                fixed_obstruction_count += group_fixed_obstructions
                hours_learned += int(row[2] or 0)
            return LearningResult(
                success=True,
                message=f"Recalculated {len(rows)} repaired group scopes",
                hours_learned=hours_learned,
                patterns_updated=patterns_updated,
                fixed_obstructions_found=fixed_obstruction_count,
            )
        except Exception as error:
            return LearningResult(success=False, message=str(error))
        finally:
            self._config["rolling_window_days"] = previous_window_days

    async def _upgrade_pattern_model_if_needed(self) -> None:
        if self._config.get("version") == SHADOW_PATTERN_MODEL_VERSION:
            return
        result = await self.recalculate_from_history(
            int(self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS))
        )
        if not result.success:
            if result.message != "No history data available for recalculation":
                raise RuntimeError(
                    f"Shadow pattern V2 rebuild failed: {result.message}"
                )
            async with self.db.transaction():
                if not await self.execute_query("DELETE FROM shadow_pattern_hourly"):
                    raise RuntimeError("Failed to clear legacy hourly shadow patterns")
                if not await self.execute_query("DELETE FROM shadow_pattern_seasonal"):
                    raise RuntimeError(
                        "Failed to clear legacy seasonal shadow patterns"
                    )
            self._hourly_patterns = {}
            self._seasonal_patterns = {}
        if not await self.execute_query(
            "INSERT INTO shadow_pattern_config\n               (id, version, updated_at)\n               VALUES (1, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   version = excluded.version,\n                   updated_at = excluded.updated_at",
            (SHADOW_PATTERN_MODEL_VERSION, SafeDateTimeUtil.now()),
        ):
            raise RuntimeError("Failed to persist shadow pattern model version")
        self._config["version"] = SHADOW_PATTERN_MODEL_VERSION

    async def _migrate_legacy_detection_history(
        self, *, include_system: bool = True
    ) -> int:
        tables = {
            str(row[0])
            for row in await self.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required_tables = {
            "weather_forecast",
            "hourly_predictions",
            "prediction_weather",
            "prediction_astronomy",
            "hourly_shadow_detection",
            "shadow_learning_history",
        }
        if not required_tables.issubset(tables):
            return 0
        before_count_row = await self.fetch_one(
            "SELECT COUNT(*) FROM shadow_learning_history"
        )
        before_count = int(before_count_row[0] or 0) if before_count_row else 0
        today_iso = SafeDateTimeUtil.now().date().isoformat()
        system_migration_sql = "\n            WITH ranked AS (\n                SELECT hp.target_date,\n                       hp.target_hour,\n                       hsd.shadow_type,\n                       hsd.shadow_percent,\n                       hsd.root_cause,\n                       hsd.confidence,\n                       hsd.sensor_fusion_confidence,\n                       hsd.sensor_fusion_root_cause,\n                       hsd.fusion_mode,\n                       pa.sun_elevation_deg,\n                       COALESCE(\n                           (SELECT pw.clouds\n                              FROM prediction_weather pw\n                             WHERE pw.prediction_id = hp.prediction_id\n                               AND pw.weather_type = 'actual'\n                             ORDER BY pw.id DESC LIMIT 1),\n                           (SELECT pw.clouds\n                              FROM prediction_weather pw\n                             WHERE pw.prediction_id = hp.prediction_id\n                               AND pw.weather_type = 'corrected'\n                             ORDER BY pw.id DESC LIMIT 1),\n                           (SELECT pw.clouds\n                              FROM prediction_weather pw\n                             WHERE pw.prediction_id = hp.prediction_id\n                               AND pw.weather_type = 'forecast'\n                             ORDER BY pw.id DESC LIMIT 1),\n                           (SELECT wf.clouds\n                              FROM weather_forecast wf\n                             WHERE wf.forecast_date = hp.target_date\n                               AND wf.hour = hp.target_hour\n                             LIMIT 1)\n                       ) AS cloud_cover_percent,\n                       hsd.theoretical_max_kwh,\n                       hp.actual_kwh,\n                       hsd.efficiency_ratio,\n                       ROW_NUMBER() OVER (\n                           PARTITION BY hp.target_date, hp.target_hour\n                           ORDER BY hp.prediction_created_at DESC, hp.id DESC\n                       ) AS row_rank\n                  FROM hourly_predictions hp\n                  JOIN hourly_shadow_detection hsd\n                    ON hsd.prediction_id = hp.prediction_id\n             LEFT JOIN prediction_astronomy pa\n                    ON pa.prediction_id = hp.prediction_id\n                 WHERE hp.target_date < ?\n                   AND hp.target_hour BETWEEN 6 AND 20\n                   AND COALESCE(hp.exclude_from_learning, 0) = 0\n                   AND COALESCE(hp.mppt_throttled, 0) = 0\n                   AND COALESCE(hp.inverter_clipped, 0) = 0\n                   AND COALESCE(hp.weather_alert_type, '') = ''\n                   AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n                   AND COALESCE(hp.data_quarantine_reason, '') = ''\n                   AND hp.actual_kwh IS NOT NULL\n                   AND COALESCE(hsd.theoretical_max_kwh, 0.0) >= 0.01\n                   AND COALESCE(hsd.confidence, 0.0) >= ?\n            )\n            INSERT OR IGNORE INTO shadow_learning_history\n                (group_name, date, hour, shadow_detected, shadow_type,\n                 shadow_percent, root_cause, confidence, sun_elevation_deg,\n                 cloud_cover_percent, theoretical_max_kwh, actual_kwh,\n                 efficiency_ratio)\n            SELECT '_system_',\n                   target_date,\n                   target_hour,\n                   CASE WHEN COALESCE(shadow_percent, 0.0) >= ? THEN 1 ELSE 0 END,\n                   shadow_type,\n                   COALESCE(shadow_percent, 0.0),\n                   CASE\n                       WHEN root_cause IN ('building_tree_obstruction',\n                                           'possible_obstruction')\n                        AND fusion_mode IN (?, ?)\n                        AND sensor_fusion_root_cause = 'building_tree_obstruction'\n                        AND COALESCE(sensor_fusion_confidence, 0.0) >= ?\n                           THEN 'possible_obstruction'\n                       WHEN root_cause IN ('building_tree_obstruction',\n                                           'possible_obstruction')\n                           THEN 'unknown'\n                       WHEN root_cause = 'clouds' THEN 'weather_clouds'\n                       ELSE COALESCE(NULLIF(root_cause, ''), 'unknown')\n                   END,\n                   CASE\n                       WHEN fusion_mode IN (?, ?)\n                        AND sensor_fusion_root_cause = 'building_tree_obstruction'\n                        AND COALESCE(sensor_fusion_confidence, 0.0) >= ?\n                           THEN MIN(confidence, sensor_fusion_confidence)\n                       ELSE confidence\n                   END,\n                   sun_elevation_deg,\n                   cloud_cover_percent,\n                   theoretical_max_kwh,\n                   actual_kwh,\n                   efficiency_ratio\n              FROM ranked\n             WHERE row_rank = 1\n               AND shadow_percent BETWEEN 0.0 AND 100.0\n        "
        system_migration_params = (
            today_iso,
            SHADOW_PATTERN_MIN_OBSERVATION_CONFIDENCE,
            SHADOW_DETECTION_THRESHOLD * 100,
            *LEGACY_LOCAL_SENSOR_FUSION_MODES,
            LEGACY_LOCAL_SENSOR_MIN_CONFIDENCE,
            *LEGACY_LOCAL_SENSOR_FUSION_MODES,
            LEGACY_LOCAL_SENSOR_MIN_CONFIDENCE,
        )
        if include_system:
            if not await self.execute_query(
                system_migration_sql, system_migration_params, timeout=60.0
            ):
                raise RuntimeError("Failed to migrate system shadow detection history")
        if "shadow_detection_groups" in tables:
            group_migration_sql = "\n                WITH ranked AS (\n                    SELECT sdg.group_name,\n                           hp.target_date,\n                           hp.target_hour,\n                           sdg.shadow_type,\n                           sdg.shadow_percent,\n                           sdg.root_cause,\n                           sdg.confidence,\n                           hsd.sensor_fusion_confidence,\n                           hsd.sensor_fusion_root_cause,\n                           hsd.fusion_mode,\n                           pa.sun_elevation_deg,\n                           COALESCE(\n                               (SELECT pw.clouds\n                                  FROM prediction_weather pw\n                                 WHERE pw.prediction_id = hp.prediction_id\n                                   AND pw.weather_type = 'actual'\n                                 ORDER BY pw.id DESC LIMIT 1),\n                               (SELECT pw.clouds\n                                  FROM prediction_weather pw\n                                 WHERE pw.prediction_id = hp.prediction_id\n                                   AND pw.weather_type = 'corrected'\n                                 ORDER BY pw.id DESC LIMIT 1),\n                               (SELECT pw.clouds\n                                  FROM prediction_weather pw\n                                 WHERE pw.prediction_id = hp.prediction_id\n                                   AND pw.weather_type = 'forecast'\n                                 ORDER BY pw.id DESC LIMIT 1),\n                               (SELECT wf.clouds\n                                  FROM weather_forecast wf\n                                 WHERE wf.forecast_date = hp.target_date\n                                   AND wf.hour = hp.target_hour\n                                 LIMIT 1)\n                           ) AS cloud_cover_percent,\n                           sdg.theoretical_max_kwh,\n                           sdg.actual_kwh,\n                           sdg.efficiency_ratio,\n                           ROW_NUMBER() OVER (\n                               PARTITION BY sdg.group_name, hp.target_date,\n                                            hp.target_hour\n                               ORDER BY hp.prediction_created_at DESC, hp.id DESC\n                           ) AS row_rank\n                      FROM hourly_predictions hp\n                      JOIN shadow_detection_groups sdg\n                        ON sdg.prediction_id = hp.prediction_id\n                 LEFT JOIN hourly_shadow_detection hsd\n                        ON hsd.prediction_id = hp.prediction_id\n                 LEFT JOIN prediction_astronomy pa\n                        ON pa.prediction_id = hp.prediction_id\n                     WHERE hp.target_date < ?\n                       AND hp.target_hour BETWEEN 6 AND 20\n                       AND COALESCE(hp.exclude_from_learning, 0) = 0\n                       AND COALESCE(hp.mppt_throttled, 0) = 0\n                       AND COALESCE(hp.inverter_clipped, 0) = 0\n                       AND COALESCE(hp.weather_alert_type, '') = ''\n                       AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n                       AND COALESCE(hp.data_quarantine_reason, '') = ''\n                       AND sdg.actual_kwh IS NOT NULL\n                       AND COALESCE(sdg.theoretical_max_kwh, 0.0) >= 0.01\n                       AND COALESCE(sdg.confidence, 0.0) >= ?\n                )\n                INSERT OR IGNORE INTO shadow_learning_history\n                    (group_name, date, hour, shadow_detected, shadow_type,\n                     shadow_percent, root_cause, confidence, sun_elevation_deg,\n                     cloud_cover_percent, theoretical_max_kwh, actual_kwh,\n                     efficiency_ratio)\n                SELECT group_name,\n                       target_date,\n                       target_hour,\n                       CASE WHEN COALESCE(shadow_percent, 0.0) >= ? THEN 1 ELSE 0 END,\n                       shadow_type,\n                       COALESCE(shadow_percent, 0.0),\n                       CASE\n                           WHEN root_cause IN ('building_tree_obstruction',\n                                               'possible_obstruction')\n                            AND fusion_mode IN (?, ?)\n                            AND sensor_fusion_root_cause = 'building_tree_obstruction'\n                            AND COALESCE(sensor_fusion_confidence, 0.0) >= ?\n                               THEN 'possible_obstruction'\n                           WHEN root_cause IN ('building_tree_obstruction',\n                                               'possible_obstruction')\n                               THEN 'unknown'\n                           WHEN root_cause = 'clouds' THEN 'weather_clouds'\n                           ELSE COALESCE(NULLIF(root_cause, ''), 'unknown')\n                       END,\n                       CASE\n                           WHEN fusion_mode IN (?, ?)\n                            AND sensor_fusion_root_cause = 'building_tree_obstruction'\n                            AND COALESCE(sensor_fusion_confidence, 0.0) >= ?\n                               THEN MIN(confidence, sensor_fusion_confidence)\n                           ELSE confidence\n                       END,\n                       sun_elevation_deg,\n                       cloud_cover_percent,\n                       theoretical_max_kwh,\n                       actual_kwh,\n                       efficiency_ratio\n                  FROM ranked\n                 WHERE row_rank = 1\n                   AND shadow_percent BETWEEN 0.0 AND 100.0\n            "
            group_migration_params = (
                today_iso,
                SHADOW_PATTERN_MIN_OBSERVATION_CONFIDENCE,
                SHADOW_DETECTION_THRESHOLD * 100,
                *LEGACY_LOCAL_SENSOR_FUSION_MODES,
                LEGACY_LOCAL_SENSOR_MIN_CONFIDENCE,
                *LEGACY_LOCAL_SENSOR_FUSION_MODES,
                LEGACY_LOCAL_SENSOR_MIN_CONFIDENCE,
            )
            if not await self.execute_query(
                group_migration_sql, group_migration_params, timeout=60.0
            ):
                raise RuntimeError("Failed to migrate panel-group shadow history")
        after_count_row = await self.fetch_one(
            "SELECT COUNT(*) FROM shadow_learning_history"
        )
        after_count = int(after_count_row[0] or 0) if after_count_row else before_count
        return max(0, after_count - before_count)

    async def _load_hourly_patterns(self) -> None:
        """Load hourly patterns from database. @zara V17.0.0: group_name dimension"""
        try:
            rows = await self.fetch_all(
                "SELECT * FROM shadow_pattern_hourly ORDER BY group_name, hour"
            )
            for row in rows:
                group_name = row[1] or "_system_"
                hour = row[2]
                pattern_key = (group_name, hour)
                self._hourly_patterns[pattern_key] = HourlyPattern(
                    group_name=group_name,
                    hour=hour,
                    shadow_occurrence_rate=row[3] or 0.0,
                    avg_shadow_percent=row[4] or 0.0,
                    std_dev_shadow_percent=row[5] or 0.0,
                    pct_weather_clouds=row[6] or 0.0,
                    pct_building_tree=row[7] or 0.0,
                    pct_low_sun=row[8] or 0.0,
                    pct_other=row[9] or 0.0,
                    pattern_type=(
                        PatternType(row[10]) if row[10] else PatternType.UNKNOWN
                    ),
                    confidence=row[11] or 0.0,
                    sample_count=row[12] or 0,
                    shadow_days=row[13] or 0,
                    clear_days=row[14] or 0,
                )
                continue
        except Exception as error:
            _LOGGER.error("Could not load hourly patterns: %s", error)
            raise

    async def _load_seasonal_patterns(self) -> None:
        """Load seasonal patterns from database. @zara V17.0.0: group_name dimension"""
        try:
            rows = await self.fetch_all(
                "SELECT * FROM shadow_pattern_seasonal ORDER BY group_name, month, hour"
            )
            for row in rows:
                group_name = row[1] or "_system_"
                month = row[2]
                hour = row[3]
                pattern_key = (group_name, month, hour)
                self._seasonal_patterns[pattern_key] = SeasonalPattern(
                    group_name=group_name,
                    month=month,
                    hour=hour,
                    shadow_occurrence_rate=row[4] or 0.0,
                    avg_shadow_percent=row[5] or 0.0,
                    std_dev_shadow_percent=row[6] or 0.0,
                    dominant_cause=row[7] or "unknown",
                    sample_count=row[8] or 0,
                    shadow_days=row[9] or 0,
                    confidence=row[10] or 0.0,
                )
        except Exception as error:
            _LOGGER.error("Could not load seasonal patterns: %s", error)
            raise

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
            observations = await self._get_shadow_data_for_date(date_str, group_name)
            if not observations:
                _LOGGER.debug(
                    "No shadow data for %s/%s - skipping learning", date_str, group_name
                )
                return LearningResult(
                    success=False,
                    message="No shadow detection data available",
                    date=date_str,
                    reason_code="shadow_detection_data_unavailable",
                )
            hours_learned = 0
            for observation in observations:
                hour = observation.get("hour")
                if hour is None:
                    continue
                await self._save_learning_history(date_str, observation, group_name)
                hours_learned += 1
            reference_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            async with self.db.transaction():
                patterns_updated, fixed_obstructions = (
                    await self._rebuild_group_patterns(group_name, reference_date)
                )
            await self._update_config_stats(date_str, hours_learned, fixed_obstructions)
            _LOGGER.info(
                "Shadow pattern learning for %s [%s]: %d hours, %d patterns updated, %d fixed obstructions",
                date_str,
                group_name,
                hours_learned,
                patterns_updated,
                fixed_obstructions,
            )
            return LearningResult(
                success=True,
                message=f"Learned from {hours_learned} hours",
                hours_learned=hours_learned,
                patterns_updated=patterns_updated,
                fixed_obstructions_found=fixed_obstructions,
                date=date_str,
            )
        except Exception as error:
            _LOGGER.error("Shadow pattern learning failed for %s: %s", date_str, error)
            return LearningResult(
                success=False,
                message=str(error),
                date=date_str,
                reason_code="shadow_pattern_learning_failed",
            )

    @staticmethod
    def _pattern_type_for_aggregate(
        occurrence: float,
        std_dev: float,
        local_obstruction_share: float,
        sample_count: int,
        min_samples: int,
        fixed_threshold: float,
        clear_days: int = 0,
    ) -> PatternType:
        if sample_count < min_samples:
            return PatternType.UNKNOWN
        if clear_days < 1:
            return PatternType.NO_SHADOW
        if local_obstruction_share < LOCAL_OBSTRUCTION_MIN_SHARE:
            return PatternType.NO_SHADOW
        if occurrence >= fixed_threshold and std_dev < 15.0:
            return PatternType.FIXED_OBSTRUCTION
        if occurrence >= FREQUENT_SHADOW_THRESHOLD:
            return PatternType.FREQUENT
        if occurrence >= OCCASIONAL_SHADOW_THRESHOLD:
            return PatternType.OCCASIONAL
        return PatternType.NO_SHADOW

    @staticmethod
    def _history_row_fields(row: Tuple[Any, ...], group_name: str) -> Dict[str, Any]:
        return (
            {
                "shadow_detected": bool(row[2]),
                "shadow_percent": row[3],
                "root_cause": str(row[4] or ""),
                "cloud_cover_percent": row[5],
                "observation_class": row[7] if len(row) > 7 else None,
                "fusion_mode": row[8] if len(row) > 8 else None,
                "group_name": group_name,
            }
            if True
            else None
        )

    @staticmethod
    def _cause_distribution(
        rows: List[Tuple[Any, ...]], group_name: str = "_system_"
    ) -> Dict[str, float]:
        shadow_rows = [row for row in rows if bool(row[2])]
        shadow_count = len(shadow_rows)
        if shadow_count == 0:
            return {
                "weather_clouds": 0.0,
                "building_tree": 0.0,
                "low_sun": 0.0,
                "other": 0.0,
            }
        local_count = 0
        weather_count = 0
        low_sun_count = 0
        for row in shadow_rows:
            observation_fields = ShadowPatternLearner._history_row_fields(
                row, group_name
            )
            root_cause = observation_fields["root_cause"]
            if is_local_learning_cause(
                root_cause=root_cause,
                observation_class=observation_fields["observation_class"],
                fusion_mode=observation_fields["fusion_mode"],
                group_name=group_name,
                shadow_detected=True,
            ):
                local_count += 1
            elif root_cause == "weather_clouds":
                weather_count += 1
            elif root_cause == "low_sun_angle":
                low_sun_count += 1
        return {
            "weather_clouds": weather_count / shadow_count,
            "building_tree": local_count / shadow_count,
            "low_sun": low_sun_count / shadow_count,
            "other": (shadow_count - weather_count - local_count - low_sun_count)
            / shadow_count,
        }

    async def _rebuild_group_patterns(
        self, group_name: str, reference_date: date
    ) -> Tuple[int, int]:
        rolling_days = max(
            1, int(self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS))
        )
        rolling_start = (reference_date - timedelta(days=rolling_days - 1)).isoformat()
        seasonal_start = (
            reference_date - timedelta(days=SEASONAL_WINDOW_DAYS - 1)
        ).isoformat()
        reference_iso = reference_date.isoformat()
        recent_rows = await self.fetch_all(
            "SELECT date, hour, shadow_detected, shadow_percent, root_cause,\n                      cloud_cover_percent, confidence, observation_class, fusion_mode\n               FROM shadow_learning_history\n               WHERE group_name = ? AND date >= ? AND date <= ?\n               ORDER BY date, hour",
            (group_name, rolling_start, reference_iso),
        )
        seasonal_rows = await self.fetch_all(
            "SELECT date, hour, shadow_detected, shadow_percent, root_cause,\n                      cloud_cover_percent, confidence, observation_class, fusion_mode\n               FROM shadow_learning_history\n               WHERE group_name = ? AND date >= ? AND date <= ?\n               ORDER BY date, hour",
            (group_name, seasonal_start, reference_iso),
        )
        recent_rows = [
            row for row in recent_rows if _valid_shadow_percent(row[3]) is not None
        ]
        seasonal_rows = [
            row for row in seasonal_rows if _valid_shadow_percent(row[3]) is not None
        ]
        previous_types = {
            pattern_key: pattern.pattern_type
            for pattern_key, pattern in self._hourly_patterns.items()
            if pattern_key[0] == group_name
        }
        if not await self.execute_query(
            "DELETE FROM shadow_pattern_hourly WHERE group_name = ?", (group_name,)
        ):
            raise RuntimeError(
                f"Failed to clear hourly shadow patterns for {group_name}"
            )
        if not await self.execute_query(
            "DELETE FROM shadow_pattern_seasonal WHERE group_name = ?", (group_name,)
        ):
            raise RuntimeError(
                f"Failed to clear seasonal shadow patterns for {group_name}"
            )
        for pattern_key in [
            pattern_key
            for pattern_key in self._hourly_patterns
            if pattern_key[0] == group_name
        ]:
            del self._hourly_patterns[pattern_key]
        for pattern_key in [
            pattern_key
            for pattern_key in self._seasonal_patterns
            if pattern_key[0] == group_name
        ]:
            del self._seasonal_patterns[pattern_key]
        rows_by_hour = {}
        for row in recent_rows:
            rows_by_hour.setdefault(int(row[1]), []).append(tuple(row))
        minimum_samples = int(
            self._config.get("min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN)
        )
        fixed_threshold = float(
            self._config.get("fixed_obstruction_threshold", FIXED_OBSTRUCTION_THRESHOLD)
        )
        patterns_updated = 0
        fixed_obstruction_count = 0
        for hour, rows in rows_by_hour.items():
            shadow_rows = [row for row in rows if bool(row[2])]
            local_shadow_rows = [
                row
                for row in shadow_rows
                if is_local_learning_cause(
                    root_cause=str(row[4] or ""),
                    observation_class=row[7] if len(row) > 7 else None,
                    fusion_mode=row[8] if len(row) > 8 else None,
                    group_name=group_name,
                    shadow_detected=True,
                )
            ]
            shadow_percentages = [
                shadow_percent
                for shadow_percent in (
                    _valid_shadow_percent(row[3]) for row in local_shadow_rows
                )
                if shadow_percent is not None
            ]
            occurrence_rate = len(shadow_rows) / len(rows)
            mean_shadow_percent = (
                sum(shadow_percentages) / len(shadow_percentages)
                if shadow_percentages
                else 0.0
            )
            variance = (
                sum(
                    (
                        (shadow_percent - mean_shadow_percent) ** 2
                        for shadow_percent in shadow_percentages
                    )
                )
                / len(shadow_percentages)
                if shadow_percentages
                else 0.0
            )
            cause_distribution = self._cause_distribution(rows, group_name)
            clear_days = len(rows) - len(shadow_rows)
            pattern_type = self._pattern_type_for_aggregate(
                occurrence_rate,
                math.sqrt(variance),
                cause_distribution["building_tree"],
                len(rows),
                minimum_samples,
                fixed_threshold,
                clear_days,
            )
            confidence = min(1.0, len(rows) / max(1, minimum_samples * 2))
            pattern = HourlyPattern(
                group_name=group_name,
                hour=hour,
                shadow_occurrence_rate=occurrence_rate,
                avg_shadow_percent=mean_shadow_percent,
                std_dev_shadow_percent=math.sqrt(variance),
                pct_weather_clouds=cause_distribution["weather_clouds"],
                pct_building_tree=cause_distribution["building_tree"],
                pct_low_sun=cause_distribution["low_sun"],
                pct_other=cause_distribution["other"],
                pattern_type=pattern_type,
                confidence=confidence,
                sample_count=len(rows),
                shadow_days=len(shadow_rows),
                clear_days=len(rows) - len(shadow_rows),
            )
            self._hourly_patterns[group_name, hour] = pattern
            await self._save_hourly_pattern(pattern)
            if previous_types.get((group_name, hour)) != pattern_type:
                patterns_updated += 1
            if pattern_type == PatternType.FIXED_OBSTRUCTION:
                fixed_obstruction_count += 1
                continue
        rows_by_month_hour = {}
        for row in seasonal_rows:
            observation_date = datetime.strptime(str(row[0]), "%Y-%m-%d").date()
            rows_by_month_hour.setdefault(
                (observation_date.month, int(row[1])), []
            ).append(tuple(row))
        for (month, hour), rows in rows_by_month_hour.items():
            shadow_rows = [row for row in rows if bool(row[2])]
            shadow_percentages = [_valid_shadow_percent(row[3]) for row in shadow_rows]
            mean_shadow_percent = (
                sum(shadow_percentages) / len(shadow_percentages)
                if shadow_percentages
                else 0.0
            )
            variance = (
                sum(
                    (
                        (shadow_percent - mean_shadow_percent) ** 2
                        for shadow_percent in shadow_percentages
                    )
                )
                / len(shadow_percentages)
                if shadow_percentages
                else 0.0
            )
            seasonal_causes = self._cause_distribution(rows, group_name)
            dominant_cause_category = max(seasonal_causes, key=seasonal_causes.get)
            dominant_cause = {
                "building_tree": "possible_obstruction",
                "weather_clouds": "weather_clouds",
                "low_sun": "low_sun_angle",
                "other": "unknown",
            }[dominant_cause_category]
            pattern = SeasonalPattern(
                group_name=group_name,
                month=month,
                hour=hour,
                shadow_occurrence_rate=len(shadow_rows) / len(rows),
                avg_shadow_percent=mean_shadow_percent,
                std_dev_shadow_percent=math.sqrt(variance),
                dominant_cause=dominant_cause,
                sample_count=len(rows),
                shadow_days=len(shadow_rows),
                confidence=min(1.0, len(rows) / (MIN_SAMPLES_FOR_SEASONAL * 2)),
            )
            self._seasonal_patterns[group_name, month, hour] = pattern
            await self._save_seasonal_pattern(pattern)
            continue
        return (patterns_updated, fixed_obstruction_count)

    async def _get_shadow_data_for_date(
        self, date_str: str, group_name: str = "_system_"
    ) -> List[Dict[str, Any]]:
        """Get shadow detection data for a specific date. @zara V17.0.0"""
        try:
            if group_name != "_system_":
                rows = await self.fetch_all(
                    "SELECT hp.target_hour, sdg.shadow_type, sdg.shadow_percent,\n                              sdg.root_cause, sdg.confidence, sdg.efficiency_ratio,\n                              sdg.theoretical_max_kwh, sdg.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover,\n                              hp.exclude_from_learning, hp.mppt_throttle_reason,\n                              hp.mppt_throttled, hp.inverter_clipped,\n                              hp.weather_alert_type, hp.actual_backfill_blocked,\n                              hp.data_quarantine_reason, NULL as fusion_mode,\n                              NULL as ensemble_mode, NULL as observation_class,\n                              NULL as evaluable, NULL as learning_eligible\n                       FROM hourly_predictions hp\n                       JOIN shadow_detection_groups sdg ON hp.prediction_id = sdg.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND sdg.group_name = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                       ORDER BY hp.target_hour",
                    (date_str, group_name),
                )
            else:
                rows = await self.fetch_all(
                    "SELECT hp.target_hour, hsd.shadow_type, hsd.shadow_percent,\n                              hsd.root_cause, hsd.confidence, hsd.efficiency_ratio,\n                              hsd.theoretical_max_kwh, hp.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover,\n                              hp.exclude_from_learning, hp.mppt_throttle_reason,\n                              hp.mppt_throttled, hp.inverter_clipped,\n                              hp.weather_alert_type, hp.actual_backfill_blocked,\n                              hp.data_quarantine_reason, hsd.fusion_mode,\n                              hsd.ensemble_mode, hsd.observation_class,\n                              hsd.evaluable, hsd.learning_eligible\n                       FROM hourly_predictions hp\n                       JOIN hourly_shadow_detection hsd ON hp.prediction_id = hsd.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                       ORDER BY hp.target_hour",
                    (date_str,),
                )
            eligible_observations = []
            for row in rows:
                observation = {
                    "hour": row[0],
                    "shadow_type": row[1],
                    "shadow_percent": row[2] or 0.0,
                    "root_cause": row[3],
                    "confidence": row[4] or 0.0,
                    "efficiency_ratio": row[5] or 0.0,
                    "theoretical_max_kwh": row[6],
                    "actual_kwh": row[7],
                    "sun_elevation_deg": row[8],
                    "cloud_cover_percent": row[9],
                    "exclude_from_learning": (
                        bool(row[10]) if row[10] is not None else False
                    ),
                    "mppt_throttle_reason": row[11],
                    "mppt_throttled": bool(row[12]) if row[12] is not None else False,
                    "inverter_clipped": bool(row[13]) if row[13] is not None else False,
                    "weather_alert_type": row[14],
                    "actual_backfill_blocked": (
                        bool(row[15]) if row[15] is not None else False
                    ),
                    "data_quarantine_reason": row[16],
                    "fusion_mode": row[17] if len(row) > 17 else None,
                    "ensemble_mode": row[18] if len(row) > 18 else None,
                    "observation_class": row[19] if len(row) > 19 else None,
                    "evaluable": (
                        bool(row[20]) if len(row) > 20 and row[20] is not None else True
                    ),
                    "learning_eligible": (
                        bool(row[21]) if len(row) > 21 and row[21] is not None else True
                    ),
                }
                if self._is_shadow_learning_eligible(observation):
                    eligible_observations.append(observation)
                    continue
            return eligible_observations
        except Exception as error:
            _LOGGER.error(
                "Failed to get shadow data for %s [%s]: %s", date_str, group_name, error
            )
            raise

    @staticmethod
    def _is_shadow_learning_eligible(hour_data: Dict[str, Any]) -> bool:
        if hour_data.get("exclude_from_learning"):
            return False
        if hour_data.get("mppt_throttled") or hour_data.get("inverter_clipped"):
            return False
        if hour_data.get("weather_alert_type"):
            return False
        if hour_data.get("actual_backfill_blocked") or hour_data.get(
            "data_quarantine_reason"
        ):
            return False
        if hour_data.get("actual_kwh") is None:
            return False
        if float(hour_data.get("theoretical_max_kwh") or 0.0) < 0.01:
            return False
        if (
            float(hour_data.get("confidence") or 0.0)
            < SHADOW_PATTERN_MIN_OBSERVATION_CONFIDENCE
        ):
            return False
        if hour_data.get("ensemble_mode") == "theory_only":
            return False
        if hour_data.get("observation_class") == OBSERVATION_NOT_EVALUABLE:
            return False
        if hour_data.get("learning_eligible") is False:
            return False
        if hour_data.get("evaluable") is False:
            return False
        if _valid_shadow_percent(hour_data.get("shadow_percent")) is None:
            return False
        return True

    async def _save_learning_history(
        self, date_str: str, hour_data: Dict[str, Any], group_name: str = "_system_"
    ) -> None:
        """Save hour data to learning history. @zara V17.0.0: group_name"""
        try:
            hour = hour_data["hour"]
            shadow_percent = _valid_shadow_percent(hour_data.get("shadow_percent"))
            if shadow_percent is None:
                raise ValueError("shadow_percent must be a finite number from 0 to 100")
            observation_class = hour_data.get(
                "observation_class"
            ) or classify_observation_class(
                fusion_mode=hour_data.get("fusion_mode"),
                ensemble_mode=hour_data.get("ensemble_mode"),
                root_cause=hour_data.get("root_cause"),
                group_name=group_name,
                evaluable=bool(hour_data.get("evaluable", True)),
                shadow_candidate=True,
            )
            if hour_data.get("root_cause") == "building_tree_obstruction":
                observation_class = (
                    hour_data.get("observation_class") or OBSERVATION_CONFIRMED_SENSOR
                )
            elif (
                hour_data.get("root_cause") == "possible_obstruction"
                and group_name != "_system_"
            ):
                observation_class = (
                    hour_data.get("observation_class") or OBSERVATION_PEER_CONFIRMED
                )
            shadow_detected = shadow_detected_from_observation(observation_class)
            if hour_data.get("root_cause") == "building_tree_obstruction":
                shadow_detected = True
            if not await self.execute_query(
                "INSERT INTO shadow_learning_history\n                   (group_name, date, hour, shadow_detected, shadow_type, shadow_percent,\n                    root_cause, confidence, sun_elevation_deg, cloud_cover_percent,\n                    theoretical_max_kwh, actual_kwh, efficiency_ratio, fusion_mode,\n                    observation_class, evaluable, learning_eligible)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, date, hour) DO UPDATE SET\n                       shadow_detected = excluded.shadow_detected,\n                       shadow_type = excluded.shadow_type,\n                       shadow_percent = excluded.shadow_percent,\n                       root_cause = excluded.root_cause,\n                       confidence = excluded.confidence,\n                       sun_elevation_deg = excluded.sun_elevation_deg,\n                       cloud_cover_percent = excluded.cloud_cover_percent,\n                       theoretical_max_kwh = excluded.theoretical_max_kwh,\n                       actual_kwh = excluded.actual_kwh,\n                       efficiency_ratio = excluded.efficiency_ratio,\n                       fusion_mode = excluded.fusion_mode,\n                       observation_class = excluded.observation_class,\n                       evaluable = excluded.evaluable,\n                       learning_eligible = excluded.learning_eligible",
                (
                    group_name,
                    date_str,
                    hour,
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
                    hour_data.get("fusion_mode"),
                    observation_class,
                    1 if hour_data.get("evaluable", True) else 0,
                    1 if hour_data.get("learning_eligible", True) else 0,
                ),
            ):
                raise RuntimeError("Shadow learning history write failed")
        except Exception as error:
            _LOGGER.error("Failed to save learning history: %s", error)
            raise

    async def _update_hourly_pattern(
        self, hour: int, hour_data: Dict[str, Any], group_name: str = "_system_"
    ) -> bool:
        """Update hourly pattern with new data using EMA. @zara V17.0.0

        Returns True if pattern classification changed.
        """
        try:
            shadow_percent = _valid_shadow_percent(hour_data.get("shadow_percent"))
            if shadow_percent is None:
                raise ValueError("shadow_percent must be a finite number from 0 to 100")
            observation_class = hour_data.get(
                "observation_class"
            ) or classify_observation_class(
                fusion_mode=hour_data.get("fusion_mode"),
                ensemble_mode=hour_data.get("ensemble_mode"),
                root_cause=hour_data.get("root_cause", "unknown"),
                group_name=group_name,
                evaluable=bool(hour_data.get("evaluable", True)),
                shadow_candidate=True,
            )
            shadow_detected = shadow_detected_from_observation(observation_class)
            if hour_data.get("root_cause") == "building_tree_obstruction":
                shadow_detected = True
            root_cause = hour_data.get("root_cause", "unknown")
            pattern_key = (group_name, hour)
            if pattern_key not in self._hourly_patterns:
                self._hourly_patterns[pattern_key] = HourlyPattern(
                    group_name=group_name, hour=hour
                )
            pattern = self._hourly_patterns[pattern_key]
            previous_type = pattern.pattern_type
            alpha = self._config.get("ema_alpha", EMA_ALPHA)
            pattern.sample_count += 1
            if shadow_detected:
                pattern.shadow_days += 1
            else:
                pattern.clear_days += 1
            occurrence_rate = pattern.shadow_days / pattern.sample_count
            if pattern.sample_count >= 3:
                pattern.shadow_occurrence_rate = (
                    alpha * occurrence_rate
                    + (1 - alpha) * pattern.shadow_occurrence_rate
                )
            else:
                pattern.shadow_occurrence_rate = occurrence_rate
            if shadow_detected:
                if pattern.shadow_days == 1:
                    pattern.avg_shadow_percent = shadow_percent
                    pattern.std_dev_shadow_percent = 0.0
                else:
                    previous_mean = pattern.avg_shadow_percent
                    pattern.avg_shadow_percent = (
                        alpha * shadow_percent + (1 - alpha) * previous_mean
                    )
                    deviation = shadow_percent - previous_mean
                    pattern.std_dev_shadow_percent = math.sqrt(
                        (1 - alpha)
                        * (pattern.std_dev_shadow_percent**2 + alpha * deviation**2)
                    )
            if shadow_detected:
                cause_alpha = (
                    1.0 / pattern.shadow_days if pattern.shadow_days > 0 else 0
                )
                cause_retention = 1.0 - cause_alpha
                pattern.pct_weather_clouds *= cause_retention
                pattern.pct_building_tree *= cause_retention
                pattern.pct_low_sun *= cause_retention
                pattern.pct_other *= cause_retention
                if root_cause == "weather_clouds":
                    pattern.pct_weather_clouds += cause_alpha
                elif root_cause == "building_tree_obstruction":
                    pattern.pct_building_tree += cause_alpha
                elif root_cause == "low_sun_angle":
                    pattern.pct_low_sun += cause_alpha
                else:
                    pattern.pct_other += cause_alpha
                cause_total = (
                    pattern.pct_weather_clouds
                    + pattern.pct_building_tree
                    + pattern.pct_low_sun
                    + pattern.pct_other
                )
                if cause_total > 0:
                    pattern.pct_weather_clouds /= cause_total
                    pattern.pct_building_tree /= cause_total
                    pattern.pct_low_sun /= cause_total
                    pattern.pct_other /= cause_total
            minimum_samples = self._config.get(
                "min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN
            )
            pattern.confidence = min(1.0, pattern.sample_count / (minimum_samples * 2))
            await self._save_hourly_pattern(pattern)
            return pattern.pattern_type != previous_type
        except Exception as error:
            _LOGGER.error(
                "Failed to update hourly pattern for hour %d: %s", hour, error
            )
            return False

    async def _save_hourly_pattern(self, pattern: HourlyPattern) -> None:
        """Save hourly pattern to database. @zara V17.0.0: group_name"""
        try:
            average_shadow_percent = _valid_shadow_percent(pattern.avg_shadow_percent)
            if average_shadow_percent is None:
                raise ValueError(
                    "avg_shadow_percent must be a finite number from 0 to 100"
                )
            if not await self.execute_query(
                "INSERT INTO shadow_pattern_hourly\n                   (group_name, hour, shadow_occurrence_rate, avg_shadow_percent,\n                    std_dev_shadow_percent, pct_weather_clouds, pct_building_tree,\n                    pct_low_sun, pct_other, pattern_type, confidence, sample_count,\n                    shadow_days, clear_days, first_learned, last_updated)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, hour) DO UPDATE SET\n                       shadow_occurrence_rate = excluded.shadow_occurrence_rate,\n                       avg_shadow_percent = excluded.avg_shadow_percent,\n                       std_dev_shadow_percent = excluded.std_dev_shadow_percent,\n                       pct_weather_clouds = excluded.pct_weather_clouds,\n                       pct_building_tree = excluded.pct_building_tree,\n                       pct_low_sun = excluded.pct_low_sun,\n                       pct_other = excluded.pct_other,\n                       pattern_type = excluded.pattern_type,\n                       confidence = excluded.confidence,\n                       sample_count = excluded.sample_count,\n                       shadow_days = excluded.shadow_days,\n                       clear_days = excluded.clear_days,\n                       last_updated = excluded.last_updated",
                (
                    pattern.group_name,
                    pattern.hour,
                    round(pattern.shadow_occurrence_rate, 4),
                    round(average_shadow_percent, 2),
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
                    (
                        SafeDateTimeUtil.now().date()
                        if pattern.sample_count == 1
                        else None
                    ),
                    SafeDateTimeUtil.now(),
                ),
            ):
                raise RuntimeError("Hourly shadow pattern write failed")
        except Exception as error:
            _LOGGER.error("Failed to save hourly pattern: %s", error)
            raise

    async def _update_seasonal_pattern(
        self,
        month: int,
        hour: int,
        hour_data: Dict[str, Any],
        group_name: str = "_system_",
    ) -> None:
        """Update seasonal pattern with new data. @zara V17.0.0: group_name"""
        try:
            shadow_percent = _valid_shadow_percent(hour_data.get("shadow_percent"))
            if shadow_percent is None:
                raise ValueError("shadow_percent must be a finite number from 0 to 100")
            observation_class = hour_data.get(
                "observation_class"
            ) or classify_observation_class(
                fusion_mode=hour_data.get("fusion_mode"),
                ensemble_mode=hour_data.get("ensemble_mode"),
                root_cause=hour_data.get("root_cause", "unknown"),
                group_name=group_name,
                evaluable=bool(hour_data.get("evaluable", True)),
                shadow_candidate=True,
            )
            shadow_detected = shadow_detected_from_observation(observation_class)
            if hour_data.get("root_cause") == "building_tree_obstruction":
                shadow_detected = True
            root_cause = hour_data.get("root_cause", "unknown")
            pattern_key = (group_name, month, hour)
            alpha = self._config.get("ema_alpha", EMA_ALPHA)
            if pattern_key not in self._seasonal_patterns:
                self._seasonal_patterns[pattern_key] = SeasonalPattern(
                    group_name=group_name, month=month, hour=hour
                )
            pattern = self._seasonal_patterns[pattern_key]
            pattern.sample_count += 1
            if shadow_detected:
                pattern.shadow_days += 1
            occurrence_rate = pattern.shadow_days / pattern.sample_count
            if pattern.sample_count >= 2:
                pattern.shadow_occurrence_rate = (
                    alpha * occurrence_rate
                    + (1 - alpha) * pattern.shadow_occurrence_rate
                )
            else:
                pattern.shadow_occurrence_rate = occurrence_rate
            if shadow_detected:
                if pattern.shadow_days == 1:
                    pattern.avg_shadow_percent = shadow_percent
                else:
                    pattern.avg_shadow_percent = (
                        alpha * shadow_percent
                        + (1 - alpha) * pattern.avg_shadow_percent
                    )
            if shadow_detected and root_cause != "unknown":
                pattern.dominant_cause = root_cause
            pattern.confidence = min(
                1.0, pattern.sample_count / (MIN_SAMPLES_FOR_SEASONAL * 2)
            )
            await self._save_seasonal_pattern(pattern)
        except Exception as error:
            _LOGGER.error(
                "Failed to update seasonal pattern for %d/%d: %s", month, hour, error
            )
        return None if True else None

    async def _save_seasonal_pattern(self, pattern: SeasonalPattern) -> None:
        """Save seasonal pattern to database. @zara V17.0.0: group_name"""
        try:
            average_shadow_percent = _valid_shadow_percent(pattern.avg_shadow_percent)
            if average_shadow_percent is None:
                raise ValueError(
                    "avg_shadow_percent must be a finite number from 0 to 100"
                )
            if not await self.execute_query(
                "INSERT INTO shadow_pattern_seasonal\n                   (group_name, month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                    std_dev_shadow_percent, dominant_cause, sample_count,\n                    shadow_days, confidence, last_updated)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(group_name, month, hour) DO UPDATE SET\n                       shadow_occurrence_rate = excluded.shadow_occurrence_rate,\n                       avg_shadow_percent = excluded.avg_shadow_percent,\n                       std_dev_shadow_percent = excluded.std_dev_shadow_percent,\n                       dominant_cause = excluded.dominant_cause,\n                       sample_count = excluded.sample_count,\n                       shadow_days = excluded.shadow_days,\n                       confidence = excluded.confidence,\n                       last_updated = excluded.last_updated",
                (
                    pattern.group_name,
                    pattern.month,
                    pattern.hour,
                    round(pattern.shadow_occurrence_rate, 4),
                    round(average_shadow_percent, 2),
                    round(pattern.std_dev_shadow_percent, 2),
                    pattern.dominant_cause,
                    pattern.sample_count,
                    pattern.shadow_days,
                    round(pattern.confidence, 3),
                    SafeDateTimeUtil.now(),
                ),
            ):
                raise RuntimeError("Seasonal shadow pattern write failed")
        except Exception as error:
            _LOGGER.error("Failed to save seasonal pattern: %s", error)
            raise

    async def _classify_all_patterns(self, group_name: Optional[str] = None) -> int:
        """Classify hourly patterns and return count of fixed obstructions. @zara V17.0.0"""
        fixed_obstruction_count = 0
        minimum_samples = self._config.get(
            "min_samples_for_pattern", MIN_SAMPLES_FOR_PATTERN
        )
        fixed_threshold = self._config.get(
            "fixed_obstruction_threshold", FIXED_OBSTRUCTION_THRESHOLD
        )
        for pattern_key, pattern in self._hourly_patterns.items():
            if group_name is not None:
                if pattern.group_name != group_name:
                    continue
            if pattern.sample_count < minimum_samples:
                pattern.pattern_type = PatternType.UNKNOWN
                continue
            occurrence_rate = pattern.shadow_occurrence_rate
            if occurrence_rate >= fixed_threshold:
                if pattern.std_dev_shadow_percent < 15.0:
                    pattern.pattern_type = PatternType.FIXED_OBSTRUCTION
                    fixed_obstruction_count += 1
                else:
                    pattern.pattern_type = PatternType.FREQUENT
            elif occurrence_rate >= FREQUENT_SHADOW_THRESHOLD:
                pattern.pattern_type = PatternType.FREQUENT
            elif occurrence_rate >= OCCASIONAL_SHADOW_THRESHOLD:
                pattern.pattern_type = PatternType.OCCASIONAL
            else:
                pattern.pattern_type = PatternType.NO_SHADOW
            await self._save_hourly_pattern(pattern)
            continue
        return fixed_obstruction_count

    async def _update_config_stats(
        self, date_str: str, hours_learned: int, fixed_obstructions: int
    ) -> None:
        """Update config statistics. @zara"""
        try:
            history_counts = await self.fetch_one(
                "SELECT COUNT(DISTINCT date), COUNT(*), MAX(date)\n                   FROM shadow_learning_history\n                   WHERE group_name = '_system_'"
            )
            self._config["total_days_learned"] = (
                int(history_counts[0] or 0) if history_counts else 0
            )
            self._config["total_hours_learned"] = (
                int(history_counts[1] or 0) if history_counts else 0
            )
            self._config["last_learning_date"] = (
                str(history_counts[2]) if history_counts and history_counts[2] else None
            )
            known_pattern_count = len(
                [
                    pattern
                    for pattern in self._hourly_patterns.values()
                    if pattern.pattern_type != PatternType.UNKNOWN
                ]
            )
            self._config["patterns_detected"] = known_pattern_count
            fixed_obstructions = sum(
                (
                    pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
                    for pattern in self._hourly_patterns.values()
                )
            )
            self._config["fixed_obstructions_detected"] = fixed_obstructions
            await self.execute_query(
                "INSERT INTO shadow_pattern_config\n                   (id, total_days_learned, total_hours_learned,\n                    last_learning_date, patterns_detected, fixed_obstructions_detected, version,\n                    updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       total_days_learned = excluded.total_days_learned,\n                       total_hours_learned = excluded.total_hours_learned,\n                       last_learning_date = excluded.last_learning_date,\n                       patterns_detected = excluded.patterns_detected,\n                       fixed_obstructions_detected = excluded.fixed_obstructions_detected,\n                       version = excluded.version,\n                       updated_at = excluded.updated_at",
                (
                    self._config["total_days_learned"],
                    self._config["total_hours_learned"],
                    self._config["last_learning_date"],
                    known_pattern_count,
                    fixed_obstructions,
                    SHADOW_PATTERN_MODEL_VERSION,
                    SafeDateTimeUtil.now(),
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
            "conditional_shadow_percent": 0.0,
            "occurrence_probability": 0.0,
            "local_obstruction_probability": 0.0,
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
        expected_shadow["expected_shadow"] = hourly_pattern.pattern_type in {
            PatternType.OCCASIONAL,
            PatternType.FREQUENT,
            PatternType.FIXED_OBSTRUCTION,
        }
        expected_shadow["conditional_shadow_percent"] = _bounded_shadow_percent(
            hourly_pattern.avg_shadow_percent
        )
        expected_shadow["occurrence_probability"] = (
            hourly_pattern.shadow_occurrence_rate
        )
        expected_shadow["local_obstruction_probability"] = (
            hourly_pattern.pct_building_tree
        )
        expected_shadow["is_fixed_obstruction"] = (
            hourly_pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        )
        expected_shadow["confidence"] = hourly_pattern.confidence
        cause_distribution = {
            "weather_clouds": hourly_pattern.pct_weather_clouds,
            "building_tree_obstruction": hourly_pattern.pct_building_tree,
            "low_sun_angle": hourly_pattern.pct_low_sun,
            "other": hourly_pattern.pct_other,
        }
        expected_shadow["dominant_cause"] = max(
            cause_distribution, key=cause_distribution.get
        )
        if month:
            seasonal_pattern = self._seasonal_patterns.get((group_name, month, hour))
            if (
                seasonal_pattern
                and seasonal_pattern.confidence > 0.3
                and (
                    seasonal_pattern.dominant_cause in SHADOW_LEARNING_LOCAL_ROOT_CAUSES
                )
            ):
                seasonal_weight = min(0.4, seasonal_pattern.confidence * 0.5)
                expected_shadow["conditional_shadow_percent"] = (
                    1 - seasonal_weight
                ) * _bounded_shadow_percent(
                    hourly_pattern.avg_shadow_percent
                ) + seasonal_weight * _bounded_shadow_percent(
                    seasonal_pattern.avg_shadow_percent
                )
                expected_shadow["occurrence_probability"] = (
                    (1 - seasonal_weight) * hourly_pattern.shadow_occurrence_rate
                    + seasonal_weight * seasonal_pattern.shadow_occurrence_rate
                )
                expected_shadow["dominant_cause"] = seasonal_pattern.dominant_cause
        if expected_shadow["expected_shadow"]:
            expected_shadow["expected_shadow_percent"] = (
                expected_shadow["conditional_shadow_percent"]
                * expected_shadow["occurrence_probability"]
            )
        expected_shadow["conditional_shadow_percent"] = _bounded_shadow_percent(
            expected_shadow["conditional_shadow_percent"]
        )
        expected_shadow["expected_shadow_percent"] = _bounded_shadow_percent(
            expected_shadow["expected_shadow_percent"]
        )
        return expected_shadow

    def is_fixed_obstruction_hour(
        self, hour: int, group_name: str = "_system_"
    ) -> bool:
        "Check if an hour is classified as having a fixed obstruction. @zara V17.0.0\n\nArgs:\n    hour: Hour to check\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    True if hour has a learned fixed obstruction pattern\n"
        pattern = self._hourly_patterns.get((group_name, hour))
        return (
            pattern is not None
            and pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        )

    def get_all_fixed_obstruction_hours(
        self, group_name: str = "_system_"
    ) -> List[int]:
        "Get all hours classified as having fixed obstructions. @zara V17.0.0\n\nArgs:\n    group_name: Panel group name ('_system_' for system-wide)\n\nReturns:\n    List of hours with fixed obstruction patterns\n"
        return [
            pattern.hour
            for pattern_key, pattern in self._hourly_patterns.items()
            if pattern_key[0] == group_name
            if pattern.pattern_type == PatternType.FIXED_OBSTRUCTION
        ]

    def get_learning_summary(self) -> Dict[str, Any]:
        "Get summary of learned patterns. @zara\n\nReturns:\n    Dictionary with learning statistics\n"
        pattern_counts = {pattern_type.value: 0 for pattern_type in PatternType}
        for pattern in self._hourly_patterns.values():
            pattern_counts[pattern.pattern_type.value] += 1
        fixed_hours = self.get_all_fixed_obstruction_hours()
        return {
            "initialized": self._initialized,
            "total_days_learned": self._config.get("total_days_learned", 0),
            "total_hours_learned": self._config.get("total_hours_learned", 0),
            "last_learning_date": self._config.get("last_learning_date"),
            "hourly_patterns_count": len(self._hourly_patterns),
            "seasonal_patterns_count": len(self._seasonal_patterns),
            "patterns_by_type": pattern_counts,
            "fixed_obstruction_hours": fixed_hours,
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
        """Recalculate all patterns from learning history. @zara V17.0.0

        Useful after parameter changes or data corrections.
        Now includes group_name dimension.

        Args:
            days: Number of days of history to use

        Returns:
            LearningResult with recalculation status
        """
        previous_window_days = self._config.get(
            "rolling_window_days", ROLLING_WINDOW_DAYS
        )
        try:
            rows = await self.fetch_all(
                "SELECT group_name, MAX(date), COUNT(*)\n                   FROM shadow_learning_history\n                   GROUP BY group_name\n                   ORDER BY group_name"
            )
            if not rows:
                return LearningResult(
                    success=False, message="No history data available for recalculation"
                )
            self._config["rolling_window_days"] = max(1, int(days))
            patterns_updated = 0
            fixed_obstruction_count = 0
            hours_learned = 0
            for row in rows:
                group_name = row[0] or "_system_"
                reference_date = datetime.strptime(str(row[1]), "%Y-%m-%d").date()
                async with self.db.transaction():
                    group_patterns_updated, group_fixed_obstructions = (
                        await self._rebuild_group_patterns(group_name, reference_date)
                    )
                patterns_updated += group_patterns_updated
                fixed_obstruction_count += group_fixed_obstructions
                hours_learned += int(row[2] or 0)
            await self._update_config_stats("", 0, fixed_obstruction_count)
            return LearningResult(
                success=True,
                message=f"Recalculated {len(rows)} scopes from history",
                hours_learned=hours_learned,
                patterns_updated=patterns_updated,
                fixed_obstructions_found=fixed_obstruction_count,
            )
        except Exception as error:
            _LOGGER.error("Recalculation failed: %s", error)
            return LearningResult(success=False, message=str(error))
        finally:
            self._config["rolling_window_days"] = previous_window_days


_shadow_pattern_learner: Optional[ShadowPatternLearner] = None


def get_shadow_pattern_learner(
    hass: HomeAssistant, db_manager: DatabaseManager
) -> ShadowPatternLearner:
    """Get or create shadow pattern learner instance. @zara"""
    global _shadow_pattern_learner
    if _shadow_pattern_learner is None:
        _shadow_pattern_learner = ShadowPatternLearner(hass, db_manager)
    return _shadow_pattern_learner
