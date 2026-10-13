# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Operational Forecast Handler for Solar Forecast ML.

Maintains a sidecar operational forecast track for user-facing intraday
reforecasting without mutating the core learning truth.

@zara
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
_CUSTOM_TIME_PATTERN = re.compile("^(?:[01]\\d|2[0-3]):[0-5]\\d$")


class DataOperationalForecastHandler(DataManagerIO):
    """Relational access layer for the operational forecast sidecar track. @zara"""

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        super().__init__(hass, db_manager)
        self._last_reforecast_settings = None

    def _validate_reforecast_settings(
        self, mode: str, custom_time: Optional[str]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Validate one operational reforecast settings payload. @zara"""
        supported_modes = {
            "standard",
            "custom_time",
            "standard_midday",
            "standard_midday_afternoon",
        }
        normalized_mode = (mode or "").strip().lower()
        normalized_custom_time = (custom_time or "").strip() or None
        if normalized_mode not in supported_modes:
            return (False, None, "Unsupported operational reforecast mode")
        if normalized_mode == "custom_time":
            if not normalized_custom_time:
                return (False, None, "custom_time is required when mode=custom_time")
            if not _CUSTOM_TIME_PATTERN.match(normalized_custom_time):
                return (False, None, "custom_time must match HH:MM")
        else:
            normalized_custom_time = None
        return (True, normalized_mode, normalized_custom_time)

    async def create_snapshot(
        self,
        snapshot_date: str,
        snapshot_type: str,
        trigger_source: str,
        status: str = "pending",
        cutoff_hour: Optional[int] = None,
        source_method: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[int]:
        """Create one operational snapshot header and return its sequence. @zara"""
        try:
            await self.ensure_initialized()
            triggered_at = dt_util.now().isoformat()
            await self.db.execute(
                "INSERT INTO ops_forecast_snapshots (\n                       snapshot_date, snapshot_type, trigger_source, triggered_at,\n                       cutoff_hour, status, source_method, notes\n                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_date,
                    snapshot_type,
                    trigger_source,
                    triggered_at,
                    cutoff_hour,
                    status,
                    source_method,
                    notes,
                ),
            )
            sequence_row = await self.db.fetchone("SELECT last_insert_rowid()")
            return int(sequence_row[0]) if sequence_row else None
        except Exception as error:
            _LOGGER.error(
                "Failed to create operational snapshot: %s", error, exc_info=True
            )
            return

    async def update_snapshot_status(
        self,
        snapshot_sequence: int,
        status: str,
        weather_refresh_started_at: Optional[str] = None,
        weather_refresh_finished_at: Optional[str] = None,
        forecast_generation_started_at: Optional[str] = None,
        forecast_generation_finished_at: Optional[str] = None,
        source_method: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """Update lifecycle status fields of one operational snapshot. @zara"""
        try:
            await self.ensure_initialized()
            await self.db.execute(
                "UPDATE ops_forecast_snapshots\n                   SET status = ?,\n                       weather_refresh_started_at = COALESCE(?, weather_refresh_started_at),\n                       weather_refresh_finished_at = COALESCE(?, weather_refresh_finished_at),\n                       forecast_generation_started_at = COALESCE(?, forecast_generation_started_at),\n                       forecast_generation_finished_at = COALESCE(?, forecast_generation_finished_at),\n                       source_method = COALESCE(?, source_method),\n                       notes = COALESCE(?, notes)\n                   WHERE snapshot_sequence = ?",
                (
                    status,
                    weather_refresh_started_at,
                    weather_refresh_finished_at,
                    forecast_generation_started_at,
                    forecast_generation_finished_at,
                    source_method,
                    notes,
                    snapshot_sequence,
                ),
            )
        except Exception as error:
            _LOGGER.error(
                "Failed to update operational snapshot status: %s", error, exc_info=True
            )
            return False
        return True

    async def supersede_active_future_rows(
        self, target_date: str, cutoff_hour: int, superseded_at: Optional[str] = None
    ) -> bool:
        """Deactivate only future operational rows from the cutoff onward. @zara"""
        try:
            await self.ensure_initialized()
            superseded_at = superseded_at or dt_util.now().isoformat()
            async with self.db.transaction():
                await self.db.execute(
                    "UPDATE ops_hourly_forecasts\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (superseded_at, target_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_prediction_weather\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (superseded_at, target_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_prediction_panel_groups\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (superseded_at, target_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_daily_forecasts\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE forecast_type = 'today'\n                         AND forecast_date = ?\n                         AND is_active = TRUE",
                    (superseded_at, target_date),
                    auto_commit=False,
                )
            if True:
                return True
        except Exception as error:
            _LOGGER.error(
                "Failed to supersede operational future rows: %s", error, exc_info=True
            )
            return False

    async def supersede_active_rows_for_dates(
        self, target_dates: List[str], superseded_at: Optional[str] = None
    ) -> bool:
        """Deactivate all active operational rows for the supplied dates. @zara"""
        try:
            await self.ensure_initialized()
            if not target_dates:
                if True:
                    return True
            superseded_at = superseded_at or dt_util.now().isoformat()
            date_placeholders = ",".join(["?"] * len(target_dates))
            date_parameters = tuple(target_dates)
            async with self.db.transaction():
                await self.db.execute(
                    f"UPDATE ops_hourly_forecasts\n                        SET is_active = FALSE,\n                            superseded_at = ?\n                        WHERE target_date IN ({date_placeholders})\n                          AND is_active = TRUE",
                    (superseded_at, *date_parameters),
                    auto_commit=False,
                )
                await self.db.execute(
                    f"UPDATE ops_prediction_weather\n                        SET is_active = FALSE,\n                            superseded_at = ?\n                        WHERE target_date IN ({date_placeholders})\n                          AND is_active = TRUE",
                    (superseded_at, *date_parameters),
                    auto_commit=False,
                )
                await self.db.execute(
                    f"UPDATE ops_prediction_panel_groups\n                        SET is_active = FALSE,\n                            superseded_at = ?\n                        WHERE target_date IN ({date_placeholders})\n                          AND is_active = TRUE",
                    (superseded_at, *date_parameters),
                    auto_commit=False,
                )
                await self.db.execute(
                    f"UPDATE ops_daily_forecasts\n                        SET is_active = FALSE,\n                            superseded_at = ?\n                        WHERE forecast_date IN ({date_placeholders})\n                          AND is_active = TRUE",
                    (superseded_at, *date_parameters),
                    auto_commit=False,
                )
            if True:
                return True
        except Exception as error:
            _LOGGER.error(
                "Failed to supersede operational active rows: %s", error, exc_info=True
            )
            if True:
                return False

    async def save_hourly_forecasts(
        self,
        snapshot_sequence: int,
        snapshot_date: str,
        snapshot_type: str,
        trigger_source: str,
        source_track: str,
        hourly_values: List[Dict[str, Any]],
        cutoff_hour: Optional[int] = None,
        actuals_by_prediction_id: Optional[Dict[str, float]] = None,
        group_actuals_by_key: Optional[Dict[Tuple[str, str], float]] = None,
        weather_by_prediction_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, int]:
        """Persist one operational hourly forecast snapshot. @zara"""
        hourly_rows = 0
        weather_rows = 0
        group_rows = 0
        created_at = dt_util.now().isoformat()
        actuals_by_prediction_id = actuals_by_prediction_id or {}
        group_actuals_by_key = group_actuals_by_key or {}
        weather_by_prediction_id = weather_by_prediction_id or {}
        try:
            await self.ensure_initialized()
            async with self.db.transaction():
                for hourly_entry in hourly_values:
                    prediction_id = f"{hourly_entry['date']}_{hourly_entry['hour']:02d}"
                    await self.db.execute(
                        "INSERT INTO ops_hourly_forecasts (\n                               ops_track_date, target_date, target_hour, prediction_id,\n                               snapshot_type, snapshot_sequence, snapshot_created_at,\n                               trigger_source, is_active, prediction_kwh,\n                               prediction_kwh_uncapped, prediction_method, physics_kwh,\n                               ai_kwh, ai_confidence, ml_contribution_percent,\n                               lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight,\n                               is_production_hour, actual_kwh, source_track,\n                               reforecast_cutoff_hour\n                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot_date,
                            hourly_entry["date"],
                            hourly_entry["hour"],
                            prediction_id,
                            snapshot_type,
                            snapshot_sequence,
                            created_at,
                            trigger_source,
                            round(hourly_entry.get("production_kwh", 0.0), 4),
                            (
                                round(hourly_entry.get("prediction_kwh_uncapped"), 4)
                                if hourly_entry.get("prediction_kwh_uncapped")
                                is not None
                                else None
                            ),
                            hourly_entry.get("method"),
                            hourly_entry.get("physics_kwh"),
                            hourly_entry.get("ai_kwh"),
                            hourly_entry.get("ai_confidence"),
                            hourly_entry.get("ml_contribution", 0),
                            hourly_entry.get("lstm_kwh"),
                            hourly_entry.get("ridge_kwh"),
                            hourly_entry.get("tfs_kwh"),
                            hourly_entry.get("tfs_weight"),
                            6 <= hourly_entry["hour"] <= 20,
                            actuals_by_prediction_id.get(prediction_id),
                            source_track,
                            cutoff_hour,
                        ),
                        auto_commit=False,
                    )
                    row = await self.db.fetchone("SELECT last_insert_rowid()", ())
                    forecast_row_id = int(row[0]) if row else None
                    hourly_rows += 1
                    weather = weather_by_prediction_id.get(prediction_id)
                    if weather:
                        await self.db.execute(
                            "INSERT INTO ops_prediction_weather (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, temperature, solar_radiation_wm2,\n                                   wind, humidity, rain, clouds, pressure, source,\n                                   lux, frost_detected, frost_score, frost_confidence,\n                                   diffuse_radiation, direct_radiation, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                hourly_entry["date"],
                                hourly_entry["hour"],
                                prediction_id,
                                snapshot_sequence,
                                weather.get("temperature")
                                or weather.get("temperature_c"),
                                weather.get("solar_radiation_wm2"),
                                weather.get("wind")
                                or weather.get("wind_speed")
                                or weather.get("wind_speed_ms"),
                                weather.get("humidity")
                                or weather.get("humidity_percent"),
                                weather.get("rain")
                                or weather.get("precipitation")
                                or weather.get("precipitation_mm"),
                                weather.get("clouds")
                                or weather.get("cloud_cover_percent")
                                or weather.get("cloud_cover"),
                                weather.get("pressure") or weather.get("pressure_hpa"),
                                weather.get("source", "unknown"),
                                weather.get("lux"),
                                weather.get("frost_detected"),
                                weather.get("frost_score"),
                                weather.get("frost_confidence"),
                                weather.get("diffuse_radiation") or weather.get("dhi"),
                                weather.get("direct_radiation") or weather.get("dni"),
                            ),
                            auto_commit=False,
                        )
                        weather_rows += 1
                    for group_entry in hourly_entry.get("groups") or []:
                        await self.db.execute(
                            "INSERT INTO ops_prediction_panel_groups (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, group_name, prediction_kwh,\n                                   physics_kwh, ai_kwh, lstm_kwh, ridge_kwh, tfs_kwh,\n                                   actual_kwh, raw_physics_kwh, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                hourly_entry["date"],
                                hourly_entry["hour"],
                                prediction_id,
                                snapshot_sequence,
                                group_entry.get("name"),
                                round(group_entry.get("power_kwh", 0.0), 4),
                                (
                                    round(group_entry.get("physics_kwh"), 4)
                                    if group_entry.get("physics_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("ai_kwh"), 4)
                                    if group_entry.get("ai_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("lstm_kwh"), 4)
                                    if group_entry.get("lstm_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("ridge_kwh"), 4)
                                    if group_entry.get("ridge_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("tfs_kwh"), 4)
                                    if group_entry.get("tfs_kwh") is not None
                                    else None
                                ),
                                (
                                    round(
                                        group_actuals_by_key.get(
                                            (prediction_id, group_entry.get("name"))
                                        ),
                                        4,
                                    )
                                    if group_actuals_by_key.get(
                                        (prediction_id, group_entry.get("name"))
                                    )
                                    is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("raw_physics_kwh"), 4)
                                    if group_entry.get("raw_physics_kwh") is not None
                                    else None
                                ),
                            ),
                            auto_commit=False,
                        )
                        group_rows += 1
                    continue
            return {
                "hourly_rows": hourly_rows,
                "weather_rows": weather_rows,
                "group_rows": group_rows,
            }
        except Exception as error:
            _LOGGER.error(
                "Failed to save operational hourly forecasts: %s", error, exc_info=True
            )
            return {
                "hourly_rows": hourly_rows,
                "weather_rows": weather_rows,
                "group_rows": group_rows,
            }

    async def save_daily_forecasts(
        self,
        snapshot_sequence: int,
        snapshot_type: str,
        trigger_source: str,
        source_method: Optional[str],
        daily_values: List[Dict[str, Any]],
    ) -> int:
        """Persist one operational daily forecast snapshot. @zara"""
        created_at = dt_util.now().isoformat()
        daily_rows = 0
        try:
            await self.ensure_initialized()
            async with self.db.transaction():
                for daily_entry in daily_values:
                    await self.db.execute(
                        "INSERT INTO ops_daily_forecasts (\n                               forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                               snapshot_created_at, trigger_source, prediction_kwh,\n                               prediction_kwh_raw, source_method, actual_kwh, is_active\n                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                        (
                            daily_entry["forecast_type"],
                            daily_entry["forecast_date"],
                            snapshot_type,
                            snapshot_sequence,
                            created_at,
                            trigger_source,
                            round(daily_entry["prediction_kwh"], 2),
                            (
                                round(daily_entry["prediction_kwh_raw"], 2)
                                if daily_entry.get("prediction_kwh_raw") is not None
                                else None
                            ),
                            source_method,
                            daily_entry.get("actual_kwh"),
                        ),
                        auto_commit=False,
                    )
                    daily_rows += 1
            return daily_rows if True else None
        except Exception as error:
            _LOGGER.error(
                "Failed to save operational daily forecasts: %s", error, exc_info=True
            )
            return daily_rows if True else None

    async def publish_intraday_reforecast(
        self,
        snapshot_date: str,
        snapshot_type: str,
        trigger_source: str,
        source_track: str,
        source_method: str,
        hourly_values: List[Dict[str, Any]],
        daily_values: List[Dict[str, Any]],
        cutoff_hour: int,
        weather_by_prediction_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Atomically publish one future-only intraday operational forecast."""
        await self.ensure_initialized()
        hourly_rows = 0
        weather_rows = 0
        group_rows = 0
        daily_rows = 0
        snapshot_sequence = None
        created_at = dt_util.now().isoformat()
        weather_by_prediction_id = weather_by_prediction_id or {}
        try:
            async with self.db.transaction():
                await self.db.execute(
                    "INSERT INTO ops_forecast_snapshots (\n                           snapshot_date, snapshot_type, trigger_source, triggered_at,\n                           cutoff_hour, status, source_method, notes,\n                           weather_refresh_finished_at, forecast_generation_started_at\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_date,
                        snapshot_type,
                        trigger_source,
                        created_at,
                        cutoff_hour,
                        "running",
                        source_method,
                        f"Future-only reforecast from hour>{cutoff_hour}",
                        created_at,
                        created_at,
                    ),
                    auto_commit=False,
                )
                row = await self.db.fetchone("SELECT last_insert_rowid()", ())
                snapshot_sequence = int(row[0]) if row else None
                if snapshot_sequence is None:
                    raise RuntimeError("Operational snapshot creation failed")
                await self.db.execute(
                    "UPDATE ops_hourly_forecasts\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (created_at, snapshot_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_prediction_weather\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (created_at, snapshot_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_prediction_panel_groups\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE target_date = ?\n                         AND target_hour > ?\n                         AND is_active = TRUE",
                    (created_at, snapshot_date, cutoff_hour),
                    auto_commit=False,
                )
                await self.db.execute(
                    "UPDATE ops_daily_forecasts\n                       SET is_active = FALSE,\n                           superseded_at = ?\n                       WHERE forecast_type = 'today'\n                         AND forecast_date = ?\n                         AND is_active = TRUE",
                    (created_at, snapshot_date),
                    auto_commit=False,
                )
                for hourly_entry in hourly_values:
                    prediction_id = f"{hourly_entry['date']}_{hourly_entry['hour']:02d}"
                    await self.db.execute(
                        "INSERT INTO ops_hourly_forecasts (\n                               ops_track_date, target_date, target_hour, prediction_id,\n                               snapshot_type, snapshot_sequence, snapshot_created_at,\n                               trigger_source, is_active, prediction_kwh,\n                               prediction_kwh_uncapped, prediction_method, physics_kwh,\n                               ai_kwh, ai_confidence, ml_contribution_percent,\n                               lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight,\n                               is_production_hour, actual_kwh, source_track,\n                               reforecast_cutoff_hour\n                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot_date,
                            hourly_entry["date"],
                            hourly_entry["hour"],
                            prediction_id,
                            snapshot_type,
                            snapshot_sequence,
                            created_at,
                            trigger_source,
                            round(hourly_entry.get("production_kwh", 0.0), 4),
                            (
                                round(hourly_entry.get("prediction_kwh_uncapped"), 4)
                                if hourly_entry.get("prediction_kwh_uncapped")
                                is not None
                                else None
                            ),
                            hourly_entry.get("method"),
                            hourly_entry.get("physics_kwh"),
                            hourly_entry.get("ai_kwh"),
                            hourly_entry.get("ai_confidence"),
                            hourly_entry.get("ml_contribution", 0),
                            hourly_entry.get("lstm_kwh"),
                            hourly_entry.get("ridge_kwh"),
                            hourly_entry.get("tfs_kwh"),
                            hourly_entry.get("tfs_weight"),
                            6 <= hourly_entry["hour"] <= 20,
                            None,
                            source_track,
                            cutoff_hour,
                        ),
                        auto_commit=False,
                    )
                    row = await self.db.fetchone("SELECT last_insert_rowid()", ())
                    forecast_row_id = int(row[0]) if row else None
                    hourly_rows += 1
                    weather = weather_by_prediction_id.get(prediction_id)
                    if weather:
                        await self.db.execute(
                            "INSERT INTO ops_prediction_weather (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, temperature, solar_radiation_wm2,\n                                   wind, humidity, rain, clouds, pressure, source,\n                                   lux, frost_detected, frost_score, frost_confidence,\n                                   diffuse_radiation, direct_radiation, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                hourly_entry["date"],
                                hourly_entry["hour"],
                                prediction_id,
                                snapshot_sequence,
                                weather.get("temperature")
                                or weather.get("temperature_c"),
                                weather.get("solar_radiation_wm2"),
                                weather.get("wind")
                                or weather.get("wind_speed")
                                or weather.get("wind_speed_ms"),
                                weather.get("humidity")
                                or weather.get("humidity_percent"),
                                weather.get("rain")
                                or weather.get("precipitation")
                                or weather.get("precipitation_mm"),
                                weather.get("clouds")
                                or weather.get("cloud_cover_percent")
                                or weather.get("cloud_cover"),
                                weather.get("pressure") or weather.get("pressure_hpa"),
                                weather.get("source", "unknown"),
                                weather.get("lux"),
                                weather.get("frost_detected"),
                                weather.get("frost_score"),
                                weather.get("frost_confidence"),
                                weather.get("diffuse_radiation") or weather.get("dhi"),
                                weather.get("direct_radiation") or weather.get("dni"),
                            ),
                            auto_commit=False,
                        )
                        weather_rows += 1
                    for group_entry in hourly_entry.get("groups") or []:
                        await self.db.execute(
                            "INSERT INTO ops_prediction_panel_groups (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, group_name, prediction_kwh,\n                                   physics_kwh, ai_kwh, lstm_kwh, ridge_kwh, tfs_kwh,\n                                   actual_kwh, raw_physics_kwh, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                hourly_entry["date"],
                                hourly_entry["hour"],
                                prediction_id,
                                snapshot_sequence,
                                group_entry.get("name"),
                                round(group_entry.get("power_kwh", 0.0), 4),
                                (
                                    round(group_entry.get("physics_kwh"), 4)
                                    if group_entry.get("physics_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("ai_kwh"), 4)
                                    if group_entry.get("ai_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("lstm_kwh"), 4)
                                    if group_entry.get("lstm_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("ridge_kwh"), 4)
                                    if group_entry.get("ridge_kwh") is not None
                                    else None
                                ),
                                (
                                    round(group_entry.get("tfs_kwh"), 4)
                                    if group_entry.get("tfs_kwh") is not None
                                    else None
                                ),
                                None,
                                (
                                    round(group_entry.get("raw_physics_kwh"), 4)
                                    if group_entry.get("raw_physics_kwh") is not None
                                    else None
                                ),
                            ),
                            auto_commit=False,
                        )
                        group_rows += 1
                    continue
                for daily_entry in daily_values:
                    await self.db.execute(
                        "INSERT INTO ops_daily_forecasts (\n                               forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                               snapshot_created_at, trigger_source, prediction_kwh,\n                               prediction_kwh_raw, source_method, actual_kwh, is_active\n                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                        (
                            daily_entry["forecast_type"],
                            daily_entry["forecast_date"],
                            snapshot_type,
                            snapshot_sequence,
                            created_at,
                            trigger_source,
                            round(daily_entry["prediction_kwh"], 2),
                            (
                                round(daily_entry["prediction_kwh_raw"], 2)
                                if daily_entry.get("prediction_kwh_raw") is not None
                                else None
                            ),
                            source_method,
                            daily_entry.get("actual_kwh"),
                        ),
                        auto_commit=False,
                    )
                    daily_rows += 1
                await self.db.execute(
                    "UPDATE ops_forecast_snapshots\n                       SET status = 'completed',\n                           forecast_generation_finished_at = ?,\n                           notes = ?\n                       WHERE snapshot_sequence = ?",
                    (
                        dt_util.now().isoformat(),
                        f"Reforecasted {hourly_rows} hourly rows and {weather_rows} weather rows and {group_rows} group rows",
                        snapshot_sequence,
                    ),
                    auto_commit=False,
                )
            return {
                "snapshot_sequence": snapshot_sequence,
                "hourly_rows": hourly_rows,
                "weather_rows": weather_rows,
                "group_rows": group_rows,
                "daily_rows": daily_rows,
            }
        except Exception as error:
            _LOGGER.error(
                "Failed to publish operational intraday reforecast: %s",
                error,
                exc_info=True,
            )
            return {
                "snapshot_sequence": snapshot_sequence,
                "hourly_rows": hourly_rows,
                "weather_rows": weather_rows,
                "group_rows": group_rows,
                "daily_rows": daily_rows,
            }

    async def publish_adaptive_correction_snapshot(
        self,
        snapshot_date: str,
        corrected_daily_kwh: float,
        cutoff_hour: int,
        created_at: Any,
    ) -> Dict[str, Any]:
        """Publish the post-MDC core forecast as the active operational snapshot. @zara"""
        await self.ensure_initialized()
        created_at_text = (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        )
        snapshot_sequence = None
        hourly_rows = 0
        weather_rows = 0
        group_rows = 0
        daily_rows = 0
        prediction_rows = await self.db.fetchall(
            "SELECT prediction_id, target_date, target_hour, prediction_kwh,\n                      prediction_kwh_uncapped, prediction_method, physics_kwh,\n                      ai_kwh, ai_confidence, ml_contribution_percent, lstm_kwh,\n                      ridge_kwh, tfs_kwh, tfs_weight, is_production_hour, actual_kwh\n               FROM hourly_predictions\n               WHERE target_date = ?\n               ORDER BY target_hour",
            (snapshot_date,),
        )
        if not prediction_rows:
            raise RuntimeError(
                f"No hourly predictions available for adaptive OPS snapshot {snapshot_date}"
            )
        weather_rows_from_core = await self.db.fetchall(
            "SELECT pw.prediction_id, hp.target_hour, pw.temperature,\n                      pw.solar_radiation_wm2, pw.wind, pw.humidity, pw.rain,\n                      pw.clouds, pw.pressure, pw.source, pw.lux,\n                      pw.frost_detected, pw.frost_score, pw.frost_confidence,\n                      pw.diffuse_radiation, pw.direct_radiation\n               FROM prediction_weather pw\n               INNER JOIN hourly_predictions hp ON hp.prediction_id = pw.prediction_id\n               WHERE hp.target_date = ?\n                 AND pw.weather_type = 'forecast'\n               ORDER BY hp.target_hour",
            (snapshot_date,),
        )
        weather_by_prediction = {row[0]: row for row in weather_rows_from_core}
        group_rows_from_core = await self.db.fetchall(
            "SELECT ppg.prediction_id, hp.target_hour, ppg.group_name,\n                      ppg.prediction_kwh, ppg.physics_kwh, ppg.ai_kwh,\n                      ppg.lstm_kwh, ppg.ridge_kwh, ppg.tfs_kwh, ppg.actual_kwh,\n                      ppg.raw_physics_kwh\n               FROM prediction_panel_groups ppg\n               INNER JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n               WHERE hp.target_date = ?\n               ORDER BY hp.target_hour, ppg.group_name",
            (snapshot_date,),
        )
        groups_by_prediction = {}
        for row in group_rows_from_core:
            groups_by_prediction.setdefault(row[0], []).append(row)
        predicted_total_kwh = round(
            sum(
                (float(prediction_row[3] or 0.0) for prediction_row in prediction_rows)
            ),
            2,
        )
        if (
            abs(predicted_total_kwh - round(float(corrected_daily_kwh or 0.0), 2))
            > 0.02
        ):
            _LOGGER.warning(
                "Adaptive OPS snapshot daily total differs from MDC total: ops=%.2f mdc=%.2f",
                predicted_total_kwh,
                corrected_daily_kwh,
            )
        try:
            async with self.db.transaction():
                await self.db.execute(
                    "INSERT INTO ops_forecast_snapshots (\n                           snapshot_date, snapshot_type, trigger_source, triggered_at,\n                           cutoff_hour, status, source_method, notes,\n                           weather_refresh_finished_at, forecast_generation_started_at\n                       ) VALUES (?, 'adaptive_correction', 'adaptive_correction', ?, ?, 'running',\n                                 'adaptive_correction', ?, ?, ?)",
                    (
                        snapshot_date,
                        created_at_text,
                        cutoff_hour,
                        f"Adaptive correction full-day snapshot at hour {cutoff_hour}",
                        created_at_text,
                        created_at_text,
                    ),
                    auto_commit=False,
                )
                row = await self.db.fetchone("SELECT last_insert_rowid()", ())
                snapshot_sequence = int(row[0]) if row else None
                if snapshot_sequence is None:
                    raise RuntimeError("Adaptive operational snapshot creation failed")
                for table_name in (
                    "ops_hourly_forecasts",
                    "ops_prediction_weather",
                    "ops_prediction_panel_groups",
                    "ops_daily_forecasts",
                ):
                    date_column = (
                        "forecast_date"
                        if table_name == "ops_daily_forecasts"
                        else "target_date"
                    )
                    await self.db.execute(
                        f"UPDATE {table_name}\n                            SET is_active = FALSE,\n                                superseded_at = ?\n                            WHERE {date_column} = ?\n                              AND is_active = TRUE",
                        (created_at_text, snapshot_date),
                        auto_commit=False,
                    )
                for prediction_row in prediction_rows:
                    prediction_id = prediction_row[0]
                    target_date = prediction_row[1]
                    target_hour = prediction_row[2]
                    prediction_kwh = round(float(prediction_row[3] or 0.0), 4)
                    uncapped_prediction_kwh = (
                        prediction_row[4]
                        if prediction_row[4] is not None
                        else prediction_row[3]
                    )
                    await self.db.execute(
                        "INSERT INTO ops_hourly_forecasts (\n                               ops_track_date, target_date, target_hour, prediction_id,\n                               snapshot_type, snapshot_sequence, snapshot_created_at,\n                               trigger_source, is_active, prediction_kwh,\n                               prediction_kwh_uncapped, prediction_method, physics_kwh,\n                               ai_kwh, ai_confidence, ml_contribution_percent,\n                               lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight,\n                               is_production_hour, actual_kwh, source_track,\n                               reforecast_cutoff_hour\n                           ) VALUES (?, ?, ?, ?, 'adaptive_correction', ?, ?, 'adaptive_correction',\n                                     TRUE, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n                                     'adaptive_correction', ?)",
                        (
                            snapshot_date,
                            target_date,
                            target_hour,
                            prediction_id,
                            snapshot_sequence,
                            created_at_text,
                            prediction_kwh,
                            (
                                round(float(uncapped_prediction_kwh), 4)
                                if uncapped_prediction_kwh is not None
                                else None
                            ),
                            prediction_row[5],
                            prediction_row[6],
                            prediction_row[7],
                            prediction_row[8],
                            prediction_row[9],
                            prediction_row[10],
                            prediction_row[11],
                            prediction_row[12],
                            prediction_row[13],
                            prediction_row[14],
                            prediction_row[15],
                            cutoff_hour,
                        ),
                        auto_commit=False,
                    )
                    row = await self.db.fetchone("SELECT last_insert_rowid()", ())
                    forecast_row_id = int(row[0]) if row else None
                    if forecast_row_id is None:
                        raise RuntimeError(
                            "Adaptive operational hourly row creation failed"
                        )
                    hourly_rows += 1
                    weather = weather_by_prediction.get(prediction_id)
                    if weather:
                        await self.db.execute(
                            "INSERT INTO ops_prediction_weather (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, temperature, solar_radiation_wm2,\n                                   wind, humidity, rain, clouds, pressure, source,\n                                   lux, frost_detected, frost_score, frost_confidence,\n                                   diffuse_radiation, direct_radiation, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                target_date,
                                target_hour,
                                prediction_id,
                                snapshot_sequence,
                                weather[2],
                                weather[3],
                                weather[4],
                                weather[5],
                                weather[6],
                                weather[7],
                                weather[8],
                                weather[9],
                                weather[10],
                                weather[11],
                                weather[12],
                                weather[13],
                                weather[14],
                                weather[15],
                            ),
                            auto_commit=False,
                        )
                        weather_rows += 1
                    for group_entry in groups_by_prediction.get(prediction_id, []):
                        await self.db.execute(
                            "INSERT INTO ops_prediction_panel_groups (\n                                   forecast_row_id, target_date, target_hour, prediction_id,\n                                   snapshot_sequence, group_name, prediction_kwh,\n                                   physics_kwh, ai_kwh, lstm_kwh, ridge_kwh, tfs_kwh,\n                                   actual_kwh, raw_physics_kwh, is_active\n                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)",
                            (
                                forecast_row_id,
                                target_date,
                                target_hour,
                                prediction_id,
                                snapshot_sequence,
                                group_entry[2],
                                round(float(group_entry[3] or 0.0), 4),
                                group_entry[4],
                                group_entry[5],
                                group_entry[6],
                                group_entry[7],
                                group_entry[8],
                                group_entry[9],
                                group_entry[10] if len(group_entry) > 10 else None,
                            ),
                            auto_commit=False,
                        )
                        group_rows += 1
                    continue
                await self.db.execute(
                    "INSERT INTO ops_daily_forecasts (\n                           forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                           snapshot_created_at, trigger_source, prediction_kwh,\n                           prediction_kwh_raw, source_method, actual_kwh, is_active\n                       ) VALUES ('today', ?, 'adaptive_correction', ?, ?, 'adaptive_correction',\n                                 ?, ?, 'adaptive_correction', NULL, TRUE)",
                    (
                        snapshot_date,
                        snapshot_sequence,
                        created_at_text,
                        predicted_total_kwh,
                        predicted_total_kwh,
                    ),
                    auto_commit=False,
                )
                daily_rows = 1
                await self.db.execute(
                    "UPDATE ops_forecast_snapshots\n                       SET status = 'completed',\n                           forecast_generation_finished_at = ?,\n                           notes = ?\n                       WHERE snapshot_sequence = ?",
                    (
                        dt_util.now().isoformat(),
                        f"Adaptive correction published {hourly_rows} hourly rows, {weather_rows} weather rows and {group_rows} group rows",
                        snapshot_sequence,
                    ),
                    auto_commit=False,
                )
            return {
                "snapshot_sequence": snapshot_sequence,
                "hourly_rows": hourly_rows,
                "weather_rows": weather_rows,
                "group_rows": group_rows,
                "daily_rows": daily_rows,
                "daily_total_kwh": predicted_total_kwh,
            }
        except Exception:
            _LOGGER.error(
                "Failed to publish adaptive operational snapshot", exc_info=True
            )
            raise

    async def mirror_morning_snapshot(
        self,
        snapshot_date: str,
        hourly_values: List[Dict[str, Any]],
        daily_values: List[Dict[str, Any]],
        actuals_by_prediction_id: Optional[Dict[str, float]] = None,
        group_actuals_by_key: Optional[Dict[Tuple[str, str], float]] = None,
        weather_by_prediction_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        """Mirror the completed morning forecast into the operational sidecar track. @zara"""
        snapshot_sequence = None
        try:
            await self.ensure_initialized()
            pass
            target_dates = sorted(
                {
                    hourly_entry.get("date")
                    for hourly_entry in hourly_values
                    if hourly_entry.get("date")
                }.union(
                    {
                        daily_entry.get("forecast_date")
                        for daily_entry in daily_values
                        if daily_entry.get("forecast_date")
                    }
                )
            )
            if not target_dates:
                _LOGGER.warning(
                    "Skipping operational morning mirror - no target dates supplied"
                )
                return False
            finished_at = dt_util.now().isoformat()
            snapshot_sequence = await self.create_snapshot(
                snapshot_date=snapshot_date,
                snapshot_type="morning_snapshot",
                trigger_source="morning_routine",
                status="running",
                source_method="morning_routine",
                notes=f"Mirror for {len(target_dates)} dates",
            )
            if snapshot_sequence is None:
                return False
            await self.update_snapshot_status(
                snapshot_sequence,
                status="running",
                forecast_generation_started_at=finished_at,
                source_method="morning_routine",
            )
            if not await self.supersede_active_rows_for_dates(target_dates):
                raise RuntimeError(
                    "Failed to supersede previous operational morning snapshot"
                )
            saved_counts = await self.save_hourly_forecasts(
                snapshot_sequence=snapshot_sequence,
                snapshot_date=snapshot_date,
                snapshot_type="morning_snapshot",
                trigger_source="morning_routine",
                source_track="core_mirror",
                hourly_values=hourly_values,
                actuals_by_prediction_id=actuals_by_prediction_id,
                group_actuals_by_key=group_actuals_by_key,
                weather_by_prediction_id=weather_by_prediction_id,
            )
            hourly_count = len(hourly_values)
            weather_count = sum(
                (
                    1
                    for hourly_entry in hourly_values
                    if weather_by_prediction_id.get(
                        f"{hourly_entry.get('date')}_{int(hourly_entry.get('hour')):02d}"
                    )
                    is not None
                )
            )
            group_count = sum(
                (
                    len(hourly_entry.get("groups") or [])
                    for hourly_entry in hourly_values
                )
            )
            if saved_counts["hourly_rows"] != hourly_count:
                raise RuntimeError(
                    f"Operational morning mirror wrote {saved_counts['hourly_rows']} of {hourly_count} hourly rows"
                )
            if saved_counts["weather_rows"] != weather_count:
                raise RuntimeError(
                    f"Operational morning mirror wrote {saved_counts['weather_rows']} of {weather_count} weather rows"
                )
            if saved_counts["group_rows"] != group_count:
                raise RuntimeError(
                    f"Operational morning mirror wrote {saved_counts['group_rows']} of {group_count} group rows"
                )
            daily_rows = await self.save_daily_forecasts(
                snapshot_sequence=snapshot_sequence,
                snapshot_type="morning_snapshot",
                trigger_source="morning_routine",
                source_method="morning_routine",
                daily_values=daily_values,
            )
            await self.update_snapshot_status(
                snapshot_sequence,
                status="completed",
                forecast_generation_finished_at=dt_util.now().isoformat(),
                source_method="morning_routine",
                notes=f"Mirrored {saved_counts['hourly_rows']} hourly rows, {saved_counts['weather_rows']} weather rows, {saved_counts['group_rows']} group rows, {daily_rows} daily rows",
            )
        except Exception as error:
            _LOGGER.error(
                "Failed to mirror morning snapshot operationally: %s",
                error,
                exc_info=True,
            )
            if snapshot_sequence is not None:
                await self.update_snapshot_status(
                    snapshot_sequence,
                    status="failed",
                    forecast_generation_finished_at=dt_util.now().isoformat(),
                    source_method="morning_routine",
                    notes=str(error),
                )
            return False
        return True

    async def get_active_hourly_forecasts(
        self, target_date: str
    ) -> List[Dict[str, Any]]:
        """Return the active operational hourly forecast view for one date. @zara"""
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT forecast_row_id, target_date, target_hour, prediction_id,\n                          snapshot_type, snapshot_sequence, snapshot_created_at,\n                          trigger_source, prediction_kwh, prediction_kwh_uncapped,\n                          prediction_method, physics_kwh, ai_kwh, ai_confidence,\n                          ml_contribution_percent, lstm_kwh, ridge_kwh, tfs_kwh,\n                          tfs_weight, is_production_hour, actual_kwh, source_track,\n                          reforecast_cutoff_hour\n                   FROM ops_hourly_forecasts\n                   WHERE target_date = ?\n                     AND is_active = TRUE\n                   ORDER BY target_hour",
                (target_date,),
            )
            return [dict(row) for row in rows]
            pass
        except Exception as error:
            _LOGGER.error(
                "Failed to load active operational hourly forecasts: %s",
                error,
                exc_info=True,
            )
            return []

    async def get_active_daily_forecast(
        self, forecast_type: str, forecast_date: str
    ) -> Optional[Dict[str, Any]]:
        """Return the active operational daily forecast row. @zara"""
        try:
            await self.ensure_initialized()
            forecast_row = await self.db.fetchone(
                "SELECT forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                          snapshot_created_at, trigger_source, prediction_kwh,\n                          prediction_kwh_raw, source_method, actual_kwh\n                   FROM ops_daily_forecasts\n                   WHERE forecast_type = ?\n                     AND forecast_date = ?\n                     AND is_active = TRUE\n                   ORDER BY snapshot_sequence DESC\n                   LIMIT 1",
                (forecast_type, forecast_date),
            )
            return dict(forecast_row) if forecast_row else None
        except Exception as error:
            _LOGGER.error(
                "Failed to load active operational daily forecast: %s",
                error,
                exc_info=True,
            )
            return

    async def save_reforecast_settings(
        self,
        mode: str,
        custom_time: Optional[str] = None,
        enabled: bool = True,
        updated_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """Persist one operational reforecast settings snapshot. @zara"""
        try:
            await self.ensure_initialized()
            valid, normalized_mode, custom_time_or_error = (
                self._validate_reforecast_settings(mode, custom_time)
            )
            if not valid:
                raise ValueError(custom_time_or_error)
            async with self.db.transaction():
                await self.db.execute(
                    "DELETE FROM ops_reforecast_settings", auto_commit=False
                )
                await self.db.execute(
                    "\n                    INSERT INTO ops_reforecast_settings (\n                        mode, custom_time, enabled, updated_at, updated_by, notes\n                    ) VALUES (?, ?, ?, ?, ?, ?)\n                    ",
                    (
                        normalized_mode,
                        custom_time_or_error,
                        bool(enabled),
                        dt_util.now().isoformat(),
                        updated_by,
                        notes,
                    ),
                    auto_commit=False,
                )
            if True:
                return True
        except Exception as error:
            _LOGGER.error(
                "Failed to save operational reforecast settings: %s",
                error,
                exc_info=True,
            )
            return False

    async def get_reforecast_settings(self) -> Dict[str, Any]:
        """Return the current operational reforecast settings. @zara"""
        defaults = {
            "mode": "standard",
            "custom_time": None,
            "enabled": False,
            "updated_at": None,
            "updated_by": None,
            "notes": None,
        }
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "\n                SELECT mode, custom_time, enabled, updated_at, updated_by, notes\n                FROM ops_reforecast_settings\n                ORDER BY settings_id DESC\n                LIMIT 1\n                "
            )
            settings = dict(row) if row else defaults
            self._last_reforecast_settings = dict(settings)
            return settings
        except Exception as error:
            if self._last_reforecast_settings is not None:
                _LOGGER.warning(
                    "Operational reforecast settings unavailable (%s); keeping last known settings",
                    error,
                )
                return dict(self._last_reforecast_settings)
            _LOGGER.warning(
                "Operational reforecast settings unavailable (%s); scheduled reforecasts stay off until the database answers",
                error,
            )
            return {**defaults, "enabled": False}
