# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..const import (
    ADAPTIVE_DEVIATION_THRESHOLD_PERCENT,
    ADAPTIVE_DEVIATION_MIN_THRESHOLD_KWH,
    ADAPTIVE_DEVIATION_PERCENT_OF_FORECAST,
    ADAPTIVE_MIN_REMAINING_HOURS,
    ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD,
    DOMAIN,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.data_learning_filter import WEATHER_ALERT_UNEXPECTED_SUN
from .production_forecast_regeneration import ForecastRegenerationHelper


class AdaptiveForecastEngine:

    async def _check_special_situations(
        self, predictions: List[Dict[str, Any]], analyzed_hour: Optional[int] = None
    ) -> Optional[str]:
        """
        Check if special situations are active (Snow/Weather-Alert).

        NOTE: Frost (heavy_frost) is NOT a special situation! @zara V16.1
        - Frost = cold air, panels are NOT covered
        - Cold temperatures actually IMPROVE panel efficiency
        - Only physical coverage (snow, ice) affects production

        Snow only blocks if majority of daylight hours (8-16) affected. @zara V16.4.2
        Non-snow alerts block only the hour being scored, not the whole morning.

        Returns:
            String with situation or None if none active. @zara
        """
        _var_var_41 = 0
        _var_var_42 = 0
        _var_var_43 = None
        for _var_var_30 in predictions:
            _var_var_44 = _var_var_30.get("flags", {})
            _var_var_45 = _var_var_44.get("weather_alert_type")
            _var_var_31 = _var_var_30.get("target_hour", 0)
            if 8 <= _var_var_31 <= 16:
                _var_var_42 += 1
                if _var_var_45 == "snow_covered_panels":
                    _var_var_41 += 1
            if analyzed_hour is not None and _var_var_31 != analyzed_hour:
                continue
            if (
                _var_var_45
                and _var_var_45
                not in ("snow_covered_panels", WEATHER_ALERT_UNEXPECTED_SUN)
                and _var_var_44.get("has_weather_alert")
            ):
                if _var_var_43 is None:
                    _var_var_43 = f"Weather alert: {_var_var_45}"
        if _var_var_42 > 0 and _var_var_41 > _var_var_42 * 0.5:
            return "Snow-covered panels"
        return _var_var_43
