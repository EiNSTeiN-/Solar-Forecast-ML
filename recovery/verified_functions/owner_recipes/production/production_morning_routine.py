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

    async def update_hourly_actual(
        self,
        date: str,
        hour: int,
        actual_kwh: float,
        sensor_data: Dict[str, Any],
        weather_actual: Dict[str, Any] = None,
        panel_group_actuals: Dict[str, float] = None,
        inverter_max_power: float = 0.0,
        zero_export_mode: bool = False,
        has_battery: bool = False,
        solar_to_battery_power: Optional[float] = None,
        battery_soc: Optional[float] = None,
        max_grid_export_w: Optional[float] = None,
        panel_group_exclusion_reason: Optional[str] = None,
        is_reconstructed: bool = False,
        reconstructed_exclude_from_learning: bool = True,
    ) -> bool:
        """Update hourly actual production value with MPPT/clipping/alert detection.

        Args:
            date: Date string (YYYY-MM-DD)
            hour: Hour of the day (0-23)
            actual_kwh: Total actual production for this hour
            sensor_data: Sensor readings (temperature, humidity, etc.)
            weather_actual: Actual weather data
            panel_group_actuals: Dict mapping group_name -> actual_kwh per group
            inverter_max_power: Max AC power in W for clipping detection
            zero_export_mode: Whether zero export is active
            has_battery: Whether system has battery storage
            solar_to_battery_power: Power flowing to battery in W
            battery_soc: Battery state of charge in percent (0-100) or 0-1
            max_grid_export_w: Configured export or AC output cap in W
            panel_group_exclusion_reason: Reason to exclude panel group actuals from learning
            is_reconstructed: Whether this actual was recovered from recorder/fallback data
            reconstructed_exclude_from_learning: Whether the recovered actual must be excluded

        @zara
        """
        if False:
            __assert_armored__ = None
        try:
            if self.db is None:
                _LOGGER.warning("Database not available for updating hourly actual")
                return False
            _var_var_46 = f"{date}_{hour:02d}"
            _var_var_129 = dt_util.now()
            _var_var_35 = await self.db.fetchone(
                "SELECT prediction_kwh, has_weather_alert, weather_alert_type\n                   FROM hourly_predictions WHERE prediction_id = ?",
                (_var_var_46,),
            )
            if not _var_var_35:
                _LOGGER.warning(
                    "No prediction found for %s hour %d to update", date, hour
                )
                return False
            if hasattr(self.db, "is_actual_backfill_blocked"):
                _var_var_130 = await self.db.is_actual_backfill_blocked(date, hour)
                if _var_var_130:
                    _var_var_131 = "emergency_outage_backfill_blocked"
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                           SET exclude_from_learning = 1,\n                               exclude_from_clean_evaluation = 1,\n                               actual_backfill_blocked = 1,\n                               data_quarantine_reason = COALESCE(data_quarantine_reason, ?),\n                               data_quarantine_at = COALESCE(data_quarantine_at, ?),\n                               mppt_throttle_reason = COALESCE(NULLIF(mppt_throttle_reason, ''), ?)\n                           WHERE prediction_id = ?",
                        (
                            _var_var_131,
                            _var_var_129.isoformat(),
                            _var_var_131,
                            _var_var_46,
                        ),
                    )
                    _LOGGER.warning(
                        "Hourly actual write blocked by emergency outage quarantine: %s",
                        _var_var_46,
                    )
                    return True
            _var_var_47 = _var_var_35[0] or 0.0
            _var_var_132 = _var_var_35[2] if _var_var_35[1] else None
            _var_var_133 = actual_kwh - _var_var_47
            _var_var_134 = _var_var_133 / _var_var_47 * 100 if _var_var_47 > 0 else 0
            _var_var_135 = 100 - abs(_var_var_134)
            _var_var_136 = self._check_sensor_completeness(sensor_data)
            _var_var_137 = await self._get_astronomy_for_hour(date, hour)
            _var_var_138 = None
            if weather_actual:
                _var_var_139 = await self.db.fetchone(
                    "SELECT temperature, solar_radiation_wm2, rain, clouds\n                       FROM prediction_weather\n                       WHERE prediction_id = ? AND weather_type = 'forecast'",
                    (_var_var_46,),
                )
                if _var_var_139:
                    _var_var_138 = {
                        "temperature_c": _var_var_139[0],
                        "solar_radiation_wm2": _var_var_139[1],
                        "precipitation_mm": _var_var_139[2],
                        "cloud_cover_percent": _var_var_139[3],
                    }
            _var_var_140 = False
            if inverter_max_power > 0:
                _var_var_140 = actual_kwh >= inverter_max_power * 0.95
            _var_var_141, _var_var_142 = self._detect_weather_alert(
                weather_actual, _var_var_138
            )
            if (
                not _var_var_141
                and production_miss_uplift(actual_kwh, _var_var_47) is not None
            ):
                _var_var_141 = True
                _var_var_142 = WEATHER_ALERT_UNEXPECTED_SUN
            _var_var_143 = {}
            if zero_export_mode or has_battery:
                _var_var_143 = await self._get_curtailment_flow_evidence(
                    date,
                    hour,
                    max_grid_export_w=max_grid_export_w,
                    zero_export_mode=zero_export_mode,
                )
            _var_var_144 = False
            _var_var_145 = None
            if zero_export_mode or has_battery:
                _var_var_144, _var_var_145 = self._detect_mppt_throttle(
                    actual_kwh,
                    has_battery,
                    solar_to_battery_power,
                    weather_actual=weather_actual,
                    astronomy_data=_var_var_137,
                    flow_evidence=_var_var_143,
                    max_grid_export_w=max_grid_export_w,
                    zero_export_mode=zero_export_mode,
                    battery_soc=battery_soc,
                )
            _var_var_146, _var_var_147 = self.evaluate_curtailment_learning_quarantine(
                actual_kwh=actual_kwh,
                solar_to_battery_power=solar_to_battery_power,
                weather_actual=weather_actual,
                astronomy_data=_var_var_137,
                flow_evidence=_var_var_143,
                max_grid_export_w=max_grid_export_w,
                zero_export_mode=zero_export_mode,
                has_battery=has_battery,
                battery_soc=battery_soc,
            )
            if _var_var_146 and (not _var_var_145):
                _var_var_145 = _var_var_147
            _var_var_141, _var_var_142 = self._resolve_detection_ownership(
                _var_var_140, _var_var_144, _var_var_141, _var_var_142
            )
            _var_var_148 = None
            _var_var_149 = False
            if _var_var_141:
                _var_var_148, _var_var_149 = await self._classify_weather_miss(
                    date=date,
                    hour=hour,
                    alert_type=_var_var_142,
                    weather_actual=weather_actual,
                    weather_forecast=_var_var_138,
                    is_clipped=_var_var_140,
                    mppt_throttled=_var_var_144,
                )
            if (
                _var_var_148 == WEATHER_MISS_CLASS_FORECAST_MISS
                and _var_var_142 in LEARNABLE_WEATHER_MISS_ALERT_TYPES
            ):
                await self._promote_recent_weather_miss_sequence(
                    date, hour, _var_var_142
                )
            _var_var_150 = (
                _var_var_140
                or _var_var_144
                or throttle_reason_excludes_from_learning(_var_var_145)
                or _var_var_146
                or (
                    _var_var_141
                    and weather_alert_excludes_from_learning(_var_var_142, _var_var_148)
                )
            )
            _var_var_151 = _var_var_150 or (
                _var_var_141
                and weather_alert_excludes_from_clean_evaluation(_var_var_142)
            )
            if not _var_var_150:
                if getattr(self.coordinator, "learning_paused", False):
                    _var_var_150 = True
                    _var_var_151 = True
                    _var_var_141 = True
                    _var_var_142 = "manual_pause"
                    _LOGGER.debug(
                        "Hour %s excluded: learning paused by user", _var_var_46
                    )
            if is_reconstructed and reconstructed_exclude_from_learning:
                _var_var_150 = True
                _var_var_151 = True
                if not _var_var_145:
                    _var_var_145 = "reconstructed_gap_interpolation"
            await self.db.execute(
                "UPDATE hourly_predictions\n                   SET actual_kwh = ?,\n                       actual_measured_at = ?,\n                       accuracy_percent = ?,\n                       error_kwh = ?,\n                       error_percent = ?,\n                       has_sensor_data = TRUE,\n                       sensor_data_complete = ?,\n                       inverter_clipped = ?,\n                       exclude_from_learning = ?,\n                       exclude_from_clean_evaluation = ?,\n                       mppt_throttled = ?,\n                       mppt_throttle_reason = ?,\n                       has_weather_alert = ?,\n                       weather_alert_type = ?,\n                       weather_miss_class = ?,\n                       reforecast_trigger_eligible = ?\n                   WHERE prediction_id = ?",
                (
                    actual_kwh,
                    _var_var_129,
                    _var_var_135,
                    _var_var_133,
                    _var_var_134,
                    _var_var_136,
                    _var_var_140,
                    _var_var_150,
                    _var_var_151,
                    _var_var_144,
                    _var_var_145,
                    _var_var_141,
                    _var_var_142,
                    _var_var_148,
                    _var_var_149,
                    _var_var_46,
                ),
            )
            if sensor_data:
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms)\n                       VALUES (?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms",
                    (
                        _var_var_46,
                        sensor_data.get("temperature_c"),
                        sensor_data.get("humidity_percent"),
                        sensor_data.get("solar_radiation_wm2"),
                        sensor_data.get("rain_mm"),
                        sensor_data.get("wind_speed_ms"),
                    ),
                )
            if weather_actual:
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, lux,\n                        frost_detected, frost_score, frost_confidence)\n                       VALUES (?, 'actual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           wind = excluded.wind,\n                           humidity = excluded.humidity,\n                           rain = excluded.rain,\n                           clouds = excluded.clouds,\n                           pressure = excluded.pressure,\n                           lux = excluded.lux,\n                           frost_detected = excluded.frost_detected,\n                           frost_score = excluded.frost_score,\n                           frost_confidence = excluded.frost_confidence",
                    (
                        _var_var_46,
                        weather_actual.get("temperature_c"),
                        weather_actual.get("solar_radiation_wm2"),
                        weather_actual.get("wind_speed_ms"),
                        weather_actual.get("humidity_percent"),
                        weather_actual.get("precipitation_mm"),
                        weather_actual.get("cloud_cover_percent"),
                        weather_actual.get("pressure_hpa"),
                        weather_actual.get("lux"),
                        weather_actual.get("frost_detected"),
                        weather_actual.get("frost_score"),
                        weather_actual.get("frost_confidence"),
                    ),
                )
            _var_var_152 = len(getattr(self.coordinator, "panel_groups", []) or [])
            _var_var_153 = False
            if panel_group_actuals:
                _var_var_154 = 0
                for _var_var_54, _var_var_155 in panel_group_actuals.items():
                    if _var_var_155 is not None:
                        _var_var_63 = (
                            await self.db.get_active_panel_group_topology_meta(
                                _var_var_54
                            )
                        )
                        await self.db.execute(
                            "INSERT INTO prediction_panel_groups\n                               (prediction_id, group_name, prediction_kwh, actual_kwh,\n                                raw_physics_kwh,\n                                config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction)\n                               VALUES (?, ?, 0.0, ?, NULL, ?, ?, ?, ?)\n                               ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                                   actual_kwh = excluded.actual_kwh,\n                                   config_epoch_id = COALESCE(excluded.config_epoch_id, config_epoch_id),\n                                   group_uid = COALESCE(excluded.group_uid, group_uid),\n                                   group_lineage_uid = COALESCE(excluded.group_lineage_uid, group_lineage_uid),\n                                   power_wp_at_prediction = COALESCE(excluded.power_wp_at_prediction, power_wp_at_prediction)",
                            (
                                _var_var_46,
                                _var_var_54,
                                _var_var_155,
                                _var_var_63.get("config_epoch_id"),
                                _var_var_63.get("group_uid"),
                                _var_var_63.get("group_lineage_uid"),
                                _var_var_63.get("power_wp_at_prediction"),
                            ),
                        )
                        _var_var_154 += 1
                if _var_var_154 > 0:
                    _var_var_156 = await self.db.fetchone(
                        "SELECT COUNT(*),\n                                  SUM(CASE WHEN actual_kwh IS NOT NULL THEN 1 ELSE 0 END)\n                           FROM prediction_panel_groups\n                           WHERE prediction_id = ?",
                        (_var_var_46,),
                    )
                    _var_var_157 = int(_var_var_156[0] or 0) if _var_var_156 else 0
                    _var_var_158 = int(_var_var_156[1] or 0) if _var_var_156 else 0
                    _var_var_153 = (
                        _var_var_152 > 0
                        and _var_var_157 == _var_var_152
                        and (_var_var_158 == _var_var_152)
                        and (len(panel_group_actuals) == _var_var_152)
                    )
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                           SET has_panel_group_actuals = ?\n                           WHERE prediction_id = ?",
                        (_var_var_153, _var_var_46),
                    )
                    _LOGGER.debug(
                        "Panel group actuals updated for %s: %d groups (stored=%d/%d)",
                        _var_var_46,
                        _var_var_154,
                        _var_var_158,
                        _var_var_152,
                    )
                    _var_var_159 = panel_group_exclusion_reason
                    if (
                        is_reconstructed
                        and reconstructed_exclude_from_learning
                        and (not _var_var_159)
                    ):
                        _var_var_159 = "reconstructed_gap_interpolation"
                    if _var_var_159:
                        try:
                            for (
                                _var_var_54,
                                _var_var_155,
                            ) in panel_group_actuals.items():
                                if _var_var_155 is None:
                                    continue
                                await self.db.update_prediction_panel_group_flags(
                                    _var_var_46,
                                    _var_var_54,
                                    {
                                        "exclude_from_learning_group": True,
                                        "exclusion_reason_group": _var_var_159,
                                    },
                                )
                        except Exception as _var_var_160:
                            _LOGGER.warning(
                                "Panel group exclusion flags could not be updated for %s: %s",
                                _var_var_46,
                                _var_var_160,
                            )
            try:
                _var_var_161 = {
                    "prediction_id": _var_var_46,
                    "actual_kwh": actual_kwh,
                    "astronomy": _var_var_137,
                    "sensor_actual": sensor_data,
                    "weather_actual": weather_actual,
                    "weather_forecast": _var_var_138,
                    "target_hour": hour,
                    "target_datetime": f"{date}T{hour:02d}:00:00",
                    "flags": {
                        "inverter_clipped": _var_var_140,
                        "mppt_throttled": _var_var_144,
                        "partial_panel_group_actuals": bool(panel_group_actuals)
                        and (not _var_var_153),
                        "mppt_throttle_reason": _var_var_145,
                        "curtailment_suspected": self.shadow_curtailment_suspected(
                            _var_var_143,
                            battery_soc=battery_soc,
                            max_grid_export_w=max_grid_export_w,
                            zero_export_mode=zero_export_mode,
                        ),
                    },
                }
                _var_var_162 = await self._shadow_detector.detect_shadow_ensemble(
                    _var_var_161
                )
                if _var_var_162:
                    await self.db.save_hourly_shadow_detection(
                        _var_var_46, _var_var_162
                    )
                    if self.coordinator.update_shadow_detection_cache(
                        _var_var_46, _var_var_162
                    ):
                        self.coordinator.async_update_listeners()
                    _LOGGER.debug(
                        "Shadow detection for %s: %s (%.1f%%)",
                        _var_var_46,
                        _var_var_162.get("shadow_type", "unknown"),
                        _var_var_162.get("shadow_percent", 0),
                    )
                if _var_var_153 and panel_group_actuals:
                    _var_var_163 = await self.db.fetchall(
                        "SELECT group_name, actual_kwh, physics_kwh\n                           FROM prediction_panel_groups\n                           WHERE prediction_id = ?\n                             AND actual_kwh IS NOT NULL\n                             AND physics_kwh IS NOT NULL\n                             AND physics_kwh > 0",
                        (_var_var_46,),
                    )
                    _var_var_164 = {_var_var_35[0] for _var_var_35 in _var_var_163}
                    _var_var_165 = set(panel_group_actuals)
                    if (
                        len(_var_var_163) == _var_var_152
                        and len(_var_var_165) == _var_var_152
                        and (_var_var_164 == _var_var_165)
                    ):
                        _var_var_166 = {
                            _var_var_35[0]: {
                                "actual_kwh": _var_var_35[1],
                                "theoretical_max_kwh": _var_var_35[2],
                            }
                            for _var_var_35 in _var_var_163
                        }
                        for _var_var_54 in list(_var_var_166):
                            _var_var_167 = await self.db.fetchone(
                                "SELECT theoretical_kwh\n                                     FROM astronomy_cache_panel_groups\n                                    WHERE cache_date = ? AND hour = ? AND group_name = ?",
                                (date, hour, _var_var_54),
                            )
                            if _var_var_167:
                                if _var_var_167[0] is not None:
                                    _var_var_166[_var_var_54]["clear_sky_kwh"] = (
                                        _var_var_167[0]
                                    )
                        _var_var_168 = (
                            await self._shadow_detector.detect_shadow_per_group(
                                _var_var_161, _var_var_166, system_ensemble=_var_var_162
                            )
                        )
                        await self._apply_group_shadow_learning_exclusions(
                            _var_var_46,
                            _var_var_161["flags"],
                            _var_var_168,
                            (weather_actual or {}).get("snow_per_group", {}),
                            panel_group_exclusion_reason,
                        )
                    else:
                        _LOGGER.debug(
                            "Per-group shadow detection skipped for %s: incomplete expected data",
                            _var_var_46,
                        )
            except _ShadowLearningExclusionError:
                raise
            except Exception as _var_var_169:
                _LOGGER.debug(
                    "Shadow detection skipped for %s: %s", _var_var_46, _var_var_169
                )
            try:
                await self._ensemble_shadow_handler.evaluate_prediction(_var_var_46)
            except Exception as _var_var_169:
                _LOGGER.debug(
                    "Ensemble shadow evaluation skipped for %s: %s",
                    _var_var_46,
                    _var_var_169,
                )
            try:
                await self._model_candidate_shadow_handler.evaluate_prediction(
                    _var_var_46
                )
            except Exception as _var_var_170:
                _LOGGER.warning(
                    "Model candidate shadow evaluation deferred for EOD retry for %s: %s",
                    _var_var_46,
                    _var_var_170,
                )
            if (
                _var_var_141
                and _var_var_142
                and (_var_var_142 != _var_var_132)
                and (
                    _var_var_142
                    in {
                        "sudden_storm",
                        "unexpected_rain",
                        "unexpected_snow",
                        "unexpected_clouds",
                    }
                )
            ):
                _var_var_171 = self.hass.data.get(DOMAIN, {}).get(
                    "notification_service"
                )
                if _var_var_171 is not None:
                    try:
                        await _var_var_171.show_weather_alert(
                            alert_type=_var_var_142,
                            reason=_var_var_148 or _var_var_142,
                            hour=hour,
                            date_str=date,
                            weather_actual=weather_actual,
                            weather_forecast=_var_var_138,
                        )
                    except Exception:
                        _LOGGER.warning(
                            "Weather alert notification failed for %s",
                            _var_var_46,
                            exc_info=True,
                        )
            if True:
                return True
        except Exception as _var_var_11:
            _LOGGER.error(
                "Error updating hourly actual: %s", _var_var_11, exc_info=True
            )
            return False
