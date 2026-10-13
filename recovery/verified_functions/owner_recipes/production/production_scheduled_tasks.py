# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
import math
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_point_in_time,
)
from .production_morning_routine import MorningRoutineHandler
from .production_adaptive_forecast import AdaptiveForecastEngine
from .production_operational_reforecast import OperationalReforecastEngine
from .production_actual_resolver import ActualSourceResolver
from ..data.data_daily_summaries import DailySummariesHandler
from ..data.data_model_candidate_shadow import ModelCandidateShadowHandler
from ..ai.ai_model_reaction import ModelReactionHandler
from ..services.service_system_report import SystemReportGenerator
from ..const import (
    ADAPTIVE_CHECK_HOUR,
    ADAPTIVE_CHECK_MINUTE,
    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
    CURTAILMENT_FLOW_LOW_BATTERY_W,
    CURTAILMENT_FLOW_MIN_HOUSE_W,
    CONF_ADAPTIVE_FORECAST_MODE,
    CONF_BATTERY_SOC_SENSOR,
    CONF_HAS_BATTERY,
    CONF_HUMIDITY_SENSOR,
    CONF_LUX_SENSOR,
    CONF_MAX_GRID_EXPORT_W,
    CONF_PRESSURE_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_SOLAR_RADIATION_SENSOR,
    CONF_SOLAR_TO_BATTERY_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WIND_SENSOR,
    CONF_ZERO_EXPORT_MODE,
    DEFAULT_ADAPTIVE_FORECAST_MODE,
    DEFAULT_HAS_BATTERY,
    DEFAULT_MAX_GRID_EXPORT_W,
    DEFAULT_WINTER_MODE,
    DEFAULT_ZERO_EXPORT_MODE,
    CONF_WINTER_MODE,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..core.core_hubble import Hubble
from ..core.core_panel_group_forecast import get_group_capacity_kwp
from ..ai.ai_weather_mlp import (
    WEATHER_MLP_EVALUATION_FOLDS,
    WEATHER_MLP_FEATURE_COUNT,
    WEATHER_MLP_MIN_TRAINING_SAMPLES,
    build_weather_mlp_features,
    day_blocked_folds,
    held_out_training_shortfall,
    pooled_evaluation_metrics,
    residual_weather_mlp_target,
    weather_mlp_persist_verdict,
)
from ..data.data_learning_filter import (
    CLEAN_EVALUATION_ELIGIBILITY_SQL,
    CURTAILMENT_THROTTLE_REASON_SQL,
    CURTAILMENT_THROTTLE_REASONS,
    HOURLY_LEARNING_ELIGIBILITY_SQL,
    WEATHER_ALERT_UNEXPECTED_SUN,
    production_miss_uplift,
)
from ..physics.physics_calibration_telemetry import (
    advantage_factor,
    group_best_method,
    recommended_tfs_weight,
    regime_best_method,
)
from ..data.db_manager import (
    WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
    WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
    WEATHER_MLP_TRAINING_CONTRACT_VERSION,
)
from .production_rest_of_day_uplift import rest_of_day_uplift_from_weather_evidence


def _classify_static_weather_regime_for_learning(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify persisted forecast weather into static learning regimes."""
    _var_var_0 = None
    if clear_sky > 50 and ghi >= 0:
        _var_var_0 = min(max(ghi / clear_sky, 0.0), 1.05)
    _var_var_1 = None
    if ghi > 5 and dhi >= 0:
        _var_var_1 = min(max(dhi / ghi, 0.0), 1.5)
    if sun_elevation <= 8.0:
        if clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD or (
            _var_var_0 is not None
            and _var_var_0 <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD
        ):
            return "low_sun_uncertain"
    if rain >= BAD_WEATHER_RAIN_MIN_MM:
        if (
            clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
            or humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD
            or (
                _var_var_0 is not None
                and _var_var_0 <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD
            )
            or (
                _var_var_1 is not None
                and _var_var_1 >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
            )
        ):
            return "rain_overcast"
    if clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD:
        if _var_var_0 is None or _var_var_0 <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD:
            return "low_ghi_overcast"
    if (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and _var_var_1 is not None
        and (_var_var_1 >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    ):
        return "diffuse_overcast"
    if clouds < 35.0:
        if _var_var_0 is None or _var_var_0 >= 0.45:
            return "stable_good"
    if rain <= 0.05 and clouds >= 60.0:
        if _var_var_0 is None or _var_var_0 <= 0.5:
            return "dry_cloud_edge"
    if rain <= 0.05 and _var_var_0 is not None and (_var_var_0 >= 0.5):
        return "bright_mixed"
    return "mixed_uncertain"


class ScheduledTasksManager:

    async def resume_pending_model_candidate_gate(self, predictor: Any) -> None:
        """Reconcile and resume the complete candidate gate after startup."""
        try:
            async with self._model_candidate_gate_lock:
                _var_var_7 = (
                    await self.model_candidate_shadow_handler.refresh_actual_evidence()
                )
                _var_var_8 = (
                    await self.db.invalidate_stale_model_candidate_evaluations()
                )
                _var_var_9 = await predictor.reconcile_pending_model_candidates()
                _var_var_10 = None
                if _var_var_9:
                    _var_var_10 = await self.model_candidate_shadow_handler.evaluate_ready_candidate(
                        candidate_id=_var_var_9
                    )
                if _var_var_10 is None:
                    _var_var_10 = (
                        await self.db.get_retryable_model_candidate_promotion()
                    )
                if _var_var_10 and _var_var_10["decision"] == "GO":
                    await self._promote_model_candidate_once(predictor, _var_var_10)
                elif _var_var_10 is None:
                    _var_var_11 = None
                    if _var_var_9:
                        _var_var_11 = (
                            await self.model_candidate_shadow_handler.gate_readiness(
                                _var_var_9
                            )
                        )
                        try:
                            _var_var_12 = getattr(
                                self.db, "persist_ai_gate_status", None
                            )
                            if callable(_var_var_12):
                                await _var_var_12(_var_var_9, _var_var_11)
                        except Exception as _var_var_13:
                            _LOGGER.debug(
                                "AI gate status persist failed: %s", _var_var_13
                            )
                    await self.db.timeout_unevaluated_model_candidate(
                        source="startup", readiness=_var_var_11
                    )
            if _var_var_9:
                _LOGGER.info(
                    "Model candidate gate resumed after startup: actual_versions=%d invalidated_evaluations=%d candidate=%s decision=%s",
                    _var_var_7,
                    _var_var_8,
                    _var_var_9,
                    _var_var_10["decision"] if _var_var_10 else "WAIT",
                )
            else:
                _LOGGER.info(
                    "Model candidate gate resumed after startup: no pending candidate (actual_versions=%d invalidated_evaluations=%d)",
                    _var_var_7,
                    _var_var_8,
                )
        except Exception:
            _LOGGER.error(
                "Failed to resume model candidate gate after startup", exc_info=True
            )
        if True:
            return

    async def _execute_morning_routine(
        self, *, date: str, forecast_cycle: str, trigger_kind: str
    ) -> bool:
        """Run one morning pipeline at a time and retry every failure phase."""
        async with self._morning_pipeline_lock:
            for _var_var_99 in range(1, 4):
                try:
                    _var_var_93 = await self._execute_morning_routine_once(
                        date=date,
                        forecast_cycle=forecast_cycle,
                        trigger_kind=trigger_kind,
                    )
                except asyncio.CancelledError:
                    self.coordinator.set_health_issue(
                        "morning_routine", "Morning routine was cancelled"
                    )
                    raise
                except Exception as _var_var_100:
                    _var_var_93 = False
                    _LOGGER.error(
                        "Morning routine attempt %d/3 failed before persistence: %s",
                        _var_var_99,
                        _var_var_100,
                        exc_info=True,
                    )
                if _var_var_93:
                    self.coordinator.clear_health_issue("morning_routine")
                    self.coordinator.clear_health_issue("database")
                    self.coordinator.update_system_status(
                        event_type="morning_routine",
                        event_status="success",
                        event_summary=f"Morning forecast completed for {date}",
                        event_details={"cycle": forecast_cycle, "attempt": _var_var_99},
                    )
                    return True
                self.coordinator.set_health_issue(
                    "morning_routine",
                    f"Morning forecast unavailable for {date} ({forecast_cycle})",
                )
                self.coordinator.update_system_status(
                    event_type="morning_routine",
                    event_status="partial" if _var_var_99 < 3 else "failed",
                    event_summary=f"Morning forecast attempt {_var_var_99}/3 failed for {date}",
                    event_details={"cycle": forecast_cycle, "attempt": _var_var_99},
                )
                await self._refresh_database_health()
                if _var_var_99 < 3:
                    await asyncio.sleep(30 * _var_var_99)
            return False
        if True:
            return

    async def _execute_morning_routine_once(
        self, *, date: str, forecast_cycle: str, trigger_kind: str
    ) -> bool:
        """Execute one complete morning attempt with fresh data."""
        try:
            _var_var_94 = dt_util.now()
            _var_var_101 = await self._generate_full_forecast(
                date, forecast_cycle=forecast_cycle, trigger_kind=trigger_kind
            )
            if not _var_var_101:
                _LOGGER.error("Failed to generate forecast for morning routine")
                return False
            _var_var_102 = _var_var_101.get("hourly", [])
            if not _var_var_102:
                _LOGGER.error("No hourly forecast data in result")
                return False
            _var_var_103 = await self._get_weather_hourly(date)
            _var_var_104 = await self._get_astronomy_data(date)
            _var_var_105 = self._get_sensor_config()
            _var_var_93 = (
                await self.morning_routine_handler.execute_morning_routine_with_retry(
                    date=date,
                    hourly_forecast=_var_var_102,
                    weather_hourly=_var_var_103,
                    astronomy_data=_var_var_104,
                    sensor_config=_var_var_105,
                    candidate_shadow=_var_var_101.get("_candidate_shadow"),
                    forecast_cycle=forecast_cycle,
                    trigger_kind=trigger_kind,
                    max_retries=1,
                )
            )
            if _var_var_93:
                _var_var_106 = _var_var_101.get("best_hour")
                _var_var_107 = _var_var_101.get("best_hour_kwh")
                if _var_var_106 is not None:
                    try:
                        await self.coordinator.data_manager.save_forecast_best_hour(
                            hour=_var_var_106,
                            prediction_kwh=_var_var_107 or 0.0,
                            source=_var_var_101.get("method", "morning_routine"),
                        )
                        _LOGGER.info(
                            "Best hour saved: %02d:00 (%.3f kWh)",
                            _var_var_106,
                            _var_var_107 or 0.0,
                        )
                    except Exception as _var_var_16:
                        _LOGGER.warning("Failed to save best hour: %s", _var_var_16)
                _var_var_108 = _var_var_101.get("tomorrow")
                if _var_var_108 is not None:
                    _var_var_109 = _var_var_94 + timedelta(days=1)
                    try:
                        await self.coordinator.data_manager.save_forecast_tomorrow(
                            target_date=_var_var_109,
                            prediction_kwh=_var_var_108,
                            source=_var_var_101.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug("Tomorrow forecast saved: %.2f kWh", _var_var_108)
                    except Exception as _var_var_16:
                        _LOGGER.warning(
                            "Failed to save tomorrow forecast: %s", _var_var_16
                        )
                _recovered_day_after = _var_var_101.get("day_after_tomorrow")
                if _recovered_day_after is not None:
                    _recovered_day_after_date = _var_var_94 + timedelta(days=2)
                    try:
                        await self.coordinator.data_manager.save_forecast_day_after(
                            target_date=_recovered_day_after_date,
                            prediction_kwh=_recovered_day_after,
                            source=_var_var_101.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug(
                            "Day-after forecast saved: %.2f kWh", _recovered_day_after
                        )
                    except Exception as _var_var_16:
                        _LOGGER.warning(
                            "Failed to save day-after forecast: %s", _var_var_16
                        )
            self.coordinator.async_update_listeners()
            return _var_var_93
        except Exception as _var_var_16:
            _LOGGER.error(
                "Morning routine execution failed: %s", _var_var_16, exc_info=True
            )
            return False

    async def _catch_up_missed_hourly_actuals(self) -> int:
        """Persist today's completed hours whose :05 update did not run. @zara

        A restart across the hourly slot leaves actual_kwh NULL although the
        recorder still holds the group power history for that hour.
        """
        if self._is_shutting_down():
            return 0
        _var_var_77 = dt_util.now()
        _var_var_66 = _var_var_77.date().isoformat()
        _var_var_126 = await self.db.fetchall(
            "SELECT target_hour FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour BETWEEN ? AND ?\n                 AND actual_kwh IS NULL\n                 AND COALESCE(manual_override, 0) = 0\n                 AND COALESCE(actual_backfill_blocked, 0) = 0\n               ORDER BY target_hour",
            (_var_var_66, HOURLY_ACTUAL_FIRST_HOUR, HOURLY_ACTUAL_LAST_HOUR),
        )
        _var_var_146 = [
            int(_var_var_62[0])
            for _var_var_62 in _var_var_126 or []
            if self._hourly_actual_slot(_var_var_66, int(_var_var_62[0]))
            + HOURLY_ACTUAL_CATCHUP_GRACE
            <= _var_var_77
        ]
        if not _var_var_146:
            return 0
        _LOGGER.info(
            "Hourly actual catch-up for %s: missed hours %s", _var_var_66, _var_var_146
        )
        _var_var_147 = 0
        for _var_var_116 in _var_var_146:
            if self._is_shutting_down():
                break
            try:
                async with self._hourly_actual_lock:
                    _var_var_62 = await self.db.fetchone(
                        "SELECT actual_kwh FROM hourly_predictions\n                           WHERE target_date = ? AND target_hour = ?",
                        (_var_var_66, _var_var_116),
                    )
                    if _var_var_62 is None or _var_var_62[0] is not None:
                        continue
                    if await self._persist_hourly_actual(
                        _var_var_66, _var_var_116, dt_util.now(), live=False
                    ):
                        _var_var_147 += 1
            except Exception as _var_var_16:
                _LOGGER.error(
                    "Hourly actual catch-up failed for %s_%02d: %s",
                    _var_var_66,
                    _var_var_116,
                    _var_var_16,
                    exc_info=True,
                )
                continue
        _LOGGER.info(
            "Hourly actual catch-up for %s: recovered %d/%d hours",
            _var_var_66,
            _var_var_147,
            len(_var_var_146),
        )
        return _var_var_147

    async def _train_weather_mlp(self, current_time) -> Dict[str, str]:
        """Train Weather MLP on historical precision data. @zara"""

        def skipped(reason: str, reason_code: str) -> Dict[str, str]:
            _LOGGER.info("Weather MLP training skipped [%s]: %s", reason_code, reason)
            return {
                "status": "skipped",
                "reason": reason,
                "reason_code": reason_code,
                "impact": "informational",
            }

        def failed(reason: str, reason_code: str) -> Dict[str, str]:
            _LOGGER.error("Weather MLP training failed [%s]: %s", reason_code, reason)
            return {"status": "failed", "reason": reason, "reason_code": reason_code}

        if self._weather_mlp is None:
            _var_var_23 = "Weather MLP is not configured"
            return skipped(_var_var_23, "weather_mlp_not_configured")
        if not self._has_available_local_solar_observation():
            self._attach_mlp_to_correctors()
            _var_var_23 = "no currently usable local solar-radiation sensor is configured; provider-only forecasts stay neutral"
            return skipped(_var_var_23, "weather_mlp_local_sensor_unavailable")
        try:
            _var_var_172 = current_time.date().isoformat()
            _var_var_173 = WEATHER_MLP_TRAINING_CONTRACT_VERSION
            _var_var_174 = WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
            _var_var_175 = await self.db.fetchone(
                "WITH precision_samples AS (\n                       SELECT wpd.id, wpd.date, wpd.hour, wpd.solar_forecast,\n                              wpd.solar_actual,\n                              CASE WHEN c.correction_snapshot_id IS NOT NULL\n                                     AND c.clouds IS NOT NULL\n                                     AND c.humidity IS NOT NULL\n                                     AND c.pressure IS NOT NULL\n                                     AND c.wind IS NOT NULL\n                                   THEN 1 ELSE 0 END AS has_weather,\n                              CASE WHEN c.sun_elevation IS NOT NULL\n                                   THEN 1 ELSE 0 END AS has_astronomy,\n                              COALESCE(c.sun_elevation, -90.0) AS sun_elevation_deg,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND COALESCE(hp.exclude_from_learning, 0) != 0\n                              ) AS learning_excluded,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND COALESCE(hp.mppt_throttled, 0) != 0\n                              ) AS mppt_excluded,\n                              EXISTS(\n                                  SELECT 1 FROM hourly_predictions hp\n                                  WHERE hp.target_date = wpd.date\n                                    AND hp.target_hour = wpd.hour\n                                    AND (COALESCE(hp.exclude_from_learning, 0) != 0\n                                         OR COALESCE(hp.mppt_throttled, 0) != 0\n                                         OR COALESCE(hp.inverter_clipped, 0) != 0)\n                              ) AS governance_excluded\n                       FROM weather_precision_daily wpd\n                       LEFT JOIN weather_forecast_usage_snapshots u\n                         ON u.usage_snapshot_id = wpd.usage_snapshot_id\n                       LEFT JOIN weather_forecast_correction_snapshots c\n                         ON c.correction_snapshot_id = u.correction_snapshot_id\n                       WHERE wpd.forecast_contract_version = ?\n                         AND COALESCE(wpd.solar_sample_eligible, 0) = 1\n                   )\n                   SELECT\n                       (SELECT COUNT(*)\n                        FROM hourly_weather_actual\n                        WHERE solar_radiation_wm2 IS NOT NULL) AS measured_all_time,\n                       (SELECT COUNT(*)\n                        FROM hourly_weather_actual\n                        WHERE date >= date(?, '-30 days')\n                          AND solar_radiation_wm2 IS NOT NULL) AS measured_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL THEN 1 ELSE 0\n                       END) AS precision_radiation_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                           THEN 1 ELSE 0\n                       END) AS weather_joined_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_astronomy = 1\n                           THEN 1 ELSE 0\n                       END) AS astronomy_joined_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                           THEN 1 ELSE 0\n                       END) AS physical_candidates_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.learning_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS learning_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.mppt_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS mppt_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.governance_excluded = 1\n                           THEN 1 ELSE 0\n                       END) AS governance_excluded_30d,\n                       SUM(CASE\n                           WHEN ps.date >= date(?, '-30 days')\n                            AND ps.solar_actual IS NOT NULL\n                            AND ps.has_weather = 1\n                            AND ps.has_astronomy = 1\n                            AND ps.solar_forecast > 10\n                            AND ps.sun_elevation_deg > 5\n                            AND ps.governance_excluded = 0\n                           THEN 1 ELSE 0\n                       END) AS eligible_30d\n                   FROM precision_samples ps",
                (_var_var_174,) + (_var_var_172,) * 9,
            )
            (
                _var_var_176,
                _var_var_177,
                _var_var_178,
                _var_var_179,
                _var_var_180,
                _var_var_181,
                _var_var_182,
                _var_var_183,
                _var_var_184,
                _var_var_185,
            ) = (
                (int(_var_var_29 or 0) for _var_var_29 in _var_var_175)
                if _var_var_175
                else (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            )
            if _var_var_176 == 0:
                return skipped(
                    "no physically measured solar-radiation observations are available; Weather MLP requires W/m² sensor data",
                    "weather_mlp_measured_radiation_unavailable",
                )
            if _var_var_177 == 0:
                return skipped(
                    "measured solar-radiation observations exist only outside the 30-day training window",
                    "weather_mlp_training_window_expired",
                )
            if _var_var_177 >= 50 and _var_var_178 == 0:
                _var_var_18 = await self.db.fetchone(
                    "SELECT\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND COALESCE(forecast_contract_version, '') != ?),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND COALESCE(solar_sample_eligible, 0) = 0\n                               AND solar_exclusion_reason IS NULL),\n                           (SELECT COUNT(*) FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND COALESCE(solar_sample_eligible, 0) = 1\n                               AND solar_actual IS NULL),\n                           (SELECT COUNT(*)\n                              FROM hourly_weather_actual a\n                              JOIN weather_forecast_usage_snapshots u\n                                ON u.target_date = a.date\n                               AND u.target_hour = a.hour\n                               AND u.contract_version = ?\n                              JOIN weather_forecast_correction_snapshots c\n                                ON c.correction_snapshot_id = u.correction_snapshot_id\n                               AND c.contract_version = ?\n                             WHERE a.date >= date(?, '-30 days')\n                               AND a.solar_radiation_wm2 IS NOT NULL),\n                           (SELECT COUNT(*)\n                              FROM hourly_weather_actual a\n                              JOIN weather_forecast_usage_snapshots u\n                                ON u.target_date = a.date\n                               AND u.target_hour = a.hour\n                               AND u.contract_version = ?\n                              JOIN weather_forecast_correction_snapshots c\n                                ON c.correction_snapshot_id = u.correction_snapshot_id\n                               AND c.contract_version = ?\n                              LEFT JOIN weather_precision_daily wpd\n                                ON wpd.date = a.date\n                               AND wpd.hour = a.hour\n                               AND wpd.forecast_contract_version = ?\n                             WHERE a.date >= date(?, '-30 days')\n                               AND a.solar_radiation_wm2 IS NOT NULL\n                               AND wpd.id IS NULL),\n                           (SELECT solar_exclusion_reason FROM weather_precision_daily\n                             WHERE date >= date(?, '-30 days')\n                               AND forecast_contract_version = ?\n                               AND solar_exclusion_reason IS NOT NULL\n                             GROUP BY solar_exclusion_reason\n                             ORDER BY COUNT(*) DESC LIMIT 1)",
                    (
                        _var_var_172,
                        _var_var_174,
                        _var_var_172,
                        _var_var_174,
                        _var_var_172,
                        _var_var_174,
                        _var_var_172,
                        _var_var_174,
                        WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                        _var_var_174,
                        _var_var_172,
                        WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                        _var_var_174,
                        _var_var_174,
                        _var_var_172,
                        _var_var_172,
                        _var_var_174,
                    ),
                )
                (
                    _var_var_186,
                    _var_var_187,
                    _var_var_188,
                    _var_var_189,
                    _var_var_190,
                    _var_var_191,
                ) = (
                    tuple((int(_var_var_29 or 0) for _var_var_29 in _var_var_18[:6]))
                    if _var_var_18
                    else (0, 0, 0, 0, 0, 0)
                )
                _var_var_192 = (_var_var_18[6] if _var_var_18 else None) or "unrecorded"
                if _var_var_190 == 0:
                    return skipped(
                        f"{_var_var_177} measured W/m² observations exist, but no frozen morning weather provenance is available for those hours yet (legacy precision rows in window: {_var_var_187})",
                        "weather_mlp_precision_samples_unavailable",
                    )
                if _var_var_191 > 0:
                    return failed(
                        f"Weather MLP pipeline invariant failed: {_var_var_191}/{_var_var_190} measured hours with frozen snapshots produced no weather precision rows",
                        "weather_mlp_precision_pipeline_invariant",
                    )
                if _var_var_188 == 0 and _var_var_189 == 0:
                    return skipped(
                        f"{_var_var_186} weather precision rows in the training window carry a recorded exclusion reason (most common: {_var_var_192})",
                        "weather_mlp_precision_samples_excluded",
                    )
                return failed(
                    f"Weather MLP pipeline invariant failed: {_var_var_177} measured W/m² observations produced no weather precision samples ({_var_var_188} rows without exclusion reason, {_var_var_189} eligible rows without measured GHI)",
                    "weather_mlp_precision_pipeline_invariant",
                )
            if _var_var_178 >= 50 and _var_var_179 < 50:
                return failed(
                    f"Weather MLP pipeline invariant failed: only {_var_var_179}/{_var_var_178} precision samples have frozen weather snapshots",
                    "weather_mlp_missing_weather_join",
                )
            if _var_var_178 >= 50 and _var_var_180 < 50:
                return failed(
                    f"Weather MLP pipeline invariant failed: only {_var_var_180}/{_var_var_178} precision samples have astronomy rows",
                    "weather_mlp_missing_astronomy_join",
                )
            if _var_var_181 >= 50 and _var_var_185 < 50:
                _var_var_193 = _var_var_181 - _var_var_185 - _var_var_184
                if _var_var_193 != 0:
                    return failed(
                        f"Weather MLP sample funnel lost {_var_var_181 - _var_var_185} samples, but only {_var_var_184} have explicit learning, MPPT, or clipping flags",
                        "weather_mlp_sample_funnel_invariant",
                    )
                return skipped(
                    f"Weather MLP governance filter retained {_var_var_185}/{_var_var_181} samples (learning={_var_var_182}, mppt={_var_var_183}, unique_excluded={_var_var_184})",
                    "weather_mlp_governance_filtered",
                )
            if _var_var_185 < 50:
                return skipped(
                    f"Weather MLP warm-up: {_var_var_185}/50 valid measured-radiation samples",
                    "weather_mlp_warmup",
                )
            _var_var_126 = await self.db.fetchall(
                "SELECT wpd.date, wpd.hour, wpd.solar_forecast, wpd.solar_actual, wpd.solar_factor,\n                      c.clouds, c.humidity, c.pressure, c.wind,\n                      c.sun_elevation, c.rain,\n                      c.cloud_cover_low, c.cloud_cover_mid, c.cloud_cover_high,\n                      c.visibility_m, c.static_solar_factor_applied\n               FROM weather_precision_daily wpd\n               JOIN weather_forecast_usage_snapshots u\n                    ON u.usage_snapshot_id = wpd.usage_snapshot_id\n               JOIN weather_forecast_correction_snapshots c\n                    ON c.correction_snapshot_id = u.correction_snapshot_id\n               WHERE wpd.solar_forecast > 10\n                 AND wpd.solar_actual IS NOT NULL\n                 AND c.sun_elevation > 5\n                 AND c.clouds IS NOT NULL\n                 AND c.humidity IS NOT NULL\n                 AND c.pressure IS NOT NULL\n                 AND c.wind IS NOT NULL\n                 AND wpd.forecast_contract_version = ?\n                 AND COALESCE(wpd.solar_sample_eligible, 0) = 1\n                 AND wpd.date >= date(?, '-30 days')\n                 AND NOT EXISTS (\n                     SELECT 1 FROM hourly_predictions hp\n                     WHERE hp.target_date = wpd.date\n                       AND hp.target_hour = wpd.hour\n                       AND (COALESCE(hp.exclude_from_learning, 0) != 0\n                            OR COALESCE(hp.mppt_throttled, 0) != 0\n                            OR COALESCE(hp.inverter_clipped, 0) != 0)\n                 )\n               ORDER BY wpd.date, wpd.hour",
                (_var_var_174, _var_var_172),
            )
            if len(_var_var_126 or []) != _var_var_185:
                return failed(
                    f"Weather MLP sample funnel counted {_var_var_185} eligible samples but the loader returned {(len(_var_var_126) if _var_var_126 else 0)}",
                    "weather_mlp_sample_funnel_invariant",
                )
            _var_var_194 = []
            _var_var_195 = []
            _var_var_196 = []
            for _var_var_62 in _var_var_126:
                (
                    _var_var_63,
                    _var_var_116,
                    _var_var_197,
                    _var_var_198,
                    _var_var_199,
                    _var_var_200,
                    _var_var_201,
                    _var_var_202,
                    _var_var_203,
                    _var_var_204,
                    _var_var_205,
                    _var_var_206,
                    _var_var_207,
                    _var_var_208,
                    _var_var_209,
                ) = _var_var_62[:15]
                _var_var_210 = _var_var_62[15] if len(_var_var_62) > 15 else None
                _var_var_211 = residual_weather_mlp_target(_var_var_199, _var_var_210)
                if not _var_var_211 is not None:
                    continue
                _var_var_212 = build_weather_mlp_features(
                    raw_ghi=_var_var_197,
                    cloud_cover=_var_var_200,
                    sun_elevation=_var_var_204,
                    humidity=_var_var_201,
                    pressure=_var_var_202,
                    wind=_var_var_203,
                    hour=_var_var_116,
                    rain=_var_var_205,
                    cloud_low=_var_var_206,
                    cloud_mid=_var_var_207,
                    cloud_high=_var_var_208,
                )
                if not _var_var_212 is not None:
                    continue
                _var_var_194.append(_var_var_212)
                _var_var_195.append(_var_var_211)
                _var_var_196.append(str(_var_var_63))
            if len(_var_var_194) < 50:
                return failed(
                    f"Weather MLP target validation retained {len(_var_var_194)}/{_var_var_185} eligible samples; {_var_var_185 - len(_var_var_194)} solar-factor targets were invalid",
                    "weather_mlp_target_validation_invariant",
                )
            _var_var_213 = held_out_training_shortfall(_var_var_196)
            if _var_var_213 is not None:
                return skipped(
                    f"Weather MLP warm-up: the leanest held-out fold trains on {_var_var_213}/{WEATHER_MLP_MIN_TRAINING_SAMPLES} samples ({len(_var_var_194)} samples over {len(set(_var_var_196))} days)",
                    "weather_mlp_warmup",
                )
            _LOGGER.info(
                "Weather MLP training: %d samples over %d days, %d features",
                len(_var_var_194),
                len(set(_var_var_196)),
                WEATHER_MLP_FEATURE_COUNT,
            )
            _var_var_54 = await self._evaluate_weather_mlp_pooled(
                _var_var_194, _var_var_195, _var_var_196
            )
            if _var_var_54 is None:
                return skipped(
                    f"Weather MLP needs at least two training days for a held-out verdict ({len(set(_var_var_196))} available)",
                    "weather_mlp_insufficient_days",
                )
            _var_var_214 = weather_mlp_persist_verdict(_var_var_54)
            if _var_var_214 is not None:
                _var_var_23 = f"Weather MLP {_var_var_214}"
                _var_var_5 = (
                    _LOGGER.info
                    if self._weather_mlp_retrain_rejected
                    else _LOGGER.warning
                )
                _var_var_5(
                    "%s; weights not persisted, serving state unchanged (pooled RMSE %.4f vs static %.4f over %d held-out samples)",
                    _var_var_23,
                    _var_var_54["rmse"],
                    _var_var_54["baseline_rmse"],
                    _var_var_54["samples"],
                )
                self._weather_mlp_retrain_rejected = True
                return {
                    "status": "skipped",
                    "reason": _var_var_23,
                    "reason_code": "weather_mlp_governance_filtered",
                    "impact": "informational",
                }
            _var_var_215 = self._spawn_weather_mlp()
            _var_var_53 = await _var_var_215.train(
                X_data=_var_var_194,
                y_targets=_var_var_195,
                epochs=50,
                validation_split=0.2,
                early_stopping_patience=10,
            )
            if not _var_var_53.get("success"):
                _var_var_23 = (
                    _var_var_53.get("error_message")
                    or "trainer reported unsuccessful completion"
                )
                _LOGGER.warning("Weather MLP training failed: %s", _var_var_23)
                return {"status": "failed", "reason": _var_var_23}
            _var_var_45 = _var_var_215.get_weights()
            _var_var_45["accuracy"] = float(_var_var_54["r2"])
            _var_var_45["rmse"] = float(_var_var_54["rmse"])
            _var_var_45["training_samples"] = int(
                _var_var_53.get("training_samples") or len(_var_var_194)
            )
            await self.db.save_weather_mlp_weights(
                _var_var_45, training_contract_version=_var_var_173
            )
            self._weather_mlp.set_weights(_var_var_45)
            self._weather_mlp_retrain_rejected = False
            self._weather_mlp_weights_loaded = True
            self._attach_mlp_to_correctors()
            _LOGGER.info(
                "Weather MLP trained and saved: pooled R²=%.3f, skill vs static=%+.3f, RMSE=%.4f (static %.4f), %d samples over %d days",
                _var_var_54["r2"],
                _var_var_54["skill"],
                _var_var_54["rmse"],
                _var_var_54["baseline_rmse"],
                _var_var_45["training_samples"],
                len(set(_var_var_196)),
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error("Weather MLP training error: %s", _var_var_16, exc_info=True)
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _collect_sensor_data(self) -> Dict[str, Any]:
        """Collect current sensor data. @zara"""
        _var_var_155 = {}
        _var_var_105 = self._get_sensor_config()
        _var_var_216 = {
            "temp_sensor": "temperature_c",
            "humidity_sensor": "humidity_percent",
            "pressure_sensor": "pressure_hpa",
            "wind_sensor": "wind_speed_ms",
            "rain_sensor": "rain_mm",
            "solar_radiation_sensor": "solar_radiation_wm2",
            "lux_sensor": "lux",
        }
        for _var_var_217, _var_var_218 in _var_var_105.items():
            if _var_var_217 not in _var_var_216:
                continue
            if _var_var_218 and isinstance(_var_var_218, str):
                _var_var_43 = self.hass.states.get(_var_var_218)
                if _var_var_43 and _var_var_43.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        _var_var_219 = _var_var_216[_var_var_217]
                        _var_var_155[_var_var_219] = float(_var_var_43.state)
                    except (ValueError, TypeError):
                        pass
        _var_var_220 = _var_var_105.get("solar_to_battery_sensor")
        if _var_var_220 and isinstance(_var_var_220, str):
            _var_var_43 = self.hass.states.get(_var_var_220)
            if _var_var_43 and _var_var_43.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    _var_var_155["solar_to_battery_power"] = float(_var_var_43.state)
                except (ValueError, TypeError):
                    pass
        _var_var_221 = _var_var_105.get("battery_soc_sensor")
        if _var_var_221 and isinstance(_var_var_221, str):
            _var_var_43 = self.hass.states.get(_var_var_221)
            if _var_var_43 and _var_var_43.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    _var_var_155["battery_soc"] = float(_var_var_43.state)
                except (ValueError, TypeError):
                    pass
        if True:
            return _var_var_155

    async def _finalize_day_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Finalize current day with actual values. @zara"""
        try:
            _var_var_280 = await self._resolve_eod_actual_yield(now)
            _var_var_281 = []
            _var_var_282 = None
            _var_var_283 = getattr(self.coordinator, "total_consumption_today", None)
            if _var_var_283:
                _var_var_284 = self.hass.states.get(_var_var_283)
                if _var_var_284 and _var_var_284.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        _var_var_282 = float(_var_var_284.state)
                    except (ValueError, TypeError):
                        _var_var_281.append(
                            f"consumption sensor {_var_var_283} has no numeric value"
                        )
            _var_var_285 = 0
            try:
                if (
                    hasattr(self.coordinator, "production_time_calculator")
                    and self.coordinator.production_time_calculator
                ):
                    _var_var_286 = (
                        self.coordinator.production_time_calculator.get_production_hours()
                    )
                    if _var_var_286 is not None:
                        _var_var_285 = int(_var_var_286 * 3600)
            except Exception as _var_var_16:
                raise RuntimeError(
                    "production-time calculation failed"
                ) from _var_var_16
            _var_var_287 = None
            try:
                _var_var_133 = now.strftime("%Y-%m-%d")
                _var_var_126 = await self.db.fetchall(
                    "SELECT exclude_from_learning, has_weather_alert, inverter_clipped,\n                              weather_alert_type, mppt_throttled\n                       FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_133,),
                )
                if _var_var_126:
                    _var_var_288 = 0
                    _var_var_289 = len(_var_var_126)
                    _var_var_290 = {}
                    for _var_var_62 in _var_var_126:
                        _var_var_291 = _var_var_62[0]
                        _var_var_292 = _var_var_62[1]
                        _var_var_293 = _var_var_62[2]
                        _var_var_294 = _var_var_62[3]
                        _var_var_295 = _var_var_62[4]
                        if _var_var_291 or _var_var_292 or _var_var_293 or _var_var_295:
                            _var_var_288 += 1
                            _var_var_23 = _var_var_294 or "other"
                            if _var_var_293:
                                _var_var_23 = "inverter_clipped"
                            elif _var_var_295:
                                _var_var_23 = "mppt_throttled"
                            _var_var_290[_var_var_23] = (
                                _var_var_290.get(_var_var_23, 0) + 1
                            )
                    _var_var_296 = (
                        _var_var_288 / _var_var_289 if _var_var_289 > 0 else 0.0
                    )
                    _var_var_287 = {
                        "count": _var_var_288,
                        "total": _var_var_289,
                        "ratio": round(_var_var_296, 3),
                        "reasons": _var_var_290 if _var_var_290 else None,
                    }
                    if _var_var_288 > 0:
                        _LOGGER.info(
                            f"Excluded hours info for {_var_var_133}: {_var_var_288}/{_var_var_289} ({_var_var_296:.1%}), reasons: {_var_var_290}"
                        )
            except Exception as _var_var_16:
                raise RuntimeError("excluded-hours calculation failed") from _var_var_16
            _var_var_297 = None
            _var_var_298 = None
            _var_var_299 = 0.0
            _var_var_300 = 0.0
            _var_var_301 = 0.0
            _var_var_302 = 0.0
            _var_var_303 = 0
            _var_var_304 = 0.0
            _var_var_305 = await self.db.fetchone(
                f"SELECT\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                               {CLEAN_EVALUATION_ELIGIBILITY_SQL}\n                               THEN prediction_kwh\n                               ELSE 0\n                           END) as evaluation_predicted,\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                               {CLEAN_EVALUATION_ELIGIBILITY_SQL}\n                               THEN actual_kwh\n                               ELSE 0\n                           END) as evaluation_actual,\n                       SUM(CASE WHEN actual_kwh IS NOT NULL THEN prediction_kwh ELSE 0 END),\n                       SUM(CASE WHEN actual_kwh IS NOT NULL THEN actual_kwh ELSE 0 END),\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                                AND (\n                                    COALESCE(mppt_throttled, 0) != 0\n                                    OR COALESCE(mppt_throttle_reason, '') IN ({', '.join((repr(_var_var_308) for _var_var_308 in sorted(CURTAILMENT_THROTTLE_REASONS)))})\n                                )\n                               THEN 1 ELSE 0\n                           END),\n                       SUM(CASE\n                               WHEN actual_kwh IS NOT NULL\n                                AND (\n                                    COALESCE(mppt_throttled, 0) != 0\n                                    OR COALESCE(mppt_throttle_reason, '') IN ({', '.join((repr(_var_var_308) for _var_var_308 in sorted(CURTAILMENT_THROTTLE_REASONS)))})\n                                )\n                               THEN prediction_kwh - actual_kwh ELSE 0\n                           END)\n                   FROM hourly_predictions hp\n                   WHERE target_date = ?",
                (now.date().isoformat(),),
            )
            if _var_var_305:
                _var_var_299 = float(_var_var_305[0] or 0.0)
                _var_var_300 = float(_var_var_305[1] or 0.0)
                _var_var_301 = float(_var_var_305[2] or 0.0)
                _var_var_302 = float(_var_var_305[3] or 0.0)
                _var_var_303 = int(_var_var_305[4] or 0)
                _var_var_304 = float(_var_var_305[5] or 0.0)
                if _var_var_299 > 0 and _var_var_300 > 0.01:
                    _var_var_100 = abs(_var_var_299 - _var_var_300)
                    _var_var_297 = max(0.0, (1.0 - _var_var_100 / _var_var_299) * 100.0)
                if _var_var_301 > 0 and _var_var_302 > 0.01:
                    _var_var_306 = abs(_var_var_301 - _var_var_302)
                    _var_var_298 = max(0.0, (1.0 - _var_var_306 / _var_var_301) * 100.0)
            _var_var_307 = {
                "date": now.date().isoformat(),
                "finalized": {
                    "yield_kwh": _var_var_280,
                    "consumption_kwh": _var_var_282,
                    "production_hours": (
                        f"{_var_var_285 // 3600}:{_var_var_285 % 3600 // 60:02d}"
                        if _var_var_285 > 0
                        else None
                    ),
                    "accuracy_percent": (
                        round(_var_var_297, 1) if _var_var_297 is not None else None
                    ),
                    "production_quality_percent": (
                        round(_var_var_297, 1) if _var_var_297 is not None else None
                    ),
                    "energy_balance_percent": (
                        round(_var_var_298, 1) if _var_var_298 is not None else None
                    ),
                    "production_predicted_kwh": round(_var_var_299, 3),
                    "production_actual_kwh": round(_var_var_300, 3),
                    "energy_predicted_kwh": round(_var_var_301, 3),
                    "energy_actual_kwh": round(_var_var_302, 3),
                    "curtailed_hours": _var_var_303,
                    "curtailed_kwh": round(_var_var_304, 3),
                    "excluded_hours": _var_var_287 or {},
                    "at": now.isoformat(),
                },
            }
            await self.db.save_daily_forecast_tracking(_var_var_307)
            _LOGGER.info(
                f"Day finalized: yield={_var_var_280:.2f} kWh, consumption={(_var_var_282 if _var_var_282 is not None else 0.0):.2f} kWh, production_quality={(_var_var_297 if _var_var_297 is not None else 0.0):.1f}%, energy_balance={(_var_var_298 if _var_var_298 is not None else 0.0):.1f}%, curtailed={_var_var_303}h/{_var_var_304:.2f} kWh"
            )
            if _var_var_281:
                return {"status": "skipped", "reason": "; ".join(_var_var_281)}
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(f"Failed to finalize day: {_var_var_16}", exc_info=True)
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _move_to_history_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Move current day to forecast history and recover missing history for last 7 days. @zara"""
        try:
            import json as _var_var_309

            _var_var_310 = 0
            for _var_var_52 in range(7):
                _var_var_311 = now - timedelta(days=_var_var_52)
                _var_var_312 = _var_var_311.date().isoformat()
                _var_var_313 = await self.db.fetchone(
                    "SELECT 1 FROM forecast_history WHERE date = ?", (_var_var_312,)
                )
                if _var_var_313 and _var_var_52 > 0:
                    continue
                _var_var_314 = None
                if _var_var_52 == 0:
                    _var_var_314 = await self.db.get_daily_forecast_tracking()
                    if not _var_var_314 or _var_var_314.get("date") != _var_var_312:
                        _var_var_314 = None
                if _var_var_314:
                    _var_var_315 = _var_var_314.get("finalized", {})
                    if not _var_var_315:
                        _LOGGER.warning(
                            f"Day {_var_var_312} not finalized, skipping history move"
                        )
                        continue
                    _var_var_316 = await self.db.fetchone(
                        "SELECT prediction_kwh, source FROM daily_forecasts\n                           WHERE forecast_type = 'today' AND forecast_date = ?",
                        (_var_var_312,),
                    )
                    _var_var_317 = _var_var_316[0] if _var_var_316 else None
                    _var_var_150 = _var_var_316[1] if _var_var_316 else None
                    _var_var_318 = {
                        "date": _var_var_312,
                        "predicted_kwh": _var_var_317,
                        "actual_kwh": _var_var_315.get("yield_kwh"),
                        "consumption_kwh": _var_var_315.get("consumption_kwh"),
                        "autarky": None,
                        "accuracy": _var_var_315.get("accuracy_percent"),
                        "production_hours": _var_var_315.get("production_hours"),
                        "peak_power": _var_var_314.get("peak_today", {}).get("power_w"),
                        "source": _var_var_150,
                        "excluded_hours": _var_var_315.get("excluded_hours"),
                        "archived_at": now.isoformat(),
                    }
                else:
                    _var_var_319 = await self.db.fetchone(
                        "SELECT predicted_total_kwh, actual_total_kwh, accuracy_percent, production_hours, \n                                  peak_power_w, excluded_hours_count, excluded_reason_breakdown_json\n                           FROM daily_summaries WHERE date = ?",
                        (_var_var_312,),
                    )
                    if not _var_var_319:
                        if _var_var_52 > 0:
                            _LOGGER.debug(
                                f"Recovery: No daily summary found for {_var_var_312}, skipping"
                            )
                        else:
                            _LOGGER.warning(
                                f"No summary or tracking for today {_var_var_312}, skipping history move"
                            )
                        continue
                    (
                        _var_var_317,
                        _var_var_320,
                        _var_var_321,
                        _var_var_322,
                        _var_var_323,
                        _var_var_324,
                        _var_var_325,
                    ) = _var_var_319
                    if _var_var_317 is None:
                        _var_var_326 = await self.db.fetchone(
                            "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                            (_var_var_312,),
                        )
                        _var_var_317 = (
                            float(_var_var_326[0])
                            if _var_var_326 and _var_var_326[0] is not None
                            else None
                        )
                    if _var_var_320 is None:
                        _var_var_327 = await self.db.fetchone(
                            "SELECT SUM(actual_kwh) FROM hourly_predictions WHERE target_date = ? AND actual_kwh IS NOT NULL",
                            (_var_var_312,),
                        )
                        _var_var_320 = (
                            float(_var_var_327[0])
                            if _var_var_327 and _var_var_327[0] is not None
                            else None
                        )
                    if _var_var_321 is None and _var_var_317 and _var_var_320:
                        _var_var_321 = max(
                            0.0,
                            min(
                                100.0,
                                100.0
                                - abs(_var_var_317 - _var_var_320)
                                / _var_var_317
                                * 100.0,
                            ),
                        )
                    if _var_var_322 is None:
                        _var_var_328 = await self.db.fetchone(
                            "SELECT COUNT(*) FROM hourly_predictions WHERE target_date = ? AND actual_kwh > 0.001",
                            (_var_var_312,),
                        )
                        _var_var_322 = (
                            int(_var_var_328[0])
                            if _var_var_328 and _var_var_328[0] is not None
                            else 0
                        )
                    _var_var_329 = {}
                    if _var_var_325:
                        try:
                            _var_var_329 = _var_var_309.loads(_var_var_325)
                        except Exception as _var_var_16:
                            raise ValueError(
                                f"invalid exclusion reason data for {_var_var_312}"
                            ) from _var_var_16
                    _var_var_330 = (
                        _var_var_324 / 24.0 if _var_var_324 is not None else 0.0
                    )
                    _var_var_316 = await self.db.fetchone(
                        "SELECT source FROM daily_forecasts\n                           WHERE forecast_type = 'today' AND forecast_date = ?",
                        (_var_var_312,),
                    )
                    _var_var_150 = (
                        _var_var_316[0] if _var_var_316 else "adaptive_correction"
                    )
                    _var_var_318 = {
                        "date": _var_var_312,
                        "predicted_kwh": _var_var_317,
                        "actual_kwh": _var_var_320,
                        "consumption_kwh": None,
                        "autarky": None,
                        "accuracy": _var_var_321,
                        "production_hours": _var_var_322,
                        "peak_power": _var_var_323,
                        "source": _var_var_150,
                        "excluded_hours": {
                            "count": _var_var_324 or 0,
                            "total": 24,
                            "ratio": _var_var_330,
                            "reasons": _var_var_329,
                        },
                        "archived_at": now.isoformat(),
                    }
                    _LOGGER.info(
                        f"Recovery: Reconstructed history entry for {_var_var_312}"
                    )
                _var_var_249 = _var_var_318["actual_kwh"] or 0
                _var_var_331 = _var_var_318["consumption_kwh"] or 0
                if _var_var_331 > 0:
                    _var_var_318["autarky"] = round(
                        _var_var_249 / _var_var_331 * 100, 1
                    )
                await self.db.save_forecast_history_entry(_var_var_318)
                _var_var_310 += 1
                if _var_var_52 == 0:
                    _LOGGER.info(
                        "Day %s moved to history: %.2f → %.2f kWh",
                        _var_var_312,
                        _var_var_318["predicted_kwh"] or 0.0,
                        _var_var_249,
                    )
            if _var_var_310 == 0:
                _var_var_23 = "no finalized or recoverable daily data available"
                _LOGGER.info("History move skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Failed to move to history/recover: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _calculate_stats_internal(self, now: datetime) -> Dict[str, str]:
        """Internal: Calculate statistics (all-time peak, current week/month, last N days). @zara"""
        try:
            _var_var_336 = {}
            _var_var_337 = await self.db.fetchone(
                "SELECT MAX(peak_power), date FROM forecast_history\n                   WHERE peak_power IS NOT NULL"
            )
            if _var_var_337 and _var_var_337[0]:
                _var_var_336["all_time_peak"] = {
                    "power_w": _var_var_337[0],
                    "date": str(_var_var_337[1]) if _var_var_337[1] else None,
                    "at": None,
                }
            _var_var_66 = now.date()
            _var_var_338 = _var_var_66 - timedelta(days=_var_var_66.weekday())
            _var_var_339 = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (_var_var_338.isoformat(), _var_var_66.isoformat()),
            )
            if _var_var_339:
                _var_var_340 = sum(
                    (_var_var_62[0] or 0 for _var_var_62 in _var_var_339)
                )
                _var_var_341 = sum(
                    (
                        _var_var_62[1] or 0
                        for _var_var_62 in _var_var_339
                        if _var_var_62[1]
                    )
                )
                _var_var_336["current_week"] = {
                    "period": f"KW {_var_var_66.isocalendar()[1]}",
                    "date_range": f"{_var_var_338.strftime('%d.%m')} - {_var_var_66.strftime('%d.%m')}",
                    "yield_kwh": round(_var_var_340, 2),
                    "consumption_kwh": (
                        round(_var_var_341, 2) if _var_var_341 > 0 else None
                    ),
                    "days": len(_var_var_339),
                    "updated_at": now.isoformat(),
                }
            _var_var_342 = _var_var_66.replace(day=1)
            _var_var_343 = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh, autarky FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (_var_var_342.isoformat(), _var_var_66.isoformat()),
            )
            if _var_var_343:
                _var_var_344 = sum(
                    (_var_var_62[0] or 0 for _var_var_62 in _var_var_343)
                )
                _var_var_345 = sum(
                    (
                        _var_var_62[1] or 0
                        for _var_var_62 in _var_var_343
                        if _var_var_62[1]
                    )
                )
                _var_var_346 = [
                    _var_var_62[2]
                    for _var_var_62 in _var_var_343
                    if _var_var_62[2] is not None
                ]
                _var_var_347 = (
                    sum(_var_var_346) / len(_var_var_346) if _var_var_346 else None
                )
                _var_var_336["current_month"] = {
                    "period": _var_var_66.strftime("%B %Y"),
                    "yield_kwh": round(_var_var_344, 2),
                    "consumption_kwh": (
                        round(_var_var_345, 2) if _var_var_345 > 0 else None
                    ),
                    "avg_autarky": round(_var_var_347, 1) if _var_var_347 else None,
                    "days": len(_var_var_343),
                    "updated_at": now.isoformat(),
                }
            _var_var_348 = (_var_var_66 - timedelta(days=7)).isoformat()
            _var_var_349 = await self.db.get_daily_summary_statistics(
                start_date=_var_var_348, end_date=_var_var_66.isoformat()
            )
            if _var_var_349["yield_days"]:
                _var_var_336["last_7_days"] = {
                    "avg_yield_kwh": round(_var_var_349["avg_yield_kwh"], 2),
                    "avg_accuracy": (
                        round(_var_var_349["avg_accuracy"], 1)
                        if _var_var_349["avg_accuracy"] is not None
                        else None
                    ),
                    "avg_evaluation_coverage_percent": (
                        round(_var_var_349["avg_evaluation_coverage_percent"], 1)
                        if _var_var_349["avg_evaluation_coverage_percent"] is not None
                        else None
                    ),
                    "avg_excluded_mppt_hours": (
                        round(_var_var_349["avg_excluded_mppt_hours"], 2)
                        if _var_var_349["avg_excluded_mppt_hours"] is not None
                        else None
                    ),
                    "total_yield_kwh": round(_var_var_349["total_yield_kwh"], 2),
                    "evaluation_days": _var_var_349["evaluation_days"],
                    "calculated_at": now.isoformat(),
                }
            _var_var_350 = (_var_var_66 - timedelta(days=30)).isoformat()
            _var_var_351 = await self.db.get_daily_summary_statistics(
                start_date=_var_var_350, end_date=_var_var_66.isoformat()
            )
            if _var_var_351["yield_days"]:
                _var_var_336["last_30_days"] = {
                    "avg_yield_kwh": round(_var_var_351["avg_yield_kwh"], 2),
                    "avg_accuracy": (
                        round(_var_var_351["avg_accuracy"], 1)
                        if _var_var_351["avg_accuracy"] is not None
                        else None
                    ),
                    "avg_evaluation_coverage_percent": (
                        round(_var_var_351["avg_evaluation_coverage_percent"], 1)
                        if _var_var_351["avg_evaluation_coverage_percent"] is not None
                        else None
                    ),
                    "avg_excluded_mppt_hours": (
                        round(_var_var_351["avg_excluded_mppt_hours"], 2)
                        if _var_var_351["avg_excluded_mppt_hours"] is not None
                        else None
                    ),
                    "total_yield_kwh": round(_var_var_351["total_yield_kwh"], 2),
                    "evaluation_days": _var_var_351["evaluation_days"],
                    "calculated_at": now.isoformat(),
                }
            _var_var_352 = (_var_var_66 - timedelta(days=365)).isoformat()
            _var_var_353 = await self.db.fetchall(
                "SELECT actual_kwh FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (_var_var_352, _var_var_66.isoformat()),
            )
            if _var_var_353:
                _var_var_354 = sum(
                    (_var_var_62[0] or 0 for _var_var_62 in _var_var_353)
                )
                _var_var_336["last_365_days"] = {
                    "avg_yield_kwh": round(_var_var_354 / len(_var_var_353), 2),
                    "total_yield_kwh": round(_var_var_354, 2),
                    "calculated_at": now.isoformat(),
                }
            if not _var_var_336:
                _var_var_23 = f"no forecast history or summary statistics available for {_var_var_66}"
                _LOGGER.info("Statistics calculation skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            await self.db.save_daily_statistics(_var_var_336)
            _LOGGER.info("Statistics calculated successfully")
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Failed to calculate statistics: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _correct_curtailment_learning_exclusions(self, now: datetime) -> int:
        """Retroactively quarantine flow-proven curtailment before learning uses it."""
        try:
            _var_var_366 = self.db
            _var_var_370 = await _var_var_366.fetchone(
                f"SELECT COUNT(*)\n                    FROM hourly_predictions\n                    WHERE COALESCE(mppt_throttle_reason, '') IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                      AND COALESCE(exclude_from_learning, 0) = 0"
            )
            _var_var_371 = int(_var_var_370[0] or 0) if _var_var_370 else 0
            if _var_var_371 > 0:
                await _var_var_366.execute(
                    f"UPDATE hourly_predictions\n                           SET exclude_from_learning = 1,\n                               exclude_from_clean_evaluation = 1\n                         WHERE COALESCE(mppt_throttle_reason, '') IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                           AND COALESCE(exclude_from_learning, 0) = 0"
                )
                await _var_var_366.commit()
                _LOGGER.info(
                    "Throttle-reason learning quarantine: %d hours excluded from learning",
                    _var_var_371,
                )
            _var_var_66 = now.date().isoformat()
            _var_var_367 = (now - timedelta(days=14)).date().isoformat()
            _var_var_105 = self._get_sensor_config()
            _var_var_372 = bool(_var_var_105.get("zero_export_mode", False))
            _var_var_373 = bool(_var_var_105.get("has_battery", False))
            _var_var_374 = _var_var_105.get(
                "max_grid_export_w", DEFAULT_MAX_GRID_EXPORT_W
            )
            _var_var_375 = MorningRoutineHandler._effective_export_limit_w(
                _var_var_374, _var_var_372
            )
            _var_var_376 = MorningRoutineHandler._cap_tolerance_w(_var_var_375)
            _var_var_126 = await _var_var_366.fetchall(
                "WITH flow_hour AS (\n                       SELECT date,\n                              hour,\n                              COUNT(*) AS samples,\n                              SUM(CASE WHEN solar_to_battery_w <= ? THEN 1 ELSE 0 END) AS low_battery_samples,\n                              SUM(CASE\n                                      WHEN solar_to_battery_w <= ?\n                                       AND solar_to_grid_w <= ?\n                                       AND house_consumption_w > ?\n                                       AND solar_to_house_w >= house_consumption_w * ?\n                                      THEN 1 ELSE 0\n                                  END) AS house_match_samples,\n                              SUM(CASE\n                                      WHEN house_consumption_w > ?\n                                       AND (\n                                           solar_to_house_w >= house_consumption_w * ?\n                                           OR grid_to_house_w <= ?\n                                       )\n                                      THEN 1 ELSE 0\n                                  END) AS house_covered_samples,\n                              SUM(CASE\n                                      WHEN ? > 0\n                                       AND (\n                                           ABS(solar_power_w - ?) <= ?\n                                           OR ABS(solar_to_grid_w - ?) <= ?\n                                           OR ABS(solar_power_w - (house_consumption_w + ?)) <= ?\n                                       )\n                                      THEN 1 ELSE 0\n                                  END) AS cap_limit_samples,\n                              AVG(solar_to_battery_w) AS avg_battery_w,\n                              AVG(solar_power_w) AS avg_solar_w,\n                              AVG(house_consumption_w) AS avg_house_w,\n                              AVG(solar_to_house_w) AS avg_solar_to_house_w,\n                              AVG(solar_to_grid_w) AS avg_solar_to_grid_w,\n                              MAX(battery_soc) AS max_battery_soc,\n                              AVG(battery_soc) AS avg_battery_soc\n                       FROM stats_power_sources\n                       GROUP BY date, hour\n                   )\n                   SELECT hp.prediction_id, hp.target_date, hp.target_hour,\n                          hp.actual_kwh, pa.theoretical_max_kwh,\n                          pw.solar_radiation_wm2, pw.clouds,\n                          fh.samples, fh.low_battery_samples,\n                          fh.house_match_samples, fh.house_covered_samples,\n                          fh.cap_limit_samples, fh.avg_battery_w,\n                          fh.avg_solar_w, fh.avg_house_w,\n                          fh.avg_solar_to_house_w, fh.avg_solar_to_grid_w,\n                          fh.max_battery_soc, fh.avg_battery_soc\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON pa.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw\n                          ON pw.prediction_id = hp.prediction_id\n                         AND pw.weather_type = 'actual'\n                   LEFT JOIN flow_hour fh\n                          ON fh.date = hp.target_date\n                         AND fh.hour = hp.target_hour\n                   WHERE hp.target_date >= ?\n                     AND hp.target_date <= ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0.01\n                     AND hp.prediction_kwh > 0.05\n                     AND pa.theoretical_max_kwh > 0\n                     AND COALESCE(hp.exclude_from_learning, 0) = 0\n                     AND COALESCE(hp.mppt_throttled, 0) = 0\n                     AND COALESCE(hp.inverter_clipped, 0) = 0",
                (
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    _var_var_375,
                    _var_var_375,
                    _var_var_376,
                    _var_var_375,
                    _var_var_376,
                    _var_var_375,
                    _var_var_376,
                    _var_var_367,
                    _var_var_66,
                ),
            )
            _var_var_266 = 0
            for _var_var_62 in _var_var_126:
                _var_var_368 = _var_var_62[0]
                _var_var_238 = _var_var_62[1]
                _var_var_116 = int(_var_var_62[2] or 0)
                _var_var_320 = float(_var_var_62[3] or 0.0)
                _var_var_369 = float(_var_var_62[4] or 0.0)
                _var_var_377 = _var_var_62[5]
                _var_var_378 = _var_var_62[6]
                _var_var_379 = {
                    "samples": int(_var_var_62[7] or 0),
                    "low_battery_samples": int(_var_var_62[8] or 0),
                    "house_match_samples": int(_var_var_62[9] or 0),
                    "house_covered_samples": int(_var_var_62[10] or 0),
                    "cap_limit_samples": int(_var_var_62[11] or 0),
                    "avg_battery_w": float(_var_var_62[12] or 0.0),
                    "avg_solar_w": float(_var_var_62[13] or 0.0),
                    "avg_house_w": float(_var_var_62[14] or 0.0),
                    "avg_solar_to_house_w": float(_var_var_62[15] or 0.0),
                    "avg_solar_to_grid_w": float(_var_var_62[16] or 0.0),
                    "max_battery_soc": (
                        float(_var_var_62[17]) if _var_var_62[17] is not None else None
                    ),
                    "avg_battery_soc": (
                        float(_var_var_62[18]) if _var_var_62[18] is not None else None
                    ),
                    "export_limit_w": _var_var_375,
                    "cap_tolerance_w": _var_var_376,
                }
                _var_var_156 = {
                    "solar_radiation_wm2": (
                        float(_var_var_377) if _var_var_377 is not None else None
                    ),
                    "cloud_cover_percent": (
                        float(_var_var_378) if _var_var_378 is not None else None
                    ),
                }
                _var_var_104 = {"theoretical_max_kwh": _var_var_369}
                _var_var_380 = MorningRoutineHandler.evaluate_curtailment_state(
                    actual_kwh=_var_var_320,
                    solar_to_battery_power=None,
                    weather_actual=_var_var_156,
                    astronomy_data=_var_var_104,
                    flow_evidence=_var_var_379,
                    max_grid_export_w=_var_var_374,
                    zero_export_mode=_var_var_372,
                    has_battery=_var_var_373,
                )
                if not _var_var_380["quarantine"]:
                    continue
                _var_var_23 = _var_var_380["reason"]
                _var_var_381 = 1 if _var_var_380["hard"] else 0
                await _var_var_366.execute(
                    "UPDATE hourly_predictions\n                       SET exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           mppt_throttled = CASE WHEN ? = 1 THEN 1 ELSE mppt_throttled END,\n                           mppt_throttle_reason = COALESCE(mppt_throttle_reason, ?)\n                       WHERE prediction_id = ?\n                         AND COALESCE(exclude_from_learning, 0) = 0",
                    (_var_var_381, _var_var_23, _var_var_368),
                )
                _var_var_266 += 1
                _LOGGER.info(
                    "Curtailment learning quarantine %s H%02d: actual=%.3f kWh, theoretical=%.3f kWh, reason=%s",
                    _var_var_238,
                    _var_var_116,
                    _var_var_320,
                    _var_var_369,
                    _var_var_23,
                )
                if _var_var_266 % 10 == 0:
                    await asyncio.sleep(0)
            if _var_var_266 > 0:
                await _var_var_366.commit()
                _LOGGER.info(
                    "Retroactively quarantined %d curtailment-contaminated learning hours",
                    _var_var_266,
                )
            return _var_var_266 + _var_var_371
        except Exception as _var_var_16:
            _LOGGER.warning(
                "Curtailment learning exclusion correction failed: %s", _var_var_16
            )
            return 0

    async def _run_daily_data_hygiene_audit(self, current_time: datetime) -> None:
        """Run nightly data hygiene audit for the day just ending. @zara"""
        try:
            _var_var_366 = self.db
            _var_var_66 = current_time.date().isoformat()
            _var_var_382 = getattr(self.coordinator, "solar_capacity", 0.0) or 0.0
            if _var_var_382 <= 0.0:
                _var_var_232 = getattr(self.coordinator, "panel_groups", [])
                _var_var_382 = sum(
                    (
                        get_group_capacity_kwp(_var_var_394)
                        for _var_var_394 in _var_var_232
                    )
                )
            if _var_var_382 <= 0.0:
                _var_var_382 = 5.0
            _var_var_126 = await _var_var_366.fetchall(
                "SELECT target_hour, actual_kwh, physics_kwh, prediction_id\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (_var_var_66,),
            )
            if not _var_var_126:
                return
            _var_var_383 = [
                float(_var_var_308["actual_kwh"])
                for _var_var_308 in _var_var_126
                if _var_var_308["actual_kwh"] is not None
            ]
            _var_var_384 = sum(_var_var_383)
            if _var_var_384 > _var_var_382 * 8.5:
                Hubble.warning(
                    f"Auffällig hoher Tagesertrag für den {_var_var_66} erkannt ({_var_var_384:.2f} kWh bei {_var_var_382:.2f} kWp installierter Leistung). Um die Genauigkeit der Prognose-KI nicht zu gefährden, wurden alle Stunden dieses Tages vom Modelltraining und der Clean-Evaluation ausgeschlossen."
                )
                await _var_var_366.execute(
                    "UPDATE hourly_predictions\n                       SET exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           mppt_throttle_reason = COALESCE(mppt_throttle_reason, 'unplausible_daily_yield_spike')\n                       WHERE target_date = ?",
                    (_var_var_66,),
                )
                await _var_var_366.execute(
                    "UPDATE prediction_panel_groups\n                       SET exclude_from_learning_group = 1,\n                           exclusion_reason_group = COALESCE(exclusion_reason_group, 'unplausible_daily_yield_spike')\n                       WHERE prediction_id IN (\n                           SELECT prediction_id FROM hourly_predictions WHERE target_date = ?\n                       )",
                    (_var_var_66,),
                )
                await _var_var_366.commit()
                return
            _var_var_385 = set()
            if hasattr(_var_var_366, "is_actual_backfill_blocked"):
                for _var_var_308 in _var_var_126:
                    _var_var_116 = int(_var_var_308["target_hour"] or 0)
                    if await _var_var_366.is_actual_backfill_blocked(
                        _var_var_66, _var_var_116
                    ):
                        _var_var_385.add(_var_var_116)
            if _var_var_385:
                _LOGGER.warning(
                    "EOD actual reconstruction skipped for emergency-quarantined hours on %s: %s",
                    _var_var_66,
                    sorted(_var_var_385),
                )
            _var_var_386 = [
                _var_var_308
                for _var_var_308 in _var_var_126
                if _var_var_308["actual_kwh"] is None
                if (_var_var_308["physics_kwh"] or 0.0) > 0.01
                if int(_var_var_308["target_hour"] or 0) not in _var_var_385
            ]
            if _var_var_386:
                _var_var_387 = await self._resolve_eod_actual_yield(current_time)
                _var_var_388 = sum(
                    (
                        max(0.0, float(_var_var_308["actual_kwh"]))
                        for _var_var_308 in _var_var_126
                        if _var_var_308["actual_kwh"] is not None
                    )
                )
                if _var_var_387 is not None:
                    if _var_var_387 > _var_var_388:
                        _var_var_389 = _var_var_387 - _var_var_388
                        _var_var_390 = sum(
                            (
                                float(_var_var_308["physics_kwh"] or 0.0)
                                for _var_var_308 in _var_var_386
                            )
                        )
                        if _var_var_390 <= 0:
                            _var_var_390 = len(_var_var_386)
                            _var_var_45 = {
                                _var_var_308["target_hour"]: 1.0
                                for _var_var_308 in _var_var_386
                            }
                        else:
                            _var_var_45 = {
                                _var_var_308["target_hour"]: float(
                                    _var_var_308["physics_kwh"] or 0.0
                                )
                                for _var_var_308 in _var_var_386
                            }
                        _var_var_391 = 0
                        _var_var_232 = getattr(self.coordinator, "panel_groups", [])
                        _var_var_392 = sum(
                            (
                                get_group_capacity_kwp(_var_var_394)
                                for _var_var_394 in _var_var_232
                            )
                        )
                        for _var_var_308 in _var_var_386:
                            _var_var_116 = _var_var_308["target_hour"]
                            _var_var_393 = _var_var_308["prediction_id"]
                            _var_var_121 = _var_var_45.get(_var_var_116, 1.0)
                            _var_var_122 = round(
                                _var_var_121 / _var_var_390 * _var_var_389, 3
                            )
                            await _var_var_366.execute(
                                "UPDATE hourly_predictions\n                               SET actual_kwh = ?,\n                                   exclude_from_learning = 1,\n                                   exclude_from_clean_evaluation = 1,\n                                   has_sensor_data = 1,\n                                   sensor_data_complete = 0,\n                                   mppt_throttle_reason = COALESCE(mppt_throttle_reason, 'reconstructed_gap_interpolation')\n                               WHERE prediction_id = ?",
                                (_var_var_122, _var_var_393),
                            )
                            for _var_var_394 in _var_var_232:
                                _var_var_395 = _var_var_394.get("name")
                                if not _var_var_395:
                                    continue
                                _var_var_396 = get_group_capacity_kwp(_var_var_394)
                                _var_var_397 = (
                                    _var_var_396 / _var_var_392
                                    if _var_var_392 > 0
                                    else 1.0 / len(_var_var_232)
                                )
                                _var_var_398 = round(_var_var_122 * _var_var_397, 3)
                                await _var_var_366.execute(
                                    "UPDATE prediction_panel_groups\n                                   SET actual_kwh = ?,\n                                       exclude_from_learning_group = 1,\n                                       exclusion_reason_group = COALESCE(exclusion_reason_group, 'reconstructed_gap_interpolation')\n                                   WHERE prediction_id = ? AND group_name = ?",
                                    (_var_var_398, _var_var_393, _var_var_395),
                                )
                            _var_var_391 += 1
                        if _var_var_391 > 0:
                            await _var_var_366.execute(
                                "UPDATE hourly_predictions\n                               SET has_panel_group_actuals = TRUE\n                               WHERE target_date = ? AND prediction_id IN (\n                                   SELECT prediction_id FROM hourly_predictions WHERE target_date = ?\n                               )",
                                (_var_var_66, _var_var_66),
                            )
                            await _var_var_366.commit()
                            Hubble.info(
                                f"Datenlücke von {_var_var_391} Stunden am {_var_var_66} erkannt. Der Ausfall wurde über die physikalische Erwartungskurve mit insgesamt {_var_var_389:.3f} kWh rekonstruiert und für das KI-Training gesperrt."
                            )
        except Exception as _var_var_16:
            _LOGGER.warning("Daily data hygiene audit failed: %s", _var_var_16)
        if True:
            return None

    async def _learn_method_performance(self, now: datetime) -> Dict[str, str]:
        """Learn physics vs AI accuracy per condition bucket. @zara V16.0.0

        Compares physics_kwh, ai_kwh, prediction_kwh (blend) against actual_kwh
        for each hour of the day. Updates method_performance_learning table
        with smoothed MAE values and derived ai_advantage_factor.
        """
        try:
            if not self.coordinator or not self.coordinator.data_manager:
                _var_var_23 = "no data manager available"
                _LOGGER.info("Method performance learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_366 = self.db
            _var_var_66 = now.date().isoformat()
            _var_var_126 = await _var_var_366.fetchall(
                f"SELECT hp.target_hour,\n                          CASE WHEN hp.adaptive_corrected = TRUE\n                               THEN hp.prediction_kwh_uncapped\n                               ELSE hp.prediction_kwh END AS blend_kwh,\n                          hp.actual_kwh,\n                          hp.physics_kwh, hp.ai_kwh, hp.ai_confidence,\n                          pw.clouds\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL\n                     {HOURLY_LEARNING_ELIGIBILITY_SQL}",
                (_var_var_66,),
            )
            if not _var_var_126:
                _var_var_23 = f"no eligible raw prediction data for {_var_var_66}"
                _LOGGER.info("Method performance learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_399 = {}
            for _var_var_86, _var_var_62 in enumerate(_var_var_126):
                _var_var_116 = _var_var_62[0]
                _var_var_400 = _var_var_62[1]
                _var_var_249 = _var_var_62[2]
                _var_var_401 = _var_var_62[3]
                _var_var_402 = _var_var_62[4]
                _var_var_378 = _var_var_62[6] or 50
                if _var_var_249 is None or _var_var_249 <= 0:
                    continue
                _var_var_403 = (
                    "clear"
                    if _var_var_378 < 30
                    else "partly_cloudy" if _var_var_378 < 70 else "overcast"
                )
                _var_var_404 = (
                    "morning"
                    if _var_var_116 < 10
                    else "midday" if _var_var_116 < 15 else "afternoon"
                )
                _var_var_73 = (_var_var_403, _var_var_404)
                if _var_var_73 not in _var_var_399:
                    _var_var_399[_var_var_73] = {"physics": [], "ai": [], "blend": []}
                _var_var_399[_var_var_73]["physics"].append(
                    abs(_var_var_401 - _var_var_249)
                )
                _var_var_399[_var_var_73]["blend"].append(
                    abs(_var_var_400 - _var_var_249)
                )
                if _var_var_402 is not None and _var_var_402 > 0:
                    _var_var_399[_var_var_73]["ai"].append(
                        abs(_var_var_402 - _var_var_249)
                    )
                if _var_var_86 % 50 == 0:
                    await asyncio.sleep(0)
            if not _var_var_399:
                _var_var_23 = f"no valid production hours for {_var_var_66}"
                _LOGGER.info("Method performance learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_405 = 0.3
            _var_var_406 = get_season(now.month)
            async with _var_var_366.transaction():
                for (_var_var_403, _var_var_404), _var_var_257 in _var_var_399.items():
                    _var_var_407 = sum(_var_var_257["physics"]) / len(
                        _var_var_257["physics"]
                    )
                    _var_var_408 = sum(_var_var_257["blend"]) / len(
                        _var_var_257["blend"]
                    )
                    _var_var_409 = (
                        sum(_var_var_257["ai"]) / len(_var_var_257["ai"])
                        if _var_var_257["ai"]
                        else None
                    )
                    for _var_var_410 in (None, _var_var_406):
                        _var_var_411 = await _var_var_366.fetchone(
                            "SELECT physics_mae, ai_mae, blend_mae, sample_count\n                               FROM method_performance_learning\n                               WHERE cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (_var_var_403, _var_var_404, _var_var_410),
                        )
                        if _var_var_411 and _var_var_411[3] and (_var_var_411[3] > 0):
                            _var_var_412 = (
                                _var_var_405 * _var_var_407
                                + (1 - _var_var_405) * _var_var_411[0]
                            )
                            _var_var_413 = (
                                _var_var_405 * _var_var_409
                                + (1 - _var_var_405) * _var_var_411[1]
                                if _var_var_409 and _var_var_411[1]
                                else _var_var_409
                            )
                            _var_var_414 = (
                                _var_var_405 * _var_var_408
                                + (1 - _var_var_405) * _var_var_411[2]
                            )
                            _var_var_415 = _var_var_411[3] + len(
                                _var_var_257["physics"]
                            )
                        else:
                            _var_var_412 = _var_var_407
                            _var_var_413 = _var_var_409
                            _var_var_414 = _var_var_408
                            _var_var_415 = len(_var_var_257["physics"])
                        _var_var_416 = advantage_factor(_var_var_412, _var_var_413)
                        _var_var_417 = (
                            _var_var_403,
                            _var_var_404,
                            _var_var_410,
                            round(_var_var_412, 6),
                            round(_var_var_413, 6) if _var_var_413 else None,
                            round(_var_var_414, 6),
                            round(_var_var_416, 4),
                            _var_var_415,
                            now,
                        )
                        if _var_var_410 is None:
                            await _var_var_366.execute(
                                "DELETE FROM method_performance_learning\n                                   WHERE cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_403, _var_var_404),
                                auto_commit=False,
                            )
                            await _var_var_366.execute(
                                "INSERT INTO method_performance_learning\n                                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                    ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_417,
                                auto_commit=False,
                            )
                        else:
                            await _var_var_366.execute(
                                "INSERT INTO method_performance_learning\n                                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                    ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                _var_var_417,
                                auto_commit=False,
                            )
                    _LOGGER.info(
                        "Method learning [%s/%s]: physics_mae=%.4f, ai_mae=%s, advantage=%.2f (%d samples)",
                        _var_var_403,
                        _var_var_404,
                        _var_var_412,
                        f"{_var_var_413:.4f}" if _var_var_413 else "N/A",
                        _var_var_416,
                        _var_var_415,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info("Method performance learning completed for %s", _var_var_66)
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Method performance learning error: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _learn_weather_regime_performance(self, now: datetime) -> Dict[str, str]:
        """Learn method accuracy and bias per static weather regime."""
        try:
            _var_var_366 = self.db
            _var_var_66 = now.date().isoformat()
            _var_var_126 = await _var_var_366.fetchall(
                "SELECT oh.target_hour,\n                          oh.prediction_kwh AS blend_kwh,\n                          hp.actual_kwh,\n                          oh.physics_kwh, oh.ai_kwh, oh.tfs_kwh,\n                          ow.clouds, ow.rain, ow.humidity,\n                          ow.solar_radiation_wm2, ow.direct_radiation,\n                          ow.diffuse_radiation,\n                          ac.clear_sky_radiation_wm2, ac.sun_elevation_deg\n                   FROM ops_daily_forecasts od\n                   JOIN ops_hourly_forecasts oh\n                       ON oh.snapshot_sequence = od.snapshot_sequence\n                       AND oh.target_date = od.forecast_date\n                   LEFT JOIN ops_prediction_weather ow\n                       ON ow.forecast_row_id = oh.forecast_row_id\n                   LEFT JOIN hourly_predictions hp\n                       ON hp.prediction_id = oh.prediction_id\n                   LEFT JOIN astronomy_cache ac\n                       ON ac.cache_date = oh.target_date AND ac.hour = oh.target_hour\n                   WHERE od.forecast_type = 'today'\n                     AND od.snapshot_type = 'morning_snapshot'\n                     AND od.forecast_date = ?\n                     AND od.snapshot_sequence = (\n                         SELECT MAX(od2.snapshot_sequence)\n                         FROM ops_daily_forecasts od2\n                         WHERE od2.forecast_type = 'today'\n                           AND od2.snapshot_type = 'morning_snapshot'\n                           AND od2.forecast_date = od.forecast_date\n                     )\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND oh.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (hp.exclude_from_clean_evaluation = FALSE OR hp.exclude_from_clean_evaluation IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN (\n                         'suspected_battery_curtailment',\n                         'demand_limited_zero_export',\n                         'full_battery_export_limit',\n                         'full_battery_zero_export',\n                         'transition_curtailment'\n                     )\n                     AND COALESCE(hp.data_quarantine_reason, '') NOT IN (\n                         'suspected_battery_curtailment',\n                         'demand_limited_zero_export',\n                         'full_battery_export_limit',\n                         'full_battery_zero_export',\n                         'transition_curtailment'\n                     )",
                (_var_var_66,),
            )
            _var_var_418 = "ops_morning_snapshot"
            if not _var_var_126:
                _var_var_126 = await _var_var_366.fetchall(
                    "SELECT hp.target_hour,\n                              CASE WHEN hp.adaptive_corrected = TRUE\n                                   THEN hp.prediction_kwh_uncapped\n                                   ELSE hp.prediction_kwh END AS blend_kwh,\n                              hp.actual_kwh,\n                              hp.physics_kwh, hp.ai_kwh, hp.tfs_kwh,\n                              pw.clouds, pw.rain, pw.humidity,\n                              pw.solar_radiation_wm2, pw.direct_radiation,\n                              pw.diffuse_radiation,\n                              ac.clear_sky_radiation_wm2, ac.sun_elevation_deg\n                       FROM hourly_predictions hp\n                       LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                           AND pw.weather_type = 'forecast'\n                       LEFT JOIN astronomy_cache ac\n                           ON ac.cache_date = hp.target_date AND ac.hour = hp.target_hour\n                       WHERE hp.target_date = ?\n                         AND hp.actual_kwh IS NOT NULL\n                         AND hp.actual_kwh > 0\n                         AND hp.physics_kwh IS NOT NULL\n                         AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                         AND (hp.exclude_from_clean_evaluation = FALSE OR hp.exclude_from_clean_evaluation IS NULL)\n                         AND COALESCE(hp.mppt_throttle_reason, '') NOT IN (\n                             'suspected_battery_curtailment',\n                             'demand_limited_zero_export',\n                             'full_battery_export_limit',\n                             'full_battery_zero_export',\n                             'transition_curtailment'\n                         )\n                         AND COALESCE(hp.data_quarantine_reason, '') NOT IN (\n                             'suspected_battery_curtailment',\n                             'demand_limited_zero_export',\n                             'full_battery_export_limit',\n                             'full_battery_zero_export',\n                             'transition_curtailment'\n                         )",
                    (_var_var_66,),
                )
                _var_var_418 = "hourly_predictions_fallback"
            if not _var_var_126:
                _var_var_23 = f"no valid hourly weather-regime data for {_var_var_66}"
                _LOGGER.info("Weather regime learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _LOGGER.info(
                "Weather regime learning: using %s source for %s (%d rows)",
                _var_var_418,
                _var_var_66,
                len(_var_var_126),
            )
            _var_var_419 = {}
            for _var_var_86, _var_var_62 in enumerate(_var_var_126):
                _var_var_116 = _var_var_62[0]
                _var_var_400 = _var_var_62[1]
                _var_var_249 = _var_var_62[2]
                _var_var_401 = _var_var_62[3]
                _var_var_402 = _var_var_62[4]
                _var_var_420 = _var_var_62[5]
                _var_var_378 = float(_var_var_62[6] or 50.0)
                _var_var_205 = float(_var_var_62[7] or 0.0)
                _var_var_201 = float(_var_var_62[8] or 70.0)
                _var_var_127 = float(_var_var_62[9] or 0.0)
                _var_var_421 = float(_var_var_62[10] or 0.0)
                _var_var_422 = float(_var_var_62[11] or 0.0)
                _var_var_423 = float(_var_var_62[12] or 0.0)
                _var_var_204 = float(_var_var_62[13] or 0.0)
                if _var_var_400 is None or _var_var_249 is None or _var_var_401 is None:
                    continue
                _var_var_424 = _classify_static_weather_regime_for_learning(
                    clouds=_var_var_378,
                    ghi=_var_var_127,
                    dni=_var_var_421,
                    dhi=_var_var_422,
                    clear_sky=_var_var_423,
                    rain=_var_var_205,
                    humidity=_var_var_201,
                    sun_elevation=_var_var_204,
                    hour=_var_var_116,
                )
                _var_var_404 = (
                    "morning"
                    if _var_var_116 < 10
                    else "midday" if _var_var_116 < 15 else "afternoon"
                )
                _var_var_73 = (_var_var_424, _var_var_404)
                if _var_var_73 not in _var_var_419:
                    _var_var_419[_var_var_73] = {
                        "physics_abs": [],
                        "ai_abs": [],
                        "tfs_abs": [],
                        "blend_abs": [],
                        "physics_bias": [],
                        "ai_bias": [],
                        "tfs_bias": [],
                        "blend_bias": [],
                    }
                _var_var_419[_var_var_73]["physics_abs"].append(
                    abs(_var_var_401 - _var_var_249)
                )
                _var_var_419[_var_var_73]["blend_abs"].append(
                    abs(_var_var_400 - _var_var_249)
                )
                _var_var_419[_var_var_73]["physics_bias"].append(
                    _var_var_401 - _var_var_249
                )
                _var_var_419[_var_var_73]["blend_bias"].append(
                    _var_var_400 - _var_var_249
                )
                if _var_var_402 is not None and _var_var_402 > 0:
                    _var_var_419[_var_var_73]["ai_abs"].append(
                        abs(_var_var_402 - _var_var_249)
                    )
                    _var_var_419[_var_var_73]["ai_bias"].append(
                        _var_var_402 - _var_var_249
                    )
                if _var_var_420 is not None and _var_var_420 > 0:
                    _var_var_419[_var_var_73]["tfs_abs"].append(
                        abs(_var_var_420 - _var_var_249)
                    )
                    _var_var_419[_var_var_73]["tfs_bias"].append(
                        _var_var_420 - _var_var_249
                    )
                if _var_var_86 % 50 == 0:
                    await asyncio.sleep(0)
            if not _var_var_419:
                _var_var_23 = f"no usable weather-regime buckets for {_var_var_66}"
                _LOGGER.info("Weather regime learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_406 = get_season(now.month)
            _var_var_425 = 0
            async with _var_var_366.transaction():
                for (_var_var_424, _var_var_404), _var_var_97 in _var_var_419.items():
                    _var_var_407 = _mean(_var_var_97["physics_abs"])
                    _var_var_409 = _mean(_var_var_97["ai_abs"])
                    _var_var_426 = _mean(_var_var_97["tfs_abs"])
                    _var_var_408 = _mean(_var_var_97["blend_abs"])
                    _var_var_427 = _mean(_var_var_97["physics_bias"])
                    _var_var_428 = _mean(_var_var_97["ai_bias"])
                    _var_var_429 = _mean(_var_var_97["tfs_bias"])
                    _var_var_430 = _mean(_var_var_97["blend_bias"])
                    _var_var_431 = len(_var_var_97["physics_abs"])
                    _var_var_432 = (
                        _var_var_426
                        if _var_var_426 is not None
                        and len(_var_var_97["tfs_abs"]) >= REGIME_TFS_MIN_SAMPLES
                        else None
                    )
                    _var_var_433 = regime_best_method(
                        _var_var_407, _var_var_408, _var_var_409, _var_var_432
                    )
                    for _var_var_410 in (None, _var_var_406):
                        _var_var_411 = await _var_var_366.fetchone(
                            "SELECT physics_mae, ai_mae, tfs_mae, blend_mae,\n                                      physics_bias, ai_bias, tfs_bias, blend_bias,\n                                      sample_count\n                               FROM weather_regime_performance_learning\n                               WHERE weather_regime = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (_var_var_424, _var_var_404, _var_var_410),
                        )
                        if _var_var_411 and _var_var_411[8] and (_var_var_411[8] > 0):

                            def smooth(new_value, old_value):
                                if new_value is None:
                                    return old_value
                                if old_value is None:
                                    return new_value
                                return (
                                    REGIME_LEARNING_SMOOTHING * new_value
                                    + (1 - REGIME_LEARNING_SMOOTHING) * old_value
                                )

                            _var_var_435 = smooth(_var_var_407, _var_var_411[0])
                            _var_var_436 = smooth(_var_var_409, _var_var_411[1])
                            _var_var_437 = smooth(_var_var_426, _var_var_411[2])
                            _var_var_438 = smooth(_var_var_408, _var_var_411[3])
                            _var_var_439 = smooth(_var_var_427, _var_var_411[4])
                            _var_var_440 = smooth(_var_var_428, _var_var_411[5])
                            _var_var_441 = smooth(_var_var_429, _var_var_411[6])
                            _var_var_442 = smooth(_var_var_430, _var_var_411[7])
                            _var_var_415 = _var_var_411[8] + _var_var_431
                        else:
                            _var_var_435 = _var_var_407
                            _var_var_436 = _var_var_409
                            _var_var_437 = _var_var_426
                            _var_var_438 = _var_var_408
                            _var_var_439 = _var_var_427
                            _var_var_440 = _var_var_428
                            _var_var_441 = _var_var_429
                            _var_var_442 = _var_var_430
                            _var_var_415 = _var_var_431
                        _var_var_443 = regime_best_method(
                            _var_var_435, _var_var_438, _var_var_436, _var_var_437
                        )
                        _var_var_444 = advantage_factor(_var_var_435, _var_var_436)
                        if (
                            _var_var_437
                            and _var_var_437 > 0
                            and _var_var_438
                            and (_var_var_438 > 0)
                        ):
                            _var_var_445 = max(
                                0.5, min(2.0, _var_var_438 / _var_var_437)
                            )
                        else:
                            _var_var_445 = 1.0
                        _var_var_446 = recommended_tfs_weight(
                            _var_var_443, _var_var_445
                        )
                        _var_var_417 = (
                            _var_var_424,
                            _var_var_404,
                            _var_var_410,
                            (
                                round(_var_var_435, 6)
                                if _var_var_435 is not None
                                else None
                            ),
                            (
                                round(_var_var_436, 6)
                                if _var_var_436 is not None
                                else None
                            ),
                            (
                                round(_var_var_437, 6)
                                if _var_var_437 is not None
                                else None
                            ),
                            (
                                round(_var_var_438, 6)
                                if _var_var_438 is not None
                                else None
                            ),
                            (
                                round(_var_var_439, 6)
                                if _var_var_439 is not None
                                else None
                            ),
                            (
                                round(_var_var_440, 6)
                                if _var_var_440 is not None
                                else None
                            ),
                            (
                                round(_var_var_441, 6)
                                if _var_var_441 is not None
                                else None
                            ),
                            (
                                round(_var_var_442, 6)
                                if _var_var_442 is not None
                                else None
                            ),
                            _var_var_443,
                            round(_var_var_446, 4) if _var_var_446 else None,
                            round(_var_var_444, 4),
                            round(_var_var_445, 4),
                            _var_var_415,
                            now,
                        )
                        if _var_var_410 is None:
                            await _var_var_366.execute(
                                "DELETE FROM weather_regime_performance_learning\n                                   WHERE weather_regime = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_424, _var_var_404),
                                auto_commit=False,
                            )
                            await _var_var_366.execute(
                                "INSERT INTO weather_regime_performance_learning\n                                   (weather_regime, hour_bucket, season,\n                                    physics_mae, ai_mae, tfs_mae, blend_mae,\n                                    physics_bias, ai_bias, tfs_bias, blend_bias,\n                                    best_method, recommended_tfs_weight,\n                                    ai_advantage_factor, tfs_advantage_factor,\n                                    sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_417,
                                auto_commit=False,
                            )
                        else:
                            await _var_var_366.execute(
                                "INSERT INTO weather_regime_performance_learning\n                                   (weather_regime, hour_bucket, season,\n                                    physics_mae, ai_mae, tfs_mae, blend_mae,\n                                    physics_bias, ai_bias, tfs_bias, blend_bias,\n                                    best_method, recommended_tfs_weight,\n                                    ai_advantage_factor, tfs_advantage_factor,\n                                    sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(weather_regime, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       tfs_mae = excluded.tfs_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       physics_bias = excluded.physics_bias,\n                                       ai_bias = excluded.ai_bias,\n                                       tfs_bias = excluded.tfs_bias,\n                                       blend_bias = excluded.blend_bias,\n                                       best_method = excluded.best_method,\n                                       recommended_tfs_weight = excluded.recommended_tfs_weight,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       tfs_advantage_factor = excluded.tfs_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                _var_var_417,
                                auto_commit=False,
                            )
                        _var_var_425 += 1
                    _LOGGER.info(
                        "Weather regime learning [%s/%s]: best=%s, physics_mae=%.4f, ai_mae=%s, tfs_mae=%s, blend_bias=%s (%d samples)",
                        _var_var_424,
                        _var_var_404,
                        _var_var_433,
                        _var_var_407,
                        f"{_var_var_409:.4f}" if _var_var_409 is not None else "N/A",
                        f"{_var_var_426:.4f}" if _var_var_426 is not None else "N/A",
                        f"{_var_var_430:.4f}" if _var_var_430 is not None else "N/A",
                        _var_var_431,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Weather regime performance learning completed for %s: %d buckets updated",
                _var_var_66,
                _var_var_425,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                "Weather regime performance learning error: %s",
                _var_var_16,
                exc_info=True,
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _train_panel_group_efficiency(self, now: datetime) -> Dict[str, str]:
        """Learn per-group ensemble weights (LSTM vs Ridge) per condition bucket. @zara V16.0.3

        For each panel group, computes LSTM and Ridge MAE against actual production,
        bucketed by cloud cover and hour of day. Derives inverse-MAE weights with
        EMA smoothing. Silent no-op when lstm_kwh/ridge_kwh data is not yet available.
        """
        try:
            _var_var_366 = self.db
            _var_var_66 = now.date().isoformat()
            _var_var_126 = await _var_var_366.fetchall(
                f"SELECT ppg.group_name, hp.target_hour,\n                          ppg.lstm_kwh, ppg.ridge_kwh, ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN panel_group_config_epochs pge_active\n                       ON pge_active.valid_to IS NULL\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                       ON pgeg_active.epoch_id = pge_active.epoch_id\n                      AND pgeg_active.display_name = ppg.group_name\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.lstm_kwh IS NOT NULL AND ppg.ridge_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                     AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n                     AND (\n                         (ppg.config_epoch_id IS NOT NULL\n                          AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                         OR\n                         (ppg.config_epoch_id IS NULL\n                          AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n                     )",
                (_var_var_66,),
            )
            if not _var_var_126:
                _var_var_23 = (
                    f"no eligible LSTM/Ridge panel-group data for {_var_var_66}"
                )
                _LOGGER.info("Ensemble group weight learning skipped: %s", _var_var_23)
                return {
                    "status": "skipped",
                    "reason": _var_var_23,
                    "reason_code": "panel_group_model_learning_data_unavailable",
                    "impact": "informational",
                }
            _var_var_399 = {}
            for _var_var_86, _var_var_62 in enumerate(_var_var_126):
                _var_var_27 = _var_var_62[0]
                _var_var_116 = _var_var_62[1]
                _var_var_447 = _var_var_62[2]
                _var_var_448 = _var_var_62[3]
                _var_var_249 = _var_var_62[4]
                _var_var_378 = _var_var_62[5] or 50
                _var_var_403 = (
                    "clear"
                    if _var_var_378 < 30
                    else "partly_cloudy" if _var_var_378 < 70 else "overcast"
                )
                _var_var_404 = (
                    "morning"
                    if _var_var_116 < 10
                    else "midday" if _var_var_116 < 15 else "afternoon"
                )
                _var_var_73 = (_var_var_27, _var_var_403, _var_var_404)
                if _var_var_73 not in _var_var_399:
                    _var_var_399[_var_var_73] = {"lstm": [], "ridge": []}
                _var_var_399[_var_var_73]["lstm"].append(
                    abs(_var_var_447 - _var_var_249)
                )
                _var_var_399[_var_var_73]["ridge"].append(
                    abs(_var_var_448 - _var_var_249)
                )
                if _var_var_86 % 50 == 0:
                    await asyncio.sleep(0)
            _var_var_405 = 0.3
            _var_var_449 = 0.2
            _var_var_450 = 0.95
            _var_var_406 = get_season(now.month)
            _var_var_425 = 0
            async with _var_var_366.transaction():
                for (
                    _var_var_27,
                    _var_var_403,
                    _var_var_404,
                ), _var_var_257 in _var_var_399.items():
                    _var_var_451 = sum(_var_var_257["lstm"]) / len(_var_var_257["lstm"])
                    _var_var_452 = sum(_var_var_257["ridge"]) / len(
                        _var_var_257["ridge"]
                    )
                    for _var_var_410 in (None, _var_var_406):
                        _var_var_411 = await _var_var_366.fetchone(
                            "SELECT lstm_mae, ridge_mae, sample_count\n                               FROM ensemble_group_weights\n                               WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (_var_var_27, _var_var_403, _var_var_404, _var_var_410),
                        )
                        if _var_var_411 and _var_var_411[2] and (_var_var_411[2] > 0):
                            _var_var_453 = (
                                _var_var_405 * _var_var_451
                                + (1 - _var_var_405) * _var_var_411[0]
                            )
                            _var_var_454 = (
                                _var_var_405 * _var_var_452
                                + (1 - _var_var_405) * _var_var_411[1]
                            )
                            _var_var_415 = _var_var_411[2] + len(_var_var_257["lstm"])
                        else:
                            _var_var_453 = _var_var_451
                            _var_var_454 = _var_var_452
                            _var_var_415 = len(_var_var_257["lstm"])
                        _var_var_455 = max(0.001, _var_var_453)
                        _var_var_456 = max(0.001, _var_var_454)
                        _var_var_457 = 1.0 / _var_var_455 + 1.0 / _var_var_456
                        _var_var_458 = 1.0 / _var_var_455 / _var_var_457
                        _var_var_459 = max(
                            _var_var_449, min(_var_var_450, _var_var_458)
                        )
                        _var_var_460 = 1.0 - _var_var_459
                        _var_var_417 = (
                            _var_var_27,
                            _var_var_403,
                            _var_var_404,
                            _var_var_410,
                            round(_var_var_459, 4),
                            round(_var_var_460, 4),
                            round(_var_var_453, 6),
                            round(_var_var_454, 6),
                            _var_var_415,
                            now,
                        )
                        if _var_var_410 is None:
                            await _var_var_366.execute(
                                "DELETE FROM ensemble_group_weights\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_27, _var_var_403, _var_var_404),
                                auto_commit=False,
                            )
                            await _var_var_366.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_417,
                                auto_commit=False,
                            )
                        else:
                            await _var_var_366.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       lstm_weight = excluded.lstm_weight,\n                                       ridge_weight = excluded.ridge_weight,\n                                       lstm_mae = excluded.lstm_mae,\n                                       ridge_mae = excluded.ridge_mae,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                _var_var_417,
                                auto_commit=False,
                            )
                        _var_var_425 += 1
                    _LOGGER.info(
                        "Ensemble weight [%s/%s/%s]: lstm=%.2f ridge=%.2f (lstm_mae=%.4f, ridge_mae=%.4f, %d samples)",
                        _var_var_27,
                        _var_var_403,
                        _var_var_404,
                        _var_var_459,
                        _var_var_460,
                        _var_var_453,
                        _var_var_454,
                        _var_var_415,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Ensemble group weight learning completed for %s: %d buckets updated",
                _var_var_66,
                _var_var_425,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Ensemble group weight learning error: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _learn_group_method_performance(self, now: datetime) -> Dict[str, str]:
        """Learn per-group method accuracy (Physics vs AI vs LSTM vs Ridge). @zara V16.4.0

        For each panel group, computes MAE of each prediction method against actual
        production, bucketed by (cloud_bucket, hour_bucket, season). Determines which
        method is most accurate per group per condition. This data feeds the graduated
        safeguard to make data-driven decisions about AI vs Physics blending.

        Silent no-op when no per-group actual data is available.
        """
        try:
            _var_var_366 = self.db
            _var_var_66 = now.date().isoformat()
            _var_var_126 = await _var_var_366.fetchall(
                f"SELECT ppg.group_name, hp.target_hour,\n                          ppg.physics_kwh, ppg.ai_kwh,\n                          ppg.lstm_kwh, ppg.ridge_kwh,\n                          ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   LEFT JOIN panel_group_config_epochs pge_active\n                       ON pge_active.valid_to IS NULL\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                       ON pgeg_active.epoch_id = pge_active.epoch_id\n                      AND pgeg_active.display_name = ppg.group_name\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)\n                     AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n                     AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n                     AND (\n                         (ppg.config_epoch_id IS NOT NULL\n                          AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                         OR\n                         (ppg.config_epoch_id IS NULL\n                          AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n                     )",
                (_var_var_66,),
            )
            if not _var_var_126:
                _var_var_23 = f"no eligible panel-group actual data for {_var_var_66}"
                _LOGGER.info("Group method performance skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_399 = {}
            for _var_var_86, _var_var_62 in enumerate(_var_var_126):
                _var_var_27 = _var_var_62[0]
                _var_var_116 = _var_var_62[1]
                _var_var_461 = _var_var_62[2]
                _var_var_462 = _var_var_62[3]
                _var_var_447 = _var_var_62[4]
                _var_var_448 = _var_var_62[5]
                _var_var_249 = _var_var_62[6]
                _var_var_378 = _var_var_62[7] or 50
                _var_var_403 = (
                    "clear"
                    if _var_var_378 < 30
                    else "partly_cloudy" if _var_var_378 < 70 else "overcast"
                )
                _var_var_404 = (
                    "morning"
                    if _var_var_116 < 10
                    else "midday" if _var_var_116 < 15 else "afternoon"
                )
                _var_var_73 = (_var_var_27, _var_var_403, _var_var_404)
                if _var_var_73 not in _var_var_399:
                    _var_var_399[_var_var_73] = {
                        "physics": [],
                        "ai": [],
                        "lstm": [],
                        "ridge": [],
                        "blend": [],
                    }
                _var_var_399[_var_var_73]["physics"].append(
                    abs(_var_var_461 - _var_var_249)
                )
                if _var_var_462 is not None and _var_var_462 > 0:
                    _var_var_399[_var_var_73]["ai"].append(
                        abs(_var_var_462 - _var_var_249)
                    )
                if _var_var_447 is not None and _var_var_447 > 0:
                    _var_var_399[_var_var_73]["lstm"].append(
                        abs(_var_var_447 - _var_var_249)
                    )
                if _var_var_448 is not None and _var_var_448 > 0:
                    _var_var_399[_var_var_73]["ridge"].append(
                        abs(_var_var_448 - _var_var_249)
                    )
                if _var_var_86 % 50 == 0:
                    await asyncio.sleep(0)
            if not _var_var_399:
                _var_var_23 = (
                    f"no usable panel-group performance buckets for {_var_var_66}"
                )
                _LOGGER.info("Group method performance skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_405 = 0.3
            _var_var_406 = get_season(now.month)
            _var_var_425 = 0
            async with _var_var_366.transaction():
                for (
                    _var_var_27,
                    _var_var_403,
                    _var_var_404,
                ), _var_var_257 in _var_var_399.items():
                    _var_var_407 = sum(_var_var_257["physics"]) / len(
                        _var_var_257["physics"]
                    )
                    _var_var_409 = (
                        sum(_var_var_257["ai"]) / len(_var_var_257["ai"])
                        if _var_var_257["ai"]
                        else None
                    )
                    _var_var_451 = (
                        sum(_var_var_257["lstm"]) / len(_var_var_257["lstm"])
                        if _var_var_257["lstm"]
                        else None
                    )
                    _var_var_452 = (
                        sum(_var_var_257["ridge"]) / len(_var_var_257["ridge"])
                        if _var_var_257["ridge"]
                        else None
                    )
                    _var_var_433 = group_best_method(
                        _var_var_407, _var_var_409, _var_var_451, _var_var_452
                    )
                    _var_var_416 = advantage_factor(_var_var_407, _var_var_409)
                    for _var_var_410 in (None, _var_var_406):
                        _var_var_411 = await _var_var_366.fetchone(
                            "SELECT physics_mae, ai_mae, lstm_mae, ridge_mae,\n                                      sample_count\n                               FROM group_method_performance\n                               WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                   AND season IS ?",
                            (_var_var_27, _var_var_403, _var_var_404, _var_var_410),
                        )
                        if _var_var_411 and _var_var_411[4] and (_var_var_411[4] > 0):
                            _var_var_463 = (
                                _var_var_405 * _var_var_407
                                + (1 - _var_var_405) * _var_var_411[0]
                            )
                            _var_var_464 = (
                                _var_var_405 * _var_var_409
                                + (1 - _var_var_405) * _var_var_411[1]
                                if _var_var_409 and _var_var_411[1]
                                else _var_var_409
                            )
                            _var_var_465 = (
                                _var_var_405 * _var_var_451
                                + (1 - _var_var_405) * _var_var_411[2]
                                if _var_var_451 and _var_var_411[2]
                                else _var_var_451
                            )
                            _var_var_466 = (
                                _var_var_405 * _var_var_452
                                + (1 - _var_var_405) * _var_var_411[3]
                                if _var_var_452 and _var_var_411[3]
                                else _var_var_452
                            )
                            _var_var_415 = _var_var_411[4] + len(
                                _var_var_257["physics"]
                            )
                        else:
                            _var_var_463 = _var_var_407
                            _var_var_464 = _var_var_409
                            _var_var_465 = _var_var_451
                            _var_var_466 = _var_var_452
                            _var_var_415 = len(_var_var_257["physics"])
                        _var_var_443 = group_best_method(
                            _var_var_463, _var_var_464, _var_var_465, _var_var_466
                        )
                        _var_var_467 = advantage_factor(_var_var_463, _var_var_464)
                        _var_var_417 = (
                            _var_var_27,
                            _var_var_403,
                            _var_var_404,
                            _var_var_410,
                            round(_var_var_463, 6),
                            round(_var_var_464, 6) if _var_var_464 else None,
                            round(_var_var_465, 6) if _var_var_465 else None,
                            round(_var_var_466, 6) if _var_var_466 else None,
                            None,
                            _var_var_443,
                            round(_var_var_467, 4),
                            _var_var_415,
                            now,
                        )
                        if _var_var_410 is None:
                            await _var_var_366.execute(
                                "DELETE FROM group_method_performance\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_27, _var_var_403, _var_var_404),
                                auto_commit=False,
                            )
                            await _var_var_366.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_417,
                                auto_commit=False,
                            )
                        else:
                            await _var_var_366.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                                   ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                       physics_mae = excluded.physics_mae,\n                                       ai_mae = excluded.ai_mae,\n                                       lstm_mae = excluded.lstm_mae,\n                                       ridge_mae = excluded.ridge_mae,\n                                       blend_mae = excluded.blend_mae,\n                                       best_method = excluded.best_method,\n                                       ai_advantage_factor = excluded.ai_advantage_factor,\n                                       sample_count = excluded.sample_count,\n                                       last_updated = excluded.last_updated",
                                _var_var_417,
                                auto_commit=False,
                            )
                        _var_var_425 += 1
                    _LOGGER.info(
                        "Group method learning [%s/%s/%s]: physics=%.4f ai=%s lstm=%s ridge=%s best=%s advantage=%.2f (%d samples)",
                        _var_var_27,
                        _var_var_403,
                        _var_var_404,
                        _var_var_407,
                        f"{_var_var_409:.4f}" if _var_var_409 else "N/A",
                        f"{_var_var_451:.4f}" if _var_var_451 else "N/A",
                        f"{_var_var_452:.4f}" if _var_var_452 else "N/A",
                        _var_var_433,
                        _var_var_416,
                        _var_var_415,
                    )
                    await asyncio.sleep(0)
            _LOGGER.info(
                "Group method performance learning completed for %s: %d buckets updated",
                _var_var_66,
                _var_var_425,
            )
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Group method performance learning error: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _run_weather_expert_learning(self, now: datetime) -> Dict[str, str]:
        """Learn weather expert weights from today's actual observations. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                _var_var_23 = "no weather pipeline manager available"
                _LOGGER.info("Weather expert learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_95 = self.coordinator.weather_pipeline_manager
            if not _var_var_95:
                _var_var_23 = "weather pipeline is unavailable"
                _LOGGER.info("Weather expert learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            if not _var_var_95.weather_expert_learner:
                _var_var_23 = "weather expert learner is not initialized"
                _LOGGER.info("Weather expert learning skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_66 = now.strftime("%Y-%m-%d")
            _var_var_473 = _var_var_95.weather_expert_learner
            _var_var_474 = None
            if not getattr(_var_var_473, "learning_enabled", True):
                _var_var_474 = "weather_expert_immutable_learning_disabled"
                _var_var_475 = getattr(
                    _var_var_473,
                    "learning_disabled_reason",
                    "immutable weather expert learning is unavailable",
                )
                _LOGGER.info("Weather expert learning skipped: %s", _var_var_475)
            else:
                _var_var_53 = await _var_var_473.learn_from_day(_var_var_66)
                if _var_var_53:
                    _LOGGER.info(f"Weather expert learning SUCCESS for {_var_var_66}")
                    _var_var_475 = ""
                else:
                    _var_var_475 = (
                        f"insufficient weather expert learning data for {_var_var_66}"
                    )
                    _LOGGER.info("Weather expert learning skipped: %s", _var_var_475)
            try:
                _var_var_476 = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_66,),
                )
                _var_var_477 = [
                    {
                        "prediction_id": _var_var_62[0],
                        "target_hour": _var_var_62[1],
                        "actual_kwh": _var_var_62[2],
                    }
                    for _var_var_62 in _var_var_476
                ]
                _var_var_478 = {}
                if _var_var_95.weather_actual_tracker:
                    _var_var_478 = await _var_var_95.weather_actual_tracker.get_daily_actual_weather(
                        _var_var_66
                    )
                if _var_var_477:
                    _var_var_479 = await _var_var_95.weather_expert_learner.learn_snow_prediction_accuracy(
                        _var_var_66, _var_var_477, _var_var_478
                    )
                    if _var_var_479.get("total_snow_predicted", 0) > 0:
                        _LOGGER.info(
                            f"Snow prediction learning for {_var_var_66}: {_var_var_479.get('snow_confirmed', 0)}/{_var_var_479.get('total_snow_predicted', 0)} confirmed ({_var_var_479.get('accuracy', 0):.0%} accuracy)"
                        )
            except Exception as _var_var_480:
                _LOGGER.error(
                    "Snow prediction learning failed: %s", _var_var_480, exc_info=True
                )
                return {"status": "failed", "reason": str(_var_var_480)}
            if _var_var_475:
                if _var_var_474:
                    return {
                        "status": "skipped",
                        "reason": _var_var_475,
                        "reason_code": _var_var_474,
                        "impact": "informational",
                    }
                return {"status": "skipped", "reason": _var_var_475}
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(
                f"Weather expert learning error: {_var_var_16}", exc_info=True
            )
            return {"status": "failed", "reason": str(_var_var_16)}

    async def _run_drift_detection(self, now: datetime) -> Dict[str, str]:
        """Run drift detection and metric calculation. @zara V17.0.0

        Calculates rolling metrics (MAE, RMSE, Bias, Coverage),
        bucket-specific drift, CUSUM detection, and automatic responses.
        Updates coordinator cache for drift sensor.
        """
        try:
            _var_var_497 = getattr(self.coordinator, "drift_monitor", None)
            if not _var_var_497:
                _var_var_23 = "no drift monitor available"
                _LOGGER.info("Drift detection skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _var_var_66 = now.strftime("%Y-%m-%d")
            _var_var_53 = await _var_var_497.calculate_rolling_metrics(_var_var_66)
            _var_var_498 = int(_var_var_53.get("metrics_failed", 0))
            _var_var_499 = int(_var_var_53.get("operations_failed", 0))
            if _var_var_498:
                _var_var_23 = f"{_var_var_498} drift rolling metric calculations failed for {_var_var_66}"
                _LOGGER.error("Drift detection failed: %s", _var_var_23)
                return {"status": "failed", "reason": _var_var_23}
            if _var_var_499:
                _var_var_23 = (
                    f"{_var_var_499} drift adaptive operations failed for {_var_var_66}"
                )
                _LOGGER.error("Drift detection failed: %s", _var_var_23)
                return {"status": "failed", "reason": _var_var_23}
            if _var_var_53.get("metrics_saved", 0) == 0:
                _var_var_23 = f"no eligible drift metrics available for {_var_var_66}"
                _LOGGER.info("Drift detection skipped: %s", _var_var_23)
                return {"status": "skipped", "reason": _var_var_23}
            _LOGGER.info(
                "Drift detection completed for %s: %d metrics, %d events, %d scopes",
                _var_var_66,
                _var_var_53.get("metrics_saved", 0),
                _var_var_53.get("events_created", 0),
                _var_var_53.get("scopes_checked", 0),
            )
            _var_var_500 = bool(_var_var_53.get("global_bias_fresh", False))
            _var_var_501 = bool(_var_var_53.get("global_clamp_fresh", False))
            try:
                _var_var_502 = self.coordinator.drift_bias_correction
                _var_var_503 = self.coordinator._drift_solar_clamp_min
                if _var_var_500:
                    _var_var_504 = _var_var_497.get_global_bias_correction()
                    self.coordinator.drift_bias_correction = _var_var_504
                    if abs(_var_var_504 - _var_var_502) > 0.005:
                        _LOGGER.info(
                            "Drift bias correction updated on coordinator: %.4f → %.4f",
                            _var_var_502,
                            _var_var_504,
                        )
                if _var_var_501:
                    _var_var_505 = _var_var_497.get_solar_clamp_min()
                    self.coordinator._drift_solar_clamp_min = _var_var_505
                    _var_var_56 = (
                        self._pipeline_corrector or self._get_weather_corrector()
                    )
                    if _var_var_56 is not None:
                        _var_var_56.set_solar_clamp_min(_var_var_505)
                        if abs(_var_var_505 - _var_var_503) > 0.001:
                            _LOGGER.info(
                                "Solar clamp updated on corrector: %.3f → %.3f",
                                _var_var_503,
                                _var_var_505,
                            )
            except Exception as _var_var_16:
                _LOGGER.error(
                    "Failed to propagate drift correction factors: %s", _var_var_16
                )
                return {"status": "failed", "reason": str(_var_var_16)}
            try:
                _var_var_271 = await _var_var_497.get_status()
                self.coordinator._drift_status_cache = _var_var_271
            except Exception as _var_var_16:
                _LOGGER.error("Failed to cache drift status: %s", _var_var_16)
                return {"status": "failed", "reason": str(_var_var_16)}
            _var_var_357 = getattr(self.coordinator, "ai_predictor", None)
            if _var_var_357 is not None:
                _var_var_506 = await self.model_reaction_handler.process(
                    _var_var_66, _var_var_357
                )
                if _var_var_506.status == "failed":
                    _LOGGER.error(
                        "Confirmed model drift reaction failed: %s", _var_var_506.reason
                    )
                    return {"status": "failed", "reason": _var_var_506.reason}
                if _var_var_506.status in {"bound", "deferred", "replaced"}:
                    _LOGGER.info(
                        "Model drift reaction: status=%s request=%s candidate=%s reason=%s",
                        _var_var_506.status,
                        _var_var_506.request_id,
                        _var_var_506.candidate_id,
                        _var_var_506.reason,
                    )
            return {"status": "succeeded", "reason": ""}
        except Exception as _var_var_16:
            _LOGGER.error(f"Drift detection error: {_var_var_16}", exc_info=True)
            return {"status": "failed", "reason": str(_var_var_16)}

    async def generate_monthly_report(self, now: datetime) -> bool:
        """Generate the monthly report once per calendar month with catch-up."""
        _var_var_94 = now if now is not None else dt_util.now()
        _var_var_542 = _var_var_94.strftime("%Y-%m")
        _var_var_543 = f"{_var_var_542}:v{MONTHLY_REPORT_FORMAT_VERSION}"
        async with self._monthly_report_lock:
            try:
                _var_var_544 = await self.db.get_scheduled_task_completed_period(
                    MONTHLY_REPORT_TASK_KEY
                )
                if _var_var_544 == _var_var_543:
                    return False
                await self.db.mark_scheduled_task_attempt(
                    MONTHLY_REPORT_TASK_KEY, _var_var_94.isoformat()
                )
                _LOGGER.info(
                    "Generating monthly system report for period %s...", _var_var_542
                )
                _var_var_540 = self.coordinator.data_manager.data_dir
                _var_var_541 = SystemReportGenerator(_var_var_540, self.db)
                _var_var_93 = await _var_var_541.generate_report()
                if not _var_var_93:
                    _LOGGER.warning(
                        "Monthly system report generation failed for period %s",
                        _var_var_542,
                    )
                    return False
                _var_var_243 = dt_util.now().isoformat()
                await self.db.mark_scheduled_task_completed(
                    MONTHLY_REPORT_TASK_KEY, _var_var_543, _var_var_243
                )
                _LOGGER.info(
                    "Monthly system report generated successfully for period %s",
                    _var_var_542,
                )
                return True
            except Exception as _var_var_16:
                _LOGGER.error(
                    "Error generating monthly report: %s", _var_var_16, exc_info=True
                )
                return False
        if True:
            return
