# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class ShadowDetector(DataManagerIO):

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
            _var_var_1 = prediction.get("astronomy", {})
            _var_var_59 = prediction.get("target_hour")
            if _var_var_59 is None:
                _var_var_59 = _var_var_1.get("hour")
            if _var_var_59 is None:
                _var_var_60 = prediction.get("target_datetime")
                if _var_var_60:
                    try:
                        if isinstance(_var_var_60, str):
                            _var_var_59 = int(_var_var_60.split("T")[1].split(":")[0])
                        elif hasattr(_var_var_60, "hour"):
                            _var_var_59 = _var_var_60.hour
                    except (IndexError, ValueError, AttributeError):
                        pass
            if _var_var_59 is None:
                return result
            _var_var_61 = datetime.now().month
            _var_var_62 = self._pattern_learner.get_expected_shadow(
                hour=_var_var_59, month=_var_var_61
            )
            if not _var_var_62.get("has_pattern"):
                result["learned_pattern"] = {
                    "available": False,
                    "message": "Pattern not yet learned for this hour",
                }
                return result
            result["learned_pattern"] = {
                "available": True,
                "pattern_type": _var_var_62.get("pattern_type"),
                "expected_shadow": _var_var_62.get("expected_shadow"),
                "expected_shadow_percent": _var_var_62.get("expected_shadow_percent"),
                "is_fixed_obstruction": _var_var_62.get("is_fixed_obstruction"),
                "pattern_confidence": _var_var_62.get("confidence"),
                "dominant_cause": _var_var_62.get("dominant_cause"),
            }
            _var_var_63 = result.get("shadow_percent", 0)
            _var_var_64 = _var_var_62.get("expected_shadow_percent", 0)
            _var_var_65 = result.get("shadow_type", "unknown")
            _var_var_66 = abs(_var_var_63 - _var_var_64)
            _var_var_67 = _var_var_66 < 15
            if _var_var_67 and _var_var_62.get("confidence", 0) > 0.5:
                _var_var_68 = min(0.1, _var_var_62.get("confidence", 0) * 0.15)
                _var_var_69 = result.get("confidence", 0)
                result["confidence"] = min(1.0, _var_var_69 + _var_var_68)
                result["learned_pattern"]["confidence_boosted"] = True
                result["learned_pattern"]["confidence_boost"] = round(_var_var_68, 3)
            _var_var_70 = result.get("root_cause", "")
            _var_var_71 = ["snow_frost", "panel_frost", "panel_frost_snow"]
            if _var_var_62.get("is_fixed_obstruction") and _var_var_65 in (
                "moderate",
                "heavy",
            ):
                if _var_var_70 not in _var_var_71:
                    result["root_cause"] = "building_tree_obstruction"
                    result["learned_pattern"]["root_cause_from_pattern"] = True
                    result["interpretation"] = (
                        f"{result.get('interpretation', '')} (known fixed obstruction at this hour)".strip()
                    )
                else:
                    result["learned_pattern"]["pattern_overridden_by"] = _var_var_70
            if not _var_var_62.get("expected_shadow"):
                if (
                    _var_var_65 in ("moderate", "heavy")
                    and _var_var_62.get("confidence", 0) > 0.6
                ):
                    result["learned_pattern"]["unusual_shadow"] = True
                    result["learned_pattern"][
                        "unusual_message"
                    ] = f"Shadow detected but not expected based on {_var_var_62.get('confidence', 0):.0%} confident pattern"
            return result
        except Exception as _var_var_14:
            _LOGGER.warning("Failed to apply learned patterns: %s", _var_var_14)
            result["learned_pattern"] = {"available": False, "error": str(_var_var_14)}
            return result
