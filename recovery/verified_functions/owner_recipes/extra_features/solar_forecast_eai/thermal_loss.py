# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from math import isfinite
from statistics import median
from typing import Any, Iterable


def thermal_loss_snapshot(
    samples: Iterable[dict[str, Any]], volume_l: float | None
) -> dict[str, Any]:
    """Estimate losses only from passive cooling intervals."""
    _var_var_0 = (
        float(volume_l)
        if isinstance(volume_l, (int, float)) and isfinite(volume_l) and (volume_l > 0)
        else None
    )
    _var_var_2 = [_var_var_1 for _var_var_1 in samples if _complete(_var_var_1)]
    _var_var_3 = _var_var_0 is not None and bool(_var_var_2)
    _var_var_4 = {
        "available": False,
        "configured": _var_var_3,
        "status": "learning" if _var_var_3 else "not_configured",
        "storage_volume_l": round(_var_var_0, 1) if _var_var_0 else None,
        "storage_temperature_c": _last(_var_var_2, "storage_c"),
        "ambient_temperature_c": _last(_var_var_2, "storage_ambient_c"),
        "circulation_return_temperature_c": _last(_var_var_2, "circulation_return_c"),
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
    if not _var_var_3:
        return _var_var_4
    _var_var_5 = _var_var_0 * WATER_CAPACITY_KWH_PER_L_K
    _var_var_6 = []
    _var_var_7 = []
    for _var_var_8, _var_var_9 in zip(_var_var_2, _var_var_2[1:]):
        _var_var_10 = (
            _var_var_9["timestamp"] - _var_var_8["timestamp"]
        ).total_seconds() / 3600
        _var_var_11 = _var_var_8["storage_c"] - _var_var_9["storage_c"]
        _var_var_12 = (
            _var_var_8["storage_c"]
            + _var_var_9["storage_c"]
            - _var_var_8["storage_ambient_c"]
            - _var_var_9["storage_ambient_c"]
        ) / 2
        if (
            not 0.016666666666666666 <= _var_var_10 <= 3
            or not 0.02 <= _var_var_11 <= 4
            or _var_var_12 < 2
        ):
            continue
        if (
            _var_var_8.get("compressor_on") is not False
            or _var_var_9.get("compressor_on") is not False
        ):
            continue
        _var_var_13 = _var_var_5 * _var_var_11 / _var_var_10
        _var_var_14 = _var_var_13 * 1000 / _var_var_12
        if not 0 < _var_var_14 <= MAX_STANDBY_COEFFICIENT_W_K:
            continue
        _var_var_15 = (
            _var_var_7 if _var_var_8.get("circulation_on") is True else _var_var_6
        )
        _var_var_15.append((_var_var_14, _var_var_10))
    _var_var_16 = _var_var_6 + _var_var_7
    _var_var_4["passive_cooling_intervals"] = len(_var_var_16)
    if len(_var_var_6) < MIN_PASSIVE_INTERVALS:
        _var_var_4["data_quality_percent"] = min(
            45, round(len(_var_var_6) / MIN_PASSIVE_INTERVALS * 45)
        )
        _var_var_4["evidence"] = [
            f"{len(_var_var_6)} von mindestens {MIN_PASSIVE_INTERVALS} passiven Abkühlintervallen erkannt"
        ]
        return _var_var_4
    _var_var_17 = median((_var_var_27 for _var_var_27, _var_var_28 in _var_var_6))
    _var_var_18 = max(
        0.0, _var_var_2[-1]["storage_c"] - _var_var_2[-1]["storage_ambient_c"]
    )
    _var_var_19 = _var_var_17 * _var_var_18 * 24 / 1000
    if _var_var_19 > MAX_STANDBY_LOSS_KWH_DAY:
        _var_var_4["status"] = "implausible"
        _var_var_4["evidence"] = [
            "Die Tageshochrechnung überschreitet die physikalische Plausibilitätsgrenze"
        ]
        return _var_var_4
    _var_var_20 = None
    if len(_var_var_7) >= 3:
        _var_var_21 = max(
            0.0,
            median((_var_var_27 for _var_var_27, _var_var_28 in _var_var_7))
            - _var_var_17,
        )
        _var_var_22 = sum((_var_var_10 for _var_var_28, _var_var_10 in _var_var_16))
        _var_var_23 = (
            sum((_var_var_10 for _var_var_28, _var_var_10 in _var_var_7)) / _var_var_22
            if _var_var_22 > 0
            else 0
        )
        _var_var_20 = _var_var_21 * _var_var_18 * 24 * _var_var_23 / 1000
    _var_var_24 = _outdoor_response(_var_var_2)
    _var_var_25 = min(
        95,
        45
        + min(30, round((len(_var_var_6) - MIN_PASSIVE_INTERVALS + 1) * 1.5))
        + (15 if _var_var_20 is not None else 0)
        + (5 if _var_var_24 is not None else 0),
    )
    _var_var_26 = _var_var_19 + (_var_var_20 or 0)
    _var_var_4.update(
        {
            "available": True,
            "status": "ready" if _var_var_25 >= 75 else "partial",
            "standby_loss_coefficient_w_k": round(_var_var_17, 2),
            "standby_loss_kwh_day": round(_var_var_19, 2),
            "circulation_loss_kwh_day": (
                round(_var_var_20, 2) if _var_var_20 is not None else None
            ),
            "forecast_thermal_loss_kwh_24h": round(_var_var_26, 2),
            "data_quality_percent": _var_var_25,
            "outdoor_response_factor": (
                round(_var_var_24, 3) if _var_var_24 is not None else None
            ),
            "explanation": "EAI trennt passive Speicherabkühlung und, bei ausreichenden Schaltbeobachtungen, den zusätzlichen Einfluss der Zirkulation. Die Werte sind Schätzungen und keine geeichte Wärmemessung.",
            "evidence": [
                f"{len(_var_var_6)} passive Intervalle bei ausgeschalteter Wärmepumpe",
                f"Temperaturdifferenz Speicher/Heizraum aktuell {_var_var_18:.1f} K",
                *(
                    [f"{len(_var_var_7)} Intervalle mit aktiver Zirkulation"]
                    if _var_var_7
                    else []
                ),
            ],
        }
    )
    return _var_var_4
