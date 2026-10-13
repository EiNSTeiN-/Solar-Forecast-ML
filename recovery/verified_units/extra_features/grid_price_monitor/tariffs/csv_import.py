# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""CSV price import: parse profiles P1/P2 and apply past corrections."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from ..const import PRICE_SOURCE_CSV

PROFILE_AUTO = "auto"
PROFILE_PRICE_LIST = "price_list"
PROFILE_COMMUNITY_SHARE = "community_share"
UNIT_AUTO = "auto"
UNIT_CT_KWH = "ct_kwh"
UNIT_EUR_KWH = "eur_kwh"
UNIT_EUR_MWH = "eur_mwh"
TZ_LOCAL = "local"
TZ_UTC = "utc"
CSV_MAX_BYTES = 5242880
CSV_MAX_ROWS = 100000
CSV_MAX_AGE_DAYS = 730
CSV_FUTURE_LIMIT_DAYS = 2
_TIMESTAMP_ALIASES = frozenset(
    {
        "von",
        "date",
        "time",
        "zeit",
        "datum",
        "start",
        "datetime",
        "timestamp",
        "zeitpunkt",
        "zeitstempel",
        "datum_uhrzeit",
    }
)
_PRICE_ALIASES = frozenset(
    {"wert", "preis", "price", "value", "total_price", "arbeitspreis"}
)
_TOTAL_KWH_ALIASES = frozenset(
    {"kwh", "kwh_total", "total_kwh", "verbrauch", "gesamt_kwh", "verbrauch_kwh"}
)
_COMMUNITY_KWH_ALIASES = frozenset(
    {"eeg", "anteil", "eeg_kwh", "community", "anteil_kwh", "community_kwh"}
)
_DT_DOT = re.compile(
    "^(\\d{1,2})\\.(\\d{1,2})\\.(\\d{4})(?:[ T](\\d{1,2}):(\\d{2})(?::(\\d{2}))?)?$"
)
_DT_DASH = re.compile(
    "^(\\d{4})-(\\d{2})-(\\d{2})(?:[ T](\\d{1,2}):(\\d{2})(?::(\\d{2}))?)?$"
)


class CsvImportError(Exception):

    def __init__(self, key: str, **placeholders: Any) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(frozen=True, slots=True)
class HourPrice:
    timestamp: str
    total_price: float
    hour: int


@dataclass(frozen=True, slots=True)
class ParseResult:
    profile: str
    detected_profile: str
    unit: str
    detected_unit: str
    headers: list[str]
    hours: list[HourPrice]
    skipped_rows: int
    ignored_old: int
    row_count: int
    range_from: str | None
    range_to: str | None
    source_hash: str
    groups: dict[str, list[dict[str, float]]] = field(default_factory=dict)
    column_map: dict[str, str] = field(default_factory=dict)


def detect_profile(headers: list[str]) -> str:
    columns = suggest_column_map(headers)
    if columns.get("total_kwh") and columns.get("community_kwh"):
        return PROFILE_COMMUNITY_SHARE
    return PROFILE_PRICE_LIST


def suggest_column_map(headers: list[str]) -> dict[str, str]:
    columns = {}
    used_headers = set()
    for aliases, field_name in (
        (_TIMESTAMP_ALIASES, "timestamp"),
        (_PRICE_ALIASES, "price"),
        (_TOTAL_KWH_ALIASES, "total_kwh"),
        (_COMMUNITY_KWH_ALIASES, "community_kwh"),
    ):
        header = _best_header(headers, aliases, used_headers)
        if header:
            columns[field_name] = header
            used_headers.add(header)
    return columns


