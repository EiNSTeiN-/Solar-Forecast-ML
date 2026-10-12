"""Paired checks for readable residual-scope modules.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only statically proved classes run, with explicit owned dependencies.
"""

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest

import numpy as np

from pure_reference import classes
from test_isolated_initialization import logger_for


class ResidualScopeModuleTests(unittest.TestCase):
    def test_consumption_store_boundaries_retries_and_atomic_meter_writes(self):
        relative = "extra_features/solar_forecast_eai/data/eai_consumption_sample_store.py"
        for mode in ("valid", "retry", "permanent", "cancelled", "unavailable"):
            outcomes = []
            for index in (0, 1):
                events, calls = [], {}

                async def sleep(seconds):
                    events.append(("sleep", seconds))

                def attempt(operation):
                    calls[operation] = calls.get(operation, 0) + 1
                    if mode == "cancelled":
                        raise asyncio.CancelledError("owned operation")
                    if mode == "permanent":
                        raise ValueError("owned permanent failure")
                    if mode == "retry" and calls[operation] == 1:
                        error = RuntimeError("owned contention")
                        error.retryable = True
                        raise error

                async def fetch(lease, operation, parameters):
                    events.append(("fetch", operation, parameters.copy()))
                    attempt(operation)
                    if operation == "load_writer_state":
                        return [(2.5, "2026-10-12T10:00:00Z", "2026-10-12",
                                 "sensor.owned", "kWh", "owned-boundary")]
                    return [{"feature_snapshot": '{"target_hour":10}'},
                            {"feature_snapshot": "[]"}]

                async def execute(lease, operation, parameters, **options):
                    events.append(("execute", operation, parameters.copy(), options))
                    attempt(operation)

                cls = classes(
                    relative,
                    "EAIConsumptionSampleStore",
                    {
                        "_LOGGER": logger_for(events),
                        "_TRANSIENT_RETRY_DELAYS": (0.5, 1.0, 2.0),
                        "asyncio": SimpleNamespace(sleep=sleep),
                        "datetime": datetime,
                        "UTC": UTC,
                        "json": json,
                    },
                )[index]
                store = cls(SimpleNamespace(), "owned-entry")
                store._lease = "owned-lease"
                store._service = SimpleNamespace(
                    fetch_domain_operation=fetch, execute_domain_operation=execute
                )
                if mode == "unavailable":
                    async def not_ready():
                        return False
                    store._async_ready = not_ready

                async def exercise():
                    results = [
                        await store.async_load_writer_state("sensor.owned", "kWh", "owned-boundary"),
                        await store.async_load_writer_state("sensor.changed", "kWh", "owned-boundary"),
                        await store.async_load_samples("2026-10-01", "2026-10-12"),
                        await store.async_save_writer_state(
                            value=2.5, time_local="2026-10-12T10:00:00+00:00",
                            date_local="2026-10-12", source_entity_id="sensor.owned",
                            source_unit="kWh", boundary_fingerprint="owned-boundary"
                        ),
                        await store.async_store_sample(
                            {"target_date": "2026-10-12", "target_hour": 10,
                             "actual_kwh": 0.5, "flags": {}}, source="owned"
                        ),
                        await store.async_store_meter_sample(
                            date="2026-10-12", hour=10, actual_kwh=0.5,
                            sensor_data={"current_yield_kwh": 2.5},
                            weather_actual={}, astronomy={}, baseline_kwh=2.5,
                            baseline_time_local="2026-10-12T10:00:00+00:00",
                            baseline_date_local="2026-10-12",
                            source_entity_id="sensor.owned", source_unit="kWh",
                            boundary_fingerprint="owned-boundary",
                            interval_start_utc="2026-10-12T09:00:00Z",
                            interval_end_utc="2026-10-12T10:00:00Z"
                        ),
                        await store.async_store_sample(
                            {"target_date": "2026-10-12", "target_hour": 24,
                             "actual_kwh": 0.5}, source="owned"
                        ),
                    ]
                    return results

                try:
                    results = asyncio.run(exercise())
                except asyncio.CancelledError:
                    results = "cancelled"
                outcomes.append((results, events))
            self.assertEqual(outcomes[1], outcomes[0])
            if mode in ("valid", "retry"):
                self.assertEqual(outcomes[1][0][1], None)
                self.assertEqual(outcomes[1][0][2], [{"target_hour": 10}])
                self.assertEqual(outcomes[1][0][3:], [True, True, True, False])
                meter_writes = [event for event in outcomes[1][1]
                                if event[:2] == ("execute", "record_meter_interval")]
                self.assertEqual(len(meter_writes), 2 if mode == "retry" else 1)
                self.assertEqual(meter_writes[-1][2]["baseline_kwh"], 2.5)
                self.assertEqual(meter_writes[-1][2]["actual_kwh"], 0.5)
            elif mode == "cancelled":
                self.assertEqual(outcomes[1][0], "cancelled")
            else:
                self.assertEqual(outcomes[1][0], [None, None, None, False, False, False, False])
                if mode == "unavailable":
                    self.assertEqual(outcomes[1][1], [])

    def test_eai_ridge_multi_output_training_restore_and_cancellation(self):
        relative = "extra_features/solar_forecast_eai/ai/ai_tiny_ridge.py"
        for mode in ("trained", "cancelled", "insufficient"):
            outcomes = []
            for index in (0, 1):
                events = []

                async def sleep(seconds):
                    events.append(("sleep", seconds))
                    if mode == "cancelled":
                        raise asyncio.CancelledError("owned training")

                cls = classes(
                    relative,
                    "TinyRidge",
                    {
                        "_ensure_numpy": lambda: np,
                        "_LOGGER": logger_for(events),
                        "asyncio": SimpleNamespace(sleep=sleep),
                    },
                )[index]
                model = cls(input_size=1, sequence_length=2, num_outputs=2)
                count = 9 if mode == "insufficient" else 24
                sequences = [
                    np.array([[number / 10], [number / 10 + 0.1]])
                    for number in range(count)
                ]
                targets = [
                    [number / 5 + 0.2, number / 10 + 0.1]
                    for number in range(count)
                ]
                try:
                    training = asyncio.run(
                        model.train(sequences, targets, validation_split=0.25)
                    )
                except asyncio.CancelledError:
                    training = "cancelled"
                weights = model.get_weights()
                predictions = errors = None
                if model.is_trained:
                    predictions = [
                        model.predict(sequences[-1]),
                        model.predict_total(sequences[-1]),
                        model.forward(sequences[-1])[0].tolist(),
                        model.predict(np.full((2, 1), float("nan"))),
                    ]
                    errors = []
                    for bad_shape in (np.zeros((3, 1)), np.zeros((2, 2, 1))):
                        try:
                            model.predict(bad_shape)
                        except ValueError as error:
                            errors.append(str(error))
                    restored = cls(input_size=1, sequence_length=2, num_outputs=2)
                    restored.set_weights(weights)
                    self.assertEqual(
                        restored.predict(sequences[-1]), predictions[0]
                    )
                    self.assertEqual(restored.get_weights(), weights)
                outcomes.append(
                    (
                        training,
                        model.is_trained,
                        weights,
                        predictions,
                        errors,
                        model.get_model_size_kb(),
                        events,
                    )
                )
            self.assertEqual(outcomes[1], outcomes[0])
            if mode == "insufficient":
                self.assertFalse(outcomes[1][0]["success"])
                self.assertFalse(outcomes[1][1])
                self.assertIsNone(outcomes[1][2]["weights"])
                self.assertFalse(any(event[0] == "sleep" for event in outcomes[1][-1]))
            else:
                # EAI fits the whole sample set before its final await. Keep
                # that original contract, including state after cancellation.
                self.assertTrue(outcomes[1][1])
                self.assertEqual(outcomes[1][2]["trained_samples"], 24)
                self.assertEqual(outcomes[1][3][3], [0.0, 0.0])
                self.assertEqual(len(outcomes[1][4]), 2)
                if mode == "cancelled":
                    self.assertEqual(outcomes[1][0], "cancelled")
                else:
                    self.assertTrue(outcomes[1][0]["success"])
                    self.assertEqual(outcomes[1][0]["num_outputs"], 2)


if __name__ == "__main__":
    unittest.main()
