"""Behavior comparisons for matched, readable coordinator and filter helpers.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
import copy
from datetime import datetime, timedelta, timezone
import logging
import os
import random
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest

from pure_reference import classes

AVAILABLE = (os.environ.get('SFML_RECOVERED_DIR') and os.environ.get('SFML_UNPACKER_DIR')
             and sys.version_info[:2] == (3, 14))
LOGGER = logging.getLogger('pure_recovery_fixture')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class CoordinatorBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        cls.original, cls.recovered = classes('core/core_coordinator_helpers.py', 'CoordinatorHelpers',
            {'datetime': datetime, 'timedelta': timedelta, 'Any': Any, 'Dict': Dict,
             'Optional': Optional, '_LOGGER': LOGGER, 'dt_util': SimpleNamespace(now=lambda: cls.now)})

    def test_update_schedule_and_staleness_boundaries(self):
        for seconds in (None, -120, 0, 59, 60, 899, 900, 3599, 3600, 21599, 21600, 86400, 90000):
            last = None if seconds is None else self.now - timedelta(seconds=seconds)
            for method in ('calculate_next_update_time', 'should_force_update', 'calculate_data_staleness'):
                with self.subTest(seconds=seconds, method=method):
                    self.assertEqual(getattr(self.recovered, method)(last), getattr(self.original, method)(last))

    def test_required_keys_and_update_summary(self):
        for data in ({}, {'last_update': None}, {'forecasts': {}}, {'last_update': None, 'forecasts': {}}):
            self.assertEqual(self.recovered.validate_coordinator_data(data), self.original.validate_coordinator_data(data))
        for data in ({}, {'a': True}, {'a': False}, {'a': True, 'b': False}, {'a': True, 'b': True}):
            self.assertEqual(self.recovered.format_update_summary(data), self.original.format_update_summary(data))

    def test_merge_preserves_result_and_existing_mutation_behavior(self):
        old = {'forecasts': {'east': 2}, 'retained': 3}
        new = {'forecasts': {'south': 5}, 'added': 7}
        left_old, right_old = copy.deepcopy(old), copy.deepcopy(old)
        left_new, right_new = copy.deepcopy(new), copy.deepcopy(new)
        result = self.original.merge_forecast_data(left_old, left_new)
        self.assertEqual(self.recovered.merge_forecast_data(right_old, right_new), result)
        self.assertEqual(right_old, left_old)
        self.assertEqual(right_new, left_new)


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class KalmanBehavior(unittest.TestCase):
    def test_updates_clamps_adaptation_and_resets_in_both_copies(self):
        globals_ = {'Any': Any, 'Dict': Dict, 'List': List, '_LOGGER': LOGGER}
        for relative in ('data/data_weather_kalman.py',
                         'extra_features/solar_forecast_eai/data/data_weather_kalman.py'):
            original, recovered = classes(relative, 'WeatherKalmanTracker', globals_)
            generator = random.Random(6317)
            residuals = [0.0, -300.0, 300.0, 3.5, -25.0]
            residuals.extend(generator.uniform(-800, 800) for _ in range(40))
            for process in (2.0, 5.0, 15.0):
                for measurement in (20.0, 50.0, 150.0):
                    left, right = original(process, measurement), recovered(process, measurement)
                    self.assertEqual(right.get_state(), left.get_state())
                    for index, residual in enumerate(residuals):
                        with self.subTest(module=relative, process=process, measurement=measurement, index=index):
                            left.update(residual)
                            right.update(residual)
                            self.assertEqual(right.get_state(), left.get_state())
                            if index % 9 == 8:
                                left.reset()
                                right.reset()
                                self.assertEqual(right.get_state(), left.get_state())
                    left.reset()
                    right.reset()
                    self.assertEqual(right.get_state(), left.get_state())
