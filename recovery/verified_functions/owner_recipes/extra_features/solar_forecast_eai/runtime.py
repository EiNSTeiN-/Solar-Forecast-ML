# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any
from .const import (
    CONF_HEAT_PUMP_ENABLED,
    CONF_LICENSE_KEY,
    CONF_WALLBOX_ENABLED,
    CONF_WEATHER_FUSION_ENTRY_ID,
    CONF_WEATHER_HISTORY_DAYS,
    CONF_WEATHER_INTELLIGENCE_ENABLED,
    DEFAULT_WEATHER_HISTORY_DAYS,
    DOMAIN,
)


class EAIRuntime:

    async def _async_update_weather_intelligence(
        self, *, refresh_history: bool | None = None
    ) -> None:
        if self._weather_engine is None:
            return
        async with self._weather_update_lock:
            if refresh_history is None:
                refresh_history = self._weather_history_refresh_required()
            if refresh_history:
                self._weather_history_last_attempt = monotonic()
            try:
                async with asyncio.timeout(_WEATHER_UPDATE_TIMEOUT_SECONDS):
                    self.weather_intelligence_snapshot = (
                        await self._weather_engine.async_snapshot(
                            refresh_history=refresh_history
                        )
                    )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "EAI weather intelligence update timed out after %d seconds",
                    _WEATHER_UPDATE_TIMEOUT_SECONDS,
                )
                self.weather_intelligence_snapshot = {
                    **self.weather_intelligence_snapshot,
                    "available": False,
                    "status": "degraded",
                    "source_status": "engine_timeout",
                    "error": "TimeoutError",
                }
            except asyncio.CancelledError:
                raise
            except Exception as _var_var_14:
                _LOGGER.exception("EAI weather intelligence update failed")
                self.weather_intelligence_snapshot = {
                    **self.weather_intelligence_snapshot,
                    "available": False,
                    "status": "degraded",
                    "source_status": "engine_error",
                    "error": type(_var_var_14).__name__,
                }
            self._sync_weather_state_listener()
        for _var_var_37 in tuple(self._weather_intelligence_listeners):
            try:
                _var_var_37()
            except Exception:
                _LOGGER.exception("EAI weather intelligence listener refresh failed")
                continue
        return None if True else None
