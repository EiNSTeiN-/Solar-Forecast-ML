# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Room heating store and controller. Decisions are pure; writes stay here."""
from __future__ import annotations
from .heating_model import (
    OpenPhase,
    RoomModel,
    adopt_offset,
    close_phase,
    expected_temperature,
    forecast_summary,
    model_from_dict,
    model_to_dict,
    phase_kind,
    phase_to_dict,
    preheat_lead_hours,
    propose_offset,
    season_with_forecast,
    thermostat_demand,
)
import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from ..const import DOMAIN, NAME, VERSION

_LOGGER = logging.getLogger(__name__)
WRITE_TIMEOUT_SECONDS = 15
_UNCLEAR_PRESENCE = frozenset({"unknown", "unavailable"})
_CLIMATE_WATCH = (
    "temperature",
    "hvac_mode",
    "target_temp_step",
    "min_temp",
    "max_temp",
    "hvac_action",
)
STORE_VERSION = 1
ABS_TOLERANCE = 0.05
DEADBAND_C = 0.5
DECREASE_LIMIT = timedelta(minutes=60)
INCREASE_LIMIT = timedelta(minutes=5)
HVAC_LIMIT = timedelta(hours=6)
STARTUP_GRACE = timedelta(seconds=180)
RETRY_AFTER = timedelta(minutes=30)
SLOW_RETRY = timedelta(hours=2)
MAX_FAST_ATTEMPTS = 3
SAMPLE_INTERVAL = timedelta(minutes=15)
SAMPLE_WINDOW = timedelta(hours=24)
OFFSET_WINDOW = timedelta(minutes=60)
OFFSET_SAMPLE_GAP = timedelta(seconds=45)
HARD_MIN_C = 5.0
HARD_MAX_C = 30.0
DEFAULT_BASE_C = 17.0
DEFAULT_FROST_C = 7.0
DEFAULT_AWAY_DELAY_MIN = 15
DEFAULT_HEATING_LIMIT_C = 15.0
DEFAULT_LIFT_K = 1.0
SEASON_HYSTERESIS_C = 1.0
DETECT_DROP_K = 1.0
DETECT_WITHIN = timedelta(minutes=10)
DETECT_HOLD = timedelta(minutes=30)
DETECT_BUFFER = timedelta(minutes=15)
LIFT_HOLD = timedelta(minutes=10)
LIFT_CAP_K = 2.0
_LIFT_REASONS = frozenset({"comfort", "preheat"})
_SLUG = re.compile("[^a-z0-9]+")
_HHMM = re.compile("^([01]\\d|2[0-3]):[0-5]\\d$")
_VACATION = re.compile("^(\\d{4})-(\\d{2})-(\\d{2})T([01]\\d|2[0-3]):([0-5]\\d)$")
_MODES = frozenset({"auto", "fixed", "combined", "schedule"})
_CANNOT_WRITE = frozenset(
    {
        "hvac_off",
        "range_setpoint",
        "awaiting_device",
        "setpoint_unreadable",
        "device_not_responding",
        "thermostat_unavailable",
    }
)


class HeatingError(Exception):

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GroupPresence(NamedTuple):
    presence: str | None
    away_since: datetime | None


@dataclass(frozen=True)
class HeatingSettings:
    base_temp_c: float = DEFAULT_BASE_C
    frost_floor_c: float = DEFAULT_FROST_C
    presence_entities: tuple[str, ...] = ()
    away_delay_min: int = DEFAULT_AWAY_DELAY_MIN
    outdoor_entity: str | None = None
    heating_limit_c: float = DEFAULT_HEATING_LIMIT_C
    vacation_start: str | None = None
    vacation_end: str | None = None
    lift_k: float = DEFAULT_LIFT_K
    lift_on_pv: bool = False
    lift_on_cheap: bool = False


@dataclass(frozen=True)
class ScheduleWindow:
    days: tuple[int, ...]
    start: str
    end: str
    temp_c: float | None = None


@dataclass(frozen=True)
class RoomConfig:
    id: str
    name: str
    climate_entities: tuple[str, ...]
    comfort_temp_c: float
    temp_sensor: str | None = None
    window_sensor: str | None = None
    active: bool = False
    schedule: tuple[ScheduleWindow, ...] = ()
    auto_hvac: bool = True
    setback_temp_c: float | None = None
    presence_enabled: bool = True
    schedule_enabled: bool = False
    presence_persons: tuple[str, ...] = ()
    window_detect: bool = False
    lift_enabled: bool = False

    @property
    def mode(self) -> str:
        if self.presence_enabled and self.schedule_enabled:
            return "combined"
        if self.schedule_enabled:
            return "schedule"
        if self.presence_enabled:
            return "auto"
        return "fixed"


@dataclass(frozen=True)
class ThermoMemory:
    last_written_c: float | None = None
    last_write_at: datetime | None = None
    override_until_presence_change: bool = False
    last_failed_at: datetime | None = None
    last_hvac_write_at: datetime | None = None
    last_hvac_mode: str | None = None
    awaiting_since: datetime | None = None
    attempt_count: int = 0
    confirmed_at: datetime | None = None
    pending_hvac: str | None = None
    seen_hvac: str | None = None
    seen_setpoint_c: float | None = None
    seen_known: bool = False


@dataclass(frozen=True)
class ThermoReading:
    available: bool
    hvac_mode: str | None
    setpoint_c: float | None
    uses_range: bool
    step: float = 0.5
    min_temp: float | None = None
    max_temp: float | None = None
    hvac_modes: tuple[str, ...] = ()
    current_c: float | None = None
    reported_at: datetime | None = None
    hvac_action: str | None = None


@dataclass(frozen=True)
class ThermoDecision:
    entity_id: str
    write: bool
    target_c: float | None
    reason: str
    mode: str
    presence: str
    memory: ThermoMemory
    hvac_write: bool = False
    hvac_target: str | None = None
    offset_k: float | None = None


@dataclass(frozen=True)
class RoomDecision:
    room_id: str
    target_c: float | None
    mode: str
    reason: str
    presence: str
    away_since: datetime | None
    thermostats: tuple[ThermoDecision, ...]
    next_at: datetime | None = None
    next_target_c: float | None = None
    preheat_for: datetime | None = None


def clamp_setpoint(value: float, frost_floor_c: float) -> float:
    lower_bound_c = max(float(frost_floor_c), HARD_MIN_C)
    return min(HARD_MAX_C, max(lower_bound_c, float(value)))


def _presence_kind(value: str | None) -> str:
    """home, unclear, or away. Zone names such as 'Arbeit' count as away."""
    if value == "home":
        return "home"
    if value is None or value in _UNCLEAR_PRESENCE:
        return "unclear"
    return "away"


def resolve_presence(
    states: dict[str, str | None],
    entities: tuple[str, ...],
    away_since: datetime | None,
    now: datetime,
    away_delay_min: int,
) -> tuple[str, datetime | None, str]:
    """Return presence, the away timer, and the presence reason."""
    if not entities:
        return ("home", None, "no_persons")
    presence_kinds = [_presence_kind(states.get(entity_id)) for entity_id in entities]
    if any((presence_kind == "home" for presence_kind in presence_kinds)):
        return ("home", None, "presence_home")
    if any((presence_kind == "unclear" for presence_kind in presence_kinds)):
        return ("home", None, "presence_unknown")
    away_started_at = away_since or now
    if now - away_started_at >= timedelta(minutes=away_delay_min):
        return ("away", away_started_at, "presence_away")
    return ("home", away_started_at, "presence_pending")


def quantize_setpoint(
    value: float,
    step: float | None,
    frost_floor_c: float,
    device_min: float | None = None,
    device_max: float | None = None,
) -> float:
    """Round to the thermostat step, then clamp. Never below the frost floor."""
    setpoint_step_c = 0.5 if step is None or step <= 0 else float(step)
    lower_bound_c = max(float(frost_floor_c), HARD_MIN_C)
    if device_min is not None:
        lower_bound_c = max(lower_bound_c, float(device_min))
    upper_bound_c = HARD_MAX_C
    if device_max is not None:
        upper_bound_c = min(upper_bound_c, float(device_max))
    if upper_bound_c < lower_bound_c:
        upper_bound_c = lower_bound_c
    rounded_setpoint_c = round(
        round(float(value) / setpoint_step_c) * setpoint_step_c, 4
    )
    return min(upper_bound_c, max(lower_bound_c, rounded_setpoint_c))


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _as_local(moment: datetime) -> datetime:
    try:
        from homeassistant.util import dt as datetime_util

        return datetime_util.as_local(moment)
    except Exception:
        return moment


def presence_group_key(entities: tuple[str, ...]) -> str:
    """Stable key for one person set. Order does not create a second group."""
    return "|".join(sorted(entities))


def effective_presence_entities(
    settings: HeatingSettings, room: RoomConfig
) -> tuple[str, ...]:
    return room.presence_persons or settings.presence_entities


def _window_covers(
    window: ScheduleWindow, weekday: int, previous: int, minute: int
) -> bool:
    start_minute = _minutes(window.start)
    end_minute = _minutes(window.end)
    if start_minute < end_minute:
        return weekday in window.days and start_minute <= minute < end_minute
    if weekday in window.days and minute >= start_minute:
        return True
    return previous in window.days and minute < end_minute


def active_schedule_window(
    local: datetime, windows: tuple[ScheduleWindow, ...]
) -> ScheduleWindow | None:
    """The first weekly window that contains local HA time. Overnight windows spill into the next day."""
    if local.tzinfo is None:
        return
    minute_of_day = local.hour * 60 + local.minute
    weekday = local.weekday()
    previous_weekday = (weekday - 1) % 7
    for schedule_window in windows:
        if not _window_covers(
            schedule_window, weekday, previous_weekday, minute_of_day
        ):
            continue
        return schedule_window


def schedule_active(local: datetime, windows: tuple[ScheduleWindow, ...]) -> bool:
    """True when local HA time sits in a weekly window. Overnight windows spill into the next day."""
    return active_schedule_window(local, windows) is not None


def _vacation_naive(value: str) -> datetime | None:
    vacation_match = _VACATION.match(value)
    if vacation_match is None:
        return
    try:
        return datetime(*(int(date_part) for date_part in vacation_match.groups()))
    except ValueError:
        return


def _civil_local(value: str | None, tzinfo: Any) -> datetime | None:
    if not isinstance(value, str) or tzinfo is None:
        return None
    naive_local = _vacation_naive(value)
    if naive_local is None:
        return
    return naive_local.replace(tzinfo=tzinfo)


def vacation_bounds(
    settings: HeatingSettings, tzinfo: Any
) -> tuple[datetime, datetime] | None:
    start_at = _civil_local(settings.vacation_start, tzinfo)
    end_at = _civil_local(settings.vacation_end, tzinfo)
    if start_at is None or end_at is None or end_at <= start_at:
        return None
    return (start_at, end_at)


def vacation_active(settings: HeatingSettings, local: datetime) -> bool:
    if local.tzinfo is None:
        return False
    vacation_interval = vacation_bounds(settings, local.tzinfo)
    if vacation_interval is None:
        return False
    start_at, end_at = vacation_interval
    return start_at <= local < end_at


def _window_goal(room: RoomConfig, window: ScheduleWindow) -> float:
    if window.temp_c is None:
        return room.comfort_temp_c
    return window.temp_c


def room_setback_c(settings: HeatingSettings, room: RoomConfig) -> float:
    """Room setback, or the global default when the room has none."""
    if room.setback_temp_c is None:
        return settings.base_temp_c
    return room.setback_temp_c


def _target_for(
    settings: HeatingSettings,
    room: RoomConfig,
    presence: str,
    window_on: bool,
    local: datetime,
    *,
    boost_c: float | None = None,
    detected_open: bool = False,
) -> tuple[float, str, str]:
    """Window, then boost, then vacation, then away, then the schedule, then comfort."""
    if window_on:
        raw_value, reason, mode = (settings.frost_floor_c, "window_open", "frost")
    elif detected_open:
        raw_value, reason, mode = (settings.frost_floor_c, "window_detected", "frost")
    elif boost_c is not None:
        raw_value, reason, mode = (boost_c, "boost", "boost")
    elif vacation_active(settings, local):
        raw_value, reason, mode = (
            room_setback_c(settings, room),
            "vacation",
            "vacation",
        )
    elif room.presence_enabled and presence == "away":
        raw_value, reason, mode = (
            room_setback_c(settings, room),
            "away_setback",
            "away",
        )
    elif room.schedule_enabled:
        schedule_window = active_schedule_window(local, room.schedule)
        if schedule_window is not None:
            raw_value, reason, mode = (
                _window_goal(room, schedule_window),
                "comfort",
                "comfort",
            )
        else:
            raw_value, reason, mode = (
                room_setback_c(settings, room),
                "schedule_setback",
                "schedule",
            )
    else:
        raw_value, reason, mode = (room.comfort_temp_c, "comfort", "comfort")
    return (clamp_setpoint(raw_value, settings.frost_floor_c), reason, mode)


def _next_window(
    room: RoomConfig, local: datetime, now: datetime
) -> tuple[datetime, ScheduleWindow] | None:
    if local.tzinfo is None:
        return
    next_window_candidate = None
    for day_offset in range(8):
        day = (local + timedelta(days=day_offset)).date()
        for schedule_window in room.schedule:
            if day.weekday() not in schedule_window.days:
                continue
            hour_text, minute_text = schedule_window.start.split(":")
            try:
                local_moment = datetime(
                    day.year,
                    day.month,
                    day.day,
                    int(hour_text),
                    int(minute_text),
                    tzinfo=local.tzinfo,
                )
            except ValueError:
                continue
            utc_moment = local_moment.astimezone(timezone.utc)
            if not utc_moment <= now and (
                next_window_candidate is None or utc_moment < next_window_candidate[0]
            ):
                next_window_candidate = (utc_moment, schedule_window)
    return next_window_candidate


def _next_window_start(
    room: RoomConfig, local: datetime, now: datetime
) -> datetime | None:
    next_window_candidate = _next_window(room, local, now)
    if next_window_candidate is None:
        return
    return next_window_candidate[0]


