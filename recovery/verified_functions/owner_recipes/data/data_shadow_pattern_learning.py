# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class ShadowPatternLearner(DataManagerIO):

    async def _recalculate_repaired_group_patterns(self, days: int) -> LearningResult:
        _var_var_12 = self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS)
        try:
            _var_var_13 = await self.fetch_all(
                "SELECT group_name, MAX(date), COUNT(*)\n                   FROM shadow_learning_history\n                   WHERE group_name <> '_system_'\n                   GROUP BY group_name\n                   ORDER BY group_name"
            )
            self._config["rolling_window_days"] = max(1, int(days))
            _var_var_14 = 0
            _var_var_8 = 0
            _var_var_15 = 0
            for _var_var_10 in _var_var_13:
                _var_var_16 = datetime.strptime(str(_var_var_10[1]), "%Y-%m-%d").date()
                _var_var_17, _var_var_18 = await self._rebuild_group_patterns(
                    str(_var_var_10[0]), _var_var_16
                )
                _var_var_14 += _var_var_17
                _var_var_8 += _var_var_18
                _var_var_15 += int(_var_var_10[2] or 0)
            return LearningResult(
                success=True,
                message=f"Recalculated {len(_var_var_13)} repaired group scopes",
                hours_learned=_var_var_15,
                patterns_updated=_var_var_14,
                fixed_obstructions_found=_var_var_8,
            )
        except Exception as _var_var_19:
            return LearningResult(success=False, message=str(_var_var_19))
        finally:
            self._config["rolling_window_days"] = _var_var_12

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
            _var_var_35 = await self._get_shadow_data_for_date(date_str, group_name)
            if not _var_var_35:
                _LOGGER.debug(
                    "No shadow data for %s/%s - skipping learning", date_str, group_name
                )
                return LearningResult(
                    success=False,
                    message="No shadow detection data available",
                    date=date_str,
                    reason_code="shadow_detection_data_unavailable",
                )
            _var_var_15 = 0
            for _var_var_36 in _var_var_35:
                _var_var_32 = _var_var_36.get("hour")
                if _var_var_32 is None:
                    continue
                await self._save_learning_history(date_str, _var_var_36, group_name)
                _var_var_15 += 1
            _var_var_16 = datetime.strptime(date_str, "%Y-%m-%d").date()
            async with self.db.transaction():
                _var_var_14, _var_var_37 = await self._rebuild_group_patterns(
                    group_name, _var_var_16
                )
            await self._update_config_stats(date_str, _var_var_15, _var_var_37)
            _LOGGER.info(
                "Shadow pattern learning for %s [%s]: %d hours, %d patterns updated, %d fixed obstructions",
                date_str,
                group_name,
                _var_var_15,
                _var_var_14,
                _var_var_37,
            )
            return LearningResult(
                success=True,
                message=f"Learned from {_var_var_15} hours",
                hours_learned=_var_var_15,
                patterns_updated=_var_var_14,
                fixed_obstructions_found=_var_var_37,
                date=date_str,
            )
        except Exception as _var_var_9:
            _LOGGER.error(
                "Shadow pattern learning failed for %s: %s", date_str, _var_var_9
            )
            return LearningResult(
                success=False,
                message=str(_var_var_9),
                date=date_str,
                reason_code="shadow_pattern_learning_failed",
            )

    async def _get_shadow_data_for_date(
        self, date_str: str, group_name: str = "_system_"
    ) -> List[Dict[str, Any]]:
        """Get shadow detection data for a specific date. @zara V17.0.0"""
        try:
            if group_name != "_system_":
                _var_var_13 = await self.fetch_all(
                    "SELECT hp.target_hour, sdg.shadow_type, sdg.shadow_percent,\n                              sdg.root_cause, sdg.confidence, sdg.efficiency_ratio,\n                              sdg.theoretical_max_kwh, sdg.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover,\n                              hp.exclude_from_learning, hp.mppt_throttle_reason,\n                              hp.mppt_throttled, hp.inverter_clipped,\n                              hp.weather_alert_type, hp.actual_backfill_blocked,\n                              hp.data_quarantine_reason, NULL as fusion_mode,\n                              NULL as ensemble_mode, NULL as observation_class,\n                              NULL as evaluable, NULL as learning_eligible\n                       FROM hourly_predictions hp\n                       JOIN shadow_detection_groups sdg ON hp.prediction_id = sdg.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND sdg.group_name = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                       ORDER BY hp.target_hour",
                    (date_str, group_name),
                )
            else:
                _var_var_13 = await self.fetch_all(
                    "SELECT hp.target_hour, hsd.shadow_type, hsd.shadow_percent,\n                              hsd.root_cause, hsd.confidence, hsd.efficiency_ratio,\n                              hsd.theoretical_max_kwh, hp.actual_kwh,\n                              pa.sun_elevation_deg,\n                              (SELECT clouds FROM weather_forecast wf\n                               WHERE wf.forecast_date = hp.target_date\n                                 AND wf.hour = hp.target_hour LIMIT 1) as cloud_cover,\n                              hp.exclude_from_learning, hp.mppt_throttle_reason,\n                              hp.mppt_throttled, hp.inverter_clipped,\n                              hp.weather_alert_type, hp.actual_backfill_blocked,\n                              hp.data_quarantine_reason, hsd.fusion_mode,\n                              hsd.ensemble_mode, hsd.observation_class,\n                              hsd.evaluable, hsd.learning_eligible\n                       FROM hourly_predictions hp\n                       JOIN hourly_shadow_detection hsd ON hp.prediction_id = hsd.prediction_id\n                       LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                       WHERE hp.target_date = ?\n                         AND hp.target_hour >= 6 AND hp.target_hour <= 20\n                       ORDER BY hp.target_hour",
                    (date_str,),
                )
            _var_var_5 = []
            for _var_var_10 in _var_var_13:
                _var_var_36 = {
                    "hour": _var_var_10[0],
                    "shadow_type": _var_var_10[1],
                    "shadow_percent": _var_var_10[2] or 0.0,
                    "root_cause": _var_var_10[3],
                    "confidence": _var_var_10[4] or 0.0,
                    "efficiency_ratio": _var_var_10[5] or 0.0,
                    "theoretical_max_kwh": _var_var_10[6],
                    "actual_kwh": _var_var_10[7],
                    "sun_elevation_deg": _var_var_10[8],
                    "cloud_cover_percent": _var_var_10[9],
                    "exclude_from_learning": (
                        bool(_var_var_10[10]) if _var_var_10[10] is not None else False
                    ),
                    "mppt_throttle_reason": _var_var_10[11],
                    "mppt_throttled": (
                        bool(_var_var_10[12]) if _var_var_10[12] is not None else False
                    ),
                    "inverter_clipped": (
                        bool(_var_var_10[13]) if _var_var_10[13] is not None else False
                    ),
                    "weather_alert_type": _var_var_10[14],
                    "actual_backfill_blocked": (
                        bool(_var_var_10[15]) if _var_var_10[15] is not None else False
                    ),
                    "data_quarantine_reason": _var_var_10[16],
                    "fusion_mode": _var_var_10[17] if len(_var_var_10) > 17 else None,
                    "ensemble_mode": _var_var_10[18] if len(_var_var_10) > 18 else None,
                    "observation_class": (
                        _var_var_10[19] if len(_var_var_10) > 19 else None
                    ),
                    "evaluable": (
                        bool(_var_var_10[20])
                        if len(_var_var_10) > 20 and _var_var_10[20] is not None
                        else True
                    ),
                    "learning_eligible": (
                        bool(_var_var_10[21])
                        if len(_var_var_10) > 21 and _var_var_10[21] is not None
                        else True
                    ),
                }
                if self._is_shadow_learning_eligible(_var_var_36):
                    _var_var_5.append(_var_var_36)
                    continue
            return _var_var_5
        except Exception as _var_var_9:
            _LOGGER.error(
                "Failed to get shadow data for %s [%s]: %s",
                date_str,
                group_name,
                _var_var_9,
            )
            raise

    async def recalculate_from_history(self, days: int = 30) -> LearningResult:
        """Recalculate all patterns from learning history. @zara V17.0.0

        Useful after parameter changes or data corrections.
        Now includes group_name dimension.

        Args:
            days: Number of days of history to use

        Returns:
            LearningResult with recalculation status
        """
        _var_var_12 = self._config.get("rolling_window_days", ROLLING_WINDOW_DAYS)
        try:
            _var_var_13 = await self.fetch_all(
                "SELECT group_name, MAX(date), COUNT(*)\n                   FROM shadow_learning_history\n                   GROUP BY group_name\n                   ORDER BY group_name"
            )
            if not _var_var_13:
                return LearningResult(
                    success=False, message="No history data available for recalculation"
                )
            self._config["rolling_window_days"] = max(1, int(days))
            _var_var_14 = 0
            _var_var_8 = 0
            _var_var_15 = 0
            for _var_var_10 in _var_var_13:
                _var_var_31 = _var_var_10[0] or "_system_"
                _var_var_16 = datetime.strptime(str(_var_var_10[1]), "%Y-%m-%d").date()
                async with self.db.transaction():
                    _var_var_17, _var_var_18 = await self._rebuild_group_patterns(
                        _var_var_31, _var_var_16
                    )
                _var_var_14 += _var_var_17
                _var_var_8 += _var_var_18
                _var_var_15 += int(_var_var_10[2] or 0)
            await self._update_config_stats("", 0, _var_var_8)
            return LearningResult(
                success=True,
                message=f"Recalculated {len(_var_var_13)} scopes from history",
                hours_learned=_var_var_15,
                patterns_updated=_var_var_14,
                fixed_obstructions_found=_var_var_8,
            )
        except Exception as _var_var_9:
            _LOGGER.error("Recalculation failed: %s", _var_var_9)
            return LearningResult(success=False, message=str(_var_var_9))
        finally:
            self._config["rolling_window_days"] = _var_var_12
