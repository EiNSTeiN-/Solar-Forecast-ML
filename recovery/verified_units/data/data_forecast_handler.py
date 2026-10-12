# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Forecast Handler for Solar Forecast ML V16.2.0.
Manages daily forecasts (today, tomorrow, day_after_tomorrow) using SQLite database.
All JSON operations replaced with database queries.

@zara
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)


class DataForecastHandler(DataManagerIO):
    """Handles daily forecasts with database backend. @zara

    Manages:
    - Today's forecast (locked at 6:00 AM)
    - Tomorrow's forecast (updated throughout day)
    - Day after tomorrow's forecast
    - Forecast history and updates tracking
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize forecast handler. @zara

        Args:
            hass: Home Assistant instance
            db_manager: DatabaseManager for DB operations
        """
        super().__init__(hass, db_manager)
        _LOGGER.debug("DataForecastHandler initialized with database backend")

    async def save_forecast_day(
        self,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = True,
        force_overwrite: bool = False,
        prediction_kwh_raw: Optional[float] = None,
        safeguard_applied: bool = False,
    ) -> bool:
        """Save today's daily forecast locked at 6 AM. @zara

        Args:
            prediction_kwh: Forecasted production in kWh
            source: Source of forecast (e.g., "ML", "Physics")
            lock: Whether to lock the forecast
            force_overwrite: Force update even if locked
            prediction_kwh_raw: Raw prediction before safeguard
            safeguard_applied: Whether safeguard was applied

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            today_date = now.date().isoformat()
            if not force_overwrite:
                existing_forecast = await self._get_forecast("today", today_date)
                if existing_forecast and existing_forecast.get("locked"):
                    _LOGGER.debug(
                        "Today's forecast locked by morning routine, skipping update"
                    )
                    return False
            safeguard_reduction = None
            if prediction_kwh_raw is not None:
                safeguard_reduction = round(prediction_kwh_raw - prediction_kwh, 2)
            await self.db.execute(
                "INSERT INTO daily_forecasts (\n                    forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                    safeguard_applied, safeguard_reduction_kwh, locked, locked_at, source\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                    prediction_kwh = excluded.prediction_kwh,\n                    prediction_kwh_raw = excluded.prediction_kwh_raw,\n                    safeguard_applied = excluded.safeguard_applied,\n                    safeguard_reduction_kwh = excluded.safeguard_reduction_kwh,\n                    locked = excluded.locked,\n                    locked_at = excluded.locked_at,\n                    source = excluded.source",
                (
                    "today",
                    today_date,
                    round(prediction_kwh, 2),
                    round(prediction_kwh_raw, 2) if prediction_kwh_raw else None,
                    safeguard_applied,
                    safeguard_reduction,
                    lock,
                    now.isoformat() if lock else None,
                    source,
                ),
            )
            lock_label = "[LOCKED]" if lock else "[UNLOCKED]"
            _LOGGER.info(
                "Today's forecast saved: %.2f kWh (source: %s) %s",
                prediction_kwh,
                source,
                lock_label,
            )
        except Exception as error:
            _LOGGER.error("Failed to save today's forecast: %s", error, exc_info=True)
            return False
        return True

    async def save_forecast_tomorrow(
        self,
        target_date: datetime,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = False,
    ) -> bool:
        """Save tomorrow's forecast. @zara

        Args:
            target_date: Target date
            prediction_kwh: Forecasted production in kWh
            source: Source of forecast
            lock: Whether to lock the forecast

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            forecast_date = (
                target_date.date().isoformat()
                if hasattr(target_date, "date")
                else str(target_date)
            )
            existing_forecast = await self._get_forecast("tomorrow", forecast_date)
            if existing_forecast and existing_forecast.get("locked"):
                _LOGGER.debug(
                    "Tomorrow's forecast locked by morning routine, skipping update"
                )
                return False
            await self.db.execute(
                "INSERT INTO daily_forecasts (\n                    forecast_type, forecast_date, prediction_kwh, locked, locked_at, source\n                ) VALUES (?, ?, ?, ?, ?, ?)\n                ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                    prediction_kwh = excluded.prediction_kwh,\n                    locked = excluded.locked,\n                    locked_at = excluded.locked_at,\n                    source = excluded.source",
                (
                    "tomorrow",
                    forecast_date,
                    round(prediction_kwh, 2),
                    lock,
                    now.isoformat() if lock else None,
                    source,
                ),
            )
            await self._save_forecast_update(
                "tomorrow", forecast_date, prediction_kwh, source
            )
            _LOGGER.info(
                "Tomorrow forecast saved: %s = %.2f kWh", forecast_date, prediction_kwh
            )
        except Exception as error:
            _LOGGER.error("Failed to save tomorrow forecast: %s", error, exc_info=True)
            return False
        return True

    async def save_forecast_day_after(
        self,
        target_date: datetime,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = False,
    ) -> bool:
        """Save day after tomorrow's forecast. @zara

        Args:
            target_date: Target date
            prediction_kwh: Forecasted production in kWh
            source: Source of forecast
            lock: Whether to lock the forecast

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            forecast_date = (
                target_date.date().isoformat()
                if hasattr(target_date, "date")
                else str(target_date)
            )
            existing_forecast = await self._get_forecast(
                "day_after_tomorrow", forecast_date
            )
            if existing_forecast and existing_forecast.get("locked"):
                _LOGGER.debug(
                    "Day after tomorrow's forecast locked by morning routine, skipping update"
                )
                return False
            await self.db.execute(
                "INSERT INTO daily_forecasts (\n                    forecast_type, forecast_date, prediction_kwh, locked, locked_at, source\n                ) VALUES (?, ?, ?, ?, ?, ?)\n                ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                    prediction_kwh = excluded.prediction_kwh,\n                    locked = excluded.locked,\n                    locked_at = excluded.locked_at,\n                    source = excluded.source",
                (
                    "day_after_tomorrow",
                    forecast_date,
                    round(prediction_kwh, 2),
                    lock,
                    now.isoformat() if lock else None,
                    source,
                ),
            )
            await self._save_forecast_update(
                "day_after_tomorrow", forecast_date, prediction_kwh, source
            )
            _LOGGER.info(
                "Day after tomorrow forecast saved: %s = %.2f kWh",
                forecast_date,
                prediction_kwh,
            )
        except Exception as error:
            _LOGGER.error(
                "Failed to save day after tomorrow forecast: %s", error, exc_info=True
            )
            return False
        return True

    async def get_forecast_today(self) -> Optional[Dict[str, Any]]:
        """Get today's forecast. @zara

        Returns:
            Forecast dict or None
        """
        today_date = dt_util.now().date().isoformat()
        return await self._get_forecast("today", today_date)

    async def get_forecast_tomorrow(self) -> Optional[Dict[str, Any]]:
        """Get tomorrow's forecast. @zara

        Returns:
            Forecast dict or None
        """
        tomorrow_date = (dt_util.now() + timedelta(days=1)).date().isoformat()
        return await self._get_forecast("tomorrow", tomorrow_date)

    async def get_forecast_day_after(self) -> Optional[Dict[str, Any]]:
        """Get day after tomorrow's forecast. @zara

        Returns:
            Forecast dict or None
        """
        day_after_date = (dt_util.now() + timedelta(days=2)).date().isoformat()
        return await self._get_forecast("day_after_tomorrow", day_after_date)

    async def get_all_current_forecasts(self) -> Dict[str, Any]:
        """Get all current forecasts (today, tomorrow, day_after). @zara

        Returns:
            Dict with all forecasts
        """
        try:
            await self.ensure_initialized()
            current_time = dt_util.now()
            today_date = current_time.date().isoformat()
            tomorrow_date = (current_time + timedelta(days=1)).date().isoformat()
            day_after_date = (current_time + timedelta(days=2)).date().isoformat()
            return {
                "today": await self._get_forecast("today", today_date),
                "tomorrow": await self._get_forecast("tomorrow", tomorrow_date),
                "day_after_tomorrow": await self._get_forecast(
                    "day_after_tomorrow", day_after_date
                ),
            }
        except Exception as error:
            _LOGGER.error("Failed to get all forecasts: %s", error)
            return {}

    async def rotate_forecasts_at_midnight(self) -> bool:
        """Rotate forecasts at midnight. @zara

        Moves tomorrow -> today, day_after -> tomorrow,
        and clears day_after_tomorrow.

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            rotation_date = now.date().isoformat()
            tomorrow_date = (now + timedelta(days=1)).date().isoformat()
            day_after_date = (now + timedelta(days=2)).date().isoformat()
            yesterday_date = (now - timedelta(days=1)).date().isoformat()
            snapshot_timestamp = now.isoformat()
            await self.db.execute(
                "UPDATE ops_hourly_forecasts\n                   SET is_active = FALSE, superseded_at = ?\n                   WHERE target_date = ? AND is_active = TRUE",
                (snapshot_timestamp, yesterday_date),
            )
            await self.db.execute(
                "UPDATE ops_prediction_weather\n                   SET is_active = FALSE, superseded_at = ?\n                   WHERE target_date = ? AND is_active = TRUE",
                (snapshot_timestamp, yesterday_date),
            )
            await self.db.execute(
                "UPDATE ops_prediction_panel_groups\n                   SET is_active = FALSE, superseded_at = ?\n                   WHERE target_date = ? AND is_active = TRUE",
                (snapshot_timestamp, yesterday_date),
            )
            await self.db.execute(
                "UPDATE ops_daily_forecasts\n                   SET is_active = FALSE, superseded_at = ?\n                   WHERE forecast_date = ? AND is_active = TRUE",
                (snapshot_timestamp, yesterday_date),
            )
            _LOGGER.info(
                "Starting midnight forecast rotation for new day: %s", rotation_date
            )
            tomorrow_forecast = await self._get_forecast("tomorrow", rotation_date)
            day_after_forecast = await self._get_forecast(
                "day_after_tomorrow", tomorrow_date
            )
            await self.db.execute(
                "INSERT INTO ops_forecast_snapshots (\n                       snapshot_date, snapshot_type, trigger_source, triggered_at, status, notes\n                   ) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rotation_date,
                    "rotation_snapshot",
                    "midnight_rotation",
                    snapshot_timestamp,
                    "completed",
                    "Midnight rotation auto-init",
                ),
            )
            row = await self.db.fetchone("SELECT last_insert_rowid()")
            snapshot_sequence = int(row[0]) if row else None
            if snapshot_sequence:
                active_today_forecast = await self.db.fetchone(
                    "SELECT prediction_kwh, prediction_kwh_raw, source_method, actual_kwh\n                       FROM ops_daily_forecasts\n                       WHERE forecast_date = ? AND forecast_type = 'tomorrow' AND is_active = TRUE",
                    (rotation_date,),
                )
                prediction_kwh = (
                    tomorrow_forecast.get("prediction_kwh")
                    if tomorrow_forecast
                    else 0.0
                )
                prediction_kwh_raw = (
                    tomorrow_forecast.get("prediction_kwh")
                    if tomorrow_forecast
                    else 0.0
                )
                source_method = "rotated_from_tomorrow"
                actual_kwh = None
                if active_today_forecast:
                    prediction_kwh = active_today_forecast["prediction_kwh"]
                    prediction_kwh_raw = active_today_forecast["prediction_kwh_raw"]
                    source_method = active_today_forecast["source_method"]
                    actual_kwh = active_today_forecast["actual_kwh"]
                await self.db.execute(
                    "INSERT INTO ops_daily_forecasts (\n                           forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                           snapshot_created_at, trigger_source, prediction_kwh, prediction_kwh_raw,\n                           source_method, actual_kwh, is_active\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "today",
                        rotation_date,
                        "rotation_snapshot",
                        snapshot_sequence,
                        snapshot_timestamp,
                        "midnight_rotation",
                        prediction_kwh,
                        prediction_kwh_raw,
                        source_method,
                        actual_kwh,
                        True,
                    ),
                )
                active_tomorrow_forecast = await self.db.fetchone(
                    "SELECT prediction_kwh, prediction_kwh_raw, source_method, actual_kwh\n                       FROM ops_daily_forecasts\n                       WHERE forecast_date = ? AND forecast_type = 'day_after_tomorrow' AND is_active = TRUE",
                    (tomorrow_date,),
                )
                prediction_kwh = (
                    day_after_forecast.get("prediction_kwh")
                    if day_after_forecast
                    else 0.0
                )
                prediction_kwh_raw = (
                    day_after_forecast.get("prediction_kwh")
                    if day_after_forecast
                    else 0.0
                )
                source_method = "rotated_from_day_after"
                actual_kwh = None
                if active_tomorrow_forecast:
                    prediction_kwh = active_tomorrow_forecast["prediction_kwh"]
                    prediction_kwh_raw = active_tomorrow_forecast["prediction_kwh_raw"]
                    source_method = active_tomorrow_forecast["source_method"]
                    actual_kwh = active_tomorrow_forecast["actual_kwh"]
                await self.db.execute(
                    "INSERT INTO ops_daily_forecasts (\n                           forecast_type, forecast_date, snapshot_type, snapshot_sequence,\n                           snapshot_created_at, trigger_source, prediction_kwh, prediction_kwh_raw,\n                           source_method, actual_kwh, is_active\n                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "tomorrow",
                        tomorrow_date,
                        "rotation_snapshot",
                        snapshot_sequence,
                        snapshot_timestamp,
                        "midnight_rotation",
                        prediction_kwh,
                        prediction_kwh_raw,
                        source_method,
                        actual_kwh,
                        True,
                    ),
                )

                async def init_hourly_ops(target_date: str):
                    hourly_rows = await self.db.fetchall(
                        "SELECT target_hour, prediction_id, prediction_kwh, prediction_kwh_uncapped,\n                                  prediction_method, physics_kwh, ai_kwh, ai_confidence, ml_contribution_percent,\n                                  lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, is_production_hour, actual_kwh, source_track\n                           FROM ops_hourly_forecasts\n                           WHERE target_date = ? AND is_active = TRUE",
                        (target_date,),
                    )
                    if hourly_rows:
                        for hourly_row in hourly_rows:
                            await self.db.execute(
                                "INSERT INTO ops_hourly_forecasts (\n                                       ops_track_date, target_date, target_hour, prediction_id,\n                                       snapshot_type, snapshot_sequence, snapshot_created_at, trigger_source,\n                                       is_active, prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                                       physics_kwh, ai_kwh, ai_confidence, ml_contribution_percent,\n                                       lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, is_production_hour,\n                                       actual_kwh, source_track\n                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    rotation_date,
                                    target_date,
                                    hourly_row["target_hour"],
                                    hourly_row["prediction_id"],
                                    "rotation_snapshot",
                                    snapshot_sequence,
                                    snapshot_timestamp,
                                    "midnight_rotation",
                                    True,
                                    hourly_row["prediction_kwh"],
                                    hourly_row["prediction_kwh_uncapped"],
                                    hourly_row["prediction_method"],
                                    hourly_row["physics_kwh"],
                                    hourly_row["ai_kwh"],
                                    hourly_row["ai_confidence"],
                                    hourly_row["ml_contribution_percent"],
                                    hourly_row["lstm_kwh"],
                                    hourly_row["ridge_kwh"],
                                    hourly_row["tfs_kwh"],
                                    hourly_row["tfs_weight"],
                                    hourly_row["is_production_hour"],
                                    hourly_row["actual_kwh"],
                                    hourly_row["source_track"],
                                ),
                            )
                    else:
                        core_hourly_rows = await self.db.fetchall(
                            "SELECT target_hour, prediction_id, prediction_kwh, prediction_kwh_uncapped,\n                                      prediction_method, physics_kwh, ai_kwh, ai_confidence, ml_contribution_percent,\n                                      lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, actual_kwh\n                               FROM hourly_predictions\n                               WHERE target_date = ?\n                               ORDER BY target_hour",
                            (target_date,),
                        )
                        for core_hourly_row in core_hourly_rows:
                            await self.db.execute(
                                "INSERT INTO ops_hourly_forecasts (\n                                       ops_track_date, target_date, target_hour, prediction_id,\n                                       snapshot_type, snapshot_sequence, snapshot_created_at, trigger_source,\n                                       is_active, prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                                       physics_kwh, ai_kwh, ai_confidence, ml_contribution_percent,\n                                       lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, is_production_hour,\n                                       actual_kwh, source_track\n                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    rotation_date,
                                    target_date,
                                    core_hourly_row["target_hour"],
                                    core_hourly_row["prediction_id"],
                                    "rotation_snapshot",
                                    snapshot_sequence,
                                    snapshot_timestamp,
                                    "midnight_rotation",
                                    True,
                                    core_hourly_row["prediction_kwh"],
                                    core_hourly_row["prediction_kwh_uncapped"],
                                    core_hourly_row["prediction_method"],
                                    core_hourly_row["physics_kwh"],
                                    core_hourly_row["ai_kwh"],
                                    core_hourly_row["ai_confidence"],
                                    core_hourly_row["ml_contribution_percent"],
                                    core_hourly_row["lstm_kwh"],
                                    core_hourly_row["ridge_kwh"],
                                    core_hourly_row["tfs_kwh"],
                                    core_hourly_row["tfs_weight"],
                                    (
                                        True
                                        if core_hourly_row["prediction_kwh"] > 0
                                        else False
                                    ),
                                    core_hourly_row["actual_kwh"],
                                    "rotated_from_core",
                                ),
                            )
                    return None if True else None

                await init_hourly_ops(rotation_date)
                await init_hourly_ops(tomorrow_date)
            if tomorrow_forecast:
                await self.db.execute(
                    "INSERT INTO daily_forecasts (\n                        forecast_type, forecast_date, prediction_kwh,\n                        locked, locked_at, source\n                    ) VALUES (?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                        prediction_kwh = excluded.prediction_kwh,\n                        locked = FALSE,\n                        locked_at = NULL,\n                        source = excluded.source",
                    (
                        "today",
                        rotation_date,
                        tomorrow_forecast.get("prediction_kwh"),
                        False,
                        None,
                        "rotated_from_tomorrow",
                    ),
                )
            if day_after_forecast:
                await self.db.execute(
                    "INSERT INTO daily_forecasts (\n                        forecast_type, forecast_date, prediction_kwh,\n                        locked, locked_at, source\n                    ) VALUES (?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                        prediction_kwh = excluded.prediction_kwh,\n                        locked = FALSE,\n                        locked_at = NULL,\n                        source = excluded.source",
                    (
                        "tomorrow",
                        tomorrow_date,
                        day_after_forecast.get("prediction_kwh"),
                        False,
                        None,
                        "rotated_from_day_after",
                    ),
                )
            await self.db.execute(
                "DELETE FROM daily_forecasts\n                   WHERE forecast_type = 'day_after_tomorrow'\n                   AND forecast_date = ?",
                (day_after_date,),
            )
            _LOGGER.info(
                "Midnight rotation complete: today=%s (unlocked), tomorrow=%s",
                rotation_date,
                tomorrow_date,
            )
        except Exception as error:
            _LOGGER.error(
                "Failed to rotate forecasts at midnight: %s", error, exc_info=True
            )
            return False
        return True

    async def lock_today_forecast(self) -> bool:
        """Lock today's forecast (called at 6:00 AM). @zara

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            today_date = now.date().isoformat()
            await self.db.execute(
                "UPDATE daily_forecasts\n                   SET locked = TRUE, locked_at = ?\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (now.isoformat(), today_date),
            )
            _LOGGER.info("Today's forecast locked at %s", now.isoformat())
        except Exception as error:
            _LOGGER.error("Failed to lock today's forecast: %s", error)
            return False
        return True

    async def get_forecast_updates(
        self, forecast_type: str, date: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get update history for a forecast. @zara

        Args:
            forecast_type: Type of forecast
            date: Forecast date
            limit: Maximum number of updates to return

        Returns:
            List of update dicts
        """
        try:
            await self.ensure_initialized()
            history_rows = await self.db.fetchall(
                "SELECT prediction_kwh, source, updated_at\n                   FROM daily_forecast_updates\n                   WHERE forecast_type = ? AND forecast_date = ?\n                   ORDER BY updated_at DESC\n                   LIMIT ?",
                (forecast_type, date, limit),
            )
            return [
                {"prediction_kwh": row[0], "source": row[1], "updated_at": row[2]}
                for row in history_rows
            ]
        except Exception as error:
            _LOGGER.error("Failed to get forecast updates: %s", error)
            return []

    async def get_forecast_history(
        self,
        days: int = 30,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get historical forecasts. @zara

        Args:
            days: Number of days to look back (if no dates specified)
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            List of forecast dicts
        """
        try:
            await self.ensure_initialized()
            if start_date and end_date:
                history_rows = await self.db.fetchall(
                    "SELECT forecast_type, forecast_date, prediction_kwh,\n                              locked, source, created_at\n                       FROM daily_forecasts\n                       WHERE forecast_date BETWEEN ? AND ?\n                       ORDER BY forecast_date DESC, forecast_type",
                    (start_date, end_date),
                )
            else:
                cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
                history_rows = await self.db.fetchall(
                    "SELECT forecast_type, forecast_date, prediction_kwh,\n                              locked, source, created_at\n                       FROM daily_forecasts\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date DESC, forecast_type",
                    (cutoff_date,),
                )
            return [
                {
                    "forecast_type": row[0],
                    "forecast_date": row[1],
                    "prediction_kwh": row[2],
                    "locked": bool(row[3]),
                    "source": row[4],
                    "created_at": row[5],
                }
                for row in history_rows
            ]
        except Exception as error:
            _LOGGER.error("Failed to get forecast history: %s", error)
            return []

    async def cleanup_old_forecasts(self, days_to_keep: int = 365) -> int:
        """Remove forecasts older than specified days. @zara

        Args:
            days_to_keep: Number of days of data to retain

        Returns:
            Number of deleted records
        """
        try:
            await self.ensure_initialized()
            cutoff_date = (
                (dt_util.now() - timedelta(days=days_to_keep)).date().isoformat()
            )
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM daily_forecasts WHERE forecast_date < ?",
                (cutoff_date,),
            )
            deleted_count = row[0] if row else 0
            if deleted_count > 0:
                await self.db.execute(
                    "DELETE FROM daily_forecasts WHERE forecast_date < ?",
                    (cutoff_date,),
                )
                await self.db.execute(
                    "DELETE FROM daily_forecast_updates WHERE forecast_date < ?",
                    (cutoff_date,),
                )
                _LOGGER.info("Cleaned up %d old forecasts", deleted_count)
            return deleted_count
        except Exception as error:
            _LOGGER.error("Failed to cleanup old forecasts: %s", error)
            return 0

    async def update_daily_tracking(
        self,
        forecast_best_hour: Optional[Dict[str, Any]] = None,
        actual_best_hour: Optional[Dict[str, Any]] = None,
        forecast_next_hour: Optional[Dict[str, Any]] = None,
        production_time: Optional[Dict[str, Any]] = None,
        peak_today: Optional[Dict[str, Any]] = None,
        yield_today: Optional[Dict[str, Any]] = None,
        consumption_today: Optional[Dict[str, Any]] = None,
        autarky: Optional[Dict[str, Any]] = None,
        finalized: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update daily forecast tracking data. @zara

        This method updates the daily tracking record with current data.
        Should be called whenever relevant tracking values change.

        Args:
            forecast_best_hour: Dict with hour, prediction_kwh, locked, source
            actual_best_hour: Dict with hour, actual_kwh, saved_at
            forecast_next_hour: Dict with period, prediction_kwh, updated_at, source
            production_time: Dict with active, duration_seconds, timestamps
            peak_today: Dict with power_w, at
            yield_today: Dict with kwh, sensor
            consumption_today: Dict with kwh, sensor
            autarky: Dict with percent, calculated_at
            finalized: Dict with yield_kwh, consumption_kwh, production_hours,
                      accuracy_percent, excluded_hours, at

        Returns:
            True if successful

        Example:
            await handler.update_daily_tracking(
                peak_today={"power_w": 3500, "at": "2026-02-02T12:30:00"},
                yield_today={"kwh": 12.5, "sensor": "sensor.solar_yield"}
            )
        """
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            today_date = now.date().isoformat()
            existing_forecast = await self.db.get_daily_forecast_tracking()
            if not existing_forecast:
                existing_forecast = {"date": today_date}
            if forecast_best_hour is not None:
                existing_forecast["forecast_best_hour"] = forecast_best_hour
            if actual_best_hour is not None:
                existing_forecast["actual_best_hour"] = actual_best_hour
            if forecast_next_hour is not None:
                existing_forecast["forecast_next_hour"] = forecast_next_hour
            if production_time is not None:
                existing_forecast["production_time"] = production_time
            if peak_today is not None:
                existing_forecast["peak_today"] = peak_today
            if yield_today is not None:
                existing_forecast["yield_today"] = yield_today
            if consumption_today is not None:
                existing_forecast["consumption_today"] = consumption_today
            if autarky is not None:
                existing_forecast["autarky"] = autarky
            if finalized is not None:
                existing_forecast["finalized"] = finalized
            existing_forecast["date"] = today_date
            await self.db.save_daily_forecast_tracking(existing_forecast)
            _LOGGER.debug("Daily tracking updated for %s", today_date)
        except Exception as error:
            _LOGGER.error("Failed to update daily tracking: %s", error, exc_info=True)
            return False
        return True

    async def get_daily_tracking(self) -> Optional[Dict[str, Any]]:
        """Get current daily forecast tracking data. @zara

        Returns:
            Dict with tracking data or None if not found
        """
        try:
            await self.ensure_initialized()
            return await self.db.get_daily_forecast_tracking()
        except Exception as error:
            _LOGGER.error("Failed to get daily tracking: %s", error)
            return

    async def _get_forecast(
        self, forecast_type: str, date: str
    ) -> Optional[Dict[str, Any]]:
        """Get a specific forecast from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT forecast_date, prediction_kwh, prediction_kwh_raw,\n                          safeguard_applied, safeguard_reduction_kwh,\n                          locked, locked_at, source, created_at\n                   FROM daily_forecasts\n                   WHERE forecast_type = ? AND forecast_date = ?",
                (forecast_type, date),
            )
            if not row:
                return
            return {
                "date": row[0],
                "prediction_kwh": row[1],
                "prediction_kwh_raw": row[2],
                "safeguard_applied": bool(row[3]) if row[3] is not None else False,
                "safeguard_reduction_kwh": row[4],
                "locked": bool(row[5]),
                "locked_at": row[6],
                "source": row[7],
                "created_at": row[8],
            }
        except Exception as error:
            _LOGGER.error("Failed to get forecast: %s", error)
            return

    async def _save_forecast_update(
        self, forecast_type: str, date: str, prediction_kwh: float, source: str
    ) -> None:
        """Save forecast update to history. @zara"""
        await self.db.execute(
            "INSERT INTO daily_forecast_updates (\n                forecast_type, forecast_date, prediction_kwh, source, updated_at\n            ) VALUES (?, ?, ?, ?, ?)",
            (
                forecast_type,
                date,
                round(prediction_kwh, 2),
                source,
                dt_util.now().isoformat(),
            ),
        )
