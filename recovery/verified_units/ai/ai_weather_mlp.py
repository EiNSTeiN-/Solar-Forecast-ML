# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
TinyWeatherMLP: Lightweight MLP for solar radiation correction.

Pure NumPy 2-layer feedforward network that learns non-linear GHI correction
factors from multi-source weather data.

@zara
"""
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

_LOGGER = logging.getLogger(__name__)
_np: Optional[Any] = None


def _ensure_numpy() -> Any:
    """Lazy import numpy. @zara"""
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


WEATHER_MLP_FEATURE_COUNT = 12
WEATHER_MLP_OUTPUT_MIN = 0.5
WEATHER_MLP_OUTPUT_MAX = 1.5
WEATHER_MLP_TARGET_REJECT_MAX = 3.0
WEATHER_MLP_NEUTRAL_RESIDUAL = 1.0
WEATHER_MLP_EVALUATION_FOLDS = 5
WEATHER_MLP_MIN_POOLED_R2 = 0.2
WEATHER_MLP_MIN_SKILL_VS_STATIC = 0.05
WEATHER_MLP_MIN_TRAINING_SAMPLES = 50


def chronological_train_val_indices(
    n_samples: int, validation_split: float
) -> Tuple[List[int], List[int]]:
    """Split ordered samples so validation is always the most recent tail."""
    if n_samples < 2:
        raise ValueError("chronological split requires at least two samples")
    validation_count = max(1, int(n_samples * validation_split))
    training_count = n_samples - validation_count
    if training_count < 1:
        training_count = n_samples - 1
        validation_count = 1
    return (list(range(training_count)), list(range(training_count, n_samples)))


def build_weather_mlp_features(
    *,
    raw_ghi: Optional[float],
    cloud_cover: Optional[float],
    sun_elevation: Optional[float],
    humidity: Optional[float],
    pressure: Optional[float],
    wind: Optional[float],
    hour: int,
    rain: Optional[float] = None,
    cloud_low: Optional[float] = None,
    cloud_mid: Optional[float] = None,
    cloud_high: Optional[float] = None,
) -> Optional[List[float]]:
    """Build the inference/training vector or return None when required inputs are missing."""
    required_inputs = (raw_ghi, cloud_cover, sun_elevation, humidity, pressure, wind)
    if any((value is None for value in required_inputs)):
        return
    if isinstance(hour, bool) or hour is None:
        return
    try:
        hour_number = int(hour)
    except (TypeError, ValueError):
        return
    if abs(float(hour) - hour_number) > 1e-09 or not 0 <= hour_number <= 23:
        return None if True else None
    hour = hour_number
    ghi = float(raw_ghi)
    clouds = float(cloud_cover)
    elevation = float(sun_elevation)
    return (
        None
        if ghi <= 0 or clouds < 0 or (not math.isfinite(ghi + clouds + elevation))
        else [
            ghi / 1200.0,
            min(1.0, max(0.0, clouds / 100.0)),
            max(0.0, min(1.0, elevation / 90.0)),
            min(1.0, max(0.0, float(humidity) / 100.0)),
            (float(pressure) - 950.0) / 100.0,
            min(1.0, max(0.0, float(wind) / 30.0)),
            math.sin(2.0 * math.pi * hour / 24.0),
            math.cos(2.0 * math.pi * hour / 24.0),
            min(1.0, max(0.0, float(rain or 0.0) / 10.0)),
            min(1.0, max(0.0, float(cloud_low or 0.0) / 100.0)),
            min(1.0, max(0.0, float(cloud_mid or 0.0) / 100.0)),
            min(1.0, max(0.0, float(cloud_high or 0.0) / 100.0)),
        ]
    )


def clamp_weather_mlp_target(value: Optional[float]) -> Optional[float]:
    """Clamp representable targets; reject non-finite, non-positive, or garbage values."""
    if value is None:
        return
    clamped_target = float(value)
    if (
        not math.isfinite(clamped_target)
        or clamped_target <= 0
        or clamped_target > WEATHER_MLP_TARGET_REJECT_MAX
    ):
        return None
    return max(WEATHER_MLP_OUTPUT_MIN, min(WEATHER_MLP_OUTPUT_MAX, clamped_target))


def residual_weather_mlp_target(
    solar_factor: Optional[float], static_factor_applied: Optional[float]
) -> Optional[float]:
    """Target relative to the static correction that was applied for that hour.

    ``solar_factor`` is measured / provider GHI; dividing by the applied static
    factor leaves what the weather-dependent model still has to explain. A
    missing or non-positive static factor means none was applied (1.0).
    """
    if solar_factor is None:
        return
    try:
        static_factor = (
            float(static_factor_applied) if static_factor_applied is not None else 1.0
        )
    except (TypeError, ValueError):
        static_factor = 1.0
    if not math.isfinite(static_factor) or static_factor <= 0:
        static_factor = 1.0
    try:
        clamped_target = float(solar_factor)
    except (TypeError, ValueError):
        return
    return clamp_weather_mlp_target(clamped_target / static_factor)


def day_blocked_folds(
    dates: Sequence[str], n_folds: int = WEATHER_MLP_EVALUATION_FOLDS
) -> List[Tuple[List[int], List[int]]]:
    """Split sample indices into consecutive whole-day blocks.

    Every fold holds out days the model never saw, which is what the nightly
    verdict has to describe. Returns [] when fewer than two days exist.
    """
    unique_dates = sorted(set(dates))
    if len(unique_dates) < 2:
        return []
    fold_count = max(2, min(int(n_folds), len(unique_dates)))
    fold_by_date = {}
    for date_index, date_or_fold in enumerate(unique_dates):
        fold_by_date[date_or_fold] = date_index * fold_count // len(unique_dates)
    folds = []
    for fold_index in range(fold_count):
        validation_indices = [
            sample_index
            for sample_index, date_or_fold in enumerate(dates)
            if fold_by_date[date_or_fold] == fold_index
        ]
        training_indices = [
            sample_index
            for sample_index, date_or_fold in enumerate(dates)
            if fold_by_date[date_or_fold] != fold_index
        ]
        if validation_indices and training_indices:
            folds.append((training_indices, validation_indices))
    return folds


def held_out_training_shortfall(
    dates: Sequence[str],
    min_samples: int = WEATHER_MLP_MIN_TRAINING_SAMPLES,
    n_folds: int = WEATHER_MLP_EVALUATION_FOLDS,
) -> Optional[int]:
    """Training size of the leanest day-blocked fold when a fit would refuse it.

    None when every fold trains on at least min_samples, or when there are
    no folds at all (fewer than two days is reported separately). A fold that
    cannot train is a warm-up condition of the window, not a training failure.
    """
    fold_count = day_blocked_folds(dates, n_folds)
    if not fold_count:
        return
    smallest_training_fold = min(
        (
            len(training_indices)
            for training_indices, unused_validation_indices in fold_count
        )
    )
    return smallest_training_fold if smallest_training_fold < min_samples else None


def pooled_evaluation_metrics(
    targets: Sequence[float], predictions: Sequence[float]
) -> Dict[str, float]:
    """R², RMSE and skill against the static-only baseline (residual 1.0)."""
    sample_count = len(targets)
    if sample_count == 0 or sample_count != len(predictions):
        return {
            "samples": 0,
            "r2": 0.0,
            "rmse": 0.0,
            "baseline_rmse": 0.0,
            "skill": 0.0,
        }
    target_mean = sum(targets) / sample_count
    residual_sum_squares = sum(
        (
            (target_value - predicted_value) ** 2
            for target_value, predicted_value in zip(targets, predictions)
        )
    )
    total_sum_squares = sum(
        ((target_value - target_mean) ** 2 for target_value in targets)
    )
    static_sum_squares = sum(
        ((target_value - WEATHER_MLP_NEUTRAL_RESIDUAL) ** 2 for target_value in targets)
    )
    r_squared = (
        float(1.0 - residual_sum_squares / total_sum_squares)
        if total_sum_squares > 1e-10
        else 0.0
    )
    skill_vs_static = (
        float(1.0 - residual_sum_squares / static_sum_squares)
        if static_sum_squares > 1e-10
        else 0.0
    )
    return {
        "samples": sample_count,
        "r2": r_squared,
        "rmse": math.sqrt(residual_sum_squares / sample_count),
        "baseline_rmse": math.sqrt(static_sum_squares / sample_count),
        "skill": skill_vs_static,
    }


def weather_mlp_persist_verdict(metrics: Dict[str, float]) -> Optional[str]:
    """None when the pooled metrics clear the persist gate, else the reason."""
    r_squared = float(metrics.get("r2", 0.0))
    skill_vs_static = float(metrics.get("skill", 0.0))
    if r_squared < WEATHER_MLP_MIN_POOLED_R2:
        return f"pooled R²={r_squared:.3f} below {WEATHER_MLP_MIN_POOLED_R2:.2f} (skill vs static={skill_vs_static:+.3f})"
    if skill_vs_static < WEATHER_MLP_MIN_SKILL_VS_STATIC:
        return f"skill vs static={skill_vs_static:+.3f} below {WEATHER_MLP_MIN_SKILL_VS_STATIC:.2f} (pooled R²={r_squared:.3f})"


class TinyWeatherMLP:
    """2-Layer MLP for solar radiation correction. @zara

    Input: 12 features (GHI, blended clouds, elevation, humidity, pressure, wind,
    hour_sin, hour_cos, rain, low/mid/high cloud layers)
    Hidden: 16 -> 8 (ReLU)
    Output: 1 residual factor in [0.50, 1.50], applied on top of the static
    solar calibration (1.0 = static correction already right)
    """

    MIN_SAMPLES = WEATHER_MLP_MIN_TRAINING_SAMPLES
    MIN_R2_THRESHOLD = 0.5
    MIN_USABLE_R2_THRESHOLD = WEATHER_MLP_MIN_POOLED_R2
    FULL_CONFIDENCE_R2_THRESHOLD = 0.65
    MAX_USABLE_CONFIDENCE = 0.65
    MAX_TRAINED_CONFIDENCE = 0.8
    CONFIDENCE_RMSE_LIMIT = 0.35

    def __init__(
        self,
        input_size: int = WEATHER_MLP_FEATURE_COUNT,
        hidden1: int = 16,
        hidden2: int = 8,
        learning_rate: float = 0.001,
    ):
        """Initialize TinyWeatherMLP. @zara"""
        np = _ensure_numpy()
        self.input_size = input_size
        self.hidden1 = hidden1
        self.hidden2 = hidden2
        self.learning_rate = learning_rate
        weight1_scale = np.sqrt(2.0 / input_size)
        self.W1 = np.random.randn(hidden1, input_size) * weight1_scale
        self.b1 = np.zeros((hidden1, 1))
        weight2_scale = np.sqrt(2.0 / hidden1)
        self.W2 = np.random.randn(hidden2, hidden1) * weight2_scale
        self.b2 = np.zeros((hidden2, 1))
        weight3_scale = np.sqrt(1.0 / hidden2)
        self.W3 = np.random.randn(1, hidden2) * weight3_scale
        self.b3 = np.zeros((1, 1))
        self._adam_state = {}
        self._adam_t = 0
        for parameter_name in ("W1", "b1", "W2", "b2", "W3", "b3"):
            parameter = getattr(self, parameter_name)
            self._adam_state[parameter_name] = {
                "m": np.zeros_like(parameter),
                "v": np.zeros_like(parameter),
            }
        self.trained_samples = 0
        self.training_accuracy = 0.0
        self.training_rmse = 0.0
        self._quality_fallback_logged = False
        _LOGGER.debug(
            "TinyWeatherMLP initialized: %d -> %d -> %d -> 1 (%d params)",
            input_size,
            hidden1,
            hidden2,
            input_size * hidden1 + hidden1 + hidden1 * hidden2 + hidden2 + hidden2 + 1,
        )

    def _sigmoid(self, x: Any) -> Any:
        """Numerically stable sigmoid. @zara"""
        np = _ensure_numpy()
        return np.where(x >= 0, 1.0 / (1.0 + np.exp(-x)), np.exp(x) / (1.0 + np.exp(x)))

    def forward(self, x: Any) -> Tuple[float, Dict[str, Any]]:
        """Forward pass. @zara"""
        np = _ensure_numpy()
        x = np.asarray(x, dtype=np.float64).reshape(-1, 1)
        hidden1_linear = self.W1 @ x + self.b1
        hidden1_activation = np.maximum(0, hidden1_linear)
        hidden2_linear = self.W2 @ hidden1_activation + self.b2
        hidden2_activation = np.maximum(0, hidden2_linear)
        output_linear = self.W3 @ hidden2_activation + self.b3
        sigmoid = self._sigmoid(output_linear)
        correction_factor = (
            sigmoid * (WEATHER_MLP_OUTPUT_MAX - WEATHER_MLP_OUTPUT_MIN)
            + WEATHER_MLP_OUTPUT_MIN
        )
        cache = {
            "x": x,
            "z1": hidden1_linear,
            "a1": hidden1_activation,
            "z2": hidden2_linear,
            "a2": hidden2_activation,
            "z3": output_linear,
            "sig": sigmoid,
        }
        return (float(correction_factor[0, 0]), cache)

    def backward(
        self, cache: Dict[str, Any], target: float, prediction: float
    ) -> Dict[str, Any]:
        """Backward pass. @zara"""
        np = _ensure_numpy()
        loss_gradient = 2.0 * (prediction - target)
        factor_gradient = loss_gradient * (
            WEATHER_MLP_OUTPUT_MAX - WEATHER_MLP_OUTPUT_MIN
        )
        sigmoid = cache["sig"]
        output_gradient = factor_gradient * sigmoid * (1.0 - sigmoid)
        weight3_gradient = output_gradient @ cache["a2"].T
        bias3_gradient = output_gradient
        hidden2_gradient = self.W3.T @ output_gradient
        hidden2_linear_gradient = hidden2_gradient * (cache["z2"] > 0).astype(
            np.float64
        )
        weight2_gradient = hidden2_linear_gradient @ cache["a1"].T
        bias2_gradient = hidden2_linear_gradient
        hidden1_gradient = self.W2.T @ hidden2_linear_gradient
        hidden1_linear_gradient = hidden1_gradient * (cache["z1"] > 0).astype(
            np.float64
        )
        weight1_gradient = hidden1_linear_gradient @ cache["x"].T
        bias1_gradient = hidden1_linear_gradient
        return {
            "W1": weight1_gradient,
            "b1": bias1_gradient,
            "W2": weight2_gradient,
            "b2": bias2_gradient,
            "W3": weight3_gradient,
            "b3": bias3_gradient,
        }

    def _adam_update(self, grads: Dict[str, Any]) -> None:
        """Adam optimizer step. @zara"""
        np = _ensure_numpy()
        self._adam_t += 1
        beta1, beta2, epsilon = (0.9, 0.999, 1e-08)
        for parameter_name in ("W1", "b1", "W2", "b2", "W3", "b3"):
            gradient = grads[parameter_name]
            optimizer_state = self._adam_state[parameter_name]
            optimizer_state["m"] = beta1 * optimizer_state["m"] + (1 - beta1) * gradient
            optimizer_state["v"] = (
                beta2 * optimizer_state["v"] + (1 - beta2) * gradient**2
            )
            corrected_first_moment = optimizer_state["m"] / (1 - beta1**self._adam_t)
            corrected_second_moment = optimizer_state["v"] / (1 - beta2**self._adam_t)
            parameter = getattr(self, parameter_name)
            parameter -= (
                self.learning_rate
                * corrected_first_moment
                / (np.sqrt(corrected_second_moment) + epsilon)
            )
            setattr(self, parameter_name, parameter)
            continue

    def train_step(self, x: Any, target: float) -> float:
        """Single training step. @zara"""
        prediction, cache = self.forward(x)
        gradients = self.backward(cache, target, prediction)
        self._adam_update(gradients)
        return (prediction - target) ** 2

    async def train(
        self,
        X_data: List[Any],
        y_targets: List[float],
        epochs: int = 50,
        validation_split: float = 0.2,
        early_stopping_patience: int = 10,
    ) -> Dict[str, Any]:
        """Train MLP on correction factor data. Runs out-of-process to protect HA event loop. @zara"""
        try:
            from ..core.core_train_worker import (
                async_run_training_in_subprocess as run_training,
            )

            folds = await run_training(
                model_type="weather_mlp",
                model_config={
                    "input_size": self.input_size,
                    "hidden1": self.hidden1,
                    "hidden2": self.hidden2,
                    "learning_rate": self.learning_rate,
                },
                current_weights=self.get_weights(),
                X_data=X_data,
                y_targets=y_targets,
                training_params={
                    "epochs": epochs,
                    "validation_split": validation_split,
                    "early_stopping_patience": early_stopping_patience,
                },
            )
            if folds.get("success"):
                self.set_weights(folds["weights"])
                return {
                    "success": True,
                    "accuracy": folds.get("accuracy", 0.0),
                    "rmse": folds.get("rmse", 0.0),
                    "epochs_trained": folds.get("epochs_trained", 0),
                    "training_samples": folds.get("training_samples", 0),
                    "val_loss": folds.get("val_loss", 0.0),
                }
            _LOGGER.warning(
                "Subprocess Weather MLP training failed, falling back to local training: %s",
                folds.get("error_message"),
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to run Weather MLP training in subprocess, falling back to local training: %s",
                error,
            )
        return await self._train_in_process(
            X_data=X_data,
            y_targets=y_targets,
            epochs=epochs,
            validation_split=validation_split,
            early_stopping_patience=early_stopping_patience,
        )

    async def _train_in_process(
        self,
        X_data: List[Any],
        y_targets: List[float],
        epochs: int = 50,
        validation_split: float = 0.2,
        early_stopping_patience: int = 10,
    ) -> Dict[str, Any]:
        """Train MLP on correction factor data. @zara"""
        np = _ensure_numpy()
        sample_count = len(X_data)
        if sample_count < self.MIN_SAMPLES:
            _LOGGER.warning(
                "Weather MLP: insufficient samples %d < %d",
                sample_count,
                self.MIN_SAMPLES,
            )
            return {
                "success": False,
                "accuracy": 0.0,
                "rmse": 0.0,
                "epochs_trained": 0,
                "training_samples": sample_count,
            }
        training_indices, validation_indices = chronological_train_val_indices(
            sample_count, validation_split
        )
        training_count = len(training_indices)
        validation_count = len(validation_indices)
        best_validation_loss = float("inf")
        epochs_without_improvement = 0
        best_weights = None
        _LOGGER.info(
            "Weather MLP training: %d samples (%d train, %d val), %d epochs",
            sample_count,
            training_count,
            validation_count,
            epochs,
        )
        for epoch in range(epochs):
            np.random.shuffle(training_indices)
            training_loss = 0.0
            for sample_index in training_indices:
                sample_loss = self.train_step(
                    X_data[sample_index], y_targets[sample_index]
                )
                training_loss += sample_loss
            training_loss /= training_count
            validation_loss = 0.0
            for sample_index in validation_indices:
                validation_prediction, predicted_factor = self.forward(
                    X_data[sample_index]
                )
                validation_loss += (
                    validation_prediction - y_targets[sample_index]
                ) ** 2
            validation_loss /= validation_count
            if validation_loss < best_validation_loss:
                best_validation_loss = validation_loss
                epochs_without_improvement = 0
                best_weights = self.get_weights()
            else:
                epochs_without_improvement += 1
            if epochs_without_improvement >= early_stopping_patience:
                _LOGGER.info("Weather MLP early stopping at epoch %d", epoch + 1)
                break
            if (epoch + 1) % 10 == 0:
                _LOGGER.debug(
                    "Weather MLP epoch %d: train_loss=%.6f, val_loss=%.6f",
                    epoch + 1,
                    training_loss,
                    validation_loss,
                )
            await asyncio.sleep(0)
            continue
        if best_weights:
            self.set_weights(best_weights)
        predictions = []
        actual_values = []
        for sample_index in validation_indices:
            validation_prediction, predicted_factor = self.forward(X_data[sample_index])
            predictions.append(validation_prediction)
            actual_values.append(y_targets[sample_index])
        predictions = np.array(predictions)
        actual_values = np.array(actual_values)
        residual_sum_squares = np.sum((actual_values - predictions) ** 2)
        total_sum_squares = np.sum((actual_values - np.mean(actual_values)) ** 2)
        r_squared = (
            float(1.0 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > 1e-10
            else 0.0
        )
        rmse = float(np.sqrt(np.mean((actual_values - predictions) ** 2)))
        self.trained_samples = sample_count
        self.training_accuracy = r_squared
        self.training_rmse = rmse
        self._quality_fallback_logged = False
        _LOGGER.info(
            "Weather MLP trained: R²=%.3f, RMSE=%.4f, samples=%d, epochs=%d",
            r_squared,
            rmse,
            sample_count,
            epoch + 1,
        )
        return {
            "success": True,
            "accuracy": r_squared,
            "rmse": rmse,
            "epochs_trained": epoch + 1,
            "training_samples": sample_count,
            "val_loss": float(best_validation_loss),
        }

    def predict(self, x: Any) -> float:
        """Predict the residual factor relative to the static calibration. @zara"""
        clamped_target, predicted_factor = self.forward(x)
        return max(WEATHER_MLP_OUTPUT_MIN, min(WEATHER_MLP_OUTPUT_MAX, clamped_target))

    def predict_correction(
        self,
        raw_ghi: float,
        cloud_cover: float,
        sun_elevation: float,
        humidity: Optional[float],
        pressure: Optional[float],
        wind: Optional[float],
        hour: int,
        rain: Optional[float] = None,
        cloud_low: Optional[float] = None,
        cloud_mid: Optional[float] = None,
        cloud_high: Optional[float] = None,
    ) -> Optional[float]:
        """Build feature vector and predict the residual factor (1.0 = static is right). @zara"""
        features = build_weather_mlp_features(
            raw_ghi=raw_ghi,
            cloud_cover=cloud_cover,
            sun_elevation=sun_elevation,
            humidity=humidity,
            pressure=pressure,
            wind=wind,
            hour=hour,
            rain=rain,
            cloud_low=cloud_low,
            cloud_mid=cloud_mid,
            cloud_high=cloud_high,
        )
        if features is None:
            return
        return self.predict(features)

    @property
    def is_trained(self) -> bool:
        """Check if model has been trained with sufficient quality. @zara"""
        if self.trained_samples < self.MIN_SAMPLES:
            return False
        if self.training_accuracy < self.MIN_R2_THRESHOLD:
            if not self._quality_fallback_logged:
                _LOGGER.debug(
                    "Weather MLP static fallback active: R²=%.3f < %.3f; repeated per-hour fallback logs suppressed",
                    self.training_accuracy,
                    self.MIN_R2_THRESHOLD,
                )
                self._quality_fallback_logged = True
            return False
        return True

    @property
    def is_usable(self) -> bool:
        """Check if model quality is sufficient for bounded blending. @zara"""
        if self.trained_samples < self.MIN_SAMPLES:
            return False
        return self.training_accuracy >= self.MIN_USABLE_R2_THRESHOLD

    @property
    def application_confidence(self) -> float:
        """Return bounded confidence for blending MLP with static corrections. @zara"""
        if not self.is_usable:
            return 0.0
        r_squared_span = max(
            0.01, self.FULL_CONFIDENCE_R2_THRESHOLD - self.MIN_USABLE_R2_THRESHOLD
        )
        accuracy_confidence = (
            self.training_accuracy - self.MIN_USABLE_R2_THRESHOLD
        ) / r_squared_span
        accuracy_confidence = max(0.0, min(1.0, accuracy_confidence))
        sample_confidence = max(0.0, min(1.0, self.trained_samples / 300.0))
        if self.training_rmse > 0:
            rmse_confidence = 1.0 - self.training_rmse / self.CONFIDENCE_RMSE_LIMIT
            rmse_confidence = max(0.0, min(1.0, rmse_confidence))
        else:
            rmse_confidence = 0.0
        confidence = 0.15 + 0.85 * (
            0.65 * accuracy_confidence
            + 0.2 * sample_confidence
            + 0.15 * rmse_confidence
        )
        confidence_cap = (
            self.MAX_TRAINED_CONFIDENCE
            if self.is_trained
            else self.MAX_USABLE_CONFIDENCE
        )
        return max(0.0, min(confidence_cap, confidence))

    def get_weights(self) -> Dict[str, Any]:
        """Export weights for persistence. @zara"""
        return {
            "input_size": self.input_size,
            "hidden1": self.hidden1,
            "hidden2": self.hidden2,
            "training_samples": self.trained_samples,
            "accuracy": self.training_accuracy,
            "rmse": self.training_rmse,
            "W1": self.W1.tolist(),
            "b1": self.b1.tolist(),
            "W2": self.W2.tolist(),
            "b2": self.b2.tolist(),
            "W3": self.W3.tolist(),
            "b3": self.b3.tolist(),
        }

    def set_weights(self, weights: Dict[str, Any]) -> None:
        """Load weights from persistence. Reject incompatible shapes. @zara"""
        np = _ensure_numpy()
        artifact_input_size = weights.get("input_size")
        if (
            artifact_input_size is not None
            and int(artifact_input_size) != self.input_size
        ):
            raise ValueError(
                f"Weather MLP input_size mismatch: stored={artifact_input_size}, expected={self.input_size}"
            )
        expected_shapes = {
            "W1": (self.hidden1, self.input_size),
            "b1": (self.hidden1, 1),
            "W2": (self.hidden2, self.hidden1),
            "b2": (self.hidden2, 1),
            "W3": (1, self.hidden2),
            "b3": (1, 1),
        }
        for tensor_name, expected_shape in expected_shapes.items():
            if tensor_name not in weights:
                raise ValueError(f"Weather MLP weights missing {tensor_name}")
            restored_tensor = np.array(weights[tensor_name], dtype=np.float64)
            if tensor_name.startswith("b") and restored_tensor.ndim == 1:
                restored_tensor = restored_tensor.reshape(-1, 1)
            if tuple(restored_tensor.shape) != expected_shape:
                raise ValueError(
                    f"Weather MLP {tensor_name} shape {restored_tensor.shape} does not match {expected_shape}"
                )
            setattr(self, tensor_name, restored_tensor)
        self._adam_t = 0
        for parameter_name in ("W1", "b1", "W2", "b2", "W3", "b3"):
            parameter = getattr(self, parameter_name)
            self._adam_state[parameter_name] = {
                "m": np.zeros_like(parameter),
                "v": np.zeros_like(parameter),
            }
        self.trained_samples = weights.get("training_samples", 0)
        self.training_accuracy = weights.get("accuracy", 0.0)
        self.training_rmse = weights.get("rmse", 0.0)
        self._quality_fallback_logged = False
        if self.trained_samples > 0:
            _LOGGER.info(
                "Weather MLP weights loaded: samples=%d, R²=%.3f",
                self.trained_samples,
                self.training_accuracy,
            )
        return None
