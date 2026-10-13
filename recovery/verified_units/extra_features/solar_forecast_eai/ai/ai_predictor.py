# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Main AI Predictor for Solar Forecast Energy AI V16.2.0.

Multi-Output AI predictor with per-group predictions using database storage.
Combines TinyLSTM and TinyRidge models with DB-based weight persistence.

@zara
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from .ai_tiny_lstm import TinyLSTM
from .ai_tiny_ridge import TinyRidge
from .ai_feature_engineering import FeatureEngineer
from .ai_seasonal import SeasonalAdjuster
from .ai_dni_tracker import DniTracker
from .ai_feature_importance import FeatureImportanceAnalyzer, FeatureImportanceResult
from ..data.eai_consumption_sample_store import EAIConsumptionSampleStore

_LOGGER = logging.getLogger(__name__)
MIN_RIDGE_SAMPLES = 10
MIN_TRAINING_SAMPLES = 50
TRAINING_DAYS = 60
MODEL_FORMAT_VERSION = "4.0"
REASON_RIDGE_WARMUP = "ridge_warmup"
REASON_TRAINING_DATA_UNAVAILABLE = "training_data_unavailable"
BASE_FEATURE_COUNT = FeatureEngineer.FEATURE_COUNT
DEFAULT_MAX_KWH_PER_HOUR = 2.0


def calculate_feature_count(num_groups: int) -> int:
    """Return the single-output EAI consumption feature count."""
    return BASE_FEATURE_COUNT


class ModelState(Enum):
    """State of AI model @zara"""

    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    TRAINING = "training"
    DEGRADED = "degraded"
    ERROR = "error"
    UNTRAINED = "untrained"


@dataclass
class TrainingResult:
    """Result of AI training @zara"""

    if True:
        success: bool
        accuracy: float = 0.0
        samples_used: int = 0
        feature_count: int = BASE_FEATURE_COUNT
        num_outputs: int = 1
        error_message: Optional[str] = None
        epochs_trained: int = 0
        has_attention: bool = False
        rmse: float = 0.0
        reason_code: Optional[str] = None


@dataclass
class GroupPrediction:
    """Prediction result for a single panel group @zara"""

    if True:
        group_name: str
        power_kwh: float
        azimuth: float
        tilt: float
        capacity_kwp: float


