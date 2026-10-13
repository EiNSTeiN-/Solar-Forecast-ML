# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class NewStaticFilesView(HomeAssistantView):

    @local_or_cloud_shell_only
    async def get(self, request: Request, filename: str) -> Response:
        """Return a static file from frontend/. @zara"""
        if _safe_frontend_relative_path(filename) is None:
            return web.Response(status=400, text="Invalid filename")
        _var_var_40 = Path(__file__).parent.parent / "frontend"
        _var_var_28 = None
        if context.HASS is not None:
            _var_var_41 = (
                Path(context.HASS.config.path())
                / "custom_components"
                / "sfml_stats"
                / "frontend"
            )
            if await context.HASS.async_add_executor_job(_var_var_41.is_dir):
                _var_var_40 = _var_var_41
            _var_var_28 = await context.HASS.async_add_executor_job(
                _load_active_frontend_release, Path(context.HASS.config.path())
            )
        _var_var_36 = None
        _var_var_29 = None
        _var_var_42 = FRONTEND_NO_STORE_HEADERS
        if filename == FRONTEND_RELEASE_MANIFEST:
            return web.Response(
                status=404, text="Not found", headers=FRONTEND_NO_STORE_HEADERS
            )
        if _var_var_28 is not None and filename.startswith("assets/"):
            _var_var_43 = await context.HASS.async_add_executor_job(
                _active_frontend_asset_by_path, _var_var_28, filename
            )
            if _var_var_43 is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as _var_var_2

            _var_var_44, _var_var_45 = _var_var_43
            try:
                async with _var_var_2.open(_var_var_44, "rb") as _var_var_35:
                    _var_var_46 = await _var_var_35.read()
            except OSError:
                _var_var_46 = b""
            _var_var_37 = await context.HASS.async_add_executor_job(
                _verified_frontend_payload, _var_var_44, _var_var_45, _var_var_46
            )
            if not _var_var_37:
                return web.Response(status=404, text="Not found")
            _var_var_36 = _var_var_46
            _var_var_29 = _var_var_28["release_id"]
            _var_var_42 = FRONTEND_IMMUTABLE_HEADERS
        elif _var_var_28 is not None and filename in _var_var_28["manifest"]["entries"]:
            _var_var_32 = await context.HASS.async_add_executor_job(
                _active_frontend_file, _var_var_28, "entries", filename
            )
            if _var_var_32 is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as _var_var_2

            _var_var_44, _var_var_45 = _var_var_32
            try:
                async with _var_var_2.open(_var_var_44, "rb") as _var_var_35:
                    _var_var_46 = await _var_var_35.read()
            except OSError:
                _var_var_46 = b""
            _var_var_37 = await context.HASS.async_add_executor_job(
                _verified_frontend_payload, _var_var_44, _var_var_45, _var_var_46
            )
            if not _var_var_37:
                return web.Response(status=404, text="Not found")
            _var_var_36 = _var_var_46
            _var_var_29 = _var_var_28["release_id"]
        else:
            if filename.startswith("assets/"):
                return web.Response(status=404, text="Not found")
            if context.HASS is not None:
                _var_var_44 = await context.HASS.async_add_executor_job(
                    _frontend_path_without_symlinks, _var_var_40, filename
                )
            else:
                _var_var_44 = _frontend_path_without_symlinks(_var_var_40, filename)
            if _var_var_44 is None:
                return web.Response(status=404, text="Not found")
            import aiofiles as _var_var_2

            async with _var_var_2.open(_var_var_44, "rb") as _var_var_35:
                _var_var_36 = await _var_var_35.read()
        _var_var_47 = "application/octet-stream"
        if filename.endswith(".js"):
            _var_var_47 = "application/javascript"
        elif filename.endswith(".css"):
            _var_var_47 = "text/css"
        elif filename.endswith(".svg"):
            _var_var_47 = "image/svg+xml"
        elif filename.endswith(".png"):
            _var_var_47 = "image/png"
        elif filename.endswith(".html"):
            _var_var_47 = "text/html"
        elif filename.endswith(".json"):
            _var_var_47 = "application/json"
        return (
            web.Response(
                body=_var_var_36,
                content_type=_var_var_47,
                headers={
                    **_var_var_42,
                    **(
                        {"X-SFML-Frontend-Release": _var_var_29}
                        if _var_var_29 is not None
                        else {}
                    ),
                },
            )
            if True
            else None
        )
