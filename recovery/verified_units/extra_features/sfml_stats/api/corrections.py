# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Authenticated admin API for premium energy corrections."""

from __future__ import annotations
import json
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..const import DOMAIN
from ..core.corrections import (
    CSV_MAX_BYTES,
    CSV_TEMPLATE,
    DEFAULT_EVIDENCE_TYPE,
    DEFAULT_REASON_CODE,
    CorrectionError,
    EnergyCorrectionStore,
)
from ..utils.time_utils import ha_today
from .views import local_or_trusted_cloud_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _premium_enabled(hass: HomeAssistant) -> bool:
    eai_data = hass.data.get("solar_forecast_eai")
    if not isinstance(eai_data, dict):
        return False
    capability_provider = eai_data.get("capability_provider")
    if capability_provider is None or not callable(
        getattr(capability_provider, "snapshot", None)
    ):
        return False
    try:
        snapshot = capability_provider.snapshot()
    except Exception:
        return False
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("provider_version") == 8
        and (snapshot.get("license_status") == "valid")
        and (snapshot.get("premium_full_package") is True)
    )


def _user(request: web.Request) -> Any:
    return request.get("hass_user")


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


class CorrectionsView(HomeAssistantView):
    """Shared authenticated correction view."""

    requires_auth = True
    cors_allowed = False
    _rate_windows: dict[str, deque[float]] = defaultdict(deque)

    def __init__(self, hass: HomeAssistant, store: EnergyCorrectionStore) -> None:
        self.hass = hass
        self.store = store

    def _authorize(self, request: web.Request) -> web.Response | None:
        user = _user(request)
        if user is None or not bool(getattr(user, "is_admin", False)):
            return _error("admin_required", "Administrator access is required.", 403)
        if not _premium_enabled(self.hass):
            return _error(
                "premium_required", "A valid Premium Full Package is required.", 403
            )

    @staticmethod
    def _actor(request: web.Request) -> str:
        return str(getattr(_user(request), "id", "unknown"))

    def _invalidate_consumers(self) -> None:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict):
                billing_calculator = entry_data.get("billing_calculator")
                if billing_calculator is not None and hasattr(
                    billing_calculator, "invalidate_cache"
                ):
                    billing_calculator.invalidate_cache()

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise CorrectionError(
                "invalid_content_type", "Content-Type must be application/json."
            )
        if request.content_length is not None and request.content_length > 8192:
            raise CorrectionError("body_too_large", "Request body exceeds 8 KiB.")
        try:
            raw_body = await request.read()
            if len(raw_body) > 8192:
                raise CorrectionError("body_too_large", "Request body exceeds 8 KiB.")
            payload = json.loads(
                raw_body.decode("utf-8"), parse_float=str, parse_int=str
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise CorrectionError(
                "invalid_json", "A JSON object is required."
            ) from error
        if not isinstance(payload, dict):
            raise CorrectionError("invalid_json", "A JSON object is required.")
        return payload

    async def _csv_upload(self, request: web.Request) -> bytes:
        if (
            request.content_length is not None
            and request.content_length > CSV_MAX_BYTES
        ):
            raise CorrectionError("csv_too_large", "CSV exceeds 5 MB.")
        raw_body = await request.read()
        if len(raw_body) > CSV_MAX_BYTES:
            raise CorrectionError("csv_too_large", "CSV exceeds 5 MB.")
        return raw_body

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
                return _error(
                    "rate_limit_exceeded", "Too many correction requests.", 429
                )
            rate_window.append(now)
        try:
            result = await operation()
        except CorrectionError as error:
            status_code = (
                409
                if error.code
                in {
                    "preview_expired",
                    "batch_not_undoable",
                    "event_not_undoable",
                    "correction_conflict",
                    "idempotency_conflict",
                    "invalid_preview_token",
                    "second_confirmation_required",
                }
                else 400
            )
            return _error(error.code, str(error), status_code)
        return web.json_response(
            {"success": True, "data": result}, headers={"Cache-Control": "no-store"}
        )


class CorrectionsStatusView(CorrectionsView):
    url = "/api/sfml_stats/corrections/status"
    name = "api:sfml_stats:corrections:status"

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            result = await self.store.async_status()
            result["latest_completed_date"] = (
                ha_today(self.hass) - timedelta(days=1)
            ).isoformat()
            return result

        return await self._run(request, operation)


class CorrectionsContextView(CorrectionsView):
    url = "/api/sfml_stats/corrections/context"
    name = "api:sfml_stats:corrections:context"

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        return await self._run(
            request,
            lambda: self.store.async_context(
                request.query.get("target_date"),
                request.query.get("metric"),
                ha_today(self.hass),
                request.query.get("mode") or "correct",
            ),
        )


class CorrectionsHistoryView(CorrectionsView):
    url = "/api/sfml_stats/corrections/history"
    name = "api:sfml_stats:corrections:history"

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError:
            return _error("invalid_limit", "History limit must be an integer.", 400)
        return await self._run(request, lambda: self.store.async_history(limit))


class CorrectionsPreviewView(CorrectionsView):
    url = "/api/sfml_stats/corrections/preview"
    name = "api:sfml_stats:corrections:preview"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            target_date = payload.get("target_date")
            metric = payload.get("metric")
            mode = payload.get("mode") or "correct"
            if mode == "backfill":
                reason_code = "historical_backfill"
                evidence_type = payload.get("evidence_type")
            else:
                reason_code = DEFAULT_REASON_CODE
                evidence_type = DEFAULT_EVIDENCE_TYPE
            return await self.store.async_preview(
                target_date,
                metric,
                payload.get("target_value_kwh"),
                reason_code,
                evidence_type,
                f"ui:{target_date}:{metric}",
                payload.get("reason_note"),
                payload.get("idempotency_key"),
                self._actor(request),
                ha_today(self.hass),
                mode,
            )

        return await self._run(request, operation)


class CorrectionsCommitView(CorrectionsView):
    url = "/api/sfml_stats/corrections/commit"
    name = "api:sfml_stats:corrections:commit"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            result = await self.store.async_commit(
                payload.get("preview_token"),
                payload.get("idempotency_key"),
                payload.get("confirmed_large_change"),
                self._actor(request),
            )
            self._invalidate_consumers()
            return result

        return await self._run(request, operation)


class CorrectionsUndoView(CorrectionsView):
    url = "/api/sfml_stats/corrections/undo"
    name = "api:sfml_stats:corrections:undo"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            result = await self.store.async_undo(
                payload.get("event_id"),
                payload.get("idempotency_key"),
                self._actor(request),
            )
            self._invalidate_consumers()
            return result

        return await self._run(request, operation)


class CorrectionsRangePreviewView(CorrectionsView):
    url = "/api/sfml_stats/corrections/range/preview"
    name = "api:sfml_stats:corrections:range:preview"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            return await self.store.async_preview_range(
                payload.get("metric"),
                payload.get("start_date"),
                payload.get("end_date"),
                payload.get("target_sum_kwh"),
                payload.get("weighting"),
                payload.get("idempotency_key"),
                self._actor(request),
                ha_today(self.hass),
            )

        return await self._run(request, operation)


class CorrectionsRangeCommitView(CorrectionsView):
    url = "/api/sfml_stats/corrections/range/commit"
    name = "api:sfml_stats:corrections:range:commit"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            result = await self.store.async_commit_range(
                payload.get("preview_token"),
                payload.get("idempotency_key"),
                payload.get("confirmed_large_change"),
                self._actor(request),
            )
            self._invalidate_consumers()
            return result

        return await self._run(request, operation)


class CorrectionsCsvTemplateView(CorrectionsView):
    url = "/api/sfml_stats/corrections/csv/template"
    name = "api:sfml_stats:corrections:csv:template"

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        blocked_response = self._authorize(request)
        if blocked_response is not None:
            return blocked_response
        return web.Response(
            body=CSV_TEMPLATE,
            content_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="sfml_stats_corrections.csv"',
                "Cache-Control": "no-store",
            },
        )


