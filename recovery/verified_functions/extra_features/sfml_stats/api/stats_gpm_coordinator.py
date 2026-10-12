"""Readable isolated stats lookup; the surrounding API remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Tests supply the context and domain globals without importing the integration.
"""
from __future__ import annotations


def _get_stats_gpm_coordinator() -> Any | None:
    if context.HASS is None:
        return
    for instance in context.HASS.data.get(DOMAIN, {}).values():
        if not isinstance(instance, dict):
            continue
        coordinator = instance.get("gpm_coordinator")
        if coordinator is None:
            continue
        return coordinator
