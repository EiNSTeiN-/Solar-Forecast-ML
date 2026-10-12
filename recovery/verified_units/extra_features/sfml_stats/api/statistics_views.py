# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Premium statistics over corrected energy. No second price formula."""
from __future__ import annotations
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.corrected_energy import (
    corrected_daily,
    projection_from_runtime,
    statement_energy_cost,
)
from ..core.corrections import METRIC_REGISTRY
from .common import local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
_PROVIDER_VERSION = 8
_MAX_PERIOD_DAYS = 8000
_DATASETS = ("day", "month", "year")
_PREMIUM_REQUIRED = "premium_required"
_UNLICENSED_STATUSES = {
    "expired",
    "not_provided",
    "not_yet_valid",
    "wrong_product",
    "invalid_format",
    "unknown_key_id",
    "validation_error",
    "invalid_signature",
    "missing_entitlement",
    "unsupported_version",
}


def license_snapshot(hass: Any) -> dict[str, Any] | None:
    """Return the EAI capability snapshot, or None when it cannot be read."""
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return
    eai_data = hass_data.get("solar_forecast_eai")
    if not isinstance(eai_data, dict):
        return
    provider = eai_data.get("capability_provider")
    snapshot_method = getattr(provider, "snapshot", None)
    if not callable(snapshot_method):
        return
    try:
        value = snapshot_method()
    except Exception:
        return
    return value if isinstance(value, dict) else None


def license_is_live(snapshot: dict[str, Any] | None) -> bool:
    """True only for provider v8 with a valid Premium Full Package."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("provider_version") != _PROVIDER_VERSION:
        return False
    license_status = snapshot.get("license_status")
    if license_status in _UNLICENSED_STATUSES or license_status != "valid":
        return False
    return snapshot.get("premium_full_package") is True


def license_denied() -> web.Response:
    """Stable 403. No demo rows."""
    return web.json_response(
        {
            "success": False,
            "error": {
                "code": _PREMIUM_REQUIRED,
                "message": "A valid Premium Full Package is required.",
            },
        },
        status=403,
    )


def license_gate(request: web.Request) -> web.Response | None:
    """Block the response unless the live licence check passes."""
    try:
        hass = request.app["hass"]
    except Exception:
        return license_denied()
    if license_is_live(license_snapshot(hass)):
        return
    return license_denied()


def _parse_day(value: str) -> datetime:
    parsed_date = datetime.strptime(value, "%Y-%m-%d")
    if parsed_date.strftime("%Y-%m-%d") != value:
        raise ValueError("invalid_period")
    return parsed_date


def parse_period(request: web.Request) -> tuple[str, str]:
    """Query parameters from/to, with start/end as aliases."""
    start_day = str(
        request.query.get("from") or request.query.get("start") or ""
    ).strip()
    end_day = str(request.query.get("to") or request.query.get("end") or "").strip()
    if not start_day or not end_day:
        from ..utils.time_utils import ha_today as ha_today
        from . import context as context

        today = ha_today(context.HASS)
        if not end_day:
            end_day = today.isoformat()
        if not start_day:
            start_day = (today - timedelta(days=30)).isoformat()
    start_date = _parse_day(start_day)
    end_date = _parse_day(end_day)
    if start_date > end_date or (end_date - start_date).days > _MAX_PERIOD_DAYS:
        raise ValueError("invalid_period")
    return (start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))


def _metric_ids() -> list[str]:
    return [str(metric["id"]) for metric in METRIC_REGISTRY]


def _metrics_for_day(
    day: str, values: dict[str, float], projection: dict[str, dict[str, float]]
) -> tuple[dict[str, float], list[str]]:
    projected_metrics = projection.get(day) or {}
    metrics = {}
    corrected_metrics = []
    for metric_definition in METRIC_REGISTRY:
        metric_id = str(metric_definition["id"])
        raw_column = f"raw_{metric_definition['daily_column']}"
        if metric_id in values:
            corrected_value = float(values[metric_id])
            raw_value = float(values.get(raw_column, corrected_value))
        else:
            corrected_value = 0.0
            raw_value = 0.0
        metrics[metric_id] = corrected_value
        if metric_id in projected_metrics or abs(corrected_value - raw_value) > 1e-09:
            corrected_metrics.append(metric_id)
    return (metrics, corrected_metrics)


def _rounded(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 3) for key, value in metrics.items()}


async def _row_cost(
    db: Any,
    start: str,
    end: str,
    projection: dict[str, dict[str, float]] | None,
    config: dict[str, Any] | None,
) -> float:
    statement = await statement_energy_cost(db, start, end, projection, config=config)
    return float(statement["energy_eur"])


async def build_statistics(
    db: Any,
    start: str,
    end: str,
    projection: dict[str, dict[str, float]] | None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Day, month and year datasets. Every figure comes from corrected_energy."""
    projection_values = projection or {}
    daily_values = await corrected_daily(db, start, end, projection_values)
    metric_ids = _metric_ids()
    daily_rows = []
    for day in sorted(daily_values):
        metrics, day_corrections = _metrics_for_day(
            day, daily_values[day], projection_values
        )
        daily_rows.append(
            {
                "period": day,
                "corrected": bool(day_corrections),
                "corrected_metrics": day_corrections,
                "cost_eur": await _row_cost(db, day, day, projection_values, config),
                "metrics": _rounded(metrics),
            }
        )
    datasets = {
        "day": daily_rows,
        "month": await _aggregate(db, daily_rows, "month", projection_values, config),
        "year": await _aggregate(db, daily_rows, "year", projection_values, config),
    }
    statement = await statement_energy_cost(
        db, start, end, projection_values, config=config
    )
    totals = {metric_key: 0.0 for metric_key in metric_ids}
    for row in daily_rows:
        for metric_key, value in row["metrics"].items():
            totals[metric_key] = round(totals[metric_key] + float(value), 3)
    return {
        "start": start,
        "end": end,
        "metrics": metric_ids,
        "datasets": datasets,
        "totals": {
            "cost_eur": statement["energy_eur"],
            "kwh": statement["kwh"],
            "metrics": totals,
            "corrected": any((row["corrected"] for row in daily_rows)),
        },
    }


