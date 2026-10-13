# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Morning Routine Handler for Solar Forecast Energy AI.

Implements bulletproof morning routine with retry logic
for creating daily predictions using database operations.

@author zara
"""
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..astronomy_provider import AstronomyProviderAdapter

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
    from ..coordinator import SolarForecastEAICoordinator
from ..data.data_shadow_detection import ShadowDetector

_LOGGER = logging.getLogger(__name__)


class MorningRoutineHandler:
    """Bulletproof morning routine with retry logic. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: "DatabaseManager",
        coordinator: "SolarForecastEAICoordinator",
        astronomy_provider: AstronomyProviderAdapter,
    ):
        """Initialize the morning routine handler. @zara"""
        self.hass = hass
        self.db = db_manager
        self.coordinator = coordinator
        self.astronomy_provider = astronomy_provider
        self._transaction_started = False
        self._shadow_detector = ShadowDetector(hass, db_manager)

    async def execute_morning_routine_with_retry(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_hourly: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
    ) -> bool:
        """
        Execute morning routine with bulletproof retry logic.

        Returns:
            True if successful, False if all retries failed. @zara
        """
        if self.db is None:
            _LOGGER.error("Database not available for morning routine")
            return False
        max_attempts = 3
        try:
            for attempt in range(1, max_attempts + 1):
                _LOGGER.info("")
                _LOGGER.info(
                    "================================================================================"
                )
                _LOGGER.info(
                    "  MORNING ROUTINE EXECUTION - Attempt %d/%d for %s",
                    attempt,
                    max_attempts,
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
                    inverter_cap_kw = getattr(
                        self.coordinator, "inverter_max_power", 0.0
                    )
                    if inverter_cap_kw > 0:
                        _LOGGER.info(
                            "  ℹ Inverter capping enabled: %.2f kW", inverter_cap_kw
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
                        inverter_max_power=inverter_cap_kw,
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
                        "  Date: %s | Attempt: %d/%d", date, attempt, max_attempts
                    )
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("")
                    return True
                except Exception as error:
                    _LOGGER.error(
                        "Morning routine attempt %d/%d FAILED: %s",
                        attempt,
                        max_attempts,
                        error,
                        exc_info=True,
                    )
                    if attempt < max_attempts:
                        retry_delay_seconds = 60 * attempt
                        _LOGGER.info(
                            "Waiting %ds before retry %d...",
                            retry_delay_seconds,
                            attempt + 1,
                        )
                        await asyncio.sleep(retry_delay_seconds)
                    else:
                        _LOGGER.error(
                            "CRITICAL: Morning routine FAILED after %d attempts! Manual intervention required",
                            max_attempts,
                        )
                        await self._send_critical_alert(
                            "Morning routine failure", str(error)
                        )
                        return False
            return False
        except Exception as error:
            _LOGGER.error(
                "CRITICAL: Morning routine handler crashed: %s", error, exc_info=True
            )
            return False

    async def _create_daily_predictions(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_forecast: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
        inverter_max_power: float = 0.0,
    ) -> set:
        """Create daily predictions in database. @zara

        Morning Routine UNLOCKS, updates predictions, and RE-LOCKS.
        This allows multiple runs per day (00:30 + sunrise-based).
        Raises on error so retry logic in caller can handle it.
        """
        if False:
            unused_layout_slot = None
        _LOGGER.info("  ├─ Checking and unlocking existing forecast locks...")
        existing_count_row = await self.db.fetchone(
            "SELECT COUNT(*) as count FROM hourly_predictions WHERE target_date = ?",
            (date,),
        )
        existing_count = existing_count_row[0] if existing_count_row else 0
        if existing_count > 0:
            _LOGGER.info(
                "  │  🔓 Found %d existing predictions - will update", existing_count
            )
        forecast_lock = await self.db.fetchone(
            "SELECT locked, locked_at, source FROM daily_forecasts\n               WHERE forecast_type = 'today' AND forecast_date = ?",
            (date,),
        )
        if forecast_lock and forecast_lock[0]:
            _LOGGER.info(
                "  │  🔓 Unlocking daily_forecast (was locked at %s by %s)",
                forecast_lock[1],
                forecast_lock[2],
            )
            await self.db.execute(
                "UPDATE daily_forecasts SET locked = 0\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
        _LOGGER.info("  ├─ Preparing to update predictions for 3 days...")
        from datetime import timedelta as timedelta

        forecast_start_date = datetime.fromisoformat(date).date()
        incoming_prediction_ids = set()
        for hour_forecast in hourly_forecast:
            forecast_hour = hour_forecast.get("hour")
            forecast_date = hour_forecast.get("date")
            if forecast_hour is not None and forecast_date:
                incoming_prediction_ids.add(f"{forecast_date}_{forecast_hour:02d}")
        preserved_hourly_actuals = {}
        preserved_group_actuals = {}
        preserved_sensor_actuals = {}
        preserved_weather_actuals = {}
        if incoming_prediction_ids:
            id_placeholders = ",".join(["?"] * len(incoming_prediction_ids))
            id_parameters = tuple(incoming_prediction_ids)
            hourly_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, actual_kwh, has_sensor_data\n                    FROM hourly_predictions\n                    WHERE prediction_id IN ({id_placeholders}) AND actual_kwh IS NOT NULL",
                id_parameters,
            )
            for row in hourly_actual_rows:
                preserved_hourly_actuals[row[0]] = {
                    "actual_kwh": row[1],
                    "has_sensor_data": row[2],
                }
            group_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, group_name, actual_kwh\n                    FROM prediction_panel_groups\n                    WHERE prediction_id IN ({id_placeholders}) AND actual_kwh IS NOT NULL",
                id_parameters,
            )
            for row in group_actual_rows:
                preserved_group_actuals[row[0], row[1]] = row[2]
            sensor_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, temperature_c, humidity_percent,\n                       solar_radiation_wm2, rain_mm, wind_speed_ms, current_yield_kwh\n                    FROM prediction_sensor_actual\n                    WHERE prediction_id IN ({id_placeholders})",
                id_parameters,
            )
            for row in sensor_actual_rows:
                preserved_sensor_actuals[row[0]] = {
                    "temperature_c": row[1],
                    "humidity_percent": row[2],
                    "solar_radiation_wm2": row[3],
                    "rain_mm": row[4],
                    "wind_speed_ms": row[5],
                    "current_yield_kwh": row[6],
                }
            weather_actual_rows = await self.db.fetchall(
                f"SELECT prediction_id, temperature, solar_radiation_wm2, wind,\n                       humidity, rain, clouds, pressure, lux,\n                       frost_detected, frost_score, frost_confidence\n                    FROM prediction_weather\n                    WHERE prediction_id IN ({id_placeholders}) AND weather_type = 'actual'",
                id_parameters,
            )
            for row in weather_actual_rows:
                preserved_weather_actuals[row[0]] = {
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
            if preserved_hourly_actuals:
                _LOGGER.info(
                    "  │  Preserved %d hourly actuals, %d group actuals before re-creation",
                    len(preserved_hourly_actuals),
                    len(preserved_group_actuals),
                )
            await self.db.execute(
                f"DELETE FROM hourly_predictions WHERE prediction_id IN ({id_placeholders})",
                id_parameters,
            )
            _LOGGER.info(
                "  │  Cleared %d existing prediction slots for update",
                len(incoming_prediction_ids),
            )
        else:
            _LOGGER.warning("  │  No incoming predictions to update - skipping DELETE")
        created_at = dt_util.now()
        weather_by_date_hour = {}
        for weather_hour in weather_forecast:
            weather_key = (weather_hour.get("date", date), weather_hour.get("hour"))
            weather_by_date_hour[weather_key] = weather_hour
        _LOGGER.info(
            "  ├─ Processing %d input forecast entries (3 days)...",
            len(hourly_forecast),
        )
        created_prediction_ids = set()
        duplicate_count = 0
        hour_counts_by_date = {}
        for hour_forecast in hourly_forecast:
            forecast_hour = hour_forecast.get("hour")
            if forecast_hour is None:
                continue
            forecast_date = hour_forecast.get("date")
            if not forecast_date:
                forecast_date = date
                _LOGGER.warning(
                    "  │  ⚠️  Missing date in hour_data for hour %d, using %s",
                    forecast_hour,
                    date,
                )
            prediction_id = f"{forecast_date}_{forecast_hour:02d}"
            if prediction_id in created_prediction_ids:
                duplicate_count += 1
                continue
            created_prediction_ids.add(prediction_id)
            hour_counts_by_date[forecast_date] = (
                hour_counts_by_date.get(forecast_date, 0) + 1
            )
            prediction_kwh = hour_forecast.get("production_kwh", 0.0)
            if inverter_max_power > 0:
                production_cap_kwh = inverter_max_power
                uncapped_prediction_kwh = prediction_kwh
                prediction_kwh = min(prediction_kwh, production_cap_kwh)
            else:
                uncapped_prediction_kwh = prediction_kwh
            target_datetime = f"{forecast_date}T{forecast_hour:02d}:00:00"
            target_date = datetime.fromisoformat(forecast_date)
            await self.db.execute(
                "INSERT OR REPLACE INTO hourly_predictions\n                   (prediction_id, prediction_created_at, prediction_created_hour,\n                    target_datetime, target_date, target_hour,\n                    target_day_of_week, target_day_of_year, target_month, target_season,\n                    prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                    ml_contribution_percent, confidence, is_production_hour,\n                    physics_kwh, ai_kwh, ai_confidence,\n                    lstm_kwh, ridge_kwh)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prediction_id,
                    created_at,
                    created_at.hour,
                    target_datetime,
                    forecast_date,
                    forecast_hour,
                    target_date.weekday(),
                    target_date.timetuple().tm_yday,
                    target_date.month,
                    get_season(target_date.month),
                    round(prediction_kwh, 4),
                    round(uncapped_prediction_kwh, 4),
                    hour_forecast.get("method", "physics"),
                    hour_forecast.get("ml_contribution", 0),
                    hour_forecast.get("confidence", 75.0),
                    6 <= forecast_hour <= 20,
                    hour_forecast.get("physics_kwh"),
                    hour_forecast.get("ai_kwh"),
                    hour_forecast.get("ai_confidence"),
                    hour_forecast.get("lstm_kwh"),
                    hour_forecast.get("ridge_kwh"),
                ),
            )
            panel_group_forecasts = hour_forecast.get("groups")
            if panel_group_forecasts:
                for group_forecast in panel_group_forecasts:
                    group_name = group_forecast.get("name")
                    group_prediction_kwh = group_forecast.get("power_kwh", 0.0)
                    group_physics_kwh = group_forecast.get("physics_kwh")
                    group_ai_kwh = group_forecast.get("ai_kwh")
                    group_lstm_kwh = group_forecast.get("lstm_kwh")
                    group_ridge_kwh = group_forecast.get("ridge_kwh")
                    await self.db.execute(
                        "INSERT INTO prediction_panel_groups\n                           (prediction_id, group_name, prediction_kwh, physics_kwh, ai_kwh,\n                            lstm_kwh, ridge_kwh)\n                           VALUES (?, ?, ?, ?, ?, ?, ?)\n                           ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                               prediction_kwh = excluded.prediction_kwh,\n                               physics_kwh = excluded.physics_kwh,\n                               ai_kwh = excluded.ai_kwh,\n                               lstm_kwh = excluded.lstm_kwh,\n                               ridge_kwh = excluded.ridge_kwh",
                        (
                            prediction_id,
                            group_name,
                            round(group_prediction_kwh, 4),
                            round(group_physics_kwh, 4) if group_physics_kwh else None,
                            round(group_ai_kwh, 4) if group_ai_kwh else None,
                            round(group_lstm_kwh, 4) if group_lstm_kwh else None,
                            round(group_ridge_kwh, 4) if group_ridge_kwh else None,
                        ),
                    )
                await self.db.execute(
                    "UPDATE hourly_predictions SET has_panel_group_predictions = TRUE\n                       WHERE prediction_id = ?",
                    (prediction_id,),
                )
            weather_forecast_hour = weather_by_date_hour.get(
                (forecast_date, forecast_hour), {}
            )
            if weather_forecast_hour:
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, source, diffuse_radiation,\n                        direct_radiation)\n                       VALUES (?, 'forecast', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        prediction_id,
                        weather_forecast_hour.get("temperature"),
                        weather_forecast_hour.get("solar_radiation_wm2"),
                        weather_forecast_hour.get("wind"),
                        weather_forecast_hour.get("humidity"),
                        weather_forecast_hour.get("rain"),
                        weather_forecast_hour.get("clouds"),
                        weather_forecast_hour.get("pressure"),
                        weather_forecast_hour.get("source", "blended"),
                        weather_forecast_hour.get("diffuse_radiation")
                        or weather_forecast_hour.get("dhi"),
                        weather_forecast_hour.get("direct_radiation"),
                    ),
                )
            astronomy_hour = astronomy_data.get(
                f"{forecast_date}_{forecast_hour:02d}", {}
            )
            if not astronomy_hour:
                astronomy_hour = astronomy_data.get(str(forecast_hour), {})
            astronomy_day = astronomy_data.get(forecast_date, astronomy_data)
            if astronomy_hour:
                sunrise = astronomy_day.get("sunrise_local")
                sunset = astronomy_day.get("sunset_local")
                solar_noon = astronomy_day.get("solar_noon_local")
                daylight_hours = astronomy_day.get("daylight_hours", 12.0)
                hours_since_solar_noon = None
                day_progress_ratio = None
                hours_after_sunrise = None
                hours_before_sunset = None
                try:
                    from datetime import datetime as datetime_type

                    hour_midpoint = datetime_type(
                        target_date.year,
                        target_date.month,
                        target_date.day,
                        forecast_hour,
                        30,
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
                        for timestamp_format in ("%H:%M:%S", "%H:%M"):
                            try:
                                parsed_time = datetime_type.strptime(
                                    timestamp_text, timestamp_format
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
                        elapsed_daylight_seconds = (
                            hour_midpoint - sunrise_datetime
                        ).total_seconds()
                        day_progress_ratio = (
                            max(
                                0.0,
                                min(1.0, elapsed_daylight_seconds / daylight_seconds),
                            )
                            if daylight_seconds > 0
                            else 0.5
                        )
                        hours_after_sunrise = max(
                            0.0, elapsed_daylight_seconds / 3600.0
                        )
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
                        astronomy_hour.get("sun_elevation_deg"),
                        astronomy_hour.get("sun_azimuth_deg"),
                        astronomy_hour.get("clear_sky_radiation_wm2"),
                        astronomy_hour.get("theoretical_max_kwh"),
                        hours_since_solar_noon,
                        day_progress_ratio,
                        hours_after_sunrise,
                        hours_before_sunset,
                    ),
                )
                continue
        restored_hourly_actuals = 0
        if preserved_hourly_actuals:
            for (
                restored_prediction_id,
                preserved_actual,
            ) in preserved_hourly_actuals.items():
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET actual_kwh = ?, has_sensor_data = ?\n                       WHERE prediction_id = ?",
                    (
                        preserved_actual["actual_kwh"],
                        preserved_actual["has_sensor_data"],
                        restored_prediction_id,
                    ),
                )
                restored_hourly_actuals += 1
        if preserved_group_actuals:
            for (
                restored_prediction_id,
                group_name,
            ), group_actual_kwh in preserved_group_actuals.items():
                await self.db.execute(
                    "UPDATE prediction_panel_groups\n                       SET actual_kwh = ?\n                       WHERE prediction_id = ? AND group_name = ?",
                    (group_actual_kwh, restored_prediction_id, group_name),
                )
        if preserved_sensor_actuals:
            for (
                restored_prediction_id,
                preserved_measurements,
            ) in preserved_sensor_actuals.items():
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms, current_yield_kwh)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms,\n                           current_yield_kwh = excluded.current_yield_kwh",
                    (
                        restored_prediction_id,
                        preserved_measurements["temperature_c"],
                        preserved_measurements["humidity_percent"],
                        preserved_measurements["solar_radiation_wm2"],
                        preserved_measurements["rain_mm"],
                        preserved_measurements["wind_speed_ms"],
                        preserved_measurements["current_yield_kwh"],
                    ),
                )
        if preserved_weather_actuals:
            for (
                restored_prediction_id,
                preserved_measurements,
            ) in preserved_weather_actuals.items():
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, lux,\n                        frost_detected, frost_score, frost_confidence)\n                       VALUES (?, 'actual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           wind = excluded.wind,\n                           humidity = excluded.humidity,\n                           rain = excluded.rain,\n                           clouds = excluded.clouds,\n                           pressure = excluded.pressure,\n                           lux = excluded.lux,\n                           frost_detected = excluded.frost_detected,\n                           frost_score = excluded.frost_score,\n                           frost_confidence = excluded.frost_confidence",
                    (
                        restored_prediction_id,
                        preserved_measurements["temperature"],
                        preserved_measurements["solar_radiation_wm2"],
                        preserved_measurements["wind"],
                        preserved_measurements["humidity"],
                        preserved_measurements["rain"],
                        preserved_measurements["clouds"],
                        preserved_measurements["pressure"],
                        preserved_measurements["lux"],
                        preserved_measurements["frost_detected"],
                        preserved_measurements["frost_score"],
                        preserved_measurements["frost_confidence"],
                    ),
                )
        if restored_hourly_actuals > 0:
            _LOGGER.info(
                "  ├─ Restored %d hourly actuals, %d group actuals, %d sensor, %d weather actuals",
                restored_hourly_actuals,
                len(preserved_group_actuals),
                len(preserved_sensor_actuals),
                len(preserved_weather_actuals),
            )
        _LOGGER.info(
            "  ├─ Calculating daily totals from saved predictions for 3 days..."
        )
        daily_totals_kwh = {}
        for day_offset in range(3):
            day_iso = (forecast_start_date + timedelta(days=day_offset)).isoformat()
            daily_total_row = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) as total FROM hourly_predictions WHERE target_date = ?",
                (day_iso,),
            )
            daily_total_kwh = (
                daily_total_row["total"]
                if daily_total_row and daily_total_row["total"]
                else 0.0
            )
            daily_totals_kwh[day_iso] = daily_total_kwh
            _LOGGER.info("  │  → %s: %.2f kWh", day_iso, daily_total_kwh)
        _LOGGER.info("  ├─ Saving daily forecasts to database for 3 days...")
        forecast_types = ["today", "tomorrow", "day_after_tomorrow"]
        for day_offset, forecast_type in enumerate(forecast_types):
            day_iso = (forecast_start_date + timedelta(days=day_offset)).isoformat()
            daily_total_kwh = daily_totals_kwh.get(day_iso, 0.0)
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                    locked, locked_at, source, created_at)\n                   VALUES (?, ?, ?, ?, TRUE, ?, 'morning_routine', ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = excluded.prediction_kwh,\n                       prediction_kwh_raw = excluded.prediction_kwh_raw,\n                       locked = excluded.locked,\n                       locked_at = excluded.locked_at,\n                       source = excluded.source",
                (
                    forecast_type,
                    day_iso,
                    round(daily_total_kwh, 2),
                    round(daily_total_kwh, 2),
                    created_at,
                    created_at,
                ),
            )
        _LOGGER.debug(
            "     → Daily forecasts LOCKED for 3 days: locked=TRUE, locked_at=%s",
            created_at,
        )
        daily_total_kwh = daily_totals_kwh.get(date, 0.0)
        _LOGGER.info("  ├─ Updating coordinator expected daily production...")
        await self.coordinator.data_manager.save_expected_daily_production(
            round(daily_total_kwh, 2)
        )
        self.coordinator.expected_daily_production = round(daily_total_kwh, 2)
        _LOGGER.info("  ├─ Setting morning routine lock for %s...", date)
        await self.db.execute(
            "UPDATE coordinator_state\n               SET last_set_date = ?, last_updated = ?\n               WHERE id = 1",
            (date, created_at),
        )
        _LOGGER.debug("     → Lock flag set: last_set_date = %s", date)
        created_count = len(created_prediction_ids)
        _LOGGER.info("  └─ SUMMARY:")
        _LOGGER.info("     • Input entries: %d (3 days)", len(hourly_forecast))
        _LOGGER.info("     • Created predictions: %d", created_count)
        if duplicate_count > 0:
            _LOGGER.info("     • Skipped duplicates: %d", duplicate_count)
        _LOGGER.info("     • Predictions per day:")
        for forecast_date, hour_count in sorted(hour_counts_by_date.items()):
            day_total_kwh = daily_totals_kwh.get(forecast_date, 0.0)
            _LOGGER.info(
                "       - %s: %d hours, %.2f kWh",
                forecast_date,
                hour_count,
                day_total_kwh,
            )
        _LOGGER.info(
            "     • Coordinator updated: Yes (today=%.2f kWh)", daily_total_kwh
        )
        return set(hour_counts_by_date.keys())

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
                from datetime import timedelta as timedelta

                forecast_start_date = datetime.fromisoformat(date).date()
                dates_to_verify = [
                    (forecast_start_date + timedelta(days=day_offset)).isoformat()
                    for day_offset in range(3)
                ]
            for date_to_verify in dates_to_verify:
                row = await self.db.fetchone(
                    "SELECT COUNT(*) FROM hourly_predictions\n                       WHERE target_date = ?",
                    (date_to_verify,),
                )
                if not row or row[0] == 0:
                    _LOGGER.error(
                        "No predictions found for %s after creation!", date_to_verify
                    )
                    return False
                duplicate_row = await self.db.fetchone(
                    "SELECT prediction_id, COUNT(*) as cnt\n                       FROM hourly_predictions\n                       WHERE target_date = ?\n                       GROUP BY prediction_id\n                       HAVING cnt > 1",
                    (date_to_verify,),
                )
                if duplicate_row:
                    _LOGGER.error(
                        "Duplicate prediction IDs found for %s: %s",
                        date_to_verify,
                        duplicate_row[0],
                    )
                    return False
            _LOGGER.debug(
                "Integrity verified for %d days starting from %s, no duplicates",
                len(dates_to_verify),
                dates_to_verify[0] if dates_to_verify else date,
            )
        except Exception as error:
            _LOGGER.error("Integrity verification failed: %s", error, exc_info=True)
            return False
        return True

    async def _send_critical_alert(self, title: str, details: str) -> None:
        """Send critical alert about failure. @zara"""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": f"Solar Forecast Energy AI: {title}",
                    "message": f"{details}\n\nCheck logs for details.",
                    "notification_id": "solar_forecast_eai_critical",
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
    ) -> bool:
        "Update hourly actual production value with MPPT/clipping/alert detection.\n\nArgs:\n    date: Date string (YYYY-MM-DD)\n    hour: Hour of the day (0-23)\n    actual_kwh: Total actual production for this hour\n    sensor_data: Sensor readings (temperature, humidity, etc.)\n    weather_actual: Actual weather data\n    panel_group_actuals: Dict mapping group_name -> actual_kwh per group\n    inverter_max_power: Max AC power in W for clipping detection\n    zero_export_mode: Whether zero export is active\n    has_battery: Whether system has battery storage\n    solar_to_battery_power: Power flowing to battery in W\n\n@zara\n"
        try:
            if self.db is None:
                _LOGGER.warning("Database not available for updating hourly actual")
                if True:
                    return False
            prediction_id = f"{date}_{hour:02d}"
            measured_at = dt_util.now()
            row = await self.db.fetchone(
                "SELECT prediction_kwh FROM hourly_predictions WHERE prediction_id = ?",
                (prediction_id,),
            )
            if not row:
                _LOGGER.warning(
                    "No prediction found for %s hour %d to update", date, hour
                )
                if True:
                    return False
            prediction_kwh = row[0] or 0.0
            error_kwh = actual_kwh - prediction_kwh
            error_percent = (
                error_kwh / prediction_kwh * 100 if prediction_kwh > 0 else 0
            )
            accuracy_percent = 100 - abs(error_percent)
            sensor_data_complete = self._check_sensor_completeness(sensor_data)
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
            inverter_clipped = False
            if inverter_max_power > 0:
                inverter_clipped = actual_kwh >= inverter_max_power * 0.95
            has_weather_alert, weather_alert_type = self._detect_weather_alert(
                weather_actual, forecast_weather
            )
            exclude_from_learning = inverter_clipped or has_weather_alert
            mppt_throttled = False
            mppt_throttle_reason = None
            if not exclude_from_learning:
                if getattr(self.coordinator, "learning_paused", False):
                    exclude_from_learning = True
                    has_weather_alert = True
                    weather_alert_type = "manual_pause"
                    _LOGGER.debug(
                        "Hour %s excluded: learning paused by user", prediction_id
                    )
            await self.db.execute(
                "UPDATE hourly_predictions\n                   SET actual_kwh = ?,\n                       actual_measured_at = ?,\n                       accuracy_percent = ?,\n                       error_kwh = ?,\n                       error_percent = ?,\n                       has_sensor_data = TRUE,\n                       sensor_data_complete = ?,\n                       inverter_clipped = ?,\n                       exclude_from_learning = ?,\n                       mppt_throttled = ?,\n                       mppt_throttle_reason = ?,\n                       has_weather_alert = ?,\n                       weather_alert_type = ?\n                   WHERE prediction_id = ?",
                (
                    actual_kwh,
                    measured_at,
                    accuracy_percent,
                    error_kwh,
                    error_percent,
                    sensor_data_complete,
                    inverter_clipped,
                    exclude_from_learning,
                    mppt_throttled,
                    mppt_throttle_reason,
                    has_weather_alert,
                    weather_alert_type,
                    prediction_id,
                ),
            )
            if sensor_data:
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms, current_yield_kwh)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms,\n                           current_yield_kwh = excluded.current_yield_kwh",
                    (
                        prediction_id,
                        sensor_data.get("temperature_c"),
                        sensor_data.get("humidity_percent"),
                        sensor_data.get("solar_radiation_wm2"),
                        sensor_data.get("rain_mm"),
                        sensor_data.get("wind_speed_ms"),
                        sensor_data.get("current_yield_kwh"),
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
            if panel_group_actuals:
                updated_group_count = 0
                for group_name, group_actual_kwh in panel_group_actuals.items():
                    if group_actual_kwh is not None:
                        await self.db.execute(
                            "UPDATE prediction_panel_groups\n                               SET actual_kwh = ?\n                               WHERE prediction_id = ? AND group_name = ?",
                            (group_actual_kwh, prediction_id, group_name),
                        )
                        updated_group_count += 1
                if updated_group_count > 0:
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                           SET has_panel_group_actuals = TRUE\n                           WHERE prediction_id = ?",
                        (prediction_id,),
                    )
                    _LOGGER.debug(
                        "Panel group actuals updated for %s: %d groups",
                        prediction_id,
                        updated_group_count,
                    )
            try:
                shadow_inputs = {
                    "actual_kwh": actual_kwh,
                    "astronomy": hour_astronomy,
                    "sensor_actual": sensor_data,
                    "weather_actual": weather_actual,
                }
                shadow_result = await self._shadow_detector.detect_shadow_ensemble(
                    shadow_inputs
                )
                if shadow_result:
                    await self.db.save_hourly_shadow_detection(
                        prediction_id, shadow_result
                    )
                    _LOGGER.debug(
                        "Shadow detection for %s: %s (%.1f%%)",
                        prediction_id,
                        shadow_result.get("shadow_type", "unknown"),
                        shadow_result.get("shadow_percent", 0),
                    )
            except Exception as shadow_error:
                _LOGGER.debug(
                    "Shadow detection skipped for %s: %s", prediction_id, shadow_error
                )
            if True:
                return True
        except Exception as error:
            _LOGGER.error("Error updating hourly actual: %s", error, exc_info=True)
            if True:
                return False

    async def _get_astronomy_for_hour(self, date: str, hour: int) -> Dict[str, Any]:
        """Get a specific hour from the read-only SFML astronomy provider."""
        try:
            astronomy_days = await self.astronomy_provider.async_get_legacy_days(
                datetime.fromisoformat(date).date()
            )
            astronomy_day = astronomy_days.get(date, {})
            astronomy_hour = astronomy_days.get(f"{date}_{hour:02d}", {})
            if not astronomy_hour or not astronomy_day:
                return {}
            return {
                **astronomy_hour,
                "is_daylight": (astronomy_hour.get("sun_elevation_deg") or 0) > 0,
                "theoretical_kwh": astronomy_hour.get("theoretical_max_kwh"),
                "sunrise": astronomy_day["sunrise_local"],
                "sunset": astronomy_day["sunset_local"],
                "solar_noon": astronomy_day["solar_noon_local"],
                "daylight_hours": astronomy_day["daylight_hours"],
            }
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
            (1 for sensor_value in sensor_data.values() if sensor_value is not None)
        )
        return valid_sensor_count >= 3

    @staticmethod
    def _detect_weather_alert(
        weather_actual: Optional[Dict[str, Any]],
        weather_forecast: Optional[Dict[str, Any]],
    ) -> Tuple[bool, Optional[str]]:
        'Detect unexpected weather conditions that invalidate learning data. @zara\n\nReturns:\n    Tuple of (has_alert, alert_type)\n\nAlert types:\n- "snow_covered_panels": Physical panel coverage detected\n- "unexpected_snow": Actual snow not in forecast\n- "unexpected_rain": Actual rain not in forecast\n- "unexpected_clouds": Actual radiation far below forecast\n'
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
