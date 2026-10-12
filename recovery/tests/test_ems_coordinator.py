"""Verified isolated lookup with original rejection, identity and error paths."""
from types import SimpleNamespace
import unittest

from pure_reference import isolated_function


class EMSCoordinatorTests(unittest.TestCase):
    def functions(self):
        return isolated_function(
            'extra_features/sfml_stats/core/ems.py',
            'extra_features/sfml_stats/core/ems_coordinator.py',
            '<module>/EMSManager#1/_gpm_coordinator#1', {'DOMAIN':'solar_forecast_ml'})

    def test_lookup_skips_invalid_instances_and_keeps_false_values(self):
        original, recovered = self.functions()
        for data in ({}, {'solar_forecast_ml':{}},
                     {'solar_forecast_ml':{'a':1, 'b':None}},
                     {'solar_forecast_ml':{'a':{}, 'b':{'gpm_coordinator':None}}},
                     {'solar_forecast_ml':{'a':1, 'b':{'gpm_coordinator':7}}},
                     {'solar_forecast_ml':{'a':{}, 'b':{'gpm_coordinator':0}, 'c':{'gpm_coordinator':9}}},
                     {'solar_forecast_ml':{'a':{'gpm_coordinator':False}, 'b':{'gpm_coordinator':9}}},
                     {'solar_forecast_ml':None}, {'solar_forecast_ml':3}):
            results = []
            for function in (original, recovered):
                instance = SimpleNamespace(hass=SimpleNamespace(data=data))
                try: result = function(instance)
                except AttributeError as error: result = type(error).__name__, str(error)
                results.append(result)
            self.assertEqual(results[1], results[0])


    def test_getter_order_identity_and_unhandled_errors_are_preserved(self):
        original, recovered = self.functions()
        for failure in (False, True):
            results = []
            for function in (original, recovered):
                calls = []
                class Coordinator:
                    def __bool__(self): raise AssertionError('Must not truth-test coordinator')
                coordinator = Coordinator()
                class Row(dict):
                    def __init__(self, label, value): self.label, self.value = label, value
                    def get(self, key):
                        calls.append((self.label, key))
                        if failure and self.label=='result': raise ValueError('owned getter failure')
                        return self.value
                data = {'solar_forecast_ml':{'invalid':object(), 'empty':Row('empty', None),
                                            'result':Row('result', coordinator),
                                            'later':Row('later', 9)}}
                instance = SimpleNamespace(hass=SimpleNamespace(data=data))
                try:
                    result = function(instance)
                    self.assertIs(result, coordinator)
                    result = 'same coordinator'
                except ValueError as error: result = type(error).__name__, str(error)
                results.append((result, calls))
            self.assertEqual(results[1], results[0])


class StatsCoordinatorTests(unittest.TestCase):
    def functions(self, context):
        return isolated_function(
            'extra_features/sfml_stats/api/common.py',
            'extra_features/sfml_stats/api/stats_gpm_coordinator.py',
            '<module>/_get_stats_gpm_coordinator#1',
            {'DOMAIN':'solar_forecast_ml', 'context':context})

    def test_missing_hass_instances_and_false_results(self):
        for data in (None, {}, {'solar_forecast_ml':{}},
                     {'solar_forecast_ml':{'a':1, 'b':None}},
                     {'solar_forecast_ml':{'a':{}, 'b':{'gpm_coordinator':None}}},
                     {'solar_forecast_ml':{'a':1, 'b':{'gpm_coordinator':7}}},
                     {'solar_forecast_ml':{'a':{}, 'b':{'gpm_coordinator':0}, 'c':{'gpm_coordinator':9}}},
                     {'solar_forecast_ml':{'a':{'gpm_coordinator':False}, 'b':{'gpm_coordinator':9}}},
                     {'solar_forecast_ml':None}, {'solar_forecast_ml':3}):
            context = SimpleNamespace(HASS=None if data is None else SimpleNamespace(data=data))
            results = []
            for function in self.functions(context):
                try: result = function()
                except AttributeError as error: result = type(error).__name__, str(error)
                results.append(result)
            self.assertEqual(results[1], results[0])

    def test_context_reads_getter_order_and_unhandled_errors(self):
        for failure in (False, True):
            results = []
            for side in (0,1):
                calls = []
                class Coordinator:
                    def __bool__(self): raise AssertionError('Must not truth-test coordinator')
                coordinator = Coordinator()
                class Row(dict):
                    def __init__(self, label, value): self.label, self.value = label, value
                    def get(self, key):
                        calls.append((self.label, key))
                        if failure and self.label=='result': raise ValueError('owned getter failure')
                        return self.value
                data = {'solar_forecast_ml':{'invalid':object(), 'empty':Row('empty', None),
                                            'result':Row('result', coordinator),
                                            'later':Row('later', 9)}}
                class Context:
                    @property
                    def HASS(self): calls.append('HASS'); return SimpleNamespace(data=data)
                function = self.functions(Context())[side]
                try:
                    result = function()
                    self.assertIs(result, coordinator)
                    result = 'same coordinator'
                except ValueError as error: result = type(error).__name__, str(error)
                results.append((result, calls))
            self.assertEqual(results[1], results[0])