async def _aggregate(
    db: Any,
    day_rows: list[dict[str, Any]],
    grain: str,
    projection: dict[str, dict[str, float]],
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows_by_period = {}
    for row in day_rows:
        period = row["period"][:7] if grain == "month" else row["period"][:4]
        rows_by_period.setdefault(period, []).append(row)
    period_rows = []
    for period in sorted(rows_by_period):
        bucket_rows = rows_by_period[period]
        metrics = {metric_key: 0.0 for metric_key in _metric_ids()}
        corrected_metrics = []
        for row in bucket_rows:
            for metric_key, value in row["metrics"].items():
                metrics[metric_key] = round(metrics[metric_key] + float(value), 3)
            for metric_key in row["corrected_metrics"]:
                if metric_key not in corrected_metrics:
                    corrected_metrics.append(metric_key)
        bucket_start = bucket_rows[0]["period"]
        bucket_end = bucket_rows[-1]["period"]
        period_rows.append(
            {
                "period": period,
                "corrected": bool(corrected_metrics),
                "corrected_metrics": corrected_metrics,
                "cost_eur": await _row_cost(
                    db, bucket_start, bucket_end, projection, config
                ),
                "metrics": metrics,
            }
        )
    return period_rows


async def payload_for_request(request: web.Request) -> dict[str, Any]:
    """Load one statistics payload for the requested period."""
    start_day, end_day = parse_period(request)
    from .common import _get_config as get_config, _get_db as get_db

    config = get_config()
    if not isinstance(config, dict):
        config = None
    async with get_db() as db:
        projection = await projection_from_runtime(start_day)
        return await build_statistics(db, start_day, end_day, projection, config=config)
    return None if True else None


def dataset_rows(
    payload: dict[str, Any], grain: str
) -> tuple[list[str], list[list[Any]]]:
    """One dataset as CSV columns and rows. grain is day, month or year."""
    if grain not in _DATASETS:
        raise ValueError("invalid_period")
    columns = ["period", "corrected", "cost_eur", *payload["metrics"]]
    rows = []
    for row in payload["datasets"][grain]:
        rows.append(
            [
                row["period"],
                1 if row["corrected"] else 0,
                row["cost_eur"],
                *[
                    row["metrics"].get(metric_key, 0.0)
                    for metric_key in payload["metrics"]
                ],
            ]
        )
    return (columns, rows)


def _csv_text(columns: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def statistics_export_map(payload: dict[str, Any]) -> dict[str, str]:
    """All three datasets plus metadata.json."""
    exports = {}
    for grain in _DATASETS:
        columns, rows = dataset_rows(payload, grain)
        exports[f"statistics_{grain}.csv"] = _csv_text(columns, rows)
    filenames = sorted(exports)
    exports["metadata.json"] = json.dumps(
        {
            "datasets": list(_DATASETS),
            "end": payload["end"],
            "files": filenames,
            "start": payload["start"],
        },
        indent=2,
        sort_keys=True,
    )
    return exports


class ModernStatisticsView(HomeAssistantView):
    """Corrected figures for a chosen period. Licence required, no demo data."""

    url = "/api/sfml_stats/modern/statistics"
    name = "api:sfml_stats:modern:statistics"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        denial = license_gate(request)
        if denial is not None:
            return denial
        try:
            payload = await payload_for_request(request)
        except ValueError:
            return web.json_response(
                {
                    "success": False,
                    "error": {
                        "code": "invalid_period",
                        "message": "The period is invalid.",
                    },
                },
                status=400,
            )
        except Exception as error:
            _LOGGER.error("Statistics request failed: %s", type(error).__name__)
            return web.json_response(
                {
                    "success": False,
                    "error": {
                        "code": "statistics_failed",
                        "message": "Statistics could not be loaded.",
                    },
                },
                status=500,
            )
        return web.json_response({"success": True, **payload})


async def async_setup_statistics_views(hass: HomeAssistant) -> None:
    """Register the modern statistics endpoint."""
    hass.http.register_view(ModernStatisticsView())
