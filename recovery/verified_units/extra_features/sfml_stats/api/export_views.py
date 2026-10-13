# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""AI status and CSV export views."""
from __future__ import annotations
import json
import logging
from datetime import timedelta
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _column_expr,
    _energy_correction_projection,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_stats_gpm_coordinator,
    _table_columns,
    local_only,
)
from ..const import (
    CONF_FEED_IN_TARIFF,
    DEFAULT_BILLING_GRID_FEES,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_FEED_IN_TARIFF,
)
from ..core.corrected_energy import (
    autarky_percent,
    corrected_daily,
    corrected_day_cost,
    corrected_value,
    kwh_unchanged,
    own_supply_kwh,
    scale_hour_money,
)
from ..utils.time_utils import ha_now, ha_today
from ..core.dev_mode import is_dev_active, json_dev_required
from .statistics_views import (
    dataset_rows,
    license_gate,
    payload_for_request,
    statistics_export_map,
)

_LOGGER = logging.getLogger(__name__)
_SOLAR_EXPORT_BLANK_COLUMNS = frozenset(
    {"ai_kwh", "tfs_kwh", "lstm_kwh", "ridge_kwh", "tfs_weight", "physics_kwh"}
)


class AIStatusView(HomeAssistantView):
    """Get basic AI/ML model status. @zara"""

    url = "/api/sfml_stats/ai_status"
    name = "api:sfml_stats:ai_status"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return AI model status as JSON. @zara"""
        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "HASS not initialized"}, status=500
                )
            async with _get_db() as connection:
                model_status = {}
                async with connection.execute(
                    "SELECT active_model, training_samples, accuracy, rmse, last_trained\n                         FROM ai_learned_weights_meta WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        model_status = {
                            "active_model": row["active_model"],
                            "training_samples": row["training_samples"],
                            "accuracy": round((row["accuracy"] or 0) * 100, 1),
                            "rmse": round(row["rmse"] or 0, 4),
                            "last_trained": row["last_trained"],
                        }
                grid_search_status = {}
                async with connection.execute(
                    "SELECT COUNT(*) as runs, MAX(accuracy) as best_acc FROM ai_grid_search_results"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        grid_search_status = {
                            "total_runs": row["runs"],
                            "best_accuracy": round((row["best_acc"] or 0) * 100, 1),
                        }
                shadow_learning_status = {}
                async with connection.execute(
                    "SELECT total_days_learned, patterns_detected, last_learning_date\n                         FROM shadow_pattern_config WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        shadow_learning_status = {
                            "days_learned": row["total_days_learned"],
                            "patterns_detected": row["patterns_detected"],
                            "last_date": row["last_learning_date"],
                        }
                drift_event_count = 0
                async with connection.execute(
                    "SELECT COUNT(*) as cnt FROM drift_events WHERE event_date >= ?",
                    ((ha_today(context.HASS) - timedelta(days=7)).isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        drift_event_count = row["cnt"]
                training_point_count = 0
                async with connection.execute(
                    "SELECT COUNT(*) as cnt FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        training_point_count = row["cnt"]
            return web.json_response(
                {
                    "success": True,
                    "data": {
                        "model": model_status,
                        "grid_search": grid_search_status,
                        "shadow_learning": shadow_learning_status,
                        "drift_events_7d": drift_event_count,
                        "training_data_points": training_point_count,
                    },
                }
            )
        except Exception as error:
            _LOGGER.error("Error getting AI status: %s", error)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )


def _export_days(request: web.Request) -> int:
    try:
        export_days = int(request.query.get("days", 365))
    except (TypeError, ValueError):
        export_days = 365
    return max(1, min(export_days, 730))


def _export_cutoff(request: web.Request) -> str:
    return (ha_today(context.HASS) - timedelta(days=_export_days(request))).isoformat()


def _csv_text(columns: list[str], rows: list[list[Any]]) -> str:
    import csv as csv
    import io as io

    text_buffer = io.StringIO()
    csv_writer = csv.writer(text_buffer, delimiter=";")
    csv_writer.writerow(columns)
    csv_writer.writerows(rows)
    return text_buffer.getvalue()


def _csv_response(
    *, filename_prefix: str, columns: list[str], rows: list[list[Any]]
) -> web.Response:
    filename = f"sfml_stats_{filename_prefix}_{ha_today(context.HASS).isoformat()}.csv"
    return web.Response(
        text=_csv_text(columns, rows),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _zip_response(
    files: dict[str, str], filename_prefix: str = "export_all"
) -> web.Response:
    import io as io
    import zipfile as zipfile

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(
        archive_buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for filename, file_text in files.items():
            archive.writestr(filename, file_text)
    filename = f"sfml_stats_{filename_prefix}_{ha_today(context.HASS).isoformat()}.zip"
    return web.Response(
        body=archive_buffer.getvalue(),
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ) as cursor:
        return await cursor.fetchone() is not None
    return None if True else None


class CSVExportView(HomeAssistantView):
    """Legacy export of stats_daily_energy as CSV download. @zara"""

    url = "/api/sfml_stats/export/csv"
    name = "api:sfml_stats:export:csv"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return the legacy daily energy CSV for compatibility. @zara"""
        license_response = license_gate(request)
        if license_response is not None:
            return license_response
        try:
            columns, rows = await _export_legacy_daily_rows(request)
            return _csv_response(filename_prefix="export", columns=columns, rows=rows)
        except Exception as error:
            _LOGGER.error("Error in CSV export: %s", error, exc_info=True)
            return web.json_response({"error": str(error)}, status=500)


