# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Isolated verified method; this class shell is not an integration module.
from __future__ import annotations
from aiohttp import web
from .views import local_only


class GPMRevertCorrectionView:
    @local_only
    async def post(self, request: web.Request) -> web.Response:
        blocked_response = _admin_forbidden(request)
        if blocked_response is not None:
            return blocked_response
        coordinator = _gpm_coordinator(self.hass)
        gate_response = revert_gate(coordinator)
        if gate_response is not None:
            return gate_response
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            correction_id = int(payload["correction_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            result = await coordinator.async_revert_correction(correction_id)
        except Exception as error:
            reason = getattr(error, "key", None) or "request_failed"
            error_key = str(reason)
            status_code = (
                403
                if error_key in {"demo_forbidden", "correction_license_required"}
                else 400
            )
            return web.json_response(
                {"success": False, "error": error_key},
                status=status_code,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"success": True, **(result if isinstance(result, dict) else {})},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
