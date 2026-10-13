"""Compare transaction ownership and cleanup after the complete source proof.

Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
Only selected functions execute, with owned asynchronous connection fixtures.
"""

import asyncio
from contextlib import asynccontextmanager
import threading
from types import FunctionType, SimpleNamespace
import unittest

from pure_reference import verified_code


class DatabaseTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pair = []
        proved_modules = verified_code("data/db_manager.py")
        from bytecode_fidelity import units

        for module in proved_modules:
            codes = dict(units(module))
            namespace = dict(
                asyncio=asyncio,
                threading=threading,
                asynccontextmanager=asynccontextmanager,
                monotonic=lambda: 100.0,
                _LOGGER=SimpleNamespace(debug=lambda *args: None),
            )
            checker = FunctionType(
                codes["<module>/_is_sqlite_authorizer_error#1"], namespace
            )
            namespace["_is_sqlite_authorizer_error"] = checker
            function = FunctionType(
                codes["<module>/DatabaseManager#1/transaction#1"], namespace
            )
            function.__kwdefaults__ = {"deadline": None}
            cls.pair.append(asynccontextmanager(function))

    def outcome(
        self,
        function,
        *,
        nested=False,
        fault="",
        deadline_mode="none",
        use_lock=False,
        closed=False,
        unmanaged=False
    ):
        events = []
        failures = set()

        def fail(stage):
            if stage in failures:
                return
            if fault == stage or fault == "cancel-" + stage:
                failures.add(stage)
                kind = (
                    asyncio.CancelledError
                    if fault.startswith("cancel-")
                    else ValueError
                )
                raise kind("owned " + stage)

        class Connection:
            in_transaction = False
            progress = None

            async def execute(self, sql):
                events.append(("execute", sql))
                await asyncio.sleep(0)
                if sql == "BEGIN IMMEDIATE":
                    if fault == "begin-active":
                        self.in_transaction = True
                        raise ValueError("owned begin active")
                    fail("begin")
                    self.in_transaction = True
                elif sql.startswith("SAVEPOINT "):
                    fail("savepoint")
                elif sql.startswith("RELEASE "):
                    fail("release")
                elif sql.startswith("ROLLBACK TO "):
                    fail("rollback-savepoint")

            async def commit(self):
                events.append("commit")
                await asyncio.sleep(0)
                fail("commit")
                self.in_transaction = False

            async def rollback(self):
                events.append("rollback")
                await asyncio.sleep(0)
                fail("rollback")
                self.in_transaction = False

            async def set_progress_handler(self, callback, cadence):
                events.append(("progress", callback is not None, cadence))
                await asyncio.sleep(0)
                fail("progress-on" if callback is not None else "progress-off")
                self.progress = callback

        class Gate:
            held = False

            async def acquire(self):
                events.append("lock-acquire")
                await asyncio.sleep(0)
                fail("lock-acquire")
                self.held = True

            def release(self):
                events.append("lock-release")
                self.held = False

            async def __aenter__(self):
                await self.acquire()

            async def __aexit__(self, *args):
                self.release()

        class Database:
            _transaction_depth = 0
            _transaction_owner = None
            _transaction_abort_event = None
            _BEGIN_RETRY_BUDGET_SECONDS = 3.0
            _LOCK_RETRY_BUDGET_SECONDS = 5.0

            def __init__(self):
                self.connection = Connection()
                self._db = None if closed else self.connection
                self.gate = Gate()
                self._write_lock = self.gate if use_lock else None

            def has_active_unmanaged_transaction(self):
                events.append("unmanaged-check")
                return unmanaged

            async def _retry_on_locked(self, operation, *, budget=None):
                events.append(("retry", budget))
                return await operation()

            async def _await_transaction_cleanup(self, operation):
                events.append("cleanup")
                return await operation()

            async def _probe_database_connection(self):
                events.append("probe")
                fail("probe")

            async def _clear_connection_authorizer(self):
                events.append("clear-authorizer")

        database = Database()

        async def run():
            deadline = {
                "none": None,
                "future": 101.0,
                "expired": 99.0,
                "timeout": 100.001,
            }[deadline_mode]
            arguments = {} if deadline is None else {"deadline": deadline}
            async with function(database, **arguments) as value:
                events.append(
                    (
                        "body",
                        value,
                        database._transaction_depth,
                        database._transaction_owner is asyncio.current_task(),
                    )
                )
                if nested:
                    async with function(database) as inner:
                        events.append(
                            ("nested-body", inner, database._transaction_depth)
                        )
                        if fault == "rollback-savepoint":
                            raise ValueError("owned nested-body")
                        fail("nested-body")
                fail("body")
                if deadline_mode == "timeout":
                    await asyncio.sleep(0.02)
            return "completed"

        try:
            result = ("return", asyncio.run(run()))
        except BaseException as error:
            result = ("raise", type(error).__name__, str(error))
        return (
            result,
            events,
            (
                database._transaction_depth,
                database._transaction_owner is None,
                database._transaction_abort_event is None,
                database.connection.in_transaction,
                database.connection.progress is None,
                database.gate.held,
            ),
        )

    def check_case(self, **case):
        before = self.outcome(self.pair[0], **case)
        after = self.outcome(self.pair[1], **case)
        self.assertEqual(before, after)
        return after

    def test_transaction_lock_ownership_and_rollback(self):
        for use_lock in (False, True):
            for fault in (
                "",
                "begin",
                "begin-active",
                "body",
                "cancel-body",
                "commit",
                "cancel-commit",
                "lock-acquire",
            ):
                with self.subTest(use_lock=use_lock, fault=fault):
                    result = self.check_case(use_lock=use_lock, fault=fault)
                    self.assertEqual(result[2][:3], (0, True, True))
                    self.assertFalse(result[2][-1])
        for closed, unmanaged in ((True, False), (False, True)):
            self.check_case(closed=closed, unmanaged=unmanaged)

    def test_nested_savepoint_cleanup_preserves_outer_owner(self):
        for use_lock in (False, True):
            for fault in (
                "",
                "savepoint",
                "release",
                "nested-body",
                "cancel-nested-body",
                "rollback-savepoint",
            ):
                with self.subTest(use_lock=use_lock, fault=fault):
                    result = self.check_case(
                        nested=True, use_lock=use_lock, fault=fault
                    )
                    self.assertEqual(result[2][:3], (0, True, True))
                    self.assertFalse(result[2][-1])

    def test_deadline_progress_handler_restoration_and_expiry(self):
        for use_lock in (False, True):
            for mode in ("future", "expired", "timeout"):
                for fault in ("", "body", "cancel-body", "progress-on", "probe"):
                    with self.subTest(use_lock=use_lock, mode=mode, fault=fault):
                        result = self.check_case(
                            use_lock=use_lock, deadline_mode=mode, fault=fault
                        )
                        self.assertEqual(result[2][:3], (0, True, True))
                        self.assertTrue(result[2][-2])
                        self.assertFalse(result[2][-1])
