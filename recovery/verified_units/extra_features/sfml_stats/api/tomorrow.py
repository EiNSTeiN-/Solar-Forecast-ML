# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Licensed SFML energy independence journal API."""
from __future__ import annotations
import logging
from datetime import date, datetime, time, timedelta
from math import isfinite, pi, sin
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..const import (
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_WALLBOX_POWER,
)
from ..core.corrected_energy import (
    corrected_daily,
    overlay_journal_row,
    projection_from_runtime,
)
from ..utils.time_utils import ha_now, ha_timezone, ha_today
from .premium_dashboard import (
    PremiumDashboardProviderError,
    _premium_is_live,
    _provider_snapshot,
)
from .views import _get_db, local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
_HISTORY_DAYS = 400
_MINIMUM_JOURNAL_DAYS = 7
_MINIMUM_DEVICE_ACTIVE_DAYS = 3
_AUTONOMOUS_DAY_PERCENT = 99.0
_SCHEMA_VERSION = 3
_ALLOWED_QUERY = frozenset({"date", "view"})
_ALLOWED_VIEWS = frozenset({"journal", "timeline"})
_SOLAR_PHASES = frozenset(
    {"noon", "night", "evening", "morning", "twilight", "afternoon"}
)
_CORE_HOUR_FIELDS = (
    "solar_yield_kwh",
    "home_consumption_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
)
_FLOW_HOUR_FIELDS = (
    "solar_to_house_kwh",
    "solar_to_battery_kwh",
    "battery_to_house_kwh",
    "grid_to_house_kwh",
    "grid_to_battery_kwh",
    "grid_import_cost_ct",
    "feed_in_revenue_ct",
    "price_ct_kwh",
    "consumer_heatingrod_kwh",
)


def _phase_for_minute(
    minute: int, events: Mapping[str, int | None], elevation: float | None = None
) -> str:
    """Map one local minute to a visual phase using local solar events."""
    dawn_minute = events.get("dawn")
    sunrise_minute = events.get("sunrise")
    solar_noon_minute = events.get("solar_noon")
    sunset_minute = events.get("sunset")
    dusk_minute = events.get("dusk")
    events_complete = all(
        (
            isinstance(event_minute, int)
            for event_minute in (
                dawn_minute,
                sunrise_minute,
                solar_noon_minute,
                sunset_minute,
                dusk_minute,
            )
        )
    )
    if events_complete:
        if (
            0
            <= dawn_minute
            < sunrise_minute
            < solar_noon_minute
            < sunset_minute
            < dusk_minute
            < 1440
        ):
            daylight_minutes = sunset_minute - sunrise_minute
            noon_half_width = max(45, min(90, round(daylight_minutes / 10)))
            evening_width = max(45, min(105, round(daylight_minutes / 8)))
            noon_start = max(sunrise_minute, solar_noon_minute - noon_half_width)
            noon_end = min(sunset_minute, solar_noon_minute + noon_half_width)
            evening_start = max(noon_end, sunset_minute - evening_width)
            if minute < dawn_minute:
                return "night"
            if minute < sunrise_minute:
                return "twilight"
            if minute < noon_start:
                return "morning"
            if minute < noon_end:
                return "noon"
            if minute < evening_start:
                return "afternoon"
            if minute < dusk_minute:
                return "evening"
            return "night"
    fallback_noon_minute = (
        solar_noon_minute if isinstance(solar_noon_minute, int) else 720
    )
    if elevation is not None:
        if elevation < -6:
            return "night"
        if elevation < 0:
            return "twilight" if minute < fallback_noon_minute else "evening"
        if abs(minute - fallback_noon_minute) <= 75:
            return "noon"
        if minute < fallback_noon_minute:
            return "morning"
        return "evening" if elevation <= 8 else "afternoon"
    if minute < 360 or minute >= 1200:
        return "night"
    if minute < 420:
        return "twilight"
    if minute < 645:
        return "morning"
    if minute < 795:
        return "noon"
    if minute < 1020:
        return "afternoon"
    return "evening"


def _local_astronomy_profile(hass: HomeAssistant, selected_day: date) -> dict[str, Any]:
    """Calculate offline visual phases for a historical day at the HA location."""
    local_timezone = ha_timezone(hass)
    event_names = {
        "dawn": "dawn",
        "sunrise": "sunrise",
        "solar_noon": "noon",
        "sunset": "sunset",
        "dusk": "dusk",
    }
    event_times = {event_key: None for event_key in event_names}
    observer = None
    try:
        import astral.sun
        from homeassistant.helpers.sun import (
            get_astral_event_date as get_astral_event,
            get_astral_observer as get_astral_observer,
        )

        observer = get_astral_observer(hass)
        for event_key, astral_event_name in event_names.items():
            event_times[event_key] = get_astral_event(
                hass, astral_event_name, selected_day
            )
    except (AttributeError, ImportError, TypeError, ValueError) as error:
        _LOGGER.debug(
            "Tomorrow local astronomy unavailable for %s: %s", selected_day, error
        )
    event_minutes = {}
    formatted_events = {}
    for event_key, event_datetime in event_times.items():
        if event_datetime is None:
            event_minutes[event_key] = None
            formatted_events[event_key] = None
            continue
        local_event_time = event_datetime.astimezone(local_timezone)
        event_minutes[event_key] = local_event_time.hour * 60 + local_event_time.minute
        formatted_events[event_key] = local_event_time.strftime("%H:%M")
    hour_phases = {}
    for hour in range(24):
        minute_midpoint = hour * 60 + 30
        sun_elevation = None
        if observer is not None:
            try:
                hour_datetime = datetime.combine(
                    selected_day, time(hour=hour, minute=30), tzinfo=local_timezone
                )
                sun_elevation = float(astral.sun.elevation(observer, hour_datetime))
            except (AttributeError, TypeError, ValueError):
                sun_elevation = None
        solar_phase = _phase_for_minute(minute_midpoint, event_minutes, sun_elevation)
        hour_phases[hour] = solar_phase if solar_phase in _SOLAR_PHASES else "night"
    return {
        "date": selected_day.isoformat(),
        "source": "home_assistant_local_astral",
        "timezone": str(local_timezone),
        "events": formatted_events,
        "phase_model": "civil_solar_local",
        "hour_phases": hour_phases,
    }


