# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Authenticated admin API for the STATS energy-management actuator layer."""

from __future__ import annotations
from collections import defaultdict, deque
import json
import time
from typing import Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.ems import EMSControlError, get_ems_manager
from .views import local_or_trusted_cloud_only


def _without_dev_why(hass: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove the EMS explanation block unless DEV mode is active."""
    from ..core.dev_mode import is_dev_active

    if is_dev_active(hass) or not isinstance(payload, dict):
        return payload
    filtered_payload = dict(payload)
    intelligence = filtered_payload.get("intelligence")
    if isinstance(intelligence, dict):
        intelligence = dict(intelligence)
        intelligence["why"] = []
        intelligence.pop("headline", None)
        filtered_payload["intelligence"] = intelligence
    plan = filtered_payload.get("plan")
    if isinstance(plan, list):
        filtered_plan = []
        for step in plan:
            if isinstance(step, dict):
                step = dict(step)
                step.pop("why", None)
            filtered_plan.append(step)
        filtered_payload["plan"] = filtered_plan
    primary = filtered_payload.get("primary")
    if isinstance(primary, dict):
        primary = dict(primary)
        primary.pop("why", None)
        filtered_payload["primary"] = primary
    return filtered_payload


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


class EMSView(HomeAssistantView):
    """Shared authorization, origin, body and rate-limit boundary."""

    requires_auth = True
    cors_allowed = False
    _rate_windows: dict[str, deque[float]] = defaultdict(deque)

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @staticmethod
    def _user(request: web.Request) -> Any:
        return request.get("hass_user")

    def _authorize(self, request: web.Request) -> web.Response | None:
        user = self._user(request)
        if user is None or not bool(getattr(user, "is_admin", False)):
            return _error("admin_required", "Administrator access is required.", 403)
        if get_ems_manager(self.hass) is None:
            return _error("ems_unavailable", "EMS runtime is not available.", 503)

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise EMSControlError(
                "invalid_content_type", "Content-Type must be application/json."
            )
        if request.content_length is not None and request.content_length > 4096:
            raise EMSControlError("body_too_large", "Request body exceeds 4 KiB.")
        try:
            raw_body = await request.read()
            if len(raw_body) > 4096:
                raise EMSControlError("body_too_large", "Request body exceeds 4 KiB.")
            payload = json.loads(
                raw_body.decode("utf-8"), parse_float=str, parse_int=str
            )
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            raise EMSControlError(
                "invalid_json", "A JSON object is required."
            ) from error
        if not isinstance(payload, dict):
            raise EMSControlError("invalid_json", "A JSON object is required.")
        return payload

    def _actor(self, request: web.Request) -> str:
        return str(getattr(self._user(request), "id", "unknown"))

    async def _run(self, request: web.Request, operation: Any) -> web.Response:
        blocked_response = self._authorize(request)
        if blocked_response is not None:
            return blocked_response
        if request.method == "POST":
            request_origin = f"{request.scheme}://{request.host}"
            if request.headers.get("Origin") != request_origin:
                return _error(
                    "same_origin_required", "Same-origin request required.", 403
                )
            actor = self._actor(request)
            now = time.monotonic()
            rate_window = self._rate_windows[actor]
            while rate_window and now - rate_window[0] > 60:
                rate_window.popleft()
            if len(rate_window) >= 20:
                return _error("rate_limit_exceeded", "Too many EMS requests.", 429)
            rate_window.append(now)
        try:
            result = await operation()
        except EMSControlError as error:
            conflict_codes = {
                "cycle_limit",
                "token_expired",
                "actor_cooldown",
                "token_replayed",
                "readback_failed",
                "decision_changed",
                "token_owner_mismatch",
                "actor_opt_in_required",
                "confirm_mode_required",
                "automatic_confirmation_required",
            }
            forbidden_codes = {"control_not_authorized"}
            status_code = (
                403
                if error.code in forbidden_codes
                else 409
                if error.code in conflict_codes
                else 400
            )
            return _error(error.code, str(error), status_code)
        return web.json_response(
            {"success": True, "data": result}, headers={"Cache-Control": "no-store"}
        )


class EMSSnapshotView(EMSView):
    url = "/api/sfml_stats/ems/snapshot"
    name = "api:sfml_stats:ems:snapshot"

    async def get(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            manager = get_ems_manager(self.hass)
            if manager is None:
                raise EMSControlError(
                    "ems_unavailable", "EMS runtime is not available."
                )
            snapshot = await manager.async_snapshot(self._actor(request))
            return _without_dev_why(self.hass, snapshot)

        return await self._run(request, operation)


class EMSModeView(EMSView):
    url = "/api/sfml_stats/ems/mode"
    name = "api:sfml_stats:ems:mode"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            if set(payload) - {"mode", "confirm_automatic"}:
                raise EMSControlError(
                    "unexpected_field", "Unexpected mode request field."
                )
            manager = get_ems_manager(self.hass)
            if manager is None:
                raise EMSControlError(
                    "ems_unavailable", "EMS runtime is not available."
                )
            return await manager.async_set_mode(
                payload.get("mode"),
                user_id=self._actor(request),
                confirm_automatic=payload.get("confirm_automatic") is True,
            )

        return await self._run(request, operation)


class EMSActorView(EMSView):
    url = "/api/sfml_stats/ems/actor"
    name = "api:sfml_stats:ems:actor"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            if set(payload) != {"actor_id", "enabled"} or not isinstance(
                payload.get("enabled"), bool
            ):
                raise EMSControlError(
                    "invalid_actor_request",
                    "Actor ID and boolean enabled are required.",
                )
            manager = get_ems_manager(self.hass)
            if manager is None:
                raise EMSControlError(
                    "ems_unavailable", "EMS runtime is not available."
                )
            return await manager.async_set_actor(
                payload.get("actor_id"),
                payload["enabled"],
                user_id=self._actor(request),
            )

        return await self._run(request, operation)


class EMSConfirmView(EMSView):
    url = "/api/sfml_stats/ems/confirm"
    name = "api:sfml_stats:ems:confirm"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            if set(payload) != {"token"}:
                raise EMSControlError(
                    "invalid_confirm_request",
                    "A single confirmation token is required.",
                )
            manager = get_ems_manager(self.hass)
            if manager is None:
                raise EMSControlError(
                    "ems_unavailable", "EMS runtime is not available."
                )
            return await manager.async_confirm(
                payload.get("token"), user_id=self._actor(request)
            )

        return await self._run(request, operation)


async def async_setup_ems_views(hass: Any) -> None:
    for view_class in (EMSSnapshotView, EMSModeView, EMSActorView, EMSConfirmView):
        hass.http.register_view(view_class(hass))
