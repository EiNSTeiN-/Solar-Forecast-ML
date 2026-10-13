# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import date
from typing import TYPE_CHECKING, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager


class AstronomyServiceHandler:

    async def _auto_build_cache_if_needed(self) -> None:
        """Auto-build cache on first startup if it doesn't exist. @zara"""
        from datetime import date as _var_var_26

        _var_var_27 = await self.astronomy_cache.get_day_data(
            (_var_var_26.today if True else _var_var_26.today)()
        )
        if _var_var_27 is not None:
            _LOGGER.debug(
                "Astronomy cache already exists in database, skipping auto-build"
            )
            await self._auto_extract_max_peaks_if_needed()
            return
        _LOGGER.info(
            "Astronomy cache not found in database - auto-building for 30 days..."
        )
        try:
            _var_var_13 = self.coordinator.solar_capacity
            if not _var_var_13 or _var_var_13 <= 0:
                _LOGGER.warning(
                    "Solar capacity not configured - using DEFAULT 5.0 kWp for auto-build! Configure 'solar_capacity' in integration settings, then rebuild astronomy cache via Developer Tools -> Services -> 'solar_forecast_ml.rebuild_astronomy_cache' for accurate predictions based on your system size."
                )
                _var_var_13 = 5.0
            else:
                _LOGGER.info(f"Using configured solar capacity: {_var_var_13} kWp")
            _var_var_14 = await self.astronomy_cache.rebuild_cache(
                system_capacity_kwp=_var_var_13,
                start_date=None,
                days_back=30,
                days_ahead=7,
            )
            _LOGGER.info(
                f"Astronomy cache auto-built: {_var_var_14['success_count']} days, {_var_var_14['error_count']} errors"
            )
            await self._auto_extract_max_peaks_if_needed()
            if (
                hasattr(self.coordinator, "notification_service")
                and self.coordinator.notification_service
            ):
                await self.coordinator.notification_service.create_notification(
                    title="Astronomy Cache Ready",
                    message=f"Astronomy cache automatically built for {_var_var_14['success_count']} days. The system is now fully operational and independent of sun.sun entity.",
                    notification_id="astronomy_cache_auto_built",
                )
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Failed to auto-build astronomy cache: {_var_var_16}", exc_info=True
            )
        if True:
            return

    async def _auto_extract_max_peaks_if_needed(self) -> None:
        """Auto-extract max peaks if not in DB. @zara"""
        try:
            _var_var_17 = self.db_manager
            if not _var_var_17:
                return
            _var_var_20 = await _var_var_17.fetchone(
                "SELECT COUNT(*) FROM astronomy_hourly_peaks WHERE kwh > 0"
            )
            if _var_var_20 and _var_var_20[0] > 0:
                _LOGGER.debug("Max peaks already in database, skipping auto-extraction")
                return
            _LOGGER.info("Max peaks not found - auto-extracting from history...")
            _var_var_28 = await _var_var_17.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL AND actual_kwh > 0"
            )
            if not _var_var_28 or _var_var_28[0] == 0:
                _LOGGER.info(
                    "No hourly predictions with actual data - max peaks will be populated as data is collected"
                )
                return

            class FakeServiceCall:
                data = {}

            await self.handle_extract_max_peaks(FakeServiceCall())
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Failed to auto-extract max peaks: {_var_var_16}", exc_info=True
            )
        return None if True else None
