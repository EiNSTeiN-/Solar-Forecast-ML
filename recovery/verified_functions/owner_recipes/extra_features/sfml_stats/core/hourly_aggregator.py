# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, TYPE_CHECKING
import aiofiles
import asyncio
import aiosqlite
from homeassistant.core import HomeAssistant
from .eai_wp_sources import resolved_heatpump_entity
from ..const import (
    DOMAIN,
    SFML_STATS_DATA,
    HOURLY_BILLING_HISTORY,
    HOURLY_CATCHUP_HOURS,
    SOLAR_FORECAST_DB,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_WORK_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_LEGACY_FIXED_PRICE_CT,
    CONF_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    DEFAULT_BILLING_FIXED_PRICE,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_FEED_IN_TARIFF,
)
from ..sfml_data_reader import SFMLDataReader
from ..utils.time_utils import (
    as_ha_local,
    format_local_hour_key,
    ha_now,
    ha_timezone,
    ha_today,
    local_date_range_strings,
    naive_ha_local,
    previous_ha_local_hour,
)
from .power_integration import integrate_power_from_recorder


class HourlyBillingAggregator:

    async def _aggregate_hour_with_lock_retry(
        self, start_local: datetime, end_local: datetime
    ) -> bool:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as _var_var_8,
        )

        _var_var_83 = None
        for _var_var_84 in range(3):
            try:
                return await self.async_aggregate_hourly(start_local, end_local)
            except Exception as _var_var_10:
                _var_var_83 = _var_var_10
                if (
                    _var_var_8._is_locked_error if True else _var_var_8._is_locked_error
                )(_var_var_10) and _var_var_84 < 2:
                    _var_var_85 = (
                        _var_var_8._retry_wait if True else _var_var_8._retry_wait
                    )(_var_var_84)
                    _LOGGER.warning(
                        "Hourly billing locked for %s (attempt %d/3), retrying in %.2fs",
                        format_local_hour_key(start_local, self._hass),
                        _var_var_84 + 1,
                        _var_var_85,
                    )
                    await asyncio.sleep(_var_var_85)
                    continue
                raise
        if _var_var_83 is not None:
            raise _var_var_83
        return False
