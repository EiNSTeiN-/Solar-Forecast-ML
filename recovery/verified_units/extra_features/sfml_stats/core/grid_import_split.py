# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-only grid-import split for the card, the bill, and the CSV export.

The stored cent columns and the daily correction overlay stay as they are.
This module only sums them the same way the billing export does.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping
from ..const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_BILLING_WORK_PRICE,
    CONF_LEGACY_FIXED_PRICE_CT,
    CONF_SENSOR_BATTERY_SOC,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_BILLING_WORK_PRICE,
    configured_battery_capacity_kwh,
)
from .corrected_energy import scale_hour_money
from .grid_split import META_GRID_SPLIT_BACKFILL, META_GRID_SPLIT_DONE

_UNASSIGNED_EUR = Decimal("0.01")
_FLOWS = (
    ("import", "grid_import_kwh", "grid_import_day_kwh"),
    ("house", "grid_to_house_kwh", "grid_to_house_day_kwh"),
    ("battery", "grid_to_battery_kwh", "grid_to_battery_day_kwh"),
)


def battery_is_configured(config: Mapping[str, Any] | None) -> bool:
    """True when a battery sensor or a positive capacity is configured."""
    if not isinstance(config, Mapping):
        return False
    if config.get(CONF_SENSOR_BATTERY_SOC) or config.get(CONF_BATTERY_SOC_SENSOR):
        return True
    return (
        configured_battery_capacity_kwh(config.get(CONF_BATTERY_CAPACITY)) is not None
    )


def fixed_total_price_ct(config: Mapping[str, Any] | None) -> float:
    """Same fallback the billing export binds when an hour has no price."""
    if not isinstance(config, Mapping):
        return float(DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES)
    legacy_price_ct = config.get(CONF_LEGACY_FIXED_PRICE_CT)
    if legacy_price_ct not in (None, ""):
        try:
            return float(legacy_price_ct)
        except (TypeError, ValueError):
            pass
    work_price_ct = config.get(CONF_BILLING_WORK_PRICE)
    grid_fees_ct = config.get(CONF_BILLING_GRID_FEES)
    if work_price_ct is not None and grid_fees_ct is not None:
        try:
            return float(work_price_ct) + float(grid_fees_ct)
        except (TypeError, ValueError):
            pass
    try:
        return float(
            config.get(
                CONF_BILLING_FIXED_PRICE,
                DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES,
            )
        )
    except (TypeError, ValueError):
        return float(DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES)


def charge_window_bounds(local_day: date) -> dict[str, tuple[str, str]]:
    """Inclusive local civil dates for today, this calendar month, this year."""
    local_date_str = local_day.isoformat()
    return {
        "today": (local_date_str, local_date_str),
        "month": (local_day.replace(day=1).isoformat(), local_date_str),
        "year": (date(local_day.year, 1, 1).isoformat(), local_date_str),
    }


def flow_cost_eur(
    kwh: Any,
    stored_ct: Any,
    price_ct: Any,
    fallback_ct: float,
    *,
    column_present: bool,
    backfill_done: bool,
) -> tuple[float, bool]:
    """Euro twin of the export CASE. A missing stored cost stays unpriced.

    The returned euro is 0 for that hour so the sum matches
    ``COALESCE(cost, 0)``. ``unpriced`` is true only when the hour has
    energy and no stored cost the marker would replace.
    """
    energy_kwh = _non_negative(kwh)
    resolved_price_ct = _price_or_fallback(price_ct, fallback_ct)
    if not column_present:
        return (energy_kwh * resolved_price_ct / 100.0, False)
    if not backfill_done:
        if (
            _stored_missing_or_zero(stored_ct)
            and energy_kwh > 0.0
            and (resolved_price_ct > 0.0)
        ):
            return (energy_kwh * resolved_price_ct / 100.0, False)
    if stored_ct is None:
        return (0.0, energy_kwh > 0.0)
    return (float(stored_ct) / 100.0, False)


def import_cost_eur(
    kwh: Any, stored_ct: Any, price_ct: Any, fallback_ct: float
) -> float:
    """Euro twin of the export import column, before the daily overlay."""
    if stored_ct is not None:
        return float(stored_ct) / 100.0
    return _non_negative(kwh) * _price_or_fallback(price_ct, fallback_ct) / 100.0