def parse_csv_bytes(
    data: bytes,
    *,
    profile: str = PROFILE_AUTO,
    price_unit: str = UNIT_AUTO,
    csv_timezone: str = TZ_LOCAL,
    column_map: Mapping[str, str] | None = None,
    local_tz: tzinfo | None = None,
    now: datetime | None = None,
    eeg_price: float | None = None,
    base_prices: Mapping[str, float] | None = None,
) -> ParseResult:
    if len(data) > CSV_MAX_BYTES:
        raise CsvImportError("csv_too_large")
    source_hash = hashlib.sha256(data).hexdigest()
    text = _decode(data)
    if not text.strip():
        raise CsvImportError("csv_missing_header")
    dialect = _sniff(text)
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        raw_headers = next(reader)
    except StopIteration as error:
        raise CsvImportError("csv_missing_header") from error
    headers = [item.strip() for item in raw_headers if item is not None]
    if not headers or all((not item for item in headers)):
        raise CsvImportError("csv_missing_header")
    detected_profile = detect_profile(headers)
    selected_profile = detected_profile if profile == PROFILE_AUTO else profile
    if selected_profile not in {PROFILE_PRICE_LIST, PROFILE_COMMUNITY_SHARE}:
        raise CsvImportError("csv_invalid_mapping")
    suggested_columns = suggest_column_map(headers)
    columns = dict(suggested_columns)
    if column_map:
        columns.update(
            {field_name: value for field_name, value in column_map.items() if value}
        )
    timestamp_column = columns.get("timestamp")
    if not timestamp_column or timestamp_column not in headers:
        raise CsvImportError("csv_invalid_mapping")
    timezone_local = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    current_time = now or datetime.now(tz=timezone_local)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone_local)
    latest_allowed = current_time.astimezone(timezone_local) + timedelta(
        days=CSV_FUTURE_LIMIT_DAYS
    )
    earliest_allowed = current_time.astimezone(timezone_local) - timedelta(
        days=CSV_MAX_AGE_DAYS
    )
    hourly_groups = {}
    price_samples = []
    skipped_rows = 0
    ignored_old = 0
    row_count = 0
    for raw_row in reader:
        row_count += 1
        if row_count > CSV_MAX_ROWS:
            raise CsvImportError("csv_too_many_rows")
        if not raw_row or all((not str(cell).strip() for cell in raw_row)):
            continue
        row = {
            headers[column_index]: (
                raw_row[column_index] if column_index < len(raw_row) else ""
            )
            for column_index in range(len(headers))
        }
        try:
            parsed_timestamp = _parse_timestamp(
                row.get(timestamp_column, ""),
                csv_timezone=csv_timezone,
                local_tz=timezone_local,
            )
        except (TypeError, ValueError):
            skipped_rows += 1
            continue
        aware_timestamp = (
            parsed_timestamp
            if parsed_timestamp.tzinfo
            else parsed_timestamp.replace(tzinfo=timezone_local)
        )
        if aware_timestamp > latest_allowed:
            raise CsvImportError("csv_future_rejected")
        if aware_timestamp < earliest_allowed:
            ignored_old += 1
            continue
        hour_start = aware_timestamp.astimezone(timezone_local).replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        )
        hour_key = hour_start.strftime("%Y-%m-%dT%H:00:00")
        try:
            if selected_profile == PROFILE_PRICE_LIST:
                price_column = columns.get("price")
                if not price_column:
                    raise CsvImportError("csv_invalid_mapping")
                value = _parse_number(row.get(price_column, ""))
                if value is None:
                    skipped_rows += 1
                    continue
                price_samples.append(value)
                hourly_groups.setdefault(hour_key, []).append({"price": value})
                continue
            total_column = columns.get("total_kwh")
            community_column = columns.get("community_kwh")
            if not total_column or not community_column:
                raise CsvImportError("csv_invalid_mapping")
            total_kwh = _parse_number(row.get(total_column, ""))
            community_kwh = _parse_number(row.get(community_column, ""))
            if total_kwh is None or community_kwh is None:
                skipped_rows += 1
                continue
            hourly_groups.setdefault(hour_key, []).append(
                {"total_kwh": total_kwh, "community_kwh": community_kwh}
            )
        except CsvImportError:
            raise
        except (TypeError, ValueError):
            skipped_rows += 1
            continue
        continue
    detected_unit = (
        _detect_unit(price_samples)
        if selected_profile == PROFILE_PRICE_LIST
        else UNIT_CT_KWH
    )
    selected_unit = detected_unit if price_unit == UNIT_AUTO else price_unit
    hours = []
    for hour_key, group in sorted(hourly_groups.items()):
        hour = int(hour_key[11:13])
        if selected_profile == PROFILE_COMMUNITY_SHARE:
            total_kwh = sum((item["total_kwh"] for item in group))
            if total_kwh == 0:
                continue
            community_kwh = sum((item["community_kwh"] for item in group))
            if base_prices is None:
                continue
            base_price = base_prices.get(hour_key)
            if base_price is None:
                skipped_rows += len(group)
                continue
            community_price = float(eeg_price or 0.0)
            grid_kwh = max(0.0, total_kwh - community_kwh)
            hour_price = round(
                (community_kwh * community_price + grid_kwh * float(base_price))
                / total_kwh,
                4,
            )
        else:
            converted_prices = [
                _to_ct_kwh(item["price"], selected_unit) for item in group
            ]
            hour_price = round(sum(converted_prices) / len(converted_prices), 4)
        hours.append(HourPrice(timestamp=hour_key, total_price=hour_price, hour=hour))
        continue
    if not hours:
        if (
            not selected_profile == PROFILE_COMMUNITY_SHARE
            or not hourly_groups
            or base_prices is not None
        ):
            raise CsvImportError("csv_no_hours")
    return ParseResult(
        profile=selected_profile,
        detected_profile=detected_profile,
        unit=selected_unit,
        detected_unit=detected_unit,
        headers=headers,
        hours=hours,
        skipped_rows=skipped_rows,
        ignored_old=ignored_old,
        row_count=row_count,
        range_from=hours[0].timestamp if hours else None,
        range_to=hours[-1].timestamp if hours else None,
        source_hash=source_hash,
        groups=hourly_groups,
        column_map=columns,
    )