class TypedCSVExportView(HomeAssistantView):
    """Export focused STATS datasets as CSV or ZIP. @zara"""

    url = "/api/sfml_stats/export/{export_type}"
    name = "api:sfml_stats:export:typed"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request, export_type: str) -> web.Response:
        license_response = license_gate(request)
        if license_response is not None:
            return license_response
        export_type = str(export_type or "").strip().lower()
        try:
            if export_type == "statistics":
                return await _statistics_export_response(request)
            if export_type == "all":
                export_files = {}
                for export_name, export_builder in EXPORT_BUILDERS.items():
                    if export_name == "statistics":
                        statistics_payload = await payload_for_request(request)
                        for grain in ("day", "month", "year"):
                            columns, rows = dataset_rows(statistics_payload, grain)
                            export_files[f"statistics_{grain}.csv"] = _csv_text(
                                columns, rows
                            )
                        continue
                    columns, rows = await export_builder(request)
                    export_files[f"{export_name}.csv"] = _csv_text(columns, rows)
                export_metadata = {
                    "generated_at": ha_now(context.HASS).isoformat(),
                    "days": _export_days(request),
                    "files": sorted(export_files),
                }
                export_files["metadata.json"] = json.dumps(
                    export_metadata, indent=2, sort_keys=True
                )
                return _zip_response(export_files)
            export_builder = EXPORT_BUILDERS.get(export_type)
            if export_builder is None:
                return web.json_response(
                    {
                        "error": "unknown export type",
                        "available": sorted([*EXPORT_BUILDERS.keys(), "all"]),
                    },
                    status=404,
                )
            columns, rows = await export_builder(request)
            return _csv_response(
                filename_prefix=export_type, columns=columns, rows=rows
            )
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
            _LOGGER.error("Error in %s export: %s", export_type, error, exc_info=True)
            return web.json_response({"error": str(error)}, status=500)


async def _statistics_export_response(request: web.Request) -> web.Response:
    """One dataset as CSV, or every dataset plus metadata.json as ZIP."""
    statistics_payload = await payload_for_request(request)
    grain = str(request.query.get("grain") or "zip").strip().lower()
    if grain in ("day", "month", "year"):
        columns, rows = dataset_rows(statistics_payload, grain)
        return _csv_response(
            filename_prefix=f"statistics_{grain}", columns=columns, rows=rows
        )
    return _zip_response(
        statistics_export_map(statistics_payload), filename_prefix="statistics"
    )


async def _export_statistics_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    """Day dataset. Month and year are written beside it in the ZIP."""
    statistics_payload = await payload_for_request(request)
    return dataset_rows(statistics_payload, "day")


