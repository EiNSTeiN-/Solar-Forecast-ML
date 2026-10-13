# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Forecast Orchestrator Module.
Selects and executes the most appropriate forecast strategy.
@zara
"""
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

_LOGGER = logging.getLogger(__name__)
MODEL_CANDIDATE_SHADOW_CONTRACT_VERSION = "model_candidate_shadow_v2"
_SHADOW_STRATEGY_STATE_FIELDS = (
    "_forecast_debug_stats",
    "_use_ai",
    "_last_component_mode",
    "_similar_weather_history_cache",
    "_daily_bias_history_cache",
    "_physics_init_attempted",
    "_panel_groups_initialized",
)


def _normalize_shadow_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite shadow context value")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized_values = {}
        for key, item_value in value.items():
            if isinstance(key, str):
                normalized_key = key
            else:
                normalized_key = repr(_normalize_shadow_value(key))
            normalized_values[normalized_key] = _normalize_shadow_value(item_value)
        return {key: normalized_values[key] for key in sorted(normalized_values)}
    if isinstance(value, (list, tuple)):
        return [_normalize_shadow_value(item_value) for item_value in value]
    if isinstance(value, set):
        return sorted((_normalize_shadow_value(item_value) for item_value in value))
    if hasattr(value, "as_dict"):
        return _normalize_shadow_value(value.as_dict())
    if hasattr(value, "keys"):
        return _normalize_shadow_value({str(key): value[key] for key in value.keys()})
    if hasattr(value, "__dict__"):
        return _normalize_shadow_value(
            {
                key: item_value
                for key, item_value in vars(value).items()
                if not key.startswith("_")
            }
        )
    raise ValueError(f"unsupported shadow context value: {type(value).__name__}")


def _runtime_file_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _runtime_file_hashes(paths: Mapping[str, str]) -> Dict[str, str]:
    return {
        source_name: _runtime_file_hash(source_path)
        for source_name, source_path in paths.items()
    }


def _capture_shadow_strategy_state(strategy: Any) -> Dict[str, Any]:
    return {
        field_name: copy.deepcopy(getattr(strategy, field_name))
        for field_name in _SHADOW_STRATEGY_STATE_FIELDS
    }


def _restore_shadow_strategy_state(strategy: Any, state: Mapping[str, Any]) -> None:
    for field_name, field_value in state.items():
        setattr(strategy, field_name, copy.deepcopy(field_value))


def _forecast_run_label(
    forecast_cycle: Optional[str], trigger_kind: Optional[str]
) -> str:
    """Name a forecast run by what it is for, so log readers can tell them apart.

    Only the morning cycles (post_midnight, pre_sunrise) and catch-ups write
    the day's locked forecast; everything else is a coordinator refresh that
    updates sensors from the same weather data.
    """
    if not forecast_cycle:
        return "Forecast refresh (coordinator update)"
    if forecast_cycle == "catchup":
        return "Catch-up morning forecast"
    if forecast_cycle == "manual":
        return "Manual forecast"
    trigger_suffix = (
        f", {trigger_kind}" if trigger_kind and trigger_kind != "scheduled" else ""
    )
    return f"Morning forecast ({forecast_cycle} cycle{trigger_suffix})"


class ForecastOrchestrator:
    """Selects and executes the most appropriate forecast strategy. @zara"""

    FALLBACK_PRODUCTION_START_HOUR = 6
    FALLBACK_PRODUCTION_END_HOUR = 21

    def __init__(
        self,
        hass: HomeAssistant,
        data_manager: Any,
        solar_capacity: float,
        weather_calculator: WeatherCalculator,
        db_manager: DatabaseManager,
        panel_groups: Optional[List[Dict[str, Any]]] = None,
        inverter_max_power: float = 0.0,
    ):
        "Initialize the ForecastOrchestrator. @zara\n\nArgs:\n    hass: Home Assistant instance\n    data_manager: Data manager reference\n    solar_capacity: System capacity in kWp\n    weather_calculator: Weather calculation utilities\n    db_manager: Database manager instance\n    panel_groups: Panel group configurations\n    inverter_max_power: Optional AC inverter limit in kW\n"
        self.hass = hass
        self.data_manager = data_manager
        self.solar_capacity = solar_capacity
        self.weather_calculator = weather_calculator
        self._db = db_manager
        self.panel_groups = panel_groups or []
        self.inverter_max_power = max(0.0, float(inverter_max_power or 0.0))
        self.rule_based_strategy = None
        self._tfs_client = TFSClient(hass)
        self._ai_predictor = None
        self._historical_cache = {}
        self.active_strategy_name = None
        self._forecast_execution_lock = asyncio.Lock()
        if self.panel_groups:
            _LOGGER.debug(
                f"ForecastOrchestrator initialized with {len(self.panel_groups)} panel groups."
            )
        else:
            _LOGGER.debug("ForecastOrchestrator initialized.")
        return None if True else None

    def is_production_hour(self, target_dt: datetime) -> bool:
        """Check if a given datetime is within realistic solar production hours. @zara

        Args:
            target_dt: Target datetime to check

        Returns:
            True if within production hours
        """
        local_target = dt_util.as_local(target_dt)
        try:
            cache_manager = get_cache_manager()
            if cache_manager.is_loaded():
                target_date_text = local_target.date().isoformat()
                production_window = cache_manager.get_production_window(
                    target_date_text
                )
                if production_window:
                    window_start_text, window_end_text = production_window
                    window_start = datetime.fromisoformat(window_start_text).replace(
                        tzinfo=None
                    )
                    window_end = datetime.fromisoformat(window_end_text).replace(
                        tzinfo=None
                    )
                    naive_target = local_target.replace(tzinfo=None)
                    if window_start <= naive_target <= window_end:
                        if True:
                            return True
                    if True:
                        return False
        except Exception as error:
            _LOGGER.debug(f"Astronomy cache access failed, using fallback: {error}")
        hour = local_target.hour
        month = local_target.month
        if month in WINTER_MONTHS:
            within_production_window = 6 <= hour <= 17
        elif month in (5, 6, 7, 8):
            within_production_window = 4 <= hour <= 21
        else:
            within_production_window = 5 <= hour <= 19
        if True:
            return within_production_window

    def initialize_strategies(
        self, ai_predictor: Optional[Any] = None, error_handler: Optional[Any] = None
    ) -> None:
        "Initialize the forecast strategy with AI. @zara\n\nArgs:\n    ai_predictor: AI predictor instance\n    error_handler: Error handling service\n"
        _LOGGER.debug("Initializing forecast strategies...")
        self._ai_predictor = ai_predictor
        if self._ai_predictor:
            self._historical_cache = self._ai_predictor._historical_cache
        try:
            self.rule_based_strategy = RuleBasedForecastStrategy(
                weather_calculator=self.weather_calculator,
                solar_capacity=self.solar_capacity,
                db_manager=self._db,
                orchestrator=self,
                panel_groups=self.panel_groups,
                ai_predictor=ai_predictor,
                inverter_max_power=self.inverter_max_power,
            )
            if ai_predictor and ai_predictor.is_ready():
                _LOGGER.debug("Forecast strategy initialized with local AI")
            elif self.panel_groups:
                _LOGGER.info(
                    f"Forecast strategy initialized with {len(self.panel_groups)} panel groups (physics)"
                )
            else:
                _LOGGER.info("Forecast strategy initialized (physics fallback)")
        except Exception as error:
            _LOGGER.error(
                f"Failed to initialize forecast strategy: {error}", exc_info=True
            )
            self.rule_based_strategy = None
        return None if True else None

    async def orchestrate_forecast(
        self,
        current_weather: Optional[Dict[str, Any]] = None,
        hourly_forecast: Optional[List[Dict[str, Any]]] = None,
        external_sensors: Optional[Dict[str, Any]] = None,
        historical_avg: Optional[float] = None,
        ml_prediction_today: Optional[float] = None,
        ml_prediction_tomorrow: Optional[float] = None,
        correction_factor: float = 1.0,
        persist: bool = True,
        candidate_shadow_id: Optional[str] = None,
        forecast_cycle: Optional[str] = None,
        trigger_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        "Orchestrate forecast creation with all available data. @zara\n\nArgs:\n    current_weather: Current weather data\n    hourly_forecast: Hourly weather forecast\n    external_sensors: External sensor data\n    historical_avg: Historical average production\n    ml_prediction_today: ML prediction for today\n    ml_prediction_tomorrow: ML prediction for tomorrow\n    correction_factor: Correction factor to apply\n\nReturns:\n    Forecast result dictionary\n"
        weather_forecast = hourly_forecast if hourly_forecast else []
        sensors = external_sensors if external_sensors else {}
        if "current_yield" not in sensors and current_weather:
            sensors["current_yield"] = 0.0
        async with self._forecast_execution_lock:
            if self._ai_predictor and forecast_cycle and trigger_kind:
                candidate_shadow_id = (
                    await self._ai_predictor.reconcile_pending_model_candidates()
                )
            if not candidate_shadow_id or not forecast_cycle or (not trigger_kind):
                candidate_shadow_id = None
            return await self.create_forecast(
                hourly_weather_forecast=weather_forecast,
                sensor_data=sensors,
                correction_factor=correction_factor,
                persist=persist,
                candidate_shadow_id=candidate_shadow_id,
                forecast_cycle=forecast_cycle,
                trigger_kind=trigger_kind,
            )
        return None if True else None

    async def create_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float = 1.0,
        persist: bool = True,
        candidate_shadow_id: Optional[str] = None,
        forecast_cycle: Optional[str] = None,
        trigger_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        "Create daily solar forecast using AI with physics fallback. @zara\n\nArgs:\n    hourly_weather_forecast: Hourly weather forecast data\n    sensor_data: Current sensor readings\n    correction_factor: Correction factor to apply\n\nReturns:\n    Forecast result dictionary\n"
        run_label = _forecast_run_label(forecast_cycle, trigger_kind)
        _LOGGER.debug("%s: creating forecast (AI + Physics)...", run_label)
        if not hourly_weather_forecast:
            raise ForecastGenerationError("no hourly weather forecast")
        forecast_result = None
        lag_features = {}
        try:
            now = dt_util.now()
            yesterday = now - timedelta(days=1)
            yesterday_date_text = yesterday.date().isoformat()
            row = await self._db.fetchone(
                "SELECT SUM(actual_kwh)\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                     AND actual_kwh IS NOT NULL\n                     AND COALESCE(exclude_from_learning, 0) = 0\n                     AND COALESCE(exclude_from_clean_evaluation, 0) = 0\n                     AND COALESCE(mppt_throttled, 0) = 0\n                     AND COALESCE(inverter_clipped, 0) = 0",
                (yesterday_date_text,),
            )
            production_yesterday_kwh = row[0] if row and row[0] else 0.0
            lag_features["production_yesterday"] = float(production_yesterday_kwh)
            _LOGGER.debug(
                f"Lag-Feature 'production_yesterday' = {production_yesterday_kwh:.2f} kWh (from DB: {yesterday_date_text})"
            )
        except Exception as error:
            _LOGGER.warning(
                f"Could not retrieve lag feature 'production_yesterday': {error}"
            )
            lag_features["production_yesterday"] = 0.0
        tfs_hourly = None
        try:
            if await self._tfs_client.is_available():
                tfs_forecast = await self._tfs_client.fetch_forecast()
                if tfs_forecast:
                    tfs_hourly = self._tfs_client.parse_hourly(tfs_forecast)
                    (_LOGGER.info if forecast_cycle else _LOGGER.debug)(
                        "%s: TFS forecast %.2f kWh (score %.2f)",
                        run_label,
                        tfs_forecast.get("total_kwh_p50", 0),
                        tfs_forecast.get("validation_score", 0),
                    )
        except Exception:
            _LOGGER.debug("TFS not available")
        candidate_shadow = None
        if self.rule_based_strategy and self.rule_based_strategy.is_available():
            try:
                shadow_pair_used = False
                if (
                    candidate_shadow_id
                    and self._ai_predictor
                    and forecast_cycle
                    and trigger_kind
                ):
                    forecast_result, candidate_shadow = (
                        await self._calculate_candidate_shadow_pair(
                            candidate_id=candidate_shadow_id,
                            hourly_weather_forecast=hourly_weather_forecast,
                            sensor_data=sensor_data,
                            correction_factor=correction_factor,
                            lag_features=lag_features,
                            tfs_hourly=tfs_hourly,
                            forecast_cycle=forecast_cycle,
                            trigger_kind=trigger_kind,
                        )
                    )
                    shadow_pair_used = forecast_result is not None
                if forecast_result is None:
                    forecast_result = await self.rule_based_strategy.calculate_forecast(
                        hourly_weather_forecast=hourly_weather_forecast,
                        sensor_data=sensor_data,
                        correction_factor=correction_factor,
                        lag_features=lag_features,
                        tfs_hourly=tfs_hourly,
                        persist=persist,
                        forecast_cycle=forecast_cycle,
                    )
                elif persist and shadow_pair_used:
                    await self.rule_based_strategy._store_hourly_predictions(
                        forecast_result.hourly_values or []
                    )
                _LOGGER.info(
                    f"{run_label} complete: today={forecast_result.forecast_today:.2f} kWh, tomorrow={forecast_result.forecast_tomorrow:.2f} kWh, day_after={forecast_result.forecast_day_after_tomorrow:.2f} kWh, method={forecast_result.method}"
                )
                if persist:
                    await self._store_daily_forecasts(forecast_result)
                else:
                    _LOGGER.debug("Forecast persistence disabled for compute-only run")
            except ForecastGenerationError:
                raise
            except Exception as forecast_error:
                _LOGGER.error(
                    f"Forecast calculation failed: {forecast_error}", exc_info=True
                )
                raise ForecastGenerationError(
                    f"Forecast calculation failed: {forecast_error}"
                ) from forecast_error
        else:
            _LOGGER.error("Forecast strategy not available")
            raise ForecastGenerationError("Forecast strategy not available")
        if not forecast_result or not forecast_result.hourly_values:
            raise ForecastGenerationError("forecast produced no hourly values")
        best_hour = forecast_result.best_hour_today
        best_hour_kwh = forecast_result.best_hour_production_kwh
        hourly_values = forecast_result.hourly_values or []
        response = {
            "today": round(forecast_result.forecast_today, 2),
            "tomorrow": round(forecast_result.forecast_tomorrow, 2),
            "day_after_tomorrow": round(forecast_result.forecast_day_after_tomorrow, 2),
            "peak_time": "12:00",
            "confidence": round(forecast_result.confidence_today, 1),
            "method": forecast_result.method,
            "model_accuracy": forecast_result.model_accuracy,
            "best_hour": best_hour,
            "best_hour_kwh": (
                round(best_hour_kwh, 3) if best_hour_kwh is not None else None
            ),
            "hourly": hourly_values,
            "today_raw": (
                round(forecast_result.forecast_today_raw, 2)
                if forecast_result.forecast_today_raw is not None
                else None
            ),
            "safeguard_applied": forecast_result.safeguard_applied_today,
        }
        if candidate_shadow is not None:
            response["_candidate_shadow"] = candidate_shadow
        return response

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
        strategy = self.rule_based_strategy
        champion_predictor = self._ai_predictor
        if not strategy or not champion_predictor:
            return (None, None)
        champion_forecast = None
        champion_strategy_state = None
        try:
            candidate_predictor, candidate_artifact_hash = (
                await champion_predictor.create_shadow_candidate_predictor(candidate_id)
            )
            champion_artifact_hash = await self._db.get_active_model_artifact_hash()
            if not champion_artifact_hash:
                raise ValueError("serving model has no verifiable artifact hash")
            frozen_prediction_history = copy.deepcopy(
                await champion_predictor._load_predictions_for_sequence()
            )
            frozen_weather = copy.deepcopy(hourly_weather_forecast)
            frozen_sensors = copy.deepcopy(sensor_data)
            frozen_lag_features = copy.deepcopy(lag_features)
            frozen_tfs = copy.deepcopy(tfs_hourly)
            reference_time = dt_util.now()
            policy_read_cache = {}
            original_predictor = strategy.ai_predictor
            original_policy_cache = strategy._forecast_policy_read_cache
            original_reference_time = strategy._forecast_reference_time
            original_champion_history = champion_predictor._frozen_prediction_history
            original_candidate_history = candidate_predictor._frozen_prediction_history
            original_champion_reference_date = (
                champion_predictor._forecast_reference_date
            )
            original_candidate_reference_date = (
                candidate_predictor._forecast_reference_date
            )
            original_calibrator = strategy._physics_calibrator
            shadow_calibrator = None
            frozen_physics_factors = {}
            if original_calibrator is not None:
                shadow_calibrator = copy.copy(original_calibrator)
                shadow_calibrator._factors = copy.deepcopy(original_calibrator._factors)
                shadow_calibrator._history = copy.deepcopy(original_calibrator._history)
                frozen_physics_factors = copy.deepcopy(shadow_calibrator._factors)
            strategy._forecast_policy_read_cache = policy_read_cache
            strategy._forecast_reference_time = reference_time
            strategy.ai_predictor = champion_predictor
            strategy._physics_calibrator = shadow_calibrator
            if strategy.panel_group_calculator is not None:
                strategy.panel_group_calculator.set_calibrator(shadow_calibrator)
            champion_predictor._frozen_prediction_history = copy.deepcopy(
                frozen_prediction_history
            )
            candidate_predictor._frozen_prediction_history = copy.deepcopy(
                frozen_prediction_history
            )
            champion_predictor._forecast_reference_date = (
                reference_time.date().isoformat()
            )
            candidate_predictor._forecast_reference_date = (
                reference_time.date().isoformat()
            )
            try:
                frozen_snow_status = await strategy._get_snow_covered_status()
                frozen_ensemble_weights = await strategy._load_ensemble_group_weights()
                champion_forecast = await strategy.calculate_forecast(
                    hourly_weather_forecast=copy.deepcopy(frozen_weather),
                    sensor_data=copy.deepcopy(frozen_sensors),
                    correction_factor=correction_factor,
                    lag_features=copy.deepcopy(frozen_lag_features),
                    tfs_hourly=copy.deepcopy(frozen_tfs),
                    frozen_snow_status=copy.deepcopy(frozen_snow_status),
                    frozen_ensemble_group_weights=copy.deepcopy(
                        frozen_ensemble_weights
                    ),
                    forecast_reference_time=reference_time,
                    persist=False,
                    forecast_cycle=forecast_cycle,
                )
                champion_strategy_state = _capture_shadow_strategy_state(strategy)
                strategy.ai_predictor = candidate_predictor
                candidate_forecast = await strategy.calculate_forecast(
                    hourly_weather_forecast=copy.deepcopy(frozen_weather),
                    sensor_data=copy.deepcopy(frozen_sensors),
                    correction_factor=correction_factor,
                    lag_features=copy.deepcopy(frozen_lag_features),
                    tfs_hourly=copy.deepcopy(frozen_tfs),
                    frozen_snow_status=copy.deepcopy(frozen_snow_status),
                    frozen_ensemble_group_weights=copy.deepcopy(
                        frozen_ensemble_weights
                    ),
                    forecast_reference_time=reference_time,
                    persist=False,
                )
            finally:
                strategy.ai_predictor = original_predictor
                strategy._forecast_policy_read_cache = original_policy_cache
                strategy._forecast_reference_time = original_reference_time
                strategy._physics_calibrator = original_calibrator
                if strategy.panel_group_calculator is not None:
                    strategy.panel_group_calculator.set_calibrator(original_calibrator)
                if champion_strategy_state is not None:
                    _restore_shadow_strategy_state(strategy, champion_strategy_state)
                champion_predictor._frozen_prediction_history = (
                    original_champion_history
                )
                candidate_predictor._frozen_prediction_history = (
                    original_candidate_history
                )
                champion_predictor._forecast_reference_date = (
                    original_champion_reference_date
                )
                candidate_predictor._forecast_reference_date = (
                    original_candidate_reference_date
                )
            runtime_code_hashes = await self.hass.async_add_executor_job(
                _runtime_file_hashes,
                {
                    "orchestrator": __file__,
                    "strategy": inspect.getfile(type(strategy)),
                    "predictor": inspect.getfile(type(champion_predictor)),
                },
            )
            shadow_packet = self._build_candidate_shadow_packet(
                candidate_id=candidate_id,
                champion_hash=champion_artifact_hash,
                candidate_hash=candidate_artifact_hash,
                reference_time=reference_time,
                frozen_weather=frozen_weather,
                frozen_sensors=frozen_sensors,
                frozen_lag=frozen_lag_features,
                frozen_tfs=frozen_tfs,
                frozen_history=frozen_prediction_history,
                frozen_snow_status=frozen_snow_status,
                frozen_ensemble_weights=frozen_ensemble_weights,
                frozen_physics_calibration=frozen_physics_factors,
                policy_cache=policy_read_cache,
                runtime_code_hashes=runtime_code_hashes,
                correction_factor=correction_factor,
                forecast_cycle=forecast_cycle,
                trigger_kind=trigger_kind,
                champion_result=champion_forecast,
                candidate_result=candidate_forecast,
            )
            return (champion_forecast, shadow_packet)
        except Exception as shadow_error:
            _LOGGER.warning(
                "Candidate shadow unavailable for %s: %s", candidate_id, shadow_error
            )
            return (champion_forecast, None)

    def _build_candidate_shadow_packet(
        self,
        *,
        candidate_id: str,
        champion_hash: str,
        candidate_hash: str,
        reference_time: datetime,
        frozen_weather: List[Dict[str, Any]],
        frozen_sensors: Dict[str, Any],
        frozen_lag: Dict[str, Any],
        frozen_tfs: Optional[Dict[str, Any]],
        frozen_history: List[Dict[str, Any]],
        frozen_snow_status: Any,
        frozen_ensemble_weights: Dict[str, Any],
        frozen_physics_calibration: Dict[str, Any],
        policy_cache: Dict[Any, Any],
        runtime_code_hashes: Dict[str, str],
        correction_factor: float,
        forecast_cycle: Optional[str],
        trigger_kind: Optional[str],
        champion_result: ForecastResult,
        candidate_result: ForecastResult,
    ) -> Dict[str, Any]:
        strategy = self.rule_based_strategy
        if strategy is None:
            raise ValueError("forecast strategy is unavailable")
        if not forecast_cycle or not trigger_kind:
            raise ValueError("candidate shadow context is required")
        input_snapshot = _normalize_shadow_value(
            {
                "contract_version": MODEL_CANDIDATE_SHADOW_CONTRACT_VERSION,
                "forecast_anchor": reference_time,
                "weather": frozen_weather,
                "sensors": frozen_sensors,
                "lag_features": frozen_lag,
                "tfs_hourly": frozen_tfs,
                "prediction_history": frozen_history,
                "snow_status": frozen_snow_status,
            }
        )
        input_snapshot_hash = canonical_stable_hash(input_snapshot)
        policy_snapshot = _normalize_shadow_value(
            {
                "contract_version": MODEL_CANDIDATE_SHADOW_CONTRACT_VERSION,
                "forecast_reference_time": reference_time,
                "solar_capacity": self.solar_capacity,
                "inverter_max_power": self.inverter_max_power,
                "panel_groups": self.panel_groups,
                "correction_factor": correction_factor,
                "ensemble_group_weights": frozen_ensemble_weights,
                "physics_calibration": frozen_physics_calibration,
                "policy_reads": policy_cache,
                "similar_weather_history": strategy._similar_weather_history_cache,
                "daily_bias_history": strategy._daily_bias_history_cache,
                "runtime_code_hashes": runtime_code_hashes,
                "champion_model_state": getattr(
                    getattr(self._ai_predictor, "state", None),
                    "value",
                    str(getattr(self._ai_predictor, "state", "unknown")),
                ),
                "champion_active_model": getattr(
                    self._ai_predictor, "active_model", "none"
                ),
            }
        )
        policy_snapshot_hash = canonical_stable_hash(policy_snapshot)
        champion_hours = {
            (str(hourly_value["date"]), int(hourly_value["hour"])): hourly_value
            for hourly_value in champion_result.hourly_values or []
        }
        candidate_hours = {
            (str(hourly_value["date"]), int(hourly_value["hour"])): hourly_value
            for hourly_value in candidate_result.hourly_values or []
        }
        if not champion_hours or set(champion_hours) != set(candidate_hours):
            raise ValueError("candidate and champion hourly outputs are unpaired")
        anchor_date = reference_time.date()
        paired_hours = []
        for hour_key in sorted(champion_hours):
            target_date_text, target_hour = hour_key
            champion_hour = _normalize_shadow_value(champion_hours[hour_key])
            candidate_hour = _normalize_shadow_value(candidate_hours[hour_key])
            target_date = date.fromisoformat(target_date_text)
            horizon_days = (target_date - anchor_date).days
            if horizon_days < 0:
                raise ValueError("candidate shadow target precedes forecast anchor")
            input_fingerprint = canonical_stable_hash(
                {
                    "input_snapshot_hash": input_snapshot_hash,
                    "target_date": target_date_text,
                    "target_hour": target_hour,
                }
            )
            paired_hours.append(
                {
                    "source_prediction_id": f"{target_date_text}_{target_hour:02d}",
                    "target_date": target_date_text,
                    "target_hour": target_hour,
                    "horizon": f"day_{horizon_days}",
                    "weather_regime": champion_hour.get("weather_regime"),
                    "input_fingerprint": input_fingerprint,
                    "policy_fingerprint": policy_snapshot_hash,
                    "champion_result": champion_hour,
                    "candidate_result": candidate_hour,
                }
            )
        return {
            "candidate_id": candidate_id,
            "forecast_cycle": forecast_cycle,
            "trigger_kind": trigger_kind,
            "forecast_anchor": reference_time.isoformat(),
            "champion_artifact_hash": champion_hash,
            "candidate_artifact_hash": candidate_hash,
            "input_snapshot": input_snapshot,
            "input_snapshot_hash": input_snapshot_hash,
            "policy_snapshot": policy_snapshot,
            "policy_snapshot_hash": policy_snapshot_hash,
            "contract_version": MODEL_CANDIDATE_SHADOW_CONTRACT_VERSION,
            "hours": paired_hours,
        }

    async def _store_daily_forecasts(self, result: ForecastResult) -> None:
        """Store daily forecasts in database. Only writes if NOT locked (morning routine has priority). @zara"""
        try:
            now = dt_util.now()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            day_after_tomorrow = today + timedelta(days=2)
            daily_forecasts = [
                (
                    "today",
                    today,
                    result.forecast_today,
                    result.forecast_today_raw,
                    result.safeguard_applied_today,
                ),
                (
                    "tomorrow",
                    tomorrow,
                    result.forecast_tomorrow,
                    result.forecast_tomorrow_raw,
                    result.safeguard_applied_tomorrow,
                ),
                (
                    "day_after_tomorrow",
                    day_after_tomorrow,
                    result.forecast_day_after_tomorrow,
                    result.forecast_day_after_raw,
                    result.safeguard_applied_day_after,
                ),
            ]
            locked_types = []
            incomplete_types = []
            for (
                forecast_type,
                forecast_date,
                prediction_kwh,
                prediction_raw_kwh,
                safeguard_applied,
            ) in daily_forecasts:
                forecast_date_text = forecast_date.isoformat()
                if not hourly_forecast_date_covered(
                    result.hourly_values or [], forecast_date_text
                ):
                    incomplete_types.append(forecast_type)
                    continue
                locked_row = await self._db.fetchone(
                    "SELECT locked FROM daily_forecasts\n                       WHERE forecast_type = ? AND forecast_date = ?",
                    (forecast_type, forecast_date_text),
                )
                if locked_row and locked_row[0]:
                    locked_types.append(forecast_type)
                    continue
                await self._db.execute(
                    "INSERT INTO daily_forecasts\n                       (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                        safeguard_applied, source, created_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                           prediction_kwh = excluded.prediction_kwh,\n                           prediction_kwh_raw = excluded.prediction_kwh_raw,\n                           safeguard_applied = excluded.safeguard_applied,\n                           source = excluded.source\n                       WHERE locked = 0 OR locked IS NULL",
                    (
                        forecast_type,
                        forecast_date_text,
                        prediction_kwh,
                        prediction_raw_kwh,
                        safeguard_applied,
                        result.method,
                        dt_util.now(),
                    ),
                )
                await self._db.execute(
                    "INSERT INTO daily_forecast_updates\n                       (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                       VALUES (?, ?, ?, ?, ?)",
                    (
                        forecast_type,
                        forecast_date_text,
                        prediction_kwh,
                        result.method,
                        dt_util.now(),
                    ),
                )
                continue
            if locked_types:
                _LOGGER.debug(
                    "Forecast storage skipped for locked morning forecasts: %s",
                    ", ".join(locked_types),
                )
            if incomplete_types:
                _LOGGER.info(
                    "Forecast storage skipped for incomplete hourly horizons: %s",
                    ", ".join(incomplete_types),
                )
        except Exception as error:
            _LOGGER.debug(f"Could not store daily forecasts: {error}")
        return None if True else None

    def calculate_next_hour_prediction(
        self,
        forecast_today_kwh: float,
        weather_data: Optional[Dict[str, Any]] = None,
        sensor_data: Optional[Dict[str, Any]] = None,
    ) -> float:
        "Estimate the solar production for the next full hour. @zara\n\nArgs:\n    forecast_today_kwh: Today's total forecast\n    weather_data: Current weather data\n    sensor_data: Current sensor data\n\nReturns:\n    Next hour prediction in kWh\n"
        _LOGGER.debug("Calculating next hour prediction...")
        try:
            now = dt_util.now()
            next_hour = now + timedelta(hours=1)
            target_hour = next_hour.hour
            if not self.is_production_hour(next_hour):
                if (
                    sensor_data
                    and sensor_data.get("lux") is not None
                    and (sensor_data["lux"] < 500)
                ):
                    _LOGGER.debug(
                        f"Target hour {target_hour} has insufficient light (lux={sensor_data['lux']}). Next hour prediction is 0.0 kWh."
                    )
                    return 0.0
                _LOGGER.debug(
                    f"Target hour {target_hour} (local) is outside production hours. Next hour prediction is 0.0 kWh."
                )
                return 0.0
            profile_base_kwh = self._get_hourly_profile_base(
                forecast_today_kwh, target_hour
            )
            if profile_base_kwh is not None:
                base_kwh = profile_base_kwh
                _LOGGER.debug(
                    f"Using profile base for hour {target_hour} (local): {base_kwh:.3f} kWh."
                )
            else:
                base_kwh = forecast_today_kwh / 10.0
                _LOGGER.warning(
                    f"Hourly profile base unavailable for hour {target_hour}. Using simple fallback base: {base_kwh:.3f} kWh."
                )
            adjustment_factors = self._get_realtime_adjustment_factors(
                weather_data, sensor_data
            )
            adjusted_kwh = base_kwh
            adjustment_labels = []
            for factor_name, factor in adjustment_factors.items():
                adjusted_kwh *= factor
                adjustment_labels.append(f"{factor_name}={factor:.2f}")
            maximum_kwh = self.solar_capacity * 1.2
            adjusted_kwh = min(adjusted_kwh, maximum_kwh)
            prediction_kwh = max(0.0, adjusted_kwh)
            _LOGGER.info(
                f"Next Hour ({target_hour:02d}:00 local) Prediction: Base={base_kwh:.3f} kWh * Adjustments [{', '.join(adjustment_labels)}] -> Final={prediction_kwh:.3f} kWh"
            )
            return round(prediction_kwh, 3)
        except Exception as error:
            _LOGGER.error(
                f"Next hour prediction calculation failed: {error}", exc_info=True
            )
            return 0.0

    def _get_hourly_profile_base(
        self, forecast_today_kwh: float, target_hour: int
    ) -> Optional[float]:
        "Calculate the base production for a specific hour using solar profile. @zara\n\nArgs:\n    forecast_today_kwh: Today's total forecast\n    target_hour: Target hour (0-23)\n\nReturns:\n    Hourly base production or None\n"
        if target_hour < 6 or target_hour > 20:
            return 0.0
        peak_hour = 12
        profile_width_hours = 4.0
        hour_weight = math.exp(
            -((target_hour - peak_hour) ** 2) / (2 * profile_width_hours**2)
        )
        total_weight = sum(
            (
                math.exp(
                    -((profile_hour - peak_hour) ** 2) / (2 * profile_width_hours**2)
                )
                for profile_hour in range(6, 21)
            )
        )
        if total_weight <= 0:
            return
        hour_fraction = hour_weight / total_weight
        profile_base_kwh = forecast_today_kwh * hour_fraction
        _LOGGER.debug(
            f"Hourly base calculation: Hour={target_hour}, Fraction={hour_fraction:.4f} -> Base={profile_base_kwh:.3f} kWh"
        )
        return profile_base_kwh

    def _get_realtime_adjustment_factors(
        self,
        current_weather_data: Optional[Dict[str, Any]],
        current_sensor_data: Optional[Dict[str, Any]],
    ) -> Dict[str, float]:
        "Calculate adjustment multipliers based on current weather and sensor readings. @zara\n\nArgs:\n    current_weather_data: Current weather data\n    current_sensor_data: Current sensor data\n\nReturns:\n    Dictionary of adjustment factors\n"
        factors = {"cloud/lux": 1.0, "temperature": 1.0, "rain": 1.0}
        _LOGGER.debug("Calculating real-time adjustment factors...")
        lux = current_sensor_data.get("lux") if current_sensor_data else None
        cloud_cover = (
            current_weather_data.get("cloud_cover", current_weather_data.get("clouds"))
            if current_weather_data
            else None
        )
        if lux is not None and lux >= 0:
            reference_lux = 60000.0
            if lux < 1000:
                factors["cloud/lux"] = 0.1
            elif lux < 20000:
                factors["cloud/lux"] = 0.1 + lux / 20000.0 * 0.6
            else:
                factors["cloud/lux"] = 0.7 + min(lux / reference_lux * 0.5, 0.5)
            _LOGGER.debug(
                f"Using Lux value ({lux} lx) for adjustment factor: {factors['cloud/lux']:.2f}"
            )
        elif cloud_cover is not None:
            cloud_factor = self.weather_calculator.get_cloud_factor(cloud_cover)
            reference_cloud_factor = 0.65
            factors["cloud/lux"] = (
                cloud_factor / reference_cloud_factor
                if reference_cloud_factor > 0
                else 1.0
            )
            factors["cloud/lux"] = max(0.1, min(1.2, factors["cloud/lux"]))
            _LOGGER.debug(
                f"Using Cloud Cover ({cloud_cover}%) for adjustment. BaseFactor={cloud_factor:.2f} -> AdjFactor={factors['cloud/lux']:.2f}"
            )
        else:
            _LOGGER.debug("No Lux or Cloud data available for real-time adjustment.")
        temperature = (
            current_sensor_data.get("temperature") if current_sensor_data else None
        )
        if temperature is None and current_weather_data:
            temperature = current_weather_data.get("temperature")
        if temperature is not None:
            temperature_factor = self.weather_calculator.get_temperature_factor(
                temperature
            )
            factors["temperature"] = max(0.7, min(1.1, temperature_factor))
            _LOGGER.debug(
                f"Using Temperature ({temperature}C) for adjustment factor: {factors['temperature']:.2f}"
            )
        else:
            _LOGGER.debug("No Temperature data available for real-time adjustment.")
        rain = current_sensor_data.get("rain") if current_sensor_data else None
        if rain is not None and rain > 0:
            if rain > 5.0:
                factors["rain"] = 0.2
            elif rain > 1.0:
                factors["rain"] = 0.5
            else:
                factors["rain"] = 0.8
            _LOGGER.debug(
                f"Using Rain value ({rain}) for adjustment factor: {factors['rain']:.2f}"
            )
        for factor_name in factors:
            factors[factor_name] = max(0.0, min(1.5, factors[factor_name]))
        _LOGGER.debug(f"Final real-time adjustment factors: {factors}")
        return factors

    async def get_latest_forecasts(self) -> Dict[str, Any]:
        "Get latest forecasts from database. @zara\n\nReturns:\n    Dictionary with today, tomorrow, day_after_tomorrow forecasts\n"
        try:
            today_date_text = dt_util.now().date().isoformat()
            rows = await self._db.fetchall(
                "SELECT forecast_type, forecast_date, prediction_kwh, locked, source\n                   FROM daily_forecasts\n                   WHERE forecast_date >= ?\n                   ORDER BY forecast_date",
                (today_date_text,),
            )
            forecasts = {"today": None, "tomorrow": None, "day_after_tomorrow": None}
            for row in rows:
                forecast_type = row[0]
                if forecast_type in forecasts:
                    forecasts[forecast_type] = {
                        "date": str(row[1]),
                        "prediction_kwh": row[2],
                        "locked": row[3],
                        "source": row[4],
                    }
            return forecasts
        except Exception as error:
            _LOGGER.error(f"Could not get latest forecasts: {error}")
            return {"today": None, "tomorrow": None, "day_after_tomorrow": None}
