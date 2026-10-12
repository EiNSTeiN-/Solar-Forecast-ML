# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""Weather platform for Weather Fusion AI.

@zara
"""
# fmt: on
from __future__ import annotations
import logging
from typing import Any
from homeassistant.components.weather import (
    ATTR_FORECAST_CLOUD_COVERAGE,
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_HUMIDITY,
    ATTR_FORECAST_NATIVE_PRECIPITATION,
    ATTR_FORECAST_NATIVE_PRESSURE,
    ATTR_FORECAST_NATIVE_TEMP,
    ATTR_FORECAST_NATIVE_TEMP_LOW,
    ATTR_FORECAST_NATIVE_WIND_SPEED,
    ATTR_FORECAST_PRECIPITATION_PROBABILITY,
    ATTR_FORECAST_TIME,
    ATTR_FORECAST_WIND_BEARING,
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, NAME, VERSION, ATTRIBUTION, CONF_LOCATION_NAME
from .coordinator import WeatherFusionCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Weather Fusion AI weather entity from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    async_add_entities([WeatherFusionAIWeather(coordinator, entry)])
    _LOGGER.info("Weather Fusion AI weather entity created")


class WeatherFusionAIWeather(SingleCoordinatorWeatherEntity[WeatherFusionCoordinator]):
    """Weather entity for Weather Fusion AI."""

    _attr_has_entity_name = True
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    def __init__(
        self, coordinator: WeatherFusionCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the weather entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._location_name = entry.data.get(CONF_LOCATION_NAME, "Home")
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self._attr_name = None
        self._attr_attribution = ATTRIBUTION
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"{NAME} ({self._location_name})",
            "manufacturer": "Zara-Toorox",
            "model": "Weather Fusion AI",
            "sw_version": VERSION,
            "entry_type": "service",
        }

    @property
    def condition(self) -> str | None:
        """Return the current condition."""
        weather = self.coordinator.current_weather
        return weather.get("condition")

    @property
    def native_temperature(self) -> float | None:
        """Return the temperature."""
        weather = self.coordinator.current_weather
        return weather.get("temperature")

    @property
    def native_apparent_temperature(self) -> float | None:
        """Return the apparent (feels like) temperature."""
        weather = self.coordinator.current_weather
        return weather.get("apparent_temperature")

    @property
    def native_dew_point(self) -> float | None:
        """Return the dew point."""
        weather = self.coordinator.current_weather
        return weather.get("dew_point")

    @property
    def humidity(self) -> float | None:
        """Return the humidity."""
        weather = self.coordinator.current_weather
        return weather.get("humidity")

    @property
    def native_pressure(self) -> float | None:
        """Return the pressure."""
        weather = self.coordinator.current_weather
        return weather.get("pressure")

    @property
    def native_wind_speed(self) -> float | None:
        """Return the wind speed."""
        weather = self.coordinator.current_weather
        return weather.get("wind_speed")

    @property
    def wind_bearing(self) -> float | str | None:
        """Return the wind bearing."""
        weather = self.coordinator.current_weather
        return weather.get("wind_bearing")

    @property
    def cloud_coverage(self) -> float | None:
        """Return the cloud coverage."""
        weather = self.coordinator.current_weather
        return weather.get("cloud_coverage")

    @property
    def native_visibility(self) -> float | None:
        """Return the visibility."""
        weather = self.coordinator.current_weather
        visibility_m = weather.get("visibility")
        if visibility_m is not None:
            return visibility_m / 1000

    @property
    def uv_index(self) -> float | None:
        """Return the UV index."""
        weather = self.coordinator.current_weather
        return weather.get("uv_index")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        weather = self.coordinator.current_weather
        learning = self.coordinator.learning_stats
        attributes = {
            "location": self._location_name,
            "latitude": self.coordinator.latitude,
            "longitude": self.coordinator.longitude,
        }
        blend_info = weather.get("blend_info", {})
        if blend_info:
            attributes["blend_cloud_type"] = blend_info.get("cloud_type")
            attributes["blend_experts_used"] = blend_info.get("experts_used", [])
        if learning:
            attributes["learning_days"] = learning.get("learning_days", 0)
            attributes["learning_accuracy"] = learning.get("accuracy_percent", 0)
            attributes["active_sources"] = learning.get("active_sources", 0)
        return attributes

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return the hourly forecast."""
        hourly = self.coordinator.hourly_forecast
        if not hourly:
            return
        forecasts = []
        for hour in hourly:
            forecast = {
                ATTR_FORECAST_TIME: hour.get("datetime"),
                ATTR_FORECAST_CONDITION: hour.get("condition"),
                ATTR_FORECAST_NATIVE_TEMP: hour.get("temperature"),
                ATTR_FORECAST_HUMIDITY: hour.get("humidity"),
                ATTR_FORECAST_NATIVE_PRESSURE: hour.get("pressure"),
                ATTR_FORECAST_NATIVE_WIND_SPEED: hour.get("wind_speed"),
                ATTR_FORECAST_WIND_BEARING: hour.get("wind_bearing"),
                ATTR_FORECAST_CLOUD_COVERAGE: hour.get("cloud_coverage"),
                ATTR_FORECAST_NATIVE_PRECIPITATION: hour.get("precipitation"),
                ATTR_FORECAST_PRECIPITATION_PROBABILITY: hour.get(
                    "precipitation_probability"
                ),
            }
            forecasts.append(forecast)
        return forecasts

    async def async_forecast_daily(self) -> list[Forecast] | None:
        """Return the daily forecast."""
        daily = self.coordinator.daily_forecast
        if not daily:
            return
        forecasts = []
        for day in daily:
            forecast = {
                ATTR_FORECAST_TIME: day.get("datetime"),
                ATTR_FORECAST_CONDITION: day.get("condition"),
                ATTR_FORECAST_NATIVE_TEMP: day.get("native_temperature"),
                ATTR_FORECAST_NATIVE_TEMP_LOW: day.get("templow"),
                ATTR_FORECAST_NATIVE_PRECIPITATION: day.get("precipitation"),
                ATTR_FORECAST_PRECIPITATION_PROBABILITY: day.get(
                    "precipitation_probability"
                ),
                ATTR_FORECAST_NATIVE_WIND_SPEED: day.get("wind_speed"),
                ATTR_FORECAST_WIND_BEARING: day.get("wind_bearing"),
            }
            forecasts.append(forecast)
        return forecasts
