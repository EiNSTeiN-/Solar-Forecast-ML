# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""DEV-mode read route and the admin proxy to the SFML token services."""

from __future__ import annotations
import logging
from typing import Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.dev_mode import forward_dev_token, public_dev_state
from .common import local_only, local_or_trusted_cloud_only

_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}


def _admin_blocked(request: web.Request) -> web.Response | None:
    user = request.get("hass_user")
    if user is None:
        return web.json_response(
            {"success": False, "error": "unauthorized"}, status=401, headers=_NO_STORE
        )
    if not bool(getattr(user, "is_admin", False)):
        return web.json_response(
            {"success": False, "error": "admin_required"}, status=403, headers=_NO_STORE
        )


class DevModeView(HomeAssistantView):
    """GET /api/sfml_stats/dev_mode — active flag and expiry."""

    url = "/api/sfml_stats/dev_mode"
    name = "api:sfml_stats:dev_mode"
    requires_auth = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"success": True, "data": public_dev_state(self.hass)}, headers=_NO_STORE
        )


class DevModeInstallView(HomeAssistantView):
    """GET the installation id. Administrators only."""

    url = "/api/sfml_stats/dev_mode/install"
    name = "api:sfml_stats:dev_mode_install"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        blocked_response = _admin_blocked(request)
        if blocked_response is not None:
            return blocked_response
        dev_state = public_dev_state(self.hass, include_install_hash=True)
        return web.json_response(
            {
                "success": True,
                "data": {"install_hash": dev_state.get("install_hash") or ""},
            },
            headers=_NO_STORE,
        )


class DevTokenView(HomeAssistantView):
    """POST the token through to SFML. STATS does not store or log it."""

    url = "/api/sfml_stats/dev_token"
    name = "api:sfml_stats:dev_token"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        blocked_response = _admin_blocked(request)
        if blocked_response is not None:
            return blocked_response
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers=_NO_STORE,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers=_NO_STORE,
            )
        token_status = await forward_dev_token(
            self.hass, str(payload.get("token") or "")
        )
        if token_status != "valid":
            return web.json_response(
                {"success": False, "error": token_status}, status=400, headers=_NO_STORE
            )
        _LOGGER.info("DEV token forwarded to SFML")
        return web.json_response(
            {"success": True, "data": {"status": token_status}}, headers=_NO_STORE
        )


class DevTokenClearView(HomeAssistantView):
    """POST clears the stored token via the SFML service."""

    url = "/api/sfml_stats/dev_token/clear"
    name = "api:sfml_stats:dev_token_clear"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        blocked_response = _admin_blocked(request)
        if blocked_response is not None:
            return blocked_response
        token_status = await forward_dev_token(self.hass, "", clear=True)
        if token_status != "cleared":
            return web.json_response(
                {"success": False, "error": token_status}, status=400, headers=_NO_STORE
            )
        _LOGGER.info("DEV token clear forwarded to SFML")
        return web.json_response(
            {"success": True, "data": {"status": token_status}}, headers=_NO_STORE
        )
