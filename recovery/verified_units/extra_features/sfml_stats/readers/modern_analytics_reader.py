# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Read-only SFML analytics reader for the modern forecast UI. @zara"""
from __future__ import annotations
import asyncio
import time
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
import aiosqlite
from ..core.forecast_quality import (
    build_health_dashboard,
    build_heatmap,
    build_milestones,
    build_replay,
    build_trends,
    calculate_model_battle,
    is_valid_hour,
)
from ..storage.db_connection_manager import DatabaseConnectionManager, get_manager
from ..utils.time_utils import ha_now

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
DAILY_COLUMNS = "\n    date, predicted_total_kwh, actual_total_kwh, accuracy_percent,\n    error_kwh, error_percent, total_hours_predicted,\n    hours_with_actual_data, evaluation_predicted_kwh,\n    evaluation_actual_kwh, evaluation_coverage_percent,\n    excluded_hours_count, evaluation_hours_count,\n    production_candidate_hours_count, missing_actual_hours_count,\n    created_at\n"


class ModernAnalyticsReader:
    """Serve deterministic analytics from the SFML database only. @zara"""

    def __init__(self, hass: HomeAssistant, db_path: Path | None = None) -> None:
        self.hass = hass
        self.db_path = (
            db_path
            or Path(hass.config.path()) / "solar_forecast_ml" / "solar_forecast.db"
        )
        self._cache = {}
        self._cache_locks = {}

    async def _fetch_all(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        connection_manager = get_manager()
        if connection_manager is not None:
            if connection_manager.is_connected:
                return await connection_manager.execute_read(query, params)
        async with DatabaseConnectionManager.connect_direct_safe(
            self.db_path, self.hass
        ) as connection:
            async with connection.execute(query, params) as cursor:
                return await cursor.fetchall()
        return None if True else None

    async def _cached(
        self,
        key: str,
        loader: Callable[[], Awaitable[dict[str, Any]]],
        *,
        ttl: float = 60.0,
    ) -> dict[str, Any]:
        cache_checked_at = time.monotonic()
        cached_entry = self._cache.get(key)
        if cached_entry and cache_checked_at - cached_entry[0] < ttl:
            return cached_entry[1]
        cache_lock = self._cache_locks.setdefault(key, asyncio.Lock())
        async with cache_lock:
            cached_entry = self._cache.get(key)
            if cached_entry and cache_checked_at - cached_entry[0] < ttl:
                return cached_entry[1]
            payload = await loader()
            self._cache[key] = (time.monotonic(), payload)
            return payload
        return None if True else None

    async def _latest_summary_date(self) -> date | None:
        rows = await self._fetch_all(
            "SELECT MAX(date) AS latest_date FROM daily_summaries"
        )
        latest_date = rows[0]["latest_date"] if rows else None
        return date.fromisoformat(str(latest_date)) if latest_date else None

    async def _daily_rows(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = await self._fetch_all(
            f"SELECT {DAILY_COLUMNS}\n                FROM daily_summaries\n                WHERE date BETWEEN ? AND ?\n                ORDER BY date",
            (start.isoformat(), end.isoformat()),
        )
        return [dict(row) for row in rows]

    async def get_dashboard(self, period_days: int) -> dict[str, Any]:

        async def load() -> dict[str, Any]:
            latest_summary_date = await self._latest_summary_date()
            calculated_at_iso = ha_now(self.hass).isoformat()
            if latest_summary_date is None:
                return build_health_dashboard(
                    [], period_days=period_days, calculated_at=calculated_at_iso
                )
            history_start_date = latest_summary_date - timedelta(
                days=max(61, period_days * 2 - 1)
            )
            from ..core.dev_mode import is_dev_active as is_dev_active

            if is_dev_active(self.hass):
                daily_rows, model_battle = await asyncio.gather(
                    self._daily_rows(history_start_date, latest_summary_date),
                    self.get_model_battle(30, "morning"),
                )
            else:
                daily_rows = await self._daily_rows(
                    history_start_date, latest_summary_date
                )
                model_battle = None
            payload = build_health_dashboard(
                daily_rows,
                period_days=period_days,
                calculated_at=calculated_at_iso,
                model_battle=model_battle,
            )
            payload["latest_completed_date"] = latest_summary_date.isoformat()
            payload["freshness_days"] = max(
                0, (ha_now(self.hass).date() - latest_summary_date).days
            )
            return payload

        return await self._cached(f"dashboard:{period_days}", load)

    async def _snapshot_model_rows(
        self, start: date, end: date, mode: str
    ) -> list[dict[str, Any]]:
        if mode == "morning":
            snapshot_type_condition = "s.snapshot_type = 'morning_snapshot'"
            target_hour_condition = "1 = 1"
        else:
            snapshot_type_condition = "s.snapshot_type IN (\n                'intraday_reforecast', 'weather_event_reforecast', 'adaptive_correction'\n            )"
            target_hour_condition = (
                "o.target_hour >= COALESCE(o.reforecast_cutoff_hour, 0)"
            )
        rows = await self._fetch_all(
            f"\n            WITH latest_snapshot AS (\n                SELECT o.target_date, MAX(o.snapshot_sequence) AS snapshot_sequence\n                FROM ops_hourly_forecasts AS o\n                JOIN ops_forecast_snapshots AS s\n                  ON s.snapshot_sequence = o.snapshot_sequence\n                WHERE o.ops_track_date = o.target_date\n                  AND o.target_date BETWEEN ? AND ?\n                  AND s.status = 'completed'\n                  AND {snapshot_type_condition}\n                GROUP BY o.target_date\n            )\n            SELECT o.target_date AS date, o.target_hour AS hour,\n                   o.prediction_kwh AS final, o.physics_kwh AS physics,\n                   o.ai_kwh AS ai, h.actual_kwh AS actual,\n                   h.exclude_from_learning, h.exclude_from_clean_evaluation,\n                   h.mppt_throttled, h.inverter_clipped, h.weather_alert_type\n            FROM latest_snapshot AS latest\n            JOIN ops_hourly_forecasts AS o\n              ON o.target_date = latest.target_date\n             AND o.snapshot_sequence = latest.snapshot_sequence\n            LEFT JOIN hourly_predictions AS h\n              ON h.target_date = o.target_date\n             AND h.target_hour = o.target_hour\n            WHERE {target_hour_condition}\n            ORDER BY o.target_date, o.target_hour\n            ",
            (start.isoformat(), end.isoformat()),
        )
        payload = []
        for row in rows:
            hour_row = dict(row)
            hour_row["eligible"] = is_valid_hour(hour_row)
            payload.append(hour_row)
        return payload

    async def get_model_battle(self, days: int, mode: str) -> dict[str, Any]:

        async def load() -> dict[str, Any]:
            latest_summary_date = await self._latest_summary_date()
            if latest_summary_date is None:
                return {"available": False, "reason": "no_daily_data", "mode": mode}
            start_date = latest_summary_date - timedelta(days=days - 1)
            rows = await self._snapshot_model_rows(
                start_date, latest_summary_date, mode
            )
            return calculate_model_battle(
                rows,
                period={
                    "start": start_date.isoformat(),
                    "end": latest_summary_date.isoformat(),
                },
                mode=mode,
            )

        return await self._cached(f"models:{days}:{mode}", load, ttl=120.0)

    async def get_heatmap(self, days: int) -> dict[str, Any]:

        async def load() -> dict[str, Any]:
            latest_summary_date = await self._latest_summary_date()
            if latest_summary_date is None:
                return {
                    "period": None,
                    "metric": "daily_summaries_accuracy_percent",
                    "days": [],
                }
            start_date = latest_summary_date - timedelta(days=days - 1)
            rows = await self._daily_rows(start_date, latest_summary_date)
            return build_heatmap(rows, start=start_date, end=latest_summary_date)

        return await self._cached(f"heatmap:{days}", load, ttl=300.0)

    async def get_milestones(self, days: int) -> dict[str, Any]:

        async def load() -> dict[str, Any]:
            latest_summary_date = await self._latest_summary_date()
            if latest_summary_date is None:
                return {"milestones": [], "valid_days": 0, "period_days": 0}
            start_date = latest_summary_date - timedelta(days=days - 1)
            rows = await self._daily_rows(start_date, latest_summary_date)
            payload = build_milestones(rows)
            payload["period"] = {
                "start": start_date.isoformat(),
                "end": latest_summary_date.isoformat(),
            }
            return payload

        return await self._cached(f"milestones:{days}", load, ttl=300.0)

    async def get_trends(self, days: int) -> dict[str, Any]:

        async def load() -> dict[str, Any]:
            latest_summary_date = await self._latest_summary_date()
            if latest_summary_date is None:
                return {"bucket": "iso_week", "points": []}
            start_date = latest_summary_date - timedelta(days=days - 1)
            rows = await self._daily_rows(start_date, latest_summary_date)
            payload = build_trends(rows)
            payload["period"] = {
                "start": start_date.isoformat(),
                "end": latest_summary_date.isoformat(),
            }
            return payload

        return await self._cached(f"trends:{days}", load, ttl=300.0)

    async def _latest_replay_date(self) -> str | None:
        rows = await self._fetch_all(
            "\n            SELECT MAX(h.target_date) AS replay_date\n            FROM hourly_predictions AS h\n            JOIN daily_summaries AS d ON d.date = h.target_date\n            WHERE h.actual_kwh IS NOT NULL\n              AND EXISTS (\n                  SELECT 1\n                  FROM ops_hourly_forecasts AS o\n                  JOIN ops_forecast_snapshots AS s\n                    ON s.snapshot_sequence = o.snapshot_sequence\n                  WHERE o.target_date = h.target_date\n                    AND o.ops_track_date = o.target_date\n                    AND s.snapshot_type = 'morning_snapshot'\n                    AND s.status = 'completed'\n              )\n            "
        )
        return str(rows[0]["replay_date"]) if rows and rows[0]["replay_date"] else None

    async def _latest_snapshot_sequence(
        self, replay_date: str, mode: str
    ) -> int | None:
        if mode == "morning":
            snapshot_type_condition = "s.snapshot_type = 'morning_snapshot'"
        else:
            snapshot_type_condition = "s.snapshot_type IN (\n                'intraday_reforecast', 'weather_event_reforecast', 'adaptive_correction'\n            )"
        rows = await self._fetch_all(
            f"\n            SELECT MAX(o.snapshot_sequence) AS snapshot_sequence\n            FROM ops_hourly_forecasts AS o\n            JOIN ops_forecast_snapshots AS s\n              ON s.snapshot_sequence = o.snapshot_sequence\n            WHERE o.ops_track_date = ?\n              AND o.target_date = ?\n              AND s.status = 'completed'\n              AND {snapshot_type_condition}\n            ",
            (replay_date, replay_date),
        )
        snapshot_sequence = rows[0]["snapshot_sequence"] if rows else None
        return int(snapshot_sequence) if snapshot_sequence is not None else None

    async def get_replay(self, requested_date: str | None) -> dict[str, Any]:
        replay_date = requested_date or await self._latest_replay_date()
        if replay_date is None:
            return {"available": False, "reason": "no_replay_data"}
        morning_sequence, reforecast_sequence = await asyncio.gather(
            self._latest_snapshot_sequence(replay_date, "morning"),
            self._latest_snapshot_sequence(replay_date, "reforecast"),
        )
        if morning_sequence is None:
            return {
                "available": False,
                "reason": "no_morning_snapshot",
                "date": replay_date,
            }
        actual_rows = await self._fetch_all(
            "\n            SELECT target_hour AS hour, actual_kwh AS actual,\n                   exclude_from_learning, exclude_from_clean_evaluation,\n                   mppt_throttled, inverter_clipped, weather_alert_type\n            FROM hourly_predictions\n            WHERE target_date = ?\n            ORDER BY target_hour\n            ",
            (replay_date,),
        )
        morning_rows = await self._fetch_all(
            "\n            SELECT target_hour AS hour, prediction_kwh AS final,\n                   physics_kwh AS physics, ai_kwh AS ai,\n                   snapshot_created_at, snapshot_type\n            FROM ops_hourly_forecasts\n            WHERE snapshot_sequence = ? AND target_date = ?\n            ORDER BY target_hour\n            ",
            (morning_sequence, replay_date),
        )
        reforecast_rows = []
        if reforecast_sequence is not None:
            reforecast_rows = await self._fetch_all(
                "\n                SELECT target_hour AS hour, prediction_kwh AS reforecast,\n                       reforecast_cutoff_hour, snapshot_created_at, snapshot_type\n                FROM ops_hourly_forecasts\n                WHERE snapshot_sequence = ? AND target_date = ?\n                  AND target_hour >= COALESCE(reforecast_cutoff_hour, 0)\n                ORDER BY target_hour\n                ",
                (reforecast_sequence, replay_date),
            )
        rows_by_hour = {}
        for row in actual_rows:
            rows_by_hour[int(row["hour"])] = dict(row)
        for row in morning_rows:
            rows_by_hour.setdefault(int(row["hour"]), {}).update(dict(row))
        for row in reforecast_rows:
            rows_by_hour.setdefault(int(row["hour"]), {}).update(dict(row))
        replay_rows = []
        for hour in sorted(rows_by_hour):
            hour_row = {"hour": hour, **rows_by_hour[hour]}
            hour_row["eligible"] = is_valid_hour(hour_row)
            if hour_row.get("actual") is None:
                hour_row["data_state"] = "forecast_without_actual"
            elif hour_row.get("final") is None:
                hour_row["data_state"] = "actual_without_forecast"
            elif hour_row["eligible"]:
                hour_row["data_state"] = "valid"
            else:
                hour_row["data_state"] = "excluded"
            replay_rows.append(hour_row)
        payload = build_replay(replay_rows, replay_date=replay_date)
        payload["available"] = True
        payload["morning_snapshot_sequence"] = morning_sequence
        payload["reforecast_snapshot_sequence"] = reforecast_sequence
        payload["data_source"] = "sfml_database"
        return payload
