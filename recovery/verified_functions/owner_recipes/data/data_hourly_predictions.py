# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager, normalize_hourly_target_datetime
from .data_shadow_detection import ShadowDetector
from .data_learning_filter import (
    CURTAILMENT_THROTTLE_REASON_SQL,
    LEARNABLE_WEATHER_MISS_ALERT_TYPES,
    WEATHER_ALERT_UNEXPECTED_SUN,
    WEATHER_MISS_CLASS_FORECAST_MISS,
    WEATHER_MISS_CLASS_LOCAL_EVENT,
    WEATHER_MISS_CLASS_NON_LEARNABLE_WEATHER,
    WEATHER_MISS_CLASS_TECHNICAL,
    WEATHER_MISS_CLASS_UNKNOWN,
    production_miss_uplift,
    should_exclude_group_from_learning,
    throttle_reason_excludes_from_learning,
    weather_alert_excludes_from_clean_evaluation,
    weather_alert_excludes_from_learning,
)
from ..const import (
    CURTAILMENT_BATTERY_SOC_MIN,
    MPPT_CLEAR_SKY_CLOUDS_MAX,
    MPPT_CLEAR_SKY_GHI_THRESHOLD,
    MPPT_THROTTLE_BATTERY_POWER_THRESHOLD,
    MPPT_THROTTLE_PRODUCTION_RATIO,
    THROTTLE_REASON_FULL_BATTERY,
    THROTTLE_REASON_ZERO_EXPORT,
)
from ..core.core_helpers import get_season


