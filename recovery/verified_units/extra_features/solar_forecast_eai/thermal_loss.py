# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Empirical thermal storage and circulation loss analysis."""
from __future__ import annotations
from math import isfinite
from statistics import median
from typing import Any, Iterable

WATER_CAPACITY_KWH_PER_L_K = 0.0011627777777777778
MIN_PASSIVE_INTERVALS = 24
MAX_STANDBY_COEFFICIENT_W_K = 15.0
MAX_STANDBY_LOSS_KWH_DAY = 12.0


def thermal_loss_snapshot(
    samples: Iterable[dict[str, Any]], volume_l: float | None
) -> dict[str, Any]:
    """Estimate losses only from passive cooling intervals."""
    storage_volume_l = (
        float(volume_l)
        if isinstance(volume_l, (int, float)) and isfinite(volume_l) and (volume_l > 0)
        else None
    )
    complete_samples = [sample for sample in samples if _complete(sample)]
    configured = storage_volume_l is not None and bool(complete_samples)
    snapshot = {
        "available": False,
        "configured": configured,
        "status": "learning" if configured else "not_configured",
        "storage_volume_l": round(storage_volume_l, 1) if storage_volume_l else None,
        "storage_temperature_c": _last(complete_samples, "storage_c"),
        "ambient_temperature_c": _last(complete_samples, "storage_ambient_c"),
        "circulation_return_temperature_c": _last(
            complete_samples, "circulation_return_c"
        ),
        "standby_loss_coefficient_w_k": None,
        "standby_loss_kwh_day": None,
        "circulation_loss_kwh_day": None,
        "forecast_thermal_loss_kwh_24h": None,
        "data_quality_percent": 0,
        "passive_cooling_intervals": 0,
        "outdoor_response_factor": None,
        "explanation": "Für die Verlustanalyse werden Speichertemperatur, Temperatur am Speicher und Speichervolumen benötigt.",
        "evidence": [],
        "origin": "empirical_passive_cooling_model",
    }
    if not configured:
        return snapshot
    water_heat_capacity_kwh_k = storage_volume_l * WATER_CAPACITY_KWH_PER_L_K
    standby_intervals = []
    circulation_intervals = []
    for previous_sample, current_sample in zip(complete_samples, complete_samples[1:]):
        interval_hours = (
            current_sample["timestamp"] - previous_sample["timestamp"]
        ).total_seconds() / 3600
        storage_drop_k = previous_sample["storage_c"] - current_sample["storage_c"]
        temperature_difference_k = (
            previous_sample["storage_c"]
            + current_sample["storage_c"]
            - previous_sample["storage_ambient_c"]
            - current_sample["storage_ambient_c"]
        ) / 2
        if (
            not 0.016666666666666666 <= interval_hours <= 3
            or not 0.02 <= storage_drop_k <= 4
            or temperature_difference_k < 2
        ):
            continue
        if (
            previous_sample.get("compressor_on") is not False
            or current_sample.get("compressor_on") is not False
        ):
            continue
        cooling_power_kw = water_heat_capacity_kwh_k * storage_drop_k / interval_hours
        loss_coefficient_w_k = cooling_power_kw * 1000 / temperature_difference_k
        if not 0 < loss_coefficient_w_k <= MAX_STANDBY_COEFFICIENT_W_K:
            continue
        target_intervals = (
            circulation_intervals
            if previous_sample.get("circulation_on") is True
            else standby_intervals
        )
        target_intervals.append((loss_coefficient_w_k, interval_hours))
    passive_intervals = standby_intervals + circulation_intervals
    snapshot["passive_cooling_intervals"] = len(passive_intervals)
    if len(standby_intervals) < MIN_PASSIVE_INTERVALS:
        snapshot["data_quality_percent"] = min(
            45, round(len(standby_intervals) / MIN_PASSIVE_INTERVALS * 45)
        )
        snapshot["evidence"] = [
            f"{len(standby_intervals)} von mindestens {MIN_PASSIVE_INTERVALS} passiven Abkühlintervallen erkannt"
        ]
        return snapshot
    standby_coefficient_w_k = median(
        (
            coefficient_w_k
            for coefficient_w_k, unused_interval_hours in standby_intervals
        )
    )
    current_temperature_difference_k = max(
        0.0,
        complete_samples[-1]["storage_c"] - complete_samples[-1]["storage_ambient_c"],
    )
    standby_loss_kwh_day = (
        standby_coefficient_w_k * current_temperature_difference_k * 24 / 1000
    )
    if standby_loss_kwh_day > MAX_STANDBY_LOSS_KWH_DAY:
        snapshot["status"] = "implausible"
        snapshot["evidence"] = [
            "Die Tageshochrechnung überschreitet die physikalische Plausibilitätsgrenze"
        ]
        return snapshot
    circulation_loss_kwh_day = None
    if len(circulation_intervals) >= 3:
        circulation_coefficient_w_k = max(
            0.0,
            median(
                (
                    coefficient_w_k
                    for coefficient_w_k, unused_interval_hours in circulation_intervals
                )
            )
            - standby_coefficient_w_k,
        )
        total_interval_hours = sum(
            (
                interval_hours
                for unused_coefficient_w_k, interval_hours in passive_intervals
            )
        )
        circulation_duty_ratio = (
            sum(
                (
                    interval_hours
                    for unused_coefficient_w_k, interval_hours in circulation_intervals
                )
            )
            / total_interval_hours
            if total_interval_hours > 0
            else 0
        )
        circulation_loss_kwh_day = (
            circulation_coefficient_w_k
            * current_temperature_difference_k
            * 24
            * circulation_duty_ratio
            / 1000
        )
    outdoor_response_factor = _outdoor_response(complete_samples)
    quality_percent = min(
        95,
        45
        + min(30, round((len(standby_intervals) - MIN_PASSIVE_INTERVALS + 1) * 1.5))
        + (15 if circulation_loss_kwh_day is not None else 0)
        + (5 if outdoor_response_factor is not None else 0),
    )
    total_thermal_loss_kwh_24h = standby_loss_kwh_day + (circulation_loss_kwh_day or 0)
    snapshot.update(
        {
            "available": True,
            "status": "ready" if quality_percent >= 75 else "partial",
            "standby_loss_coefficient_w_k": round(standby_coefficient_w_k, 2),
            "standby_loss_kwh_day": round(standby_loss_kwh_day, 2),
            "circulation_loss_kwh_day": (
                round(circulation_loss_kwh_day, 2)
                if circulation_loss_kwh_day is not None
                else None
            ),
            "forecast_thermal_loss_kwh_24h": round(total_thermal_loss_kwh_24h, 2),
            "data_quality_percent": quality_percent,
            "outdoor_response_factor": (
                round(outdoor_response_factor, 3)
                if outdoor_response_factor is not None
                else None
            ),
            "explanation": "EAI trennt passive Speicherabkühlung und, bei ausreichenden Schaltbeobachtungen, den zusätzlichen Einfluss der Zirkulation. Die Werte sind Schätzungen und keine geeichte Wärmemessung.",
            "evidence": [
                f"{len(standby_intervals)} passive Intervalle bei ausgeschalteter Wärmepumpe",
                f"Temperaturdifferenz Speicher/Heizraum aktuell {current_temperature_difference_k:.1f} K",
                *(
                    [f"{len(circulation_intervals)} Intervalle mit aktiver Zirkulation"]
                    if circulation_intervals
                    else []
                ),
            ],
        }
    )
    return snapshot