@dataclass(frozen=True)
class SplitHours:
    hours: list[dict[str, Any]]
    house_column: bool
    battery_column: bool
    backfill_done: bool


def summarize_import_split(
    hours: list[Mapping[str, Any]],
    daily: Mapping[str, Mapping[str, Any]] | None,
    projection: Mapping[str, Mapping[str, Any]] | None,
    *,
    fallback_price_ct: float,
    house_column: bool,
    battery_column: bool,
    backfill_done: bool,
) -> dict[str, Any]:
    """House, battery, and import after the same overlay the export applies."""
    prepared_hours = [
        _prepare_hour(
            hour_row,
            fallback_price_ct,
            house_column=house_column,
            battery_column=battery_column,
            backfill_done=backfill_done,
        )
        for hour_row in hours
    ]
    hours_by_date = {}
    for hour_row in prepared_hours:
        hours_by_date.setdefault(hour_row["date"], []).append(hour_row)
    for date_key, day_hours in hours_by_date.items():
        _scale_day(
            day_hours,
            (daily or {}).get(date_key, {}),
            (projection or {}).get(date_key, {}),
            fallback_price_ct,
        )
    return _public_split(prepared_hours)


def battery_card(split: Mapping[str, Any]) -> dict[str, Any]:
    """Netzladung card for one window. Omits a limit share that is unknown."""
    battery_flow = split["battery"]
    card_payload = {
        "kwh": battery_flow["kwh"],
        "cost_eur": battery_flow["cost_eur"],
        "avg_ct_kwh": battery_flow["avg_ct_kwh"],
        "unpriced_kwh": battery_flow["unpriced_kwh"],
    }
    if battery_flow.get("under_limit_share") is not None:
        card_payload["under_limit_kwh"] = battery_flow["under_limit_kwh"]
        card_payload["under_limit_share"] = battery_flow["under_limit_share"]
    return card_payload


async def load_split_hours(db: Any, start: str, end: str) -> SplitHours:
    """Hourly billing rows for a local-date window. Reads only."""
    available_columns = await _column_names(db, "stats_hourly_billing")
    if "date" not in available_columns:
        return SplitHours([], False, False, False)
    selected_columns = [
        column_name
        for column_name in (
            "date",
            "grid_import_kwh",
            "grid_import_cost_ct",
            "grid_to_house_kwh",
            "grid_to_house_cost_ct",
            "grid_to_battery_kwh",
            "grid_to_battery_cost_ct",
            "price_ct_kwh",
        )
        if column_name in available_columns
    ]
    query = f"SELECT {', '.join(selected_columns)} FROM stats_hourly_billing WHERE date >= ? AND date <= ? ORDER BY date"
    try:
        async with db.execute(query, (start, end)) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        return SplitHours(
            [],
            "grid_to_house_cost_ct" in available_columns,
            "grid_to_battery_cost_ct" in available_columns,
            False,
        )
    hours = [_hour_from_row(row, selected_columns) for row in rows]
    return SplitHours(
        hours,
        "grid_to_house_cost_ct" in available_columns,
        "grid_to_battery_cost_ct" in available_columns,
        await _backfill_done(db),
    )


async def import_split_between(
    db: Any,
    start: str,
    end: str,
    daily: Mapping[str, Mapping[str, Any]] | None,
    projection: Mapping[str, Mapping[str, Any]] | None,
    fallback_price_ct: float,
) -> dict[str, Any]:
    split_hours = await load_split_hours(db, start, end)
    return summarize_import_split(
        split_hours.hours,
        daily,
        projection,
        fallback_price_ct=fallback_price_ct,
        house_column=split_hours.house_column,
        battery_column=split_hours.battery_column,
        backfill_done=split_hours.backfill_done,
    )


