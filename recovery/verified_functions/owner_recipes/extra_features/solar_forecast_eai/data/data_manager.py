# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..ai.ai_types import HourlyProfile, LearnedWeights
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_adapter import TypedDataAdapter
from .data_backup_handler import DataBackupHandler
from .data_persistence import DataPersistence
from .data_state_handler import DataStateHandler
from .data_startup_initializer import StartupInitializer


class DataManager(DataManagerIO):

    async def _load_shadow_detection_for_date(
        self, target_date: str
    ) -> dict[str, dict[str, Any]]:
        """Load shadow detection data for all predictions of a date. @zara V16.1

        Returns:
            Dict mapping prediction_id to shadow_detection dict
        """
        try:
            _var_var_6 = await self.db.fetchall(
                "SELECT sd.* FROM hourly_shadow_detection sd\n                   JOIN hourly_predictions hp ON sd.prediction_id = hp.prediction_id\n                   WHERE hp.target_date = ?",
                (target_date,),
            )
            _var_var_21 = {}
            for _var_var_8 in _var_var_6:
                _var_var_27 = self.data_adapter.row_to_dict(_var_var_8)
                _var_var_23 = _var_var_27.get("prediction_id")
                if _var_var_23:
                    _var_var_21[_var_var_23] = {
                        "method": _var_var_27.get("method"),
                        "ensemble_mode": _var_var_27.get("ensemble_mode"),
                        "shadow_type": _var_var_27.get("shadow_type"),
                        "shadow_percent": _var_var_27.get("shadow_percent"),
                        "confidence": _var_var_27.get("confidence"),
                        "root_cause": _var_var_27.get("root_cause"),
                        "fusion_mode": _var_var_27.get("fusion_mode"),
                        "efficiency_ratio": _var_var_27.get("efficiency_ratio"),
                        "loss_kwh": _var_var_27.get("loss_kwh"),
                        "theoretical_max_kwh": _var_var_27.get("theoretical_max_kwh"),
                        "interpretation": _var_var_27.get("interpretation"),
                        "methods": {
                            "theory_ratio": {
                                "shadow_type": _var_var_27.get(
                                    "theory_ratio_shadow_type"
                                ),
                                "shadow_percent": _var_var_27.get(
                                    "theory_ratio_shadow_percent"
                                ),
                                "confidence": _var_var_27.get(
                                    "theory_ratio_confidence"
                                ),
                                "efficiency_ratio": _var_var_27.get(
                                    "theory_ratio_efficiency_ratio"
                                ),
                                "clear_sky_wm2": _var_var_27.get(
                                    "theory_ratio_clear_sky_wm2"
                                ),
                            },
                            "sensor_fusion": {
                                "shadow_type": _var_var_27.get(
                                    "sensor_fusion_shadow_type"
                                ),
                                "shadow_percent": _var_var_27.get(
                                    "sensor_fusion_shadow_percent"
                                ),
                                "confidence": _var_var_27.get(
                                    "sensor_fusion_confidence"
                                ),
                                "efficiency_ratio": _var_var_27.get(
                                    "sensor_fusion_efficiency_ratio"
                                ),
                                "loss_kwh": _var_var_27.get("sensor_fusion_loss_kwh"),
                                "mode": _var_var_27.get("sensor_fusion_mode"),
                                "lux_analysis": {
                                    "shadow_percent": _var_var_27.get(
                                        "sensor_fusion_lux_shadow_percent"
                                    )
                                },
                                "irradiance_factor": _var_var_27.get(
                                    "sensor_fusion_irradiance_factor"
                                ),
                                "irradiance_shadow_percent": _var_var_27.get(
                                    "sensor_fusion_irradiance_shadow_percent"
                                ),
                            },
                        },
                        "weights": {
                            "theory_ratio": _var_var_27.get("weight_theory_ratio"),
                            "sensor_fusion": _var_var_27.get("weight_sensor_fusion"),
                        },
                    }
            return _var_var_21
        except Exception as _var_var_2:
            _LOGGER.debug("Failed to load shadow detection: %s", _var_var_2)
            return {}
