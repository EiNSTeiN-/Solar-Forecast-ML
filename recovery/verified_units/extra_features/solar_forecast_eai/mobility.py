# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Advisory wallbox planning on residual PV after heat-pump demand."""
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


def _number(value: Any) -> float | None:
    try:
        parsed_number = float(value)
    except (TypeError, ValueError):
        return
    return parsed_number if isfinite(parsed_number) else None


_ALLOCATION_STATUSES = frozenset(
    {
        "valid",
        "closed",
        "balanced",
        "complete",
        "balance_mismatch",
        "forecast_data_incomplete",
        "energy_context_incomplete",
        "energy_context_unavailable",
    }
)


def _allocation_status(value: Any) -> str:
    return (
        value if isinstance(value, str) and value in _ALLOCATION_STATUSES else "unknown"
    )


class EAIMobilityPlanner:
    """Create a read-only charging plan without invoking actuator services."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass

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
        demand_mode = config.get(CONF_EV_DEMAND_MODE)
        vehicle_soc_percent = (
            self._state_measurement(
                config.get(CONF_EV_SOC_ENTITY), now, "vehicle_soc", {"%": 1.0}, 21600
            )
            if demand_mode in (None, "soc")
            else None
        )
        if vehicle_soc_percent is not None:
            if not 0 <= vehicle_soc_percent <= 100:
                vehicle_soc_percent = None
        battery_capacity_kwh = self._bounded(
            config.get(CONF_EV_BATTERY_CAPACITY_KWH, DEFAULT_EV_BATTERY_CAPACITY_KWH),
            5,
            250,
        )
        target_soc_percent = self._bounded(
            config.get(CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC), 10, 100
        )
        requested_energy_kwh = None
        if demand_mode == "energy" or (
            demand_mode is None and vehicle_soc_percent is None
        ):
            requested_energy_kwh = self._state_measurement(
                config.get(CONF_EV_REQUIRED_ENERGY_ENTITY),
                now,
                "requested_energy",
                {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0},
                21600,
            )
        planned_distance_km = None
        if demand_mode == "distance" or (
            demand_mode is None
            and vehicle_soc_percent is None
            and (requested_energy_kwh is None)
        ):
            planned_distance_km = self._state_measurement(
                config.get(CONF_EV_PLANNED_DISTANCE_ENTITY),
                now,
                "planned_distance",
                {"km": 1.0, "mi": 1.609344},
                21600,
            )
        requested_energy_kwh = (
            requested_energy_kwh
            if requested_energy_kwh is not None and requested_energy_kwh >= 0
            else None
        )
        planned_distance_km = (
            planned_distance_km
            if planned_distance_km is not None and planned_distance_km >= 0
            else None
        )
        consumption_kwh_per_100km = self._bounded(
            config.get(
                CONF_EV_CONSUMPTION_KWH_PER_100KM, DEFAULT_EV_CONSUMPTION_KWH_PER_100KM
            ),
            5,
            60,
        )
        charging_efficiency_percent = self._bounded(
            config.get(
                CONF_EV_CHARGING_EFFICIENCY_PERCENT,
                DEFAULT_EV_CHARGING_EFFICIENCY_PERCENT,
            ),
            50,
            100,
        )
        max_charging_power_kw = self._bounded(
            config.get(CONF_WALLBOX_MAX_POWER_KW, DEFAULT_WALLBOX_MAX_POWER_KW), 1, 50
        )
        departure_time = self._departure(
            config.get(CONF_EV_DEPARTURE_TIME, DEFAULT_EV_DEPARTURE_TIME), now
        )
        demand_source, battery_energy_demand_kwh, demand_confidence_percent = (
            self._demand(
                mode=demand_mode,
                soc=vehicle_soc_percent,
                target_soc=target_soc_percent,
                capacity=battery_capacity_kwh,
                requested_energy=requested_energy_kwh,
                planned_distance=planned_distance_km,
                consumption=consumption_kwh_per_100km,
            )
        )
        if demand_source is not None:
            active_demand_input = {
                "soc": "vehicle_soc",
                "energy": "requested_energy",
                "distance": "planned_distance",
            }[demand_source]
            demand_inputs = {"vehicle_soc", "planned_distance", "requested_energy"}
            inactive_demand_inputs = demand_inputs - {active_demand_input}
            self._missing_inputs.difference_update(inactive_demand_inputs)
            self._input_reasons = {
                reason_code
                for reason_code in self._input_reasons
                if not any(
                    (
                        reason_code == inactive_input
                        or reason_code.startswith(f"{inactive_input}_")
                        for inactive_input in inactive_demand_inputs
                    )
                )
            }
        required_energy_kwh = (
            battery_energy_demand_kwh / (charging_efficiency_percent / 100)
            if battery_energy_demand_kwh is not None
            else None
        )
        connected = self._state_bool(
            config.get(CONF_WALLBOX_CONNECTED_ENTITY), now, "wallbox_connected", 1800
        )
        charging = self._state_bool(
            config.get(CONF_WALLBOX_CHARGING_ENTITY), now, "wallbox_charging", 1800
        )
        wallbox_power_kw = self._state_measurement(
            config.get(CONF_WALLBOX_POWER_ENTITY),
            now,
            "wallbox_power",
            {"w": 0.001, "kw": 1.0, "mw": 1000.0},
            600,
        )
        energy_today_kwh = self._state_measurement(
            config.get(CONF_WALLBOX_ENERGY_TODAY_ENTITY),
            now,
            "wallbox_energy_today",
            {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0},
            21600,
        )
        electricity_price_ct = self._price_ct(
            config.get(CONF_ELECTRICITY_PRICE_ENTITY),
            config.get(CONF_ELECTRICITY_PRICE_UNIT),
        )
        feed_in_tariff_ct = self._price_ct(
            config.get(CONF_FEED_IN_TARIFF_ENTITY), config.get(CONF_FEED_IN_TARIFF_UNIT)
        )
        low_price_threshold_ct = self._bounded(
            config.get(CONF_LOW_PRICE_THRESHOLD_CT, DEFAULT_LOW_PRICE_THRESHOLD_CT),
            0,
            200,
        )
        eligible_intervals = self._eligible_hours(
            forecast.get("hours"), now, departure_time
        )
        planning_window = self._planning_window_contract(
            eligible_intervals, now, departure_time
        )
        target_reached = required_energy_kwh is not None and required_energy_kwh <= 0.1
        plan_available = bool(
            required_energy_kwh is not None
            and (
                target_reached
                or (eligible_intervals and planning_window["planning_window_complete"])
            )
        )
        allocations_valid = bool(eligible_intervals) and all(
            (interval["allocation_valid"] for interval in eligible_intervals)
        )
        pv_allocation_available = plan_available and allocations_valid
        allocation_status = (
            "valid"
            if allocations_valid
            else (
                "mixed"
                if any(
                    (interval["allocation_valid"] for interval in eligible_intervals)
                )
                else "invalid" if eligible_intervals else "no_intervals"
            )
        )
        allocated_intervals = self._allocate(
            eligible_intervals,
            required_energy_kwh,
            max_charging_power_kw,
            use_residual_pv=allocations_valid,
        )
        planned_charging_kwh = sum(
            (interval["wallbox_kwh"] for interval in allocated_intervals)
        )
        planned_pv_charging_kwh = sum(
            (interval["pv_wallbox_kwh"] for interval in allocated_intervals)
        )
        planned_grid_charging_kwh = sum(
            (interval["grid_wallbox_kwh"] for interval in allocated_intervals)
        )
        household_grid_import_kwh = (
            sum(
                (
                    interval["household_grid_import_kwh"]
                    for interval in allocated_intervals
                )
            )
            if allocations_valid
            else None
        )
        heat_pump_grid_import_kwh = (
            sum(
                (
                    interval["heat_pump_grid_import_kwh"]
                    for interval in allocated_intervals
                )
            )
            if allocations_valid
            else None
        )
        expected_grid_export_kwh = (
            sum(
                (
                    interval["expected_grid_export_kwh"]
                    for interval in allocated_intervals
                )
            )
            if allocations_valid
            else None
        )
        charging_intervals = [
            interval for interval in allocated_intervals if interval["wallbox_kwh"] > 0
        ]
        planned_pv_lower_kwh = sum(
            (
                (
                    min(interval["pv_wallbox_kwh"], interval["residual_pv_lower_kwh"])
                    if interval["residual_pv_lower_kwh"] is not None
                    else 0.0
                )
                for interval in charging_intervals
            )
        )
        planned_pv_upper_kwh = sum(
            (
                (
                    min(interval["pv_wallbox_kwh"], interval["residual_pv_upper_kwh"])
                    if interval["residual_pv_upper_kwh"] is not None
                    else 0.0
                )
                for interval in charging_intervals
            )
        )
        forecast_confidence_percent = (
            round(
                sum(
                    (
                        interval["forecast_confidence_percent"]
                        for interval in charging_intervals
                    )
                )
                / len(charging_intervals)
            )
            if charging_intervals
            else 0
        )
        forecast_uncertainty_percent = (
            round(
                sum(
                    (
                        interval["forecast_uncertainty_percent"]
                        for interval in charging_intervals
                    )
                )
                / len(charging_intervals)
            )
            if charging_intervals
            else 100
        )
        pv_share_percent = (
            round(planned_pv_charging_kwh / planned_charging_kwh * 100)
            if allocations_valid and planned_charging_kwh > 0
            else None
        )
        departure_readiness_percent = (
            round(min(100.0, planned_charging_kwh / required_energy_kwh * 100))
            if required_energy_kwh and required_energy_kwh > 0
            else 100 if required_energy_kwh is not None else 0
        )
        departure_risk = bool(
            required_energy_kwh is not None
            and required_energy_kwh > 0.1
            and (
                planned_charging_kwh + 0.05 < required_energy_kwh or connected is False
            )
        )
        charging_start = next(
            (
                self._interval_boundary(interval, "available_start")
                for interval in allocated_intervals
                if interval["wallbox_kwh"] > 0
            ),
            None,
        )
        charging_end = next(
            (
                self._interval_boundary(interval, "available_end")
                for interval in reversed(allocated_intervals)
                if interval["wallbox_kwh"] > 0
            ),
            None,
        )
        current_interval = next(
            (
                interval
                for interval in allocated_intervals
                if self._interval_boundary(interval, "available_start")
                <= now
                < self._interval_boundary(interval, "available_end")
            ),
            None,
        )
        current_charging_window = bool(
            current_interval and current_interval["wallbox_kwh"] > 0
        )
        current_pv_charging = bool(
            current_interval and current_interval["pv_wallbox_kwh"] > 0
        )
        current_residual_pv_window = bool(
            current_interval
            and current_interval["allocation_valid"]
            and (current_interval["residual_pv_kwh"] >= 0.5)
        )
        low_price = bool(
            electricity_price_ct is not None
            and electricity_price_ct <= low_price_threshold_ct
        )
        recommended_action, reason_code = self._recommendation(
            required=required_energy_kwh,
            connected=connected,
            departure_risk=departure_risk,
            current_window=current_charging_window,
            current_pv=current_pv_charging,
            low_price=low_price,
            planned_pv=pv_allocation_available and planned_pv_charging_kwh > 0,
        )
        if (
            required_energy_kwh is not None
            and required_energy_kwh > 0.1
            and eligible_intervals
            and (not plan_available)
        ):
            recommended_action = "data_unavailable"
            reason_code = "planning_window_incomplete"
            self._input_reasons.add(reason_code)
        immediate_cost_eur = (
            round(required_energy_kwh * electricity_price_ct / 100, 2)
            if required_energy_kwh is not None and electricity_price_ct is not None
            else None
        )
        advised_cost_eur = (
            round(
                planned_grid_charging_kwh * electricity_price_ct / 100
                + (planned_pv_charging_kwh if allocations_valid else 0.0)
                * (feed_in_tariff_ct or 0)
                / 100,
                2,
            )
            if electricity_price_ct is not None
            else None
        )
        cost_advantage_eur = (
            round(max(0.0, immediate_cost_eur - advised_cost_eur), 2)
            if immediate_cost_eur is not None and advised_cost_eur is not None
            else None
        )
        input_availability = (
            required_energy_kwh is not None,
            wallbox_power_kw is not None,
            connected is not None,
            bool(eligible_intervals),
        )
        energy_context_quality_percent = (
            round(
                sum(
                    (
                        interval["energy_context_quality_percent"]
                        for interval in eligible_intervals
                    )
                )
                / len(eligible_intervals)
            )
            if eligible_intervals
            else 0
        )
        data_quality_percent = round(
            (sum(input_availability) + energy_context_quality_percent / 100)
            / (len(input_availability) + 1)
            * 100
        )
        recommendation_confidence_percent = min(
            data_quality_percent,
            forecast_confidence_percent or data_quality_percent,
            demand_confidence_percent,
        )
        forecast_uncertainty_percent = max(
            forecast_uncertainty_percent, 100 - demand_confidence_percent
        )
        explanation = self._explanation(
            action=recommended_action,
            reason=reason_code,
            required=required_energy_kwh,
            pv_energy=planned_pv_charging_kwh if pv_allocation_available else None,
            pv_energy_lower=planned_pv_lower_kwh if pv_allocation_available else None,
            pv_energy_upper=planned_pv_upper_kwh if pv_allocation_available else None,
            target_soc=target_soc_percent,
            demand_source=demand_source,
            battery_energy=battery_energy_demand_kwh,
            planned_distance=planned_distance_km,
            consumption=consumption_kwh_per_100km,
            charging_efficiency=charging_efficiency_percent,
            departure=departure_time,
            price_ct=electricity_price_ct,
            confidence=recommendation_confidence_percent,
            uncertainty=forecast_uncertainty_percent,
        )
        if not eligible_intervals and (not target_reached):
            self._input_reasons.add("forecast_plan_unavailable")
            self._missing_inputs.add("forecast_plan")
        if not allocations_valid:
            if eligible_intervals:
                self._input_reasons.add("pv_allocation_invalid")
        telemetry_reason_codes = sorted(
            (
                reason_code
                for reason_code in self._input_reasons
                if reason_code.startswith("wallbox_")
            )
        )
        demand_reason_codes = sorted(
            (
                reason_code
                for reason_code in self._input_reasons
                if reason_code.startswith(
                    ("vehicle_soc_", "requested_energy_", "planned_distance_")
                )
            )
        )
        return {
            "available": plan_available,
            "configured": True,
            "reason": (
                None
                if plan_available
                else (
                    reason_code
                    if reason_code == "planning_window_incomplete"
                    else "data_unavailable"
                )
            ),
            "reason_codes": sorted(self._input_reasons),
            "missing_inputs": sorted(self._missing_inputs),
            "telemetry_available": any(
                (
                    numeric_value is not None
                    for numeric_value in (
                        wallbox_power_kw,
                        energy_today_kwh,
                        connected,
                        charging,
                    )
                )
            ),
            "telemetry": {
                "available": any(
                    (
                        numeric_value is not None
                        for numeric_value in (
                            wallbox_power_kw,
                            energy_today_kwh,
                            connected,
                            charging,
                        )
                    )
                ),
                "reason_codes": telemetry_reason_codes,
            },
            "demand_available": required_energy_kwh is not None,
            "demand": {
                "available": required_energy_kwh is not None,
                "reason_codes": demand_reason_codes,
            },
            "plan_available": plan_available,
            "pv_allocation_available": pv_allocation_available,
            "connected": connected,
            "charging": charging,
            "current_power_kw": wallbox_power_kw,
            "energy_today_kwh": energy_today_kwh,
            "current_soc_percent": (
                round(vehicle_soc_percent, 1)
                if vehicle_soc_percent is not None
                else None
            ),
            "target_soc_percent": round(target_soc_percent, 1),
            "battery_capacity_kwh": round(battery_capacity_kwh, 1),
            "demand_source": demand_source,
            "demand_confidence_percent": demand_confidence_percent,
            "battery_energy_demand_kwh": (
                round(battery_energy_demand_kwh, 2)
                if battery_energy_demand_kwh is not None
                else None
            ),
            "requested_energy_kwh": (
                round(requested_energy_kwh, 2)
                if requested_energy_kwh is not None
                else None
            ),
            "planned_distance_km": (
                round(planned_distance_km, 1)
                if planned_distance_km is not None
                else None
            ),
            "consumption_kwh_per_100km": round(consumption_kwh_per_100km, 1),
            "charging_efficiency_percent": round(charging_efficiency_percent),
            "max_charging_power_kw": round(max_charging_power_kw, 1),
            "required_energy_kwh": (
                round(required_energy_kwh, 2)
                if required_energy_kwh is not None
                else None
            ),
            "departure_time": departure_time.isoformat(),
            **planning_window,
            "recommended_action": recommended_action,
            "recommendation_reason": reason_code,
            "recommendation_explanation": explanation,
            "recommendation_confidence_percent": recommendation_confidence_percent,
            "forecast_uncertainty_percent": forecast_uncertainty_percent,
            "recommended_start": (
                charging_start.isoformat()
                if plan_available and charging_start
                else None
            ),
            "recommended_end": (
                charging_end.isoformat() if plan_available and charging_end else None
            ),
            "recommended_energy_kwh": (
                round(planned_charging_kwh, 2) if plan_available else None
            ),
            "expected_pv_energy_kwh": (
                round(planned_pv_charging_kwh, 2) if pv_allocation_available else None
            ),
            "expected_pv_energy_lower_kwh": (
                round(planned_pv_lower_kwh, 2) if pv_allocation_available else None
            ),
            "expected_pv_energy_upper_kwh": (
                round(planned_pv_upper_kwh, 2) if pv_allocation_available else None
            ),
            "expected_grid_energy_kwh": (
                round(planned_grid_charging_kwh, 2) if plan_available else None
            ),
            "household_grid_import_kwh": (
                round(household_grid_import_kwh, 2)
                if household_grid_import_kwh is not None
                else None
            ),
            "heat_pump_grid_import_kwh": (
                round(heat_pump_grid_import_kwh, 2)
                if heat_pump_grid_import_kwh is not None
                else None
            ),
            "expected_grid_export_kwh": (
                round(expected_grid_export_kwh, 2)
                if expected_grid_export_kwh is not None
                else None
            ),
            "expected_pv_share_percent": pv_share_percent,
            "departure_readiness_percent": departure_readiness_percent,
            "departure_risk": departure_risk,
            "charging_window_active": current_charging_window,
            "pv_charging_recommended": allocations_valid
            and current_charging_window
            and current_pv_charging,
            "residual_pv_window_active": allocations_valid
            and current_residual_pv_window,
            "low_price_charging_recommended": current_charging_window and low_price,
            "estimated_immediate_cost_eur": immediate_cost_eur,
            "estimated_advised_cost_eur": advised_cost_eur,
            "estimated_cost_advantage_eur": cost_advantage_eur,
            "electricity_price_ct_per_kwh": electricity_price_ct,
            "feed_in_tariff_ct_per_kwh": feed_in_tariff_ct,
            "data_quality_percent": data_quality_percent,
            "energy_context_quality_percent": energy_context_quality_percent,
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
            "allocation_valid": allocations_valid,
            "allocation_status": allocation_status,
            "advisory_only": True,
            "control_services_called": False,
            "hours": allocated_intervals,
        }

    @staticmethod
    def _explanation(
        *,
        action: str,
        reason: str,
        required: float | None,
        pv_energy: float | None,
        pv_energy_lower: float | None,
        pv_energy_upper: float | None,
        target_soc: float,
        demand_source: str | None,
        battery_energy: float | None,
        planned_distance: float | None,
        consumption: float,
        charging_efficiency: float,
        departure: datetime,
        price_ct: float | None,
        confidence: int,
        uncertainty: int,
    ) -> dict[str, Any]:
        if action == "data_unavailable":
            headline = "Noch keine belastbare Ladeempfehlung"
            detail = "Ladebedarf oder Prognosedaten fehlen für einen begründeten Plan."
        elif action == "complete":
            headline = "Ladeziel bereits erreicht"
            detail = "Bis zur Abfahrt ist kein zusätzlicher Ladevorgang erforderlich."
        elif action == "connect":
            headline = "Fahrzeug jetzt verbinden"
            detail = "Das Fahrzeug muss verbunden sein, damit das Abfahrtsziel erreichbar bleibt."
        elif action == "charge" and reason == "pv_surplus":
            headline = "Jetzt verbleibenden PV-Überschuss nutzen"
            detail = "Haus, Wärmepumpe und Speicher sind berücksichtigt; der verbleibende PV-Anteil kann für das Fahrzeug genutzt werden."
        elif action == "charge" and reason == "low_price":
            headline = "Jetzt im günstigen Tarifzeitfenster laden"
            detail = (
                "Der aktuelle Strompreis liegt im konfigurierten Niedrigpreisfenster."
            )
        elif action == "charge":
            headline = "Jetzt laden, um das Abfahrtsziel zu sichern"
            detail = "Ein weiteres Verschieben würde die rechnerische Abfahrtsbereitschaft gefährden."
        elif reason == "pv_window_upcoming":
            headline = "Auf das nächste PV-Fenster warten"
            detail = "Ein späteres Zeitfenster bietet voraussichtlich mehr verbleibende PV-Energie."
        else:
            headline = "Laden bis zur Abfahrt einplanen"
            detail = "Der Ladebedarf wird auf die verfügbaren Stunden bis zur Abfahrt verteilt."
        calculation_steps = []
        if demand_source == "soc":
            calculation_steps.append(
                f"Aus Fahrzeug-Ladezustand und Ziel {target_soc:.0f} % berechnet"
            )
        elif demand_source == "energy" and battery_energy is not None:
            calculation_steps.append(
                f"Gewünschte zusätzliche Batterieenergie {battery_energy:.2f} kWh"
            )
        elif demand_source == "distance" and planned_distance is not None:
            calculation_steps.append(
                f"{planned_distance:.0f} km mit {consumption:.1f} kWh/100 km geplant"
            )
        if (
            pv_energy is not None
            and pv_energy_lower is not None
            and (pv_energy_upper is not None)
        ):
            calculation_steps.append(
                f"PV-Anteil {pv_energy:.2f} kWh, Prognoseband {pv_energy_lower:.2f}–{pv_energy_upper:.2f} kWh"
            )
        if required is not None:
            calculation_steps.insert(
                0,
                f"Benötigte Wallbox-Energie {required:.2f} kWh bei {charging_efficiency:.0f} % Ladeeffizienz",
            )
        if price_ct is not None:
            calculation_steps.append(f"Aktueller Strompreis {price_ct:.1f} ct/kWh")
        return {
            "headline": headline,
            "summary": detail,
            "evidence": calculation_steps,
            "confidence_percent": confidence,
            "uncertainty_percent": uncertainty,
            "reason": reason,
        }

    @staticmethod
    def _bounded(value: Any, minimum: float, maximum: float) -> float:
        parsed_number = _number(value)
        if parsed_number is None:
            return minimum
        return min(maximum, max(minimum, parsed_number))

    def _state_measurement(
        self,
        entity_id: str | None,
        now: datetime,
        input_name: str,
        unit_factors: dict[str, float],
        max_age_seconds: int,
    ) -> float | None:
        """Read a finite, fresh measurement without exposing entity identifiers."""
        entity_state = self._fresh_state(entity_id, now, input_name, max_age_seconds)
        if entity_state is None:
            return
        numeric_value = _number(entity_state.state)
        if numeric_value is None:
            self._input_reasons.add(f"{input_name}_invalid_value")
            return
        unit = (
            str((entity_state.attributes or {}).get("unit_of_measurement") or "")
            .strip()
            .lower()
        )
        unit_factor = unit_factors.get(unit)
        if unit_factor is None:
            self._input_reasons.add(f"{input_name}_invalid_unit")
            return
        return round(numeric_value * unit_factor, 6)

    def _fresh_state(
        self,
        entity_id: str | None,
        now: datetime,
        input_name: str,
        max_age_seconds: int,
    ) -> Any | None:
        if not entity_id:
            self._input_reasons.add(f"{input_name}_not_configured")
            self._missing_inputs.add(input_name)
            return
        entity_state = self.hass.states.get(entity_id)
        if entity_state is None:
            self._input_reasons.add(f"{input_name}_unavailable")
            self._missing_inputs.add(input_name)
            return
        reported_at = getattr(entity_state, "last_reported", None) or getattr(
            entity_state, "last_updated", None
        )
        if reported_at is None:
            return entity_state
        if not isinstance(reported_at, datetime):
            self._input_reasons.add(f"{input_name}_invalid_timestamp")
            return
        reported_at = (
            reported_at.replace(tzinfo=timezone.utc)
            if reported_at.tzinfo is None
            else reported_at.astimezone(timezone.utc)
        )
        now_utc = (
            now.replace(tzinfo=timezone.utc)
            if now.tzinfo is None
            else now.astimezone(timezone.utc)
        )
        age_seconds = (now_utc - reported_at).total_seconds()
        if age_seconds < -300:
            self._input_reasons.add(f"{input_name}_future_timestamp")
            return
        if age_seconds > max_age_seconds:
            self._input_reasons.add(f"{input_name}_stale")
            return
        return entity_state

    @staticmethod
    def _demand(
        *,
        mode: Any,
        soc: float | None,
        target_soc: float,
        capacity: float,
        requested_energy: float | None,
        planned_distance: float | None,
        consumption: float,
    ) -> tuple[str | None, float | None, int]:
        if mode in (None, "soc") and soc is not None:
            return ("soc", max(0.0, capacity * (target_soc - soc) / 100), 95)
        if mode in (None, "energy") and requested_energy is not None:
            return ("energy", requested_energy, 90)
        if mode in (None, "distance") and planned_distance is not None:
            return ("distance", planned_distance * consumption / 100, 75)
        return (None, None, 0)

    def _state_bool(
        self,
        entity_id: str | None,
        now: datetime,
        input_name: str,
        max_age_seconds: int,
    ) -> bool | None:
        entity_state = self._fresh_state(entity_id, now, input_name, max_age_seconds)
        if entity_state is None:
            return
        state_text = str(entity_state.state).strip().lower()
        if state_text in {"1", "on", "yes", "true", "charging", "connected"}:
            return True
        if state_text in {"0", "no", "off", "idle", "false", "disconnected"}:
            return False
        self._input_reasons.add(f"{input_name}_invalid_value")

    def _price_ct(
        self, entity_id: str | None, configured_unit: str | None = None
    ) -> float | None:
        if not entity_id or (entity_state := self.hass.states.get(entity_id)) is None:
            return None
        numeric_value = _number(entity_state.state)
        if numeric_value is None:
            return
        unit = str(
            (entity_state.attributes or {}).get("unit_of_measurement") or ""
        ).lower()
        price_is_eur = configured_unit == "eur_per_kwh" or (
            configured_unit in (None, "auto") and ("eur" in unit or "€/" in unit)
        )
        return (
            round(numeric_value * 100, 3) if price_is_eur else round(numeric_value, 3)
        )

    def _departure(self, value: Any, now: datetime) -> datetime:
        if isinstance(value, time):
            departure_clock = value
        else:
            try:
                departure_clock = time.fromisoformat(str(value))
            except ValueError:
                departure_clock = time.fromisoformat(DEFAULT_EV_DEPARTURE_TIME)
        time_zone_name = getattr(getattr(self.hass, "config", None), "time_zone", None)
        try:
            local_timezone = ZoneInfo(time_zone_name) if time_zone_name else now.tzinfo
        except ZoneInfoNotFoundError:
            local_timezone = now.tzinfo
        local_now = now.astimezone(local_timezone or timezone.utc)
        departure_timezone = local_timezone or timezone.utc
        now_utc = local_now.astimezone(timezone.utc)
        for day_offset in (0, 1):
            departure_date = local_now.date() + timedelta(days=day_offset)
            local_departure = datetime.combine(departure_date, departure_clock)
            timestamp_candidates = {
                candidate_datetime.astimezone(timezone.utc)
                for fold in (0, 1)
                if (
                    candidate_datetime := local_departure.replace(
                        tzinfo=departure_timezone, fold=fold
                    )
                )
                .astimezone(timezone.utc)
                .astimezone(departure_timezone)
                .replace(tzinfo=None)
                == local_departure
            }
            future_candidates = sorted(
                (
                    candidate_datetime
                    for candidate_datetime in timestamp_candidates
                    if candidate_datetime > now_utc
                )
            )
            if future_candidates:
                return future_candidates[0]
            if timestamp_candidates:
                continue
            gap_candidates = set()
            for fold in (0, 1):
                candidate_datetime = local_departure.replace(
                    tzinfo=departure_timezone, fold=fold
                ).astimezone(timezone.utc)
                roundtrip_local_datetime = candidate_datetime.astimezone(
                    departure_timezone
                ).replace(tzinfo=None)
                if roundtrip_local_datetime > local_departure:
                    gap_candidates.add(candidate_datetime)
            future_candidates = sorted(
                (
                    candidate_datetime
                    for candidate_datetime in gap_candidates
                    if candidate_datetime > now_utc
                )
            )
            if not future_candidates:
                continue
            return future_candidates[0]
        return now_utc + timedelta(days=1)

    def _timestamp(self, point: dict[str, Any]) -> datetime:
        timestamp = datetime.fromisoformat(
            str(point["timestamp"]).replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            local_timezone = self._local_timezone()
            timestamp_candidates = {
                candidate_datetime.astimezone(timezone.utc)
                for fold in (0, 1)
                if (
                    candidate_datetime := timestamp.replace(
                        tzinfo=local_timezone, fold=fold
                    )
                )
                .astimezone(timezone.utc)
                .astimezone(local_timezone)
                .replace(tzinfo=None)
                == timestamp
            }
            if len(timestamp_candidates) != 1:
                raise ValueError("ambiguous or nonexistent local forecast timestamp")
            timestamp = timestamp_candidates.pop()
        return timestamp.astimezone(timezone.utc)

    def _interval_boundary(self, point: dict[str, Any], key: str) -> datetime:
        return self._timestamp({"timestamp": point[key]})

    def _planning_window_contract(
        self, points: list[dict[str, Any]], start: datetime, end: datetime
    ) -> dict[str, Any]:
        window_start_utc = (
            start.replace(tzinfo=timezone.utc)
            if start.tzinfo is None
            else start.astimezone(timezone.utc)
        )
        window_end_utc = (
            end.replace(tzinfo=timezone.utc)
            if end.tzinfo is None
            else end.astimezone(timezone.utc)
        )
        horizon_seconds = max(0.0, (window_end_utc - window_start_utc).total_seconds())
        covered_seconds = sum(
            (
                (
                    self._interval_boundary(interval, "available_end")
                    - self._interval_boundary(interval, "available_start")
                ).total_seconds()
                for interval in points
            )
        )
        tolerance_seconds = 1.0
        contiguous = bool(points)
        if contiguous:
            first_interval_start = self._interval_boundary(points[0], "available_start")
            previous_interval_end = first_interval_start
            contiguous = (
                abs((first_interval_start - window_start_utc).total_seconds())
                <= tolerance_seconds
            )
            for interval in points:
                interval_start = self._interval_boundary(interval, "available_start")
                interval_end = self._interval_boundary(interval, "available_end")
                if (
                    interval_end <= interval_start
                    or abs((interval_start - previous_interval_end).total_seconds())
                    > tolerance_seconds
                ):
                    contiguous = False
                    break
                previous_interval_end = interval_end
            contiguous = bool(
                contiguous
                and abs((previous_interval_end - window_end_utc).total_seconds())
                <= tolerance_seconds
            )
        window_complete = bool(
            contiguous and abs(covered_seconds - horizon_seconds) <= tolerance_seconds
        )
        return {
            "planning_window_start": window_start_utc.isoformat(),
            "planning_window_end": window_end_utc.isoformat(),
            "planning_horizon_hours": round(horizon_seconds / 3600, 6),
            "planning_covered_hours": round(covered_seconds / 3600, 6),
            "planning_intervals_contiguous": contiguous,
            "planning_window_complete": window_complete,
        }

    def _local_timezone(self):
        time_zone_name = getattr(getattr(self.hass, "config", None), "time_zone", None)
        try:
            return ZoneInfo(time_zone_name) if time_zone_name else timezone.utc
        except ZoneInfoNotFoundError:
            return timezone.utc

    def _eligible_hours(
        self, hours: Any, now: datetime, departure: datetime
    ) -> list[dict[str, Any]]:
        if not isinstance(hours, list):
            return []
        indexed_intervals = []
        for source_index, interval_record in enumerate(hours):
            if isinstance(interval_record, dict):
                start_value = interval_record.get(
                    "interval_start", interval_record.get("timestamp")
                )
                if not start_value:
                    continue
                try:
                    source_interval_start = self._timestamp({"timestamp": start_value})
                except ValueError:
                    continue
                indexed_intervals.append(
                    (source_interval_start, source_index, interval_record)
                )
        indexed_intervals.sort(key=lambda item: item[0])
        eligible_intervals = []
        for sorted_index, (
            source_interval_start,
            source_index,
            interval_record,
        ) in enumerate(indexed_intervals):
            source_interval_end = None
            invalid_end = False
            for end_key in ("interval_end", "end_timestamp", "end"):
                if not interval_record.get(end_key):
                    continue
                try:
                    source_interval_end = self._timestamp(
                        {"timestamp": interval_record[end_key]}
                    )
                except (TypeError, ValueError):
                    invalid_end = True
                break
            if invalid_end:
                continue
            if (
                source_interval_end is None
                and sorted_index + 1 < len(indexed_intervals)
                and (indexed_intervals[sorted_index + 1][1] == source_index + 1)
            ):
                source_interval_end = indexed_intervals[sorted_index + 1][0]
            if source_interval_end is None:
                source_interval_end = source_interval_start + timedelta(hours=1)
            if source_interval_end <= source_interval_start:
                continue
            available_start = max(source_interval_start, now)
            available_end = min(source_interval_end, departure)
            if available_end <= available_start:
                continue
            source_hours = (
                source_interval_end - source_interval_start
            ).total_seconds() / 3600
            available_hours = (available_end - available_start).total_seconds() / 3600
            availability_fraction = available_hours / source_hours

            def energy(key: str) -> float | None:
                numeric_value = _number(interval_record.get(key))
                return (
                    numeric_value
                    if numeric_value is not None and numeric_value >= 0
                    else None
                )

            def scaled(value: float | None) -> float | None:
                return value * availability_fraction if value is not None else None

            pv_forecast_kwh = energy("pv_forecast_kwh")
            forecast_power_kw = _number(interval_record.get("forecast_kw"))
            if forecast_power_kw is not None and forecast_power_kw < 0:
                forecast_power_kw = None
            heat_pump_kwh = _number(interval_record.get("heat_pump_kwh"))
            if heat_pump_kwh is not None and heat_pump_kwh < 0:
                heat_pump_kwh = None
            if heat_pump_kwh is None and forecast_power_kw is not None:
                heat_pump_kwh = forecast_power_kw * source_hours
            household_base_load_kwh = energy("household_base_load_kwh")
            household_pv_kwh = energy("household_pv_kwh")
            heat_pump_pv_kwh = energy("heat_pump_pv_kwh")
            battery_reserve_kwh = energy("battery_reserve_kwh")
            battery_pv_reserve_kwh = energy("battery_pv_reserve_kwh")
            if battery_pv_reserve_kwh is None:
                battery_pv_reserve_kwh = energy("battery_pv_reserved_kwh")
            calibration_reserve_kwh = energy("pv_calibration_reserve_kwh")
            wallbox_pv_available_kwh = energy("wallbox_pv_available_kwh")
            unallocated_or_export_kwh = energy("unallocated_or_export_kwh")
            pv_balance_error_kwh = _number(interval_record.get("pv_balance_error_kwh"))
            pv_balance_tolerance_kwh = energy("pv_balance_tolerance_kwh")
            allocation_status = _allocation_status(
                interval_record.get("allocation_status")
            )
            pv_components = (
                pv_forecast_kwh,
                household_pv_kwh,
                heat_pump_pv_kwh,
                battery_pv_reserve_kwh,
                calibration_reserve_kwh,
                wallbox_pv_available_kwh,
                unallocated_or_export_kwh,
            )
            computed_balance_error_kwh = (
                pv_forecast_kwh - sum(pv_components[1:])
                if all((numeric_value is not None for numeric_value in pv_components))
                else None
            )
            allocations_within_demand = bool(
                pv_balance_tolerance_kwh is not None
                and household_base_load_kwh is not None
                and (household_pv_kwh is not None)
                and (
                    household_pv_kwh
                    <= household_base_load_kwh + pv_balance_tolerance_kwh
                )
                and (heat_pump_kwh is not None)
                and (heat_pump_pv_kwh is not None)
                and (heat_pump_pv_kwh <= heat_pump_kwh + pv_balance_tolerance_kwh)
                and (battery_reserve_kwh is not None)
                and (battery_pv_reserve_kwh is not None)
                and (
                    battery_pv_reserve_kwh
                    <= battery_reserve_kwh + pv_balance_tolerance_kwh
                )
            )
            allocation_valid = bool(
                interval_record.get("allocation_valid") is True
                and allocation_status in {"valid", "closed", "balanced", "complete"}
                and (heat_pump_kwh is not None)
                and (household_base_load_kwh is not None)
                and (battery_reserve_kwh is not None)
                and (pv_balance_error_kwh is not None)
                and (pv_balance_tolerance_kwh is not None)
                and (computed_balance_error_kwh is not None)
                and (
                    abs(computed_balance_error_kwh - pv_balance_error_kwh)
                    <= pv_balance_tolerance_kwh
                )
                and (abs(pv_balance_error_kwh) <= pv_balance_tolerance_kwh)
                and allocations_within_demand
            )
            household_grid_import_kwh = (
                max(0.0, household_base_load_kwh - household_pv_kwh)
                if allocation_valid
                else None
            )
            heat_pump_grid_import_kwh = (
                max(0.0, heat_pump_kwh - heat_pump_pv_kwh) if allocation_valid else None
            )
            lower_power_kw = _number(interval_record.get("lower_kw"))
            upper_power_kw = _number(interval_record.get("upper_kw"))
            if lower_power_kw is None:
                lower_power_kw = forecast_power_kw
            if upper_power_kw is None:
                upper_power_kw = forecast_power_kw
            residual_pv_lower_kwh = _number(
                interval_record.get("residual_pv_lower_kwh")
            )
            residual_pv_upper_kwh = _number(
                interval_record.get("residual_pv_upper_kwh")
            )
            if (
                allocation_valid
                and residual_pv_lower_kwh is None
                and (upper_power_kw is not None)
            ):
                residual_pv_lower_kwh = max(
                    0.0,
                    wallbox_pv_available_kwh
                    - max(
                        0.0,
                        (upper_power_kw - (forecast_power_kw or 0.0)) * source_hours,
                    ),
                )
            if (
                allocation_valid
                and residual_pv_upper_kwh is None
                and (lower_power_kw is not None)
            ):
                residual_pv_upper_kwh = wallbox_pv_available_kwh + max(
                    0.0, ((forecast_power_kw or 0.0) - lower_power_kw) * source_hours
                )
            if allocation_valid and residual_pv_lower_kwh is None:
                residual_pv_lower_kwh = wallbox_pv_available_kwh
            if allocation_valid and residual_pv_upper_kwh is None:
                residual_pv_upper_kwh = wallbox_pv_available_kwh
            if residual_pv_lower_kwh is not None and residual_pv_lower_kwh < 0:
                residual_pv_lower_kwh = None
            if residual_pv_upper_kwh is not None and residual_pv_upper_kwh < 0:
                residual_pv_upper_kwh = None
            forecast_uncertainty_percent = _number(
                interval_record.get("uncertainty_percent")
            )
            if forecast_uncertainty_percent is None:
                forecast_uncertainty_percent = (
                    (upper_power_kw - lower_power_kw) / (2 * forecast_power_kw) * 100
                    if forecast_power_kw is not None
                    and forecast_power_kw > 0
                    and (lower_power_kw is not None)
                    and (upper_power_kw is not None)
                    else 30.0
                )
            forecast_uncertainty_percent = min(
                100.0, max(0.0, forecast_uncertainty_percent)
            )
            forecast_confidence_percent = _number(
                interval_record.get("confidence_percent")
            )
            if forecast_confidence_percent is None:
                forecast_confidence_percent = max(
                    0.0, 100.0 - forecast_uncertainty_percent
                )
            eligible_intervals.append(
                {
                    "timestamp": available_start.isoformat(),
                    "interval_start": available_start.isoformat(),
                    "interval_end": available_end.isoformat(),
                    "interval_hours": available_hours,
                    "source_interval_start": source_interval_start.isoformat(),
                    "source_interval_end": source_interval_end.isoformat(),
                    "source_interval_hours": source_hours,
                    "available_start": available_start.isoformat(),
                    "available_end": available_end.isoformat(),
                    "available_hours": available_hours,
                    "pv_forecast_kwh": scaled(pv_forecast_kwh),
                    "household_base_load_kwh": scaled(household_base_load_kwh),
                    "household_pv_kwh": scaled(household_pv_kwh),
                    "household_grid_import_kwh": scaled(household_grid_import_kwh),
                    "heat_pump_kwh": scaled(heat_pump_kwh),
                    "heat_pump_pv_kwh": scaled(heat_pump_pv_kwh),
                    "heat_pump_grid_import_kwh": scaled(heat_pump_grid_import_kwh),
                    "battery_reserve_kwh": scaled(battery_reserve_kwh),
                    "battery_pv_reserve_kwh": scaled(battery_pv_reserve_kwh),
                    "battery_pv_reserved_kwh": scaled(battery_pv_reserve_kwh),
                    "pv_calibration_reserve_kwh": scaled(calibration_reserve_kwh),
                    "wallbox_pv_available_kwh": scaled(wallbox_pv_available_kwh),
                    "unallocated_or_export_kwh": scaled(unallocated_or_export_kwh),
                    "expected_grid_export_kwh": scaled(unallocated_or_export_kwh),
                    "pv_balance_error_kwh": scaled(pv_balance_error_kwh),
                    "pv_balance_tolerance_kwh": scaled(pv_balance_tolerance_kwh),
                    "energy_context_quality_percent": round(
                        max(
                            0.0,
                            _number(
                                interval_record.get("energy_context_quality_percent")
                            )
                            or 0.0,
                        )
                    ),
                    "allocation_valid": allocation_valid,
                    "allocation_status": allocation_status,
                    "residual_pv_kwh": scaled(wallbox_pv_available_kwh),
                    "residual_pv_lower_kwh": scaled(residual_pv_lower_kwh),
                    "residual_pv_upper_kwh": scaled(residual_pv_upper_kwh),
                    "forecast_confidence_percent": round(
                        min(100.0, max(0.0, forecast_confidence_percent))
                    ),
                    "forecast_uncertainty_percent": round(forecast_uncertainty_percent),
                    "wallbox_kwh": 0.0,
                    "pv_wallbox_kwh": 0.0,
                    "grid_wallbox_kwh": 0.0,
                }
            )
            continue
        return sorted(eligible_intervals, key=lambda point: point["timestamp"])

    @staticmethod
    def _allocate(
        points: list[dict[str, Any]],
        required: float | None,
        max_power: float,
        *,
        use_residual_pv: bool = True,
    ) -> list[dict[str, Any]]:
        allocated_intervals = [dict(interval) for interval in points]
        if required is None or required <= 0:
            for interval in allocated_intervals:
                interval["expected_grid_export_kwh"] = (
                    interval["unallocated_or_export_kwh"] + interval["residual_pv_kwh"]
                    if use_residual_pv and interval["allocation_valid"]
                    else None
                )
            return allocated_intervals
        remaining_energy_kwh = required
        if use_residual_pv:
            for interval in sorted(
                allocated_intervals,
                key=lambda item: item["residual_pv_kwh"],
                reverse=True,
            ):
                interval_capacity_kwh = max_power * interval["available_hours"]
                allocated_energy_kwh = min(
                    remaining_energy_kwh,
                    interval_capacity_kwh,
                    interval["residual_pv_kwh"],
                )
                interval["wallbox_kwh"] = allocated_energy_kwh
                interval["pv_wallbox_kwh"] = allocated_energy_kwh
                remaining_energy_kwh -= allocated_energy_kwh
                if remaining_energy_kwh <= 0.001:
                    break
        if remaining_energy_kwh > 0.001:
            for interval in allocated_intervals:
                remaining_interval_capacity_kwh = (
                    max_power * interval["available_hours"] - interval["wallbox_kwh"]
                )
                allocated_energy_kwh = min(
                    remaining_energy_kwh, remaining_interval_capacity_kwh
                )
                interval["wallbox_kwh"] += allocated_energy_kwh
                interval["grid_wallbox_kwh"] = allocated_energy_kwh
                remaining_energy_kwh -= allocated_energy_kwh
                if remaining_energy_kwh <= 0.001:
                    break
        for interval in allocated_intervals:
            interval["expected_grid_export_kwh"] = (
                interval["unallocated_or_export_kwh"]
                + max(0.0, interval["residual_pv_kwh"] - interval["pv_wallbox_kwh"])
                if use_residual_pv and interval["allocation_valid"]
                else None
            )
        return allocated_intervals

    @staticmethod
    def _recommendation(
        *,
        required: float | None,
        connected: bool | None,
        departure_risk: bool,
        current_window: bool,
        current_pv: bool,
        low_price: bool,
        planned_pv: bool,
    ) -> tuple[str, str]:
        if required is None:
            return (
                (
                    (("data_unavailable", "data_unavailable") if True else None)
                    if True
                    else None
                )
                if True
                else None
            )
        if required <= 0.1:
            return (
                ((("complete", "target_reached") if True else None) if True else None)
                if True
                else None
            )
        if connected is False:
            return (
                (
                    (("connect", "departure_deadline") if True else None)
                    if True
                    else None
                )
                if True
                else None
            )
        if departure_risk:
            return (
                ((("charge", "departure_deadline") if True else None) if True else None)
                if True
                else None
            )
        if current_window:
            if current_pv:
                return (
                    ((("charge", "pv_surplus") if True else None) if True else None)
                    if True
                    else None
                )
            return (
                (
                    (
                        ("charge", "low_price" if low_price else "deadline_plan")
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        return (
            (
                (
                    ("defer", "pv_window_upcoming" if planned_pv else "deadline_plan")
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
