# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Monthly electricity-bill aggregates for the GPM page (read-only)."""
from __future__ import annotations
import calendar
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

LICENSED_SOURCES = frozenset({"eai_entry", "manual"})
COMPLETE_COVERAGE = 90.0
_CONFIGURE_LINK = "/config/integrations/integration/grid_price_monitor"
MONTH_COST_KEYS = (
    "month",
    "period_start",
    "period_end",
    "total_eur",
    "energy_eur",
    "base_fee_eur",
    "kwh",
    "avg_price_ct",
    "hours_with_data",
    "hours_in_month",
    "coverage_percent",
    "complete",
    "is_current",
    "priced_days",
    "days_hourly",
    "days_daily_avg",
    "days_kwh_only",
)
PROJECTION_KEYS = ("actual_to_date_eur", "expected_eur", "low_eur", "high_eur", "basis")
BASIS_KEYS = ("complete_months", "price_level_ct", "method")
MONTHLY_COSTS_KEYS = (
    "success",
    "year",
    "mode",
    "billing_start",
    "available_years",
    "months",
    "previous_year",
    "projection",
    "projection_reason",
    "is_demo",
    "license_required",
    "links",
)


def license_allows_real_monthly_costs(license_info: Mapping[str, Any] | None) -> bool:
    license_data = license_info if isinstance(license_info, Mapping) else {}
    license_status = license_data.get("status")
    source = license_data.get("source")
    return license_status == "valid" and source in LICENSED_SOURCES


def clamp_billing_start(month: Any, day: Any) -> tuple[int, int]:
    try:
        billing_month = int(month)
    except (TypeError, ValueError):
        billing_month = 1
    try:
        billing_day = int(day)
    except (TypeError, ValueError):
        billing_day = 1
    billing_month = min(12, max(1, billing_month or 1))
    billing_day = min(31, max(1, billing_day or 1))
    return (billing_month, billing_day)


def period_bounds(
    year: int, mode: str, start_month: int, start_day: int
) -> tuple[date, date]:
    if mode == "billing":
        start_day_clamped = min(start_day, calendar.monthrange(year, start_month)[1])
        period_start = date(year, start_month, start_day_clamped)
        next_start_day_clamped = min(
            start_day, calendar.monthrange(year + 1, start_month)[1]
        )
        period_end = date(year + 1, start_month, next_start_day_clamped)
        return (period_start, period_end)
    return (date(year, 1, 1), date(year + 1, 1, 1))


def iter_month_slices(start: date, end: date) -> list[tuple[date, date, str]]:
    month_slices = []
    month_start = date(start.year, start.month, 1)
    while month_start < end:
        days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
        next_month = date(
            month_start.year, month_start.month, days_in_month
        ) + timedelta(days=1)
        slice_start = max(start, month_start)
        slice_end = min(end, next_month)
        if slice_start < slice_end:
            month_key = f"{month_start.year:04d}-{month_start.month:02d}"
            month_slices.append((slice_start, slice_end, month_key))
        if month_start.month == 12:
            month_start = date(month_start.year + 1, 1, 1)
        else:
            month_start = date(month_start.year, month_start.month + 1, 1)
    return month_slices


def shift_month_key(month_key: str, years: int) -> str | None:
    if not isinstance(month_key, str) or len(month_key) < 7:
        return
    try:
        year_number = int(month_key[:4])
        month_number = int(month_key[5:7])
    except (TypeError, ValueError):
        return
    return f"{year_number + years:04d}-{month_number:02d}"


def _parse_iso_date(value: Any) -> date | None:
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


def billing_year_for_date(value: date, start_month: int, start_day: int) -> int:
    start_day_clamped = min(start_day, calendar.monthrange(value.year, start_month)[1])
    period_start = date(value.year, start_month, start_day_clamped)
    if value >= period_start:
        return value.year
    return value.year - 1


def available_years_from_dates(
    dates: Iterable[Any], start_month: int, start_day: int
) -> dict[str, list[int]]:
    calendar_years = set()
    billing_years = set()
    for date_value in dates:
        parsed_date = _parse_iso_date(date_value)
        if parsed_date is not None:
            calendar_years.add(parsed_date.year)
            billing_years.add(
                billing_year_for_date(parsed_date, start_month, start_day)
            )
    return {"calendar": sorted(calendar_years), "billing": sorted(billing_years)}


