"""Paired policy and historical weather analytics with owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, HTTP server, native runtime or real database is loaded.
"""

import ast
import asyncio
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import statistics
from types import SimpleNamespace
import unittest

from pure_reference import classes, functions


class PolicyAndWeatherEnergyTests(unittest.TestCase):
    def test_promotion_policy_noise_bands_regimes_age_and_dependency_calls(self):
        relative = 'data/data_model_promotion_policy.py'
        path = Path(__file__).resolve().parents[1] / 'verified_units' / relative
        constants = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if not node.targets[0].id.startswith('MODEL_PROMOTION_'): continue
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'frozenset':
                    constants[node.targets[0].id] = frozenset(ast.literal_eval(node.value.args[0]))
                else: constants[node.targets[0].id] = ast.literal_eval(node.value)
        names = ('_sample_weather_regime', '_sample_target_date', '_observed_regimes',
                 '_daily_delta_noise_band', 'slice_allowed_daily_energy_regression',
                 'champion_age_days', 'build_cycle_aware_promotion_policy_config',
                 'build_cycle_aware_promotion_policy_config_v3')
        scenarios = ([], 'invalid', None,
            [dict(target_date='2026-10-10', weather_regime='unrecognized', delta=0.1)],
            [dict(target_date='2026-10-10', weather_regime='stable_good', delta=0.1),
             dict(target_date='2026-10-11', weather_regime='rain_overcast', delta=-0.05)],
            [SimpleNamespace(target_date='2026-10-11', weather_regime='stable_good', delta=0.2)])
        for samples in scenarios:
            outputs = []
            for index in (0, 1):
                events = []
                def deltas(rows, forecast_cycle, weather_regime):
                    events.append(('deltas', forecast_cycle, weather_regime, len(rows)))
                    return [(row['delta'] if isinstance(row, Mapping) else row.delta) for row in rows]
                def importer(name, globals=None, locals=None, fromlist=(), level=0):
                    self.assertEqual(name, 'ai.ai_model_promotion')
                    self.assertEqual(level, 2)
                    self.assertEqual(fromlist, ('cycle_slice_day_deltas',))
                    events.append(('import', name, fromlist, level))
                    return SimpleNamespace(cycle_slice_day_deltas=deltas)
                selected = functions(relative, names, dict(constants, math=math, statistics=statistics,
                    Mapping=Mapping, date=date, datetime=datetime),
                    builtins_overrides={'__import__': importer})[index]
                result = []
                for name in ('build_cycle_aware_promotion_policy_config', 'build_cycle_aware_promotion_policy_config_v3'):
                    try:
                        options = {'champion_trained_on': date(2026, 9, 1)} if name.endswith('_v3') else {}
                        result.append(selected[name](samples, **options))
                    except (TypeError, ValueError) as error: result.append((type(error).__name__, str(error)))
                bands = [selected['_daily_delta_noise_band'](values) for values in
                         ([], [0.1], [0.1, 0.1], [0.1, -0.1], [number / 100 for number in range(20)])]
                ages = [selected['champion_age_days'](
                    [dict(target_date='2026-10-11', weather_regime='stable_good')], trained)
                    for trained in (None, date(2026, 9, 1), datetime(2026, 9, 1), date(2026, 12, 1))]
                outputs.append((result, bands, ages, events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][2], [None, 40, 40, 0])

    def test_weather_api_dates_dev_gates_and_callbacks(self):
        relative = 'extra_features/sfml_stats/api/modern_weather_energy.py'
        class Response(SimpleNamespace): pass
        for value in (None, '', 'invalid', '2026-10-10', '2026-10-12', '20261011'):
            for restricted in (False, True):
                outputs = []
                for index in (0, 1):
                    events = []
                    class Base:
                        def __init__(self, hass, reader): self.hass = hass; self.reader = reader
                        def _require_dev(self): return Response(error='owned_dev') if restricted else None
                        def _days(self, request, **options): events.append(('days', options)); return 90
                        async def _run(self, callback):
                            events.append(('run',)); return await callback()
                    class Reader:
                        async def get_summary(self, target): events.append(('summary', target)); return {'summary': target}
                        async def get_day(self, target): events.append(('day', target)); return {'day': target}
                        async def get_impact(self, days): events.append(('impact', days)); return {'impact': days}
                        async def get_comparable_days(self, target): events.append(('comparable', target)); return {'comparable': target}
                    globals_ = {'date': date, 'web': SimpleNamespace(Response=Response),
                        'ha_today': lambda hass: date(2026, 10, 11),
                        '_error': lambda code, message, status: Response(code=code, message=message, status=status),
                        'local_only': lambda function: function}
                    parent = classes(relative, 'WeatherEnergyView', globals_, bases=(Base,))[index]
                    selected = [classes(relative, name, globals_, bases=(parent,))[index] for name in
                        ('WeatherEnergySummaryView', 'WeatherEnergyDayView', 'WeatherEnergyImpactView',
                         'WeatherEnergyComparableDaysView')]
                    request = SimpleNamespace(query={} if value is None else {'date': value})
                    async def exercise():
                        return [await cls(SimpleNamespace(), Reader()).get(request) for cls in selected]
                    results = asyncio.run(exercise())
                    required = parent(SimpleNamespace(), Reader())._date(request, required=True)
                    outputs.append((results, required, events))
                self.assertEqual(outputs[1], outputs[0])

    def test_weather_reader_queries_cache_snapshots_periods_and_failures(self):
        relative = 'extra_features/sfml_stats/readers/weather_energy_reader.py'
        for mode in ('snapshot', 'archive', 'no_date', 'missing_hours', 'failure'):
            outputs = []
            for index in (0, 1):
                events = []
                class Base:
                    def __init__(self, hass, db_path=None): events.append(('initialize', db_path))
                    async def _cached(self, key, callback, ttl):
                        events.append(('cached', key, ttl)); return await callback()
                    async def _fetch_all(self, sql, parameters=()):
                        events.append(('query', sql, parameters))
                        if mode == 'failure': raise RuntimeError('owned query failure')
                        if 'MAX(d.date)' in sql:
                            return [] if mode == 'no_date' else [{'latest_date':'2026-10-11'}]
                        if 's.snapshot_sequence' in sql:
                            return [{'snapshot_sequence':7}] if mode == 'snapshot' else []
                        if 'SELECT e.*' in sql:
                            return [{'installed_capacity_kwp':4, 'accuracy_percent':80,
                                     'evaluation_coverage_percent':90}]
                        if 'AS month_yield' in sql: return [{'month_yield':5,'year_yield':42}]
                        if 'ORDER BY solar_yield_kwh' in sql:
                            return [{'date':'2026-10-11','solar_yield_kwh':4}]
                        if 'SELECT e.date' in sql:
                            return [{'date':'2026-10-11','solar_yield_kwh':4,'accuracy_percent':80}]
                        return [] if mode == 'missing_hours' else [{'date':'2026-10-11','hour':12,'actual':1}]
                def record(name):
                    def build(*args, **options):
                        events.append(('build', name, args, options)); return {'kind':name, **options}
                    return build
                def day_story(rows, target_date):
                    events.append(('build', 'day', rows, target_date))
                    return {'hours':[{'state':'missing' if not rows else 'owned'}],
                            'weather_coverage_percent':50,'energy_coverage_percent':60}
                cls = classes(relative, 'WeatherEnergyReader', {
                    'date':date, 'timedelta':timedelta, 'defaultdict':defaultdict,
                    'build_day_story':day_story, 'build_weather_impact':record('impact'),
                    'build_weather_energy_insights':record('insights'),
                    'build_production_performance':record('production'),
                    'calculate_solar_conditions':lambda rows: {'score':len(rows)},
                    'build_day_features':record('features'),
                    'build_comparable_days':record('comparable')}, bases=(Base,))[index]
                reader = cls(SimpleNamespace(), None)
                async def exercise():
                    results = []
                    for method, args in ((reader.get_day,(None,)), (reader.get_summary,(None,)),
                                         (reader.get_impact,(30,)), (reader.get_comparable_days,(None,))):
                        try: results.append(await method(*args))
                        except RuntimeError as error: results.append((type(error).__name__,str(error)))
                    return results
                outputs.append((asyncio.run(exercise()),events))
            self.assertEqual(outputs[1],outputs[0])
            if mode == 'no_date': self.assertFalse(outputs[1][0][0]['available'])
            if mode == 'snapshot': self.assertEqual(outputs[1][0][0]['forecast_basis'],'morning_snapshot')
            if mode == 'archive': self.assertEqual(outputs[1][0][0]['forecast_basis'],'weather_precision_archive')


if __name__ == '__main__':
    unittest.main()
