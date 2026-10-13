# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, Optional, List
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class WeatherActualTracker(DataManagerIO):

    async def _read_sensors(self) -> dict[str, Any]:
        """Read current values from all configured sensors. @zara"""
        _var_var_42 = {}
        _var_var_43 = 0
        for _var_var_22, _var_var_31 in self.sensor_mapping.items():
            if not _var_var_31:
                continue
            try:
                _var_var_32 = self.hass.states.get(_var_var_31)
                if _var_var_32 is None or _var_var_32.state in (
                    "unavailable",
                    "unknown",
                    None,
                ):
                    continue
                _var_var_33 = _var_var_32.state
                if _var_var_22 == "condition":
                    _var_var_42[_var_var_22] = _var_var_33
                    _var_var_43 += 1
                else:
                    try:
                        _var_var_44 = float(_var_var_33)
                        _var_var_42[_var_var_22] = round(_var_var_44, 2)
                        _var_var_43 += 1
                        if _var_var_22 == "precipitation_mm":
                            _var_var_45, _var_var_1, _var_var_46 = (
                                classify_rain_sensor_state(_var_var_32)
                            )
                            _var_var_5, _var_var_45, _var_var_1, _var_var_46 = (
                                persistable_rain_observation(
                                    _var_var_44, _var_var_45, _var_var_1, _var_var_46
                                )
                            )
                            if _var_var_5 is not None:
                                _var_var_42[_var_var_22] = _var_var_5
                            _var_var_42["precipitation_semantics"] = _var_var_45
                            _var_var_42["precipitation_unit"] = _var_var_1
                            _var_var_42["precipitation_quality"] = _var_var_46
                    except (ValueError, TypeError):
                        continue
            except Exception:
                continue
        return _var_var_42 if _var_var_43 > 0 else {}

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
        _var_var_84 = [
            _var_var_83
            for _var_var_83 in (temp_c, forecast_temp_c)
            if _var_var_83 is not None
        ]
        if not _var_var_84:
            return True
        _var_var_85 = max(_var_var_84)
        if weather_code in SNOW_WEATHER_CODES:
            if _var_var_85 > SNOW_TEMP_MAX_C:
                _LOGGER.info(
                    "Snow code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    SNOW_WEATHER_CODES.get(weather_code, "unknown"),
                    _var_var_85,
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
            if _var_var_85 > FREEZING_RAIN_TEMP_MAX_C:
                _LOGGER.info(
                    "Freezing rain code %d (%s) REJECTED: temp %.1f°C exceeds %.1f°C threshold (sensor=%s, forecast=%s)",
                    weather_code,
                    FREEZING_RAIN_CODES.get(weather_code, "unknown"),
                    _var_var_85,
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

    async def _detect_sensor_freeze(
        self, date_str: str, hour: int, current_val: float, field: str
    ) -> bool:
        """Detect if sensor values are frozen (identical for 3+ consecutive hours). @zara V16.2"""
        if current_val <= 0.0 and field in ("solar_radiation_wm2", "lux"):
            return False
        try:
            _var_var_88 = await self.fetch_all(
                "SELECT {field} FROM hourly_weather_actual\n                   WHERE (date = ? AND hour < ?) OR (date < ?)\n                   ORDER BY date DESC, hour DESC\n                   LIMIT 2".format(
                    field=field
                ),
                (date_str, hour, date_str),
            )
            if not _var_var_88 or len(_var_var_88) < 2:
                return False
            _var_var_151 = [current_val]
            for _var_var_152 in _var_var_88:
                if _var_var_152[0] is not None:
                    _var_var_151.append(float(_var_var_152[0]))
            if len(_var_var_151) < 3:
                return False
            if all((_var_var_153 == _var_var_151[0] for _var_var_153 in _var_var_151)):
                _LOGGER.warning(
                    "Sensor freeze detected for field '%s': 3 identical values (%s)",
                    field,
                    _var_var_151,
                )
                return True
            return False
        except Exception as _var_var_41:
            _LOGGER.debug("Error in freeze detection: %s", _var_var_41)
            return False
