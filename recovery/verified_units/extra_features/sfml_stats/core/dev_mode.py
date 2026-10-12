# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Read the DEV-mode state published by Solar Forecast ML. Never raises."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)
_SFML_DOMAIN = "solar_forecast_ml"
_INACTIVE = {
    "active": False,
    "expires_at": None,
    "install_hash": "",
    "status": "not_provided",
}


def _expiry_in_future(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 40:
        return False
    normalized_expiry = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        expiry_datetime = datetime.fromisoformat(normalized_expiry)
    except ValueError:
        return False
    if expiry_datetime.tzinfo is None:
        return False
    return expiry_datetime.astimezone(timezone.utc) > datetime.now(timezone.utc)


def dev_state(hass: Any) -> dict[str, Any]:
    """Return the published DEV-mode dict, or an inactive state."""
    try:
        hass_data = getattr(hass, "data", None)
        domain_data = hass_data.get(_SFML_DOMAIN) if hass_data is not None else None
        published_state = (
            domain_data.get("dev_mode") if isinstance(domain_data, dict) else None
        )
        if not isinstance(published_state, dict):
            return dict(_INACTIVE)
        expires_at = published_state.get("expires_at")
        expires_at = expires_at if isinstance(expires_at, str) and expires_at else None
        install_hash = published_state.get("install_hash")
        status = published_state.get("status")
        return (
            (
                {
                    "active": published_state.get("active") is True
                    and _expiry_in_future(expires_at),
                    "expires_at": expires_at,
                    "install_hash": (
                        install_hash if isinstance(install_hash, str) else ""
                    ),
                    "status": (
                        status if isinstance(status, str) and status else "not_provided"
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
    except Exception:
        return dict(_INACTIVE)


def is_dev_active(hass: Any) -> bool:
    """True only while the published expiry is still in the future."""
    state = dev_state(hass)
    return state.get("active") is True and _expiry_in_future(state.get("expires_at"))


def public_dev_state(
    hass: Any, *, include_install_hash: bool = False
) -> dict[str, Any]:
    """Banner payload. The installation id is opt-in for the settings page."""
    state = dev_state(hass)
    payload = {"active": state["active"], "expires_at": state["expires_at"]}
    if include_install_hash:
        payload["install_hash"] = state["install_hash"]
    return payload


def _dev_mode_api(hass: Any) -> dict[str, Any] | None:
    try:
        hass_data = getattr(hass, "data", None)
        domain_data = hass_data.get(_SFML_DOMAIN) if hass_data is not None else None
        dev_api = (
            domain_data.get("dev_mode_api") if isinstance(domain_data, dict) else None
        )
    except Exception:
        return
    return dev_api if isinstance(dev_api, dict) else None


async def forward_dev_token(hass: Any, token: str, *, clear: bool = False) -> str:
    """Ask SFML to store or clear the token. The token is never logged."""
    if not clear:
        if not (isinstance(token, str) and token.strip()):
            return "invalid"
    dev_api = _dev_mode_api(hass)
    if dev_api is None:
        return "failed"
    try:
        if clear:
            clear_token = dev_api.get("clear")
            if not callable(clear_token):
                return "failed"
            await clear_token()
            return "cleared"
        set_token = dev_api.get("set")
        if not callable(set_token):
            return "failed"
        status = await set_token(token)
    except Exception as error:
        _LOGGER.warning("DEV token service failed: %s", type(error).__name__)
        return "failed"
    if not isinstance(status, str) or not status:
        return "failed"
    return status


def json_dev_required():
    """403 body used by every DEV-only route."""
    from aiohttp import web as web

    return web.json_response(
        {"success": False, "error": {"code": "dev_mode_required"}, "data": {}},
        status=403,
        headers={"Cache-Control": "no-store"},
    )
