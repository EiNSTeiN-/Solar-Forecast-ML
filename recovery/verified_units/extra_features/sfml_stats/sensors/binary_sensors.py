# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Binary sensors for SFML Stats. @zara"""
from __future__ import annotations
from datetime import datetime, timedelta
import logging
from typing import Any
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from ..const import (
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SMART_CHARGING_ENABLED,
    DOMAIN,
    NAME,
    VERSION,
)
from ..sfml_data_reader import SFMLDataReader

_LOGGER = logging.getLogger(__name__)
SURPLUS_EXPORT_HOLD_SECONDS = 300
SURPLUS_UPDATE_SECONDS = 15
SURPLUS_EXPORT_MIN_W = 50.0
SURPLUS_DEMAND_MARGIN_W = 150.0
SURPLUS_IMPORT_OFF_W = 50.0
SURPLUS_IMPORT_GRACE_SECONDS = 60
SURPLUS_MAX_UPDATE_FAILURES = 3


def _numeric_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", None):
        return
    try:
        power_w = float(state.state)
    except (TypeError, ValueError):
        return
    unit = str(state.attributes.get("unit_of_measurement", "")).strip().lower()
    if unit == "kw":
        power_w *= 1000.0
    return power_w


def _positive_power(value: float | None) -> float | None:
    if value is None:
        return
    return max(0.0, value)


def _import_power(value: float | None) -> float | None:
    if value is None:
        return
    return value if value > 0 else 0.0


def _export_power(value: float | None) -> float | None:
    if value is None:
        return
    return abs(value) if value < 0 else value


