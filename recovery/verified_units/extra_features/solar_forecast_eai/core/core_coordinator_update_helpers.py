# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Coordinator Update Helpers.
Provides helper methods for coordinator data updates and
consumption forecast generation.

@zara
"""
import logging
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple
from homeassistant.helpers.update_coordinator import UpdateFailed
from .core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager
from ..const import (
    DATA_KEY_FORECAST_TODAY,
    DATA_KEY_FORECAST_TOMORROW,
    DATA_KEY_FORECAST_DAY_AFTER,
    DATA_KEY_HOURLY_FORECAST,
    DATA_KEY_CURRENT_WEATHER,
    DATA_KEY_EXTERNAL_SENSORS,
    DATA_KEY_HEATING_TIME,
    DATA_KEY_PEAK_TODAY,
    DATA_KEY_CONSUMPTION_TODAY,
    DATA_KEY_EXPECTED_DAILY_CONSUMPTION,
    DATA_KEY_STATISTICS,
    HEATING_TIME_ACTIVE,
    HEATING_TIME_DURATION_SECONDS,
    HEATING_TIME_START_TIME,
    HEATING_TIME_END_TIME,
    PEAK_TODAY_POWER_W,
    PEAK_TODAY_AT,
    CONSUMPTION_TODAY_KWH,
    CONSUMPTION_TODAY_SENSOR,
    FORECAST_KEY_TODAY,
    FORECAST_KEY_TOMORROW,
    FORECAST_KEY_DAY_AFTER,
    FORECAST_KEY_HOURLY,
    FORECAST_KEY_METHOD,
    EXT_SENSOR_WP_ENERGY_TODAY,
    STATS_ALL_TIME_PEAK,
    STATS_LAST_7_DAYS,
    STATS_LAST_30_DAYS,
    STATS_AVG_ACCURACY,
    STATS_CONSUMPTION_KWH,
    STATS_AVG_CONSUMPTION_KWH,
    STATS_CURRENT_MONTH,
    STATS_CURRENT_WEEK,
)

_LOGGER = logging.getLogger(__name__)


class CoordinatorUpdateHelpers:
    """Helper methods for coordinator data updates. @zara"""

    def __init__(self, coordinator: Any, db_manager: DatabaseManager):
        self.coordinator = coordinator
        self.db = db_manager

    async def fetch_weather_data(self) -> Tuple[Optional[Dict], Optional[list]]:
        """Fetch current weather and hourly forecast. @zara"""
        current_weather = None
        hourly_weather = None
        if self.coordinator.weather_service:
            try:
                current_weather = (
                    await self.coordinator.weather_service.get_current_weather()
                )
                hourly_weather = (
                    await self.coordinator.weather_service.get_corrected_hourly_forecast()
                )
                self.coordinator._last_weather_update = dt_util.now()
                if not hourly_weather or len(hourly_weather) == 0:
                    _LOGGER.warning("Weather service returned no hourly forecast data")
            except Exception as error:
                _LOGGER.error(f"Error fetching weather data: {error}", exc_info=True)
        return (current_weather, hourly_weather) if True else None

    async def generate_forecast(
        self,
        current_weather: Optional[Dict],
        hourly_forecast: Optional[list],
        external_sensors: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate forecast using orchestrator. @zara"""
        forecast = await self.coordinator.forecast_orchestrator.orchestrate_forecast(
            current_weather=current_weather,
            hourly_forecast=hourly_forecast,
            external_sensors=external_sensors,
            ml_prediction_today=None,
            ml_prediction_tomorrow=None,
            correction_factor=self.coordinator.learned_correction_factor,
        )
        if not forecast:
            raise UpdateFailed("Forecast generation failed")
        hourly_count = len(forecast.get(FORECAST_KEY_HOURLY, []))
        _LOGGER.debug(
            f"Forecast generated: today={forecast.get(FORECAST_KEY_TODAY, 'N/A')} kWh, tomorrow={forecast.get(FORECAST_KEY_TOMORROW, 'N/A')} kWh, method={forecast.get(FORECAST_KEY_METHOD, 'unknown')}, hourly_entries={hourly_count}"
        )
        return forecast

    async def build_coordinator_result(
        self,
        forecast: Dict[str, Any],
        current_weather: Optional[Dict],
        external_sensors: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build coordinator result dictionary. @zara"""
        statistics = await self._load_statistics()
        peak_power_w = getattr(self.coordinator, "_peak_power_today", 0.0)
        peak_time = getattr(self.coordinator, "_peak_time_today", None)
        if peak_power_w == 0.0:
            stored_peak = await self._get_today_peak()
            if stored_peak:
                peak_power_w = stored_peak.get("power_w", 0.0)
                peak_time = stored_peak.get("time")
        result = {
            DATA_KEY_FORECAST_TODAY: forecast.get(FORECAST_KEY_TODAY),
            DATA_KEY_FORECAST_TOMORROW: forecast.get(FORECAST_KEY_TOMORROW),
            DATA_KEY_FORECAST_DAY_AFTER: forecast.get(FORECAST_KEY_DAY_AFTER),
            DATA_KEY_HOURLY_FORECAST: (
                forecast.get(FORECAST_KEY_HOURLY, [])
                if self.coordinator.enable_hourly
                else []
            ),
            DATA_KEY_CURRENT_WEATHER: current_weather,
            DATA_KEY_EXTERNAL_SENSORS: external_sensors,
            DATA_KEY_HEATING_TIME: {
                HEATING_TIME_ACTIVE: self.coordinator.consumption_tracker.is_active,
                HEATING_TIME_DURATION_SECONDS: self.coordinator.consumption_tracker.total_seconds,
                HEATING_TIME_START_TIME: self.coordinator.consumption_tracker.start_time,
                HEATING_TIME_END_TIME: self.coordinator.consumption_tracker.end_time,
            },
            DATA_KEY_PEAK_TODAY: {
                PEAK_TODAY_POWER_W: peak_power_w,
                PEAK_TODAY_AT: peak_time,
            },
            DATA_KEY_CONSUMPTION_TODAY: {
                CONSUMPTION_TODAY_KWH: external_sensors.get(EXT_SENSOR_WP_ENERGY_TODAY),
                CONSUMPTION_TODAY_SENSOR: self.coordinator.wp_energy_today,
            },
            DATA_KEY_EXPECTED_DAILY_CONSUMPTION: self.coordinator.expected_daily_consumption,
            DATA_KEY_STATISTICS: statistics,
        }
        return result

    async def _load_statistics(self) -> Dict[str, Any]:
        """Load statistics from database. @zara"""
        statistics = {
            STATS_ALL_TIME_PEAK: {},
            STATS_LAST_7_DAYS: {},
            STATS_LAST_30_DAYS: {},
            STATS_CURRENT_MONTH: {},
            STATS_CURRENT_WEEK: {},
        }
        try:
            all_time_peak_row = await self.db.fetchone(
                "SELECT peak_record_w, peak_record_date, peak_record_time\n                   FROM production_time_state\n                   WHERE id = 1 AND peak_record_w IS NOT NULL"
            )
            if all_time_peak_row and all_time_peak_row[0]:
                statistics[STATS_ALL_TIME_PEAK] = {
                    PEAK_TODAY_POWER_W: float(all_time_peak_row[0]),
                    PEAK_TODAY_AT: (
                        f"{all_time_peak_row[1]} {all_time_peak_row[2]}"
                        if all_time_peak_row[1] and all_time_peak_row[2]
                        else None
                    ),
                    "date": all_time_peak_row[1],
                }
            weekly_stats_row = await self.db.fetchone(
                "SELECT AVG(actual_total_kwh), AVG(accuracy_percent), SUM(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE date >= DATE('now', '-7 days') AND actual_total_kwh IS NOT NULL"
            )
            if weekly_stats_row:
                statistics[STATS_LAST_7_DAYS] = {
                    STATS_AVG_CONSUMPTION_KWH: (
                        float(weekly_stats_row[0]) if weekly_stats_row[0] else None
                    ),
                    STATS_AVG_ACCURACY: (
                        float(weekly_stats_row[1]) if weekly_stats_row[1] else None
                    ),
                    STATS_CONSUMPTION_KWH: (
                        float(weekly_stats_row[2]) if weekly_stats_row[2] else None
                    ),
                }
            monthly_stats_row = await self.db.fetchone(
                "SELECT AVG(actual_total_kwh), AVG(accuracy_percent), SUM(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE date >= DATE('now', '-30 days') AND actual_total_kwh IS NOT NULL"
            )
            if monthly_stats_row:
                statistics[STATS_LAST_30_DAYS] = {
                    STATS_AVG_CONSUMPTION_KWH: (
                        float(monthly_stats_row[0]) if monthly_stats_row[0] else None
                    ),
                    STATS_AVG_ACCURACY: (
                        float(monthly_stats_row[1]) if monthly_stats_row[1] else None
                    ),
                    STATS_CONSUMPTION_KWH: (
                        float(monthly_stats_row[2]) if monthly_stats_row[2] else None
                    ),
                }
            month_start = dt_util.now().date().replace(day=1).isoformat()
            month_total_row = await self.db.fetchone(
                "SELECT SUM(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (month_start,),
            )
            statistics[STATS_CURRENT_MONTH] = {
                STATS_CONSUMPTION_KWH: (
                    float(month_total_row[0])
                    if month_total_row and month_total_row[0]
                    else 0.0
                )
            }
            week_start = (
                dt_util.now().date() - timedelta(days=dt_util.now().date().weekday())
            ).isoformat()
            week_total_row = await self.db.fetchone(
                "SELECT SUM(actual_total_kwh)\n                   FROM daily_summaries\n                   WHERE date >= ? AND actual_total_kwh IS NOT NULL",
                (week_start,),
            )
            statistics[STATS_CURRENT_WEEK] = {
                STATS_CONSUMPTION_KWH: (
                    float(week_total_row[0])
                    if week_total_row and week_total_row[0]
                    else 0.0
                )
            }
        except Exception as error:
            _LOGGER.warning(f"Failed to load statistics: {error}")
        return statistics if True else None

    async def _get_today_peak(self) -> Optional[Dict[str, Any]]:
        """Get today's peak power from production_time_state. @zara"""
        try:
            today = dt_util.now().date().isoformat()
            today_peak_row = await self.db.fetchone(
                "SELECT peak_power_w, peak_power_time\n                   FROM production_time_state\n                   WHERE id = 1 AND date = ?",
                (today,),
            )
            if today_peak_row and today_peak_row[0]:
                return (
                    None
                    if False
                    else {
                        "power_w": float(today_peak_row[0]),
                        "time": str(today_peak_row[1]) if today_peak_row[1] else None,
                    }
                )
        except Exception as error:
            _LOGGER.debug(f"Could not get today's peak: {error}")
        return None if True else None

    async def save_forecasts(
        self, forecast_data: Dict[str, Any], hourly_forecast: list
    ) -> None:
        """Save forecasts to database. Only writes if NOT locked (morning routine has priority). @zara"""
        try:
            current_time = dt_util.now()
            today_str = current_time.date().isoformat()
            tomorrow_str = (current_time.date() + timedelta(days=1)).isoformat()
            day_after_str = (current_time.date() + timedelta(days=2)).isoformat()
            daily_forecasts = [
                ("today", today_str, forecast_data.get(FORECAST_KEY_TODAY)),
                ("tomorrow", tomorrow_str, forecast_data.get(FORECAST_KEY_TOMORROW)),
                (
                    "day_after_tomorrow",
                    day_after_str,
                    forecast_data.get(FORECAST_KEY_DAY_AFTER),
                ),
            ]
            for forecast_type, forecast_date, forecast_kwh in daily_forecasts:
                if not forecast_kwh is not None:
                    continue
                locked_row = await self.db.fetchone(
                    "SELECT locked FROM daily_forecasts\n                        WHERE forecast_type = ? AND forecast_date = ?",
                    (forecast_type, forecast_date),
                )
                if locked_row and locked_row[0]:
                    _LOGGER.debug(
                        "Skipping %s forecast update - locked by morning routine",
                        forecast_type,
                    )
                    continue
                await self.db.execute(
                    "INSERT INTO daily_forecasts\n                        (forecast_type, forecast_date, prediction_kwh, source)\n                        VALUES (?, ?, ?, 'coordinator_update')\n                        ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                            prediction_kwh = excluded.prediction_kwh,\n                            source = excluded.source\n                        WHERE locked = 0 OR locked IS NULL",
                    (forecast_type, forecast_date, forecast_kwh),
                )
            _LOGGER.debug("Forecasts saved to database (respecting locks)")
        except Exception as error:
            _LOGGER.error(f"Error saving forecasts to database: {error}", exc_info=True)
        return None if True else None

    async def handle_startup_recovery(self) -> None:
        """Handle startup recovery for missing forecasts. @zara"""
        try:
            startup_time = dt_util.now()
            today = startup_time.date().isoformat()
            startup_forecast_row = await self.db.fetchone(
                "SELECT prediction_kwh, locked\n                   FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?\n                   ORDER BY created_at DESC LIMIT 1",
                (today,),
            )
            if not startup_forecast_row or not startup_forecast_row[1]:
                if startup_time.hour < 12:
                    _LOGGER.info(
                        "System started without locked forecast (before 12:00) - initiating recovery"
                    )
                    if hasattr(self.coordinator, "_recovery_forecast_process"):
                        await self.coordinator._recovery_forecast_process(
                            source="startup_recovery"
                        )
                else:
                    _LOGGER.info(
                        "System started late without forecast (after 12:00) - using current forecast (NOT morning baseline)"
                    )
                    if (
                        self.coordinator.data
                        and DATA_KEY_FORECAST_TODAY in self.coordinator.data
                    ):
                        current_forecast_kwh = self.coordinator.data.get(
                            DATA_KEY_FORECAST_TODAY
                        )
                        await self.db.execute(
                            "INSERT OR IGNORE INTO daily_forecasts\n                               (forecast_type, forecast_date, prediction_kwh, source, locked)\n                               VALUES ('today', ?, ?, ?, TRUE)",
                            (
                                today,
                                current_forecast_kwh,
                                f"late_startup_{startup_time.hour:02d}:{startup_time.minute:02d}",
                            ),
                        )
                        _LOGGER.info(
                            f"Set forecast to current value: {current_forecast_kwh:.2f} kWh (not representative of morning prediction)"
                        )
        except Exception as error:
            _LOGGER.error(f"Error during startup recovery: {error}", exc_info=True)
        return None if True else None