def _apply_preheat(
    settings: HeatingSettings,
    room: RoomConfig,
    presence: str,
    window_on: bool,
    local: datetime,
    now: datetime,
    target: float,
    reason: str,
    mode: str,
    model: RoomModel | None,
    actual: float | None,
    outdoor_c: float | None,
) -> tuple[float, str, str, datetime | None]:
    if (
        window_on
        or model is None
        or (not model.ready)
        or (model.rate_k_per_h is None)
        or (model.tau_h is None)
        or (actual is None)
        or (outdoor_c is None)
    ):
        return (target, reason, mode, None)
    rate_k_per_h = model.rate_k_per_h
    tau_h = model.tau_h

    def toward(
        goal: float, moment: datetime | None
    ) -> tuple[float, str, str, datetime] | None:
        if moment is None:
            return
        hours_until_goal = (moment - now).total_seconds() / 3600
        if hours_until_goal <= 0:
            return
        predicted_temperature_c = expected_temperature(
            actual, outdoor_c, tau_h, hours_until_goal
        )
        lead_hours = preheat_lead_hours(goal, predicted_temperature_c, rate_k_per_h)
        if lead_hours <= 0 or hours_until_goal > lead_hours:
            return None
        preheat_target_c = clamp_setpoint(goal, settings.frost_floor_c)
        if preheat_target_c <= target + ABS_TOLERANCE:
            return
        return (preheat_target_c, "preheat", "comfort", moment)

    if (
        room.schedule_enabled
        and reason == "schedule_setback"
        and (not room.presence_enabled or not presence == "away")
    ):
        next_window = _next_window(room, local, now)
        if next_window is not None:
            utc_moment, schedule_window = next_window
            preheat_result = toward(_window_goal(room, schedule_window), utc_moment)
            if preheat_result is not None:
                return preheat_result
    if reason == "vacation" and local.tzinfo is not None:
        vacation_end_local = _civil_local(settings.vacation_end, local.tzinfo)
        if vacation_end_local is not None:
            post_vacation_target_c, unused_target_metadata, unused_target_metadata = (
                _target_for(settings, room, presence, False, vacation_end_local)
            )
            preheat_result = toward(
                post_vacation_target_c, vacation_end_local.astimezone(timezone.utc)
            )
            if preheat_result is not None:
                return preheat_result
    return (target, reason, mode, None)


def apply_lift(
    target: float,
    reason: str,
    mode: str,
    lift_k: float,
    kind: str | None,
    frost_floor_c: float,
) -> tuple[float, str, str]:
    """Add lift_k to a comfort or preheat target. Never above that target plus 2 K or 30 °C."""
    if kind not in {"pv", "price"} or reason not in _LIFT_REASONS:
        return (target, reason, mode)
    lift_ceiling_c = min(float(target) + LIFT_CAP_K, HARD_MAX_C)
    lifted_target_c = clamp_setpoint(
        min(float(target) + float(lift_k), lift_ceiling_c), frost_floor_c
    )
    if lifted_target_c <= target + ABS_TOLERANCE:
        return (target, reason, mode)
    lift_reason = "pv_lift" if kind == "pv" else "price_lift"
    return (lifted_target_c, lift_reason, lift_reason)


def advance_lift_signal(
    since: datetime | None, raw_on: bool, now: datetime
) -> tuple[datetime | None, bool]:
    """A lift signal counts only after it has stayed on for ten minutes."""
    if not raw_on:
        return (None, False)
    since_at = since or now
    return (since_at, now - since_at >= LIFT_HOLD)


def choose_lift(pv_ready: bool, cheap_ready: bool) -> str | None:
    if pv_ready:
        return "pv"
    if cheap_ready:
        return "price"


def find_pv_surplus_state(hass: Any) -> Any | None:
    """Same entity search as the EMS surplus sensor, without changing EMS."""
    states = getattr(hass, "states", None)
    if states is None:
        return
    surplus_entity_id = "binary_sensor.solar_forecast_stats_pv_surplus_available"
    state = states.get(surplus_entity_id)
    if state is not None:
        return state
    all_states = getattr(states, "async_all", None)
    if not callable(all_states):
        return
    try:
        binary_sensor_states = all_states("binary_sensor")
    except Exception:
        return
    for candidate_state in binary_sensor_states:
        if not str(getattr(candidate_state, "entity_id", "")).endswith(
            "pv_surplus_available"
        ):
            continue
        return candidate_state


def pv_surplus_on(hass: Any) -> bool:
    state = find_pv_surplus_state(hass)
    if state is None:
        return False
    return str(getattr(state, "state", "")).lower() == "on"


def cheap_price_on(hass: Any, now: datetime, resolver: Any = None) -> bool:
    """True only for a live cheap hour. A missing runtime or any error is no signal."""
    try:
        if resolver is None:
            from .grid_charge_gate import resolve_hour_is_cheap as cheap_hour_resolver

            resolver = cheap_hour_resolver
        cheap_hour = resolver(hass, now)
    except Exception:
        return False
    return cheap_hour is True


def trim_window_trace(
    samples: tuple[tuple[datetime, float], ...], now: datetime
) -> tuple[tuple[datetime, float], ...]:
    return tuple(
        (
            sample
            for sample in samples
            if timedelta(0) <= now - sample[0] <= DETECT_BUFFER
        )
    )


def detect_window_drop(
    samples: tuple[tuple[datetime, float], ...], now: datetime, heating: bool
) -> bool:
    """True when the room fell by at least 1 K inside ten minutes while it is heating."""
    if not heating:
        return False
    recent_samples = [
        sample for sample in samples if timedelta(0) <= now - sample[0] <= DETECT_WITHIN
    ]
    if len(recent_samples) < 2:
        return False
    latest_at, latest_temperature_c = max(recent_samples, key=lambda item: item[0])
    prior_temperatures_c = [
        sample_temperature_c
        for sample_at, sample_temperature_c in recent_samples
        if sample_at < latest_at
    ]
    if not prior_temperatures_c:
        return False
    return max(prior_temperatures_c) - latest_temperature_c >= DETECT_DROP_K - 1e-09


def advance_window_detection(
    samples: tuple[tuple[datetime, float], ...],
    detected_until: datetime | None,
    now: datetime,
    heating: bool,
    actual: float | None,
) -> tuple[tuple[tuple[datetime, float], ...], datetime | None, bool]:
    """Record one sample and return the trace, the hold end, and whether detection is active."""
    window_trace = trim_window_trace(samples, now)
    if actual is not None:
        window_trace = trim_window_trace((*window_trace, (now, float(actual))), now)
    if detected_until is not None and now < detected_until:
        return (window_trace, detected_until, True)
    if detect_window_drop(window_trace, now, heating):
        return ((), now + DETECT_HOLD, True)
    return (window_trace, None, False)


def _next_change(
    settings: HeatingSettings,
    room: RoomConfig,
    presence: str,
    presence_reason: str,
    window_on: bool,
    away_since: datetime | None,
    now: datetime,
    local: datetime,
    target: float,
    *,
    boost_c: float | None = None,
    boost_until: datetime | None = None,
    detected_until: datetime | None = None,
) -> tuple[datetime | None, float | None]:
    """Soonest future moment the target temperature changes. A contact has no known end."""
    if window_on:
        return (None, None)

    def forecast(moment: datetime, presence_at: str, local_at: datetime) -> float:
        active_boost_c = (
            boost_c if boost_until is not None and moment < boost_until else None
        )
        detection_still_active = detected_until is not None and moment < detected_until
        future_target_c, unused_target_metadata, unused_target_metadata = _target_for(
            settings,
            room,
            presence_at,
            False,
            local_at,
            boost_c=active_boost_c,
            detected_open=detection_still_active,
        )
        return future_target_c

    change_candidates = []
    if detected_until is not None and detected_until > now:
        future_target_c = forecast(detected_until, presence, _as_local(detected_until))
        if abs(future_target_c - target) > ABS_TOLERANCE:
            change_candidates.append((detected_until, future_target_c))
    if boost_until is not None and boost_until > now:
        future_target_c = forecast(boost_until, presence, _as_local(boost_until))
        if abs(future_target_c - target) > ABS_TOLERANCE:
            change_candidates.append((boost_until, future_target_c))
    if (
        presence_reason == "presence_pending"
        and away_since is not None
        and room.presence_enabled
    ):
        change_at = away_since + timedelta(minutes=settings.away_delay_min)
        if change_at > now:
            change_candidates.append(
                (change_at, forecast(change_at, "away", _as_local(change_at)))
            )
    if local.tzinfo is not None:
        for boundary_text in (settings.vacation_start, settings.vacation_end):
            local_moment = _civil_local(boundary_text, local.tzinfo)
            if local_moment is not None:
                utc_moment = local_moment.astimezone(timezone.utc)
                if utc_moment <= now:
                    continue
                future_target_c = forecast(utc_moment, presence, local_moment)
                if abs(future_target_c - target) > ABS_TOLERANCE:
                    change_candidates.append((utc_moment, future_target_c))
    if room.schedule_enabled and (not room.presence_enabled or not presence == "away"):
        if boost_c is None:
            for day_offset in range(8):
                day = (local + timedelta(days=day_offset)).date()
                for schedule_window in room.schedule:
                    for boundary_text in (schedule_window.start, schedule_window.end):
                        hour_text, minute_text = boundary_text.split(":")
                        try:
                            local_moment = datetime(
                                day.year,
                                day.month,
                                day.day,
                                int(hour_text),
                                int(minute_text),
                                tzinfo=local.tzinfo,
                            )
                        except ValueError:
                            continue
                        utc_moment = local_moment.astimezone(timezone.utc)
                        if utc_moment <= now:
                            continue
                        future_target_c = forecast(
                            utc_moment, presence, _as_local(utc_moment)
                        )
                        if abs(future_target_c - target) > ABS_TOLERANCE:
                            change_candidates.append((utc_moment, future_target_c))
                continue
    if not change_candidates:
        return (None, None)
    change_at, future_target_c = min(change_candidates, key=lambda item: item[0])
    return (change_at, future_target_c)


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= ABS_TOLERANCE


