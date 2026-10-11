# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Development source; verified separately before replacing an integration file.
"""Pure panel-group shadow comparison shared by live and repair paths."""
from __future__ import annotations

import math
from typing import Any, Mapping

from .data_shadow_observation import (
    OBSERVATION_CLEAR_NEGATIVE,
    OBSERVATION_PEER_CONFIRMED,
    median,
)

MIN_GROUP_EXPECTED_KWH = 0.01
GROUP_PEER_RATIO_MAX = 0.7
GROUP_PEER_GAP_MIN = 0.25
GROUP_GLOBAL_WEATHER_RATIO_MAX = 0.55


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _not_evaluable_result(data: Mapping[str, Any]) -> dict[str, Any]:
    actual = _finite_number(data.get('actual_kwh'))
    theoretical = _finite_number(data.get('theoretical_max_kwh'))
    return {
        'shadow_type': 'not_evaluable',
        'shadow_percent': 0.0,
        'confidence': 0.0,
        'root_cause': 'insufficient_reference',
        'efficiency_ratio': round(actual / theoretical, 3)
        if actual is not None and actual >= 0.0 and theoretical is not None
        and theoretical >= MIN_GROUP_EXPECTED_KWH else 0.0,
        'loss_kwh': round(max(0.0, theoretical - actual), 3)
        if actual is not None and actual >= 0.0 and theoretical is not None
        and theoretical >= MIN_GROUP_EXPECTED_KWH else 0.0,
        'theoretical_max_kwh': round(theoretical, 4) if theoretical is not None else 0.0,
        'actual_kwh': round(actual, 4) if actual is not None else 0.0,
        'evaluable': False,
        'pattern_learning_eligible': False,
        'forecast_learning_eligible': False,
    }


def compare_panel_group_performance(
    panel_group_actuals: Mapping[str, Mapping[str, Any]], *, reference_is_valid: bool
) -> dict[str, dict[str, Any]]:
    """Return deterministic relative peer-loss results for a complete group set."""
    normalized = {}
    for group_name, group_data in panel_group_actuals.items():
        actual = _finite_number(group_data.get('actual_kwh'))
        theoretical = _finite_number(group_data.get('theoretical_max_kwh'))
        if (
            actual is None or actual < 0.0
            or theoretical is None or theoretical < MIN_GROUP_EXPECTED_KWH
        ):
            reference_is_valid = False
            continue
        normalized[group_name] = (actual / theoretical, actual, theoretical)

    if len(normalized) < 3 or len(normalized) != len(panel_group_actuals):
        reference_is_valid = False
    if not reference_is_valid:
        return {
            group_name: _not_evaluable_result(group_data)
            for group_name, group_data in panel_group_actuals.items()
        }

    clear_sky_ratios = []
    for group_name, group_data in panel_group_actuals.items():
        if group_name not in normalized:
            continue
        actual = normalized[group_name][1]
        clear_sky = _finite_number(group_data.get('clear_sky_kwh'))
        if clear_sky is None or clear_sky < MIN_GROUP_EXPECTED_KWH:
            clear_sky_ratios = []
            break
        clear_sky_ratios.append(actual / clear_sky)
    if clear_sky_ratios:
        global_weather = median(clear_sky_ratios) <= GROUP_GLOBAL_WEATHER_RATIO_MAX
    else:
        global_weather = False

    results = {}
    for group_name, (efficiency, actual, theoretical) in normalized.items():
        peer_ratios = sorted(
            peer_values[0] for peer_name, peer_values in normalized.items()
            if peer_name != group_name
        )
        peer_median = median(peer_ratios)
        peer_gap = peer_median - efficiency
        loss_ratio = peer_gap / peer_median if peer_median > 0.0 else 0.0
        shadow_percent = min(100.0, max(0.0, loss_ratio * 100.0))
        obstructed = (
            not global_weather and peer_median >= GROUP_GLOBAL_WEATHER_RATIO_MAX
            and efficiency <= peer_median * GROUP_PEER_RATIO_MAX
            and peer_gap >= GROUP_PEER_GAP_MIN
        )
        cause = (
            'possible_obstruction' if obstructed
            else 'weather_clouds' if global_weather else 'normal_variation'
        )
        results[group_name] = {
            'shadow_type': 'possible_obstruction' if obstructed else 'none',
            'shadow_percent': round(shadow_percent, 1),
            'confidence': 0.55,
            'root_cause': cause,
            'efficiency_ratio': round(efficiency, 3),
            'loss_kwh': round(max(0.0, theoretical - actual), 3),
            'theoretical_max_kwh': round(theoretical, 4),
            'actual_kwh': round(actual, 4),
            'evaluable': True,
            'learning_eligible': True,
            'observation_class': OBSERVATION_PEER_CONFIRMED if obstructed else OBSERVATION_CLEAR_NEGATIVE,
            'pattern_learning_eligible': True,
            'forecast_learning_eligible': not obstructed,
            'peer_efficiency_ratio': round(peer_median, 3),
            'peer_gap_percent': round(loss_ratio * 100.0, 1),
            'peer_gap_ratio': round(peer_gap, 3),
        }
        continue
    return results
