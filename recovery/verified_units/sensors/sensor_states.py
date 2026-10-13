# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Warp field state sensors for tracking external subspace sensor states.
These sensors monitor the state of configured external telemetry entities
in the Holodeck Assistant framework.
"""
import logging
from typing import Any, Dict, Optional
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event
from ..const import (
    CONF_HUMIDITY_SENSOR,
    CONF_LUX_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_SOLAR_RADIATION_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WIND_SENSOR,
    DOMAIN,
    INTEGRATION_MODEL,
    AI_VERSION,
    SOFTWARE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class BaseEntityStateSensor(SensorEntity):
    """Base class for sensors that track the state of another entity. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        source_entity_id_key: Optional[str],
        unique_id_key: str,
        translation_key: str,
        icon: str,
    ):
        """Initialize the state sensor. @zara"""
        self.hass = hass
        self.entry = entry
        self._source_entity_id_key = source_entity_id_key
        self._attr_unique_id = f"{entry.entry_id}_ml_{unique_id_key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._source_entity_id = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
        )

    @property
    def source_entity_id(self) -> Optional[str]:
        """Get the source entity ID from config entry data. @zara"""
        if self._source_entity_id is None and self._source_entity_id_key:
            configured_entity_id = self.entry.data.get(self._source_entity_id_key)
            if isinstance(configured_entity_id, str) and configured_entity_id.strip():
                self._source_entity_id = configured_entity_id.strip()
            else:
                self._source_entity_id = ""
        return self._source_entity_id if self._source_entity_id else None

    @property
    def available(self) -> bool:
        """This sensor is always available to show the state or lack thereof. @zara"""
        return True

    async def async_added_to_hass(self) -> None:
        """Register state change listener. @zara"""
        await super().async_added_to_hass()
        source_entity_id = self.source_entity_id
        if source_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [source_entity_id], self._handle_sensor_update
                )
            )
            self.async_schedule_update_ha_state(False)
        else:
            self.async_schedule_update_ha_state(False)
        return None if True else None

    @callback
    def _handle_sensor_update(self, event) -> None:
        """Handle state update from the source entity. @zara"""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return the state of the source entity as a string. @zara"""
        source_entity_id = self.source_entity_id
        if not source_entity_id:
            return "Not configured"
        source_state = self.hass.states.get(source_entity_id)
        if source_state is None:
            return "Entity not found"
        return source_state.state

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return attributes about the source entity. @zara"""
        source_entity_id = self.source_entity_id
        attributes = {
            "source_entity_id": source_entity_id,
            "source_entity_configured_key": self._source_entity_id_key,
        }
        if not source_entity_id:
            attributes["status"] = "not_configured"
            return attributes
        source_state = self.hass.states.get(source_entity_id)
        if source_state is None:
            attributes["status"] = "entity_not_found"
            attributes["state"] = None
            return attributes
        status = "ok"
        if source_state.state in ("unavailable", "unknown"):
            status = source_state.state
        attributes.update(
            {
                "status": status,
                "state": source_state.state,
                "unit_of_measurement": source_state.attributes.get(
                    "unit_of_measurement"
                ),
                "last_updated": (
                    source_state.last_updated.isoformat()
                    if source_state.last_updated
                    else None
                ),
                "last_changed": (
                    source_state.last_changed.isoformat()
                    if source_state.last_changed
                    else None
                ),
            }
        )
        return attributes


