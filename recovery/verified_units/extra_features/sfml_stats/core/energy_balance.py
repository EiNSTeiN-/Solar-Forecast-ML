# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Energy balance diagnostics for billing payloads. @zara"""
from __future__ import annotations
from math import floor, isfinite
from typing import Any

ABS_TOLERANCE_KWH = 0.05
PERCENT_TOLERANCE = 0.2


def _clean_number(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) else 0.0


def _round(value: float, digits: int = 2) -> float:
    return round(_clean_number(value), digits)


def _percent(gap: float, denominator: float) -> float:
    denominator = abs(_clean_number(denominator))
    if denominator <= 0.0:
        return 0.0
    return _round(gap / denominator * 100.0, 2)


def _is_relevant(gap: float, total: float) -> bool:
    if abs(gap) <= ABS_TOLERANCE_KWH:
        return False
    absolute_total = abs(_clean_number(total))
    if absolute_total <= 0.0:
        return True
    return abs(_percent(gap, absolute_total)) > PERCENT_TOLERANCE


def _whole_percentages(values: list[float]) -> list[int]:
    total = sum(values)
    if total <= 0.0:
        return [0] * len(values)
    exact_percentages = [source_kwh / total * 100.0 for source_kwh in values]
    floored_percentages = [floor(source_kwh) for source_kwh in exact_percentages]
    remaining_points = 100 - sum(floored_percentages)
    remainder_order = sorted(
        range(len(exact_percentages)),
        key=lambda index: (
            exact_percentages[index] - floored_percentages[index],
            values[index],
        ),
        reverse=True,
    )
    for source_index in remainder_order[:remaining_points]:
        floored_percentages[source_index] += 1
    return floored_percentages


def classify_energy_balance_quality(
    home_gap_kwh: float,
    grid_gap_kwh: float,
    solar_gap_kwh: float,
    household_total_kwh: float,
    grid_total_kwh: float,
    solar_total_kwh: float,
) -> tuple[str, list[str]]:
    """Classify period balance gaps without mutating the source values."""
    issues = []
    if _is_relevant(home_gap_kwh, household_total_kwh):
        issues.append(
            "household_negative_gap" if home_gap_kwh < 0 else "household_unknown_gap"
        )
    if _is_relevant(grid_gap_kwh, grid_total_kwh):
        issues.append("grid_negative_gap" if grid_gap_kwh < 0 else "grid_import_gap")
    if _is_relevant(solar_gap_kwh, solar_total_kwh):
        issues.append(
            "solar_negative_gap" if solar_gap_kwh < 0 else "solar_unassigned_gap"
        )
    if any((issue.endswith("negative_gap") for issue in issues)):
        return ("critical", issues)
    if issues:
        return ("warning", issues)
    return ("ok", issues)


def calculate_balance_diagnostics(
    *,
    household_total_kwh: float,
    solar_to_house_kwh: float,
    battery_to_house_kwh: float,
    grid_to_house_kwh: float,
    smartmeter_import_kwh: float,
    grid_to_battery_kwh: float,
    solar_total_kwh: float,
    solar_to_battery_kwh: float,
    grid_export_kwh: float,
) -> dict[str, Any]:
    """Return additive energy-balance diagnostics for the billing period."""
    household_kwh = _clean_number(household_total_kwh)
    solar_house_kwh = _clean_number(solar_to_house_kwh)
    battery_house_kwh = _clean_number(battery_to_house_kwh)
    grid_house_kwh = _clean_number(grid_to_house_kwh)
    import_kwh = _clean_number(smartmeter_import_kwh)
    grid_battery_kwh = _clean_number(grid_to_battery_kwh)
    solar_generation_kwh = _clean_number(solar_total_kwh)
    solar_battery_kwh = _clean_number(solar_to_battery_kwh)
    export_kwh = _clean_number(grid_export_kwh)
    assigned_household_kwh = solar_house_kwh + battery_house_kwh + grid_house_kwh
    home_gap_kwh = household_kwh - assigned_household_kwh
    assigned_grid_kwh = grid_house_kwh + grid_battery_kwh
    grid_gap_kwh = import_kwh - assigned_grid_kwh
    assigned_solar_kwh = solar_house_kwh + solar_battery_kwh + export_kwh
    solar_gap_kwh = solar_generation_kwh - assigned_solar_kwh
    data_quality, issues = classify_energy_balance_quality(
        home_gap_kwh,
        grid_gap_kwh,
        solar_gap_kwh,
        household_kwh,
        import_kwh,
        solar_generation_kwh,
    )
    return {
        "household_assigned_kwh": _round(assigned_household_kwh),
        "household_unknown_kwh": _round(max(0.0, home_gap_kwh)),
        "home_gap_kwh": _round(home_gap_kwh),
        "grid_gap_kwh": _round(grid_gap_kwh),
        "solar_gap_kwh": _round(solar_gap_kwh),
        "home_gap_percent": _percent(home_gap_kwh, household_kwh),
        "grid_gap_percent": _percent(grid_gap_kwh, import_kwh),
        "solar_gap_percent": _percent(solar_gap_kwh, solar_generation_kwh),
        "assigned_household_kwh": _round(assigned_household_kwh),
        "assigned_grid_import_kwh": _round(assigned_grid_kwh),
        "assigned_solar_kwh": _round(assigned_solar_kwh),
        "data_quality": data_quality,
        "issues": issues,
    }


def build_daily_supply_balance(
    *,
    household_total_kwh: float,
    solar_to_house_kwh: float,
    battery_to_house_kwh: float,
    grid_to_house_kwh: float,
    battery_available: bool,
) -> dict[str, Any]:
    """Build a stable household supply breakdown for the Hubble dashboard."""
    household_kwh = max(0.0, _clean_number(household_total_kwh))
    solar_house_kwh = max(0.0, _clean_number(solar_to_house_kwh))
    battery_house_kwh = (
        max(0.0, _clean_number(battery_to_house_kwh)) if battery_available else 0.0
    )
    grid_house_kwh = max(0.0, _clean_number(grid_to_house_kwh))
    assigned_household_kwh = solar_house_kwh + battery_house_kwh + grid_house_kwh
    home_gap_kwh = household_kwh - assigned_household_kwh
    data_quality, issues = classify_energy_balance_quality(
        home_gap_kwh, 0.0, 0.0, household_kwh, 0.0, 0.0
    )
    household_issues = [issue for issue in issues if issue.startswith("household_")]
    if not household_issues:
        data_quality = "ok"
    unknown_supply_kwh = (
        max(0.0, home_gap_kwh) if "household_unknown_gap" in household_issues else 0.0
    )
    source_values = [
        ("solar", solar_house_kwh),
        *((("battery", battery_house_kwh),) if battery_available else ()),
        ("grid", grid_house_kwh),
        *((("unknown", unknown_supply_kwh),) if unknown_supply_kwh > 0.0 else ()),
    ]
    source_percentages = _whole_percentages(
        [source_kwh for unused_source_name, source_kwh in source_values]
    )
    sources = {
        source_name: {"kwh": _round(source_kwh), "percent": source_percent}
        for (source_name, source_kwh), source_percent in zip(
            source_values, source_percentages, strict=True
        )
    }
    return {
        "available": household_kwh > 0.0 or assigned_household_kwh > 0.0,
        "home_consumption_kwh": _round(household_kwh),
        "assigned_kwh": _round(assigned_household_kwh),
        "home_gap_kwh": _round(home_gap_kwh),
        "battery_available": bool(battery_available),
        "data_quality": data_quality,
        "issues": household_issues,
        "sources": sources,
    }
