# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from ..const import AI_VERSION, DOMAIN, INTEGRATION_MODEL, SOFTWARE_VERSION
from ..core.core_panel_group_forecast import get_group_power_wp


class ActualLiveStateManager:

    async def async_refresh(self, *, force: bool = False) -> None:
        if self._stopped:
            return
        _var_var_5 = datetime.now().astimezone()
        if not force and self._last_refresh is not None:
            if _var_var_5 - self._last_refresh < self.MIN_REFRESH_INTERVAL:
                return
        if self._refreshing:
            self._refresh_requested = True
            return
        if self._unsub_follow_up is not None:
            self._unsub_follow_up()
            self._unsub_follow_up = None
        self._refresh_requested = False
        self._refreshing = True
        while True:
            try:
                _var_var_6 = getattr(
                    getattr(self.coordinator, "data_manager", None), "_db_manager", None
                )
                if _var_var_6 is None:
                    break
                _var_var_7 = getattr(
                    _var_var_6, "has_active_unmanaged_transaction", None
                )
                if callable(_var_var_7) and _var_var_7():
                    self._refresh_requested = True
                    self._schedule_retry(
                        "database transaction is active",
                        warning_threshold=self.TRANSACTION_WARNING_THRESHOLD,
                    )
                    break
                _var_var_8 = _var_var_5.date().isoformat()
                _var_var_9 = await _var_var_6.build_actual_live_state_from_hourly(
                    _var_var_8
                )
                _var_var_4 = _var_var_9.get("panel_groups", {})
                _var_var_10 = getattr(self.coordinator, "panel_groups", []) or []
                _var_var_11 = {}
                _var_var_12 = 0.0
                _var_var_13 = len(_var_var_10)
                _var_var_14 = 0
                for _var_var_15, _var_var_3 in enumerate(_var_var_10):
                    _var_var_16 = self._group_name(_var_var_3, _var_var_15)
                    _var_var_17 = self._group_power_sensor(_var_var_3)
                    _var_var_18, _var_var_19 = self._read_group_power_w(
                        _var_var_3, _var_var_17
                    )
                    if _var_var_18 is not None:
                        _var_var_12 += _var_var_18
                        _var_var_14 += 1
                    _var_var_11[_var_var_16] = {
                        "power_w": (
                            round(_var_var_18, 2) if _var_var_18 is not None else None
                        ),
                        "actual_today_kwh": round(
                            float(
                                _var_var_4.get(_var_var_16, {}).get(
                                    "actual_today_kwh", 0.0
                                )
                            ),
                            4,
                        ),
                        "power_sensor": _var_var_17,
                        "quality": _var_var_19,
                        "updated_at": _var_var_5.isoformat(),
                    }
                if _var_var_13 == 0:
                    _var_var_19 = "no_groups"
                    _var_var_20 = None
                elif _var_var_14 == _var_var_13:
                    _var_var_19 = "exact"
                    _var_var_20 = round(_var_var_12, 2)
                elif _var_var_14 > 0:
                    _var_var_19 = "partial"
                    _var_var_20 = round(_var_var_12, 2)
                else:
                    _var_var_19 = "unavailable"
                    _var_var_20 = None
                _var_var_21 = int(_var_var_9.get("actual_hours", 0) or 0)
                _var_var_22 = "hourly_predictions"
                if _var_var_21 > 0 and _var_var_13 > 0:
                    _var_var_23 = sum(
                        (
                            1
                            for _var_var_16 in _var_var_11
                            if _var_var_4.get(_var_var_16, {}).get("actual_hours", 0)
                            > 0
                        )
                    )
                    if _var_var_23 not in (0, _var_var_13):
                        _var_var_22 = "hourly_predictions_partial_groups"
                _var_var_24 = {
                    "date": _var_var_8,
                    "total_power_w": _var_var_20,
                    "total_actual_kwh": round(
                        float(_var_var_9.get("total_actual_kwh", 0.0)), 4
                    ),
                    "power_source": "sum_panel_group_power",
                    "actual_source": _var_var_22,
                    "groups_expected": _var_var_13,
                    "groups_available": _var_var_14,
                    "quality": _var_var_19,
                    "updated_at": _var_var_5.isoformat(),
                }
                await _var_var_6.save_actual_live_state(_var_var_24, _var_var_11)
                if self._stopped:
                    break
                self._state = _var_var_24
                self._groups = _var_var_11
                self._last_refresh = _var_var_5
                self._refresh_failures = 0
                self._last_refresh_error = None
                if self._unsub_retry is not None:
                    self._unsub_retry()
                    self._unsub_retry = None
                _var_var_25 = getattr(self.coordinator, "clear_health_issue", None)
                if callable(_var_var_25):
                    _var_var_25("actual_live_state")
                for _var_var_26 in list(self._listeners):
                    try:
                        _var_var_26()
                    except Exception:
                        _LOGGER.exception(
                            "SFML actual live-state listener refresh failed"
                        )
                        continue
            except Exception as _var_var_27:
                self._refresh_requested = True
                self._schedule_retry(str(_var_var_27))
            finally:
                self._refreshing = False
                if not self._stopped:
                    if self._refresh_requested and self._unsub_retry is None:
                        self._refresh_requested = False
                        self._schedule_follow_up()
            break
        return None
