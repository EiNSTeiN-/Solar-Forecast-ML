# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
from collections import defaultdict
from typing import Any, Iterable, Mapping

ACTIVE_PATTERN_TYPES = frozenset({"frequent", "occasional", "fixed_obstruction"})
LOCAL_ROOT_CAUSES = frozenset({"building_tree_obstruction", "possible_obstruction"})
MIN_SAMPLES = 7
MIN_CONFIDENCE = 0.5
MIN_LOCAL_PROBABILITY = 0.6
MIN_SEASONAL_SAMPLES = 3
MIN_SEASONAL_CONFIDENCE = 0.5


def build_shadow_signal(
    rows: Iterable[Mapping[str, Any]], *, start_hour: int
) -> dict[str, Any] | None:
    candidates_by_group = defaultdict(list)
    for row in rows:
        candidate = _eligible_candidate(row, start_hour)
        if candidate:
            candidates_by_group[candidate["group_name"]].append(candidate)
    if not candidates_by_group:
        return
    selected_groups = (
        ["_system_"]
        if "_system_" in candidates_by_group
        else sorted(candidates_by_group)
    )
    segments = []
    for group_name in selected_groups:
        current_segment = []
        previous_hour = None
        for candidate in sorted(
            candidates_by_group[group_name], key=lambda item: item["hour"]
        ):
            if previous_hour is None or candidate["hour"] == previous_hour + 1:
                current_segment.append(candidate)
            else:
                if current_segment:
                    segments.append(current_segment)
                current_segment = [candidate]
            previous_hour = candidate["hour"]
        if current_segment:
            segments.append(current_segment)
    if not segments:
        return
    best_segment = max(
        segments,
        key=lambda segment: (
            sum((pattern_entry["expected_loss_percent"] for pattern_entry in segment)),
            len(segment),
            max((pattern_entry["confidence"] for pattern_entry in segment)),
        ),
    )
    occurrence_probability = sum(
        (pattern_entry["occurrence_probability"] for pattern_entry in best_segment)
    ) / len(best_segment)
    conditional_loss_percent = sum(
        (pattern_entry["conditional_loss_percent"] for pattern_entry in best_segment)
    ) / len(best_segment)
    expected_loss_percent = sum(
        (pattern_entry["expected_loss_percent"] for pattern_entry in best_segment)
    ) / len(best_segment)
    confidence = min((pattern_entry["confidence"] for pattern_entry in best_segment))
    sample_count = min(
        (pattern_entry["sample_count"] for pattern_entry in best_segment)
    )
    pattern_type = (
        "fixed_obstruction"
        if any(
            (
                pattern_entry["pattern_type"] == "fixed_obstruction"
                for pattern_entry in best_segment
            )
        )
        else max(best_segment, key=lambda item: item["occurrence_probability"])[
            "pattern_type"
        ]
    )
    return {
        "available": True,
        "group_name": best_segment[0]["group_name"],
        "scope": (
            "system" if best_segment[0]["group_name"] == "_system_" else "panel_group"
        ),
        "start_hour": best_segment[0]["hour"],
        "end_hour": best_segment[-1]["hour"],
        "occurrence_percent": round(occurrence_probability * 100.0, 1),
        "conditional_loss_percent": round(conditional_loss_percent, 1),
        "expected_loss_percent": round(expected_loss_percent, 1),
        "confidence_percent": round(confidence * 100.0, 1),
        "sample_count": sample_count,
        "pattern_type": pattern_type,
        "forecast_effect_included": True,
    }


