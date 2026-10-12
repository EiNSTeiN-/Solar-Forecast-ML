"""Selected shared-return functions with exact unit proof and owned dependencies."""
import unittest

from pure_reference import functions, isolated_function


class SharedReturnFunctionTests(unittest.TestCase):
    def diagnostic_functions(self, globals_):
        return isolated_function('sensors/sensor_diagnostic.py', 'sensors/gate_status_text.py',
                                 '<module>/_gate_status_text#1', globals_)

    def test_diagnostic_language_defaults_and_false_values(self):
        class Token:
            def __bool__(self): raise AssertionError('Must not truth-test a supplied value')
            def __str__(self): return 'token'
        token = Token()
        for language in ('en', 'de'):
            for eligible, required in ((None, None), (0, 0), (False, False), (-1, 3),
                                       (2, 7), ('a', 'b'), (token, token)):
                calls = []
                def select_language(coordinator):
                    calls.append(coordinator)
                    return language
                original, recovered = self.diagnostic_functions({
                    '_gate_ui_language':select_language,
                    '_GATE_STATUS_TEMPLATES':{
                        'en':{'learning':'Learning {eligible_days}/{required_days}'},
                        'de':{'learning':'Lernen {eligible_days}/{required_days}'},
                    },
                })
                owner = object()
                values = [function('learning', owner, eligible_days=eligible, required_days=required)
                          for function in (original, recovered)]
                self.assertEqual(values[1], values[0])
                self.assertEqual(calls, [owner, owner])

    def test_diagnostic_getter_order_and_unhandled_failures(self):
        for failure in (None, 'language', 'language-key', 'kind-key', 'format-get', 'format-call'):
            outcomes = []
            for side in (0, 1):
                calls = []
                class Formatter:
                    @property
                    def format(self):
                        calls.append('format-get')
                        if failure == 'format-get': raise ValueError('owned format getter')
                        def format_(**values):
                            calls.append(('format-call', values))
                            if failure == 'format-call': raise ValueError('owned formatter')
                            return 'formatted'
                        return format_
                class Kinds:
                    def __getitem__(self, key):
                        calls.append(('kind-key', key))
                        if failure == 'kind-key': raise KeyError('owned kind')
                        return Formatter()
                class Languages:
                    def __getitem__(self, key):
                        calls.append(('language-key', key))
                        if failure == 'language-key': raise KeyError('owned language key')
                        return Kinds()
                def select_language(owner):
                    calls.append('language')
                    if failure == 'language': raise ValueError('owned language selector')
                    return 'en'
                function = self.diagnostic_functions({'_gate_ui_language':select_language,
                                                       '_GATE_STATUS_TEMPLATES':Languages()})[side]
                try: result = function('learning', object(), eligible_days=None, required_days=0)
                except (ValueError, KeyError) as error: result = type(error).__name__, str(error)
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_entitlement_payload_identity_getter_order_and_constructor_errors(self):
        for absent in (False, True):
            for failure in (None, 'payload', 'status', 'status-value', 'license', 'expires', 'construct'):
                outcomes = []
                for side in (0, 1):
                    calls = []
                    def access(name, value):
                        calls.append(name)
                        if failure == name: raise ValueError('owned ' + name)
                        return value
                    class Payload:
                        def __bool__(self): raise AssertionError('Must not truth-test payload')
                        @property
                        def license_id(self): return access('license', 'owned-id')
                        @property
                        def expires_at(self): return access('expires', 'owned-date')
                    class Status:
                        @property
                        def value(self): return access('status-value', 'problem')
                    class Result:
                        @property
                        def payload(self): return access('payload', None if absent else Payload())
                        @property
                        def status(self): return access('status', Status())
                    def construct(**values):
                        calls.append(('construct', values))
                        if failure == 'construct': raise ValueError('owned constructor')
                        return values
                    function = functions(
                        'extra_features/grid_price_monitor/license/entitlements.py',
                        ['_problem_resolution'],
                        {'EntitlementResolution':construct, 'SOURCE_EAI_ENTRY':'owned-source'})[side]['_problem_resolution']
                    try: result = function(Result())
                    except ValueError as error: result = type(error).__name__, str(error)
                    outcomes.append((result, calls))
                self.assertEqual(outcomes[1], outcomes[0])

    def bounded_functions(self, optional_number):
        return isolated_function('extra_features/solar_forecast_eai/energy_context.py',
                                 'extra_features/solar_forecast_eai/bounded_number.py',
                                 '<module>/_bounded_number#1', {'_optional_number':optional_number})

    def test_bounded_values_preserve_conversion_identity_nan_and_errors(self):
        for number in (None, -1, 0, 5, 10, 11, float('nan'), float('inf'), ValueError('owned conversion')):
            outcomes = []
            for side in (0, 1):
                calls = []
                value = object()
                def optional_number(supplied):
                    self.assertIs(supplied, value)
                    calls.append('conversion')
                    if isinstance(number, Exception): raise number
                    return number
                function = self.bounded_functions(optional_number)[side]
                try:
                    result = function(value, 0, 10)
                    if result is not None:
                        self.assertIs(result, number)
                        result = 'same number'
                except ValueError as error: result = type(error).__name__, str(error)
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_bounded_rich_comparisons_keep_short_circuit_and_truth_errors(self):
        class FailingTruth:
            def __bool__(self): raise ValueError('owned comparison truth')
        for lower_result, upper_result in ((False, True), (True, False), (True, True),
                                          (ValueError('owned lower'), True),
                                          (True, ValueError('owned upper')), (FailingTruth(), True)):
            outcomes = []
            for side in (0, 1):
                calls = []
                def compare_result(name, result):
                    calls.append(name)
                    if isinstance(result, Exception): raise result
                    return result
                class Lower:
                    def __le__(self, number): return compare_result('lower', lower_result)
                class Number:
                    def __le__(self, maximum): return compare_result('upper', upper_result)
                    def __bool__(self): raise AssertionError('Must not truth-test number')
                number = Number()
                def optional_number(value): calls.append('conversion'); return number
                function = self.bounded_functions(optional_number)[side]
                try:
                    result = function(object(), Lower(), object())
                    result = 'same number' if result is number else result
                except ValueError as error: result = type(error).__name__, str(error)
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])


if __name__ == '__main__':
    unittest.main()
