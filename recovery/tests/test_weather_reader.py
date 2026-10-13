"""Paired weather queries with owned database contexts and row constructors.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The complete module is proved before building its class; no integration imports.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
import unittest

from pure_reference import classes
from test_isolated_initialization import logger_for

RELATIVE = 'extra_features/sfml_stats/readers/weather_reader.py'
DAY = date(2026, 10, 12)
FIELDS = ('temperature_c', 'humidity_percent', 'wind_speed_ms', 'precipitation_mm',
          'pressure_hpa', 'solar_radiation_wm2', 'cloud_cover_percent',
          'precipitation_semantics', 'precipitation_unit', 'precipitation_quality')


def diagnostic_names(value):
    """Canonicalize only renamed local names in UnboundLocalError messages."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] == 'UnboundLocalError':
        message = value[1]
        for old, new in ((4, 'connection'), (5, 'columns'), (6, 'cursor'),
                         (12, 'rows'), (13, 'weather_readings'), (20, 'archive_readings')):
            message = message.replace(f"'_var_var_{old}'", repr(new))
        return value[0], message
    if isinstance(value, (tuple, list)):
        return type(value)(diagnostic_names(item) for item in value)
    return value


class WeatherReaderTests(unittest.TestCase):
    def test_manager_reconnect_direct_fallback_and_generator_failure_order(self):
        for initial in ('connected', 'disconnected', 'missing'):
            for failure in (None, 'connect', 'get-connection', 'direct-enter',
                            'body', 'cancel-connect', 'cancel-body'):
                outcomes = []
                for index in (0, 1):
                    events = []
                    marker = object()
                    connected = initial == 'connected'
                    def stage(name):
                        events.append(name)
                        if failure == name:
                            raise ValueError('owned ' + name)
                        if failure == 'cancel-' + name:
                            raise asyncio.CancelledError('owned ' + name)
                    class Manager:
                        @property
                        def is_connected(self):
                            events.append('connected')
                            return connected
                        async def connect(self):
                            nonlocal connected
                            stage('connect')
                            connected = True
                        async def get_connection(self):
                            stage('get-connection')
                            return marker
                    manager = Manager()
                    class Direct:
                        async def __aenter__(self):
                            stage('direct-enter')
                            return marker
                        async def __aexit__(self, kind, error, traceback):
                            events.append(('direct-exit', kind.__name__ if kind else None))
                            return False
                    def get_manager():
                        events.append('get-manager')
                        return None if initial == 'missing' else manager
                    def direct(path):
                        events.append(('direct', str(path)))
                        return Direct()
                    def owned_import(name, globals_, locals_, fromlist, level):
                        self.assertEqual((name, fromlist, level),
                            ('storage.db_connection_manager', ('get_manager', 'DatabaseConnectionManager'), 2))
                        return SimpleNamespace(get_manager=get_manager,
                            DatabaseConnectionManager=SimpleNamespace(connect_direct_safe=direct))
                    cls = classes(RELATIVE, 'WeatherDataReader', {
                        '_LOGGER': logger_for(events), 'Path': Path,
                        'SOLAR_FORECAST_DB': 'owned.sqlite', 'date': date,
                        'HourlyWeather': object, 'DatabaseConnectionManager': object,
                        'AsyncIterator': AsyncIterator,
                        'aiosqlite': SimpleNamespace(Connection=object),
                        'asynccontextmanager': asynccontextmanager,
                    }, builtins_overrides={'__import__': owned_import})[index]
                    cls.__init__.__globals__['WeatherDataReader'] = cls
                    reader = cls(Path('/owned'))
                    async def consume():
                        async with reader._get_db_connection() as connection:
                            self.assertIs(connection, marker)
                            stage('body')
                            return 'owned connection'
                    try:
                        result = ('return', asyncio.run(consume()))
                    except (Exception, asyncio.CancelledError) as error:
                        result = ('error', type(error).__name__, str(error))
                    outcomes.append((result, events))
                self.assertEqual(outcomes[0], outcomes[1])
                if failure is None:
                    self.assertEqual(outcomes[1][0], ('return', 'owned connection'))

    def paired_query(self, method, rows, columns=(), dates=(None, None), failure=None,
                     cursor_suppress=False, db_suppress=False, available=True):
        outcomes = []
        for index in (0, 1):
            events = []
            def stage(name):
                events.append(name)
                if failure == name:
                    raise ValueError('owned ' + name)
                if failure == 'cancel-' + name:
                    raise asyncio.CancelledError('owned ' + name)
            class Cursor:
                def __init__(self, schema):
                    self.schema = schema
                async def __aenter__(self):
                    stage('schema-enter' if self.schema else 'cursor-enter')
                    return self
                async def __aexit__(self, kind, error, traceback):
                    events.append(('cursor-exit', kind.__name__ if kind else None))
                    return cursor_suppress
                async def fetchall(self):
                    stage('schema-fetch' if self.schema else 'fetch')
                    return [(number, name) for number, name in enumerate(columns)] if self.schema else rows
            class Connection:
                async def __aenter__(self):
                    stage('db-enter')
                    return self
                async def __aexit__(self, kind, error, traceback):
                    events.append(('db-exit', kind.__name__ if kind else None))
                    stage('db-close')
                    return db_suppress
                def execute(self, query, parameters=()):
                    events.append(('query', query, tuple(parameters)))
                    stage('schema-query' if query.startswith('PRAGMA') else 'query')
                    return Cursor(query.startswith('PRAGMA'))
            def reading(**values):
                stage('construct')
                events.append(('weather', values))
                return values
            cls = classes(RELATIVE, 'WeatherDataReader', {
                '_LOGGER': logger_for(events), 'Path': Path, 'date': date,
                'HourlyWeather': reading, 'SOLAR_FORECAST_DB': 'owned.sqlite',
                'DatabaseConnectionManager': object, 'AsyncIterator': AsyncIterator,
                'aiosqlite': SimpleNamespace(Connection=object),
                'asynccontextmanager': asynccontextmanager,
            })[index]
            cls.__init__.__globals__['WeatherDataReader'] = cls
            reader = cls(Path('/owned'), SimpleNamespace(is_available=available))
            def connection():
                stage('connection')
                return Connection()
            reader._get_db_connection = connection
            try:
                result = ('return', asyncio.run(getattr(reader, method)(*dates)))
            except (Exception, asyncio.CancelledError) as error:
                result = ('error', type(error).__name__, str(error))
            outcomes.append((result, diagnostic_names(events)))
        self.assertEqual(outcomes[0], outcomes[1])
        return outcomes[1]

    def test_actual_and_forecast_rows_filters_and_context_order(self):
        actual = dict(date=DAY.isoformat(), hour=None, **dict.fromkeys(FIELDS))
        actual.update(temperature_c=0, precipitation_mm=0, precipitation_unit='mm')
        forecast = dict(date=DAY, hour=12, temperature=2, humidity=80, wind=1,
                        rain=0, solar_radiation_wm2=500, clouds=20)
        for method, row in (('async_get_hourly_weather', actual),
                            ('async_get_forecast_weather', forecast)):
            for dates in ((None, None), (DAY, None), (None, DAY), (DAY, DAY)):
                for rows in ([], [row], [{'date': 'invalid'}, row], [None, row]):
                    result, events = self.paired_query(method, rows,
                        columns=('date', 'hour', 'rain_semantics', 'precipitation_unit'), dates=dates)
                    self.assertEqual(result[0], 'return')
                    self.assertEqual(len(result[1]), bool(rows))
                    if result[1]:
                        self.assertEqual(result[1][0]['date'], DAY)
                        self.assertEqual(result[1][0]['hour'], 0 if method == 'async_get_hourly_weather' else 12)
                        built = next(i for i, event in enumerate(events) if event == 'construct')
                        closed = next(i for i, event in enumerate(events)
                                      if isinstance(event, tuple) and event[0] == 'db-exit')
                        self.assertLess(built, closed)
                    query = next(event for event in events if isinstance(event, tuple)
                                 and event[0] == 'query' and not event[1].startswith('PRAGMA'))
                    self.assertEqual(query[2], tuple(value.isoformat() for value in dates if value))

    def test_archive_schema_aliases_zero_values_and_malformed_rows(self):
        zero = (DAY.isoformat(), None, 0, None, None, None, None, None, None, None, None, None)
        empty = (DAY.isoformat(), 12, *([None] * 10))
        malformed = ('invalid', 12, 1, *([None] * 9))
        for columns in (('date', 'hour', 'temp_actual', 'actual_temp'),
                        ('date', 'hour'), ('date',)):
            for rows in ([], [zero], [empty, malformed, zero], None):
                result, events = self.paired_query('async_get_archived_hourly_weather', rows,
                    columns=columns, dates=(DAY, DAY))
                self.assertEqual(result[0], 'return')
                self.assertEqual(len(result[1]), int(rows is not None and bool(rows) and 'hour' in columns))
                if result[1]:
                    self.assertEqual(result[1][0]['temperature_c'], 0)
                if 'hour' not in columns:
                    self.assertFalse(any(event == 'fetch' for event in events))
                elif rows is not None:
                    query = next(event[1] for event in events if isinstance(event, tuple)
                                 and event[0] == 'query' and not event[1].startswith('PRAGMA'))
                    self.assertIn('"temp_actual" AS "temperature_c"' if 'temp_actual' in columns
                                  else 'NULL AS "temperature_c"', query)

    def test_query_failures_cancellation_and_context_suppression_keep_original_routes(self):
        actual = dict(date=DAY, hour=12, **dict.fromkeys(FIELDS))
        forecast = dict(date=DAY, hour=12, temperature=0, humidity=0, wind=0,
                        rain=0, solar_radiation_wm2=0, clouds=0)
        archive = (DAY, 12, 0, *([None] * 9))
        for method, row in (('async_get_hourly_weather', actual),
                            ('async_get_forecast_weather', forecast),
                            ('async_get_archived_hourly_weather', archive)):
            for failure in ('connection', 'db-enter', 'query', 'cursor-enter', 'fetch',
                            'construct', 'db-close', 'cancel-fetch', 'cancel-construct'):
                for cursor_suppress, db_suppress in ((False, False), (True, False), (False, True)):
                    result, events = self.paired_query(method, [row], ('date', 'hour'),
                        failure=failure, cursor_suppress=cursor_suppress, db_suppress=db_suppress)
                    if failure.startswith('cancel-') and not (cursor_suppress or db_suppress):
                        self.assertEqual(result[:2], ('error', 'CancelledError'))
        for method in ('async_get_hourly_weather', 'async_get_forecast_weather',
                       'async_get_archived_hourly_weather'):
            result, events = self.paired_query(method, [], available=False)
            self.assertEqual(result, ('return', []))
            self.assertNotIn('connection', events)
