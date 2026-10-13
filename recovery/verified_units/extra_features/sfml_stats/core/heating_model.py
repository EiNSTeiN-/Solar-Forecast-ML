# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Pure Kepler room model and Hubble season helpers. No Home Assistant writes."""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_PHASE = timedelta(minutes=45)
READY_PHASES = 3
MAX_PHASES = 20
MAX_PREHEAT = timedelta(hours=3)
HEAT_CALL_GAP = 0.5
HEAT_PHASE_GAP = 0.3
COOL_OUTDOOR_GAP = 3.0
OFFSET_CAP_K = 4.0
OFFSET_HYSTERESIS_K = 0.5
OFFSET_MIN_SAMPLES = 3
_MISSING_ACTION = frozenset({"", "none", "unknown", "unavailable"})


@dataclass(frozen=True)
class RoomModel:
    heat_rates: tuple[float, ...] = ()
    cool_taus: tuple[float, ...] = ()

    @property
    def heat_count(self) -> int:
        return len(self.heat_rates)

    @property
    def cool_count(self) -> int:
        return len(self.cool_taus)

    @property
    def ready(self) -> bool:
        return self.heat_count >= READY_PHASES and self.cool_count >= READY_PHASES

    @property
    def rate_k_per_h(self) -> float | None:
        return _median(self.heat_rates) if self.heat_rates else None

    @property
    def tau_h(self) -> float | None:
        return _median(self.cool_taus) if self.cool_taus else None


@dataclass(frozen=True)
class OpenPhase:
    kind: str
    started: datetime
    start_c: float
    outdoor_c: float | None
    offsets: tuple[tuple[str, float], ...] = ()


def forecast_summary(
    points: list[tuple[datetime, float]], now: datetime
) -> tuple[float | None, float | None]:
    """Mean of the next 24 h and minimum of the next 12 h."""
    future_points = [
        (point_time, temperature_c)
        for point_time, temperature_c in points
        if point_time >= now
    ]
    next_day_temperatures = [
        temperature_c
        for point_time, temperature_c in future_points
        if point_time < now + timedelta(hours=24)
    ]
    next_half_day_temperatures = [
        temperature_c
        for point_time, temperature_c in future_points
        if point_time < now + timedelta(hours=12)
    ]
    forecast_mean_c = (
        sum(next_day_temperatures) / len(next_day_temperatures)
        if next_day_temperatures
        else None
    )
    forecast_min_c = (
        min(next_half_day_temperatures) if next_half_day_temperatures else None
    )
    return (forecast_mean_c, forecast_min_c)


def season_with_forecast(
    measured: float | None, forecast: float | None, limit: float, previous: bool | None
) -> tuple[bool | None, str | None]:
    """On when the colder of measurement and forecast is below the limit."""
    if measured is None:
        return (previous, "outdoor_unavailable") if True else None
    if forecast is None:
        if measured < limit:
            season_enabled = True
        elif measured > limit + 1.0:
            season_enabled = False
        elif previous is None:
            season_enabled = False
        else:
            season_enabled = previous
        return (season_enabled, "forecast_unavailable") if True else None
    effective_coldest_c = min(measured, forecast)
    if effective_coldest_c < limit:
        return (True, None) if True else None
    if measured > limit + 1.0 and forecast > limit + 1.0:
        return (False, None) if True else None
    if previous is None:
        return (False, None) if True else None
    return (previous, None) if True else None


def thermostat_demand(
    hvac_action: str | None,
    valve_pct: float | None,
    hvac_mode: str | None,
    setpoint_c: float | None,
    current_c: float | None,
) -> bool:
    """True when the valve is actually heating. Mode alone is only a last resort."""
    normalized_action = (
        hvac_action.strip().lower() if isinstance(hvac_action, str) else ""
    )
    has_action = normalized_action not in _MISSING_ACTION
    has_numeric_valve = isinstance(valve_pct, (int, float)) and (
        not isinstance(valve_pct, bool)
    )
    if has_action or has_numeric_valve:
        return (
            has_action
            and normalized_action == "heating"
            or (has_numeric_valve and float(valve_pct) > 0)
        )
    if (hvac_mode or "").lower() != "heat":
        return False
    if setpoint_c is None or current_c is None:
        return False
    return float(setpoint_c) >= float(current_c) + HEAT_CALL_GAP


def phase_kind(
    demand: bool, target: float | None, actual: float | None, outdoor: float | None
) -> str | None:
    """Heat while the room is short of its target and calling. Cool only with no demand."""
    if actual is None:
        return
    if (
        demand
        and target is not None
        and (float(target) >= float(actual) + HEAT_PHASE_GAP)
    ):
        return "heat"
    if not demand and outdoor is not None:
        if float(actual) > float(outdoor) + COOL_OUTDOOR_GAP:
            return "cool"
    return None if True else None


