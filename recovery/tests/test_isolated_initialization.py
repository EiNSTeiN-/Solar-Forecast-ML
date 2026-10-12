"""Paired behavior checks for reconstructed initialization and dashboard trees.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only verified code objects run. All imports, files, tasks and services are owned
stand-ins; no integration module or actual external service is imported.
"""
import asyncio
import copy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest

from pure_reference import isolated_function


NOW = datetime(2026, 10, 11, 12, 0)


def logger_for(events):
    def log(level):
        def emit(*args, **options):
            # Separate executions create distinct exception instances. Compare
            # their concrete type/message rather than object identity.
            arguments = tuple((type(value).__name__, str(value))
                              if isinstance(value, BaseException) else value for value in args)
            events.append(('log', level, arguments, options))
        return emit
    return SimpleNamespace(**{level: log(level) for level in ('info', 'warning', 'error', 'debug')})


class IsolatedInitializationTests(unittest.TestCase):
    def test_dashboard_preserves_kpis_reader_file_and_error_paths(self):
        scenarios = ('normal', 'empty', 'daily_error', 'prediction_error',
                     'missing_file', 'bad_json', 'import_error', 'invalid_total')
        for scenario in scenarios:
            outputs = []
            for index in (0, 1):
                events, responses = [], []

                class Path:
                    def __init__(self, parts):
                        self.parts = parts

                    def __truediv__(self, part):
                        return Path(self.parts + '/' + part)

                    def exists(self):
                        events.append(('exists', self.parts))
                        return scenario != 'missing_file'

                class Reader:
                    async def async_get_daily_summaries(self, **options):
                        events.append(('daily', options))
                        if scenario == 'daily_error':
                            raise RuntimeError('owned daily failure')
                        if scenario == 'empty':
                            return []
                        fields = dict(predicted_total_kwh=9, actual_total_kwh=8,
                            accuracy_percent=90, evaluation_predicted_kwh=7,
                            evaluation_actual_kwh=6, evaluation_coverage_percent=80,
                            excluded_hours_count=1, excluded_mppt_hours_count=2,
                            evaluation_hours_count=12, production_candidate_hours_count=14,
                            missing_actual_hours_count=0, excluded_weather_alert_hours_count=0,
                            excluded_inverter_clipped_hours_count=1,
                            excluded_reason_breakdown_json='{}')
                        if scenario == 'invalid_total':
                            fields['actual_total_kwh'] = None
                        return [SimpleNamespace(date=NOW.date(), **fields),
                                SimpleNamespace(date=NOW.date() - timedelta(days=1),
                                    **dict(fields, actual_total_kwh=4, accuracy_percent=None,
                                           evaluation_coverage_percent=None,
                                           excluded_mppt_hours_count=None))]

                    async def async_get_hourly_predictions(self, *, target_date):
                        events.append(('hourly', target_date))
                        if scenario == 'prediction_error':
                            raise RuntimeError('owned hourly failure')
                        return [SimpleNamespace(target_datetime=NOW, target_hour=12,
                            target_date=target_date, prediction_kwh=1.5, actual_kwh=1.0)]

                class File:
                    async def read(self):
                        events.append(('read',))
                        if scenario == 'bad_json':
                            return '{'
                        return json.dumps({'prices': [{'price_net': 0}, {'price_net': -5},
                                                     {'price_net': 8}, {}]})

                class Context:
                    async def __aenter__(self):
                        events.append(('enter',))
                        return File()

                    async def __aexit__(self, type_, error, traceback):
                        events.append(('exit', type_.__name__ if type_ else None))
                        return False

                def open_file(path, mode):
                    events.append(('open', path.parts, mode))
                    return Context()

                def import_owned(name, globals=None, locals=None, fromlist=(), level=0):
                    events.append(('import', name, level))
                    if name == 'aiofiles' and level == 0 and scenario != 'import_error':
                        return SimpleNamespace(open=open_file)
                    raise ImportError('owned missing module')

                connection = SimpleNamespace(
                    send_result=lambda id_, result: responses.append(('result', id_, copy.deepcopy(result))),
                    send_error=lambda *args: responses.append(('error', *args)))
                functions = isolated_function(
                    'extra_features/sfml_stats/api/websocket.py',
                    'extra_features/sfml_stats/api/websocket_dashboard.py',
                    '<module>/websocket_get_dashboard_data#1', {
                        '_get_config_paths': lambda hass: (Path('solar'), Path('prices')),
                        '_get_solar_reader': lambda hass: Reader(),
                        'ha_now': lambda hass: NOW, 'ha_today': lambda hass: NOW.date(),
                        'timedelta': timedelta, 'json': json, '_LOGGER': logger_for(events),
                    }, builtins_overrides={'__import__': import_owned})
                value = asyncio.run(functions[index](None, connection, {'id': 7}))
                outputs.append((value, responses, events))
            self.assertEqual(outputs[1], outputs[0], scenario)
            if scenario == 'normal':
                kpis = outputs[1][1][0][2]['kpis']
                self.assertEqual(kpis, {'week_production': 12, 'avg_accuracy': 90,
                    'avg_evaluation_coverage_percent': 80, 'avg_excluded_mppt_hours': 1,
                    'price_current': 8, 'price_min': -5, 'price_max': 8})

    def test_background_initialization_preserves_continuations_and_tasks(self):
        scenarios = ('normal', 'already_ready', 'no_ai', 'services_false',
                     'services_error', 'setup_false', 'setup_error', 'weather_error',
                     'delayed_error', 'peak_error', 'schedule_error', 'cache_error',
                     'start_error', 'cancel_services')
        for scenario in scenarios:
            outputs = []
            for index in (0, 1):
                events, queued, listeners = [], [], []

                class Lock:
                    async def __aenter__(self):
                        events.append(('lock', 'enter'))

                    async def __aexit__(self, type_, error, traceback):
                        events.append(('lock', 'exit', type_.__name__ if type_ else None))
                        return False

                class Data:
                    _db_manager = 'owned-db'
                    data_dir = 'owned-data'

                    async def get_hourly_predictions(self, target_date):
                        events.append(('predictions', target_date))
                        if scenario == 'cache_error':
                            raise RuntimeError('owned cache failure')
                        return [{'target_hour': 9, 'prediction_kwh': 0},
                                {'target_hour': 10, 'prediction_kwh': 2},
                                {'target_hour': 11, 'prediction_kwh': 3}]

                class Pipeline:
                    weather_corrector = 'owned-corrector'

                    def __init__(self, **options):
                        events.append(('pipeline', options['db_manager']))

                    async def async_setup(self):
                        events.append(('setup',))
                        if scenario == 'setup_error':
                            raise RuntimeError('owned setup failure')
                        return scenario != 'setup_false'

                    async def start_pipeline(self):
                        events.append(('start',))
                        if scenario == 'start_error':
                            raise RuntimeError('owned start failure')
                        return True

                class Weather:
                    def __init__(self, **options):
                        events.append(('weather', options['latitude'], options['longitude']))

                    async def initialize(self):
                        events.append(('weather_init',))
                        if scenario == 'weather_error':
                            raise RuntimeError('owned weather failure')

                class Tasks:
                    def set_pipeline_corrector(self, corrector):
                        events.append(('corrector', corrector))

                    async def schedule_all_tasks(self):
                        events.append(('schedule',))
                        if scenario == 'schedule_error':
                            raise RuntimeError('owned scheduling failure')

                class AI:
                    async def train_model(self):
                        events.append(('train',))

                class Coordinator:
                    def __init__(self):
                        self.hass = SimpleNamespace(config=SimpleNamespace(latitude=45, longitude=-73))
                        self._services_initialized = scenario == 'already_ready'
                        self._services_init_lock = Lock()
                        self.data_manager, self.scheduled_tasks = Data(), Tasks()
                        self.ai_predictor = None if scenario == 'no_ai' else AI()
                        self.entry, self.error_handler = 'owned-entry', 'owned-handler'
                        self._ml_ready, self.heating_capacity_kw = True, 6
                        self._hourly_predictions_cache = None
                        self._unsub_weekly_retraining_listener = None

                    async def _initialize_services(self):
                        events.append(('services',))
                        if scenario == 'services_error':
                            raise RuntimeError('owned services failure')
                        if scenario == 'cancel_services':
                            raise asyncio.CancelledError()
                        return scenario != 'services_false'

                    async def _schedule_delayed_sensor_init(self):
                        events.append(('delayed',))
                        if scenario == 'delayed_error':
                            raise RuntimeError('owned delayed failure')

                    async def _setup_power_peak_tracking(self):
                        events.append(('peak',))
                        if scenario == 'peak_error':
                            raise RuntimeError('owned peak failure')

                    def _create_background_task(self, coroutine, *, name):
                        events.append(('create', name))
                        queued.append(coroutine)

                def import_owned(name, globals=None, locals=None, fromlist=(), level=0):
                    events.append(('import', name, level))
                    if name == 'data.data_weather_pipeline_manager' and level == 1:
                        return SimpleNamespace(WeatherDataPipelineManager=Pipeline)
                    raise ImportError('unexpected owned import')

                def track(hass, callback, **options):
                    events.append(('listener', options))
                    listeners.append(callback)
                    return 'owned-unsubscribe'

                coordinator = Coordinator()
                functions = isolated_function(
                    'extra_features/solar_forecast_eai/coordinator.py',
                    'extra_features/solar_forecast_eai/background_initialization.py',
                    '<module>/SolarForecastEAICoordinator#1/_background_initialization#1', {
                        '_LOGGER': logger_for(events), 'WeatherService': Weather,
                        'dt_util': SimpleNamespace(now=lambda: NOW), 'timedelta': timedelta,
                        'callback': lambda callback: callback, 'async_track_time_change': track,
                    }, builtins_overrides={'__import__': import_owned})

                async def run():
                    try:
                        value = await functions[index](coordinator)
                    except asyncio.CancelledError:
                        value = 'cancelled'
                    for listener in listeners:
                        listener(NOW + timedelta(days=1))  # Monday: no retraining.
                        listener(NOW)  # Sunday: one queued retraining task.
                    for coroutine in queued:
                        await coroutine
                    return value

                value = asyncio.run(run())
                outputs.append((value, events, copy.deepcopy(coordinator._hourly_predictions_cache),
                                coordinator._unsub_weekly_retraining_listener))
            self.assertEqual(outputs[1], outputs[0], scenario)
            if scenario == 'services_error':
                self.assertIn(('pipeline', 'owned-db'), outputs[1][1])
            if scenario == 'normal':
                self.assertEqual(outputs[1][2]['peak_hour_today'], {'hour': 11, 'kwh': 3})
                self.assertEqual(outputs[1][1].count(('train',)), 1)


if __name__ == '__main__':
    unittest.main()
