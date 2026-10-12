"""Paired future-only reforecast checks with owned generation and writer stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration, database, inverter or weather service is imported.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest

from pure_reference import classes

MODULE = "production/production_operational_reforecast.py"
NOW = datetime(2026, 10, 12, 10, 30, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()


class Logger:
    def __init__(self):
        self.calls = []

    def record(self, level, *args, **kwargs):
        normalized = tuple(
            (
                (type(value).__name__, str(value))
                if isinstance(value, BaseException)
                else value
            )
            for value in args
        )
        self.calls.append((level, normalized, kwargs))

    def debug(self, *args, **kwargs):
        self.record("debug", *args, **kwargs)

    def info(self, *args, **kwargs):
        self.record("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self.record("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self.record("error", *args, **kwargs)


def cap(value):
    return min(1.25, max(1.0, float(value)))


def zero(row):
    row["production_kwh"] = 0.0


def should_zero(*, sun_elevation_deg):
    return sun_elevation_deg is not None and sun_elevation_deg <= 0


class OperationalReforecastTests(unittest.TestCase):
    def pair(self):
        return classes(
            MODULE,
            "OperationalReforecastEngine",
            {
                "asyncio": asyncio,
                "datetime": datetime,
                "Any": Any,
                "Dict": Dict,
                "List": List,
                "Optional": Optional,
                "HomeAssistant": object,
                "ForecastRegenerationHelper": lambda *args: None,
                "cap_rest_of_day_uplift": cap,
                "should_zero_hourly_yield": should_zero,
                "zero_hourly_yield_entry": zero,
                "dt_util": SimpleNamespace(now=lambda: NOW),
                "_LOGGER": Logger(),
            },
        )

    def test_future_row_filters_copying_duplicates_sun_gates_and_weather_keys(self):
        pair = self.pair()
        rows = [
            {"date": "2026-10-11", "hour": 20, "production_kwh": 7},
            {"date": TODAY, "hour": None, "production_kwh": 7},
            {"date": TODAY, "hour": 10, "production_kwh": 7},
            {
                "date": TODAY,
                "hour": 11,
                "production_kwh": 3,
                "sun_elevation_deg": 10,
                "groups": [{"power_kwh": 2}],
            },
            {"date": TODAY, "hour": 11, "production_kwh": 9},
            {"date": TODAY, "hour": 12, "production_kwh": 3, "sun_elevation_deg": 0},
            {"date": TODAY, "hour": 13, "production_kwh": 3, "sun_gated": True},
        ]
        weather = {
            (TODAY, 10): {"old": 1},
            (TODAY, 11): {"temperature": 20},
            (TODAY, None): {},
            ("2026-10-11", 11): {"other": 1},
        }
        for maximum in (0.0, 1.5, 5.0):
            outputs = []
            for cls in pair:
                source = deepcopy(rows)
                engine = cls(
                    object(), SimpleNamespace(inverter_max_power=maximum), object()
                )
                selected = engine._build_future_entries(TODAY, 10, source)
                weather_rows = engine._build_future_weather_map(TODAY, 10, weather)
                self.assertIsNot(selected[0], source[3])
                self.assertIs(selected[0]["groups"], source[3]["groups"])
                outputs.append(
                    (
                        selected,
                        weather_rows,
                        source,
                        engine._compute_rest_of_day_total(selected),
                    )
                )
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual([row["hour"] for row in outputs[0][0]], [11, 12, 13])
            self.assertEqual(
                [row["production_kwh"] for row in outputs[0][0]],
                [min(3, maximum) if maximum > 0 else 3, 0, 0],
            )
            self.assertEqual(outputs[0][1], {TODAY + "_11": {"temperature": 20}})
            self.assertEqual(outputs[0][2], rows)

    def test_uplift_mutates_existing_fields_groups_and_keeps_conversion_errors(self):
        pair = self.pair()
        for uplift in (0.5, 1.0, 1.1, 1.25, 2.0):
            for maximum in (0.0, 1.0):
                outputs = []
                for cls in pair:
                    rows = [
                        {
                            "production_kwh": 2.0,
                            "physics_kwh": "1.23456",
                            "ai_kwh": "unavailable",
                            "lstm_kwh": None,
                            "groups": [{"power_kwh": 1.0, "raw_physics_kwh": "2.5"}],
                        }
                    ]
                    engine = cls(
                        object(), SimpleNamespace(inverter_max_power=maximum), object()
                    )
                    applied = engine._apply_rest_of_day_uplift(rows, uplift)
                    outputs.append((applied, rows))
                self.assertEqual(outputs[1], outputs[0])
                self.assertEqual(outputs[0][1][0]["ai_kwh"], "unavailable")
                self.assertIsNone(outputs[0][1][0]["lstm_kwh"])
                if uplift > 1:
                    self.assertEqual(outputs[0][0], min(uplift, 1.25))
                    self.assertEqual(
                        outputs[0][1][0]["groups"][0]["power_kwh"], min(uplift, 1.25)
                    )

    def test_locked_reforecast_keeps_generation_publication_validation_and_cancellation(
        self,
    ):
        pair = self.pair()
        modes = (
            "ok",
            "refresh-error",
            "generate-error",
            "empty",
            "no-future",
            "publish-error",
            "missing-sequence",
            "hour-count",
            "weather-count",
            "group-count",
            "daily-count",
            "refresh-cancel",
            "publish-cancel",
            "listener-error",
        )
        for mode in modes:
            outputs = []
            for cls in pair:
                calls, logger = [], Logger()
                cls.__init__.__globals__["_LOGGER"] = logger
                rows = [
                    {
                        "date": TODAY,
                        "hour": 11,
                        "production_kwh": 2.0,
                        "groups": [{"power_kwh": 1.0}],
                        "sun_elevation_deg": 20,
                    },
                    {
                        "date": TODAY,
                        "hour": 12,
                        "production_kwh": 1.0,
                        "sun_elevation_deg": 30,
                    },
                ]
                weather = {(TODAY, 11): {"temperature": 20}}

                class Regenerator:
                    async def refresh_weather_data(self, **options):
                        calls.append(("refresh", options))
                        await asyncio.sleep(0)
                        if mode == "refresh-error":
                            raise ValueError("refresh failed")
                        if mode == "refresh-cancel":
                            raise asyncio.CancelledError("refresh cancelled")

                    async def generate_full_forecast(self, today, **options):
                        calls.append(("generate", today, options))
                        await asyncio.sleep(0)
                        if mode == "generate-error":
                            raise ValueError("generate failed")
                        return (
                            []
                            if mode == "empty"
                            else (
                                [dict(rows[0], hour=10)]
                                if mode == "no-future"
                                else deepcopy(rows)
                            )
                        ), weather

                class Writer:
                    async def publish_intraday_reforecast(self, **options):
                        calls.append(("publish", deepcopy(options)))
                        await asyncio.sleep(0)
                        if mode == "publish-error":
                            raise ValueError("publish failed")
                        if mode == "publish-cancel":
                            raise asyncio.CancelledError("publish cancelled")
                        result = {
                            "snapshot_sequence": 7,
                            "hourly_rows": 2,
                            "weather_rows": 1,
                            "group_rows": 1,
                            "daily_rows": 1,
                        }
                        if mode == "missing-sequence":
                            result["snapshot_sequence"] = None
                        for label, key in (
                            ("hour-count", "hourly_rows"),
                            ("weather-count", "weather_rows"),
                            ("group-count", "group_rows"),
                            ("daily-count", "daily_rows"),
                        ):
                            if mode == label:
                                result[key] = 0
                        return result

                def notify():
                    calls.append(("notify",))
                    if mode == "listener-error":
                        raise RuntimeError("listener failed")

                engine = cls(
                    object(),
                    SimpleNamespace(
                        inverter_max_power=5.0, async_update_listeners=notify
                    ),
                    object(),
                )
                engine._forecast_regeneration = Regenerator()
                try:
                    result = asyncio.run(
                        engine._run_reforecast_locked(
                            Writer(), "owned", 10, NOW, TODAY, "owned-snapshot", 1.1
                        )
                    )
                except asyncio.CancelledError as error:
                    result = ("cancelled", str(error))
                outputs.append((result, calls, logger.calls))
            self.assertEqual(outputs[1], outputs[0])
            if mode in ("ok", "listener-error"):
                self.assertTrue(outputs[0][0])
                published = next(
                    call[1] for call in outputs[0][1] if call[0] == "publish"
                )
                self.assertEqual(published["daily_values"][0]["prediction_kwh"], 3.3)
                self.assertEqual(published["source_track"], "operational_reforecast")
                self.assertEqual(published["cutoff_hour"], 10)
            elif not mode.endswith("cancel"):
                self.assertFalse(outputs[0][0])

    def test_public_run_keeps_handler_and_lock_guards_and_releases_after_cancellation(
        self,
    ):
        pair = self.pair()
        for mode in ("ok", "missing", "locked", "cancel"):
            outputs = []
            for cls in pair:
                calls, logger = [], Logger()
                cls.__init__.__globals__["_LOGGER"] = logger
                handler = object()
                manager = SimpleNamespace(
                    operational_forecast_handler=None if mode == "missing" else handler
                )
                engine = cls(object(), SimpleNamespace(data_manager=manager), object())

                async def run_locked(**options):
                    self.assertIs(options["operational_handler"], handler)
                    calls.append(
                        {
                            key: value
                            for key, value in options.items()
                            if key != "operational_handler"
                        }
                    )
                    if mode == "cancel":
                        raise asyncio.CancelledError("run cancelled")
                    return True

                engine._run_reforecast_locked = run_locked

                async def run():
                    if mode == "locked":
                        await engine._runtime_lock.acquire()
                    try:
                        result = await engine.run_reforecast(
                            "owned", now=NOW, snapshot_type="owned"
                        )
                    except asyncio.CancelledError as error:
                        result = ("cancelled", str(error))
                    locked = engine._runtime_lock.locked()
                    if mode == "locked":
                        engine._runtime_lock.release()
                    return result, locked

                outputs.append((asyncio.run(run()), calls, logger.calls))
            self.assertEqual(outputs[1], outputs[0])
            if mode == "ok":
                self.assertEqual(outputs[0][0], (True, False))
                self.assertEqual(outputs[0][1][0]["cutoff_hour"], NOW.hour)
            if mode == "cancel":
                self.assertFalse(outputs[0][0][1])


if __name__ == "__main__":
    unittest.main()
