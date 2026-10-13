# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

_LOGGER = logging.getLogger(__name__)
_LIVE_POWER_UNITS = frozenset({"", "w", "kw", "mw"})


def power_state_w(state: Any) -> float | None:
    power_w = signed_power_state_w(state)
    return max(0.0, power_w) if power_w is not None else None


def live_power_state_w(state: Any, *, signed: bool = False) -> float | None:
    """Live display watts. Unknown units are rejected; conversion stays in signed_power_state_w."""
    if state is None:
        return
    state_value = getattr(state, "state", None)
    if state_value in (None, "", "unknown", "unavailable"):
        return
    attributes = getattr(state, "attributes", None) or {}
    unit = str(attributes.get("unit_of_measurement", "") or "").strip().lower()
    if unit not in _LIVE_POWER_UNITS:
        return
    if signed:
        return signed_power_state_w(state)
    return power_state_w(state)


def signed_power_state_w(state: Any) -> float | None:
    try:
        power_w = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "W")).lower()
    if unit == "kw":
        power_w *= 1000.0
    elif unit == "mw":
        power_w *= 1000000.0
    return power_w if True else None


def integrate_power_states(
    states: Iterable[Any], start_time: datetime, end_time: datetime
) -> float | None:
    ordered_states = sorted(states, key=lambda state: state.last_changed)
    if not ordered_states:
        return
    energy_wh = 0.0
    has_valid_sample = False
    for index, state in enumerate(ordered_states):
        interval_start = max(state.last_changed, start_time)
        interval_end = end_time
        if index + 1 < len(ordered_states):
            interval_end = min(ordered_states[index + 1].last_changed, end_time)
        if interval_end <= interval_start:
            continue
        power_w = power_state_w(state)
        if power_w is not None:
            energy_wh += power_w * (
                (interval_end - interval_start).total_seconds() / 3600.0
            )
            has_valid_sample = True
    return energy_wh / 1000.0 if has_valid_sample else None


def integrate_signed_power_states(
    states: Iterable[Any], start_time: datetime, end_time: datetime
) -> tuple[float, float] | None:
    ordered_states = sorted(states, key=lambda state: state.last_changed)
    if not ordered_states:
        return
    positive_energy_wh = 0.0
    negative_energy_wh = 0.0
    has_valid_sample = False
    for index, state in enumerate(ordered_states):
        interval_start = max(state.last_changed, start_time)
        interval_end = end_time
        if index + 1 < len(ordered_states):
            interval_end = min(ordered_states[index + 1].last_changed, end_time)
        if interval_end <= interval_start:
            continue
        power_w = signed_power_state_w(state)
        if power_w is not None:
            interval_hours = (interval_end - interval_start).total_seconds() / 3600.0
            if power_w >= 0:
                positive_energy_wh += power_w * interval_hours
            else:
                negative_energy_wh += -power_w * interval_hours
            has_valid_sample = True
    if not has_valid_sample:
        return
    return (positive_energy_wh / 1000.0, negative_energy_wh / 1000.0)


def power_values_at_timestamps(
    states: Iterable[Any], timestamps: Iterable[datetime], *, signed: bool = False
) -> list[float | None]:
    ordered_states = sorted(states, key=lambda state: state.last_changed)
    read_power = signed_power_state_w if signed else power_state_w
    power_values = []
    state_index = 0
    last_power_w = None
    for timestamp in timestamps:
        while (
            state_index < len(ordered_states)
            and ordered_states[state_index].last_changed <= timestamp
        ):
            last_power_w = read_power(ordered_states[state_index])
            state_index += 1
        power_values.append(last_power_w)
    return power_values


async def integrate_power_from_recorder(
    hass: Any, entity_id: str | None, start_time: datetime, end_time: datetime
) -> float | None:
    if not entity_id or end_time <= start_time:
        return None
    from ..utils.time_utils import as_ha_local as as_ha_local

    if start_time.tzinfo is None:
        start_time = as_ha_local(start_time, hass)
    if end_time.tzinfo is None:
        end_time = as_ha_local(end_time, hass)
    start_time = start_time.astimezone(timezone.utc)
    end_time = end_time.astimezone(timezone.utc)
    states = await power_states_from_recorder(hass, entity_id, start_time, end_time)
    return integrate_power_states(states, start_time, end_time)


async def power_states_from_recorder(
    hass: Any, entity_id: str | None, start_time: datetime, end_time: datetime
) -> list[Any]:
    if not entity_id or end_time <= start_time:
        return []
    from ..utils.time_utils import as_ha_local as as_ha_local

    if start_time.tzinfo is None:
        start_time = as_ha_local(start_time, hass)
    if end_time.tzinfo is None:
        end_time = as_ha_local(end_time, hass)
    start_time = start_time.astimezone(timezone.utc)
    end_time = end_time.astimezone(timezone.utc)
    try:
        from homeassistant.components.recorder import (
            get_instance as get_recorder_instance,
        )
        from homeassistant.components.recorder.history import (
            state_changes_during_period as state_changes_during_period,
        )

        recorder = get_recorder_instance(hass)
        history = await recorder.async_add_executor_job(
            state_changes_during_period,
            hass,
            start_time,
            end_time,
            entity_id,
            True,
            False,
            None,
        )
        return history.get(entity_id.lower(), []) or history.get(entity_id, [])
    except (ImportError, RuntimeError) as error:
        _LOGGER.debug("Recorder unavailable for %s: %s", entity_id, error)
    except Exception as error:
        _LOGGER.warning("Recorder history failed for %s: %s", entity_id, error)
    return [] if True else None
