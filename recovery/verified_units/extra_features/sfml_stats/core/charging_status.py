# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Display facts for Smart Charging. Does not decide or switch."""
from __future__ import annotations
from typing import Any, Mapping
from .grid_charge_gate import (
    classify_snapshot,
    grid_charge_allowed,
    price_snapshot_is_current_hour,
)


def _finite_price(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def grid_release_view(
    data: Mapping[str, Any] | None,
    now: Any,
    coordinator: object | None = None,
    *,
    is_demo: bool = False,
) -> dict[str, Any]:
    """Cheap hour, force price, or why the gate is closed."""
    snapshot = dict(data) if isinstance(data, Mapping) else {}
    if is_demo:
        snapshot["is_demo"] = True
    if snapshot.get("is_demo") is True:
        hour_is_cheap, under_force_price = classify_snapshot(snapshot, now, coordinator)
        return {
            "allowed": False,
            "hour_is_cheap": hour_is_cheap,
            "under_force": under_force_price,
            "kind": "demo",
        }
    if not snapshot:
        return {
            "allowed": False,
            "hour_is_cheap": False,
            "under_force": False,
            "kind": "unspecified",
        }
    allowed = grid_charge_allowed(snapshot, now, coordinator)
    hour_is_cheap, under_force_price = classify_snapshot(snapshot, now, coordinator)
    if (
        "last_update" not in snapshot
        and _finite_price(snapshot.get("total_price")) is None
    ):
        gate_kind = "unspecified"
    elif not price_snapshot_is_current_hour(snapshot, now):
        gate_kind = "stale"
    elif _finite_price(snapshot.get("total_price")) is None:
        gate_kind = "missing"
    elif under_force_price and (not hour_is_cheap):
        gate_kind = "force"
    elif allowed:
        gate_kind = "cheap"
    else:
        gate_kind = "closed"
    return {
        "allowed": allowed,
        "hour_is_cheap": hour_is_cheap,
        "under_force": under_force_price,
        "kind": gate_kind,
    }


_REACHED_REASONS = frozenset({"soc_reached_target_or_hysteresis", "band_soc_reached"})
_FORCE_REASONS = frozenset(
    {
        "force_price_charge_start",
        "force_price_charge_continuous",
        "band_force_below_max_soc_start",
        "band_force_below_max_soc_continuous",
    }
)
_BAND_CHARGE_REASONS = frozenset(
    {"band_charge_below_target_start", "band_charge_below_target_continuous"}
)


def status_facts(
    *,
    active: bool,
    reason: str | None,
    grid_kind: str,
    mode: str | None = None,
    target_soc: object = None,
    max_soc: object = None,
    min_soc: object = None,
    next_cheap_hour: object = None,
    under_force: bool = False,
    now_hour: int | None = None,
    observe: bool = False,
) -> dict[str, Any]:
    """Headline, one reason code, and an optional next step. No guessing."""
    why_code = str(reason or "").strip()
    if grid_kind in {"stale", "missing"}:
        reason = "price_unavailable"
        why_code = reason
        charging_active = False
    elif grid_kind == "demo":
        reason = "price_demo"
        why_code = reason
        charging_active = False
    else:
        why_code = why_code or "unknown"
        if grid_kind == "unspecified":
            charging_active = bool(active)
        else:
            charging_active = bool(active) and grid_kind in {"open", "cheap", "force"}
    next_step = None
    if observe:
        next_step = {"kind": "observe"}
    elif charging_active and why_code not in _REACHED_REASONS:
        stop_soc = _stop_percent(
            why=why_code,
            mode=mode,
            target_soc=target_soc,
            max_soc=max_soc,
            min_soc=min_soc,
            under_force=under_force,
        )
        if stop_soc is not None:
            next_step = {"kind": "stop_soc", "soc": stop_soc}
        elif (
            mode in {"combined", "forecast"}
            and (not under_force)
            and (why_code not in _FORCE_REASONS)
        ):
            next_step = {"kind": "stop_hour"}
    elif not charging_active:
        if why_code not in _REACHED_REASONS and grid_kind == "closed":
            hour = _hour_number(next_cheap_hour)
            if hour is not None and hour != now_hour:
                next_hour_step = {"kind": "next_hour", "hour": hour}
                target_soc_value = _finite_price(target_soc)
                if mode == "price_band_soc" and target_soc_value is not None:
                    next_hour_step["soc"] = int(round(target_soc_value))
                next_step = next_hour_step
    return {
        "headline": "charging" if charging_active else "idle",
        "why_code": why_code,
        "next": next_step,
    }


def _hour_number(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return
    else:
        return ((hour if 0 <= hour <= 23 else None) if True else None) if True else None


def _stop_percent(
    *,
    why: str,
    mode: str | None,
    target_soc: object,
    max_soc: object,
    min_soc: object,
    under_force: bool,
) -> int | None:
    if under_force or why in _FORCE_REASONS:
        max_soc_value = _finite_price(max_soc)
        if max_soc_value is None:
            return
        min_soc_value = _finite_price(min_soc)
        if min_soc_value is None:
            min_soc_value = 0.0
        return int(round(max(min_soc_value, max_soc_value - 2.0)))
    if why in _BAND_CHARGE_REASONS or mode == "price_band_soc":
        target_soc_value = _finite_price(target_soc)
        if target_soc_value is None:
            return
        return int(round(target_soc_value - 1.0))
