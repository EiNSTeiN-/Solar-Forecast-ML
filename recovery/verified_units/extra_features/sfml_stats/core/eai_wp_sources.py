# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""EAI heat-pump sensors as the read source, with stored STATS values as fallback."""

from __future__ import annotations
from typing import Any
from ..const import (
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HEATPUMP_DAILY,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HP_COMPRESSOR_STARTS,
    CONF_SENSOR_HP_DHW_CHARGING,
    CONF_SENSOR_HP_DHW_MODE,
    CONF_SENSOR_HP_ELECTRIC_POWER,
    CONF_SENSOR_HP_GRID_ENERGY_DAILY,
    CONF_SENSOR_HP_HEATING_MODE,
    CONF_SENSOR_HP_JAZ,
    CONF_SENSOR_HP_PV_ACTIVE,
    CONF_SENSOR_HP_PV_ENERGY_DAILY,
    CONF_SENSOR_HP_STORAGE_TEMP,
    CONF_SENSOR_HP_THERMAL_POWER,
)

EAI_DOMAIN = "solar_forecast_eai"
EAI_INTEGRATION_PATH = "/config/integrations/integration/solar_forecast_eai"
EAI_SENSOR_MENU = "Sensoren"
POWER_UNITS = {
    "w",
    "kw",
    "mw",
    "watt",
    "watts",
    "kilowatt",
    "megawatt",
    "kilowatts",
    "megawatts",
}
ENERGY_UNITS = {"wh", "kwh", "mwh"}
TEMPERATURE_UNITS = {"c", "f", "k", "°c", "°f"}
EAI_HEATPUMP_SENSORS: tuple[dict[str, str], ...] = (
    {
        "key": "wp_power_entity",
        "tier": "Pflicht",
        "unit_class": "power",
        "label": "Elektrische Leistung der Wärmepumpe",
        "effect": "Ohne ihn fehlt die aktuelle elektrische Aufnahme.",
    },
    {
        "key": "wp_energy_today",
        "tier": "Pflicht",
        "unit_class": "energy",
        "label": "Tagesenergie der Wärmepumpe",
        "effect": "Ohne ihn fehlt die elektrische Energie seit Mitternacht.",
    },
    {
        "key": "outdoor_temp_entity",
        "tier": "Pflicht",
        "unit_class": "temperature",
        "label": "Außentemperatur",
        "effect": "Ohne ihn fehlt die Grundlage für Heizbedarf und Prognose.",
    },
    {
        "key": "indoor_temp_entity",
        "tier": "Standard",
        "unit_class": "temperature",
        "label": "Innentemperatur",
        "effect": "Ohne ihn fehlt der Vergleich zum Sollwert.",
    },
    {
        "key": "target_temp_entity",
        "tier": "Standard",
        "unit_class": "temperature",
        "label": "Raum-Solltemperatur",
        "effect": "Ohne ihn fehlt das Komfortziel.",
    },
    {
        "key": "operation_mode_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Betriebsmodus",
        "effect": "Ohne ihn fehlt, ob geheizt, gekühlt oder abgeschaltet wird.",
    },
    {
        "key": "compressor_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Kompressorstatus",
        "effect": "Ohne ihn fehlt, ob die Wärmepumpe gerade arbeitet.",
    },
    {
        "key": "heating_element_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Heizstabstatus",
        "effect": "Ohne ihn fehlt, ob elektrisch nachgeheizt wird.",
    },
    {
        "key": "dhw_temp_entity",
        "tier": "Standard",
        "unit_class": "temperature",
        "label": "Warmwassertemperatur",
        "effect": "Ohne ihn fehlt die Warmwassertemperatur.",
    },
    {
        "key": "flow_temp_entity",
        "tier": "Standard",
        "unit_class": "temperature",
        "label": "Vorlauftemperatur",
        "effect": "Ohne ihn fehlt die abgegebene Heiztemperatur.",
    },
    {
        "key": "return_temp_entity",
        "tier": "Standard",
        "unit_class": "temperature",
        "label": "Rücklauftemperatur",
        "effect": "Ohne ihn fehlt die Temperaturdifferenz des Heizkreises.",
    },
    {
        "key": "starts_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Kompressorstarts",
        "effect": "Ohne ihn fehlt die Taktung des Kompressors.",
    },
    {
        "key": "runtime_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Betriebsstunden des Kompressors",
        "effect": "Ohne ihn fehlt die Laufzeit des Kompressors.",
    },
    {
        "key": "runtime_heating_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Betriebsstunden Heizung",
        "effect": "Ohne ihn fehlt die Laufzeit im Heizbetrieb.",
    },
    {
        "key": "runtime_dhw_entity",
        "tier": "Standard",
        "unit_class": "none",
        "label": "Betriebsstunden Warmwasser",
        "effect": "Ohne ihn fehlt die Laufzeit der Warmwasserbereitung.",
    },
    {
        "key": "heating_element_power_entity",
        "tier": "Standard",
        "unit_class": "power",
        "label": "Elektrische Heizstableistung",
        "effect": "Ohne ihn fehlt die Leistung der elektrischen Zusatzheizung.",
    },
    {
        "key": "heating_element_energy_today_entity",
        "tier": "Standard",
        "unit_class": "energy",
        "label": "Tagesenergie Heizstab",
        "effect": "Ohne ihn fehlt die Heizstab-Energie seit Mitternacht.",
    },
    {
        "key": "thermal_energy_entity",
        "tier": "Erweitert",
        "unit_class": "energy",
        "label": "Thermische Tagesenergie",
        "effect": "Ohne ihn fehlt die abgegebene Wärme seit Mitternacht.",
    },
    {
        "key": "jaz_entity",
        "tier": "Erweitert",
        "unit_class": "none",
        "label": "JAZ / gemessene Arbeitszahl",
        "effect": "Ohne ihn fehlt die gemessene Effizienz.",
    },
    {
        "key": "volume_flow_entity",
        "tier": "Erweitert",
        "unit_class": "none",
        "label": "Volumenstrom",
        "effect": "Ohne ihn fehlt der Heizwasserdurchsatz.",
    },
    {
        "key": "source_temp_entity",
        "tier": "Erweitert",
        "unit_class": "temperature",
        "label": "Quellentemperatur",
        "effect": "Ohne ihn fehlt die Temperatur der Wärmequelle.",
    },
    {
        "key": "storage_temp_entity",
        "tier": "Erweitert",
        "unit_class": "temperature",
        "label": "Speichertemperatur",
        "effect": "Ohne ihn fehlt die Temperatur im Speicher.",
    },
    {
        "key": "storage_ambient_temp_entity",
        "tier": "Erweitert",
        "unit_class": "temperature",
        "label": "Temperatur am Speicher / Heizraum",
        "effect": "Ohne ihn fehlt die Umgebungstemperatur für die Verlustrechnung.",
    },
    {
        "key": "circulation_pump_entity",
        "tier": "Erweitert",
        "unit_class": "none",
        "label": "Zirkulationspumpe",
        "effect": "Ohne ihn fehlt, ob Warmwasser umgewälzt wird.",
    },
    {
        "key": "circulation_return_temp_entity",
        "tier": "Erweitert",
        "unit_class": "temperature",
        "label": "Zirkulations-Rücklauftemperatur",
        "effect": "Ohne ihn fehlt die Rücklauftemperatur der Zirkulation.",
    },
)
STATS_TO_EAI: dict[str, str | None] = {
    CONF_SENSOR_HEATPUMP_POWER: "wp_power_entity",
    CONF_SENSOR_HEATPUMP_DAILY: "wp_energy_today",
    CONF_SENSOR_HEATPUMP_COP: "jaz_entity",
    CONF_SENSOR_HP_HEATING_MODE: "operation_mode_entity",
    CONF_SENSOR_HP_DHW_MODE: None,
    CONF_SENSOR_HP_DHW_CHARGING: None,
    CONF_SENSOR_HP_PV_ACTIVE: None,
    CONF_SENSOR_HP_ELECTRIC_POWER: "wp_power_entity",
    CONF_SENSOR_HP_THERMAL_POWER: None,
    CONF_SENSOR_HP_GRID_ENERGY_DAILY: None,
    CONF_SENSOR_HP_PV_ENERGY_DAILY: None,
    CONF_SENSOR_HP_JAZ: "jaz_entity",
    CONF_SENSOR_HP_COMPRESSOR_STARTS: "starts_entity",
    CONF_SENSOR_HP_STORAGE_TEMP: "storage_temp_entity",
}
_UNIT_SETS = {
    "power": POWER_UNITS,
    "energy": ENERGY_UNITS,
    "temperature": TEMPERATURE_UNITS,
}


