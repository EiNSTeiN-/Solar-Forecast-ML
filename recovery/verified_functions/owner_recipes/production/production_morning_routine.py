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

    async def execute_morning_routine_with_retry(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_hourly: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
        candidate_shadow: Optional[Dict[str, Any]] = None,
        forecast_cycle: str = "",
        trigger_kind: str = "",
        max_retries: int = 3,
    ) -> bool:
        """
        Execute morning routine with bulletproof retry logic.

        Returns:
            True if successful, False if all retries failed. @zara
        """
        if self.db is None:
            _LOGGER.error("Database not available for morning routine")
            return False
        max_retries = max(1, min(int(max_retries), 3))
        _var_var_6 = None
        try:
            _var_var_6, _var_var_7 = (
                await self._ensemble_shadow_handler.acquire_morning_batch(
                    date, forecast_cycle, trigger_kind
                )
            )
            _LOGGER.info(
                "Ensemble shadow morning batch %s: %s",
                "resumed" if _var_var_7 else "created",
                _var_var_6,
            )
        except Exception:
            _LOGGER.exception(
                "Ensemble shadow batch setup failed; continuing the production forecast"
            )
        try:
            for _var_var_8 in range(1, max_retries + 1):
                _LOGGER.info("")
                _LOGGER.info(
                    "================================================================================"
                )
                _LOGGER.info(
                    "  MORNING ROUTINE EXECUTION - Attempt %d/%d for %s",
                    _var_var_8,
                    max_retries,
                    date,
                )
                _LOGGER.info(
                    "================================================================================"
                )
                try:
                    _LOGGER.info("→ Step 1/5: Validating prerequisites...")
                    if not await self._validate_prerequisites(
                        date, hourly_forecast, weather_hourly, astronomy_data
                    ):
                        raise ValueError("Prerequisites validation failed")
                    _LOGGER.info(
                        "  ✓ Prerequisites validated: %d forecast hours, %d weather hours",
                        len(hourly_forecast),
                        len(weather_hourly),
                    )
                    _var_var_9 = getattr(self.coordinator, "inverter_max_power", 0.0)
                    if _var_var_9 > 0:
                        _LOGGER.info(
                            "  ℹ Inverter capping enabled: %.2f kW", _var_var_9
                        )
                    _LOGGER.info(
                        "→ Step 2/5: Creating hourly predictions in database..."
                    )
                    _var_var_10 = await self._create_daily_predictions(
                        date=date,
                        hourly_forecast=hourly_forecast,
                        weather_forecast=weather_hourly,
                        astronomy_data=astronomy_data,
                        sensor_config=sensor_config,
                        inverter_max_power=_var_var_9,
                        morning_batch_id=_var_var_6 or "",
                        candidate_shadow=candidate_shadow,
                        forecast_cycle=forecast_cycle,
                    )
                    _LOGGER.info("  ✓ Hourly predictions created successfully")
                    _LOGGER.info("→ Step 3/5: Verifying data integrity...")
                    if not await self._verify_integrity(date, _var_var_10):
                        raise RuntimeError("Integrity verification failed")
                    _LOGGER.info("  ✓ Data integrity verified - no duplicates found")
                    _LOGGER.info("→ Step 4/5: Refreshing coordinator cache...")
                    await self.coordinator._refresh_hourly_predictions_cache()
                    _LOGGER.info("  ✓ Coordinator cache refreshed")
                    _LOGGER.info("→ Step 5/5: Triggering sensor updates...")
                    await self.coordinator.async_request_refresh()
                    _LOGGER.info("  ✓ Sensor updates triggered")
                    _LOGGER.info("")
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("  ✓✓✓ MORNING ROUTINE SUCCESSFUL ✓✓✓")
                    _LOGGER.info(
                        "  Date: %s | Attempt: %d/%d", date, _var_var_8, max_retries
                    )
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("")
                    await self._finish_ensemble_shadow_batch(_var_var_6, True)
                    return True
                except Exception as _var_var_11:
                    _LOGGER.error(
                        "Morning routine attempt %d/%d FAILED: %s",
                        _var_var_8,
                        max_retries,
                        _var_var_11,
                        exc_info=True,
                    )
                    if _var_var_8 < max_retries:
                        _var_var_12 = 60 * _var_var_8
                        _LOGGER.info(
                            "Waiting %ds before retry %d...",
                            _var_var_12,
                            _var_var_8 + 1,
                        )
                        await asyncio.sleep(_var_var_12)
                    else:
                        _LOGGER.error(
                            "CRITICAL: Morning routine FAILED after %d attempts! Manual intervention required",
                            max_retries,
                        )
                        await self._send_critical_alert(
                            "Morning routine failure", str(_var_var_11)
                        )
                        await self._finish_ensemble_shadow_batch(_var_var_6, False)
                        return False
            await self._finish_ensemble_shadow_batch(_var_var_6, False)
            return False
        except Exception as _var_var_11:
            _LOGGER.error(
                "CRITICAL: Morning routine handler crashed: %s",
                _var_var_11,
                exc_info=True,
            )
            return False

    async def _persist_daily_predictions(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_forecast: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
        inverter_max_power: float = 0.0,
        morning_batch_id: str = "",
        candidate_shadow: Optional[Dict[str, Any]] = None,
        forecast_cycle: str = "",
    ) -> tuple[
        set,
        List[str],
        Dict[str, float],
        Dict[str, Dict[str, Any]],
        Dict[Tuple[str, str], float],
        float,
    ]:
        """Persist the core morning forecast inside the caller's transaction.

        00:25 writes a fallback today lock. pre_sunrise unlocks that fallback,
        writes fresh weather hours, and becomes the today anchor. After that
        canonical lock, later runs keep today's hours and may still refresh
        tomorrow / day-after. Raises on error so retry logic can handle it.
        """
        if False:
            __assert_armored__ = None
        _LOGGER.info("  ├─ Checking existing forecast locks...")
        _var_var_16 = await self.db.fetchone(
            "SELECT COUNT(*) as count FROM hourly_predictions WHERE target_date = ?",
            (date,),
        )
        _var_var_17 = _var_var_16[0] if _var_var_16 else 0
        if _var_var_17 > 0:
            _LOGGER.info(
                "  │  Found %d existing predictions - will update", _var_var_17
            )
        _var_var_18 = await self.db.fetchone(
            "SELECT locked, locked_at, source, prediction_kwh FROM daily_forecasts\n               WHERE forecast_type = 'today' AND forecast_date = ?",
            (date,),
        )
        _var_var_19 = str(_var_var_18[2] or "") if _var_var_18 else ""
        _var_var_20 = preserve_existing_today_morning_lock(
            has_today_hours=_var_var_17 > 0,
            locked=bool(_var_var_18 and _var_var_18[0]),
            lock_source=_var_var_19,
            forecast_cycle=forecast_cycle,
            locked_at=_var_var_18[1] if _var_var_18 else None,
        )
        _var_var_21 = None
        if _var_var_20 and _var_var_18 and (_var_var_18[3] is not None):
            _var_var_21 = float(_var_var_18[3])
        _var_var_22 = (
            _var_var_19
            if _var_var_20 and _var_var_19
            else today_lock_source_for_cycle(forecast_cycle)
        )
        if _var_var_18 and _var_var_18[0]:
            if not _var_var_20:
                _LOGGER.info(
                    "  │  Unlocking daily_forecast (was locked at %s by %s) for cycle=%s",
                    _var_var_18[1],
                    _var_var_18[2],
                    forecast_cycle or "unspecified",
                )
                await self.db.execute(
                    "UPDATE daily_forecasts SET locked = 0\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                    (date,),
                )
        _LOGGER.info("  ├─ Preparing to update predictions for 3 days...")
        from datetime import timedelta as _var_var_23

        _var_var_24 = datetime.fromisoformat(date).date()
        _var_var_25 = set()
        for _var_var_26 in hourly_forecast:
            _var_var_3 = _var_var_26.get("hour")
            _var_var_27 = _var_var_26.get("date")
            if not _var_var_3 is None and (not not _var_var_27):
                if _var_var_20:
                    if _var_var_27 == date:
                        continue
                _var_var_25.add(f"{_var_var_27}_{_var_var_3:02d}")
        _var_var_28 = {}
        _var_var_29 = {}
        _var_var_30 = {}
        _var_var_31 = {}
        if _var_var_25:
            _var_var_32 = ",".join(["?"] * len(_var_var_25))
            _var_var_33 = tuple(_var_var_25)
            _var_var_34 = await self.db.fetchall(
                f"SELECT prediction_id, actual_kwh, has_sensor_data\n                    FROM hourly_predictions\n                    WHERE prediction_id IN ({_var_var_32}) AND actual_kwh IS NOT NULL",
                _var_var_33,
            )
            for _var_var_35 in _var_var_34:
                _var_var_28[_var_var_35[0]] = {
                    "actual_kwh": _var_var_35[1],
                    "has_sensor_data": _var_var_35[2],
                }
            _var_var_36 = await self.db.fetchall(
                f"SELECT prediction_id, group_name, actual_kwh\n                    FROM prediction_panel_groups\n                    WHERE prediction_id IN ({_var_var_32}) AND actual_kwh IS NOT NULL",
                _var_var_33,
            )
            for _var_var_35 in _var_var_36:
                _var_var_29[_var_var_35[0], _var_var_35[1]] = _var_var_35[2]
            _var_var_37 = await self.db.fetchall(
                f"SELECT prediction_id, temperature_c, humidity_percent,\n                       solar_radiation_wm2, rain_mm, wind_speed_ms\n                    FROM prediction_sensor_actual\n                    WHERE prediction_id IN ({_var_var_32})",
                _var_var_33,
            )
            for _var_var_35 in _var_var_37:
                _var_var_30[_var_var_35[0]] = {
                    "temperature_c": _var_var_35[1],
                    "humidity_percent": _var_var_35[2],
                    "solar_radiation_wm2": _var_var_35[3],
                    "rain_mm": _var_var_35[4],
                    "wind_speed_ms": _var_var_35[5],
                }
            _var_var_38 = await self.db.fetchall(
                f"SELECT prediction_id, temperature, solar_radiation_wm2, wind,\n                       humidity, rain, clouds, pressure, lux,\n                       frost_detected, frost_score, frost_confidence\n                    FROM prediction_weather\n                    WHERE prediction_id IN ({_var_var_32}) AND weather_type = 'actual'",
                _var_var_33,
            )
            for _var_var_35 in _var_var_38:
                _var_var_31[_var_var_35[0]] = {
                    "temperature": _var_var_35[1],
                    "solar_radiation_wm2": _var_var_35[2],
                    "wind": _var_var_35[3],
                    "humidity": _var_var_35[4],
                    "rain": _var_var_35[5],
                    "clouds": _var_var_35[6],
                    "pressure": _var_var_35[7],
                    "lux": _var_var_35[8],
                    "frost_detected": _var_var_35[9],
                    "frost_score": _var_var_35[10],
                    "frost_confidence": _var_var_35[11],
                }
            if _var_var_28:
                _LOGGER.info(
                    "  │  Preserved %d hourly actuals, %d group actuals before re-creation",
                    len(_var_var_28),
                    len(_var_var_29),
                )
            await self.db.execute(
                f"DELETE FROM hourly_predictions WHERE prediction_id IN ({_var_var_32})",
                _var_var_33,
            )
            _LOGGER.info(
                "  │  Cleared %d existing prediction slots for update", len(_var_var_25)
            )
        else:
            _LOGGER.warning("  │  No incoming predictions to update - skipping DELETE")
        _var_var_39 = dt_util.now()
        _var_var_40 = {}
        for _var_var_41 in weather_forecast:
            _var_var_42 = (_var_var_41.get("date", date), _var_var_41.get("hour"))
            _var_var_40[_var_var_42] = _var_var_41
        _LOGGER.info(
            "  ├─ Processing %d input forecast entries (3 days)...",
            len(hourly_forecast),
        )
        _var_var_43 = set()
        _var_var_44 = 0
        _var_var_45 = {}
        if _var_var_20:
            _var_var_45[date] = _var_var_17
            _LOGGER.info(
                "  │  Preserving %d today hourly rows under morning anchor %.2f kWh",
                _var_var_17,
                _var_var_21 or 0.0,
            )
        for _var_var_26 in hourly_forecast:
            _var_var_3 = _var_var_26.get("hour")
            if _var_var_3 is None:
                continue
            _var_var_27 = _var_var_26.get("date")
            if not _var_var_27:
                _var_var_27 = date
                _LOGGER.warning(
                    "  │  ⚠️  Missing date in hour_data for hour %d, using %s",
                    _var_var_3,
                    date,
                )
            if _var_var_20 and _var_var_27 == date:
                continue
            _var_var_46 = f"{_var_var_27}_{_var_var_3:02d}"
            if _var_var_46 in _var_var_43:
                _var_var_44 += 1
                continue
            _var_var_43.add(_var_var_46)
            _var_var_45[_var_var_27] = _var_var_45.get(_var_var_27, 0) + 1
            _var_var_47 = _var_var_26.get("production_kwh", 0.0)
            _var_var_48 = _var_var_26.get("prediction_kwh_uncapped", _var_var_47)
            if inverter_max_power > 0:
                _var_var_49 = inverter_max_power
                _var_var_47 = min(_var_var_47, _var_var_49)
            _var_var_50 = f"{_var_var_27}T{_var_var_3:02d}:00:00"
            _var_var_51 = datetime.fromisoformat(_var_var_27)
            await self.db.execute(
                "INSERT OR REPLACE INTO hourly_predictions\n                   (prediction_id, prediction_created_at, prediction_created_hour,\n                    morning_batch_id,\n                    target_datetime, target_date, target_hour,\n                    target_day_of_week, target_day_of_year, target_month, target_season,\n                    prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                    ml_contribution_percent, confidence, is_production_hour,\n                    physics_kwh, ai_kwh, ai_confidence,\n                    lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight,\n                    ghi_before_cap, ghi_after_cap, ghi_cap_applied)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _var_var_46,
                    _var_var_39,
                    _var_var_39.hour,
                    morning_batch_id,
                    _var_var_50,
                    _var_var_27,
                    _var_var_3,
                    _var_var_51.weekday(),
                    _var_var_51.timetuple().tm_yday,
                    _var_var_51.month,
                    get_season(_var_var_51.month),
                    round(_var_var_47, 4),
                    round(_var_var_48, 4),
                    _var_var_26.get("method", "physics"),
                    _var_var_26.get("ml_contribution", 0),
                    _var_var_26.get("confidence", 75.0),
                    6 <= _var_var_3 <= 20,
                    _var_var_26.get("physics_kwh"),
                    _var_var_26.get("ai_kwh"),
                    _var_var_26.get("ai_confidence"),
                    _var_var_26.get("lstm_kwh"),
                    _var_var_26.get("ridge_kwh"),
                    _var_var_26.get("tfs_kwh"),
                    _var_var_26.get("tfs_weight"),
                    _var_var_26.get("ghi_before_cap"),
                    _var_var_26.get("ghi_after_cap"),
                    _var_var_26.get("ghi_cap_applied"),
                ),
            )
            _var_var_52 = _var_var_26.get("groups")
            if _var_var_52:
                _var_var_52 = (
                    normalize_group_predictions_to_total(_var_var_52, _var_var_47) or []
                )
                _var_var_26["groups"] = _var_var_52
                _var_var_26["panel_group_predictions"] = (
                    sync_panel_group_prediction_map(_var_var_52)
                )
                for _var_var_53 in _var_var_52:
                    _var_var_54 = _var_var_53.get("name")
                    _var_var_55 = _var_var_53.get("power_kwh", 0.0)
                    _var_var_56 = _var_var_53.get("physics_kwh")
                    _var_var_57 = _var_var_53.get("ai_kwh")
                    _var_var_58 = _var_var_53.get("lstm_kwh")
                    _var_var_59 = _var_var_53.get("ridge_kwh")
                    _var_var_60 = _var_var_53.get("tfs_kwh")
                    _var_var_61 = _var_var_53.get("tfs_weight")
                    _var_var_62 = _var_var_53.get("raw_physics_kwh")
                    _var_var_63 = await self.db.get_active_panel_group_topology_meta(
                        _var_var_54
                    )
                    await self.db.execute(
                        "INSERT INTO prediction_panel_groups\n                           (prediction_id, group_name, prediction_kwh, physics_kwh, ai_kwh,\n                            lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, raw_physics_kwh,\n                            config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction)\n                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                           ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                               prediction_kwh = excluded.prediction_kwh,\n                               physics_kwh = excluded.physics_kwh,\n                               ai_kwh = excluded.ai_kwh,\n                               lstm_kwh = excluded.lstm_kwh,\n                               ridge_kwh = excluded.ridge_kwh,\n                               tfs_kwh = excluded.tfs_kwh,\n                               tfs_weight = excluded.tfs_weight,\n                               raw_physics_kwh = excluded.raw_physics_kwh,\n                               config_epoch_id = excluded.config_epoch_id,\n                               group_uid = excluded.group_uid,\n                               group_lineage_uid = excluded.group_lineage_uid,\n                               power_wp_at_prediction = excluded.power_wp_at_prediction",
                        (
                            _var_var_46,
                            _var_var_54,
                            round(_var_var_55, 4),
                            round(_var_var_56, 4) if _var_var_56 else None,
                            round(_var_var_57, 4) if _var_var_57 else None,
                            round(_var_var_58, 4) if _var_var_58 else None,
                            round(_var_var_59, 4) if _var_var_59 else None,
                            round(_var_var_60, 4) if _var_var_60 else None,
                            round(_var_var_61, 3) if _var_var_61 is not None else None,
                            round(_var_var_62, 4) if _var_var_62 is not None else None,
                            _var_var_63.get("config_epoch_id"),
                            _var_var_63.get("group_uid"),
                            _var_var_63.get("group_lineage_uid"),
                            _var_var_63.get("power_wp_at_prediction"),
                        ),
                    )
                    continue
                await self.db.execute(
                    "UPDATE hourly_predictions SET has_panel_group_predictions = TRUE\n                       WHERE prediction_id = ?",
                    (_var_var_46,),
                )
            _var_var_64 = _var_var_40.get((_var_var_27, _var_var_3), {})
            if _var_var_64:
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, source, diffuse_radiation,\n                        direct_radiation)\n                       VALUES (?, 'forecast', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_46,
                        _var_var_64.get("temperature"),
                        _var_var_64.get("solar_radiation_wm2"),
                        _var_var_64.get("wind"),
                        _var_var_64.get("humidity"),
                        _var_var_64.get("rain"),
                        _var_var_64.get("clouds"),
                        _var_var_64.get("pressure"),
                        _var_var_64.get("source", "blended"),
                        _var_var_64.get("diffuse_radiation") or _var_var_64.get("dhi"),
                        _var_var_64.get("direct_radiation"),
                    ),
                )
                _var_var_65 = _var_var_64.get("correction_snapshot_id")
                if _var_var_65:
                    await self.db.insert_weather_forecast_usage_snapshot(
                        prediction_id=_var_var_46,
                        morning_batch_id=morning_batch_id,
                        correction_snapshot_id=_var_var_65,
                        locked_at=_var_var_39,
                        target_datetime=_var_var_50,
                        target_date=_var_var_27,
                        target_hour=_var_var_3,
                    )
                else:
                    _LOGGER.warning(
                        "  │  Weather usage provenance unavailable for %s; EOD/MLP learning will skip this hour",
                        _var_var_46,
                    )
            _var_var_66 = astronomy_data.get(f"{_var_var_27}_{_var_var_3:02d}", {})
            if not _var_var_66:
                _var_var_66 = astronomy_data.get(str(_var_var_3), {})
            _var_var_67 = astronomy_data.get(_var_var_27, astronomy_data)
            if not not _var_var_66:
                _var_var_68 = _var_var_67.get("sunrise_local")
                _var_var_69 = _var_var_67.get("sunset_local")
                _var_var_70 = _var_var_67.get("solar_noon_local")
                _var_var_71 = _var_var_67.get("daylight_hours", 12.0)
                _var_var_72 = None
                _var_var_73 = None
                _var_var_74 = None
                _var_var_75 = None
                try:
                    from datetime import datetime as _var_var_14

                    _var_var_15 = _var_var_14(
                        _var_var_51.year,
                        _var_var_51.month,
                        _var_var_51.day,
                        _var_var_3,
                        30,
                    )

                    def _parse_ts(ts):
                        if ts is None:
                            return
                        if isinstance(ts, _var_var_14):
                            return ts.replace(
                                year=_var_var_15.year,
                                month=_var_var_15.month,
                                day=_var_var_15.day,
                                tzinfo=None,
                            )
                        _var_var_108 = str(ts).strip()
                        if "T" in _var_var_108 or len(_var_var_108) > 10:
                            try:
                                _var_var_109 = _var_var_14.fromisoformat(
                                    _var_var_108.replace("Z", "+00:00")
                                )
                                return _var_var_109.replace(
                                    year=_var_var_15.year,
                                    month=_var_var_15.month,
                                    day=_var_var_15.day,
                                    tzinfo=None,
                                )
                            except (ValueError, TypeError):
                                pass
                        for _var_var_110 in ("%H:%M:%S", "%H:%M"):
                            try:
                                _var_var_111 = _var_var_14.strptime(
                                    _var_var_108, _var_var_110
                                )
                                return _var_var_15.replace(
                                    hour=_var_var_111.hour,
                                    minute=_var_var_111.minute,
                                    second=_var_var_111.second,
                                )
                            except (ValueError, TypeError):
                                continue

                    _var_var_77 = _parse_ts(_var_var_68)
                    _var_var_78 = _parse_ts(_var_var_69)
                    _var_var_79 = _parse_ts(_var_var_70)
                    if _var_var_77 and _var_var_78:
                        _var_var_80 = (_var_var_78 - _var_var_77).total_seconds()
                        _var_var_81 = (_var_var_15 - _var_var_77).total_seconds()
                        _var_var_73 = (
                            max(0.0, min(1.0, _var_var_81 / _var_var_80))
                            if _var_var_80 > 0
                            else 0.5
                        )
                        _var_var_74 = max(0.0, _var_var_81 / 3600.0)
                        _var_var_75 = max(
                            0.0, (_var_var_78 - _var_var_15).total_seconds() / 3600.0
                        )
                    if _var_var_79:
                        _var_var_72 = (
                            _var_var_15 - _var_var_79
                        ).total_seconds() / 3600.0
                except Exception:
                    pass
                await self.db.execute(
                    "INSERT INTO prediction_astronomy\n                       (prediction_id, sunrise, sunset, solar_noon, daylight_hours,\n                        sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2,\n                        theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio,\n                        hours_after_sunrise, hours_before_sunset)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           sunrise = excluded.sunrise,\n                           sunset = excluded.sunset,\n                           solar_noon = excluded.solar_noon,\n                           daylight_hours = excluded.daylight_hours,\n                           sun_elevation_deg = excluded.sun_elevation_deg,\n                           sun_azimuth_deg = excluded.sun_azimuth_deg,\n                           clear_sky_radiation_wm2 = excluded.clear_sky_radiation_wm2,\n                           theoretical_max_kwh = excluded.theoretical_max_kwh,\n                           hours_since_solar_noon = excluded.hours_since_solar_noon,\n                           day_progress_ratio = excluded.day_progress_ratio,\n                           hours_after_sunrise = excluded.hours_after_sunrise,\n                           hours_before_sunset = excluded.hours_before_sunset",
                    (
                        _var_var_46,
                        _var_var_68,
                        _var_var_69,
                        _var_var_70,
                        _var_var_71,
                        _var_var_66.get("sun_elevation_deg"),
                        _var_var_66.get("sun_azimuth_deg"),
                        _var_var_66.get("clear_sky_radiation_wm2"),
                        _var_var_66.get("theoretical_max_kwh"),
                        _var_var_72,
                        _var_var_73,
                        _var_var_74,
                        _var_var_75,
                    ),
                )
        _var_var_82 = 0
        if _var_var_28:
            for _var_var_83, _var_var_84 in _var_var_28.items():
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET actual_kwh = ?, has_sensor_data = ?\n                       WHERE prediction_id = ?",
                    (
                        _var_var_84["actual_kwh"],
                        _var_var_84["has_sensor_data"],
                        _var_var_83,
                    ),
                )
                _var_var_82 += 1
        if _var_var_29:
            for (_var_var_83, _var_var_54), _var_var_85 in _var_var_29.items():
                await self.db.execute(
                    "INSERT INTO prediction_panel_groups\n                       (prediction_id, group_name, prediction_kwh, actual_kwh, raw_physics_kwh)\n                       VALUES (?, ?, 0.0, ?, NULL)\n                       ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                           actual_kwh = excluded.actual_kwh",
                    (_var_var_83, _var_var_54, _var_var_85),
                )
        if _var_var_30:
            for _var_var_83, _var_var_86 in _var_var_30.items():
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms)\n                       VALUES (?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms",
                    (
                        _var_var_83,
                        _var_var_86["temperature_c"],
                        _var_var_86["humidity_percent"],
                        _var_var_86["solar_radiation_wm2"],
                        _var_var_86["rain_mm"],
                        _var_var_86["wind_speed_ms"],
                    ),
                )
        if _var_var_31:
            for _var_var_83, _var_var_86 in _var_var_31.items():
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, lux,\n                        frost_detected, frost_score, frost_confidence)\n                       VALUES (?, 'actual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           wind = excluded.wind,\n                           humidity = excluded.humidity,\n                           rain = excluded.rain,\n                           clouds = excluded.clouds,\n                           pressure = excluded.pressure,\n                           lux = excluded.lux,\n                           frost_detected = excluded.frost_detected,\n                           frost_score = excluded.frost_score,\n                           frost_confidence = excluded.frost_confidence",
                    (
                        _var_var_83,
                        _var_var_86["temperature"],
                        _var_var_86["solar_radiation_wm2"],
                        _var_var_86["wind"],
                        _var_var_86["humidity"],
                        _var_var_86["rain"],
                        _var_var_86["clouds"],
                        _var_var_86["pressure"],
                        _var_var_86["lux"],
                        _var_var_86["frost_detected"],
                        _var_var_86["frost_score"],
                        _var_var_86["frost_confidence"],
                    ),
                )
        if morning_batch_id:
            try:
                await self._ensemble_shadow_handler.capture_morning_batch(
                    morning_batch_id, _var_var_39
                )
            except Exception as _var_var_87:
                raise RuntimeError(
                    f"mandatory ensemble shadow provenance failed for batch {morning_batch_id}"
                ) from _var_var_87
            _var_var_88 = filter_candidate_shadow_for_preserved_today(
                candidate_shadow, preserve_today=_var_var_20, today=date
            )
            if _var_var_88 is not None:
                try:
                    _var_var_89 = (
                        await self._model_candidate_shadow_handler.capture_morning_run(
                            morning_batch_id, _var_var_88
                        )
                    )
                    _LOGGER.info(
                        "  │  Candidate shadow evidence captured for batch %s: %d rows",
                        morning_batch_id,
                        _var_var_89,
                    )
                except Exception as _var_var_90:
                    raise RuntimeError(
                        f"mandatory candidate shadow provenance failed for batch {morning_batch_id}"
                    ) from _var_var_90
        if _var_var_82 > 0:
            _LOGGER.info(
                "  ├─ Restored %d hourly actuals, %d group actuals, %d sensor, %d weather actuals",
                _var_var_82,
                len(_var_var_29),
                len(_var_var_30),
                len(_var_var_31),
            )
        _LOGGER.info(
            "  ├─ Calculating daily totals from saved predictions for 3 days..."
        )
        _var_var_91 = {}
        for _var_var_92 in range(3):
            _var_var_93 = (_var_var_24 + _var_var_23(days=_var_var_92)).isoformat()
            _var_var_94 = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) as total FROM hourly_predictions WHERE target_date = ?",
                (_var_var_93,),
            )
            _var_var_95 = (
                _var_var_94["total"] if _var_var_94 and _var_var_94["total"] else 0.0
            )
            _var_var_91[_var_var_93] = _var_var_95
            _LOGGER.info("  │  → %s: %.2f kWh", _var_var_93, _var_var_95)
        _LOGGER.info("  ├─ Saving daily forecasts to database for 3 days...")
        _var_var_96 = {}
        for _var_var_64 in weather_forecast:
            _var_var_97 = _var_var_64.get("date") or date
            if _var_var_97:
                _var_var_96[_var_var_97] = _var_var_96.get(_var_var_97, 0) + 1
        _var_var_98 = ["today", "tomorrow", "day_after_tomorrow"]
        _var_var_99 = 0
        _var_var_100 = 0
        for _var_var_92, _var_var_101 in enumerate(_var_var_98):
            _var_var_93 = (_var_var_24 + _var_var_23(days=_var_var_92)).isoformat()
            _var_var_95 = _var_var_91.get(_var_var_93, 0.0)
            if _var_var_101 == "today" and _var_var_20:
                if _var_var_21 is not None:
                    _var_var_91[_var_var_93] = _var_var_21
                _LOGGER.info(
                    "  │  Preserving today lock %.2f kWh source=%s (cycle=%s)",
                    _var_var_21 or 0.0,
                    _var_var_22,
                    forecast_cycle or "unspecified",
                )
                _var_var_100 += 1
                continue
            _var_var_102 = _var_var_96.get(_var_var_93, 0)
            _var_var_103 = _var_var_45.get(_var_var_93, 0)
            if not morning_day_lock_allowed(_var_var_102, _var_var_103):
                _var_var_100 += 1
                _LOGGER.info(
                    "  │  Skipping lock for %s (%s): weather_hours=%d prediction_hours=%d total=%.2f kWh",
                    _var_var_93,
                    _var_var_101,
                    _var_var_102,
                    _var_var_103,
                    _var_var_95,
                )
                continue
            _var_var_104 = (
                _var_var_22 if _var_var_101 == "today" else CANONICAL_TODAY_LOCK_SOURCE
            )
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                    locked, locked_at, source, created_at)\n                   VALUES (?, ?, ?, ?, TRUE, ?, ?, ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = excluded.prediction_kwh,\n                       prediction_kwh_raw = excluded.prediction_kwh_raw,\n                       locked = excluded.locked,\n                       locked_at = excluded.locked_at,\n                       source = excluded.source",
                (
                    _var_var_101,
                    _var_var_93,
                    round(_var_var_95, 2),
                    round(_var_var_95, 2),
                    _var_var_39,
                    _var_var_104,
                    _var_var_39,
                ),
            )
            _var_var_99 += 1
            continue
        _LOGGER.debug(
            "     → Daily forecasts locked=%d skipped=%d locked_at=%s",
            _var_var_99,
            _var_var_100,
            _var_var_39,
        )
        _var_var_95 = _var_var_91.get(date, 0.0)
        _LOGGER.info("  ├─ Updating coordinator expected daily production...")
        await self.coordinator.data_manager.save_expected_daily_production(
            round(_var_var_95, 2)
        )
        _LOGGER.info("  ├─ Setting morning routine lock for %s...", date)
        await self.db.execute(
            "UPDATE coordinator_state\n               SET last_set_date = ?, last_updated = ?\n               WHERE id = 1",
            (date, _var_var_39),
        )
        _LOGGER.debug("     → Lock flag set: last_set_date = %s", date)
        _var_var_105 = len(_var_var_43)
        _LOGGER.info("  └─ SUMMARY:")
        _LOGGER.info("     • Input entries: %d (3 days)", len(hourly_forecast))
        _LOGGER.info("     • Created predictions: %d", _var_var_105)
        if _var_var_44 > 0:
            _LOGGER.info("     • Skipped duplicates: %d", _var_var_44)
        _LOGGER.info("     • Predictions per day:")
        for _var_var_27, _var_var_106 in sorted(_var_var_45.items()):
            _var_var_107 = _var_var_91.get(_var_var_27, 0.0)
            _LOGGER.info(
                "       - %s: %d hours, %.2f kWh",
                _var_var_27,
                _var_var_106,
                _var_var_107,
            )
        _LOGGER.info("     • Coordinator updated: Yes (today=%.2f kWh)", _var_var_95)
        return (
            set(_var_var_45.keys()),
            _var_var_98,
            _var_var_91,
            _var_var_28,
            _var_var_29,
            _var_var_95,
        )

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
