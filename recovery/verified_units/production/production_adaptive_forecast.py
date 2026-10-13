# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Adaptive Forecast Engine for Solar Forecast ML.

Implements autonomous midday correction logic. The user only enables
the mode (ON/OFF) - SFML decides fully autonomously whether and when
a correction is meaningful.

Decision criteria (ALL must be met):
1. Significant deviation on the last completed hour (> 35% AND > dynamic threshold)
2. No special situation on that hour (Snow/Weather-Alert)
3. Sufficient production time remaining (>= 4h until sunset)
4. Fresh weather data available (newer than morning forecast)
5. Significant weather data change (cloud cover diff > 25pp)

On correction, a full-day re-forecast is generated and ALL prediction
columns are updated via UPDATE (actuals are never touched).
A prior-hour rain alert does not block a later clearing hour.

@author zara
"""
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

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)


class AdaptiveForecastEngine:
    """
    Autonomous decision logic for adaptive forecast corrections.

    The user only enables the mode - SFML decides fully autonomously
    whether and when a correction is meaningful.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "SolarForecastMLCoordinator",
        db_manager: "DatabaseManager",
    ):
        """Initialize the Adaptive Forecast Engine. @zara"""
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager
        self._forecast_regeneration = ForecastRegenerationHelper(
            hass, coordinator, db_manager
        )
        self._last_correction_date = None
        self._correction_applied_today = False
        _LOGGER.debug("AdaptiveForecastEngine initialized")

    async def run_midday_check(self, manual: bool = False) -> None:
        """
        Main entry point for midday check (12:30).

        Checks whether a correction should be performed and executes
        it autonomously if needed.

        Args:
            manual: If True, skip cooldown and criteria checks (service call). @zara
        """
        now = dt_util.now()
        today = now.date().isoformat()
        _LOGGER.info("=== Adaptive Forecast Check%s ===", " (MANUAL)" if manual else "")
        _LOGGER.info("Time: %s", now.strftime("%H:%M:%S"))
        if manual:
            _LOGGER.info("Manual mode: Skipping cooldown and criteria checks")
            correction_succeeded = await self._execute_correction({"now": now})
            if correction_succeeded:
                _LOGGER.info("Manual adaptive correction completed successfully")
            else:
                _LOGGER.error("Manual adaptive correction failed")
            if True:
                return
        if self._last_correction_date == today:
            _LOGGER.info("Cooldown active: Already performed a correction today")
            _LOGGER.info("== Result: NO CORRECTION (Cooldown) ==")
            return
        should_correct, reason, details = await self._should_correct()
        if should_correct:
            _LOGGER.info("== Result: CORRECTION WILL BE EXECUTED ==")
            correction_succeeded = await self._execute_correction(details)
            if correction_succeeded:
                self._last_correction_date = today
                self._correction_applied_today = True
                _LOGGER.info("Adaptive correction completed successfully")
            else:
                _LOGGER.error("Adaptive correction failed")
        else:
            _LOGGER.info("== Result: NO CORRECTION ==")
            _LOGGER.info("Reason: %s", reason)
        if True:
            return

    async def _should_correct(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Check whether a correction should be performed.

        Returns:
            (should_correct: bool, reason: str, details: dict) @zara
        """
        now = dt_util.now()
        today = now.date().isoformat()
        details = {}
        _LOGGER.info("Collecting analysis data...")
        astronomy = await self._get_astronomy_data(today)
        if not astronomy:
            return (False, "No astronomy data available", {})
        sunrise_value = astronomy.get("sunrise")
        sunset_value = astronomy.get("sunset")
        if not sunrise_value or not sunset_value:
            return (False, "Sunrise/Sunset not available", {})
        try:
            sunrise = (
                datetime.fromisoformat(str(sunrise_value))
                if isinstance(sunrise_value, str)
                else sunrise_value
            )
            sunset = (
                datetime.fromisoformat(str(sunset_value))
                if isinstance(sunset_value, str)
                else sunset_value
            )
            if sunrise.tzinfo is None and now.tzinfo is not None:
                sunrise = sunrise.replace(tzinfo=now.tzinfo)
            if sunset.tzinfo is None and now.tzinfo is not None:
                sunset = sunset.replace(tzinfo=now.tzinfo)
        except (ValueError, TypeError) as parse_error:
            return (False, f"Error parsing Sunrise/Sunset: {parse_error}", {})
        details["sunrise"] = sunrise
        details["sunset"] = sunset
        details["now"] = now
        predictions = await self._get_predictions_for_date(today)
        if not predictions:
            return (False, "No predictions for today", {})
        hour_analysis = self._analyze_last_completed_hour(predictions, sunrise, now)
        if hour_analysis is None:
            return (False, "Not enough data for last-hour analysis", {})
        details.update(hour_analysis)
        _LOGGER.info(
            "Last completed hour (%02d): Actual=%.2f kWh, Predicted=%.2f kWh, Deviation=%+.0f%% (%+.2f kWh)",
            hour_analysis.get("analyzed_hour"),
            hour_analysis["actual_kwh"],
            hour_analysis["predicted_kwh"],
            hour_analysis["deviation_percent"],
            hour_analysis["deviation_kwh"],
        )
        absolute_deviation_percent = abs(hour_analysis["deviation_percent"])
        absolute_deviation_kwh = abs(hour_analysis["deviation_kwh"])
        daily_forecast_kwh = await self._get_daily_forecast_kwh(today)
        dynamic_threshold_kwh = max(
            ADAPTIVE_DEVIATION_MIN_THRESHOLD_KWH,
            daily_forecast_kwh * ADAPTIVE_DEVIATION_PERCENT_OF_FORECAST,
        )
        details["daily_forecast_kwh"] = daily_forecast_kwh
        details["dynamic_threshold_kwh"] = dynamic_threshold_kwh
        deviation_is_significant = (
            absolute_deviation_percent > ADAPTIVE_DEVIATION_THRESHOLD_PERCENT
            and absolute_deviation_kwh > dynamic_threshold_kwh
        )
        _LOGGER.info(
            "Criterion 1: Daily forecast=%.2f kWh, Dynamic threshold=%.2f kWh, Deviation=%.2f kWh",
            daily_forecast_kwh,
            dynamic_threshold_kwh,
            absolute_deviation_kwh,
        )
        if not deviation_is_significant:
            return (
                False,
                f"Deviation below threshold ({absolute_deviation_kwh:.2f} kWh < {dynamic_threshold_kwh:.2f} kWh)",
                details,
            )
        special_situation = await self._check_special_situations(
            predictions, analyzed_hour=hour_analysis.get("analyzed_hour")
        )
        if special_situation:
            _LOGGER.info(
                "Criterion 2 (No special situation): NOT MET (%s)", special_situation
            )
            return (False, f"Special situation active: {special_situation}", details)
        _LOGGER.info("Criterion 2 (No special situation): MET")
        remaining_hours = (sunset - now).total_seconds() / 3600
        if remaining_hours >= ADAPTIVE_MIN_REMAINING_HOURS:
            _LOGGER.info(
                "Criterion 3 (>= %dh until sunset): MET (%.1fh)",
                ADAPTIVE_MIN_REMAINING_HOURS,
                remaining_hours,
            )
            details["remaining_hours"] = remaining_hours
        else:
            _LOGGER.info(
                "Criterion 3 (>= %dh until sunset): NOT MET (%.1fh)",
                ADAPTIVE_MIN_REMAINING_HOURS,
                remaining_hours,
            )
            return (
                False,
                f"Not enough time for correction remaining ({remaining_hours:.1f}h)",
                details,
            )
        weather_freshness = await self._check_weather_freshness()
        if weather_freshness["fresh_data_available"]:
            _LOGGER.info(
                "Criterion 4 (Fresh weather data): MET (%s)",
                weather_freshness["model_run"],
            )
            details["weather_model_run"] = weather_freshness["model_run"]
        else:
            _LOGGER.info("Criterion 4 (Fresh weather data): NOT MET")
            return (False, "No fresher weather data available", details)
        production_override = (
            absolute_deviation_percent > 100 and absolute_deviation_kwh > 0.5
        )
        cloud_analysis = await self._analyze_cloud_cover_change(predictions, now)
        if cloud_analysis["significant_change"]:
            _LOGGER.info(
                "Criterion 5 (Cloud cover diff > %dpp): MET - Morning=%.0f%%, Current=%.0f%% (%+.0fpp)",
                ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD,
                cloud_analysis["morning_cloud_cover"],
                cloud_analysis["current_cloud_cover"],
                cloud_analysis["diff"],
            )
            details["cloud_analysis"] = cloud_analysis
        elif production_override:
            _LOGGER.info(
                "Criterion 5 (Cloud cover diff > %dpp): NOT MET - Morning=%.0f%%, Current=%.0f%% (%+.0fpp)",
                ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD,
                cloud_analysis["morning_cloud_cover"],
                cloud_analysis["current_cloud_cover"],
                cloud_analysis["diff"],
            )
            _LOGGER.info(
                "Criterion 5 BYPASSED: Production override — deviation %.0f%% (%.2f kWh) proves forecast wrong",
                absolute_deviation_percent,
                absolute_deviation_kwh,
            )
            details["cloud_analysis"] = cloud_analysis
            details["production_override"] = True
        else:
            _LOGGER.info(
                "Criterion 5 (Cloud cover diff > %dpp): NOT MET - Morning=%.0f%%, Current=%.0f%% (%+.0fpp)",
                ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD,
                cloud_analysis["morning_cloud_cover"],
                cloud_analysis["current_cloud_cover"],
                cloud_analysis["diff"],
            )
            return (
                False,
                f"Weather data nearly unchanged (Diff: {cloud_analysis['diff']:.0f}pp)",
                details,
            )
        return (True, "All criteria met", details)

    def _analyze_last_completed_hour(
        self, predictions: List[Dict[str, Any]], sunrise: datetime, now: datetime
    ) -> Optional[Dict[str, Any]]:
        """Analyze the last completed production hour only.

        Sunrise-to-now net energy hides rain-then-sun. One completed hour
        is enough; the caller still requires remaining daylight and weather
        freshness before correcting.

        Returns:
            Dict with actual_kwh, predicted_kwh, deviation_percent, deviation_kwh
            or None if not enough data.
        """
        sunrise_hour = sunrise.hour
        current_hour = now.hour
        analyzed_hour = current_hour - 1
        if analyzed_hour < sunrise_hour:
            return
        actual_kwh = 0.0
        predicted_kwh = 0.0
        matching_prediction_count = 0
        for prediction in predictions:
            target_hour = prediction.get("target_hour")
            if target_hour != analyzed_hour:
                continue
            actual_value_kwh = prediction.get("actual_kwh")
            predicted_value_kwh = prediction.get("prediction_kwh", 0)
            if actual_value_kwh is not None:
                actual_kwh += actual_value_kwh
                predicted_kwh += predicted_value_kwh
                matching_prediction_count += 1
        if matching_prediction_count < 1:
            return
        if predicted_kwh > 0.01:
            deviation_percent = (actual_kwh - predicted_kwh) / predicted_kwh * 100
        else:
            deviation_percent = 0.0
        deviation_kwh = actual_kwh - predicted_kwh
        return {
            "actual_kwh": actual_kwh,
            "predicted_kwh": predicted_kwh,
            "deviation_percent": deviation_percent,
            "deviation_kwh": deviation_kwh,
            "hours_analyzed": matching_prediction_count,
            "start_hour": analyzed_hour,
            "end_hour": current_hour,
            "analyzed_hour": analyzed_hour,
        }

    def _analyze_morning_production(
        self, predictions: List[Dict[str, Any]], sunrise: datetime, now: datetime
    ) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper; scoring uses the last completed hour."""
        return self._analyze_last_completed_hour(predictions, sunrise, now)

    async def _get_predictions_for_date(self, date: str) -> List[Dict[str, Any]]:
        """Get hourly predictions for a specific date from database. @zara"""
        try:
            prediction_rows = await self.db.fetchall(
                "SELECT prediction_id, target_hour, prediction_kwh, actual_kwh,\n                          confidence, is_production_hour, has_weather_alert,\n                          weather_alert_type\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (date,),
            )
            predictions = []
            for prediction_row in prediction_rows:
                prediction = {
                    "id": prediction_row[0],
                    "target_hour": prediction_row[1],
                    "prediction_kwh": prediction_row[2],
                    "actual_kwh": prediction_row[3],
                    "confidence": prediction_row[4],
                    "is_production_hour": prediction_row[5],
                    "flags": {
                        "has_weather_alert": prediction_row[6],
                        "weather_alert_type": prediction_row[7],
                    },
                }
                weather_rows = await self.db.fetchall(
                    "SELECT weather_type, temperature, clouds, solar_radiation_wm2\n                       FROM prediction_weather\n                       WHERE prediction_id = ?",
                    (prediction_row[0],),
                )
                for weather_row in weather_rows:
                    weather_type = weather_row[0]
                    weather = {
                        "temperature": weather_row[1],
                        "clouds": weather_row[2],
                        "solar_radiation_wm2": weather_row[3],
                    }
                    if weather_type == "forecast":
                        prediction["weather_forecast"] = weather
                    elif weather_type == "actual":
                        prediction["weather_actual"] = weather
                predictions.append(prediction)
            return predictions
        except Exception as error:
            _LOGGER.error("Error getting predictions for date %s: %s", date, error)
            return []

    async def _get_daily_forecast_kwh(self, date: str) -> float:
        """
        Get daily forecast for the specified date.

        Returns:
            Daily forecast in kWh or 1.0 as fallback. @zara
        """
        try:
            forecast_row = await self.db.fetchone(
                "SELECT prediction_kwh FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
            if forecast_row and forecast_row[0] is not None and (forecast_row[0] > 0):
                return float(forecast_row[0])
            predictions = await self._get_predictions_for_date(date)
            if predictions:
                summed_forecast_kwh = sum(
                    (
                        prediction.get("prediction_kwh", 0) or 0
                        for prediction in predictions
                    )
                )
                if summed_forecast_kwh > 0:
                    return summed_forecast_kwh
            _LOGGER.warning("No daily forecast available - using fallback 1.0 kWh")
        except Exception as error:
            _LOGGER.error("Error getting daily forecast: %s", error)
        return 1.0

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
        snow_covered_predictions = 0
        daylight_predictions = 0
        alert_reason = None
        for prediction in predictions:
            flags = prediction.get("flags", {})
            weather_alert_type = flags.get("weather_alert_type")
            target_hour = prediction.get("target_hour", 0)
            if 8 <= target_hour <= 16:
                daylight_predictions += 1
                if weather_alert_type == "snow_covered_panels":
                    snow_covered_predictions += 1
            if analyzed_hour is not None and target_hour != analyzed_hour:
                continue
            if (
                weather_alert_type
                and weather_alert_type
                not in ("snow_covered_panels", WEATHER_ALERT_UNEXPECTED_SUN)
                and flags.get("has_weather_alert")
            ):
                if alert_reason is None:
                    alert_reason = f"Weather alert: {weather_alert_type}"
        if (
            daylight_predictions > 0
            and snow_covered_predictions > daylight_predictions * 0.5
        ):
            return "Snow-covered panels"
        return alert_reason

    async def _check_weather_freshness(self) -> Dict[str, Any]:
        """
        Check if fresher weather data is available by querying the database.

        Verifies that weather_forecast table was actually updated recently
        (within 3 hours), not just assumed fresh based on UTC hour.

        Returns:
            Dict with fresh_data_available and model_run. @zara
        """
        now = dt_util.now()
        today = now.date().isoformat()
        utc_time_tuple = now.utctimetuple()
        utc_hour = utc_time_tuple.tm_hour
        if utc_hour >= 15:
            model_run = "12:00 UTC"
        elif utc_hour >= 9:
            model_run = "06:00 UTC"
        elif utc_hour >= 3:
            model_run = "00:00 UTC"
        else:
            model_run = "18:00 UTC (yesterday)"
        try:
            updated_at_row = await self.db.fetchone(
                "SELECT MAX(updated_at) FROM weather_forecast WHERE forecast_date = ?",
                (today,),
            )
            if updated_at_row and updated_at_row[0]:
                last_updated = datetime.fromisoformat(str(updated_at_row[0]))
                if last_updated.tzinfo is None and now.tzinfo is not None:
                    last_updated = last_updated.replace(tzinfo=now.tzinfo)
                age_hours = (now - last_updated).total_seconds() / 3600
                fresh_data_available = age_hours <= 3.0
                _LOGGER.debug(
                    "Weather data freshness: last_updated=%s, age=%.1fh, fresh=%s",
                    last_updated.isoformat(),
                    age_hours,
                    fresh_data_available,
                )
            else:
                fresh_data_available = False
                _LOGGER.warning("No weather forecast data found for today in DB")
        except Exception as error:
            _LOGGER.warning("Error checking weather freshness from DB: %s", error)
            fresh_data_available = False
        return {"fresh_data_available": fresh_data_available, "model_run": model_run}

    async def _analyze_cloud_cover_change(
        self, predictions: List[Dict[str, Any]], now: datetime
    ) -> Dict[str, Any]:
        """
        Compare cloud cover from morning forecast with current data.

        Returns:
            Dict with morning_cloud_cover, current_cloud_cover, diff, significant_change. @zara
        """
        morning_cloud_values = []
        for prediction in predictions:
            target_hour = prediction.get("target_hour", 0)
            if target_hour < now.hour or target_hour > 20:
                continue
            weather_forecast = prediction.get("weather_forecast", {})
            cloud_cover = weather_forecast.get("clouds")
            if cloud_cover is not None:
                morning_cloud_values.append(cloud_cover)
        if not morning_cloud_values:
            return {
                "morning_cloud_cover": 0,
                "current_cloud_cover": 0,
                "diff": 0,
                "significant_change": False,
            }
        morning_cloud_cover = sum(morning_cloud_values) / len(morning_cloud_values)
        current_cloud_cover = await self._get_current_cloud_cover_forecast(now)
        if current_cloud_cover is None:
            current_cloud_cover = morning_cloud_cover
        cloud_cover_diff = current_cloud_cover - morning_cloud_cover
        significant_change = abs(cloud_cover_diff) > ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD
        return {
            "morning_cloud_cover": morning_cloud_cover,
            "current_cloud_cover": current_cloud_cover,
            "diff": cloud_cover_diff,
            "significant_change": significant_change,
        }

    async def _get_current_cloud_cover_forecast(self, now: datetime) -> Optional[float]:
        """Get current cloud cover forecast for the afternoon. @zara"""
        try:
            date_str = now.date().isoformat()
            weather_rows = await self.db.fetchall(
                "SELECT hour, clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND hour >= ? AND hour <= 20",
                (date_str, now.hour),
            )
            if weather_rows:
                cloud_values = [
                    weather_row[1]
                    for weather_row in weather_rows
                    if weather_row[1] is not None
                ]
                if cloud_values:
                    return sum(cloud_values) / len(cloud_values)
        except Exception as error:
            _LOGGER.debug("Error getting current cloud cover: %s", error)
            return
        return None if True else None

    async def _get_astronomy_data(self, date: str) -> Optional[Dict[str, Any]]:
        """Get astronomy data for the specified date. @zara"""
        try:
            astronomy_row = await self.db.fetchone(
                "SELECT sunrise, sunset, solar_noon, daylight_hours\n                   FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = 12\n                   LIMIT 1",
                (date,),
            )
            if astronomy_row:
                return {
                    "sunrise": astronomy_row[0],
                    "sunset": astronomy_row[1],
                    "solar_noon": astronomy_row[2],
                    "daylight_hours": astronomy_row[3],
                }
        except Exception as error:
            _LOGGER.error("Error reading astronomy data: %s", error)
            return

    async def _execute_correction(self, details: Dict[str, Any]) -> bool:
        """
        Execute midday forecast correction.

        1) Refresh weather API + corrected forecast
        2) Generate new forecast via orchestrator
        3) Past hours (start..now): prediction_kwh = actual_kwh
        4) Future hours (now+1..sunset): prediction_kwh = new forecast
        5) Same for prediction_panel_groups + prediction_weather
        6) daily_forecasts = SUM(prediction_kwh), re-lock
        7) Coordinator state + sensors
        Returns:
            True on success. @zara
        """
        now = details.get("now", dt_util.now())
        date_str = now.date().isoformat()
        current_hour = now.hour
        correction_time = now
        _LOGGER.info(
            "ADAPTIVE CORRECTION: Start — current hour=%d, date=%s",
            current_hour,
            date_str,
        )
        try:
            await self._forecast_regeneration.refresh_weather_data(
                log_prefix="Adaptive"
            )
            hourly_forecast, weather_by_date_hour = (
                await self._forecast_regeneration.generate_full_forecast(
                    date_str, log_prefix="Adaptive"
                )
            )
            if not hourly_forecast:
                raise RuntimeError("Full forecast could not be generated")
            forecast_by_hour = {}
            for forecast_hour in hourly_forecast:
                if forecast_hour.get("date", "")[:10] == date_str:
                    forecast_by_hour[forecast_hour.get("hour", -1)] = forecast_hour
            _LOGGER.info(
                "ADAPTIVE CORRECTION: New forecast has %d hours for today",
                len(forecast_by_hour),
            )
            correction_time_iso = correction_time.isoformat()
            past_hours_updated = 0
            future_hours_updated = 0
            published_snapshot = {}
            async with self.db.transaction():
                prediction_rows = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh\n                       FROM hourly_predictions WHERE target_date = ?",
                    (date_str,),
                )
                for prediction_row in prediction_rows:
                    prediction_id = prediction_row[0]
                    target_hour = prediction_row[1]
                    actual_kwh = prediction_row[2]
                    if target_hour < current_hour:
                        if actual_kwh is not None:
                            await self.db.execute(
                                "UPDATE hourly_predictions\n                                   SET prediction_kwh = ?,\n                                       adaptive_corrected = TRUE,\n                                       adaptive_correction_time = ?\n                                   WHERE prediction_id = ?",
                                (actual_kwh, correction_time_iso, prediction_id),
                                auto_commit=False,
                            )
                            await self.db.execute(
                                "UPDATE prediction_panel_groups\n                                   SET prediction_kwh = COALESCE(actual_kwh, prediction_kwh)\n                                   WHERE prediction_id = ?",
                                (prediction_id,),
                                auto_commit=False,
                            )
                            past_hours_updated += 1
                            continue
                        continue
                    hourly_prediction = forecast_by_hour.get(target_hour)
                    if hourly_prediction:
                        forecast_kwh = hourly_prediction.get("production_kwh", 0.0)
                        await self.db.execute(
                            "UPDATE hourly_predictions\n                                   SET prediction_kwh = ?,\n                                       prediction_kwh_uncapped = ?,\n                                       prediction_method = ?,\n                                       prediction_created_at = ?,\n                                       physics_kwh = ?,\n                                       ai_kwh = ?,\n                                       ai_confidence = ?,\n                                       ml_contribution_percent = ?,\n                                       confidence = ?,\n                                       lstm_kwh = ?,\n                                       ridge_kwh = ?,\n                                       tfs_kwh = ?,\n                                       tfs_weight = ?,\n                                       adaptive_corrected = TRUE,\n                                       adaptive_correction_time = ?\n                                   WHERE prediction_id = ?",
                            (
                                round(forecast_kwh, 4),
                                round(forecast_kwh, 4),
                                hourly_prediction.get("method", "physics"),
                                correction_time,
                                hourly_prediction.get("physics_kwh"),
                                hourly_prediction.get("ai_kwh"),
                                hourly_prediction.get("ai_confidence"),
                                hourly_prediction.get("ml_contribution", 0),
                                hourly_prediction.get("confidence", 75.0),
                                hourly_prediction.get("lstm_kwh"),
                                hourly_prediction.get("ridge_kwh"),
                                hourly_prediction.get("tfs_kwh"),
                                hourly_prediction.get("tfs_weight"),
                                correction_time_iso,
                                prediction_id,
                            ),
                            auto_commit=False,
                        )
                        panel_group_predictions = hourly_prediction.get("groups")
                        if panel_group_predictions:
                            for group_prediction in panel_group_predictions:
                                await self.db.execute(
                                    "UPDATE prediction_panel_groups\n                                           SET prediction_kwh = ?,\n                                               physics_kwh = ?,\n                                               ai_kwh = ?,\n                                               lstm_kwh = ?,\n                                               ridge_kwh = ?,\n                                               tfs_kwh = ?,\n                                               tfs_weight = ?,\n                                               raw_physics_kwh = COALESCE(?, raw_physics_kwh)\n                                           WHERE prediction_id = ? AND group_name = ?",
                                    (
                                        round(
                                            group_prediction.get("power_kwh", 0.0), 4
                                        ),
                                        (
                                            round(group_prediction["physics_kwh"], 4)
                                            if group_prediction.get("physics_kwh")
                                            else None
                                        ),
                                        (
                                            round(group_prediction["ai_kwh"], 4)
                                            if group_prediction.get("ai_kwh")
                                            else None
                                        ),
                                        (
                                            round(group_prediction["lstm_kwh"], 4)
                                            if group_prediction.get("lstm_kwh")
                                            else None
                                        ),
                                        (
                                            round(group_prediction["ridge_kwh"], 4)
                                            if group_prediction.get("ridge_kwh")
                                            else None
                                        ),
                                        (
                                            round(group_prediction["tfs_kwh"], 4)
                                            if group_prediction.get("tfs_kwh")
                                            else None
                                        ),
                                        (
                                            round(group_prediction["tfs_weight"], 3)
                                            if group_prediction.get("tfs_weight")
                                            is not None
                                            else None
                                        ),
                                        (
                                            round(
                                                group_prediction["raw_physics_kwh"], 4
                                            )
                                            if group_prediction.get("raw_physics_kwh")
                                            is not None
                                            else None
                                        ),
                                        prediction_id,
                                        group_prediction.get("name"),
                                    ),
                                    auto_commit=False,
                                )
                                continue
                        weather_prediction = weather_by_date_hour.get(
                            (date_str, target_hour)
                        )
                        if weather_prediction:
                            await self.db.execute(
                                "UPDATE prediction_weather\n                                       SET temperature = ?,\n                                           solar_radiation_wm2 = ?,\n                                           wind = ?,\n                                           humidity = ?,\n                                           rain = ?,\n                                           clouds = ?,\n                                           pressure = ?,\n                                           diffuse_radiation = ?,\n                                           direct_radiation = ?\n                                       WHERE prediction_id = ? AND weather_type = 'forecast'",
                                (
                                    weather_prediction.get("temperature"),
                                    weather_prediction.get("solar_radiation_wm2"),
                                    weather_prediction.get("wind"),
                                    weather_prediction.get("humidity"),
                                    weather_prediction.get("rain"),
                                    weather_prediction.get("clouds"),
                                    weather_prediction.get("pressure"),
                                    weather_prediction.get("diffuse_radiation")
                                    or weather_prediction.get("dhi"),
                                    weather_prediction.get("direct_radiation"),
                                    prediction_id,
                                ),
                                auto_commit=False,
                            )
                        future_hours_updated += 1
                original_daily_kwh = await self._get_daily_forecast_kwh(date_str)
                daily_sum_row = await self.db.fetchone(
                    "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                    (date_str,),
                )
                corrected_daily_kwh = (
                    round(daily_sum_row[0], 2)
                    if daily_sum_row and daily_sum_row[0]
                    else 0.0
                )
                await self.db.execute(
                    "UPDATE daily_forecasts\n                       SET prediction_kwh = ?,\n                           prediction_kwh_raw = ?,\n                           locked = TRUE,\n                           locked_at = ?,\n                           source = 'adaptive_correction'\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (
                        corrected_daily_kwh,
                        corrected_daily_kwh,
                        correction_time,
                        date_str,
                    ),
                    auto_commit=False,
                )
                await self.db.execute(
                    "INSERT INTO daily_forecast_updates\n                       (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                       VALUES ('today', ?, ?, 'adaptive_correction', ?)",
                    (date_str, corrected_daily_kwh, correction_time),
                    auto_commit=False,
                )
                details["original_daily_kwh"] = original_daily_kwh
                details["corrected_daily_kwh"] = corrected_daily_kwh
                data_manager = getattr(self.coordinator, "data_manager", None)
                operational_forecast_handler = getattr(
                    data_manager, "operational_forecast_handler", None
                )
                if operational_forecast_handler is None:
                    raise RuntimeError(
                        "Operational forecast handler is not available for adaptive correction"
                    )
                published_snapshot = await operational_forecast_handler.publish_adaptive_correction_snapshot(
                    snapshot_date=date_str,
                    corrected_daily_kwh=corrected_daily_kwh,
                    cutoff_hour=current_hour,
                    created_at=correction_time,
                )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: %d past hours (pred=actual), %d future hours (new forecast)",
                past_hours_updated,
                future_hours_updated,
            )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: daily_forecasts %.2f -> %.2f kWh (re-locked)",
                original_daily_kwh,
                corrected_daily_kwh,
            )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: OPS snapshot #%s synchronized (%d hourly, %d weather, %d groups)",
                published_snapshot.get("snapshot_sequence"),
                published_snapshot.get("hourly_rows", 0),
                published_snapshot.get("weather_rows", 0),
                published_snapshot.get("group_rows", 0),
            )
            await self._send_correction_notification(details, future_hours_updated)
            if hasattr(self.coordinator, "data_manager"):
                await self.coordinator.data_manager.save_expected_daily_production(
                    corrected_daily_kwh
                )
            self.coordinator.expected_daily_production = corrected_daily_kwh
            if self.coordinator.data:
                self.coordinator.data["forecast_today"] = corrected_daily_kwh
                self.coordinator.data["expected_daily_production"] = corrected_daily_kwh
            if hasattr(self.coordinator, "_refresh_hourly_predictions_cache"):
                await self.coordinator._refresh_hourly_predictions_cache()
            self.coordinator.async_update_listeners()
            _LOGGER.info(
                "ADAPTIVE CORRECTION: Complete — %.2f kWh, sensors refreshed",
                corrected_daily_kwh,
            )
            return True
        except Exception as error:
            _LOGGER.error("ADAPTIVE CORRECTION failed: %s", error, exc_info=True)
            return False

    async def _send_correction_notification(
        self, details: Dict[str, Any], updated_hours: int
    ) -> None:
        """Send notification about performed correction. @zara"""
        try:
            notification_service = self.hass.data.get(DOMAIN, {}).get(
                "notification_service"
            )
            if not notification_service:
                _LOGGER.debug("Notification service not available")
                return
            actual_kwh = details.get("actual_kwh", 0)
            predicted_kwh = details.get("predicted_kwh", 0)
            original_daily_kwh = details.get("original_daily_kwh", 0)
            corrected_daily_kwh = details.get("corrected_daily_kwh", 0)
            await notification_service.show_adaptive_correction(
                original_kwh=original_daily_kwh,
                corrected_kwh=corrected_daily_kwh,
                reason="Updated weather data",
                hours_corrected=updated_hours,
                am_actual=actual_kwh,
                am_predicted=predicted_kwh,
            )
            _LOGGER.info("Notification sent")
        except Exception as error:
            _LOGGER.error("Error sending notification: %s", error)
        return None if True else None
