# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Authenticated API behind the "Fehler melden" page.

``GET  /api/sfml_stats/support/preview`` shows exactly what would leave the
installation; ``POST /api/sfml_stats/support/report`` rebuilds that bundle and
hands it to the website intake. Logs are never accepted from the browser. @zara
"""
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

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
BODY_LIMIT_BYTES = 16384
SUBMIT_COOLDOWN_SECONDS = 300
SFML_LOG_RELATIVE = ("solar_forecast_ml", "logs", "solar_forecast_ml.log")
HA_LOG_NAME = "home-assistant.log"
TEXT_RULES: dict[str, tuple[int, int]] = {
    "title": (5, 180),
    "expected": (10, 2000),
    "actual": (10, 2000),
    "steps": (10, 5000),
}


class SupportError(Exception):

    def __init__(
        self, code: str, message: str, status: int = 400, field: str = ""
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.field = field


def _error(code: str, message: str, status: int, field: str = "") -> web.Response:
    error_detail = {"code": code, "message": message}
    if field:
        error_detail["field"] = field
    return web.json_response(
        {"success": False, "error": error_detail},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _ok(data: dict[str, Any]) -> web.Response:
    return web.json_response(
        {"success": True, "data": data}, headers={"Cache-Control": "no-store"}
    )


def _window_hours(value: Any) -> int:
    try:
        window_hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_HOURS
    return (
        window_hours if window_hours in WINDOW_HOURS_CHOICES else DEFAULT_WINDOW_HOURS
    )


async def _collect_integrations(hass: HomeAssistant) -> list[IntegrationInfo]:
    integrations = []
    for domain in FAMILY_DOMAINS:
        try:
            integration = await async_get_integration(hass, domain)
        except IntegrationNotFound:
            continue
        except Exception as error:
            integrations.append(
                IntegrationInfo(domain, "?", f"load_error: {type(error).__name__}")
            )
            continue
        version = str(integration.version) if integration.version else "?"
        config_entries = hass.config_entries.async_entries(domain)
        if config_entries:
            entry_states = []
            for config_entry in config_entries:
                entry_state = getattr(
                    config_entry.state, "value", str(config_entry.state)
                )
                entry_reason = getattr(config_entry, "reason", None)
                if entry_reason and entry_state != "loaded":
                    entry_state = f"{entry_state}: {str(entry_reason)[:80]}"
                entry_states.append(entry_state)
            integration_state = ", ".join(entry_states)
        else:
            integration_state = (
                "loaded" if domain in hass.config.components else "not_configured"
            )
        integrations.append(IntegrationInfo(domain, version, integration_state))
        continue
    return integrations


def _system_log_records(hass: HomeAssistant) -> list[LogRecord]:
    """Fallback when home-assistant.log is not readable: the in-memory system_log store."""
    system_log_store = hass.data.get("system_log")
    stored_records = getattr(system_log_store, "records", None)
    if stored_records is None:
        return []
    log_records = []
    try:
        records = list(stored_records.values())
    except Exception:
        return []
    for record in records:
        try:
            timestamp = dt_util.as_local(
                dt_util.utc_from_timestamp(float(record.timestamp))
            ).replace(tzinfo=None)
            level = str(record.level).upper()
            logger_name = str(record.name)
            messages = list(record.message) if record.message else [""]
            header_line = f"{timestamp:%Y-%m-%d %H:%M:%S}.000 {level} (system_log) [{logger_name}] {messages[-1]}"
            record_lines = [header_line]
            if getattr(record, "count", 1) > 1:
                record_lines.append(f"  (occurred {record.count} times)")
            if record.exception:
                record_lines.extend(str(record.exception).rstrip("\n").split("\n"))
            log_records.append(LogRecord(timestamp, level, logger_name, record_lines))
            continue
        except Exception:
            pass
    log_records.sort(key=lambda record: record.timestamp)
    return log_records


async def async_build_report(hass: HomeAssistant, hours: int) -> dict[str, Any]:
    """Collect context and logs; returns the logs field plus everything the page shows."""
    now = dt_util.now()
    window_until = now.replace(tzinfo=None, microsecond=0)
    window_since = window_until - timedelta(hours=hours)
    system_info = {}
    try:
        system_info = await async_get_system_info(hass)
    except Exception as error:
        _LOGGER.debug("System info unavailable for bug report: %s", error)
    integrations = await _collect_integrations(hass)
    report_context = ReportContext(
        client_version=VERSION,
        created_at=window_until,
        timezone=str(hass.config.time_zone or ""),
        ha_core_version=str(system_info.get("version") or HA_CORE_VERSION),
        host_os=str(system_info.get("host_os") or ""),
        installation_type=str(system_info.get("installation_type") or ""),
        python_version=str(system_info.get("python_version") or ""),
        arch=str(system_info.get("arch") or ""),
        ha_logger_level=logging.getLevelName(
            logging.getLogger("custom_components").getEffectiveLevel()
        ),
        integrations=integrations,
        window_since=window_since,
        window_until=window_until,
        window_hours=hours,
        dev_mode_active=is_dev_active(hass),
    )
    sfml_log_path = Path(hass.config.path(*SFML_LOG_RELATIVE))
    ha_log_path = Path(hass.config.path(HA_LOG_NAME))
    fallback_log_records = (
        _system_log_records(hass) if not ha_log_path.is_file() else []
    )
    ha_log_selection, sfml_log_selection, redactions = (
        await hass.async_add_executor_job(
            collect_log_sections,
            sfml_log_path,
            ha_log_path,
            window_since,
            window_until,
            fallback_log_records,
        )
    )
    logs_text = render_logs_field(
        report_context, ha_log_selection, sfml_log_selection, redactions
    )
    versions_by_domain = {
        integration.domain: integration.version for integration in integrations
    }
    providers = [
        provider_code
        for provider_code, domain in PROVIDER_DOMAINS.items()
        if domain in versions_by_domain
    ]
    for provider_code in ("sfml", "stats"):
        if provider_code not in providers:
            providers.append(provider_code)
    return {
        "context": {
            "client_version": VERSION,
            "ha_core_version": report_context.ha_core_version,
            "host_os": report_context.host_os,
            "installation_type": report_context.installation_type,
            "python_version": report_context.python_version,
            "arch": report_context.arch,
            "timezone": report_context.timezone,
            "ha_logger_level": report_context.ha_logger_level,
            "window_hours": hours,
            "window_since": window_since.isoformat(sep=" "),
            "window_until": window_until.isoformat(sep=" "),
            "integrations": [
                {
                    "domain": integration.domain,
                    "version": integration.version,
                    "state": integration.state,
                    "provider": next(
                        (
                            provider_code
                            for provider_code, provider_domain in PROVIDER_DOMAINS.items()
                            if provider_domain == integration.domain
                        ),
                        None,
                    ),
                }
                for integration in integrations
            ],
            "providers": providers,
            "environment_guess": guess_environment(
                report_context.arch, report_context.installation_type
            ),
            "installation_guess": guess_installation(hass.config.components),
            "tracker_urls": {
                language: TRACKER_URL.format(language=language)
                for language in LANGUAGES
            },
        },
        "logs": logs_text,
        "stats": {
            "characters": len(logs_text),
            "ha": _selection_stats(ha_log_selection),
            "sfml": _selection_stats(sfml_log_selection),
            "redactions": redactions,
        },
        "_versions": versions_by_domain,
    }


def _selection_stats(selection: LogSelection) -> dict[str, Any]:
    return {
        "available": bool(selection.source),
        "source": selection.source,
        "total": selection.total,
        "kept": selection.kept,
        "kept_counts": selection.kept_counts,
        "total_counts": selection.total_counts,
        "dropped_before": {
            level: dropped_before.isoformat(sep=" ")
            for level, dropped_before in selection.dropped_before.items()
        },
        "scan_exhausted": selection.scan_exhausted,
    }


def _validate_submission(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the user-supplied part of the website payload or raise SupportError."""
    if payload.get("privacy") is not True:
        raise SupportError(
            "confirmation_required",
            "The confirmation checkbox is required.",
            422,
            "privacy",
        )
    provider = payload.get("provider")
    if provider not in PROVIDER_DOMAINS:
        raise SupportError("invalid_field", "Unknown product.", 422, "provider")
    environment = payload.get("environment")
    if environment not in ENVIRONMENTS:
        raise SupportError("invalid_field", "Unknown environment.", 422, "environment")
    installation = payload.get("installation")
    if installation not in INSTALLATIONS:
        raise SupportError(
            "invalid_field", "Unknown installation type.", 422, "installation"
        )
    language = payload.get("language")
    if language not in LANGUAGES:
        language = "en"
    validated_submission = {
        "provider": provider,
        "environment": environment,
        "installation": installation,
        "language": language,
        "privacy": True,
    }
    for field, (min_length, max_length) in TEXT_RULES.items():
        text = payload.get(field)
        if not isinstance(text, str):
            raise SupportError(
                "invalid_field", f"Field {field} is required.", 422, field
            )
        text = text.strip()
        if not min_length <= len(text) <= max_length:
            raise SupportError(
                "invalid_field",
                f"Field {field} must be {min_length} to {max_length} characters.",
                422,
                field,
            )
        validated_submission[field] = text
    return validated_submission


