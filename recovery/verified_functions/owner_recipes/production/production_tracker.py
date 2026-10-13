# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..core.core_user_messages import user_msg
from ..const import DATA_KEY_PEAK_TODAY, PEAK_TODAY_AT, PEAK_TODAY_POWER_W


class ProductionTimeCalculator:

    async def _load_persistent_state(self) -> bool:
        """Load production time state from database. @zara"""
        if not self.db:
            return False
        try:
            _var_var_0 = await self.db.fetchone(
                "SELECT date, accumulated_hours, is_active, start_time, production_time_today\n                   FROM production_time_state WHERE id = 1"
            )
            if not _var_var_0:
                return False
            _var_var_1 = str(_var_var_0[0])
            _var_var_2 = dt_util.now().date().isoformat()
            if _var_var_1 != _var_var_2:
                _LOGGER.info(
                    "Persistent state is from %s, not loading (today is %s)",
                    _var_var_1,
                    _var_var_2,
                )
                return False
            self._accumulated_hours = _var_var_0[1] or 0.0
            self._is_active = bool(_var_var_0[2])
            if self._is_active and _var_var_0[3]:
                try:
                    _var_var_3 = str(_var_var_0[3])
                    self._start_time = dt_util.parse_datetime(_var_var_3)
                    if self._start_time:
                        self._start_time = dt_util.ensure_local(self._start_time)
                except Exception as _var_var_4:
                    _LOGGER.warning("Could not restore start_time: %s", _var_var_4)
                    self._is_active = False
                    self._start_time = None
            _LOGGER.debug(
                "Loaded production time state: accumulated=%.2fh, active=%s, start_time=%s",
                self._accumulated_hours,
                self._is_active,
                self._start_time.isoformat() if self._start_time else "None",
            )
            return True
        except Exception as _var_var_4:
            _LOGGER.error("Failed to load production time state: %s", _var_var_4)
            return False

    async def stop_tracking(self) -> None:
        """Stop all tracking listeners and save final state. @zara"""
        try:
            if self.db and self._needs_save:
                await self._save_state_async()
            if self._state_listener_remove:
                try:
                    self._state_listener_remove()
                except ValueError:
                    pass
                self._state_listener_remove = None
            if self._sot_listener_remove:
                try:
                    self._sot_listener_remove()
                except ValueError:
                    pass
                self._sot_listener_remove = None
            if self._midnight_listener_remove:
                try:
                    self._midnight_listener_remove()
                except ValueError:
                    pass
                self._midnight_listener_remove = None
            if self._periodic_save_listener_remove:
                try:
                    self._periodic_save_listener_remove()
                except ValueError:
                    pass
                self._periodic_save_listener_remove = None
            if self._is_active:
                _var_var_5 = dt_util.now()
                self._stop_production_tracking(_var_var_5)
                if self.db:
                    await self._save_state_async()
            _LOGGER.info("Production time tracking stopped")
        except Exception as _var_var_4:
            _LOGGER.error(
                "Error stopping production time tracking: %s", _var_var_4, exc_info=True
            )
        return None if False else None
