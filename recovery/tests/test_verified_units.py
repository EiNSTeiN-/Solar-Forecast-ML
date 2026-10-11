"""Compare explicitly selected pure original/reconstructed functions.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module or PyArmor runtime is imported. The original code is
instantiated only after a static unit match, with controlled standard-library
globals and bounded synthetic inputs.
"""
import importlib.util
import marshal
import math
import os
from pathlib import Path
import random
import statistics
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
BYTECODE_ROOT = os.environ.get('SFML_RECOVERED_DIR')
UNPACKER = os.environ.get('SFML_UNPACKER_DIR')


@unittest.skipUnless(BYTECODE_ROOT and UNPACKER and sys.version_info[:2] == (3, 14),
                     'Set SFML_RECOVERED_DIR and SFML_UNPACKER_DIR; use CPython 3.14')
class VerifiedNumericUnits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(UNPACKER) / 'oneshot'))
        from bytecode_fidelity import compare, compile_source
        from modern_backend import MarkerNormalizer, normalize_code
        path = Path(BYTECODE_ROOT) / 'data/data_shadow_group_math.py.1shot.recovered.pyc'
        data = path.read_bytes()
        if data[:4] != importlib.util.MAGIC_NUMBER:
            raise ValueError('Reference interpreter magic does not match')
        reference = normalize_code(marshal.loads(data[16:]))
        source = ROOT / 'verified_units/data/data_shadow_group_math.py'
        candidate = compile_source(source.read_text(), str(source))
        metadata = Path(str(path).removesuffix('.recovered.pyc') + '.seq').read_bytes()[:34]
        report = compare(reference, candidate, MarkerNormalizer(metadata[1:17], metadata[18:30]))
        if not report['all_units_match']:
            raise AssertionError(f'The complete reconstructed module failed static comparison: {report}')
        cls.functions = {}
        for name in ('_finite_number', '_not_evaluable_result', 'compare_panel_group_performance'):
            original = next(code for code in reference.co_consts
                            if isinstance(code, types.CodeType) and code.co_name == name)
            recovered = next(code for code in candidate.co_consts
                             if isinstance(code, types.CodeType) and code.co_name == name)
            report = compare(original, recovered)
            if not report['all_units_match']:
                raise AssertionError(f'{name} failed the static unit check: {report}')
            globals_ = {'math': math, 'MIN_GROUP_EXPECTED_KWH': 0.01,
                        'GROUP_PEER_RATIO_MAX': 0.7, 'GROUP_PEER_GAP_MIN': 0.25,
                        'GROUP_GLOBAL_WEATHER_RATIO_MAX': 0.55,
                        'OBSERVATION_CLEAR_NEGATIVE': 'clear_negative',
                        'OBSERVATION_PEER_CONFIRMED': 'peer_confirmed',
                        'median': statistics.median,
                        '__builtins__': {'isinstance': isinstance, 'bool': bool, 'float': float,
                                         'TypeError': TypeError, 'ValueError': ValueError,
                                         'round': round, 'max': max, 'min': min,
                                         'len': len, 'sorted': sorted}}
            left, right = dict(globals_), dict(globals_)
            for dependency, functions in cls.functions.items():
                left[dependency], right[dependency] = functions
            cls.functions[name] = (types.FunctionType(original, left), types.FunctionType(recovered, right))

    def test_finite_number_values_and_invalid_inputs(self):
        original, recovered = self.functions['_finite_number']
        values = [None, True, False, 0, -2, 4.25, '12.5', '', 'bad', {}, [],
                  math.nan, math.inf, -math.inf]
        values.extend(number / 8 for number in range(-32, 33))
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(recovered(value), original(value))

    def test_unusable_reference_result(self):
        original, recovered = self.functions['_not_evaluable_result']
        values = [None, True, -1, 0, 0.009, 0.01, 1.234567, '2.5', 'bad', math.nan, math.inf]
        self.assertEqual(recovered({}), original({}))
        for actual in values:
            for theoretical in values:
                data = {'actual_kwh': actual, 'theoretical_max_kwh': theoretical}
                with self.subTest(data=data):
                    self.assertEqual(recovered(data), original(data))

    def test_peer_comparison_reference_validation_and_weather(self):
        original, recovered = self.functions['compare_panel_group_performance']
        cases = [{}, {'east': {'actual_kwh': 1, 'theoretical_max_kwh': 2}},
                 {'east': {'actual_kwh': 0.9, 'theoretical_max_kwh': 1, 'clear_sky_kwh': 1},
                  'west': {'actual_kwh': 0.8, 'theoretical_max_kwh': 1, 'clear_sky_kwh': 1},
                  'south': {'actual_kwh': 0.2, 'theoretical_max_kwh': 1, 'clear_sky_kwh': 1}},
                 {name: {'actual_kwh': 0.2, 'theoretical_max_kwh': 1, 'clear_sky_kwh': 1}
                  for name in ('east', 'west', 'south')}]
        for case in cases:
            for valid in (False, True):
                with self.subTest(case=case, valid=valid):
                    self.assertEqual(recovered(case, reference_is_valid=valid),
                                     original(case, reference_is_valid=valid))

    def test_peer_comparison_bounded_random_cases(self):
        original, recovered = self.functions['compare_panel_group_performance']
        generator = random.Random(3473)
        values = [None, True, -0.1, 0, 0.009, 0.01, 0.3, 0.7, 1, 2.1,
                  '1.25', 'bad', math.nan, math.inf]
        for number in range(200):
            case = {str(index): {'actual_kwh': generator.choice(values),
                                 'theoretical_max_kwh': generator.choice(values),
                                 'clear_sky_kwh': generator.choice(values)}
                    for index in range(generator.randrange(7))}
            valid = bool(generator.randrange(2))
            with self.subTest(number=number):
                self.assertEqual(recovered(case, reference_is_valid=valid),
                                 original(case, reference_is_valid=valid))


if __name__ == '__main__':
    unittest.main()
