# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Solar data reader for SFML Stats. @zara"""
from __future__ import annotations
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING, AsyncIterator
import aiosqlite
from ..const import (
    DOMAIN,
    RUNTIME_READY_KEY,
    RUNTIME_READY_TIMEOUT_SECONDS,
    SOLAR_FORECAST_DB,
)
from ..utils.time_utils import ha_timezone, ha_today

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from ..storage.db_connection_manager import DatabaseConnectionManager
_LOGGER = logging.getLogger(__name__)


def _average_present(values: list[float | None]) -> float | None:
    """Average only present values. @zara"""
    present_values = [value for value in values if value is not None]
    if not present_values:
        return
    return sum(present_values) / len(present_values)


@dataclass
class DailySummary:
    """Daily solar production summary. @zara"""

    date: date
    day_of_week: int
    month: int
    season: str
    predicted_total_kwh: float
    actual_total_kwh: float
    accuracy_percent: float | None
    error_kwh: float
    production_hours: int
    peak_hour: int
    peak_kwh: float
    morning_accuracy: float | None = None
    midday_accuracy: float | None = None
    afternoon_accuracy: float | None = None
    ml_mae: float | None = None
    ml_rmse: float | None = None
    ml_r2_score: float | None = None
    shadow_hours_count: int = 0
    shadow_loss_kwh: float = 0.0
    frost_hours_count: int = 0
    evaluation_predicted_kwh: float | None = None
    evaluation_actual_kwh: float | None = None
    evaluation_coverage_percent: float | None = None
    excluded_hours_count: int = 0
    excluded_mppt_hours_count: int = 0
    evaluation_hours_count: int = 0
    production_candidate_hours_count: int = 0
    missing_actual_hours_count: int = 0
    excluded_weather_alert_hours_count: int = 0
    excluded_inverter_clipped_hours_count: int = 0
    excluded_reason_breakdown_json: str | None = None
    raw_data: dict = field(default_factory=dict)


@dataclass
class HourlyPrediction:
    """Hourly prediction data. @zara"""

    target_datetime: datetime
    target_hour: int
    target_date: date
    prediction_kwh: float
    actual_kwh: float | None
    accuracy_percent: float | None
    error_kwh: float | None
    prediction_method: str
    ml_contribution_percent: float
    confidence: float
    temperature: float | None = None
    solar_radiation: float | None = None
    clouds: float | None = None
    sun_elevation: float | None = None
    theoretical_max_kwh: float | None = None
    tfs_kwh: float | None = None
    tfs_weight: float | None = None
    ai_kwh: float | None = None
    physics_kwh: float | None = None
    lstm_kwh: float | None = None
    ridge_kwh: float | None = None


@dataclass
class OperationalHybridHourlyForecast:
    """Operational hybrid hourly forecast data from OPS sidecar tables. @zara"""

    target_hour: int
    target_date: date
    prediction_kwh: float
    snapshot_type: str | None = None
    source_track: str | None = None
    snapshot_sequence: int | None = None
    trigger_source: str | None = None
    reforecast_cutoff_hour: int | None = None


@dataclass
class OperationalHybridForecast:
    """Operational hybrid forecast payload for a single day. @zara"""

    target_date: date
    total_kwh: float
    hourly: list[OperationalHybridHourlyForecast] = field(default_factory=list)
    ops_today_row_kwh: float | None = None
    ops_today_row_scope: str | None = None


@dataclass
class ConservativeHourlyForecast:
    """Conservative planning forecast data from SFML tracking."""

    target_hour: int
    target_date: date
    prediction_kwh: float


@dataclass
class ConservativePlanningForecast:
    """Conservative planning forecast payload for a single day."""

    target_date: date
    total_kwh: float
    hourly: list[ConservativeHourlyForecast] = field(default_factory=list)
    updated_at: str | None = None


@dataclass
class ModelState:
    """ML model state. @zara"""

    model_loaded: bool
    algorithm_used: str
    training_samples: int
    current_accuracy: float
    last_training: datetime | None
    peak_power_kw: float
    feature_weights: dict[str, float] = field(default_factory=dict)
    feature_importance: dict[str, float] = field(default_factory=dict)


@dataclass
class PanelGroupData:
    """Panel group prediction and actual data. @zara"""

    group_name: str
    prediction_kwh: float
    actual_kwh: float | None = None
    target_hour: int | None = None
    exclude_from_learning: bool = False
    exclude_from_clean_evaluation: bool = False
    mppt_throttled: bool = False
    inverter_clipped: bool = False
    mppt_throttle_reason: str | None = None
    has_weather_alert: bool = False
    weather_alert_type: str | None = None
    exclude_from_learning_group: bool = False
    exclusion_reason_group: str | None = None
    snow_covered_group: bool = False
    topology_compatible: bool = True
    topology_exclusion_reason: str | None = None


