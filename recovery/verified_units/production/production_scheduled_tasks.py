# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Scheduled Tasks Manager for Solar Forecast ML.

Manages all scheduled tasks including morning routine, hourly updates,
end-of-day processing, and adaptive forecast checks.

@author zara
"""
import asyncio
import logging
import math
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_point_in_time,
)
from .production_morning_routine import MorningRoutineHandler
from .production_adaptive_forecast import AdaptiveForecastEngine
from .production_operational_reforecast import OperationalReforecastEngine
from .production_actual_resolver import ActualSourceResolver
from ..data.data_daily_summaries import DailySummariesHandler
from ..data.data_model_candidate_shadow import ModelCandidateShadowHandler
from ..ai.ai_model_reaction import ModelReactionHandler
from ..services.service_system_report import SystemReportGenerator
from ..const import (
    ADAPTIVE_CHECK_HOUR,
    ADAPTIVE_CHECK_MINUTE,
    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
    CURTAILMENT_FLOW_LOW_BATTERY_W,
    CURTAILMENT_FLOW_MIN_HOUSE_W,
    CONF_ADAPTIVE_FORECAST_MODE,
    CONF_BATTERY_SOC_SENSOR,
    CONF_HAS_BATTERY,
    CONF_HUMIDITY_SENSOR,
    CONF_LUX_SENSOR,
    CONF_MAX_GRID_EXPORT_W,
    CONF_PRESSURE_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_SOLAR_RADIATION_SENSOR,
    CONF_SOLAR_TO_BATTERY_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WIND_SENSOR,
    CONF_ZERO_EXPORT_MODE,
    DEFAULT_ADAPTIVE_FORECAST_MODE,
    DEFAULT_HAS_BATTERY,
    DEFAULT_MAX_GRID_EXPORT_W,
    DEFAULT_WINTER_MODE,
    DEFAULT_ZERO_EXPORT_MODE,
    CONF_WINTER_MODE,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..core.core_hubble import Hubble
from ..core.core_panel_group_forecast import get_group_capacity_kwp
from ..ai.ai_weather_mlp import (
    WEATHER_MLP_EVALUATION_FOLDS,
    WEATHER_MLP_FEATURE_COUNT,
    WEATHER_MLP_MIN_TRAINING_SAMPLES,
    build_weather_mlp_features,
    day_blocked_folds,
    held_out_training_shortfall,
    pooled_evaluation_metrics,
    residual_weather_mlp_target,
    weather_mlp_persist_verdict,
)
from ..data.data_learning_filter import (
    CLEAN_EVALUATION_ELIGIBILITY_SQL,
    CURTAILMENT_THROTTLE_REASON_SQL,
    CURTAILMENT_THROTTLE_REASONS,
    HOURLY_LEARNING_ELIGIBILITY_SQL,
    WEATHER_ALERT_UNEXPECTED_SUN,
    production_miss_uplift,
)
from ..physics.physics_calibration_telemetry import (
    advantage_factor,
    group_best_method,
    recommended_tfs_weight,
    regime_best_method,
)
from ..data.db_manager import (
    WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
    WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
    WEATHER_MLP_TRAINING_CONTRACT_VERSION,
)
from .production_rest_of_day_uplift import rest_of_day_uplift_from_weather_evidence

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)
DYNAMIC_TASK_BUNDLE_PAUSE_SECONDS = 2.0
HOURLY_ACTUAL_UPDATE_MINUTE = 5
HOURLY_ACTUAL_FIRST_HOUR = 4
HOURLY_ACTUAL_LAST_HOUR = 21
HOURLY_ACTUAL_CATCHUP_GRACE = timedelta(minutes=1)
HOURLY_ACTUAL_PARTIAL_WARN_MIN_KWH = 0.01
REGIME_LEARNING_SMOOTHING = 0.3
REGIME_TFS_MIN_SAMPLES = 3
BAD_WEATHER_RAIN_MIN_MM = 0.2
BAD_WEATHER_RAIN_CLOUDS_THRESHOLD = 55.0
BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD = 80.0
BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD = 0.45
BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD = 0.72
BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD = 80.0
BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD = 0.4
BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD = 70.0
BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD = 0.82
BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD = 70.0
BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD = 0.35
POWER_SHADOW_DIFF_ABS_KWH = 0.05
POWER_SHADOW_DIFF_RELATIVE = 0.25
MONTHLY_REPORT_TASK_KEY = "monthly_system_report"
MONTHLY_REPORT_FORMAT_VERSION = 3
FAST_WEATHER_EVENT_CHECK_MINUTES = (0, 15, 30, 45)
FAST_WEATHER_EVENT_MIN_FORECAST_WM2 = 100.0
FAST_WEATHER_EVENT_MAX_RATIO = 0.45
FAST_WEATHER_EVENT_CONFIRMATIONS = 2
FAST_WEATHER_EVENT_MAX_SENSOR_AGE_MINUTES = 20


def _is_fast_weather_event_sample(
    actual_radiation: Optional[float], forecast_radiation: Optional[float]
) -> bool:
    """Return whether a live radiation sample proves a strong local miss."""
    if actual_radiation is None or forecast_radiation is None:
        return False
    if not (math.isfinite(actual_radiation) and math.isfinite(forecast_radiation)):
        return False
    if actual_radiation < 0 or forecast_radiation < FAST_WEATHER_EVENT_MIN_FORECAST_WM2:
        return False
    return actual_radiation < forecast_radiation * FAST_WEATHER_EVENT_MAX_RATIO


def _classify_static_weather_regime_for_learning(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify persisted forecast weather into static learning regimes."""
    clear_sky_ratio = None
    if clear_sky > 50 and ghi >= 0:
        clear_sky_ratio = min(max(ghi / clear_sky, 0.0), 1.05)
    diffuse_share = None
    if ghi > 5 and dhi >= 0:
        diffuse_share = min(max(dhi / ghi, 0.0), 1.5)
    if sun_elevation <= 8.0:
        if clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD or (
            clear_sky_ratio is not None
            and clear_sky_ratio <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD
        ):
            return "low_sun_uncertain"
    if rain >= BAD_WEATHER_RAIN_MIN_MM:
        if (
            clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
            or humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD
            or (
                clear_sky_ratio is not None
                and clear_sky_ratio <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD
            )
            or (
                diffuse_share is not None
                and diffuse_share >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
            )
        ):
            return "rain_overcast"
    if clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD:
        if (
            clear_sky_ratio is None
            or clear_sky_ratio <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD
        ):
            return "low_ghi_overcast"
    if (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and diffuse_share is not None
        and (diffuse_share >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    ):
        return "diffuse_overcast"
    if clouds < 35.0:
        if clear_sky_ratio is None or clear_sky_ratio >= 0.45:
            return "stable_good"
    if rain <= 0.05 and clouds >= 60.0:
        if clear_sky_ratio is None or clear_sky_ratio <= 0.5:
            return "dry_cloud_edge"
    if rain <= 0.05 and clear_sky_ratio is not None and (clear_sky_ratio >= 0.5):
        return "bright_mixed"
    return "mixed_uncertain"


def rescore_raw_forecast_blend(day_folds):
    """Chronological MAE rescore for the raw-forecast contract migration.

    Each fold is (key, physics absolute errors, AI absolute errors) for one day.
    The physics error is |applied factor × raw − actual|.
    """
    from ..physics.physics_calibration_telemetry import (
        smoothed_mae_rows as smooth_mae_rows,
    )

    return smooth_mae_rows(day_folds)


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


class ScheduledTasksManager:
    """Manages scheduled tasks for the Solar Forecast ML integration. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "SolarForecastMLCoordinator",
        db_manager: "DatabaseManager",
    ):
        """Initialize the ScheduledTasksManager. @zara"""
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager
        self._listeners = []
        self._dynamic_listeners = []
        self._stop_listener = None
        self._startup_listener = None
        self._hourly_actual_lock = asyncio.Lock()
        self.morning_routine_handler = MorningRoutineHandler(
            hass, db_manager, coordinator
        )
        self.model_candidate_shadow_handler = ModelCandidateShadowHandler(db_manager)
        self.model_reaction_handler = ModelReactionHandler(db_manager)
        self.actual_resolver = ActualSourceResolver(
            hass=hass, coordinator=coordinator, db_manager=db_manager
        )
        self.adaptive_forecast_engine = AdaptiveForecastEngine(
            hass, coordinator, db_manager
        )
        self.operational_reforecast_engine = OperationalReforecastEngine(
            hass, coordinator, db_manager
        )
        self.daily_summaries_handler = DailySummariesHandler(hass, db_manager)
        self._end_of_day_running = False
        self._weather_corrector = None
        self._pipeline_corrector = None
        self._kalman_tracker = None
        self._weather_mlp = None
        self._weather_mlp_retrain_rejected = False
        self._weather_mlp_weights_loaded = False
        self._last_operational_reforecast_trigger_key = None
        self._last_weather_event_reforecast_at = None
        self._last_weather_event_reforecast_hour_key = None
        self._fast_weather_event_lock = asyncio.Lock()
        self._fast_weather_event_confirmations = 0
        self._fast_weather_event_last_sample_key = None
        self._monthly_report_lock = asyncio.Lock()
        self._morning_pipeline_lock = asyncio.Lock()
        self._model_candidate_gate_lock = asyncio.Lock()
        self._astronomy_refresh_task = None
        self._operational_reforecast_failures = 0
        self._operational_reforecast_retry_at = None
        self._pending_operational_reforecast = None
        self._last_operational_reforecast_error = None
        self._init_weather_ai()
        _LOGGER.debug("ScheduledTasksManager initialized")

    def set_db_manager(self, db_manager: "DatabaseManager") -> None:
        """Bind database-backed helpers after DataManager initialization."""
        self.db = db_manager
        self.morning_routine_handler.set_db_manager(db_manager)
        self.model_candidate_shadow_handler = ModelCandidateShadowHandler(db_manager)
        self.model_reaction_handler.set_db_manager(db_manager)
        self.actual_resolver.db = db_manager
        self.adaptive_forecast_engine.db = db_manager
        self.adaptive_forecast_engine._forecast_regeneration.db = db_manager
        self.operational_reforecast_engine.db = db_manager
        self.operational_reforecast_engine._forecast_regeneration.db = db_manager
        self.daily_summaries_handler.db = db_manager

    async def resume_pending_model_reaction(self, predictor: Any) -> None:
        """Resume a persisted model reaction after Home Assistant startup."""
        try:
            retrain_request = await self.db.get_open_model_retrain_request()
            if retrain_request is None:
                return
            reaction_outcome = await self.model_reaction_handler.process(
                dt_util.now().date().isoformat(), predictor
            )
            log_outcome = (
                _LOGGER.error if reaction_outcome.status == "failed" else _LOGGER.info
            )
            log_outcome(
                "Startup model reaction resume: status=%s request=%s candidate=%s reason=%s",
                reaction_outcome.status,
                reaction_outcome.request_id,
                reaction_outcome.candidate_id,
                reaction_outcome.reason,
            )
        except Exception as resume_error:
            _LOGGER.error(
                "Startup model reaction resume failed: %s", resume_error, exc_info=True
            )
        return None if True else None

    async def resume_pending_model_candidate_gate(self, predictor: Any) -> None:
        """Reconcile and resume the complete candidate gate after startup."""
        try:
            async with self._model_candidate_gate_lock:
                evidence_version_count = (
                    await self.model_candidate_shadow_handler.refresh_actual_evidence()
                )
                invalidated_evaluation_count = (
                    await self.db.invalidate_stale_model_candidate_evaluations()
                )
                candidate_id = await predictor.reconcile_pending_model_candidates()
                candidate_evaluation = None
                if candidate_id:
                    candidate_evaluation = await self.model_candidate_shadow_handler.evaluate_ready_candidate(
                        candidate_id=candidate_id
                    )
                if candidate_evaluation is None:
                    candidate_evaluation = (
                        await self.db.get_retryable_model_candidate_promotion()
                    )
                if candidate_evaluation and candidate_evaluation["decision"] == "GO":
                    await self._promote_model_candidate_once(
                        predictor, candidate_evaluation
                    )
                elif candidate_evaluation is None:
                    gate_readiness = None
                    if candidate_id:
                        gate_readiness = (
                            await self.model_candidate_shadow_handler.gate_readiness(
                                candidate_id
                            )
                        )
                        try:
                            persist_gate_status = getattr(
                                self.db, "persist_ai_gate_status", None
                            )
                            if callable(persist_gate_status):
                                await persist_gate_status(candidate_id, gate_readiness)
                        except Exception as gate_status_error:
                            _LOGGER.debug(
                                "AI gate status persist failed: %s", gate_status_error
                            )
                    await self.db.timeout_unevaluated_model_candidate(
                        source="startup", readiness=gate_readiness
                    )
            if candidate_id:
                _LOGGER.info(
                    "Model candidate gate resumed after startup: actual_versions=%d invalidated_evaluations=%d candidate=%s decision=%s",
                    evidence_version_count,
                    invalidated_evaluation_count,
                    candidate_id,
                    (
                        candidate_evaluation["decision"]
                        if candidate_evaluation
                        else "WAIT"
                    ),
                )
            else:
                _LOGGER.info(
                    "Model candidate gate resumed after startup: no pending candidate (actual_versions=%d invalidated_evaluations=%d)",
                    evidence_version_count,
                    invalidated_evaluation_count,
                )
        except Exception:
            _LOGGER.error(
                "Failed to resume model candidate gate after startup", exc_info=True
            )
        if True:
            return

    async def _promote_model_candidate_once(
        self, predictor: Any, evaluation: Dict[str, Any]
    ) -> bool:
        """Promote once or accept a concurrently completed identical promotion."""
        try:
            await predictor.promote_model_candidate(
                evaluation["candidate_id"], evaluation["evaluation_id"]
            )
        except Exception:
            active_candidate_row = await self.db.fetchone(
                "SELECT active_candidate_id FROM ai_active_model_pointer\n                    WHERE id = 1"
            )
            if (
                active_candidate_row
                and active_candidate_row[0] == evaluation["candidate_id"]
            ):
                _LOGGER.info(
                    "Model candidate promotion already completed concurrently: candidate=%s evaluation=%s",
                    evaluation["candidate_id"],
                    evaluation["evaluation_id"],
                )
                return False
            raise
        return True

    def _init_weather_ai(self):
        """Initialize Weather MLP and Kalman tracker. @zara"""
        try:
            from ..data.data_weather_kalman import (
                WeatherKalmanTracker as kalman_tracker_type,
            )

            self._kalman_tracker = kalman_tracker_type()
        except Exception as error:
            _LOGGER.warning("Could not initialize Kalman tracker: %s", error)
        try:
            from ..ai.ai_weather_mlp import TinyWeatherMLP as weather_mlp_type

            self._weather_mlp = weather_mlp_type()
        except Exception as error:
            _LOGGER.warning("Could not initialize Weather MLP: %s", error)
        return None if True else None

    def _log_group_power_shadow(
        self,
        *,
        date: str,
        hour: int,
        group_resolution: Any,
        reference_kwh: Optional[float],
        reference_label: str,
    ) -> None:
        if getattr(group_resolution, "source", None) != "group_power_recorder":
            return None if True else None
        diagnostics = getattr(group_resolution, "diagnostics", {}) or {}
        configured_group_count = diagnostics.get("configured_groups")
        exact_group_count = diagnostics.get("exact_groups", 0)
        total_group_count = diagnostics.get("total_groups", configured_group_count)
        max_uncovered_gap_seconds = diagnostics.get("max_uncovered_gap_seconds")
        reason = (
            getattr(group_resolution, "exclusion_reason", None)
            or diagnostics.get("reason")
            or getattr(group_resolution, "quality", "unknown")
        )
        if getattr(group_resolution, "hour_kwh", None) is None:
            if reason == "missing_group_power_sensors":
                _LOGGER.info(
                    "Group power shadow for %s_%02d unavailable: groups=%s/%s configured, reason=%s",
                    date,
                    hour,
                    configured_group_count,
                    total_group_count,
                    reason,
                )
            else:
                _LOGGER.info(
                    "Group power shadow for %s_%02d unavailable: groups=%s/%s exact, min_coverage=%.2f%%, max_gap=%ss, max_uncovered=%ss, invalid=%d, quality=%s, reason=%s",
                    date,
                    hour,
                    exact_group_count,
                    configured_group_count,
                    getattr(group_resolution, "total_power_coverage_percent", 0.0),
                    getattr(group_resolution, "max_sample_gap_seconds", None),
                    max_uncovered_gap_seconds,
                    getattr(group_resolution, "invalid_sample_count", 0),
                    getattr(group_resolution, "quality", "unknown"),
                    reason,
                )
            return None if True else None
        resolved_kwh = float(group_resolution.hour_kwh)
        group_diagnostics = diagnostics.get("groups") or {}
        group_descriptions = []
        for group_name, group_detail in group_diagnostics.items():
            value = group_detail.get("hour_kwh")
            if not value is not None:
                continue
            group_descriptions.append(f"{group_name}={float(value):.4f}")
        if group_descriptions:
            _LOGGER.debug(
                "Group power shadow details for %s_%02d: %s",
                date,
                hour,
                ", ".join(group_descriptions),
            )
        if reference_kwh is None:
            _LOGGER.debug(
                "Group power shadow for %s_%02d: group_sum=%.4f kWh, %s=NULL, groups=%s/%s exact, min_coverage=%.2f%%, max_gap=%ss, max_uncovered=%ss, invalid=%d, quality=%s",
                date,
                hour,
                resolved_kwh,
                reference_label,
                exact_group_count,
                configured_group_count,
                getattr(group_resolution, "total_power_coverage_percent", 0.0),
                getattr(group_resolution, "max_sample_gap_seconds", None),
                max_uncovered_gap_seconds,
                getattr(group_resolution, "invalid_sample_count", 0),
                getattr(group_resolution, "quality", "unknown"),
            )
            return None if True else None
        reference_energy_kwh = float(reference_kwh)
        difference_kwh = resolved_kwh - reference_energy_kwh
        absolute_difference_kwh = abs(difference_kwh)
        relative_difference = absolute_difference_kwh / max(
            abs(reference_energy_kwh), 0.01
        )
        excessive_difference = (
            absolute_difference_kwh > POWER_SHADOW_DIFF_ABS_KWH
            and relative_difference > POWER_SHADOW_DIFF_RELATIVE
        )
        log_difference = _LOGGER.warning if excessive_difference else _LOGGER.debug
        log_difference(
            "Group power shadow for %s_%02d: group_sum=%.4f kWh, %s=%.4f kWh, diff=%+.4f kWh, groups=%s/%s exact, min_coverage=%.2f%%, max_gap=%ss, max_uncovered=%ss, invalid=%d, quality=%s%s",
            date,
            hour,
            resolved_kwh,
            reference_label,
            reference_energy_kwh,
            difference_kwh,
            exact_group_count,
            configured_group_count,
            getattr(group_resolution, "total_power_coverage_percent", 0.0),
            getattr(group_resolution, "max_sample_gap_seconds", None),
            max_uncovered_gap_seconds,
            getattr(group_resolution, "invalid_sample_count", 0),
            getattr(group_resolution, "quality", "unknown"),
            " (exceeds shadow guard)" if excessive_difference else "",
        )
        return None if True else None

    def set_pipeline_corrector(self, corrector) -> None:
        """Register pipeline corrector for MLP/Kalman attachment. @zara"""
        self._pipeline_corrector = corrector
        if self._kalman_tracker:
            corrector.set_kalman_tracker(self._kalman_tracker)
        self._attach_mlp_to_correctors()
        self._schedule_weather_mlp_refresh()

    def _weather_mlp_load_pending(self) -> bool:
        """True while persisted weights could be read but have not been yet."""
        return bool(
            self._weather_mlp is not None
            and self.db is not None
            and (not self._weather_mlp_weights_loaded)
            and self._has_available_local_solar_observation()
        )

    def _schedule_weather_mlp_refresh(self) -> None:
        if self._weather_mlp_load_pending():
            self.hass.async_create_task(self.async_refresh_weather_mlp_attachment())
        return None if True else None

    async def async_refresh_weather_mlp_attachment(self) -> None:
        """Re-evaluate MLP attachment; read persisted weights once that is possible. @zara

        Called after the pipeline corrector exists and whenever the local
        sensors become available, so a restart during sensor unavailability
        does not leave a persisted model unused until the next training.
        """
        if self._weather_mlp is None:
            return
        if self._weather_mlp_load_pending():
            await self._load_weather_mlp_weights()
            return
        self._attach_mlp_to_correctors()

    def _has_available_local_solar_observation(self) -> bool:
        """Return whether this installation currently has usable measured GHI."""
        try:
            coordinator = getattr(self, "coordinator", None)
            config_entry = getattr(coordinator, "config_entry", None)
            entry_data = getattr(config_entry, "data", {}) or {}
            entry_options = getattr(config_entry, "options", {}) or {}
            solar_radiation_entity_id = entry_options.get(
                CONF_SOLAR_RADIATION_SENSOR
            ) or entry_data.get(CONF_SOLAR_RADIATION_SENSOR)
            if (
                not isinstance(solar_radiation_entity_id, str)
                or not solar_radiation_entity_id
            ):
                return False
            states = getattr(getattr(self, "hass", None), "states", None)
            get_state = getattr(states, "get", None)
            if not callable(get_state):
                return False
            state = get_state(solar_radiation_entity_id)
            state_value = getattr(state, "state", None)
            if state_value is None or str(state_value).strip().lower() in {
                "",
                "none",
                "unknown",
                "unavailable",
            }:
                return False
            value = float(state_value)
            return math.isfinite(value) and value >= 0.0
        except (TypeError, ValueError, AttributeError):
            return False

    async def _load_weather_mlp_weights(self):
        """Load Weather MLP weights from DB if available. @zara"""
        if self.db is None:
            return
        if not self._has_available_local_solar_observation():
            self._attach_mlp_to_correctors()
            _LOGGER.info(
                "Weather MLP disabled: no currently usable local radiation sensor"
            )
            return
        try:
            weights = await self.db.load_weather_mlp_weights()
            self._weather_mlp_weights_loaded = True
            if weights and self._weather_mlp:
                self._weather_mlp.set_weights(weights)
                _LOGGER.info(
                    "Weather MLP loaded from DB: %d samples, R²=%.3f",
                    weights.get("training_samples", 0),
                    weights.get("accuracy", 0.0),
                )
            else:
                _LOGGER.info("Weather MLP: not trained yet, using static factors")
            self._attach_mlp_to_correctors()
        except Exception as error:
            _LOGGER.debug("Could not load Weather MLP weights: %s", error)
        return None if True else None

    def _spawn_weather_mlp(self):
        """Fresh model of the serving model's type; evaluation and the final fit never touch the serving model. @zara"""
        return type(self._weather_mlp)()

    async def _evaluate_weather_mlp_pooled(
        self, X_data: List[Any], y_targets: List[float], sample_dates: List[str]
    ) -> Optional[Dict[str, float]]:
        """Held-out verdict: train on day blocks, predict the days left out, pool everything. @zara

        Returns None when fewer than two days exist. Every fold starts from a
        fresh model so no held-out day leaks in through warm starts.
        """
        folds = day_blocked_folds(sample_dates, WEATHER_MLP_EVALUATION_FOLDS)
        if not folds:
            return
        pooled_targets = []
        pooled_predictions = []
        for training_indices, held_out_indices in folds:
            fold_model = self._spawn_weather_mlp()
            training_result = await fold_model.train(
                X_data=[X_data[sample_index] for sample_index in training_indices],
                y_targets=[
                    y_targets[sample_index] for sample_index in training_indices
                ],
                epochs=50,
                validation_split=0.2,
                early_stopping_patience=10,
            )
            if not training_result.get("success"):
                raise RuntimeError(
                    training_result.get("error_message")
                    or "held-out fold training failed"
                )
            for sample_index in held_out_indices:
                pooled_targets.append(float(y_targets[sample_index]))
                pooled_predictions.append(
                    float(fold_model.predict(X_data[sample_index]))
                )
        metrics = pooled_evaluation_metrics(pooled_targets, pooled_predictions)
        metrics["folds"] = len(folds)
        return metrics

    def _attach_mlp_to_correctors(self) -> None:
        """Synchronize MLP attachment with the current measured-GHI capability."""
        serving_mlp = None
        if (
            self._weather_mlp
            and self._weather_mlp.is_usable
            and self._has_available_local_solar_observation()
        ):
            serving_mlp = self._weather_mlp
        for corrector in (self._weather_corrector, self._pipeline_corrector):
            if corrector is not None:
                corrector.set_weather_mlp(serving_mlp)

    def _get_weather_corrector(self):
        """Get or create WeatherForecastCorrector instance (lazy initialization). @zara"""
        if self._weather_corrector is None:
            from ..data.data_weather_corrector import (
                WeatherForecastCorrector as weather_corrector_type,
            )

            winter_mode = DEFAULT_WINTER_MODE
            if self.coordinator and self.coordinator.config_entry:
                winter_mode = self.coordinator.config_entry.options.get(
                    CONF_WINTER_MODE, DEFAULT_WINTER_MODE
                )
            self._weather_corrector = weather_corrector_type(
                self.hass, self.db, winter_mode=winter_mode
            )
            self._attach_mlp_to_correctors()
            self._schedule_weather_mlp_refresh()
            if self._kalman_tracker:
                self._weather_corrector.set_kalman_tracker(self._kalman_tracker)
        return self._weather_corrector

    @staticmethod
    def _parse_cache_datetime(
        value: Any, reference: Optional[datetime] = None
    ) -> Optional[datetime]:
        if value is None:
            return
        if isinstance(value, datetime):
            parsed_datetime = value
        else:
            try:
                parsed_datetime = datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return
        if reference is not None:
            if parsed_datetime.tzinfo is None and reference.tzinfo is not None:
                parsed_datetime = parsed_datetime.replace(tzinfo=reference.tzinfo)
            elif parsed_datetime.tzinfo is not None and reference.tzinfo is None:
                parsed_datetime = parsed_datetime.replace(tzinfo=None)
            elif parsed_datetime.tzinfo is not None and reference.tzinfo is not None:
                parsed_datetime = parsed_datetime.astimezone(reference.tzinfo)
        return parsed_datetime if True else None

    async def _resolve_eod_actual_yield(self, now: datetime) -> float:
        live_state_manager = getattr(
            self.coordinator, "actual_live_state_manager", None
        )
        live_state = getattr(live_state_manager, "state", {}) or {}
        if live_state.get("date") == now.date().isoformat():
            try:
                actual_yield_kwh = float(live_state.get("total_actual_kwh"))
            except (TypeError, ValueError):
                actual_yield_kwh = 0.0
            if actual_yield_kwh > 0:
                return actual_yield_kwh
        actual_sum_row = await self.db.fetchone(
            "SELECT COALESCE(SUM(actual_kwh), 0.0)\n               FROM hourly_predictions\n               WHERE target_date = ? AND actual_kwh IS NOT NULL",
            (now.date().isoformat(),),
        )
        try:
            return float(actual_sum_row[0] or 0.0) if actual_sum_row else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def _get_production_window_from_cache(
        self, target_date: datetime
    ) -> Optional[tuple]:
        """Get production window from astronomy cache. @zara"""
        try:
            date_key = (
                target_date.isoformat()
                if hasattr(target_date, "isoformat")
                else str(target_date)
            )
            row = await self.db.fetchone(
                "SELECT sunrise, sunset FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = 12 LIMIT 1",
                (date_key,),
            )
            if row and row[0] and row[1]:
                production_start = (
                    datetime.fromisoformat(str(row[0]))
                    if isinstance(row[0], str)
                    else row[0]
                )
                production_end = (
                    datetime.fromisoformat(str(row[1]))
                    if isinstance(row[1], str)
                    else row[1]
                )
                return (production_start, production_end)
        except Exception as error:
            _LOGGER.error("Error getting production window: %s", error)
            return
        return None if True else None

    async def _get_sunrise_for_today(self) -> Optional[datetime]:
        """Get sunrise time for today from database. @zara"""
        try:
            today_date = dt_util.now().date()
            row = await self.db.fetchone(
                "SELECT sunrise FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = 12 LIMIT 1",
                (today_date.isoformat(),),
            )
            if not row or not row[0]:
                _LOGGER.warning("No astronomy data for %s", today_date)
                return
            sunrise_value = str(row[0])
            sunrise = datetime.fromisoformat(sunrise_value)
            if sunrise.tzinfo is None:
                local_timezone = dt_util.now().tzinfo
                if local_timezone:
                    sunrise = sunrise.replace(tzinfo=local_timezone)
            return sunrise
        except Exception as error:
            _LOGGER.error("Error getting sunrise: %s", error)
            return

    def _calculate_dynamic_times(self, sunrise: datetime) -> dict:
        """Calculate all dynamic task times based on sunrise. @zara"""
        return {
            "weather_blend": sunrise - timedelta(minutes=55),
            "corrected_forecast": sunrise - timedelta(minutes=50),
            "final_forecast": sunrise - timedelta(minutes=45),
        }

    def _group_dynamic_tasks(
        self, times: Dict[str, datetime]
    ) -> List[tuple[datetime, List[str]]]:
        """Group dynamic tasks that resolve to the same execution time. @zara"""
        task_groups = {}
        ordered_task_names = ["weather_blend", "corrected_forecast", "final_forecast"]
        for task_name in ordered_task_names:
            task_time = times[task_name]
            time_key = task_time.isoformat()
            task_group = task_groups.setdefault(
                time_key, {"time": task_time, "tasks": []}
            )
            task_group["tasks"].append(task_name)
        grouped_tasks = sorted(
            (
                (task_group["time"], task_group["tasks"])
                for task_group in task_groups.values()
            ),
            key=lambda item: item[0],
        )
        return grouped_tasks

    async def _schedule_dynamic_tasks_for_today(self) -> None:
        """Schedule dynamic tasks based on today's sunrise. @zara"""
        for remove_listener in self._dynamic_listeners:
            try:
                remove_listener()
            except Exception:
                continue
        self._dynamic_listeners = []
        sunrise = await self._get_sunrise_for_today()
        if not sunrise:
            _LOGGER.warning("Could not get sunrise - using fallback (08:00)")
            current_time = dt_util.now()
            sunrise = datetime.combine(current_time.date(), time(8, 0))
            if current_time.tzinfo:
                sunrise = sunrise.replace(tzinfo=current_time.tzinfo)
        dynamic_times = self._calculate_dynamic_times(sunrise)
        current_time = dt_util.now()
        grouped_tasks = self._group_dynamic_tasks(dynamic_times)
        catchup_time = current_time + timedelta(minutes=2)
        task_labels = {
            "weather_blend": "Weather blend",
            "corrected_forecast": "Corrected forecast",
            "final_forecast": "Final forecast",
        }
        task_offsets = {
            "weather_blend": "55min",
            "corrected_forecast": "50min",
            "final_forecast": "45min",
        }
        for scheduled_time, bundled_task_names in grouped_tasks:
            if scheduled_time <= catchup_time:
                continue
            if len(bundled_task_names) == 1:
                task_name = bundled_task_names[0]
                task_callbacks = {
                    "weather_blend": self._dynamic_weather_blend,
                    "corrected_forecast": self._dynamic_corrected_forecast,
                    "final_forecast": self._dynamic_final_forecast,
                }
                remove_listener = async_track_point_in_time(
                    self.hass, task_callbacks[task_name], scheduled_time
                )
                self._dynamic_listeners.append(remove_listener)
                _LOGGER.info(
                    "Dynamic scheduling: %s at %s (sunrise %s - %s)",
                    task_labels[task_name],
                    scheduled_time.strftime("%H:%M"),
                    sunrise.strftime("%H:%M"),
                    task_offsets[task_name],
                )
                continue

            async def _run_bundle(
                run_at: datetime, tasks: List[str] = bundled_task_names
            ) -> None:
                await self._dynamic_task_bundle(run_at, tasks)

            remove_listener = async_track_point_in_time(
                self.hass, _run_bundle, scheduled_time
            )
            self._dynamic_listeners.append(remove_listener)
            _LOGGER.info(
                "Dynamic scheduling: Bundled %s at %s because sunrise-based times collapsed to the same minute (sunrise %s)",
                ", ".join(
                    (
                        task_labels[bundled_task_name]
                        for bundled_task_name in bundled_task_names
                    )
                ),
                scheduled_time.strftime("%H:%M"),
                sunrise.strftime("%H:%M"),
            )
            continue
        if not self._dynamic_listeners:
            _LOGGER.debug("All dynamic task times already passed for today")
            await self._check_morning_routine_catchup()
        return None if True else None

    async def _dynamic_task_bundle(self, now: datetime, task_names: List[str]) -> None:
        """Run colliding dynamic morning tasks sequentially. @zara"""
        task_callbacks = {
            "weather_blend": self._dynamic_weather_blend,
            "corrected_forecast": self._dynamic_corrected_forecast,
            "final_forecast": self._dynamic_final_forecast,
        }
        _LOGGER.info(
            "Dynamic scheduling: Running bundled morning tasks sequentially at %s: %s",
            now.strftime("%H:%M"),
            ", ".join(task_names),
        )
        for index, task_name in enumerate(task_names):
            await task_callbacks[task_name](now)
            if index < len(task_names) - 1:
                await asyncio.sleep(DYNAMIC_TASK_BUNDLE_PAUSE_SECONDS)

    async def _check_morning_routine_catchup(self) -> None:
        """Check if morning routine was missed and run catch-up if needed.

        IMPORTANT: This ONLY runs on fresh installations to avoid overwriting
        the correct morning forecast created 45min before sunrise! @zara
        """
        try:
            today = dt_util.now().date().isoformat()
            hour = dt_util.now().hour
            if hour < 5 or hour > 20:
                _LOGGER.debug("Outside catch-up window (5:00-20:00), skipping")
                return
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE target_date = ?",
                (today,),
            )
            prediction_count = row[0] if row else 0
            if prediction_count > 0:
                _LOGGER.debug(
                    "Predictions exist for %s (%d hours) - no catch-up to protect existing data",
                    today,
                    prediction_count,
                )
                return
            forecast_source_row = await self.db.fetchone(
                "SELECT source FROM daily_forecasts\n                   WHERE forecast_date = ? AND forecast_type = 'today'",
                (today,),
            )
            if forecast_source_row:
                _LOGGER.debug(
                    "Daily forecast exists for %s (source=%s) - no catch-up needed",
                    today,
                    forecast_source_row[0],
                )
                return
            yesterday = (dt_util.now() - timedelta(days=1)).date().isoformat()
            yesterday_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE target_date = ?",
                (yesterday,),
            )
            has_yesterday_predictions = (
                (yesterday_count_row[0] or 0) > 0 if yesterday_count_row else False
            )
            if has_yesterday_predictions:
                _LOGGER.debug(
                    "Historical data exists - this is not a fresh install, morning routine will run at scheduled time"
                )
                return
            _LOGGER.warning(
                "FRESH INSTALLATION detected! Running initial forecast setup for %s",
                today,
            )
            await self._dynamic_weather_blend(dt_util.now())
            await asyncio.sleep(2)
            await self._dynamic_corrected_forecast(dt_util.now())
            await asyncio.sleep(2)
            success = await self._execute_morning_routine(
                date=today, forecast_cycle="catchup", trigger_kind="catchup"
            )
            if success:
                _LOGGER.info(
                    "Initial forecast setup SUCCESSFUL for %s (fresh installation)",
                    today,
                )
            else:
                _LOGGER.error(
                    "Initial forecast setup FAILED for %s (fresh installation)", today
                )
        except Exception as error:
            _LOGGER.error("Morning routine catch-up error: %s", error, exc_info=True)
        return None if True else None

    async def _dynamic_weather_blend(self, now: datetime) -> None:
        """Dynamic Task 1: Refresh Multi-Weather Blender. @zara"""
        run_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ [1/3] WEATHER BLEND REFRESH @ %-45s║", run_time.strftime("%H:%M")
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            if hasattr(self.coordinator, "weather_pipeline_manager"):
                pipeline_manager = self.coordinator.weather_pipeline_manager
                _LOGGER.info("  → Fetching fresh weather data from multiple sources...")
                sources_refreshed = await asyncio.wait_for(
                    pipeline_manager._refresh_all_weather_sources(force=True),
                    timeout=60.0,
                )
                if sources_refreshed:
                    pipeline_status = {}
                    if pipeline_manager.weather_expert_blender:
                        pipeline_status = (
                            pipeline_manager.weather_expert_blender.get_blend_stats()
                        )
                    enabled_experts = list(pipeline_status.get("experts_enabled") or [])
                    _LOGGER.info("  ✓ Weather blend SUCCESS!")
                    _LOGGER.info(
                        "    • Experts enabled: %d (%s)",
                        len(enabled_experts),
                        ", ".join(enabled_experts) or "none",
                    )
                    _LOGGER.info(
                        "    • Learned expert weights: %s",
                        (
                            "loaded"
                            if pipeline_status.get("weights_loaded")
                            else "static defaults"
                        ),
                    )
                    _LOGGER.info("    • Corrected forecast and caches refreshed")
                else:
                    _LOGGER.warning("  ✗ Weather blend FAILED - using cached data")
            else:
                _LOGGER.warning("  ✗ Weather pipeline manager not available")
        except asyncio.TimeoutError:
            _LOGGER.warning("  ✗ Weather blend TIMED OUT (60s)")
        except Exception as error:
            _LOGGER.error("  ✗ Weather blend ERROR: %s", error, exc_info=True)
        _LOGGER.info("")

    async def _dynamic_corrected_forecast(self, now: datetime) -> None:
        """Dynamic Task 2: Create corrected weather forecast. @zara"""
        run_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info("║ [2/3] CORRECTED FORECAST @ %-48s║", run_time.strftime("%H:%M"))
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            if hasattr(self.coordinator, "weather_pipeline_manager"):
                pipeline_manager = self.coordinator.weather_pipeline_manager
                if (
                    hasattr(pipeline_manager, "weather_corrector")
                    and pipeline_manager.weather_corrector
                ):
                    _LOGGER.info("  → Applying learned corrections to weather data...")
                    success = await asyncio.wait_for(
                        pipeline_manager.weather_corrector.create_corrected_forecast(),
                        timeout=30.0,
                    )
                    if success:
                        _LOGGER.info("  ✓ Corrected forecast SUCCESS!")
                        _LOGGER.info("    • Temperature corrections applied")
                        _LOGGER.info("    • Solar radiation corrections applied")
                        _LOGGER.info("    • Cloud cover corrections applied")
                        _LOGGER.info("    • Corrected forecast saved to database")
                    else:
                        _LOGGER.warning("  ✗ Corrected forecast creation FAILED")
                else:
                    _LOGGER.warning("  ✗ Weather corrector not available")
            else:
                _LOGGER.warning("  ✗ Weather pipeline manager not available")
        except asyncio.TimeoutError:
            _LOGGER.warning("  ✗ Corrected forecast TIMED OUT (30s)")
        except Exception as error:
            _LOGGER.error("  ✗ Corrected forecast ERROR: %s", error, exc_info=True)
        _LOGGER.info("")

    async def _dynamic_final_forecast(self, now: datetime) -> None:
        """Dynamic Task 3: Execute final morning forecast. @zara"""
        run_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ [3/3] FINAL MORNING FORECAST @ %-44s║", run_time.strftime("%H:%M")
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        _LOGGER.info("  → Executing complete morning routine...")
        try:
            success = await self._execute_morning_routine(
                date=run_time.date().isoformat(),
                forecast_cycle="pre_sunrise",
                trigger_kind="scheduled",
            )
            if success:
                _LOGGER.info("")
                _LOGGER.info("  ✓✓✓ ALL MORNING TASKS COMPLETED SUCCESSFULLY! ✓✓✓")
                _LOGGER.info("")
            else:
                _LOGGER.error("  ✗✗✗ MORNING ROUTINE FAILED - CHECK LOGS ABOVE")
        except Exception as error:
            _LOGGER.error("  ✗ Final forecast ERROR: %s", error, exc_info=True)
        return None if True else None

    async def _execute_morning_routine(
        self, *, date: str, forecast_cycle: str, trigger_kind: str
    ) -> bool:
        """Run one morning pipeline at a time and retry every failure phase."""
        async with self._morning_pipeline_lock:
            for attempt in range(1, 4):
                try:
                    success = await self._execute_morning_routine_once(
                        date=date,
                        forecast_cycle=forecast_cycle,
                        trigger_kind=trigger_kind,
                    )
                except asyncio.CancelledError:
                    self.coordinator.set_health_issue(
                        "morning_routine", "Morning routine was cancelled"
                    )
                    raise
                except Exception as attempt_error:
                    success = False
                    _LOGGER.error(
                        "Morning routine attempt %d/3 failed before persistence: %s",
                        attempt,
                        attempt_error,
                        exc_info=True,
                    )
                if success:
                    self.coordinator.clear_health_issue("morning_routine")
                    self.coordinator.clear_health_issue("database")
                    self.coordinator.update_system_status(
                        event_type="morning_routine",
                        event_status="success",
                        event_summary=f"Morning forecast completed for {date}",
                        event_details={"cycle": forecast_cycle, "attempt": attempt},
                    )
                    return True
                self.coordinator.set_health_issue(
                    "morning_routine",
                    f"Morning forecast unavailable for {date} ({forecast_cycle})",
                )
                self.coordinator.update_system_status(
                    event_type="morning_routine",
                    event_status="partial" if attempt < 3 else "failed",
                    event_summary=f"Morning forecast attempt {attempt}/3 failed for {date}",
                    event_details={"cycle": forecast_cycle, "attempt": attempt},
                )
                await self._refresh_database_health()
                if attempt < 3:
                    await asyncio.sleep(30 * attempt)
            return False
        if True:
            return

    async def _refresh_database_health(self) -> bool:
        """Persist database degradation until a direct probe succeeds."""
        try:
            row = await self.db.fetchone("SELECT 1")
            if row is None or row[0] != 1:
                raise RuntimeError("database probe returned no result")
        except Exception as attempt_error:
            self.coordinator.set_health_issue(
                "database", f"SFML database is not usable: {attempt_error}"
            )
            return False
        self.coordinator.clear_health_issue("database")
        return True

    async def _execute_morning_routine_once(
        self, *, date: str, forecast_cycle: str, trigger_kind: str
    ) -> bool:
        """Execute one complete morning attempt with fresh data."""
        try:
            run_time = dt_util.now()
            full_forecast = await self._generate_full_forecast(
                date, forecast_cycle=forecast_cycle, trigger_kind=trigger_kind
            )
            if not full_forecast:
                _LOGGER.error("Failed to generate forecast for morning routine")
                return False
            hourly_forecast = full_forecast.get("hourly", [])
            if not hourly_forecast:
                _LOGGER.error("No hourly forecast data in result")
                return False
            hourly_weather = await self._get_weather_hourly(date)
            astronomy_data = await self._get_astronomy_data(date)
            sensor_config = self._get_sensor_config()
            success = (
                await self.morning_routine_handler.execute_morning_routine_with_retry(
                    date=date,
                    hourly_forecast=hourly_forecast,
                    weather_hourly=hourly_weather,
                    astronomy_data=astronomy_data,
                    sensor_config=sensor_config,
                    candidate_shadow=full_forecast.get("_candidate_shadow"),
                    forecast_cycle=forecast_cycle,
                    trigger_kind=trigger_kind,
                    max_retries=1,
                )
            )
            if success:
                best_hour = full_forecast.get("best_hour")
                best_hour_kwh = full_forecast.get("best_hour_kwh")
                if best_hour is not None:
                    try:
                        await self.coordinator.data_manager.save_forecast_best_hour(
                            hour=best_hour,
                            prediction_kwh=best_hour_kwh or 0.0,
                            source=full_forecast.get("method", "morning_routine"),
                        )
                        _LOGGER.info(
                            "Best hour saved: %02d:00 (%.3f kWh)",
                            best_hour,
                            best_hour_kwh or 0.0,
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save best hour: %s", error)
                tomorrow_prediction_kwh = full_forecast.get("tomorrow")
                if tomorrow_prediction_kwh is not None:
                    tomorrow_date = run_time + timedelta(days=1)
                    try:
                        await self.coordinator.data_manager.save_forecast_tomorrow(
                            target_date=tomorrow_date,
                            prediction_kwh=tomorrow_prediction_kwh,
                            source=full_forecast.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug(
                            "Tomorrow forecast saved: %.2f kWh", tomorrow_prediction_kwh
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save tomorrow forecast: %s", error)
                day_after_prediction_kwh = full_forecast.get("day_after_tomorrow")
                if day_after_prediction_kwh is not None:
                    day_after_date = run_time + timedelta(days=2)
                    try:
                        await self.coordinator.data_manager.save_forecast_day_after(
                            target_date=day_after_date,
                            prediction_kwh=day_after_prediction_kwh,
                            source=full_forecast.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug(
                            "Day-after forecast saved: %.2f kWh",
                            day_after_prediction_kwh,
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save day-after forecast: %s", error)
            self.coordinator.async_update_listeners()
            return success
        except Exception as error:
            _LOGGER.error("Morning routine execution failed: %s", error, exc_info=True)
            return False

    async def _generate_full_forecast(
        self, date: str, *, forecast_cycle: str, trigger_kind: str
    ) -> Dict[str, Any]:
        "Generate full forecast (today + tomorrow + day_after) using coordinator's forecast services. @zara\n\nReturns:\n    Dict with keys: hourly, today, tomorrow, day_after_tomorrow, method\n"
        try:
            hourly_weather_data = []
            if (
                hasattr(self.coordinator, "weather_service")
                and self.coordinator.weather_service
            ):
                hourly_weather_data = (
                    await self.coordinator.weather_service.get_corrected_hourly_forecast()
                    or []
                )
            if not hourly_weather_data:
                _LOGGER.warning("No weather data available, trying database fallback")
                hourly_weather_data = await self._get_weather_hourly(date)
            if not hourly_weather_data:
                _LOGGER.error("No weather data for hourly forecast generation")
                return {}
            external_sensor_data = {}
            if hasattr(self.coordinator, "sensor_collector"):
                external_sensor_data = (
                    self.coordinator.sensor_collector.collect_all_sensor_data_dict()
                )
            if not hasattr(self.coordinator, "forecast_orchestrator"):
                _LOGGER.error("Forecast orchestrator not available")
                return {}
            full_forecast = (
                await self.coordinator.forecast_orchestrator.orchestrate_forecast(
                    current_weather=None,
                    hourly_forecast=hourly_weather_data,
                    external_sensors=external_sensor_data,
                    correction_factor=getattr(
                        self.coordinator, "learned_correction_factor", 1.0
                    ),
                    forecast_cycle=forecast_cycle,
                    trigger_kind=trigger_kind,
                )
            )
            hourly_forecast = full_forecast.get("hourly", [])
            if not hourly_forecast:
                _LOGGER.error("Forecast orchestrator did not return hourly data")
                return {}
            _LOGGER.info(
                "Generated full forecast for %s: today=%.2f, tomorrow=%.2f, day_after=%.2f kWh",
                date,
                full_forecast.get("today", 0.0),
                full_forecast.get("tomorrow", 0.0),
                full_forecast.get("day_after_tomorrow", 0.0),
            )
            return full_forecast
        except Exception as error:
            _LOGGER.error("Error generating full forecast: %s", error, exc_info=True)
            return {}

    async def _estimate_hourly_from_daily(
        self, date: str, daily_total: float
    ) -> List[Dict[str, Any]]:
        """Estimate hourly distribution from daily total using astronomy curve. @zara"""
        try:
            astronomy_by_hour = await self._get_astronomy_data(date)
            if not astronomy_by_hour:
                _LOGGER.warning("No astronomy data, using flat distribution")
                hourly_forecast_rows = []
                for target_hour in range(6, 21):
                    hourly_forecast_rows.append(
                        {
                            "hour": target_hour,
                            "production_kwh": round(daily_total / 15.0, 4),
                            "method": "flat_estimate",
                            "confidence": 50.0,
                        }
                    )
                return hourly_forecast_rows
            hourly_forecast_rows = []
            elevation_total = 0.0
            elevations_by_hour = {}
            for target_hour in range(5, 22):
                hour_astronomy = astronomy_by_hour.get(str(target_hour), {})
                sun_elevation_deg = hour_astronomy.get("sun_elevation_deg", 0)
                if sun_elevation_deg > 0:
                    elevations_by_hour[target_hour] = sun_elevation_deg
                    elevation_total += sun_elevation_deg
            if elevation_total > 0:
                for target_hour, sun_elevation_deg in elevations_by_hour.items():
                    hour_share = sun_elevation_deg / elevation_total
                    hour_energy_kwh = round(daily_total * hour_share, 4)
                    hourly_forecast_rows.append(
                        {
                            "hour": target_hour,
                            "production_kwh": hour_energy_kwh,
                            "method": "elevation_weighted",
                            "confidence": 60.0,
                        }
                    )
            else:
                for target_hour in range(6, 21):
                    hourly_forecast_rows.append(
                        {
                            "hour": target_hour,
                            "production_kwh": round(daily_total / 15.0, 4),
                            "method": "flat_estimate",
                            "confidence": 50.0,
                        }
                    )
            return sorted(hourly_forecast_rows, key=lambda x: x["hour"])
        except Exception as error:
            _LOGGER.error("Error estimating hourly from daily: %s", error)
            return []

    async def _get_weather_hourly(self, date: str) -> List[Dict[str, Any]]:
        """Get hourly weather forecast from database for 3 days. @zara

        Loads weather data for today, tomorrow, and day_after_tomorrow.
        Keys are normalized to match what forecast_rule_based_strategy expects.
        """
        try:
            from datetime import datetime as datetime_type

            target_date_value = datetime_type.fromisoformat(date).date()
            range_end_date = target_date_value + timedelta(days=2)
            rows = await self.db.fetchall(
                "SELECT forecast_date, hour, temperature, solar_radiation_wm2,\n                          wind, humidity, rain, clouds, pressure,\n                          direct_radiation, diffuse_radiation, weather_code,\n                          correction_snapshot_id\n                   FROM weather_forecast\n                   WHERE forecast_date >= ? AND forecast_date <= ?\n                   ORDER BY forecast_date, hour",
                (target_date_value.isoformat(), range_end_date.isoformat()),
            )
            result = []
            for row in rows:
                date_key = str(row[0])
                target_hour = row[1]
                ghi_wm2 = row[3] or 0.0
                dni_wm2 = row[9] or 0.0
                dhi_wm2 = row[10] or 0.0
                result.append(
                    {
                        "datetime": f"{date_key}T{target_hour:02d}:00:00",
                        "date": date_key,
                        "hour": target_hour,
                        "temperature": row[2],
                        "solar_radiation_wm2": ghi_wm2,
                        "solar_radiation": ghi_wm2,
                        "ghi": ghi_wm2,
                        "direct_radiation": dni_wm2,
                        "diffuse_radiation": dhi_wm2,
                        "wind": row[4],
                        "wind_speed": row[4],
                        "humidity": row[5],
                        "rain": row[6],
                        "precipitation": row[6],
                        "clouds": row[7],
                        "cloud_cover": row[7],
                        "pressure": row[8],
                        "weather_code": row[11],
                        "correction_snapshot_id": row[12],
                    }
                )
            _LOGGER.debug(
                "Loaded %d weather hours from DB (3-day range: %s to %s)",
                len(result),
                target_date_value.isoformat(),
                range_end_date.isoformat(),
            )
            return result
        except Exception as error:
            _LOGGER.error("Error getting weather hourly: %s", error)
            return []

    async def _get_astronomy_data(self, date: str) -> Dict[str, Any]:
        """Get astronomy data from database for 3 days. @zara"""
        try:
            from datetime import datetime as datetime_type

            target_date_value = datetime_type.fromisoformat(date).date()
            range_end_date = target_date_value + timedelta(days=2)
            daily_astronomy_rows = await self.db.fetchall(
                "SELECT cache_date, sunrise, sunset, solar_noon, daylight_hours\n                   FROM astronomy_cache\n                   WHERE cache_date >= ? AND cache_date <= ? AND hour = 12\n                   ORDER BY cache_date",
                (target_date_value.isoformat(), range_end_date.isoformat()),
            )
            if not daily_astronomy_rows:
                return {}
            result = {}
            for row in daily_astronomy_rows:
                date_key = str(row[0])
                result[date_key] = {
                    "sunrise_local": row[1],
                    "sunset_local": row[2],
                    "solar_noon_local": row[3],
                    "daylight_hours": row[4],
                }
            first_astronomy_row = daily_astronomy_rows[0]
            result["sunrise_local"] = first_astronomy_row[1]
            result["sunset_local"] = first_astronomy_row[2]
            result["solar_noon_local"] = first_astronomy_row[3]
            result["daylight_hours"] = first_astronomy_row[4]
            hourly_astronomy_rows = await self.db.fetchall(
                "SELECT cache_date, hour, sun_elevation_deg, sun_azimuth_deg,\n                          clear_sky_radiation_wm2, theoretical_max_kwh\n                   FROM astronomy_cache\n                   WHERE cache_date >= ? AND cache_date <= ?\n                   ORDER BY cache_date, hour",
                (target_date_value.isoformat(), range_end_date.isoformat()),
            )
            today_key = target_date_value.isoformat()
            for astronomy_row in hourly_astronomy_rows:
                date_key = str(astronomy_row[0])
                prediction_id = f"{date_key}_{astronomy_row[1]:02d}"
                hour_astronomy = {
                    "sun_elevation_deg": astronomy_row[2],
                    "sun_azimuth_deg": astronomy_row[3],
                    "clear_sky_radiation_wm2": astronomy_row[4],
                    "theoretical_max_kwh": astronomy_row[5],
                }
                result[prediction_id] = hour_astronomy
                if date_key == today_key:
                    result[str(astronomy_row[1])] = hour_astronomy
            return result
        except Exception as error:
            _LOGGER.error("Error getting astronomy data: %s", error)
            return {}

    def _get_sensor_config(self) -> Dict[str, Any]:
        """Get sensor configuration from coordinator. @zara"""
        config_entry = self.coordinator.config_entry
        return {
            "temp_sensor": config_entry.data.get(CONF_TEMP_SENSOR),
            "humidity_sensor": config_entry.data.get(CONF_HUMIDITY_SENSOR),
            "pressure_sensor": config_entry.data.get(CONF_PRESSURE_SENSOR),
            "wind_sensor": config_entry.data.get(CONF_WIND_SENSOR),
            "rain_sensor": config_entry.data.get(CONF_RAIN_SENSOR),
            "solar_radiation_sensor": config_entry.data.get(
                CONF_SOLAR_RADIATION_SENSOR
            ),
            "lux_sensor": config_entry.data.get(CONF_LUX_SENSOR),
            "has_battery": config_entry.options.get(
                CONF_HAS_BATTERY, DEFAULT_HAS_BATTERY
            ),
            "zero_export_mode": config_entry.options.get(
                CONF_ZERO_EXPORT_MODE, DEFAULT_ZERO_EXPORT_MODE
            ),
            "solar_to_battery_sensor": config_entry.options.get(
                CONF_SOLAR_TO_BATTERY_SENSOR
            ),
            "battery_soc_sensor": config_entry.options.get(CONF_BATTERY_SOC_SENSOR),
            "max_grid_export_w": config_entry.options.get(
                CONF_MAX_GRID_EXPORT_W, DEFAULT_MAX_GRID_EXPORT_W
            ),
        }

    async def schedule_all_tasks(self) -> None:
        """Schedule all recurring tasks. @zara"""
        if self._stop_listener is None:
            self._stop_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._handle_home_assistant_stop
            )
        remove_hourly_listener = async_track_time_change(
            self.hass, self._hourly_update, minute=HOURLY_ACTUAL_UPDATE_MINUTE, second=0
        )
        self._listeners.append(remove_hourly_listener)
        remove_midnight_listener = async_track_time_change(
            self.hass, self._midnight_task, hour=0, minute=10, second=0
        )
        self._listeners.append(remove_midnight_listener)
        remove_astronomy_listener = async_track_time_change(
            self.hass, self._astronomy_cache_refresh, hour=0, minute=20, second=0
        )
        self._listeners.append(remove_astronomy_listener)
        _LOGGER.debug("Astronomy cache refresh scheduled for 00:20 daily")
        remove_early_forecast_listener = async_track_time_change(
            self.hass, self._early_morning_forecast, hour=0, minute=25, second=0
        )
        self._listeners.append(remove_early_forecast_listener)
        _LOGGER.debug("Early morning forecast scheduled for 00:25 daily")
        remove_eod_listener = async_track_time_change(
            self.hass, self._end_of_day_task, hour=23, minute=30, second=0
        )
        self._listeners.append(remove_eod_listener)
        _LOGGER.debug("End-of-day task scheduled for 23:30 daily")
        remove_monthly_listener = async_track_time_change(
            self.hass, self._monthly_report_task, hour=7, minute=30, second=0
        )
        self._listeners.append(remove_monthly_listener)
        _LOGGER.debug("Monthly system report check scheduled daily at 07:30")
        self.hass.async_create_task(self.generate_monthly_report(dt_util.now()))
        remove_operational_listener = async_track_time_change(
            self.hass, self._scheduled_operational_reforecast_tick, second=0
        )
        self._listeners.append(remove_operational_listener)
        _LOGGER.debug(
            "Operational reforecast scheduler armed for minute-based mode checks"
        )
        config_entry = self.coordinator.config_entry
        if config_entry.options.get(
            CONF_ADAPTIVE_FORECAST_MODE, DEFAULT_ADAPTIVE_FORECAST_MODE
        ):
            remove_adaptive_listener = async_track_time_change(
                self.hass,
                self._adaptive_forecast_check,
                hour=ADAPTIVE_CHECK_HOUR,
                minute=ADAPTIVE_CHECK_MINUTE,
                second=0,
            )
            self._listeners.append(remove_adaptive_listener)
            _LOGGER.debug(
                "Adaptive forecast check scheduled at %02d:%02d",
                ADAPTIVE_CHECK_HOUR,
                ADAPTIVE_CHECK_MINUTE,
            )
        await self._schedule_dynamic_tasks_for_today()
        self._schedule_hourly_actual_catchup()
        _LOGGER.debug("============================================================")
        _LOGGER.debug("All scheduled tasks registered successfully:")
        _LOGGER.debug(
            "  • Hourly update: Every hour at :%02d", HOURLY_ACTUAL_UPDATE_MINUTE
        )
        _LOGGER.debug(
            "  • Hourly actual catch-up: Once after startup for missed :%02d slots",
            HOURLY_ACTUAL_UPDATE_MINUTE,
        )
        _LOGGER.debug("  • Midnight task: Daily at 00:10")
        _LOGGER.debug("  • Astronomy cache refresh: Daily at 00:20")
        _LOGGER.debug("  • Early morning forecast: Daily at 00:25")
        _LOGGER.debug("  • End-of-day: Daily at 23:30")
        _LOGGER.debug("  • Monthly report: Daily catch-up check at 07:30")
        _LOGGER.debug("  • Operational reforecast mode checks: Every minute at :00")
        if config_entry.options.get(
            CONF_ADAPTIVE_FORECAST_MODE, DEFAULT_ADAPTIVE_FORECAST_MODE
        ):
            _LOGGER.debug(
                "  • Adaptive forecast: Daily at %02d:%02d",
                ADAPTIVE_CHECK_HOUR,
                ADAPTIVE_CHECK_MINUTE,
            )
        _LOGGER.debug(
            "  • Dynamic morning tasks: Sunrise-based (Weather blend, Corrected forecast, Final forecast)"
        )
        _LOGGER.debug("  Total listeners: %d", len(self._listeners))
        _LOGGER.debug("============================================================")

    @callback
    def _hourly_update(self, now: datetime) -> None:
        """Handle hourly update task. @zara"""
        self.hass.async_create_task(self._execute_hourly_update())

    @callback
    def _scheduled_operational_reforecast_tick(self, now: datetime) -> None:
        """Handle scheduled operational reforecast time checks. @zara"""
        self.hass.async_create_task(self._execute_scheduled_operational_reforecast(now))

    async def _execute_hourly_update(self) -> None:
        """Execute hourly update. @zara"""
        try:
            _LOGGER.debug("Executing hourly update")
            if self._is_shutting_down():
                _LOGGER.info("Hourly update skipped: Home Assistant is stopping")
                return
            run_time = dt_util.now()
            today = run_time.date().isoformat()
            hour = run_time.hour - 1
            if hour < HOURLY_ACTUAL_FIRST_HOUR or hour > HOURLY_ACTUAL_LAST_HOUR:
                return None
            async with self._hourly_actual_lock:
                await self._persist_hourly_actual(today, hour, run_time, live=True)
        except Exception as error:
            _LOGGER.error("Hourly update failed: %s", error, exc_info=True)
        return None if True else None

    def _is_shutting_down(self) -> bool:
        """Return True once Home Assistant has entered its shutdown sequence. @zara"""
        return bool(getattr(self.hass, "is_stopping", False))

    @callback
    def _handle_home_assistant_stop(self, _event: Any) -> None:
        """Release time triggers before the database service closes. @zara"""
        self._stop_listener = None
        self._remove_time_listeners()
        _LOGGER.debug("Scheduled tasks released for Home Assistant shutdown")

    def _schedule_hourly_actual_catchup(self) -> None:
        """Run the missed-hour catch-up once the recorder is fully available. @zara"""

        async def _run(_event: Any = None) -> None:
            self._startup_listener = None
            try:
                await self._catch_up_missed_hourly_actuals()
            except Exception as error:
                _LOGGER.error("Hourly actual catch-up failed: %s", error, exc_info=True)
            return None if True else None

        if self.hass.is_running:
            self.hass.async_create_task(
                _run(), name="solar_forecast_ml_hourly_actual_catchup"
            )
        else:
            self._startup_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _run
            )
        return None if True else None

    @staticmethod
    def _hourly_actual_slot(target_date: str, target_hour: int) -> datetime:
        """Return the local time at which the regular hourly update owns this hour. @zara"""
        hour_start = dt_util.ensure_local(
            datetime.fromisoformat(f"{target_date}T{target_hour:02d}:00:00")
        )
        return hour_start + timedelta(hours=1, minutes=HOURLY_ACTUAL_UPDATE_MINUTE)

    async def _catch_up_missed_hourly_actuals(self) -> int:
        """Persist today's completed hours whose :05 update did not run. @zara

        A restart across the hourly slot leaves actual_kwh NULL although the
        recorder still holds the group power history for that hour.
        """
        if self._is_shutting_down():
            return 0
        current_time = dt_util.now()
        today = current_time.date().isoformat()
        rows = await self.db.fetchall(
            "SELECT target_hour FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour BETWEEN ? AND ?\n                 AND actual_kwh IS NULL\n                 AND COALESCE(manual_override, 0) = 0\n                 AND COALESCE(actual_backfill_blocked, 0) = 0\n               ORDER BY target_hour",
            (today, HOURLY_ACTUAL_FIRST_HOUR, HOURLY_ACTUAL_LAST_HOUR),
        )
        missed_hours = [
            int(row[0])
            for row in rows or []
            if self._hourly_actual_slot(today, int(row[0]))
            + HOURLY_ACTUAL_CATCHUP_GRACE
            <= current_time
        ]
        if not missed_hours:
            return 0
        _LOGGER.info(
            "Hourly actual catch-up for %s: missed hours %s", today, missed_hours
        )
        recovered_count = 0
        for target_hour in missed_hours:
            if self._is_shutting_down():
                break
            try:
                async with self._hourly_actual_lock:
                    row = await self.db.fetchone(
                        "SELECT actual_kwh FROM hourly_predictions\n                           WHERE target_date = ? AND target_hour = ?",
                        (today, target_hour),
                    )
                    if row is None or row[0] is not None:
                        continue
                    if await self._persist_hourly_actual(
                        today, target_hour, dt_util.now(), live=False
                    ):
                        recovered_count += 1
            except Exception as error:
                _LOGGER.error(
                    "Hourly actual catch-up failed for %s_%02d: %s",
                    today,
                    target_hour,
                    error,
                    exc_info=True,
                )
                continue
        _LOGGER.info(
            "Hourly actual catch-up for %s: recovered %d/%d hours",
            today,
            recovered_count,
            len(missed_hours),
        )
        return recovered_count

    async def _get_tracked_weather(
        self, target_date: str, target_hour: int
    ) -> Optional[Dict[str, Any]]:
        """Return the stored weather actual row for the hour, if tracked. @zara"""
        weather_tracker = getattr(
            getattr(self.coordinator, "weather_pipeline_manager", None),
            "weather_actual_tracker",
            None,
        )
        if not weather_tracker:
            return
        return await weather_tracker.get_actual_weather(target_date, target_hour)

    @staticmethod
    def _sensor_data_from_tracked_weather(
        tracked_weather: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Rebuild the hourly sensor snapshot from the stored weather actual row. @zara"""
        if not tracked_weather:
            return {}
        sensor_field_mapping = {
            "temperature_c": "temperature_c",
            "humidity_percent": "humidity_percent",
            "pressure_hpa": "pressure_hpa",
            "wind_speed_ms": "wind_speed_ms",
            "rain_mm": "precipitation_mm",
            "solar_radiation_wm2": "solar_radiation_wm2",
            "lux": "lux",
        }
        return {
            sensor_field: tracked_weather[weather_field]
            for sensor_field, weather_field in sensor_field_mapping.items()
            if tracked_weather.get(weather_field) is not None
        }

    async def _persist_hourly_actual(
        self, target_date: str, target_hour: int, current_time: datetime, *, live: bool
    ) -> bool:
        """Resolve and persist the actual production of one completed hour. @zara

        live=True is the regular :05 timer path with current sensor readings.
        live=False recovers a missed hour after startup from the stored weather
        actual row and skips live-only side effects (Kalman, event reforecast).
        """
        group_resolution = await self.actual_resolver.resolve_group_power_hour(
            target_date=target_date, target_hour=target_hour, current_time=current_time
        )
        usable_group_actuals = (
            group_resolution.source == "group_power_recorder"
            and group_resolution.hour_kwh is not None
            and bool(group_resolution.panel_group_actuals)
        )
        reconstructed_actuals = (
            usable_group_actuals and group_resolution.quality != "exact"
        )
        if not usable_group_actuals:
            self._log_group_power_shadow(
                date=target_date,
                hour=target_hour,
                group_resolution=group_resolution,
                reference_kwh=None,
                reference_label="group_power",
            )
            diagnostics = group_resolution.diagnostics or {}
            _LOGGER.info(
                "Hourly actual update skipped for %s_%02d: group power actuals have no usable values (quality=%s, reason=%s, available_groups=%s/%s, coverage=%.1f%%, max_uncovered=%s, invalid=%d)",
                target_date,
                target_hour,
                group_resolution.quality,
                group_resolution.exclusion_reason or diagnostics.get("reason"),
                diagnostics.get("available_groups", diagnostics.get("exact_groups", 0)),
                diagnostics.get("configured_groups") or diagnostics.get("total_groups"),
                group_resolution.total_power_coverage_percent,
                diagnostics.get("max_uncovered_gap_seconds"),
                group_resolution.invalid_sample_count,
            )
            return False
        hour_energy_kwh = group_resolution.hour_kwh
        _LOGGER.info(
            "Hourly actual %s %02d:00: %.3f kWh (quality=%s, %d groups, %s)",
            target_date,
            target_hour,
            hour_energy_kwh,
            group_resolution.quality,
            len(group_resolution.panel_group_actuals or {}),
            "live" if live else "catch-up",
        )
        self._log_group_power_shadow(
            date=target_date,
            hour=target_hour,
            group_resolution=group_resolution,
            reference_kwh=hour_energy_kwh,
            reference_label="group_power",
        )
        tracked_weather = await self._get_tracked_weather(target_date, target_hour)
        if live:
            sensor_data = await self._collect_sensor_data()
        else:
            sensor_data = self._sensor_data_from_tracked_weather(tracked_weather)
        weather_actual = {
            "temperature_c": sensor_data.get("temperature_c"),
            "humidity_percent": sensor_data.get("humidity_percent"),
            "wind_speed_ms": sensor_data.get("wind_speed_ms"),
            "precipitation_mm": sensor_data.get("rain_mm"),
            "pressure_hpa": sensor_data.get("pressure_hpa"),
            "solar_radiation_wm2": sensor_data.get("solar_radiation_wm2"),
            "lux": sensor_data.get("lux"),
        }
        if tracked_weather:
            weather_actual["cloud_cover_percent"] = tracked_weather.get(
                "cloud_cover_percent"
            )
            weather_actual["frost_detected"] = tracked_weather.get("frost_detected")
            weather_actual["frost_score"] = tracked_weather.get("frost_score")
            weather_actual["frost_confidence"] = tracked_weather.get("frost_confidence")
            weather_actual["snow_covered_panels"] = tracked_weather.get(
                "snow_covered_panels"
            )
            for observation_field in ("solar_radiation_wm2", "lux"):
                tracked_value = tracked_weather.get(observation_field)
                if tracked_value is not None:
                    sensor_data[observation_field] = tracked_value
                    weather_actual[observation_field] = tracked_value
        raw_group_actuals = group_resolution.panel_group_actuals or {}
        panel_group_actuals = {
            group_name: round(float(group_energy_kwh), 4)
            for group_name, group_energy_kwh in raw_group_actuals.items()
            if group_energy_kwh is not None
        }
        configured_group_count = len(
            getattr(self.coordinator, "panel_groups", []) or []
        )
        if (
            configured_group_count > 0
            and len(panel_group_actuals) != configured_group_count
            and (not reconstructed_actuals)
        ):
            _LOGGER.error(
                "Group power actual payload for %s_%02d is incomplete before persistence: groups=%d/%d",
                target_date,
                target_hour,
                len(panel_group_actuals),
                configured_group_count,
            )
            return False
        hour_energy_kwh = round(sum(panel_group_actuals.values()), 4)
        if reconstructed_actuals:
            partial_diagnostics = group_resolution.diagnostics or {}
            log_partial_actuals = (
                _LOGGER.warning
                if hour_energy_kwh >= HOURLY_ACTUAL_PARTIAL_WARN_MIN_KWH
                else _LOGGER.info
            )
            log_partial_actuals(
                "Hourly actual persistence payload for %s_%02d uses partial group power actuals and is excluded from learning: total=sum(available_groups)=%.4f kWh, groups=%d/%d, reason=%s, partial=%s",
                target_date,
                target_hour,
                hour_energy_kwh,
                len(panel_group_actuals),
                configured_group_count,
                group_resolution.exclusion_reason,
                ", ".join(partial_diagnostics.get("partial_reasons") or []) or "n/a",
            )
        else:
            _LOGGER.debug(
                "Hourly actual persistence payload for %s_%02d uses group power actuals: total=sum(groups)=%.4f kWh, groups=%s",
                target_date,
                target_hour,
                hour_energy_kwh,
                ", ".join(
                    (
                        f"{group_name}={group_energy_kwh:.4f}"
                        for group_name, group_energy_kwh in panel_group_actuals.items()
                    )
                ),
            )
        group_exclusion_reason = (
            group_resolution.exclusion_reason if panel_group_actuals else None
        )
        sensor_config = self._get_sensor_config()
        update_succeeded = await self.morning_routine_handler.update_hourly_actual(
            date=target_date,
            hour=target_hour,
            actual_kwh=hour_energy_kwh,
            sensor_data=sensor_data,
            weather_actual=weather_actual,
            panel_group_actuals=panel_group_actuals,
            inverter_max_power=self.coordinator.inverter_max_power or 0.0,
            zero_export_mode=sensor_config.get("zero_export_mode", False),
            has_battery=sensor_config.get("has_battery", False),
            solar_to_battery_power=sensor_data.get("solar_to_battery_power"),
            battery_soc=sensor_data.get("battery_soc"),
            max_grid_export_w=sensor_config.get("max_grid_export_w"),
            panel_group_exclusion_reason=group_exclusion_reason,
            is_reconstructed=reconstructed_actuals,
            reconstructed_exclude_from_learning=True,
        )
        if update_succeeded:
            self.coordinator.async_update_listeners()
            if live:
                await self._maybe_trigger_weather_event_reforecast(
                    target_date, target_hour
                )
        if live and self._kalman_tracker:
            if 7 <= target_hour <= 19 and self._has_available_local_solar_observation():
                actual_radiation_wm2 = sensor_data.get("solar_radiation_wm2")
                if actual_radiation_wm2 is not None:
                    try:
                        forecast_radiation_anchor = (
                            await self._get_kalman_update_anchor(
                                target_date, target_hour
                            )
                        )
                        if forecast_radiation_anchor is not None:
                            radiation_residual_wm2 = (
                                float(actual_radiation_wm2) - forecast_radiation_anchor
                            )
                            self._kalman_tracker.update(radiation_residual_wm2)
                    except Exception as error:
                        _LOGGER.debug("Kalman update skipped: %s", error)
        return bool(update_succeeded)

    async def _get_kalman_update_anchor(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Return x-free P0 from the current correction snapshot, or skip."""
        try:
            row = await self.db.fetchone(
                "SELECT c.contract_version, c.final_used_ghi, c.kalman_bias\n                     FROM weather_forecast wf\n                     JOIN weather_forecast_correction_snapshots c\n                       ON c.correction_snapshot_id = wf.correction_snapshot_id\n                    WHERE wf.forecast_date = ? AND wf.hour = ?",
                (date_str, hour),
            )
            if not row or row[1] is None:
                return None
            if row[0] != WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION:
                return
            return float(row[1]) - float(row[2] or 0.0)
        except Exception:
            return

    async def _train_weather_mlp(self, current_time) -> Dict[str, str]:
        """Train Weather MLP on historical precision data. @zara"""

        def skipped(reason: str, reason_code: str) -> Dict[str, str]:
            _LOGGER.info("Weather MLP training skipped [%s]: %s", reason_code, reason)
            return {
                "status": "skipped",
                "reason": reason,
                "reason_code": reason_code,
                "impact": "informational",
            }

        def failed(reason: str, reason_code: str) -> Dict[str, str]:
            _LOGGER.error("Weather MLP training failed [%s]: %s", reason_code, reason)
            return {"status": "failed", "reason": reason, "reason_code": reason_code}

        if self._weather_mlp is None:
            reason = "Weather MLP is not configured"
            return skipped(reason, "weather_mlp_not_configured")
        if not self._has_available_local_solar_observation():
            self._attach_mlp_to_correctors()
            reason = "no currently usable local solar-radiation sensor is configured; provider-only forecasts stay neutral"
            return skipped(reason, "weather_mlp_local_sensor_unavailable")
        try:
            training_date = current_time.date().isoformat()
            training_contract_version = WEATHER_MLP_TRAINING_CONTRACT_VERSION
            correction_contract_version = WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
            funnel_row = await self.db.fetchone(
                "WITH precision_samples AS (\n                       SELECT wpd.id, wpd.date, wpd.hour, wpd.solar_forecast,\n                              wpd.solar_actual,\n                              CASE WHEN c.correction_snapshot_id IS NOT NULL\n                                     AND c.clouds IS NOT NULL\n                                     AND c.humidity IS NOT NULL\n                                     AND c.pressure IS NOT NULL\n                                     AND c.wind IS NOT NULL\n                                   THEN 1 ELSE 0 END AS has_weather,\n                              CASE WHEN c.sun_elevation IS NOT NULL\n                                   THEN 1 ELSE 0 END AS has_astronomy,\n                              COALESCE(c.sun_elevation, -90.0) AS sun_elevation_deg,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND COALESCE(hp.exclude_from_learning, 0) != 0\n                              ) AS learning_excluded,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND COALESCE(hp.mppt_throttled, 0) != 0\n                              ) AS mppt_excluded,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND (COALESCE(hp.exclude_from_learning, 0) != 0\n                                         OR COALESCE(hp.mppt_throttled, 0) != 0\n                                         OR COALESCE(hp.inverter_clipped, 0) != 0)\n                              ) AS governance_excluded\n                       FROM weather_precision_daily wpd\n                       LEFT JOIN weather_forecast_usage_snapshots u\n                         ON u.usage_snapshot_id = wpd.usage_snapshot_id\n                       LEFT JOIN weather_forecast_correction_snapshots c\n                         ON c.correction_snapshot_id = u.correction_snapshot_id\n                       WHERE wpd.forecast_contract_version = ?\n                         AND COALESCE(wpd.solar_sample_eligible, 0) = 1\n                   )\n                   SELECT\n                       (SELECT COUNT(*)\n                        FROM hourly_weather_actual\n                        WHERE solar_radiation_wm2 IS NOT NULL) AS measured_all_time,\n                       (SELECT COUNT(*)\n                        FROM hourly_weather_actual\n                        WHERE date >= date(?, '-30 days')\n                          AND solar_radiation_wm2 IS NOT NULL) AS measured_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL THEN 1 ELSE 0\n                       END) AS precision_radiation_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                           THEN 1 ELSE 0\n                       END) AS weather_joined_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_astronomy = 1\n                           THEN 1 ELSE 0\n                       END) AS astronomy_joined_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                           THEN 1 ELSE 0\n                       END) AS physical_candidates_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.learning_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS learning_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.mppt_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS mppt_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.governance_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS governance_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.governance_excluded = 0\n                           THEN 1 ELSE 0\n                       END) AS eligible_30d\n                   FROM precision_samples ps",
                (correction_contract_version,) + (training_date,) * 9,
            )
            (
                measured_all_time,
                measured_30d,
                precision_radiation_30d,
                weather_joined_30d,
                astronomy_joined_30d,
                physical_candidates_30d,
                learning_excluded_30d,
                mppt_excluded_30d,
                governance_excluded_30d,
                eligible_30d,
            ) = (
                (int(count_value or 0) for count_value in funnel_row)
                if funnel_row
                else (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            )
            if measured_all_time == 0:
                return skipped(
                    "no physically measured solar-radiation observations are available; Weather MLP requires W/m² sensor data",
                    "weather_mlp_measured_radiation_unavailable",
                )
            if measured_30d == 0:
                return skipped(
                    "measured solar-radiation observations exist only outside the 30-day training window",
                    "weather_mlp_training_window_expired",
                )
            if measured_30d >= 50 and precision_radiation_30d == 0:
                provenance_row = await self.db.fetchone(
                    "SELECT\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND COALESCE(forecast_contract_version, '') != ?),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND COALESCE(solar_sample_eligible, 0) = 0\n                               AND solar_exclusion_reason IS NULL),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND COALESCE(solar_sample_eligible, 0) = 1\n                               AND solar_actual IS NULL),\n                           (SELECT COUNT(*)\n                              FROM hourly_weather_actual a\n                              JOIN weather_forecast_usage_snapshots u\n                                ON u.target_date = a.date\n                               AND u.target_hour = a.hour\n                               AND u.contract_version = ?\n                              JOIN weather_forecast_correction_snapshots c\n                                ON c.correction_snapshot_id = u.correction_snapshot_id\n                               AND c.contract_version = ?\n                             WHERE a.date >= date(?, '-30 days')\n                               AND a.solar_radiation_wm2 IS NOT NULL),\n                           (SELECT COUNT(*)\n                              FROM hourly_weather_actual a\n                              JOIN weather_forecast_usage_snapshots u\n                                ON u.target_date = a.date\n                               AND u.target_hour = a.hour\n                               AND u.contract_version = ?\n                              JOIN weather_forecast_correction_snapshots c\n                                ON c.correction_snapshot_id = u.correction_snapshot_id\n                               AND c.contract_version = ?\n                              LEFT JOIN weather_precision_daily wpd\n                                ON wpd.date = a.date\n                               AND wpd.hour = a.hour\n                               AND wpd.forecast_contract_version = ?\n                             WHERE a.date >= date(?, '-30 days')\n                               AND a.solar_radiation_wm2 IS NOT NULL\n                               AND wpd.id IS NULL),\n                           (SELECT solar_exclusion_reason FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND solar_exclusion_reason IS NOT NULL\n                             GROUP BY solar_exclusion_reason\n                             ORDER BY COUNT(*) DESC LIMIT 1)",
                    (
                        training_date,
                        correction_contract_version,
                        training_date,
                        correction_contract_version,
                        training_date,
                        correction_contract_version,
                        training_date,
                        correction_contract_version,
                        WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                        correction_contract_version,
                        training_date,
                        WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                        correction_contract_version,
                        correction_contract_version,
                        training_date,
                        training_date,
                        correction_contract_version,
                    ),
                )
                (
                    current_precision_rows,
                    legacy_precision_rows,
                    unexplained_ineligible_rows,
                    eligible_without_actual_rows,
                    frozen_measured_hours,
                    missing_precision_rows,
                ) = (
                    tuple((int(count_value or 0) for count_value in provenance_row[:6]))
                    if provenance_row
                    else (0, 0, 0, 0, 0, 0)
                )
                dominant_exclusion_reason = (
                    provenance_row[6] if provenance_row else None
                ) or "unrecorded"
                if frozen_measured_hours == 0:
                    return skipped(
                        f"{measured_30d} measured W/m² observations exist, but no frozen morning weather provenance is available for those hours yet (legacy precision rows in window: {legacy_precision_rows})",
                        "weather_mlp_precision_samples_unavailable",
                    )
                if missing_precision_rows > 0:
                    return failed(
                        f"Weather MLP pipeline invariant failed: {missing_precision_rows}/{frozen_measured_hours} measured hours with frozen snapshots produced no weather precision rows",
                        "weather_mlp_precision_pipeline_invariant",
                    )
                if (
                    unexplained_ineligible_rows == 0
                    and eligible_without_actual_rows == 0
                ):
                    return skipped(
                        f"{current_precision_rows} weather precision rows in the training window carry a recorded exclusion reason (most common: {dominant_exclusion_reason})",
                        "weather_mlp_precision_samples_excluded",
                    )
                return failed(
                    f"Weather MLP pipeline invariant failed: {measured_30d} measured W/m² observations produced no weather precision samples ({unexplained_ineligible_rows} rows without exclusion reason, {eligible_without_actual_rows} eligible rows without measured GHI)",
                    "weather_mlp_precision_pipeline_invariant",
                )
            if precision_radiation_30d >= 50 and weather_joined_30d < 50:
                return failed(
                    f"Weather MLP pipeline invariant failed: only {weather_joined_30d}/{precision_radiation_30d} precision samples have frozen weather snapshots",
                    "weather_mlp_missing_weather_join",
                )
            if precision_radiation_30d >= 50 and astronomy_joined_30d < 50:
                return failed(
                    f"Weather MLP pipeline invariant failed: only {astronomy_joined_30d}/{precision_radiation_30d} precision samples have astronomy rows",
                    "weather_mlp_missing_astronomy_join",
                )
            if physical_candidates_30d >= 50 and eligible_30d < 50:
                unexplained_funnel_loss = (
                    physical_candidates_30d - eligible_30d - governance_excluded_30d
                )
                if unexplained_funnel_loss != 0:
                    return failed(
                        f"Weather MLP sample funnel lost {physical_candidates_30d - eligible_30d} samples, but only {governance_excluded_30d} have explicit learning, MPPT, or clipping flags",
                        "weather_mlp_sample_funnel_invariant",
                    )
                return skipped(
                    f"Weather MLP governance filter retained {eligible_30d}/{physical_candidates_30d} samples (learning={learning_excluded_30d}, mppt={mppt_excluded_30d}, unique_excluded={governance_excluded_30d})",
                    "weather_mlp_governance_filtered",
                )
            if eligible_30d < 50:
                return skipped(
                    f"Weather MLP warm-up: {eligible_30d}/50 valid measured-radiation samples",
                    "weather_mlp_warmup",
                )
            rows = await self.db.fetchall(
                "SELECT wpd.date, wpd.hour, wpd.solar_forecast, wpd.solar_actual, wpd.solar_factor,\n                      c.clouds, c.humidity, c.pressure, c.wind,\n                      c.sun_elevation, c.rain,\n                      c.cloud_cover_low, c.cloud_cover_mid, c.cloud_cover_high,\n                      c.visibility_m, c.static_solar_factor_applied\n               FROM weather_precision_daily wpd\n               JOIN weather_forecast_usage_snapshots u\n                    ON u.usage_snapshot_id = wpd.usage_snapshot_id\n               JOIN weather_forecast_correction_snapshots c\n                    ON c.correction_snapshot_id = u.correction_snapshot_id\n               WHERE wpd.solar_forecast > 10\n                 AND wpd.solar_actual IS NOT NULL\n                 AND c.sun_elevation > 5\n                 AND c.clouds IS NOT NULL\n                 AND c.humidity IS NOT NULL\n                 AND c.pressure IS NOT NULL\n                 AND c.wind IS NOT NULL\n                 AND wpd.forecast_contract_version = ?\n                 AND COALESCE(wpd.solar_sample_eligible, 0) = 1\n                 AND wpd.date >= date(?, '-30 days')\n                 AND NOT EXISTS (\n                     SELECT 1 FROM hourly_predictions hp\n                     WHERE hp.target_date = wpd.date\n                       AND hp.target_hour = wpd.hour\n                       AND (COALESCE(hp.exclude_from_learning, 0) != 0\n                            OR COALESCE(hp.mppt_throttled, 0) != 0\n                            OR COALESCE(hp.inverter_clipped, 0) != 0)\n                 )\n               ORDER BY wpd.date, wpd.hour",
                (correction_contract_version, training_date),
            )
            if len(rows or []) != eligible_30d:
                return failed(
                    f"Weather MLP sample funnel counted {eligible_30d} eligible samples but the loader returned {(len(rows) if rows else 0)}",
                    "weather_mlp_sample_funnel_invariant",
                )
            training_features = []
            training_targets = []
            training_dates = []
            for row in rows:
                (
                    date_key,
                    target_hour,
                    forecast_ghi_wm2,
                    measured_ghi_wm2,
                    solar_factor,
                    cloud_cover_percent,
                    humidity_percent,
                    pressure_hpa,
                    wind_speed_ms,
                    sun_elevation_deg,
                    rain_mm,
                    low_cloud_cover_percent,
                    mid_cloud_cover_percent,
                    high_cloud_cover_percent,
                    visibility_m,
                ) = row[:15]
                static_factor_applied = row[15] if len(row) > 15 else None
                residual_target = residual_weather_mlp_target(
                    solar_factor, static_factor_applied
                )
                if not residual_target is not None:
                    continue
                features = build_weather_mlp_features(
                    raw_ghi=forecast_ghi_wm2,
                    cloud_cover=cloud_cover_percent,
                    sun_elevation=sun_elevation_deg,
                    humidity=humidity_percent,
                    pressure=pressure_hpa,
                    wind=wind_speed_ms,
                    hour=target_hour,
                    rain=rain_mm,
                    cloud_low=low_cloud_cover_percent,
                    cloud_mid=mid_cloud_cover_percent,
                    cloud_high=high_cloud_cover_percent,
                )
                if not features is not None:
                    continue
                training_features.append(features)
                training_targets.append(residual_target)
                training_dates.append(str(date_key))
            if len(training_features) < 50:
                return failed(
                    f"Weather MLP target validation retained {len(training_features)}/{eligible_30d} eligible samples; {eligible_30d - len(training_features)} solar-factor targets were invalid",
                    "weather_mlp_target_validation_invariant",
                )
            held_out_shortfall = held_out_training_shortfall(training_dates)
            if held_out_shortfall is not None:
                return skipped(
                    f"Weather MLP warm-up: the leanest held-out fold trains on {held_out_shortfall}/{WEATHER_MLP_MIN_TRAINING_SAMPLES} samples ({len(training_features)} samples over {len(set(training_dates))} days)",
                    "weather_mlp_warmup",
                )
            _LOGGER.info(
                "Weather MLP training: %d samples over %d days, %d features",
                len(training_features),
                len(set(training_dates)),
                WEATHER_MLP_FEATURE_COUNT,
            )
            metrics = await self._evaluate_weather_mlp_pooled(
                training_features, training_targets, training_dates
            )
            if metrics is None:
                return skipped(
                    f"Weather MLP needs at least two training days for a held-out verdict ({len(set(training_dates))} available)",
                    "weather_mlp_insufficient_days",
                )
            rejection_reason = weather_mlp_persist_verdict(metrics)
            if rejection_reason is not None:
                reason = f"Weather MLP {rejection_reason}"
                log_outcome = (
                    _LOGGER.info
                    if self._weather_mlp_retrain_rejected
                    else _LOGGER.warning
                )
                log_outcome(
                    "%s; weights not persisted, serving state unchanged (pooled RMSE %.4f vs static %.4f over %d held-out samples)",
                    reason,
                    metrics["rmse"],
                    metrics["baseline_rmse"],
                    metrics["samples"],
                )
                self._weather_mlp_retrain_rejected = True
                return {
                    "status": "skipped",
                    "reason": reason,
                    "reason_code": "weather_mlp_governance_filtered",
                    "impact": "informational",
                }
            trained_model = self._spawn_weather_mlp()
            training_result = await trained_model.train(
                X_data=training_features,
                y_targets=training_targets,
                epochs=50,
                validation_split=0.2,
                early_stopping_patience=10,
            )
            if not training_result.get("success"):
                reason = (
                    training_result.get("error_message")
                    or "trainer reported unsuccessful completion"
                )
                _LOGGER.warning("Weather MLP training failed: %s", reason)
                return {"status": "failed", "reason": reason}
            weights = trained_model.get_weights()
            weights["accuracy"] = float(metrics["r2"])
            weights["rmse"] = float(metrics["rmse"])
            weights["training_samples"] = int(
                training_result.get("training_samples") or len(training_features)
            )
            await self.db.save_weather_mlp_weights(
                weights, training_contract_version=training_contract_version
            )
            self._weather_mlp.set_weights(weights)
            self._weather_mlp_retrain_rejected = False
            self._weather_mlp_weights_loaded = True
            self._attach_mlp_to_correctors()
            _LOGGER.info(
                "Weather MLP trained and saved: pooled R²=%.3f, skill vs static=%+.3f, RMSE=%.4f (static %.4f), %d samples over %d days",
                metrics["r2"],
                metrics["skill"],
                metrics["rmse"],
                metrics["baseline_rmse"],
                weights["training_samples"],
                len(set(training_dates)),
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error("Weather MLP training error: %s", error, exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _collect_sensor_data(self) -> Dict[str, Any]:
        """Collect current sensor data. @zara"""
        sensor_data = {}
        sensor_config = self._get_sensor_config()
        sensor_fields = {
            "temp_sensor": "temperature_c",
            "humidity_sensor": "humidity_percent",
            "pressure_sensor": "pressure_hpa",
            "wind_sensor": "wind_speed_ms",
            "rain_sensor": "rain_mm",
            "solar_radiation_sensor": "solar_radiation_wm2",
            "lux_sensor": "lux",
        }
        for config_key, entity_id in sensor_config.items():
            if config_key not in sensor_fields:
                continue
            if entity_id and isinstance(entity_id, str):
                state = self.hass.states.get(entity_id)
                if state and state.state not in (None, "unknown", "unavailable"):
                    try:
                        sensor_field = sensor_fields[config_key]
                        sensor_data[sensor_field] = float(state.state)
                    except (ValueError, TypeError):
                        pass
        battery_flow_entity_id = sensor_config.get("solar_to_battery_sensor")
        if battery_flow_entity_id and isinstance(battery_flow_entity_id, str):
            state = self.hass.states.get(battery_flow_entity_id)
            if state and state.state not in (None, "unknown", "unavailable"):
                try:
                    sensor_data["solar_to_battery_power"] = float(state.state)
                except (ValueError, TypeError):
                    pass
        battery_soc_entity_id = sensor_config.get("battery_soc_sensor")
        if battery_soc_entity_id and isinstance(battery_soc_entity_id, str):
            state = self.hass.states.get(battery_soc_entity_id)
            if state and state.state not in (None, "unknown", "unavailable"):
                try:
                    sensor_data["battery_soc"] = float(state.state)
                except (ValueError, TypeError):
                    pass
        if True:
            return sensor_data

    @callback
    def _midnight_task(self, now: datetime) -> None:
        """Handle midnight task. @zara"""
        self.hass.async_create_task(self._execute_midnight_task())

    async def _execute_midnight_task(self) -> None:
        """Execute midnight task. @zara"""
        try:
            _LOGGER.info("Executing midnight task")
            run_time = dt_util.now()
            today = run_time.date().isoformat()
            production_time_calculator = getattr(
                self.coordinator, "production_time_calculator", None
            )
            reset_daily_peak = getattr(
                production_time_calculator, "reset_daily_peak", None
            )
            if callable(reset_daily_peak):
                await reset_daily_peak(run_time)
            else:
                self.coordinator._peak_power_today = 0.0
                self.coordinator._peak_time_today = None
                self.coordinator._peak_date_today = today
                self.coordinator.async_update_listeners()
            _LOGGER.debug("Reset daily peak tracking")
            await self.coordinator.reset_expected_daily_production()
            await self._schedule_dynamic_tasks_for_today()
            if self._kalman_tracker:
                self._kalman_tracker.reset()
        except Exception as error:
            _LOGGER.error("Midnight task failed: %s", error, exc_info=True)
        return None if True else None

    @callback
    def _astronomy_cache_refresh(self, now: datetime) -> None:
        """Handle astronomy cache refresh at 00:20. @zara V16.1"""
        _LOGGER.info(
            "Astronomy cache refresh triggered at %s",
            now.strftime("%H:%M:%S") if now else "unknown",
        )
        if self._astronomy_refresh_task is not None and (
            not self._astronomy_refresh_task.done()
        ):
            _LOGGER.info("Astronomy cache refresh is already running")
            return
        self._astronomy_refresh_task = self.hass.async_create_task(
            self._execute_astronomy_cache_refresh()
        )

    async def _execute_astronomy_cache_refresh(self) -> bool:
        """Execute astronomy cache refresh for today + 7 days. @zara V16.1"""
        try:
            from ..astronomy.astronomy_cache import (
                AstronomyCache as astronomy_cache_type,
            )
            from ..astronomy.astronomy_cache_manager import (
                get_cache_manager as cache_manager_factory,
            )

            _LOGGER.info("Refreshing astronomy cache for today + 7 days...")
            db_manager = self.coordinator.data_manager._db_manager
            astronomy_cache = astronomy_cache_type(db_manager=db_manager)
            latitude = self.hass.config.latitude
            longitude = self.hass.config.longitude
            timezone_name = str(self.hass.config.time_zone)
            elevation_m = self.hass.config.elevation or 0
            astronomy_cache.initialize_location(
                latitude, longitude, timezone_name, elevation_m
            )
            panel_groups = getattr(self.coordinator, "panel_groups", [])
            if panel_groups:
                astronomy_cache.set_panel_groups(panel_groups)
                _LOGGER.debug(
                    "Panel groups set for astronomy refresh: %d groups",
                    len(panel_groups),
                )
            solar_capacity_kwp = self.coordinator.solar_capacity
            if not solar_capacity_kwp:
                _LOGGER.warning(
                    "Solar capacity not configured, using 5.0 kWp for astronomy refresh"
                )
                solar_capacity_kwp = 5.0
            from datetime import date as date_type

            result = await astronomy_cache.rebuild_cache(
                system_capacity_kwp=solar_capacity_kwp,
                start_date=date_type.today(),
                days_back=0,
                days_ahead=0,
            )
            success_count = int(result.get("success_count", 0))
            error_count = int(result.get("error_count", 0))
            for day_offset in range(1, 8):
                cache_date = date_type.today() + timedelta(days=day_offset)
                if await astronomy_cache.build_cache_for_date(
                    cache_date, solar_capacity_kwp
                ):
                    success_count += 1
                else:
                    error_count += 1
            _LOGGER.info(
                "Astronomy cache refreshed: %d days updated, %d errors",
                success_count,
                error_count,
            )
            cache_manager = cache_manager_factory(db_manager=db_manager)
            await cache_manager.initialize()
            if error_count:
                self.coordinator.set_health_issue(
                    "astronomy_refresh",
                    f"Astronomy refresh incomplete: {error_count} day(s) failed",
                )
                return False
            self.coordinator.clear_health_issue("astronomy_refresh")
        except Exception as error:
            _LOGGER.error("Astronomy cache refresh failed: %s", error, exc_info=True)
            self.coordinator.set_health_issue(
                "astronomy_refresh", f"Astronomy refresh failed: {error}"
            )
            return False
        return True

    @callback
    def _early_morning_forecast(self, now: datetime) -> None:
        """Handle early morning forecast task at 00:25. @zara"""
        _LOGGER.info(
            "=== EARLY MORNING FORECAST SCHEDULER TRIGGERED at %s ===",
            now.strftime("%H:%M:%S") if now else "unknown",
        )
        _LOGGER.info("Creating early morning forecast task...")
        self.hass.async_create_task(self._execute_early_morning_forecast(now))

    async def _execute_early_morning_forecast(self, scheduled_at: datetime) -> None:
        """Execute the initial morning forecast scheduled at 00:25. @zara"""
        _LOGGER.info(
            ">>> STARTING EARLY MORNING FORECAST (00:25 scheduled execution) <<<"
        )
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ EARLY MORNING FORECAST @ 00:25 - Initial daily forecast            ║"
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            if self._astronomy_refresh_task is not None and (
                not self._astronomy_refresh_task.done()
            ):
                _LOGGER.info(
                    "Waiting for the 00:20 astronomy refresh before morning forecast"
                )
                astronomy_refreshed = await asyncio.shield(self._astronomy_refresh_task)
                if not astronomy_refreshed:
                    _LOGGER.warning(
                        "Astronomy refresh was incomplete; continuing with existing cache fallback"
                    )
            success = await self._execute_morning_routine(
                date=scheduled_at.date().isoformat(),
                forecast_cycle="post_midnight",
                trigger_kind="scheduled",
            )
            if success:
                _LOGGER.info("  ✓✓✓ EARLY MORNING FORECAST COMPLETED SUCCESSFULLY! ✓✓✓")
                _LOGGER.info("")
            else:
                _LOGGER.error("  ✗✗✗ EARLY MORNING FORECAST FAILED - CHECK LOGS ABOVE")
        except Exception as error:
            _LOGGER.error("  ✗ Early morning forecast ERROR: %s", error, exc_info=True)
        return None if True else None

    @callback
    def _end_of_day_task(self, now: datetime) -> None:
        """Handle end-of-day task. @zara"""
        _LOGGER.info(
            "=== EOD SCHEDULER TRIGGERED at %s ===",
            now.strftime("%H:%M:%S") if now else "unknown",
        )
        if self._end_of_day_running:
            _LOGGER.warning("End-of-day task already running, skipping")
            return
        _LOGGER.info("Creating EOD workflow task...")
        self.hass.async_create_task(self._execute_end_of_day_task())

    async def end_of_day_workflow(self, now: datetime) -> None:
        """Consolidated End-of-Day Workflow at 23:30 - All tasks in sequence. @zara"""
        run_time = now if now is not None else dt_util.now()
        if self._end_of_day_running:
            _LOGGER.debug("END_OF_DAY_WORKFLOW already running - skipping")
            return
        self._end_of_day_running = True
        if self.coordinator:
            self.coordinator.start_workflow_status(
                "eod", current_step="Preparing end-of-day workflow", total_steps=16
            )
        try:
            await self._end_of_day_workflow_internal(run_time)
        except Exception as eod_error:
            if self.coordinator:
                self.coordinator.finish_workflow_status(
                    status="error",
                    current_step="End-of-day workflow failed",
                    last_error=str(eod_error),
                )
            raise
        finally:
            self._end_of_day_running = False

    async def _eod_completed_at(self, day: str) -> Optional[str]:
        """Return the stored completion time when this date already finished."""
        row = await self.db.fetchone(
            "SELECT eod_completed_at FROM daily_summaries\n               WHERE date = ? AND eod_completed_at IS NOT NULL",
            (day,),
        )
        if not row or not row[0]:
            return None
        return str(row[0])

    async def _execute_end_of_day_task(self) -> None:
        """Execute end-of-day processing (entry point from scheduled task). @zara"""
        current_time = dt_util.now()
        eod_date = current_time.date().isoformat()
        eod_completed_at = await self._eod_completed_at(eod_date)
        if eod_completed_at:
            _LOGGER.info(
                "EOD for %s already completed at %s, scheduled run skipped",
                eod_date,
                eod_completed_at,
            )
            return
        _LOGGER.info(">>> STARTING EOD WORKFLOW (scheduled execution) <<<")
        await self.end_of_day_workflow(current_time)

    async def _store_tfs_comparison(self, current_time: datetime) -> None:
        today = current_time.date().isoformat()
        evaluation_query = f"\n            SELECT SUM(tfs_kwh), SUM(prediction_kwh), SUM(actual_kwh)\n              FROM hourly_predictions hp\n             WHERE target_date = ?\n               AND actual_kwh IS NOT NULL\n               {CLEAN_EVALUATION_ELIGIBILITY_SQL}\n        "
        energy_query = "\n            SELECT SUM(tfs_kwh), SUM(prediction_kwh), SUM(actual_kwh)\n              FROM hourly_predictions\n             WHERE target_date = ?\n               AND actual_kwh IS NOT NULL\n        "
        evaluation_row = await self.db.fetchone(evaluation_query, (today,))
        energy_row = await self.db.fetchone(energy_query, (today,))
        tfs_evaluation_kwh = (
            evaluation_row[0]
            if evaluation_row and evaluation_row[0] is not None
            else None
        )
        if tfs_evaluation_kwh is None:
            return
        actual_kwh = float(evaluation_row[2] or 0.0)
        if actual_kwh <= 0:
            return
        sfml_evaluation_kwh = float(evaluation_row[1] or 0.0)
        sfml_accuracy_percent = max(
            0.0, (1.0 - abs(sfml_evaluation_kwh - actual_kwh) / actual_kwh) * 100
        )
        tfs_accuracy_percent = max(
            0.0, (1.0 - abs(float(tfs_evaluation_kwh) - actual_kwh) / actual_kwh) * 100
        )
        best_source = "tfs" if tfs_accuracy_percent > sfml_accuracy_percent else "sfml"
        total_actual_kwh = float(energy_row[2] or 0.0) if energy_row else 0.0
        total_sfml_kwh = float(energy_row[1] or 0.0) if energy_row else 0.0
        total_tfs_kwh = float(energy_row[0] or 0.0) if energy_row else 0.0
        await self.db.execute(
            "INSERT INTO sfml_tfs_forecast_comparison\n               (date, actual_kwh, sfml_forecast_kwh, sfml_accuracy_percent,\n                tfs_forecast_kwh, tfs_accuracy_percent, best_source, created_at)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(date) DO UPDATE SET\n                   actual_kwh=excluded.actual_kwh,\n                   sfml_forecast_kwh=excluded.sfml_forecast_kwh,\n                   sfml_accuracy_percent=excluded.sfml_accuracy_percent,\n                   tfs_forecast_kwh=excluded.tfs_forecast_kwh,\n                   tfs_accuracy_percent=excluded.tfs_accuracy_percent,\n                   best_source=excluded.best_source,\n                   updated_at=CURRENT_TIMESTAMP",
            (
                today,
                actual_kwh,
                sfml_evaluation_kwh,
                round(sfml_accuracy_percent, 1),
                tfs_evaluation_kwh,
                round(tfs_accuracy_percent, 1),
                best_source,
                dt_util.now(),
            ),
        )
        _LOGGER.info(
            "TFS vs SFML: production_quality TFS=%.2f kWh (%.1f%%), SFML=%.2f kWh (%.1f%%), IST=%.2f kWh → best=%s; energy_balance TFS=%.2f SFML=%.2f IST=%.2f",
            tfs_evaluation_kwh,
            tfs_accuracy_percent,
            sfml_evaluation_kwh,
            sfml_accuracy_percent,
            actual_kwh,
            best_source,
            total_tfs_kwh,
            total_sfml_kwh,
            total_actual_kwh,
        )

    async def _end_of_day_workflow_internal(self, current_time: datetime) -> None:
        """Internal implementation of end-of-day workflow (called with mutex held). @zara"""
        started_at = asyncio.get_event_loop().time()
        _LOGGER.info(
            "================================================================================"
        )
        _LOGGER.info(
            "    END-OF-DAY WORKFLOW STARTED at %s",
            current_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        _LOGGER.info(
            "================================================================================"
        )
        steps_completed = 0
        total_steps = 16
        errors = []
        outcomes = []
        informational_codes = {
            "weather_mlp_warmup",
            "weather_mlp_not_configured",
            "weather_mlp_governance_filtered",
            "shadow_detection_data_unavailable",
            "candidate_shadow_evaluation_pending",
            "weather_mlp_training_window_expired",
            "weather_mlp_local_sensor_unavailable",
            "insufficient_independent_training_days",
            "weather_mlp_precision_samples_excluded",
            "weather_mlp_precision_samples_unavailable",
            "weather_expert_immutable_learning_disabled",
            "weather_mlp_measured_radiation_unavailable",
            "panel_group_model_learning_data_unavailable",
        }

        def normalize_outcome(step: str, outcome: Any) -> Dict[str, str]:
            if not isinstance(outcome, dict):
                return {
                    "status": "failed",
                    "reason": f"{step} returned no explicit outcome",
                }
            status = outcome.get("status")
            reason = str(outcome.get("reason") or "")
            if status not in {"failed", "skipped", "succeeded"}:
                return {
                    "status": "failed",
                    "reason": f"{step} returned invalid outcome status: {status!r}",
                }
            if status != "succeeded" and (not reason):
                reason = f"{step} returned {status} without a reason"
            normalized_outcome = {"status": status, "reason": reason}
            reason_code = outcome.get("reason_code")
            impact = outcome.get("impact")
            if reason_code is not None:
                normalized_outcome["reason_code"] = str(reason_code)
            if impact is not None:
                normalized_outcome["impact"] = str(impact)
            return normalized_outcome

        def record_step(step: str, outcome: Any) -> None:
            nonlocal steps_completed
            normalized_outcome = normalize_outcome(step, outcome)
            status = normalized_outcome["status"]
            reason = normalized_outcome["reason"]
            outcomes.append({"step": step, **normalized_outcome})
            informational_skip = (
                status == "skipped"
                and normalized_outcome.get("impact") == "informational"
                and (normalized_outcome.get("reason_code") in informational_codes)
            )
            if status == "succeeded" or informational_skip:
                steps_completed += 1
            elif reason:
                errors.append(f"{step}: {reason}")
            if True:
                return None

        def set_workflow_step(step: str) -> None:
            if self.coordinator:
                self.coordinator.update_workflow_status(
                    current_step=step,
                    steps_completed=steps_completed,
                    total_steps=total_steps,
                )
            if True:
                return None

        set_workflow_step("Preparing curtailment learning checks")
        try:
            curtailment_corrections = (
                await self._correct_curtailment_learning_exclusions(current_time)
            )
            if curtailment_corrections > 0:
                _LOGGER.info(
                    "Curtailment learning correction: %d hours quarantined before EOD",
                    curtailment_corrections,
                )
        except Exception as error:
            _LOGGER.warning("Curtailment learning correction failed: %s", error)
        try:
            await self._run_daily_data_hygiene_audit(current_time)
        except Exception as error:
            _LOGGER.error("Daily data hygiene audit failed: %s", error, exc_info=True)
        await asyncio.sleep(0)
        set_workflow_step("Finalizing day")
        try:
            record_step("Finalize", await self._finalize_day_internal(current_time))
        except Exception as error:
            _LOGGER.error(f"Step 1 (finalize) failed: {error}", exc_info=True)
            record_step("Finalize", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Creating daily summary")
        try:
            today = current_time.date().isoformat()
            record_step("Daily summary", await self._create_daily_summary(today))
        except Exception as error:
            _LOGGER.error(f"Step 2 (summary) failed: {error}", exc_info=True)
            record_step("Daily summary", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Archiving day to forecast history")
        try:
            record_step("History", await self._move_to_history_internal(current_time))
        except Exception as error:
            _LOGGER.error(f"Step 3 (history) failed: {error}", exc_info=True)
            record_step("History", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Updating previous-day deviation")
        try:
            record_step(
                "Deviation",
                await self._update_yesterday_deviation_internal(current_time),
            )
        except Exception as error:
            _LOGGER.error(f"Step 4 (deviation) failed: {error}", exc_info=True)
            record_step("Deviation", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Calculating statistics")
        try:
            record_step(
                "Statistics", await self._calculate_stats_internal(current_time)
            )
        except Exception as error:
            _LOGGER.error(f"Step 5 (stats) failed: {error}", exc_info=True)
            record_step("Statistics", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        try:
            await self._store_tfs_comparison(current_time)
        except Exception as error:
            _LOGGER.debug("TFS comparison skipped: %s", error)
        set_workflow_step("Calculating weather precision")
        try:
            record_step(
                "Weather precision",
                await self._calculate_weather_precision(current_time),
            )
        except Exception as error:
            _LOGGER.error(f"Step 6 (weather precision) failed: {error}", exc_info=True)
            record_step("Weather precision", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Training weather model")
        try:
            reaction_outcome = await self._train_weather_mlp(current_time)
            record_step("Weather MLP", reaction_outcome)
        except Exception as error:
            _LOGGER.error(f"Step 6b (Weather MLP) failed: {error}", exc_info=True)
            record_step("Weather MLP", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Running night cleanup")
        try:
            record_step("Cleanup", await self._night_cleanup_internal(current_time))
        except Exception as error:
            _LOGGER.error(f"Step 7 (cleanup) failed: {error}", exc_info=True)
            record_step("Cleanup", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        try:
            snow_corrections = await self._correct_snow_learning_exclusions(
                current_time
            )
            if snow_corrections > 0:
                _LOGGER.info(
                    "Snow learning correction: %d hours unblocked for learning",
                    snow_corrections,
                )
        except Exception as error:
            _LOGGER.warning("Snow learning correction failed: %s", error)
        await asyncio.sleep(0)
        set_workflow_step("Training main AI model")
        try:
            reaction_outcome = await self._train_ml_main_model(current_time)
            record_step("AI model", reaction_outcome)
        except Exception as error:
            _LOGGER.error(f"Step 8 (AI model training) failed: {error}", exc_info=True)
            record_step("AI model", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        set_workflow_step("Learning method performance")
        try:
            method_outcome = normalize_outcome(
                "Method performance", await self._learn_method_performance(current_time)
            )
            regime_outcome = normalize_outcome(
                "Weather regime performance",
                await self._learn_weather_regime_performance(current_time),
            )
            learning_outcomes = [method_outcome, regime_outcome]
            if any(
                (
                    learning_outcome["status"] == "failed"
                    for learning_outcome in learning_outcomes
                )
            ):
                status = "failed"
            elif any(
                (
                    learning_outcome["status"] == "skipped"
                    for learning_outcome in learning_outcomes
                )
            ):
                status = "skipped"
            else:
                status = "succeeded"
            outcome_reasons = [
                learning_outcome["reason"]
                for learning_outcome in learning_outcomes
                if learning_outcome["reason"]
            ]
            record_step(
                "Method Performance",
                {"status": status, "reason": "; ".join(outcome_reasons)},
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 9 (Method Performance Learning) failed: {error}", exc_info=True
            )
            record_step(
                "Method Performance", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Learning panel-group efficiency")
        try:
            record_step(
                "Panel Group Efficiency",
                await self._train_panel_group_efficiency(current_time),
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 10 (Panel Group Efficiency learning) failed: {error}",
                exc_info=True,
            )
            record_step(
                "Panel Group Efficiency", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Learning group method performance")
        try:
            record_step(
                "Group Method Performance",
                await self._learn_group_method_performance(current_time),
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 10b (Group Method Performance) failed: {error}", exc_info=True
            )
            record_step(
                "Group Method Performance", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Calibrating physics engine")
        try:
            record_step(
                "Physics Calibration", await self._run_physics_calibration(current_time)
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 11 (Physics Calibration) failed: {error}", exc_info=True
            )
            record_step(
                "Physics Calibration", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Learning weather expert weights")
        try:
            record_step(
                "Weather Expert Learning",
                await self._run_weather_expert_learning(current_time),
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 12 (Weather Expert Learning) failed: {error}", exc_info=True
            )
            record_step(
                "Weather Expert Learning", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Learning shadow patterns")
        try:
            record_step(
                "Shadow Pattern Learning",
                await self._run_shadow_pattern_learning(current_time),
            )
        except Exception as error:
            _LOGGER.error(
                f"Step 13 (Shadow Pattern Learning) failed: {error}", exc_info=True
            )
            record_step(
                "Shadow Pattern Learning", {"status": "failed", "reason": str(error)}
            )
        await asyncio.sleep(0)
        set_workflow_step("Running drift detection")
        try:
            record_step(
                "Drift Detection", await self._run_drift_detection(current_time)
            )
        except Exception as error:
            _LOGGER.error(f"Step 14 (Drift Detection) failed: {error}", exc_info=True)
            record_step("Drift Detection", {"status": "failed", "reason": str(error)})
        await asyncio.sleep(0)
        duration_seconds = asyncio.get_event_loop().time() - started_at
        try:
            today_key = current_time.date().isoformat()
            rounded_duration_seconds = round(duration_seconds, 1)
            eod_completed_at = dt_util.now().isoformat()
            await self.db.execute(
                "UPDATE daily_summaries\n                   SET eod_duration_seconds = ?, eod_completed_at = ?\n                   WHERE date = ?",
                (rounded_duration_seconds, eod_completed_at, today_key),
            )
            await self.db.commit()
            if self.coordinator:
                self.coordinator.eod_duration_seconds = rounded_duration_seconds
                self.coordinator.async_update_listeners()
            _LOGGER.info(
                "EOD duration saved: %.1fs for %s", rounded_duration_seconds, today_key
            )
        except Exception as error:
            _LOGGER.warning("Could not save EOD duration: %s", error)
        try:
            await self._refresh_system_report()
        except Exception as error:
            _LOGGER.info("System report refresh after EOD skipped: %s", error)
        if self.coordinator:
            self.coordinator.update_workflow_status(
                steps_completed=steps_completed, total_steps=total_steps
            )
        if steps_completed == total_steps:
            _LOGGER.info(
                f"End-of-day workflow completed ({steps_completed}/{total_steps} steps, {duration_seconds:.1f}s)"
            )
            if self.coordinator:
                self.coordinator.finish_workflow_status(
                    status="success", current_step="End-of-day workflow completed"
                )
            try:
                if self.coordinator:
                    self.coordinator.update_system_status(
                        event_type="end_of_day_workflow",
                        event_status="success",
                        event_summary="Tagesabschluss erfolgreich",
                        event_details={
                            "duration_seconds": round(duration_seconds, 1),
                            "steps_completed": f"{steps_completed}/{total_steps}",
                            "step_outcomes": outcomes,
                        },
                    )
            except Exception:
                pass
        else:
            _LOGGER.warning(
                f"End-of-day workflow partial ({steps_completed}/{total_steps}): {'; '.join(errors)}"
            )
            if self.coordinator:
                self.coordinator.finish_workflow_status(
                    status="partial",
                    current_step="End-of-day workflow completed with issues",
                    last_error="; ".join(errors) if errors else None,
                )
            try:
                if self.coordinator:
                    self.coordinator.update_system_status(
                        event_type="end_of_day_workflow",
                        event_status="partial",
                        event_summary=f"Tagesabschluss teilweise ({steps_completed}/{total_steps})",
                        event_details={
                            "duration_seconds": round(duration_seconds, 1),
                            "steps_completed": f"{steps_completed}/{total_steps}",
                            "errors": errors,
                            "step_outcomes": outcomes,
                        },
                    )
            except Exception:
                pass
        if True:
            return None

    async def _finalize_day_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Finalize current day with actual values. @zara"""
        try:
            actual_yield_kwh = await self._resolve_eod_actual_yield(now)
            warnings = []
            consumption_kwh = None
            consumption_entity_id = getattr(
                self.coordinator, "total_consumption_today", None
            )
            if consumption_entity_id:
                consumption_state = self.hass.states.get(consumption_entity_id)
                if consumption_state and consumption_state.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        consumption_kwh = float(consumption_state.state)
                    except (ValueError, TypeError):
                        warnings.append(
                            f"consumption sensor {consumption_entity_id} has no numeric value"
                        )
            production_seconds = 0
            try:
                if (
                    hasattr(self.coordinator, "production_time_calculator")
                    and self.coordinator.production_time_calculator
                ):
                    calculated_production_hours = (
                        self.coordinator.production_time_calculator.get_production_hours()
                    )
                    if calculated_production_hours is not None:
                        production_seconds = int(calculated_production_hours * 3600)
            except Exception as error:
                raise RuntimeError("production-time calculation failed") from error
            excluded_hours_info = None
            try:
                today_key = now.strftime("%Y-%m-%d")
                rows = await self.db.fetchall(
                    "SELECT exclude_from_learning, has_weather_alert, inverter_clipped,\n                              weather_alert_type, mppt_throttled\n                       FROM hourly_predictions\n                       WHERE target_date = ?",
                    (today_key,),
                )
                if rows:
                    excluded_count = 0
                    total_prediction_hours = len(rows)
                    excluded_reason_counts = {}
                    for row in rows:
                        learning_excluded = row[0]
                        has_weather_alert = row[1]
                        inverter_clipped = row[2]
                        weather_alert_type = row[3]
                        mppt_throttled = row[4]
                        if (
                            learning_excluded
                            or has_weather_alert
                            or inverter_clipped
                            or mppt_throttled
                        ):
                            excluded_count += 1
                            reason = weather_alert_type or "other"
                            if inverter_clipped:
                                reason = "inverter_clipped"
                            elif mppt_throttled:
                                reason = "mppt_throttled"
                            excluded_reason_counts[reason] = (
                                excluded_reason_counts.get(reason, 0) + 1
                            )
                    excluded_ratio = (
                        excluded_count / total_prediction_hours
                        if total_prediction_hours > 0
                        else 0.0
                    )
                    excluded_hours_info = {
                        "count": excluded_count,
                        "total": total_prediction_hours,
                        "ratio": round(excluded_ratio, 3),
                        "reasons": (
                            excluded_reason_counts if excluded_reason_counts else None
                        ),
                    }
                    if excluded_count > 0:
                        _LOGGER.info(
                            f"Excluded hours info for {today_key}: {excluded_count}/{total_prediction_hours} ({excluded_ratio:.1%}), reasons: {excluded_reason_counts}"
                        )
            except Exception as error:
                raise RuntimeError("excluded-hours calculation failed") from error
            production_quality_percent = None
            energy_balance_percent = None
            evaluation_predicted_kwh = 0.0
            evaluation_actual_kwh = 0.0
            energy_predicted_kwh = 0.0
            energy_actual_kwh = 0.0
            curtailed_hours = 0
            curtailed_kwh = 0.0
            totals_row = await self.db.fetchone(
                f"SELECT\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                               {CLEAN_EVALUATION_ELIGIBILITY_SQL}\n                               THEN prediction_kwh\n                               ELSE 0\n                           END) as evaluation_predicted,\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                               {CLEAN_EVALUATION_ELIGIBILITY_SQL}\n                               THEN actual_kwh\n                               ELSE 0\n                           END) as evaluation_actual,\n                       SUM(CASE WHEN actual_kwh IS NOT NULL THEN prediction_kwh ELSE 0 END),\n                       SUM(CASE WHEN actual_kwh IS NOT NULL THEN actual_kwh ELSE 0 END),\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                                AND (\n                                    COALESCE(mppt_throttled, 0) != 0\n                                    OR COALESCE(mppt_throttle_reason, '') IN ({', '.join((repr(throttle_reason) for throttle_reason in sorted(CURTAILMENT_THROTTLE_REASONS)))})\n                                )\n                               THEN 1 ELSE 0\n                           END),\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                                AND (\n                                    COALESCE(mppt_throttled, 0) != 0\n                                    OR COALESCE(mppt_throttle_reason, '') IN ({', '.join((repr(throttle_reason) for throttle_reason in sorted(CURTAILMENT_THROTTLE_REASONS)))})\n                                )\n                               THEN prediction_kwh - actual_kwh ELSE 0\n                           END)\n                   FROM hourly_predictions hp\n                   WHERE target_date = ?",
                (now.date().isoformat(),),
            )
            if totals_row:
                evaluation_predicted_kwh = float(totals_row[0] or 0.0)
                evaluation_actual_kwh = float(totals_row[1] or 0.0)
                energy_predicted_kwh = float(totals_row[2] or 0.0)
                energy_actual_kwh = float(totals_row[3] or 0.0)
                curtailed_hours = int(totals_row[4] or 0)
                curtailed_kwh = float(totals_row[5] or 0.0)
                if evaluation_predicted_kwh > 0 and evaluation_actual_kwh > 0.01:
                    evaluation_difference_kwh = abs(
                        evaluation_predicted_kwh - evaluation_actual_kwh
                    )
                    production_quality_percent = max(
                        0.0,
                        (1.0 - evaluation_difference_kwh / evaluation_predicted_kwh)
                        * 100.0,
                    )
                if energy_predicted_kwh > 0 and energy_actual_kwh > 0.01:
                    energy_difference_kwh = abs(
                        energy_predicted_kwh - energy_actual_kwh
                    )
                    energy_balance_percent = max(
                        0.0,
                        (1.0 - energy_difference_kwh / energy_predicted_kwh) * 100.0,
                    )
            daily_tracking = {
                "date": now.date().isoformat(),
                "finalized": {
                    "yield_kwh": actual_yield_kwh,
                    "consumption_kwh": consumption_kwh,
                    "production_hours": (
                        f"{production_seconds // 3600}:{production_seconds % 3600 // 60:02d}"
                        if production_seconds > 0
                        else None
                    ),
                    "accuracy_percent": (
                        round(production_quality_percent, 1)
                        if production_quality_percent is not None
                        else None
                    ),
                    "production_quality_percent": (
                        round(production_quality_percent, 1)
                        if production_quality_percent is not None
                        else None
                    ),
                    "energy_balance_percent": (
                        round(energy_balance_percent, 1)
                        if energy_balance_percent is not None
                        else None
                    ),
                    "production_predicted_kwh": round(evaluation_predicted_kwh, 3),
                    "production_actual_kwh": round(evaluation_actual_kwh, 3),
                    "energy_predicted_kwh": round(energy_predicted_kwh, 3),
                    "energy_actual_kwh": round(energy_actual_kwh, 3),
                    "curtailed_hours": curtailed_hours,
                    "curtailed_kwh": round(curtailed_kwh, 3),
                    "excluded_hours": excluded_hours_info or {},
                    "at": now.isoformat(),
                },
            }
            await self.db.save_daily_forecast_tracking(daily_tracking)
            _LOGGER.info(
                f"Day finalized: yield={actual_yield_kwh:.2f} kWh, consumption={(consumption_kwh if consumption_kwh is not None else 0.0):.2f} kWh, production_quality={(production_quality_percent if production_quality_percent is not None else 0.0):.1f}%, energy_balance={(energy_balance_percent if energy_balance_percent is not None else 0.0):.1f}%, curtailed={curtailed_hours}h/{curtailed_kwh:.2f} kWh"
            )
            if warnings:
                return {"status": "skipped", "reason": "; ".join(warnings)}
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Failed to finalize day: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _move_to_history_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Move current day to forecast history and recover missing history for last 7 days. @zara"""
        try:
            import json as json_module

            archived_count = 0
            for days_back in range(7):
                archive_datetime = now - timedelta(days=days_back)
                archive_date = archive_datetime.date().isoformat()
                existing_history_row = await self.db.fetchone(
                    "SELECT 1 FROM forecast_history WHERE date = ?", (archive_date,)
                )
                if existing_history_row and days_back > 0:
                    continue
                tracking = None
                if days_back == 0:
                    tracking = await self.db.get_daily_forecast_tracking()
                    if not tracking or tracking.get("date") != archive_date:
                        tracking = None
                if tracking:
                    finalized = tracking.get("finalized", {})
                    if not finalized:
                        _LOGGER.warning(
                            f"Day {archive_date} not finalized, skipping history move"
                        )
                        continue
                    daily_forecast_row = await self.db.fetchone(
                        "SELECT prediction_kwh, source FROM daily_forecasts\n                           WHERE forecast_type = 'today' AND forecast_date = ?",
                        (archive_date,),
                    )
                    predicted_kwh = (
                        daily_forecast_row[0] if daily_forecast_row else None
                    )
                    forecast_source = (
                        daily_forecast_row[1] if daily_forecast_row else None
                    )
                    history_entry = {
                        "date": archive_date,
                        "predicted_kwh": predicted_kwh,
                        "actual_kwh": finalized.get("yield_kwh"),
                        "consumption_kwh": finalized.get("consumption_kwh"),
                        "autarky": None,
                        "accuracy": finalized.get("accuracy_percent"),
                        "production_hours": finalized.get("production_hours"),
                        "peak_power": tracking.get("peak_today", {}).get("power_w"),
                        "source": forecast_source,
                        "excluded_hours": finalized.get("excluded_hours"),
                        "archived_at": now.isoformat(),
                    }
                else:
                    summary_row = await self.db.fetchone(
                        "SELECT predicted_total_kwh, actual_total_kwh, accuracy_percent, production_hours, \n                                  peak_power_w, excluded_hours_count, excluded_reason_breakdown_json\n                           FROM daily_summaries WHERE date = ?",
                        (archive_date,),
                    )
                    if not summary_row:
                        if days_back > 0:
                            _LOGGER.debug(
                                f"Recovery: No daily summary found for {archive_date}, skipping"
                            )
                        else:
                            _LOGGER.warning(
                                f"No summary or tracking for today {archive_date}, skipping history move"
                            )
                        continue
                    (
                        predicted_kwh,
                        actual_total_kwh,
                        accuracy_percent,
                        production_hours,
                        peak_power_w,
                        excluded_hours_count,
                        excluded_reasons_json,
                    ) = summary_row
                    if predicted_kwh is None:
                        predicted_sum_row = await self.db.fetchone(
                            "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                            (archive_date,),
                        )
                        predicted_kwh = (
                            float(predicted_sum_row[0])
                            if predicted_sum_row and predicted_sum_row[0] is not None
                            else None
                        )
                    if actual_total_kwh is None:
                        actual_sum_row = await self.db.fetchone(
                            "SELECT SUM(actual_kwh) FROM hourly_predictions WHERE target_date = ? AND actual_kwh IS NOT NULL",
                            (archive_date,),
                        )
                        actual_total_kwh = (
                            float(actual_sum_row[0])
                            if actual_sum_row and actual_sum_row[0] is not None
                            else None
                        )
                    if accuracy_percent is None and predicted_kwh and actual_total_kwh:
                        accuracy_percent = max(
                            0.0,
                            min(
                                100.0,
                                100.0
                                - abs(predicted_kwh - actual_total_kwh)
                                / predicted_kwh
                                * 100.0,
                            ),
                        )
                    if production_hours is None:
                        production_hours_row = await self.db.fetchone(
                            "SELECT COUNT(*) FROM hourly_predictions WHERE target_date = ? AND actual_kwh > 0.001",
                            (archive_date,),
                        )
                        production_hours = (
                            int(production_hours_row[0])
                            if production_hours_row
                            and production_hours_row[0] is not None
                            else 0
                        )
                    excluded_reasons = {}
                    if excluded_reasons_json:
                        try:
                            excluded_reasons = json_module.loads(excluded_reasons_json)
                        except Exception as error:
                            raise ValueError(
                                f"invalid exclusion reason data for {archive_date}"
                            ) from error
                    excluded_hour_ratio = (
                        excluded_hours_count / 24.0
                        if excluded_hours_count is not None
                        else 0.0
                    )
                    daily_forecast_row = await self.db.fetchone(
                        "SELECT source FROM daily_forecasts\n                           WHERE forecast_type = 'today' AND forecast_date = ?",
                        (archive_date,),
                    )
                    forecast_source = (
                        daily_forecast_row[0]
                        if daily_forecast_row
                        else "adaptive_correction"
                    )
                    history_entry = {
                        "date": archive_date,
                        "predicted_kwh": predicted_kwh,
                        "actual_kwh": actual_total_kwh,
                        "consumption_kwh": None,
                        "autarky": None,
                        "accuracy": accuracy_percent,
                        "production_hours": production_hours,
                        "peak_power": peak_power_w,
                        "source": forecast_source,
                        "excluded_hours": {
                            "count": excluded_hours_count or 0,
                            "total": 24,
                            "ratio": excluded_hour_ratio,
                            "reasons": excluded_reasons,
                        },
                        "archived_at": now.isoformat(),
                    }
                    _LOGGER.info(
                        f"Recovery: Reconstructed history entry for {archive_date}"
                    )
                actual_kwh = history_entry["actual_kwh"] or 0
                consumed_kwh = history_entry["consumption_kwh"] or 0
                if consumed_kwh > 0:
                    history_entry["autarky"] = round(actual_kwh / consumed_kwh * 100, 1)
                await self.db.save_forecast_history_entry(history_entry)
                archived_count += 1
                if days_back == 0:
                    _LOGGER.info(
                        "Day %s moved to history: %.2f → %.2f kWh",
                        archive_date,
                        history_entry["predicted_kwh"] or 0.0,
                        actual_kwh,
                    )
            if archived_count == 0:
                reason = "no finalized or recoverable daily data available"
                _LOGGER.info("History move skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Failed to move to history/recover: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _update_yesterday_deviation_internal(
        self, now: datetime
    ) -> Dict[str, str]:
        """Internal: Update yesterday deviation after moving to history. @zara"""
        try:
            yesterday = (now.date() - timedelta(days=1)).isoformat()
            yesterday_row = await self.db.fetchone(
                "SELECT predicted_kwh, actual_kwh FROM forecast_history\n                   WHERE date = ?",
                (yesterday,),
            )
            if not yesterday_row:
                self.coordinator.last_day_error_kwh = 0.0
                self.coordinator.yesterday_accuracy = 0.0
                reason = f"no forecast history available for {yesterday}"
                _LOGGER.info("Yesterday deviation skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            predicted_yesterday_kwh = yesterday_row[0] or 0.0
            actual_total_kwh = yesterday_row[1] or 0.0
            deviation_kwh = abs(predicted_yesterday_kwh - actual_total_kwh)
            accuracy_percent = 0.0
            if actual_total_kwh > 0.1:
                deviation_ratio = deviation_kwh / actual_total_kwh
                accuracy_percent = max(0.0, 1.0 - deviation_ratio)
            elif predicted_yesterday_kwh < 0.1 and actual_total_kwh < 0.1:
                accuracy_percent = 1.0
            self.coordinator.last_day_error_kwh = round(deviation_kwh, 2)
            self.coordinator.yesterday_accuracy = round(accuracy_percent * 100.0, 1)
            self.coordinator.async_update_listeners()
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(
                f"Failed to update yesterday deviation: {error}", exc_info=True
            )
            self.coordinator.last_day_error_kwh = 0.0
            self.coordinator.yesterday_accuracy = 0.0
            return {"status": "failed", "reason": str(error)}

    async def _calculate_stats_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Calculate statistics (all-time peak, current week/month, last N days). @zara"""
        try:
            statistics = {}
            record_peak_row = await self.db.fetchone(
                "SELECT MAX(peak_power), date FROM forecast_history\n                   WHERE peak_power IS NOT NULL"
            )
            if record_peak_row and record_peak_row[0]:
                statistics["all_time_peak"] = {
                    "power_w": record_peak_row[0],
                    "date": str(record_peak_row[1]) if record_peak_row[1] else None,
                    "at": None,
                }
            today = now.date()
            week_start = today - timedelta(days=today.weekday())
            week_rows = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (week_start.isoformat(), today.isoformat()),
            )
            if week_rows:
                week_yield_kwh = sum((row[0] or 0 for row in week_rows))
                week_consumption_kwh = sum((row[1] or 0 for row in week_rows if row[1]))
                statistics["current_week"] = {
                    "period": f"KW {today.isocalendar()[1]}",
                    "date_range": f"{week_start.strftime('%d.%m')} - {today.strftime('%d.%m')}",
                    "yield_kwh": round(week_yield_kwh, 2),
                    "consumption_kwh": (
                        round(week_consumption_kwh, 2)
                        if week_consumption_kwh > 0
                        else None
                    ),
                    "days": len(week_rows),
                    "updated_at": now.isoformat(),
                }
            month_start = today.replace(day=1)
            month_rows = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh, autarky FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (month_start.isoformat(), today.isoformat()),
            )
            if month_rows:
                month_yield_kwh = sum((row[0] or 0 for row in month_rows))
                month_consumption_kwh = sum(
                    (row[1] or 0 for row in month_rows if row[1])
                )
                autarky_values = [row[2] for row in month_rows if row[2] is not None]
                mean_autarky_percent = (
                    sum(autarky_values) / len(autarky_values)
                    if autarky_values
                    else None
                )
                statistics["current_month"] = {
                    "period": today.strftime("%B %Y"),
                    "yield_kwh": round(month_yield_kwh, 2),
                    "consumption_kwh": (
                        round(month_consumption_kwh, 2)
                        if month_consumption_kwh > 0
                        else None
                    ),
                    "avg_autarky": (
                        round(mean_autarky_percent, 1) if mean_autarky_percent else None
                    ),
                    "days": len(month_rows),
                    "updated_at": now.isoformat(),
                }
            last_week_start = (today - timedelta(days=7)).isoformat()
            week_summary_statistics = await self.db.get_daily_summary_statistics(
                start_date=last_week_start, end_date=today.isoformat()
            )
            if week_summary_statistics["yield_days"]:
                statistics["last_7_days"] = {
                    "avg_yield_kwh": round(week_summary_statistics["avg_yield_kwh"], 2),
                    "avg_accuracy": (
                        round(week_summary_statistics["avg_accuracy"], 1)
                        if week_summary_statistics["avg_accuracy"] is not None
                        else None
                    ),
                    "avg_evaluation_coverage_percent": (
                        round(
                            week_summary_statistics["avg_evaluation_coverage_percent"],
                            1,
                        )
                        if week_summary_statistics["avg_evaluation_coverage_percent"]
                        is not None
                        else None
                    ),
                    "avg_excluded_mppt_hours": (
                        round(week_summary_statistics["avg_excluded_mppt_hours"], 2)
                        if week_summary_statistics["avg_excluded_mppt_hours"]
                        is not None
                        else None
                    ),
                    "total_yield_kwh": round(
                        week_summary_statistics["total_yield_kwh"], 2
                    ),
                    "evaluation_days": week_summary_statistics["evaluation_days"],
                    "calculated_at": now.isoformat(),
                }
            last_month_start = (today - timedelta(days=30)).isoformat()
            month_summary_statistics = await self.db.get_daily_summary_statistics(
                start_date=last_month_start, end_date=today.isoformat()
            )
            if month_summary_statistics["yield_days"]:
                statistics["last_30_days"] = {
                    "avg_yield_kwh": round(
                        month_summary_statistics["avg_yield_kwh"], 2
                    ),
                    "avg_accuracy": (
                        round(month_summary_statistics["avg_accuracy"], 1)
                        if month_summary_statistics["avg_accuracy"] is not None
                        else None
                    ),
                    "avg_evaluation_coverage_percent": (
                        round(
                            month_summary_statistics["avg_evaluation_coverage_percent"],
                            1,
                        )
                        if month_summary_statistics["avg_evaluation_coverage_percent"]
                        is not None
                        else None
                    ),
                    "avg_excluded_mppt_hours": (
                        round(month_summary_statistics["avg_excluded_mppt_hours"], 2)
                        if month_summary_statistics["avg_excluded_mppt_hours"]
                        is not None
                        else None
                    ),
                    "total_yield_kwh": round(
                        month_summary_statistics["total_yield_kwh"], 2
                    ),
                    "evaluation_days": month_summary_statistics["evaluation_days"],
                    "calculated_at": now.isoformat(),
                }
            year_start = (today - timedelta(days=365)).isoformat()
            year_rows = await self.db.fetchall(
                "SELECT actual_kwh FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (year_start, today.isoformat()),
            )
            if year_rows:
                year_yield_kwh = sum((row[0] or 0 for row in year_rows))
                statistics["last_365_days"] = {
                    "avg_yield_kwh": round(year_yield_kwh / len(year_rows), 2),
                    "total_yield_kwh": round(year_yield_kwh, 2),
                    "calculated_at": now.isoformat(),
                }
            if not statistics:
                reason = (
                    f"no forecast history or summary statistics available for {today}"
                )
                _LOGGER.info("Statistics calculation skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            await self.db.save_daily_statistics(statistics)
            _LOGGER.info("Statistics calculated successfully")
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Failed to calculate statistics: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _calculate_weather_precision(self, now: datetime) -> Dict[str, str]:
        """Calculate weather precision factors (compare forecast vs actual weather). @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                reason = "no weather pipeline manager available"
                _LOGGER.info("Weather precision skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            pipeline_manager = self.coordinator.weather_pipeline_manager
            if not pipeline_manager:
                reason = "weather pipeline is unavailable"
                _LOGGER.info("Weather precision skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            if (
                not pipeline_manager.weather_precision
                or not pipeline_manager.weather_actual_tracker
                or (not pipeline_manager.weather_corrector)
            ):
                reason = "required weather precision components are not initialized"
                _LOGGER.info("Weather precision skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            today = now.strftime("%Y-%m-%d")
            _LOGGER.info(f"Calculating weather precision for {today}")
            success = await pipeline_manager.weather_precision.calculate_correction_factors_v2(
                weather_actual_tracker=pipeline_manager.weather_actual_tracker,
                date_str=today,
            )
            if success:
                _LOGGER.info(f"Weather precision calculated for {today}")
                return {"status": "succeeded", "reason": ""}
            reason = f"no weather observations available for {today}"
            _LOGGER.info("Weather precision skipped: %s", reason)
            return {"status": "skipped", "reason": reason}
        except Exception as error:
            _LOGGER.error(
                f"Weather precision calculation error: {error}", exc_info=True
            )
            return {"status": "failed", "reason": str(error)}

    async def _night_cleanup_internal(self, now: datetime) -> Dict[str, str]:
        """Night cleanup: VACUUM DISABLED to prevent data loss. @zara

        VACUUM was causing DB shrinkage and potential data loss.
        Database optimization is handled by SQLite automatically.
        """
        try:
            _LOGGER.info("Night cleanup: VACUUM disabled (DB auto-optimizes)")
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Night cleanup error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _train_ml_main_model(self, now: datetime) -> Dict[str, str]:
        """Train the AI model. @zara"""
        training_task = None
        training_succeeded = False
        try:
            if not self.coordinator or not self.coordinator.ai_predictor:
                reason = "no AI predictor available"
                _LOGGER.info("AI model training skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            _LOGGER.info("EOD training uses already persisted actuals")
            predictor = self.coordinator.ai_predictor
            async with self._model_candidate_gate_lock:
                refreshed_evidence_count = (
                    await self.model_candidate_shadow_handler.refresh_actual_evidence()
                )
                invalidated_candidates = (
                    await self.db.invalidate_stale_model_candidate_evaluations()
                )
                if refreshed_evidence_count or invalidated_candidates:
                    _LOGGER.info(
                        "Model candidate evidence reconciled before promotion: actual_versions=%d invalidated_evaluations=%d",
                        refreshed_evidence_count,
                        invalidated_candidates,
                    )
                reconciled_candidate_id = (
                    await predictor.reconcile_pending_model_candidates()
                )
                candidate_evaluation = None
                if reconciled_candidate_id:
                    candidate_evaluation = await self.model_candidate_shadow_handler.evaluate_ready_candidate(
                        candidate_id=reconciled_candidate_id
                    )
                if candidate_evaluation is None:
                    candidate_evaluation = (
                        await self.db.get_retryable_model_candidate_promotion()
                    )
                readiness = None
                if candidate_evaluation is None:
                    if reconciled_candidate_id:
                        readiness = (
                            await self.model_candidate_shadow_handler.gate_readiness(
                                reconciled_candidate_id
                            )
                        )
                        try:
                            persist_gate_status = getattr(
                                self.db, "persist_ai_gate_status", None
                            )
                            if callable(persist_gate_status):
                                await persist_gate_status(
                                    reconciled_candidate_id, readiness
                                )
                        except Exception as gate_status_error:
                            _LOGGER.debug(
                                "AI gate status persist failed: %s", gate_status_error
                            )
                if candidate_evaluation:
                    _LOGGER.info(
                        "Model candidate shadow evaluation recorded: candidate=%s decision=%s",
                        candidate_evaluation["candidate_id"],
                        candidate_evaluation["decision"],
                    )
                    if candidate_evaluation["decision"] == "GO":
                        try:
                            await self._promote_model_candidate_once(
                                predictor, candidate_evaluation
                            )
                        except Exception as error:
                            reason = f"verified model candidate promotion failed for {candidate_evaluation['candidate_id']}: {error}"
                            _LOGGER.error(
                                "AI model training failed: %s", reason, exc_info=True
                            )
                            return {"status": "failed", "reason": reason}
                        _LOGGER.info(
                            "Verified model candidate promoted: candidate=%s evaluation=%s",
                            candidate_evaluation["candidate_id"],
                            candidate_evaluation["evaluation_id"],
                        )
            try:
                compacted_evidence_count = (
                    await self.db.compact_terminal_model_candidate_evidence(limit=2)
                )
                if compacted_evidence_count:
                    _LOGGER.info(
                        "Terminal candidate evidence maintenance completed: candidates=%d",
                        compacted_evidence_count,
                    )
            except Exception:
                _LOGGER.error(
                    "Terminal candidate evidence maintenance deferred safely",
                    exc_info=True,
                )
            await self.db.timeout_unevaluated_model_candidate(
                source="eod", readiness=readiness
            )
            reconciled_candidate_id = await self.db.get_pending_model_candidate_id()
            if reconciled_candidate_id:
                reason = (
                    f"candidate shadow evaluation pending for {reconciled_candidate_id}"
                )
                _LOGGER.info("AI model training skipped: %s", reason)
                return {
                    "status": "skipped",
                    "reason": reason,
                    "reason_code": "candidate_shadow_evaluation_pending",
                    "impact": "informational",
                }
            training_task = getattr(predictor, "notification_service", None)
            if training_task is not None:
                try:
                    await training_task.show_training_start()
                except Exception:
                    _LOGGER.warning("Training start notification failed", exc_info=True)
            training_succeeded = True
            result = await predictor.train_model(through_date=now.date().isoformat())
            if result.success:
                attention_note = ", attention=ON" if result.has_attention else ""
                rmse_note = f", RMSE={result.rmse:.3f}kWh" if result.rmse else ""
                _LOGGER.info(
                    f"AI model trained: R²={result.accuracy:.3f}{rmse_note}, samples={result.samples_used}, features={result.feature_count}, outputs={result.num_outputs}{attention_note}"
                )
                if self.coordinator:
                    refresh_ai_diagnostics = getattr(
                        self.coordinator, "async_refresh_ai_diagnostic_snapshot", None
                    )
                    if callable(refresh_ai_diagnostics):
                        await refresh_ai_diagnostics()
                    self.coordinator.on_ai_training_complete(
                        timestamp=dt_util.now(), accuracy=result.accuracy
                    )
                if training_task is not None:
                    try:
                        await training_task.show_training_complete(
                            success=True,
                            accuracy=result.accuracy * 100.0,
                            sample_count=result.samples_used,
                        )
                    except Exception:
                        _LOGGER.warning(
                            "Training completion notification failed", exc_info=True
                        )
            else:
                if result.error_message:
                    if training_task is not None:
                        try:
                            await training_task.show_training_complete(
                                success=False, sample_count=result.samples_used
                            )
                        except Exception:
                            _LOGGER.warning(
                                "Training failure notification failed", exc_info=True
                            )
                    _LOGGER.warning(
                        "AI model training failed: %s", result.error_message
                    )
                    return {"status": "failed", "reason": result.error_message}
                reason_code = getattr(result, "reason_code", None)
                reason = (
                    "training needs independent training and holdout days"
                    if reason_code == "insufficient_independent_training_days"
                    else "training needs more data"
                )
                _LOGGER.info("AI model training skipped: %s", reason)
                if reason_code:
                    return {
                        "status": "skipped",
                        "reason": reason,
                        "reason_code": reason_code,
                        "impact": "informational",
                    }
                return {"status": "skipped", "reason": reason}
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            if training_succeeded:
                if training_task is not None:
                    try:
                        await training_task.show_training_complete(success=False)
                    except Exception:
                        _LOGGER.warning(
                            "Training failure notification failed", exc_info=True
                        )
            _LOGGER.error(f"AI model training error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _correct_snow_learning_exclusions(self, now: datetime) -> int:
        """Correct exclude_from_learning for snow-flagged hours with actual production. @zara

        When the snow tracker marks hours as covered but actual production shows panels
        were clearly producing, the exclusion was wrong. Clear it before learning runs.

        Uses production vs theoretical_max_kwh (astronomy) as evidence:
        - actual > 25% of theoretical = panels were NOT covered
        - Safe threshold: real snow coverage typically yields < 10% theoretical

        Returns number of corrected hours.
        """
        try:
            database = self.db
            today = now.date().isoformat()
            lookback_start = (now - timedelta(days=7)).date().isoformat()
            rows = await database.fetchall(
                "SELECT hp.prediction_id, hp.target_hour, hp.actual_kwh,\n                          pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON pa.prediction_id = hp.prediction_id\n                   WHERE hp.target_date >= ?\n                     AND hp.target_date <= ?\n                     AND hp.weather_alert_type = 'snow_covered_panels'\n                     AND hp.exclude_from_learning = 1\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND pa.theoretical_max_kwh > 0.05",
                (lookback_start, today),
            )
            if not rows:
                return 0
            curtailment_corrections = 0
            for snow_date, target_hour, actual_kwh, snow_hour in rows:
                excluded_ratio = actual_kwh / snow_hour if snow_hour > 0 else 0
                if excluded_ratio > 0.25:
                    await database.execute(
                        "UPDATE hourly_predictions\n                           SET exclude_from_learning = 0,\n                               exclude_from_clean_evaluation = 0,\n                               has_weather_alert = 0,\n                               weather_alert_type = NULL\n                           WHERE prediction_id = ?",
                        (snow_date,),
                    )
                    curtailment_corrections += 1
                    _LOGGER.info(
                        "Snow exclusion corrected H%02d: actual=%.3f kWh (%.0f%% of theoretical) — panels were producing",
                        target_hour,
                        actual_kwh,
                        excluded_ratio * 100,
                    )
                    if curtailment_corrections % 10 == 0:
                        await asyncio.sleep(0)
            if curtailment_corrections > 0:
                await database.commit()
                _LOGGER.info(
                    "Corrected %d snow-excluded hours with actual production evidence",
                    curtailment_corrections,
                )
            return curtailment_corrections
        except Exception as error:
            _LOGGER.warning("Snow learning exclusion correction failed: %s", error)
            return 0

    async def _correct_curtailment_learning_exclusions(self, now: datetime) -> int:
        """Retroactively quarantine flow-proven curtailment before learning uses it."""
        try:
            database = self.db
            curtailment_count_row = await database.fetchone(
                f"SELECT COUNT(*)\n                    FROM hourly_predictions\n                    WHERE COALESCE(mppt_throttle_reason, '') IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                      AND COALESCE(exclude_from_learning, 0) = 0"
            )
            curtailment_exclusion_count = (
                int(curtailment_count_row[0] or 0) if curtailment_count_row else 0
            )
            if curtailment_exclusion_count > 0:
                await database.execute(
                    f"UPDATE hourly_predictions\n                           SET exclude_from_learning = 1,\n                               exclude_from_clean_evaluation = 1\n                         WHERE COALESCE(mppt_throttle_reason, '') IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                           AND COALESCE(exclude_from_learning, 0) = 0"
                )
                await database.commit()
                _LOGGER.info(
                    "Throttle-reason learning quarantine: %d hours excluded from learning",
                    curtailment_exclusion_count,
                )
            today = now.date().isoformat()
            lookback_start = (now - timedelta(days=14)).date().isoformat()
            sensor_config = self._get_sensor_config()
            zero_export_mode = bool(sensor_config.get("zero_export_mode", False))
            has_battery = bool(sensor_config.get("has_battery", False))
            configured_export_limit_w = sensor_config.get(
                "max_grid_export_w", DEFAULT_MAX_GRID_EXPORT_W
            )
            effective_export_limit_w = MorningRoutineHandler._effective_export_limit_w(
                configured_export_limit_w, zero_export_mode
            )
            export_cap_tolerance_w = MorningRoutineHandler._cap_tolerance_w(
                effective_export_limit_w
            )
            rows = await database.fetchall(
                "WITH flow_hour AS (\n                       SELECT date,\n                              hour,\n                              COUNT(*) AS samples,\n                              SUM(CASE WHEN solar_to_battery_w <= ? THEN 1 ELSE 0 END) AS low_battery_samples,\n                              SUM(CASE\n                                      WHEN solar_to_battery_w <= ?\n                                       AND solar_to_grid_w <= ?\n                                       AND house_consumption_w > ?\n                                       AND solar_to_house_w >= house_consumption_w * ?\n                                      THEN 1 ELSE 0\n                                  END) AS house_match_samples,\n                              SUM(CASE\n                                      WHEN house_consumption_w > ?\n                                       AND (\n                                           solar_to_house_w >= house_consumption_w * ?\n                                           OR grid_to_house_w <= ?\n                                       )\n                                      THEN 1 ELSE 0\n                                  END) AS house_covered_samples,\n                              SUM(CASE\n                                      WHEN ? > 0\n                                       AND (\n                                           ABS(solar_power_w - ?) <= ?\n                                           OR ABS(solar_to_grid_w - ?) <= ?\n                                           OR ABS(solar_power_w - (house_consumption_w + ?)) <= ?\n                                       )\n                                      THEN 1 ELSE 0\n                                  END) AS cap_limit_samples,\n                              AVG(solar_to_battery_w) AS avg_battery_w,\n                              AVG(solar_power_w) AS avg_solar_w,\n                              AVG(house_consumption_w) AS avg_house_w,\n                              AVG(solar_to_house_w) AS avg_solar_to_house_w,\n                              AVG(solar_to_grid_w) AS avg_solar_to_grid_w,\n                              MAX(battery_soc) AS max_battery_soc,\n                              AVG(battery_soc) AS avg_battery_soc\n                       FROM stats_power_sources\n                       GROUP BY date, hour\n                   )\n                   SELECT hp.prediction_id, hp.target_date, hp.target_hour,\n                          hp.actual_kwh, pa.theoretical_max_kwh,\n                          pw.solar_radiation_wm2, pw.clouds,\n                          fh.samples, fh.low_battery_samples,\n                          fh.house_match_samples, fh.house_covered_samples,\n                          fh.cap_limit_samples, fh.avg_battery_w,\n                          fh.avg_solar_w, fh.avg_house_w,\n                          fh.avg_solar_to_house_w, fh.avg_solar_to_grid_w,\n                          fh.max_battery_soc, fh.avg_battery_soc\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON pa.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw\n                          ON pw.prediction_id = hp.prediction_id\n                         AND pw.weather_type = 'actual'\n                   LEFT JOIN flow_hour fh\n                          ON fh.date = hp.target_date\n                         AND fh.hour = hp.target_hour\n                   WHERE hp.target_date >= ?\n                     AND hp.target_date <= ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0.01\n                     AND hp.prediction_kwh > 0.05\n                     AND pa.theoretical_max_kwh > 0\n                     AND COALESCE(hp.exclude_from_learning, 0) = 0\n                     AND COALESCE(hp.mppt_throttled, 0) = 0\n                     AND COALESCE(hp.inverter_clipped, 0) = 0",
                (
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    effective_export_limit_w,
                    effective_export_limit_w,
                    export_cap_tolerance_w,
                    effective_export_limit_w,
                    export_cap_tolerance_w,
                    effective_export_limit_w,
                    export_cap_tolerance_w,
                    lookback_start,
                    today,
                ),
            )
            curtailment_corrections = 0
            for row in rows:
                snow_date = row[0]
                cache_date = row[1]
                target_hour = int(row[2] or 0)
                actual_total_kwh = float(row[3] or 0.0)
                snow_hour = float(row[4] or 0.0)
                clear_sky_kwh = row[5]
                clouds_percent = row[6]
                flow_evidence = {
                    "samples": int(row[7] or 0),
                    "low_battery_samples": int(row[8] or 0),
                    "house_match_samples": int(row[9] or 0),
                    "house_covered_samples": int(row[10] or 0),
                    "cap_limit_samples": int(row[11] or 0),
                    "avg_battery_w": float(row[12] or 0.0),
                    "avg_solar_w": float(row[13] or 0.0),
                    "avg_house_w": float(row[14] or 0.0),
                    "avg_solar_to_house_w": float(row[15] or 0.0),
                    "avg_solar_to_grid_w": float(row[16] or 0.0),
                    "max_battery_soc": float(row[17]) if row[17] is not None else None,
                    "avg_battery_soc": float(row[18]) if row[18] is not None else None,
                    "export_limit_w": effective_export_limit_w,
                    "cap_tolerance_w": export_cap_tolerance_w,
                }
                weather_actual = {
                    "solar_radiation_wm2": (
                        float(clear_sky_kwh) if clear_sky_kwh is not None else None
                    ),
                    "cloud_cover_percent": (
                        float(clouds_percent) if clouds_percent is not None else None
                    ),
                }
                astronomy_data = {"theoretical_max_kwh": snow_hour}
                curtailment_state = MorningRoutineHandler.evaluate_curtailment_state(
                    actual_kwh=actual_total_kwh,
                    solar_to_battery_power=None,
                    weather_actual=weather_actual,
                    astronomy_data=astronomy_data,
                    flow_evidence=flow_evidence,
                    max_grid_export_w=configured_export_limit_w,
                    zero_export_mode=zero_export_mode,
                    has_battery=has_battery,
                )
                if not curtailment_state["quarantine"]:
                    continue
                reason = curtailment_state["reason"]
                hard_curtailment = 1 if curtailment_state["hard"] else 0
                await database.execute(
                    "UPDATE hourly_predictions\n                       SET exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           mppt_throttled = CASE WHEN ? = 1 THEN 1 ELSE mppt_throttled END,\n                           mppt_throttle_reason = COALESCE(mppt_throttle_reason, ?)\n                       WHERE prediction_id = ?\n                         AND COALESCE(exclude_from_learning, 0) = 0",
                    (hard_curtailment, reason, snow_date),
                )
                curtailment_corrections += 1
                _LOGGER.info(
                    "Curtailment learning quarantine %s H%02d: actual=%.3f kWh, theoretical=%.3f kWh, reason=%s",
                    cache_date,
                    target_hour,
                    actual_total_kwh,
                    snow_hour,
                    reason,
                )
                if curtailment_corrections % 10 == 0:
                    await asyncio.sleep(0)
            if curtailment_corrections > 0:
                await database.commit()
                _LOGGER.info(
                    "Retroactively quarantined %d curtailment-contaminated learning hours",
                    curtailment_corrections,
                )
            return curtailment_corrections + curtailment_exclusion_count
        except Exception as error:
            _LOGGER.warning(
                "Curtailment learning exclusion correction failed: %s", error
            )
            return 0

    async def _run_daily_data_hygiene_audit(self, current_time: datetime) -> None:
        """Run nightly data hygiene audit for the day just ending. @zara"""
        try:
            database = self.db
            today = current_time.date().isoformat()
            installed_capacity_kwp = (
                getattr(self.coordinator, "solar_capacity", 0.0) or 0.0
            )
            if installed_capacity_kwp <= 0.0:
                panel_groups = getattr(self.coordinator, "panel_groups", [])
                installed_capacity_kwp = sum(
                    (
                        get_group_capacity_kwp(panel_group)
                        for panel_group in panel_groups
                    )
                )
            if installed_capacity_kwp <= 0.0:
                installed_capacity_kwp = 5.0
            rows = await database.fetchall(
                "SELECT target_hour, actual_kwh, physics_kwh, prediction_id\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (today,),
            )
            if not rows:
                return
            measured_hour_values = [
                float(prediction_row["actual_kwh"])
                for prediction_row in rows
                if prediction_row["actual_kwh"] is not None
            ]
            measured_daily_kwh = sum(measured_hour_values)
            if measured_daily_kwh > installed_capacity_kwp * 8.5:
                Hubble.warning(
                    f"Auffällig hoher Tagesertrag für den {today} erkannt ({measured_daily_kwh:.2f} kWh bei {installed_capacity_kwp:.2f} kWp installierter Leistung). Um die Genauigkeit der Prognose-KI nicht zu gefährden, wurden alle Stunden dieses Tages vom Modelltraining und der Clean-Evaluation ausgeschlossen."
                )
                await database.execute(
                    "UPDATE hourly_predictions\n                       SET exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           mppt_throttle_reason = COALESCE(mppt_throttle_reason, 'unplausible_daily_yield_spike')\n                       WHERE target_date = ?",
                    (today,),
                )
                await database.execute(
                    "UPDATE prediction_panel_groups\n                       SET exclude_from_learning_group = 1,\n                           exclusion_reason_group = COALESCE(exclusion_reason_group, 'unplausible_daily_yield_spike')\n                       WHERE prediction_id IN (\n                           SELECT prediction_id FROM hourly_predictions WHERE target_date = ?\n                       )",
                    (today,),
                )
                await database.commit()
                return
            blocked_hours = set()
            if hasattr(database, "is_actual_backfill_blocked"):
                for prediction_row in rows:
                    target_hour = int(prediction_row["target_hour"] or 0)
                    if await database.is_actual_backfill_blocked(today, target_hour):
                        blocked_hours.add(target_hour)
            if blocked_hours:
                _LOGGER.warning(
                    "EOD actual reconstruction skipped for emergency-quarantined hours on %s: %s",
                    today,
                    sorted(blocked_hours),
                )
            missing_predictions = [
                prediction_row
                for prediction_row in rows
                if prediction_row["actual_kwh"] is None
                if (prediction_row["physics_kwh"] or 0.0) > 0.01
                if int(prediction_row["target_hour"] or 0) not in blocked_hours
            ]
            if missing_predictions:
                daily_actual_yield_kwh = await self._resolve_eod_actual_yield(
                    current_time
                )
                recorded_actual_kwh = sum(
                    (
                        max(0.0, float(prediction_row["actual_kwh"]))
                        for prediction_row in rows
                        if prediction_row["actual_kwh"] is not None
                    )
                )
                if daily_actual_yield_kwh is not None:
                    if daily_actual_yield_kwh > recorded_actual_kwh:
                        unrecorded_kwh = daily_actual_yield_kwh - recorded_actual_kwh
                        missing_physics_kwh = sum(
                            (
                                float(prediction_row["physics_kwh"] or 0.0)
                                for prediction_row in missing_predictions
                            )
                        )
                        if missing_physics_kwh <= 0:
                            missing_physics_kwh = len(missing_predictions)
                            weights = {
                                prediction_row["target_hour"]: 1.0
                                for prediction_row in missing_predictions
                            }
                        else:
                            weights = {
                                prediction_row["target_hour"]: float(
                                    prediction_row["physics_kwh"] or 0.0
                                )
                                for prediction_row in missing_predictions
                            }
                        reconstructed_count = 0
                        panel_groups = getattr(self.coordinator, "panel_groups", [])
                        total_group_capacity_kwp = sum(
                            (
                                get_group_capacity_kwp(panel_group)
                                for panel_group in panel_groups
                            )
                        )
                        for prediction_row in missing_predictions:
                            target_hour = prediction_row["target_hour"]
                            missing_prediction_id = prediction_row["prediction_id"]
                            hour_share = weights.get(target_hour, 1.0)
                            hour_energy_kwh = round(
                                hour_share / missing_physics_kwh * unrecorded_kwh, 3
                            )
                            await database.execute(
                                "UPDATE hourly_predictions\n                               SET actual_kwh = ?,\n                                   exclude_from_learning = 1,\n                                   exclude_from_clean_evaluation = 1,\n                                   has_sensor_data = 1,\n                                   sensor_data_complete = 0,\n                                   mppt_throttle_reason = COALESCE(mppt_throttle_reason, 'reconstructed_gap_interpolation')\n                               WHERE prediction_id = ?",
                                (hour_energy_kwh, missing_prediction_id),
                            )
                            for panel_group in panel_groups:
                                panel_group_name = panel_group.get("name")
                                if not panel_group_name:
                                    continue
                                group_capacity_kwp = get_group_capacity_kwp(panel_group)
                                group_share = (
                                    group_capacity_kwp / total_group_capacity_kwp
                                    if total_group_capacity_kwp > 0
                                    else 1.0 / len(panel_groups)
                                )
                                group_actual_kwh = round(
                                    hour_energy_kwh * group_share, 3
                                )
                                await database.execute(
                                    "UPDATE prediction_panel_groups\n                                   SET actual_kwh = ?,\n                                       exclude_from_learning_group = 1,\n                                       exclusion_reason_group = COALESCE(exclusion_reason_group, 'reconstructed_gap_interpolation')\n                                   WHERE prediction_id = ? AND group_name = ?",
                                    (
                                        group_actual_kwh,
                                        missing_prediction_id,
                                        panel_group_name,
                                    ),
                                )
                            reconstructed_count += 1
                        if reconstructed_count > 0:
                            await database.execute(
                                "UPDATE hourly_predictions\n                               SET has_panel_group_actuals = TRUE\n                               WHERE target_date = ? AND prediction_id IN (\n                                   SELECT prediction_id FROM hourly_predictions WHERE target_date = ?\n                               )",
                                (today, today),
                            )
                            await database.commit()
                            Hubble.info(
                                f"Datenlücke von {reconstructed_count} Stunden am {today} erkannt. Der Ausfall wurde über die physikalische Erwartungskurve mit insgesamt {unrecorded_kwh:.3f} kWh rekonstruiert und für das KI-Training gesperrt."
                            )
        except Exception as error:
            _LOGGER.warning("Daily data hygiene audit failed: %s", error)
        if True:
            return None

    async def _learn_method_performance(self, now: datetime) -> Dict[str, str]:
        """Learn physics vs AI accuracy per condition bucket. @zara V16.0.0

        Compares physics_kwh, ai_kwh, prediction_kwh (blend) against actual_kwh
        for each hour of the day. Updates method_performance_learning table
        with smoothed MAE values and derived ai_advantage_factor.
        """
        try:
            if not self.coordinator or not self.coordinator.data_manager:
                reason = "no data manager available"
                _LOGGER.info("Method performance learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            database = self.db
            today = now.date().isoformat()
            rows = await database.fetchall(
                f"SELECT hp.target_hour,\n                          CASE WHEN hp.adaptive_corrected = TRUE\n                               THEN hp.prediction_kwh_uncapped\n                               ELSE hp.prediction_kwh END AS blend_kwh,\n                          hp.actual_kwh,\n                          hp.physics_kwh, hp.ai_kwh, hp.ai_confidence,\n                          pw.clouds\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL\n                     {HOURLY_LEARNING_ELIGIBILITY_SQL}",
                (today,),
            )
            if not rows:
                reason = f"no eligible raw prediction data for {today}"
                _LOGGER.info("Method performance learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            errors_by_bucket = {}
            for index, row in enumerate(rows):
                target_hour = row[0]
                blend_kwh = row[1]
                actual_kwh = row[2]
                physics_kwh = row[3]
                ai_kwh = row[4]
                clouds_percent = row[6] or 50
                if actual_kwh is None or actual_kwh <= 0:
                    continue
                cloud_bucket = (
                    "clear"
                    if clouds_percent < 30
                    else "partly_cloudy" if clouds_percent < 70 else "overcast"
                )
                hour_bucket = (
                    "morning"
                    if target_hour < 10
                    else "midday" if target_hour < 15 else "afternoon"
                )
                bucket_key = (cloud_bucket, hour_bucket)
                if bucket_key not in errors_by_bucket:
                    errors_by_bucket[bucket_key] = {
                        "physics": [],
                        "ai": [],
                        "blend": [],
                    }
                errors_by_bucket[bucket_key]["physics"].append(
                    abs(physics_kwh - actual_kwh)
                )
                errors_by_bucket[bucket_key]["blend"].append(
                    abs(blend_kwh - actual_kwh)
                )
                if ai_kwh is not None and ai_kwh > 0:
                    errors_by_bucket[bucket_key]["ai"].append(abs(ai_kwh - actual_kwh))
                if index % 50 == 0:
                    await asyncio.sleep(0)
            if not errors_by_bucket:
                reason = f"no valid production hours for {today}"
                _LOGGER.info("Method performance learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            smoothing_alpha = 0.3
            current_season = get_season(now.month)
            async with database.transaction():
                for (
                    cloud_bucket,
                    hour_bucket,
                ), bucket_errors in errors_by_bucket.items():
                    physics_mae = sum(bucket_errors["physics"]) / len(
                        bucket_errors["physics"]
                    )
                    blend_mae = sum(bucket_errors["blend"]) / len(
                        bucket_errors["blend"]
                    )
                    ai_mae = (
                        sum(bucket_errors["ai"]) / len(bucket_errors["ai"])
                        if bucket_errors["ai"]
                        else None
                    )
                    for bucket_season in (None, current_season):
                        previous_learning_row = await database.fetchone(
                            "SELECT physics_mae, ai_mae, blend_mae, sample_count\n                               FROM method_performance_learning\n                               WHERE cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (cloud_bucket, hour_bucket, bucket_season),
                        )
                        if (
                            previous_learning_row
                            and previous_learning_row[3]
                            and (previous_learning_row[3] > 0)
                        ):
                            smoothed_physics_mae = (
                                smoothing_alpha * physics_mae
                                + (1 - smoothing_alpha) * previous_learning_row[0]
                            )
                            smoothed_ai_mae = (
                                smoothing_alpha * ai_mae
                                + (1 - smoothing_alpha) * previous_learning_row[1]
                                if ai_mae and previous_learning_row[1]
                                else ai_mae
                            )
                            smoothed_blend_mae = (
                                smoothing_alpha * blend_mae
                                + (1 - smoothing_alpha) * previous_learning_row[2]
                            )
                            sample_count = previous_learning_row[3] + len(
                                bucket_errors["physics"]
                            )
                        else:
                            smoothed_physics_mae = physics_mae
                            smoothed_ai_mae = ai_mae
                            smoothed_blend_mae = blend_mae
                            sample_count = len(bucket_errors["physics"])
                        ai_advantage_factor = advantage_factor(
                            smoothed_physics_mae, smoothed_ai_mae
                        )
                        learning_params = (
                            cloud_bucket,
                            hour_bucket,
                            bucket_season,
                            round(smoothed_physics_mae, 6),
                            round(smoothed_ai_mae, 6) if smoothed_ai_mae else None,
                            round(smoothed_blend_mae, 6),
                            round(ai_advantage_factor, 4),
                            sample_count,
                            now,
                        )
                        if bucket_season is None:
                            await database.execute(
                                "DELETE FROM method_performance_learning\n                                   WHERE cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (cloud_bucket, hour_bucket),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO method_performance_learning\n                                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                    ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                learning_params,
                                auto_commit=False,
                            )
                        else:
                            await database.execute(
                                "INSERT INTO method_performance_learning\n                                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                    ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                learning_params,
                                auto_commit=False,
                            )
                    _LOGGER.info(
                        "Method learning [%s/%s]: physics_mae=%.4f, ai_mae=%s, advantage=%.2f (%d samples)",
                        cloud_bucket,
                        hour_bucket,
                        smoothed_physics_mae,
                        f"{smoothed_ai_mae:.4f}" if smoothed_ai_mae else "N/A",
                        ai_advantage_factor,
                        sample_count,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info("Method performance learning completed for %s", today)
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Method performance learning error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _learn_weather_regime_performance(self, now: datetime) -> Dict[str, str]:
        """Learn method accuracy and bias per static weather regime."""
        try:
            database = self.db
            today = now.date().isoformat()
            rows = await database.fetchall(
                "SELECT oh.target_hour,\n                          oh.prediction_kwh AS blend_kwh,\n                          hp.actual_kwh,\n                          oh.physics_kwh, oh.ai_kwh, oh.tfs_kwh,\n                          ow.clouds, ow.rain, ow.humidity,\n                          ow.solar_radiation_wm2, ow.direct_radiation,\n                          ow.diffuse_radiation,\n                          ac.clear_sky_radiation_wm2, ac.sun_elevation_deg\n                   FROM ops_daily_forecasts od\n                   JOIN ops_hourly_forecasts oh\n                       ON oh.snapshot_sequence = od.snapshot_sequence\n                       AND oh.target_date = od.forecast_date\n                   LEFT JOIN ops_prediction_weather ow\n                       ON ow.forecast_row_id = oh.forecast_row_id\n                   LEFT JOIN hourly_predictions hp\n                       ON hp.prediction_id = oh.prediction_id\n                   LEFT JOIN astronomy_cache ac\n                       ON ac.cache_date = oh.target_date AND ac.hour = oh.target_hour\n                   WHERE od.forecast_type = 'today'\n                     AND od.snapshot_type = 'morning_snapshot'\n                     AND od.forecast_date = ?\n                     AND od.snapshot_sequence = (\n                         SELECT MAX(od2.snapshot_sequence)\n                         FROM ops_daily_forecasts od2\n                         WHERE od2.forecast_type = 'today'\n                           AND od2.snapshot_type = 'morning_snapshot'\n                           AND od2.forecast_date = od.forecast_date\n                     )\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND oh.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (hp.exclude_from_clean_evaluation = FALSE OR hp.exclude_from_clean_evaluation IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN (\n                         'suspected_battery_curtailment',\n                         'demand_limited_zero_export',\n                         'full_battery_export_limit',\n                         'full_battery_zero_export',\n                         'transition_curtailment'\n                     )\n                     AND COALESCE(hp.data_quarantine_reason, '') NOT IN (\n                         'suspected_battery_curtailment',\n                         'demand_limited_zero_export',\n                         'full_battery_export_limit',\n                         'full_battery_zero_export',\n                         'transition_curtailment'\n                     )",
                (today,),
            )
            ops_snapshot_cycle = "ops_morning_snapshot"
            if not rows:
                rows = await database.fetchall(
                    "SELECT hp.target_hour,\n                              CASE WHEN hp.adaptive_corrected = TRUE\n                                   THEN hp.prediction_kwh_uncapped\n                                   ELSE hp.prediction_kwh END AS blend_kwh,\n                              hp.actual_kwh,\n                              hp.physics_kwh, hp.ai_kwh, hp.tfs_kwh,\n                              pw.clouds, pw.rain, pw.humidity,\n                              pw.solar_radiation_wm2, pw.direct_radiation,\n                              pw.diffuse_radiation,\n                              ac.clear_sky_radiation_wm2, ac.sun_elevation_deg\n                       FROM hourly_predictions hp\n                       LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                           AND pw.weather_type = 'forecast'\n                       LEFT JOIN astronomy_cache ac\n                           ON ac.cache_date = hp.target_date AND ac.hour = hp.target_hour\n                       WHERE hp.target_date = ?\n                         AND hp.actual_kwh IS NOT NULL\n                         AND hp.actual_kwh > 0\n                         AND hp.physics_kwh IS NOT NULL\n                         AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                         AND (hp.exclude_from_clean_evaluation = FALSE OR hp.exclude_from_clean_evaluation IS NULL)\n                         AND COALESCE(hp.mppt_throttle_reason, '') NOT IN (\n                             'suspected_battery_curtailment',\n                             'demand_limited_zero_export',\n                             'full_battery_export_limit',\n                             'full_battery_zero_export',\n                             'transition_curtailment'\n                         )\n                         AND COALESCE(hp.data_quarantine_reason, '') NOT IN (\n                             'suspected_battery_curtailment',\n                             'demand_limited_zero_export',\n                             'full_battery_export_limit',\n                             'full_battery_zero_export',\n                             'transition_curtailment'\n                         )",
                    (today,),
                )
                ops_snapshot_cycle = "hourly_predictions_fallback"
            if not rows:
                reason = f"no valid hourly weather-regime data for {today}"
                _LOGGER.info("Weather regime learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            _LOGGER.info(
                "Weather regime learning: using %s source for %s (%d rows)",
                ops_snapshot_cycle,
                today,
                len(rows),
            )
            errors_by_regime = {}
            for index, row in enumerate(rows):
                target_hour = row[0]
                blend_kwh = row[1]
                actual_kwh = row[2]
                physics_kwh = row[3]
                ai_kwh = row[4]
                temperature_c = row[5]
                clouds_percent = float(row[6] or 50.0)
                rain_mm = float(row[7] or 0.0)
                humidity_percent = float(row[8] or 70.0)
                ghi_wm2 = float(row[9] or 0.0)
                direct_radiation_wm2 = float(row[10] or 0.0)
                diffuse_radiation_wm2 = float(row[11] or 0.0)
                clear_sky_radiation_wm2 = float(row[12] or 0.0)
                sun_elevation_deg = float(row[13] or 0.0)
                if blend_kwh is None or actual_kwh is None or physics_kwh is None:
                    continue
                static_regime = _classify_static_weather_regime_for_learning(
                    clouds=clouds_percent,
                    ghi=ghi_wm2,
                    dni=direct_radiation_wm2,
                    dhi=diffuse_radiation_wm2,
                    clear_sky=clear_sky_radiation_wm2,
                    rain=rain_mm,
                    humidity=humidity_percent,
                    sun_elevation=sun_elevation_deg,
                    hour=target_hour,
                )
                hour_bucket = (
                    "morning"
                    if target_hour < 10
                    else "midday" if target_hour < 15 else "afternoon"
                )
                bucket_key = (static_regime, hour_bucket)
                if bucket_key not in errors_by_regime:
                    errors_by_regime[bucket_key] = {
                        "physics_abs": [],
                        "ai_abs": [],
                        "tfs_abs": [],
                        "blend_abs": [],
                        "physics_bias": [],
                        "ai_bias": [],
                        "tfs_bias": [],
                        "blend_bias": [],
                    }
                errors_by_regime[bucket_key]["physics_abs"].append(
                    abs(physics_kwh - actual_kwh)
                )
                errors_by_regime[bucket_key]["blend_abs"].append(
                    abs(blend_kwh - actual_kwh)
                )
                errors_by_regime[bucket_key]["physics_bias"].append(
                    physics_kwh - actual_kwh
                )
                errors_by_regime[bucket_key]["blend_bias"].append(
                    blend_kwh - actual_kwh
                )
                if ai_kwh is not None and ai_kwh > 0:
                    errors_by_regime[bucket_key]["ai_abs"].append(
                        abs(ai_kwh - actual_kwh)
                    )
                    errors_by_regime[bucket_key]["ai_bias"].append(ai_kwh - actual_kwh)
                if temperature_c is not None and temperature_c > 0:
                    errors_by_regime[bucket_key]["tfs_abs"].append(
                        abs(temperature_c - actual_kwh)
                    )
                    errors_by_regime[bucket_key]["tfs_bias"].append(
                        temperature_c - actual_kwh
                    )
                if index % 50 == 0:
                    await asyncio.sleep(0)
            if not errors_by_regime:
                reason = f"no usable weather-regime buckets for {today}"
                _LOGGER.info("Weather regime learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            current_season = get_season(now.month)
            updated_regime_count = 0
            async with database.transaction():
                for (
                    static_regime,
                    hour_bucket,
                ), regime_errors in errors_by_regime.items():
                    physics_mae = _mean(regime_errors["physics_abs"])
                    ai_mae = _mean(regime_errors["ai_abs"])
                    tfs_mae = _mean(regime_errors["tfs_abs"])
                    blend_mae = _mean(regime_errors["blend_abs"])
                    physics_bias = _mean(regime_errors["physics_bias"])
                    ai_bias = _mean(regime_errors["ai_bias"])
                    tfs_bias = _mean(regime_errors["tfs_bias"])
                    blend_bias = _mean(regime_errors["blend_bias"])
                    regime_sample_count = len(regime_errors["physics_abs"])
                    eligible_tfs_mae = (
                        tfs_mae
                        if tfs_mae is not None
                        and len(regime_errors["tfs_abs"]) >= REGIME_TFS_MIN_SAMPLES
                        else None
                    )
                    best_regime_method = regime_best_method(
                        physics_mae, blend_mae, ai_mae, eligible_tfs_mae
                    )
                    for bucket_season in (None, current_season):
                        previous_learning_row = await database.fetchone(
                            "SELECT physics_mae, ai_mae, tfs_mae, blend_mae,\n                                      physics_bias, ai_bias, tfs_bias, blend_bias,\n                                      sample_count\n                               FROM weather_regime_performance_learning\n                               WHERE weather_regime = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (static_regime, hour_bucket, bucket_season),
                        )
                        if (
                            previous_learning_row
                            and previous_learning_row[8]
                            and (previous_learning_row[8] > 0)
                        ):

                            def smooth(new_value, old_value):
                                if new_value is None:
                                    return old_value
                                if old_value is None:
                                    return new_value
                                return (
                                    REGIME_LEARNING_SMOOTHING * new_value
                                    + (1 - REGIME_LEARNING_SMOOTHING) * old_value
                                )

                            smoothed_regime_physics_mae = smooth(
                                physics_mae, previous_learning_row[0]
                            )
                            smoothed_regime_ai_mae = smooth(
                                ai_mae, previous_learning_row[1]
                            )
                            smoothed_regime_tfs_mae = smooth(
                                tfs_mae, previous_learning_row[2]
                            )
                            smoothed_regime_blend_mae = smooth(
                                blend_mae, previous_learning_row[3]
                            )
                            smoothed_physics_bias = smooth(
                                physics_bias, previous_learning_row[4]
                            )
                            smoothed_ai_bias = smooth(ai_bias, previous_learning_row[5])
                            smoothed_tfs_bias = smooth(
                                tfs_bias, previous_learning_row[6]
                            )
                            smoothed_blend_bias = smooth(
                                blend_bias, previous_learning_row[7]
                            )
                            sample_count = (
                                previous_learning_row[8] + regime_sample_count
                            )
                        else:
                            smoothed_regime_physics_mae = physics_mae
                            smoothed_regime_ai_mae = ai_mae
                            smoothed_regime_tfs_mae = tfs_mae
                            smoothed_regime_blend_mae = blend_mae
                            smoothed_physics_bias = physics_bias
                            smoothed_ai_bias = ai_bias
                            smoothed_tfs_bias = tfs_bias
                            smoothed_blend_bias = blend_bias
                            sample_count = regime_sample_count
                        smoothed_best_method = regime_best_method(
                            smoothed_regime_physics_mae,
                            smoothed_regime_blend_mae,
                            smoothed_regime_ai_mae,
                            smoothed_regime_tfs_mae,
                        )
                        regime_ai_advantage_factor = advantage_factor(
                            smoothed_regime_physics_mae, smoothed_regime_ai_mae
                        )
                        if (
                            smoothed_regime_tfs_mae
                            and smoothed_regime_tfs_mae > 0
                            and smoothed_regime_blend_mae
                            and (smoothed_regime_blend_mae > 0)
                        ):
                            tfs_advantage_factor = max(
                                0.5,
                                min(
                                    2.0,
                                    smoothed_regime_blend_mae / smoothed_regime_tfs_mae,
                                ),
                            )
                        else:
                            tfs_advantage_factor = 1.0
                        tfs_weight = recommended_tfs_weight(
                            smoothed_best_method, tfs_advantage_factor
                        )
                        learning_params = (
                            static_regime,
                            hour_bucket,
                            bucket_season,
                            (
                                round(smoothed_regime_physics_mae, 6)
                                if smoothed_regime_physics_mae is not None
                                else None
                            ),
                            (
                                round(smoothed_regime_ai_mae, 6)
                                if smoothed_regime_ai_mae is not None
                                else None
                            ),
                            (
                                round(smoothed_regime_tfs_mae, 6)
                                if smoothed_regime_tfs_mae is not None
                                else None
                            ),
                            (
                                round(smoothed_regime_blend_mae, 6)
                                if smoothed_regime_blend_mae is not None
                                else None
                            ),
                            (
                                round(smoothed_physics_bias, 6)
                                if smoothed_physics_bias is not None
                                else None
                            ),
                            (
                                round(smoothed_ai_bias, 6)
                                if smoothed_ai_bias is not None
                                else None
                            ),
                            (
                                round(smoothed_tfs_bias, 6)
                                if smoothed_tfs_bias is not None
                                else None
                            ),
                            (
                                round(smoothed_blend_bias, 6)
                                if smoothed_blend_bias is not None
                                else None
                            ),
                            smoothed_best_method,
                            round(tfs_weight, 4) if tfs_weight else None,
                            round(regime_ai_advantage_factor, 4),
                            round(tfs_advantage_factor, 4),
                            sample_count,
                            now,
                        )
                        if bucket_season is None:
                            await database.execute(
                                "DELETE FROM weather_regime_performance_learning\n                                   WHERE weather_regime = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (static_regime, hour_bucket),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO weather_regime_performance_learning\n                                   (weather_regime, hour_bucket, season,\n                                    physics_mae, ai_mae, tfs_mae, blend_mae,\n                                    physics_bias, ai_bias, tfs_bias, blend_bias,\n                                    best_method, recommended_tfs_weight,\n                                    ai_advantage_factor, tfs_advantage_factor,\n                                    sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                learning_params,
                                auto_commit=False,
                            )
                        else:
                            await database.execute(
                                "INSERT INTO weather_regime_performance_learning\n                                   (weather_regime, hour_bucket, season,\n                                    physics_mae, ai_mae, tfs_mae, blend_mae,\n                                    physics_bias, ai_bias, tfs_bias, blend_bias,\n                                    best_method, recommended_tfs_weight,\n                                    ai_advantage_factor, tfs_advantage_factor,\n                                    sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(weather_regime, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       tfs_mae = excluded.tfs_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       physics_bias = excluded.physics_bias,\n                                       ai_bias = excluded.ai_bias,\n                                       tfs_bias = excluded.tfs_bias,\n                                       blend_bias = excluded.blend_bias,\n                                       best_method = excluded.best_method,\n                                       recommended_tfs_weight = excluded.recommended_tfs_weight,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       tfs_advantage_factor = excluded.tfs_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                learning_params,
                                auto_commit=False,
                            )
                        updated_regime_count += 1
                    _LOGGER.info(
                        "Weather regime learning [%s/%s]: best=%s, physics_mae=%.4f, ai_mae=%s, tfs_mae=%s, blend_bias=%s (%d samples)",
                        static_regime,
                        hour_bucket,
                        best_regime_method,
                        physics_mae,
                        f"{ai_mae:.4f}" if ai_mae is not None else "N/A",
                        f"{tfs_mae:.4f}" if tfs_mae is not None else "N/A",
                        f"{blend_bias:.4f}" if blend_bias is not None else "N/A",
                        regime_sample_count,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Weather regime performance learning completed for %s: %d buckets updated",
                today,
                updated_regime_count,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(
                "Weather regime performance learning error: %s", error, exc_info=True
            )
            return {"status": "failed", "reason": str(error)}

    async def _train_panel_group_efficiency(self, now: datetime) -> Dict[str, str]:
        """Learn per-group ensemble weights (LSTM vs Ridge) per condition bucket. @zara V16.0.3

        For each panel group, computes LSTM and Ridge MAE against actual production,
        bucketed by cloud cover and hour of day. Derives inverse-MAE weights with
        EMA smoothing. Silent no-op when lstm_kwh/ridge_kwh data is not yet available.
        """
        try:
            database = self.db
            today = now.date().isoformat()
            rows = await database.fetchall(
                f"SELECT ppg.group_name, hp.target_hour,\n                          ppg.lstm_kwh, ppg.ridge_kwh, ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN panel_group_config_epochs pge_active\n                       ON pge_active.valid_to IS NULL\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                       ON pgeg_active.epoch_id = pge_active.epoch_id\n                      AND pgeg_active.display_name = ppg.group_name\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.lstm_kwh IS NOT NULL AND ppg.ridge_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                     AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n                     AND (\n                         (ppg.config_epoch_id IS NOT NULL\n                          AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                         OR\n                         (ppg.config_epoch_id IS NULL\n                          AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n                     )",
                (today,),
            )
            if not rows:
                reason = f"no eligible LSTM/Ridge panel-group data for {today}"
                _LOGGER.info("Ensemble group weight learning skipped: %s", reason)
                return {
                    "status": "skipped",
                    "reason": reason,
                    "reason_code": "panel_group_model_learning_data_unavailable",
                    "impact": "informational",
                }
            errors_by_bucket = {}
            for index, row in enumerate(rows):
                group_name = row[0]
                target_hour = row[1]
                lstm_kwh = row[2]
                ridge_kwh = row[3]
                actual_kwh = row[4]
                clouds_percent = row[5] or 50
                cloud_bucket = (
                    "clear"
                    if clouds_percent < 30
                    else "partly_cloudy" if clouds_percent < 70 else "overcast"
                )
                hour_bucket = (
                    "morning"
                    if target_hour < 10
                    else "midday" if target_hour < 15 else "afternoon"
                )
                bucket_key = (group_name, cloud_bucket, hour_bucket)
                if bucket_key not in errors_by_bucket:
                    errors_by_bucket[bucket_key] = {"lstm": [], "ridge": []}
                errors_by_bucket[bucket_key]["lstm"].append(abs(lstm_kwh - actual_kwh))
                errors_by_bucket[bucket_key]["ridge"].append(
                    abs(ridge_kwh - actual_kwh)
                )
                if index % 50 == 0:
                    await asyncio.sleep(0)
            smoothing_alpha = 0.3
            minimum_lstm_weight = 0.2
            maximum_lstm_weight = 0.95
            current_season = get_season(now.month)
            updated_regime_count = 0
            async with database.transaction():
                for (
                    group_name,
                    cloud_bucket,
                    hour_bucket,
                ), bucket_errors in errors_by_bucket.items():
                    lstm_mae = sum(bucket_errors["lstm"]) / len(bucket_errors["lstm"])
                    ridge_mae = sum(bucket_errors["ridge"]) / len(
                        bucket_errors["ridge"]
                    )
                    for bucket_season in (None, current_season):
                        previous_learning_row = await database.fetchone(
                            "SELECT lstm_mae, ridge_mae, sample_count\n                               FROM ensemble_group_weights\n                               WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (group_name, cloud_bucket, hour_bucket, bucket_season),
                        )
                        if (
                            previous_learning_row
                            and previous_learning_row[2]
                            and (previous_learning_row[2] > 0)
                        ):
                            smoothed_lstm_mae = (
                                smoothing_alpha * lstm_mae
                                + (1 - smoothing_alpha) * previous_learning_row[0]
                            )
                            smoothed_ridge_mae = (
                                smoothing_alpha * ridge_mae
                                + (1 - smoothing_alpha) * previous_learning_row[1]
                            )
                            sample_count = previous_learning_row[2] + len(
                                bucket_errors["lstm"]
                            )
                        else:
                            smoothed_lstm_mae = lstm_mae
                            smoothed_ridge_mae = ridge_mae
                            sample_count = len(bucket_errors["lstm"])
                        bounded_lstm_mae = max(0.001, smoothed_lstm_mae)
                        bounded_ridge_mae = max(0.001, smoothed_ridge_mae)
                        inverse_error_total = (
                            1.0 / bounded_lstm_mae + 1.0 / bounded_ridge_mae
                        )
                        inverse_error_lstm_weight = (
                            1.0 / bounded_lstm_mae / inverse_error_total
                        )
                        lstm_weight = max(
                            minimum_lstm_weight,
                            min(maximum_lstm_weight, inverse_error_lstm_weight),
                        )
                        ridge_weight = 1.0 - lstm_weight
                        learning_params = (
                            group_name,
                            cloud_bucket,
                            hour_bucket,
                            bucket_season,
                            round(lstm_weight, 4),
                            round(ridge_weight, 4),
                            round(smoothed_lstm_mae, 6),
                            round(smoothed_ridge_mae, 6),
                            sample_count,
                            now,
                        )
                        if bucket_season is None:
                            await database.execute(
                                "DELETE FROM ensemble_group_weights\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (group_name, cloud_bucket, hour_bucket),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                learning_params,
                                auto_commit=False,
                            )
                        else:
                            await database.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       lstm_weight = excluded.lstm_weight,\n                                       ridge_weight = excluded.ridge_weight,\n                                       lstm_mae = excluded.lstm_mae,\n                                       ridge_mae = excluded.ridge_mae,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                learning_params,
                                auto_commit=False,
                            )
                        updated_regime_count += 1
                    _LOGGER.info(
                        "Ensemble weight [%s/%s/%s]: lstm=%.2f ridge=%.2f (lstm_mae=%.4f, ridge_mae=%.4f, %d samples)",
                        group_name,
                        cloud_bucket,
                        hour_bucket,
                        lstm_weight,
                        ridge_weight,
                        smoothed_lstm_mae,
                        smoothed_ridge_mae,
                        sample_count,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Ensemble group weight learning completed for %s: %d buckets updated",
                today,
                updated_regime_count,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(
                f"Ensemble group weight learning error: {error}", exc_info=True
            )
            return {"status": "failed", "reason": str(error)}

    async def _learn_group_method_performance(self, now: datetime) -> Dict[str, str]:
        """Learn per-group method accuracy (Physics vs AI vs LSTM vs Ridge). @zara V16.4.0

        For each panel group, computes MAE of each prediction method against actual
        production, bucketed by (cloud_bucket, hour_bucket, season). Determines which
        method is most accurate per group per condition. This data feeds the graduated
        safeguard to make data-driven decisions about AI vs Physics blending.

        Silent no-op when no per-group actual data is available.
        """
        try:
            database = self.db
            today = now.date().isoformat()
            rows = await database.fetchall(
                f"SELECT ppg.group_name, hp.target_hour,\n                          ppg.physics_kwh, ppg.ai_kwh,\n                          ppg.lstm_kwh, ppg.ridge_kwh,\n                          ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN panel_group_config_epochs pge_active\n                       ON pge_active.valid_to IS NULL\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                       ON pgeg_active.epoch_id = pge_active.epoch_id\n                      AND pgeg_active.display_name = ppg.group_name\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                     AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n                     AND (\n                         (ppg.config_epoch_id IS NOT NULL\n                          AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                         OR\n                         (ppg.config_epoch_id IS NULL\n                          AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n                     )",
                (today,),
            )
            if not rows:
                reason = f"no eligible panel-group actual data for {today}"
                _LOGGER.info("Group method performance skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            errors_by_bucket = {}
            for index, row in enumerate(rows):
                group_name = row[0]
                target_hour = row[1]
                group_physics_kwh = row[2]
                group_ai_kwh = row[3]
                lstm_kwh = row[4]
                ridge_kwh = row[5]
                actual_kwh = row[6]
                clouds_percent = row[7] or 50
                cloud_bucket = (
                    "clear"
                    if clouds_percent < 30
                    else "partly_cloudy" if clouds_percent < 70 else "overcast"
                )
                hour_bucket = (
                    "morning"
                    if target_hour < 10
                    else "midday" if target_hour < 15 else "afternoon"
                )
                bucket_key = (group_name, cloud_bucket, hour_bucket)
                if bucket_key not in errors_by_bucket:
                    errors_by_bucket[bucket_key] = {
                        "physics": [],
                        "ai": [],
                        "lstm": [],
                        "ridge": [],
                        "blend": [],
                    }
                errors_by_bucket[bucket_key]["physics"].append(
                    abs(group_physics_kwh - actual_kwh)
                )
                if group_ai_kwh is not None and group_ai_kwh > 0:
                    errors_by_bucket[bucket_key]["ai"].append(
                        abs(group_ai_kwh - actual_kwh)
                    )
                if lstm_kwh is not None and lstm_kwh > 0:
                    errors_by_bucket[bucket_key]["lstm"].append(
                        abs(lstm_kwh - actual_kwh)
                    )
                if ridge_kwh is not None and ridge_kwh > 0:
                    errors_by_bucket[bucket_key]["ridge"].append(
                        abs(ridge_kwh - actual_kwh)
                    )
                if index % 50 == 0:
                    await asyncio.sleep(0)
            if not errors_by_bucket:
                reason = f"no usable panel-group performance buckets for {today}"
                _LOGGER.info("Group method performance skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            smoothing_alpha = 0.3
            current_season = get_season(now.month)
            updated_regime_count = 0
            async with database.transaction():
                for (
                    group_name,
                    cloud_bucket,
                    hour_bucket,
                ), bucket_errors in errors_by_bucket.items():
                    physics_mae = sum(bucket_errors["physics"]) / len(
                        bucket_errors["physics"]
                    )
                    ai_mae = (
                        sum(bucket_errors["ai"]) / len(bucket_errors["ai"])
                        if bucket_errors["ai"]
                        else None
                    )
                    lstm_mae = (
                        sum(bucket_errors["lstm"]) / len(bucket_errors["lstm"])
                        if bucket_errors["lstm"]
                        else None
                    )
                    ridge_mae = (
                        sum(bucket_errors["ridge"]) / len(bucket_errors["ridge"])
                        if bucket_errors["ridge"]
                        else None
                    )
                    best_regime_method = group_best_method(
                        physics_mae, ai_mae, lstm_mae, ridge_mae
                    )
                    ai_advantage_factor = advantage_factor(physics_mae, ai_mae)
                    for bucket_season in (None, current_season):
                        previous_learning_row = await database.fetchone(
                            "SELECT physics_mae, ai_mae, lstm_mae, ridge_mae,\n                                      sample_count\n                               FROM group_method_performance\n                               WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (group_name, cloud_bucket, hour_bucket, bucket_season),
                        )
                        if (
                            previous_learning_row
                            and previous_learning_row[4]
                            and (previous_learning_row[4] > 0)
                        ):
                            smoothed_group_physics_mae = (
                                smoothing_alpha * physics_mae
                                + (1 - smoothing_alpha) * previous_learning_row[0]
                            )
                            smoothed_group_ai_mae = (
                                smoothing_alpha * ai_mae
                                + (1 - smoothing_alpha) * previous_learning_row[1]
                                if ai_mae and previous_learning_row[1]
                                else ai_mae
                            )
                            smoothed_group_lstm_mae = (
                                smoothing_alpha * lstm_mae
                                + (1 - smoothing_alpha) * previous_learning_row[2]
                                if lstm_mae and previous_learning_row[2]
                                else lstm_mae
                            )
                            smoothed_group_ridge_mae = (
                                smoothing_alpha * ridge_mae
                                + (1 - smoothing_alpha) * previous_learning_row[3]
                                if ridge_mae and previous_learning_row[3]
                                else ridge_mae
                            )
                            sample_count = previous_learning_row[4] + len(
                                bucket_errors["physics"]
                            )
                        else:
                            smoothed_group_physics_mae = physics_mae
                            smoothed_group_ai_mae = ai_mae
                            smoothed_group_lstm_mae = lstm_mae
                            smoothed_group_ridge_mae = ridge_mae
                            sample_count = len(bucket_errors["physics"])
                        smoothed_best_method = group_best_method(
                            smoothed_group_physics_mae,
                            smoothed_group_ai_mae,
                            smoothed_group_lstm_mae,
                            smoothed_group_ridge_mae,
                        )
                        group_ai_advantage_factor = advantage_factor(
                            smoothed_group_physics_mae, smoothed_group_ai_mae
                        )
                        learning_params = (
                            group_name,
                            cloud_bucket,
                            hour_bucket,
                            bucket_season,
                            round(smoothed_group_physics_mae, 6),
                            (
                                round(smoothed_group_ai_mae, 6)
                                if smoothed_group_ai_mae
                                else None
                            ),
                            (
                                round(smoothed_group_lstm_mae, 6)
                                if smoothed_group_lstm_mae
                                else None
                            ),
                            (
                                round(smoothed_group_ridge_mae, 6)
                                if smoothed_group_ridge_mae
                                else None
                            ),
                            None,
                            smoothed_best_method,
                            round(group_ai_advantage_factor, 4),
                            sample_count,
                            now,
                        )
                        if bucket_season is None:
                            await database.execute(
                                "DELETE FROM group_method_performance\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (group_name, cloud_bucket, hour_bucket),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                learning_params,
                                auto_commit=False,
                            )
                        else:
                            await database.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       lstm_mae = excluded.lstm_mae,\n                                       ridge_mae = excluded.ridge_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       best_method = excluded.best_method,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                learning_params,
                                auto_commit=False,
                            )
                        updated_regime_count += 1
                    _LOGGER.info(
                        "Group method learning [%s/%s/%s]: physics=%.4f ai=%s lstm=%s ridge=%s best=%s advantage=%.2f (%d samples)",
                        group_name,
                        cloud_bucket,
                        hour_bucket,
                        physics_mae,
                        f"{ai_mae:.4f}" if ai_mae else "N/A",
                        f"{lstm_mae:.4f}" if lstm_mae else "N/A",
                        f"{ridge_mae:.4f}" if ridge_mae else "N/A",
                        best_regime_method,
                        ai_advantage_factor,
                        sample_count,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Group method performance learning completed for %s: %d buckets updated",
                today,
                updated_regime_count,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(
                f"Group method performance learning error: {error}", exc_info=True
            )
            return {"status": "failed", "reason": str(error)}

    async def _run_physics_calibration(self, now: datetime) -> Dict[str, str]:
        """Run physics engine calibration learning. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "physics_calibrator"
            ):
                reason = "no physics calibrator available"
                _LOGGER.info("Physics calibration skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            physics_calibrator = self.coordinator.physics_calibrator
            if not physics_calibrator:
                reason = "physics calibrator is unavailable"
                _LOGGER.info("Physics calibration skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            today = now.date().isoformat()
            if hasattr(physics_calibrator, "learn_from_day"):
                result = await physics_calibrator.learn_from_day(today)
                if result and result.get("success"):
                    _LOGGER.info(
                        f"Physics calibration SUCCESS for {today}: Updated {result.get('groups_updated', 0)} groups"
                    )
                    calibration_outcome = {"status": "succeeded", "reason": ""}
                else:
                    reason = f"no eligible physics calibration data for {today}"
                    _LOGGER.info("Physics calibration skipped: %s", reason)
                    calibration_outcome = {"status": "skipped", "reason": reason}
            else:
                reason = "physics calibrator does not support daily learning"
                _LOGGER.info("Physics calibration skipped: %s", reason)
                calibration_outcome = {"status": "skipped", "reason": reason}
            try:
                from ..physics.physics_calibration_telemetry import (
                    persist_ghi_sensor_geometry_diagnosis as persist_geometry_diagnosis,
                    prune_physics_calibration_telemetry as prune_calibration_telemetry,
                )

                await persist_geometry_diagnosis(self.db, today)
                await prune_calibration_telemetry(self.db, today)
            except Exception as geometry_diagnosis_error:
                _LOGGER.info(
                    "Physics calibration telemetry skipped: %s",
                    geometry_diagnosis_error,
                )
            return {
                "status": calibration_outcome["status"],
                "reason": calibration_outcome["reason"],
            }
        except Exception as error:
            _LOGGER.error(f"Physics calibration error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _run_weather_expert_learning(self, now: datetime) -> Dict[str, str]:
        """Learn weather expert weights from today's actual observations. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                reason = "no weather pipeline manager available"
                _LOGGER.info("Weather expert learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            pipeline_manager = self.coordinator.weather_pipeline_manager
            if not pipeline_manager:
                reason = "weather pipeline is unavailable"
                _LOGGER.info("Weather expert learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            if not pipeline_manager.weather_expert_learner:
                reason = "weather expert learner is not initialized"
                _LOGGER.info("Weather expert learning skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            today = now.strftime("%Y-%m-%d")
            expert_learner = pipeline_manager.weather_expert_learner
            frozen_weather_status = None
            if not getattr(expert_learner, "learning_enabled", True):
                frozen_weather_status = "weather_expert_immutable_learning_disabled"
                learning_disabled_reason = getattr(
                    expert_learner,
                    "learning_disabled_reason",
                    "immutable weather expert learning is unavailable",
                )
                _LOGGER.info(
                    "Weather expert learning skipped: %s", learning_disabled_reason
                )
            else:
                result = await expert_learner.learn_from_day(today)
                if result:
                    _LOGGER.info(f"Weather expert learning SUCCESS for {today}")
                    learning_disabled_reason = ""
                else:
                    learning_disabled_reason = (
                        f"insufficient weather expert learning data for {today}"
                    )
                    _LOGGER.info(
                        "Weather expert learning skipped: %s", learning_disabled_reason
                    )
            try:
                prediction_rows = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh FROM hourly_predictions\n                       WHERE target_date = ?",
                    (today,),
                )
                hourly_predictions = [
                    {
                        "prediction_id": row[0],
                        "target_hour": row[1],
                        "actual_kwh": row[2],
                    }
                    for row in prediction_rows
                ]
                snow_context = {}
                if pipeline_manager.weather_actual_tracker:
                    snow_context = await pipeline_manager.weather_actual_tracker.get_daily_actual_weather(
                        today
                    )
                if hourly_predictions:
                    snow_learning_result = await pipeline_manager.weather_expert_learner.learn_snow_prediction_accuracy(
                        today, hourly_predictions, snow_context
                    )
                    if snow_learning_result.get("total_snow_predicted", 0) > 0:
                        _LOGGER.info(
                            f"Snow prediction learning for {today}: {snow_learning_result.get('snow_confirmed', 0)}/{snow_learning_result.get('total_snow_predicted', 0)} confirmed ({snow_learning_result.get('accuracy', 0):.0%} accuracy)"
                        )
            except Exception as snow_learning_error:
                _LOGGER.error(
                    "Snow prediction learning failed: %s",
                    snow_learning_error,
                    exc_info=True,
                )
                return {"status": "failed", "reason": str(snow_learning_error)}
            if learning_disabled_reason:
                if frozen_weather_status:
                    return {
                        "status": "skipped",
                        "reason": learning_disabled_reason,
                        "reason_code": frozen_weather_status,
                        "impact": "informational",
                    }
                return {"status": "skipped", "reason": learning_disabled_reason}
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Weather expert learning error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _run_shadow_pattern_learning(self, now: datetime) -> Dict[str, str]:
        """Learn shadow patterns from today's shadow detection data. @zara V17.0.0

        Uses ShadowPatternLearner to:
        - Identify recurring shadow patterns per hour
        - Classify fixed obstructions vs variable shadows
        - Track seasonal variations
        - V17.0.0: Per-group shadow pattern learning
        """
        try:
            from ..data.data_shadow_detection import (
                get_performance_analyzer as performance_analyzer_factory,
            )
            from ..data.data_shadow_pattern_learning import (
                get_shadow_pattern_learner as shadow_learner_factory,
            )

            shadow_learner = shadow_learner_factory(self.hass, self.db)
            if not shadow_learner._initialized:
                learner_initialized = await shadow_learner.async_init()
                if not learner_initialized:
                    reason = "shadow pattern learner initialization failed"
                    _LOGGER.warning(reason)
                    return {"status": "failed", "reason": reason}
            today = now.strftime("%Y-%m-%d")
            learned_pattern_count = 0
            learned_group_count = 0
            result = await shadow_learner.learn_from_day(today)
            if result.success:
                learned_pattern_count += 1
                _LOGGER.info(
                    f"Shadow pattern learning SUCCESS [_system_] for {today}: {result.hours_learned} hours learned, {result.patterns_updated} patterns updated, {result.fixed_obstructions_found} fixed obstructions"
                )
                group_learning_summary = shadow_learner.get_learning_summary()
                fixed_obstruction_hours = group_learning_summary.get(
                    "fixed_obstruction_hours", []
                )
                if fixed_obstruction_hours:
                    _LOGGER.info(
                        f"Fixed obstruction hours detected [_system_]: {fixed_obstruction_hours}"
                    )
            else:
                if result.reason_code != "shadow_detection_data_unavailable":
                    reason = result.message or "system shadow pattern learning failed"
                    return {"status": "failed", "reason": f"System: {reason}"}
                learned_group_count += 1
                reason = result.message or f"no system shadow data for {today}"
                _LOGGER.info(
                    "Shadow pattern learning skipped [_system_] for %s: %s",
                    today,
                    reason,
                )
            panel_groups = getattr(self.coordinator, "panel_groups", [])
            if panel_groups:
                groups_with_fixed_obstruction = 0
                for group_config in panel_groups:
                    group_name = (
                        group_config.name
                        if hasattr(group_config, "name")
                        else group_config.get("name", "")
                    )
                    if not group_name:
                        continue
                    try:
                        group_learning_result = await shadow_learner.learn_from_day(
                            today, group_name=group_name
                        )
                        if group_learning_result.success:
                            groups_with_fixed_obstruction += 1
                            learned_pattern_count += 1
                            _LOGGER.info(
                                f"Shadow pattern learning SUCCESS [{group_name}] for {today}: {group_learning_result.hours_learned} hours, {group_learning_result.patterns_updated} patterns, {group_learning_result.fixed_obstructions_found} fixed obstructions"
                            )
                            continue
                        if (
                            group_learning_result.reason_code
                            != "shadow_detection_data_unavailable"
                        ):
                            reason = (
                                group_learning_result.message
                                or "shadow pattern learning failed"
                            )
                            return {
                                "status": "failed",
                                "reason": f"{group_name}: {reason}",
                            }
                        learned_group_count += 1
                        reason = (
                            group_learning_result.message
                            or f"no shadow data for {group_name}"
                        )
                        _LOGGER.info(
                            "Shadow pattern learning skipped [%s] for %s: %s",
                            group_name,
                            today,
                            reason,
                        )
                    except Exception as error:
                        _LOGGER.warning(
                            f"Shadow pattern learning failed [{group_name}]: {error}"
                        )
                        return {"status": "failed", "reason": f"{group_name}: {error}"}
                    continue
                if groups_with_fixed_obstruction > 0:
                    _LOGGER.info(
                        f"Per-group shadow pattern learning: {groups_with_fixed_obstruction}/{len(panel_groups)} groups learned"
                    )
            try:
                peer_persistence = await performance_analyzer_factory(
                    self.hass, self.db
                ).analyze_group_peer_persistence(as_of_date=today)
                self.coordinator.shadow_peer_persistence = peer_persistence
                for group_name, peer_group_status in peer_persistence.get(
                    "groups", {}
                ).items():
                    peer_observation_count = int(
                        peer_group_status.get("peer_candidate_observations", 0)
                    )
                    if peer_observation_count <= 0:
                        continue
                    _LOGGER.info(
                        "Shadow-only peer persistence [%s]: %d/%d observations across %d/%d days, median peer gap %.1f%%, fusion context %s; promotion_eligible=false",
                        group_name,
                        peer_observation_count,
                        int(peer_group_status.get("evaluated_observations", 0)),
                        int(peer_group_status.get("peer_candidate_days", 0)),
                        int(peer_group_status.get("evaluated_days", 0)),
                        float(peer_group_status.get("median_peer_gap_percent", 0.0)),
                        peer_group_status.get("fusion_cause_counts", {}),
                    )
            except Exception as peer_analysis_error:
                self.coordinator.shadow_peer_persistence = {
                    "mode": "shadow_only",
                    "error": str(peer_analysis_error),
                    "promotion_eligible": False,
                }
                _LOGGER.warning(
                    "Shadow-only peer persistence analysis failed: %s",
                    peer_analysis_error,
                    exc_info=True,
                )
            try:
                sensorless_persistence = await performance_analyzer_factory(
                    self.hass, self.db
                ).analyze_sensorless_observation_persistence(as_of_date=today)
                self.coordinator.shadow_sensorless_persistence = sensorless_persistence
                _LOGGER.info(
                    "Sensorless shadow observation stand: evaluable=%d/%d, ready_bins=%d/%d; promotion_eligible=false",
                    int(sensorless_persistence.get("evaluable_hours", 0)),
                    int(sensorless_persistence.get("source_hours", 0)),
                    int(sensorless_persistence.get("ready_bins", 0)),
                    int(sensorless_persistence.get("bin_count", 0)),
                )
            except Exception as sensorless_analysis_error:
                self.coordinator.shadow_sensorless_persistence = {
                    "mode": "shadow_only",
                    "error": str(sensorless_analysis_error),
                    "promotion_eligible": False,
                }
                _LOGGER.warning(
                    "Sensorless shadow observation stand failed: %s",
                    sensorless_analysis_error,
                    exc_info=True,
                )
            if learned_pattern_count > 0:
                return {"status": "succeeded", "reason": ""}
            if learned_group_count > 0:
                return {
                    "status": "skipped",
                    "reason": "No eligible shadow observations were available today",
                    "reason_code": "shadow_detection_data_unavailable",
                    "impact": "informational",
                }
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Shadow pattern learning error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _run_drift_detection(self, now: datetime) -> Dict[str, str]:
        """Run drift detection and metric calculation. @zara V17.0.0

        Calculates rolling metrics (MAE, RMSE, Bias, Coverage),
        bucket-specific drift, CUSUM detection, and automatic responses.
        Updates coordinator cache for drift sensor.
        """
        try:
            drift_monitor = getattr(self.coordinator, "drift_monitor", None)
            if not drift_monitor:
                reason = "no drift monitor available"
                _LOGGER.info("Drift detection skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            today = now.strftime("%Y-%m-%d")
            result = await drift_monitor.calculate_rolling_metrics(today)
            failed_metric_count = int(result.get("metrics_failed", 0))
            failed_operation_count = int(result.get("operations_failed", 0))
            if failed_metric_count:
                reason = f"{failed_metric_count} drift rolling metric calculations failed for {today}"
                _LOGGER.error("Drift detection failed: %s", reason)
                return {"status": "failed", "reason": reason}
            if failed_operation_count:
                reason = f"{failed_operation_count} drift adaptive operations failed for {today}"
                _LOGGER.error("Drift detection failed: %s", reason)
                return {"status": "failed", "reason": reason}
            if result.get("metrics_saved", 0) == 0:
                reason = f"no eligible drift metrics available for {today}"
                _LOGGER.info("Drift detection skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            _LOGGER.info(
                "Drift detection completed for %s: %d metrics, %d events, %d scopes",
                today,
                result.get("metrics_saved", 0),
                result.get("events_created", 0),
                result.get("scopes_checked", 0),
            )
            fresh_global_bias = bool(result.get("global_bias_fresh", False))
            fresh_global_clamp = bool(result.get("global_clamp_fresh", False))
            try:
                previous_bias_correction = self.coordinator.drift_bias_correction
                previous_solar_clamp_min = self.coordinator._drift_solar_clamp_min
                if fresh_global_bias:
                    new_bias_correction = drift_monitor.get_global_bias_correction()
                    self.coordinator.drift_bias_correction = new_bias_correction
                    if abs(new_bias_correction - previous_bias_correction) > 0.005:
                        _LOGGER.info(
                            "Drift bias correction updated on coordinator: %.4f → %.4f",
                            previous_bias_correction,
                            new_bias_correction,
                        )
                if fresh_global_clamp:
                    new_solar_clamp_min = drift_monitor.get_solar_clamp_min()
                    self.coordinator._drift_solar_clamp_min = new_solar_clamp_min
                    corrector = (
                        self._pipeline_corrector or self._get_weather_corrector()
                    )
                    if corrector is not None:
                        corrector.set_solar_clamp_min(new_solar_clamp_min)
                        if abs(new_solar_clamp_min - previous_solar_clamp_min) > 0.001:
                            _LOGGER.info(
                                "Solar clamp updated on corrector: %.3f → %.3f",
                                previous_solar_clamp_min,
                                new_solar_clamp_min,
                            )
            except Exception as error:
                _LOGGER.error("Failed to propagate drift correction factors: %s", error)
                return {"status": "failed", "reason": str(error)}
            try:
                status = await drift_monitor.get_status()
                self.coordinator._drift_status_cache = status
            except Exception as error:
                _LOGGER.error("Failed to cache drift status: %s", error)
                return {"status": "failed", "reason": str(error)}
            predictor = getattr(self.coordinator, "ai_predictor", None)
            if predictor is not None:
                model_reaction = await self.model_reaction_handler.process(
                    today, predictor
                )
                if model_reaction.status == "failed":
                    _LOGGER.error(
                        "Confirmed model drift reaction failed: %s",
                        model_reaction.reason,
                    )
                    return {"status": "failed", "reason": model_reaction.reason}
                if model_reaction.status in {"bound", "deferred", "replaced"}:
                    _LOGGER.info(
                        "Model drift reaction: status=%s request=%s candidate=%s reason=%s",
                        model_reaction.status,
                        model_reaction.request_id,
                        model_reaction.candidate_id,
                        model_reaction.reason,
                    )
            return {"status": "succeeded", "reason": ""}
        except Exception as error:
            _LOGGER.error(f"Drift detection error: {error}", exc_info=True)
            return {"status": "failed", "reason": str(error)}

    async def _create_daily_summary(self, date: str) -> Dict[str, str]:
        """Create daily summary using DailySummariesHandler. @zara

        Creates comprehensive daily summary including:
        - Main daily_summaries table
        - Time windows analysis (morning, midday, afternoon)
        - Frost analysis
        - Shadow analysis
        """
        try:
            rows = await self.db.fetchall(
                "SELECT hp.prediction_id, hp.target_datetime, hp.target_hour,\n                          hp.target_date, hp.prediction_kwh, hp.actual_kwh,\n                          hp.accuracy_percent, hp.error_kwh, hp.error_percent,\n                          hp.exclude_from_learning, hp.has_weather_alert,\n                          hp.inverter_clipped, hp.mppt_throttled,\n                          hp.weather_alert_type, hp.exclude_from_clean_evaluation,\n                          pwf.temperature, pwf.solar_radiation_wm2, pwf.wind,\n                          pwf.humidity, pwf.rain, pwf.clouds, pwf.pressure,\n                          pwa.temperature, pwa.solar_radiation_wm2, pwa.wind,\n                          pwa.humidity, pwa.rain, pwa.clouds, pwa.pressure,\n                          COALESCE(pwa.frost_detected, hwa.frost_detected),\n                          COALESCE(pwa.frost_score, hwa.frost_score),\n                          COALESCE(pwa.frost_confidence, hwa.frost_confidence),\n                          hwa.temperature_c, hwa.solar_radiation_wm2,\n                          hwa.wind_speed_ms, hwa.humidity_percent,\n                          hwa.precipitation_mm, hwa.pressure_hpa,\n                          hwa.cloud_cover_percent, hwa.snow_covered_panels\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwf ON hp.prediction_id = pwf.prediction_id\n                        AND pwf.weather_type = 'forecast'\n                   LEFT JOIN prediction_weather pwa ON hp.prediction_id = pwa.prediction_id\n                        AND pwa.weather_type = 'actual'\n                   LEFT JOIN hourly_weather_actual hwa ON hp.target_date = hwa.date\n                        AND hp.target_hour = hwa.hour\n                   WHERE hp.target_date = ?\n                   ORDER BY hp.target_hour",
                (date,),
            )
            if not rows:
                reason = f"no hourly predictions available for {date}"
                _LOGGER.info("Daily summary skipped: %s", reason)
                return {"status": "skipped", "reason": reason}
            hourly_predictions = []
            for row in rows:
                weather_forecast = (
                    {
                        "temperature": row[15],
                        "temperature_c": row[15],
                        "solar_radiation_wm2": row[16],
                        "wind": row[17],
                        "humidity": row[18],
                        "rain": row[19],
                        "precipitation_mm": row[19],
                        "clouds": row[20],
                        "cloud_cover_percent": row[20],
                        "pressure": row[21],
                        "pressure_hpa": row[21],
                    }
                    if any((weather_value is not None for weather_value in row[15:22]))
                    else None
                )
                actual_temperature_c = row[22] if row[22] is not None else row[32]
                actual_radiation_wm2 = row[23] if row[23] is not None else row[33]
                actual_wind_speed_ms = row[24] if row[24] is not None else row[34]
                actual_humidity_percent = row[25] if row[25] is not None else row[35]
                actual_rain_mm = row[26] if row[26] is not None else row[36]
                actual_clouds_percent = row[27] if row[27] is not None else row[38]
                actual_pressure_hpa = row[28] if row[28] is not None else row[37]
                weather_actual = (
                    {
                        "temperature": actual_temperature_c,
                        "temperature_c": actual_temperature_c,
                        "solar_radiation_wm2": actual_radiation_wm2,
                        "wind": actual_wind_speed_ms,
                        "wind_speed_ms": actual_wind_speed_ms,
                        "humidity": actual_humidity_percent,
                        "humidity_percent": actual_humidity_percent,
                        "rain": actual_rain_mm,
                        "precipitation_mm": actual_rain_mm,
                        "clouds": actual_clouds_percent,
                        "cloud_cover_percent": actual_clouds_percent,
                        "pressure": actual_pressure_hpa,
                        "pressure_hpa": actual_pressure_hpa,
                        "frost_detected": row[29],
                        "frost_score": row[30],
                        "frost_confidence": row[31],
                        "snow_covered_panels": row[39],
                    }
                    if any((weather_value is not None for weather_value in row[22:40]))
                    else None
                )
                hourly_prediction = {
                    "prediction_id": row[0],
                    "target_datetime": row[1],
                    "target_hour": row[2],
                    "target_date": row[3],
                    "prediction_kwh": row[4],
                    "actual_kwh": row[5],
                    "accuracy_percent": row[6],
                    "error_kwh": row[7],
                    "error_percent": row[8],
                    "exclude_from_learning": (
                        bool(row[9]) if row[9] is not None else False
                    ),
                    "has_weather_alert": (
                        bool(row[10]) if row[10] is not None else False
                    ),
                    "inverter_clipped": bool(row[11]) if row[11] is not None else False,
                    "mppt_throttled": bool(row[12]) if row[12] is not None else False,
                    "weather_alert_type": row[13],
                    "exclude_from_clean_evaluation": (
                        bool(row[14]) if row[14] is not None else None
                    ),
                    "weather_forecast": weather_forecast,
                    "weather_actual": weather_actual,
                }
                hourly_predictions.append(hourly_prediction)
                continue
            success = await self.daily_summaries_handler.create_daily_summary(
                date, hourly_predictions
            )
            if success:
                _LOGGER.info(
                    "Daily summary created for %s using DailySummariesHandler", date
                )
                return {"status": "succeeded", "reason": ""}
            reason = f"DailySummariesHandler failed for {date}"
            _LOGGER.warning(reason)
            return {"status": "failed", "reason": reason}
        except Exception as error:
            _LOGGER.error("Error creating daily summary: %s", error, exc_info=True)
            return {"status": "failed", "reason": str(error)}

    @callback
    def _adaptive_forecast_check(self, now: datetime) -> None:
        """Handle adaptive forecast check. @zara"""
        self.hass.async_create_task(self._execute_adaptive_forecast_check())

    async def _execute_adaptive_forecast_check(self) -> None:
        """Execute adaptive forecast check. @zara"""
        try:
            await self.adaptive_forecast_engine.run_midday_check()
        except Exception as error:
            _LOGGER.error("Adaptive forecast check failed: %s", error, exc_info=True)
        return None if True else None

    async def _execute_scheduled_operational_reforecast(self, now: datetime) -> None:
        """Execute scheduled operational reforecast when the configured mode matches. @zara"""
        try:
            if (
                self._operational_reforecast_retry_at is not None
                and now < self._operational_reforecast_retry_at
            ):
                return
            pending_reforecast = self._pending_operational_reforecast
            if pending_reforecast is None:
                settings = await self._get_operational_reforecast_settings()
                if not settings.get("enabled", False):
                    return
                scheduled_times = self._resolve_operational_reforecast_times(settings)
                if not scheduled_times or (now.hour, now.minute) not in scheduled_times:
                    await self._maybe_trigger_fast_weather_event_reforecast(now)
                    return
                scheduled_slot = (
                    f"{now.date().isoformat()}_{now.hour:02d}:{now.minute:02d}"
                )
                if scheduled_slot == self._last_operational_reforecast_trigger_key:
                    return
                pending_reforecast = {
                    "key": scheduled_slot,
                    "mode": settings.get("mode"),
                    "cutoff_hour": now.hour,
                    "scheduled_at": now,
                }
                self._pending_operational_reforecast = pending_reforecast
            _LOGGER.info(
                "Scheduled operational reforecast triggered for mode=%s at %02d:%02d",
                pending_reforecast.get("mode"),
                pending_reforecast["scheduled_at"].hour,
                pending_reforecast["scheduled_at"].minute,
            )
            success = await self.operational_reforecast_engine.run_reforecast(
                trigger_source="scheduled_mode",
                cutoff_hour=pending_reforecast["cutoff_hour"],
                now=now,
            )
            if not success:
                raise RuntimeError(
                    "operational reforecast returned no successful result"
                )
            self._last_operational_reforecast_trigger_key = pending_reforecast["key"]
            self._pending_operational_reforecast = None
            self._operational_reforecast_failures = 0
            self._operational_reforecast_retry_at = None
            self._last_operational_reforecast_error = None
            self._fast_weather_event_confirmations = 0
            self.coordinator.clear_health_issue("operational_reforecast")
        except Exception as error:
            self._operational_reforecast_failures += 1
            retry_minutes = min(2 ** (self._operational_reforecast_failures - 1), 30)
            self._operational_reforecast_retry_at = now + timedelta(
                minutes=retry_minutes
            )
            last_error = str(error)
            if last_error != self._last_operational_reforecast_error:
                _LOGGER.error(
                    "Operational reforecast failed; retrying in %d minute(s): %s",
                    retry_minutes,
                    error,
                    exc_info=True,
                )
                self._last_operational_reforecast_error = last_error
            self.coordinator.set_health_issue(
                "operational_reforecast",
                f"Operational reforecast retry pending after database failure: {last_error}",
            )
        return None if True else None

    async def _maybe_trigger_fast_weather_event_reforecast(self, now: datetime) -> None:
        """Detect a strong live radiation miss before the hourly persistence cycle."""
        if now.minute not in FAST_WEATHER_EVENT_CHECK_MINUTES:
            return
        if now.hour < 6 or now.hour > 19:
            self._fast_weather_event_confirmations = 0
            return
        event_slot = f"{now.date().isoformat()}_{now.hour:02d}:{now.minute:02d}"
        if event_slot == self._fast_weather_event_last_sample_key:
            return
        self._fast_weather_event_last_sample_key = event_slot
        if self._fast_weather_event_lock.locked():
            return
        async with self._fast_weather_event_lock:
            actual_radiation_wm2 = self._read_fresh_live_solar_radiation(now)
            active_forecast_radiation_wm2 = (
                await self._get_active_ops_radiation_for_hour(
                    now.date().isoformat(), now.hour
                )
            )
            if not _is_fast_weather_event_sample(
                actual_radiation_wm2, active_forecast_radiation_wm2
            ):
                self._fast_weather_event_confirmations = 0
                return
            self._fast_weather_event_confirmations += 1
            if (
                self._fast_weather_event_confirmations
                < FAST_WEATHER_EVENT_CONFIRMATIONS
            ):
                _LOGGER.debug(
                    "Fast weather-event candidate: actual=%.1f W/m² forecast=%.1f W/m² confirmation=%d/%d",
                    actual_radiation_wm2,
                    active_forecast_radiation_wm2,
                    self._fast_weather_event_confirmations,
                    FAST_WEATHER_EVENT_CONFIRMATIONS,
                )
                return
            if self._last_weather_event_reforecast_at is not None:
                if now - self._last_weather_event_reforecast_at < timedelta(hours=1):
                    return
            completed_hour = max(-1, now.hour - 1)
            future_forecast_row = await self.db.fetchone(
                "SELECT 1 FROM ops_hourly_forecasts\n                   WHERE target_date = ?\n                     AND target_hour > ?\n                     AND is_active = TRUE\n                   LIMIT 1",
                (now.date().isoformat(), completed_hour),
            )
            if not future_forecast_row:
                self._fast_weather_event_confirmations = 0
                return
            if self._kalman_tracker is None:
                _LOGGER.debug(
                    "Fast weather-event check skipped: Kalman tracker unavailable"
                )
                self._fast_weather_event_confirmations = 0
                return
            morning_radiation_anchor = await self._get_kalman_update_anchor(
                now.date().isoformat(), now.hour
            )
            if morning_radiation_anchor is not None:
                self._kalman_tracker.update(
                    float(actual_radiation_wm2) - morning_radiation_anchor
                )
            excluded_ratio = actual_radiation_wm2 / active_forecast_radiation_wm2
            _LOGGER.info(
                "Fast weather-event OPS reforecast triggered: hour=%02d actual=%.1f W/m² forecast=%.1f W/m² ratio=%.3f",
                now.hour,
                actual_radiation_wm2,
                active_forecast_radiation_wm2,
                excluded_ratio,
            )
            success = await self.operational_reforecast_engine.run_reforecast(
                trigger_source="live_sensor_fast_path:unexpected_clouds",
                cutoff_hour=completed_hour,
                now=now,
                snapshot_type="weather_event_reforecast",
            )
            if success:
                self._last_weather_event_reforecast_at = now
                self._last_weather_event_reforecast_hour_key = (
                    f"{now.date().isoformat()}_{now.hour:02d}_fast_unexpected_clouds"
                )
                self._fast_weather_event_confirmations = 0
        return None if True else None

    def _read_fresh_live_solar_radiation(self, now: datetime) -> Optional[float]:
        """Read a configured, fresh radiation sensor without persisting a sample."""
        entity_id = self._get_sensor_config().get("solar_radiation_sensor")
        if not entity_id or not isinstance(entity_id, str):
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0:
            return None
        last_updated = getattr(state, "last_updated", None)
        if last_updated is None or now.tzinfo is None or last_updated.tzinfo is None:
            return None
        sensor_age = now - last_updated
        if sensor_age < timedelta(0) or sensor_age > timedelta(
            minutes=FAST_WEATHER_EVENT_MAX_SENSOR_AGE_MINUTES
        ):
            return None
        return value

    async def _get_active_ops_radiation_for_hour(
        self, target_date: str, target_hour: int
    ) -> Optional[float]:
        """Return the currently published operational radiation for one hour."""
        row = await self.db.fetchone(
            "SELECT ow.solar_radiation_wm2\n               FROM ops_prediction_weather ow\n               JOIN ops_hourly_forecasts oh\n                 ON oh.forecast_row_id = ow.forecast_row_id\n               WHERE ow.target_date = ?\n                 AND ow.target_hour = ?\n                 AND ow.is_active = TRUE\n                 AND oh.is_active = TRUE\n               ORDER BY ow.snapshot_sequence DESC\n               LIMIT 1",
            (target_date, target_hour),
        )
        if not row or row[0] is None:
            row = await self.db.fetchone(
                "SELECT solar_radiation_wm2 FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (target_date, target_hour),
            )
        if not row or row[0] is None:
            return
        try:
            value = float(row[0])
        except (TypeError, ValueError):
            return
        return value if math.isfinite(value) and value >= 0 else None

    def _has_local_solar_radiation_sensor(self) -> bool:
        """Return True when a local GHI/solar-radiation sensor is configured."""
        pipeline_manager = getattr(self.coordinator, "weather_pipeline_manager", None)
        weather_tracker = (
            getattr(pipeline_manager, "weather_actual_tracker", None)
            if pipeline_manager
            else None
        )
        if weather_tracker is None:
            data_manager = getattr(self.coordinator, "data_manager", None)
            weather_tracker = getattr(data_manager, "weather_actual_tracker", None)
        return bool(getattr(weather_tracker, "has_solar_radiation_sensor", False))

    async def _get_actual_radiation_for_hour(
        self, target_date: str, target_hour: int
    ) -> Optional[float]:
        """Return measured GHI for one hour, or None when unavailable."""
        snow_date = f"{target_date}_{target_hour:02d}"
        row = await self.db.fetchone(
            "SELECT solar_radiation_wm2\n               FROM prediction_weather\n               WHERE prediction_id = ?\n                 AND weather_type = 'actual'",
            (snow_date,),
        )
        value = self._finite_non_negative(row[0] if row else None)
        if value is not None:
            return ((value if True else None) if True else None) if True else None
        row = await self.db.fetchone(
            "SELECT solar_radiation_wm2\n               FROM hourly_weather_actual\n               WHERE date = ? AND hour = ?",
            (target_date, target_hour),
        )
        return (
            (
                (self._finite_non_negative(row[0] if row else None) if True else None)
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def _finite_non_negative(raw: Any) -> Optional[float]:
        if raw is None:
            return
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return
        return value if math.isfinite(value) and value >= 0 else None

    async def _maybe_trigger_weather_event_reforecast(
        self, target_date: str, target_hour: int
    ) -> None:
        try:
            settings = await self._get_operational_reforecast_settings()
            if not settings.get("enabled", False):
                _LOGGER.info(
                    "Weather-event OPS reforecast skipped: customer operational reforecast setting is disabled (target=%s hour=%02d)",
                    target_date,
                    target_hour,
                )
                return
            row = await self.db.fetchone(
                "SELECT weather_alert_type, weather_miss_class, reforecast_trigger_eligible,\n                          actual_kwh, prediction_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND target_hour = ?",
                (target_date, target_hour),
            )
            if not row:
                return
            alert_type = row["weather_alert_type"]
            weather_miss_class = row["weather_miss_class"]
            reforecast_eligible = bool(row["reforecast_trigger_eligible"])
            if not reforecast_eligible or weather_miss_class != "local_event":
                return None
            current_time = dt_util.now()
            prediction_id = f"{target_date}_{target_hour:02d}_{alert_type}"
            if prediction_id == self._last_weather_event_reforecast_hour_key:
                return
            if (
                self._last_weather_event_reforecast_at is not None
                and current_time - self._last_weather_event_reforecast_at
                < timedelta(hours=1)
            ):
                _LOGGER.info(
                    "Weather-event OPS reforecast skipped by cooldown: alert=%s hour=%s",
                    alert_type,
                    prediction_id,
                )
                return
            future_forecast_row = await self.db.fetchone(
                "SELECT 1\n                   FROM ops_hourly_forecasts\n                   WHERE target_date = ?\n                     AND target_hour > ?\n                     AND is_active = TRUE\n                   LIMIT 1",
                (target_date, target_hour),
            )
            if not future_forecast_row:
                _LOGGER.debug(
                    "Weather-event OPS reforecast skipped: no active future OPS rows for %s after %02d",
                    target_date,
                    target_hour,
                )
                return
            _LOGGER.info(
                "Weather-event OPS reforecast triggered: alert=%s class=%s cutoff_hour=%02d",
                alert_type,
                weather_miss_class,
                target_hour,
            )
            rest_of_day_uplift = 1.0
            if alert_type == WEATHER_ALERT_UNEXPECTED_SUN:
                hour_uplift = (
                    production_miss_uplift(row["actual_kwh"], row["prediction_kwh"])
                    or 1.0
                )
                rest_of_day_uplift = rest_of_day_uplift_from_weather_evidence(
                    hour_uplift,
                    has_local_ghi_sensor=self._has_local_solar_radiation_sensor(),
                    actual_ghi_wm2=await self._get_actual_radiation_for_hour(
                        target_date, target_hour
                    ),
                    forecast_ghi_wm2=await self._get_active_ops_radiation_for_hour(
                        target_date, target_hour
                    ),
                )
                if rest_of_day_uplift <= 1.0 and hour_uplift > 1.0:
                    _LOGGER.info(
                        "Weather-event OPS rest-of-day scalar skipped: no GHI confirmation for alert=%s hour=%s (production_uplift=%.3f)",
                        alert_type,
                        prediction_id,
                        hour_uplift,
                    )
            success = await self.operational_reforecast_engine.run_reforecast(
                trigger_source=f"unexpected_weather:{alert_type}",
                cutoff_hour=target_hour,
                now=current_time,
                snapshot_type="weather_event_reforecast",
                rest_of_day_uplift=rest_of_day_uplift,
            )
            if success:
                self._last_weather_event_reforecast_at = current_time
                self._last_weather_event_reforecast_hour_key = prediction_id
        except Exception as error:
            _LOGGER.error(
                "Weather-event OPS reforecast trigger failed: %s", error, exc_info=True
            )
        return None if True else None

    async def _get_operational_reforecast_settings(self) -> Dict[str, Any]:
        """Load current operational reforecast settings from the sidecar handler. @zara"""
        data_manager = getattr(self.coordinator, "data_manager", None)
        operational_forecast_handler = getattr(
            data_manager, "operational_forecast_handler", None
        )
        if operational_forecast_handler is None:
            return {
                "mode": OPERATIONAL_REFORECAST_MODE_STANDARD,
                "custom_time": None,
                "enabled": False,
            }
        return await operational_forecast_handler.get_reforecast_settings()

    def _resolve_operational_reforecast_times(
        self, settings: Dict[str, Any]
    ) -> List[tuple[int, int]]:
        """Resolve configured operational reforecast times for the current mode. @zara"""
        mode = settings.get("mode") or OPERATIONAL_REFORECAST_MODE_STANDARD
        custom_time = settings.get("custom_time")
        if mode == OPERATIONAL_REFORECAST_MODE_STANDARD:
            return []
        if mode == OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY:
            return [(12, 0)]
        if mode == OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON:
            return [(12, 0), (16, 0)]
        if mode == OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME and custom_time:
            try:
                hour_text, minute_text = str(custom_time).split(":", 1)
                return [(int(hour_text), int(minute_text))]
            except ValueError:
                _LOGGER.warning(
                    "Invalid operational reforecast custom_time in settings: %s",
                    custom_time,
                )
                return []
        return []

    @callback
    def _monthly_report_task(self, now: datetime) -> None:
        """Handle the monthly report schedule."""
        self.hass.async_create_task(self.generate_monthly_report(now))

    async def _refresh_system_report(self) -> bool:
        """Rewrite system_report.md from current DB. Does not consume the monthly slot."""
        if not self.coordinator or not getattr(self.coordinator, "data_manager", None):
            _LOGGER.info("System report refresh skipped: no data manager")
            return False
        data_dir = self.coordinator.data_manager.data_dir
        report_generator = SystemReportGenerator(data_dir, self.db)
        success = await report_generator.generate_report()
        if success:
            _LOGGER.info("System report refreshed from current telemetry")
        return success

    async def generate_monthly_report(self, now: datetime) -> bool:
        """Generate the monthly report once per calendar month with catch-up."""
        run_time = now if now is not None else dt_util.now()
        month_key = run_time.strftime("%Y-%m")
        report_period_key = f"{month_key}:v{MONTHLY_REPORT_FORMAT_VERSION}"
        async with self._monthly_report_lock:
            try:
                completed_period = await self.db.get_scheduled_task_completed_period(
                    MONTHLY_REPORT_TASK_KEY
                )
                if completed_period == report_period_key:
                    return False
                await self.db.mark_scheduled_task_attempt(
                    MONTHLY_REPORT_TASK_KEY, run_time.isoformat()
                )
                _LOGGER.info(
                    "Generating monthly system report for period %s...", month_key
                )
                data_dir = self.coordinator.data_manager.data_dir
                report_generator = SystemReportGenerator(data_dir, self.db)
                success = await report_generator.generate_report()
                if not success:
                    _LOGGER.warning(
                        "Monthly system report generation failed for period %s",
                        month_key,
                    )
                    return False
                eod_completed_at = dt_util.now().isoformat()
                await self.db.mark_scheduled_task_completed(
                    MONTHLY_REPORT_TASK_KEY, report_period_key, eod_completed_at
                )
                _LOGGER.info(
                    "Monthly system report generated successfully for period %s",
                    month_key,
                )
                return True
            except Exception as error:
                _LOGGER.error(
                    "Error generating monthly report: %s", error, exc_info=True
                )
                return False
        if True:
            return

    def _remove_time_listeners(self) -> None:
        """Unsubscribe every recurring and sunrise-based time trigger. @zara"""
        for remove_listener in self._listeners + self._dynamic_listeners:
            try:
                remove_listener()
            except Exception:
                continue
        self._listeners = []
        self._dynamic_listeners = []

    async def cancel_all_tasks(self) -> None:
        """Cancel all scheduled tasks. @zara"""
        for listener_attribute in ("_stop_listener", "_startup_listener"):
            remove_listener = getattr(self, listener_attribute)
            if not remove_listener is not None:
                continue
            try:
                remove_listener()
            except Exception:
                pass
            setattr(self, listener_attribute, None)
        self._remove_time_listeners()
        _LOGGER.info("All scheduled tasks cancelled")