async def _export_legacy_daily_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    cutoff = _export_cutoff(request)
    columns = [
        "date",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "self_consumption_kwh",
        "autarkie_percent",
        "peak_solar_w",
        "peak_solar_time",
        "total_cost_eur",
        "feed_in_revenue_eur",
        "savings_eur",
    ]
    rows = []
    projection = await _energy_correction_projection(cutoff)
    async with _get_db() as connection:
        available_columns = await _table_columns(connection, "stats_daily_energy")
        sql_column_expressions = [
            (
                _column_expr(available_columns, column, default="0")
                if column != "peak_solar_time"
                else _column_expr(available_columns, column)
            )
            for column in columns
        ]
        async with connection.execute(
            f"SELECT {', '.join(sql_column_expressions)}\n                FROM stats_daily_energy\n                WHERE date >= ?\n                ORDER BY date",
            (cutoff,),
        ) as cursor:
            for row in await cursor.fetchall():
                csv_values = [row[column_index] for column_index in range(len(columns))]
                raw_import_kwh = csv_values[3]
                raw_export_kwh = csv_values[4]
                raw_cost_eur = csv_values[9]
                raw_revenue_eur = csv_values[10]
                csv_values[1] = corrected_value(
                    projection, str(csv_values[0]), "solar_yield_day_kwh", csv_values[1]
                )
                csv_values[2] = corrected_value(
                    projection,
                    str(csv_values[0]),
                    "home_consumption_day_kwh",
                    csv_values[2],
                )
                csv_values[3] = corrected_value(
                    projection, str(csv_values[0]), "grid_import_day_kwh", csv_values[3]
                )
                csv_values[4] = corrected_value(
                    projection, str(csv_values[0]), "grid_export_day_kwh", csv_values[4]
                )
                day_projection = projection.get(str(csv_values[0]), {})
                if "grid_import_day_kwh" in day_projection:
                    csv_values[9] = corrected_day_cost(
                        raw_import_kwh,
                        csv_values[3],
                        raw_cost_eur,
                        fallback_price_ct=_export_import_price_ct(),
                        corrected=True,
                    )
                if "grid_export_day_kwh" in day_projection:
                    csv_values[10] = corrected_day_cost(
                        raw_export_kwh,
                        csv_values[4],
                        raw_revenue_eur,
                        fallback_price_ct=_export_feed_in_ct(),
                        corrected=True,
                    )
                rows.append(csv_values)
                continue
    if True:
        return (columns, rows)
    else:
        return (columns, rows)


async def _export_energy_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    cutoff = _export_cutoff(request)
    columns = [
        "date",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "smartmeter_import_kwh",
        "smartmeter_export_kwh",
        "self_consumption_kwh",
        "autarkie_percent",
        "solar_to_house_kwh",
        "solar_to_battery_kwh",
        "battery_to_house_kwh",
        "grid_to_house_kwh",
        "grid_to_battery_kwh",
        "battery_charge_solar_kwh",
        "battery_charge_grid_kwh",
        "consumer_heatpump_kwh",
        "consumer_heatingrod_kwh",
        "consumer_wallbox_kwh",
        "peak_solar_w",
        "peak_solar_time",
    ]
    rows = []
    projection = await _energy_correction_projection(cutoff)
    async with _get_db() as connection:
        available_columns = await _table_columns(connection, "stats_daily_energy")
        sql_column_expressions = [
            (
                _column_expr(available_columns, column, default="0")
                if column != "peak_solar_time"
                else _column_expr(available_columns, column)
            )
            for column in columns
        ]
        async with connection.execute(
            f"SELECT {', '.join(sql_column_expressions)}\n                FROM stats_daily_energy\n                WHERE date >= ?\n                ORDER BY date",
            (cutoff,),
        ) as cursor:
            for row in await cursor.fetchall():
                csv_values = [row[column] for column in columns]
                csv_values[1] = corrected_value(
                    projection, str(csv_values[0]), "solar_yield_day_kwh", csv_values[1]
                )
                csv_values[2] = corrected_value(
                    projection,
                    str(csv_values[0]),
                    "home_consumption_day_kwh",
                    csv_values[2],
                )
                csv_values[3] = corrected_value(
                    projection, str(csv_values[0]), "grid_import_day_kwh", csv_values[3]
                )
                csv_values[4] = corrected_value(
                    projection, str(csv_values[0]), "grid_export_day_kwh", csv_values[4]
                )
                day_projection = projection.get(str(csv_values[0]), {})
                if "grid_import_day_kwh" in day_projection:
                    csv_values[5] = csv_values[3]
                if "grid_export_day_kwh" in day_projection:
                    csv_values[6] = csv_values[4]
                _project_energy_export_flows(columns, csv_values, projection)
                rows.append(csv_values)
                continue
    if True:
        return (columns, rows)
    else:
        return (columns, rows)


