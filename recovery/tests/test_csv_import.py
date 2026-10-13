"""Paired CSV import behavior with controlled records and database dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
No original integration module, real database or armor runtime is imported.
"""

import ast
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Mapping
import unittest
from zoneinfo import ZoneInfo

from pure_reference import functions

RELATIVE = "extra_features/grid_price_monitor/tariffs/csv_import.py"
NAMES = (
    "detect_profile",
    "suggest_column_map",
    "parse_csv_bytes",
    "restore_from_raw",
    "preview_against_history",
    "apply_csv_hours",
    "_decode",
    "_sniff",
    "_normalize_header",
    "_best_header",
    "_parse_number",
    "_parse_timestamp",
    "_detect_unit",
    "_to_ct_kwh",
    "default_local_tz",
)


@dataclass(frozen=True)
class HourPrice:
    timestamp: str
    total_price: float
    hour: int


class CsvImportError(Exception):
    def __init__(self, key, **placeholders):
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


class CsvImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = dict(
            csv=csv,
            hashlib=hashlib,
            io=io,
            json=json,
            re=re,
            datetime=datetime,
            timedelta=timedelta,
            timezone=timezone,
            Mapping=Mapping,
            ZoneInfo=ZoneInfo,
            CsvImportError=CsvImportError,
            HourPrice=HourPrice,
            ParseResult=lambda **values: SimpleNamespace(**values),
            PRICE_SOURCE_CSV="owned_csv",
        )
        source = Path(__file__).resolve().parents[1] / "verified_units" / RELATIVE
        for statement in ast.parse(source.read_text()).body:
            if not isinstance(statement, ast.Assign):
                continue
            if len(statement.targets) != 1 or not isinstance(
                statement.targets[0], ast.Name
            ):
                continue
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, TypeError):
                call = statement.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "frozenset"
                ):
                    value = frozenset(ast.literal_eval(call.args[0]))
                elif (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "re"
                    and call.func.attr == "compile"
                ):
                    value = re.compile(ast.literal_eval(call.args[0]))
                else:
                    continue
            namespace[statement.targets[0].id] = value
        cls.pair = functions(
            RELATIVE,
            NAMES,
            namespace,
            defaults={"restore_from_raw": (None,), "default_local_tz": (None,)},
            kwdefaults={
                "parse_csv_bytes": dict(
                    profile="auto",
                    price_unit="auto",
                    csv_timezone="local",
                    column_map=None,
                    local_tz=None,
                    now=None,
                    eeg_price=None,
                    base_prices=None,
                )
            },
        )

    def outcome(self, function, *arguments, **keywords):
        try:
            result = function(*arguments, **keywords)
            if isinstance(result, SimpleNamespace):
                result = vars(result)
            return "return", result
        except BaseException as error:
            return (
                "raise",
                type(error).__name__,
                str(error),
                getattr(error, "placeholders", None),
            )

    def assert_pair(self, name, *arguments, **keywords):
        result = [
            self.outcome(group[name], *arguments, **keywords) for group in self.pair
        ]
        self.assertEqual(*result)
        return result[0]

    def test_profiles_numbers_headers_and_timezones(self):
        for headers in (
            ["Date", "Price"],
            ["Datum", "Verbrauch kWh", "EEG Anteil"],
            ["timestamp", "total_price"],
            ["Preis", "Arbeitspreis", "Date"],
            [],
        ):
            self.assert_pair("detect_profile", headers)
            self.assert_pair("suggest_column_map", headers)
        for value in (
            None,
            "",
            "1,23",
            "1.23",
            "1.234,56",
            "1,234.56",
            "1 234",
            "2\u00a0500",
            "-4",
            "bad",
            0,
        ):
            self.assert_pair("_parse_number", value)
        for value in (b"Date;Price", b"\xef\xbb\xbfDate;Price", b"Pr\xe4is"):
            self.assert_pair("_decode", value)
        for samples in ([], [0.2, 0.3], [12, 14, 16], [150, 200]):
            self.assert_pair("_detect_unit", samples)
        for value in (0, 0.5, 15, -2):
            for unit in ("ct_kwh", "eur_kwh", "eur_mwh", "unknown"):
                self.assert_pair("_to_ct_kwh", value, unit)
        for value in (
            "2026-10-12T01:30:00Z",
            "12.10.2026 13:14:15",
            "2026-10-12",
            "bad",
            "",
        ):
            for mode in ("local", "utc"):
                self.assert_pair(
                    "_parse_timestamp",
                    value,
                    csv_timezone=mode,
                    local_tz=ZoneInfo("America/Toronto"),
                )
        for name in ("UTC", "America/Toronto", "Europe/Vienna", "owned/invalid"):
            # A fixed datetime stand-in makes the fallback deterministic.
            class FixedDateTime:
                @staticmethod
                def now(*arguments, **keywords):
                    return datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

            old = [
                group[name_].__globals__["datetime"]
                for group, name_ in (
                    (self.pair[0], "default_local_tz"),
                    (self.pair[1], "default_local_tz"),
                )
            ]
            try:
                for group in self.pair:
                    group["default_local_tz"].__globals__["datetime"] = FixedDateTime
                self.assert_pair("default_local_tz", name)
            finally:
                for group, value in zip(self.pair, old):
                    group["default_local_tz"].__globals__["datetime"] = value

    def test_price_and_community_rows_validation_and_limits(self):
        cases = (
            (b"Date;Price\n2026-10-12 01:15;12\n2026-10-12 01:45;18\n", {}),
            (b"Date,Price\n2026-10-12T02:00:00Z,0.12\n", {}),
            (
                b"Date;Price\n\n;\ninvalid;10\n2020-01-01;15\n2026-10-12;bad\n2026-10-12;12\n",
                {},
            ),
            (b"Date;Price\n2026-10-12;150\n", {"price_unit": "eur_mwh"}),
            (
                b"Datum;Verbrauch kWh;EEG kWh\n2026-10-12;10;4\n",
                {"eeg_price": 5, "base_prices": {"2026-10-12T00:00:00": 20}},
            ),
            (b"Datum;Verbrauch kWh;EEG kWh\n2026-10-12;10;4\n", {}),
            (b"Datum;Verbrauch kWh;EEG kWh\n2026-10-12;0;0\n", {"base_prices": {}}),
            (b"Datum;Verbrauch kWh;EEG kWh\n2026-10-12;10;bad\n", {}),
            (b"Date;Price\n2026-10-16;10\n", {}),
            (b"Date;Price\n2026-10-12;12\n", {"profile": "invalid"}),
            (b"x;y\n2026-10-12;12\n", {"column_map": {"timestamp": "x", "price": "y"}}),
            (b"x;y\n2026-10-12;12\n", {}),
            (b"", {}),
            (b";\n", {}),
        )
        options = dict(
            local_tz=timezone.utc, now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        )
        for data, extra in cases:
            with self.subTest(data=data, extra=extra):
                self.assert_pair("parse_csv_bytes", data, **(options | extra))
        result = self.assert_pair("parse_csv_bytes", cases[0][0], **options)
        self.assertEqual(
            result[1]["hours"], [HourPrice("2026-10-12T01:00:00", 15.0, 1)]
        )
        for limit, data in (
            ("CSV_MAX_BYTES", b"Date;Price\n2026-10-12;12\n"),
            ("CSV_MAX_ROWS", b"Date;Price\n2026-10-12;12\n2026-10-12;14\n"),
        ):
            saved = [group["parse_csv_bytes"].__globals__[limit] for group in self.pair]
            try:
                for group in self.pair:
                    group["parse_csv_bytes"].__globals__[limit] = 1
                result = self.assert_pair("parse_csv_bytes", data, **options)
                self.assertEqual(result[0], "raise")
            finally:
                for group, value in zip(self.pair, saved):
                    group["parse_csv_bytes"].__globals__[limit] = value

    def test_restore_previous_raw_prices_and_sources(self):
        rows = [
            dict(timestamp="a", total_price_raw=2, price_source="current"),
            dict(timestamp="b", total_price_raw=None),
            dict(
                timestamp="c",
                total_price_raw="3",
                price_source_original="old",
                price_source="current",
            ),
        ]
        for summary in (
            None,
            {},
            {"original_sources": {"a": "saved"}},
            {"original_sources": []},
        ):
            self.assert_pair("restore_from_raw", rows, summary)
        for rows_ in (
            [],
            [{"total_price_raw": 2}],
            [{"timestamp": "a", "total_price_raw": "bad"}],
        ):
            self.assert_pair("restore_from_raw", rows_)

    def test_preview_and_apply_database_order_faults_and_cancellation(self):
        hours = [
            HourPrice("2026-10-12T01:00:00", 15, 1),
            HourPrice("2026-10-12T02:00:00", 20, 2),
        ]
        for name in ("preview_against_history", "apply_csv_hours"):
            for scenario in ("new", "raw", "csv", "monthly"):
                for failure in (
                    "",
                    "fetchall",
                    "fetchone",
                    "execute",
                    "commit",
                    "cancel-fetchall",
                ):
                    outcomes = []
                    for group in self.pair:
                        events = []

                        def fail(stage):
                            if failure == stage:
                                raise ValueError("owned " + stage)
                            if failure == "cancel-" + stage:
                                raise asyncio.CancelledError("owned " + stage)

                        class Database:
                            async def require_price_write_schema(self):
                                events.append("schema")

                            async def fetchall(self, query, arguments):
                                events.append(("fetchall", query, arguments))
                                fail("fetchall")
                                if "DISTINCT month" in query:
                                    return [{"month": "2026-10"}]
                                if "SELECT id, kind" in query:
                                    return [{"id": 9, "kind": scenario}]
                                if scenario == "new":
                                    return []
                                return [
                                    dict(
                                        timestamp=hours[0].timestamp,
                                        total_price=10,
                                        total_price_raw=8,
                                        correction_id=None if scenario == "raw" else 9,
                                        price_source="previous",
                                    )
                                ]

                            async def fetchone(self, query, *arguments):
                                events.append(("fetchone", query, arguments))
                                fail("fetchone")
                                if "last_insert_rowid" in query:
                                    return {"id": 21}
                                if "AVG" in query:
                                    return dict(
                                        average_net=17,
                                        average_total=18,
                                        min_price=10,
                                        max_price=20,
                                    )
                                return {"price_revision": 5}

                            async def execute(self, query, arguments, *, auto_commit):
                                events.append(
                                    ("execute", query, arguments, auto_commit)
                                )
                                fail("execute")

                            async def commit(self):
                                events.append("commit")
                                fail("commit")

                        class Statistics:
                            async def async_update_daily_average(self, **values):
                                events.append(("statistics", values))

                        async def run():
                            if name == "preview_against_history":
                                return await group[name](Database(), hours)
                            return await group[name](
                                Database(),
                                hours,
                                stats_store=Statistics(),
                                params={"source": "owned"},
                                source_hash="owned-hash",
                                now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
                            )

                        try:
                            result = ("return", asyncio.run(run()))
                        except BaseException as error:
                            result = ("raise", type(error).__name__, str(error))
                        outcomes.append((result, events))
                    with self.subTest(name=name, scenario=scenario, failure=failure):
                        self.assertEqual(*outcomes)
