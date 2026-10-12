"""Paired astronomical gating and forecast-reader checks with controlled data.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No real database or integration is loaded.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from pure_reference import classes, functions
from test_isolated_initialization import logger_for


class TerminalReturnCheckpointTests(unittest.TestCase):
    def test_sun_gate_boundaries_errors_and_existing_mutation_contract(self):
        module = 'forecast/forecast_sun_gate.py'
        yields = ('production_kwh', 'prediction_kwh_uncapped', 'physics_kwh', 'ai_kwh', 'lstm_kwh', 'ridge_kwh', 'tfs_kwh')
        groups = ('power_kwh', 'physics_kwh', 'ai_kwh', 'lstm_kwh', 'ridge_kwh', 'tfs_kwh', 'raw_physics_kwh')
        pairs = functions(module, ('should_zero_hourly_yield', 'zeroed_hour_production_result', 'zero_hourly_yield_entry'),
            {'YIELD_KWH_KEYS': yields, 'GROUP_YIELD_KWH_KEYS': groups},
            defaults={'should_zero_hourly_yield': (None, None, None), 'zeroed_hour_production_result': (None,)})
        sunset = datetime(2026, 10, 11, 18)
        for elevation in (None, 'invalid', -1, 0, 1, float('nan')):
            for start in (None, sunset - timedelta(hours=1), sunset, sunset + timedelta(hours=1),
                          sunset.replace(tzinfo=timezone.utc)):
                self.assertEqual(pairs[0]['should_zero_hourly_yield'](elevation, start, sunset),
                                 pairs[1]['should_zero_hourly_yield'](elevation, start, sunset))
        payload = {'production_kwh': 3, 'physics_kwh': None, 'production': 3,
                   'groups': [{'power_kwh': 2, 'physics_kwh': None, 'extra': 5}, 'owned-other'], 'extra': 7}
        outputs = []
        for namespace in pairs:
            entry = copy.deepcopy(payload)
            result = namespace['zero_hourly_yield_entry'](entry)
            self.assertIs(result, entry)
            outputs.append((result, namespace['zeroed_hour_production_result'](0)))
        self.assertEqual(outputs[1], outputs[0])
        self.assertIsNone(outputs[1][0]['physics_kwh'])
        self.assertEqual(outputs[1][0]['groups'][0]['extra'], 5)

    def test_forecast_reader_time_boundaries_queries_and_failure_fallbacks(self):
        module = 'extra_features/sfml_stats/core/solar_forecast_reader_gpm.py'
        scenarios = ('normal', 'empty', 'error', 'null_sum')
        for scenario in scenarios:
            for hour in (0, 5, 6, 21, 22, 23):
                outputs = []
                for index in (0, 1):
                    events = []
                    now = datetime(2026, 10, 11, hour)
                    class Database:
                        async def execute_read(self, sql, parameters):
                            events.append(('read', sql, parameters))
                            if scenario == 'error':
                                raise RuntimeError('owned read failure')
                            if scenario == 'empty':
                                return []
                            if 'daily_forecasts' in sql:
                                return [('today', '2026-10-11', 4, 'owned', True),
                                        ('tomorrow', '2026-10-12', 8, 'owned', False),
                                        ('other', '2026-10-12', 99, 'owned', False)]
                            return [(None,)] if scenario == 'null_sum' else [(2.5,)]
                    cls = classes(module, 'SolarForecastReader', {
                        'dt_util': SimpleNamespace(now=lambda: now), 'timedelta': timedelta,
                        'SolarForecast': SimpleNamespace, '_LOGGER': logger_for(events)})[index]
                    reader = cls(Database())
                    async def run():
                        forecasts = await reader.async_get_forecasts()
                        remaining = await reader.async_get_remaining_forecast_today_kwh()
                        relevant = await reader.async_get_relevant_forecast_kwh()
                        average = await reader.async_get_average_consumption_kwh(days=3)
                        return ({key: vars(value) if value else None for key, value in forecasts.items()},
                                remaining, relevant, average)
                    outputs.append((asyncio.run(run()), events))
                self.assertEqual(outputs[1], outputs[0], (scenario, hour))
                if scenario == 'normal':
                    expected = 2.5 if 6 <= hour < 22 else 8 if hour >= 22 else 4
                    self.assertEqual(outputs[1][0][2], expected)


if __name__ == '__main__':
    unittest.main()
