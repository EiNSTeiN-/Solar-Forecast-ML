# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class ShadowPatternLearner(DataManagerIO):

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
            _var_var_10 = await self._get_shadow_data_for_date(date_str, group_name)
            if not _var_var_10:
                _LOGGER.debug(
                    "No shadow data for %s/%s - skipping learning", date_str, group_name
                )
                return LearningResult(
                    success=False,
                    message="No shadow detection data available",
                    date=date_str,
                )
            _var_var_11 = datetime.strptime(date_str, "%Y-%m-%d")
            _var_var_9 = _var_var_11.month
            _var_var_12 = 0
            _var_var_13 = 0
            for _var_var_14 in _var_var_10:
                _var_var_7 = _var_var_14.get("hour")
                if _var_var_7 is None:
                    continue
                await self._save_learning_history(date_str, _var_var_14, group_name)
                _var_var_15 = await self._update_hourly_pattern(
                    _var_var_7, _var_var_14, group_name
                )
                if _var_var_15:
                    _var_var_13 += 1
                await self._update_seasonal_pattern(
                    _var_var_9, _var_var_7, _var_var_14, group_name
                )
                _var_var_12 += 1
            _var_var_16 = await self._classify_all_patterns(group_name)
            await self._update_config_stats(date_str, _var_var_12, _var_var_16)
            _LOGGER.info(
                "Shadow pattern learning for %s [%s]: %d hours, %d patterns updated, %d fixed obstructions",
                date_str,
                group_name,
                _var_var_12,
                _var_var_13,
                _var_var_16,
            )
            return LearningResult(
                success=True,
                message=f"Learned from {_var_var_12} hours",
                hours_learned=_var_var_12,
                patterns_updated=_var_var_13,
                fixed_obstructions_found=_var_var_16,
                date=date_str,
            )
        except Exception as _var_var_3:
            _LOGGER.error(
                "Shadow pattern learning failed for %s: %s", date_str, _var_var_3
            )
            return LearningResult(success=False, message=str(_var_var_3), date=date_str)
