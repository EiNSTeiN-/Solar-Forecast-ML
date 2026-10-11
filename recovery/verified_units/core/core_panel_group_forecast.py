# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Panel group forecast consistency helpers."""

from typing import Any, Dict, List, Optional


def _read_group_value(group: Any, key: str) -> Any:
    if isinstance(group, dict):
        return group.get(key)
    return getattr(group, key, None)


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def get_group_power_wp(group: Any) -> float:
    for key in ("power_wp", "capacity_wp", "peak_power_wp"):
        power_wp = _positive_float(_read_group_value(group, key))
        if not power_wp > 0:
            continue
        return power_wp
    for key in ("capacity_kwp", "kwp", "power_kwp"):
        capacity_kwp = _positive_float(_read_group_value(group, key))
        if not capacity_kwp > 0:
            continue
        return capacity_kwp * 1000.0
    return 0.0


def get_group_capacity_kwp(group: Any) -> float:
    power_wp = get_group_power_wp(group)
    return power_wp / 1000.0 if power_wp > 0 else 0.0


def normalize_panel_group_capacity_fields(group: Any) -> Dict[str, Any]:
    normalized = (
        dict(group)
        if isinstance(group, dict)
        else dict(getattr(group, "__dict__", {}) or {})
    )
    power_wp = get_group_power_wp(normalized)
    if power_wp > 0:
        if _positive_float(normalized.get("power_wp")) <= 0:
            normalized["power_wp"] = power_wp
        if _positive_float(normalized.get("capacity_kwp")) <= 0:
            normalized["capacity_kwp"] = power_wp / 1000.0
    return normalized


def sync_panel_group_prediction_map(groups: List[Dict[str, Any]]) -> Dict[str, float]:
    return {
        group["name"]: round(float(group.get("power_kwh") or 0.0), 4)
        for group in groups
        if group.get("name")
    }


def normalize_group_predictions_to_total(
    groups: Optional[List[Dict[str, Any]]], target_total: float
) -> Optional[List[Dict[str, Any]]]:
    if not groups:
        return groups
    target_kwh = round(max(0.0, float(target_total or 0.0)), 3)
    group_copies = [dict(group) for group in groups]
    if target_kwh <= 0:
        for group in group_copies:
            group["power_kwh"] = 0.0
            group["contribution_percent"] = 0.0
        return group_copies
    existing_kwh = [
        max(0.0, float(group.get("power_kwh") or 0.0)) for group in group_copies
    ]
    existing_total_kwh = sum(existing_kwh)
    if existing_total_kwh > 0:
        scaled_kwh = [
            energy_kwh * target_kwh / existing_total_kwh for energy_kwh in existing_kwh
        ]
    else:
        capacities_kwp = [get_group_capacity_kwp(group) for group in group_copies]
        total_capacity_kwp = sum(capacities_kwp)
        if total_capacity_kwp > 0:
            scaled_kwh = [
                target_kwh * capacity_kwp / total_capacity_kwp
                for capacity_kwp in capacities_kwp
            ]
        else:
            equal_share_kwh = target_kwh / len(group_copies)
            scaled_kwh = [equal_share_kwh for unused_group in group_copies]
    rounded_kwh = [round(energy_kwh, 4) for energy_kwh in scaled_kwh]
    residual_kwh = round(target_kwh - sum(rounded_kwh), 4)
    if rounded_kwh and abs(residual_kwh) >= 0.0001:
        if residual_kwh >= 0:
            adjustment_index = max(
                range(len(rounded_kwh)), key=lambda idx: rounded_kwh[idx]
            )
        else:
            adjustment_index = max(
                range(len(rounded_kwh)),
                key=lambda idx: rounded_kwh[idx]
                if rounded_kwh[idx] + residual_kwh >= 0
                else -1.0,
            )
        rounded_kwh[adjustment_index] = round(
            max(0.0, rounded_kwh[adjustment_index] + residual_kwh), 4
        )
    rounded_total_kwh = sum(rounded_kwh)
    for group, energy_kwh in zip(group_copies, rounded_kwh):
        group["power_kwh"] = energy_kwh
        group["contribution_percent"] = (
            round(energy_kwh / rounded_total_kwh * 100, 1)
            if rounded_total_kwh > 0
            else 0.0
        )
    return group_copies
