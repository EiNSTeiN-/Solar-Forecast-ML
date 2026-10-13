# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-time corrected energy and the costs that follow it.

Raw rows in stats_daily_energy and stats_hourly_billing stay unchanged.
Scaling happens only while reading.
"""
from __future__ import annotations
from typing import Any, Mapping
from .corrections import METRIC_REGISTRY, EnergyCorrectionStore

_DAILY_TABLE = "stats_daily_energy"
_HOURLY_TABLE = "stats_hourly_billing"
_GPM_HISTORY = "GPM_price_history"
_GPM_AVERAGES = "GPM_daily_averages"


def corrected_value(
    projection: dict[str, dict[str, float]] | None,
    day: Any,
    metric_id: str,
    raw_value: Any,
) -> float:
    """Corrected daily quantity. The only consumer of project_value outside the store."""
    return EnergyCorrectionStore.project_value(
        projection or {}, str(day), metric_id, raw_value
    )


def scale_hourly_amounts(
    raw_daily: float, corrected_daily: float, hourly_amounts: list[float]
) -> list[float] | None:
    """Scale one metric's hours. None means the day has no hourly rows."""
    if not hourly_amounts:
        return
    raw_value_kwh = float(raw_daily or 0.0)
    corrected_value_kwh = float(corrected_daily or 0.0)
    if raw_value_kwh > 0.0:
        scale_factor = corrected_value_kwh / raw_value_kwh
        return [
            float(hourly_value or 0.0) * scale_factor for hourly_value in hourly_amounts
        ]
    if corrected_value_kwh == 0.0:
        return [float(hourly_value or 0.0) for hourly_value in hourly_amounts]
    hourly_total_kwh = sum(
        (float(hourly_value or 0.0) for hourly_value in hourly_amounts)
    )
    if hourly_total_kwh > 0.0:
        scale_factor = corrected_value_kwh / hourly_total_kwh
        return [
            float(hourly_value or 0.0) * scale_factor for hourly_value in hourly_amounts
        ]
    equal_hourly_share = corrected_value_kwh / len(hourly_amounts)
    return [equal_hourly_share for unused_hour in hourly_amounts]


