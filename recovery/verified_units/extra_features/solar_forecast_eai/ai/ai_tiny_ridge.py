# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
TinyRidge: Lightweight Ridge Regression for Solar Forecast Energy AI V16.2.0.

Pure NumPy Ridge Regression for the bootstrap phase (10-50 samples) before LSTM.
Uses closed-form LOO-CV for automatic alpha selection.

@zara
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)
_np: Optional[Any] = None


def _ensure_numpy() -> Any:
    """Lazy import numpy for Home Assistant compatibility @zara"""
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


class TinyRidge:
    """Multi-Output Ridge Regression with LOO-CV alpha selection @zara

    Designed for bootstrap phase (10-50 samples) before LSTM becomes viable.
    Uses flattened 24-hour sequences as input for maximum temporal context.
    """

    MIN_SAMPLES = 10
    DEFAULT_ALPHAS = [0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0]

    def __init__(
        self,
        input_size: int = 28,
        hidden_size: int = 32,
        sequence_length: int = 24,
        num_outputs: int = 1,
        learning_rate: float = 0.005,
        dropout: float = 0.2,
        use_attention: bool = False,
        **kwargs,
    ):
        """Initialize TinyRidge @zara"""
        np = _ensure_numpy()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.sequence_length = sequence_length
        self.num_outputs = num_outputs
        self.flat_size = input_size * sequence_length
        self.weights = None
        self.alpha = None
        self.feature_means = None
        self.feature_stds = None
        self.trained_samples = 0
        self.loo_cv_score = 0.0
        self.training_accuracy = 0.0
        self.training_rmse = 0.0
        _LOGGER.info(
            f"TinyRidge initialized: input={input_size}, seq={sequence_length}, flat_size={self.flat_size}, outputs={num_outputs}"
        )

    def _flatten_sequence(self, X: Any) -> Any:
        """Flatten (seq_len, features) to (seq_len * features,) @zara"""
        np = _ensure_numpy()
        return np.asarray(X).flatten()

    def _normalize_features(self, X_flat: Any, fit: bool = False) -> Any:
        """Normalize features using z-score normalization @zara"""
        np = _ensure_numpy()
        if fit:
            self.feature_means = np.mean(X_flat, axis=0)
            self.feature_stds = np.std(X_flat, axis=0)
            self.feature_stds = np.where(
                self.feature_stds < 1e-08, 1.0, self.feature_stds
            )
        if self.feature_means is None or self.feature_stds is None:
            return X_flat
        return (X_flat - self.feature_means) / self.feature_stds

    def _add_bias(self, X: Any) -> Any:
        """Add bias column (ones) to feature matrix @zara"""
        np = _ensure_numpy()
        sample_count = X.shape[0]
        return np.hstack([X, np.ones((sample_count, 1))])

    def _ridge_closed_form(self, X: Any, y: Any, alpha: float) -> Any:
        """Solve Ridge regression using closed-form solution @zara"""
        np = _ensure_numpy()
        feature_count = X.shape[1]
        regularizer = alpha * np.eye(feature_count)
        regularizer[-1, -1] = alpha * 0.01
        gram_matrix = np.dot(X.T, X)
        regularized_gram = gram_matrix + regularizer
        try:
            inverse_gram = np.linalg.inv(regularized_gram)
        except np.linalg.LinAlgError:
            _LOGGER.warning("Ridge matrix singular, using pseudo-inverse")
            inverse_gram = np.linalg.pinv(regularized_gram)
        feature_targets = np.dot(X.T, y)
        weights = np.dot(inverse_gram, feature_targets)
        return weights

    def _loo_cv_score(self, X: Any, y: Any, alpha: float) -> float:
        """Calculate Leave-One-Out CV score using closed-form @zara"""
        np = _ensure_numpy()
        sample_count, feature_count = X.shape
        regularizer = alpha * np.eye(feature_count)
        regularizer[-1, -1] = alpha * 0.01
        gram_matrix = np.dot(X.T, X)
        regularized_gram = gram_matrix + regularizer
        try:
            inverse_gram = np.linalg.inv(regularized_gram)
        except np.linalg.LinAlgError:
            return float("inf")
        hat_diagonal = np.sum(np.dot(X, inverse_gram) * X, axis=1)
        hat_diagonal = np.clip(hat_diagonal, 0.0, 0.999999)
        weights = np.dot(inverse_gram, np.dot(X.T, y))
        predictions = np.dot(X, weights)
        residuals = y - predictions
        loo_residuals = residuals / (1 - hat_diagonal + 1e-10)
        return float(np.mean(loo_residuals**2))

    def _select_alpha(
        self, X: Any, y: Any, alphas: Optional[List[float]] = None
    ) -> Tuple[float, float]:
        """Select best alpha using LOO-CV @zara"""
        if alphas is None:
            alphas = self.DEFAULT_ALPHAS
        best_alpha = alphas[0]
        best_score = float("inf")
        for alpha in alphas:
            score = self._loo_cv_score(X, y, alpha)
            _LOGGER.debug(f"Alpha {alpha}: LOO-CV MSE = {score:.6f}")
            if score < best_score:
                best_score = score
                best_alpha = alpha
        _LOGGER.info(f"Selected alpha = {best_alpha} (LOO-CV MSE = {best_score:.6f})")
        return (best_alpha, best_score)

    async def train(
        self,
        X_sequences: List[Any],
        y_targets: List[Any],
        epochs: int = 1,
        batch_size: int = 16,
        validation_split: float = 0.2,
        early_stopping_patience: int = 10,
        checkpoint_callback: Any = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Train TinyRidge model @zara"""
        np = _ensure_numpy()
        sample_count = len(X_sequences)
        _LOGGER.info(f"TinyRidge training started: {sample_count} samples")
        if sample_count < self.MIN_SAMPLES:
            _LOGGER.warning(
                f"Insufficient samples for Ridge: {sample_count} < {self.MIN_SAMPLES}"
            )
            return {
                "success": False,
                "accuracy": 0.0,
                "rmse": 0.0,
                "message": f"Need at least {self.MIN_SAMPLES} samples, got {sample_count}",
                "epochs_trained": 0,
                "num_outputs": self.num_outputs,
                "training_samples": sample_count,
                "history": {"train_loss": [], "val_loss": []},
            }

        def _to_target_array(y: Any) -> Any:
            """Convert single target to array for multi-output @zara"""
            if isinstance(y, (int, float)):
                return np.array([float(y)] * self.num_outputs)
            if isinstance(y, (list, tuple)):
                target_array = np.array([float(target_value) for target_value in y])
                if len(target_array) < self.num_outputs:
                    target_array = np.concatenate(
                        [target_array, np.zeros(self.num_outputs - len(target_array))]
                    )
                return target_array[: self.num_outputs]
            return np.zeros(self.num_outputs)

        flat_sequences = np.array(
            [self._flatten_sequence(np.array(sequence)) for sequence in X_sequences]
        )
        targets = np.array([_to_target_array(target) for target in y_targets])
        normalized_sequences = self._normalize_features(flat_sequences, fit=True)
        biased_sequences = self._add_bias(normalized_sequences)
        alpha_results = [
            self._select_alpha(biased_sequences, targets[:, output_index])
            for output_index in range(self.num_outputs)
        ]
        selected_alphas = [
            selected_alpha for selected_alpha, unused_score in alpha_results
        ]
        selected_scores = [
            selected_score for unused_score, selected_score in alpha_results
        ]
        self.alpha = float(np.median(selected_alphas))
        self.loo_cv_score = float(np.mean(selected_scores))
        self.weights = np.zeros((self.num_outputs, biased_sequences.shape[1]))
        for weight_index in range(self.num_outputs):
            self.weights[weight_index] = self._ridge_closed_form(
                biased_sequences, targets[:, weight_index], self.alpha
            )
        self.trained_samples = sample_count
        validation_count = max(1, int(sample_count * validation_split))
        validation_slice = slice(sample_count - validation_count, sample_count)
        validation_sequences = biased_sequences[validation_slice]
        validation_targets = targets[validation_slice]
        predictions = np.dot(validation_sequences, self.weights.T)
        predictions = np.maximum(predictions, 0)
        predicted_totals = np.sum(predictions, axis=1)
        actual_totals = np.sum(validation_targets, axis=1)
        residual_sum_squares = np.sum((actual_totals - predicted_totals) ** 2)
        total_sum_squares = np.sum((actual_totals - np.mean(actual_totals)) ** 2)
        r_squared = (
            float(1 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > 1e-10
            else 0.0
        )
        rmse = float(np.sqrt(np.mean((actual_totals - predicted_totals) ** 2)))
        self.training_accuracy = r_squared
        self.training_rmse = rmse
        await asyncio.sleep(0)
        _LOGGER.info(
            f"TinyRidge trained: alpha={self.alpha:.4f}, R²={r_squared:.3f}, RMSE={rmse:.4f}, samples={sample_count}, outputs={self.num_outputs}"
        )
        return {
            "success": True,
            "accuracy": r_squared,
            "rmse": rmse,
            "alpha": self.alpha,
            "loo_cv_score": self.loo_cv_score,
            "epochs_trained": 1,
            "num_outputs": self.num_outputs,
            "training_samples": sample_count,
            "final_train_loss": self.loo_cv_score,
            "final_val_loss": rmse**2,
            "history": {
                "train_loss": [float(self.loo_cv_score)],
                "val_loss": [float(rmse**2)],
            },
        }

    def predict(self, X_sequence: Any) -> List[float]:
        """Predict production for single sequence @zara"""
        np = _ensure_numpy()
        if self.weights is None:
            raise ValueError("Model not trained - call train() first")
        sequence_array = np.array(X_sequence)
        if sequence_array.ndim == 3:
            if sequence_array.shape[0] != 1:
                raise ValueError(
                    f"Batch prediction not supported, expected batch_size=1, got shape {sequence_array.shape}"
                )
            sequence_array = sequence_array.squeeze(0)
        expected_shape = (self.sequence_length, self.input_size)
        if sequence_array.shape != expected_shape:
            raise ValueError(
                f"Expected input shape {expected_shape}, got {sequence_array.shape}"
            )
        flat_sequences = self._flatten_sequence(sequence_array).reshape(1, -1)
        normalized_sequences = self._normalize_features(flat_sequences, fit=False)
        biased_sequences = self._add_bias(normalized_sequences)
        if not np.isfinite(biased_sequences).all():
            _LOGGER.warning("Ridge predict: NaN/Inf in input, returning zeros")
            return [0.0] * self.num_outputs
        predictions = np.dot(biased_sequences, self.weights.T).flatten()
        predictions = np.maximum(predictions, 0)
        if not np.isfinite(predictions).all():
            _LOGGER.warning("Ridge predict: NaN/Inf in output, returning zeros")
            return [0.0] * self.num_outputs
        return [float(target) for target in predictions]

    def predict_total(self, X_sequence: Any) -> float:
        """Predict total production (sum of all outputs) @zara"""
        return sum(self.predict(X_sequence))

    def forward(
        self, X: Any, training: bool = False
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Forward pass for interface compatibility with TinyLSTM @zara"""
        np = _ensure_numpy()
        prediction_array = np.array(self.predict(X))
        return (prediction_array, None)

    def get_weights(self) -> Dict[str, Any]:
        """Export weights for persistence @zara"""
        _ensure_numpy()
        return {
            "model_type": "TinyRidge",
            "weights": self.weights.tolist() if self.weights is not None else None,
            "alpha": self.alpha,
            "feature_means": (
                self.feature_means.tolist() if self.feature_means is not None else None
            ),
            "feature_stds": (
                self.feature_stds.tolist() if self.feature_stds is not None else None
            ),
            "input_size": self.input_size,
            "hidden_size": self.hidden_size,
            "sequence_length": self.sequence_length,
            "num_outputs": self.num_outputs,
            "flat_size": self.flat_size,
            "trained_samples": self.trained_samples,
            "loo_cv_score": self.loo_cv_score,
            "accuracy": self.training_accuracy,
            "rmse": self.training_rmse,
        }

    def set_weights(self, weights: Dict[str, Any]) -> None:
        """Load weights from persistence @zara"""
        np = _ensure_numpy()
        self.weights = np.array(weights["weights"]) if weights.get("weights") else None
        self.alpha = weights.get("alpha")
        self.feature_means = (
            np.array(weights["feature_means"]) if weights.get("feature_means") else None
        )
        self.feature_stds = (
            np.array(weights["feature_stds"]) if weights.get("feature_stds") else None
        )
        self.input_size = weights.get("input_size", self.input_size)
        self.hidden_size = weights.get("hidden_size", self.hidden_size)
        self.sequence_length = weights.get("sequence_length", self.sequence_length)
        self.num_outputs = weights.get("num_outputs", self.num_outputs)
        self.flat_size = weights.get(
            "flat_size", self.input_size * self.sequence_length
        )
        self.trained_samples = weights.get("trained_samples", 0)
        self.loo_cv_score = weights.get("loo_cv_score", 0.0)
        self.training_accuracy = weights.get("accuracy", 0.0)
        self.training_rmse = weights.get("rmse", 0.0)
        _LOGGER.info(
            f"TinyRidge weights loaded: outputs={self.num_outputs}, samples={self.trained_samples}, alpha={self.alpha}, accuracy={self.training_accuracy:.3f}"
        )

    def get_model_size_kb(self) -> float:
        """Calculate model size in KB @zara"""
        if self.weights is None:
            return 0.0
        parameter_count = self.weights.size
        if self.feature_means is not None:
            parameter_count += self.feature_means.size
        if self.feature_stds is not None:
            parameter_count += self.feature_stds.size
        return parameter_count * 8 / 1024

    @property
    def is_trained(self) -> bool:
        """Check if model has been trained @zara"""
        return self.weights is not None and self.trained_samples >= self.MIN_SAMPLES