async def _export_solar_rows(request: web.Request) -> tuple[list[str], list[list[Any]]]:
    cutoff = _export_cutoff(request)
    columns = [
        "target_date",
        "target_hour",
        "target_datetime",
        "prediction_kwh",
        "actual_kwh",
        "confidence",
        "prediction_method",
        "source",
        "ml_contribution_percent",
        "tfs_kwh",
        "tfs_weight",
        "ai_kwh",
        "physics_kwh",
        "lstm_kwh",
        "ridge_kwh",
        "temperature",
        "clouds",
        "solar_radiation",
        "clean_evaluation_eligible",
        "exclude_from_learning",
        "exclude_from_clean_evaluation",
        "mppt_throttled",
        "inverter_clipped",
        "has_weather_alert",
        "weather_alert_type",
        "created_at",
    ]
    rows = []
    async with _get_db() as connection:
        if not await _table_exists(connection, "hourly_predictions"):
            return (columns, rows)
        available_columns = await _table_columns(connection, "hourly_predictions")
        sql_column_expressions = [
            _column_expr(available_columns, column, table_alias="hp")
            for column in columns
        ]
        async with connection.execute(
            f"SELECT {', '.join(sql_column_expressions)}\n                FROM hourly_predictions hp\n                WHERE hp.target_date >= ?\n                ORDER BY hp.target_date, hp.target_hour",
            (cutoff,),
        ) as cursor:
            for row in await cursor.fetchall():
                csv_values = [row[column] for column in columns]
                for column_position, column in enumerate(columns):
                    if column in _SOLAR_EXPORT_BLANK_COLUMNS:
                        csv_values[column_position] = ""
                rows.append(csv_values)
    return (columns, rows) if True else None


