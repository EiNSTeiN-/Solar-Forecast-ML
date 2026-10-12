"""Controlled comparisons for correction guards and the isolated pipeline task.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration, HTTP service, database, or vendor runtime is loaded.
"""
import asyncio
from collections import defaultdict, deque
import copy
import json
from types import SimpleNamespace
import unittest

from pure_reference import classes, functions, isolated_function


MODULE = 'extra_features/sfml_stats/api/corrections.py'


class OwnedCorrectionError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def error_response(code, message, status):
    return {'code': code, 'message': message, 'status': status}


def correction_classes(name='CorrectionsView', bases=(object,)):
    return classes(MODULE, name, {
        'defaultdict': defaultdict, 'deque': deque, 'json': json,
        'CorrectionError': OwnedCorrectionError, 'CSV_MAX_BYTES': 32,
        '_user': lambda request: request.get('hass_user'),
        '_premium_enabled': lambda hass: hass.premium,
        '_error': error_response,
        'time': SimpleNamespace(monotonic=lambda: 100.0),
        'web': SimpleNamespace(json_response=lambda payload, **options: (payload, options)),
        'local_or_trusted_cloud_only': lambda function: function,
    }, bases)


class GuardCheckpointTests(unittest.TestCase):
    def test_capability_snapshot_gate_preserves_fail_closed_behavior(self):
        left, right = functions(MODULE, ('_premium_enabled',), {})
        valid = {'provider_version': 8, 'license_status': 'valid',
                 'premium_full_package': True}
        for snapshot in (None, [], {}, valid, dict(valid, provider_version=7),
                         dict(valid, license_status='invalid'),
                         dict(valid, premium_full_package=1), RuntimeError('owned failure')):
            outputs = []
            for selected in (left, right):
                calls = []

                def read_snapshot():
                    calls.append('snapshot')
                    if isinstance(snapshot, Exception):
                        raise snapshot
                    return copy.deepcopy(snapshot)

                hass = SimpleNamespace(data={'solar_forecast_eai': {
                    'capability_provider': SimpleNamespace(snapshot=read_snapshot)}})
                outputs.append((selected['_premium_enabled'](hass), calls))
            self.assertEqual(outputs[1], outputs[0])
        for data in (None, [], {}, {'capability_provider': object()},
                     {'capability_provider': SimpleNamespace(snapshot=None)}):
            hass = SimpleNamespace(data={'solar_forecast_eai': data})
            self.assertFalse(left['_premium_enabled'](hass))
            self.assertFalse(right['_premium_enabled'](hass))

    def test_json_body_limits_errors_and_numeric_strings(self):
        class Request:
            def __init__(self, body, content_type, length):
                self.body, self.content_type, self.content_length = body, content_type, length
                self.reads = 0

            async def read(self):
                self.reads += 1
                return self.body

        original, recovered = correction_classes()
        cases = [(b'{"energy": 1.25, "count": 7}', 'application/json', None),
                 (b'{}', 'text/plain', 2), (b'{}', 'application/json', 8193),
                 (b' ' * 8193, 'application/json', None),
                 (b'[]', 'application/json', 2), (b'{', 'application/json', 1),
                 (b'\xff', 'application/json', None),
                 (b'{}' + b' ' * 8190, 'application/json', 8192)]
        for body, content_type, length in cases:
            outputs = []
            for cls in (original, recovered):
                request = Request(body, content_type, length)
                try:
                    result = asyncio.run(cls._body(request))
                except OwnedCorrectionError as error:
                    cause = error.__cause__
                    result = (error.code, str(error), type(cause).__name__ if cause else None)
                outputs.append((result, request.reads))
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(asyncio.run(recovered._body(Request(
            b'{"energy": 1.25, "count": 7}', 'application/json', None))),
            {'energy': '1.25', 'count': '7'})

    def test_operation_guards_rate_window_and_error_routes(self):
        class Request(dict):
            method = 'POST'
            scheme = 'https'
            host = 'owned.invalid'

            def __init__(self, admin, origin):
                super().__init__(hass_user=SimpleNamespace(id='owned', is_admin=admin))
                self.headers = {'Origin': origin}

        original, recovered = correction_classes()
        cases = [(False, True, 'https://owned.invalid', [], {'energy': 1}),
                 (True, False, 'https://owned.invalid', [], {'energy': 1}),
                 (True, True, 'https://other.invalid', [], {'energy': 1}),
                 (True, True, 'https://owned.invalid', [41.0] * 20, {'energy': 1}),
                 (True, True, 'https://owned.invalid', [39.0] * 20, {'energy': 1}),
                 (True, True, 'https://owned.invalid', [40.0] * 20, {'energy': 1})]
        for code in ('preview_expired', 'batch_not_undoable', 'event_not_undoable',
                     'correction_conflict', 'idempotency_conflict',
                     'invalid_preview_token', 'second_confirmation_required', 'owned_invalid'):
            cases.append((True, True, 'https://owned.invalid', [],
                          OwnedCorrectionError(code, 'Owned message')))
        for admin, premium, origin, timestamps, result in cases:
            outputs = []
            for cls in (original, recovered):
                cls._rate_windows = defaultdict(deque, owned=deque(timestamps))
                calls = []

                async def operation():
                    calls.append('operation')
                    if isinstance(result, Exception):
                        raise result
                    return copy.deepcopy(result)

                view = cls(SimpleNamespace(premium=premium), None)
                response = asyncio.run(view._run(Request(admin, origin), operation))
                outputs.append((response, list(cls._rate_windows['owned']), calls))
            self.assertEqual(outputs[1], outputs[0])

    def test_history_limit_and_callback_capture(self):
        class Base:
            def __init__(self, store):
                self.store = store

            async def _run(self, request, operation):
                return await operation()

        class Store:
            def __init__(self):
                self.calls = []

            async def async_history(self, limit):
                self.calls.append(limit)
                return ['owned-record', limit]

        original, recovered = correction_classes('CorrectionsHistoryView', (Base,))
        for query in ({}, {'limit': '7'}, {'limit': '-1'}, {'limit': 'not-an-int'}):
            outputs = []
            for cls in (original, recovered):
                store = Store()
                result = asyncio.run(cls(store).get(SimpleNamespace(query=query)))
                outputs.append((result, store.calls))
            self.assertEqual(outputs[1], outputs[0])

    def test_isolated_pipeline_preserves_success_error_and_cancellation(self):
        class Pipeline:
            def __init__(self, result):
                self.result, self.calls = result, []

            async def start_pipeline(self):
                self.calls.append('start')
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        for result in (True, False, None, RuntimeError('owned failure'), asyncio.CancelledError()):
            outputs = []
            for index in (0, 1):
                pipeline = Pipeline(result)
                messages = []
                logger = SimpleNamespace(
                    warning=lambda message: messages.append(('warning', message)),
                    info=lambda message: messages.append(('info', message)))
                pair = isolated_function(
                    'extra_features/solar_forecast_eai/coordinator.py',
                    'extra_features/solar_forecast_eai/pipeline_start.py',
                    '<module>/SolarForecastEAICoordinator#1/_background_initialization#1/_start_pipeline_background#1',
                    {'_LOGGER': logger}, closure_values={
                        'self': SimpleNamespace(weather_pipeline_manager=pipeline)})
                try:
                    value = asyncio.run(pair[index]())
                except asyncio.CancelledError:
                    value = 'cancelled'
                outputs.append((value, pipeline.calls, messages))
            self.assertEqual(outputs[1], outputs[0])


if __name__ == '__main__':
    unittest.main()