def build_shadow_fingerprint(
    hourly_rows: Iterable[Mapping[str, Any]],
    seasonal_rows: Iterable[Mapping[str, Any]],
    *,
    history_since: str | None = None,
) -> dict[str, Any]:
    hourly_records = [dict(row) for row in hourly_rows]
    seasonal_records = [dict(row) for row in seasonal_rows]
    hourly_fingerprint = []
    for row in hourly_records:
        candidate = eligible_local_pattern(row)
        if candidate is None:
            continue
        hour = candidate["hour"]
        hourly_fingerprint.append(
            {
                "hour": hour,
                "rate": round(candidate["occurrence_probability"], 3),
                "avg_percent": round(candidate["conditional_loss_percent"], 1),
                "intensity": round(candidate["expected_loss_percent"], 1),
                "pattern": candidate["pattern_type"],
                "confidence": round(candidate["confidence"], 2),
                "samples": candidate["sample_count"],
                "cause_building_tree": round(
                    _fraction(row.get("pct_building_tree")), 3
                ),
                "cause_weather": round(_fraction(row.get("pct_weather_clouds")), 3),
                "cause_low_sun": round(_fraction(row.get("pct_low_sun")), 3),
                "cause_other": round(_fraction(row.get("pct_other")), 3),
            }
        )
        continue
    seasonal_fingerprint = []
    for row in seasonal_records:
        hour = int(row.get("hour") or -1)
        dominant_cause = str(row.get("dominant_cause") or "unknown")
        seasonal_sample_count = int(row.get("sample_count") or 0)
        confidence = _fraction(row.get("confidence"))
        if (
            dominant_cause not in LOCAL_ROOT_CAUSES
            or seasonal_sample_count < MIN_SEASONAL_SAMPLES
            or confidence < MIN_SEASONAL_CONFIDENCE
        ):
            continue
        seasonal_occurrence_probability = _fraction(row.get("shadow_occurrence_rate"))
        seasonal_loss_percent = _percent(row.get("avg_shadow_percent"))
        seasonal_fingerprint.append(
            {
                "month": int(row.get("month") or 0),
                "hour": hour,
                "rate": round(seasonal_occurrence_probability, 3),
                "avg_percent": round(seasonal_loss_percent, 1),
                "intensity": round(
                    seasonal_occurrence_probability * seasonal_loss_percent, 1
                ),
                "cause": dominant_cause,
                "samples": seasonal_sample_count,
                "confidence": round(confidence, 2),
            }
        )
    cause_percentages = {
        "building_tree": sum(
            (
                pattern_entry["cause_building_tree"]
                for pattern_entry in hourly_fingerprint
            )
        ),
        "weather": sum(
            (pattern_entry["cause_weather"] for pattern_entry in hourly_fingerprint)
        ),
        "low_sun": sum(
            (pattern_entry["cause_low_sun"] for pattern_entry in hourly_fingerprint)
        ),
        "other": sum(
            (pattern_entry["cause_other"] for pattern_entry in hourly_fingerprint)
        ),
    }
    cause_total = sum(cause_percentages.values())
    if cause_total:
        cause_percentages = {
            cause: round(cause_count / cause_total * 100.0, 1)
            for cause, cause_count in cause_percentages.items()
        }
    else:
        cause_percentages = {cause: 0.0 for cause in cause_percentages}
    return (
        None
        if False
        else {
            "hourly": sorted(hourly_fingerprint, key=lambda item: item["hour"]),
            "seasonal": sorted(
                seasonal_fingerprint, key=lambda item: (item["month"], item["hour"])
            ),
            "summary": {
                "evaluated_samples": sum(
                    (int(row.get("sample_count") or 0) for row in hourly_records)
                ),
                "current_window_samples": sum(
                    (int(row.get("sample_count") or 0) for row in hourly_records)
                ),
                "seasonal_evaluated_samples": sum(
                    (int(row.get("sample_count") or 0) for row in seasonal_records)
                ),
                "qualified_samples": sum(
                    (pattern_entry["samples"] for pattern_entry in hourly_fingerprint)
                ),
                "shadow_hours": len(hourly_fingerprint),
                "seasonal_patterns": len(seasonal_fingerprint),
                "seasonal_shadow_hours": len(
                    {pattern_entry["hour"] for pattern_entry in seasonal_fingerprint}
                ),
                "fixed_obstructions": sum(
                    (
                        pattern_entry["pattern"] == "fixed_obstruction"
                        for pattern_entry in hourly_fingerprint
                    )
                ),
                "history_since": history_since,
                "causes": cause_percentages,
            },
            "empty_reason": (
                None if seasonal_fingerprint else "no_reliable_local_patterns"
            ),
        }
    )


def eligible_local_pattern(
    row: Mapping[str, Any], *, start_hour: int = 0
) -> dict[str, Any] | None:
    return _eligible_candidate(row, start_hour)


def is_local_shadow_observation(root_cause: Any, confidence: Any) -> bool:
    return (
        str(root_cause or "unknown") in LOCAL_ROOT_CAUSES
        and _fraction(confidence) >= MIN_CONFIDENCE
    )


def _eligible_candidate(
    row: Mapping[str, Any], start_hour: int
) -> dict[str, Any] | None:
    hour = int(row.get("hour") or -1)
    if hour < start_hour:
        return
    pattern_type = str(row.get("pattern_type") or "unknown")
    sample_count = int(row.get("sample_count") or 0)
    confidence = _fraction(row.get("confidence"))
    local_probability = _fraction(row.get("pct_building_tree"))
    if (
        pattern_type not in ACTIVE_PATTERN_TYPES
        or sample_count < MIN_SAMPLES
        or confidence < MIN_CONFIDENCE
        or (local_probability < MIN_LOCAL_PROBABILITY)
    ):
        return None
    occurrence_probability = _fraction(row.get("shadow_occurrence_rate"))
    conditional_loss_percent = _percent(row.get("avg_shadow_percent"))
    seasonal_dominant_cause = str(row.get("seasonal_dominant_cause") or "unknown")
    seasonal_sample_count = int(row.get("seasonal_sample_count") or 0)
    seasonal_confidence = _fraction(row.get("seasonal_confidence"))
    if (
        seasonal_dominant_cause in LOCAL_ROOT_CAUSES
        and seasonal_sample_count >= MIN_SEASONAL_SAMPLES
        and (seasonal_confidence >= MIN_SEASONAL_CONFIDENCE)
    ):
        seasonal_weight = min(0.4, seasonal_confidence * 0.4)
        occurrence_probability = (
            occurrence_probability * (1.0 - seasonal_weight)
            + _fraction(row.get("seasonal_occurrence_rate")) * seasonal_weight
        )
        conditional_loss_percent = (
            conditional_loss_percent * (1.0 - seasonal_weight)
            + _percent(row.get("seasonal_avg_shadow_percent")) * seasonal_weight
        )
    return {
        "group_name": str(row.get("group_name") or "_system_"),
        "hour": hour,
        "occurrence_probability": occurrence_probability,
        "conditional_loss_percent": conditional_loss_percent,
        "expected_loss_percent": occurrence_probability * conditional_loss_percent,
        "confidence": confidence,
        "sample_count": sample_count,
        "pattern_type": pattern_type,
    }


def _fraction(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _percent(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0
