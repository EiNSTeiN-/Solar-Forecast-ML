# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import datetime, timedelta
import logging
from typing import Any
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from ..const import (
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SMART_CHARGING_ENABLED,
    DOMAIN,
    NAME,
    VERSION,
)
from ..sfml_data_reader import SFMLDataReader


class SurplusAvailableBinarySensor(BinarySensorEntity):

    async def _async_refresh(self, now: datetime | None = None) -> None:
        now = now or dt_util.now()
        try:
            await self._evaluate(now)
            self._consecutive_update_failures = 0
        except Exception as _var_var_4:
            self._consecutive_update_failures += 1
            _LOGGER.debug(
                "Failed to update PV surplus binary sensor (%s/%s): %s",
                self._consecutive_update_failures,
                SURPLUS_MAX_UPDATE_FAILURES,
                _var_var_4,
            )
            if self._consecutive_update_failures >= SURPLUS_MAX_UPDATE_FAILURES:
                self._is_on = False
                self._export_started_at = None
                self._import_started_at = None
                self._last_reason = "update_failed"
            else:
                self._last_reason = "update_failed_retained"
        self.async_write_ha_state()