def eai_config(hass: Any) -> dict[str, Any]:
    """Merge the first EAI config entry. Empty when EAI is not installed."""
    get_entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if not callable(get_entries):
        return {}
    entries = get_entries(EAI_DOMAIN)
    if not entries:
        return {}
    entry = entries[0]
    return {
        **dict(getattr(entry, "data", {}) or {}),
        **dict(getattr(entry, "options", {}) or {}),
    }


def _clean_entity(value: Any) -> str | None:
    if not isinstance(value, str):
        return
    entity_id = value.strip()
    return entity_id or None


def _state(hass: Any, entity_id: str | None) -> Any:
    if hass is None or not entity_id:
        return None
    states = getattr(hass, "states", None)
    if states is None:
        return
    return states.get(entity_id)


def _unit_text(state: Any) -> str:
    attributes = getattr(state, "attributes", None) or {}
    return str(attributes.get("unit_of_measurement") or "").strip()


def power_unit_accepted(unit: Any) -> bool:
    return str(unit or "").strip().lower() in POWER_UNITS


def _unit_status(unit_class: str, unit: str, available: bool) -> str | None:
    accepted_units = _UNIT_SETS.get(unit_class)
    if not accepted_units or not available:
        return None
    if unit.strip().lower() not in accepted_units:
        return "wrong_unit"


