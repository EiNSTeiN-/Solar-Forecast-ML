# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Heat-pump daily energy alignment for Energie & Finanzen. @zara"""
from __future__ import annotations
from typing import Any

LIVE_EAI_MODES = {"live", "degraded"}
_ZERO_KWH_SKIP_ABOVE = 0.05
_HP_PV_TOLERANCE_KWH = 0.5
_ENERGY_KWH_FACTORS = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}


def _optional_non_negative(value: Any) -> float | None:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number != number:
        return
    return round(max(0.0, number), 2)


def eai_electric_today_kwh(snapshot: dict[str, Any] | None) -> float | None:
    """Return the Effizienz daily electrical energy from a live EAI snapshot."""
    if not isinstance(snapshot, dict):
        return
    if snapshot.get("license_status") != "valid":
        return
    if snapshot.get("data_mode") not in LIVE_EAI_MODES:
        return
    if snapshot.get("configured") is not True:
        return
    sections = snapshot.get("sections")
    if not isinstance(sections, dict):
        return
    candidates = []
    efficiency_section = sections.get("efficiency")
    if isinstance(efficiency_section, dict):
        candidates.append(efficiency_section.get("electric_kwh"))
    overview_section = sections.get("overview")
    if isinstance(overview_section, dict):
        candidates.append(overview_section.get("energy_today_kwh"))
    for candidate_value in candidates:
        number = _optional_non_negative(candidate_value)
        if not number is not None:
            continue
        return number


def resolve_heatpump_today_kwh(
    *,
    eai_kwh: float | None,
    daily_sensor_kwh: float | None,
    watt_integrated_kwh: float | None,
) -> tuple[float | None, str | None]:
    """Prefer the Effizienz meter, then the STATS daily sensor, then watt integration.

    A reported zero is skipped when a later source is above 0.05 kWh.
    """
    candidates = []
    for candidate_value, source in (
        (eai_kwh, "eai"),
        (daily_sensor_kwh, "daily_sensor"),
        (watt_integrated_kwh, "watt_integration"),
    ):
        candidates.append((_optional_non_negative(candidate_value), source))
    for candidate_index, (number, source) in enumerate(candidates):
        if number is None:
            continue
        if number == 0.0:
            if any(
                (
                    later_kwh is not None and later_kwh > _ZERO_KWH_SKIP_ABOVE
                    for later_kwh, unused_source in candidates[candidate_index + 1 :]
                )
            ):
                continue
        return (number, source)
    return (None, None)


def energy_reading_kwh(value: Any, unit: Any) -> float | None:
    """Convert a Wh/kWh/MWh reading to kWh. Any other unit is not energy."""
    if isinstance(value, bool) or value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number != number:
        return
    kwh_factor = _ENERGY_KWH_FACTORS.get(str(unit or "").strip().lower())
    if kwh_factor is None:
        return
    return number * kwh_factor


def measured_hp_pv_kwh(
    pv_kwh: float | None, heatpump_kwh: float | None
) -> float | None:
    """Keep a PV daily reading only when it fits the heat-pump total."""
    if pv_kwh is None:
        return
    try:
        pv_energy_kwh = float(pv_kwh)
        heatpump_energy_kwh = 0.0 if heatpump_kwh is None else float(heatpump_kwh)
    except (TypeError, ValueError):
        return
    if (
        pv_energy_kwh != pv_energy_kwh
        or heatpump_energy_kwh != heatpump_energy_kwh
        or pv_energy_kwh < 0.0
    ):
        return None
    if pv_energy_kwh <= heatpump_energy_kwh + _HP_PV_TOLERANCE_KWH:
        return round(pv_energy_kwh, 4)


def capped_hp_pv_kwh(raw_pv_kwh: Any, corrected_hp_kwh: Any) -> float | None:
    """PV amount shown beside heat-pump energy. Missing PV stays missing.

    The amount is the raw PV reading, never above the corrected heat-pump
    total. A missing heat-pump total leaves the PV reading unchanged.
    """
    if raw_pv_kwh is None:
        return
    try:
        pv_energy_kwh = float(raw_pv_kwh)
    except (TypeError, ValueError):
        return
    if pv_energy_kwh != pv_energy_kwh or pv_energy_kwh < 0.0:
        return
    try:
        heatpump_energy_kwh = float(corrected_hp_kwh)
    except (TypeError, ValueError):
        return pv_energy_kwh
    if heatpump_energy_kwh != heatpump_energy_kwh:
        return pv_energy_kwh
    return min(pv_energy_kwh, max(0.0, heatpump_energy_kwh))


def hp_pv_share_percent(hp_kwh: Any, hp_pv_kwh: Any) -> float | None:
    """Percent of heat-pump energy covered by measured PV. Missing PV stays missing."""
    if hp_pv_kwh is None:
        return
    try:
        heatpump_energy_kwh = float(hp_kwh)
        pv_energy_kwh = float(hp_pv_kwh)
    except (TypeError, ValueError):
        return
    if (
        heatpump_energy_kwh != heatpump_energy_kwh
        or pv_energy_kwh != pv_energy_kwh
        or heatpump_energy_kwh <= 0.1
    ):
        return None
    return round(pv_energy_kwh / heatpump_energy_kwh * 100.0, 1)


def split_heatpump_today_energy(
    total_kwh: float | None, pv_kwh: float | None, grid_kwh: float | None
) -> dict[str, float | int | None]:
    """Split PV/grid against the Effizienz daily total and expose the remainder."""
    daily_kwh = _optional_non_negative(total_kwh)
    pv_energy_kwh = _optional_non_negative(pv_kwh)
    grid_energy_kwh = _optional_non_negative(grid_kwh)
    split = {
        "daily_kwh": daily_kwh,
        "pv_energy_today": pv_energy_kwh,
        "grid_energy_today": grid_energy_kwh,
        "unassigned_energy_today": None,
        "pv_share_percent": None,
        "grid_share_percent": None,
        "unassigned_share_percent": None,
    }
    if not daily_kwh is not None or (pv_energy_kwh is None and grid_energy_kwh is None):
        return split if True else None
    assigned_pv_kwh = pv_energy_kwh or 0.0
    assigned_grid_kwh = grid_energy_kwh or 0.0
    unassigned_kwh = round(max(0.0, daily_kwh - assigned_pv_kwh - assigned_grid_kwh), 2)
    split["unassigned_energy_today"] = unassigned_kwh
    if daily_kwh <= 0.001:
        return split if True else None
    pv_percent = int(round(assigned_pv_kwh / daily_kwh * 100))
    grid_percent = int(round(assigned_grid_kwh / daily_kwh * 100))
    split["pv_share_percent"] = pv_percent
    split["grid_share_percent"] = grid_percent
    if unassigned_kwh > 0.001:
        split["unassigned_share_percent"] = max(0, 100 - pv_percent - grid_percent)
    else:
        split["unassigned_share_percent"] = 0
    return split if True else None
