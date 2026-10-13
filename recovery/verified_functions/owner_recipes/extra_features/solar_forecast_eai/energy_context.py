# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class EAISharedEnergyAllocator:

    def enrich(
        self, hours: list[dict[str, Any]], wallbox: WallboxEnvelope | None = None
    ) -> list[dict[str, Any]]:
        _var_var_3 = (
            wallbox if isinstance(wallbox, WallboxEnvelope) else WallboxEnvelope()
        )
        _var_var_4 = self._context()
        _var_var_6 = {
            (
                int(_var_var_5.get("weekday", -1)),
                int(_var_var_5.get("hour", -1)),
            ): _var_var_5
            for _var_var_5 in _var_var_4.get("profile", [])
            if isinstance(_var_var_5, dict)
        }
        _var_var_7 = bool(_var_var_4.get("available") and _var_var_6)
        _var_var_8 = self._intervals(hours)
        _var_var_9 = []
        for _var_var_10, _var_var_11, _var_var_12, _var_var_13 in _var_var_8:
            _var_var_5 = dict(_var_var_10)
            _var_var_14 = (_var_var_12 - _var_var_11).total_seconds() / 3600
            _var_var_15 = (
                self._profile_segments(_var_var_6, _var_var_11, _var_var_12)
                if _var_var_7
                else []
            )
            _var_var_16 = bool(_var_var_15) and all(
                (_var_var_17 is not None for _var_var_44, _var_var_17 in _var_var_15)
            )
            _var_var_18 = [
                (
                    _optional_number(
                        (_var_var_17 or {}).get("household_base_load_kwh")
                    ),
                    hours,
                )
                for hours, _var_var_17 in _var_var_15
            ]
            _var_var_19 = (
                sum(
                    (
                        _var_var_45 * _var_var_46
                        for _var_var_45, _var_var_46 in _var_var_18
                    )
                )
                if _var_var_16
                and all(
                    (
                        _var_var_45 is not None
                        for _var_var_45, _var_var_44 in _var_var_18
                    )
                )
                else None
            )
            _var_var_20 = [
                (
                    _optional_number((_var_var_17 or {}).get("battery_reserve_kwh")),
                    hours,
                )
                for hours, _var_var_17 in _var_var_15
            ]
            _var_var_21 = (
                sum(
                    (
                        _var_var_45 * _var_var_46
                        for _var_var_45, _var_var_46 in _var_var_20
                    )
                )
                if _var_var_16
                and all(
                    (
                        _var_var_45 is not None
                        for _var_var_45, _var_var_44 in _var_var_20
                    )
                )
                else None
            )
            _var_var_22 = (
                sum(
                    (
                        min(
                            1.0,
                            _number(
                                (_var_var_17 or {}).get("surplus_calibration_factor"),
                                1.0,
                            ),
                        )
                        * _var_var_46
                        for _var_var_46, _var_var_17 in _var_var_15
                    )
                )
                / _var_var_14
                if _var_var_15 and _var_var_14 > 0
                else 1.0
            )
            _var_var_23 = (
                sum(
                    (
                        _number((_var_var_17 or {}).get("historical_grid_export_kwh"))
                        * _var_var_46
                        for _var_var_46, _var_var_17 in _var_var_15
                    )
                )
                if _var_var_16
                else None
            )
            _var_var_24 = (
                sum(
                    (
                        _number((_var_var_17 or {}).get("quality_percent"))
                        * _var_var_46
                        for _var_var_46, _var_var_17 in _var_var_15
                    )
                )
                / _var_var_14
                if _var_var_16 and _var_var_14 > 0
                else 0.0
            )
            _var_var_25 = _optional_number(_var_var_5.get("pv_forecast_kwh"))
            _var_var_26 = _optional_number(_var_var_5.get("forecast_kw"))
            _var_var_27 = _var_var_26 * _var_var_14 if _var_var_26 is not None else None
            _var_var_28 = bool(
                _var_var_7
                and _var_var_16
                and (not _var_var_13)
                and (_var_var_25 is not None)
                and (_var_var_27 is not None)
                and (_var_var_19 is not None)
                and (_var_var_21 is not None)
            )
            if _var_var_28:
                _var_var_29 = min(_var_var_25, _var_var_19)
                _var_var_30 = max(0.0, _var_var_19 - _var_var_29)
                _var_var_31 = max(0.0, _var_var_25 - _var_var_19)
                _var_var_32 = min(_var_var_31, _var_var_27)
                _var_var_33 = max(0.0, _var_var_27 - _var_var_32)
                _var_var_34 = max(0.0, _var_var_31 - _var_var_27)
                _var_var_35 = min(_var_var_34, _var_var_21)
                _var_var_36 = max(0.0, _var_var_34 - _var_var_35)
                _var_var_37 = _var_var_36 * (1.0 - _var_var_22)
                _var_var_38 = _var_var_36 - _var_var_37
                _var_var_39 = min(_var_var_38, _var_var_3.capacity_kwh(_var_var_14))
                _var_var_40 = max(0.0, _var_var_38 - _var_var_39)
                _var_var_41 = _var_var_25 - (
                    _var_var_29
                    + _var_var_32
                    + _var_var_35
                    + _var_var_39
                    + _var_var_37
                    + _var_var_40
                )
                _var_var_42 = abs(_var_var_41) <= _BALANCE_TOLERANCE_KWH
                _var_var_43 = "valid" if _var_var_42 else "balance_mismatch"
            else:
                _var_var_42 = False
                _var_var_29 = None
                _var_var_30 = None
                _var_var_31 = None
                _var_var_32 = None
                _var_var_33 = None
                _var_var_34 = None
                _var_var_35 = None
                _var_var_38 = None
                _var_var_40 = None
                _var_var_39 = None
                _var_var_37 = None
                _var_var_41 = None
                _var_var_43 = (
                    "energy_context_unavailable"
                    if not _var_var_7 or not _var_var_16
                    else (
                        "energy_context_incomplete"
                        if _var_var_19 is None or _var_var_21 is None
                        else "forecast_data_incomplete"
                    )
                )
            _var_var_5.update(
                {
                    "interval_start": _var_var_11.isoformat(),
                    "interval_end": _var_var_12.isoformat(),
                    "interval_hours": round(_var_var_14, 6),
                    "interval_gap_before": _var_var_13,
                    "heat_pump_kwh": self._rounded(_var_var_27),
                    "household_base_load_kwh": self._rounded(
                        _var_var_19 if _var_var_42 else None
                    ),
                    "household_pv_kwh": self._rounded(_var_var_29),
                    "household_grid_import_kwh": self._rounded(_var_var_30),
                    "pv_after_household_kwh": self._rounded(_var_var_31),
                    "heat_pump_pv_kwh": self._rounded(_var_var_32),
                    "heat_pump_grid_import_kwh": self._rounded(_var_var_33),
                    "pv_after_heat_pump_kwh": self._rounded(_var_var_34),
                    "battery_reserve_kwh": self._rounded(
                        _var_var_21 if _var_var_42 else None
                    ),
                    "battery_pv_reserve_kwh": self._rounded(_var_var_35),
                    "battery_pv_reserved_kwh": self._rounded(_var_var_35),
                    "pv_calibration_reserve_kwh": self._rounded(_var_var_37),
                    "wallbox_pv_available_kwh": self._rounded(_var_var_39),
                    "residual_pv_kwh": self._rounded(_var_var_38),
                    "unallocated_or_export_kwh": self._rounded(_var_var_40),
                    "expected_grid_export_kwh": self._rounded(_var_var_40),
                    "pv_balance_error_kwh": self._rounded(_var_var_41),
                    "pv_balance_tolerance_kwh": _BALANCE_TOLERANCE_KWH,
                    "allocation_valid": _var_var_42,
                    "allocation_status": _var_var_43,
                    "historical_grid_export_kwh": (
                        round(_var_var_23, 3)
                        if _var_var_42 and _var_var_23 is not None
                        else None
                    ),
                    "surplus_calibration_factor": round(_var_var_22, 3),
                    "energy_context_quality_percent": round(_var_var_24),
                    "energy_context_origin": (
                        _var_var_4.get("base_load_origin")
                        if _var_var_7
                        else "fallback_without_stats_history"
                    ),
                    "allocation_priority_order": list(self.priority_order),
                }
            )
            _var_var_9.append(_var_var_5)
            continue
        return _var_var_9
