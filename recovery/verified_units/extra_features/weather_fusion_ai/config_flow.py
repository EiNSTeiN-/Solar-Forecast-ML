# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Config flow for Weather Fusion AI integration.

@zara
"""
from __future__ import annotations
import logging
import re
from typing import Any
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from .const import (
    DOMAIN,
    NAME,
    CONF_LOCATION_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_TEMP_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_WIND_SPEED_SENSOR,
    CONF_WIND_DIRECTION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_PIRATE_WEATHER_API_KEY,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    SENSOR_CONFIG_KEYS,
)

_LOGGER = logging.getLogger(__name__)


def location_unique_id(
    latitude: float, longitude: float, location_name: str | None
) -> str:
    """Build a unique ID from coordinates plus a location slug."""
    location_slug = re.sub("[^a-z0-9]+", "-", (location_name or "home").strip().lower())
    location_slug = location_slug.strip("-") or "home"
    return f"{float(latitude):.4f}_{float(longitude):.4f}_{location_slug}"


def get_sensor_selector() -> selector.EntitySelector:
    """Get entity selector for sensor entities."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def get_secret_selector() -> selector.TextSelector:
    """Get a password-style selector for optional provider credentials."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the options form, using current values as defaults."""
    return vol.Schema(
        {
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
            ),
            vol.Optional(
                CONF_TEMP_SENSOR, default=current.get(CONF_TEMP_SENSOR) or vol.UNDEFINED
            ): get_sensor_selector(),
            vol.Optional(
                CONF_HUMIDITY_SENSOR,
                default=current.get(CONF_HUMIDITY_SENSOR) or vol.UNDEFINED,
            ): get_sensor_selector(),
            vol.Optional(
                CONF_PRESSURE_SENSOR,
                default=current.get(CONF_PRESSURE_SENSOR) or vol.UNDEFINED,
            ): get_sensor_selector(),
            vol.Optional(
                CONF_WIND_SPEED_SENSOR,
                default=current.get(CONF_WIND_SPEED_SENSOR) or vol.UNDEFINED,
            ): get_sensor_selector(),
            vol.Optional(
                CONF_WIND_DIRECTION_SENSOR,
                default=current.get(CONF_WIND_DIRECTION_SENSOR) or vol.UNDEFINED,
            ): get_sensor_selector(),
            vol.Optional(
                CONF_RAIN_SENSOR, default=current.get(CONF_RAIN_SENSOR) or vol.UNDEFINED
            ): get_sensor_selector(),
            vol.Optional(
                CONF_PIRATE_WEATHER_API_KEY,
                default=current.get(CONF_PIRATE_WEATHER_API_KEY) or vol.UNDEFINED,
            ): get_secret_selector(),
        }
    )


class WeatherFusionAIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Weather Fusion AI."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data = {}
        self._errors = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - location setup."""
        errors = {}
        if user_input is not None:
            self._data = dict(user_input)
            return await self.async_step_sensors()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCATION_NAME, default="Home"): str,
                    vol.Required(
                        CONF_LATITUDE, default=self.hass.config.latitude
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_LONGITUDE, default=self.hass.config.longitude
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"name": NAME},
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the sensor selection step."""
        errors = {}
        if user_input is not None:
            has_sensors = any(
                [
                    user_input.get(CONF_TEMP_SENSOR),
                    user_input.get(CONF_HUMIDITY_SENSOR),
                    user_input.get(CONF_PRESSURE_SENSOR),
                    user_input.get(CONF_WIND_SPEED_SENSOR),
                    user_input.get(CONF_WIND_DIRECTION_SENSOR),
                    user_input.get(CONF_RAIN_SENSOR),
                ]
            )
            if not has_sensors:
                errors["base"] = "no_sensors"
            else:
                self._data.update(user_input)
                return await self.async_step_api_keys()
        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TEMP_SENSOR): get_sensor_selector(),
                    vol.Optional(CONF_HUMIDITY_SENSOR): get_sensor_selector(),
                    vol.Optional(CONF_PRESSURE_SENSOR): get_sensor_selector(),
                    vol.Optional(CONF_WIND_SPEED_SENSOR): get_sensor_selector(),
                    vol.Optional(CONF_WIND_DIRECTION_SENSOR): get_sensor_selector(),
                    vol.Optional(CONF_RAIN_SENSOR): get_sensor_selector(),
                }
            ),
            errors=errors,
            description_placeholders={"name": NAME},
        )

    async def async_step_api_keys(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle optional API key configuration."""
        if user_input is not None:
            self._data.update(user_input)
            await self.async_set_unique_id(
                location_unique_id(
                    self._data[CONF_LATITUDE],
                    self._data[CONF_LONGITUDE],
                    self._data.get(CONF_LOCATION_NAME),
                )
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._data.get(CONF_LOCATION_NAME, "Weather Fusion AI"),
                data=self._data,
            )
        return self.async_show_form(
            step_id="api_keys",
            data_schema=vol.Schema(
                {vol.Optional(CONF_PIRATE_WEATHER_API_KEY): get_secret_selector()}
            ),
            description_placeholders={
                "name": NAME,
                "pirate_weather_url": "https://pirate-weather.apiable.io/",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return WeatherFusionAIOptionsFlow()


class WeatherFusionAIOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Weather Fusion AI."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            current_options = {**self.config_entry.data, **self.config_entry.options}
            merged_options = {**current_options, **user_input}
            for sensor_key in SENSOR_CONFIG_KEYS:
                if sensor_key not in user_input:
                    merged_options[sensor_key] = None
                elif not user_input.get(sensor_key):
                    merged_options[sensor_key] = None
            if CONF_PIRATE_WEATHER_API_KEY not in user_input:
                merged_options[CONF_PIRATE_WEATHER_API_KEY] = None
            elif not user_input.get(CONF_PIRATE_WEATHER_API_KEY):
                merged_options[CONF_PIRATE_WEATHER_API_KEY] = None
            has_sensors = any(
                (merged_options.get(sensor_key) for sensor_key in SENSOR_CONFIG_KEYS)
            )
            if not has_sensors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(merged_options),
                    errors={"base": "no_sensors"},
                )
            return self.async_create_entry(title="", data=merged_options)
        current_options = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current_options)
        )
