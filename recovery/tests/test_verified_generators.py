"""Selected complete generator units; no containing module is executed."""
import builtins
import importlib.util
import marshal
import os
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def paired_generators(relative, unit_path, context_name, globals_=None, captures=()):
    """Compare the exact subtree before constructing either generator function."""
    sys.path.insert(0, str(Path(os.environ['SFML_UNPACKER_DIR']) / 'oneshot'))
    from bytecode_fidelity import compare, compile_source, units
    from modern_backend import MarkerNormalizer, normalize_code
    prefix = Path(os.environ['SFML_RECOVERED_DIR']) / (relative + '.1shot')
    data = Path(str(prefix) + '.recovered.pyc').read_bytes()
    if data[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError('Reference interpreter magic does not match')
    reference = dict(units(normalize_code(marshal.loads(data[16:]))))[unit_path]
    source_name = ('verified_annotated_generators.py' if context_name in
                   ('actual_energy_terms', 'coverage_milestone_terms') else 'verified_generators.py')
    candidate = dict(units(compile_source((ROOT / source_name).read_text(), source_name)))[
                                             f'<module>/{context_name}#1/<genexpr>#1']
    metadata = Path(str(prefix) + '.seq').read_bytes()[:34]
    if len(metadata) != 34 or metadata[0] != 0xa1 or metadata[17] != 0xa2:
        raise ValueError('Missing reference metadata')
    proof = compare(reference, candidate, MarkerNormalizer(metadata[1:17], metadata[18:30]))
    if not proof['all_units_match']:
        raise AssertionError(f'{relative}: {unit_path} did not match: {proof}')
    if len(reference.co_freevars) != len(captures) or len(candidate.co_freevars) != len(captures):
        raise ValueError('Supply exactly the verified capture positions')
    return tuple(types.FunctionType(
        code, dict(globals_ or {}, __builtins__=builtins.__dict__),
        closure=tuple(types.CellType(value) for value in captures) or None)
        for code in (reference, candidate))


class VerifiedGeneratorTests(unittest.TestCase):
    def test_production_and_consumption_daily_amounts_keep_filters_and_errors(self):
        modules = ('production/production_scheduled_tasks.py',
                   'extra_features/solar_forecast_eai/consumption/consumption_scheduled_tasks.py')
        scenarios = ([], [(1, 2), (2, 3), (3, 0)], [(1, None), (2, -2), (3, 4)],
                     [(1, ''), (2, 'kept')], [(1,)], [None])
        for relative in modules:
            for index in (2, 4):
                original, recovered = paired_generators(
                    relative, f'<module>/ScheduledTasksManager#1/_calculate_stats_internal#1/<genexpr>#{index}',
                    'daily_amount_terms')
                for rows in scenarios:
                    results = []
                    for function in (original, recovered):
                        try: result = tuple(function(iter(rows)))
                        except (IndexError, TypeError) as error: result = type(error).__name__, str(error)
                        results.append(result)
                    self.assertEqual(results[1], results[0])

    def test_actual_energy_terms_keep_two_getters_and_single_truth_test(self):
        original, recovered = paired_generators(
            'extra_features/sfml_stats/api/common.py',
            '<module>/_build_live_today_clean_eval#1/<genexpr>#2', 'actual_energy_terms')
        for amounts in ([], [None, 0, 2, -3], [False, True, '', 'kept'],
                        [('owned false', False), ('owned true', True)]):
            results = []
            for function in (original, recovered):
                calls = []
                class Truth:
                    def __init__(self, label, value): self.label, self.value = label, value
                    def __bool__(self): calls.append(('truth', self.label)); return self.value
                class Row:
                    def __init__(self, index, value): self.index, self.value = index, value
                    def get(self, key): calls.append(('get', key, self.index)); return self.value
                rows = [Row(index, Truth(*value) if isinstance(value, tuple) else value)
                        for index, value in enumerate(amounts)]
                result = tuple(value.label if isinstance(value, Truth) else value
                               for value in function(iter(rows)))
                results.append((result, calls))
            self.assertEqual(results[1], results[0])

    def test_getter_stopiteration_retains_generator_error_conversion(self):
        original, recovered = paired_generators(
            'extra_features/sfml_stats/api/common.py',
            '<module>/_build_live_today_clean_eval#1/<genexpr>#2', 'actual_energy_terms')
        results = []
        for function in (original, recovered):
            calls = []
            class Row:
                def get(self, key):
                    calls.append(key)
                    raise StopIteration('owned getter failure')
            with self.assertRaises(RuntimeError) as raised:
                tuple(function(iter([Row()])))
            error = raised.exception
            results.append((str(error), type(error.__cause__).__name__, str(error.__cause__), calls))
        self.assertEqual(results[1], results[0])

    def test_milestone_terms_keep_short_circuit_predicate_order(self):
        for rows in ([], [(False, 100)], [(True, None), (True, 94.9), (True, 95), (True, 100)],
                     [(True, float('nan')), (False, None)]):
            results = []
            for side in (0, 1):
                calls = []
                def is_valid_day(row): calls.append(('valid', row)); return row[0]
                def daily_coverage(row): calls.append(('coverage', row)); return row[1]
                function = paired_generators(
                    'extra_features/sfml_stats/core/forecast_quality.py',
                    '<module>/build_milestones#1/<genexpr>#1', 'coverage_milestone_terms',
                    {'is_valid_day': is_valid_day, 'daily_coverage': daily_coverage})[side]
                results.append((tuple(function(iter(rows))), calls))
            self.assertEqual(results[1], results[0])

    def test_finite_sample_terms_keep_captured_lookup_laziness_and_errors(self):
        import numpy as np
        unit = '<module>/ScheduledTasksManager#1/_train_weather_mlp#1/<genexpr>#1'
        for values in ([], [0, 1.0, -2.5, float('nan'), float('inf'), float('-inf')],
                       [np.float64(3.5)], ['invalid']):
            results = []
            for side in (0, 1):
                calls = []
                class NumericLibrary:
                    @property
                    def isfinite(self):
                        calls.append('lookup')
                        def check(value): calls.append(('check', value)); return np.isfinite(value)
                        return check
                library = NumericLibrary()
                function = paired_generators(
                    'extra_features/solar_forecast_eai/consumption/consumption_scheduled_tasks.py',
                    unit, 'finite_sample_terms', captures=(library,))[side]
                generator = function(iter(values))
                self.assertEqual(calls, [])
                try: result = tuple(bool(value) for value in generator)
                except TypeError as error: result = type(error).__name__, str(error)
                results.append((result, calls))
            self.assertEqual(results[1], results[0])