class TomorrowRequestError(ValueError):
    """Raised when a requested journal timeline is outside the allowed contract."""


def _number(value: Any, digits: int = 2) -> float:
    """Return a bounded non-negative display number."""
    try:
        return round(max(0.0, float(value)), digits)
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any, digits: int = 3) -> float | None:
    """Normalize a present non-negative database value without filling data gaps."""
    if value is None:
        return
    try:
        return round(max(0.0, float(value)), digits)
    except (TypeError, ValueError):
        return


def _optional_percent(value: Any, digits: int = 1) -> float | None:
    """Normalize an optional percentage without inventing missing quality."""
    if value is None:
        return
    try:
        percent_value = float(value)
    except (TypeError, ValueError):
        return
    if not isfinite(percent_value) or percent_value < 0:
        return None
    return round(min(100.0, percent_value), digits)


def _optional_soc_percent(value: Any, digits: int = 1) -> float | None:
    """Normalize a measured battery state of charge within its physical range."""
    if value is None:
        return
    try:
        percent_value = float(value)
    except (TypeError, ValueError):
        return
    if not (isfinite(percent_value) and 0 <= percent_value <= 100):
        return None if True else None
    return round(percent_value, digits)


def _value(row: Mapping[str, Any], key: str) -> Any:
    """Read mapping-like database rows without leaking implementation details."""
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return


def _shift_iso_day(value: str, days: int) -> str | None:
    try:
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return


def _own_energy_kwh(
    row: Mapping[str, Any], home: float | None
) -> tuple[float | None, str]:
    """Return canonical own house energy and the day-level quality flag."""
    if home is None:
        return (None, "missing")
    solar_to_house_kwh = _optional_number(_value(row, "solar_to_house_kwh"), 3)
    battery_to_house_kwh = _optional_number(_value(row, "battery_to_house_kwh"), 3)
    grid_to_battery_kwh = _optional_number(_value(row, "grid_to_battery_kwh"), 3)
    stored_autarky_percent = _optional_percent(_value(row, "autarkie_percent"))
    if solar_to_house_kwh is not None:
        own_energy_kwh = solar_to_house_kwh + max(
            0.0, (battery_to_house_kwh or 0.0) - (grid_to_battery_kwh or 0.0)
        )
        return (round(max(0.0, min(home, own_energy_kwh)), 3), "complete")
    if stored_autarky_percent is not None:
        return (round(max(0.0, home * stored_autarky_percent / 100.0), 3), "complete")
    grid_import_kwh = _optional_number(_value(row, "grid_import_kwh"), 3)
    if grid_import_kwh is None:
        return (None, "missing")
    return (round(max(0.0, home - min(home, grid_import_kwh)), 3), "incomplete")


def _story_kind(autonomy: float | None, quality: str) -> str:
    if quality == "missing" or autonomy is None:
        return "unknown"
    if autonomy >= _AUTONOMOUS_DAY_PERCENT:
        return "autonomous"
    if autonomy >= 80:
        return "strong"
    if autonomy >= 50:
        return "balanced"
    return "grid"


def _timeline_reason(
    *,
    sensor_configured: bool,
    hourly_row_count: int,
    coverage_hours: int,
    qualified: bool,
) -> str:
    if not sensor_configured:
        return "sensor_not_configured"
    if hourly_row_count == 0:
        return "no_hourly_rows"
    if not qualified:
        return "partial_coverage"
    if coverage_hours < 24:
        return "partial_coverage"
    return "complete"


