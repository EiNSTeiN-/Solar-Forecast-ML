# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Shared household energy allocation for heat-pump and mobility advice."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_BALANCE_TOLERANCE_KWH = 0.01
_STATS_DOMAIN = "sfml_stats"
_ENERGY_CONTEXT_VERSION = 1
_MAX_PROFILE_SLOTS = 168
_CONTEXT_STATUSES = frozenset(
    {
        "unknown",
        "available",
        "no_samples",
        "provider_error",
        "profile_invalid",
        "provider_ambiguous",
        "missing_time_columns",
        "provider_unavailable",
        "invalid_required_values",
        "missing_required_sources",
        "provider_contract_invalid",
    }
)
_BASE_LOAD_ORIGINS = frozenset({"stats_hourly_house_minus_large_consumers", "unknown"})
_EXPORT_USAGE = frozenset({"unused", "unknown", "calibration_only"})
_WALLBOX_MIN_POWER_KW = 1.0
_WALLBOX_MAX_POWER_KW = 50.0


@dataclass(frozen=True, slots=True)
class WallboxEnvelope:
    """Optional wallbox intake cap. Absent or disabled sends residual to export."""

    enabled: bool = False
    max_power_kw: float | None = None

    def capacity_kwh(self, interval_hours: float) -> float:
        if (
            not self.enabled
            or self.max_power_kw is None
            or interval_hours <= 0
            or (not isfinite(interval_hours))
        ):
            return 0.0
        max_power_kw = self.max_power_kw
        if (
            not isfinite(max_power_kw)
            or max_power_kw < _WALLBOX_MIN_POWER_KW
            or max_power_kw > _WALLBOX_MAX_POWER_KW
        ):
            return 0.0
        return max_power_kw * interval_hours


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, numeric_value) if isfinite(numeric_value) else fallback


def _optional_number(value: Any) -> float | None:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return
    return max(0.0, numeric_value) if isfinite(numeric_value) else None


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    numeric_value = _optional_number(value)
    if numeric_value is None or not minimum <= numeric_value <= maximum:
        return None if True else None
    return numeric_value if True else None


