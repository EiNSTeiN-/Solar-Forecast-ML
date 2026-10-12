"""Paired forecast persistence and circuit-breaker behavior with owned stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No real database, Home Assistant module or vendor runtime is imported.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from types import SimpleNamespace
import typing
import unittest
from pure_reference import classes

FORECAST = "extra_features/solar_forecast_eai/data/data_forecast_handler.py"
ERRORS = "services/service_error_handler.py"
NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
LOGGER = logging.getLogger("owned_recovery_fixture")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class IO:
    def __init__(self, hass, db_manager):
        self.hass = hass
        self.db = db_manager

    async def ensure_initialized(self):
        self.db.calls.append("initialize")
        if self.db.failure == "initialize":
            raise ValueError("owned initialize")
        if self.db.failure == "cancel":
            raise asyncio.CancelledError("owned cancelled")


class Database:
    def __init__(self, rows, failure=""):
        self.rows = rows
        self.failure = failure
        self.calls = []

    async def fetchall(self, query, params):
        self.calls.append(("fetchall", query, params))
        await asyncio.sleep(0)
        if self.failure == "fetch":
            raise ValueError("owned fetch")
        if self.failure == "cancel-fetch":
            raise asyncio.CancelledError("owned cancelled fetch")
        return self.rows

    async def execute(self, query, params):
        self.calls.append(("execute", query, params))

    async def get_daily_forecast_tracking(self):
        self.calls.append("tracking")
        return self.rows

    async def save_daily_forecast_tracking(self, row):
        self.calls.append(("save-tracking", dict(row)))


class ForecastPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        globals_ = dict(
            vars(typing),
            HomeAssistant=object,
            DatabaseManager=object,
            DataManagerIO=IO,
            datetime=datetime,
            timedelta=timedelta,
            dt_util=SimpleNamespace(now=lambda: NOW),
            _LOGGER=LOGGER,
        )
        cls.pair = classes(FORECAST, "DataForecastHandler", globals_, bases=(IO,))

    def test_history_conversion_errors_stay_inside_handler_and_query_filters_match(
        self,
    ):
        scenarios = ([("today", "2026-10-11", 7.5, 1, "ML", "created")], [])
        for rows in scenarios + ([("short",)],):
            for failure in ("", "fetch", "initialize", "cancel", "cancel-fetch"):
                for kwargs in (
                    {},
                    {"start_date": "2026-10-01", "end_date": "2026-10-12"},
                    {"days": 7},
                ):
                    outputs = []
                    for owner in self.pair:
                        db = Database(rows, failure)
                        instance = owner(object(), db)
                        try:
                            result = asyncio.run(
                                instance.get_forecast_history(**kwargs)
                            )
                        except asyncio.CancelledError as error:
                            result = ("cancelled", str(error))
                        outputs.append((result, db.calls))
                    self.assertEqual(outputs[1], outputs[0])
                    if (
                        failure in ("fetch", "initialize")
                        or rows == [("short",)]
                        and not failure
                    ):
                        self.assertEqual(outputs[0][0], [])

    def test_update_rows_and_tracking_mutations_keep_existing_fields(self):
        for rows in ([], [(2.5, "Physics", "updated")], [(1,)]):
            outputs = []
            for owner in self.pair:
                db = Database(rows)
                instance = owner(object(), db)
                result = asyncio.run(
                    instance.get_forecast_updates("tomorrow", "2026-10-13", 5)
                )
                outputs.append((result, db.calls))
            self.assertEqual(outputs[1], outputs[0])
        for row in (None, {"date": "yesterday", "existing": 3}):
            outputs = []
            for owner in self.pair:
                db = Database(dict(row) if row else None)
                instance = owner(object(), db)
                result = asyncio.run(
                    instance.update_daily_tracking(
                        peak_today={"power_w": 1000}, autarky={"percent": 90}
                    )
                )
                outputs.append((result, db.calls))
            self.assertEqual(outputs[1], outputs[0])
            saved = outputs[0][1][-1][1]
            self.assertEqual(saved["date"], "2026-10-12")
            self.assertEqual(saved["peak_today"], {"power_w": 1000})
            if row:
                self.assertEqual(saved["existing"], 3)


class FixedClock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


class CircuitBreakerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        states = classes(ERRORS, "CircuitBreakerState", {"Enum": Enum}, bases=(Enum,))[
            0
        ]
        errors = classes(ERRORS, "ErrorType", {"Enum": Enum}, bases=(Enum,))[0]
        cls.states = states
        cls.errors = errors
        exceptions = {
            name: type(name, (Exception,), {})
            for name in (
                "CircuitBreakerOpenException",
                "ConfigurationException",
                "DataIntegrityException",
                "MLModelException",
                "WeatherAPIException",
            )
        }
        globals_ = dict(
            vars(typing),
            datetime=FixedClock,
            timedelta=timedelta,
            timezone=timezone,
            defaultdict=defaultdict,
            _LOGGER=LOGGER,
            CircuitBreakerState=states,
            ErrorType=errors,
            DatabaseManager=object,
            logging=logging,
            asyncio=asyncio,
            traceback=SimpleNamespace(format_exc=lambda: "owned-trace"),
            **exceptions
        )
        cls.breakers = classes(ERRORS, "CircuitBreaker", globals_)
        cls.services = classes(
            ERRORS,
            "ErrorHandlingService",
            dict(globals_, CircuitBreaker=cls.breakers[0]),
        )
        for index, owner in enumerate(cls.services):
            owner.__init__.__globals__["CircuitBreaker"] = cls.breakers[index]

    def test_open_timeout_half_open_success_and_configuration_failure(self):
        outputs = []
        for owner in self.breakers:
            FixedClock.current = NOW
            breaker = owner(
                "owned",
                failure_threshold=2,
                success_threshold=2,
                open_timeout_seconds=60,
            )
            events = [breaker.allow_request()]
            breaker.record_failure(self.errors.API_ERROR)
            events.append(breaker.get_status())
            breaker.record_failure(self.errors.API_ERROR)
            events.append(breaker.allow_request())
            FixedClock.current = NOW + timedelta(seconds=59)
            events.append(breaker.allow_request())
            FixedClock.current = NOW + timedelta(seconds=60)
            events.append(breaker.allow_request())
            breaker.record_success()
            events.append(breaker.get_status())
            breaker.record_success()
            events.append(breaker.get_status())
            breaker.record_failure(self.errors.CONFIGURATION)
            events.append(breaker.get_status())
            breaker.reset()
            events.append(breaker.get_status())
            outputs.append(events)
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[0][2:5], [False, False, True])
        self.assertEqual(outputs[0][6]["state"], "closed")
        self.assertEqual(outputs[0][7]["state"], "open")

    def test_error_history_conversion_and_fetch_failure_keep_memory_fallback(self):
        for rows in (
            [],
            [("timestamp", "source", "type", "classification", "message")],
            [("short",)],
        ):
            for failure in ("", "fetch", "cancel-fetch"):
                outputs = []
                for owner in self.services:
                    db = Database(rows, failure)
                    service = owner(db)
                    service.error_log = [{"message": "memory"}]
                    try:
                        result = asyncio.run(service.get_error_history_from_db(4))
                    except asyncio.CancelledError as error:
                        result = ("cancelled", str(error))
                    outputs.append((result, db.calls))
                self.assertEqual(outputs[1], outputs[0])
                if failure == "fetch" or not failure and rows == [("short",)]:
                    self.assertEqual(outputs[0][0], [{"message": "memory"}])
        outputs = [
            asyncio.run(owner().get_error_history_from_db()) for owner in self.services
        ]
        self.assertEqual(outputs, [[], []])
