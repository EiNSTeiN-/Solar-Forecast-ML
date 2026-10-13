# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event


class BatteryTracker:

    async def _async_save_data(self) -> None:
        """Save statistics to database with retry for SMB/NFS locking @zara"""
        if not self._db:
            return
        import asyncio as _var_var_16

        for _var_var_17 in range(3):
            try:
                await self._db.execute(
                    "INSERT INTO GPM_battery_stats\n                       (id, energy_today_wh, energy_week_wh, energy_month_wh,\n                        current_day, current_week, current_month)\n                       VALUES (1, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(id) DO UPDATE SET\n                           energy_today_wh = excluded.energy_today_wh,\n                           energy_week_wh = excluded.energy_week_wh,\n                           energy_month_wh = excluded.energy_month_wh,\n                           current_day = excluded.current_day,\n                           current_week = excluded.current_week,\n                           current_month = excluded.current_month",
                    (
                        self._energy_today_wh,
                        self._energy_week_wh,
                        self._energy_month_wh,
                        self._current_day,
                        self._current_week,
                        self._current_month,
                    ),
                )
            except Exception as _var_var_12:
                if "locked" in str(_var_var_12).lower() and _var_var_17 < 2:
                    _LOGGER.debug(
                        "DB locked on save attempt %d, retrying...", _var_var_17 + 1
                    )
                    await _var_var_16.sleep(1 + _var_var_17)
                    continue
                else:
                    _LOGGER.error("Failed to save battery statistics: %s", _var_var_12)
                    continue
            return
