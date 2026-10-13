# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Cached household energy context for advisory consumers."""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOGGER = logging.getLogger(__name__)
_HISTORY_DAYS = 90
_TARGET_SAMPLES = 4
_FIELDS = (
    "home_consumption_kwh",
    "consumer_heatpump_kwh",
    "consumer_heatingrod_kwh",
    "consumer_wallbox_kwh",
    "solar_to_battery_kwh",
    "battery_to_house_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "solar_yield_kwh",
)
_REQUIRED_FIELDS = frozenset(
    {
        "grid_export_kwh",
        "solar_yield_kwh",
        "consumer_wallbox_kwh",
        "home_consumption_kwh",
        "solar_to_battery_kwh",
        "consumer_heatpump_kwh",
        "consumer_heatingrod_kwh",
    }
)
_FIELD_ORIGINS = {field: f"stats_hourly_billing.{field}" for field in _FIELDS}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return
    return numeric_value if isfinite(numeric_value) and numeric_value >= 0 else None


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return
    return (
        (numeric_value if True else None)
        if isfinite(numeric_value) and minimum <= numeric_value <= maximum
        else None
    )


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return


def _row_has_field(row: Any, key: str) -> bool:
    if isinstance(row, dict):
        return key in row
    row_keys = getattr(row, "keys", None)
    return key in row_keys() if callable(row_keys) else False


def _sample(row: Any) -> dict[str, float | None] | None:
    try:
        sample_date = date.fromisoformat(str(_row_value(row, "date")))
        hour = int(_row_value(row, "hour"))
    except (TypeError, ValueError):
        return
    if hour < 0 or hour > 23:
        return None
    field_values = {
        field_name: _number(_row_value(row, field_name)) for field_name in _FIELDS
    }
    consumer_components = (
        field_values["home_consumption_kwh"],
        field_values["consumer_heatpump_kwh"],
        field_values["consumer_heatingrod_kwh"],
        field_values["consumer_wallbox_kwh"],
    )
    if all((component_value is not None for component_value in consumer_components)):
        home_consumption_kwh, heatpump_kwh, heatingrod_kwh, wallbox_kwh = (
            consumer_components
        )
        large_consumer_kwh = heatpump_kwh + heatingrod_kwh + wallbox_kwh
        household_base_load_kwh = (
            max(0.0, home_consumption_kwh - large_consumer_kwh)
            if home_consumption_kwh + 0.05 >= large_consumer_kwh
            else home_consumption_kwh
        )
    else:
        household_base_load_kwh = None
    field_values.update(
        {
            "weekday": float(sample_date.weekday()),
            "hour": float(hour),
            "household_base_load_kwh": household_base_load_kwh,
        }
    )
    return field_values