async def _export_billing_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    cutoff = _export_cutoff(request)
    columns = [
        "date",
        "hour",
        "hour_key",
        "price_ct_kwh",
        "grid_import_kwh",
        "grid_import_cost_eur",
        "grid_export_kwh",
        "feed_in_tariff_ct",
        "feed_in_revenue_eur",
        "self_consumption_kwh",
        "self_consumption_savings_eur",
        "solar_to_house_kwh",
        "battery_to_house_kwh",
        "grid_to_battery_kwh",
        "grid_to_house_kwh",
        "grid_to_house_cost_eur",
        "grid_to_battery_cost_eur",
        "consumer_heatpump_kwh",
        "consumer_heatpump_cost_eur",
        "consumer_heatingrod_kwh",
        "consumer_heatingrod_cost_eur",
        "consumer_wallbox_kwh",
        "consumer_wallbox_cost_eur",
        "data_source",
    ]
    rows = []
    config = _get_config()
    try:
        fallback_price_ct = float(_get_fixed_total_price(config))
    except (TypeError, ValueError):
        fallback_price_ct = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    async with _get_db() as connection:
        if not await _table_exists(connection, "stats_hourly_billing"):
            return (columns, rows)
        available_columns = await _table_columns(connection, "stats_hourly_billing")
        price_expression = (
            "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
            if "price_ct_kwh" in available_columns
            else "?"
        )
        own_supply_expression = "COALESCE(solar_to_house_kwh, 0) + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))"
        from ..core.grid_split import (
            META_GRID_SPLIT_BACKFILL as META_GRID_SPLIT_BACKFILL,
            META_GRID_SPLIT_DONE as META_GRID_SPLIT_DONE,
            battery_export_cost_sql as battery_export_cost_sql,
            house_export_cost_sql as house_export_cost_sql,
        )

        backfill_done = False
        try:
            async with connection.execute(
                "SELECT value FROM stats_schema_meta WHERE key = ?",
                (META_GRID_SPLIT_BACKFILL,),
            ) as marker_cursor:
                marker_row = await marker_cursor.fetchone()
            if marker_row is not None:
                marker_value = (
                    marker_row["value"]
                    if hasattr(marker_row, "keys")
                    else marker_row[0]
                )
                backfill_done = marker_value == META_GRID_SPLIT_DONE
        except Exception:
            backfill_done = False
        battery_export_cost_expression = battery_export_cost_sql(
            price_expression,
            column_present="grid_to_battery_cost_ct" in available_columns,
            backfill_done=backfill_done,
        )
        house_export_cost_expression = house_export_cost_sql(
            price_expression,
            column_present="grid_to_house_cost_ct" in available_columns,
            backfill_done=backfill_done,
        )
        query = f"SELECT\n                    date,\n                    hour,\n                    hour_key,\n                    {price_expression} AS price_ct_kwh,\n                    COALESCE(grid_import_kwh, 0) AS grid_import_kwh,\n                    ROUND(COALESCE(grid_import_cost_ct, COALESCE(grid_import_kwh, 0) * {price_expression}) / 100.0, 4) AS grid_import_cost_eur,\n                    COALESCE(grid_export_kwh, 0) AS grid_export_kwh,\n                    COALESCE(feed_in_tariff_ct, 0) AS feed_in_tariff_ct,\n                    ROUND(COALESCE(feed_in_revenue_ct, 0) / 100.0, 4) AS feed_in_revenue_eur,\n                    ROUND({own_supply_expression}, 4) AS self_consumption_kwh,\n                    ROUND(({own_supply_expression}) * {price_expression} / 100.0, 4) AS self_consumption_savings_eur,\n                    COALESCE(solar_to_house_kwh, 0) AS solar_to_house_kwh,\n                    COALESCE(battery_to_house_kwh, 0) AS battery_to_house_kwh,\n                    COALESCE(grid_to_battery_kwh, 0) AS grid_to_battery_kwh,\n                    COALESCE(grid_to_house_kwh, 0) AS grid_to_house_kwh,\n                    ROUND(({house_export_cost_expression}) / 100.0, 4) AS grid_to_house_cost_eur,\n                    ROUND(({battery_export_cost_expression}) / 100.0, 4) AS grid_to_battery_cost_eur,\n                    COALESCE(consumer_heatpump_kwh, 0) AS consumer_heatpump_kwh,\n                    ROUND(COALESCE(consumer_heatpump_cost_ct, 0) / 100.0, 4) AS consumer_heatpump_cost_eur,\n                    COALESCE(consumer_heatingrod_kwh, 0) AS consumer_heatingrod_kwh,\n                    ROUND(COALESCE(consumer_heatingrod_cost_ct, 0) / 100.0, 4) AS consumer_heatingrod_cost_eur,\n                    COALESCE(consumer_wallbox_kwh, 0) AS consumer_wallbox_kwh,\n                    ROUND(COALESCE(consumer_wallbox_cost_ct, 0) / 100.0, 4) AS consumer_wallbox_cost_eur,\n                    data_source\n                FROM stats_hourly_billing\n                WHERE date >= ?\n                ORDER BY date, hour"
        price_placeholder_count = query.count("?") - 1
        async with connection.execute(
            query, (fallback_price_ct,) * price_placeholder_count + (cutoff,)
        ) as cursor:
            for row in await cursor.fetchall():
                rows.append([row[column] for column in columns])
        await _project_billing_export_rows(
            rows, columns, connection, cutoff, fallback_price_ct
        )
    if True:
        return (columns, rows)
    else:
        return (columns, rows)


def _export_import_price_ct() -> float | None:
    try:
        return float(_get_fixed_total_price(_get_config()))
    except (TypeError, ValueError):
        return


def _export_feed_in_ct() -> float | None:
    try:
        return float(
            _get_config().get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
        )
    except (TypeError, ValueError):
        return DEFAULT_FEED_IN_TARIFF


