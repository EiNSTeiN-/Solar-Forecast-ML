# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Calendar-year history: invoices allocated by day, measurement fills gaps (v2)."""
from __future__ import annotations
import calendar
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from ..api.gpm_monthly import iter_month_slices, prorate_base_fee
from .corrected_energy import projection_from_runtime
from .daily_costs import (
    SOURCE_DAILY_AVG,
    SOURCE_HOURLY,
    SOURCE_KWH_ONLY,
    daily_costs_from_hourly_rows,
    load_daily_costs,
)
from .invoice_archive import list_invoices

_HOURLY_TABLE = "stats_hourly_billing"
_DAILY_ENERGY_TABLE = "stats_daily_energy"
_KWH_DIGITS = 3
_MONEY_DIGITS = 2
_CT_DIGITS = 2


def _round_money(value: float) -> float:
    return round(float(value), _MONEY_DIGITS)


def _round_kwh(value: float) -> float:
    return round(float(value), _KWH_DIGITS)


def _round_ct(value: float) -> float:
    return round(float(value), _CT_DIGITS)


def _parse_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_text = str(value or "").strip()
    if len(date_text) < 10:
        return
    try:
        return date.fromisoformat(date_text[:10])
    except ValueError:
        return


def _invoice_window(invoice: Mapping[str, Any]) -> tuple[date, date] | None:
    period_start = _parse_day(invoice.get("period_from"))
    period_end = _parse_day(invoice.get("period_to"))
    if period_start is None or period_end is None or period_start > period_end:
        return None
    return (period_start, period_end)


def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def _overlap_days(start: date, end: date, year: int) -> int:
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    overlap_start = max(start, year_start)
    overlap_end = min(end, year_end)
    if overlap_start > overlap_end:
        return 0
    return (overlap_end - overlap_start).days + 1


def _invoice_label(start: date, end: date) -> str:
    return f"{start.month:02d}/{start.year}–{end.month:02d}/{end.year}"


def _collect_days(start: date, end: date) -> set[date]:
    day_set = set()
    day = start
    while day <= end:
        day_set.add(day)
        day += timedelta(days=1)
    return day_set


def year_span_days(year: int, today: date) -> set[date]:
    period_start = date(year, 1, 1)
    period_end = min(date(year, 12, 31), today)
    if period_start > period_end:
        return set()
    return _collect_days(period_start, period_end)


def complement_days(year: int, invoice_days: set[date], today: date) -> set[date]:
    """Calendar days in the year through today that no invoice covers."""
    return year_span_days(year, today) - set(invoice_days)