def build_energy_context(
    rows: list[Any],
    *,
    generated_at: datetime,
    available_fields: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build a robust weekday/hour profile without double-counting consumers."""
    valid_samples = [
        sample for billing_row in rows if (sample := _sample(billing_row)) is not None
    ]
    if available_fields is None:
        available_fields = {
            field_name
            for field_name in _FIELDS
            if rows
            if all((_row_has_field(billing_row, field_name) for billing_row in rows))
        }
    else:
        available_fields = set(available_fields)
    source_availability = {}
    for field_name in _FIELDS:
        valid_sample_count = sum(
            (sample_row[field_name] is not None for sample_row in valid_samples)
        )
        source_availability[field_name] = {
            "available": field_name in available_fields and valid_sample_count > 0,
            "column_present": field_name in available_fields,
            "valid_sample_count": valid_sample_count,
            "invalid_sample_count": len(valid_samples) - valid_sample_count,
            "origin": _FIELD_ORIGINS[field_name],
        }
    complete_samples = [
        sample_row
        for sample_row in valid_samples
        if sample_row["household_base_load_kwh"] is not None
        if all((sample_row[field_name] is not None for field_name in _REQUIRED_FIELDS))
    ]
    weekday_hour_samples = defaultdict(list)
    hour_samples = defaultdict(list)
    weekday_hour_battery = defaultdict(list)
    hour_battery = defaultdict(list)
    for sample in valid_samples:
        battery_delivery_kwh = sample["battery_to_house_kwh"]
        if battery_delivery_kwh is not None:
            weekday = int(sample["weekday"])
            hour = int(sample["hour"])
            weekday_hour_battery[weekday, hour].append(battery_delivery_kwh)
            hour_battery[hour].append(battery_delivery_kwh)
    for sample in complete_samples:
        weekday = int(sample["weekday"])
        hour = int(sample["hour"])
        weekday_hour_samples[weekday, hour].append(sample)
        hour_samples[hour].append(sample)
    profile = []
    for weekday in range(7):
        for hour in range(24):
            weekday_samples = weekday_hour_samples.get((weekday, hour)) or []
            hour_fallback_samples = hour_samples.get(hour) or []
            selected_samples = weekday_samples or hour_fallback_samples
            profile_origin = (
                "weekday_hour_profile"
                if weekday_samples
                else "hour_profile_fallback" if hour_fallback_samples else "unavailable"
            )
            sample_count = len(selected_samples)
            weekday_battery_values = weekday_hour_battery.get((weekday, hour)) or []
            hour_battery_values = hour_battery.get(hour) or []
            battery_values = weekday_battery_values or hour_battery_values
            battery_profile_origin = (
                "weekday_hour_median_90d"
                if weekday_battery_values
                else (
                    "hour_median_90d_fallback" if hour_battery_values else "unavailable"
                )
            )

            def typical(field: str) -> float | None:
                valid_field_values = [
                    profile_sample[field]
                    for profile_sample in selected_samples
                    if profile_sample[field] is not None
                ]
                return (
                    round(median(valid_field_values), 3) if valid_field_values else None
                )

            export_ratios = []
            for profile_sample in selected_samples:
                if profile_sample["consumer_wallbox_kwh"] > 0.05:
                    continue
                surplus_kwh = max(
                    0.0,
                    profile_sample["solar_yield_kwh"]
                    - profile_sample["household_base_load_kwh"]
                    - profile_sample["consumer_heatpump_kwh"]
                    - profile_sample["consumer_heatingrod_kwh"]
                    - profile_sample["solar_to_battery_kwh"],
                )
                if surplus_kwh > 0.1:
                    export_ratios.append(
                        min(1.0, profile_sample["grid_export_kwh"] / surplus_kwh)
                    )
            surplus_calibration_factor = (
                round(median(export_ratios), 3)
                if export_ratios
                else 1.0 if selected_samples else None
            )
            profile.append(
                {
                    "weekday": weekday,
                    "hour": hour,
                    "available": bool(selected_samples),
                    "profile_origin": profile_origin,
                    "household_base_load_kwh": typical("household_base_load_kwh"),
                    "battery_reserve_kwh": typical("solar_to_battery_kwh"),
                    "battery_to_house_forecast_kwh": (
                        round(median(battery_values), 3) if battery_values else None
                    ),
                    "battery_to_house_profile_origin": battery_profile_origin,
                    "battery_to_house_sample_count": len(battery_values),
                    "battery_to_house_quality_percent": min(
                        100, round(len(battery_values) / _TARGET_SAMPLES * 100)
                    ),
                    "historical_grid_export_kwh": typical("grid_export_kwh"),
                    "historical_grid_import_kwh": typical("grid_import_kwh"),
                    "surplus_calibration_factor": surplus_calibration_factor,
                    "sample_count": sample_count,
                    "quality_percent": min(
                        100, round(sample_count / _TARGET_SAMPLES * 100)
                    ),
                }
            )
            continue
        continue
    populated_profile = [
        profile_entry for profile_entry in profile if profile_entry["sample_count"] > 0
    ]
    quality_percent = (
        round(
            sum(
                (
                    profile_entry["quality_percent"]
                    for profile_entry in populated_profile
                )
            )
            / len(populated_profile)
        )
        if populated_profile
        else 0
    )
    unavailable_sources = sorted(
        (
            field_name
            for field_name in _REQUIRED_FIELDS
            if not source_availability[field_name]["available"]
        )
    )
    missing_columns = sorted(
        (
            field_name
            for field_name in _REQUIRED_FIELDS
            if not source_availability[field_name]["column_present"]
        )
    )
    available = bool(complete_samples and (not unavailable_sources))
    availability_status = (
        "available"
        if available
        else (
            "missing_required_sources"
            if missing_columns
            else "invalid_required_values" if valid_samples else "no_samples"
        )
    )
    return {
        "context_version": 1,
        "available": available,
        "availability_status": availability_status,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "history_days": _HISTORY_DAYS,
        "sample_count": len(complete_samples),
        "row_count": len(valid_samples),
        "quality_percent": quality_percent,
        "profile": profile,
        "base_load_origin": "stats_hourly_house_minus_large_consumers",
        "battery_reserve_origin": "stats_hourly_billing.solar_to_battery_kwh",
        "battery_to_house_forecast_origin": "stats_hourly_billing.battery_to_house_kwh.median_90d",
        "source_origin": "stats_hourly_billing",
        "source_availability": source_availability,
        "required_sources": sorted(_REQUIRED_FIELDS),
        "unavailable_sources": unavailable_sources,
        "export_usage": "calibration_only",
    }


class StatsEnergyContextProvider:
    """Refresh an immutable read-only context cache from the STATS database."""

    provider_version = 1

    def __init__(self, hass: Any, db_manager: Any) -> None:
        self.hass = hass
        self.db_manager = db_manager
        self._cache = build_energy_context([], generated_at=datetime.now(timezone.utc))

    def snapshot(self) -> dict[str, Any]:
        return self._cache

    async def async_refresh(self) -> dict[str, Any]:
        current_time = datetime.now(self._local_timezone())
        try:
            column_rows = await self.db_manager.execute_read(
                "PRAGMA table_info(stats_hourly_billing)"
            )
            column_names = {
                str(_row_value(billing_row, "name") or _row_value(billing_row, 1))
                for billing_row in column_rows
            }
            if not {"date", "hour"} <= column_names:
                self._cache = build_energy_context(
                    [], generated_at=current_time, available_fields=column_names
                )
                self._cache["availability_status"] = "missing_time_columns"
                return self._cache if True else None
            select_columns = ["date", "hour"]
            select_columns.extend(
                (
                    (
                        field_name
                        if field_name in column_names
                        else f"NULL AS {field_name}"
                    )
                    for field_name in _FIELDS
                )
            )
            history_rows = await self.db_manager.execute_read(
                "SELECT "
                + ", ".join(select_columns)
                + " FROM stats_hourly_billing WHERE date >= ? AND date < ? ORDER BY date, hour",
                [
                    (current_time.date() - timedelta(days=_HISTORY_DAYS)).isoformat(),
                    current_time.date().isoformat(),
                ],
            )
            history_rows = await self._scale_with_corrections(
                history_rows, column_names, current_time
            )
            self._cache = build_energy_context(
                history_rows, generated_at=current_time, available_fields=column_names
            )
            self._cache["observed_hours"] = await self._observed_hours(
                current_time, column_names
            )
            self._cache["price_hours"] = await self._price_hours(current_time)
            self._cache.update(await self._loss_adjusted_autarky(current_time))
        except Exception as error:
            _LOGGER.warning("Could not refresh advisory energy context: %s", error)
            self._cache = build_energy_context(
                [], generated_at=current_time, available_fields=set()
            )
            self._cache["availability_status"] = "source_unavailable"
        return self._cache if True else None

    async def _scale_with_corrections(
        self, rows: list[Any], names: set[str], now: datetime
    ) -> list[Any]:
        """Scale hourly history with corrected daily totals. Failures keep raw rows."""
        try:
            from .corrected_energy import (
                corrected_daily_read as corrected_daily_read,
                projection_from_runtime as projection_from_runtime,
                scale_field_hours as scale_field_hours,
            )

            start_date = (now.date() - timedelta(days=_HISTORY_DAYS)).isoformat()
            end_date = (now.date() - timedelta(days=1)).isoformat()
            projection = await projection_from_runtime(start_date)
            if not projection:
                return rows
            corrected_totals = await corrected_daily_read(
                self.db_manager.execute_read, start_date, end_date, projection
            )
            return scale_field_hours(
                rows, corrected_totals, projection, _FIELDS, present_columns=names
            )
        except Exception as error:
            _LOGGER.warning("Could not apply energy-context corrections: %s", error)
            return rows

    async def _observed_hours(
        self, now: datetime, available_fields: set[str]
    ) -> list[dict[str, Any]]:
        """Return bounded current-day battery delivery without inventing future data."""
        if "battery_to_house_kwh" not in available_fields:
            return []
        try:
            observation_rows = await self.db_manager.execute_read(
                "SELECT date, hour, battery_to_house_kwh, price_ct_kwh FROM stats_hourly_billing WHERE date = ? ORDER BY hour LIMIT 25",
                [now.date().isoformat()],
            )
        except Exception:
            return []
        result_hours = []
        today_str = now.date().isoformat()
        for billing_row in observation_rows:
            row_date = str(_row_value(billing_row, "date") or "")
            raw_hour = _row_value(billing_row, "hour")
            if isinstance(raw_hour, bool):
                continue
            try:
                date.fromisoformat(row_date)
                hour = int(raw_hour)
            except (TypeError, ValueError):
                continue
            if row_date != today_str or not 0 <= hour <= min(23, now.hour):
                continue
            result_hours.append(
                {
                    "date": row_date,
                    "hour": hour,
                    "battery_to_house_kwh": _bounded_number(
                        _row_value(billing_row, "battery_to_house_kwh"), 0, 1000
                    ),
                    "price_ct_kwh": _bounded_number(
                        _row_value(billing_row, "price_ct_kwh"), -1000, 5000
                    ),
                    "origin": "stats_hourly_billing",
                }
            )
        return result_hours

    async def _loss_adjusted_autarky(self, now: datetime) -> dict[str, Any]:
        """Expose STATS' measured conversion-loss-adjusted autonomy for today."""
        unavailable_result = {
            "loss_adjusted_autarky_percent": None,
            "system_efficiency_percent": None,
            "autarky_origin": "unavailable",
        }
        try:
            efficiency_rows = await self.db_manager.execute_read(
                "SELECT d.autarkie_percent, e.system_input_sample_kwh, e.system_efficiency_percent FROM stats_daily_energy d JOIN stats_energy_efficiency_daily e ON e.date = d.date WHERE d.date = ? LIMIT 1",
                [now.date().isoformat()],
            )
        except Exception:
            return unavailable_result
        if not efficiency_rows:
            return unavailable_result
        billing_row = efficiency_rows[0]
        measured_autarky_percent = _bounded_number(
            _row_value(billing_row, "autarkie_percent"), 0, 100
        )
        system_input_kwh = _bounded_number(
            _row_value(billing_row, "system_input_sample_kwh"), 0, 100000
        )
        system_efficiency_percent = _bounded_number(
            _row_value(billing_row, "system_efficiency_percent"), 0, 100
        )
        if (
            measured_autarky_percent is None
            or system_input_kwh is None
            or system_input_kwh < 0.05
            or (system_efficiency_percent is None)
            or (system_efficiency_percent <= 0)
        ):
            return unavailable_result
        return {
            "loss_adjusted_autarky_percent": round(
                measured_autarky_percent * system_efficiency_percent / 100, 1
            ),
            "system_efficiency_percent": round(system_efficiency_percent, 1),
            "autarky_origin": "stats_energy_efficiency_daily",
        }

    async def _price_hours(self, now: datetime) -> list[dict[str, Any]]:
        """Return at most three HA calendar days of GPM total prices."""
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_exclusive = start_of_day + timedelta(days=3)
        try:
            price_rows = await self.db_manager.execute_read(
                "SELECT timestamp, hour, total_price FROM GPM_price_history WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp LIMIT 72",
                [
                    start_of_day.replace(tzinfo=None).isoformat(timespec="seconds"),
                    end_exclusive.replace(tzinfo=None).isoformat(timespec="seconds"),
                ],
            )
        except Exception:
            return []
        result_hours = []
        for billing_row in price_rows:
            raw_timestamp = _row_value(billing_row, "timestamp")
            raw_hour = _row_value(billing_row, "hour")
            price_ct_kwh = _bounded_number(
                _row_value(billing_row, "total_price"), -1000, 5000
            )
            if isinstance(raw_hour, bool) or price_ct_kwh is None:
                continue
            try:
                parsed_timestamp = datetime.fromisoformat(str(raw_timestamp))
                hour = int(raw_hour)
            except (TypeError, ValueError):
                continue
            if not 0 <= hour <= 23:
                continue
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(
                    tzinfo=self._local_timezone()
                )
            local_timestamp = parsed_timestamp.astimezone(self._local_timezone())
            if (
                not not local_timestamp.hour != hour
                or not start_of_day <= local_timestamp < end_exclusive
            ):
                continue
            result_hours.append(
                {
                    "timestamp": local_timestamp.isoformat(),
                    "price_ct_kwh": round(price_ct_kwh, 3),
                    "origin": "GPM_price_history",
                }
            )
            continue
        return result_hours

    def _local_timezone(self):
        time_zone = getattr(getattr(self.hass, "config", None), "time_zone", None)
        try:
            return ZoneInfo(time_zone) if time_zone else timezone.utc
        except ZoneInfoNotFoundError:
            return timezone.utc
