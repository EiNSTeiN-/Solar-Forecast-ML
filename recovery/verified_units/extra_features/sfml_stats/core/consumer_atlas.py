# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Consumer atlas helpers for SFML Stats."""

from __future__ import annotations
import calendar
import re
from datetime import date, timedelta
from typing import Any

DEFAULT_CONSUMER_COLORS = (
    "#06b6d4",
    "#22c55e",
    "#f59e0b",
    "#ec4899",
    "#8b5cf6",
    "#ef4444",
    "#14b8a6",
    "#f97316",
)
VALID_CONSUMER_CATEGORIES = {
    "other",
    "office",
    "heating",
    "kitchen",
    "utility",
    "mobility",
    "entertainment",
}


def _to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return fallback
    if number != number:
        return fallback
    return number


def _to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return fallback


def make_consumer_id(name: Any, entity_id: Any) -> str:
    identifier = str(entity_id or name or "consumer").strip().lower()
    identifier = identifier.replace("sensor.", "")
    identifier = re.sub("[^a-z0-9_]+", "_", identifier)
    identifier = re.sub("_+", "_", identifier).strip("_")
    return (identifier or "consumer")[:64]


def sanitize_consumer_config(
    payload: dict[str, Any] | None,
    current: dict[str, Any] | None = None,
    *,
    default_sort_order: int = 0,
) -> dict[str, Any]:
    merged = {**(current or {}), **(payload or {})}
    entity_id = str(merged.get("entity_id") or "").strip()
    name = str(merged.get("name") or "").strip()
    if not name:
        if entity_id:
            name = entity_id.split(".", 1)[-1].replace("_", " ").strip().title()
    consumer_id = str(merged.get("consumer_id") or merged.get("id") or "").strip()
    if not consumer_id:
        consumer_id = make_consumer_id(name, entity_id)
    consumer_id = make_consumer_id(consumer_id, consumer_id)
    category = str(merged.get("category") or "other").strip().lower()
    if category not in VALID_CONSUMER_CATEGORIES:
        category = "other"
    icon = re.sub(
        "[^a-z0-9_-]+", "", str(merged.get("icon") or "plug").strip().lower()
    )[:32]
    if not icon:
        icon = "plug"
    color = str(merged.get("color") or "").strip()
    if not re.fullmatch("#[0-9a-fA-F]{6}", color):
        color = DEFAULT_CONSUMER_COLORS[
            abs(hash(consumer_id)) % len(DEFAULT_CONSUMER_COLORS)
        ]
    enabled_value = merged.get("enabled", True)
    enabled = bool(enabled_value)
    if isinstance(enabled_value, str):
        enabled = enabled_value.strip().lower() not in {"0", "no", "off", "false"}
    sort_order = _to_int(merged.get("sort_order"), default_sort_order)
    sort_order = max(0, min(9999, sort_order))
    start_kwh = _to_float(merged.get("start_kwh", merged.get("initial_kwh", 0.0)))
    start_kwh = max(0.0, min(999999.0, start_kwh))
    return {
        "consumer_id": consumer_id,
        "name": name[:80],
        "entity_id": entity_id[:160],
        "category": category,
        "icon": icon,
        "color": color.lower(),
        "start_kwh": round(start_kwh, 4),
        "enabled": enabled,
        "sort_order": sort_order,
    }


def valid_consumer_config(config: dict[str, Any]) -> bool:
    return bool(
        config.get("consumer_id") and config.get("name") and config.get("entity_id")
    )


def calculate_billing_period(
    today: date, start_month: Any, start_day: Any
) -> tuple[date, date]:
    month = max(1, min(12, _to_int(start_month, 1) or 1))
    day = max(1, _to_int(start_day, 1) or 1)
    current_year_day = min(day, calendar.monthrange(today.year, month)[1])
    period_start = date(today.year, month, current_year_day)
    if period_start > today:
        previous_year_day = min(day, calendar.monthrange(today.year - 1, month)[1])
        period_start = date(today.year - 1, month, previous_year_day)
    next_year_day = min(day, calendar.monthrange(period_start.year + 1, month)[1])
    period_end = date(period_start.year + 1, month, next_year_day) - timedelta(days=1)
    return (period_start, period_end)


