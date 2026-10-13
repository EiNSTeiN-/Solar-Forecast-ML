# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from ..const import (
    DOMAIN,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
    SERVICE_ANALYZE_FEATURE_IMPORTANCE,
    SERVICE_BACKFILL_SHADOW_DETECTION,
    SERVICE_BUILD_ASTRONOMY_CACHE,
    SERVICE_EMERGENCY_OUTAGE_QUARANTINE,
    SERVICE_RUN_WEATHER_CORRECTION,
    SERVICE_REFRESH_MULTI_WEATHER,
    SERVICE_REFRESH_CACHE_TODAY,
    SERVICE_REPAIR_TOOL,
    SERVICE_RESET_AI_MODEL,
    SERVICE_RETRAIN_AI_MODEL,
    SERVICE_RUN_ADAPTIVE_FORECAST,
    SERVICE_RUN_OPERATIONAL_REFORECAST,
    SERVICE_SET_OPERATIONAL_REFORECAST_MODE,
    SERVICE_RUN_ALL_DAY_END_TASKS,
    SERVICE_RUN_GRID_SEARCH,
    SERVICE_SEND_DAILY_BRIEFING,
    SERVICE_TEST_MORNING_ROUTINE,
    SERVICE_TEST_RETROSPECTIVE_FORECAST,
)
from ..const import CONF_WINTER_MODE, DEFAULT_WINTER_MODE
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager


class ServiceRegistry:

    async def _handle_backfill_shadow_detection(self, call: ServiceCall) -> None:
        """Handle backfill_shadow_detection service. @zara V16.2

        Runs shadow detection for hours that have actual production data
        but no shadow detection results.
        """
        _LOGGER.info("Service: backfill_shadow_detection")
        try:
            _var_var_65 = call.data.get("target_date")
            if not _var_var_65:
                _var_var_65 = dt_util.now().date().isoformat()
            _LOGGER.info(f"Backfilling shadow detection for {_var_var_65}")
            if (
                not hasattr(self.coordinator, "scheduled_tasks")
                or not self.coordinator.scheduled_tasks
                or (not self.coordinator.scheduled_tasks.morning_routine_handler)
            ):
                _LOGGER.error("Morning routine handler not available")
                return
            _var_var_66 = self.coordinator.scheduled_tasks.morning_routine_handler
            _var_var_67 = await self.db_manager.fetchall(
                "SELECT hp.target_hour, hp.prediction_id, hp.actual_kwh,\n                          COALESCE(hp.inverter_clipped, 0), COALESCE(hp.mppt_throttled, 0)\n                   FROM hourly_predictions hp\n                   LEFT JOIN hourly_shadow_detection hsd ON hp.prediction_id = hsd.prediction_id\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n                     AND hsd.prediction_id IS NULL\n                     AND hp.target_hour >= 6\n                     AND hp.target_hour <= 20\n                   ORDER BY hp.target_hour",
                (_var_var_65,),
            )
            if not _var_var_67:
                _LOGGER.info(f"No missing shadow detection for {_var_var_65}")
                return
            _LOGGER.info(f"Found {len(_var_var_67)} hours missing shadow detection")
            _var_var_68 = _var_var_66._shadow_detector
            if not _var_var_68:
                _LOGGER.error("Shadow detector not available")
                return
            _var_var_69 = 0
            _var_var_70 = 0
            for _var_var_71 in _var_var_67:
                _var_var_72 = _var_var_71[0]
                _var_var_73 = _var_var_71[1]
                _var_var_74 = _var_var_71[2]
                _var_var_75 = bool(_var_var_71[3])
                _var_var_76 = bool(_var_var_71[4])
                try:
                    if hasattr(self.db_manager, "is_actual_backfill_blocked"):
                        if await self.db_manager.is_actual_backfill_blocked(
                            _var_var_65, _var_var_72
                        ):
                            _LOGGER.warning(
                                "Shadow backfill skipped for emergency-quarantined hour: %s_%02d",
                                _var_var_65,
                                _var_var_72,
                            )
                            continue
                    _var_var_77 = await _var_var_66._get_astronomy_for_hour(
                        _var_var_65, _var_var_72
                    )
                    if not _var_var_77:
                        _LOGGER.debug(f"No astronomy data for hour {_var_var_72}")
                        continue
                    _var_var_78 = await self.db_manager.fetchone(
                        "SELECT temperature_c, humidity_percent, cloud_cover_percent,\n                                  precipitation_mm, wind_speed_ms, snow_covered_panels,\n                                  frost_detected\n                           FROM hourly_weather_actual\n                           WHERE date = ? AND hour = ?",
                        (_var_var_65, _var_var_72),
                    )
                    _var_var_79 = {}
                    if _var_var_78:
                        _var_var_79 = {
                            "temperature_c": _var_var_78[0],
                            "humidity_percent": _var_var_78[1],
                            "cloud_cover_percent": _var_var_78[2],
                            "precipitation_mm": _var_var_78[3],
                            "wind_speed_ms": _var_var_78[4],
                            "snow_covered_panels": _var_var_78[5],
                            "frost_detected": _var_var_78[6],
                        }
                    if not _var_var_79.get("snow_covered_panels"):
                        _var_var_80 = f"{_var_var_65}T{_var_var_72:02d}:00:00"
                        _var_var_81 = await self.db_manager.fetchone(
                            "SELECT panels_covered_since, cleared_at\n                               FROM snow_tracking\n                               WHERE id = 1",
                            (),
                        )
                        if _var_var_81 and _var_var_81[0]:
                            _var_var_82 = _var_var_81[0]
                            _var_var_83 = _var_var_81[1]
                            if _var_var_82 <= _var_var_80 and (
                                _var_var_83 is None or _var_var_83 > _var_var_80
                            ):
                                _var_var_79["snow_covered_panels"] = True
                                _LOGGER.debug(
                                    "Backfill: Snow detected from snow_tracking for %s hour %d",
                                    _var_var_65,
                                    _var_var_72,
                                )
                    _var_var_84 = {
                        "actual_kwh": _var_var_74,
                        "astronomy": _var_var_77,
                        "target_hour": _var_var_72,
                        "target_datetime": f"{_var_var_65}T{_var_var_72:02d}:00:00",
                        "weather_actual": _var_var_79,
                        "sensor_actual": {},
                        "flags": {
                            "inverter_clipped": _var_var_75,
                            "mppt_throttled": _var_var_76,
                        },
                    }
                    _var_var_85 = await _var_var_68.detect_shadow_ensemble(_var_var_84)
                    if _var_var_85:
                        await self.db_manager.save_hourly_shadow_detection(
                            _var_var_73, _var_var_85
                        )
                        if self.coordinator.update_shadow_detection_cache(
                            _var_var_73, _var_var_85
                        ):
                            self.coordinator.async_update_listeners()
                        _var_var_69 += 1
                        _LOGGER.debug(
                            "Shadow detection for %s hour %d: %s (%.1f%%)",
                            _var_var_65,
                            _var_var_72,
                            _var_var_85.get("shadow_type", "unknown"),
                            _var_var_85.get("shadow_percent", 0),
                        )
                except Exception as _var_var_14:
                    _var_var_70 += 1
                    _LOGGER.warning(
                        f"Failed shadow detection for hour {_var_var_72}: {_var_var_14}"
                    )
            _LOGGER.info(
                f"Shadow detection backfill complete for {_var_var_65}: {_var_var_69} processed, {_var_var_70} errors"
            )
        except Exception as _var_var_64:
            _LOGGER.error(
                f"Error in backfill_shadow_detection service: {_var_var_64}",
                exc_info=True,
            )
        return None if True else None
