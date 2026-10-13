# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import json
import logging
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from .precision_tracker import PrecisionTracker, CloudType
from ..const import EXPERT_WEIGHT_KEYS, LEARNING_SMOOTHING_FACTOR
from ..file_io import async_write_json


class WeatherLearner:

    def _calculate_expert_accuracy_from_records(
        self, cloud_type: str
    ) -> Dict[str, ExpertAccuracy]:
        """Calculate expert accuracy from recent deviations."""
        _var_var_22 = {}
        if hasattr(self._tracker, "iter_rolling_records"):
            _var_var_9 = self._tracker.iter_rolling_records()
        else:
            _var_var_9 = getattr(self._tracker, "_rolling_records", [])
        for _var_var_23 in _var_var_9:
            if _var_var_23.cloud_type != cloud_type:
                continue
            if _var_var_23.expert not in EXPERT_WEIGHT_KEYS:
                continue
            _var_var_22.setdefault(_var_var_23.expert, []).append(_var_var_23)
        _var_var_24 = {}
        for _var_var_25, _var_var_26 in _var_var_22.items():
            if len(_var_var_26) < self.MIN_SAMPLES_FOR_LEARNING:
                continue
            _var_var_27 = 0.0
            _var_var_28 = 0.0
            _var_var_29 = []
            for _var_var_30, _var_var_31 in self.FIELD_IMPORTANCE.items():
                _var_var_32 = [
                    abs(float(_var_var_23.deviations[_var_var_30]))
                    for _var_var_23 in _var_var_26
                    if _var_var_30 in _var_var_23.deviations
                ]
                if len(_var_var_32) < 3:
                    continue
                _var_var_33 = statistics.mean(_var_var_32)
                _var_var_34 = self._normalize_error(_var_var_30, _var_var_33)
                _var_var_27 += _var_var_34 * _var_var_31
                _var_var_28 += _var_var_31
                _var_var_29.extend(_var_var_32)
            if _var_var_28 == 0:
                continue
            _var_var_35 = _var_var_27 / _var_var_28
            _var_var_36 = max(0.0, min(100.0, 100.0 - _var_var_35))
            _var_var_24[_var_var_25] = ExpertAccuracy(
                expert_name=_var_var_25,
                sample_count=len(_var_var_26),
                mean_error=round(_var_var_35, 2),
                std_error=(
                    round(statistics.stdev(_var_var_29), 2)
                    if len(_var_var_29) > 1
                    else 0.0
                ),
                accuracy_score=round(_var_var_36, 2),
                last_updated=_utc_now().isoformat(),
            )
            continue
        return _var_var_24