def _priced(price_ct: Any) -> bool:
    try:
        resolved_price_ct = float(price_ct)
    except (TypeError, ValueError):
        return False
    return resolved_price_ct != 0.0


def prorate_base_fee(
    monthly_eur: float, month_year: int, month_num: int, days_counted: int
) -> float:
    calendar_days = calendar.monthrange(month_year, month_num)[1]
    if calendar_days <= 0 or days_counted <= 0 or monthly_eur <= 0:
        return 0.0
    counted_days = min(days_counted, calendar_days)
    return round(monthly_eur * counted_days / calendar_days, 4)


def _round_money(value: float) -> float:
    return round(value, 2)


def _round_kwh(value: float) -> float:
    return round(value, 3)


def _round_ct(value: float) -> float:
    return round(value, 2)


def _empty_month(
    key: str, slice_start: date, slice_end: date, today: date, base_fee_eur: float
) -> dict[str, Any]:
    hours_in_slice = (slice_end - slice_start).days * 24
    coverage_percent = 0.0
    return {
        "month": key,
        "period_start": slice_start.isoformat(),
        "period_end": (slice_end - timedelta(days=1)).isoformat(),
        "total_eur": _round_money(base_fee_eur),
        "energy_eur": 0.0,
        "base_fee_eur": _round_money(base_fee_eur),
        "kwh": 0.0,
        "avg_price_ct": None,
        "hours_with_data": 0,
        "hours_in_month": hours_in_slice,
        "coverage_percent": coverage_percent,
        "complete": False,
        "is_current": slice_start <= today < slice_end,
        "priced_days": 0,
        "days_hourly": 0,
        "days_daily_avg": 0,
        "days_kwh_only": 0,
    }


def _in_slice(day: date | None, slice_start: date, slice_end: date) -> bool:
    return day is not None and slice_start <= day < slice_end


def _slice_has_rows(
    rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]] | None,
    slice_start: date,
    slice_end: date,
) -> bool:
    for row in rows:
        if not _in_slice(_parse_iso_date(row.get("date")), slice_start, slice_end):
            continue
        return True
    for row in daily_rows or ():
        if not _in_slice(_parse_iso_date(row.get("date")), slice_start, slice_end):
            continue
        return True
    return False