def _prepare_hour(
    hour: Mapping[str, Any],
    fallback_ct: float,
    *,
    house_column: bool,
    battery_column: bool,
    backfill_done: bool,
) -> dict[str, Any]:
    price_ct = hour.get("price_ct")
    house_cost_eur, house_unpriced = flow_cost_eur(
        hour.get("house_kwh"),
        hour.get("house_cost_ct"),
        price_ct,
        fallback_ct,
        column_present=house_column,
        backfill_done=backfill_done,
    )
    battery_cost_eur, battery_unpriced = flow_cost_eur(
        hour.get("battery_kwh"),
        hour.get("battery_cost_ct"),
        price_ct,
        fallback_ct,
        column_present=battery_column,
        backfill_done=backfill_done,
    )
    prepared_hour = {
        "date": str(hour.get("date") or ""),
        "import_kwh": _non_negative(hour.get("import_kwh")),
        "import_eur": _eur4(
            import_cost_eur(
                hour.get("import_kwh"),
                hour.get("import_cost_ct"),
                price_ct,
                fallback_ct,
            )
        ),
        "house_kwh": _non_negative(hour.get("house_kwh")),
        "house_eur": _eur4(house_cost_eur),
        "house_unpriced": house_unpriced,
        "battery_kwh": _non_negative(hour.get("battery_kwh")),
        "battery_eur": _eur4(battery_cost_eur),
        "battery_unpriced": battery_unpriced,
    }
    if "under_limit" in hour:
        prepared_hour["under_limit"] = hour.get("under_limit")
    return prepared_hour


def _scale_day(
    rows: list[dict[str, Any]],
    values: Mapping[str, Any],
    day_projection: Mapping[str, Any],
    fallback_price_ct: float,
) -> None:
    for flow_name, raw_column, correction_key in _FLOWS:
        raw_value_key = f"raw_{raw_column}"
        if raw_value_key not in values and correction_key not in values:
            continue
        raw_kwh = float(values.get(raw_value_key, 0.0) or 0.0)
        corrected_kwh = float(values.get(correction_key, raw_kwh) or 0.0)
        hour_kwh = [float(row[f"{flow_name}_kwh"]) for row in rows]
        hour_cost_eur = [float(row[f"{flow_name}_eur"]) for row in rows]
        scaled_kwh, scaled_cost_eur = scale_hour_money(
            raw_kwh,
            corrected_kwh,
            hour_kwh,
            hour_cost_eur,
            fallback_price_ct=fallback_price_ct,
            hour_sum=sum(hour_kwh),
            corrected=correction_key in day_projection,
        )
        for row, scaled_hour_kwh, scaled_hour_eur in zip(
            rows, scaled_kwh, scaled_cost_eur
        ):
            row[f"{flow_name}_kwh"] = float(scaled_hour_kwh)
            row[f"{flow_name}_eur"] = float(scaled_hour_eur)
            if row.get(f"{flow_name}_unpriced") and float(scaled_hour_eur) != 0.0:
                row[f"{flow_name}_unpriced"] = False
        continue


def _public_split(hours: list[dict[str, Any]]) -> dict[str, Any]:
    flows = {
        column_name: _flow_public(hours, column_name)
        for column_name in ("import", "house", "battery")
    }
    import_cost_eur = Decimal(str(flows["import"]["cost_eur"]))
    house_cost_eur = Decimal(str(flows["house"]["cost_eur"]))
    battery_cost_eur = Decimal(str(flows["battery"]["cost_eur"]))
    unassigned_eur = _q2(import_cost_eur - house_cost_eur - battery_cost_eur)
    import_kwh = Decimal(str(flows["import"]["kwh"]))
    house_kwh = Decimal(str(flows["house"]["kwh"]))
    battery_kwh = Decimal(str(flows["battery"]["kwh"]))
    unassigned_kwh = _q2(import_kwh - house_kwh - battery_kwh)
    return {
        "import": flows["import"],
        "house": flows["house"],
        "battery": flows["battery"],
        "unassigned_kwh": float(unassigned_kwh),
        "unassigned_eur": float(unassigned_eur),
        "show_unassigned": unassigned_eur > _UNASSIGNED_EUR,
    }


