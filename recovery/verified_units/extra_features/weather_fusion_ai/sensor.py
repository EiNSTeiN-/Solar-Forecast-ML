# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Sensor platform for Weather Fusion AI diagnostics.

@zara
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, NAME, VERSION, CONF_LOCATION_NAME, LEARNING_MIN_DAYS
from .coordinator import WeatherFusionCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Weather Fusion AI sensors from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    sensors = [
        StatusSensor(coordinator, entry),
        ForecastAccuracySensor(coordinator, entry),
        LearningProgressSensor(coordinator, entry),
        ActiveSourcesSensor(coordinator, entry),
        LastLearningSensor(coordinator, entry),
        CloudTypeSensor(coordinator, entry),
    ]
    async_add_entities(sensors)
    _LOGGER.info("Weather Fusion AI sensors created: %d", len(sensors))


class WeatherFusionBaseSensor(
    CoordinatorEntity[WeatherFusionCoordinator], SensorEntity
):
    """Base class for Weather Fusion AI sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WeatherFusionCoordinator,
        entry: ConfigEntry,
        sensor_type: str,
        name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._location_name = entry.data.get(CONF_LOCATION_NAME, "Home")
        self._attr_unique_id = f"{entry.entry_id}_{sensor_type}"
        self._attr_translation_key = sensor_type
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"{NAME} ({self._location_name})",
            "manufacturer": "Zara-Toorox",
            "model": "Weather Fusion AI",
            "sw_version": VERSION,
        }


class StatusSensor(WeatherFusionBaseSensor):
    """Sensor showing system status: Active, Learning, or Error."""

    _attr_icon = "mdi:information-outline"

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "status", "Status")

    @property
    def native_value(self) -> str:
        """Return system status: Aktiv, Lernend, or Fehler."""
        learning_stats = self.coordinator.learning_stats
        if learning_stats.get("error"):
            return "error"
        learning_days = learning_stats.get("learning_days", 0)
        if learning_days < LEARNING_MIN_DAYS:
            return "learning"
        return "active"

    @property
    def icon(self) -> str:
        """Return icon based on status."""
        status = self.native_value
        if status == "error":
            return "mdi:alert-circle"
        if status == "learning":
            return "mdi:school"
        return "mdi:check-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        learning_stats = self.coordinator.learning_stats
        learning_days = learning_stats.get("learning_days", 0)
        attributes = {"learning_days": learning_days}
        if self.native_value == "learning":
            remaining_days = LEARNING_MIN_DAYS - learning_days
            attributes["description"] = (
                f"Collecting data. {remaining_days} day(s) until full activation."
            )
        elif self.native_value == "error":
            attributes["description"] = learning_stats.get("error", "Unknown error")
        else:
            attributes["description"] = (
                "System is active and using learned data for forecasts."
            )
        return attributes if True else None


class ActiveSourcesSensor(WeatherFusionBaseSensor):
    """Sensor showing number of active weather sources."""

    _attr_icon = "mdi:cloud-sync"

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "active_sources", "Active Weather Sources")

    @property
    def native_value(self) -> int | None:
        """Return the number of active sources."""
        learning_stats = self.coordinator.learning_stats
        return learning_stats.get("active_sources", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes with source details."""
        attributes = {
            "available_sources": [
                "GFS (NOAA)",
                "ICON (DWD)",
                "ECMWF (wttr.in)",
                "Pirate Weather",
            ],
            "description": "Number of raw data sources contributing to the blend",
        }
        weather = self.coordinator.current_weather
        blend_info = weather.get("blend_info", {})
        if blend_info:
            attributes["experts_used"] = blend_info.get("experts_used", [])
            attributes["expert_names"] = blend_info.get("expert_names", {})
            attributes["blend_weights"] = blend_info.get("weights", {})
        return attributes


