# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
from __future__ import annotations
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping, Sequence

THRESHOLD_MODE_ABSOLUTE = "absolute"
THRESHOLD_MODE_BELOW_AVERAGE = "below_average"
THRESHOLD_MODE_CHEAPEST_HOURS = "cheapest_hours"
THRESHOLD_MODES = (
    THRESHOLD_MODE_ABSOLUTE,
    THRESHOLD_MODE_BELOW_AVERAGE,
    THRESHOLD_MODE_CHEAPEST_HOURS,
)
THRESHOLD_LIMITS: dict[str, dict[str, float]] = {
    "max_price": {"min": 0, "max": 100, "step": 0.5},
    "force_charge_price": {"min": 0, "max": 100, "step": 0.5},
    "below_average_pct": {"min": 0, "max": 50, "step": 1},
    "cheapest_hours": {"min": 1, "max": 12, "step": 1},
}
DEFAULT_FORCE_CHARGE_PRICE = 15.0
DEFAULT_THRESHOLD_MODE = THRESHOLD_MODE_ABSOLUTE
DEFAULT_BELOW_AVERAGE_PCT = 10
DEFAULT_CHEAPEST_HOURS = 4


@dataclass(frozen=True)
class ThresholdConfig:
    mode: str
    max_price: float
    force_charge_price: float
    below_average_pct: float
    cheapest_hours: int


@dataclass(frozen=True)
class DayThresholds:
    cheap_hours: tuple[int, ...]
    effective_threshold_ct: float | None


def thresholds_payload(config: ThresholdConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "max_price": config.max_price,
        "force_charge_price": config.force_charge_price,
        "below_average_pct": config.below_average_pct,
        "cheapest_hours": config.cheapest_hours,
    }


def threshold_options(config: ThresholdConfig) -> dict[str, Any]:
    return {
        "threshold_mode": config.mode,
        "max_price": config.max_price,
        "force_charge_price": config.force_charge_price,
        "below_average_pct": config.below_average_pct,
        "cheapest_hours": config.cheapest_hours,
    }


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return
    try:
        return float(value)
    except (TypeError, ValueError):
        return


def _in_limit(field: str, value: float) -> bool:
    limits = THRESHOLD_LIMITS[field]
    return limits["min"] <= value <= limits["max"]


def merge_and_validate_thresholds(
    current: ThresholdConfig, changes: Mapping[str, Any]
) -> tuple[ThresholdConfig | None, dict[str, str]]:
    allowed_keys = {
        "mode",
        "max_price",
        "cheapest_hours",
        "threshold_mode",
        "below_average_pct",
        "force_charge_price",
    }
    errors = {}
    for key in changes:
        if key not in allowed_keys:
            errors[key] = "unexpected_key"
    if errors:
        return (None, errors)
    mode = current.mode
    if "threshold_mode" in changes or "mode" in changes:
        requested_mode = changes.get("threshold_mode", changes.get("mode"))
        if requested_mode not in THRESHOLD_MODES:
            errors["threshold_mode"] = "invalid_mode"
        else:
            mode = str(requested_mode)
    max_price = current.max_price
    if "max_price" in changes:
        number = _as_number(changes["max_price"])
        if number is None or not _in_limit("max_price", number):
            errors["max_price"] = "range"
        else:
            max_price = number
    force_charge_price = current.force_charge_price
    if "force_charge_price" in changes:
        number = _as_number(changes["force_charge_price"])
        if number is None or not _in_limit("force_charge_price", number):
            errors["force_charge_price"] = "range"
        else:
            force_charge_price = number
    below_average_pct = current.below_average_pct
    if "below_average_pct" in changes:
        number = _as_number(changes["below_average_pct"])
        if number is None or not _in_limit("below_average_pct", number):
            errors["below_average_pct"] = "range"
        else:
            below_average_pct = number
    cheapest_hours = current.cheapest_hours
    if "cheapest_hours" in changes:
        number = _as_number(changes["cheapest_hours"])
        if (
            number is None
            or number != int(number)
            or (not _in_limit("cheapest_hours", number))
        ):
            errors["cheapest_hours"] = "range"
        else:
            cheapest_hours = int(number)
    if errors:
        return (None, errors)
    if mode == THRESHOLD_MODE_ABSOLUTE and force_charge_price > max_price:
        return (None, {"force_charge_price": "force_above_cheap"})
    return (
        ThresholdConfig(
            mode=mode,
            max_price=max_price,
            force_charge_price=force_charge_price,
            below_average_pct=below_average_pct,
            cheapest_hours=cheapest_hours,
        ),
        {},
    )


def evaluate_day(
    slots: Sequence[tuple[int, float]], config: ThresholdConfig
) -> DayThresholds:
    if not slots:
        return DayThresholds(cheap_hours=(), effective_threshold_ct=None)
    if config.mode == THRESHOLD_MODE_ABSOLUTE:
        cheap_hours = tuple((hour for hour, price in slots if price < config.max_price))
        return DayThresholds(
            cheap_hours=cheap_hours, effective_threshold_ct=config.max_price
        )
    if config.mode == THRESHOLD_MODE_BELOW_AVERAGE:
        threshold = fmean((price for hour, price in slots)) * (
            1 - config.below_average_pct / 100.0
        )
        cheap_hours = tuple((hour for hour, price in slots if price < threshold))
        return DayThresholds(cheap_hours=cheap_hours, effective_threshold_ct=threshold)
    ranked_slots = sorted(
        enumerate(slots), key=lambda item: (item[1][1], item[1][0], item[0])
    )
    selected_slots = ranked_slots[: max(config.cheapest_hours, 0)]
    threshold = selected_slots[-1][1][1] if selected_slots else None
    cheap_hours = tuple(
        (
            slots[slot_index][0]
            for slot_index, slot in sorted(selected_slots, key=lambda item: item[0])
        )
    )
    return DayThresholds(cheap_hours=cheap_hours, effective_threshold_ct=threshold)


def slot_is_cheap(
    hour: int, total: float, day: DayThresholds, config: ThresholdConfig
) -> bool:
    """Whether one price slot is cheap under the day's engine result."""
    if config.mode == THRESHOLD_MODE_CHEAPEST_HOURS:
        return hour in day.cheap_hours
    if day.effective_threshold_ct is None:
        return False
    return total < day.effective_threshold_ct


def evaluate_current(
    total_price: float | None,
    current_hour: int,
    day: DayThresholds,
    config: ThresholdConfig,
) -> tuple[bool, bool]:
    if total_price is None:
        return (False, False)
    force_charge = total_price < config.force_charge_price
    cheap = slot_is_cheap(current_hour, total_price, day, config) or force_charge
    return (cheap, force_charge)
