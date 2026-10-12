"""Paired license/error routing and corrected statistics with owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The verifier stand-ins exercise call routing, not cryptographic strength. No
vendor key, real license, integration module or native runtime is used.
"""

import ast
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from pure_reference import classes, functions

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class InvalidSignature(Exception):
    pass


class PayloadError(ValueError):
    pass


Status = Enum(
    "Status",
    {
        name: name.lower()
        for name in (
            "NOT_PROVIDED",
            "INVALID_FORMAT",
            "UNKNOWN_KEY_ID",
            "INVALID_SIGNATURE",
            "VALIDATION_ERROR",
            "WRONG_PRODUCT",
            "UNSUPPORTED_VERSION",
            "NOT_YET_VALID",
            "EXPIRED",
            "MISSING_ENTITLEMENT",
            "VALID",
        )
    },
)


@dataclass
class Payload:
    version: int
    key_id: str
    license_id: str
    product: str
    issued_at: datetime
    expires_at: datetime | None
    entitlements: frozenset
    customer_ref: str | None = None
    order_ref: str | None = None
    max_installations: int | None = None


@dataclass
class Result:
    status: Status
    payload: Payload | None
    entitlements: frozenset
    message_key: str


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ValidatorRoutingTests(unittest.TestCase):
    def validator_pair(self, relative, raw, failure):
        calls = []
        policy = SimpleNamespace(
            MAX_STRING_LENGTH=80,
            MAX_ENTITLEMENTS=10,
            KNOWN_ENTITLEMENTS={"full", "other"},
            MAX_KEY_LENGTH=1000,
            PREFIX="OWNED",
            MAX_PAYLOAD_BYTES=4096,
            SIGNING_CONTEXT=b"owned-context:",
            PRODUCT="owned-product",
            REQUIRED_ENTITLEMENT="full",
        )

        def decode(segment, maximum):
            calls.append(("decode", segment, maximum))
            if failure == "decode":
                raise ValueError("owned decode")
            if failure == "unexpected":
                raise RuntimeError("owned unexpected")
            if segment == "payload":
                return canonical(raw) if failure != "json" else b"bad json"
            return b"x" * (63 if failure == "short-signature" else 64)

        class PublicKey:
            @classmethod
            def from_public_bytes(cls, value):
                calls.append(("key", value))
                return cls()

            def verify(self, signature, data):
                calls.append(("verify", signature, data))
                if failure == "signature":
                    raise InvalidSignature("owned signature")

        globals_ = dict(
            json=json,
            Mapping=Mapping,
            datetime=Clock,
            timezone=timezone,
            InvalidSignature=InvalidSignature,
            Ed25519PublicKey=PublicKey,
            policy=policy,
            canonical_payload=canonical,
            decode_segment=decode,
            LicensePayload=Payload,
            LicenseStatus=Status,
            LicenseValidationResult=Result,
            _PayloadError=PayloadError,
            _REQUIRED={
                "v",
                "key_id",
                "product",
                "issued_at",
                "expires_at",
                "license_id",
                "entitlements",
            },
            _OPTIONAL={"order_ref", "customer_ref", "max_installations"},
            PRODUCTION_PUBLIC_KEYS={},
        )
        # GPM qualifies policy constants; EAI imports the same constants
        # directly. Supply both original dependency shapes.
        globals_.update(vars(policy))
        return classes(relative, "OfflineLicenseValidator", globals_), calls

    def test_validator_status_routing_and_signature_call_data(self):
        base = {
            "v": 1,
            "key_id": "owned-key",
            "license_id": "owned-id",
            "product": "owned-product",
            "issued_at": "2026-10-01T00:00:00Z",
            "expires_at": None,
            "entitlements": ["full"],
        }
        scenarios = [
            (base, "", "OWNED.payload.signature", Status.VALID),
            (base, "signature", "OWNED.payload.signature", Status.INVALID_SIGNATURE),
            (base, "decode", "OWNED.payload.signature", Status.INVALID_FORMAT),
            (base, "unexpected", "OWNED.payload.signature", Status.VALIDATION_ERROR),
            (base, "short-signature", "OWNED.payload.signature", Status.INVALID_FORMAT),
            (base, "json", "OWNED.payload.signature", Status.INVALID_FORMAT),
            (
                dict(base, product="other"),
                "",
                "OWNED.payload.signature",
                Status.WRONG_PRODUCT,
            ),
            (
                dict(base, v=2),
                "",
                "OWNED.payload.signature",
                Status.UNSUPPORTED_VERSION,
            ),
            (
                dict(base, issued_at="2026-10-13T00:00:00Z"),
                "",
                "OWNED.payload.signature",
                Status.NOT_YET_VALID,
            ),
            (
                dict(base, expires_at="2026-10-12T12:00:00Z"),
                "",
                "OWNED.payload.signature",
                Status.EXPIRED,
            ),
            (
                dict(base, entitlements=["other"]),
                "",
                "OWNED.payload.signature",
                Status.MISSING_ENTITLEMENT,
            ),
            (
                dict(base, key_id="missing"),
                "",
                "OWNED.payload.signature",
                Status.UNKNOWN_KEY_ID,
            ),
            (
                dict(base, entitlements=["full", "full"]),
                "",
                "OWNED.payload.signature",
                Status.INVALID_FORMAT,
            ),
            (base, "", "", Status.NOT_PROVIDED),
            (base, "", "wrong", Status.INVALID_FORMAT),
        ]
        for feature in ("grid_price_monitor", "solar_forecast_eai"):
            relative = f"extra_features/{feature}/license/validator.py"
            for raw, failure, key, expected in scenarios:
                owners, calls = self.validator_pair(relative, raw, failure)
                outputs = []
                for owner in owners:
                    calls.clear()
                    result = owner({"owned-key": b"owned-public"}).validate(key)
                    outputs.append((result, list(calls)))
                self.assertEqual(outputs[1], outputs[0])
                self.assertEqual(outputs[0][0].status, expected)
                if expected == Status.VALID:
                    self.assertEqual(
                        outputs[0][1][-1],
                        ("verify", b"x" * 64, b"owned-context:" + canonical(raw)),
                    )

    def test_schema_and_datetime_helpers_keep_type_limits(self):
        raw = {
            "v": 1,
            "key_id": "owned-key",
            "license_id": "owned-id",
            "product": "owned-product",
            "issued_at": "2026-10-01T00:00:00Z",
            "expires_at": None,
            "entitlements": ["full"],
        }
        for feature in ("grid_price_monitor", "solar_forecast_eai"):
            owners, _ = self.validator_pair(
                f"extra_features/{feature}/license/validator.py", raw, ""
            )
            for value in (
                "2026-10-01T00:00:00Z",
                "2026-10-01",
                None,
                10,
                "x" * 41 + "Z",
            ):
                outputs = []
                for owner in owners:
                    try:
                        result = owner._datetime(value, "issued_at")
                    except ValueError as error:
                        result = (type(error).__name__, str(error))
                    outputs.append(result)
                self.assertEqual(outputs[1], outputs[0])
            for change in (
                {"v": True},
                {"max_installations": 0},
                {"max_installations": True},
                {"unexpected": 1},
                {"entitlements": []},
            ):
                outputs = []
                for owner in owners:
                    try:
                        result = owner()._parse(dict(raw, **change))
                    except ValueError as error:
                        result = (type(error).__name__, str(error))
                    outputs.append(result)
                self.assertEqual(outputs[1], outputs[0])
                self.assertIsInstance(outputs[0], tuple)


