# Powered by Solar Forecast ML by Zara-Toorox; subject to the repository LICENSE.
# Entire module statically verified against the recovered bytecode.
# Retained folded return expressions preserve verified compiler edges.
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

PV_ACTIVE_THRESHOLD_W = 50.0
PV_IDLE_THRESHOLD_W = 25.0
BATTERY_ACTIVE_THRESHOLD_W = 25.0
GRID_CHARGE_IDLE_THRESHOLD_W = 25.0
MAX_SAMPLE_GAP_SECONDS = 300.0
MIN_EFFICIENCY_ENERGY_KWH = 0.05
MAX_PLAUSIBLE_EFFICIENCY_PERCENT = 100.0
GOOD_MEASUREMENT_COVERAGE_PERCENT = 80.0
EFFICIENCY_CALCULATION_VERSION = 5
EFFICIENCY_WINDOW_MINUTES = 60
EFFICIENCY_WINDOW_GRACE_MINUTES = 10
MIN_WINDOW_COVERAGE_PERCENT = 90.0
EFFICIENCY_DAILY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stats_energy_efficiency_daily (
    date TEXT PRIMARY KEY, solar_dc_input_kwh REAL, inverter_ac_output_kwh REAL,
    battery_charge_dc_kwh REAL, battery_discharge_dc_kwh REAL,
    pv_dc_sample_kwh REAL NOT NULL DEFAULT 0, pv_ac_sample_kwh REAL NOT NULL DEFAULT 0,
    battery_dc_sample_kwh REAL NOT NULL DEFAULT 0, battery_ac_sample_kwh REAL NOT NULL DEFAULT 0,
    mixed_dc_sample_kwh REAL NOT NULL DEFAULT 0, mixed_ac_sample_kwh REAL NOT NULL DEFAULT 0,
    system_input_sample_kwh REAL NOT NULL DEFAULT 0, system_output_sample_kwh REAL NOT NULL DEFAULT 0,
    inverter_efficiency_percent REAL, battery_discharge_efficiency_percent REAL,
    mixed_efficiency_percent REAL, system_efficiency_percent REAL,
    ac_measurement_seconds REAL NOT NULL DEFAULT 0, battery_measurement_seconds REAL NOT NULL DEFAULT 0,
    pv_sample_seconds REAL NOT NULL DEFAULT 0, battery_sample_seconds REAL NOT NULL DEFAULT 0,
    mixed_sample_seconds REAL NOT NULL DEFAULT 0, system_sample_seconds REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
EFFICIENCY_META_TABLE_SQL = """CREATE TABLE IF NOT EXISTS stats_energy_efficiency_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""


@dataclass(slots=True)
class EfficiencyTotals:
    pv_dc_kwh: float = 0.0
    pv_ac_kwh: float = 0.0
    battery_dc_kwh: float = 0.0
    battery_ac_kwh: float = 0.0
    mixed_dc_kwh: float = 0.0
    mixed_ac_kwh: float = 0.0
    system_input_kwh: float = 0.0
    system_output_kwh: float = 0.0
    ac_measurement_seconds: float = 0.0
    battery_measurement_seconds: float = 0.0
    pv_sample_seconds: float = 0.0
    battery_sample_seconds: float = 0.0
    mixed_sample_seconds: float = 0.0
    system_sample_seconds: float = 0.0

    def add_sample(
        self,
        *,
        seconds: float,
        solar_dc_w: float | None,
        inverter_ac_w: float | None,
        battery_dc_w: float | None,
        grid_to_battery_w: float | None,
        battery_configured: bool,
        solar_to_battery_w: float | None = None,
    ) -> None:
        if seconds <= 0 or seconds > MAX_SAMPLE_GAP_SECONDS or inverter_ac_w is None:
            return None
        solar_power = max(0.0, solar_dc_w or 0.0)
        ac_output_power = max(0.0, inverter_ac_w)
        battery_power = battery_dc_w if battery_configured else 0.0
        grid_charge_power = max(0.0, grid_to_battery_w or 0.0)
        kwh_per_watt = seconds / 3600000.0
        self.ac_measurement_seconds += seconds
        if battery_configured and battery_dc_w is not None:
            self.battery_measurement_seconds += seconds
        battery_discharging = (
            battery_configured
            and battery_power is not None
            and (battery_power <= -BATTERY_ACTIVE_THRESHOLD_W)
        )
        if (
            solar_power >= PV_ACTIVE_THRESHOLD_W
            and (not battery_discharging)
            and (grid_charge_power <= GRID_CHARGE_IDLE_THRESHOLD_W)
        ):
            pv_inverter_input_power = _pv_inverter_input_w(
                solar_w=solar_power, battery_w=battery_power, solar_to_battery_w=solar_to_battery_w
            )
            if _is_plausible_conversion_sample(pv_inverter_input_power, ac_output_power):
                self.pv_dc_kwh += pv_inverter_input_power * kwh_per_watt
                self.pv_ac_kwh += ac_output_power * kwh_per_watt
                self.pv_sample_seconds += seconds
        if (
            battery_configured
            and battery_power is not None
            and (solar_power <= PV_IDLE_THRESHOLD_W)
            and (battery_power <= -BATTERY_ACTIVE_THRESHOLD_W)
            and (grid_charge_power <= GRID_CHARGE_IDLE_THRESHOLD_W)
            and _is_plausible_conversion_sample(-battery_power, ac_output_power)
        ):
            self.battery_dc_kwh += -battery_power * kwh_per_watt
            self.battery_ac_kwh += ac_output_power * kwh_per_watt
            self.battery_sample_seconds += seconds
        if battery_power is not None and grid_charge_power <= GRID_CHARGE_IDLE_THRESHOLD_W:
            pv_inverter_input_power = _pv_inverter_input_w(
                solar_w=solar_power, battery_w=battery_power, solar_to_battery_w=solar_to_battery_w
            )
            system_input_power = pv_inverter_input_power + max(0.0, -battery_power)
            system_output_power = ac_output_power
            if _is_plausible_conversion_sample(system_input_power, system_output_power):
                self.system_input_kwh += system_input_power * kwh_per_watt
                self.system_output_kwh += system_output_power * kwh_per_watt
                self.system_sample_seconds += seconds
                if battery_discharging and pv_inverter_input_power >= PV_ACTIVE_THRESHOLD_W:
                    self.mixed_dc_kwh += system_input_power * kwh_per_watt
                    self.mixed_ac_kwh += system_output_power * kwh_per_watt
                    self.mixed_sample_seconds += seconds
        return None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "pv_dc_sample_kwh": self.pv_dc_kwh,
            "pv_ac_sample_kwh": self.pv_ac_kwh,
            "battery_dc_sample_kwh": self.battery_dc_kwh,
            "battery_ac_sample_kwh": self.battery_ac_kwh,
            "mixed_dc_sample_kwh": self.mixed_dc_kwh,
            "mixed_ac_sample_kwh": self.mixed_ac_kwh,
            "system_input_sample_kwh": self.system_input_kwh,
            "system_output_sample_kwh": self.system_output_kwh,
            "inverter_efficiency_percent": efficiency_percent(self.pv_ac_kwh, self.pv_dc_kwh),
            "battery_discharge_efficiency_percent": efficiency_percent(
                self.battery_ac_kwh, self.battery_dc_kwh
            ),
            "mixed_efficiency_percent": efficiency_percent(self.mixed_ac_kwh, self.mixed_dc_kwh),
            "system_efficiency_percent": efficiency_percent(
                self.system_output_kwh, self.system_input_kwh
            ),
            "ac_measurement_seconds": self.ac_measurement_seconds,
            "battery_measurement_seconds": self.battery_measurement_seconds,
            "pv_sample_seconds": self.pv_sample_seconds,
            "battery_sample_seconds": self.battery_sample_seconds,
            "mixed_sample_seconds": self.mixed_sample_seconds,
            "system_sample_seconds": self.system_sample_seconds,
        }


def efficiency_percent(output_kwh: float, input_kwh: float) -> float | None:
    if input_kwh < MIN_EFFICIENCY_ENERGY_KWH:
        return
    return round(output_kwh / input_kwh * 100.0, 2)


def _pv_inverter_input_w(
    *, solar_w: float, battery_w: float | None, solar_to_battery_w: float | None
) -> float:
    solar_charge_power = max(
        0.0, solar_to_battery_w if solar_to_battery_w is not None else battery_w or 0.0
    )
    return max(0.0, solar_w - solar_charge_power)


def _is_plausible_conversion_sample(input_w: float, output_w: float) -> bool:
    if input_w < PV_ACTIVE_THRESHOLD_W:
        return False
    return output_w <= input_w


def build_energy_chain_summary(
    values: Any,
    *,
    net_autarky_percent: float | None,
    elapsed_seconds: float,
    battery_available: bool,
) -> dict[str, Any]:
    """Build display-safe energy-chain values from aligned daily measurements."""
    ac_measurement_seconds = max(0.0, _number(_value(values, "ac_measurement_seconds")) or 0.0)
    system_sample_seconds = max(0.0, _number(_value(values, "system_sample_seconds")) or 0.0)
    measurement_coverage_percent = round(
        min(100.0, system_sample_seconds / max(1.0, elapsed_seconds) * 100.0), 1
    )
    ac_sensor_coverage_percent = round(
        min(100.0, ac_measurement_seconds / max(1.0, elapsed_seconds) * 100.0), 1
    )
    dc_input_kwh = max(0.0, _number(_value(values, "system_input_sample_kwh")) or 0.0)
    ac_output_kwh = max(0.0, _number(_value(values, "system_output_sample_kwh")) or 0.0)
    system_efficiency = _number(_value(values, "system_efficiency_percent"))
    inconsistent_efficiency = system_efficiency is not None and (
        system_efficiency <= 0.0 or system_efficiency > MAX_PLAUSIBLE_EFFICIENCY_PERCENT
    )
    system_available = (
        dc_input_kwh >= MIN_EFFICIENCY_ENERGY_KWH
        and system_efficiency is not None
        and (0.0 < system_efficiency <= MAX_PLAUSIBLE_EFFICIENCY_PERCENT)
    )
    if inconsistent_efficiency:
        system_available = False
    if inconsistent_efficiency:
        data_quality = "inconsistent"
    elif not system_available:
        data_quality = "pending"
    elif measurement_coverage_percent >= GOOD_MEASUREMENT_COVERAGE_PERCENT:
        data_quality = "good"
    else:
        data_quality = "partial"
    net_autarky = _number(net_autarky_percent)
    if net_autarky is not None:
        net_autarky = round(min(100.0, max(0.0, net_autarky)), 1)
    loss_adjusted_autarky = None
    if net_autarky is not None and system_available:
        loss_adjusted_autarky = round(net_autarky * min(100.0, system_efficiency) / 100.0, 1)
    return (
        (
            (
                {
                    "available": True,
                    "data_quality": data_quality,
                    "measurement_coverage_percent": measurement_coverage_percent,
                    "ac_sensor_coverage_percent": ac_sensor_coverage_percent,
                    "net_autarky_percent": net_autarky,
                    "loss_adjusted_autarky_percent": loss_adjusted_autarky,
                    "system": {
                        "available": system_available,
                        "dc_input_kwh": round(dc_input_kwh, 3),
                        "useful_output_kwh": round(ac_output_kwh, 3),
                        "loss_kwh": round(max(0.0, dc_input_kwh - ac_output_kwh), 3),
                        "efficiency_percent": (
                            round(min(100.0, system_efficiency), 1) if system_available else None
                        ),
                    },
                    "pv": _build_efficiency_branch(
                        values,
                        input_key="pv_dc_sample_kwh",
                        output_key="pv_ac_sample_kwh",
                        efficiency_key="inverter_efficiency_percent",
                        seconds_key="pv_sample_seconds",
                    ),
                    "battery": (
                        _build_efficiency_branch(
                            values,
                            input_key="battery_dc_sample_kwh",
                            output_key="battery_ac_sample_kwh",
                            efficiency_key="battery_discharge_efficiency_percent",
                            seconds_key="battery_sample_seconds",
                        )
                        if battery_available
                        else None
                    ),
                    "mixed": (
                        _build_efficiency_branch(
                            values,
                            input_key="mixed_dc_sample_kwh",
                            output_key="mixed_ac_sample_kwh",
                            efficiency_key="mixed_efficiency_percent",
                            seconds_key="mixed_sample_seconds",
                        )
                        if battery_available
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


def _build_efficiency_branch(
    values: Any, *, input_key: str, output_key: str, efficiency_key: str, seconds_key: str
) -> dict[str, Any]:
    input_kwh = max(0.0, _number(_value(values, input_key)) or 0.0)
    output_kwh = max(0.0, _number(_value(values, output_key)) or 0.0)
    efficiency = _number(_value(values, efficiency_key))
    available = (
        input_kwh >= MIN_EFFICIENCY_ENERGY_KWH
        and efficiency is not None
        and (0.0 < efficiency <= MAX_PLAUSIBLE_EFFICIENCY_PERCENT)
    )
    return {
        "available": available,
        "input_kwh": round(input_kwh, 3),
        "output_kwh": round(output_kwh, 3),
        "efficiency_percent": round(min(100.0, efficiency), 1) if available else None,
        "sample_seconds": round(max(0.0, _number(_value(values, seconds_key)) or 0.0), 1),
    }


def aggregate_efficiency_rows(rows: Iterable[Any]) -> EfficiencyTotals:
    chronological_rows = sorted(rows, key=lambda row: _parse_timestamp(_value(row, "timestamp")))
    totals = EfficiencyTotals()
    if len(chronological_rows) < 2:
        return totals
    battery_configured = any(
        (_value(row, "battery_power_dc_w") is not None for row in chronological_rows)
    )
    windows = {}
    for index, row in enumerate(chronological_rows[:-1]):
        start_time = _parse_timestamp(_value(row, "timestamp"))
        end_time = _parse_timestamp(_value(chronological_rows[index + 1], "timestamp"))
        interval_seconds = (end_time - start_time).total_seconds()
        if interval_seconds <= 0 or interval_seconds > MAX_SAMPLE_GAP_SECONDS:
            continue
        _integrate_interval_into_windows(
            windows, start=start_time, end=end_time, row=row, battery_configured=battery_configured
        )
    latest_timestamp = _parse_timestamp(_value(chronological_rows[-1], "timestamp"))
    for window_start in sorted(windows):
        window = windows[window_start]
        finalized_after = window_start + timedelta(
            minutes=EFFICIENCY_WINDOW_MINUTES + EFFICIENCY_WINDOW_GRACE_MINUTES
        )
        if finalized_after > latest_timestamp:
            continue
        window.add_to(totals)
    return totals


@dataclass(slots=True)
class _EfficiencyWindow:
    ac_sensor_seconds: float = 0.0
    battery_sensor_seconds: float = 0.0
    valid_seconds: float = 0.0
    solar_dc_kwh: float = 0.0
    pv_inverter_dc_kwh: float = 0.0
    battery_discharge_dc_kwh: float = 0.0
    ac_kwh: float = 0.0

    def add_to(self, totals: EfficiencyTotals) -> None:
        window_seconds = EFFICIENCY_WINDOW_MINUTES * 60.0
        totals.ac_measurement_seconds += min(window_seconds, self.ac_sensor_seconds)
        totals.battery_measurement_seconds += min(window_seconds, self.battery_sensor_seconds)
        if self.valid_seconds / window_seconds * 100.0 < MIN_WINDOW_COVERAGE_PERCENT:
            return
        dc_input = self.pv_inverter_dc_kwh + self.battery_discharge_dc_kwh
        minimum_conversion_input_kwh = PV_ACTIVE_THRESHOLD_W * self.valid_seconds / 3600000.0
        if dc_input < minimum_conversion_input_kwh or self.ac_kwh > dc_input:
            return None
        totals.system_input_kwh += dc_input
        totals.system_output_kwh += self.ac_kwh
        totals.system_sample_seconds += self.valid_seconds
        battery_active_limit_kwh = BATTERY_ACTIVE_THRESHOLD_W * self.valid_seconds / 3600000.0
        pv_idle_limit_kwh = PV_IDLE_THRESHOLD_W * self.valid_seconds / 3600000.0
        if self.battery_discharge_dc_kwh <= battery_active_limit_kwh:
            totals.pv_dc_kwh += dc_input
            totals.pv_ac_kwh += self.ac_kwh
            totals.pv_sample_seconds += self.valid_seconds
        elif self.pv_inverter_dc_kwh <= pv_idle_limit_kwh:
            totals.battery_dc_kwh += dc_input
            totals.battery_ac_kwh += self.ac_kwh
            totals.battery_sample_seconds += self.valid_seconds
        else:
            totals.mixed_dc_kwh += dc_input
            totals.mixed_ac_kwh += self.ac_kwh
            totals.mixed_sample_seconds += self.valid_seconds
        return None if True else None


def _integrate_interval_into_windows(
    windows: dict[datetime, _EfficiencyWindow],
    *,
    start: datetime,
    end: datetime,
    row: Any,
    battery_configured: bool,
) -> None:
    solar_power = max(0.0, _number(_value(row, "solar_power_w")) or 0.0)
    ac_output_power = _number(_value(row, "inverter_ac_output_w"))
    raw_battery_power = _value(row, "battery_power_dc_w")
    battery_power = _number(raw_battery_power) if battery_configured else 0.0
    grid_charge_power = max(0.0, _number(_value(row, "grid_to_battery_w")) or 0.0)
    solar_to_battery_power = _number(_value(row, "solar_to_battery_w"))
    cursor = start
    while cursor < end:
        window_start = cursor.replace(
            minute=cursor.minute // EFFICIENCY_WINDOW_MINUTES * EFFICIENCY_WINDOW_MINUTES,
            second=0,
            microsecond=0,
        )
        window_end = window_start + timedelta(minutes=EFFICIENCY_WINDOW_MINUTES)
        segment_end = min(end, window_end)
        interval_seconds = (segment_end - cursor).total_seconds()
        window = windows.setdefault(window_start, _EfficiencyWindow())
        if ac_output_power is not None:
            window.ac_sensor_seconds += interval_seconds
        if raw_battery_power is not None:
            window.battery_sensor_seconds += interval_seconds
        if (
            ac_output_power is not None
            and battery_power is not None
            and (grid_charge_power <= GRID_CHARGE_IDLE_THRESHOLD_W)
        ):
            kwh_per_watt = interval_seconds / 3600000.0
            inverter_input_power = _pv_inverter_input_w(
                solar_w=solar_power,
                battery_w=battery_power,
                solar_to_battery_w=solar_to_battery_power,
            )
            window.valid_seconds += interval_seconds
            window.solar_dc_kwh += solar_power * kwh_per_watt
            window.pv_inverter_dc_kwh += inverter_input_power * kwh_per_watt
            window.battery_discharge_dc_kwh += max(0.0, -battery_power) * kwh_per_watt
            window.ac_kwh += max(0.0, ac_output_power) * kwh_per_watt
        cursor = segment_end


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
