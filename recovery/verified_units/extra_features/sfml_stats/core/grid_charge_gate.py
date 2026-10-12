# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Fail-closed price gate for the grid-charge switch.

The published ``is_cheap`` flag ORs the force price in. This gate ignores
that flag and ``is_force_price``. Charging is allowed when the current hour
is cheap, or when ``total_price`` is strictly below the force price.
"""
from __future__ import annotations
from datetime import datetime
from math import isfinite
from typing import Mapping


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if not isfinite(number):
        return
    return number


def _parse_stamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
    else:
        return
    return stamp if True else None


def price_snapshot_is_current_hour(data: Mapping, now: datetime) -> bool:
    """True when ``last_update`` falls in the same local hour as ``now``.

    GPM writes ``last_update`` as UTC in the same coordinator pass that binds
    ``total_price`` to the local hour. A stamp from another hour is stale.
    """
    if now.tzinfo is None:
        return False
    snapshot_stamp = _parse_stamp(data.get("last_update"))
    if snapshot_stamp is None or snapshot_stamp.tzinfo is None:
        return False
    local_stamp = snapshot_stamp.astimezone(now.tzinfo)
    return local_stamp.date() == now.date() and local_stamp.hour == now.hour


def _snapshot_force_price(data: Mapping) -> tuple[bool, float | None]:
    """Force price carried by the snapshot.

    Present means the field exists. A present but unusable value does not
    fall through to the config entry. GPM publishes it at
    ``thresholds.force_charge_price`` (coordinator data via
    ``thresholds_payload``). A top-level ``force_charge_price`` is accepted
    when the nested field is absent.
    """
    thresholds = data.get("thresholds")
    if isinstance(thresholds, Mapping) and "force_charge_price" in thresholds:
        return (True, _finite(thresholds.get("force_charge_price")))
    if "force_charge_price" in data:
        return (True, _finite(data.get("force_charge_price")))
    return (False, None)


def force_price_from_entry(coordinator: object) -> float | None:
    """Force price from the GPM config entry when the snapshot omits it.

    ``GridPriceCoordinator._entry_config`` merges ``entry.data`` then
    ``entry.options``, so options win. There is no numeric fallback.
    """
    entry = getattr(coordinator, "entry", None)
    if entry is None:
        return
    configuration = {}
    entry_data = getattr(entry, "data", None)
    entry_options = getattr(entry, "options", None)
    if isinstance(entry_data, Mapping):
        configuration.update(entry_data)
    if isinstance(entry_options, Mapping):
        configuration.update(entry_options)
    if "force_charge_price" not in configuration:
        return
    return _finite(configuration.get("force_charge_price"))


def _hour_is_cheap(data: Mapping, now: datetime, price: float) -> bool:
    threshold_mode = data.get("threshold_mode")
    if threshold_mode in ("absolute", "below_average"):
        effective_threshold = _finite(data.get("effective_threshold_ct"))
        if effective_threshold is None:
            return False
        return price < effective_threshold
    if threshold_mode == "cheapest_hours":
        cheap_hours = data.get("cheap_hours_today")
        if not isinstance(cheap_hours, (list, tuple)):
            return False
        try:
            pass
        except (TypeError, ValueError):
            return False
        try:
            cheap_hour_set = {int(hour) for hour in cheap_hours}
        except (TypeError, ValueError):
            return False
        return now.hour in cheap_hour_set
    return False


def classify_snapshot(
    data: Mapping | None, now: datetime, coordinator: object | None = None
) -> tuple[bool, bool]:
    """``(hour_is_cheap, under_force)``. Stale, demo, and missing price are closed."""
    if not isinstance(data, Mapping) or not data:
        return (False, False)
    if data.get("is_demo") is True:
        return (False, False)
    if not price_snapshot_is_current_hour(data, now):
        return (False, False)
    price = _finite(data.get("total_price"))
    if price is None:
        return (False, False)
    hour_is_cheap = _hour_is_cheap(data, now, price)
    has_force_price, force_price = _snapshot_force_price(data)
    if not has_force_price and coordinator is not None:
        force_price = force_price_from_entry(coordinator)
    under_force = force_price is not None and price < force_price
    return (hour_is_cheap, under_force)


def grid_charge_allowed(
    data: Mapping | None, now: datetime, coordinator: object | None = None
) -> bool:
    """Cheap hour or a price strictly below the force price."""
    hour_is_cheap, under_force = classify_snapshot(data, now, coordinator)
    return hour_is_cheap or under_force


def _live_price_source(hass: object) -> tuple[dict, object] | None | dict:
    """Payload and coordinator. ``None`` means no Home Assistant instance.

    An empty dict means Home Assistant is present but GPM has no payload.
    """
    if hass is None:
        return
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return {}
    gpm_entries = hass_data.get("grid_price_monitor")
    if isinstance(gpm_entries, dict):
        for coordinator in gpm_entries.values():
            payload = getattr(coordinator, "data", None)
            if not isinstance(payload, dict):
                continue
            return (payload, coordinator)
    stats_entries = hass_data.get("sfml_stats")
    if isinstance(stats_entries, dict):
        for stats_entry in stats_entries.values():
            if not isinstance(stats_entry, dict):
                continue
            coordinator = stats_entry.get("gpm_coordinator")
            payload = getattr(coordinator, "data", None)
            if not isinstance(payload, dict):
                continue
            return (payload, coordinator)
    return {}


def coordinator_price_data(hass: object) -> dict | None:
    """Return the live GPM payload. ``None`` means Home Assistant is absent."""
    price_source = _live_price_source(hass)
    if price_source is None:
        return
    if isinstance(price_source, dict):
        return price_source
    return price_source[0]


def resolve_grid_charge(hass: object, now: datetime) -> tuple[bool, bool] | None:
    """``(hour_is_cheap, under_force)``. ``None`` only without Home Assistant."""
    price_source = _live_price_source(hass)
    if price_source is None:
        return
    if isinstance(price_source, dict):
        return classify_snapshot(price_source, now, None)
    payload, coordinator = price_source
    return classify_snapshot(payload, now, coordinator)


def resolve_hour_is_cheap(hass: object, now: datetime) -> bool | None:
    """Whether the switch may turn on. ``None`` only without Home Assistant."""
    classification = resolve_grid_charge(hass, now)
    if classification is None:
        return
    cheap_hour, under_force = classification
    return cheap_hour or under_force
