# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-time daily cost rows: hourly, else daily kWh times GPM average, else kWh only."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

SOURCE_HOURLY = "hourly"
SOURCE_DAILY_AVG = "daily_avg"
SOURCE_KWH_ONLY = "kwh_only"
_HOURLY_TABLE = "stats_hourly_billing"
_DAILY_ENERGY_TABLE = "stats_daily_energy"
_GPM_AVERAGES_TABLE = "GPM_daily_averages"


def _parse_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    day_text = str(value or "").strip()
    if len(day_text) < 10:
        return
    try:
        return date.fromisoformat(day_text[:10])
    except ValueError:
        return


def _priced(price_ct: Any) -> bool:
    try:
        return float(price_ct) != 0.0
    except (TypeError, ValueError):
        return False


def _cell(row: Any, index: int, key: str) -> Any:
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            return row[index]
    return row[index]


def merge_daily_costs(
    start: date,
    end_exclusive: date,
    hourly_by_day: Mapping[date, Mapping[str, Any]],
    daily_kwh: Mapping[date, float],
    daily_avg_ct: Mapping[date, float],
) -> list[dict[str, Any]]:
    """One row per calendar day in [start, end_exclusive) with the K1 fallback chain."""
    cost_rows = []
    for day in sorted(set(hourly_by_day) | set(daily_kwh)):
        if day < start or day >= end_exclusive:
            continue
        hourly_summary = hourly_by_day.get(day)
        hours_with_data = 0
        if hourly_summary is not None:
            try:
                hours_with_data = int(hourly_summary.get("hours") or 0)
            except (TypeError, ValueError):
                hours_with_data = 0
        if hours_with_data >= 1:
            try:
                grid_kwh = float(hourly_summary.get("kwh") or 0.0)
            except (TypeError, ValueError):
                grid_kwh = 0.0
            try:
                energy_eur = float(hourly_summary.get("energy_eur") or 0.0)
            except (TypeError, ValueError):
                energy_eur = 0.0
            try:
                priced_kwh = float(hourly_summary.get("priced_kwh") or 0.0)
            except (TypeError, ValueError):
                priced_kwh = 0.0
            cost_rows.append(
                {
                    "date": day.isoformat(),
                    "grid_kwh": grid_kwh,
                    "energy_eur": energy_eur,
                    "source": SOURCE_HOURLY,
                    "hours_with_data": hours_with_data,
                    "priced_kwh": priced_kwh,
                }
            )
            continue
        if day not in daily_kwh:
            continue
        try:
            grid_kwh = float(daily_kwh[day] or 0.0)
        except (TypeError, ValueError):
            grid_kwh = 0.0
        average_price_ct = daily_avg_ct.get(day)
        if average_price_ct is not None:
            if _priced(average_price_ct):
                energy_eur = grid_kwh * float(average_price_ct) / 100.0
                cost_rows.append(
                    {
                        "date": day.isoformat(),
                        "grid_kwh": grid_kwh,
                        "energy_eur": energy_eur,
                        "source": SOURCE_DAILY_AVG,
                        "hours_with_data": 0,
                        "priced_kwh": grid_kwh,
                    }
                )
                continue
        cost_rows.append(
            {
                "date": day.isoformat(),
                "grid_kwh": grid_kwh,
                "energy_eur": None,
                "source": SOURCE_KWH_ONLY,
                "hours_with_data": 0,
                "priced_kwh": 0.0,
            }
        )
    return cost_rows


def daily_costs_from_hourly_rows(
    hourly_rows: Sequence[Mapping[str, Any]], start: date, end_exclusive: date
) -> list[dict[str, Any]]:
    """Group raw hourly billing rows into daily cost rows (source hourly only)."""
    hourly_by_day = {}
    seen_hours = set()
    for row in hourly_rows:
        day = _parse_day(row.get("date"))
        if day is None or day < start or day >= end_exclusive:
            continue
        try:
            hour = int(row.get("hour"))
        except (TypeError, ValueError):
            hour = 0
        date_hour_key = (day.isoformat(), hour)
        if date_hour_key in seen_hours:
            continue
        seen_hours.add(date_hour_key)
        day_summary = hourly_by_day.setdefault(
            day, {"kwh": 0.0, "energy_eur": 0.0, "hours": 0, "priced_kwh": 0.0}
        )
        day_summary["hours"] += 1
        try:
            grid_import_kwh = float(row.get("grid_import_kwh") or 0.0)
        except (TypeError, ValueError):
            grid_import_kwh = 0.0
        day_summary["kwh"] += grid_import_kwh
        price_ct = row.get("price_ct_kwh")
        if not _priced(price_ct):
            continue
        day_summary["priced_kwh"] += grid_import_kwh
        day_summary["energy_eur"] += grid_import_kwh * float(price_ct) / 100.0
    return merge_daily_costs(start, end_exclusive, hourly_by_day, {}, {})