class LastLearningSensor(WeatherFusionBaseSensor):
    """Sensor showing last learning update timestamp."""

    _attr_icon = "mdi:brain"

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, entry, "last_learning_update", "Last Learning Update"
        )

    @property
    def native_value(self) -> str:
        """Return the last learning update time as string or 'Pending'."""
        learning_stats = self.coordinator.learning_stats
        last_learning = learning_stats.get("last_learning")
        if last_learning:
            try:
                learning_time = datetime.fromisoformat(last_learning)
                if learning_time.tzinfo is None:
                    learning_time = learning_time.replace(
                        tzinfo=self.coordinator.time_zone
                    )
                else:
                    learning_time = learning_time.astimezone(self.coordinator.time_zone)
                return learning_time.strftime("%d.%m. %H:%M")
            except (ValueError, TypeError):
                pass
        return "pending" if True else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        learning_stats = self.coordinator.learning_stats
        last_learning = learning_stats.get("last_learning")
        attributes = {
            "description": "When expert weights were last updated based on learned data"
        }
        if last_learning:
            try:
                learning_time = datetime.fromisoformat(last_learning)
                if learning_time.tzinfo is None:
                    learning_time = learning_time.replace(
                        tzinfo=self.coordinator.time_zone
                    )
                else:
                    learning_time = learning_time.astimezone(self.coordinator.time_zone)
                next_learning = learning_time + timedelta(hours=6)
                attributes["next_learning"] = next_learning.isoformat()
            except (ValueError, TypeError):
                pass
        return attributes if True else None


class CloudTypeSensor(WeatherFusionBaseSensor):
    """Sensor showing current detected cloud type."""

    _attr_icon = "mdi:weather-cloudy"

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "cloud_type", "Cloud Type")

    @property
    def native_value(self) -> str | None:
        """Return the current cloud type."""
        weather = self.coordinator.current_weather
        blend_info = weather.get("blend_info", {})
        cloud_type = blend_info.get("cloud_type")
        return cloud_type

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        weather = self.coordinator.current_weather
        blend_info = weather.get("blend_info", {})
        attributes = {
            "raw_type": blend_info.get("cloud_type"),
            "description": "Cloud type affects which weather sources are weighted highest",
        }
        if weather.get("cloud_coverage") is not None:
            attributes["total_cloud_cover"] = weather["cloud_coverage"]
        return attributes


class ForecastAccuracySensor(WeatherFusionBaseSensor):
    """Sensor showing forecast accuracy percentage."""

    _attr_icon = "mdi:target"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "forecast_accuracy", "Forecast Accuracy")

    @property
    def native_value(self) -> float | None:
        """Return the forecast accuracy percentage."""
        learning_stats = self.coordinator.learning_stats
        if not learning_stats.get("calibration_complete", False):
            return
        accuracy_percent = learning_stats.get("accuracy_percent")
        if accuracy_percent is not None:
            return round(float(accuracy_percent), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        learning_stats = self.coordinator.learning_stats
        calibration_complete = learning_stats.get("calibration_complete", False)
        return (
            (
                (
                    {
                        "learning_hours": learning_stats.get("learning_hours", 0),
                        "learning_days": learning_stats.get("learning_days", 0),
                        "calibration_complete": calibration_complete,
                        "description": (
                            "Forecast accuracy based on comparison with local sensor observations"
                            if calibration_complete
                            else "Accuracy is hidden until the minimum calibration period is complete"
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )


class LearningProgressSensor(WeatherFusionBaseSensor):
    """Sensor showing learning progress phase."""

    _attr_icon = "mdi:school"

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "learning_progress", "Learning Progress")

    @property
    def native_value(self) -> str:
        """Return learning progress as 'Tag X/3' or 'Aktiv'."""
        learning_stats = self.coordinator.learning_stats
        learning_days = learning_stats.get("learning_days", 0)
        if learning_days < LEARNING_MIN_DAYS:
            return f"day {learning_days}/{LEARNING_MIN_DAYS}"
        return "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        learning_stats = self.coordinator.learning_stats
        learning_days = learning_stats.get("learning_days", 0)
        return {
            "learning_days": learning_days,
            "phase": "calibration" if learning_days < LEARNING_MIN_DAYS else "active",
            "description": f"Learning progress towards full calibration ({LEARNING_MIN_DAYS} days minimum)",
        }
