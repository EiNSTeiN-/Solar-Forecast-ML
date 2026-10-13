"""Paired candidate-selection checks with an owned transactional database.

Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
Only the statically proved owner tree executes, with supplied dependencies.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest

from pure_reference import isolated_function


class CandidateReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clock = SimpleNamespace(now=lambda: datetime(2026, 10, 12, 9, 30))
        cls.pair = isolated_function(
            "data/db_manager.py",
            "owner_recipes/data/db_manager.py",
            "<module>/DatabaseManager#1/reconcile_pending_model_candidates#1",
            {"SafeDateTimeUtil": clock},
        )

    def outcome(
        self,
        function,
        compatible,
        incompatible,
        *,
        pending=(),
        dispositions=(),
        selected=None,
        failure="",
        suppress=False
    ):
        events = []
        fetch_count = 0

        def fail(stage):
            if failure == stage:
                raise ValueError("owned " + stage)
            if failure == "cancel-" + stage:
                raise asyncio.CancelledError("owned " + stage)

        class Transaction:
            async def __aenter__(self):
                events.append("enter")
                await asyncio.sleep(0)
                fail("enter")

            async def __aexit__(self, kind, error, traceback):
                events.append(("exit", kind.__name__ if kind else None))
                await asyncio.sleep(0)
                fail("exit")
                return suppress

        class Database:
            def transaction(self):
                events.append("transaction")
                fail("transaction")
                return Transaction()

            async def fetchall(self, query, *arguments):
                nonlocal fetch_count
                fetch_count += 1
                events.append(("fetchall", query, arguments))
                await asyncio.sleep(0)
                fail("pending" if fetch_count == 1 else "dispositions")
                return (
                    [(item,) for item in pending] if fetch_count == 1 else dispositions
                )

            async def fetchone(self, query, *arguments):
                events.append(("fetchone", query, arguments))
                await asyncio.sleep(0)
                fail("select")
                return selected

            async def executemany(self, query, rows):
                events.append(("executemany", query, rows))
                await asyncio.sleep(0)
                fail("write")

        try:
            value = asyncio.run(
                function(
                    Database(),
                    compatible_candidate_ids=compatible,
                    incompatible_candidate_ids=incompatible,
                )
            )
            result = ("return", value)
        except BaseException as error:
            # Suppression before selection leaves the original local unbound.
            # Its spelling differs; compare the exception type, not that name.
            message = None if isinstance(error, UnboundLocalError) else str(error)
            result = ("raise", type(error).__name__, message)
        return result, events

    def assert_pair(self, compatible, incompatible, **options):
        outcomes = [
            self.outcome(function, compatible, incompatible, **options)
            for function in self.pair
        ]
        self.assertEqual(*outcomes)
        return outcomes[0]

    def test_validation_and_selection(self):
        invalid = (
            (("a", "a"), ()),
            ((), ("a", "a")),
            (("a",), ("a",)),
            (("",), ()),
            ((), ("",)),
            ((1,), ()),
            ((), (None,)),
        )
        for compatible, incompatible in invalid:
            with self.subTest(compatible=compatible, incompatible=incompatible):
                result, events = self.assert_pair(compatible, incompatible)
                self.assertEqual(
                    result,
                    (
                        "raise",
                        "ValueError",
                        "runtime candidate ids must be unique and valid",
                    ),
                )
                self.assertEqual(events, [])

        scenarios = (
            ((), (), (), (), None, None),
            (("a",), (), ("a",), (), ("a",), "a"),
            (("a", "b"), ("z",), ("a", "b", "z"), (), ("b",), "b"),
            ((), ("b", "a"), ("a", "b"), (), None, None),
            (("a",), ("old",), ("a",), (("old", "incompatible_runtime"),), None, None),
        )
        for (
            compatible,
            incompatible,
            pending,
            dispositions,
            selected,
            expected,
        ) in scenarios:
            with self.subTest(compatible=compatible, incompatible=incompatible):
                result, events = self.assert_pair(
                    compatible,
                    incompatible,
                    pending=pending,
                    dispositions=dispositions,
                    selected=selected,
                )
                self.assertEqual(result, ("return", expected))
                self.assertEqual(events[-1], ("exit", None))
                writes = [event for event in events if event[0] == "executemany"]
                expected_ids = sorted(set(incompatible) & set(pending))
                self.assertEqual(len(writes), bool(expected_ids))
                if writes:
                    self.assertEqual(
                        writes[0][2],
                        [
                            (
                                item,
                                "incompatible_runtime",
                                expected,
                                "2026-10-12T09:30:00",
                            )
                            for item in expected_ids
                        ],
                    )

        result, _ = self.assert_pair(("a",), (), pending=("a", "b"))
        self.assertEqual(result[2], "every pending candidate must be runtime-verified")
        result, _ = self.assert_pair(("a",), ("old",), pending=("a",))
        self.assertEqual(result[2], "incompatible candidate is not pending")

    def test_transaction_faults_cancellation_and_suppression(self):
        for stage in (
            "transaction",
            "enter",
            "pending",
            "dispositions",
            "select",
            "write",
            "exit",
        ):
            for prefix in ("", "cancel-"):
                for suppress in (False, True):
                    with self.subTest(stage=stage, prefix=prefix, suppress=suppress):
                        self.assert_pair(
                            ("a",),
                            ("b", "old"),
                            pending=("a", "b"),
                            dispositions=(("old", "incompatible_runtime"),),
                            selected=("a",),
                            failure=prefix + stage,
                            suppress=suppress,
                        )