class SurplusAvailableBinarySensor(BinarySensorEntity):
    """Binary sensor for sustained PV surplus availability. @zara"""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower-export"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize surplus availability binary sensor. @zara"""
        self.hass = hass
        self._entry = entry
        self._reader = SFMLDataReader(hass)
        self._attr_unique_id = f"{entry.entry_id}_surplus_available"
        self._attr_name = "PV Surplus Available"
        self._is_on = False
        self._export_started_at = None
        self._last_reason = "initializing"
        self._last_values = {}
        self._consecutive_update_failures = 0
        self._import_started_at = None

    @property
    def is_on(self) -> bool:
        """Return true if export has been stable for the configured hold time. @zara"""
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostics for automations and troubleshooting. @zara"""
        export_started_at_iso = (
            self._export_started_at.isoformat() if self._export_started_at else None
        )
        return {
            "export_power_w": self._last_values.get("export_power_w"),
            "surplus_power_w": self._last_values.get("surplus_power_w"),
            "solar_power_w": self._last_values.get("solar_power_w"),
            "home_consumption_w": self._last_values.get("home_consumption_w"),
            "grid_import_w": self._last_values.get("grid_import_w"),
            "export_duration_s": self._last_values.get("export_duration_s", 0),
            "required_duration_s": SURPLUS_EXPORT_HOLD_SECONDS,
            "export_threshold_w": SURPLUS_EXPORT_MIN_W,
            "demand_margin_w": SURPLUS_DEMAND_MARGIN_W,
            "import_off_threshold_w": SURPLUS_IMPORT_OFF_W,
            "import_grace_seconds": SURPLUS_IMPORT_GRACE_SECONDS,
            "update_failures": self._consecutive_update_failures,
            "adjusted_home_consumption_w": self._last_values.get(
                "adjusted_home_consumption_w"
            ),
            "battery_charge_w": self._last_values.get("battery_charge_w"),
            "import_duration_s": self._last_values.get("import_duration_s", 0),
            "export_started_at": export_started_at_iso,
            "export_source": self._last_values.get("export_source"),
            "reason": self._last_reason,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for grouping entities. @zara"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=NAME,
            manufacturer="Zara-Toorox",
            model="Solar Forecast Stats",
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        """Start periodic refresh. @zara"""
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_interval,
                timedelta(seconds=SURPLUS_UPDATE_SECONDS),
            )
        )
        await self._async_refresh()

    @callback
    def _handle_interval(self, now: datetime) -> None:
        self.hass.async_create_task(self._async_refresh(now))

    async def _async_refresh(self, now: datetime | None = None) -> None:
        now = now or dt_util.now()
        try:
            await self._evaluate(now)
            self._consecutive_update_failures = 0
        except Exception as error:
            self._consecutive_update_failures += 1
            _LOGGER.debug(
                "Failed to update PV surplus binary sensor (%s/%s): %s",
                self._consecutive_update_failures,
                SURPLUS_MAX_UPDATE_FAILURES,
                error,
            )
            if self._consecutive_update_failures >= SURPLUS_MAX_UPDATE_FAILURES:
                self._is_on = False
                self._export_started_at = None
                self._import_started_at = None
                self._last_reason = "update_failed"
            else:
                self._last_reason = "update_failed_retained"
        self.async_write_ha_state()

    async def _evaluate(self, now: datetime) -> None:
        config = {**self._entry.data, **self._entry.options}
        house_export_entity = config.get(CONF_SENSOR_HOUSE_TO_GRID)
        smartmeter_export_entity = config.get(CONF_SENSOR_SMARTMETER_EXPORT)
        house_import_entity = config.get(CONF_SENSOR_GRID_TO_HOUSE)
        smartmeter_import_entity = config.get(CONF_SENSOR_SMARTMETER_IMPORT)
        export_source = "house_to_grid"
        export_power_w = _export_power(_numeric_state(self.hass, house_export_entity))
        if export_power_w is None:
            export_source = "smartmeter_export"
            export_power_w = _export_power(
                _numeric_state(self.hass, smartmeter_export_entity)
            )
        import_power_w = _import_power(_numeric_state(self.hass, house_import_entity))
        if import_power_w is None:
            import_power_w = _import_power(
                _numeric_state(self.hass, smartmeter_import_entity)
            )
        if (
            import_power_w is None
            and house_export_entity
            and (house_export_entity == smartmeter_import_entity)
        ):
            import_power_w = _import_power(
                _numeric_state(self.hass, house_export_entity)
            )
        if (
            import_power_w is None
            and smartmeter_export_entity
            and (smartmeter_export_entity == smartmeter_import_entity)
        ):
            import_power_w = _import_power(
                _numeric_state(self.hass, smartmeter_export_entity)
            )
        solar_power_w = await self._reader.get_live_power()
        home_consumption_w = _positive_power(
            _numeric_state(self.hass, config.get(CONF_SENSOR_HOME_CONSUMPTION))
        )
        battery_power_w = _numeric_state(
            self.hass, config.get(CONF_SENSOR_BATTERY_POWER)
        )
        solar_to_battery_w = _positive_power(
            _numeric_state(self.hass, config.get(CONF_SENSOR_SOLAR_TO_BATTERY))
        )
        grid_to_battery_w = _positive_power(
            _numeric_state(self.hass, config.get(CONF_SENSOR_GRID_TO_BATTERY))
        )
        solar_battery_charge_w = 0.0
        if solar_to_battery_w is not None:
            solar_battery_charge_w += solar_to_battery_w
        elif battery_power_w is not None and battery_power_w > 0:
            solar_battery_charge_w += battery_power_w
        if grid_to_battery_w is not None:
            solar_battery_charge_w = max(
                0.0, solar_battery_charge_w - grid_to_battery_w
            )
        adjusted_home_consumption_w = home_consumption_w
        if adjusted_home_consumption_w is not None and solar_battery_charge_w > 0:
            adjusted_home_consumption_w = max(
                0.0, adjusted_home_consumption_w - solar_battery_charge_w
            )
        demand_exceeds_solar = (
            solar_power_w is not None
            and adjusted_home_consumption_w is not None
            and (adjusted_home_consumption_w > solar_power_w + SURPLUS_DEMAND_MARGIN_W)
        )
        calculated_surplus_w = None
        if solar_power_w is not None and adjusted_home_consumption_w is not None:
            calculated_surplus_w = max(0.0, solar_power_w - adjusted_home_consumption_w)
        if export_power_w is not None and calculated_surplus_w is not None:
            available_surplus_w = min(export_power_w, calculated_surplus_w)
        elif export_power_w is not None:
            available_surplus_w = export_power_w
        else:
            available_surplus_w = calculated_surplus_w
        has_grid_import = (
            import_power_w is not None and import_power_w >= SURPLUS_IMPORT_OFF_W
        )
        if has_grid_import:
            if self._import_started_at is None:
                self._import_started_at = now
            import_duration_s = max(
                0, int((now - self._import_started_at).total_seconds())
            )
        else:
            self._import_started_at = None
            import_duration_s = 0
        sustained_grid_import = (
            has_grid_import and import_duration_s >= SURPLUS_IMPORT_GRACE_SECONDS
        )
        has_export = (
            available_surplus_w is not None
            and available_surplus_w >= SURPLUS_EXPORT_MIN_W
        )
        if sustained_grid_import:
            self._is_on = False
            self._export_started_at = None
            self._last_reason = "grid_import_active"
        elif demand_exceeds_solar:
            self._is_on = False
            self._export_started_at = None
            self._last_reason = "home_demand_exceeds_solar"
        elif not has_export:
            self._is_on = False
            self._export_started_at = None
            self._last_reason = "no_export"
        else:
            if self._export_started_at is None:
                self._export_started_at = now
            export_duration_s = max(
                0, int((now - self._export_started_at).total_seconds())
            )
            self._is_on = export_duration_s >= SURPLUS_EXPORT_HOLD_SECONDS
            if has_grid_import:
                self._last_reason = "transient_grid_import"
            else:
                self._last_reason = (
                    "stable_export" if self._is_on else "waiting_for_hold_time"
                )
        export_duration_s = (
            max(0, int((now - self._export_started_at).total_seconds()))
            if self._export_started_at
            else 0
        )
        self._last_values = {
            "export_power_w": (
                round(export_power_w, 1) if export_power_w is not None else None
            ),
            "surplus_power_w": (
                round(available_surplus_w, 1)
                if available_surplus_w is not None
                else None
            ),
            "solar_power_w": (
                round(solar_power_w, 1) if solar_power_w is not None else None
            ),
            "home_consumption_w": (
                round(home_consumption_w, 1) if home_consumption_w is not None else None
            ),
            "adjusted_home_consumption_w": (
                round(adjusted_home_consumption_w, 1)
                if adjusted_home_consumption_w is not None
                else None
            ),
            "battery_charge_w": round(solar_battery_charge_w, 1),
            "grid_import_w": (
                round(import_power_w, 1) if import_power_w is not None else None
            ),
            "import_duration_s": import_duration_s,
            "export_duration_s": export_duration_s,
            "export_source": export_source if export_power_w is not None else None,
        }


class CheapEnergyBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether current energy price is cheap. @zara"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize cheap energy binary sensor. @zara"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cheap_energy"
        self._attr_name = "Cheap Energy"
        self._attr_icon = "mdi:cash-check"

    @property
    def is_on(self) -> bool:
        """Return True if current energy price is considered cheap. @zara"""
        if self.coordinator.data:
            return self.coordinator.data.get("is_cheap", False)
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator_data = self.coordinator.data or {}
        thresholds = (
            coordinator_data.get("thresholds")
            if isinstance(coordinator_data.get("thresholds"), dict)
            else {}
        )
        return {
            "is_force_price": bool(coordinator_data.get("is_force_price")),
            "threshold_mode": thresholds.get("mode"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for grouping entities. @zara"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=NAME,
            manufacturer="Zara-Toorox",
            model="Solar Forecast Stats",
            sw_version=VERSION,
        )


class SmartChargingBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether smart charging is active. @zara"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize smart charging binary sensor. @zara"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_smart_charging"
        self._attr_name = "Smart Charging Active"
        self._attr_icon = "mdi:battery-charging"

    def _smart_charging_enabled(self) -> bool:
        config = {**self._entry.data, **getattr(self._entry, "options", {})}
        return bool(config.get(CONF_SMART_CHARGING_ENABLED, False))

    @property
    def is_on(self) -> bool:
        """Return True if smart charging is currently active. @zara"""
        if not self._smart_charging_enabled():
            return False
        if self.coordinator.data:
            return self.coordinator.data.get("smart_charging_active", False)
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for grouping entities. @zara"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=NAME,
            manufacturer="Zara-Toorox",
            model="Solar Forecast Stats",
            sw_version=VERSION,
        )
