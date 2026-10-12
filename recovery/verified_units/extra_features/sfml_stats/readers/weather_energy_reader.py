# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Read-only SFML weather and energy analytics for the modern UI. @zara"""

from __future__ import annotations
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from ..core.weather_energy import (
    build_comparable_days,
    build_day_features,
    build_day_story,
    build_production_performance,
    build_weather_energy_insights,
    build_weather_impact,
    calculate_solar_conditions,
)
from .modern_analytics_reader import ModernAnalyticsReader

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class WeatherEnergyReader(ModernAnalyticsReader):
    """Aggregate weather, forecast and energy from the SFML database. @zara"""

    def __init__(self, hass: HomeAssistant, db_path: Path | None = None) -> None:
        super().__init__(hass, db_path)

    async def _latest_common_date(self) -> date | None:
        rows = await self._fetch_all("""
            SELECT MAX(d.date) AS latest_date
            FROM daily_summaries AS d
            JOIN stats_daily_energy AS e ON e.date = d.date
            WHERE EXISTS (
                SELECT 1 FROM weather_precision_daily AS w
                WHERE w.date = d.date AND w.solar_actual IS NOT NULL
            )
            """)
        latest_date_text = rows[0]["latest_date"] if rows else None
        return date.fromisoformat(str(latest_date_text)) if latest_date_text else None

    async def _resolve_date(self, requested: str | None) -> date | None:
        if requested:
            return date.fromisoformat(requested)
        return await self._latest_common_date()

    async def _morning_snapshot(self, target_date: str) -> dict[str, Any] | None:
        rows = await self._fetch_all(
            """
            SELECT s.snapshot_sequence, s.triggered_at,
                   s.weather_refresh_finished_at, s.forecast_generation_finished_at
            FROM ops_forecast_snapshots AS s
            WHERE s.snapshot_date = ?
              AND s.snapshot_type = 'morning_snapshot'
              AND s.status = 'completed'
              AND EXISTS (
                  SELECT 1 FROM ops_hourly_forecasts AS o
                  WHERE o.snapshot_sequence = s.snapshot_sequence
                    AND o.target_date = ? AND o.ops_track_date = o.target_date
              )
            ORDER BY s.snapshot_sequence DESC
            LIMIT 1
            """,
            (target_date, target_date),
        )
        return dict(rows[0]) if rows else None

    async def _day_rows(
        self, target_date: str, snapshot_sequence: int | None
    ) -> list[dict[str, Any]]:
        if snapshot_sequence is not None:
            rows = await self._fetch_all(
                """
                SELECT o.target_hour AS hour,
                       o.prediction_kwh AS forecast,
                       o.physics_kwh AS physics,
                       o.ai_kwh AS ai,
                       h.actual_kwh AS actual,
                       h.exclude_from_learning,
                       h.exclude_from_clean_evaluation,
                       h.mppt_throttled,
                       h.inverter_clipped,
                       h.weather_alert_type,
                       h.data_quarantine_reason,
                       ow.solar_radiation_wm2 AS solar_forecast_wm2,
                       wp.solar_actual AS solar_actual_wm2,
                       wp.clouds_actual AS cloud_actual_percent,
                       wp.temp_actual AS temperature_actual_c,
                       a.clear_sky_radiation_wm2 AS clear_sky_wm2,
                       a.sun_elevation_deg,
                       a.sun_azimuth_deg,
                       a.sunrise, a.sunset, a.daylight_hours
                FROM ops_hourly_forecasts AS o
                LEFT JOIN hourly_predictions AS h
                  ON h.target_date = o.target_date AND h.target_hour = o.target_hour
                LEFT JOIN ops_prediction_weather AS ow
                  ON ow.forecast_row_id = o.forecast_row_id
                LEFT JOIN weather_precision_daily AS wp
                  ON wp.date = o.target_date AND wp.hour = o.target_hour
                LEFT JOIN astronomy_cache AS a
                  ON a.cache_date = o.target_date AND a.hour = o.target_hour
                WHERE o.snapshot_sequence = ? AND o.target_date = ?
                ORDER BY o.target_hour
                """,
                (snapshot_sequence, target_date),
            )
        else:
            rows = await self._fetch_all(
                """
                WITH day_hours AS (
                    SELECT target_hour AS hour FROM hourly_predictions WHERE target_date = ?
                    UNION
                    SELECT hour FROM weather_precision_daily WHERE date = ?
                )
                SELECT k.hour,
                       h.prediction_kwh AS forecast,
                       h.physics_kwh AS physics,
                       h.ai_kwh AS ai,
                       h.actual_kwh AS actual,
                       h.exclude_from_learning,
                       h.exclude_from_clean_evaluation,
                       h.mppt_throttled,
                       h.inverter_clipped,
                       h.weather_alert_type,
                       h.data_quarantine_reason,
                       wp.solar_forecast AS solar_forecast_wm2,
                       wp.solar_actual AS solar_actual_wm2,
                       wp.clouds_actual AS cloud_actual_percent,
                       wp.temp_actual AS temperature_actual_c,
                       a.clear_sky_radiation_wm2 AS clear_sky_wm2,
                       a.sun_elevation_deg,
                       a.sun_azimuth_deg,
                       a.sunrise, a.sunset, a.daylight_hours
                FROM day_hours AS k
                LEFT JOIN hourly_predictions AS h
                  ON h.target_date = ? AND h.target_hour = k.hour
                LEFT JOIN weather_precision_daily AS wp
                  ON wp.date = ? AND wp.hour = k.hour
                LEFT JOIN astronomy_cache AS a
                  ON a.cache_date = ? AND a.hour = k.hour
                ORDER BY k.hour
                """,
                (target_date, target_date, target_date, target_date, target_date),
            )
        return [dict(row) for row in rows]

    async def get_day(self, requested_date: str | None) -> dict[str, Any]:
        target_date = await self._resolve_date(requested_date)
        if target_date is None:
            return {"available": False, "reason": "no_common_weather_energy_day"}
        target_date_iso = target_date.isoformat()

        async def load() -> dict[str, Any]:
            snapshot = await self._morning_snapshot(target_date_iso)
            rows = await self._day_rows(
                target_date_iso,
                int(snapshot["snapshot_sequence"]) if snapshot else None,
            )
            story = build_day_story(rows, target_date=target_date_iso)
            story.update(
                {
                    "available": any(
                        (hour["state"] != "missing" for hour in story["hours"])
                    ),
                    "forecast_basis": "morning_snapshot"
                    if snapshot
                    else "weather_precision_archive",
                    "snapshot": snapshot,
                    "data_source": "sfml_database",
                }
            )
            return story

        return await self._cached(
            f"weather-energy:day:{target_date_iso}", load, ttl=120.0
        )

    async def get_summary(self, requested_date: str | None) -> dict[str, Any]:
        target_date = await self._resolve_date(requested_date)
        if target_date is None:
            return {"available": False, "reason": "no_common_weather_energy_day"}
        target_date_iso = target_date.isoformat()

        async def load() -> dict[str, Any]:
            day = await self.get_day(target_date_iso)
            summary_rows = await self._fetch_all(
                """
                SELECT e.*, d.predicted_total_kwh, d.actual_total_kwh,
                       d.accuracy_percent, d.production_hours,
                       d.peak_power_time, d.evaluation_coverage_percent,
                       a.installed_capacity_kwp
                FROM stats_daily_energy AS e
                LEFT JOIN daily_summaries AS d ON d.date = e.date
                LEFT JOIN astronomy_system_info AS a ON a.id = (
                    SELECT id FROM astronomy_system_info ORDER BY updated_at DESC LIMIT 1
                )
                WHERE e.date = ?
                """,
                (target_date_iso,),
            )
            summary = dict(summary_rows[0]) if summary_rows else {}
            month_start = target_date.replace(day=1).isoformat()
            year_start = target_date.replace(month=1, day=1).isoformat()
            yield_rows = await self._fetch_all(
                """
                SELECT
                    SUM(CASE WHEN date BETWEEN ? AND ? THEN solar_yield_kwh END) AS month_yield,
                    SUM(CASE WHEN date BETWEEN ? AND ? THEN solar_yield_kwh END) AS year_yield
                FROM stats_daily_energy
                """,
                (month_start, target_date_iso, year_start, target_date_iso),
            )
            ranked_days = await self._fetch_all(
                """SELECT date, solar_yield_kwh FROM stats_daily_energy
                   WHERE date BETWEEN ? AND ? ORDER BY solar_yield_kwh DESC, date""",
                (month_start, target_date_iso),
            )
            monthly_rank = next(
                (
                    rank
                    for rank, row in enumerate(ranked_days, 1)
                    if str(row["date"]) == target_date_iso
                ),
                None,
            )
            solar_conditions = calculate_solar_conditions(day.get("hours", []))
            yields = dict(yield_rows[0]) if yield_rows else {}
            production = build_production_performance(
                summary,
                summary,
                capacity_kwp=summary.get("installed_capacity_kwp"),
                month_yield_kwh=yields.get("month_yield"),
                year_yield_kwh=yields.get("year_yield"),
                solar_conditions=solar_conditions,
            )
            insights = build_weather_energy_insights(
                day,
                production,
                solar_conditions,
                monthly_rank=monthly_rank,
                monthly_days=len(ranked_days),
            )
            latest_date = await self._latest_common_date()
            return {
                "available": day.get("available", False),
                "date": target_date_iso,
                "latest_available_date": latest_date.isoformat()
                if latest_date
                else None,
                "solar_conditions": solar_conditions,
                "production": production,
                "forecast_quality": {
                    "accuracy_percent": summary.get("accuracy_percent"),
                    "evaluation_coverage_percent": summary.get(
                        "evaluation_coverage_percent"
                    ),
                },
                "astronomy": day.get("astronomy", {}),
                "data_completeness": {
                    "weather_percent": day.get("weather_coverage_percent"),
                    "energy_percent": day.get("energy_coverage_percent"),
                    "valid_comparison_hours": day.get("valid_energy_hours"),
                },
                "largest_deviation": day.get("largest_deviation"),
                "insights": insights,
                "data_source": "sfml_database",
                "weather_basis": "observed_archive",
                "forecast_basis": day.get("forecast_basis"),
            }

        return await self._cached(
            f"weather-energy:summary:{target_date_iso}", load, ttl=120.0
        )

    async def get_impact(self, days: int) -> dict[str, Any]:
        latest_date = await self._latest_common_date()
        if latest_date is None:
            return {"available": False, "reason": "no_common_weather_energy_day"}
        start_date = latest_date - timedelta(days=days - 1)

        async def load() -> dict[str, Any]:
            rows = await self._fetch_all(
                """
                WITH latest_morning AS (
                    SELECT o.target_date, MAX(o.snapshot_sequence) AS snapshot_sequence
                    FROM ops_hourly_forecasts AS o
                    JOIN ops_forecast_snapshots AS s
                      ON s.snapshot_sequence = o.snapshot_sequence
                    WHERE o.target_date BETWEEN ? AND ?
                      AND o.ops_track_date = o.target_date
                      AND s.snapshot_type = 'morning_snapshot'
                      AND s.status = 'completed'
                    GROUP BY o.target_date
                )
                SELECT o.target_date AS date, o.target_hour AS hour,
                       o.prediction_kwh AS forecast, o.physics_kwh AS physics,
                       o.ai_kwh AS ai, h.actual_kwh AS actual,
                       h.exclude_from_learning, h.exclude_from_clean_evaluation,
                       h.mppt_throttled, h.inverter_clipped,
                       h.weather_alert_type, h.data_quarantine_reason,
                       w.clouds_actual AS cloud_actual_percent,
                       w.solar_actual AS solar_actual_wm2,
                       a.clear_sky_radiation_wm2 AS clear_sky_wm2
                FROM latest_morning AS latest
                JOIN ops_hourly_forecasts AS o
                  ON o.target_date = latest.target_date
                 AND o.snapshot_sequence = latest.snapshot_sequence
                LEFT JOIN hourly_predictions AS h
                  ON h.target_date = o.target_date AND h.target_hour = o.target_hour
                LEFT JOIN weather_precision_daily AS w
                  ON w.date = o.target_date AND w.hour = o.target_hour
                LEFT JOIN astronomy_cache AS a
                  ON a.cache_date = o.target_date AND a.hour = o.target_hour
                ORDER BY o.target_date, o.target_hour
                """,
                (start_date.isoformat(), latest_date.isoformat()),
            )
            result = build_weather_impact(
                [dict(row) for row in rows],
                period={
                    "start": start_date.isoformat(),
                    "end": latest_date.isoformat(),
                },
            )
            result.update(
                {
                    "available": True,
                    "requested_days": days,
                    "data_source": "sfml_database",
                }
            )
            return result

        return await self._cached(f"weather-energy:impact:{days}", load, ttl=300.0)

    async def get_comparable_days(
        self, requested_date: str | None, days: int = 365
    ) -> dict[str, Any]:
        target_date = await self._resolve_date(requested_date)
        if target_date is None:
            return {
                "available": False,
                "reason": "no_common_weather_energy_day",
                "days": [],
            }
        start_date = target_date - timedelta(days=days)
        target_date_iso = target_date.isoformat()

        async def load() -> dict[str, Any]:
            rows = await self._fetch_all(
                """
                SELECT w.date, w.hour,
                       w.solar_actual AS solar_actual_wm2,
                       w.clouds_actual AS cloud_actual_percent,
                       w.temp_actual AS temperature_actual_c,
                       h.actual_kwh AS actual,
                       a.clear_sky_radiation_wm2 AS clear_sky_wm2,
                       a.daylight_hours
                FROM weather_precision_daily AS w
                LEFT JOIN hourly_predictions AS h
                  ON h.target_date = w.date AND h.target_hour = w.hour
                LEFT JOIN astronomy_cache AS a
                  ON a.cache_date = w.date AND a.hour = w.hour
                WHERE w.date BETWEEN ? AND ?
                ORDER BY w.date, w.hour
                """,
                (start_date.isoformat(), target_date_iso),
            )
            energy_rows = await self._fetch_all(
                """
                SELECT e.date, e.solar_yield_kwh,
                       d.accuracy_percent
                FROM stats_daily_energy AS e
                LEFT JOIN daily_summaries AS d ON d.date = e.date
                WHERE e.date BETWEEN ? AND ?
                """,
                (start_date.isoformat(), target_date_iso),
            )
            weather_rows = [dict(row) for row in rows]
            rows_by_date = defaultdict(list)
            for row in weather_rows:
                rows_by_date[str(row["date"])].append(row)
            conditions_by_date = {
                day: calculate_solar_conditions(day_rows)
                for day, day_rows in rows_by_date.items()
            }
            energy_by_date = {
                str(row["date"]): {
                    "yield_kwh": row["solar_yield_kwh"],
                    "forecast_accuracy_percent": row["accuracy_percent"],
                    "conditions_score": conditions_by_date.get(
                        str(row["date"]), {}
                    ).get("score"),
                }
                for row in energy_rows
            }
            day_features = build_day_features(weather_rows, energy_by_date)
            result = build_comparable_days(day_features, target_date=target_date_iso)
            result.update({"period_days": days, "data_source": "sfml_database"})
            return result

        return await self._cached(
            f"weather-energy:comparable:{target_date_iso}:{days}", load, ttl=300.0
        )
