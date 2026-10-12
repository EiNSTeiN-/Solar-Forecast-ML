# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
TinyWeatherMLP: Lightweight MLP for solar radiation correction.

Pure NumPy 2-layer feedforward network that learns non-linear GHI correction
factors from multi-source weather data.

@zara
"""
# fmt: on
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)
_np: Optional[Any] = None


def _ensure_numpy() -> Any:
    """Lazy import numpy. @zara"""
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


class TinyWeatherMLP:
    # fmt: off
    """2-Layer MLP for solar radiation correction. @zara

    Input: 8 features (GHI, clouds, elevation, humidity, pressure, wind, hour_sin, hour_cos)
    Hidden: 16 -> 8 (ReLU)
    Output: 1 correction factor (Sigmoid * 1.5 + 0.25 -> [0.25, 1.75])
    """
    # fmt: on
    MIN_SAMPLES = 50
    MIN_R2_THRESHOLD = 0.5

    def __init__(
        self,
        input_size: int = 8,
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
        input_scale = np.sqrt(2.0 / input_size)
        self.W1 = np.random.randn(hidden1, input_size) * input_scale
        self.b1 = np.zeros((hidden1, 1))
        hidden1_scale = np.sqrt(2.0 / hidden1)
        self.W2 = np.random.randn(hidden2, hidden1) * hidden1_scale
        self.b2 = np.zeros((hidden2, 1))
        hidden2_scale = np.sqrt(1.0 / hidden2)
        self.W3 = np.random.randn(1, hidden2) * hidden2_scale
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
        _LOGGER.info(
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
        correction_factor = sigmoid * 1.5 + 0.25
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
        factor_gradient = loss_gradient * 1.5
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
        validation_count = max(1, int(sample_count * validation_split))
        training_count = sample_count - validation_count
        sample_indices = list(range(sample_count))
        np.random.shuffle(sample_indices)
        training_indices = sample_indices[:training_count]
        validation_indices = sample_indices[training_count:]
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
                prediction, unused_cache = self.forward(X_data[sample_index])
                validation_loss += (prediction - y_targets[sample_index]) ** 2
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
        targets = []
        for sample_index in validation_indices:
            prediction, unused_cache = self.forward(X_data[sample_index])
            predictions.append(prediction)
            targets.append(y_targets[sample_index])
        predictions = np.array(predictions)
        targets = np.array(targets)
        residual_sum_squares = np.sum((targets - predictions) ** 2)
        total_sum_squares = np.sum((targets - np.mean(targets)) ** 2)
        r_squared = (
            float(1.0 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > 1e-10
            else 0.0
        )
        rmse = float(np.sqrt(np.mean((targets - predictions) ** 2)))
        self.trained_samples = sample_count
        self.training_accuracy = r_squared
        self.training_rmse = rmse
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
        """Predict correction factor. @zara"""
        prediction, unused_cache = self.forward(x)
        return max(0.25, min(1.75, prediction))

    def predict_correction(
        self,
        raw_ghi: float,
        cloud_cover: float,
        sun_elevation: float,
        humidity: Optional[float],
        pressure: Optional[float],
        wind: Optional[float],
        hour: int,
    ) -> float:
        """Build feature vector and predict correction factor. @zara"""
        features = [
            raw_ghi / 1200.0,
            (cloud_cover or 50.0) / 100.0,
            max(0.0, min(1.0, (sun_elevation or 0.0) / 90.0)),
            (humidity or 70.0) / 100.0,
            ((pressure or 1013.0) - 950.0) / 100.0,
            (wind or 3.0) / 30.0,
            math.sin(2.0 * math.pi * hour / 24.0),
            math.cos(2.0 * math.pi * hour / 24.0),
        ]
        return self.predict(features)

    @property
    def is_trained(self) -> bool:
        """Check if model has been trained with sufficient quality. @zara"""
        if self.trained_samples < self.MIN_SAMPLES:
            return False
        if self.training_accuracy < self.MIN_R2_THRESHOLD:
            _LOGGER.debug(
                "Weather MLP quality insufficient: R²=%.3f < %.3f, using static factors",
                self.training_accuracy,
                self.MIN_R2_THRESHOLD,
            )
            return False
        return True

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
        """Load weights from persistence. @zara"""
        np = _ensure_numpy()
        for loaded_parameter_name in ("W1", "b1", "W2", "b2", "W3", "b3"):
            if loaded_parameter_name in weights:
                values = np.array(weights[loaded_parameter_name], dtype=np.float64)
                if loaded_parameter_name.startswith("b") and values.ndim == 1:
                    values = values.reshape(-1, 1)
                setattr(self, loaded_parameter_name, values)
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
        if self.trained_samples > 0:
            _LOGGER.info(
                "Weather MLP weights loaded: samples=%d, R²=%.3f",
                self.trained_samples,
                self.training_accuracy,
            )
        # Preserve the verified shared-return edge.
        return None if True else None
