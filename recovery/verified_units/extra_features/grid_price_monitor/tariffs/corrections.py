# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Monthly price corrections and undo for CSV or monthly rows."""
from __future__ import annotations
import json
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from ..const import PRICE_SOURCE_CSV, PRICE_SOURCE_GAP_FILL, PRICE_SOURCE_MONTHLY

METHOD_ADDITIVE = "additive"
METHOD_MULTIPLICATIVE = "multiplicative"
WEIGHTING_CONSUMPTION = "consumption"
WEIGHTING_UNIFORM = "uniform"
COVERAGE_THRESHOLD = 50.0
VERIFY_TOLERANCE_CT = 0.005
MAX_COMPLETED_MONTHS = 24
PRICE_SOURCE_CSV_VALUE = PRICE_SOURCE_CSV


class CorrectionError(Exception):

    def __init__(self, key: str, **placeholders: Any) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(slots=True)
class HourPlan:
    timestamp: str
    hour: int
    base_price: float
    weight: float
    corrected: float
    original_source: str
    existed: bool
    raw_to_store: float | None


@dataclass(slots=True)
class CorrectionPlan:
    year: int
    month: int
    month_key: str
    method: str
    requested_method: str
    weighting: str
    requested_weighting: str
    billed_kwh: float
    billed_eur: float
    base_fee_eur: float
    target_price: float
    mean_price: float
    delta: float | None
    factor: float | None
    coverage_percent: float
    energy_deviation_percent: float | None
    hours: list[HourPlan]
    fallback_reason: str | None = None
    energy_warning: bool = False
    original_sources: dict[str, str] = field(default_factory=dict)

    def as_preview(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "month": self.month,
            "month_key": self.month_key,
            "method": self.method,
            "requested_method": self.requested_method,
            "weighting": self.weighting,
            "requested_weighting": self.requested_weighting,
            "billed_kwh": self.billed_kwh,
            "billed_eur": self.billed_eur,
            "base_fee_eur": self.base_fee_eur,
            "target_price": round(self.target_price, 6),
            "mean_price": round(self.mean_price, 6),
            "delta": None if self.delta is None else round(self.delta, 6),
            "factor": None if self.factor is None else round(self.factor, 8),
            "coverage_percent": round(self.coverage_percent, 4),
            "energy_deviation_percent": (
                None
                if self.energy_deviation_percent is None
                else round(self.energy_deviation_percent, 4)
            ),
            "energy_warning": self.energy_warning,
            "fallback_reason": self.fallback_reason,
            "hours": len(self.hours),
            "hours_existing": sum((1 for hour_plan in self.hours if hour_plan.existed)),
            "hours_gap_fill": sum(
                (1 for hour_plan in self.hours if not hour_plan.existed)
            ),
        }


def completed_month_keys(now: datetime, limit: int = MAX_COMPLETED_MONTHS) -> list[str]:
    local_now = now.astimezone() if now.tzinfo else now
    current_year, current_month = (local_now.year, local_now.month)
    month_keys = []
    for month_offset in range(limit):
        current_month -= 1
        if current_month == 0:
            current_month = 12
            current_year -= 1
        month_keys.append(f"{current_year:04d}-{current_month:02d}")
    return month_keys


