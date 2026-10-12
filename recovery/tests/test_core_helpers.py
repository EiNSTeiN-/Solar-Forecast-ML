"""Verified datetime utilities with stdlib inputs and owned HA/clock stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from datetime import datetime, timezone, tzinfo
from types import SimpleNamespace
from typing import Optional
import unittest
from zoneinfo import ZoneInfo

from pure_reference import classes, functions


MODULE = "core/core_helpers.py"
LOCAL = ZoneInfo("America/Toronto")
NOW = datetime(2026, 7, 4, 12, 34, 56, 123456, tzinfo=LOCAL)


class Logger:
    def __init__(self):
        self.calls = []

    def debug(self, *arguments):
        self.calls.append(("debug", arguments))

    def warning(self, *arguments):
        self.calls.append(("warning", arguments))


def outcome(operation):
    try:
        return ("value", operation())
    except (Exception, KeyboardInterrupt) as error:
        return (type(error).__name__, str(error))


class CoreHelperTests(unittest.TestCase):
    def pair(self, extra=None):
        namespace = {
            "datetime": datetime,
            "timezone": timezone,
            "tzinfo": tzinfo,
            "Optional": Optional,
            "_HAS_HA_DT": False,
            "_LOGGER": Logger(),
            "get_local_tz": lambda: LOCAL,
        }
        namespace.update(extra or {})
        pair = classes(MODULE, "SafeDateTimeUtil", namespace)
        for cls in pair:
            cls.now.__globals__["SafeDateTimeUtil"] = cls
        return pair

    def test_local_timezone_keeps_clock_errors_warning_and_none_result(self):
        pair = [
            entry["get_local_tz"] for entry in functions(MODULE, ["get_local_tz"], {})
        ]
        for failure in ("", "none", "now", "astimezone", "tzinfo", "base"):
            results = []
            for function in pair:
                calls, logger = [], Logger()

                class Clock:
                    def now(self):
                        calls.append("now")
                        if failure == "now":
                            raise ValueError("now failed")
                        if failure == "base":
                            raise KeyboardInterrupt("clock interrupted")
                        return self

                    def astimezone(self):
                        calls.append("astimezone")
                        if failure == "astimezone":
                            raise ValueError("timezone failed")
                        return self

                    @property
                    def tzinfo(self):
                        calls.append("tzinfo")
                        if failure == "tzinfo":
                            raise AttributeError("tzinfo failed")
                        return None if failure == "none" else LOCAL

                function.__globals__.update(
                    datetime=Clock(), timezone=timezone, _LOGGER=logger
                )
                results.append((outcome(function), calls, logger.calls))
            self.assertEqual(results[1], results[0])
            if failure in ("now", "astimezone", "tzinfo"):
                self.assertEqual(results[0][0], ("value", timezone.utc))
                self.assertEqual(len(results[0][2]), 1)
            if failure == "none":
                self.assertEqual(results[0][0], ("value", None))

    def test_ha_now_and_default_zone_preserve_fallback_errors_and_logs(self):
        for method in ("now", "get_default_time_zone"):
            pair = self.pair()
            for enabled in (False, True):
                for ha_mode in ("value", "none", "error", "base"):
                    for local in (LOCAL, None):
                        results = []
                        for cls in pair:
                            calls, logger = [], Logger()

                            def ha_read():
                                calls.append("ha")
                                if ha_mode == "error":
                                    raise ValueError("HA read failed")
                                if ha_mode == "base":
                                    raise KeyboardInterrupt("HA interrupted")
                                if ha_mode == "none":
                                    return None
                                return NOW if method == "now" else LOCAL

                            def local_read():
                                calls.append("local")
                                return local

                            class Clock:
                                def now(self, zone):
                                    calls.append(("now", zone))
                                    return NOW

                            cls.now.__globals__.update(
                                _HAS_HA_DT=enabled,
                                _LOGGER=logger,
                                datetime=Clock(),
                                get_local_tz=local_read,
                                ha_dt_util=SimpleNamespace(
                                    now=ha_read, get_default_time_zone=ha_read
                                ),
                            )
                            results.append(
                                (outcome(getattr(cls, method)), calls, logger.calls)
                            )
                        self.assertEqual(results[1], results[0])
                        if enabled and ha_mode == "value":
                            self.assertEqual(
                                results[0],
                                (
                                    ("value", NOW if method == "now" else LOCAL),
                                    ["ha"],
                                    [],
                                ),
                            )
                        if (
                            enabled
                            and ha_mode == "none"
                            and method == "get_default_time_zone"
                        ):
                            self.assertEqual(results[0], (("value", None), ["ha"], []))

    def test_direct_ha_delegation_keeps_values_and_unhandled_errors(self):
        pair = self.pair()
        for method in ("utcnow", "as_local"):
            for failure in ("", "error", "base"):
                results = []
                for cls in pair:
                    calls = []

                    def delegate(*arguments):
                        calls.append(arguments)
                        if failure == "error":
                            raise RuntimeError("delegate failed")
                        if failure == "base":
                            raise KeyboardInterrupt("delegate interrupted")
                        return NOW

                    cls.now.__globals__.update(
                        _HAS_HA_DT=True,
                        ha_dt_util=SimpleNamespace(utcnow=delegate, as_local=delegate),
                    )
                    args = () if method == "utcnow" else (NOW,)
                    results.append(
                        (outcome(lambda: getattr(cls, method)(*args)), calls)
                    )
                self.assertEqual(results[1], results[0])
                if failure == "":
                    self.assertEqual(results[0], (("value", NOW), [args]))

    def test_real_datetime_conversions_and_dst_preserve_naive_assumptions(self):
        for enabled in (False, True):
            pair = self.pair(
                {
                    "_HAS_HA_DT": enabled,
                    "ha_dt_util": SimpleNamespace(
                        get_default_time_zone=lambda: LOCAL,
                        as_local=lambda value: value.astimezone(LOCAL),
                    ),
                }
            )
            samples = (
                datetime(2026, 1, 4, 8),
                datetime(2026, 7, 4, 8),
                datetime(2026, 7, 4, 12, tzinfo=timezone.utc),
                NOW,
                None,
            )
            for method in ("as_local", "as_utc", "ensure_local", "is_dst"):
                for sample in samples:
                    results = []
                    for cls in pair:
                        logger = Logger()
                        cls.now.__globals__["_LOGGER"] = logger
                        results.append(
                            (
                                outcome(lambda: getattr(cls, method)(sample)),
                                logger.calls,
                            )
                        )
                    self.assertEqual(results[1], results[0])
            self.assertEqual(
                pair[1].ensure_local(datetime(2026, 1, 4, 8)).tzinfo, LOCAL
            )
            self.assertFalse(pair[1].is_dst(datetime(2026, 1, 4, 8)))
            self.assertTrue(pair[1].is_dst(datetime(2026, 7, 4, 8)))

    def test_parse_datetime_iso_values_and_exact_handler_boundaries(self):
        pair = self.pair()
        samples = (
            None,
            "",
            0,
            7,
            False,
            True,
            "invalid",
            "2026-07-04",
            "2026-07-04T12:34:56",
            "2026-07-04T12:34:56Z",
            "2026-07-04T12:34:56-04:00",
        )
        for sample in samples:
            results = []
            for cls in pair:
                logger = Logger()
                cls.now.__globals__["_LOGGER"] = logger
                results.append(
                    (outcome(lambda: cls.parse_datetime(sample)), logger.calls)
                )
            self.assertEqual(results[1], results[0])
        self.assertEqual(
            pair[1].parse_datetime("2026-07-04T12:34:56Z"),
            datetime(2026, 7, 4, 12, 34, 56, tzinfo=timezone.utc),
        )
        for failure in ("", "none", "value", "type", "runtime", "base"):
            results = []
            for cls in pair:
                calls, logger = [], Logger()

                def parse(value):
                    calls.append(value)
                    errors = {
                        "value": ValueError,
                        "type": TypeError,
                        "runtime": RuntimeError,
                        "base": KeyboardInterrupt,
                    }
                    if failure in errors:
                        raise errors[failure]("owned parsing")
                    return None if failure == "none" else NOW

                cls.now.__globals__.update(
                    _HAS_HA_DT=True,
                    _LOGGER=logger,
                    ha_dt_util=SimpleNamespace(parse_datetime=parse),
                )
                results.append(
                    (
                        outcome(lambda: cls.parse_datetime("owned-input")),
                        calls,
                        logger.calls,
                    )
                )
            self.assertEqual(results[1], results[0])
            if failure == "":
                self.assertEqual(results[0], (("value", NOW), ["owned-input"], []))

    def test_day_boundaries_and_ha_flag_use_the_expected_zone(self):
        for enabled in (False, True):
            pair = self.pair(
                {
                    "_HAS_HA_DT": enabled,
                    "ha_dt_util": SimpleNamespace(
                        now=lambda: NOW,
                        get_default_time_zone=lambda: LOCAL,
                        as_local=lambda value: value.astimezone(LOCAL),
                    ),
                }
            )
            for cls in pair:

                class Clock:
                    def now(self, zone):
                        return NOW

                cls.now.__globals__["datetime"] = Clock()
            for method in ("start_of_day", "end_of_day"):
                for sample in (
                    None,
                    NOW,
                    NOW.replace(tzinfo=None),
                    NOW.astimezone(timezone.utc),
                ):
                    self.assertEqual(
                        getattr(pair[1], method)(sample),
                        getattr(pair[0], method)(sample),
                    )
            self.assertEqual(
                pair[1].start_of_day(),
                NOW.replace(hour=0, minute=0, second=0, microsecond=0),
            )
            self.assertEqual(
                pair[1].end_of_day(),
                NOW.replace(hour=23, minute=59, second=59, microsecond=999999),
            )
            self.assertEqual(pair[1].is_using_ha_time(), enabled)

    def test_seasons_keep_the_owned_relative_import_and_ssot_months(self):
        for winter in ((12, 1, 2), (6,)):
            calls = []

            def importer(name, globals=None, locals=None, fromlist=(), level=0):
                calls.append((name, tuple(fromlist), level))
                self.assertEqual(
                    (name, tuple(fromlist), level), ("const", ("WINTER_MONTHS",), 2)
                )
                return SimpleNamespace(WINTER_MONTHS=winter)

            pair = [
                entry["get_season"]
                for entry in functions(
                    MODULE,
                    ["get_season"],
                    {},
                    builtins_overrides={"__import__": importer},
                )
            ]
            for month in (*range(-2, 16), None, "3", []):
                calls.clear()
                left = outcome(lambda: pair[0](month))
                left_calls = list(calls)
                calls.clear()
                right = outcome(lambda: pair[1](month))
                self.assertEqual((right, calls), (left, left_calls))
            self.assertEqual(pair[1](winter[0]), "winter")


if __name__ == "__main__":
    unittest.main()
