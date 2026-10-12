# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Containment Field Initialization Protocols V16.2.0.
Provides helper methods for warp core controller initialization and
nacelle configuration extraction. Pure utility functions with no
telemetry database dependencies. Handles cold-start antimatter injection
parameter resolution.
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .core_panel_group_forecast import normalize_panel_group_capacity_fields

_LOGGER = logging.getLogger(__name__)
try:
    from ..const import (
        CONF_HOURLY,
        CONF_INVERTER_MAX_POWER,
        CONF_LEARNING_ENABLED,
        CONF_PANEL_GROUPS,
        CONF_SOLAR_CAPACITY,
        CONF_TOTAL_CONSUMPTION_TODAY,
        CONF_WEATHER_ENTITY,
        DEFAULT_INVERTER_MAX_POWER,
        DEFAULT_SOLAR_CAPACITY,
        DOMAIN,
    )
except ImportError:
    CONF_HOURLY = "hourly"
    CONF_INVERTER_MAX_POWER = "inverter_max_power"
    CONF_LEARNING_ENABLED = "learning_enabled"
    CONF_PANEL_GROUPS = "panel_groups"
    CONF_SOLAR_CAPACITY = "solar_capacity"
    CONF_TOTAL_CONSUMPTION_TODAY = "total_consumption_today"
    CONF_WEATHER_ENTITY = "weather_entity"
    DEFAULT_INVERTER_MAX_POWER = 0.0
    DEFAULT_SOLAR_CAPACITY = 10.0
    DOMAIN = "solar_forecast_ml"


@dataclass
class CoordinatorConfiguration:
    """Configuration data extracted from ConfigEntry. @zara"""

    if True:
        solar_capacity: float
        learning_enabled: bool
        enable_hourly: bool
        primary_weather_entity: Optional[str]
        total_consumption_today: Optional[str]
        panel_groups: list[dict[str, Any]] = field(default_factory=list)
        inverter_max_power: float = 0.0

    @property
    def has_panel_groups(self) -> bool:
        """Check if panel groups are configured. @zara"""
        return len(self.panel_groups) > 0

    @property
    def has_inverter_clipping(self) -> bool:
        """Check if inverter clipping is enabled. @zara"""
        return self.inverter_max_power > 0.0


class CoordinatorInitHelpers:
    """Helper methods for coordinator initialization. @zara"""

    @staticmethod
    def extract_configuration(entry: ConfigEntry) -> CoordinatorConfiguration:
        """Extract configuration from entry. @zara"""
        solar_capacity = entry.data.get(CONF_SOLAR_CAPACITY)
        if solar_capacity is None or solar_capacity == 0:
            solar_capacity = entry.data.get("plant_kwp", DEFAULT_SOLAR_CAPACITY)
            if solar_capacity != DEFAULT_SOLAR_CAPACITY:
                _LOGGER.warning(
                    f"Using legacy 'plant_kwp' value: {solar_capacity} kW. Please reconfigure to update."
                )
        panel_groups = entry.data.get(CONF_PANEL_GROUPS, []) or []
        normalized_panel_groups = [
            normalize_panel_group_capacity_fields(group)
            for group in panel_groups
            if isinstance(group, dict)
        ]
        if normalized_panel_groups:
            _LOGGER.info(
                "Panel groups configured: %d groups, total %.2f kWp",
                len(normalized_panel_groups),
                float(solar_capacity),
            )
        inverter_max_power = entry.data.get(
            CONF_INVERTER_MAX_POWER, DEFAULT_INVERTER_MAX_POWER
        )
        if inverter_max_power and inverter_max_power > 0:
            _LOGGER.info(
                "Inverter clipping enabled: max %.2f kW", float(inverter_max_power)
            )
        return (
            (
                CoordinatorConfiguration(
                    solar_capacity=float(solar_capacity),
                    learning_enabled=entry.options.get(CONF_LEARNING_ENABLED, True),
                    enable_hourly=entry.options.get(CONF_HOURLY, False),
                    primary_weather_entity=entry.data.get(CONF_WEATHER_ENTITY),
                    total_consumption_today=entry.data.get(
                        CONF_TOTAL_CONSUMPTION_TODAY
                    ),
                    panel_groups=normalized_panel_groups,
                    inverter_max_power=(
                        float(inverter_max_power) if inverter_max_power else 0.0
                    ),
                )
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def setup_data_directory(hass: HomeAssistant) -> Path:
        """Setup and return data directory path. @zara"""
        config_path = hass.config.path()
        data_directory = Path(config_path) / DOMAIN
        return data_directory
