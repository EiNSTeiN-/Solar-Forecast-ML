# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Hourly Predictions Handler for Solar Forecast ML V16.2.0.
Manages hourly predictions using SQLite database.
All JSON operations replaced with database queries.

@zara
"""
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

_LOGGER = logging.getLogger(__name__)


class HourlyPredictionsHandler(DataManagerIO):
    """Manages hourly predictions with database backend. @zara

    Handles:
    - Creating daily predictions at 6:00 AM
    - Updating actual values hourly
    - Storing weather, astronomy, sensor data per prediction
    - Panel group predictions
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize hourly predictions handler. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for DB operations\n"
        super().__init__(hass, db_manager)
        self._shadow_detector = ShadowDetector(hass, db_manager)
        _LOGGER.debug("HourlyPredictionsHandler initialized with database backend")

    async def create_daily_predictions(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_forecast: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, bool],
        inverter_max_power: float = 0.0,
    ) -> bool:
        "Create all hourly predictions for a day. @zara\n\nCalled at 6:00 AM to create predictions for the day.\n\nArgs:\n    date: Date string YYYY-MM-DD\n    hourly_forecast: ML hourly predictions with production_kwh\n    weather_forecast: Weather service hourly forecast\n    astronomy_data: Sun position data\n    sensor_config: Which sensors are available\n    inverter_max_power: Max AC power in kW for capping\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            now = dt_util.now()
            created_count = 0
            async with self.db.transaction():
                await self.db.execute(
                    "DELETE FROM hourly_predictions WHERE target_date = ?",
                    (date,),
                    auto_commit=False,
                )
                date_dt = datetime.fromisoformat(date)
                for hour_forecast in hourly_forecast:
                    target_hour = hour_forecast.get("hour")
                    forecast_datetime = hour_forecast.get("datetime")
                    if target_hour is None or forecast_datetime is None:
                        _LOGGER.warning(
                            "Skipping hour with missing data: %s", hour_forecast
                        )
                        continue
                    hour_prediction_id = f"{date}_{target_hour:02d}"
                    uncapped_prediction_kwh = hour_forecast.get("production_kwh", 0.0)
                    if (
                        inverter_max_power > 0
                        and uncapped_prediction_kwh > inverter_max_power
                    ):
                        capped_prediction_kwh = inverter_max_power
                    else:
                        capped_prediction_kwh = uncapped_prediction_kwh
                    target_datetime = normalize_hourly_target_datetime(
                        forecast_datetime,
                        date,
                        target_hour,
                        local_timezone=dt_util.get_default_time_zone(),
                    ).isoformat()
                    await self.db.execute(
                        "INSERT OR REPLACE INTO hourly_predictions (\n                            prediction_id, prediction_created_at, prediction_created_hour,\n                            target_datetime, target_date, target_hour,\n                            target_day_of_week, target_day_of_year, target_month, target_season,\n                            prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                            ml_contribution_percent, model_version, confidence,\n                            is_production_hour, has_panel_group_predictions,\n                            prediction_confidence, weather_forecast_age_hours,\n                            sensor_data_quality, data_completeness_percent\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            hour_prediction_id,
                            now.isoformat(),
                            now.hour,
                            target_datetime,
                            date,
                            target_hour,
                            date_dt.weekday(),
                            date_dt.timetuple().tm_yday,
                            date_dt.month,
                            get_season(date_dt.month),
                            round(capped_prediction_kwh, 4),
                            (
                                round(uncapped_prediction_kwh, 4)
                                if inverter_max_power > 0
                                else None
                            ),
                            "blended",
                            0,
                            "1.0",
                            75.0,
                            hour_forecast.get("production_kwh", 0) > 0,
                            hour_forecast.get("panel_group_predictions") is not None,
                            self._get_confidence_level(75.0),
                            0,
                            "unknown",
                            100.0,
                        ),
                        auto_commit=False,
                    )
                    hour_weather = self._find_weather_for_hour(
                        weather_forecast, target_hour
                    )
                    if hour_weather:
                        await self._save_prediction_weather(
                            hour_prediction_id,
                            "forecast",
                            hour_weather,
                            auto_commit=False,
                        )
                    hour_astronomy = self._get_astronomy_for_hour(
                        astronomy_data, target_hour
                    )
                    if hour_astronomy:
                        await self._save_prediction_astronomy(
                            hour_prediction_id, hour_astronomy, auto_commit=False
                        )
                    groups = hour_forecast.get("groups")
                    if groups:
                        await self._save_panel_group_predictions(
                            hour_prediction_id, groups, auto_commit=False
                        )
                    else:
                        panel_group_predictions = hour_forecast.get(
                            "panel_group_predictions"
                        )
                        if panel_group_predictions:
                            await self._save_panel_group_predictions(
                                hour_prediction_id,
                                panel_group_predictions,
                                auto_commit=False,
                            )
                    created_count += 1
                    continue
            _LOGGER.info("Created %d hourly predictions for %s", created_count, date)
        except Exception as error:
            _LOGGER.error(
                "Failed to create daily predictions: %s", error, exc_info=True
            )
            return False
        return True

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
            unused_layout_slot = None
        try:
            await self.ensure_initialized()
            hour_prediction_id = f"{date}_{hour:02d}"
            measured_at = dt_util.now()
            row = await self.db.fetchone(
                "SELECT prediction_kwh FROM hourly_predictions WHERE prediction_id = ?",
                (hour_prediction_id,),
            )
            if not row:
                _LOGGER.warning("No prediction found for %s", hour_prediction_id)
                return False
            predicted_kwh = row[0]
            forecast_weather = await self._get_prediction_weather(
                hour_prediction_id, "forecast"
            )
            accuracy_percent, error_kwh, error_percent = self._calculate_accuracy(
                predicted_kwh, actual_kwh
            )
            is_clipped = False
            if inverter_max_power > 0:
                clipping_threshold_kwh = inverter_max_power * 0.95
                is_clipped = actual_kwh >= clipping_threshold_kwh
            mppt_throttled = False
            throttle_reason = None
            if zero_export_mode or has_battery:
                mppt_throttled, throttle_reason = self._detect_mppt_throttle(
                    actual_kwh,
                    has_battery,
                    solar_to_battery_power,
                    zero_export_mode=zero_export_mode,
                    weather_actual=weather_actual,
                    astronomy_data=astronomy_update,
                    battery_soc=battery_soc,
                )
            has_weather_alert, weather_alert_type = self._detect_weather_alert(
                weather_actual, forecast_weather
            )
            if (
                not has_weather_alert
                and production_miss_uplift(actual_kwh, predicted_kwh) is not None
            ):
                has_weather_alert = True
                weather_alert_type = WEATHER_ALERT_UNEXPECTED_SUN
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
            learning_weather_miss_class = weather_miss_class
            if (
                weather_miss_class == WEATHER_MISS_CLASS_FORECAST_MISS
                and weather_alert_type in LEARNABLE_WEATHER_MISS_ALERT_TYPES
            ):
                await self._promote_recent_weather_miss_sequence(
                    date, hour, weather_alert_type
                )
            prior_flags_row = await self.db.fetchone(
                "SELECT exclude_from_learning, mppt_throttle_reason\n                   FROM hourly_predictions WHERE prediction_id = ?",
                (hour_prediction_id,),
            )
            prior_learning_exclusion = (
                bool(prior_flags_row[0]) if prior_flags_row else False
            )
            prior_throttle_reason = prior_flags_row[1] if prior_flags_row else None
            exclude_from_learning = (
                is_clipped
                or mppt_throttled
                or throttle_reason_excludes_from_learning(throttle_reason)
                or throttle_reason_excludes_from_learning(prior_throttle_reason)
                or prior_learning_exclusion
                or (
                    has_weather_alert
                    and weather_alert_excludes_from_learning(
                        weather_alert_type, learning_weather_miss_class
                    )
                )
            )
            throttle_reason = throttle_reason or prior_throttle_reason
            exclude_from_clean_evaluation = exclude_from_learning or (
                has_weather_alert
                and weather_alert_excludes_from_clean_evaluation(weather_alert_type)
            )
            await self.db.execute(
                "UPDATE hourly_predictions SET\n                    actual_kwh = ?,\n                    actual_measured_at = ?,\n                    accuracy_percent = ?,\n                    error_kwh = ?,\n                    error_percent = ?,\n                    inverter_clipped = ?,\n                    has_sensor_data = TRUE,\n                    sensor_data_complete = ?,\n                    exclude_from_learning = ?,\n                    exclude_from_clean_evaluation = ?,\n                    mppt_throttled = ?,\n                    mppt_throttle_reason = ?,\n                    has_weather_alert = ?,\n                    weather_alert_type = ?,\n                    weather_miss_class = ?,\n                    reforecast_trigger_eligible = ?\n                WHERE prediction_id = ?",
                (
                    round(actual_kwh, 4),
                    measured_at.isoformat(),
                    accuracy_percent,
                    error_kwh,
                    error_percent,
                    is_clipped,
                    self._check_sensor_completeness(sensor_data),
                    exclude_from_learning,
                    exclude_from_clean_evaluation,
                    mppt_throttled,
                    throttle_reason,
                    has_weather_alert,
                    weather_alert_type,
                    weather_miss_class,
                    reforecast_eligible,
                    hour_prediction_id,
                ),
            )
            await self._save_sensor_actual(hour_prediction_id, sensor_data)
            if weather_actual:
                await self._save_prediction_weather(
                    hour_prediction_id, "actual", weather_actual
                )
            if astronomy_update:
                await self._update_prediction_astronomy(
                    hour_prediction_id, astronomy_update
                )
            configured_group_count = len(
                getattr(self.coordinator, "panel_groups", []) or []
            )
            complete_group_actuals = False
            if panel_group_actuals:
                for (
                    provided_group_name,
                    provided_group_actual_kwh,
                ) in panel_group_actuals.items():
                    await self.db.execute(
                        "UPDATE prediction_panel_groups\n                           SET actual_kwh = ?\n                           WHERE prediction_id = ? AND group_name = ?",
                        (
                            round(provided_group_actual_kwh, 4),
                            hour_prediction_id,
                            provided_group_name,
                        ),
                    )
                    group_prediction_row = await self.db.fetchone(
                        "SELECT prediction_kwh, actual_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (hour_prediction_id, provided_group_name),
                    )
                    if group_prediction_row:
                        if group_prediction_row[1] is not None:
                            group_predicted_kwh = group_prediction_row[0]
                            group_actual_kwh = group_prediction_row[1]
                            (
                                group_accuracy_percent,
                                group_error_kwh,
                                group_error_percent,
                            ) = self._calculate_accuracy(
                                group_predicted_kwh, group_actual_kwh
                            )
                            group_accuracy = {
                                "prediction_kwh": group_predicted_kwh,
                                "actual_kwh": group_actual_kwh,
                                "error_kwh": group_error_kwh,
                                "error_percent": group_error_percent,
                                "accuracy_percent": group_accuracy_percent,
                            }
                            await self.db.save_hourly_panel_group_accuracy(
                                hour_prediction_id, provided_group_name, group_accuracy
                            )
                group_counts_row = await self.db.fetchone(
                    "SELECT COUNT(*),\n                              SUM(CASE WHEN actual_kwh IS NOT NULL THEN 1 ELSE 0 END)\n                       FROM prediction_panel_groups\n                       WHERE prediction_id = ?",
                    (hour_prediction_id,),
                )
                prediction_group_count = (
                    int(group_counts_row[0] or 0) if group_counts_row else 0
                )
                measured_group_count = (
                    int(group_counts_row[1] or 0) if group_counts_row else 0
                )
                stored_group_rows = await self.db.fetchall(
                    "SELECT group_name FROM prediction_panel_groups\n                       WHERE prediction_id = ?",
                    (hour_prediction_id,),
                )
                stored_group_names = {row[0] for row in stored_group_rows}
                provided_group_names = set(panel_group_actuals)
                complete_group_actuals = (
                    configured_group_count > 0
                    and prediction_group_count == configured_group_count
                    and (measured_group_count == configured_group_count)
                    and (len(panel_group_actuals) == configured_group_count)
                    and (stored_group_names == provided_group_names)
                )
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET has_panel_group_actuals = ?\n                       WHERE prediction_id = ?",
                    (complete_group_actuals, hour_prediction_id),
                )
            astronomy = await self._get_prediction_astronomy(hour_prediction_id)
            shadow_sample = {
                "prediction_id": hour_prediction_id,
                "actual_kwh": actual_kwh,
                "astronomy": astronomy,
                "sensor_actual": sensor_data,
                "weather_actual": weather_actual,
                "weather_forecast": forecast_weather,
                "target_hour": hour,
                "target_datetime": f"{date}T{hour:02d}:00:00",
                "flags": {
                    "inverter_clipped": is_clipped,
                    "mppt_throttled": mppt_throttled,
                    "partial_panel_group_actuals": bool(panel_group_actuals)
                    and (not complete_group_actuals),
                },
            }
            ensemble_shadow = await self._shadow_detector.detect_shadow_ensemble(
                shadow_sample
            )
            await self.db.save_hourly_shadow_detection(
                hour_prediction_id, ensemble_shadow
            )
            coordinator = getattr(self, "coordinator", None)
            if coordinator is not None and coordinator.update_shadow_detection_cache(
                hour_prediction_id, ensemble_shadow
            ):
                coordinator.async_update_listeners()
            if complete_group_actuals and panel_group_actuals:
                group_shadow_inputs = {}
                for group_name in panel_group_actuals:
                    group_physics_row = await self.db.fetchone(
                        "SELECT actual_kwh, physics_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (hour_prediction_id, group_name),
                    )
                    if (
                        not group_physics_row
                        or group_physics_row[0] is None
                        or group_physics_row[1] is None
                        or (group_physics_row[1] <= 0)
                    ):
                        group_shadow_inputs = {}
                        break
                    group_shadow_inputs[group_name] = {
                        "actual_kwh": group_physics_row[0],
                        "theoretical_max_kwh": group_physics_row[1],
                    }
                    clear_sky_row = await self.db.fetchone(
                        "SELECT theoretical_kwh\n                             FROM astronomy_cache_panel_groups\n                            WHERE cache_date = ? AND hour = ? AND group_name = ?",
                        (date, hour, group_name),
                    )
                    if clear_sky_row:
                        if clear_sky_row[0] is not None:
                            group_shadow_inputs[group_name]["clear_sky_kwh"] = (
                                clear_sky_row[0]
                            )
                group_shadows = {}
                if len(group_shadow_inputs) == configured_group_count:
                    group_shadows = await self._shadow_detector.detect_shadow_per_group(
                        shadow_sample,
                        group_shadow_inputs,
                        system_ensemble=ensemble_shadow,
                    )
                snow_per_group = (
                    weather_actual.get("snow_per_group", {}) if weather_actual else {}
                )
                if not snow_per_group:
                    try:
                        snow_per_group = await self.db.load_snow_tracking_groups()
                    except Exception:
                        snow_per_group = {}
                group_exclusion_sample = {
                    "flags": {
                        "inverter_clipped": is_clipped,
                        "mppt_throttled": mppt_throttled,
                        "mppt_throttle_reason": throttle_reason,
                        "has_weather_alert": has_weather_alert,
                        "weather_alert_type": weather_alert_type,
                        "weather_miss_class": weather_miss_class,
                    }
                }
                for group_name in panel_group_actuals:
                    if panel_group_exclusion_reason:
                        exclude_group = True
                        group_exclusion_reason = panel_group_exclusion_reason
                    else:
                        exclude_group, group_exclusion_reason = (
                            should_exclude_group_from_learning(
                                group_exclusion_sample,
                                group_name,
                                snow_per_group,
                                group_shadows,
                            )
                        )
                    snow_covered_group = snow_per_group.get(group_name, {}).get(
                        "snow_covered", False
                    )
                    shadow_type_group = group_shadows.get(group_name, {}).get(
                        "shadow_type"
                    )
                    await self.db.update_prediction_panel_group_flags(
                        hour_prediction_id,
                        group_name,
                        {
                            "exclude_from_learning_group": exclude_group,
                            "exclusion_reason_group": (
                                group_exclusion_reason if exclude_group else None
                            ),
                            "snow_covered_group": snow_covered_group,
                            "shadow_type_group": shadow_type_group,
                        },
                    )
            production_metrics = await self._calculate_production_metrics(date)
            await self.db.save_hourly_production_metrics(
                hour_prediction_id, production_metrics
            )
            historical_context = await self._calculate_historical_context(date, hour)
            await self.db.save_hourly_historical_context(
                hour_prediction_id, historical_context
            )
            _LOGGER.debug(
                "Updated actual for %s: %.3f kWh, accuracy: %.1f%%",
                hour_prediction_id,
                actual_kwh,
                accuracy_percent or 0,
            )
            return True
        except Exception as error:
            _LOGGER.error("Failed to update hourly actual: %s", error, exc_info=True)
            return False

    async def get_prediction_by_id(
        self, prediction_id: str
    ) -> Optional[Dict[str, Any]]:
        "Get specific prediction by ID. @zara\n\nArgs:\n    prediction_id: ID in format YYYY-MM-DD_HH\n\nReturns:\n    Prediction dict or None if not found\n"
        try:
            await self.ensure_initialized()
            row = await self.db.fetchone(
                "SELECT * FROM hourly_predictions WHERE prediction_id = ?",
                (prediction_id,),
            )
            if not row:
                return
            prediction = dict(row)
            prediction["weather_forecast"] = await self._get_prediction_weather(
                prediction_id, "forecast"
            )
            prediction["weather_actual"] = await self._get_prediction_weather(
                prediction_id, "actual"
            )
            prediction["astronomy"] = await self._get_prediction_astronomy(
                prediction_id
            )
            prediction["sensor_actual"] = await self._get_sensor_actual(prediction_id)
            prediction["panel_group_predictions"] = await self._get_panel_groups(
                prediction_id
            )
            return prediction
        except Exception as error:
            _LOGGER.error("Failed to get prediction %s: %s", prediction_id, error)
            return

    async def get_predictions_for_date(self, date: str) -> List[Dict[str, Any]]:
        "Get all predictions for a specific date. @zara\n\nArgs:\n    date: Date string YYYY-MM-DD\n\nReturns:\n    List of prediction dicts\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT * FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (date,),
            )
            predictions = []
            for row in rows:
                prediction = dict(row)
                row_prediction_id = prediction["prediction_id"]
                prediction["weather_forecast"] = await self._get_prediction_weather(
                    row_prediction_id, "forecast"
                )
                prediction["astronomy"] = await self._get_prediction_astronomy(
                    row_prediction_id
                )
                prediction["panel_group_predictions"] = await self._get_panel_groups(
                    row_prediction_id
                )
                predictions.append(prediction)
            return predictions
        except Exception as error:
            _LOGGER.error("Failed to get predictions for %s: %s", date, error)
            return []

    async def get_best_hour_today(self) -> Optional[Dict[str, Any]]:
        "Get prediction with highest production for today. @zara\n\nReturns:\n    Prediction dict for best hour or None\n"
        today = dt_util.now().date().isoformat()
        predictions = await self.get_predictions_for_date(today)
        if not predictions:
            return
        return max(predictions, key=lambda x: x.get("prediction_kwh", 0))

    async def get_best_hour_string(self) -> Optional[str]:
        "Get best hour as string (e.g. '11:00'). @zara\n\nReturns:\n    Hour string or None\n"
        best_prediction = await self.get_best_hour_today()
        if best_prediction and best_prediction.get("target_hour") is not None:
            return f"{best_prediction['target_hour']:02d}:00"
        return None if True else None

    async def get_predictions_with_actuals(
        self,
        days: int = 30,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        "Get predictions that have actual values for ML training. @zara\n\nArgs:\n    days: Number of days to look back (if no dates specified)\n    start_date: Optional start date filter\n    end_date: Optional end date filter\n\nReturns:\n    List of predictions with actual values\n"
        try:
            await self.ensure_initialized()
            if start_date and end_date:
                rows = await self.db.fetchall(
                    "SELECT * FROM hourly_predictions\n                       WHERE actual_kwh IS NOT NULL\n                       AND target_date BETWEEN ? AND ?\n                       ORDER BY target_datetime",
                    (start_date, end_date),
                )
            else:
                cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
                rows = await self.db.fetchall(
                    "SELECT * FROM hourly_predictions\n                       WHERE actual_kwh IS NOT NULL\n                       AND target_date >= ?\n                       ORDER BY target_datetime",
                    (cutoff_date,),
                )
            return [dict(row) for row in rows]
            pass
        except Exception as error:
            _LOGGER.error("Failed to get predictions with actuals: %s", error)
            return []

    async def get_predictions_count(self) -> int:
        "Get total number of predictions in database. @zara\n\nReturns:\n    Count of predictions\n"
        try:
            await self.ensure_initialized()
            count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions"
            )
            return count_row[0] if count_row else 0
        except Exception as error:
            _LOGGER.error("Failed to get predictions count: %s", error)
            return 0

    async def cleanup_old_predictions(self, days_to_keep: int = 365) -> int:
        "Remove predictions older than specified days. @zara\n\nArgs:\n    days_to_keep: Number of days of data to retain\n\nReturns:\n    Number of deleted records\n"
        try:
            await self.ensure_initialized()
            cutoff_date = (
                (dt_util.now() - timedelta(days=days_to_keep)).date().isoformat()
            )
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE target_date < ?",
                (cutoff_date,),
            )
            deleted_count = row[0] if row else 0
            if deleted_count > 0:
                await self.db.execute(
                    "DELETE FROM hourly_predictions WHERE target_date < ?",
                    (cutoff_date,),
                )
                _LOGGER.info("Cleaned up %d old predictions", deleted_count)
            return deleted_count
        except Exception as error:
            _LOGGER.error("Failed to cleanup old predictions: %s", error)
            return 0

    async def _save_prediction_weather(
        self,
        prediction_id: str,
        weather_type: str,
        weather: Dict[str, Any],
        auto_commit: bool = True,
    ) -> None:
        """Save weather data for a prediction. @zara"""
        await self.db.execute(
            "INSERT INTO prediction_weather (\n                prediction_id, weather_type,\n                temperature, solar_radiation_wm2, wind, humidity,\n                rain, clouds, pressure, source,\n                lux, frost_detected, frost_score, frost_confidence,\n                diffuse_radiation, direct_radiation\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                temperature = excluded.temperature,\n                solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                wind = excluded.wind,\n                humidity = excluded.humidity,\n                rain = excluded.rain,\n                clouds = excluded.clouds,\n                pressure = excluded.pressure,\n                source = excluded.source,\n                lux = excluded.lux,\n                frost_detected = excluded.frost_detected,\n                frost_score = excluded.frost_score,\n                frost_confidence = excluded.frost_confidence,\n                diffuse_radiation = excluded.diffuse_radiation,\n                direct_radiation = excluded.direct_radiation",
            (
                prediction_id,
                weather_type,
                weather.get("temperature") or weather.get("temperature_c"),
                weather.get("solar_radiation_wm2"),
                weather.get("wind")
                or weather.get("wind_speed")
                or weather.get("wind_speed_ms"),
                weather.get("humidity") or weather.get("humidity_percent"),
                weather.get("rain")
                or weather.get("precipitation")
                or weather.get("precipitation_mm"),
                weather.get("clouds")
                or weather.get("cloud_cover_percent")
                or weather.get("cloud_cover"),
                weather.get("pressure") or weather.get("pressure_hpa"),
                weather.get("source", "unknown"),
                weather.get("lux"),
                weather.get("frost_detected"),
                weather.get("frost_score"),
                weather.get("frost_confidence"),
                weather.get("diffuse_radiation") or weather.get("dhi"),
                weather.get("direct_radiation") or weather.get("dni"),
            ),
            auto_commit=auto_commit,
        )

    async def _get_prediction_weather(
        self, prediction_id: str, weather_type: str
    ) -> Optional[Dict[str, Any]]:
        """Get weather data for a prediction. @zara"""
        row = await self.db.fetchone(
            "SELECT temperature, solar_radiation_wm2, wind, humidity,\n                      rain, clouds, pressure, source, lux,\n                      frost_detected, frost_score, frost_confidence\n               FROM prediction_weather\n               WHERE prediction_id = ? AND weather_type = ?",
            (prediction_id, weather_type),
        )
        if not row:
            return
        return {
            "temperature": row[0],
            "solar_radiation_wm2": row[1],
            "wind": row[2],
            "humidity": row[3],
            "rain": row[4],
            "clouds": row[5],
            "pressure": row[6],
            "source": row[7],
            "lux": row[8],
            "frost_detected": row[9],
            "frost_score": row[10],
            "frost_confidence": row[11],
        }

    async def _save_prediction_astronomy(
        self, prediction_id: str, astro: Dict[str, Any], auto_commit: bool = True
    ) -> None:
        """Save astronomy data for a prediction. @zara"""
        await self.db.execute(
            "INSERT INTO prediction_astronomy (\n                prediction_id, sunrise, sunset, solar_noon, daylight_hours,\n                sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2,\n                theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio,\n                hours_after_sunrise, hours_before_sunset\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(prediction_id) DO UPDATE SET\n                sunrise = excluded.sunrise,\n                sunset = excluded.sunset,\n                solar_noon = excluded.solar_noon,\n                daylight_hours = excluded.daylight_hours,\n                sun_elevation_deg = excluded.sun_elevation_deg,\n                sun_azimuth_deg = excluded.sun_azimuth_deg,\n                clear_sky_radiation_wm2 = excluded.clear_sky_radiation_wm2,\n                theoretical_max_kwh = excluded.theoretical_max_kwh,\n                hours_since_solar_noon = excluded.hours_since_solar_noon,\n                day_progress_ratio = excluded.day_progress_ratio,\n                hours_after_sunrise = excluded.hours_after_sunrise,\n                hours_before_sunset = excluded.hours_before_sunset",
            (
                prediction_id,
                astro.get("sunrise"),
                astro.get("sunset"),
                astro.get("solar_noon"),
                astro.get("daylight_hours"),
                astro.get("sun_elevation_deg"),
                astro.get("sun_azimuth_deg"),
                astro.get("clear_sky_radiation_wm2"),
                astro.get("theoretical_max_kwh"),
                astro.get("hours_since_solar_noon"),
                astro.get("day_progress_ratio"),
                astro.get("hours_after_sunrise"),
                astro.get("hours_before_sunset"),
            ),
            auto_commit=auto_commit,
        )

    async def _update_prediction_astronomy(
        self, prediction_id: str, updates: Dict[str, Any]
    ) -> None:
        """Update astronomy data for a prediction. @zara"""
        assignments = []
        params = []
        for field, value in updates.items():
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if assignments:
            params.append(prediction_id)
            await self.db.execute(
                f"UPDATE prediction_astronomy\n                    SET {', '.join(assignments)}\n                    WHERE prediction_id = ?",
                tuple(params),
            )
        return None if True else None

    async def _get_prediction_astronomy(
        self, prediction_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get astronomy data for a prediction. @zara"""
        row = await self.db.fetchone(
            "SELECT sunrise, sunset, solar_noon, daylight_hours,\n                      sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2,\n                      theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio,\n                      hours_after_sunrise, hours_before_sunset\n               FROM prediction_astronomy WHERE prediction_id = ?",
            (prediction_id,),
        )
        if not row:
            return
        return {
            "sunrise": row[0],
            "sunset": row[1],
            "solar_noon": row[2],
            "daylight_hours": row[3],
            "sun_elevation_deg": row[4],
            "sun_azimuth_deg": row[5],
            "clear_sky_radiation_wm2": row[6],
            "theoretical_max_kwh": row[7],
            "hours_since_solar_noon": row[8],
            "day_progress_ratio": row[9],
            "hours_after_sunrise": row[10],
            "hours_before_sunset": row[11],
        }

    async def _save_sensor_actual(
        self, prediction_id: str, sensor_data: Dict[str, Any]
    ) -> None:
        """Save sensor actual data for a prediction. @zara"""
        await self.db.execute(
            "INSERT INTO prediction_sensor_actual (\n                prediction_id, temperature_c, humidity_percent,\n                solar_radiation_wm2, rain_mm, uv_index, wind_speed_ms,\n                lux\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(prediction_id) DO UPDATE SET\n                temperature_c = excluded.temperature_c,\n                humidity_percent = excluded.humidity_percent,\n                solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                rain_mm = excluded.rain_mm,\n                uv_index = excluded.uv_index,\n                wind_speed_ms = excluded.wind_speed_ms,\n                lux = excluded.lux",
            (
                prediction_id,
                sensor_data.get("temperature_c"),
                sensor_data.get("humidity_percent"),
                sensor_data.get("solar_radiation_wm2"),
                sensor_data.get("rain_mm"),
                sensor_data.get("uv_index"),
                sensor_data.get("wind_speed_ms"),
                sensor_data.get("lux"),
            ),
        )

    async def _get_sensor_actual(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """Get sensor actual data for a prediction. @zara"""
        row = await self.db.fetchone(
            "SELECT temperature_c, humidity_percent, solar_radiation_wm2,\n                      rain_mm, uv_index, wind_speed_ms, current_yield_kwh, lux\n               FROM prediction_sensor_actual WHERE prediction_id = ?",
            (prediction_id,),
        )
        if not row:
            return
        return {
            "temperature_c": row[0],
            "humidity_percent": row[1],
            "solar_radiation_wm2": row[2],
            "rain_mm": row[3],
            "uv_index": row[4],
            "wind_speed_ms": row[5],
            "current_yield_kwh": row[6],
            "lux": row[7],
        }

    async def _save_panel_group_predictions(
        self, prediction_id: str, panel_groups: Any, auto_commit: bool = True
    ) -> None:
        """Save panel group predictions. @zara"""
        if isinstance(panel_groups, dict):
            group_predictions = [
                {"name": dict_group_name, "power_kwh": group_power_kwh}
                for dict_group_name, group_power_kwh in panel_groups.items()
            ]
        else:
            group_predictions = list(panel_groups or [])
        for group_prediction in group_predictions:
            group_name = group_prediction.get("name")
            if not group_name:
                continue
            prediction_kwh = group_prediction.get(
                "power_kwh", group_prediction.get("prediction_kwh", 0.0)
            )
            raw_physics_kwh = group_prediction.get("raw_physics_kwh")
            topology_meta = await self.db.get_active_panel_group_topology_meta(
                group_name
            )
            await self.db.execute(
                "INSERT INTO prediction_panel_groups (\n                    prediction_id, group_name, prediction_kwh, raw_physics_kwh,\n                    config_epoch_id, group_uid, group_lineage_uid, power_wp_at_prediction\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                    prediction_kwh = excluded.prediction_kwh,\n                    raw_physics_kwh = COALESCE(\n                        excluded.raw_physics_kwh, prediction_panel_groups.raw_physics_kwh\n                    ),\n                    config_epoch_id = excluded.config_epoch_id,\n                    group_uid = excluded.group_uid,\n                    group_lineage_uid = excluded.group_lineage_uid,\n                    power_wp_at_prediction = excluded.power_wp_at_prediction",
                (
                    prediction_id,
                    group_name,
                    round(float(prediction_kwh or 0.0), 4),
                    (
                        round(float(raw_physics_kwh), 4)
                        if raw_physics_kwh is not None
                        else None
                    ),
                    topology_meta.get("config_epoch_id"),
                    topology_meta.get("group_uid"),
                    topology_meta.get("group_lineage_uid"),
                    topology_meta.get("power_wp_at_prediction"),
                ),
                auto_commit=auto_commit,
            )
            continue

    async def _get_panel_groups(self, prediction_id: str) -> Optional[Dict[str, float]]:
        """Get panel group predictions for a prediction. @zara"""
        rows = await self.db.fetchall(
            "SELECT group_name, prediction_kwh\n               FROM prediction_panel_groups WHERE prediction_id = ?",
            (prediction_id,),
        )
        if not rows:
            return
        return {row[0]: row[1] for row in rows}

    def _find_weather_for_hour(self, weather_forecast: List[Dict], hour: int) -> Dict:
        """Find weather data for specific hour. @zara"""
        for hour_weather in weather_forecast:
            if (
                hour_weather.get("local_hour") == hour
                or hour_weather.get("hour") == hour
            ):
                return hour_weather
        return {}

    def _get_astronomy_for_hour(self, astro_data: Dict, hour: int) -> Dict:
        """Get astronomy data for specific hour with enrichment. @zara V16.0.0"""
        hour_astronomy = astro_data.get("hourly", {}).get(str(hour), {})
        astronomy = {
            "sunrise": astro_data.get("sunrise"),
            "sunset": astro_data.get("sunset"),
            "solar_noon": astro_data.get("solar_noon"),
            "daylight_hours": astro_data.get("daylight_hours"),
            "sun_elevation_deg": hour_astronomy.get("elevation_deg"),
            "sun_azimuth_deg": hour_astronomy.get("azimuth_deg"),
            "clear_sky_radiation_wm2": hour_astronomy.get(
                "clear_sky_solar_radiation_wm2"
            ),
            "theoretical_max_kwh": hour_astronomy.get("theoretical_max_pv_kwh"),
            "hours_since_solar_noon": hour_astronomy.get("hours_since_solar_noon"),
            "day_progress_ratio": hour_astronomy.get("day_progress_ratio"),
        }
        sunrise_value = astro_data.get("sunrise")
        sunset_value = astro_data.get("sunset")
        if sunrise_value and sunset_value:
            try:
                from datetime import datetime as datetime_type

                def _parse_t(s):
                    s = str(s).strip()
                    for date_format in (
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S",
                        "%H:%M:%S",
                        "%H:%M",
                    ):
                        try:
                            pass
                            return datetime_type.strptime(s, date_format)
                        except (ValueError, TypeError):
                            continue

                sunrise_dt = _parse_t(sunrise_value)
                sunset_dt = _parse_t(sunset_value)
                if sunrise_dt and sunset_dt:
                    sunrise_hour = sunrise_dt.hour + sunrise_dt.minute / 60.0
                    sunset_hour = sunset_dt.hour + sunset_dt.minute / 60.0
                    astronomy["hours_after_sunrise"] = max(
                        0.0, hour + 0.5 - sunrise_hour
                    )
                    astronomy["hours_before_sunset"] = max(
                        0.0, sunset_hour - (hour + 0.5)
                    )
            except Exception:
                pass
        return astronomy if True else None

    def _get_confidence_level(self, confidence: float) -> str:
        """Convert confidence percentage to level. @zara"""
        if confidence >= 80:
            return "high"
        if confidence >= 60:
            return "medium"
        return "low"

    def _check_sensor_completeness(self, sensor_data: Dict) -> bool:
        """Check if all configured sensors have data. @zara"""
        present_sensor_count = sum(
            (1 for sensor_value in sensor_data.values() if sensor_value is not None)
        )
        return present_sensor_count >= 3

    def _calculate_accuracy(
        self, predicted: float, actual: float
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        "Calculate accuracy metrics. @zara\n\nReturns:\n    Tuple of (accuracy_percent, error_kwh, error_percent)\n"
        if predicted > 0:
            error_kwh = round(actual - predicted, 3)
            error_percent = round((actual - predicted) / predicted * 100, 1)
            accuracy_percent = max(0.0, 100.0 - abs(error_percent))
            return (round(accuracy_percent, 1), error_kwh, error_percent)
        if actual < 0.001:
            return (100.0, 0.0, 0.0)
        return (0.0, round(actual, 3), None)

    def _detect_weather_alert(
        self,
        weather_actual: Optional[Dict[str, Any]],
        weather_forecast: Optional[Dict[str, Any]],
    ) -> Tuple[bool, Optional[str]]:
        'Detect unexpected weather conditions. @zara\n\nReturns:\n    Tuple of (has_alert, alert_type)\n\nAlert types:\n- "unexpected_rain": actual rain > 0.5mm, forecast < 0.1mm\n- "unexpected_snow": temp < 2°C AND rain > 0.5mm, forecast < 0.1mm\n- "unexpected_clouds": actual radiation < 40% forecast (when forecast > 100)\n- "unexpected_sun": actual kWh far above the published hour forecast\n- "snow_covered_panels": snow coverage detected\n'
        if not weather_actual:
            return (False, None)
        if weather_actual.get("snow_covered_panels"):
            return (True, "snow_covered_panels")
        if not weather_forecast:
            return (False, None)
        actual_rain_mm = (
            weather_actual.get("rain") or weather_actual.get("precipitation_mm") or 0
        )
        forecast_rain_mm = (
            weather_forecast.get("rain")
            or weather_forecast.get("precipitation_mm")
            or 0
        )
        actual_temperature_c = weather_actual.get("temperature") or weather_actual.get(
            "temperature_c"
        )
        actual_radiation_wm2 = weather_actual.get("solar_radiation_wm2")
        forecast_radiation_wm2 = weather_forecast.get("solar_radiation_wm2")
        if actual_rain_mm > 0.5 and forecast_rain_mm < 0.1:
            if actual_temperature_c is not None and actual_temperature_c < 2.0:
                return (True, "unexpected_snow")
            return (True, "unexpected_rain")
        if (
            actual_radiation_wm2 is not None
            and forecast_radiation_wm2 is not None
            and (forecast_radiation_wm2 > 100)
            and (actual_radiation_wm2 < forecast_radiation_wm2 * 0.4)
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
        rows = await self.db.fetchall(
            "SELECT target_hour, weather_alert_type\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour < ?\n                 AND target_hour >= ?\n                 AND has_weather_alert = TRUE\n               ORDER BY target_hour DESC",
            (date, hour, max(0, hour - max_lookback_hours)),
        )
        consecutive_miss_count = 0
        expected_hour = hour - 1
        for row in rows:
            row_hour = int(row["target_hour"])
            if row_hour != expected_hour:
                break
            if row["weather_alert_type"] != alert_type:
                break
            consecutive_miss_count += 1
            expected_hour -= 1
        return consecutive_miss_count if True else None

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
        actual_rain_mm = (
            weather_actual.get("rain") or weather_actual.get("precipitation_mm") or 0
        )
        forecast_rain_mm = (
            weather_forecast.get("rain")
            or weather_forecast.get("precipitation_mm")
            or 0
        )
        if alert_type in ("unexpected_rain", "unexpected_snow"):
            return actual_rain_mm > 0.5 and forecast_rain_mm < 0.1
        if alert_type == WEATHER_ALERT_UNEXPECTED_SUN:
            return True
        actual_radiation_wm2 = weather_actual.get("solar_radiation_wm2")
        forecast_radiation_wm2 = weather_forecast.get("solar_radiation_wm2")
        if (
            alert_type == "unexpected_clouds"
            and actual_radiation_wm2 is not None
            and (forecast_radiation_wm2 is not None)
            and (forecast_radiation_wm2 > 100)
        ):
            return actual_radiation_wm2 < forecast_radiation_wm2 * 0.4
        return False

    def _detect_mppt_throttle(
        self,
        actual_kwh: float,
        has_battery: bool,
        solar_to_battery_power: Optional[float],
        zero_export_mode: bool = False,
        weather_actual: Optional[Dict[str, Any]] = None,
        astronomy_data: Optional[Dict[str, Any]] = None,
        battery_soc: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Detect MPPT throttle conditions with weather and astronomy validation. @zara"""
        if actual_kwh <= 0.01 or (not has_battery and (not zero_export_mode)):
            return (False, None)
        soc_percent = None
        if battery_soc is not None:
            try:
                soc_percent = float(battery_soc)
            except (TypeError, ValueError):
                soc_percent = None
            else:
                if 0.0 <= soc_percent <= 1.0:
                    soc_percent *= 100.0
                if soc_percent < 0.0 or soc_percent > 100.0:
                    soc_percent = None
        full_battery_curtailment = (
            has_battery
            and solar_to_battery_power is not None
            and (solar_to_battery_power < MPPT_THROTTLE_BATTERY_POWER_THRESHOLD)
            and (soc_percent is not None)
            and (soc_percent >= CURTAILMENT_BATTERY_SOC_MIN)
        )
        if weather_actual:
            cloud_cover_percent = weather_actual.get("cloud_cover_percent")
            ghi_wm2 = weather_actual.get("solar_radiation_wm2")
            if (
                cloud_cover_percent is not None
                and cloud_cover_percent > MPPT_CLEAR_SKY_CLOUDS_MAX
            ):
                return (False, None)
            if ghi_wm2 is not None and ghi_wm2 < MPPT_CLEAR_SKY_GHI_THRESHOLD:
                return (False, None)
        theoretical_max_kwh = None
        if astronomy_data:
            theoretical_max_kwh = astronomy_data.get("theoretical_max_kwh")
        if not theoretical_max_kwh or theoretical_max_kwh <= 0:
            return (False, None)
        production_ratio = actual_kwh / theoretical_max_kwh
        if production_ratio >= MPPT_THROTTLE_PRODUCTION_RATIO:
            return (False, None)
        if actual_kwh < theoretical_max_kwh * 0.15:
            return (False, None)
        if full_battery_curtailment:
            return (True, THROTTLE_REASON_FULL_BATTERY)
        if zero_export_mode and (not has_battery):
            return (True, THROTTLE_REASON_ZERO_EXPORT)
        return (False, None)

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

    async def _calculate_production_metrics(self, target_date: str) -> Dict[str, Any]:
        "Calculate production metrics for a specific date. @zara\n\nAggregates actual production data for the day:\n- peak_power_today_kwh: Maximum production hour\n- production_hours_today: Count of hours with production\n- cumulative_today_kwh: Total production for the day\n\nArgs:\n    target_date: Date string YYYY-MM-DD\n\nReturns:\n    Dict with production metrics\n"
        try:
            peak_row = await self.db.fetchone(
                "SELECT MAX(actual_kwh) FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh IS NOT NULL",
                (target_date,),
            )
            peak_kwh = peak_row[0] if peak_row and peak_row[0] else 0.0
            production_hours_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh > 0",
                (target_date,),
            )
            production_hours = production_hours_row[0] if production_hours_row else 0
            cumulative_row = await self.db.fetchone(
                "SELECT SUM(actual_kwh) FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh IS NOT NULL",
                (target_date,),
            )
            cumulative_kwh = (
                cumulative_row[0] if cumulative_row and cumulative_row[0] else 0.0
            )
            return {
                "peak_power_today_kwh": round(peak_kwh, 4),
                "production_hours_today": production_hours,
                "cumulative_today_kwh": round(cumulative_kwh, 4),
            }
        except Exception as error:
            _LOGGER.error("Failed to calculate production metrics: %s", error)
            return {
                "peak_power_today_kwh": 0.0,
                "production_hours_today": 0,
                "cumulative_today_kwh": 0.0,
            }

    async def _calculate_historical_context(
        self, target_date: str, target_hour: int
    ) -> Dict[str, Any]:
        "Calculate historical context for a specific hour. @zara\n\nProvides historical comparison data:\n- yesterday_same_hour: Production from same hour yesterday\n- same_hour_avg_7days: Average production for this hour over last 7 days\n\nArgs:\n    target_date: Date string YYYY-MM-DD\n    target_hour: Hour (0-23)\n\nReturns:\n    Dict with historical context\n"
        try:
            date_dt = datetime.fromisoformat(target_date)
            yesterday = (date_dt - timedelta(days=1)).date().isoformat()
            yesterday_row = await self.db.fetchone(
                "SELECT actual_kwh FROM hourly_predictions\n                   WHERE target_date = ? AND target_hour = ?",
                (yesterday, target_hour),
            )
            yesterday_kwh = (
                yesterday_row[0] if yesterday_row and yesterday_row[0] else None
            )
            week_start = (date_dt - timedelta(days=7)).date().isoformat()
            average_row = await self.db.fetchone(
                "SELECT AVG(actual_kwh) FROM hourly_predictions\n                   WHERE target_hour = ?\n                   AND target_date >= ?\n                   AND target_date < ?\n                   AND actual_kwh IS NOT NULL",
                (target_hour, week_start, target_date),
            )
            average_kwh = average_row[0] if average_row and average_row[0] else None
            return (
                (
                    {
                        "yesterday_same_hour": (
                            round(yesterday_kwh, 4) if yesterday_kwh else None
                        ),
                        "same_hour_avg_7days": (
                            round(average_kwh, 4) if average_kwh else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error("Failed to calculate historical context: %s", error)
            return (
                (
                    {"yesterday_same_hour": None, "same_hour_avg_7days": None}
                    if True
                    else None
                )
                if True
                else None
            )
