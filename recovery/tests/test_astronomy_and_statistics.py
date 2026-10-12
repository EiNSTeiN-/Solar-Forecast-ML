"""Paired astronomy and statistics checks with owned database stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, native runtime or real database is loaded.
"""

import asyncio
from datetime import date, datetime, timedelta
import math
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from pure_reference import classes
from test_isolated_initialization import logger_for


class AstronomyAndStatisticsTests(unittest.TestCase):
    def test_astronomy_geometry_cache_rows_and_database_windows(self):
        outputs = []
        for index in (0, 1):
            events = []
            rows = []
            selected = []

            class Database:
                async def executemany(self, sql, parameters):
                    events.append((sql, parameters))

                async def execute(self, sql, parameters):
                    events.append((sql, parameters))

                async def fetchall(self, sql, parameters):
                    events.append((sql, parameters))
                    return rows

                async def fetchone(self, sql, parameters):
                    events.append((sql, parameters))
                    return selected

            class Loop:
                async def run_in_executor(self, executor, callback):
                    events.append(('executor', executor))
                    return callback()

            globals_ = {
                'math': math, 'datetime': datetime, 'date': date,
                'timedelta': timedelta, 'ZoneInfo': ZoneInfo,
                'asyncio': SimpleNamespace(get_running_loop=Loop, gather=asyncio.gather),
                '_LOGGER': logger_for(events),
                'PanelGroupTheoreticalMax': lambda **values: SimpleNamespace(**values),
                'DNI_MAX_WM2': 1200.0, 'HORIZONTAL_DIRECT_FRACTION': 0.8,
                'HORIZONTAL_DIFFUSE_FRACTION': 0.2,
                'POA_DNI_IAM_FORMULA_VERSION': 'owned-test-version',
                'beam_iam_martin_ruiz': lambda angle: 0.9,
            }
            cls = classes('astronomy/astronomy_cache.py', 'AstronomyCache', globals_)[index]
            cache = cls(Database())
            self.assertFalse(asyncio.run(cache.build_cache_for_date(date(2026, 6, 21), 3)))
            cache.initialize_location(46.0, -73.0, 'America/Toronto', 120)
            geometry = []
            for month in (3, 6, 12):
                for hour in (0, 6, 12, 18):
                    sample = datetime(2026, month, 21, hour, tzinfo=ZoneInfo('America/Toronto'))
                    elevation, azimuth = cache._calculate_sun_position(sample, 46.0, -73.0)
                    geometry.append((elevation, azimuth,
                                     cache._calculate_aoi(elevation, azimuth, 40, 180)))
            radiations = [cache._calculate_clear_sky_solar_radiation(elevation, 172)
                          for elevation in (-10, 0, 1, 30, 75)]
            poa = [cache._calculate_poa_from_ghi(ghi, elevation, 180, 40, 180, aoi)
                   for ghi, elevation, aoi in ((0, 40, 20), (600, 0, 20),
                                               (600, 40, 20), (600, 40, 100))]
            cache.set_panel_groups([
                {'name': 'south', 'power_wp': 3000, 'azimuth': 180, 'tilt': 40},
                {'name': 'disabled', 'power_wp': 0},
            ])
            grouped = cache._calculate_theoretical_pv_per_group(600, 40, 180)
            grouped = (grouped[0], [vars(value) for value in grouped[1]])
            build = asyncio.run(cache.build_cache_for_date(date(2026, 6, 21), 3))
            self.assertTrue(build)
            writes = [entry for entry in events if len(entry) == 2 and
                      isinstance(entry[0], str) and entry[0].startswith('INSERT INTO astronomy_cache')]
            self.assertEqual(len(writes[0][1]), 24)
            # Panel-group rows exist only while the sun is above the horizon.
            self.assertGreater(len(writes[1][1]), 0)
            self.assertLess(len(writes[1][1]), 24)
            rows[:] = [(12, 40.0, 180.0, 600.0, 1.7, 'sunrise', 'sunset', 'noon', 15.0)]
            day_data = asyncio.run(cache.get_day_data(date(2026, 6, 21)))
            selected[:] = [40.0, 180.0, 600.0, 1.7]
            hour_data = asyncio.run(cache.get_hourly_data(date(2026, 6, 21), 12))
            windows = []
            for values in (['2026-06-21T05:00:00-04:00', '2026-06-21T21:00:00-04:00'],
                           ['invalid', 'invalid'], [None, None], []):
                selected[:] = values
                windows.append(asyncio.run(cache.get_production_window(date(2026, 6, 21))))
            rows.clear()
            empty_day = asyncio.run(cache.get_day_data(date(2026, 6, 21)))
            outputs.append((geometry, radiations, poa, grouped, build, day_data,
                            hour_data, windows, empty_day, events))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1][1][:2], [0.0, 0.0])
        self.assertEqual(outputs[1][7][1:], [None, None, None])

    def test_statistics_storage_read_write_failures_and_cancellation(self):
        relative = 'extra_features/grid_price_monitor/storage/statistics_store.py'
        for mode in ('full', 'empty', 'read_error', 'write_error', 'bad_row'):
            outputs = []
            for index in (0, 1):
                events = []

                class Database:
                    async def fetchone(self, sql, parameters=()):
                        events.append(('read', sql, parameters))
                        if mode == 'read_error':
                            raise RuntimeError('owned read failure')
                        if mode == 'empty':
                            return None
                        if mode == 'bad_row':
                            return {'unrelated': 1}
                        if 'COUNT(*)' in sql:
                            return {'cnt': 2}
                        if 'GPM_daily_averages' in sql:
                            return dict(date='2026-10-11', average_net=10, average_total=12,
                                        min_price=3, max_price=20)
                        if 'GPM_monthly_summaries' in sql:
                            return dict(month='2026-10', average_price=12, cheap_hours=30, country='CA')
                        if 'GPM_price_extremes' in sql:
                            return dict(all_time_low=1, all_time_low_date='2026-01-01',
                                        all_time_high=30, all_time_high_date='2026-02-01')
                        return dict(today_kwh=1, week_kwh=4, month_kwh=12)

                    async def execute(self, sql, parameters=()):
                        events.append(('write', sql, parameters))
                        if mode == 'write_error':
                            raise RuntimeError('owned write failure')

                cls = classes(relative, 'StatisticsStore', {
                    '_LOGGER': logger_for(events), 'DAILY_AVERAGES_RETENTION_ROWS': 30,
                    'MONTHLY_SUMMARIES_RETENTION_MONTHS': 24})[index]
                store = cls(Database())

                async def exercise():
                    results = [await store.async_load()]
                    results.append(await store.async_update_daily_average('2026-10-11', 10, 12, 3, 20))
                    results.append(await store.async_update_daily_average('2026-10-11', 11, 13))
                    results.append(await store.async_update_monthly_summary(2026, 10, 12, 30, 'CA'))
                    results.append(await store.async_update_battery_totals(1, 4, 12))
                    results.append(await store.async_get_daily_average(datetime(2026, 10, 11)))
                    results.append(await store.async_get_monthly_summary(2026, 10))
                    results.append(await store.async_get_price_extremes())
                    results.append(await store.async_get_battery_totals())
                    results.append(await store.async_get_stats_summary())
                    return results

                outputs.append((asyncio.run(exercise()), store._loaded,
                    store.get_daily_average(datetime(2026, 10, 11)),
                    store.get_monthly_summary(2026, 10), store.get_price_extremes(),
                    store.get_battery_totals(), store.get_stats_summary(), events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertTrue(outputs[1][0][0])
        for index in (0, 1):
            class CancelledDatabase:
                async def fetchone(self, *args):
                    raise asyncio.CancelledError()

            cls = classes(relative, 'StatisticsStore', {
                '_LOGGER': logger_for([]), 'DAILY_AVERAGES_RETENTION_ROWS': 30,
                'MONTHLY_SUMMARIES_RETENTION_MONTHS': 24})[index]
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(cls(CancelledDatabase()).async_get_stats_summary())


if __name__ == '__main__':
    unittest.main()