RELATIVE = "extra_features/sfml_stats/api/statistics_views.py"


class StatisticsViewTests(unittest.TestCase):
    def pair(self):
        calls = []
        registry = [
            {"id": "load", "daily_column": "load_kwh"},
            {"id": "solar", "daily_column": "solar_kwh"},
        ]

        async def daily(db, start, end, projection):
            calls.append(("daily", start, end, projection))
            return {
                "2026-10-11": {"load": 2.1234, "raw_load_kwh": 2, "solar": 1},
                "2026-10-12": {"load": 3, "solar": 4},
            }

        async def cost(db, start, end, projection, *, config=None):
            calls.append(("cost", start, end, projection, config))
            return {"energy_eur": 2.5, "kwh": 5.1234}

        web = SimpleNamespace(json_response=lambda payload, **kwargs: (payload, kwargs))
        globals_ = dict(
            datetime=datetime,
            timedelta=timedelta,
            csv=csv,
            io=io,
            json=json,
            web=web,
            METRIC_REGISTRY=registry,
            _PROVIDER_VERSION=8,
            _MAX_PERIOD_DAYS=8000,
            _DATASETS=("day", "month", "year"),
            _PREMIUM_REQUIRED="premium_required",
            _UNLICENSED_STATUSES={"expired", "not_provided"},
            corrected_daily=daily,
            statement_energy_cost=cost,
        )
        tree = ast.parse(
            (
                Path(__file__).resolve().parents[1] / "verified_units" / RELATIVE
            ).read_text()
        )
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        return (
            functions(
                RELATIVE,
                names,
                globals_,
                kwdefaults={"build_statistics": {"config": None}},
            ),
            calls,
        )

    def test_license_snapshot_and_gates_keep_missing_and_expired_telemetry_closed(self):
        pair, _ = self.pair()
        valid = {
            "provider_version": 8,
            "license_status": "valid",
            "premium_full_package": True,
        }
        for snapshot in (
            None,
            {},
            valid,
            dict(valid, provider_version=7),
            dict(valid, license_status="expired"),
            dict(valid, premium_full_package=1),
        ):
            hass = SimpleNamespace(
                data={
                    "solar_forecast_eai": {
                        "capability_provider": SimpleNamespace(
                            snapshot=lambda: snapshot
                        )
                    }
                }
            )
            request = SimpleNamespace(app={"hass": hass})
            for name, args in (
                ("license_snapshot", (hass,)),
                ("license_is_live", (snapshot,)),
                ("license_gate", (request,)),
            ):
                self.assertEqual(pair[1][name](*args), pair[0][name](*args))

        def failure():
            raise RuntimeError("owned snapshot")

        broken = SimpleNamespace(
            data={
                "solar_forecast_eai": {
                    "capability_provider": SimpleNamespace(snapshot=failure)
                }
            }
        )
        self.assertIsNone(pair[1]["license_snapshot"](broken))
        self.assertEqual(
            pair[1]["license_gate"](SimpleNamespace(app={}))[1], {"status": 403}
        )

    def test_statistics_aggregates_exports_and_corrected_flags(self):
        pair, calls = self.pair()
        outputs = []
        for api in pair:
            calls.clear()
            payload = asyncio.run(
                api["build_statistics"](
                    object(),
                    "2026-10-11",
                    "2026-10-12",
                    {"2026-10-12": {"solar": 4}},
                    config={"owned": True},
                )
            )
            outputs.append(
                (payload, api["statistics_export_map"](payload), list(calls))
            )
        self.assertEqual(outputs[1], outputs[0])
        payload, exports, _ = outputs[0]
        self.assertEqual(payload["totals"]["metrics"], {"load": 5.123, "solar": 5.0})
        self.assertTrue(payload["totals"]["corrected"])
        self.assertEqual(
            set(exports),
            {
                "statistics_day.csv",
                "statistics_month.csv",
                "statistics_year.csv",
                "metadata.json",
            },
        )
        self.assertIn(
            "period;corrected;cost_eur;load;solar", exports["statistics_day.csv"]
        )
        for api in pair:
            with self.assertRaisesRegex(ValueError, "invalid_period"):
                api["dataset_rows"](payload, "hour")

    def test_explicit_period_aliases_and_invalid_dates(self):
        pair, _ = self.pair()
        cases = (
            {"from": "2026-10-01", "to": "2026-10-12"},
            {"start": "2026-10-01", "end": "2026-10-12"},
            {"from": "2026-10-13", "to": "2026-10-12"},
            {"from": "2026-02-30", "to": "2026-10-12"},
            {"from": "2000-01-01", "to": "2026-10-12"},
        )
        for query in cases:
            outputs = []
            for api in pair:
                try:
                    result = api["parse_period"](SimpleNamespace(query=query))
                except ValueError as error:
                    result = (type(error).__name__, str(error))
                outputs.append(result)
            self.assertEqual(outputs[1], outputs[0])
