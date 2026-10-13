# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-only GPM status API for SFML Stats."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..mock.gpm import scenario_payload
from .views import local_only

PUBLIC_STATES = frozenset({"live", "gpm_demo", "incompatible", "not_installed"})
_TOP_KEYS = (
    "success",
    "state",
    "is_demo",
    "provider_version",
    "license",
    "tariff",
    "prices",
    "threshold_ct",
    "thresholds",
    "is_cheap",
    "is_force_price",
    "next_cheap",
    "cheap_hours",
    "tomorrow_available",
    "price_components",
    "composition_ok",
    "corrections",
    "months",
    "effective_fees",
    "tariff_schedule",
    "diagnostics",
    "revision",
    "last_correction",
    "capabilities",
    "links",
    "updated_at",
)
_LICENSE_KEYS = ("status", "source", "id_masked", "expires_at")
_TARIFF_KEYS = (
    "mode",
    "label",
    "country",
    "has_spot_component",
    "feed_in_tariff_ct",
    "base_fee_eur_month",
)
_PRICE_KEYS = (
    "current_total",
    "next_total",
    "average_today",
    "cheapest_hour",
    "most_expensive_hour",
    "today",
    "tomorrow",
)
_POINT_KEYS = ("hour", "total_price", "spot_price", "is_cheap")
_CAPABILITY_KEYS = ("tariff_models", "csv_import", "corrections")
_CORRECTION_KEYS = ("id", "kind", "month", "applied_at", "range_from", "range_to")
_CORRECTION_ROW_KEYS = (
    "id",
    "kind",
    "status",
    "month",
    "range_from",
    "range_to",
    "applied_at",
    "rows_affected",
    "revision",
)
_NEXT_CHEAP_KEYS = ("hour", "timestamp")
_CHEAP_HOURS_KEYS = ("today", "tomorrow")
_COMPONENT_KEYS = ("vat_rate", "grid_fee", "taxes_fees", "provider_markup")
_FEE_KEYS = ("feed_in_tariff_ct", "base_fee_eur_month", "source")
_MONTH_KEYS = ("month", "average", "min", "max", "cheap_hours", "current")
_SCHEDULE_KEYS = ("days", "holiday_hint")
_DIAGNOSTIC_KEYS = (
    "last_fetch",
    "cache_age_seconds",
    "price_revision",
    "last_correction",
    "provider_version",
    "composition_mismatch",
)
_LINK_KEYS = ("configure", "smart_charging")
_THRESHOLD_KEYS = (
    "mode",
    "max_price",
    "force_charge_price",
    "below_average_pct",
    "cheapest_hours",
)
_SMART_CHARGING_LINK = "#smart_charging"
_PRIVATE_KEYS = frozenset(
    {
        "data_dir",
        "entry_id",
        "entity_id",
        "file_path",
        "order_ref",
        "entity_ids",
        "license_id",
        "coordinator",
        "license_key",
        "customer_ref",
        "config_entry_id",
    }
)
_CONFIGURE_LINK = "/config/integrations/integration/grid_price_monitor"


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {field_name: source.get(field_name) for field_name in keys}


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    try:
        number_value = float(value)
    except (TypeError, ValueError):
        return
    return int(number_value) if number_value.is_integer() else number_value


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return
    normalized_text = value.strip()
    return normalized_text or None


def _price_points(
    rows: Any, *, threshold_ct: float | None = None
) -> list[dict[str, Any]]:
    price_points = []
    if not isinstance(rows, list):
        return price_points
    for price_row in rows:
        if isinstance(price_row, dict):
            hour = _number(price_row.get("hour"))
            total_price = _number(price_row.get("total_price"))
            spot_price = _number(
                price_row.get("spot_price")
                if "spot_price" in price_row
                else price_row.get("spot_price_net")
            )
            if hour is not None or total_price is not None:
                is_cheap = price_row.get("is_cheap")
                if (
                    is_cheap is None
                    and total_price is not None
                    and (threshold_ct is not None)
                ):
                    is_cheap = total_price < threshold_ct
                price_points.append(
                    {
                        "hour": None if hour is None else int(hour),
                        "total_price": total_price,
                        "spot_price": spot_price,
                        "is_cheap": bool(is_cheap),
                    }
                )
    return price_points


def _correction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return
    correction = {
        "id": _number(value.get("id")),
        "kind": _text(value.get("kind")),
        "month": _text(value.get("month")),
        "applied_at": _text(value.get("applied_at")),
        "range_from": _text(value.get("range_from")),
        "range_to": _text(value.get("range_to")),
    }
    if all((field_value is None for field_value in correction.values())):
        return
    return correction


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    integers = []
    for integer_input in value:
        number_value = _number(integer_input)
        if number_value is not None:
            integers.append(int(number_value))
    return integers


def _revision_from_summary(value: Any) -> int | None:
    if isinstance(value, dict):
        summary = value
    elif isinstance(value, str) and value.strip():
        try:
            decoded_summary = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        summary = decoded_summary if isinstance(decoded_summary, dict) else {}
    else:
        return
    revision_value = _number(summary.get("revision", summary.get("price_revision")))
    return None if revision_value is None else int(revision_value)


def _correction_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return
    correction = {
        "id": _number(value.get("id")),
        "kind": _text(value.get("kind")),
        "status": _text(value.get("status")),
        "month": _text(value.get("month")),
        "range_from": _text(value.get("range_from")),
        "range_to": _text(value.get("range_to")),
        "applied_at": _text(value.get("applied_at")),
        "rows_affected": _number(value.get("rows_affected")),
        "revision": (
            _revision_from_summary(value.get("summary_json"))
            if "summary_json" in value
            else _number(value.get("revision"))
        ),
    }
    if correction["id"] is None and correction["kind"] is None:
        return
    return correction