def _project_energy_export_flows(
    columns: list[str], values: list[Any], projection: dict[str, dict[str, float]]
) -> None:
    """Correct flow kWh and rebuild self-consumption and autarky when those inputs changed."""
    day_projection = projection.get(str(values[0]), {})
    flow_key_map = {
        "solar_to_house_kwh": "solar_to_house_day_kwh",
        "solar_to_battery_kwh": "solar_to_battery_day_kwh",
        "battery_to_house_kwh": "battery_to_house_day_kwh",
        "grid_to_house_kwh": "grid_to_house_day_kwh",
        "grid_to_battery_kwh": "grid_to_battery_day_kwh",
        "consumer_heatpump_kwh": "consumer_heatpump_day_kwh",
        "consumer_heatingrod_kwh": "consumer_heatingrod_day_kwh",
        "consumer_wallbox_kwh": "consumer_wallbox_day_kwh",
    }
    for column, correction_key in flow_key_map.items():
        if correction_key not in day_projection or column not in columns:
            continue
        values[columns.index(column)] = corrected_value(
            projection, str(values[0]), correction_key, values[columns.index(column)]
        )
    own_supply_input_keys = {
        "solar_to_house_day_kwh",
        "grid_to_battery_day_kwh",
        "battery_to_house_day_kwh",
        "home_consumption_day_kwh",
    }
    if not own_supply_input_keys & day_projection.keys():
        return
    if "self_consumption_kwh" not in columns or "autarkie_percent" not in columns:
        return None
    corrected_own_supply_kwh = own_supply_kwh(
        values[columns.index("solar_to_house_kwh")],
        values[columns.index("battery_to_house_kwh")],
        values[columns.index("grid_to_battery_kwh")],
    )
    values[columns.index("self_consumption_kwh")] = corrected_own_supply_kwh
    values[columns.index("autarkie_percent")] = autarky_percent(
        corrected_own_supply_kwh, values[columns.index("home_consumption_kwh")]
    )


async def _project_billing_export_rows(
    rows: list[list[Any]],
    columns: list[str],
    db: Any,
    cutoff: str,
    fallback_price: float,
) -> None:
    projection = await _energy_correction_projection(cutoff)
    if not projection:
        return
    corrected_days = await corrected_daily(db, cutoff, None, projection)
    column_index = {
        column_name: column_position
        for column_position, column_name in enumerate(columns)
    }
    rows_by_date = {}
    for row in rows:
        rows_by_date.setdefault(str(row[column_index["date"]]), []).append(row)
    feed_in_tariff_ct = _export_feed_in_ct()
    for date_key, day_rows in rows_by_date.items():
        day_values = corrected_days.get(date_key)
        if not day_values:
            continue
        raw_hourly_own_supply = _hour_self(day_rows, column_index)
        day_projection = projection.get(date_key, {})
        _scale_export_column(
            day_rows,
            column_index,
            "grid_import_kwh",
            "grid_import_cost_eur",
            day_values.get("raw_grid_import_kwh", 0.0),
            day_values.get(
                "grid_import_day_kwh", day_values.get("raw_grid_import_kwh", 0.0)
            ),
            fallback_price,
            active="grid_import_day_kwh" in day_projection,
        )
        _scale_export_column(
            day_rows,
            column_index,
            "grid_export_kwh",
            "feed_in_revenue_eur",
            day_values.get("raw_grid_export_kwh", 0.0),
            day_values.get(
                "grid_export_day_kwh", day_values.get("raw_grid_export_kwh", 0.0)
            ),
            feed_in_tariff_ct,
            active="grid_export_day_kwh" in day_projection,
        )
        for energy_column, cost_column, correction_key in (
            (
                "consumer_heatpump_kwh",
                "consumer_heatpump_cost_eur",
                "consumer_heatpump",
            ),
            (
                "consumer_heatingrod_kwh",
                "consumer_heatingrod_cost_eur",
                "consumer_heatingrod",
            ),
            ("consumer_wallbox_kwh", "consumer_wallbox_cost_eur", "consumer_wallbox"),
            ("grid_to_battery_kwh", "grid_to_battery_cost_eur", "grid_to_battery"),
            ("solar_to_house_kwh", "", "solar_to_house"),
            ("battery_to_house_kwh", "", "battery_to_house"),
            ("grid_to_house_kwh", "grid_to_house_cost_eur", "grid_to_house"),
        ):
            raw_key = f"raw_{energy_column}"
            corrected_key = f"{correction_key}_day_kwh"
            if raw_key not in day_values:
                if corrected_key not in day_values:
                    continue
            _scale_export_column(
                day_rows,
                column_index,
                energy_column,
                cost_column,
                day_values.get(raw_key, 0.0),
                day_values.get(corrected_key, day_values.get(raw_key, 0.0)),
                fallback_price,
                active=corrected_key in day_projection,
            )
        _scale_export_self_savings(
            day_rows, column_index, day_values, raw_hourly_own_supply, fallback_price
        )
        continue


