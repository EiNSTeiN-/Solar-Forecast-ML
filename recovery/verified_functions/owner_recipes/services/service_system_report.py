# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from ..data.db_manager import DatabaseManager


class SystemReportGenerator:

    async def _load_physics_telemetry(self) -> Dict[str, Any]:
        """Load diagnosis-only physics and GHI geometry evidence for the report."""
        _var_var_12 = {
            "contract_versions": [],
            "geometry": None,
            "sectors": [],
            "hourly_shape": [],
            "sample_kinds": [],
            "clamp_kinds": [],
        }
        if not self._db:
            return _var_var_12
        try:
            _var_var_13 = dict(_var_var_12)
            if await self._table_exists("physics_calibration_groups"):
                _var_var_6 = await self._db.fetchall(
                    "SELECT learning_contract_version, COUNT(*)\n                       FROM physics_calibration_groups\n                       GROUP BY learning_contract_version"
                )
                _var_var_13["contract_versions"] = [
                    {
                        "version": _var_var_7[0] or "unset",
                        "groups": int(_var_var_7[1] or 0),
                    }
                    for _var_var_7 in _var_var_6
                ]
            if await self._table_exists("ghi_sensor_geometry_daily"):
                _var_var_7 = await self._db.fetchone(
                    "SELECT computed_on, lookback_days, sample_hours, bin_count,\n                              overlapping_elev_bins, max_east_west_k_p90_ratio,\n                              diagnosis\n                       FROM ghi_sensor_geometry_daily\n                       ORDER BY computed_on DESC\n                       LIMIT 1"
                )
                if _var_var_7:
                    _var_var_13["geometry"] = {
                        "computed_on": _var_var_7[0],
                        "lookback_days": _var_var_7[1],
                        "sample_hours": _var_var_7[2],
                        "bin_count": _var_var_7[3],
                        "overlapping_elev_bins": _var_var_7[4],
                        "max_east_west_k_p90_ratio": _var_var_7[5],
                        "diagnosis": _var_var_7[6],
                    }
            _var_var_14 = _var_var_13.get("geometry")
            if _var_var_14:
                if await self._table_exists("ghi_sensor_geometry_sectors"):
                    _var_var_6 = await self._db.fetchall(
                        "SELECT elev_bin, sector, sample_count, k_avg, k_p90, k_max\n                       FROM ghi_sensor_geometry_sectors\n                       WHERE computed_on = ?\n                       ORDER BY elev_bin, sector",
                        (_var_var_14["computed_on"],),
                    )
                    _var_var_13["sectors"] = [
                        {
                            "elev_bin": int(_var_var_7[0]),
                            "sector": _var_var_7[1],
                            "sample_count": int(_var_var_7[2] or 0),
                            "k_avg": _var_var_7[3],
                            "k_p90": _var_var_7[4],
                            "k_max": _var_var_7[5],
                        }
                        for _var_var_7 in _var_var_6
                    ]
            if await self._table_exists("physics_calibration_hourly_shape"):
                _var_var_6 = await self._db.fetchall(
                    "SELECT s.date, s.group_name, s.min_hour, s.max_hour,\n                              s.hour_count, s.min_factor, s.max_factor, s.spread\n                       FROM physics_calibration_hourly_shape s\n                       JOIN (\n                           SELECT group_name, MAX(date) AS date\n                           FROM physics_calibration_hourly_shape\n                           GROUP BY group_name\n                       ) latest\n                         ON latest.group_name = s.group_name\n                        AND latest.date = s.date\n                       ORDER BY s.group_name"
                )
                _var_var_13["hourly_shape"] = [
                    {
                        "date": _var_var_7[0],
                        "group_name": _var_var_7[1],
                        "min_hour": _var_var_7[2],
                        "max_hour": _var_var_7[3],
                        "hour_count": _var_var_7[4],
                        "min_factor": _var_var_7[5],
                        "max_factor": _var_var_7[6],
                        "spread": _var_var_7[7],
                    }
                    for _var_var_7 in _var_var_6
                ]
            if await self._table_exists("physics_calibration_samples"):
                _var_var_6 = await self._db.fetchall(
                    "SELECT physics_ref_kind, COUNT(*), SUM(ratio_clamp_hit)\n                       FROM physics_calibration_samples\n                       GROUP BY physics_ref_kind\n                       ORDER BY physics_ref_kind"
                )
                _var_var_13["sample_kinds"] = [
                    {
                        "kind": _var_var_7[0],
                        "samples": int(_var_var_7[1] or 0),
                        "ratio_clamp_hits": int(_var_var_7[2] or 0),
                    }
                    for _var_var_7 in _var_var_6
                ]
            if await self._table_exists("physics_calibration_clamp_hits"):
                _var_var_6 = await self._db.fetchall(
                    "SELECT clamp_kind, COUNT(*)\n                       FROM physics_calibration_clamp_hits\n                       GROUP BY clamp_kind\n                       ORDER BY clamp_kind"
                )
                _var_var_13["clamp_kinds"] = [
                    {"kind": _var_var_7[0], "count": int(_var_var_7[1] or 0)}
                    for _var_var_7 in _var_var_6
                ]
            return _var_var_13
        except Exception as _var_var_5:
            _LOGGER.warning(
                "Physics telemetry unavailable for system report: %s", _var_var_5
            )
            return _var_var_12

    def _calculate_seasonal_stats(
        self, history: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate production statistics per season. @zara"""
        _var_var_10 = {
            "winter": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "spring": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "summer": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "autumn": {"days": 0, "total": 0, "best_day": 0, "values": []},
        }
        for _var_var_68 in history:
            _var_var_69 = _var_var_68.get("date", "")
            _var_var_70 = _var_var_68.get("actual_kwh") or 0
            if not _var_var_69 or _var_var_70 <= 0:
                continue
            try:
                _var_var_71 = datetime.strptime(_var_var_69, "%Y-%m-%d")
                _var_var_72 = _var_var_71.month
            except ValueError:
                continue
            _var_var_30 = None
            for _var_var_73, _var_var_74 in SEASONS.items():
                if not _var_var_72 in _var_var_74:
                    continue
                _var_var_30 = _var_var_73
                break
            if not _var_var_30:
                continue
            _var_var_10[_var_var_30]["days"] += 1
            _var_var_10[_var_var_30]["total"] += _var_var_70
            _var_var_10[_var_var_30]["values"].append(_var_var_70)
            if not _var_var_70 > _var_var_10[_var_var_30]["best_day"]:
                continue
            _var_var_10[_var_var_30]["best_day"] = _var_var_70
            continue
        for _var_var_30 in _var_var_10:
            if _var_var_10[_var_var_30]["days"] > 0:
                _var_var_10[_var_var_30]["avg_daily"] = (
                    _var_var_10[_var_var_30]["total"] / _var_var_10[_var_var_30]["days"]
                )
            else:
                _var_var_10[_var_var_30]["avg_daily"] = 0
        return _var_var_10
