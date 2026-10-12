"""Published DEV state and delegation tested with owned payloads and callbacks.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No real token, license service or Home Assistant instance is used.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from pure_reference import functions

MODULE = "extra_features/sfml_stats/core/dev_mode.py"
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
INACTIVE = {
    "active": False,
    "expires_at": None,
    "install_hash": "",
    "status": "not_provided",
}


class Clock:
    @staticmethod
    def fromisoformat(value):
        return datetime.fromisoformat(value)

    @staticmethod
    def now(zone):
        return NOW


class Logger:
    def __init__(self):
        self.calls = []

    def warning(self, *args):
        self.calls.append(args)


def pair(logger=None):
    return functions(
        MODULE,
        [
            "_expiry_in_future",
            "dev_state",
            "is_dev_active",
            "public_dev_state",
            "_dev_mode_api",
            "forward_dev_token",
        ],
        {
            "datetime": Clock,
            "timezone": timezone,
            "_LOGGER": logger or Logger(),
            "_INACTIVE": dict(INACTIVE),
            "_SFML_DOMAIN": "solar_forecast_ml",
        },
        kwdefaults={
            "public_dev_state": {"include_install_hash": False},
            "forward_dev_token": {"clear": False},
        },
    )


class StatsDevStateTests(unittest.TestCase):
    def test_expiry_published_payload_and_optional_install_id_are_preserved(self):
        original, recovered = pair()
        values = (
            None,
            "",
            3,
            "invalid",
            "2026-10-12T12:00:00Z",
            "2026-10-12T11:59:59Z",
            "2026-10-12T12:00:01Z",
            "2026-10-12T15:00:00+02:00",
            "2026-10-12T13:00:00",
            "x" * 41,
        )
        for value in values:
            self.assertEqual(
                recovered["_expiry_in_future"](value),
                original["_expiry_in_future"](value),
            )
            for active in (False, True, 1):
                hass = SimpleNamespace(
                    data={
                        "solar_forecast_ml": {
                            "dev_mode": {
                                "active": active,
                                "expires_at": value,
                                "install_hash": "owned-install",
                                "status": "owned-status",
                            }
                        }
                    }
                )
                for name in ("dev_state", "is_dev_active", "public_dev_state"):
                    self.assertEqual(recovered[name](hass), original[name](hass))
                self.assertNotIn("install_hash", recovered["public_dev_state"](hass))
                self.assertEqual(
                    recovered["public_dev_state"](hass, include_install_hash=True),
                    original["public_dev_state"](hass, include_install_hash=True),
                )
        for data in (
            None,
            {},
            {"solar_forecast_ml": None},
            {"solar_forecast_ml": {}},
            {"solar_forecast_ml": {"dev_mode": []}},
            {
                "solar_forecast_ml": {
                    "dev_mode": {
                        "active": True,
                        "expires_at": "invalid",
                        "install_hash": 3,
                        "status": "",
                    }
                }
            },
        ):
            hass = SimpleNamespace(data=data)
            self.assertEqual(recovered["dev_state"](hass), original["dev_state"](hass))
        hass = SimpleNamespace(
            data={
                "solar_forecast_ml": {
                    "dev_mode": {"active": True, "expires_at": "2026-10-12T12:00:01Z"}
                }
            }
        )
        self.assertTrue(recovered["is_dev_active"](hass))
        self.assertEqual(recovered["dev_state"](SimpleNamespace(data=None)), INACTIVE)

    def test_owned_forward_callbacks_keep_validation_results_logs_and_cancellation(
        self,
    ):
        both = pair()
        for token in (None, "", "   ", 7, "owned-test-token"):
            for clear in (False, True):
                for mode in (
                    "ok",
                    "missing",
                    "noncallable",
                    "none",
                    "empty",
                    "error",
                    "cancel",
                ):
                    outcomes = []
                    for namespace in both:
                        calls, logger = [], Logger()
                        namespace["forward_dev_token"].__globals__["_LOGGER"] = logger

                        async def set_token(value):
                            calls.append(("set", value))
                            await asyncio.sleep(0)
                            if mode == "error":
                                raise ValueError("owned-test-token must stay unlogged")
                            if mode == "cancel":
                                raise asyncio.CancelledError("owned cancelled")
                            return (
                                None
                                if mode == "none"
                                else "" if mode == "empty" else "owned-status"
                            )

                        async def clear_token():
                            calls.append(("clear",))
                            await asyncio.sleep(0)
                            if mode == "error":
                                raise ValueError("owned-test-token must stay unlogged")
                            if mode == "cancel":
                                raise asyncio.CancelledError("owned cancelled")

                        api = {"set": set_token, "clear": clear_token}
                        if mode == "missing":
                            api = {}
                        if mode == "noncallable":
                            api = {"set": 7, "clear": 7}
                        hass = SimpleNamespace(
                            data={"solar_forecast_ml": {"dev_mode_api": api}}
                        )
                        try:
                            result = asyncio.run(
                                namespace["forward_dev_token"](hass, token, clear=clear)
                            )
                        except asyncio.CancelledError as error:
                            result = ("cancelled", str(error))
                        outcomes.append((result, calls, logger.calls))
                        self.assertNotIn("owned-test-token", repr(logger.calls))
                    self.assertEqual(outcomes[1], outcomes[0])
                    if mode == "ok" and clear:
                        self.assertEqual(outcomes[0][0], "cleared")
                    if mode == "ok" and token == "owned-test-token" and not clear:
                        self.assertEqual(outcomes[0][0], "owned-status")
                    if not clear and token != "owned-test-token":
                        self.assertEqual(outcomes[0], ("invalid", [], []))

    def test_required_response_retains_owned_import_status_headers_and_body(self):
        calls = []

        def response(value, **options):
            calls.append((value, options))
            return value, options

        def importer(name, globals=None, locals=None, fromlist=(), level=0):
            self.assertEqual((name, tuple(fromlist), level), ("aiohttp", ("web",), 0))
            return SimpleNamespace(web=SimpleNamespace(json_response=response))

        original, recovered = functions(
            MODULE,
            ["json_dev_required"],
            {},
            builtins_overrides={"__import__": importer},
        )
        self.assertEqual(
            recovered["json_dev_required"](), original["json_dev_required"]()
        )
        self.assertEqual(
            calls[0],
            (
                {"success": False, "error": {"code": "dev_mode_required"}, "data": {}},
                {"status": 403, "headers": {"Cache-Control": "no-store"}},
            ),
        )


if __name__ == "__main__":
    unittest.main()
