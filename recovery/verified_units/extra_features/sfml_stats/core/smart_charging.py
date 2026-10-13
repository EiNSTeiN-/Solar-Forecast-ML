# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Iterable, Sequence
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .grid_charge_gate import resolve_grid_charge, resolve_hour_is_cheap
from .solar_forecast_reader_gpm import SolarForecastReader
from ..const import (
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_SMART_CHARGING_MODE,
    SMC_MODE_COMBINED,
    SMC_MODE_FORECAST,
    SMC_MODE_PRICE_BAND_SOC,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SmartChargingState:
    """Current state of the smart charging logic. @zara"""

    target_soc: float
    current_soc: float | None
    solar_forecast_kwh: float | None
    solar_forecast_today_kwh: float | None
    solar_forecast_tomorrow_kwh: float | None
    is_active: bool
    reason: str
    requested_grid_charge_kwh: float = 0.0
    economic_decision: str = "not_load"
    effective_storage_cost_ct_kwh: float | None = None
    compared_future_price_ct_kwh: float | None = None
    effective_roundtrip_efficiency: float | None = None
    reserved_future_grid_charge_kwh: float = 0.0
    mode: str | None = None
    band_target_soc: float | None = None
    forecast_target_soc: float | None = None


@dataclass(frozen=True)
class EconomicChargingInput:
    """Inputs required to compare grid charging with later grid imports."""

    current_price_ct_kwh: float | None
    future_price_ct_kwh: Sequence[object] | None
    current_soc: float | None
    battery_capacity_kwh: float
    min_soc: float
    max_soc: float
    average_consumption_kwh: float | None
    solar_today_kwh: float | None
    solar_tomorrow_kwh: float | None
    max_charge_power_kw: float
    inverter_nominal_power_kw: float
    ac_dc_efficiency: float = 0.94
    battery_charge_efficiency: float = 0.96
    battery_discharge_efficiency: float = 0.96
    inverter_nominal_efficiency: float = 0.96
    inverter_partial_load_efficiency: float = 0.9
    slot_duration_hours: float = 1.0
    future_slot_duration_hours: float = 1.0
    planning_time: datetime | None = None
    demand_deadline_hours: float = 36.0
    storage_self_consumption_fraction: float = 0.01
    storage_self_consumption_kw: float = 0.02
    storage_hold_hours: float = 8.0


@dataclass(frozen=True)
class EconomicChargingPlan:
    """Explainable outcome of one economic charging evaluation."""

    decision: str
    reason: str
    target_soc: float
    required_grid_charge_kwh: float
    current_storage_cost_ct_kwh: float | None
    cheaper_future_price_ct_kwh: float | None
    effective_roundtrip_efficiency: float | None
    reserved_future_grid_charge_kwh: float = 0.0


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _as_positive_prices(
    values: Iterable[object] | None,
    planning_time: datetime | None = None,
    default_duration_hours: float = 1.0,
    initial_offset_hours: float | None = None,
    deadline_hours: float = 36.0,
) -> list[tuple[float, float, float]]:
    """Normalize price slots; timestamp-less slots after explicit ones are ignored."""
    normalized_slots = []
    try:
        next_offset_hours = max(
            0.0,
            float(
                default_duration_hours
                if initial_offset_hours is None
                else initial_offset_hours
            ),
        )
    except (TypeError, ValueError):
        return normalized_slots
    seen_timestamp = False
    for slot_value in values or ():
        timestamp = None
        duration_value = default_duration_hours
        if isinstance(slot_value, dict):
            timestamp = slot_value.get("timestamp")
            duration_value = slot_value.get("duration_hours", default_duration_hours)
            slot_value = slot_value.get("total_price", slot_value.get("price"))
        elif isinstance(slot_value, (tuple, list)) and slot_value:
            if len(slot_value) >= 2:
                timestamp, slot_value = (slot_value[0], slot_value[-1])
            else:
                slot_value = slot_value[-1]
        if timestamp is not None:
            seen_timestamp = True
        try:
            price_ct_kwh = float(slot_value)
            duration_hours = max(0.0, float(duration_value))
        except (TypeError, ValueError):
            continue
        if not isfinite(price_ct_kwh) or duration_hours <= 0.0:
            continue
        offset_hours = next_offset_hours
        if timestamp is not None and planning_time is not None:
            try:
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                offset_hours = (timestamp - planning_time).total_seconds() / 3600.0
            except (TypeError, ValueError):
                continue
        else:
            if seen_timestamp:
                continue
            next_offset_hours += duration_hours
        deadline_limit_hours = max(0.0, float(deadline_hours))
        slot_start_hours = max(0.0, offset_hours)
        slot_end_hours = min(deadline_limit_hours, offset_hours + duration_hours)
        usable_duration_hours = max(0.0, slot_end_hours - slot_start_hours)
        if not usable_duration_hours > 0.0:
            continue
        normalized_slots.append((price_ct_kwh, slot_start_hours, usable_duration_hours))
        continue
    return normalized_slots


def plan_economic_grid_charge(data: EconomicChargingInput) -> EconomicChargingPlan:
    """Return an energy-limited charging decision without Home Assistant I/O."""
    try:
        capacity_kwh = float(data.battery_capacity_kwh)
    except (TypeError, ValueError):
        capacity_kwh = 0.0
    if not isfinite(capacity_kwh):
        capacity_kwh = 0.0
    if capacity_kwh <= 0.0 or data.current_soc is None:
        return EconomicChargingPlan(
            "not_load", "economic_input_unavailable", 0.0, 0.0, None, None, None
        )
    try:
        current_soc = float(data.current_soc)
        current_price_ct_kwh = (
            float(data.current_price_ct_kwh)
            if data.current_price_ct_kwh is not None
            else None
        )
        average_consumption_kwh = (
            float(data.average_consumption_kwh)
            if data.average_consumption_kwh is not None
            else None
        )
        solar_today_kwh = (
            float(data.solar_today_kwh) if data.solar_today_kwh is not None else None
        )
        solar_tomorrow_kwh = (
            float(data.solar_tomorrow_kwh)
            if data.solar_tomorrow_kwh is not None
            else None
        )
        minimum_soc = float(data.min_soc)
        maximum_soc = float(data.max_soc)
    except (TypeError, ValueError):
        return EconomicChargingPlan(
            "not_load", "economic_input_unavailable", 0.0, 0.0, None, None, None
        )
    if (
        current_price_ct_kwh is None
        or not isfinite(current_price_ct_kwh)
        or average_consumption_kwh is None
        or (not isfinite(average_consumption_kwh))
        or (average_consumption_kwh < 0.0)
        or (solar_today_kwh is None)
        or (not isfinite(solar_today_kwh))
        or (solar_tomorrow_kwh is None)
        or (not isfinite(solar_tomorrow_kwh))
        or (not isfinite(current_soc))
        or (not isfinite(minimum_soc))
        or (not isfinite(maximum_soc))
    ):
        return EconomicChargingPlan(
            "not_load", "forecast_data_unavailable", current_soc, 0.0, None, None, None
        )
    current_soc = _bounded(current_soc, 0.0, 100.0)
    minimum_soc = _bounded(minimum_soc, 0.0, 100.0)
    maximum_soc = _bounded(maximum_soc, minimum_soc, 100.0)
    try:
        numeric_inputs = tuple(
            (
                float(slot_value)
                for slot_value in (
                    data.slot_duration_hours,
                    data.future_slot_duration_hours,
                    data.max_charge_power_kw,
                    data.inverter_nominal_power_kw,
                    data.inverter_nominal_efficiency,
                    data.inverter_partial_load_efficiency,
                    data.ac_dc_efficiency,
                    data.battery_charge_efficiency,
                    data.battery_discharge_efficiency,
                    data.storage_self_consumption_kw,
                    data.storage_hold_hours,
                    data.storage_self_consumption_fraction,
                    data.demand_deadline_hours,
                )
            )
        )
    except (TypeError, ValueError):
        numeric_inputs = ()
    if not numeric_inputs or not all(
        (isfinite(slot_value) for slot_value in numeric_inputs)
    ):
        return EconomicChargingPlan(
            "not_load", "economic_input_unavailable", current_soc, 0.0, None, None, None
        )
    current_slot_hours = max(0.0, numeric_inputs[0])
    max_charge_power_kw = max(0.0, numeric_inputs[2])
    deadline_hours = max(0.0, numeric_inputs[12])
    storage_hold_hours = min(max(0.0, numeric_inputs[10]), deadline_hours)
    stored_energy_kwh = capacity_kwh * current_soc / 100.0
    reserve_energy_kwh = capacity_kwh * minimum_soc / 100.0
    expected_demand_kwh = average_consumption_kwh * 1.35
    hold_demand_kwh = (
        expected_demand_kwh * storage_hold_hours / deadline_hours
        if deadline_hours > 0.0
        else 0.0
    )
    hold_storage_kwh = (
        hold_demand_kwh
        / (
            _bounded(numeric_inputs[8], 0.01, 1.0)
            * _bounded(numeric_inputs[4], 0.01, 1.0)
        )
        + max(0.0, numeric_inputs[9]) * storage_hold_hours
    )
    required_stored_kwh = max(
        reserve_energy_kwh + hold_storage_kwh,
        expected_demand_kwh - max(0.0, solar_today_kwh),
    )
    required_stored_kwh = min(required_stored_kwh, capacity_kwh * maximum_soc / 100.0)
    energy_deficit_kwh = max(0.0, required_stored_kwh - stored_energy_kwh)
    target_soc = _bounded(
        (stored_energy_kwh + energy_deficit_kwh) * 100.0 / capacity_kwh,
        minimum_soc,
        maximum_soc,
    )
    if energy_deficit_kwh <= 0.01:
        return EconomicChargingPlan(
            "not_load",
            "forecast_covers_expected_demand",
            target_soc,
            0.0,
            None,
            None,
            None,
        )
    if current_slot_hours <= 0.0:
        return EconomicChargingPlan(
            "wait", "charge_window_unavailable", target_soc, 0.0, None, None, None
        )
    unlimited_charge = max_charge_power_kw == 0.0
    current_slot_capacity_kwh = (
        None if unlimited_charge else current_slot_hours * max_charge_power_kw
    )
    if current_slot_capacity_kwh is not None and current_slot_capacity_kwh <= 0.0:
        return EconomicChargingPlan(
            "wait", "charge_window_unavailable", target_soc, 0.0, None, None, None
        )
    inverter_nominal_power_kw = float(data.inverter_nominal_power_kw)
    required_charge_power_kw = energy_deficit_kwh / current_slot_hours
    if unlimited_charge:
        effective_charge_power_kw = required_charge_power_kw
    else:
        effective_charge_power_kw = min(max_charge_power_kw, required_charge_power_kw)
    if inverter_nominal_power_kw == 0.0:
        inverter_efficiency = float(data.inverter_nominal_efficiency)
    else:
        inverter_load_ratio = _bounded(
            effective_charge_power_kw / inverter_nominal_power_kw, 0.0, 1.0
        )
        efficiency_blend = _bounded(inverter_load_ratio / 0.35, 0.0, 1.0)
        inverter_efficiency = (
            float(data.inverter_partial_load_efficiency)
            + (
                float(data.inverter_nominal_efficiency)
                - float(data.inverter_partial_load_efficiency)
            )
            * efficiency_blend
        )
    charge_efficiency = _bounded(float(data.ac_dc_efficiency), 0.01, 1.0) * _bounded(
        float(data.battery_charge_efficiency), 0.01, 1.0
    )
    self_consumption_kwh = energy_deficit_kwh * max(
        0.0, float(data.storage_self_consumption_fraction)
    )
    grid_energy_required_kwh = (
        energy_deficit_kwh + self_consumption_kwh
    ) / charge_efficiency
    delivered_energy_kwh = (
        energy_deficit_kwh
        * _bounded(float(data.battery_discharge_efficiency), 0.01, 1.0)
        * _bounded(float(inverter_efficiency), 0.01, 1.0)
    )
    storage_cost_ct_kwh = (
        current_price_ct_kwh * grid_energy_required_kwh / delivered_energy_kwh
    )
    roundtrip_efficiency = delivered_energy_kwh / grid_energy_required_kwh
    if deadline_hours <= 0.0:
        charge_now_kwh = grid_energy_required_kwh
        if current_slot_capacity_kwh is not None:
            charge_now_kwh = min(grid_energy_required_kwh, current_slot_capacity_kwh)
        return EconomicChargingPlan(
            "load",
            "demand_deadline_unavailable_charge_now",
            target_soc,
            round(charge_now_kwh, 3),
            storage_cost_ct_kwh,
            None,
            roundtrip_efficiency,
        )
    future_slots = _as_positive_prices(
        data.future_price_ct_kwh,
        planning_time=data.planning_time,
        default_duration_hours=data.future_slot_duration_hours,
        initial_offset_hours=data.slot_duration_hours,
        deadline_hours=data.demand_deadline_hours,
    )
    if not future_slots:
        return EconomicChargingPlan(
            "wait",
            "future_price_forecast_unavailable",
            target_soc,
            0.0,
            storage_cost_ct_kwh,
            None,
            roundtrip_efficiency,
        )
    minimum_future_price = min(
        (
            price_ct_kwh
            for price_ct_kwh, unused_offset_hours, unused_duration_hours in future_slots
        )
    )
    demand_power_kw = (
        expected_demand_kwh / deadline_hours if deadline_hours > 0.0 else 0.0
    )
    reserve_runway_hours = (
        max(0.0, stored_energy_kwh - reserve_energy_kwh) / demand_power_kw
        if demand_power_kw > 0.0
        else 0.0
    )
    remaining_grid_kwh = grid_energy_required_kwh
    reserved_future_kwh = 0.0
    for price_ct_kwh, future_offset_hours, future_duration_hours in sorted(
        future_slots, key=lambda slot: slot[1]
    ):
        if future_offset_hours >= reserve_runway_hours:
            continue
        if price_ct_kwh > storage_cost_ct_kwh:
            continue
        if unlimited_charge:
            slot_reserved_kwh = remaining_grid_kwh
        else:
            future_slot_capacity_kwh = max_charge_power_kw * future_duration_hours
            slot_reserved_kwh = min(remaining_grid_kwh, future_slot_capacity_kwh)
        reserved_future_kwh += slot_reserved_kwh
        remaining_grid_kwh -= slot_reserved_kwh
        if remaining_grid_kwh <= 0.01:
            break
    if remaining_grid_kwh <= 0.01:
        return EconomicChargingPlan(
            "wait",
            "later_grid_import_is_cheaper",
            target_soc,
            0.0,
            storage_cost_ct_kwh,
            minimum_future_price,
            roundtrip_efficiency,
            round(reserved_future_kwh, 3),
        )
    charge_now_kwh = remaining_grid_kwh
    if current_slot_capacity_kwh is not None:
        charge_now_kwh = min(remaining_grid_kwh, current_slot_capacity_kwh)
    if charge_now_kwh <= 0.01:
        return EconomicChargingPlan(
            "wait",
            "charge_window_unavailable",
            target_soc,
            0.0,
            storage_cost_ct_kwh,
            minimum_future_price,
            roundtrip_efficiency,
        )
    reason = "charge_now_avoids_costlier_later_import"
    if reserved_future_kwh > 0.01:
        reason = "later_cheap_slots_insufficient_capacity"
    return EconomicChargingPlan(
        "load",
        reason,
        target_soc,
        round(charge_now_kwh, 3),
        storage_cost_ct_kwh,
        minimum_future_price,
        roundtrip_efficiency,
        round(reserved_future_kwh, 3),
    )


def sfml_inverter_nominal_power_kw(hass: HomeAssistant) -> float:
    """Return SFML inverter_max_power unchanged; missing or invalid is 0 (unlimited)."""
    entries = hass.config_entries.async_entries("solar_forecast_ml")
    if not entries:
        return 0.0
    entry = entries[0]
    config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    if "inverter_max_power" not in config:
        return 0.0
    try:
        return float(config["inverter_max_power"])
    except (TypeError, ValueError):
        return 0.0


def force_price_window(
    *, min_soc: float, max_soc: float, current_soc: float | None, was_active: bool
) -> tuple[bool, str, float]:
    """Charge to max SoC while the price is under the force price.

    Start is max minus 7 points, stop is max minus 2 points. The same window
    already gated forecast force charging.
    """
    charge_target = float(max_soc)
    if current_soc is None:
        return (False, "soc_unavailable_force_charge_blocked", charge_target)
    stop_soc = max(float(min_soc), charge_target - 2.0)
    start_soc = max(float(min_soc), charge_target - 7.0)
    if stop_soc - start_soc < 2.0:
        start_soc = max(float(min_soc), stop_soc - 2.0)
    if was_active and float(current_soc) < stop_soc:
        return (True, "force_price_charge_continuous", charge_target)
    if not was_active:
        if float(current_soc) < start_soc:
            return (True, "force_price_charge_start", charge_target)
    return (False, "soc_reached_target_or_hysteresis", charge_target)


class SmartChargingManager:
    """Manages smart charging decisions based on solar forecast and battery SoC. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        forecast_reader: SolarForecastReader,
        battery_capacity_kwh: float,
        soc_sensor_entity: str,
        max_soc: int = DEFAULT_MAX_SOC,
        min_soc: int = DEFAULT_MIN_SOC,
        smart_charging_switch: str | None = None,
        home_consumption_sensor: str | None = None,
        solar_power_sensor: str | None = None,
        main_soc_sensor_entity: str | None = None,
        mode: str = DEFAULT_SMART_CHARGING_MODE,
        max_charge_power_kw: float = 0.0,
        inverter_nominal_power_kw: float = 0.0,
        target_soc: float | None = None,
    ) -> None:
        """Initialize the smart charging manager. @zara"""
        self._hass = hass
        self._forecast_reader = forecast_reader
        self._battery_capacity_kwh = battery_capacity_kwh
        self._soc_sensor_entity = soc_sensor_entity
        self._max_soc = max_soc
        self._min_soc = min_soc
        self._smart_charging_switch = smart_charging_switch
        self._home_consumption_sensor = home_consumption_sensor
        self._solar_power_sensor = solar_power_sensor
        self._main_soc_sensor_entity = main_soc_sensor_entity
        self._mode = mode or DEFAULT_SMART_CHARGING_MODE
        self._max_charge_power_kw = max_charge_power_kw
        self._inverter_nominal_power_kw = inverter_nominal_power_kw
        self._target_soc = target_soc if target_soc is not None else max_soc
        self._last_state = None
        self._normalize_config()

    def _normalize_config(self) -> None:
        try:
            self._battery_capacity_kwh = max(0.0, float(self._battery_capacity_kwh))
        except (TypeError, ValueError):
            self._battery_capacity_kwh = 0.0
        try:
            self._max_soc = max(0.0, min(100.0, float(self._max_soc)))
        except (TypeError, ValueError):
            self._max_soc = float(DEFAULT_MAX_SOC)
        try:
            self._min_soc = max(0.0, min(self._max_soc, float(self._min_soc)))
        except (TypeError, ValueError):
            self._min_soc = min(self._max_soc, float(DEFAULT_MIN_SOC))
        if self._mode not in (
            SMC_MODE_FORECAST,
            SMC_MODE_PRICE_BAND_SOC,
            SMC_MODE_COMBINED,
        ):
            self._mode = DEFAULT_SMART_CHARGING_MODE
        try:
            self._max_charge_power_kw = max(0.0, float(self._max_charge_power_kw))
        except (TypeError, ValueError):
            self._max_charge_power_kw = 0.0
        try:
            self._inverter_nominal_power_kw = max(
                0.0, float(self._inverter_nominal_power_kw)
            )
        except (TypeError, ValueError):
            self._inverter_nominal_power_kw = 0.0
        try:
            self._target_soc = float(getattr(self, "_target_soc", self._max_soc))
        except (TypeError, ValueError):
            self._target_soc = float(self._max_soc)
        self._target_soc = max(self._min_soc, min(self._max_soc, self._target_soc))

    @property
    def last_state(self) -> SmartChargingState | None:
        """Get the last computed state. @zara"""
        return self._last_state

    def _get_current_soc(self) -> float | None:
        """Read current battery SoC from HA sensor. @zara"""
        soc_entity = self._soc_sensor_entity
        state = self._hass.states.get(soc_entity)
        use_main_soc = False
        if state is None or state.state in ("unknown", "unavailable"):
            use_main_soc = True
        else:
            try:
                soc_value = float(state.state)
                if soc_value == 0.0 and self._main_soc_sensor_entity:
                    main_state = self._hass.states.get(self._main_soc_sensor_entity)
                    if main_state is not None and main_state.state not in (
                        "unknown",
                        "unavailable",
                    ):
                        main_soc = float(main_state.state)
                        if main_soc > 1.0:
                            use_main_soc = True
            except (ValueError, TypeError):
                use_main_soc = True
        if use_main_soc and self._main_soc_sensor_entity:
            soc_entity = self._main_soc_sensor_entity
            state = self._hass.states.get(soc_entity)
            if state is None or state.state in ("unknown", "unavailable"):
                return None
        elif use_main_soc:
            return
        try:
            soc_value = float(state.state)
            if 0 <= soc_value <= 100:
                return soc_value if True else None
            _LOGGER.warning("Battery SoC out of range (0-100): %.1f", soc_value)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid battery SoC value: %s", state.state)
            return

    def _get_sensor_value_kw(self, entity_id: str | None) -> float | None:
        """Read state of a sensor entity and convert to kW if it is in W. @zara"""
        if not entity_id:
            return
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return
        try:
            sensor_value = float(state.state)
            unit = (
                str(state.attributes.get("unit_of_measurement") or "").strip().upper()
            )
            if unit == "W" or (not unit and sensor_value > 100.0):
                return sensor_value / 1000.0
            return sensor_value
        except (ValueError, TypeError):
            return

    def _is_night(self) -> bool:
        """Determine if it is night based on sun.sun state. @zara"""
        sun_state = self._hass.states.get("sun.sun")
        if sun_state is not None:
            return sun_state.state == "below_horizon"
        now = dt_util.now()
        return now.hour < 6 or now.hour >= 21

    def _calculate_target_soc(
        self, solar_forecast_kwh: float | None, average_consumption_kwh: float | None
    ) -> float:
        """Calculate target SoC using consumption estimation and solar surplus room. @zara"""
        if self._battery_capacity_kwh <= 0:
            return float(self._max_soc)
        if average_consumption_kwh is None or average_consumption_kwh <= 0:
            if solar_forecast_kwh is None or solar_forecast_kwh <= 0:
                return float(self._max_soc)
            solar_room_percent = solar_forecast_kwh / self._battery_capacity_kwh * 100
            target_soc = self._max_soc - solar_room_percent
            return max(self._min_soc, min(self._max_soc, round(target_soc, 1)))
        now = dt_util.now()
        hour = now.hour
        daytime_consumption_kwh = average_consumption_kwh * 0.65
        if 6 <= hour < 22:
            remaining_hours = 22 - hour
            remaining_daytime_consumption_kwh = daytime_consumption_kwh * (
                remaining_hours / 16.0
            )
        else:
            remaining_daytime_consumption_kwh = daytime_consumption_kwh
        solar_kwh = solar_forecast_kwh or 0.0
        solar_surplus_kwh = max(0.0, solar_kwh - remaining_daytime_consumption_kwh)
        remaining_night_consumption_kwh = 0.0
        if hour >= 22 or hour < 6:
            night_consumption_kwh = average_consumption_kwh * 0.35
            night_consumption_kw = night_consumption_kwh / 8.0
            remaining_hours = 6 - hour if hour < 6 else 6 + (24 - hour)
            remaining_night_consumption_kwh = remaining_hours * night_consumption_kw
        available_capacity_kwh = self._battery_capacity_kwh - solar_surplus_kwh
        required_capacity_kwh = available_capacity_kwh + remaining_night_consumption_kwh
        target_soc = required_capacity_kwh / self._battery_capacity_kwh * 100
        minimum_target_soc = min(self._max_soc, self._min_soc + 5.0)
        target_soc = max(minimum_target_soc, min(self._max_soc, target_soc))
        return round(target_soc, 1)

    async def async_update(
        self,
        is_cheap: bool,
        current_price: float | None = None,
        future_total_price_slots: Sequence[object] | None = None,
        is_force_price: bool = False,
    ) -> SmartChargingState:
        """Update smart charging state and control the HA switch. @zara"""
        forecasts = await self._forecast_reader.async_get_forecasts()
        today_forecast = forecasts.get("today")
        tomorrow_forecast = forecasts.get("tomorrow")
        relevant_solar_kwh = (
            await self._forecast_reader.async_get_relevant_forecast_kwh()
        )
        average_consumption_kwh = (
            await self._forecast_reader.async_get_average_consumption_kwh()
        )
        target_soc = self._calculate_target_soc(
            relevant_solar_kwh, average_consumption_kwh
        )
        forecast_target_soc = target_soc
        band_target_soc = float(getattr(self, "_target_soc", self._max_soc))
        current_soc = self._get_current_soc()
        requested_grid_charge_kwh = 0.0
        economic_decision = "not_load"
        storage_cost_ct_kwh = None
        future_price_ct_kwh = None
        roundtrip_efficiency = None
        reserved_future_kwh = 0.0
        mode = getattr(self, "_mode", "forecast")
        now = dt_util.now()
        grid_charge_gate = resolve_grid_charge(getattr(self, "_hass", None), now)
        if grid_charge_gate is None:
            effective_cheap = bool(is_cheap)
            effective_force = bool(is_force_price)
        else:
            effective_cheap, effective_force = grid_charge_gate
        is_cheap = effective_cheap
        uses_economic_plan = (
            mode in ("forecast", "combined") and future_total_price_slots is not None
        )
        uses_price_band = mode in ("price_band_soc", "combined")
        should_charge = False
        reason = "smart_charging_disabled"
        if uses_economic_plan:
            economic_solar_kwh = relevant_solar_kwh
            if economic_solar_kwh is None and now.hour >= 22:
                economic_solar_kwh = 0.0
            remaining_slot_hours = max(
                0.016666666666666666, (60.0 - now.minute - now.second / 60.0) / 60.0
            )
            economic_plan = plan_economic_grid_charge(
                EconomicChargingInput(
                    current_price_ct_kwh=current_price,
                    future_price_ct_kwh=future_total_price_slots,
                    current_soc=current_soc,
                    battery_capacity_kwh=self._battery_capacity_kwh,
                    min_soc=self._min_soc,
                    max_soc=self._max_soc,
                    average_consumption_kwh=average_consumption_kwh,
                    solar_today_kwh=economic_solar_kwh,
                    solar_tomorrow_kwh=(
                        tomorrow_forecast.prediction_kwh if tomorrow_forecast else None
                    ),
                    max_charge_power_kw=self._max_charge_power_kw,
                    inverter_nominal_power_kw=self._inverter_nominal_power_kw,
                    inverter_partial_load_efficiency=0.9,
                    slot_duration_hours=remaining_slot_hours,
                    future_slot_duration_hours=1.0,
                    planning_time=now,
                    demand_deadline_hours=36.0,
                    storage_self_consumption_kw=0.02,
                    storage_hold_hours=8.0,
                )
            )
            target_soc = economic_plan.target_soc
            forecast_target_soc = economic_plan.target_soc
            should_charge = economic_plan.decision == "load"
            reason = economic_plan.reason
            requested_grid_charge_kwh = economic_plan.required_grid_charge_kwh
            economic_decision = economic_plan.decision
            if should_charge and (not is_cheap):
                should_charge = False
                reason = "price_above_threshold"
                requested_grid_charge_kwh = 0.0
                economic_decision = "wait"
            storage_cost_ct_kwh = economic_plan.current_storage_cost_ct_kwh
            future_price_ct_kwh = economic_plan.cheaper_future_price_ct_kwh
            roundtrip_efficiency = economic_plan.effective_roundtrip_efficiency
            reserved_future_kwh = economic_plan.reserved_future_grid_charge_kwh
        elif mode in ("forecast", "combined"):
            previous_mode = self._mode
            self._mode = SMC_MODE_FORECAST
            should_charge, reason = self._evaluate_charging(
                is_cheap=is_cheap,
                current_price=current_price,
                current_soc=current_soc,
                target_soc=target_soc,
                solar_kwh=relevant_solar_kwh,
                is_force_price=effective_force,
            )
            self._mode = previous_mode
            economic_decision = "load" if should_charge else "not_load"
        if uses_price_band:
            previous_mode = self._mode
            self._mode = SMC_MODE_PRICE_BAND_SOC
            band_should_charge, band_reason = self._evaluate_charging(
                is_cheap=is_cheap,
                current_price=current_price,
                current_soc=current_soc,
                target_soc=band_target_soc,
                solar_kwh=relevant_solar_kwh,
                is_force_price=effective_force,
            )
            self._mode = previous_mode
            band_effective_target = (
                float(self._max_soc) if effective_force else band_target_soc
            )
            if mode == "price_band_soc":
                target_soc = band_effective_target
                should_charge = band_should_charge
                reason = band_reason
                economic_decision = "load" if should_charge else "not_load"
            else:
                target_soc = max(float(target_soc), band_effective_target)
                if band_should_charge:
                    should_charge = True
                    reason = band_reason
                    economic_decision = "load"
                elif should_charge:
                    economic_decision = economic_decision
                else:
                    economic_decision = "not_load"
        if should_charge and (not is_cheap) and (not effective_force):
            should_charge = False
            reason = "price_above_threshold"
            requested_grid_charge_kwh = 0.0
            economic_decision = "wait"
        if effective_force and mode == "forecast":
            was_active = bool(
                self._last_state is not None and self._last_state.is_active
            )
            should_charge, reason, target_soc = force_price_window(
                min_soc=float(self._min_soc),
                max_soc=float(self._max_soc),
                current_soc=current_soc,
                was_active=was_active,
            )
            forecast_target_soc = target_soc
            economic_decision = "load" if should_charge else "not_load"
            if not should_charge:
                requested_grid_charge_kwh = 0.0
        elif effective_force and mode == "combined":
            should_charge = band_should_charge
            reason = band_reason
            target_soc = float(self._max_soc)
            economic_decision = "load" if should_charge else "not_load"
            if not should_charge:
                requested_grid_charge_kwh = 0.0
        charging_state = SmartChargingState(
            target_soc=target_soc,
            current_soc=current_soc,
            solar_forecast_kwh=relevant_solar_kwh,
            solar_forecast_today_kwh=(
                today_forecast.prediction_kwh if today_forecast else None
            ),
            solar_forecast_tomorrow_kwh=(
                tomorrow_forecast.prediction_kwh if tomorrow_forecast else None
            ),
            is_active=should_charge,
            reason=reason,
            requested_grid_charge_kwh=requested_grid_charge_kwh,
            economic_decision=economic_decision,
            effective_storage_cost_ct_kwh=storage_cost_ct_kwh,
            compared_future_price_ct_kwh=future_price_ct_kwh,
            effective_roundtrip_efficiency=roundtrip_efficiency,
            reserved_future_grid_charge_kwh=reserved_future_kwh,
            mode=mode,
            band_target_soc=band_target_soc if uses_price_band else None,
            forecast_target_soc=(
                forecast_target_soc if mode in ("forecast", "combined") else None
            ),
        )
        self._last_state = charging_state
        _LOGGER.debug(
            "Smart charging: active=%s, target_soc=%.1f%%, current_soc=%s, forecast=%.1f kWh, avg_consumption=%s, reason=%s",
            should_charge,
            target_soc,
            f"{current_soc:.1f}%" if current_soc is not None else "N/A",
            relevant_solar_kwh or 0,
            (
                f"{average_consumption_kwh:.2f} kWh"
                if average_consumption_kwh is not None
                else "N/A"
            ),
            reason,
        )
        await self._async_set_switch(should_charge, blocking=False)
        return charging_state

    async def async_force_off(self, reason: str = "smart_charging_disabled") -> None:
        """Turn off grid charging before removing the manager. @zara"""
        await self._async_set_switch(False, blocking=True)
        if self._last_state is not None:
            self._last_state.is_active = False
            self._last_state.reason = reason
        return None if True else None

    async def _async_set_switch(self, is_active: bool, blocking: bool) -> bool:
        if is_active:
            hour_is_cheap = resolve_hour_is_cheap(
                getattr(self, "_hass", None), dt_util.now()
            )
            if hour_is_cheap is not True:
                is_active = False
        if not self._smart_charging_switch:
            return False
        try:
            service_name = "turn_on" if is_active else "turn_off"
            domain, object_id = self._smart_charging_switch.split(".", 1)
            await self._hass.services.async_call(
                domain,
                service_name,
                {"entity_id": self._smart_charging_switch},
                blocking=blocking,
            )
            _LOGGER.info(
                "Called %s.%s on %s for smart charging",
                domain,
                service_name,
                self._smart_charging_switch,
            )
        except Exception as error:
            _LOGGER.error(
                "Failed to call switch service for %s: %s",
                self._smart_charging_switch,
                error,
            )
            return False
        return True

    def _get_standard_hysteresis_thresholds(
        self, target_soc: float
    ) -> tuple[float, float]:
        minimum_soc = float(self._min_soc)
        charge_target = max(minimum_soc, min(float(self._max_soc), float(target_soc)))
        soc_window = max(0.0, charge_target - minimum_soc)
        if soc_window <= 0.0:
            return (minimum_soc, minimum_soc)
        minimum_margin = 2.0
        maximum_margin = 7.0
        if soc_window <= minimum_margin:
            start_margin = soc_window / 2.0
        else:
            start_margin = min(maximum_margin, max(minimum_margin, soc_window * 0.4))
        start_soc = max(minimum_soc, charge_target - start_margin)
        return (start_soc, charge_target)

    def _evaluate_price_band_soc(
        self,
        is_cheap: bool,
        is_force_price: bool,
        current_soc: float | None,
        target_soc: float,
        was_active: bool,
    ) -> tuple[bool, str]:
        if current_soc is None:
            return (False, "soc_unavailable")
        if is_force_price:
            charge_target = float(self._max_soc)
            reason_prefix = "band_force_below_max_soc"
        else:
            if not is_cheap:
                return (False, "price_above_threshold")
            charge_target = float(target_soc)
            reason_prefix = "band_charge_below_target"
        stop_soc = charge_target - 1.0
        start_soc = charge_target - 2.0
        if was_active:
            if current_soc < stop_soc:
                return (True, f"{reason_prefix}_continuous")
            return (False, "band_soc_reached")
        if current_soc < start_soc:
            return (True, f"{reason_prefix}_start")
        return (False, "band_soc_reached")

    def _evaluate_charging(
        self,
        is_cheap: bool,
        current_price: float | None,
        current_soc: float | None,
        target_soc: float,
        solar_kwh: float | None,
        is_force_price: bool,
    ) -> tuple[bool, str]:
        """Evaluate whether grid charging should be active. @zara"""
        was_active = False
        if self._last_state is not None:
            was_active = self._last_state.is_active
        mode = getattr(self, "_mode", SMC_MODE_FORECAST)
        if mode in (SMC_MODE_PRICE_BAND_SOC, SMC_MODE_COMBINED):
            return self._evaluate_price_band_soc(
                bool(is_cheap),
                bool(is_force_price),
                current_soc,
                target_soc,
                was_active,
            )
        if is_force_price:
            force_active, reason, force_target = force_price_window(
                min_soc=float(self._min_soc),
                max_soc=float(self._max_soc),
                current_soc=current_soc,
                was_active=was_active,
            )
            return (force_active, reason)
        if not is_cheap:
            return (False, "price_above_threshold")
        night = self._is_night()
        standard_allowed = night and is_cheap
        if current_soc is None:
            if standard_allowed:
                return (False, "soc_unavailable_standard_charge_blocked")
            if not night:
                if is_cheap:
                    return (False, "daytime_standard_charge_disabled")
            if not is_cheap:
                return (False, "price_too_high")
            return (False, "soc_unavailable")
        if standard_allowed:
            start_soc, stop_soc = self._get_standard_hysteresis_thresholds(target_soc)
            if was_active:
                if current_soc < stop_soc:
                    return (True, "standard_charge_below_target_continuous")
            elif current_soc < start_soc:
                return (True, "standard_charge_below_target_start")
        if not night:
            if is_cheap:
                return (False, "daytime_standard_charge_disabled")
        if not is_cheap:
            return (False, "price_too_high")
        return (False, "soc_reached_target_or_hysteresis")

    def update_config(
        self,
        battery_capacity_kwh: float,
        soc_sensor_entity: str,
        max_soc: int,
        min_soc: int,
        smart_charging_switch: str | None = None,
        home_consumption_sensor: str | None = None,
        solar_power_sensor: str | None = None,
        main_soc_sensor_entity: str | None = None,
        mode: str = DEFAULT_SMART_CHARGING_MODE,
        max_charge_power_kw: float = 0.0,
        inverter_nominal_power_kw: float = 0.0,
        target_soc: float | None = None,
    ) -> None:
        """Update configuration values. @zara"""
        self._battery_capacity_kwh = battery_capacity_kwh
        self._soc_sensor_entity = soc_sensor_entity
        self._max_soc = max_soc
        self._min_soc = min_soc
        self._smart_charging_switch = smart_charging_switch
        self._home_consumption_sensor = home_consumption_sensor
        self._solar_power_sensor = solar_power_sensor
        self._main_soc_sensor_entity = main_soc_sensor_entity
        self._mode = mode
        self._max_charge_power_kw = max_charge_power_kw
        self._inverter_nominal_power_kw = inverter_nominal_power_kw
        if target_soc is not None:
            self._target_soc = target_soc
        elif not hasattr(self, "_target_soc"):
            self._target_soc = max_soc
        self._last_state = None
        self._normalize_config()
