# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Thermodynamics Engine Module.
Heat pump energy consumption calculation engine.

Calculates expected electrical consumption based on:
- Outdoor temperature (primary driver)
- Heat pump type and COP curve
- Building heat loss model (U-factor learned by ML)
- Wind chill effect (air-source heat pumps)
- Defrost cycle penalty

@zara
"""
from __future__ import annotations
import math
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Any
from ..const import (
    COP_MODE_DHW,
    COP_MODE_HEATING,
    DEFAULT_COP_RATED,
    DEFAULT_HEATING_CAPACITY_KW,
    DEFAULT_WP_TYPE,
    DHW_DAILY_DRAW_L_MAX,
    DHW_DAILY_DRAW_L_MIN,
    HEATING_LIMIT_DAILY_MEAN_C,
    SINK_ORIGIN_HEATING_CURVE,
    SINK_ORIGIN_MEASURED,
    SINK_ORIGIN_OPERATING_MODE,
    SINK_ORIGIN_UNCONFIGURED,
)

_LOGGER = logging.getLogger(__name__)
STC_OUTDOOR_TEMP = 7.0
STC_FLOW_TEMP = 35.0
DEFAULT_INDOOR_TEMP = 21.0
DEFAULT_DHW_SINK_TEMP = 55.0
SINK_COP_PENALTY_PER_K = 0.025
DESIGN_OUTDOOR_TEMP = -10.0
DHW_COLD_INLET_C = 10.0
WATER_HEAT_CAPACITY_KWH_PER_L_K = 0.001163
DHW_CIRCULATION_LOSS_KWH_PER_DAY = 1.5
HEATING_LIMIT_MIN_HOURS = 12
U_FACTOR_MIN_SAMPLES = 24
DHW_TAP_PROFILE = (
    0.01,
    0.008,
    0.008,
    0.008,
    0.01,
    0.016,
    0.08,
    0.13,
    0.09,
    0.03,
    0.02,
    0.02,
    0.05,
    0.07,
    0.025,
    0.02,
    0.02,
    0.03,
    0.08,
    0.11,
    0.09,
    0.045,
    0.02,
    0.01,
)


def _finite_optional(value: float | None) -> float | None:
    if value is None:
        return
    try:
        finite_value = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(finite_value):
        return
    return finite_value


COP_COEFFICIENTS = {
    "air_water": {"a": 0.0008, "b": 0.065, "c": 2.8, "min_cop": 1.5, "max_cop": 6.0},
    "brine_water": {"a": 0.0003, "b": 0.035, "c": 3.8, "min_cop": 2.5, "max_cop": 5.5},
    "water_water": {"a": 0.0002, "b": 0.025, "c": 4.2, "min_cop": 3.0, "max_cop": 6.0},
}
DEFROST_START_TEMP = 5.0
DEFROST_PEAK_TEMP = -2.0
DEFROST_MAX_PENALTY = 0.15
DEFROST_END_TEMP = -15.0
WIND_CHILL_FACTOR = 0.5
WIND_CHILL_MAX_REDUCTION = 5.0


@dataclass
class WeatherConditions:
    """Current weather conditions for thermodynamic calculations. @zara"""

    outdoor_temp: float
    wind_speed: float = 0.0
    humidity: float = 50.0
    cloud_cover: float = 50.0
    precipitation: float = 0.0

    @property
    def is_freezing(self) -> bool:
        """Check if conditions are below freezing. @zara"""
        return self.outdoor_temp <= 0.0

    @property
    def is_mild(self) -> bool:
        """Check if conditions are mild (limited heating needed). @zara"""
        return self.outdoor_temp >= 15.0


@dataclass
class CopEvaluation:
    """COP together with the sink temperature actually used."""

    cop: float
    sink_temp: float
    sink_origin: str
    outdoor_temp: float
    mode: str


@dataclass
class HeatPumpConfig:
    """Heat pump configuration. @zara"""

    wp_type: str = "air_water"
    heating_capacity_kw: float = 10.0
    cop_rated: float = 4.0
    indoor_temp: float = DEFAULT_INDOOR_TEMP
    target_temp: float = DEFAULT_INDOOR_TEMP


@dataclass
class HeatingResult:
    """Result of thermodynamic calculation for one hour. @zara"""

    heating_demand_kw: float
    cop: float
    electrical_consumption_kw: float
    defrost_factor: float
    effective_outdoor_temp: float
    is_heating_needed: bool
    sink_temp: float = STC_FLOW_TEMP
    sink_origin: str = SINK_ORIGIN_UNCONFIGURED

    @property
    def consumption_kwh(self) -> float:
        """Hourly consumption in kWh (assuming 1 hour). @zara"""
        return self.electrical_consumption_kw


@dataclass
class DailyConsumptionResult:
    """Result of daily consumption forecast. @zara"""

    total_consumption_kwh: float
    avg_cop: float
    peak_consumption_kw: float
    peak_hour: int
    heating_hours: int
    hourly_results: list


class ThermodynamicsEngine:
    """Heat pump thermodynamics calculation engine. @zara

    Calculates expected electrical consumption based on outdoor temperature,
    heat pump type, building characteristics, and weather conditions.
    The building-specific heat loss factor (U-factor) is initially estimated
    and then learned/calibrated by the ML system over time.
    """

    def __init__(
        self,
        wp_type: str = DEFAULT_WP_TYPE,
        heating_capacity_kw: float = DEFAULT_HEATING_CAPACITY_KW,
        cop_rated: float = DEFAULT_COP_RATED,
    ):
        """Initialize thermodynamics engine. @zara"""
        if wp_type not in COP_COEFFICIENTS:
            raise ValueError(f"Unsupported heat pump type: {wp_type}")
        if not (math.isfinite(float(cop_rated)) and 1.0 <= float(cop_rated) <= 10.0):
            raise ValueError("Rated COP must be finite and between 1 and 10")
        if not (
            math.isfinite(float(heating_capacity_kw))
            and 1.0 <= float(heating_capacity_kw) <= 100.0
        ):
            raise ValueError("Heating capacity must be finite and between 1 and 100 kW")
        self._wp_type = wp_type
        self._heating_capacity_kw = heating_capacity_kw
        self._cop_rated = cop_rated
        self._cop_coeff = COP_COEFFICIENTS[wp_type]
        reference_cop = self._raw_cop(STC_OUTDOOR_TEMP)
        self._cop_scale = cop_rated / reference_cop if reference_cop > 0 else 1.0
        self._u_factor = heating_capacity_kw / 25.0
        _LOGGER.debug(
            "ThermodynamicsEngine initialized: type=%s, capacity=%.1fkW, rated_cop=%.1f, cop_scale=%.3f, u_factor=%.3f",
            wp_type,
            heating_capacity_kw,
            cop_rated,
            self._cop_scale,
            self._u_factor,
        )

    @property
    def u_factor(self) -> float:
        """Current building heat loss factor (kW/K). @zara"""
        return self._u_factor

    @u_factor.setter
    def u_factor(self, value: float) -> None:
        """Set building heat loss factor (learned by calibrator). @zara"""
        self._u_factor = max(0.05, min(value, 5.0))

    def _raw_cop(self, outdoor_temp: float) -> float:
        """Calculate raw COP from temperature using quadratic model. @zara"""
        cop_coefficients = self._cop_coeff
        raw_cop = (
            cop_coefficients["a"] * outdoor_temp**2
            + cop_coefficients["b"] * outdoor_temp
            + cop_coefficients["c"]
        )
        return max(
            cop_coefficients["min_cop"], min(raw_cop, cop_coefficients["max_cop"])
        )

    def resolve_sink_temperature(
        self,
        *,
        flow_temp: float | None = None,
        outdoor_temp: float | None = None,
        mode: str | None = None,
        dhw_setpoint: float | None = None,
        design_flow_temp: float | None = None,
        design_outdoor_temp: float = DESIGN_OUTDOOR_TEMP,
        generator_flow_implausible: bool = False,
    ) -> tuple[float, str]:
        """Resolve the sink temperature through the measured → curve → mode cascade.

        ``generator_flow_implausible`` is the shared DHW generator-circuit
        decision. A rejected flow is not a measured sink; the setpoint / mode
        cascade below is unchanged. Heating never sets the flag.
        """
        if not generator_flow_implausible and flow_temp is not None:
            if math.isfinite(float(flow_temp)):
                return (float(flow_temp), SINK_ORIGIN_MEASURED) if True else None
        resolved_mode = (
            mode if mode in {COP_MODE_HEATING, COP_MODE_DHW} else COP_MODE_HEATING
        )
        design_sink_temp_c = _finite_optional(design_flow_temp)
        if (
            resolved_mode == COP_MODE_HEATING
            and outdoor_temp is not None
            and math.isfinite(float(outdoor_temp))
        ):
            if design_sink_temp_c is None:
                return (STC_FLOW_TEMP, SINK_ORIGIN_UNCONFIGURED) if True else None
            outdoor_span_c = STC_OUTDOOR_TEMP - design_outdoor_temp
            if outdoor_span_c == 0:
                curve_sink_temp_c = design_sink_temp_c
            else:
                interpolation_fraction = (
                    float(outdoor_temp) - design_outdoor_temp
                ) / outdoor_span_c
                curve_sink_temp_c = design_sink_temp_c + (
                    STC_FLOW_TEMP - design_sink_temp_c
                ) * max(0.0, min(interpolation_fraction, 1.5))
            return (curve_sink_temp_c, SINK_ORIGIN_HEATING_CURVE) if True else None
        if resolved_mode == COP_MODE_DHW:
            dhw_sink_temp_c = _finite_optional(dhw_setpoint)
            if dhw_sink_temp_c is None:
                return (
                    (DEFAULT_DHW_SINK_TEMP, SINK_ORIGIN_UNCONFIGURED) if True else None
                )
            return (dhw_sink_temp_c, SINK_ORIGIN_OPERATING_MODE) if True else None
        if design_sink_temp_c is not None:
            return (design_sink_temp_c, SINK_ORIGIN_OPERATING_MODE) if True else None
        return (STC_FLOW_TEMP, SINK_ORIGIN_UNCONFIGURED) if True else None

    def evaluate_cop(
        self,
        outdoor_temp: float,
        wind_speed: float = 0.0,
        *,
        flow_temp: float | None = None,
        mode: str | None = None,
        dhw_setpoint: float | None = None,
        design_flow_temp: float | None = None,
        generator_flow_implausible: bool = False,
    ) -> CopEvaluation:
        """Calculate COP including the sink-temperature lift."""
        resolved_mode = (
            mode if mode in {COP_MODE_HEATING, COP_MODE_DHW} else COP_MODE_HEATING
        )
        sink_temp_c, sink_origin = self.resolve_sink_temperature(
            flow_temp=flow_temp,
            outdoor_temp=outdoor_temp,
            mode=resolved_mode,
            dhw_setpoint=dhw_setpoint,
            design_flow_temp=design_flow_temp,
            generator_flow_implausible=generator_flow_implausible,
        )
        effective_outdoor_temp_c = self._get_effective_temp(outdoor_temp, wind_speed)
        raw_cop = self._raw_cop(effective_outdoor_temp_c)
        scaled_cop = raw_cop * self._cop_scale
        sink_lift_c = sink_temp_c - STC_FLOW_TEMP
        sink_adjusted_cop = scaled_cop * max(
            0.4, 1.0 - SINK_COP_PENALTY_PER_K * sink_lift_c
        )
        bounded_cop = max(
            self._cop_coeff["min_cop"],
            min(sink_adjusted_cop, self._cop_coeff["max_cop"]),
        )
        return CopEvaluation(
            cop=bounded_cop,
            sink_temp=sink_temp_c,
            sink_origin=sink_origin,
            outdoor_temp=effective_outdoor_temp_c,
            mode=resolved_mode,
        )

    def calculate_cop(
        self, outdoor_temp: float, wind_speed: float = 0.0, **kwargs: Any
    ) -> float:
        """Calculate COP at given conditions. @zara

        For air-source heat pumps, wind chill reduces effective temperature.
        Optional kwargs: flow_temp, mode, dhw_setpoint, design_flow_temp.
        """
        return self.evaluate_cop(outdoor_temp, wind_speed, **kwargs).cop

    def _get_effective_temp(self, outdoor_temp: float, wind_speed: float) -> float:
        """Calculate effective outdoor temperature with wind chill. @zara

        Wind chill only applies to air-source heat pumps.
        """
        if self._wp_type != "air_water":
            return outdoor_temp
        if wind_speed <= 0:
            return outdoor_temp
        wind_chill_reduction_c = min(
            wind_speed * WIND_CHILL_FACTOR, WIND_CHILL_MAX_REDUCTION
        )
        return outdoor_temp - wind_chill_reduction_c

    def calculate_defrost_factor(
        self, outdoor_temp: float, humidity: float = 50.0
    ) -> float:
        """Calculate defrost cycle penalty factor. @zara

        Only applies to air-source heat pumps.
        Defrost cycles are most frequent around 0°C to -5°C with high humidity.
        Returns factor >= 1.0 (1.0 = no penalty, 1.15 = 15% extra consumption).
        """
        if self._wp_type != "air_water":
            return 1.0
        if outdoor_temp > DEFROST_START_TEMP:
            return 1.0
        if outdoor_temp < DEFROST_END_TEMP:
            return 1.0 + DEFROST_MAX_PENALTY * 0.3
        if outdoor_temp >= DEFROST_PEAK_TEMP:
            defrost_ramp_fraction = (DEFROST_START_TEMP - outdoor_temp) / (
                DEFROST_START_TEMP - DEFROST_PEAK_TEMP
            )
        else:
            defrost_ramp_fraction = (outdoor_temp - DEFROST_END_TEMP) / (
                DEFROST_PEAK_TEMP - DEFROST_END_TEMP
            )
        defrost_ramp_fraction = max(0.0, min(defrost_ramp_fraction, 1.0))
        defrost_penalty = DEFROST_MAX_PENALTY * defrost_ramp_fraction
        humidity_multiplier = 1.0 + max(0.0, humidity - 60.0) / 100.0
        defrost_penalty *= humidity_multiplier
        return 1.0 + min(defrost_penalty, DEFROST_MAX_PENALTY * 1.4)

    def calculate_heating_demand(
        self,
        outdoor_temp: float,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        wind_speed: float = 0.0,
    ) -> float:
        """Calculate thermal heating demand in kW. @zara

        Uses simplified heat loss model: Q = U * (T_in - T_eff_out)
        U-factor is learned by the calibrator over time.
        """
        effective_outdoor_temp_c = self._get_effective_temp(outdoor_temp, wind_speed)
        temperature_difference_c = indoor_temp - effective_outdoor_temp_c
        if temperature_difference_c <= 0:
            return 0.0
        heating_demand_kw = self._u_factor * temperature_difference_c
        return min(heating_demand_kw, self._heating_capacity_kw)

    def calculate_dhw_thermal_kw(
        self,
        hour: int | None,
        *,
        has_dhw: bool = False,
        dhw_setpoint: float | None = None,
        storage_volume_l: float | None = None,
        dhw_daily_draw_l: float | None = None,
        has_circulation: bool = False,
    ) -> float:
        """Hourly DHW thermal demand from daily tap volume, setpoint and profile.

        Storage volume is not a tap-energy driver. Standby losses and cycling
        stay on the empirical thermal-loss path that already uses
        ``storage_volume_l``. Missing draw volume yields 0.0 with no substitute.
        """
        if not has_dhw or hour is None:
            return 0.0 if True else None
        daily_draw_l = _finite_optional(dhw_daily_draw_l)
        dhw_setpoint_c = _finite_optional(dhw_setpoint)
        unused_storage_volume_l = storage_volume_l
        if daily_draw_l is None or dhw_setpoint_c is None:
            return 0.0 if True else None
        if not DHW_DAILY_DRAW_L_MIN <= daily_draw_l <= DHW_DAILY_DRAW_L_MAX:
            return 0.0 if True else None
        water_temperature_lift_c = max(0.0, dhw_setpoint_c - DHW_COLD_INLET_C)
        if water_temperature_lift_c <= 0:
            return 0.0 if True else None
        daily_dhw_energy_kwh = (
            daily_draw_l * WATER_HEAT_CAPACITY_KWH_PER_L_K * water_temperature_lift_c
        )
        profile_hour = int(hour) % 24
        hourly_dhw_thermal_kw = daily_dhw_energy_kwh * DHW_TAP_PROFILE[profile_hour]
        if has_circulation:
            hourly_dhw_thermal_kw += DHW_CIRCULATION_LOSS_KWH_PER_DAY / 24.0
        return hourly_dhw_thermal_kw if True else None

    def calculate_hourly_consumption(
        self,
        weather: WeatherConditions,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        *,
        flow_temp: float | None = None,
        mode: str | None = None,
        dhw_setpoint: float | None = None,
        design_flow_temp: float | None = None,
        hour: int | None = None,
        has_dhw: bool = False,
        storage_volume_l: float | None = None,
        dhw_daily_draw_l: float | None = None,
        has_circulation: bool = False,
        suppress_space_heating: bool = False,
        generator_flow_implausible: bool = False,
    ) -> HeatingResult:
        """Calculate expected electrical consumption for one hour. @zara"""
        effective_outdoor_temp_c = self._get_effective_temp(
            weather.outdoor_temp, weather.wind_speed
        )
        if suppress_space_heating:
            space_heating_demand_kw = 0.0
        else:
            space_heating_demand_kw = self.calculate_heating_demand(
                weather.outdoor_temp, indoor_temp, weather.wind_speed
            )
        dhw_thermal_kw = self.calculate_dhw_thermal_kw(
            hour,
            has_dhw=has_dhw,
            dhw_setpoint=dhw_setpoint,
            storage_volume_l=storage_volume_l,
            dhw_daily_draw_l=dhw_daily_draw_l,
            has_circulation=has_circulation,
        )
        resolved_mode = (
            mode if mode in {COP_MODE_HEATING, COP_MODE_DHW} else COP_MODE_HEATING
        )
        heating_flow_temp_c = flow_temp if resolved_mode != COP_MODE_DHW else None
        dhw_flow_temp_c = flow_temp if resolved_mode == COP_MODE_DHW else None
        heating_cop_evaluation = self.evaluate_cop(
            weather.outdoor_temp,
            weather.wind_speed,
            flow_temp=heating_flow_temp_c,
            mode=COP_MODE_HEATING,
            dhw_setpoint=dhw_setpoint,
            design_flow_temp=design_flow_temp,
        )
        dhw_cop_evaluation = self.evaluate_cop(
            weather.outdoor_temp,
            weather.wind_speed,
            flow_temp=dhw_flow_temp_c,
            mode=COP_MODE_DHW,
            dhw_setpoint=dhw_setpoint,
            design_flow_temp=design_flow_temp,
            generator_flow_implausible=generator_flow_implausible,
        )
        defrost_factor = self.calculate_defrost_factor(
            weather.outdoor_temp, weather.humidity
        )
        electrical_consumption_kw = 0.0
        if space_heating_demand_kw > 0.01:
            electrical_consumption_kw += (
                space_heating_demand_kw / heating_cop_evaluation.cop * defrost_factor
            )
        if dhw_thermal_kw > 0.0:
            electrical_consumption_kw += (
                dhw_thermal_kw / dhw_cop_evaluation.cop * defrost_factor
            )
        total_thermal_demand_kw = space_heating_demand_kw + dhw_thermal_kw
        if dhw_thermal_kw > space_heating_demand_kw:
            dominant_cop = dhw_cop_evaluation.cop
            sink_temp_c, sink_origin = (
                dhw_cop_evaluation.sink_temp,
                dhw_cop_evaluation.sink_origin,
            )
        else:
            dominant_cop = heating_cop_evaluation.cop
            sink_temp_c, sink_origin = (
                heating_cop_evaluation.sink_temp,
                heating_cop_evaluation.sink_origin,
            )
        return HeatingResult(
            heating_demand_kw=space_heating_demand_kw,
            cop=dominant_cop,
            electrical_consumption_kw=round(electrical_consumption_kw, 3),
            defrost_factor=defrost_factor if total_thermal_demand_kw > 0 else 1.0,
            effective_outdoor_temp=effective_outdoor_temp_c,
            is_heating_needed=total_thermal_demand_kw > 0.01,
            sink_temp=sink_temp_c,
            sink_origin=sink_origin,
        )

    def calculate_daily_consumption(
        self,
        hourly_temps: list[float],
        hourly_wind: list[float] | None = None,
        hourly_humidity: list[float] | None = None,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        *,
        flow_temp: float | None = None,
        mode: str | None = None,
        dhw_setpoint: float | None = None,
        design_flow_temp: float | None = None,
        has_dhw: bool = False,
        storage_volume_l: float | None = None,
        dhw_daily_draw_l: float | None = None,
        has_circulation: bool = False,
    ) -> DailyConsumptionResult:
        "Calculate expected daily consumption from hourly weather forecast. @zara\n\nArgs:\n    hourly_temps: List of 24 outdoor temperatures (°C)\n    hourly_wind: Optional list of 24 wind speeds (m/s)\n    hourly_humidity: Optional list of 24 humidity values (%)\n    indoor_temp: Indoor target temperature\n\nReturns:\n    DailyConsumptionResult with total and hourly breakdown\n"
        if not hourly_temps:
            return DailyConsumptionResult(
                total_consumption_kwh=0.0,
                avg_cop=0.0,
                peak_consumption_kw=0.0,
                peak_hour=0,
                heating_hours=0,
                hourly_results=[],
            )
        suppress_space_heating = False
        sample_count = min(len(hourly_temps), 24)
        if sample_count:
            reference_day = datetime(2000, 1, 1, tzinfo=timezone.utc)
            wind_samples = hourly_wind or []
            weather_samples = []
            for hour_index in range(sample_count):
                wind_speed_mps = (
                    wind_samples[hour_index] if hour_index < len(wind_samples) else 0.0
                )
                weather_samples.append(
                    (
                        reference_day.replace(hour=hour_index),
                        hourly_temps[hour_index],
                        wind_speed_mps,
                    )
                )
            suppressed_days = space_heating_suppressed_by_day(
                self, weather_samples, timezone.utc
            )
            suppress_space_heating = suppressed_days.get(reference_day.date(), False)
        padded_temperatures_c = (hourly_temps + [hourly_temps[-1]] * 24)[:24]
        padded_wind_mps = ((hourly_wind or [0.0] * 24) + [0.0] * 24)[:24]
        padded_humidity_percent = ((hourly_humidity or [50.0] * 24) + [50.0] * 24)[:24]
        hourly_results = []
        total_consumption_kwh = 0.0
        cop_sum = 0.0
        cop_count = 0
        peak_consumption_kw = 0.0
        peak_hour = 0
        heating_hours = 0
        for hour_index in range(24):
            weather_conditions = WeatherConditions(
                outdoor_temp=padded_temperatures_c[hour_index],
                wind_speed=padded_wind_mps[hour_index],
                humidity=padded_humidity_percent[hour_index],
            )
            hourly_result = self.calculate_hourly_consumption(
                weather_conditions,
                indoor_temp,
                flow_temp=flow_temp,
                mode=mode,
                dhw_setpoint=dhw_setpoint,
                design_flow_temp=design_flow_temp,
                hour=hour_index,
                has_dhw=has_dhw,
                storage_volume_l=storage_volume_l,
                dhw_daily_draw_l=dhw_daily_draw_l,
                has_circulation=has_circulation,
                suppress_space_heating=suppress_space_heating,
            )
            hourly_results.append(hourly_result)
            total_consumption_kwh += hourly_result.consumption_kwh
            if hourly_result.is_heating_needed:
                cop_sum += hourly_result.cop
                cop_count += 1
                heating_hours += 1
                if hourly_result.electrical_consumption_kw > peak_consumption_kw:
                    peak_consumption_kw = hourly_result.electrical_consumption_kw
                    peak_hour = hour_index
        average_cop = cop_sum / cop_count if cop_count > 0 else 0.0
        return DailyConsumptionResult(
            total_consumption_kwh=round(total_consumption_kwh, 2),
            avg_cop=round(average_cop, 2),
            peak_consumption_kw=round(peak_consumption_kw, 3),
            peak_hour=peak_hour,
            heating_hours=heating_hours,
            hourly_results=hourly_results,
        )


def local_calendar_date(moment: datetime, tz: tzinfo) -> date:
    """Local calendar date of one forecast timestamp."""
    if moment.tzinfo is None:
        local_moment = moment.replace(tzinfo=tz)
    else:
        local_moment = moment.astimezone(tz)
    return local_moment.date() if True else None


def daily_mean_suppresses_space_heating(effective_outdoor_c: list[float]) -> bool:
    """True when enough wind-effective samples average at or above the heating limit."""
    if len(effective_outdoor_c) < HEATING_LIMIT_MIN_HOURS:
        return False
    mean_outdoor_temp_c = sum(effective_outdoor_c) / len(effective_outdoor_c)
    return mean_outdoor_temp_c >= HEATING_LIMIT_DAILY_MEAN_C


def space_heating_suppressed_by_day(
    engine: ThermodynamicsEngine,
    samples: list[tuple[datetime, float | None, float | None]],
    tz: tzinfo,
    calibrator: Any | None = None,
    has_dhw: bool = False,
) -> dict[date, bool]:
    """Whether space heating is off for each local day.

    Each sample is ``(timestamp, outdoor_c, wind_m_s)``. Hours without a finite
    outdoor forecast are ignored. Wind chill uses the engine effective temperature.
    A day is suppressed only with at least ``HEATING_LIMIT_MIN_HOURS`` samples
    whose mean is at or above ``HEATING_LIMIT_DAILY_MEAN_C``.

    The limit stays lifted while the calibrator says so. Evidence lifts it
    immediately; seven consecutive suppressible days without renewed evidence
    restore it. A calibrator error keeps the temperature-only decision.
    Bucket confidence alone does not lift the limit.
    """
    temperatures_by_day = {}
    for sample_datetime, raw_outdoor_temp_c, wind_speed_mps in samples:
        calendar_date = local_calendar_date(sample_datetime, tz)
        temperatures_by_day.setdefault(calendar_date, [])
        outdoor_temp_c = _finite_optional(raw_outdoor_temp_c)
        if outdoor_temp_c is not None:
            finite_wind_mps = _finite_optional(wind_speed_mps)
            if finite_wind_mps is None:
                finite_wind_mps = 0.0
            temperatures_by_day[calendar_date].append(
                engine._get_effective_temp(outdoor_temp_c, finite_wind_mps)
            )
    heating_limit_lifted = False
    if calibrator is not None:
        try:
            check_heating_limit_evidence = getattr(
                calibrator, "heating_limit_is_lifted", None
            )
            if check_heating_limit_evidence is None:
                check_heating_limit_evidence = calibrator.heating_above_limit_proven
            heating_limit_lifted = bool(check_heating_limit_evidence(bool(has_dhw)))
        except Exception:
            _LOGGER.warning(
                "Heating-limit evidence check failed; keeping the temperature limit"
            )
            heating_limit_lifted = False
    suppressed_days = {}
    for calendar_date, effective_temperatures_c in temperatures_by_day.items():
        suppress_day = daily_mean_suppresses_space_heating(effective_temperatures_c)
        if suppress_day and heating_limit_lifted:
            suppress_day = False
        suppressed_days[calendar_date] = suppress_day
    return suppressed_days


async def learn_consumption_hour(
    calibrator: Any,
    engine: ThermodynamicsEngine,
    *,
    above_heating_limit: bool,
    has_dhw: bool,
    actual_kwh: float,
    outdoor_temp: float,
    indoor_temp: float,
    wind_speed: float,
    humidity: float,
    hour: int,
    sink_kwargs: dict | None = None,
    dhw_kwargs: dict | None = None,
    limit_lifted: bool = False,
) -> float | None:
    """Learn one hour against the physics the forecast would have used.

    Days above the heating limit always keep the evidence path: hot-water
    physics in the DHW-only bucket, or the measured kWh for the no-hot-water
    counter. Once the limit is lifted they also learn unsuppressed physics,
    the temperature bucket and the U-factor. Days below the limit do only that.
    """
    no_dhw_observed_kwh = None
    if above_heating_limit and (not has_dhw):
        try:
            observed_kwh = float(actual_kwh)
        except (TypeError, ValueError):
            observed_kwh = None
        if (
            observed_kwh is not None
            and math.isfinite(observed_kwh)
            and (observed_kwh >= 0.0)
        ):
            no_dhw_observed_kwh = observed_kwh
    learn_unsuppressed = not above_heating_limit or bool(limit_lifted)
    learn_dhw_only = above_heating_limit and bool(has_dhw)
    if not learn_unsuppressed and (not learn_dhw_only):
        return no_dhw_observed_kwh
    sink_parameters = dict(sink_kwargs or {})
    dhw_parameters = dict(dhw_kwargs or {})
    dhw_parameters["has_dhw"] = bool(has_dhw)
    weather_conditions = WeatherConditions(
        outdoor_temp=outdoor_temp, wind_speed=wind_speed, humidity=humidity
    )
    if learn_dhw_only:
        dhw_only_result = engine.calculate_hourly_consumption(
            weather_conditions,
            indoor_temp=indoor_temp,
            hour=hour,
            suppress_space_heating=True,
            **sink_parameters,
            **dhw_parameters,
        )
        await calibrator.update_calibration(
            predicted_kwh=float(dhw_only_result.consumption_kwh),
            actual_kwh=actual_kwh,
            outdoor_temp=outdoor_temp,
            indoor_temp=indoor_temp,
            wind_speed=wind_speed,
            hour=hour,
            is_dhw_only=True,
            dhw_electrical_kwh=float(dhw_only_result.consumption_kwh),
        )
    if not learn_unsuppressed:
        return no_dhw_observed_kwh
    full_result = engine.calculate_hourly_consumption(
        weather_conditions,
        indoor_temp=indoor_temp,
        hour=hour,
        suppress_space_heating=False,
        **sink_parameters,
        **dhw_parameters,
    )
    full_consumption_kwh = float(full_result.consumption_kwh)
    heating_only_result = engine.calculate_hourly_consumption(
        weather_conditions,
        indoor_temp=indoor_temp,
        hour=hour,
        suppress_space_heating=False,
        **sink_parameters,
        **{**dhw_parameters, "has_dhw": False},
    )
    dhw_electrical_kwh = max(
        0.0, full_consumption_kwh - float(heating_only_result.consumption_kwh)
    )
    is_dhw_only = bool(has_dhw) and float(full_result.heating_demand_kw) <= 0.01
    heating_cop, learning_defrost_factor = _learning_cop_and_defrost(
        engine, outdoor_temp, wind_speed, sink_parameters, full_result
    )
    await calibrator.update_calibration(
        predicted_kwh=full_consumption_kwh,
        actual_kwh=actual_kwh,
        outdoor_temp=outdoor_temp,
        indoor_temp=indoor_temp,
        wind_speed=wind_speed,
        hour=hour,
        is_dhw_only=is_dhw_only,
        dhw_electrical_kwh=dhw_electrical_kwh,
        heating_cop=heating_cop,
        defrost_factor=learning_defrost_factor,
    )
    return no_dhw_observed_kwh


def _learning_cop_and_defrost(
    engine: ThermodynamicsEngine,
    outdoor_temp: float,
    wind_speed: float,
    sink_kwargs: dict,
    full: HeatingResult,
) -> tuple[float, float]:
    """Heating COP and defrost factor the engine used for this hour."""
    configured_mode = sink_kwargs.get("mode")
    resolved_mode = (
        configured_mode
        if configured_mode in {COP_MODE_HEATING, COP_MODE_DHW}
        else COP_MODE_HEATING
    )
    configured_flow_temp_c = sink_kwargs.get("flow_temp")
    heating_flow_temp_c = (
        configured_flow_temp_c if resolved_mode != COP_MODE_DHW else None
    )
    heating_cop_evaluation = engine.evaluate_cop(
        outdoor_temp,
        wind_speed,
        flow_temp=heating_flow_temp_c,
        mode=COP_MODE_HEATING,
        dhw_setpoint=sink_kwargs.get("dhw_setpoint"),
        design_flow_temp=sink_kwargs.get("design_flow_temp"),
    )
    try:
        learning_defrost_factor = float(full.defrost_factor)
    except (TypeError, ValueError):
        learning_defrost_factor = 1.0
    if not math.isfinite(learning_defrost_factor) or learning_defrost_factor <= 0.0:
        learning_defrost_factor = 1.0
    return (float(heating_cop_evaluation.cop), learning_defrost_factor)
