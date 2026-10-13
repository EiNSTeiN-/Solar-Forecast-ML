# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_CORE_VERSION
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.system_info import async_get_system_info
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util
from ..const import VERSION
from ..core.dev_mode import is_dev_active
from ..core.support_report import (
    DEFAULT_WINDOW_HOURS,
    ENVIRONMENTS,
    FAMILY_DOMAINS,
    INSTALLATIONS,
    LANGUAGES,
    PROVIDER_DOMAINS,
    WINDOW_HOURS_CHOICES,
    IntegrationInfo,
    LogRecord,
    LogSelection,
    ReportContext,
    collect_log_sections,
    guess_environment,
    guess_installation,
    render_logs_field,
)
from ..core.support_submit import TRACKER_URL, SupportSubmitError, submit_report
from .views import local_or_trusted_cloud_only


class SupportSubmitView(SupportView):

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        _var_var_52 = f"{request.scheme}://{request.host}"
        if request.headers.get("Origin") != _var_var_52:
            return _error("same_origin_required", "Same-origin request required.", 403)
        try:
            _var_var_50 = await self._body(request)
            _var_var_45 = _validate_submission(_var_var_50)
        except SupportError as _var_var_5:
            return _error(
                _var_var_5.code, str(_var_var_5), _var_var_5.status, _var_var_5.field
            )
        _var_var_53 = type(self)
        _var_var_54 = time.monotonic()
        if _var_var_53._in_flight:
            return _error(
                "submission_in_progress", "A report is already being sent.", 409
            )
        if (
            _var_var_53._last_submit
            and _var_var_54 - _var_var_53._last_submit < SUBMIT_COOLDOWN_SECONDS
        ):
            return _error(
                "cooldown",
                "Please wait a few minutes before sending another report.",
                429,
            )
        _var_var_53._in_flight = True
        try:
            _var_var_1 = _window_hours(_var_var_50.get("hours"))
            _var_var_51 = await async_build_report(self.hass, _var_var_1)
            _var_var_36 = _var_var_51.get("_versions", {})
            _var_var_55 = {
                **_var_var_45,
                "componentVersion": _var_var_36.get(
                    PROVIDER_DOMAINS[_var_var_45["provider"]], "unknown"
                ),
                "haCoreVersion": _var_var_51["context"]["ha_core_version"],
                "haOsVersion": _var_var_51["context"]["host_os"] or "n/a",
                "logs": _var_var_51["logs"],
            }
            _var_var_56 = await submit_report(
                async_get_clientsession(self.hass), _var_var_55, VERSION
            )
        except SupportSubmitError as _var_var_5:
            _LOGGER.warning(
                "Bug report not accepted by the website: %s", _var_var_5.detail
            )
            _var_var_57 = {
                "rate_limited": 429,
                "too_large": 413,
                "offline": 502,
                "unavailable": 503,
            }.get(_var_var_5.code, 502)
            return _error(
                _var_var_5.code,
                _var_var_5.detail or _var_var_5.code,
                _var_var_57,
                _var_var_5.field,
            )
        except Exception as _var_var_5:
            _LOGGER.warning("Bug report failed: %s", _var_var_5, exc_info=True)
            return _error("report_failed", "The report could not be assembled.", 500)
        finally:
            _var_var_53._in_flight = False
        _var_var_53._last_submit = time.monotonic()
        _LOGGER.info(
            "Bug report %s submitted (%s characters of logs)",
            _var_var_56.reference_id,
            len(_var_var_51["logs"]),
        )
        return _ok(
            {
                "reference_id": _var_var_56.reference_id,
                "notified": _var_var_56.notified,
                "tracker_url": TRACKER_URL.format(language=_var_var_45["language"]),
                "characters": len(_var_var_51["logs"]),
            }
        )