def _daily_forecast_record(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose the same stored daily forecast result used by the live overview."""
    if not row:
        return (
            (
                (
                    {"forecast_kwh": None, "actual_kwh": None, "accuracy_percent": None}
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
    forecast_kwh = _optional_number(_value(row, "predicted_total_kwh"))
    actual_kwh = _optional_number(
        _value(row, "actual_total_kwh")
        if _value(row, "actual_total_kwh") is not None
        else _value(row, "actual_kwh")
    )
    return (
        (
            (
                {
                    "forecast_kwh": forecast_kwh,
                    "actual_kwh": actual_kwh,
                    "accuracy_percent": _optional_percent(
                        _value(row, "accuracy_percent")
                    ),
                    "delta_kwh": (
                        round(actual_kwh - forecast_kwh, 3)
                        if actual_kwh is not None and forecast_kwh is not None
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


def _day_finance(hours: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum stored hourly money fields without inventing missing costs."""
    import_cost_values = []
    revenue_values = []
    peak_price = None
    for hour_record in hours:
        import_cost_ct = _optional_number(hour_record.get("grid_import_cost_ct"), 2)
        revenue_ct = _optional_number(hour_record.get("feed_in_revenue_ct"), 2)
        price_ct = _optional_number(hour_record.get("price_ct_kwh"), 2)
        if import_cost_ct is not None:
            import_cost_values.append(import_cost_ct)
        if revenue_ct is not None:
            revenue_values.append(revenue_ct)
        if price_ct is not None and (
            peak_price is None or price_ct > peak_price["price_ct_kwh"]
        ):
            peak_price = {"hour": hour_record.get("hour"), "price_ct_kwh": price_ct}
    return (
        (
            (
                {
                    "grid_import_cost_ct": (
                        round(sum(import_cost_values), 2)
                        if import_cost_values
                        else None
                    ),
                    "feed_in_revenue_ct": (
                        round(sum(revenue_values), 2) if revenue_values else None
                    ),
                    "peak_price_hour": peak_price["hour"] if peak_price else None,
                    "peak_price_ct_kwh": (
                        peak_price["price_ct_kwh"] if peak_price else None
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


def _day_timeline(
    selected_day: date,
    rows: Iterable[Mapping[str, Any]],
    phase_by_hour: Mapping[int, str] | None = None,
    *,
    sensor_configured: bool = True,
    expected_hours: int = 24,
) -> dict[str, Any]:
    """Build a 24-hour timeline without estimating absent or ambiguous source rows."""
    rows_by_hour = {}
    valid_row_count = 0
    for source_row in rows:
        raw_hour = _value(source_row, "hour")
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            rows_by_hour.setdefault(hour, []).append(source_row)
            valid_row_count += 1
    timeline_hours = []
    coverage_hours = 0
    for hour in range(24):
        hour_rows = rows_by_hour.get(hour, [])
        flow_values = {field_name: None for field_name in _FLOW_HOUR_FIELDS}
        if not hour_rows:
            core_values = {field_name: None for field_name in _CORE_HOUR_FIELDS}
            quality = "missing"
            battery_soc_percent = None
            soc_values = {
                "battery_soc_min": None,
                "battery_soc_avg": None,
                "battery_soc_max": None,
                "battery_soc_end": None,
            }
        elif len(hour_rows) > 1:
            core_values = {field_name: None for field_name in _CORE_HOUR_FIELDS}
            quality = "ambiguous"
            battery_soc_percent = None
            soc_values = {
                "battery_soc_min": None,
                "battery_soc_avg": None,
                "battery_soc_max": None,
                "battery_soc_end": None,
            }
        else:
            hour_source_row = hour_rows[0]
            core_values = {
                field_name: _optional_number(_value(hour_source_row, field_name))
                for field_name in _CORE_HOUR_FIELDS
            }
            flow_values = {
                field_name: _optional_number(
                    _value(hour_source_row, field_name), 3 if "kwh" in field_name else 2
                )
                for field_name in _FLOW_HOUR_FIELDS
            }
            quality = (
                "complete"
                if all((core_value is not None for core_value in core_values.values()))
                else "incomplete"
            )
            soc_values = {
                "battery_soc_min": _optional_soc_percent(
                    _value(hour_source_row, "battery_soc_min")
                ),
                "battery_soc_avg": _optional_soc_percent(
                    _value(hour_source_row, "battery_soc_avg")
                ),
                "battery_soc_max": _optional_soc_percent(
                    _value(hour_source_row, "battery_soc_max")
                ),
                "battery_soc_end": _optional_soc_percent(
                    _value(hour_source_row, "battery_soc_end")
                ),
            }
            battery_soc_percent = soc_values["battery_soc_end"]
            if battery_soc_percent is None:
                battery_soc_percent = _optional_soc_percent(
                    _value(hour_source_row, "battery_soc_percent")
                )
        if quality == "complete":
            coverage_hours += 1
        timeline_hours.append(
            {
                "hour": hour,
                **core_values,
                **flow_values,
                **soc_values,
                "battery_soc_percent": battery_soc_percent,
                "quality": quality,
                "solar_phase": (
                    phase_by_hour.get(hour)
                    if phase_by_hour is not None
                    and phase_by_hour.get(hour) in _SOLAR_PHASES
                    else None
                ),
            }
        )
        continue
    complete_hours = [
        hour_record
        for hour_record in timeline_hours
        if hour_record["quality"] == "complete"
    ]
    max_export_kwh = max(
        (hour_record["grid_export_kwh"] for hour_record in complete_hours), default=0.0
    )
    if max_export_kwh > 0:
        highlight_hour = next(
            (
                hour_record["hour"]
                for hour_record in complete_hours
                if hour_record["grid_export_kwh"] == max_export_kwh
            )
        )
    elif complete_hours:
        highlight_hour = min(
            complete_hours, key=lambda item: (item["grid_import_kwh"], item["hour"])
        )["hour"]
    else:
        highlight_hour = None
    qualified = coverage_hours >= 20
    return {
        "date": selected_day.isoformat(),
        "coverage_hours": coverage_hours,
        "expected_hours": expected_hours,
        "qualified": qualified,
        "reason": _timeline_reason(
            sensor_configured=sensor_configured,
            hourly_row_count=valid_row_count,
            coverage_hours=coverage_hours,
            qualified=qualified,
        ),
        "highlight_hour": highlight_hour,
        "finance": _day_finance(timeline_hours),
        "hours": timeline_hours,
    }


def _selected_day(
    rows: Iterable[Mapping[str, Any]], requested_day: date | None
) -> date:
    """Allow only a completed day that the current history response contains."""
    available_days = set()
    for source_row in rows:
        try:
            available_days.add(date.fromisoformat(str(_value(source_row, "date"))))
        except (TypeError, ValueError):
            continue
    if not available_days:
        raise TomorrowRequestError("timeline_history_unavailable")
    if requested_day is not None:
        if requested_day not in available_days:
            raise TomorrowRequestError("date_not_available")
        return requested_day
    return max(available_days)


def _parse_query_arguments(request: web.Request) -> tuple[date | None, str]:
    """Parse the small allowlisted timeline query surface."""
    if set(request.query) - _ALLOWED_QUERY:
        raise TomorrowRequestError("unsupported_query_parameter")
    requested_view = request.query.get("view") or "journal"
    if requested_view not in _ALLOWED_VIEWS:
        raise TomorrowRequestError("unsupported_query_parameter")
    requested_date_text = request.query.get("date")
    requested_date = None
    if requested_date_text is not None:
        try:
            requested_date = date.fromisoformat(requested_date_text)
        except (TypeError, ValueError):
            raise TomorrowRequestError("invalid_date") from None
    return (requested_date, requested_view) if True else None


def _day_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one historical energy day for the public journal contract."""
    home_consumption_kwh = _optional_number(_value(row, "home_consumption_kwh"), 3)
    solar_yield_kwh = _optional_number(_value(row, "solar_yield_kwh"), 3)
    grid_import_kwh = _optional_number(_value(row, "grid_import_kwh"), 3)
    grid_export_kwh = _optional_number(_value(row, "grid_export_kwh"), 3)
    own_energy_kwh, quality = _own_energy_kwh(row, home_consumption_kwh)
    stored_autarky_percent = _optional_percent(_value(row, "autarkie_percent"))
    if stored_autarky_percent is not None:
        autonomy_percent = stored_autarky_percent
    elif (
        home_consumption_kwh
        and home_consumption_kwh > 0
        and (own_energy_kwh is not None)
    ):
        autonomy_percent = round(
            min(100.0, 100.0 * own_energy_kwh / home_consumption_kwh), 1
        )
    else:
        autonomy_percent = None
        if quality == "complete":
            quality = "missing"
    if autonomy_percent is None:
        quality = "missing" if quality != "incomplete" else quality
    self_consumption_kwh = _optional_number(_value(row, "self_consumption_kwh"), 3)
    solar_use_percent = (
        min(100.0, round(100 * self_consumption_kwh / solar_yield_kwh, 1))
        if self_consumption_kwh is not None
        and solar_yield_kwh
        and (solar_yield_kwh > 0)
        else None
    )
    story_kind = _story_kind(autonomy_percent, quality)
    return (
        (
            (
                {
                    "date": str(_value(row, "date")),
                    "quality": quality,
                    "autonomy_percent": autonomy_percent,
                    "autonomous": bool(
                        autonomy_percent is not None
                        and autonomy_percent >= _AUTONOMOUS_DAY_PERCENT
                    ),
                    "story_kind": story_kind,
                    "solar_yield_kwh": solar_yield_kwh,
                    "home_consumption_kwh": home_consumption_kwh,
                    "grid_import_kwh": grid_import_kwh,
                    "grid_export_kwh": grid_export_kwh,
                    "own_energy_kwh": own_energy_kwh,
                    "solar_use_percent": solar_use_percent,
                    "heat_pump_kwh": _optional_number(
                        _value(row, "consumer_heatpump_kwh"), 2
                    ),
                    "heating_rod_kwh": _optional_number(
                        _value(row, "consumer_heatingrod_kwh"), 2
                    ),
                    "wallbox_kwh": _optional_number(
                        _value(row, "consumer_wallbox_kwh"), 2
                    ),
                    "peak_solar_w": _optional_number(_value(row, "peak_solar_w"), 0),
                    "peak_solar_time": (
                        str(_value(row, "peak_solar_time"))
                        if _value(row, "peak_solar_time")
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


def _journal_day(
    row: Mapping[str, Any],
    daily: dict[str, dict[str, float]] | None = None,
    projection: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Normalize one day, applying the correction overlay only when that day has one."""
    day_key = str(_value(row, "date"))
    overlaid_row, corrected_metrics = overlay_journal_row(
        row, (daily or {}).get(day_key), (projection or {}).get(day_key)
    )
    journal_record = _day_record(overlaid_row)
    if corrected_metrics:
        journal_record["corrected"] = True
        journal_record["corrected_metrics"] = list(corrected_metrics)
    return journal_record


def _journal_records(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute streaks and best periods from the already-normalized history."""
    usable_days = [
        day_record
        for day_record in history
        if day_record.get("date")
        if day_record.get("quality") != "missing"
    ]
    autonomy_days = [
        day_record
        for day_record in usable_days
        if day_record.get("autonomy_percent") is not None
    ]
    best_day = max(autonomy_days, key=lambda day: day["autonomy_percent"], default=None)
    current_streak = 0
    longest_streak = 0
    streak_end = None
    previous_day = None
    for day_record in usable_days:
        if day_record.get("autonomous") is True:
            next_day = _shift_iso_day(previous_day, 1) if previous_day else None
            current_streak = current_streak + 1 if next_day == day_record["date"] else 1
            if current_streak >= longest_streak:
                longest_streak = current_streak
                streak_end = day_record["date"]
        else:
            current_streak = 0
        previous_day = day_record["date"]
    best_week = None
    for start_index, day_record in enumerate(usable_days):
        week_days = usable_days[start_index : start_index + 7]
        if len(week_days) < 7:
            break
        week_start = week_days[0]["date"]
        week_end = week_days[6]["date"]
        if _shift_iso_day(week_start, 6) != week_end:
            continue
        weekly_autonomy_values = [
            week_day_record["autonomy_percent"]
            for week_day_record in week_days
            if week_day_record.get("autonomy_percent") is not None
        ]
        if len(weekly_autonomy_values) < 7:
            continue
        weekly_autonomy = round(sum(weekly_autonomy_values) / 7, 1)
        if best_week is None or weekly_autonomy > best_week["autonomy_percent"]:
            best_week = {
                "start": week_start,
                "end": week_end,
                "autonomy_percent": weekly_autonomy,
            }
    return (
        {
            "best_day": (
                {
                    "date": best_day["date"],
                    "autonomy_percent": best_day["autonomy_percent"],
                }
                if best_day
                else None
            ),
            "longest_autonomous_streak": (
                {
                    "days": longest_streak,
                    "end": streak_end,
                    "start": (
                        _shift_iso_day(streak_end, 1 - longest_streak)
                        if streak_end
                        else None
                    ),
                }
                if longest_streak
                else None
            ),
            "best_week": best_week,
        }
        if True
        else None
    )


def _month_rollup(
    history: list[dict[str, Any]], year: int, month: int
) -> dict[str, Any] | None:
    month_prefix = f"{year:04d}-{month:02d}"
    month_days = [
        day_record
        for day_record in history
        if str(day_record.get("date", "")).startswith(month_prefix)
        if day_record.get("quality") != "missing"
    ]
    if not month_days:
        return ((None if True else None) if True else None) if True else None
    home_consumption_kwh = sum(
        (day_record["home_consumption_kwh"] or 0.0 for day_record in month_days)
    )
    own_energy_kwh = sum(
        (day_record["own_energy_kwh"] or 0.0 for day_record in month_days)
    )
    return (
        (
            (
                {
                    "year": year,
                    "month": month,
                    "days": len(month_days),
                    "own_energy_kwh": round(own_energy_kwh, 1),
                    "autonomy_percent": (
                        round(100 * own_energy_kwh / home_consumption_kwh, 1)
                        if home_consumption_kwh > 0
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


def _period_comparisons(history: list[dict[str, Any]], today: date) -> dict[str, Any]:
    previous_month = today.month - 1 or 12
    previous_year = today.year if today.month > 1 else today.year - 1
    return {
        "current_month": _month_rollup(history, today.year, today.month),
        "previous_month": _month_rollup(history, previous_year, previous_month),
        "same_month_last_year": _month_rollup(history, today.year - 1, today.month),
    }


def _demo_consumer_kwh(day_value: str, kind: str) -> float:
    """Return a stable demonstration kWh that does not use customer history."""
    try:
        day_ordinal = date.fromisoformat(str(day_value)).toordinal()
    except (TypeError, ValueError):
        day_ordinal = 0
    phase_offset = {"wallbox": 7, "heating_rod": 11}.get(kind, 2)
    pattern_factor = 0.55 + 0.35 * sin((day_ordinal + phase_offset) * pi / 11)
    base_kwh = {"heat_pump": 3.4, "wallbox": 1.8, "heating_rod": 1.1}.get(kind, 1.8)
    return round(max(0.2, base_kwh * pattern_factor), 2)


def _demo_devices() -> dict[str, Any]:
    return {
        "heat_pump": {
            "visible": True,
            "reason": "demo",
            "is_demo": True,
            "sample_reason": "no_license",
            "active_history_days": 90,
            "minimum_history_days": _MINIMUM_DEVICE_ACTIVE_DAYS,
        },
        "wallbox": {
            "visible": True,
            "reason": "demo",
            "is_demo": True,
            "sample_reason": "no_license",
            "active_history_days": 90,
            "minimum_history_days": _MINIMUM_DEVICE_ACTIVE_DAYS,
        },
        "heating_rod": {
            "visible": True,
            "reason": "demo",
            "is_demo": True,
            "sample_reason": "no_license",
            "active_history_days": 90,
            "minimum_history_days": _MINIMUM_DEVICE_ACTIVE_DAYS,
        },
    }


def _apply_device_history(
    history: list[dict[str, Any]], devices: dict[str, Any], *, allow_demo: bool = False
) -> None:
    device_fields = {
        "heat_pump": ("heat_pump_kwh", "heat_pump"),
        "wallbox": ("wallbox_kwh", "wallbox"),
        "heating_rod": ("heating_rod_kwh", "heating_rod"),
    }
    for device_kind, (field_name, demo_kind) in device_fields.items():
        device_info = devices.get(device_kind, {})
        include_device = device_info.get("visible") is True and (
            allow_demo or device_info.get("is_demo") is not True
        )
        if not include_device:
            for day_record in history:
                day_record.pop(field_name, None)
        elif device_info.get("is_demo") is True:
            for day_record in history:
                day_record[field_name] = _demo_consumer_kwh(
                    day_record["date"], demo_kind
                )


def _journal_payload(
    now: datetime,
    rows: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    licensed: bool,
    is_demo: bool,
    license_status: str,
    devices: dict[str, Any] | None = None,
    daily: dict[str, dict[str, float]] | None = None,
    projection: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Build the shared historical journal contract."""
    if projection:
        history_records = [
            _journal_day(source_row, daily, projection) for source_row in rows
        ]
    else:
        history_records = [_day_record(source_row) for source_row in rows]
    complete_days = [
        day_record
        for day_record in history_records
        if day_record.get("quality") != "missing"
        if day_record.get("home_consumption_kwh") is not None
        if day_record.get("own_energy_kwh") is not None
    ]
    consumption_total_kwh = sum(
        (day_record["home_consumption_kwh"] or 0.0 for day_record in complete_days)
    )
    own_energy_total_kwh = sum(
        (day_record["own_energy_kwh"] or 0.0 for day_record in complete_days)
    )
    self_supply_percent = (
        round(100 * own_energy_total_kwh / consumption_total_kwh, 1)
        if consumption_total_kwh > 0
        else 0.0
    )
    autonomous_days = sum(
        (1 for day_record in history_records if day_record["autonomous"])
    )
    period_start = history_records[0]["date"] if history_records else None
    period_end = history_records[-1]["date"] if history_records else None
    if devices is not None:
        visible_devices = devices
    elif is_demo:
        visible_devices = _demo_devices()
    else:
        visible_devices = {}
    _apply_device_history(history_records, visible_devices, allow_demo=is_demo)
    today_day = now.date()
    return {
        "schema_version": _SCHEMA_VERSION,
        "mode": mode,
        "data_mode": "demo" if is_demo else "live",
        "data_provenance": "demo_history" if is_demo else "local_daily_energy",
        "house_data_connected": not is_demo,
        "is_demo": is_demo,
        "sample_reason": (
            "no_license" if is_demo or license_status != "valid" else "live"
        ),
        "licensed": licensed,
        "license_status": license_status,
        "generated_at": now.isoformat(),
        "period": {
            "start": period_start,
            "end": period_end,
            "days": len(history_records),
        },
        "kpis": [
            {
                "id": "autonomous_days",
                "value": autonomous_days,
                "unit": "days",
                "detail_code": "min_autonomy",
            },
            {
                "id": "self_supply",
                "value": self_supply_percent,
                "unit": "%",
                "detail_code": "weighted_home",
            },
            {
                "id": "own_energy",
                "value": round(own_energy_total_kwh, 1),
                "unit": "kWh",
                "detail_code": "instead_of_grid",
            },
        ],
        "records": _journal_records(history_records),
        "comparisons": _period_comparisons(history_records, today_day),
        "history": history_records,
        "devices": visible_devices,
        "automation": {
            "enabled": False,
            "events_enabled": False,
            "reason": "historical_insight_only",
        },
    }


def _mock_rows(now: datetime, days: int = 90) -> list[dict[str, Any]]:
    """Return deterministic example history without reading customer data."""
    last_completed_day = now.date() - timedelta(days=1)
    mock_day_rows = []
    for day_index in range(days):
        current_day = last_completed_day - timedelta(days=days - 1 - day_index)
        solar_factor = 0.58 + 0.34 * sin((day_index + 4) * pi / 19)
        solar_yield_kwh = round(max(0.7, 10.8 * solar_factor), 3)
        home_consumption_kwh = round(7.2 + 1.15 * sin((day_index + 1) * pi / 5), 3)
        autonomy_percent = min(100.0, max(24.0, 42 + solar_yield_kwh * 7.4))
        own_energy_kwh = round(home_consumption_kwh * autonomy_percent / 100.0, 3)
        grid_import_kwh = round(max(0.0, home_consumption_kwh - own_energy_kwh), 3)
        day_text = current_day.isoformat()
        mock_day_rows.append(
            {
                "date": day_text,
                "solar_yield_kwh": solar_yield_kwh,
                "solar_to_house_kwh": own_energy_kwh,
                "battery_to_house_kwh": 0.0,
                "grid_to_battery_kwh": 0.0,
                "grid_import_kwh": grid_import_kwh,
                "grid_export_kwh": max(0.0, solar_yield_kwh - own_energy_kwh),
                "home_consumption_kwh": home_consumption_kwh,
                "self_consumption_kwh": min(solar_yield_kwh, own_energy_kwh),
                "autarkie_percent": autonomy_percent,
                "consumer_heatpump_kwh": _demo_consumer_kwh(day_text, "heat_pump"),
                "consumer_wallbox_kwh": _demo_consumer_kwh(day_text, "wallbox"),
                "consumer_heatingrod_kwh": _demo_consumer_kwh(day_text, "heating_rod"),
                "peak_solar_w": round(1200 + 800 * solar_factor, 0),
                "peak_solar_time": "13:12",
            }
        )
        continue
    return mock_day_rows


def _mock_hourly_rows(selected_day: date) -> list[dict[str, Any]]:
    """Return a deterministic 24-hour demonstration day without customer reads."""
    day_variation = selected_day.toordinal() % 17
    mock_hour_rows = []
    for hour in range(24):
        solar_profile = max(0.0, sin((hour - 6) * pi / 14)) if 6 <= hour <= 20 else 0.0
        solar_yield_kwh = round(4.2 * solar_profile * (0.86 + day_variation / 100), 3)
        home_consumption_kwh = round(
            0.34 + (0.64 if hour in {6, 7, 18, 19, 20} else 0.0), 3
        )
        solar_to_house_kwh = round(min(solar_yield_kwh, home_consumption_kwh), 3)
        grid_import_kwh = round(max(0.0, home_consumption_kwh - solar_yield_kwh), 3)
        grid_export_kwh = round(max(0.0, solar_yield_kwh - home_consumption_kwh), 3)
        mock_hour_rows.append(
            {
                "hour": hour,
                "solar_yield_kwh": solar_yield_kwh,
                "home_consumption_kwh": home_consumption_kwh,
                "grid_import_kwh": grid_import_kwh,
                "grid_export_kwh": grid_export_kwh,
                "solar_to_house_kwh": solar_to_house_kwh,
                "solar_to_battery_kwh": round(
                    max(0.0, solar_yield_kwh - solar_to_house_kwh - grid_export_kwh), 3
                ),
                "battery_to_house_kwh": round(
                    max(
                        0.0, home_consumption_kwh - solar_to_house_kwh - grid_import_kwh
                    ),
                    3,
                ),
                "grid_to_house_kwh": grid_import_kwh,
                "grid_to_battery_kwh": 0.0,
                "grid_import_cost_ct": round(grid_import_kwh * 28.4, 2),
                "feed_in_revenue_ct": round(grid_export_kwh * 8.2, 2),
                "price_ct_kwh": 18.0 + (6 if hour in {17, 18, 19} else 0),
                "consumer_heatingrod_kwh": round(0.12 if hour in {6, 7} else 0.0, 3),
                "battery_soc_end": (
                    round(40 + 35 * solar_profile, 1) if solar_profile else 38.0
                ),
            }
        )
        continue
    return mock_hour_rows


def _mock_payload(
    now: datetime,
    license_status: str = "not_provided",
    requested_day: date | None = None,
    astronomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an interactive unlicensed journal demo."""
    mock_rows = _mock_rows(now)
    selected_day = _selected_day(mock_rows, requested_day)
    payload = _journal_payload(
        now,
        mock_rows,
        mode="mock",
        licensed=False,
        is_demo=True,
        license_status=license_status,
        devices=_demo_devices(),
    )
    phase_by_hour = astronomy.get("hour_phases", {}) if astronomy else {}
    payload["day_timeline"] = _day_timeline(
        selected_day,
        _mock_hourly_rows(selected_day),
        phase_by_hour,
        sensor_configured=True,
    )
    timeline_hours = payload["day_timeline"]["hours"]
    actual_solar_kwh = round(
        sum((hour_record["solar_yield_kwh"] or 0.0 for hour_record in timeline_hours)),
        3,
    )
    forecast_solar_kwh = round(
        actual_solar_kwh * (0.94 + selected_day.toordinal() % 7 / 100), 3
    )
    forecast_accuracy = (
        round(
            100
            * min(actual_solar_kwh, forecast_solar_kwh)
            / max(actual_solar_kwh, forecast_solar_kwh),
            1,
        )
        if max(actual_solar_kwh, forecast_solar_kwh) > 0
        else None
    )
    payload["day_timeline"]["daily_forecast"] = {
        "forecast_kwh": forecast_solar_kwh,
        "actual_kwh": actual_solar_kwh,
        "accuracy_percent": forecast_accuracy,
        "delta_kwh": round(actual_solar_kwh - forecast_solar_kwh, 3),
    }
    if astronomy:
        payload["astronomy"] = {
            astronomy_key: astronomy_value
            for astronomy_key, astronomy_value in astronomy.items()
            if astronomy_key != "hour_phases"
        }
    return payload


def _stats_runtime(hass: HomeAssistant) -> dict[str, Any]:
    """Return the first configured STATS runtime entry."""
    stats_entries = hass.data.get("sfml_stats", {})
    if not isinstance(stats_entries, dict):
        return {}
    return next(
        (
            runtime_entry
            for runtime_entry in stats_entries.values()
            if isinstance(runtime_entry, dict)
            and isinstance(runtime_entry.get("config"), dict)
        ),
        {},
    )


def _entity_available(hass: HomeAssistant, entity_id: Any) -> bool:
    """Return whether a configured entity currently provides a usable state."""
    if not (isinstance(entity_id, str) and entity_id.strip()):
        return False
    entity_state = hass.states.get(entity_id.strip())
    return bool(
        entity_state is not None
        and entity_state.state not in {"unknown", "unavailable"}
    )


def _smartmeter_configured(hass: HomeAssistant) -> bool:
    runtime = _stats_runtime(hass)
    stats_config = (
        runtime.get("config") if isinstance(runtime.get("config"), dict) else {}
    )
    configured_entity_id = stats_config.get(CONF_SENSOR_SMARTMETER_IMPORT)
    return isinstance(configured_entity_id, str) and bool(configured_entity_id.strip())


def _device_insight(
    *, configured: bool, entity_available: bool, active_days: int
) -> dict[str, Any]:
    """Licensed journal: show only configured consumers; never invent demo kWh."""
    if not configured:
        return {
            "visible": False,
            "reason": "not_configured",
            "is_demo": False,
            "active_history_days": 0,
            "minimum_history_days": _MINIMUM_DEVICE_ACTIVE_DAYS,
        }
    ready = bool(entity_available and active_days >= _MINIMUM_DEVICE_ACTIVE_DAYS)
    return {
        "visible": True,
        "reason": "ready" if ready else "configured",
        "is_demo": False,
        "active_history_days": active_days,
        "minimum_history_days": _MINIMUM_DEVICE_ACTIVE_DAYS,
    }


async def _device_visibility(
    hass: HomeAssistant, db: Any, earliest_date: str, latest_date: str
) -> dict[str, Any]:
    """Expose licensed consumer cards only when that consumer is configured."""
    runtime = _stats_runtime(hass)
    stats_config = (
        runtime.get("config") if isinstance(runtime.get("config"), dict) else {}
    )
    active_days_by_device = {}
    async with db.execute(
        "\n        SELECT COUNT(DISTINCT date) AS active_days\n        FROM stats_hourly_billing\n        WHERE date >= ? AND date < ? AND consumer_heatpump_kwh > 0.01\n        ",
        (earliest_date, latest_date),
    ) as cursor:
        count_row = await cursor.fetchone()
        active_days_by_device["heat_pump"] = (
            int(count_row["active_days"] or 0) if count_row else 0
        )
    async with db.execute(
        "\n        SELECT COUNT(DISTINCT date) AS active_days\n        FROM stats_hourly_billing\n        WHERE date >= ? AND date < ? AND consumer_wallbox_kwh > 0.01\n        ",
        (earliest_date, latest_date),
    ) as cursor:
        count_row = await cursor.fetchone()
        active_days_by_device["wallbox"] = (
            int(count_row["active_days"] or 0) if count_row else 0
        )
    async with db.execute(
        "\n        SELECT COUNT(DISTINCT date) AS active_days\n        FROM stats_hourly_billing\n        WHERE date >= ? AND date < ? AND consumer_heatingrod_kwh > 0.01\n        ",
        (earliest_date, latest_date),
    ) as cursor:
        count_row = await cursor.fetchone()
        active_days_by_device["heating_rod"] = (
            int(count_row["active_days"] or 0) if count_row else 0
        )
    device_config_keys = {
        "heat_pump": CONF_SENSOR_HEATPUMP_POWER,
        "wallbox": CONF_SENSOR_WALLBOX_POWER,
        "heating_rod": CONF_SENSOR_HEATINGROD_POWER,
    }
    devices_result = {}
    for device_kind, config_key in device_config_keys.items():
        from ..core.eai_wp_sources import (
            resolved_heatpump_entity as resolve_heatpump_entity,
        )

        configured_entity_id = (
            resolve_heatpump_entity(hass, stats_config, config_key)
            if device_kind == "heat_pump"
            else stats_config.get(config_key)
        )
        configured = isinstance(configured_entity_id, str) and bool(
            configured_entity_id.strip()
        )
        devices_result[device_kind] = _device_insight(
            configured=configured,
            entity_available=_entity_available(hass, configured_entity_id),
            active_days=active_days_by_device[device_kind],
        )
    return devices_result


def _public_astronomy(astronomy: dict[str, Any]) -> dict[str, Any]:
    return {
        astronomy_key: astronomy_value
        for astronomy_key, astronomy_value in astronomy.items()
        if astronomy_key != "hour_phases"
    }


def _unavailable_payload(
    *, history_days: int, devices: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "mode": "unavailable",
        "data_mode": "live",
        "is_demo": False,
        "licensed": True,
        "license_status": "valid",
        "message_code": "need_seven_days",
        "data_readiness": {
            "history_days": history_days,
            "minimum_history_days": _MINIMUM_JOURNAL_DAYS,
        },
        "devices": devices,
    }


async def _selected_day_bundle(
    hass: HomeAssistant, selected_day: date, *, sensor_configured: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    astronomy_profile = _local_astronomy_profile(hass, selected_day)
    async with _get_db() as database:
        async with database.execute(
            "\n            SELECT hb.hour, hb.solar_yield_kwh, hb.home_consumption_kwh,\n                   hb.grid_import_kwh, hb.grid_export_kwh,\n                   hb.solar_to_house_kwh, hb.solar_to_battery_kwh,\n                   hb.battery_to_house_kwh, hb.grid_to_house_kwh,\n                   hb.grid_to_battery_kwh, hb.grid_import_cost_ct,\n                   hb.feed_in_revenue_ct, hb.price_ct_kwh,\n                   hb.consumer_heatingrod_kwh,\n                   hb.battery_soc_min, hb.battery_soc_avg,\n                   hb.battery_soc_max, hb.battery_soc_end,\n                   COALESCE(\n                       hb.battery_soc_end,\n                       (\n                           SELECT ps.battery_soc\n                           FROM stats_power_sources AS ps\n                           WHERE ps.date = hb.date\n                             AND ps.hour = hb.hour\n                             AND ps.battery_soc IS NOT NULL\n                             AND ps.battery_soc BETWEEN 0 AND 100\n                           ORDER BY ps.timestamp DESC\n                           LIMIT 1\n                       )\n                   ) AS battery_soc_percent\n            FROM stats_hourly_billing AS hb\n            WHERE hb.date = ?\n            ORDER BY hb.hour ASC\n            ",
            (selected_day.isoformat(),),
        ) as cursor:
            hourly_rows = await cursor.fetchall()
        async with database.execute(
            "\n            SELECT predicted_total_kwh, actual_total_kwh, accuracy_percent\n            FROM daily_summaries\n            WHERE date = ?\n            LIMIT 1\n            ",
            (selected_day.isoformat(),),
        ) as cursor:
            daily_forecast_row = await cursor.fetchone()
        async with database.execute(
            "\n            SELECT actual_kwh, sfml_forecast_kwh, external_1_kwh, external_2_kwh,\n                   best_source\n            FROM stats_forecast_comparison\n            WHERE date = ?\n            LIMIT 1\n            ",
            (selected_day.isoformat(),),
        ) as cursor:
            comparison_row = await cursor.fetchone()
    timeline = _day_timeline(
        selected_day,
        hourly_rows,
        astronomy_profile.get("hour_phases", {}),
        sensor_configured=sensor_configured,
    )
    timeline["daily_forecast"] = _daily_forecast_record(daily_forecast_row)
    if comparison_row:
        timeline["forecast_comparison"] = {
            "actual_kwh": _optional_number(_value(comparison_row, "actual_kwh")),
            "sfml_forecast_kwh": _optional_number(
                _value(comparison_row, "sfml_forecast_kwh")
            ),
            "external_1_kwh": _optional_number(
                _value(comparison_row, "external_1_kwh")
            ),
            "external_2_kwh": _optional_number(
                _value(comparison_row, "external_2_kwh")
            ),
            "best_source": _value(comparison_row, "best_source"),
        }
    else:
        timeline["forecast_comparison"] = None
    if True:
        return (timeline, astronomy_profile)


async def _live_payload(
    hass: HomeAssistant,
    snapshot: dict[str, Any],
    requested_day: date | None = None,
    view: str = "journal",
) -> dict[str, Any]:
    """Return the licensed journal using local completed daily balances."""
    del snapshot
    current_time = ha_now(hass)
    today_day = ha_today(hass)
    earliest_day = today_day - timedelta(days=_HISTORY_DAYS)
    smartmeter_configured = _smartmeter_configured(hass)
    async with _get_db() as database:
        async with database.execute(
            "\n            SELECT date, solar_yield_kwh, grid_import_kwh, grid_export_kwh,\n                   home_consumption_kwh, self_consumption_kwh, autarkie_percent,\n                   solar_to_house_kwh, battery_to_house_kwh, grid_to_battery_kwh,\n                   consumer_heatpump_kwh, consumer_wallbox_kwh,\n                   consumer_heatingrod_kwh, peak_solar_w, peak_solar_time\n            FROM stats_daily_energy\n            WHERE date >= ? AND date < ?\n            ORDER BY date ASC\n            ",
            (earliest_day.isoformat(), today_day.isoformat()),
        ) as cursor:
            daily_rows = await cursor.fetchall()
        devices = await _device_visibility(
            hass, database, earliest_day.isoformat(), today_day.isoformat()
        )
        correction_projection = {}
        corrected_daily_values = {}
        try:
            correction_projection = await projection_from_runtime(
                earliest_day.isoformat()
            )
            if correction_projection:
                corrected_daily_values = await corrected_daily(
                    database,
                    earliest_day.isoformat(),
                    (today_day - timedelta(days=1)).isoformat(),
                    correction_projection,
                )
                if not corrected_daily_values:
                    correction_projection = {}
                    corrected_daily_values = {}
        except Exception as error:
            _LOGGER.warning("Journal corrections unavailable: %s", error)
            correction_projection = {}
            corrected_daily_values = {}
    if not daily_rows:
        return _unavailable_payload(history_days=0, devices=devices)
    selected_day = _selected_day(daily_rows, requested_day)
    if view == "timeline":
        timeline, astronomy_profile = await _selected_day_bundle(
            hass, selected_day, sensor_configured=smartmeter_configured
        )
        selected_source_row = next(
            (
                source_row
                for source_row in daily_rows
                if str(_value(source_row, "date")) == selected_day.isoformat()
            ),
            daily_rows[-1],
        )
        selected_day_record = _journal_day(
            selected_source_row, corrected_daily_values, correction_projection
        )
        _apply_device_history([selected_day_record], devices, allow_demo=False)
        return {
            "schema_version": _SCHEMA_VERSION,
            "view": "timeline",
            "mode": "live",
            "licensed": True,
            "license_status": "valid",
            "is_demo": False,
            "selected_day": selected_day_record,
            "day_timeline": timeline,
            "astronomy": _public_astronomy(astronomy_profile),
        }
    if len(daily_rows) < _MINIMUM_JOURNAL_DAYS:
        return _unavailable_payload(history_days=len(daily_rows), devices=devices)
    timeline, astronomy_profile = await _selected_day_bundle(
        hass, selected_day, sensor_configured=smartmeter_configured
    )
    payload = _journal_payload(
        current_time,
        daily_rows,
        mode="live",
        licensed=True,
        is_demo=False,
        license_status="valid",
        devices=devices,
        daily=corrected_daily_values,
        projection=correction_projection,
    )
    payload["day_timeline"] = timeline
    payload["astronomy"] = _public_astronomy(astronomy_profile)
    return payload


class TomorrowView(HomeAssistantView):
    """Serve the license-gated local energy independence journal."""

    url = "/api/sfml_stats/modern/tomorrow"
    name = "api:sfml_stats:modern:tomorrow"
    requires_auth = True
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        try:
            requested_date, requested_view = _parse_query_arguments(request)
            provider_snapshot = _provider_snapshot(self.hass)
            if _premium_is_live(provider_snapshot):
                payload = await _live_payload(
                    self.hass, provider_snapshot, requested_date, requested_view
                )
            else:
                provider_license_status = (
                    str(provider_snapshot.get("license_status"))
                    if provider_snapshot is not None
                    else "not_provided"
                )
                current_time = ha_now(self.hass)
                selected_day = _selected_day(_mock_rows(current_time), requested_date)
                payload = _mock_payload(
                    current_time,
                    provider_license_status,
                    requested_date,
                    _local_astronomy_profile(self.hass, selected_day),
                )
                if requested_view == "timeline":
                    payload = {
                        "schema_version": _SCHEMA_VERSION,
                        "view": "timeline",
                        "mode": payload["mode"],
                        "licensed": payload["licensed"],
                        "is_demo": True,
                        "selected_day": next(
                            (
                                day_record
                                for day_record in payload["history"]
                                if day_record["date"] == selected_day.isoformat()
                            ),
                            payload["history"][-1],
                        ),
                        "day_timeline": payload["day_timeline"],
                        "astronomy": payload.get("astronomy"),
                    }
            return web.json_response(
                {"success": True, "data": payload},
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                },
            )
        except TomorrowRequestError as error:
            return web.json_response(
                {"success": False, "error": {"code": str(error)}},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        except PremiumDashboardProviderError as error:
            _LOGGER.warning("Tomorrow licence provider unavailable: %s", error)
            return web.json_response(
                {"success": False, "error": {"code": "tomorrow_provider_unavailable"}},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        except Exception as error:
            _LOGGER.error(
                "Tomorrow request failed (%s)", type(error).__name__, exc_info=True
            )
            return web.json_response(
                {
                    "success": False,
                    "error": {
                        "code": "tomorrow_unavailable",
                        "message": "SFML Tomorrow is temporarily unavailable.",
                    },
                },
                status=503,
                headers={"Cache-Control": "no-store"},
            )


async def async_setup_tomorrow_view(hass: HomeAssistant) -> None:
    hass.http.register_view(TomorrowView(hass))
