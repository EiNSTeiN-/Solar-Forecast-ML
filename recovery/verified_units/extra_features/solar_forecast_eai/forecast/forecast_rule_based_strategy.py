# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Rule-Based Forecast Strategy Module.
Thermodynamics-based heat pump consumption forecast with AI hybrid blending.

Uses ThermodynamicsEngine for physics baseline (COP curves, heating demand,
wind chill, defrost penalties) and blends with LSTM/Ridge AI predictions
when ML models are trained and available.

@zara
"""
import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.util import dt as dt_util
from ..const import (
    CONF_FLOW_TEMP_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_OPERATION_MODE_ENTITY,
    CONF_TARGET_TEMP_ENTITY,
    DEFAULT_COP_RATED,
    DEFAULT_WP_TYPE,
    DOMAIN,
    SUPPORTED_WP_TYPES,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util_safe, get_season
from ..data.db_manager import DatabaseManager
from ..setup_state import cop_sink_kwargs, dhw_forecast_kwargs
from ..thermodynamics import (
    ThermodynamicsCalibrator,
    ThermodynamicsEngine,
    WeatherConditions,
)
from ..thermodynamics.thermodynamics_engine import (
    DEFAULT_INDOOR_TEMP,
    local_calendar_date,
    space_heating_suppressed_by_day,
)
from .forecast_strategy_base import ForecastResult, ForecastStrategy
from .forecast_weather_calculator import WeatherCalculator

_LOGGER = logging.getLogger(__name__)
MAX_AI_THERMO_DEVIATION = 0.5
ENSEMBLE_LSTM_WEIGHT = 0.85
ENSEMBLE_RIDGE_WEIGHT = 0.15
DISAGREEMENT_THRESHOLD = 0.3
DISAGREEMENT_CONFIDENCE_PENALTY = 0.7
RIDGE_CONFIRMS_THRESHOLD = 0.5
FORECAST_INDOOR_TEMP_MIN_C = 5.0
FORECAST_INDOOR_TEMP_MAX_C = 30.0


class RuleBasedForecastStrategy(ForecastStrategy):
    """Thermodynamics-based forecast strategy with AI hybrid blending. @zara"""

    def __init__(
        self,
        weather_calculator: WeatherCalculator,
        heating_capacity_kw: float,
        db_manager: DatabaseManager,
        orchestrator: Optional[Any] = None,
        ai_predictor: Optional[Any] = None,
        wp_type: str = DEFAULT_WP_TYPE,
        cop_rated: float = DEFAULT_COP_RATED,
    ):
        "Initialize strategy. @zara\n\nArgs:\n    weather_calculator: Weather calculation utilities\n    heating_capacity_kw: Heat pump heating capacity in kW\n    db_manager: Database manager for DB operations\n    orchestrator: Parent orchestrator reference\n    ai_predictor: AI predictor instance\n"
        super().__init__("rule_based")
        self.weather_calculator = weather_calculator
        self.heating_capacity_kw = heating_capacity_kw
        self._db = db_manager
        self.orchestrator = orchestrator
        self.ai_predictor = ai_predictor
        if wp_type not in SUPPORTED_WP_TYPES:
            raise ValueError(f"Unsupported heat pump type: {wp_type}")
        if not (isfinite(float(cop_rated)) and 1.0 <= float(cop_rated) <= 10.0):
            raise ValueError("Rated COP must be finite and between 1.0 and 10.0")
        self.wp_type = wp_type
        self.cop_rated = float(cop_rated)
        self._thermodynamics_engine = None
        self._thermo_init_attempted = False
        self._run_u_factor = None
        self._hour_is_dhw_only = False
        self._thermodynamics_calibrator = None
        self._use_ai = False
        if ai_predictor:
            _LOGGER.info("Thermodynamics+ML Strategy: Local AI available")
        _LOGGER.debug(
            "RuleBasedForecastStrategy initialized (capacity=%.1f kW)",
            heating_capacity_kw,
        )

    def set_thermodynamics_calibrator(
        self, calibrator: ThermodynamicsCalibrator
    ) -> None:
        """Set the thermodynamics calibrator for self-learning corrections. @zara"""
        self._thermodynamics_calibrator = calibrator
        _LOGGER.info("ThermodynamicsCalibrator connected to forecast strategy")

    def set_weather_actual_tracker(self, tracker: Any) -> None:
        """Set the weather actual tracker. @zara"""
        _LOGGER.debug("WeatherActualTracker connected")

    def is_available(self) -> bool:
        """Always available as fallback. @zara"""
        return True

    def get_priority(self) -> int:
        """Priority lower than pure AI strategy. @zara"""
        return 50

    async def calculate_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        **kwargs,
    ) -> ForecastResult:
        """Calculate consumption forecast using thermodynamics engine. @zara"""
        try:
            await self._ensure_components_initialized()
            if not hourly_weather_forecast:
                return self._empty_result()
            forecast_now = dt_util_safe.now()
            today = forecast_now.date()
            tomorrow = today + timedelta(days=1)
            day_after_tomorrow = today + timedelta(days=2)
            try:
                requested_correction_factor = float(correction_factor)
            except (TypeError, ValueError):
                requested_correction_factor = 1.0
            if (
                not isfinite(requested_correction_factor)
                or not 0.5 <= requested_correction_factor <= 1.5
            ):
                _LOGGER.warning(
                    "Invalid correction factor %r; using 1.0", correction_factor
                )
                requested_correction_factor = 1.0
            today_consumption_kwh = 0.0
            tomorrow_consumption_kwh = 0.0
            day_after_consumption_kwh = 0.0
            today_raw_consumption_kwh = 0.0
            hourly_predictions = []
            best_hour = None
            best_hour_consumption_kwh = 0.0
            cop_parameters = self._cop_sink_kwargs()
            forecast_timezone = self._forecast_timezone()
            indoor_setpoint_c = self._configured_indoor_temp()
            await self._apply_learned_u_factor()
            blocked_heating_days = self._blocked_space_heating_days(
                hourly_weather_forecast
            )
            day_correction_factors = self._day_correction_factors(
                hourly_weather_forecast
            )
            for weather_hour in hourly_weather_forecast:
                forecast_datetime = self._parse_datetime(weather_hour)
                if not forecast_datetime:
                    continue
                forecast_date = forecast_datetime.date()
                forecast_hour = forecast_datetime.hour
                space_heating_blocked = blocked_heating_days.get(
                    local_calendar_date(forecast_datetime, forecast_timezone), False
                )
                local_forecast_date = local_calendar_date(
                    forecast_datetime, forecast_timezone
                )
                effective_correction_factor = self._blend_beside_day_factor(
                    requested_correction_factor,
                    day_correction_factors.get(local_forecast_date),
                )
                hour_prediction = await self._calculate_hour_consumption(
                    weather_hour,
                    forecast_datetime,
                    sink_kwargs=cop_parameters,
                    indoor_temp=indoor_setpoint_c,
                    suppress_space_heating=space_heating_blocked,
                    day_factor=day_correction_factors.get(
                        local_calendar_date(forecast_datetime, forecast_timezone)
                    ),
                )
                raw_consumption_kwh = hour_prediction["consumption"]
                corrected_consumption_kwh = min(
                    raw_consumption_kwh * effective_correction_factor,
                    self.heating_capacity_kw,
                )
                if forecast_date == today:
                    today_raw_consumption_kwh += raw_consumption_kwh
                    today_consumption_kwh += corrected_consumption_kwh
                    if corrected_consumption_kwh > best_hour_consumption_kwh:
                        best_hour_consumption_kwh = corrected_consumption_kwh
                        best_hour = forecast_hour
                elif forecast_date == tomorrow:
                    tomorrow_consumption_kwh += corrected_consumption_kwh
                elif forecast_date == day_after_tomorrow:
                    day_after_consumption_kwh += corrected_consumption_kwh
                hourly_prediction = {
                    "hour": forecast_hour,
                    "datetime": forecast_datetime.isoformat(),
                    "date": forecast_date.isoformat(),
                    "production_kwh": round(corrected_consumption_kwh, 3),
                    "production_kwh_raw": round(raw_consumption_kwh, 3),
                    "correction_factor": effective_correction_factor,
                    "method": hour_prediction["method"],
                    "physics_kwh": hour_prediction["thermo_kwh"],
                    "ai_kwh": hour_prediction["ai_kwh"],
                    "ai_confidence": hour_prediction["ai_confidence"],
                    "ml_contribution": hour_prediction["ml_contribution"],
                    "confidence": 75.0,
                    "lstm_kwh": hour_prediction.get("lstm_kwh"),
                    "ridge_kwh": hour_prediction.get("ridge_kwh"),
                }
                hourly_predictions.append(hourly_prediction)
                continue
            await self._store_hourly_predictions(hourly_predictions)
            return ForecastResult(
                forecast_today=round(today_consumption_kwh, 2),
                forecast_tomorrow=round(tomorrow_consumption_kwh, 2),
                forecast_day_after_tomorrow=round(day_after_consumption_kwh, 2),
                confidence_today=70.0,
                confidence_tomorrow=60.0,
                confidence_day_after=50.0,
                method="thermodynamics" if not self._use_ai else "AI + Thermodynamics",
                calibrated=bool(self._thermodynamics_calibrator),
                best_hour_today=best_hour,
                best_hour_consumption_kwh=round(best_hour_consumption_kwh, 3),
                hourly_values=hourly_predictions,
                model_accuracy=None,
                forecast_today_raw=today_raw_consumption_kwh,
                safeguard_applied_today=False,
            )
        except Exception as error:
            _LOGGER.error(f"Forecast calculation failed: {error}", exc_info=True)
            return self._empty_result()

    async def _store_hourly_predictions(
        self, hourly_values: List[Dict[str, Any]]
    ) -> None:
        """Store hourly predictions in database. Locked dates are skipped. @zara"""
        try:
            locked_dates = set()
            prediction_dates = {prediction["date"] for prediction in hourly_values}
            for prediction_date in prediction_dates:
                locked_date_row = await self._db.fetchone(
                    "SELECT locked FROM daily_forecasts\n                       WHERE forecast_date = ? AND locked = 1\n                       LIMIT 1",
                    (prediction_date,),
                )
                if locked_date_row and locked_date_row[0]:
                    locked_dates.add(prediction_date)
            if locked_dates:
                _LOGGER.debug(
                    "Skipping hourly predictions for locked dates: %s",
                    ", ".join(sorted(locked_dates)),
                )
            for prediction in hourly_values:
                if prediction["date"] in locked_dates:
                    continue
                prediction_id = f"{prediction['date']}_{prediction['hour']:02d}"
                target_datetime = datetime.fromisoformat(prediction["datetime"])
                prediction_method = prediction.get("method", "thermodynamics")
                await self._db.execute(
                    "INSERT INTO hourly_predictions\n                       (prediction_id, prediction_created_at, prediction_created_hour,\n                        target_datetime, target_date, target_hour, prediction_kwh,\n                        prediction_method, is_production_hour,\n                        physics_kwh, ai_kwh, ai_confidence,\n                        ml_contribution_percent, confidence,\n                        lstm_kwh, ridge_kwh, prediction_kwh_raw, correction_factor)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           prediction_kwh = excluded.prediction_kwh,\n                           prediction_method = excluded.prediction_method,\n                           prediction_created_at = excluded.prediction_created_at,\n                           physics_kwh = excluded.physics_kwh,\n                           ai_kwh = excluded.ai_kwh,\n                           ai_confidence = excluded.ai_confidence,\n                           ml_contribution_percent = excluded.ml_contribution_percent,\n                           confidence = excluded.confidence,\n                           lstm_kwh = excluded.lstm_kwh,\n                           ridge_kwh = excluded.ridge_kwh,\n                           prediction_kwh_raw = excluded.prediction_kwh_raw,\n                           correction_factor = excluded.correction_factor",
                    (
                        prediction_id,
                        dt_util_safe.now(),
                        dt_util_safe.now().hour,
                        target_datetime,
                        prediction["date"],
                        prediction["hour"],
                        prediction["production_kwh"],
                        prediction_method,
                        True,
                        prediction.get("physics_kwh"),
                        prediction.get("ai_kwh"),
                        prediction.get("ai_confidence"),
                        prediction.get("ml_contribution", 0),
                        prediction.get("confidence", 75.0),
                        prediction.get("lstm_kwh"),
                        prediction.get("ridge_kwh"),
                        prediction.get("production_kwh_raw"),
                        prediction.get("correction_factor", 1.0),
                    ),
                    auto_commit=True,
                )
                continue
        except Exception as error:
            _LOGGER.debug(f"Could not store hourly predictions: {error}")
        return None if True else None

    async def _ensure_components_initialized(self) -> None:
        """Initialize AI and thermodynamics components. @zara"""
        if self.ai_predictor and self.ai_predictor.is_ready():
            self._use_ai = True
            if self.ai_predictor.is_bootstrap_phase():
                training_sample_count = self.ai_predictor.training_samples or 0
                _LOGGER.debug(
                    f"Thermodynamics+Ridge blend active ({training_sample_count} samples)"
                )
            else:
                _LOGGER.debug("Thermodynamics+LSTM blend active")
        else:
            self._use_ai = False
        if not self._thermo_init_attempted:
            self._thermo_init_attempted = True
            try:
                self._thermodynamics_engine = ThermodynamicsEngine(
                    wp_type=self.wp_type,
                    heating_capacity_kw=self.heating_capacity_kw,
                    cop_rated=self.cop_rated,
                )
                _LOGGER.debug("ThermodynamicsEngine initialized")
            except Exception as error:
                _LOGGER.warning(f"ThermodynamicsEngine init failed: {error}")
        return None if True else None

    def _cop_sink_kwargs(self) -> Dict[str, Any]:
        """Read live flow/mode and configured sink temperatures for COP.

        The insights hint latch is forwarded as ``generator_flow_hint``.
        This path does not compare flow, return, and tank temperatures;
        that decision already lives on the insights engine.
        """
        merged_config = self._merged_config()
        hass = getattr(self.orchestrator, "hass", None)
        flow_temp_c = self._entity_float(hass, merged_config.get(CONF_FLOW_TEMP_ENTITY))
        operation_mode = self._entity_state(
            hass, merged_config.get(CONF_OPERATION_MODE_ENTITY)
        )
        return cop_sink_kwargs(
            merged_config,
            flow_temp=flow_temp_c,
            operation_mode=operation_mode,
            generator_flow_hint=self._generator_flow_hint_latched(),
        )

    def _generator_flow_hint_latched(self) -> bool:
        """Read ``_generator_flow_hint`` from the live insights engine.

        Runtime modules reach that engine through
        ``hass.data[DOMAIN][entry_id].provider``. A missing runtime,
        provider, or engine keeps today's measured-flow sink.
        """
        orchestrator = self.orchestrator
        hass = getattr(orchestrator, "hass", None)
        config_entry = getattr(orchestrator, "config_entry", None)
        entry_id = getattr(config_entry, "entry_id", None)
        hass_data = getattr(hass, "data", None)
        if not isinstance(hass_data, dict) or not entry_id:
            return False
        domain_data = hass_data.get(DOMAIN)
        if not isinstance(domain_data, dict):
            return False
        runtime = domain_data.get(entry_id)
        provider = getattr(runtime, "provider", None)
        insights_engine = getattr(provider, "_engine", None)
        return getattr(insights_engine, "_generator_flow_hint", False) is True

    def _merged_config(self) -> Dict[str, Any]:
        config_entry = getattr(self.orchestrator, "config_entry", None)
        if config_entry is None:
            return {}
        return {
            **dict(getattr(config_entry, "data", {}) or {}),
            **dict(getattr(config_entry, "options", {}) or {}),
        }

    def _dhw_kwargs(self) -> Dict[str, Any]:
        return dhw_forecast_kwargs(self._merged_config())

    def _forecast_timezone(self) -> tzinfo:
        forecast_timezone = getattr(dt_util, "DEFAULT_TIME_ZONE", None)
        if isinstance(forecast_timezone, tzinfo):
            return forecast_timezone
        return timezone.utc

    def _configured_indoor_temp(self) -> float:
        """Setpoint, otherwise indoor sensor, otherwise 21 °C.

        Values outside 5–30 °C and unavailable states fall through.
        """
        merged_config = self._merged_config()
        hass = getattr(self.orchestrator, "hass", None)
        for temperature_entity_key in (
            CONF_TARGET_TEMP_ENTITY,
            CONF_INDOOR_TEMP_ENTITY,
        ):
            entity_value = self._entity_float(
                hass, merged_config.get(temperature_entity_key)
            )
            if entity_value is not None:
                if (
                    FORECAST_INDOOR_TEMP_MIN_C
                    <= entity_value
                    <= FORECAST_INDOOR_TEMP_MAX_C
                ):
                    return entity_value if True else None
        return DEFAULT_INDOOR_TEMP if True else None

    def _limit_outdoor_wind(
        self, hour_data: Dict[str, Any]
    ) -> tuple[Optional[float], Optional[float]]:
        """Outdoor forecast and the wind the hour's physics will use.

        A missing temperature does not count toward the daily mean. Missing
        wind uses the same 3 m/s default as the thermodynamics hour.
        """
        if "temperature" not in hour_data:
            return (None, None)
        try:
            outdoor_temp_c = float(hour_data["temperature"])
        except (TypeError, ValueError):
            return (None, None)
        if not isfinite(outdoor_temp_c):
            return (None, None)
        try:
            wind_speed_mps = float(hour_data.get("wind_speed", 3))
        except (TypeError, ValueError):
            wind_speed_mps = 3.0
        if not isfinite(wind_speed_mps):
            wind_speed_mps = 3.0
        return (outdoor_temp_c, wind_speed_mps)

    @staticmethod
    def _blend_beside_day_factor(
        requested: float, day_factor: Optional[float]
    ) -> float:
        """Keep the coordinator blend off a settled day factor.

        Both numbers scale the same electrical kWh. Once the day factor is
        active, a second multiply would correct the same error again.
        """
        if day_factor is not None and isfinite(day_factor) and (day_factor > 0.0):
            return 1.0
        return requested

    def _day_correction_factors(
        self, hourly_weather_forecast: List[Dict[str, Any]]
    ) -> Dict[date, Optional[float]]:
        """Day-level factor from the mean temperature, once that bucket has settled."""
        calibrator = self._thermodynamics_calibrator
        settled_day_factor = getattr(calibrator, "settled_day_factor", None)
        if settled_day_factor is None:
            return {}
        weather_by_date = {}
        forecast_timezone = self._forecast_timezone()
        for weather_hour in hourly_weather_forecast:
            forecast_datetime = self._parse_datetime(weather_hour)
            if forecast_datetime is not None:
                outdoor_temp_c, wind_speed_mps = self._limit_outdoor_wind(weather_hour)
                if outdoor_temp_c is not None:
                    weather_by_date.setdefault(
                        local_calendar_date(forecast_datetime, forecast_timezone), []
                    ).append(
                        (
                            outdoor_temp_c,
                            0.0 if wind_speed_mps is None else wind_speed_mps,
                        )
                    )
        day_factors = {}
        for calendar_date, daily_weather_samples in weather_by_date.items():
            if len(daily_weather_samples) < 12:
                day_factors[calendar_date] = None
                continue
            mean_outdoor_temp_c = sum(
                (weather_sample[0] for weather_sample in daily_weather_samples)
            ) / len(daily_weather_samples)
            mean_wind_speed_mps = sum(
                (weather_sample[1] for weather_sample in daily_weather_samples)
            ) / len(daily_weather_samples)
            try:
                day_factors[calendar_date] = settled_day_factor(
                    mean_outdoor_temp_c, mean_wind_speed_mps
                )
            except Exception:
                day_factors[calendar_date] = None
                continue
        return day_factors

    def _blocked_space_heating_days(
        self, hourly_weather_forecast: List[Dict[str, Any]]
    ) -> Dict[date, bool]:
        thermodynamics_engine = self._thermodynamics_engine
        if thermodynamics_engine is None:
            try:
                thermodynamics_engine = ThermodynamicsEngine(
                    wp_type=self.wp_type,
                    heating_capacity_kw=self.heating_capacity_kw,
                    cop_rated=self.cop_rated,
                )
            except Exception:
                return {}
        weather_samples = []
        for weather_hour in hourly_weather_forecast:
            forecast_datetime = self._parse_datetime(weather_hour)
            if forecast_datetime is not None:
                outdoor_temp_c, wind_speed_mps = self._limit_outdoor_wind(weather_hour)
                weather_samples.append(
                    (forecast_datetime, outdoor_temp_c, wind_speed_mps)
                )
        return space_heating_suppressed_by_day(
            thermodynamics_engine,
            weather_samples,
            self._forecast_timezone(),
            calibrator=self._thermodynamics_calibrator,
            has_dhw=bool(self._dhw_kwargs().get("has_dhw")),
        )

    async def _apply_learned_u_factor(self) -> None:
        """Adopt the learned U-factor once for this forecast run.

        Below 24 samples the engine keeps heating_capacity/25. Adoption and
        later moves above 5 % rescale the heating buckets first, then freeze
        the value so later hours of the same run cannot jump.
        """
        self._run_u_factor = None
        thermodynamics_engine = self._thermodynamics_engine
        calibrator = self._thermodynamics_calibrator
        if thermodynamics_engine is None or calibrator is None:
            return None
        align_forecast_u = getattr(calibrator, "align_forecast_u", None)
        if align_forecast_u is None:
            return
        try:
            learned_u_factor = await align_forecast_u(
                thermodynamics_engine.u_factor, thermodynamics_engine
            )
        except Exception:
            _LOGGER.warning("Learned U-factor was not applied", exc_info=True)
            return
        if learned_u_factor is None or not isfinite(learned_u_factor):
            return None
        self._run_u_factor = float(learned_u_factor)
        thermodynamics_engine.u_factor = float(learned_u_factor)

    @staticmethod
    def _entity_state(hass: Any, entity_id: Any) -> Any:
        if hass is None or not isinstance(entity_id, str) or (not entity_id):
            return None
        entity_states = getattr(hass, "states", None)
        state_getter = getattr(entity_states, "get", None)
        if state_getter is None:
            return
        entity_state = state_getter(entity_id)
        if entity_state is None:
            return
        entity_value = getattr(entity_state, "state", entity_state)
        if entity_value in {"", "unknown", "unavailable"}:
            return
        return entity_value

    @classmethod
    def _entity_float(cls, hass: Any, entity_id: Any) -> Optional[float]:
        entity_value = cls._entity_state(hass, entity_id)
        if entity_value is None:
            return
        try:
            numeric_state = float(entity_value)
        except (TypeError, ValueError):
            return
        return numeric_state if isfinite(numeric_state) else None

    def _blend_calibrator_state(
        self, outdoor_temp: float, wind_speed: float, hour: int
    ) -> Tuple[int, float, float]:
        """Samples, confidence and hourly factor for the hour being blended."""
        calibrator = self._thermodynamics_calibrator
        if calibrator is None:
            return (0, 0.0, 1.0)
        try:
            calibration_result = calibrator.get_correction_factor(
                outdoor_temp=outdoor_temp,
                wind_speed=wind_speed,
                hour=hour,
                is_dhw_only=self._hour_is_dhw_only,
            )
        except Exception:
            return (0, 0.0, 1.0)
        if not calibration_result:
            return (0, 0.0, 1.0)
        try:
            hour_correction_factor = float(calibration_result.correction_factor)
        except (TypeError, ValueError):
            hour_correction_factor = 1.0
        if not isfinite(hour_correction_factor) or hour_correction_factor <= 0.0:
            hour_correction_factor = 1.0
        return (
            int(calibration_result.sample_count),
            float(calibration_result.confidence),
            hour_correction_factor,
        )

    @staticmethod
    def _ai_follows_correction(
        thermo_pred: float, ai_pred: float, factor: float, confidence: float
    ) -> Optional[bool]:
        """Whether the AI sits inside a correction that has left 1.0.

        None means the calibrator has no direction: too little confidence, or
        the factor is still within 15 % of 1. True means the AI lies between
        the hour before that correction and the calibrated hour.
        """
        if confidence <= 0.3 or not isfinite(factor) or factor <= 0.0:
            return None
        if abs(factor - 1.0) <= 0.15:
            return
        uncorrected_thermo_kwh = thermo_pred / factor
        if not isfinite(uncorrected_thermo_kwh):
            return
        lower_consumption_bound, upper_consumption_bound = (
            (uncorrected_thermo_kwh, thermo_pred)
            if uncorrected_thermo_kwh <= thermo_pred
            else (thermo_pred, uncorrected_thermo_kwh)
        )
        return (
            lower_consumption_bound * 0.95 <= ai_pred <= upper_consumption_bound * 1.05
        )

    async def _calculate_hour_consumption(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        sink_kwargs: Optional[Dict[str, Any]] = None,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        suppress_space_heating: bool = False,
        day_factor: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Hybrid Thermodynamics + ML consumption prediction for one hour. @zara"""
        try:
            forecast_hour = hour_dt.hour
            outdoor_temp_c = float(hour_data.get("temperature", 5))
            wind_speed_mps = float(hour_data.get("wind_speed", 3))
            humidity_percent = float(hour_data.get("humidity", 70))
            cloud_cover_percent = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            thermodynamics_kwh = self._calculate_thermodynamics_consumption(
                hour_data,
                hour_dt,
                sink_kwargs=sink_kwargs,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
                day_factor=day_factor,
            )
            ai_kwh = 0.0
            ai_confidence = 0.0
            bootstrap_phase = False
            lstm_kwh = 0.0
            ridge_kwh = 0.0
            lstm_available = False
            ridge_available = False
            if self._use_ai and self.ai_predictor:
                model_accuracy = self.ai_predictor.current_accuracy or 0.0
                bootstrap_phase = self.ai_predictor.is_bootstrap_phase()
                if bootstrap_phase:
                    training_sample_count = self.ai_predictor.training_samples or 10
                    ai_confidence = self._calculate_bootstrap_confidence(
                        model_accuracy, training_sample_count, outdoor_temp_c
                    )
                else:
                    ai_confidence = self._calculate_hourly_ai_confidence(
                        model_accuracy, outdoor_temp_c, forecast_hour
                    )
                learned_confidence = await self._get_learned_confidence_adjustment(
                    cloud_cover_percent, forecast_hour
                )
                ai_confidence = ai_confidence * learned_confidence["advantage_factor"]
                if (
                    learned_confidence["physics_mae"]
                    and learned_confidence["ai_mae"]
                    and (learned_confidence["sample_count"] >= 10)
                ):
                    physics_mae = max(0.001, learned_confidence["physics_mae"])
                    ai_mae = max(0.001, learned_confidence["ai_mae"])
                    inverse_error_sum = 1.0 / physics_mae + 1.0 / ai_mae
                    inverse_error_ai_weight = 1.0 / ai_mae / inverse_error_sum
                    ai_confidence = 0.5 * ai_confidence + 0.5 * inverse_error_ai_weight
                ai_confidence = min(0.96, max(0.0, ai_confidence))
                if ai_confidence > 0:
                    prediction_weather = {
                        "temperature": outdoor_temp_c,
                        "wind": wind_speed_mps,
                        "humidity": humidity_percent,
                        "clouds": cloud_cover_percent,
                        "rain": hour_data.get("precipitation", 0),
                        "pressure": hour_data.get("pressure", 1013.0),
                        "solar_radiation_wm2": hour_data.get("solar_radiation", 0),
                        "dni": hour_data.get("direct_radiation", 0),
                        "diffuse_radiation": hour_data.get("diffuse_radiation", 0),
                    }
                    try:
                        ensemble_prediction = (
                            await self.ai_predictor.predict_hour_ensemble(
                                hour=forecast_hour, weather_data=prediction_weather
                            )
                        )
                        lstm_kwh = ensemble_prediction.get("lstm_total", 0.0)
                        ridge_kwh = ensemble_prediction.get("ridge_total", 0.0)
                        lstm_available = lstm_kwh > 0
                        ridge_available = ridge_kwh > 0
                        if lstm_available and ridge_available:
                            ai_kwh = (
                                lstm_kwh * ENSEMBLE_LSTM_WEIGHT
                                + ridge_kwh * ENSEMBLE_RIDGE_WEIGHT
                            )
                        elif lstm_available:
                            ai_kwh = lstm_kwh
                        elif ridge_available:
                            ai_kwh = ridge_kwh
                    except AttributeError:
                        try:
                            ensemble_prediction = (
                                await self.ai_predictor.predict_hour_per_group_ensemble(
                                    hour=forecast_hour,
                                    weather_data=prediction_weather,
                                    astronomy_data={},
                                    target_datetime=hour_dt,
                                )
                            )
                            lstm_kwh = ensemble_prediction.get("lstm_total", 0.0)
                            ridge_kwh = ensemble_prediction.get("ridge_total", 0.0)
                            lstm_available = lstm_kwh > 0
                            ridge_available = ridge_kwh > 0
                            if lstm_available and ridge_available:
                                ai_kwh = (
                                    lstm_kwh * ENSEMBLE_LSTM_WEIGHT
                                    + ridge_kwh * ENSEMBLE_RIDGE_WEIGHT
                                )
                            elif lstm_available:
                                ai_kwh = lstm_kwh
                            elif ridge_available:
                                ai_kwh = ridge_kwh
                        except Exception:
                            pass
                    if lstm_available and ridge_available:
                        ensemble_mean_kwh = (lstm_kwh + ridge_kwh) / 2.0
                        if ensemble_mean_kwh > 0.005:
                            ensemble_disagreement = (
                                abs(lstm_kwh - ridge_kwh) / ensemble_mean_kwh
                            )
                        else:
                            ensemble_disagreement = 0.0
                        if ensemble_disagreement > DISAGREEMENT_THRESHOLD:
                            disagreement_multiplier = max(
                                0.5, 1.0 - ensemble_disagreement * 0.5
                            )
                            ai_confidence *= disagreement_multiplier
            blend_ai = False
            blend_weight = ai_confidence
            calibration_sample_count, calibration_confidence, hour_correction_factor = (
                self._blend_calibrator_state(
                    outdoor_temp_c, wind_speed_mps, forecast_hour
                )
            )
            if (
                ai_confidence > 0
                and ai_kwh > 0
                and (thermodynamics_kwh >= 0.05)
                and (calibration_sample_count >= 3)
            ):
                ai_thermo_deviation = (
                    abs(ai_kwh - thermodynamics_kwh) / thermodynamics_kwh
                )
                ai_tracks_correction = self._ai_follows_correction(
                    thermodynamics_kwh,
                    ai_kwh,
                    hour_correction_factor,
                    calibration_confidence,
                )
                ai_above_physics = ai_kwh > thermodynamics_kwh
                if ai_above_physics:
                    baseline_u_factor = self.heating_capacity_kw / 25.0
                    active_u_factor = baseline_u_factor
                    if self._thermodynamics_engine is not None:
                        active_u_factor = self._thermodynamics_engine.u_factor
                    elevated_u_factor = active_u_factor > baseline_u_factor * 1.05
                    ridge_confirms = True
                    if lstm_available and ridge_available:
                        confirmation_mean_kwh = (lstm_kwh + ridge_kwh) / 2.0
                        if confirmation_mean_kwh > 0.005:
                            ridge_confirms = (
                                abs(lstm_kwh - ridge_kwh) / confirmation_mean_kwh
                                <= RIDGE_CONFIRMS_THRESHOLD
                            )
                    if ai_tracks_correction is True and ai_thermo_deviation <= 1.0:
                        blend_ai = True
                        if ai_thermo_deviation <= MAX_AI_THERMO_DEVIATION:
                            blend_weight = max(ai_confidence, 0.9)
                        else:
                            excess_deviation = (
                                ai_thermo_deviation - MAX_AI_THERMO_DEVIATION
                            )
                            deviation_multiplier = max(
                                0.2, 1.0 - excess_deviation * 0.6
                            )
                            blend_weight = ai_confidence * deviation_multiplier
                    elif (
                        elevated_u_factor
                        and ridge_confirms
                        and (ai_thermo_deviation <= MAX_AI_THERMO_DEVIATION)
                    ):
                        blend_ai = True
                        blend_weight = max(ai_confidence, 0.9)
                elif (
                    ai_thermo_deviation <= MAX_AI_THERMO_DEVIATION
                    or ai_tracks_correction is True
                ):
                    blend_ai = True
                    if ai_thermo_deviation <= MAX_AI_THERMO_DEVIATION:
                        blend_weight = max(ai_confidence, 0.9)
                    else:
                        excess_deviation = ai_thermo_deviation - MAX_AI_THERMO_DEVIATION
                        deviation_multiplier = max(0.2, 1.0 - excess_deviation * 0.6)
                        blend_weight = ai_confidence * deviation_multiplier
            if blend_ai:
                final_consumption_kwh = (
                    thermodynamics_kwh * (1 - blend_weight) + ai_kwh * blend_weight
                )
                ai_method = "ridge" if bootstrap_phase else "lstm"
                prediction_method = f"thermo_{ai_method}{int(blend_weight * 100)}"
            else:
                final_consumption_kwh = thermodynamics_kwh
                prediction_method = "thermodynamics"
            return (
                (
                    (
                        {
                            "consumption": max(
                                0.0,
                                min(
                                    final_consumption_kwh,
                                    self.heating_capacity_kw * 1.1,
                                ),
                            ),
                            "thermo_kwh": round(thermodynamics_kwh, 4),
                            "ai_kwh": (
                                round(ai_kwh, 4)
                                if ai_kwh > 0 and thermodynamics_kwh > 0
                                else None
                            ),
                            "ai_confidence": (
                                round(blend_weight, 4) if blend_weight > 0 else None
                            ),
                            "method": prediction_method,
                            "ml_contribution": (
                                int(blend_weight * 100) if blend_ai else 0
                            ),
                            "lstm_kwh": round(lstm_kwh, 4) if lstm_available else None,
                            "ridge_kwh": (
                                round(ridge_kwh, 4) if ridge_available else None
                            ),
                        }
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.debug(f"Hour consumption calc failed: {error}")
            fallback_consumption_kwh = self._calculate_thermodynamics_consumption(
                hour_data,
                hour_dt,
                sink_kwargs=sink_kwargs,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
                day_factor=day_factor,
            )
            return {
                "consumption": fallback_consumption_kwh,
                "thermo_kwh": round(fallback_consumption_kwh, 4),
                "ai_kwh": None,
                "ai_confidence": None,
                "method": "thermodynamics",
                "ml_contribution": 0,
                "lstm_kwh": None,
                "ridge_kwh": None,
            }

    def _calculate_thermodynamics_consumption(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        sink_kwargs: Optional[Dict[str, Any]] = None,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        suppress_space_heating: bool = False,
        day_factor: Optional[float] = None,
    ) -> float:
        """Calculate thermodynamics-based hourly consumption. @zara

        Uses ThermodynamicsEngine for COP and heating demand calculation.
        Falls back to simple temperature-based heuristic if engine unavailable.
        """
        self._hour_is_dhw_only = False
        try:
            outdoor_temp_c = float(hour_data.get("temperature", 5))
            wind_speed_mps = float(hour_data.get("wind_speed", 3))
            humidity_percent = float(hour_data.get("humidity", 70))
            cloud_cover_percent = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            dhw_parameters = self._dhw_kwargs()
            has_dhw = bool(dhw_parameters.get("has_dhw"))
            if self._thermodynamics_engine:
                weather_conditions = WeatherConditions(
                    outdoor_temp=outdoor_temp_c,
                    wind_speed=wind_speed_mps,
                    humidity=humidity_percent,
                    cloud_cover=cloud_cover_percent,
                    precipitation=float(hour_data.get("precipitation", 0)),
                )
                thermodynamics_result = (
                    self._thermodynamics_engine.calculate_hourly_consumption(
                        weather=weather_conditions,
                        indoor_temp=indoor_temp,
                        hour=hour_dt.hour,
                        suppress_space_heating=suppress_space_heating,
                        **sink_kwargs or {},
                        **dhw_parameters,
                    )
                )
                consumption_kwh = thermodynamics_result.consumption_kwh
                self._hour_is_dhw_only = has_dhw and (
                    suppress_space_heating
                    or thermodynamics_result.heating_demand_kw <= 0.01
                )
                if (
                    day_factor is not None
                    and isfinite(day_factor)
                    and (day_factor > 0.0)
                    and (not self._hour_is_dhw_only)
                ):
                    consumption_kwh *= day_factor
                elif self._thermodynamics_calibrator:
                    try:
                        calibration_result = (
                            self._thermodynamics_calibrator.get_correction_factor(
                                outdoor_temp=outdoor_temp_c,
                                wind_speed=wind_speed_mps,
                                hour=hour_dt.hour,
                                is_dhw_only=self._hour_is_dhw_only,
                            )
                        )
                        if calibration_result and calibration_result.confidence > 0.3:
                            consumption_kwh *= calibration_result.correction_factor
                    except Exception:
                        pass
                return max(0.0, consumption_kwh)
            self._hour_is_dhw_only = has_dhw and suppress_space_heating
            return self._simple_temperature_heuristic(
                outdoor_temp_c,
                wind_speed_mps,
                hour_dt.hour,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
            )
        except Exception as error:
            _LOGGER.debug(f"Thermodynamics consumption calc failed: {error}")
            outdoor_temp_c = float(hour_data.get("temperature", 5))
            return self._simple_temperature_heuristic(
                outdoor_temp_c,
                3.0,
                hour_dt.hour,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
            )

    def _simple_temperature_heuristic(
        self,
        temp: float,
        wind_speed: float,
        hour: int,
        *,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        suppress_space_heating: bool = False,
    ) -> float:
        """Engine-based fallback when the cached thermodynamics instance is missing."""
        try:
            thermodynamics_engine = self._thermodynamics_engine or ThermodynamicsEngine(
                wp_type=self.wp_type,
                heating_capacity_kw=self.heating_capacity_kw,
                cop_rated=self.cop_rated,
            )
            if self._run_u_factor is not None:
                thermodynamics_engine.u_factor = self._run_u_factor
            weather_conditions = WeatherConditions(
                outdoor_temp=float(temp), wind_speed=float(wind_speed)
            )
            thermodynamics_result = thermodynamics_engine.calculate_hourly_consumption(
                weather_conditions,
                indoor_temp=indoor_temp,
                hour=hour,
                suppress_space_heating=suppress_space_heating,
                **self._cop_sink_kwargs(),
                **self._dhw_kwargs(),
            )
            return max(
                0.0,
                min(thermodynamics_result.consumption_kwh, self.heating_capacity_kw),
            )
        except Exception:
            return 0.0

    def _calculate_hourly_ai_confidence(
        self, base_r2: float, temp: float, hour: int
    ) -> float:
        """Calculate dynamic AI confidence for specific hour. @zara

        Temperature stability and time of day affect confidence.
        """
        if base_r2 <= 0:
            return 0.0
        if base_r2 < 0.5:
            accuracy_confidence = base_r2
        elif base_r2 < 0.8:
            accuracy_confidence = 0.5 + (base_r2 - 0.5) * 1.333
        else:
            accuracy_confidence = 0.9 + min(base_r2 - 0.8, 0.2) * 0.5
        if -5 <= temp <= 15:
            temperature_multiplier = 1.0
        elif temp < -15 or temp > 25:
            temperature_multiplier = 0.7
        else:
            temperature_multiplier = 0.85
        if 0 <= hour <= 5 or 21 <= hour <= 23:
            hour_multiplier = 1.0
        elif 9 <= hour <= 15:
            hour_multiplier = 0.85
        else:
            hour_multiplier = 0.9
        return accuracy_confidence * temperature_multiplier * hour_multiplier

    def _calculate_bootstrap_confidence(
        self, base_r2: float, n_samples: int, temp: float
    ) -> float:
        """Calculate confidence for Ridge in bootstrap phase (10-50 samples). @zara"""
        sample_progress = (n_samples - 10) / 40
        sample_confidence = 0.15 + sample_progress * 0.35
        accuracy_multiplier = min(1.0, base_r2 / 0.6)
        temperature_multiplier = 1.0 if -5 <= temp <= 15 else 0.8
        bootstrap_confidence = (
            sample_confidence * accuracy_multiplier * temperature_multiplier
        )
        return min(0.5, max(0.0, bootstrap_confidence))

    async def _get_learned_confidence_adjustment(
        self, clouds: float, hour: int
    ) -> Dict[str, Any]:
        """Learned confidence adjustment with MAE data. @zara"""
        cloud_bucket = (
            "clear" if clouds < 30 else "partly_cloudy" if clouds < 70 else "overcast"
        )
        hour_bucket = "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
        learned_confidence = {
            "advantage_factor": 1.0,
            "physics_mae": None,
            "ai_mae": None,
            "sample_count": 0,
        }
        try:
            season = get_season(dt_util_safe.now().month)
            performance_row = await self._db.fetchone(
                "SELECT ai_advantage_factor, sample_count, physics_mae, ai_mae\n                   FROM method_performance_learning\n                   WHERE cloud_bucket = ? AND hour_bucket = ? AND season = ?",
                (cloud_bucket, hour_bucket, season),
            )
            if not performance_row or not performance_row[1] or performance_row[1] < 5:
                performance_row = await self._db.fetchone(
                    "SELECT ai_advantage_factor, sample_count, physics_mae, ai_mae\n                       FROM method_performance_learning\n                       WHERE cloud_bucket = ? AND hour_bucket = ? AND season IS NULL",
                    (cloud_bucket, hour_bucket),
                )
            if performance_row and performance_row[1] and (performance_row[1] >= 5):
                learned_confidence["advantage_factor"] = performance_row[0]
                learned_confidence["sample_count"] = performance_row[1]
                learned_confidence["physics_mae"] = performance_row[2]
                learned_confidence["ai_mae"] = performance_row[3]
        except Exception:
            pass
        return learned_confidence if True else None

    def _parse_datetime(self, hour_data: Dict[str, Any]) -> Optional[datetime]:
        """Parse datetime from hour data. @zara"""
        try:
            timestamp_value = hour_data.get("datetime") or hour_data.get("time")
            if timestamp_value:
                if isinstance(timestamp_value, datetime):
                    return timestamp_value
                return datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        except Exception:
            pass
        return None if True else None

    def _empty_result(self) -> ForecastResult:
        """Return empty result. @zara"""
        return ForecastResult(
            forecast_today=0.0,
            forecast_tomorrow=0.0,
            forecast_day_after_tomorrow=0.0,
            confidence_today=0.0,
            confidence_tomorrow=0.0,
            confidence_day_after=0.0,
            method="thermodynamics_empty",
            calibrated=False,
            best_hour_today=6,
            best_hour_consumption_kwh=0.0,
            hourly_values=[],
            model_accuracy=None,
            forecast_today_raw=0.0,
            safeguard_applied_today=False,
        )
