# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..const import (
    CURTAILMENT_BATTERY_SOC_MIN,
    CURTAILMENT_CAP_POTENTIAL_MARGIN,
    CURTAILMENT_CAP_RATIO_MAX,
    CURTAILMENT_CAP_TOLERANCE_RATIO,
    CURTAILMENT_CAP_TOLERANCE_W,
    CURTAILMENT_CAP_TRANSITION_RATIO_MAX,
    CURTAILMENT_FLOW_HARD_MIN_SAMPLES,
    CURTAILMENT_FLOW_HARD_SHARE,
    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
    CURTAILMENT_FLOW_HOUSE_MATCH_SHARE,
    CURTAILMENT_FLOW_LOW_BATTERY_W,
    CURTAILMENT_FLOW_MIN_HOUSE_W,
    CURTAILMENT_FLOW_MIN_SAMPLES,
    CURTAILMENT_FLOW_TRANSITION_SHARE,
    CURTAILMENT_QUARANTINE_CLOUDS_MAX,
    CURTAILMENT_QUARANTINE_GHI_MIN,
    CURTAILMENT_QUARANTINE_RATIO_MAX,
    DOMAIN,
    MPPT_CLEAR_SKY_CLOUDS_MAX,
    MPPT_CLEAR_SKY_GHI_THRESHOLD,
    MPPT_THROTTLE_BATTERY_POWER_THRESHOLD,
    MPPT_THROTTLE_PRODUCTION_RATIO,
    THROTTLE_REASON_DEMAND_LIMITED_ZERO_EXPORT,
    THROTTLE_REASON_EXPORT_LIMIT,
    THROTTLE_REASON_FULL_BATTERY,
    THROTTLE_REASON_SUSPECTED_BATTERY_CURTAILMENT,
    THROTTLE_REASON_TRANSITION_CURTAILMENT,
    THROTTLE_REASON_ZERO_EXPORT,
)
from ..data.data_shadow_detection import ShadowDetector
from ..data.data_ensemble_shadow import EnsembleShadowHandler
from ..data.data_model_candidate_shadow import ModelCandidateShadowHandler
from ..data.data_weather_radiation_ensemble import morning_day_lock_allowed
from ..data.data_learning_filter import (
    CURTAILMENT_THROTTLE_REASON_SQL,
    LEARNABLE_WEATHER_MISS_ALERT_TYPES,
    WEATHER_ALERT_UNEXPECTED_SUN,
    WEATHER_MISS_CLASS_FORECAST_MISS,
    WEATHER_MISS_CLASS_LOCAL_EVENT,
    WEATHER_MISS_CLASS_NON_LEARNABLE_WEATHER,
    WEATHER_MISS_CLASS_TECHNICAL,
    WEATHER_MISS_CLASS_UNKNOWN,
    production_miss_uplift,
    throttle_reason_excludes_from_learning,
    weather_alert_excludes_from_clean_evaluation,
    weather_alert_excludes_from_learning,
    should_exclude_group_from_learning,
)
from ..core.core_panel_group_forecast import (
    normalize_group_predictions_to_total,
    sync_panel_group_prediction_map,
)


class MorningRoutineHandler:

    async def _verify_integrity(self, date: str, created_dates: set = None) -> bool:
        """Verify data integrity after creation. @zara"""
        try:
            if created_dates:
                _var_var_123 = sorted(created_dates)
            else:
                from datetime import timedelta as _var_var_124

                _var_var_125 = datetime.fromisoformat(date).date()
                _var_var_123 = [
                    (_var_var_125 + _var_var_124(days=_var_var_126)).isoformat()
                    for _var_var_126 in range(3)
                ]
            for _var_var_127 in _var_var_123:
                _var_var_35 = await self.db.fetchone(
                    "SELECT COUNT(*) FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_127,),
                )
                if not _var_var_35 or _var_var_35[0] == 0:
                    _LOGGER.error(
                        "No predictions found for %s after creation!", _var_var_127
                    )
                    return False
                _var_var_128 = await self.db.fetchone(
                    "SELECT prediction_id, COUNT(*) as cnt\n                       FROM hourly_predictions\n                       WHERE target_date = ?\n                       GROUP BY prediction_id\n                       HAVING cnt > 1",
                    (_var_var_127,),
                )
                if _var_var_128:
                    _LOGGER.error(
                        "Duplicate prediction IDs found for %s: %s",
                        _var_var_127,
                        _var_var_128[0],
                    )
                    return False
            _LOGGER.debug(
                "Integrity verified for %d days starting from %s, no duplicates",
                len(_var_var_123),
                _var_var_123[0] if _var_var_123 else date,
            )
            return True
        except Exception as _var_var_11:
            _LOGGER.error(
                "Integrity verification failed: %s", _var_var_11, exc_info=True
            )
            return False
