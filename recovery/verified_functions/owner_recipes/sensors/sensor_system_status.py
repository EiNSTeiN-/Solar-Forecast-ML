# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ..const import DOMAIN
from ..coordinator import SolarForecastMLCoordinator
from ..core.core_helpers import SafeDateTimeUtil as dt_util


class SystemStatusSensor(CoordinatorEntity, SensorEntity):

    def _get_hourly_forecast_for_day(self, day: str) -> List[Dict[str, Any]]:
        """Extract hourly forecast data for a specific day from coordinator cache. @zara"""
        try:
            if not self.coordinator.data or not self.coordinator.data.get(
                "hourly_forecast"
            ):
                return []
            _var_var_31 = self.coordinator.data.get("hourly_forecast", [])
            if not _var_var_31:
                return []
            _var_var_23 = dt_util.now()
            if day == "today":
                _var_var_32 = _var_var_23.date()
            elif day == "tomorrow":
                _var_var_32 = (_var_var_23 + timedelta(days=1)).date()
            elif day == "day_after_tomorrow":
                _var_var_32 = (_var_var_23 + timedelta(days=2)).date()
            else:
                return []
            _var_var_33 = []
            for _var_var_34 in _var_var_31:
                try:
                    _var_var_35 = _var_var_34.get("local_datetime") or _var_var_34.get(
                        "datetime"
                    )
                    if not _var_var_35:
                        continue
                    if isinstance(_var_var_35, str):
                        _var_var_35 = dt_util.parse_datetime(_var_var_35)
                        if not _var_var_35:
                            continue
                    if _var_var_35.date() == _var_var_32:
                        _var_var_33.append(
                            {
                                "hour": _var_var_35.hour,
                                "datetime": _var_var_35.isoformat(),
                                "production_kwh": round(
                                    _var_var_34.get("production_kwh", 0.0), 3
                                ),
                            }
                        )
                except Exception as _var_var_36:
                    _LOGGER.debug(f"Error processing hourly data entry: {_var_var_36}")
            _var_var_33.sort(key=lambda x: x["hour"])
            return _var_var_33
        except Exception as _var_var_36:
            _LOGGER.debug(f"Error extracting hourly forecast for {day}: {_var_var_36}")
            return []
