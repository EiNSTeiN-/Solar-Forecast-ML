# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Shared constants, access helpers, and multi-group API helpers."""
from __future__ import annotations
import functools
import hashlib
import ipaddress
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
import aiosqlite
from aiohttp import web
from hass_nabucasa import remote as nabucasa_remote
from homeassistant.components.http import KEY_AUTHENTICATED
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from ..const import (
    CONF_BILLING_FIXED_PRICE,
    CONF_BILLING_GRID_FEES,
    CONF_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_BILLING_WORK_PRICE,
    DOMAIN,
    SOLAR_FORECAST_DB,
)
from ..core.power_integration import live_power_state_w
from ..core.corrections import EnergyCorrectionStore
from ..readers.solar_reader import SolarDataReader
from ..utils.time_utils import (
    ha_now,
    ha_timezone,
    local_date_range_strings,
    local_dates_range_strings,
    local_hour_key,
)
from . import context

_LOGGER = logging.getLogger(__name__)
PRODUCTION_CANDIDATE_KWH_THRESHOLD = 0.01
VISIBLE_WEATHER_MISS_ALERT_TYPES = frozenset(
    {"unexpected_rain", "unexpected_snow", "unexpected_clouds"}
)
HARD_WEATHER_EXCLUSION_ALERT_TYPES = frozenset({"snow_covered_panels", "manual_pause"})
GPM_PRICE_FRESHNESS_MINUTES = 30
LOCAL_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
]
FRONTEND_RELEASE_SCHEMA_VERSION = 1
FRONTEND_RELEASE_ID_PATTERN = re.compile("^[0-9a-f]{64}$")
FRONTEND_RELEASE_ROOT = ".sfml_stats_frontend"
FRONTEND_RELEASE_MANIFEST = "asset-manifest.json"
FRONTEND_NO_STORE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}
FRONTEND_IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


def _safe_frontend_relative_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    relative_path = PurePosixPath(value)
    if (
        relative_path.is_absolute()
        or str(relative_path) != value
        or any(
            (
                path_component in {"", ".", ".."}
                for path_component in relative_path.parts
            )
        )
    ):
        return None
    return relative_path


def _frontend_path_without_symlinks(base: Path, relative: str) -> Path | None:
    validated_relative = _safe_frontend_relative_path(relative)
    if validated_relative is None or base.is_symlink() or (not base.is_dir()):
        return None
    candidate_path = base.joinpath(*validated_relative.parts)
    current_path = base
    for path_component in validated_relative.parts:
        current_path = current_path / path_component
        if not current_path.is_symlink():
            continue
        return
    try:
        resolved_base = base.resolve(strict=True)
        resolved_file = candidate_path.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if not (resolved_file.is_relative_to(resolved_base) and resolved_file.is_file()):
        return None
    return resolved_file


def _valid_frontend_manifest_record(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "size", "sha256"}
        and (_safe_frontend_relative_path(value.get("path")) is not None)
        and isinstance(value.get("sha256"), str)
        and (FRONTEND_RELEASE_ID_PATTERN.fullmatch(value["sha256"]) is not None)
        and isinstance(value.get("size"), int)
        and (not isinstance(value["size"], bool))
        and (value["size"] >= 0)
    )


def _frontend_file_size_matches(path: Path, expected_size: int) -> bool:
    try:
        return path.stat().st_size == expected_size
    except OSError:
        return False


def _expected_frontend_asset_path(logical_path: str, sha256: str) -> str | None:
    logical_relative = _safe_frontend_relative_path(logical_path)
    if logical_relative is None or not logical_relative.suffix:
        return None
    hashed_asset_name = f"{logical_relative.stem}.{sha256}{logical_relative.suffix}"
    return str(PurePosixPath("assets") / logical_relative.parent / hashed_asset_name)


