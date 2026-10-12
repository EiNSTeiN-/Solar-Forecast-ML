"""Paired isolated checks for recovered timezone and JSON conversion returns.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Mapping
import math
import unittest
from zoneinfo import ZoneInfo

from pure_reference import isolated_function


class PaddedReturnFunctionTests(unittest.TestCase):
    def timezone_pair(self, module, globals_):
        return isolated_function(
            "extra_features/grid_price_monitor/tariffs/" + module + ".py",
            "extra_features/grid_price_monitor/default_local_tz.py",
            "<module>/default_local_tz#1",
            globals_,
        )

    def json_pair(self):
        pair = isolated_function(
            "data/db_manager.py",
            "data/json_ready.py",
            "<module>/json_ready#1",
            {"datetime": datetime, "Mapping": Mapping},
        )
        for function in pair:
            function.__globals__["json_ready"] = function
        return pair

    def test_timezone_actual_stdlib_constructor_and_local_fallback(self):
        for module in ("corrections", "csv_import"):
            pair = self.timezone_pair(
                module,
                {"ZoneInfo": ZoneInfo, "datetime": datetime, "timezone": timezone},
            )
            for name in (None, "", "UTC", "America/Toronto", "Owned/Invalid-Zone"):
                outcomes = [function(name) for function in pair]
                self.assertEqual(outcomes[1], outcomes[0])
                self.assertIsNotNone(outcomes[0])
                if name == "UTC":
                    self.assertEqual(outcomes[0].key, "UTC")

    def test_timezone_truth_is_outside_handler_and_zone_errors_fall_through(self):
        for module in ("corrections", "csv_import"):
            pair = self.timezone_pair(module, {})
            for truth in (False, True):
                for failure in (
                    "",
                    "truth",
                    "zone",
                    "zone-base",
                    "now",
                    "astimezone",
                    "tzinfo",
                ):
                    outcomes = []
                    for function in pair:
                        calls = []

                        class Name:
                            def __bool__(self):
                                calls.append("truth")
                                if failure == "truth":
                                    raise ValueError("truth failed")
                                return truth

                        class Clock:
                            def now(self):
                                calls.append("now")
                                if failure == "now":
                                    raise RuntimeError("now failed")
                                return self

                            def astimezone(self):
                                calls.append("astimezone")
                                if failure == "astimezone":
                                    raise RuntimeError("astimezone failed")
                                return self

                            @property
                            def tzinfo(self):
                                calls.append("tzinfo")
                                if failure == "tzinfo":
                                    raise AttributeError("tzinfo failed")
                                return "local-zone"

                        def zone(name):
                            calls.append("zone")
                            if failure == "zone":
                                raise KeyError("zone failed")
                            if failure == "zone-base":
                                raise KeyboardInterrupt("zone interrupted")
                            return "requested-zone"

                        function.__globals__.update(
                            ZoneInfo=zone, datetime=Clock(), timezone=timezone
                        )
                        try:
                            outcome = ("value", function(Name()))
                        except (Exception, KeyboardInterrupt) as error:
                            outcome = (type(error).__name__, str(error))
                        outcomes.append((outcome, calls))
                    self.assertEqual(outcomes[1], outcomes[0])
                    if truth and failure == "":
                        self.assertEqual(
                            outcomes[0],
                            (("value", "requested-zone"), ["truth", "zone"]),
                        )
                    if not truth and failure == "":
                        self.assertEqual(
                            outcomes[0],
                            (
                                ("value", "local-zone"),
                                ["truth", "now", "astimezone", "tzinfo"],
                            ),
                        )

    def test_timezone_false_local_value_keeps_truth_and_utc_getter_order(self):
        for module in ("corrections", "csv_import"):
            pair = self.timezone_pair(module, {})
            for local_truth in (False, True):
                for failure in ("", "local-truth", "utc"):
                    outcomes = []
                    for function in pair:
                        calls = []

                        class Local:
                            def __bool__(self):
                                calls.append("local-truth")
                                if failure == "local-truth":
                                    raise ValueError("local truth failed")
                                return local_truth

                        class Clock:
                            def now(self):
                                calls.append("now")
                                return self

                            def astimezone(self):
                                calls.append("astimezone")
                                return self

                            @property
                            def tzinfo(self):
                                calls.append("tzinfo")
                                return Local()

                        class Timezone:
                            @property
                            def utc(self):
                                calls.append("utc")
                                if failure == "utc":
                                    raise RuntimeError("utc failed")
                                return timezone.utc

                        function.__globals__.update(
                            datetime=Clock(), timezone=Timezone()
                        )
                        try:
                            result = function(None)
                            outcome = (
                                "value",
                                "local" if isinstance(result, Local) else "utc",
                            )
                        except Exception as error:
                            outcome = (type(error).__name__, str(error))
                        outcomes.append((outcome, calls))
                    self.assertEqual(outcomes[1], outcomes[0])
                    if failure == "":
                        self.assertEqual(
                            outcomes[0][0], ("value", "local" if local_truth else "utc")
                        )

    def test_json_scalars_datetime_nested_values_and_rejected_raw_bytes(self):
        pair = self.json_pair()
        samples = (
            None,
            False,
            True,
            0,
            -2,
            1.25,
            "",
            "text",
            float("nan"),
            float("inf"),
            datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            b"bytes",
            bytearray(b"bytes"),
            [],
            (),
            [1, None, {"name": (True, 2.5)}],
            {1: "one", False: "false"},
            object(),
        )
        for sample in samples:
            outcomes = []
            for function in pair:
                try:
                    result = function(sample)
                    outcome = (
                        "value",
                        (
                            "nan"
                            if isinstance(result, float) and math.isnan(result)
                            else result
                        ),
                    )
                except ValueError as error:
                    outcome = (type(error).__name__, str(error))
                outcomes.append(outcome)
            self.assertEqual(outcomes[1], outcomes[0])
        self.assertEqual(
            pair[1]([1, None, {"name": (True, 2.5)}]), [1, None, {"name": [True, 2.5]}]
        )
        self.assertEqual(pair[1]({1: "one"}), {"1": "one"})

    def test_json_array_and_item_conversion_preserve_recursive_calls_and_errors(self):
        pair = self.json_pair()
        for kind in ("array", "item"):
            for failure in ("", "getter", "call", "value", "runtime", "base"):
                outcomes = []
                for function in pair:
                    calls = []

                    class Value:
                        def __getattr__(self, name):
                            calls.append(("get", name))
                            if name != ("tolist" if kind == "array" else "item"):
                                raise AttributeError(name)
                            if failure == "getter":
                                raise AttributeError("owned getter")

                            def read():
                                calls.append(("call", name))
                                errors = {
                                    "call": AttributeError,
                                    "value": ValueError,
                                    "runtime": RuntimeError,
                                    "base": KeyboardInterrupt,
                                }
                                if failure in errors:
                                    raise errors[failure]("owned conversion")
                                return [1, (None, 2)] if kind == "array" else 7

                            return read

                    try:
                        outcome = ("value", function(Value()))
                    except (Exception, KeyboardInterrupt) as error:
                        outcome = (type(error).__name__, str(error))
                    outcomes.append((outcome, calls))
                self.assertEqual(outcomes[1], outcomes[0])
                if failure == "":
                    self.assertEqual(
                        outcomes[0][0],
                        ("value", [1, [None, 2]] if kind == "array" else 7),
                    )

    def test_json_mapping_key_conversion_iteration_and_list_errors(self):
        pair = self.json_pair()
        for failure in ("", "key", "items", "iteration", "child"):
            outcomes = []
            for function in pair:
                calls = []

                class Key:
                    def __str__(self):
                        calls.append("key")
                        if failure == "key":
                            raise ValueError("key failed")
                        return "owned-key"

                class Rows(Mapping):
                    def __len__(self):
                        return 1

                    def __iter__(self):
                        raise AssertionError("items override must be used")

                    def __getitem__(self, key):
                        raise AssertionError("items override must be used")

                    def items(self):
                        calls.append("items")
                        if failure == "items":
                            raise RuntimeError("items failed")

                        def generate():
                            calls.append("iteration")
                            if failure == "iteration":
                                raise RuntimeError("iteration failed")
                            yield Key(), b"bad" if failure == "child" else [1, 2]

                        return generate()

                try:
                    outcome = ("value", function(Rows()))
                except Exception as error:
                    outcome = (type(error).__name__, str(error))
                outcomes.append((outcome, calls))
            self.assertEqual(outcomes[1], outcomes[0])
            if failure == "":
                self.assertEqual(
                    outcomes[0],
                    (("value", {"owned-key": [1, 2]}), ["items", "iteration", "key"]),
                )


if __name__ == "__main__":
    unittest.main()