class SupportView(HomeAssistantView):
    """Shared behaviour: signed-in user, JSON body limits, no caching."""

    requires_auth = True
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise SupportError(
                "invalid_content_type", "Content-Type must be application/json."
            )
        if (
            request.content_length is not None
            and request.content_length > BODY_LIMIT_BYTES
        ):
            raise SupportError("body_too_large", "Request body exceeds 16 KiB.", 413)
        body_bytes = await request.read()
        if len(body_bytes) > BODY_LIMIT_BYTES:
            raise SupportError("body_too_large", "Request body exceeds 16 KiB.", 413)
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise SupportError("invalid_json", "A JSON object is required.") from error
        if not isinstance(payload, dict):
            raise SupportError("invalid_json", "A JSON object is required.")
        return payload


class SupportPreviewView(SupportView):
    url = "/api/sfml_stats/support/preview"
    name = "api:sfml_stats:support_preview"

    @local_or_trusted_cloud_only
    async def get(self, request: web.Request) -> web.Response:
        window_hours = _window_hours(request.query.get("hours"))
        try:
            report = await async_build_report(self.hass, window_hours)
        except Exception as error:
            _LOGGER.warning("Bug report preview failed: %s", error, exc_info=True)
            return _error("preview_failed", "The report could not be assembled.", 500)
        report.pop("_versions", None)
        return _ok(report)


