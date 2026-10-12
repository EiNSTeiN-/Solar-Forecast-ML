# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""SMC enable flag with an origin that a settings body cannot forge."""
from __future__ import annotations
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

EMS_ORIGIN = "ems"
EXTERNAL_ORIGIN = "external"
SMC_ENABLE_LEASE = "smart_charging_enabled"
_origin: ContextVar[str] = ContextVar("sfml_smc_enable_origin", default=EXTERNAL_ORIGIN)
_writer: Callable[[Any, bool], Awaitable[dict[str, object]]] | None = None


def current_origin() -> str:
    """Return the caller origin for the active enable write."""
    return _origin.get()


def bind_enable_writer(
    writer: Callable[[Any, bool], Awaitable[dict[str, object]]],
) -> None:
    """Install the single settings writer used by EMS."""
    global _writer
    _writer = writer


async def async_set_enabled(hass: Any, enabled: bool) -> dict[str, object]:
    """Set the SMC enable flag and mark this task as the EMS writer."""
    if _writer is None:
        raise RuntimeError("smart charging enable writer is not bound")
    origin_token = _origin.set(EMS_ORIGIN)
    try:
        return await _writer(hass, bool(enabled))
    finally:
        _origin.reset(origin_token)