def _hour_self(hours: list[list[Any]], index: dict[str, int]) -> list[float]:
    required_flow_columns = (
        "solar_to_house_kwh",
        "battery_to_house_kwh",
        "grid_to_battery_kwh",
    )
    if any((required_column not in index for required_column in required_flow_columns)):
        if "self_consumption_kwh" not in index:
            return []
        return [float(row[index["self_consumption_kwh"]] or 0.0) for row in hours]
    return [
        own_supply_kwh(
            row[index["solar_to_house_kwh"]],
            row[index["battery_to_house_kwh"]],
            row[index["grid_to_battery_kwh"]],
        )
        for row in hours
    ]


def _scale_export_column(
    hours: list[list[Any]],
    index: dict[str, int],
    kwh_name: str,
    cost_name: str,
    raw_kwh: float,
    corrected_kwh: float,
    fallback_price_ct: float | None,
    *,
    active: bool = False,
) -> None:
    if kwh_name not in index:
        return
    energy_column_index = index[kwh_name]
    hourly_kwh = [float(row[energy_column_index] or 0.0) for row in hours]
    if not active:
        if kwh_unchanged(raw_kwh, corrected_kwh):
            return
    has_cost_column = bool(cost_name) and cost_name in index
    hourly_costs = (
        [float(row[index[cost_name]] or 0.0) for row in hours]
        if has_cost_column
        else [0.0 for unused_item in hours]
    )
    scaled_hourly_kwh, scaled_hourly_costs = scale_hour_money(
        raw_kwh,
        corrected_kwh,
        hourly_kwh,
        hourly_costs,
        fallback_price_ct=fallback_price_ct,
        hour_sum=sum(hourly_kwh),
        corrected=active,
    )
    for row, hour_kwh, hour_cost_eur in zip(
        hours, scaled_hourly_kwh, scaled_hourly_costs
    ):
        row[energy_column_index] = hour_kwh
        if has_cost_column:
            row[index[cost_name]] = hour_cost_eur


def _scale_export_self_savings(
    hours: list[list[Any]],
    index: dict[str, int],
    values: dict[str, float],
    raw_self: list[float],
    fallback_price_ct: float | None,
) -> None:
    if (
        "self_consumption_savings_eur" not in index
        or "self_consumption_kwh" not in index
    ):
        return None
    if not raw_self:
        return
    flow_column_pairs = (
        ("solar_to_house_kwh", "solar_to_house_day_kwh"),
        ("battery_to_house_kwh", "battery_to_house_day_kwh"),
        ("grid_to_battery_kwh", "grid_to_battery_day_kwh"),
    )
    if all(
        (
            kwh_unchanged(
                values.get(f"raw_{energy_column}", 0.0),
                values.get(correction_key, values.get(f"raw_{energy_column}", 0.0)),
            )
            for energy_column, correction_key in flow_column_pairs
        )
    ):
        return
    corrected_hourly_own_supply = _hour_self(hours, index)
    raw_own_supply_total = sum(raw_self)
    corrected_own_supply_total = sum(corrected_hourly_own_supply)
    if kwh_unchanged(raw_own_supply_total, corrected_own_supply_total):
        return
    hourly_savings = [
        float(row[index["self_consumption_savings_eur"]] or 0.0) for row in hours
    ]
    unused_scaled_kwh, scaled_hourly_savings = scale_hour_money(
        raw_own_supply_total,
        corrected_own_supply_total,
        raw_self,
        hourly_savings,
        fallback_price_ct=fallback_price_ct,
    )
    for row, own_supply_kwh, hour_savings_eur in zip(
        hours, corrected_hourly_own_supply, scaled_hourly_savings
    ):
        row[index["self_consumption_kwh"]] = own_supply_kwh
        row[index["self_consumption_savings_eur"]] = hour_savings_eur