def iter_backfill_dates(start: date, end: date, *, max_days: int = 370) -> list[date]:
    if end < start:
        return []
    max_days = max(1, min(370, int(max_days or 370)))
    effective_start = max(start, end - timedelta(days=max_days - 1))
    day_count = (end - effective_start).days + 1
    return [effective_start + timedelta(days=offset) for offset in range(day_count)]


def build_consumer_atlas_summary(
    configs: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    *,
    home_consumption_kwh: float = 0.0,
    today: date | None = None,
    live_power: dict[str, float] | None = None,
) -> dict[str, Any]:
    today_iso = today.isoformat() if today else None
    live_power = live_power or {}
    consumers_by_id = {}
    for config in configs:
        consumers_by_id[config["consumer_id"]] = {
            **config,
            "period_kwh": max(0.0, _to_float(config.get("start_kwh"))),
            "today_kwh": 0.0,
            "peak_w": 0.0,
            "peak_time": None,
            "last_power_w": live_power.get(config["consumer_id"], 0.0),
            "samples": 0,
        }
    daily_totals = {}
    for row in daily_rows:
        consumer_id = str(row.get("consumer_id") or "")
        if consumer_id not in consumers_by_id:
            continue
        row_date = str(row.get("date") or "")
        energy_kwh = max(0.0, _to_float(row.get("kwh")))
        peak_w = max(0.0, _to_float(row.get("peak_w")))
        samples = max(0, _to_int(row.get("samples")))
        consumers_by_id[consumer_id]["period_kwh"] += energy_kwh
        consumers_by_id[consumer_id]["samples"] += samples
        if today_iso and row_date == today_iso:
            consumers_by_id[consumer_id]["today_kwh"] += energy_kwh
            if consumer_id not in live_power:
                consumers_by_id[consumer_id]["last_power_w"] = max(
                    0.0, _to_float(row.get("last_power_w"))
                )
        if peak_w > consumers_by_id[consumer_id]["peak_w"]:
            consumers_by_id[consumer_id]["peak_w"] = peak_w
            consumers_by_id[consumer_id]["peak_time"] = row.get("peak_time")
        day_total = daily_totals.setdefault(
            row_date, {"date": row_date, "total_known_kwh": 0.0, "consumers": {}}
        )
        day_total["consumers"][consumer_id] = round(
            day_total["consumers"].get(consumer_id, 0.0) + energy_kwh, 4
        )
        day_total["total_known_kwh"] += energy_kwh
        continue
    known_kwh = sum((consumer["period_kwh"] for consumer in consumers_by_id.values()))
    home_kwh = max(0.0, _to_float(home_consumption_kwh))
    unknown_kwh = max(0.0, home_kwh - known_kwh)
    share_basis_kwh = home_kwh if home_kwh > 0 else known_kwh
    consumers = []
    for consumer in consumers_by_id.values():
        share_percent = (
            consumer["period_kwh"] / share_basis_kwh * 100.0
            if share_basis_kwh > 0
            else 0.0
        )
        consumers.append(
            {
                **consumer,
                "period_kwh": round(consumer["period_kwh"], 4),
                "today_kwh": round(consumer["today_kwh"], 4),
                "peak_w": round(consumer["peak_w"], 1),
                "last_power_w": round(consumer["last_power_w"], 1),
                "share_percent": round(share_percent, 1),
            }
        )
    consumers.sort(
        key=lambda item: (-item["period_kwh"], item["sort_order"], item["name"])
    )
    active_power_w = sum((max(0.0, consumer["last_power_w"]) for consumer in consumers))
    top_consumer = consumers[0] if consumers else None
    return {
        "summary": {
            "configured_count": len(configs),
            "enabled_count": sum(
                (1 for consumer in configs if consumer.get("enabled"))
            ),
            "known_kwh": round(known_kwh, 4),
            "home_consumption_kwh": round(home_kwh, 4),
            "unknown_kwh": round(unknown_kwh, 4),
            "active_power_w": round(active_power_w, 1),
            "top_consumer": top_consumer,
        },
        "consumers": consumers,
        "daily": [
            {**row, "total_known_kwh": round(row["total_known_kwh"], 4)}
            for row in sorted(daily_totals.values(), key=lambda item: item["date"])
        ],
    }


def integrate_power_to_kwh(power_w: Any, interval_seconds: int | float) -> float:
    power = max(0.0, _to_float(power_w))
    seconds = max(0.0, _to_float(interval_seconds))
    return power * seconds / 3600000