def _enum(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _canonical_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed_datetime = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
    else:
        return
    if parsed_datetime.tzinfo is None:
        parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
    return parsed_datetime.astimezone(timezone.utc).isoformat()


class EAISharedEnergyAllocator:
    """Apply one explicit PV priority order to all advisory consumers."""

    priority_order = (
        "household_base_load",
        "heat_pump",
        "battery_reserve",
        "calibration_reserve",
        "wallbox",
        "grid_export",
    )

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    def enrich(
        self, hours: list[dict[str, Any]], wallbox: WallboxEnvelope | None = None
    ) -> list[dict[str, Any]]:
        wallbox_envelope = (
            wallbox if isinstance(wallbox, WallboxEnvelope) else WallboxEnvelope()
        )
        energy_context = self._context()
        profile_by_slot = {
            (int(point.get("weekday", -1)), int(point.get("hour", -1))): point
            for point in energy_context.get("profile", [])
            if isinstance(point, dict)
        }
        context_available = bool(energy_context.get("available") and profile_by_slot)
        intervals = self._intervals(hours)
        enriched_hours = []
        for source_point, interval_start, interval_end, gap_before in intervals:
            point = dict(source_point)
            interval_hours = (interval_end - interval_start).total_seconds() / 3600
            profile_segments = (
                self._profile_segments(profile_by_slot, interval_start, interval_end)
                if context_available
                else []
            )
            profile_complete = bool(profile_segments) and all(
                (
                    profile_slot is not None
                    for segment_hours, profile_slot in profile_segments
                )
            )
            household_segments = [
                (
                    _optional_number(
                        (profile_slot or {}).get("household_base_load_kwh")
                    ),
                    hours,
                )
                for hours, profile_slot in profile_segments
            ]
            household_load_kwh = (
                sum(
                    (
                        household_slot_kwh * segment_hours
                        for household_slot_kwh, segment_hours in household_segments
                    )
                )
                if profile_complete
                and all(
                    (
                        household_slot_kwh is not None
                        for household_slot_kwh, segment_hours in household_segments
                    )
                )
                else None
            )
            battery_segments = [
                (
                    _optional_number((profile_slot or {}).get("battery_reserve_kwh")),
                    hours,
                )
                for hours, profile_slot in profile_segments
            ]
            battery_reserve_kwh = (
                sum(
                    (
                        battery_slot_kwh * segment_hours
                        for battery_slot_kwh, segment_hours in battery_segments
                    )
                )
                if profile_complete
                and all(
                    (
                        battery_slot_kwh is not None
                        for battery_slot_kwh, segment_hours in battery_segments
                    )
                )
                else None
            )
            surplus_calibration_factor = (
                sum(
                    (
                        min(
                            1.0,
                            _number(
                                (profile_slot or {}).get("surplus_calibration_factor"),
                                1.0,
                            ),
                        )
                        * segment_hours
                        for segment_hours, profile_slot in profile_segments
                    )
                )
                / interval_hours
                if profile_segments and interval_hours > 0
                else 1.0
            )
            historical_grid_export_kwh = (
                sum(
                    (
                        _number((profile_slot or {}).get("historical_grid_export_kwh"))
                        * segment_hours
                        for segment_hours, profile_slot in profile_segments
                    )
                )
                if profile_complete
                else None
            )
            context_quality_percent = (
                sum(
                    (
                        _number((profile_slot or {}).get("quality_percent"))
                        * segment_hours
                        for segment_hours, profile_slot in profile_segments
                    )
                )
                / interval_hours
                if profile_complete and interval_hours > 0
                else 0.0
            )
            pv_forecast_kwh = _optional_number(point.get("pv_forecast_kwh"))
            heat_pump_forecast_kw = _optional_number(point.get("forecast_kw"))
            heat_pump_kwh = (
                heat_pump_forecast_kw * interval_hours
                if heat_pump_forecast_kw is not None
                else None
            )
            allocation_inputs_ready = bool(
                context_available
                and profile_complete
                and (not gap_before)
                and (pv_forecast_kwh is not None)
                and (heat_pump_kwh is not None)
                and (household_load_kwh is not None)
                and (battery_reserve_kwh is not None)
            )
            if allocation_inputs_ready:
                household_pv_kwh = min(pv_forecast_kwh, household_load_kwh)
                household_grid_import_kwh = max(
                    0.0, household_load_kwh - household_pv_kwh
                )
                pv_after_household_kwh = max(0.0, pv_forecast_kwh - household_load_kwh)
                heat_pump_pv_kwh = min(pv_after_household_kwh, heat_pump_kwh)
                heat_pump_grid_import_kwh = max(0.0, heat_pump_kwh - heat_pump_pv_kwh)
                pv_after_heat_pump_kwh = max(
                    0.0, pv_after_household_kwh - heat_pump_kwh
                )
                battery_pv_reserve_kwh = min(
                    pv_after_heat_pump_kwh, battery_reserve_kwh
                )
                pv_after_battery_kwh = max(
                    0.0, pv_after_heat_pump_kwh - battery_pv_reserve_kwh
                )
                calibration_reserve_kwh = pv_after_battery_kwh * (
                    1.0 - surplus_calibration_factor
                )
                residual_pv_kwh = pv_after_battery_kwh - calibration_reserve_kwh
                wallbox_pv_kwh = min(
                    residual_pv_kwh, wallbox_envelope.capacity_kwh(interval_hours)
                )
                grid_export_kwh = max(0.0, residual_pv_kwh - wallbox_pv_kwh)
                balance_error_kwh = pv_forecast_kwh - (
                    household_pv_kwh
                    + heat_pump_pv_kwh
                    + battery_pv_reserve_kwh
                    + wallbox_pv_kwh
                    + calibration_reserve_kwh
                    + grid_export_kwh
                )
                balance_valid = abs(balance_error_kwh) <= _BALANCE_TOLERANCE_KWH
                allocation_status = "valid" if balance_valid else "balance_mismatch"
            else:
                balance_valid = False
                household_pv_kwh = None
                household_grid_import_kwh = None
                pv_after_household_kwh = None
                heat_pump_pv_kwh = None
                heat_pump_grid_import_kwh = None
                pv_after_heat_pump_kwh = None
                battery_pv_reserve_kwh = None
                residual_pv_kwh = None
                grid_export_kwh = None
                wallbox_pv_kwh = None
                calibration_reserve_kwh = None
                balance_error_kwh = None
                allocation_status = (
                    "energy_context_unavailable"
                    if not context_available or not profile_complete
                    else (
                        "energy_context_incomplete"
                        if household_load_kwh is None or battery_reserve_kwh is None
                        else "forecast_data_incomplete"
                    )
                )
            point.update(
                {
                    "interval_start": interval_start.isoformat(),
                    "interval_end": interval_end.isoformat(),
                    "interval_hours": round(interval_hours, 6),
                    "interval_gap_before": gap_before,
                    "heat_pump_kwh": self._rounded(heat_pump_kwh),
                    "household_base_load_kwh": self._rounded(
                        household_load_kwh if balance_valid else None
                    ),
                    "household_pv_kwh": self._rounded(household_pv_kwh),
                    "household_grid_import_kwh": self._rounded(
                        household_grid_import_kwh
                    ),
                    "pv_after_household_kwh": self._rounded(pv_after_household_kwh),
                    "heat_pump_pv_kwh": self._rounded(heat_pump_pv_kwh),
                    "heat_pump_grid_import_kwh": self._rounded(
                        heat_pump_grid_import_kwh
                    ),
                    "pv_after_heat_pump_kwh": self._rounded(pv_after_heat_pump_kwh),
                    "battery_reserve_kwh": self._rounded(
                        battery_reserve_kwh if balance_valid else None
                    ),
                    "battery_pv_reserve_kwh": self._rounded(battery_pv_reserve_kwh),
                    "battery_pv_reserved_kwh": self._rounded(battery_pv_reserve_kwh),
                    "pv_calibration_reserve_kwh": self._rounded(
                        calibration_reserve_kwh
                    ),
                    "wallbox_pv_available_kwh": self._rounded(wallbox_pv_kwh),
                    "residual_pv_kwh": self._rounded(residual_pv_kwh),
                    "unallocated_or_export_kwh": self._rounded(grid_export_kwh),
                    "expected_grid_export_kwh": self._rounded(grid_export_kwh),
                    "pv_balance_error_kwh": self._rounded(balance_error_kwh),
                    "pv_balance_tolerance_kwh": _BALANCE_TOLERANCE_KWH,
                    "allocation_valid": balance_valid,
                    "allocation_status": allocation_status,
                    "historical_grid_export_kwh": (
                        round(historical_grid_export_kwh, 3)
                        if balance_valid and historical_grid_export_kwh is not None
                        else None
                    ),
                    "surplus_calibration_factor": round(surplus_calibration_factor, 3),
                    "energy_context_quality_percent": round(context_quality_percent),
                    "energy_context_origin": (
                        energy_context.get("base_load_origin")
                        if context_available
                        else "fallback_without_stats_history"
                    ),
                    "allocation_priority_order": list(self.priority_order),
                }
            )
            enriched_hours.append(point)
            continue
        return enriched_hours

    @staticmethod
    def _rounded(value: float | None) -> float | None:
        return round(value, 3) if value is not None else None

    def _profile_segments(
        self,
        profile: dict[tuple[int, int], dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> list[tuple[float, dict[str, Any] | None]]:
        segments = []
        segment_start = start.astimezone(timezone.utc)
        while segment_start < end:
            segment_end = min(end, self._next_local_hour_boundary(segment_start))
            segment_hours = (segment_end - segment_start).total_seconds() / 3600
            segments.append((segment_hours, profile.get(self._slot(segment_start))))
            segment_start = segment_end
        return segments

    def _next_local_hour_boundary(self, cursor: datetime) -> datetime:
        local_timezone = self._local_timezone()
        local_moment = cursor.astimezone(local_timezone)
        next_naive_boundary = local_moment.replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        ) + timedelta(hours=1)
        for boundary_attempt in range(4):
            candidate_utc_times = {
                candidate_local_time.astimezone(timezone.utc)
                for fold in (0, 1)
                if (
                    candidate_local_time := next_naive_boundary.replace(
                        tzinfo=local_timezone, fold=fold
                    )
                )
                .astimezone(timezone.utc)
                .astimezone(local_timezone)
                .replace(tzinfo=None)
                == next_naive_boundary
            }
            future_boundaries = sorted(
                (
                    candidate_local_time
                    for candidate_local_time in candidate_utc_times
                    if candidate_local_time > cursor
                )
            )
            if future_boundaries:
                return future_boundaries[0]
            next_naive_boundary += timedelta(hours=1)
        return cursor + timedelta(hours=1)

    def _intervals(
        self, hours: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], datetime, datetime, bool]]:
        ordered_points = []
        for source_index, source_point in enumerate(hours):
            if isinstance(source_point, dict):
                raw_interval_start = source_point.get(
                    "interval_start", source_point.get("timestamp")
                )
                interval_start = self._timestamp(raw_interval_start)
                if interval_start is not None:
                    ordered_points.append((interval_start, source_index, source_point))
        ordered_points.sort(key=lambda item: item[0])
        resolved_intervals = []
        latest_interval_end = None
        for sorted_index, (interval_start, source_index, source_point) in enumerate(
            ordered_points
        ):
            interval_end = self._timestamp(source_point.get("interval_end"))
            if not (
                source_point.get("interval_end") is None or interval_end is not None
            ):
                continue
            if (
                interval_end is None
                and sorted_index + 1 < len(ordered_points)
                and (ordered_points[sorted_index + 1][1] == source_index + 1)
            ):
                interval_end = ordered_points[sorted_index + 1][0]
            if interval_end is None:
                interval_end = interval_start + timedelta(hours=1)
            if not interval_end > interval_start:
                continue
            gap_before = bool(
                latest_interval_end is not None and interval_start > latest_interval_end
            )
            resolved_intervals.append(
                (source_point, interval_start, interval_end, gap_before)
            )
            latest_interval_end = (
                max(latest_interval_end, interval_end)
                if latest_interval_end
                else interval_end
            )
        return resolved_intervals

    def _timestamp(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed_datetime = value
        else:
            try:
                parsed_datetime = datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return
        if parsed_datetime.tzinfo is None:
            local_timezone = self._local_timezone()
            candidate_utc_times = {
                candidate_local_time.astimezone(timezone.utc)
                for fold in (0, 1)
                if (
                    candidate_local_time := parsed_datetime.replace(
                        tzinfo=local_timezone, fold=fold
                    )
                )
                .astimezone(timezone.utc)
                .astimezone(local_timezone)
                .replace(tzinfo=None)
                == parsed_datetime
            }
            if len(candidate_utc_times) != 1:
                return
            parsed_datetime = candidate_utc_times.pop()
        return parsed_datetime.astimezone(timezone.utc)

    def status(self) -> dict[str, Any]:
        energy_context = self._context()
        return {
            "available": bool(energy_context.get("available")),
            "availability_status": energy_context.get("availability_status"),
            "provider_version": energy_context.get("context_version"),
            "quality_percent": energy_context.get("quality_percent", 0),
            "sample_count": energy_context.get("sample_count", 0),
            "history_days": energy_context.get("history_days", 0),
            "generated_at": energy_context.get("generated_at"),
            "base_load_origin": energy_context.get("base_load_origin"),
            "grid_export_usage": energy_context.get("export_usage"),
            "priority_order": list(self.priority_order),
        }

    def _context(self) -> dict[str, Any]:
        stats_data = getattr(self.hass, "data", {}).get(_STATS_DOMAIN, {})
        config_entries = getattr(self.hass, "config_entries", None)
        get_config_entry = getattr(config_entries, "async_get_entry", None)
        if not (isinstance(stats_data, dict) and callable(get_config_entry)):
            return self._unavailable_context("provider_unavailable")
        selected_provider = None
        for entry_id, runtime_data in stats_data.items():
            if (
                not isinstance(entry_id, str)
                or not isinstance(runtime_data, dict)
                or "energy_context_provider" not in runtime_data
            ):
                continue
            if selected_provider is not None:
                return self._unavailable_context("provider_ambiguous")
            selected_provider = (entry_id, runtime_data)
        if selected_provider is None:
            return self._unavailable_context("provider_unavailable")
        entry_id, runtime_data = selected_provider
        try:
            config_entry = get_config_entry(entry_id)
        except Exception:
            return self._unavailable_context("provider_error")
        entry_state = getattr(config_entry, "state", None)
        state_value = getattr(entry_state, "value", entry_state)
        stored_config_entry = runtime_data.get("config_entry")
        provider = runtime_data.get("energy_context_provider")
        provider_snapshot = getattr(provider, "snapshot", None)
        if (
            config_entry is None
            or getattr(config_entry, "domain", None) != _STATS_DOMAIN
            or state_value != "loaded"
            or (stored_config_entry is not config_entry)
            or (getattr(provider, "provider_version", None) != _ENERGY_CONTEXT_VERSION)
            or (not callable(provider_snapshot))
        ):
            return self._unavailable_context("provider_unavailable")
        try:
            context_snapshot = provider_snapshot()
        except Exception:
            return self._unavailable_context("provider_error")
        return self._normalize_context(context_snapshot)

    @staticmethod
    def _unavailable_context(status: str) -> dict[str, Any]:
        return {
            "context_version": _ENERGY_CONTEXT_VERSION,
            "available": False,
            "availability_status": _enum(status, _CONTEXT_STATUSES),
            "generated_at": None,
            "history_days": 0,
            "sample_count": 0,
            "row_count": 0,
            "quality_percent": 0,
            "base_load_origin": "unknown",
            "export_usage": "unknown",
            "profile": [],
        }

    @classmethod
    def _normalize_context(cls, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("context_version") != _ENERGY_CONTEXT_VERSION
        ):
            return cls._unavailable_context("provider_contract_invalid")
        raw_profile = value.get("profile")
        if not isinstance(raw_profile, list) or len(raw_profile) > _MAX_PROFILE_SLOTS:
            return cls._unavailable_context("profile_invalid")
        normalized_profile = []
        seen_slots = set()
        for raw_slot in raw_profile:
            if not isinstance(raw_slot, dict):
                return cls._unavailable_context("profile_invalid")
            weekday = _bounded_number(raw_slot.get("weekday"), 0, 6)
            hour = _bounded_number(raw_slot.get("hour"), 0, 23)
            household_base_load_kwh = _bounded_number(
                raw_slot.get("household_base_load_kwh"), 0, 1000
            )
            battery_reserve_kwh = _bounded_number(
                raw_slot.get("battery_reserve_kwh"), 0, 1000
            )
            historical_grid_export_kwh = _bounded_number(
                raw_slot.get("historical_grid_export_kwh", 0), 0, 1000
            )
            surplus_calibration_factor = _bounded_number(
                raw_slot.get("surplus_calibration_factor", 1), 0, 1
            )
            quality_percent = _bounded_number(
                raw_slot.get("quality_percent", 0), 0, 100
            )
            if not (
                weekday is not None
                and hour is not None
                and (
                    not "household_base_load_kwh" in raw_slot
                    or household_base_load_kwh is not None
                )
                and (
                    not "battery_reserve_kwh" in raw_slot
                    or battery_reserve_kwh is not None
                )
            ) or (
                historical_grid_export_kwh is None
                or surplus_calibration_factor is None
                or quality_percent is None
                or (not weekday.is_integer())
                or (not hour.is_integer())
            ):
                return cls._unavailable_context("profile_invalid")
            slot_key = (int(weekday), int(hour))
            if slot_key in seen_slots:
                return cls._unavailable_context("profile_invalid")
            seen_slots.add(slot_key)
            normalized_profile.append(
                {
                    "weekday": slot_key[0],
                    "hour": slot_key[1],
                    "household_base_load_kwh": household_base_load_kwh,
                    "battery_reserve_kwh": battery_reserve_kwh,
                    "historical_grid_export_kwh": historical_grid_export_kwh,
                    "surplus_calibration_factor": surplus_calibration_factor,
                    "quality_percent": quality_percent,
                }
            )
            continue
        generated_at = _canonical_timestamp(value.get("generated_at"))
        history_days = _bounded_number(value.get("history_days"), 0, 3650)
        sample_count = _bounded_number(value.get("sample_count"), 0, 10000000)
        row_count = _bounded_number(value.get("row_count", sample_count), 0, 10000000)
        quality_percent = _bounded_number(value.get("quality_percent"), 0, 100)
        availability_status = _enum(value.get("availability_status"), _CONTEXT_STATUSES)
        context_available = bool(
            value.get("available") is True
            and availability_status == "available"
            and (generated_at is not None)
            and normalized_profile
            and (history_days is not None)
            and (sample_count is not None)
            and (row_count is not None)
            and (quality_percent is not None)
        )
        if not context_available:
            if availability_status == "available":
                availability_status = "provider_contract_invalid"
        return {
            "context_version": _ENERGY_CONTEXT_VERSION,
            "available": context_available,
            "availability_status": availability_status,
            "generated_at": generated_at,
            "history_days": int(history_days or 0),
            "sample_count": int(sample_count or 0),
            "row_count": int(row_count or 0),
            "quality_percent": round(quality_percent or 0),
            "base_load_origin": _enum(
                value.get("base_load_origin"), _BASE_LOAD_ORIGINS
            ),
            "export_usage": _enum(value.get("export_usage"), _EXPORT_USAGE),
            "profile": normalized_profile,
        }

    def _slot(self, value: Any) -> tuple[int, int]:
        try:
            parsed_datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed_datetime = datetime.now(timezone.utc)
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
        local_moment = parsed_datetime.astimezone(self._local_timezone())
        return (local_moment.weekday(), local_moment.hour)

    def _local_timezone(self):
        timezone_name = getattr(getattr(self.hass, "config", None), "time_zone", None)
        try:
            return ZoneInfo(timezone_name) if timezone_name else timezone.utc
        except ZoneInfoNotFoundError:
            return timezone.utc
