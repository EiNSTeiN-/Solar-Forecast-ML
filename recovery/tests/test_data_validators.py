"""Paired dictionary/database validation with only owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Every full module is proved before its class is instantiated; no HA imports.
"""
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict
import unittest

from pure_reference import classes
from test_isolated_initialization import logger_for


MODULES = ('data/data_validator.py',
           'extra_features/solar_forecast_eai/data/data_validator.py')
NOW = datetime(2026, 10, 12, 12)


class OwnedDataIO:
    def __init__(self, hass, db):
        self.hass, self.db = hass, db

    async def fetch_one(self, *arguments):
        return await self.db.fetch_one(*arguments)


def outcome(awaitable):
    try:
        return ('return', asyncio.run(awaitable))
    except (Exception, asyncio.CancelledError) as error:
        return ('error', type(error).__name__, str(error))


class DataValidatorTests(unittest.TestCase):
    def validator(self, relative, index, events, rows=(), weights=None, failure=None):
        pending = iter(rows)
        async def fetch(*arguments):
            events.append(('fetch', arguments))
            if failure:
                raise failure('owned database failure')
            return next(pending)
        async def get_weights():
            events.append('weights')
            if failure:
                raise failure('owned weights failure')
            return copy.deepcopy(weights)
        cls = classes(relative, 'DataValidator', {
            '_LOGGER': logger_for(events), 'Any': Any, 'Dict': Dict,
            'HomeAssistant': object, 'DatabaseManager': object,
            'DataManagerIO': OwnedDataIO, 'DATA_VERSION': 'owned-version',
            'MIN_TRAINING_DATA_POINTS': 10,
            'datetime': SimpleNamespace(now=lambda: NOW, fromisoformat=datetime.fromisoformat),
            'SafeDateTimeUtil': SimpleNamespace(now=lambda: NOW),
        }, bases=(OwnedDataIO,))[index]
        return cls(object(), SimpleNamespace(fetch_one=fetch, get_model_weights=get_weights))

    def test_dictionary_validation_keeps_bounds_warnings_and_type_errors(self):
        cases = (
            ('validate_prediction_data', ({}, {'timestamp': 'day', 'prediction_kwh': 0, 'confidence': 0},
                {'timestamp': 'day', 'prediction_kwh': -1, 'confidence': 1},
                {'timestamp': 'day', 'prediction_kwh': 1, 'confidence': 1.01},
                {'timestamp': 'day', 'prediction_kwh': 'bad', 'confidence': 0.5})),
            ('validate_sample_data', ({}, {'timestamp': 'day', 'actual_power': None, 'features': {}},
                {'timestamp': 'day', 'actual_power': 'unvalidated', 'features': []},
                {'timestamp': 'day', 'actual_power': 1, 'features': {'hour': 12, 'temperature': 0, 'cloud_cover': 0}})),
            ('validate_model_state', ({}, {'version': 'owned-version', 'model_loaded': False, 'training_samples': 0},
                {'version': 'old-version', 'model_loaded': True, 'training_samples': -1},
                {'version': 'old-version', 'model_loaded': True, 'training_samples': 'bad'})),
            ('validate_daily_forecast', ({}, {'date': '2026-10-12', 'prediction_kwh': 0},
                {'date': '', 'prediction_kwh': None}, {'date': 'invalid', 'prediction_kwh': 1},
                {'date': '2026-10-12', 'prediction_kwh': -1},
                {'date': '2026-10-12', 'prediction_kwh': 'bad'})),
        )
        for relative in MODULES:
            for method, values in cases:
                for value in values:
                    paired = []
                    for index in (0, 1):
                        events = []
                        validator = self.validator(relative, index, events)
                        data = copy.deepcopy(value)
                        paired.append((outcome(getattr(validator, method)(data)), data, events))
                    self.assertEqual(paired[1], paired[0])
                    self.assertEqual(paired[1][1], value)

    def test_quality_queries_thresholds_failures_and_cancellation(self):
        for relative in MODULES:
            for rows, failure in (((None,), None), (((0,),), None),
                                  (((9,), (9,)), None), (((10,), (8,)), None),
                                  (((10,), (9,)), None), (((10,), (0,)), None),
                                  (((10,), ('bad',)), None), ((), RuntimeError),
                                  ((), asyncio.CancelledError)):
                paired = []
                for index in (0, 1):
                    events = []
                    validator = self.validator(relative, index, events, rows, failure=failure)
                    paired.append((outcome(validator.check_data_quality()), events))
                self.assertEqual(paired[1], paired[0])
                if failure is asyncio.CancelledError:
                    self.assertEqual(paired[1][0][:2], ('error', 'CancelledError'))
                elif failure is None and rows == ((10,), (8,)):
                    report = paired[1][0][1]
                    self.assertTrue(report['sufficient_for_training'])
                    self.assertEqual(report['quality_score'], 0.8)
                    self.assertEqual(len(report['issues']), 1)

    def test_weather_learning_and_summary_database_bounds(self):
        cases = (
            ('validate_weather_forecast_in_db', ('2026-10-12', 12), (
                None, (None, None, None, None), (-50, 0, 0, 100),
                (60, 100, 100, 0), (-51, 101, -1, -1), (0,), ('bad', 0, 0, 0))),
            ('validate_learning_config_in_db', (), (
                None, ('v', 0, 1, 0.9, 7), ('v', None, None, None, None),
                ('v', -0.1, 1.1, 0.9, 7), ('v',))),
            ('validate_daily_summary', ('2026-10-12',), (
                None, (0, 0, -100, 0), (1, 1, 200, 24), (None, None, None, None),
                (-1, -2, 201, 25), (0,), ('bad', 0, 0, 0))),
        )
        for relative in MODULES:
            for method, arguments, rows in cases:
                for row, failure in [(row, None) for row in rows] + [
                        (None, RuntimeError), (None, asyncio.CancelledError)]:
                    paired = []
                    for index in (0, 1):
                        events = []
                        validator = self.validator(relative, index, events, (row,), failure=failure)
                        paired.append((outcome(getattr(validator, method)(*arguments)), events))
                    self.assertEqual(paired[1], paired[0])
                    if row is None and failure is None:
                        self.assertFalse(paired[1][0][1]['exists'])
                        self.assertEqual(len(paired[1][0][1]['issues']), 1)

    def test_weight_fields_attention_empty_arrays_and_failure_routes(self):
        complete = {field: [1] for field in ('Wf', 'Wi', 'Wc', 'Wo', 'bf', 'bi', 'bc', 'bo', 'Wy', 'by')}
        complete.update(input_size=3, hidden_size=8)
        attentive = dict(complete, has_attention=True)
        full_attention = dict(attentive, **{field: [1] for field in
            ('W_query', 'W_key', 'W_value', 'W_attn_out', 'b_attn_out')})
        for relative in MODULES:
            for weights, failure in [(value, None) for value in
                    (None, {}, {'Wf': []}, complete, dict(complete, Wo=[]), attentive, full_attention)] + [
                    (None, RuntimeError), (None, asyncio.CancelledError)]:
                paired = []
                for index in (0, 1):
                    events = []
                    validator = self.validator(relative, index, events, weights=weights, failure=failure)
                    paired.append((outcome(validator.validate_model_weights_in_db()), events))
                self.assertEqual(paired[1], paired[0])
                if weights is complete:
                    report = paired[1][0][1]
                    self.assertTrue(report['valid'])
                    self.assertEqual(report['model_info']['sequence_length'], 24)
                    self.assertFalse(report['has_attention'])
                if weights is attentive:
                    self.assertEqual(len(paired[1][0][1]['issues']), 5)

    def test_report_order_optional_absence_clock_and_cancellation(self):
        for relative in MODULES:
            for quality, learning_exists, learning_valid, weights_exist, weights_valid in (
                (True, False, False, False, False), (True, True, False, False, False),
                (True, False, False, True, False), (True, True, True, True, True),
                (False, False, False, False, False),
            ):
                paired = []
                for index in (0, 1):
                    events = []
                    validator = self.validator(relative, index, events)
                    async def quality_check():
                        events.append('quality')
                        return {'sufficient_for_training': quality}
                    async def learning_check():
                        events.append('learning')
                        return {'exists': learning_exists, 'valid': learning_valid}
                    async def weights_check():
                        events.append('weights')
                        return {'exists': weights_exist, 'valid': weights_valid}
                    validator.check_data_quality = quality_check
                    validator.validate_learning_config_in_db = learning_check
                    validator.validate_model_weights_in_db = weights_check
                    paired.append((outcome(validator.get_validation_report()), events))
                self.assertEqual(paired[1], paired[0])
                report = paired[1][0][1]
                self.assertEqual(report['timestamp'], NOW.isoformat())
                self.assertEqual(report['overall_valid'], quality and
                                 (not learning_exists or learning_valid) and
                                 (not weights_exist or weights_valid))
                self.assertEqual(paired[1][1][-3:], ['quality', 'learning', 'weights'])
            paired = []
            for index in (0, 1):
                events = []
                validator = self.validator(relative, index, events)
                async def cancelled(): raise asyncio.CancelledError('owned report cancellation')
                validator.check_data_quality = cancelled
                paired.append((outcome(validator.get_validation_report()), events))
            self.assertEqual(paired[1], paired[0])
            self.assertEqual(paired[1][0][:2], ('error', 'CancelledError'))


if __name__ == '__main__':
    unittest.main()
