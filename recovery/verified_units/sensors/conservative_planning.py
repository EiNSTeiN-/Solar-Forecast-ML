# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
import math
from datetime import date, datetime
from typing import Optional

DEFAULT_SFML_WEIGHT = 0.65
DEFAULT_TFS_P10_WEIGHT = 0.35
P10_PERSISTENCE_MAX_AGE_HOURS = 30.0
P10_MORNING_RECOMPUTE_WINDOW_MINUTES = 45.0
P10_TRACKING_COLUMNS = (
    "conservative_planning_forecast_kwh",
    "conservative_planning_forecast_updated_at",
    "conservative_planning_forecast_hours_json",
    "conservative_planning_forecast_panel_groups_json",
    "conservative_planning_forecast_group_totals_json",
)


def _non_negative_float(value: object) -> Optional[float]:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(number):
        return
    return max(0.0, number)


def _parse_date(value: object) -> Optional[date]:
    if value is None:
        return
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return


def parse_p10_timestamp(value: object) -> Optional[datetime]:
    if value is None:
        return
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return


def p10_age_hours(
    updated_at: object, now: Optional[datetime] = None
) -> Optional[float]:
    timestamp = parse_p10_timestamp(updated_at)
    if timestamp is None:
        return
    current_time = now or datetime.now(timestamp.tzinfo)
    if timestamp.tzinfo is None and current_time.tzinfo is not None:
        current_time = current_time.replace(tzinfo=None)
    elif timestamp.tzinfo is not None and current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timestamp.tzinfo)
    return round((current_time - timestamp).total_seconds() / 3600.0, 3)


def persisted_p10_freshness(
    forecast_date: object,
    updated_at: object,
    *,
    now: Optional[datetime] = None,
    max_age_hours: float = P10_PERSISTENCE_MAX_AGE_HOURS,
) -> dict:
    target_date = _parse_date(forecast_date)
    update_time = parse_p10_timestamp(updated_at)
    age_hours = p10_age_hours(update_time, now)
    if target_date is None:
        return {
            "fresh": False,
            "reason": "missing_forecast_date",
            "age_hours": age_hours,
        }
    if update_time is None:
        return {
            "fresh": False,
            "reason": "missing_p10_updated_at",
            "age_hours": age_hours,
        }
    if update_time.date() != target_date:
        return {"fresh": False, "reason": "stale_persisted_p10", "age_hours": age_hours}
    if age_hours is not None and age_hours > max_age_hours:
        return {
            "fresh": False,
            "reason": "expired_persisted_p10",
            "age_hours": age_hours,
        }
    return {"fresh": True, "reason": "fresh_persisted_p10", "age_hours": age_hours}


def p10_morning_recompute_allowed(
    forecast_date: object,
    lock_source: object,
    locked_at: object,
    *,
    now: Optional[datetime] = None,
    window_minutes: float = P10_MORNING_RECOMPUTE_WINDOW_MINUTES,
) -> bool:
    if str(lock_source or "") != "morning_routine":
        return False
    target_date = _parse_date(forecast_date)
    lock_time = parse_p10_timestamp(locked_at)
    if target_date is None or lock_time is None:
        return False
    if lock_time.date() != target_date:
        return False
    current_time = now or datetime.now(lock_time.tzinfo)
    if lock_time.tzinfo is None and current_time.tzinfo is not None:
        current_time = current_time.replace(tzinfo=None)
    elif lock_time.tzinfo is not None and current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=lock_time.tzinfo)
    elapsed_minutes = (current_time - lock_time).total_seconds() / 60.0
    return 0.0 <= elapsed_minutes <= window_minutes


def round_not_above(value: float, upper_bound: float, precision: int) -> float:
    cap = max(0.0, float(upper_bound or 0.0))
    rounded = round(max(0.0, float(value or 0.0)), precision)
    if rounded <= cap:
        return rounded
    scale = 10**precision
    return round(math.floor(cap * scale) / scale, precision)


