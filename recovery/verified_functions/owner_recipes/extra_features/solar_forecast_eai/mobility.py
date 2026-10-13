# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .const import (
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_ELECTRICITY_PRICE_UNIT,
    CONF_EV_BATTERY_CAPACITY_KWH,
    CONF_EV_CHARGING_EFFICIENCY_PERCENT,
    CONF_EV_CONSUMPTION_KWH_PER_100KM,
    CONF_EV_DEMAND_MODE,
    CONF_EV_DEPARTURE_TIME,
    CONF_EV_PLANNED_DISTANCE_ENTITY,
    CONF_EV_REQUIRED_ENERGY_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_FEED_IN_TARIFF_ENTITY,
    CONF_FEED_IN_TARIFF_UNIT,
    CONF_LOW_PRICE_THRESHOLD_CT,
    CONF_WALLBOX_CHARGING_ENTITY,
    CONF_WALLBOX_CONNECTED_ENTITY,
    CONF_WALLBOX_ENABLED,
    CONF_WALLBOX_ENERGY_TODAY_ENTITY,
    CONF_WALLBOX_MAX_POWER_KW,
    CONF_WALLBOX_POWER_ENTITY,
    DEFAULT_EV_BATTERY_CAPACITY_KWH,
    DEFAULT_EV_CHARGING_EFFICIENCY_PERCENT,
    DEFAULT_EV_CONSUMPTION_KWH_PER_100KM,
    DEFAULT_EV_DEPARTURE_TIME,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_LOW_PRICE_THRESHOLD_CT,
    DEFAULT_WALLBOX_MAX_POWER_KW,
)


