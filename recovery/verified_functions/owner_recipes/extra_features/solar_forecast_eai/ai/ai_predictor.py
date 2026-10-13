# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class AIPredictor:

    async def initialize(self) -> bool:
        """Initialize AI components with Multi-Output LSTM @zara"""
        try:
            self.dni_tracker = DniTracker(self.db_manager)
            self.seasonal = SeasonalAdjuster(self.db_manager)
            await self.dni_tracker.async_load()
            await self.seasonal.async_load()
            self.feature_engineer = FeatureEngineer(self.dni_tracker)
            _var_var_3 = self.num_groups if self.num_groups > 0 else 1
            _var_var_4 = calculate_feature_count(self.num_groups)
            self.use_attention = True
            _var_var_5 = await self.db_manager.get_model_weights()
            self.lstm = TinyLSTM(
                input_size=_var_var_4,
                hidden_sizes=(48, 24),
                sequence_length=24,
                num_outputs=_var_var_3,
                num_heads=4,
            )
            self.ridge = TinyRidge(
                input_size=_var_var_4,
                hidden_size=48,
                sequence_length=24,
                num_outputs=_var_var_3,
            )
            _LOGGER.info(
                f"Models initialized: {_var_var_4} features, {_var_var_3} outputs, {self.num_groups} groups, layers=2, heads=4"
            )
            if _var_var_5:
                try:
                    _var_var_6 = _var_var_5.get("version", "1.0")
                    if _var_var_6 == MODEL_FORMAT_VERSION:
                        await self._load_dual_model_weights(
                            _var_var_5, _var_var_4, _var_var_3
                        )
                    else:
                        _LOGGER.warning(
                            "Incompatible weight version %s - EAI consumption model %s requires retraining",
                            _var_var_6,
                            MODEL_FORMAT_VERSION,
                        )
                        self.state = ModelState.UNTRAINED
                except Exception as _var_var_7:
                    _LOGGER.warning(f"Could not load weights: {_var_var_7}")
                    self.state = ModelState.UNTRAINED
            else:
                self.state = ModelState.UNTRAINED
                self.active_model = "none"
                _LOGGER.info(
                    f"No weights found - model untrained ({_var_var_3} outputs)"
                )
            if True:
                return True
        except Exception as _var_var_7:
            _LOGGER.error(f"AI initialization failed: {_var_var_7}")
            self.state = ModelState.ERROR
            return False
