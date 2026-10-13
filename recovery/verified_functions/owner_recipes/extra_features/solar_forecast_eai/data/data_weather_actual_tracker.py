# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class WeatherActualTracker(DataManagerIO):

    async def _read_sensors(self) -> dict[str, Any]:
        """Read current values from all configured sensors. @zara"""
        _var_var_19 = {}
        _var_var_20 = 0
        for _var_var_21, _var_var_22 in self.sensor_mapping.items():
            if not _var_var_22:
                continue
            try:
                _var_var_23 = self.hass.states.get(_var_var_22)
                if _var_var_23 is None or _var_var_23.state in (
                    "unavailable",
                    "unknown",
                    None,
                ):
                    continue
                _var_var_24 = _var_var_23.state
                if _var_var_21 == "condition":
                    _var_var_19[_var_var_21] = _var_var_24
                    _var_var_20 += 1
                else:
                    try:
                        _var_var_25 = float(_var_var_24)
                        if _var_var_21 == "precipitation_mm":
                            _var_var_26 = self._precipitation_snapshot(
                                _var_var_25,
                                getattr(_var_var_23, "attributes", {}) or {},
                            )
                            if _var_var_26 is None:
                                _LOGGER.debug(
                                    "Ignoring ambiguous precipitation sensor %s",
                                    _var_var_22,
                                )
                                continue
                            _var_var_19.update(_var_var_26)
                        else:
                            _var_var_19[_var_var_21] = round(_var_var_25, 2)
                        _var_var_20 += 1
                    except (ValueError, TypeError):
                        continue
            except Exception:
                continue
        return _var_var_19 if _var_var_20 > 0 else {}

    async def _detect_fog(
        self, weather_data: dict[str, Any], date_str: str, hour: int
    ) -> Optional[dict[str, Any]]:
        """Detect fog conditions from forecast data and meteorological indicators. @zara

        Two-source detection:
        1. Primary: Read fog_detected/fog_type from weather_forecast table
           (set by WeatherForecastCorrector from Open-Meteo visibility data)
        2. Fallback: Meteorological detection from local sensor data
           (temperature, humidity, dewpoint, cloud cover)

        Args:
            weather_data: Current sensor readings
            date_str: Current date (YYYY-MM-DD)
            hour: Current hour (0-23)

        Returns:
            Dict with fog detection results or None on error
        """
        try:
            _var_var_50 = False
            _var_var_51 = "none"
            _var_var_52 = 0.0
            _var_var_53 = None
            try:
                _var_var_38 = await self.fetch_one(
                    "SELECT visibility_m, fog_detected, fog_type\n                       FROM weather_forecast\n                       WHERE forecast_date = ? AND hour = ?",
                    (date_str, hour),
                )
                if _var_var_38:
                    _var_var_53 = _var_var_38[0]
                    if _var_var_38[1]:
                        _var_var_50 = True
                        _var_var_51 = _var_var_38[2] or "dense"
                        _var_var_52 = 0.85
            except Exception:
                pass
            if not _var_var_50:
                _var_var_40 = weather_data.get("temperature_c")
                _var_var_41 = weather_data.get("humidity_percent")
                if _var_var_40 is not None and _var_var_41 is not None:
                    _var_var_54 = None
                    if _var_var_41 > 50:
                        _var_var_54 = _var_var_40 - (100 - _var_var_41) / 5.0
                    _var_var_14 = weather_data.get("cloud_cover_percent", 50)
                    from .data_weather_corrector import (
                        detect_fog_from_meteorology as _var_var_55,
                    )

                    _var_var_56, _var_var_44, _var_var_57 = _var_var_55(
                        _var_var_40, _var_var_54, _var_var_41, _var_var_14, hour
                    )
                    if _var_var_56:
                        _var_var_50 = True
                        _var_var_51 = _var_var_57
                        _var_var_52 = _var_var_44
            if not _var_var_50:
                return
            _var_var_58 = False
            if _var_var_53 is not None:
                _var_var_58 = _var_var_53 < 1000
            if _var_var_58:
                await self._send_fog_notification(
                    visibility_m=_var_var_53,
                    temp_c=weather_data.get("temperature_c", 0.0),
                    humidity=weather_data.get("humidity_percent", 0.0),
                    fog_type=_var_var_51,
                    date_str=date_str,
                    hour=hour,
                )
            return {
                "fog_detected": True,
                "fog_type": _var_var_51,
                "fog_confidence": round(_var_var_52, 2),
            }
        except Exception as _var_var_46:
            _LOGGER.error("Error during fog detection: %s", _var_var_46)
            return

    def _is_snow_plausible(
        self,
        temp_c: Optional[float],
        forecast_temp_c: Optional[float],
        weather_code: int,
    ) -> bool:
        """Check if snow/ice is physically plausible given temperature data. @zara V16.8.3

        Uses the WARMER of actual sensor temp and forecast temp for validation.
        If either source says it's warm, the snow code is implausible.
        If no temperature data is available at all, trusts the weather code.
        """
        _var_var_78 = [
            _var_var_77
            for _var_var_77 in (temp_c, forecast_temp_c)
            if _var_var_77 is not None
        ]
        if not _var_var_78:
            return True
        _var_var_79 = max(_var_var_78)
        if weather_code in SNOW_WEATHER_CODES:
            if _var_var_79 > SNOW_TEMP_MAX_C:
                _LOGGER.info(
                    "Snow code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    SNOW_WEATHER_CODES.get(weather_code, "unknown"),
                    _var_var_79,
                    SNOW_TEMP_MAX_C,
                    f"{temp_c:.1f}°C" if temp_c is not None else "n/a",
                    (
                        f"{forecast_temp_c:.1f}°C"
                        if forecast_temp_c is not None
                        else "n/a"
                    ),
                )
                return False
            return True
        if weather_code in FREEZING_RAIN_CODES:
            if _var_var_79 > FREEZING_RAIN_TEMP_MAX_C:
                _LOGGER.info(
                    "Freezing rain code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    FREEZING_RAIN_CODES.get(weather_code, "unknown"),
                    _var_var_79,
                    FREEZING_RAIN_TEMP_MAX_C,
                    f"{temp_c:.1f}°C" if temp_c is not None else "n/a",
                    (
                        f"{forecast_temp_c:.1f}°C"
                        if forecast_temp_c is not None
                        else "n/a"
                    ),
                )
                return False
            return True
        return True

    async def _ensure_snow_tracking_schema(self) -> None:
        """Ensure snow_tracking table has all required columns. @zara V16.1"""
        try:
            await self.execute_query(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       last_snow_event TIMESTAMP,\n                       panels_covered_since TIMESTAMP,\n                       estimated_depth_mm REAL DEFAULT 0,\n                       melt_hours REAL DEFAULT 0,\n                       detection_source TEXT,\n                       cleared_at TIMESTAMP,\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            _var_var_125 = set()
            try:
                _var_var_82 = await self.fetch_all("PRAGMA table_info(snow_tracking)")
                _var_var_125 = (
                    {_var_var_38[1] for _var_var_38 in _var_var_82}
                    if _var_var_82
                    else set()
                )
            except Exception:
                pass
            for _var_var_126 in (
                "melt_hours REAL DEFAULT 0",
                "detection_source TEXT",
                "cleared_at TIMESTAMP",
            ):
                _var_var_127 = _var_var_126.split()[0]
                if _var_var_127 not in _var_var_125:
                    try:
                        await self.execute_query(
                            f"ALTER TABLE snow_tracking ADD COLUMN {_var_var_126}"
                        )
                        _LOGGER.info(
                            "Migration: Added column %s to snow_tracking", _var_var_127
                        )
                    except Exception:
                        continue
            try:
                _var_var_128 = await self.fetch_all(
                    "PRAGMA table_info(weather_forecast)"
                )
                _var_var_129 = (
                    {_var_var_38[1] for _var_var_38 in _var_var_128}
                    if _var_var_128
                    else set()
                )
                if "weather_code" not in _var_var_129:
                    await self.execute_query(
                        "ALTER TABLE weather_forecast ADD COLUMN weather_code INTEGER"
                    )
                    _LOGGER.info(
                        "Migration: Added weather_code column to weather_forecast"
                    )
            except Exception:
                pass
        except Exception as _var_var_46:
            _LOGGER.debug("Error ensuring snow_tracking schema: %s", _var_var_46)
        return None if False else None