def corrected_month_sum(
    day_values: list[dict[str, Any]], key: str, fallback: Any
) -> float:
    """Sum one corrected metric across a month, or keep the raw SQL fallback."""
    if not day_values or key not in day_values[0]:
        try:
            return float(fallback or 0.0)
        except (TypeError, ValueError):
            return 0.0
    corrected_total = 0.0
    for day_record in day_values:
        try:
            corrected_total += float(day_record.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return corrected_total


_HOUR_SCALE_COLUMNS = (
    ("grid_import_kwh", "grid_import_day_kwh", None),
    ("grid_to_house_kwh", "grid_to_house_day_kwh", None),
    ("grid_to_battery_kwh", "grid_to_battery_day_kwh", None),
    ("solar_to_house_kwh", "solar_to_house_day_kwh", None),
    ("battery_to_house_kwh", "battery_to_house_day_kwh", None),
    ("grid_export_kwh", "grid_export_day_kwh", "feed_in_revenue_ct"),
    ("consumer_heatpump_kwh", "consumer_heatpump_day_kwh", "consumer_heatpump_cost_ct"),
    (
        "consumer_heatingrod_kwh",
        "consumer_heatingrod_day_kwh",
        "consumer_heatingrod_cost_ct",
    ),
    ("consumer_wallbox_kwh", "consumer_wallbox_day_kwh", "consumer_wallbox_cost_ct"),
)


def scale_billing_rows(
    rows: list[dict[str, Any]],
    daily: dict[str, dict[str, float]],
    projection: dict[str, dict[str, float]] | None = None,
) -> None:
    """Scale hourly copies in memory. Stored cost fields are cleared so euros follow kWh."""
    rows_by_date = {}
    for billing_row in rows:
        rows_by_date.setdefault(str(billing_row.get("date")), []).append(billing_row)
    for day_key, day_hours in rows_by_date.items():
        daily_values = daily.get(day_key, {})
        for hourly_column, metric_id, cost_column in _HOUR_SCALE_COLUMNS:
            raw_column_key = f"raw_{hourly_column}"
            if raw_column_key not in daily_values and metric_id not in daily_values:
                continue
            raw_value_kwh = float(daily_values.get(raw_column_key, 0.0) or 0.0)
            corrected_value_kwh = float(
                daily_values.get(metric_id, raw_value_kwh) or 0.0
            )
            hourly_amounts = [
                float(hour_row.get(hourly_column) or 0.0) for hour_row in day_hours
            ]
            is_corrected = metric_id in (projection or {}).get(day_key, {})
            scale_basis_kwh = scaling_basis_kwh(
                raw_value_kwh, sum(hourly_amounts), corrected=is_corrected
            )
            scaled_amounts = scale_hourly_amounts(
                scale_basis_kwh, corrected_value_kwh, hourly_amounts
            )
            if scaled_amounts is None:
                continue
            for hour_row, hourly_value in zip(day_hours, scaled_amounts):
                if (
                    abs(hourly_value - float(hour_row.get(hourly_column) or 0.0))
                    <= 1e-09
                ):
                    continue
                hour_row[hourly_column] = hourly_value
                if cost_column and cost_column in hour_row:
                    hour_row[cost_column] = 0.0
            continue
        continue


def cost_source_name(hour_priced: int, tariff_priced: int) -> str:
    """hourly, hybrid, or fixed_fallback."""
    if hour_priced and tariff_priced:
        return "hybrid"
    if hour_priced:
        return "hourly"
    return "fixed_fallback"


def daily_row_kwh(row: dict[str, Any] | Any) -> float:
    """Grid kWh already corrected by the daily-cost read path."""
    try:
        return float(row.get("grid_kwh") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def daily_row_eur(row: dict[str, Any] | Any) -> float:
    """Euro already corrected by the daily-cost read path. Missing stays zero."""
    stored_energy_eur = row.get("energy_eur") if hasattr(row, "get") else None
    if stored_energy_eur is None:
        return 0.0
    try:
        return float(stored_energy_eur)
    except (TypeError, ValueError):
        return 0.0


def _cell(row: Any, index: int, key: str) -> Any:
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            return row[index]
    return row[index]


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _columns(db: Any, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        return set()
    try:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return set()
    column_names = set()
    for database_row in database_rows:
        if hasattr(database_row, "keys"):
            column_names.add(str(database_row["name"]))
        else:
            column_names.add(str(database_row[1]))
    return column_names


def _date_clause(start: str, end: str | None) -> tuple[str, tuple[Any, ...]]:
    if end is None:
        return ("date >= ?", (start,))
    return ("date >= ? AND date <= ?", (start, end))


async def corrected_daily(
    db: Any, start: str, end: str | None, projection: dict[str, dict[str, float]] | None
) -> dict[str, dict[str, float]]:
    """Corrected daily registry metrics keyed by date. Reads only."""
    present_daily_columns = await _columns(db, _DAILY_TABLE)
    metric_specs = [
        metric_spec
        for metric_spec in METRIC_REGISTRY
        if metric_spec["daily_column"] in present_daily_columns
    ]
    if not metric_specs:
        return {}
    selected_columns = [metric_spec["daily_column"] for metric_spec in metric_specs]
    date_clause, query_params = _date_clause(start, end)
    query = f"SELECT date, {', '.join(selected_columns)} FROM {_DAILY_TABLE} WHERE {date_clause} ORDER BY date"
    try:
        async with db.execute(query, query_params) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return {}
    return _daily_from_fetched(database_rows, metric_specs, projection)


def _pragma_names(rows: list[Any]) -> set[str]:
    column_names = set()
    for database_row in rows:
        if isinstance(database_row, dict) and database_row.get("name") is not None:
            column_names.add(str(database_row["name"]))
            continue
        try:
            column_names.add(str(database_row["name"]))
            continue
        except (KeyError, TypeError, IndexError):
            pass
        try:
            column_names.add(str(database_row[1]))
        except (KeyError, TypeError, IndexError):
            pass
    return column_names


def _daily_from_fetched(
    rows: list[Any],
    specs: list[dict[str, Any]],
    projection: dict[str, dict[str, float]] | None,
) -> dict[str, dict[str, float]]:
    values_by_date = {}
    for database_row in rows:
        day_key = str(_cell(database_row, 0, "date"))
        daily_values = {}
        for column_index, metric_spec in enumerate(specs, start=1):
            raw_value_kwh = _number(
                _cell(database_row, column_index, metric_spec["daily_column"])
            )
            daily_values[metric_spec["id"]] = corrected_value(
                projection, day_key, metric_spec["id"], raw_value_kwh
            )
            daily_values[f"raw_{metric_spec['daily_column']}"] = raw_value_kwh
        values_by_date[day_key] = daily_values
    return values_by_date


async def corrected_daily_read(
    read: Any,
    start: str,
    end: str | None,
    projection: dict[str, dict[str, float]] | None,
) -> dict[str, dict[str, float]]:
    """corrected_daily for a connection-manager read(query, params) callable."""
    try:
        pragma_rows = await read(f"PRAGMA table_info({_DAILY_TABLE})")
    except Exception:
        return {}
    present_daily_columns = _pragma_names(pragma_rows or [])
    metric_specs = [
        metric_spec
        for metric_spec in METRIC_REGISTRY
        if metric_spec["daily_column"] in present_daily_columns
    ]
    if not metric_specs or "date" not in present_daily_columns:
        return {}
    selected_columns = [metric_spec["daily_column"] for metric_spec in metric_specs]
    date_clause, query_params = _date_clause(start, end)
    query = f"SELECT date, {', '.join(selected_columns)} FROM {_DAILY_TABLE} WHERE {date_clause} ORDER BY date"
    try:
        database_rows = await read(query, list(query_params))
    except Exception:
        return {}
    return _daily_from_fetched(database_rows or [], metric_specs, projection)


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return


def _copy_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {column_name: row[column_name] for column_name in row.keys()}


_HOURLY_METRIC_BY_COLUMN = {
    item["hourly_column"]: item["id"]
    for item in METRIC_REGISTRY
    if item["hourly_column"] is not None
}


def scale_field_hours(
    rows: list[Any],
    daily: dict[str, dict[str, float]],
    projection: dict[str, dict[str, float]] | None,
    fields: tuple[str, ...] | list[str],
    *,
    present_columns: set[str] | frozenset[str] | None = None,
) -> list[Any]:
    """Scale hourly copies the way billing does. Uncorrected rows stay as they are."""
    indexed_rows_by_date = {}
    for row_index, input_row in enumerate(rows):
        day_key = _row_get(input_row, "date")
        if day_key is not None:
            indexed_rows_by_date.setdefault(str(day_key), []).append(
                (row_index, input_row)
            )
    modified_rows_by_index = {}
    active_projection = projection or {}
    for day_key, indexed_day_hours in indexed_rows_by_date.items():
        day_projection = active_projection.get(day_key) or {}
        if not day_projection:
            continue
        daily_values = daily.get(day_key, {})
        for field_column in fields:
            metric_id = _HOURLY_METRIC_BY_COLUMN.get(field_column)
            if metric_id is None or metric_id not in day_projection:
                continue
            if present_columns is not None:
                if field_column not in present_columns:
                    continue
            raw_column_key = f"raw_{field_column}"
            if raw_column_key not in daily_values and metric_id not in daily_values:
                continue
            raw_value_kwh = float(daily_values.get(raw_column_key, 0.0) or 0.0)
            corrected_value_kwh = float(
                daily_values.get(metric_id, raw_value_kwh) or 0.0
            )
            day_hours = [hour_row for unused_index, hour_row in indexed_day_hours]
            hourly_amounts = [
                float(_row_get(hour_row, field_column) or 0.0) for hour_row in day_hours
            ]
            scale_basis_kwh = scaling_basis_kwh(
                raw_value_kwh, sum(hourly_amounts), corrected=True
            )
            scaled_amounts = scale_hourly_amounts(
                scale_basis_kwh, corrected_value_kwh, hourly_amounts
            )
            if scaled_amounts is None:
                continue
            for (row_index, hour_row), hourly_value in zip(
                indexed_day_hours, scaled_amounts
            ):
                raw_hour_value = _row_get(hour_row, field_column)
                try:
                    original_hour_kwh = float(raw_hour_value or 0.0)
                except (TypeError, ValueError):
                    original_hour_kwh = 0.0
                if abs(hourly_value - original_hour_kwh) <= 1e-09:
                    continue
                modified_row = modified_rows_by_index.get(row_index)
                if modified_row is None:
                    modified_row = _copy_row(hour_row)
                    modified_rows_by_index[row_index] = modified_row
                modified_row[field_column] = hourly_value
            continue
        continue
    if not modified_rows_by_index:
        return rows
    return [
        modified_rows_by_index.get(row_index, input_row)
        for row_index, input_row in enumerate(rows)
    ]


_JOURNAL_COLUMNS = (
    ("solar_yield_kwh", "solar_yield_day_kwh", "solar_yield_kwh"),
    ("grid_import_kwh", "grid_import_day_kwh", "grid_import_kwh"),
    ("grid_export_kwh", "grid_export_day_kwh", "grid_export_kwh"),
    ("home_consumption_kwh", "home_consumption_day_kwh", "home_consumption_kwh"),
    ("solar_to_house_kwh", "solar_to_house_day_kwh", None),
    ("battery_to_house_kwh", "battery_to_house_day_kwh", None),
    ("grid_to_battery_kwh", "grid_to_battery_day_kwh", None),
    ("consumer_heatpump_kwh", "consumer_heatpump_day_kwh", "heat_pump_kwh"),
    ("consumer_heatingrod_kwh", "consumer_heatingrod_day_kwh", "heating_rod_kwh"),
    ("consumer_wallbox_kwh", "consumer_wallbox_day_kwh", "wallbox_kwh"),
)


def _listed_changed(values: dict[str, float], raw_key: str, metric_id: str) -> bool:
    if raw_key not in values or metric_id not in values:
        return False
    return not kwh_unchanged(values[raw_key], values[metric_id])


def _journal_metric_number(
    prepared: Mapping[str, Any], values: dict[str, float], column: str, metric_id: str
) -> float:
    if column in prepared and prepared.get(column) is not None:
        return _number(prepared.get(column))
    if metric_id in values:
        return _number(values[metric_id])
    return _number(values.get(f"raw_{column}"))


def overlay_journal_row(
    row: Mapping[str, Any],
    daily_values: dict[str, float] | None,
    projection_day: dict[str, float] | None,
) -> tuple[Mapping[str, Any], list[str]]:
    """Apply one day's correction overlay. An uncorrected row is returned unchanged."""
    active_projection = projection_day or {}
    if not active_projection:
        return (row, [])
    overlaid_row = dict(row)
    changed_journal_metrics = []
    changed = False
    for hourly_column, metric_id, journal_metric in _JOURNAL_COLUMNS:
        if metric_id not in active_projection:
            continue
        overlaid_row[hourly_column] = corrected_value(
            {str(overlaid_row.get("date")): active_projection},
            str(overlaid_row.get("date")),
            metric_id,
            overlaid_row.get(hourly_column),
        )
        changed = True
        if journal_metric and journal_metric not in changed_journal_metrics:
            changed_journal_metrics.append(journal_metric)
    day_values = daily_values or {}
    if day_values:
        if _self_supply_changed(day_values) or _listed_changed(
            day_values, "raw_home_consumption_kwh", "home_consumption_day_kwh"
        ):
            solar_to_house_kwh = _journal_metric_number(
                overlaid_row, day_values, "solar_to_house_kwh", "solar_to_house_day_kwh"
            )
            battery_to_house_kwh = _journal_metric_number(
                overlaid_row,
                day_values,
                "battery_to_house_kwh",
                "battery_to_house_day_kwh",
            )
            grid_to_battery_kwh = _journal_metric_number(
                overlaid_row,
                day_values,
                "grid_to_battery_kwh",
                "grid_to_battery_day_kwh",
            )
            own_energy_kwh = own_supply_kwh(
                solar_to_house_kwh, battery_to_house_kwh, grid_to_battery_kwh
            )
            consumption_kwh = overlaid_row.get("home_consumption_kwh")
            if consumption_kwh is not None:
                overlaid_row["autarkie_percent"] = autarky_percent(
                    own_energy_kwh, consumption_kwh
                )
                for journal_metric in ("own_energy_kwh", "autonomy_percent"):
                    if journal_metric not in changed_journal_metrics:
                        changed_journal_metrics.append(journal_metric)
                changed = True
    if day_values:
        if _listed_changed(
            day_values, "raw_solar_yield_kwh", "solar_yield_day_kwh"
        ) or _listed_changed(day_values, "raw_grid_export_kwh", "grid_export_day_kwh"):
            solar_yield_kwh = _journal_metric_number(
                overlaid_row, day_values, "solar_yield_kwh", "solar_yield_day_kwh"
            )
            grid_export_kwh = _journal_metric_number(
                overlaid_row, day_values, "grid_export_kwh", "grid_export_day_kwh"
            )
            overlaid_row["self_consumption_kwh"] = round(
                max(0.0, solar_yield_kwh - grid_export_kwh), 4
            )
            if "solar_use_percent" not in changed_journal_metrics:
                changed_journal_metrics.append("solar_use_percent")
            changed = True
    if not changed:
        return (row, [])
    return (overlaid_row, changed_journal_metrics)


async def corrected_daily_report(
    db: Any, start: str, end: str | None, projection: dict[str, dict[str, float]] | None
) -> dict[str, dict[str, float]]:
    """Report view: registry metrics plus the historical import/export blend."""
    corrected_days = await corrected_daily(db, start, end, projection)
    present_daily_columns = await _columns(db, _DAILY_TABLE)
    report_fields = [
        "grid_import_kwh",
        "smartmeter_import_kwh",
        "grid_to_house_kwh",
        "grid_to_battery_kwh",
        "grid_export_kwh",
        "smartmeter_export_kwh",
        "solar_yield_kwh",
    ]
    selected_report_columns = [
        hourly_column
        for hourly_column in report_fields
        if hourly_column in present_daily_columns
    ]
    if not selected_report_columns:
        return corrected_days
    date_clause, query_params = _date_clause(start, end)
    query = f"SELECT date, {', '.join(selected_report_columns)} FROM {_DAILY_TABLE} WHERE {date_clause}"
    try:
        async with db.execute(query, query_params) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return corrected_days
    for database_row in database_rows:
        day_key = str(_cell(database_row, 0, "date"))
        raw_day_values = {
            hourly_column: _number(_cell(database_row, column_index, hourly_column))
            for column_index, hourly_column in enumerate(
                selected_report_columns, start=1
            )
        }
        day_values = corrected_days.setdefault(day_key, {})
        raw_import_kwh = raw_day_values.get("smartmeter_import_kwh", 0.0)
        if raw_import_kwh <= 0.0:
            raw_import_kwh = max(
                raw_day_values.get("grid_import_kwh", 0.0),
                raw_day_values.get("grid_to_house_kwh", 0.0)
                + raw_day_values.get("grid_to_battery_kwh", 0.0),
            )
        raw_export_kwh = raw_day_values.get("smartmeter_export_kwh", 0.0)
        if raw_export_kwh <= 0.0:
            raw_export_kwh = raw_day_values.get("grid_export_kwh", 0.0)
        day_values["grid_import_day_kwh"] = corrected_value(
            projection, day_key, "grid_import_day_kwh", raw_import_kwh
        )
        day_values["grid_export_day_kwh"] = corrected_value(
            projection, day_key, "grid_export_day_kwh", raw_export_kwh
        )
        day_values["solar_yield_day_kwh"] = corrected_value(
            projection,
            day_key,
            "solar_yield_day_kwh",
            raw_day_values.get("solar_yield_kwh", 0.0),
        )
        day_values["raw_blended_grid_import_kwh"] = raw_import_kwh
        day_values["raw_grid_export_kwh"] = raw_export_kwh
        day_values["raw_solar_yield_kwh"] = raw_day_values.get("solar_yield_kwh", 0.0)
        continue
    return corrected_days


async def _gpm_hour_prices(
    db: Any, start: str, end: str | None
) -> dict[tuple[str, int], float]:
    if _GPM_HISTORY not in await _existing_tables(db):
        return {}
    start_timestamp = f"{start}T00:00:00"
    end_timestamp = f"{end}T24:00:00" if end else "9999-12-31T00:00:00"
    try:
        async with db.execute(
            f"SELECT timestamp, hour, total_price FROM {_GPM_HISTORY}\n                WHERE timestamp >= ? AND timestamp < ?\n                ORDER BY timestamp",
            (start_timestamp, end_timestamp),
        ) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return {}
    prices_by_hour = {}
    for database_row in database_rows:
        timestamp = str(_cell(database_row, 0, "timestamp") or "")
        day_key = timestamp[:10]
        try:
            parsed_hour = int(_cell(database_row, 1, "hour"))
        except (TypeError, ValueError):
            continue
        prices_by_hour[day_key, parsed_hour] = _number(
            _cell(database_row, 2, "total_price")
        )
    return prices_by_hour


async def _existing_tables(db: Any) -> set[str]:
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return set()
    table_names = set()
    for database_row in database_rows:
        table_names.add(str(_cell(database_row, 0, "name")))
    return table_names


async def _daily_averages(db: Any, start: str, end: str | None) -> dict[str, float]:
    if "average_total" not in await _columns(db, _GPM_AVERAGES):
        return {}
    date_clause, query_params = _date_clause(start, end)
    try:
        async with db.execute(
            f"SELECT date, average_total FROM {_GPM_AVERAGES} WHERE {date_clause}",
            query_params,
        ) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return {}
    average_by_date = {}
    for database_row in database_rows:
        average_by_date[str(_cell(database_row, 0, "date"))] = _number(
            _cell(database_row, 1, "average_total")
        )
    return average_by_date


def _present_price(value: Any) -> float | None:
    """Same gap rule as price_mode: missing or zero is not a price. Negatives bill."""
    if value in (None, ""):
        return
    try:
        price_value = float(value)
    except (TypeError, ValueError):
        return
    if price_value == 0.0:
        return
    return price_value


def _resolve_price(
    *,
    gpm_total: Any = None,
    stored_price: Any = None,
    daily_average: Any = None,
    tariff_fallback_ct: Any = None,
    price_mode: str | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[float | None, str | None]:
    """GPM hour price, then the stored hour price, then the daily average.

    The month tariff is only the last step. ``price_mode`` and ``config`` stay
    in the signature so callers match billing; legacy fixed price is applied by
    the billing legacy branch, not here.
    """
    del price_mode, config
    for candidate_price in (gpm_total, stored_price, daily_average):
        resolved_price_ct = _present_price(candidate_price)
        if not resolved_price_ct is not None:
            continue
        return (resolved_price_ct, "hour")
    fallback_price_value_ct = _present_price(tariff_fallback_ct)
    if fallback_price_value_ct is None:
        return (None, None)
    return (fallback_price_value_ct, "tariff")


async def import_period_cost(
    db: Any,
    start: str,
    end: str | None,
    projection: dict[str, dict[str, float]] | None,
    *,
    tariff_fallback_ct: float | None = None,
    price_mode: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grid-import energy cost for one period. Reads GPM prices; writes nothing.

    A corrected day scales hourly grid import by corrected/raw. A backfill with
    no hourly rows uses the GPM daily average, then the month tariff.
    """
    corrected_days = await corrected_daily(db, start, end, projection)
    gpm_hour_prices = await _gpm_hour_prices(db, start, end)
    daily_average_prices = await _daily_averages(db, start, end)
    hourly_columns = await _columns(db, _HOURLY_TABLE)
    hours_by_date = {}
    if "grid_import_kwh" in hourly_columns and "hour" in hourly_columns:
        selected_hour_columns = ["date", "hour", "grid_import_kwh"]
        for optional_column in (
            "grid_to_house_kwh",
            "grid_to_battery_kwh",
            "price_ct_kwh",
        ):
            if optional_column in hourly_columns:
                selected_hour_columns.append(optional_column)
        date_clause, query_params = _date_clause(start, end)
        try:
            async with db.execute(
                f"SELECT {', '.join(selected_hour_columns)} FROM {_HOURLY_TABLE} WHERE {date_clause} ORDER BY date, hour",
                query_params,
            ) as cursor:
                hourly_rows = await cursor.fetchall()
        except Exception:
            hourly_rows = []
        for database_row in hourly_rows:
            day_key = str(_cell(database_row, 0, "date"))
            try:
                hour_item = int(_cell(database_row, 1, "hour"))
            except (TypeError, ValueError):
                hour_item = 0
            hour_record = {
                "hour": hour_item,
                "grid_import_kwh": _number(_cell(database_row, 2, "grid_import_kwh")),
                "grid_to_house_kwh": 0.0,
                "grid_to_battery_kwh": 0.0,
                "price_ct_kwh": None,
            }
            for field_column in (
                "grid_to_house_kwh",
                "grid_to_battery_kwh",
                "price_ct_kwh",
            ):
                if field_column in selected_hour_columns:
                    hour_record[field_column] = _cell(
                        database_row,
                        selected_hour_columns.index(field_column),
                        field_column,
                    )
                    if field_column != "price_ct_kwh":
                        hour_record[field_column] = _number(hour_record[field_column])
            hours_by_date.setdefault(day_key, []).append(hour_record)
    energy_cost_ct = 0.0
    priced_import_kwh = 0.0
    unpriced_import_kwh = 0.0
    hour_price_count = 0
    tariff_price_count = 0
    missing_price_count = 0
    days_with_hours = set()
    for day_key, day_hours in hours_by_date.items():
        days_with_hours.add(day_key)
        raw_daily_import_kwh = corrected_days.get(day_key, {}).get(
            "raw_grid_import_kwh", 0.0
        )
        corrected_import_kwh = corrected_value(
            projection, day_key, "grid_import_day_kwh", raw_daily_import_kwh
        )
        raw_hourly_imports = [hour_item["grid_import_kwh"] for hour_item in day_hours]
        is_corrected = "grid_import_day_kwh" in (projection or {}).get(day_key, {})
        scale_basis_kwh = scaling_basis_kwh(
            raw_daily_import_kwh, sum(raw_hourly_imports), corrected=is_corrected
        )
        scaled_hourly_imports = (
            scale_hourly_amounts(
                scale_basis_kwh, corrected_import_kwh, raw_hourly_imports
            )
            or []
        )
        energy_changed = abs(corrected_import_kwh - raw_daily_import_kwh) > 1e-09 or (
            raw_daily_import_kwh <= 0.0 and corrected_import_kwh > 0.0
        )
        for hour_item, scaled_hour_kwh in zip(day_hours, scaled_hourly_imports):
            if energy_changed:
                billable_import_kwh = scaled_hour_kwh
            else:
                hourly_flow_import_kwh = _number(
                    hour_item["grid_to_house_kwh"]
                ) + _number(hour_item["grid_to_battery_kwh"])
                billable_import_kwh = max(scaled_hour_kwh, hourly_flow_import_kwh)
            resolved_price_ct, price_source = _resolve_price(
                gpm_total=gpm_hour_prices.get((day_key, int(hour_item["hour"]))),
                stored_price=hour_item["price_ct_kwh"],
                tariff_fallback_ct=tariff_fallback_ct,
                price_mode=price_mode,
                config=config,
            )
            if resolved_price_ct is None or price_source is None:
                missing_price_count += 1
                unpriced_import_kwh += billable_import_kwh
            else:
                energy_cost_ct += billable_import_kwh * resolved_price_ct
                priced_import_kwh += billable_import_kwh
                if price_source == "tariff":
                    tariff_price_count += 1
                else:
                    hour_price_count += 1
        continue
    for day_key, daily_values in corrected_days.items():
        if day_key in days_with_hours:
            raw_grid_to_battery_kwh = daily_values.get("raw_grid_to_battery_kwh", 0.0)
            corrected_grid_to_battery_kwh = daily_values.get(
                "grid_to_battery_day_kwh", raw_grid_to_battery_kwh
            )
            hourly_grid_to_battery_kwh = sum(
                (
                    _number(hour_item["grid_to_battery_kwh"])
                    for hour_item in hours_by_date.get(day_key, [])
                )
            )
            battery_charge_remainder_kwh = max(
                0.0, corrected_grid_to_battery_kwh - hourly_grid_to_battery_kwh
            )
            if battery_charge_remainder_kwh <= 0.0:
                continue
            resolved_price_ct, price_source = _resolve_price(
                daily_average=daily_average_prices.get(day_key),
                tariff_fallback_ct=tariff_fallback_ct,
                price_mode=price_mode,
                config=config,
            )
            if resolved_price_ct is None or price_source is None:
                unpriced_import_kwh += battery_charge_remainder_kwh
                continue
            energy_cost_ct += battery_charge_remainder_kwh * resolved_price_ct
            priced_import_kwh += battery_charge_remainder_kwh
            if price_source == "tariff":
                tariff_price_count += 1
            else:
                hour_price_count += 1
            continue
        raw_daily_import_kwh = daily_values.get("raw_grid_import_kwh", 0.0)
        corrected_import_kwh = corrected_value(
            projection, day_key, "grid_import_day_kwh", raw_daily_import_kwh
        )
        energy_changed = abs(corrected_import_kwh - raw_daily_import_kwh) > 1e-09 or (
            raw_daily_import_kwh <= 0.0 and corrected_import_kwh > 0.0
        )
        if energy_changed:
            billable_import_kwh = corrected_import_kwh
        else:
            billable_import_kwh = daily_values.get(
                "raw_grid_to_house_kwh", 0.0
            ) + daily_values.get("raw_grid_to_battery_kwh", 0.0)
        resolved_price_ct, price_source = _resolve_price(
            daily_average=daily_average_prices.get(day_key),
            tariff_fallback_ct=tariff_fallback_ct,
            price_mode=price_mode,
            config=config,
        )
        if resolved_price_ct is None or price_source is None:
            missing_price_count += 1
            unpriced_import_kwh += billable_import_kwh
            continue
        energy_cost_ct += billable_import_kwh * resolved_price_ct
        priced_import_kwh += billable_import_kwh
        if price_source == "tariff":
            tariff_price_count += 1
            continue
        hour_price_count += 1
        continue
    return {
        "energy_eur": round(energy_cost_ct / 100.0, 2),
        "energy_ct": energy_cost_ct,
        "kwh": priced_import_kwh,
        "avg_ct": (
            round(energy_cost_ct / priced_import_kwh, 2)
            if priced_import_kwh > 0.0
            else 0.0
        ),
        "hours_without_price": missing_price_count,
        "unpriced_kwh": unpriced_import_kwh,
        "cost_source": cost_source_name(hour_price_count, tariff_price_count),
    }


async def monthly_table_energy_cost(
    db: Any,
    start: str,
    end: str | None,
    projection: dict[str, dict[str, float]] | None,
    month_tariff_ct: float | None,
    *,
    price_mode: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fixed-tariff month table: GPM hour prices, month tariff only for gaps."""
    return await import_period_cost(
        db,
        start,
        end,
        projection,
        tariff_fallback_ct=month_tariff_ct,
        price_mode=price_mode,
        config=config,
    )


async def statement_energy_cost(
    db: Any,
    start: str,
    end: str | None,
    projection: dict[str, dict[str, float]] | None,
    *,
    tariff_fallback_ct: float | None = None,
    price_mode: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Meine Stromrechnung energy cost. Same calculator as the month table."""
    return await import_period_cost(
        db,
        start,
        end,
        projection,
        tariff_fallback_ct=tariff_fallback_ct,
        price_mode=price_mode,
        config=config,
    )


async def grid_import_factors(
    db: Any, start: str, end: str | None, projection: dict[str, dict[str, float]] | None
) -> dict[str, tuple[float, float]]:
    """date -> (raw grid import, corrected grid import) from the daily column."""
    corrected_days = await corrected_daily(db, start, end, projection)
    import_factors = {}
    for day_key, daily_values in corrected_days.items():
        raw_value_kwh = daily_values.get("raw_grid_import_kwh", 0.0)
        corrected_value_kwh = corrected_value(
            projection, day_key, "grid_import_day_kwh", raw_value_kwh
        )
        import_factors[day_key] = (raw_value_kwh, corrected_value_kwh)
    return import_factors


RAW_COST_KWH_MIN = 0.01
HOUR_SUM_ABS_KWH = 1.0
HOUR_SUM_RELATIVE = 0.5


def kwh_unchanged(raw_kwh: float, corrected_kwh: float) -> bool:
    return abs(_number(corrected_kwh) - _number(raw_kwh)) <= 1e-09


def scaling_basis_kwh(
    raw_kwh: float, hour_sum: float | None, *, corrected: bool
) -> float:
    """Denominator for corrected-day scaling.

    Uncorrected days and days whose hour sum stays within max(1 kWh, 50 % of
    the daily raw value) keep the daily raw value. A corrected day whose hours
    diverge uses the hour sum, including when the corrected total equals the
    daily raw value.
    """
    raw_value_kwh = _number(raw_kwh)
    if not corrected or hour_sum is None:
        return raw_value_kwh
    hour_sum_value_kwh = _number(hour_sum)
    if hour_sum_value_kwh <= 0.0:
        return raw_value_kwh
    hour_sum_tolerance = max(HOUR_SUM_ABS_KWH, HOUR_SUM_RELATIVE * abs(raw_value_kwh))
    if abs(hour_sum_value_kwh - raw_value_kwh) > hour_sum_tolerance:
        return hour_sum_value_kwh
    return raw_value_kwh


def corrected_day_cost(
    raw_kwh: float,
    corrected_kwh: float,
    raw_cost: float,
    *,
    fallback_price_ct: float | None = None,
    cost_in_cent: bool = False,
    hour_sum: float | None = None,
    corrected: bool = False,
) -> float:
    """Money for one corrected day, in the same unit as ``raw_cost``.

    At least 0.01 raw kWh keeps that day's price (raw cost / raw kWh), up or
    down. Below that, ``fallback_price_ct`` is the price-chain result.
    Equal kWh returns ``raw_cost`` unchanged unless a corrected day must scale
    from its hour sum.
    """
    raw_value_kwh = _number(raw_kwh)
    corrected_value_kwh = _number(corrected_kwh)
    raw_cost_value = _number(raw_cost)
    scale_basis_kwh = scaling_basis_kwh(raw_value_kwh, hour_sum, corrected=corrected)
    if kwh_unchanged(scale_basis_kwh, corrected_value_kwh):
        return raw_cost_value
    if scale_basis_kwh >= RAW_COST_KWH_MIN:
        return corrected_value_kwh * (raw_cost_value / scale_basis_kwh)
    resolved_price_ct = _present_price(fallback_price_ct)
    if resolved_price_ct is None:
        return 0.0
    unit_price = resolved_price_ct if cost_in_cent else resolved_price_ct / 100.0
    return corrected_value_kwh * unit_price


def own_supply_kwh(
    solar_to_house: float, battery_to_house: float, grid_to_battery: float
) -> float:
    """PV to the house plus battery discharge that was not grid-charged."""
    return _number(solar_to_house) + max(
        0.0, _number(battery_to_house) - _number(grid_to_battery)
    )


def autarky_percent(self_kwh: float, consumption_kwh: float) -> float:
    consumption_value_kwh = _number(consumption_kwh)
    if consumption_value_kwh <= 0.0:
        return 0.0
    return min(100.0, _number(self_kwh) / consumption_value_kwh * 100.0)


def correction_remainder(
    daily_value: float, hourly_value: float, *, corrected: bool
) -> float:
    """kWh in the daily row that the hour rows do not already contain.

    Uncorrected days stay one-sided, matching the previous month backfill.
    A corrected day may reduce as well as increase the remainder.
    """
    remainder_kwh = _number(daily_value) - _number(hourly_value)
    if corrected:
        return remainder_kwh
    return max(0.0, remainder_kwh)


def covered_day_cost(
    raw_kwh: float,
    corrected_kwh: float,
    covered_kwh: float,
    covered_cost: float,
    uncovered_price_ct: float,
    *,
    fallback_price_ct: float | None = None,
) -> float:
    """One day of import cost, self-consumption savings, or feed-in revenue."""
    raw_value_kwh = _number(raw_kwh)
    uncovered_kwh = max(0.0, raw_value_kwh - _number(covered_kwh))
    day_cost_eur = (
        _number(covered_cost) + uncovered_kwh * _number(uncovered_price_ct) / 100.0
    )
    fallback_price_value_ct = (
        uncovered_price_ct if fallback_price_ct is None else fallback_price_ct
    )
    return corrected_day_cost(
        raw_value_kwh,
        corrected_kwh,
        day_cost_eur,
        fallback_price_ct=fallback_price_value_ct,
    )


def legacy_dynamic_month(
    *,
    import_kwh: float,
    covered_import_kwh: float,
    covered_import_cost: float,
    self_kwh: float,
    covered_self_kwh: float,
    covered_self_savings: float,
    export_kwh: float,
    covered_export_kwh: float,
    covered_export_revenue: float,
    month_price_ct: float,
    month_feed_in_ct: float,
) -> dict[str, float]:
    """Previous month formula: only the positive gap is priced at the month tariff."""
    uncovered_import_kwh = max(0.0, _number(import_kwh) - _number(covered_import_kwh))
    uncovered_self_kwh = max(0.0, _number(self_kwh) - _number(covered_self_kwh))
    uncovered_export_kwh = max(0.0, _number(export_kwh) - _number(covered_export_kwh))
    return {
        "energy_cost": _number(covered_import_cost)
        + uncovered_import_kwh * _number(month_price_ct) / 100.0,
        "self_savings": _number(covered_self_savings)
        + uncovered_self_kwh * _number(month_price_ct) / 100.0,
        "feed_in_revenue": _number(covered_export_revenue)
        + uncovered_export_kwh * _number(month_feed_in_ct) / 100.0,
    }


def dynamic_month_average(
    *, priced_cost_eur: float, priced_import_kwh: float, total_import_kwh: float
) -> tuple[float | None, float, float]:
    """Average ct/kWh over priced import only. The gap is not priced.

    Returns ``(avg_ct or None, energy_eur, unpriced_import_kwh)``.
    """
    try:
        priced_cost_value_eur = float(priced_cost_eur)
    except (TypeError, ValueError):
        priced_cost_value_eur = 0.0
    try:
        priced_import_value_kwh = float(priced_import_kwh)
    except (TypeError, ValueError):
        priced_import_value_kwh = 0.0
    try:
        total_import_value_kwh = float(total_import_kwh)
    except (TypeError, ValueError):
        total_import_value_kwh = 0.0
    if priced_import_value_kwh < 0.0:
        priced_import_value_kwh = 0.0
    average_price_ct = (
        priced_cost_value_eur * 100.0 / priced_import_value_kwh
        if priced_import_value_kwh > 0.0
        else None
    )
    unpriced_kwh = max(0.0, total_import_value_kwh - priced_import_value_kwh)
    return (average_price_ct, priced_cost_value_eur, unpriced_kwh)


def projected_dynamic_month(
    days: list[dict[str, float]],
    *,
    month_price_ct: float,
    month_feed_in_ct: float,
    import_fallback_ct: float | None = None,
) -> dict[str, float]:
    """Each day uses its own raw price. Uncorrected days keep their raw day cost."""
    energy_eur = 0.0
    self_savings_eur = 0.0
    feed_in_revenue_eur = 0.0
    import_fallback_value_ct = (
        month_price_ct if import_fallback_ct is None else import_fallback_ct
    )
    for day_record in days:
        energy_eur += covered_day_cost(
            day_record["raw_import"],
            day_record["corrected_import"],
            day_record["covered_import"],
            day_record["covered_import_cost"],
            month_price_ct,
            fallback_price_ct=import_fallback_value_ct,
        )
        self_savings_eur += covered_day_cost(
            day_record["raw_self"],
            day_record["corrected_self"],
            day_record["covered_self"],
            day_record["covered_self_savings"],
            month_price_ct,
            fallback_price_ct=import_fallback_value_ct,
        )
        feed_in_revenue_eur += covered_day_cost(
            day_record["raw_export"],
            day_record["corrected_export"],
            day_record["covered_export"],
            day_record["covered_export_revenue"],
            month_feed_in_ct,
            fallback_price_ct=month_feed_in_ct,
        )
    return {
        "energy_cost": energy_eur,
        "self_savings": self_savings_eur,
        "feed_in_revenue": feed_in_revenue_eur,
    }


def dynamic_month_costs(
    days: list[dict[str, float]] | None,
    *,
    changed: bool,
    legacy: dict[str, float],
    month_price_ct: float,
    month_feed_in_ct: float,
    import_fallback_ct: float | None = None,
) -> dict[str, float]:
    """Uncorrected months return the caller-supplied legacy totals unchanged."""
    if not changed:
        return {
            "energy_cost": _number(legacy["energy_cost"]),
            "self_savings": _number(legacy["self_savings"]),
            "feed_in_revenue": _number(legacy["feed_in_revenue"]),
        }
    return projected_dynamic_month(
        days or [],
        month_price_ct=month_price_ct,
        month_feed_in_ct=month_feed_in_ct,
        import_fallback_ct=import_fallback_ct,
    )


def energy_flows_changed(values: dict[str, float]) -> bool:
    """True when import, export, or own-supply kWh on this day was corrected."""
    raw_import_key = (
        "raw_blended_grid_import_kwh"
        if "raw_blended_grid_import_kwh" in values
        else "raw_grid_import_kwh"
    )
    if _pair_changed(values, raw_import_key, "grid_import_day_kwh"):
        return True
    if _pair_changed(values, "raw_grid_export_kwh", "grid_export_day_kwh"):
        return True
    return _self_supply_changed(values)


def _pair_changed(values: dict[str, float], raw_key: str, corrected_key: str) -> bool:
    if raw_key not in values or corrected_key not in values:
        return False
    return not kwh_unchanged(values[raw_key], values[corrected_key])


def _self_supply_changed(values: dict[str, float]) -> bool:
    self_supply_pairs = (
        ("raw_solar_to_house_kwh", "solar_to_house_day_kwh"),
        ("raw_battery_to_house_kwh", "battery_to_house_day_kwh"),
        ("raw_grid_to_battery_kwh", "grid_to_battery_day_kwh"),
    )
    if any(
        (
            raw_column_key not in values or corrected_metric_key not in values
            for raw_column_key, corrected_metric_key in self_supply_pairs
        )
    ):
        return False
    raw_self_kwh = own_supply_kwh(
        *(
            values[raw_column_key]
            for raw_column_key, unused_corrected_key in self_supply_pairs
        )
    )
    corrected_self_kwh = own_supply_kwh(
        *(
            values[corrected_metric_key]
            for unused_raw_key, corrected_metric_key in self_supply_pairs
        )
    )
    return not kwh_unchanged(raw_self_kwh, corrected_self_kwh)


def component_day(
    values: dict[str, float], covered: dict[str, float] | None
) -> dict[str, float]:
    """Join one corrected daily row with that day's hourly money."""
    covered = covered or {}
    raw_import_kwh = values.get(
        "raw_blended_grid_import_kwh", values.get("raw_grid_import_kwh", 0.0)
    )
    raw_export_kwh = values.get(
        "raw_grid_export_kwh", values.get("grid_export_day_kwh", 0.0)
    )
    raw_self_kwh = own_supply_kwh(
        values.get("raw_solar_to_house_kwh", 0.0),
        values.get("raw_battery_to_house_kwh", 0.0),
        values.get("raw_grid_to_battery_kwh", 0.0),
    )
    corrected_self_kwh = own_supply_kwh(
        values.get("solar_to_house_day_kwh", values.get("raw_solar_to_house_kwh", 0.0)),
        values.get(
            "battery_to_house_day_kwh", values.get("raw_battery_to_house_kwh", 0.0)
        ),
        values.get(
            "grid_to_battery_day_kwh", values.get("raw_grid_to_battery_kwh", 0.0)
        ),
    )
    return {
        "raw_import": _number(raw_import_kwh),
        "corrected_import": _number(values.get("grid_import_day_kwh", raw_import_kwh)),
        "covered_import": _number(covered.get("import_kwh")),
        "covered_import_cost": _number(covered.get("import_cost")),
        "raw_self": raw_self_kwh,
        "corrected_self": corrected_self_kwh,
        "covered_self": _number(covered.get("self_kwh")),
        "covered_self_savings": _number(covered.get("self_savings")),
        "raw_export": _number(raw_export_kwh),
        "corrected_export": _number(values.get("grid_export_day_kwh", raw_export_kwh)),
        "covered_export": _number(covered.get("export_kwh")),
        "covered_export_revenue": _number(covered.get("export_revenue")),
    }


def yesterday_figures(
    *,
    raw_consumption: float,
    consumption: float,
    raw_solar_to_house: float,
    solar_to_house: float,
    raw_battery_to_house: float,
    battery_to_house: float,
    raw_grid_to_battery: float,
    grid_to_battery: float,
    raw_export: float,
    export_kwh: float,
    stored_autarky: float | None,
    stored_savings: float | None,
    stored_revenue: float | None,
    import_fallback_ct: float | None,
    feed_in_tariff_ct: float | None,
) -> dict[str, float | None]:
    """Autarky, savings, and feed-in for one completed day.

    With no corrected kWh the stored figures are returned as they are.
    """
    raw_self_kwh = own_supply_kwh(
        raw_solar_to_house, raw_battery_to_house, raw_grid_to_battery
    )
    corrected_self_kwh = own_supply_kwh(
        solar_to_house, battery_to_house, grid_to_battery
    )
    self_supply_changed = not kwh_unchanged(raw_self_kwh, corrected_self_kwh)
    consumption_changed = not kwh_unchanged(raw_consumption, consumption)
    export_changed = not kwh_unchanged(raw_export, export_kwh)
    if self_supply_changed or consumption_changed:
        autarky_value = autarky_percent(corrected_self_kwh, consumption)
    else:
        autarky_value = stored_autarky
    if self_supply_changed:
        self_savings_eur = corrected_day_cost(
            raw_self_kwh,
            corrected_self_kwh,
            _number(stored_savings),
            fallback_price_ct=import_fallback_ct,
        )
    else:
        self_savings_eur = stored_savings
    if export_changed:
        feed_in_revenue_eur = corrected_day_cost(
            raw_export,
            export_kwh,
            _number(stored_revenue),
            fallback_price_ct=feed_in_tariff_ct,
        )
    else:
        feed_in_revenue_eur = stored_revenue
    return {
        "autarky_percent": autarky_value,
        "savings_eur": self_savings_eur,
        "feed_in_revenue_eur": feed_in_revenue_eur,
    }


def day_autarky_percent(
    *,
    stored: float | None,
    raw_consumption: float,
    consumption: float,
    raw_solar_to_house: float,
    solar_to_house: float,
    raw_battery_to_house: float,
    battery_to_house: float,
    raw_grid_to_battery: float,
    grid_to_battery: float,
) -> float | None:
    """Stored autarky when the kWh are unchanged, otherwise the corrected figure."""
    raw_self_kwh = own_supply_kwh(
        raw_solar_to_house, raw_battery_to_house, raw_grid_to_battery
    )
    corrected_self_kwh = own_supply_kwh(
        solar_to_house, battery_to_house, grid_to_battery
    )
    if kwh_unchanged(raw_self_kwh, corrected_self_kwh) and kwh_unchanged(
        raw_consumption, consumption
    ):
        if stored is None:
            return
        return _number(stored)
    return autarky_percent(corrected_self_kwh, consumption)


def window_autarky_average(days: list[dict[str, float | None]]) -> float | None:
    """Mean autarky after corrections. None means every day still matches the stored value."""
    changed = False
    autarky_values = []
    for day_record in days:
        day_autarky = day_autarky_percent(
            stored=day_record.get("stored"),
            raw_consumption=day_record.get("raw_consumption") or 0.0,
            consumption=day_record.get("consumption") or 0.0,
            raw_solar_to_house=day_record.get("raw_solar_to_house") or 0.0,
            solar_to_house=day_record.get("solar_to_house") or 0.0,
            raw_battery_to_house=day_record.get("raw_battery_to_house") or 0.0,
            battery_to_house=day_record.get("battery_to_house") or 0.0,
            raw_grid_to_battery=day_record.get("raw_grid_to_battery") or 0.0,
            grid_to_battery=day_record.get("grid_to_battery") or 0.0,
        )
        stored_autarky_value = day_record.get("stored")
        if stored_autarky_value is None:
            matches_stored = day_autarky is None
        elif day_autarky is None:
            matches_stored = False
        else:
            matches_stored = (
                abs(_number(day_autarky) - _number(stored_autarky_value)) <= 1e-09
            )
        if not matches_stored:
            changed = True
        if day_autarky is None:
            continue
        autarky_values.append(day_autarky)
        continue
    if not changed or not autarky_values:
        return None
    return sum(autarky_values) / len(autarky_values)


def projected_import_cost_totals(
    days: list[dict[str, float | None]],
) -> tuple[float, float] | None:
    """Sum of corrected import kWh and cost. None when no import kWh changed."""
    changed = False
    total_import_kwh = 0.0
    total_energy_eur = 0.0
    for day_record in days:
        raw_import_kwh = _number(day_record.get("raw_import"))
        corrected_import_value_kwh = _number(day_record.get("corrected_import"))
        raw_cost_eur = _number(day_record.get("raw_cost"))
        if not kwh_unchanged(raw_import_kwh, corrected_import_value_kwh):
            changed = True
        total_import_kwh += corrected_import_value_kwh
        total_energy_eur += corrected_day_cost(
            raw_import_kwh,
            corrected_import_value_kwh,
            raw_cost_eur,
            fallback_price_ct=day_record.get("fallback_price_ct"),
        )
    if not changed:
        return
    return (total_import_kwh, total_energy_eur)


def scale_hour_money(
    raw_kwh: float,
    corrected_kwh: float,
    hour_kwh: list[float],
    hour_cost: list[float],
    *,
    fallback_price_ct: float | None = None,
    cost_in_cent: bool = False,
    hour_sum: float | None = None,
    corrected: bool = False,
) -> tuple[list[float], list[float]]:
    """Scale one metric's hours. Equal daily kWh leaves both lists unchanged."""
    raw_value_kwh = _number(raw_kwh)
    corrected_value_kwh = _number(corrected_kwh)
    effective_hour_sum = (
        sum((_number(hourly_value) for hourly_value in hour_kwh))
        if hour_sum is None
        else hour_sum
    )
    scale_basis_kwh = scaling_basis_kwh(
        raw_value_kwh, effective_hour_sum, corrected=corrected
    )
    if kwh_unchanged(scale_basis_kwh, corrected_value_kwh):
        return (list(hour_kwh), list(hour_cost))
    scaled_amounts = scale_hourly_amounts(
        scale_basis_kwh, corrected_value_kwh, hour_kwh
    )
    if scaled_amounts is None:
        return (list(hour_kwh), list(hour_cost))
    if scale_basis_kwh >= RAW_COST_KWH_MIN:
        scale_factor = corrected_value_kwh / scale_basis_kwh
        return (
            scaled_amounts,
            [_number(hour_cost_value) * scale_factor for hour_cost_value in hour_cost],
        )
    resolved_price_ct = _present_price(fallback_price_ct)
    if resolved_price_ct is None:
        return (scaled_amounts, [0.0 for unused_hour in hour_cost])
    unit_price = resolved_price_ct if cost_in_cent else resolved_price_ct / 100.0
    return (
        scaled_amounts,
        [_number(hourly_value) * unit_price for hourly_value in scaled_amounts],
    )


def apply_grid_import_factor(
    raw: float,
    corrected: float,
    hourly_kwh: float,
    hourly_eur: float,
    priced_kwh: float,
    *,
    hour_sum: float | None = None,
    corrected_day: bool = False,
) -> tuple[float, float, float]:
    """Scale one day's aggregated hourly import. raw == 0 leaves the bucket for the caller."""
    scale_basis_kwh = scaling_basis_kwh(raw, hour_sum, corrected=corrected_day)
    if scale_basis_kwh > 0.0:
        scale_factor = corrected / scale_basis_kwh
        if scale_basis_kwh >= RAW_COST_KWH_MIN:
            scaled_hour_eur = corrected_day_cost(
                raw, corrected, hourly_eur, hour_sum=hour_sum, corrected=corrected_day
            )
        else:
            scaled_hour_eur = hourly_eur * scale_factor
        return (hourly_kwh * scale_factor, scaled_hour_eur, priced_kwh * scale_factor)
    return (hourly_kwh, hourly_eur, priced_kwh)


async def load_hourly_component_days(
    db: Any, start: str, end: str | None, fallback_price_ct: float
) -> dict[str, dict[str, float]]:
    """Per-day hourly kWh and money used as the raw price base."""
    if _HOURLY_TABLE not in await _existing_tables(db):
        return {}
    present_hourly_columns = await _columns(db, _HOURLY_TABLE)
    if "date" not in present_hourly_columns:
        return {}
    price_sql_expression = (
        "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
        if "price_ct_kwh" in present_hourly_columns
        else "?"
    )
    import_cost_sql = (
        "COALESCE(grid_import_cost_ct, 0)"
        if "grid_import_cost_ct" in present_hourly_columns
        else "0"
    )
    export_revenue_sql = (
        "COALESCE(feed_in_revenue_ct, 0)"
        if "feed_in_revenue_ct" in present_hourly_columns
        else "0"
    )
    own_supply_sql = "COALESCE(solar_to_house_kwh, 0) + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))"
    date_clause = "date >= ?"
    query_params = [fallback_price_ct, start]
    if end is not None:
        date_clause += " AND date <= ?"
        query_params.append(end)
    query = f"SELECT date, SUM(COALESCE(grid_import_kwh, 0)), SUM({import_cost_sql}) / 100.0, SUM(COALESCE(grid_export_kwh, 0)), SUM({export_revenue_sql}) / 100.0, SUM({own_supply_sql}), SUM(({own_supply_sql}) * {price_sql_expression}) / 100.0 FROM {_HOURLY_TABLE} WHERE {date_clause} GROUP BY date"
    try:
        async with db.execute(query, tuple(query_params)) as cursor:
            database_rows = await cursor.fetchall()
    except Exception:
        return {}
    components_by_date = {}
    for database_row in database_rows:
        components_by_date[str(_cell(database_row, 0, "date"))] = {
            "import_kwh": _number(_cell(database_row, 1, "import_kwh")),
            "import_cost": _number(_cell(database_row, 2, "import_cost")),
            "export_kwh": _number(_cell(database_row, 3, "export_kwh")),
            "export_revenue": _number(_cell(database_row, 4, "export_revenue")),
            "self_kwh": _number(_cell(database_row, 5, "self_kwh")),
            "self_savings": _number(_cell(database_row, 6, "self_savings")),
        }
    return components_by_date


async def projection_from_runtime(start: str | None) -> dict[str, dict[str, float]]:
    """Active correction projection, or empty when no runtime store is present."""
    try:
        from ..api.common import (
            _energy_correction_projection as runtime_energy_projection,
        )

        return await runtime_energy_projection(start)
    except Exception:
        return {}