async def _fetch_all(db: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
    try:
        async with db.execute(sql, params) as cursor:
            return list(await cursor.fetchall())
    except Exception:
        return []
    return None if True else None


async def _apply_grid_import_corrections(
    db: Any,
    start: date,
    end_exclusive: date,
    hourly_by_day: dict[date, dict[str, Any]],
    daily_kwh: dict[date, float],
    daily_avg_ct: dict[date, float],
    projection: dict[str, dict[str, float]] | None,
) -> None:
    """Scale read-time import kWh and euros. Standalone imports keep raw rows."""
    try:
        from .corrected_energy import (
            RAW_COST_KWH_MIN as RAW_COST_KWH_MIN,
            apply_grid_import_factor as apply_grid_import_factor,
            corrected_day_cost as corrected_day_cost,
            grid_import_factors as grid_import_factors,
            projection_from_runtime as projection_from_runtime,
            scaling_basis_kwh as scaling_basis_kwh,
        )
    except ImportError:
        return
    if projection is None:
        projection = await projection_from_runtime(start.isoformat())
    last_day = start
    if end_exclusive > start:
        last_day = end_exclusive - timedelta(days=1)
    import_factors = await grid_import_factors(
        db, start.isoformat(), last_day.isoformat(), projection
    )
    for date_key, (raw_day_kwh, corrected_day_kwh) in import_factors.items():
        day = _parse_day(date_key)
        if day is None:
            continue
        day_summary = hourly_by_day.get(day)
        has_correction = "grid_import_day_kwh" in (projection or {}).get(date_key, {})
        hourly_sum_kwh = (
            float(day_summary.get("kwh") or 0.0) if day_summary is not None else None
        )
        cost_scaling_basis_kwh = scaling_basis_kwh(
            raw_day_kwh, hourly_sum_kwh, corrected=has_correction
        )
        if (
            day_summary is not None
            and cost_scaling_basis_kwh > 0.0
            and (raw_day_kwh > 0.0 or cost_scaling_basis_kwh != raw_day_kwh)
        ):
            grid_kwh, energy_eur, priced_kwh = apply_grid_import_factor(
                raw_day_kwh,
                corrected_day_kwh,
                float(day_summary.get("kwh") or 0.0),
                float(day_summary.get("energy_eur") or 0.0),
                float(day_summary.get("priced_kwh") or 0.0),
                hour_sum=hourly_sum_kwh,
                corrected_day=has_correction,
            )
            day_summary["kwh"] = grid_kwh
            day_summary["energy_eur"] = energy_eur
            day_summary["priced_kwh"] = priced_kwh
        elif (
            day_summary is not None and corrected_day_kwh > 0.0 and (raw_day_kwh <= 0.0)
        ):
            existing_hourly_kwh = float(day_summary.get("kwh") or 0.0)
            if existing_hourly_kwh > 0.0:
                scale_factor = corrected_day_kwh / existing_hourly_kwh
                day_summary["kwh"] = corrected_day_kwh
                existing_energy_eur = float(day_summary.get("energy_eur") or 0.0)
                if existing_hourly_kwh >= RAW_COST_KWH_MIN:
                    day_summary["energy_eur"] = corrected_day_cost(
                        existing_hourly_kwh, corrected_day_kwh, existing_energy_eur
                    )
                else:
                    day_summary["energy_eur"] = existing_energy_eur * scale_factor
                day_summary["priced_kwh"] = (
                    float(day_summary.get("priced_kwh") or 0.0) * scale_factor
                )
            else:
                day_summary["kwh"] = corrected_day_kwh
                average_price_ct = daily_avg_ct.get(day)
                fallback_price_ct = (
                    float(average_price_ct)
                    if average_price_ct is not None and _priced(average_price_ct)
                    else None
                )
                day_summary["energy_eur"] = corrected_day_cost(
                    0.0, corrected_day_kwh, 0.0, fallback_price_ct=fallback_price_ct
                )
                if fallback_price_ct is not None:
                    day_summary["priced_kwh"] = corrected_day_kwh
        elif day_summary is None:
            if day in daily_kwh or corrected_day_kwh != raw_day_kwh:
                daily_kwh[day] = corrected_day_kwh
        if day in daily_kwh or corrected_day_kwh != raw_day_kwh:
            daily_kwh[day] = corrected_day_kwh
            continue


async def load_daily_costs(
    db: Any,
    start: date,
    end_exclusive: date,
    projection: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Load one merged daily cost row per calendar day (K1). One query per source."""
    start_iso = start.isoformat()
    end_iso = end_exclusive.isoformat()
    query_parameters = (start_iso, end_iso)
    hourly_by_day = {}
    for row in await _fetch_all(
        db,
        f"\n        SELECT date,\n               COUNT(DISTINCT hour) AS hours,\n               SUM(COALESCE(grid_import_kwh, 0)) AS kwh,\n               SUM(\n                   CASE\n                       WHEN price_ct_kwh IS NOT NULL\n                            AND CAST(price_ct_kwh AS REAL) != 0\n                       THEN COALESCE(grid_import_kwh, 0)\n                       ELSE 0\n                   END\n               ) AS priced_kwh,\n               SUM(\n                   CASE\n                       WHEN price_ct_kwh IS NOT NULL\n                            AND CAST(price_ct_kwh AS REAL) != 0\n                       THEN COALESCE(grid_import_kwh, 0)\n                            * CAST(price_ct_kwh AS REAL) / 100.0\n                       ELSE 0\n                   END\n               ) AS energy_eur\n        FROM {_HOURLY_TABLE}\n        WHERE date >= ? AND date < ?\n        GROUP BY date\n        ",
        query_parameters,
    ):
        day = _parse_day(_cell(row, 0, "date"))
        if day is None:
            continue
        try:
            hours_with_data = int(_cell(row, 1, "hours") or 0)
        except (TypeError, ValueError):
            hours_with_data = 0
        try:
            grid_kwh = float(_cell(row, 2, "kwh") or 0.0)
        except (TypeError, ValueError):
            grid_kwh = 0.0
        try:
            priced_kwh = float(_cell(row, 3, "priced_kwh") or 0.0)
        except (TypeError, ValueError):
            priced_kwh = 0.0
        try:
            energy_eur = float(_cell(row, 4, "energy_eur") or 0.0)
        except (TypeError, ValueError):
            energy_eur = 0.0
        hourly_by_day[day] = {
            "hours": hours_with_data,
            "kwh": grid_kwh,
            "priced_kwh": priced_kwh,
            "energy_eur": energy_eur,
        }
    daily_kwh = {}
    for row in await _fetch_all(
        db,
        f"\n        SELECT date, grid_import_kwh\n        FROM {_DAILY_ENERGY_TABLE}\n        WHERE date >= ? AND date < ?\n        ",
        query_parameters,
    ):
        day = _parse_day(_cell(row, 0, "date"))
        if day is None:
            continue
        try:
            daily_kwh[day] = float(_cell(row, 1, "grid_import_kwh") or 0.0)
        except (TypeError, ValueError):
            daily_kwh[day] = 0.0
            continue
    daily_avg_ct = {}
    for row in await _fetch_all(
        db,
        f"\n        SELECT date, average_total\n        FROM {_GPM_AVERAGES_TABLE}\n        WHERE date >= ? AND date < ?\n        ",
        query_parameters,
    ):
        day = _parse_day(_cell(row, 0, "date"))
        if not day is not None:
            continue
        average_price_ct = _cell(row, 1, "average_total")
        if average_price_ct is None or not _priced(average_price_ct):
            continue
        daily_avg_ct[day] = float(average_price_ct)
    await _apply_grid_import_corrections(
        db, start, end_exclusive, hourly_by_day, daily_kwh, daily_avg_ct, projection
    )
    return merge_daily_costs(
        start, end_exclusive, hourly_by_day, daily_kwh, daily_avg_ct
    )


def _dominant_count(counts: Mapping[str, int], allowed: Sequence[str]) -> str | None:
    dominant_source = None
    largest_count = 0
    for source in allowed:
        try:
            count = int(counts.get(source) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > largest_count:
            dominant_source = source
            largest_count = count
    return dominant_source


def provenance_from_counts(
    cost_sources: Sequence[str], hour_counts: Mapping[str, int], *, corrected: bool
) -> dict[str, Any]:
    """Additive origin flags. Codes stay stable; labels live in the frontend."""
    cost_counts = {}
    for cost_source in cost_sources:
        if cost_source not in (SOURCE_HOURLY, SOURCE_DAILY_AVG, SOURCE_KWH_ONLY):
            continue
        cost_counts[cost_source] = cost_counts.get(cost_source, 0) + 1
    return {
        "cost_source": _dominant_count(
            cost_counts, (SOURCE_HOURLY, SOURCE_DAILY_AVG, SOURCE_KWH_ONLY)
        ),
        "hour_source": _dominant_count(hour_counts, ("watt_recorder", "aggregator")),
        "corrected": bool(corrected),
    }


async def load_provenance(
    db: Any,
    start: date,
    end_exclusive: date,
    projection: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    """Cost source, hour source, and correction flag for one date window."""
    cost_sources = []
    try:
        cost_rows = await load_daily_costs(db, start, end_exclusive, projection)
        cost_sources = [str(row.get("source") or "") for row in cost_rows]
    except Exception:
        cost_sources = []
    hour_counts = {}
    try:
        async with db.execute(
            "SELECT data_source, COUNT(*)\n               FROM stats_hourly_billing\n               WHERE date >= ? AND date < ?\n               GROUP BY data_source",
            (start.isoformat(), end_exclusive.isoformat()),
        ) as cursor:
            for row in await cursor.fetchall():
                hour_counts[str(row[0] or "")] = int(row[1] or 0)
    except Exception:
        hour_counts = {}
    has_correction = False
    start_iso = start.isoformat()
    end_iso = end_exclusive.isoformat()
    for projection_date, day_projection in (projection or {}).items():
        day_text = str(projection_date)
        if start_iso <= day_text < end_iso and day_projection:
            has_correction = True
            break
    return provenance_from_counts(cost_sources, hour_counts, corrected=has_correction)