async def _export_smart_charging_rows(
    request: web.Request,
) -> tuple[list[str], list[list[Any]]]:
    cutoff = _export_cutoff(request)
    columns = [
        "date",
        "hour",
        "hour_key",
        "price_ct_kwh",
        "max_price_ct",
        "force_charge_price_ct",
        "grid_to_battery_kwh",
        "solar_to_battery_kwh",
        "battery_to_house_kwh",
        "solar_yield_kwh",
        "home_consumption_kwh",
        "is_cheap",
        "is_force_price",
        "is_night_window",
        "data_source",
    ]
    rows = []
    config = _get_config()
    gpm_coordinator = _get_stats_gpm_coordinator()
    thresholds = {}
    if gpm_coordinator is not None and isinstance(
        getattr(gpm_coordinator, "data", None), dict
    ):
        raw_thresholds = gpm_coordinator.data.get("thresholds") or {}
        if isinstance(raw_thresholds, dict):
            thresholds = raw_thresholds
    max_price_ct = thresholds.get("max_price")
    force_charge_price_ct = thresholds.get("force_charge_price")
    try:
        fallback_price_ct = float(_get_fixed_total_price(config))
    except (TypeError, ValueError):
        fallback_price_ct = DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
    async with _get_db() as connection:
        if not await _table_exists(connection, "stats_hourly_billing"):
            return (columns, rows)
        available_columns = await _table_columns(connection, "stats_hourly_billing")
        price_expression = (
            "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
            if "price_ct_kwh" in available_columns
            else "?"
        )
        async with connection.execute(
            f"SELECT\n                    date,\n                    hour,\n                    hour_key,\n                    {price_expression} AS price_ct_kwh,\n                    COALESCE(grid_to_battery_kwh, 0) AS grid_to_battery_kwh,\n                    COALESCE(solar_to_battery_kwh, 0) AS solar_to_battery_kwh,\n                    COALESCE(battery_to_house_kwh, 0) AS battery_to_house_kwh,\n                    COALESCE(solar_yield_kwh, 0) AS solar_yield_kwh,\n                    COALESCE(home_consumption_kwh, 0) AS home_consumption_kwh,\n                    data_source\n                FROM stats_hourly_billing\n                WHERE date >= ?\n                ORDER BY date, hour",
            (fallback_price_ct, cutoff),
        ) as cursor:
            for row in await cursor.fetchall():
                hour = int(row["hour"] or 0)
                price_ct = float(row["price_ct_kwh"] or 0.0)
                is_night_window = hour < 6 or hour >= 21
                rows.append(
                    [
                        row["date"],
                        hour,
                        row["hour_key"],
                        price_ct,
                        max_price_ct,
                        force_charge_price_ct,
                        row["grid_to_battery_kwh"],
                        row["solar_to_battery_kwh"],
                        row["battery_to_house_kwh"],
                        row["solar_yield_kwh"],
                        row["home_consumption_kwh"],
                        int(
                            max_price_ct is not None and price_ct < float(max_price_ct)
                        ),
                        int(
                            force_charge_price_ct is not None
                            and price_ct < float(force_charge_price_ct)
                        ),
                        int(is_night_window),
                        row["data_source"],
                    ]
                )
    if True:
        return (columns, rows)
    else:
        return (columns, rows)


EXPORT_BUILDERS = {
    "legacy": _export_legacy_daily_rows,
    "energy": _export_energy_rows,
    "solar": _export_solar_rows,
    "billing": _export_billing_rows,
    "smart_charging": _export_smart_charging_rows,
    "statistics": _export_statistics_rows,
}