def _load_active_frontend_release(config_root: Path) -> dict[str, Any] | None:
    frontend_root = config_root / FRONTEND_RELEASE_ROOT
    releases_directory = frontend_root / "releases"
    current_link = frontend_root / "current"
    if (
        frontend_root.is_symlink()
        or releases_directory.is_symlink()
        or (not releases_directory.is_dir())
        or (not current_link.is_symlink())
    ):
        return
    try:
        resolved_releases_directory = releases_directory.resolve(strict=True)
        active_release_directory = current_link.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if (
        active_release_directory.parent != resolved_releases_directory
        or FRONTEND_RELEASE_ID_PATTERN.fullmatch(active_release_directory.name) is None
        or (not active_release_directory.is_dir())
    ):
        return None
    manifest_path = _frontend_path_without_symlinks(
        active_release_directory, FRONTEND_RELEASE_MANIFEST
    )
    if manifest_path is None:
        return
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        canonical_manifest_bytes = (
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode()
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return
    if (
        canonical_manifest_bytes != manifest_bytes
        or hashlib.sha256(manifest_bytes).hexdigest() != active_release_directory.name
        or (not isinstance(manifest, dict))
        or (set(manifest) != {"assets", "entries", "schema_version"})
        or (manifest.get("schema_version") != FRONTEND_RELEASE_SCHEMA_VERSION)
        or (not isinstance(manifest.get("assets"), dict))
        or (not isinstance(manifest.get("entries"), dict))
    ):
        return None
    for logical_path, manifest_record in manifest["assets"].items():
        if not _valid_frontend_manifest_record(manifest_record) or manifest_record[
            "path"
        ] != _expected_frontend_asset_path(logical_path, manifest_record["sha256"]):
            return
        asset_relative_path = PurePosixPath(manifest_record["path"])
        asset_file = _frontend_path_without_symlinks(
            frontend_root / "assets", str(PurePosixPath(*asset_relative_path.parts[1:]))
        )
        if asset_file is not None:
            if _frontend_file_size_matches(asset_file, manifest_record["size"]):
                continue
        return
    for logical_path, manifest_record in manifest["entries"].items():
        if (
            not _valid_frontend_manifest_record(manifest_record)
            or _safe_frontend_relative_path(logical_path) is None
            or manifest_record["path"] != logical_path
            or (PurePosixPath(logical_path).suffix.lower() != ".html")
        ):
            return
        entry_file = _frontend_path_without_symlinks(
            active_release_directory, manifest_record["path"]
        )
        if entry_file is not None:
            if _frontend_file_size_matches(entry_file, manifest_record["size"]):
                continue
        return
    return {
        "root": frontend_root,
        "release_dir": active_release_directory,
        "manifest": manifest,
        "manifest_bytes": manifest_bytes,
        "release_id": active_release_directory.name,
    }


def _active_frontend_file(
    release: dict[str, Any], group: str, logical_path: str
) -> tuple[Path, dict[str, Any]] | None:
    manifest_group = release["manifest"].get(group)
    if not isinstance(manifest_group, dict):
        return
    manifest_record = manifest_group.get(logical_path)
    if not _valid_frontend_manifest_record(manifest_record):
        return
    if group == "assets":
        base_directory = release["root"] / "assets"
        asset_relative_path = PurePosixPath(manifest_record["path"])
        if not asset_relative_path.parts or asset_relative_path.parts[0] != "assets":
            return None
        relative_name = str(PurePosixPath(*asset_relative_path.parts[1:]))
    elif group == "entries":
        base_directory = release["release_dir"]
        relative_name = manifest_record["path"]
    else:
        return
    file_path = _frontend_path_without_symlinks(base_directory, relative_name)
    if file_path is None:
        return
    return (file_path, manifest_record)


def _active_frontend_asset_by_path(
    release: dict[str, Any], asset_path: str
) -> tuple[Path, dict[str, Any]] | None:
    for logical_path, manifest_record in release["manifest"]["assets"].items():
        if not manifest_record["path"] == asset_path:
            continue
        return _active_frontend_file(release, "assets", logical_path)


def _verified_frontend_payload(
    path: Path, record: dict[str, Any], payload: bytes
) -> bool:
    return (
        len(payload) == record["size"]
        and hashlib.sha256(payload).hexdigest() == record["sha256"]
        and (not path.is_symlink())
    )


def _panel_group_exclusion_reason(row: Any) -> str | None:
    if bool(_attr_or_item(row, "mppt_throttled", False)):
        return "mppt_throttled"
    if bool(_attr_or_item(row, "inverter_clipped", False)):
        return "inverter_clipped"
    if bool(_attr_or_item(row, "exclude_from_learning_group", False)):
        return (
            _attr_or_item(row, "exclusion_reason_group")
            or "group_excluded_from_learning"
        )
    topology_compatible = _attr_or_item(row, "topology_compatible", True)
    if topology_compatible is not None and (not bool(topology_compatible)):
        return (
            _attr_or_item(row, "topology_exclusion_reason") or "topology_incompatible"
        )
    if bool(_attr_or_item(row, "snow_covered_group", False)):
        return "snow_covered_group"


def _clock_time(value: Any) -> str | None:
    if not value:
        return
    try:
        if isinstance(value, datetime):
            return dt_util.as_local(value).strftime("%H:%M")
        timestamp_text = str(value)
        parsed_datetime = dt_util.parse_datetime(timestamp_text)
        if parsed_datetime is not None:
            return dt_util.as_local(parsed_datetime).strftime("%H:%M")
        if "T" in timestamp_text:
            return timestamp_text.split("T", 1)[1][:5]
        if " " in timestamp_text:
            return timestamp_text.split(" ", 1)[1][:5]
        return timestamp_text[:5]
    except Exception:
        return


def _get_client_ip(request: web.Request) -> str:
    """Use only the client address validated by Home Assistant middleware."""
    if request.remote:
        return request.remote
    peer_address = (
        request.transport.get_extra_info("peername")
        if request.transport is not None
        else None
    )
    if peer_address:
        return peer_address[0]
    return "unknown"


def _is_trusted_cloud_request(request: web.Request | None = None) -> bool:
    """Return whether this is an authenticated Nabu Casa Remote UI request."""
    if not nabucasa_remote.is_cloud_request.get():
        return False
    if request is None:
        return True
    return bool(request.get(KEY_AUTHENTICATED, False))


def _is_local_ip(ip_str: str) -> bool:
    """Check if IP is in local network range. @zara"""
    try:
        client_address = ipaddress.ip_address(ip_str)
        return any((client_address in network for network in LOCAL_NETWORKS))
    except ValueError:
        return False


def local_only(func):
    """Decorator: Block external (non-local) requests. @zara"""

    @functools.wraps(func)
    async def wrapper(self, request: web.Request, *args, **kwargs):
        if nabucasa_remote.is_cloud_request.get():
            if not _is_trusted_cloud_request(request):
                return web.json_response(
                    {"success": False, "error": {"code": "authentication_required"}},
                    status=401,
                    headers={"Cache-Control": "no-store"},
                )
            if getattr(request, "method", "GET").upper() in {"GET", "HEAD"}:
                return await func(self, request, *args, **kwargs)
            logging.getLogger(__name__).warning(
                "Blocked Remote UI write access to %s", request.path
            )
            return web.json_response(
                {"success": False, "error": {"code": "local_write_required"}},
                status=403,
            )
        client_ip = _get_client_ip(request)
        if not _is_local_ip(client_ip):
            logging.getLogger(__name__).warning(
                "Blocked external access from %s to %s", client_ip, request.path
            )
            return web.Response(
                text="<!DOCTYPE html><html><head><title>Access Denied</title></head><body style='font-family:sans-serif;text-align:center;padding:50px;'><h1>403 - Access Denied</h1><p>SFML Stats is only accessible from the local network.</p></body></html>",
                status=403,
                content_type="text/html",
            )
        return await func(self, request, *args, **kwargs)

    wrapper.__sfml_guard__ = "local_only"
    return wrapper


def local_or_cloud_shell_only(func):
    """Allow local clients or the Nabu Casa transport to load public UI assets."""

    @functools.wraps(func)
    async def wrapper(self, request: web.Request, *args, **kwargs):
        if nabucasa_remote.is_cloud_request.get():
            if getattr(request, "method", "GET").upper() in {"GET", "HEAD"}:
                return await func(self, request, *args, **kwargs)
            return web.json_response(
                {"success": False, "error": {"code": "read_only_transport"}},
                status=403,
                headers={"Cache-Control": "no-store"},
            )
        if not _is_local_ip(_get_client_ip(request)):
            return web.json_response(
                {"success": False, "error": {"code": "local_access_required"}},
                status=403,
                headers={"Cache-Control": "no-store"},
            )
        return await func(self, request, *args, **kwargs)

    wrapper.__sfml_guard__ = "local_or_cloud_shell_only"
    return wrapper


def strict_local_only(func):
    """Block non-local requests without the Remote UI exception."""

    @functools.wraps(func)
    async def wrapper(self, request: web.Request, *args, **kwargs):
        if _is_trusted_cloud_request() or not _is_local_ip(_get_client_ip(request)):
            return web.json_response(
                {"success": False, "error": {"code": "local_access_required"}},
                status=403,
            )
        return await func(self, request, *args, **kwargs)

    wrapper.__sfml_guard__ = "strict_local_only"
    return wrapper


def local_or_trusted_cloud_only(func):
    """Allow local requests and Home Assistant authenticated Remote UI requests."""

    @functools.wraps(func)
    async def wrapper(self, request: web.Request, *args, **kwargs):
        if nabucasa_remote.is_cloud_request.get():
            if _is_trusted_cloud_request(request):
                return await func(self, request, *args, **kwargs)
            return web.json_response(
                {"success": False, "error": {"code": "authentication_required"}},
                status=401,
                headers={"Cache-Control": "no-store"},
            )
        if not _is_local_ip(_get_client_ip(request)):
            return web.json_response(
                {
                    "success": False,
                    "error": {
                        "code": "local_access_required",
                        "message": "Use the local Home Assistant UI or Home Assistant Cloud.",
                    },
                },
                status=403,
                headers={"Cache-Control": "no-store"},
            )
        return await func(self, request, *args, **kwargs)

    wrapper.__sfml_guard__ = "local_or_trusted_cloud_only"
    return wrapper


def _normalized_alert_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_visible_weather_miss(alert_type: Any) -> bool:
    return _normalized_alert_type(alert_type) in VISIBLE_WEATHER_MISS_ALERT_TYPES


def _stable_api_error(
    code: str,
    message: str,
    *,
    status: str = "error",
    data_available: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_payload = {
        "success": False,
        "status": status,
        "data_available": data_available,
        "error": {"code": code, "message": message},
    }
    if details:
        error_payload["error"]["details"] = details
    return error_payload


def _time_context() -> dict[str, Any]:
    local_now = ha_now(context.HASS)
    today = local_now.date()
    return {
        "timezone": getattr(getattr(context.HASS, "config", None), "time_zone", None)
        or str(ha_timezone(context.HASS)),
        "now": local_now.isoformat(),
        "today": today.isoformat(),
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "current_hour": local_now.hour,
        "current_minute": local_now.minute,
        "current_month": local_now.month,
        "current_year": local_now.year,
    }


def _stats_visible_exclusion_reason(prediction: Any) -> str | None:
    get_field = (
        prediction.get if isinstance(prediction, dict) else prediction.__getitem__
    )
    alert_type = _normalized_alert_type(get_field("weather_alert_type"))
    throttle_reason = get_field("mppt_throttle_reason")
    if bool(get_field("mppt_throttled")):
        return "mppt_throttled"
    if bool(get_field("inverter_clipped")):
        return "inverter_clipped"
    if bool(get_field("exclude_from_learning")):
        return throttle_reason or alert_type or "excluded_from_learning"
    if alert_type in HARD_WEATHER_EXCLUSION_ALERT_TYPES:
        return alert_type
    if bool(get_field("exclude_from_clean_evaluation")):
        if _is_visible_weather_miss(alert_type):
            return
        return throttle_reason or alert_type or "excluded_from_clean_evaluation"


def _attr_or_item(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    try:
        return value[key]
    except (KeyError, TypeError, IndexError):
        return getattr(value, key, default)


def _build_forecast_error_metrics(
    predicted_kwh: float | None, actual_kwh: float | None
) -> dict[str, float | None]:
    """Build explicit forecast error metrics with unambiguous denominators. @zara"""
    forecast_value = float(predicted_kwh or 0.0)
    actual_value = float(actual_kwh or 0.0)
    delta_vs_forecast_percent = (
        (actual_value - forecast_value) / forecast_value * 100.0
        if forecast_value > 0.01
        else None
    )
    accuracy_vs_forecast_percent = (
        max(0.0, 100.0 - abs(delta_vs_forecast_percent))
        if delta_vs_forecast_percent is not None
        else None
    )
    error_vs_actual_percent = (
        (forecast_value - actual_value) / actual_value * 100.0
        if actual_value > 0.01
        else None
    )
    accuracy_vs_actual_percent = (
        max(0.0, 100.0 - abs(error_vs_actual_percent))
        if error_vs_actual_percent is not None
        else None
    )
    return (
        (
            (
                {
                    "yield_delta_vs_forecast_percent": (
                        round(delta_vs_forecast_percent, 1)
                        if delta_vs_forecast_percent is not None
                        else None
                    ),
                    "forecast_error_vs_sot_percent": (
                        round(delta_vs_forecast_percent, 1)
                        if delta_vs_forecast_percent is not None
                        else None
                    ),
                    "forecast_accuracy_vs_sot_percent": (
                        round(accuracy_vs_forecast_percent, 1)
                        if accuracy_vs_forecast_percent is not None
                        else None
                    ),
                    "forecast_error_vs_actual_percent": (
                        round(error_vs_actual_percent, 1)
                        if error_vs_actual_percent is not None
                        else None
                    ),
                    "forecast_accuracy_vs_actual_percent": (
                        round(accuracy_vs_actual_percent, 1)
                        if accuracy_vs_actual_percent is not None
                        else None
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


def _build_live_today_clean_eval(
    rows: list[aiosqlite.Row], *, evaluation_cutoff_hour: int | None = None
) -> dict[str, Any]:
    """Build today's clean-evaluation metrics from hourly rows. @zara"""
    prediction_rows = [dict(row) for row in rows]
    total_forecast_kwh = sum(
        (
            prediction_row.get("prediction_kwh") or 0.0
            for prediction_row in prediction_rows
        )
    )
    total_actual_kwh = sum(
        (
            prediction_row.get("actual_kwh") or 0.0
            for prediction_row in prediction_rows
            if prediction_row.get("actual_kwh") is not None
        )
    )
    clean_rows = []
    candidate_hours = 0
    completed_candidate_hours = 0
    pending_candidate_hours = 0
    missing_actual_hours = 0
    excluded_hours = 0
    excluded_mppt_hours = 0
    excluded_weather_hours = 0
    excluded_clipped_hours = 0
    learning_hours = 0
    discarded_learning_hours = 0
    discarded_reason_counts = {}
    excluded_reason_counts = {}
    weather_alert_hours = 0
    weather_reason_counts = {}
    sfml_clean_excluded_hours = 0
    for prediction_row in prediction_rows:
        forecast_kwh = float(prediction_row.get("prediction_kwh") or 0.0)
        raw_actual = prediction_row.get("actual_kwh")
        actual_kwh = float(raw_actual) if raw_actual is not None else None
        target_hour = int(prediction_row.get("target_hour") or -1)
        production_candidate = forecast_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD or (
            actual_kwh is not None and actual_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
        )
        if not production_candidate:
            continue
        candidate_hours += 1
        if evaluation_cutoff_hour is not None:
            if target_hour > evaluation_cutoff_hour:
                pending_candidate_hours += 1
                continue
        completed_candidate_hours += 1
        if actual_kwh is None:
            missing_actual_hours += 1
            continue
        mppt_throttled = bool(prediction_row.get("mppt_throttled"))
        inverter_clipped = bool(prediction_row.get("inverter_clipped"))
        excluded_from_learning = bool(prediction_row.get("exclude_from_learning"))
        excluded_from_clean_evaluation = bool(
            prediction_row.get("exclude_from_clean_evaluation")
        )
        throttle_reason = prediction_row.get("mppt_throttle_reason")
        alert_type = _normalized_alert_type(prediction_row.get("weather_alert_type"))
        if bool(prediction_row.get("has_weather_alert")) or alert_type:
            weather_alert_hours += 1
            weather_reason = alert_type or "weather_alert"
            weather_reason_counts[weather_reason] = (
                weather_reason_counts.get(weather_reason, 0) + 1
            )
        learning_exclusion_reason = None
        if excluded_from_learning or mppt_throttled or inverter_clipped:
            if mppt_throttled:
                learning_exclusion_reason = "mppt_throttled"
            elif inverter_clipped:
                learning_exclusion_reason = "inverter_clipped"
            elif throttle_reason:
                learning_exclusion_reason = throttle_reason
            elif prediction_row.get("weather_alert_type"):
                learning_exclusion_reason = prediction_row.get("weather_alert_type")
            elif bool(prediction_row.get("has_weather_alert")):
                learning_exclusion_reason = "weather_alert"
            else:
                learning_exclusion_reason = "excluded"
        if learning_exclusion_reason:
            discarded_learning_hours += 1
            discarded_reason_counts[learning_exclusion_reason] = (
                discarded_reason_counts.get(learning_exclusion_reason, 0) + 1
            )
        else:
            learning_hours += 1
        if excluded_from_clean_evaluation:
            sfml_clean_excluded_hours += 1
        stats_exclusion_reason = _stats_visible_exclusion_reason(prediction_row)
        if stats_exclusion_reason:
            excluded_hours += 1
            if stats_exclusion_reason == "mppt_throttled":
                excluded_mppt_hours += 1
            if stats_exclusion_reason == "inverter_clipped":
                excluded_clipped_hours += 1
            if stats_exclusion_reason not in {"mppt_throttled", "inverter_clipped"}:
                if bool(prediction_row.get("has_weather_alert")) or alert_type:
                    excluded_weather_hours += 1
            excluded_reason_counts[stats_exclusion_reason] = (
                excluded_reason_counts.get(stats_exclusion_reason, 0) + 1
            )
            continue
        clean_rows.append(prediction_row)
        continue
    clean_forecast_kwh = sum(
        (prediction_row.get("prediction_kwh") or 0.0 for prediction_row in clean_rows)
    )
    clean_actual_kwh = sum(
        (prediction_row.get("actual_kwh") or 0.0 for prediction_row in clean_rows)
    )
    if clean_actual_kwh > 0.01 and clean_forecast_kwh > 0:
        absolute_error_kwh = abs(clean_forecast_kwh - clean_actual_kwh)
        clean_accuracy = max(
            0.0, (1.0 - absolute_error_kwh / clean_forecast_kwh) * 100.0
        )
    else:
        clean_accuracy = 0.0
    clean_error_metrics = _build_forecast_error_metrics(
        clean_forecast_kwh, clean_actual_kwh
    )
    live_day_error_metrics = _build_forecast_error_metrics(
        total_forecast_kwh, total_actual_kwh
    )
    evaluation_hours = len(clean_rows)
    evaluation_coverage_percent = (
        evaluation_hours / candidate_hours * 100.0 if candidate_hours > 0 else 0.0
    )
    peak_prediction = max(
        prediction_rows, key=lambda p: p.get("prediction_kwh") or 0.0, default=None
    )
    return (
        (
            {
                "production": round(total_actual_kwh, 2),
                "forecast": round(total_forecast_kwh, 2),
                "accuracy": round(clean_accuracy, 1),
                **clean_error_metrics,
                "evaluation_coverage_percent": round(evaluation_coverage_percent, 1),
                "excluded_mppt_hours_count": excluded_mppt_hours,
                "excluded_hours_count": excluded_hours,
                "evaluation_hours_count": evaluation_hours,
                "production_candidate_hours_count": candidate_hours,
                "completed_candidate_hours_count": completed_candidate_hours,
                "pending_candidate_hours_count": pending_candidate_hours,
                "missing_actual_hours_count": missing_actual_hours,
                "learning_candidate_hours_count": candidate_hours,
                "learning_hours_count": learning_hours,
                "discarded_learning_hours_count": discarded_learning_hours,
                "discarded_learning_reason_breakdown": discarded_reason_counts,
                "excluded_weather_alert_hours_count": excluded_weather_hours,
                "excluded_inverter_clipped_hours_count": excluded_clipped_hours,
                "weather_alert_hours_count": weather_alert_hours,
                "weather_alert_reason_breakdown": weather_reason_counts,
                "sfml_clean_excluded_hours_count": sfml_clean_excluded_hours,
                "excluded_reason_breakdown_json": (
                    json.dumps(excluded_reason_counts, sort_keys=True)
                    if excluded_reason_counts
                    else None
                ),
                "evaluation_actual_kwh": round(clean_actual_kwh, 2),
                "evaluation_predicted_kwh": round(clean_forecast_kwh, 2),
                "evaluation_cutoff_hour": evaluation_cutoff_hour,
                "evaluation_scope": (
                    "completed_local_hours"
                    if evaluation_cutoff_hour is not None
                    else "complete_day"
                ),
                "live_day_yield_delta_vs_forecast_percent": live_day_error_metrics[
                    "yield_delta_vs_forecast_percent"
                ],
                "live_day_forecast_error_vs_sot_percent": live_day_error_metrics[
                    "forecast_error_vs_sot_percent"
                ],
                "live_day_forecast_accuracy_vs_sot_percent": live_day_error_metrics[
                    "forecast_accuracy_vs_sot_percent"
                ],
                "peak_hour": (
                    peak_prediction.get("target_hour") if peak_prediction else None
                ),
                "peak_kwh": (
                    round(peak_prediction.get("prediction_kwh") or 0.0, 3)
                    if peak_prediction
                    else None
                ),
            }
            if True
            else None
        )
        if True
        else None
    )


class APIContext:
    """Singleton context for API views. @zara"""

    _instance: "APIContext | None" = None

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the context. @zara"""
        self.hass = hass
        self.config_path = Path(hass.config.path())
        self.solar_path = self.config_path / "solar_forecast_ml"
        self.grid_path = self.config_path / "grid_price_monitor"

    @classmethod
    def get(cls) -> "APIContext":
        """Get the singleton instance. @zara"""
        if cls._instance is None:
            raise RuntimeError("APIContext not initialized - call initialize() first")
        return cls._instance

    @classmethod
    def initialize(cls, hass: HomeAssistant) -> "APIContext":
        """Initialize the singleton instance. @zara"""
        cls._instance = cls(hass)
        return cls._instance

    @classmethod
    def is_initialized(cls) -> bool:
        """Check if context is initialized. @zara"""
        return cls._instance is not None


async def _energy_correction_projection(
    start_date: str | None = None,
) -> dict[str, dict[str, float]]:
    """Load the effective correction projection without touching SFML raw data."""
    if context.HASS is None:
        return {}
    correction_store = context.HASS.data.get(DOMAIN, {}).get("correction_store")
    if not isinstance(correction_store, EnergyCorrectionStore):
        return {}
    return await correction_store.async_projection(start_date)


async def _corrected_daily_energy_values(
    start_date: str, end_date: str | None = None
) -> dict[str, dict[str, float]]:
    """Thin wrapper: report-facing daily energy comes from corrected_energy."""
    from ..core.corrected_energy import corrected_daily_report as corrected_daily_report

    correction_projection = await _energy_correction_projection(start_date)
    async with _get_db() as connection:
        return await corrected_daily_report(
            connection, start_date, end_date, correction_projection
        )
    return None if True else None


@asynccontextmanager
async def _get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Get DB connection via manager with direct fallback. @zara"""
    from ..storage.db_connection_manager import (
        get_manager as get_manager,
        DatabaseConnectionManager as DatabaseConnectionManager,
    )

    connection = None
    manager = get_manager()
    if manager is not None:
        try:
            if not manager.is_connected:
                await manager.connect()
            if manager.is_connected:
                connection = await manager.get_connection()
        except Exception as error:
            _LOGGER.warning(
                "Database connection manager auto-reconnect failed: %s", error
            )
    if connection is not None:
        yield connection
        return
    database_path = Path(context.HASS.config.path()) / SOLAR_FORECAST_DB
    async with DatabaseConnectionManager.connect_direct_safe(
        database_path, context.HASS
    ) as connection:
        yield connection
    if True:
        return


async def _table_columns(db: aiosqlite.Connection, table_name: str) -> set[str]:
    if not table_name.replace("_", "").isalnum():
        return set()
    try:
        async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
            table_info = await cursor.fetchall()
    except Exception:
        return set()
    columns = set()
    for row in table_info:
        if hasattr(row, "keys") and "name" in row.keys():
            columns.add(row["name"])
        elif len(row) > 1:
            columns.add(row[1])
    return columns


def _get_local_naive_range_for_local_date(local_date_str: str) -> tuple[str, str]:
    """Return the local-naive DB range for a Home Assistant local date. @zara"""
    return local_date_range_strings(local_date_str)


def _get_broad_timestamp_range_for_local_dates(
    local_dates: list[str],
) -> tuple[str, str]:
    """Return a broad timestamp range for mixed local-naive and UTC-aware DB rows. @zara"""
    range_start, range_end = local_dates_range_strings(local_dates)
    broad_start = datetime.fromisoformat(range_start) - timedelta(hours=14)
    broad_end = datetime.fromisoformat(range_end) + timedelta(hours=14)
    return (broad_start.isoformat(), broad_end.isoformat())


def _gpm_local_hour_key(timestamp_str: str) -> str | None:
    """Return the Home Assistant local hour key for a GPM timestamp. @zara"""
    return local_hour_key(timestamp_str, context.HASS)


def _get_solar_reader() -> SolarDataReader:
    """Get a SolarDataReader instance for database access. @zara"""
    if context.SOLAR_PATH is None:
        raise RuntimeError("SOLAR_PATH not initialized - was async_setup_views called?")
    return SolarDataReader(context.SOLAR_PATH.parent, hass=context.HASS)


def _get_config() -> dict[str, Any]:
    """Get current configuration from the first config entry. @zara"""
    if context.HASS is None:
        return {}
    domain_data = context.HASS.data.get(DOMAIN, {})
    for entry_id, entry_data in domain_data.items():
        if not isinstance(entry_data, dict):
            continue
        if not "config" in entry_data:
            continue
        return entry_data["config"]
    config_entries = context.HASS.config_entries.async_entries(DOMAIN)
    if config_entries:
        first_entry = config_entries[0]
        return dict(first_entry.data)
    return {}


def _get_sensor_value(entity_id: str | None) -> float | None:
    """Read current value from a sensor. @zara"""
    if not entity_id or not context.HASS:
        return None
    sensor_state = context.HASS.states.get(entity_id)
    if sensor_state is None or sensor_state.state in ("unknown", "unavailable"):
        return
    try:
        return float(sensor_state.state)
    except (ValueError, TypeError):
        return


def _get_power_w(entity_id: str | None, *, signed: bool = False) -> float | None:
    """Read a live power sensor in watts. Non-power units yield None."""
    if not entity_id or not context.HASS:
        return None
    return live_power_state_w(context.HASS.states.get(entity_id), signed=signed)


def _get_power_kw(entity_id: str | None) -> float | None:
    """Read a live power sensor in kilowatts for labels that already say kW."""
    watts = _get_power_w(entity_id)
    if watts is None:
        return
    return round(watts / 1000.0, 2)


def _get_fixed_total_price(config: dict) -> float:
    """Get total fixed price from config (legacy or work_price + grid_fees). @zara"""
    from ..const import CONF_LEGACY_FIXED_PRICE_CT as CONF_LEGACY_FIXED_PRICE_CT

    legacy_fixed_price = config.get(CONF_LEGACY_FIXED_PRICE_CT)
    if legacy_fixed_price not in (None, ""):
        try:
            return float(legacy_fixed_price)
        except (TypeError, ValueError):
            pass
    work_price = config.get(CONF_BILLING_WORK_PRICE)
    grid_fees = config.get(CONF_BILLING_GRID_FEES)
    if work_price is not None and grid_fees is not None:
        return work_price + grid_fees
    return config.get(
        CONF_BILLING_FIXED_PRICE, DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    )


def _column_expr(
    existing_columns: set[str],
    column: str,
    *,
    alias: str | None = None,
    table_alias: str | None = None,
    default: str = "NULL",
) -> str:
    output_name = alias or column
    if column in existing_columns:
        table_prefix = f"{table_alias}." if table_alias else ""
        return f"{table_prefix}{column} AS {output_name}"
    return f"{default} AS {output_name}"


def _get_stats_gpm_coordinator() -> Any | None:
    if context.HASS is None:
        return
    for entry_data in context.HASS.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        gpm_coordinator = entry_data.get("gpm_coordinator")
        if not gpm_coordinator is not None:
            continue
        return gpm_coordinator
