"""Paired complete-module manager checks with owned database responses.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

import asyncio
from datetime import date
from threading import Lock
from typing import Any, Dict, Optional
import unittest

from pure_reference import classes, functions

DAY = date(2026, 10, 12)
SYSTEM = (45.0, -73.0, 10.0, "America/Toronto", 12.0, 4.0)
DAYS = [(DAY, "06:00", "18:00", "12:00", 12.0, 50.0)]
HOURS = [(DAY, 12, 50.0, 180.0, 800.0, 4.0, 12.0, "06:00", "18:00", "12:00")]


class Logger:
    def __init__(self):
        self.calls = []

    def record(self, level, *args, **kwargs):
        self.calls.append((level, args, kwargs))

    def debug(self, *args, **kwargs):
        self.record("debug", *args, **kwargs)

    def info(self, *args, **kwargs):
        self.record("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self.record("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self.record("error", *args, **kwargs)


class CacheAndPeakTests(unittest.TestCase):
    def cache_classes(self):
        return classes(
            "astronomy/astronomy_cache_manager.py",
            "AstronomyCacheManager",
            {
                "date": date,
                "Lock": Lock,
                "Any": Any,
                "Dict": Dict,
                "Optional": Optional,
                "DatabaseManager": object,
                "_LOGGER": Logger(),
            },
        )

    def peak_classes(self):
        return classes(
            "astronomy/max_peak_tracker.py",
            "MaxPeakTracker",
            {
                "date": date,
                "Dict": Dict,
                "Optional": Optional,
                "DatabaseManager": object,
                "_LOGGER": Logger(),
            },
        )

    def test_cache_initialization_keeps_partial_load_failures_and_cancellation(self):
        pair = self.cache_classes()
        for mode in (
            "ok",
            "missing",
            "short",
            "system-error",
            "day-error",
            "hour-error",
            "system-cancel",
            "day-cancel",
        ):
            outcomes = []
            for cls in pair:
                calls, logger = [], Logger()
                cls.__init__.__globals__["_LOGGER"] = logger

                class DB:
                    async def fetchone(self, query):
                        calls.append(("one", query))
                        if mode == "system-error":
                            raise ValueError("system failed")
                        if mode == "system-cancel":
                            raise asyncio.CancelledError("system cancelled")
                        return (
                            None
                            if mode == "missing"
                            else (1,) if mode == "short" else SYSTEM
                        )

                    async def fetchall(self, query):
                        kind = "hours" if "hour, sun_elevation" in query else "days"
                        calls.append((kind, query))
                        if mode == kind[:-1] + "-error":
                            raise ValueError(kind + " failed")
                        if mode == "day-cancel" and kind == "days":
                            raise asyncio.CancelledError("days cancelled")
                        return HOURS if kind == "hours" else DAYS

                obj = cls(DB())
                try:
                    result = asyncio.run(obj.initialize())
                except asyncio.CancelledError as error:
                    result = ("cancelled", str(error))
                outcomes.append(
                    (result, obj._cache, obj.is_loaded(), calls, logger.calls)
                )
            self.assertEqual(outcomes[1], outcomes[0])
            if mode == "ok":
                self.assertTrue(outcomes[0][0])
                self.assertTrue(outcomes[0][2])
            if mode in ("day-error", "hour-error"):
                self.assertTrue(outcomes[0][0])
                self.assertTrue(outcomes[0][2])
            if mode == "missing":
                self.assertEqual(outcomes[0][0:3], (False, {}, False))

    def test_cache_readers_keep_underlying_references_clear_and_missing_values(self):
        pair = self.cache_classes()
        for loaded in (False, True):
            for window in ((None, None), ("06:00", "18:00"), ("", "18:00")):
                outputs = []
                for cls in pair:
                    day_data = {
                        "production_window_start": window[0],
                        "production_window_end": window[1],
                        "hourly": {"12": {"elevation_deg": 50}},
                    }
                    system = {"capacity": 12}
                    obj = cls(object())
                    obj._cache = {
                        "days": {DAY.isoformat(): day_data},
                        "system_info": system,
                    }
                    obj._loaded = loaded
                    result = (
                        obj.get_day_data(DAY),
                        obj.get_production_window(DAY),
                        obj.get_hourly_data(DAY, 12),
                        obj.get_hourly_data(DAY, 13),
                        obj.get_system_info(),
                        obj.is_loaded(),
                    )
                    if loaded:
                        self.assertIs(result[0], day_data)
                        self.assertIs(result[4], system)
                    obj.clear()
                    outputs.append(
                        (result, obj._cache, obj.is_loaded(), obj.get_day_data(DAY))
                    )
                self.assertEqual(outputs[1], outputs[0])
                self.assertEqual(outputs[0][1:], ({}, False, None))

    def test_cache_singleton_updates_database_without_replacing_the_instance(self):
        pair = self.cache_classes()
        for cls in pair:
            namespace = functions(
                "astronomy/astronomy_cache_manager.py",
                ["get_cache_manager", "reset_cache_manager"],
                {
                    "_cache_manager": None,
                    "AstronomyCacheManager": cls,
                    "_LOGGER": Logger(),
                },
                defaults={"get_cache_manager": (None,)},
            )[pair.index(cls)]
            get, reset = (
                namespace["get_cache_manager"],
                namespace["reset_cache_manager"],
            )
            self.assertIsNone(get())
            first_db, second_db = object(), object()
            first = get(first_db)
            first._loaded = True
            first._cache["owned"] = 1
            self.assertIs(get(), first)
            self.assertIs(get(first_db), first)
            self.assertTrue(first.is_loaded())
            self.assertIs(get(second_db), first)
            self.assertIs(first.db, second_db)
            self.assertFalse(first.is_loaded())
            self.assertEqual(first._cache, {"owned": 1})
            reset()
            self.assertEqual(first._cache, {})
            self.assertIsNone(get())
            reset()

    def test_peak_update_keeps_thresholds_database_calls_and_partial_failures(self):
        pair = self.peak_classes()
        for actual, hourly, global_peak in (
            (0.5, 1.0, 2.0),
            (1.0, 1.0, 2.0),
            (1.5, 1.0, 2.0),
            (3.0, 1.0, 2.0),
            (1.0, None, None),
        ):
            for failure in (
                "",
                "hour-read",
                "hour-write",
                "global-read",
                "global-write",
                "cancel",
            ):
                outcomes = []
                for cls in pair:
                    calls, logger = [], Logger()
                    cls.__init__.__globals__["_LOGGER"] = logger

                    class DB:
                        async def fetchone(self, query, args=None):
                            kind = (
                                "hour-read"
                                if "hourly_peaks" in query
                                else "global-read"
                            )
                            calls.append((kind, query, args))
                            if failure == kind:
                                raise ValueError(kind + " failed")
                            if failure == "cancel":
                                raise asyncio.CancelledError("read cancelled")
                            value = hourly if kind == "hour-read" else global_peak
                            return None if value is None else (value,)

                        async def execute(self, query, args):
                            kind = "hour-write" if "INSERT" in query else "global-write"
                            calls.append((kind, query, args))
                            if failure == kind:
                                raise ValueError(kind + " failed")

                    obj = cls(DB())
                    try:
                        result = asyncio.run(
                            obj.check_and_update_peak(
                                DAY, 12, actual, {"temperature_c": 20}
                            )
                        )
                    except asyncio.CancelledError as error:
                        result = ("cancelled", str(error))
                    outcomes.append((result, calls, logger.calls))
                self.assertEqual(outcomes[1], outcomes[0])
                if failure == "" and hourly is not None and actual <= hourly:
                    self.assertFalse(outcomes[0][0])
                    self.assertEqual(len(outcomes[0][1]), 1)
                if failure == "" and actual > (hourly or 0):
                    self.assertTrue(outcomes[0][0])

    def test_peak_readers_keep_rows_missing_defaults_dates_and_conditions(self):
        pair = self.peak_classes()
        row = (12, 3.0, DAY, 50.0, 20.0, 18.0, 800.0)
        for mode in ("present", "missing", "error", "cancel", "zero"):
            outcomes = []
            for cls in pair:
                calls, logger = [], Logger()
                cls.__init__.__globals__["_LOGGER"] = logger

                class DB:
                    async def fetchone(self, query, args=None):
                        calls.append(("one", query, args))
                        if mode == "error":
                            raise ValueError("read failed")
                        if mode == "cancel":
                            raise asyncio.CancelledError("read cancelled")
                        if mode == "missing":
                            return None
                        if "max_peak_date" in query:
                            return (
                                0.0 if mode == "zero" else 3.0,
                                DAY,
                                12,
                                50,
                                20,
                                18,
                                800,
                            )
                        return (0.0 if mode == "zero" else 3.0,)

                    async def fetchall(self, query):
                        calls.append(("all", query))
                        if mode == "error":
                            raise ValueError("read failed")
                        if mode == "cancel":
                            raise asyncio.CancelledError("read cancelled")
                        return [] if mode == "missing" else [row]

                obj = cls(DB())

                async def read():
                    return (
                        await obj.get_historical_max_for_hour(12),
                        await obj.get_all_hourly_peaks(),
                        await obj.get_global_max(),
                    )

                try:
                    result = asyncio.run(read())
                except asyncio.CancelledError as error:
                    result = ("cancelled", str(error))
                outcomes.append((result, calls, logger.calls))
            self.assertEqual(outcomes[1], outcomes[0])
            if mode == "missing":
                self.assertEqual(outcomes[0][0], (0.0, {}, None))
            if mode == "present":
                self.assertEqual(outcomes[0][0][1][12]["date"], DAY.isoformat())


if __name__ == "__main__":
    unittest.main()
