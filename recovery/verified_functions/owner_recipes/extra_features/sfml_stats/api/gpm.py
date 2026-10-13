# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..mock.gpm import scenario_payload
from .views import local_only


def public_gpm_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the browser-facing GPM DTO. Private keys never leave this helper."""
    _var_var_13 = {
        _var_var_0: _var_var_26
        for _var_var_0, _var_var_26 in (payload or {}).items()
        if _var_var_0 not in _PRIVATE_KEYS
    }
    _var_var_27 = _var_var_13.get("state")
    if _var_var_27 not in PUBLIC_STATES:
        _var_var_27 = "not_installed"
    _var_var_28 = (
        _var_var_13.get("license")
        if isinstance(_var_var_13.get("license"), dict)
        else {}
    )
    _var_var_29 = (
        _var_var_13.get("tariff") if isinstance(_var_var_13.get("tariff"), dict) else {}
    )
    _var_var_30 = (
        _var_var_13.get("prices") if isinstance(_var_var_13.get("prices"), dict) else {}
    )
    _var_var_31 = (
        _var_var_13.get("capabilities")
        if isinstance(_var_var_13.get("capabilities"), dict)
        else {}
    )
    _var_var_32 = (
        _var_var_13.get("links") if isinstance(_var_var_13.get("links"), dict) else {}
    )
    _var_var_33 = _number(_var_var_13.get("threshold_ct"))
    _var_var_34 = _price_components(_var_var_13.get("price_components"))
    _var_var_35 = _correction(_var_var_13.get("last_correction"))
    _var_var_36 = (
        bool(_var_var_13.get("composition_ok"))
        if "composition_ok" in _var_var_13
        else False
    )
    if "composition_ok" not in _var_var_13:
        _var_var_36 = composition_matches(
            spot_net=_number(
                _var_var_30.get("current_spot_net") or _var_var_30.get("spot_net")
            ),
            total=_number(_var_var_30.get("current_total")),
            components=_var_var_34,
        )
    _var_var_37 = (
        _var_var_13.get("diagnostics")
        if isinstance(_var_var_13.get("diagnostics"), dict)
        else {}
    )
    _var_var_38 = (
        _var_var_13.get("cheap_hours")
        if isinstance(_var_var_13.get("cheap_hours"), dict)
        else {}
    )
    _var_var_39 = (
        _var_var_13.get("corrections")
        if isinstance(_var_var_13.get("corrections"), list)
        else []
    )
    _var_var_40 = [
        _var_var_4
        for _var_var_4 in (_correction_row(_var_var_10) for _var_var_10 in _var_var_39)
        if _var_var_4
    ]
    _var_var_41 = {
        "success": True,
        "state": _var_var_27,
        "is_demo": bool(_var_var_13.get("is_demo")),
        "provider_version": _number(_var_var_13.get("provider_version")) or 1,
        "license": {
            "status": _text(_var_var_28.get("status")) or "not_provided",
            "source": _text(_var_var_28.get("source")),
            "id_masked": _text(_var_var_28.get("id_masked")),
            "expires_at": _text(_var_var_28.get("expires_at")),
        },
        "tariff": {
            "mode": _text(_var_var_29.get("mode")) or "dynamic",
            "label": _text(_var_var_29.get("label")),
            "country": _text(_var_var_29.get("country")) or "DE",
            "has_spot_component": bool(_var_var_29.get("has_spot_component")),
            "feed_in_tariff_ct": _number(_var_var_29.get("feed_in_tariff_ct")),
            "base_fee_eur_month": _number(_var_var_29.get("base_fee_eur_month")),
        },
        "prices": {
            "current_total": _number(_var_var_30.get("current_total")),
            "next_total": _number(_var_var_30.get("next_total")),
            "average_today": _number(_var_var_30.get("average_today")),
            "cheapest_hour": _number(_var_var_30.get("cheapest_hour")),
            "most_expensive_hour": _number(_var_var_30.get("most_expensive_hour")),
            "today": _price_points(_var_var_30.get("today"), threshold_ct=_var_var_33),
            "tomorrow": _price_points(
                _var_var_30.get("tomorrow"), threshold_ct=_var_var_33
            ),
        },
        "threshold_ct": _var_var_33,
        "thresholds": _thresholds(_var_var_13.get("thresholds")),
        "is_cheap": bool(_var_var_13.get("is_cheap")),
        "is_force_price": bool(_var_var_13.get("is_force_price")),
        "next_cheap": _next_cheap(_var_var_13.get("next_cheap")),
        "cheap_hours": {
            "today": _int_list(_var_var_38.get("today")),
            "tomorrow": _int_list(_var_var_38.get("tomorrow")),
        },
        "tomorrow_available": bool(
            _var_var_13.get("tomorrow_available", _var_var_30.get("tomorrow"))
        ),
        "price_components": _var_var_34,
        "composition_ok": _var_var_36,
        "corrections": _var_var_40,
        "months": _months(_var_var_13.get("months")),
        "effective_fees": _effective_fees(_var_var_13.get("effective_fees")),
        "tariff_schedule": _tariff_schedule(_var_var_13.get("tariff_schedule")),
        "diagnostics": {
            "last_fetch": _text(_var_var_37.get("last_fetch")),
            "cache_age_seconds": _number(_var_var_37.get("cache_age_seconds")),
            "price_revision": int(
                _number(_var_var_37.get("price_revision"))
                or _var_var_13.get("revision")
                or 0
            ),
            "last_correction": _var_var_35,
            "provider_version": _number(_var_var_37.get("provider_version"))
            or _number(_var_var_13.get("provider_version"))
            or 1,
            "composition_mismatch": bool(
                _var_var_37.get("composition_mismatch", not _var_var_36)
            ),
        },
        "revision": int(_number(_var_var_13.get("revision")) or 0),
        "last_correction": _var_var_35,
        "capabilities": {
            "tariff_models": bool(_var_var_31.get("tariff_models")),
            "csv_import": bool(_var_var_31.get("csv_import")),
            "corrections": bool(_var_var_31.get("corrections")),
        },
        "links": {
            "configure": _text(_var_var_32.get("configure")) or _CONFIGURE_LINK,
            "smart_charging": _text(_var_var_32.get("smart_charging"))
            or _SMART_CHARGING_LINK,
        },
        "updated_at": _text(_var_var_13.get("updated_at")),
    }
    if not _var_var_41["tariff"]["has_spot_component"]:
        _var_var_41["composition_ok"] = False
        _var_var_41["diagnostics"]["composition_mismatch"] = bool(
            _var_var_37.get("composition_mismatch", False)
        )
    assert set(_var_var_41) == set(_TOP_KEYS)
    assert set(_var_var_41["license"]) == set(_LICENSE_KEYS)
    assert set(_var_var_41["tariff"]) == set(_TARIFF_KEYS)
    assert set(_var_var_41["prices"]) == set(_PRICE_KEYS)
    assert set(_var_var_41["capabilities"]) == set(_CAPABILITY_KEYS)
    assert set(_var_var_41["links"]) == set(_LINK_KEYS)
    assert set(_var_var_41["thresholds"]) == set(_THRESHOLD_KEYS)
    assert set(_var_var_41["cheap_hours"]) == set(_CHEAP_HOURS_KEYS)
    assert set(_var_var_41["effective_fees"]) == set(_FEE_KEYS)
    assert set(_var_var_41["diagnostics"]) == set(_DIAGNOSTIC_KEYS)
    assert _var_var_41["last_correction"] is None or set(
        _var_var_41["last_correction"]
    ) == set(_CORRECTION_KEYS)
    assert _var_var_41["next_cheap"] is None or set(_var_var_41["next_cheap"]) == set(
        _NEXT_CHEAP_KEYS
    )
    assert _var_var_41["price_components"] is None or set(
        _var_var_41["price_components"]
    ) == set(_COMPONENT_KEYS)
    assert _var_var_41["tariff_schedule"] is None or set(
        _var_var_41["tariff_schedule"]
    ) == set(_SCHEDULE_KEYS)
    for _var_var_42 in (
        _var_var_41["prices"]["today"] + _var_var_41["prices"]["tomorrow"]
    ):
        if set(_var_var_42) == set(_POINT_KEYS):
            continue
        assert False
    for _var_var_4 in _var_var_41["corrections"]:
        if set(_var_var_4) == set(_CORRECTION_ROW_KEYS):
            continue
        assert False
    for _var_var_4 in _var_var_41["months"]:
        if set(_var_var_4) == set(_MONTH_KEYS):
            continue
        assert False
    return _var_var_41


async def _read_csv_upload(request: web.Request) -> tuple[bytes, dict[str, str]]:
    if csv_too_large_from_length(request.content_length):
        raise _CsvLimitError("csv_too_large")
    try:
        _var_var_105 = await request.multipart()
    except Exception as _var_var_101:
        raise _CsvLimitError("invalid_payload") from _var_var_101
    _var_var_106 = {}
    _var_var_107 = None
    while True:
        _var_var_108 = await _var_var_105.next()
        if _var_var_108 is None:
            break
        _var_var_44 = getattr(_var_var_108, "name", None)
        if not _var_var_44:
            continue
        if _var_var_44 == "file":
            _var_var_107 = await _read_limited_part(_var_var_108, CSV_MAX_BYTES)
            continue
        _var_var_13 = await _read_limited_part(_var_var_108, 65536)
        _var_var_106[str(_var_var_44)] = _var_var_13.decode("utf-8", errors="replace")
    if not _var_var_107:
        raise _CsvLimitError("invalid_payload")
    if len(_var_var_107) > CSV_MAX_BYTES:
        raise _CsvLimitError("csv_too_large")
    return (_var_var_107, _var_var_106)
