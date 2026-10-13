# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Callable
from ..data.db_manager import DatabaseManager
from .core_helpers import SafeDateTimeUtil as dt_util


class StartupDataResolver:

    async def _sensor_retry_loop(self) -> None:
        """Background loop to retry sensor collection."""
        while not self._shutdown and self._retry_count < self._max_retries:
            try:
                await asyncio.sleep(self._retry_interval)
                if self._shutdown:
                    break
                self._retry_count += 1
                _LOGGER.debug(
                    "Sensor retry attempt %d/%d", self._retry_count, self._max_retries
                )
                _var_var_6 = self._try_collect_sensors()
                if self._has_valid_sensors(_var_var_6):
                    self._sensors_resolved = True
                    _LOGGER.info(
                        "External sensors became available after %d retries",
                        self._retry_count,
                    )
                    if self.on_sensors_available:
                        try:
                            self.on_sensors_available(_var_var_6)
                        except Exception as _var_var_11:
                            _LOGGER.error(
                                "Callback on_sensors_available failed: %s", _var_var_11
                            )
                    await self._update_db_with_sensor_data(_var_var_6)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as _var_var_11:
                _LOGGER.warning(
                    "Sensor retry attempt %d/%d failed: %s",
                    self._retry_count,
                    self._max_retries,
                    _var_var_11,
                    exc_info=True,
                )
                continue
        if not self._sensors_resolved:
            _LOGGER.warning(
                "External sensors remain offline after %d retries - using DB data only",
                self._max_retries,
            )
        return None if True else None
