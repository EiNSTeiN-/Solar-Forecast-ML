# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Stacked LSTM Neural Network for Solar Forecast Energy AI V16.8.0 - AI V3.0.

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
        self.pe = self._build_positional_encoding()
        layer1_concat_width = input_size + self.hidden1
        layer1_init_limit = np.sqrt(6 / (input_size + self.hidden1))
        self.Wf = np.random.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_concat_width)
        )
        self.Wi = np.random.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_concat_width)
        )
        self.Wc = np.random.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_concat_width)
        )
        self.Wo = np.random.uniform(
            -layer1_init_limit, layer1_init_limit, (self.hidden1, layer1_concat_width)
        )
        self.bf = np.ones((self.hidden1, 1))
        self.bi = np.zeros((self.hidden1, 1))
        self.bc = np.zeros((self.hidden1, 1))
        self.bo = np.zeros((self.hidden1, 1))
        self.ln_gamma = np.ones((self.hidden1, 1))
        self.ln_beta = np.zeros((self.hidden1, 1))
        layer2_concat_width = self.hidden1 + self.hidden2
        layer2_init_limit = np.sqrt(6 / (self.hidden1 + self.hidden2))
        self.Wf2 = np.random.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_concat_width)
        )
        self.Wi2 = np.random.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_concat_width)
        )
        self.Wc2 = np.random.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_concat_width)
        )
        self.Wo2 = np.random.uniform(
            -layer2_init_limit, layer2_init_limit, (self.hidden2, layer2_concat_width)
        )
        self.bf2 = np.ones((self.hidden2, 1))
        self.bi2 = np.zeros((self.hidden2, 1))
        self.bc2 = np.zeros((self.hidden2, 1))
        self.bo2 = np.zeros((self.hidden2, 1))
        attention_init_limit = np.sqrt(6 / (self.hidden2 + self.hidden2))
        self.W_query = np.random.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_key = np.random.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_value = np.random.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.W_attn_out = np.random.uniform(
            -attention_init_limit, attention_init_limit, (self.hidden2, self.hidden2)
        )
        self.b_attn_out = np.zeros((self.hidden2, 1))
        output_init_limit = np.sqrt(6 / (self.hidden2 + num_outputs))
        self.Wy = np.random.uniform(
            -output_init_limit, output_init_limit, (num_outputs, self.hidden2)
        )
        self.by = np.zeros((num_outputs, 1))
        self.beta1 = 0.9
        self.beta2 = 0.999
        self.epsilon = 1e-08
        self._init_adam_states()
        _LOGGER.info(
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
        positional_encoding = np.zeros((self.sequence_length, self.input_size))
        for position_index in range(self.sequence_length):
            for feature_index in range(0, self.input_size, 2):
                position_frequency = math.pow(
                    10000, 2 * feature_index / self.input_size
                )
                positional_encoding[position_index, feature_index] = math.sin(
                    position_index / position_frequency
                )
                if feature_index + 1 < self.input_size:
                    positional_encoding[position_index, feature_index + 1] = math.cos(
                        position_index / position_frequency
                    )
        return positional_encoding

    def _init_adam_states(self):
        np = _ensure_numpy()
        self.m = {}
        self.v = {}
        for weight_name in self._weight_names():
            weight_array = getattr(self, weight_name)
            self.m[weight_name] = np.zeros_like(weight_array)
            self.v[weight_name] = np.zeros_like(weight_array)

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

    def _sigmoid(self, x: Any) -> Any:
        np = _ensure_numpy()
        clipped_input = np.clip(x, -500, 500)
        return 1 / (1 + np.exp(-clipped_input))

    def _tanh(self, x: Any) -> Any:
        np = _ensure_numpy()
        return np.tanh(np.clip(x, -500, 500))

    def _layer_norm_forward(self, x: Any) -> Tuple[Any, Dict[str, Any]]:
        np = _ensure_numpy()
        input_mean = np.mean(x, axis=0, keepdims=True)
        input_variance = np.var(x, axis=0, keepdims=True)
        normalization_epsilon = 1e-05
        normalized_input = (x - input_mean) / np.sqrt(
            input_variance + normalization_epsilon
        )
        normalized_output = self.ln_gamma * normalized_input + self.ln_beta
        forward_cache = {
            "x": x,
            "x_norm": normalized_input,
            "mean": input_mean,
            "var": input_variance,
            "eps": normalization_epsilon,
        }
        return (normalized_output, forward_cache)

    def _layer_norm_backward(
        self, dy: Any, cache: Dict[str, Any]
    ) -> Tuple[Any, Any, Any]:
        np = _ensure_numpy()
        normalized_input = cache["x_norm"]
        input_variance = cache["var"]
        normalization_epsilon = cache["eps"]
        feature_count = cache["x"].shape[0]
        gamma_gradient = dy * normalized_input
        beta_gradient = dy.copy()
        normalized_gradient = dy * self.ln_gamma
        inverse_stddev = 1.0 / np.sqrt(input_variance + normalization_epsilon)
        gradient_sum = np.sum(normalized_gradient, axis=0, keepdims=True)
        normalized_gradient_sum = np.sum(
            normalized_gradient * normalized_input, axis=0, keepdims=True
        )
        input_gradient = (
            inverse_stddev
            / feature_count
            * (
                feature_count * normalized_gradient
                - gradient_sum
                - normalized_input * normalized_gradient_sum
            )
        )
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
        concatenated_input = np.vstack([h_prev, xt])
        forget_gate = self._sigmoid(np.dot(W_f, concatenated_input) + b_f)
        input_gate = self._sigmoid(np.dot(W_i, concatenated_input) + b_i)
        cell_candidate = self._tanh(np.dot(W_c, concatenated_input) + b_c)
        output_gate = self._sigmoid(np.dot(W_o, concatenated_input) + b_o)
        cell_state = forget_gate * c_prev + input_gate * cell_candidate
        hidden_state = output_gate * self._tanh(cell_state)
        forward_cache = {
            "concat": concatenated_input,
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
        concatenated_input = cache["concat"]
        forget_gate, input_gate, cell_candidate, output_gate = (
            cache["ft"],
            cache["it"],
            cache["ct_hat"],
            cache["ot"],
        )
        previous_cell_state, cell_state = (cache["c_prev"], cache["ct"])
        output_gate_gradient = (
            dh_next * self._tanh(cell_state) * output_gate * (1 - output_gate)
        )
        cell_gradient = (
            dh_next * output_gate * (1 - self._tanh(cell_state) ** 2) + dc_next
        )
        forget_gate_gradient = (
            cell_gradient * previous_cell_state * forget_gate * (1 - forget_gate)
        )
        input_gate_gradient = (
            cell_gradient * cell_candidate * input_gate * (1 - input_gate)
        )
        candidate_gradient = cell_gradient * input_gate * (1 - cell_candidate**2)
        gradients = {
            "Wf": np.dot(forget_gate_gradient, concatenated_input.T),
            "Wi": np.dot(input_gate_gradient, concatenated_input.T),
            "Wc": np.dot(candidate_gradient, concatenated_input.T),
            "Wo": np.dot(output_gate_gradient, concatenated_input.T),
            "bf": forget_gate_gradient,
            "bi": input_gate_gradient,
            "bc": candidate_gradient,
            "bo": output_gate_gradient,
        }
        concatenated_gradient = (
            np.dot(W_f.T, forget_gate_gradient)
            + np.dot(W_i.T, input_gate_gradient)
            + np.dot(W_c.T, candidate_gradient)
            + np.dot(W_o.T, output_gate_gradient)
        )
        previous_hidden_gradient = concatenated_gradient[:hidden_size, :]
        input_gradient = concatenated_gradient[hidden_size:, :]
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
        hidden_state_matrix = np.hstack(hidden_states)
        query_projection = np.dot(self.W_query, query)
        key_projection = np.dot(self.W_key, hidden_state_matrix)
        value_projection = np.dot(self.W_value, hidden_state_matrix)
        sequence_steps = len(hidden_states)
        all_head_context = np.zeros((self.hidden2, 1))
        head_caches = []
        for head_index in range(self.num_heads):
            head_start = head_index * self.head_dim
            head_stop = head_start + self.head_dim
            query_head = query_projection[head_start:head_stop, :]
            key_head = key_projection[head_start:head_stop, :]
            value_head = value_projection[head_start:head_stop, :]
            attention_scores = np.dot(query_head.T, key_head) / np.sqrt(self.head_dim)
            maximum_score = np.max(attention_scores, axis=1, keepdims=True)
            score_exponentials = np.exp(
                np.clip(attention_scores - maximum_score, -500, 500)
            )
            attention_weights = score_exponentials / (
                np.sum(score_exponentials, axis=1, keepdims=True) + 1e-10
            )
            head_context = np.dot(value_head, attention_weights.T)
            all_head_context[head_start:head_stop, :] = head_context
            head_caches.append(
                {
                    "Q_h": query_head,
                    "K_h": key_head,
                    "V_h": value_head,
                    "scores": attention_scores,
                    "attn_w": attention_weights,
                    "context_h": head_context,
                }
            )
            continue
        attention_output = np.dot(self.W_attn_out, all_head_context) + self.b_attn_out
        attention_output = attention_output + query
        flattened_attention_weights = np.concatenate(
            [head_cache["attn_w"].flatten() for head_cache in head_caches]
        )
        forward_cache = {
            "query": query,
            "H": hidden_state_matrix,
            "Q": query_projection,
            "K": key_projection,
            "V": value_projection,
            "all_context": all_head_context,
            "head_caches": head_caches,
            "seq_len": sequence_steps,
        }
        return (attention_output, flattened_attention_weights, forward_cache)

    def _multi_head_attention_backward(
        self, dout: Any, cache: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any], Any]:
        np = _ensure_numpy()
        query_input = cache["query"]
        hidden_state_matrix = cache["H"]
        query_projection = cache["Q"]
        key_projection = cache["K"]
        value_projection = cache["V"]
        all_head_context = cache["all_context"]
        head_caches = cache["head_caches"]
        residual_query_gradient = dout.copy()
        attention_output_gradient = dout.copy()
        gradients = {}
        gradients["W_attn_out"] = np.dot(attention_output_gradient, all_head_context.T)
        gradients["b_attn_out"] = attention_output_gradient.copy()
        context_gradient = np.dot(self.W_attn_out.T, attention_output_gradient)
        query_projection_gradient = np.zeros_like(query_projection)
        key_projection_gradient = np.zeros_like(key_projection)
        value_projection_gradient = np.zeros_like(value_projection)
        for head_index in range(self.num_heads):
            head_start = head_index * self.head_dim
            head_stop = head_start + self.head_dim
            head_cache = head_caches[head_index]
            head_context_gradient = context_gradient[head_start:head_stop, :]
            head_value_gradient = np.dot(head_context_gradient, head_cache["attn_w"])
            attention_weight_gradient = np.dot(
                head_cache["V_h"].T, head_context_gradient
            ).T
            weighted_gradient_mean = np.sum(
                head_cache["attn_w"] * attention_weight_gradient, axis=1, keepdims=True
            )
            score_gradient = head_cache["attn_w"] * (
                attention_weight_gradient - weighted_gradient_mean
            )
            scaled_score_gradient = score_gradient / np.sqrt(self.head_dim)
            head_query_gradient = np.dot(head_cache["K_h"], scaled_score_gradient.T)
            head_key_gradient = np.dot(head_cache["Q_h"], scaled_score_gradient)
            query_projection_gradient[head_start:head_stop, :] += head_query_gradient
            key_projection_gradient[head_start:head_stop, :] += head_key_gradient
            value_projection_gradient[head_start:head_stop, :] += head_value_gradient
            continue
        gradients["W_query"] = np.dot(query_projection_gradient, query_input.T)
        gradients["W_key"] = np.dot(key_projection_gradient, hidden_state_matrix.T)
        gradients["W_value"] = np.dot(value_projection_gradient, hidden_state_matrix.T)
        query_input_gradient = np.dot(self.W_query.T, query_projection_gradient)
        total_query_gradient = residual_query_gradient + query_input_gradient
        hidden_state_gradient = np.dot(self.W_key.T, key_projection_gradient) + np.dot(
            self.W_value.T, value_projection_gradient
        )
        return (total_query_gradient, gradients, hidden_state_gradient)

    def forward(
        self, X: Any, training: bool = False
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        np = _ensure_numpy()
        if training and self.noise_std > 0:
            X = X + np.random.randn(*X.shape) * self.noise_std
        positioned_input = X + self.pe[: X.shape[0], : X.shape[1]]
        layer1_hidden_state = np.zeros((self.hidden1, 1))
        layer1_cell_state = np.zeros((self.hidden1, 1))
        layer2_hidden_state = np.zeros((self.hidden2, 1))
        layer2_cell_state = np.zeros((self.hidden2, 1))
        layer1_caches = []
        layer2_caches = []
        normalization_caches = []
        layer2_hidden_states = []
        for step_index in range(positioned_input.shape[0]):
            input_column = positioned_input[step_index : step_index + 1].T
            layer1_hidden_state, layer1_cell_state, layer1_cache = (
                self._lstm_cell_forward(
                    input_column,
                    layer1_hidden_state,
                    layer1_cell_state,
                    self.Wf,
                    self.Wi,
                    self.Wc,
                    self.Wo,
                    self.bf,
                    self.bi,
                    self.bc,
                    self.bo,
                )
            )
            if training and self.dropout > 0:
                layer1_dropout_mask = (
                    np.random.rand(*layer1_hidden_state.shape) > self.dropout
                ).astype(float)
                layer1_dropout_mask /= 1 - self.dropout
                layer1_hidden_state = layer1_hidden_state * layer1_dropout_mask
                layer1_cache["dropout_mask"] = layer1_dropout_mask
            normalized_layer1_hidden, normalization_cache = self._layer_norm_forward(
                layer1_hidden_state
            )
            layer2_hidden_state, layer2_cell_state, layer2_cache = (
                self._lstm_cell_forward(
                    normalized_layer1_hidden,
                    layer2_hidden_state,
                    layer2_cell_state,
                    self.Wf2,
                    self.Wi2,
                    self.Wc2,
                    self.Wo2,
                    self.bf2,
                    self.bi2,
                    self.bc2,
                    self.bo2,
                )
            )
            if training and self.dropout > 0:
                layer2_dropout_mask = (
                    np.random.rand(*layer2_hidden_state.shape) > self.dropout
                ).astype(float)
                layer2_dropout_mask /= 1 - self.dropout
                layer2_hidden_state = layer2_hidden_state * layer2_dropout_mask
                layer2_cache["dropout_mask"] = layer2_dropout_mask
            if training:
                layer1_caches.append(layer1_cache)
                layer2_caches.append(layer2_cache)
                normalization_caches.append(normalization_cache)
            layer2_hidden_states.append(layer2_hidden_state.copy())
            continue
        attention_hidden_state, flattened_attention_weights, attention_cache = (
            self._multi_head_attention_forward(
                layer2_hidden_state, layer2_hidden_states
            )
        )
        prediction = np.dot(self.Wy, attention_hidden_state) + self.by
        if training:
            return (
                prediction,
                {
                    "caches1": layer1_caches,
                    "caches2": layer2_caches,
                    "ln_caches": normalization_caches,
                    "final_h2": layer2_hidden_state,
                    "hidden_states_2": layer2_hidden_states,
                    "attn_cache": attention_cache,
                    "attn_weights": flattened_attention_weights,
                    "h_attn": attention_hidden_state,
                },
            )
        return (prediction, None)

    def backward(self, dy: Any, cache: Dict[str, Any]) -> Dict[str, Any]:
        np = _ensure_numpy()
        layer1_caches = cache["caches1"]
        layer2_caches = cache["caches2"]
        normalization_caches = cache["ln_caches"]
        attention_hidden_state = cache["h_attn"]
        attention_cache = cache["attn_cache"]
        output_hidden_gradient = np.dot(self.Wy.T, dy)
        all_gradients = {
            initial_weight_name: np.zeros_like(getattr(self, initial_weight_name))
            for initial_weight_name in self._weight_names()
        }
        all_gradients["Wy"] = np.dot(dy, attention_hidden_state.T)
        all_gradients["by"] = dy.copy()
        layer2_hidden_gradient, attention_gradients, sequence_hidden_gradients = (
            self._multi_head_attention_backward(output_hidden_gradient, attention_cache)
        )
        for weight_name in attention_gradients:
            all_gradients[weight_name] = attention_gradients[weight_name]
        layer2_cell_gradient = np.zeros((self.hidden2, 1))
        layer1_cell_gradient = np.zeros((self.hidden1, 1))
        next_layer1_hidden_gradient = np.zeros((self.hidden1, 1))
        for step_index in reversed(range(len(layer2_caches))):
            layer2_cache = layer2_caches[step_index]
            layer2_hidden_gradient = (
                layer2_hidden_gradient
                + sequence_hidden_gradients[:, step_index : step_index + 1]
            )
            if "dropout_mask" in layer2_cache:
                layer2_hidden_gradient = (
                    layer2_hidden_gradient * layer2_cache["dropout_mask"]
                )
            (
                previous_layer2_hidden_gradient,
                previous_layer2_cell_gradient,
                normalized_layer1_gradient,
                layer2_weight_gradients,
            ) = self._lstm_cell_backward(
                layer2_hidden_gradient,
                layer2_cell_gradient,
                layer2_cache,
                self.Wf2,
                self.Wi2,
                self.Wc2,
                self.Wo2,
                self.hidden2,
            )
            for weight_name in ("Wf", "Wi", "Wc", "Wo", "bf", "bi", "bc", "bo"):
                all_gradients[weight_name + "2"] += layer2_weight_gradients[weight_name]
            normalized_input_gradient, gamma_gradient, beta_gradient = (
                self._layer_norm_backward(
                    normalized_layer1_gradient, normalization_caches[step_index]
                )
            )
            all_gradients["ln_gamma"] += gamma_gradient
            all_gradients["ln_beta"] += beta_gradient
            layer1_hidden_gradient = (
                normalized_input_gradient + next_layer1_hidden_gradient
            )
            layer1_cache = layer1_caches[step_index]
            if "dropout_mask" in layer1_cache:
                layer1_hidden_gradient = (
                    layer1_hidden_gradient * layer1_cache["dropout_mask"]
                )
            (
                next_layer1_hidden_gradient,
                previous_layer1_cell_gradient,
                input_gradient,
                layer1_weight_gradients,
            ) = self._lstm_cell_backward(
                layer1_hidden_gradient,
                layer1_cell_gradient,
                layer1_cache,
                self.Wf,
                self.Wi,
                self.Wc,
                self.Wo,
                self.hidden1,
            )
            for weight_name in ("Wf", "Wi", "Wc", "Wo", "bf", "bi", "bc", "bo"):
                all_gradients[weight_name] += layer1_weight_gradients[weight_name]
            layer2_hidden_gradient = previous_layer2_hidden_gradient
            layer2_cell_gradient = previous_layer2_cell_gradient
            layer1_cell_gradient = previous_layer1_cell_gradient
            continue
        gradient_clip_norm = 5.0
        global_gradient_norm = np.sqrt(
            sum((np.sum(gradient**2) for gradient in all_gradients.values()))
        )
        if global_gradient_norm > gradient_clip_norm:
            for weight_name in all_gradients:
                all_gradients[weight_name] *= gradient_clip_norm / global_gradient_norm
        return all_gradients

    def _cosine_lr(self, epoch: int, max_epochs: int) -> float:
        return self.lr_min + 0.5 * (self.lr_max - self.lr_min) * (
            1 + math.cos(math.pi * epoch / max_epochs)
        )

    def _update_weights_adam(self, grads: Dict[str, Any], t: int):
        np = _ensure_numpy()
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
            weight_array = getattr(self, weight_name)
            weight_array -= (
                self.learning_rate
                * corrected_first_moment
                / (np.sqrt(corrected_second_moment) + self.epsilon)
            )
            setattr(self, weight_name, weight_array)
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
                converted_targets = np.array(
                    [[float(target_value)] for target_value in y]
                )
                if converted_targets.shape[0] != self.num_outputs:
                    if converted_targets.shape[0] < self.num_outputs:
                        converted_targets = np.vstack(
                            [
                                converted_targets,
                                np.zeros(
                                    (self.num_outputs - converted_targets.shape[0], 1)
                                ),
                            ]
                        )
                    else:
                        converted_targets = converted_targets[: self.num_outputs]
                return converted_targets if True else None
            return np.array([[0.0]] * self.num_outputs) if True else None

        sample_count = len(X_sequences)
        validation_sample_count = int(sample_count * validation_split)
        training_sample_count = sample_count - validation_sample_count
        if validation_sample_count < 1 and sample_count >= 2:
            validation_sample_count = 1
            training_sample_count = sample_count - 1
        elif sample_count < 2:
            validation_sample_count = 0
            training_sample_count = sample_count
        shuffled_indices = np.random.permutation(sample_count)
        training_indices = shuffled_indices[:training_sample_count]
        validation_indices = shuffled_indices[training_sample_count:]
        training_sequences = [
            X_sequences[validation_index] for validation_index in training_indices
        ]
        training_targets = [
            y_targets[validation_index] for validation_index in training_indices
        ]
        validation_sequences = [
            X_sequences[validation_index] for validation_index in validation_indices
        ]
        validation_targets = [
            y_targets[validation_index] for validation_index in validation_indices
        ]
        best_validation_loss = float("inf")
        epochs_without_improvement = 0
        has_validation = len(validation_sequences) > 0
        training_history = {"train_loss": [], "val_loss": []}
        for epoch in range(1, epochs + 1):
            self.learning_rate = self._cosine_lr(epoch, epochs)
            epoch_sample_order = np.random.permutation(training_sample_count)
            epoch_loss_sum = 0.0
            for batch_start in range(0, training_sample_count, batch_size):
                batch_end = min(batch_start + batch_size, training_sample_count)
                batch_indices = epoch_sample_order[batch_start:batch_end]
                batch_loss_sum = 0.0
                batch_gradients = None
                for sample_index in batch_indices:
                    input_sequence = np.array(training_sequences[sample_index])
                    target_array = _to_target_array(training_targets[sample_index])
                    prediction, training_cache = self.forward(
                        input_sequence, training=True
                    )
                    sample_loss = ((prediction - target_array) ** 2).mean()
                    batch_loss_sum += sample_loss
                    output_gradient = (
                        2 * (prediction - target_array) / target_array.size
                    )
                    sample_gradients = self.backward(output_gradient, training_cache)
                    if batch_gradients is None:
                        batch_gradients = sample_gradients
                    else:
                        for weight_name in batch_gradients:
                            batch_gradients[weight_name] += sample_gradients[
                                weight_name
                            ]
                if batch_gradients is None:
                    continue
                for weight_name in batch_gradients:
                    batch_gradients[weight_name] /= len(batch_indices)
                self._update_weights_adam(batch_gradients, epoch)
                epoch_loss_sum += batch_loss_sum
                await asyncio.sleep(0)
                continue
            training_loss = (
                epoch_loss_sum / training_sample_count
                if training_sample_count > 0
                else 0.0
            )
            validation_loss = 0.0
            if has_validation:
                for validation_index in range(len(validation_sequences)):
                    input_sequence = np.array(validation_sequences[validation_index])
                    target_array = _to_target_array(
                        validation_targets[validation_index]
                    )
                    prediction, unused_forward_cache = self.forward(
                        input_sequence, training=False
                    )
                    validation_loss += ((prediction - target_array) ** 2).mean()
                validation_loss /= len(validation_sequences)
            training_history["train_loss"].append(float(training_loss))
            training_history["val_loss"].append(float(validation_loss))
            if epoch % 10 == 0:
                _LOGGER.info(
                    "Epoch %d: train=%.4f, val=%.4f, lr=%.6f",
                    epoch,
                    training_loss,
                    validation_loss,
                    self.learning_rate,
                )
            if has_validation:
                if validation_loss < best_validation_loss:
                    best_validation_loss = validation_loss
                    epochs_without_improvement = 0
                else:
                    epochs_without_improvement += 1
                if epochs_without_improvement >= early_stopping_patience:
                    _LOGGER.info("Early stopping at epoch %d", epoch)
                    break
            if checkpoint_callback and epoch % 10 == 0:
                await checkpoint_callback(epoch, self.get_weights())
            await asyncio.sleep(0.01)
            continue
        validation_predictions = []
        validation_actuals = []
        for validation_index, input_sequence in enumerate(validation_sequences):
            prediction, unused_forward_cache = self.forward(
                np.array(input_sequence), training=False
            )
            validation_predictions.append(float(np.sum(prediction)))
            validation_target_values = _to_target_array(
                validation_targets[validation_index]
            )
            validation_actuals.append(float(np.sum(validation_target_values)))
        validation_r2 = 0.0
        validation_rmse = 0.0
        if len(validation_actuals) > 0:
            validation_target_values = np.array(validation_actuals)
            validation_prediction_values = np.array(validation_predictions)
            residual_sum_squares = np.sum(
                (validation_target_values - validation_prediction_values) ** 2
            )
            total_sum_squares = np.sum(
                (validation_target_values - np.mean(validation_target_values)) ** 2
            )
            validation_r2 = (
                1 - residual_sum_squares / total_sum_squares
                if total_sum_squares > 0
                else 0.0
            )
            validation_rmse = float(
                np.sqrt(
                    np.mean(
                        (validation_target_values - validation_prediction_values) ** 2
                    )
                )
            )
        _LOGGER.info(
            "Training complete: R2=%.3f, RMSE=%.3f kWh, outputs=%d, layers=2, heads=%d",
            validation_r2,
            validation_rmse,
            self.num_outputs,
            self.num_heads,
        )
        return {
            "success": True,
            "accuracy": float(validation_r2),
            "rmse": validation_rmse,
            "final_train_loss": float(training_history["train_loss"][-1]),
            "final_val_loss": float(training_history["val_loss"][-1]),
            "best_val_loss": float(best_validation_loss),
            "epochs_trained": epoch,
            "num_outputs": self.num_outputs,
            "history": training_history,
        }

    def predict(self, X_sequence: Any) -> List[float]:
        np = _ensure_numpy()
        input_sequence = np.array(X_sequence)
        if input_sequence.ndim == 3:
            if input_sequence.shape[0] != 1:
                raise ValueError(
                    f"Batch not supported, got shape {input_sequence.shape}"
                )
            input_sequence = input_sequence.squeeze(0)
        if input_sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D, got {input_sequence.ndim}D shape {input_sequence.shape}"
            )
        if input_sequence.shape[0] != self.sequence_length:
            raise ValueError(
                f"Sequence length: expected {self.sequence_length}, got {input_sequence.shape[0]}"
            )
        if input_sequence.shape[1] != self.input_size:
            raise ValueError(
                f"Features: expected {self.input_size}, got {input_sequence.shape[1]}"
            )
        if not np.isfinite(input_sequence).all():
            _LOGGER.warning("LSTM predict: NaN/Inf in input, returning zeros")
            return [0.0] * self.num_outputs
        prediction, unused_forward_cache = self.forward(input_sequence, training=False)
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
        weights_payload = {
            "input_size": self.input_size,
            "hidden_size": self.hidden1,
            "hidden2": self.hidden2,
            "sequence_length": self.sequence_length,
            "num_outputs": self.num_outputs,
            "has_attention": True,
            "num_layers": 2,
            "num_heads": self.num_heads,
        }
        for weight_name in self._weight_names():
            weights_payload[weight_name] = getattr(self, weight_name).tolist()
        return weights_payload

    def set_weights(self, weights: Dict[str, Any]):
        np = _ensure_numpy()
        self.input_size = weights.get("input_size", self.input_size)
        self.hidden1 = weights.get("hidden_size", self.hidden1)
        self.hidden2 = weights.get("hidden2", self.hidden2)
        self.sequence_length = weights.get("sequence_length", self.sequence_length)
        self.num_outputs = weights.get("num_outputs", self.num_outputs)
        self.num_heads = weights.get("num_heads", self.num_heads)
        self.head_dim = self.hidden2 // self.num_heads
        self.pe = self._build_positional_encoding()
        for weight_name in self._weight_names():
            if weight_name in weights:
                restored_weight_array = np.array(weights[weight_name])
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
                    restored_weight_array = restored_weight_array.reshape(-1, 1)
                setattr(self, weight_name, restored_weight_array)
        self._init_adam_states()
        _LOGGER.info(
            "Weights loaded: layers=(%d,%d), outputs=%d, heads=%d",
            self.hidden1,
            self.hidden2,
            self.num_outputs,
            self.num_heads,
        )

    def get_model_size_kb(self) -> float:
        parameter_count = 0
        for weight_name in self._weight_names():
            parameter_count += getattr(self, weight_name).size
        return parameter_count * 8 / 1024
