# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Actual production source resolver for hourly persistence."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..core.core_panel_group_forecast import get_group_power_wp

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
_LOGGER = logging.getLogger(__name__)
POWER_BOUNDARY_GRACE = timedelta(minutes=5)
MIN_POWER_COVERAGE_PERCENT = 90.0
MAX_POWER_UNCOVERED_GAP_SECONDS = 600
MAX_POWER_IMPUTED_GAP_SECONDS = 600
DEFAULT_MAX_POWER_W = 50000.0


@dataclass
class ActualResolution:
    source: str
    hour_kwh: Optional[float]
    panel_group_actuals: Optional[Dict[str, float]]
    sensor_data_updates: Dict[str, Any]
    quality: str
    total_power_coverage_percent: float
    group_power_coverage_percent: Optional[Dict[str, float]]
    max_sample_gap_seconds: Optional[int]
    invalid_sample_count: int
    exclusion_reason: Optional[str]
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _PowerEvent:
    timestamp: datetime
    value_w: Optional[float]
    valid: bool
    reason: Optional[str] = None


class ActualSourceResolver:
    """Resolve hourly actuals without writing persistence state."""

    def __init__(
        self, hass: Any, coordinator: "SolarForecastMLCoordinator", db_manager: Any
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager

    async def resolve_hour(
        self,
        *,
        target_date: str,
        target_hour: int,
        current_time: datetime,
        sensor_data: Dict[str, Any],
    ) -> ActualResolution:
        power_entity = getattr(self.coordinator, "power_entity", None)
        if not power_entity:
            return self._unavailable("power_recorder", "missing_power_entity")
        hour_start = dt_util.ensure_local(
            datetime.fromisoformat(f"{target_date}T{target_hour:02d}:00:00")
        )
        hour_end = hour_start + timedelta(hours=1)
        if hour_end > current_time:
            return self._unavailable("power_recorder", "hour_not_completed")
        return await self.resolve_from_recorder_power(
            entity_id=power_entity,
            hour_start=hour_start,
            hour_end=hour_end,
            sensor_data=sensor_data,
        )

    async def resolve_from_recorder_power(
        self,
        *,
        entity_id: str,
        hour_start: datetime,
        hour_end: datetime,
        sensor_data: Dict[str, Any],
        source: str = "power_recorder",
        max_power_w: Optional[float] = None,
    ) -> ActualResolution:
        events = await self._load_recorder_power_events(
            entity_id=entity_id,
            start_time=hour_start,
            end_time=hour_end,
            max_power_w=max_power_w,
        )
        if not events:
            return self._unavailable(source, "no_recorder_power_events")
        integration = self._integrate_power_events(
            events=events,
            hour_start=hour_start,
            hour_end=hour_end,
            max_power_w=max_power_w,
        )
        if integration["hour_kwh"] is None:
            return ActualResolution(
                source=source,
                hour_kwh=None,
                panel_group_actuals=None,
                sensor_data_updates={},
                quality="unavailable",
                total_power_coverage_percent=integration["coverage_percent"],
                group_power_coverage_percent=None,
                max_sample_gap_seconds=integration["max_sample_gap_seconds"],
                invalid_sample_count=integration["invalid_sample_count"],
                exclusion_reason=integration["exclusion_reason"],
                diagnostics=integration["diagnostics"],
            )
        quality = "exact"
        exclusion_reason = None
        if (
            integration["coverage_percent"] < MIN_POWER_COVERAGE_PERCENT
            or (integration["max_uncovered_gap_seconds"] or 0)
            > MAX_POWER_UNCOVERED_GAP_SECONDS
        ):
            quality = "partial"
            exclusion_reason = "power_coverage_partial"
        return ActualResolution(
            source=source,
            hour_kwh=round(integration["hour_kwh"], 4),
            panel_group_actuals=None,
            sensor_data_updates={},
            quality=quality,
            total_power_coverage_percent=integration["coverage_percent"],
            group_power_coverage_percent=None,
            max_sample_gap_seconds=integration["max_sample_gap_seconds"],
            invalid_sample_count=integration["invalid_sample_count"],
            exclusion_reason=exclusion_reason,
            diagnostics=integration["diagnostics"],
        )

    async def resolve_group_power_hour(
        self, *, target_date: str, target_hour: int, current_time: datetime
    ) -> ActualResolution:
        panel_groups = list(getattr(self.coordinator, "panel_groups", None) or [])
        if not panel_groups:
            return self._unavailable("group_power_recorder", "missing_panel_groups")
        hour_start = dt_util.ensure_local(
            datetime.fromisoformat(f"{target_date}T{target_hour:02d}:00:00")
        )
        hour_end = hour_start + timedelta(hours=1)
        if hour_end > current_time:
            return self._unavailable("group_power_recorder", "hour_not_completed")
        missing_groups = [
            self._group_name(group, index)
            for index, group in enumerate(panel_groups)
            if not self._group_power_sensor(group)
        ]
        if missing_groups:
            diagnostics = {
                "reason": "missing_group_power_sensors",
                "configured_groups": len(panel_groups) - len(missing_groups),
                "total_groups": len(panel_groups),
                "missing_groups": missing_groups,
            }
            return ActualResolution(
                source="group_power_recorder",
                hour_kwh=None,
                panel_group_actuals=None,
                sensor_data_updates={},
                quality="unavailable",
                total_power_coverage_percent=0.0,
                group_power_coverage_percent={},
                max_sample_gap_seconds=None,
                invalid_sample_count=0,
                exclusion_reason="missing_group_power_sensors",
                diagnostics=diagnostics,
            )
        group_actuals = {}
        group_coverage = {}
        group_diagnostics = {}
        max_sample_gap = 0
        max_uncovered_gap = 0
        max_imputed_gap = 0
        invalid_samples = 0
        partial_reasons = []
        for index, group in enumerate(panel_groups):
            group_name = self._group_name(group, index)
            power_sensor = self._group_power_sensor(group)
            group_power_limit = self._group_max_plausible_power_w(group)
            resolution = await self.resolve_from_recorder_power(
                entity_id=power_sensor,
                hour_start=hour_start,
                hour_end=hour_end,
                sensor_data={},
                source="group_power_recorder",
                max_power_w=group_power_limit,
            )
            diagnostics = resolution.diagnostics or {}
            group_status = {
                "entity_id": power_sensor,
                "quality": resolution.quality,
                "hour_kwh": resolution.hour_kwh,
                "coverage_percent": resolution.total_power_coverage_percent,
                "max_sample_gap_seconds": resolution.max_sample_gap_seconds,
                "max_uncovered_gap_seconds": diagnostics.get(
                    "max_uncovered_gap_seconds"
                ),
                "imputed_seconds": diagnostics.get("imputed_seconds", 0),
                "max_imputed_gap_seconds": diagnostics.get(
                    "max_imputed_gap_seconds", 0
                ),
                "invalid_sample_count": resolution.invalid_sample_count,
                "exclusion_reason": resolution.exclusion_reason,
            }
            group_diagnostics[group_name] = group_status
            invalid_samples += resolution.invalid_sample_count
            if resolution.max_sample_gap_seconds is not None:
                max_sample_gap = max(max_sample_gap, resolution.max_sample_gap_seconds)
            uncovered_gap = diagnostics.get("max_uncovered_gap_seconds")
            if uncovered_gap is not None:
                max_uncovered_gap = max(max_uncovered_gap, int(uncovered_gap))
            imputed_gap = diagnostics.get("max_imputed_gap_seconds")
            if imputed_gap is not None:
                max_imputed_gap = max(max_imputed_gap, int(imputed_gap))
            if resolution.hour_kwh is None:
                partial_reasons.append(
                    f"{group_name}:{resolution.exclusion_reason or resolution.quality}"
                )
                continue
            group_actuals[group_name] = round(resolution.hour_kwh, 4)
            group_coverage[group_name] = resolution.total_power_coverage_percent
            if resolution.quality != "exact":
                partial_reasons.append(
                    f"{group_name}:{resolution.exclusion_reason or resolution.quality}"
                )
                continue
        if partial_reasons or len(group_actuals) != len(panel_groups):
            partial_total = (
                round(sum(group_actuals.values()), 4) if group_actuals else None
            )
            diagnostics = {
                "reason": "group_power_partial",
                "groups": group_diagnostics,
                "partial_reasons": partial_reasons,
                "configured_groups": len(panel_groups),
                "available_groups": len(group_actuals),
                "exact_groups": sum(
                    (
                        1
                        for group_status in group_diagnostics.values()
                        if group_status.get("quality") == "exact"
                    )
                ),
                "max_uncovered_gap_seconds": max_uncovered_gap,
                "max_imputed_gap_seconds": max_imputed_gap,
            }
            return ActualResolution(
                source="group_power_recorder",
                hour_kwh=partial_total,
                panel_group_actuals=group_actuals or None,
                sensor_data_updates={},
                quality="partial",
                total_power_coverage_percent=(
                    min(group_coverage.values()) if group_coverage else 0.0
                ),
                group_power_coverage_percent=group_coverage,
                max_sample_gap_seconds=max_sample_gap,
                invalid_sample_count=invalid_samples,
                exclusion_reason="group_power_partial",
                diagnostics=diagnostics,
            )
        total_kwh = round(sum(group_actuals.values()), 4)
        minimum_coverage = min(group_coverage.values()) if group_coverage else 0.0
        diagnostics = {
            "groups": group_diagnostics,
            "configured_groups": len(panel_groups),
            "exact_groups": len(group_actuals),
            "min_group_coverage_percent": minimum_coverage,
            "max_uncovered_gap_seconds": max_uncovered_gap,
            "max_imputed_gap_seconds": max_imputed_gap,
        }
        return ActualResolution(
            source="group_power_recorder",
            hour_kwh=total_kwh,
            panel_group_actuals=group_actuals,
            sensor_data_updates={},
            quality="exact",
            total_power_coverage_percent=minimum_coverage,
            group_power_coverage_percent=group_coverage,
            max_sample_gap_seconds=max_sample_gap,
            invalid_sample_count=invalid_samples,
            exclusion_reason=None,
            diagnostics=diagnostics,
        )

    async def _load_recorder_power_events(
        self,
        *,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
        max_power_w: Optional[float] = None,
    ) -> List[_PowerEvent]:
        try:
            from homeassistant.components.recorder import get_instance as get_instance
            from homeassistant.components.recorder.history import (
                state_changes_during_period as state_changes_during_period,
            )
        except ImportError:
            _LOGGER.debug("Recorder not available for power actual resolution")
            return []
        try:
            recorder = get_instance(self.hass)

            def load_states() -> Any:
                try:
                    return state_changes_during_period(
                        self.hass,
                        start_time,
                        end_time + POWER_BOUNDARY_GRACE,
                        entity_id=entity_id,
                        no_attributes=False,
                        include_start_time_state=True,
                    )
                except TypeError:
                    return state_changes_during_period(
                        self.hass,
                        start_time,
                        end_time + POWER_BOUNDARY_GRACE,
                        entity_id,
                        False,
                        True,
                        None,
                    )

            states_by_entity = await recorder.async_add_executor_job(load_states)
        except Exception as error:
            _LOGGER.warning(
                "Recorder power actual resolution failed for %s: %s", entity_id, error
            )
            return []
        states = states_by_entity.get(entity_id, []) if states_by_entity else []
        events = []
        for state in states:
            timestamp = self._recorder_state_time(state, start_time)
            if not timestamp is not None:
                continue
            power_value, reason = self._normalize_power_state(
                state, max_power_w=max_power_w
            )
            events.append(
                _PowerEvent(
                    timestamp=timestamp,
                    value_w=power_value,
                    valid=power_value is not None,
                    reason=reason,
                )
            )
        events.sort(key=lambda item: item.timestamp)
        return events

    def _normalize_power_state(
        self, state: Any, *, max_power_w: Optional[float] = None
    ) -> tuple[Optional[float], Optional[str]]:
        state_value = getattr(state, "state", None)
        if state_value in (None, "", "unknown", "unavailable"):
            return (None, "invalid_state")
        try:
            raw_power = float(state_value)
        except (TypeError, ValueError):
            return (None, "non_numeric_state")
        attributes = getattr(state, "attributes", {}) or {}
        unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
        if unit == "kw":
            power_w = raw_power * 1000.0
        elif unit == "w":
            power_w = raw_power
        elif unit == "":
            power_w = self._normalize_power_without_unit(
                raw_power, max_power_w=max_power_w
            )
            if power_w is None:
                return (None, "missing_unit")
        else:
            return (None, f"unsupported_unit:{unit}")
        if power_w < 0:
            power_w = 0.0
        power_limit = max_power_w or self._max_plausible_power_w()
        if power_w > power_limit:
            return (None, "power_spike")
        return (power_w, None)

    def _normalize_power_without_unit(
        self, raw_value: float, *, max_power_w: Optional[float] = None
    ) -> Optional[float]:
        power_limit = max_power_w or self._max_plausible_power_w()
        kw_boundary = power_limit / 1000.0
        if raw_value > kw_boundary and raw_value <= power_limit:
            return raw_value
        return None if True else None

    def _max_plausible_power_w(self) -> float:
        power_limits = []
        for capacity_attribute in (
            "solar_capacity",
            "solar_capacity_kwp",
            "total_capacity_kwp",
            "pv_capacity_kwp",
        ):
            capacity_setting = getattr(self.coordinator, capacity_attribute, None)
            try:
                capacity_kwp = float(capacity_setting)
            except (TypeError, ValueError):
                continue
            if capacity_kwp > 0:
                power_limits.append(capacity_kwp * 1000.0 * 1.5)
        inverter_setting = getattr(self.coordinator, "inverter_max_power", None)
        try:
            inverter_limit = float(inverter_setting)
        except (TypeError, ValueError):
            inverter_limit = 0.0
        if inverter_limit > 0:
            inverter_w = (
                inverter_limit * 1000.0 if inverter_limit <= 100.0 else inverter_limit
            )
            power_limits.append(inverter_w * 1.5)
        if not power_limits:
            return DEFAULT_MAX_POWER_W
        return max(max(power_limits), 500.0)

    def _integrate_power_events(
        self,
        *,
        events: List[_PowerEvent],
        hour_start: datetime,
        hour_end: datetime,
        max_power_w: Optional[float] = None,
    ) -> Dict[str, Any]:
        relevant_events = [
            event
            for event in events
            if event.timestamp <= hour_end + POWER_BOUNDARY_GRACE
        ]
        if not relevant_events:
            return self._empty_integration("no_relevant_power_events")
        prepared_events = self._prepare_hour_events(
            events=relevant_events, hour_start=hour_start, hour_end=hour_end
        )
        if not prepared_events:
            return self._empty_integration("no_boundary_power_state")
        invalid_sample_count = sum((1 for event in prepared_events if not event.valid))
        max_sample_gap = self._max_sample_gap_seconds(
            events=prepared_events, hour_start=hour_start, hour_end=hour_end
        )
        covered_seconds = 0.0
        imputed_seconds = 0.0
        max_uncovered_gap = 0.0
        max_imputed_gap = 0.0
        energy_wh = 0.0
        intervals = []
        for index, event in enumerate(prepared_events):
            interval_start = max(event.timestamp, hour_start)
            if interval_start >= hour_end:
                continue
            if index + 1 < len(prepared_events):
                interval_end = min(prepared_events[index + 1].timestamp, hour_end)
            else:
                interval_end = hour_end
            if interval_end > interval_start:
                intervals.append((interval_start, interval_end, event))
        index = 0
        previous_valid_power = None
        while index < len(intervals):
            interval_start, interval_end, event = intervals[index]
            interval_seconds = (interval_end - interval_start).total_seconds()
            if event.valid and event.value_w is not None:
                covered_seconds += interval_seconds
                energy_wh += event.value_w * interval_seconds / 3600.0
                previous_valid_power = event.value_w
                index += 1
                continue
            gap_start = interval_start
            gap_end = interval_end
            next_index = index + 1
            while next_index < len(intervals):
                next_start, next_end, next_event = intervals[next_index]
                if next_event.valid and next_event.value_w is not None:
                    break
                gap_end = next_end
                next_index += 1
            gap_seconds = (gap_end - gap_start).total_seconds()
            next_valid_power = None
            if next_index < len(intervals):
                next_event = intervals[next_index][2]
                if next_event.valid and next_event.value_w is not None:
                    next_valid_power = next_event.value_w
            neighbor_powers = [
                power_value
                for power_value in (previous_valid_power, next_valid_power)
                if power_value is not None
            ]
            if gap_seconds <= MAX_POWER_IMPUTED_GAP_SECONDS and neighbor_powers:
                imputed_power = sum(neighbor_powers) / len(neighbor_powers)
                imputed_seconds += gap_seconds
                max_imputed_gap = max(max_imputed_gap, gap_seconds)
                energy_wh += imputed_power * gap_seconds / 3600.0
            else:
                max_uncovered_gap = max(max_uncovered_gap, gap_seconds)
            index = next_index
        hour_seconds = max((hour_end - hour_start).total_seconds(), 1.0)
        effective_seconds = covered_seconds + imputed_seconds
        coverage_percent = min(100.0, effective_seconds / hour_seconds * 100.0)
        if effective_seconds <= 0:
            return {
                "hour_kwh": None,
                "coverage_percent": coverage_percent,
                "max_sample_gap_seconds": max_sample_gap,
                "max_uncovered_gap_seconds": int(max_uncovered_gap),
                "max_imputed_gap_seconds": int(max_imputed_gap),
                "invalid_sample_count": invalid_sample_count,
                "exclusion_reason": "no_valid_power_coverage",
                "diagnostics": {
                    "event_count": len(prepared_events),
                    "covered_seconds": int(covered_seconds),
                    "imputed_seconds": int(imputed_seconds),
                    "max_sample_gap_seconds": max_sample_gap,
                    "max_uncovered_gap_seconds": int(max_uncovered_gap),
                    "max_imputed_gap_seconds": int(max_imputed_gap),
                },
            }
        hour_kwh = energy_wh / 1000.0
        power_limit = max_power_w or self._max_plausible_power_w()
        max_hourly_kwh = power_limit / 1000.0 * 1.3
        if hour_kwh > max_hourly_kwh:
            return {
                "hour_kwh": None,
                "coverage_percent": coverage_percent,
                "max_sample_gap_seconds": max_sample_gap,
                "max_uncovered_gap_seconds": int(max_uncovered_gap),
                "max_imputed_gap_seconds": int(max_imputed_gap),
                "invalid_sample_count": invalid_sample_count + 1,
                "exclusion_reason": "power_hourly_limit",
                "diagnostics": {
                    "event_count": len(prepared_events),
                    "hour_kwh": round(hour_kwh, 4),
                    "max_hourly_kwh": round(max_hourly_kwh, 4),
                    "covered_seconds": int(covered_seconds),
                    "imputed_seconds": int(imputed_seconds),
                    "max_sample_gap_seconds": max_sample_gap,
                    "max_uncovered_gap_seconds": int(max_uncovered_gap),
                    "max_imputed_gap_seconds": int(max_imputed_gap),
                },
            }
        return {
            "hour_kwh": hour_kwh,
            "coverage_percent": coverage_percent,
            "max_sample_gap_seconds": max_sample_gap,
            "max_uncovered_gap_seconds": int(max_uncovered_gap),
            "max_imputed_gap_seconds": int(max_imputed_gap),
            "invalid_sample_count": invalid_sample_count,
            "exclusion_reason": None,
            "diagnostics": {
                "event_count": len(prepared_events),
                "covered_seconds": int(covered_seconds),
                "imputed_seconds": int(imputed_seconds),
                "max_sample_gap_seconds": max_sample_gap,
                "max_uncovered_gap_seconds": int(max_uncovered_gap),
                "max_imputed_gap_seconds": int(max_imputed_gap),
            },
        }

    def _max_sample_gap_seconds(
        self, *, events: List[_PowerEvent], hour_start: datetime, hour_end: datetime
    ) -> Optional[int]:
        if not events:
            return
        sample_times = [
            min(max(event.timestamp, hour_start), hour_end)
            for event in events
            if event.timestamp <= hour_end
        ]
        if not sample_times:
            return
        sample_times = sorted(set(sample_times))
        if sample_times[0] > hour_start:
            sample_times.insert(0, hour_start)
        if sample_times[-1] < hour_end:
            sample_times.append(hour_end)
        if len(sample_times) < 2:
            return 0
        return int(
            max(
                (
                    (sample_times[index + 1] - sample_times[index]).total_seconds()
                    for index in range(len(sample_times) - 1)
                )
            )
        )

    def _prepare_hour_events(
        self, *, events: List[_PowerEvent], hour_start: datetime, hour_end: datetime
    ) -> List[_PowerEvent]:
        prepared_events = []
        start_event = None
        first_after_start = None
        for event in events:
            if event.timestamp <= hour_start:
                start_event = event
            elif first_after_start is None:
                first_after_start = event
        if start_event is not None:
            prepared_events.append(
                _PowerEvent(
                    timestamp=hour_start,
                    value_w=start_event.value_w,
                    valid=start_event.valid,
                    reason=start_event.reason,
                )
            )
        elif (
            first_after_start is not None
            and first_after_start.timestamp - hour_start <= POWER_BOUNDARY_GRACE
        ):
            prepared_events.append(
                _PowerEvent(
                    timestamp=hour_start,
                    value_w=first_after_start.value_w,
                    valid=first_after_start.valid,
                    reason=first_after_start.reason,
                )
            )
        elif first_after_start is not None:
            prepared_events.append(
                _PowerEvent(
                    timestamp=first_after_start.timestamp,
                    value_w=first_after_start.value_w,
                    valid=first_after_start.valid,
                    reason=first_after_start.reason,
                )
            )
        for event in events:
            if hour_start < event.timestamp < hour_end:
                prepared_events.append(event)
        prepared_events.sort(key=lambda item: item.timestamp)
        deduplicated_events = []
        for event in prepared_events:
            if (
                deduplicated_events
                and event.timestamp == deduplicated_events[-1].timestamp
            ):
                deduplicated_events[-1] = event
            else:
                deduplicated_events.append(event)
        return deduplicated_events

    def _recorder_state_time(
        self, state: Any, reference: datetime
    ) -> Optional[datetime]:
        timestamp = getattr(state, "last_updated", None) or getattr(
            state, "last_changed", None
        )
        return self._normalize_state_time(timestamp, reference)

    def _normalize_state_time(
        self, value: Any, reference: datetime
    ) -> Optional[datetime]:
        if value is None:
            return
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=reference.tzinfo)
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return
        if not isinstance(value, datetime):
            return
        if value.tzinfo is None:
            return dt_util.ensure_local(value)
        if reference.tzinfo is None:
            return value
        return value.astimezone(reference.tzinfo)

    def _unavailable(self, source: str, reason: str) -> ActualResolution:
        return ActualResolution(
            source=source,
            hour_kwh=None,
            panel_group_actuals=None,
            sensor_data_updates={},
            quality="unavailable",
            total_power_coverage_percent=0.0,
            group_power_coverage_percent=None,
            max_sample_gap_seconds=None,
            invalid_sample_count=0,
            exclusion_reason=reason,
            diagnostics={"reason": reason},
        )

    def _group_name(self, group: Any, idx: int) -> str:
        if isinstance(group, dict):
            return str(group.get("name") or f"Gruppe {idx + 1}")
        return str(getattr(group, "name", None) or f"Gruppe {idx + 1}")

    def _group_power_sensor(self, group: Any) -> Optional[str]:
        if isinstance(group, dict):
            power_sensor = group.get("power_sensor")
        else:
            power_sensor = getattr(group, "power_sensor", None)
        return str(power_sensor).strip() if power_sensor else None

    def _group_max_plausible_power_w(self, group: Any) -> float:
        return max(get_group_power_wp(group) * 1.5, 50.0)

    def _empty_integration(self, reason: str) -> Dict[str, Any]:
        return {
            "hour_kwh": None,
            "coverage_percent": 0.0,
            "max_sample_gap_seconds": None,
            "max_uncovered_gap_seconds": None,
            "invalid_sample_count": 0,
            "exclusion_reason": reason,
            "diagnostics": {"reason": reason},
        }
