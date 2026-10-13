# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..astronomy_provider import AstronomyProviderAdapter
from ..data.data_shadow_detection import ShadowDetector


class MorningRoutineHandler:

    async def execute_morning_routine_with_retry(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_hourly: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
    ) -> bool:
        """
        Execute morning routine with bulletproof retry logic.

        Returns:
            True if successful, False if all retries failed. @zara
        """
        if self.db is None:
            _LOGGER.error("Database not available for morning routine")
            return False
        _var_var_0 = 3
        try:
            for _var_var_1 in range(1, _var_var_0 + 1):
                _LOGGER.info("")
                _LOGGER.info(
                    "================================================================================"
                )
                _LOGGER.info(
                    "  MORNING ROUTINE EXECUTION - Attempt %d/%d for %s",
                    _var_var_1,
                    _var_var_0,
                    date,
                )
                _LOGGER.info(
                    "================================================================================"
                )
                try:
                    _LOGGER.info("→ Step 1/5: Validating prerequisites...")
                    if not await self._validate_prerequisites(
                        date, hourly_forecast, weather_hourly, astronomy_data
                    ):
                        raise ValueError("Prerequisites validation failed")
                    _LOGGER.info(
                        "  ✓ Prerequisites validated: %d forecast hours, %d weather hours",
                        len(hourly_forecast),
                        len(weather_hourly),
                    )
                    _var_var_2 = getattr(self.coordinator, "inverter_max_power", 0.0)
                    if _var_var_2 > 0:
                        _LOGGER.info(
                            "  ℹ Inverter capping enabled: %.2f kW", _var_var_2
                        )
                    _LOGGER.info(
                        "→ Step 2/5: Creating hourly predictions in database..."
                    )
                    _var_var_3 = await self._create_daily_predictions(
                        date=date,
                        hourly_forecast=hourly_forecast,
                        weather_forecast=weather_hourly,
                        astronomy_data=astronomy_data,
                        sensor_config=sensor_config,
                        inverter_max_power=_var_var_2,
                    )
                    _LOGGER.info("  ✓ Hourly predictions created successfully")
                    _LOGGER.info("→ Step 3/5: Verifying data integrity...")
                    if not await self._verify_integrity(date, _var_var_3):
                        raise RuntimeError("Integrity verification failed")
                    _LOGGER.info("  ✓ Data integrity verified - no duplicates found")
                    _LOGGER.info("→ Step 4/5: Refreshing coordinator cache...")
                    await self.coordinator._refresh_hourly_predictions_cache()
                    _LOGGER.info("  ✓ Coordinator cache refreshed")
                    _LOGGER.info("→ Step 5/5: Triggering sensor updates...")
                    await self.coordinator.async_request_refresh()
                    _LOGGER.info("  ✓ Sensor updates triggered")
                    _LOGGER.info("")
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("  ✓✓✓ MORNING ROUTINE SUCCESSFUL ✓✓✓")
                    _LOGGER.info(
                        "  Date: %s | Attempt: %d/%d", date, _var_var_1, _var_var_0
                    )
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("")
                    return True
                except Exception as _var_var_4:
                    _LOGGER.error(
                        "Morning routine attempt %d/%d FAILED: %s",
                        _var_var_1,
                        _var_var_0,
                        _var_var_4,
                        exc_info=True,
                    )
                    if _var_var_1 < _var_var_0:
                        _var_var_5 = 60 * _var_var_1
                        _LOGGER.info(
                            "Waiting %ds before retry %d...", _var_var_5, _var_var_1 + 1
                        )
                        await asyncio.sleep(_var_var_5)
                    else:
                        _LOGGER.error(
                            "CRITICAL: Morning routine FAILED after %d attempts! Manual intervention required",
                            _var_var_0,
                        )
                        await self._send_critical_alert(
                            "Morning routine failure", str(_var_var_4)
                        )
                        return False
            return False
        except Exception as _var_var_4:
            _LOGGER.error(
                "CRITICAL: Morning routine handler crashed: %s",
                _var_var_4,
                exc_info=True,
            )
            return False

    async def _verify_integrity(self, date: str, created_dates: set = None) -> bool:
        """Verify data integrity after creation. @zara"""
        try:
            if created_dates:
                _var_var_85 = sorted(created_dates)
            else:
                from datetime import timedelta as _var_var_86

                _var_var_87 = datetime.fromisoformat(date).date()
                _var_var_85 = [
                    (_var_var_87 + _var_var_86(days=_var_var_88)).isoformat()
                    for _var_var_88 in range(3)
                ]
            for _var_var_89 in _var_var_85:
                _var_var_24 = await self.db.fetchone(
                    "SELECT COUNT(*) FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_89,),
                )
                if not _var_var_24 or _var_var_24[0] == 0:
                    _LOGGER.error(
                        "No predictions found for %s after creation!", _var_var_89
                    )
                    return False
                _var_var_90 = await self.db.fetchone(
                    "SELECT prediction_id, COUNT(*) as cnt\n                       FROM hourly_predictions\n                       WHERE target_date = ?\n                       GROUP BY prediction_id\n                       HAVING cnt > 1",
                    (_var_var_89,),
                )
                if _var_var_90:
                    _LOGGER.error(
                        "Duplicate prediction IDs found for %s: %s",
                        _var_var_89,
                        _var_var_90[0],
                    )
                    return False
            _LOGGER.debug(
                "Integrity verified for %d days starting from %s, no duplicates",
                len(_var_var_85),
                _var_var_85[0] if _var_var_85 else date,
            )
        except Exception as _var_var_4:
            _LOGGER.error(
                "Integrity verification failed: %s", _var_var_4, exc_info=True
            )
            return False
        return True
