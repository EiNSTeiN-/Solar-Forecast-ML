# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Customer utility-invoice archive. Never writes GPM tables or hourly prices."""

from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Any, Mapping

TABLE = "stats_invoice_archive"
SOURCE_CUSTOMER = "customer"
_DATE_FORMAT = "%Y-%m-%d"
_OPTIONAL_NUMBERS = (
    "base_fee_eur",
    "net_energy_price_ct",
    "solar_kwh",
    "feed_in_eur",
    "savings_eur",
)


class InvoiceArchiveError(ValueError):
    """Stable invoice-archive error with a machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.key = code


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_date(value: Any, code: str) -> date:
    if not (isinstance(value, str) and value.strip()):
        raise InvoiceArchiveError(code)
    try:
        parsed_date = datetime.strptime(value.strip(), _DATE_FORMAT).date()
    except ValueError as error:
        raise InvoiceArchiveError(code) from error
    if parsed_date.strftime(_DATE_FORMAT) != value.strip():
        raise InvoiceArchiveError(code)
    return parsed_date


def _required_positive(value: Any, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise InvoiceArchiveError(code) from error
    if number != number or number <= 0.0:
        raise InvoiceArchiveError(code)
    return number


def _optional_non_negative(value: Any) -> float | None:
    if value is None or value == "":
        return
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise InvoiceArchiveError("invalid_optional") from error
    if number != number or number < 0.0:
        raise InvoiceArchiveError("invalid_optional")
    return number


def _optional_text(value: Any) -> str | None:
    if value is None:
        return
    if not isinstance(value, str):
        raise InvoiceArchiveError("invalid_payload")
    text = value.strip()
    return text or None


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    row_keys = getattr(row, "keys", None)
    if callable(row_keys) and key in row_keys():
        return row[key]
    return row[index]


def validate_invoice(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a create/update payload or raise InvoiceArchiveError."""
    if not isinstance(payload, Mapping):
        raise InvoiceArchiveError("invalid_payload")
    period_from = _parse_date(payload.get("period_from"), "invalid_date")
    period_to = _parse_date(payload.get("period_to"), "invalid_date")
    if period_from >= period_to:
        raise InvoiceArchiveError("invalid_period")
    invoice_id = payload.get("id")
    parsed_id = None
    if invoice_id not in (None, ""):
        try:
            parsed_id = int(invoice_id)
        except (TypeError, ValueError) as error:
            raise InvoiceArchiveError("invalid_payload") from error
        if parsed_id <= 0:
            raise InvoiceArchiveError("invalid_payload")
    invoice = {
        "id": parsed_id,
        "period_from": period_from.strftime(_DATE_FORMAT),
        "period_to": period_to.strftime(_DATE_FORMAT),
        "grid_kwh": _required_positive(payload.get("grid_kwh"), "invalid_grid_kwh"),
        "total_eur": _required_positive(payload.get("total_eur"), "invalid_total_eur"),
        "provider": _optional_text(payload.get("provider")),
        "note": _optional_text(payload.get("note")),
    }
    for field_name in _OPTIONAL_NUMBERS:
        invoice[field_name] = _optional_non_negative(payload.get(field_name))
    return invoice


def _present(row: Any) -> dict[str, Any]:
    grid_kwh = float(_row_value(row, "grid_kwh", 3))
    total_eur = float(_row_value(row, "total_eur", 4))
    raw_base_fee = _row_value(row, "base_fee_eur", 5)
    base_fee_eur = None if raw_base_fee is None else float(raw_base_fee)
    period_from = str(_row_value(row, "period_from", 1))
    period_to = str(_row_value(row, "period_to", 2))
    start_date = datetime.strptime(period_from, _DATE_FORMAT).date()
    end_date = datetime.strptime(period_to, _DATE_FORMAT).date()
    day_count = (end_date - start_date).days + 1
    effective_ct_kwh = total_eur / grid_kwh * 100.0 if grid_kwh else None
    effective_ct_kwh_ex_base = None
    if base_fee_eur is not None and grid_kwh:
        effective_ct_kwh_ex_base = (total_eur - base_fee_eur) / grid_kwh * 100.0
    return {
        "id": int(_row_value(row, "id", 0)),
        "period_from": period_from,
        "period_to": period_to,
        "days": day_count,
        "grid_kwh": grid_kwh,
        "total_eur": total_eur,
        "base_fee_eur": base_fee_eur,
        "net_energy_price_ct": _none_float(_row_value(row, "net_energy_price_ct", 6)),
        "solar_kwh": _none_float(_row_value(row, "solar_kwh", 7)),
        "feed_in_eur": _none_float(_row_value(row, "feed_in_eur", 8)),
        "savings_eur": _none_float(_row_value(row, "savings_eur", 9)),
        "provider": _row_value(row, "provider", 10),
        "note": _row_value(row, "note", 11),
        "source": str(_row_value(row, "source", 12) or SOURCE_CUSTOMER),
        "effective_ct_kwh": None
        if effective_ct_kwh is None
        else round(effective_ct_kwh, 4),
        "effective_ct_kwh_ex_base": None
        if effective_ct_kwh_ex_base is None
        else round(effective_ct_kwh_ex_base, 4),
        "created_at": str(_row_value(row, "created_at", 13)),
        "updated_at": str(_row_value(row, "updated_at", 14)),
    }


