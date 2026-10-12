"""Compare verified HTTP-view code with owned requests, gates and managers.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No Home Assistant module, HTTP server, real license or actuator is used.
"""

import asyncio
import builtins
import json
from types import FunctionType, SimpleNamespace
import unittest

from pure_reference import verified_code

RELATIVE = "extra_features/sfml_stats/api/heating.py"
VIEW_NAMES = (
    "HeatingView",
    "HeatingStatusView",
    "HeatingSettingsView",
    "HeatingRoomsView",
    "HeatingRoomView",
    "HeatingRoomActiveView",
    "HeatingRoomResumeView",
    "HeatingRoomComfortView",
    "HeatingRoomBoostView",
)


class HeatingError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Request(dict):
    def __init__(self, user=None, body=b"{}", content_type="application/json"):
        super().__init__(hass_user=user)
        self.body, self.content_type, self.reads = body, content_type, 0

    async def read(self):
        self.reads += 1
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


def error_value(error):
    cause = error.__cause__
    return (
        type(error).__name__,
        str(error),
        getattr(error, "code", None),
        None if cause is None else (type(cause).__name__, str(cause)),
        error.__suppress_context__,
    )


async def async_outcome(work):
    try:
        return ("value", await work)
    except (Exception, asyncio.CancelledError) as error:
        return error_value(error)


def sync_outcome(work, *args, **kwargs):
    try:
        return ("value", work(*args, **kwargs))
    except Exception as error:
        return error_value(error)


def model(module, hass, calls):
    from bytecode_fidelity import units

    def response(payload, *, status=200, headers=None):
        return {"payload": payload, "status": status, "headers": headers}

    namespace = {
        "__name__": "owned_heating_fixture",
        "__builtins__": builtins.__dict__,
        "json": json,
        "web": SimpleNamespace(json_response=response),
        "HeatingError": HeatingError,
        "HomeAssistantView": type("OwnedView", (), {}),
        "COMFORT_STEP_C": 0.5,
        "COMFORT_INPUT_MIN_C": 0.0,
        "COMFORT_INPUT_MAX_C": 100.0,
        "POLICY_CONTROL": "control",
        "local_or_trusted_cloud_only": lambda work: work,
        "get_heating_manager": lambda value: value.manager,
        "license_snapshot": lambda value: value.live,
        "license_is_live": bool,
        "license_denied": lambda: response({"denied": True}, status=403),
        "_eai_config": lambda value: value.enabled,
        "_eai_feature_enabled": lambda config, feature: bool(config),
        "public_status": lambda live, enabled, body: {
            "live": live,
            "enabled": enabled,
            "body": body,
        },
        "validate_boost": lambda body: (body["temp_c"], body["minutes"]),
        "_LOGGER": SimpleNamespace(
            info=lambda message, *args: calls.append(("log", message, args))
        ),
    }
    code_units = dict(units(module))
    for name in ("_error", "_member_status", "async_setup_heating_views"):
        namespace[name] = FunctionType(code_units[f"<module>/{name}#1"], namespace)
    for name in VIEW_NAMES:
        base = (
            namespace["HomeAssistantView"]
            if name == "HeatingView"
            else namespace["HeatingView"]
        )
        namespace[name] = builtins.__build_class__(
            FunctionType(code_units[f"<module>/{name}#1"], namespace), name, base
        )
    return namespace


class HeatingAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = verified_code(RELATIVE)

    def test_authorization_run_status_mapping_unhandled_errors_and_cancellation(self):
        from itertools import product

        async def exercise(
            module, is_admin, require_admin, live, enabled, manager, operation_error
        ):
            calls = []
            hass = SimpleNamespace(
                live=live, enabled=enabled, manager=object() if manager else None
            )
            namespace = model(module, hass, calls)
            user = None if is_admin is None else SimpleNamespace(is_admin=is_admin)
            request = Request(user)
            view = namespace["HeatingView"](hass)

            async def operation():
                calls.append("operation")
                if operation_error == "cancel":
                    raise asyncio.CancelledError()
                if operation_error == "runtime":
                    raise RuntimeError("unhandled")
                if operation_error:
                    raise HeatingError(operation_error, "operation failed")
                return {"result": 7}

            return (
                await async_outcome(view._run(request, operation, admin=require_admin)),
                calls,
            )

        for admin, require, live, enabled, manager in product(
            (None, False, True),
            (False, True),
            (False, True),
            (False, True),
            (False, True),
        ):
            for error in (
                "",
                "unknown_room",
                "control_denied",
                "invalid",
                "runtime",
                "cancel",
            ):
                arguments = admin, require, live, enabled, manager, error
                self.assertEqual(
                    asyncio.run(exercise(self.modules[1], *arguments)),
                    asyncio.run(exercise(self.modules[0], *arguments)),
                )

    def test_body_content_type_size_json_shape_utf8_errors_and_cancellation(self):
        cases = (
            (b"{}", "application/json"),
            (b'{"value":7}', "application/json"),
            (b"[]", "application/json"),
            (b"null", "application/json"),
            (b"bad", "application/json"),
            (b"\xff", "application/json"),
            (b" " * 8190 + b"{}", "application/json"),
            (b" " * 8191 + b"{}", "application/json"),
            (b"{}", "text/plain"),
            (RuntimeError("read failed"), "application/json"),
            (asyncio.CancelledError(), "application/json"),
        )

        async def exercise(module, body, content_type):
            namespace = model(module, None, [])
            request = Request(body=body, content_type=content_type)
            return (
                await async_outcome(namespace["HeatingView"]._body(request)),
                request.reads,
            )

        for body, content_type in cases:
            self.assertEqual(
                asyncio.run(exercise(self.modules[1], body, content_type)),
                asyncio.run(exercise(self.modules[0], body, content_type)),
            )

    def test_room_permissions_keep_admin_bypass_unknown_room_and_each_entity_check(
        self,
    ):
        cases = (
            (None, None, "room"),
            (True, None, "missing"),
            (False, None, "room"),
            (False, {}, "missing"),
            (False, {"climate.a": True, "climate.b": True}, "room"),
            (False, {"climate.a": True, "climate.b": False}, "room"),
            (False, {"climate.a": False, "climate.b": True}, "room"),
        )

        def exercise(module, admin, allowed, room_id):
            calls = []

            def permission(entity, policy):
                calls.append((entity, policy))
                return allowed.get(entity, False)

            user = (
                None
                if admin is None
                else SimpleNamespace(
                    is_admin=admin,
                    permissions=(
                        None
                        if allowed is None
                        else SimpleNamespace(check_entity=permission)
                    ),
                )
            )
            manager = SimpleNamespace(
                rooms=[
                    SimpleNamespace(
                        id="room", climate_entities=["climate.a", "climate.b"]
                    )
                ]
            )
            hass = SimpleNamespace(manager=manager)
            view = model(module, hass, calls)["HeatingView"](hass)
            return sync_outcome(view._check_room_control, Request(user), room_id), calls

        for arguments in cases:
            self.assertEqual(
                exercise(self.modules[1], *arguments),
                exercise(self.modules[0], *arguments),
            )

    def test_comfort_validation_rounding_unchanged_updates_manager_errors_and_cancellation(
        self,
    ):
        cases = (
            {},
            {"comfort_temp_c": True},
            {"comfort_temp_c": "21"},
            {"comfort_temp_c": None},
            {"comfort_temp_c": -0.1},
            {"comfort_temp_c": 100.1},
            {"comfort_temp_c": float("nan")},
            {"comfort_temp_c": 21.25},
            {"comfort_temp_c": 21.75},
            {"comfort_temp_c": 21.5},
            {"comfort_temp_c": 0.0},
            {"comfort_temp_c": 100.0},
            {"comfort_temp_c": 21, "extra": 1},
        )

        async def exercise(module, body, state):
            calls = []

            async def update(room_id, payload):
                calls.append(("update", room_id, payload))
                if state == "failure":
                    raise HeatingError("unknown_room", "missing")
                if state == "cancel":
                    raise asyncio.CancelledError()
                return {"id": room_id, **payload}

            manager = (
                None
                if state == "missing"
                else SimpleNamespace(
                    rooms=[SimpleNamespace(id="room", comfort_temp_c=21.5)],
                    async_update_room=update,
                )
            )
            hass = SimpleNamespace(manager=manager)
            view = model(module, hass, calls)["HeatingRoomComfortView"](hass)

            async def run(request, operation, *, admin):
                calls.append(("run", admin))
                return await operation()

            view._run = run
            view._check_room_control = lambda request, room_id: calls.append(
                ("control", room_id)
            )
            request = Request(SimpleNamespace(id="owned"), json.dumps(body).encode())
            return await async_outcome(view.post(request, "room")), calls, request.reads

        for body in cases:
            for state in ("available", "missing", "failure", "cancel"):
                self.assertEqual(
                    asyncio.run(exercise(self.modules[1], body, state)),
                    asyncio.run(exercise(self.modules[0], body, state)),
                )

    def test_status_privacy_runtime_gates_and_registration_order(self):
        from itertools import product

        async def exercise(module, admin, live, enabled, available):
            calls = []
            body = {
                "settings": {"secret": 1},
                "persons": [{"state": "home"}, {"state": "away"}],
                "rooms": [
                    {
                        "id": "room",
                        "away_since": 123,
                        "presence_persons": ["private"],
                        "value": 7,
                    }
                ],
            }

            async def status():
                calls.append("status")
                return body

            hass = SimpleNamespace(
                manager=SimpleNamespace(async_status=status) if available else None,
                live=live,
                enabled=enabled,
                http=SimpleNamespace(
                    register_view=lambda view: calls.append(
                        ("register", type(view).__name__)
                    )
                ),
            )
            namespace = model(module, hass, calls)
            request = Request(
                None if admin is None else SimpleNamespace(is_admin=admin)
            )
            result = await namespace["HeatingStatusView"](hass).get(request)
            await namespace["async_setup_heating_views"](hass)
            return result, calls, body

        for arguments in product(
            (None, False, True), (False, True), (False, True), (False, True)
        ):
            self.assertEqual(
                asyncio.run(exercise(self.modules[1], *arguments)),
                asyncio.run(exercise(self.modules[0], *arguments)),
            )
        result, calls, original = asyncio.run(
            exercise(self.modules[1], False, True, True, True)
        )
        public = result["payload"]["data"]["body"]
        self.assertNotIn("settings", public)
        self.assertNotIn("persons", public)
        self.assertNotIn("presence_persons", public["rooms"][0])
        self.assertEqual((public["persons_home"], public["persons_total"]), (1, 2))
        self.assertIn("settings", original)
        self.assertEqual(
            [
                call[1]
                for call in calls
                if isinstance(call, tuple) and call[0] == "register"
            ],
            list(VIEW_NAMES[1:]),
        )


if __name__ == "__main__":
    unittest.main()