def conservative_planning_value(
    sfml_value: object,
    tfs_p10_value: object,
    *,
    sfml_weight: float = DEFAULT_SFML_WEIGHT,
    tfs_weight: float = DEFAULT_TFS_P10_WEIGHT,
    precision: int = 4,
) -> float:
    sfml = _non_negative_float(sfml_value) or 0.0
    p10 = _non_negative_float(tfs_p10_value)
    if p10 is None:
        p10 = sfml
    weighted = sfml * sfml_weight + p10 * tfs_weight
    capped = max(0.0, min(sfml, weighted))
    return round_not_above(capped, sfml, precision)


def cap_persisted_conservative_value(
    persisted_value: object, full_day_sfml_cap: object, *, precision: int = 2
) -> Optional[float]:
    persisted_kwh = _non_negative_float(persisted_value)
    forecast_cap = _non_negative_float(full_day_sfml_cap)
    if persisted_kwh is None:
        return
    if forecast_cap is None:
        return round(persisted_kwh, precision)
    return round_not_above(min(persisted_kwh, forecast_cap), forecast_cap, precision)


def remaining_hourly_forecast_kwh(
    hourly_values: object, current_hour: int, *, precision: int = 2
) -> Optional[float]:
    """Sum forecast kWh for hours at or after the current local hour."""
    if not isinstance(hourly_values, dict) or not hourly_values:
        return
    try:
        start_hour = int(current_hour)
    except (TypeError, ValueError):
        return
    if start_hour < 0 or start_hour > 23:
        return
    total_kwh = 0.0
    has_reading = False
    for key, value in hourly_values.items():
        hour = None
        if isinstance(key, int):
            hour = key
        else:
            text = str(key).strip()
            if not text:
                continue
            try:
                hour = int(text.split(":", 1)[0])
            except ValueError:
                continue
        if hour < start_hour or hour > 23:
            continue
        energy = _non_negative_float(value)
        if energy is not None:
            has_reading = True
            total_kwh += energy
    if not has_reading:
        return
    return round(max(0.0, total_kwh), precision)


def remaining_today_expected_kwh(
    *,
    current_hour: int,
    hourly_values: object = None,
    expected_day_kwh: object = None,
    actual_today_kwh: object = None,
    precision: int = 2,
) -> float:
    # fmt: off
    """Rest-of-day expected energy for the Resttag sensor.

    Prefer remaining hourly forecast from now, and never exceed
    max(0, expected_day - actual_today) when that cap is known.
    """
    # fmt: on
    hourly_remaining = remaining_hourly_forecast_kwh(
        hourly_values, current_hour, precision=precision
    )
    actual_kwh = _non_negative_float(actual_today_kwh) or 0.0
    expected_kwh = _non_negative_float(expected_day_kwh)
    day_remaining = None
    if expected_kwh is not None:
        day_remaining = round(max(0.0, expected_kwh - actual_kwh), precision)
    if hourly_remaining is not None and day_remaining is not None:
        return round(min(hourly_remaining, day_remaining), precision)
    if hourly_remaining is not None:
        return hourly_remaining
    if day_remaining is not None:
        return day_remaining
    return 0.0


def remaining_conservative_value(
    conservative_full_day: object,
    actual_today: object,
    today_forecast_cap: object,
    *,
    precision: int = 2,
) -> Optional[float]:
    conservative_kwh = _non_negative_float(conservative_full_day)
    if conservative_kwh is None:
        return
    actual_kwh = _non_negative_float(actual_today) or 0.0
    remaining_kwh = max(0.0, conservative_kwh - actual_kwh)
    forecast_cap = _non_negative_float(today_forecast_cap)
    if forecast_cap is not None:
        remaining_kwh = min(remaining_kwh, forecast_cap)
        return round_not_above(remaining_kwh, forecast_cap, precision)
    return round(remaining_kwh, precision)