class CorrectionsCsvPreviewView(CorrectionsView):
    url = "/api/sfml_stats/corrections/csv/preview"
    name = "api:sfml_stats:corrections:csv:preview"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            raw_body = await self._csv_upload(request)
            return await self.store.async_preview_csv(
                raw_body,
                request.query.get("idempotency_key"),
                self._actor(request),
                ha_today(self.hass),
            )

        return await self._run(request, operation)


class CorrectionsCsvCommitView(CorrectionsView):
    url = "/api/sfml_stats/corrections/csv/commit"
    name = "api:sfml_stats:corrections:csv:commit"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            result = await self.store.async_commit_csv(
                payload.get("preview_token"),
                payload.get("idempotency_key"),
                payload.get("confirmed_large_change"),
                self._actor(request),
            )
            self._invalidate_consumers()
            return result

        return await self._run(request, operation)


class CorrectionsUndoBatchView(CorrectionsView):
    url = "/api/sfml_stats/corrections/undo_batch"
    name = "api:sfml_stats:corrections:undo_batch"

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        async def operation() -> dict[str, Any]:
            payload = await self._body(request)
            result = await self.store.async_undo_batch(
                payload.get("batch_id"),
                payload.get("idempotency_key"),
                self._actor(request),
            )
            self._invalidate_consumers()
            return result

        return await self._run(request, operation)


async def async_setup_correction_views(hass: HomeAssistant) -> None:
    config_path = Path(hass.config.path())
    store = EnergyCorrectionStore(
        config_path / "sfml_stats" / "corrections.db",
        config_path / "solar_forecast_ml" / "solar_forecast.db",
        hass.config.time_zone,
    )
    await store.async_initialize()
    hass.data.setdefault(DOMAIN, {})["correction_store"] = store
    for view_class in (
        CorrectionsStatusView,
        CorrectionsContextView,
        CorrectionsHistoryView,
        CorrectionsPreviewView,
        CorrectionsCommitView,
        CorrectionsUndoView,
        CorrectionsRangePreviewView,
        CorrectionsRangeCommitView,
        CorrectionsCsvTemplateView,
        CorrectionsCsvPreviewView,
        CorrectionsCsvCommitView,
        CorrectionsUndoBatchView,
    ):
        hass.http.register_view(view_class(hass, store))
