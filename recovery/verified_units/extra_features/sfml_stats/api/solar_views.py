# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Solar, summary, energy-flow, statistics, and annual-forecast views."""
from __future__ import annotations
import calendar
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from . import context
from .common import (
    PRODUCTION_CANDIDATE_KWH_THRESHOLD,
    _build_forecast_error_metrics,
    _build_live_today_clean_eval,
    _clock_time,
    _column_expr,
    _corrected_daily_energy_values,
    _energy_correction_projection,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_local_naive_range_for_local_date,
    _get_power_w,
    _get_sensor_value,
    _get_solar_reader,
    _get_stats_gpm_coordinator,
    _gpm_local_hour_key,
    _panel_group_exclusion_reason,
    _stats_visible_exclusion_reason,
    _table_columns,
    _time_context,
    local_only,
)
from .hubble import (
    _hubble_best_window,
    _hubble_build_memory,
    _hubble_decline_hour,
    _hubble_has_battery,
    _hubble_read_workflow_status,
    _hubble_recommendation_start_hour,
    _hubble_resolve_moment,
    _hubble_round,
    _hubble_time_to_minutes,
)
from ..core.shadow_context import build_shadow_signal
from ..core.heatpump_today import capped_hp_pv_kwh, hp_pv_share_percent
from ..core.grid_split import SPLIT_TOLERANCE_W, reconcile_grid_split
from ..core.corrected_energy import (
    _resolve_price,
    component_day,
    corrected_month_sum,
    corrected_value,
    dynamic_month_costs,
    energy_flows_changed,
    dynamic_month_average,
    legacy_dynamic_month,
    load_hourly_component_days,
    monthly_table_energy_cost,
    window_autarky_average,
    yesterday_figures,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today
from ..core.price_mode import (
    MODE_LEGACY_FIXED,
    effective_price_mode,
    feed_in_tariff_ct,
    is_fixed_like,
)
from ..const import (
    DOMAIN,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_INVERTER_AC_OUTPUT,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_WEATHER_ENTITY,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    CONF_BILLING_BASE_FEE,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_PANEL_GROUP_NAMES,
    CONF_SHOW_PANEL_GROUPS,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    DEFAULT_HEATPUMP_COP,
)
from ..readers.solar_reader import PanelGroupData
from ..readers.annual_forecast_reader import AnnualForecastReader
from ..readers.weather_reader import WeatherDataReader
from ..sfml_data_reader import SFMLDataReader
from ..core.consumer_atlas import calculate_billing_period
from ..core.energy_balance import build_daily_supply_balance
from ..core.energy_efficiency import build_energy_chain_summary

if TYPE_CHECKING:
    from aiohttp.web import Request, Response
_LOGGER = logging.getLogger(__name__)


def _query_local_date(request: Request, *, default: date | None = None) -> date:
    date_query = request.query.get("date")
    if not date_query:
        return default or ha_today(context.HASS)
    try:
        return date.fromisoformat(str(date_query))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid date. Expected YYYY-MM-DD.")


def _today_sun_times_from_ha() -> dict[str, Any] | None:
    if context.HASS is None:
        return
    today = ha_today(context.HASS)
    sunrise_datetime = None
    sunset_datetime = None
    try:
        from homeassistant.helpers.sun import (
            get_astral_event_date as get_astral_event_date,
        )

        sunrise_datetime = get_astral_event_date(context.HASS, "sunrise", today)
        sunset_datetime = get_astral_event_date(context.HASS, "sunset", today)
    except Exception as error:
        _LOGGER.debug("Astral sun fallback failed: %s", error)
    if sunrise_datetime is None or sunset_datetime is None:
        sun_state = context.HASS.states.get("sun.sun")
        attributes = sun_state.attributes if sun_state else {}
        if sunrise_datetime is None:
            sunrise_text = attributes.get("next_rising") or attributes.get("sunrise")
            sunrise_datetime = (
                dt_util.parse_datetime(str(sunrise_text)) if sunrise_text else None
            )
        if sunset_datetime is None:
            sunset_text = attributes.get("next_setting") or attributes.get("sunset")
            sunset_datetime = (
                dt_util.parse_datetime(str(sunset_text)) if sunset_text else None
            )
    sunrise_time = _clock_time(sunrise_datetime)
    sunset_time = _clock_time(sunset_datetime)
    if not sunrise_time and (not sunset_time):
        return
    production_duration_seconds = None
    if sunrise_datetime is not None and sunset_datetime is not None:
        local_sunrise = dt_util.as_local(sunrise_datetime)
        local_sunset = dt_util.as_local(sunset_datetime)
        if local_sunset > local_sunrise:
            production_duration_seconds = int(
                (local_sunset - local_sunrise).total_seconds()
            )
    return {
        "sun_times": {"sunrise": sunrise_time, "sunset": sunset_time},
        "production_time": {
            "active": bool(sunrise_time and sunset_time),
            "start_time": sunrise_time,
            "end_time": sunset_time,
            "duration_seconds": production_duration_seconds,
        },
    }


def _clean_evaluation_flags(row: Any) -> dict[str, Any]:
    predicted_kwh = float(row["prediction_kwh"] or 0.0)
    raw_actual_kwh = row["actual_kwh"]
    actual_kwh = float(raw_actual_kwh) if raw_actual_kwh is not None else None
    production_candidate = predicted_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD or (
        actual_kwh is not None and actual_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
    )
    exclusion_reason = _stats_visible_exclusion_reason(row)
    return {
        "production_candidate": production_candidate,
        "clean_evaluation_eligible": bool(
            production_candidate
            and actual_kwh is not None
            and (exclusion_reason is None)
        ),
        "stats_visible_exclusion_reason": exclusion_reason,
        "exclude_from_clean_evaluation": bool(row["exclude_from_clean_evaluation"]),
        "mppt_throttled": bool(row["mppt_throttled"]),
        "mppt_throttle_reason": row["mppt_throttle_reason"],
        "inverter_clipped": bool(row["inverter_clipped"]),
        "has_weather_alert": bool(row["has_weather_alert"]),
        "weather_alert_type": row["weather_alert_type"],
    }


def _average_present(values: list[float | None]) -> float | None:
    """Average only present values. @zara"""
    present_values = [value for value in values if value is not None]
    if not present_values:
        return
    return sum(present_values) / len(present_values)


def _build_daily_summary_forecast_metrics(
    predicted_total_kwh: float | None,
    actual_total_kwh: float | None,
    evaluation_predicted_kwh: float | None,
    evaluation_actual_kwh: float | None,
    accuracy_percent: float | None,
) -> dict[str, float | None]:
    raw_day_metrics = _build_forecast_error_metrics(
        predicted_total_kwh, actual_total_kwh
    )
    evaluation_predicted_kwh_23 = float(evaluation_predicted_kwh or 0.0)
    evaluation_actual_kwh_24 = float(evaluation_actual_kwh or 0.0)
    if evaluation_predicted_kwh_23 > 0.01 and evaluation_actual_kwh_24 > 0.01:
        evaluation_metrics = _build_forecast_error_metrics(
            evaluation_predicted_kwh_23, evaluation_actual_kwh_24
        )
        if accuracy_percent is not None:
            accuracy = round(float(accuracy_percent), 1)
            delta_sign = (
                1.0 if evaluation_actual_kwh_24 >= evaluation_predicted_kwh_23 else -1.0
            )
            signed_error_percent = round(delta_sign * max(0.0, 100.0 - accuracy), 1)
            evaluation_metrics["yield_delta_vs_forecast_percent"] = signed_error_percent
            evaluation_metrics["forecast_error_vs_sot_percent"] = signed_error_percent
            evaluation_metrics["forecast_accuracy_vs_sot_percent"] = accuracy
    else:
        evaluation_metrics = raw_day_metrics.copy()
        if accuracy_percent is not None:
            evaluation_metrics["forecast_accuracy_vs_sot_percent"] = round(
                float(accuracy_percent), 1
            )
    evaluation_metrics.update(
        {
            "raw_day_yield_delta_vs_forecast_percent": raw_day_metrics[
                "yield_delta_vs_forecast_percent"
            ],
            "raw_day_forecast_error_vs_sot_percent": raw_day_metrics[
                "forecast_error_vs_sot_percent"
            ],
            "raw_day_forecast_accuracy_vs_sot_percent": raw_day_metrics[
                "forecast_accuracy_vs_sot_percent"
            ],
            "raw_day_forecast_error_vs_actual_percent": raw_day_metrics[
                "forecast_error_vs_actual_percent"
            ],
            "raw_day_forecast_accuracy_vs_actual_percent": raw_day_metrics[
                "forecast_accuracy_vs_actual_percent"
            ],
        }
    )
    return evaluation_metrics


def _build_statistics_window_from_history_rows(
    rows: list[dict[str, Any]], days: int
) -> dict[str, Any] | None:
    """Build user-facing rolling statistics from completed daily rows. @zara"""
    window_rows = rows[:days]
    if not window_rows:
        return
    return {
        "avg_yield_kwh": round(
            sum((row.get("actual_kwh") or 0.0 for row in window_rows))
            / len(window_rows),
            1,
        ),
        "avg_accuracy": _average_present(
            [
                row.get(
                    "forecast_accuracy_vs_sot_percent",
                    row.get("forecast_accuracy_vs_actual_percent", row.get("accuracy")),
                )
                for row in window_rows
            ]
        ),
        "avg_forecast_error_vs_actual_percent": _average_present(
            [row.get("forecast_error_vs_actual_percent") for row in window_rows]
        ),
        "avg_forecast_error_vs_sot_percent": _average_present(
            [row.get("forecast_error_vs_sot_percent") for row in window_rows]
        ),
        "avg_evaluation_coverage_percent": _average_present(
            [row.get("evaluation_coverage_percent") for row in window_rows]
        ),
        "avg_excluded_mppt_hours": round(
            sum((row.get("excluded_mppt_hours_count") or 0.0 for row in window_rows))
            / len(window_rows),
            1,
        ),
        "total_yield_kwh": round(
            sum((row.get("actual_kwh") or 0.0 for row in window_rows)), 2
        ),
    }


async def _get_summary_outdoor_temperature(config: dict[str, Any]) -> dict[str, Any]:
    """Return compact outdoor weather info from today's coherent weather snapshot. @zara"""
    today_key = ha_today(context.HASS).isoformat()
    try:
        async with _get_db() as db:
            async with db.execute(
                "SELECT temperature_c, cloud_cover_percent, condition, precipitation_mm\n                   FROM hourly_weather_actual\n                   WHERE date = ?\n                     AND (\n                         temperature_c IS NOT NULL\n                         OR cloud_cover_percent IS NOT NULL\n                         OR condition IS NOT NULL\n                         OR precipitation_mm IS NOT NULL\n                     )\n                   ORDER BY hour DESC\n                   LIMIT 1",
                (today_key,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and (
                    row[0] is not None
                    or row[1] is not None
                    or row[2] is not None
                    or (row[3] is not None)
                ):
                    condition = row[2]
                    cloud_cover = row[1]
                    precipitation_mm = row[3]
                    if condition is None:
                        if precipitation_mm is not None and precipitation_mm > 0.5:
                            condition = "rainy"
                        elif cloud_cover is not None and cloud_cover >= 85:
                            condition = "cloudy"
                        elif cloud_cover is not None and cloud_cover >= 35:
                            condition = "partlycloudy"
                        elif cloud_cover is not None:
                            condition = "sunny"
                    return {
                        "temperature_c": (
                            round(float(row[0]), 1) if row[0] is not None else None
                        ),
                        "cloud_cover_percent": (
                            round(float(cloud_cover), 0)
                            if cloud_cover is not None
                            else None
                        ),
                        "condition": condition,
                        "source": "actual_db",
                    }
    except Exception as error:
        _LOGGER.debug("Outdoor temperature measured lookup failed: %s", error)
    weather_entity_id = config.get(CONF_WEATHER_ENTITY)
    if weather_entity_id:
        entity_state = context.HASS.states.get(weather_entity_id)
        if entity_state is not None:
            attributes = entity_state.attributes or {}
            temperature_c = attributes.get("temperature")
            cloud_coverage = attributes.get("cloud_coverage")
            if cloud_coverage is None:
                cloud_coverage = attributes.get("cloud_cover")
            condition = entity_state.state
            if temperature_c is not None:
                return {
                    "temperature_c": round(float(temperature_c), 1),
                    "cloud_cover_percent": (
                        round(float(cloud_coverage), 0)
                        if cloud_coverage is not None
                        else None
                    ),
                    "condition": condition,
                    "source": "sensor",
                }
    try:
        async with _get_db() as db:
            current_hour = ha_now(context.HASS).hour
            async with db.execute(
                "SELECT temperature, clouds\n                   FROM weather_forecast\n                   WHERE forecast_date = ?\n                     AND hour >= ?\n                     AND (temperature IS NOT NULL OR clouds IS NOT NULL)\n                   ORDER BY hour\n                   LIMIT 1",
                (today_key, current_hour),
            ) as cursor:
                row = await cursor.fetchone()
                if row and (row[0] is not None or row[1] is not None):
                    return {
                        "temperature_c": (
                            round(float(row[0]), 1) if row[0] is not None else None
                        ),
                        "cloud_cover_percent": (
                            round(float(row[1]), 0) if row[1] is not None else None
                        ),
                        "condition": None,
                        "source": "forecast",
                    }
            async with db.execute(
                "SELECT temperature, clouds\n                   FROM weather_forecast\n                   WHERE forecast_date >= ?\n                     AND (temperature IS NOT NULL OR clouds IS NOT NULL)\n                   ORDER BY forecast_date, hour\n                   LIMIT 1",
                (today_key,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and (row[0] is not None or row[1] is not None):
                    return {
                        "temperature_c": (
                            round(float(row[0]), 1) if row[0] is not None else None
                        ),
                        "cloud_cover_percent": (
                            round(float(row[1]), 0) if row[1] is not None else None
                        ),
                        "condition": None,
                        "source": "forecast",
                    }
    except Exception as error:
        _LOGGER.debug("Outdoor temperature summary lookup failed: %s", error)
    return {
        "temperature_c": None,
        "cloud_cover_percent": None,
        "condition": None,
        "source": None,
    }


async def _build_hubble_summary(
    today: date, summary: dict[str, Any]
) -> dict[str, Any] | None:
    if False:
        unused_layout_slot = None
    now = ha_now(context.HASS)
    today_key = today.isoformat()
    today_summary = summary.get("today") or {}
    daily_stats = summary.get("daily_stats") or {}
    kpis = summary.get("kpis") or {}
    config = _get_config()
    yesterday_energy_row = None
    yesterday_summary_row = None
    yesterday_price_average = None
    corrected_autarky_7d = None
    efficiency_row = None
    shadow_rows = []
    try:
        async with _get_db() as db:
            async with db.execute("PRAGMA table_info(hourly_predictions)") as cursor:
                available_columns = {row["name"] for row in await cursor.fetchall()}
            optional_columns = [
                optional_column
                for optional_column in (
                    "physics_kwh",
                    "ai_kwh",
                    "ai_confidence",
                    "confidence",
                    "ml_contribution_percent",
                    "clouds",
                    "solar_radiation",
                    "weather_alert_type",
                    "has_weather_alert",
                )
                if optional_column in available_columns
            ]
            select_columns = [
                "target_hour",
                "prediction_kwh",
                "actual_kwh",
                *optional_columns,
            ]
            async with db.execute(
                f"SELECT {', '.join(select_columns)}\n                    FROM hourly_predictions\n                    WHERE target_date = ?\n                    ORDER BY target_hour",
                (today_key,),
            ) as cursor:
                hourly_rows = [dict(row) for row in await cursor.fetchall()]
            try:
                async with db.execute(
                    "SELECT version FROM shadow_pattern_config WHERE id = 1"
                ) as cursor:
                    shadow_version_row = await cursor.fetchone()
                if shadow_version_row:
                    if str(shadow_version_row[0] or "") == "2.0":
                        async with db.execute(
                            "SELECT h.group_name, h.hour, h.shadow_occurrence_rate,\n                                  h.avg_shadow_percent, h.pct_building_tree,\n                                  h.pattern_type, h.confidence, h.sample_count,\n                                  s.shadow_occurrence_rate AS seasonal_occurrence_rate,\n                                  s.avg_shadow_percent AS seasonal_avg_shadow_percent,\n                                  s.dominant_cause AS seasonal_dominant_cause,\n                                  s.sample_count AS seasonal_sample_count,\n                                  s.confidence AS seasonal_confidence\n                           FROM shadow_pattern_hourly h\n                           LEFT JOIN shadow_pattern_seasonal s\n                             ON s.group_name = h.group_name\n                            AND s.hour = h.hour\n                            AND s.month = ?\n                           ORDER BY h.group_name, h.hour",
                            (today.month,),
                        ) as cursor:
                            shadow_rows = [dict(row) for row in await cursor.fetchall()]
            except Exception as error:
                _LOGGER.debug(
                    "Could not read learned shadow context for Hubble: %s", error
                )
            async with db.execute(
                "SELECT AVG(home_consumption_kwh) AS avg_home,\n                          AVG(solar_to_battery_kwh) AS avg_battery_charge,\n                          AVG(autarkie_percent) AS avg_autarky\n                   FROM stats_daily_energy\n                   WHERE date < ?\n                     AND date >= date(?, '-7 days')",
                (today_key, today_key),
            ) as cursor:
                average_energy_row = await cursor.fetchone()
            try:
                historical_energy_columns = await _table_columns(
                    db, "stats_daily_energy"
                )
                autarky_columns = (
                    "date",
                    "autarkie_percent",
                    "home_consumption_kwh",
                    "solar_to_house_kwh",
                    "battery_to_house_kwh",
                    "grid_to_battery_kwh",
                )
                autarky_select = ", ".join(
                    (
                        (
                            "date"
                            if column == "date"
                            else _column_expr(historical_energy_columns, column)
                        )
                        for column in autarky_columns
                    )
                )
                async with db.execute(
                    f"SELECT {autarky_select}\n                        FROM stats_daily_energy\n                        WHERE date < ?\n                          AND date >= date(?, '-7 days')",
                    (today_key, today_key),
                ) as cursor:
                    daily_energy_rows = await cursor.fetchall()
                corrections_7d = await _energy_correction_projection(
                    (today - timedelta(days=7)).isoformat()
                )
                corrected_autarky_rows = []
                for row in daily_energy_rows:
                    day_key = str(row["date"])
                    raw_consumption_kwh = float(row["home_consumption_kwh"] or 0.0)
                    raw_solar_to_house_kwh = float(row["solar_to_house_kwh"] or 0.0)
                    raw_battery_to_house_kwh = float(row["battery_to_house_kwh"] or 0.0)
                    raw_grid_to_battery_kwh = float(row["grid_to_battery_kwh"] or 0.0)
                    corrected_autarky_rows.append(
                        {
                            "stored": row["autarkie_percent"],
                            "raw_consumption": raw_consumption_kwh,
                            "consumption": corrected_value(
                                corrections_7d,
                                day_key,
                                "home_consumption_day_kwh",
                                raw_consumption_kwh,
                            ),
                            "raw_solar_to_house": raw_solar_to_house_kwh,
                            "solar_to_house": corrected_value(
                                corrections_7d,
                                day_key,
                                "solar_to_house_day_kwh",
                                raw_solar_to_house_kwh,
                            ),
                            "raw_battery_to_house": raw_battery_to_house_kwh,
                            "battery_to_house": corrected_value(
                                corrections_7d,
                                day_key,
                                "battery_to_house_day_kwh",
                                raw_battery_to_house_kwh,
                            ),
                            "raw_grid_to_battery": raw_grid_to_battery_kwh,
                            "grid_to_battery": corrected_value(
                                corrections_7d,
                                day_key,
                                "grid_to_battery_day_kwh",
                                raw_grid_to_battery_kwh,
                            ),
                        }
                    )
                corrected_autarky_7d = window_autarky_average(corrected_autarky_rows)
            except Exception as error:
                _LOGGER.debug("Could not project Hubble 7-day autarky: %s", error)
            if config.get(CONF_SENSOR_INVERTER_AC_OUTPUT):
                try:
                    async with db.execute(
                        "SELECT *\n                           FROM stats_energy_efficiency_daily\n                           WHERE date = ?",
                        (today_key,),
                    ) as cursor:
                        efficiency_row = await cursor.fetchone()
                except Exception as error:
                    _LOGGER.debug("Could not read Hubble energy chain: %s", error)
            async with db.execute(
                "SELECT MAX(solar_yield_kwh) AS record_yield\n                   FROM stats_daily_energy\n                   WHERE date < ?",
                (today_key,),
            ) as cursor:
                record_yield_row = await cursor.fetchone()
            yesterday_key = (today - timedelta(days=1)).isoformat()
            try:
                daily_energy_columns = await _table_columns(db, "stats_daily_energy")
                yesterday_columns = (
                    "solar_yield_kwh",
                    "home_consumption_kwh",
                    "grid_import_kwh",
                    "grid_export_kwh",
                    "autarkie_percent",
                    "savings_eur",
                    "feed_in_revenue_eur",
                    "total_cost_eur",
                    "consumer_heatpump_kwh",
                    "consumer_wallbox_kwh",
                    "consumer_hp_pv_kwh",
                    "grid_to_battery_kwh",
                    "solar_to_battery_kwh",
                    "battery_to_house_kwh",
                    "solar_to_house_kwh",
                )
                yesterday_select = ", ".join(
                    (
                        _column_expr(daily_energy_columns, column)
                        for column in yesterday_columns
                    )
                )
                async with db.execute(
                    f"SELECT {yesterday_select}\n                       FROM stats_daily_energy\n                       WHERE date = ?",
                    (yesterday_key,),
                ) as cursor:
                    yesterday_energy_row = await cursor.fetchone()
                try:
                    async with db.execute(
                        "SELECT average_total FROM GPM_daily_averages WHERE date = ?",
                        (yesterday_key,),
                    ) as cursor:
                        yesterday_price_row = await cursor.fetchone()
                        if yesterday_price_row is not None:
                            yesterday_price_average = yesterday_price_row[0]
                except Exception as error:
                    _LOGGER.debug("Could not read yesterday price average: %s", error)
            except Exception as error:
                _LOGGER.debug(
                    "Could not read yesterday daily energy for Hubble: %s", error
                )
            try:
                async with db.execute(
                    "SELECT accuracy_percent, error_kwh, error_percent, avg_temperature_diff, avg_cloud_cover_diff,\n                              forecast_dominant, actual_dominant, predicted_total_kwh, actual_total_kwh\n                       FROM daily_summaries\n                       WHERE date = ?",
                    (yesterday_key,),
                ) as cursor:
                    yesterday_summary_row = await cursor.fetchone()
            except Exception as error:
                _LOGGER.debug("Could not read yesterday summary for Hubble: %s", error)
            memory = None
            try:
                memory = await _hubble_build_memory(
                    db,
                    today,
                    hourly_rows,
                    daily_stats,
                    today_summary,
                    available_columns,
                )
            except Exception as error:
                _LOGGER.debug("Could not build Hubble memory: %s", error)
    except Exception as error:
        _LOGGER.debug("Could not build Hubble summary: %s", error)
        return
    if not hourly_rows and (not daily_stats):
        return
    evaluation_hours_count = int(today_summary.get("evaluation_hours_count") or 0)
    actual_hours = [
        row
        for row in hourly_rows
        if row.get("actual_kwh") is not None
        if float(row.get("actual_kwh") or 0.0) > PRODUCTION_CANDIDATE_KWH_THRESHOLD
        or float(row.get("prediction_kwh") or 0.0) > PRODUCTION_CANDIDATE_KWH_THRESHOLD
    ]
    forecast_today_kwh = sum(
        (float(row.get("prediction_kwh") or 0.0) for row in hourly_rows)
    )
    forecast_tomorrow_kwh = None
    tomorrow = today + timedelta(days=1)
    try:
        solar_reader = _get_solar_reader()
        tomorrow_hourly_predictions = await solar_reader.async_get_hourly_predictions(
            tomorrow
        )
        if tomorrow_hourly_predictions:
            forecast_tomorrow_kwh = _hubble_round(
                sum((row.prediction_kwh for row in tomorrow_hourly_predictions)), 1
            )
        else:
            daily_forecasts = await solar_reader.async_get_daily_forecasts()
            tomorrow_forecast = daily_forecasts.get("tomorrow")
            if tomorrow_forecast and tomorrow_forecast.prediction_kwh is not None:
                forecast_tomorrow_kwh = _hubble_round(
                    tomorrow_forecast.prediction_kwh, 1
                )
    except Exception as error:
        _LOGGER.debug("Could not build Hubble tomorrow's forecast: %s", error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT prediction_kwh\n                       FROM daily_forecasts\n                       WHERE forecast_date = ?\n                       ORDER BY forecast_type = 'tomorrow' DESC\n                       LIMIT 1",
                    (tomorrow.isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0] is not None:
                        forecast_tomorrow_kwh = _hubble_round(row[0], 1)
        except Exception as fallback_error:
            _LOGGER.debug(
                "Could not load Hubble tomorrow daily fallback: %s", fallback_error
            )
    sunset_minutes = _hubble_time_to_minutes(
        (summary.get("sun_times") or {}).get("sunset")
    )
    if sunset_minutes is None:
        try:
            sun_state = context.HASS.states.get("sun.sun")
            if sun_state:
                sunset_text = sun_state.attributes.get(
                    "next_setting"
                ) or sun_state.attributes.get("sunset")
                if sunset_text:
                    sunset_datetime = datetime.fromisoformat(sunset_text)
                    local_sunset = as_ha_local(sunset_datetime, context.HASS)
                    sunset_minutes = local_sunset.hour * 60 + local_sunset.minute
        except Exception as error:
            _LOGGER.debug("Hubble sunset fallback from HA sun.sun failed: %s", error)
    if sunset_minutes is None:
        sunset_minutes = 1080
    moment, moment_gate = _hubble_resolve_moment(
        now, sunset_minutes, forecast_today_kwh, evaluation_hours_count
    )
    recommendation_start_hour = _hubble_recommendation_start_hour(now)
    best_window = _hubble_best_window(
        hourly_rows, recommendation_start_hour, sunset_minutes
    )
    shadow_signal = build_shadow_signal(
        shadow_rows, start_hour=recommendation_start_hour
    )
    if moment == "morning":
        best_window = (
            _hubble_best_window(
                hourly_rows, max(8, recommendation_start_hour), sunset_minutes
            )
            or best_window
        )
    decline_hour = _hubble_decline_hour(hourly_rows, best_window)
    average_consumption_7d_kwh = (
        _hubble_round(average_energy_row["avg_home"], 1) if average_energy_row else None
    )
    average_battery_charge_7d_kwh = (
        _hubble_round(average_energy_row["avg_battery_charge"], 1)
        if average_energy_row
        else None
    )
    if corrected_autarky_7d is None:
        average_autarky_7d_percent = (
            _hubble_round(average_energy_row["avg_autarky"], 1)
            if average_energy_row
            else None
        )
    else:
        average_autarky_7d_percent = _hubble_round(corrected_autarky_7d, 1)
    solar_to_battery = _hubble_round(daily_stats.get("solar_to_battery_kwh"), 2)
    battery_available = _hubble_has_battery(
        config, daily_stats, average_battery_charge_7d_kwh
    )
    day_balance = build_daily_supply_balance(
        household_total_kwh=daily_stats.get("home_consumption_kwh"),
        solar_to_house_kwh=daily_stats.get("solar_to_house_kwh"),
        battery_to_house_kwh=daily_stats.get("battery_to_house_kwh"),
        grid_to_house_kwh=daily_stats.get("grid_to_house_kwh"),
        battery_available=battery_available,
    )
    energy_chain = None
    if efficiency_row is not None:
        elapsed_seconds = max(1.0, now.hour * 3600.0 + now.minute * 60.0 + now.second)
        energy_chain = build_energy_chain_summary(
            efficiency_row,
            net_autarky_percent=daily_stats.get("autarkie_percent"),
            elapsed_seconds=elapsed_seconds,
            battery_available=battery_available,
        )
    if not battery_available:
        solar_to_battery = None
        average_battery_charge_7d_kwh = None
    battery_near_average = (
        battery_available
        and solar_to_battery is not None
        and (average_battery_charge_7d_kwh is not None)
        and (
            abs(solar_to_battery - average_battery_charge_7d_kwh)
            <= max(0.5, average_battery_charge_7d_kwh * 0.15)
        )
    )
    remaining_forecast_kwh = sum(
        (
            float(row.get("prediction_kwh") or 0.0)
            for row in hourly_rows
            if int(row.get("target_hour") or -1) >= now.hour
        )
    )
    strongest_ai_delta = None
    for row in hourly_rows:
        physics_kwh = row.get("physics_kwh")
        ai_kwh = row.get("ai_kwh")
        if physics_kwh is None or ai_kwh is None:
            continue
        physics_kwh = float(physics_kwh or 0.0)
        ai_kwh = float(ai_kwh or 0.0)
        if physics_kwh <= 0.05:
            continue
        ai_delta_percent = (ai_kwh - physics_kwh) / physics_kwh * 100.0
        ai_delta_candidate = {
            "hour": int(row.get("target_hour") or 0),
            "delta_percent": round(ai_delta_percent, 1),
            "physics_kwh": round(physics_kwh, 2),
            "ai_kwh": round(ai_kwh, 2),
            "confidence": _hubble_round(
                row.get("ai_confidence") or row.get("confidence"), 2
            ),
            "ml_share_percent": _hubble_round(row.get("ml_contribution_percent"), 0),
        }
        if strongest_ai_delta is None or abs(ai_delta_candidate["delta_percent"]) > abs(
            strongest_ai_delta["delta_percent"]
        ):
            strongest_ai_delta = ai_delta_candidate
    forecast_quality_percent = _hubble_round(
        today_summary.get(
            "forecast_accuracy_vs_sot_percent",
            today_summary.get("forecast_accuracy_vs_actual_percent"),
        ),
        1,
    )
    forecast_error_percent = _hubble_round(
        today_summary.get(
            "forecast_error_vs_sot_percent",
            today_summary.get("forecast_error_vs_actual_percent"),
        ),
        1,
    )
    tip_type = "flex_load_window" if best_window else "watch_day"
    if moment == "evening":
        tip_type = "evening_review"
    elif battery_near_average and solar_to_battery:
        tip_type = "battery_served"
    elif (
        best_window
        and kpis.get("price_current") is not None
        and (kpis.get("price_avg") is not None)
    ):
        try:
            if float(kpis["price_current"]) < float(kpis["price_avg"]):
                tip_type = "cheap_price_plus_solar"
        except (TypeError, ValueError):
            pass
    yesterday_summary = None
    if yesterday_energy_row:
        yesterday_energy = dict(yesterday_energy_row)
        yesterday_forecast_summary = (
            dict(yesterday_summary_row) if yesterday_summary_row else {}
        )
        yesterday_corrections = await _energy_correction_projection(yesterday_key)

        def _keep_corrected(raw: Any, metric: str) -> Any:
            if raw is None:
                return
            return corrected_value(yesterday_corrections, yesterday_key, metric, raw)

        raw_consumption_kwh = yesterday_energy.get("home_consumption_kwh")
        raw_export_kwh = yesterday_energy.get("grid_export_kwh")
        raw_solar_to_house_kwh = yesterday_energy.get("solar_to_house_kwh")
        raw_battery_to_house_kwh = yesterday_energy.get("battery_to_house_kwh")
        raw_grid_to_battery_kwh = yesterday_energy.get("grid_to_battery_kwh")
        consumption_kwh = corrected_value(
            yesterday_corrections,
            yesterday_key,
            "home_consumption_day_kwh",
            raw_consumption_kwh,
        )
        export_kwh = corrected_value(
            yesterday_corrections, yesterday_key, "grid_export_day_kwh", raw_export_kwh
        )
        solar_to_house_kwh = _keep_corrected(
            raw_solar_to_house_kwh, "solar_to_house_day_kwh"
        )
        battery_to_house_kwh = _keep_corrected(
            raw_battery_to_house_kwh, "battery_to_house_day_kwh"
        )
        grid_to_battery_kwh = _keep_corrected(
            raw_grid_to_battery_kwh, "grid_to_battery_day_kwh"
        )
        solar_to_battery_kwh = _keep_corrected(
            yesterday_energy.get("solar_to_battery_kwh"), "solar_to_battery_day_kwh"
        )
        heatpump_kwh = _keep_corrected(
            yesterday_energy.get("consumer_heatpump_kwh"), "consumer_heatpump_day_kwh"
        )
        wallbox_kwh = _keep_corrected(
            yesterday_energy.get("consumer_wallbox_kwh"), "consumer_wallbox_day_kwh"
        )
        heatpump_daily_kwh = heatpump_kwh or 0.0
        heatpump_pv_kwh = capped_hp_pv_kwh(
            yesterday_energy.get("consumer_hp_pv_kwh"), heatpump_kwh
        )
        heatpump_pv_percent = hp_pv_share_percent(heatpump_daily_kwh, heatpump_pv_kwh)
        try:
            fixed_total_price = float(_get_fixed_total_price(config))
        except (TypeError, ValueError):
            fixed_total_price = None
        try:
            configured_feed_in_tariff = float(
                config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0
            )
        except (TypeError, ValueError):
            configured_feed_in_tariff = DEFAULT_FEED_IN_TARIFF
        import_price_ct, import_price_source = _resolve_price(
            daily_average=yesterday_price_average, tariff_fallback_ct=fixed_total_price
        )
        yesterday_figures_137 = yesterday_figures(
            raw_consumption=raw_consumption_kwh or 0.0,
            consumption=consumption_kwh,
            raw_solar_to_house=raw_solar_to_house_kwh or 0.0,
            solar_to_house=0.0 if solar_to_house_kwh is None else solar_to_house_kwh,
            raw_battery_to_house=raw_battery_to_house_kwh or 0.0,
            battery_to_house=(
                0.0 if battery_to_house_kwh is None else battery_to_house_kwh
            ),
            raw_grid_to_battery=raw_grid_to_battery_kwh or 0.0,
            grid_to_battery=0.0 if grid_to_battery_kwh is None else grid_to_battery_kwh,
            raw_export=raw_export_kwh or 0.0,
            export_kwh=export_kwh,
            stored_autarky=yesterday_energy.get("autarkie_percent"),
            stored_savings=yesterday_energy.get("savings_eur"),
            stored_revenue=yesterday_energy.get("feed_in_revenue_eur"),
            import_fallback_ct=import_price_ct,
            feed_in_tariff_ct=configured_feed_in_tariff,
        )
        yesterday_summary = {
            "solar_yield_kwh": _hubble_round(
                corrected_value(
                    yesterday_corrections,
                    yesterday_key,
                    "solar_yield_day_kwh",
                    yesterday_energy.get("solar_yield_kwh"),
                ),
                2,
            ),
            "home_consumption_kwh": _hubble_round(consumption_kwh, 2),
            "grid_import_kwh": _hubble_round(
                corrected_value(
                    yesterday_corrections,
                    yesterday_key,
                    "grid_import_day_kwh",
                    yesterday_energy.get("grid_to_house_kwh")
                    or yesterday_energy.get("grid_import_kwh"),
                ),
                2,
            ),
            "grid_export_kwh": _hubble_round(export_kwh, 2),
            "autarky_percent": _hubble_round(
                yesterday_figures_137["autarky_percent"], 1
            ),
            "savings_eur": _hubble_round(yesterday_figures_137["savings_eur"], 2),
            "feed_in_revenue_eur": _hubble_round(
                yesterday_figures_137["feed_in_revenue_eur"], 2
            ),
            "grid_to_battery_kwh": _hubble_round(grid_to_battery_kwh, 2),
            "solar_to_battery_kwh": _hubble_round(solar_to_battery_kwh, 2),
            "battery_to_house_kwh": _hubble_round(battery_to_house_kwh, 2),
            "hp_kwh": _hubble_round(heatpump_daily_kwh, 2),
            "hp_pv_kwh": _hubble_round(heatpump_pv_kwh, 2),
            "hp_pv_percent": heatpump_pv_percent,
            "wallbox_kwh": _hubble_round(wallbox_kwh, 2),
            "accuracy_percent": _hubble_round(
                yesterday_forecast_summary.get("accuracy_percent"), 1
            ),
            "error_percent": _hubble_round(
                yesterday_forecast_summary.get("error_percent"), 1
            ),
            "avg_temperature_diff": _hubble_round(
                yesterday_forecast_summary.get("avg_temperature_diff"), 1
            ),
            "avg_cloud_cover_diff": _hubble_round(
                yesterday_forecast_summary.get("avg_cloud_cover_diff"), 1
            ),
            "forecast_dominant": yesterday_forecast_summary.get("forecast_dominant"),
            "actual_dominant": yesterday_forecast_summary.get("actual_dominant"),
            "predicted_total_kwh": _hubble_round(
                yesterday_forecast_summary.get("predicted_total_kwh"), 2
            ),
            "actual_total_kwh": _hubble_round(
                yesterday_forecast_summary.get("actual_total_kwh"), 2
            ),
        }
    record_signal = None
    solar_yield_kwh = _hubble_round(daily_stats.get("solar_yield_kwh"), 2)
    record_yield_kwh = (
        _hubble_round(record_yield_row["record_yield"], 2) if record_yield_row else None
    )
    if (
        solar_yield_kwh is not None
        and record_yield_kwh is not None
        and (record_yield_kwh > 0)
    ):
        if solar_yield_kwh >= record_yield_kwh:
            record_signal = {
                "level": "new_record",
                "solar_kwh": solar_yield_kwh,
                "record_kwh": record_yield_kwh,
            }
        elif solar_yield_kwh >= record_yield_kwh * 0.9:
            record_signal = {
                "level": "near_record",
                "solar_kwh": solar_yield_kwh,
                "record_kwh": record_yield_kwh,
            }
    forecast_issue = None
    if forecast_error_percent is not None:
        if abs(float(forecast_error_percent)) >= 30.0:
            warning_reason = None
            for row in hourly_rows:
                if row.get("weather_alert_type"):
                    warning_reason = row.get("weather_alert_type")
                    break
                if row.get("has_weather_alert"):
                    warning_reason = "weather alert"
            forecast_issue = {
                "error_percent": forecast_error_percent,
                "reason": warning_reason,
            }
    heatpump_feedback = None
    if (
        yesterday_summary
        and yesterday_summary.get("hp_kwh")
        and (yesterday_summary.get("hp_pv_percent") is not None)
    ):
        heatpump_pv_share = float(yesterday_summary["hp_pv_percent"])
        if heatpump_pv_share >= 70.0:
            heatpump_feedback = {
                "type": "heatpump_praise",
                "hp_pv_percent": _hubble_round(heatpump_pv_share, 0),
            }
        elif heatpump_pv_share < 25.0 and best_window:
            heatpump_feedback = {
                "type": "heatpump_tip",
                "hp_pv_percent": _hubble_round(heatpump_pv_share, 0),
                "window": best_window,
            }
    negative_price_signal = None
    try:
        current_price_ct = float(kpis.get("price_current"))
        if current_price_ct < 0:
            negative_price_signal = {"price_ct": _hubble_round(current_price_ct, 2)}
    except (TypeError, ValueError):
        pass
    from ..core.dev_mode import is_dev_active as is_dev_active

    workflow_status = (
        _hubble_read_workflow_status() if is_dev_active(context.HASS) else None
    )
    if not is_dev_active(context.HASS):
        strongest_ai_delta = None
    configured_consumers = []
    consumer_specs = [
        (CONF_SENSOR_HEATPUMP_POWER, "heatpump"),
        (CONF_SENSOR_HEATINGROD_POWER, "heatingrod"),
        (CONF_SENSOR_WALLBOX_POWER, "wallbox"),
    ]
    from ..core.eai_wp_sources import (
        resolved_heatpump_entity as resolved_heatpump_entity,
    )

    for sensor_config_key, consumer_key in consumer_specs:
        power_entity_id = (
            resolved_heatpump_entity(context.HASS, config, sensor_config_key)
            if sensor_config_key == CONF_SENSOR_HEATPUMP_POWER
            else config.get(sensor_config_key)
        )
        if power_entity_id:
            configured_consumers.append(
                {
                    "config_key": consumer_key,
                    "power_entity_id": power_entity_id,
                    "daily_entity_id": None,
                    "mode": "integrated",
                }
            )
    return {
        "available": True,
        "agent": "Hubble",
        "moment": moment,
        "moment_gate": moment_gate,
        "tip_type": tip_type,
        "generated_at": now.isoformat(timespec="seconds"),
        "actual_hours_count": len(actual_hours),
        "best_window": best_window,
        "variant_seed": (today.day + today.weekday() + now.hour // 4) % 3,
        "yesterday": yesterday_summary,
        "configured_consumers": configured_consumers,
        "day_balance": day_balance,
        "energy_chain": energy_chain,
        "metrics": {
            "solar_yield_kwh": _hubble_round(daily_stats.get("solar_yield_kwh"), 2),
            "home_consumption_kwh": _hubble_round(
                daily_stats.get("home_consumption_kwh"), 2
            ),
            "grid_import_kwh": _hubble_round(
                (
                    daily_stats.get("grid_to_house_kwh")
                    if daily_stats.get("grid_to_house_kwh") not in (None, 0)
                    else daily_stats.get("grid_import_kwh")
                ),
                2,
            ),
            "autarky_percent": _hubble_round(daily_stats.get("autarkie_percent"), 1),
            "battery_available": battery_available,
            "solar_to_battery_kwh": solar_to_battery,
            "battery_to_house_kwh": (
                _hubble_round(daily_stats.get("battery_to_house_kwh"), 2)
                if battery_available
                else None
            ),
            "peak_solar_w": _hubble_round(daily_stats.get("peak_solar_w"), 0),
            "peak_solar_time": daily_stats.get("peak_solar_time") or None,
            "forecast_today_kwh": _hubble_round(forecast_today_kwh, 1),
            "forecast_tomorrow_kwh": forecast_tomorrow_kwh,
            "forecast_remaining_kwh": _hubble_round(remaining_forecast_kwh, 1),
            "evaluation_predicted_kwh": _hubble_round(
                today_summary.get("evaluation_predicted_kwh"), 2
            ),
            "evaluation_actual_kwh": _hubble_round(
                today_summary.get("evaluation_actual_kwh"), 2
            ),
            "forecast_quality_percent": forecast_quality_percent,
            "forecast_error_percent": forecast_error_percent,
            "avg_home_consumption_7d_kwh": average_consumption_7d_kwh,
            "avg_battery_charge_7d_kwh": average_battery_charge_7d_kwh,
            "avg_autarky_7d_percent": average_autarky_7d_percent,
            "price_current_ct": _hubble_round(kpis.get("price_current"), 2),
            "price_avg_ct": _hubble_round(kpis.get("price_avg"), 2),
            "price_min_ct": _hubble_round(kpis.get("price_min"), 2),
            "price_max_ct": _hubble_round(kpis.get("price_max"), 2),
            "decline_hour": decline_hour,
        },
        "signals": {
            "battery_available": battery_available,
            "battery_near_average": battery_near_average,
            "strongest_ai_delta": strongest_ai_delta,
            "record_signal": record_signal,
            "forecast_issue": forecast_issue,
            "helper_feedback": heatpump_feedback,
            "negative_price_signal": negative_price_signal,
            "workflow_status": workflow_status,
            "learned_shadow": shadow_signal,
        },
        "memory": memory,
    }


def _build_learning_basis_by_day(
    rows: list[aiosqlite.Row],
) -> dict[str, dict[str, Any]]:
    """Build learning eligibility metrics from hourly prediction rows. @zara"""
    learning_by_day = {}
    for row in rows:
        target_date = row["target_date"]
        if not target_date:
            continue
        predicted_kwh = float(row["prediction_kwh"] or 0.0)
        raw_actual_kwh = row["actual_kwh"]
        actual_kwh = float(raw_actual_kwh) if raw_actual_kwh is not None else None
        production_candidate = predicted_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD or (
            actual_kwh is not None and actual_kwh > PRODUCTION_CANDIDATE_KWH_THRESHOLD
        )
        if not production_candidate:
            continue
        day_learning = learning_by_day.setdefault(
            target_date,
            {
                "learning_candidate_hours_count": 0,
                "learning_hours_count": 0,
                "discarded_learning_hours_count": 0,
                "discarded_learning_reason_breakdown": {},
            },
        )
        day_learning["learning_candidate_hours_count"] += 1
        learning_exclusion_reason = None
        if actual_kwh is None:
            learning_exclusion_reason = "missing_data"
        elif bool(row["exclude_from_learning"]):
            if bool(row["mppt_throttled"]):
                learning_exclusion_reason = "mppt_throttled"
            elif bool(row["inverter_clipped"]):
                learning_exclusion_reason = "inverter_clipped"
            elif row["mppt_throttle_reason"]:
                learning_exclusion_reason = row["mppt_throttle_reason"]
            elif row["weather_alert_type"]:
                learning_exclusion_reason = row["weather_alert_type"]
            elif bool(row["has_weather_alert"]):
                learning_exclusion_reason = "weather_alert"
            else:
                learning_exclusion_reason = "excluded"
        if learning_exclusion_reason:
            day_learning["discarded_learning_hours_count"] += 1
            reason_breakdown = day_learning["discarded_learning_reason_breakdown"]
            reason_breakdown[learning_exclusion_reason] = (
                reason_breakdown.get(learning_exclusion_reason, 0) + 1
            )
            continue
        day_learning["learning_hours_count"] += 1
        continue
    for day_learning in learning_by_day.values():
        candidate_hours = day_learning["learning_candidate_hours_count"]
        day_learning["learning_coverage_percent"] = (
            round(day_learning["learning_hours_count"] / candidate_hours * 100.0, 1)
            if candidate_hours
            else None
        )
    return learning_by_day


def _build_summary_week_from_hourly_rows(
    rows: list[aiosqlite.Row],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Build summary metrics from hourly rows using the active clean-eval rule. @zara"""
    rows_by_day = {}
    for row in rows:
        day_key = row["target_date"]
        if day_key:
            rows_by_day.setdefault(day_key, []).append(row)
    if not rows_by_day:
        return ({}, None)
    today_key = ha_today(context.HASS).isoformat()
    evaluation_cutoff_hour = ha_now(context.HASS).hour - 1
    daily_summaries = {
        day_key: _build_live_today_clean_eval(
            day_rows,
            evaluation_cutoff_hour=(
                evaluation_cutoff_hour if day_key == today_key else None
            ),
        )
        for day_key, day_rows in rows_by_day.items()
    }
    summary_values = list(daily_summaries.values())
    week_summary = {
        "total_production": round(
            sum((day_summary["production"] for day_summary in summary_values)), 2
        ),
        "total_forecast": round(
            sum((day_summary["forecast"] for day_summary in summary_values)), 2
        ),
        "avg_accuracy": _average_present(
            [
                day_summary.get(
                    "forecast_accuracy_vs_sot_percent",
                    day_summary.get(
                        "forecast_accuracy_vs_actual_percent",
                        day_summary.get("accuracy"),
                    ),
                )
                for day_summary in summary_values
            ]
        ),
        "avg_forecast_error_vs_actual_percent": _average_present(
            [
                day_summary.get("forecast_error_vs_actual_percent")
                for day_summary in summary_values
            ]
        ),
        "avg_forecast_error_vs_sot_percent": _average_present(
            [
                day_summary.get("forecast_error_vs_sot_percent")
                for day_summary in summary_values
            ]
        ),
        "avg_evaluation_coverage_percent": _average_present(
            [
                day_summary["evaluation_coverage_percent"]
                for day_summary in summary_values
            ]
        ),
        "avg_excluded_mppt_hours": (
            sum(
                (
                    day_summary["excluded_mppt_hours_count"]
                    for day_summary in summary_values
                )
            )
            / len(summary_values)
            if summary_values
            else None
        ),
        "days_count": len(summary_values),
    }
    return (daily_summaries, week_summary)


async def _corrected_finalized_solar_values(start_date: str) -> dict[str, float]:
    """Project PV yield for the exact finalized-day population used by annual reports."""
    energy_corrections = await _energy_correction_projection(start_date)
    result = {}
    async with _get_db() as db:
        async with db.execute(
            "SELECT date, actual_total_kwh solar_yield_kwh\n               FROM daily_summaries\n               WHERE date >= ? AND actual_total_kwh > 0",
            (start_date,),
        ) as cursor:
            for row in await cursor.fetchall():
                result[row["date"]] = corrected_value(
                    energy_corrections,
                    row["date"],
                    "solar_yield_day_kwh",
                    row["solar_yield_kwh"],
                )
    if True:
        return result


async def _get_today_yield_from_db() -> float | None:
    """Get today's solar yield from the SFML live actual SOT. @zara"""
    try:
        today_key = ha_today(context.HASS).isoformat()
        async with _get_db() as db:
            async with db.execute(
                "SELECT total_actual_kwh\n                   FROM actual_live_state\n                   WHERE date = ?",
                (today_key,),
            ) as cursor:
                live_actual_row = await cursor.fetchone()
            if live_actual_row:
                if live_actual_row["total_actual_kwh"] is not None:
                    return max(0.0, float(live_actual_row["total_actual_kwh"]))
            async with db.execute(
                "SELECT SUM(ppg.actual_kwh) as total\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                   WHERE hp.target_date = ?",
                (today_key,),
            ) as cursor:
                row = await cursor.fetchone()
        if row and row["total"] is not None:
            return max(0.0, float(row["total"]))
    except Exception as db_error:
        _LOGGER.error("Error getting today's yield from database: %s", db_error)
        return
    return None if True else None


async def _get_weather_from_db(
    days: int = 7, *, start_date: date | None = None, end_date: date | None = None
) -> dict[str, dict[str, dict]]:
    """Get hourly weather data from database. @zara"""
    try:
        if context.SOLAR_PATH is None:
            return {}
        weather_reader = WeatherDataReader(context.SOLAR_PATH.parent)
        if not weather_reader.is_available:
            _LOGGER.debug("Weather database not available")
            return {}
        weather_start_date = start_date or ha_today(context.HASS) - timedelta(days=days)
        weather_rows = await weather_reader.async_get_hourly_weather(
            start_date=weather_start_date
        )
        if not weather_rows:
            _LOGGER.debug("No hourly weather data in database")
            return {}
        result = {}
        for weather_hour in weather_rows:
            if end_date is not None:
                if weather_hour.date > end_date:
                    continue
            date_key = weather_hour.date.isoformat()
            hour_key = str(weather_hour.hour)
            if date_key not in result:
                result[date_key] = {}
            result[date_key][hour_key] = {
                "temperature_c": weather_hour.temperature_c,
                "humidity_percent": weather_hour.humidity_percent,
                "wind_speed_ms": weather_hour.wind_speed_ms,
                "precipitation_mm": weather_hour.precipitation_mm,
                "solar_radiation_wm2": weather_hour.solar_radiation_wm2,
                "cloud_cover_percent": weather_hour.cloud_cover_percent,
            }
        _LOGGER.debug("Loaded weather data for %d days from database", len(result))
        return result
    except Exception as db_error:
        _LOGGER.error("Error getting weather from database: %s", db_error)
        return {}


async def _get_weather_forecast_from_db(
    days: int = 3, *, start_date: date | None = None, end_date: date | None = None
) -> dict[str, dict[str, dict]]:
    """Get corrected weather forecast from database. @zara"""
    try:
        if context.SOLAR_PATH is None:
            return {}
        weather_reader = WeatherDataReader(context.SOLAR_PATH.parent)
        if not weather_reader.is_available:
            _LOGGER.debug("Weather database not available for forecast")
            return {}
        forecast_start_date = start_date or ha_today(context.HASS)
        forecast_end_date = end_date or forecast_start_date + timedelta(days=days)
        forecast_rows = await weather_reader.async_get_forecast_weather(
            start_date=forecast_start_date, end_date=forecast_end_date
        )
        if not forecast_rows:
            _LOGGER.debug("No forecast weather data in database")
            return {}
        result = {}
        for weather_hour in forecast_rows:
            date_key = weather_hour.date.isoformat()
            hour_key = str(weather_hour.hour)
            if date_key not in result:
                result[date_key] = {}
            result[date_key][hour_key] = {
                "temperature": weather_hour.temperature_c,
                "humidity": weather_hour.humidity_percent,
                "wind": weather_hour.wind_speed_ms,
                "precipitation": weather_hour.precipitation_mm,
                "clouds": weather_hour.cloud_cover_percent,
                "solar_radiation_wm2": weather_hour.solar_radiation_wm2,
            }
        _LOGGER.debug("Loaded forecast weather for %d days from database", len(result))
        return result
    except Exception as db_error:
        _LOGGER.error("Error getting weather forecast from database: %s", db_error)
        return {}


class SolarDataView(HomeAssistantView):
    """API for solar data. @zara"""

    url = "/api/sfml_stats/solar"
    name = "api:sfml_stats:solar"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return solar data. @zara"""
        requested_days = max(1, min(int(request.query.get("days", 7)), 365))
        include_hourly = request.query.get("hourly", "true").lower() == "true"
        target_date = _query_local_date(request)
        range_start_date = target_date - timedelta(days=requested_days - 1)
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "selected_date": target_date.isoformat(),
            "data": {},
        }
        try:
            solar_reader = _get_solar_reader()
            daily_summary_rows = await solar_reader.async_get_daily_summaries(
                start_date=range_start_date, end_date=target_date
            )
            async with _get_db() as db:
                async with db.execute(
                    "SELECT target_date, target_hour, prediction_kwh, actual_kwh,\n                              exclude_from_learning, exclude_from_clean_evaluation,\n                              mppt_throttled, mppt_throttle_reason,\n                              inverter_clipped, has_weather_alert,\n                              weather_alert_type\n                       FROM hourly_predictions\n                       WHERE target_date BETWEEN ? AND ?\n                       ORDER BY target_date, target_hour",
                    (range_start_date.isoformat(), target_date.isoformat()),
                ) as cursor:
                    hourly_rows = await cursor.fetchall()
            learning_basis_by_day = _build_learning_basis_by_day(hourly_rows)
            evaluation_flags_by_hour = {
                (
                    row["target_date"],
                    int(row["target_hour"] or 0),
                ): _clean_evaluation_flags(row)
                for row in hourly_rows
            }
            result["data"]["daily"] = [
                {
                    "date": daily_summary.date.isoformat(),
                    "overall": {
                        "predicted_total_kwh": daily_summary.predicted_total_kwh,
                        "actual_total_kwh": daily_summary.actual_total_kwh,
                        "accuracy_percent": daily_summary.accuracy_percent,
                        **_build_daily_summary_forecast_metrics(
                            daily_summary.predicted_total_kwh,
                            daily_summary.actual_total_kwh,
                            daily_summary.evaluation_predicted_kwh,
                            daily_summary.evaluation_actual_kwh,
                            daily_summary.accuracy_percent,
                        ),
                        "error_kwh": daily_summary.error_kwh,
                        "production_hours": daily_summary.production_hours,
                        "peak_hour": daily_summary.peak_hour,
                        "peak_kwh": daily_summary.peak_kwh,
                        "evaluation_predicted_kwh": daily_summary.evaluation_predicted_kwh,
                        "evaluation_actual_kwh": daily_summary.evaluation_actual_kwh,
                        "evaluation_coverage_percent": daily_summary.evaluation_coverage_percent,
                        "excluded_hours_count": daily_summary.excluded_hours_count,
                        "excluded_mppt_hours_count": daily_summary.excluded_mppt_hours_count,
                        "evaluation_hours_count": daily_summary.evaluation_hours_count,
                        "production_candidate_hours_count": daily_summary.production_candidate_hours_count,
                        "missing_actual_hours_count": daily_summary.missing_actual_hours_count,
                        "excluded_weather_alert_hours_count": daily_summary.excluded_weather_alert_hours_count,
                        "excluded_inverter_clipped_hours_count": daily_summary.excluded_inverter_clipped_hours_count,
                        "excluded_reason_breakdown_json": daily_summary.excluded_reason_breakdown_json,
                        **learning_basis_by_day.get(daily_summary.date.isoformat(), {}),
                    },
                    "time_windows": {
                        "morning_accuracy": daily_summary.morning_accuracy,
                        "midday_accuracy": daily_summary.midday_accuracy,
                        "afternoon_accuracy": daily_summary.afternoon_accuracy,
                    },
                    "ml_metrics": {
                        "mae": daily_summary.ml_mae,
                        "rmse": daily_summary.ml_rmse,
                        "r2_score": daily_summary.ml_r2_score,
                    },
                }
                for daily_summary in daily_summary_rows
            ]
        except Exception as db_error:
            _LOGGER.error("Error loading daily summaries from database: %s", db_error)
            result["data"]["daily"] = []
        if include_hourly:
            try:
                solar_reader = _get_solar_reader()
                hourly_results = []
                current_date = range_start_date
                while current_date <= target_date:
                    day_predictions = await solar_reader.async_get_hourly_predictions(
                        target_date=current_date
                    )
                    for prediction in day_predictions:
                        evaluation_flags = evaluation_flags_by_hour.get(
                            (
                                prediction.target_date.isoformat(),
                                int(prediction.target_hour or 0),
                            ),
                            {},
                        )
                        hourly_results.append(
                            {
                                "target_datetime": prediction.target_datetime.isoformat(),
                                "target_hour": prediction.target_hour,
                                "target_date": prediction.target_date.isoformat(),
                                "prediction_kwh": prediction.prediction_kwh,
                                "actual_kwh": prediction.actual_kwh,
                                "accuracy_percent": prediction.accuracy_percent,
                                "error_kwh": prediction.error_kwh,
                                "prediction_method": prediction.prediction_method,
                                "ml_contribution_percent": prediction.ml_contribution_percent,
                                "confidence": prediction.confidence,
                                "temperature": prediction.temperature,
                                "solar_radiation": prediction.solar_radiation,
                                "clouds": prediction.clouds,
                                "sun_elevation": prediction.sun_elevation,
                                "theoretical_max_kwh": prediction.theoretical_max_kwh,
                                **evaluation_flags,
                            }
                        )
                    current_date += timedelta(days=1)
                result["data"]["hourly"] = hourly_results
            except Exception as db_error:
                _LOGGER.error(
                    "Error loading hourly predictions from database: %s", db_error
                )
                result["data"]["hourly"] = []
            try:
                solar_reader = _get_solar_reader()
                hybrid_forecast = (
                    await solar_reader.async_get_operational_hybrid_forecast(
                        target_date=target_date
                    )
                )
                if hybrid_forecast:
                    result["data"]["hybrid"] = {
                        "available": True,
                        "target_date": hybrid_forecast.target_date.isoformat(),
                        "total_kwh": hybrid_forecast.total_kwh,
                        "ops_today_row_kwh": hybrid_forecast.ops_today_row_kwh,
                        "ops_today_row_scope": hybrid_forecast.ops_today_row_scope,
                        "hourly": [
                            {
                                "target_hour": hybrid_hour.target_hour,
                                "target_date": hybrid_hour.target_date.isoformat(),
                                "prediction_kwh": hybrid_hour.prediction_kwh,
                                "snapshot_type": hybrid_hour.snapshot_type,
                                "source_track": hybrid_hour.source_track,
                                "snapshot_sequence": hybrid_hour.snapshot_sequence,
                                "trigger_source": hybrid_hour.trigger_source,
                                "reforecast_cutoff_hour": hybrid_hour.reforecast_cutoff_hour,
                            }
                            for hybrid_hour in hybrid_forecast.hourly
                        ],
                    }
                else:
                    result["data"]["hybrid"] = {"available": False, "hourly": []}
            except Exception as db_error:
                _LOGGER.error(
                    "Error loading operational hybrid forecast from database: %s",
                    db_error,
                )
                result["data"]["hybrid"] = {"available": False, "hourly": []}
            try:
                solar_reader = _get_solar_reader()
                conservative_forecast = (
                    await solar_reader.async_get_conservative_planning_forecast(
                        target_date=target_date
                    )
                )
                if conservative_forecast:
                    result["data"]["conservative"] = {
                        "available": True,
                        "target_date": conservative_forecast.target_date.isoformat(),
                        "total_kwh": conservative_forecast.total_kwh,
                        "updated_at": conservative_forecast.updated_at,
                        "source": "daily_forecast_tracking",
                        "hourly": [
                            {
                                "target_hour": hybrid_hour.target_hour,
                                "target_date": hybrid_hour.target_date.isoformat(),
                                "prediction_kwh": hybrid_hour.prediction_kwh,
                            }
                            for hybrid_hour in conservative_forecast.hourly
                        ],
                    }
                else:
                    result["data"]["conservative"] = {"available": False, "hourly": []}
            except Exception as db_error:
                _LOGGER.error(
                    "Error loading conservative planning forecast from database: %s",
                    db_error,
                )
                result["data"]["conservative"] = {"available": False, "hourly": []}
        actual_weather = await _get_weather_from_db(
            days=requested_days, start_date=range_start_date, end_date=target_date
        )
        if actual_weather:
            result["data"]["weather"] = actual_weather
        forecast_weather = await _get_weather_forecast_from_db(
            days=requested_days, start_date=range_start_date, end_date=target_date
        )
        if forecast_weather:
            result["data"]["weather_corrected"] = forecast_weather
        try:
            solar_reader = _get_solar_reader()
            model_state = await solar_reader.async_get_model_state()
            if model_state:
                result["data"]["ai_state"] = {
                    "model_loaded": model_state.model_loaded,
                    "algorithm_used": model_state.algorithm_used,
                    "training_samples": model_state.training_samples,
                    "current_accuracy": model_state.current_accuracy,
                    "last_training": (
                        model_state.last_training.isoformat()
                        if model_state.last_training
                        else None
                    ),
                    "peak_power_kw": model_state.peak_power_kw,
                    "feature_weights": model_state.feature_weights,
                    "feature_importance": model_state.feature_importance,
                }
        except Exception as db_error:
            _LOGGER.error("Error loading AI model state from database: %s", db_error)
        try:
            solar_reader = _get_solar_reader()
            daily_forecasts = await solar_reader.async_get_daily_forecasts()
            today_forecast = daily_forecasts.get("today")
            tomorrow_forecast = daily_forecasts.get("tomorrow")
            day_after_tomorrow_forecast = daily_forecasts.get("day_after_tomorrow")
            result["data"]["forecasts"] = {
                "today": {
                    "prediction_kwh": (
                        today_forecast.prediction_kwh if today_forecast else None
                    ),
                    "prediction_kwh_display": (
                        f"{today_forecast.prediction_kwh:.2f}"
                        if today_forecast
                        else None
                    ),
                },
                "tomorrow": {
                    "date": (
                        tomorrow_forecast.forecast_date.isoformat()
                        if tomorrow_forecast and tomorrow_forecast.forecast_date
                        else None
                    ),
                    "prediction_kwh": (
                        tomorrow_forecast.prediction_kwh if tomorrow_forecast else None
                    ),
                    "prediction_kwh_display": (
                        f"{tomorrow_forecast.prediction_kwh:.2f}"
                        if tomorrow_forecast
                        else None
                    ),
                },
                "day_after_tomorrow": {
                    "date": (
                        day_after_tomorrow_forecast.forecast_date.isoformat()
                        if day_after_tomorrow_forecast
                        and day_after_tomorrow_forecast.forecast_date
                        else None
                    ),
                    "prediction_kwh": (
                        day_after_tomorrow_forecast.prediction_kwh
                        if day_after_tomorrow_forecast
                        else None
                    ),
                    "prediction_kwh_display": (
                        f"{day_after_tomorrow_forecast.prediction_kwh:.2f}"
                        if day_after_tomorrow_forecast
                        else None
                    ),
                },
            }
        except Exception as db_error:
            _LOGGER.error("Error loading forecasts from database: %s", db_error)
            result["data"]["forecasts"] = {
                "today": {"prediction_kwh": None, "prediction_kwh_display": None},
                "tomorrow": {
                    "date": None,
                    "prediction_kwh": None,
                    "prediction_kwh_display": None,
                },
                "day_after_tomorrow": {
                    "date": None,
                    "prediction_kwh": None,
                    "prediction_kwh_display": None,
                },
            }
        try:
            range_start_key = range_start_date.isoformat()
            async with _get_db() as db:
                async with db.execute(
                    "SELECT cache_date, sunrise, sunset, daylight_hours\n                       FROM astronomy_cache\n                       WHERE cache_date >= ? AND cache_date <= ? AND hour = 12\n                       ORDER BY cache_date",
                    (range_start_key, target_date.isoformat()),
                ) as cursor:
                    astronomy_rows = await cursor.fetchall()
            if astronomy_rows:
                result["data"]["astronomy"] = {
                    record["cache_date"]: {
                        "daylight_hours": record["daylight_hours"],
                        "sunrise": record["sunrise"],
                        "sunset": record["sunset"],
                    }
                    for record in astronomy_rows
                }
        except Exception as db_error:
            _LOGGER.error("Error loading astronomy history from DB: %s", db_error)
        try:
            solar_reader = _get_solar_reader()
            today = ha_today(context.HASS)
            tomorrow = today + timedelta(days=1)
            day_after_tomorrow = today + timedelta(days=2)
            multi_day_hourly = {}
            for target_date in (today, tomorrow, day_after_tomorrow):
                hourly_predictions = await solar_reader.async_get_hourly_predictions(
                    target_date=target_date
                )
                if hourly_predictions:
                    group_hours = []
                    for prediction in hourly_predictions:
                        group_hours.append(
                            {
                                "hour": prediction.target_hour,
                                "prediction_kwh": prediction.prediction_kwh,
                                "actual_kwh": prediction.actual_kwh,
                            }
                        )
                    multi_day_hourly[target_date.isoformat()] = {
                        "date": target_date.isoformat(),
                        "hourly": group_hours,
                        "total_kwh": sum(
                            (
                                prediction.prediction_kwh
                                for prediction in hourly_predictions
                            )
                        ),
                    }
            if multi_day_hourly:
                result["data"]["multi_day_hourly"] = multi_day_hourly
        except Exception as db_error:
            _LOGGER.error(
                "Error loading multi-day hourly forecast from database: %s", db_error
            )
        return web.json_response(result)


class PriceDataView(HomeAssistantView):
    """API for electricity price data. @zara"""

    url = "/api/sfml_stats/prices"
    name = "api:sfml_stats:prices"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return price data. @zara"""
        requested_days = int(request.query.get("days", 7))
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "data": {},
        }
        try:
            history_start_key = (
                ha_today(context.HASS) - timedelta(days=requested_days)
            ).isoformat()
            local_range_start, local_range_end = _get_local_naive_range_for_local_date(
                history_start_key
            )
            async with _get_db() as db:
                async with db.execute(
                    "\n                    SELECT timestamp, hour, price_net, total_price\n                    FROM GPM_price_history\n                    WHERE timestamp >= ?\n                    ORDER BY timestamp\n                ",
                    (local_range_start,),
                ) as cursor:
                    rows = await cursor.fetchall()
            if rows:
                price_history = []
                for row in rows:
                    local_hour_key = _gpm_local_hour_key(row["timestamp"])
                    price_history.append(
                        {
                            "timestamp": row["timestamp"],
                            "date": (
                                local_hour_key[:10]
                                if local_hour_key
                                else str(row["timestamp"])[:10]
                            ),
                            "hour": (
                                int(local_hour_key[11:13])
                                if local_hour_key
                                else row["hour"]
                            ),
                            "price_net": row["price_net"] or 0,
                            "price_total": row["total_price"] or 0,
                        }
                    )
                result["data"]["prices"] = price_history
        except Exception as db_error:
            _LOGGER.error("Error loading prices from DB: %s", db_error)
        return web.json_response(result)


class SummaryDataView(HomeAssistantView):
    """API for aggregated dashboard data. @zara"""

    url = "/api/sfml_stats/summary"
    name = "api:sfml_stats:summary"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return a summary for the dashboard. @zara"""
        empty_today_summary = {
            "production": 0.0,
            "forecast": 0.0,
            "accuracy": 0.0,
            "forecast_accuracy_vs_sot_percent": 0.0,
            "forecast_error_vs_sot_percent": 0.0,
            "forecast_accuracy_vs_actual_percent": 0.0,
            "forecast_error_vs_actual_percent": 0.0,
            "forecast_absolute_error_kwh": 0.0,
            "forecast_relative_error_percent": 0.0,
            "evaluation_coverage_percent": 0.0,
            "excluded_hours_count": 0,
            "excluded_mppt_hours_count": 0,
            "evaluation_hours_count": 0,
            "production_candidate_hours_count": 0,
            "completed_candidate_hours_count": 0,
            "pending_candidate_hours_count": 0,
            "missing_actual_hours_count": 0,
            "excluded_weather_alert_hours_count": 0,
            "excluded_inverter_clipped_hours_count": 0,
            "excluded_reason_breakdown_json": "{}",
            "evaluation_actual_kwh": 0.0,
            "evaluation_predicted_kwh": 0.0,
            "evaluation_cutoff_hour": None,
            "evaluation_scope": None,
            "live_day_yield_delta_vs_forecast_percent": 0.0,
            "live_day_forecast_error_vs_sot_percent": 0.0,
            "live_day_forecast_accuracy_vs_sot_percent": 0.0,
            "peak_hour": None,
            "peak_kwh": 0.0,
            "forecast_tomorrow": 0.0,
        }
        empty_week_summary = {
            "total_production": 0.0,
            "total_forecast": 0.0,
            "avg_accuracy": 0.0,
            "avg_forecast_error_vs_sot_percent": 0.0,
            "avg_forecast_error_vs_actual_percent": 0.0,
            "avg_evaluation_coverage_percent": 0.0,
            "avg_excluded_mppt_hours": 0.0,
            "days_count": 0,
        }
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "kpis": {},
            "today": empty_today_summary.copy(),
            "week": empty_week_summary.copy(),
        }
        today = ha_today(context.HASS)
        week_start_date = today - timedelta(days=7)
        config = _get_config()
        try:
            solar_reader = _get_solar_reader()
            daily_summary_rows = await solar_reader.async_get_daily_summaries(
                start_date=week_start_date, end_date=today
            )
            async with _get_db() as db:
                async with db.execute(
                    "SELECT hp.target_date,\n                              hp.target_hour,\n                              hp.prediction_kwh AS prediction_kwh,\n                              hp.actual_kwh,\n                              hp.mppt_throttled,\n                              hp.mppt_throttle_reason,\n                              hp.inverter_clipped,\n                              hp.exclude_from_learning,\n                              hp.exclude_from_clean_evaluation,\n                              hp.has_weather_alert,\n                              hp.weather_alert_type\n                       FROM hourly_predictions hp\n                       WHERE hp.target_date BETWEEN ? AND ?\n                       ORDER BY hp.target_date, hp.target_hour",
                    (week_start_date.isoformat(), today.isoformat()),
                ) as cursor:
                    week_hourly_rows = await cursor.fetchall()
            summaries_by_day, week_summary = _build_summary_week_from_hourly_rows(
                week_hourly_rows
            )
            today_daily_summary = next(
                (
                    daily_summary
                    for daily_summary in daily_summary_rows
                    if daily_summary.date == today
                ),
                None,
            )
            today_hourly_summary = summaries_by_day.get(today.isoformat())
            if today_hourly_summary:
                result["today"] = today_hourly_summary
            elif today_daily_summary:
                result["today"] = {
                    "production": today_daily_summary.actual_total_kwh,
                    "forecast": today_daily_summary.predicted_total_kwh,
                    "accuracy": today_daily_summary.accuracy_percent,
                    **_build_daily_summary_forecast_metrics(
                        today_daily_summary.predicted_total_kwh,
                        today_daily_summary.actual_total_kwh,
                        today_daily_summary.evaluation_predicted_kwh,
                        today_daily_summary.evaluation_actual_kwh,
                        today_daily_summary.accuracy_percent,
                    ),
                    "evaluation_coverage_percent": today_daily_summary.evaluation_coverage_percent,
                    "excluded_hours_count": today_daily_summary.excluded_hours_count,
                    "excluded_mppt_hours_count": today_daily_summary.excluded_mppt_hours_count,
                    "evaluation_hours_count": today_daily_summary.evaluation_hours_count,
                    "production_candidate_hours_count": today_daily_summary.production_candidate_hours_count,
                    "missing_actual_hours_count": today_daily_summary.missing_actual_hours_count,
                    "excluded_weather_alert_hours_count": today_daily_summary.excluded_weather_alert_hours_count,
                    "excluded_inverter_clipped_hours_count": today_daily_summary.excluded_inverter_clipped_hours_count,
                    "excluded_reason_breakdown_json": today_daily_summary.excluded_reason_breakdown_json,
                    "evaluation_actual_kwh": today_daily_summary.evaluation_actual_kwh,
                    "evaluation_predicted_kwh": today_daily_summary.evaluation_predicted_kwh,
                    "peak_hour": today_daily_summary.peak_hour,
                    "peak_kwh": today_daily_summary.peak_kwh,
                }
            if week_summary:
                result["week"] = week_summary
            elif daily_summary_rows:
                daily_accuracy_values = [
                    _build_daily_summary_forecast_metrics(
                        daily_summary.predicted_total_kwh,
                        daily_summary.actual_total_kwh,
                        daily_summary.evaluation_predicted_kwh,
                        daily_summary.evaluation_actual_kwh,
                        daily_summary.accuracy_percent,
                    )["forecast_accuracy_vs_sot_percent"]
                    for daily_summary in daily_summary_rows
                ]
                daily_sot_error_values = [
                    _build_daily_summary_forecast_metrics(
                        daily_summary.predicted_total_kwh,
                        daily_summary.actual_total_kwh,
                        daily_summary.evaluation_predicted_kwh,
                        daily_summary.evaluation_actual_kwh,
                        daily_summary.accuracy_percent,
                    )["forecast_error_vs_sot_percent"]
                    for daily_summary in daily_summary_rows
                ]
                daily_actual_error_values = [
                    _build_daily_summary_forecast_metrics(
                        daily_summary.predicted_total_kwh,
                        daily_summary.actual_total_kwh,
                        daily_summary.evaluation_predicted_kwh,
                        daily_summary.evaluation_actual_kwh,
                        daily_summary.accuracy_percent,
                    )["forecast_error_vs_actual_percent"]
                    for daily_summary in daily_summary_rows
                ]
                average_accuracy_percent = _average_present(daily_accuracy_values)
                average_evaluation_coverage_percent = _average_present(
                    [
                        daily_summary.evaluation_coverage_percent
                        for daily_summary in daily_summary_rows
                    ]
                )
                result["week"] = {
                    "total_production": sum(
                        (
                            daily_summary.actual_total_kwh
                            for daily_summary in daily_summary_rows
                        )
                    ),
                    "total_forecast": sum(
                        (
                            daily_summary.predicted_total_kwh
                            for daily_summary in daily_summary_rows
                        )
                    ),
                    "avg_accuracy": average_accuracy_percent,
                    "avg_forecast_error_vs_sot_percent": _average_present(
                        daily_sot_error_values
                    ),
                    "avg_forecast_error_vs_actual_percent": _average_present(
                        daily_actual_error_values
                    ),
                    "avg_evaluation_coverage_percent": average_evaluation_coverage_percent,
                    "avg_excluded_mppt_hours": sum(
                        (
                            daily_summary.excluded_mppt_hours_count
                            for daily_summary in daily_summary_rows
                        )
                    )
                    / len(daily_summary_rows),
                    "days_count": len(daily_summary_rows),
                }
        except Exception as db_error:
            _LOGGER.error("Error loading daily summaries from database: %s", db_error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT date, predicted_total_kwh, actual_total_kwh,\n                              accuracy_percent, evaluation_coverage_percent,\n                              excluded_mppt_hours_count\n                       FROM daily_summaries\n                       WHERE date < ?\n                       ORDER BY date DESC\n                       LIMIT 30",
                    (today.isoformat(),),
                ) as cursor:
                    daily_history_rows = await cursor.fetchall()
            statistics_history = [
                {
                    "date": row["date"],
                    "predicted_kwh": row["predicted_total_kwh"] or 0.0,
                    "actual_kwh": row["actual_total_kwh"] or 0.0,
                    "accuracy": row["accuracy_percent"],
                    "forecast_accuracy_vs_sot_percent": row["accuracy_percent"],
                    "forecast_accuracy_vs_actual_percent": row["accuracy_percent"],
                    "evaluation_coverage_percent": row["evaluation_coverage_percent"],
                    "excluded_mppt_hours_count": row["excluded_mppt_hours_count"] or 0,
                }
                for row in daily_history_rows
            ]
            result["statistics"] = {}
            statistics_7d = _build_statistics_window_from_history_rows(
                statistics_history, 7
            )
            if statistics_7d is not None:
                result["statistics"]["last_7_days"] = statistics_7d
            statistics_30d = _build_statistics_window_from_history_rows(
                statistics_history, 30
            )
            if statistics_30d is not None:
                result["statistics"]["last_30_days"] = statistics_30d
        except Exception as db_error:
            _LOGGER.debug("Could not load summary rolling statistics: %s", db_error)
            result["statistics"] = {}
        try:
            result["panel_groups"] = await StatisticsView()._get_panel_group_data()
        except Exception as db_error:
            _LOGGER.debug("Could not load summary panel groups: %s", db_error)
            result["panel_groups"] = {"available": False, "groups": {}}
        try:
            result["outdoor_temperature"] = await _get_summary_outdoor_temperature(
                config
            )
        except Exception as db_error:
            _LOGGER.debug("Could not load outdoor temperature summary: %s", db_error)
            result["outdoor_temperature"] = {"temperature_c": None, "source": None}
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT total_price, average_today\n                       FROM GPM_current_price WHERE id = 1"
                ) as cursor:
                    current_price_row = await cursor.fetchone()
                if current_price_row and current_price_row[0] is not None:
                    result["kpis"]["price_current"] = current_price_row[0]
                    if current_price_row[1] is not None:
                        result["kpis"]["price_avg"] = round(current_price_row[1], 2)
                price_history_start = (
                    (ha_now(context.HASS) - timedelta(hours=48))
                    .replace(tzinfo=None)
                    .isoformat(timespec="seconds")
                )
                async with db.execute(
                    "\n                    SELECT price_net, total_price\n                    FROM GPM_price_history\n                    WHERE timestamp >= ?\n                    ORDER BY timestamp\n                ",
                    (price_history_start,),
                ) as cursor:
                    price_history_rows = await cursor.fetchall()
            if price_history_rows:
                total_prices = [
                    record["total_price"]
                    for record in price_history_rows
                    if record["total_price"]
                ]
                if total_prices:
                    if "price_current" not in result["kpis"]:
                        result["kpis"]["price_current"] = total_prices[-1]
                    if "price_avg" not in result["kpis"]:
                        result["kpis"]["price_avg"] = round(
                            sum(total_prices) / len(total_prices), 2
                        )
                    result["kpis"]["price_min"] = min(total_prices)
                    result["kpis"]["price_max"] = max(total_prices)
        except Exception as db_error:
            _LOGGER.error("Error loading price KPIs from DB: %s", db_error)
        try:
            solar_reader = _get_solar_reader()
            model_state = await solar_reader.async_get_model_state()
            if model_state:
                result["kpis"]["ai_training_samples"] = model_state.training_samples
        except Exception as db_error:
            _LOGGER.error("Error loading AI model state from database: %s", db_error)

        def extract_time(iso_string: str | None) -> str | None:
            """Extract HH:MM from ISO string. @zara"""
            if not iso_string:
                return
            try:
                if "T" in iso_string:
                    time_part = iso_string.split("T")[1]
                    return time_part[:5]
                if " " in iso_string:
                    time_part = iso_string.split(" ")[1]
                    return time_part[:5]
                return iso_string[:5]
            except Exception:
                return

        today_key = ha_today(context.HASS).isoformat()
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT sunrise, sunset, daylight_hours\n                       FROM astronomy_cache\n                       WHERE cache_date = ? AND hour = 12",
                    (today_key,),
                ) as cursor:
                    astronomy_row = await cursor.fetchone()
            if astronomy_row:
                sunrise_time = extract_time(astronomy_row["sunrise"])
                sunset_time = extract_time(astronomy_row["sunset"])
                result["sun_times"] = {"sunrise": sunrise_time, "sunset": sunset_time}
                if sunrise_time and sunset_time:
                    result["production_time"] = {
                        "active": True,
                        "start_time": sunrise_time,
                        "end_time": sunset_time,
                        "duration_seconds": int(
                            (astronomy_row["daylight_hours"] or 0) * 3600
                        ),
                    }
            else:
                sun_times_fallback = _today_sun_times_from_ha()
                if sun_times_fallback:
                    result.update(sun_times_fallback)
        except Exception as db_error:
            _LOGGER.error("Error loading astronomy data from DB: %s", db_error)
            sun_times_fallback = _today_sun_times_from_ha()
            if sun_times_fallback:
                result.update(sun_times_fallback)
        try:
            solar_reader = _get_solar_reader()
            hourly_predictions = await solar_reader.async_get_hourly_predictions(
                target_date=today
            )
            if hourly_predictions:
                result["hourly_predictions"] = [
                    {
                        "hour": prediction.target_hour,
                        "predicted": (
                            round(prediction.prediction_kwh, 3)
                            if prediction.prediction_kwh
                            else 0
                        ),
                        "actual": (
                            round(prediction.actual_kwh, 3)
                            if prediction.actual_kwh
                            else 0
                        ),
                    }
                    for prediction in hourly_predictions
                ]
        except Exception as db_error:
            _LOGGER.debug("Error loading hourly predictions: %s", db_error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT forecast_type, prediction_kwh, forecast_date\n                       FROM daily_forecasts\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date",
                    (ha_today(context.HASS).isoformat(),),
                ) as cursor:
                    forecasts_by_type = {}
                    for row in await cursor.fetchall():
                        forecasts_by_type[row[0]] = {
                            "kwh": round(row[1], 1),
                            "date": row[2],
                        }
                    result["daily_forecasts"] = forecasts_by_type
                    if "tomorrow" in forecasts_by_type:
                        result["today"]["forecast_tomorrow"] = forecasts_by_type[
                            "tomorrow"
                        ]["kwh"]
        except Exception as db_error:
            _LOGGER.debug("Error loading daily forecasts: %s", db_error)
        try:
            config = {}
            for coordinator in context.HASS.data.get(DOMAIN, {}).values():
                if not not isinstance(coordinator, dict) and (
                    not not "config" in coordinator
                ):
                    config = coordinator["config"]
                    break
            configured_price_mode = effective_price_mode(context.HASS, config)
            if configured_price_mode == MODE_LEGACY_FIXED:
                result["kpis"]["price_current"] = _get_fixed_total_price(config)
        except Exception:
            pass
        try:
            async with _get_db() as db:
                energy_columns = await _table_columns(db, "stats_daily_energy")
                energy_metric_columns = (
                    "solar_yield_kwh",
                    "home_consumption_kwh",
                    "grid_import_kwh",
                    "grid_export_kwh",
                    "battery_to_house_kwh",
                    "solar_to_house_kwh",
                    "self_consumption_kwh",
                    "autarkie_percent",
                    "peak_solar_w",
                    "peak_solar_time",
                    "total_cost_eur",
                    "feed_in_revenue_eur",
                    "savings_eur",
                    "solar_to_battery_kwh",
                    "grid_to_house_kwh",
                    "grid_to_battery_kwh",
                )
                energy_select = ", ".join(
                    (
                        _column_expr(energy_columns, column)
                        for column in energy_metric_columns
                    )
                )
                async with db.execute(
                    f"SELECT {energy_select}\n                       FROM stats_daily_energy WHERE date = ?",
                    (today_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        today_corrections = await _energy_correction_projection(
                            today_key
                        )
                        result["daily_stats"] = {
                            "solar_yield_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "solar_yield_day_kwh",
                                    row[0],
                                ),
                                2,
                            ),
                            "home_consumption_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "home_consumption_day_kwh",
                                    row[1],
                                ),
                                2,
                            ),
                            "grid_import_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "grid_import_day_kwh",
                                    row[2],
                                ),
                                2,
                            ),
                            "grid_export_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "grid_export_day_kwh",
                                    row[3],
                                ),
                                2,
                            ),
                            "battery_to_house_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "battery_to_house_day_kwh",
                                    row[4],
                                ),
                                2,
                            ),
                            "solar_to_house_kwh": round(
                                corrected_value(
                                    today_corrections,
                                    today_key,
                                    "solar_to_house_day_kwh",
                                    row[5],
                                ),
                                2,
                            ),
                            "self_consumption_kwh": round(row[6] or 0, 2),
                            "autarkie_percent": round(row[7] or 0, 1),
                            "peak_solar_w": round(row[8] or 0, 0),
                            "peak_solar_time": row[9] or "",
                            "total_cost_eur": round(row[10] or 0, 2),
                            "feed_in_revenue_eur": round(row[11] or 0, 2),
                            "savings_eur": round(row[12] or 0, 2),
                            "solar_to_battery_kwh": round(row[13] or 0, 2),
                            "grid_to_house_kwh": round(row[14] or 0, 2),
                            "grid_to_battery_kwh": round(row[15] or 0, 2),
                        }
        except Exception as db_error:
            _LOGGER.debug("Error loading daily stats: %s", db_error)
        try:
            sfml_reader = SFMLDataReader(context.HASS)
            live_yield_kwh = await _get_today_yield_from_db()
            if live_yield_kwh is None:
                live_yield_kwh = await sfml_reader.get_live_yield()
            if live_yield_kwh is not None:
                daily_stats = result.setdefault("daily_stats", {})
                daily_stats["solar_yield_kwh"] = round(live_yield_kwh, 2)
                daily_stats["solar_yield_source"] = "sfml_live"
        except Exception as db_error:
            _LOGGER.debug("Error loading SFML live yield for summary: %s", db_error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT ppg.group_name,\n                              SUM(ppg.prediction_kwh) as pred,\n                              SUM(ppg.actual_kwh) as actual\n                       FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date = ?\n                       GROUP BY ppg.group_name",
                    (today_key,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    if rows:
                        result["panel_groups_daily"] = [
                            {
                                "name": record[0],
                                "predicted_kwh": round(record[1] or 0, 2),
                                "actual_kwh": round(record[2] or 0, 2),
                            }
                            for record in rows
                        ]
        except Exception as db_error:
            _LOGGER.debug("Error loading panel group totals: %s", db_error)
        try:
            from ..core.dev_mode import is_dev_active as is_dev_active

            if is_dev_active(context.HASS):
                async with _get_db() as db:
                    async with db.execute(
                        "SELECT active_model, training_samples, accuracy, rmse, last_trained FROM ai_learned_weights_meta LIMIT 1"
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            result["ai_status"] = {
                                "model": row[0],
                                "training_samples": row[1],
                                "accuracy": round((row[2] or 0) * 100, 1),
                                "rmse": round(row[3] or 0, 4),
                                "last_trained": row[4],
                            }
        except Exception as db_error:
            _LOGGER.debug("Error loading AI status: %s", db_error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT SUM(theoretical_max_kwh) FROM astronomy_cache WHERE cache_date = ?",
                    (today_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        result["theoretical_max_kwh"] = round(row[0], 2)
        except Exception as db_error:
            _LOGGER.debug("Error loading theoretical max: %s", db_error)
        try:
            historical_corrections = await _energy_correction_projection("2020-01-01")
            corrected_energy_by_day = await _corrected_daily_energy_values("2020-01-01")
            monthly_solar_adjustments = {}
            for day_key, energy_values in historical_corrections.items():
                if (
                    "solar_yield_day_kwh" not in energy_values
                    or day_key not in corrected_energy_by_day
                ):
                    continue
                month_key = day_key[:7]
                corrected_day = corrected_energy_by_day[day_key]
                monthly_solar_adjustments[month_key] = (
                    monthly_solar_adjustments.get(month_key, 0.0)
                    + corrected_day["solar_yield_day_kwh"]
                    - corrected_day["raw_solar_yield_kwh"]
                )
            async with _get_db() as db:
                async with db.execute(
                    "SELECT strftime('%Y', date) as year,\n                              strftime('%m', date) as month,\n                              SUM(actual_total_kwh) as total_kwh,\n                              COUNT(*) as days\n                       FROM daily_summaries\n                       GROUP BY year, month\n                       ORDER BY year, month"
                ) as cursor:
                    rows = await cursor.fetchall()
                    if rows:
                        result["monthly_by_year"] = [
                            {
                                "year": int(record[0]),
                                "month": int(record[1]),
                                "total_kwh": round(
                                    (record[2] or 0)
                                    + monthly_solar_adjustments.get(
                                        f"{int(record[0]):04d}-{int(record[1]):02d}",
                                        0.0,
                                    ),
                                    2,
                                ),
                                "days": record[3],
                            }
                            for record in rows
                        ]
        except Exception as db_error:
            _LOGGER.debug("Error loading monthly by year: %s", db_error)
        try:
            async with _get_db() as db:
                billing_config = {}
                monthly_tariff_manager = None
                for integration_data in context.HASS.data.get(DOMAIN, {}).values():
                    if not isinstance(integration_data, dict):
                        continue
                    if (
                        monthly_tariff_manager is None
                        and "monthly_tariff_manager" in integration_data
                    ):
                        monthly_tariff_manager = integration_data[
                            "monthly_tariff_manager"
                        ]
                    if not billing_config and "config" in integration_data:
                        billing_config = integration_data["config"]
                billing_start_date, billing_end_date = calculate_billing_period(
                    ha_today(context.HASS),
                    billing_config.get(
                        CONF_BILLING_START_MONTH, DEFAULT_BILLING_START_MONTH
                    ),
                    billing_config.get(
                        CONF_BILLING_START_DAY, DEFAULT_BILLING_START_DAY
                    ),
                )
                billing_start_key = billing_start_date.isoformat()
                billing_end_key = min(
                    billing_end_date, ha_today(context.HASS)
                ).isoformat()
                corrected_energy_by_day_0000 = await _corrected_daily_energy_values(
                    billing_start_key, billing_end_key
                )
                correction_projection = await _energy_correction_projection(
                    billing_start_key
                )
                async with db.execute(
                    "SELECT strftime('%Y-%m', date) as month,\n                              SUM(home_consumption_kwh) as consumption,\n                              SUM(solar_yield_kwh) as solar,\n                              SUM(self_consumption_kwh) as self_cons,\n                              SUM(solar_to_house_kwh) as solar_to_house,\n                              SUM(battery_to_house_kwh) as bat_to_house,\n                              SUM(grid_to_house_kwh) as grid_to_house,\n                              SUM(smartmeter_import_kwh) as sm_import,\n                              SUM(grid_export_kwh) as grid_export,\n                              SUM(smartmeter_export_kwh) as sm_export,\n                              SUM(home_consumption_kwh) as total_consumption_for_autarkie,\n                              COUNT(*) as days,\n                              SUM(grid_to_battery_kwh) as grid_to_battery\n                       FROM stats_daily_energy\n                       WHERE date >= ? AND date <= ?\n                       GROUP BY month\n                       ORDER BY month",
                    (billing_start_key, billing_end_key),
                ) as cursor:
                    monthly_energy_rows = await cursor.fetchall()
                try:
                    base_fee_eur = float(
                        billing_config.get(CONF_BILLING_BASE_FEE, 0) or 0
                    )
                except (TypeError, ValueError):
                    base_fee_eur = 0.0
                try:
                    feed_in_tariff_ct = float(
                        billing_config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF)
                        or 0
                    )
                except (TypeError, ValueError):
                    feed_in_tariff_ct = DEFAULT_FEED_IN_TARIFF
                feed_in_tariff_ct = max(0.0, feed_in_tariff_ct)
                try:
                    fallback_import_price_ct = float(
                        _get_fixed_total_price(billing_config)
                    )
                except (TypeError, ValueError):
                    fallback_import_price_ct = (
                        DEFAULT_BILLING_WORK_PRICE + DEFAULT_BILLING_GRID_FEES
                    )
                price_mode = effective_price_mode(context.HASS, billing_config)
                billing_by_month = {}
                try:
                    hourly_billing_columns = await _table_columns(
                        db, "stats_hourly_billing"
                    )
                    self_consumption_price_expr = (
                        "CASE WHEN COALESCE(price_ct_kwh, 0) > 0 THEN price_ct_kwh ELSE ? END"
                        if "price_ct_kwh" in hourly_billing_columns
                        else "?"
                    )
                    if "price_ct_kwh" in hourly_billing_columns:
                        priced_cost_expr = "SUM(CASE WHEN price_ct_kwh IS NOT NULL AND CAST(price_ct_kwh AS REAL) != 0 THEN COALESCE(grid_import_cost_ct, COALESCE(grid_import_kwh, 0) * price_ct_kwh, 0) ELSE 0 END) / 100.0"
                        priced_import_expr = "SUM(CASE WHEN price_ct_kwh IS NOT NULL AND CAST(price_ct_kwh AS REAL) != 0 THEN COALESCE(grid_import_kwh, 0) ELSE 0 END)"
                    else:
                        priced_cost_expr = "0"
                        priced_import_expr = "0"
                    async with db.execute(
                        f"SELECT strftime('%Y-%m', date) as month,\n                                  SUM(grid_import_cost_ct) / 100.0 as cost_eur,\n                                  SUM(grid_import_kwh) as import_kwh,\n                                  SUM(grid_export_kwh) as export_kwh,\n                                  SUM(COALESCE(feed_in_revenue_ct, 0)) / 100.0 as feed_in_revenue_eur,\n                                  SUM(\n                                      (\n                                          COALESCE(solar_to_house_kwh, 0)\n                                          + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                      )\n                                      * {self_consumption_price_expr}\n                                  ) / 100.0 as self_savings_eur,\n                                  SUM(\n                                      COALESCE(solar_to_house_kwh, 0)\n                                      + MAX(0, COALESCE(battery_to_house_kwh, 0) - COALESCE(grid_to_battery_kwh, 0))\n                                  ) as self_consumption_kwh,\n                                  COUNT(*) as hours,\n                                  {priced_cost_expr} as priced_cost_eur,\n                                  {priced_import_expr} as priced_import_kwh\n                           FROM stats_hourly_billing\n                           WHERE date >= ? AND date <= ?\n                           GROUP BY month\n                           ORDER BY month",
                        (fallback_import_price_ct, billing_start_key, billing_end_key),
                    ) as cursor:
                        for record in await cursor.fetchall():
                            if not not record[0] and (not not record[7] > 0):
                                billing_by_month[record[0]] = {
                                    "cost_eur": round(record[1] or 0, 2),
                                    "import_kwh": round(record[2] or 0, 4),
                                    "export_kwh": round(record[3] or 0, 4),
                                    "feed_in_revenue_eur": round(record[4] or 0, 2),
                                    "self_savings_eur": round(record[5] or 0, 2),
                                    "self_consumption_kwh": round(record[6] or 0, 4),
                                    "hours": record[7],
                                    "priced_cost_eur": (
                                        0.0 if record[8] is None else record[8]
                                    ),
                                    "priced_import_kwh": (
                                        0.0 if record[9] is None else record[9]
                                    ),
                                }
                except Exception:
                    pass
                try:
                    pass
                except Exception:
                    pass
                hourly_component_days = None
                if any(
                    (
                        energy_flows_changed(energy_values)
                        for energy_values in corrected_energy_by_day_0000.values()
                    )
                ):
                    try:
                        hourly_component_days = await load_hourly_component_days(
                            db,
                            billing_start_key,
                            billing_end_key,
                            fallback_import_price_ct,
                        )
                    except Exception:
                        _LOGGER.debug("Hourly correction base unavailable")
                        hourly_component_days = None
                import_split_by_month = {}
                try:
                    from ..core.grid_import_split import (
                        battery_is_configured as battery_is_configured,
                        load_split_hours as load_split_hours,
                        summarize_import_split as summarize_import_split,
                    )

                    if battery_is_configured(billing_config):
                        import_split = await load_split_hours(
                            db, billing_start_key, billing_end_key
                        )
                        split_hours_by_month = {}
                        for split_hour in import_split.hours:
                            split_hours_by_month.setdefault(
                                str(split_hour["date"])[:7], []
                            ).append(split_hour)
                        for split_month, split_hours in split_hours_by_month.items():
                            import_split_by_month[split_month] = summarize_import_split(
                                split_hours,
                                corrected_energy_by_day_0000,
                                correction_projection,
                                fallback_price_ct=fallback_import_price_ct,
                                house_column=import_split.house_column,
                                battery_column=import_split.battery_column,
                                backfill_done=import_split.backfill_done,
                            )
                except Exception:
                    _LOGGER.debug("Monthly import split unavailable")
                if monthly_energy_rows:
                    result["monthly_energy"] = []
                    for record in monthly_energy_rows:
                        month_key = record[0]
                        grid_import_kwh = (
                            round(record[7] or 0, 1)
                            if (record[7] or 0) > 0
                            else round((record[6] or 0) + (record[12] or 0), 1)
                        )
                        grid_export_kwh = (
                            round(record[9] or 0, 1)
                            if (record[9] or 0) > 0
                            else round(record[8] or 0, 1)
                        )
                        corrected_month_days = [
                            energy_values
                            for day_key, energy_values in corrected_energy_by_day_0000.items()
                            if day_key.startswith(f"{month_key}-")
                            if day_key <= billing_end_key
                        ]
                        solar_kwh = corrected_month_sum(
                            corrected_month_days,
                            "solar_yield_day_kwh",
                            record[2] or 0.0,
                        )
                        home_consumption_kwh = corrected_month_sum(
                            corrected_month_days,
                            "home_consumption_day_kwh",
                            record[1] or 0.0,
                        )
                        solar_to_house_kwh = corrected_month_sum(
                            corrected_month_days,
                            "solar_to_house_day_kwh",
                            record[4] or 0.0,
                        )
                        battery_to_house_kwh = corrected_month_sum(
                            corrected_month_days,
                            "battery_to_house_day_kwh",
                            record[5] or 0.0,
                        )
                        grid_to_house_kwh = corrected_month_sum(
                            corrected_month_days,
                            "grid_to_house_day_kwh",
                            record[6] or 0.0,
                        )
                        grid_to_battery_kwh = corrected_month_sum(
                            corrected_month_days,
                            "grid_to_battery_day_kwh",
                            record[12] or 0.0,
                        )
                        if (
                            corrected_month_days
                            and "grid_import_day_kwh" in corrected_month_days[0]
                        ):
                            grid_import_kwh = sum(
                                (
                                    energy_values["grid_import_day_kwh"]
                                    for energy_values in corrected_month_days
                                )
                            )
                            grid_export_kwh = sum(
                                (
                                    energy_values["grid_export_day_kwh"]
                                    for energy_values in corrected_month_days
                                )
                            )
                        has_energy_corrections = any(
                            (
                                day_key.startswith(f"{month_key}-")
                                and bool(energy_values)
                                for day_key, energy_values in correction_projection.items()
                            )
                        )
                        has_grid_corrections = any(
                            (
                                day_key.startswith(f"{month_key}-")
                                and bool(
                                    {"grid_import_day_kwh", "grid_export_day_kwh"}
                                    & energy_values.keys()
                                )
                                for day_key, energy_values in correction_projection.items()
                            )
                        )
                        billing_month = billing_by_month.get(month_key)
                        year, month = (
                            int(month_component)
                            for month_component in month_key.split("-")
                        )
                        days_in_month = calendar.monthrange(year, month)[1]
                        measured_days = min(record[11] or 0, days_in_month)
                        month_import_price_ct = fallback_import_price_ct
                        month_feed_in_tariff_ct = feed_in_tariff_ct
                        month_base_fee_eur = base_fee_eur
                        tariff_source = "config_fallback"
                        if monthly_tariff_manager is not None:
                            try:
                                monthly_fallbacks = (
                                    await monthly_tariff_manager.get_monthly_fallbacks(
                                        year,
                                        month,
                                        import_price_ct=fallback_import_price_ct,
                                        feed_in_tariff_ct=feed_in_tariff_ct,
                                        base_fee_eur=base_fee_eur,
                                    )
                                )
                                import_price_fallback = monthly_fallbacks[
                                    "import_price_ct"
                                ]
                                feed_in_tariff_fallback = monthly_fallbacks[
                                    "feed_in_tariff_ct"
                                ]
                                base_fee_fallback = monthly_fallbacks["base_fee_eur"]
                                month_import_price_ct = float(
                                    import_price_fallback["value"]
                                    if import_price_fallback["value"] is not None
                                    else fallback_import_price_ct
                                )
                                month_feed_in_tariff_ct = float(
                                    feed_in_tariff_fallback["value"]
                                    or feed_in_tariff_ct
                                )
                                month_base_fee_eur = float(
                                    base_fee_fallback["value"] or base_fee_eur
                                )
                                tariff_source = import_price_fallback.get(
                                    "source", "fixed_month"
                                )
                            except Exception as error:
                                _LOGGER.debug(
                                    "Monthly tariff fallback failed for %s: %s",
                                    month_key,
                                    error,
                                )
                        battery_self_consumption_kwh = max(
                            0.0, battery_to_house_kwh - grid_to_battery_kwh
                        )
                        self_consumption_kwh = (
                            solar_to_house_kwh + battery_self_consumption_kwh
                        )
                        unpriced_import_kwh = 0.0
                        if is_fixed_like(price_mode):
                            month_last_date = date(year, month, days_in_month)
                            billing_end_date_309 = date.fromisoformat(billing_end_key)
                            month_cost_end_key = min(
                                month_last_date, billing_end_date_309
                            ).isoformat()
                            monthly_costs = await monthly_table_energy_cost(
                                db,
                                f"{month_key}-01",
                                month_cost_end_key,
                                correction_projection,
                                month_import_price_ct,
                                price_mode=price_mode,
                                config=billing_config,
                            )
                            energy_cost_eur = monthly_costs["energy_eur"]
                            average_price_ct = (
                                monthly_costs["avg_ct"] or month_import_price_ct
                            )
                            unpriced_import_kwh = float(
                                monthly_costs.get("unpriced_kwh") or 0.0
                            )
                            self_consumption_savings_eur = (
                                self_consumption_kwh * average_price_ct / 100
                            )
                            feed_in_revenue_eur = (
                                grid_export_kwh * month_feed_in_tariff_ct / 100
                            )
                            cost_source = monthly_costs["cost_source"]
                        elif billing_month:
                            month_components = [
                                (day_key, energy_values)
                                for day_key, energy_values in corrected_energy_by_day_0000.items()
                                if day_key.startswith(f"{month_key}-")
                                if day_key <= billing_end_key
                            ]
                            energy_flows_corrected = any(
                                (
                                    energy_flows_changed(energy_values)
                                    for unused_day_key, energy_values in month_components
                                )
                            )
                            legacy_costs = legacy_dynamic_month(
                                import_kwh=grid_import_kwh,
                                covered_import_kwh=billing_month["import_kwh"],
                                covered_import_cost=billing_month["cost_eur"],
                                self_kwh=self_consumption_kwh,
                                covered_self_kwh=billing_month["self_consumption_kwh"],
                                covered_self_savings=billing_month["self_savings_eur"],
                                export_kwh=grid_export_kwh,
                                covered_export_kwh=billing_month["export_kwh"],
                                covered_export_revenue=billing_month[
                                    "feed_in_revenue_eur"
                                ],
                                month_price_ct=month_import_price_ct,
                                month_feed_in_ct=month_feed_in_tariff_ct,
                            )
                            if (
                                energy_flows_corrected
                                and hourly_component_days is not None
                            ):
                                dynamic_costs = dynamic_month_costs(
                                    [
                                        component_day(
                                            energy_values,
                                            hourly_component_days.get(day_key),
                                        )
                                        for day_key, energy_values in month_components
                                    ],
                                    changed=True,
                                    legacy=legacy_costs,
                                    month_price_ct=month_import_price_ct,
                                    month_feed_in_ct=month_feed_in_tariff_ct,
                                )
                            else:
                                dynamic_costs = legacy_costs
                            (
                                dynamic_average_price_ct,
                                priced_import_cost_eur,
                                unpriced_import_kwh,
                            ) = dynamic_month_average(
                                priced_cost_eur=billing_month.get("priced_cost_eur")
                                or 0.0,
                                priced_import_kwh=billing_month.get("priced_import_kwh")
                                or 0.0,
                                total_import_kwh=grid_import_kwh,
                            )
                            energy_cost_eur = priced_import_cost_eur
                            self_consumption_savings_eur = dynamic_costs["self_savings"]
                            feed_in_revenue_eur = dynamic_costs["feed_in_revenue"]
                            average_price_ct = dynamic_average_price_ct
                            cost_source = (
                                "dynamic" if unpriced_import_kwh <= 0.001 else "hybrid"
                            )
                        else:
                            average_price_ct = None
                            energy_cost_eur = 0.0
                            unpriced_import_kwh = float(grid_import_kwh)
                            self_consumption_savings_eur = 0.0
                            feed_in_revenue_eur = (
                                grid_export_kwh * month_feed_in_tariff_ct / 100
                            )
                            cost_source = "estimated"
                        prorated_base_fee_eur = (
                            month_base_fee_eur * measured_days / days_in_month
                            if days_in_month > 0
                            else month_base_fee_eur
                        )
                        total_savings_eur = (
                            self_consumption_savings_eur + feed_in_revenue_eur
                        )
                        total_cost_eur = round(
                            energy_cost_eur + prorated_base_fee_eur, 2
                        )
                        result["monthly_energy"].append(
                            {
                                "month": month_key,
                                "consumption_kwh": round(home_consumption_kwh, 1),
                                "solar_kwh": round(solar_kwh, 1),
                                "self_consumption_kwh": round(self_consumption_kwh, 1),
                                "solar_to_house_kwh": round(solar_to_house_kwh, 1),
                                "battery_to_house_kwh": round(battery_to_house_kwh, 1),
                                "grid_to_house_kwh": round(grid_to_house_kwh, 1),
                                "grid_to_battery_kwh": round(grid_to_battery_kwh, 1),
                                "grid_import_kwh": grid_import_kwh,
                                "grid_export_kwh": grid_export_kwh,
                                "autarkie_percent": (
                                    round(
                                        min(
                                            100,
                                            self_consumption_kwh
                                            / home_consumption_kwh
                                            * 100,
                                        ),
                                        1,
                                    )
                                    if home_consumption_kwh > 0
                                    else 0
                                ),
                                "days": record[11],
                                "cost_eur": total_cost_eur,
                                "energy_cost_eur": round(energy_cost_eur, 2),
                                "base_fee_eur": round(prorated_base_fee_eur, 2),
                                "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
                                "self_consumption_savings_eur": round(
                                    self_consumption_savings_eur, 2
                                ),
                                "savings_eur": round(total_savings_eur, 2),
                                "total_savings_eur": round(total_savings_eur, 2),
                                "avg_price_ct": (
                                    None
                                    if average_price_ct is None
                                    else round(float(average_price_ct), 2)
                                ),
                                "unpriced_import_kwh": round(
                                    float(unpriced_import_kwh), 4
                                ),
                                "cost_source": cost_source,
                                "energy_corrections_applied": has_energy_corrections
                                or has_grid_corrections,
                                "costs_include_energy_corrections": True,
                                "correction_cost_contract": "costs_match_corrected_energy",
                                "tariff_source": tariff_source,
                                "price_mode": price_mode,
                                "hours_without_price": (
                                    0 if month_import_price_ct else 1
                                ),
                                "fallback_price_ct": round(month_import_price_ct, 2),
                                "feed_in_tariff_ct": round(month_feed_in_tariff_ct, 2),
                                "import_split": import_split_by_month.get(month_key),
                            }
                        )
                        continue
        except Exception as db_error:
            _LOGGER.debug("Error loading monthly energy: %s", db_error)
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT peak_solar_w, peak_solar_time, date\n                       FROM stats_daily_energy\n                       WHERE peak_solar_w = (SELECT MAX(peak_solar_w) FROM stats_daily_energy)\n                       LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        result["alltime_peak"] = {
                            "watts": round(row[0], 0),
                            "time": row[1] or "",
                            "date": row[2] or "",
                        }
        except Exception as db_error:
            _LOGGER.debug("Error loading alltime peak: %s", db_error)
        try:
            from ..core.daily_costs import load_provenance as load_provenance

            async with _get_db() as db:
                result["provenance"] = await load_provenance(
                    db,
                    week_start_date,
                    today,
                    await _energy_correction_projection(week_start_date.isoformat()),
                )
        except Exception as error:
            _LOGGER.debug("Summary provenance unavailable: %s", error)
        result["hubble"] = await _build_hubble_summary(today, result)
        return web.json_response(result)


class RealtimeDataView(HomeAssistantView):
    """API for realtime data (polled by frontend). @zara"""

    url = "/api/sfml_stats/realtime"
    name = "api:sfml_stats:realtime"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return current realtime data. @zara"""
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "current_hour": ha_now(context.HASS).hour,
            "data": {},
        }
        try:
            solar_reader = _get_solar_reader()
            now = ha_now(context.HASS)
            hourly_predictions = await solar_reader.async_get_hourly_predictions(
                target_date=now.date()
            )
            current_prediction = next(
                (
                    prediction
                    for prediction in hourly_predictions
                    if prediction.target_hour == now.hour
                ),
                None,
            )
            actual_kwh = current_prediction.actual_kwh if current_prediction else None
            if current_prediction:
                result["data"]["solar"] = {
                    "prediction_kwh": current_prediction.prediction_kwh,
                    "actual_kwh": actual_kwh,
                    "weather": {
                        "temperature": current_prediction.temperature,
                        "solar_radiation": current_prediction.solar_radiation,
                        "clouds": current_prediction.clouds,
                    },
                    "astronomy": {
                        "sun_elevation": current_prediction.sun_elevation,
                        "theoretical_max_kwh": current_prediction.theoretical_max_kwh,
                    },
                }
        except Exception as db_error:
            _LOGGER.error(
                "Error loading realtime prediction from database: %s", db_error
            )
        try:
            now = ha_now(context.HASS)
            today_key = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            local_range_start, local_range_end = _get_local_naive_range_for_local_date(
                today_key
            )
            async with _get_db() as db:
                async with db.execute(
                    "\n                    SELECT price_net, hour\n                    FROM GPM_price_history\n                    WHERE timestamp >= ? AND timestamp < ? AND hour = ?\n                    ORDER BY timestamp DESC LIMIT 1\n                ",
                    (local_range_start, local_range_end, current_hour),
                ) as cursor:
                    current_price_row = await cursor.fetchone()
            if current_price_row:
                result["data"]["price"] = {
                    "current": current_price_row["price_net"] or 0,
                    "hour": current_price_row["hour"],
                }
        except Exception as db_error:
            _LOGGER.error("Error loading current price from DB: %s", db_error)
        today_key = ha_today(context.HASS).isoformat()
        hour_key = str(ha_now(context.HASS).hour)
        actual_weather = await _get_weather_from_db(days=1)
        if (
            actual_weather
            and today_key in actual_weather
            and (hour_key in actual_weather[today_key])
        ):
            result["data"]["weather_actual"] = actual_weather[today_key][hour_key]
        return web.json_response(result)


def normalized_group_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _get_sfml_panel_groups() -> list[dict[str, Any]]:
    """Get panel groups configuration from SFML. @zara"""
    if context.HASS is None:
        return []
    try:
        integration_domain = "solar_forecast_ml"
        integration_entries = context.HASS.data.get(integration_domain, {})
        for entry_id, coordinator in integration_entries.items():
            if hasattr(coordinator, "panel_groups"):
                panel_groups = coordinator.panel_groups
                if panel_groups:
                    _LOGGER.debug(
                        "Found %d panel groups from SFML coordinator", len(panel_groups)
                    )
                    return panel_groups
        return []
    except Exception as db_error:
        _LOGGER.debug("Could not get SFML panel groups: %s", db_error)
        return []


def _read_panel_group_sensor(entity_id: str) -> float | None:
    """Read current value from a panel group energy sensor. @zara"""
    if not context.HASS or not entity_id:
        return
    try:
        entity_state = context.HASS.states.get(entity_id)
        if entity_state is None or entity_state.state in (
            "unavailable",
            "unknown",
            None,
        ):
            return None
        energy_kwh = float(entity_state.state)
        unit_of_measurement = entity_state.attributes.get("unit_of_measurement", "")
        if unit_of_measurement.lower() == "wh":
            energy_kwh = energy_kwh / 1000.0
        return round(energy_kwh, 3)
    except (ValueError, TypeError):
        return


def _get_status_chip(
    entity_id: str, label: str, source: str, *, binary: bool = False
) -> dict[str, Any]:
    """Read a compact dashboard status chip from a Home Assistant entity. @zara"""
    chip_result = _new_status_chip(label, source, entity_id=entity_id)
    if context.HASS is None:
        return chip_result
    entity_state = context.HASS.states.get(entity_id)
    return _status_chip_from_state(chip_result, entity_state, binary=binary)


def _new_status_chip(
    label: str, source: str, *, entity_id: str | None = None
) -> dict[str, Any]:
    return {
        "label": label,
        "source": source,
        "entity_id": entity_id,
        "state": None,
        "display": "--",
        "available": False,
        "active": None,
        "unit": None,
    }


def _status_chip_from_state(
    chip: dict[str, Any], state: Any, *, binary: bool = False
) -> dict[str, Any]:
    if state is None or state.state in ("unknown", "unavailable", None):
        return chip
    state_text = str(state.state)
    unit_of_measurement = state.attributes.get("unit_of_measurement")
    chip["state"] = state_text
    chip["available"] = True
    chip["unit"] = unit_of_measurement
    if binary:
        active = state_text.lower() == "on"
        chip["active"] = active
        chip["display"] = "ON" if active else "OFF"
        return chip
    try:
        numeric_value = float(state_text)
        if abs(numeric_value) >= 100:
            display_value = f"{numeric_value:.0f}"
        elif abs(numeric_value) >= 10:
            display_value = f"{numeric_value:.1f}"
        else:
            display_value = f"{numeric_value:.2f}".rstrip("0").rstrip(".")
        chip["display"] = (
            f"{display_value} {unit_of_measurement}".strip()
            if unit_of_measurement
            else display_value
        )
    except (TypeError, ValueError):
        chip["display"] = state_text
    return chip if True else None


def _status_chip_from_value(
    label: str,
    source: str,
    value: Any,
    *,
    unit: str | None = None,
    entity_id: str | None = None,
    binary: bool = False,
) -> dict[str, Any]:
    chip_result = _new_status_chip(label, source, entity_id=entity_id)
    if value is None:
        return chip_result
    state_text = str(value)
    chip_result["state"] = state_text
    chip_result["available"] = True
    chip_result["unit"] = unit
    if binary:
        active = bool(value)
        chip_result["active"] = active
        chip_result["display"] = "ON" if active else "OFF"
        return chip_result
    try:
        numeric_value = float(state_text)
        if abs(numeric_value) >= 100:
            display_value = f"{numeric_value:.0f}"
        elif abs(numeric_value) >= 10:
            display_value = f"{numeric_value:.1f}"
        else:
            display_value = f"{numeric_value:.2f}".rstrip("0").rstrip(".")
        chip_result["display"] = (
            f"{display_value} {unit}".strip() if unit else display_value
        )
    except (TypeError, ValueError):
        chip_result["display"] = state_text
    return chip_result if True else None


def _resolve_entity_id_by_unique_suffix(
    entity_domain: str,
    platform: str,
    suffixes: tuple[str, ...],
    candidates: tuple[str, ...] = (),
) -> str | None:
    if context.HASS is None:
        return
    for resolved_entity_id in candidates:
        entity_state = context.HASS.states.get(resolved_entity_id)
        if entity_state is None:
            continue
        if not entity_state.state not in ("unknown", "unavailable", None):
            continue
        return resolved_entity_id
    try:
        entity_registry = er.async_get(context.HASS)
        for registry_entry in entity_registry.entities.values():
            if (
                registry_entry.domain != entity_domain
                or registry_entry.platform != platform
            ):
                continue
            unique_id = str(registry_entry.unique_id or "").lower()
            if not any((unique_id.endswith(suffix.lower()) for suffix in suffixes)):
                continue
            if registry_entry.disabled_by is not None:
                continue
            entity_state = context.HASS.states.get(registry_entry.entity_id)
            if entity_state is not None:
                if entity_state.state not in ("unknown", "unavailable", None):
                    return registry_entry.entity_id
    except Exception as error:
        _LOGGER.debug("Entity registry status-chip lookup failed: %s", error)
    lowercase_suffixes = tuple((suffix.lower() for suffix in suffixes))
    for entity_state in context.HASS.states.async_all(entity_domain):
        entity_suffix = entity_state.entity_id.split(".", 1)[-1].lower()
        if not entity_suffix.endswith(lowercase_suffixes):
            continue
        if not entity_state.state not in ("unknown", "unavailable", None):
            continue
        return entity_state.entity_id


async def _get_today_calculated_energy() -> dict[str, float]:
    energy_columns = (
        "grid_import_kwh",
        "grid_export_kwh",
        "smartmeter_import_kwh",
        "smartmeter_export_kwh",
        "solar_to_battery_kwh",
        "grid_to_battery_kwh",
        "consumer_heatpump_kwh",
        "consumer_heatingrod_kwh",
        "consumer_wallbox_kwh",
    )
    calculated_energy = {column: 0.0 for column in energy_columns}
    try:
        async with _get_db() as db:
            async with db.execute(
                f"SELECT {', '.join(energy_columns)} FROM stats_daily_energy WHERE date = ?",
                (ha_today(context.HASS).isoformat(),),
            ) as cursor:
                row = await cursor.fetchone()
        if row is not None:
            calculated_energy.update(
                {
                    column: float(dict(row).get(column) or 0.0)
                    for column in energy_columns
                }
            )
    except Exception as error:
        _LOGGER.debug("Calculated daily energy unavailable: %s", error)
    return calculated_energy if True else None


def _get_status_chip_resolved(
    label: str,
    source: str,
    entity_domain: str,
    platform: str,
    suffixes: tuple[str, ...],
    candidates: tuple[str, ...],
    *,
    binary: bool = False,
) -> dict[str, Any]:
    resolved_entity_id = _resolve_entity_id_by_unique_suffix(
        entity_domain, platform, suffixes, candidates
    )
    return (
        _get_status_chip(resolved_entity_id, label, source, binary=binary)
        if resolved_entity_id
        else _new_status_chip(
            label, source, entity_id=candidates[0] if candidates else None
        )
    )


def _get_sfml_coordinator() -> Any | None:
    if context.HASS is None:
        return
    for coordinator in context.HASS.data.get("solar_forecast_ml", {}).values():
        if not hasattr(coordinator, "async_add_listener"):
            continue
        if not hasattr(coordinator, "data"):
            continue
        return coordinator


def _get_w_stability_status_chip() -> dict[str, Any]:
    try:
        return _get_w_stability_status_chip_inner()
    except Exception:
        return _new_status_chip("W-Stability", "SFML")


def _get_w_stability_status_chip_inner() -> dict[str, Any]:
    coordinator = _get_sfml_coordinator()
    if coordinator is not None:
        cloudiness_volatility = getattr(coordinator, "cloudiness_volatility", None)
        if cloudiness_volatility is not None:
            try:
                weather_stability_percent = max(
                    0.0, min(100.0, 100.0 - float(cloudiness_volatility))
                )
                return _status_chip_from_value(
                    "W-Stability",
                    "SFML",
                    round(weather_stability_percent, 1),
                    unit="%",
                    entity_id="sfml_stats.internal.w_stability",
                )
            except (TypeError, ValueError):
                pass
    return _get_status_chip_resolved(
        "W-Stability",
        "SFML",
        "sensor",
        "solar_forecast_ml",
        ("_ml_cloudiness_volatility", "cloudiness_volatility"),
        ("sensor.solar_forecast_ml_cloudiness_volatility",),
    )


async def _get_p_time_status_chip() -> dict[str, Any]:
    try:
        async with _get_db() as db:
            async with db.execute(
                "SELECT production_time_today FROM production_time_state WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            production_time = (
                row["production_time_today"] if hasattr(row, "keys") else row[0]
            )
            if production_time:
                return _status_chip_from_value(
                    "P-Time",
                    "SFML",
                    production_time,
                    entity_id="sfml_stats.internal.p_time",
                )
    except Exception as error:
        _LOGGER.debug("Production-time status-chip DB lookup failed: %s", error)
    resolved_entity_id = _resolve_entity_id_by_unique_suffix(
        "sensor",
        "solar_forecast_ml",
        ("_ml_production_time", "production_time", "produktionszeit_heute"),
        (
            "sensor.solar_forecast_ml_produktionszeit_heute",
            "sensor.solar_forecast_ml_production_time",
        ),
    )
    if resolved_entity_id:
        return _get_status_chip(resolved_entity_id, "P-Time", "SFML")
    return _new_status_chip("P-Time", "SFML")


def _get_smc_status_chip() -> dict[str, Any]:
    coordinator = _get_stats_gpm_coordinator()
    if coordinator is not None:
        coordinator_data = coordinator.data or {}
        return _status_chip_from_value(
            "SMC",
            "STATS",
            bool(coordinator_data.get("smart_charging_active", False)),
            entity_id="sfml_stats.internal.smc",
            binary=True,
        )
    return _get_status_chip_resolved(
        "SMC",
        "STATS",
        "binary_sensor",
        DOMAIN,
        ("_smart_charging", "smart_charging_active"),
        ("binary_sensor.solar_forecast_stats_smart_charging_active",),
        binary=True,
    )


def _get_s_plus_status_chip() -> dict[str, Any]:
    return _get_status_chip_resolved(
        "S-Plus",
        "STATS",
        "binary_sensor",
        DOMAIN,
        ("_surplus_available", "pv_surplus_available"),
        ("binary_sensor.solar_forecast_stats_pv_surplus_available",),
        binary=True,
    )


def _get_weather_data(entity_id: str | None) -> dict[str, Any] | None:
    """Read weather data from a Home Assistant weather entity. @zara"""
    if not entity_id or not context.HASS:
        return None
    entity_state = context.HASS.states.get(entity_id)
    if entity_state is None or entity_state.state in ("unknown", "unavailable"):
        return None
    attributes = entity_state.attributes
    wind_speed = attributes.get("wind_speed")
    if wind_speed is not None:
        wind_speed_unit = attributes.get("wind_speed_unit", "km/h")
        if wind_speed_unit == "km/h":
            wind_speed = round(wind_speed / 3.6, 1)
    return {
        "state": entity_state.state,
        "temperature": attributes.get("temperature"),
        "humidity": attributes.get("humidity"),
        "wind_speed": wind_speed,
        "wind_bearing": attributes.get("wind_bearing"),
        "pressure": attributes.get("pressure"),
        "cloud_coverage": attributes.get("cloud_coverage"),
        "visibility": attributes.get("visibility"),
        "uv_index": attributes.get("uv_index"),
    }


class EnergyFlowView(HomeAssistantView):
    """API for energy flow data from Home Assistant sensors. @zara"""

    url = "/api/sfml_stats/energy_flow"
    name = "api:sfml_stats:energy_flow"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return current energy flow data. @zara"""
        config = _get_config()
        sfml_reader = SFMLDataReader(context.HASS)
        solar_power_w = await sfml_reader.get_live_power()
        solar_house_power_w = _get_power_w(config.get(CONF_SENSOR_SOLAR_TO_HOUSE))
        solar_battery_power_w = _get_power_w(config.get(CONF_SENSOR_SOLAR_TO_BATTERY))
        if solar_power_w is not None and solar_power_w < 0:
            solar_power_w = 0.0
        if solar_house_power_w is not None and solar_house_power_w < 0:
            solar_house_power_w = 0.0
        if solar_battery_power_w is not None and solar_battery_power_w < 0:
            solar_battery_power_w = 0.0
        if solar_power_w is not None and solar_power_w <= 0:
            solar_house_power_w = 0.0
            solar_battery_power_w = 0.0
        elif solar_power_w is not None and solar_battery_power_w is not None:
            solar_battery_power_w = min(solar_battery_power_w, solar_power_w)
        if solar_power_w is not None and solar_house_power_w is not None:
            solar_house_power_w = min(solar_house_power_w, solar_power_w)
        if (
            solar_house_power_w is None
            and solar_power_w is not None
            and (solar_power_w > 0)
        ):
            solar_charge_power_w = solar_battery_power_w or 0
            solar_house_power_w = max(0, solar_power_w - solar_charge_power_w)
        battery_configured = config.get(CONF_SENSOR_BATTERY_SOC) is not None
        battery_soc = (
            _get_sensor_value(config.get(CONF_SENSOR_BATTERY_SOC))
            if battery_configured
            else None
        )
        battery_power_w = (
            _get_power_w(config.get(CONF_SENSOR_BATTERY_POWER), signed=True)
            if battery_configured
            else None
        )
        battery_house_power_w = (
            _get_power_w(config.get(CONF_SENSOR_BATTERY_TO_HOUSE))
            if battery_configured
            else None
        )
        grid_battery_power_w = (
            _get_power_w(config.get(CONF_SENSOR_GRID_TO_BATTERY), signed=True)
            if battery_configured
            else None
        )
        if not battery_configured:
            solar_battery_power_w = None
        if battery_configured:
            if battery_power_w is None or battery_power_w == 0:
                derived_charging_power_w = (solar_battery_power_w or 0) + (
                    grid_battery_power_w or 0
                )
                battery_discharge_power_w = battery_house_power_w or 0
                if derived_charging_power_w > 0:
                    battery_power_w = derived_charging_power_w
                elif battery_discharge_power_w > 0:
                    battery_power_w = -battery_discharge_power_w
        measured_yield_kwh = await _get_today_yield_from_db()
        today_yield_kwh = (
            measured_yield_kwh
            if measured_yield_kwh is not None
            else await sfml_reader.get_live_yield()
        )
        calculated_daily_energy = await _get_today_calculated_energy()
        grid_house_entity_id = config.get(CONF_SENSOR_GRID_TO_HOUSE)
        grid_house_power_w = (
            _get_power_w(grid_house_entity_id, signed=True)
            if grid_house_entity_id
            else None
        )
        measured_grid_import_w = _get_power_w(
            config.get(CONF_SENSOR_SMARTMETER_IMPORT), signed=True
        )
        if grid_house_power_w is None:
            grid_house_power_w = measured_grid_import_w
        elif measured_grid_import_w is not None:
            grid_house_power_w, adjusted_grid_battery_w = reconcile_grid_split(
                measured_grid_import_w,
                grid_house_power_w,
                grid_battery_power_w,
                tolerance=SPLIT_TOLERANCE_W,
            )
            if config.get(CONF_SENSOR_GRID_TO_BATTERY):
                grid_battery_power_w = adjusted_grid_battery_w
        grid_export_power_w = _get_power_w(
            config.get(CONF_SENSOR_HOUSE_TO_GRID), signed=True
        )
        if grid_export_power_w is None:
            grid_export_power_w = _get_power_w(
                config.get(CONF_SENSOR_SMARTMETER_EXPORT), signed=True
            )
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "flows": {
                "solar_power": solar_power_w,
                "solar_to_house": solar_house_power_w,
                "solar_to_battery": solar_battery_power_w,
                "battery_to_house": battery_house_power_w,
                "grid_to_house": grid_house_power_w,
                "grid_to_battery": grid_battery_power_w,
                "house_to_grid": grid_export_power_w,
            },
            "battery": {"soc": battery_soc, "power": battery_power_w},
            "home": {
                "consumption": _get_power_w(config.get(CONF_SENSOR_HOME_CONSUMPTION))
            },
            "statistics": {
                "solar_yield_daily": today_yield_kwh,
                "grid_import_daily": calculated_daily_energy["smartmeter_import_kwh"]
                or calculated_daily_energy["grid_import_kwh"],
                "battery_charge_solar_daily": calculated_daily_energy[
                    "solar_to_battery_kwh"
                ],
                "battery_charge_grid_daily": calculated_daily_energy[
                    "grid_to_battery_kwh"
                ],
            },
            "configured_sensors": {
                "solar_power": "SFML actual_live_state.total_power_w",
                "solar_to_house": config.get(CONF_SENSOR_SOLAR_TO_HOUSE),
                "solar_to_battery": config.get(CONF_SENSOR_SOLAR_TO_BATTERY),
                "battery_to_house": config.get(CONF_SENSOR_BATTERY_TO_HOUSE),
                "grid_to_house": config.get(CONF_SENSOR_GRID_TO_HOUSE),
                "grid_to_battery": config.get(CONF_SENSOR_GRID_TO_BATTERY),
                "house_to_grid": config.get(CONF_SENSOR_HOUSE_TO_GRID),
                "battery_soc": config.get(CONF_SENSOR_BATTERY_SOC),
                "home_consumption": config.get(CONF_SENSOR_HOME_CONSUMPTION),
                "solar_yield_daily": "SFML actual_live_state.total_actual_kwh",
                "weather_entity": config.get(CONF_WEATHER_ENTITY),
            },
            "panels": await self._get_panel_data(config),
            "consumers": self._get_consumer_data(config, calculated_daily_energy),
            "weather_ha": _get_weather_data(config.get(CONF_WEATHER_ENTITY)),
            "status_chips": [
                _get_w_stability_status_chip(),
                await _get_p_time_status_chip(),
                _get_smc_status_chip(),
                _get_s_plus_status_chip(),
            ],
            "sun_position": await self._get_sun_position(),
            "current_price": await self._get_current_price(),
            "feed_in_tariff": feed_in_tariff_ct(context.HASS, config),
            "price_mode": effective_price_mode(context.HASS, config),
            "hours_without_price": 0,
        }
        return web.json_response(result)

    async def _get_current_price(self) -> dict[str, Any] | None:
        """Read current electricity price from GPM_price_history DB. @zara"""
        try:
            today_key = ha_today(context.HASS).isoformat()
            current_hour = ha_now(context.HASS).hour
            local_range_start, local_range_end = _get_local_naive_range_for_local_date(
                today_key
            )
            async with _get_db() as db:
                async with db.execute(
                    "\n                    SELECT price_net, total_price, hour\n                    FROM GPM_price_history\n                    WHERE timestamp >= ? AND timestamp < ? AND hour = ?\n                    ORDER BY timestamp DESC LIMIT 1\n                ",
                    (local_range_start, local_range_end, current_hour),
                ) as cursor:
                    row = await cursor.fetchone()
            if row:
                return {
                    "total_price": row["total_price"],
                    "net_price": row["price_net"],
                    "hour": current_hour,
                }
        except Exception as db_error:
            _LOGGER.error("Error loading current price from DB: %s", db_error)
        return None if True else None

    async def _get_sun_position(self) -> dict[str, Any] | None:
        """Read current sun position from astronomy_cache DB table. @zara"""
        today_key = ha_today(context.HASS).isoformat()
        current_hour = ha_now(context.HASS).hour
        try:
            async with _get_db() as db:
                async with db.execute(
                    "SELECT sun_elevation_deg, sun_azimuth_deg,\n                              sunrise, sunset, solar_noon, daylight_hours\n                       FROM astronomy_cache\n                       WHERE cache_date = ? AND hour = ?",
                    (today_key, current_hour),
                ) as cursor:
                    row = await cursor.fetchone()
            if not row:
                return
            sun_azimuth = row["sun_azimuth_deg"] or 0
            delta_sign = self._azimuth_to_direction(sun_azimuth)
            return {
                "elevation_deg": row["sun_elevation_deg"],
                "azimuth_deg": sun_azimuth,
                "direction": delta_sign,
                "sunrise": self._extract_time(row["sunrise"]),
                "sunset": self._extract_time(row["sunset"]),
                "solar_noon": self._extract_time(row["solar_noon"]),
                "daylight_hours": row["daylight_hours"],
            }
        except Exception as db_error:
            _LOGGER.error("Error loading sun position from DB: %s", db_error)
            return

    def _azimuth_to_direction(self, azimuth: float) -> str:
        """Convert azimuth degrees to cardinal direction. @zara"""
        if azimuth is None:
            return "—"
        azimuth = azimuth % 360
        compass_directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        direction_index = int((azimuth + 22.5) / 45) % 8
        return compass_directions[direction_index]

    def _extract_time(self, iso_string: str | None) -> str | None:
        """Extract HH:MM from ISO or DB datetime string. @zara"""
        if not iso_string:
            return
        try:
            if "T" in iso_string:
                time_part = iso_string.split("T")[1]
                return time_part[:5]
            if " " in iso_string:
                time_part = iso_string.split(" ")[1]
                return time_part[:5]
            return iso_string[:5]
        except Exception:
            return

    async def _get_panel_data(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Read panel data from SFML SOT and calculate today's peak power. @zara"""
        sfml_reader = SFMLDataReader(context.HASS)
        live_panel_groups = await sfml_reader.get_live_panel_groups()
        if not live_panel_groups:
            return []
        today_key = ha_now(context.HASS).strftime("%Y-%m-%d 00:00:00")
        panel_energy_today = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        try:
            async with _get_db() as db:
                async with db.execute(
                    "\n                    SELECT MAX(panel1_watt) as p1,\n                           MAX(panel2_watt) as p2,\n                           MAX(panel3_watt) as p3,\n                           MAX(panel4_watt) as p4\n                    FROM sensor_power_live\n                    WHERE timestamp >= ?\n                ",
                    (today_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        panel_energy_today[1] = row["p1"] or 0.0
                        panel_energy_today[2] = row["p2"] or 0.0
                        panel_energy_today[3] = row["p3"] or 0.0
                        panel_energy_today[4] = row["p4"] or 0.0
        except Exception as db_error:
            _LOGGER.debug("Could not load panel max values from DB: %s", db_error)
        result = []
        for live_panel_group in live_panel_groups:
            panel_group_id = live_panel_group["id"]
            power_w = live_panel_group.get("power") or 0.0
            result.append(
                {
                    "id": panel_group_id,
                    "name": live_panel_group.get("name"),
                    "power": power_w,
                    "max_today": round(
                        max(panel_energy_today.get(panel_group_id, 0.0), power_w), 2
                    ),
                    "actual_today_kwh": live_panel_group.get("actual_today_kwh"),
                    "quality": live_panel_group.get("quality"),
                }
            )
        return result

    def _get_consumer_data(
        self, config: dict[str, Any], calculated_daily: dict[str, float]
    ) -> dict[str, Any]:
        """Read consumer data from configured sensors. @zara"""
        consumers = {"heatpump": None, "heatingrod": None, "wallbox": None}
        from ..core.eai_wp_sources import (
            resolved_heatpump_entity as resolved_heatpump_entity,
        )

        heatpump_power_entity = resolved_heatpump_entity(
            context.HASS, config, CONF_SENSOR_HEATPUMP_POWER
        )
        heatpump_cop_entity = resolved_heatpump_entity(
            context.HASS, config, CONF_SENSOR_HEATPUMP_COP
        )
        if heatpump_power_entity:
            power_w = _get_power_w(heatpump_power_entity)
            consumer_daily_kwh = calculated_daily["consumer_heatpump_kwh"]
            heatpump_cop = _get_sensor_value(heatpump_cop_entity)
            if heatpump_cop is None:
                heatpump_cop = DEFAULT_HEATPUMP_COP
            consumers["heatpump"] = {
                "power": power_w,
                "daily_kwh": consumer_daily_kwh,
                "cop": heatpump_cop,
                "configured": True,
            }
        if config.get(CONF_SENSOR_HEATINGROD_POWER):
            power_w = _get_power_w(config.get(CONF_SENSOR_HEATINGROD_POWER))
            consumer_daily_kwh = calculated_daily["consumer_heatingrod_kwh"]
            consumers["heatingrod"] = {
                "power": power_w,
                "daily_kwh": consumer_daily_kwh,
                "configured": True,
            }
        if config.get(CONF_SENSOR_WALLBOX_POWER):
            power_w = _get_power_w(config.get(CONF_SENSOR_WALLBOX_POWER))
            consumer_daily_kwh = calculated_daily["consumer_wallbox_kwh"]
            entity_state = _get_sensor_value(config.get(CONF_SENSOR_WALLBOX_STATE))
            consumers["wallbox"] = {
                "power": power_w,
                "daily_kwh": consumer_daily_kwh,
                "state": entity_state,
                "configured": True,
            }
        return consumers


class StatisticsView(HomeAssistantView):
    """API for statistics data from JSON files. @zara"""

    url = "/api/sfml_stats/statistics"
    name = "api:sfml_stats:statistics"
    requires_auth = False

    @local_only
    async def get(self, request: Request) -> Response:
        """Return statistics data from SFML SQLite database. @zara"""
        selected_date = _query_local_date(request)
        result = {
            "success": True,
            "timestamp": ha_now(context.HASS).isoformat(),
            "time_context": _time_context(),
            "selected_date": selected_date.isoformat(),
            "peaks": {},
            "production": {},
            "statistics": {},
        }
        solar_reader = _get_solar_reader()
        try:
            daily_forecasts = await solar_reader.async_get_daily_forecasts()
            today_forecast = daily_forecasts.get("today")
            if today_forecast:
                result["production"]["today"] = {
                    "forecast_kwh": today_forecast.prediction_kwh,
                    "forecast_kwh_display": f"{today_forecast.prediction_kwh:.2f}",
                    "yield_kwh": None,
                    "source": today_forecast.source,
                    "locked": today_forecast.locked,
                }
            else:
                result["production"]["today"] = {
                    "forecast_kwh": None,
                    "forecast_kwh_display": None,
                    "yield_kwh": None,
                }
            tomorrow_forecast = daily_forecasts.get("tomorrow")
            if tomorrow_forecast:
                result["production"]["tomorrow"] = {
                    "forecast_kwh": tomorrow_forecast.prediction_kwh,
                    "forecast_kwh_display": f"{tomorrow_forecast.prediction_kwh:.2f}",
                    "date": (
                        tomorrow_forecast.forecast_date.isoformat()
                        if tomorrow_forecast.forecast_date
                        else None
                    ),
                }
            else:
                result["production"]["tomorrow"] = {
                    "forecast_kwh": None,
                    "forecast_kwh_display": None,
                }
        except Exception as db_error:
            _LOGGER.error("Error loading daily forecasts from database: %s", db_error)
            result["production"]["today"] = {
                "forecast_kwh": None,
                "forecast_kwh_display": None,
                "yield_kwh": None,
            }
            result["production"]["tomorrow"] = {
                "forecast_kwh": None,
                "forecast_kwh_display": None,
            }
        try:
            sfml_reader = SFMLDataReader(context.HASS)
            live_yield_kwh = await sfml_reader.get_live_yield()
            if live_yield_kwh is not None:
                result["production"]["today"]["yield_kwh"] = live_yield_kwh
        except Exception as db_error:
            _LOGGER.debug("Could not load yield from SFML: %s", db_error)
        try:
            async with _get_db() as stats_db:
                async with stats_db.execute(
                    "SELECT peak_power_w, peak_power_time, peak_record_w, peak_record_date, peak_record_time, production_time_today FROM production_time_state WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        result["peaks"]["today"] = {
                            "power_w": row["peak_power_w"],
                            "at": row["peak_power_time"],
                        }
                        result["peaks"]["record"] = {
                            "power_w": row["peak_record_w"],
                            "date": row["peak_record_date"],
                            "at": row["peak_record_time"],
                        }
                        result["production_time"] = row["production_time_today"]
                    else:
                        result["peaks"]["today"] = {"power_w": None, "at": None}
        except Exception as db_error:
            _LOGGER.debug("Could not load today's peak from database: %s", db_error)
            result["peaks"]["today"] = {"power_w": None, "at": None}
        try:
            async with solar_reader._get_db_connection() as stats_db:
                async with stats_db.execute(
                    "SELECT all_time_peak_power_w, all_time_peak_date, all_time_peak_at\n                       FROM daily_statistics WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        result["peaks"]["all_time"] = {
                            "power_w": row["all_time_peak_power_w"],
                            "date": row["all_time_peak_date"],
                            "at": row["all_time_peak_at"],
                        }
                    else:
                        result["peaks"]["all_time"] = {
                            "power_w": None,
                            "date": None,
                            "at": None,
                        }
        except Exception as db_error:
            _LOGGER.debug("Could not load all-time peak from database: %s", db_error)
            result["peaks"]["all_time"] = {"power_w": None, "date": None, "at": None}
        try:
            today_predictions = await solar_reader.async_get_hourly_predictions(
                target_date=ha_today(context.HASS)
            )
            result["best_hour"] = {"hour": None, "prediction_kwh": None}
            if today_predictions:
                producing_hours = [
                    prediction
                    for prediction in today_predictions
                    if prediction.prediction_kwh > 0
                ]
                if producing_hours:
                    peak_prediction = max(
                        producing_hours, key=lambda x: x.prediction_kwh
                    )
                    result["best_hour"] = {
                        "hour": peak_prediction.target_hour,
                        "prediction_kwh": peak_prediction.prediction_kwh,
                    }
        except Exception as db_error:
            _LOGGER.error("Error loading best hour from database: %s", db_error)
            result["best_hour"] = {"hour": None, "prediction_kwh": None}
        try:
            async with solar_reader._get_db_connection() as stats_db:
                async with stats_db.execute(
                    "SELECT\n                        current_week_period, current_week_yield_kwh, current_week_consumption_kwh, current_week_days,\n                        current_month_period, current_month_yield_kwh, current_month_consumption_kwh, current_month_avg_autarky, current_month_days,\n                        last_7_days_avg_yield_kwh, last_7_days_avg_accuracy, last_7_days_total_yield_kwh,\n                        last_7_days_avg_evaluation_coverage_percent, last_7_days_avg_excluded_mppt_hours,\n                        last_30_days_avg_yield_kwh, last_30_days_avg_accuracy, last_30_days_total_yield_kwh,\n                        last_30_days_avg_evaluation_coverage_percent, last_30_days_avg_excluded_mppt_hours,\n                        last_365_days_avg_yield_kwh, last_365_days_total_yield_kwh\n                       FROM daily_statistics WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        result["statistics"]["current_week"] = {
                            "period": row["current_week_period"],
                            "yield_kwh": row["current_week_yield_kwh"],
                            "consumption_kwh": row["current_week_consumption_kwh"],
                            "days": row["current_week_days"],
                        }
                        result["statistics"]["current_month"] = {
                            "period": row["current_month_period"],
                            "yield_kwh": row["current_month_yield_kwh"],
                            "consumption_kwh": row["current_month_consumption_kwh"],
                            "avg_autarky": row["current_month_avg_autarky"],
                            "days": row["current_month_days"],
                        }
                        result["statistics"]["last_7_days"] = {
                            "avg_yield_kwh": row["last_7_days_avg_yield_kwh"],
                            "avg_accuracy": row["last_7_days_avg_accuracy"],
                            "avg_evaluation_coverage_percent": row[
                                "last_7_days_avg_evaluation_coverage_percent"
                            ],
                            "avg_excluded_mppt_hours": row[
                                "last_7_days_avg_excluded_mppt_hours"
                            ],
                            "total_yield_kwh": row["last_7_days_total_yield_kwh"],
                        }
                        result["statistics"]["last_30_days"] = {
                            "avg_yield_kwh": row["last_30_days_avg_yield_kwh"],
                            "avg_accuracy": row["last_30_days_avg_accuracy"],
                            "avg_evaluation_coverage_percent": row[
                                "last_30_days_avg_evaluation_coverage_percent"
                            ],
                            "avg_excluded_mppt_hours": row[
                                "last_30_days_avg_excluded_mppt_hours"
                            ],
                            "total_yield_kwh": row["last_30_days_total_yield_kwh"],
                        }
                        result["statistics"]["last_365_days"] = {
                            "avg_yield_kwh": row["last_365_days_avg_yield_kwh"],
                            "total_yield_kwh": row["last_365_days_total_yield_kwh"],
                        }
        except Exception as db_error:
            _LOGGER.debug("Could not load statistics from database: %s", db_error)
        try:
            async with solar_reader._get_db_connection() as stats_db:
                async with stats_db.execute(
                    "SELECT date, predicted_total_kwh, actual_total_kwh,\n                              accuracy_percent, peak_power_w, peak_hour,\n                              evaluation_predicted_kwh, evaluation_actual_kwh,\n                              evaluation_coverage_percent, excluded_hours_count,\n                              excluded_mppt_hours_count,\n                              evaluation_hours_count, production_candidate_hours_count,\n                              missing_actual_hours_count, excluded_weather_alert_hours_count,\n                              excluded_inverter_clipped_hours_count,\n                              excluded_reason_breakdown_json,\n                              peak_power_time, production_hours\n                       FROM daily_summaries\n                       WHERE date < ?\n                       ORDER BY date DESC\n                       LIMIT 365",
                    (ha_today(context.HASS).isoformat(),),
                ) as cursor:
                    rows = await cursor.fetchall()
                    statistics_history = []
                    for row in rows:
                        predicted_total_kwh = row["predicted_total_kwh"] or 0
                        actual_total_kwh = row["actual_total_kwh"] or 0
                        statistics_history.append(
                            {
                                "date": row["date"],
                                "predicted_kwh": predicted_total_kwh,
                                "actual_kwh": actual_total_kwh,
                                "accuracy": (
                                    round(row["accuracy_percent"], 1)
                                    if row["accuracy_percent"] is not None
                                    else None
                                ),
                                **_build_daily_summary_forecast_metrics(
                                    predicted_total_kwh,
                                    actual_total_kwh,
                                    row["evaluation_predicted_kwh"],
                                    row["evaluation_actual_kwh"],
                                    row["accuracy_percent"],
                                ),
                                "evaluation_predicted_kwh": row[
                                    "evaluation_predicted_kwh"
                                ],
                                "evaluation_actual_kwh": row["evaluation_actual_kwh"],
                                "evaluation_coverage_percent": row[
                                    "evaluation_coverage_percent"
                                ],
                                "excluded_hours_count": row["excluded_hours_count"]
                                or 0,
                                "excluded_mppt_hours_count": row[
                                    "excluded_mppt_hours_count"
                                ]
                                or 0,
                                "evaluation_hours_count": row["evaluation_hours_count"]
                                or 0,
                                "production_candidate_hours_count": row[
                                    "production_candidate_hours_count"
                                ]
                                or 0,
                                "missing_actual_hours_count": row[
                                    "missing_actual_hours_count"
                                ]
                                or 0,
                                "excluded_weather_alert_hours_count": row[
                                    "excluded_weather_alert_hours_count"
                                ]
                                or 0,
                                "excluded_inverter_clipped_hours_count": row[
                                    "excluded_inverter_clipped_hours_count"
                                ]
                                or 0,
                                "excluded_reason_breakdown_json": row[
                                    "excluded_reason_breakdown_json"
                                ],
                                "peak_power_w": row["peak_power_w"],
                                "peak_hour": row["peak_hour"],
                                "peak_at": row["peak_power_time"],
                                "production_hours": row["production_hours"],
                            }
                        )
                        continue
                    result["history"] = statistics_history
                    statistics_7d = _build_statistics_window_from_history_rows(
                        statistics_history, 7
                    )
                    if statistics_7d is not None:
                        result["statistics"]["last_7_days"] = statistics_7d
                    statistics_30d = _build_statistics_window_from_history_rows(
                        statistics_history, 30
                    )
                    if statistics_30d is not None:
                        result["statistics"]["last_30_days"] = statistics_30d
                async with stats_db.execute(
                    "SELECT target_date, target_hour, actual_kwh\n                       FROM hourly_predictions\n                       WHERE actual_kwh IS NOT NULL\n                         AND target_date >= ?\n                       ORDER BY target_date, target_hour",
                    ((ha_today(context.HASS) - timedelta(days=90)).isoformat(),),
                ) as cursor:
                    hourly_rows = await cursor.fetchall()
                    group_hours = {}
                    for row in hourly_rows:
                        date_key = row["target_date"]
                        if date_key not in group_hours:
                            group_hours[date_key] = {}
                        group_hours[date_key][row["target_hour"]] = row["actual_kwh"]
                    result["hourly_production"] = group_hours
        except Exception as db_error:
            _LOGGER.debug("Could not load history from database: %s", db_error)
            result["history"] = []
            result["hourly_production"] = {}
        result["panel_groups"] = await self._get_panel_group_data(
            target_date=selected_date
        )
        return web.json_response(result)

    async def _get_panel_group_data(
        self, target_date: date | None = None
    ) -> dict[str, Any]:
        """Extract panel group predictions and actuals for a local day. @zara"""
        config = _get_config()
        if config.get(CONF_SHOW_PANEL_GROUPS) is False:
            return {"available": False, "groups": {}}
        selected_date = target_date or ha_today(context.HASS)
        is_today = selected_date == ha_today(context.HASS)
        current_hour = ha_now(context.HASS).hour if is_today else 23
        live_groups_by_name = {}
        if is_today:
            try:
                for live_group in await SFMLDataReader(
                    context.HASS
                ).get_live_panel_groups():
                    live_groups_by_name[
                        normalized_group_key(live_group.get("name"))
                    ] = live_group
            except Exception as db_error:
                _LOGGER.debug("Could not load live panel group SOT: %s", db_error)
        try:
            solar_reader = _get_solar_reader()
            group_forecasts = await solar_reader.async_get_panel_group_data(
                target_date=selected_date
            )
            if not group_forecasts:
                group_forecasts = await self._get_panel_group_forecast_fallback(
                    selected_date
                )
                if not group_forecasts and (not live_groups_by_name):
                    return {
                        "available": False,
                        "target_date": selected_date.isoformat(),
                        "groups": {},
                    }
                if not group_forecasts:
                    return {
                        "available": True,
                        "target_date": selected_date.isoformat(),
                        "forecast_available": False,
                        "live_only": True,
                        "groups": self._build_live_panel_group_fallback(
                            live_groups_by_name
                        ),
                    }
        except Exception as db_error:
            _LOGGER.error("Error loading panel group data from database: %s", db_error)
            group_forecasts = await self._get_panel_group_forecast_fallback(
                selected_date
            )
            if not group_forecasts and (not live_groups_by_name):
                return {
                    "available": False,
                    "target_date": selected_date.isoformat(),
                    "groups": {},
                }
            if not group_forecasts:
                return {
                    "available": True,
                    "target_date": selected_date.isoformat(),
                    "forecast_available": False,
                    "live_only": True,
                    "groups": self._build_live_panel_group_fallback(
                        live_groups_by_name
                    ),
                }
        config = _get_config()
        group_name_mapping = config.get(CONF_PANEL_GROUP_NAMES, {})
        if not isinstance(group_name_mapping, dict):
            group_name_mapping = {}
        grouped_result = {}
        for group_name, group_hours in group_forecasts.items():
            display_name = group_name_mapping.get(group_name, group_name)
            live_group = live_groups_by_name.get(
                normalized_group_key(group_name)
            ) or live_groups_by_name.get(normalized_group_key(display_name))
            live_actual_kwh = None
            if live_group and live_group.get("actual_today_kwh") is not None:
                try:
                    live_actual_kwh = max(
                        0.0, float(live_group.get("actual_today_kwh") or 0.0)
                    )
                except (TypeError, ValueError):
                    live_actual_kwh = None
            group_summary = {
                "name": display_name,
                "original_name": group_name,
                "prediction_day_kwh": 0.0,
                "prediction_until_now_kwh": 0.0,
                "actual_until_now_kwh": 0.0,
                "clean_prediction_until_now_kwh": 0.0,
                "clean_actual_until_now_kwh": 0.0,
                "raw_prediction_until_now_kwh": 0.0,
                "raw_actual_until_now_kwh": 0.0,
                "day_progress_percent": None,
                "forecast_until_now_accuracy_percent": None,
                "clean_accuracy_percent": None,
                "elapsed_hours_count": 0,
                "clean_hours_count": 0,
                "excluded_hours_count": 0,
                "missing_actual_hours_count": 0,
                "exclusion_reason_breakdown": {},
                "hourly": [],
                "forecast_available": True,
                "live_only": False,
            }
            recorded_actual_kwh = 0.0
            current_hour_entry = None
            for group_hour in group_hours:
                predicted_hour_kwh = group_hour.prediction_kwh
                actual_kwh = group_hour.actual_kwh
                hour = (
                    group_hour.target_hour if group_hour.target_hour is not None else 0
                )
                hour_exclusion_reason = _panel_group_exclusion_reason(group_hour)
                hour_excluded = hour_exclusion_reason is not None
                if predicted_hour_kwh is not None:
                    group_summary["prediction_day_kwh"] += predicted_hour_kwh
                if actual_kwh is not None:
                    recorded_actual_kwh += actual_kwh
                if hour <= current_hour:
                    group_summary["elapsed_hours_count"] += 1
                    if predicted_hour_kwh is not None:
                        group_summary["prediction_until_now_kwh"] += predicted_hour_kwh
                        group_summary[
                            "raw_prediction_until_now_kwh"
                        ] += predicted_hour_kwh
                    if actual_kwh is not None:
                        group_summary["actual_until_now_kwh"] += actual_kwh
                        group_summary["raw_actual_until_now_kwh"] += actual_kwh
                    else:
                        group_summary["missing_actual_hours_count"] += 1
                    if hour_excluded:
                        group_summary["excluded_hours_count"] += 1
                        reason_breakdown = group_summary["exclusion_reason_breakdown"]
                        reason_breakdown[hour_exclusion_reason] = (
                            reason_breakdown.get(hour_exclusion_reason, 0) + 1
                        )
                    elif actual_kwh is not None:
                        group_summary["clean_hours_count"] += 1
                        if predicted_hour_kwh is not None:
                            group_summary[
                                "clean_prediction_until_now_kwh"
                            ] += predicted_hour_kwh
                        group_summary["clean_actual_until_now_kwh"] += actual_kwh
                hour_entry = {
                    "hour": hour,
                    "prediction_kwh": predicted_hour_kwh,
                    "actual_kwh": actual_kwh,
                    "excluded": hour_excluded,
                    "exclusion_reason": hour_exclusion_reason,
                }
                group_summary["hourly"].append(hour_entry)
                if hour == current_hour:
                    current_hour_entry = hour_entry
            group_summary["actual_source"] = "db_hourly_sum"
            group_summary["prediction_total_kwh"] = group_summary["prediction_day_kwh"]
            if is_today and live_actual_kwh is not None:
                live_actual_adjustment = live_actual_kwh - recorded_actual_kwh
                if live_actual_adjustment > 0.01 and group_summary["hourly"]:
                    adjustment_target = current_hour_entry
                    if adjustment_target is None:
                        elapsed_hour_entries = [
                            row
                            for row in group_summary["hourly"]
                            if int(row.get("hour") or -1) <= current_hour
                        ]
                        adjustment_target = (
                            elapsed_hour_entries[-1]
                            if elapsed_hour_entries
                            else group_summary["hourly"][-1]
                        )
                    adjustment_target["actual_kwh"] = (
                        adjustment_target.get("actual_kwh") or 0.0
                    ) + live_actual_adjustment
                    if int(adjustment_target.get("hour") or -1) <= current_hour:
                        group_summary[
                            "raw_actual_until_now_kwh"
                        ] += live_actual_adjustment
                        if not adjustment_target.get("excluded"):
                            if (
                                adjustment_target.get("actual_kwh") or 0.0
                            ) == live_actual_adjustment:
                                group_summary["clean_hours_count"] += 1
                                group_summary["missing_actual_hours_count"] = max(
                                    0, group_summary["missing_actual_hours_count"] - 1
                                )
                                group_summary["clean_prediction_until_now_kwh"] += (
                                    adjustment_target.get("prediction_kwh") or 0.0
                                )
                            group_summary[
                                "clean_actual_until_now_kwh"
                            ] += live_actual_adjustment
                group_summary["actual_until_now_kwh"] = live_actual_kwh
                group_summary["actual_total_kwh"] = live_actual_kwh
                group_summary["raw_actual_until_now_kwh"] = live_actual_kwh
                group_summary["actual_source"] = "actual_live_panel_groups"
                group_summary["live_power_w"] = (
                    live_group.get("power") if live_group else None
                )
                group_summary["live_quality"] = (
                    live_group.get("quality") if live_group else None
                )
                group_summary["live_updated_at"] = (
                    live_group.get("updated_at") if live_group else None
                )
            else:
                group_summary["actual_total_kwh"] = group_summary[
                    "actual_until_now_kwh"
                ]
            if group_summary["prediction_day_kwh"] > 0:
                group_summary["day_progress_percent"] = (
                    group_summary["actual_total_kwh"]
                    / group_summary["prediction_day_kwh"]
                    * 100
                )
            if (
                group_summary["raw_prediction_until_now_kwh"] > 0
                and group_summary["raw_actual_until_now_kwh"] > 0
            ):
                raw_error_percent = (
                    abs(
                        (
                            group_summary["raw_actual_until_now_kwh"]
                            - group_summary["raw_prediction_until_now_kwh"]
                        )
                        / group_summary["raw_prediction_until_now_kwh"]
                    )
                    * 100
                )
                group_summary["raw_accuracy_percent"] = max(
                    0, min(100, 100 - raw_error_percent)
                )
                group_summary["forecast_until_now_accuracy_percent"] = group_summary[
                    "raw_accuracy_percent"
                ]
            else:
                group_summary["raw_accuracy_percent"] = None
            if (
                group_summary["clean_prediction_until_now_kwh"] > 0
                and group_summary["clean_actual_until_now_kwh"] > 0
            ):
                clean_error_percent = (
                    abs(
                        (
                            group_summary["clean_actual_until_now_kwh"]
                            - group_summary["clean_prediction_until_now_kwh"]
                        )
                        / group_summary["clean_prediction_until_now_kwh"]
                    )
                    * 100
                )
                group_summary["accuracy_percent"] = max(
                    0, min(100, 100 - clean_error_percent)
                )
                group_summary["clean_accuracy_percent"] = group_summary[
                    "accuracy_percent"
                ]
            else:
                group_summary["accuracy_percent"] = None
            grouped_result[display_name] = group_summary
            continue
        return {
            "available": True,
            "target_date": selected_date.isoformat(),
            "forecast_available": True,
            "live_only": False,
            "groups": grouped_result,
        }

    async def _get_panel_group_forecast_fallback(
        self, target_date: date | None = None
    ) -> dict[str, list[PanelGroupData]]:
        target_date_key = (target_date or ha_today(context.HASS)).isoformat()
        try:
            async with _get_db() as db:
                panel_prediction_columns = await _table_columns(
                    db, "prediction_panel_groups"
                )
                hourly_prediction_columns = await _table_columns(
                    db, "hourly_predictions"
                )

                def hp_expr(name: str, default: str) -> str:
                    return (
                        f"hp.{name}"
                        if name in hourly_prediction_columns
                        else f"{default} AS {name}"
                    )

                def ppg_expr(name: str, default: str) -> str:
                    return (
                        f"ppg.{name}"
                        if name in panel_prediction_columns
                        else f"{default} AS {name}"
                    )

                async with db.execute(
                    f"SELECT ppg.group_name, ppg.prediction_kwh, ppg.actual_kwh,\n                              hp.target_hour,\n                              {hp_expr('exclude_from_learning', '0')},\n                              {hp_expr('exclude_from_clean_evaluation', '0')},\n                              {hp_expr('mppt_throttled', '0')},\n                              {hp_expr('inverter_clipped', '0')},\n                              {hp_expr('mppt_throttle_reason', 'NULL')},\n                              {hp_expr('has_weather_alert', '0')},\n                              {hp_expr('weather_alert_type', 'NULL')},\n                              {ppg_expr('exclude_from_learning_group', '0')},\n                              {ppg_expr('exclusion_reason_group', 'NULL')},\n                              {ppg_expr('snow_covered_group', '0')},\n                              {ppg_expr('topology_compatible', '1')},\n                              {ppg_expr('topology_exclusion_reason', 'NULL')}\n                       FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                       WHERE hp.target_date = ?\n                         AND ppg.group_name IS NOT NULL\n                         AND TRIM(ppg.group_name) != ''\n                       ORDER BY hp.target_hour, ppg.group_name",
                    (target_date_key,),
                ) as cursor:
                    rows = await cursor.fetchall()
        except Exception as error:
            _LOGGER.debug("Direct panel-group forecast fallback failed: %s", error)
            return {}
        grouped_result = {}
        for row in rows:
            group_name = row["group_name"]
            grouped_result.setdefault(group_name, []).append(
                PanelGroupData(
                    group_name=group_name,
                    prediction_kwh=row["prediction_kwh"] or 0.0,
                    actual_kwh=row["actual_kwh"],
                    target_hour=row["target_hour"],
                    exclude_from_learning=bool(row["exclude_from_learning"]),
                    exclude_from_clean_evaluation=bool(
                        row["exclude_from_clean_evaluation"]
                    ),
                    mppt_throttled=bool(row["mppt_throttled"]),
                    inverter_clipped=bool(row["inverter_clipped"]),
                    mppt_throttle_reason=row["mppt_throttle_reason"],
                    has_weather_alert=bool(row["has_weather_alert"]),
                    weather_alert_type=row["weather_alert_type"],
                    exclude_from_learning_group=bool(
                        row["exclude_from_learning_group"]
                    ),
                    exclusion_reason_group=row["exclusion_reason_group"],
                    snow_covered_group=bool(row["snow_covered_group"]),
                    topology_compatible=(
                        True
                        if row["topology_compatible"] is None
                        else bool(row["topology_compatible"])
                    ),
                    topology_exclusion_reason=row["topology_exclusion_reason"],
                )
            )
            continue
        return grouped_result

    def _build_live_panel_group_fallback(
        self, live_groups_by_name: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        config = _get_config()
        group_name_mapping = config.get(CONF_PANEL_GROUP_NAMES, {})
        if not isinstance(group_name_mapping, dict):
            group_name_mapping = {}
        grouped_result = {}
        for live_group in live_groups_by_name.values():
            original_name = live_group.get("name")
            display_name = group_name_mapping.get(original_name, original_name)
            if display_name:
                raw_live_actual_kwh = live_group.get("actual_today_kwh")
                try:
                    live_actual_kwh = max(0.0, float(raw_live_actual_kwh or 0.0))
                except (TypeError, ValueError):
                    live_actual_kwh = 0.0
                grouped_result[display_name] = {
                    "name": display_name,
                    "original_name": original_name,
                    "prediction_day_kwh": 0.0,
                    "prediction_until_now_kwh": 0.0,
                    "prediction_total_kwh": 0.0,
                    "actual_until_now_kwh": live_actual_kwh,
                    "actual_total_kwh": live_actual_kwh,
                    "clean_prediction_until_now_kwh": 0.0,
                    "clean_actual_until_now_kwh": 0.0,
                    "raw_prediction_until_now_kwh": 0.0,
                    "raw_actual_until_now_kwh": live_actual_kwh,
                    "day_progress_percent": None,
                    "forecast_until_now_accuracy_percent": None,
                    "clean_accuracy_percent": None,
                    "elapsed_hours_count": 0,
                    "clean_hours_count": 0,
                    "excluded_hours_count": 0,
                    "missing_actual_hours_count": 0,
                    "exclusion_reason_breakdown": {},
                    "actual_source": "actual_live_panel_groups",
                    "forecast_available": False,
                    "live_only": True,
                    "live_power_w": live_group.get("power"),
                    "live_quality": live_group.get("quality"),
                    "live_updated_at": live_group.get("updated_at"),
                    "accuracy_percent": None,
                    "raw_accuracy_percent": None,
                    "hourly": [],
                }
        return grouped_result


_DAILY_ASTRONOMY_QUERY = "SELECT MIN(sunrise), MAX(sunset), MIN(solar_noon),\n                                   MAX(daylight_hours), MAX(sun_elevation_deg)\n                            FROM astronomy_cache\n                            WHERE cache_date = ?"


class AnnualForecastView(HomeAssistantView):
    """API endpoint for annual yield forecast. @zara"""

    url = "/api/sfml_stats/annual_forecast"
    name = "api:sfml_stats:annual_forecast"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return annual yield forecast with monthly breakdown. @zara"""
        try:
            ha_config_path = Path(context.HASS.config.path()) if context.HASS else None
            if not ha_config_path:
                return web.json_response(
                    {"success": False, "error": "HA not initialized"}, status=500
                )
            annual_reader = AnnualForecastReader(ha_config_path, hass=context.HASS)
            annual_forecast = await annual_reader.async_get_annual_forecast()
            if not annual_forecast:
                return web.json_response(
                    {"success": False, "error": "Insufficient data for annual forecast"}
                )
            correction_projection = await _energy_correction_projection()
            corrected_energy_by_day = await _corrected_daily_energy_values("2020-01-01")
            finalized_solar_by_day = await _corrected_finalized_solar_values(
                "2020-01-01"
            )
            monthly_results = []
            for month_forecast in annual_forecast.months:
                month_prefix = f"{month_forecast.year}-{month_forecast.month:02d}-"
                month_daily_yields = [
                    day_yield
                    for day_key, day_yield in finalized_solar_by_day.items()
                    if day_key.startswith(month_prefix)
                ]
                solar_corrections_applied = any(
                    (
                        day_key.startswith(month_prefix)
                        and "solar_yield_day_kwh" in energy_values
                        for day_key, energy_values in correction_projection.items()
                    )
                )
                measured_yield_kwh = (
                    sum(month_daily_yields)
                    if solar_corrections_applied and month_daily_yields
                    else month_forecast.measured_yield_kwh
                )
                measured_min_day_kwh = (
                    min(month_daily_yields)
                    if solar_corrections_applied and month_daily_yields
                    else month_forecast.measured_min_day_kwh
                )
                measured_max_day_kwh = (
                    max(month_daily_yields)
                    if solar_corrections_applied and month_daily_yields
                    else month_forecast.measured_max_day_kwh
                )
                corrected_grid_export_kwh = month_forecast.grid_export_kwh + sum(
                    (
                        corrected_energy_by_day[day_key]["grid_export_day_kwh"]
                        - corrected_energy_by_day[day_key]["raw_grid_export_kwh"]
                        for day_key, energy_values in correction_projection.items()
                        if day_key.startswith(month_prefix)
                        and "grid_export_day_kwh" in energy_values
                        and (day_key in corrected_energy_by_day)
                    )
                )
                monthly_results.append(
                    {
                        "month": month_forecast.month,
                        "year": month_forecast.year,
                        "measured_days": month_forecast.measured_days,
                        "total_days": month_forecast.total_days_in_month,
                        "source": month_forecast.source,
                        "projected_yield_kwh": month_forecast.projected_yield_kwh,
                        "measured_yield_kwh": measured_yield_kwh,
                        "avg_daily_kwh": (
                            measured_yield_kwh / len(month_daily_yields)
                            if solar_corrections_applied and month_daily_yields
                            else month_forecast.measured_avg_daily_kwh
                        ),
                        "min_day_kwh": measured_min_day_kwh,
                        "max_day_kwh": measured_max_day_kwh,
                        "avg_peak_w": month_forecast.measured_avg_peak_w,
                        "avg_production_hours": month_forecast.measured_avg_production_hours,
                        "avg_accuracy": month_forecast.measured_avg_accuracy,
                        "self_consumption_kwh": month_forecast.self_consumption_kwh,
                        "grid_export_kwh": corrected_grid_export_kwh,
                        "avg_autarky_percent": month_forecast.avg_autarky_percent,
                    }
                )
                continue
            panel_group_results = []
            for forecast_panel_group in annual_forecast.panel_groups:
                panel_group_results.append(
                    {
                        "group_name": forecast_panel_group.group_name,
                        "global_factor": round(forecast_panel_group.global_factor, 3),
                        "sample_count": forecast_panel_group.sample_count,
                        "confidence": round(forecast_panel_group.confidence, 2),
                    }
                )
            return web.json_response(
                {
                    "success": True,
                    "timestamp": ha_now(context.HASS).isoformat(),
                    "system": {
                        "latitude": round(annual_forecast.latitude, 4),
                        "longitude": round(annual_forecast.longitude, 4),
                        "installed_kwp": annual_forecast.installed_kwp,
                        "panel_groups": panel_group_results,
                    },
                    "data_coverage": {
                        "first_date": annual_forecast.first_data_date.isoformat(),
                        "last_date": annual_forecast.last_data_date.isoformat(),
                        "total_measured_days": annual_forecast.total_measured_days,
                        "measured_months": annual_forecast.measured_months,
                        "estimated_months": annual_forecast.estimated_months,
                        "confidence_percent": annual_forecast.confidence_percent,
                    },
                    "annual": {
                        "yield_kwh": annual_forecast.annual_yield_kwh,
                        "specific_yield_kwh_per_kwp": annual_forecast.specific_yield_kwh_per_kwp,
                        "performance_ratio": annual_forecast.performance_ratio,
                        "best_month": annual_forecast.best_month,
                        "worst_month": annual_forecast.worst_month,
                        "optimistic_kwh": annual_forecast.optimistic_yield_kwh,
                        "pessimistic_kwh": annual_forecast.pessimistic_yield_kwh,
                    },
                    "records": {
                        "best_day_kwh": annual_forecast.record_day_kwh,
                        "best_day_date": annual_forecast.record_day_date,
                        "peak_power_w": annual_forecast.record_peak_w,
                    },
                    "months": monthly_results,
                }
            )
        except Exception as error:
            _LOGGER.error("Error in annual forecast API: %s", error)
            return web.json_response(
                {"success": False, "error": str(error)}, status=500
            )
