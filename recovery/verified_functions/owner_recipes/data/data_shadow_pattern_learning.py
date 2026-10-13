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
