# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""SFML-owned actual and power state sensors."""
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

_LOGGER = logging.getLogger(__name__)


class ActualLiveStateManager:
    """Persist and expose SFML-owned live actual and panel-group power state."""

    MIN_REFRESH_INTERVAL = timedelta(seconds=15)
    DEBOUNCE_SECONDS = 2
    PERIODIC_REFRESH_INTERVAL = timedelta(minutes=1)
    TRANSACTION_WARNING_THRESHOLD = 4

    def __init__(
        self, hass: HomeAssistant, coordinator: Any, entry: ConfigEntry
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.entry = entry
        self._state = {}
        self._groups = {}
        self._listeners = []
        self._unsub_state = None
        self._unsub_debounce = None
        self._unsub_interval = None
        self._unsub_retry = None
        self._unsub_follow_up = None
        self._last_refresh = None
        self._refreshing = False
        self._refresh_requested = False
        self._refresh_failures = 0
        self._last_refresh_error = None
        self._stopped = False

    @property
    def state(self) -> Dict[str, Any]:
        return self._state

    @property
    def groups(self) -> Dict[str, Dict[str, Any]]:
        return self._groups

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)
            return None if True else None

        return _remove

    async def async_start(self) -> None:
        self._stopped = False
        power_sensor_entity_ids = [
            power_sensor_entity_id
            for power_sensor_entity_id in (
                self._group_power_sensor(panel_group)
                for panel_group in getattr(self.coordinator, "panel_groups", []) or []
            )
            if power_sensor_entity_id
        ]
        if power_sensor_entity_ids:
            self._unsub_state = async_track_state_change_event(
                self.hass, power_sensor_entity_ids, self._handle_group_power_update
            )
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_periodic_refresh, self.PERIODIC_REFRESH_INTERVAL
        )
        await self.async_refresh(force=True)

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_debounce:
            self._unsub_debounce()
            self._unsub_debounce = None
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_retry:
            self._unsub_retry()
            self._unsub_retry = None
        if self._unsub_follow_up:
            self._unsub_follow_up()
            self._unsub_follow_up = None
        return None if True else None

    @callback
    def _handle_group_power_update(self, _event) -> None:
        if self._stopped:
            return
        if self._unsub_debounce:
            return
        self._unsub_debounce = async_call_later(
            self.hass, self.DEBOUNCE_SECONDS, self._debounced_refresh
        )

    @callback
    def _debounced_refresh(self, _now) -> None:
        self._unsub_debounce = None
        if self._stopped:
            return
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _handle_periodic_refresh(self, _now) -> None:
        if self._stopped:
            return
        self.hass.async_create_task(self.async_refresh(force=True))

    async def async_refresh(self, *, force: bool = False) -> None:
        if self._stopped:
            return
        now = datetime.now().astimezone()
        if not force and self._last_refresh is not None:
            if now - self._last_refresh < self.MIN_REFRESH_INTERVAL:
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
                db_manager = getattr(
                    getattr(self.coordinator, "data_manager", None), "_db_manager", None
                )
                if db_manager is None:
                    break
                has_unmanaged_transaction = getattr(
                    db_manager, "has_active_unmanaged_transaction", None
                )
                if callable(has_unmanaged_transaction) and has_unmanaged_transaction():
                    self._refresh_requested = True
                    self._schedule_retry(
                        "database transaction is active",
                        warning_threshold=self.TRANSACTION_WARNING_THRESHOLD,
                    )
                    break
                today_key = now.date().isoformat()
                hourly_actuals = await db_manager.build_actual_live_state_from_hourly(
                    today_key
                )
                hourly_group_actuals = hourly_actuals.get("panel_groups", {})
                panel_groups = getattr(self.coordinator, "panel_groups", []) or []
                group_states = {}
                available_power_sum_w = 0.0
                groups_expected = len(panel_groups)
                groups_available = 0
                for group_index, panel_group in enumerate(panel_groups):
                    group_name = self._group_name(panel_group, group_index)
                    power_sensor_entity_id = self._group_power_sensor(panel_group)
                    group_power_w, power_quality = self._read_group_power_w(
                        panel_group, power_sensor_entity_id
                    )
                    if group_power_w is not None:
                        available_power_sum_w += group_power_w
                        groups_available += 1
                    group_states[group_name] = {
                        "power_w": (
                            round(group_power_w, 2)
                            if group_power_w is not None
                            else None
                        ),
                        "actual_today_kwh": round(
                            float(
                                hourly_group_actuals.get(group_name, {}).get(
                                    "actual_today_kwh", 0.0
                                )
                            ),
                            4,
                        ),
                        "power_sensor": power_sensor_entity_id,
                        "quality": power_quality,
                        "updated_at": now.isoformat(),
                    }
                if groups_expected == 0:
                    power_quality = "no_groups"
                    total_power_w = None
                elif groups_available == groups_expected:
                    power_quality = "exact"
                    total_power_w = round(available_power_sum_w, 2)
                elif groups_available > 0:
                    power_quality = "partial"
                    total_power_w = round(available_power_sum_w, 2)
                else:
                    power_quality = "unavailable"
                    total_power_w = None
                actual_hours = int(hourly_actuals.get("actual_hours", 0) or 0)
                actual_source = "hourly_predictions"
                if actual_hours > 0 and groups_expected > 0:
                    groups_with_actual_hours = sum(
                        (
                            1
                            for group_name in group_states
                            if hourly_group_actuals.get(group_name, {}).get(
                                "actual_hours", 0
                            )
                            > 0
                        )
                    )
                    if groups_with_actual_hours not in (0, groups_expected):
                        actual_source = "hourly_predictions_partial_groups"
                live_state = {
                    "date": today_key,
                    "total_power_w": total_power_w,
                    "total_actual_kwh": round(
                        float(hourly_actuals.get("total_actual_kwh", 0.0)), 4
                    ),
                    "power_source": "sum_panel_group_power",
                    "actual_source": actual_source,
                    "groups_expected": groups_expected,
                    "groups_available": groups_available,
                    "quality": power_quality,
                    "updated_at": now.isoformat(),
                }
                await db_manager.save_actual_live_state(live_state, group_states)
                if self._stopped:
                    break
                self._state = live_state
                self._groups = group_states
                self._last_refresh = now
                self._refresh_failures = 0
                self._last_refresh_error = None
                if self._unsub_retry is not None:
                    self._unsub_retry()
                    self._unsub_retry = None
                clear_health_issue = getattr(
                    self.coordinator, "clear_health_issue", None
                )
                if callable(clear_health_issue):
                    clear_health_issue("actual_live_state")
                for listener in list(self._listeners):
                    try:
                        listener()
                    except Exception:
                        _LOGGER.exception(
                            "SFML actual live-state listener refresh failed"
                        )
                        continue
            except Exception as error:
                self._refresh_requested = True
                self._schedule_retry(str(error))
            finally:
                self._refreshing = False
                if not self._stopped:
                    if self._refresh_requested and self._unsub_retry is None:
                        self._refresh_requested = False
                        self._schedule_follow_up()
            break
        return None

    def _schedule_follow_up(self) -> None:
        """Run one silent refresh after concurrent requests were coalesced."""
        if self._stopped or self._unsub_follow_up is not None:
            return None
        self._unsub_follow_up = async_call_later(self.hass, 0, self._run_follow_up)

    @callback
    def _run_follow_up(self, _now) -> None:
        self._unsub_follow_up = None
        if self._stopped:
            return
        self.hass.async_create_task(self.async_refresh(force=True))

    def _schedule_retry(self, reason: str, *, warning_threshold: int = 1) -> None:
        """Coalesce failed refreshes behind bounded exponential backoff."""
        if self._stopped or self._unsub_retry is not None:
            return None
        self._refresh_failures += 1
        retry_delay_seconds = min(2 ** (self._refresh_failures - 1), 60)
        warning_threshold_reached = self._refresh_failures >= warning_threshold
        should_warn = warning_threshold_reached and reason != self._last_refresh_error
        if should_warn:
            _LOGGER.warning(
                "SFML actual live-state refresh deferred for %ds: %s",
                retry_delay_seconds,
                reason,
            )
            self._last_refresh_error = reason
        elif not warning_threshold_reached:
            _LOGGER.debug(
                "SFML actual live-state refresh deferred for %ds: %s (attempt %d/%d before warning)",
                retry_delay_seconds,
                reason,
                self._refresh_failures,
                warning_threshold,
            )
        if should_warn:
            set_health_issue = getattr(self.coordinator, "set_health_issue", None)
            if callable(set_health_issue):
                set_health_issue(
                    "actual_live_state",
                    f"Actual live-state database retry pending: {reason}",
                )
        self._unsub_retry = async_call_later(
            self.hass, retry_delay_seconds, self._retry_refresh
        )

    @callback
    def _retry_refresh(self, _now) -> None:
        self._unsub_retry = None
        self._refresh_requested = False
        if self._stopped:
            return
        self.hass.async_create_task(self.async_refresh(force=True))

    def _read_group_power_w(
        self, group: Any, power_sensor: Optional[str]
    ) -> tuple[Optional[float], str]:
        if not power_sensor:
            return (None, "not_configured")
        entity_state = self.hass.states.get(power_sensor)
        if entity_state is None:
            return (None, "not_found")
        if entity_state.state in ("unavailable", "unknown", "none", "", None):
            return (None, str(entity_state.state or "unavailable"))
        try:
            raw_power_value = float(
                str(entity_state.state).split(" ")[0].replace(",", ".")
            )
        except (TypeError, ValueError):
            return (None, "invalid")
        unit_of_measurement = str(
            entity_state.attributes.get("unit_of_measurement") or ""
        ).lower()
        power_w = (
            raw_power_value * 1000.0
            if unit_of_measurement in ("kw", "kilowatt", "kilowatts")
            else raw_power_value
        )
        if power_w < 0:
            return (None, "negative")
        max_power_w = self._group_max_power_w(group)
        if power_w > max_power_w:
            return (None, "implausible")
        return (power_w, "exact")

    @staticmethod
    def _group_name(group: Any, index: int) -> str:
        if isinstance(group, dict):
            return str(group.get("name") or f"Group {index + 1}")
        return str(getattr(group, "name", None) or f"Group {index + 1}")

    @staticmethod
    def _group_power_sensor(group: Any) -> Optional[str]:
        if isinstance(group, dict):
            power_sensor_entity_id = group.get("power_sensor")
        else:
            power_sensor_entity_id = getattr(group, "power_sensor", None)
        return (
            power_sensor_entity_id.strip()
            if isinstance(power_sensor_entity_id, str)
            and power_sensor_entity_id.strip()
            else None
        )

    @staticmethod
    def _group_max_power_w(group: Any) -> float:
        return max(get_group_power_wp(group) * 1.5, 50.0)


