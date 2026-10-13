# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from typing import Any, Dict, List, Optional
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from ..const import (
    DOMAIN,
    INTEGRATION_MODEL,
    SOFTWARE_VERSION,
    AI_VERSION,
    CACHE_HOURLY_PREDICTIONS,
    CACHE_PREDICTIONS,
    PRED_TARGET_DATE,
)


class ShadowTodaySensor(CoordinatorEntity, SensorEntity):

    def _get_today_analysis(self) -> Dict[str, Any]:
        """Calculate today's shadow analysis with caching. @zara"""
        _var_var_9 = dt_util.now().date().isoformat()
        if self._cache_date == _var_var_9 and self._cached_analysis is not None:
            return self._cached_analysis
        try:
            _var_var_24 = _get_today_predictions_from_cache(self.coordinator)
            _var_var_25 = _filter_valid_shadow_predictions(_var_var_24)
            if not _var_var_25:
                self._cached_analysis = {
                    "status": "no_data",
                    "shadow_hours": 0,
                    "hourly_breakdown": [],
                }
                self._cache_date = _var_var_9
                return self._cached_analysis
            _var_var_26 = {"none": 0, "light": 0, "moderate": 0, "heavy": 0}
            _var_var_27 = {
                "weather_clouds": 0,
                "building_tree_obstruction": 0,
                "possible_obstruction": 0,
                "low_sun_angle": 0,
                "other": 0,
            }
            _var_var_28 = {
                "weather_clouds": [],
                "building_tree_obstruction": [],
                "possible_obstruction": [],
                "low_sun_angle": [],
                "other": [],
            }
            _var_var_29 = []
            _var_var_30 = 0.0
            _var_var_31 = 0.0
            _var_var_32 = []
            _var_var_33 = None
            _var_var_34 = 0.0
            _var_var_35 = []
            _var_var_36 = 0
            _var_var_37 = []
            _var_var_38 = []
            for _var_var_39 in _var_var_25:
                _var_var_5 = _shadow_data(_var_var_39)
                _var_var_40 = _var_var_39.get("weather_actual", {})
                if not isinstance(_var_var_40, dict):
                    _var_var_40 = {}
                _var_var_17 = _shadow_type_key(_var_var_5.get("shadow_type", "unknown"))
                _var_var_41 = _root_cause_key(_var_var_5.get("root_cause", "unknown"))
                _var_var_42 = _var_var_39.get("target_hour")
                _var_var_43 = _numeric_value(_var_var_5.get("shadow_percent"))
                _var_var_44 = _var_var_43 is not None
                _var_var_45 = _var_var_40.get("snow_covered_panels", False)
                _var_var_46 = _var_var_40.get("snow_coverage_source", "none")
                _var_var_47 = _var_var_40.get("frost_detected", False)
                _var_var_48 = _var_var_40.get("frost_type")
                if _var_var_45 and _var_var_42 is not None:
                    _var_var_38.append(_var_var_42)
                if _var_var_42 is not None:
                    if not _var_var_44:
                        _var_var_35.append(_var_var_42)
                    _var_var_49 = _cause_breakdown(_var_var_41, _var_var_43)
                    _var_var_29.append(
                        {
                            "hour": _var_var_42,
                            "shadow_percent": _safe_round(_var_var_43, 1),
                            "root_cause": _translate_root_cause(_var_var_41, self.hass),
                            "shadow_type": _var_var_17,
                            "cloud_pct": _var_var_49["weather_clouds"],
                            "shadow_pct": _var_var_49["building_tree_obstruction"],
                            "sun_pct": _var_var_49["low_sun_angle"],
                            "snow_covered": _var_var_45,
                            "snow_source": _var_var_46 if _var_var_45 else None,
                            "frost_detected": _var_var_47,
                            "frost_type": _var_var_48 if _var_var_47 else None,
                            "status": "complete" if _var_var_44 else "incomplete",
                        }
                    )
                if _var_var_44:
                    _var_var_36 += 1
                if _var_var_44 and _var_var_17 in _var_var_26:
                    _var_var_26[_var_var_17] += 1
                _var_var_50 = _var_var_5.get("learned_pattern") or {}
                if (
                    isinstance(_var_var_50, dict)
                    and _var_var_50.get("is_fixed_obstruction")
                    and (_var_var_42 is not None)
                ):
                    _var_var_37.append(_var_var_42)
                if (
                    _var_var_44
                    and _var_var_17 in ("moderate", "heavy")
                    and (_var_var_42 is not None)
                ):
                    _var_var_32.append(_var_var_42)
                    if _var_var_41 in _var_var_27:
                        _var_var_27[_var_var_41] += 1
                        _var_var_28[_var_var_41].append(_var_var_42)
                    else:
                        _var_var_27["other"] += 1
                        _var_var_28["other"].append(_var_var_42)
                _var_var_51 = _var_var_5.get("loss_kwh")
                _var_var_52 = _var_var_5.get("theoretical_max_kwh")
                if isinstance(_var_var_51, (int, float)) and _var_var_51 >= 0:
                    _var_var_30 += _var_var_51
                if isinstance(_var_var_52, (int, float)) and _var_var_52 >= 0:
                    _var_var_31 += _var_var_52
                _var_var_53 = _numeric_value(_var_var_5.get("shadow_percent"))
                if isinstance(_var_var_53, (int, float)):
                    if _var_var_53 > _var_var_34:
                        _var_var_34 = _var_var_53
                        _var_var_33 = _var_var_39.get("target_hour")
                        continue
            if _var_var_31 > 0:
                _var_var_54 = _var_var_30 / _var_var_31 * 100.0
            else:
                _var_var_54 = 0.0
            self._cached_analysis = {
                "shadow_hours": float(len(_var_var_32)),
                "shadow_hours_count": len(_var_var_32),
                "shadow_hours_list": _var_var_32,
                "none_count": _var_var_26["none"],
                "light_count": _var_var_26["light"],
                "moderate_count": _var_var_26["moderate"],
                "heavy_count": _var_var_26["heavy"],
                "total_analyzed_hours": _var_var_36,
                "total_candidate_hours": len(_var_var_25),
                "incomplete_hours_count": len(_var_var_35),
                "incomplete_hours_list": sorted(_var_var_35),
                "peak_shadow_hour": _var_var_33,
                "peak_shadow_percent": round(_var_var_34, 1),
                "cumulative_loss_kwh": round(_var_var_30, 3),
                "daily_loss_percent": round(_var_var_54, 1),
                "date": _var_var_9,
                "cloud_hours": _var_var_27["weather_clouds"],
                "cloud_hours_list": _var_var_28["weather_clouds"],
                "real_shadow_hours": _var_var_27["building_tree_obstruction"],
                "real_shadow_hours_list": _var_var_28["building_tree_obstruction"],
                "candidate_hours": _var_var_27["possible_obstruction"],
                "candidate_hours_list": _var_var_28["possible_obstruction"],
                "forecast_shadow_applied": sorted(set(_var_var_37)),
                "low_sun_hours": _var_var_27["low_sun_angle"],
                "low_sun_hours_list": _var_var_28["low_sun_angle"],
                "snow_hours": len(_var_var_38),
                "snow_hours_list": _var_var_38,
                "hourly_breakdown": sorted(_var_var_29, key=lambda x: x["hour"]),
            }
            self._cache_date = _var_var_9
            return self._cached_analysis
        except Exception as _var_var_11:
            _LOGGER.error(f"Error calculating shadow today analysis: {_var_var_11}")
            self._cached_analysis = {
                "status": "error",
                "error": str(_var_var_11),
                "shadow_hours": 0,
                "hourly_breakdown": [],
            }
            self._cache_date = _var_var_9
            return self._cached_analysis