def _thresholds(value: Any) -> dict[str, Any]:
    source_data = value if isinstance(value, dict) else {}
    cheapest_hours = _number(source_data.get("cheapest_hours"))
    return (
        (
            (
                {
                    "mode": _text(
                        source_data.get("mode") or source_data.get("threshold_mode")
                    ),
                    "max_price": _number(source_data.get("max_price")),
                    "force_charge_price": _number(
                        source_data.get("force_charge_price")
                    ),
                    "below_average_pct": _number(source_data.get("below_average_pct")),
                    "cheapest_hours": (
                        int(cheapest_hours) if cheapest_hours is not None else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


def _next_cheap(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return
    next_cheap = {
        "hour": (
            None
            if _number(value.get("hour")) is None
            else int(_number(value.get("hour")))
        ),
        "timestamp": _text(value.get("timestamp")),
    }
    if next_cheap["hour"] is None and next_cheap["timestamp"] is None:
        return
    return next_cheap


def _price_components(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return
    components = {
        "vat_rate": _number(value.get("vat_rate")),
        "grid_fee": _number(value.get("grid_fee")),
        "taxes_fees": _number(value.get("taxes_fees")),
        "provider_markup": _number(value.get("provider_markup")),
    }
    if all((component_value is None for component_value in components.values())):
        return
    return components


def _effective_fees(value: Any) -> dict[str, Any]:
    fee_data = value if isinstance(value, dict) else {}
    fee_source = _text(fee_data.get("source")) or "gpm"
    if fee_source not in {"gpm", "stats_legacy"}:
        fee_source = "gpm"
    return {
        "feed_in_tariff_ct": _number(fee_data.get("feed_in_tariff_ct")),
        "base_fee_eur_month": _number(fee_data.get("base_fee_eur_month")),
        "source": fee_source,
    }


def _months(value: Any) -> list[dict[str, Any]]:
    normalized_months = []
    if not isinstance(value, list):
        return normalized_months
    for month_data in value:
        if isinstance(month_data, dict):
            normalized_months.append(
                {
                    "month": _text(month_data.get("month")),
                    "average": _number(month_data.get("average")),
                    "min": _number(month_data.get("min")),
                    "max": _number(month_data.get("max")),
                    "cheap_hours": _number(month_data.get("cheap_hours")),
                    "current": bool(month_data.get("current")),
                }
            )
    return normalized_months


def _tariff_schedule(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return
    raw_days = value.get("days")
    normalized_days = []
    if isinstance(raw_days, list) and len(raw_days) == 7:
        for day_prices in raw_days:
            if not isinstance(day_prices, list) or len(day_prices) != 24:
                return
            normalized_days.append([_number(price_value) for price_value in day_prices])
    if len(normalized_days) != 7:
        return
    return {"days": normalized_days, "holiday_hint": _text(value.get("holiday_hint"))}


def composition_matches(
    *, spot_net: float | None, total: float | None, components: dict[str, Any] | None
) -> bool:
    if spot_net is None or total is None or (not components):
        return False
    vat_rate = components.get("vat_rate")
    grid_fee = components.get("grid_fee")
    taxes_fees = components.get("taxes_fees")
    provider_markup = components.get("provider_markup")
    if None in (vat_rate, grid_fee, taxes_fees, provider_markup):
        return False
    expected_total_price = (
        spot_net * (1 + float(vat_rate) / 100.0)
        + float(grid_fee)
        + float(taxes_fees)
        + float(provider_markup)
    )
    return abs(expected_total_price - float(total)) <= 0.010000001


def public_gpm_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the browser-facing GPM DTO. Private keys never leave this helper."""
    source_data = {
        field_name: field_value
        for field_name, field_value in (payload or {}).items()
        if field_name not in _PRIVATE_KEYS
    }
    public_state = source_data.get("state")
    if public_state not in PUBLIC_STATES:
        public_state = "not_installed"
    license_data = (
        source_data.get("license")
        if isinstance(source_data.get("license"), dict)
        else {}
    )
    tariff_data = (
        source_data.get("tariff") if isinstance(source_data.get("tariff"), dict) else {}
    )
    price_data = (
        source_data.get("prices") if isinstance(source_data.get("prices"), dict) else {}
    )
    capabilities = (
        source_data.get("capabilities")
        if isinstance(source_data.get("capabilities"), dict)
        else {}
    )
    links = (
        source_data.get("links") if isinstance(source_data.get("links"), dict) else {}
    )
    threshold_ct = _number(source_data.get("threshold_ct"))
    price_components = _price_components(source_data.get("price_components"))
    last_correction = _correction(source_data.get("last_correction"))
    composition_ok = (
        bool(source_data.get("composition_ok"))
        if "composition_ok" in source_data
        else False
    )
    if "composition_ok" not in source_data:
        composition_ok = composition_matches(
            spot_net=_number(
                price_data.get("current_spot_net") or price_data.get("spot_net")
            ),
            total=_number(price_data.get("current_total")),
            components=price_components,
        )
    diagnostics = (
        source_data.get("diagnostics")
        if isinstance(source_data.get("diagnostics"), dict)
        else {}
    )
    cheap_hours = (
        source_data.get("cheap_hours")
        if isinstance(source_data.get("cheap_hours"), dict)
        else {}
    )
    raw_corrections = (
        source_data.get("corrections")
        if isinstance(source_data.get("corrections"), list)
        else []
    )
    corrections = [
        public_row
        for public_row in (
            _correction_row(correction_input) for correction_input in raw_corrections
        )
        if public_row
    ]
    public_status = {
        "success": True,
        "state": public_state,
        "is_demo": bool(source_data.get("is_demo")),
        "provider_version": _number(source_data.get("provider_version")) or 1,
        "license": {
            "status": _text(license_data.get("status")) or "not_provided",
            "source": _text(license_data.get("source")),
            "id_masked": _text(license_data.get("id_masked")),
            "expires_at": _text(license_data.get("expires_at")),
        },
        "tariff": {
            "mode": _text(tariff_data.get("mode")) or "dynamic",
            "label": _text(tariff_data.get("label")),
            "country": _text(tariff_data.get("country")) or "DE",
            "has_spot_component": bool(tariff_data.get("has_spot_component")),
            "feed_in_tariff_ct": _number(tariff_data.get("feed_in_tariff_ct")),
            "base_fee_eur_month": _number(tariff_data.get("base_fee_eur_month")),
        },
        "prices": {
            "current_total": _number(price_data.get("current_total")),
            "next_total": _number(price_data.get("next_total")),
            "average_today": _number(price_data.get("average_today")),
            "cheapest_hour": _number(price_data.get("cheapest_hour")),
            "most_expensive_hour": _number(price_data.get("most_expensive_hour")),
            "today": _price_points(price_data.get("today"), threshold_ct=threshold_ct),
            "tomorrow": _price_points(
                price_data.get("tomorrow"), threshold_ct=threshold_ct
            ),
        },
        "threshold_ct": threshold_ct,
        "thresholds": _thresholds(source_data.get("thresholds")),
        "is_cheap": bool(source_data.get("is_cheap")),
        "is_force_price": bool(source_data.get("is_force_price")),
        "next_cheap": _next_cheap(source_data.get("next_cheap")),
        "cheap_hours": {
            "today": _int_list(cheap_hours.get("today")),
            "tomorrow": _int_list(cheap_hours.get("tomorrow")),
        },
        "tomorrow_available": bool(
            source_data.get("tomorrow_available", price_data.get("tomorrow"))
        ),
        "price_components": price_components,
        "composition_ok": composition_ok,
        "corrections": corrections,
        "months": _months(source_data.get("months")),
        "effective_fees": _effective_fees(source_data.get("effective_fees")),
        "tariff_schedule": _tariff_schedule(source_data.get("tariff_schedule")),
        "diagnostics": {
            "last_fetch": _text(diagnostics.get("last_fetch")),
            "cache_age_seconds": _number(diagnostics.get("cache_age_seconds")),
            "price_revision": int(
                _number(diagnostics.get("price_revision"))
                or source_data.get("revision")
                or 0
            ),
            "last_correction": last_correction,
            "provider_version": _number(diagnostics.get("provider_version"))
            or _number(source_data.get("provider_version"))
            or 1,
            "composition_mismatch": bool(
                diagnostics.get("composition_mismatch", not composition_ok)
            ),
        },
        "revision": int(_number(source_data.get("revision")) or 0),
        "last_correction": last_correction,
        "capabilities": {
            "tariff_models": bool(capabilities.get("tariff_models")),
            "csv_import": bool(capabilities.get("csv_import")),
            "corrections": bool(capabilities.get("corrections")),
        },
        "links": {
            "configure": _text(links.get("configure")) or _CONFIGURE_LINK,
            "smart_charging": _text(links.get("smart_charging"))
            or _SMART_CHARGING_LINK,
        },
        "updated_at": _text(source_data.get("updated_at")),
    }
    if not public_status["tariff"]["has_spot_component"]:
        public_status["composition_ok"] = False
        public_status["diagnostics"]["composition_mismatch"] = bool(
            diagnostics.get("composition_mismatch", False)
        )
    assert set(public_status) == set(_TOP_KEYS)
    assert set(public_status["license"]) == set(_LICENSE_KEYS)
    assert set(public_status["tariff"]) == set(_TARIFF_KEYS)
    assert set(public_status["prices"]) == set(_PRICE_KEYS)
    assert set(public_status["capabilities"]) == set(_CAPABILITY_KEYS)
    assert set(public_status["links"]) == set(_LINK_KEYS)
    assert set(public_status["thresholds"]) == set(_THRESHOLD_KEYS)
    assert set(public_status["cheap_hours"]) == set(_CHEAP_HOURS_KEYS)
    assert set(public_status["effective_fees"]) == set(_FEE_KEYS)
    assert set(public_status["diagnostics"]) == set(_DIAGNOSTIC_KEYS)
    assert public_status["last_correction"] is None or set(
        public_status["last_correction"]
    ) == set(_CORRECTION_KEYS)
    assert public_status["next_cheap"] is None or set(
        public_status["next_cheap"]
    ) == set(_NEXT_CHEAP_KEYS)
    assert public_status["price_components"] is None or set(
        public_status["price_components"]
    ) == set(_COMPONENT_KEYS)
    assert public_status["tariff_schedule"] is None or set(
        public_status["tariff_schedule"]
    ) == set(_SCHEDULE_KEYS)
    for price_point in (
        public_status["prices"]["today"] + public_status["prices"]["tomorrow"]
    ):
        if set(price_point) == set(_POINT_KEYS):
            continue
        assert False
    for public_row in public_status["corrections"]:
        if set(public_row) == set(_CORRECTION_ROW_KEYS):
            continue
        assert False
    for public_row in public_status["months"]:
        if set(public_row) == set(_MONTH_KEYS):
            continue
        assert False
    return public_status


CSV_MAX_BYTES = 5242880
CSV_MULTIPART_OVERHEAD = 65536
CSV_PROFILES = frozenset({"auto", "price_list", "community_share"})
CSV_UNITS = frozenset({"auto", "ct_kwh", "eur_kwh", "eur_mwh"})
CSV_TIMEZONES = frozenset({"local", "utc"})
CSV_TEMPLATE_PROFILES = frozenset({"price_list", "community_share"})
CSV_TEMPLATE_LANGS = frozenset({"de", "en"})
_CSV_PUBLIC_KEYS = (
    "success",
    "profile",
    "unit",
    "headers",
    "column_map",
    "range_from",
    "range_to",
    "row_count",
    "skipped_rows",
    "hours_new",
    "hours_matched",
    "stale_months",
)
_CSV_APPLY_KEYS = _CSV_PUBLIC_KEYS + ("revision", "correction_id", "applied_at")
_CSV_TEMPLATE_HEADERS = {
    ("price_list", "de"): ("Zeitstempel", "Preis (ct/kWh)"),
    ("price_list", "en"): ("timestamp", "price_ct_kwh"),
    ("community_share", "de"): ("Zeitstempel", "Verbrauch (kWh)", "EEG-Anteil (kWh)"),
    ("community_share", "en"): ("timestamp", "total_kwh", "community_kwh"),
}


class _CsvLimitError(Exception):

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _csv_value(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _csv_headers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        header.strip() for header in value if isinstance(header, str) if header.strip()
    ]


def _csv_column_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    column_map = {}
    for field_name, column_value in value.items():
        column_name = _text(field_name) or str(field_name)
        mapped_column = _text(column_value)
        if column_name and mapped_column:
            column_map[column_name] = mapped_column
    return column_map


def _csv_stale_months(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    stale_months = []
    for month_value in value:
        month_text = _text(month_value)
        if month_text:
            stale_months.append(month_text)
    return stale_months


def public_csv_result(
    result: Mapping[str, Any] | None, *, applied: bool
) -> dict[str, Any]:
    """Browser-facing CSV preview/apply DTO. Private keys never leave this helper."""
    source_data = dict(result or {})
    parsed_csv = source_data.get("parsed")
    public_result = {
        "success": True,
        "profile": _text(_csv_value(parsed_csv, "profile", source_data.get("profile")))
        or "price_list",
        "unit": _text(
            _csv_value(
                parsed_csv,
                "unit",
                source_data.get("unit") or source_data.get("detected_unit"),
            )
        )
        or "ct_kwh",
        "headers": _csv_headers(
            _csv_value(parsed_csv, "headers", source_data.get("headers"))
        ),
        "column_map": _csv_column_map(
            _csv_value(parsed_csv, "column_map", source_data.get("column_map"))
        ),
        "range_from": _text(
            source_data.get("range_from")
            or source_data.get("from")
            or _csv_value(parsed_csv, "range_from")
        ),
        "range_to": _text(
            source_data.get("range_to")
            or source_data.get("to")
            or _csv_value(parsed_csv, "range_to")
        ),
        "row_count": int(
            _number(source_data.get("row_count") or _csv_value(parsed_csv, "row_count"))
            or 0
        ),
        "skipped_rows": int(
            _number(
                source_data.get("skipped_rows")
                or _csv_value(parsed_csv, "skipped_rows")
            )
            or 0
        ),
        "hours_new": int(_number(source_data.get("hours_new")) or 0),
        "hours_matched": int(_number(source_data.get("hours_matched")) or 0),
        "stale_months": _csv_stale_months(source_data.get("stale_months")),
    }
    if applied:
        public_result["revision"] = int(_number(source_data.get("revision")) or 0)
        public_result["correction_id"] = _number(source_data.get("correction_id"))
        public_result["applied_at"] = _text(source_data.get("applied_at"))
        assert set(public_result) == set(_CSV_APPLY_KEYS)
    else:
        assert set(public_result) == set(_CSV_PUBLIC_KEYS)
    return public_result if True else None


def csv_too_large_from_length(content_length: int | None) -> bool:
    if content_length is None:
        return False
    return int(content_length) > CSV_MAX_BYTES + CSV_MULTIPART_OVERHEAD


def _template_stamp(moment: datetime, lang: str) -> str:
    if lang == "de":
        return moment.strftime("%d.%m.%Y %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _template_number(value: float, lang: str) -> str:
    formatted_number = f"{value:.2f}"
    return formatted_number.replace(".", ",") if lang == "de" else formatted_number


def build_csv_template(profile: str, lang: str, *, today: date | None = None) -> bytes:
    normalized_profile = profile if profile in CSV_TEMPLATE_PROFILES else "price_list"
    normalized_language = lang if lang in CSV_TEMPLATE_LANGS else "de"
    headers = _CSV_TEMPLATE_HEADERS[normalized_profile, normalized_language]
    template_day = (today or date.today()) - timedelta(days=1)
    csv_lines = [";".join(headers)]
    is_price_list = normalized_profile == "price_list"
    for hour in range(24):
        timestamp = _template_stamp(
            datetime(template_day.year, template_day.month, template_day.day, hour, 0),
            normalized_language,
        )
        if is_price_list:
            csv_lines.append(
                f"{timestamp};{_template_number(20.0 + hour * 0.25, normalized_language)}"
            )
        else:
            csv_lines.append(
                f"{timestamp};{_template_number(1.2, normalized_language)};{_template_number(0.4, normalized_language)}"
            )
    for minute, price_increment in ((15, 0.1), (30, 0.2)):
        timestamp = _template_stamp(
            datetime(
                template_day.year, template_day.month, template_day.day, 0, minute
            ),
            normalized_language,
        )
        if is_price_list:
            csv_lines.append(
                f"{timestamp};{_template_number(20.0 + price_increment, normalized_language)}"
            )
        else:
            csv_lines.append(
                f"{timestamp};{_template_number(0.3, normalized_language)};{_template_number(0.1, normalized_language)}"
            )
    csv_text = "\n".join(csv_lines) + "\n"
    return b"\xef\xbb\xbf" + csv_text.encode("utf-8")


def csv_template_filename(profile: str, lang: str) -> str:
    normalized_profile = profile if profile in CSV_TEMPLATE_PROFILES else "price_list"
    normalized_language = lang if lang in CSV_TEMPLATE_LANGS else "de"
    return f"gpm_{normalized_profile}_{normalized_language}.csv"


def _gpm_coordinator(hass: Any) -> Any | None:
    gpm_entries = getattr(hass, "data", {}).get("grid_price_monitor") or {}
    for candidate_coordinator in gpm_entries.values():
        if hasattr(candidate_coordinator, "snapshot") or hasattr(
            candidate_coordinator, "data"
        ):
            return candidate_coordinator


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return _text(value)


def _forecast_points(rows: Any) -> list[dict[str, Any]]:
    price_points = []
    if not isinstance(rows, list):
        return price_points
    for forecast_row in rows:
        if isinstance(forecast_row, dict):
            price_points.append(
                {
                    "hour": forecast_row.get("hour"),
                    "total_price": forecast_row.get("total_price"),
                    "spot_price": forecast_row.get(
                        "spot_price", forecast_row.get("spot_price_net")
                    ),
                    "is_cheap": forecast_row.get("is_cheap"),
                }
            )
    return price_points


async def _last_correction(hass: Any) -> dict[str, Any] | None:
    correction_rows = await _correction_rows(hass, limit=1)
    return (
        None
        if not correction_rows
        else {
            "id": correction_rows[0]["id"],
            "kind": correction_rows[0]["kind"],
            "month": correction_rows[0]["month"],
            "applied_at": correction_rows[0]["applied_at"],
            "range_from": correction_rows[0]["range_from"],
            "range_to": correction_rows[0]["range_to"],
        }
    )


async def _correction_rows(hass: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    try:
        from .views import _get_db as get_database
    except Exception:
        return []
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT id, kind, status, month, range_from, range_to,\n                       applied_at, rows_affected, summary_json\n                FROM GPM_price_corrections\n                ORDER BY id DESC\n                LIMIT ?\n                ",
                (int(limit),),
            ) as cursor:
                database_rows = await cursor.fetchall()
    except Exception:
        return []
    correction_rows = []
    for correction_row in database_rows or []:
        correction_rows.append(
            {
                "id": correction_row[0],
                "kind": correction_row[1],
                "status": correction_row[2],
                "month": correction_row[3],
                "range_from": correction_row[4],
                "range_to": correction_row[5],
                "applied_at": correction_row[6],
                "rows_affected": correction_row[7],
                "summary_json": correction_row[8],
            }
        )
    return correction_rows


async def _month_rows(
    hass: Any, *, now: datetime | None = None, months: int = 12
) -> list[dict[str, Any]]:
    current_time = now or datetime.now().astimezone()
    current_month = f"{current_time.year:04d}-{current_time.month:02d}"
    month_count = _month_span(months)
    start_date = _month_window_start(current_time, month_count)
    try:
        from .views import _get_db as get_database
    except Exception:
        return []
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT substr(date, 1, 7) AS month,\n                       AVG(average_total) AS average,\n                       MIN(average_total) AS min_price,\n                       MAX(average_total) AS max_price\n                FROM GPM_daily_averages\n                WHERE date >= ?\n                GROUP BY substr(date, 1, 7)\n                ORDER BY month DESC\n                LIMIT ?\n                ",
                (start_date, month_count),
            ) as cursor:
                monthly_average_rows = await cursor.fetchall()
            cheap_hours_by_month = {}
            try:
                async with database.execute(
                    "\n                    SELECT month, cheap_hours\n                    FROM GPM_monthly_summaries\n                    "
                ) as cursor:
                    for cheap_hours_row in await cursor.fetchall():
                        cheap_hours_by_month[str(cheap_hours_row[0])] = cheap_hours_row[
                            1
                        ]
            except Exception:
                cheap_hours_by_month = {}
    except Exception:
        return []
    month_rows = []
    for monthly_average_row in monthly_average_rows or []:
        month = monthly_average_row[0]
        month_rows.append(
            {
                "month": month,
                "average": monthly_average_row[1],
                "min": monthly_average_row[2],
                "max": monthly_average_row[3],
                "cheap_hours": cheap_hours_by_month.get(month),
                "current": month == current_month,
            }
        )
    return month_rows


def _month_span(value: Any, default: int = 12) -> int:
    try:
        month_count = int(value)
    except (TypeError, ValueError):
        return default
    if month_count < 1 or month_count > 120:
        return default
    return month_count


def _month_window_start(clock: datetime, months: int) -> str:
    start_year = clock.year
    start_month = clock.month - months
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return f"{start_year:04d}-{start_month:02d}-01"


async def _cache_meta(hass: Any) -> dict[str, Any]:
    empty_cache_meta = {"last_fetch": None, "cache_age_seconds": None}
    try:
        from .views import _get_db as get_database
    except Exception:
        return empty_cache_meta
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT last_fetch FROM GPM_price_cache_meta WHERE id = 1\n                "
            ) as cursor:
                cache_row = await cursor.fetchone()
    except Exception:
        return empty_cache_meta
    if not cache_row or not cache_row[0]:
        return empty_cache_meta
    last_fetch_value = cache_row[0]
    cache_age_seconds = None
    last_fetch_time = None
    if isinstance(last_fetch_value, datetime):
        last_fetch_time = last_fetch_value
    elif isinstance(last_fetch_value, str):
        try:
            last_fetch_time = datetime.fromisoformat(last_fetch_value)
        except ValueError:
            last_fetch_time = None
    if last_fetch_time is not None:
        if last_fetch_time.tzinfo is None:
            last_fetch_time = last_fetch_time.replace(tzinfo=timezone.utc)
        cache_age_seconds = int(
            (
                datetime.now(timezone.utc) - last_fetch_time.astimezone(timezone.utc)
            ).total_seconds()
        )
    return (
        {
            "last_fetch": (
                last_fetch_value
                if isinstance(last_fetch_value, str)
                else _iso(last_fetch_time)
            ),
            "cache_age_seconds": cache_age_seconds,
        }
        if True
        else None
    )


async def _fallback_current_price(hass: Any) -> dict[str, Any]:
    empty_prices = {
        "current_total": None,
        "next_total": None,
        "average_today": None,
        "cheapest_hour": None,
        "most_expensive_hour": None,
        "today": [],
        "tomorrow": [],
    }
    try:
        from .views import _get_db as get_database
    except Exception:
        return empty_prices
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT total_price, price_next_hour, average_today,\n                       cheapest_today, most_expensive_today\n                FROM GPM_current_price\n                WHERE id = 1\n                "
            ) as cursor:
                current_price_row = await cursor.fetchone()
    except Exception:
        return empty_prices
    if not current_price_row:
        return empty_prices
    return {
        "current_total": current_price_row[0],
        "next_total": current_price_row[1],
        "average_today": current_price_row[2],
        "cheapest_hour": None,
        "most_expensive_hour": None,
        "today": [],
        "tomorrow": [],
    }


def _from_snapshot(
    snapshot: dict[str, Any],
    data: dict[str, Any],
    *,
    state: str,
    last_correction: dict[str, Any] | None,
    corrections: list[dict[str, Any]] | None = None,
    months: list[dict[str, Any]] | None = None,
    cache_meta: dict[str, Any] | None = None,
    effective_fees: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    capabilities = (
        snapshot.get("capabilities")
        if isinstance(snapshot.get("capabilities"), dict)
        else {}
    )
    threshold_ct = _number(data.get("max_price_threshold"))
    today_prices = _forecast_points(data.get("forecast_today"))
    tomorrow_prices = _forecast_points(data.get("forecast_tomorrow"))
    price_components = (
        snapshot.get("price_components")
        if isinstance(snapshot.get("price_components"), dict)
        else None
    )
    spot_net = _number(data.get("spot_price_net"))
    total_price = _number(data.get("total_price"))
    has_spot_component = bool(
        snapshot.get("has_spot_component", data.get("has_spot_component"))
    )
    composition_ok = has_spot_component and composition_matches(
        spot_net=spot_net, total=total_price, components=price_components
    )
    next_cheap_hour = data.get("next_cheap_hour")
    next_cheap_timestamp = data.get("next_cheap_timestamp")
    next_cheap = None
    if next_cheap_hour is not None or next_cheap_timestamp:
        next_cheap = {"hour": next_cheap_hour, "timestamp": next_cheap_timestamp}
    current_time = now or datetime.now().astimezone()
    tomorrow_available = bool(tomorrow_prices)
    return {
        "success": True,
        "state": state,
        "is_demo": bool(snapshot.get("is_demo") or state == "gpm_demo"),
        "provider_version": snapshot.get("provider_version") or 1,
        "license": {
            "status": snapshot.get("license_status") or "not_provided",
            "source": snapshot.get("license_source"),
            "id_masked": snapshot.get("license_id_masked"),
            "expires_at": snapshot.get("license_expires_at"),
        },
        "tariff": {
            "mode": snapshot.get("tariff_mode") or data.get("tariff_mode") or "dynamic",
            "label": snapshot.get("tariff_label") or data.get("tariff_label"),
            "country": snapshot.get("country") or data.get("country") or "DE",
            "has_spot_component": has_spot_component,
            "feed_in_tariff_ct": snapshot.get(
                "feed_in_tariff_ct", data.get("feed_in_tariff_ct")
            ),
            "base_fee_eur_month": snapshot.get(
                "base_fee_eur_month", data.get("base_fee_eur_month")
            ),
        },
        "prices": {
            "current_total": total_price,
            "next_total": data.get("total_price_next_hour"),
            "average_today": data.get("average_price_today"),
            "cheapest_hour": data.get("cheapest_hour_today"),
            "most_expensive_hour": data.get("most_expensive_hour_today"),
            "current_spot_net": spot_net,
            "today": today_prices,
            "tomorrow": tomorrow_prices,
        },
        "threshold_ct": threshold_ct,
        "thresholds": {
            "mode": (
                (data.get("thresholds") or {}).get("mode")
                if isinstance(data.get("thresholds"), dict)
                else snapshot.get("threshold_mode")
            ),
            "max_price": (
                (data.get("thresholds") or {}).get("max_price")
                if isinstance(data.get("thresholds"), dict)
                else threshold_ct
            ),
            "force_charge_price": (
                (data.get("thresholds") or {}).get("force_charge_price")
                if isinstance(data.get("thresholds"), dict)
                else None
            ),
            "below_average_pct": (
                (data.get("thresholds") or {}).get("below_average_pct")
                if isinstance(data.get("thresholds"), dict)
                else None
            ),
            "cheapest_hours": (
                (data.get("thresholds") or {}).get("cheapest_hours")
                if isinstance(data.get("thresholds"), dict)
                else None
            ),
        },
        "is_cheap": bool(data.get("is_cheap")),
        "is_force_price": bool(data.get("is_force_price")),
        "next_cheap": next_cheap,
        "cheap_hours": {
            "today": data.get("cheap_hours_today") or [],
            "tomorrow": data.get("cheap_hours_tomorrow") or [],
        },
        "tomorrow_available": tomorrow_available,
        "price_components": price_components,
        "composition_ok": composition_ok,
        "corrections": corrections or [],
        "months": months or [],
        "effective_fees": effective_fees or {},
        "tariff_schedule": snapshot.get("tariff_schedule"),
        "diagnostics": {
            "last_fetch": None if not cache_meta else cache_meta.get("last_fetch"),
            "cache_age_seconds": (
                None if not cache_meta else cache_meta.get("cache_age_seconds")
            ),
            "price_revision": snapshot.get("price_revision")
            or data.get("price_revision")
            or 0,
            "last_correction": last_correction,
            "provider_version": snapshot.get("provider_version") or 1,
            "composition_mismatch": bool(has_spot_component and (not composition_ok)),
        },
        "revision": snapshot.get("price_revision") or data.get("price_revision") or 0,
        "last_correction": last_correction or snapshot.get("last_correction"),
        "capabilities": {
            "tariff_models": bool(capabilities.get("tariff_models")),
            "csv_import": bool(capabilities.get("csv_import")),
            "corrections": bool(capabilities.get("corrections")),
        },
        "links": {"configure": _CONFIGURE_LINK, "smart_charging": _SMART_CHARGING_LINK},
        "updated_at": _iso(data.get("last_update"))
        or current_time.astimezone(timezone.utc).isoformat(),
    }


def _license_from_snapshot(
    snapshot: Mapping[str, Any], data: Mapping[str, Any]
) -> dict[str, Any]:
    license_data = (
        snapshot.get("license") if isinstance(snapshot.get("license"), dict) else {}
    )
    return {
        "status": snapshot.get("license_status")
        or license_data.get("status")
        or data.get("license_status")
        or "not_provided",
        "source": snapshot.get("license_source") or license_data.get("source"),
        "id_masked": snapshot.get("license_id_masked"),
        "expires_at": snapshot.get("license_expires_at"),
    }


def _billing_start_from_config(config: Mapping[str, Any] | None) -> tuple[int, int]:
    from ..const import (
        CONF_BILLING_START_DAY as billing_start_day_key,
        CONF_BILLING_START_MONTH as billing_start_month_key,
    )

    billing_config = config or {}
    return (
        billing_config.get(billing_start_month_key, 1),
        billing_config.get(billing_start_day_key, 1),
    )


def _hass_today(hass: Any) -> date:
    try:
        local_now = hass.config.as_local(datetime.now(timezone.utc))
        return local_now.date()
    except Exception:
        return datetime.now().date()


async def _hourly_billing_rows(start: date, end: date) -> list[dict[str, Any]]:
    from .views import _get_db as get_database

    start_iso = start.isoformat()
    end_iso = end.isoformat()
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT date, hour, grid_import_kwh, price_ct_kwh\n                FROM stats_hourly_billing\n                WHERE date >= ? AND date < ?\n                ",
                (start_iso, end_iso),
            ) as cursor:
                database_rows = await cursor.fetchall()
    except Exception:
        return []
    billing_rows = []
    for billing_row in database_rows:
        billing_rows.append(
            {
                "date": billing_row[0],
                "hour": billing_row[1],
                "grid_import_kwh": billing_row[2],
                "price_ct_kwh": billing_row[3],
            }
        )
    return billing_rows


async def _billing_date_list() -> list[str]:
    from .views import _get_db as get_database

    try:
        async with get_database() as database:
            async with database.execute(
                "SELECT DISTINCT date FROM stats_hourly_billing WHERE date IS NOT NULL"
            ) as cursor:
                database_rows = await cursor.fetchall()
    except Exception:
        return []
    return [str(date_row[0]) for date_row in database_rows if date_row if date_row[0]]


async def _recent_gpm_average_ct(today: date) -> float | None:
    from .views import _get_db as get_database

    start_date = (today - timedelta(days=30)).isoformat()
    try:
        async with get_database() as database:
            async with database.execute(
                "\n                SELECT AVG(total_price)\n                FROM GPM_price_history\n                WHERE total_price IS NOT NULL AND total_price > 0\n                  AND timestamp >= ?\n                ",
                (start_date,),
            ) as cursor:
                average_row = await cursor.fetchone()
    except Exception:
        return
    if not average_row or average_row[0] is None:
        return
    try:
        return float(average_row[0])
    except (TypeError, ValueError):
        return


class GPMStatusView(HomeAssistantView):
    """GET /api/sfml_stats/gpm/status — local, read-only GPM DTO."""

    url = "/api/sfml_stats/gpm/status"
    name = "api:sfml_stats:gpm_status"
    requires_auth = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        coordinator = _gpm_coordinator(self.hass)
        if coordinator is None:
            status_payload = public_gpm_status(scenario_payload("not_installed"))
            return web.json_response(
                _without_dev_diagnostics(self.hass, status_payload),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
                },
            )
        coordinator_data = dict(getattr(coordinator, "data", None) or {})
        snapshot = {}
        if hasattr(coordinator, "snapshot"):
            try:
                snapshot = dict(coordinator.snapshot() or {})
            except Exception:
                snapshot = {}
        last_correction = await _last_correction(self.hass)
        corrections = await _correction_rows(self.hass)
        months = await _month_rows(
            self.hass, months=_month_span(request.query.get("months"))
        )
        cache_meta = await _cache_meta(self.hass)
        fee_data = {}
        try:
            from ..core.price_mode import effective_fees as resolve_effective_fees
            from .views import _get_config as get_config

            fee_data = resolve_effective_fees(self.hass, get_config())
        except Exception:
            fee_data = {}
        status_details = {
            "corrections": corrections,
            "months": months,
            "cache_meta": cache_meta,
            "effective_fees": fee_data,
        }
        provider_version = snapshot.get("provider_version")
        if provider_version not in (1, None):
            fallback_prices = await _fallback_current_price(self.hass)
            status_payload = public_gpm_status(
                {
                    "state": "incompatible",
                    "is_demo": False,
                    "provider_version": provider_version,
                    "license": {
                        "status": snapshot.get("license_status") or "not_provided",
                        "source": snapshot.get("license_source"),
                        "id_masked": snapshot.get("license_id_masked"),
                        "expires_at": snapshot.get("license_expires_at"),
                    },
                    "tariff": {
                        "mode": snapshot.get("tariff_mode")
                        or coordinator_data.get("tariff_mode"),
                        "label": snapshot.get("tariff_label")
                        or coordinator_data.get("tariff_label"),
                        "country": snapshot.get("country")
                        or coordinator_data.get("country")
                        or "DE",
                        "has_spot_component": bool(
                            snapshot.get(
                                "has_spot_component",
                                coordinator_data.get("has_spot_component"),
                            )
                        ),
                        "feed_in_tariff_ct": snapshot.get("feed_in_tariff_ct"),
                        "base_fee_eur_month": snapshot.get("base_fee_eur_month"),
                    },
                    "prices": {
                        "current_total": coordinator_data.get(
                            "total_price", fallback_prices["current_total"]
                        ),
                        "next_total": coordinator_data.get(
                            "total_price_next_hour", fallback_prices["next_total"]
                        ),
                        "average_today": coordinator_data.get(
                            "average_price_today", fallback_prices["average_today"]
                        ),
                        "cheapest_hour": coordinator_data.get("cheapest_hour_today"),
                        "most_expensive_hour": coordinator_data.get(
                            "most_expensive_hour_today"
                        ),
                        "today": _forecast_points(
                            coordinator_data.get("forecast_today")
                        ),
                        "tomorrow": _forecast_points(
                            coordinator_data.get("forecast_tomorrow")
                        ),
                    },
                    "revision": snapshot.get("price_revision") or 0,
                    "last_correction": last_correction,
                    "corrections": status_details["corrections"],
                    "months": status_details["months"],
                    "effective_fees": status_details["effective_fees"],
                    "diagnostics": status_details["cache_meta"],
                    "capabilities": snapshot.get("capabilities") or {},
                    "links": {
                        "configure": _CONFIGURE_LINK,
                        "smart_charging": _SMART_CHARGING_LINK,
                    },
                    "updated_at": _iso(coordinator_data.get("last_update")),
                }
            )
            return web.json_response(
                _without_dev_diagnostics(self.hass, status_payload),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
                },
            )
        public_state = (
            "gpm_demo"
            if snapshot.get("is_demo") or coordinator_data.get("is_demo")
            else "live"
        )
        status_payload = public_gpm_status(
            _from_snapshot(
                snapshot,
                coordinator_data,
                state=public_state,
                last_correction=last_correction,
                corrections=status_details["corrections"],
                months=status_details["months"],
                cache_meta=status_details["cache_meta"],
                effective_fees=status_details["effective_fees"],
            )
        )
        return web.json_response(
            _without_dev_diagnostics(self.hass, status_payload),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


def _without_dev_diagnostics(hass: Any, payload: dict) -> dict:
    """Drop GPM diagnostics unless DEV mode is active."""
    from ..core.dev_mode import is_dev_active as is_dev_mode_active

    if is_dev_mode_active(hass) or not isinstance(payload, dict):
        return payload
    filtered_payload = dict(payload)
    filtered_payload.pop("diagnostics", None)
    return filtered_payload


def _admin_forbidden(request: web.Request) -> web.Response | None:
    user = request.get("hass_user")
    if user is None or not bool(getattr(user, "is_admin", False)):
        return web.json_response(
            {"success": False, "error": "admin_required"},
            status=403,
            headers={"Cache-Control": "no-store"},
        )


def revert_gate(coordinator: Any) -> web.Response | None:
    """401 is handled by requires_auth; this covers 403 demo/license."""
    if coordinator is None or not hasattr(coordinator, "async_revert_correction"):
        return web.json_response(
            {"success": False, "error": "gpm_not_installed"},
            status=404,
            headers={"Cache-Control": "no-store"},
        )
    is_demo = bool(getattr(coordinator, "is_demo", False))
    if is_demo:
        return web.json_response(
            {"success": False, "error": "demo_forbidden"},
            status=403,
            headers={"Cache-Control": "no-store"},
        )
    snapshot = {}
    if hasattr(coordinator, "snapshot"):
        try:
            snapshot = dict(coordinator.snapshot() or {})
        except Exception:
            snapshot = {}
    capabilities = (
        snapshot.get("capabilities")
        if isinstance(snapshot.get("capabilities"), dict)
        else {}
    )
    if not bool(capabilities.get("corrections")):
        return web.json_response(
            {"success": False, "error": "correction_license_required"},
            status=403,
            headers={"Cache-Control": "no-store"},
        )


class GPMCorrectMonthView(HomeAssistantView):
    """POST /api/sfml_stats/gpm/correct_month — preview or apply via GPM."""

    url = "/api/sfml_stats/gpm/correct_month"
    name = "api:sfml_stats:gpm_correct_month"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        coordinator = _gpm_coordinator(self.hass)
        if coordinator is None or not hasattr(
            coordinator, "async_preview_month_correction"
        ):
            return web.json_response(
                {"success": False, "error": "gpm_not_installed"},
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        try:
            request_payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(request_payload, dict):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            correction_options = {
                "year": int(request_payload["year"]),
                "month": int(request_payload["month"]),
                "billed_kwh": float(request_payload["billed_kwh"]),
                "billed_eur": float(request_payload["billed_eur"]),
                "base_fee_eur": float(request_payload.get("base_fee_eur") or 0.0),
                "method": str(request_payload.get("method") or "additive"),
                "weighting": str(request_payload.get("weighting") or "consumption"),
                "force": bool(request_payload.get("force")),
            }
        except (KeyError, TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        apply_correction = bool(request_payload.get("apply"))
        try:
            if apply_correction:
                operation_result = await coordinator.async_apply_month_correction(
                    **correction_options
                )
            else:
                operation_result = await coordinator.async_preview_month_correction(
                    **correction_options
                )
        except Exception as operation_error:
            error_key = getattr(operation_error, "key", None) or "request_failed"
            return web.json_response(
                {"success": False, "error": str(error_key)},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"success": True, **operation_result},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


async def _read_limited_part(part: Any, limit: int) -> bytes:
    buffer = bytearray()
    read_part = getattr(part, "read", None)
    if callable(read_part):
        part_data = await read_part()
        if isinstance(part_data, str):
            part_data = part_data.encode("utf-8")
        if not isinstance(part_data, (bytes, bytearray)):
            part_data = b""
        if len(part_data) > limit:
            raise _CsvLimitError("csv_too_large")
        return bytes(part_data)
    async for chunk in part:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise _CsvLimitError("csv_too_large")
    return bytes(buffer)


async def _read_csv_upload(request: web.Request) -> tuple[bytes, dict[str, str]]:
    if csv_too_large_from_length(request.content_length):
        raise _CsvLimitError("csv_too_large")
    try:
        multipart_reader = await request.multipart()
    except Exception as operation_error:
        raise _CsvLimitError("invalid_payload") from operation_error
    form_fields = {}
    csv_bytes = None
    while True:
        part = await multipart_reader.next()
        if part is None:
            break
        column_name = getattr(part, "name", None)
        if not column_name:
            continue
        if column_name == "file":
            csv_bytes = await _read_limited_part(part, CSV_MAX_BYTES)
            continue
        field_bytes = await _read_limited_part(part, 65536)
        form_fields[str(column_name)] = field_bytes.decode("utf-8", errors="replace")
    if not csv_bytes:
        raise _CsvLimitError("invalid_payload")
    if len(csv_bytes) > CSV_MAX_BYTES:
        raise _CsvLimitError("csv_too_large")
    return (csv_bytes, form_fields)


def _csv_form_options(fields: Mapping[str, str]) -> dict[str, Any] | web.Response:
    csv_profile = _text(fields.get("profile")) or "auto"
    price_unit = _text(fields.get("price_unit")) or "auto"
    csv_timezone = _text(fields.get("timezone")) or "local"
    if (
        csv_profile not in CSV_PROFILES
        or price_unit not in CSV_UNITS
        or csv_timezone not in CSV_TIMEZONES
    ):
        return web.json_response(
            {"success": False, "error": "invalid_payload"},
            status=400,
            headers={"Cache-Control": "no-store"},
        )
    column_map = None
    column_map_text = fields.get("column_map")
    if column_map_text and column_map_text.strip():
        try:
            decoded_column_map = json.loads(column_map_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(decoded_column_map, dict):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        column_map = _csv_column_map(decoded_column_map) or None
    return {
        "profile": csv_profile,
        "price_unit": price_unit,
        "csv_timezone": csv_timezone,
        "column_map": column_map,
    }


def _csv_error_response(err: BaseException) -> web.Response:
    error_key = getattr(err, "key", None) or "request_failed"
    error_text = str(error_key)
    error_status = 413 if error_text == "csv_too_large" else 400
    return web.json_response(
        {"success": False, "error": error_text},
        status=error_status,
        headers={"Cache-Control": "no-store"},
    )


class _GPMCsvUploadView(HomeAssistantView):
    """Shared admin/local CSV upload handler."""

    requires_auth = True

    def __init__(self, hass: Any, *, apply_change: bool) -> None:
        self.hass = hass
        self._apply_change = apply_change

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        coordinator = _gpm_coordinator(self.hass)
        csv_method_name = (
            "async_import_csv_bytes"
            if self._apply_change
            else "async_preview_csv_bytes"
        )
        if coordinator is None or not hasattr(coordinator, csv_method_name):
            return web.json_response(
                {"success": False, "error": "gpm_not_installed"},
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        try:
            csv_data, form_fields = await _read_csv_upload(request)
        except _CsvLimitError as operation_error:
            return _csv_error_response(operation_error)
        import_options = _csv_form_options(form_fields)
        if isinstance(import_options, web.Response):
            return import_options
        try:
            import_method = getattr(coordinator, csv_method_name)
            operation_result = await import_method(
                csv_data,
                profile=import_options["profile"],
                price_unit=import_options["price_unit"],
                csv_timezone=import_options["csv_timezone"],
                column_map=import_options["column_map"],
            )
        except Exception as operation_error:
            return _csv_error_response(operation_error)
        csv_result = public_csv_result(operation_result, applied=self._apply_change)
        return web.json_response(
            csv_result,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


class GPMRevertCorrectionView(HomeAssistantView):
    """POST /api/sfml_stats/gpm/revert_correction — undo via GPM coordinator."""

    url = "/api/sfml_stats/gpm/revert_correction"
    name = "api:sfml_stats:gpm_revert_correction"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        coordinator = _gpm_coordinator(self.hass)
        revert_error_response = revert_gate(coordinator)
        if revert_error_response is not None:
            return revert_error_response
        try:
            request_payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(request_payload, dict):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            correction_id = int(request_payload["correction_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            operation_result = await coordinator.async_revert_correction(correction_id)
        except Exception as operation_error:
            error_key = getattr(operation_error, "key", None) or "request_failed"
            error_text = str(error_key)
            error_status = (
                403
                if error_text in {"demo_forbidden", "correction_license_required"}
                else 400
            )
            return web.json_response(
                {"success": False, "error": error_text},
                status=error_status,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {
                "success": True,
                **(operation_result if isinstance(operation_result, dict) else {}),
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


class GPMCsvPreviewView(_GPMCsvUploadView):
    """POST /api/sfml_stats/gpm/csv/preview — multipart CSV preview."""

    url = "/api/sfml_stats/gpm/csv/preview"
    name = "api:sfml_stats:gpm_csv_preview"

    def __init__(self, hass: Any) -> None:
        super().__init__(hass, apply_change=False)


class GPMCsvApplyView(_GPMCsvUploadView):
    """POST /api/sfml_stats/gpm/csv/apply — multipart CSV apply."""

    url = "/api/sfml_stats/gpm/csv/apply"
    name = "api:sfml_stats:gpm_csv_apply"

    def __init__(self, hass: Any) -> None:
        super().__init__(hass, apply_change=True)


class GPMCsvTemplateView(HomeAssistantView):
    """GET /api/sfml_stats/gpm/csv/template — example CSV download."""

    url = "/api/sfml_stats/gpm/csv/template"
    name = "api:sfml_stats:gpm_csv_template"
    requires_auth = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        csv_profile = _text(request.query.get("profile")) or "price_list"
        language = _text(request.query.get("lang")) or "de"
        if (
            csv_profile not in CSV_TEMPLATE_PROFILES
            or language not in CSV_TEMPLATE_LANGS
        ):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        template_bytes = build_csv_template(csv_profile, language)
        filename = csv_template_filename(csv_profile, language)
        return web.Response(
            body=template_bytes,
            status=200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


class GPMMonthlyCostsView(HomeAssistantView):
    """GET /api/sfml_stats/gpm/monthly_costs — local, read-only monthly bill."""

    url = "/api/sfml_stats/gpm/monthly_costs"
    name = "api:sfml_stats:gpm_monthly_costs"
    requires_auth = False

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        from ..core.daily_costs import load_daily_costs as load_daily_costs
        from .gpm_monthly import (
            build_monthly_costs as build_monthly_costs,
            license_allows_real_monthly_costs as license_allows_real_costs,
            monthly_costs_demo_payload as build_demo_monthly_costs,
            period_bounds as period_bounds,
        )
        from .views import _get_config as get_config, _get_db as get_database

        year_query = request.query.get("year")
        period_mode = (request.query.get("mode") or "calendar").strip().lower()
        if period_mode not in {"billing", "calendar"}:
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        today_date = _hass_today(self.hass)
        try:
            year = int(year_query) if year_query not in (None, "") else today_date.year
        except (TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        stats_config = {}
        try:
            stats_config = get_config() or {}
        except Exception:
            stats_config = {}
        billing_start_month, billing_start_day = _billing_start_from_config(
            stats_config
        )
        coordinator = _gpm_coordinator(self.hass)
        snapshot = {}
        coordinator_data = {}
        if coordinator is not None:
            coordinator_data = dict(getattr(coordinator, "data", None) or {})
            if hasattr(coordinator, "snapshot"):
                try:
                    snapshot = dict(coordinator.snapshot() or {})
                except Exception:
                    snapshot = {}
        license_data = _license_from_snapshot(snapshot, coordinator_data)
        real_costs_allowed = coordinator is not None and license_allows_real_costs(
            license_data
        )
        response_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
        }
        if not real_costs_allowed:
            monthly_costs = build_demo_monthly_costs(
                year,
                period_mode,
                start_month=int(billing_start_month or 1),
                start_day=int(billing_start_day or 1),
            )
            return web.json_response(monthly_costs, headers=response_headers)
        try:
            from ..core.price_mode import (
                base_fee_eur_month as resolve_base_fee_eur_month,
            )

            base_fee_eur = float(
                resolve_base_fee_eur_month(self.hass, stats_config) or 0.0
            )
        except Exception:
            base_fee_eur = 0.0
        period_start, period_end = period_bounds(
            year,
            period_mode,
            int(billing_start_month or 1),
            int(billing_start_day or 1),
        )
        previous_period_start, previous_period_end = period_bounds(
            year - 1,
            period_mode,
            int(billing_start_month or 1),
            int(billing_start_day or 1),
        )
        hourly_billing_rows = await _hourly_billing_rows(
            previous_period_start, period_end
        )
        try:
            async with get_database() as database:
                daily_cost_rows = await load_daily_costs(
                    database, previous_period_start, period_end
                )
        except Exception:
            daily_cost_rows = []
        billing_dates = await _billing_date_list()
        all_billing_dates = list(
            {str(billing_date) for billing_date in billing_dates}
            | {
                str(daily_cost_row.get("date"))
                for daily_cost_row in daily_cost_rows
                if daily_cost_row.get("date")
            }
        )
        recent_average_ct = await _recent_gpm_average_ct(today_date)
        monthly_costs = build_monthly_costs(
            year=year,
            mode=period_mode,
            start_month=int(billing_start_month or 1),
            start_day=int(billing_start_day or 1),
            today=today_date,
            base_fee_eur=base_fee_eur,
            rows=hourly_billing_rows,
            date_list=all_billing_dates,
            recent_avg_ct=recent_average_ct,
            is_demo=False,
            license_required=False,
            daily_rows=daily_cost_rows,
        )
        return web.json_response(monthly_costs, headers=response_headers)


def _invoice_error(err: BaseException) -> web.Response:
    error_key = (
        getattr(err, "key", None) or getattr(err, "code", None) or "request_failed"
    )
    return web.json_response(
        {"success": False, "error": str(error_key)},
        status=400,
        headers={"Cache-Control": "no-store"},
    )


class GPMInvoicesView(HomeAssistantView):
    """GET/POST /api/sfml_stats/gpm/invoices — stats_invoice_archive CRUD."""

    url = "/api/sfml_stats/gpm/invoices"
    name = "api:sfml_stats:gpm_invoices"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        from ..core.invoice_archive import (
            InvoiceArchiveError as invoice_archive_error_type,
            list_invoices as list_invoices,
        )
        from .views import _get_db as get_database

        try:
            async with get_database() as database:
                invoices = await list_invoices(database)
        except invoice_archive_error_type as operation_error:
            return _invoice_error(operation_error)
        except Exception:
            return _invoice_error(invoice_archive_error_type("request_failed"))
        return web.json_response(
            {"success": True, "invoices": invoices},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @local_only
    async def post(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        from ..core.invoice_archive import (
            InvoiceArchiveError as invoice_archive_error_type,
            upsert_invoice as upsert_invoice,
        )
        from .views import _get_db as get_database

        try:
            request_payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid_json"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(request_payload, dict):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            async with get_database() as database:
                invoice = await upsert_invoice(database, request_payload)
        except invoice_archive_error_type as operation_error:
            return _invoice_error(operation_error)
        except Exception:
            return _invoice_error(invoice_archive_error_type("request_failed"))
        return web.json_response(
            {"success": True, "invoice": invoice},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


class GPMInvoiceItemView(HomeAssistantView):
    """DELETE /api/sfml_stats/gpm/invoices/{invoice_id} — stats_invoice_archive."""

    url = "/api/sfml_stats/gpm/invoices/{invoice_id}"
    name = "api:sfml_stats:gpm_invoice_item"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def delete(self, request: web.Request, invoice_id: str) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        from ..core.invoice_archive import (
            InvoiceArchiveError as invoice_archive_error_type,
            delete_invoice as delete_invoice,
        )
        from .views import _get_db as get_database

        try:
            parsed_invoice_id = int(invoice_id)
        except (TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "invalid_payload"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            async with get_database() as database:
                await delete_invoice(database, parsed_invoice_id)
        except invoice_archive_error_type as operation_error:
            return _invoice_error(operation_error)
        except Exception:
            return _invoice_error(invoice_archive_error_type("request_failed"))
        return web.json_response(
            {"success": True},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


class GPMYearlyHistoryView(HomeAssistantView):
    """GET /api/sfml_stats/gpm/yearly_history — calendar-year rows with invoices and gap fill."""

    url = "/api/sfml_stats/gpm/yearly_history"
    name = "api:sfml_stats:gpm_yearly_history"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        admin_error_response = _admin_forbidden(request)
        if admin_error_response is not None:
            return admin_error_response
        from ..core.yearly_history import build_yearly_history as build_yearly_history

        try:
            yearly_rows = await build_yearly_history(self.hass)
        except Exception:
            return web.json_response(
                {"success": False, "error": "request_failed"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"rows": yearly_rows},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


async def async_setup_gpm_views(hass: Any) -> None:
    hass.http.register_view(GPMStatusView(hass))
    hass.http.register_view(GPMMonthlyCostsView(hass))
    hass.http.register_view(GPMCorrectMonthView(hass))
    hass.http.register_view(GPMRevertCorrectionView(hass))
    hass.http.register_view(GPMCsvPreviewView(hass))
    hass.http.register_view(GPMCsvApplyView(hass))
    hass.http.register_view(GPMCsvTemplateView(hass))
    hass.http.register_view(GPMInvoicesView(hass))
    hass.http.register_view(GPMInvoiceItemView(hass))
    hass.http.register_view(GPMYearlyHistoryView(hass))