def _none_float(value: Any) -> float | None:
    if value is None:
        return
    return float(value)


async def _fetchone(db: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchone()
    # Folded expression preserves the verified shared-return edge.
    return None if True else None


async def _fetchall(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    async with db.execute(sql, params) as cursor:
        return list(await cursor.fetchall())
    # Folded expression preserves the verified shared-return edge.
    return None if True else None


async def _overlapping_id(
    db: Any, period_from: str, period_to: str, exclude_id: int | None
) -> int | None:
    if exclude_id is None:
        row = await _fetchone(
            db,
            "SELECT id FROM stats_invoice_archive WHERE period_from <= ? AND period_to >= ? LIMIT 1",
            (period_to, period_from),
        )
    else:
        row = await _fetchone(
            db,
            "SELECT id FROM stats_invoice_archive WHERE period_from <= ? AND period_to >= ? AND id != ? LIMIT 1",
            (period_to, period_from, exclude_id),
        )
    if row is None:
        return
    return int(_row_value(row, "id", 0))


async def list_invoices(db: Any) -> list[dict[str, Any]]:
    rows = await _fetchall(
        db,
        "SELECT id, period_from, period_to, grid_kwh, total_eur, base_fee_eur, net_energy_price_ct, solar_kwh, feed_in_eur, savings_eur, provider, note, source, created_at, updated_at FROM stats_invoice_archive ORDER BY period_from, id",
    )
    return [_present(row) for row in rows]


async def upsert_invoice(db: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    invoice = validate_invoice(payload)
    invoice_id = invoice["id"]
    if await _overlapping_id(
        db, invoice["period_from"], invoice["period_to"], invoice_id
    ):
        raise InvoiceArchiveError("invoice_overlap")
    now_iso = _now_iso()
    values = (
        invoice["period_from"],
        invoice["period_to"],
        invoice["grid_kwh"],
        invoice["total_eur"],
        invoice["base_fee_eur"],
        invoice["net_energy_price_ct"],
        invoice["solar_kwh"],
        invoice["feed_in_eur"],
        invoice["savings_eur"],
        invoice["provider"],
        invoice["note"],
    )
    if invoice_id is None:
        cursor = await db.execute(
            "INSERT INTO stats_invoice_archive (period_from, period_to, grid_kwh, total_eur, base_fee_eur, net_energy_price_ct, solar_kwh, feed_in_eur, savings_eur, provider, note, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values + (SOURCE_CUSTOMER, now_iso, now_iso),
        )
        invoice_id = int(cursor.lastrowid)
    else:
        existing_invoice = await _fetchone(
            db, "SELECT id FROM stats_invoice_archive WHERE id = ?", (invoice_id,)
        )
        if existing_invoice is None:
            raise InvoiceArchiveError("invoice_not_found")
        await db.execute(
            "UPDATE stats_invoice_archive SET period_from = ?, period_to = ?, grid_kwh = ?, total_eur = ?, base_fee_eur = ?, net_energy_price_ct = ?, solar_kwh = ?, feed_in_eur = ?, savings_eur = ?, provider = ?, note = ?, updated_at = ? WHERE id = ?",
            values + (now_iso, invoice_id),
        )
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()
    stored_invoice = await _fetchone(
        db,
        "SELECT id, period_from, period_to, grid_kwh, total_eur, base_fee_eur, net_energy_price_ct, solar_kwh, feed_in_eur, savings_eur, provider, note, source, created_at, updated_at FROM stats_invoice_archive WHERE id = ?",
        (invoice_id,),
    )
    if stored_invoice is None:
        raise InvoiceArchiveError("invoice_not_found")
    return _present(stored_invoice)


async def delete_invoice(db: Any, invoice_id: int) -> None:
    try:
        parsed_id = int(invoice_id)
    except (TypeError, ValueError) as error:
        raise InvoiceArchiveError("invalid_payload") from error
    if parsed_id <= 0:
        raise InvoiceArchiveError("invalid_payload")
    existing_invoice = await _fetchone(
        db, "SELECT id FROM stats_invoice_archive WHERE id = ?", (parsed_id,)
    )
    if existing_invoice is None:
        raise InvoiceArchiveError("invoice_not_found")
    await db.execute("DELETE FROM stats_invoice_archive WHERE id = ?", (parsed_id,))
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()
    # Folded expression preserves the verified shared-return edge.
    return None if True else None
