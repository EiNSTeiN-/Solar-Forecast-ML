# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from homeassistant.core import HomeAssistant, ServiceCall
from ..core.core_helpers import SafeDateTimeUtil as dt_util


class EmergencyOutageService:

    async def _invalidate_learning(self, payload: dict[str, Any]) -> dict[str, int]:
        _var_var_3 = self.db_manager
        _var_var_34 = {}
        _var_var_7 = payload["start_date"]
        _var_var_8 = payload["end_date"]
        _var_var_35 = [
            (
                "physics_calibration_history",
                "DELETE FROM physics_calibration_history WHERE date BETWEEN ? AND ?",
                (_var_var_7, _var_var_8),
            ),
            (
                "shadow_learning_history",
                "DELETE FROM shadow_learning_history WHERE date BETWEEN ? AND ?",
                (_var_var_7, _var_var_8),
            ),
            (
                "drift_events",
                "DELETE FROM drift_events WHERE event_date BETWEEN ? AND ?",
                (_var_var_7, _var_var_8),
            ),
            ("drift_metrics_rolling", "DELETE FROM drift_metrics_rolling", ()),
            ("drift_metrics_bucket", "DELETE FROM drift_metrics_bucket", ()),
            ("drift_cusum_state", "DELETE FROM drift_cusum_state", ()),
            (
                "method_performance_learning",
                "DELETE FROM method_performance_learning",
                (),
            ),
            ("group_method_performance", "DELETE FROM group_method_performance", ()),
            (
                "weather_regime_performance_learning",
                "DELETE FROM weather_regime_performance_learning",
                (),
            ),
            (
                "physics_calibration_hourly",
                "DELETE FROM physics_calibration_hourly",
                (),
            ),
            (
                "physics_calibration_buckets",
                "DELETE FROM physics_calibration_buckets",
                (),
            ),
            (
                "physics_calibration_bucket_hourly",
                "DELETE FROM physics_calibration_bucket_hourly",
                (),
            ),
            ("ai_lstm_weights", "DELETE FROM ai_lstm_weights", ()),
            ("ai_lstm_meta", "DELETE FROM ai_lstm_meta", ()),
            ("ai_ridge_weights", "DELETE FROM ai_ridge_weights", ()),
            ("ai_ridge_meta", "DELETE FROM ai_ridge_meta", ()),
            ("ai_ridge_normalization", "DELETE FROM ai_ridge_normalization", ()),
            ("ai_learned_weights_meta", "DELETE FROM ai_learned_weights_meta", ()),
        ]
        async with _var_var_3.transaction():
            for _var_var_36, _var_var_37, _var_var_20 in _var_var_35:
                try:
                    _var_var_38 = await self._table_count(_var_var_36)
                    await _var_var_3.execute(
                        _var_var_37, _var_var_20, auto_commit=False
                    )
                    _var_var_39 = await self._table_count(_var_var_36)
                    _var_var_34[_var_var_36] = max(0, _var_var_38 - _var_var_39)
                except Exception as _var_var_2:
                    _LOGGER.debug(
                        "Emergency invalidation skipped for %s: %s",
                        _var_var_36,
                        _var_var_2,
                    )
                    _var_var_34[_var_var_36] = 0
                    continue
        return _var_var_34 if True else None
