# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo
import aiosqlite


class EnergyCorrectionStore:

    async def _raw_day(self, day: date, mode: str) -> tuple[dict[str, float], set[str]]:
        _var_var_95 = {_var_var_3["id"]: 0.0 for _var_var_3 in METRIC_REGISTRY}
        _var_var_96 = []
        for _var_var_3 in METRIC_REGISTRY:
            _var_var_97 = _var_var_3["daily_column"]
            if not _COLUMN.fullmatch(_var_var_97):
                raise CorrectionError(
                    "unsupported_metric", "This daily energy metric is not supported."
                )
            _var_var_96.append(_var_var_97)
        _var_var_98 = f"file:{quote(str(self._solar_path), safe='/')}?mode=ro"
        try:
            async with aiosqlite.connect(_var_var_98, uri=True) as _var_var_0:
                _var_var_0.row_factory = aiosqlite.Row
                if mode != "backfill":
                    _var_var_99 = await self._one(
                        _var_var_0,
                        "SELECT 1 present FROM daily_summaries WHERE date=? LIMIT 1",
                        (day.isoformat(),),
                    )
                    if _var_var_99 is None:
                        raise CorrectionError(
                            "eod_not_finalized", "No finalized daily summary exists."
                        )
                _var_var_100 = await self._all(
                    _var_var_0, "PRAGMA table_info(stats_daily_energy)"
                )
                _var_var_101 = {_var_var_1["name"] for _var_var_1 in _var_var_100}
                _var_var_102 = [
                    _var_var_97
                    for _var_var_97 in _var_var_96
                    if _var_var_97 in _var_var_101
                ]
                if not _var_var_102:
                    if mode == "backfill":
                        return (_var_var_95, set())
                    raise CorrectionError(
                        "source_value_unavailable", "Daily metric is unavailable."
                    )
                _var_var_1 = await self._one(
                    _var_var_0,
                    f"SELECT {', '.join(_var_var_102)} FROM stats_daily_energy WHERE date=?",
                    (day.isoformat(),),
                )
        except CorrectionError:
            raise
        except (OSError, aiosqlite.Error) as _var_var_46:
            raise CorrectionError(
                "source_unavailable", "Read-only energy source is unavailable."
            ) from _var_var_46
        if _var_var_1 is None:
            _var_var_103 = {
                _var_var_3["id"]
                for _var_var_3 in METRIC_REGISTRY
                if _var_var_3["daily_column"] in _var_var_101
            }
            if mode == "backfill":
                return (_var_var_95, _var_var_103)
            raise CorrectionError(
                "source_value_unavailable", "Daily metric is unavailable."
            )
        _var_var_104 = dict(_var_var_95)
        _var_var_81 = set()
        for _var_var_3 in METRIC_REGISTRY:
            _var_var_97 = _var_var_3["daily_column"]
            if _var_var_97 not in _var_var_101:
                continue
            _var_var_81.add(_var_var_3["id"])
            _var_var_82 = _var_var_1[_var_var_97]
            if _var_var_82 is None:
                continue
            _var_var_105 = float(_var_var_82)
            if math.isfinite(_var_var_105):
                _var_var_104[_var_var_3["id"]] = _var_var_105
        return (_var_var_104, _var_var_81)