class AIPredictor:
    """Multi-Output AI predictor with per-group predictions @zara"""

    def __init__(
        self,
        hass: Any,
        db_manager: Any,
        error_handler: Optional[Any] = None,
        notification_service: Optional[Any] = None,
        config_entry: Optional[Any] = None,
        panel_groups: Optional[List[Dict[str, Any]]] = None,
        solar_capacity: float = 5.0,
    ):
        """Initialize AI predictor @zara"""
        self.hass = hass
        self.db_manager = db_manager
        self.error_handler = error_handler
        self.notification_service = notification_service
        self.config_entry = config_entry
        self.panel_groups = panel_groups or []
        self.num_groups = len(self.panel_groups)
        self.total_capacity = self._calculate_total_capacity(
            self.panel_groups, solar_capacity
        )
        self.lstm = None
        self._lstm_ready = False
        self.ridge = None
        self.active_model = "none"
        self.feature_engineer = None
        self.seasonal = None
        self.dni_tracker = None
        self.state = ModelState.INITIALIZING
        self.current_accuracy = None
        self.current_rmse = None
        self.last_training_time = None
        self.last_training_samples = 0
        self.training_samples = 0
        self.use_attention = True
        self._last_feature_importance = None
        self._historical_cache = {}
        entry_id = getattr(config_entry, "entry_id", "default")
        self._learning_store = EAIConsumptionSampleStore(hass, entry_id)
        self.solar_capacity = self.total_capacity
        self.power_entity = None
        self.weather_entity = None
        self._max_kwh_per_hour = max(
            DEFAULT_MAX_KWH_PER_HOUR,
            self.total_capacity * 1.2 / max(self.num_groups, 1),
        )
        _LOGGER.info(
            f"AIPredictor initialized: {self.num_groups} groups, {self.total_capacity:.2f} kWp total, max_kwh_per_hour={self._max_kwh_per_hour:.2f}"
        )

    def _calculate_total_capacity(
        self, panel_groups: List[Dict[str, Any]], fallback_capacity: float
    ) -> float:
        """Calculate total capacity from panel groups @zara"""
        if not panel_groups:
            return fallback_capacity
        total_capacity_kwp = 0.0
        for panel_group in panel_groups:
            if "capacity_kwp" in panel_group:
                total_capacity_kwp += float(panel_group["capacity_kwp"])
            elif "kwp" in panel_group:
                total_capacity_kwp += float(panel_group["kwp"])
            elif "power_wp" in panel_group:
                total_capacity_kwp += float(panel_group["power_wp"]) / 1000.0
            else:
                total_capacity_kwp += 1.0
        return total_capacity_kwp if total_capacity_kwp > 0 else fallback_capacity

    def set_entities(
        self,
        solar_capacity: float = 5.0,
        power_entity: Optional[str] = None,
        weather_entity: Optional[str] = None,
        **kwargs,
    ):
        """Set entity configuration @zara"""
        self.solar_capacity = solar_capacity
        self.power_entity = power_entity
        self.weather_entity = weather_entity

    async def initialize(self) -> bool:
        """Initialize AI components with Multi-Output LSTM @zara"""
        try:
            self.dni_tracker = DniTracker(self.db_manager)
            self.seasonal = SeasonalAdjuster(self.db_manager)
            await self.dni_tracker.async_load()
            await self.seasonal.async_load()
            self.feature_engineer = FeatureEngineer(self.dni_tracker)
            output_count = self.num_groups if self.num_groups > 0 else 1
            feature_count = calculate_feature_count(self.num_groups)
            self.use_attention = True
            stored_weights = await self.db_manager.get_model_weights()
            self.lstm = TinyLSTM(
                input_size=feature_count,
                hidden_sizes=(48, 24),
                sequence_length=24,
                num_outputs=output_count,
                num_heads=4,
            )
            self.ridge = TinyRidge(
                input_size=feature_count,
                hidden_size=48,
                sequence_length=24,
                num_outputs=output_count,
            )
            _LOGGER.info(
                f"Models initialized: {feature_count} features, {output_count} outputs, {self.num_groups} groups, layers=2, heads=4"
            )
            if stored_weights:
                try:
                    stored_version = stored_weights.get("version", "1.0")
                    if stored_version == MODEL_FORMAT_VERSION:
                        await self._load_dual_model_weights(
                            stored_weights, feature_count, output_count
                        )
                    else:
                        _LOGGER.warning(
                            "Incompatible weight version %s - EAI consumption model %s requires retraining",
                            stored_version,
                            MODEL_FORMAT_VERSION,
                        )
                        self.state = ModelState.UNTRAINED
                except Exception as predictor_error:
                    _LOGGER.warning(f"Could not load weights: {predictor_error}")
                    self.state = ModelState.UNTRAINED
            else:
                self.state = ModelState.UNTRAINED
                self.active_model = "none"
                _LOGGER.info(
                    f"No weights found - model untrained ({output_count} outputs)"
                )
            if True:
                return True
        except Exception as predictor_error:
            _LOGGER.error(f"AI initialization failed: {predictor_error}")
            self.state = ModelState.ERROR
            return False

    def is_ready(self) -> bool:
        """Check if model is ready for predictions @zara"""
        if self.state != ModelState.READY:
            return False
        if (
            self.active_model == "tiny_lstm"
            and self.lstm is not None
            and self._lstm_ready
        ):
            return True
        if (
            self.active_model == "ridge"
            and self.ridge is not None
            and self.ridge.is_trained
        ):
            return True
        return False

    def is_healthy(self) -> bool:
        """Check if predictor is in healthy state @zara"""
        has_model = self.lstm is not None or self.ridge is not None
        return self.state in (ModelState.READY, ModelState.UNTRAINED) and has_model

    def is_bootstrap_phase(self) -> bool:
        """Check if we're in bootstrap phase (Ridge active, 10-50 samples) @zara"""
        return (
            self.active_model == "ridge"
            and self.training_samples is not None
            and (self.training_samples >= MIN_RIDGE_SAMPLES)
            and (self.training_samples < MIN_TRAINING_SAMPLES)
        )

    def get_bootstrap_progress(self) -> float:
        """Get progress through bootstrap phase (0.0 to 1.0) @zara"""
        if not self.training_samples:
            return 0.0
        bootstrap_progress = (self.training_samples - MIN_RIDGE_SAMPLES) / (
            MIN_TRAINING_SAMPLES - MIN_RIDGE_SAMPLES
        )
        return max(0.0, min(1.0, bootstrap_progress))

    def get_base_ai_confidence(self) -> float:
        """Get base confidence level for AI predictions (0.0 - 1.0) @zara"""
        if not self.is_ready():
            return 0.0
        accuracy = self.current_accuracy or 0.0
        if accuracy <= 0:
            return 0.0
        if accuracy < 0.5:
            return accuracy
        if accuracy < 0.8:
            return 0.5 + (accuracy - 0.5) * 1.333
        return 0.9 + min(accuracy - 0.8, 0.2) * 0.5

    @property
    def base_ai_confidence(self) -> float:
        """Property accessor for base AI confidence @zara"""
        return self.get_base_ai_confidence()

    async def predict_hour(
        self,
        hour: int,
        weather_data: Dict[str, Any],
        astronomy_data: Dict[str, Any],
        target_datetime: Optional[datetime] = None,
    ) -> float:
        """Predict total production for single hour (sum of all groups) @zara"""
        if not self.is_ready():
            return 0.0
        try:
            group_predictions = await self.predict_hour_per_group(
                hour, weather_data, astronomy_data, target_datetime
            )
            total_power_kwh = sum(
                (group_prediction.power_kwh for group_prediction in group_predictions)
            )
            return max(0.0, total_power_kwh)
        except Exception as predictor_error:
            _LOGGER.error(f"Prediction failed: {predictor_error}")
            return 0.0

    def _get_model_predictions(self, sequence: List[List[float]]) -> List[float]:
        """Get predictions from the active model (Ridge or LSTM) @zara"""
        try:
            if self.active_model == "tiny_lstm" and self.lstm and self._lstm_ready:
                return self.lstm.predict(sequence)
            if self.active_model == "ridge" and self.ridge and self.ridge.is_trained:
                return self.ridge.predict(sequence)
            _LOGGER.debug(f"No active model available (active={self.active_model})")
            return []
        except Exception as predictor_error:
            _LOGGER.debug(f"Model prediction failed: {predictor_error}")
            return []

    def _get_ensemble_predictions(
        self, sequence: List[List[float]]
    ) -> Dict[str, List[float]]:
        """Get predictions from both LSTM and Ridge independently @zara V16.0.2"""
        ensemble_predictions = {"lstm": [], "ridge": []}
        if self.lstm and self._lstm_ready:
            try:
                ensemble_predictions["lstm"] = self.lstm.predict(sequence)
            except Exception as predictor_error:
                _LOGGER.debug(f"LSTM ensemble prediction failed: {predictor_error}")
        if self.ridge and self.ridge.is_trained:
            try:
                ensemble_predictions["ridge"] = self.ridge.predict(sequence)
            except Exception as predictor_error:
                _LOGGER.debug(f"Ridge ensemble prediction failed: {predictor_error}")
        return ensemble_predictions if True else None

    async def predict_hour_per_group_ensemble(
        self,
        hour: int,
        weather_data: Dict[str, Any],
        astronomy_data: Dict[str, Any],
        target_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Predict production per panel group using both LSTM and Ridge @zara V16.0.2

        Returns dict with lstm_predictions, ridge_predictions, lstm_total, ridge_total.
        """
        ensemble_result = {
            "lstm_predictions": [],
            "ridge_predictions": [],
            "lstm_total": 0.0,
            "ridge_total": 0.0,
        }
        if not self.is_ready():
            return ensemble_result
        try:
            prediction_scale = 1.0
            historical_predictions = await self._load_predictions_for_sequence()
            target_datetime_value = target_datetime or datetime.now()
            target_date_str = target_datetime_value.strftime("%Y-%m-%d")
            sequence, previous_day_production, previous_hour_production = (
                await self._build_temporal_sequence(
                    target_hour=hour,
                    target_date=target_date_str,
                    weather_data=weather_data,
                    astronomy_data=astronomy_data,
                    all_predictions=historical_predictions,
                )
            )
            model_predictions = self._get_ensemble_predictions(sequence)

            def _build_group_predictions(
                predictions: List[float],
            ) -> List[GroupPrediction]:
                group_predictions = []
                if self.panel_groups:
                    for group_index, panel_group in enumerate(self.panel_groups):
                        if predictions and group_index < len(predictions):
                            group_power_kwh = (
                                predictions[group_index]
                                * self._max_kwh_per_hour
                                * prediction_scale
                            )
                        else:
                            group_power_kwh = 0.0
                        group_predictions.append(
                            GroupPrediction(
                                group_name=panel_group.get(
                                    "name", f"Group_{group_index + 1}"
                                ),
                                power_kwh=max(0.0, group_power_kwh),
                                azimuth=panel_group.get("azimuth", 180.0),
                                tilt=panel_group.get("tilt", 30.0),
                                capacity_kwp=panel_group.get(
                                    "capacity_kwp", panel_group.get("kwp", 1.0)
                                ),
                            )
                        )
                else:
                    group_power_kwh = (
                        predictions[0] * self._max_kwh_per_hour * prediction_scale
                        if predictions
                        else 0.0
                    )
                    group_predictions.append(
                        GroupPrediction(
                            group_name="Total",
                            power_kwh=max(0.0, group_power_kwh),
                            azimuth=180.0,
                            tilt=30.0,
                            capacity_kwp=self.solar_capacity,
                        )
                    )
                return group_predictions if True else None

            lstm_group_predictions = _build_group_predictions(model_predictions["lstm"])
            ridge_group_predictions = _build_group_predictions(
                model_predictions["ridge"]
            )
            return {
                "lstm_predictions": lstm_group_predictions,
                "ridge_predictions": ridge_group_predictions,
                "lstm_total": sum(
                    (
                        group_prediction.power_kwh
                        for group_prediction in lstm_group_predictions
                    )
                ),
                "ridge_total": sum(
                    (
                        group_prediction.power_kwh
                        for group_prediction in ridge_group_predictions
                    )
                ),
            }
        except Exception as predictor_error:
            _LOGGER.error(f"Ensemble prediction failed: {predictor_error}")
            return ensemble_result

    async def predict_hour_per_group(
        self,
        hour: int,
        weather_data: Dict[str, Any],
        astronomy_data: Dict[str, Any],
        target_datetime: Optional[datetime] = None,
    ) -> List[GroupPrediction]:
        """Predict production per panel group for single hour @zara"""
        if not self.is_ready():
            return [] if True else None
        try:
            prediction_scale = 1.0
            historical_predictions = await self._load_predictions_for_sequence()
            target_datetime_value = target_datetime or datetime.now()
            target_date_str = target_datetime_value.strftime("%Y-%m-%d")
            sequence, previous_day_production, previous_hour_production = (
                await self._build_temporal_sequence(
                    target_hour=hour,
                    target_date=target_date_str,
                    weather_data=weather_data,
                    astronomy_data=astronomy_data,
                    all_predictions=historical_predictions,
                )
            )
            group_predictions = []
            predictions = self._get_model_predictions(sequence)
            if self.panel_groups:
                if not predictions or len(predictions) == 0:
                    predictions = []
                for group_index, panel_group in enumerate(self.panel_groups):
                    if predictions and group_index < len(predictions):
                        group_power_kwh = (
                            predictions[group_index]
                            * self._max_kwh_per_hour
                            * prediction_scale
                        )
                    else:
                        group_power_kwh = 0.0
                    group_predictions.append(
                        GroupPrediction(
                            group_name=panel_group.get(
                                "name", f"Group_{group_index + 1}"
                            ),
                            power_kwh=max(0.0, group_power_kwh),
                            azimuth=panel_group.get("azimuth", 180.0),
                            tilt=panel_group.get("tilt", 30.0),
                            capacity_kwp=panel_group.get(
                                "capacity_kwp", panel_group.get("kwp", 1.0)
                            ),
                        )
                    )
            else:
                group_power_kwh = (
                    predictions[0] * self._max_kwh_per_hour * prediction_scale
                    if predictions
                    else 0.0
                )
                group_predictions.append(
                    GroupPrediction(
                        group_name="Total",
                        power_kwh=max(0.0, group_power_kwh),
                        azimuth=180.0,
                        tilt=30.0,
                        capacity_kwp=self.solar_capacity,
                    )
                )
            return group_predictions if True else None
        except Exception as predictor_error:
            _LOGGER.error(f"Per-group prediction failed: {predictor_error}")
            return [] if True else None

    async def train_model(self, run_feature_importance: bool = False) -> TrainingResult:
        """Train AI models with historical data @zara"""
        if not self.feature_engineer:
            return TrainingResult(
                success=False, error_message="FeatureEngineer not initialized"
            )
        lstm_was_ready = (
            self.active_model == "tiny_lstm"
            and self.lstm is not None
            and self._lstm_ready
        )
        ridge_was_ready = (
            self.active_model == "ridge"
            and self.ridge is not None
            and self.ridge.is_trained
        )
        model_was_ready = lstm_was_ready or ridge_was_ready
        self.state = ModelState.TRAINING
        _LOGGER.info(f"AI training started: {self.num_groups} outputs")
        try:
            (
                training_sequences,
                training_targets,
                daily_production_totals,
                daily_physics_totals,
            ) = await self._prepare_training_data()
            sample_count = len(training_sequences)
            if sample_count < MIN_RIDGE_SAMPLES:
                if model_was_ready:
                    self.state = ModelState.READY
                else:
                    self.state = ModelState.UNTRAINED
                    self.active_model = "none"
                return TrainingResult(
                    success=False,
                    samples_used=sample_count,
                    error_message=f"Ridge warm-up: {sample_count}/{MIN_RIDGE_SAMPLES} valid samples",
                    reason_code=REASON_RIDGE_WARMUP,
                )
            feature_count = calculate_feature_count(self.num_groups)
            output_count = self.num_groups if self.num_groups > 0 else 1
            ridge_result = None
            lstm_result = None
            lstm_training_attempted = False
            if self.ridge and sample_count >= MIN_RIDGE_SAMPLES:
                _LOGGER.info(f"Training Ridge with {sample_count} samples...")
                ridge_result = await self.ridge.train(
                    X_sequences=training_sequences,
                    y_targets=training_targets,
                    validation_split=0.2,
                )
                if ridge_result.get("success"):
                    _LOGGER.info(
                        f"Ridge trained: R²={ridge_result.get('accuracy', 0):.3f}, alpha={ridge_result.get('alpha', 0):.4f}"
                    )
                    lstm_training_attempted = True
            if self.lstm and sample_count >= MIN_TRAINING_SAMPLES:
                _LOGGER.info(f"Training LSTM with {sample_count} samples...")
                lstm_result = await self.lstm.train(
                    X_sequences=training_sequences,
                    y_targets=training_targets,
                    epochs=200,
                    batch_size=16,
                    validation_split=0.2,
                    early_stopping_patience=20,
                )
                if lstm_result.get("success"):
                    self._lstm_ready = True
                    _LOGGER.info(
                        f"LSTM trained: R²={lstm_result.get('accuracy', 0):.3f}, epochs={lstm_result.get('epochs_trained', 0)}, layers=2, heads=4"
                    )
                    lstm_training_attempted = True
            if lstm_training_attempted:
                self._select_active_model(sample_count, ridge_result, lstm_result)
                self.training_samples = sample_count
                if (
                    self.active_model == "tiny_lstm"
                    and lstm_result
                    and lstm_result.get("success")
                ):
                    selected_result = lstm_result
                elif ridge_result and ridge_result.get("success"):
                    selected_result = ridge_result
                else:
                    selected_result = None
                self.current_accuracy = (
                    selected_result.get("accuracy", 0.0) if selected_result else 0.0
                )
                self.current_rmse = (
                    selected_result.get("rmse", 0.0) if selected_result else 0.0
                )
                self.last_training_time = datetime.now()
                self.last_training_samples = sample_count
                await self._save_weights_async()
                await self._update_dni_tracker()
                self.state = ModelState.READY
                rmse_text = (
                    f", RMSE={self.current_rmse:.3f}kWh" if self.current_rmse else ""
                )
                _LOGGER.info(
                    f"Training complete: active={self.active_model}, R²={self.current_accuracy:.3f}{rmse_text}, samples={sample_count}, outputs={output_count}"
                )
                if run_feature_importance and sample_count >= MIN_TRAINING_SAMPLES:
                    _LOGGER.debug(
                        "Running post-training feature importance analysis..."
                    )
                    await self.analyze_feature_importance(num_permutations=3)
                return TrainingResult(
                    success=True,
                    accuracy=self.current_accuracy,
                    samples_used=sample_count,
                    feature_count=feature_count,
                    num_outputs=output_count,
                    epochs_trained=(
                        lstm_result.get("epochs_trained", 0) if lstm_result else 1
                    ),
                    has_attention=self.use_attention,
                    rmse=self.current_rmse or 0.0,
                )
            if model_was_ready:
                self.state = ModelState.READY
            else:
                self.state = ModelState.ERROR
                self.active_model = "none"
            return TrainingResult(
                success=False, error_message="Both Ridge and LSTM training failed"
            )
        except Exception as predictor_error:
            _LOGGER.error(f"Training failed: {predictor_error}")
            if model_was_ready:
                self.state = ModelState.READY
            else:
                self.state = ModelState.ERROR
                self.active_model = "none"
            return TrainingResult(
                success=False,
                error_message=str(predictor_error),
                reason_code=REASON_TRAINING_DATA_UNAVAILABLE,
            )

    def _select_active_model(
        self,
        n_samples: int,
        ridge_result: Optional[Dict[str, Any]],
        lstm_result: Optional[Dict[str, Any]],
    ) -> None:
        """Select which model to use for predictions @zara"""
        ridge_succeeded = ridge_result and ridge_result.get("success")
        lstm_succeeded = lstm_result and lstm_result.get("success")
        if n_samples >= MIN_TRAINING_SAMPLES and lstm_succeeded:
            self.active_model = "tiny_lstm"
            _LOGGER.info(f"Active model: LSTM ({n_samples} samples)")
        elif ridge_succeeded:
            self.active_model = "ridge"
            _LOGGER.info(f"Active model: Ridge ({n_samples} samples, bootstrap phase)")
        else:
            self.active_model = "none"
            _LOGGER.warning("No model available")
        return None if True else None

    async def _prepare_training_data(
        self,
    ) -> Tuple[List[List[List[float]]], List[Any], Dict[str, float], Dict[str, float]]:
        """Load and prepare Multi-Output training data from database @zara"""
        training_sequences = []
        training_targets = []
        daily_production_totals = {}
        daily_physics_totals = {}
        try:
            today = datetime.now().date()
            cutoff_date = today - timedelta(days=TRAINING_DAYS)
            learning_samples = await self._learning_store.async_load_samples(
                cutoff_date.isoformat(), today.isoformat()
            )
            if learning_samples is None:
                raise RuntimeError(
                    "Central EAI learning samples unavailable; training deferred"
                )
            predictions_by_key = {}
            for learning_sample in learning_samples:
                training_date_str = learning_sample.get("target_date")
                sample_hour = learning_sample.get("target_hour")
                if not (
                    isinstance(training_date_str, str) and isinstance(sample_hour, int)
                ):
                    continue
                interval_start_utc = learning_sample.get("interval_start_utc")
                if not isinstance(interval_start_utc, str):
                    interval_start_utc = (
                        f"{training_date_str}T{sample_hour:02d}:00:00+00:00"
                    )
                interval_datetime_utc = datetime.fromisoformat(
                    interval_start_utc.replace("Z", "+00:00")
                ).astimezone(UTC)
                interval_key = interval_datetime_utc.isoformat().replace("+00:00", "Z")
                predictions_by_key[interval_key] = learning_sample
            for training_record in predictions_by_key.values():
                self._process_hour_data_for_training(
                    training_record,
                    cutoff_date,
                    today,
                    training_sequences,
                    training_targets,
                    daily_production_totals,
                    daily_physics_totals,
                    predictions_by_key,
                )
            _LOGGER.info(
                "Prepared %d central EAI training samples from %d days, %d outputs",
                len(training_sequences),
                len(daily_production_totals),
                self.num_groups,
            )
            return (
                training_sequences,
                training_targets,
                daily_production_totals,
                daily_physics_totals,
            )
        except Exception as predictor_error:
            _LOGGER.error(f"Error preparing training data: {predictor_error}")
            raise RuntimeError(
                "Central EAI training data preparation failed"
            ) from predictor_error

    def _process_hour_data_for_training(
        self,
        hour_data: Dict[str, Any],
        cutoff: Any,
        today: Any,
        X_sequences: List,
        y_targets: List,
        daily_productions: Dict[str, float],
        daily_physics: Dict[str, float],
        predictions_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        """Process single hour data for Multi-Output training @zara"""
        training_date_str = hour_data.get("target_date")
        if not training_date_str:
            return
        try:
            training_date = datetime.strptime(training_date_str, "%Y-%m-%d").date()
            if training_date < cutoff or training_date >= today:
                return
        except ValueError:
            return
        training_hour = hour_data.get("target_hour")
        if training_hour is None:
            return
        actual_kwh = hour_data.get("actual_kwh")
        group_actuals = hour_data.get("panel_group_actuals")
        if actual_kwh is None or actual_kwh < 0:
            return None
        flags = hour_data.get("flags", {}) or {}
        if flags.get("is_outlier") or flags.get("inverter_clipped"):
            return None
        target_datetime_value = datetime.strptime(training_date_str, "%Y-%m-%d")
        interval_start_utc = hour_data.get("interval_start_utc")
        if isinstance(interval_start_utc, str):
            interval_datetime_utc = datetime.fromisoformat(
                interval_start_utc.replace("Z", "+00:00")
            ).astimezone(UTC)
        else:
            interval_datetime_utc = target_datetime_value.replace(
                hour=int(training_hour), tzinfo=UTC
            )
        training_previous_date = (target_datetime_value - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        previous_day_production = sum(
            (
                float(prediction_record.get("actual_kwh") or 0.0)
                for prediction_record in predictions_by_id.values()
                if prediction_record.get("target_date") == training_previous_date
            )
        )
        previous_hour_production = sum(
            (
                float(prediction_record.get("actual_kwh") or 0.0)
                for prediction_record in predictions_by_id.values()
                if prediction_record.get("target_date") == training_previous_date
                if prediction_record.get("target_hour") == training_hour
            )
        )
        sequence = []
        null_feature_count = 0
        for hour_offset in range(-23, 1):
            sequence_interval_utc = interval_datetime_utc + timedelta(hours=hour_offset)
            prediction_key = sequence_interval_utc.isoformat().replace("+00:00", "Z")
            historical_record = predictions_by_id.get(prediction_key)
            if historical_record:
                sample_date_str = str(
                    historical_record.get("target_date") or training_date_str
                )
                sample_hour = int(historical_record.get("target_hour", training_hour))
                sample_datetime = datetime.strptime(
                    sample_date_str, "%Y-%m-%d"
                ).replace(hour=sample_hour)
            else:
                sample_datetime = target_datetime_value.replace(
                    hour=training_hour
                ) + timedelta(hours=hour_offset)
                sample_date_str = sample_datetime.strftime("%Y-%m-%d")
                sample_hour = sample_datetime.hour
            if historical_record and hour_offset < 0:
                historical_weather = historical_record.get("weather_corrected") or {}
                historical_astronomy = historical_record.get("astronomy") or {}
                sample_previous_date = (sample_datetime - timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
                sample_previous_day_production = sum(
                    (
                        float(prediction_record.get("actual_kwh") or 0.0)
                        for prediction_record in predictions_by_id.values()
                        if prediction_record.get("target_date") == sample_previous_date
                    )
                )
                sample_previous_hour_production = sum(
                    (
                        float(prediction_record.get("actual_kwh") or 0.0)
                        for prediction_record in predictions_by_id.values()
                        if prediction_record.get("target_date") == sample_previous_date
                        if prediction_record.get("target_hour") == sample_hour
                    )
                )
                feature_record = {
                    "target_hour": sample_hour,
                    "target_day_of_year": sample_datetime.timetuple().tm_yday,
                    "target_month": sample_datetime.month,
                    "weather_corrected": {
                        "temperature": historical_weather.get(
                            "temperature", historical_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": historical_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": historical_weather.get(
                            "wind", historical_weather.get("wind_speed", 3)
                        ),
                        "humidity": historical_weather.get(
                            "humidity", historical_weather.get("humidity_percent", 70)
                        ),
                        "rain": historical_weather.get(
                            "rain", historical_weather.get("precipitation", 0)
                        ),
                        "clouds": historical_weather.get(
                            "clouds", historical_weather.get("cloud_cover", 50)
                        ),
                        "dni": historical_weather.get("direct_radiation", 0),
                        "pressure": historical_weather.get("pressure", 1013.0),
                        "diffuse_radiation": historical_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": historical_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": historical_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": historical_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": historical_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": historical_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": historical_astronomy.get(
                            "daylight_hours", 12.0
                        ),
                        "day_progress_ratio": historical_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": historical_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": historical_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": historical_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": sample_previous_day_production,
                    "production_same_hour_yesterday": sample_previous_hour_production,
                }
            else:
                current_weather = hour_data.get("weather_corrected", {}) or {}
                current_astronomy = hour_data.get("astronomy", {}) or {}
                feature_record = {
                    "target_hour": training_hour,
                    "target_day_of_year": target_datetime_value.timetuple().tm_yday,
                    "target_month": target_datetime_value.month,
                    "weather_corrected": {
                        "temperature": current_weather.get(
                            "temperature", current_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": current_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": current_weather.get(
                            "wind", current_weather.get("wind_speed", 3)
                        ),
                        "humidity": current_weather.get(
                            "humidity", current_weather.get("humidity_percent", 70)
                        ),
                        "rain": current_weather.get(
                            "rain", current_weather.get("precipitation", 0)
                        ),
                        "clouds": current_weather.get(
                            "clouds", current_weather.get("cloud_cover", 50)
                        ),
                        "dni": current_weather.get("direct_radiation", 0),
                        "pressure": current_weather.get("pressure", 1013.0),
                        "diffuse_radiation": current_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": current_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": current_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": current_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": current_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": current_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": current_astronomy.get("daylight_hours", 12.0),
                        "day_progress_ratio": current_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": current_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": current_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": current_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": previous_day_production,
                    "production_same_hour_yesterday": previous_hour_production,
                }
            features = self.feature_engineer.extract_record_features(feature_record)
            if features is None:
                null_feature_count += 1
                feature_count = calculate_feature_count(self.num_groups)
                features = [0.0] * feature_count
            sequence.append(features)
            continue
        if null_feature_count > 12:
            _LOGGER.debug(
                "Skipping training sample %s_%02d: %d/24 timesteps had null features",
                training_date_str,
                training_hour,
                null_feature_count,
            )
            return
        if self.panel_groups:
            if group_actuals and isinstance(group_actuals, dict):
                normalized_targets = []
                for panel_group in self.panel_groups:
                    group_name = panel_group.get("name", "")
                    group_actual_kwh = group_actuals.get(group_name, 0.0)
                    normalized_targets.append(
                        float(group_actual_kwh) / self._max_kwh_per_hour
                    )
            else:
                normalized_targets = []
                for panel_group in self.panel_groups:
                    group_capacity_kwp = panel_group.get(
                        "capacity_kwp", panel_group.get("kwp", 1.0)
                    )
                    capacity_share = (
                        group_capacity_kwp / self.total_capacity
                        if self.total_capacity > 0
                        else 1.0 / self.num_groups
                    )
                    normalized_targets.append(
                        actual_kwh * capacity_share / self._max_kwh_per_hour
                    )
        else:
            normalized_targets = [actual_kwh / self._max_kwh_per_hour]
        X_sequences.append(sequence)
        y_targets.append(normalized_targets)
        if training_date_str not in daily_productions:
            daily_productions[training_date_str] = 0.0
        daily_productions[training_date_str] += actual_kwh
        physics_kwh = hour_data.get("physics_kwh") or 0.0
        if training_date_str not in daily_physics:
            daily_physics[training_date_str] = 0.0
        daily_physics[training_date_str] += physics_kwh

    async def _update_seasonal_factors(
        self, daily_productions: Dict[str, float], daily_physics: Dict[str, float]
    ):
        """Update seasonal adjustment factors based on production data @zara"""
        if not self.seasonal or not daily_productions:
            return
        try:
            productions_by_month = {}
            physics_by_month = {}
            for training_date_str, daily_production in daily_productions.items():
                try:
                    training_date = datetime.strptime(training_date_str, "%Y-%m-%d")
                    month = training_date.month
                    if month not in productions_by_month:
                        productions_by_month[month] = []
                    productions_by_month[month].append(daily_production)
                    daily_physics_kwh = daily_physics.get(training_date_str, 0.0)
                    if daily_physics_kwh > 0:
                        if month not in physics_by_month:
                            physics_by_month[month] = []
                        physics_by_month[month].append(daily_physics_kwh)
                except ValueError:
                    continue
            for month, monthly_productions in productions_by_month.items():
                if len(monthly_productions) >= 3:
                    average_production = sum(monthly_productions) / len(
                        monthly_productions
                    )
                    monthly_physics = physics_by_month.get(month, [])
                    if monthly_physics:
                        average_physics = sum(monthly_physics) / len(monthly_physics)
                        self.seasonal.update(month, average_production, average_physics)
                    else:
                        fallback_physics_kwh = self.total_capacity * 5.0
                        if fallback_physics_kwh > 0:
                            self.seasonal.update(
                                month, average_production, fallback_physics_kwh
                            )
            await self.seasonal.async_save()
            _LOGGER.debug(
                f"Updated seasonal factors for {len(productions_by_month)} months"
            )
        except Exception as predictor_error:
            _LOGGER.warning(f"Error updating seasonal factors: {predictor_error}")
        return None if True else None

    async def _update_dni_tracker(self):
        """Update DNI tracker with today's data @zara"""
        if not self.dni_tracker:
            return
        try:
            today = datetime.now().date().isoformat()
            database_rows = await self.db_manager.fetchall(
                "SELECT hp.target_hour, pw.direct_radiation\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?",
                (today,),
            )
            for database_row in database_rows:
                direct_radiation = database_row[1] or 0
                if direct_radiation > 0:
                    sample_hour = database_row[0]
                    self.dni_tracker.record_dni(sample_hour, direct_radiation)
            await self.dni_tracker.async_save()
        except Exception as predictor_error:
            _LOGGER.warning(f"Error updating DNI tracker: {predictor_error}")
        return None if True else None

    async def _build_temporal_sequence(
        self,
        target_hour: int,
        target_date: str,
        weather_data: Dict[str, Any],
        astronomy_data: Dict[str, Any],
        all_predictions: List[Dict[str, Any]],
    ) -> Tuple[List[List[float]], float, float]:
        """Build a real 24-hour temporal sequence for LSTM prediction @zara"""
        try:
            target_datetime_value = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            target_datetime_value = datetime.now()
            target_date = target_datetime_value.strftime("%Y-%m-%d")
        predictions_by_key = {}
        for indexed_prediction in all_predictions:
            prediction_id = indexed_prediction.get("id")
            if prediction_id:
                predictions_by_key[prediction_id] = indexed_prediction
        training_previous_date = (target_datetime_value - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        previous_day_production = 0.0
        for day_hour in range(24):
            prediction_key = f"{training_previous_date}_{day_hour:02d}"
            daily_prediction_record = predictions_by_key.get(prediction_key)
            if daily_prediction_record:
                if daily_prediction_record.get("actual_kwh") is not None:
                    previous_day_production += daily_prediction_record.get(
                        "actual_kwh", 0.0
                    )
        previous_day_hour_key = f"{training_previous_date}_{target_hour:02d}"
        previous_day_hour_record = predictions_by_key.get(previous_day_hour_key)
        previous_hour_production = (
            previous_day_hour_record.get("actual_kwh", 0.0)
            if previous_day_hour_record
            and previous_day_hour_record.get("actual_kwh") is not None
            else 0.0
        )
        sequence = []
        for hour_offset in range(-23, 1):
            sample_datetime = target_datetime_value.replace(
                hour=target_hour
            ) + timedelta(hours=hour_offset)
            sample_date_str = sample_datetime.strftime("%Y-%m-%d")
            sample_hour = sample_datetime.hour
            prediction_key = f"{sample_date_str}_{sample_hour:02d}"
            historical_record = predictions_by_key.get(prediction_key)
            if historical_record and hour_offset < 0:
                historical_weather = (
                    historical_record.get("weather_corrected")
                    or historical_record.get("weather_forecast")
                    or {}
                )
                historical_astronomy = historical_record.get("astronomy") or {}
                sample_previous_date = (sample_datetime - timedelta(days=1)).strftime(
                    "%Y-%m-%d"
                )
                sample_previous_day_production = 0.0
                for day_hour in range(24):
                    prediction_id = f"{sample_previous_date}_{day_hour:02d}"
                    indexed_prediction = predictions_by_key.get(prediction_id)
                    if indexed_prediction:
                        if indexed_prediction.get("actual_kwh") is not None:
                            sample_previous_day_production += indexed_prediction.get(
                                "actual_kwh", 0.0
                            )
                sample_previous_hour_key = f"{sample_previous_date}_{sample_hour:02d}"
                sample_previous_hour_record = predictions_by_key.get(
                    sample_previous_hour_key
                )
                sample_previous_hour_production = (
                    sample_previous_hour_record.get("actual_kwh", 0.0)
                    if sample_previous_hour_record
                    and sample_previous_hour_record.get("actual_kwh") is not None
                    else 0.0
                )
                feature_record = {
                    "target_hour": sample_hour,
                    "target_day_of_year": sample_datetime.timetuple().tm_yday,
                    "target_month": sample_datetime.month,
                    "weather_corrected": {
                        "temperature": historical_weather.get(
                            "temperature", historical_weather.get("temperature_c", 15)
                        ),
                        "solar_radiation_wm2": historical_weather.get(
                            "solar_radiation_wm2", 0
                        ),
                        "wind": historical_weather.get(
                            "wind", historical_weather.get("wind_speed", 3)
                        ),
                        "humidity": historical_weather.get(
                            "humidity", historical_weather.get("humidity_percent", 70)
                        ),
                        "rain": historical_weather.get(
                            "rain", historical_weather.get("precipitation", 0)
                        ),
                        "clouds": historical_weather.get(
                            "clouds", historical_weather.get("cloud_cover", 50)
                        ),
                        "dni": historical_weather.get("direct_radiation", 0),
                        "pressure": historical_weather.get("pressure", 1013.0),
                        "diffuse_radiation": historical_weather.get(
                            "diffuse_radiation", 0
                        ),
                    },
                    "astronomy": {
                        "sun_elevation_deg": historical_astronomy.get(
                            "sun_elevation_deg", 0
                        ),
                        "theoretical_max_kwh": historical_astronomy.get(
                            "theoretical_max_kwh", 0
                        ),
                        "clear_sky_radiation_wm2": historical_astronomy.get(
                            "clear_sky_radiation_wm2", 0
                        ),
                        "max_elevation_today": historical_astronomy.get(
                            "max_elevation_today", 60
                        ),
                        "sun_azimuth_deg": historical_astronomy.get(
                            "sun_azimuth_deg", 180
                        ),
                        "daylight_hours": historical_astronomy.get(
                            "daylight_hours", 12.0
                        ),
                        "day_progress_ratio": historical_astronomy.get(
                            "day_progress_ratio", 0.5
                        ),
                        "hours_after_sunrise": historical_astronomy.get(
                            "hours_after_sunrise", 6.0
                        ),
                        "hours_before_sunset": historical_astronomy.get(
                            "hours_before_sunset", 6.0
                        ),
                        "hours_since_solar_noon": historical_astronomy.get(
                            "hours_since_solar_noon", 0.0
                        ),
                    },
                    "production_yesterday": sample_previous_day_production,
                    "production_same_hour_yesterday": sample_previous_hour_production,
                }
            else:
                feature_record = {
                    "target_hour": sample_hour if hour_offset < 0 else target_hour,
                    "target_day_of_year": (
                        sample_datetime.timetuple().tm_yday
                        if hour_offset < 0
                        else target_datetime_value.timetuple().tm_yday
                    ),
                    "target_month": (
                        sample_datetime.month
                        if hour_offset < 0
                        else target_datetime_value.month
                    ),
                    "weather_corrected": weather_data,
                    "astronomy": astronomy_data,
                    "production_yesterday": previous_day_production,
                    "production_same_hour_yesterday": previous_hour_production,
                }
            features = self.feature_engineer.extract_record_features(feature_record)
            if features is None:
                feature_count = calculate_feature_count(self.num_groups)
                features = [0.0] * feature_count
            sequence.append(features)
            continue
        return (sequence, previous_day_production, previous_hour_production)

    async def _load_predictions_for_sequence(self) -> List[Dict[str, Any]]:
        """Load recent predictions for building temporal sequences @zara"""
        try:
            database_rows = await self.db_manager.fetchall(
                "SELECT hp.target_date, hp.target_hour, hp.actual_kwh, hp.prediction_id,\n                          pw.temperature, pw.solar_radiation_wm2, pw.wind, pw.humidity,\n                          pw.rain, pw.clouds,\n                          pa.sun_elevation_deg, pa.theoretical_max_kwh, pa.clear_sky_radiation_wm2,\n                          pw.pressure,\n                          pw.diffuse_radiation,\n                          pw.direct_radiation,\n                          pa.sun_azimuth_deg,\n                          pa.daylight_hours,\n                          pa.day_progress_ratio,\n                          pa.hours_after_sunrise,\n                          pa.hours_before_sunset,\n                          pa.hours_since_solar_noon\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date >= date('now', '-7 days')\n                   ORDER BY hp.target_date, hp.target_hour"
            )
            historical_predictions = []
            for database_row in database_rows:
                prediction_key = f"{database_row[0]}_{database_row[1]:02d}"
                historical_predictions.append(
                    {
                        "id": prediction_key,
                        "target_date": database_row[0],
                        "target_hour": database_row[1],
                        "actual_kwh": database_row[2],
                        "weather_corrected": (
                            {
                                "temperature": database_row[4],
                                "solar_radiation_wm2": database_row[5],
                                "wind": database_row[6],
                                "humidity": database_row[7],
                                "rain": database_row[8],
                                "clouds": database_row[9],
                                "pressure": database_row[13],
                                "diffuse_radiation": database_row[14],
                                "dni": database_row[15],
                            }
                            if database_row[4] is not None
                            else None
                        ),
                        "astronomy": (
                            {
                                "sun_elevation_deg": database_row[10],
                                "theoretical_max_kwh": database_row[11],
                                "clear_sky_radiation_wm2": database_row[12],
                                "sun_azimuth_deg": database_row[16],
                                "daylight_hours": database_row[17],
                                "day_progress_ratio": database_row[18],
                                "hours_after_sunrise": database_row[19],
                                "hours_before_sunset": database_row[20],
                                "hours_since_solar_noon": database_row[21],
                            }
                            if database_row[10] is not None
                            else None
                        ),
                    }
                )
                continue
            return historical_predictions
        except Exception as predictor_error:
            _LOGGER.debug(f"Could not load predictions for sequence: {predictor_error}")
            return []

    async def _load_dual_model_weights(
        self, weights: Dict[str, Any], feature_count: int, num_outputs: int
    ) -> None:
        """Load dual-model weights (v3.0 format) @zara"""
        self.training_samples = weights.get("training_samples", 0)
        self.current_accuracy = weights.get("accuracy")
        self.current_rmse = weights.get("rmse")
        if weights.get("last_trained"):
            try:
                self.last_training_time = datetime.fromisoformat(
                    weights["last_trained"]
                )
            except (ValueError, TypeError):
                pass
        ridge_loaded = False
        lstm_loaded = False
        ridge_weights = weights.get("ridge")
        if ridge_weights and self.ridge:
            try:
                saved_input_size = ridge_weights.get("input_size", 17)
                saved_output_count = ridge_weights.get("num_outputs", 1)
                saved_flat_size = ridge_weights.get("flat_size", saved_input_size * 24)
                required_flat_size = self.ridge.flat_size
                if (
                    saved_input_size == feature_count
                    and saved_output_count == num_outputs
                    and (saved_flat_size == required_flat_size)
                ):
                    self.ridge.set_weights(ridge_weights)
                    ridge_loaded = True
                    _LOGGER.info(
                        f"Ridge weights loaded: {ridge_weights.get('trained_samples', 0)} samples"
                    )
                else:
                    _LOGGER.warning(
                        f"Ridge architecture mismatch: saved input={saved_input_size}/flat_size={saved_flat_size}/outputs={saved_output_count}, needed input={feature_count}/flat_size={required_flat_size}/outputs={num_outputs}"
                    )
            except Exception as predictor_error:
                _LOGGER.warning(f"Could not load Ridge weights: {predictor_error}")
        lstm_weights = weights.get("lstm")
        if lstm_weights and self.lstm:
            try:
                saved_input_size = lstm_weights.get("input_size", 17)
                saved_output_count = lstm_weights.get("num_outputs", 1)
                saved_hidden_size = lstm_weights.get("hidden_size", 48)
                saved_second_hidden_size = lstm_weights.get("hidden2", 24)
                saved_head_count = lstm_weights.get("num_heads", 4)
                if (
                    saved_input_size == feature_count
                    and saved_output_count == num_outputs
                    and (saved_hidden_size == self.lstm.hidden1)
                    and (saved_second_hidden_size == self.lstm.hidden2)
                    and (saved_head_count == self.lstm.num_heads)
                ):
                    self.lstm.set_weights(lstm_weights)
                    lstm_loaded = True
                    self._lstm_ready = True
                    _LOGGER.info(
                        f"LSTM weights loaded: {lstm_weights.get('training_samples', 0)} samples"
                    )
                else:
                    _LOGGER.warning(
                        f"LSTM architecture mismatch: saved input={saved_input_size}/hidden=({saved_hidden_size},{saved_second_hidden_size})/heads={saved_head_count}/outputs={saved_output_count}, needed input={feature_count}/hidden=({self.lstm.hidden1},{self.lstm.hidden2})/heads={self.lstm.num_heads}/outputs={num_outputs}. Retraining required."
                    )
            except Exception as predictor_error:
                _LOGGER.warning(f"Could not load LSTM weights: {predictor_error}")
        saved_active_model = weights.get("active_model", "none")
        if saved_active_model == "tiny_lstm" and lstm_loaded:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif saved_active_model == "ridge" and ridge_loaded:
            self.active_model = "ridge"
            self.state = ModelState.READY
        elif lstm_loaded:
            self.active_model = "tiny_lstm"
            self.state = ModelState.READY
        elif ridge_loaded:
            self.active_model = "ridge"
            self.state = ModelState.READY
        else:
            self.active_model = "none"
            self.state = ModelState.UNTRAINED
            _LOGGER.warning(
                f"No compatible weights found. Config has {feature_count} features, {num_outputs} outputs. Model needs retraining."
            )
        active_model_text = f"active={self.active_model}"
        rmse_text = f", RMSE={self.current_rmse:.3f}kWh" if self.current_rmse else ""
        if self.state == ModelState.READY:
            _LOGGER.info(
                f"Dual-model weights loaded: {self.training_samples} samples, {active_model_text}, R²={self.current_accuracy:.3f}{rmse_text}"
                if self.current_accuracy
                else f"Dual-model weights loaded: {self.training_samples} samples, {active_model_text}"
            )
        return None if True else None

    async def _save_weights_async(self):
        """Save model weights asynchronously to database @zara"""
        try:
            weights_data = {
                "version": MODEL_FORMAT_VERSION,
                "active_model": self.active_model,
                "training_samples": self.training_samples,
                "last_trained": datetime.now().isoformat(),
                "accuracy": self.current_accuracy,
                "rmse": self.current_rmse,
                "ridge": (
                    self.ridge.get_weights()
                    if self.ridge and self.ridge.is_trained
                    else None
                ),
                "lstm": (
                    self.lstm.get_weights() if self.lstm and self._lstm_ready else None
                ),
            }
            await self.db_manager.save_model_weights(weights_data)
            _LOGGER.info("Weights saved to database")
        except Exception as predictor_error:
            _LOGGER.error(f"Failed to save weights: {predictor_error}")
        return None if True else None

    async def _archive_current_model(self, season: str) -> None:
        """Archive current model weights for seasonal reuse. @zara V16.8.4"""
        archive_year = datetime.now().year
        if self.lstm and self._lstm_ready:
            try:
                weights_data = self.lstm.get_weights()
                await self.db_manager.save_seasonal_archive(
                    season=season,
                    archive_type="lstm",
                    weights_data=weights_data,
                    training_samples=self.training_samples,
                    accuracy=self.current_accuracy or 0.0,
                    rmse=self.current_rmse or 0.0,
                    source_year=archive_year,
                )
            except Exception as predictor_error:
                _LOGGER.debug("LSTM seasonal archive failed: %s", predictor_error)
        if self.ridge and self.ridge.is_trained:
            try:
                weights_data = self.ridge.get_weights()
                await self.db_manager.save_seasonal_archive(
                    season=season,
                    archive_type="ridge",
                    weights_data=weights_data,
                    training_samples=self.training_samples,
                    accuracy=self.current_accuracy or 0.0,
                    rmse=self.current_rmse or 0.0,
                    source_year=archive_year,
                )
            except Exception as predictor_error:
                _LOGGER.debug("Ridge seasonal archive failed: %s", predictor_error)
        _LOGGER.info("Model archived for season %s/%d", season, archive_year)

    async def _load_seasonal_archive(self, season: str) -> Optional[Dict[str, Any]]:
        """Load archived model weights for seasonal warm-start. @zara V16.8.4"""
        compatible_archives = {}
        feature_count = calculate_feature_count(self.num_groups)
        output_count = self.num_groups if self.num_groups > 0 else 1
        for archive_type in ("lstm", "ridge"):
            try:
                archived_weights = await self.db_manager.load_seasonal_archive(
                    season, archive_type
                )
                if not archived_weights:
                    continue
                saved_input_size = archived_weights.get("input_size")
                saved_output_count = archived_weights.get("num_outputs")
                if (
                    saved_input_size != feature_count
                    or saved_output_count != output_count
                ):
                    _LOGGER.debug(
                        "Archive %s/%s architecture mismatch: input=%s/%s, outputs=%s/%s",
                        season,
                        archive_type,
                        saved_input_size,
                        feature_count,
                        saved_output_count,
                        output_count,
                    )
                    continue
                if archive_type == "lstm" and self.lstm:
                    saved_hidden_size = archived_weights.get("hidden_size", 48)
                    saved_second_hidden_size = archived_weights.get("hidden2", 24)
                    if (
                        saved_hidden_size != self.lstm.hidden1
                        or saved_second_hidden_size != self.lstm.hidden2
                    ):
                        _LOGGER.debug(
                            "Archive LSTM hidden mismatch: (%s,%s) vs (%s,%s)",
                            saved_hidden_size,
                            saved_second_hidden_size,
                            self.lstm.hidden1,
                            self.lstm.hidden2,
                        )
                        continue
                compatible_archives[archive_type] = archived_weights
            except Exception as predictor_error:
                _LOGGER.debug(
                    "Failed to load archive %s/%s: %s",
                    season,
                    archive_type,
                    predictor_error,
                )
                continue
            continue
        return compatible_archives if compatible_archives else None

    async def enable_attention(self, enable: bool = True) -> bool:
        """Attention is always active in V3.0 architecture @zara"""
        _LOGGER.info("Multi-head attention always enabled in V3.0")
        return True

    def get_model_info(self) -> Dict[str, Any]:
        """Get model information for diagnostics @zara"""
        model_info = {
            "state": self.state.value if self.state else "unknown",
            "training_samples": self.training_samples,
            "accuracy": self.current_accuracy,
            "rmse": self.current_rmse,
            "num_groups": self.num_groups,
            "total_capacity_kwp": self.total_capacity,
            "use_attention": self.use_attention,
            "last_training_time": (
                self.last_training_time.isoformat() if self.last_training_time else None
            ),
            "active_model": self.active_model,
        }
        if self.lstm:
            model_info["model_size_kb"] = self.lstm.get_model_size_kb()
            model_info["feature_count"] = self.lstm.input_size
            model_info["hidden_sizes"] = (self.lstm.hidden1, self.lstm.hidden2)
            model_info["num_layers"] = 2
            model_info["num_heads"] = self.lstm.num_heads
            model_info["num_outputs"] = self.lstm.num_outputs
        return model_info

    async def analyze_feature_importance(
        self, num_permutations: int = 5, progress_callback: Optional[callable] = None
    ) -> Optional[FeatureImportanceResult]:
        """Analyze which features help or hurt model performance @zara"""
        if not self.lstm or not self.feature_engineer:
            _LOGGER.warning("Cannot analyze features: model not initialized")
            return
        if not self._lstm_ready:
            _LOGGER.info(
                "Feature importance skipped: LSTM has no trained or compatible weights"
            )
            return
        if self.state != ModelState.READY:
            _LOGGER.warning(
                f"Cannot analyze features: model state is {self.state.value}"
            )
            return
        _LOGGER.debug("Starting Feature Importance analysis...")
        try:
            training_sequences, training_targets, unused_daily_totals, unused_daily_totals = await self._prepare_training_data()  # fmt: skip
            if len(training_sequences) < 20:
                _LOGGER.warning(
                    f"Not enough data for feature importance analysis: {len(training_sequences)} samples (need 20+)"
                )
                return
            import numpy as numpy_module

            sample_count = len(training_sequences)
            analysis_sample_count = max(20, int(sample_count * 0.2))
            sample_indices = numpy_module.random.permutation(sample_count)[
                :analysis_sample_count
            ]
            analysis_sequences = [
                training_sequences[sample_index] for sample_index in sample_indices
            ]
            analysis_targets = [
                training_targets[sample_index] for sample_index in sample_indices
            ]
            feature_names = list(self.feature_engineer.FEATURE_NAMES)
            analyzer = FeatureImportanceAnalyzer(
                self.db_manager, num_permutations=num_permutations
            )
            importance_result = await analyzer.analyze(
                lstm=self.lstm,
                X_sequences=analysis_sequences,
                y_targets=analysis_targets,
                feature_names=feature_names,
                progress_callback=progress_callback,
            )
            if importance_result.success:
                _LOGGER.debug(
                    f"Feature Importance complete: {len(importance_result.helpful_features)} helpful, {len(importance_result.harmful_features)} harmful, {len(importance_result.neutral_features)} neutral"
                )
                self._last_feature_importance = importance_result
            return importance_result
        except Exception as predictor_error:
            _LOGGER.error(f"Feature importance analysis failed: {predictor_error}")
            return