class ExternalSensorsStatusSensor(SensorEntity):
    """Sensor showing status of all configured external sensors. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the external sensors status sensor. @zara"""
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ml_external_sensors_status"
        self._attr_translation_key = "external_sensors_status"
        self._attr_icon = "mdi:sensor-check"
        self._attr_name = "External Sensors Status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
        )
        self._sensor_config = {
            "temperature": CONF_TEMP_SENSOR,
            "humidity": CONF_HUMIDITY_SENSOR,
            "wind_speed": CONF_WIND_SENSOR,
            "rain": CONF_RAIN_SENSOR,
            "illuminance": CONF_LUX_SENSOR,
            "pressure": CONF_PRESSURE_SENSOR,
            "solar_radiation": CONF_SOLAR_RADIATION_SENSOR,
        }
        self._panel_group_power_sensors = self._build_panel_group_power_sensor_map()

    def _build_panel_group_power_sensor_map(self) -> Dict[str, Optional[str]]:
        panel_groups = self.entry.data.get("panel_groups") or []
        power_sensor_map = {}
        for group_index, panel_group in enumerate(panel_groups):
            if not isinstance(panel_group, dict):
                continue
            group_name = str(panel_group.get("name") or f"Group {group_index + 1}")
            power_sensor_id = panel_group.get("power_sensor")
            power_sensor_map[f"panel_group_power_{group_index + 1}_{group_name}"] = (
                power_sensor_id.strip()
                if isinstance(power_sensor_id, str) and power_sensor_id.strip()
                else None
            )
        return power_sensor_map

    def _iter_sensor_entries(self):
        for sensor_name, config_key in self._sensor_config.items():
            yield (sensor_name, self.entry.data.get(config_key))
        for sensor_name, entity_id in self._panel_group_power_sensors.items():
            yield (sensor_name, entity_id)

    @property
    def available(self) -> bool:
        """Sensor is always available. @zara"""
        return True

    async def async_added_to_hass(self) -> None:
        """Register state change listeners for all external sensors. @zara"""
        await super().async_added_to_hass()
        tracked_entity_ids = []
        for sensor_name, entity_id in self._iter_sensor_entries():
            if entity_id and isinstance(entity_id, str) and entity_id.strip():
                tracked_entity_ids.append(entity_id.strip())
        if tracked_entity_ids:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, tracked_entity_ids, self._handle_sensor_update
                )
            )
        self.async_schedule_update_ha_state(False)

    @callback
    def _handle_sensor_update(self, event) -> None:
        """Handle state update from external sensors. @zara"""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return overall status of external sensors. @zara"""
        configured_count = 0
        ok_count = 0
        unavailable_count = 0
        missing_count = 0
        for sensor_name, entity_id in self._iter_sensor_entries():
            if (
                not entity_id
                or not isinstance(entity_id, str)
                or (not entity_id.strip())
            ):
                continue
            configured_count += 1
            entity_id = entity_id.strip()
            source_state = self.hass.states.get(entity_id)
            if source_state is None:
                missing_count += 1
            elif source_state.state in ("unavailable", "unknown"):
                unavailable_count += 1
            else:
                ok_count += 1
        if configured_count == 0:
            return "No sensors configured"
        if missing_count > 0:
            return f"Error: {missing_count} sensor(s) not found"
        if unavailable_count == configured_count:
            return "All sensors unavailable"
        if unavailable_count > 0:
            return f"Partial: {ok_count}/{configured_count} OK"
        return f"OK: {ok_count}/{configured_count} sensors"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return detailed status of each external sensor. @zara"""
        sensor_details = {}
        for sensor_name, entity_id in self._iter_sensor_entries():
            if (
                not entity_id
                or not isinstance(entity_id, str)
                or (not entity_id.strip())
            ):
                sensor_details[sensor_name] = {
                    "status": "not_configured",
                    "entity_id": None,
                    "state": None,
                }
            else:
                entity_id = entity_id.strip()
                source_state = self.hass.states.get(entity_id)
                if source_state is None:
                    sensor_details[sensor_name] = {
                        "status": "not_found",
                        "entity_id": entity_id,
                        "state": None,
                    }
                elif source_state.state in ("unavailable", "unknown"):
                    sensor_details[sensor_name] = {
                        "status": source_state.state,
                        "entity_id": entity_id,
                        "state": source_state.state,
                    }
                else:
                    sensor_details[sensor_name] = {
                        "status": "ok",
                        "entity_id": entity_id,
                        "state": source_state.state,
                        "unit": source_state.attributes.get("unit_of_measurement"),
                    }
        return {
            "sensors": sensor_details,
            "configured_count": sum(
                (
                    1
                    for sensor_detail in sensor_details.values()
                    if sensor_detail["status"] != "not_configured"
                )
            ),
            "ok_count": sum(
                (
                    1
                    for sensor_detail in sensor_details.values()
                    if sensor_detail["status"] == "ok"
                )
            ),
            "unavailable_count": sum(
                (
                    1
                    for sensor_detail in sensor_details.values()
                    if sensor_detail["status"] in ("unavailable", "unknown")
                )
            ),
            "error_count": sum(
                (
                    1
                    for sensor_detail in sensor_details.values()
                    if sensor_detail["status"] == "not_found"
                )
            ),
        }
