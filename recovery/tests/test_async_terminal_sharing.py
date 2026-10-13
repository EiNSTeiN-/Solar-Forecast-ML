"""Owned async behavior evidence for the approved terminal-layout variance."""

import asyncio
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ['SFML_UNPACKER_DIR']) / 'oneshot'))
from bytecode_fidelity import compare, compile_source


SPLIT = '''async def work(owner, owns):
    try:
        async with owner.context() as response:
            await response.read()
        raise owner.failure
    finally:
        if owns:
            await owner.close()
'''
SHARED = '''async def work(owner, owns):
    try:
        async with owner.context() as response:
            await response.read()
        raise owner.failure
    finally:
        while True:
            if owns:
                await owner.close()
            break
'''


def function(source):
    code = next(value for value in compile_source(source, 'owned-async-terminal').co_consts
                if isinstance(value, types.CodeType))
    return types.FunctionType(code, {})


class Owner:
    def __init__(self, case):
        self.case = case
        self.events = []
        self.ready = asyncio.Event()
        self.failure = LookupError('owned original')

    async def stage(self, name):
        self.events.append(name + ':start')
        if self.case.get('cancel_at') == name:
            self.ready.set()
            await asyncio.Future()
        await asyncio.sleep(0)
        self.events.append(name + ':resume')
        failure = self.case.get(name + '_error')
        if failure:
            raise failure('owned ' + name)

    def context(self):
        self.events.append('context')
        return Context(self)

    async def read(self):
        await self.stage('read')

    async def close(self):
        await self.stage('close')


class Context:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        await self.owner.stage('enter')
        return self.owner

    async def __aexit__(self, kind, error, traceback):
        self.owner.events.append(('exit-received', kind.__name__ if kind else None))
        await self.owner.stage('exit')
        return self.owner.case.get('suppress', False)


class Flag:
    def __init__(self, owner):
        self.owner = owner

    def __bool__(self):
        self.owner.events.append('truth')
        failure = self.owner.case.get('truth_error')
        if failure:
            raise failure('owned truth')
        return self.owner.case.get('owns', True)


def describe(error, original, seen=None):
    if error is None:
        return None
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {'cycle': True}
    seen.add(id(error))
    return dict(kind=type(error).__name__, args=error.args,
                original_identity=error is original,
                suppressed_context=error.__suppress_context__,
                context=describe(error.__context__, original, seen.copy()),
                cause=describe(error.__cause__, original, seen.copy()))


async def invoke(work, case):
    owner = Owner(case)
    task = asyncio.create_task(work(owner, Flag(owner)))
    if case.get('cancel_at'):
        await asyncio.wait_for(owner.ready.wait(), 5)
        task.cancel('owned cancellation')
    try:
        outcome = {'return': await task}
    except BaseException as error:
        outcome = {'error': describe(error, owner.failure)}
    outcome.update(events=owner.events, task_cancelled=task.cancelled())
    return outcome


class AsyncTerminalSharingTests(unittest.TestCase):
    CASES = (
        ('cleanup-taken', {}), ('cleanup-skipped', {'owns': False}),
        ('truth-value-error', {'truth_error': ValueError}),
        ('truth-stop-iteration', {'truth_error': StopIteration}),
        ('response-error', {'read_error': ValueError}),
        ('response-error-suppressed', {'read_error': ValueError, 'suppress': True}),
        ('exit-replaces-response-error', {'read_error': ValueError, 'exit_error': OSError}),
        ('close-value-error', {'close_error': ValueError}),
        ('close-stop-iteration', {'close_error': StopIteration}),
        ('close-raised-cancellation', {'close_error': asyncio.CancelledError}),
        ('cancel-suspended-close', {'cancel_at': 'close'}),
        ('cancel-suspended-response', {'cancel_at': 'read'}),
        ('cancel-suspended-response-suppressed', {'cancel_at': 'read', 'suppress': True}),
    )

    def test_async_errors_context_and_actual_cancellation_agree(self):
        split, shared = function(SPLIT), function(SHARED)
        self.assertTrue(compare(split.__code__, shared.__code__)['all_units_match'])

        async def check():
            outcomes = {}
            for label, case in self.CASES:
                with self.subTest(case=label):
                    left, right = await invoke(split, case), await invoke(shared, case)
                    self.assertEqual(left, right)
                    self.assertEqual(left['events'].count('truth'), 1)
                    self.assertLessEqual(left['events'].count('close:start'), 1)
                    outcomes[label] = left
            self.assertEqual(outcomes['cancel-suspended-close']['error']['kind'], 'CancelledError')
            self.assertTrue(outcomes['cancel-suspended-close']['error']['context']['original_identity'])
            self.assertTrue(outcomes['cancel-suspended-response']['task_cancelled'])
            self.assertEqual(outcomes['cancel-suspended-response']['events'][-1], 'close:resume')
            self.assertTrue(outcomes['cancel-suspended-response-suppressed']['error']['original_identity'])
            for label in ('truth-stop-iteration', 'close-stop-iteration'):
                self.assertEqual(outcomes[label]['error']['kind'], 'RuntimeError')

        asyncio.run(check())

    def test_changed_guard_and_swallowed_cleanup_errors_fail_both_checks(self):
        shared = function(SHARED)
        negatives = (
            (SHARED.replace('if owns:', 'if not owns:'), {'owns': False}),
            (SHARED.replace('                await owner.close()',
                            '                try:\n                    await owner.close()\n'
                            '                except Exception:\n                    pass'),
             {'close_error': ValueError}),
        )

        async def check():
            for source, case in negatives:
                changed = function(source)
                self.assertFalse(compare(shared.__code__, changed.__code__)['all_units_match'])
                self.assertNotEqual(await invoke(shared, case), await invoke(changed, case))

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