def restore_from_raw(
    rows: list[Mapping[str, Any]], summary: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Undo helper: restore total_price from total_price_raw."""
    original_sources = {}
    if summary:
        saved_sources = summary.get("original_sources") or {}
        if isinstance(saved_sources, Mapping):
            original_sources = {
                str(saved_timestamp): str(source)
                for saved_timestamp, source in saved_sources.items()
            }
    restored = []
    for row in rows:
        raw_price = row.get("total_price_raw")
        if raw_price is not None:
            timestamp = str(row["timestamp"])
            restored.append(
                {
                    "timestamp": timestamp,
                    "total_price": float(raw_price),
                    "price_source": original_sources.get(timestamp)
                    or row.get("price_source_original")
                    or row.get("price_source"),
                }
            )
    return restored


async def preview_against_history(db: Any, hours: list[HourPrice]) -> dict[str, Any]:
    timestamps = [hour_price.timestamp for hour_price in hours]
    existing_timestamps = set()
    if timestamps:
        timestamp_placeholders = ",".join(("?" for item in timestamps))
        history_rows = await db.fetchall(
            f"SELECT timestamp FROM GPM_price_history WHERE timestamp IN ({timestamp_placeholders})",
            tuple(timestamps),
        )
        existing_timestamps = {row["timestamp"] for row in history_rows or []}
    months = sorted({hour_price.timestamp[:7] for hour_price in hours})
    stale_months = []
    if months:
        month_placeholders = ",".join(("?" for item in months))
        correction_rows = await db.fetchall(
            f"SELECT DISTINCT month FROM GPM_price_corrections\n                WHERE kind = 'monthly' AND status = 'applied' AND month IN ({month_placeholders})",
            tuple(months),
        )
        stale_months = [row["month"] for row in correction_rows or [] if row["month"]]
    return {
        "hours_matched": sum(
            (1 for hour_price in hours if hour_price.timestamp in existing_timestamps)
        ),
        "hours_new": sum(
            (
                1
                for hour_price in hours
                if hour_price.timestamp not in existing_timestamps
            )
        ),
        "stale_months": stale_months,
        "months": months,
    }


async def apply_csv_hours(
    db: Any,
    hours: list[HourPrice],
    *,
    stats_store: Any | None,
    params: Mapping[str, Any],
    source_hash: str,
    now: datetime,
) -> dict[str, Any]:
    await db.require_price_write_schema()
    if not hours:
        raise CsvImportError("csv_no_hours")
    preview = await preview_against_history(db, hours)
    applied_at = now.astimezone().replace(microsecond=0).isoformat()
    range_from = hours[0].timestamp
    range_to = hours[-1].timestamp
    await db.execute(
        "INSERT INTO GPM_price_corrections\n           (kind, status, created_at, applied_at, range_from, range_to, month,\n            params_json, summary_json, rows_affected, source_hash)\n           VALUES ('csv', 'applied', ?, ?, ?, ?, NULL, ?, ?, 0, ?)",
        (
            applied_at,
            applied_at,
            range_from,
            range_to,
            json.dumps(dict(params), sort_keys=True),
            json.dumps(
                {
                    "hours": len(hours),
                    "hours_matched": preview["hours_matched"],
                    "hours_new": preview["hours_new"],
                    "stale_months": preview["stale_months"],
                },
                sort_keys=True,
            ),
            source_hash,
        ),
        auto_commit=False,
    )
    inserted_row = await db.fetchone("SELECT last_insert_rowid() AS id")
    correction_id = int(inserted_row["id"])
    timestamps = [hour_price.timestamp for hour_price in hours]
    timestamp_placeholders = ",".join(("?" for item in timestamps))
    history_rows = await db.fetchall(
        f"SELECT timestamp, total_price, total_price_raw, correction_id, price_source\n            FROM GPM_price_history WHERE timestamp IN ({timestamp_placeholders})",
        tuple(timestamps),
    )
    existing_rows = {row["timestamp"]: row for row in history_rows or []}
    original_sources = {}
    previous_correction_ids = {
        int(row["correction_id"])
        for row in existing_rows.values()
        if row["correction_id"] is not None
    }
    previous_correction_kinds = {}
    if previous_correction_ids:
        correction_placeholders = ",".join(("?" for item in previous_correction_ids))
        correction_rows = await db.fetchall(
            f"SELECT id, kind FROM GPM_price_corrections WHERE id IN ({correction_placeholders})",
            tuple(previous_correction_ids),
        )
        previous_correction_kinds = {
            int(row["id"]): row["kind"] for row in correction_rows or []
        }
    for hour_price in hours:
        existing = existing_rows.get(hour_price.timestamp)
        if existing is None:
            await db.execute(
                "INSERT INTO GPM_price_history\n                   (timestamp, price_net, total_price, hour, price_source,\n                    total_price_raw, correction_id)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hour_price.timestamp,
                    hour_price.total_price,
                    hour_price.total_price,
                    hour_price.hour,
                    PRICE_SOURCE_CSV,
                    hour_price.total_price,
                    correction_id,
                ),
                auto_commit=False,
            )
            continue
        previous_kind = (
            previous_correction_kinds.get(int(existing["correction_id"]))
            if existing["correction_id"] is not None
            else None
        )
        raw_price = existing["total_price_raw"]
        if existing["correction_id"] is None:
            raw_price = existing["total_price"] if raw_price is None else raw_price
        elif previous_kind == "csv":
            raw_price = existing["total_price"]
        original_source = existing["price_source"]
        if original_source:
            original_sources[hour_price.timestamp] = str(original_source)
        await db.execute(
            "UPDATE GPM_price_history\n               SET total_price_raw = ?,\n                   total_price = ?,\n                   price_net = ?,\n                   hour = ?,\n                   price_source = ?,\n                   correction_id = ?\n               WHERE timestamp = ?",
            (
                raw_price,
                hour_price.total_price,
                hour_price.total_price,
                hour_price.hour,
                PRICE_SOURCE_CSV,
                correction_id,
                hour_price.timestamp,
            ),
            auto_commit=False,
        )
        continue
    stale_months = preview["stale_months"]
    if stale_months:
        month_placeholders = ",".join(("?" for item in stale_months))
        await db.execute(
            f"UPDATE GPM_price_corrections\n                SET status = 'stale'\n                WHERE kind = 'monthly' AND status = 'applied'\n                  AND month IN ({month_placeholders})",
            tuple(stale_months),
            auto_commit=False,
        )
    days = sorted({hour_price.timestamp[:10] for hour_price in hours})
    for day in days:
        day_start = f"{day}T00:00:00"
        day_end = (datetime.fromisoformat(day) + timedelta(days=1)).strftime(
            "%Y-%m-%dT00:00:00"
        )
        daily_average = await db.fetchone(
            "SELECT AVG(price_net) AS average_net,\n                      AVG(total_price) AS average_total,\n                      MIN(total_price) AS min_price,\n                      MAX(total_price) AS max_price\n               FROM GPM_price_history\n               WHERE timestamp >= ? AND timestamp < ?",
            (day_start, day_end),
        )
        if daily_average:
            if daily_average["average_total"] is not None:
                if stats_store is not None:
                    await stats_store.async_update_daily_average(
                        date=day,
                        average_net=float(daily_average["average_net"]),
                        average_total=float(daily_average["average_total"]),
                        min_price=(
                            None
                            if daily_average["min_price"] is None
                            else float(daily_average["min_price"])
                        ),
                        max_price=(
                            None
                            if daily_average["max_price"] is None
                            else float(daily_average["max_price"])
                        ),
                    )
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
        auto_commit=False,
    )
    await db.execute(
        "UPDATE GPM_price_corrections\n           SET rows_affected = ?, summary_json = ?\n           WHERE id = ?",
        (
            len(hours),
            json.dumps(
                {
                    "hours": len(hours),
                    "hours_matched": preview["hours_matched"],
                    "hours_new": preview["hours_new"],
                    "stale_months": stale_months,
                    "revision": revision,
                    "original_sources": original_sources,
                },
                sort_keys=True,
            ),
            correction_id,
        ),
        auto_commit=False,
    )
    await db.commit()
    return {
        "correction_id": correction_id,
        "rows_affected": len(hours),
        "revision": revision,
        "from": range_from[:10],
        "to": range_to[:10],
        "reason": "csv",
        "stale_months": stale_months,
        "applied_at": applied_at,
        "hours_matched": preview["hours_matched"],
        "hours_new": preview["hours_new"],
        "source_hash": source_hash,
    }


def _decode(data: bytes) -> str:
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def _sniff(text: str) -> type[csv.Dialect]:
    sample = text[:4096]
    try:
        detected = csv.Sniffer().sniff(sample, delimiters=";\t,")
        delimiter = detected.delimiter
    except csv.Error:
        semicolons = sample.count(";")
        commas = sample.count(",")
        tabs = sample.count("\t")
        if tabs > semicolons and tabs > commas:
            delimiter = "\t"
        elif semicolons >= commas:
            delimiter = ";"
        else:
            delimiter = ","

    class _Dialect(csv.excel):
        pass

    _Dialect.delimiter = delimiter
    return _Dialect


def _normalize_header(value: str) -> str:
    return re.sub("[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _best_header(
    headers: list[str], aliases: frozenset[str], used: set[str]
) -> str | None:
    candidates = [
        (_normalize_header(candidate_header), candidate_header)
        for candidate_header in headers
        if candidate_header not in used
    ]
    for normalized, header in candidates:
        if not normalized in aliases:
            continue
        return header
    for normalized, header in candidates:
        if not any((word in aliases for word in normalized.split("_") if word)):
            continue
        return header
    for normalized, header in candidates:
        if not any((alias in normalized for alias in aliases if len(alias) >= 4)):
            continue
        return header


def _parse_number(value: Any) -> float | None:
    if value is None:
        return
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if not text:
        return
    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return


def _parse_timestamp(value: str, *, csv_timezone: str, local_tz: tzinfo) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    iso_text = text.replace("Z", "+00:00")
    parsed = None
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None
    if parsed is None:
        matched = _DT_DOT.match(text) or _DT_DASH.match(text)
        if matched is None:
            raise ValueError(text)
        if matched.re is _DT_DOT:
            day, month, year, hour, minute, second = matched.groups()
        else:
            year, month, day, hour, minute, second = matched.groups()
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    if parsed.tzinfo is None:
        if csv_timezone == TZ_UTC:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _detect_unit(samples: list[float]) -> str:
    if not samples:
        return UNIT_CT_KWH
    sorted_samples = sorted(samples)
    median = sorted_samples[len(sorted_samples) // 2]
    if median > 100:
        return UNIT_EUR_MWH
    if median < 1:
        return UNIT_EUR_KWH
    return UNIT_CT_KWH


def _to_ct_kwh(value: float, unit: str) -> float:
    if unit == UNIT_EUR_KWH:
        return value * 100.0
    if unit == UNIT_EUR_MWH:
        return value / 10.0
    return value


def default_local_tz(name: str | None = None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
