# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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
    _var_var_5 = hashlib.sha256(data).hexdigest()
    _var_var_6 = _decode(data)
    if not _var_var_6.strip():
        raise CsvImportError("csv_missing_header")
    _var_var_7 = _sniff(_var_var_6)
    _var_var_8 = csv.reader(io.StringIO(_var_var_6), _var_var_7)
    try:
        _var_var_9 = next(_var_var_8)
    except StopIteration as _var_var_10:
        raise CsvImportError("csv_missing_header") from _var_var_10
    _var_var_12 = [
        _var_var_11.strip() for _var_var_11 in _var_var_9 if _var_var_11 is not None
    ]
    if not _var_var_12 or all((not _var_var_11 for _var_var_11 in _var_var_12)):
        raise CsvImportError("csv_missing_header")
    _var_var_13 = detect_profile(_var_var_12)
    _var_var_14 = _var_var_13 if profile == PROFILE_AUTO else profile
    if _var_var_14 not in {PROFILE_PRICE_LIST, PROFILE_COMMUNITY_SHARE}:
        raise CsvImportError("csv_invalid_mapping")
    _var_var_15 = suggest_column_map(_var_var_12)
    _var_var_0 = dict(_var_var_15)
    if column_map:
        _var_var_0.update(
            {
                _var_var_3: _var_var_16
                for _var_var_3, _var_var_16 in column_map.items()
                if _var_var_16
            }
        )
    _var_var_17 = _var_var_0.get("timestamp")
    if not _var_var_17 or _var_var_17 not in _var_var_12:
        raise CsvImportError("csv_invalid_mapping")
    _var_var_18 = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    _var_var_19 = now or datetime.now(tz=_var_var_18)
    if _var_var_19.tzinfo is None:
        _var_var_19 = _var_var_19.replace(tzinfo=_var_var_18)
    _var_var_20 = _var_var_19.astimezone(_var_var_18) + timedelta(
        days=CSV_FUTURE_LIMIT_DAYS
    )
    _var_var_21 = _var_var_19.astimezone(_var_var_18) - timedelta(days=CSV_MAX_AGE_DAYS)
    _var_var_22 = {}
    _var_var_23 = []
    _var_var_24 = 0
    _var_var_25 = 0
    _var_var_26 = 0
    for _var_var_27 in _var_var_8:
        _var_var_26 += 1
        if _var_var_26 > CSV_MAX_ROWS:
            raise CsvImportError("csv_too_many_rows")
        if not _var_var_27 or all(
            (not str(_var_var_49).strip() for _var_var_49 in _var_var_27)
        ):
            continue
        _var_var_29 = {
            _var_var_12[_var_var_28]: (
                _var_var_27[_var_var_28] if _var_var_28 < len(_var_var_27) else ""
            )
            for _var_var_28 in range(len(_var_var_12))
        }
        try:
            _var_var_30 = _parse_timestamp(
                _var_var_29.get(_var_var_17, ""),
                csv_timezone=csv_timezone,
                local_tz=_var_var_18,
            )
        except (TypeError, ValueError):
            _var_var_24 += 1
            continue
        _var_var_31 = (
            _var_var_30
            if _var_var_30.tzinfo
            else _var_var_30.replace(tzinfo=_var_var_18)
        )
        if _var_var_31 > _var_var_20:
            raise CsvImportError("csv_future_rejected")
        if _var_var_31 < _var_var_21:
            _var_var_25 += 1
            continue
        _var_var_32 = _var_var_31.astimezone(_var_var_18).replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        )
        _var_var_33 = _var_var_32.strftime("%Y-%m-%dT%H:00:00")
        try:
            if _var_var_14 == PROFILE_PRICE_LIST:
                _var_var_34 = _var_var_0.get("price")
                if not _var_var_34:
                    raise CsvImportError("csv_invalid_mapping")
                _var_var_16 = _parse_number(_var_var_29.get(_var_var_34, ""))
                if _var_var_16 is None:
                    _var_var_24 += 1
                    continue
                _var_var_23.append(_var_var_16)
                _var_var_22.setdefault(_var_var_33, []).append({"price": _var_var_16})
                continue
            _var_var_35 = _var_var_0.get("total_kwh")
            _var_var_36 = _var_var_0.get("community_kwh")
            if not _var_var_35 or not _var_var_36:
                raise CsvImportError("csv_invalid_mapping")
            _var_var_37 = _parse_number(_var_var_29.get(_var_var_35, ""))
            _var_var_38 = _parse_number(_var_var_29.get(_var_var_36, ""))
            if _var_var_37 is None or _var_var_38 is None:
                _var_var_24 += 1
                continue
            _var_var_22.setdefault(_var_var_33, []).append(
                {"total_kwh": _var_var_37, "community_kwh": _var_var_38}
            )
        except CsvImportError:
            raise
        except (TypeError, ValueError):
            _var_var_24 += 1
            continue
        continue
    _var_var_39 = (
        _detect_unit(_var_var_23) if _var_var_14 == PROFILE_PRICE_LIST else UNIT_CT_KWH
    )
    _var_var_40 = _var_var_39 if price_unit == UNIT_AUTO else price_unit
    _var_var_41 = []
    for _var_var_33, _var_var_42 in sorted(_var_var_22.items()):
        _var_var_43 = int(_var_var_33[11:13])
        if _var_var_14 == PROFILE_COMMUNITY_SHARE:
            _var_var_37 = sum((_var_var_11["total_kwh"] for _var_var_11 in _var_var_42))
            if _var_var_37 == 0:
                continue
            _var_var_38 = sum(
                (_var_var_11["community_kwh"] for _var_var_11 in _var_var_42)
            )
            if base_prices is None:
                continue
            _var_var_44 = base_prices.get(_var_var_33)
            if _var_var_44 is None:
                _var_var_24 += len(_var_var_42)
                continue
            _var_var_45 = float(eeg_price or 0.0)
            _var_var_46 = max(0.0, _var_var_37 - _var_var_38)
            _var_var_47 = round(
                (_var_var_38 * _var_var_45 + _var_var_46 * float(_var_var_44))
                / _var_var_37,
                4,
            )
        else:
            _var_var_48 = [
                _to_ct_kwh(_var_var_11["price"], _var_var_40)
                for _var_var_11 in _var_var_42
            ]
            _var_var_47 = round(sum(_var_var_48) / len(_var_var_48), 4)
        _var_var_41.append(
            HourPrice(timestamp=_var_var_33, total_price=_var_var_47, hour=_var_var_43)
        )
        continue
    if not _var_var_41:
        if (
            not _var_var_14 == PROFILE_COMMUNITY_SHARE
            or not _var_var_22
            or base_prices is not None
        ):
            raise CsvImportError("csv_no_hours")
    return ParseResult(
        profile=_var_var_14,
        detected_profile=_var_var_13,
        unit=_var_var_40,
        detected_unit=_var_var_39,
        headers=_var_var_12,
        hours=_var_var_41,
        skipped_rows=_var_var_24,
        ignored_old=_var_var_25,
        row_count=_var_var_26,
        range_from=_var_var_41[0].timestamp if _var_var_41 else None,
        range_to=_var_var_41[-1].timestamp if _var_var_41 else None,
        source_hash=_var_var_5,
        groups=_var_var_22,
        column_map=_var_var_0,
    )


def _sniff(text: str) -> type[csv.Dialect]:
    _var_var_87 = text[:4096]
    try:
        _var_var_7 = csv.Sniffer().sniff(_var_var_87, delimiters=";\t,")
        _var_var_88 = _var_var_7.delimiter
    except csv.Error:
        _var_var_89 = _var_var_87.count(";")
        _var_var_90 = _var_var_87.count(",")
        _var_var_91 = _var_var_87.count("\t")
        if _var_var_91 > _var_var_89 and _var_var_91 > _var_var_90:
            _var_var_88 = "\t"
        elif _var_var_89 >= _var_var_90:
            _var_var_88 = ";"
        else:
            _var_var_88 = ","

    class _Dialect(csv.excel):
        pass

    _Dialect.delimiter = _var_var_88
    return _Dialect