def month_hours(year: int, month: int, local_tz: tzinfo) -> list[str]:
    range_start = datetime(year, month, 1, tzinfo=local_tz)
    if month == 12:
        range_end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        range_end = datetime(year, month + 1, 1, tzinfo=local_tz)
    hour_stamps = []
    seen_stamps = set()
    utc_hour = range_start.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    utc_end = range_end.astimezone(timezone.utc)
    while utc_hour < utc_end:
        local_hour = utc_hour.astimezone(local_tz)
        stamp = local_hour.replace(
            tzinfo=None, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:00:00")
        if stamp not in seen_stamps:
            seen_stamps.add(stamp)
            hour_stamps.append(stamp)
        utc_hour += timedelta(hours=1)
    return hour_stamps


def default_local_tz(name: str | None = None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


async def plan_month_correction(
    db: Any,
    *,
    year: int,
    month: int,
    billed_kwh: float,
    billed_eur: float,
    base_fee_eur: float = 0.0,
    method: str = METHOD_ADDITIVE,
    weighting: str = WEIGHTING_CONSUMPTION,
    force: bool = False,
    now: datetime,
    local_tz: tzinfo,
    expected_stamps: list[str] | None = None,
) -> CorrectionPlan:
    _validate_inputs(
        year, month, billed_kwh, billed_eur, method, weighting, now, local_tz
    )
    month_key = f"{year:04d}-{month:02d}"
    hour_stamps = expected_stamps or month_hours(year, month, local_tz)
    if not hour_stamps:
        raise CorrectionError("correction_no_hours")
    range_start = hour_stamps[0]
    range_end = datetime(
        year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1
    ).strftime("%Y-%m-%dT00:00:00")
    rows = await db.fetchall(
        "SELECT timestamp, total_price, total_price_raw, price_source, correction_id\n           FROM GPM_price_history\n           WHERE timestamp >= ? AND timestamp < ?\n           ORDER BY timestamp",
        (range_start, range_end),
    )
    rows_by_stamp = {
        row["timestamp"]: row
        for row in rows or []
        if row["timestamp"] in set(hour_stamps)
    }
    coverage_percent = len(rows_by_stamp) / len(hour_stamps) * 100.0
    if not rows_by_stamp:
        raise CorrectionError("correction_no_hours")
    if coverage_percent < COVERAGE_THRESHOLD and (not force):
        raise CorrectionError(
            "correction_low_coverage", coverage=f"{coverage_percent:.1f}"
        )
    base_prices = {}
    resolved_sources = {}
    raw_prices = {}
    for stamp, row in rows_by_stamp.items():
        current_source = str(row["price_source"] or "")
        original_source = await _resolve_original_source(
            db, stamp, current_source, row["correction_id"]
        )
        resolved_sources[stamp] = original_source
        raw_prices[stamp] = (
            None if row["total_price_raw"] is None else float(row["total_price_raw"])
        )
        if current_source == PRICE_SOURCE_CSV_VALUE:
            base_prices[stamp] = float(row["total_price"])
        else:
            raw_price = row["total_price_raw"]
            base_prices[stamp] = float(
                row["total_price"] if raw_price is None else raw_price
            )
    gap_fill_price = sum(base_prices.values()) / len(base_prices)
    for stamp in hour_stamps:
        if stamp in rows_by_stamp:
            continue
        base_prices[stamp] = gap_fill_price
        resolved_sources[stamp] = PRICE_SOURCE_GAP_FILL
        raw_prices[stamp] = gap_fill_price
    hour_weights, applied_weighting, weighting_fallback = await _load_weights(
        db, hour_stamps, weighting
    )
    total_weight = sum((hour_weights[stamp] for stamp in hour_stamps))
    if total_weight <= 0:
        hour_weights = {stamp: 1.0 for stamp in hour_stamps}
        total_weight = float(len(hour_stamps))
        applied_weighting = WEIGHTING_UNIFORM
        weighting_fallback = weighting_fallback or "missing_consumption"
    weighted_mean_price_ct = (
        sum((hour_weights[stamp] * base_prices[stamp] for stamp in hour_stamps))
        / total_weight
    )
    if applied_weighting == WEIGHTING_CONSUMPTION:
        energy_deviation_percent = (total_weight - billed_kwh) / billed_kwh * 100.0
        energy_warning = abs(energy_deviation_percent) > 10.0
    else:
        energy_deviation_percent = None
        energy_warning = False
    target_price_ct = (
        (float(billed_eur) - float(base_fee_eur or 0.0)) * 100.0 / float(billed_kwh)
    )
    if target_price_ct <= 0:
        raise CorrectionError("correction_invalid_billed")
    requested_method = (
        method
        if method in {METHOD_ADDITIVE, METHOD_MULTIPLICATIVE}
        else METHOD_ADDITIVE
    )
    applied_method = requested_method
    fallback_reason = weighting_fallback
    delta_ct = None
    factor = None
    prices_positive = all((base_prices[stamp] > 0 for stamp in hour_stamps))
    if requested_method == METHOD_MULTIPLICATIVE:
        if weighted_mean_price_ct <= 0 or not prices_positive:
            applied_method = METHOD_ADDITIVE
            fallback_reason = "non_positive_prices"
    if applied_method == METHOD_MULTIPLICATIVE:
        factor = target_price_ct / weighted_mean_price_ct
        corrected_prices = {
            stamp: round(base_prices[stamp] * factor, 4) for stamp in hour_stamps
        }
        delta_ct = None
    else:
        delta_ct = target_price_ct - weighted_mean_price_ct
        corrected_prices = {
            stamp: round(base_prices[stamp] + delta_ct, 4) for stamp in hour_stamps
        }
        factor = None
    hour_plans = []
    original_sources = {}
    for stamp in hour_stamps:
        existed = stamp in rows_by_stamp
        hour_original_source = resolved_sources[stamp]
        original_sources[stamp] = hour_original_source
        existing_raw_price = raw_prices.get(stamp)
        if (
            existed
            and existing_raw_price is not None
            and (hour_original_source != PRICE_SOURCE_CSV_VALUE)
        ):
            raw_to_store = existing_raw_price
        else:
            raw_to_store = base_prices[stamp]
        hour_plans.append(
            HourPlan(
                timestamp=stamp,
                hour=int(stamp[11:13]),
                base_price=base_prices[stamp],
                weight=hour_weights[stamp],
                corrected=corrected_prices[stamp],
                original_source=hour_original_source,
                existed=existed,
                raw_to_store=raw_to_store,
            )
        )
    achieved_price_ct = (
        sum((hour_plan.weight * hour_plan.corrected for hour_plan in hour_plans))
        / total_weight
    )
    if abs(achieved_price_ct - target_price_ct) >= VERIFY_TOLERANCE_CT:
        raise CorrectionError(
            "correction_verify_failed",
            deviation=f"{abs(achieved_price_ct - target_price_ct):.4f}",
        )
    return CorrectionPlan(
        year=year,
        month=month,
        month_key=month_key,
        method=applied_method,
        requested_method=requested_method,
        weighting=applied_weighting,
        requested_weighting=weighting,
        billed_kwh=float(billed_kwh),
        billed_eur=float(billed_eur),
        base_fee_eur=float(base_fee_eur or 0.0),
        target_price=target_price_ct,
        mean_price=weighted_mean_price_ct,
        delta=delta_ct,
        factor=factor,
        coverage_percent=coverage_percent,
        energy_deviation_percent=energy_deviation_percent,
        hours=hour_plans,
        fallback_reason=fallback_reason,
        energy_warning=energy_warning,
        original_sources=original_sources,
    )


async def apply_month_correction(
    db: Any, plan: CorrectionPlan, *, stats_store: Any | None, now: datetime
) -> dict[str, Any]:
    await db.require_price_write_schema()
    applied_at = now.astimezone().replace(microsecond=0).isoformat()
    first_stamp = plan.hours[0].timestamp
    last_stamp = plan.hours[-1].timestamp
    params = {
        "year": plan.year,
        "month": plan.month,
        "billed_kwh": plan.billed_kwh,
        "billed_eur": plan.billed_eur,
        "base_fee_eur": plan.base_fee_eur,
        "method": plan.requested_method,
        "weighting": plan.requested_weighting,
    }
    preview = plan.as_preview()
    await db.execute(
        "INSERT INTO GPM_price_corrections\n           (kind, status, created_at, applied_at, range_from, range_to, month,\n            params_json, summary_json, rows_affected, source_hash)\n           VALUES ('monthly', 'applied', ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
        (
            applied_at,
            applied_at,
            first_stamp,
            last_stamp,
            plan.month_key,
            json.dumps(params, sort_keys=True),
            json.dumps(preview, sort_keys=True),
        ),
        auto_commit=False,
    )
    id_row = await db.fetchone("SELECT last_insert_rowid() AS id")
    correction_id = int(id_row["id"])
    snapshot = await _snapshot_hours(
        db, [hour_plan.timestamp for hour_plan in plan.hours]
    )
    plans_by_day = {}
    for hour_plan in plan.hours:
        plans_by_day.setdefault(hour_plan.timestamp[:10], []).append(hour_plan)
    try:
        for day, day_plans in sorted(plans_by_day.items()):
            for hour_plan in day_plans:
                if hour_plan.existed:
                    await db.execute(
                        "UPDATE GPM_price_history\n                           SET total_price_raw = ?,\n                               total_price = ?,\n                               price_net = ?,\n                               hour = ?,\n                               price_source = ?,\n                               correction_id = ?\n                           WHERE timestamp = ?",
                        (
                            hour_plan.raw_to_store,
                            hour_plan.corrected,
                            hour_plan.corrected,
                            hour_plan.hour,
                            PRICE_SOURCE_MONTHLY,
                            correction_id,
                            hour_plan.timestamp,
                        ),
                        auto_commit=False,
                    )
                else:
                    await db.execute(
                        "INSERT INTO GPM_price_history\n                           (timestamp, price_net, total_price, hour, price_source,\n                            total_price_raw, correction_id)\n                           VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            hour_plan.timestamp,
                            hour_plan.corrected,
                            hour_plan.corrected,
                            hour_plan.hour,
                            PRICE_SOURCE_MONTHLY,
                            hour_plan.raw_to_store,
                            correction_id,
                        ),
                        auto_commit=False,
                    )
            await db.commit()
            await _rebuild_daily_average(db, stats_store, day)
            continue
        await db.execute(
            "UPDATE GPM_price_corrections\n               SET status = 'stale'\n               WHERE kind = 'monthly' AND status = 'applied'\n                 AND month = ? AND id != ?",
            (plan.month_key, correction_id),
            auto_commit=False,
        )
        revision = await _bump_revision(db, auto_commit=False)
        summary = {
            **preview,
            "revision": revision,
            "mean_price": plan.mean_price,
            "target_price": plan.target_price,
            "delta": plan.delta,
            "factor": plan.factor,
            "coverage_percent": plan.coverage_percent,
            "energy_deviation_percent": plan.energy_deviation_percent,
            "hours": len(plan.hours),
            "original_sources": plan.original_sources,
            "inserted_stamps": [
                hour_plan.timestamp for hour_plan in plan.hours if not hour_plan.existed
            ],
            "P_bar": plan.mean_price,
            "P_star": plan.target_price,
            "d": plan.delta,
            "k": plan.factor,
        }
        await db.execute(
            "UPDATE GPM_price_corrections\n               SET rows_affected = ?, summary_json = ?\n               WHERE id = ?",
            (len(plan.hours), json.dumps(summary, sort_keys=True), correction_id),
            auto_commit=False,
        )
        await db.commit()
        await _verify_written(db, plan)
    except Exception:
        await _restore_snapshot(db, snapshot)
        await db.execute(
            "DELETE FROM GPM_price_corrections WHERE id = ?", (correction_id,)
        )
        raise
    return {
        "correction_id": correction_id,
        "rows_affected": len(plan.hours),
        "revision": revision,
        "from": first_stamp[:10],
        "to": last_stamp[:10],
        "reason": "monthly",
        "applied_at": applied_at,
        "month": plan.month_key,
        **preview,
        "achieved_price": round(
            sum((hour_plan.weight * hour_plan.corrected for hour_plan in plan.hours))
            / sum((hour_plan.weight for hour_plan in plan.hours)),
            6,
        ),
        "deviation_ct": round(
            abs(
                sum(
                    (hour_plan.weight * hour_plan.corrected for hour_plan in plan.hours)
                )
                / sum((hour_plan.weight for hour_plan in plan.hours))
                - plan.target_price
            ),
            6,
        ),
    }


async def revert_correction(
    db: Any, correction_id: int, *, stats_store: Any | None, now: datetime
) -> dict[str, Any]:
    await db.require_price_write_schema()
    row = await db.fetchone(
        "SELECT id, kind, status, month, range_from, range_to, summary_json\n           FROM GPM_price_corrections WHERE id = ?",
        (correction_id,),
    )
    if row is None:
        raise CorrectionError("correction_not_found")
    if row["status"] != "applied":
        raise CorrectionError("correction_not_applied")
    summary = {}
    if row["summary_json"]:
        try:
            decoded_summary = json.loads(row["summary_json"])
            if isinstance(decoded_summary, dict):
                summary = decoded_summary
        except json.JSONDecodeError:
            summary = {}
    original_sources = summary.get("original_sources") or {}
    inserted_stamps = {str(stamp) for stamp in summary.get("inserted_stamps") or []}
    price_rows = await db.fetchall(
        "SELECT timestamp, total_price_raw, hour FROM GPM_price_history\n           WHERE correction_id = ?",
        (correction_id,),
    )
    if not price_rows:
        raise CorrectionError("correction_not_found")
    reverted_at = now.astimezone().replace(microsecond=0).isoformat()
    affected_days = set()
    for price_row in price_rows:
        stamp = price_row["timestamp"]
        original_source = original_sources.get(stamp) or original_sources.get(
            str(stamp)
        )
        inserted_gap = (
            stamp in inserted_stamps or original_source == PRICE_SOURCE_GAP_FILL
        )
        if inserted_gap:
            await db.execute(
                "DELETE FROM GPM_price_history WHERE timestamp = ?",
                (stamp,),
                auto_commit=False,
            )
            affected_days.add(stamp[:10])
            continue
        raw_price = price_row["total_price_raw"]
        if not raw_price is not None:
            continue
        await db.execute(
            "UPDATE GPM_price_history\n               SET total_price = ?,\n                   price_net = ?,\n                   hour = ?,\n                   price_source = ?,\n                   correction_id = NULL\n               WHERE timestamp = ?",
            (
                float(raw_price),
                float(raw_price),
                price_row["hour"],
                original_source,
                stamp,
            ),
            auto_commit=False,
        )
        affected_days.add(stamp[:10])
    await db.execute(
        "UPDATE GPM_price_corrections\n           SET status = 'reverted', reverted_at = ?\n           WHERE id = ?",
        (reverted_at, correction_id),
        auto_commit=False,
    )
    revision = await _bump_revision(db, auto_commit=False)
    await db.commit()
    for day in sorted(affected_days):
        await _rebuild_daily_average(db, stats_store, day)
    return {
        "correction_id": correction_id,
        "kind": row["kind"],
        "month": row["month"],
        "rows_affected": len(price_rows),
        "revision": revision,
        "reason": "revert",
        "reverted_at": reverted_at,
        "from": str(row["range_from"])[:10],
        "to": str(row["range_to"])[:10],
    }


async def list_corrections(db: Any, limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, kind, status, month, applied_at, reverted_at, rows_affected\n           FROM GPM_price_corrections\n           ORDER BY id DESC\n           LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows or []]


def _validate_inputs(
    year: int,
    month: int,
    billed_kwh: float,
    billed_eur: float,
    method: str,
    weighting: str,
    now: datetime,
    local_tz: tzinfo,
) -> None:
    if month not in range(1, 13) or year < 2000:
        raise CorrectionError("correction_invalid_billed")
    if float(billed_kwh) <= 0 or float(billed_eur) <= 0:
        raise CorrectionError("correction_invalid_billed")
    if method not in {METHOD_ADDITIVE, METHOD_MULTIPLICATIVE}:
        raise CorrectionError("correction_invalid_billed")
    if weighting not in {WEIGHTING_CONSUMPTION, WEIGHTING_UNIFORM}:
        raise CorrectionError("correction_invalid_billed")
    local_now = now.astimezone(local_tz) if now.tzinfo else now.replace(tzinfo=local_tz)
    current_month_key = f"{local_now.year:04d}-{local_now.month:02d}"
    month_key = f"{year:04d}-{month:02d}"
    if month_key >= current_month_key:
        raise CorrectionError("correction_month_current")
    allowed_months = set(completed_month_keys(local_now, MAX_COMPLETED_MONTHS))
    if month_key not in allowed_months:
        raise CorrectionError("correction_month_too_old")
    monthrange(year, month)


async def _load_weights(
    db: Any, stamps: list[str], weighting: str
) -> tuple[dict[str, float], str, str | None]:
    uniform_weights = {stamp: 1.0 for stamp in stamps}
    if weighting != WEIGHTING_CONSUMPTION:
        return (uniform_weights, WEIGHTING_UNIFORM, None)
    try:
        billing_table = await db.fetchone(
            "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name='stats_hourly_billing'"
        )
    except Exception:
        billing_table = None
    if billing_table is None:
        return (uniform_weights, WEIGHTING_UNIFORM, "missing_consumption")
    hour_keys = [stamp[:16] for stamp in stamps]
    placeholders = ",".join(("?" for placeholder_index in hour_keys))
    try:
        rows = await db.fetchall(
            f"SELECT hour_key, grid_import_kwh FROM stats_hourly_billing\n                WHERE hour_key IN ({placeholders})",
            tuple(hour_keys),
        )
    except Exception:
        return (uniform_weights, WEIGHTING_UNIFORM, "missing_consumption")
    consumption_by_hour = {
        row["hour_key"]: float(row["grid_import_kwh"] or 0.0) for row in rows or []
    }
    if not consumption_by_hour:
        return (uniform_weights, WEIGHTING_UNIFORM, "missing_consumption")
    covered_hours = sum((1 for stamp in stamps if stamp[:16] in consumption_by_hour))
    consumption_coverage = covered_hours / len(stamps) * 100.0 if stamps else 0.0
    if consumption_coverage < COVERAGE_THRESHOLD:
        return (uniform_weights, WEIGHTING_UNIFORM, "missing_consumption")
    hour_weights = {
        stamp: float(consumption_by_hour.get(stamp[:16], 0.0)) for stamp in stamps
    }
    if sum(hour_weights.values()) <= 0:
        return (uniform_weights, WEIGHTING_UNIFORM, "missing_consumption")
    return (hour_weights, WEIGHTING_CONSUMPTION, None)


async def _snapshot_hours(
    db: Any, stamps: list[str]
) -> dict[str, dict[str, Any] | None]:
    placeholders = ",".join(("?" for placeholder_index in stamps))
    rows = await db.fetchall(
        f"SELECT timestamp, total_price, total_price_raw, price_net, hour,\n                  price_source, correction_id\n            FROM GPM_price_history WHERE timestamp IN ({placeholders})",
        tuple(stamps),
    )
    rows_by_stamp = {row["timestamp"]: dict(row) for row in rows or []}
    return {stamp: rows_by_stamp.get(stamp) for stamp in stamps}


async def _restore_snapshot(
    db: Any, snapshot: Mapping[str, dict[str, Any] | None]
) -> None:
    try:
        await db.rollback()
    except Exception:
        pass
    for stamp, row in snapshot.items():
        if row is None:
            await db.execute(
                "DELETE FROM GPM_price_history WHERE timestamp = ?",
                (stamp,),
                auto_commit=False,
            )
            continue
        await db.execute(
            "UPDATE GPM_price_history\n               SET total_price = ?, total_price_raw = ?, price_net = ?, hour = ?,\n                   price_source = ?, correction_id = ?\n               WHERE timestamp = ?",
            (
                row["total_price"],
                row["total_price_raw"],
                row["price_net"],
                row["hour"],
                row["price_source"],
                row["correction_id"],
                stamp,
            ),
            auto_commit=False,
        )
    await db.commit()


async def _rebuild_daily_average(db: Any, stats_store: Any | None, day: str) -> None:
    day_start = f"{day}T00:00:00"
    day_end = (datetime.fromisoformat(day) + timedelta(days=1)).strftime(
        "%Y-%m-%dT00:00:00"
    )
    averages = await db.fetchone(
        "SELECT AVG(price_net) AS average_net,\n                  AVG(total_price) AS average_total,\n                  MIN(total_price) AS min_price,\n                  MAX(total_price) AS max_price\n           FROM GPM_price_history\n           WHERE timestamp >= ? AND timestamp < ?",
        (day_start, day_end),
    )
    if averages:
        if averages["average_total"] is not None and stats_store is not None:
            await stats_store.async_update_daily_average(
                date=day,
                average_net=float(averages["average_net"]),
                average_total=float(averages["average_total"]),
                min_price=(
                    None
                    if averages["min_price"] is None
                    else float(averages["min_price"])
                ),
                max_price=(
                    None
                    if averages["max_price"] is None
                    else float(averages["max_price"])
                ),
            )
    return None if True else None


async def _bump_revision(db: Any, *, auto_commit: bool) -> int:
    revision_row = await db.fetchone(
        "SELECT price_revision FROM GPM_price_cache_meta WHERE id = 1"
    )
    revision = (
        0
        if revision_row is None or revision_row["price_revision"] is None
        else int(revision_row["price_revision"])
    )
    revision += 1
    await db.execute(
        "INSERT INTO GPM_price_cache_meta (id, price_revision)\n           VALUES (1, ?)\n           ON CONFLICT(id) DO UPDATE SET price_revision = excluded.price_revision",
        (revision,),
        auto_commit=auto_commit,
    )
    return revision


async def _resolve_original_source(
    db: Any, stamp: str, current_source: str, correction_id: Any
) -> str:
    if current_source in {PRICE_SOURCE_CSV_VALUE, PRICE_SOURCE_GAP_FILL}:
        return current_source
    if correction_id is None:
        return current_source
    try:
        ancestor_id = int(correction_id)
    except (TypeError, ValueError):
        return current_source
    seen_correction_ids = set()
    resolved_source = current_source
    while ancestor_id and ancestor_id not in seen_correction_ids:
        seen_correction_ids.add(ancestor_id)
        row = await db.fetchone(
            "SELECT id, kind, month, summary_json\n               FROM GPM_price_corrections WHERE id = ?",
            (ancestor_id,),
        )
        if row is None:
            break
        summary = {}
        if row["summary_json"]:
            try:
                decoded_summary = json.loads(row["summary_json"])
                if isinstance(decoded_summary, dict):
                    summary = decoded_summary
            except json.JSONDecodeError:
                summary = {}
        resolved_sources = summary.get("original_sources") or {}
        source_value = resolved_sources.get(stamp) or resolved_sources.get(str(stamp))
        if not source_value:
            break
        source_name = str(source_value)
        if source_name != PRICE_SOURCE_MONTHLY:
            return source_name
        resolved_source = source_name
        previous_correction = await db.fetchone(
            "SELECT id FROM GPM_price_corrections\n               WHERE kind = 'monthly' AND month = ? AND id < ?\n               ORDER BY id DESC LIMIT 1",
            (row["month"], ancestor_id),
        )
        if previous_correction is None:
            break
        ancestor_id = int(previous_correction["id"])
    return resolved_source if True else None


async def _verify_written(db: Any, plan: CorrectionPlan) -> None:
    hour_stamps = [hour_plan.timestamp for hour_plan in plan.hours]
    placeholders = ",".join(("?" for placeholder_index in hour_stamps))
    rows = await db.fetchall(
        f"SELECT timestamp, total_price FROM GPM_price_history WHERE timestamp IN ({placeholders})",
        tuple(hour_stamps),
    )
    written_prices = {row["timestamp"]: float(row["total_price"]) for row in rows or []}
    total_weight = sum((hour_plan.weight for hour_plan in plan.hours))
    achieved_price_ct = (
        sum(
            (
                hour_plan.weight * written_prices[hour_plan.timestamp]
                for hour_plan in plan.hours
            )
        )
        / total_weight
    )
    if abs(achieved_price_ct - plan.target_price) >= VERIFY_TOLERANCE_CT:
        raise CorrectionError(
            "correction_verify_failed",
            deviation=f"{abs(achieved_price_ct - plan.target_price):.4f}",
        )