def _complete(sample: dict[str, Any]) -> bool:
    return (
        all(
            (
                isinstance(sample.get(temperature_field), (int, float))
                and isfinite(sample[temperature_field])
                for temperature_field in ("storage_c", "storage_ambient_c")
            )
        )
        and sample.get("timestamp") is not None
    )


def _last(rows: list[dict[str, Any]], key: str) -> float | None:
    for row in reversed(rows):
        value = row.get(key)
        if not isinstance(value, (int, float)):
            continue
        if not isfinite(value):
            continue
        return round(float(value), 1)


def _outdoor_response(rows: list[dict[str, Any]]) -> float | None:
    outdoor_ambient_pairs = [
        (row.get("outdoor_c"), row.get("storage_ambient_c"))
        for row in rows
        if isinstance(row.get("outdoor_c"), (int, float))
    ]
    if len(outdoor_ambient_pairs) < 12:
        return
    outdoor_values_c = [
        float(temperature_pair[0]) for temperature_pair in outdoor_ambient_pairs
    ]
    ambient_values_c = [
        float(temperature_pair[1]) for temperature_pair in outdoor_ambient_pairs
    ]
    if max(outdoor_values_c) - min(outdoor_values_c) < 3:
        return
    outdoor_mean_c = sum(outdoor_values_c) / len(outdoor_values_c)
    ambient_mean_c = sum(ambient_values_c) / len(ambient_values_c)
    outdoor_variance = sum(
        ((outdoor_c - outdoor_mean_c) ** 2 for outdoor_c in outdoor_values_c)
    )
    if outdoor_variance <= 0:
        return
    response_factor = (
        sum(
            (
                (outdoor_c - outdoor_mean_c) * (ambient_c - ambient_mean_c)
                for outdoor_c, ambient_c in zip(outdoor_values_c, ambient_values_c)
            )
        )
        / outdoor_variance
    )
    return min(1.0, max(-0.2, response_factor))
