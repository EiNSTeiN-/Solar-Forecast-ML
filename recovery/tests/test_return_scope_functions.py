"""Verified isolated return scopes with owned conversion/access dependencies."""
from datetime import timezone
import math
import unittest

from pure_reference import isolated_function


NUMBERS=(None,0,False,True,-1,1.5,'2.3','unknown','NaN','inf',object(),
         'fixture:type','fixture:value','fixture:runtime','fixture:good','fixture:invalid')


def number_sample(value,calls):
    if not isinstance(value,str) or not value.startswith('fixture:'): return value
    mode=value.split(':')[1]
    class Number:
        def __bool__(self): raise AssertionError('Numeric conversion must not truth-test value')
        def __float__(self):
            calls.append('float')
            errors={'type':TypeError,'value':ValueError,'runtime':RuntimeError}
            if mode in errors: raise errors[mode]('owned '+mode)
            return 'invalid' if mode=='invalid' else 3.5
    return Number()


def stable_number(value,default=None):
    if value is default: return 'same default'
    if isinstance(value,float) and math.isnan(value): return 'nan'
    return value


class ReturnScopeFunctionTests(unittest.TestCase):
    def test_feature_float_preserves_defaults_conversion_hooks_nan_and_errors(self):
        pair=isolated_function('ai/ai_feature_engineering.py','ai/feature_safe_float.py',
                               '<module>/FeatureEngineer#1/_safe_float#1',{})
        for sample in NUMBERS:
            outcomes=[]
            for function in pair:
                calls=[]
                value=number_sample(sample,calls)
                default=object()
                try: result=stable_number(function(object(),value,default),default)
                except RuntimeError as error: result=type(error).__name__,str(error)
                outcomes.append((result,calls))
            self.assertEqual(outcomes[1],outcomes[0])

    def test_optional_number_preserves_none_hooks_nonfinite_and_unhandled_errors(self):
        pair=isolated_function('extra_features/sfml_stats/core/energy_efficiency.py',
                               'extra_features/sfml_stats/core/optional_number.py',
                               '<module>/_number#1',{})
        for sample in NUMBERS:
            outcomes=[]
            for function in pair:
                calls=[]
                value=number_sample(sample,calls)
                try: result=stable_number(function(value))
                except RuntimeError as error: result=type(error).__name__,str(error)
                outcomes.append((result,calls))
            self.assertEqual(outcomes[1],outcomes[0])

    def test_both_row_helpers_preserve_key_index_fallback_order_and_base_exceptions(self):
        for module in ('corrected_energy','daily_costs'):
            pair=isolated_function('extra_features/sfml_stats/core/'+module+'.py',
                                   'extra_features/sfml_stats/core/row_cell.py',
                                   '<module>/_cell#1',{})
            for mode in ('ok','key-error','fallback-error','missing-keys','keys-error',
                         'base-exception','none-key','false-key'):
                outcomes=[]
                for function in pair:
                    calls=[]
                    sentinel=object()
                    class Row:
                        @property
                        def keys(self):
                            calls.append('keys')
                            if mode=='missing-keys': raise AttributeError('owned missing keys')
                            if mode=='keys-error': raise ValueError('owned keys getter')
                            return None
                        def __getitem__(self,key):
                            calls.append(('item',key))
                            if key=='value':
                                if mode in ('key-error','fallback-error'): raise KeyError('owned key')
                                if mode=='base-exception': raise KeyboardInterrupt('owned key')
                                if mode=='none-key': return None
                                if mode=='false-key': return False
                            elif mode=='fallback-error': raise IndexError('owned index')
                            return sentinel
                    try:
                        result=function(Row(),0,'value')
                        result='same value' if result is sentinel else result
                    except (ValueError,IndexError,KeyboardInterrupt) as error:
                        result=type(error).__name__,str(error)
                    outcomes.append((result,calls))
                self.assertEqual(outcomes[1],outcomes[0])

    def zone_functions(self,globals_):
        return isolated_function('extra_features/solar_forecast_eai/weather_intelligence.py',
                                 'extra_features/solar_forecast_eai/safe_zone.py',
                                 '<module>/_safe_zone#1',globals_)

    def test_timezone_truth_constructor_identity_and_exact_handled_error_types(self):
        for sample in (None,'','UTC','false','true','truth-value-error','truth-key-error'):
            for failure in (None,ValueError,TypeError,KeyError):
                outcomes=[]
                for side in (0,1):
                    calls=[]
                    sentinel=object()
                    class Name:
                        def __bool__(self):
                            calls.append('truth')
                            if sample=='truth-value-error': raise ValueError('owned truth')
                            if sample=='truth-key-error': raise KeyError('owned truth')
                            return sample=='true'
                    value=sample if sample in (None,'','UTC') else Name()
                    def constructor(name):
                        self.assertIs(name,value)
                        calls.append('zone')
                        if failure is not None: raise failure('owned zone')
                        return sentinel
                    function=self.zone_functions({'ZoneInfo':constructor,'timezone':timezone})[side]
                    try:
                        result=function(value)
                        result='same zone' if result is sentinel else 'utc' if result is timezone.utc else result
                    except KeyError as error: result=type(error).__name__,str(error)
                    outcomes.append((result,calls))
                self.assertEqual(outcomes[1],outcomes[0])

    def test_timezone_fallback_getter_errors_keep_nested_handler_boundaries(self):
        for value in (None,'UTC'):
            for mode in ('ok','first-error','always-error'):
                outcomes=[]
                for side in (0,1):
                    calls=[]
                    class Timezone:
                        count=0
                        @property
                        def utc(self):
                            self.count+=1
                            calls.append(('utc',self.count))
                            if mode=='always-error' or mode=='first-error' and self.count==1:
                                raise ValueError('owned utc getter')
                            return timezone.utc
                    def constructor(name): calls.append('zone'); raise ValueError('owned zone')
                    function=self.zone_functions({'ZoneInfo':constructor,'timezone':Timezone()})[side]
                    try: result=function(value)
                    except ValueError as error: result=type(error).__name__,str(error)
                    outcomes.append((result,calls))
                self.assertEqual(outcomes[1],outcomes[0])


if __name__=='__main__':
    unittest.main()
