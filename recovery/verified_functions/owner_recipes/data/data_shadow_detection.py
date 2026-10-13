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