def _flow_public(hours: list[dict[str, Any]], name: str) -> dict[str, Any]:
    total_kwh = sum((float(hour_row[f"{name}_kwh"]) for hour_row in hours))
    total_cost_eur = sum((float(hour_row[f"{name}_eur"]) for hour_row in hours))
    unpriced_kwh = sum(
        (
            float(hour_row[f"{name}_kwh"])
            for hour_row in hours
            if hour_row.get(f"{name}_unpriced")
        )
    )
    priced_kwh = total_kwh - unpriced_kwh
    flow_summary = {
        "kwh": float(_q2(Decimal(str(total_kwh)))),
        "cost_eur": float(_q2(Decimal(str(total_cost_eur)))),
        "unpriced_kwh": float(_q2(Decimal(str(unpriced_kwh)))),
        "avg_ct_kwh": (
            float(
                _q2(
                    Decimal(str(total_cost_eur))
                    * Decimal("100")
                    / Decimal(str(priced_kwh))
                )
            )
            if priced_kwh > 0.0
            else None
        ),
    }
    if name == "battery":
        limit_result = _limit_share(hours)
        flow_summary["under_limit_kwh"] = (
            None if limit_result is None else limit_result[0]
        )
        flow_summary["under_limit_share"] = (
            None if limit_result is None else limit_result[1]
        )
    return flow_summary


def _limit_share(hours: list[dict[str, Any]]) -> tuple[float, float] | None:
    charged_hours = [
        hour_row for hour_row in hours if float(hour_row["battery_kwh"]) > 0.0
    ]
    if not charged_hours or any(
        ("under_limit" not in hour_row for hour_row in charged_hours)
    ):
        return None
    if any((hour_row.get("under_limit") is None for hour_row in charged_hours)):
        return
    total_charge_kwh = sum(
        (float(hour_row["battery_kwh"]) for hour_row in charged_hours)
    )
    if total_charge_kwh <= 0.0:
        return
    under_limit_kwh = sum(
        (
            float(hour_row["battery_kwh"])
            for hour_row in charged_hours
            if hour_row.get("under_limit")
        )
    )
    under_limit_share = float(
        Decimal(str(under_limit_kwh / total_charge_kwh)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    )
    return (float(_q2(Decimal(str(under_limit_kwh)))), under_limit_share)


def _hour_from_row(row: Any, columns: list[str]) -> dict[str, Any]:
    cells = {
        column_name: _cell(row, column_index, column_name)
        for column_index, column_name in enumerate(columns)
    }
    return {
        "date": str(cells.get("date") or ""),
        "import_kwh": cells.get("grid_import_kwh"),
        "import_cost_ct": cells.get("grid_import_cost_ct"),
        "house_kwh": cells.get("grid_to_house_kwh"),
        "house_cost_ct": cells.get("grid_to_house_cost_ct"),
        "battery_kwh": cells.get("grid_to_battery_kwh"),
        "battery_cost_ct": cells.get("grid_to_battery_cost_ct"),
        "price_ct": cells.get("price_ct_kwh"),
    }


async def _column_names(db: Any, table: str) -> set[str]:
    try:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            schema_rows = await cursor.fetchall()
    except Exception:
        return set()
    column_names = set()
    for row in schema_rows:
        column_name = _cell(row, 1, "name")
        if column_name is not None:
            column_names.add(str(column_name))
    return column_names


async def _backfill_done(db: Any) -> bool:
    try:
        async with db.execute(
            "SELECT value FROM stats_schema_meta WHERE key = ?",
            (META_GRID_SPLIT_BACKFILL,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception:
        return False
    if row is None:
        return False
    return _cell(row, 0, "value") == META_GRID_SPLIT_DONE


def _cell(row: Any, index: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        pass
    try:
        return row[index]
    except (TypeError, IndexError, KeyError):
        return


def _non_negative(value: Any) -> float:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric_value < 0.0:
        return 0.0
    return numeric_value


def _price_or_fallback(price_ct: Any, fallback_ct: float) -> float:
    try:
        resolved_price_ct = float(price_ct)
    except (TypeError, ValueError):
        resolved_price_ct = 0.0
    if resolved_price_ct > 0.0:
        return resolved_price_ct
    try:
        fallback_price_ct = float(fallback_ct)
    except (TypeError, ValueError):
        return 0.0
    return fallback_price_ct if fallback_price_ct > 0.0 else 0.0


def _stored_missing_or_zero(stored_ct: Any) -> bool:
    if stored_ct is None:
        return True
    try:
        return float(stored_ct) == 0.0
    except (TypeError, ValueError):
        return True


def _eur4(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    )


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
