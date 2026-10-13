# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import date, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_shadow_group_math import (
    GROUP_GLOBAL_WEATHER_RATIO_MAX as DEFAULT_GROUP_GLOBAL_WEATHER_RATIO_MAX,
    GROUP_PEER_GAP_MIN as DEFAULT_GROUP_PEER_GAP_MIN,
    GROUP_PEER_RATIO_MAX as DEFAULT_GROUP_PEER_RATIO_MAX,
    MIN_GROUP_EXPECTED_KWH as DEFAULT_MIN_GROUP_EXPECTED_KWH,
    compare_panel_group_performance,
)
from .data_shadow_observation import (
    CEILING_WINDOW_DAYS,
    OBSERVATION_NOT_EVALUABLE,
    build_geometry_ceilings,
    classify_observation_class,
    classify_sensorless_hour,
    elevation_bin,
    pyranometer_is_implausible,
)
from ..const import CURTAILMENT_THROTTLE_REASON_SQL, CURTAILMENT_THROTTLE_REASONS
from ..core.core_helpers import SafeDateTimeUtil


class ShadowDetector(DataManagerIO):

    @staticmethod
    def _first_numeric(*values: Any) -> Optional[float]:
        for _var_var_46 in values:
            if _var_var_46 is None or isinstance(_var_var_46, bool):
                continue
            try:
                _var_var_47 = float(_var_var_46)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(_var_var_47):
                continue
            return _var_var_47

    def _apply_learned_patterns(
        self, result: Dict[str, Any], prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply learned shadow patterns to enhance detection result. @zara V16.2

        Uses the ShadowPatternLearner to:
        - Boost confidence if current detection matches learned pattern
        - Add pattern context (is this a known fixed obstruction hour?)
        - Adjust root cause based on historical data

        Args:
            result: Current detection result dict
            prediction: Original prediction dict with hour info

        Returns:
            Enhanced result dict with pattern information
        """
        if not self._pattern_learner or not self._pattern_learner._initialized:
            return result
        try:
            _var_var_6 = prediction.get("astronomy") or {}
            _var_var_74 = prediction.get("target_hour")
            if _var_var_74 is None:
                _var_var_74 = _var_var_6.get("hour")
            if _var_var_74 is None:
                _var_var_75 = prediction.get("target_datetime")
                if _var_var_75:
                    try:
                        if isinstance(_var_var_75, str):
                            _var_var_74 = int(_var_var_75.split("T")[1].split(":")[0])
                        elif hasattr(_var_var_75, "hour"):
                            _var_var_74 = _var_var_75.hour
                    except (IndexError, ValueError, AttributeError):
                        pass
            if _var_var_74 is None:
                _var_var_76 = str(prediction.get("prediction_id") or "")
                if "_" in _var_var_76:
                    _var_var_77 = _var_var_76.rsplit("_", 1)[-1]
                    if _var_var_77.isdigit():
                        _var_var_74 = int(_var_var_77)
            if _var_var_74 is None:
                return result
            _var_var_78 = SafeDateTimeUtil.now().month
            _var_var_79 = self._pattern_learner.get_expected_shadow(
                hour=_var_var_74, month=_var_var_78
            )
            if not _var_var_79.get("has_pattern"):
                result["learned_pattern"] = {
                    "available": False,
                    "message": "Pattern not yet learned for this hour",
                }
                return result
            result["learned_pattern"] = {
                "available": True,
                "pattern_type": _var_var_79.get("pattern_type"),
                "expected_shadow": _var_var_79.get("expected_shadow"),
                "expected_shadow_percent": _var_var_79.get("expected_shadow_percent"),
                "is_fixed_obstruction": _var_var_79.get("is_fixed_obstruction"),
                "pattern_confidence": _var_var_79.get("confidence"),
                "dominant_cause": _var_var_79.get("dominant_cause"),
            }
            _var_var_80 = result.get("shadow_percent", 0)
            _var_var_81 = _var_var_79.get("expected_shadow_percent", 0)
            _var_var_82 = result.get("shadow_type", "unknown")
            _var_var_83 = (
                result.get("fusion_mode") == "cloud_based"
                or result.get("ensemble_mode") == "cloud_based"
            )
            _var_var_84 = abs(_var_var_80 - _var_var_81)
            _var_var_85 = _var_var_84 < 15
            if not _var_var_83:
                if _var_var_85 and _var_var_79.get("confidence", 0) > 0.5:
                    _var_var_86 = min(0.1, _var_var_79.get("confidence", 0) * 0.15)
                    _var_var_87 = result.get("confidence", 0)
                    result["confidence"] = min(1.0, _var_var_87 + _var_var_86)
                    result["learned_pattern"]["confidence_boosted"] = True
                    result["learned_pattern"]["confidence_boost"] = round(
                        _var_var_86, 3
                    )
            _var_var_88 = result.get("root_cause", "")
            _var_var_89 = ["snow_frost", "panel_frost", "panel_frost_snow"]
            if (
                _var_var_79.get("is_fixed_obstruction")
                and (not _var_var_83)
                and (_var_var_82 in ("moderate", "heavy"))
            ):
                if _var_var_88 not in _var_var_89:
                    result["root_cause"] = "building_tree_obstruction"
                    result["learned_pattern"]["root_cause_from_pattern"] = True
                    result["interpretation"] = (
                        f"{result.get('interpretation', '')} (known fixed obstruction at this hour)".strip()
                    )
                else:
                    result["learned_pattern"]["pattern_overridden_by"] = _var_var_88
            if not _var_var_79.get("expected_shadow"):
                if (
                    _var_var_82 in ("moderate", "heavy")
                    and _var_var_79.get("confidence", 0) > 0.6
                ):
                    result["learned_pattern"]["unusual_shadow"] = True
                    result["learned_pattern"][
                        "unusual_message"
                    ] = f"Shadow detected but not expected based on {_var_var_79.get('confidence', 0):.0%} confident pattern"
            return result
        except Exception as _var_var_20:
            _LOGGER.warning("Failed to apply learned patterns: %s", _var_var_20)
            result["learned_pattern"] = {"available": False, "error": str(_var_var_20)}
            return result
