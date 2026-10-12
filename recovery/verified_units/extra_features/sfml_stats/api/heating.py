# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Room heating API. License and feature gates apply to every call.

Any signed-in Home Assistant user may read the room status, change a room's
comfort temperature and resume a paused room (dashboard card). Creating,
editing and deleting rooms, settings and the observe/switch flag stay admin-only.
"""
from __future__ import annotations
import json
import logging
from typing import Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.heating import (
    HeatingError,
    get_heating_manager,
    public_status,
    validate_boost,
)
from .premium_dashboard import _eai_config, _eai_feature_enabled
from .statistics_views import license_denied, license_is_live, license_snapshot
from .views import local_or_trusted_cloud_only

_LOGGER = logging.getLogger(__name__)
COMFORT_STEP_C = 0.5
COMFORT_INPUT_MIN_C = 0.0
COMFORT_INPUT_MAX_C = 100.0
POLICY_CONTROL = "control"


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


class HeatingView(HomeAssistantView):
    requires_auth = True
    cors_allowed = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    def _authorize(self, request: web.Request, *, admin: bool) -> web.Response | None:
        user = request.get("hass_user")
        if user is None:
            return _error("auth_required", "A signed-in user is required.", 401)
        if admin and (not bool(getattr(user, "is_admin", False))):
            return _error("admin_required", "Administrator access is required.", 403)
        if not license_is_live(license_snapshot(self.hass)):
            return license_denied()
        if not _eai_feature_enabled(_eai_config(self.hass), "heating_enabled"):
            return _error("feature_disabled", "Room heating is switched off.", 403)
        if get_heating_manager(self.hass) is None:
            return _error(
                "heating_unavailable", "Heating runtime is not available.", 503
            )

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise HeatingError(
                "invalid_content_type", "Content-Type must be application/json."
            )
        raw_body = await request.read()
        if len(raw_body) > 8192:
            raise HeatingError("body_too_large", "Request body is too large.")
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise HeatingError("invalid_json", "A JSON object is required.") from error
        if not isinstance(body, dict):
            raise HeatingError("invalid_json", "A JSON object is required.")
        return body

    def _check_room_control(self, request: web.Request, room_id: str) -> None:
        """Non-admins need HA control permission on every thermostat of the room.

        Keeps read-only users and entity-level restrictions effective, because the
        regulator writes the room's climate entities on the user's behalf.
        """
        user = request.get("hass_user")
        if user is None or bool(getattr(user, "is_admin", False)):
            return None
        manager = get_heating_manager(self.hass)
        room = next(
            (
                candidate_room
                for candidate_room in getattr(manager, "rooms", [])
                if candidate_room.id == room_id
            ),
            None,
        )
        if room is None:
            raise HeatingError("unknown_room", "Unknown room.")
        permissions = getattr(user, "permissions", None)
        for entity_id in room.climate_entities:
            if permissions is not None:
                if permissions.check_entity(entity_id, POLICY_CONTROL):
                    continue
            raise HeatingError("control_denied", "Not allowed to control this room.")

    async def _run(
        self, request: web.Request, operation: Any, *, admin: bool
    ) -> web.Response:
        authorization_error = self._authorize(request, admin=admin)
        if authorization_error is not None:
            return authorization_error
        try:
            result = await operation()
        except HeatingError as error:
            status_code = {"unknown_room": 404, "control_denied": 403}.get(
                error.code, 400
            )
            return _error(error.code, str(error), status_code)
        return web.json_response(
            {"success": True, "data": result}, headers={"Cache-Control": "no-store"}
        )


def _member_status(body: dict[str, Any]) -> dict[str, Any]:
    """Data minimisation for non-admins: no configuration, no per-person details."""
    persons = body.get("persons") or []
    member_status = {
        field_name: field_value
        for field_name, field_value in body.items()
        if field_name not in {"persons", "settings"}
    }
    member_status["persons_home"] = sum(
        (1 for person in persons if person.get("state") == "home")
    )
    member_status["persons_total"] = len(persons)
    member_status["rooms"] = [
        {
            field_name: field_value
            for field_name, field_value in room.items()
            if field_name not in {"away_since", "presence_persons"}
        }
        for room in body.get("rooms") or []
    ]
    return member_status


class HeatingStatusView(HeatingView):
    url = "/api/sfml_stats/heating/status"
    name = "api:sfml_stats:heating:status"

    async def get(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None:
            return _error("auth_required", "A signed-in user is required.", 401)
        is_admin = bool(getattr(user, "is_admin", False))
        license_live = license_is_live(license_snapshot(self.hass))
        feature_enabled = _eai_feature_enabled(
            _eai_config(self.hass), "heating_enabled"
        )
        manager = get_heating_manager(self.hass)
        runtime_status = None
        if license_live and feature_enabled:
            if manager is not None:
                runtime_status = await manager.async_status()
                if not is_admin:
                    runtime_status = _member_status(runtime_status)
        status = public_status(license_live, feature_enabled, runtime_status)
        status["can_manage"] = is_admin
        return web.json_response(
            {"success": True, "data": status}, headers={"Cache-Control": "no-store"}
        )


class HeatingSettingsView(HeatingView):
    url = "/api/sfml_stats/heating/settings"
    name = "api:sfml_stats:heating:settings"

    async def get(self, request: web.Request) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            status_payload = await manager.async_status()
            return status_payload["settings"]

        return await self._run(request, operation, admin=True)

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            return await manager.async_update_settings(await self._body(request))

        return await self._run(request, operation, admin=True)


class HeatingRoomsView(HeatingView):
    url = "/api/sfml_stats/heating/rooms"
    name = "api:sfml_stats:heating:rooms"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            return await manager.async_add_room(await self._body(request))

        return await self._run(request, operation, admin=True)


class HeatingRoomView(HeatingView):
    url = "/api/sfml_stats/heating/rooms/{room_id}"
    name = "api:sfml_stats:heating:room"

    @local_or_trusted_cloud_only
    async def delete(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            await manager.async_delete_room(room_id)
            return {"id": room_id, "deleted": True}

        return await self._run(request, operation, admin=True)

    @local_or_trusted_cloud_only
    async def put(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            return await manager.async_update_room(room_id, await self._body(request))

        return await self._run(request, operation, admin=True)


class HeatingRoomActiveView(HeatingView):
    url = "/api/sfml_stats/heating/rooms/{room_id}/active"
    name = "api:sfml_stats:heating:room_active"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            body = await self._body(request)
            if set(body) != {"active"} or not isinstance(body.get("active"), bool):
                raise HeatingError(
                    "invalid_active", "A boolean active flag is required."
                )
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            return await manager.async_set_active(room_id, body["active"])

        return await self._run(request, operation, admin=True)


class HeatingRoomResumeView(HeatingView):
    url = "/api/sfml_stats/heating/rooms/{room_id}/resume"
    name = "api:sfml_stats:heating:room_resume"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            self._check_room_control(request, room_id)
            result = await manager.async_resume(room_id)
            _LOGGER.info(
                "Heating room %s control resumed by user %s",
                room_id,
                getattr(request.get("hass_user"), "id", "unknown"),
            )
            return result

        return await self._run(request, operation, admin=False)


class HeatingRoomComfortView(HeatingView):
    url = "/api/sfml_stats/heating/rooms/{room_id}/comfort"
    name = "api:sfml_stats:heating:room_comfort"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            body = await self._body(request)
            comfort_temperature = body.get("comfort_temp_c")
            if (
                set(body) != {"comfort_temp_c"}
                or isinstance(comfort_temperature, bool)
                or (not isinstance(comfort_temperature, (int, float)))
                or (
                    not COMFORT_INPUT_MIN_C
                    <= comfort_temperature
                    <= COMFORT_INPUT_MAX_C
                )
            ):
                raise HeatingError(
                    "invalid_comfort", "A numeric comfort_temp_c is required."
                )
            rounded_temperature = round(
                round(float(comfort_temperature) / COMFORT_STEP_C) * COMFORT_STEP_C, 1
            )
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            self._check_room_control(request, room_id)
            room = next(
                (
                    candidate_room
                    for candidate_room in manager.rooms
                    if candidate_room.id == room_id
                ),
                None,
            )
            if room is not None and room.comfort_temp_c == rounded_temperature:
                return {
                    "id": room_id,
                    "comfort_temp_c": rounded_temperature,
                    "unchanged": True,
                }
            result = await manager.async_update_room(
                room_id, {"comfort_temp_c": rounded_temperature}
            )
            user = request.get("hass_user")
            _LOGGER.info(
                "Heating room %s comfort temperature set to %.1f °C by user %s",
                room_id,
                rounded_temperature,
                getattr(user, "id", "unknown"),
            )
            return result

        return await self._run(request, operation, admin=False)


class HeatingRoomBoostView(HeatingView):
    url = "/api/sfml_stats/heating/rooms/{room_id}/boost"
    name = "api:sfml_stats:heating:room_boost"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            boost_temperature, boost_minutes = validate_boost(await self._body(request))
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            self._check_room_control(request, room_id)
            result = await manager.async_set_boost(
                room_id, boost_temperature, boost_minutes
            )
            user = request.get("hass_user")
            _LOGGER.info(
                "Heating room %s boost set to %.1f °C for %s minutes by user %s",
                room_id,
                boost_temperature,
                boost_minutes,
                getattr(user, "id", "unknown"),
            )
            return result

        return await self._run(request, operation, admin=False)

    @local_or_trusted_cloud_only
    async def delete(self, request: web.Request, room_id: str) -> web.Response:

        async def operation() -> dict[str, Any]:
            manager = get_heating_manager(self.hass)
            if manager is None:
                raise HeatingError(
                    "heating_unavailable", "Heating runtime is not available."
                )
            self._check_room_control(request, room_id)
            result = await manager.async_clear_boost(room_id)
            _LOGGER.info(
                "Heating room %s boost cleared by user %s",
                room_id,
                getattr(request.get("hass_user"), "id", "unknown"),
            )
            return result

        return await self._run(request, operation, admin=False)


async def async_setup_heating_views(hass: Any) -> None:
    for view_class in (
        HeatingStatusView,
        HeatingSettingsView,
        HeatingRoomsView,
        HeatingRoomView,
        HeatingRoomActiveView,
        HeatingRoomResumeView,
        HeatingRoomComfortView,
        HeatingRoomBoostView,
    ):
        hass.http.register_view(view_class(hass))
