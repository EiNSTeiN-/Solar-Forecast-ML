# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Realistic, deterministic EAI preview scenarios."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from math import cos, pi, sin
from typing import Any

SECTIONS = (
    "overview",
    "forecast",
    "operation",
    "efficiency",
    "building",
    "energy",
    "mobility",
    "weather",
    "diagnostics",
)
_MOCK_PRICES = [
    31.2,
    29.8,
    28.6,
    27.9,
    29.4,
    34.8,
    41.6,
    45.2,
    42.8,
    35.4,
    27.2,
    21.8,
    18.6,
    17.9,
    20.4,
    26.8,
    34.2,
    43.7,
    49.1,
    46.3,
    40.8,
    36.1,
    33.4,
    32.0,
]


def _forecast() -> list[dict[str, Any]]:
    forecast_start = datetime(2026, 1, 15, tzinfo=timezone.utc)
    forecast_hours = []
    for hour_offset in range(72):
        valid_at = forecast_start + timedelta(hours=hour_offset)
        outdoor_c = round(1.5 + 5.5 * sin((valid_at.hour - 8) * pi / 12), 1)
        heat_pump_forecast_kw = round(
            max(
                0.4,
                2.2 - outdoor_c * 0.13 + (0.8 if valid_at.hour in {5, 6, 18} else 0),
            ),
            2,
        )
        actual_kw = (
            round(heat_pump_forecast_kw * (0.94 + 0.05 * cos(hour_offset / 3)), 2)
            if hour_offset < 18
            else None
        )
        uncertainty_percent = min(34, 14 + hour_offset // 4)
        confidence_percent = max(62, 92 - hour_offset // 3)
        pv_forecast_kwh = round(max(0, 4.6 * sin((valid_at.hour - 6) * pi / 12)), 2)
        household_load_kwh = round(
            0.42 + (0.38 if valid_at.hour in {6, 7, 18, 19, 20} else 0.0), 2
        )
        household_pv_kwh = round(min(pv_forecast_kwh, household_load_kwh), 2)
        pv_after_household_kwh = max(0.0, pv_forecast_kwh - household_pv_kwh)
        heat_pump_pv_kwh = min(pv_after_household_kwh, heat_pump_forecast_kw)
        pv_after_heat_pump_kwh = max(
            0.0, pv_after_household_kwh - heat_pump_forecast_kw
        )
        battery_reserve_kwh = round(
            min(pv_after_heat_pump_kwh, 0.55 if 10 <= valid_at.hour <= 15 else 0.0), 2
        )
        pv_after_battery_kwh = max(0.0, pv_after_heat_pump_kwh - battery_reserve_kwh)
        wallbox_pv_available_kwh = round(pv_after_battery_kwh * 0.92, 2)
        calibration_reserve_kwh = round(
            pv_after_battery_kwh - wallbox_pv_available_kwh, 2
        )
        unallocated_pv_kwh = round(
            pv_forecast_kwh
            - household_pv_kwh
            - heat_pump_pv_kwh
            - battery_reserve_kwh
            - calibration_reserve_kwh
            - wallbox_pv_available_kwh,
            2,
        )
        forecast_hours.append(
            {
                "timestamp": valid_at.isoformat(),
                "interval_start": valid_at.isoformat(),
                "interval_end": (valid_at + timedelta(hours=1)).isoformat(),
                "outdoor_c": outdoor_c,
                "forecast_kw": heat_pump_forecast_kw,
                "lower_kw": round(
                    heat_pump_forecast_kw * (1 - uncertainty_percent / 100), 2
                ),
                "upper_kw": round(
                    heat_pump_forecast_kw * (1 + uncertainty_percent / 100), 2
                ),
                "uncertainty_percent": uncertainty_percent,
                "confidence_percent": confidence_percent,
                "uncertainty_drivers": [
                    "Demo-Modellqualität",
                    "Außentemperaturprognose",
                    "Prognosehorizont",
                ],
                "interval_method": "demo_quality_adjusted_prediction_interval",
                "actual_kw": actual_kw,
                "pv_forecast_kwh": pv_forecast_kwh,
                "household_base_load_kwh": household_load_kwh,
                "household_pv_kwh": household_pv_kwh,
                "household_grid_import_kwh": round(
                    max(0.0, household_load_kwh - household_pv_kwh), 2
                ),
                "pv_after_household_kwh": round(pv_after_household_kwh, 2),
                "heat_pump_kwh": heat_pump_forecast_kw,
                "heat_pump_pv_kwh": round(heat_pump_pv_kwh, 2),
                "heat_pump_grid_import_kwh": round(
                    max(0.0, heat_pump_forecast_kw - heat_pump_pv_kwh), 2
                ),
                "pv_after_heat_pump_kwh": round(pv_after_heat_pump_kwh, 2),
                "battery_reserve_kwh": battery_reserve_kwh,
                "battery_pv_reserved_kwh": battery_reserve_kwh,
                "battery_pv_reserve_kwh": battery_reserve_kwh,
                "pv_calibration_reserve_kwh": calibration_reserve_kwh,
                "wallbox_pv_available_kwh": wallbox_pv_available_kwh,
                "residual_pv_kwh": round(
                    wallbox_pv_available_kwh + unallocated_pv_kwh, 2
                ),
                "unallocated_or_export_kwh": unallocated_pv_kwh,
                "expected_grid_export_kwh": unallocated_pv_kwh,
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
                "historical_grid_export_kwh": round(wallbox_pv_available_kwh * 0.88, 2),
                "surplus_calibration_factor": 0.92,
                "energy_context_quality_percent": 92,
                "energy_context_origin": "stats_hourly_house_minus_large_consumers",
                "temperature_origin": "demo_weather_forecast",
                "mode": (
                    "dhw"
                    if valid_at.hour == 13
                    else "defrost" if hour_offset == 9 else "heating"
                ),
            }
        )
        continue
    return forecast_hours


_FORECAST_HOURS = _forecast()


def _allocate_mobility_hours(
    points: list[dict[str, Any]], required_kwh: float, max_power_kw: float
) -> list[dict[str, Any]]:
    allocated_points = [dict(point) for point in points]
    remaining_kwh = required_kwh
    for point in sorted(
        allocated_points, key=lambda item: item["residual_pv_kwh"], reverse=True
    ):
        interval_capacity_kwh = max_power_kw * point["available_hours"]
        allocated_kwh = min(
            remaining_kwh, interval_capacity_kwh, point["residual_pv_kwh"]
        )
        point["wallbox_kwh"] = allocated_kwh
        point["pv_wallbox_kwh"] = allocated_kwh
        remaining_kwh -= allocated_kwh
        if remaining_kwh <= 0.001:
            break
    if remaining_kwh > 0.001:
        for point in allocated_points:
            remaining_capacity_kwh = (
                max_power_kw * point["available_hours"] - point["wallbox_kwh"]
            )
            allocated_kwh = min(remaining_kwh, remaining_capacity_kwh)
            point["wallbox_kwh"] += allocated_kwh
            point["grid_wallbox_kwh"] = allocated_kwh
            remaining_kwh -= allocated_kwh
            if remaining_kwh <= 0.001:
                break
    for point in allocated_points:
        point["expected_grid_export_kwh"] = point["unallocated_or_export_kwh"] + max(
            0.0, point["residual_pv_kwh"] - point["pv_wallbox_kwh"]
        )
    return allocated_points


def _mobility_hours(forecast_hours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mobility_hours = []
    for point in forecast_hours[:31]:
        hour = datetime.fromisoformat(point["timestamp"]).hour
        residual_pv_kwh = point["wallbox_pv_available_kwh"]
        residual_pv_lower_kwh = (
            max(
                0.0,
                point["pv_forecast_kwh"]
                - point["household_base_load_kwh"]
                - point["upper_kw"]
                - point["battery_pv_reserved_kwh"],
            )
            * 0.92
        )
        residual_pv_upper_kwh = (
            max(
                0.0,
                point["pv_forecast_kwh"]
                - point["household_base_load_kwh"]
                - point["lower_kw"]
                - point["battery_pv_reserved_kwh"],
            )
            * 0.92
        )
        mobility_hours.append(
            {
                "timestamp": point["timestamp"],
                "interval_start": point["interval_start"],
                "interval_end": point["interval_end"],
                "source_interval_start": point["interval_start"],
                "source_interval_end": point["interval_end"],
                "source_interval_hours": point["interval_hours"],
                "available_start": point["interval_start"],
                "available_end": point["interval_end"],
                "pv_forecast_kwh": point["pv_forecast_kwh"],
                "household_base_load_kwh": point["household_base_load_kwh"],
                "household_pv_kwh": point["household_pv_kwh"],
                "household_grid_import_kwh": round(
                    point["household_base_load_kwh"] - point["household_pv_kwh"], 2
                ),
                "heat_pump_kwh": point["heat_pump_kwh"],
                "heat_pump_pv_kwh": point["heat_pump_pv_kwh"],
                "heat_pump_grid_import_kwh": round(
                    point["heat_pump_kwh"] - point["heat_pump_pv_kwh"], 2
                ),
                "battery_reserve_kwh": point["battery_reserve_kwh"],
                "battery_pv_reserve_kwh": point["battery_pv_reserve_kwh"],
                "pv_calibration_reserve_kwh": point["pv_calibration_reserve_kwh"],
                "wallbox_pv_available_kwh": round(residual_pv_kwh, 2),
                "unallocated_or_export_kwh": point["unallocated_or_export_kwh"],
                "expected_grid_export_kwh": point["unallocated_or_export_kwh"],
                "pv_balance_error_kwh": point["pv_balance_error_kwh"],
                "pv_balance_tolerance_kwh": point["pv_balance_tolerance_kwh"],
                "allocation_valid": True,
                "allocation_status": "balanced",
                "allocation_priority_order": point["allocation_priority_order"],
                "interval_hours": point["interval_hours"],
                "available_hours": point["available_hours"],
                "forecast_confidence_percent": point["confidence_percent"],
                "forecast_uncertainty_percent": point["uncertainty_percent"],
                "energy_context_quality_percent": point[
                    "energy_context_quality_percent"
                ],
                "price_ct_per_kwh": _MOCK_PRICES[hour],
                "residual_pv_lower_kwh": round(residual_pv_lower_kwh, 2),
                "residual_pv_upper_kwh": round(residual_pv_upper_kwh, 2),
                "wallbox_kwh": 0.0,
                "pv_wallbox_kwh": 0.0,
                "grid_wallbox_kwh": 0.0,
                "battery_pv_reserved_kwh": point["battery_pv_reserved_kwh"],
                "residual_pv_kwh": round(residual_pv_kwh, 2),
            }
        )
        continue
    return _allocate_mobility_hours(mobility_hours, 29.3, 11.0)


_ENERGY_HOURS = _FORECAST_HOURS[:24]
_MOBILITY_HOURS = _mobility_hours(_FORECAST_HOURS)
_MOBILITY_CHARGING_HOURS = [
    point for point in _MOBILITY_HOURS if point["wallbox_kwh"] > 0
]


def _energy_total(key: str) -> float:
    return round(sum((point[key] for point in _ENERGY_HOURS)), 2)


_FULL_DATA = {
    "overview": {
        "available": True,
        "current_power_kw": 1.84,
        "household_power_kw": 0.72,
        "solar_to_house_power_kw": 0.72,
        "battery_to_house_power_kw": 0.0,
        "grid_to_house_power_kw": 0.0,
        "energy_today_kwh": 11.7,
        "expected_today_kwh": 10.5,
        "expected_tomorrow_kwh": 9.8,
        "operation_mode": "heating",
        "data_quality_percent": 94,
        "pv_coverage_percent": 44,
        "expected_grid_import_kwh": 5.88,
        "health_score": 94,
        "why_now": {
            "headline": "Die Wärmepumpe nutzt gerade günstige PV-Energie",
            "explanation": "Demo-Erklärung aus Wärmebedarf, Speichertemperatur und PV-Prognose.",
            "evidence": [
                "Innen 20,6 °C bei Soll 21,0 °C",
                "PV-Überschuss 2,4 kW",
                "Warmwasser 46,8 °C",
            ],
            "confidence_percent": 91,
            "origin": "demo_rule_based_explanation",
        },
        "briefing": {
            "headline": "Heute 3,1 kWh Netzbezug vermeiden",
            "summary": "Das Gebäude hält Wärme lange. Das beste Ladefenster liegt zwischen 11:20 und 13:40 Uhr.",
            "actions": [
                "Warmwasser-Soll im PV-Fenster um 3 K anheben",
                "Nachtabsenkung heute 35 Minuten früher starten",
                "Außentemperatursensor wegen kurzer Datenlücke prüfen",
            ],
            "origin": "demo_energy_briefing",
        },
        "opportunities": [
            {"title": "PV-Wärmefenster", "value": "11:20–13:40", "saving_kwh": 3.1}
        ],
        "calculator_defaults": {
            "electricity_price_ct": 36.9,
            "feed_in_tariff_ct": 8.2,
            "annual_heat_demand_kwh": 12000,
            "heat_pump_pv_coverage_percent": 44,
            "tariff_mode": "Dynamischer Mock-Tarif",
            "tariff_source": "Mock-Tarifdaten",
            "hourly_prices_ct": _MOCK_PRICES,
        },
    },
    "forecast": {
        "available": True,
        "locked": False,
        "hours": _FORECAST_HOURS,
        "run_status": "ready",
        "quality": "demo_high",
        "model_origin": "demo_explainable_heat_demand_model",
        "main_drivers": ["Außentemperatur", "Warmwasserzyklus", "Gebäudeträgheit"],
        "uncertainty": {
            "available": True,
            "average_percent": 22,
            "confidence_percent": 80,
            "method": "demo_data_quality_model_source_temperature_and_horizon",
            "explanation": "Das Band wird mit Modellqualität, Außentemperatur und Prognosehorizont sichtbar breiter.",
            "lower_field": "lower_kw",
            "upper_field": "upper_kw",
        },
        "optimization": {
            "available": True,
            "start": "2026-01-15T11:20:00+00:00",
            "duration_minutes": 140,
            "pv_surplus_kwh": 3.1,
            "recommendation": "Warmwasser und Pufferspeicher in dieses PV-Fenster legen",
            "confidence_percent": 88,
            "uncertainty_percent": 16,
            "explanation": {
                "headline": "Warum genau dieses Zeitfenster?",
                "summary": "PV-Erzeugung übersteigt Hausgrundlast, Wärmepumpenbedarf und Speicherreserve gleichzeitig.",
                "evidence": [
                    "3,1 kWh verbleibender PV-Überschuss",
                    "Wärmepumpenbedarf 1,74 kWh, Band 1,46–2,02 kWh",
                    "Hausgrundlast und Speicherreserve bereits berücksichtigt",
                ],
                "confidence_percent": 88,
                "uncertainty_percent": 16,
            },
            "origin": "demo_pv_and_load_forecast",
        },
    },
    "operation": {
        "available": True,
        "current_power_kw": 1.84,
        "current_mode": "heating",
        "compressor_on": True,
        "runtime_hours": 1460.0,
        "runtime_heating_hours": 1250.0,
        "runtime_dhw_hours": 210.0,
        "runtime_origin": "heating_plus_dhw_counters",
        "runtime_scope": "lifetime",
        "starts": 1288,
        "starts_scope": "lifetime",
        "average_cycle_minutes": 68,
        "average_cycle_reason": None,
        "average_cycle_minutes_reason": None,
        "average_cycle_origin": "lifetime_runtime_hours_divided_by_lifetime_starts",
        "temperatures_c": {
            "outdoor": 2.8,
            "indoor": 21.1,
            "flow": 34.7,
            "return": 29.8,
        },
        "modes_minutes": {"heating": 421, "dhw": 54, "standby": 72, "defrost": 8},
        "sensor_coverage_percent": 92,
        "why_now": {
            "headline": "Komfortbedarf und PV-Überschuss fallen zusammen",
            "explanation": "Die Demo kombiniert aktuelle Temperaturen, Betriebszustand und Prognose.",
            "evidence": [
                "0,4 K unter Soll",
                "PV-Erzeugung steigt",
                "Langer effizienter Takt",
            ],
            "confidence_percent": 91,
            "origin": "demo_rule_based_explanation",
        },
    },
    "efficiency": {
        "available": True,
        "electric_kwh": 11.7,
        "thermal_kwh": 42.1,
        "daily_work_factor": 3.6,
        "reported_jaz": 3.8,
        "work_factor_origin": "configured_jaz_sensor",
        "cop": 3.6,
        "electric_power_kw": 1.84,
        "thermal_power_kw": 6.62,
        "cop_origin": "demo_synchronous_water_loop",
        "flow_c": 34.7,
        "return_c": 29.8,
        "volume_flow_l_min": 19.4,
        "note": "Der Demo-COP zeigt die Berechnung aus synchronen Leistungswerten.",
        "heating_share_percent": 82,
        "dhw_share_percent": 18,
    },
    "building": {
        "available": True,
        "locked": False,
        "indoor_c": 21.1,
        "outdoor_c": 2.8,
        "comfort_delta_c": 0.1,
        "thermal_inertia_hours": 6.4,
        "heat_loss_kw": 4.8,
        "heating_range_kw": [4.2, 5.6],
        "uncertainty_percent": 18,
        "target_c": 21.0,
        "heat_loss_origin": "demo_steady_state_model",
        "learning_progress_percent": 86,
        "temperature_spread_c": 18.2,
        "source_temperature_c": 6.1,
        "storage_temperature_c": 46.8,
        "thermal_loss": {
            "available": True,
            "configured": True,
            "status": "ready",
            "storage_volume_l": 300.0,
            "storage_temperature_c": 46.8,
            "ambient_temperature_c": 18.7,
            "circulation_return_temperature_c": 37.4,
            "standby_loss_coefficient_w_k": 2.15,
            "standby_loss_kwh_day": 1.45,
            "circulation_loss_kwh_day": 0.62,
            "forecast_thermal_loss_kwh_24h": 2.07,
            "data_quality_percent": 88,
            "passive_cooling_intervals": 34,
            "outdoor_response_factor": 0.12,
            "explanation": "Demo-Schätzung aus passiven Abkühlphasen; die Zirkulation wird nur bei beobachtetem Pumpenbetrieb getrennt ausgewiesen.",
            "evidence": [
                "34 geeignete Mock-Abkühlintervalle",
                "Temperaturdifferenz Speicher/Heizraum 28,1 K",
                "Zirkulationszustand im Mock-Datensatz berücksichtigt",
            ],
            "origin": "demo_empirical_passive_cooling_model",
        },
    },
    "energy": {
        "available": True,
        "locked": False,
        "pv_forecast_kwh": _energy_total("pv_forecast_kwh"),
        "heat_pump_kwh": _energy_total("heat_pump_kwh"),
        "household_base_load_kwh": _energy_total("household_base_load_kwh"),
        "household_power_kw": 0.72,
        "solar_to_house_power_kw": 0.72,
        "battery_to_house_power_kw": 0.0,
        "grid_to_house_power_kw": 0.0,
        "household_pv_kwh": _energy_total("household_pv_kwh"),
        "household_grid_import_kwh": _energy_total("household_grid_import_kwh"),
        "heat_pump_pv_kwh": _energy_total("heat_pump_pv_kwh"),
        "heat_pump_grid_import_kwh": _energy_total("heat_pump_grid_import_kwh"),
        "battery_reserve_kwh": _energy_total("battery_reserve_kwh"),
        "battery_pv_reserve_kwh": _energy_total("battery_pv_reserve_kwh"),
        "pv_calibration_reserve_kwh": _energy_total("pv_calibration_reserve_kwh"),
        "wallbox_pv_available_kwh": _energy_total("wallbox_pv_available_kwh"),
        "residual_pv_kwh": _energy_total("residual_pv_kwh"),
        "unallocated_or_export_kwh": _energy_total("unallocated_or_export_kwh"),
        "expected_grid_export_kwh": _energy_total("expected_grid_export_kwh"),
        "pv_balance_error_kwh": _energy_total("pv_balance_error_kwh"),
        "pv_balance_tolerance_kwh": 0.02,
        "pv_balance_difference_kwh": _energy_total("pv_balance_error_kwh"),
        "allocation_valid": True,
        "allocation_status": "balanced",
        "forecast_interval_start": "2026-01-15T00:00:00+00:00",
        "forecast_period_start": "2026-01-15T00:00:00+00:00",
        "forecast_last_interval_start": "2026-01-15T23:00:00+00:00",
        "forecast_interval_end": "2026-01-16T00:00:00+00:00",
        "forecast_interval_hours": 24.0,
        "forecast_intervals_contiguous": True,
        "forecast_covers_24h": True,
        "forecast_period_status": "complete_24h",
        "forecast_interval_count": 24,
        "allocation_method": "hourly_priority_with_conservative_surplus_calibration",
        "allocation_provenance": {
            "producer": "solar_forecast_eai",
            "context_provider": "sfml_stats",
            "context_version": 1,
            "base_load_origin": "stats_hourly_house_minus_large_consumers",
            "method": "hourly_priority_with_conservative_surplus_calibration",
            "priority_order": [
                "household_base_load",
                "heat_pump",
                "battery_reserve",
                "calibration_reserve",
                "wallbox",
                "grid_export",
            ],
        },
        "energy_context_quality_percent": 92,
        "grid_import_kwh": _energy_total("heat_pump_grid_import_kwh"),
        "surplus_windows": ["11:20–13:40"],
        "possible_dhw_windows": ["12:00–13:00"],
        "pv_coverage_percent": round(
            _energy_total("heat_pump_pv_kwh") / _energy_total("heat_pump_kwh") * 100
        ),
        "expected_grid_import_kwh": _energy_total("heat_pump_grid_import_kwh"),
        "expected_load_grid_import_kwh": round(
            _energy_total("household_grid_import_kwh")
            + _energy_total("heat_pump_grid_import_kwh"),
            2,
        ),
        "energy_context": {
            "available": True,
            "provider_version": 1,
            "quality_percent": 92,
            "sample_count": 612,
            "history_days": 35,
            "base_load_origin": "stats_hourly_house_minus_large_consumers",
            "generated_at": "2026-01-15T00:00:00+00:00",
            "grid_export_usage": "calibration_only",
            "priority_order": [
                "household_base_load",
                "heat_pump",
                "battery_reserve",
                "calibration_reserve",
                "wallbox",
                "grid_export",
            ],
        },
        "optimization": {
            "available": True,
            "start": "2026-01-15T11:20:00+00:00",
            "duration_minutes": 140,
            "pv_surplus_kwh": 3.1,
            "recommendation": "Thermischen Speicher solar laden",
            "origin": "demo_pv_and_load_forecast",
        },
    },
    "mobility": {
        "available": True,
        "configured": True,
        "reason": None,
        "wallbox_name": "KEPLER Wallbox",
        "connected": True,
        "charging": False,
        "current_power_kw": 0.0,
        "energy_today_kwh": 4.2,
        "current_soc_percent": 42,
        "target_soc_percent": 80,
        "battery_capacity_kwh": 77,
        "demand_source": "distance",
        "demand_confidence_percent": 75,
        "battery_energy_demand_kwh": 29.28,
        "requested_energy_kwh": 30,
        "planned_distance_km": 160,
        "consumption_kwh_per_100km": 18.3,
        "charging_efficiency_percent": 90,
        "max_charging_power_kw": 11,
        "required_energy_kwh": 29.3,
        "departure_time": "2026-01-16T07:00:00+00:00",
        "planning_window_start": "2026-01-15T00:00:00+00:00",
        "planning_window_end": "2026-01-16T07:00:00+00:00",
        "planning_horizon_hours": 31.0,
        "planning_covered_hours": 31.0,
        "planning_intervals_contiguous": True,
        "planning_window_complete": True,
        "recommended_action": "defer",
        "recommendation_reason": "pv_window_upcoming",
        "recommendation_explanation": {
            "headline": "Auf das nächste PV-Fenster warten",
            "summary": "Zwischen Haus, Wärmepumpe und Speicher bleibt mittags die meiste Energie für das Fahrzeug.",
            "evidence": [
                "29,3 kWh Ladebedarf bis zur Abfahrt",
                "10,96 kWh erwarteter PV-Anteil",
                "PV-Prognoseband 9,4–12,5 kWh",
            ],
            "confidence_percent": 86,
            "uncertainty_percent": 18,
            "reason": "pv_window_upcoming",
        },
        "recommendation_confidence_percent": 86,
        "forecast_uncertainty_percent": 18,
        "recommended_start": _MOBILITY_CHARGING_HOURS[0]["interval_start"],
        "recommended_end": _MOBILITY_CHARGING_HOURS[-1]["interval_end"],
        "recommended_energy_kwh": 29.3,
        "expected_pv_energy_kwh": 10.96,
        "expected_pv_energy_lower_kwh": 9.4,
        "expected_pv_energy_upper_kwh": 12.5,
        "expected_grid_energy_kwh": 18.34,
        "household_grid_import_kwh": 10.3,
        "heat_pump_grid_import_kwh": 55.66,
        "expected_grid_export_kwh": 0.0,
        "expected_pv_share_percent": 37,
        "departure_readiness_percent": 100,
        "departure_risk": False,
        "charging_window_active": False,
        "pv_charging_recommended": False,
        "residual_pv_window_active": False,
        "low_price_charging_recommended": False,
        "estimated_immediate_cost_eur": 10.81,
        "estimated_advised_cost_eur": 4.25,
        "estimated_cost_advantage_eur": 6.56,
        "electricity_price_ct_per_kwh": 36.9,
        "feed_in_tariff_ct_per_kwh": 8.2,
        "data_quality_percent": 96,
        "energy_context_quality_percent": 92,
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
        "allocation_valid": True,
        "allocation_status": "valid",
        "advisory_only": True,
        "control_services_called": False,
        "hours": _MOBILITY_HOURS,
    },
    "weather": {
        "available": True,
        "status": "ready",
        "source_status": "available",
        "issued_at": "2026-01-15T08:00:00+00:00",
        "valid_at": "2026-01-15T08:30:00+00:00",
        "horizon_hours": [0, 6, 24, 72],
        "data_quality": {
            "forecast_available": True,
            "forecast_stale": False,
            "hourly_points": 72,
            "score_percent": 94,
            "actual_available": True,
            "paired_now": 3,
            "paired_samples": 286,
            "coverage_percent": 96,
            "data_since": "2025-03-25",
        },
        "outlook": {
            "next_hours": {
                "hours": 24,
                "temperature_min_c": -0.4,
                "temperature_max_c": 7.1,
                "precipitation_forecast_mm": 4.8,
                "precipitation_probability_max": 82,
                "wind_speed_max": 34,
                "summary_code": "heavy_rain",
            },
            "hourly": [
                {
                    "valid_at": "2026-01-15T09:00:00+00:00",
                    "temperature_c": 5.2,
                    "humidity": 72,
                    "pressure": 1009,
                    "precipitation": 0.0,
                    "precipitation_probability": 15,
                    "wind_speed": 14,
                },
                {
                    "valid_at": "2026-01-15T12:00:00+00:00",
                    "temperature_c": 7.1,
                    "humidity": 76,
                    "pressure": 1007,
                    "precipitation": 0.2,
                    "precipitation_probability": 38,
                    "wind_speed": 19,
                },
                {
                    "valid_at": "2026-01-15T15:00:00+00:00",
                    "temperature_c": 5.9,
                    "humidity": 82,
                    "pressure": 1004,
                    "precipitation": 1.6,
                    "precipitation_probability": 78,
                    "wind_speed": 28,
                },
                {
                    "valid_at": "2026-01-15T18:00:00+00:00",
                    "temperature_c": 3.6,
                    "humidity": 88,
                    "pressure": 1002,
                    "precipitation": 2.4,
                    "precipitation_probability": 82,
                    "wind_speed": 34,
                },
                {
                    "valid_at": "2026-01-16T00:00:00+00:00",
                    "temperature_c": -0.4,
                    "humidity": 79,
                    "pressure": 1009,
                    "precipitation": 0.0,
                    "precipitation_probability": 20,
                    "wind_speed": 12,
                },
            ],
            "daily": [
                {
                    "date": "2026-01-15",
                    "temperature_min_c": 1.8,
                    "temperature_max_c": 7.1,
                    "temperature_avg_c": 4.8,
                    "precipitation_forecast_mm": 4.8,
                    "precipitation_probability_max": 82,
                    "wind_speed_max": 34,
                    "summary_code": "heavy_rain",
                },
                {
                    "date": "2026-01-16",
                    "temperature_min_c": -1.4,
                    "temperature_max_c": 4.2,
                    "temperature_avg_c": 1.2,
                    "precipitation_forecast_mm": 0.6,
                    "precipitation_probability_max": 35,
                    "wind_speed_max": 19,
                    "summary_code": "frost",
                },
                {
                    "date": "2026-01-17",
                    "temperature_min_c": 0.6,
                    "temperature_max_c": 6.8,
                    "temperature_avg_c": 3.6,
                    "precipitation_forecast_mm": 2.1,
                    "precipitation_probability_max": 64,
                    "wind_speed_max": 25,
                    "summary_code": "rain_likely",
                },
            ],
        },
        "warnings": [
            {
                "code": "heavy_precipitation",
                "severity": "warning",
                "start": "2026-01-15T17:00:00+00:00",
                "end": "2026-01-15T20:00:00+00:00",
                "impact_code": "heavy_rain_precautions",
                "evidence": {"precipitation": 6.4, "precipitation_probability": 82},
            },
            {
                "code": "freeze_risk",
                "severity": "advisory",
                "start": "2026-01-16T03:00:00+00:00",
                "end": "2026-01-16T06:00:00+00:00",
                "impact_code": "freeze_precautions",
                "evidence": {"temperature_c": -1.4},
            },
        ],
        "accuracy": {
            "0_6h": {"pairs": 96, "mae": 0.62, "bias": -0.08, "rmse": 0.79},
            "6_24h": {"pairs": 103, "mae": 0.91, "bias": 0.14, "rmse": 1.16},
            "24_72h": {"pairs": 87, "mae": 1.38, "bias": 0.31, "rmse": 1.72},
        },
        "uncertainty": {
            "available": True,
            "method": "empirical_p90_absolute_error",
            "pairs": 286,
            "temperature_c": 1.9,
        },
        "events": [
            {
                "code": "freeze_risk",
                "valid_at": "2026-01-16T04:00:00+00:00",
                "evidence": {"temperature_c": -1.4},
            },
            {
                "code": "high_precipitation_probability",
                "valid_at": "2026-01-15T18:00:00+00:00",
                "evidence": {"precipitation_probability": 78},
            },
        ],
        "actual_history": {
            "available": True,
            "status": "available",
            "source": "sfml_database",
            "sources": {
                "sfml_database": True,
                "weather_fusion_tracker": False,
                "home_assistant_recorder": False,
            },
            "current": {
                "available": True,
                "source": "sfml_database",
                "observed_at": "2026-01-15T08:00:00+00:00",
                "observation_date": "2026-01-15",
                "temperature_c": 4.6,
                "humidity": 70,
                "pressure": 1010,
                "solar_radiation_wm2": 72,
                "wind_speed": 11,
                "wind_bearing": 214,
                "precipitation": 0,
            },
            "timeline": [
                {
                    "observed_at": "2026-01-14T12:00:00+00:00",
                    "temperature_c": 6.8,
                    "humidity": 68,
                    "pressure": 1018,
                    "wind_speed": 8,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-14T15:00:00+00:00",
                    "temperature_c": 7.4,
                    "humidity": 65,
                    "pressure": 1016,
                    "wind_speed": 10,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-14T18:00:00+00:00",
                    "temperature_c": 5.2,
                    "humidity": 73,
                    "pressure": 1015,
                    "wind_speed": 12,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-14T21:00:00+00:00",
                    "temperature_c": 3.1,
                    "humidity": 80,
                    "pressure": 1014,
                    "wind_speed": 9,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-15T00:00:00+00:00",
                    "temperature_c": 1.7,
                    "humidity": 84,
                    "pressure": 1013,
                    "wind_speed": 7,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-15T03:00:00+00:00",
                    "temperature_c": 0.9,
                    "humidity": 86,
                    "pressure": 1012,
                    "wind_speed": 8,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-15T06:00:00+00:00",
                    "temperature_c": 2.8,
                    "humidity": 78,
                    "pressure": 1011,
                    "wind_speed": 10,
                    "precipitation": 0,
                },
                {
                    "observed_at": "2026-01-15T08:00:00+00:00",
                    "temperature_c": 4.6,
                    "humidity": 70,
                    "pressure": 1010,
                    "wind_speed": 11,
                    "precipitation": 0,
                },
            ],
            "aggregates": {
                "data_since": "2025-03-25",
                "coverage_percent": 96,
                "temperature_min_c": -8.2,
                "temperature_max_c": 32.4,
                "temperature_avg_c": 11.1,
                "solar_radiation_avg_wm2": 134.2,
                "solar_radiation_max_wm2": 982,
                "valid_days": 286,
            },
            "records": [
                {
                    "type": "Kältester Tag",
                    "value": -8.2,
                    "unit": "°C",
                    "date": "2025-12-28",
                },
                {
                    "type": "Wärmster Tag",
                    "value": 32.4,
                    "unit": "°C",
                    "date": "2025-07-02",
                },
            ],
            "monthly_series": [
                {
                    "month": "Mär",
                    "temperature_avg_c": 8.6,
                    "temperature_min_c": -2.1,
                    "temperature_max_c": 18.4,
                },
                {
                    "month": "Apr",
                    "temperature_avg_c": 11.4,
                    "temperature_min_c": 0.8,
                    "temperature_max_c": 22.1,
                },
                {
                    "month": "Mai",
                    "temperature_avg_c": 15.2,
                    "temperature_min_c": 4.1,
                    "temperature_max_c": 27.0,
                },
                {
                    "month": "Jun",
                    "temperature_avg_c": 18.7,
                    "temperature_min_c": 7.2,
                    "temperature_max_c": 30.8,
                },
                {
                    "month": "Jul",
                    "temperature_avg_c": 21.2,
                    "temperature_min_c": 9.1,
                    "temperature_max_c": 32.4,
                },
                {
                    "month": "Aug",
                    "temperature_avg_c": 20.1,
                    "temperature_min_c": 8.4,
                    "temperature_max_c": 31.2,
                },
                {
                    "month": "Sep",
                    "temperature_avg_c": 15.7,
                    "temperature_min_c": 4.9,
                    "temperature_max_c": 25.5,
                },
                {
                    "month": "Okt",
                    "temperature_avg_c": 10.2,
                    "temperature_min_c": -0.8,
                    "temperature_max_c": 19.7,
                },
                {
                    "month": "Nov",
                    "temperature_avg_c": 5.8,
                    "temperature_min_c": -3.4,
                    "temperature_max_c": 14.2,
                },
                {
                    "month": "Dez",
                    "temperature_avg_c": 2.1,
                    "temperature_min_c": -8.2,
                    "temperature_max_c": 10.1,
                },
                {
                    "month": "Jan",
                    "temperature_avg_c": 3.0,
                    "temperature_min_c": -5.7,
                    "temperature_max_c": 11.3,
                },
            ],
            "precipitation": {
                "available": True,
                "source": "sfml_database",
                "total_mm": 486.2,
                "rain_days": 91,
                "dry_spell_days": 16,
                "daily": [
                    {"date": "2026-01-09", "total_mm": 0},
                    {"date": "2026-01-10", "total_mm": 2.4},
                    {"date": "2026-01-11", "total_mm": 0.4},
                    {"date": "2026-01-12", "total_mm": 8.7},
                    {"date": "2026-01-13", "total_mm": 0},
                    {"date": "2026-01-14", "total_mm": 0},
                    {"date": "2026-01-15", "total_mm": 0},
                ],
            },
            "year_comparison": {
                "available": True,
                "current": {
                    "year": 2026,
                    "valid_days": 15,
                    "temperature_avg_c": 3.0,
                    "precipitation_total_mm": 18.7,
                    "rain_days": 5,
                    "dry_spell_days": 4,
                },
                "previous": {
                    "year": 2025,
                    "valid_days": 286,
                    "temperature_avg_c": 12.7,
                    "precipitation_total_mm": 467.5,
                    "rain_days": 86,
                    "dry_spell_days": 16,
                },
                "same_day_last_year": {"date": "2025-01-15", "temperature_avg_c": 3.8},
            },
            "source_comparison": {
                "sfml_expert_blend": [
                    {
                        "valid_at": "2026-01-15T09:00:00+00:00",
                        "temperature_c": 5.6,
                        "precipitation": 0,
                        "ghi_wm2": 84,
                        "dni_wm2": 31,
                        "dhi_wm2": 53,
                    },
                    {
                        "valid_at": "2026-01-15T12:00:00+00:00",
                        "temperature_c": 6.8,
                        "precipitation": 0.3,
                        "ghi_wm2": 126,
                        "dni_wm2": 42,
                        "dhi_wm2": 84,
                    },
                ],
                "kepler_input": [
                    {
                        "valid_at": "2026-01-15T09:00:00+00:00",
                        "temperature_c": 5.1,
                        "precipitation": 0,
                        "ghi_wm2": 79,
                        "dni_wm2": 28,
                        "dhi_wm2": 51,
                    },
                    {
                        "valid_at": "2026-01-15T12:00:00+00:00",
                        "temperature_c": 7.0,
                        "precipitation": 0.2,
                        "ghi_wm2": 121,
                        "dni_wm2": 40,
                        "dhi_wm2": 81,
                    },
                ],
            },
        },
        "forecast_vs_actual": {
            "temperature_c": {
                "by_horizon": {
                    "0_6h": {"pairs": 96, "mae": 0.62, "bias": -0.08},
                    "6_24h": {"pairs": 103, "mae": 0.91, "bias": 0.14},
                    "24_72h": {"pairs": 87, "mae": 1.38, "bias": 0.31},
                },
                "trend": {
                    "direction": "improving",
                    "recent_mae": 0.71,
                    "previous_mae": 0.89,
                },
            }
        },
        "cold_start": False,
    },
    "diagnostics": {
        "available": True,
        "locked": False,
        "health_score": 94,
        "health_score_available": True,
        "health_score_reliable": False,
        "health_score_origin": "data_quality",
        "data_health_score": 94,
        "setup_complete": True,
        "data_ready": True,
        "sensor_coverage_percent": 100,
        "sensor_count_available": 20,
        "sensor_count_configured": 20,
        "sensor_count_supported": 20,
        "sensor_quality": "good",
        "data_gaps": 0,
        "degraded": False,
        "degraded_reasons": [],
        "sensor_age_seconds": {},
        "forecast_quality": "preview",
        "model_status": "learning",
        "plant_assessment_reliable": False,
        "plant_assessment": {
            "status": "not_assessed",
            "reliable": False,
            "score": None,
        },
        "readiness": {
            "setup": {"status": "complete", "complete": True},
            "data": {"status": "ready", "ready": True},
            "model": {"status": "learning"},
            "plant": {"status": "not_assessed", "reliable": False, "score": None},
        },
        "drift": "stable",
        "provider_status": "available",
        "issues": [],
        "anomalies": [],
        "checks": {
            "required_sensors": True,
            "timestamps_fresh": True,
            "assignments_unique": True,
            "forecast_source": True,
        },
    },
}


def _status(**changes: Any) -> dict[str, Any]:
    status = {
        "data_mode": "mock",
        "eai_installed": False,
        "license_status": "not_provided",
        "configuration_status": "not_configured",
        "capability_level": "preview",
        "data_freshness": None,
        "provider_version": 8,
        "is_demo": True,
    }
    status.update(changes)
    return status


SCENARIOS: dict[str, dict[str, Any]] = {
    "demo_full_capability": {"status": _status(), **_FULL_DATA},
    "demo_minimal_sensors": {
        "status": _status(eai_installed=True, capability_level="essential"),
        **_FULL_DATA,
    },
    "demo_learning_phase": {
        "status": _status(
            eai_installed=True,
            license_status="valid",
            configuration_status="configured",
            capability_level="standard",
            data_mode="onboarding",
        ),
        **_FULL_DATA,
    },
    "demo_license_required": {"status": _status(eai_installed=True), **_FULL_DATA},
    "demo_provider_unavailable": {
        "status": _status(eai_installed=True, data_mode="unavailable", is_demo=False),
        **{section: {} for section in SECTIONS},
    },
    "demo_degraded_data": {
        "status": _status(
            eai_installed=True,
            license_status="valid",
            configuration_status="configured",
            data_mode="degraded",
            data_freshness="2026-01-15T08:00:00+00:00",
            is_demo=False,
        ),
        **{section: {} for section in SECTIONS},
    },
    "demo_configured_no_history": {
        "status": _status(
            eai_installed=True,
            license_status="valid",
            configuration_status="configured",
            capability_level="standard",
            data_mode="onboarding",
            is_demo=False,
        ),
        **{section: {} for section in SECTIONS},
    },
    "demo_feature_unconfigured": {
        "status": _status(
            eai_installed=True,
            license_status="valid",
            configuration_status="not_configured",
            capability_level="preview",
            data_mode="mock",
            is_demo=True,
        ),
        **_FULL_DATA,
    },
}
_SCENARIO_SAMPLE_REASON = {
    "demo_full_capability": "no_license",
    "demo_minimal_sensors": "no_license",
    "demo_license_required": "no_license",
    "demo_provider_unavailable": "no_license",
    "demo_feature_unconfigured": "feature_off",
    "demo_learning_phase": "learning",
    "demo_configured_no_history": "learning",
    "demo_degraded_data": "live",
}
for _scenario_name, _scenario in SCENARIOS.items():
    _scenario["status"]["sample_reason"] = _SCENARIO_SAMPLE_REASON[_scenario_name]


def scenario_payload(name: str, section: str) -> dict[str, Any]:
    scenario = SCENARIOS[name]
    return {**scenario["status"], "section": section, "data": scenario[section]}
