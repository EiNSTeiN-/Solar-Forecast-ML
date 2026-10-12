"""Paired diagnostic sensor properties with owned coordinator and clock data.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration, native runtime, forecast service or database is imported.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
import unittest

from pure_reference import classes, functions

WEATHER = "extra_features/weather_fusion_ai/sensor.py"
DIAGNOSTIC = "sensors/sensor_diagnostic.py"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ENTRY = SimpleNamespace(entry_id="owned", data={"location_name": "Owned location"})


class Base:
    def __init__(self, coordinator, entry=None, *args):
        self.coordinator = coordinator
        self.hass = getattr(coordinator, "hass", None)

    @property
    def db_manager(self):
        return getattr(self.coordinator, "db_manager", None)


class Entity:
    pass


class Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class WeatherSensorTests(unittest.TestCase):
    def pair(self, name, bases=(Base,)):
        return classes(
            WEATHER,
            name,
            {
                "datetime": datetime,
                "timedelta": timedelta,
                "Any": Any,
                "WeatherFusionCoordinator": object,
                "ConfigEntry": object,
                "LEARNING_MIN_DAYS": 3,
                "CONF_LOCATION_NAME": "location_name",
                "DOMAIN": "owned_weather",
                "NAME": "Owned Weather",
                "VERSION": "owned-version",
                "SensorStateClass": SimpleNamespace(MEASUREMENT="measurement"),
                "_LOGGER": Logger(),
            },
            bases=bases,
        )

    def test_status_calibration_accuracy_learning_phase_and_metadata(self):
        pairs = {
            name: self.pair(name)
            for name in (
                "StatusSensor",
                "ForecastAccuracySensor",
                "LearningProgressSensor",
                "ActiveSourcesSensor",
            )
        }
        for days in (0, 2, 3, 4):
            for error in (None, "owned fault"):
                outputs = []
                for side in (0, 1):
                    coordinator = SimpleNamespace(
                        learning_stats={
                            "learning_days": days,
                            "learning_hours": days * 24,
                            "error": error,
                            "calibration_complete": days >= 3,
                            "accuracy_percent": 87.654,
                            "active_sources": 2,
                        },
                        current_weather={},
                    )
                    status = pairs["StatusSensor"][side](coordinator, ENTRY)
                    accuracy = pairs["ForecastAccuracySensor"][side](coordinator, ENTRY)
                    progress = pairs["LearningProgressSensor"][side](coordinator, ENTRY)
                    sources = pairs["ActiveSourcesSensor"][side](coordinator, ENTRY)
                    self.assertEqual(
                        status.native_value,
                        "error" if error else "learning" if days < 3 else "active",
                    )
                    self.assertEqual(accuracy.native_value, None if days < 3 else 87.7)
                    self.assertEqual(sources.native_value, 2)
                    outputs.append(
                        (
                            status.native_value,
                            status.icon,
                            status.extra_state_attributes,
                            accuracy.native_value,
                            accuracy.extra_state_attributes,
                            progress.native_value,
                            progress.extra_state_attributes,
                            sources.native_value,
                            sources.extra_state_attributes,
                        )
                    )
                self.assertEqual(outputs[1], outputs[0])
        for cls in self.pair("WeatherFusionBaseSensor", bases=(Base, Entity)):
            sensor = cls(SimpleNamespace(), ENTRY, "owned_type", "Owned name")
            self.assertEqual(sensor._attr_unique_id, "owned_owned_type")
            self.assertEqual(
                sensor._attr_device_info["identifiers"], {("owned_weather", "owned")}
            )
            self.assertEqual(sensor._attr_device_info["sw_version"], "owned-version")

    def test_learning_dates_clouds_blend_attributes_and_invalid_accuracy(self):
        pairs = {
            name: self.pair(name)
            for name in (
                "LastLearningSensor",
                "CloudTypeSensor",
                "ActiveSourcesSensor",
                "ForecastAccuracySensor",
            )
        }
        for timestamp in (
            None,
            "bad",
            4,
            "2026-10-12T08:00:00",
            "2026-10-12T08:00:00+00:00",
        ):
            outputs = []
            for side in (0, 1):
                coordinator = SimpleNamespace(
                    learning_stats={"last_learning": timestamp},
                    time_zone=timezone.utc,
                    current_weather={
                        "cloud_coverage": 0,
                        "blend_info": {
                            "cloud_type": "owned cloud",
                            "experts_used": ["owned"],
                            "weights": {"owned": 1},
                        },
                    },
                )
                sensors = [
                    pairs[name][side](coordinator, ENTRY)
                    for name in (
                        "LastLearningSensor",
                        "CloudTypeSensor",
                        "ActiveSourcesSensor",
                    )
                ]
                outputs.append(
                    [
                        (sensor.native_value, sensor.extra_state_attributes)
                        for sensor in sensors
                    ]
                )
            self.assertEqual(outputs[1], outputs[0])
        for accuracy in (None, 0, "bad"):
            outputs = []
            for cls in pairs["ForecastAccuracySensor"]:
                sensor = cls(
                    SimpleNamespace(
                        learning_stats={
                            "calibration_complete": True,
                            "accuracy_percent": accuracy,
                        }
                    ),
                    ENTRY,
                )
                try:
                    result = sensor.native_value
                except ValueError as error:
                    result = (type(error).__name__, str(error))
                outputs.append(result)
            self.assertEqual(outputs[1], outputs[0])


class DiagnosticSensorTests(unittest.TestCase):
    def pair(self, name):
        self.clock = SimpleNamespace(
            now=lambda: NOW, get_default_time_zone=lambda: timezone.utc
        )
        shared = {
            "Any": Any,
            "Dict": Dict,
            "Optional": Optional,
            "datetime": datetime,
            "timedelta": timedelta,
            "dt_util": self.clock,
            "_LOGGER": Logger(),
            "_GATE_STATUS_TEMPLATES": {"de": {}, "en": {}},
        }
        names = (
            "_ai_snapshot",
            "_primary_ai_model",
            "_latest_ai_model",
            "_published_dev_mode_active",
            "_diagnostic_datetime",
        )
        # Whole-module proof covers these helper trees. Use one proved helper
        # set as a dependency for each class pair, with owned clock/HASS data.
        helpers = functions(DIAGNOSTIC, names, shared)[0]
        return classes(
            DIAGNOSTIC,
            name,
            {
                **shared,
                **helpers,
                "ConfigEntry": object,
                "SolarForecastMLCoordinator": object,
                "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
                "SensorDeviceClass": SimpleNamespace(TIMESTAMP="timestamp"),
                "SensorStateClass": SimpleNamespace(MEASUREMENT="measurement"),
                "UnitOfEnergy": SimpleNamespace(KILO_WATT_HOUR="kWh"),
                "DAILY_UPDATE_HOUR": 7,
                "UPDATE_INTERVAL": timedelta(minutes=5),
                "format_time_ago": lambda value: "owned age",
                "_gate_status_text": lambda kind, owner, **values: (kind, values),
            },
            bases=(Base,),
        )

    def test_workflow_duration_copying_progress_and_error_redaction(self):
        pair = self.pair("WorkflowStatusSensor")
        for running in (False, True):
            for start in (None, "bad", (NOW - timedelta(seconds=12.34)).isoformat()):
                source = {
                    "is_running": running,
                    "workflow": "owned flow",
                    "started_at": start,
                    "duration_seconds": 4,
                    "steps_completed": 2,
                    "total_steps": 5,
                    "last_error": "private error",
                }
                outputs = []
                for cls in pair:
                    original = deepcopy(source)
                    sensor = cls(SimpleNamespace(active_workflow=original), ENTRY)
                    attributes = sensor.extra_state_attributes
                    self.assertEqual(original, source)
                    self.assertEqual(attributes["progress"], "2/5")
                    self.assertEqual(attributes["last_error_code"], "workflow_error")
                    self.assertNotIn("private error", str(attributes))
                    outputs.append((sensor.native_value, sensor.icon, attributes))
                self.assertEqual(outputs[1], outputs[0])

    def test_update_timestamps_health_thresholds_and_persisted_strings(self):
        health = self.pair("CoordinatorHealthSensor")
        update = self.pair("LastCoordinatorUpdateSensor")
        for elapsed in (0, 449, 450, 899, 900):
            timestamp = NOW - timedelta(seconds=elapsed)
            outputs = []
            for side in (0, 1):
                coordinator = SimpleNamespace(
                    last_update_success_time=timestamp,
                    last_update_success=True,
                    last_update="2026-10-12T11:00:00Z",
                    update_interval=timedelta(minutes=5),
                )
                sensor = health[side](coordinator, ENTRY)
                expected = (
                    "Healthy"
                    if elapsed < 450
                    else "Delayed" if elapsed < 900 else "Stale"
                )
                self.assertEqual(sensor.native_value, expected)
                last_update = update[side](coordinator, ENTRY)
                outputs.append(
                    (
                        sensor.native_value,
                        sensor.extra_state_attributes,
                        last_update.native_value,
                        last_update.extra_state_attributes,
                    )
                )
            self.assertEqual(outputs[1], outputs[0])
        for timestamp in (None, "persisted", NOW):
            outputs = []
            for cls in update:
                sensor = cls(SimpleNamespace(last_update_success_time=timestamp), ENTRY)
                outputs.append((sensor.native_value, sensor.extra_state_attributes))
            self.assertEqual(outputs[1], outputs[0])

    def test_ai_gates_keep_public_privacy_and_dev_candidate_deadlines(self):
        pairs = {
            name: self.pair(name)
            for name in (
                "AIGateStatusSensor",
                "MLMetricsSensor",
                "LastMLTrainingSensor",
            )
        }
        for active in (False, True):
            for serving in (False, True):
                snapshot = {
                    "serving": (
                        {"candidate_id": "owned serving", "r2_score": 0.9}
                        if serving
                        else None
                    ),
                    "pending": {
                        "candidate_id": "owned pending",
                        "r2_score": 0.8,
                        "trained_at": "2026-10-10T08:00:00",
                    },
                    "gate": {
                        "candidate_created_at": "2026-10-10T08:00:00",
                        "calendar_day_limit": 7,
                        "eligible_days": 2,
                        "required_days": 7,
                    },
                }
                outputs = []
                for side in (0, 1):
                    coordinator = SimpleNamespace(
                        ai_diagnostic_snapshot=deepcopy(snapshot),
                        hass=SimpleNamespace(
                            data={"solar_forecast_ml": {"dev_mode": {"active": active}}}
                        ),
                    )
                    gate = pairs["AIGateStatusSensor"][side](coordinator, ENTRY)
                    metrics = pairs["MLMetricsSensor"][side](coordinator, ENTRY)
                    training = pairs["LastMLTrainingSensor"][side](coordinator, ENTRY)
                    if not active:
                        self.assertEqual(
                            gate.extra_state_attributes,
                            {
                                "status": (
                                    "forecast_active"
                                    if serving
                                    else "forecast_learning"
                                )
                            },
                        )
                    else:
                        self.assertEqual(
                            gate.extra_state_attributes["candidate_age_days"], 2
                        )
                        self.assertEqual(
                            gate.extra_state_attributes["decision_deadline"],
                            "2026-10-17T08:00:00+00:00",
                        )
                    self.assertEqual(
                        training.native_value,
                        datetime(2026, 10, 10, 8, tzinfo=timezone.utc),
                    )
                    outputs.append(
                        (
                            gate.native_value,
                            gate.extra_state_attributes,
                            metrics.native_value,
                            metrics.extra_state_attributes,
                            training.native_value,
                            training.extra_state_attributes,
                        )
                    )
                self.assertEqual(outputs[1], outputs[0])

    def test_schedule_boundaries_and_sunday_training_match_original(self):
        pair = self.pair("NextScheduledUpdateSensor")
        for now in (
            NOW,
            NOW.replace(hour=0, minute=9),
            NOW.replace(hour=0, minute=10),
            NOW.replace(hour=7, minute=0),
            NOW.replace(hour=23, minute=31),
            NOW - timedelta(days=1, hours=10),
        ):
            self.clock.now = lambda: now
            outputs = []
            for cls in pair:
                sensor = cls(SimpleNamespace(), ENTRY)
                attributes = sensor.extra_state_attributes
                self.assertGreater(
                    datetime.fromisoformat(attributes["next_update_time_iso"]), now
                )
                self.assertEqual(attributes["is_sunday"], now.weekday() == 6)
                outputs.append((sensor.native_value, attributes))
            self.assertEqual(outputs[1], outputs[0])
