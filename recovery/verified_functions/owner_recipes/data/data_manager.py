# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class DataManager(DataManagerIO):

    async def initialize(self) -> bool:
        """Async initialization - creates database and connects. @zara

        Returns:
            True if initialization successful
        """
        try:
            _LOGGER.debug("DataManager: Running startup initializer...")
            _var_var_0 = StartupInitializer(self.data_dir, self._startup_config)
            _var_var_1 = await self.hass.async_add_executor_job(
                _var_var_0.initialize_all
            )
            if not _var_var_1:
                raise RuntimeError("SFML database startup initialization failed")
            _var_var_2 = _var_var_0.get_db_path()
            self._db_manager = DatabaseManager(_var_var_2)
            _var_var_3 = None
            try:
                from homeassistant.helpers.instance_id import async_get as _var_var_4

                _var_var_3 = await _var_var_4(self.hass)
            except Exception:
                _var_var_3 = None
            if isinstance(_var_var_3, str) and _var_var_3:
                self._db_manager.artifact_instance_id = _var_var_3
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
                async_attach_database_manager as _var_var_5,
            )

            await _var_var_5(self.hass, self._db_manager)
            self._initialized = True
            _LOGGER.debug("DataManager initialized successfully")
            return True
        except Exception as _var_var_6:
            if self._db_manager is not None:
                try:
                    await self._db_manager.close()
                except Exception:
                    _LOGGER.exception(
                        "Failed to close database after initialization error"
                    )
            _LOGGER.error(
                "Failed to initialize DataManager: %s", _var_var_6, exc_info=True
            )
        return False

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
        _var_var_7 = None
        try:
            _var_var_8 = dt_util.now()
            _var_var_9 = _var_var_8.date().isoformat()
            if not await self._has_hourly_forecast_coverage(_var_var_9):
                _LOGGER.info(
                    "Skipping today aggregate for %s: incomplete hourly horizon",
                    _var_var_9,
                )
                return False
            async with self.db.transaction():
                _var_var_10 = await self.db.fetchone(
                    "SELECT locked\n                       FROM daily_forecasts\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (_var_var_9,),
                )
                if _var_var_10 and _var_var_10[0]:
                    _LOGGER.debug(
                        "Skipping today forecast update - locked by morning routine"
                    )
                    return False
                await self.db.execute(
                    "INSERT INTO daily_forecasts\n                       (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                        safeguard_applied, locked, locked_at, source)\n                       VALUES ('today', ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                           prediction_kwh = excluded.prediction_kwh,\n                           prediction_kwh_raw = excluded.prediction_kwh_raw,\n                           safeguard_applied = excluded.safeguard_applied,\n                           locked = CASE WHEN excluded.locked THEN TRUE ELSE locked END,\n                           locked_at = CASE WHEN excluded.locked THEN excluded.locked_at ELSE locked_at END,\n                           source = excluded.source\n                       WHERE daily_forecasts.locked = 0 OR daily_forecasts.locked IS NULL",
                    (
                        _var_var_9,
                        prediction_kwh,
                        prediction_kwh_raw,
                        safeguard_applied,
                        lock,
                        _var_var_8.isoformat() if lock else None,
                        source,
                    ),
                    auto_commit=False,
                )
            _LOGGER.debug(
                "Saved forecast day: %.2f kWh (source: %s)", prediction_kwh, source
            )
            return True
        except Exception as _var_var_6:
            _LOGGER.error("Failed to save forecast day: %s", _var_var_6)
            return False

    async def _load_shadow_detection_for_date(
        self, target_date: str
    ) -> dict[str, dict[str, Any]]:
        """Load shadow detection data for all predictions of a date. @zara V16.1

        Returns:
            Dict mapping prediction_id to shadow_detection dict
        """
        try:
            _var_var_14 = await self.db.fetchall(
                "SELECT sd.* FROM hourly_shadow_detection sd\n                   JOIN hourly_predictions hp ON sd.prediction_id = hp.prediction_id\n                   WHERE hp.target_date = ?",
                (target_date,),
            )
            _var_var_28 = {}
            for _var_var_11 in _var_var_14:
                _var_var_34 = self.data_adapter.row_to_dict(_var_var_11)
                _var_var_30 = _var_var_34.get("prediction_id")
                if _var_var_30:
                    _var_var_28[_var_var_30] = {
                        "method": _var_var_34.get("method"),
                        "ensemble_mode": _var_var_34.get("ensemble_mode"),
                        "shadow_type": _var_var_34.get("shadow_type"),
                        "shadow_percent": _var_var_34.get("shadow_percent"),
                        "confidence": _var_var_34.get("confidence"),
                        "root_cause": _var_var_34.get("root_cause"),
                        "fusion_mode": _var_var_34.get("fusion_mode"),
                        "efficiency_ratio": _var_var_34.get("efficiency_ratio"),
                        "loss_kwh": _var_var_34.get("loss_kwh"),
                        "theoretical_max_kwh": _var_var_34.get("theoretical_max_kwh"),
                        "interpretation": _var_var_34.get("interpretation"),
                        "methods": {
                            "theory_ratio": {
                                "shadow_type": _var_var_34.get(
                                    "theory_ratio_shadow_type"
                                ),
                                "shadow_percent": _var_var_34.get(
                                    "theory_ratio_shadow_percent"
                                ),
                                "confidence": _var_var_34.get(
                                    "theory_ratio_confidence"
                                ),
                                "efficiency_ratio": _var_var_34.get(
                                    "theory_ratio_efficiency_ratio"
                                ),
                                "clear_sky_wm2": _var_var_34.get(
                                    "theory_ratio_clear_sky_wm2"
                                ),
                            },
                            "sensor_fusion": {
                                "shadow_type": _var_var_34.get(
                                    "sensor_fusion_shadow_type"
                                ),
                                "shadow_percent": _var_var_34.get(
                                    "sensor_fusion_shadow_percent"
                                ),
                                "confidence": _var_var_34.get(
                                    "sensor_fusion_confidence"
                                ),
                                "efficiency_ratio": _var_var_34.get(
                                    "sensor_fusion_efficiency_ratio"
                                ),
                                "loss_kwh": _var_var_34.get("sensor_fusion_loss_kwh"),
                                "mode": _var_var_34.get("sensor_fusion_mode"),
                                "lux_analysis": {
                                    "shadow_percent": _var_var_34.get(
                                        "sensor_fusion_lux_shadow_percent"
                                    )
                                },
                                "irradiance_factor": _var_var_34.get(
                                    "sensor_fusion_irradiance_factor"
                                ),
                                "irradiance_shadow_percent": _var_var_34.get(
                                    "sensor_fusion_irradiance_shadow_percent"
                                ),
                            },
                        },
                        "weights": {
                            "theory_ratio": _var_var_34.get("weight_theory_ratio"),
                            "sensor_fusion": _var_var_34.get("weight_sensor_fusion"),
                        },
                    }
                    continue
            return _var_var_28
        except Exception as _var_var_6:
            _LOGGER.debug("Failed to load shadow detection: %s", _var_var_6)
            return {}