class _ActualSotSensorBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: ActualLiveStateManager, entry: ConfigEntry) -> None:
        self.manager = manager
        self.entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.manager.add_listener(self._handle_manager_update))
        self.async_write_ha_state()

    @callback
    def _handle_manager_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return bool(self.manager.state)


class SFMLTotalPowerSensor(_ActualSotSensorBase):
    """SFML total power from persisted panel-group live state."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, manager: ActualLiveStateManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_total_power"
        self._attr_translation_key = "total_power"
        self._attr_name = "SFML Total Power"
        self._attr_icon = "mdi:solar-power-variant"

    @property
    def native_value(self) -> Optional[float]:
        power_w = self.manager.state.get("total_power_w")
        return round(float(power_w), 1) if power_w is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        live_state = self.manager.state
        return {
            "source": live_state.get("power_source"),
            "quality": live_state.get("quality"),
            "groups_expected": live_state.get("groups_expected"),
            "groups_available": live_state.get("groups_available"),
            "updated_at": live_state.get("updated_at"),
        }


class SFMLTotalActualTodaySensor(_ActualSotSensorBase):
    """SFML total actual today from persisted hourly actual truth."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3

    def __init__(self, manager: ActualLiveStateManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_total_actual_today"
        self._attr_translation_key = "total_actual_today"
        self._attr_name = "SFML Total Actual Today"
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self) -> Optional[float]:
        total_actual_kwh = self.manager.state.get("total_actual_kwh")
        return (
            round(float(total_actual_kwh), 3) if total_actual_kwh is not None else None
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        live_state = self.manager.state
        return {
            "source": live_state.get("actual_source"),
            "quality": live_state.get("quality"),
            "groups_expected": live_state.get("groups_expected"),
            "groups_available": live_state.get("groups_available"),
            "updated_at": live_state.get("updated_at"),
        }


class SFMLTotalYieldSensor(_ActualSotSensorBase):
    """SFML daily total yield from persisted hourly actual truth."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3

    def __init__(self, manager: ActualLiveStateManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_total_yield"
        self._attr_translation_key = "total_yield"
        self._attr_name = "SFML Total Yield Today"
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self) -> Optional[float]:
        total_actual_kwh = self.manager.state.get("total_actual_kwh")
        return (
            round(float(total_actual_kwh), 3) if total_actual_kwh is not None else None
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        live_state = self.manager.state
        return {
            "source": live_state.get("actual_source"),
            "date": live_state.get("date"),
            "actual_hours": live_state.get("actual_hours"),
            "quality": live_state.get("quality"),
            "groups_expected": live_state.get("groups_expected"),
            "groups_available": live_state.get("groups_available"),
            "updated_at": live_state.get("updated_at"),
        }


class SFMLPanelGroupPowerSensor(_ActualSotSensorBase):
    """SFML panel-group live power from persisted live state."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        manager: ActualLiveStateManager,
        entry: ConfigEntry,
        group_name: str,
        index: int,
    ) -> None:
        super().__init__(manager, entry)
        self.group_name = group_name
        self._attr_unique_id = f"{entry.entry_id}_ml_panel_group_{index + 1}_power"
        self._attr_name = f"{group_name} Power"
        self._attr_icon = "mdi:solar-panel"

    @property
    def native_value(self) -> Optional[float]:
        power_w = self.manager.groups.get(self.group_name, {}).get("power_w")
        return round(float(power_w), 1) if power_w is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        group_state = self.manager.groups.get(self.group_name, {})
        return {
            "group_name": self.group_name,
            "source": "panel_group_power_sensor",
            "source_entity_id": group_state.get("power_sensor"),
            "quality": group_state.get("quality"),
            "updated_at": group_state.get("updated_at"),
        }


class SFMLPanelGroupActualTodaySensor(_ActualSotSensorBase):
    """SFML panel-group actual today from persisted group actual truth."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        manager: ActualLiveStateManager,
        entry: ConfigEntry,
        group_name: str,
        index: int,
    ) -> None:
        super().__init__(manager, entry)
        self.group_name = group_name
        self._attr_unique_id = (
            f"{entry.entry_id}_ml_panel_group_{index + 1}_actual_today"
        )
        self._attr_name = f"{group_name} Actual Today"
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self) -> Optional[float]:
        actual_today_kwh = self.manager.groups.get(self.group_name, {}).get(
            "actual_today_kwh"
        )
        return (
            round(float(actual_today_kwh), 3) if actual_today_kwh is not None else None
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        group_state = self.manager.groups.get(self.group_name, {})
        return {
            "group_name": self.group_name,
            "source": "prediction_panel_groups",
            "quality": group_state.get("quality"),
            "power_sensor": group_state.get("power_sensor"),
            "updated_at": group_state.get("updated_at"),
        }