def propose_offset(samples: tuple[float, ...]) -> float | None:
    """Median room-minus-radiator gap, clamped. Needs three samples."""
    if len(samples) < OFFSET_MIN_SAMPLES:
        return
    ordered_samples = tuple(sorted((float(offset_sample) for offset_sample in samples)))
    middle_index = len(ordered_samples) // 2
    median_gap_k = (
        ordered_samples[middle_index]
        if len(ordered_samples) % 2
        else (ordered_samples[middle_index - 1] + ordered_samples[middle_index]) / 2
    )
    return max(-OFFSET_CAP_K, min(OFFSET_CAP_K, median_gap_k))


def adopt_offset(previous: float | None, proposed: float | None) -> float | None:
    """Keep the last offset until the median moves by at least half a kelvin."""
    if proposed is None:
        return previous
    if previous is None or abs(proposed - previous) >= OFFSET_HYSTERESIS_K:
        return proposed
    return previous


def accept_rate(rate: float) -> bool:
    return 0.05 <= rate <= 8.0


def accept_tau(tau: float) -> bool:
    return 0.5 <= tau <= 72.0


def heat_rate(start_c: float, end_c: float, hours: float) -> float | None:
    if hours <= 0:
        return
    rate_k_per_hour = (end_c - start_c) / hours
    return rate_k_per_hour if accept_rate(rate_k_per_hour) else None


def cool_tau(
    start_c: float, end_c: float, outdoor_c: float, hours: float
) -> float | None:
    if hours <= 0 or start_c <= outdoor_c or end_c <= outdoor_c or (end_c >= start_c):
        return None
    cooling_ratio = (end_c - outdoor_c) / (start_c - outdoor_c)
    if cooling_ratio <= 0 or cooling_ratio >= 1:
        return None
    tau_hours = -hours / math.log(cooling_ratio)
    return tau_hours if accept_tau(tau_hours) else None


def close_phase(
    model: RoomModel,
    phase: OpenPhase,
    end_c: float,
    outdoor_c: float | None,
    ended: datetime,
) -> RoomModel:
    elapsed_hours = (ended - phase.started).total_seconds() / 3600
    if ended - phase.started < MIN_PHASE:
        return model
    if phase.kind == "heat":
        rate_k_per_hour = heat_rate(phase.start_c, end_c, elapsed_hours)
        if rate_k_per_hour is None:
            return model
        return RoomModel(
            heat_rates=_keep(model.heat_rates, rate_k_per_hour),
            cool_taus=model.cool_taus,
        )
    effective_outdoor_c = outdoor_c if outdoor_c is not None else phase.outdoor_c
    if effective_outdoor_c is None:
        return model
    tau_hours = cool_tau(phase.start_c, end_c, effective_outdoor_c, elapsed_hours)
    if tau_hours is None:
        return model
    return RoomModel(
        heat_rates=model.heat_rates, cool_taus=_keep(model.cool_taus, tau_hours)
    )


def expected_temperature(
    actual: float, outdoor: float, tau_h: float, hours: float
) -> float:
    if tau_h <= 0 or hours <= 0:
        return actual
    return outdoor + (actual - outdoor) * math.exp(-hours / tau_h)


def preheat_lead_hours(comfort: float, expected: float, rate: float) -> float:
    if rate <= 0 or expected >= comfort - HEAT_CALL_GAP:
        return 0.0
    lead_hours = (comfort - expected) / rate
    return max(0.0, min(MAX_PREHEAT.total_seconds() / 3600, lead_hours))


def model_to_dict(model: RoomModel) -> dict[str, list[float]]:
    return {"heat_rates": list(model.heat_rates), "cool_taus": list(model.cool_taus)}


def model_from_dict(payload: object) -> RoomModel:
    if not isinstance(payload, dict):
        return RoomModel()
    return RoomModel(
        heat_rates=_numbers(payload.get("heat_rates")),
        cool_taus=_numbers(payload.get("cool_taus")),
    )


def phase_to_dict(phase: OpenPhase | None) -> dict[str, object] | None:
    if phase is None:
        return
    return {
        "kind": phase.kind,
        "started": phase.started.isoformat(),
        "start_c": phase.start_c,
        "outdoor_c": phase.outdoor_c,
        "offsets": {
            offset_key: offset_value for offset_key, offset_value in phase.offsets
        },
    }


def _keep(values: tuple[float, ...], value: float) -> tuple[float, ...]:
    return (*values, value)[-MAX_PHASES:]


def _numbers(payload: object) -> tuple[float, ...]:
    if not isinstance(payload, list):
        return ()
    numeric_values = []
    for value in payload:
        if isinstance(value, (int, float)) and (not isinstance(value, bool)):
            numeric_values.append(float(value))
    return tuple(numeric_values[-MAX_PHASES:])


def _median(values: tuple[float, ...]) -> float:
    ordered_samples = tuple(sorted(values))
    middle_index = len(ordered_samples) // 2
    if len(ordered_samples) % 2:
        return ordered_samples[middle_index]
    return (ordered_samples[middle_index - 1] + ordered_samples[middle_index]) / 2
