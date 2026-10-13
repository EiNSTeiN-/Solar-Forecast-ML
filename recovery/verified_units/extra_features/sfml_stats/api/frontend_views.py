# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Frontend shell and static asset views."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.util import dt as dt_util
from . import context
from .common import (
    APIContext,
    FRONTEND_IMMUTABLE_HEADERS,
    FRONTEND_NO_STORE_HEADERS,
    FRONTEND_RELEASE_MANIFEST,
    GPM_PRICE_FRESHNESS_MINUTES,
    _active_frontend_asset_by_path,
    _active_frontend_file,
    _frontend_path_without_symlinks,
    _get_db,
    _get_local_naive_range_for_local_date,
    _load_active_frontend_release,
    _safe_frontend_relative_path,
    _verified_frontend_payload,
    local_only,
    local_or_cloud_shell_only,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today
from ..const import DOMAIN, VERSION

if TYPE_CHECKING:
    from aiohttp.web import Request, Response
_LOGGER = logging.getLogger(__name__)


def _parse_local_datetime(value: Any) -> datetime | None:
    if value is None:
        return
    try:
        parsed_datetime = dt_util.parse_datetime(str(value))
    except (TypeError, ValueError):
        return
    if parsed_datetime is None:
        return
    if parsed_datetime.tzinfo is None:
        local_timezone = (
            dt_util.get_time_zone(context.HASS.config.time_zone)
            if context.HASS
            else None
        )
        parsed_datetime = parsed_datetime.replace(tzinfo=local_timezone or timezone.utc)
    return as_ha_local(parsed_datetime)


async def _read_json_file(path: Path | None) -> dict | None:
    """Read a JSON file asynchronously. @zara"""
    if path is None:
        _LOGGER.warning("Path is None - was async_setup_views called?")
        return
    if not path.exists():
        _LOGGER.debug("File not found: %s", path)
        return
    try:
        import aiofiles as aiofiles

        async with aiofiles.open(path, "r", encoding="utf-8") as json_file:
            json_text = await json_file.read()
            json_payload = json.loads(json_text)
            _LOGGER.debug("Successfully loaded: %s (%d bytes)", path, len(json_text))
            return json_payload
    except Exception as read_error:
        _LOGGER.error("Error reading %s: %s", path, read_error)
        return
    return None if True else None


class HealthCheckView(HomeAssistantView):
    """Health check endpoint for monitoring. @zara"""

    url = "/api/sfml_stats/health"
    name = "api:sfml_stats:health"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return health status. @zara"""
        try:
            api_context = APIContext.get()
            checks = {
                "solar_data_available": api_context.solar_path.exists(),
                "grid_data_available": api_context.grid_path.exists(),
                "integration_loaded": DOMAIN in api_context.hass.data,
                "config_entries_present": len(
                    api_context.hass.config_entries.async_entries(DOMAIN)
                )
                > 0,
            }
            if checks["solar_data_available"]:
                checks["solar_stats_available"] = (
                    api_context.solar_path / "solar_forecast.db"
                ).exists()
            else:
                checks["solar_stats_available"] = False
            if checks["grid_data_available"]:
                grid_price_health = await self._get_grid_price_health()
                checks["grid_prices_available"] = grid_price_health["available"]
                checks["grid_prices_fresh"] = grid_price_health["fresh"]
                checks["grid_prices_source"] = grid_price_health["source"]
                checks["grid_prices_last_updated"] = grid_price_health["last_updated"]
                checks["grid_prices_age_minutes"] = grid_price_health["age_minutes"]
            else:
                checks["grid_prices_available"] = False
                checks["grid_prices_fresh"] = False
                checks["grid_prices_source"] = None
                checks["grid_prices_last_updated"] = None
                checks["grid_prices_age_minutes"] = None
            required_checks = [
                checks["integration_loaded"],
                checks["config_entries_present"],
            ]
            boolean_checks = [
                check_value
                for check_value in checks.values()
                if isinstance(check_value, bool)
            ]
            required_ready = all(required_checks)
            degraded = not all(boolean_checks) and required_ready
            if required_ready and (not degraded):
                health_status = "healthy"
                http_status = 200
            elif degraded:
                health_status = "degraded"
                http_status = 200
            else:
                health_status = "unhealthy"
                http_status = 503
            return web.json_response(
                {
                    "status": health_status,
                    "version": VERSION,
                    "checks": checks,
                    "timestamp": ha_now(context.HASS).isoformat(),
                },
                status=http_status,
            )
        except RuntimeError:
            return web.json_response(
                {
                    "status": "unhealthy",
                    "version": VERSION,
                    "error": "API context not initialized",
                    "timestamp": ha_now(context.HASS).isoformat(),
                },
                status=503,
            )
        except Exception as health_error:
            _LOGGER.error("Health check error: %s", health_error)
            return web.json_response(
                {
                    "status": "error",
                    "version": VERSION,
                    "error": "Internal server error",
                    "timestamp": ha_now(context.HASS).isoformat(),
                },
                status=500,
            )

    async def _get_grid_price_health(self) -> dict[str, Any]:
        price_health = {
            "available": False,
            "fresh": False,
            "source": None,
            "last_updated": None,
            "age_minutes": None,
        }
        now = ha_now(context.HASS)
        try:
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT total_price, last_updated\n                       FROM GPM_current_price\n                       WHERE id = 1"
                ) as cursor:
                    price_row = await cursor.fetchone()
                if price_row:
                    if price_row[0] is not None:
                        price_health["available"] = True
                        price_health["source"] = "GPM_current_price"
                        price_health["last_updated"] = price_row[1]
                        last_updated = _parse_local_datetime(price_row[1])
                        if last_updated is not None:
                            age_minutes = max(
                                0.0, (now - last_updated).total_seconds() / 60.0
                            )
                            price_health["age_minutes"] = round(age_minutes, 1)
                            price_health["fresh"] = (
                                age_minutes <= GPM_PRICE_FRESHNESS_MINUTES
                            )
                        return price_health
                today_iso = ha_today(context.HASS).isoformat()
                day_start, day_end = _get_local_naive_range_for_local_date(today_iso)
                async with connection.execute(
                    "SELECT MAX(timestamp)\n                       FROM GPM_price_history\n                       WHERE timestamp >= ? AND timestamp < ?\n                         AND total_price IS NOT NULL",
                    (day_start, day_end),
                ) as cursor:
                    price_row = await cursor.fetchone()
                if price_row and price_row[0]:
                    price_health["available"] = True
                    price_health["source"] = "GPM_price_history"
                    price_health["last_updated"] = price_row[0]
                    last_updated = _parse_local_datetime(price_row[0])
                    if last_updated is not None:
                        age_minutes = max(
                            0.0, (now - last_updated).total_seconds() / 60.0
                        )
                        price_health["age_minutes"] = round(age_minutes, 1)
                        price_health["fresh"] = (
                            age_minutes <= GPM_PRICE_FRESHNESS_MINUTES
                        )
        except Exception as health_error:
            _LOGGER.debug("GPM price health check failed: %s", health_error)
        return price_health if True else None


class DashboardView(HomeAssistantView):
    """Main view serving the Vue.js app. @zara"""

    url = "/api/sfml_stats/dashboard"
    name = "api:sfml_stats:dashboard"
    requires_auth = False
    cors_allowed = True

    @local_or_cloud_shell_only
    async def get(self, request: Request) -> Response:
        """Return the dashboard HTML page. @zara"""
        active_release = None
        release_id = None
        if context.HASS is not None:
            active_release = await context.HASS.async_add_executor_job(
                _load_active_frontend_release, Path(context.HASS.config.path())
            )
        entry_relative_path = "modern/index.html"
        html_text = None
        if active_release is not None:
            release_entry = await context.HASS.async_add_executor_job(
                _active_frontend_file, active_release, "entries", entry_relative_path
            )
            if release_entry is not None:
                import aiofiles as aiofiles

                entry_path, entry_metadata = release_entry
                try:
                    async with aiofiles.open(entry_path, "rb") as frontend_file:
                        payload = await frontend_file.read()
                except OSError:
                    payload = b""
                payload_verified = await context.HASS.async_add_executor_job(
                    _verified_frontend_payload, entry_path, entry_metadata, payload
                )
                if payload_verified:
                    try:
                        html_text = payload.decode("utf-8")
                        release_id = active_release["release_id"]
                    except UnicodeDecodeError:
                        html_text = None
        if html_text is None:
            if context.HASS is not None:
                fallback_path = (
                    Path(context.HASS.config.path())
                    / "custom_components"
                    / "sfml_stats"
                    / "frontend"
                    / entry_relative_path
                )
            else:
                fallback_path = (
                    Path(__file__).parent.parent / "frontend" / entry_relative_path
                )
            fallback_exists = (
                await context.HASS.async_add_executor_job(fallback_path.exists)
                if context.HASS is not None
                else fallback_path.exists()
            )
            if not fallback_exists:
                html_text = self._get_fallback_html()
            else:
                import aiofiles as aiofiles

                async with aiofiles.open(
                    fallback_path, "r", encoding="utf-8"
                ) as frontend_file:
                    html_text = await frontend_file.read()
        return (
            web.Response(
                text=html_text,
                content_type="text/html",
                headers={
                    "X-Frame-Options": "SAMEORIGIN",
                    "Content-Security-Policy": "frame-ancestors 'self'",
                    **FRONTEND_NO_STORE_HEADERS,
                    **(
                        {"X-SFML-Frontend-Release": release_id}
                        if release_id is not None
                        else {}
                    ),
                },
            )
            if True
            else None
        )

    def _get_fallback_html(self) -> str:
        """Return fallback HTML when build is not present. @zara"""
        return '<!DOCTYPE html>\n<html>\n<head>\n    <title>SFML Stats Dashboard</title>\n    <style>\n        body {\n            background: #0a0a1a;\n            color: #fff;\n            font-family: system-ui;\n            display: flex;\n            justify-content: center;\n            align-items: center;\n            height: 100vh;\n            margin: 0;\n        }\n        .message { text-align: center; }\n        h1 { color: #00ffff; }\n    </style>\n</head>\n<body>\n    <div class="message">\n        <h1>SFML Stats Dashboard</h1>\n        <p>Frontend wird geladen...</p>\n        <p style="color: #666;">Falls diese Meldung bleibt, wurde das Frontend noch nicht gebaut.</p>\n    </div>\n</body>\n</html>'


class FrontendReleaseManifestView(HomeAssistantView):
    """Serve the active frontend release manifest outside HA's static cache."""

    url = "/api/sfml_stats/frontend-manifest"
    name = "api:sfml_stats:frontend_manifest"
    requires_auth = False
    cors_allowed = True

    @local_or_cloud_shell_only
    async def get(self, request: Request) -> Response:
        """Return the canonical manifest for the atomically active release."""
        if context.HASS is None:
            return web.Response(
                status=404, text="Not found", headers=FRONTEND_NO_STORE_HEADERS
            )
        active_release = await context.HASS.async_add_executor_job(
            _load_active_frontend_release, Path(context.HASS.config.path())
        )
        if active_release is None:
            return web.Response(
                status=404, text="Not found", headers=FRONTEND_NO_STORE_HEADERS
            )
        return web.Response(
            body=active_release["manifest_bytes"],
            content_type="application/json",
            headers={
                **FRONTEND_NO_STORE_HEADERS,
                "X-SFML-Frontend-Release": active_release["release_id"],
            },
        )


class NewStaticFilesView(HomeAssistantView):
    """Serve static files from new V17 frontend directory. @zara"""

    url = "/api/sfml_stats/static/{filename:.*}"
    name = "api:sfml_stats:static"
    requires_auth = False

    @local_or_cloud_shell_only
    async def get(self, request: Request, filename: str) -> Response:
        """Return a static file from frontend/. @zara"""
        if _safe_frontend_relative_path(filename) is None:
            return web.Response(status=400, text="Invalid filename")
        frontend_directory = Path(__file__).parent.parent / "frontend"
        active_release = None
        if context.HASS is not None:
            configured_frontend_directory = (
                Path(context.HASS.config.path())
                / "custom_components"
                / "sfml_stats"
                / "frontend"
            )
            if await context.HASS.async_add_executor_job(
                configured_frontend_directory.is_dir
            ):
                frontend_directory = configured_frontend_directory
            active_release = await context.HASS.async_add_executor_job(
                _load_active_frontend_release, Path(context.HASS.config.path())
            )
        payload = None
        release_id = None
        cache_headers = FRONTEND_NO_STORE_HEADERS
        if filename == FRONTEND_RELEASE_MANIFEST:
            return web.Response(
                status=404, text="Not found", headers=FRONTEND_NO_STORE_HEADERS
            )
        if active_release is not None and filename.startswith("assets/"):
            release_asset = await context.HASS.async_add_executor_job(
                _active_frontend_asset_by_path, active_release, filename
            )
            if release_asset is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as aiofiles

            asset_path, asset_metadata = release_asset
            try:
                async with aiofiles.open(asset_path, "rb") as frontend_file:
                    asset_payload = await frontend_file.read()
            except OSError:
                asset_payload = b""
            payload_verified = await context.HASS.async_add_executor_job(
                _verified_frontend_payload, asset_path, asset_metadata, asset_payload
            )
            if not payload_verified:
                return web.Response(status=404, text="Not found")
            payload = asset_payload
            release_id = active_release["release_id"]
            cache_headers = FRONTEND_IMMUTABLE_HEADERS
        elif (
            active_release is not None
            and filename in active_release["manifest"]["entries"]
        ):
            release_entry = await context.HASS.async_add_executor_job(
                _active_frontend_file, active_release, "entries", filename
            )
            if release_entry is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as aiofiles

            asset_path, asset_metadata = release_entry
            try:
                async with aiofiles.open(asset_path, "rb") as frontend_file:
                    asset_payload = await frontend_file.read()
            except OSError:
                asset_payload = b""
            payload_verified = await context.HASS.async_add_executor_job(
                _verified_frontend_payload, asset_path, asset_metadata, asset_payload
            )
            if not payload_verified:
                return web.Response(status=404, text="Not found")
            payload = asset_payload
            release_id = active_release["release_id"]
        else:
            if filename.startswith("assets/"):
                return web.Response(status=404, text="Not found")
            if context.HASS is not None:
                asset_path = await context.HASS.async_add_executor_job(
                    _frontend_path_without_symlinks, frontend_directory, filename
                )
            else:
                asset_path = _frontend_path_without_symlinks(
                    frontend_directory, filename
                )
            if asset_path is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as aiofiles

            async with aiofiles.open(asset_path, "rb") as frontend_file:
                payload = await frontend_file.read()
        content_type = "application/octet-stream"
        if filename.endswith(".js"):
            content_type = "application/javascript"
        elif filename.endswith(".css"):
            content_type = "text/css"
        elif filename.endswith(".svg"):
            content_type = "image/svg+xml"
        elif filename.endswith(".png"):
            content_type = "image/png"
        elif filename.endswith(".html"):
            content_type = "text/html"
        elif filename.endswith(".json"):
            content_type = "application/json"
        return (
            web.Response(
                body=payload,
                content_type=content_type,
                headers={
                    **cache_headers,
                    **(
                        {"X-SFML-Frontend-Release": release_id}
                        if release_id is not None
                        else {}
                    ),
                },
            )
            if True
            else None
        )
