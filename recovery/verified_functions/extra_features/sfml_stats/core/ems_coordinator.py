"""Readable isolated coordinator lookup; the EMS module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The class provides compilation context only; the verified unit is its one
lookup method, whose independent rejection guards and return scope are retained.
"""
from __future__ import annotations


class EMSManager:
    def _gpm_coordinator(self) -> Any | None:
        for instance in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(instance, dict):
                continue
            coordinator = instance.get("gpm_coordinator")
            if coordinator is None:
                continue
            return coordinator
