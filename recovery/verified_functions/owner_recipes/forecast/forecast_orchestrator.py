# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import copy
import hashlib
import inspect
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from homeassistant.core import HomeAssistant
from ..astronomy.astronomy_cache_manager import get_cache_manager
from ..const import WINTER_MONTHS
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager, canonical_stable_hash
from ..data.data_weather_radiation_ensemble import hourly_forecast_date_covered
from .forecast_rule_based_strategy import RuleBasedForecastStrategy
from .forecast_strategy_base import ForecastResult, ForecastGenerationError
from .forecast_tfs_client import TFSClient
from .forecast_weather_calculator import WeatherCalculator


class ForecastOrchestrator:

    def is_production_hour(self, target_dt: datetime) -> bool:
        """Check if a given datetime is within realistic solar production hours. @zara

        Args:
            target_dt: Target datetime to check

        Returns:
            True if within production hours
        """
        _var_var_9 = dt_util.as_local(target_dt)
        try:
            _var_var_10 = get_cache_manager()
            if _var_var_10.is_loaded():
                _var_var_11 = _var_var_9.date().isoformat()
                _var_var_12 = _var_var_10.get_production_window(_var_var_11)
                if _var_var_12:
                    _var_var_13, _var_var_14 = _var_var_12
                    _var_var_15 = datetime.fromisoformat(_var_var_13).replace(
                        tzinfo=None
                    )
                    _var_var_16 = datetime.fromisoformat(_var_var_14).replace(
                        tzinfo=None
                    )
                    _var_var_17 = _var_var_9.replace(tzinfo=None)
                    if _var_var_15 <= _var_var_17 <= _var_var_16:
                        if True:
                            return True
                    if True:
                        return False
        except Exception as _var_var_18:
            _LOGGER.debug(
                f"Astronomy cache access failed, using fallback: {_var_var_18}"
            )
        _var_var_19 = _var_var_9.hour
        _var_var_20 = _var_var_9.month
        if _var_var_20 in WINTER_MONTHS:
            _var_var_21 = 6 <= _var_var_19 <= 17
        elif _var_var_20 in (5, 6, 7, 8):
            _var_var_21 = 4 <= _var_var_19 <= 21
        else:
            _var_var_21 = 5 <= _var_var_19 <= 19
        if True:
            return _var_var_21

    async def _calculate_candidate_shadow_pair(
        self,
        *,
        candidate_id: str,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        lag_features: Dict[str, Any],
        tfs_hourly: Optional[Dict[str, Any]],
        forecast_cycle: Optional[str] = None,
        trigger_kind: Optional[str] = None,
    ) -> tuple[Optional[ForecastResult], Optional[Dict[str, Any]]]:
        if not forecast_cycle or not trigger_kind:
            _LOGGER.debug(
                "Skipping candidate shadow pair without complete cycle context"
            )
            return (None, None)
        _var_var_41 = self.rule_based_strategy
        _var_var_42 = self._ai_predictor
        if not _var_var_41 or not _var_var_42:
            return (None, None)
        _var_var_43 = None
        _var_var_44 = None
        try:
            _var_var_45, _var_var_46 = (
                await _var_var_42.create_shadow_candidate_predictor(candidate_id)
            )
            _var_var_47 = await self._db.get_active_model_artifact_hash()
            if not _var_var_47:
                raise ValueError("serving model has no verifiable artifact hash")
            _var_var_48 = copy.deepcopy(
                await _var_var_42._load_predictions_for_sequence()
            )
            _var_var_49 = copy.deepcopy(hourly_weather_forecast)
            _var_var_50 = copy.deepcopy(sensor_data)
            _var_var_51 = copy.deepcopy(lag_features)
            _var_var_52 = copy.deepcopy(tfs_hourly)
            _var_var_53 = dt_util.now()
            _var_var_54 = {}
            _var_var_55 = _var_var_41.ai_predictor
            _var_var_56 = _var_var_41._forecast_policy_read_cache
            _var_var_57 = _var_var_41._forecast_reference_time
            _var_var_58 = _var_var_42._frozen_prediction_history
            _var_var_59 = _var_var_45._frozen_prediction_history
            _var_var_60 = _var_var_42._forecast_reference_date
            _var_var_61 = _var_var_45._forecast_reference_date
            _var_var_62 = _var_var_41._physics_calibrator
            _var_var_63 = None
            _var_var_64 = {}
            if _var_var_62 is not None:
                _var_var_63 = copy.copy(_var_var_62)
                _var_var_63._factors = copy.deepcopy(_var_var_62._factors)
                _var_var_63._history = copy.deepcopy(_var_var_62._history)
                _var_var_64 = copy.deepcopy(_var_var_63._factors)
            _var_var_41._forecast_policy_read_cache = _var_var_54
            _var_var_41._forecast_reference_time = _var_var_53
            _var_var_41.ai_predictor = _var_var_42
            _var_var_41._physics_calibrator = _var_var_63
            if _var_var_41.panel_group_calculator is not None:
                _var_var_41.panel_group_calculator.set_calibrator(_var_var_63)
            _var_var_42._frozen_prediction_history = copy.deepcopy(_var_var_48)
            _var_var_45._frozen_prediction_history = copy.deepcopy(_var_var_48)
            _var_var_42._forecast_reference_date = _var_var_53.date().isoformat()
            _var_var_45._forecast_reference_date = _var_var_53.date().isoformat()
            try:
                _var_var_65 = await _var_var_41._get_snow_covered_status()
                _var_var_66 = await _var_var_41._load_ensemble_group_weights()
                _var_var_43 = await _var_var_41.calculate_forecast(
                    hourly_weather_forecast=copy.deepcopy(_var_var_49),
                    sensor_data=copy.deepcopy(_var_var_50),
                    correction_factor=correction_factor,
                    lag_features=copy.deepcopy(_var_var_51),
                    tfs_hourly=copy.deepcopy(_var_var_52),
                    frozen_snow_status=copy.deepcopy(_var_var_65),
                    frozen_ensemble_group_weights=copy.deepcopy(_var_var_66),
                    forecast_reference_time=_var_var_53,
                    persist=False,
                    forecast_cycle=forecast_cycle,
                )
                _var_var_44 = _capture_shadow_strategy_state(_var_var_41)
                _var_var_41.ai_predictor = _var_var_45
                _var_var_67 = await _var_var_41.calculate_forecast(
                    hourly_weather_forecast=copy.deepcopy(_var_var_49),
                    sensor_data=copy.deepcopy(_var_var_50),
                    correction_factor=correction_factor,
                    lag_features=copy.deepcopy(_var_var_51),
                    tfs_hourly=copy.deepcopy(_var_var_52),
                    frozen_snow_status=copy.deepcopy(_var_var_65),
                    frozen_ensemble_group_weights=copy.deepcopy(_var_var_66),
                    forecast_reference_time=_var_var_53,
                    persist=False,
                )
            finally:
                _var_var_41.ai_predictor = _var_var_55
                _var_var_41._forecast_policy_read_cache = _var_var_56
                _var_var_41._forecast_reference_time = _var_var_57
                _var_var_41._physics_calibrator = _var_var_62
                if _var_var_41.panel_group_calculator is not None:
                    _var_var_41.panel_group_calculator.set_calibrator(_var_var_62)
                if _var_var_44 is not None:
                    _restore_shadow_strategy_state(_var_var_41, _var_var_44)
                _var_var_42._frozen_prediction_history = _var_var_58
                _var_var_45._frozen_prediction_history = _var_var_59
                _var_var_42._forecast_reference_date = _var_var_60
                _var_var_45._forecast_reference_date = _var_var_61
            _var_var_68 = await self.hass.async_add_executor_job(
                _runtime_file_hashes,
                {
                    "orchestrator": __file__,
                    "strategy": inspect.getfile(type(_var_var_41)),
                    "predictor": inspect.getfile(type(_var_var_42)),
                },
            )
            _var_var_69 = self._build_candidate_shadow_packet(
                candidate_id=candidate_id,
                champion_hash=_var_var_47,
                candidate_hash=_var_var_46,
                reference_time=_var_var_53,
                frozen_weather=_var_var_49,
                frozen_sensors=_var_var_50,
                frozen_lag=_var_var_51,
                frozen_tfs=_var_var_52,
                frozen_history=_var_var_48,
                frozen_snow_status=_var_var_65,
                frozen_ensemble_weights=_var_var_66,
                frozen_physics_calibration=_var_var_64,
                policy_cache=_var_var_54,
                runtime_code_hashes=_var_var_68,
                correction_factor=correction_factor,
                forecast_cycle=forecast_cycle,
                trigger_kind=trigger_kind,
                champion_result=_var_var_43,
                candidate_result=_var_var_67,
            )
            return (_var_var_43, _var_var_69)
        except Exception as _var_var_70:
            _LOGGER.warning(
                "Candidate shadow unavailable for %s: %s", candidate_id, _var_var_70
            )
            return (_var_var_43, None)
