# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
TinyRidge: Lightweight Ridge Regression for Solar Forecast ML V16.2.0.

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
        _LOGGER.debug(
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

    async def _select_alpha(
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
            await asyncio.sleep(0)
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
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
        **kwargs,
    ) -> Dict[str, Any]:
        """Train TinyRidge model. Runs out-of-process to protect HA event loop. @zara"""
        if (
            any(
                (
                    sample_metadata is not None
                    for sample_metadata in (
                        sample_groups,
                        sample_dates,
                        source_intervals,
                    )
                )
            )
            or embargo
        ):
            return await self._train_in_process(
                X_sequences=X_sequences,
                y_targets=y_targets,
                epochs=epochs,
                batch_size=batch_size,
                validation_split=validation_split,
                early_stopping_patience=early_stopping_patience,
                checkpoint_callback=checkpoint_callback,
                sample_groups=sample_groups,
                sample_dates=sample_dates,
                source_intervals=source_intervals,
                embargo=embargo,
                **kwargs,
            )
        try:
            from ..core.core_train_worker import (
                async_run_training_in_subprocess as run_training,
            )

            worker_result = await run_training(
                model_type="ridge",
                model_config={
                    "input_size": self.input_size,
                    "hidden_size": self.hidden_size,
                    "sequence_length": self.sequence_length,
                    "num_outputs": self.num_outputs,
                    "learning_rate": 0.005,
                    "dropout": 0.2,
                    "use_attention": False,
                },
                current_weights=self.get_weights(),
                X_data=X_sequences,
                y_targets=y_targets,
                training_params={
                    "epochs": epochs,
                    "batch_size": batch_size,
                    "validation_split": validation_split,
                    "early_stopping_patience": early_stopping_patience,
                },
            )
            if worker_result.get("success"):
                self.set_weights(worker_result["weights"])
                return {
                    "success": True,
                    "accuracy": worker_result.get("accuracy", 0.0),
                    "rmse": worker_result.get("rmse", 0.0),
                    "alpha": worker_result.get("alpha", 0.0),
                    "loo_cv_score": worker_result.get("loo_cv_score", 0.0),
                    "epochs_trained": worker_result.get("epochs_trained", 0),
                    "training_samples": worker_result.get("training_samples", 0),
                    "num_outputs": self.num_outputs,
                }
            _LOGGER.warning(
                "Subprocess Ridge training failed, falling back to local training: %s",
                worker_result.get("error_message"),
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to run Ridge training in subprocess, falling back to local training: %s",
                error,
            )
        return await self._train_in_process(
            X_sequences=X_sequences,
            y_targets=y_targets,
            epochs=epochs,
            batch_size=batch_size,
            validation_split=validation_split,
            early_stopping_patience=early_stopping_patience,
            checkpoint_callback=checkpoint_callback,
            **kwargs,
        )

    async def _train_in_process(
        self,
        X_sequences: List[Any],
        y_targets: List[Any],
        epochs: int = 1,
        batch_size: int = 16,
        validation_split: float = 0.2,
        early_stopping_patience: int = 10,
        checkpoint_callback: Any = None,
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
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
        training_indices, holdout_indices = self._chronological_split_indices(
            sample_count,
            validation_split,
            sample_groups,
            sample_dates,
            source_intervals,
            embargo,
        )
        training_count = len(training_indices)
        holdout_count = len(holdout_indices)
        if training_count < self.MIN_SAMPLES or holdout_count < 1:
            return {
                "success": False,
                "error_message": f"Need at least {self.MIN_SAMPLES} training and 1 holdout samples after temporal purge, got {training_count}/{holdout_count}",
                "training_samples": training_count,
                "holdout_samples": holdout_count,
            }
        training_sequences = flat_sequences[training_indices]
        training_targets = targets[training_indices]
        holdout_sequences = flat_sequences[holdout_indices]
        holdout_targets = targets[holdout_indices]
        normalized_training = self._normalize_features(training_sequences, fit=True)
        biased_training = self._add_bias(normalized_training)
        alpha_results = []
        for output_index in range(self.num_outputs):
            alpha_results.append(
                await self._select_alpha(
                    biased_training, training_targets[:, output_index]
                )
            )
        selected_alphas = [
            selected_alpha for selected_alpha, unused_score in alpha_results
        ]
        selected_scores = [
            selected_score for unused_score, selected_score in alpha_results
        ]
        self.alpha = float(np.median(selected_alphas))
        self.loo_cv_score = float(np.mean(selected_scores))
        self.weights = np.zeros((self.num_outputs, biased_training.shape[1]))
        for weight_index in range(self.num_outputs):
            self.weights[weight_index] = self._ridge_closed_form(
                biased_training, training_targets[:, weight_index], self.alpha
            )
        self.trained_samples = training_count
        biased_holdout = self._add_bias(
            self._normalize_features(holdout_sequences, fit=False)
        )
        predictions = np.dot(biased_holdout, self.weights.T)
        predictions = np.maximum(predictions, 0)
        predicted_totals = np.sum(predictions, axis=1)
        actual_totals = np.sum(holdout_targets, axis=1)
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
            "training_samples": training_count,
            "holdout_samples": holdout_count,
            "final_train_loss": self.loo_cv_score,
            "final_val_loss": rmse**2,
            "history": {
                "train_loss": [float(self.loo_cv_score)],
                "val_loss": [float(rmse**2)],
            },
        }

    @staticmethod
    def _intervals_overlap(
        first: Tuple[Any, Any], second: Tuple[Any, Any], embargo: Any = 0
    ) -> bool:
        first_start, first_end = first
        second_start, second_end = second
        if first_end < first_start:
            first_start, first_end = (first_end, first_start)
        if second_end < second_start:
            second_start, second_end = (second_end, second_start)
        if embargo:
            second_start -= embargo
            second_end += embargo
        return first_start <= second_end and second_start <= first_end

    @classmethod
    def _chronological_split_indices(
        cls,
        n_samples: int,
        validation_split: float,
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
    ) -> Tuple[List[int], List[int]]:
        group_labels = sample_groups if sample_groups is not None else sample_dates
        if group_labels is not None and len(group_labels) != n_samples:
            raise ValueError("sample group/date labels must match sample count")
        if source_intervals is not None and len(source_intervals) != n_samples:
            raise ValueError("source intervals must match sample count")
        validation_count = (
            max(1, int(n_samples * validation_split)) if n_samples >= 2 else 0
        )
        if group_labels is None:
            ordered_indices = list(range(n_samples))
            holdout_indices = ordered_indices[n_samples - validation_count :]
            training_indices = ordered_indices[: n_samples - validation_count]
        else:
            ordered_indices = sorted(
                range(n_samples), key=lambda index: str(group_labels[index])
            )
            holdout_indices = []
            holdout_groups = set()
            for group_index in reversed(ordered_indices):
                holdout_indices.append(group_index)
                holdout_groups.add(group_labels[group_index])
                if len(holdout_indices) >= validation_count:
                    break
            holdout_indices.reverse()
            training_indices = [
                group_index
                for group_index in ordered_indices
                if group_labels[group_index] not in holdout_groups
            ]
        if source_intervals is not None:
            if holdout_indices:
                holdout_intervals = [
                    source_intervals[group_index] for group_index in holdout_indices
                ]
                training_indices = [
                    group_index
                    for group_index in training_indices
                    if not any(
                        (
                            cls._intervals_overlap(
                                source_intervals[group_index], holdout_interval, embargo
                            )
                            for holdout_interval in holdout_intervals
                        )
                    )
                ]
        return (training_indices, holdout_indices)

    @classmethod
    def has_viable_temporal_split(
        cls,
        n_samples: int,
        validation_split: float,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
    ) -> bool:
        """Return whether enough leakage-safe training and holdout samples remain."""
        training_indices, holdout_indices = cls._chronological_split_indices(
            n_samples,
            validation_split,
            sample_dates=sample_dates,
            source_intervals=source_intervals,
        )
        return len(training_indices) >= cls.MIN_SAMPLES and bool(holdout_indices)

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
        normalized_sequence = self._normalize_features(flat_sequences, fit=False)
        biased_sequence = self._add_bias(normalized_sequence)
        if not np.isfinite(biased_sequence).all():
            _LOGGER.warning("Ridge predict: NaN/Inf in input, returning zeros")
            return [0.0] * self.num_outputs
        predictions = np.dot(biased_sequence, self.weights.T).flatten()
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
        output_array = np.array(self.predict(X))
        return (output_array, None)

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

    def set_weights(self, weights: Dict[str, Any], strict: bool = False) -> None:
        """Load weights from persistence @zara"""
        np = _ensure_numpy()
        restored_weights = (
            np.asarray(weights["weights"], dtype=float)
            if weights.get("weights") is not None
            else None
        )
        restored_means = (
            np.asarray(weights["feature_means"], dtype=float)
            if weights.get("feature_means") is not None
            else None
        )
        restored_stds = (
            np.asarray(weights["feature_stds"], dtype=float)
            if weights.get("feature_stds") is not None
            else None
        )
        restored_alpha = weights.get("alpha")
        input_size = int(weights.get("input_size", self.input_size))
        sequence_length = int(weights.get("sequence_length", self.sequence_length))
        output_count = int(weights.get("num_outputs", self.num_outputs))
        flat_size = int(weights.get("flat_size", input_size * sequence_length))
        training_samples = int(weights.get("trained_samples", 0))
        if strict:
            if flat_size != input_size * sequence_length:
                raise ValueError("Ridge artifact flat_size is inconsistent")
            expected_weight_shape = (output_count, flat_size + 1)
            if restored_weights is None:
                raise ValueError("Ridge artifact is missing weights")
            if restored_weights.shape != expected_weight_shape:
                raise ValueError("Ridge artifact weights have an invalid shape")
            if restored_means is None:
                raise ValueError("Ridge artifact is missing feature_means")
            if restored_means.shape != (flat_size,):
                raise ValueError("Ridge artifact feature_means have an invalid shape")
            if restored_stds is None:
                raise ValueError("Ridge artifact is missing feature_stds")
            if restored_stds.shape != (flat_size,):
                raise ValueError("Ridge artifact feature_stds have an invalid shape")
            if not all(
                (
                    np.all(np.isfinite(sample_metadata))
                    for sample_metadata in (
                        restored_weights,
                        restored_means,
                        restored_stds,
                    )
                )
            ):
                raise ValueError("Ridge artifact contains non-finite tensors")
            if np.any(restored_stds <= 0.0):
                raise ValueError("Ridge artifact feature_stds must be positive")
            if restored_alpha is None or not np.isfinite(float(restored_alpha)):
                raise ValueError("Ridge artifact alpha is invalid")
            if float(restored_alpha) <= 0.0:
                raise ValueError("Ridge artifact alpha must be positive")
            if training_samples < self.MIN_SAMPLES:
                raise ValueError("Ridge artifact has insufficient training samples")
        self.weights = restored_weights
        self.alpha = weights.get("alpha")
        self.feature_means = restored_means
        self.feature_stds = restored_stds
        self.input_size = input_size
        self.hidden_size = weights.get("hidden_size", self.hidden_size)
        self.sequence_length = sequence_length
        self.num_outputs = output_count
        self.flat_size = flat_size
        self.trained_samples = training_samples
        self.loo_cv_score = weights.get("loo_cv_score", 0.0)
        self.training_accuracy = weights.get("accuracy", 0.0)
        self.training_rmse = weights.get("rmse", 0.0)
        _LOGGER.debug(
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