def slice_invoice_by_year(invoice: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Allocate one invoice onto calendar years (V2, remainder on last slice)."""
    invoice_window = _invoice_window(invoice)
    if invoice_window is None:
        return {}
    period_start, period_end = invoice_window
    total_days = (period_end - period_start).days + 1
    if total_days <= 0:
        return {}
    try:
        grid_kwh = float(invoice.get("grid_kwh") or 0.0)
        total_eur = float(invoice.get("total_eur") or 0.0)
    except (TypeError, ValueError):
        return {}
    year_slices = [
        (year, _overlap_days(period_start, period_end, year))
        for year in range(period_start.year, period_end.year + 1)
    ]
    year_slices = [
        (year, overlap_days) for year, overlap_days in year_slices if overlap_days > 0
    ]
    if not year_slices:
        return {}
    allocated_kwh = 0.0
    allocated_eur = 0.0
    allocations_by_year = {}
    last_slice_index = len(year_slices) - 1
    for slice_index, (year, overlap_days) in enumerate(year_slices):
        if slice_index == last_slice_index:
            slice_kwh = _round_kwh(grid_kwh - allocated_kwh)
            slice_eur = _round_money(total_eur - allocated_eur)
        else:
            day_fraction = overlap_days / total_days
            slice_kwh = _round_kwh(grid_kwh * day_fraction)
            slice_eur = _round_money(total_eur * day_fraction)
            allocated_kwh += slice_kwh
            allocated_eur += slice_eur
        allocations_by_year[year] = {
            "days": overlap_days,
            "grid_kwh": slice_kwh,
            "total_eur": slice_eur,
        }
    return allocations_by_year


def fill_gap_from_daily_costs(
    gap_days: set[date],
    daily_rows: Sequence[Mapping[str, Any]],
    monthly_base_fee: float,
) -> dict[str, Any]:
    """Fill invoice complement days from daily_costs; base fee on (a)/(b) days."""
    measured_day_set = set()
    priced_day_set = set()
    days_hourly = 0
    days_daily_avg = 0
    days_kwh_only = 0
    grid_kwh = 0.0
    energy_eur = 0.0
    measured_hours = 0
    for row in daily_rows:
        day = _parse_day(row.get("date"))
        if day is None or day not in gap_days:
            continue
        measured_day_set.add(day)
        source = str(row.get("source") or "")
        if source == SOURCE_HOURLY:
            days_hourly += 1
            priced_day_set.add(day)
        elif source == SOURCE_DAILY_AVG:
            days_daily_avg += 1
            priced_day_set.add(day)
        elif source == SOURCE_KWH_ONLY:
            days_kwh_only += 1
        try:
            grid_kwh += float(row.get("grid_kwh") or 0.0)
        except (TypeError, ValueError):
            pass
        row_energy_eur = row.get("energy_eur")
        if row_energy_eur is not None:
            try:
                energy_eur += float(row_energy_eur)
            except (TypeError, ValueError):
                pass
        try:
            measured_hours += int(row.get("hours_with_data") or 0)
        except (TypeError, ValueError):
            pass
    base_fee_eur = 0.0
    if priced_day_set and monthly_base_fee > 0:
        overlap_start = min(gap_days) if gap_days else min(priced_day_set)
        overlap_end = max(gap_days) if gap_days else max(priced_day_set)
        gap_end_exclusive = overlap_end + timedelta(days=1)
        for month_start, month_end, month_key in iter_month_slices(
            overlap_start.replace(day=1), gap_end_exclusive
        ):
            month_priced_days = sum(
                (1 for day in priced_day_set if month_start <= day < month_end)
            )
            if month_priced_days <= 0:
                continue
            fee_year = int(month_key[:4])
            fee_month = int(month_key[5:7])
            base_fee_eur += prorate_base_fee(
                monthly_base_fee, fee_year, fee_month, month_priced_days
            )
    return {
        "grid_kwh": _round_kwh(grid_kwh),
        "energy_eur": _round_money(energy_eur),
        "base_fee_eur": _round_money(base_fee_eur),
        "total_eur": _round_money(energy_eur + base_fee_eur),
        "measured_days": len(measured_day_set),
        "measured_day_set": measured_day_set,
        "gap_days_without_data": len(gap_days - measured_day_set),
        "measured_hours": measured_hours,
        "measured_hours_expected": days_hourly * 24,
        "days_hourly": days_hourly,
        "days_daily_avg": days_daily_avg,
        "days_kwh_only": days_kwh_only,
        "priced_days": len(priced_day_set),
    }


def _ct_kwh(total_eur: float, grid_kwh: float) -> float | None:
    if grid_kwh == 0:
        return
    return _round_ct(total_eur / grid_kwh * 100.0)


def _invoice_coverage(
    invoices: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, set[date]], dict[int, list[dict[str, Any]]]]:
    invoice_days_by_year = {}
    invoice_slices_by_year = {}
    for invoice in invoices:
        invoice_window = _invoice_window(invoice)
        if invoice_window is not None:
            period_start, period_end = invoice_window
            allocations_by_year = slice_invoice_by_year(invoice)
            if allocations_by_year:
                invoice_id = invoice.get("id")
                invoice_label = _invoice_label(period_start, period_end)
                for day in _collect_days(period_start, period_end):
                    invoice_days_by_year.setdefault(day.year, set()).add(day)
                for year, year_slice in allocations_by_year.items():
                    invoice_slices_by_year.setdefault(year, []).append(
                        {
                            "invoice_id": invoice_id,
                            "label": invoice_label,
                            "days_in_year": year_slice["days"],
                            "grid_kwh": year_slice["grid_kwh"],
                            "total_eur": year_slice["total_eur"],
                        }
                    )
    return (invoice_days_by_year, invoice_slices_by_year)


def build_year_rows(
    invoices: list[Mapping[str, Any]],
    today: date,
    *,
    gap_by_year: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One calendar-year row: invoice allocation plus optional gap measurement."""
    invoice_days_by_year, invoice_slices_by_year = _invoice_coverage(invoices)
    gap_summaries_by_year = {
        int(year): dict(input_gap_summary)
        for year, input_gap_summary in (gap_by_year or {}).items()
    }
    years = sorted(
        set(invoice_slices_by_year)
        | set(gap_summaries_by_year)
        | set(invoice_days_by_year)
    )
    year_rows = []
    for year in years:
        year_invoices = invoice_slices_by_year.get(year) or []
        invoice_day_set = invoice_days_by_year.get(year) or set()
        gap_summary = gap_summaries_by_year.get(year) or {}
        measured_day_set = set(gap_summary.get("measured_day_set") or ())
        gap_days = complement_days(year, invoice_day_set, today)
        measured_days = int(gap_summary.get("measured_days") or len(measured_day_set))
        if gap_summary:
            gap_days_without_data = int(gap_summary.get("gap_days_without_data") or 0)
            measured_hours = int(gap_summary.get("measured_hours") or 0)
            measured_hours_expected = int(
                gap_summary.get("measured_hours_expected") or 0
            )
            measured_grid_kwh = float(gap_summary.get("grid_kwh") or 0.0)
            measured_total_eur = float(gap_summary.get("total_eur") or 0.0)
            days_hourly = int(gap_summary.get("days_hourly") or 0)
            days_daily_avg = int(gap_summary.get("days_daily_avg") or 0)
            days_kwh_only = int(gap_summary.get("days_kwh_only") or 0)
            priced_days = int(gap_summary.get("priced_days") or 0)
        else:
            gap_days_without_data = len(gap_days)
            measured_hours = 0
            measured_hours_expected = 0
            measured_grid_kwh = 0.0
            measured_total_eur = 0.0
            days_hourly = 0
            days_daily_avg = 0
            days_kwh_only = 0
            priced_days = 0
        if not year_invoices:
            if measured_days <= 0:
                if not invoice_day_set:
                    continue
        grid_kwh = _round_kwh(
            sum((invoice_slice["grid_kwh"] for invoice_slice in year_invoices))
            + measured_grid_kwh
        )
        total_eur = _round_money(
            sum((invoice_slice["total_eur"] for invoice_slice in year_invoices))
            + measured_total_eur
        )
        covered_day_set = invoice_day_set | measured_day_set
        if covered_day_set:
            first_day = min(covered_day_set)
            last_day = max(covered_day_set)
        else:
            first_day = date(year, 1, 1)
            last_day = date(year, 12, 31)
        year_rows.append(
            {
                "year": year,
                "grid_kwh": grid_kwh,
                "total_eur": total_eur,
                "ct_kwh": _ct_kwh(total_eur, grid_kwh),
                "partial": len(covered_day_set) < _days_in_year(year),
                "coverage": {
                    "first_day": first_day.isoformat(),
                    "last_day": last_day.isoformat(),
                    "invoice_days": len(invoice_day_set),
                    "measured_days": measured_days,
                    "gap_days_without_data": gap_days_without_data,
                    "measured_hours": measured_hours,
                    "measured_hours_expected": measured_hours_expected,
                    "days_hourly": days_hourly,
                    "days_daily_avg": days_daily_avg,
                    "days_kwh_only": days_kwh_only,
                    "priced_days": priced_days,
                },
                "invoices": year_invoices,
            }
        )
        continue
    return year_rows


async def _table_columns(db: Any, table_name: str) -> set[str]:
    if not table_name.replace("_", "").isalnum():
        return set()
    try:
        async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
            rows = await cursor.fetchall()
    except Exception:
        return set()
    columns = set()
    for row in rows:
        if hasattr(row, "keys") and "name" in row.keys():
            columns.add(str(row["name"]))
        elif len(row) > 1:
            columns.add(str(row[1]))
    return columns


async def _distinct_years(db: Any, table_name: str) -> set[int]:
    columns = await _table_columns(db, table_name)
    if "date" not in columns:
        return set()
    try:
        async with db.execute(
            f"SELECT DISTINCT substr(date, 1, 4) FROM {table_name} WHERE date IS NOT NULL AND length(date) >= 4"
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        return set()
    years = set()
    for row in rows:
        year_text = str(row[0] if not hasattr(row, "keys") else row[0] or "")
        try:
            years.add(int(year_text))
        except (TypeError, ValueError):
            continue
    return years


async def _daily_costs_for_year(
    year: int, *, db: Any, daily_costs_loader: Any | None, hourly_loader: Any | None
) -> list[dict[str, Any]]:
    period_start = date(year, 1, 1)
    year_end_exclusive = date(year + 1, 1, 1)
    if daily_costs_loader is not None:
        try:
            daily_rows = await daily_costs_loader(period_start, year_end_exclusive)
        except Exception:
            return []
        return list(daily_rows or [])
    if hourly_loader is not None:
        try:
            hourly_rows = await hourly_loader(period_start, year_end_exclusive)
        except Exception:
            return []
        return daily_costs_from_hourly_rows(
            list(hourly_rows or []), period_start, year_end_exclusive
        )
    projection = await projection_from_runtime(period_start.isoformat())
    return await load_daily_costs(db, period_start, year_end_exclusive, projection)


async def yearly_history_from_db(
    db: Any,
    *,
    today: date,
    base_fee_eur: float = 0.0,
    hourly_loader: Any | None = None,
    daily_costs_loader: Any | None = None,
) -> list[dict[str, Any]]:
    try:
        invoices = await list_invoices(db)
    except Exception:
        invoices = []
    invoice_days_by_year, unused_invoice_slices = _invoice_coverage(
        list(invoices or [])
    )
    try:
        monthly_base_fee_eur = max(0.0, float(base_fee_eur or 0.0))
    except (TypeError, ValueError):
        monthly_base_fee_eur = 0.0
    hourly_years = await _distinct_years(db, _HOURLY_TABLE)
    daily_energy_years = await _distinct_years(db, _DAILY_ENERGY_TABLE)
    years = set(invoice_days_by_year) | hourly_years | daily_energy_years
    gap_by_year = {}
    for year in sorted(years):
        gap_days = complement_days(year, invoice_days_by_year.get(year) or set(), today)
        if not gap_days:
            continue
        daily_rows = await _daily_costs_for_year(
            year,
            db=db,
            daily_costs_loader=daily_costs_loader,
            hourly_loader=hourly_loader,
        )
        gap_by_year[year] = fill_gap_from_daily_costs(
            gap_days, daily_rows, monthly_base_fee_eur
        )
    return build_year_rows(list(invoices or []), today, gap_by_year=gap_by_year)


async def build_yearly_history(hass: Any) -> list[dict[str, Any]]:
    from ..api.views import _get_config as get_config, _get_db as get_db
    from ..core.price_mode import base_fee_eur_month as base_fee_eur_month
    from ..utils.time_utils import ha_today as ha_today

    today = ha_today(hass)
    config = {}
    try:
        config = get_config() or {}
    except Exception:
        config = {}
    try:
        monthly_base_fee_eur = float(base_fee_eur_month(hass, config) or 0.0)
    except Exception:
        monthly_base_fee_eur = 0.0
    async with get_db() as db:
        return await yearly_history_from_db(
            db, today=today, base_fee_eur=monthly_base_fee_eur
        )
    return None if True else None
