"""Paired physics, MLP training and repair checks with owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, native runtime or real database is loaded.
"""

import asyncio
from datetime import datetime, timezone
import json
import math
from types import SimpleNamespace
import unittest

import numpy as np

from pure_reference import classes
from test_isolated_initialization import logger_for


class Record(SimpleNamespace):
    def to_dict(self):
        return {key: value.to_dict() if isinstance(value, Record) else value
                for key, value in vars(self).items()}


def geometry(tilt_deg, azimuth_deg):
    return Record(tilt_deg=tilt_deg, azimuth_deg=azimuth_deg,
                  tilt_rad=math.radians(tilt_deg), azimuth_rad=math.radians(azimuth_deg))


def sun(elevation_deg, azimuth_deg):
    return Record(elevation_deg=elevation_deg, azimuth_deg=azimuth_deg,
                  zenith_rad=math.radians(90 - elevation_deg),
                  azimuth_rad=math.radians(azimuth_deg))


class PhysicsModelAndRepairTests(unittest.TestCase):
    def test_engine_geometry_twilight_heat_power_and_weather_fallbacks(self):
        outputs = []
        for index in (0, 1):
            events = []
            cls = classes('physics/physics_engine.py', 'PhysicsEngine', {
                'math': math, '_LOGGER': logger_for(events), 'PanelGeometry': geometry,
                'POAResult': Record, 'PowerResult': Record, 'SunPosition': sun,
                'IrradianceData': lambda **values: Record(**values, is_valid=lambda: True),
                'beam_iam_martin_ruiz': lambda angle: 0.9,
                'poa_irradiance_components': lambda ghi, direct, diffuse, elevation:
                    (float(ghi or 0), float(direct or 0), float(diffuse or 0))})[index]
            engine = cls(4)
            default_confidence = engine.geometry_confidence
            engine.update_geometry(40, 180, 0.8)
            readings = []
            for elevation in (-5, -2, 0, 2, 3, 5, 45):
                for temperature in (-30, 25, 100):
                    for valid in (False, True):
                        irradiance = Record(ghi=600, dni=650, dhi=100,
                                            is_valid=lambda value=valid: value)
                        position = sun(elevation, 180)
                        result = engine.calculate_power_output(irradiance, position, temperature)
                        readings.append(result.to_dict())
            forecasts = [engine.calculate_hourly_forecast(weather, astronomy) for weather, astronomy in (
                ({}, {}), ({'ghi': 600, 'dni': 650, 'dhi': 100, 'temperature': 0},
                            {'elevation_deg': 45, 'azimuth_deg': 0}),
                ({'ghi': 600, 'direct_radiation': 650, 'diffuse_radiation': 100},
                            {'elevation_deg': 45, 'azimuth_deg': 180}))]
            inferred = [engine.estimate_geometry_from_production(sun(elevation, 180), power,
                         Record(ghi=ghi), 15) for elevation, power, ghi in
                        ((45, 1, 600), (0, 1, 600), (45, 0, 600), (45, 1, 10))]
            outputs.append((default_confidence, engine.geometry.to_dict(), engine.geometry_confidence,
                            readings, forecasts, inferred, events))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1][0], 0.0)
        self.assertEqual(outputs[1][2], 0.8)
        self.assertEqual(outputs[1][5][1:], [None, None, None])

    def test_mlp_weights_gradients_training_restore_quality_and_cancellation(self):
        relative = 'extra_features/solar_forecast_eai/ai/ai_weather_mlp.py'
        for cancelled in (False, True):
            outputs = []
            for index in (0, 1):
                events = []

                async def sleep(seconds):
                    events.append(('sleep', seconds))
                    if cancelled:
                        raise asyncio.CancelledError()

                cls = classes(relative, 'TinyWeatherMLP', {
                    '_ensure_numpy': lambda: np, 'math': math,
                    '_LOGGER': logger_for(events), 'asyncio': SimpleNamespace(sleep=sleep)})[index]
                np.random.seed(1729)
                model = cls(hidden1=4, hidden2=3)
                initial = model.get_weights()
                x = np.arange(8, dtype=np.float64) / 10
                prediction, cache = model.forward(x)
                gradients = {key: value.tolist() for key, value in model.backward(cache, 1.1, prediction).items()}
                step_loss = model.train_step(x, 1.1)
                insufficient = asyncio.run(model.train([x] * 3, [1.0] * 3))
                inputs = [x + number / 100 for number in range(50)]
                targets = [0.8 + number / 100 for number in range(50)]
                try:
                    training = asyncio.run(model.train(inputs, targets, epochs=3,
                                                       early_stopping_patience=2))
                except asyncio.CancelledError:
                    training = 'cancelled'
                trained_weights = model.get_weights()
                # Persisted flat bias arrays must still be restored as columns.
                persisted = dict(initial)
                for name in ('b1', 'b2', 'b3'):
                    persisted[name] = np.asarray(persisted[name]).reshape(-1).tolist()
                model.set_weights(persisted)
                predictions = [model.predict_correction(600, cloud, 45, humidity, pressure, wind, 12)
                               for cloud, humidity, pressure, wind in ((20, None, None, None),
                                                                       (0, 0, 0, 0))]
                quality = []
                for samples, accuracy in ((0, 0.9), (50, 0.4), (50, 0.6)):
                    model.trained_samples = samples; model.training_accuracy = accuracy
                    quality.append(model.is_trained)
                outputs.append((initial, prediction, gradients, step_loss, insufficient, training,
                                trained_weights, model.get_weights(), predictions, quality, events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][9], [False, False, True])
            if cancelled:
                self.assertEqual(outputs[1][5], 'cancelled')
            else:
                self.assertTrue(outputs[1][5]['success'])

    def test_repair_dry_run_acknowledgment_quarantine_and_transaction_failures(self):
        relative = 'services/service_repair_tool.py'
        measured = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        for mode in ('dry', 'live', 'blocked', 'unacknowledged', 'nothing', 'write_error', 'no_db'):
            outputs = []
            for index in (0, 1):
                events = []
                rows = [dict(prediction_id=f'owned-{hour}', target_hour=hour, prediction_kwh=power,
                             actual_kwh=None, manual_override=0, has_sensor_data=0, sensor_data_complete=0)
                        for hour, power in ((10, 1), (11, 2))]

                class Transaction:
                    async def __aenter__(self):
                        events.append(('begin',)); return self
                    async def __aexit__(self, error_type, error, traceback):
                        events.append(('end', error_type.__name__ if error_type else None))

                class Database:
                    def transaction(self): return Transaction()
                    async def is_actual_backfill_blocked(self, target_date, hour):
                        events.append(('quarantine', target_date, hour)); return mode == 'blocked'
                    async def fetchone(self, sql, parameters=()):
                        events.append(('one', sql, parameters)); return [4 if mode == 'nothing' else 1]
                    async def fetchall(self, sql, parameters=()):
                        events.append(('all', sql, parameters))
                        if 'BETWEEN' in sql: return rows
                        if 'SUM(ppg.actual_kwh)' in sql:
                            return [dict(group_name='east', actual_sum=30), dict(group_name='west', actual_sum=70)]
                        if 'WHERE prediction_id' in sql:
                            return [dict(group_name='east', prediction_kwh=1), dict(group_name='west', prediction_kwh=2)]
                        return [dict(target_hour=9, actual_kwh=1), dict(target_hour=10, actual_kwh=None),
                                dict(target_hour=11, actual_kwh=None)]
                    async def execute(self, sql, parameters=(), **options):
                        events.append(('write', sql, parameters, options))
                        if mode == 'write_error': raise RuntimeError('owned write failure')

                class Shadow:
                    def __init__(self, db): events.append(('shadow',))
                    async def evaluate_prediction(self, prediction_id): events.append(('evaluate', prediction_id))

                async def refresh(): events.append(('refresh',))
                coordinator = SimpleNamespace(data_manager=SimpleNamespace(
                    _db_manager=None if mode == 'no_db' else Database()),
                    _refresh_hourly_predictions_cache=refresh,
                    async_update_listeners=lambda: events.append(('listeners',)))
                cls = classes(relative, 'RepairToolService', {
                    'datetime': datetime, 'json': json, '_LOGGER': logger_for(events),
                    'dt_util': SimpleNamespace(now=lambda: measured, ensure_local=lambda value: value.replace(tzinfo=timezone.utc)),
                    'RepairHour': Record, 'EnsembleShadowHandler': Shadow,
                    'OPERATION_DAILY_CUMULATIVE_ACTUAL': 'daily_cumulative_actual',
                    'DEFAULT_DISTRIBUTION': 'forecast_shape',
                    'DEFAULT_GROUP_DISTRIBUTION': 'historical_clean_share',
                    'DEFAULT_LEARNING_POLICY': 'exclude'})[index]
                service = cls(SimpleNamespace(), coordinator)
                data = dict(date='2026-10-11', measured_at=measured.isoformat(), cumulative_kwh=4,
                            start_hour=10, end_hour=11, dry_run=mode != 'live' and mode != 'write_error',
                            acknowledge_live_db_risk=mode != 'unacknowledged')
                if mode == 'unacknowledged': data['dry_run'] = False
                try:
                    result = asyncio.run(service._repair_daily_cumulative_actual(data))
                except (RuntimeError, ValueError) as error:
                    result = (type(error).__name__, str(error))
                guards = []
                for value in (None, '', 'invalid', -1, 0, 1.23456789):
                    try: guards.append(service._parse_positive_float(value, 'owned_field'))
                    except ValueError as error: guards.append((type(error).__name__, str(error)))
                outputs.append((result, guards, events))
            self.assertEqual(outputs[1], outputs[0])
            if mode == 'live': self.assertTrue(outputs[1][0]['written'])
            if mode == 'dry':
                self.assertEqual(sum(row['actual_kwh'] for row in outputs[1][0]['repaired_hours']), 3)
                self.assertFalse(any(event[0] == 'write' for event in outputs[1][2]))


if __name__ == '__main__':
    unittest.main()
