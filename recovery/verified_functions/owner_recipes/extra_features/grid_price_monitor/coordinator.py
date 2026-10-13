# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    BACKFILL_DAYS,
    CONF_BASE_FEE_EUR_MONTH,
    CONF_BATTERY_POWER_SENSOR,
    CONF_COUNTRY,
    CONF_CSV_COMMUNITY,
    CONF_FEED_IN_TARIFF_CT,
    CONF_GRID_FEE,
    CONF_MAX_PRICE,
    CONF_FORCE_CHARGE_PRICE,
    CONF_THRESHOLD_MODE,
    CONF_BELOW_AVERAGE_PCT,
    CONF_CHEAPEST_HOURS,
    CONF_PROVIDER_MARKUP,
    CONF_TARIFF_MODE,
    CONF_TAXES_FEES,
    CONF_TIME_OF_USE,
    CONF_VAT_RATE,
    DB_PATH,
    DEFAULT_BASE_FEE_EUR_MONTH,
    DEFAULT_COUNTRY,
    DEFAULT_FEED_IN_TARIFF_CT,
    DEFAULT_GRID_FEE,
    DEFAULT_MAX_PRICE,
    DEFAULT_FORCE_CHARGE_PRICE,
    DEFAULT_THRESHOLD_MODE,
    DEFAULT_BELOW_AVERAGE_PCT,
    DEFAULT_CHEAPEST_HOURS,
    DEFAULT_PROVIDER_MARKUP,
    DEFAULT_TAXES_FEES,
    DOMAIN,
    PRICE_FETCH_INTERVAL,
    SIGNAL_PRICES_REVISED,
    TARIFF_MODE_CSV_COMMUNITY,
    TARIFF_MODE_DEMO,
    TARIFF_MODE_DYNAMIC,
    TARIFF_MODE_TIME_OF_USE,
    TARIFF_MODE_TIME_WINDOWS,
    THRESHOLD_MODES,
    UPDATE_INTERVAL,
    VAT_RATE_AT,
    VAT_RATE_DE,
)
from .core import ElectricityPriceService, BatteryTracker, PriceCalculator
from .core.thresholds import (
    DayThresholds,
    THRESHOLD_LIMITS,
    ThresholdConfig,
    evaluate_current,
    evaluate_day,
    merge_and_validate_thresholds,
    slot_is_cheap,
    threshold_options,
    thresholds_payload,
)
from .helpers import GPMLogger, async_setup_gpm_logging
from .license import (
    has_corrections,
    has_csv_import,
    has_tariff_models,
    is_demo_entitlements,
    mask_license_id,
    resolve_entitlements,
)
from .storage import (
    DataValidator,
    GPMDatabaseConnector,
    HistoryManager,
    PriceCache,
    PriceWriter,
    StatisticsStore,
)
from .storage.demo_persist import async_persist_demo_prices
from .tariffs.base import week_schedule_prices
from .tariffs.demo import DemoSource, demo_window
from .tariffs.factory import build_price_source


class GridPriceMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):

    async def _async_monitor_license(self, expires_at: datetime) -> None:
        from .license import resolve_entitlements as _attempt__recovered_capture_0004
        from .license.models import LicenseStatus as A_recovered_capture_0001

        _var_var_19 = 60.0
        while True:
            _var_var_20 = datetime.now(timezone.utc)
            _var_var_21 = (
                expires_at.astimezone(timezone.utc)
                if expires_at.tzinfo
                else expires_at.replace(tzinfo=timezone.utc)
            )
            await asyncio.sleep(
                max(0.0, min(3600.0, (_var_var_21 - _var_var_20).total_seconds()))
            )
            try:
                _var_var_2 = _attempt__recovered_capture_0004(self.hass, self.entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "GPM license revalidation failed; retrying in %.0f seconds",
                    _var_var_19,
                )
                await asyncio.sleep(_var_var_19)
                _var_var_19 = min(_var_var_19 * 2, 3600.0)
                continue
            _var_var_19 = 60.0
            if _var_var_2.status == A_recovered_capture_0001.VALID.value:
                if _var_var_2.expires_at is None:
                    return
                expires_at = _var_var_2.expires_at
                continue
            await self.hass.config_entries.async_reload(self.entry.entry_id)
            return