@dataclass
class DailyForecast:
    """Daily forecast data from SFML database, read-only. @zara"""

    forecast_type: str
    forecast_date: date
    prediction_kwh: float
    prediction_kwh_raw: float | None = None
    locked: bool = False
    source: str | None = None
    created_at: datetime | None = None


class SolarDataReader:
    """Reads and parses data from Solar Forecast ML SQLite database. @zara"""

    _db_manager: DatabaseConnectionManager | None = None

    def __init__(
        self,
        config_path: Path,
        db_manager: DatabaseConnectionManager | None = None,
        hass: HomeAssistant | None = None,
    ) -> None:
        """Initialize the solar data reader. @zara"""
        self._config_path = config_path
        self._db_path = config_path / SOLAR_FORECAST_DB
        self._hass = hass
        if db_manager is not None:
            SolarDataReader._db_manager = db_manager
        return None if True else None

    @property
    def is_available(self) -> bool:
        """Check if Solar Forecast ML database is available. @zara"""
        if self._db_manager is not None:
            return self._db_manager.is_available
        return self._db_path.exists()

    @asynccontextmanager
    async def _get_db_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get a database connection from the manager. @zara"""
        from ..storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        if self._hass is not None:
            runtime_ready = self._hass.data.get(DOMAIN, {}).get(RUNTIME_READY_KEY)
            if isinstance(runtime_ready, asyncio.Event):
                if not runtime_ready.is_set():
                    _LOGGER.debug(
                        "SolarDataReader: Waiting for STATS runtime initialization"
                    )
                    try:
                        await (asyncio.wait_for if True else None)(
                            runtime_ready.wait(), timeout=RUNTIME_READY_TIMEOUT_SECONDS
                        )
                    except TimeoutError as error:
                        raise RuntimeError(
                            "STATS runtime did not become ready for database access"
                        ) from error
        db_manager = get_manager()
        if db_manager is not None:
            try:
                if not db_manager.is_connected:
                    await db_manager.connect()
                if db_manager.is_connected:
                    _LOGGER.debug("SolarDataReader: Using database connection manager")
                    yield (await db_manager.get_connection())
                    return
            except Exception as error:
                _LOGGER.warning(
                    "SolarDataReader: Database manager auto-reconnect failed: %s", error
                )
        if self._hass is not None:
            raise RuntimeError(
                "STATS database manager unavailable in managed Home Assistant runtime"
            )
        _LOGGER.warning(
            "SolarDataReader: Database manager not available, using direct connection"
        )
        async with (DatabaseConnectionManager.connect_direct_safe if True else None)(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    async def async_get_daily_summaries(
        self,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[DailySummary]:
        """Read daily summaries from database. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return []
        try:
            async with self._get_db_connection() as connection:
                query = "\n                    SELECT\n                        ds.date, ds.day_of_week, ds.month, ds.season,\n                        ds.predicted_total_kwh, ds.actual_total_kwh,\n                        ds.accuracy_percent, ds.error_kwh, ds.production_hours,\n                        ds.peak_hour, ds.peak_kwh, ds.peak_power_w,\n                        ds.ml_mae, ds.ml_rmse, ds.ml_r2_score,\n                        ds.evaluation_predicted_kwh, ds.evaluation_actual_kwh,\n                        ds.evaluation_coverage_percent, ds.excluded_hours_count,\n                        ds.excluded_mppt_hours_count, ds.evaluation_hours_count,\n                        ds.production_candidate_hours_count, ds.missing_actual_hours_count,\n                        ds.excluded_weather_alert_hours_count,\n                        ds.excluded_inverter_clipped_hours_count,\n                        ds.excluded_reason_breakdown_json\n                    FROM daily_summaries ds\n                "
                query_params = []
                where_clauses = []
                if start_date:
                    where_clauses.append("ds.date >= ?")
                    query_params.append(start_date.isoformat())
                if end_date:
                    where_clauses.append("ds.date <= ?")
                    query_params.append(end_date.isoformat())
                if where_clauses:
                    query += " WHERE " + " AND ".join(where_clauses)
                query += " ORDER BY ds.date DESC"
                if days is not None:
                    query += " LIMIT ?"
                    query_params.append(days)
                async with connection.execute(query, query_params) as cursor:
                    rows = await cursor.fetchall()
                daily_summaries = []
                date_strs = [summary_row["date"] for summary_row in rows]
                time_windows_by_date = await self._get_time_windows_batch(
                    connection, date_strs
                )
                frost_by_date = await self._get_frost_analysis_batch(
                    connection, date_strs
                )
                for row in rows:
                    try:
                        summary_date = (
                            date.fromisoformat(row["date"])
                            if isinstance(row["date"], str)
                            else row["date"]
                        )
                        time_windows = time_windows_by_date.get(row["date"], {})
                        frost_analysis = frost_by_date.get(row["date"])
                        summary = DailySummary(
                            date=summary_date,
                            day_of_week=row["day_of_week"] or 0,
                            month=row["month"] or 1,
                            season=row["season"] or "unknown",
                            predicted_total_kwh=row["predicted_total_kwh"] or 0.0,
                            actual_total_kwh=row["actual_total_kwh"] or 0.0,
                            accuracy_percent=row["accuracy_percent"],
                            error_kwh=row["error_kwh"] or 0.0,
                            production_hours=row["production_hours"] or 0,
                            peak_hour=row["peak_hour"] or 12,
                            peak_kwh=row["peak_kwh"] or 0.0,
                            morning_accuracy=time_windows.get("morning_7_10", {}).get(
                                "accuracy"
                            ),
                            midday_accuracy=time_windows.get("midday_11_14", {}).get(
                                "accuracy"
                            ),
                            afternoon_accuracy=time_windows.get(
                                "afternoon_15_17", {}
                            ).get("accuracy"),
                            ml_mae=row["ml_mae"],
                            ml_rmse=row["ml_rmse"],
                            ml_r2_score=row["ml_r2_score"],
                            frost_hours_count=(
                                frost_analysis.get("total_affected_hours", 0)
                                if frost_analysis
                                else 0
                            ),
                            evaluation_predicted_kwh=row["evaluation_predicted_kwh"],
                            evaluation_actual_kwh=row["evaluation_actual_kwh"],
                            evaluation_coverage_percent=row[
                                "evaluation_coverage_percent"
                            ],
                            excluded_hours_count=row["excluded_hours_count"] or 0,
                            excluded_mppt_hours_count=row["excluded_mppt_hours_count"]
                            or 0,
                            evaluation_hours_count=row["evaluation_hours_count"] or 0,
                            production_candidate_hours_count=row[
                                "production_candidate_hours_count"
                            ]
                            or 0,
                            missing_actual_hours_count=row["missing_actual_hours_count"]
                            or 0,
                            excluded_weather_alert_hours_count=row[
                                "excluded_weather_alert_hours_count"
                            ]
                            or 0,
                            excluded_inverter_clipped_hours_count=row[
                                "excluded_inverter_clipped_hours_count"
                            ]
                            or 0,
                            excluded_reason_breakdown_json=row[
                                "excluded_reason_breakdown_json"
                            ],
                            raw_data=dict(row),
                        )
                        daily_summaries.append(summary)
                    except Exception as error:
                        _LOGGER.warning("Error parsing summary row: %s", error)
                        continue
                return daily_summaries
        except Exception as error:
            _LOGGER.error("Error reading daily summaries from database: %s", error)
            return []
        return None if True else None

    async def _get_time_windows(
        self, conn: aiosqlite.Connection, date_str: str
    ) -> dict[str, dict]:
        """Get time windows for a specific date. @zara"""
        try:
            async with conn.execute(
                "SELECT window_name, predicted_kwh, actual_kwh, accuracy, stable, hours_count\n                   FROM daily_summary_time_windows WHERE date = ?",
                (date_str,),
            ) as cursor:
                rows = await cursor.fetchall()
            return {
                row["window_name"]: {
                    "predicted_kwh": row["predicted_kwh"],
                    "actual_kwh": row["actual_kwh"],
                    "accuracy": row["accuracy"],
                    "stable": bool(row["stable"]),
                    "hours_count": row["hours_count"],
                }
                for row in rows
            }
        except Exception:
            return {}

    async def _get_frost_analysis(
        self, conn: aiosqlite.Connection, date_str: str
    ) -> dict | None:
        """Get frost analysis for a specific date. @zara"""
        try:
            async with conn.execute(
                "SELECT hours_analyzed, frost_detected, total_affected_hours,\n                          heavy_frost_hours, light_frost_hours\n                   FROM daily_summary_frost_analysis WHERE date = ?",
                (date_str,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return
            return {
                "hours_analyzed": row["hours_analyzed"],
                "frost_detected": bool(row["frost_detected"]),
                "total_affected_hours": row["total_affected_hours"],
                "heavy_frost_hours": row["heavy_frost_hours"],
                "light_frost_hours": row["light_frost_hours"],
            }
        except Exception:
            return

    async def _get_time_windows_batch(
        self, conn: aiosqlite.Connection, date_strs: list[str]
    ) -> dict[str, dict[str, dict]]:
        """Get time windows for multiple dates in one query. @zara"""
        if not date_strs:
            return {}
        try:
            placeholders = ",".join(("?" for date_str in date_strs))
            async with conn.execute(
                f"SELECT date, window_name, predicted_kwh, actual_kwh, accuracy, stable, hours_count\n                   FROM daily_summary_time_windows WHERE date IN ({placeholders})",
                date_strs,
            ) as cursor:
                rows = await cursor.fetchall()
            windows_by_date = {}
            for row in rows:
                date_key = row["date"]
                if date_key not in windows_by_date:
                    windows_by_date[date_key] = {}
                windows_by_date[date_key][row["window_name"]] = {
                    "predicted_kwh": row["predicted_kwh"],
                    "actual_kwh": row["actual_kwh"],
                    "accuracy": row["accuracy"],
                    "stable": bool(row["stable"]),
                    "hours_count": row["hours_count"],
                }
            return windows_by_date
        except Exception:
            return {}

    async def _get_frost_analysis_batch(
        self, conn: aiosqlite.Connection, date_strs: list[str]
    ) -> dict[str, dict]:
        """Get frost analysis for multiple dates in one query. @zara"""
        if not date_strs:
            return {}
        try:
            placeholders = ",".join(("?" for date_str in date_strs))
            async with conn.execute(
                f"SELECT date, hours_analyzed, frost_detected, total_affected_hours,\n                          heavy_frost_hours, light_frost_hours\n                   FROM daily_summary_frost_analysis WHERE date IN ({placeholders})",
                date_strs,
            ) as cursor:
                rows = await cursor.fetchall()
            return {
                row["date"]: {
                    "hours_analyzed": row["hours_analyzed"],
                    "frost_detected": bool(row["frost_detected"]),
                    "total_affected_hours": row["total_affected_hours"],
                    "heavy_frost_hours": row["heavy_frost_hours"],
                    "light_frost_hours": row["light_frost_hours"],
                }
                for row in rows
            }
        except Exception:
            return {}

    async def async_get_hourly_predictions(
        self, target_date: date | None = None, include_no_production: bool = False
    ) -> list[HourlyPrediction]:
        """Read hourly predictions from database. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return []
        try:
            async with self._get_db_connection() as connection:
                query = "\n                    SELECT\n                        hp.target_datetime, hp.target_hour, hp.target_date,\n                        hp.prediction_kwh, hp.actual_kwh, hp.accuracy_percent,\n                        hp.error_kwh, hp.prediction_method, hp.ml_contribution_percent,\n                        hp.confidence,\n                        hp.tfs_kwh, hp.tfs_weight, hp.ai_kwh,\n                        hp.physics_kwh, hp.lstm_kwh, hp.ridge_kwh,\n                        pw.temperature, pw.solar_radiation_wm2, pw.clouds,\n                        pa.sun_elevation_deg, pa.theoretical_max_kwh\n                    FROM hourly_predictions hp\n                    LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                        AND pw.weather_type = 'forecast'\n                    LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                "
                query_params = []
                where_clauses = []
                if target_date:
                    where_clauses.append("hp.target_date = ?")
                    query_params.append(target_date.isoformat())
                if not include_no_production:
                    where_clauses.append(
                        "(hp.prediction_kwh > 0 OR (hp.actual_kwh IS NOT NULL AND hp.actual_kwh > 0))"
                    )
                if where_clauses:
                    query += " WHERE " + " AND ".join(where_clauses)
                query += " ORDER BY hp.target_datetime"
                async with connection.execute(query, query_params) as cursor:
                    rows = await cursor.fetchall()
                hourly_predictions = []
                for row in rows:
                    try:
                        prediction_date = (
                            date.fromisoformat(row["target_date"])
                            if isinstance(row["target_date"], str)
                            else row["target_date"]
                        )
                        prediction_datetime = (
                            datetime.fromisoformat(row["target_datetime"])
                            if isinstance(row["target_datetime"], str)
                            else row["target_datetime"]
                        )
                        if prediction_datetime.tzinfo is None:
                            prediction_datetime = prediction_datetime.replace(
                                tzinfo=ha_timezone(self._hass)
                            )
                        prediction = HourlyPrediction(
                            target_datetime=prediction_datetime,
                            target_hour=row["target_hour"] or 0,
                            target_date=prediction_date,
                            prediction_kwh=row["prediction_kwh"] or 0.0,
                            actual_kwh=row["actual_kwh"],
                            accuracy_percent=row["accuracy_percent"],
                            error_kwh=row["error_kwh"],
                            prediction_method=row["prediction_method"] or "unknown",
                            ml_contribution_percent=row["ml_contribution_percent"]
                            or 0.0,
                            confidence=row["confidence"] or 0.0,
                            temperature=row["temperature"],
                            solar_radiation=row["solar_radiation_wm2"],
                            clouds=row["clouds"],
                            sun_elevation=row["sun_elevation_deg"],
                            theoretical_max_kwh=row["theoretical_max_kwh"],
                            tfs_kwh=row["tfs_kwh"],
                            tfs_weight=row["tfs_weight"],
                            ai_kwh=row["ai_kwh"],
                            physics_kwh=row["physics_kwh"],
                            lstm_kwh=row["lstm_kwh"],
                            ridge_kwh=row["ridge_kwh"],
                        )
                        hourly_predictions.append(prediction)
                    except Exception as error:
                        _LOGGER.warning("Error parsing prediction row: %s", error)
                        continue
                return hourly_predictions
        except Exception as error:
            _LOGGER.error("Error reading hourly predictions from database: %s", error)
            return []
        return None if True else None

    async def async_get_operational_hybrid_forecast(
        self, target_date: date
    ) -> OperationalHybridForecast | None:
        """Read active operational hybrid forecast rows from OPS tables. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "\n                    SELECT\n                        target_hour, target_date, prediction_kwh, snapshot_type,\n                        source_track, snapshot_sequence, trigger_source,\n                        reforecast_cutoff_hour\n                    FROM ops_hourly_forecasts\n                    WHERE target_date = ?\n                      AND is_active = TRUE\n                    ORDER BY target_hour\n                    ",
                    (target_date.isoformat(),),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    return
                hourly_forecasts = []
                forecast_total_kwh = 0.0
                for row in rows:
                    forecast_date = (
                        date.fromisoformat(row["target_date"])
                        if isinstance(row["target_date"], str)
                        else row["target_date"]
                    )
                    hour_prediction_kwh = float(row["prediction_kwh"] or 0.0)
                    hourly_forecasts.append(
                        OperationalHybridHourlyForecast(
                            target_hour=int(row["target_hour"] or 0),
                            target_date=forecast_date,
                            prediction_kwh=hour_prediction_kwh,
                            snapshot_type=row["snapshot_type"],
                            source_track=row["source_track"],
                            snapshot_sequence=row["snapshot_sequence"],
                            trigger_source=row["trigger_source"],
                            reforecast_cutoff_hour=row["reforecast_cutoff_hour"],
                        )
                    )
                    forecast_total_kwh += hour_prediction_kwh
                async with connection.execute(
                    "\n                    SELECT prediction_kwh, snapshot_type\n                    FROM ops_daily_forecasts\n                    WHERE forecast_type = 'today'\n                      AND forecast_date = ?\n                      AND is_active = TRUE\n                    ORDER BY snapshot_sequence DESC\n                    LIMIT 1\n                    ",
                    (target_date.isoformat(),),
                ) as cursor:
                    daily_forecast_row = await cursor.fetchone()
                ops_today_kwh = None
                ops_today_scope = None
                if daily_forecast_row:
                    ops_today_kwh = float(daily_forecast_row["prediction_kwh"] or 0.0)
                    ops_today_scope = (
                        "rest_of_day_total"
                        if daily_forecast_row["snapshot_type"]
                        in ("intraday_reforecast", "weather_event_reforecast")
                        else "full_day_snapshot"
                    )
                return OperationalHybridForecast(
                    target_date=target_date,
                    total_kwh=round(forecast_total_kwh, 3),
                    hourly=hourly_forecasts,
                    ops_today_row_kwh=ops_today_kwh,
                    ops_today_row_scope=ops_today_scope,
                )
        except Exception as error:
            _LOGGER.error(
                "Error reading operational hybrid forecast from database: %s", error
            )
            return
        return None if True else None

    async def async_get_conservative_planning_forecast(
        self, target_date: date
    ) -> ConservativePlanningForecast | None:
        """Read the conservative planning forecast persisted by SFML."""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "PRAGMA table_info(daily_forecast_tracking)"
                ) as cursor:
                    tracking_columns = {row["name"] for row in await cursor.fetchall()}
                required_columns = {
                    "conservative_planning_forecast_kwh",
                    "conservative_planning_forecast_hours_json",
                }
                if not required_columns.issubset(tracking_columns):
                    return
                async with connection.execute(
                    "\n                    SELECT conservative_planning_forecast_kwh,\n                           conservative_planning_forecast_updated_at,\n                           conservative_planning_forecast_hours_json\n                    FROM daily_forecast_tracking\n                    WHERE date = ?\n                    ORDER BY id DESC\n                    LIMIT 1\n                    ",
                    (target_date.isoformat(),),
                ) as cursor:
                    row = await cursor.fetchone()
                if not row or row["conservative_planning_forecast_kwh"] is None:
                    return
                async with connection.execute(
                    "\n                    SELECT target_hour, prediction_kwh\n                    FROM hourly_predictions\n                    WHERE target_date = ?\n                    ORDER BY target_hour\n                    ",
                    (target_date.isoformat(),),
                ) as cursor:
                    prediction_rows = await cursor.fetchall()
                base_predictions_by_hour = {
                    int(prediction_row["target_hour"] or 0): max(
                        0.0, float(prediction_row["prediction_kwh"] or 0.0)
                    )
                    for prediction_row in prediction_rows
                }
                hourly_forecasts = []
                hourly_forecast_json = row["conservative_planning_forecast_hours_json"]
                if hourly_forecast_json:
                    try:
                        hourly_forecast_data = json.loads(hourly_forecast_json)
                    except (TypeError, ValueError):
                        hourly_forecast_data = {}
                    if isinstance(hourly_forecast_data, dict):
                        for hour_key, value in sorted(hourly_forecast_data.items()):
                            try:
                                target_hour = int(str(hour_key).split(":", 1)[0])
                                planning_prediction_kwh = max(0.0, float(value or 0.0))
                            except (TypeError, ValueError):
                                continue
                            base_prediction_kwh = base_predictions_by_hour.get(
                                target_hour
                            )
                            if base_prediction_kwh is not None:
                                planning_prediction_kwh = min(
                                    planning_prediction_kwh, base_prediction_kwh
                                )
                            hourly_forecasts.append(
                                ConservativeHourlyForecast(
                                    target_hour=target_hour,
                                    target_date=target_date,
                                    prediction_kwh=round(planning_prediction_kwh, 3),
                                )
                            )
                forecast_total_kwh = max(
                    0.0, float(row["conservative_planning_forecast_kwh"] or 0.0)
                )
                if hourly_forecasts:
                    forecast_total_kwh = sum(
                        (
                            hourly_forecast.prediction_kwh
                            for hourly_forecast in hourly_forecasts
                        )
                    )
                elif base_predictions_by_hour:
                    forecast_total_kwh = min(
                        forecast_total_kwh, sum(base_predictions_by_hour.values())
                    )
                return ConservativePlanningForecast(
                    target_date=target_date,
                    total_kwh=round(forecast_total_kwh, 3),
                    hourly=hourly_forecasts,
                    updated_at=row["conservative_planning_forecast_updated_at"],
                )
        except Exception as error:
            _LOGGER.error(
                "Error reading conservative planning forecast from database: %s", error
            )
            return
        return None if True else None

    async def async_get_model_state(self) -> ModelState | None:
        """Read the current ML model state from database. @zara"""
        if not self.is_available:
            return
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "SELECT version, active_model, training_samples, last_trained,\n                              accuracy, rmse\n                       FROM ai_learned_weights_meta WHERE id = 1"
                ) as cursor:
                    model_row = await cursor.fetchone()
                if not model_row:
                    return
                last_training = None
                if model_row["last_trained"]:
                    try:
                        last_training = datetime.fromisoformat(
                            str(model_row["last_trained"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                async with connection.execute(
                    "SELECT COUNT(*) FROM ai_lstm_weights"
                ) as cursor:
                    weight_count = (await cursor.fetchone())[0]
                model_loaded = weight_count > 0
                return ModelState(
                    model_loaded=model_loaded,
                    algorithm_used=model_row["active_model"] or "TinyLSTM",
                    training_samples=model_row["training_samples"] or 0,
                    current_accuracy=model_row["accuracy"] or 0.0,
                    last_training=last_training,
                    peak_power_kw=0.0,
                    feature_weights={},
                )
        except Exception as error:
            _LOGGER.error("Error reading model state from database: %s", error)
            return
        return None if True else None

    async def async_get_weekly_stats(self, year: int, week: int) -> dict[str, Any]:
        """Calculate statistics for a specific calendar week. @zara"""
        period_start = date.fromisocalendar(year, week, 1)
        period_end = date.fromisocalendar(year, week, 7)
        daily_summaries = await self.async_get_daily_summaries(
            start_date=period_start, end_date=period_end
        )
        if not daily_summaries:
            return {"week": week, "year": year, "data_available": False}
        total_predicted_kwh = sum(
            (summary.predicted_total_kwh for summary in daily_summaries)
        )
        total_actual_kwh = sum(
            (summary.actual_total_kwh for summary in daily_summaries)
        )
        average_accuracy = _average_present(
            [summary.accuracy_percent for summary in daily_summaries]
        )
        average_evaluation_coverage = _average_present(
            [summary.evaluation_coverage_percent for summary in daily_summaries]
        )
        hourly_predictions = await self.async_get_hourly_predictions()
        week_predictions = [
            prediction
            for prediction in hourly_predictions
            if prediction.target_date.isocalendar()[0] == year
            if prediction.target_date.isocalendar()[1] == week
        ]
        average_ml_contribution = (
            sum((prediction.ml_contribution_percent for prediction in week_predictions))
            / len(week_predictions)
            if week_predictions
            else 0.0
        )
        return {
            "week": week,
            "year": year,
            "data_available": True,
            "days_count": len(daily_summaries),
            "total_predicted_kwh": round(total_predicted_kwh, 2),
            "total_actual_kwh": round(total_actual_kwh, 2),
            "average_accuracy_percent": (
                round(average_accuracy, 1) if average_accuracy is not None else None
            ),
            "average_evaluation_coverage_percent": (
                round(average_evaluation_coverage, 1)
                if average_evaluation_coverage is not None
                else None
            ),
            "average_excluded_mppt_hours": round(
                sum((summary.excluded_mppt_hours_count for summary in daily_summaries))
                / len(daily_summaries),
                2,
            ),
            "avg_ml_contribution_percent": round(average_ml_contribution, 1),
            "total_shadow_hours": sum(
                (summary.shadow_hours_count for summary in daily_summaries)
            ),
            "total_shadow_loss_kwh": round(
                sum((summary.shadow_loss_kwh for summary in daily_summaries)), 2
            ),
            "total_frost_hours": sum(
                (summary.frost_hours_count for summary in daily_summaries)
            ),
            "total_excluded_mppt_hours": sum(
                (summary.excluded_mppt_hours_count for summary in daily_summaries)
            ),
            "daily_summaries": daily_summaries,
        }

    async def async_get_monthly_stats(self, year: int, month: int) -> dict[str, Any]:
        """Calculate statistics for a specific month. @zara"""
        import calendar as calendar_module

        period_start = date(year, month, 1)
        days_in_month = calendar_module.monthrange(year, month)[1]
        period_end = date(year, month, days_in_month)
        daily_summaries = await self.async_get_daily_summaries(
            start_date=period_start, end_date=period_end
        )
        if not daily_summaries:
            return {"month": month, "year": year, "data_available": False}
        total_predicted_kwh = sum(
            (summary.predicted_total_kwh for summary in daily_summaries)
        )
        total_actual_kwh = sum(
            (summary.actual_total_kwh for summary in daily_summaries)
        )
        best_day = max(daily_summaries, key=lambda x: x.actual_total_kwh)
        worst_day = min(daily_summaries, key=lambda x: x.actual_total_kwh)
        average_accuracy = _average_present(
            [summary.accuracy_percent for summary in daily_summaries]
        )
        average_evaluation_coverage = _average_present(
            [summary.evaluation_coverage_percent for summary in daily_summaries]
        )
        return {
            "month": month,
            "year": year,
            "data_available": True,
            "days_count": len(daily_summaries),
            "total_predicted_kwh": round(total_predicted_kwh, 2),
            "total_actual_kwh": round(total_actual_kwh, 2),
            "average_daily_kwh": round(total_actual_kwh / len(daily_summaries), 2),
            "average_accuracy_percent": (
                round(average_accuracy, 1) if average_accuracy is not None else None
            ),
            "average_evaluation_coverage_percent": (
                round(average_evaluation_coverage, 1)
                if average_evaluation_coverage is not None
                else None
            ),
            "average_excluded_mppt_hours": round(
                sum((summary.excluded_mppt_hours_count for summary in daily_summaries))
                / len(daily_summaries),
                2,
            ),
            "best_day": {
                "date": best_day.date.isoformat(),
                "kwh": best_day.actual_total_kwh,
            },
            "worst_day": {
                "date": worst_day.date.isoformat(),
                "kwh": worst_day.actual_total_kwh,
            },
            "total_shadow_loss_kwh": round(
                sum((summary.shadow_loss_kwh for summary in daily_summaries)), 2
            ),
            "total_frost_hours": sum(
                (summary.frost_hours_count for summary in daily_summaries)
            ),
            "total_excluded_mppt_hours": sum(
                (summary.excluded_mppt_hours_count for summary in daily_summaries)
            ),
            "daily_summaries": daily_summaries,
        }

    async def async_get_panel_group_data(
        self, target_date: date
    ) -> dict[str, list[PanelGroupData]]:
        """Get panel group predictions and actuals for a specific date. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return {}
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "PRAGMA table_info(prediction_panel_groups)"
                ) as cursor:
                    panel_group_columns = {
                        (
                            row["name"]
                            if hasattr(row, "keys") and "name" in row.keys()
                            else row[1]
                        )
                        for row in await cursor.fetchall()
                    }
                async with connection.execute(
                    "PRAGMA table_info(hourly_predictions)"
                ) as cursor:
                    hourly_prediction_columns = {
                        (
                            row["name"]
                            if hasattr(row, "keys") and "name" in row.keys()
                            else row[1]
                        )
                        for row in await cursor.fetchall()
                    }

                def hp_expr(name: str, default: str) -> str:
                    return (
                        f"hp.{name}"
                        if name in hourly_prediction_columns
                        else f"{default} AS {name}"
                    )

                def ppg_expr(name: str, default: str) -> str:
                    return (
                        f"ppg.{name}"
                        if name in panel_group_columns
                        else f"{default} AS {name}"
                    )

                async with connection.execute(
                    f"SELECT ppg.group_name, ppg.prediction_kwh, ppg.actual_kwh,\n                              hp.target_hour,\n                              {hp_expr('exclude_from_learning', '0')},\n                              {hp_expr('exclude_from_clean_evaluation', '0')},\n                              {hp_expr('mppt_throttled', '0')},\n                              {hp_expr('inverter_clipped', '0')},\n                              {hp_expr('mppt_throttle_reason', 'NULL')},\n                              {hp_expr('has_weather_alert', '0')},\n                              {hp_expr('weather_alert_type', 'NULL')},\n                              {ppg_expr('exclude_from_learning_group', '0')},\n                              {ppg_expr('exclusion_reason_group', 'NULL')},\n                              {ppg_expr('snow_covered_group', '0')},\n                              {ppg_expr('topology_compatible', '1')},\n                              {ppg_expr('topology_exclusion_reason', 'NULL')}\n                       FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                       WHERE hp.target_date = ?\n                       ORDER BY hp.target_hour, ppg.group_name",
                    (target_date.isoformat(),),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    return {}
                panel_groups = {}
                for row in rows:
                    group_name = row["group_name"]
                    if group_name not in panel_groups:
                        panel_groups[group_name] = []
                    panel_groups[group_name].append(
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
                return panel_groups
        except Exception as error:
            _LOGGER.error("Error reading panel group data from database: %s", error)
            return {}
        return None if True else None

    async def async_get_daily_forecasts(
        self, forecast_types: list[str] | None = None
    ) -> dict[str, DailyForecast]:
        """Read daily forecasts from SFML database. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return {}
        if forecast_types is None:
            forecast_types = ["today", "tomorrow", "day_after_tomorrow"]
        today = ha_today(self._hass)
        dates_by_forecast_type = {
            "today": today,
            "tomorrow": today + timedelta(days=1),
            "day_after_tomorrow": today + timedelta(days=2),
        }
        forecasts = {}
        try:
            async with self._get_db_connection() as connection:
                for forecast_type in forecast_types:
                    target_forecast_date = dates_by_forecast_type.get(forecast_type)
                    if not target_forecast_date:
                        continue
                    query = "\n                        SELECT forecast_type, forecast_date, prediction_kwh,\n                               prediction_kwh_raw, locked, source, created_at\n                        FROM daily_forecasts\n                        WHERE forecast_type = ? AND forecast_date = ?\n                    "
                    async with connection.execute(
                        query, (forecast_type, target_forecast_date.isoformat())
                    ) as cursor:
                        row = await cursor.fetchone()
                    if not row:
                        _LOGGER.debug(
                            "No daily_forecast found for type=%s, date=%s",
                            forecast_type,
                            target_forecast_date,
                        )
                        continue
                    try:
                        forecast_date = row["forecast_date"]
                        if isinstance(forecast_date, str):
                            forecast_date = date.fromisoformat(forecast_date)
                        created_at = row["created_at"]
                        if isinstance(created_at, str):
                            created_at = datetime.fromisoformat(created_at)
                        forecast = DailyForecast(
                            forecast_type=row["forecast_type"],
                            forecast_date=forecast_date,
                            prediction_kwh=row["prediction_kwh"] or 0.0,
                            prediction_kwh_raw=row["prediction_kwh_raw"],
                            locked=(
                                bool(row["locked"])
                                if row["locked"] is not None
                                else False
                            ),
                            source=row["source"],
                            created_at=created_at,
                        )
                        forecasts[forecast_type] = forecast
                    except Exception as error:
                        _LOGGER.warning("Error parsing daily forecast row: %s", error)
                        continue
                _LOGGER.debug("Loaded %d daily forecasts from database", len(forecasts))
                return forecasts
        except Exception as error:
            _LOGGER.error("Error reading daily forecasts from database: %s", error)
            return {}
        return None if True else None
