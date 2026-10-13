# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Morning Routine Handler for Solar Forecast ML.

Implements bulletproof morning routine with retry logic
for creating daily predictions using database operations.

@author zara
"""
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

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
    from ..coordinator import SolarForecastMLCoordinator
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

_LOGGER = logging.getLogger(__name__)
CANONICAL_TODAY_LOCK_SOURCE = "morning_routine"
FALLBACK_TODAY_LOCK_SOURCE = "morning_fallback"
POST_MIDNIGHT_TODAY_LOCK_HOUR_MAX = 3


def today_lock_source_for_cycle(forecast_cycle: str) -> str:
    """00:25 is fallback; pre_sunrise/catchup/manual become the today anchor."""
    if forecast_cycle == "post_midnight":
        return FALLBACK_TODAY_LOCK_SOURCE
    return CANONICAL_TODAY_LOCK_SOURCE


def _locked_at_hour(locked_at: Any) -> Optional[int]:
    if locked_at is None:
        return
    if hasattr(locked_at, "hour"):
        try:
            return int(locked_at.hour)
        except (TypeError, ValueError):
            return
    timestamp_text = str(locked_at).replace("T", " ").strip()
    if not timestamp_text:
        return
    try:
        time_part = (
            timestamp_text.split(" ", 1)[1] if " " in timestamp_text else timestamp_text
        )
        return int(time_part.split(":", 1)[0])
    except (IndexError, ValueError, TypeError):
        return


def preserve_existing_today_morning_lock(
    *,
    has_today_hours: bool,
    locked: bool,
    lock_source: str,
    forecast_cycle: str,
    locked_at: Any = None,
) -> bool:
    """Keep today's hours only when the current cycle must not replace them.

    pre_sunrise may replace morning_fallback and a legacy 00:25 lock that was
    stored as morning_routine. After a real sunrise lock, no later cycle may
    raise today.
    """
    if not has_today_hours or not locked:
        return False
    source = str(lock_source or "")
    if source == FALLBACK_TODAY_LOCK_SOURCE:
        return forecast_cycle != "pre_sunrise"
    if source == CANONICAL_TODAY_LOCK_SOURCE:
        if forecast_cycle == "pre_sunrise":
            locked_hour = _locked_at_hour(locked_at)
            if (
                locked_hour is not None
                and locked_hour <= POST_MIDNIGHT_TODAY_LOCK_HOUR_MAX
            ):
                return False
        return True
    return False


def filter_candidate_shadow_for_preserved_today(
    candidate_shadow: Optional[Dict[str, Any]], *, preserve_today: bool, today: str
) -> Optional[Dict[str, Any]]:
    """Drop today hours when persist skips rewriting the morning today lock."""
    if candidate_shadow is None or not preserve_today:
        return candidate_shadow
    remaining_hours = [
        hour_entry
        for hour_entry in candidate_shadow.get("hours") or []
        if str(hour_entry.get("target_date") or "") != today
        if hour_entry.get("horizon") != "day_0"
    ]
    if not remaining_hours:
        return
    filtered_shadow = dict(candidate_shadow)
    filtered_shadow["hours"] = remaining_hours
    return filtered_shadow


class _ShadowLearningExclusionError(RuntimeError):
    """Raised when local-shadow evidence cannot be quarantined from learning."""


class MorningRoutineHandler:
    """Bulletproof morning routine with retry logic. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: "DatabaseManager",
        coordinator: "SolarForecastMLCoordinator",
    ):
        """Initialize the morning routine handler. @zara"""
        self.hass = hass
        self.db = db_manager
        self.coordinator = coordinator
        self._transaction_started = False
        self._shadow_detector = ShadowDetector(hass, db_manager)
        self._ensemble_shadow_handler = EnsembleShadowHandler(db_manager)
        self._model_candidate_shadow_handler = ModelCandidateShadowHandler(db_manager)

    def set_db_manager(self, db_manager: "DatabaseManager") -> None:
        """Bind database-backed helpers after DataManager initialization."""
        self.db = db_manager
        self._shadow_detector = ShadowDetector(self.hass, db_manager)
        self._ensemble_shadow_handler = EnsembleShadowHandler(db_manager)
        self._model_candidate_shadow_handler = ModelCandidateShadowHandler(db_manager)

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
        morning_batch_id = None
        try:
            morning_batch_id, batch_resumed = (
                await self._ensemble_shadow_handler.acquire_morning_batch(
                    date, forecast_cycle, trigger_kind
                )
            )
            _LOGGER.info(
                "Ensemble shadow morning batch %s: %s",
                "resumed" if batch_resumed else "created",
                morning_batch_id,
            )
        except Exception:
            _LOGGER.exception(
                "Ensemble shadow batch setup failed; continuing the production forecast"
            )
        try:
            for attempt in range(1, max_retries + 1):
                _LOGGER.info("")
                _LOGGER.info(
                    "================================================================================"
                )
                _LOGGER.info(
                    "  MORNING ROUTINE EXECUTION - Attempt %d/%d for %s",
                    attempt,
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
                    inverter_limit = getattr(
                        self.coordinator, "inverter_max_power", 0.0
                    )
                    if inverter_limit > 0:
                        _LOGGER.info(
                            "  ℹ Inverter capping enabled: %.2f kW", inverter_limit
                        )
                    _LOGGER.info(
                        "→ Step 2/5: Creating hourly predictions in database..."
                    )
                    created_dates = await self._create_daily_predictions(
                        date=date,
                        hourly_forecast=hourly_forecast,
                        weather_forecast=weather_hourly,
                        astronomy_data=astronomy_data,
                        sensor_config=sensor_config,
                        inverter_max_power=inverter_limit,
                        morning_batch_id=morning_batch_id or "",
                        candidate_shadow=candidate_shadow,
                        forecast_cycle=forecast_cycle,
                    )
                    _LOGGER.info("  ✓ Hourly predictions created successfully")
                    _LOGGER.info("→ Step 3/5: Verifying data integrity...")
                    if not await self._verify_integrity(date, created_dates):
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
                        "  Date: %s | Attempt: %d/%d", date, attempt, max_retries
                    )
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("")
                    await self._finish_ensemble_shadow_batch(morning_batch_id, True)
                    return True
                except Exception as error:
                    _LOGGER.error(
                        "Morning routine attempt %d/%d FAILED: %s",
                        attempt,
                        max_retries,
                        error,
                        exc_info=True,
                    )
                    if attempt < max_retries:
                        retry_delay = 60 * attempt
                        _LOGGER.info(
                            "Waiting %ds before retry %d...", retry_delay, attempt + 1
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        _LOGGER.error(
                            "CRITICAL: Morning routine FAILED after %d attempts! Manual intervention required",
                            max_retries,
                        )
                        await self._send_critical_alert(
                            "Morning routine failure", str(error)
                        )
                        await self._finish_ensemble_shadow_batch(
                            morning_batch_id, False
                        )
                        return False
            await self._finish_ensemble_shadow_batch(morning_batch_id, False)
            return False
        except Exception as error:
            _LOGGER.error(
                "CRITICAL: Morning routine handler crashed: %s", error, exc_info=True
            )
            return False

    async def _finish_ensemble_shadow_batch(
        self, morning_batch_id: Optional[str], succeeded: bool
    ) -> None:
        if not morning_batch_id:
            return
        try:
            await self._ensemble_shadow_handler.finish_morning_batch(
                morning_batch_id, succeeded
            )
        except Exception:
            _LOGGER.exception(
                "Failed to finalize ensemble shadow batch %s", morning_batch_id
            )
        return None if True else None

    async def _create_daily_predictions(
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
    ) -> set:
        """Atomically replace the complete three-day morning forecast batch."""
        async with self.db.transaction():
            persistence_result = await self._persist_daily_predictions(
                date=date,
                hourly_forecast=hourly_forecast,
                weather_forecast=weather_forecast,
                astronomy_data=astronomy_data,
                sensor_config=sensor_config,
                inverter_max_power=inverter_max_power,
                morning_batch_id=morning_batch_id,
                candidate_shadow=candidate_shadow,
                forecast_cycle=forecast_cycle,
            )
        await self._mirror_operational_morning_snapshot(
            date=date,
            hourly_forecast=hourly_forecast,
            weather_forecast=weather_forecast,
            forecast_types=persistence_result[1],
            daily_totals=persistence_result[2],
            saved_actuals=persistence_result[3],
            saved_group_actuals=persistence_result[4],
            inverter_max_power=inverter_max_power,
        )
        self.coordinator.expected_daily_production = round(persistence_result[5], 2)
        return persistence_result[0]

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
            unused_layout_slot = None
        _LOGGER.info("  ├─ Checking existing forecast locks...")
        existing_count_row = await self.db.fetchone(
            "SELECT COUNT(*) as count FROM hourly_predictions WHERE target_date = ?",
            (date,),
        )
        existing_count = existing_count_row[0] if existing_count_row else 0
        if existing_count > 0:
            _LOGGER.info(
                "  │  Found %d existing predictions - will update", existing_count
            )
        existing_lock = await self.db.fetchone(
            "SELECT locked, locked_at, source, prediction_kwh FROM daily_forecasts\n               WHERE forecast_type = 'today' AND forecast_date = ?",
            (date,),
        )
        existing_source = str(existing_lock[2] or "") if existing_lock else ""
        preserve_today = preserve_existing_today_morning_lock(
            has_today_hours=existing_count > 0,
            locked=bool(existing_lock and existing_lock[0]),
            lock_source=existing_source,
            forecast_cycle=forecast_cycle,
            locked_at=existing_lock[1] if existing_lock else None,
        )
        existing_today_total = None
        if preserve_today and existing_lock and (existing_lock[3] is not None):
            existing_today_total = float(existing_lock[3])
        today_lock_source = (
            existing_source
            if preserve_today and existing_source
            else today_lock_source_for_cycle(forecast_cycle)
        )
        if existing_lock and existing_lock[0]:
            if not preserve_today:
                _LOGGER.info(
                    "  │  Unlocking daily_forecast (was locked at %s by %s) for cycle=%s",
                    existing_lock[1],
                    existing_lock[2],
                    forecast_cycle or "unspecified",
                )
                await self.db.execute(
                    "UPDATE daily_forecasts SET locked = 0\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                    (date,),
                )
        _LOGGER.info("  ├─ Preparing to update predictions for 3 days...")
        from datetime import timedelta as timedelta_type

        base_date = datetime.fromisoformat(date).date()
        incoming_prediction_ids = set()
        for hour_data in hourly_forecast:
            hour = hour_data.get("hour")
            target_date = hour_data.get("date")
            if not hour is None and (not not target_date):
                if preserve_today:
                    if target_date == date:
                        continue
                incoming_prediction_ids.add(f"{target_date}_{hour:02d}")
        saved_actuals = {}
        saved_group_actuals = {}
        saved_sensor_actuals = {}
        saved_weather_actuals = {}
        if incoming_prediction_ids:
            placeholders = ",".join(["?"] * len(incoming_prediction_ids))
            prediction_params = tuple(incoming_prediction_ids)
            actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, actual_kwh, has_sensor_data\n                    FROM hourly_predictions\n                    WHERE prediction_id IN ({placeholders}) AND actual_kwh IS NOT NULL",
                prediction_params,
            )
            for row in actual_rows:
                saved_actuals[row[0]] = {
                    "actual_kwh": row[1],
                    "has_sensor_data": row[2],
                }
            group_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, group_name, actual_kwh\n                    FROM prediction_panel_groups\n                    WHERE prediction_id IN ({placeholders}) AND actual_kwh IS NOT NULL",
                prediction_params,
            )
            for row in group_actual_rows:
                saved_group_actuals[row[0], row[1]] = row[2]
            sensor_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, temperature_c, humidity_percent,\n                       solar_radiation_wm2, rain_mm, wind_speed_ms\n                    FROM prediction_sensor_actual\n                    WHERE prediction_id IN ({placeholders})",
                prediction_params,
            )
            for row in sensor_actual_rows:
                saved_sensor_actuals[row[0]] = {
                    "temperature_c": row[1],
                    "humidity_percent": row[2],
                    "solar_radiation_wm2": row[3],
                    "rain_mm": row[4],
                    "wind_speed_ms": row[5],
                }
            weather_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, temperature, solar_radiation_wm2, wind,\n                       humidity, rain, clouds, pressure, lux,\n                       frost_detected, frost_score, frost_confidence\n                    FROM prediction_weather\n                    WHERE prediction_id IN ({placeholders}) AND weather_type = 'actual'",
                prediction_params,
            )
            for row in weather_actual_rows:
                saved_weather_actuals[row[0]] = {
                    "temperature": row[1],
                    "solar_radiation_wm2": row[2],
                    "wind": row[3],
                    "humidity": row[4],
                    "rain": row[5],
                    "clouds": row[6],
                    "pressure": row[7],
                    "lux": row[8],
                    "frost_detected": row[9],
                    "frost_score": row[10],
                    "frost_confidence": row[11],
                }
            if saved_actuals:
                _LOGGER.info(
                    "  │  Preserved %d hourly actuals, %d group actuals before re-creation",
                    len(saved_actuals),
                    len(saved_group_actuals),
                )
            await self.db.execute(
                f"DELETE FROM hourly_predictions WHERE prediction_id IN ({placeholders})",
                prediction_params,
            )
            _LOGGER.info(
                "  │  Cleared %d existing prediction slots for update",
                len(incoming_prediction_ids),
            )
        else:
            _LOGGER.warning("  │  No incoming predictions to update - skipping DELETE")
        now = dt_util.now()
        weather_by_hour = {}
        for weather_hour in weather_forecast:
            weather_key = (weather_hour.get("date", date), weather_hour.get("hour"))
            weather_by_hour[weather_key] = weather_hour
        _LOGGER.info(
            "  ├─ Processing %d input forecast entries (3 days)...",
            len(hourly_forecast),
        )
        created_prediction_ids = set()
        duplicates_skipped = 0
        predictions_per_day = {}
        if preserve_today:
            predictions_per_day[date] = existing_count
            _LOGGER.info(
                "  │  Preserving %d today hourly rows under morning anchor %.2f kWh",
                existing_count,
                existing_today_total or 0.0,
            )
        for hour_data in hourly_forecast:
            hour = hour_data.get("hour")
            if hour is None:
                continue
            target_date = hour_data.get("date")
            if not target_date:
                target_date = date
                _LOGGER.warning(
                    "  │  ⚠️  Missing date in hour_data for hour %d, using %s",
                    hour,
                    date,
                )
            if preserve_today and target_date == date:
                continue
            prediction_id = f"{target_date}_{hour:02d}"
            if prediction_id in created_prediction_ids:
                duplicates_skipped += 1
                continue
            created_prediction_ids.add(prediction_id)
            predictions_per_day[target_date] = (
                predictions_per_day.get(target_date, 0) + 1
            )
            predicted_kwh = hour_data.get("production_kwh", 0.0)
            uncapped_kwh = hour_data.get("prediction_kwh_uncapped", predicted_kwh)
            if inverter_max_power > 0:
                power_cap = inverter_max_power
                predicted_kwh = min(predicted_kwh, power_cap)
            target_datetime = f"{target_date}T{hour:02d}:00:00"
            target_day = datetime.fromisoformat(target_date)
            await self.db.execute(
                "INSERT OR REPLACE INTO hourly_predictions\n                   (prediction_id, prediction_created_at, prediction_created_hour,\n                    morning_batch_id,\n                    target_datetime, target_date, target_hour,\n                    target_day_of_week, target_day_of_year, target_month, target_season,\n                    prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                    ml_contribution_percent, confidence, is_production_hour,\n                    physics_kwh, ai_kwh, ai_confidence,\n                    lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight,\n                    ghi_before_cap, ghi_after_cap, ghi_cap_applied)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prediction_id,
                    now,
                    now.hour,
                    morning_batch_id,
                    target_datetime,
                    target_date,
                    hour,
                    target_day.weekday(),
                    target_day.timetuple().tm_yday,
                    target_day.month,
                    get_season(target_day.month),
                    round(predicted_kwh, 4),
                    round(uncapped_kwh, 4),
                    hour_data.get("method", "physics"),
                    hour_data.get("ml_contribution", 0),
                    hour_data.get("confidence", 75.0),
                    6 <= hour <= 20,
                    hour_data.get("physics_kwh"),
                    hour_data.get("ai_kwh"),
                    hour_data.get("ai_confidence"),
                    hour_data.get("lstm_kwh"),
                    hour_data.get("ridge_kwh"),
                    hour_data.get("tfs_kwh"),
                    hour_data.get("tfs_weight"),
                    hour_data.get("ghi_before_cap"),
                    hour_data.get("ghi_after_cap"),
                    hour_data.get("ghi_cap_applied"),
                ),
            )
            group_predictions = hour_data.get("groups")
            if group_predictions:
                group_predictions = (
                    normalize_group_predictions_to_total(
                        group_predictions, predicted_kwh
                    )
                    or []
                )
                hour_data["groups"] = group_predictions
                hour_data["panel_group_predictions"] = sync_panel_group_prediction_map(
                    group_predictions
                )
                for group_prediction in group_predictions:
                    group_name = group_prediction.get("name")
                    group_kwh = group_prediction.get("power_kwh", 0.0)
                    physics_kwh = group_prediction.get("physics_kwh")
                    ai_kwh = group_prediction.get("ai_kwh")
                    lstm_kwh = group_prediction.get("lstm_kwh")
                    ridge_kwh = group_prediction.get("ridge_kwh")
                    tfs_kwh = group_prediction.get("tfs_kwh")
                    tfs_weight = group_prediction.get("tfs_weight")
                    raw_physics_kwh = group_prediction.get("raw_physics_kwh")
                    topology_meta = await self.db.get_active_panel_group_topology_meta(
                        group_name
                    )
                    await self.db.execute(
                        "INSERT INTO prediction_panel_groups\n                           (prediction_id, group_name, prediction_kwh, physics_kwh, ai_kwh,\n                            lstm_kwh, ridge_kwh, tfs_kwh, tfs_weight, raw_physics_kwh,\n                            config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction)\n                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                           ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                               prediction_kwh = excluded.prediction_kwh,\n                               physics_kwh = excluded.physics_kwh,\n                               ai_kwh = excluded.ai_kwh,\n                               lstm_kwh = excluded.lstm_kwh,\n                               ridge_kwh = excluded.ridge_kwh,\n                               tfs_kwh = excluded.tfs_kwh,\n                               tfs_weight = excluded.tfs_weight,\n                               raw_physics_kwh = excluded.raw_physics_kwh,\n                               config_epoch_id = excluded.config_epoch_id,\n                               group_uid = excluded.group_uid,\n                               group_lineage_uid = excluded.group_lineage_uid,\n                               power_wp_at_prediction = excluded.power_wp_at_prediction",
                        (
                            prediction_id,
                            group_name,
                            round(group_kwh, 4),
                            round(physics_kwh, 4) if physics_kwh else None,
                            round(ai_kwh, 4) if ai_kwh else None,
                            round(lstm_kwh, 4) if lstm_kwh else None,
                            round(ridge_kwh, 4) if ridge_kwh else None,
                            round(tfs_kwh, 4) if tfs_kwh else None,
                            round(tfs_weight, 3) if tfs_weight is not None else None,
                            (
                                round(raw_physics_kwh, 4)
                                if raw_physics_kwh is not None
                                else None
                            ),
                            topology_meta.get("config_epoch_id"),
                            topology_meta.get("group_uid"),
                            topology_meta.get("group_lineage_uid"),
                            topology_meta.get("power_wp_at_prediction"),
                        ),
                    )
                    continue
                await self.db.execute(
                    "UPDATE hourly_predictions SET has_panel_group_predictions = TRUE\n                       WHERE prediction_id = ?",
                    (prediction_id,),
                )
            weather_data = weather_by_hour.get((target_date, hour), {})
            if weather_data:
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, source, diffuse_radiation,\n                        direct_radiation)\n                       VALUES (?, 'forecast', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        prediction_id,
                        weather_data.get("temperature"),
                        weather_data.get("solar_radiation_wm2"),
                        weather_data.get("wind"),
                        weather_data.get("humidity"),
                        weather_data.get("rain"),
                        weather_data.get("clouds"),
                        weather_data.get("pressure"),
                        weather_data.get("source", "blended"),
                        weather_data.get("diffuse_radiation")
                        or weather_data.get("dhi"),
                        weather_data.get("direct_radiation"),
                    ),
                )
                correction_snapshot_id = weather_data.get("correction_snapshot_id")
                if correction_snapshot_id:
                    await self.db.insert_weather_forecast_usage_snapshot(
                        prediction_id=prediction_id,
                        morning_batch_id=morning_batch_id,
                        correction_snapshot_id=correction_snapshot_id,
                        locked_at=now,
                        target_datetime=target_datetime,
                        target_date=target_date,
                        target_hour=hour,
                    )
                else:
                    _LOGGER.warning(
                        "  │  Weather usage provenance unavailable for %s; EOD/MLP learning will skip this hour",
                        prediction_id,
                    )
            hour_astronomy = astronomy_data.get(f"{target_date}_{hour:02d}", {})
            if not hour_astronomy:
                hour_astronomy = astronomy_data.get(str(hour), {})
            day_astronomy = astronomy_data.get(target_date, astronomy_data)
            if not not hour_astronomy:
                sunrise = day_astronomy.get("sunrise_local")
                sunset = day_astronomy.get("sunset_local")
                solar_noon = day_astronomy.get("solar_noon_local")
                daylight_hours = day_astronomy.get("daylight_hours", 12.0)
                hours_since_solar_noon = None
                day_progress = None
                hours_after_sunrise = None
                hours_before_sunset = None
                try:
                    from datetime import datetime as datetime_type

                    hour_midpoint = datetime_type(
                        target_day.year, target_day.month, target_day.day, hour, 30
                    )

                    def _parse_ts(ts):
                        if ts is None:
                            return
                        if isinstance(ts, datetime_type):
                            return ts.replace(
                                year=hour_midpoint.year,
                                month=hour_midpoint.month,
                                day=hour_midpoint.day,
                                tzinfo=None,
                            )
                        timestamp_text = str(ts).strip()
                        if "T" in timestamp_text or len(timestamp_text) > 10:
                            try:
                                parsed_datetime = datetime_type.fromisoformat(
                                    timestamp_text.replace("Z", "+00:00")
                                )
                                return parsed_datetime.replace(
                                    year=hour_midpoint.year,
                                    month=hour_midpoint.month,
                                    day=hour_midpoint.day,
                                    tzinfo=None,
                                )
                            except (ValueError, TypeError):
                                pass
                        for time_format in ("%H:%M:%S", "%H:%M"):
                            try:
                                parsed_time = datetime_type.strptime(
                                    timestamp_text, time_format
                                )
                                return hour_midpoint.replace(
                                    hour=parsed_time.hour,
                                    minute=parsed_time.minute,
                                    second=parsed_time.second,
                                )
                            except (ValueError, TypeError):
                                continue

                    sunrise_datetime = _parse_ts(sunrise)
                    sunset_datetime = _parse_ts(sunset)
                    solar_noon_datetime = _parse_ts(solar_noon)
                    if sunrise_datetime and sunset_datetime:
                        daylight_seconds = (
                            sunset_datetime - sunrise_datetime
                        ).total_seconds()
                        seconds_after_sunrise = (
                            hour_midpoint - sunrise_datetime
                        ).total_seconds()
                        day_progress = (
                            max(0.0, min(1.0, seconds_after_sunrise / daylight_seconds))
                            if daylight_seconds > 0
                            else 0.5
                        )
                        hours_after_sunrise = max(0.0, seconds_after_sunrise / 3600.0)
                        hours_before_sunset = max(
                            0.0,
                            (sunset_datetime - hour_midpoint).total_seconds() / 3600.0,
                        )
                    if solar_noon_datetime:
                        hours_since_solar_noon = (
                            hour_midpoint - solar_noon_datetime
                        ).total_seconds() / 3600.0
                except Exception:
                    pass
                await self.db.execute(
                    "INSERT INTO prediction_astronomy\n                       (prediction_id, sunrise, sunset, solar_noon, daylight_hours,\n                        sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2,\n                        theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio,\n                        hours_after_sunrise, hours_before_sunset)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           sunrise = excluded.sunrise,\n                           sunset = excluded.sunset,\n                           solar_noon = excluded.solar_noon,\n                           daylight_hours = excluded.daylight_hours,\n                           sun_elevation_deg = excluded.sun_elevation_deg,\n                           sun_azimuth_deg = excluded.sun_azimuth_deg,\n                           clear_sky_radiation_wm2 = excluded.clear_sky_radiation_wm2,\n                           theoretical_max_kwh = excluded.theoretical_max_kwh,\n                           hours_since_solar_noon = excluded.hours_since_solar_noon,\n                           day_progress_ratio = excluded.day_progress_ratio,\n                           hours_after_sunrise = excluded.hours_after_sunrise,\n                           hours_before_sunset = excluded.hours_before_sunset",
                    (
                        prediction_id,
                        sunrise,
                        sunset,
                        solar_noon,
                        daylight_hours,
                        hour_astronomy.get("sun_elevation_deg"),
                        hour_astronomy.get("sun_azimuth_deg"),
                        hour_astronomy.get("clear_sky_radiation_wm2"),
                        hour_astronomy.get("theoretical_max_kwh"),
                        hours_since_solar_noon,
                        day_progress,
                        hours_after_sunrise,
                        hours_before_sunset,
                    ),
                )
        actuals_restored = 0
        if saved_actuals:
            for saved_prediction_id, saved_actual in saved_actuals.items():
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET actual_kwh = ?, has_sensor_data = ?\n                       WHERE prediction_id = ?",
                    (
                        saved_actual["actual_kwh"],
                        saved_actual["has_sensor_data"],
                        saved_prediction_id,
                    ),
                )
                actuals_restored += 1
        if saved_group_actuals:
            for (
                saved_prediction_id,
                group_name,
            ), saved_group_actual in saved_group_actuals.items():
                await self.db.execute(
                    "INSERT INTO prediction_panel_groups\n                       (prediction_id, group_name, prediction_kwh, actual_kwh, raw_physics_kwh)\n                       VALUES (?, ?, 0.0, ?, NULL)\n                       ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                           actual_kwh = excluded.actual_kwh",
                    (saved_prediction_id, group_name, saved_group_actual),
                )
        if saved_sensor_actuals:
            for saved_prediction_id, saved_readings in saved_sensor_actuals.items():
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms)\n                       VALUES (?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms",
                    (
                        saved_prediction_id,
                        saved_readings["temperature_c"],
                        saved_readings["humidity_percent"],
                        saved_readings["solar_radiation_wm2"],
                        saved_readings["rain_mm"],
                        saved_readings["wind_speed_ms"],
                    ),
                )
        if saved_weather_actuals:
            for saved_prediction_id, saved_readings in saved_weather_actuals.items():
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, lux,\n                        frost_detected, frost_score, frost_confidence)\n                       VALUES (?, 'actual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           wind = excluded.wind,\n                           humidity = excluded.humidity,\n                           rain = excluded.rain,\n                           clouds = excluded.clouds,\n                           pressure = excluded.pressure,\n                           lux = excluded.lux,\n                           frost_detected = excluded.frost_detected,\n                           frost_score = excluded.frost_score,\n                           frost_confidence = excluded.frost_confidence",
                    (
                        saved_prediction_id,
                        saved_readings["temperature"],
                        saved_readings["solar_radiation_wm2"],
                        saved_readings["wind"],
                        saved_readings["humidity"],
                        saved_readings["rain"],
                        saved_readings["clouds"],
                        saved_readings["pressure"],
                        saved_readings["lux"],
                        saved_readings["frost_detected"],
                        saved_readings["frost_score"],
                        saved_readings["frost_confidence"],
                    ),
                )
        if morning_batch_id:
            try:
                await self._ensemble_shadow_handler.capture_morning_batch(
                    morning_batch_id, now
                )
            except Exception as ensemble_error:
                raise RuntimeError(
                    f"mandatory ensemble shadow provenance failed for batch {morning_batch_id}"
                ) from ensemble_error
            filtered_candidate_shadow = filter_candidate_shadow_for_preserved_today(
                candidate_shadow, preserve_today=preserve_today, today=date
            )
            if filtered_candidate_shadow is not None:
                try:
                    candidate_rows = (
                        await self._model_candidate_shadow_handler.capture_morning_run(
                            morning_batch_id, filtered_candidate_shadow
                        )
                    )
                    _LOGGER.info(
                        "  │  Candidate shadow evidence captured for batch %s: %d rows",
                        morning_batch_id,
                        candidate_rows,
                    )
                except Exception as candidate_error:
                    raise RuntimeError(
                        f"mandatory candidate shadow provenance failed for batch {morning_batch_id}"
                    ) from candidate_error
        if actuals_restored > 0:
            _LOGGER.info(
                "  ├─ Restored %d hourly actuals, %d group actuals, %d sensor, %d weather actuals",
                actuals_restored,
                len(saved_group_actuals),
                len(saved_sensor_actuals),
                len(saved_weather_actuals),
            )
        _LOGGER.info(
            "  ├─ Calculating daily totals from saved predictions for 3 days..."
        )
        daily_totals = {}
        for day_offset in range(3):
            forecast_date = (base_date + timedelta_type(days=day_offset)).isoformat()
            total_row = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) as total FROM hourly_predictions WHERE target_date = ?",
                (forecast_date,),
            )
            daily_total = (
                total_row["total"] if total_row and total_row["total"] else 0.0
            )
            daily_totals[forecast_date] = daily_total
            _LOGGER.info("  │  → %s: %.2f kWh", forecast_date, daily_total)
        _LOGGER.info("  ├─ Saving daily forecasts to database for 3 days...")
        weather_hours_per_day = {}
        for weather_data in weather_forecast:
            weather_date = weather_data.get("date") or date
            if weather_date:
                weather_hours_per_day[weather_date] = (
                    weather_hours_per_day.get(weather_date, 0) + 1
                )
        forecast_types = ["today", "tomorrow", "day_after_tomorrow"]
        days_locked = 0
        days_skipped = 0
        for day_offset, forecast_type in enumerate(forecast_types):
            forecast_date = (base_date + timedelta_type(days=day_offset)).isoformat()
            daily_total = daily_totals.get(forecast_date, 0.0)
            if forecast_type == "today" and preserve_today:
                if existing_today_total is not None:
                    daily_totals[forecast_date] = existing_today_total
                _LOGGER.info(
                    "  │  Preserving today lock %.2f kWh source=%s (cycle=%s)",
                    existing_today_total or 0.0,
                    today_lock_source,
                    forecast_cycle or "unspecified",
                )
                days_skipped += 1
                continue
            weather_hour_count = weather_hours_per_day.get(forecast_date, 0)
            prediction_hour_count = predictions_per_day.get(forecast_date, 0)
            if not morning_day_lock_allowed(weather_hour_count, prediction_hour_count):
                days_skipped += 1
                _LOGGER.info(
                    "  │  Skipping lock for %s (%s): weather_hours=%d prediction_hours=%d total=%.2f kWh",
                    forecast_date,
                    forecast_type,
                    weather_hour_count,
                    prediction_hour_count,
                    daily_total,
                )
                continue
            lock_source = (
                today_lock_source
                if forecast_type == "today"
                else CANONICAL_TODAY_LOCK_SOURCE
            )
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                    locked, locked_at, source, created_at)\n                   VALUES (?, ?, ?, ?, TRUE, ?, ?, ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = excluded.prediction_kwh,\n                       prediction_kwh_raw = excluded.prediction_kwh_raw,\n                       locked = excluded.locked,\n                       locked_at = excluded.locked_at,\n                       source = excluded.source",
                (
                    forecast_type,
                    forecast_date,
                    round(daily_total, 2),
                    round(daily_total, 2),
                    now,
                    lock_source,
                    now,
                ),
            )
            days_locked += 1
            continue
        _LOGGER.debug(
            "     → Daily forecasts locked=%d skipped=%d locked_at=%s",
            days_locked,
            days_skipped,
            now,
        )
        daily_total = daily_totals.get(date, 0.0)
        _LOGGER.info("  ├─ Updating coordinator expected daily production...")
        await self.coordinator.data_manager.save_expected_daily_production(
            round(daily_total, 2)
        )
        _LOGGER.info("  ├─ Setting morning routine lock for %s...", date)
        await self.db.execute(
            "UPDATE coordinator_state\n               SET last_set_date = ?, last_updated = ?\n               WHERE id = 1",
            (date, now),
        )
        _LOGGER.debug("     → Lock flag set: last_set_date = %s", date)
        created_count = len(created_prediction_ids)
        _LOGGER.info("  └─ SUMMARY:")
        _LOGGER.info("     • Input entries: %d (3 days)", len(hourly_forecast))
        _LOGGER.info("     • Created predictions: %d", created_count)
        if duplicates_skipped > 0:
            _LOGGER.info("     • Skipped duplicates: %d", duplicates_skipped)
        _LOGGER.info("     • Predictions per day:")
        for target_date, hour_count in sorted(predictions_per_day.items()):
            day_total = daily_totals.get(target_date, 0.0)
            _LOGGER.info(
                "       - %s: %d hours, %.2f kWh", target_date, hour_count, day_total
            )
        _LOGGER.info("     • Coordinator updated: Yes (today=%.2f kWh)", daily_total)
        return (
            set(predictions_per_day.keys()),
            forecast_types,
            daily_totals,
            saved_actuals,
            saved_group_actuals,
            daily_total,
        )

    async def _mirror_operational_morning_snapshot(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_forecast: List[Dict[str, Any]],
        forecast_types: List[str],
        daily_totals: Dict[str, float],
        saved_actuals: Dict[str, Dict[str, Any]],
        saved_group_actuals: Dict[Tuple[str, str], float],
        inverter_max_power: float,
    ) -> None:
        """Mirror the completed morning snapshot into the operational sidecar track. @zara"""
        data_manager = getattr(self.coordinator, "data_manager", None)
        operational_handler = getattr(
            data_manager, "operational_forecast_handler", None
        )
        if operational_handler is None:
            _LOGGER.debug(
                "  ├─ Operational forecast handler unavailable - skipping mirror write"
            )
            return
        hourly_values = []
        seen_prediction_ids = set()
        for hour_data in hourly_forecast:
            hour = hour_data.get("hour")
            if hour is None:
                continue
            target_date = hour_data.get("date") or date
            prediction_id = f"{target_date}_{hour:02d}"
            if prediction_id in seen_prediction_ids:
                continue
            seen_prediction_ids.add(prediction_id)
            normalized_hour = dict(hour_data)
            normalized_hour["date"] = target_date
            predicted_kwh = normalized_hour.get("production_kwh", 0.0)
            if inverter_max_power > 0:
                predicted_kwh = min(predicted_kwh, inverter_max_power)
            normalized_hour["production_kwh"] = predicted_kwh
            hourly_values.append(normalized_hour)
        weather_by_prediction_id = {}
        for weather_data in weather_forecast:
            weather_date = weather_data.get("date") or date
            weather_hour = weather_data.get("hour")
            if not weather_hour is not None:
                continue
            weather_by_prediction_id[f"{weather_date}_{weather_hour:02d}"] = (
                weather_data
            )
        base_date = datetime.fromisoformat(date).date()
        daily_values = []
        for day_offset, forecast_type in enumerate(forecast_types):
            forecast_date = base_date + timedelta(days=day_offset)
            forecast_date_text = forecast_date.isoformat()
            daily_values.append(
                {
                    "forecast_type": forecast_type,
                    "forecast_date": forecast_date_text,
                    "prediction_kwh": round(
                        daily_totals.get(forecast_date_text, 0.0), 2
                    ),
                    "prediction_kwh_raw": round(
                        daily_totals.get(forecast_date_text, 0.0), 2
                    ),
                    "actual_kwh": None,
                }
            )
        actuals_by_prediction_id = {
            prediction_id: actual_data["actual_kwh"]
            for prediction_id, actual_data in saved_actuals.items()
            if actual_data.get("actual_kwh") is not None
        }
        mirror_succeeded = await operational_handler.mirror_morning_snapshot(
            snapshot_date=date,
            hourly_values=hourly_values,
            daily_values=daily_values,
            actuals_by_prediction_id=actuals_by_prediction_id,
            group_actuals_by_key=saved_group_actuals,
            weather_by_prediction_id=weather_by_prediction_id,
        )
        if mirror_succeeded:
            _LOGGER.info("  ├─ Operational morning snapshot mirrored successfully")
        else:
            _LOGGER.warning(
                "  ├─ Operational morning snapshot mirror failed - core morning snapshot remains authoritative"
            )
        return None if True else None

    async def _validate_prerequisites(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_hourly: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
    ) -> bool:
        """Validate all prerequisites before creating predictions. @zara"""
        try:
            datetime.fromisoformat(date)
            if not hourly_forecast or not isinstance(hourly_forecast, list):
                _LOGGER.error("Invalid hourly_forecast: %s", type(hourly_forecast))
                return False
            if len(hourly_forecast) < 8:
                _LOGGER.warning(
                    "Only %d hours in forecast (expected 8+ for winter, 12+ for summer)",
                    len(hourly_forecast),
                )
            if not weather_hourly or not isinstance(weather_hourly, list):
                _LOGGER.error("Invalid weather_hourly: %s", type(weather_hourly))
                return False
            if not astronomy_data or not isinstance(astronomy_data, dict):
                _LOGGER.error("Invalid astronomy_data: %s", type(astronomy_data))
                return False
            _LOGGER.debug("All prerequisites validated")
            return True
        except Exception as error:
            _LOGGER.error("Prerequisite validation failed: %s", error, exc_info=True)
            return False

    async def _verify_integrity(self, date: str, created_dates: set = None) -> bool:
        """Verify data integrity after creation. @zara"""
        try:
            if created_dates:
                dates_to_verify = sorted(created_dates)
            else:
                from datetime import timedelta as timedelta_type

                base_date = datetime.fromisoformat(date).date()
                dates_to_verify = [
                    (base_date + timedelta_type(days=day_offset)).isoformat()
                    for day_offset in range(3)
                ]
            for target_date in dates_to_verify:
                row = await self.db.fetchone(
                    "SELECT COUNT(*) FROM hourly_predictions\n                       WHERE target_date = ?",
                    (target_date,),
                )
                if not row or row[0] == 0:
                    _LOGGER.error(
                        "No predictions found for %s after creation!", target_date
                    )
                    return False
                duplicate_row = await self.db.fetchone(
                    "SELECT prediction_id, COUNT(*) as cnt\n                       FROM hourly_predictions\n                       WHERE target_date = ?\n                       GROUP BY prediction_id\n                       HAVING cnt > 1",
                    (target_date,),
                )
                if duplicate_row:
                    _LOGGER.error(
                        "Duplicate prediction IDs found for %s: %s",
                        target_date,
                        duplicate_row[0],
                    )
                    return False
            _LOGGER.debug(
                "Integrity verified for %d days starting from %s, no duplicates",
                len(dates_to_verify),
                dates_to_verify[0] if dates_to_verify else date,
            )
            return True
        except Exception as error:
            _LOGGER.error("Integrity verification failed: %s", error, exc_info=True)
            return False

    async def _send_critical_alert(self, title: str, details: str) -> None:
        """Send critical alert about failure. @zara"""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": f"Solar Forecast ML: {title}",
                    "message": f"{details}\n\nThe morning forecast was not saved. Check that the weather integration is available, then reload Solar Forecast ML or wait for the next scheduled run.",
                    "notification_id": "solar_forecast_ml_critical",
                },
            )
        except Exception as error:
            _LOGGER.error("Could not send critical alert: %s", error)
        return None if True else None

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
            unused_layout_slot = None
        try:
            if self.db is None:
                _LOGGER.warning("Database not available for updating hourly actual")
                return False
            prediction_id = f"{date}_{hour:02d}"
            measured_at = dt_util.now()
            row = await self.db.fetchone(
                "SELECT prediction_kwh, has_weather_alert, weather_alert_type\n                   FROM hourly_predictions WHERE prediction_id = ?",
                (prediction_id,),
            )
            if not row:
                _LOGGER.warning(
                    "No prediction found for %s hour %d to update", date, hour
                )
                return False
            if hasattr(self.db, "is_actual_backfill_blocked"):
                backfill_blocked = await self.db.is_actual_backfill_blocked(date, hour)
                if backfill_blocked:
                    quarantine_reason = "emergency_outage_backfill_blocked"
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                           SET exclude_from_learning = 1,\n                               exclude_from_clean_evaluation = 1,\n                               actual_backfill_blocked = 1,\n                               data_quarantine_reason = COALESCE(data_quarantine_reason, ?),\n                               data_quarantine_at = COALESCE(data_quarantine_at, ?),\n                               mppt_throttle_reason = COALESCE(NULLIF(mppt_throttle_reason, ''), ?)\n                           WHERE prediction_id = ?",
                        (
                            quarantine_reason,
                            measured_at.isoformat(),
                            quarantine_reason,
                            prediction_id,
                        ),
                    )
                    _LOGGER.warning(
                        "Hourly actual write blocked by emergency outage quarantine: %s",
                        prediction_id,
                    )
                    return True
            predicted_kwh = row[0] or 0.0
            previous_alert_type = row[2] if row[1] else None
            error_kwh = actual_kwh - predicted_kwh
            error_percent = error_kwh / predicted_kwh * 100 if predicted_kwh > 0 else 0
            accuracy_percent = 100 - abs(error_percent)
            sensors_complete = self._check_sensor_completeness(sensor_data)
            hour_astronomy = await self._get_astronomy_for_hour(date, hour)
            forecast_weather = None
            if weather_actual:
                forecast_weather_row = await self.db.fetchone(
                    "SELECT temperature, solar_radiation_wm2, rain, clouds\n                       FROM prediction_weather\n                       WHERE prediction_id = ? AND weather_type = 'forecast'",
                    (prediction_id,),
                )
                if forecast_weather_row:
                    forecast_weather = {
                        "temperature_c": forecast_weather_row[0],
                        "solar_radiation_wm2": forecast_weather_row[1],
                        "precipitation_mm": forecast_weather_row[2],
                        "cloud_cover_percent": forecast_weather_row[3],
                    }
            is_clipped = False
            if inverter_max_power > 0:
                is_clipped = actual_kwh >= inverter_max_power * 0.95
            has_weather_alert, weather_alert_type = self._detect_weather_alert(
                weather_actual, forecast_weather
            )
            if (
                not has_weather_alert
                and production_miss_uplift(actual_kwh, predicted_kwh) is not None
            ):
                has_weather_alert = True
                weather_alert_type = WEATHER_ALERT_UNEXPECTED_SUN
            flow_evidence = {}
            if zero_export_mode or has_battery:
                flow_evidence = await self._get_curtailment_flow_evidence(
                    date,
                    hour,
                    max_grid_export_w=max_grid_export_w,
                    zero_export_mode=zero_export_mode,
                )
            mppt_throttled = False
            throttle_reason = None
            if zero_export_mode or has_battery:
                mppt_throttled, throttle_reason = self._detect_mppt_throttle(
                    actual_kwh,
                    has_battery,
                    solar_to_battery_power,
                    weather_actual=weather_actual,
                    astronomy_data=hour_astronomy,
                    flow_evidence=flow_evidence,
                    max_grid_export_w=max_grid_export_w,
                    zero_export_mode=zero_export_mode,
                    battery_soc=battery_soc,
                )
            curtailment_quarantine, curtailment_reason = (
                self.evaluate_curtailment_learning_quarantine(
                    actual_kwh=actual_kwh,
                    solar_to_battery_power=solar_to_battery_power,
                    weather_actual=weather_actual,
                    astronomy_data=hour_astronomy,
                    flow_evidence=flow_evidence,
                    max_grid_export_w=max_grid_export_w,
                    zero_export_mode=zero_export_mode,
                    has_battery=has_battery,
                    battery_soc=battery_soc,
                )
            )
            if curtailment_quarantine and (not throttle_reason):
                throttle_reason = curtailment_reason
            has_weather_alert, weather_alert_type = self._resolve_detection_ownership(
                is_clipped, mppt_throttled, has_weather_alert, weather_alert_type
            )
            weather_miss_class = None
            reforecast_eligible = False
            if has_weather_alert:
                weather_miss_class, reforecast_eligible = (
                    await self._classify_weather_miss(
                        date=date,
                        hour=hour,
                        alert_type=weather_alert_type,
                        weather_actual=weather_actual,
                        weather_forecast=forecast_weather,
                        is_clipped=is_clipped,
                        mppt_throttled=mppt_throttled,
                    )
                )
            if (
                weather_miss_class == WEATHER_MISS_CLASS_FORECAST_MISS
                and weather_alert_type in LEARNABLE_WEATHER_MISS_ALERT_TYPES
            ):
                await self._promote_recent_weather_miss_sequence(
                    date, hour, weather_alert_type
                )
            exclude_learning = (
                is_clipped
                or mppt_throttled
                or throttle_reason_excludes_from_learning(throttle_reason)
                or curtailment_quarantine
                or (
                    has_weather_alert
                    and weather_alert_excludes_from_learning(
                        weather_alert_type, weather_miss_class
                    )
                )
            )
            exclude_clean_evaluation = exclude_learning or (
                has_weather_alert
                and weather_alert_excludes_from_clean_evaluation(weather_alert_type)
            )
            if not exclude_learning:
                if getattr(self.coordinator, "learning_paused", False):
                    exclude_learning = True
                    exclude_clean_evaluation = True
                    has_weather_alert = True
                    weather_alert_type = "manual_pause"
                    _LOGGER.debug(
                        "Hour %s excluded: learning paused by user", prediction_id
                    )
            if is_reconstructed and reconstructed_exclude_from_learning:
                exclude_learning = True
                exclude_clean_evaluation = True
                if not throttle_reason:
                    throttle_reason = "reconstructed_gap_interpolation"
            await self.db.execute(
                "UPDATE hourly_predictions\n                   SET actual_kwh = ?,\n                       actual_measured_at = ?,\n                       accuracy_percent = ?,\n                       error_kwh = ?,\n                       error_percent = ?,\n                       has_sensor_data = TRUE,\n                       sensor_data_complete = ?,\n                       inverter_clipped = ?,\n                       exclude_from_learning = ?,\n                       exclude_from_clean_evaluation = ?,\n                       mppt_throttled = ?,\n                       mppt_throttle_reason = ?,\n                       has_weather_alert = ?,\n                       weather_alert_type = ?,\n                       weather_miss_class = ?,\n                       reforecast_trigger_eligible = ?\n                   WHERE prediction_id = ?",
                (
                    actual_kwh,
                    measured_at,
                    accuracy_percent,
                    error_kwh,
                    error_percent,
                    sensors_complete,
                    is_clipped,
                    exclude_learning,
                    exclude_clean_evaluation,
                    mppt_throttled,
                    throttle_reason,
                    has_weather_alert,
                    weather_alert_type,
                    weather_miss_class,
                    reforecast_eligible,
                    prediction_id,
                ),
            )
            if sensor_data:
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms)\n                       VALUES (?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms",
                    (
                        prediction_id,
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
                        prediction_id,
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
            expected_group_count = len(
                getattr(self.coordinator, "panel_groups", []) or []
            )
            group_actuals_complete = False
            if panel_group_actuals:
                groups_updated = 0
                for group_name, group_actual_kwh in panel_group_actuals.items():
                    if group_actual_kwh is not None:
                        topology_meta = (
                            await self.db.get_active_panel_group_topology_meta(
                                group_name
                            )
                        )
                        await self.db.execute(
                            "INSERT INTO prediction_panel_groups\n                               (prediction_id, group_name, prediction_kwh, actual_kwh,\n                                raw_physics_kwh,\n                                config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction)\n                               VALUES (?, ?, 0.0, ?, NULL, ?, ?, ?, ?)\n                               ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                                   actual_kwh = excluded.actual_kwh,\n                                   config_epoch_id = COALESCE(excluded.config_epoch_id, config_epoch_id),\n                                   group_uid = COALESCE(excluded.group_uid, group_uid),\n                                   group_lineage_uid = COALESCE(excluded.group_lineage_uid, group_lineage_uid),\n                                   power_wp_at_prediction = COALESCE(excluded.power_wp_at_prediction, power_wp_at_prediction)",
                            (
                                prediction_id,
                                group_name,
                                group_actual_kwh,
                                topology_meta.get("config_epoch_id"),
                                topology_meta.get("group_uid"),
                                topology_meta.get("group_lineage_uid"),
                                topology_meta.get("power_wp_at_prediction"),
                            ),
                        )
                        groups_updated += 1
                if groups_updated > 0:
                    group_count_row = await self.db.fetchone(
                        "SELECT COUNT(*),\n                                  SUM(CASE WHEN actual_kwh IS NOT NULL THEN 1 ELSE 0 END)\n                           FROM prediction_panel_groups\n                           WHERE prediction_id = ?",
                        (prediction_id,),
                    )
                    stored_group_count = (
                        int(group_count_row[0] or 0) if group_count_row else 0
                    )
                    measured_group_count = (
                        int(group_count_row[1] or 0) if group_count_row else 0
                    )
                    group_actuals_complete = (
                        expected_group_count > 0
                        and stored_group_count == expected_group_count
                        and (measured_group_count == expected_group_count)
                        and (len(panel_group_actuals) == expected_group_count)
                    )
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                           SET has_panel_group_actuals = ?\n                           WHERE prediction_id = ?",
                        (group_actuals_complete, prediction_id),
                    )
                    _LOGGER.debug(
                        "Panel group actuals updated for %s: %d groups (stored=%d/%d)",
                        prediction_id,
                        groups_updated,
                        measured_group_count,
                        expected_group_count,
                    )
                    group_exclusion_reason = panel_group_exclusion_reason
                    if (
                        is_reconstructed
                        and reconstructed_exclude_from_learning
                        and (not group_exclusion_reason)
                    ):
                        group_exclusion_reason = "reconstructed_gap_interpolation"
                    if group_exclusion_reason:
                        try:
                            for (
                                group_name,
                                group_actual_kwh,
                            ) in panel_group_actuals.items():
                                if group_actual_kwh is None:
                                    continue
                                await self.db.update_prediction_panel_group_flags(
                                    prediction_id,
                                    group_name,
                                    {
                                        "exclude_from_learning_group": True,
                                        "exclusion_reason_group": group_exclusion_reason,
                                    },
                                )
                        except Exception as group_flags_error:
                            _LOGGER.warning(
                                "Panel group exclusion flags could not be updated for %s: %s",
                                prediction_id,
                                group_flags_error,
                            )
            try:
                prediction_context = {
                    "prediction_id": prediction_id,
                    "actual_kwh": actual_kwh,
                    "astronomy": hour_astronomy,
                    "sensor_actual": sensor_data,
                    "weather_actual": weather_actual,
                    "weather_forecast": forecast_weather,
                    "target_hour": hour,
                    "target_datetime": f"{date}T{hour:02d}:00:00",
                    "flags": {
                        "inverter_clipped": is_clipped,
                        "mppt_throttled": mppt_throttled,
                        "partial_panel_group_actuals": bool(panel_group_actuals)
                        and (not group_actuals_complete),
                        "mppt_throttle_reason": throttle_reason,
                        "curtailment_suspected": self.shadow_curtailment_suspected(
                            flow_evidence,
                            battery_soc=battery_soc,
                            max_grid_export_w=max_grid_export_w,
                            zero_export_mode=zero_export_mode,
                        ),
                    },
                }
                shadow_result = await self._shadow_detector.detect_shadow_ensemble(
                    prediction_context
                )
                if shadow_result:
                    await self.db.save_hourly_shadow_detection(
                        prediction_id, shadow_result
                    )
                    if self.coordinator.update_shadow_detection_cache(
                        prediction_id, shadow_result
                    ):
                        self.coordinator.async_update_listeners()
                    _LOGGER.debug(
                        "Shadow detection for %s: %s (%.1f%%)",
                        prediction_id,
                        shadow_result.get("shadow_type", "unknown"),
                        shadow_result.get("shadow_percent", 0),
                    )
                if group_actuals_complete and panel_group_actuals:
                    group_physics_rows = await self.db.fetchall(
                        "SELECT group_name, actual_kwh, physics_kwh\n                           FROM prediction_panel_groups\n                           WHERE prediction_id = ?\n                             AND actual_kwh IS NOT NULL\n                             AND physics_kwh IS NOT NULL\n                             AND physics_kwh > 0",
                        (prediction_id,),
                    )
                    stored_group_names = {row[0] for row in group_physics_rows}
                    received_group_names = set(panel_group_actuals)
                    if (
                        len(group_physics_rows) == expected_group_count
                        and len(received_group_names) == expected_group_count
                        and (stored_group_names == received_group_names)
                    ):
                        group_evidence = {
                            row[0]: {
                                "actual_kwh": row[1],
                                "theoretical_max_kwh": row[2],
                            }
                            for row in group_physics_rows
                        }
                        for group_name in list(group_evidence):
                            group_astronomy_row = await self.db.fetchone(
                                "SELECT theoretical_kwh\n                                     FROM astronomy_cache_panel_groups\n                                    WHERE cache_date = ? AND hour = ? AND group_name = ?",
                                (date, hour, group_name),
                            )
                            if group_astronomy_row:
                                if group_astronomy_row[0] is not None:
                                    group_evidence[group_name]["clear_sky_kwh"] = (
                                        group_astronomy_row[0]
                                    )
                        group_shadow_results = (
                            await self._shadow_detector.detect_shadow_per_group(
                                prediction_context,
                                group_evidence,
                                system_ensemble=shadow_result,
                            )
                        )
                        await self._apply_group_shadow_learning_exclusions(
                            prediction_id,
                            prediction_context["flags"],
                            group_shadow_results,
                            (weather_actual or {}).get("snow_per_group", {}),
                            panel_group_exclusion_reason,
                        )
                    else:
                        _LOGGER.debug(
                            "Per-group shadow detection skipped for %s: incomplete expected data",
                            prediction_id,
                        )
            except _ShadowLearningExclusionError:
                raise
            except Exception as shadow_error:
                _LOGGER.debug(
                    "Shadow detection skipped for %s: %s", prediction_id, shadow_error
                )
            try:
                await self._ensemble_shadow_handler.evaluate_prediction(prediction_id)
            except Exception as shadow_error:
                _LOGGER.debug(
                    "Ensemble shadow evaluation skipped for %s: %s",
                    prediction_id,
                    shadow_error,
                )
            try:
                await self._model_candidate_shadow_handler.evaluate_prediction(
                    prediction_id
                )
            except Exception as candidate_error:
                _LOGGER.warning(
                    "Model candidate shadow evaluation deferred for EOD retry for %s: %s",
                    prediction_id,
                    candidate_error,
                )
            if (
                has_weather_alert
                and weather_alert_type
                and (weather_alert_type != previous_alert_type)
                and (
                    weather_alert_type
                    in {
                        "sudden_storm",
                        "unexpected_rain",
                        "unexpected_snow",
                        "unexpected_clouds",
                    }
                )
            ):
                notification_service = self.hass.data.get(DOMAIN, {}).get(
                    "notification_service"
                )
                if notification_service is not None:
                    try:
                        await notification_service.show_weather_alert(
                            alert_type=weather_alert_type,
                            reason=weather_miss_class or weather_alert_type,
                            hour=hour,
                            date_str=date,
                            weather_actual=weather_actual,
                            weather_forecast=forecast_weather,
                        )
                    except Exception:
                        _LOGGER.warning(
                            "Weather alert notification failed for %s",
                            prediction_id,
                            exc_info=True,
                        )
            if True:
                return True
        except Exception as error:
            _LOGGER.error("Error updating hourly actual: %s", error, exc_info=True)
            return False

    async def _apply_group_shadow_learning_exclusions(
        self,
        prediction_id: str,
        flags: Dict[str, Any],
        shadow_per_group: Dict[str, Dict[str, Any]],
        snow_per_group: Dict[str, Dict[str, Any]],
        panel_group_exclusion_reason: Optional[str],
    ) -> None:
        """Persist only new group-specific shadow exclusions."""
        if panel_group_exclusion_reason:
            return
        prediction_context = {"flags": flags}
        pending_exclusions = []
        for group_name, group_shadow in shadow_per_group.items():
            if group_shadow.get("shadow_type") == "not_evaluable":
                continue
            existing_exclusion = await self.db.fetchone(
                "SELECT exclude_from_learning_group\n                   FROM prediction_panel_groups\n                   WHERE prediction_id = ? AND group_name = ?",
                (prediction_id, group_name),
            )
            if existing_exclusion:
                if existing_exclusion[0]:
                    continue
            exclude_group, exclusion_reason = should_exclude_group_from_learning(
                prediction_context, group_name, snow_per_group, shadow_per_group
            )
            if exclude_group:
                pending_exclusions.append((group_name, exclusion_reason))
        if not pending_exclusions:
            return
        try:
            async with self.db.transaction():
                for group_name, exclusion_reason in pending_exclusions:
                    await self.db.update_prediction_panel_group_flags(
                        prediction_id,
                        group_name,
                        {
                            "exclude_from_learning_group": True,
                            "exclusion_reason_group": exclusion_reason,
                        },
                        auto_commit=False,
                    )
        except Exception as write_error:
            try:
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           data_quarantine_reason = COALESCE(\n                               NULLIF(data_quarantine_reason, ''),\n                               'shadow_exclusion_write_failed'\n                           )\n                       WHERE prediction_id = ?",
                    (prediction_id,),
                )
            except Exception:
                _LOGGER.exception(
                    "Shadow exclusion recovery gate failed for %s", prediction_id
                )
            raise _ShadowLearningExclusionError(
                f"Could not persist local-shadow learning exclusions for {prediction_id}"
            ) from write_error
        return None if True else None

    async def _get_astronomy_for_hour(self, date: str, hour: int) -> Dict[str, Any]:
        """Get astronomy data for a specific hour from cache. @zara V16.1

        Fixed column names to match actual DB schema V16.2.
        """
        try:
            row = await self.db.fetchone(
                "SELECT sun_elevation_deg, sun_azimuth_deg,\n                          clear_sky_radiation_wm2, theoretical_max_kwh, sunrise, sunset,\n                          daylight_hours, solar_noon\n                   FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = ?",
                (date, hour),
            )
            if row:
                sun_elevation = row[0] or 0
                is_daylight = sun_elevation > 0
                return {
                    "sun_elevation_deg": row[0],
                    "sun_azimuth_deg": row[1],
                    "is_daylight": is_daylight,
                    "clear_sky_radiation_wm2": row[2],
                    "theoretical_max_kwh": row[3],
                    "theoretical_kwh": row[3],
                    "sunrise": row[4],
                    "sunset": row[5],
                    "daylight_hours": row[6],
                    "solar_noon": row[7],
                }
            return {}
        except Exception as error:
            _LOGGER.debug(
                "Error getting astronomy for %s hour %d: %s", date, hour, error
            )
            return {}

    @staticmethod
    def _check_sensor_completeness(sensor_data: Dict[str, Any]) -> bool:
        """Check if enough sensors have valid data. @zara"""
        if not sensor_data:
            return False
        valid_sensor_count = sum(
            (1 for reading in sensor_data.values() if reading is not None)
        )
        return valid_sensor_count >= 3

    @staticmethod
    def _detect_weather_alert(
        weather_actual: Optional[Dict[str, Any]],
        weather_forecast: Optional[Dict[str, Any]],
    ) -> Tuple[bool, Optional[str]]:
        'Detect unexpected weather conditions that invalidate learning data. @zara\n\nReturns:\n    Tuple of (has_alert, alert_type)\n\nAlert types:\n- "snow_covered_panels": Physical panel coverage detected\n- "unexpected_snow": Actual snow not in forecast\n- "unexpected_rain": Actual rain not in forecast\n- "unexpected_clouds": Actual radiation far below forecast\n- "unexpected_sun": Actual kWh far above the published hour forecast\n'
        if not weather_actual:
            return (False, None)
        if weather_actual.get("snow_covered_panels"):
            return (True, "snow_covered_panels")
        if not weather_forecast:
            return (False, None)
        actual_rain = (
            weather_actual.get("rain") or weather_actual.get("precipitation_mm") or 0
        )
        forecast_rain = (
            weather_forecast.get("rain")
            or weather_forecast.get("precipitation_mm")
            or 0
        )
        temperature = weather_actual.get("temperature") or weather_actual.get(
            "temperature_c"
        )
        actual_radiation = weather_actual.get("solar_radiation_wm2")
        forecast_radiation = weather_forecast.get("solar_radiation_wm2")
        if actual_rain > 0.5 and forecast_rain < 0.1:
            if temperature is not None and temperature < 2.0:
                return (True, "unexpected_snow")
            return (True, "unexpected_rain")
        if (
            actual_radiation is not None
            and forecast_radiation is not None
            and (forecast_radiation > 100)
            and (actual_radiation < forecast_radiation * 0.4)
        ):
            return (True, "unexpected_clouds")
        return (False, None)

    async def _classify_weather_miss(
        self,
        date: str,
        hour: int,
        alert_type: Optional[str],
        weather_actual: Optional[Dict[str, Any]],
        weather_forecast: Optional[Dict[str, Any]],
        is_clipped: bool,
        mppt_throttled: bool,
    ) -> Tuple[str, bool]:
        if is_clipped or mppt_throttled:
            return (WEATHER_MISS_CLASS_TECHNICAL, False)
        if not alert_type:
            return (WEATHER_MISS_CLASS_UNKNOWN, False)
        if alert_type == WEATHER_ALERT_UNEXPECTED_SUN:
            return (WEATHER_MISS_CLASS_LOCAL_EVENT, True)
        if alert_type not in LEARNABLE_WEATHER_MISS_ALERT_TYPES:
            return (WEATHER_MISS_CLASS_NON_LEARNABLE_WEATHER, False)
        if not weather_actual or not weather_forecast:
            return (WEATHER_MISS_CLASS_UNKNOWN, False)
        recent_miss_count = await self._count_recent_same_weather_miss(
            date, hour, alert_type, max_lookback_hours=2
        )
        if recent_miss_count >= 2:
            return (WEATHER_MISS_CLASS_FORECAST_MISS, False)
        if self._is_abrupt_local_weather_event(
            alert_type, weather_actual, weather_forecast
        ):
            return (WEATHER_MISS_CLASS_LOCAL_EVENT, True)
        return (WEATHER_MISS_CLASS_UNKNOWN, False)

    async def _count_recent_same_weather_miss(
        self, date: str, hour: int, alert_type: str, max_lookback_hours: int
    ) -> int:
        alert_rows = await self.db.fetchall(
            "SELECT target_hour, weather_alert_type\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour < ?\n                 AND target_hour >= ?\n                 AND has_weather_alert = TRUE\n               ORDER BY target_hour DESC",
            (date, hour, max(0, hour - max_lookback_hours)),
        )
        matching_count = 0
        expected_hour = hour - 1
        for row in alert_rows:
            target_hour = int(row["target_hour"])
            if target_hour != expected_hour:
                break
            if row["weather_alert_type"] != alert_type:
                break
            matching_count += 1
            expected_hour -= 1
        return matching_count if True else None

    async def _promote_recent_weather_miss_sequence(
        self, date: str, hour: int, alert_type: str
    ) -> None:
        await self.db.execute(
            f"UPDATE hourly_predictions\n               SET weather_miss_class = ?,\n                   exclude_from_learning = FALSE,\n                   reforecast_trigger_eligible = FALSE\n               WHERE target_date = ?\n                 AND target_hour BETWEEN ? AND ?\n                 AND weather_alert_type = ?\n                 AND COALESCE(mppt_throttled, 0) = 0\n                 AND COALESCE(inverter_clipped, 0) = 0\n                 AND COALESCE(mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})",
            (
                WEATHER_MISS_CLASS_FORECAST_MISS,
                date,
                max(0, hour - 2),
                hour,
                alert_type,
            ),
        )

    @staticmethod
    def _is_abrupt_local_weather_event(
        alert_type: str,
        weather_actual: Dict[str, Any],
        weather_forecast: Dict[str, Any],
    ) -> bool:
        actual_rain = (
            weather_actual.get("rain") or weather_actual.get("precipitation_mm") or 0
        )
        forecast_rain = (
            weather_forecast.get("rain")
            or weather_forecast.get("precipitation_mm")
            or 0
        )
        if alert_type in ("unexpected_rain", "unexpected_snow"):
            return actual_rain > 0.5 and forecast_rain < 0.1
        if alert_type == WEATHER_ALERT_UNEXPECTED_SUN:
            return True
        actual_radiation = weather_actual.get("solar_radiation_wm2")
        forecast_radiation = weather_forecast.get("solar_radiation_wm2")
        if (
            alert_type == "unexpected_clouds"
            and actual_radiation is not None
            and (forecast_radiation is not None)
            and (forecast_radiation > 100)
        ):
            return actual_radiation < forecast_radiation * 0.4
        return False

    @staticmethod
    def _resolve_detection_ownership(
        is_clipped: bool,
        mppt_throttled: bool,
        has_weather_alert: bool,
        weather_alert_type: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """Keep technical curtailment from being stored as a cloud or sun miss."""
        if (is_clipped or mppt_throttled) and weather_alert_type in (
            "unexpected_clouds",
            WEATHER_ALERT_UNEXPECTED_SUN,
        ):
            return (False, None)
        return (has_weather_alert, weather_alert_type)

    @staticmethod
    def _detect_mppt_throttle(
        actual_kwh: float,
        has_battery: bool,
        solar_to_battery_power: Optional[float],
        weather_actual: Optional[Dict[str, Any]] = None,
        astronomy_data: Optional[Dict[str, Any]] = None,
        flow_evidence: Optional[Dict[str, Any]] = None,
        max_grid_export_w: Optional[float] = None,
        zero_export_mode: bool = False,
        battery_soc: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Detect hard MPPT throttle conditions with flow-backed evidence."""
        curtailment_state = MorningRoutineHandler.evaluate_curtailment_state(
            actual_kwh=actual_kwh,
            solar_to_battery_power=solar_to_battery_power,
            weather_actual=weather_actual,
            astronomy_data=astronomy_data,
            flow_evidence=flow_evidence,
            max_grid_export_w=max_grid_export_w,
            zero_export_mode=zero_export_mode,
            has_battery=has_battery,
            battery_soc=battery_soc,
        )
        if curtailment_state["hard"]:
            return (True, curtailment_state["reason"])
        if actual_kwh <= 0.01:
            return (False, None)
        full_battery_throttle = (
            has_battery
            and solar_to_battery_power is not None
            and (solar_to_battery_power < MPPT_THROTTLE_BATTERY_POWER_THRESHOLD)
            and MorningRoutineHandler.battery_soc_reached_full(
                battery_soc, flow_evidence
            )
        )
        if weather_actual:
            cloud_cover = weather_actual.get("cloud_cover_percent")
            radiation = weather_actual.get("solar_radiation_wm2")
            if cloud_cover is not None and cloud_cover > MPPT_CLEAR_SKY_CLOUDS_MAX:
                return (False, None)
            if radiation is not None and radiation < MPPT_CLEAR_SKY_GHI_THRESHOLD:
                return (False, None)
        theoretical_kwh = None
        if astronomy_data:
            theoretical_kwh = astronomy_data.get("theoretical_max_kwh")
        if not theoretical_kwh or theoretical_kwh <= 0:
            return (False, None)
        production_ratio = actual_kwh / theoretical_kwh
        if production_ratio > MPPT_THROTTLE_PRODUCTION_RATIO:
            return (False, None)
        if actual_kwh < theoretical_kwh * 0.15:
            return (False, None)
        if production_ratio < MPPT_THROTTLE_PRODUCTION_RATIO:
            if full_battery_throttle:
                return (True, THROTTLE_REASON_FULL_BATTERY)
            if not has_battery:
                return (True, THROTTLE_REASON_ZERO_EXPORT)
        return (False, None)

    async def _detect_curtailment_learning_quarantine(
        self,
        date: str,
        hour: int,
        actual_kwh: float,
        has_battery: bool,
        zero_export_mode: bool,
        solar_to_battery_power: Optional[float],
        weather_actual: Optional[Dict[str, Any]] = None,
        astronomy_data: Optional[Dict[str, Any]] = None,
        max_grid_export_w: Optional[float] = None,
        flow_evidence: Optional[Dict[str, Any]] = None,
        battery_soc: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Detect probable curtailment that is unsafe for learning but not hard MPPT."""
        if not has_battery and (not zero_export_mode):
            return (False, None)
        if actual_kwh <= 0.01:
            return (False, None)
        flows = flow_evidence
        if flows is None:
            flows = await self._get_curtailment_flow_evidence(
                date,
                hour,
                max_grid_export_w=max_grid_export_w,
                zero_export_mode=zero_export_mode,
            )
        return self.evaluate_curtailment_learning_quarantine(
            actual_kwh=actual_kwh,
            solar_to_battery_power=solar_to_battery_power,
            weather_actual=weather_actual,
            astronomy_data=astronomy_data,
            flow_evidence=flows,
            max_grid_export_w=max_grid_export_w,
            zero_export_mode=zero_export_mode,
            has_battery=has_battery,
            battery_soc=battery_soc,
        )

    async def _get_curtailment_flow_evidence(
        self,
        date: str,
        hour: int,
        max_grid_export_w: Optional[float] = None,
        zero_export_mode: bool = False,
    ) -> Dict[str, Any]:
        """Read optional power-flow evidence for a forecast hour."""
        flow_result = {}
        export_limit = MorningRoutineHandler._effective_export_limit_w(
            max_grid_export_w, zero_export_mode
        )
        cap_tolerance = MorningRoutineHandler._cap_tolerance_w(export_limit)
        try:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS samples,\n                          SUM(CASE WHEN solar_to_battery_w <= ? THEN 1 ELSE 0 END) AS low_battery_samples,\n                          SUM(CASE\n                                  WHEN solar_to_battery_w <= ?\n                                   AND solar_to_grid_w <= ?\n                                   AND house_consumption_w > ?\n                                   AND solar_to_house_w >= house_consumption_w * ?\n                                  THEN 1 ELSE 0\n                              END) AS house_match_samples,\n                          SUM(CASE\n                                  WHEN house_consumption_w > ?\n                                   AND (\n                                       solar_to_house_w >= house_consumption_w * ?\n                                       OR grid_to_house_w <= ?\n                                   )\n                                  THEN 1 ELSE 0\n                              END) AS house_covered_samples,\n                          SUM(CASE\n                                  WHEN ? > 0\n                                   AND (\n                                       ABS(solar_power_w - ?) <= ?\n                                       OR ABS(solar_to_grid_w - ?) <= ?\n                                       OR ABS(solar_power_w - (house_consumption_w + ?)) <= ?\n                                   )\n                                  THEN 1 ELSE 0\n                              END) AS cap_limit_samples,\n                          AVG(solar_to_battery_w) AS avg_battery_w,\n                          AVG(solar_power_w) AS avg_solar_w,\n                          AVG(house_consumption_w) AS avg_house_w,\n                          AVG(solar_to_house_w) AS avg_solar_to_house_w,\n                          AVG(solar_to_grid_w) AS avg_solar_to_grid_w,\n                          AVG(grid_to_house_w) AS avg_grid_to_house_w,\n                          MAX(battery_soc) AS max_battery_soc,\n                          AVG(battery_soc) AS avg_battery_soc\n                   FROM stats_power_sources\n                   WHERE date = ? AND hour = ?",
                (
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_MIN_HOUSE_W,
                    CURTAILMENT_FLOW_HOUSE_MATCH_RATIO,
                    CURTAILMENT_FLOW_LOW_BATTERY_W,
                    export_limit,
                    export_limit,
                    cap_tolerance,
                    export_limit,
                    cap_tolerance,
                    export_limit,
                    cap_tolerance,
                    date,
                    hour,
                ),
            )
            if row:
                if (row[0] or 0) > 0:
                    flow_result.update(
                        {
                            "samples": int(row[0] or 0),
                            "low_battery_samples": int(row[1] or 0),
                            "house_match_samples": int(row[2] or 0),
                            "house_covered_samples": int(row[3] or 0),
                            "cap_limit_samples": int(row[4] or 0),
                            "avg_battery_w": float(row[5] or 0.0),
                            "avg_solar_w": float(row[6] or 0.0),
                            "avg_house_w": float(row[7] or 0.0),
                            "avg_solar_to_house_w": float(row[8] or 0.0),
                            "avg_solar_to_grid_w": float(row[9] or 0.0),
                            "avg_grid_to_house_w": float(row[10] or 0.0),
                            "max_battery_soc": (
                                float(row[11]) if row[11] is not None else None
                            ),
                            "avg_battery_soc": (
                                float(row[12]) if row[12] is not None else None
                            ),
                            "export_limit_w": export_limit,
                            "cap_tolerance_w": cap_tolerance,
                        }
                    )
                    return flow_result
        except Exception:
            pass
        try:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS samples,\n                          SUM(CASE WHEN solar_to_battery_watt <= ? THEN 1 ELSE 0 END) AS low_battery_samples,\n                          AVG(solar_to_battery_watt) AS avg_battery_w\n                   FROM sensor_power_live\n                   WHERE date(timestamp, 'localtime') = ?\n                     AND CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) = ?",
                (CURTAILMENT_FLOW_LOW_BATTERY_W, date, hour),
            )
            if row:
                if (row[0] or 0) > 0:
                    flow_result.update(
                        {
                            "samples": int(row[0] or 0),
                            "low_battery_samples": int(row[1] or 0),
                            "house_match_samples": 0,
                            "house_covered_samples": 0,
                            "cap_limit_samples": 0,
                            "avg_battery_w": float(row[2] or 0.0),
                            "export_limit_w": export_limit,
                            "cap_tolerance_w": cap_tolerance,
                        }
                    )
        except Exception:
            pass
        return flow_result if True else None

    @staticmethod
    def _effective_export_limit_w(
        max_grid_export_w: Optional[float], zero_export_mode: bool
    ) -> float:
        if zero_export_mode:
            return 0.0
        try:
            limit = float(max_grid_export_w) if max_grid_export_w is not None else 0.0
        except (TypeError, ValueError):
            limit = 0.0
        return max(0.0, limit)

    @staticmethod
    def _cap_tolerance_w(export_limit_w: float) -> float:
        if export_limit_w <= 0:
            return CURTAILMENT_CAP_TOLERANCE_W
        return max(
            CURTAILMENT_CAP_TOLERANCE_W,
            export_limit_w * CURTAILMENT_CAP_TOLERANCE_RATIO,
        )

    @staticmethod
    def normalize_battery_soc_percent(value: Any) -> Optional[float]:
        """Return SOC as 0-100 percent, or None when the reading is unusable."""
        if value is None:
            return
        try:
            soc_percent = float(value)
        except (TypeError, ValueError):
            return
        if soc_percent != soc_percent:
            return
        if 0.0 <= soc_percent <= 1.0:
            soc_percent *= 100.0
        if soc_percent < 0.0 or soc_percent > 100.0:
            return
        return soc_percent

    @staticmethod
    def resolved_battery_soc_percent(
        battery_soc: Optional[float] = None,
        flow_evidence: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Prefer the highest usable SOC from the live reading and hourly flow samples."""
        flows = flow_evidence or {}
        highest_soc = None
        for soc_reading in (
            battery_soc,
            flows.get("max_battery_soc"),
            flows.get("avg_battery_soc"),
        ):
            normalized_soc = MorningRoutineHandler.normalize_battery_soc_percent(
                soc_reading
            )
            if normalized_soc is not None and (
                highest_soc is None or normalized_soc > highest_soc
            ):
                highest_soc = normalized_soc
        return highest_soc

    @staticmethod
    def battery_soc_reached_full(
        battery_soc: Optional[float] = None,
        flow_evidence: Optional[Dict[str, Any]] = None,
        min_percent: float = CURTAILMENT_BATTERY_SOC_MIN,
    ) -> bool:
        """True only when a usable SOC reading reached the full-battery floor."""
        highest_soc = MorningRoutineHandler.resolved_battery_soc_percent(
            battery_soc, flow_evidence
        )
        return highest_soc is not None and highest_soc >= min_percent

    @staticmethod
    def shadow_curtailment_suspected(
        flow_evidence: Optional[Dict[str, Any]],
        *,
        battery_soc: Optional[float] = None,
        max_grid_export_w: Optional[float] = None,
        zero_export_mode: bool = False,
    ) -> bool:
        """Soft flow evidence for shadow detection only.

        A false positive drops one shadow observation. The learning quarantine
        keeps its stricter gates, because a false positive there drops a
        learning hour.
        """
        flows = flow_evidence or {}
        sample_count = int(flows.get("samples") or 0)
        if sample_count < CURTAILMENT_FLOW_MIN_SAMPLES:
            return False
        if not MorningRoutineHandler.battery_soc_reached_full(battery_soc, flows):
            return False
        export_limit = MorningRoutineHandler._effective_export_limit_w(
            (
                max_grid_export_w
                if max_grid_export_w is not None
                else flows.get("export_limit_w")
            ),
            zero_export_mode,
        )
        average_grid_export = float(flows.get("avg_solar_to_grid_w") or 0.0)
        average_battery_power = float(flows.get("avg_battery_w") or 0.0)
        average_house_power = float(flows.get("avg_house_w") or 0.0)
        return (
            average_grid_export <= MorningRoutineHandler._cap_tolerance_w(export_limit)
            and average_battery_power <= CURTAILMENT_FLOW_LOW_BATTERY_W
            and (average_house_power > CURTAILMENT_FLOW_MIN_HOUSE_W)
        )

    @staticmethod
    def evaluate_curtailment_state(
        *,
        actual_kwh: float,
        solar_to_battery_power: Optional[float],
        weather_actual: Optional[Dict[str, Any]],
        astronomy_data: Optional[Dict[str, Any]],
        flow_evidence: Optional[Dict[str, Any]] = None,
        max_grid_export_w: Optional[float] = None,
        zero_export_mode: bool = False,
        has_battery: bool = False,
        battery_soc: Optional[float] = None,
    ) -> Dict[str, Any]:
        result = {
            "hard": False,
            "quarantine": False,
            "transition": False,
            "reason": None,
        }
        if actual_kwh <= 0.01:
            return result
        theoretical_kwh = None
        if astronomy_data:
            theoretical_kwh = astronomy_data.get("theoretical_max_kwh")
        if not theoretical_kwh or theoretical_kwh <= 0:
            return result
        production_ratio = actual_kwh / theoretical_kwh
        flows = flow_evidence or {}
        sample_count = int(flows.get("samples") or 0)
        low_battery_samples = int(flows.get("low_battery_samples") or 0)
        house_match_samples = int(flows.get("house_match_samples") or 0)
        house_covered_samples = int(flows.get("house_covered_samples") or 0)
        cap_limit_samples = int(flows.get("cap_limit_samples") or 0)
        average_battery_power = float(flows.get("avg_battery_w") or 0.0)
        average_grid_export = float(flows.get("avg_solar_to_grid_w") or 0.0)
        export_limit = MorningRoutineHandler._effective_export_limit_w(
            (
                max_grid_export_w
                if max_grid_export_w is not None
                else flows.get("export_limit_w")
            ),
            zero_export_mode,
        )
        battery_full = bool(
            has_battery
        ) and MorningRoutineHandler.battery_soc_reached_full(battery_soc, flows)
        radiation = None
        cloud_cover = None
        if weather_actual:
            radiation = weather_actual.get("solar_radiation_wm2")
            cloud_cover = weather_actual.get("cloud_cover_percent")
        poor_weather = False
        if radiation is not None and radiation < CURTAILMENT_QUARANTINE_GHI_MIN:
            poor_weather = True
        if cloud_cover is not None and cloud_cover > CURTAILMENT_QUARANTINE_CLOUDS_MAX:
            poor_weather = True
        if sample_count > 0:
            low_battery_share = low_battery_samples / sample_count
            house_match_share = house_match_samples / sample_count
            house_covered_share = house_covered_samples / sample_count
            cap_limit_share = cap_limit_samples / sample_count
            cap_has_potential = (
                export_limit > 0
                and theoretical_kwh
                >= export_limit / 1000.0 * CURTAILMENT_CAP_POTENTIAL_MARGIN
            )
            flow_supported = (
                sample_count >= CURTAILMENT_FLOW_MIN_SAMPLES
                and low_battery_share >= CURTAILMENT_FLOW_HOUSE_MATCH_SHARE
                and (house_covered_share >= CURTAILMENT_FLOW_HOUSE_MATCH_SHARE)
            )
            hard_flow_supported = (
                sample_count >= CURTAILMENT_FLOW_HARD_MIN_SAMPLES
                and low_battery_share >= CURTAILMENT_FLOW_HARD_SHARE
                and (house_covered_share >= CURTAILMENT_FLOW_HARD_SHARE)
            )
            if (
                hard_flow_supported
                and cap_has_potential
                and (cap_limit_share >= CURTAILMENT_FLOW_HARD_SHARE)
                and (production_ratio < CURTAILMENT_CAP_RATIO_MAX)
            ):
                result.update(
                    {
                        "hard": True,
                        "quarantine": True,
                        "reason": THROTTLE_REASON_EXPORT_LIMIT,
                    }
                )
                return result
            if (
                flow_supported
                and cap_has_potential
                and (cap_limit_share >= CURTAILMENT_FLOW_TRANSITION_SHARE)
                and (production_ratio < CURTAILMENT_CAP_TRANSITION_RATIO_MAX)
            ):
                result.update(
                    {
                        "quarantine": True,
                        "transition": True,
                        "reason": THROTTLE_REASON_TRANSITION_CURTAILMENT,
                    }
                )
                return result
            if average_grid_export > MorningRoutineHandler._cap_tolerance_w(
                export_limit
            ):
                return result
            if (
                sample_count >= CURTAILMENT_FLOW_MIN_SAMPLES
                and house_match_share >= CURTAILMENT_FLOW_HOUSE_MATCH_SHARE
                and (production_ratio < CURTAILMENT_QUARANTINE_RATIO_MAX)
            ):
                result.update(
                    {
                        "quarantine": True,
                        "reason": THROTTLE_REASON_DEMAND_LIMITED_ZERO_EXPORT,
                    }
                )
                return result
            if poor_weather:
                return result
            if (
                battery_full
                and low_battery_share >= CURTAILMENT_FLOW_HOUSE_MATCH_SHARE
                and (average_battery_power <= CURTAILMENT_FLOW_LOW_BATTERY_W)
                and (production_ratio < MPPT_THROTTLE_PRODUCTION_RATIO)
            ):
                result.update(
                    {
                        "quarantine": True,
                        "reason": THROTTLE_REASON_SUSPECTED_BATTERY_CURTAILMENT,
                    }
                )
                return result
        if poor_weather:
            return result
        if production_ratio >= CURTAILMENT_QUARANTINE_RATIO_MAX:
            return result
        if (
            battery_full
            and solar_to_battery_power is not None
            and (solar_to_battery_power <= CURTAILMENT_FLOW_LOW_BATTERY_W)
            and (production_ratio < MPPT_THROTTLE_PRODUCTION_RATIO)
        ):
            result.update(
                {
                    "quarantine": True,
                    "reason": THROTTLE_REASON_SUSPECTED_BATTERY_CURTAILMENT,
                }
            )
            return result
        return result

    @staticmethod
    def evaluate_curtailment_learning_quarantine(
        *,
        actual_kwh: float,
        solar_to_battery_power: Optional[float],
        weather_actual: Optional[Dict[str, Any]],
        astronomy_data: Optional[Dict[str, Any]],
        flow_evidence: Optional[Dict[str, Any]] = None,
        max_grid_export_w: Optional[float] = None,
        zero_export_mode: bool = False,
        has_battery: bool = False,
        battery_soc: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Return learning-only curtailment evidence from weather, astronomy, and flow data."""
        curtailment_state = MorningRoutineHandler.evaluate_curtailment_state(
            actual_kwh=actual_kwh,
            solar_to_battery_power=solar_to_battery_power,
            weather_actual=weather_actual,
            astronomy_data=astronomy_data,
            flow_evidence=flow_evidence,
            max_grid_export_w=max_grid_export_w,
            zero_export_mode=zero_export_mode,
            has_battery=has_battery,
            battery_soc=battery_soc,
        )
        return (bool(curtailment_state["quarantine"]), curtailment_state["reason"])
