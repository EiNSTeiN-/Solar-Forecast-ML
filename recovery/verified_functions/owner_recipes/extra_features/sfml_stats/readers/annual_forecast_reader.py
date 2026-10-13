# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, TYPE_CHECKING
import aiosqlite
from ..const import SOLAR_FORECAST_DB
from ..utils.time_utils import ha_today


class AnnualForecastReader:

    async def _get_calibration_data(
        self, conn: aiosqlite.Connection
    ) -> list[CalibrationData]:
        """Read panel group calibration factors. @zara"""
        _var_var_31 = []
        try:
            _var_var_32 = []
            try:
                async with conn.execute(
                    "SELECT group_name\n                       FROM panel_group_config_snapshot\n                       WHERE group_name IS NOT NULL\n                         AND TRIM(group_name) != ''\n                       ORDER BY group_name"
                ) as _var_var_29:
                    for _var_var_30 in await _var_var_29.fetchall():
                        _var_var_33 = _var_var_30["group_name"]
                        if _var_var_33 not in _var_var_32:
                            _var_var_32.append(_var_var_33)
            except Exception as _var_var_5:
                _LOGGER.debug(
                    "Could not load panel group config snapshot: %s", _var_var_5
                )
            if not _var_var_32:
                try:
                    async with conn.execute(
                        "SELECT DISTINCT group_name\n                           FROM astronomy_cache_panel_groups\n                           WHERE group_name IS NOT NULL\n                             AND TRIM(group_name) != ''"
                    ) as _var_var_29:
                        for _var_var_30 in await _var_var_29.fetchall():
                            _var_var_33 = _var_var_30["group_name"]
                            if _var_var_33 not in _var_var_32:
                                _var_var_32.append(_var_var_33)
                except Exception as _var_var_5:
                    _LOGGER.debug(
                        "Could not load valid panel groups for annual calibration filter: %s",
                        _var_var_5,
                    )
            async with conn.execute(
                "SELECT group_name, global_factor, sample_count, confidence\n                   FROM physics_calibration_groups"
            ) as _var_var_29:
                _var_var_34 = await _var_var_29.fetchall()
                for _var_var_30 in _var_var_34:
                    _var_var_33 = _var_var_30["group_name"]
                    if not _var_var_33:
                        continue
                    if _var_var_32:
                        if _var_var_33 not in _var_var_32:
                            continue
                    _var_var_31.append(
                        CalibrationData(
                            group_name=_var_var_33,
                            global_factor=_var_var_30["global_factor"],
                            sample_count=_var_var_30["sample_count"],
                            confidence=_var_var_30["confidence"],
                        )
                    )
            _var_var_36 = {_var_var_35.group_name for _var_var_35 in _var_var_31}
            for _var_var_33 in _var_var_32:
                if _var_var_33 in _var_var_36:
                    continue
                _var_var_31.append(
                    CalibrationData(
                        group_name=_var_var_33,
                        global_factor=1.0,
                        sample_count=0,
                        confidence=0.0,
                    )
                )
        except Exception as _var_var_5:
            _LOGGER.debug("Error reading calibration data: %s", _var_var_5)
        _var_var_31.sort(key=lambda item: _panel_group_sort_key(item.group_name))
        return _var_var_31
