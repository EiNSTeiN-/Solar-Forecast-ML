# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Data Manager for Solar Forecast ML V16.2.0.
MAIN DATA MANAGER - Central API for all data operations.
Uses DatabaseManager for all database operations.

@zara
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..ai.ai_types import HourlyProfile, LearnedWeights
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import (
    DatabaseManager,
    loads_protected_json,
    normalize_hourly_target_datetime,
    note_unbound_artifact_key,
    seal_protected_text,
)
from .db_manager import artifact_crypto as _artifact_crypto
from .data_io import DataManagerIO
from .data_adapter import TypedDataAdapter
from .data_backup_handler import DataBackupHandler
from .data_operational_forecast_handler import DataOperationalForecastHandler
from .data_persistence import DataPersistence
from .data_state_handler import DataStateHandler
from .data_startup_initializer import StartupInitializer
from .data_weather_radiation_ensemble import MIN_PREDICTION_HOURS_TO_LOCK_DAY

_LOGGER = logging.getLogger(__name__)


class DataManager(DataManagerIO):
    "Main Data Manager API for Solar Forecast ML. @zara\n\nCentral facade providing unified access to all data operations.\nAll specialized handlers use the shared DatabaseManager.\n\nUsage:\n    data_manager = DataManager(hass, entry_id, data_dir)\n    await data_manager.initialize()\n    weights = await data_manager.load_learned_weights()\n"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        data_dir: Path,
        config: Optional[dict[str, Any]] = None,
    ):
        "Initialize the Data Manager. @zara\n\nArgs:\n    hass: Home Assistant instance\n    entry_id: Integration entry ID\n    data_dir: Base data directory path\n    config: Optional configuration dictionary\n"
        self.hass = hass
        self.data_dir = Path(data_dir)
        self.entry_id = entry_id
        self.config = config or {}
        self._db_manager = None
        self._startup_config = config or {}
        self.state_handler = None
        self.backup_handler = None
        self.persistence = None
        self.operational_forecast_handler = None
        self.data_adapter = TypedDataAdapter()
        _LOGGER.debug("DataManager created - awaiting initialize()")

    async def initialize(self) -> bool:
        """Async initialization - creates database and connects. @zara

        Returns:
            True if initialization successful
        """
        try:
            _LOGGER.debug("DataManager: Running startup initializer...")
            startup_initializer = StartupInitializer(
                self.data_dir, self._startup_config
            )
            startup_ok = await self.hass.async_add_executor_job(
                startup_initializer.initialize_all
            )
            if not startup_ok:
                raise RuntimeError("SFML database startup initialization failed")
            database_path = startup_initializer.get_db_path()
            self._db_manager = DatabaseManager(database_path)
            instance_id = None
            try:
                from homeassistant.helpers.instance_id import (
                    async_get as get_instance_id,
                )

                instance_id = await get_instance_id(self.hass)
            except Exception:
                instance_id = None
            if isinstance(instance_id, str) and instance_id:
                self._db_manager.artifact_instance_id = instance_id
            else:
                self._db_manager.artifact_instance_id = None
                note_unbound_artifact_key("startup")
            super().__init__(self.hass, self._db_manager)
            self.state_handler = DataStateHandler(self.hass, self._db_manager)
            self.backup_handler = DataBackupHandler(
                self.hass, self._db_manager, self.data_dir
            )
            self.persistence = DataPersistence(
                self.hass, self._db_manager, self.data_dir
            )
            self.operational_forecast_handler = DataOperationalForecastHandler(
                self.hass, self._db_manager
            )
            _LOGGER.debug("DataManager: Connecting to database...")
            await self._db_manager.connect()
            from ..core.database_service import (
                async_attach_database_manager as attach_database_manager,
            )

            await attach_database_manager(self.hass, self._db_manager)
            self._initialized = True
            _LOGGER.debug("DataManager initialized successfully")
            return True
        except Exception as error:
            if self._db_manager is not None:
                try:
                    await self._db_manager.close()
                except Exception:
                    _LOGGER.exception(
                        "Failed to close database after initialization error"
                    )
            _LOGGER.error("Failed to initialize DataManager: %s", error, exc_info=True)
        return False

    async def async_initialize(self) -> None:
        """Alias for initialize(). @zara"""
        await self.initialize()

    async def save_forecast_day(
        self,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = True,
        force_overwrite: bool = False,
        prediction_kwh_raw: Optional[float] = None,
        safeguard_applied: bool = False,
    ) -> bool:
        """Save today's daily forecast. @zara"""
        unused_value = None
        try:
            current_time = dt_util.now()
            forecast_date = current_time.date().isoformat()
            if not await self._has_hourly_forecast_coverage(forecast_date):
                _LOGGER.info(
                    "Skipping today aggregate for %s: incomplete hourly horizon",
                    forecast_date,
                )
                return False
            async with self.db.transaction():
                lock_row = await self.db.fetchone(
                    "SELECT locked\n                       FROM daily_forecasts\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (forecast_date,),
                )
                if lock_row and lock_row[0]:
                    _LOGGER.debug(
                        "Skipping today forecast update - locked by morning routine"
                    )
                    return False
                await self.db.execute(
                    "INSERT INTO daily_forecasts\n                       (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                        safeguard_applied, locked, locked_at, source)\n                       VALUES ('today', ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                           prediction_kwh = excluded.prediction_kwh,\n                           prediction_kwh_raw = excluded.prediction_kwh_raw,\n                           safeguard_applied = excluded.safeguard_applied,\n                           locked = CASE WHEN excluded.locked THEN TRUE ELSE locked END,\n                           locked_at = CASE WHEN excluded.locked THEN excluded.locked_at ELSE locked_at END,\n                           source = excluded.source\n                       WHERE daily_forecasts.locked = 0 OR daily_forecasts.locked IS NULL",
                    (
                        forecast_date,
                        prediction_kwh,
                        prediction_kwh_raw,
                        safeguard_applied,
                        lock,
                        current_time.isoformat() if lock else None,
                        source,
                    ),
                    auto_commit=False,
                )
            _LOGGER.debug(
                "Saved forecast day: %.2f kWh (source: %s)", prediction_kwh, source
            )
            return True
        except Exception as error:
            _LOGGER.error("Failed to save forecast day: %s", error)
            return False

    async def _has_hourly_forecast_coverage(self, date_str: str) -> bool:
        """Return whether a daily aggregate has its required hourly substrate."""
        database_row = await self.db.fetchone(
            "SELECT COUNT(DISTINCT target_hour)\n               FROM hourly_predictions\n               WHERE target_date = ?",
            (date_str,),
        )
        return bool(
            database_row
            and int(database_row[0] or 0) >= MIN_PREDICTION_HOURS_TO_LOCK_DAY
        )

    async def save_forecast_tomorrow(
        self,
        target_date: datetime,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = False,
    ) -> bool:
        """Save tomorrow's forecast. Only writes if NOT locked (morning routine has priority). @zara"""
        try:
            current_time = dt_util.now()
            forecast_date = target_date.date().isoformat()
            if not await self._has_hourly_forecast_coverage(forecast_date):
                _LOGGER.info(
                    "Skipping tomorrow aggregate for %s: incomplete hourly horizon",
                    forecast_date,
                )
                return False
            lock_row = await self.db.fetchone(
                "SELECT locked FROM daily_forecasts\n                   WHERE forecast_type = 'tomorrow' AND forecast_date = ?",
                (forecast_date,),
            )
            if lock_row and lock_row[0]:
                _LOGGER.debug(
                    "Skipping tomorrow forecast update - locked by morning routine"
                )
                return False
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, locked, locked_at, source)\n                   VALUES ('tomorrow', ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = CASE WHEN NOT locked THEN excluded.prediction_kwh ELSE prediction_kwh END,\n                       locked = CASE WHEN excluded.locked THEN TRUE ELSE locked END,\n                       locked_at = CASE WHEN excluded.locked AND NOT locked THEN excluded.locked_at ELSE locked_at END,\n                       source = CASE WHEN NOT locked THEN excluded.source ELSE source END",
                (
                    forecast_date,
                    prediction_kwh,
                    lock,
                    current_time.isoformat() if lock else None,
                    source,
                ),
            )
            await self.db.execute(
                "INSERT INTO daily_forecast_updates\n                   (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                   VALUES ('tomorrow', ?, ?, ?, ?)",
                (forecast_date, prediction_kwh, source, current_time.isoformat()),
            )
        except Exception as error:
            _LOGGER.error("Failed to save forecast tomorrow: %s", error)
            return False
        return True

    async def save_forecast_day_after(
        self,
        target_date: datetime,
        prediction_kwh: float,
        source: str = "ML",
        lock: bool = False,
    ) -> bool:
        """Save day after tomorrow's forecast. Only writes if NOT locked (morning routine has priority). @zara"""
        try:
            current_time = dt_util.now()
            forecast_date = target_date.date().isoformat()
            if not await self._has_hourly_forecast_coverage(forecast_date):
                _LOGGER.info(
                    "Skipping day-after aggregate for %s: incomplete hourly horizon",
                    forecast_date,
                )
                return False
            lock_row = await self.db.fetchone(
                "SELECT locked FROM daily_forecasts\n                   WHERE forecast_type = 'day_after_tomorrow' AND forecast_date = ?",
                (forecast_date,),
            )
            if lock_row and lock_row[0]:
                _LOGGER.debug(
                    "Skipping day_after_tomorrow forecast update - locked by morning routine"
                )
                return False
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, locked, locked_at, source)\n                   VALUES ('day_after_tomorrow', ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = CASE WHEN NOT locked THEN excluded.prediction_kwh ELSE prediction_kwh END,\n                       source = CASE WHEN NOT locked THEN excluded.source ELSE source END",
                (
                    forecast_date,
                    prediction_kwh,
                    lock,
                    current_time.isoformat() if lock else None,
                    source,
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save forecast day after tomorrow: %s", error)
            return False
        return True

    async def load_daily_forecasts(self) -> dict[str, Any]:
        """Load all daily forecasts. @zara"""
        try:
            database_rows = await self.db.fetchall(
                "SELECT forecast_type, forecast_date, prediction_kwh,\n                          prediction_kwh_raw, safeguard_applied, locked,\n                          locked_at, source\n                   FROM daily_forecasts\n                   WHERE forecast_date >= DATE('now', '-1 day')\n                   ORDER BY forecast_date"
            )
            daily_forecasts = {"today": {}, "tomorrow": {}, "day_after_tomorrow": {}}
            for database_row in database_rows:
                forecast_type = database_row[0]
                daily_forecasts[forecast_type] = {
                    "date": str(database_row[1]),
                    "prediction_kwh": database_row[2],
                    "prediction_kwh_raw": database_row[3],
                    "safeguard_applied": (
                        bool(database_row[4]) if database_row[4] is not None else False
                    ),
                    "locked": (
                        bool(database_row[5]) if database_row[5] is not None else False
                    ),
                    "locked_at": database_row[6],
                    "source": database_row[7],
                }
            return daily_forecasts
        except Exception as error:
            _LOGGER.error("Failed to load daily forecasts: %s", error)
            return {"today": {}, "tomorrow": {}, "day_after_tomorrow": {}}

    async def get_current_day_forecast(self) -> Optional[dict[str, Any]]:
        """Get today's forecast. @zara"""
        try:
            forecast_date = dt_util.now().date().isoformat()
            database_row = await self.db.fetchone(
                "SELECT forecast_type, forecast_date, prediction_kwh,\n                          prediction_kwh_raw, safeguard_applied, locked,\n                          locked_at, source\n                   FROM daily_forecasts\n                   WHERE forecast_type = 'today'\n                     AND forecast_date = ?",
                (forecast_date,),
            )
            if not database_row:
                return
            return {
                "date": str(database_row[1]),
                "prediction_kwh": database_row[2],
                "prediction_kwh_raw": database_row[3],
                "safeguard_applied": (
                    bool(database_row[4]) if database_row[4] is not None else False
                ),
                "locked": (
                    bool(database_row[5]) if database_row[5] is not None else False
                ),
                "locked_at": database_row[6],
                "source": database_row[7],
            }
        except Exception as error:
            _LOGGER.error("Failed to get current day forecast: %s", error)
            return

    async def save_daily_forecast(
        self, prediction_kwh: float, source: str = "ML", force_overwrite: bool = False
    ) -> bool:
        """Save daily forecast (simplified wrapper for save_forecast_day). @zara"""
        return await self.save_forecast_day(
            prediction_kwh=prediction_kwh,
            source=source,
            lock=True,
            force_overwrite=force_overwrite,
        )

    async def save_learned_weights(self, weights: LearnedWeights) -> bool:
        """Save learned weights to database. @zara"""
        try:
            serialized_weights = self.data_adapter.learned_weights_to_dict(weights)
            await self.db.save_model_weights(serialized_weights)
            _LOGGER.debug("Learned weights saved")
        except Exception as error:
            _LOGGER.error("Failed to save learned weights: %s", error)
            return False
        return True

    async def load_learned_weights(self) -> Optional[LearnedWeights]:
        """Load learned weights from database. @zara"""
        try:
            weights_data = await self.db.get_model_weights()
            if weights_data is None:
                return
            return self.data_adapter.dict_to_learned_weights(weights_data)
        except Exception as error:
            _LOGGER.error("Failed to load learned weights: %s", error)
            return

    async def get_learned_weights(self) -> Optional[LearnedWeights]:
        """Alias for load_learned_weights. @zara"""
        return await self.load_learned_weights()

    async def delete_learned_weights(self) -> bool:
        """Delete learned weights from database. @zara"""
        try:
            await self.db.execute("DELETE FROM ai_model_weights")
            _LOGGER.info("Learned weights deleted")
        except Exception as error:
            _LOGGER.error("Failed to delete learned weights: %s", error)
            return False
        return True

    async def save_hourly_profile(self, profile: HourlyProfile) -> bool:
        """Save hourly profile to database. @zara"""
        try:
            profile_data = self.data_adapter.hourly_profile_to_dict(profile)
            import json as json_module

            await self.db.execute(
                "INSERT INTO ai_model_weights (weight_type, weight_data, updated_at)\n                   VALUES ('hourly_profile', ?, ?)\n                   ON CONFLICT(weight_type) DO UPDATE SET\n                       weight_data = excluded.weight_data,\n                       updated_at = excluded.updated_at",
                (
                    seal_protected_text(
                        json_module.dumps(profile_data),
                        "ai_model_weights",
                        "hourly_profile",
                    ),
                    dt_util.now().isoformat(),
                ),
            )
            _LOGGER.debug("Hourly profile saved")
        except _artifact_crypto.ArtifactCryptoError:
            note_unbound_artifact_key("hourly profile")
            return False
        except Exception as error:
            _LOGGER.error("Failed to save hourly profile: %s", error)
            return False
        return True

    async def load_hourly_profile(self) -> Optional[HourlyProfile]:
        """Load hourly profile from database. @zara"""
        try:
            import json as json_module

            database_row = await self.db.fetchone(
                "SELECT weight_data FROM ai_model_weights WHERE weight_type = 'hourly_profile'"
            )
            if database_row is None:
                return
            profile_data = loads_protected_json(
                database_row[0], "ai_model_weights", "hourly_profile", "weights"
            )
            return self.data_adapter.dict_to_hourly_profile(profile_data)
        except Exception as error:
            _LOGGER.error("Failed to load hourly profile: %s", error)
            return

    async def get_hourly_profile(self) -> Optional[HourlyProfile]:
        """Alias for load_hourly_profile. @zara"""
        return await self.load_hourly_profile()

    async def save_expected_daily_production(self, value: float) -> bool:
        """Save expected daily production. @zara"""
        return await self.state_handler.save_expected_daily_production(value)

    async def load_expected_daily_production(self) -> Optional[float]:
        """Load expected daily production. @zara"""
        return await self.state_handler.load_expected_daily_production()

    async def clear_expected_daily_production(self) -> bool:
        """Clear expected daily production. @zara"""
        return await self.state_handler.clear_expected_daily_production()

    async def get_last_collected_hour(self) -> Optional[datetime]:
        """Get last collected hour timestamp. @zara"""
        return await self.state_handler.get_last_collected_hour()

    async def set_last_collected_hour(self, timestamp: datetime) -> bool:
        """Set last collected hour timestamp. @zara"""
        return await self.state_handler.set_last_collected_hour(timestamp)

    async def create_backup(
        self, backup_name: Optional[str] = None, backup_type: str = "manual"
    ) -> bool:
        """Create backup. @zara"""
        return await self.backup_handler.create_backup(backup_name, backup_type)

    async def cleanup_old_backups(
        self, backup_type: str = "auto", retention_days: Optional[int] = None
    ) -> int:
        """Cleanup old backups. @zara"""
        return await self.backup_handler.cleanup_old_backups(
            backup_type, retention_days
        )

    async def cleanup_excess_backups(
        self, backup_type: str = "auto", max_backups: Optional[int] = None
    ) -> int:
        """Cleanup excess backups. @zara"""
        return await self.backup_handler.cleanup_excess_backups(
            backup_type, max_backups
        )

    async def list_backups(self, backup_type: Optional[str] = None) -> list:
        """List backups. @zara"""
        return await self.backup_handler.list_backups(backup_type)

    async def restore_backup(
        self, backup_name: str, backup_type: str = "manual"
    ) -> bool:
        """Restore backup. @zara"""
        return await self.backup_handler.restore_backup(backup_name, backup_type)

    async def delete_backup(
        self, backup_name: str, backup_type: str = "manual"
    ) -> bool:
        """Delete backup. @zara"""
        return await self.backup_handler.delete_backup(backup_name, backup_type)

    async def get_backup_info(
        self, backup_name: str, backup_type: str = "manual"
    ) -> Optional[dict]:
        """Get backup info. @zara"""
        return await self.backup_handler.get_backup_info(backup_name, backup_type)

    async def save_hourly_prediction(self, prediction: dict[str, Any]) -> bool:
        """Save an hourly prediction. @zara"""
        try:
            target_datetime_iso = normalize_hourly_target_datetime(
                prediction.get("target_datetime"),
                prediction.get("target_date"),
                prediction.get("target_hour"),
                local_timezone=dt_util.get_default_time_zone(),
            ).isoformat()
            await self.db.execute(
                "INSERT INTO hourly_predictions\n                   (prediction_id, prediction_created_at, prediction_created_hour,\n                    target_datetime, target_date, target_hour, prediction_kwh,\n                    prediction_method, model_version, confidence)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(prediction_id) DO UPDATE SET\n                       prediction_kwh = excluded.prediction_kwh,\n                       model_version = excluded.model_version",
                (
                    prediction.get("prediction_id"),
                    prediction.get("created_at"),
                    prediction.get("created_hour"),
                    target_datetime_iso,
                    prediction.get("target_date"),
                    prediction.get("target_hour"),
                    prediction.get("prediction_kwh"),
                    prediction.get("method"),
                    prediction.get("model_version"),
                    prediction.get("confidence"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save hourly prediction: %s", error)
            return False
        return True

    async def update_hourly_prediction_actual(
        self, prediction_id: str, actual_kwh: float
    ) -> bool:
        """Update hourly prediction with actual value. @zara"""
        try:
            current_time = dt_util.now()
            database_row = await self.db.fetchone(
                "SELECT prediction_kwh FROM hourly_predictions WHERE prediction_id = ?",
                (prediction_id,),
            )
            if database_row is None:
                return False
            predicted_kwh = float(database_row[0])
            if predicted_kwh > 0:
                accuracy_percent = max(
                    0, 100 - abs(predicted_kwh - actual_kwh) / predicted_kwh * 100
                )
                error_kwh = actual_kwh - predicted_kwh
                error_percent = (
                    error_kwh / predicted_kwh * 100 if predicted_kwh > 0 else 0
                )
            else:
                accuracy_percent = 100 if actual_kwh == 0 else 0
                error_kwh = actual_kwh
                error_percent = 0
            await self.db.execute(
                "UPDATE hourly_predictions SET\n                       actual_kwh = ?,\n                       actual_measured_at = ?,\n                       accuracy_percent = ?,\n                       error_kwh = ?,\n                       error_percent = ?\n                   WHERE prediction_id = ?",
                (
                    actual_kwh,
                    current_time.isoformat(),
                    accuracy_percent,
                    error_kwh,
                    error_percent,
                    prediction_id,
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to update hourly prediction actual: %s", error)
            return False
        return True

    async def get_hourly_predictions(
        self,
        target_date: str,
        with_actual: bool = False,
        with_shadow: bool = True,
        with_weather_actual: bool = True,
    ) -> list[dict[str, Any]]:
        "Get hourly predictions for a date with optional shadow/weather data. @zara\n\nArgs:\n    target_date: Date in YYYY-MM-DD format\n    with_actual: Only return predictions with actual_kwh\n    with_shadow: Include shadow_detection data (default True)\n    with_weather_actual: Include weather_actual data incl. snow (default True)\n"
        try:
            if with_actual:
                query = "SELECT * FROM hourly_predictions\n                         WHERE target_date = ? AND actual_kwh IS NOT NULL\n                         ORDER BY target_hour"
            else:
                query = "SELECT * FROM hourly_predictions\n                         WHERE target_date = ?\n                         ORDER BY target_hour"
            database_rows = await self.db.fetchall(query, (target_date,))
            predictions = [
                self.data_adapter.row_to_dict(database_row)
                for database_row in database_rows
            ]
            if with_shadow and predictions:
                shadow_by_prediction = await self._load_shadow_detection_for_date(
                    target_date
                )
                for prediction_row in predictions:
                    prediction_id = prediction_row.get("prediction_id")
                    if prediction_id and prediction_id in shadow_by_prediction:
                        prediction_row["shadow_detection"] = shadow_by_prediction[
                            prediction_id
                        ]
            if with_weather_actual and predictions:
                weather_by_hour = await self._load_weather_actual_for_date(target_date)
                for prediction_row in predictions:
                    target_hour = prediction_row.get("target_hour")
                    if target_hour is not None and target_hour in weather_by_hour:
                        prediction_row["weather_actual"] = weather_by_hour[target_hour]
            return predictions
        except Exception as error:
            _LOGGER.error("Failed to get hourly predictions: %s", error)
            return []

    async def _load_weather_actual_for_date(
        self, target_date: str
    ) -> dict[int, dict[str, Any]]:
        "Load weather actual data for all hours of a date. @zara V16.1\n\nIncludes snow detection info for each hour.\n\nReturns:\n    Dict mapping hour (int) to weather_actual dict\n"
        try:
            database_rows = await self.db.fetchall(
                "SELECT hour, temperature_c, humidity_percent, cloud_cover_percent,\n                          precipitation_mm, wind_speed_ms, frost_detected, frost_score,\n                          frost_confidence, frost_notification_sent, frost_type,\n                          snow_covered_panels, snow_coverage_source, snow_confidence,\n                          snow_event_detected, snow_clearing_progress\n                   FROM hourly_weather_actual\n                   WHERE date = ?",
                (target_date,),
            )
            weather_by_hour = {}
            for database_row in database_rows:
                target_hour = database_row[0]
                weather_by_hour[target_hour] = {
                    "hour": target_hour,
                    "temperature_c": database_row[1],
                    "humidity_percent": database_row[2],
                    "cloud_cover_percent": database_row[3],
                    "precipitation_mm": database_row[4],
                    "wind_speed_ms": database_row[5],
                    "frost_detected": (
                        database_row[6]
                        if database_row[6] not in (None, "none")
                        else False
                    ),
                    "frost_score": database_row[7],
                    "frost_confidence": database_row[8],
                    "frost_notification_sent": (
                        bool(database_row[9]) if database_row[9] is not None else False
                    ),
                    "frost_type": database_row[10],
                    "snow_covered_panels": (
                        bool(database_row[11])
                        if database_row[11] is not None
                        else False
                    ),
                    "snow_coverage_source": database_row[12],
                    "snow_confidence": database_row[13],
                    "snow_event_detected": (
                        bool(database_row[14])
                        if database_row[14] is not None
                        else False
                    ),
                    "snow_clearing_progress": database_row[15],
                }
            return weather_by_hour
        except Exception as error:
            _LOGGER.debug("Failed to load weather actual: %s", error)
            return {}

    async def _load_shadow_detection_for_date(
        self, target_date: str
    ) -> dict[str, dict[str, Any]]:
        """Load shadow detection data for all predictions of a date. @zara V16.1

        Returns:
            Dict mapping prediction_id to shadow_detection dict
        """
        try:
            database_rows = await self.db.fetchall(
                "SELECT sd.* FROM hourly_shadow_detection sd\n                   JOIN hourly_predictions hp ON sd.prediction_id = hp.prediction_id\n                   WHERE hp.target_date = ?",
                (target_date,),
            )
            shadow_by_prediction = {}
            for database_row in database_rows:
                shadow_row = self.data_adapter.row_to_dict(database_row)
                prediction_id = shadow_row.get("prediction_id")
                if prediction_id:
                    shadow_by_prediction[prediction_id] = {
                        "method": shadow_row.get("method"),
                        "ensemble_mode": shadow_row.get("ensemble_mode"),
                        "shadow_type": shadow_row.get("shadow_type"),
                        "shadow_percent": shadow_row.get("shadow_percent"),
                        "confidence": shadow_row.get("confidence"),
                        "root_cause": shadow_row.get("root_cause"),
                        "fusion_mode": shadow_row.get("fusion_mode"),
                        "efficiency_ratio": shadow_row.get("efficiency_ratio"),
                        "loss_kwh": shadow_row.get("loss_kwh"),
                        "theoretical_max_kwh": shadow_row.get("theoretical_max_kwh"),
                        "interpretation": shadow_row.get("interpretation"),
                        "methods": {
                            "theory_ratio": {
                                "shadow_type": shadow_row.get(
                                    "theory_ratio_shadow_type"
                                ),
                                "shadow_percent": shadow_row.get(
                                    "theory_ratio_shadow_percent"
                                ),
                                "confidence": shadow_row.get("theory_ratio_confidence"),
                                "efficiency_ratio": shadow_row.get(
                                    "theory_ratio_efficiency_ratio"
                                ),
                                "clear_sky_wm2": shadow_row.get(
                                    "theory_ratio_clear_sky_wm2"
                                ),
                            },
                            "sensor_fusion": {
                                "shadow_type": shadow_row.get(
                                    "sensor_fusion_shadow_type"
                                ),
                                "shadow_percent": shadow_row.get(
                                    "sensor_fusion_shadow_percent"
                                ),
                                "confidence": shadow_row.get(
                                    "sensor_fusion_confidence"
                                ),
                                "efficiency_ratio": shadow_row.get(
                                    "sensor_fusion_efficiency_ratio"
                                ),
                                "loss_kwh": shadow_row.get("sensor_fusion_loss_kwh"),
                                "mode": shadow_row.get("sensor_fusion_mode"),
                                "lux_analysis": {
                                    "shadow_percent": shadow_row.get(
                                        "sensor_fusion_lux_shadow_percent"
                                    )
                                },
                                "irradiance_factor": shadow_row.get(
                                    "sensor_fusion_irradiance_factor"
                                ),
                                "irradiance_shadow_percent": shadow_row.get(
                                    "sensor_fusion_irradiance_shadow_percent"
                                ),
                            },
                        },
                        "weights": {
                            "theory_ratio": shadow_row.get("weight_theory_ratio"),
                            "sensor_fusion": shadow_row.get("weight_sensor_fusion"),
                        },
                    }
                    continue
            return shadow_by_prediction
        except Exception as error:
            _LOGGER.debug("Failed to load shadow detection: %s", error)
            return {}

    async def save_daily_summary(self, summary: dict[str, Any]) -> bool:
        """Save a daily summary. @zara"""
        try:
            await self.db.execute(
                "INSERT INTO daily_summaries\n                   (date, predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                    production_hours, peak_power_w, peak_hour)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date) DO UPDATE SET\n                       actual_total_kwh = excluded.actual_total_kwh,\n                       accuracy_percent = excluded.accuracy_percent,\n                       production_hours = excluded.production_hours,\n                       peak_power_w = excluded.peak_power_w,\n                       peak_hour = excluded.peak_hour",
                (
                    summary.get("date"),
                    summary.get("predicted_total_kwh"),
                    summary.get("actual_total_kwh"),
                    summary.get("accuracy_percent"),
                    summary.get("production_hours"),
                    summary.get("peak_power_w"),
                    summary.get("peak_hour"),
                ),
            )
        except Exception as error:
            _LOGGER.error("Failed to save daily summary: %s", error)
            return False
        return True

    async def get_history(
        self,
        days: int = 30,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get historical daily summaries. @zara"""
        try:
            if start_date and end_date:
                query = "SELECT * FROM daily_summaries\n                         WHERE date BETWEEN ? AND ?\n                         ORDER BY date DESC"
                database_rows = await self.db.fetchall(query, (start_date, end_date))
            else:
                query = "SELECT * FROM daily_summaries\n                         WHERE date >= DATE('now', ?)\n                         ORDER BY date DESC"
                database_rows = await self.db.fetchall(query, (f"-{days} days",))
            return [
                self.data_adapter.row_to_dict(database_row)
                for database_row in database_rows
            ]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get history: %s", error)
            return []

    async def calculate_statistics(self) -> bool:
        """Calculate and update aggregated statistics. @zara"""
        try:
            today_day = dt_util.now().date()
            summary_statistics = await self.db.get_daily_summary_statistics(
                start_date=(today_day - timedelta(days=7)).isoformat(),
                end_date=today_day.isoformat(),
            )
            if summary_statistics["yield_days"]:
                _LOGGER.debug(
                    "Last 7 days: avg_yield=%.2f, avg_accuracy=%.1f%%, total=%.2f",
                    summary_statistics["avg_yield_kwh"] or 0,
                    summary_statistics["avg_accuracy"] or 0,
                    summary_statistics["total_yield_kwh"] or 0,
                )
        except Exception as error:
            _LOGGER.error("Failed to calculate statistics: %s", error)
            return False
        return True

    async def cleanup(self) -> None:
        """Cleanup all resources. @zara"""
        cleanup_error = None
        try:
            _LOGGER.info("DataManager cleanup started...")
            if self._db_manager is not None:
                from ..core.database_service import (
                    async_detach_database_manager as detach_database_manager,
                )

                await detach_database_manager(self.hass, self._db_manager)
            await super().cleanup()
            _LOGGER.info("DataManager cleanup completed")
        except Exception as error:
            cleanup_error = error
            _LOGGER.error("Error during DataManager cleanup: %s", error)
        if cleanup_error is not None:
            raise cleanup_error

    async def get_all_time_peak(self) -> Optional[float]:
        "Get the all-time peak power value in Watts. @zara\n\nReads from production_time_state for the all-time record.\n\nReturns:\n    All-time peak power in Watts or None if no data exists\n"
        try:
            database_row = await self.db.fetchone(
                "SELECT peak_record_w\n                   FROM production_time_state\n                   WHERE id = 1 AND peak_record_w IS NOT NULL"
            )
            if database_row and database_row[0] is not None:
                return float(database_row[0])
        except Exception as error:
            _LOGGER.error("Failed to get all-time peak: %s", error)
            return
        return None if True else None

    async def save_power_peak(
        self, power_w: float, timestamp: datetime, is_all_time: bool = False
    ) -> bool:
        "Save power peak to astronomy_hourly_peaks table. @zara\n\nArgs:\n    power_w: Power value in Watts\n    timestamp: Timestamp of the measurement\n    is_all_time: Whether this is a new all-time record\n\nReturns:\n    True if saved successfully\n"
        try:
            peak_hour = timestamp.hour
            forecast_date = timestamp.date().isoformat()
            peak_kw = power_w / 1000.0
            sun_elevation_deg = None
            cloud_cover_percent = None
            temperature_c = None
            solar_radiation_wm2 = None
            astronomy_row = await self.db.fetchone(
                "SELECT sun_elevation_deg FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = ?",
                (forecast_date, peak_hour),
            )
            if astronomy_row:
                sun_elevation_deg = astronomy_row[0]
            weather_row = await self.db.fetchone(
                "SELECT clouds, temperature, solar_radiation_wm2\n                   FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (forecast_date, peak_hour),
            )
            if weather_row:
                cloud_cover_percent = weather_row[0]
                temperature_c = weather_row[1]
                solar_radiation_wm2 = weather_row[2]
            await self.db.execute(
                "INSERT INTO astronomy_hourly_peaks\n                   (hour, kwh, date, sun_elevation_deg, cloud_cover_percent,\n                    temperature_c, solar_radiation_wm2)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(hour) DO UPDATE SET\n                       kwh = CASE WHEN excluded.kwh > kwh THEN excluded.kwh ELSE kwh END,\n                       date = CASE WHEN excluded.kwh > kwh THEN excluded.date ELSE date END,\n                       sun_elevation_deg = CASE WHEN excluded.kwh > kwh THEN excluded.sun_elevation_deg ELSE sun_elevation_deg END,\n                       cloud_cover_percent = CASE WHEN excluded.kwh > kwh THEN excluded.cloud_cover_percent ELSE cloud_cover_percent END,\n                       temperature_c = CASE WHEN excluded.kwh > kwh THEN excluded.temperature_c ELSE temperature_c END,\n                       solar_radiation_wm2 = CASE WHEN excluded.kwh > kwh THEN excluded.solar_radiation_wm2 ELSE solar_radiation_wm2 END",
                (
                    peak_hour,
                    peak_kw,
                    forecast_date,
                    sun_elevation_deg,
                    cloud_cover_percent,
                    temperature_c,
                    solar_radiation_wm2,
                ),
            )
            if is_all_time:
                _LOGGER.info(
                    "New all-time peak recorded: %.1f W at hour %d on %s",
                    power_w,
                    peak_hour,
                    forecast_date,
                )
            else:
                _LOGGER.debug("Power peak saved: %.1f W at hour %d", power_w, peak_hour)
            if True:
                return True
        except Exception as error:
            _LOGGER.error("Failed to save power peak: %s", error)
            return False

    async def save_forecast_best_hour(
        self, hour: int, prediction_kwh: float, source: str = "ML"
    ) -> bool:
        'Save the best production hour forecast to retrospective_forecast. @zara\n\nArgs:\n    hour: Best hour of day (0-23)\n    prediction_kwh: Predicted production for that hour in kWh\n    source: Source of the forecast (e.g., "ML-Hourly", "Profile", "Solar-Noon")\n\nReturns:\n    True if saved successfully\n'
        try:
            current_time = dt_util.now()
            await self.db.execute(
                "INSERT INTO retrospective_forecast\n                   (id, best_hour, best_hour_kwh, method, generated_at, last_updated)\n                   VALUES (1, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       best_hour = excluded.best_hour,\n                       best_hour_kwh = excluded.best_hour_kwh,\n                       method = excluded.method,\n                       generated_at = excluded.generated_at,\n                       last_updated = excluded.last_updated",
                (
                    hour,
                    prediction_kwh,
                    source,
                    current_time.isoformat(),
                    current_time.isoformat(),
                ),
            )
            _LOGGER.debug(
                "Best hour forecast saved: hour=%d, prediction=%.3f kWh, source=%s",
                hour,
                prediction_kwh,
                source,
            )
        except Exception as error:
            _LOGGER.error("Failed to save best hour forecast: %s", error)
            return False
        return True

    async def get_forecast_best_hour(self) -> Optional[dict[str, Any]]:
        "Get the current best hour forecast. @zara\n\nReturns:\n    Dict with best_hour and best_hour_kwh or None\n"
        try:
            database_row = await self.db.fetchone(
                "SELECT best_hour, best_hour_kwh, method, generated_at\n                   FROM retrospective_forecast WHERE id = 1"
            )
            if database_row is None:
                return
            return {
                "best_hour": database_row[0],
                "best_hour_kwh": database_row[1],
                "method": database_row[2],
                "generated_at": database_row[3],
            }
        except Exception as error:
            _LOGGER.error("Failed to get best hour forecast: %s", error)
            return

    async def save_multi_day_hourly_forecast(
        self, hourly_forecast: list[dict[str, Any]]
    ) -> bool:
        "Save multi-day hourly forecast to database. @zara\n\nArgs:\n    hourly_forecast: List of hourly forecast dictionaries containing\n                    hour, production_kwh, temperature, cloud_cover, etc.\n\nReturns:\n    True if saved successfully\n"
        try:
            current_time = dt_util.now()
            today_day = current_time.date()
            saved_entries = 0
            for forecast_entry in hourly_forecast:
                target_hour = forecast_entry.get("hour") or forecast_entry.get(
                    "local_hour"
                )
                if target_hour is None:
                    continue
                date_text = forecast_entry.get("date")
                if date_text:
                    from datetime import date as date_type

                    try:
                        forecast_day = date_type.fromisoformat(date_text)
                    except (ValueError, TypeError):
                        forecast_day = today_day
                else:
                    forecast_day = today_day
                day_offset = (forecast_day - today_day).days
                if day_offset == 0:
                    day_type = "today"
                elif day_offset == 1:
                    day_type = "tomorrow"
                elif day_offset == 2:
                    day_type = "day_after_tomorrow"
                else:
                    continue
                predicted_kwh = (
                    forecast_entry.get("production_kwh")
                    or forecast_entry.get("prediction_kwh")
                    or 0.0
                )
                cloud_cover_percent = (
                    forecast_entry.get("cloud_cover")
                    or forecast_entry.get("clouds")
                    or forecast_entry.get("cloud_cover_percent")
                )
                temperature_c = forecast_entry.get("temperature") or forecast_entry.get(
                    "temperature_c"
                )
                solar_radiation_wm2 = forecast_entry.get(
                    "solar_radiation_wm2"
                ) or forecast_entry.get("solar_radiation")
                weather_source = (
                    forecast_entry.get("source")
                    or forecast_entry.get("weather_source")
                    or "unknown"
                )
                await self.db.execute(
                    "INSERT INTO multi_day_hourly_forecast\n                       (forecast_date, day_type, hour, prediction_kwh, cloud_cover,\n                        temperature, solar_radiation_wm2, weather_source, updated_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                           day_type = excluded.day_type,\n                           prediction_kwh = excluded.prediction_kwh,\n                           cloud_cover = excluded.cloud_cover,\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           weather_source = excluded.weather_source,\n                           updated_at = excluded.updated_at",
                    (
                        forecast_day.isoformat(),
                        day_type,
                        target_hour,
                        predicted_kwh,
                        cloud_cover_percent,
                        temperature_c,
                        solar_radiation_wm2,
                        weather_source,
                        current_time.isoformat(),
                    ),
                )
                saved_entries += 1
                continue
            _LOGGER.debug("Multi-day hourly forecast saved: %d entries", saved_entries)
            return True
        except Exception as error:
            _LOGGER.error("Failed to save multi-day hourly forecast: %s", error)
            return False

    async def get_multi_day_hourly_forecast(
        self, day_type: Optional[str] = None
    ) -> list[dict[str, Any]]:
        "Get multi-day hourly forecast from database. @zara\n\nArgs:\n    day_type: Optional filter for 'today', 'tomorrow', or 'day_after_tomorrow'\n\nReturns:\n    List of hourly forecast dictionaries\n"
        try:
            if day_type:
                database_rows = await self.db.fetchall(
                    "SELECT forecast_date, day_type, hour, prediction_kwh,\n                              cloud_cover, temperature, solar_radiation_wm2,\n                              weather_source, updated_at\n                       FROM multi_day_hourly_forecast\n                       WHERE day_type = ?\n                       ORDER BY forecast_date, hour",
                    (day_type,),
                )
            else:
                database_rows = await self.db.fetchall(
                    "SELECT forecast_date, day_type, hour, prediction_kwh,\n                              cloud_cover, temperature, solar_radiation_wm2,\n                              weather_source, updated_at\n                       FROM multi_day_hourly_forecast\n                       ORDER BY forecast_date, hour"
                )
            return [
                {
                    "date": str(database_row[0]),
                    "day_type": database_row[1],
                    "hour": database_row[2],
                    "prediction_kwh": database_row[3],
                    "cloud_cover": database_row[4],
                    "temperature": database_row[5],
                    "solar_radiation_wm2": database_row[6],
                    "weather_source": database_row[7],
                    "updated_at": database_row[8],
                }
                for database_row in database_rows
            ]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get multi-day hourly forecast: %s", error)
            return []
