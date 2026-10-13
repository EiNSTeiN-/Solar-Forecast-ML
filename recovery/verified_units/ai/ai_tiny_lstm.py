# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Stacked LSTM Neural Network for Solar Forecast ML V16.8.0 - AI V3.0.

2-Layer Stacked LSTM with Multi-Head Attention, Positional Encoding,
Layer Normalization, Cosine Annealing LR and Data Augmentation.
Pure NumPy implementation for Home Assistant compatibility.

@zara
"""
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)
_np: Optional[Any] = None


def _ensure_numpy() -> Any:
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


class TinyLSTM:
    """Stacked Multi-Output LSTM with Multi-Head Attention for solar forecasting. @zara"""

    def __init__(
        self,
        input_size: int = 28,
        hidden_sizes: Tuple[int, int] = (48, 24),
        sequence_length: int = 24,
        num_outputs: int = 1,
        learning_rate: float = 0.005,
        dropout: float = 0.2,
        num_heads: int = 4,
        seed: Optional[int] = None,
    ):
        np = _ensure_numpy()
        self.input_size = input_size
        self.hidden1 = hidden_sizes[0]
        self.hidden2 = hidden_sizes[1]
        self.sequence_length = sequence_length
        self.num_outputs = num_outputs
        self.learning_rate = learning_rate
        self.lr_max = learning_rate
        self.lr_min = 0.0001
        self.dropout = dropout
        self.num_heads = num_heads
        self.head_dim = self.hidden2 // num_heads
        self.noise_std = 0.02
        self.seed = seed
        self._rng = np.random.default_rng(seed)
        self.adam_step = 0
        self.pe = self._build_positional_encoding()
        layer1_combined_size = input_size + self.hidden1
        layer1_init_limit = np.sqrt(6 / (input_size + self.hidden1))
        self.Wf = self._rng.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_combined_size)
        )
        self.Wi = self._rng.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_combined_size)
        )
        self.Wc = self._rng.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_combined_size)
        )
        self.Wo = self._rng.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_combined_size)
        )
        self.bf = np.ones((self.hidden1, 1))
        self.bi = np.zeros((self.hidden1, 1))
        self.bc = np.zeros((self.hidden1, 1))
        self.bo = np.zeros((self.hidden1, 1))
        self.ln_gamma = np.ones((self.hidden1, 1))
        self.ln_beta = np.zeros((self.hidden1, 1))
        layer2_combined_size = self.hidden1 + self.hidden2
        layer2_init_limit = np.sqrt(6 / (self.hidden1 + self.hidden2))
        self.Wf2 = self._rng.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_combined_size)
        )
        self.Wi2 = self._rng.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_combined_size)
        )
        self.Wc2 = self._rng.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_combined_size)
        )
        self.Wo2 = self._rng.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_combined_size)
        )
        self.bf2 = np.ones((self.hidden2, 1))
        self.bi2 = np.zeros((self.hidden2, 1))
        self.bc2 = np.zeros((self.hidden2, 1))
        self.bo2 = np.zeros((self.hidden2, 1))
        attention_init_limit = np.sqrt(6 / (self.hidden2 + self.hidden2))
        self.W_query = self._rng.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_key = self._rng.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_value = self._rng.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_attn_out = self._rng.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.b_attn_out = np.zeros((self.hidden2, 1))
        output_init_limit = np.sqrt(6 / (self.hidden2 + num_outputs))
        self.Wy = self._rng.uniform(
            -output_init_limit, output_init_limit, (num_outputs, self.hidden2)
        )
        self.by = np.zeros((num_outputs, 1))
        self.beta1 = 0.9
        self.beta2 = 0.999
        self.epsilon = 1e-08
        self._init_adam_states()
        self.adam_step = 0
        _LOGGER.debug(
            "TinyLSTM V3.0: input=%d, layers=(%d,%d), seq=%d, outputs=%d, heads=%d",
            input_size,
            self.hidden1,
            self.hidden2,
            sequence_length,
            num_outputs,
            num_heads,
        )

    def _build_positional_encoding(self) -> Any:
        np = _ensure_numpy()
        encoding = np.zeros((self.sequence_length, self.input_size))
        for position in range(self.sequence_length):
            for feature_index in range(0, self.input_size, 2):
                frequency_scale = math.pow(10000, 2 * feature_index / self.input_size)
                encoding[position, feature_index] = math.sin(position / frequency_scale)
                if feature_index + 1 < self.input_size:
                    encoding[position, feature_index + 1] = math.cos(
                        position / frequency_scale
                    )
        return encoding

    def _init_adam_states(self):
        np = _ensure_numpy()
        self.m = {}
        self.v = {}
        for weight_name in self._weight_names():
            weight = getattr(self, weight_name)
            self.m[weight_name] = np.zeros_like(weight)
            self.v[weight_name] = np.zeros_like(weight)

    def _weight_names(self) -> List[str]:
        return [
            "Wf",
            "Wi",
            "Wc",
            "Wo",
            "bf",
            "bi",
            "bc",
            "bo",
            "ln_gamma",
            "ln_beta",
            "Wf2",
            "Wi2",
            "Wc2",
            "Wo2",
            "bf2",
            "bi2",
            "bc2",
            "bo2",
            "W_query",
            "W_key",
            "W_value",
            "W_attn_out",
            "b_attn_out",
            "Wy",
            "by",
        ]

    def _snapshot_weights(self) -> Dict[str, Any]:
        return {
            weight_name: getattr(self, weight_name).copy()
            for weight_name in self._weight_names()
        }

    def _restore_weights(self, snapshot: Dict[str, Any]) -> None:
        for weight_name, weight_values in snapshot.items():
            setattr(self, weight_name, weight_values.copy())

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
            try:
                second_start -= embargo
                second_end += embargo
            except TypeError:
                _LOGGER.warning("Unable to apply embargo to source intervals")
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
        sample_labels = sample_groups if sample_groups is not None else sample_dates
        if sample_labels is not None and len(sample_labels) != n_samples:
            raise ValueError("sample group/date labels must match sample count")
        if source_intervals is not None and len(source_intervals) != n_samples:
            raise ValueError("source intervals must match sample count")
        validation_count = (
            max(1, int(n_samples * validation_split)) if n_samples >= 2 else 0
        )
        if sample_labels is None:
            ordered_indices = list(range(n_samples))
            validation_indices = ordered_indices[n_samples - validation_count :]
            train_indices = ordered_indices[: n_samples - validation_count]
        else:
            try:
                ordered_indices = sorted(
                    range(n_samples), key=lambda index: sample_labels[index]
                )
            except TypeError:
                ordered_indices = sorted(
                    range(n_samples), key=lambda index: str(sample_labels[index])
                )
            validation_indices = []
            validation_groups = set()
            for sample_index in reversed(ordered_indices):
                validation_indices.append(sample_index)
                validation_groups.add(sample_labels[sample_index])
                if len(validation_indices) >= validation_count:
                    break
            train_indices = [
                sample_index
                for sample_index in ordered_indices
                if sample_labels[sample_index] not in validation_groups
            ]
            validation_indices.reverse()
        if source_intervals is not None and validation_indices:
            validation_intervals = [
                source_intervals[sample_index] for sample_index in validation_indices
            ]
            train_indices = [
                sample_index
                for sample_index in train_indices
                if not any(
                    (
                        cls._intervals_overlap(
                            source_intervals[sample_index], validation_interval, embargo
                        )
                        for validation_interval in validation_intervals
                    )
                )
            ]
        return (train_indices, validation_indices)

    def _sigmoid(self, x: Any) -> Any:
        np = _ensure_numpy()
        clipped_input = np.clip(x, -500, 500)
        return 1 / (1 + np.exp(-clipped_input))

    def _tanh(self, x: Any) -> Any:
        np = _ensure_numpy()
        return np.tanh(np.clip(x, -500, 500))

    def _layer_norm_forward(self, x: Any) -> Tuple[Any, Dict[str, Any]]:
        np = _ensure_numpy()
        mean = np.mean(x, axis=0, keepdims=True)
        variance = np.var(x, axis=0, keepdims=True)
        epsilon = 1e-05
        normalized_input = (x - mean) / np.sqrt(variance + epsilon)
        output = self.ln_gamma * normalized_input + self.ln_beta
        forward_cache = {
            "x": x,
            "x_norm": normalized_input,
            "mean": mean,
            "var": variance,
            "eps": epsilon,
        }
        return (output, forward_cache)

    def _layer_norm_backward(
        self, dy: Any, cache: Dict[str, Any]
    ) -> Tuple[Any, Any, Any]:
        np = _ensure_numpy()
        normalized_input = cache["x_norm"]
        variance = cache["var"]
        epsilon = cache["eps"]
        gamma_gradient = dy * normalized_input
        beta_gradient = dy.copy()
        normalized_gradient = dy * self.ln_gamma
        inverse_std = 1.0 / np.sqrt(variance + epsilon)
        input_gradient = normalized_gradient * inverse_std
        return (input_gradient, gamma_gradient, beta_gradient)

    def _lstm_cell_forward(
        self,
        xt: Any,
        h_prev: Any,
        c_prev: Any,
        W_f: Any,
        W_i: Any,
        W_c: Any,
        W_o: Any,
        b_f: Any,
        b_i: Any,
        b_c: Any,
        b_o: Any,
    ) -> Tuple[Any, Any, Dict[str, Any]]:
        np = _ensure_numpy()
        combined_input = np.concatenate((h_prev, xt), axis=0)
        forget_gate = self._sigmoid(np.dot(W_f, combined_input) + b_f)
        input_gate = self._sigmoid(np.dot(W_i, combined_input) + b_i)
        cell_candidate = self._tanh(np.dot(W_c, combined_input) + b_c)
        output_gate = self._sigmoid(np.dot(W_o, combined_input) + b_o)
        cell_state = forget_gate * c_prev + input_gate * cell_candidate
        hidden_state = output_gate * self._tanh(cell_state)
        forward_cache = {
            "concat": combined_input,
            "ft": forget_gate,
            "it": input_gate,
            "ct_hat": cell_candidate,
            "ot": output_gate,
            "c_prev": c_prev,
            "ct": cell_state,
            "ht": hidden_state,
            "h_prev": h_prev,
            "xt": xt,
        }
        return (hidden_state, cell_state, forward_cache)

    def _lstm_cell_backward(
        self,
        dh_next: Any,
        dc_next: Any,
        cache: Dict[str, Any],
        W_f: Any,
        W_i: Any,
        W_c: Any,
        W_o: Any,
        hidden_size: int,
    ) -> Tuple[Any, Any, Any, Dict[str, Any]]:
        np = _ensure_numpy()
        combined_input = cache["concat"]
        forget_gate, input_gate, cell_candidate, output_gate = (
            cache["ft"],
            cache["it"],
            cache["ct_hat"],
            cache["ot"],
        )
        previous_cell, cell_state = (cache["c_prev"], cache["ct"])
        output_gate_gradient = (
            dh_next * self._tanh(cell_state) * output_gate * (1 - output_gate)
        )
        cell_gradient = (
            dh_next * output_gate * (1 - self._tanh(cell_state) ** 2) + dc_next
        )
        forget_gate_gradient = (
            cell_gradient * previous_cell * forget_gate * (1 - forget_gate)
        )
        input_gate_gradient = (
            cell_gradient * cell_candidate * input_gate * (1 - input_gate)
        )
        cell_candidate_gradient = cell_gradient * input_gate * (1 - cell_candidate**2)
        gradients = {
            "Wf": np.dot(forget_gate_gradient, combined_input.T),
            "Wi": np.dot(input_gate_gradient, combined_input.T),
            "Wc": np.dot(cell_candidate_gradient, combined_input.T),
            "Wo": np.dot(output_gate_gradient, combined_input.T),
            "bf": forget_gate_gradient,
            "bi": input_gate_gradient,
            "bc": cell_candidate_gradient,
            "bo": output_gate_gradient,
        }
        combined_gradient = (
            np.dot(W_f.T, forget_gate_gradient)
            + np.dot(W_i.T, input_gate_gradient)
            + np.dot(W_c.T, cell_candidate_gradient)
            + np.dot(W_o.T, output_gate_gradient)
        )
        previous_hidden_gradient = combined_gradient[:hidden_size, :]
        input_gradient = combined_gradient[hidden_size:, :]
        previous_cell_gradient = cell_gradient * forget_gate
        return (
            previous_hidden_gradient,
            previous_cell_gradient,
            input_gradient,
            gradients,
        )

    def _multi_head_attention_forward(
        self, query: Any, hidden_states: List[Any]
    ) -> Tuple[Any, Any, Dict[str, Any]]:
        np = _ensure_numpy()
        hidden_matrix = np.hstack(hidden_states)
        projected_query = np.dot(self.W_query, query)
        projected_keys = np.dot(self.W_key, hidden_matrix)
        projected_values = np.dot(self.W_value, hidden_matrix)
        sequence_steps = len(hidden_states)
        all_context = np.zeros((self.hidden2, 1))
        head_caches = []
        for head_index in range(self.num_heads):
            head_start = head_index * self.head_dim
            head_end = head_start + self.head_dim
            head_query = projected_query[head_start:head_end, :]
            head_keys = projected_keys[head_start:head_end, :]
            head_values = projected_values[head_start:head_end, :]
            scores = np.dot(head_query.T, head_keys) / np.sqrt(self.head_dim)
            score_max = np.max(scores, axis=1, keepdims=True)
            exp_scores = np.exp(np.clip(scores - score_max, -500, 500))
            attention_weights = exp_scores / (
                np.sum(exp_scores, axis=1, keepdims=True) + 1e-10
            )
            head_context = np.dot(head_values, attention_weights.T)
            all_context[head_start:head_end, :] = head_context
            head_caches.append(
                {
                    "Q_h": head_query,
                    "K_h": head_keys,
                    "V_h": head_values,
                    "scores": scores,
                    "attn_w": attention_weights,
                    "context_h": head_context,
                }
            )
            continue
        output = np.dot(self.W_attn_out, all_context) + self.b_attn_out
        output = output + query
        flat_attention_weights = np.concatenate(
            [head_cache["attn_w"].flatten() for head_cache in head_caches]
        )
        forward_cache = {
            "query": query,
            "H": hidden_matrix,
            "Q": projected_query,
            "K": projected_keys,
            "V": projected_values,
            "all_context": all_context,
            "head_caches": head_caches,
            "seq_len": sequence_steps,
        }
        return (output, flat_attention_weights, forward_cache)

    def _multi_head_attention_backward(
        self, dout: Any, cache: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any]]:
        np = _ensure_numpy()
        query_vector = cache["query"]
        hidden_matrix = cache["H"]
        projected_query = cache["Q"]
        projected_keys = cache["K"]
        projected_values = cache["V"]
        all_context = cache["all_context"]
        head_caches = cache["head_caches"]
        residual_gradient = dout.copy()
        output_gradient = dout.copy()
        gradients = {}
        gradients["W_attn_out"] = np.dot(output_gradient, all_context.T)
        gradients["b_attn_out"] = output_gradient.copy()
        context_gradient = np.dot(self.W_attn_out.T, output_gradient)
        query_gradient_matrix = np.zeros_like(projected_query)
        key_gradient_matrix = np.zeros_like(projected_keys)
        value_gradient_matrix = np.zeros_like(projected_values)
        for head_index in range(self.num_heads):
            head_start = head_index * self.head_dim
            head_end = head_start + self.head_dim
            head_cache = head_caches[head_index]
            head_context_gradient = context_gradient[head_start:head_end, :]
            head_value_gradient = np.dot(head_context_gradient, head_cache["attn_w"])
            attention_weight_gradient = np.dot(
                head_cache["V_h"].T, head_context_gradient
            ).T
            softmax_dot = np.sum(
                head_cache["attn_w"] * attention_weight_gradient, axis=1, keepdims=True
            )
            score_gradient = head_cache["attn_w"] * (
                attention_weight_gradient - softmax_dot
            )
            scaled_score_gradient = score_gradient / np.sqrt(self.head_dim)
            head_query_gradient = np.dot(head_cache["K_h"], scaled_score_gradient.T)
            head_key_gradient = np.dot(head_cache["Q_h"], scaled_score_gradient)
            query_gradient_matrix[head_start:head_end, :] += head_query_gradient
            key_gradient_matrix[head_start:head_end, :] += head_key_gradient
            value_gradient_matrix[head_start:head_end, :] += head_value_gradient
            continue
        gradients["W_query"] = np.dot(query_gradient_matrix, query_vector.T)
        gradients["W_key"] = np.dot(key_gradient_matrix, hidden_matrix.T)
        gradients["W_value"] = np.dot(value_gradient_matrix, hidden_matrix.T)
        projected_query_gradient = np.dot(self.W_query.T, query_gradient_matrix)
        query_gradient = residual_gradient + projected_query_gradient
        return (query_gradient, gradients)

    def forward(
        self, X: Any, training: bool = False
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        np = _ensure_numpy()
        if training and self.noise_std > 0:
            X = X + self._rng.normal(0.0, self.noise_std, X.shape)
        encoded_input = X + self.pe[: X.shape[0], : X.shape[1]]
        hidden_state_1 = np.zeros((self.hidden1, 1))
        cell_state_1 = np.zeros((self.hidden1, 1))
        hidden_state_2 = np.zeros((self.hidden2, 1))
        cell_state_2 = np.zeros((self.hidden2, 1))
        layer1_caches = []
        layer2_caches = []
        layer_norm_caches = []
        layer2_hidden_states = []
        for time_index in range(encoded_input.shape[0]):
            step_input = encoded_input[time_index : time_index + 1].T
            hidden_state_1, cell_state_1, layer1_cache = self._lstm_cell_forward(
                step_input,
                hidden_state_1,
                cell_state_1,
                self.Wf,
                self.Wi,
                self.Wc,
                self.Wo,
                self.bf,
                self.bi,
                self.bc,
                self.bo,
            )
            if training and self.dropout > 0:
                layer1_dropout_mask = (
                    self._rng.random(hidden_state_1.shape) > self.dropout
                ).astype(float)
                layer1_dropout_mask /= 1 - self.dropout
                hidden_state_1 = hidden_state_1 * layer1_dropout_mask
                layer1_cache["dropout_mask"] = layer1_dropout_mask
            normalized_hidden, layer_norm_cache = self._layer_norm_forward(
                hidden_state_1
            )
            hidden_state_2, cell_state_2, layer2_cache = self._lstm_cell_forward(
                normalized_hidden,
                hidden_state_2,
                cell_state_2,
                self.Wf2,
                self.Wi2,
                self.Wc2,
                self.Wo2,
                self.bf2,
                self.bi2,
                self.bc2,
                self.bo2,
            )
            if training and self.dropout > 0:
                layer2_dropout_mask = (
                    self._rng.random(hidden_state_2.shape) > self.dropout
                ).astype(float)
                layer2_dropout_mask /= 1 - self.dropout
                hidden_state_2 = hidden_state_2 * layer2_dropout_mask
                layer2_cache["dropout_mask"] = layer2_dropout_mask
            if training:
                layer1_caches.append(layer1_cache)
                layer2_caches.append(layer2_cache)
                layer_norm_caches.append(layer_norm_cache)
            layer2_hidden_states.append(hidden_state_2.copy())
            continue
        attention_output, flat_attention_weights, attention_cache = (
            self._multi_head_attention_forward(hidden_state_2, layer2_hidden_states)
        )
        prediction = np.dot(self.Wy, attention_output) + self.by
        if training:
            return (
                prediction,
                {
                    "caches1": layer1_caches,
                    "caches2": layer2_caches,
                    "ln_caches": layer_norm_caches,
                    "final_h2": hidden_state_2,
                    "hidden_states_2": layer2_hidden_states,
                    "attn_cache": attention_cache,
                    "attn_weights": flat_attention_weights,
                    "h_attn": attention_output,
                },
            )
        return (prediction, None)

    def backward(self, dy: Any, cache: Dict[str, Any]) -> Dict[str, Any]:
        np = _ensure_numpy()
        layer1_caches = cache["caches1"]
        layer2_caches = cache["caches2"]
        layer_norm_caches = cache["ln_caches"]
        attention_output = cache["h_attn"]
        attention_cache = cache["attn_cache"]
        attention_output_gradient = np.dot(self.Wy.T, dy)
        all_gradients = {
            initial_weight_name: np.zeros_like(getattr(self, initial_weight_name))
            for initial_weight_name in self._weight_names()
        }
        all_gradients["Wy"] = np.dot(dy, attention_output.T)
        all_gradients["by"] = dy.copy()
        hidden_gradient_2, attention_gradients = self._multi_head_attention_backward(
            attention_output_gradient, attention_cache
        )
        for weight_name in attention_gradients:
            all_gradients[weight_name] = attention_gradients[weight_name]
        cell_gradient_2 = np.zeros((self.hidden2, 1))
        cell_gradient_1 = np.zeros((self.hidden1, 1))
        for time_index in reversed(range(len(layer2_caches))):
            layer2_cache = layer2_caches[time_index]
            if "dropout_mask" in layer2_cache:
                hidden_gradient_2 = hidden_gradient_2 * layer2_cache["dropout_mask"]
            (
                previous_hidden_gradient_2,
                previous_cell_gradient_2,
                normalized_hidden_gradient,
                layer2_gradients,
            ) = self._lstm_cell_backward(
                hidden_gradient_2,
                cell_gradient_2,
                layer2_cache,
                self.Wf2,
                self.Wi2,
                self.Wc2,
                self.Wo2,
                self.hidden2,
            )
            for weight_name in ("Wf", "Wi", "Wc", "Wo", "bf", "bi", "bc", "bo"):
                all_gradients[weight_name + "2"] += layer2_gradients[weight_name]
            layer1_normalized_gradient, gamma_gradient, beta_gradient = (
                self._layer_norm_backward(
                    normalized_hidden_gradient, layer_norm_caches[time_index]
                )
            )
            all_gradients["ln_gamma"] += gamma_gradient
            all_gradients["ln_beta"] += beta_gradient
            hidden_gradient_1 = layer1_normalized_gradient
            layer1_cache = layer1_caches[time_index]
            if "dropout_mask" in layer1_cache:
                hidden_gradient_1 = hidden_gradient_1 * layer1_cache["dropout_mask"]
            (
                previous_hidden_gradient_1,
                previous_cell_gradient_1,
                input_gradient,
                layer1_gradients,
            ) = self._lstm_cell_backward(
                hidden_gradient_1,
                cell_gradient_1,
                layer1_cache,
                self.Wf,
                self.Wi,
                self.Wc,
                self.Wo,
                self.hidden1,
            )
            for weight_name in ("Wf", "Wi", "Wc", "Wo", "bf", "bi", "bc", "bo"):
                all_gradients[weight_name] += layer1_gradients[weight_name]
            hidden_gradient_2 = previous_hidden_gradient_2
            cell_gradient_2 = previous_cell_gradient_2
            cell_gradient_1 = (
                previous_cell_gradient_1
                if time_index > 0
                else np.zeros((self.hidden1, 1))
            )
            continue
        max_gradient_norm = 5.0
        gradient_norm = np.sqrt(
            sum((np.sum(gradient**2) for gradient in all_gradients.values()))
        )
        if gradient_norm > max_gradient_norm:
            for weight_name in all_gradients:
                all_gradients[weight_name] *= max_gradient_norm / gradient_norm
        return all_gradients

    def _cosine_lr(self, epoch: int, max_epochs: int) -> float:
        return self.lr_min + 0.5 * (self.lr_max - self.lr_min) * (
            1 + math.cos(math.pi * epoch / max_epochs)
        )

    def _update_weights_adam(self, grads: Dict[str, Any], t: int):
        np = _ensure_numpy()
        if t <= 0:
            raise ValueError("Adam step must be positive")
        for weight_name in grads:
            if weight_name not in self.m:
                continue
            self.m[weight_name] = (
                self.beta1 * self.m[weight_name] + (1 - self.beta1) * grads[weight_name]
            )
            self.v[weight_name] = (
                self.beta2 * self.v[weight_name]
                + (1 - self.beta2) * grads[weight_name] ** 2
            )
            corrected_first_moment = self.m[weight_name] / (1 - self.beta1**t)
            corrected_second_moment = self.v[weight_name] / (1 - self.beta2**t)
            weight = getattr(self, weight_name)
            weight -= (
                self.learning_rate
                * corrected_first_moment
                / (np.sqrt(corrected_second_moment) + self.epsilon)
            )
            setattr(self, weight_name, weight)
            continue

    async def train(
        self,
        X_sequences: List[Any],
        y_targets: List[Any],
        epochs: int = 200,
        batch_size: int = 16,
        validation_split: float = 0.2,
        early_stopping_patience: int = 20,
        checkpoint_callback: Optional[callable] = None,
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Train Stacked LSTM model. Runs out-of-process to protect HA event loop. @zara"""
        if (
            any(
                (
                    optional_value is not None
                    for optional_value in (
                        sample_groups,
                        sample_dates,
                        source_intervals,
                        seed,
                    )
                )
            )
            or embargo
        ):
            return await self._train_in_process(
                X_sequences,
                y_targets,
                epochs,
                batch_size,
                validation_split,
                early_stopping_patience,
                checkpoint_callback,
                sample_groups,
                sample_dates,
                source_intervals,
                embargo,
                seed,
            )
        try:
            from ..core.core_train_worker import (
                async_run_training_in_subprocess as async_run_training_in_subprocess,
            )

            worker_result = await async_run_training_in_subprocess(
                model_type="lstm",
                model_config={
                    "input_size": self.input_size,
                    "hidden_sizes": (self.hidden1, self.hidden2),
                    "sequence_length": self.sequence_length,
                    "num_outputs": self.num_outputs,
                    "learning_rate": self.learning_rate,
                    "dropout": self.dropout,
                    "num_heads": self.num_heads,
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
                    "epochs_trained": worker_result.get("epochs_trained", 0),
                    "training_samples": worker_result.get("training_samples", 0),
                    "has_attention": worker_result.get("has_attention", False),
                }
            _LOGGER.warning(
                "Subprocess LSTM training failed, falling back to local training: %s",
                worker_result.get("error_message"),
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to run LSTM training in subprocess, falling back to local training: %s",
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
        )

    async def _train_in_process(
        self,
        X_sequences: List[Any],
        y_targets: List[Any],
        epochs: int = 200,
        batch_size: int = 16,
        validation_split: float = 0.2,
        early_stopping_patience: int = 20,
        checkpoint_callback: Optional[callable] = None,
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        np = _ensure_numpy()
        _LOGGER.info(
            "Training V3.0: %d samples, %d epochs, %d outputs",
            len(X_sequences),
            epochs,
            self.num_outputs,
        )

        def _to_target_array(y):
            if isinstance(y, (int, float)):
                return np.array([[float(y)]] * self.num_outputs) if True else None
            if isinstance(y, (list, tuple)):
                target_array = np.array([[float(target_value)] for target_value in y])
                if target_array.shape[0] != self.num_outputs:
                    if target_array.shape[0] < self.num_outputs:
                        target_array = np.vstack(
                            [
                                target_array,
                                np.zeros((self.num_outputs - target_array.shape[0], 1)),
                            ]
                        )
                    else:
                        target_array = target_array[: self.num_outputs]
                return target_array if True else None
            return np.array([[0.0]] * self.num_outputs) if True else None

        sample_count = len(X_sequences)
        if seed is not None:
            self.seed = seed
            self._rng = np.random.default_rng(seed)
        self.adam_step = 0
        train_indices, validation_indices = self._chronological_split_indices(
            sample_count,
            validation_split,
            sample_groups,
            sample_dates,
            source_intervals,
            embargo,
        )
        training_sample_count = len(train_indices)
        if training_sample_count < 1:
            return {
                "success": False,
                "error_message": "No training samples remain after temporal purge",
                "training_samples": 0,
                "holdout_samples": len(validation_indices),
            }
        sequence_arrays = [np.array(raw_sequence) for raw_sequence in X_sequences]
        target_arrays = [_to_target_array(raw_target) for raw_target in y_targets]
        train_sequences = [
            sequence_arrays[validation_index] for validation_index in train_indices
        ]
        train_targets = [
            target_arrays[validation_index] for validation_index in train_indices
        ]
        validation_sequences = [
            sequence_arrays[validation_index] for validation_index in validation_indices
        ]
        validation_targets = [
            target_arrays[validation_index] for validation_index in validation_indices
        ]
        best_validation_loss = float("inf")
        best_weights = None
        patience_count = 0
        has_validation = len(validation_sequences) > 0
        history = {"train_loss": [], "val_loss": []}
        for epoch in range(1, epochs + 1):
            self.learning_rate = self._cosine_lr(epoch, epochs)
            shuffled_indices = self._rng.permutation(training_sample_count)
            epoch_loss_total = 0.0
            for batch_start in range(0, training_sample_count, batch_size):
                batch_end = min(batch_start + batch_size, training_sample_count)
                batch_indices = shuffled_indices[batch_start:batch_end]
                batch_loss_total = 0.0
                batch_gradients = None
                for sample_index in batch_indices:
                    sample_sequence = train_sequences[sample_index]
                    sample_target = train_targets[sample_index]
                    prediction, training_cache = self.forward(
                        sample_sequence, training=True
                    )
                    sample_loss = ((prediction - sample_target) ** 2).mean()
                    batch_loss_total += sample_loss
                    prediction_gradient = (
                        2 * (prediction - sample_target) / sample_target.size
                    )
                    gradients = self.backward(prediction_gradient, training_cache)
                    if batch_gradients is None:
                        batch_gradients = gradients
                    else:
                        for weight_name in batch_gradients:
                            batch_gradients[weight_name] += gradients[weight_name]
                if batch_gradients is None:
                    continue
                for weight_name in batch_gradients:
                    batch_gradients[weight_name] /= len(batch_indices)
                self.adam_step += 1
                self._update_weights_adam(batch_gradients, self.adam_step)
                epoch_loss_total += batch_loss_total
                await asyncio.sleep(0)
                continue
            train_loss = (
                epoch_loss_total / training_sample_count
                if training_sample_count > 0
                else 0.0
            )
            validation_loss = 0.0
            if has_validation:
                for validation_index in range(len(validation_sequences)):
                    sample_sequence = validation_sequences[validation_index]
                    sample_target = validation_targets[validation_index]
                    prediction, unused_cache = self.forward(
                        sample_sequence, training=False
                    )
                    validation_loss += ((prediction - sample_target) ** 2).mean()
                    if validation_index % 10 == 0:
                        await asyncio.sleep(0)
                validation_loss /= len(validation_sequences)
            history["train_loss"].append(float(train_loss))
            history["val_loss"].append(float(validation_loss))
            if epoch % 10 == 0:
                _LOGGER.info(
                    "Epoch %d: train=%.4f, val=%.4f, lr=%.6f",
                    epoch,
                    train_loss,
                    validation_loss,
                    self.learning_rate,
                )
            if has_validation:
                if validation_loss < best_validation_loss:
                    best_validation_loss = validation_loss
                    best_weights = self._snapshot_weights()
                    patience_count = 0
                else:
                    patience_count += 1
                if patience_count >= early_stopping_patience:
                    _LOGGER.info("Early stopping at epoch %d", epoch)
                    break
            if checkpoint_callback and epoch % 10 == 0:
                await checkpoint_callback(epoch, self.get_weights())
            await asyncio.sleep(0.01)
            continue
        if best_weights is not None:
            self._restore_weights(best_weights)
        validation_predictions = []
        validation_actuals = []
        for validation_index, sample_sequence in enumerate(validation_sequences):
            prediction, unused_cache = self.forward(sample_sequence, training=False)
            validation_predictions.append(float(np.sum(prediction)))
            validation_actuals.append(
                float(np.sum(validation_targets[validation_index]))
            )
        r2 = 0.0
        rmse = 0.0
        if len(validation_actuals) > 0:
            actual_array = np.array(validation_actuals)
            prediction_array = np.array(validation_predictions)
            residual_sum_squares = np.sum((actual_array - prediction_array) ** 2)
            total_sum_squares = np.sum((actual_array - np.mean(actual_array)) ** 2)
            r2 = (
                1 - residual_sum_squares / total_sum_squares
                if total_sum_squares > 0
                else 0.0
            )
            rmse = float(np.sqrt(np.mean((actual_array - prediction_array) ** 2)))
        _LOGGER.info(
            "Training complete: R2=%.3f, RMSE=%.3f kWh, outputs=%d, layers=2, heads=%d",
            r2,
            rmse,
            self.num_outputs,
            self.num_heads,
        )
        return {
            "success": True,
            "accuracy": float(r2),
            "rmse": rmse,
            "final_train_loss": float(history["train_loss"][-1]),
            "final_val_loss": float(history["val_loss"][-1]),
            "best_val_loss": float(best_validation_loss),
            "epochs_trained": epoch,
            "num_outputs": self.num_outputs,
            "training_samples": training_sample_count,
            "holdout_samples": len(validation_indices),
            "adam_steps": self.adam_step,
            "history": history,
        }

    def predict(self, X_sequence: Any) -> List[float]:
        np = _ensure_numpy()
        sequence_array = np.array(X_sequence)
        if sequence_array.ndim == 3:
            if sequence_array.shape[0] != 1:
                raise ValueError(
                    f"Batch not supported, got shape {sequence_array.shape}"
                )
            sequence_array = sequence_array.squeeze(0)
        if sequence_array.ndim != 2:
            raise ValueError(
                f"Expected 2D, got {sequence_array.ndim}D shape {sequence_array.shape}"
            )
        if sequence_array.shape[0] != self.sequence_length:
            raise ValueError(
                f"Sequence length: expected {self.sequence_length}, got {sequence_array.shape[0]}"
            )
        if sequence_array.shape[1] != self.input_size:
            raise ValueError(
                f"Features: expected {self.input_size}, got {sequence_array.shape[1]}"
            )
        if not np.isfinite(sequence_array).all():
            _LOGGER.warning("LSTM predict: NaN/Inf in input, returning zeros")
            return [0.0] * self.num_outputs
        prediction, unused_cache = self.forward(sequence_array, training=False)
        if not np.isfinite(prediction).all():
            _LOGGER.warning("LSTM predict: NaN/Inf in output, returning zeros")
            return [0.0] * self.num_outputs
        return [
            float(prediction[output_index, 0])
            for output_index in range(self.num_outputs)
        ]

    def predict_total(self, X_sequence: Any) -> float:
        return sum(self.predict(X_sequence))

    def get_weights(self) -> Dict[str, Any]:
        weight_artifact = {
            "input_size": self.input_size,
            "hidden_size": self.hidden1,
            "hidden2": self.hidden2,
            "sequence_length": self.sequence_length,
            "num_outputs": self.num_outputs,
            "has_attention": True,
            "num_layers": 2,
            "num_heads": self.num_heads,
            "training_learning_rate": self.lr_max,
            "dropout": self.dropout,
        }
        for weight_name in self._weight_names():
            weight_artifact[weight_name] = getattr(self, weight_name).tolist()
        return weight_artifact

    def set_weights(self, weights: Dict[str, Any]):
        np = _ensure_numpy()
        artifact_architecture = (
            int(weights.get("input_size", self.input_size)),
            int(weights.get("hidden_size", self.hidden1)),
            int(weights.get("hidden2", self.hidden2)),
            int(weights.get("sequence_length", self.sequence_length)),
            int(weights.get("num_outputs", self.num_outputs)),
            int(weights.get("num_heads", self.num_heads)),
        )
        model_architecture = (
            self.input_size,
            self.hidden1,
            self.hidden2,
            self.sequence_length,
            self.num_outputs,
            self.num_heads,
        )
        if artifact_architecture != model_architecture:
            raise ValueError(
                "LSTM weight architecture does not match constructed model"
            )
        validated_weights = {}
        for weight_name in self._weight_names():
            if weight_name not in weights:
                raise ValueError(f"LSTM artifact is missing weight {weight_name}")
            weight_values = np.array(weights[weight_name], dtype=np.float64)
            if weight_name in (
                "bf",
                "bi",
                "bc",
                "bo",
                "ln_gamma",
                "ln_beta",
                "bf2",
                "bi2",
                "bc2",
                "bo2",
                "b_attn_out",
                "by",
            ):
                weight_values = weight_values.reshape(-1, 1)
            if weight_values.shape != getattr(self, weight_name).shape:
                raise ValueError(f"LSTM weight {weight_name} has an invalid shape")
            if not np.isfinite(weight_values).all():
                raise ValueError(
                    f"LSTM weight {weight_name} contains non-finite values"
                )
            validated_weights[weight_name] = weight_values
        self.lr_max = float(weights.get("training_learning_rate", self.lr_max))
        self.learning_rate = self.lr_max
        self.dropout = float(weights.get("dropout", self.dropout))
        for weight_name, weight_values in validated_weights.items():
            setattr(self, weight_name, weight_values)
        self._init_adam_states()
        self.adam_step = 0
        _LOGGER.debug(
            "TinyLSTM weights loaded: layers=(%d,%d), outputs=%d, heads=%d",
            self.hidden1,
            self.hidden2,
            self.num_outputs,
            self.num_heads,
        )

    def get_model_size_kb(self) -> float:
        element_count = 0
        for weight_name in self._weight_names():
            element_count += getattr(self, weight_name).size
        return element_count * 8 / 1024
