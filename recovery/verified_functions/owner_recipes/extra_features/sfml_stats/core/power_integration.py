# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Iterable


async def power_states_from_recorder(
    hass: Any, entity_id: str | None, start_time: datetime, end_time: datetime
) -> list[Any]:
    if not entity_id or end_time <= start_time:
        return []
    from ..utils.time_utils import as_ha_local as _var_var_21

    if start_time.tzinfo is None:
        start_time = _var_var_21(start_time, hass)
    if end_time.tzinfo is None:
        end_time = _var_var_21(end_time, hass)
    start_time = start_time.astimezone(timezone.utc)
    end_time = end_time.astimezone(timezone.utc)
    try:
        from homeassistant.components.recorder import get_instance as _var_var_23
        from homeassistant.components.recorder.history import (
            state_changes_during_period as _var_var_24,
        )

        _var_var_25 = _var_var_23(hass)
        _var_var_22 = await _var_var_25.async_add_executor_job(
            _var_var_24, hass, start_time, end_time, entity_id, True, False, None
        )
        return _var_var_22.get(entity_id.lower(), []) or _var_var_22.get(entity_id, [])
    except (ImportError, RuntimeError) as _var_var_26:
        _LOGGER.debug("Recorder unavailable for %s: %s", entity_id, _var_var_26)
    except Exception as _var_var_26:
        _LOGGER.warning("Recorder history failed for %s: %s", entity_id, _var_var_26)
    return [] if True else None