class EAIMobilityPlanner:

    def snapshot(
        self, config: dict[str, Any], forecast: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        if False:
            __assert_armored__ = None
        if not config.get(CONF_WALLBOX_ENABLED):
            return {
                "available": False,
                "configured": False,
                "reason": "not_configured",
                "advisory_only": True,
                "control_services_called": False,
                "planning_window_start": None,
                "planning_window_end": None,
                "planning_horizon_hours": None,
                "planning_covered_hours": 0.0,
                "planning_intervals_contiguous": False,
                "planning_window_complete": False,
                "hours": [],
            }
        self._input_reasons = set()
        self._missing_inputs = set()
        _var_var_2 = config.get(CONF_EV_DEMAND_MODE)
        _var_var_3 = (
            self._state_measurement(
                config.get(CONF_EV_SOC_ENTITY), now, "vehicle_soc", {"%": 1.0}, 21600
            )
            if _var_var_2 in (None, "soc")
            else None
        )
        if _var_var_3 is not None:
            if not 0 <= _var_var_3 <= 100:
                _var_var_3 = None
        _var_var_4 = self._bounded(
            config.get(CONF_EV_BATTERY_CAPACITY_KWH, DEFAULT_EV_BATTERY_CAPACITY_KWH),
            5,
            250,
        )
        _var_var_5 = self._bounded(
            config.get(CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC), 10, 100
        )
        _var_var_6 = None
        if _var_var_2 == "energy" or (_var_var_2 is None and _var_var_3 is None):
            _var_var_6 = self._state_measurement(
                config.get(CONF_EV_REQUIRED_ENERGY_ENTITY),
                now,
                "requested_energy",
                {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0},
                21600,
            )
        _var_var_7 = None
        if _var_var_2 == "distance" or (
            _var_var_2 is None and _var_var_3 is None and (_var_var_6 is None)
        ):
            _var_var_7 = self._state_measurement(
                config.get(CONF_EV_PLANNED_DISTANCE_ENTITY),
                now,
                "planned_distance",
                {"km": 1.0, "mi": 1.609344},
                21600,
            )
        _var_var_6 = _var_var_6 if _var_var_6 is not None and _var_var_6 >= 0 else None
        _var_var_7 = _var_var_7 if _var_var_7 is not None and _var_var_7 >= 0 else None
        _var_var_8 = self._bounded(
            config.get(
                CONF_EV_CONSUMPTION_KWH_PER_100KM, DEFAULT_EV_CONSUMPTION_KWH_PER_100KM
            ),
            5,
            60,
        )
        _var_var_9 = self._bounded(
            config.get(
                CONF_EV_CHARGING_EFFICIENCY_PERCENT,
                DEFAULT_EV_CHARGING_EFFICIENCY_PERCENT,
            ),
            50,
            100,
        )
        _var_var_10 = self._bounded(
            config.get(CONF_WALLBOX_MAX_POWER_KW, DEFAULT_WALLBOX_MAX_POWER_KW), 1, 50
        )
        _var_var_11 = self._departure(
            config.get(CONF_EV_DEPARTURE_TIME, DEFAULT_EV_DEPARTURE_TIME), now
        )
        _var_var_12, _var_var_13, _var_var_14 = self._demand(
            mode=_var_var_2,
            soc=_var_var_3,
            target_soc=_var_var_5,
            capacity=_var_var_4,
            requested_energy=_var_var_6,
            planned_distance=_var_var_7,
            consumption=_var_var_8,
        )
        if _var_var_12 is not None:
            _var_var_15 = {
                "soc": "vehicle_soc",
                "energy": "requested_energy",
                "distance": "planned_distance",
            }[_var_var_12]
            _var_var_16 = {"vehicle_soc", "planned_distance", "requested_energy"}
            _var_var_17 = _var_var_16 - {_var_var_15}
            self._missing_inputs.difference_update(_var_var_17)
            self._input_reasons = {
                _var_var_1
                for _var_var_1 in self._input_reasons
                if not any(
                    (
                        _var_var_1 == _var_var_67
                        or _var_var_1.startswith(f"{_var_var_67}_")
                        for _var_var_67 in _var_var_17
                    )
                )
            }
        _var_var_18 = (
            _var_var_13 / (_var_var_9 / 100) if _var_var_13 is not None else None
        )
        _var_var_19 = self._state_bool(
            config.get(CONF_WALLBOX_CONNECTED_ENTITY), now, "wallbox_connected", 1800
        )
        _var_var_20 = self._state_bool(
            config.get(CONF_WALLBOX_CHARGING_ENTITY), now, "wallbox_charging", 1800
        )
        _var_var_21 = self._state_measurement(
            config.get(CONF_WALLBOX_POWER_ENTITY),
            now,
            "wallbox_power",
            {"w": 0.001, "kw": 1.0, "mw": 1000.0},
            600,
        )
        _var_var_22 = self._state_measurement(
            config.get(CONF_WALLBOX_ENERGY_TODAY_ENTITY),
            now,
            "wallbox_energy_today",
            {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0},
            21600,
        )
        _var_var_23 = self._price_ct(
            config.get(CONF_ELECTRICITY_PRICE_ENTITY),
            config.get(CONF_ELECTRICITY_PRICE_UNIT),
        )
        _var_var_24 = self._price_ct(
            config.get(CONF_FEED_IN_TARIFF_ENTITY), config.get(CONF_FEED_IN_TARIFF_UNIT)
        )
        _var_var_25 = self._bounded(
            config.get(CONF_LOW_PRICE_THRESHOLD_CT, DEFAULT_LOW_PRICE_THRESHOLD_CT),
            0,
            200,
        )
        _var_var_26 = self._eligible_hours(forecast.get("hours"), now, _var_var_11)
        _var_var_27 = self._planning_window_contract(_var_var_26, now, _var_var_11)
        _var_var_28 = _var_var_18 is not None and _var_var_18 <= 0.1
        _var_var_29 = bool(
            _var_var_18 is not None
            and (
                _var_var_28 or (_var_var_26 and _var_var_27["planning_window_complete"])
            )
        )
        _var_var_30 = bool(_var_var_26) and all(
            (_var_var_40["allocation_valid"] for _var_var_40 in _var_var_26)
        )
        _var_var_31 = _var_var_29 and _var_var_30
        _var_var_32 = (
            "valid"
            if _var_var_30
            else (
                "mixed"
                if any((_var_var_40["allocation_valid"] for _var_var_40 in _var_var_26))
                else "invalid" if _var_var_26 else "no_intervals"
            )
        )
        _var_var_33 = self._allocate(
            _var_var_26, _var_var_18, _var_var_10, use_residual_pv=_var_var_30
        )
        _var_var_34 = sum((_var_var_40["wallbox_kwh"] for _var_var_40 in _var_var_33))
        _var_var_35 = sum(
            (_var_var_40["pv_wallbox_kwh"] for _var_var_40 in _var_var_33)
        )
        _var_var_36 = sum(
            (_var_var_40["grid_wallbox_kwh"] for _var_var_40 in _var_var_33)
        )
        _var_var_37 = (
            sum(
                (
                    _var_var_40["household_grid_import_kwh"]
                    for _var_var_40 in _var_var_33
                )
            )
            if _var_var_30
            else None
        )
        _var_var_38 = (
            sum(
                (
                    _var_var_40["heat_pump_grid_import_kwh"]
                    for _var_var_40 in _var_var_33
                )
            )
            if _var_var_30
            else None
        )
        _var_var_39 = (
            sum(
                (_var_var_40["expected_grid_export_kwh"] for _var_var_40 in _var_var_33)
            )
            if _var_var_30
            else None
        )
        _var_var_41 = [
            _var_var_40 for _var_var_40 in _var_var_33 if _var_var_40["wallbox_kwh"] > 0
        ]
        _var_var_42 = sum(
            (
                (
                    min(
                        _var_var_40["pv_wallbox_kwh"],
                        _var_var_40["residual_pv_lower_kwh"],
                    )
                    if _var_var_40["residual_pv_lower_kwh"] is not None
                    else 0.0
                )
                for _var_var_40 in _var_var_41
            )
        )
        _var_var_43 = sum(
            (
                (
                    min(
                        _var_var_40["pv_wallbox_kwh"],
                        _var_var_40["residual_pv_upper_kwh"],
                    )
                    if _var_var_40["residual_pv_upper_kwh"] is not None
                    else 0.0
                )
                for _var_var_40 in _var_var_41
            )
        )
        _var_var_44 = (
            round(
                sum(
                    (
                        _var_var_40["forecast_confidence_percent"]
                        for _var_var_40 in _var_var_41
                    )
                )
                / len(_var_var_41)
            )
            if _var_var_41
            else 0
        )
        _var_var_45 = (
            round(
                sum(
                    (
                        _var_var_40["forecast_uncertainty_percent"]
                        for _var_var_40 in _var_var_41
                    )
                )
                / len(_var_var_41)
            )
            if _var_var_41
            else 100
        )
        _var_var_46 = (
            round(_var_var_35 / _var_var_34 * 100)
            if _var_var_30 and _var_var_34 > 0
            else None
        )
        _var_var_47 = (
            round(min(100.0, _var_var_34 / _var_var_18 * 100))
            if _var_var_18 and _var_var_18 > 0
            else 100 if _var_var_18 is not None else 0
        )
        _var_var_48 = bool(
            _var_var_18 is not None
            and _var_var_18 > 0.1
            and (_var_var_34 + 0.05 < _var_var_18 or _var_var_19 is False)
        )
        _var_var_49 = next(
            (
                self._interval_boundary(_var_var_40, "available_start")
                for _var_var_40 in _var_var_33
                if _var_var_40["wallbox_kwh"] > 0
            ),
            None,
        )
        _var_var_50 = next(
            (
                self._interval_boundary(_var_var_40, "available_end")
                for _var_var_40 in reversed(_var_var_33)
                if _var_var_40["wallbox_kwh"] > 0
            ),
            None,
        )
        _var_var_51 = next(
            (
                _var_var_40
                for _var_var_40 in _var_var_33
                if self._interval_boundary(_var_var_40, "available_start")
                <= now
                < self._interval_boundary(_var_var_40, "available_end")
            ),
            None,
        )
        _var_var_52 = bool(_var_var_51 and _var_var_51["wallbox_kwh"] > 0)
        _var_var_53 = bool(_var_var_51 and _var_var_51["pv_wallbox_kwh"] > 0)
        _var_var_54 = bool(
            _var_var_51
            and _var_var_51["allocation_valid"]
            and (_var_var_51["residual_pv_kwh"] >= 0.5)
        )
        _var_var_55 = bool(_var_var_23 is not None and _var_var_23 <= _var_var_25)
        _var_var_56, _var_var_1 = self._recommendation(
            required=_var_var_18,
            connected=_var_var_19,
            departure_risk=_var_var_48,
            current_window=_var_var_52,
            current_pv=_var_var_53,
            low_price=_var_var_55,
            planned_pv=_var_var_31 and _var_var_35 > 0,
        )
        if (
            _var_var_18 is not None
            and _var_var_18 > 0.1
            and _var_var_26
            and (not _var_var_29)
        ):
            _var_var_56 = "data_unavailable"
            _var_var_1 = "planning_window_incomplete"
            self._input_reasons.add(_var_var_1)
        _var_var_57 = (
            round(_var_var_18 * _var_var_23 / 100, 2)
            if _var_var_18 is not None and _var_var_23 is not None
            else None
        )
        _var_var_58 = (
            round(
                _var_var_36 * _var_var_23 / 100
                + (_var_var_35 if _var_var_30 else 0.0) * (_var_var_24 or 0) / 100,
                2,
            )
            if _var_var_23 is not None
            else None
        )
        _var_var_59 = (
            round(max(0.0, _var_var_57 - _var_var_58), 2)
            if _var_var_57 is not None and _var_var_58 is not None
            else None
        )
        _var_var_60 = (
            _var_var_18 is not None,
            _var_var_21 is not None,
            _var_var_19 is not None,
            bool(_var_var_26),
        )
        _var_var_61 = (
            round(
                sum(
                    (
                        _var_var_40["energy_context_quality_percent"]
                        for _var_var_40 in _var_var_26
                    )
                )
                / len(_var_var_26)
            )
            if _var_var_26
            else 0
        )
        _var_var_62 = round(
            (sum(_var_var_60) + _var_var_61 / 100) / (len(_var_var_60) + 1) * 100
        )
        _var_var_63 = min(_var_var_62, _var_var_44 or _var_var_62, _var_var_14)
        _var_var_45 = max(_var_var_45, 100 - _var_var_14)
        _var_var_64 = self._explanation(
            action=_var_var_56,
            reason=_var_var_1,
            required=_var_var_18,
            pv_energy=_var_var_35 if _var_var_31 else None,
            pv_energy_lower=_var_var_42 if _var_var_31 else None,
            pv_energy_upper=_var_var_43 if _var_var_31 else None,
            target_soc=_var_var_5,
            demand_source=_var_var_12,
            battery_energy=_var_var_13,
            planned_distance=_var_var_7,
            consumption=_var_var_8,
            charging_efficiency=_var_var_9,
            departure=_var_var_11,
            price_ct=_var_var_23,
            confidence=_var_var_63,
            uncertainty=_var_var_45,
        )
        if not _var_var_26 and (not _var_var_28):
            self._input_reasons.add("forecast_plan_unavailable")
            self._missing_inputs.add("forecast_plan")
        if not _var_var_30:
            if _var_var_26:
                self._input_reasons.add("pv_allocation_invalid")
        _var_var_65 = sorted(
            (
                _var_var_1
                for _var_var_1 in self._input_reasons
                if _var_var_1.startswith("wallbox_")
            )
        )
        _var_var_66 = sorted(
            (
                _var_var_1
                for _var_var_1 in self._input_reasons
                if _var_var_1.startswith(
                    ("vehicle_soc_", "requested_energy_", "planned_distance_")
                )
            )
        )
        return {
            "available": _var_var_29,
            "configured": True,
            "reason": (
                None
                if _var_var_29
                else (
                    _var_var_1
                    if _var_var_1 == "planning_window_incomplete"
                    else "data_unavailable"
                )
            ),
            "reason_codes": sorted(self._input_reasons),
            "missing_inputs": sorted(self._missing_inputs),
            "telemetry_available": any(
                (
                    _var_var_68 is not None
                    for _var_var_68 in (
                        _var_var_21,
                        _var_var_22,
                        _var_var_19,
                        _var_var_20,
                    )
                )
            ),
            "telemetry": {
                "available": any(
                    (
                        _var_var_68 is not None
                        for _var_var_68 in (
                            _var_var_21,
                            _var_var_22,
                            _var_var_19,
                            _var_var_20,
                        )
                    )
                ),
                "reason_codes": _var_var_65,
            },
            "demand_available": _var_var_18 is not None,
            "demand": {
                "available": _var_var_18 is not None,
                "reason_codes": _var_var_66,
            },
            "plan_available": _var_var_29,
            "pv_allocation_available": _var_var_31,
            "connected": _var_var_19,
            "charging": _var_var_20,
            "current_power_kw": _var_var_21,
            "energy_today_kwh": _var_var_22,
            "current_soc_percent": (
                round(_var_var_3, 1) if _var_var_3 is not None else None
            ),
            "target_soc_percent": round(_var_var_5, 1),
            "battery_capacity_kwh": round(_var_var_4, 1),
            "demand_source": _var_var_12,
            "demand_confidence_percent": _var_var_14,
            "battery_energy_demand_kwh": (
                round(_var_var_13, 2) if _var_var_13 is not None else None
            ),
            "requested_energy_kwh": (
                round(_var_var_6, 2) if _var_var_6 is not None else None
            ),
            "planned_distance_km": (
                round(_var_var_7, 1) if _var_var_7 is not None else None
            ),
            "consumption_kwh_per_100km": round(_var_var_8, 1),
            "charging_efficiency_percent": round(_var_var_9),
            "max_charging_power_kw": round(_var_var_10, 1),
            "required_energy_kwh": (
                round(_var_var_18, 2) if _var_var_18 is not None else None
            ),
            "departure_time": _var_var_11.isoformat(),
            **_var_var_27,
            "recommended_action": _var_var_56,
            "recommendation_reason": _var_var_1,
            "recommendation_explanation": _var_var_64,
            "recommendation_confidence_percent": _var_var_63,
            "forecast_uncertainty_percent": _var_var_45,
            "recommended_start": (
                _var_var_49.isoformat() if _var_var_29 and _var_var_49 else None
            ),
            "recommended_end": (
                _var_var_50.isoformat() if _var_var_29 and _var_var_50 else None
            ),
            "recommended_energy_kwh": round(_var_var_34, 2) if _var_var_29 else None,
            "expected_pv_energy_kwh": round(_var_var_35, 2) if _var_var_31 else None,
            "expected_pv_energy_lower_kwh": (
                round(_var_var_42, 2) if _var_var_31 else None
            ),
            "expected_pv_energy_upper_kwh": (
                round(_var_var_43, 2) if _var_var_31 else None
            ),
            "expected_grid_energy_kwh": round(_var_var_36, 2) if _var_var_29 else None,
            "household_grid_import_kwh": (
                round(_var_var_37, 2) if _var_var_37 is not None else None
            ),
            "heat_pump_grid_import_kwh": (
                round(_var_var_38, 2) if _var_var_38 is not None else None
            ),
            "expected_grid_export_kwh": (
                round(_var_var_39, 2) if _var_var_39 is not None else None
            ),
            "expected_pv_share_percent": _var_var_46,
            "departure_readiness_percent": _var_var_47,
            "departure_risk": _var_var_48,
            "charging_window_active": _var_var_52,
            "pv_charging_recommended": _var_var_30 and _var_var_52 and _var_var_53,
            "residual_pv_window_active": _var_var_30 and _var_var_54,
            "low_price_charging_recommended": _var_var_52 and _var_var_55,
            "estimated_immediate_cost_eur": _var_var_57,
            "estimated_advised_cost_eur": _var_var_58,
            "estimated_cost_advantage_eur": _var_var_59,
            "electricity_price_ct_per_kwh": _var_var_23,
            "feed_in_tariff_ct_per_kwh": _var_var_24,
            "data_quality_percent": _var_var_62,
            "energy_context_quality_percent": _var_var_61,
            "allocation_origin": "stats_household_then_heat_pump_then_battery_then_calibration_reserve_then_wallbox_then_grid_export",
            "allocation_provenance": {
                "producer": "solar_forecast_eai",
                "context_provider": "sfml_stats",
                "method": "interval_priority_with_departure_window",
                "priority_order": [
                    "household_base_load",
                    "heat_pump",
                    "battery_reserve",
                    "calibration_reserve",
                    "wallbox",
                    "grid_export",
                ],
            },
            "allocation_valid": _var_var_30,
            "allocation_status": _var_var_32,
            "advisory_only": True,
            "control_services_called": False,
            "hours": _var_var_33,
        }