def resolved_heatpump_entity(
    hass: Any, stats_config: dict[str, Any] | None, stats_key: str
) -> str | None:
    """Prefer the mapped EAI entity. Power uses EAI only with a W/kW/MW unit."""
    settings = stats_config or {}
    eai_key = STATS_TO_EAI.get(stats_key)
    if eai_key:
        entity_id = _clean_entity(eai_config(hass).get(eai_key))
        if entity_id:
            if stats_key != CONF_SENSOR_HEATPUMP_POWER:
                return entity_id
            state = _state(hass, entity_id)
            if state is not None and power_unit_accepted(_unit_text(state)):
                return entity_id
    return _clean_entity(settings.get(stats_key))


def mapping_config(hass: Any, stats_config: dict[str, Any] | None) -> dict[str, Any]:
    """Copy of the STATS config with heat-pump entities resolved for readers."""
    settings = dict(stats_config or {})
    for stats_key in STATS_TO_EAI:
        entity_id = resolved_heatpump_entity(hass, settings, stats_key)
        if entity_id:
            settings[stats_key] = entity_id
    return settings


def heatpump_setup_status(hass: Any) -> dict[str, Any]:
    """Read-only sensor status for the heat-pump setup tab. No other config values."""
    get_entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    integration_present = bool(callable(get_entries) and get_entries(EAI_DOMAIN))
    settings = eai_config(hass) if integration_present else {}
    sensors = []
    for sensor_config in EAI_HEATPUMP_SENSORS:
        entity_id = (
            _clean_entity(settings.get(sensor_config["key"]))
            if integration_present
            else None
        )
        state = _state(hass, entity_id)
        state_text = (
            None if state is None else str(getattr(state, "state", "") or "").strip()
        )
        unavailable = entity_id is not None and (
            state is None
            or state_text is None
            or state_text.lower() in {"", "none", "unknown", "unavailable"}
        )
        unit = _unit_text(state) if state is not None and (not unavailable) else ""
        if entity_id is None:
            status = "missing"
            state_text = None
            unit = ""
        elif unavailable:
            status = "unavailable"
            state_text = None
            unit = ""
        else:
            status = _unit_status(sensor_config["unit_class"], unit, True) or "ok"
        sensors.append(
            {
                "key": sensor_config["key"],
                "tier": sensor_config["tier"],
                "label": sensor_config["label"],
                "entity_id": entity_id,
                "state": state_text,
                "unit": unit or None,
                "status": status,
                "effect": sensor_config["effect"],
            }
        )
        continue
    return {
        "feature_enabled": settings.get("heat_pump_enabled") is True,
        "integration_present": integration_present,
        "integration_path": EAI_INTEGRATION_PATH,
        "menu_name": EAI_SENSOR_MENU,
        "sensors": sensors,
    }