def _corrected_daily_kwh(row: Mapping[str, Any]) -> float:
    """Read a daily cost row through corrected_energy when the package is loaded."""
    try:
        from ..core.corrected_energy import daily_row_kwh as daily_row_kwh
    except ImportError:
        try:
            return float(row.get("grid_kwh") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return daily_row_kwh(row)


def _corrected_daily_eur(row: Mapping[str, Any]) -> float:
    """Read daily euros through corrected_energy. Missing energy stays zero."""
    if row.get("energy_eur") is None:
        return 0.0
    try:
        from ..core.corrected_energy import daily_row_eur as daily_row_eur
    except ImportError:
        try:
            return float(row.get("energy_eur") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return daily_row_eur(row)


def aggregate_slice(
    rows: Sequence[Mapping[str, Any]],
    slice_start: date,
    slice_end: date,
    month_key: str,
    today: date,
    monthly_base_fee: float,
    daily_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """kWh/€ from hourly rows or daily_costs fallback; complete is hours-only (K4)."""
    hours_in_slice = (slice_end - slice_start).days * 24
    hours_with_data = 0
    total_kwh = 0.0
    priced_kwh = 0.0
    energy_eur = 0.0
    period_start_iso = slice_start.isoformat()
    seen_hours = set()
    seen_dates = set()
    for row in rows:
        row_date = _parse_iso_date(row.get("date"))
        if not _in_slice(row_date, slice_start, slice_end):
            continue
        try:
            row_hour = int(row.get("hour"))
        except (TypeError, ValueError):
            row_hour = 0
        hour_key = (row_date.isoformat(), row_hour)
        if hour_key in seen_hours:
            continue
        seen_hours.add(hour_key)
        seen_dates.add(row_date)
        hours_with_data += 1
        try:
            import_kwh = float(row.get("grid_import_kwh") or 0.0)
        except (TypeError, ValueError):
            import_kwh = 0.0
        total_kwh += import_kwh
        raw_price_ct = row.get("price_ct_kwh")
        if not _priced(raw_price_ct):
            continue
        price_ct = float(raw_price_ct)
        priced_kwh += import_kwh
        energy_eur += import_kwh * price_ct / 100.0
        continue
    days_hourly = len(seen_dates)
    days_daily_avg = 0
    days_kwh_only = 0
    priced_days = days_hourly
    if daily_rows:
        total_kwh = 0.0
        priced_kwh = 0.0
        energy_eur = 0.0
        days_hourly = 0
        days_daily_avg = 0
        days_kwh_only = 0
        priced_days = 0
        daily_hours_with_data = 0
        for row in daily_rows:
            row_date = _parse_iso_date(row.get("date"))
            if not _in_slice(row_date, slice_start, slice_end):
                continue
            source = str(row.get("source") or "")
            if source == "hourly":
                days_hourly += 1
                priced_days += 1
            elif source == "daily_avg":
                days_daily_avg += 1
                priced_days += 1
            elif source == "kwh_only":
                days_kwh_only += 1
            total_kwh += _corrected_daily_kwh(row)
            energy_eur += _corrected_daily_eur(row)
            try:
                priced_kwh += float(row.get("priced_kwh") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                daily_hours_with_data += int(row.get("hours_with_data") or 0)
                continue
            except (TypeError, ValueError):
                pass
        if hours_with_data == 0:
            hours_with_data = daily_hours_with_data
    month_number = int(month_key[5:7])
    year_number = int(month_key[:4])
    allocated_base_fee_eur = prorate_base_fee(
        monthly_base_fee, year_number, month_number, priced_days
    )
    coverage_percent = (
        0.0
        if hours_in_slice <= 0
        else round(100.0 * hours_with_data / hours_in_slice, 1)
    )
    average_price_ct = (
        None if priced_kwh <= 0 else _round_ct(energy_eur * 100.0 / priced_kwh)
    )
    return {
        "month": month_key,
        "period_start": period_start_iso,
        "period_end": (slice_end - timedelta(days=1)).isoformat(),
        "total_eur": _round_money(energy_eur + allocated_base_fee_eur),
        "energy_eur": _round_money(energy_eur),
        "base_fee_eur": _round_money(allocated_base_fee_eur),
        "kwh": _round_kwh(total_kwh),
        "avg_price_ct": average_price_ct,
        "hours_with_data": hours_with_data,
        "hours_in_month": hours_in_slice,
        "coverage_percent": coverage_percent,
        "complete": coverage_percent >= COMPLETE_COVERAGE,
        "is_current": slice_start <= today < slice_end,
        "priced_days": priced_days,
        "days_hourly": days_hourly,
        "days_daily_avg": days_daily_avg,
        "days_kwh_only": days_kwh_only,
    }


def _index_by_month(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("month")): row for row in rows if row.get("month")}


def project_year(
    months: Sequence[dict[str, Any]],
    previous_by_month: Mapping[str, dict[str, Any]],
    today: date,
    recent_avg_ct: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    elapsed_months = []
    future_months = []
    for row in months:
        period_start = _parse_iso_date(row.get("period_start"))
        if period_start is None:
            continue
        if period_start > today:
            future_months.append(row)
        else:
            elapsed_months.append(row)
    complete_months = [row for row in elapsed_months if row.get("complete")]
    if len(complete_months) < 3:
        return (None, "too_few_complete_months")
    actual_to_date_eur = _round_money(
        sum((float(row.get("total_eur") or 0.0) for row in elapsed_months))
    )
    average_complete_cost_eur = sum(
        (float(row.get("total_eur") or 0.0) for row in complete_months)
    ) / len(complete_months)
    complete_prices_ct = [
        float(row["avg_price_ct"])
        for row in complete_months
        if row.get("avg_price_ct") is not None
    ]
    average_complete_price_ct = (
        sum(complete_prices_ct) / len(complete_prices_ct)
        if complete_prices_ct
        else None
    )
    if recent_avg_ct and average_complete_price_ct:
        price_scale = recent_avg_ct / average_complete_price_ct
    else:
        price_scale = 1.0
    scaled_monthly_eur = average_complete_cost_eur * price_scale
    complete_costs_eur = [float(row.get("total_eur") or 0.0) for row in complete_months]
    minimum_month_eur = min(complete_costs_eur)
    maximum_month_eur = max(complete_costs_eur)
    future_total_eur = 0.0
    prior_year_months = 0
    scaled_months = 0
    for row in future_months:
        prior_month_key = shift_month_key(str(row.get("month")), -1)
        prior_month = previous_by_month.get(prior_month_key or "")
        if prior_month and prior_month.get("hours_with_data"):
            future_total_eur += float(prior_month.get("total_eur") or 0.0)
            prior_year_months += 1
        else:
            future_total_eur += scaled_monthly_eur
            scaled_months += 1
    if future_months and prior_year_months and (not scaled_months):
        projection_method = "prior_year"
    elif future_months and scaled_months and (not prior_year_months):
        projection_method = "scaled_complete"
    elif future_months and prior_year_months and scaled_months:
        projection_method = "mixed"
    else:
        projection_method = "complete_only"
    future_month_count = len(future_months)
    return (
        {
            "actual_to_date_eur": actual_to_date_eur,
            "expected_eur": _round_money(actual_to_date_eur + future_total_eur),
            "low_eur": _round_money(
                actual_to_date_eur + future_month_count * minimum_month_eur
            ),
            "high_eur": _round_money(
                actual_to_date_eur + future_month_count * maximum_month_eur
            ),
            "basis": {
                "complete_months": len(complete_months),
                "price_level_ct": (
                    None if recent_avg_ct is None else _round_ct(recent_avg_ct)
                ),
                "method": projection_method,
            },
        },
        None,
    )


def public_month_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    month_row = row if isinstance(row, Mapping) else {}
    return {field_key: month_row.get(field_key) for field_key in MONTH_COST_KEYS}


def public_monthly_costs(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    resolved_payload = dict(payload or {})
    months = (
        resolved_payload.get("months")
        if isinstance(resolved_payload.get("months"), list)
        else []
    )
    previous_months = (
        resolved_payload.get("previous_year")
        if isinstance(resolved_payload.get("previous_year"), list)
        else []
    )
    projection = resolved_payload.get("projection")
    public_projection = None
    if isinstance(projection, Mapping):
        projection_basis = (
            projection.get("basis")
            if isinstance(projection.get("basis"), Mapping)
            else {}
        )
        public_projection = {
            "actual_to_date_eur": projection.get("actual_to_date_eur"),
            "expected_eur": projection.get("expected_eur"),
            "low_eur": projection.get("low_eur"),
            "high_eur": projection.get("high_eur"),
            "basis": {
                basis_key: projection_basis.get(basis_key) for basis_key in BASIS_KEYS
            },
        }
    available_years = (
        resolved_payload.get("available_years")
        if isinstance(resolved_payload.get("available_years"), Mapping)
        else {}
    )
    billing_start = (
        resolved_payload.get("billing_start")
        if isinstance(resolved_payload.get("billing_start"), Mapping)
        else {}
    )
    return {
        "success": True,
        "year": resolved_payload.get("year"),
        "mode": resolved_payload.get("mode"),
        "billing_start": {
            "month": billing_start.get("month"),
            "day": billing_start.get("day"),
        },
        "available_years": {
            "calendar": list(available_years.get("calendar") or []),
            "billing": list(available_years.get("billing") or []),
        },
        "months": [public_month_row(row) for row in months if isinstance(row, Mapping)],
        "previous_year": [
            public_month_row(row) for row in previous_months if isinstance(row, Mapping)
        ],
        "projection": public_projection,
        "projection_reason": resolved_payload.get("projection_reason"),
        "is_demo": bool(resolved_payload.get("is_demo")),
        "license_required": bool(resolved_payload.get("license_required")),
        "links": {"configure": _CONFIGURE_LINK},
    }


def build_monthly_costs(
    *,
    year: int,
    mode: str,
    start_month: int,
    start_day: int,
    today: date,
    base_fee_eur: float,
    rows: Sequence[Mapping[str, Any]],
    date_list: Iterable[Any],
    recent_avg_ct: float | None,
    is_demo: bool,
    license_required: bool,
    daily_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    start_month, start_day = clamp_billing_start(start_month, start_day)
    period_mode = "billing" if mode == "billing" else "calendar"
    period_start, period_end = period_bounds(year, period_mode, start_month, start_day)
    prior_period_start, prior_period_end = period_bounds(
        year - 1, period_mode, start_month, start_day
    )
    month_slices = iter_month_slices(period_start, period_end)
    prior_month_slices = iter_month_slices(prior_period_start, prior_period_end)
    months = []
    for slice_start, slice_end, month_key in month_slices:
        has_rows = _slice_has_rows(rows, daily_rows, slice_start, slice_end)
        if has_rows or slice_start <= today:
            months.append(
                aggregate_slice(
                    rows,
                    slice_start,
                    slice_end,
                    month_key,
                    today,
                    base_fee_eur,
                    daily_rows=daily_rows,
                )
            )
        else:
            months.append(_empty_month(month_key, slice_start, slice_end, today, 0.0))
    prior_months = []
    for slice_start, slice_end, month_key in prior_month_slices:
        has_rows = _slice_has_rows(rows, daily_rows, slice_start, slice_end)
        if has_rows:
            prior_months.append(
                aggregate_slice(
                    rows,
                    slice_start,
                    slice_end,
                    month_key,
                    today,
                    base_fee_eur,
                    daily_rows=daily_rows,
                )
            )
        else:
            prior_months.append(
                _empty_month(month_key, slice_start, slice_end, today, 0.0)
            )
    active_year = (
        today.year
        if period_mode == "calendar"
        else billing_year_for_date(today, start_month, start_day)
    )
    projection = None
    projection_reason = None
    if year == active_year:
        projection, projection_reason = project_year(
            months, _index_by_month(prior_months), today, recent_avg_ct
        )
    payload = {
        "success": True,
        "year": year,
        "mode": period_mode,
        "billing_start": {"month": start_month, "day": start_day},
        "available_years": available_years_from_dates(
            date_list, start_month, start_day
        ),
        "months": months,
        "previous_year": prior_months,
        "projection": projection,
        "projection_reason": projection_reason,
        "is_demo": is_demo,
        "license_required": license_required,
        "links": {"configure": _CONFIGURE_LINK},
    }
    return public_monthly_costs(payload)


DEMO_TODAY = date(2026, 9, 19)
_DEMO_BASE_FEE = 12.5
_DEMO_KWH = (420, 390, 350, 300, 260, 240, 250, 270, 300, 340, 380, 410)
_DEMO_PRICE_CT = (
    32.0,
    31.0,
    29.0,
    27.0,
    25.0,
    24.0,
    24.5,
    25.5,
    27.0,
    29.0,
    31.0,
    32.5,
)
_DEMO_RECENT_CT = 28.0


def _demo_hourly_rows(through: date) -> list[dict[str, Any]]:
    demo_rows = []
    for year_number in (2024, 2025, 2026):
        for month_number in range(1, 13):
            calendar_days = calendar.monthrange(year_number, month_number)[1]
            if date(year_number, month_number, 1) > through:
                continue
            days_counted = (
                min(calendar_days, through.day)
                if year_number == through.year and month_number == through.month
                else calendar_days
            )
            import_kwh = _DEMO_KWH[month_number - 1] / (calendar_days * 24)
            raw_price_ct = _DEMO_PRICE_CT[month_number - 1]
            for day_number in range(1, days_counted + 1):
                row_date = date(year_number, month_number, day_number)
                if row_date > through:
                    break
                for row_hour in range(24):
                    demo_rows.append(
                        {
                            "date": row_date.isoformat(),
                            "hour": row_hour,
                            "grid_import_kwh": round(import_kwh, 6),
                            "price_ct_kwh": raw_price_ct,
                        }
                    )
    return demo_rows


def monthly_costs_demo_payload(
    year: int, mode: str = "calendar", *, start_month: int = 1, start_day: int = 1
) -> dict[str, Any]:
    demo_rows = _demo_hourly_rows(DEMO_TODAY)
    date_set = {row["date"] for row in demo_rows}
    return build_monthly_costs(
        year=year,
        mode=mode,
        start_month=start_month,
        start_day=start_day,
        today=DEMO_TODAY,
        base_fee_eur=_DEMO_BASE_FEE,
        rows=demo_rows,
        date_list=date_set,
        recent_avg_ct=_DEMO_RECENT_CT,
        is_demo=True,
        license_required=True,
    )
