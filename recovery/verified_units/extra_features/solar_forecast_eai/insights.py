# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Licensed heat-pump insights assembled from verified runtime inputs."""
from __future__ import annotations
from collections import deque
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .setup_state import (
    DERIVED_ENERGY_TODAY_KEYS,
    cop_sink_kwargs,
    dhw_forecast_kwargs,
    generator_circuit_flow_implausible,
    hydraulics_setup_incomplete,
    is_false,
    is_true,
    normalize_operation_mode,
    power_watts,
)
from .daily_energy import (
    ENERGY_COUNTER_MODE_CUMULATIVE,
    DailyEnergyTracker,
    counter_rise_limit_kw,
    resolve_energy_counter_mode,
)
from .const import (
    ADVANCED_SENSORS,
    CONF_COP_RATED,
    CONF_DATA_QUALITY_TIER,
    CONF_CIRCULATION_PUMP_ENTITY,
    CONF_CIRCULATION_RETURN_TEMP_ENTITY,
    CONF_COMPRESSOR_ENTITY,
    CONF_DHW_TEMP_ENTITY,
    CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    CONF_FLOW_TEMP_ENTITY,
    CONF_HAS_DHW,
    CONF_HAS_HEATING_ELEMENT,
    CONF_HEAT_PUMP_ENABLED,
    CONF_HEATING_CAPACITY_KW,
    CONF_HEATING_ELEMENT_ENTITY,
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY,
    CONF_HEATING_ELEMENT_POWER_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_JAZ_ENTITY,
    CONF_OPERATION_MODE_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_RUNTIME_COUNTER_SCOPE,
    CONF_RUNTIME_ENTITY,
    CONF_RUNTIME_DHW_ENTITY,
    CONF_RUNTIME_HEATING_ENTITY,
    CONF_SOURCE_TEMP_ENTITY,
    CONF_STARTS_COUNTER_SCOPE,
    CONF_STARTS_ENTITY,
    CONF_STORAGE_AMBIENT_TEMP_ENTITY,
    CONF_STORAGE_TEMP_ENTITY,
    CONF_STORAGE_VOLUME_L,
    CONF_TARGET_TEMP_ENTITY,
    CONF_THERMAL_ENERGY_ENTITY,
    CONF_VOLUME_FLOW_ENTITY,
    CONF_WALLBOX_ENABLED,
    CONF_WALLBOX_MAX_POWER_KW,
    CONF_WP_ENERGY_TODAY,
    CONF_WP_POWER_ENTITY,
    CONF_WP_TYPE,
    DEFAULT_WALLBOX_MAX_POWER_KW,
    COP_MODE_DHW,
    COP_MODE_HEATING,
    DEFAULT_COP_RATED,
    GENERATOR_FLOW_HINT_CLEAR_S,
    GENERATOR_FLOW_HINT_DE,
    GENERATOR_FLOW_HINT_EN,
    GENERATOR_FLOW_HINT_PERSIST_S,
    GENERATOR_FLOW_HYSTERESIS_K,
    GENERATOR_FLOW_THERMAL_REASON,
    GENERATOR_FLOW_TOLERANCE_K,
    DEFAULT_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    DEFAULT_HEATING_CAPACITY_KW,
    DEFAULT_WP_TYPE,
    COUNTER_SCOPE_DAILY,
    COUNTER_SCOPE_LIFETIME,
    COUNTER_SCOPE_UNKNOWN,
    ELECTRICAL_TOPOLOGY_LEGACY_AGGREGATE,
    ELECTRICAL_TOPOLOGY_SEPARATE,
    REQUIRED_SENSORS,
    SINK_ORIGIN_UNCONFIGURED,
    STANDARD_SENSORS,
)
from .energy_context import EAISharedEnergyAllocator, WallboxEnvelope
from .mobility import EAIMobilityPlanner
from .assignment_policy import is_allowed_shared_assignment
from .thermal_loss import thermal_loss_snapshot

_SAMPLE_LIMIT = 2016
_STALE_SECONDS_BY_KIND = {
    "live": 600,
    "status": 1800,
    "temperature": 1800,
    "daily": 21600,
    "daily_counter": int(91800.0),
    "lifetime": 86400,
}
_STATE_AVAILABILITY_FRESHNESS_KINDS = frozenset({"status", "temperature"})
_MAX_FUTURE_CLOCK_SKEW_SECONDS = 300
_COP_MAX_INPUT_SKEW_SECONDS = 300
_STANDBY_POWER_MAX_W = 150.0
_FLOW_RETURN_INVERSION_MIN_K = 2.0
_MIN_COP_ELECTRICAL_POWER_W = 100.0
_LAST_VALID_MAX_AGE = timedelta(hours=24)
_MAX_PLAUSIBLE_AVERAGE_CYCLE_MINUTES = 10080
_MAX_PLAUSIBLE_DAILY_RUNTIME_HOURS = 25.5
_WEATHER_FORECAST_MAX_AGE = timedelta(hours=6)
_BUILDING_LEARNING_REQUIRED_HOURS = 24.0
_LEARNING_NUMERIC_RANGES = {
    "power_w": (0.0, 100000.0),
    "indoor_c": (-100.0, 200.0),
    "outdoor_c": (-100.0, 200.0),
    "target_c": (-100.0, 200.0),
    "thermal_power_kw": (0.0, 200.0),
    "storage_c": (-100.0, 200.0),
    "storage_ambient_c": (-100.0, 200.0),
    "circulation_return_c": (-100.0, 200.0),
}
_LEARNING_BOOLEAN_FIELDS = frozenset(
    {"compressor_on", "circulation_on", "heating_element_on"}
)
_LEARNING_OPERATION_MODES = frozenset(
    {"dhw", "defrost", "heating", "standby", "unknown"}
)
_FRESHNESS_KIND_BY_SENSOR = {
    CONF_WP_POWER_ENTITY: "live",
    CONF_HEATING_ELEMENT_POWER_ENTITY: "live",
    CONF_VOLUME_FLOW_ENTITY: "live",
    CONF_CIRCULATION_PUMP_ENTITY: "status",
    CONF_COMPRESSOR_ENTITY: "status",
    CONF_HEATING_ELEMENT_ENTITY: "status",
    CONF_OPERATION_MODE_ENTITY: "status",
    CONF_OUTDOOR_TEMP_ENTITY: "temperature",
    CONF_INDOOR_TEMP_ENTITY: "temperature",
    CONF_TARGET_TEMP_ENTITY: "temperature",
    CONF_DHW_TEMP_ENTITY: "temperature",
    CONF_FLOW_TEMP_ENTITY: "temperature",
    CONF_RETURN_TEMP_ENTITY: "temperature",
    CONF_SOURCE_TEMP_ENTITY: "temperature",
    CONF_STORAGE_TEMP_ENTITY: "temperature",
    CONF_STORAGE_AMBIENT_TEMP_ENTITY: "temperature",
    CONF_CIRCULATION_RETURN_TEMP_ENTITY: "temperature",
    CONF_WP_ENERGY_TODAY: "daily",
    CONF_THERMAL_ENERGY_ENTITY: "daily",
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY: "daily",
    CONF_JAZ_ENTITY: "lifetime",
}
_INPUT_LABELS = {
    CONF_WP_POWER_ENTITY: "Elektrische Leistung der Wärmepumpe",
    CONF_WP_ENERGY_TODAY: "Tagesenergie der Wärmepumpe",
    CONF_OUTDOOR_TEMP_ENTITY: "Außentemperatur",
    CONF_INDOOR_TEMP_ENTITY: "Innentemperatur",
    CONF_TARGET_TEMP_ENTITY: "Raum-Solltemperatur",
    CONF_OPERATION_MODE_ENTITY: "Betriebsmodus",
    CONF_COMPRESSOR_ENTITY: "Kompressorstatus",
    CONF_HEATING_ELEMENT_ENTITY: "Heizstabstatus",
    CONF_HEATING_ELEMENT_POWER_ENTITY: "Elektrische Heizstableistung",
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY: "Tagesenergie Heizstab",
    CONF_DHW_TEMP_ENTITY: "Warmwassertemperatur",
    CONF_FLOW_TEMP_ENTITY: "Vorlauftemperatur",
    CONF_RETURN_TEMP_ENTITY: "Rücklauftemperatur",
    CONF_STARTS_ENTITY: "Kompressorstarts",
    CONF_RUNTIME_ENTITY: "Betriebsstunden des Kompressors",
    CONF_RUNTIME_HEATING_ENTITY: "Betriebsstunden Heizung",
    CONF_RUNTIME_DHW_ENTITY: "Betriebsstunden Warmwasser",
    CONF_THERMAL_ENERGY_ENTITY: "Thermische Tagesenergie",
    CONF_JAZ_ENTITY: "JAZ / gemessene Arbeitszahl",
    CONF_VOLUME_FLOW_ENTITY: "Volumenstrom",
    CONF_SOURCE_TEMP_ENTITY: "Quellentemperatur",
    CONF_STORAGE_TEMP_ENTITY: "Speichertemperatur",
    CONF_STORAGE_AMBIENT_TEMP_ENTITY: "Temperatur am Speicher / Heizraum",
    CONF_CIRCULATION_PUMP_ENTITY: "Zirkulationspumpe",
    CONF_CIRCULATION_RETURN_TEMP_ENTITY: "Zirkulations-Rücklauftemperatur",
    "outdoor_temperature_forecast": "Außentemperatur-Prognose",
}
_REMEDIATIONS = {
    "not_configured": {
        "code": "configure_required_inputs",
        "target": "eai_configuration",
        "instruction": "Öffne die EAI-Einrichtung und ordne die betroffenen Messgrößen zu.",
    },
    "unavailable": {
        "code": "restore_data_sources",
        "target": "source_integration",
        "instruction": "Prüfe die betroffenen Datenquellen und stelle gültige Messwerte wieder her.",
    },
    "stale": {
        "code": "refresh_stale_sources",
        "target": "source_integration",
        "instruction": "Prüfe, warum die betroffenen Messwerte nicht mehr aktualisiert werden.",
    },
    "duplicate_assignment": {
        "code": "assign_independent_sources",
        "target": "eai_configuration",
        "instruction": "Ordne jeder betroffenen Messgröße eine eigenständige Datenquelle zu.",
    },
    "forecast_temperature_fallback": {
        "code": "configure_forecast_temperature",
        "target": "forecast_configuration",
        "instruction": "Der aktuelle Außentemperatursensor ist nutzbar. Stelle zusätzlich eine zukünftige stündliche Temperaturprognose für die Modellberechnung bereit.",
    },
    "flow_return_inverted": {
        "code": "verify_flow_and_return_assignment",
        "target": "eai_configuration",
        "instruction": "Prüfe, ob Vorlauf- und Rücklauffühler zum selben Erzeugerkreis der Wärmepumpe gehören. Sie können vertauscht zugeordnet sein oder auf verschiedenen Seiten eines Pufferspeichers sitzen - dann ist ihre Differenz kein Temperaturhub des Erzeugerkreises.",
    },
    "generator_circuit_flow_implausible": {
        "code": "assign_generator_circuit_flow",
        "target": "eai_configuration",
        "instruction": GENERATOR_FLOW_HINT_DE,
        "instruction_en": GENERATOR_FLOW_HINT_EN,
    },
    "short_cycling_observed": {
        "code": "review_compressor_cycling",
        "target": "heat_pump_operation",
        "instruction": "Prüfe Heizkurve, Mindestlaufzeit und Hydraulik. EAI hat bei mindestens fünf Starts eine mittlere Laufzeit unter zehn Minuten erkannt.",
    },
}


def _customer_visible_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return
    stripped_label = value.strip()
    if not stripped_label or "_entity" in stripped_label:
        return None
    folded_label = stripped_label.casefold()
    for entity_domain_prefix in (
        "sensor.",
        "binary_sensor.",
        "switch.",
        "climate.",
        "number.",
        "input_number.",
    ):
        if not entity_domain_prefix in folded_label:
            continue
        return
    return stripped_label


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and isfinite(value) else None


def rebase_daily_energy_baselines(tracker: DailyEnergyTracker, local_day: date) -> None:
    """Baseline and last reading become the current counter. Carry is cleared."""
    tracker_state = tracker.export_state()
    counter_states = tracker_state.get("counters")
    if not isinstance(counter_states, dict):
        return
    for counter_state in counter_states.values():
        if not isinstance(counter_state, dict) or "last" not in counter_state:
            continue
        counter_state["baseline"] = counter_state["last"]
        counter_state["carry"] = 0.0
        counter_state["date"] = local_day.isoformat()
        counter_state["complete"] = False
        counter_state.pop("pending", None)
        counter_state.pop("anchor", None)
    tracker.restore_state(tracker_state)


