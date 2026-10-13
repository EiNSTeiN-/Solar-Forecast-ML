# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Modern-only forecast intelligence API. @zara"""
from __future__ import annotations
import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..const import DOMAIN
from ..core.dev_mode import is_dev_active, json_dev_required
from ..readers.modern_analytics_reader import ModernAnalyticsReader
from ..utils.time_utils import ha_today
from .views import local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)


def _response(data: dict[str, Any]) -> web.Response:
    return web.json_response({"success": True, "data": data})


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"success": False, "error": {"code": code, "message": message}}, status=status
    )


class ModernAnalyticsView(HomeAssistantView):
    """Shared modern request handling. @zara"""

    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant, reader: ModernAnalyticsReader) -> None:
        self.hass = hass
        self.reader = reader

    def _is_enabled(self) -> bool:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict):
                continue
            if not "config" in entry_data:
                continue
            return True
        return bool(self.hass.config_entries.async_entries(DOMAIN))

    def _require_dev(self) -> web.Response | None:
        if is_dev_active(self.hass):
            return
        return json_dev_required()

    async def _run(
        self, loader: Callable[[], Awaitable[dict[str, Any]]]
    ) -> web.Response:
        if not self._is_enabled():
            return _error(
                "modern_ui_disabled", "Modern analytics are not enabled.", 404
            )
        try:
            return _response(await loader())
        except Exception as error:
            _LOGGER.error("Modern analytics request failed: %s", error, exc_info=True)
            return _error(
                "analytics_unavailable",
                "Forecast analytics are temporarily unavailable.",
                500,
            )

    @staticmethod
    def _days(
        request: web.Request,
        *,
        default: int,
        allowed: set[int] | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | web.Response:
        raw_days = request.query.get("days", str(default))
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return _error("invalid_days", "The days parameter must be an integer.", 400)
        if allowed is not None and days not in allowed:
            return _error(
                "invalid_days", f"Allowed day ranges: {sorted(allowed)}.", 400
            )
        if (
            minimum is not None
            and days < minimum
            or (maximum is not None and days > maximum)
        ):
            return _error(
                "invalid_days",
                f"The days parameter must be between {minimum} and {maximum}.",
                400,
            )
        return days


class ModernDashboardView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/dashboard"
    name = "api:sfml_stats:modern:dashboard"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        days = self._days(request, default=14, allowed={7, 14, 30})
        if isinstance(days, web.Response):
            return days
        return await self._run(lambda: self._public_dashboard(days))

    async def _public_dashboard(self, days: int) -> dict[str, Any]:
        dashboard_payload = await self.reader.get_dashboard(days)
        if is_dev_active(self.hass) or not isinstance(dashboard_payload, dict):
            return dashboard_payload
        health = dashboard_payload.get("health")
        if isinstance(health, dict):
            for diagnostic_key in (
                "absolute_error_kwh",
                "bias_kwh",
                "bias_percent",
                "wape_percent",
                "formula",
            ):
                health.pop(diagnostic_key, None)
            health_components = health.get("components")
            if isinstance(health_components, list):
                health["components"] = [
                    summary_item
                    for summary_item in health_components
                    if isinstance(summary_item, dict)
                    if summary_item.get("id") == "accuracy"
                ]
        dashboard_payload["executive_summary"] = [
            summary_item
            for summary_item in dashboard_payload.get("executive_summary") or []
            if isinstance(summary_item, dict)
            if summary_item.get("code") == "health"
        ]
        dashboard_payload["insights"] = []
        return dashboard_payload


class ModernReplayView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/replay"
    name = "api:sfml_stats:modern:replay"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_required_response = self._require_dev()
        if dev_required_response is not None:
            return dev_required_response
        date_text = request.query.get("date")
        if date_text:
            try:
                parsed_date = date.fromisoformat(date_text)
            except ValueError:
                return _error("invalid_date", "Expected date format YYYY-MM-DD.", 400)
            if parsed_date > ha_today(self.hass):
                return _error(
                    "future_date",
                    "Replay is only available for past or current days.",
                    400,
                )
        return await self._run(lambda: self.reader.get_replay(date_text))


class ModernModelsView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/models"
    name = "api:sfml_stats:modern:models"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_required_response = self._require_dev()
        if dev_required_response is not None:
            return dev_required_response
        days = self._days(request, default=30, allowed={7, 30, 90})
        if isinstance(days, web.Response):
            return days
        mode = request.query.get("mode", "morning")
        if mode not in {"morning", "reforecast"}:
            return _error("invalid_mode", "Mode must be morning or reforecast.", 400)
        return await self._run(lambda: self.reader.get_model_battle(days, mode))


class ModernHeatmapView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/heatmap"
    name = "api:sfml_stats:modern:heatmap"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_required_response = self._require_dev()
        if dev_required_response is not None:
            return dev_required_response
        days = self._days(request, default=365, minimum=90, maximum=365)
        if isinstance(days, web.Response):
            return days
        return await self._run(lambda: self.reader.get_heatmap(days))


class ModernMilestonesView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/milestones"
    name = "api:sfml_stats:modern:milestones"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_required_response = self._require_dev()
        if dev_required_response is not None:
            return dev_required_response
        days = self._days(request, default=365, minimum=90, maximum=365)
        if isinstance(days, web.Response):
            return days
        return await self._run(lambda: self.reader.get_milestones(days))


class ModernTrendsView(ModernAnalyticsView):
    url = "/api/sfml_stats/modern/trends"
    name = "api:sfml_stats:modern:trends"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_required_response = self._require_dev()
        if dev_required_response is not None:
            return dev_required_response
        days = self._days(request, default=90, minimum=30, maximum=365)
        if isinstance(days, web.Response):
            return days
        return await self._run(lambda: self.reader.get_trends(days))


async def async_setup_modern_views(hass: HomeAssistant) -> None:
    """Register modern-only analytics endpoints. @zara"""
    from .modern_weather_energy import (
        async_setup_weather_energy_views as async_setup_weather_energy_views,
    )
    from .eai import async_setup_eai_views as async_setup_eai_views
    from .premium_dashboard import (
        async_setup_premium_dashboard_view as async_setup_premium_dashboard_view,
    )
    from .tomorrow import async_setup_tomorrow_view as async_setup_tomorrow_view
    from .corrections import (
        async_setup_correction_views as async_setup_correction_views,
    )
    from .statistics_views import (
        async_setup_statistics_views as async_setup_statistics_views,
    )
    from .ems import async_setup_ems_views as async_setup_ems_views
    from .heating import async_setup_heating_views as async_setup_heating_views

    reader = ModernAnalyticsReader(hass)
    for view_class in (
        ModernDashboardView,
        ModernReplayView,
        ModernModelsView,
        ModernHeatmapView,
        ModernMilestonesView,
        ModernTrendsView,
    ):
        hass.http.register_view(view_class(hass, reader))
    await async_setup_weather_energy_views(hass)
    await async_setup_eai_views(hass)
    await async_setup_premium_dashboard_view(hass)
    await async_setup_tomorrow_view(hass)
    await async_setup_correction_views(hass)
    await async_setup_statistics_views(hass)
    await async_setup_ems_views(hass)
    await async_setup_heating_views(hass)
