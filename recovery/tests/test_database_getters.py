"""Paired isolated database lifecycle checks with owned import stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, runtime, real database or Home Assistant is imported.
"""

import asyncio
from types import SimpleNamespace
import unittest

from pure_reference import isolated_function

GETTERS = (
    (
        "extra_features/sfml_stats/core/forecast_collector.py",
        "extra_features/sfml_stats/core/forecast_db_manager.py",
        "ForecastComparisonCollector",
        "_hass",
        2,
    ),
    (
        "extra_features/sfml_stats/core/hourly_aggregator.py",
        "extra_features/sfml_stats/core/hourly_db_manager.py",
        "HourlyBillingAggregator",
        "_hass",
        2,
    ),
    (
        "extra_features/sfml_stats/power_sources_collector.py",
        "extra_features/sfml_stats/power_sources_db_manager.py",
        "PowerSourcesCollector",
        "hass",
        1,
    ),
)


class DatabaseGetterTests(unittest.TestCase):
    def run_pair(self, specification, initial, failure=""):
        module, source, owner, hass_attribute, import_level = specification
        events = []
        connected = initial == "connected"
        marker = object()

        def fail(stage):
            if failure == stage:
                raise ValueError("owned " + stage)
            if failure == "cancel-" + stage:
                raise asyncio.CancelledError("owned " + stage)

        class Manager:
            @property
            def is_connected(self):
                events.append("connected")
                fail("connected")
                return connected

            async def connect(self):
                nonlocal connected
                events.append("connect")
                await asyncio.sleep(0)
                fail("connect")
                connected = True

        manager = Manager()

        def get_manager():
            events.append("get")
            fail("get")
            return None if initial == "missing" else manager

        class FactoryMeta(type):
            @property
            def get_instance(cls):
                events.append("lookup")
                fail("lookup")

                async def create(hass):
                    nonlocal connected
                    self.assertIs(hass, marker)
                    events.append("create")
                    await asyncio.sleep(0)
                    fail("create")
                    connected = True
                    return manager

                return create

        class Factory(metaclass=FactoryMeta):
            pass

        def owned_import(name, globals_, locals_, fromlist, level):
            self.assertEqual(name, "storage.db_connection_manager")
            self.assertEqual(fromlist, ("DatabaseConnectionManager", "get_manager"))
            self.assertEqual(level, import_level)
            events.append(("import", name, fromlist, level))
            fail("import")
            return SimpleNamespace(
                DatabaseConnectionManager=Factory, get_manager=get_manager
            )

        class Collector:
            def __getattr__(self, name):
                self_outer.assertEqual(name, hass_attribute)
                events.append("hass")
                fail("hass")
                return marker

        self_outer = self
        pair = isolated_function(
            module,
            source,
            f"<module>/{owner}#1/_get_db_manager#1",
            {},
            builtins_overrides={"__import__": owned_import},
        )
        outputs = []
        for function in pair:
            events.clear()
            connected = initial == "connected"
            try:
                result = asyncio.run(function(Collector()))
                self.assertIs(result, manager)
                self.assertTrue(connected)
                outcome = ("manager", connected)
            except (ValueError, asyncio.CancelledError) as error:
                self.assertTrue(failure)
                outcome = (type(error).__name__, str(error))
            outputs.append((outcome, list(events)))
        self.assertEqual(outputs[1], outputs[0])
        return outputs[0]

    def test_create_reuse_and_reconnect_keep_import_levels_and_exact_order(self):
        for specification in GETTERS:
            for initial, expected in (
                ("missing", ["get", "lookup", "hass", "create"]),
                ("connected", ["get", "connected"]),
                ("disconnected", ["get", "connected", "connect"]),
            ):
                with self.subTest(module=specification[0], initial=initial):
                    outcome, events = self.run_pair(specification, initial)
                    self.assertEqual(outcome, ("manager", True))
                    self.assertEqual(events[1:], expected)

    def test_import_access_descriptor_and_database_errors_keep_original_boundaries(
        self,
    ):
        for specification in GETTERS:
            for initial in ("missing", "connected", "disconnected"):
                for failure in (
                    "import",
                    "get",
                    "lookup",
                    "hass",
                    "connected",
                    "create",
                    "connect",
                ):
                    with self.subTest(
                        module=specification[0], initial=initial, failure=failure
                    ):
                        self.run_pair(specification, initial, failure)

    def test_cancellation_propagates_from_creation_and_reconnection(self):
        for specification in GETTERS:
            for initial in ("missing", "connected", "disconnected"):
                for failure in ("cancel-create", "cancel-connect"):
                    with self.subTest(
                        module=specification[0], initial=initial, failure=failure
                    ):
                        outcome, _ = self.run_pair(specification, initial, failure)
                        active = (
                            initial == "missing" and failure == "cancel-create"
                        ) or (initial == "disconnected" and failure == "cancel-connect")
                        self.assertEqual(
                            outcome[0], "CancelledError" if active else "manager"
                        )