class EAIInsightsEngine:
    """Create read-only explainable insights without mutating source systems."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self._config = {}
        self._entitlements = frozenset()
        self._samples = deque(maxlen=_SAMPLE_LIMIT)
        self._last_sample_minute = None
        self._weather_temperature_sources = {}
        self._runtime = None
        self._mobility = EAIMobilityPlanner(hass)
        self._energy_allocator = EAISharedEnergyAllocator(hass)
        self._daily_energy = DailyEnergyTracker()
        self._generator_flow_suspect_since = None
        self._generator_flow_plausible_since = None
        self._generator_flow_hint = False

    def attach_runtime(self, runtime: Any | None) -> None:
        """Use learned EAI forecasts when the licensed runtime is ready."""
        self._runtime = runtime

    def configure(self, config: dict[str, Any], entitlements: frozenset[str]) -> None:
        self._config = dict(config)
        self._entitlements = entitlements

    def _wallbox_envelope(self) -> WallboxEnvelope:
        """Match mobility's 1–50 kW clamp so allocator and planner share one cap."""
        if not self._config.get(CONF_WALLBOX_ENABLED):
            return WallboxEnvelope()
        configured_max_power_kw = self._config.get(
            CONF_WALLBOX_MAX_POWER_KW, DEFAULT_WALLBOX_MAX_POWER_KW
        )
        try:
            max_power_kw = float(configured_max_power_kw)
        except (TypeError, ValueError):
            return WallboxEnvelope(enabled=True, max_power_kw=None)
        if not isfinite(max_power_kw):
            return WallboxEnvelope(enabled=True, max_power_kw=None)
        return WallboxEnvelope(
            enabled=True, max_power_kw=min(50.0, max(1.0, max_power_kw))
        )

    def capture_learning_sample(self) -> bool:
        """Capture one bounded learning sample independently of dashboard views."""
        now = datetime.now(timezone.utc)
        previous_sample_count = len(self._samples)
        self._capture_sample(now, self._read_sensors(now))
        return len(self._samples) != previous_sample_count

    def export_learning_samples(self) -> list[dict[str, Any]]:
        """Return JSON-safe recent samples for entry-scoped persistence."""
        exported_samples = []
        for sample in self._samples:
            sanitized_sample = self._sanitize_learning_sample(sample)
            if sanitized_sample is not None:
                exported_samples.append(
                    {
                        **sanitized_sample,
                        "timestamp": sanitized_sample["timestamp"].isoformat(),
                    }
                )
        return exported_samples

    def restore_learning_samples(self, samples: Any) -> None:
        """Restore only valid, recent samples from the entry-scoped store."""
        if not isinstance(samples, list):
            return
        now = datetime.now(timezone.utc)
        earliest_timestamp = now - timedelta(days=7)
        restored_samples = []
        for raw_sample in samples[-_SAMPLE_LIMIT:]:
            sanitized_sample = self._sanitize_learning_sample(raw_sample)
            if sanitized_sample is not None:
                sample_timestamp = sanitized_sample["timestamp"]
                if (
                    not earliest_timestamp
                    <= sample_timestamp
                    <= now + timedelta(minutes=5)
                ):
                    continue
                restored_samples.append(sanitized_sample)
        self._samples.clear()
        self._samples.extend(
            sorted(restored_samples, key=lambda item: item["timestamp"])
        )
        if self._samples:
            self._last_sample_minute = self._sample_bucket(
                self._samples[-1]["timestamp"]
            )
        return None if True else None

    def export_daily_energy_state(self) -> dict[str, Any]:
        """Return the midnight baselines of every cumulative energy counter."""
        return self._daily_energy.export_state()

    def restore_daily_energy_state(self, payload: Any) -> None:
        """Restore midnight baselines so a restart does not lose today's energy."""
        self._daily_energy.restore_state(payload)

    @property
    def daily_energy_dirty(self) -> bool:
        """Return whether a cumulative baseline changed since the last export."""
        return self._daily_energy.dirty

    @staticmethod
    def _sanitize_learning_sample(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return
        raw_timestamp = value.get("timestamp")
        if isinstance(raw_timestamp, datetime):
            sample_timestamp = raw_timestamp
        elif isinstance(raw_timestamp, str):
            try:
                sample_timestamp = datetime.fromisoformat(
                    raw_timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                return
        else:
            return
        if sample_timestamp.tzinfo is None:
            sample_timestamp = sample_timestamp.replace(tzinfo=timezone.utc)
        sanitized_sample = {"timestamp": sample_timestamp.astimezone(timezone.utc)}
        for field_name, (
            minimum_value,
            maximum_value,
        ) in _LEARNING_NUMERIC_RANGES.items():
            raw_value = value.get(field_name)
            if raw_value is None:
                continue
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                continue
            numeric_value = float(raw_value)
            if isfinite(numeric_value):
                if minimum_value <= numeric_value <= maximum_value:
                    sanitized_sample[field_name] = numeric_value
        for field_name in _LEARNING_BOOLEAN_FIELDS:
            raw_value = value.get(field_name)
            if isinstance(raw_value, bool):
                sanitized_sample[field_name] = raw_value
        operation_mode = value.get("operation_mode")
        sanitized_sample["operation_mode"] = (
            operation_mode if operation_mode in _LEARNING_OPERATION_MODES else "unknown"
        )
        return sanitized_sample

    def snapshot(self) -> tuple[dict[str, dict[str, Any]], datetime]:
        now = datetime.now(timezone.utc)
        sensor_readings = self._read_sensors(now)
        self._capture_sample(now, sensor_readings)
        forecast = self._forecast(sensor_readings, now)
        mobility = self._mobility.snapshot(self._config, forecast, now)
        operation = self._operation(sensor_readings)
        efficiency = self._efficiency(sensor_readings, now)
        building = self._building(sensor_readings)
        energy = self._energy(forecast)
        diagnostics = self._diagnostics(
            sensor_readings, forecast, operation, building, now
        )
        overview = self._overview(
            sensor_readings,
            forecast,
            operation,
            efficiency,
            building,
            energy,
            diagnostics,
            now,
        )
        snapshot_data = {
            "overview": overview,
            "forecast": self._gate("forecast_enabled", forecast),
            "operation": operation,
            "efficiency": efficiency,
            "building": self._gate("building_analysis_enabled", building),
            "energy": self._gate("forecast_enabled", energy),
            "mobility": self._gate("forecast_enabled", mobility),
            "diagnostics": self._gate("advanced_diagnostics_enabled", diagnostics),
        }
        return (snapshot_data, now)

    def _gate(self, entitlement: str, data: dict[str, Any]) -> dict[str, Any]:
        if entitlement in self._entitlements:
            return data
        return {"available": False, "locked": True, "required_entitlement": entitlement}

    def _read_sensors(self, now: datetime) -> dict[str, dict[str, Any]]:
        input_keys = REQUIRED_SENSORS + STANDARD_SENSORS + ADVANCED_SENSORS
        sensor_readings = {
            input_key: self._reading(input_key, now) for input_key in input_keys
        }
        self._accept_unchanged_standby_power(sensor_readings)
        self._accept_unchanged_standby_volume_flow(sensor_readings)
        self._accept_unchanged_inactive_heating_element_power(sensor_readings)
        return sensor_readings

    def _standby_evidence_confirms_idle(
        self, readings: dict[str, dict[str, Any]]
    ) -> bool:
        idle_evidence = []
        compressor_reading = readings[CONF_COMPRESSOR_ENTITY]
        if compressor_reading.get("entity_id") and compressor_reading.get("available"):
            compressor_on = self._binary_on({**compressor_reading, "stale": False})
            if compressor_on is not None:
                idle_evidence.append(compressor_on is False)
        mode_reading = readings[CONF_OPERATION_MODE_ENTITY]
        if mode_reading.get("entity_id") and mode_reading.get("available"):
            normalized_mode = self._normalized_mode(mode_reading.get("state"))
            if normalized_mode is not None:
                idle_evidence.append(normalized_mode == "standby")
        return bool(idle_evidence) and all(idle_evidence)

    def _accept_unchanged_standby_power(
        self, readings: dict[str, dict[str, Any]]
    ) -> None:
        """Keep an unchanged low standby load usable when status confirms idle."""
        power_reading = readings[CONF_WP_POWER_ENTITY]
        if (
            not (power_reading.get("available") and power_reading.get("stale"))
            or float(power_reading.get("future_skew_seconds") or 0)
            > _MAX_FUTURE_CLOCK_SKEW_SECONDS
        ):
            return None if True else None
        electrical_power_w = self._power_w({**power_reading, "stale": False})
        if (
            electrical_power_w is None
            or not 0 <= electrical_power_w < _STANDBY_POWER_MAX_W
        ):
            return None if True else None
        if self._standby_evidence_confirms_idle(readings):
            power_reading["stale"] = False
            power_reading["freshness_exception"] = "confirmed_standby"
        return None if True else None

    def _accept_unchanged_standby_volume_flow(
        self, readings: dict[str, dict[str, Any]]
    ) -> None:
        """Keep an unchanged zero flow usable when status confirms idle."""
        volume_flow_reading = readings[CONF_VOLUME_FLOW_ENTITY]
        if (
            not (
                volume_flow_reading.get("available")
                and volume_flow_reading.get("stale")
            )
            or float(volume_flow_reading.get("future_skew_seconds") or 0)
            > _MAX_FUTURE_CLOCK_SKEW_SECONDS
        ):
            return
        volume_flow_l_min = self._volume_l_min({**volume_flow_reading, "stale": False})
        if volume_flow_l_min != 0 or not self._standby_evidence_confirms_idle(readings):
            return None
        volume_flow_reading["stale"] = False
        volume_flow_reading["freshness_exception"] = "confirmed_standby"

    def _accept_unchanged_inactive_heating_element_power(
        self, readings: dict[str, dict[str, Any]]
    ) -> None:
        """Keep an unchanged zero heater load usable when its status is off."""
        power_reading = readings[CONF_HEATING_ELEMENT_POWER_ENTITY]
        heating_element_reading = readings[CONF_HEATING_ELEMENT_ENTITY]
        if (
            not (power_reading.get("available") and power_reading.get("stale"))
            or float(power_reading.get("future_skew_seconds") or 0)
            > _MAX_FUTURE_CLOCK_SKEW_SECONDS
        ):
            return None if True else None
        heating_element_power_w = self._power_w({**power_reading, "stale": False})
        heating_element_on = self._binary_on(
            {**heating_element_reading, "stale": False}
        )
        if (
            heating_element_power_w is None
            or not 0 <= heating_element_power_w < 50
            or heating_element_on is not False
        ):
            return None if True else None
        power_reading["stale"] = False
        power_reading["freshness_exception"] = "confirmed_inactive_heating_element"

    def _reading(self, key: str, now: datetime) -> dict[str, Any]:
        if key in {
            CONF_HEATING_ELEMENT_ENTITY,
            CONF_HEATING_ELEMENT_POWER_ENTITY,
            CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY,
        } and is_false(self._config, CONF_HAS_HEATING_ELEMENT):
            return {"entity_id": None, "available": False, "reason": "disabled"}
        entity_id = self._config.get(key)
        if not entity_id:
            return {"entity_id": None, "available": False, "reason": "not_configured"}
        entity_state = self.hass.states.get(entity_id)
        if entity_state is None or entity_state.state in {
            "",
            "none",
            "unknown",
            "unavailable",
        }:
            return {"entity_id": entity_id, "available": False, "reason": "unavailable"}
        attributes = getattr(entity_state, "attributes", {}) or {}
        last_reported = getattr(entity_state, "last_reported", None)
        last_updated = getattr(entity_state, "last_updated", None)
        report_age_seconds = self._timestamp_age(now, last_reported)
        update_age_seconds = self._timestamp_age(now, last_updated)
        freshness_kind = self._freshness_kind(key, attributes.get("state_class"))
        maximum_age_seconds = _STALE_SECONDS_BY_KIND[freshness_kind]
        age_seconds = (
            report_age_seconds if report_age_seconds is not None else update_age_seconds
        )
        source_timestamp = (
            last_reported if report_age_seconds is not None else last_updated
        )
        freshness_at = self._timestamp_utc(source_timestamp)
        future_skew_seconds = self._timestamp_future_skew(now, source_timestamp)
        if future_skew_seconds > _MAX_FUTURE_CLOCK_SKEW_SECONDS:
            age_seconds = future_skew_seconds
        raw_state = entity_state.state
        climate_attribute = None
        if entity_id.startswith("climate."):
            climate_attribute = {
                CONF_INDOOR_TEMP_ENTITY: "current_temperature",
                CONF_TARGET_TEMP_ENTITY: "temperature",
            }.get(key)
            if climate_attribute:
                raw_state = attributes.get(climate_attribute)
                if raw_state is None:
                    return {
                        "entity_id": entity_id,
                        "available": False,
                        "reason": "unavailable",
                    }
        unit = attributes.get("unit_of_measurement") or attributes.get(
            "temperature_unit"
        )
        if not unit:
            if climate_attribute or freshness_kind == "temperature":
                unit = self._system_temperature_unit()
        try:
            numeric_value = float(str(raw_state).replace(",", "."))
        except (TypeError, ValueError):
            numeric_value = None
        energy_counter_mode = None
        daily_energy = None
        if key in DERIVED_ENERGY_TODAY_KEYS:
            energy_counter_mode = resolve_energy_counter_mode(
                self._config, attributes.get("state_class")
            )
            if energy_counter_mode == ENERGY_COUNTER_MODE_CUMULATIVE:
                daily_energy = self._daily_energy.observe(
                    entity_id,
                    value=numeric_value,
                    unit=attributes.get("unit_of_measurement"),
                    local_date=now.astimezone(self._local_timezone()).date(),
                    observed_at=self._timestamp_utc(source_timestamp),
                    max_rise_kw=self._counter_rise_limit_kw(key),
                )
                if daily_energy is None:
                    return {
                        "entity_id": entity_id,
                        "available": False,
                        "reason": "unavailable",
                    }
        daily_counter_wrong_day = bool(
            freshness_kind == "daily_counter"
            and isinstance(source_timestamp, datetime)
            and (
                self._timestamp_utc(source_timestamp)
                .astimezone(self._local_timezone())
                .date()
                != now.astimezone(self._local_timezone()).date()
            )
        )
        state_availability_freshness = (
            freshness_kind in _STATE_AVAILABILITY_FRESHNESS_KINDS
        )
        stale = bool(
            future_skew_seconds > _MAX_FUTURE_CLOCK_SKEW_SECONDS
            or daily_counter_wrong_day
            or (
                not state_availability_freshness
                and age_seconds is not None
                and (age_seconds > maximum_age_seconds)
            )
        )
        return {
            "entity_id": entity_id,
            "available": True,
            "state": entity_state.state,
            "numeric": daily_energy.kwh if daily_energy else numeric_value,
            "unit": "kWh" if daily_energy else unit,
            "energy_counter_mode": energy_counter_mode,
            "energy_origin": daily_energy.origin if daily_energy else None,
            "cumulative_counter_kwh": _round(
                daily_energy.counter_kwh if daily_energy else None
            ),
            "device_class": attributes.get("device_class"),
            "state_class": attributes.get("state_class"),
            "friendly_name": attributes.get("friendly_name"),
            "last_reset": attributes.get("last_reset"),
            "counter_period": attributes.get("counter_period")
            or attributes.get("period")
            or attributes.get("cycle"),
            "age_seconds": _round(age_seconds, 0),
            "report_age_seconds": _round(report_age_seconds, 0),
            "update_age_seconds": _round(update_age_seconds, 0),
            "future_skew_seconds": _round(future_skew_seconds, 0),
            "freshness_source": (
                "last_reported"
                if report_age_seconds is not None
                else (
                    "last_updated"
                    if update_age_seconds is not None
                    else "not_available"
                )
            ),
            "freshness_kind": freshness_kind,
            "freshness_at": freshness_at,
            "freshness_exception": (
                "state_availability" if state_availability_freshness else None
            ),
            "stale": stale,
            "maximum_age_seconds": maximum_age_seconds,
        }

    def _flow_return_inverted(self, readings: dict[str, dict[str, Any]]) -> bool:
        """Return whether the generator-circuit flow reads colder than its return.

        Only space heating makes that impossible: the heat pump delivers heat
        into the generator circuit, so flow is the warmer end. The check
        therefore runs only in a confirmed heating mode while the compressor
        draws power.

        DHW charging, cooling, and a resting plant are excluded. A colder
        flow in those states is not reported as a swapped generator-circuit
        sensor.
        """
        flow_c = self._temperature_c(readings[CONF_FLOW_TEMP_ENTITY])
        return_c = self._temperature_c(readings[CONF_RETURN_TEMP_ENTITY])
        if flow_c is None or return_c is None:
            return False
        if return_c - flow_c < _FLOW_RETURN_INVERSION_MIN_K:
            return False
        electrical_power_w = self._power_w(readings[CONF_WP_POWER_ENTITY])
        if electrical_power_w is None or electrical_power_w <= _STANDBY_POWER_MAX_W:
            return False
        operation_mode = self._normalized_mode(
            self._reading_state(readings[CONF_OPERATION_MODE_ENTITY])
        )
        if operation_mode != COP_MODE_HEATING:
            return False
        volume_flow_l_min = self._volume_l_min(readings[CONF_VOLUME_FLOW_ENTITY])
        return volume_flow_l_min is None or volume_flow_l_min > 0

    def _explicit_compressor_on(
        self, readings: dict[str, dict[str, Any]]
    ) -> bool | None:
        """Compressor contact only. Mode and power are not a substitute."""
        reading = readings.get(CONF_COMPRESSOR_ENTITY, {})
        if (
            not reading.get("entity_id")
            or not reading.get("available")
            or reading.get("stale")
        ):
            return None
        return self._binary_on(reading)

    def _generator_reference_key(self) -> str | None:
        if self._config.get(CONF_STORAGE_TEMP_ENTITY):
            return CONF_STORAGE_TEMP_ENTITY
        if self._config.get(CONF_DHW_TEMP_ENTITY):
            return CONF_DHW_TEMP_ENTITY

    def _generator_inputs_fresh(self, readings: dict[str, dict[str, Any]]) -> bool:
        required_input_keys = [CONF_FLOW_TEMP_ENTITY, CONF_WP_POWER_ENTITY]
        if self._config.get(CONF_RETURN_TEMP_ENTITY):
            required_input_keys.append(CONF_RETURN_TEMP_ENTITY)
        if self._config.get(CONF_OPERATION_MODE_ENTITY):
            required_input_keys.append(CONF_OPERATION_MODE_ENTITY)
        reference_key = self._generator_reference_key()
        if reference_key is not None:
            required_input_keys.append(reference_key)
        synchronous_readings = []
        for input_key in required_input_keys:
            reading = readings.get(input_key, {})
            if not reading.get("available") or reading.get("stale"):
                return False
            synchronous_readings.append(reading)
        return self._inputs_synchronous(*synchronous_readings)

    def _dhw_mode_seconds(
        self, now: datetime, readings: dict[str, dict[str, Any]]
    ) -> float:
        """Seconds of the current continuous DHW episode, ending at ``now``."""
        operation_mode = self._normalized_mode(
            self._reading_state(readings.get(CONF_OPERATION_MODE_ENTITY, {}))
        )
        if operation_mode != COP_MODE_DHW:
            return 0.0
        episode_start = now
        for sample in reversed(self._samples):
            if sample.get("operation_mode") != COP_MODE_DHW:
                break
            sample_timestamp = sample.get("timestamp")
            if not isinstance(sample_timestamp, datetime):
                break
            if sample_timestamp.tzinfo is None:
                sample_timestamp = sample_timestamp.replace(tzinfo=timezone.utc)
            episode_start = sample_timestamp
        return max(0.0, (now - episode_start).total_seconds())

    def _generator_reference_c(
        self, readings: dict[str, dict[str, Any]]
    ) -> float | None:
        reference_key = self._generator_reference_key()
        if reference_key is None:
            return
        return self._temperature_c(readings.get(reference_key, {}))

    def _judge_generator_flow(
        self,
        readings: dict[str, dict[str, Any]],
        now: datetime,
        *,
        margin_k: float = GENERATOR_FLOW_TOLERANCE_K,
    ) -> bool | None:
        return_c = None
        if self._config.get(CONF_RETURN_TEMP_ENTITY):
            return_c = self._temperature_c(readings.get(CONF_RETURN_TEMP_ENTITY, {}))
        return generator_circuit_flow_implausible(
            has_dhw=is_true(self._config, CONF_HAS_DHW),
            mode=self._normalized_mode(
                self._reading_state(readings.get(CONF_OPERATION_MODE_ENTITY, {}))
            ),
            compressor_on=self._explicit_compressor_on(readings),
            power_w=self._power_w(readings.get(CONF_WP_POWER_ENTITY, {})),
            heating_element_on=self._heating_element_on(readings),
            inputs_fresh_and_sync=self._generator_inputs_fresh(readings),
            dhw_active_seconds=self._dhw_mode_seconds(now, readings),
            flow_c=self._temperature_c(readings.get(CONF_FLOW_TEMP_ENTITY, {})),
            return_c=return_c,
            reference_c=self._generator_reference_c(readings),
            margin_k=margin_k,
        )

    def _observe_generator_flow(
        self, readings: dict[str, dict[str, Any]], now: datetime
    ) -> bool:
        """Update the hint latch and return whether this sample is unusable.

        A skipped sample pauses both timers. One judged sample cannot arm or
        clear the hint; that takes the persist and clear dwells. Recovery uses
        a tighter margin so a reading on the trip line does not flutter.
        """
        flow_implausible = self._judge_generator_flow(readings, now)
        recovery_margin_k = GENERATOR_FLOW_TOLERANCE_K - GENERATOR_FLOW_HYSTERESIS_K
        if flow_implausible is True:
            self._generator_flow_plausible_since = None
            if self._generator_flow_suspect_since is None:
                self._generator_flow_suspect_since = now
            elif (
                now - self._generator_flow_suspect_since
            ).total_seconds() >= GENERATOR_FLOW_HINT_PERSIST_S:
                self._generator_flow_hint = True
        elif (
            flow_implausible is False
            and self._judge_generator_flow(readings, now, margin_k=recovery_margin_k)
            is False
        ):
            self._generator_flow_suspect_since = None
            if self._generator_flow_hint:
                if self._generator_flow_plausible_since is None:
                    self._generator_flow_plausible_since = now
                elif (
                    now - self._generator_flow_plausible_since
                ).total_seconds() >= GENERATOR_FLOW_HINT_CLEAR_S:
                    self._generator_flow_hint = False
                    self._generator_flow_plausible_since = None
            else:
                self._generator_flow_plausible_since = None
        elif flow_implausible is False:
            self._generator_flow_suspect_since = None
            self._generator_flow_plausible_since = None
        return flow_implausible is True if True else None

    def _generator_sink_kwargs(
        self, readings: dict[str, dict[str, Any]], now: datetime
    ) -> dict[str, Any]:
        return_c = None
        if self._config.get(CONF_RETURN_TEMP_ENTITY):
            return_c = self._temperature_c(readings.get(CONF_RETURN_TEMP_ENTITY, {}))
        return cop_sink_kwargs(
            self._config,
            flow_temp=self._temperature_c(readings.get(CONF_FLOW_TEMP_ENTITY, {})),
            operation_mode=self._normalized_mode(
                self._reading_state(readings.get(CONF_OPERATION_MODE_ENTITY, {}))
            ),
            return_temp=return_c,
            reference_temp=self._generator_reference_c(readings),
            compressor_on=self._explicit_compressor_on(readings),
            power_w=self._power_w(readings.get(CONF_WP_POWER_ENTITY, {})),
            heating_element_on=self._heating_element_on(readings),
            inputs_fresh_and_sync=self._generator_inputs_fresh(readings),
            dhw_active_seconds=self._dhw_mode_seconds(now, readings),
        )

    def _language_is_english(self) -> bool:
        hass_config = getattr(self.hass, "config", None)
        language = str(getattr(hass_config, "language", "") or "")
        return language.lower().startswith("en")

    def _system_temperature_unit(self) -> str:
        """Return the unit Home Assistant publishes temperatures in."""
        unit_system = getattr(getattr(self.hass, "config", None), "units", None)
        unit = getattr(unit_system, "temperature_unit", None)
        return str(unit) if unit else "°C"

    def _counter_rise_limit_kw(self, key: str) -> float:
        energy_kind = "thermal" if key == CONF_THERMAL_ENERGY_ENTITY else "electrical"
        return counter_rise_limit_kw(
            energy_kind, self._configured_heating_capacity_kw()
        )

    def _configured_heating_capacity_kw(self) -> float | None:
        configured_capacity_kw = self._config.get(CONF_HEATING_CAPACITY_KW)
        if isinstance(configured_capacity_kw, bool) or configured_capacity_kw is None:
            return
        try:
            heating_capacity_kw = float(configured_capacity_kw)
        except (TypeError, ValueError):
            return
        if not isfinite(heating_capacity_kw) or heating_capacity_kw <= 0:
            return None
        return heating_capacity_kw

    @staticmethod
    def _freshness_diagnostic(reading: dict[str, Any]) -> dict[str, Any]:
        freshness_diagnostic = {
            "source": reading.get("freshness_source"),
            "kind": reading.get("freshness_kind"),
            "maximum_age_seconds": reading.get("maximum_age_seconds"),
            "future_skew_seconds": reading.get("future_skew_seconds"),
            "exception": reading.get("freshness_exception"),
        }
        energy_counter_mode = reading.get("energy_counter_mode")
        if energy_counter_mode:
            freshness_diagnostic["energy_counter_mode"] = energy_counter_mode
        return freshness_diagnostic

    def _freshness_kind(self, key: str, state_class: Any = None) -> str:
        if (
            key in DERIVED_ENERGY_TODAY_KEYS
            and resolve_energy_counter_mode(self._config, state_class)
            == ENERGY_COUNTER_MODE_CUMULATIVE
        ):
            return "lifetime"
        if key == CONF_STARTS_ENTITY:
            return (
                "daily_counter"
                if self._config.get(CONF_STARTS_COUNTER_SCOPE) == COUNTER_SCOPE_DAILY
                else "lifetime"
            )
        if key in {
            CONF_RUNTIME_ENTITY,
            CONF_RUNTIME_HEATING_ENTITY,
            CONF_RUNTIME_DHW_ENTITY,
        }:
            return (
                "daily_counter"
                if self._config.get(CONF_RUNTIME_COUNTER_SCOPE) == COUNTER_SCOPE_DAILY
                else "lifetime"
            )
        return _FRESHNESS_KIND_BY_SENSOR.get(key, "status")

    @staticmethod
    def _timestamp_utc(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return
        return (
            value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        ).astimezone(timezone.utc)

    @staticmethod
    def _timestamp_age(now: datetime, value: Any) -> float | None:
        timestamp_utc = EAIInsightsEngine._timestamp_utc(value)
        if timestamp_utc is None:
            return
        now_utc = EAIInsightsEngine._timestamp_utc(now)
        if now_utc is None:
            return
        return max(
            0.0,
            (
                now_utc.astimezone(timezone.utc)
                - timestamp_utc.astimezone(timezone.utc)
            ).total_seconds(),
        )

    @staticmethod
    def _timestamp_future_skew(now: datetime, value: Any) -> float:
        timestamp_utc = EAIInsightsEngine._timestamp_utc(value)
        now_utc = EAIInsightsEngine._timestamp_utc(now)
        if timestamp_utc is None or now_utc is None:
            return 0.0
        return max(
            0.0,
            (
                timestamp_utc.astimezone(timezone.utc)
                - now_utc.astimezone(timezone.utc)
            ).total_seconds(),
        )

    @staticmethod
    def _reading_state(reading: dict[str, Any]) -> Any | None:
        if not reading.get("available") or reading.get("stale"):
            return None
        return reading.get("state")

    def _capture_sample(
        self, now: datetime, readings: dict[str, dict[str, Any]]
    ) -> None:
        sample_bucket = self._sample_bucket(now)
        if sample_bucket == self._last_sample_minute:
            return
        self._last_sample_minute = sample_bucket
        flow_c = self._temperature_c(readings[CONF_FLOW_TEMP_ENTITY])
        return_c = self._temperature_c(readings[CONF_RETURN_TEMP_ENTITY])
        volume_flow_l_min = self._volume_l_min(readings[CONF_VOLUME_FLOW_ENTITY])
        compressor_on = self._compressor_on(readings)
        electrical_power_w = self._power_w(readings[CONF_WP_POWER_ENTITY])
        generator_flow_implausible = self._observe_generator_flow(readings, now)
        thermal_power_kw = (
            volume_flow_l_min * (flow_c - return_c) * 0.06977
            if (
                flow_c is not None
                and return_c is not None
                and (volume_flow_l_min is not None)
                and (volume_flow_l_min > 0)
                and (flow_c > return_c)
                and (compressor_on is not False)
                and (
                    electrical_power_w is None
                    or electrical_power_w > _MIN_COP_ELECTRICAL_POWER_W
                )
            )
            and (
                self._inputs_synchronous(
                    readings[CONF_FLOW_TEMP_ENTITY],
                    readings[CONF_RETURN_TEMP_ENTITY],
                    readings[CONF_VOLUME_FLOW_ENTITY],
                )
                and (not generator_flow_implausible)
            )
            else None
        )
        self._samples.append(
            {
                "timestamp": now,
                "power_w": electrical_power_w,
                "indoor_c": self._temperature_c(readings[CONF_INDOOR_TEMP_ENTITY]),
                "outdoor_c": self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY]),
                "target_c": self._temperature_c(readings[CONF_TARGET_TEMP_ENTITY]),
                "thermal_power_kw": thermal_power_kw,
                "compressor_on": self._compressor_on(readings),
                "heating_element_on": self._heating_element_on(readings),
                "operation_mode": self._normalized_mode(
                    self._reading_state(readings[CONF_OPERATION_MODE_ENTITY])
                ),
                "storage_c": self._temperature_c(readings[CONF_STORAGE_TEMP_ENTITY])
                or self._temperature_c(readings[CONF_DHW_TEMP_ENTITY]),
                "storage_ambient_c": self._temperature_c(
                    readings[CONF_STORAGE_AMBIENT_TEMP_ENTITY]
                ),
                "circulation_on": self._binary_on(
                    readings[CONF_CIRCULATION_PUMP_ENTITY]
                ),
                "circulation_return_c": self._temperature_c(
                    readings[CONF_CIRCULATION_RETURN_TEMP_ENTITY]
                ),
            }
        )

    @staticmethod
    def _sample_bucket(timestamp: datetime) -> str:
        return timestamp.replace(
            minute=timestamp.minute // 5 * 5, second=0, microsecond=0
        ).isoformat()

    @staticmethod
    def _binary_on(reading: dict[str, Any]) -> bool | None:
        if not reading.get("available") or reading.get("stale"):
            return None
        normalized_state = str(reading.get("state", "")).strip().lower()
        if normalized_state in {"1", "an", "on", "ein", "true", "active", "running"}:
            return True
        if normalized_state in {"0", "aus", "off", "idle", "false", "inactive"}:
            return False
        numeric_value = reading.get("numeric")
        return bool(numeric_value) if isinstance(numeric_value, (int, float)) else None

    @staticmethod
    def _number(reading: dict[str, Any]) -> float | None:
        if not reading.get("available"):
            return
        if reading.get("stale") and reading.get("freshness_kind") != "lifetime":
            return
        numeric_value = reading.get("numeric")
        return (
            float(numeric_value)
            if isinstance(numeric_value, (int, float)) and isfinite(numeric_value)
            else None
        )

    def _power_w(self, reading: dict[str, Any]) -> float | None:
        power_value = self._number(reading)
        if power_value is None:
            return
        return power_watts(power_value, reading.get("unit"))

    def _energy_kwh(self, reading: dict[str, Any]) -> float | None:
        energy_value = self._number(reading)
        if energy_value is None:
            return
        unit = str(reading.get("unit") or "").strip().lower()
        if unit in {"kwh", "kilowatt_hour", "kilowatt-hours"}:
            return energy_value
        if unit in {"wh", "watt_hour", "watt-hours"}:
            return energy_value / 1000
        if unit in {"mwh", "megawatt_hour", "megawatt-hours"}:
            return energy_value * 1000

    def _temperature_c(self, reading: dict[str, Any]) -> float | None:
        temperature_value = self._number(reading)
        if temperature_value is None:
            return
        unit = str(reading.get("unit") or "").strip().lower()
        if unit in {"c", "degc", "celsius", "°c"}:
            return temperature_value
        if unit in {"f", "degf", "fahrenheit", "°f"}:
            return (temperature_value - 32) * 5 / 9

    def _volume_l_min(self, reading: dict[str, Any]) -> float | None:
        volume_flow_value = self._number(reading)
        if volume_flow_value is None:
            return
        unit = str(reading.get("unit") or "").strip().lower().replace("³", "3")
        if unit in {"lpm", "l/m", "l/min", "liter/min", "litre/min"}:
            return volume_flow_value
        if unit in {"l/h", "l/hour", "liter/hour", "litre/hour"}:
            return volume_flow_value / 60
        if unit in {"m3/h", "m3/hour"}:
            return volume_flow_value * 1000 / 60
        if unit in {"m3/m", "m3/min"}:
            return volume_flow_value * 1000
        if unit in {"m3/s", "m3/sec"}:
            return volume_flow_value * 60000
        if unit in {"lps", "l/s", "liter/s", "litre/s"}:
            return volume_flow_value * 60

    @staticmethod
    def _inputs_synchronous(*readings: dict[str, Any]) -> bool:
        freshness_timestamps = [
            freshness_timestamp
            for reading in readings
            if isinstance(
                (freshness_timestamp := reading.get("freshness_at")), datetime
            )
        ]
        return (
            len(freshness_timestamps) == len(readings)
            and (max(freshness_timestamps) - min(freshness_timestamps)).total_seconds()
            <= _COP_MAX_INPUT_SKEW_SECONDS
        )

    @staticmethod
    def _input_unavailable_reason(reading: dict[str, Any], field: str) -> str:
        if reading.get("stale"):
            return f"{field}_stale"
        if not reading.get("available"):
            return f"{field}_{reading.get('reason', 'unavailable')}"
        if reading.get("numeric") is None:
            return f"{field}_not_numeric"
        return f"{field}_unsupported_unit"

    def _runtime_hours(self, reading: dict[str, Any]) -> float | None:
        runtime_value = self._number(reading)
        if runtime_value is None or runtime_value < 0:
            return None
        unit = str(reading.get("unit") or "").strip().lower()
        if unit in {"h", "hr", "hour", "hours"}:
            return runtime_value
        if unit in {"s", "sec", "seconds"}:
            return runtime_value / 3600
        if unit in {"min", "minutes"}:
            return runtime_value / 60
        if unit in {"d", "day", "days"}:
            return runtime_value * 24

    @staticmethod
    def _datetime_hour(value: Any, default_timezone: Any) -> datetime | None:
        if isinstance(value, datetime):
            timestamp = value
        elif isinstance(value, str):
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return
        else:
            return
        if timestamp.tzinfo is None:
            timezone_candidates = [
                timestamp.replace(tzinfo=default_timezone, fold=fold) for fold in (0, 1)
            ]
            valid_timestamps = [
                candidate_timestamp
                for candidate_timestamp in timezone_candidates
                if candidate_timestamp.astimezone(timezone.utc)
                .astimezone(default_timezone)
                .replace(tzinfo=None)
                == timestamp
            ]
            if not valid_timestamps:
                return
            if (
                len(valid_timestamps) == 2
                and valid_timestamps[0].utcoffset() != valid_timestamps[1].utcoffset()
            ):
                return
            timestamp = valid_timestamps[0]
        return timestamp.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )

    def _forecast_point_hour(self, point: dict[str, Any]) -> datetime | None:
        for timestamp_key in ("target_datetime", "valid_at", "timestamp", "datetime"):
            timestamp = self._datetime_hour(
                point.get(timestamp_key), self._local_timezone()
            )
            if not timestamp is not None:
                continue
            if True:
                return timestamp
        target_date = point.get("target_date", point.get("date"))
        target_hour = point.get("target_hour", point.get("hour"))
        if target_date is None or target_hour is None or isinstance(target_hour, bool):
            if True:
                return
        try:
            hour_number = int(target_hour)
            if not 0 <= hour_number <= 23:
                if True:
                    return
            if True:
                return self._datetime_hour(
                    f"{target_date}T{hour_number:02d}:00:00", self._local_timezone()
                )
        except (OverflowError, TypeError, ValueError):
            if True:
                return

    def _weather_temperature_by_hour(self) -> dict[datetime, float]:
        temperature_by_hour = self._coordinator_weather_temperature_by_hour()
        temperature_sources = {
            forecast_hour: "eai_internal_weather"
            for forecast_hour in temperature_by_hour
        }
        self._weather_temperature_sources = temperature_sources
        weather_snapshot = getattr(self._runtime, "weather_intelligence_snapshot", None)
        if (
            not isinstance(weather_snapshot, dict)
            or weather_snapshot.get("available") is not True
        ):
            return temperature_by_hour
        data_quality = weather_snapshot.get("data_quality")
        if not isinstance(data_quality, dict):
            return temperature_by_hour
        if data_quality.get("forecast_available") is not True:
            return temperature_by_hour
        stale = data_quality.get("forecast_stale") is True
        weather_outlook = weather_snapshot.get("outlook")
        hourly_weather = (
            weather_outlook.get("hourly") if isinstance(weather_outlook, dict) else None
        )
        if not isinstance(hourly_weather, list):
            return temperature_by_hour
        for forecast_point in hourly_weather:
            if isinstance(forecast_point, dict):
                forecast_hour = self._forecast_point_hour(forecast_point)
                temperature_c = self._forecast_temperature(forecast_point)
                if forecast_hour is None or temperature_c is None:
                    continue
                if stale:
                    if forecast_hour in temperature_by_hour:
                        continue
                temperature_by_hour[forecast_hour] = temperature_c
                temperature_sources[forecast_hour] = (
                    "weather_intelligence_stale" if stale else "weather_intelligence"
                )
        self._weather_temperature_sources = temperature_sources
        return temperature_by_hour

    def _coordinator_weather_temperature_by_hour(self) -> dict[datetime, float]:
        coordinator = getattr(self._runtime, "coordinator", None)
        weather_cache = getattr(coordinator, "_eai_hourly_weather_cache", None)
        if not isinstance(weather_cache, dict):
            return {}
        hourly_weather = weather_cache.get("hourly")
        if not isinstance(hourly_weather, list):
            return {}
        generated_at = self._forecast_issued_at(weather_cache.get("generated_at"))
        now = datetime.now(timezone.utc)
        temperature_by_hour = {}
        for forecast_point in hourly_weather:
            if isinstance(forecast_point, dict):
                forecast_hour = self._forecast_point_hour(forecast_point)
                temperature_c = self._forecast_temperature(forecast_point)
                if forecast_hour is None or temperature_c is None:
                    continue
                issued_at = (
                    self._forecast_issued_at(forecast_point.get("_issued_at"))
                    or generated_at
                )
                if issued_at is None or not self._forecast_issue_is_usable(
                    now, issued_at
                ):
                    continue
                temperature_by_hour[forecast_hour] = temperature_c
        return temperature_by_hour

    def _forecast_issued_at(self, value: Any) -> datetime | None:
        """Return a forecast issue time, accepting a text timestamp as well."""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return
        return self._timestamp_utc(value)

    @staticmethod
    def _forecast_issue_is_usable(now: datetime, issued_at: datetime) -> bool:
        """Return whether one forecast hour was issued recently enough."""
        issue_age = now.astimezone(timezone.utc) - issued_at.astimezone(timezone.utc)
        return timedelta(minutes=-5) <= issue_age <= _WEATHER_FORECAST_MAX_AGE

    def _compressor_on(self, readings: dict[str, dict[str, Any]]) -> bool | None:
        return self._compressor_evaluation(readings)["value"]

    def _heat_pump_idle(
        self,
        readings: dict[str, dict[str, Any]],
        electrical_power_w: float | None,
        *,
        volume: float | None = None,
        flow: float | None = None,
        return_temp: float | None = None,
    ) -> bool:
        if self._compressor_on(readings) is False:
            return True
        if (
            isinstance(electrical_power_w, (int, float))
            and electrical_power_w <= _MIN_COP_ELECTRICAL_POWER_W
        ):
            return True
        return (
            isinstance(volume, (int, float))
            and volume <= 0
            and isinstance(flow, (int, float))
            and isinstance(return_temp, (int, float))
            and (flow <= return_temp)
        )

    def _last_valid_operating_metrics(self, now: datetime) -> dict[str, Any] | None:
        for sample in reversed(self._samples):
            thermal_power_kw = sample.get("thermal_power_kw")
            sample_timestamp = sample.get("timestamp")
            electrical_power_w = sample.get("power_w")
            if not isinstance(thermal_power_kw, (int, float)) or thermal_power_kw <= 0:
                continue
            if (
                isinstance(electrical_power_w, (int, float))
                and electrical_power_w <= _MIN_COP_ELECTRICAL_POWER_W
            ):
                continue
            if not isinstance(sample_timestamp, datetime):
                continue
            sample_time_utc = (
                sample_timestamp
                if sample_timestamp.tzinfo is not None
                else sample_timestamp.replace(tzinfo=timezone.utc)
            )
            if now - sample_time_utc > _LAST_VALID_MAX_AGE:
                continue
            cop = None
            if (
                isinstance(electrical_power_w, (int, float))
                and electrical_power_w > _MIN_COP_ELECTRICAL_POWER_W
            ):
                calculated_cop = thermal_power_kw / (electrical_power_w / 1000.0)
                if 0.5 <= calculated_cop <= 12:
                    cop = calculated_cop
            return {
                "thermal_power_kw": thermal_power_kw,
                "cop": cop,
                "timestamp": sample_timestamp,
            }

    def _compressor_evaluation(
        self, readings: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        reading = readings.get(CONF_COMPRESSOR_ENTITY, {})
        binary_status = self._binary_on(reading)
        normalized_activity = self._normalized_mode(self._reading_state(reading))
        compressor_on = binary_status
        signal_origin = "configured_binary_status"
        if compressor_on is None and normalized_activity in {
            "dhw",
            "cooling",
            "defrost",
            "heating",
        }:
            compressor_on = True
            signal_origin = "configured_activity_status"
        elif compressor_on is None and normalized_activity == "standby":
            compressor_on = False
            signal_origin = "configured_activity_status"
        operation_mode = self._normalized_mode(
            self._reading_state(readings.get(CONF_OPERATION_MODE_ENTITY, {}))
        )
        mode_indicates_active = (
            True
            if operation_mode in {"dhw", "cooling", "defrost", "heating"}
            else False if operation_mode == "standby" else None
        )
        electrical_power_w = self._power_w(readings[CONF_WP_POWER_ENTITY])
        power_indicates_active = (
            electrical_power_w >= 150 if electrical_power_w is not None else None
        )
        conflict_sources = []
        if (
            compressor_on is not None
            and mode_indicates_active is not None
            and (compressor_on != mode_indicates_active)
        ):
            conflict_sources.append("operation_mode")
        if (
            compressor_on is not None
            and power_indicates_active is not None
            and (compressor_on != power_indicates_active)
        ):
            conflict_sources.append("electrical_power")
        if compressor_on is not None:
            return {
                "value": compressor_on,
                "origin": signal_origin,
                "normalized_activity": normalized_activity,
                "conflict": bool(conflict_sources),
                "conflict_sources": conflict_sources,
            }
        electrical_topology = self._config.get(
            CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
            DEFAULT_ELECTRICAL_MEASUREMENT_TOPOLOGY,
        )
        heating_element_on = self._heating_element_on(readings)
        if electrical_topology == ELECTRICAL_TOPOLOGY_LEGACY_AGGREGATE and is_true(
            self._config, CONF_HAS_HEATING_ELEMENT
        ):
            if operation_mode == "standby":
                return {
                    "value": False,
                    "origin": "operation_mode",
                    "normalized_activity": normalized_activity,
                    "conflict": False,
                    "conflict_sources": [],
                }
            if heating_element_on is not False:
                return {
                    "value": None,
                    "origin": "insufficient_evidence",
                    "normalized_activity": normalized_activity,
                    "conflict": False,
                    "conflict_sources": [],
                }
        if mode_indicates_active is not None:
            return {
                "value": mode_indicates_active,
                "origin": "operation_mode",
                "normalized_activity": normalized_activity,
                "conflict": False,
                "conflict_sources": [],
            }
        return {
            "value": power_indicates_active,
            "origin": (
                "electrical_power" if power_indicates_active is not None else "unknown"
            ),
            "normalized_activity": normalized_activity,
            "conflict": False,
            "conflict_sources": [],
        }

    def _heating_element_on(self, readings: dict[str, dict[str, Any]]) -> bool | None:
        if not is_true(self._config, CONF_HAS_HEATING_ELEMENT):
            return
        binary_status = self._binary_on(readings.get(CONF_HEATING_ELEMENT_ENTITY, {}))
        if binary_status is not None:
            return binary_status
        heating_element_power_w = self._power_w(
            readings.get(CONF_HEATING_ELEMENT_POWER_ENTITY, {})
        )
        return (
            heating_element_power_w >= 50
            if heating_element_power_w is not None
            else None
        )

    @staticmethod
    def _normalized_mode(value: Any) -> str | None:
        """Heating and DHW come from ``normalize_operation_mode``.

        Defrost and standby stay local: the COP cascade does not use them.
        """
        normalized_text = str(value or "").strip().lower()
        if not normalized_text:
            return
        operation_mode = normalize_operation_mode(value)
        if operation_mode in {"dhw", "heating"}:
            return operation_mode
        if any((mode_token in normalized_text for mode_token in ("defrost", "abtau"))):
            return "defrost"
        if any(
            (
                mode_token in normalized_text
                for mode_token in ("off", "idle", "standby", "aus", "bereit")
            )
        ):
            return "standby"

    @staticmethod
    def _bounded_predictions(cache: dict[str, Any]) -> list[dict[str, Any]]:
        """Read at most 72 cache slots without copying unbounded input lists."""
        predictions = []
        prediction_limit = 72
        for cache_key in (
            "predictions",
            "predictions_tomorrow",
            "predictions_day_after",
        ):
            cached_predictions = cache.get(cache_key)
            if not isinstance(cached_predictions, list) or prediction_limit <= 0:
                continue
            bounded_predictions = cached_predictions[:prediction_limit]
            prediction_limit -= len(bounded_predictions)
            predictions.extend(
                (
                    prediction
                    for prediction in bounded_predictions
                    if isinstance(prediction, dict)
                )
            )
        return predictions

    def _sfml_predictions(self) -> list[dict[str, Any]]:
        sfml_data = self.hass.data.get("solar_forecast_ml", {})
        for coordinator in sfml_data.values():
            predictions_cache = getattr(coordinator, "_hourly_predictions_cache", None)
            if not isinstance(predictions_cache, dict):
                continue
            predictions = self._bounded_predictions(predictions_cache)
            if not predictions:
                continue
            return predictions
        return []

    def _coordinator_predictions(self) -> list[dict[str, Any]]:
        coordinator = getattr(self._runtime, "coordinator", None)
        predictions_cache = getattr(coordinator, "_hourly_predictions_cache", None)
        if not isinstance(predictions_cache, dict):
            return []
        return self._bounded_predictions(predictions_cache)

    @staticmethod
    def _point_key(point: dict[str, Any]) -> tuple[str | None, int | None]:
        target_date = point.get("target_date")
        target_hour = point.get("target_hour")
        try:
            if isinstance(target_hour, bool):
                raise ValueError
            hour_number = int(target_hour) if target_hour is not None else None
        except (OverflowError, TypeError, ValueError):
            hour_number = None
        if hour_number is not None:
            if not 0 <= hour_number <= 23:
                hour_number = None
        return (
            ((str(target_date) if target_date else None, hour_number) if True else None)
            if True
            else None
        )

    @staticmethod
    def _forecast_temperature(point: dict[str, Any]) -> float | None:
        temperature_candidates = (
            point.get("temperature_c"),
            point.get("temperature"),
            point.get("weather_temperature"),
            (
                (point.get("weather") or {}).get("temperature_c")
                if isinstance(point.get("weather"), dict)
                else None
            ),
        )
        for temperature_c in temperature_candidates:
            if (
                not isinstance(temperature_c, bool)
                and isinstance(temperature_c, (int, float))
                and isfinite(temperature_c)
            ):
                if -100 <= float(temperature_c) <= 100:
                    return float(temperature_c)

    @staticmethod
    def _source_confidence(point: dict[str, Any]) -> float | None:
        for confidence_key in ("prediction_confidence", "ai_confidence", "confidence"):
            if confidence_key not in point:
                continue
            raw_confidence = point.get(confidence_key)
            if raw_confidence is None or isinstance(raw_confidence, str):
                continue
            if (
                not (
                    not isinstance(raw_confidence, bool)
                    and isinstance(raw_confidence, (int, float))
                    and isfinite(raw_confidence)
                )
                or not 0 <= float(raw_confidence) <= 100
            ):
                if True:
                    return 0.0
            normalized_confidence = (
                float(raw_confidence) / 100
                if raw_confidence > 1
                else float(raw_confidence)
            )
            return min(1.0, max(0.0, normalized_confidence))

    @classmethod
    def _prediction_interval(
        cls,
        point: dict[str, Any],
        load_kw: float,
        *,
        coordinator_source: bool,
        temperature_origin: str,
        horizon_hours: int,
    ) -> tuple[float, float, int, int, list[str], str]:
        source_confidence = (
            cls._source_confidence(point) if coordinator_source else None
        )
        confidence = (
            source_confidence
            if source_confidence is not None
            else 0.88 if coordinator_source else 0.78
        )
        uncertainty_drivers = [
            (
                "gelernte Modellprognose"
                if coordinator_source
                else "erklärbares Wärmebedarfsmodell"
            )
        ]
        if temperature_origin != "forecast":
            confidence -= 0.18
            uncertainty_drivers.append("aktuelle Außentemperatur statt Stundenprognose")
        horizon_penalty = min(0.16, max(0, horizon_hours) * 0.003)
        confidence -= horizon_penalty
        if horizon_penalty >= 0.04:
            uncertainty_drivers.append("größerer Prognosehorizont")
        confidence = min(0.95, max(0.4, confidence))
        source_lower_kw = next(
            (
                float(point[bound_key])
                for bound_key in ("lower_kw", "prediction_lower_kwh", "lower_kwh")
                if not isinstance(point.get(bound_key), bool)
                and isinstance(point.get(bound_key), (int, float))
                and isfinite(point[bound_key])
                if 0 <= float(point[bound_key]) <= 1000
            ),
            None,
        )
        source_upper_kw = next(
            (
                float(point[bound_key])
                for bound_key in ("upper_kw", "prediction_upper_kwh", "upper_kwh")
                if not isinstance(point.get(bound_key), bool)
                and isinstance(point.get(bound_key), (int, float))
                and isfinite(point[bound_key])
                if 0 <= float(point[bound_key]) <= 1000
            ),
            None,
        )
        if (
            source_lower_kw is not None and source_upper_kw is not None
        ) and 0 <= source_lower_kw <= load_kw <= source_upper_kw:
            lower_kw = source_lower_kw
            upper_kw = source_upper_kw
            uncertainty_fraction = (
                (upper_kw - lower_kw) / (2 * load_kw) if load_kw > 0 else 0.0
            )
            interval_method = "source_prediction_interval"
            uncertainty_drivers.append("vom Prognosemodell geliefertes Intervall")
        else:
            uncertainty_fraction = min(
                0.45, max(0.08, 0.08 + (1 - confidence) * 0.48 + horizon_penalty * 0.35)
            )
            lower_kw = max(0.0, load_kw * (1 - uncertainty_fraction))
            upper_kw = load_kw * (1 + uncertainty_fraction)
            interval_method = "quality_adjusted_prediction_interval"
        return (
            lower_kw,
            upper_kw,
            round(uncertainty_fraction * 100),
            round(confidence * 100),
            uncertainty_drivers,
            interval_method,
        )

    def _forecast(
        self, readings: dict[str, dict[str, Any]], now: datetime
    ) -> dict[str, Any]:
        current_outdoor_c = self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY])
        target_c = self._temperature_c(readings[CONF_TARGET_TEMP_ENTITY]) or 21.0
        has_dhw = is_true(self._config, CONF_HAS_DHW)
        coordinator_predictions = [
            forecast_point
            for forecast_point in self._coordinator_predictions()
            if self._forecast_point_hour(forecast_point) is not None
        ]
        sfml_predictions = [
            forecast_point
            for forecast_point in self._sfml_predictions()
            if self._forecast_point_hour(forecast_point) is not None
        ]
        heat_pump_enabled = self._config.get(CONF_HEAT_PUMP_ENABLED, True) is True
        use_coordinator_predictions = heat_pump_enabled and bool(
            coordinator_predictions
        )
        source_predictions = (
            coordinator_predictions if use_coordinator_predictions else sfml_predictions
        )
        model_origin = (
            "persistent_eai_coordinator"
            if use_coordinator_predictions
            else (
                "sfml_energy_context_only"
                if not heat_pump_enabled
                else "explainable_heat_demand_model"
            )
        )
        weather_temperature_by_hour = self._weather_temperature_by_hour()
        weather_temperature_sources = self._weather_temperature_sources
        calibrator = (
            self._forecast_calibrator()
            if heat_pump_enabled and (not use_coordinator_predictions)
            else None
        )
        learned_u_factor = self._learned_u_factor(calibrator)
        blocked_heating_days = (
            self._blocked_space_heating_days(
                source_predictions, weather_temperature_by_hour, calibrator=calibrator
            )
            if heat_pump_enabled and (not use_coordinator_predictions)
            else set()
        )
        pv_predictions_by_key = {
            self._point_key(forecast_point): forecast_point.get("prediction_kwh")
            for forecast_point in sfml_predictions
        }
        from .thermodynamics.thermodynamics_engine import (
            local_calendar_date as local_calendar_date,
        )

        forecast_hours = []
        for horizon_index, forecast_point in enumerate(source_predictions):
            forecast_hour = self._forecast_point_hour(forecast_point)
            if forecast_hour is None:
                continue
            target_datetime = (
                forecast_point.get("target_datetime") or forecast_hour.isoformat()
            )
            outdoor_c = self._forecast_temperature(forecast_point)
            temperature_source = (
                "load_forecast" if outdoor_c is not None else "forecast_temperature"
            )
            if outdoor_c is None:
                outdoor_c = weather_temperature_by_hour.get(forecast_hour)
                if outdoor_c is not None:
                    temperature_source = weather_temperature_sources.get(
                        forecast_hour, "forecast_temperature"
                    )
            temperature_origin = (
                "forecast" if outdoor_c is not None else "current_fallback"
            )
            if outdoor_c is None:
                temperature_source = "current_sensor_fallback"
            outdoor_c = current_outdoor_c if outdoor_c is None else outdoor_c
            if outdoor_c is None:
                if heat_pump_enabled:
                    continue
            local_hour = forecast_hour.astimezone(self._local_timezone()).hour
            if use_coordinator_predictions:
                raw_prediction_kwh = forecast_point.get(
                    "prediction_kwh", forecast_point.get("predicted_kwh")
                )
                if (
                    isinstance(raw_prediction_kwh, bool)
                    or not isinstance(raw_prediction_kwh, (int, float))
                    or (not isfinite(raw_prediction_kwh))
                    or (not 0 <= float(raw_prediction_kwh) <= 1000)
                ):
                    continue
                forecast_load_kw = max(0.0, float(raw_prediction_kwh))
                pv_forecast_kwh = pv_predictions_by_key.get(
                    self._point_key(forecast_point)
                )
            elif heat_pump_enabled:
                modeled_consumption = self._modeled_hourly_consumption(
                    readings,
                    outdoor_c,
                    local_hour,
                    target_c,
                    suppress_space_heating=local_calendar_date(
                        forecast_hour, self._local_timezone()
                    )
                    in blocked_heating_days,
                    learned_u_factor=learned_u_factor,
                    now=now,
                )
                if modeled_consumption is None:
                    continue
                forecast_load_kw = max(
                    0.0, modeled_consumption.electrical_consumption_kw
                )
                if calibrator is not None:
                    space_heating_suppressed = (
                        local_calendar_date(forecast_hour, self._local_timezone())
                        in blocked_heating_days
                    )
                    dhw_only = has_dhw and (
                        space_heating_suppressed
                        or modeled_consumption.heating_demand_kw <= 0.01
                    )
                    try:
                        calibration_factor = calibrator.get_correction_factor(
                            float(outdoor_c), 0.0, local_hour, is_dhw_only=dhw_only
                        )
                        if (
                            calibration_factor is not None
                            and float(calibration_factor.confidence) > 0.3
                        ):
                            forecast_load_kw *= float(
                                calibration_factor.correction_factor
                            )
                    except Exception:
                        pass
                    forecast_load_kw = max(0.0, forecast_load_kw)
                dhw_load_kw = (
                    modeled_consumption.electrical_consumption_kw
                    if modeled_consumption.heating_demand_kw <= 0.01
                    and modeled_consumption.is_heating_needed
                    else 0.0
                )
                pv_forecast_kwh = forecast_point.get("prediction_kwh")
            else:
                temperature_origin = "not_required"
                temperature_source = "not_applicable"
                dhw_load_kw = 0.0
                forecast_load_kw = 0.0
                pv_forecast_kwh = forecast_point.get("prediction_kwh")
            (
                lower_kw,
                upper_kw,
                uncertainty_percent,
                confidence_percent,
                uncertainty_drivers,
                interval_method,
            ) = self._prediction_interval(
                forecast_point,
                forecast_load_kw,
                coordinator_source=use_coordinator_predictions,
                temperature_origin=temperature_origin,
                horizon_hours=horizon_index,
            )
            forecast_hours.append(
                {
                    "timestamp": target_datetime or now.isoformat(),
                    "outdoor_c": _round(outdoor_c, 1),
                    "forecast_kw": _round(forecast_load_kw),
                    "lower_kw": _round(lower_kw),
                    "upper_kw": _round(upper_kw),
                    "uncertainty_percent": uncertainty_percent,
                    "confidence_percent": confidence_percent,
                    "uncertainty_drivers": uncertainty_drivers,
                    "interval_method": interval_method,
                    "actual_kw": _round(
                        self._completed_actual_kw(forecast_point, forecast_hour, now)
                    ),
                    "pv_forecast_kwh": (
                        _round(float(pv_forecast_kwh))
                        if not isinstance(pv_forecast_kwh, bool)
                        and isinstance(pv_forecast_kwh, (int, float))
                        and isfinite(float(pv_forecast_kwh))
                        and (0 <= float(pv_forecast_kwh) <= 1000)
                        else None
                    ),
                    "temperature_origin": temperature_origin,
                    "temperature_source": temperature_source,
                    "mode": (
                        forecast_point.get("mode", "heating")
                        if use_coordinator_predictions
                        else (
                            "standby"
                            if not heat_pump_enabled
                            else "dhw" if dhw_load_kw else "heating"
                        )
                    ),
                }
            )
            continue
        forecast_hours = self._energy_allocator.enrich(
            forecast_hours, self._wallbox_envelope()
        )
        temperature_forecast_hours = (
            sum(
                (
                    forecast_point["temperature_origin"] == "forecast"
                    for forecast_point in forecast_hours
                )
            )
            if heat_pump_enabled
            else 0
        )
        temperature_fallback_hours = (
            len(forecast_hours) - temperature_forecast_hours if heat_pump_enabled else 0
        )
        temperature_stale_forecast_hours = (
            sum(
                (
                    forecast_point["temperature_source"] == "weather_intelligence_stale"
                    for forecast_point in forecast_hours
                )
            )
            if heat_pump_enabled
            else 0
        )
        forecast_status = (
            "ready"
            if forecast_hours
            and (not heat_pump_enabled or temperature_fallback_hours == 0)
            else (
                "partial"
                if temperature_forecast_hours
                else "estimated" if forecast_hours else "unavailable"
            )
        )
        average_uncertainty_percent = (
            round(
                sum(
                    (
                        forecast_point["uncertainty_percent"]
                        for forecast_point in forecast_hours
                    )
                )
                / len(forecast_hours)
            )
            if forecast_hours
            else None
        )
        average_confidence_percent = (
            round(
                sum(
                    (
                        forecast_point["confidence_percent"]
                        for forecast_point in forecast_hours
                    )
                )
                / len(forecast_hours)
            )
            if forecast_hours
            else None
        )
        return {
            "available": bool(forecast_hours),
            "locked": False,
            "hours": forecast_hours,
            "run_status": forecast_status,
            "quality": forecast_status,
            "temperature_forecast_hours": temperature_forecast_hours,
            "temperature_fallback_hours": temperature_fallback_hours,
            "temperature_stale_forecast_hours": temperature_stale_forecast_hours,
            "temperature_forecast_coverage_percent": (
                _round(temperature_forecast_hours / len(forecast_hours) * 100, 0)
                if forecast_hours and heat_pump_enabled
                else None
            ),
            "model_origin": model_origin,
            "main_drivers": (
                ["SFML-Solarprognose", "STATS-Haushaltsprofil"]
                if not heat_pump_enabled
                else [
                    "Außentemperatur",
                    "Nenn-Heizleistung",
                    "Nenn-COP",
                    "Tagesprofil",
                    "Warmwasserzyklus" if has_dhw else "Raumheizung",
                ]
            ),
            "uncertainty": {
                "available": bool(forecast_hours),
                "average_percent": average_uncertainty_percent,
                "confidence_percent": average_confidence_percent,
                "method": "data_quality_model_source_temperature_and_horizon",
                "explanation": "Das Band wird mit Modellherkunft, Datenqualität, Temperaturquelle und Prognosehorizont breiter oder schmaler.",
                "lower_field": "lower_kw",
                "upper_field": "upper_kw",
            },
            "optimization": self._optimization(forecast_hours),
        }

    @staticmethod
    def _optimization(hours: list[dict[str, Any]]) -> dict[str, Any]:
        pv_window_candidates = []
        for forecast_point in hours:
            pv_forecast_kwh = forecast_point.get("pv_forecast_kwh")
            heat_pump_kwh = forecast_point.get("heat_pump_kwh")
            pv_after_household_kwh = forecast_point.get("pv_after_household_kwh")
            if (
                forecast_point.get("allocation_valid") is True
                and isinstance(pv_forecast_kwh, (int, float))
                and isinstance(heat_pump_kwh, (int, float))
                and isinstance(pv_after_household_kwh, (int, float))
            ):
                pv_window_candidates.append(
                    (
                        float(pv_after_household_kwh) - float(heat_pump_kwh),
                        forecast_point,
                    )
                )
        if not pv_window_candidates:
            return {
                "available": False,
                "reason": "pv_forecast_missing",
                "explanation": {
                    "headline": "Noch keine belastbare Empfehlung",
                    "summary": "Für eine begründete Empfehlung fehlt eine nutzbare PV- oder Lastprognose.",
                    "evidence": [],
                    "confidence_percent": 0,
                    "uncertainty_percent": 100,
                },
            }
        pv_surplus_kwh, best_window = max(
            pv_window_candidates, key=lambda item: item[0]
        )
        confidence_percent = int(best_window.get("confidence_percent") or 0)
        uncertainty_percent = int(best_window.get("uncertainty_percent") or 100)
        interval_hours = float(best_window.get("interval_hours") or 1)
        forecast_load_kw = float(best_window.get("forecast_kw") or 0)
        lower_kw = float(best_window.get("lower_kw") or forecast_load_kw)
        upper_kw = float(best_window.get("upper_kw") or forecast_load_kw)
        pv_forecast_kwh = float(best_window.get("pv_forecast_kwh") or 0)
        evidence = [
            f"PV-Prognose {pv_forecast_kwh:.2f} kWh",
            f"Wärmepumpenbedarf {forecast_load_kw * interval_hours:.2f} kWh, Band {lower_kw * interval_hours:.2f}–{upper_kw * interval_hours:.2f} kWh",
        ]
        household_base_load_kwh = best_window.get("household_base_load_kwh")
        if isinstance(household_base_load_kwh, (int, float)):
            evidence.append(
                f"Hausgrundlast {float(household_base_load_kwh):.2f} kWh bereits berücksichtigt"
            )
        battery_pv_reserve_kwh = best_window.get("battery_pv_reserved_kwh")
        if (
            isinstance(battery_pv_reserve_kwh, (int, float))
            and battery_pv_reserve_kwh > 0
        ):
            evidence.append(
                f"Speicherreserve {float(battery_pv_reserve_kwh):.2f} kWh bereits berücksichtigt"
            )
        confidence_sufficient = confidence_percent >= 60
        recommendation = (
            "Warmwasser oder thermischen Speicher bevorzugt in dieses PV-Fenster legen"
            if pv_surplus_kwh > 0 and confidence_sufficient
            else (
                "Mögliches PV-Fenster weiter beobachten; die Datensicherheit reicht noch nicht für eine konkrete Empfehlung"
                if pv_surplus_kwh > 0
                else "Kein belastbares PV-Überschussfenster erkannt"
            )
        )
        return {
            "available": pv_surplus_kwh > 0 and confidence_sufficient,
            "start": best_window.get("interval_start", best_window["timestamp"]),
            "duration_minutes": round(interval_hours * 60),
            "pv_surplus_kwh": _round(max(0.0, pv_surplus_kwh)),
            "recommendation": recommendation,
            "confidence_percent": confidence_percent,
            "uncertainty_percent": uncertainty_percent,
            "explanation": {
                "headline": (
                    "PV-Fenster mit Restenergie erkannt"
                    if pv_surplus_kwh > 0 and confidence_sufficient
                    else "Kein belastbares PV-Fenster"
                ),
                "summary": recommendation,
                "evidence": evidence,
                "confidence_percent": confidence_percent,
                "uncertainty_percent": uncertainty_percent,
            },
            "origin": "sfml_pv_forecast_and_eai_load_model",
        }

    def _operation(self, readings: dict[str, dict[str, Any]]) -> dict[str, Any]:
        electrical_power_w = self._power_w(readings[CONF_WP_POWER_ENTITY])
        compressor_power_w = (
            electrical_power_w if self._is_separate_topology() else None
        )
        heating_element_power_w = self._power_w(
            readings[CONF_HEATING_ELEMENT_POWER_ENTITY]
        )
        total_electrical_power_w = (
            (compressor_power_w or 0.0) + (heating_element_power_w or 0.0)
            if self._is_separate_topology()
            and (compressor_power_w is not None or heating_element_power_w is not None)
            else electrical_power_w
        )
        compressor_evaluation = self._compressor_evaluation(readings)
        compressor_on = compressor_evaluation["value"]
        heating_element_on = self._heating_element_on(readings)
        operation_mode = self._normalized_mode(
            self._reading_state(readings[CONF_OPERATION_MODE_ENTITY])
        )
        if not operation_mode:
            operation_mode = (
                "heating"
                if compressor_on
                else "standby" if compressor_on is False else "unknown"
            )
        runtime_hours = self._runtime_hours(readings[CONF_RUNTIME_ENTITY])
        runtime_heating_hours = self._runtime_hours(
            readings[CONF_RUNTIME_HEATING_ENTITY]
        )
        runtime_dhw_hours = self._runtime_hours(readings[CONF_RUNTIME_DHW_ENTITY])
        runtime_origin = "total_counter" if runtime_hours is not None else None
        if (
            runtime_hours is None
            and runtime_heating_hours is not None
            and (runtime_dhw_hours is not None)
        ):
            runtime_hours = runtime_heating_hours + runtime_dhw_hours
            runtime_origin = "heating_plus_dhw_counters"
        starts = self._number(readings[CONF_STARTS_ENTITY])
        starts_invalid = bool(
            starts is not None and (starts < 0 or not starts.is_integer())
        )
        if starts_invalid:
            starts = None
        starts_scope = self._config.get(
            CONF_STARTS_COUNTER_SCOPE, COUNTER_SCOPE_UNKNOWN
        )
        runtime_scope = self._config.get(
            CONF_RUNTIME_COUNTER_SCOPE, COUNTER_SCOPE_UNKNOWN
        )
        valid_counter_scopes = {
            COUNTER_SCOPE_UNKNOWN,
            COUNTER_SCOPE_LIFETIME,
            COUNTER_SCOPE_DAILY,
        }
        if starts_scope not in valid_counter_scopes:
            starts_scope = COUNTER_SCOPE_UNKNOWN
        if runtime_scope not in valid_counter_scopes:
            runtime_scope = COUNTER_SCOPE_UNKNOWN
        runtime_implausible = bool(
            runtime_hours is not None
            and runtime_scope == COUNTER_SCOPE_DAILY
            and (runtime_hours > _MAX_PLAUSIBLE_DAILY_RUNTIME_HOURS)
        )
        heating_runtime_implausible = bool(
            runtime_heating_hours is not None
            and runtime_scope == COUNTER_SCOPE_DAILY
            and (runtime_heating_hours > _MAX_PLAUSIBLE_DAILY_RUNTIME_HOURS)
        )
        dhw_runtime_implausible = bool(
            runtime_dhw_hours is not None
            and runtime_scope == COUNTER_SCOPE_DAILY
            and (runtime_dhw_hours > _MAX_PLAUSIBLE_DAILY_RUNTIME_HOURS)
        )
        if runtime_implausible:
            runtime_hours = None
            runtime_origin = None
        if heating_runtime_implausible:
            runtime_heating_hours = None
        if dhw_runtime_implausible:
            runtime_dhw_hours = None
        raw_average_cycle_minutes = (
            runtime_hours * 60 / starts
            if runtime_hours is not None and starts is not None and (starts > 0)
            else None
        )
        average_cycle_minutes = raw_average_cycle_minutes
        average_cycle_reason = None
        if average_cycle_minutes is not None and COUNTER_SCOPE_UNKNOWN in {
            runtime_scope,
            starts_scope,
        }:
            average_cycle_minutes = None
            average_cycle_reason = "counter_scope_unverified"
        elif average_cycle_minutes is not None and runtime_scope != starts_scope:
            average_cycle_minutes = None
            average_cycle_reason = "counter_scope_mismatch"
        elif (
            average_cycle_minutes is not None
            and average_cycle_minutes > _MAX_PLAUSIBLE_AVERAGE_CYCLE_MINUTES
        ):
            average_cycle_minutes = None
            average_cycle_reason = "average_cycle_implausible"
        elif average_cycle_minutes is None:
            average_cycle_reason = (
                "runtime_scope_implausible"
                if runtime_implausible
                else (
                    "runtime_unavailable"
                    if runtime_hours is None
                    else (
                        "starts_invalid"
                        if starts_invalid
                        else "starts_unavailable" if starts is None else "starts_zero"
                    )
                )
            )
        why_now = self._why_now(
            readings, electrical_power_w, compressor_on, str(operation_mode)
        )
        return {
            "available": total_electrical_power_w is not None,
            "current_power_kw": _round(
                total_electrical_power_w / 1000
                if total_electrical_power_w is not None
                else None
            ),
            "compressor_power_kw": _round(
                compressor_power_w / 1000 if compressor_power_w is not None else None
            ),
            "heating_element_power_kw": _round(
                heating_element_power_w / 1000
                if heating_element_power_w is not None
                else None
            ),
            "current_mode": operation_mode,
            "compressor_on": compressor_on,
            "compressor_signal_origin": compressor_evaluation["origin"],
            "compressor_signal_activity": compressor_evaluation["normalized_activity"],
            "compressor_signal_conflict": compressor_evaluation["conflict"],
            "compressor_signal_conflict_sources": compressor_evaluation[
                "conflict_sources"
            ],
            "heating_element_on": heating_element_on,
            "runtime_hours": _round(runtime_hours, 1),
            "runtime_hours_reason": (
                "runtime_scope_implausible"
                if runtime_implausible
                else "runtime_unavailable" if runtime_hours is None else None
            ),
            "runtime_heating_hours": _round(runtime_heating_hours, 1),
            "runtime_heating_hours_reason": (
                "runtime_scope_implausible"
                if heating_runtime_implausible
                else "runtime_unavailable" if runtime_heating_hours is None else None
            ),
            "runtime_dhw_hours": _round(runtime_dhw_hours, 1),
            "runtime_dhw_hours_reason": (
                "runtime_scope_implausible"
                if dhw_runtime_implausible
                else "runtime_unavailable" if runtime_dhw_hours is None else None
            ),
            "runtime_origin": runtime_origin,
            "runtime_scope": runtime_scope,
            "starts": int(starts) if starts is not None else None,
            "starts_scope": starts_scope,
            "average_cycle_minutes": _round(average_cycle_minutes, 0),
            "average_cycle_origin": (
                f"{runtime_scope}_runtime_hours_divided_by_{starts_scope}_starts"
                if average_cycle_minutes is not None
                else "not_available"
            ),
            "average_cycle_reason": average_cycle_reason,
            "average_cycle_minutes_reason": average_cycle_reason,
            "sensor_coverage_percent": self._coverage(readings),
            "why_now": why_now,
            "modes_minutes": self._observed_modes(),
        }

    def _why_now(
        self,
        readings: dict[str, dict[str, Any]],
        power_w: float | None,
        compressor: bool | None,
        mode: str,
    ) -> dict[str, Any]:
        indoor_c = self._temperature_c(readings[CONF_INDOOR_TEMP_ENTITY])
        target_c = self._temperature_c(readings[CONF_TARGET_TEMP_ENTITY])
        outdoor_c = self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY])
        dhw_c = self._temperature_c(readings[CONF_DHW_TEMP_ENTITY])
        evidence = []
        if power_w is not None:
            evidence.append(f"Elektrische Leistung {_round(power_w / 1000)} kW")
        if indoor_c is not None and target_c is not None:
            evidence.append(f"Innen {indoor_c:.1f} °C bei Soll {target_c:.1f} °C")
        if outdoor_c is not None:
            evidence.append(f"Außen {outdoor_c:.1f} °C")
        if dhw_c is not None:
            evidence.append(f"Warmwasser {dhw_c:.1f} °C")
        normalized_mode = mode.lower()
        if (
            "water" in normalized_mode
            or "dhw" in normalized_mode
            or "warm" in normalized_mode
        ):
            headline = "Warmwasserbereitung ist aktiv"
        elif (
            compressor is True
            and indoor_c is not None
            and (target_c is not None)
            and (indoor_c < target_c - 0.2)
        ):
            headline = "Die Wärmepumpe gleicht den Komfortbedarf aus"
        elif compressor is True:
            headline = "Der Kompressor deckt aktuell Wärmebedarf"
        elif compressor is False:
            headline = "Aktuell besteht keine Kompressoranfrage"
        else:
            headline = "Betriebsgrund noch nicht eindeutig"
        confidence_percent = min(95, 35 + len(evidence) * 15)
        if confidence_percent < 60:
            headline = "Betriebsgrund noch nicht sicher bestimmbar"
        return {
            "headline": headline,
            "explanation": "Die Aussage basiert nur auf den aktuell verfügbaren Betriebs- und Temperatursignalen.",
            "evidence": evidence,
            "confidence_percent": confidence_percent,
            "origin": "rule_based_live_explanation",
        }

    def _observed_modes(self) -> dict[str, int]:
        mode_durations_seconds = self._observed_durations()
        return {
            operation_mode: round(duration_seconds / 60)
            for operation_mode, duration_seconds in mode_durations_seconds.items()
        }

    def _observed_durations(self) -> dict[str, float]:
        mode_durations_seconds = {
            "heating": 0.0,
            "dhw": 0.0,
            "standby": 0.0,
            "defrost": 0.0,
            "unknown": 0.0,
        }
        samples_by_timestamp = {
            sample.get("timestamp"): sample
            for sample in self._samples
            if isinstance(sample.get("timestamp"), datetime)
        }
        ordered_samples = sorted(
            samples_by_timestamp.values(), key=lambda item: item["timestamp"]
        )
        maximum_gap_seconds = 900
        for previous_sample, next_sample in zip(ordered_samples, ordered_samples[1:]):
            elapsed_seconds = (
                next_sample["timestamp"] - previous_sample["timestamp"]
            ).total_seconds()
            if elapsed_seconds <= 0:
                continue
            if elapsed_seconds > maximum_gap_seconds:
                mode_durations_seconds["unknown"] += elapsed_seconds
                continue
            operation_mode = previous_sample.get("operation_mode")
            if (
                operation_mode not in mode_durations_seconds
                or operation_mode == "unknown"
            ):
                compressor_on = previous_sample.get("compressor_on")
                operation_mode = (
                    "heating"
                    if compressor_on is True
                    else "standby" if compressor_on is False else "unknown"
                )
            mode_durations_seconds[operation_mode] += elapsed_seconds
        return mode_durations_seconds

    def _efficiency(
        self, readings: dict[str, dict[str, Any]], now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        electrical_power_w = self._power_w(readings[CONF_WP_POWER_ENTITY])
        compressor_power_w = (
            electrical_power_w if self._is_separate_topology() else None
        )
        heating_element_power_w = self._power_w(
            readings[CONF_HEATING_ELEMENT_POWER_ENTITY]
        )
        total_electrical_power_w = (
            (compressor_power_w or 0.0) + (heating_element_power_w or 0.0)
            if self._is_separate_topology()
            and (compressor_power_w is not None or heating_element_power_w is not None)
            else electrical_power_w
        )
        flow_c = self._temperature_c(readings[CONF_FLOW_TEMP_ENTITY])
        return_c = self._temperature_c(readings[CONF_RETURN_TEMP_ENTITY])
        volume_flow_l_min = self._volume_l_min(readings[CONF_VOLUME_FLOW_ENTITY])
        outdoor_c = self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY])
        modeled_cop = (
            self._modeled_cop_evaluation(readings, outdoor_c, now)
            if outdoor_c is not None
            and (not is_false(self._config, CONF_HEAT_PUMP_ENABLED))
            else None
        )
        thermal_power_kw = None
        thermal_power_reason = None
        hydraulic_readings = (
            readings[CONF_FLOW_TEMP_ENTITY],
            readings[CONF_RETURN_TEMP_ENTITY],
            readings[CONF_VOLUME_FLOW_ENTITY],
        )
        generator_flow_implausible = self._observe_generator_flow(readings, now)
        if flow_c is None:
            thermal_power_reason = self._input_unavailable_reason(
                readings[CONF_FLOW_TEMP_ENTITY], "flow_temperature"
            )
        elif return_c is None:
            thermal_power_reason = self._input_unavailable_reason(
                readings[CONF_RETURN_TEMP_ENTITY], "return_temperature"
            )
        elif volume_flow_l_min is None:
            thermal_power_reason = self._input_unavailable_reason(
                readings[CONF_VOLUME_FLOW_ENTITY], "volume_flow"
            )
        elif self._heat_pump_idle(
            readings,
            total_electrical_power_w,
            volume=volume_flow_l_min,
            flow=flow_c,
            return_temp=return_c,
        ):
            thermal_power_reason = "heat_pump_idle"
        elif generator_flow_implausible:
            thermal_power_reason = GENERATOR_FLOW_THERMAL_REASON
        elif volume_flow_l_min <= 0:
            thermal_power_reason = "volume_flow_non_positive"
        elif flow_c <= return_c:
            thermal_power_reason = "flow_temperature_not_above_return"
        elif not self._inputs_synchronous(*hydraulic_readings):
            thermal_power_reason = "hydraulic_inputs_not_synchronous"
        else:
            thermal_power_kw = volume_flow_l_min * (flow_c - return_c) * 0.06977
        electrical_boundary_complete = not (
            self._is_separate_topology()
            and is_true(self._config, CONF_HAS_HEATING_ELEMENT)
            and (heating_element_power_w is None)
        )
        cop = None
        cop_reason = None
        electrical_readings = [readings[CONF_WP_POWER_ENTITY]]
        if self._is_separate_topology() and is_true(
            self._config, CONF_HAS_HEATING_ELEMENT
        ):
            electrical_readings.append(readings[CONF_HEATING_ELEMENT_POWER_ENTITY])
        if thermal_power_kw is None:
            cop_reason = thermal_power_reason
        elif total_electrical_power_w is None:
            cop_reason = self._input_unavailable_reason(
                readings[CONF_WP_POWER_ENTITY], "electrical_power"
            )
        elif total_electrical_power_w <= _MIN_COP_ELECTRICAL_POWER_W:
            cop_reason = "electrical_power_too_low"
        elif not electrical_boundary_complete:
            cop_reason = "electrical_power_boundary_incomplete"
        elif not self._inputs_synchronous(*hydraulic_readings, *electrical_readings):
            cop_reason = "cop_inputs_not_synchronous"
        else:
            calculated_cop = thermal_power_kw / (total_electrical_power_w / 1000)
            if 0.5 <= calculated_cop <= 12:
                cop = calculated_cop
            else:
                cop_reason = "cop_outside_plausible_range"
        electric_kwh = self._energy_kwh(readings[CONF_WP_ENERGY_TODAY])
        compressor_electric_kwh = electric_kwh if self._is_separate_topology() else None
        heating_element_electric_kwh = self._energy_kwh(
            readings[CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY]
        )
        component_electric_kwh = sum(
            (
                metric_value
                for metric_value in (
                    compressor_electric_kwh,
                    heating_element_electric_kwh,
                )
                if metric_value is not None
            )
        )
        if self._is_separate_topology() and electric_kwh is not None:
            electric_kwh += heating_element_electric_kwh or 0.0
        elif electric_kwh is None and component_electric_kwh:
            electric_kwh = component_electric_kwh
        thermal_kwh = self._energy_kwh(readings[CONF_THERMAL_ENERGY_ENTITY])
        daily_work_factor = (
            thermal_kwh / electric_kwh
            if thermal_kwh is not None
            and electric_kwh is not None
            and (electric_kwh > 0.1)
            else None
        )
        if daily_work_factor is not None:
            if not 0.5 <= daily_work_factor <= 12:
                daily_work_factor = None
        reported_jaz = self._number(readings[CONF_JAZ_ENTITY])
        if reported_jaz is not None:
            if not 0.5 <= reported_jaz <= 12:
                reported_jaz = None
        mode_segmented_cop = self._mode_segmented_cop()
        heat_pump_idle = thermal_power_reason == "heat_pump_idle"
        last_valid_metrics = (
            self._last_valid_operating_metrics(now) if heat_pump_idle else None
        )
        last_valid_thermal_kw = (
            last_valid_metrics["thermal_power_kw"] if last_valid_metrics else None
        )
        last_valid_cop = last_valid_metrics["cop"] if last_valid_metrics else None
        last_valid_at = (
            last_valid_metrics["timestamp"].isoformat() if last_valid_metrics else None
        )
        return {
            "available": any(
                (
                    metric_value is not None
                    for metric_value in (
                        electrical_power_w,
                        daily_work_factor,
                        reported_jaz,
                    )
                )
            ),
            "electric_kwh": _round(electric_kwh),
            "compressor_electric_kwh": _round(compressor_electric_kwh),
            "heating_element_electric_kwh": _round(heating_element_electric_kwh),
            "electric_energy_origin": (
                "component_daily_energy_counters"
                if self._is_separate_topology() and compressor_electric_kwh is not None
                else (
                    "total_daily_energy_counter"
                    if self._energy_kwh(readings[CONF_WP_ENERGY_TODAY]) is not None
                    else (
                        "component_daily_energy_counters"
                        if component_electric_kwh
                        else "not_available"
                    )
                )
            ),
            "thermal_kwh": _round(thermal_kwh),
            "daily_work_factor": _round(daily_work_factor),
            "reported_jaz": _round(reported_jaz),
            "work_factor_origin": (
                "configured_jaz_sensor"
                if reported_jaz is not None
                else (
                    "matching_daily_energy_counters"
                    if daily_work_factor is not None
                    else "not_available"
                )
            ),
            "electric_power_kw": _round(
                total_electrical_power_w / 1000
                if total_electrical_power_w is not None
                else None
            ),
            "compressor_power_kw": _round(
                compressor_power_w / 1000 if compressor_power_w is not None else None
            ),
            "heating_element_power_kw": _round(
                heating_element_power_w / 1000
                if heating_element_power_w is not None
                else None
            ),
            "thermal_power_kw": _round(thermal_power_kw),
            "thermal_power_origin": (
                "derived_synchronous_water_loop"
                if thermal_power_kw is not None
                else (
                    "last_measured"
                    if heat_pump_idle and last_valid_thermal_kw is not None
                    else "not_available"
                )
            ),
            "thermal_power_reason": thermal_power_reason,
            "thermal_power_last_valid_kw": _round(last_valid_thermal_kw),
            "cop": _round(cop),
            "cop_origin": (
                "derived_synchronous_water_loop"
                if cop is not None
                else (
                    "last_measured"
                    if heat_pump_idle and last_valid_cop is not None
                    else "not_available"
                )
            ),
            "cop_reason": cop_reason,
            "cop_last_valid": _round(last_valid_cop),
            "last_valid_at": last_valid_at,
            "cop_heating": _round(mode_segmented_cop["heating"]),
            "cop_dhw": _round(mode_segmented_cop["dhw"]),
            "cop_heating_samples": mode_segmented_cop["heating_samples"],
            "cop_dhw_samples": mode_segmented_cop["dhw_samples"],
            "cop_heating_reason": mode_segmented_cop["heating_reason"],
            "cop_dhw_reason": mode_segmented_cop["dhw_reason"],
            "cop_input_max_skew_seconds": _COP_MAX_INPUT_SKEW_SECONDS,
            "flow_c": _round(flow_c, 1),
            "return_c": _round(return_c, 1),
            "volume_flow_l_min": _round(volume_flow_l_min, 1),
            "modeled_cop": _round(modeled_cop.cop) if modeled_cop is not None else None,
            "cop_sink_temp": (
                _round(modeled_cop.sink_temp, 1) if modeled_cop is not None else None
            ),
            "cop_sink_origin": (
                modeled_cop.sink_origin
                if modeled_cop is not None
                else (
                    None
                    if is_false(self._config, CONF_HEAT_PUMP_ENABLED)
                    else SINK_ORIGIN_UNCONFIGURED
                )
            ),
            "note": "Ein realer COP wird nur aus synchronen elektrischen und hydraulischen Momentanwerten berechnet.",
        }

    def _modeled_cop_evaluation(
        self,
        readings: dict[str, dict[str, Any]],
        outdoor: float,
        now: datetime | None = None,
    ) -> Any | None:
        """Evaluate the model COP with the same sink cascade used in production."""
        from .thermodynamics.thermodynamics_engine import (
            ThermodynamicsEngine as thermodynamics_engine_type,
        )

        try:
            thermodynamics_engine = thermodynamics_engine_type(
                self._config.get(CONF_WP_TYPE, DEFAULT_WP_TYPE),
                float(
                    self._config.get(
                        CONF_HEATING_CAPACITY_KW, DEFAULT_HEATING_CAPACITY_KW
                    )
                ),
                float(self._config.get(CONF_COP_RATED, DEFAULT_COP_RATED)),
            )
        except (TypeError, ValueError):
            return
        return thermodynamics_engine.evaluate_cop(
            outdoor,
            **self._generator_sink_kwargs(readings, now or datetime.now(timezone.utc)),
        )

    @staticmethod
    def _completed_actual_kw(
        point: dict[str, Any], forecast_hour: datetime, now: datetime
    ) -> float | None:
        """kW of a finished hour. The current hour and every later hour stay empty."""
        actual_kwh = point.get("actual_kwh")
        if (
            isinstance(actual_kwh, bool)
            or not isinstance(actual_kwh, (int, float))
            or (not isfinite(float(actual_kwh)))
            or (float(actual_kwh) < 0)
        ):
            return None
        interval_hours = EAIInsightsEngine._interval_duration_hours(point)
        if interval_hours <= 0:
            interval_hours = 1.0
        interval_end = forecast_hour + timedelta(hours=interval_hours)
        if interval_end.tzinfo is None:
            interval_end = interval_end.replace(tzinfo=timezone.utc)
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if interval_end > now_utc.astimezone(interval_end.tzinfo):
            return
        return float(actual_kwh) / interval_hours

    def _consumption_drift(self, now: datetime) -> str:
        """Seven-day median of forecast/actual from the calibrator, excluding today."""
        calibrator = self._forecast_calibrator()
        get_drift_status = getattr(calibrator, "drift_status", None)
        if get_drift_status is None:
            return "insufficient_history"
        local_day = (
            now.astimezone(self._local_timezone()).date()
            if now.tzinfo is not None
            else now.date()
        )
        try:
            drift_status = get_drift_status(local_day)
        except Exception:
            return "insufficient_history"
        if drift_status not in {"stable", "degraded", "insufficient_history"}:
            return "insufficient_history"
        return drift_status

    def rebase_energy_counters(self, local_day: date) -> None:
        """Set every counter baseline to the last accepted reading.

        History rows stay. The next rise is only the new increment.
        """
        rebase_daily_energy_baselines(self._daily_energy, local_day)

    def _forecast_calibrator(self) -> Any | None:
        """Coordinator calibrator, or None when this runtime cannot see it.

        Without it the building model keeps the temperature-only heating limit.
        """
        coordinator = getattr(self._runtime, "coordinator", None)
        return getattr(coordinator, "thermodynamics_calibrator", None)

    def _learned_u_factor(self, calibrator: Any | None) -> float | None:
        """U-factor snapshot for this forecast, or None below 24 samples."""
        if calibrator is None:
            return
        try:
            sample_count = int(getattr(calibrator, "u_factor_samples", 0))
        except (TypeError, ValueError):
            return
        if sample_count < 24:
            return
        aligned_u_factor = getattr(calibrator, "u_factor_aligned", None)
        if aligned_u_factor is not None:
            try:
                parsed_u_factor = float(aligned_u_factor)
            except (TypeError, ValueError):
                parsed_u_factor = None
            if parsed_u_factor is not None and isfinite(parsed_u_factor):
                return parsed_u_factor
        try:
            estimated_u_factor = float(getattr(calibrator, "u_factor_estimate"))
        except (TypeError, ValueError):
            return
        if not isfinite(estimated_u_factor):
            return
        return estimated_u_factor

    def _blocked_space_heating_days(
        self,
        source: list[dict[str, Any]],
        weather_temperature_by_hour: dict[datetime, float],
        calibrator: Any | None = None,
    ) -> set[date]:
        """Local days whose forecast mean trips the shared heating limit.

        Current-sensor fallback hours are not forecast samples and do not count.
        Wind is 0 m/s, matching ``WeatherConditions`` in the building model.
        """
        from .thermodynamics.thermodynamics_engine import (
            ThermodynamicsEngine as thermodynamics_engine_type,
            space_heating_suppressed_by_day as space_heating_suppressed_by_day,
        )

        try:
            thermodynamics_engine = thermodynamics_engine_type(
                self._config.get(CONF_WP_TYPE, DEFAULT_WP_TYPE),
                float(
                    self._config.get(
                        CONF_HEATING_CAPACITY_KW, DEFAULT_HEATING_CAPACITY_KW
                    )
                ),
                float(self._config.get(CONF_COP_RATED, DEFAULT_COP_RATED)),
            )
        except (TypeError, ValueError):
            return set()
        local_timezone = self._local_timezone()
        weather_samples = []
        for forecast_point in source:
            forecast_hour = self._forecast_point_hour(forecast_point)
            if forecast_hour is not None:
                outdoor_c = self._forecast_temperature(forecast_point)
                if outdoor_c is None:
                    outdoor_c = weather_temperature_by_hour.get(forecast_hour)
                weather_samples.append((forecast_hour, outdoor_c, 0.0))
        suppression_by_day = space_heating_suppressed_by_day(
            thermodynamics_engine,
            weather_samples,
            local_timezone,
            calibrator=calibrator,
            has_dhw=is_true(self._config, CONF_HAS_DHW),
        )
        return {
            local_day
            for local_day, suppressed in suppression_by_day.items()
            if suppressed
        }

    def _modeled_hourly_consumption(
        self,
        readings: dict[str, dict[str, Any]],
        outdoor: float,
        hour: int,
        indoor_temp: float,
        suppress_space_heating: bool = False,
        learned_u_factor: float | None = None,
        now: datetime | None = None,
    ) -> Any | None:
        """Hourly electrical load from the same engine used in production."""
        from .thermodynamics.thermodynamics_engine import (
            ThermodynamicsEngine as thermodynamics_engine_type,
            WeatherConditions as weather_conditions_type,
        )

        try:
            thermodynamics_engine = thermodynamics_engine_type(
                self._config.get(CONF_WP_TYPE, DEFAULT_WP_TYPE),
                float(
                    self._config.get(
                        CONF_HEATING_CAPACITY_KW, DEFAULT_HEATING_CAPACITY_KW
                    )
                ),
                float(self._config.get(CONF_COP_RATED, DEFAULT_COP_RATED)),
            )
        except (TypeError, ValueError):
            return
        if learned_u_factor is not None:
            thermodynamics_engine.u_factor = learned_u_factor
        return thermodynamics_engine.calculate_hourly_consumption(
            weather_conditions_type(outdoor_temp=outdoor),
            indoor_temp=indoor_temp,
            hour=hour,
            suppress_space_heating=suppress_space_heating,
            **self._generator_sink_kwargs(readings, now or datetime.now(timezone.utc)),
            **dhw_forecast_kwargs(self._config),
        )

    def _cop_sink_origin(self, readings: dict[str, dict[str, Any]]) -> str | None:
        if is_false(self._config, CONF_HEAT_PUMP_ENABLED):
            return
        outdoor_c = self._temperature_c(readings.get(CONF_OUTDOOR_TEMP_ENTITY, {}))
        if outdoor_c is None:
            return SINK_ORIGIN_UNCONFIGURED
        cop_evaluation = self._modeled_cop_evaluation(readings, outdoor_c)
        if cop_evaluation is None:
            return SINK_ORIGIN_UNCONFIGURED
        return cop_evaluation.sink_origin

    def _mode_segmented_cop(self) -> dict[str, Any]:
        totals_by_mode = {
            "heating": {"thermal": 0.0, "electric": 0.0, "samples": 0},
            "dhw": {"thermal": 0.0, "electric": 0.0, "samples": 0},
        }
        for sample in self._samples:
            if sample.get("compressor_on") is False:
                continue
            thermal_power_kw = sample.get("thermal_power_kw")
            electrical_power_w = sample.get("power_w")
            if (
                not isinstance(thermal_power_kw, (int, float))
                or not isinstance(electrical_power_w, (int, float))
                or (not not electrical_power_w <= _MIN_COP_ELECTRICAL_POWER_W)
                or (thermal_power_kw <= 0)
            ):
                continue
            operation_mode = sample.get("operation_mode")
            if operation_mode not in totals_by_mode:
                continue
            electrical_power_kw = electrical_power_w / 1000.0
            if electrical_power_kw <= 0:
                continue
            sample_cop = thermal_power_kw / electrical_power_kw
            if not 0.5 <= sample_cop <= 12:
                continue
            totals_by_mode[operation_mode]["thermal"] += thermal_power_kw
            totals_by_mode[operation_mode]["electric"] += electrical_power_kw
            totals_by_mode[operation_mode]["samples"] += 1
            continue
        cop_by_mode = {
            "heating": None,
            "dhw": None,
            "heating_samples": 0,
            "dhw_samples": 0,
            "heating_reason": None,
            "dhw_reason": None,
        }
        for operation_mode in ("heating", "dhw"):
            sample_count = totals_by_mode[operation_mode]["samples"]
            cop_by_mode[f"{operation_mode}_samples"] = sample_count
            mode_electrical_total = totals_by_mode[operation_mode]["electric"]
            if sample_count < 6 or mode_electrical_total <= 0:
                cop_by_mode[f"{operation_mode}_reason"] = "insufficient_samples"
            else:
                cop_by_mode[operation_mode] = (
                    totals_by_mode[operation_mode]["thermal"] / mode_electrical_total
                )
        return cop_by_mode

    def _is_separate_topology(self) -> bool:
        return (
            self._config.get(
                CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
                DEFAULT_ELECTRICAL_MEASUREMENT_TOPOLOGY,
            )
            == ELECTRICAL_TOPOLOGY_SEPARATE
        )

    def _building(self, readings: dict[str, dict[str, Any]]) -> dict[str, Any]:
        indoor_c = self._temperature_c(readings[CONF_INDOOR_TEMP_ENTITY])
        outdoor_c = self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY])
        target_c = self._temperature_c(readings[CONF_TARGET_TEMP_ENTITY])
        thermal_inertia_hours = self._thermal_inertia()
        (
            heat_loss_kw,
            heat_loss_coefficient_kw_k,
            heat_loss_reason,
            heat_loss_sample_count,
            heat_loss_observed_hours,
        ) = self._heat_loss_estimate(indoor_c, outdoor_c, target_c)
        learning_observed_seconds = sum(
            (
                mode_duration_seconds
                for operation_mode, mode_duration_seconds in self._observed_durations().items()
                if operation_mode != "unknown"
            )
        )
        learning_progress_percent = min(
            100, round(learning_observed_seconds / 86400 * 100)
        )
        thermal_loss = thermal_loss_snapshot(
            self._samples, self._config.get(CONF_STORAGE_VOLUME_L)
        )
        return {
            "available": indoor_c is not None and outdoor_c is not None,
            "indoor_c": _round(indoor_c, 1),
            "outdoor_c": _round(outdoor_c, 1),
            "target_c": _round(target_c, 1),
            "comfort_delta_c": (
                _round(indoor_c - target_c, 1)
                if indoor_c is not None and target_c is not None
                else None
            ),
            "thermal_inertia_hours": _round(thermal_inertia_hours, 1),
            "thermal_inertia_hours_reason": self._thermal_inertia_reason(),
            "heat_loss_kw": _round(heat_loss_kw),
            "heat_loss_kw_reason": heat_loss_reason,
            "heat_loss_coefficient_kw_k": _round(heat_loss_coefficient_kw_k, 3),
            "heat_loss_origin": (
                "normalized_stable_heating_observation"
                if heat_loss_kw is not None
                else "learning"
            ),
            "heat_loss_sample_count": heat_loss_sample_count,
            "heat_loss_observed_hours": _round(heat_loss_observed_hours, 1),
            "learning_progress_percent": learning_progress_percent,
            "learning_observed_hours": _round(learning_observed_seconds / 3600, 1),
            "learning_required_hours": _BUILDING_LEARNING_REQUIRED_HOURS,
            "learning_sample_count": len(self._samples),
            "temperature_spread_c": (
                _round((target_c or indoor_c) - outdoor_c, 1)
                if outdoor_c is not None
                and (target_c is not None or indoor_c is not None)
                else None
            ),
            "source_temperature_c": _round(
                self._temperature_c(readings[CONF_SOURCE_TEMP_ENTITY]), 1
            ),
            "storage_temperature_c": _round(
                self._temperature_c(readings[CONF_STORAGE_TEMP_ENTITY]), 1
            ),
            "uncertainty_percent": max(10, 100 - learning_progress_percent),
            "thermal_loss": thermal_loss,
        }

    def _heat_loss_estimate(
        self, indoor: float | None, outdoor: float | None, target: float | None
    ) -> tuple[float | None, float | None, str | None, int, float]:
        valid_samples = sorted(
            (
                sample
                for sample in self._samples
                if sample.get("compressor_on") is True
                if sample.get("operation_mode") == "heating"
                if isinstance(sample.get("thermal_power_kw"), (int, float))
                if isinstance(sample.get("indoor_c"), (int, float))
                if isinstance(sample.get("outdoor_c"), (int, float))
                if isinstance(sample.get("target_c"), (int, float))
                if abs(float(sample["indoor_c"]) - float(sample["target_c"])) <= 0.3
                if float(sample["indoor_c"]) - float(sample["outdoor_c"]) >= 5
            ),
            key=lambda sample: sample["timestamp"],
        )
        sample_count = len(valid_samples)
        observed_hours = (
            (
                valid_samples[-1]["timestamp"] - valid_samples[0]["timestamp"]
            ).total_seconds()
            / 3600
            if sample_count >= 2
            else 0.0
        )
        if sample_count < 6:
            return (
                None,
                None,
                "steady_state_samples_insufficient",
                sample_count,
                observed_hours,
            )
        if observed_hours < 2:
            return (
                None,
                None,
                "steady_state_observation_too_short",
                sample_count,
                observed_hours,
            )
        indoor_temperatures_c = [float(sample["indoor_c"]) for sample in valid_samples]
        if max(indoor_temperatures_c) - min(indoor_temperatures_c) > 0.3:
            return (
                None,
                None,
                "indoor_temperature_not_stable",
                sample_count,
                observed_hours,
            )
        heat_loss_coefficients_kw_k = [
            float(sample["thermal_power_kw"])
            / (float(sample["indoor_c"]) - float(sample["outdoor_c"]))
            for sample in valid_samples
        ]
        heat_loss_coefficients_kw_k = [
            heat_loss_coefficient
            for heat_loss_coefficient in heat_loss_coefficients_kw_k
            if 0.01 <= heat_loss_coefficient <= 5
        ]
        if len(heat_loss_coefficients_kw_k) < 6:
            return (
                None,
                None,
                "heat_loss_coefficient_implausible",
                sample_count,
                observed_hours,
            )
        reference_indoor_c = target if target is not None else indoor
        if reference_indoor_c is None or outdoor is None:
            return (
                None,
                None,
                "temperature_spread_unavailable",
                sample_count,
                observed_hours,
            )
        temperature_spread_k = reference_indoor_c - outdoor
        if temperature_spread_k < 1:
            return (
                None,
                None,
                "temperature_spread_insufficient",
                sample_count,
                observed_hours,
            )
        heat_loss_coefficient_kw_k = median(heat_loss_coefficients_kw_k)
        return (
            heat_loss_coefficient_kw_k * temperature_spread_k,
            heat_loss_coefficient_kw_k,
            None,
            sample_count,
            observed_hours,
        )

    def _thermal_inertia(self) -> float | None:
        temperature_samples = [
            sample for sample in self._samples if sample.get("indoor_c") is not None
        ]
        if len(temperature_samples) < 12:
            return
        response_candidates = self._thermal_response_candidates(temperature_samples)
        return median(response_candidates) if response_candidates else None

    def _thermal_inertia_reason(self) -> str | None:
        temperature_samples = [
            sample for sample in self._samples if sample.get("indoor_c") is not None
        ]
        if len(temperature_samples) < 12:
            return "learning_samples_insufficient"
        observed_hours = (
            temperature_samples[-1]["timestamp"] - temperature_samples[0]["timestamp"]
        ).total_seconds() / 3600
        if observed_hours < 1:
            return "observation_window_too_short"
        if not self._thermal_response_candidates(temperature_samples):
            return "temperature_change_insufficient"

    @staticmethod
    def _thermal_response_candidates(valid: list[dict[str, Any]]) -> list[float]:
        ordered_samples = sorted(valid, key=lambda sample: sample["timestamp"])
        response_candidates = []
        for sample_index, earlier_sample in enumerate(ordered_samples[:-1]):
            for later_sample in ordered_samples[sample_index + 1 :]:
                elapsed_hours = (
                    later_sample["timestamp"] - earlier_sample["timestamp"]
                ).total_seconds() / 3600
                if elapsed_hours > 24:
                    break
                temperature_change_k = abs(
                    float(later_sample["indoor_c"]) - float(earlier_sample["indoor_c"])
                )
                if not elapsed_hours >= 1:
                    continue
                if not temperature_change_k >= 0.1:
                    continue
                response_candidates.append(elapsed_hours / temperature_change_k)
                break
        return response_candidates

    @classmethod
    def _forecast_horizon(
        cls, hours: Any, target_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        if not isinstance(hours, list):
            return []
        selected_intervals = []
        covered_hours = 0.0
        for forecast_point in hours:
            if not isinstance(forecast_point, dict):
                continue
            selected_intervals.append(forecast_point)
            covered_hours += cls._interval_duration_hours(forecast_point)
            if covered_hours >= target_hours - 0.0002777777777777778:
                break
        return selected_intervals if True else None

    @staticmethod
    def _interval_duration_hours(point: dict[str, Any]) -> float:
        try:
            interval_start = datetime.fromisoformat(
                str(point["interval_start"]).replace("Z", "+00:00")
            )
            interval_end = datetime.fromisoformat(
                str(point["interval_end"]).replace("Z", "+00:00")
            )
            if interval_start.tzinfo is None:
                interval_start = interval_start.replace(tzinfo=timezone.utc)
            if interval_end.tzinfo is None:
                interval_end = interval_end.replace(tzinfo=timezone.utc)
            interval_hours = (interval_end - interval_start).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            interval_hours = 0.0
        if interval_hours > 0 and isfinite(interval_hours):
            return interval_hours
        try:
            interval_hours = float(point.get("interval_hours"))
        except (TypeError, ValueError):
            return 0.0
        return (
            interval_hours if interval_hours > 0 and isfinite(interval_hours) else 0.0
        )

    @staticmethod
    def _allocation_number(
        point: dict[str, Any], key: str, *aliases: str
    ) -> float | None:
        for candidate_key in [key, *aliases]:
            raw_value = point.get(candidate_key)
            if isinstance(raw_value, bool):
                continue
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not isfinite(numeric_value):
                continue
            if not numeric_value >= 0:
                continue
            return numeric_value

    @classmethod
    def _allocation_point_valid(cls, point: dict[str, Any]) -> bool:
        pv_forecast_kwh = cls._allocation_number(point, "pv_forecast_kwh")
        household_base_load_kwh = cls._allocation_number(
            point, "household_base_load_kwh"
        )
        household_pv_kwh = cls._allocation_number(point, "household_pv_kwh")
        heat_pump_kwh = cls._allocation_number(point, "heat_pump_kwh")
        heat_pump_pv_kwh = cls._allocation_number(point, "heat_pump_pv_kwh")
        battery_reserve_kwh = cls._allocation_number(point, "battery_reserve_kwh")
        battery_pv_reserve_kwh = cls._allocation_number(
            point, "battery_pv_reserve_kwh", "battery_pv_reserved_kwh"
        )
        pv_calibration_reserve_kwh = cls._allocation_number(
            point, "pv_calibration_reserve_kwh"
        )
        wallbox_pv_available_kwh = cls._allocation_number(
            point, "wallbox_pv_available_kwh"
        )
        unallocated_or_export_kwh = cls._allocation_number(
            point, "unallocated_or_export_kwh", "expected_grid_export_kwh"
        )
        residual_pv_kwh = cls._allocation_number(point, "residual_pv_kwh")
        pv_balance_error_kwh = point.get("pv_balance_error_kwh")
        pv_balance_tolerance_kwh = cls._allocation_number(
            point, "pv_balance_tolerance_kwh"
        )
        if isinstance(pv_balance_error_kwh, bool):
            pv_balance_error_kwh = None
        try:
            pv_balance_error_kwh = float(pv_balance_error_kwh)
        except (TypeError, ValueError):
            pv_balance_error_kwh = None
        required_values = (
            pv_forecast_kwh,
            household_base_load_kwh,
            household_pv_kwh,
            heat_pump_kwh,
            heat_pump_pv_kwh,
            battery_reserve_kwh,
            battery_pv_reserve_kwh,
            pv_calibration_reserve_kwh,
            wallbox_pv_available_kwh,
            unallocated_or_export_kwh,
            pv_balance_tolerance_kwh,
        )
        if (
            any((allocation_value is None for allocation_value in required_values))
            or pv_balance_error_kwh is None
            or (not isfinite(pv_balance_error_kwh))
        ):
            return False
        calculated_balance_error_kwh = pv_forecast_kwh - (
            household_pv_kwh
            + heat_pump_pv_kwh
            + battery_pv_reserve_kwh
            + pv_calibration_reserve_kwh
            + wallbox_pv_available_kwh
            + unallocated_or_export_kwh
        )
        residual_balance_valid = (
            residual_pv_kwh is None
            or abs(
                residual_pv_kwh - wallbox_pv_available_kwh - unallocated_or_export_kwh
            )
            <= pv_balance_tolerance_kwh
        )
        return bool(
            point.get("allocation_valid") is True
            and point.get("allocation_status")
            in {"valid", "closed", "balanced", "complete"}
            and (household_pv_kwh <= household_base_load_kwh + pv_balance_tolerance_kwh)
            and (heat_pump_pv_kwh <= heat_pump_kwh + pv_balance_tolerance_kwh)
            and (
                battery_pv_reserve_kwh <= battery_reserve_kwh + pv_balance_tolerance_kwh
            )
            and (abs(pv_balance_error_kwh) <= pv_balance_tolerance_kwh)
            and (
                abs(calculated_balance_error_kwh - pv_balance_error_kwh)
                <= pv_balance_tolerance_kwh
            )
            and residual_balance_valid
        )

    def _energy(self, forecast: dict[str, Any]) -> dict[str, Any]:
        forecast_hours = self._forecast_horizon(forecast.get("hours"))
        allocation_status = self._energy_allocator.status()

        def total(key: str, *aliases: str) -> float | None:
            allocation_values = [
                self._allocation_number(forecast_point, key, *aliases)
                for forecast_point in forecast_hours
            ]
            return (
                sum(
                    (
                        allocation_value
                        for allocation_value in allocation_values
                        if allocation_value is not None
                    )
                )
                if forecast_hours
                and all(
                    (
                        allocation_value is not None
                        for allocation_value in allocation_values
                    )
                )
                else None
            )

        pv_forecast_kwh = total("pv_forecast_kwh")
        heat_pump_kwh = total("heat_pump_kwh")
        household_pv_kwh = total("household_pv_kwh")
        heat_pump_pv_kwh = total("heat_pump_pv_kwh")
        household_base_load_kwh = total("household_base_load_kwh")
        battery_reserve_kwh = total("battery_reserve_kwh")
        battery_pv_reserve_kwh = total(
            "battery_pv_reserve_kwh", "battery_pv_reserved_kwh"
        )
        pv_calibration_reserve_kwh = total("pv_calibration_reserve_kwh")
        wallbox_pv_available_kwh = total("wallbox_pv_available_kwh")
        unallocated_or_export_kwh = total(
            "unallocated_or_export_kwh", "expected_grid_export_kwh"
        )
        residual_pv_kwh = total("residual_pv_kwh")
        if (
            residual_pv_kwh is None
            and wallbox_pv_available_kwh is not None
            and (unallocated_or_export_kwh is not None)
        ):
            residual_pv_kwh = wallbox_pv_available_kwh + unallocated_or_export_kwh
        balance_errors_kwh = [
            forecast_point.get("pv_balance_error_kwh")
            for forecast_point in forecast_hours
            if isinstance(forecast_point.get("pv_balance_error_kwh"), (int, float))
            if not isinstance(forecast_point.get("pv_balance_error_kwh"), bool)
            if isfinite(float(forecast_point["pv_balance_error_kwh"]))
        ]
        pv_balance_error_kwh = (
            sum(balance_errors_kwh)
            if len(balance_errors_kwh) == len(forecast_hours)
            else None
        )
        first_interval_start = (
            forecast_hours[0].get("interval_start") if forecast_hours else None
        )
        last_interval_end = (
            forecast_hours[-1].get("interval_end") if forecast_hours else None
        )
        last_interval_start = (
            forecast_hours[-1].get("interval_start") if forecast_hours else None
        )
        horizon_hours = sum(
            (
                self._interval_duration_hours(forecast_point)
                for forecast_point in forecast_hours
            )
        )
        intervals_contiguous = self._intervals_contiguous(forecast_hours)
        full_day_covered = bool(
            intervals_contiguous and abs(horizon_hours - 24.0) <= 0.016666666666666666
        )
        allocation_valid = bool(
            forecast_hours
            and intervals_contiguous
            and full_day_covered
            and all(
                (
                    self._allocation_point_valid(forecast_point)
                    for forecast_point in forecast_hours
                )
            )
            and (pv_balance_error_kwh is not None)
            and (abs(pv_balance_error_kwh) <= 0.02)
        )
        household_grid_import_kwh = (
            max(0.0, household_base_load_kwh - household_pv_kwh)
            if allocation_valid
            else None
        )
        heat_pump_grid_import_kwh = (
            max(0.0, heat_pump_kwh - heat_pump_pv_kwh) if allocation_valid else None
        )
        horizon_status = (
            "valid"
            if allocation_valid
            else (
                "no_forecast_intervals"
                if not forecast_hours
                else (
                    "incomplete_or_non_contiguous"
                    if not intervals_contiguous or not full_day_covered
                    else (
                        "balance_mismatch"
                        if pv_balance_error_kwh is not None
                        and abs(pv_balance_error_kwh) > 0.02
                        else "invalid_energy_context"
                    )
                )
            )
        )
        return {
            "available": bool(forecast_hours),
            "pv_forecast_kwh": _round(pv_forecast_kwh),
            "heat_pump_kwh": _round(heat_pump_kwh),
            "household_base_load_kwh": (
                _round(household_base_load_kwh) if allocation_valid else None
            ),
            "household_pv_kwh": _round(household_pv_kwh) if allocation_valid else None,
            "heat_pump_pv_kwh": _round(heat_pump_pv_kwh) if allocation_valid else None,
            "household_grid_import_kwh": _round(household_grid_import_kwh),
            "heat_pump_grid_import_kwh": _round(heat_pump_grid_import_kwh),
            "battery_reserve_kwh": (
                _round(battery_reserve_kwh) if allocation_valid else None
            ),
            "battery_pv_reserve_kwh": (
                _round(battery_pv_reserve_kwh) if allocation_valid else None
            ),
            "pv_calibration_reserve_kwh": (
                _round(pv_calibration_reserve_kwh) if allocation_valid else None
            ),
            "wallbox_pv_available_kwh": (
                _round(wallbox_pv_available_kwh) if allocation_valid else None
            ),
            "residual_pv_kwh": _round(residual_pv_kwh) if allocation_valid else None,
            "unallocated_or_export_kwh": (
                _round(unallocated_or_export_kwh) if allocation_valid else None
            ),
            "expected_grid_export_kwh": (
                _round(unallocated_or_export_kwh) if allocation_valid else None
            ),
            "pv_balance_error_kwh": _round(pv_balance_error_kwh),
            "pv_balance_tolerance_kwh": 0.02,
            "pv_balance_difference_kwh": _round(pv_balance_error_kwh),
            "allocation_valid": allocation_valid,
            "allocation_status": horizon_status,
            "forecast_interval_start": first_interval_start,
            "forecast_interval_end": last_interval_end,
            "forecast_interval_hours": _round(horizon_hours, 3),
            "forecast_intervals_contiguous": intervals_contiguous,
            "forecast_covers_24h": full_day_covered,
            "forecast_period_status": (
                "complete_24h" if full_day_covered else "incomplete_or_non_contiguous"
            ),
            "forecast_period_start": first_interval_start,
            "forecast_last_interval_start": last_interval_start,
            "forecast_interval_count": len(forecast_hours),
            "allocation_method": "hourly_priority_with_conservative_surplus_calibration",
            "allocation_provenance": {
                "producer": "solar_forecast_eai",
                "context_provider": "sfml_stats",
                "context_version": allocation_status.get("provider_version"),
                "base_load_origin": allocation_status.get("base_load_origin"),
                "method": "hourly_priority_with_conservative_surplus_calibration",
                "priority_order": allocation_status.get("priority_order", []),
            },
            "energy_context_quality_percent": allocation_status.get(
                "quality_percent", 0
            ),
            "pv_coverage_percent": (
                _round(heat_pump_pv_kwh / heat_pump_kwh * 100, 0)
                if allocation_valid and heat_pump_kwh > 0
                else None
            ),
            "expected_grid_import_kwh": (
                _round(max(0.0, heat_pump_kwh - heat_pump_pv_kwh))
                if allocation_valid
                else None
            ),
            "expected_load_grid_import_kwh": (
                _round(household_grid_import_kwh + heat_pump_grid_import_kwh)
                if allocation_valid
                else None
            ),
            "energy_context": allocation_status,
            "optimization": forecast.get("optimization", {}),
        }

    @staticmethod
    def _intervals_contiguous(hours: list[dict[str, Any]]) -> bool:
        if not hours:
            return False
        previous_interval_end = None
        for forecast_point in hours:
            try:
                interval_start = datetime.fromisoformat(
                    str(forecast_point["interval_start"]).replace("Z", "+00:00")
                )
                interval_end = datetime.fromisoformat(
                    str(forecast_point["interval_end"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                return False
            if interval_start.tzinfo is None:
                interval_start = interval_start.replace(tzinfo=timezone.utc)
            if interval_end.tzinfo is None:
                interval_end = interval_end.replace(tzinfo=timezone.utc)
            if not not interval_end <= interval_start or (
                previous_interval_end is not None
                and abs((interval_start - previous_interval_end).total_seconds()) > 1
            ):
                return False
            previous_interval_end = interval_end
            continue
        return True

    def _diagnostics(
        self,
        readings: dict[str, dict[str, Any]],
        forecast: dict[str, Any],
        operation: dict[str, Any],
        building: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if now is None:
            now = datetime.now(timezone.utc)
        issues = []
        missing_required_keys = [
            input_key
            for input_key in REQUIRED_SENSORS
            if readings[input_key].get("reason") == "not_configured"
        ]
        unavailable_keys = [
            input_key
            for input_key, reading in readings.items()
            if reading.get("reason") == "unavailable"
        ]
        stale_keys = [
            input_key for input_key, reading in readings.items() if reading.get("stale")
        ]
        if self._flow_return_inverted(readings):
            issues.append(
                self._issue(
                    code="flow_return_inverted",
                    category="assignment",
                    severity="warning",
                    input_keys=[CONF_FLOW_TEMP_ENTITY, CONF_RETURN_TEMP_ENTITY],
                    action="Vorlauf- und Rücklauftemperatur prüfen. Ohne eine belastbare Temperaturdifferenz bleiben Wärmeleistung und momentaner COP leer.",
                    technical_details={
                        "flow_c": _round(
                            self._temperature_c(readings[CONF_FLOW_TEMP_ENTITY]), 1
                        ),
                        "return_c": _round(
                            self._temperature_c(readings[CONF_RETURN_TEMP_ENTITY]), 1
                        ),
                        "minimum_inversion_k": _FLOW_RETURN_INVERSION_MIN_K,
                    },
                )
            )
        if self._generator_flow_hint:
            generator_flow_hint = (
                GENERATOR_FLOW_HINT_EN
                if self._language_is_english()
                else GENERATOR_FLOW_HINT_DE
            )
            issues.append(
                self._issue(
                    code="generator_circuit_flow_implausible",
                    category="assignment",
                    severity="warning",
                    input_keys=[CONF_FLOW_TEMP_ENTITY],
                    action=generator_flow_hint,
                    technical_details={
                        "flow_c": _round(
                            self._temperature_c(readings[CONF_FLOW_TEMP_ENTITY]), 1
                        ),
                        "tolerance_k": GENERATOR_FLOW_TOLERANCE_K,
                    },
                )
            )
        assignments_by_entity = {}
        for input_key, reading in readings.items():
            entity_id = reading.get("entity_id")
            if entity_id:
                assignments_by_entity.setdefault(entity_id, []).append(input_key)
        duplicate_assignments = {
            entity_id: assigned_input_keys
            for entity_id, assigned_input_keys in assignments_by_entity.items()
            if len(assigned_input_keys) > 1
            if not is_allowed_shared_assignment(
                self._config, entity_id, set(assigned_input_keys)
            )
        }
        duplicate_input_keys = sorted(
            (
                input_key
                for assigned_input_keys in duplicate_assignments.values()
                for input_key in assigned_input_keys
            )
        )
        if missing_required_keys:
            issues.append(
                self._issue(
                    code="not_configured",
                    category="setup",
                    severity="critical",
                    input_keys=missing_required_keys,
                    action="Configure the required inputs.",
                )
            )
        if unavailable_keys:
            issues.append(
                self._issue(
                    code="unavailable",
                    category="data_source",
                    severity="critical",
                    input_keys=unavailable_keys,
                    action="Restore the configured data sources.",
                )
            )
        if stale_keys:
            issues.append(
                self._issue(
                    code="stale",
                    category="data_source",
                    severity="warning",
                    input_keys=stale_keys,
                    action="Check why the affected data sources are no longer updating.",
                )
            )
        if duplicate_assignments:
            issues.append(
                self._issue(
                    code="duplicate_assignment",
                    category="configuration",
                    severity="warning",
                    input_keys=duplicate_input_keys,
                    action="Assign each input to its intended independent data source.",
                    technical_details={
                        "duplicate_group_count": len(duplicate_assignments)
                    },
                )
            )
        compressor_signal_conflict = bool(operation.get("compressor_signal_conflict"))
        if compressor_signal_conflict:
            issues.append(
                self._issue(
                    code="compressor_signal_conflict",
                    category="data_source",
                    severity="warning",
                    input_keys=[
                        CONF_COMPRESSOR_ENTITY,
                        CONF_OPERATION_MODE_ENTITY,
                        CONF_WP_POWER_ENTITY,
                    ],
                    action="Verify the configured compressor status source and its operating-state semantics.",
                    technical_details={
                        "signal_origin": operation.get("compressor_signal_origin"),
                        "conflict_sources": operation.get(
                            "compressor_signal_conflict_sources", []
                        ),
                    },
                )
            )
        if forecast.get("temperature_fallback_hours", 0) > 0:
            issues.append(
                self._issue(
                    code="forecast_temperature_fallback",
                    category="forecast",
                    severity="info",
                    input_keys=["outdoor_temperature_forecast"],
                    action="Provide forecast temperature data for full forecast quality.",
                    technical_details={
                        "current_outdoor_sensor_available": self._temperature_c(
                            readings[CONF_OUTDOOR_TEMP_ENTITY]
                        )
                        is not None,
                        "forecast_hours": len(forecast.get("hours", [])),
                        "temperature_forecast_hours": forecast.get(
                            "temperature_forecast_hours", 0
                        ),
                        "temperature_fallback_hours": forecast.get(
                            "temperature_fallback_hours", 0
                        ),
                    },
                )
            )
        setup_complete = not missing_required_keys
        required_inputs_usable = not any(
            (
                input_key in REQUIRED_SENSORS
                for input_key in [*unavailable_keys, *stale_keys]
            )
        )
        required_assignment_conflict = any(
            (input_key in REQUIRED_SENSORS for input_key in duplicate_input_keys)
        )
        data_ready = bool(
            setup_complete
            and required_inputs_usable
            and (not required_assignment_conflict)
        )
        data_health_score = max(
            0,
            100
            - len(stale_keys) * 8
            - len(duplicate_assignments) * 10
            - (5 if compressor_signal_conflict else 0),
        )
        configured_sensor_count = sum(
            (bool(reading.get("entity_id")) for reading in readings.values())
        )
        unique_source_count = len(assignments_by_entity)
        available_sensor_count = sum(
            (
                bool(reading.get("entity_id"))
                and bool(reading.get("available"))
                and (not bool(reading.get("stale")))
                for reading in readings.values()
            )
        )
        anomalies = []

        def display_names(keys: list[str]) -> list[str]:
            return [
                self._input_details(input_key)["display_name"]
                for input_key in sorted(set(keys))
            ]

        missing_required_inputs = sorted(
            set(missing_required_keys)
            | {
                input_key
                for input_key in unavailable_keys
                if input_key in REQUIRED_SENSORS
            }
        )
        if missing_required_inputs:
            anomalies.append(
                {
                    "severity": "critical",
                    "code": "required_sensor_missing",
                    "evidence": display_names(missing_required_inputs),
                }
            )
        if stale_keys:
            anomalies.append(
                {
                    "severity": "warning",
                    "code": "stale_sensor",
                    "evidence": display_names(stale_keys),
                }
            )
        if duplicate_assignments:
            anomalies.append(
                {
                    "severity": "warning",
                    "code": "duplicate_assignment",
                    "evidence": display_names(duplicate_input_keys),
                }
            )
        if compressor_signal_conflict:
            anomalies.append(
                {
                    "severity": "warning",
                    "code": "compressor_signal_conflict",
                    "evidence": operation.get("compressor_signal_conflict_sources", []),
                }
            )
        if forecast.get("temperature_fallback_hours", 0) > 0:
            anomalies.append(
                {
                    "severity": "info",
                    "code": "forecast_temperature_fallback",
                    "evidence": "hourly_temperature_forecast_incomplete",
                }
            )
        model_status = (
            "learning"
            if building.get("learning_progress_percent", 0) < 100
            else "ready"
        )
        plant_assessment = self._plant_assessment(
            data_ready=data_ready, operation=operation, building=building
        )
        for plant_finding in plant_assessment["findings"]:
            issues.append(
                self._issue(
                    code=plant_finding,
                    category="plant",
                    severity="warning",
                    input_keys=[CONF_RUNTIME_ENTITY, CONF_STARTS_ENTITY],
                    action="Check compressor cycling and the assigned counters.",
                )
            )
            anomalies.append(
                {
                    "severity": "warning",
                    "code": plant_finding,
                    "evidence": "validated_runtime_and_start_counters",
                }
            )
        return {
            "available": True,
            "health_score": data_health_score if data_ready else None,
            "health_score_available": data_ready,
            "health_score_reliable": False,
            "health_score_origin": "data_quality",
            "data_health_score": data_health_score if data_ready else None,
            "setup_complete": setup_complete,
            "hydraulics_complete": not hydraulics_setup_incomplete(self._config),
            "data_quality_tier": self._config.get(CONF_DATA_QUALITY_TIER),
            "cop_sink_origin": self._cop_sink_origin(readings),
            "data_ready": data_ready,
            "sensor_quality": (
                "good"
                if data_ready and data_health_score >= 85
                else (
                    "attention"
                    if data_ready and data_health_score >= 60
                    else "critical" if data_ready else "unavailable"
                )
            ),
            "sensor_coverage_percent": self._coverage(readings),
            "sensor_count_available": available_sensor_count,
            "sensor_count_configured": configured_sensor_count,
            "sensor_count_supported": len(readings),
            "sensor_count_unique_sources": unique_source_count,
            "duplicate_assignment_group_count": len(duplicate_assignments),
            "data_gaps": len(unavailable_keys) + len(stale_keys),
            "degraded": bool(
                missing_required_keys
                or unavailable_keys
                or stale_keys
                or duplicate_assignments
                or compressor_signal_conflict
            ),
            "degraded_reasons": [
                *(
                    ["required_sensor_missing"]
                    if missing_required_keys or unavailable_keys
                    else []
                ),
                *(["stale_sensor"] if stale_keys else []),
                *(["duplicate_assignment"] if duplicate_assignments else []),
                *(["compressor_signal_conflict"] if compressor_signal_conflict else []),
            ],
            "sensor_age_seconds": {
                input_key: reading.get("age_seconds")
                for input_key, reading in readings.items()
                if reading.get("entity_id")
            },
            "sensor_report_age_seconds": {
                input_key: reading.get("report_age_seconds")
                for input_key, reading in readings.items()
                if reading.get("entity_id")
            },
            "sensor_update_age_seconds": {
                input_key: reading.get("update_age_seconds")
                for input_key, reading in readings.items()
                if reading.get("entity_id")
            },
            "sensor_freshness": {
                input_key: self._freshness_diagnostic(reading)
                for input_key, reading in readings.items()
                if reading.get("entity_id")
            },
            "sensor_sources": {
                input_key: {
                    "display_name": reading.get("friendly_name")
                    or self._input_details(input_key)["display_name"],
                    "available": bool(reading.get("available")),
                    "raw_state": reading.get("state"),
                    "unit": reading.get("unit"),
                    "reason": reading.get("reason"),
                    "stale": bool(reading.get("stale")),
                    "freshness_at": (
                        freshness_at.isoformat()
                        if isinstance(
                            (freshness_at := reading.get("freshness_at")), datetime
                        )
                        else None
                    ),
                    "age_seconds": reading.get("age_seconds"),
                }
                for input_key, reading in readings.items()
                if reading.get("entity_id")
            },
            "forecast_quality": forecast.get("quality"),
            "model_status": model_status,
            "plant_assessment_reliable": plant_assessment["reliable"],
            "plant_assessment": plant_assessment,
            "readiness": {
                "setup": {
                    "status": "complete" if setup_complete else "incomplete",
                    "complete": setup_complete,
                },
                "data": {
                    "status": (
                        "ready"
                        if data_ready
                        else "unavailable" if not setup_complete else "degraded"
                    ),
                    "ready": data_ready,
                },
                "model": {"status": model_status},
                "plant": {**plant_assessment},
            },
            "drift": self._consumption_drift(now),
            "provider_status": "available",
            "issues": issues,
            "anomalies": anomalies,
            "checks": {
                "required_sensors": setup_complete and required_inputs_usable,
                "timestamps_fresh": not stale_keys,
                "assignments_unique": not duplicate_assignments,
                "forecast_source": bool(forecast.get("hours")),
            },
        }

    @staticmethod
    def _plant_assessment(
        *, data_ready: bool, operation: dict[str, Any], building: dict[str, Any]
    ) -> dict[str, Any]:
        observed_hours = float(building.get("learning_observed_hours") or 0.0)
        missing_requirements = []
        if not data_ready:
            missing_requirements.append("data_sources_ready")
        if observed_hours < _BUILDING_LEARNING_REQUIRED_HOURS:
            missing_requirements.append("observation_24h")
        average_cycle_minutes = operation.get("average_cycle_minutes")
        starts = operation.get("starts")
        if (
            not isinstance(average_cycle_minutes, (int, float))
            or not isinstance(starts, int)
            or starts < 5
        ):
            missing_requirements.append("validated_cycle_metric")
        if missing_requirements:
            return {
                "status": (
                    "data_required"
                    if not data_ready
                    else (
                        "learning"
                        if "observation_24h" in missing_requirements
                        else "operation_data_required"
                    )
                ),
                "reliable": False,
                "score": None,
                "observed_hours": _round(observed_hours, 1),
                "required_hours": _BUILDING_LEARNING_REQUIRED_HOURS,
                "missing_requirements": missing_requirements,
                "findings": [],
                "method": "observed_operation_v1",
            }
        findings = []
        if (
            isinstance(average_cycle_minutes, (int, float))
            and isinstance(starts, int)
            and (starts >= 5)
            and (average_cycle_minutes < 10)
        ):
            findings.append("short_cycling_observed")
        return {
            "status": "cycle_attention" if findings else "cycle_observed",
            "reliable": False,
            "score": None,
            "observed_hours": _round(observed_hours, 1),
            "required_hours": _BUILDING_LEARNING_REQUIRED_HOURS,
            "missing_requirements": [],
            "findings": findings,
            "method": "compressor_cycle_observation_v1",
            "scope": "compressor_cycling_only_not_plant_assessment",
        }

    def _remediation(self, code: str) -> dict[str, str]:
        remediation = dict(
            _REMEDIATIONS.get(
                code,
                {
                    "code": "review_diagnostic_issue",
                    "target": "eai_diagnostics",
                    "instruction": "Prüfe den Diagnosehinweis und die betroffenen Messgrößen.",
                },
            )
        )
        english_instruction = remediation.pop("instruction_en", None)
        if english_instruction and self._language_is_english():
            remediation["instruction"] = english_instruction
        return remediation

    def _issue(
        self,
        *,
        code: str,
        category: str,
        severity: str,
        input_keys: list[str],
        action: str,
        technical_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        affected_inputs = [
            self._input_details(input_key) for input_key in sorted(set(input_keys))
        ]
        issue = {
            "code": code,
            "category": category,
            "severity": severity,
            "status": "open",
            "input_keys": sorted(set(input_keys)),
            "affected_inputs": affected_inputs,
            "display_labels": [
                input_detail["display_name"] for input_detail in affected_inputs
            ],
            "display_names": [
                input_detail["display_name"] for input_detail in affected_inputs
            ],
            "action": action,
            "remediation": self._remediation(code),
            "evidence": [
                input_detail["display_name"] for input_detail in affected_inputs
            ],
        }
        if technical_details:
            issue["technical_details"] = technical_details
        return issue

    def _input_details(self, key: str) -> dict[str, str]:
        display_name = _INPUT_LABELS.get(
            key, key.removesuffix("_entity").replace("_", " ").title()
        )
        return {"key": key, "display_label": display_name, "display_name": display_name}

    def _overview(
        self,
        readings: dict[str, dict[str, Any]],
        forecast: dict[str, Any],
        operation: dict[str, Any],
        efficiency: dict[str, Any],
        building: dict[str, Any],
        energy: dict[str, Any],
        diagnostics: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        optimization = forecast.get("optimization", {})
        actions = []
        if optimization.get("available"):
            actions.append(optimization["recommendation"])
        if diagnostics["anomalies"]:
            actions.append("Offene Punkte im Reiter Einrichtung prüfen")
        if not actions:
            actions.append(
                "Betrieb weiter beobachten; aktuell ist kein Eingriff erforderlich"
            )
        readiness_summary = (
            "Einrichtung noch nicht vollständig. "
            if not diagnostics.get("setup_complete")
            else (
                "Datengrundlage noch nicht ausreichend. "
                if not diagnostics.get("data_ready")
                else "Datengrundlage vollständig. "
            )
        )
        forecast_hours = forecast.get("hours", [])
        local_timezone = self._local_timezone()
        today = now.astimezone(local_timezone).date()
        customer_readiness = self._customer_readiness(diagnostics, building)
        return {
            "available": True,
            "current_power_kw": operation.get("current_power_kw"),
            "energy_today_kwh": efficiency.get("electric_kwh"),
            "expected_today_kwh": _round(
                self._forecast_energy_for_day(forecast_hours, today, local_timezone)
            ),
            "expected_tomorrow_kwh": _round(
                self._forecast_energy_for_day(
                    forecast_hours, today + timedelta(days=1), local_timezone
                )
            ),
            "pv_coverage_percent": energy.get("pv_coverage_percent"),
            "expected_grid_import_kwh": energy.get("expected_grid_import_kwh"),
            "health_score": diagnostics.get("health_score"),
            "why_now": operation.get("why_now"),
            "briefing": {
                "headline": operation.get("why_now", {}).get("headline"),
                "summary": f"{readiness_summary}Gebäudemodell {building.get('learning_progress_percent')} % angelernt.",
                "actions": actions,
                "origin": "live_rule_based_briefing",
            },
            "opportunities": [optimization] if optimization.get("available") else [],
            "setup_complete": customer_readiness["setup_complete"],
            "readiness": customer_readiness,
            **self._overview_data_quality(readings, forecast, efficiency),
        }

    def _customer_readiness(
        self, diagnostics: dict[str, Any], building: dict[str, Any]
    ) -> dict[str, Any]:
        """Status a customer can use without the diagnostics entitlement."""
        readiness = diagnostics.get("readiness")
        readiness = readiness if isinstance(readiness, dict) else {}
        setup_readiness = (
            readiness.get("setup") if isinstance(readiness.get("setup"), dict) else {}
        )
        data_readiness = (
            readiness.get("data") if isinstance(readiness.get("data"), dict) else {}
        )
        model_readiness = (
            readiness.get("model") if isinstance(readiness.get("model"), dict) else {}
        )
        plant_readiness = (
            readiness.get("plant") if isinstance(readiness.get("plant"), dict) else {}
        )

        def flag(source: dict[str, Any], key: str) -> bool | None:
            value = source.get(key)
            return value if isinstance(value, bool) else None

        def text(source: dict[str, Any], key: str) -> str | None:
            value = source.get(key)
            return value if isinstance(value, str) and value else None

        def number(value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            numeric_value = float(value)
            if not isfinite(numeric_value):
                return
            return numeric_value

        setup_complete = flag(setup_readiness, "complete")
        if setup_complete is None and isinstance(
            diagnostics.get("setup_complete"), bool
        ):
            setup_complete = diagnostics["setup_complete"]
        missing_requirements = []
        for requirement in plant_readiness.get("missing_requirements") or []:
            if (
                isinstance(requirement, str)
                and requirement.isidentifier()
                and (requirement == requirement.lower())
                and ("_entity" not in requirement)
                and (requirement not in missing_requirements)
            ):
                missing_requirements.append(requirement)
        missing_inputs = []
        for issue in diagnostics.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("code") != "not_configured":
                continue
            for raw_display_label in issue.get("display_names") or []:
                display_label = _customer_visible_label(raw_display_label)
                if display_label and display_label not in missing_inputs:
                    missing_inputs.append(display_label)
        learning_progress_percent = number(building.get("learning_progress_percent"))
        if learning_progress_percent is not None:
            learning_progress_percent = max(0.0, min(100.0, learning_progress_percent))
        return {
            "setup": {
                "status": text(setup_readiness, "status"),
                "complete": setup_complete,
            },
            "data": {
                "status": text(data_readiness, "status"),
                "ready": flag(data_readiness, "ready"),
            },
            "model": {"status": text(model_readiness, "status")},
            "plant": {
                "status": text(plant_readiness, "status"),
                "reliable": flag(plant_readiness, "reliable"),
                "observed_hours": number(plant_readiness.get("observed_hours")),
                "required_hours": number(plant_readiness.get("required_hours")),
                "missing_requirements": missing_requirements,
            },
            "learning_progress_percent": learning_progress_percent,
            "missing_inputs": missing_inputs,
            "setup_complete": setup_complete,
        }

    def _local_timezone(self):
        timezone_name = getattr(getattr(self.hass, "config", None), "time_zone", None)
        try:
            return ZoneInfo(timezone_name) if timezone_name else timezone.utc
        except ZoneInfoNotFoundError:
            return timezone.utc

    @staticmethod
    def _forecast_energy_for_day(
        points: list[dict[str, Any]], target_day: Any, local_timezone: Any
    ) -> float:
        day_start = datetime.combine(
            target_day, datetime.min.time(), tzinfo=local_timezone
        )
        day_end = day_start + timedelta(days=1)
        total_energy_kwh = 0.0
        for forecast_point in points:
            try:
                interval_start = datetime.fromisoformat(
                    str(forecast_point["interval_start"]).replace("Z", "+00:00")
                )
                interval_end = datetime.fromisoformat(
                    str(forecast_point["interval_end"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if interval_start.tzinfo is None:
                interval_start = interval_start.replace(tzinfo=timezone.utc)
            if interval_end.tzinfo is None:
                interval_end = interval_end.replace(tzinfo=timezone.utc)
            overlap_start = max(interval_start, day_start)
            overlap_end = min(interval_end, day_end)
            overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600
            interval_hours = (interval_end - interval_start).total_seconds() / 3600
            if overlap_hours <= 0 or interval_hours <= 0:
                continue
            heat_pump_kwh = forecast_point.get("heat_pump_kwh")
            if isinstance(heat_pump_kwh, (int, float)) and isfinite(heat_pump_kwh):
                total_energy_kwh += (
                    max(0.0, float(heat_pump_kwh)) * overlap_hours / interval_hours
                )
                continue
            forecast_kw = forecast_point.get("forecast_kw")
            if not isinstance(forecast_kw, (int, float)):
                continue
            if not isfinite(forecast_kw):
                continue
            total_energy_kwh += max(0.0, float(forecast_kw)) * overlap_hours
        return total_energy_kwh

    @staticmethod
    def _forecast_energy_kwh(points: list[dict[str, Any]]) -> float:
        total_energy_kwh = 0.0
        for forecast_point in points:
            heat_pump_kwh = forecast_point.get("heat_pump_kwh")
            if isinstance(heat_pump_kwh, (int, float)) and isfinite(heat_pump_kwh):
                total_energy_kwh += max(0.0, float(heat_pump_kwh))
                continue
            forecast_kw = forecast_point.get("forecast_kw")
            interval_hours = forecast_point.get("interval_hours")
            if not isinstance(forecast_kw, (int, float)):
                continue
            if not isfinite(forecast_kw):
                continue
            if not isinstance(interval_hours, (int, float)):
                continue
            if not isfinite(interval_hours):
                continue
            if not interval_hours > 0:
                continue
            total_energy_kwh += max(0.0, float(forecast_kw)) * float(interval_hours)
            continue
        return total_energy_kwh

    @staticmethod
    def _coverage(readings: dict[str, dict[str, Any]]) -> int:
        configured_readings = [
            reading for reading in readings.values() if reading.get("entity_id")
        ]
        if not configured_readings:
            return 0
        available_count = sum(
            (
                reading.get("available") is True and reading.get("stale") is False
                for reading in configured_readings
            )
        )
        return round(available_count / len(configured_readings) * 100)

    @classmethod
    def _overview_data_quality(
        cls,
        readings: dict[str, dict[str, Any]],
        forecast: dict[str, Any],
        efficiency: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Describe all usable EAI inputs, including shared STATS/SFML context."""
        configured_readings = [
            reading for reading in readings.values() if reading.get("entity_id")
        ]
        quality_scores = []
        quality_origins = []
        sensor_coverage_percent = None
        if configured_readings:
            sensor_coverage_percent = cls._coverage(readings)
            quality_scores.append(float(sensor_coverage_percent))
            quality_origins.append("configured_eai_sensors")
        forecast_hours = forecast.get("hours")
        if isinstance(forecast_hours, list) and forecast_hours:
            valid_pv_hours = sum(
                (
                    isinstance(forecast_point, dict)
                    and (not isinstance(forecast_point.get("pv_forecast_kwh"), bool))
                    and isinstance(forecast_point.get("pv_forecast_kwh"), (int, float))
                    and isfinite(float(forecast_point["pv_forecast_kwh"]))
                    and (0 <= float(forecast_point["pv_forecast_kwh"]) <= 1000)
                    for forecast_point in forecast_hours
                )
            )
            pv_coverage_percent = valid_pv_hours / len(forecast_hours) * 100
            energy_context_quality_scores = [
                float(energy_context_quality_percent)
                for forecast_point in forecast_hours
                if isinstance(forecast_point, dict)
                if not isinstance(
                    (
                        energy_context_quality_percent := forecast_point.get(
                            "energy_context_quality_percent"
                        )
                    ),
                    bool,
                )
                if isinstance(energy_context_quality_percent, (int, float))
                if isfinite(float(energy_context_quality_percent))
                if 0 <= float(energy_context_quality_percent) <= 100
            ]
            forecast_quality_scores = [pv_coverage_percent]
            if energy_context_quality_scores:
                forecast_quality_scores.append(
                    sum(energy_context_quality_scores)
                    / len(energy_context_quality_scores)
                )
            quality_scores.append(
                sum(forecast_quality_scores) / len(forecast_quality_scores)
            )
            quality_origins.append(
                "stats_sfml_shared_context"
                if energy_context_quality_scores
                else "sfml_forecast"
            )
        cop_sink_origin = None
        if isinstance(efficiency, dict):
            cop_sink_origin = efficiency.get("cop_sink_origin")
        if cop_sink_origin:
            quality_origins.append(f"cop_sink_{cop_sink_origin}")
        data_quality_percent = (
            round(sum(quality_scores) / len(quality_scores)) if quality_scores else None
        )
        return {
            "data_quality_percent": data_quality_percent,
            "data_quality_origin": (
                "+".join(quality_origins) if quality_origins else "unavailable"
            ),
            "sensor_coverage_percent": sensor_coverage_percent,
            "cop_sink_origin": cop_sink_origin,
        }