def decide_room(
    *,
    settings: HeatingSettings,
    room: RoomConfig,
    now: datetime,
    presence_states: dict[str, str | None],
    away_since: datetime | None,
    previous_presence: str | None,
    window_on: bool,
    readings: dict[str, ThermoReading],
    memory: dict[str, ThermoMemory],
    license_live: bool,
    feature_enabled: bool,
    allow_writes: bool,
    release_override: bool = False,
    heating_season: bool | None = None,
    started_at: datetime | None = None,
    room_model: RoomModel | None = None,
    room_actual: float | None = None,
    outdoor_c: float | None = None,
    offsets: dict[str, float] | None = None,
    boost_c: float | None = None,
    boost_until: datetime | None = None,
    detected_open: bool = False,
    detected_until: datetime | None = None,
    ease_decrease: bool = False,
    lift: str | None = None,
) -> RoomDecision:
    """Decide one room without touching Home Assistant."""
    current_presence, current_away_since, presence_reason = resolve_presence(
        presence_states,
        effective_presence_entities(settings, room),
        away_since,
        now,
        settings.away_delay_min,
    )
    presence_changed = (
        previous_presence is not None and previous_presence != current_presence
    )
    release_requested = release_override or presence_changed
    local_now = _as_local(now)
    target_c, target_reason, target_mode = _target_for(
        settings,
        room,
        current_presence,
        window_on,
        local_now,
        boost_c=boost_c,
        detected_open=detected_open,
    )
    preheat_for = None
    target_c, target_reason, target_mode, preheat_for = _apply_preheat(
        settings,
        room,
        current_presence,
        window_on,
        local_now,
        now,
        target_c,
        target_reason,
        target_mode,
        room_model,
        room_actual,
        outdoor_c,
    )
    if room.lift_enabled and (not detected_open) and (not window_on):
        target_c, target_reason, target_mode = apply_lift(
            target_c,
            target_reason,
            target_mode,
            settings.lift_k,
            lift,
            settings.frost_floor_c,
        )
    if not license_live:
        gate_reason, gate_mode = ("locked", "locked")
    elif not feature_enabled:
        gate_reason, gate_mode = ("disabled", "disabled")
    else:
        gate_reason, gate_mode = (None, None)
    thermostat_decisions = []
    manual_override = False
    for entity_id in room.climate_entities:
        thermostat_memory = memory.get(entity_id, ThermoMemory())
        thermostat_reading = readings.get(
            entity_id, ThermoReading(False, None, None, False)
        )
        if release_requested:
            thermostat_memory = replace(
                thermostat_memory, override_until_presence_change=False
            )
        original_reading = thermostat_reading
        if _in_startup_grace(thermostat_reading, started_at, now) or _same_seen(
            thermostat_memory, thermostat_reading
        ):
            thermostat_reading = replace(
                thermostat_reading,
                reported_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
        if thermostat_memory.awaiting_since is not None and _report_after(
            thermostat_reading, thermostat_memory.awaiting_since, now
        ):
            if _report_matches(
                thermostat_memory, thermostat_reading, settings.frost_floor_c
            ):
                thermostat_memory = replace(
                    thermostat_memory,
                    awaiting_since=None,
                    attempt_count=0,
                    confirmed_at=now,
                )
            elif not release_requested and gate_reason is None:
                thermostat_memory = replace(
                    thermostat_memory,
                    override_until_presence_change=True,
                    awaiting_since=None,
                )
                manual_override = True
        elif thermostat_memory.awaiting_since is None and _report_after(
            thermostat_reading, _sent_at(thermostat_memory), now
        ):
            written_setpoint_c = (
                quantize_setpoint(
                    thermostat_memory.last_written_c,
                    thermostat_reading.step,
                    settings.frost_floor_c,
                    thermostat_reading.min_temp,
                    thermostat_reading.max_temp,
                )
                if thermostat_memory.last_written_c is not None
                else None
            )
            reported_hvac_mode = (thermostat_reading.hvac_mode or "").lower()
            setpoint_changed = (
                written_setpoint_c is not None
                and thermostat_reading.setpoint_c is not None
                and (not _close(thermostat_reading.setpoint_c, written_setpoint_c))
            )
            hvac_mode_changed = (
                bool(thermostat_memory.last_hvac_mode)
                and bool(reported_hvac_mode)
                and (reported_hvac_mode != thermostat_memory.last_hvac_mode)
            )
            if not release_requested and gate_reason is None:
                if setpoint_changed or hvac_mode_changed:
                    thermostat_memory = replace(
                        thermostat_memory, override_until_presence_change=True
                    )
                    manual_override = True
        thermostat_memory = _remember_seen(thermostat_memory, original_reading)
        thermostat_offset_k = None if offsets is None else offsets.get(entity_id)
        thermostat_target_c = (
            target_c if thermostat_offset_k is None else target_c - thermostat_offset_k
        )
        thermostat_decision = replace(
            _decide_thermostat(
                entity_id=entity_id,
                reading=thermostat_reading,
                memory=thermostat_memory,
                target=thermostat_target_c,
                target_reason=target_reason,
                target_mode=target_mode,
                presence=current_presence,
                room_active=room.active,
                gate_reason=gate_reason,
                gate_mode=gate_mode,
                allow_writes=allow_writes,
                now=now,
                frost_floor_c=settings.frost_floor_c,
                heating_season=heating_season,
                auto_hvac=room.auto_hvac,
                ease_decrease=ease_decrease,
            ),
            offset_k=thermostat_offset_k,
        )
        if thermostat_decision.reason == "manual_override":
            manual_override = True
        thermostat_decisions.append(thermostat_decision)
        continue
    if gate_reason is not None:
        reason, mode = (gate_reason, gate_mode)
    elif manual_override:
        reason, mode = ("manual_override", "override")
    elif not room.active:
        reason, mode = ("room_inactive", "observe")
    else:
        reason, mode = _summarize_reasons(
            thermostat_decisions, target_reason, target_mode
        )
    next_change_at, next_target_c = _next_change(
        settings,
        room,
        current_presence,
        presence_reason,
        window_on,
        current_away_since,
        now,
        local_now,
        target_c,
        boost_c=boost_c if target_reason == "boost" else None,
        boost_until=boost_until if target_reason == "boost" else None,
        detected_until=detected_until if detected_open and (not window_on) else None,
    )
    return RoomDecision(
        room_id=room.id,
        target_c=target_c,
        mode=mode or "observe",
        reason=reason or presence_reason,
        presence=current_presence,
        away_since=current_away_since,
        thermostats=tuple(thermostat_decisions),
        next_at=next_change_at,
        next_target_c=next_target_c,
        preheat_for=preheat_for,
    )


def _summarize_reasons(
    thermostats: list[ThermoDecision], target_reason: str, target_mode: str
) -> tuple[str, str]:
    """When every thermostat is blocked, the room reason is that shared block (or mixed)."""
    if not thermostats:
        return (target_reason, target_mode)
    if any(
        (
            thermostat_decision.write or thermostat_decision.hvac_write
            for thermostat_decision in thermostats
        )
    ):
        if all(
            (
                thermostat_decision.hvac_write and (not thermostat_decision.write)
                for thermostat_decision in thermostats
            )
        ):
            return ("hvac_switched", target_mode)
        return (target_reason, target_mode)
    thermostat_reasons = [
        thermostat_decision.reason for thermostat_decision in thermostats
    ]
    if all(
        (thermostat_reason in _CANNOT_WRITE for thermostat_reason in thermostat_reasons)
    ):
        unique_reasons = set(thermostat_reasons)
        if len(unique_reasons) == 1:
            return (thermostat_reasons[0], target_mode)
        return ("mixed_reasons", target_mode)
    return (target_reason, target_mode)


def _sent_at(memory: ThermoMemory) -> datetime | None:
    write_times = [
        sent_at
        for sent_at in (
            memory.awaiting_since,
            memory.last_write_at,
            memory.last_hvac_write_at,
        )
        if sent_at
    ]
    return max(write_times) if write_times else None


def _in_startup_grace(
    reading: ThermoReading, started_at: datetime | None, now: datetime
) -> bool:
    """Restored entity states in the first three minutes are not a device report."""
    if started_at is None:
        return False
    report_at = reading.reported_at if reading.reported_at is not None else now
    return report_at < started_at + STARTUP_GRACE


def _same_seen(memory: ThermoMemory, reading: ThermoReading) -> bool:
    """A new last_updated with the same mode and setpoint is not a new report."""
    if not memory.seen_known:
        return False
    reported_hvac_mode = (reading.hvac_mode or "").lower()
    previously_seen_mode = (memory.seen_hvac or "").lower()
    if reported_hvac_mode != previously_seen_mode:
        return False
    if reading.setpoint_c is None or memory.seen_setpoint_c is None:
        return reading.setpoint_c is None and memory.seen_setpoint_c is None
    return _close(reading.setpoint_c, memory.seen_setpoint_c)


def _remember_seen(memory: ThermoMemory, reading: ThermoReading) -> ThermoMemory:
    return replace(
        memory,
        seen_hvac=(reading.hvac_mode or "").lower() or None,
        seen_setpoint_c=reading.setpoint_c,
        seen_known=True,
    )


def _report_after(reading: ThermoReading, sent: datetime | None, now: datetime) -> bool:
    """A missing timestamp counts as this cycle. An older last_updated is still the pre-write state."""
    if sent is None:
        return True
    report_at = reading.reported_at if reading.reported_at is not None else now
    return report_at > sent


def _report_matches(
    memory: ThermoMemory, reading: ThermoReading, frost_floor_c: float
) -> bool:
    reported_hvac_mode = (reading.hvac_mode or "").lower()
    if memory.pending_hvac == "off":
        return reported_hvac_mode == "off"
    if memory.pending_hvac == "heat" and reported_hvac_mode != "heat":
        return False
    if memory.last_written_c is None or reading.setpoint_c is None:
        return memory.pending_hvac == "off"
    written_setpoint_c = quantize_setpoint(
        memory.last_written_c,
        reading.step,
        frost_floor_c,
        reading.min_temp,
        reading.max_temp,
    )
    return _close(reading.setpoint_c, written_setpoint_c)


def _retry_due(memory: ThermoMemory, now: datetime) -> bool:
    if memory.awaiting_since is None:
        return False
    retry_interval = (
        RETRY_AFTER if memory.attempt_count < MAX_FAST_ATTEMPTS else SLOW_RETRY
    )
    return now - memory.awaiting_since >= retry_interval


def _mark_sent(
    memory: ThermoMemory, now: datetime, target: float, pending: str | None
) -> ThermoMemory:
    return replace(
        memory,
        last_written_c=target,
        last_write_at=now,
        last_hvac_write_at=now if pending else memory.last_hvac_write_at,
        last_hvac_mode=pending or memory.last_hvac_mode,
        awaiting_since=now,
        attempt_count=memory.attempt_count + 1,
        pending_hvac=pending,
        last_failed_at=None,
        override_until_presence_change=False,
    )


def _decide_thermostat(
    *,
    entity_id: str,
    reading: ThermoReading,
    memory: ThermoMemory,
    target: float,
    target_reason: str,
    target_mode: str,
    presence: str,
    room_active: bool,
    gate_reason: str | None,
    gate_mode: str | None,
    allow_writes: bool,
    now: datetime,
    frost_floor_c: float,
    heating_season: bool | None = None,
    auto_hvac: bool = True,
    ease_decrease: bool = False,
) -> ThermoDecision:
    target = quantize_setpoint(
        target, reading.step, frost_floor_c, reading.min_temp, reading.max_temp
    )

    def hold(reason: str, mode: str) -> ThermoDecision:
        return ThermoDecision(entity_id, False, target, reason, mode, presence, memory)

    if gate_reason is not None:
        return (
            (
                (hold(gate_reason, gate_mode or gate_reason) if True else None)
                if True
                else None
            )
            if True
            else None
        )
    if memory.override_until_presence_change:
        return (
            ((hold("manual_override", "override") if True else None) if True else None)
            if True
            else None
        )
    if not room_active:
        return (
            ((hold("room_inactive", "observe") if True else None) if True else None)
            if True
            else None
        )
    if not allow_writes:
        return (
            ((hold("startup_hold", target_mode) if True else None) if True else None)
            if True
            else None
        )
    if not reading.available:
        return (
            (
                (hold("thermostat_unavailable", target_mode) if True else None)
                if True
                else None
            )
            if True
            else None
        )
    if memory.awaiting_since is not None and (
        not _report_after(reading, memory.awaiting_since, now)
    ):
        if not _retry_due(memory, now):
            reason = (
                "device_not_responding"
                if memory.attempt_count >= MAX_FAST_ATTEMPTS
                else "awaiting_device"
            )
            return (
                ((hold(reason, target_mode) if True else None) if True else None)
                if True
                else None
            )
        pending_hvac_mode = memory.pending_hvac
        updated_memory = _mark_sent(memory, now, target, pending_hvac_mode)
        if pending_hvac_mode == "off":
            return (
                (
                    (
                        ThermoDecision(
                            entity_id,
                            False,
                            target,
                            "awaiting_device",
                            target_mode,
                            presence,
                            updated_memory,
                            True,
                            "off",
                        )
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        retry_heat = pending_hvac_mode == "heat"
        return (
            (
                (
                    ThermoDecision(
                        entity_id,
                        True,
                        target,
                        "awaiting_device",
                        target_mode,
                        presence,
                        updated_memory,
                        retry_heat,
                        "heat" if retry_heat else None,
                    )
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
    write_hvac, hvac_target = _plan_hvac(
        reading, memory, heating_season, auto_hvac, now, allow_writes=True
    )
    if write_hvac and hvac_target == "heat":
        updated_memory = _mark_sent(memory, now, target, "heat")
        return (
            (
                (
                    ThermoDecision(
                        entity_id,
                        True,
                        target,
                        "awaiting_device",
                        target_mode,
                        presence,
                        updated_memory,
                        True,
                        "heat",
                    )
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
    if write_hvac and hvac_target == "off":
        updated_memory = _mark_sent(
            memory,
            now,
            memory.last_written_c if memory.last_written_c is not None else target,
            "off",
        )
        return (
            (
                (
                    ThermoDecision(
                        entity_id,
                        False,
                        target,
                        "awaiting_device",
                        target_mode,
                        presence,
                        updated_memory,
                        True,
                        "off",
                    )
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
    if reading.uses_range:
        return (
            ((hold("range_setpoint", target_mode) if True else None) if True else None)
            if True
            else None
        )
    heat_supported = "heat" in {hvac_mode.lower() for hvac_mode in reading.hvac_modes}
    if (reading.hvac_mode or "").lower() == "off":
        if heating_season is not True or not auto_hvac or heat_supported:
            return (
                ((hold("hvac_off", target_mode) if True else None) if True else None)
                if True
                else None
            )
    if reading.setpoint_c is None:
        return (
            (
                (hold("setpoint_unreadable", target_mode) if True else None)
                if True
                else None
            )
            if True
            else None
        )
    previous_target_c = (
        quantize_setpoint(
            memory.last_written_c,
            reading.step,
            frost_floor_c,
            reading.min_temp,
            reading.max_temp,
        )
        if memory.last_written_c is not None
        else None
    )
    if _close(target, previous_target_c):
        return (
            ((hold("same_value", target_mode) if True else None) if True else None)
            if True
            else None
        )
    if abs(reading.setpoint_c - target) < DEADBAND_C:
        return (
            ((hold("within_band", target_mode) if True else None) if True else None)
            if True
            else None
        )
    if (
        memory.last_failed_at is not None
        and now - memory.last_failed_at < INCREASE_LIMIT
    ):
        return (
            ((hold("write_failed", target_mode) if True else None) if True else None)
            if True
            else None
        )
    last_confirmed_write_at = (
        memory.confirmed_at
        if memory.confirmed_at is not None
        else memory.last_write_at if memory.attempt_count == 0 else None
    )
    is_decrease = target < reading.setpoint_c - ABS_TOLERANCE
    window_safety_drop = (
        target_reason in {"window_open", "window_detected"} and is_decrease
    )
    allow_boost_drop = ease_decrease and is_decrease and (target_reason != "boost")
    if (
        is_decrease
        and (not window_safety_drop)
        and (not allow_boost_drop)
        and (last_confirmed_write_at is not None)
        and (now - last_confirmed_write_at < DECREASE_LIMIT)
    ):
        return (
            (
                (hold("decrease_limited", target_mode) if True else None)
                if True
                else None
            )
            if True
            else None
        )
    if not is_decrease and last_confirmed_write_at is not None:
        if now - last_confirmed_write_at < INCREASE_LIMIT:
            return (
                (
                    (hold("increase_limited", target_mode) if True else None)
                    if True
                    else None
                )
                if True
                else None
            )
    updated_memory = _mark_sent(memory, now, target, None)
    return (
        (
            (
                ThermoDecision(
                    entity_id,
                    True,
                    target,
                    "awaiting_device",
                    target_mode,
                    presence,
                    updated_memory,
                )
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


_EASE_IDLE = frozenset(
    {"hvac_off", "same_value", "within_band", "room_inactive", "manual_override"}
)


def boost_return_settled(
    thermostats: tuple[ThermoDecision, ...],
    readings: dict[str, ThermoReading] | None = None,
) -> bool:
    """Drop the one-shot bypass only when every thermostat lowered or needs no drop."""
    for thermostat_decision in thermostats:
        if thermostat_decision.reason in _EASE_IDLE:
            continue
        thermostat_reading = (
            None if readings is None else readings.get(thermostat_decision.entity_id)
        )
        setpoint_c = (
            None if thermostat_reading is None else thermostat_reading.setpoint_c
        )
        decrease_sent = (
            thermostat_decision.write
            and thermostat_decision.target_c is not None
            and (setpoint_c is not None)
            and (thermostat_decision.target_c < setpoint_c - ABS_TOLERANCE)
        )
        if decrease_sent:
            continue
        return False
    return True


def _plan_hvac(
    reading: ThermoReading,
    memory: ThermoMemory,
    heating_season: bool | None,
    auto_hvac: bool,
    now: datetime,
    *,
    allow_writes: bool,
) -> tuple[bool, str | None]:
    """heat when the season is on, off when it is off. None means leave the mode alone."""
    if (
        not allow_writes
        or not auto_hvac
        or heating_season is None
        or (not reading.available)
    ):
        return (False, None)
    if memory.override_until_presence_change:
        return (False, None)
    desired_hvac_mode = "heat" if heating_season else "off"
    supported_hvac_modes = {hvac_mode.lower() for hvac_mode in reading.hvac_modes}
    if desired_hvac_mode not in supported_hvac_modes:
        return (False, None)
    if (reading.hvac_mode or "").lower() == desired_hvac_mode:
        return (False, None)
    if (
        memory.confirmed_at is not None
        and memory.last_hvac_write_at is not None
        and (memory.awaiting_since is None)
        and (now - memory.last_hvac_write_at < HVAC_LIMIT)
        and (memory.last_hvac_mode == desired_hvac_mode)
    ):
        return (False, None)
    if (
        memory.last_failed_at is not None
        and now - memory.last_failed_at < INCREASE_LIMIT
    ):
        return (False, None)
    return (True, desired_hvac_mode)


def slug_room_id(name: str, taken: set[str]) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-") or "room"
    slug = slug[:40]
    if slug not in taken:
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in taken:
        suffix += 1
    return f"{slug}-{suffix}"


def validate_settings(
    payload: dict[str, Any], current: HeatingSettings
) -> HeatingSettings:
    if not isinstance(payload, dict):
        raise HeatingError("invalid_json", "A JSON object is required.")
    allowed_fields = {
        "lift_k",
        "lift_on_pv",
        "base_temp_c",
        "vacation_end",
        "frost_floor_c",
        "lift_on_cheap",
        "away_delay_min",
        "outdoor_entity",
        "vacation_start",
        "heating_limit_c",
        "presence_entities",
    }
    if set(payload) - allowed_fields:
        raise HeatingError("unexpected_field", "Unexpected settings field.")
    base_temp_c = _temp(payload, "base_temp_c", current.base_temp_c, 5.0, 30.0)
    frost_floor_c = _temp(payload, "frost_floor_c", current.frost_floor_c, 5.0, 12.0)
    heating_limit_c = _temp(
        payload, "heating_limit_c", current.heating_limit_c, 5.0, 25.0
    )
    away_delay_min = current.away_delay_min
    if "away_delay_min" in payload:
        raw_value = payload["away_delay_min"]
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int)
            or (not 0 <= raw_value <= 240)
        ):
            raise HeatingError("invalid_delay", "Away delay must be 0 to 240 minutes.")
        away_delay_min = raw_value
    presence_entities = current.presence_entities
    if "presence_entities" in payload:
        presence_entities = _entity_list(
            payload["presence_entities"], "person", "invalid_person"
        )
    outdoor_entity = current.outdoor_entity
    if "outdoor_entity" in payload:
        outdoor_entity = _optional_outdoor(payload.get("outdoor_entity"))
    vacation_start, vacation_end = _vacation_pair(payload, current)
    lift_k = _lift_k(payload, current.lift_k)
    lift_on_pv = _bool_field(payload, "lift_on_pv", current.lift_on_pv)
    lift_on_cheap = _bool_field(payload, "lift_on_cheap", current.lift_on_cheap)
    return HeatingSettings(
        base_temp_c=base_temp_c,
        frost_floor_c=frost_floor_c,
        presence_entities=presence_entities,
        away_delay_min=away_delay_min,
        outdoor_entity=outdoor_entity,
        heating_limit_c=(
            heating_limit_c if heating_limit_c is not None else current.heating_limit_c
        ),
        vacation_start=vacation_start,
        vacation_end=vacation_end,
        lift_k=lift_k,
        lift_on_pv=lift_on_pv,
        lift_on_cheap=lift_on_cheap,
    )


def validate_room(payload: dict[str, Any], taken: set[str]) -> RoomConfig:
    if not isinstance(payload, dict):
        raise HeatingError("invalid_json", "A JSON object is required.")
    allowed_fields = {
        "mode",
        "name",
        "schedule",
        "auto_hvac",
        "temp_sensor",
        "lift_enabled",
        "window_detect",
        "window_sensor",
        "comfort_temp_c",
        "setback_temp_c",
        "climate_entities",
        "presence_enabled",
        "presence_persons",
        "schedule_enabled",
    }
    if set(payload) - allowed_fields:
        raise HeatingError("unexpected_field", "Unexpected room field.")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise HeatingError("invalid_name", "Room name is required.")
    climate_entities = _entity_list(
        payload.get("climate_entities"), "climate", "invalid_climate"
    )
    if not climate_entities:
        raise HeatingError(
            "climate_required", "At least one climate entity is required."
        )
    temp_sensor = _optional_entity(
        payload.get("temp_sensor"), "sensor", "invalid_temp_sensor"
    )
    window_sensor = _optional_entity(
        payload.get("window_sensor"), "binary_sensor", "invalid_window_sensor"
    )
    comfort_temp_c = _temp(payload, "comfort_temp_c", None, 5.0, 30.0)
    if comfort_temp_c is None:
        raise HeatingError("invalid_temperature", "Comfort temperature is required.")
    setback_temp_c = _optional_temp(payload, "setback_temp_c", 5.0, 30.0)
    presence_enabled, schedule_enabled = _resolve_flags(payload)
    presence_persons = _presence_persons(payload)
    schedule_windows = _schedule(payload.get("schedule", []))
    auto_hvac = payload.get("auto_hvac", True)
    if not isinstance(auto_hvac, bool):
        raise HeatingError("invalid_active", "auto_hvac must be a boolean.")
    window_detect = _bool_field(payload, "window_detect", False)
    lift_enabled = _bool_field(payload, "lift_enabled", False)
    return RoomConfig(
        id=slug_room_id(name, taken),
        name=name.strip(),
        climate_entities=climate_entities,
        comfort_temp_c=comfort_temp_c,
        temp_sensor=temp_sensor,
        window_sensor=window_sensor,
        active=False,
        schedule=schedule_windows,
        auto_hvac=auto_hvac,
        setback_temp_c=setback_temp_c,
        presence_enabled=presence_enabled,
        schedule_enabled=schedule_enabled,
        presence_persons=presence_persons,
        window_detect=window_detect,
        lift_enabled=lift_enabled,
    )


def _temp(
    payload: dict[str, Any], key: str, default: float | None, low: float, high: float
) -> float | None:
    if key not in payload:
        return default
    raw_value = payload[key]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise HeatingError("invalid_temperature", f"{key} is out of range.")
    temperature_c = float(raw_value)
    if (
        temperature_c < low
        or temperature_c > high
        or abs(temperature_c * 2 - round(temperature_c * 2)) > 1e-06
    ):
        raise HeatingError(
            "invalid_temperature", f"{key} must be a 0.5 step inside range."
        )
    return temperature_c


def _optional_temp(
    payload: dict[str, Any], key: str, low: float, high: float
) -> float | None:
    if key not in payload or payload[key] in (None, ""):
        return None
    return _temp(payload, key, None, low, high)


def _entity_list(value: Any, domain: str, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise HeatingError(code, f"Expected a short list of {domain} entities.")
    entity_ids = []
    for raw_entity_id in value:
        entity_id = _require_domain(raw_entity_id, domain, code)
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return tuple(entity_ids)


def _optional_outdoor(value: Any) -> str | None:
    if value in (None, ""):
        return
    if isinstance(value, str):
        if value.startswith("sensor.") or value.startswith("weather."):
            return value
    raise HeatingError(
        "invalid_outdoor", "Outdoor source must be a sensor or weather entity."
    )


def _schedule(value: Any) -> tuple[ScheduleWindow, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or len(value) > 12:
        raise HeatingError("invalid_schedule", "At most 12 schedule windows.")
    windows = []
    for window_payload in value:
        if not isinstance(window_payload, dict):
            raise HeatingError(
                "invalid_schedule", "A schedule window must be an object."
            )
        raw_days = window_payload.get("days")
        if not isinstance(raw_days, list) or not raw_days or len(raw_days) > 7:
            raise HeatingError("invalid_schedule", "Each window needs weekdays 0 to 6.")
        weekdays = []
        for weekday in raw_days:
            if (
                isinstance(weekday, bool)
                or not isinstance(weekday, int)
                or (not 0 <= weekday <= 6)
                or (weekday in weekdays)
            ):
                raise HeatingError(
                    "invalid_schedule", "Weekdays must be unique numbers from 0 to 6."
                )
            weekdays.append(weekday)
        start_time, end_time = (window_payload.get("start"), window_payload.get("end"))
        if not (
            isinstance(start_time, str)
            and isinstance(end_time, str)
            and _HHMM.match(start_time)
            and _HHMM.match(end_time)
        ):
            raise HeatingError("invalid_schedule", "Start and end must be HH:MM.")
        if start_time == end_time:
            raise HeatingError("invalid_schedule", "A window needs a real duration.")
        windows.append(
            ScheduleWindow(
                tuple(weekdays),
                start_time,
                end_time,
                _optional_temp(window_payload, "temp_c", 5.0, 30.0),
            )
        )
        continue
    return tuple(windows)


def _flags_from_mode(mode: str) -> tuple[bool, bool]:
    if mode == "combined":
        return (True, True)
    if mode == "schedule":
        return (False, True)
    if mode == "fixed":
        return (False, False)
    return (True, False)


def _require_mode(mode: Any) -> str:
    if mode not in _MODES:
        raise HeatingError(
            "invalid_mode", "Room mode must be auto, schedule, combined or fixed."
        )
    return str(mode)


def _require_flag(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise HeatingError("invalid_flag", f"{key} must be a boolean.")
    return value


def _bool_field(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    return _require_flag(payload[key], key)


def _lift_k(payload: dict[str, Any], default: float) -> float:
    if "lift_k" not in payload:
        return default
    raw_value = payload["lift_k"]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise HeatingError("invalid_lift", "lift_k is out of range.")
    lift_k = float(raw_value)
    if lift_k < 0.5 or lift_k > 2.0:
        raise HeatingError("invalid_lift", "lift_k must be from 0.5 to 2.0.")
    return lift_k


def validate_boost(payload: dict[str, Any]) -> tuple[float, int]:
    if not isinstance(payload, dict) or set(payload) != {"temp_c", "minutes"}:
        raise HeatingError("invalid_boost", "Boost needs temp_c and minutes.")
    try:
        boost_temp_c = _temp(payload, "temp_c", None, 5.0, 30.0)
    except HeatingError as error:
        raise HeatingError(
            "invalid_boost", "Boost temperature must be a 0.5 step from 5 to 30."
        ) from error
    if boost_temp_c is None:
        raise HeatingError("invalid_boost", "Boost temperature is required.")
    boost_minutes = payload["minutes"]
    if (
        isinstance(boost_minutes, bool)
        or not isinstance(boost_minutes, int)
        or (not 30 <= boost_minutes <= 1440)
    ):
        raise HeatingError(
            "invalid_boost", "Boost duration must be 30 to 1440 minutes."
        )
    return (boost_temp_c, boost_minutes)


def _resolve_flags(payload: dict[str, Any]) -> tuple[bool, bool]:
    """Old mode sets the checkboxes. Explicit checkboxes win when both are sent."""
    explicit_flags = "presence_enabled" in payload or "schedule_enabled" in payload
    if not explicit_flags:
        return _flags_from_mode(_require_mode(payload.get("mode", "auto")))
    if "mode" in payload:
        mode = _require_mode(payload.get("mode"))
    else:
        mode = "auto"
    presence_enabled, schedule_enabled = _flags_from_mode(mode)
    if "presence_enabled" in payload:
        presence_enabled = _require_flag(
            payload["presence_enabled"], "presence_enabled"
        )
    if "schedule_enabled" in payload:
        schedule_enabled = _require_flag(
            payload["schedule_enabled"], "schedule_enabled"
        )
    return (presence_enabled, schedule_enabled)


def _presence_persons(payload: dict[str, Any]) -> tuple[str, ...]:
    if "presence_persons" not in payload or payload.get("presence_persons") in (
        None,
        [],
    ):
        return ()
    return _entity_list(payload.get("presence_persons"), "person", "invalid_person")


def _vacation_field(value: Any) -> str | None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or _vacation_naive(value) is None:
        raise HeatingError(
            "invalid_vacation", "Vacation times must be YYYY-MM-DDTHH:MM."
        )
    return value


def _vacation_pair(
    payload: dict[str, Any], current: HeatingSettings
) -> tuple[str | None, str | None]:
    if "vacation_start" not in payload and "vacation_end" not in payload:
        return (current.vacation_start, current.vacation_end)
    start_at = (
        _vacation_field(payload["vacation_start"])
        if "vacation_start" in payload
        else current.vacation_start
    )
    end_at = (
        _vacation_field(payload["vacation_end"])
        if "vacation_end" in payload
        else current.vacation_end
    )
    if (start_at is None) != (end_at is None):
        raise HeatingError(
            "vacation_incomplete", "Vacation needs both start and end, or neither."
        )
    if start_at is None or end_at is None:
        return (None, None)
    vacation_start_local = _vacation_naive(start_at)
    vacation_end_local = _vacation_naive(end_at)
    if (
        vacation_start_local is None
        or vacation_end_local is None
        or vacation_end_local <= vacation_start_local
        or (vacation_end_local > vacation_start_local + timedelta(days=366))
    ):
        raise HeatingError(
            "invalid_vacation",
            "Vacation end must be after the start and within 366 days.",
        )
    return (start_at, end_at)


def _optional_entity(value: Any, domain: str, code: str) -> str | None:
    if value in (None, ""):
        return
    return _require_domain(value, domain, code)


def _require_domain(value: Any, domain: str, code: str) -> str:
    if not (isinstance(value, str) and value.startswith(f"{domain}.")):
        raise HeatingError(code, f"Entity must use the {domain} domain.")
    return value


def settings_to_dict(settings: HeatingSettings) -> dict[str, Any]:
    return {
        "base_temp_c": settings.base_temp_c,
        "frost_floor_c": settings.frost_floor_c,
        "presence_entities": list(settings.presence_entities),
        "away_delay_min": settings.away_delay_min,
        "outdoor_entity": settings.outdoor_entity,
        "heating_limit_c": settings.heating_limit_c,
        "vacation_start": settings.vacation_start,
        "vacation_end": settings.vacation_end,
        "lift_k": settings.lift_k,
        "lift_on_pv": settings.lift_on_pv,
        "lift_on_cheap": settings.lift_on_cheap,
    }


def room_to_dict(room: RoomConfig) -> dict[str, Any]:
    return {
        "id": room.id,
        "name": room.name,
        "climate_entities": list(room.climate_entities),
        "temp_sensor": room.temp_sensor,
        "window_sensor": room.window_sensor,
        "comfort_temp_c": room.comfort_temp_c,
        "setback_temp_c": room.setback_temp_c,
        "active": room.active,
        "mode": room.mode,
        "presence_enabled": room.presence_enabled,
        "schedule_enabled": room.schedule_enabled,
        "presence_persons": list(room.presence_persons),
        "window_detect": room.window_detect,
        "lift_enabled": room.lift_enabled,
        "auto_hvac": room.auto_hvac,
        "schedule": [
            {
                "days": list(schedule_window.days),
                "start": schedule_window.start,
                "end": schedule_window.end,
                "temp_c": schedule_window.temp_c,
            }
            for schedule_window in room.schedule
        ],
    }


def memory_to_dict(memory: ThermoMemory) -> dict[str, Any]:
    return {
        "last_written_c": memory.last_written_c,
        "last_write_at": (
            memory.last_write_at.isoformat() if memory.last_write_at else None
        ),
        "override_until_presence_change": memory.override_until_presence_change,
        "last_failed_at": (
            memory.last_failed_at.isoformat() if memory.last_failed_at else None
        ),
        "last_hvac_write_at": (
            memory.last_hvac_write_at.isoformat() if memory.last_hvac_write_at else None
        ),
        "last_hvac_mode": memory.last_hvac_mode,
        "awaiting_since": (
            memory.awaiting_since.isoformat() if memory.awaiting_since else None
        ),
        "attempt_count": memory.attempt_count,
        "confirmed_at": (
            memory.confirmed_at.isoformat() if memory.confirmed_at else None
        ),
        "pending_hvac": memory.pending_hvac,
        "seen_hvac": memory.seen_hvac,
        "seen_setpoint_c": memory.seen_setpoint_c,
        "seen_known": memory.seen_known,
    }


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return
    parsed_value = datetime.fromisoformat(value)
    if parsed_value.tzinfo is None:
        parsed_value = parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value


def memory_from_dict(payload: dict[str, Any]) -> ThermoMemory:
    written_c = payload.get("last_written_c")
    legacy_memory = "awaiting_since" not in payload and "attempt_count" not in payload
    override_active = bool(payload.get("override_until_presence_change"))
    last_write_at = _parse_stamp(payload.get("last_write_at"))
    last_hvac_write_at = _parse_stamp(payload.get("last_hvac_write_at"))
    if (
        legacy_memory
        and override_active
        and (last_write_at is not None or last_hvac_write_at is not None)
    ):
        override_active = False
    return ThermoMemory(
        float(written_c) if isinstance(written_c, (int, float)) else None,
        last_write_at,
        override_active,
        _parse_stamp(payload.get("last_failed_at")),
        last_hvac_write_at,
        (
            payload.get("last_hvac_mode")
            if isinstance(payload.get("last_hvac_mode"), str)
            else None
        ),
        _parse_stamp(payload.get("awaiting_since")),
        int(payload.get("attempt_count") or 0),
        _parse_stamp(payload.get("confirmed_at")),
        (
            payload.get("pending_hvac")
            if payload.get("pending_hvac") in {"off", "heat"}
            else None
        ),
        payload.get("seen_hvac") if isinstance(payload.get("seen_hvac"), str) else None,
        (
            float(payload["seen_setpoint_c"])
            if isinstance(payload.get("seen_setpoint_c"), (int, float))
            and (not isinstance(payload.get("seen_setpoint_c"), bool))
            else None
        ),
        bool(payload.get("seen_known")),
    )


def public_status(
    licensed: bool, feature_enabled: bool, body: dict[str, Any] | None
) -> dict[str, Any]:
    """Navigation payload. Room data stays hidden while the feature is closed."""
    if not licensed or not feature_enabled or body is None:
        return {"licensed": bool(licensed), "feature_enabled": bool(feature_enabled)}
    return {"licensed": True, "feature_enabled": True, **body}


def state_change_matters(
    entity_id: str, old_state: Any, new_state: Any, climate_ids: set[str]
) -> bool:
    """Ignore live temperature chatter. Presence, window, setpoint and availability count."""
    if entity_id in climate_ids:
        if _state_text(old_state) != _state_text(new_state):
            return True
        old_attributes = getattr(old_state, "attributes", {}) or {}
        new_attributes = getattr(new_state, "attributes", {}) or {}
        return any(
            (
                old_attributes.get(attribute_name) != new_attributes.get(attribute_name)
                for attribute_name in _CLIMATE_WATCH
            )
        )
    return _state_text(old_state) != _state_text(new_state)


def _state_text(state: Any) -> str | None:
    if state is None:
        return
    state_value = getattr(state, "state", None)
    return None if state_value is None else str(state_value)


def validate_room_update(payload: dict[str, Any], room: RoomConfig) -> RoomConfig:
    merged_payload = {
        "name": room.name,
        "climate_entities": list(room.climate_entities),
        "temp_sensor": room.temp_sensor,
        "window_sensor": room.window_sensor,
        "comfort_temp_c": room.comfort_temp_c,
        "setback_temp_c": room.setback_temp_c,
        "mode": room.mode,
        "presence_persons": list(room.presence_persons),
        "window_detect": room.window_detect,
        "lift_enabled": room.lift_enabled,
        "schedule": [
            {
                "days": list(schedule_window.days),
                "start": schedule_window.start,
                "end": schedule_window.end,
                "temp_c": schedule_window.temp_c,
            }
            for schedule_window in room.schedule
        ],
        "auto_hvac": room.auto_hvac,
        **payload,
    }
    updated_room = validate_room(merged_payload, set())
    return replace(updated_room, id=room.id, active=room.active)


def document_from_dict(
    payload: dict[str, Any] | None,
) -> tuple[
    HeatingSettings,
    list[RoomConfig],
    dict[str, dict[str, ThermoMemory]],
    datetime | None,
    str | None,
    tuple[tuple[datetime, float], ...],
    bool | None,
    dict[
        str,
        tuple[
            tuple[datetime, float | None, float | None, float | None, bool | None], ...
        ],
    ],
    dict[str, RoomModel],
    dict[str, OpenPhase],
    dict[str, dict[str, float]],
    dict[str, tuple[float, datetime]],
    frozenset[str],
    datetime | None,
    datetime | None,
    dict[str, GroupPresence],
]:
    if not isinstance(payload, dict):
        return (
            HeatingSettings(),
            [],
            {},
            None,
            None,
            (),
            None,
            {},
            {},
            {},
            {},
            {},
            frozenset(),
            None,
            None,
            {},
        )
    settings_payload = (
        payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    )
    try:
        heating_settings = validate_settings(settings_payload, HeatingSettings())
    except HeatingError:
        heating_settings = HeatingSettings()
    rooms = []
    for row in payload.get("rooms") or []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        try:
            pass
        except HeatingError:
            continue
        try:
            room_config = validate_room(
                {
                    "name": row.get("name") or row["id"],
                    "climate_entities": row.get("climate_entities") or [],
                    "temp_sensor": row.get("temp_sensor"),
                    "window_sensor": row.get("window_sensor"),
                    "comfort_temp_c": row.get("comfort_temp_c", DEFAULT_BASE_C),
                    "setback_temp_c": row.get("setback_temp_c"),
                },
                {existing_room.id for existing_room in rooms},
            )
        except HeatingError:
            continue
        presence_enabled, schedule_enabled = _stored_flags(row)
        try:
            schedule_windows = _schedule(row.get("schedule") or [])
        except HeatingError:
            schedule_windows = ()
        auto_hvac = row.get("auto_hvac") is not False
        rooms.append(
            replace(
                room_config,
                id=row["id"],
                active=bool(row.get("active")),
                presence_enabled=presence_enabled,
                schedule_enabled=schedule_enabled,
                presence_persons=_stored_persons(row),
                window_detect=row.get("window_detect") is True,
                lift_enabled=row.get("lift_enabled") is True,
                schedule=schedule_windows,
                auto_hvac=auto_hvac,
            )
        )
        continue
    runtime_memory = {}
    runtime_payload = payload.get("runtime")
    if isinstance(runtime_payload, dict):
        for room_key, room_runtime in runtime_payload.items():
            if isinstance(room_runtime, dict):
                runtime_memory[str(room_key)] = {
                    entity_id: memory_from_dict(value)
                    for entity_id, value in room_runtime.items()
                    if isinstance(value, dict)
                }
    raw_away_since = payload.get("away_since")
    restored_away_since = None
    if isinstance(raw_away_since, str):
        restored_away_since = datetime.fromisoformat(raw_away_since)
        if restored_away_since.tzinfo is None:
            restored_away_since = restored_away_since.replace(tzinfo=timezone.utc)
    presence_value = payload.get("presence")
    samples = []
    for row in payload.get("outdoor_samples") or []:
        if isinstance(row, dict):
            local_moment = _parse_stamp(row.get("at"))
            value = row.get("c")
            if (
                local_moment is not None
                and isinstance(value, (int, float))
                and (not isinstance(value, bool))
            ):
                samples.append((local_moment, float(value)))
    season_value = payload.get("heating_season")
    restored_presence = presence_value if isinstance(presence_value, str) else None
    presence_groups = _groups_from_dict(
        payload, heating_settings, restored_away_since, restored_presence
    )
    default_group = presence_groups[
        presence_group_key(heating_settings.presence_entities)
    ]
    boosts, boost_release = _boosts_from_dict(payload, _utcnow())
    pv_since, cheap_since = _lift_since_from_dict(payload.get("lift_since"))
    return (
        heating_settings,
        rooms,
        runtime_memory,
        default_group.away_since,
        default_group.presence,
        tuple(samples),
        season_value if isinstance(season_value, bool) else None,
        _room_samples_from_dict(payload.get("room_samples")),
        _models_from_dict(payload.get("room_models")),
        _phases_from_dict(payload.get("open_phases")),
        _offsets_from_dict(payload.get("thermostat_offsets")),
        boosts,
        boost_release,
        pv_since,
        cheap_since,
        presence_groups,
    )


def _boosts_from_dict(
    payload: dict[str, Any], now: datetime
) -> tuple[dict[str, tuple[float, datetime]], frozenset[str]]:
    boosts = {}
    boost_release = set()
    boosts_payload = payload.get("boosts")
    if isinstance(boosts_payload, dict):
        for room_key, boost_payload in boosts_payload.items():
            if isinstance(boost_payload, dict):
                until_at = _parse_stamp(boost_payload.get("until"))
                boost_temp_c = boost_payload.get("temp_c")
                if until_at is None or not _is_number(boost_temp_c):
                    continue
                if until_at <= now:
                    boost_release.add(str(room_key))
                    continue
                boosts[str(room_key)] = (float(boost_temp_c), until_at)
    release_payload = payload.get("boost_release")
    if isinstance(release_payload, list):
        boost_release.update(
            (
                str(released_room_id)
                for released_room_id in release_payload
                if isinstance(released_room_id, str)
            )
        )
    return (boosts, frozenset(boost_release))


def _lift_since_from_dict(payload: Any) -> tuple[datetime | None, datetime | None]:
    if not isinstance(payload, dict):
        return (None, None)
    return (_parse_stamp(payload.get("pv")), _parse_stamp(payload.get("cheap")))


def _stored_flags(item: dict[str, Any]) -> tuple[bool, bool]:
    mode = item.get("mode") if item.get("mode") in _MODES else "auto"
    presence_enabled, schedule_enabled = _flags_from_mode(mode)
    if "presence_enabled" in item or "schedule_enabled" in item:
        if "presence_enabled" in item:
            presence_enabled = item.get("presence_enabled") is True
        if "schedule_enabled" in item:
            schedule_enabled = item.get("schedule_enabled") is True
    return (presence_enabled, schedule_enabled)


def _stored_persons(item: dict[str, Any]) -> tuple[str, ...]:
    presence_persons = item.get("presence_persons")
    if not presence_persons:
        return ()
    try:
        return _entity_list(presence_persons, "person", "invalid_person")
    except HeatingError:
        return ()


def _groups_from_dict(
    payload: dict[str, Any],
    settings: HeatingSettings,
    away_since: datetime | None,
    presence: str | None,
) -> dict[str, GroupPresence]:
    presence_groups = {}
    groups_payload = payload.get("presence_groups")
    if isinstance(groups_payload, dict):
        for group_key, group_payload in groups_payload.items():
            if not (isinstance(group_key, str) and isinstance(group_payload, dict)):
                continue
            stored_presence = group_payload.get("presence")
            presence_groups[group_key] = GroupPresence(
                stored_presence if isinstance(stored_presence, str) else None,
                _parse_stamp(group_payload.get("away_since")),
            )
    default_group_key = presence_group_key(settings.presence_entities)
    if default_group_key not in presence_groups:
        presence_groups[default_group_key] = GroupPresence(presence, away_since)
    return presence_groups


def _forecast_zone(hass: Any):
    """Home Assistant local zone. Forecast hours in SFML are stored in that zone."""
    from homeassistant.util import dt as datetime_util

    hass_config = getattr(hass, "config", None)
    time_zone = getattr(hass_config, "time_zone", None)
    zone = datetime_util.get_time_zone(time_zone) if time_zone else None
    return zone or datetime_util.DEFAULT_TIME_ZONE


def _forecast_local_span(now: datetime, zone) -> tuple[date, date]:
    start_date = now.astimezone(zone).date()
    return (start_date, start_date + timedelta(days=2))


def _local_hour_to_utc(day: date, hour: int, zone) -> datetime | None:
    """Civil hour in the HA zone, as UTC. The missing spring hour is dropped; the repeated autumn hour uses the earlier fold."""
    if not isinstance(day, date) or hour < 0 or hour > 23:
        return None
    naive_local = datetime(day.year, day.month, day.day, hour)
    earliest_utc = None
    for fold in (0, 1):
        local_candidate = naive_local.replace(tzinfo=zone, fold=fold)
        roundtrip_local = local_candidate.astimezone(timezone.utc).astimezone(zone)
        if (
            roundtrip_local.date() == day
            and roundtrip_local.hour == hour
            and (roundtrip_local.fold == fold)
        ):
            candidate_utc = local_candidate.astimezone(timezone.utc)
            if earliest_utc is None or candidate_utc < earliest_utc:
                earliest_utc = candidate_utc
    return earliest_utc


class HeatingManager:

    def __init__(self, hass: Any, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = HeatingSettings()
        self.rooms = []
        self.runtime = {}
        self.away_since = None
        self.presence = "home"
        self.presence_groups = {}
        self.boosts = {}
        self.ease_decrease = set()
        self.pv_since = None
        self.cheap_since = None
        self.lift_active = None
        self.detected_until = {}
        self._window_trace = {}
        self.outdoor_samples = ()
        self.room_samples = {}
        self.room_models = {}
        self.open_phases = {}
        self.offsets = {}
        self._offset_samples = {}
        self._forecast_points = ()
        self._forecast_at = None
        self._forecast_mean = None
        self._forecast_min_12h = None
        self.heating_season = None
        self._outdoor_current = None
        self._outdoor_mean = None
        self._outdoor_reason = None
        self.decisions = {}
        self._armed = False
        self._started_at = None
        self._lock = asyncio.Lock()
        self._release_rooms = set()
        self._store = Store(hass, STORE_VERSION, f"sfml_stats.heating.{entry.entry_id}")
        self._cancel_interval = None
        self._cancel_listen = None
        self._sensor_add = None
        self._switch_add = None
        self.sensors = {}
        self.switches = {}

    async def async_setup(self) -> None:
        stored_document = await self._store.async_load()
        (
            self.settings,
            self.rooms,
            self.runtime,
            self.away_since,
            self.presence,
            self.outdoor_samples,
            self.heating_season,
            self.room_samples,
            self.room_models,
            self.open_phases,
            self.offsets,
            self.boosts,
            restored_release_rooms,
            self.pv_since,
            self.cheap_since,
            self.presence_groups,
        ) = document_from_dict(
            stored_document if isinstance(stored_document, dict) else None
        )
        self.ease_decrease = set(restored_release_rooms)
        self._started_at = _utcnow()
        self._persisted = self._document()
        self._rebuild_entities()
        self._cancel_interval = async_track_time_interval(
            self.hass, self._scheduled_cycle, timedelta(seconds=60)
        )
        self._listen()

    async def async_shutdown(self) -> None:
        if self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None
        if self._cancel_listen is not None:
            self._cancel_listen()
            self._cancel_listen = None
        await self._save()

    def bind_platforms(self, *, sensors: Any = None, switches: Any = None) -> None:
        if sensors is not None:
            self._sensor_add = sensors
        if switches is not None:
            self._switch_add = switches
        return None if True else None

    def sensor_entities(self) -> list[Any]:
        return list(self.sensors.values())

    def switch_entities(self) -> list[Any]:
        return list(self.switches.values())

    async def async_status(self) -> dict[str, Any]:
        return {
            "settings": settings_to_dict(self.settings),
            "presence": self.presence,
            "outdoor": self._outdoor_status(),
            "persons": self._person_status(),
            "lift": self._lift_status(),
            "rooms": [self._room_status(room_config) for room_config in self.rooms],
        }

    async def async_update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.settings = validate_settings(payload, self.settings)
        await self._save()
        await self._cycle(_utcnow(), allow_writes=self._armed)
        return await self.async_status()

    async def async_add_room(self, payload: dict[str, Any]) -> dict[str, Any]:
        room_config = validate_room(
            payload, {existing_room.id for existing_room in self.rooms}
        )
        self.rooms.append(room_config)
        self._ensure_entities(room_config)
        await self._save()
        self._listen()
        return room_to_dict(room_config)

    async def async_delete_room(self, room_id: str) -> None:
        room_config = self._room(room_id)
        self.rooms = [
            existing_room for existing_room in self.rooms if existing_room.id != room_id
        ]
        self.runtime.pop(room_id, None)
        self.decisions.pop(room_id, None)
        self.room_samples.pop(room_id, None)
        self.boosts.pop(room_id, None)
        self.ease_decrease.discard(room_id)
        self.detected_until.pop(room_id, None)
        self._window_trace.pop(room_id, None)
        await self._remove_entities(room_config)
        await self._save()
        self._listen()

    async def async_set_active(self, room_id: str, active: bool) -> dict[str, Any]:
        room_config = self._room(room_id)
        updated_room = replace(room_config, active=bool(active))
        self.rooms = [
            updated_room if existing_room.id == room_id else existing_room
            for existing_room in self.rooms
        ]
        self._release_rooms.add(room_id)
        await self._save()
        await self._cycle(_utcnow(), allow_writes=self._armed)
        self._publish()
        return room_to_dict(updated_room)

    async def _scheduled_cycle(self, now: datetime) -> None:
        if not self._armed:
            self._armed = True
            await self._cycle(now, allow_writes=False)
            return
        await self._cycle(now, allow_writes=True)

    async def async_update_room(
        self, room_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        room_config = self._room(room_id)
        updated_room = validate_room_update(payload, room_config)
        if (
            updated_room.comfort_temp_c != room_config.comfort_temp_c
            or updated_room.setback_temp_c != room_config.setback_temp_c
        ):
            self._release_rooms.add(room_id)
        climate_ids = set(updated_room.climate_entities)
        room_runtime = self.runtime.get(room_id, {})
        self.runtime[room_id] = {
            entity_id: entity_memory
            for entity_id, entity_memory in room_runtime.items()
            if entity_id in climate_ids
        }
        self.rooms = [
            updated_room if existing_room.id == room_id else existing_room
            for existing_room in self.rooms
        ]
        await self._save()
        self._listen()
        await self._cycle(_utcnow(), allow_writes=self._armed)
        return room_to_dict(updated_room)

    async def async_set_boost(
        self, room_id: str, temp_c: float, minutes: int
    ) -> dict[str, Any]:
        room_config = self._room(room_id)
        now = _utcnow()
        until_at = now + timedelta(minutes=int(minutes))
        self.boosts[room_id] = (float(temp_c), until_at)
        self.ease_decrease.discard(room_id)
        self.runtime[room_id] = {
            entity_id: replace(entity_memory, override_until_presence_change=False)
            for entity_id, entity_memory in self.runtime.get(room_id, {}).items()
        }
        self._release_rooms.add(room_id)
        await self._save()
        await self._cycle(now, allow_writes=self._armed)
        return self._room_status(room_config)

    async def async_clear_boost(self, room_id: str) -> dict[str, Any]:
        room_config = self._room(room_id)
        if room_id in self.boosts:
            self.boosts.pop(room_id, None)
            self.ease_decrease.add(room_id)
        await self._save()
        await self._cycle(_utcnow(), allow_writes=self._armed)
        return self._room_status(room_config)

    async def async_resume(self, room_id: str) -> dict[str, Any]:
        room_config = self._room(room_id)
        self._release_rooms.add(room_id)
        await self._cycle(_utcnow(), allow_writes=self._armed)
        return room_to_dict(room_config)

    async def _on_state(self, event: Any) -> None:
        if not self._armed:
            return
        event_data = getattr(event, "data", {}) or {}
        climate_entities = {
            entity_id
            for room_config in self.rooms
            for entity_id in room_config.climate_entities
        }
        if not state_change_matters(
            str(event_data.get("entity_id") or ""),
            event_data.get("old_state"),
            event_data.get("new_state"),
            climate_entities,
        ):
            return
        await self._cycle(_utcnow(), allow_writes=True)

    async def _cycle(self, now: datetime, *, allow_writes: bool) -> None:
        async with self._lock:
            await self._run_cycle(now, allow_writes=allow_writes)
        return None if True else None

    async def _run_cycle(self, now: datetime, *, allow_writes: bool) -> None:
        released_rooms = set(self._release_rooms)
        self._release_rooms.clear()
        await self._refresh_forecast(now)
        self._observe_outdoor(now)
        license_live, feature_enabled = self._gates()
        self._expire_boosts(now)
        self.lift_active = self._refresh_lift(now)
        presence_entity_ids = list(self.settings.presence_entities)
        for room_config in self.rooms:
            for entity_id in room_config.presence_persons:
                if entity_id not in presence_entity_ids:
                    presence_entity_ids.append(entity_id)
        presence_states = {
            entity_id: self._state_value(entity_id) for entity_id in presence_entity_ids
        }
        previous_groups = {}
        next_groups = dict(self.presence_groups)
        for room_config in self.rooms:
            readings = {
                entity_id: self._reading(entity_id)
                for entity_id in room_config.climate_entities
            }
            thermostat_offsets = {
                entity_id: offset_k
                for entity_id in room_config.climate_entities
                if (offset_k := self._track_offset(room_config, entity_id, now))
                is not None
            }
            room_persons = effective_presence_entities(self.settings, room_config)
            group_key = presence_group_key(room_persons)
            if group_key not in previous_groups:
                previous_groups[group_key] = self._group_before(group_key)
            previous_group = previous_groups[group_key]
            actual_c = self._room_actual(room_config)
            detected_open, detected_until = self._observe_window(
                room_config, readings, actual_c, now
            )
            boost = self.boosts.get(room_config.id)
            ease_decrease = room_config.id in self.ease_decrease
            room_decision = decide_room(
                settings=self.settings,
                room=room_config,
                now=now,
                presence_states=presence_states,
                away_since=previous_group.away_since,
                previous_presence=previous_group.presence,
                window_on=self._window_on(room_config),
                readings=readings,
                memory=self.runtime.get(room_config.id, {}),
                license_live=license_live,
                feature_enabled=feature_enabled,
                allow_writes=allow_writes,
                release_override=room_config.id in released_rooms,
                heating_season=(
                    self.heating_season if self.settings.outdoor_entity else None
                ),
                started_at=self._started_at,
                room_model=self.room_models.get(room_config.id),
                room_actual=actual_c,
                outdoor_c=self._forecast_at_time(
                    _next_window_start(room_config, _as_local(now), now)
                )
                or self._outdoor_current,
                offsets=thermostat_offsets,
                boost_c=None if boost is None else boost[0],
                boost_until=None if boost is None else boost[1],
                detected_open=detected_open,
                detected_until=detected_until,
                ease_decrease=ease_decrease,
                lift=self.lift_active,
            )
            next_groups[group_key] = GroupPresence(
                room_decision.presence, room_decision.away_since
            )
            self.decisions[room_config.id] = room_decision
            room_runtime = dict(self.runtime.get(room_config.id, {}))
            written_decisions = []
            for thermostat_decision in room_decision.thermostats:
                if (
                    thermostat_decision.write
                    and thermostat_decision.hvac_target == "heat"
                ):
                    if thermostat_decision.target_c is not None:
                        try:
                            await self._write_temperature(
                                thermostat_decision.entity_id,
                                thermostat_decision.target_c,
                                hvac_mode="heat",
                            )
                        except Exception as error:
                            _LOGGER.warning(
                                "Heating heat write failed for %s: %s",
                                thermostat_decision.entity_id,
                                error,
                            )
                            thermostat_memory = room_runtime.get(
                                thermostat_decision.entity_id, ThermoMemory()
                            )
                            failed_memory = replace(
                                thermostat_memory, last_failed_at=now
                            )
                            room_runtime[thermostat_decision.entity_id] = failed_memory
                            written_decisions.append(
                                replace(
                                    thermostat_decision,
                                    write=False,
                                    hvac_write=False,
                                    reason="write_failed",
                                    memory=failed_memory,
                                )
                            )
                            continue
                        room_runtime[thermostat_decision.entity_id] = (
                            thermostat_decision.memory
                        )
                        written_decisions.append(thermostat_decision)
                        continue
                if (
                    thermostat_decision.hvac_write
                    and thermostat_decision.hvac_target == "off"
                ):
                    try:
                        await self._write_hvac_mode(
                            thermostat_decision.entity_id, "off"
                        )
                    except Exception as error:
                        _LOGGER.warning(
                            "Heating mode write failed for %s: %s",
                            thermostat_decision.entity_id,
                            error,
                        )
                        thermostat_memory = room_runtime.get(
                            thermostat_decision.entity_id, ThermoMemory()
                        )
                        failed_memory = replace(thermostat_memory, last_failed_at=now)
                        room_runtime[thermostat_decision.entity_id] = failed_memory
                        written_decisions.append(
                            replace(
                                thermostat_decision,
                                hvac_write=False,
                                reason="write_failed",
                                memory=failed_memory,
                            )
                        )
                        continue
                if thermostat_decision.write:
                    if thermostat_decision.target_c is not None:
                        try:
                            await self._write_temperature(
                                thermostat_decision.entity_id,
                                thermostat_decision.target_c,
                            )
                        except Exception as error:
                            _LOGGER.warning(
                                "Heating write failed for %s: %s",
                                thermostat_decision.entity_id,
                                error,
                            )
                            thermostat_memory = room_runtime.get(
                                thermostat_decision.entity_id, ThermoMemory()
                            )
                            failed_memory = replace(
                                thermostat_memory, last_failed_at=now
                            )
                            room_runtime[thermostat_decision.entity_id] = failed_memory
                            written_decisions.append(
                                replace(
                                    thermostat_decision,
                                    write=False,
                                    reason="write_failed",
                                    memory=failed_memory,
                                )
                            )
                            continue
                room_runtime[thermostat_decision.entity_id] = thermostat_decision.memory
                written_decisions.append(thermostat_decision)
                continue
            self.decisions[room_config.id] = replace(
                room_decision, thermostats=tuple(written_decisions)
            )
            if (
                ease_decrease
                and allow_writes
                and boost_return_settled(tuple(written_decisions), readings)
            ):
                self.ease_decrease.discard(room_config.id)
            self.runtime[room_config.id] = room_runtime
            continue
        self.presence_groups = next_groups
        default_group_key = presence_group_key(self.settings.presence_entities)
        if default_group_key in next_groups:
            self.presence = next_groups[default_group_key].presence
            self.away_since = next_groups[default_group_key].away_since
        self._record_room_samples(now)
        await self._save()
        self._publish()

    async def _write_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        if hvac_mode != "off":
            raise HeatingError(
                "invalid_mode", "Only off is written with set_hvac_mode."
            )
        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": hvac_mode},
                blocking=True,
            )
        return None if True else None

    async def _write_temperature(
        self, entity_id: str, temperature: float, hvac_mode: str | None = None
    ) -> None:
        service_data = {"entity_id": entity_id, "temperature": round(temperature, 1)}
        if hvac_mode == "heat":
            service_data["hvac_mode"] = "heat"
        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            await self.hass.services.async_call(
                "climate", "set_temperature", service_data, blocking=True
            )
        return None if True else None

    def _record_room_samples(self, now: datetime) -> None:
        """Append one room sample per 15 minutes. A quiet minute does not touch the store."""
        for room_config in self.rooms:
            room_decision = self.decisions.get(room_config.id)
            actual_c = self._room_actual(room_config)
            target_c = room_decision.target_c if room_decision is not None else None
            if actual_c is None and target_c is None:
                continue
            previous_samples = self.room_samples.get(room_config.id, ())
            if previous_samples and now - previous_samples[-1][0] < SAMPLE_INTERVAL:
                continue
            heating = self._room_demand(room_config)
            outdoor_c = self._outdoor_current
            self.room_samples[room_config.id] = _trim_room_samples(
                (*previous_samples, (now, actual_c, target_c, outdoor_c, heating)), now
            )
            self._advance_model(
                room_config, now, actual_c, target_c, outdoor_c, heating
            )
            continue

    def _room_actual(self, room: RoomConfig) -> float | None:
        measured_room_c = self._measured(room.temp_sensor)
        if measured_room_c is not None:
            return measured_room_c
        thermostat_temperatures_c = [
            self._reading(entity_id).current_c for entity_id in room.climate_entities
        ]
        available_temperatures_c = [
            temperature_c
            for temperature_c in thermostat_temperatures_c
            if temperature_c is not None
        ]
        if not available_temperatures_c:
            return
        return sum(available_temperatures_c) / len(available_temperatures_c)

    def _entity_demand(self, entity_id: str) -> bool:
        thermostat_reading = self._reading(entity_id)
        valve_pct = _device_siblings(self.hass, entity_id).get("valve_pct")
        return thermostat_demand(
            thermostat_reading.hvac_action,
            valve_pct,
            thermostat_reading.hvac_mode,
            thermostat_reading.setpoint_c,
            thermostat_reading.current_c,
        )

    def _room_demand(self, room: RoomConfig) -> bool:
        return any(
            (self._entity_demand(entity_id) for entity_id in room.climate_entities)
        )

    def _track_offset(
        self, room: RoomConfig, entity_id: str, now: datetime
    ) -> float | None:
        """Adopted room-minus-TRV offset. Better Thermostat and rooms without a sensor stay raw."""
        if not room.temp_sensor or self._is_better_thermostat(entity_id):
            return None
        adopted_offset_k = self.offsets.get(room.id, {}).get(entity_id)
        measured_room_c = self._measured(room.temp_sensor)
        thermostat_current_c = self._reading(entity_id).current_c
        if measured_room_c is None or thermostat_current_c is None:
            return adopted_offset_k
        offset_key = (room.id, entity_id)
        offset_samples = tuple(
            (
                sample
                for sample in self._offset_samples.get(offset_key, ())
                if now - sample[0] <= OFFSET_WINDOW
            )
        )
        if not offset_samples or now - offset_samples[-1][0] >= OFFSET_SAMPLE_GAP:
            offset_samples = (
                *offset_samples,
                (now, measured_room_c - thermostat_current_c),
            )[-60:]
            self._offset_samples[offset_key] = offset_samples
        next_offset_k = adopt_offset(
            adopted_offset_k,
            propose_offset(tuple((offset_k for sample_at, offset_k in offset_samples))),
        )
        if next_offset_k is not None:
            self.offsets.setdefault(room.id, {})[entity_id] = next_offset_k
        return next_offset_k

    def _is_better_thermostat(self, entity_id: str) -> bool:
        try:
            from homeassistant.helpers import entity_registry as entity_registry_api

            registry = entity_registry_api.async_get(self.hass)
            registry_entry = registry.async_get(entity_id)
        except Exception:
            return True
        platform = (
            getattr(registry_entry, "platform", None)
            if registry_entry is not None
            else None
        )
        if not platform:
            return True
        return str(platform) == "better_thermostat"

    def _offset_snapshot(self, room: RoomConfig) -> tuple[tuple[str, float], ...]:
        thermostat_offsets = self.offsets.get(room.id, {})
        if not room.temp_sensor:
            return ()
        return tuple(
            (
                (entity_id, float(thermostat_offsets[entity_id]))
                for entity_id in room.climate_entities
                if entity_id in thermostat_offsets
                and (not self._is_better_thermostat(entity_id))
            )
        )

    def _offset_changed(self, room: RoomConfig, phase: OpenPhase) -> bool:
        if phase.kind == "heat":
            return False
        current_offsets = dict(self._offset_snapshot(room))
        previous_offsets = dict(phase.offsets)
        offset_entity_ids = set(current_offsets) | set(previous_offsets)
        for entity_id in offset_entity_ids:
            previous_offset_k = previous_offsets.get(entity_id)
            current_offset_k = current_offsets.get(entity_id)
            if previous_offset_k is not None and current_offset_k is not None:
                if _close(previous_offset_k, current_offset_k):
                    continue
            return True
        return False

    def _phase_blocked(self, room: RoomConfig) -> bool:
        if self._window_on(room):
            return True
        room_decision = self.decisions.get(room.id)
        if room_decision is not None and room_decision.reason == "manual_override":
            return True
        for entity_id in room.climate_entities:
            thermostat_reading = self._reading(entity_id)
            if (thermostat_reading.hvac_mode or "").lower() == "off":
                return True
            entity_memory = self.runtime.get(room.id, {}).get(entity_id)
            if not entity_memory is not None:
                continue
            if not entity_memory.awaiting_since is not None:
                continue
            return True
        return False

    def _advance_model(
        self,
        room: RoomConfig,
        now: datetime,
        actual: float | None,
        target: float | None,
        outdoor: float | None,
        heating: bool,
    ) -> None:
        if self._phase_blocked(room) or actual is None:
            self.open_phases.pop(room.id, None)
            return
        current_phase_kind = phase_kind(heating, target, actual, outdoor)
        open_phase = self.open_phases.get(room.id)
        room_model = self.room_models.get(room.id, RoomModel())
        if open_phase is not None and self._offset_changed(room, open_phase):
            self.open_phases.pop(room.id, None)
            open_phase = None
        if open_phase is not None and current_phase_kind != open_phase.kind:
            self.room_models[room.id] = close_phase(
                room_model, open_phase, actual, outdoor, now
            )
            open_phase = None
        if current_phase_kind is None:
            self.open_phases.pop(room.id, None)
            return
        if open_phase is None:
            self.open_phases[room.id] = OpenPhase(
                current_phase_kind, now, actual, outdoor, self._offset_snapshot(room)
            )
        return None if True else None

    async def _refresh_forecast(self, now: datetime) -> None:
        if self._forecast_at is not None and now - self._forecast_at < timedelta(
            minutes=30
        ):
            return
        self._forecast_at = now
        try:
            from pathlib import Path as filesystem_path
            from ..readers.weather_reader import (
                WeatherDataReader as weather_data_reader,
            )

            weather_reader = weather_data_reader(
                filesystem_path(self.hass.config.path())
            )
            zone = _forecast_zone(self.hass)
            start_date, end_date = _forecast_local_span(now, zone)
            weather_records = await weather_reader.async_get_forecast_weather(
                start_date, end_date
            )
        except Exception:
            self._forecast_mean = None
            self._forecast_min_12h = None
            return
        forecast_points = []
        for weather_record in weather_records:
            if not _is_number(weather_record.temperature_c):
                continue
            forecast_at = _local_hour_to_utc(
                weather_record.date, int(weather_record.hour), zone
            )
            if forecast_at is None:
                continue
            forecast_points.append((forecast_at, float(weather_record.temperature_c)))
        self._forecast_points = tuple(forecast_points)
        self._forecast_mean, self._forecast_min_12h = forecast_summary(
            forecast_points, now
        )

    def _forecast_at_time(self, moment: datetime | None) -> float | None:
        if moment is None or not self._forecast_points:
            return None
        nearest_point = min(
            self._forecast_points, key=lambda item: abs(item[0] - moment)
        )
        if abs(nearest_point[0] - moment) > timedelta(hours=2):
            return
        return nearest_point[1]

    def _observe_outdoor(self, now: datetime) -> None:
        outdoor_entity_id = self.settings.outdoor_entity
        if not outdoor_entity_id:
            self._outdoor_reason = "outdoor_missing"
            self._outdoor_current = None
            self._outdoor_mean = None
            self.heating_season = None
            return
        outdoor_current_c = self._outdoor_c(outdoor_entity_id)
        if outdoor_current_c is None:
            self._outdoor_reason = "outdoor_unavailable"
            self._outdoor_current = None
            self._outdoor_mean = (
                _sample_mean(self.outdoor_samples, now, 0.0)
                if self.outdoor_samples
                else None
            )
            if not self.outdoor_samples:
                self._outdoor_mean = None
            return
        outdoor_samples = _trim_samples(self.outdoor_samples, now)
        if not outdoor_samples or now - outdoor_samples[-1][0] >= SAMPLE_INTERVAL:
            outdoor_samples = _trim_samples(
                (*outdoor_samples, (now, outdoor_current_c)), now
            )
        self.outdoor_samples = outdoor_samples
        outdoor_mean_c = _sample_mean(outdoor_samples, now, outdoor_current_c)
        self._outdoor_current = outdoor_current_c
        self._outdoor_mean = outdoor_mean_c
        self.heating_season, self._outdoor_reason = season_with_forecast(
            outdoor_mean_c,
            self._forecast_mean,
            self.settings.heating_limit_c,
            self.heating_season,
        )

    def _outdoor_c(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or str(state.state).lower() in {"unknown", "unavailable"}:
            return None
        attributes = state.attributes or {}
        if entity_id.startswith("weather."):
            temperature_value = attributes.get("temperature")
        else:
            unit = attributes.get("unit_of_measurement")
            if unit not in {"C", "°C"}:
                return
            temperature_value = state.state
        if not _is_number(temperature_value):
            try:
                temperature_value = float(temperature_value)
            except (TypeError, ValueError):
                return
        return float(temperature_value)

    def _outdoor_status(self) -> dict[str, Any]:
        return (
            (
                (
                    {
                        "entity_id": self.settings.outdoor_entity,
                        "current_c": self._outdoor_current,
                        "mean_c": self._outdoor_mean,
                        "season": self.heating_season,
                        "limit_c": self.settings.heating_limit_c,
                        "reason": self._outdoor_reason,
                        "forecast_mean_24h": self._forecast_mean,
                        "forecast_min_next_12h": self._forecast_min_12h,
                        "forecast_source": (
                            "weather_forecast"
                            if self._forecast_mean is not None
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

    def _person_status(self) -> list[dict[str, Any]]:
        persons = []
        for entity_id in self.settings.presence_entities:
            persons.append(
                {
                    "entity_id": entity_id,
                    "name": self._friendly(entity_id),
                    "state": self._state_value(entity_id),
                }
            )
        return persons

    def _friendly(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None:
            return entity_id
        name = (state.attributes or {}).get("friendly_name")
        return str(name or entity_id)

    def _gates(self) -> tuple[bool, bool]:
        from ..api.premium_dashboard import (
            _eai_config as eai_config,
            _eai_feature_enabled as eai_feature_enabled,
        )
        from ..api.statistics_views import (
            license_is_live as license_is_live,
            license_snapshot as license_snapshot,
        )

        license_state = license_snapshot(self.hass)
        return (
            license_is_live(license_state),
            eai_feature_enabled(eai_config(self.hass), "heating_enabled"),
        )

    def _reading(self, entity_id: str) -> ThermoReading:
        state = self.hass.states.get(entity_id)
        if state is None or str(state.state).lower() in {"unknown", "unavailable"}:
            return (
                (
                    (ThermoReading(False, None, None, False) if True else None)
                    if True
                    else None
                )
                if True
                else None
            )
        attributes = state.attributes or {}
        target_low_c = attributes.get("target_temp_low")
        target_high_c = attributes.get("target_temp_high")
        setpoint_c = attributes.get("temperature")
        uses_range = (
            _is_number(target_low_c)
            and _is_number(target_high_c)
            and (not _is_number(setpoint_c))
        )
        step_c = attributes.get("target_temp_step")
        hvac_modes = attributes.get("hvac_modes") or ()
        current_c = attributes.get("current_temperature")
        return (
            (
                (
                    ThermoReading(
                        True,
                        attributes.get("hvac_mode") or state.state,
                        float(setpoint_c) if _is_number(setpoint_c) else None,
                        uses_range,
                        (
                            float(step_c)
                            if _is_number(step_c) and float(step_c) > 0
                            else 0.5
                        ),
                        (
                            float(attributes["min_temp"])
                            if _is_number(attributes.get("min_temp"))
                            else None
                        ),
                        (
                            float(attributes["max_temp"])
                            if _is_number(attributes.get("max_temp"))
                            else None
                        ),
                        (
                            tuple((str(hvac_mode) for hvac_mode in hvac_modes))
                            if isinstance(hvac_modes, (list, tuple))
                            else ()
                        ),
                        float(current_c) if _is_number(current_c) else None,
                        getattr(state, "last_updated", None),
                        (
                            attributes.get("hvac_action")
                            if isinstance(attributes.get("hvac_action"), str)
                            else None
                        ),
                    )
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    def _window_on(self, room: RoomConfig) -> bool:
        if not room.window_sensor:
            return False
        return self._state_value(room.window_sensor) == "on"

    def _state_value(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        return str(state.state)

    def _expire_boosts(self, now: datetime) -> None:
        for room_key, (unused_boost_c, until_at) in list(self.boosts.items()):
            if until_at <= now:
                self.boosts.pop(room_key, None)
                self.ease_decrease.add(room_key)

    def _refresh_lift(self, now: datetime) -> str | None:
        pv_signal = self.settings.lift_on_pv and pv_surplus_on(self.hass)
        cheap_signal = self.settings.lift_on_cheap and cheap_price_on(self.hass, now)
        self.pv_since, pv_ready = advance_lift_signal(self.pv_since, pv_signal, now)
        self.cheap_since, cheap_ready = advance_lift_signal(
            self.cheap_since, cheap_signal, now
        )
        return choose_lift(pv_ready, cheap_ready)

    def _observe_window(
        self,
        room: RoomConfig,
        readings: dict[str, ThermoReading],
        actual: float | None,
        now: datetime,
    ) -> tuple[bool, datetime | None]:
        if not room.window_detect:
            self._window_trace.pop(room.id, None)
            self.detected_until.pop(room.id, None)
            return (False, None)
        heating = any(
            (
                (thermostat_reading.hvac_mode or "").lower() == "heat"
                for thermostat_reading in readings.values()
            )
        )
        window_trace, until_at, detection_on = advance_window_detection(
            self._window_trace.get(room.id, ()),
            self.detected_until.get(room.id),
            now,
            heating,
            actual,
        )
        self._window_trace[room.id] = window_trace
        if until_at is None:
            self.detected_until.pop(room.id, None)
        else:
            self.detected_until[room.id] = until_at
        return (detection_on, until_at) if True else None

    def _lift_status(self) -> dict[str, Any]:
        return {
            "lift_k": self.settings.lift_k,
            "lift_on_pv": self.settings.lift_on_pv,
            "lift_on_cheap": self.settings.lift_on_cheap,
            "pv_since": self.pv_since.isoformat() if self.pv_since else None,
            "cheap_since": self.cheap_since.isoformat() if self.cheap_since else None,
            "active": self.lift_active,
        }

    def _boost_fields(
        self, room_id: str, now: datetime | None = None
    ) -> tuple[float | None, str | None]:
        boost = self.boosts.get(room_id)
        if boost is None:
            return (None, None)
        boost_c, until_at = boost
        if now is not None and until_at <= now:
            return (None, None)
        return (boost_c, until_at.isoformat())

    def _group_before(self, key: str) -> GroupPresence:
        group_presence = self.presence_groups.get(key)
        if group_presence is not None:
            return group_presence
        if key == presence_group_key(self.settings.presence_entities):
            return GroupPresence(self.presence, self.away_since)
        return GroupPresence(None, None)

    def _room_group(self, room: RoomConfig) -> tuple[str, datetime | None]:
        group_key = presence_group_key(effective_presence_entities(self.settings, room))
        group_presence = self.presence_groups.get(group_key)
        if group_presence is not None:
            return (group_presence.presence or "home", group_presence.away_since)
        if group_key == presence_group_key(self.settings.presence_entities):
            return (self.presence or "home", self.away_since)
        return ("home", None)

    def _room(self, room_id: str) -> RoomConfig:
        for room_config in self.rooms:
            if not room_config.id == room_id:
                continue
            return room_config
        raise HeatingError("unknown_room", "Unknown room.")

    def _room_status(self, room: RoomConfig) -> dict[str, Any]:
        room_decision = self.decisions.get(room.id)
        thermostat_statuses = []
        for entity_id in room.climate_entities:
            thermostat_reading = self._reading(entity_id)
            thermostat_decision = None
            if room_decision is not None:
                thermostat_decision = next(
                    (
                        candidate_decision
                        for candidate_decision in room_decision.thermostats
                        if candidate_decision.entity_id == entity_id
                    ),
                    None,
                )
            sibling_readings = _device_siblings(self.hass, entity_id)
            thermostat_statuses.append(
                {
                    "entity_id": entity_id,
                    "name": self._friendly(entity_id),
                    "available": thermostat_reading.available,
                    "hvac_mode": thermostat_reading.hvac_mode,
                    "current_c": thermostat_reading.current_c,
                    "setpoint_c": thermostat_reading.setpoint_c,
                    "valve_pct": sibling_readings.get("valve_pct"),
                    "battery_pct": sibling_readings.get("battery_pct"),
                    "reason": (
                        thermostat_decision.reason if thermostat_decision else None
                    ),
                    "demand": self._entity_demand(entity_id),
                    "offset_k": (
                        thermostat_decision.offset_k if thermostat_decision else None
                    ),
                    "sent_setpoint": (
                        thermostat_decision.target_c if thermostat_decision else None
                    ),
                    "last_write": (
                        thermostat_decision.memory.last_write_at.isoformat()
                        if thermostat_decision
                        and thermostat_decision.memory.last_write_at
                        else None
                    ),
                }
            )
            continue
        temperature_siblings = (
            _device_siblings(self.hass, room.temp_sensor) if room.temp_sensor else {}
        )
        window_siblings = (
            _device_siblings(self.hass, room.window_sensor)
            if room.window_sensor
            else {}
        )
        group_presence_text, group_away_since = self._room_group(room)
        away_since = (
            room_decision.away_since if room_decision is not None else group_away_since
        )
        boost_temperature_c, boost_until = self._boost_fields(room.id)
        detected_until = self.detected_until.get(room.id)
        window_detected = detected_until is not None and detected_until > _utcnow()
        active_lift = (
            room_decision.reason
            if room_decision and room_decision.reason in {"pv_lift", "price_lift"}
            else None
        )
        return (
            (
                {
                    **room_to_dict(room),
                    "target_c": room_decision.target_c if room_decision else None,
                    "control_mode": room_decision.mode if room_decision else "observe",
                    "reason": (
                        room_decision.reason if room_decision else "room_inactive"
                    ),
                    "presence": (
                        room_decision.presence if room_decision else group_presence_text
                    ),
                    "away_since": away_since.isoformat() if away_since else None,
                    "boost_temp_c": boost_temperature_c,
                    "boost_until": boost_until,
                    "window_detected": window_detected,
                    "lift": active_lift,
                    "window_open": (
                        self._window_on(room) if room.window_sensor else None
                    ),
                    "humidity_pct": temperature_siblings.get("humidity_pct"),
                    "sensor_battery_pct": temperature_siblings.get("battery_pct"),
                    "window_battery_pct": window_siblings.get("battery_pct"),
                    "history": [
                        {
                            "at": sample[0].isoformat(),
                            "actual_c": sample[1],
                            "target_c": sample[2],
                        }
                        for sample in self.room_samples.get(room.id, ())
                    ],
                    "next_change": (
                        {
                            "at": room_decision.next_at.isoformat(),
                            "target_c": room_decision.next_target_c,
                        }
                        if room_decision and room_decision.next_at is not None
                        else None
                    ),
                    "thermostats": thermostat_statuses,
                    "temp_sensor_c": self._measured(room.temp_sensor),
                    "demand": self._room_demand(room),
                    "open_phase": phase_to_dict(self.open_phases.get(room.id)),
                    "model": _public_model(self.room_models.get(room.id, RoomModel())),
                    "preheat_for": (
                        room_decision.preheat_for.isoformat()
                        if room_decision and room_decision.preheat_for
                        else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )

    def _measured(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return

    def _document(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "settings": settings_to_dict(self.settings),
            "rooms": [room_to_dict(room_config) for room_config in self.rooms],
            "runtime": {
                room_key: {
                    entity_id: memory_to_dict(entity_memory)
                    for entity_id, entity_memory in room_runtime.items()
                }
                for room_key, room_runtime in self.runtime.items()
            },
            "away_since": self.away_since.isoformat() if self.away_since else None,
            "presence": self.presence,
            "boosts": {
                room_key: {"temp_c": boost_c, "until": until_at.isoformat()}
                for room_key, (boost_c, until_at) in self.boosts.items()
            },
            "boost_release": sorted(self.ease_decrease),
            "lift_since": {
                "pv": self.pv_since.isoformat() if self.pv_since else None,
                "cheap": self.cheap_since.isoformat() if self.cheap_since else None,
            },
            "presence_groups": {
                group_key: {
                    "presence": record.presence,
                    "away_since": (
                        record.away_since.isoformat() if record.away_since else None
                    ),
                }
                for group_key, record in self.presence_groups.items()
                if group_key != presence_group_key(self.settings.presence_entities)
            },
            "outdoor_samples": [
                {"at": sample_at.isoformat(), "c": temperature_c}
                for sample_at, temperature_c in self.outdoor_samples
            ],
            "heating_season": self.heating_season,
            "room_models": {
                room_key: model_to_dict(room_model)
                for room_key, room_model in self.room_models.items()
            },
            "open_phases": {
                room_key: phase_to_dict(open_phase)
                for room_key, open_phase in self.open_phases.items()
            },
            "thermostat_offsets": {
                room_key: dict(room_offsets)
                for room_key, room_offsets in self.offsets.items()
                if room_offsets
            },
            "room_samples": {
                room_key: [
                    {
                        "at": record[0].isoformat(),
                        "actual_c": record[1],
                        "target_c": record[2],
                        "outdoor_c": record[3] if len(record) > 3 else None,
                        "heating": record[4] if len(record) > 4 else None,
                    }
                    for record in samples
                ]
                for room_key, samples in self.room_samples.items()
            },
        }

    async def _save(self) -> None:
        document = self._document()
        if document == getattr(self, "_persisted", None):
            return
        await self._store.async_save(document)
        self._persisted = document

    def _rebuild_entities(self) -> None:
        for room_config in self.rooms:
            self._ensure_entities(room_config)

    def _ensure_entities(self, room: RoomConfig) -> None:
        target_sensor_class, active_switch_class = _entity_classes()
        if room.id not in self.sensors:
            target_sensor = target_sensor_class(self, room.id)
            self.sensors[room.id] = target_sensor
            if self._sensor_add is not None:
                self._sensor_add([target_sensor])
        if room.id not in self.switches:
            active_switch = active_switch_class(self, room.id)
            self.switches[room.id] = active_switch
            if self._switch_add is not None:
                self._switch_add([active_switch])
        return None if True else None

    async def _remove_entities(self, room: RoomConfig) -> None:
        from homeassistant.helpers import entity_registry as entity_registry_api

        for entity_collection, domain in (
            (self.sensors, "sensor"),
            (self.switches, "switch"),
        ):
            entity = entity_collection.pop(room.id, None)
            if not entity is not None:
                continue
            unique_id = entity.unique_id
            entity.async_remove()
            registry = entity_registry_api.async_get(self.hass)
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id:
                registry.async_remove(entity_id)

    def _listen(self) -> None:
        if self._cancel_listen is not None:
            self._cancel_listen()
        watched_entities = list(self.settings.presence_entities)
        if self.settings.outdoor_entity:
            watched_entities.append(self.settings.outdoor_entity)
        for room_config in self.rooms:
            watched_entities.extend(room_config.presence_persons)
            watched_entities.extend(room_config.climate_entities)
            if room_config.window_sensor:
                watched_entities.append(room_config.window_sensor)
        if not watched_entities:
            self._cancel_listen = None
            return
        self._cancel_listen = async_track_state_change_event(
            self.hass, watched_entities, self._on_state
        )

    def _publish(self) -> None:
        for entity in [*self.sensors.values(), *self.switches.values()]:
            if getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()


def _public_model(model: RoomModel) -> dict[str, Any]:
    return {
        "status": "ready" if model.ready else "learning",
        "heat_phases": model.heat_count,
        "cool_phases": model.cool_count,
        "rate_k_per_h": model.rate_k_per_h,
        "tau_h": model.tau_h,
    }


def _models_from_dict(payload: Any) -> dict[str, RoomModel]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(room_key): model_from_dict(model_payload)
        for room_key, model_payload in payload.items()
    }


def _phases_from_dict(payload: Any) -> dict[str, OpenPhase]:
    phases = {}
    if not isinstance(payload, dict):
        return phases
    for room_key, phase_payload in payload.items():
        if not isinstance(phase_payload, dict) or phase_payload.get("kind") not in {
            "cool",
            "heat",
        }:
            continue
        started_at = _parse_stamp(phase_payload.get("started"))
        phase_start_c = phase_payload.get("start_c")
        if not started_at is not None or not _is_number(phase_start_c):
            continue
        outdoor_c = phase_payload.get("outdoor_c")
        offsets_payload = (
            phase_payload.get("offsets")
            if isinstance(phase_payload.get("offsets"), dict)
            else {}
        )
        thermostat_offsets = tuple(
            (
                (str(entity_id), float(offset_k))
                for entity_id, offset_k in offsets_payload.items()
                if _is_number(offset_k)
            )
        )
        phases[str(room_key)] = OpenPhase(
            str(phase_payload["kind"]),
            started_at,
            float(phase_start_c),
            float(outdoor_c) if _is_number(outdoor_c) else None,
            thermostat_offsets,
        )
        continue
    return phases


def _offsets_from_dict(payload: Any) -> dict[str, dict[str, float]]:
    offsets_by_room = {}
    if not isinstance(payload, dict):
        return offsets_by_room
    for room_key, offsets_payload in payload.items():
        if isinstance(offsets_payload, dict):
            room_offsets = {
                str(entity_id): float(offset_k)
                for entity_id, offset_k in offsets_payload.items()
                if _is_number(offset_k)
            }
            if room_offsets:
                offsets_by_room[str(room_key)] = room_offsets
    return offsets_by_room


def _room_samples_from_dict(
    payload: Any,
) -> dict[
    str,
    tuple[tuple[datetime, float | None, float | None, float | None, bool | None], ...],
]:
    samples_by_room = {}
    if not isinstance(payload, dict):
        return samples_by_room
    for room_key, raw_samples in payload.items():
        if not isinstance(raw_samples, list):
            continue
        room_samples = []
        for sample_payload in raw_samples:
            if not isinstance(sample_payload, dict):
                continue
            sample_at = _parse_stamp(sample_payload.get("at"))
            if sample_at is None:
                continue
            actual_c = sample_payload.get("actual_c")
            target_c = sample_payload.get("target_c")
            outdoor_c = sample_payload.get("outdoor_c")
            heating = sample_payload.get("heating")
            room_samples.append(
                (
                    sample_at,
                    float(actual_c) if _is_number(actual_c) else None,
                    float(target_c) if _is_number(target_c) else None,
                    float(outdoor_c) if _is_number(outdoor_c) else None,
                    bool(heating) if isinstance(heating, bool) else None,
                )
            )
            continue
        samples_by_room[str(room_key)] = tuple(room_samples)
        continue
    return samples_by_room


def _trim_room_samples(
    samples: tuple[
        tuple[datetime, float | None, float | None, float | None, bool | None], ...
    ],
    now: datetime,
) -> tuple[tuple[datetime, float | None, float | None, float | None, bool | None], ...]:
    recent_samples = tuple(
        (sample for sample in samples if now - sample[0] <= SAMPLE_WINDOW)
    )
    return recent_samples[-96:]


def _trim_samples(
    samples: tuple[tuple[datetime, float], ...], now: datetime
) -> tuple[tuple[datetime, float], ...]:
    recent_samples = tuple(
        (sample for sample in samples if now - sample[0] <= SAMPLE_WINDOW)
    )
    return recent_samples[-96:]


def _sample_mean(
    samples: tuple[tuple[datetime, float], ...], now: datetime, current: float
) -> float:
    recent_samples = _trim_samples(samples, now)
    if not recent_samples:
        return current
    return sum((temperature_c for sample_at, temperature_c in recent_samples)) / len(
        recent_samples
    )


def _device_siblings(hass: Any, entity_id: str | None) -> dict[str, float | None]:
    """Read-only valve percent, battery and humidity from the same device."""
    sibling_values = {}
    if not entity_id or hass is None:
        return sibling_values
    try:
        from homeassistant.helpers import entity_registry as entity_registry_api

        registry = entity_registry_api.async_get(hass)
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None or not registry_entry.device_id:
            return sibling_values
        sibling_readings = entity_registry_api.async_entries_for_device(
            registry, registry_entry.device_id
        )
    except Exception:
        return sibling_values
    states = getattr(hass, "states", None)
    if states is None:
        return sibling_values
    for sibling_entry in sibling_readings:
        state = states.get(sibling_entry.entity_id)
        if state is None:
            continue
        attributes = getattr(state, "attributes", {}) or {}
        if str(getattr(state, "state", "")).lower() in {
            "",
            "none",
            "unknown",
            "unavailable",
        }:
            continue
        numeric_value = _number_state(state)
        domain = getattr(sibling_entry, "entity_id", "").split(".", 1)[0]
        unit = attributes.get("unit_of_measurement")
        device_class = (
            attributes.get("device_class")
            or getattr(sibling_entry, "device_class", None)
            or getattr(sibling_entry, "original_device_class", None)
        )
        if domain == "number" and unit == "%" and ("valve_pct" not in sibling_values):
            sibling_values["valve_pct"] = numeric_value
        if (
            "battery_pct" not in sibling_values
            and domain == "sensor"
            and (device_class == "battery")
            and (unit == "%")
            and ("batter" in sibling_entry.entity_id)
            and (numeric_value is not None)
        ):
            sibling_values["battery_pct"] = numeric_value
        if not domain == "sensor":
            continue
        if not attributes.get("device_class") == "humidity":
            continue
        if not "humidity_pct" not in sibling_values:
            continue
        sibling_values["humidity_pct"] = numeric_value
        continue
    return sibling_values


def _number_state(state: Any) -> float | None:
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and (not isinstance(value, bool))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _entity_classes() -> tuple[type, type]:
    cached_classes = getattr(_entity_classes, "cached", None)
    if cached_classes is not None:
        return cached_classes
    from homeassistant.components.sensor import (
        SensorDeviceClass as SensorDeviceClass,
        SensorEntity as sensor_entity_base,
        SensorStateClass as SensorStateClass,
    )
    from homeassistant.components.switch import SwitchEntity as switch_entity_base
    from homeassistant.const import UnitOfTemperature as UnitOfTemperature
    from homeassistant.helpers.entity import DeviceInfo as DeviceInfo

    class _HeatingEntity:
        _attr_has_entity_name = True
        _attr_should_poll = False

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            self.manager = manager
            self.room_id = room_id
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, manager.entry.entry_id)},
                name=NAME,
                manufacturer="Zara-Toorox",
                model="Solar Forecast Stats",
                sw_version=VERSION,
            )

        @property
        def available(self) -> bool:
            return True

        def _room(self) -> RoomConfig:
            return self.manager._room(self.room_id)

        def _decision(self) -> RoomDecision | None:
            return self.manager.decisions.get(self.room_id)

    class HeatingTargetSensor(_HeatingEntity, sensor_entity_base):
        _attr_device_class = SensorDeviceClass.TEMPERATURE
        _attr_state_class = SensorStateClass.MEASUREMENT
        _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        _attr_translation_key = "heating_target"

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            super().__init__(manager, room_id)
            self._attr_unique_id = f"{manager.entry.entry_id}_heating_{room_id}_target"

        @property
        def native_value(self) -> float | None:
            room_decision = self._decision()
            return room_decision.target_c if room_decision else None

        @property
        def extra_state_attributes(self) -> dict[str, Any]:
            room_decision = self._decision()
            last_write = None
            if room_decision is not None:
                write_times = [
                    thermostat_decision.memory.last_write_at
                    for thermostat_decision in room_decision.thermostats
                    if thermostat_decision.memory.last_write_at is not None
                ]
                if write_times:
                    last_write = max(write_times).isoformat()
            boost_until = None
            boost = self.manager.boosts.get(self.room_id)
            if boost is not None and boost[1] > _utcnow():
                boost_until = boost[1].isoformat()
            detected_until = self.manager.detected_until.get(self.room_id)
            return (
                (
                    (
                        {
                            "mode": room_decision.mode if room_decision else "observe",
                            "reason": (
                                room_decision.reason
                                if room_decision
                                else "room_inactive"
                            ),
                            "presence": (
                                room_decision.presence
                                if room_decision
                                else self.manager.presence
                            ),
                            "last_write": last_write,
                            "boost_until": boost_until,
                            "window_detected": detected_until is not None
                            and detected_until > _utcnow(),
                            "lift": (
                                room_decision.reason
                                if room_decision
                                and room_decision.reason in {"pv_lift", "price_lift"}
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

    class HeatingActiveSwitch(_HeatingEntity, switch_entity_base):
        _attr_translation_key = "heating_active"

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            super().__init__(manager, room_id)
            self._attr_unique_id = f"{manager.entry.entry_id}_heating_{room_id}_active"

        @property
        def is_on(self) -> bool:
            try:
                return self._room().active
            except HeatingError:
                return False

        async def async_turn_on(self, **kwargs: Any) -> None:
            await self.manager.async_set_active(self.room_id, True)

        async def async_turn_off(self, **kwargs: Any) -> None:
            await self.manager.async_set_active(self.room_id, False)

    _entity_classes.cached = (HeatingTargetSensor, HeatingActiveSwitch)
    return (HeatingTargetSensor, HeatingActiveSwitch)


def get_heating_manager(hass: Any) -> HeatingManager | None:
    domain_data = hass.data.get(DOMAIN) if hasattr(hass, "data") else None
    if not isinstance(domain_data, dict):
        return
    for integration_data in domain_data.values():
        if not isinstance(integration_data, dict):
            continue
        if not integration_data.get("heating_manager") is not None:
            continue
        return integration_data["heating_manager"]
