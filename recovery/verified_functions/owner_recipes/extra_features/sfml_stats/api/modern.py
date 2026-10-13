# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class ModernAnalyticsView(HomeAssistantView):

    @staticmethod
    def _days(
        request: web.Request,
        *,
        default: int,
        allowed: set[int] | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | web.Response:
        _var_var_2 = request.query.get("days", str(default))
        try:
            _var_var_3 = int(_var_var_2)
        except (TypeError, ValueError):
            return _error("invalid_days", "The days parameter must be an integer.", 400)
        if allowed is not None and _var_var_3 not in allowed:
            return _error(
                "invalid_days", f"Allowed day ranges: {sorted(allowed)}.", 400
            )
        if (
            minimum is not None
            and _var_var_3 < minimum
            or (maximum is not None and _var_var_3 > maximum)
        ):
            return _error(
                "invalid_days",
                f"The days parameter must be between {minimum} and {maximum}.",
                400,
            )
        return _var_var_3
