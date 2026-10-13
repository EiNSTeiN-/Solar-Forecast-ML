# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Adaptive Forecast Engine for Solar Forecast Energy AI.

Implements autonomous midday correction logic. The user only enables
the mode (ON/OFF) - SFML decides fully autonomously whether and when
a correction is meaningful.

Decision criteria (ALL must be met):
1. Significant deviation (> 35% AND > dynamic threshold)
2. No special situation active (Snow/Weather-Alert)
3. Sufficient production time remaining (>= 4h until sunset)
4. Fresh weather data available (newer than morning forecast)
5. Significant weather data change (cloud cover diff > 25pp)

On correction, a full-day re-forecast is generated and ALL prediction
columns are updated via UPDATE (actuals are never touched).

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
from ..astronomy_provider import AstronomyProviderAdapter

if TYPE_CHECKING:
    from ..coordinator import SolarForecastEAICoordinator
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
        coordinator: "SolarForecastEAICoordinator",
        db_manager: "DatabaseManager",
        astronomy_provider: AstronomyProviderAdapter,
    ):
        """Initialize the Adaptive Forecast Engine. @zara"""
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager
        self.astronomy_provider = astronomy_provider
        self._last_correction_date = None
        self._correction_applied_today = False
        _LOGGER.debug("AdaptiveForecastEngine initialized")

    async def run_midday_check(self, manual: bool = False) -> None:
        "\nMain entry point for midday check (12:30).\n\nChecks whether a correction should be performed and executes\nit autonomously if needed.\n\nArgs:\n    manual: If True, skip cooldown and criteria checks (service call). @zara\n"
        current_time = dt_util.now()
        today = current_time.date().isoformat()
        _LOGGER.info("=== Adaptive Forecast Check%s ===", " (MANUAL)" if manual else "")
        _LOGGER.info("Time: %s", current_time.strftime("%H:%M:%S"))
        if manual:
            _LOGGER.info("Manual mode: Skipping cooldown and criteria checks")
            correction_succeeded = await self._execute_correction({"now": current_time})
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
        "\nCheck whether a correction should be performed.\n\nReturns:\n    (should_correct: bool, reason: str, details: dict) @zara\n"
        current_time = dt_util.now()
        today = current_time.date().isoformat()
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
            if sunrise.tzinfo is None and current_time.tzinfo is not None:
                sunrise = sunrise.replace(tzinfo=current_time.tzinfo)
            if sunset.tzinfo is None and current_time.tzinfo is not None:
                sunset = sunset.replace(tzinfo=current_time.tzinfo)
        except (ValueError, TypeError) as error:
            return (False, f"Error parsing Sunrise/Sunset: {error}", {})
        details["sunrise"] = sunrise
        details["sunset"] = sunset
        details["now"] = current_time
        predictions = await self._get_predictions_for_date(today)
        if not predictions:
            return (False, "No predictions for today", {})
        morning_analysis = self._analyze_morning_production(
            predictions, sunrise, current_time
        )
        if morning_analysis is None:
            return (False, "Not enough data for morning analysis", {})
        details.update(morning_analysis)
        _LOGGER.info(
            "Morning analysis (%s-%s): Actual=%.2f kWh, Predicted=%.2f kWh, Deviation=%+.0f%% (%+.2f kWh)",
            sunrise.strftime("%H:%M"),
            current_time.strftime("%H:%M"),
            morning_analysis["actual_kwh"],
            morning_analysis["predicted_kwh"],
            morning_analysis["deviation_percent"],
            morning_analysis["deviation_kwh"],
        )
        deviation_percent = abs(morning_analysis["deviation_percent"])
        deviation_kwh = abs(morning_analysis["deviation_kwh"])
        daily_forecast_kwh = await self._get_daily_forecast_kwh(today)
        dynamic_threshold_kwh = max(
            ADAPTIVE_DEVIATION_MIN_THRESHOLD_KWH,
            daily_forecast_kwh * ADAPTIVE_DEVIATION_PERCENT_OF_FORECAST,
        )
        details["daily_forecast_kwh"] = daily_forecast_kwh
        details["dynamic_threshold_kwh"] = dynamic_threshold_kwh
        significant_deviation = (
            deviation_percent > ADAPTIVE_DEVIATION_THRESHOLD_PERCENT
            and deviation_kwh > dynamic_threshold_kwh
        )
        _LOGGER.info(
            "Criterion 1: Daily forecast=%.2f kWh, Dynamic threshold=%.2f kWh, Deviation=%.2f kWh",
            daily_forecast_kwh,
            dynamic_threshold_kwh,
            deviation_kwh,
        )
        if not significant_deviation:
            return (
                False,
                f"Deviation below threshold ({deviation_kwh:.2f} kWh < {dynamic_threshold_kwh:.2f} kWh)",
                details,
            )
        special_situation = await self._check_special_situations(predictions)
        if special_situation:
            _LOGGER.info(
                "Criterion 2 (No special situation): NOT MET (%s)", special_situation
            )
            return (False, f"Special situation active: {special_situation}", details)
        _LOGGER.info("Criterion 2 (No special situation): MET")
        remaining_hours = (sunset - current_time).total_seconds() / 3600
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
        freshness = await self._check_weather_freshness()
        if freshness["fresh_data_available"]:
            _LOGGER.info(
                "Criterion 4 (Fresh weather data): MET (%s)", freshness["model_run"]
            )
            details["weather_model_run"] = freshness["model_run"]
        else:
            _LOGGER.info("Criterion 4 (Fresh weather data): NOT MET")
            return (False, "No fresher weather data available", details)
        production_override = deviation_percent > 100 and deviation_kwh > 0.5
        cloud_analysis = await self._analyze_cloud_cover_change(
            predictions, current_time
        )
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
                deviation_percent,
                deviation_kwh,
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

    def _analyze_morning_production(
        self, predictions: List[Dict[str, Any]], sunrise: datetime, now: datetime
    ) -> Optional[Dict[str, Any]]:
        "\nAnalyze morning production (sunrise until now).\n\nReturns:\n    Dict with actual_kwh, predicted_kwh, deviation_percent, deviation_kwh\n    or None if not enough data. @zara\n"
        sunrise_hour = sunrise.hour
        current_hour = now.hour
        actual_kwh = 0.0
        predicted_kwh = 0.0
        hours_analyzed = 0
        for prediction in predictions:
            target_hour = prediction.get("target_hour")
            if target_hour is None:
                continue
            if target_hour < sunrise_hour or target_hour >= current_hour:
                continue
            actual_hour_kwh = prediction.get("actual_kwh")
            predicted_hour_kwh = prediction.get("prediction_kwh", 0)
            if actual_hour_kwh is not None:
                actual_kwh += actual_hour_kwh
                predicted_kwh += predicted_hour_kwh
                hours_analyzed += 1
        if hours_analyzed < 2:
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
            "hours_analyzed": hours_analyzed,
            "start_hour": sunrise_hour,
            "end_hour": current_hour,
        }

    async def _get_predictions_for_date(self, date: str) -> List[Dict[str, Any]]:
        """Get hourly predictions for a specific date from database. @zara"""
        try:
            database_rows = await self.db.fetchall(
                "SELECT prediction_id, target_hour, prediction_kwh, actual_kwh,\n                          confidence, is_production_hour, has_weather_alert,\n                          weather_alert_type\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (date,),
            )
            predictions = []
            for database_row in database_rows:
                prediction = {
                    "id": database_row[0],
                    "target_hour": database_row[1],
                    "prediction_kwh": database_row[2],
                    "actual_kwh": database_row[3],
                    "confidence": database_row[4],
                    "is_production_hour": database_row[5],
                    "flags": {
                        "has_weather_alert": database_row[6],
                        "weather_alert_type": database_row[7],
                    },
                }
                weather_rows = await self.db.fetchall(
                    "SELECT weather_type, temperature, clouds, solar_radiation_wm2\n                       FROM prediction_weather\n                       WHERE prediction_id = ?",
                    (database_row[0],),
                )
                for weather_row in weather_rows:
                    weather_type = weather_row[0]
                    weather_data = {
                        "temperature": weather_row[1],
                        "clouds": weather_row[2],
                        "solar_radiation_wm2": weather_row[3],
                    }
                    if weather_type == "forecast":
                        prediction["weather_forecast"] = weather_data
                    elif weather_type == "actual":
                        prediction["weather_actual"] = weather_data
                predictions.append(prediction)
            return predictions
        except Exception as error:
            _LOGGER.error("Error getting predictions for date %s: %s", date, error)
            return []

    async def _get_daily_forecast_kwh(self, date: str) -> float:
        "\nGet daily forecast for the specified date.\n\nReturns:\n    Daily forecast in kWh or 1.0 as fallback. @zara\n"
        try:
            database_row = await self.db.fetchone(
                "SELECT prediction_kwh FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
            if database_row and database_row[0] is not None and (database_row[0] > 0):
                return float(database_row[0])
            predictions = await self._get_predictions_for_date(date)
            if predictions:
                hourly_total_kwh = sum(
                    (
                        prediction.get("prediction_kwh", 0) or 0
                        for prediction in predictions
                    )
                )
                if hourly_total_kwh > 0:
                    return hourly_total_kwh
            _LOGGER.warning("No daily forecast available - using fallback 1.0 kWh")
        except Exception as error:
            _LOGGER.error("Error getting daily forecast: %s", error)
        return 1.0

    async def _check_special_situations(
        self, predictions: List[Dict[str, Any]]
    ) -> Optional[str]:
        "\nCheck if special situations are active (Snow/Weather-Alert).\n\nNOTE: Frost (heavy_frost) is NOT a special situation! @zara V16.1\n- Frost = cold air, panels are NOT covered\n- Cold temperatures actually IMPROVE panel efficiency\n- Only physical coverage (snow, ice) affects production\n\nSnow only blocks if majority of daylight hours (8-16) affected. @zara V16.4.2\n\nReturns:\n    String with situation or None if none active. @zara\n"
        snow_hours = 0
        daylight_hours = 0
        special_reason = None
        for prediction in predictions:
            flags = prediction.get("flags", {})
            alert_type = flags.get("weather_alert_type")
            target_hour = prediction.get("target_hour", 0)
            if 8 <= target_hour <= 16:
                daylight_hours += 1
                if alert_type == "snow_covered_panels":
                    snow_hours += 1
            if (
                alert_type
                and alert_type != "snow_covered_panels"
                and flags.get("has_weather_alert")
            ):
                if special_reason is None:
                    special_reason = f"Weather alert: {alert_type}"
        if daylight_hours > 0 and snow_hours > daylight_hours * 0.5:
            return "Snow-covered panels"
        return special_reason

    async def _check_weather_freshness(self) -> Dict[str, Any]:
        "\nCheck if fresher weather data is available by querying the database.\n\nVerifies that weather_forecast table was actually updated recently\n(within 3 hours), not just assumed fresh based on UTC hour.\n\nReturns:\n    Dict with fresh_data_available and model_run. @zara\n"
        current_time = dt_util.now()
        today = current_time.date().isoformat()
        utc_time = current_time.utctimetuple()
        utc_hour = utc_time.tm_hour
        if utc_hour >= 15:
            model_run = "12:00 UTC"
        elif utc_hour >= 9:
            model_run = "06:00 UTC"
        elif utc_hour >= 3:
            model_run = "00:00 UTC"
        else:
            model_run = "18:00 UTC (yesterday)"
        try:
            database_row = await self.db.fetchone(
                "SELECT MAX(updated_at) FROM weather_forecast WHERE forecast_date = ?",
                (today,),
            )
            if database_row and database_row[0]:
                last_updated = datetime.fromisoformat(str(database_row[0]))
                if last_updated.tzinfo is None and current_time.tzinfo is not None:
                    last_updated = last_updated.replace(tzinfo=current_time.tzinfo)
                weather_age_hours = (current_time - last_updated).total_seconds() / 3600
                fresh_data_available = weather_age_hours <= 3.0
                _LOGGER.debug(
                    "Weather data freshness: last_updated=%s, age=%.1fh, fresh=%s",
                    last_updated.isoformat(),
                    weather_age_hours,
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
        "\nCompare cloud cover from morning forecast with current data.\n\nReturns:\n    Dict with morning_cloud_cover, current_cloud_cover, diff, significant_change. @zara\n"
        morning_cloud_values = []
        for prediction in predictions:
            target_hour = prediction.get("target_hour", 0)
            if target_hour < now.hour or target_hour > 20:
                continue
            original_weather = prediction.get("weather_forecast", {})
            forecast_cloud_cover = original_weather.get("clouds")
            if forecast_cloud_cover is not None:
                morning_cloud_values.append(forecast_cloud_cover)
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
        significant_cloud_change = (
            abs(cloud_cover_diff) > ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD
        )
        return {
            "morning_cloud_cover": morning_cloud_cover,
            "current_cloud_cover": current_cloud_cover,
            "diff": cloud_cover_diff,
            "significant_change": significant_cloud_change,
        }

    async def _get_current_cloud_cover_forecast(self, now: datetime) -> Optional[float]:
        """Get current cloud cover forecast for the afternoon. @zara"""
        try:
            today = now.date().isoformat()
            database_rows = await self.db.fetchall(
                "SELECT hour, clouds FROM weather_forecast\n                   WHERE forecast_date = ? AND hour >= ? AND hour <= 20",
                (today, now.hour),
            )
            if database_rows:
                current_cloud_values = [
                    weather_row[1]
                    for weather_row in database_rows
                    if weather_row[1] is not None
                ]
                if current_cloud_values:
                    return sum(current_cloud_values) / len(current_cloud_values)
        except Exception as error:
            _LOGGER.debug("Error getting current cloud cover: %s", error)
            return
        return None if True else None

    async def _get_astronomy_data(self, date: str) -> Optional[Dict[str, Any]]:
        """Get a complete, timezone-aware SFML astronomy day."""
        astronomy_day = await self.astronomy_provider.async_get_day(
            datetime.fromisoformat(date).date()
        )
        if not astronomy_day:
            return
        return {
            "sunrise": astronomy_day["sunrise_local"],
            "sunset": astronomy_day["sunset_local"],
            "solar_noon": astronomy_day["solar_noon_local"],
            "daylight_hours": astronomy_day["daylight_hours"],
        }

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
        current_time = details.get("now", dt_util.now())
        today = current_time.date().isoformat()
        current_hour = current_time.hour
        correction_time = current_time
        _LOGGER.info(
            "ADAPTIVE CORRECTION: Start — current hour=%d, date=%s", current_hour, today
        )
        try:
            await self._refresh_weather_data()
            hourly_forecast, weather_by_date_hour = await self._generate_full_forecast(
                today
            )
            if not hourly_forecast:
                raise RuntimeError("Full forecast could not be generated")
            forecast_by_hour = {}
            for hourly_entry in hourly_forecast:
                if hourly_entry.get("date", "")[:10] == today:
                    forecast_by_hour[hourly_entry.get("hour", -1)] = hourly_entry
            _LOGGER.info(
                "ADAPTIVE CORRECTION: New forecast has %d hours for today",
                len(forecast_by_hour),
            )
            correction_timestamp = correction_time.isoformat()
            past_hours_updated = 0
            future_hours_updated = 0
            existing_prediction_rows = await self.db.fetchall(
                "SELECT prediction_id, target_hour, actual_kwh\n                   FROM hourly_predictions WHERE target_date = ?",
                (today,),
            )
            for existing_prediction in existing_prediction_rows:
                prediction_id = existing_prediction[0]
                target_hour = existing_prediction[1]
                row_actual_kwh = existing_prediction[2]
                if target_hour < current_hour:
                    if row_actual_kwh is not None:
                        await self.db.execute(
                            "UPDATE hourly_predictions\n                               SET prediction_kwh = ?,\n                                   adaptive_corrected = TRUE,\n                                   adaptive_correction_time = ?\n                               WHERE prediction_id = ?",
                            (row_actual_kwh, correction_timestamp, prediction_id),
                        )
                        await self.db.execute(
                            "UPDATE prediction_panel_groups\n                               SET prediction_kwh = COALESCE(actual_kwh, prediction_kwh)\n                               WHERE prediction_id = ?",
                            (prediction_id,),
                        )
                        past_hours_updated += 1
                        continue
                    continue
                new_prediction = forecast_by_hour.get(target_hour)
                if new_prediction:
                    predicted_production_kwh = new_prediction.get("production_kwh", 0.0)
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                               SET prediction_kwh = ?,\n                                   prediction_kwh_uncapped = ?,\n                                   prediction_method = ?,\n                                   prediction_created_at = ?,\n                                   physics_kwh = ?,\n                                   ai_kwh = ?,\n                                   ai_confidence = ?,\n                                   ml_contribution_percent = ?,\n                                   confidence = ?,\n                                   lstm_kwh = ?,\n                                   ridge_kwh = ?,\n                                   adaptive_corrected = TRUE,\n                                   adaptive_correction_time = ?\n                               WHERE prediction_id = ?",
                        (
                            round(predicted_production_kwh, 4),
                            round(predicted_production_kwh, 4),
                            new_prediction.get("method", "physics"),
                            correction_time,
                            new_prediction.get("physics_kwh"),
                            new_prediction.get("ai_kwh"),
                            new_prediction.get("ai_confidence"),
                            new_prediction.get("ml_contribution", 0),
                            new_prediction.get("confidence", 75.0),
                            new_prediction.get("lstm_kwh"),
                            new_prediction.get("ridge_kwh"),
                            correction_timestamp,
                            prediction_id,
                        ),
                    )
                    prediction_groups = new_prediction.get("groups")
                    if prediction_groups:
                        for panel_group in prediction_groups:
                            await self.db.execute(
                                "UPDATE prediction_panel_groups\n                                       SET prediction_kwh = ?,\n                                           physics_kwh = ?,\n                                           ai_kwh = ?,\n                                           lstm_kwh = ?,\n                                           ridge_kwh = ?\n                                       WHERE prediction_id = ? AND group_name = ?",
                                (
                                    round(panel_group.get("power_kwh", 0.0), 4),
                                    (
                                        round(panel_group["physics_kwh"], 4)
                                        if panel_group.get("physics_kwh")
                                        else None
                                    ),
                                    (
                                        round(panel_group["ai_kwh"], 4)
                                        if panel_group.get("ai_kwh")
                                        else None
                                    ),
                                    (
                                        round(panel_group["lstm_kwh"], 4)
                                        if panel_group.get("lstm_kwh")
                                        else None
                                    ),
                                    (
                                        round(panel_group["ridge_kwh"], 4)
                                        if panel_group.get("ridge_kwh")
                                        else None
                                    ),
                                    prediction_id,
                                    panel_group.get("name"),
                                ),
                            )
                            continue
                    current_weather = weather_by_date_hour.get((today, target_hour))
                    if current_weather:
                        await self.db.execute(
                            "UPDATE prediction_weather\n                                   SET temperature = ?,\n                                       solar_radiation_wm2 = ?,\n                                       wind = ?,\n                                       humidity = ?,\n                                       rain = ?,\n                                       clouds = ?,\n                                       pressure = ?,\n                                       diffuse_radiation = ?,\n                                       direct_radiation = ?\n                                   WHERE prediction_id = ? AND weather_type = 'forecast'",
                            (
                                current_weather.get("temperature"),
                                current_weather.get("solar_radiation_wm2"),
                                current_weather.get("wind"),
                                current_weather.get("humidity"),
                                current_weather.get("rain"),
                                current_weather.get("clouds"),
                                current_weather.get("pressure"),
                                current_weather.get("diffuse_radiation")
                                or current_weather.get("dhi"),
                                current_weather.get("direct_radiation"),
                                prediction_id,
                            ),
                        )
                    future_hours_updated += 1
            _LOGGER.info(
                "ADAPTIVE CORRECTION: %d past hours (pred=actual), %d future hours (new forecast)",
                past_hours_updated,
                future_hours_updated,
            )
            original_daily_kwh = await self._get_daily_forecast_kwh(today)
            total_prediction_row = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                (today,),
            )
            corrected_daily_kwh = (
                round(total_prediction_row[0], 2)
                if total_prediction_row and total_prediction_row[0]
                else 0.0
            )
            await self.db.execute(
                "UPDATE daily_forecasts\n                   SET prediction_kwh = ?,\n                       prediction_kwh_raw = ?,\n                       locked = TRUE,\n                       locked_at = ?,\n                       source = 'adaptive_correction'\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (corrected_daily_kwh, corrected_daily_kwh, correction_time, today),
            )
            await self.db.execute(
                "INSERT INTO daily_forecast_updates\n                   (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                   VALUES ('today', ?, ?, 'adaptive_correction', ?)",
                (today, corrected_daily_kwh, correction_time),
            )
            details["original_daily_kwh"] = original_daily_kwh
            details["corrected_daily_kwh"] = corrected_daily_kwh
            _LOGGER.info(
                "ADAPTIVE CORRECTION: daily_forecasts %.2f -> %.2f kWh (re-locked)",
                original_daily_kwh,
                corrected_daily_kwh,
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

    async def _refresh_weather_data(self) -> None:
        """Fetch fresh weather from API and rebuild corrected forecast. @zara"""
        try:
            weather_pipeline = getattr(
                self.coordinator, "weather_pipeline_manager", None
            )
            if weather_pipeline:
                _LOGGER.info("Adaptive: Refreshing weather data from API...")
                weather_refreshed = await weather_pipeline.update_weather_cache(
                    force=True
                )
                if weather_refreshed:
                    _LOGGER.info("Adaptive: Weather API refresh successful")
                else:
                    _LOGGER.warning(
                        "Adaptive: Weather API refresh failed — using cached data"
                    )
            else:
                _LOGGER.warning(
                    "Adaptive: Weather pipeline not available — using cached data"
                )
            weather_corrector = (
                getattr(weather_pipeline, "weather_corrector", None)
                if weather_pipeline
                else None
            )
            if weather_corrector:
                _LOGGER.info("Adaptive: Rebuilding corrected forecast...")
                corrected_weather_rebuilt = (
                    await weather_corrector.create_corrected_forecast()
                )
                if corrected_weather_rebuilt:
                    _LOGGER.info("Adaptive: Corrected forecast rebuilt")
                else:
                    _LOGGER.warning("Adaptive: Corrected forecast rebuild failed")
            else:
                _LOGGER.warning("Adaptive: Weather corrector not available")
        except Exception as error:
            _LOGGER.warning("Adaptive: Weather refresh error (non-fatal): %s", error)
        return None if True else None

    async def _generate_full_forecast(
        self, date: str
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, int], Dict[str, Any]]]:
        "Generate complete forecast for all hours using the standard pipeline.\n\nReturns:\n    Tuple of (hourly_forecast_list, weather_by_date_hour_dict). @zara\n"
        empty_result = ([], {})
        if (
            not hasattr(self.coordinator, "weather_service")
            or not self.coordinator.weather_service
        ):
            _LOGGER.error("Weather service not available")
            return empty_result
        hourly_weather = (
            await self.coordinator.weather_service.get_corrected_hourly_forecast()
        )
        if not hourly_weather:
            _LOGGER.warning("No corrected weather data available")
            return empty_result
        weather_map = {}
        for weather_entry in hourly_weather:
            weather_key = (weather_entry.get("date", date), weather_entry.get("hour"))
            weather_map[weather_key] = weather_entry
        external_sensors = {}
        if hasattr(self.coordinator, "sensor_collector"):
            external_sensors = (
                self.coordinator.sensor_collector.collect_all_sensor_data_dict()
            )
        if not hasattr(self.coordinator, "forecast_orchestrator"):
            _LOGGER.error("Forecast orchestrator not available")
            return empty_result
        orchestration_result = (
            await self.coordinator.forecast_orchestrator.orchestrate_forecast(
                current_weather=None,
                hourly_forecast=hourly_weather,
                external_sensors=external_sensors,
                correction_factor=getattr(
                    self.coordinator, "learned_correction_factor", 1.0
                ),
            )
        )
        hourly_forecast = orchestration_result.get("hourly", [])
        if not hourly_forecast:
            _LOGGER.warning("Forecast orchestrator returned no hourly data")
            return empty_result
        _LOGGER.info(
            "Adaptive: Full forecast generated — %d hours total", len(hourly_forecast)
        )
        return (hourly_forecast, weather_map)

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
            morning_actual_kwh = details.get("actual_kwh", 0)
            morning_predicted_kwh = details.get("predicted_kwh", 0)
            original_daily_kwh = details.get("original_daily_kwh", 0)
            corrected_daily_kwh = details.get("corrected_daily_kwh", 0)
            await notification_service.show_adaptive_correction(
                original_kwh=original_daily_kwh,
                corrected_kwh=corrected_daily_kwh,
                reason="Updated weather data",
                hours_corrected=updated_hours,
                am_actual=morning_actual_kwh,
                am_predicted=morning_predicted_kwh,
            )
            _LOGGER.info("Notification sent")
        except Exception as error:
            _LOGGER.error("Error sending notification: %s", error)
        return None if True else None
