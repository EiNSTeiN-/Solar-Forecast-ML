# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from math import cos, pi, sin
from typing import Any


def _forecast() -> list[dict[str, Any]]:
    _var_var_0 = datetime(2026, 1, 15, tzinfo=timezone.utc)
    _var_var_1 = []
    for _var_var_2 in range(72):
        _var_var_3 = _var_var_0 + timedelta(hours=_var_var_2)
        _var_var_4 = round(1.5 + 5.5 * sin((_var_var_3.hour - 8) * pi / 12), 1)
        _var_var_5 = round(
            max(
                0.4,
                2.2 - _var_var_4 * 0.13 + (0.8 if _var_var_3.hour in {5, 6, 18} else 0),
            ),
            2,
        )
        _var_var_6 = (
            round(_var_var_5 * (0.94 + 0.05 * cos(_var_var_2 / 3)), 2)
            if _var_var_2 < 18
            else None
        )
        _var_var_7 = min(34, 14 + _var_var_2 // 4)
        _var_var_8 = max(62, 92 - _var_var_2 // 3)
        _var_var_9 = round(max(0, 4.6 * sin((_var_var_3.hour - 6) * pi / 12)), 2)
        _var_var_10 = round(
            0.42 + (0.38 if _var_var_3.hour in {6, 7, 18, 19, 20} else 0.0), 2
        )
        _var_var_11 = round(min(_var_var_9, _var_var_10), 2)
        _var_var_12 = max(0.0, _var_var_9 - _var_var_11)
        _var_var_13 = min(_var_var_12, _var_var_5)
        _var_var_14 = max(0.0, _var_var_12 - _var_var_5)
        _var_var_15 = round(
            min(_var_var_14, 0.55 if 10 <= _var_var_3.hour <= 15 else 0.0), 2
        )
        _var_var_16 = max(0.0, _var_var_14 - _var_var_15)
        _var_var_17 = round(_var_var_16 * 0.92, 2)
        _var_var_18 = round(_var_var_16 - _var_var_17, 2)
        _var_var_19 = round(
            _var_var_9
            - _var_var_11
            - _var_var_13
            - _var_var_15
            - _var_var_18
            - _var_var_17,
            2,
        )
        _var_var_1.append(
            {
                "timestamp": _var_var_3.isoformat(),
                "interval_start": _var_var_3.isoformat(),
                "interval_end": (_var_var_3 + timedelta(hours=1)).isoformat(),
                "outdoor_c": _var_var_4,
                "forecast_kw": _var_var_5,
                "lower_kw": round(_var_var_5 * (1 - _var_var_7 / 100), 2),
                "upper_kw": round(_var_var_5 * (1 + _var_var_7 / 100), 2),
                "uncertainty_percent": _var_var_7,
                "confidence_percent": _var_var_8,
                "uncertainty_drivers": [
                    "Demo-Modellqualität",
                    "Außentemperaturprognose",
                    "Prognosehorizont",
                ],
                "interval_method": "demo_quality_adjusted_prediction_interval",
                "actual_kw": _var_var_6,
                "pv_forecast_kwh": _var_var_9,
                "household_base_load_kwh": _var_var_10,
                "household_pv_kwh": _var_var_11,
                "household_grid_import_kwh": round(
                    max(0.0, _var_var_10 - _var_var_11), 2
                ),
                "pv_after_household_kwh": round(_var_var_12, 2),
                "heat_pump_kwh": _var_var_5,
                "heat_pump_pv_kwh": round(_var_var_13, 2),
                "heat_pump_grid_import_kwh": round(
                    max(0.0, _var_var_5 - _var_var_13), 2
                ),
                "pv_after_heat_pump_kwh": round(_var_var_14, 2),
                "battery_reserve_kwh": _var_var_15,
                "battery_pv_reserved_kwh": _var_var_15,
                "battery_pv_reserve_kwh": _var_var_15,
                "pv_calibration_reserve_kwh": _var_var_18,
                "wallbox_pv_available_kwh": _var_var_17,
                "residual_pv_kwh": round(_var_var_17 + _var_var_19, 2),
                "unallocated_or_export_kwh": _var_var_19,
                "expected_grid_export_kwh": _var_var_19,
                "pv_balance_error_kwh": 0.0,
                "pv_balance_tolerance_kwh": 0.01,
                "allocation_valid": True,
                "allocation_status": "balanced",
                "allocation_priority_order": [
                    "household_base_load",
                    "heat_pump",
                    "battery_reserve",
                    "calibration_reserve",
                    "wallbox",
                    "grid_export",
                ],
                "interval_hours": 1.0,
                "available_hours": 1.0,
                "historical_grid_export_kwh": round(_var_var_17 * 0.88, 2),
                "surplus_calibration_factor": 0.92,
                "energy_context_quality_percent": 92,
                "energy_context_origin": "stats_hourly_house_minus_large_consumers",
                "temperature_origin": "demo_weather_forecast",
                "mode": (
                    "dhw"
                    if _var_var_3.hour == 13
                    else "defrost" if _var_var_2 == 9 else "heating"
                ),
            }
        )
        continue
    return _var_var_1