class HourlyPredictionsHandler(DataManagerIO):

    async def update_hourly_actual(
        self,
        date: str,
        hour: int,
        actual_kwh: float,
        sensor_data: Dict[str, Any],
        weather_actual: Optional[Dict[str, Any]] = None,
        astronomy_update: Optional[Dict[str, Any]] = None,
        inverter_max_power: float = 0.0,
        panel_group_actuals: Optional[Dict[str, float]] = None,
        zero_export_mode: bool = False,
        has_battery: bool = False,
        solar_to_battery_power: Optional[float] = None,
        battery_soc: Optional[float] = None,
        panel_group_exclusion_reason: Optional[str] = None,
    ) -> bool:
        """Update actual values for a specific hour. @zara

        Called every hour (e.g. at 12:05 to update 11:00-12:00).

        Args:
            date: Date string YYYY-MM-DD
            hour: Hour (0-23)
            actual_kwh: Actual production in kWh
            sensor_data: Sensor readings
            weather_actual: Actual weather data
            astronomy_update: Updated astronomy data
            inverter_max_power: Max AC power for clipping detection
            panel_group_actuals: Actual per-group production
            zero_export_mode: Whether zero export is active
            has_battery: Whether system has battery
            solar_to_battery_power: Power flowing to battery (W)
            battery_soc: Battery state of charge in percent (0-100) or 0-1

        Returns:
            True if successful
        """
        if False:
            __assert_armored__ = None
        try:
            await self.ensure_initialized()
            _var_var_6 = f"{date}_{hour:02d}"
            _var_var_15 = dt_util.now()
            _var_var_16 = await self.db.fetchone(
                "SELECT prediction_kwh FROM hourly_predictions WHERE prediction_id = ?",
                (_var_var_6,),
            )
            if not _var_var_16:
                _LOGGER.warning("No prediction found for %s", _var_var_6)
                return False
            _var_var_17 = _var_var_16[0]
            _var_var_18 = await self._get_prediction_weather(_var_var_6, "forecast")
            _var_var_19, _var_var_20, _var_var_21 = self._calculate_accuracy(
                _var_var_17, actual_kwh
            )
            _var_var_22 = False
            if inverter_max_power > 0:
                _var_var_23 = inverter_max_power * 0.95
                _var_var_22 = actual_kwh >= _var_var_23
            _var_var_24 = False
            _var_var_25 = None
            if zero_export_mode or has_battery:
                _var_var_24, _var_var_25 = self._detect_mppt_throttle(
                    actual_kwh,
                    has_battery,
                    solar_to_battery_power,
                    zero_export_mode=zero_export_mode,
                    weather_actual=weather_actual,
                    astronomy_data=astronomy_update,
                    battery_soc=battery_soc,
                )
            _var_var_26, _var_var_27 = self._detect_weather_alert(
                weather_actual, _var_var_18
            )
            if (
                not _var_var_26
                and production_miss_uplift(actual_kwh, _var_var_17) is not None
            ):
                _var_var_26 = True
                _var_var_27 = WEATHER_ALERT_UNEXPECTED_SUN
            _var_var_26, _var_var_27 = self._resolve_detection_ownership(
                _var_var_22, _var_var_24, _var_var_26, _var_var_27
            )
            _var_var_28 = None
            _var_var_29 = False
            if _var_var_26:
                _var_var_28, _var_var_29 = await self._classify_weather_miss(
                    date=date,
                    hour=hour,
                    alert_type=_var_var_27,
                    weather_actual=weather_actual,
                    weather_forecast=_var_var_18,
                    is_clipped=_var_var_22,
                    mppt_throttled=_var_var_24,
                )
            _var_var_30 = _var_var_28
            if (
                _var_var_28 == WEATHER_MISS_CLASS_FORECAST_MISS
                and _var_var_27 in LEARNABLE_WEATHER_MISS_ALERT_TYPES
            ):
                await self._promote_recent_weather_miss_sequence(
                    date, hour, _var_var_27
                )
            _var_var_31 = await self.db.fetchone(
                "SELECT exclude_from_learning, mppt_throttle_reason\n                   FROM hourly_predictions WHERE prediction_id = ?",
                (_var_var_6,),
            )
            _var_var_32 = bool(_var_var_31[0]) if _var_var_31 else False
            _var_var_33 = _var_var_31[1] if _var_var_31 else None
            _var_var_34 = (
                _var_var_22
                or _var_var_24
                or throttle_reason_excludes_from_learning(_var_var_25)
                or throttle_reason_excludes_from_learning(_var_var_33)
                or _var_var_32
                or (
                    _var_var_26
                    and weather_alert_excludes_from_learning(_var_var_27, _var_var_30)
                )
            )
            _var_var_25 = _var_var_25 or _var_var_33
            _var_var_35 = _var_var_34 or (
                _var_var_26
                and weather_alert_excludes_from_clean_evaluation(_var_var_27)
            )
            await self.db.execute(
                "UPDATE hourly_predictions SET\n                    actual_kwh = ?,\n                    actual_measured_at = ?,\n                    accuracy_percent = ?,\n                    error_kwh = ?,\n                    error_percent = ?,\n                    inverter_clipped = ?,\n                    has_sensor_data = TRUE,\n                    sensor_data_complete = ?,\n                    exclude_from_learning = ?,\n                    exclude_from_clean_evaluation = ?,\n                    mppt_throttled = ?,\n                    mppt_throttle_reason = ?,\n                    has_weather_alert = ?,\n                    weather_alert_type = ?,\n                    weather_miss_class = ?,\n                    reforecast_trigger_eligible = ?\n                WHERE prediction_id = ?",
                (
                    round(actual_kwh, 4),
                    _var_var_15.isoformat(),
                    _var_var_19,
                    _var_var_20,
                    _var_var_21,
                    _var_var_22,
                    self._check_sensor_completeness(sensor_data),
                    _var_var_34,
                    _var_var_35,
                    _var_var_24,
                    _var_var_25,
                    _var_var_26,
                    _var_var_27,
                    _var_var_28,
                    _var_var_29,
                    _var_var_6,
                ),
            )
            await self._save_sensor_actual(_var_var_6, sensor_data)
            if weather_actual:
                await self._save_prediction_weather(
                    _var_var_6, "actual", weather_actual
                )
            if astronomy_update:
                await self._update_prediction_astronomy(_var_var_6, astronomy_update)
            _var_var_36 = len(getattr(self.coordinator, "panel_groups", []) or [])
            _var_var_37 = False
            if panel_group_actuals:
                for _var_var_38, _var_var_39 in panel_group_actuals.items():
                    await self.db.execute(
                        "UPDATE prediction_panel_groups\n                           SET actual_kwh = ?\n                           WHERE prediction_id = ? AND group_name = ?",
                        (round(_var_var_39, 4), _var_var_6, _var_var_38),
                    )
                    _var_var_40 = await self.db.fetchone(
                        "SELECT prediction_kwh, actual_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (_var_var_6, _var_var_38),
                    )
                    if _var_var_40:
                        if _var_var_40[1] is not None:
                            _var_var_41 = _var_var_40[0]
                            _var_var_42 = _var_var_40[1]
                            _var_var_43, _var_var_44, _var_var_45 = (
                                self._calculate_accuracy(_var_var_41, _var_var_42)
                            )
                            _var_var_46 = {
                                "prediction_kwh": _var_var_41,
                                "actual_kwh": _var_var_42,
                                "error_kwh": _var_var_44,
                                "error_percent": _var_var_45,
                                "accuracy_percent": _var_var_43,
                            }
                            await self.db.save_hourly_panel_group_accuracy(
                                _var_var_6, _var_var_38, _var_var_46
                            )
                _var_var_47 = await self.db.fetchone(
                    "SELECT COUNT(*),\n                              SUM(CASE WHEN actual_kwh IS NOT NULL THEN 1 ELSE 0 END)\n                       FROM prediction_panel_groups\n                       WHERE prediction_id = ?",
                    (_var_var_6,),
                )
                _var_var_48 = int(_var_var_47[0] or 0) if _var_var_47 else 0
                _var_var_49 = int(_var_var_47[1] or 0) if _var_var_47 else 0
                _var_var_50 = await self.db.fetchall(
                    "SELECT group_name FROM prediction_panel_groups\n                       WHERE prediction_id = ?",
                    (_var_var_6,),
                )
                _var_var_51 = {_var_var_16[0] for _var_var_16 in _var_var_50}
                _var_var_52 = set(panel_group_actuals)
                _var_var_37 = (
                    _var_var_36 > 0
                    and _var_var_48 == _var_var_36
                    and (_var_var_49 == _var_var_36)
                    and (len(panel_group_actuals) == _var_var_36)
                    and (_var_var_51 == _var_var_52)
                )
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET has_panel_group_actuals = ?\n                       WHERE prediction_id = ?",
                    (_var_var_37, _var_var_6),
                )
            _var_var_53 = await self._get_prediction_astronomy(_var_var_6)
            _var_var_54 = {
                "prediction_id": _var_var_6,
                "actual_kwh": actual_kwh,
                "astronomy": _var_var_53,
                "sensor_actual": sensor_data,
                "weather_actual": weather_actual,
                "weather_forecast": _var_var_18,
                "target_hour": hour,
                "target_datetime": f"{date}T{hour:02d}:00:00",
                "flags": {
                    "inverter_clipped": _var_var_22,
                    "mppt_throttled": _var_var_24,
                    "partial_panel_group_actuals": bool(panel_group_actuals)
                    and (not _var_var_37),
                },
            }
            _var_var_55 = await self._shadow_detector.detect_shadow_ensemble(
                _var_var_54
            )
            await self.db.save_hourly_shadow_detection(_var_var_6, _var_var_55)
            _var_var_56 = getattr(self, "coordinator", None)
            if _var_var_56 is not None and _var_var_56.update_shadow_detection_cache(
                _var_var_6, _var_var_55
            ):
                _var_var_56.async_update_listeners()
            if _var_var_37 and panel_group_actuals:
                _var_var_57 = {}
                for _var_var_58 in panel_group_actuals:
                    _var_var_59 = await self.db.fetchone(
                        "SELECT actual_kwh, physics_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (_var_var_6, _var_var_58),
                    )
                    if (
                        not _var_var_59
                        or _var_var_59[0] is None
                        or _var_var_59[1] is None
                        or (_var_var_59[1] <= 0)
                    ):
                        _var_var_57 = {}
                        break
                    _var_var_57[_var_var_58] = {
                        "actual_kwh": _var_var_59[0],
                        "theoretical_max_kwh": _var_var_59[1],
                    }
                    _var_var_60 = await self.db.fetchone(
                        "SELECT theoretical_kwh\n                             FROM astronomy_cache_panel_groups\n                            WHERE cache_date = ? AND hour = ? AND group_name = ?",
                        (date, hour, _var_var_58),
                    )
                    if _var_var_60:
                        if _var_var_60[0] is not None:
                            _var_var_57[_var_var_58]["clear_sky_kwh"] = _var_var_60[0]
                _var_var_61 = {}
                if len(_var_var_57) == _var_var_36:
                    _var_var_61 = await self._shadow_detector.detect_shadow_per_group(
                        _var_var_54, _var_var_57, system_ensemble=_var_var_55
                    )
                _var_var_62 = (
                    weather_actual.get("snow_per_group", {}) if weather_actual else {}
                )
                if not _var_var_62:
                    try:
                        _var_var_62 = await self.db.load_snow_tracking_groups()
                    except Exception:
                        _var_var_62 = {}
                _var_var_63 = {
                    "flags": {
                        "inverter_clipped": _var_var_22,
                        "mppt_throttled": _var_var_24,
                        "mppt_throttle_reason": _var_var_25,
                        "has_weather_alert": _var_var_26,
                        "weather_alert_type": _var_var_27,
                        "weather_miss_class": _var_var_28,
                    }
                }
                for _var_var_58 in panel_group_actuals:
                    if panel_group_exclusion_reason:
                        _var_var_64 = True
                        _var_var_65 = panel_group_exclusion_reason
                    else:
                        _var_var_64, _var_var_65 = should_exclude_group_from_learning(
                            _var_var_63, _var_var_58, _var_var_62, _var_var_61
                        )
                    _var_var_66 = _var_var_62.get(_var_var_58, {}).get(
                        "snow_covered", False
                    )
                    _var_var_67 = _var_var_61.get(_var_var_58, {}).get("shadow_type")
                    await self.db.update_prediction_panel_group_flags(
                        _var_var_6,
                        _var_var_58,
                        {
                            "exclude_from_learning_group": _var_var_64,
                            "exclusion_reason_group": (
                                _var_var_65 if _var_var_64 else None
                            ),
                            "snow_covered_group": _var_var_66,
                            "shadow_type_group": _var_var_67,
                        },
                    )
            _var_var_68 = await self._calculate_production_metrics(date)
            await self.db.save_hourly_production_metrics(_var_var_6, _var_var_68)
            _var_var_69 = await self._calculate_historical_context(date, hour)
            await self.db.save_hourly_historical_context(_var_var_6, _var_var_69)
            _LOGGER.debug(
                "Updated actual for %s: %.3f kWh, accuracy: %.1f%%",
                _var_var_6,
                actual_kwh,
                _var_var_19 or 0,
            )
            return True
        except Exception as _var_var_14:
            _LOGGER.error(
                "Failed to update hourly actual: %s", _var_var_14, exc_info=True
            )
            return False