class SupportSubmitView(SupportView):
    url = "/api/sfml_stats/support/report"
    name = "api:sfml_stats:support_report"
    _last_submit: float = 0.0
    _in_flight: bool = False

    @local_or_trusted_cloud_only
    async def post(self, request: web.Request) -> web.Response:
        expected_origin = f"{request.scheme}://{request.host}"
        if request.headers.get("Origin") != expected_origin:
            return _error("same_origin_required", "Same-origin request required.", 403)
        try:
            payload = await self._body(request)
            validated_submission = _validate_submission(payload)
        except SupportError as error:
            return _error(error.code, str(error), error.status, error.field)
        view_class = type(self)
        checked_at = time.monotonic()
        if view_class._in_flight:
            return _error(
                "submission_in_progress", "A report is already being sent.", 409
            )
        if (
            view_class._last_submit
            and checked_at - view_class._last_submit < SUBMIT_COOLDOWN_SECONDS
        ):
            return _error(
                "cooldown",
                "Please wait a few minutes before sending another report.",
                429,
            )
        view_class._in_flight = True
        try:
            window_hours = _window_hours(payload.get("hours"))
            report = await async_build_report(self.hass, window_hours)
            versions_by_domain = report.get("_versions", {})
            submission_payload = {
                **validated_submission,
                "componentVersion": versions_by_domain.get(
                    PROVIDER_DOMAINS[validated_submission["provider"]], "unknown"
                ),
                "haCoreVersion": report["context"]["ha_core_version"],
                "haOsVersion": report["context"]["host_os"] or "n/a",
                "logs": report["logs"],
            }
            submission_result = await submit_report(
                async_get_clientsession(self.hass), submission_payload, VERSION
            )
        except SupportSubmitError as error:
            _LOGGER.warning("Bug report not accepted by the website: %s", error.detail)
            error_status = {
                "rate_limited": 429,
                "too_large": 413,
                "offline": 502,
                "unavailable": 503,
            }.get(error.code, 502)
            return _error(
                error.code, error.detail or error.code, error_status, error.field
            )
        except Exception as error:
            _LOGGER.warning("Bug report failed: %s", error, exc_info=True)
            return _error("report_failed", "The report could not be assembled.", 500)
        finally:
            view_class._in_flight = False
        view_class._last_submit = time.monotonic()
        _LOGGER.info(
            "Bug report %s submitted (%s characters of logs)",
            submission_result.reference_id,
            len(report["logs"]),
        )
        return _ok(
            {
                "reference_id": submission_result.reference_id,
                "notified": submission_result.notified,
                "tracker_url": TRACKER_URL.format(
                    language=validated_submission["language"]
                ),
                "characters": len(report["logs"]),
            }
        )


async def async_setup_support_views(hass: HomeAssistant) -> None:
    hass.http.register_view(SupportPreviewView(hass))
    hass.http.register_view(SupportSubmitView(hass))
