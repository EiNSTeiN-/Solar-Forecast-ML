"""Behavior comparisons for newly verified modules and a retained API method.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only selected code objects run, with explicit controlled dependencies.
"""
import asyncio
import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from pure_reference import classes, functions, isolated_function


class AnnotationCheckpointTests(unittest.TestCase):
    def test_csv_community_forwards_slots_and_source_metadata(self):
        class Base:
            label = "Owned tariff"
            has_spot_component = True
            needs_network = True

            def __init__(self, result):
                self.result, self.calls = result, []

            async def async_get_slots(self, start, end):
                self.calls.append((start, end))
                return self.result

        original, recovered = classes(
            'extra_features/grid_price_monitor/tariffs/csv_community.py',
            'CsvCommunitySource', {'TARIFF_MODE_CSV_COMMUNITY': 'csv_community'})
        start = datetime(2026, 10, 11)
        for result in (None, [], [{'price': -5}, {'price': 20}]):
            outputs = []
            for cls in (original, recovered):
                base = Base(copy.deepcopy(result))
                source = cls(base)
                slots = asyncio.run(source.async_get_slots(start, start + timedelta(days=1)))
                outputs.append((slots, base.calls, source.label,
                                source.has_spot_component, source.needs_network))
                self.assertIs(slots, base.result)
            self.assertEqual(outputs[1], outputs[0])

    def test_model_reaction_states_keep_database_and_training_order(self):
        class Database:
            def __init__(self, request, pending, covered):
                self.request, self.pending, self.covered = request, pending, covered
                self.calls = []

            async def get_or_create_confirmed_model_retrain_request(self, through_date):
                self.calls.append(('request', through_date))
                return copy.deepcopy(self.request)

            async def get_pending_model_candidate_id(self):
                self.calls.append(('pending',))
                return self.pending

            async def model_candidate_covers_retrain_request(self, request_id, candidate_id):
                self.calls.append(('covers', request_id, candidate_id))
                return self.covered

            async def bind_model_retrain_request(self, request_id, candidate_id):
                self.calls.append(('bind', request_id, candidate_id))

        class Predictor:
            _latest_model_candidate_id = 'replacement'

            def __init__(self, success, error):
                self.success, self.error, self.calls = success, error, []

            async def train_model_for_reaction(self, request_id, candidate_id, event_date):
                self.calls.append((request_id, candidate_id, event_date))
                return SimpleNamespace(success=self.success, error_message=self.error)

        original, recovered = classes('ai/ai_model_reaction.py', 'ModelReactionHandler',
            {'ModelReactionOutcome': lambda **values: values})
        request = {'request_id': 'owned-request', 'status': 'pending',
                   'last_event_date': '2026-10-10'}
        scenarios = [(None, 'candidate', False, True, None),
                     (dict(request, status='bound', bound_candidate_id='old'), 'candidate', False, True, None),
                     (request, None, False, True, None),
                     (request, 'candidate', True, True, None),
                     (request, 'candidate', False, True, None),
                     (request, 'candidate', False, False, None),
                     (request, 'candidate', False, False, 'owned failure')]
        for record, pending, covered, success, error in scenarios:
            outputs = []
            for cls in (original, recovered):
                db = Database(record, pending, covered)
                predictor = Predictor(success, error)
                result = asyncio.run(cls(db).process('2026-10-11', predictor))
                outputs.append((result, db.calls, predictor.calls))
            self.assertEqual(outputs[1], outputs[0])

    def test_admin_gate_and_error_payload_are_preserved(self):
        web = SimpleNamespace(json_response=lambda payload, **options: (payload, options))
        left, right = functions('extra_features/sfml_stats/api/dev_mode_views.py',
                                 ('_admin_blocked',), {'web': web, '_NO_STORE': {'Cache-Control': 'no-store'}})
        for user in (None, SimpleNamespace(is_admin=False), SimpleNamespace(is_admin=True),
                     SimpleNamespace()):
            self.assertEqual(right['_admin_blocked']({'hass_user': user}),
                             left['_admin_blocked']({'hass_user': user}))
        left, right = functions('extra_features/sfml_stats/api/ems.py', ('_error',), {'web': web})
        for status in (400, 403, 409):
            self.assertEqual(right['_error']('owned_code', 'Owned message', status),
                             left['_error']('owned_code', 'Owned message', status))

    def test_retained_revert_method_keeps_all_success_and_error_responses(self):
        class OwnedError(Exception):
            def __init__(self, key):
                self.key = key

        class Request:
            def __init__(self, payload, blocked):
                self.payload, self.blocked = payload, blocked

            async def json(self):
                if isinstance(self.payload, Exception):
                    raise self.payload
                return copy.deepcopy(self.payload)

        class Coordinator:
            def __init__(self, result, gate):
                self.result, self.gate, self.calls = result, gate, []

            async def async_revert_correction(self, correction_id):
                self.calls.append(correction_id)
                if isinstance(self.result, Exception):
                    raise self.result
                return copy.deepcopy(self.result)

        web = SimpleNamespace(json_response=lambda payload, **options: (payload, options))
        original, recovered = isolated_function(
            'extra_features/sfml_stats/api/gpm.py',
            'extra_features/sfml_stats/api/gpm_revert_post.py',
            '<module>/GPMRevertCorrectionView#1/post#1',
            {'web': web, '_admin_forbidden': lambda request: request.blocked,
             '_gpm_coordinator': lambda hass: hass,
             'revert_gate': lambda coordinator: coordinator.gate})
        cases = [(ValueError('invalid json'), None, None, None),
                 ([], None, None, None), ({}, None, None, None),
                 ({'correction_id': 'bad'}, None, None, None),
                 ({'correction_id': '7'}, {'restored': True}, None, None),
                 ({'correction_id': 7}, None, None, None),
                 ({'correction_id': 7}, [], None, None),
                 ({'correction_id': 7}, OwnedError('demo_forbidden'), None, None),
                 ({'correction_id': 7}, OwnedError('correction_license_required'), None, None),
                 ({'correction_id': 7}, OwnedError('owned_failure'), None, None),
                 ({'correction_id': 7}, RuntimeError('owned failure'), None, None),
                 ({'correction_id': 7}, {'restored': True}, {'blocked': True}, None),
                 ({'correction_id': 7}, {'restored': True}, None, {'gated': True})]
        for payload, result, blocked, gate in cases:
            outputs = []
            for function in (original, recovered):
                coordinator = Coordinator(result, gate)
                value = asyncio.run(function(SimpleNamespace(hass=coordinator), Request(payload, blocked)))
                outputs.append((value, coordinator.calls))
            self.assertEqual(outputs[1], outputs[0])


if __name__ == '__main__':
    unittest.main()
