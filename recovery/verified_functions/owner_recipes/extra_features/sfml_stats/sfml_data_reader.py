# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite
from .utils.time_utils import ha_today


class SFMLDataReader:

    async def get_daily_yield_from_hourly(
        self, date: str | None = None
    ) -> dict[str, Any]:
        """Get daily yield totals by summing panel group actuals. @zara"""
        if not self.is_available:
            return {}
        if date is None:
            date = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as _var_var_7:
                async with _var_var_7.execute(
                    "\n                    SELECT ppg.group_name,\n                           SUM(ppg.actual_kwh) as total\n                    FROM prediction_panel_groups ppg\n                    JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                    WHERE hp.target_date = ?\n                    GROUP BY ppg.group_name\n                ",
                    (date,),
                ) as _var_var_8:
                    _var_var_13 = await _var_var_8.fetchall()
            if not _var_var_13:
                return {}
            _var_var_26 = {
                _var_var_9["group_name"]: _var_var_9["total"] or 0.0
                for _var_var_9 in _var_var_13
            }
            return {
                "date": date,
                "yield_total_kwh": sum(_var_var_26.values()),
                "groups": _var_var_26,
            }
        except Exception as _var_var_10:
            self._log_read_error(
                "Error reading daily yield from prediction_panel_groups", _var_var_10
            )
        return {} if True else None
