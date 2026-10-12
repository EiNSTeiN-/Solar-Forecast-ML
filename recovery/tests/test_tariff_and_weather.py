"""Compare matched tariff/weather units using explicit, controlled dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, HTTP service, or real database is loaded.
"""
import asyncio
import copy
from datetime import datetime, time, timedelta, timezone
import logging
import os
import random
import sys
from types import SimpleNamespace
from typing import Any
import unittest

from pure_reference import classes, functions

AVAILABLE = (os.environ.get('SFML_RECOVERED_DIR') and os.environ.get('SFML_UNPACKER_DIR')
             and sys.version_info[:2] == (3, 14))
LOGGER = logging.getLogger('tariff_weather_fixture')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class PriceSlot:
    """Record calls to the external slot dependency without loading its module."""
    def __init__(self, **values):
        self.values = values

    def as_dict(self):
        return dict(self.values)


def local_hours(start, end):
    current = start
    while current < end:
        yield current, current
        current += timedelta(hours=1)


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class TariffBehavior(unittest.TestCase):
    def test_fixed_and_deterministic_demo_slots(self):
        globals_ = {'Any': Any, 'datetime': datetime, 'timedelta': timedelta,
                    'PriceSlot': PriceSlot, 'iter_local_hours': local_hours,
                    'PRICE_SOURCE_FIXED': 'fixed', 'TARIFF_MODE_FIXED': 'fixed', 'random': random,
                    '_DEMO_SHAPE': [22, 21, 20, 20, 21, 23, 27, 31, 30, 28, 26, 25, 24, 24, 25, 27, 31, 36, 38, 35, 31, 28, 25, 23]}
        fixed = classes('extra_features/grid_price_monitor/tariffs/fixed.py', 'FixedSource', globals_)
        demo = classes('extra_features/grid_price_monitor/tariffs/demo.py', 'DemoSource', globals_)
        start = datetime(2026, 10, 11)
        for length in (0, 1, 12, 24, 48):
            end = start + timedelta(hours=length)
            for price in (-10, 0, 30.5):
                left, right = (cls(price) for cls in fixed)
                self.assertEqual(right.label, left.label)
                self.assertEqual(asyncio.run(right.async_get_slots(start, end)),
                                 asyncio.run(left.async_get_slots(start, end)))
            left, right = (cls() for cls in demo)
            self.assertEqual(asyncio.run(right.async_get_slots(start, end)),
                             asyncio.run(left.async_get_slots(start, end)))

    def test_clock_and_time_of_use_boundaries(self):
        relative = 'extra_features/grid_price_monitor/tariffs/time_of_use.py'
        globals_ = {'Any': Any, 'datetime': datetime, 'time': time, '_LOGGER': LOGGER,
                    'PRICE_SOURCE_TIME_OF_USE': 'time_of_use', 'TARIFF_MODE_TIME_OF_USE': 'time_of_use',
                    'PriceSlot': PriceSlot, 'iter_local_hours': local_hours}
        left, right = functions(relative, ('parse_clock', '_in_high_window'), globals_)
        for value in ('6', '06:00', '22:30:10.5', time(3, 4, 5, 123)):
            self.assertEqual(right['parse_clock'](value), left['parse_clock'](value))
        for high_start, high_end in ((time(6), time(22)), (time(22), time(6)), (time(0), time(0))):
            for hour in range(24):
                self.assertEqual(right['_in_high_window'](time(hour), high_start, high_end),
                                 left['_in_high_window'](time(hour), high_start, high_end))
        class_globals = dict(globals_, parse_clock=left['parse_clock'], _in_high_window=left['_in_high_window'],
                             nationwide_holiday=lambda country, day: day.day == 11)
        original, recovered = classes(relative, 'TimeOfUseSource', class_globals)
        for start, end in (('06:00', '22:00'), ('22:00', '06:00'), ('00:00', '00:00')):
            for weekend in (False, True):
                for holiday in (False, True):
                    options = dict(high_price=35, low_price=15, high_start=start, high_end=end,
                                   weekend_low=weekend, holiday_low=holiday, country='CA')
                    first, second = original(**options), recovered(**options)
                    for day in (9, 10, 11, 12):
                        for hour in (0, 5, 6, 21, 22, 23):
                            local = datetime(2026, 10, day, hour)
                            self.assertEqual(second.price_for(local), first.price_for(local))

    def test_awattar_dependency_calls_and_empty_responses(self):
        class Calculator:
            def calculate_total_price(self, price):
                return round(price * 1.19 + 10, 2)
        class Service:
            def __init__(self, prices):
                self.prices, self.calls = prices, []
            def _parse_awattar_response(self, response):
                return response['prices']
            async def fetch_day_ahead_prices(self, **window):
                self.calls.append(window)
                return copy.deepcopy(self.prices)
        original, recovered = classes('extra_features/grid_price_monitor/tariffs/awattar.py', 'AwattarSource',
            {'Any': Any, 'datetime': datetime, 'PriceSlot': PriceSlot,
             'PRICE_SOURCE_AWATTAR': 'awattar', 'TARIFF_MODE_DYNAMIC': 'dynamic'})
        start, end = datetime(2026, 10, 11), datetime(2026, 10, 12)
        for prices in (None, [], [{'timestamp': '2026-10-11T12:30:00+00:00', 'price': 5}],
                       [{'timestamp': datetime(2026, 10, 11, 13, tzinfo=timezone.utc), 'price': -10}]):
            first_service, second_service = Service(prices), Service(prices)
            first, second = original('CA', Calculator(), first_service), recovered('CA', Calculator(), second_service)
            self.assertEqual(asyncio.run(second.async_get_slots(start, end)),
                             asyncio.run(first.async_get_slots(start, end)))
            self.assertEqual(second_service.calls, first_service.calls)
            if prices is not None:
                self.assertEqual(second.slots_from_response({'prices': copy.deepcopy(prices)}),
                                 first.slots_from_response({'prices': copy.deepcopy(prices)}))

    def test_demo_persistence_records_identical_calls(self):
        relative = 'extra_features/grid_price_monitor/storage/demo_persist.py'
        left, right = functions(relative, ('async_persist_demo_prices',), {'datetime': datetime})
        class Recorder:
            def __init__(self):
                self.calls = []
            async def require_price_write_schema(self):
                self.calls.append(('schema',))
            async def execute(self, *args):
                self.calls.append(('execute', *args))
            async def async_save(self, *args, **kwargs):
                self.calls.append(('cache', args, kwargs))
        now = datetime(2026, 10, 11, 12, 25)
        for slots in ([], [{'hour': 12, 'date': '2026-10-11', 'total_price': 25}],
                      [{'hour': 11, 'date': '2026-10-11', 'total_price': 10},
                       {'hour': 12, 'date': '2026-10-11', 'total_price': 20},
                       {'hour': 13, 'date': '2026-10-11', 'total_price': 30}]):
            observed = []
            for function in (left['async_persist_demo_prices'], right['async_persist_demo_prices']):
                db, cache = Recorder(), Recorder()
                payload = copy.deepcopy(slots)
                asyncio.run(function(db, cache, payload, country='CA', now_local=now,
                                     feed_in_tariff_ct=None, base_fee_eur_month=5))
                observed.append((db.calls, cache.calls, payload))
            self.assertEqual(observed[1], observed[0])


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class WeatherPlatformBehavior(unittest.TestCase):
    def test_weather_properties_and_forecasts(self):
        class Base:
            def __init__(self, coordinator):
                self.coordinator = coordinator
        globals_ = {'Any': Any, 'DOMAIN': 'weather_fixture', 'NAME': 'Weather Fixture',
                    'VERSION': 'owned', 'ATTRIBUTION': 'fixture', 'CONF_LOCATION_NAME': 'location_name',
                    'UnitOfTemperature': SimpleNamespace(CELSIUS='C'), 'UnitOfPressure': SimpleNamespace(HPA='hPa'),
                    'UnitOfSpeed': SimpleNamespace(KILOMETERS_PER_HOUR='km/h'),
                    'UnitOfPrecipitationDepth': SimpleNamespace(MILLIMETERS='mm'),
                    'WeatherEntityFeature': SimpleNamespace(FORECAST_HOURLY=1, FORECAST_DAILY=2)}
        for name in ('CLOUD_COVERAGE', 'CONDITION', 'HUMIDITY', 'NATIVE_PRECIPITATION', 'NATIVE_PRESSURE',
                     'NATIVE_TEMP', 'NATIVE_TEMP_LOW', 'NATIVE_WIND_SPEED', 'PRECIPITATION_PROBABILITY', 'TIME', 'WIND_BEARING'):
            globals_['ATTR_FORECAST_' + name] = name.lower()
        original, recovered = classes('extra_features/weather_fusion_ai/weather.py', 'WeatherFusionAIWeather', globals_, (Base,))
        entry = SimpleNamespace(entry_id='owned_entry', data={'location_name': 'Garage'})
        for current in ({}, {'temperature': 12, 'humidity': 75, 'visibility': 10000, 'wind_bearing': 'NE'},
                        {'condition': 'rainy', 'blend_info': {'cloud_type': 'mixed', 'experts_used': ['test']}}):
            coordinator = SimpleNamespace(current_weather=current, learning_stats={'learning_days': 10, 'accuracy_percent': 90},
                latitude=45, longitude=-73, hourly_forecast=[{'datetime': '2026-10-11T12:00:00', 'temperature': 12}],
                daily_forecast=[{'datetime': '2026-10-11', 'native_temperature': 15, 'templow': 3}])
            first, second = original(coordinator, entry), recovered(coordinator, entry)
            for name in ('condition', 'native_temperature', 'native_apparent_temperature', 'native_dew_point',
                         'humidity', 'native_pressure', 'native_wind_speed', 'wind_bearing', 'cloud_coverage',
                         'native_visibility', 'uv_index', 'extra_state_attributes'):
                self.assertEqual(getattr(second, name), getattr(first, name))
            for name in ('async_forecast_hourly', 'async_forecast_daily'):
                self.assertEqual(asyncio.run(getattr(second, name)()), asyncio.run(getattr(first, name)()))
            coordinator.hourly_forecast = []
            coordinator.daily_forecast = []
            self.assertIsNone(asyncio.run(second.async_forecast_hourly()))
            self.assertIsNone(asyncio.run(second.async_forecast_daily()))
