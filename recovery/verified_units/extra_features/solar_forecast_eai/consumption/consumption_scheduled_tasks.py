# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Scheduled Tasks Manager for Solar Forecast Energy AI.

Manages all scheduled tasks including morning routine, hourly updates,
end-of-day processing, and adaptive forecast checks.

@author zara
"""
import asyncio
import hashlib
import logging
import math
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_point_in_time,
)
from .consumption_morning_routine import MorningRoutineHandler
from .consumption_adaptive_forecast import AdaptiveForecastEngine
from ..data.data_daily_summaries import DailySummariesHandler
from ..data.eai_consumption_sample_store import EAIConsumptionSampleStore
from ..ai.ai_predictor import MIN_RIDGE_SAMPLES, REASON_RIDGE_WARMUP
from ..services.service_system_report import SystemReportGenerator
from ..setup_state import is_true
from ..const import (
    ADAPTIVE_CHECK_HOUR,
    ADAPTIVE_CHECK_MINUTE,
    CONF_ADAPTIVE_FORECAST_MODE,
    CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    CONF_HAS_HEATING_ELEMENT,
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WIND_SENSOR,
    DEFAULT_ADAPTIVE_FORECAST_MODE,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..astronomy_provider import AstronomyProviderAdapter

if TYPE_CHECKING:
    from ..coordinator import SolarForecastEAICoordinator
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)
MINIMUM_FINAL_FORECAST_TIME = time(5, 0)
ASTRONOMY_RECOVERY_DELAYS_SECONDS = (30.0, 60.0, 120.0, 240.0, 300.0)


class ScheduledTasksManager:
    """Manages scheduled tasks for the Solar Forecast Energy AI integration. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "SolarForecastEAICoordinator",
        wp_energy_today_entity_id: Optional[str],
        db_manager: "DatabaseManager",
    ):
        """Initialize the ScheduledTasksManager. @zara"""
        self.hass = hass
        self.coordinator = coordinator
        self.wp_energy_today_entity_id = wp_energy_today_entity_id
        self.db = db_manager
        self._listeners = []
        self._dynamic_listeners = []
        self._astronomy_recovery_tasks = {}
        self._shutdown_requested = False
        self._morning_routine_lock = asyncio.Lock()
        self.astronomy_provider = AstronomyProviderAdapter(hass, coordinator.entry)
        self.morning_routine_handler = MorningRoutineHandler(
            hass, db_manager, coordinator, self.astronomy_provider
        )
        self.adaptive_forecast_engine = AdaptiveForecastEngine(
            hass, coordinator, db_manager, self.astronomy_provider
        )
        self.daily_summaries_handler = DailySummariesHandler(hass, db_manager)
        config_entry_id = getattr(
            getattr(coordinator, "config_entry", None), "entry_id", "default"
        )
        self._learning_store = EAIConsumptionSampleStore(hass, config_entry_id)
        self._end_of_day_running = False
        self._weather_corrector = None
        self._pipeline_corrector = None
        self._kalman_tracker = None
        self._weather_mlp = None
        self._init_weather_ai()
        _LOGGER.debug("ScheduledTasksManager initialized")

    def _init_weather_ai(self):
        """Initialize Weather MLP and Kalman tracker. @zara"""
        try:
            from ..data.data_weather_kalman import (
                WeatherKalmanTracker as weather_kalman_tracker_class,
            )

            self._kalman_tracker = weather_kalman_tracker_class()
        except Exception as error:
            _LOGGER.warning("Could not initialize Kalman tracker: %s", error)
        try:
            from ..ai.ai_weather_mlp import TinyWeatherMLP as tiny_weather_mlp_class

            self._weather_mlp = tiny_weather_mlp_class()
            self.hass.async_create_task(self._load_weather_mlp_weights())
        except Exception as error:
            _LOGGER.warning("Could not initialize Weather MLP: %s", error)
        return None if True else None

    def set_pipeline_corrector(self, corrector) -> None:
        """Register pipeline corrector for MLP/Kalman attachment. @zara"""
        self._pipeline_corrector = corrector
        if self._kalman_tracker:
            corrector.set_kalman_tracker(self._kalman_tracker)
        if self._weather_mlp and self._weather_mlp.is_trained:
            corrector.set_weather_mlp(self._weather_mlp)
        elif self._weather_mlp and self.db:
            self.hass.async_create_task(self._load_weather_mlp_weights())
        return None if True else None

    async def _load_weather_mlp_weights(self):
        """Load Weather MLP weights from DB if available. @zara"""
        if self.db is None:
            return
        try:
            weights = await self.db.load_weather_mlp_weights()
            if weights and self._weather_mlp:
                self._weather_mlp.set_weights(weights)
                self._attach_mlp_to_correctors()
                _LOGGER.info(
                    "Weather MLP loaded from DB: %d samples, R²=%.3f",
                    weights.get("training_samples", 0),
                    weights.get("accuracy", 0.0),
                )
            else:
                _LOGGER.info("Weather MLP: not trained yet, using static factors")
        except Exception as error:
            _LOGGER.debug("Could not load Weather MLP weights: %s", error)
        return None if True else None

    def _attach_mlp_to_correctors(self) -> None:
        """Attach MLP to all known corrector instances. @zara"""
        if not self._weather_mlp:
            return
        for corrector in (self._weather_corrector, self._pipeline_corrector):
            if corrector is not None:
                corrector.set_weather_mlp(self._weather_mlp)

    def _get_weather_corrector(self):
        """Get or create WeatherForecastCorrector instance (lazy initialization). @zara"""
        if self._weather_corrector is None:
            from ..data.data_weather_corrector import (
                WeatherForecastCorrector as weather_corrector_class,
            )

            winter_mode = True
            self._weather_corrector = weather_corrector_class(
                self.hass,
                self.db,
                winter_mode=winter_mode,
                astronomy_provider=self.astronomy_provider,
            )
            if self._weather_mlp and self._weather_mlp.is_trained:
                self._weather_corrector.set_weather_mlp(self._weather_mlp)
            elif self._weather_mlp and (not self._weather_mlp.is_trained) and self.db:
                self.hass.async_create_task(self._load_weather_mlp_weights())
            if self._kalman_tracker:
                self._weather_corrector.set_kalman_tracker(self._kalman_tracker)
        return self._weather_corrector

    async def _load_yield_cache(self) -> Dict[str, Any]:
        """Load the EAI-owned central baseline, never a private DB cache."""
        energy_entity_id, energy_unit, measurement_fingerprint = (
            self._measurement_source_contract()
        )
        return (
            await self._learning_store.async_load_writer_state(
                energy_entity_id, energy_unit, measurement_fingerprint
            )
            or {}
        )

    async def _save_yield_cache(self, cache_data: Dict[str, Any]) -> bool:
        """Persist the restart-safe EAI baseline through the SFML DB lease."""
        cache_value = cache_data.get("value")
        cache_time = cache_data.get("time")
        cache_date = cache_data.get("date")
        if cache_value is None or not cache_time or (not cache_date):
            return False
        energy_entity_id, energy_unit, measurement_fingerprint = (
            self._measurement_source_contract()
        )
        return await self._learning_store.async_save_writer_state(
            value=float(cache_value),
            time_local=str(cache_time),
            date_local=str(cache_date),
            source_entity_id=energy_entity_id,
            source_unit=energy_unit,
            boundary_fingerprint=measurement_fingerprint,
        )

    def _measurement_source_contract(self) -> tuple[str, str, str]:
        """Bind a baseline to its exact daily-meter measurement boundary."""
        daily_energy_entity_id = self.wp_energy_today_entity_id or ""
        sensor_state = (
            self.hass.states.get(daily_energy_entity_id)
            if daily_energy_entity_id
            else None
        )
        daily_energy_unit = str(
            (getattr(sensor_state, "attributes", {}) or {}).get("unit_of_measurement")
            or "kWh"
        )
        config_entry = self.coordinator.config_entry
        config = {**config_entry.data, **config_entry.options}
        measurement_topology = str(
            config.get(CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY) or "combined"
        )
        has_heating_element = is_true(config, CONF_HAS_HEATING_ELEMENT)
        heating_element_energy_entity_id = str(
            config.get(CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY) or ""
        )
        heating_element_state = (
            self.hass.states.get(heating_element_energy_entity_id)
            if heating_element_energy_entity_id
            else None
        )
        heating_element_energy_unit = str(
            (getattr(heating_element_state, "attributes", {}) or {}).get(
                "unit_of_measurement"
            )
            or "kWh"
        )
        measurement_fingerprint = hashlib.sha256(
            f"eai-daily-energy-v2:{daily_energy_entity_id}:{daily_energy_unit}:{measurement_topology}:{int(has_heating_element)}:{heating_element_energy_entity_id}:{heating_element_energy_unit}".encode()
        ).hexdigest()
        return (daily_energy_entity_id, daily_energy_unit, measurement_fingerprint)

    async def recover_missing_hourly_actuals(self) -> int:
        """Recover missing hourly actuals using yield deltas from sensor. @zara"""
        now = dt_util.now()
        date = now.date().isoformat()
        try:
            current_meter_kwh = None
            if self.wp_energy_today_entity_id:
                energy_state = self.hass.states.get(self.wp_energy_today_entity_id)
                if energy_state and energy_state.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        current_meter_kwh = float(energy_state.state)
                    except (ValueError, TypeError):
                        pass
            if current_meter_kwh is None:
                _LOGGER.warning(
                    "Cannot recover hourly actuals: yield sensor unavailable"
                )
                return 0
            rows = await self.db.fetchall(
                "SELECT target_hour, prediction_kwh, actual_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (date,),
            )
            if not rows:
                _LOGGER.warning(
                    "Cannot recover hourly actuals: no predictions for today"
                )
                return 0
            production_window = await self._get_production_window_from_cache(now.date())
            if not production_window:
                _LOGGER.warning("Cannot recover hourly actuals: no production window")
                return 0
            production_start, production_end = production_window
            start_hour = production_start.hour
            end_hour = min(now.hour, production_end.hour)
            missing_hours = []
            known_actual_kwh = 0.0
            for row in rows:
                hour = row[0]
                actual_kwh = row[2]
                if hour < start_hour or hour > end_hour:
                    continue
                if actual_kwh is not None:
                    known_actual_kwh = actual_kwh
                elif hour < now.hour:
                    missing_hours.append(hour)
            if not missing_hours:
                _LOGGER.debug("No missing hours to recover")
                return 0
            unallocated_kwh = current_meter_kwh - known_actual_kwh
            if unallocated_kwh <= 0:
                _LOGGER.debug(
                    "No production delta to distribute (current=%.3f, last_known=%.3f)",
                    current_meter_kwh,
                    known_actual_kwh,
                )
                return 0
            weights_by_hour = {}
            total_weight = 0.0
            for row in rows:
                hour = row[0]
                if hour in missing_hours:
                    predicted_kwh = row[1] or 1.0
                    weights_by_hour[hour] = max(predicted_kwh, 0.01)
                    total_weight += weights_by_hour[hour]
            if total_weight == 0:
                total_weight = len(missing_hours)
                weights_by_hour = {missing_hour: 1.0 for missing_hour in missing_hours}
            recovered_count = 0
            cumulative_kwh = known_actual_kwh
            _LOGGER.info(
                "Recovering %d missing hours: %s - Total delta: %.3f kWh",
                len(missing_hours),
                missing_hours,
                unallocated_kwh,
            )
            for hour in sorted(missing_hours):
                hour_weight = weights_by_hour.get(hour, 1.0)
                hourly_actual_kwh = round(
                    hour_weight / total_weight * unallocated_kwh, 3
                )
                cumulative_kwh += hourly_actual_kwh
                sensor_data = {"current_yield_kwh": round(cumulative_kwh, 3)}
                result = await self.morning_routine_handler.update_hourly_actual(
                    date=date,
                    hour=hour,
                    actual_kwh=hourly_actual_kwh,
                    sensor_data=sensor_data,
                )
                if result:
                    recovered_count += 1
                    _LOGGER.debug(
                        "  Recovered hour %02d: %.3f kWh", hour, hourly_actual_kwh
                    )
            await self._save_yield_cache(
                {"value": current_meter_kwh, "time": now.isoformat(), "date": date}
            )
            if recovered_count > 0:
                self.coordinator.async_update_listeners()
            _LOGGER.info(
                "Recovered %d/%d missing hourly actuals",
                recovered_count,
                len(missing_hours),
            )
            return recovered_count
        except Exception as error:
            _LOGGER.error(
                "Error recovering missing hourly actuals: %s", error, exc_info=True
            )
            return 0

    async def _get_production_window_from_cache(
        self, target_date: datetime
    ) -> Optional[tuple]:
        """Get the SFML-owned production window for an exact local date."""
        try:
            astronomy_data = await self.astronomy_provider.async_get_day(target_date)
            if astronomy_data:
                sunrise = datetime.fromisoformat(astronomy_data["sunrise_local"])
                sunset = datetime.fromisoformat(astronomy_data["sunset_local"])
                return (sunrise, sunset)
        except Exception as error:
            _LOGGER.error("Error getting production window: %s", error)
            return

    async def _get_sunrise_for_today(self) -> Optional[datetime]:
        """Get today's sunrise from the validated SFML provider."""
        try:
            date = dt_util.now().date()
            astronomy_data = await self.astronomy_provider.async_get_day(date)
            if not astronomy_data:
                _LOGGER.warning("No astronomy data for %s", date)
                return
            return datetime.fromisoformat(astronomy_data["sunrise_local"])
        except Exception as error:
            _LOGGER.error("Error getting sunrise: %s", error)
            return

    def _calculate_dynamic_times(self, sunrise: datetime) -> dict:
        """Calculate all dynamic task times based on sunrise. @zara"""
        minimum_forecast_time = datetime.combine(
            sunrise.date(), MINIMUM_FINAL_FORECAST_TIME
        )
        if sunrise.tzinfo:
            minimum_forecast_time = minimum_forecast_time.replace(tzinfo=sunrise.tzinfo)

        def apply_minimum(dt: datetime) -> datetime:
            return max(dt, minimum_forecast_time)

        return {
            "weather_blend": apply_minimum(sunrise - timedelta(minutes=45)),
            "corrected_forecast": apply_minimum(sunrise - timedelta(minutes=40)),
            "final_forecast": apply_minimum(sunrise - timedelta(minutes=30)),
        }

    async def _schedule_dynamic_tasks_for_today(
        self, *, schedule_recovery: bool = True
    ) -> bool:
        """Schedule dynamic tasks based on today's sunrise. @zara"""
        for unsubscribe in self._dynamic_listeners:
            try:
                unsubscribe()
            except Exception:
                continue
        self._dynamic_listeners = []
        sunrise = await self._get_sunrise_for_today()
        if not sunrise:
            _LOGGER.warning(
                "SFML astronomy provider unavailable or incomplete; dynamic scheduling skipped"
            )
            if schedule_recovery:
                self._ensure_astronomy_recovery("dynamic_schedule")
            return False
        task_times = self._calculate_dynamic_times(sunrise)
        now = dt_util.now()
        retry_time = now + timedelta(minutes=2)
        if task_times["weather_blend"] > retry_time:
            blend_listener = async_track_point_in_time(
                self.hass, self._dynamic_weather_blend, task_times["weather_blend"]
            )
            self._dynamic_listeners.append(blend_listener)
            _LOGGER.info(
                "Dynamic scheduling: Weather blend at %s (sunrise %s - 45min)",
                task_times["weather_blend"].strftime("%H:%M"),
                sunrise.strftime("%H:%M"),
            )
        if task_times["corrected_forecast"] > retry_time:
            corrected_listener = async_track_point_in_time(
                self.hass,
                self._dynamic_corrected_forecast,
                task_times["corrected_forecast"],
            )
            self._dynamic_listeners.append(corrected_listener)
            _LOGGER.info(
                "Dynamic scheduling: Corrected forecast at %s (sunrise %s - 40min)",
                task_times["corrected_forecast"].strftime("%H:%M"),
                sunrise.strftime("%H:%M"),
            )
        if task_times["final_forecast"] > retry_time:
            final_listener = async_track_point_in_time(
                self.hass, self._dynamic_final_forecast, task_times["final_forecast"]
            )
            self._dynamic_listeners.append(final_listener)
            _LOGGER.info(
                "Dynamic scheduling: Final forecast at %s (sunrise %s - 30min)",
                task_times["final_forecast"].strftime("%H:%M"),
                sunrise.strftime("%H:%M"),
            )
        if not self._dynamic_listeners:
            _LOGGER.debug("All dynamic task times already passed for today")
            await self._check_morning_routine_catchup()
        return True

    def _ensure_astronomy_recovery(self, operation: str) -> None:
        """Start one bounded recovery loop for an astronomy-dependent operation."""
        if self._shutdown_requested:
            return
        existing_task = self._astronomy_recovery_tasks.get(operation)
        if existing_task is not None and (not existing_task.done()):
            return
        recovery_task = self.hass.async_create_task(
            self._retry_astronomy_operation(operation)
        )
        self._astronomy_recovery_tasks[operation] = recovery_task
        recovery_task.add_done_callback(
            lambda completed, name=operation: self._finish_astronomy_recovery(
                name, completed
            )
        )

    def _finish_astronomy_recovery(
        self, operation: str, task: asyncio.Task[Any]
    ) -> None:
        if self._astronomy_recovery_tasks.get(operation) is task:
            self._astronomy_recovery_tasks.pop(operation, None)
        return None if True else None

    async def _retry_astronomy_operation(self, operation: str) -> None:
        """Retry a failed astronomy dependency without concurrent reruns."""
        for attempt_number, retry_delay_seconds in enumerate(
            ASTRONOMY_RECOVERY_DELAYS_SECONDS, start=1
        ):
            await asyncio.sleep(retry_delay_seconds)
            if self._shutdown_requested:
                return
            if operation == "dynamic_schedule":
                success = await self._schedule_dynamic_tasks_for_today(
                    schedule_recovery=False
                )
            elif operation == "morning_routine":
                success = await self._execute_morning_routine()
            else:
                raise ValueError(f"Unknown astronomy recovery operation: {operation}")
            if success:
                _LOGGER.info(
                    "Astronomy-dependent %s recovered on attempt %d",
                    operation,
                    attempt_number,
                )
                return
        _LOGGER.warning(
            "Astronomy-dependent %s did not recover after %d attempts",
            operation,
            len(ASTRONOMY_RECOVERY_DELAYS_SECONDS),
        )

    async def _check_morning_routine_catchup(self) -> None:
        """Check if morning routine was missed and run catch-up if needed.

        IMPORTANT: This ONLY runs on fresh installations to avoid overwriting
        the correct morning forecast created 30min before sunrise! @zara
        """
        try:
            date = dt_util.now().date().isoformat()
            current_hour = dt_util.now().hour
            if current_hour < 5 or current_hour > 20:
                _LOGGER.debug("Outside catch-up window (5:00-20:00), skipping")
                return
            row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE target_date = ?",
                (date,),
            )
            prediction_count = row[0] if row else 0
            if prediction_count > 0:
                _LOGGER.debug(
                    "Predictions exist for %s (%d hours) - no catch-up to protect existing data",
                    date,
                    prediction_count,
                )
                return
            daily_forecast_row = await self.db.fetchone(
                "SELECT source FROM daily_forecasts\n                   WHERE forecast_date = ? AND forecast_type = 'today'",
                (date,),
            )
            if daily_forecast_row:
                _LOGGER.debug(
                    "Daily forecast exists for %s (source=%s) - no catch-up needed",
                    date,
                    daily_forecast_row[0],
                )
                return
            yesterday = (dt_util.now() - timedelta(days=1)).date().isoformat()
            yesterday_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE target_date = ?",
                (yesterday,),
            )
            has_yesterday_predictions = (
                (yesterday_count_row[0] or 0) > 0 if yesterday_count_row else False
            )
            if has_yesterday_predictions:
                _LOGGER.debug(
                    "Historical data exists - this is not a fresh install, morning routine will run at scheduled time"
                )
                return
            _LOGGER.warning(
                "FRESH INSTALLATION detected! Running initial forecast setup for %s",
                date,
            )
            await self._dynamic_weather_blend(dt_util.now())
            await asyncio.sleep(2)
            await self._dynamic_corrected_forecast(dt_util.now())
            await asyncio.sleep(2)
            result = await self._execute_morning_routine()
            if result:
                _LOGGER.info(
                    "Initial forecast setup SUCCESSFUL for %s (fresh installation)",
                    date,
                )
            else:
                _LOGGER.error(
                    "Initial forecast setup FAILED for %s (fresh installation)", date
                )
        except Exception as error:
            _LOGGER.error("Morning routine catch-up error: %s", error, exc_info=True)
        return None if True else None

    async def _dynamic_weather_blend(self, now: datetime) -> None:
        """Dynamic Task 1: Refresh Multi-Weather Blender. @zara"""
        current_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ [1/3] WEATHER BLEND REFRESH @ %-45s║", current_time.strftime("%H:%M")
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            if hasattr(self.coordinator, "weather_pipeline_manager"):
                weather_pipeline = self.coordinator.weather_pipeline_manager
                _LOGGER.info("  → Fetching fresh weather data from multiple sources...")
                refreshed = await asyncio.wait_for(
                    weather_pipeline._refresh_all_weather_sources(force=True),
                    timeout=60.0,
                )
                if refreshed:
                    blend_stats = {}
                    if weather_pipeline.weather_expert_blender:
                        blend_stats = (
                            weather_pipeline.weather_expert_blender.get_blend_stats()
                        )
                    _LOGGER.info("  ✓ Weather blend SUCCESS!")
                    _LOGGER.info(
                        "    • Active sources: %d", blend_stats.get("active_sources", 0)
                    )
                    _LOGGER.info(
                        "    • Hours blended: %d", blend_stats.get("hours_blended", 0)
                    )
                    _LOGGER.info("    • Cache updated")
                else:
                    _LOGGER.warning("  ✗ Weather blend FAILED - using cached data")
            else:
                _LOGGER.warning("  ✗ Weather pipeline manager not available")
        except asyncio.TimeoutError:
            _LOGGER.warning("  ✗ Weather blend TIMED OUT (60s)")
        except Exception as error:
            _LOGGER.error("  ✗ Weather blend ERROR: %s", error, exc_info=True)
        _LOGGER.info("")

    async def _dynamic_corrected_forecast(self, now: datetime) -> None:
        """Dynamic Task 2: Create corrected weather forecast. @zara"""
        current_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ [2/3] CORRECTED FORECAST @ %-48s║", current_time.strftime("%H:%M")
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            if hasattr(self.coordinator, "weather_pipeline_manager"):
                weather_pipeline = self.coordinator.weather_pipeline_manager
                if (
                    hasattr(weather_pipeline, "weather_corrector")
                    and weather_pipeline.weather_corrector
                ):
                    _LOGGER.info("  → Applying learned corrections to weather data...")
                    result = await asyncio.wait_for(
                        weather_pipeline.weather_corrector.create_corrected_forecast(),
                        timeout=30.0,
                    )
                    if result:
                        _LOGGER.info("  ✓ Corrected forecast SUCCESS!")
                        _LOGGER.info("    • Temperature corrections applied")
                        _LOGGER.info("    • Solar radiation corrections applied")
                        _LOGGER.info("    • Cloud cover corrections applied")
                        _LOGGER.info("    • Corrected forecast saved to database")
                    else:
                        _LOGGER.warning("  ✗ Corrected forecast creation FAILED")
                else:
                    _LOGGER.warning("  ✗ Weather corrector not available")
            else:
                _LOGGER.warning("  ✗ Weather pipeline manager not available")
        except asyncio.TimeoutError:
            _LOGGER.warning("  ✗ Corrected forecast TIMED OUT (30s)")
        except Exception as error:
            _LOGGER.error("  ✗ Corrected forecast ERROR: %s", error, exc_info=True)
        _LOGGER.info("")

    async def _dynamic_final_forecast(self, now: datetime) -> None:
        """Dynamic Task 3: Execute final morning forecast. @zara"""
        current_time = now if now is not None else dt_util.now()
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ [3/3] FINAL MORNING FORECAST @ %-44s║", current_time.strftime("%H:%M")
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        _LOGGER.info("  → Executing complete morning routine...")
        try:
            result = await asyncio.wait_for(
                self._execute_morning_routine(), timeout=300.0
            )
            if result:
                _LOGGER.info("")
                _LOGGER.info("  ✓✓✓ ALL MORNING TASKS COMPLETED SUCCESSFULLY! ✓✓✓")
                _LOGGER.info("")
            else:
                _LOGGER.error("  ✗✗✗ MORNING ROUTINE FAILED - CHECK LOGS ABOVE")
        except asyncio.TimeoutError:
            _LOGGER.warning("  ✗ Final forecast TIMED OUT (300s) - will retry later")
        except Exception as error:
            _LOGGER.error("  ✗ Final forecast ERROR: %s", error, exc_info=True)
        return None if True else None

    async def _execute_morning_routine(self) -> bool:
        """Serialize morning writes so recovery cannot create concurrent runs."""
        morning_routine_lock = getattr(self, "_morning_routine_lock", None)
        if morning_routine_lock is None:
            morning_routine_lock = self._morning_routine_lock = asyncio.Lock()
        async with morning_routine_lock:
            return await self._execute_morning_routine_unlocked()
        return None if True else None

    async def _execute_morning_routine_unlocked(self) -> bool:
        """Execute the morning routine with fresh data. @zara"""
        try:
            date = dt_util.now().date().isoformat()
            now = dt_util.now()
            forecast_result = await self._generate_full_forecast(date)
            if not forecast_result:
                _LOGGER.error("Failed to generate forecast for morning routine")
                return False
            hourly_forecast = forecast_result.get("hourly", [])
            if not hourly_forecast:
                _LOGGER.error("No hourly forecast data in result")
                return False
            hourly_weather = await self._get_weather_hourly(date)
            astronomy_data = await self._get_astronomy_data(date)
            if not astronomy_data:
                _LOGGER.warning(
                    "SFML astronomy provider unavailable or incomplete; morning routine skipped"
                )
                self._ensure_astronomy_recovery("morning_routine")
                return False
            sensor_config = self._get_sensor_config()
            result = (
                await self.morning_routine_handler.execute_morning_routine_with_retry(
                    date=date,
                    hourly_forecast=hourly_forecast,
                    weather_hourly=hourly_weather,
                    astronomy_data=astronomy_data,
                    sensor_config=sensor_config,
                )
            )
            if result:
                best_hour = forecast_result.get("best_hour")
                best_hour_kwh = forecast_result.get("best_hour_kwh")
                if best_hour is not None:
                    try:
                        await self.coordinator.data_manager.save_forecast_best_hour(
                            hour=best_hour,
                            prediction_kwh=best_hour_kwh or 0.0,
                            source=forecast_result.get("method", "morning_routine"),
                        )
                        _LOGGER.info(
                            "Best hour saved: %02d:00 (%.3f kWh)",
                            best_hour,
                            best_hour_kwh or 0.0,
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save best hour: %s", error)
                tomorrow_forecast = forecast_result.get("tomorrow")
                if tomorrow_forecast is not None:
                    tomorrow = now + timedelta(days=1)
                    try:
                        await self.coordinator.data_manager.save_forecast_tomorrow(
                            target_date=tomorrow,
                            prediction_kwh=tomorrow_forecast,
                            source=forecast_result.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug(
                            "Tomorrow forecast saved: %.2f kWh", tomorrow_forecast
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save tomorrow forecast: %s", error)
                day_after_forecast = forecast_result.get("day_after_tomorrow")
                if day_after_forecast is not None:
                    day_after_tomorrow = now + timedelta(days=2)
                    try:
                        await self.coordinator.data_manager.save_forecast_day_after(
                            target_date=day_after_tomorrow,
                            prediction_kwh=day_after_forecast,
                            source=forecast_result.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug(
                            "Day-after forecast saved: %.2f kWh", day_after_forecast
                        )
                    except Exception as error:
                        _LOGGER.warning("Failed to save day-after forecast: %s", error)
                self.coordinator.async_update_listeners()
            return result
        except Exception as error:
            _LOGGER.error("Morning routine execution failed: %s", error, exc_info=True)
            return False

    async def _generate_full_forecast(self, date: str) -> Dict[str, Any]:
        "Generate full forecast (today + tomorrow + day_after) using coordinator's forecast services. @zara\n\nReturns:\n    Dict with keys: hourly, today, tomorrow, day_after_tomorrow, method\n"
        try:
            hourly_weather = []
            if (
                hasattr(self.coordinator, "weather_service")
                and self.coordinator.weather_service
            ):
                hourly_weather = (
                    await self.coordinator.weather_service.get_corrected_hourly_forecast()
                    or []
                )
            if not hourly_weather:
                _LOGGER.warning("No weather data available, trying database fallback")
                hourly_weather = await self._get_weather_hourly(date)
            if not hourly_weather:
                _LOGGER.error("No weather data for hourly forecast generation")
                return {}
            sensor_data = {}
            if hasattr(self.coordinator, "sensor_collector"):
                sensor_data = (
                    self.coordinator.sensor_collector.collect_all_sensor_data_dict()
                )
            if not hasattr(self.coordinator, "forecast_orchestrator"):
                _LOGGER.error("Forecast orchestrator not available")
                return {}
            forecast_result = (
                await self.coordinator.forecast_orchestrator.orchestrate_forecast(
                    current_weather=None,
                    hourly_forecast=hourly_weather,
                    external_sensors=sensor_data,
                    correction_factor=getattr(
                        self.coordinator, "learned_correction_factor", 1.0
                    ),
                )
            )
            hourly_forecast = forecast_result.get("hourly", [])
            if not hourly_forecast:
                _LOGGER.warning(
                    "Forecast orchestrator did not return hourly data, creating from daily"
                )
                forecast_today = forecast_result.get("today", 0.0)
                hourly_forecast = await self._estimate_hourly_from_daily(
                    date, forecast_today
                )
                forecast_result["hourly"] = hourly_forecast
            _LOGGER.info(
                "Generated full forecast for %s: today=%.2f, tomorrow=%.2f, day_after=%.2f kWh",
                date,
                forecast_result.get("today", 0.0),
                forecast_result.get("tomorrow", 0.0),
                forecast_result.get("day_after_tomorrow", 0.0),
            )
            return forecast_result
        except Exception as error:
            _LOGGER.error("Error generating full forecast: %s", error, exc_info=True)
            return {}

    async def _estimate_hourly_from_daily(
        self, date: str, daily_total: float
    ) -> List[Dict[str, Any]]:
        """Estimate hourly distribution from daily total using astronomy curve. @zara"""
        try:
            astronomy_data = await self._get_astronomy_data(date)
            if not astronomy_data:
                _LOGGER.warning(
                    "SFML astronomy provider unavailable or incomplete; hourly estimate skipped"
                )
                return []
            hourly_estimates = []
            total_elevation = 0.0
            elevations_by_hour = {}
            for hour in range(5, 22):
                hourly_astronomy = astronomy_data.get(str(hour), {})
                elevation = hourly_astronomy.get("sun_elevation_deg", 0)
                if elevation > 0:
                    elevations_by_hour[hour] = elevation
                    total_elevation += elevation
            if total_elevation > 0:
                for hour, elevation in elevations_by_hour.items():
                    elevation_weight = elevation / total_elevation
                    hourly_prediction_kwh = round(daily_total * elevation_weight, 4)
                    hourly_estimates.append(
                        {
                            "hour": hour,
                            "production_kwh": hourly_prediction_kwh,
                            "method": "elevation_weighted",
                            "confidence": 60.0,
                        }
                    )
            else:
                _LOGGER.warning(
                    "SFML astronomy contains no positive solar elevation; hourly estimate skipped"
                )
                return []
            return sorted(hourly_estimates, key=lambda x: x["hour"])
        except Exception as error:
            _LOGGER.error("Error estimating hourly from daily: %s", error)
            return []

    async def _get_weather_hourly(self, date: str) -> List[Dict[str, Any]]:
        """Get hourly weather forecast from database for 3 days. @zara

        Loads weather data for today, tomorrow, and day_after_tomorrow.
        Keys are normalized to match what forecast_rule_based_strategy expects.
        """
        try:
            from datetime import datetime as datetime_factory

            target_date = datetime_factory.fromisoformat(date).date()
            end_date = target_date + timedelta(days=2)
            rows = await self.db.fetchall(
                "SELECT forecast_date, hour, temperature, solar_radiation_wm2,\n                          wind, humidity, rain, clouds, pressure,\n                          direct_radiation, diffuse_radiation, weather_code\n                   FROM weather_forecast\n                   WHERE forecast_date >= ? AND forecast_date <= ?\n                   ORDER BY forecast_date, hour",
                (target_date.isoformat(), end_date.isoformat()),
            )
            hourly_weather = []
            for row in rows:
                forecast_date = str(row[0])
                hour = row[1]
                solar_radiation = row[3] or 0.0
                direct_radiation = row[9] or 0.0
                diffuse_radiation = row[10] or 0.0
                hourly_weather.append(
                    {
                        "datetime": f"{forecast_date}T{hour:02d}:00:00",
                        "date": forecast_date,
                        "hour": hour,
                        "temperature": row[2],
                        "solar_radiation_wm2": solar_radiation,
                        "solar_radiation": solar_radiation,
                        "ghi": solar_radiation,
                        "direct_radiation": direct_radiation,
                        "diffuse_radiation": diffuse_radiation,
                        "wind": row[4],
                        "wind_speed": row[4],
                        "humidity": row[5],
                        "rain": row[6],
                        "precipitation": row[6],
                        "clouds": row[7],
                        "cloud_cover": row[7],
                        "pressure": row[8],
                        "weather_code": row[11],
                    }
                )
            _LOGGER.debug(
                "Loaded %d weather hours from DB (3-day range: %s to %s)",
                len(hourly_weather),
                target_date.isoformat(),
                end_date.isoformat(),
            )
            return hourly_weather
        except Exception as error:
            _LOGGER.error("Error getting weather hourly: %s", error)
            return []

    async def _get_astronomy_data(self, date: str) -> Dict[str, Any]:
        """Get today through day+2 from the read-only SFML provider."""
        return await self.astronomy_provider.async_get_legacy_days(
            datetime.fromisoformat(date).date()
        )

    def _get_sensor_config(self) -> Dict[str, Any]:
        """Get sensor configuration from coordinator. @zara"""
        config_entry = self.coordinator.config_entry
        return {
            "temp_sensor": config_entry.data.get(CONF_TEMP_SENSOR),
            "humidity_sensor": config_entry.data.get(CONF_HUMIDITY_SENSOR),
            "pressure_sensor": config_entry.data.get(CONF_PRESSURE_SENSOR),
            "wind_sensor": config_entry.data.get(CONF_WIND_SENSOR),
            "rain_sensor": config_entry.data.get(CONF_RAIN_SENSOR),
        }

    async def schedule_all_tasks(self) -> None:
        """Schedule all recurring tasks. @zara"""
        hourly_listener = async_track_time_change(
            self.hass, self._hourly_update, minute=5, second=0
        )
        self._listeners.append(hourly_listener)
        midnight_listener = async_track_time_change(
            self.hass, self._midnight_task, hour=0, minute=10, second=0
        )
        self._listeners.append(midnight_listener)
        early_morning_listener = async_track_time_change(
            self.hass, self._early_morning_forecast, hour=0, minute=30, second=0
        )
        self._listeners.append(early_morning_listener)
        _LOGGER.info("Early morning forecast scheduled for 00:30 daily")
        end_of_day_listener = async_track_time_change(
            self.hass, self._end_of_day_task, hour=23, minute=40, second=0
        )
        self._listeners.append(end_of_day_listener)
        _LOGGER.info("End-of-day task scheduled for 23:40 daily")
        config_entry = self.coordinator.config_entry
        if config_entry.options.get(
            CONF_ADAPTIVE_FORECAST_MODE, DEFAULT_ADAPTIVE_FORECAST_MODE
        ):
            adaptive_listener = async_track_time_change(
                self.hass,
                self._adaptive_forecast_check,
                hour=ADAPTIVE_CHECK_HOUR,
                minute=ADAPTIVE_CHECK_MINUTE,
                second=0,
            )
            self._listeners.append(adaptive_listener)
            _LOGGER.info(
                "Adaptive forecast check scheduled at %02d:%02d",
                ADAPTIVE_CHECK_HOUR,
                ADAPTIVE_CHECK_MINUTE,
            )
        await self._schedule_dynamic_tasks_for_today()
        _LOGGER.info("============================================================")
        _LOGGER.info("All scheduled tasks registered successfully:")
        _LOGGER.info("  • Hourly update: Every hour at :05")
        _LOGGER.info("  • Midnight task: Daily at 00:10")
        _LOGGER.info("  • Early morning forecast: Daily at 00:30")
        _LOGGER.info("  • End-of-day: Daily at 23:40")
        if config_entry.options.get(
            CONF_ADAPTIVE_FORECAST_MODE, DEFAULT_ADAPTIVE_FORECAST_MODE
        ):
            _LOGGER.info(
                "  • Adaptive forecast: Daily at %02d:%02d",
                ADAPTIVE_CHECK_HOUR,
                ADAPTIVE_CHECK_MINUTE,
            )
        _LOGGER.info(
            "  • Dynamic morning tasks: Sunrise-based (Weather blend, Corrected forecast, Final forecast)"
        )
        _LOGGER.info("  Total listeners: %d", len(self._listeners))
        _LOGGER.info("============================================================")

    @callback
    def _hourly_update(self, now: datetime) -> None:
        """Handle hourly update task. @zara"""
        self.hass.async_create_task(self._execute_hourly_update())

    async def _execute_hourly_update(self) -> None:
        """Execute hourly update. @zara"""
        try:
            _LOGGER.debug("Executing hourly update")
            now = dt_util.now()
            now_utc = now.astimezone(UTC)
            sample_start_utc = now_utc
            previous_hour_utc = now_utc - timedelta(hours=1)
            previous_hour_local = previous_hour_utc.astimezone(now.tzinfo)
            target_date = previous_hour_local.date().isoformat()
            target_hour = previous_hour_local.hour
            sensor_data = await self._collect_sensor_data()
            current_meter_kwh = sensor_data.get("current_yield_kwh")
            if current_meter_kwh is None:
                _LOGGER.info(
                    "Hourly consumption update skipped: daily heat-pump energy sensor is unavailable"
                )
                return
            yield_cache = await self._load_yield_cache()
            cache_date = str(yield_cache.get("date") or "")
            today = now.date().isoformat()
            if target_date != today:
                cache_saved = await self._save_yield_cache(
                    {"value": current_meter_kwh, "time": now.isoformat(), "date": today}
                )
                if not cache_saved:
                    _LOGGER.info(
                        "New-day EAI baseline is not ready yet; next hourly run will rebaseline safely"
                    )
                _LOGGER.debug(
                    "Hourly consumption update skipped across daily-meter reset for %s hour %02d",
                    target_date,
                    target_hour,
                )
                return
            cache_timestamp = dt_util.parse_datetime(str(yield_cache.get("time") or ""))
            cache_interval_valid = False
            baseline_utc = previous_hour_utc
            if cache_timestamp is not None:
                baseline_utc = cache_timestamp.astimezone(UTC)
                elapsed_seconds = (now_utc - baseline_utc).total_seconds()
                cache_interval_valid = 2700 <= elapsed_seconds <= 5400
            if cache_date != today:
                await self._save_yield_cache(
                    {"value": current_meter_kwh, "time": now.isoformat(), "date": today}
                )
                _LOGGER.info(
                    "Hourly consumption update rebaselined after restart or missing daily cache"
                )
                return
            if cache_date == today:
                if not cache_interval_valid:
                    await self._save_yield_cache(
                        {
                            "value": current_meter_kwh,
                            "time": now.isoformat(),
                            "date": today,
                        }
                    )
                    _LOGGER.info(
                        "Hourly consumption update rebaselined after a sensor or scheduler gap"
                    )
                    return
            previous_meter_kwh = (
                float(yield_cache.get("value") or 0.0) if cache_date == today else 0.0
            )
            meter_delta_kwh = float(current_meter_kwh) - previous_meter_kwh
            if meter_delta_kwh < 0:
                await self._save_yield_cache(
                    {"value": current_meter_kwh, "time": now.isoformat(), "date": today}
                )
                _LOGGER.info(
                    "Hourly consumption update rebaselined after an unexpected daily-meter reset"
                )
                return
            hourly_actual_kwh = meter_delta_kwh
            weather_actual = {
                "temperature_c": sensor_data.get("temperature_c"),
                "humidity_percent": sensor_data.get("humidity_percent"),
                "wind_speed_ms": sensor_data.get("wind_speed_ms"),
                "precipitation_mm": sensor_data.get("rain_mm"),
                "pressure_hpa": sensor_data.get("pressure_hpa"),
                "solar_radiation_wm2": sensor_data.get("solar_radiation_wm2"),
                "lux": sensor_data.get("lux"),
            }
            weather_actual_tracker = getattr(
                getattr(self.coordinator, "weather_pipeline_manager", None),
                "weather_actual_tracker",
                None,
            )
            if weather_actual_tracker:
                tracked_weather = await weather_actual_tracker.get_actual_weather(
                    target_date, target_hour
                )
                if tracked_weather:
                    weather_actual["cloud_cover_percent"] = tracked_weather.get(
                        "cloud_cover_percent"
                    )
                    weather_actual["frost_detected"] = tracked_weather.get(
                        "frost_detected"
                    )
                    weather_actual["frost_score"] = tracked_weather.get("frost_score")
                    weather_actual["frost_confidence"] = tracked_weather.get(
                        "frost_confidence"
                    )
                    weather_actual["snow_covered_panels"] = tracked_weather.get(
                        "snow_covered_panels"
                    )
                    if not weather_actual.get("lux"):
                        if tracked_weather.get("lux"):
                            weather_actual["lux"] = tracked_weather.get("lux")
            group_actuals = None
            panel_group_reader = getattr(
                self.coordinator, "panel_group_sensor_reader", None
            )
            if panel_group_reader:
                try:
                    group_actuals = (
                        await panel_group_reader.get_all_hourly_productions()
                    )
                    if group_actuals:
                        _LOGGER.debug(
                            "Panel group actuals for hour %d: %s",
                            target_hour,
                            group_actuals,
                        )
                except Exception as error:
                    _LOGGER.warning("Failed to read panel group actuals: %s", error)
            sensor_config = self._get_sensor_config()
            await self.morning_routine_handler.update_hourly_actual(
                date=target_date,
                hour=target_hour,
                actual_kwh=hourly_actual_kwh,
                sensor_data=sensor_data,
                weather_actual=weather_actual,
                panel_group_actuals=group_actuals,
                inverter_max_power=getattr(self.coordinator, "inverter_max_power", 0.0)
                or 0.0,
                zero_export_mode=sensor_config.get("zero_export_mode", False),
                has_battery=sensor_config.get("has_battery", False),
                solar_to_battery_power=sensor_data.get("solar_to_battery_power"),
            )
            astronomy_data = await self.morning_routine_handler._get_astronomy_for_hour(
                target_date, target_hour
            )
            energy_entity_id, energy_unit, measurement_fingerprint = (
                self._measurement_source_contract()
            )
            stored = await self._learning_store.async_store_meter_sample(
                date=target_date,
                hour=target_hour,
                actual_kwh=hourly_actual_kwh,
                sensor_data=sensor_data,
                weather_actual=weather_actual,
                astronomy=astronomy_data,
                baseline_kwh=float(current_meter_kwh),
                baseline_time_local=now.isoformat(),
                baseline_date_local=today,
                source_entity_id=energy_entity_id,
                source_unit=energy_unit,
                boundary_fingerprint=measurement_fingerprint,
                interval_start_utc=baseline_utc.isoformat().replace("+00:00", "Z"),
                interval_end_utc=sample_start_utc.isoformat().replace("+00:00", "Z"),
            )
            if not stored:
                _LOGGER.warning(
                    "EAI interval was not persisted; baseline intentionally not advanced"
                )
                return
            self.coordinator.async_update_listeners()
            if self._kalman_tracker and 7 <= target_hour <= 19:
                measured_radiation = sensor_data.get("solar_radiation_wm2")
                if measured_radiation is not None:
                    try:
                        forecast_radiation = (
                            await self._get_forecast_radiation_for_hour(
                                target_date, target_hour
                            )
                        )
                        if forecast_radiation is not None and forecast_radiation > 0:
                            radiation_error = measured_radiation - forecast_radiation
                            self._kalman_tracker.update(radiation_error)
                    except Exception as error:
                        _LOGGER.debug("Kalman update skipped: %s", error)
        except Exception as error:
            _LOGGER.error("Hourly update failed: %s", error, exc_info=True)
        if True:
            return None
        else:
            return None

    async def _get_forecast_radiation_for_hour(
        self, date_str: str, hour: int
    ) -> Optional[float]:
        """Get forecast solar radiation for Kalman comparison. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT solar_radiation_wm2 FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (date_str, hour),
            )
            return float(row[0]) if row and row[0] is not None else None
        except Exception:
            return

    async def _train_weather_mlp(self, current_time) -> None:
        """Train Weather MLP on historical precision data. @zara"""
        if self._weather_mlp is None:
            return
        import math as math_library

        rows = await self.db.fetchall(
            "SELECT wpd.hour, wpd.solar_forecast, wpd.solar_actual, wpd.solar_factor,\n                      wco.cloud_cover, wco.humidity, wco.pressure, wco.wind_speed,\n                      wpd.date\n               FROM weather_precision_daily wpd\n               JOIN weather_cache_open_meteo wco\n                    ON wpd.date = wco.forecast_date AND wpd.hour = wco.hour\n               WHERE wpd.solar_forecast > 10\n                 AND wpd.solar_actual IS NOT NULL\n                 AND wpd.date >= date(?, '-30 days')\n               ORDER BY wpd.date, wpd.hour",
            (current_time.date().isoformat(),),
        )
        if not rows or len(rows) < 50:
            _LOGGER.info(
                "Weather MLP: insufficient training data (%d samples, need 50)",
                len(rows) if rows else 0,
            )
            return
        merged_rows = []
        unique_dates = []
        for row in rows:
            try:
                parsed_sample_date = datetime.fromisoformat(str(row[8])).date()
                hour = int(row[0])
                forecast_radiation = float(row[1])
                correction_factor = float(row[3])
                cloud_cover = float(row[4] if row[4] is not None else 50.0)
                humidity = float(row[5] if row[5] is not None else 70.0)
                pressure = float(row[6] if row[6] is not None else 1013.0)
                wind_speed = float(row[7] if row[7] is not None else 3.0)
            except (TypeError, ValueError, OverflowError):
                continue
            sample_features = (
                forecast_radiation,
                correction_factor,
                cloud_cover,
                humidity,
                pressure,
                wind_speed,
            )
            if (
                hour not in range(24)
                or not all(
                    (
                        (math_library.isfinite if True else None)(feature_value)
                        for feature_value in sample_features
                    )
                )
                or forecast_radiation <= 10
                or (not 0 < correction_factor <= 3.0)
            ):
                continue
            merged_rows.append(
                (
                    hour,
                    forecast_radiation,
                    row[2],
                    correction_factor,
                    cloud_cover,
                    humidity,
                    pressure,
                    wind_speed,
                    parsed_sample_date.isoformat(),
                )
            )
            unique_dates.append(parsed_sample_date)
            continue
        if len(merged_rows) < 50:
            _LOGGER.info(
                "Weather MLP: insufficient date-valid training data (%d samples, need 50)",
                len(merged_rows),
            )
            return
        start_date = min(unique_dates)
        end_date = max(unique_dates)
        astronomy_data = await self.astronomy_provider.async_get_legacy_days(
            start_date, days=(end_date - start_date).days + 1
        )
        if not astronomy_data:
            _LOGGER.info("Weather MLP: SFML astronomy unavailable; training skipped")
            return
        features = []
        targets = []
        for row in merged_rows:
            (
                hour,
                forecast_radiation,
                actual_radiation,
                correction_factor,
                cloud_cover,
                humidity,
                pressure,
                wind_speed,
                sample_date_value,
            ) = row
            sun_elevation = astronomy_data.get(
                f"{sample_date_value}_{hour:02d}", {}
            ).get("sun_elevation_deg")
            if sun_elevation is None or sun_elevation <= 5:
                continue
            input_features = [
                forecast_radiation / 1200.0,
                (cloud_cover or 50.0) / 100.0,
                max(0.0, min(1.0, (sun_elevation or 0.0) / 90.0)),
                (humidity or 70.0) / 100.0,
                ((pressure or 1013.0) - 950.0) / 100.0,
                (wind_speed or 3.0) / 30.0,
                (math_library.sin if True else None)(
                    2.0 * math_library.pi * hour / 24.0
                ),
                (math_library.cos if True else None)(
                    2.0 * math_library.pi * hour / 24.0
                ),
            ]
            target_factor = min(2.0, max(0.1, correction_factor))
            features.append(input_features)
            targets.append(target_factor)
        if len(features) < 50:
            _LOGGER.info(
                "Weather MLP: after filtering only %d valid samples", len(features)
            )
            return
        _LOGGER.info("Weather MLP training: %d samples, 8 features", len(features))
        result = await self._weather_mlp.train(
            X_data=features,
            y_targets=targets,
            epochs=50,
            validation_split=0.2,
            early_stopping_patience=10,
        )
        if result.get("success"):
            weights = self._weather_mlp.get_weights()
            await self.db.save_weather_mlp_weights(weights)
            self._attach_mlp_to_correctors()
            _LOGGER.info(
                "Weather MLP trained and saved: R²=%.3f, RMSE=%.4f, %d samples",
                result.get("accuracy", 0.0),
                result.get("rmse", 0.0),
                result.get("training_samples", 0),
            )
        else:
            _LOGGER.warning("Weather MLP training failed")
        if True:
            return

    async def _collect_sensor_data(self) -> Dict[str, Any]:
        """Collect current sensor data. @zara"""
        sensor_data = {}
        if self.wp_energy_today_entity_id:
            sensor_state = self.hass.states.get(self.wp_energy_today_entity_id)
            if sensor_state and sensor_state.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                meter_kwh = self._energy_state_to_kwh(sensor_state)
                if meter_kwh is not None:
                    sensor_data["current_yield_kwh"] = meter_kwh
        config_entry = self.coordinator.config_entry
        config = {**config_entry.data, **config_entry.options}
        add_heating_element = config.get(
            CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY
        ) == "separate" and is_true(config, CONF_HAS_HEATING_ELEMENT)
        if add_heating_element:
            heating_element_energy_entity_id = config.get(
                CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY
            )
            heating_element_state = (
                self.hass.states.get(heating_element_energy_entity_id)
                if isinstance(heating_element_energy_entity_id, str)
                and heating_element_energy_entity_id
                else None
            )
            heating_element_kwh = (
                self._energy_state_to_kwh(heating_element_state)
                if heating_element_state
                and heating_element_state.state not in (None, "unknown", "unavailable")
                else None
            )
            base_consumption_kwh = sensor_data.get("current_yield_kwh")
            if base_consumption_kwh is None or heating_element_kwh is None:
                sensor_data.pop("current_yield_kwh", None)
            else:
                sensor_data["current_yield_kwh"] = (
                    base_consumption_kwh + heating_element_kwh
                )
        sensor_config = self._get_sensor_config()
        sensor_keys = {
            "temp_sensor": "temperature_c",
            "humidity_sensor": "humidity_percent",
            "pressure_sensor": "pressure_hpa",
            "wind_sensor": "wind_speed_ms",
            "rain_sensor": "rain_mm",
            "solar_radiation_sensor": "solar_radiation_wm2",
            "lux_sensor": "lux",
        }
        for sensor_config_key, sensor_entity_id in sensor_config.items():
            if sensor_config_key not in sensor_keys:
                continue
            if sensor_entity_id:
                if isinstance(sensor_entity_id, str):
                    sensor_state = self.hass.states.get(sensor_entity_id)
                    if sensor_state:
                        if sensor_state.state not in (None, "unknown", "unavailable"):
                            try:
                                sensor_data_key = sensor_keys[sensor_config_key]
                                sensor_data[sensor_data_key] = float(sensor_state.state)
                            except (ValueError, TypeError):
                                continue
        solar_to_battery_entity_id = sensor_config.get("solar_to_battery_sensor")
        if solar_to_battery_entity_id and isinstance(solar_to_battery_entity_id, str):
            sensor_state = self.hass.states.get(solar_to_battery_entity_id)
            if sensor_state and sensor_state.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    sensor_data["solar_to_battery_power"] = float(sensor_state.state)
                except (ValueError, TypeError):
                    pass
        if True:
            return sensor_data

    @staticmethod
    def _energy_state_to_kwh(state: Any) -> Optional[float]:
        """Normalize a Home Assistant energy state to canonical kWh."""
        try:
            energy_kwh = float(state.state)
        except (TypeError, ValueError):
            return
        if not math.isfinite(energy_kwh) or energy_kwh < 0:
            return None
        energy_unit = (
            str(
                (getattr(state, "attributes", {}) or {}).get("unit_of_measurement")
                or ""
            )
            .strip()
            .lower()
        )
        if energy_unit == "kwh":
            return energy_kwh
        if energy_unit == "wh":
            return energy_kwh / 1000.0
        if energy_unit == "mwh":
            return energy_kwh * 1000.0

    @callback
    def _midnight_task(self, now: datetime) -> None:
        """Handle midnight task. @zara"""
        self.hass.async_create_task(self._execute_midnight_task())

    async def _execute_midnight_task(self) -> None:
        """Execute midnight task. @zara"""
        try:
            _LOGGER.info("Executing midnight task")
            if hasattr(self.coordinator, "_peak_power_today"):
                self.coordinator._peak_power_today = 0.0
                self.coordinator._peak_time_today = None
                try:
                    date = dt_util.now().date().isoformat()
                    await self.coordinator.data_manager._db_manager.execute(
                        "UPDATE production_time_state\n                           SET peak_power_w = 0, peak_power_time = NULL,\n                               date = ?, last_updated = ?\n                           WHERE id = 1",
                        (date, dt_util.now().isoformat()),
                    )
                except Exception:
                    pass
                _LOGGER.debug("Reset daily peak tracking")
            await self.coordinator.reset_expected_daily_consumption()
            await self._schedule_dynamic_tasks_for_today()
            if self._kalman_tracker:
                self._kalman_tracker.reset()
        except Exception as error:
            _LOGGER.error("Midnight task failed: %s", error, exc_info=True)
        if True:
            return None

    @callback
    def _early_morning_forecast(self, now: datetime) -> None:
        """Handle early morning forecast task at 00:30. @zara"""
        _LOGGER.info(
            "=== EARLY MORNING FORECAST SCHEDULER TRIGGERED at %s ===",
            now.strftime("%H:%M:%S") if now else "unknown",
        )
        _LOGGER.info("Creating early morning forecast task...")
        self.hass.async_create_task(self._execute_early_morning_forecast())

    async def _execute_early_morning_forecast(self) -> None:
        """Execute early morning forecast at 00:30. @zara"""
        _LOGGER.info(
            ">>> STARTING EARLY MORNING FORECAST (00:30 scheduled execution) <<<"
        )
        _LOGGER.info("")
        _LOGGER.info(
            "╔══════════════════════════════════════════════════════════════════════════════╗"
        )
        _LOGGER.info(
            "║ EARLY MORNING FORECAST @ 00:30 - Initial daily forecast            ║"
        )
        _LOGGER.info(
            "╚══════════════════════════════════════════════════════════════════════════════╝"
        )
        try:
            result = await self._execute_morning_routine()
            if result:
                _LOGGER.info("  ✓✓✓ EARLY MORNING FORECAST COMPLETED SUCCESSFULLY! ✓✓✓")
                _LOGGER.info("")
            else:
                _LOGGER.error("  ✗✗✗ EARLY MORNING FORECAST FAILED - CHECK LOGS ABOVE")
        except Exception as error:
            _LOGGER.error("  ✗ Early morning forecast ERROR: %s", error, exc_info=True)
        return None if True else None

    @callback
    def _end_of_day_task(self, now: datetime) -> None:
        """Handle end-of-day task. @zara"""
        _LOGGER.info(
            "=== EOD SCHEDULER TRIGGERED at %s ===",
            now.strftime("%H:%M:%S") if now else "unknown",
        )
        if self._end_of_day_running:
            _LOGGER.warning("End-of-day task already running, skipping")
            return
        _LOGGER.info("Creating EOD workflow task...")
        self.hass.async_create_task(self._execute_end_of_day_task())

    async def end_of_day_workflow(self, now: datetime) -> None:
        """Consolidated End-of-Day Workflow at 23:40 - All tasks in sequence. @zara"""
        current_time = now if now is not None else dt_util.now()
        if self._end_of_day_running:
            _LOGGER.debug("END_OF_DAY_WORKFLOW already running - skipping")
            return
        self._end_of_day_running = True
        try:
            await self._end_of_day_workflow_internal(current_time)
        finally:
            self._end_of_day_running = False

    async def _execute_end_of_day_task(self) -> None:
        """Execute end-of-day processing (entry point from scheduled task). @zara"""
        _LOGGER.info(">>> STARTING EOD WORKFLOW (scheduled execution) <<<")
        await self.end_of_day_workflow(dt_util.now())

    async def _end_of_day_workflow_internal(self, current_time: datetime) -> None:
        """Internal implementation of end-of-day workflow (called with mutex held). @zara"""
        start_monotonic = asyncio.get_event_loop().time()
        _LOGGER.info(
            "================================================================================"
        )
        _LOGGER.info(
            "    END-OF-DAY WORKFLOW STARTED at %s",
            current_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        _LOGGER.info(
            "================================================================================"
        )
        success_count = 0
        total_steps = 15
        step_errors = []
        try:
            await self._finalize_day_internal(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 1 (finalize) failed: {error}", exc_info=True)
            step_errors.append(f"Finalize: {str(error)}")
        try:
            date = current_time.date().isoformat()
            await self._create_daily_summary(date)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 2 (summary) failed: {error}", exc_info=True)
            step_errors.append(f"Daily summary: {str(error)}")
        try:
            await self._move_to_history_internal(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 3 (history) failed: {error}", exc_info=True)
            step_errors.append(f"History: {str(error)}")
        try:
            await self._update_yesterday_deviation_internal(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 4 (deviation) failed: {error}", exc_info=True)
            step_errors.append(f"Deviation: {str(error)}")
        try:
            await self._calculate_stats_internal(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 5 (stats) failed: {error}", exc_info=True)
            step_errors.append(f"Statistics: {str(error)}")
        try:
            await self._calculate_weather_precision(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 6 (weather precision) failed: {error}", exc_info=True)
            step_errors.append(f"Weather precision: {str(error)}")
        try:
            await self._train_weather_mlp(current_time)
        except Exception as error:
            _LOGGER.error(f"Step 6b (Weather MLP) failed: {error}", exc_info=True)
            step_errors.append(f"Weather MLP: {str(error)}")
        try:
            await self._night_cleanup_internal(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 7 (cleanup) failed: {error}", exc_info=True)
            step_errors.append(f"Cleanup: {str(error)}")
        try:
            await self._train_ml_main_model(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 8 (AI model training) failed: {error}", exc_info=True)
            step_errors.append(f"AI model: {str(error)}")
        try:
            corrected_exclusions = await self._correct_snow_learning_exclusions(
                current_time
            )
            if corrected_exclusions > 0:
                _LOGGER.info(
                    "Snow learning correction: %d hours unblocked for learning",
                    corrected_exclusions,
                )
        except Exception as error:
            _LOGGER.warning("Snow learning correction failed: %s", error)
        try:
            await self._learn_method_performance(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 9 (Method Performance Learning) failed: {error}", exc_info=True
            )
            step_errors.append(f"Method Performance: {str(error)}")
        try:
            await self._train_panel_group_efficiency(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 10 (Panel Group Efficiency learning) failed: {error}",
                exc_info=True,
            )
            step_errors.append(f"Panel Group Efficiency: {str(error)}")
        try:
            await self._learn_group_method_performance(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 10b (Group Method Performance) failed: {error}", exc_info=True
            )
            step_errors.append(f"Group Method Performance: {str(error)}")
        try:
            await self._run_physics_calibration(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 11 (Physics Calibration) failed: {error}", exc_info=True
            )
            step_errors.append(f"Physics Calibration: {str(error)}")
        try:
            await self._run_weather_expert_learning(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 12 (Weather Expert Learning) failed: {error}", exc_info=True
            )
            step_errors.append(f"Weather Expert Learning: {str(error)}")
        try:
            await self._run_shadow_pattern_learning(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(
                f"Step 13 (Shadow Pattern Learning) failed: {error}", exc_info=True
            )
            step_errors.append(f"Shadow Pattern Learning: {str(error)}")
        try:
            await self._run_drift_detection(current_time)
            success_count += 1
        except Exception as error:
            _LOGGER.error(f"Step 14 (Drift Detection) failed: {error}", exc_info=True)
            step_errors.append(f"Drift Detection: {str(error)}")
        elapsed_seconds = asyncio.get_event_loop().time() - start_monotonic
        try:
            date_str = current_time.date().isoformat()
            elapsed_rounded = round(elapsed_seconds, 1)
            await self.db.execute(
                "UPDATE daily_summaries SET eod_duration_seconds = ?\n                   WHERE date = ?",
                (elapsed_rounded, date_str),
            )
            await self.db.commit()
            if self.coordinator:
                self.coordinator.eod_duration_seconds = elapsed_rounded
                self.coordinator.async_update_listeners()
            _LOGGER.info("EOD duration saved: %.1fs for %s", elapsed_rounded, date_str)
        except Exception as error:
            _LOGGER.warning("Could not save EOD duration: %s", error)
        if success_count == total_steps:
            _LOGGER.info(
                f"End-of-day workflow completed ({success_count}/{total_steps} steps, {elapsed_seconds:.1f}s)"
            )
            try:
                if self.coordinator:
                    self.coordinator.update_system_status(
                        event_type="end_of_day_workflow",
                        event_status="success",
                        event_summary="Tagesabschluss erfolgreich",
                        event_details={
                            "duration_seconds": round(elapsed_seconds, 1),
                            "steps_completed": f"{success_count}/{total_steps}",
                        },
                    )
            except Exception:
                pass
        else:
            _LOGGER.warning(
                f"End-of-day workflow partial ({success_count}/{total_steps}): {'; '.join(step_errors)}"
            )
            try:
                if self.coordinator:
                    self.coordinator.update_system_status(
                        event_type="end_of_day_workflow",
                        event_status="partial",
                        event_summary=f"Tagesabschluss teilweise ({success_count}/{total_steps})",
                        event_details={
                            "duration_seconds": round(elapsed_seconds, 1),
                            "steps_completed": f"{success_count}/{total_steps}",
                            "errors": step_errors,
                        },
                    )
            except Exception:
                pass
        return None if True else None

    async def _finalize_day_internal(self, now: datetime) -> None:
        """Internal: Finalize current day with actual values. @zara"""
        try:
            actual_daily_kwh = 0.0
            if self.wp_energy_today_entity_id:
                energy_state = self.hass.states.get(self.wp_energy_today_entity_id)
                if energy_state and energy_state.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        actual_daily_kwh = float(energy_state.state)
                    except (ValueError, TypeError):
                        pass
            total_consumption_kwh = None
            consumption_entity_id = getattr(
                self.coordinator, "total_consumption_today", None
            )
            if consumption_entity_id:
                consumption_state = self.hass.states.get(consumption_entity_id)
                if consumption_state and consumption_state.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        total_consumption_kwh = float(consumption_state.state)
                    except (ValueError, TypeError):
                        pass
            production_seconds = 0
            try:
                if (
                    hasattr(self.coordinator, "production_time_calculator")
                    and self.coordinator.production_time_calculator
                ):
                    production_hours = (
                        self.coordinator.production_time_calculator.get_production_hours()
                    )
                    if production_hours is not None:
                        production_seconds = int(production_hours * 3600)
            except Exception:
                pass
            exclusion_summary = None
            try:
                date_str = now.strftime("%Y-%m-%d")
                rows = await self.db.fetchall(
                    "SELECT exclude_from_learning, has_weather_alert, inverter_clipped,\n                              weather_alert_type, mppt_throttled\n                       FROM hourly_predictions\n                       WHERE target_date = ?",
                    (date_str,),
                )
                if rows:
                    excluded_count = 0
                    total_hours = len(rows)
                    reason_counts = {}
                    for row in rows:
                        excluded_from_learning = row[0]
                        weather_alert = row[1]
                        inverter_clipped = row[2]
                        weather_alert_type = row[3]
                        mppt_throttled = row[4]
                        if (
                            excluded_from_learning
                            or weather_alert
                            or inverter_clipped
                            or mppt_throttled
                        ):
                            excluded_count += 1
                            exclusion_reason = weather_alert_type or "other"
                            if inverter_clipped:
                                exclusion_reason = "inverter_clipped"
                            elif mppt_throttled:
                                exclusion_reason = "mppt_throttled"
                            reason_counts[exclusion_reason] = (
                                reason_counts.get(exclusion_reason, 0) + 1
                            )
                    exclusion_ratio = (
                        excluded_count / total_hours if total_hours > 0 else 0.0
                    )
                    exclusion_summary = {
                        "count": excluded_count,
                        "total": total_hours,
                        "ratio": round(exclusion_ratio, 3),
                        "reasons": reason_counts if reason_counts else None,
                    }
                    if excluded_count > 0:
                        _LOGGER.info(
                            f"Excluded hours info for {date_str}: {excluded_count}/{total_hours} ({exclusion_ratio:.1%}), reasons: {reason_counts}"
                        )
            except Exception as error:
                _LOGGER.debug(f"Could not calculate excluded hours info: {error}")
            accuracy_percent = None
            if actual_daily_kwh > 0:
                daily_forecast_row = await self.db.fetchone(
                    "SELECT prediction_kwh FROM daily_forecasts\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (now.date().isoformat(),),
                )
                if daily_forecast_row and daily_forecast_row[0]:
                    predicted_daily_kwh = daily_forecast_row[0]
                    absolute_error_kwh = abs(predicted_daily_kwh - actual_daily_kwh)
                    if actual_daily_kwh > 0.1:
                        accuracy_percent = max(
                            0.0, (1.0 - absolute_error_kwh / actual_daily_kwh) * 100.0
                        )
            tracking_data = {
                "date": now.date().isoformat(),
                "finalized": {
                    "yield_kwh": actual_daily_kwh,
                    "consumption_kwh": total_consumption_kwh,
                    "production_hours": (
                        f"{production_seconds // 3600}:{production_seconds % 3600 // 60:02d}"
                        if production_seconds > 0
                        else None
                    ),
                    "accuracy_percent": (
                        round(accuracy_percent, 1)
                        if accuracy_percent is not None
                        else None
                    ),
                    "excluded_hours": exclusion_summary,
                    "at": now.isoformat(),
                },
            }
            await self.db.save_daily_forecast_tracking(tracking_data)
            _LOGGER.info(
                f"Day finalized: yield={actual_daily_kwh:.2f} kWh, consumption={(total_consumption_kwh if total_consumption_kwh is not None else 0.0):.2f} kWh, accuracy={(accuracy_percent if accuracy_percent is not None else 0.0):.1f}%"
            )
        except Exception as error:
            _LOGGER.error(f"Failed to finalize day: {error}", exc_info=True)
        return None if True else None

    async def _move_to_history_internal(self, now: datetime) -> None:
        """Internal: Move current day to forecast history. @zara"""
        try:
            date = now.date().isoformat()
            tracking_data = await self.db.get_daily_forecast_tracking()
            if not tracking_data or tracking_data.get("date") != date:
                _LOGGER.warning(f"No finalized data for {date}, skipping history move")
                return
            finalized_data = tracking_data.get("finalized", {})
            if not finalized_data:
                _LOGGER.warning(f"Day {date} not finalized, skipping history move")
                return
            forecast_row = await self.db.fetchone(
                "SELECT prediction_kwh, source FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
            predicted_kwh = forecast_row[0] if forecast_row else None
            source_method = forecast_row[1] if forecast_row else None
            history_entry = {
                "date": date,
                "predicted_kwh": predicted_kwh,
                "actual_kwh": finalized_data.get("yield_kwh"),
                "consumption_kwh": finalized_data.get("consumption_kwh"),
                "autarky": None,
                "accuracy": finalized_data.get("accuracy_percent"),
                "production_hours": finalized_data.get("production_hours"),
                "peak_power": tracking_data.get("peak_today", {}).get("power_w"),
                "source": source_method,
                "excluded_hours": finalized_data.get("excluded_hours"),
                "archived_at": now.isoformat(),
            }
            actual_kwh = history_entry["actual_kwh"] or 0
            consumption_kwh = history_entry["consumption_kwh"] or 0
            if consumption_kwh > 0:
                history_entry["autarky"] = round(actual_kwh / consumption_kwh * 100, 1)
            await self.db.save_forecast_history_entry(history_entry)
            _LOGGER.info(
                "Day %s moved to history: %.2f → %.2f kWh",
                date,
                predicted_kwh or 0.0,
                actual_kwh,
            )
        except Exception as error:
            _LOGGER.error(f"Failed to move to history: {error}", exc_info=True)
        return None if True else None

    async def _update_yesterday_deviation_internal(self, now: datetime) -> None:
        """Internal: Update yesterday deviation after moving to history. @zara"""
        try:
            yesterday = (now.date() - timedelta(days=1)).isoformat()
            history_row = await self.db.fetchone(
                "SELECT predicted_kwh, actual_kwh FROM forecast_history\n                   WHERE date = ?",
                (yesterday,),
            )
            if not history_row:
                self.coordinator.last_day_error_kwh = 0.0
                self.coordinator.yesterday_accuracy = 0.0
                return
            predicted_kwh = history_row[0] or 0.0
            actual_kwh = history_row[1] or 0.0
            absolute_error_kwh = abs(predicted_kwh - actual_kwh)
            accuracy_ratio = 0.0
            if actual_kwh > 0.1:
                relative_error = absolute_error_kwh / actual_kwh
                accuracy_ratio = max(0.0, 1.0 - relative_error)
            elif predicted_kwh < 0.1 and actual_kwh < 0.1:
                accuracy_ratio = 1.0
            self.coordinator.last_day_error_kwh = round(absolute_error_kwh, 2)
            self.coordinator.yesterday_accuracy = round(accuracy_ratio * 100.0, 1)
            self.coordinator.async_update_listeners()
        except Exception as error:
            _LOGGER.error(
                f"Failed to update yesterday deviation: {error}", exc_info=True
            )
            self.coordinator.last_day_error_kwh = 0.0
            self.coordinator.yesterday_accuracy = 0.0
        return None if True else None

    async def _calculate_stats_internal(self, now: datetime) -> None:
        """Internal: Calculate statistics (all-time peak, current week/month, last N days). @zara"""
        try:
            statistics = {}
            peak_row = await self.db.fetchone(
                "SELECT MAX(peak_power), date FROM forecast_history\n                   WHERE peak_power IS NOT NULL"
            )
            if peak_row and peak_row[0]:
                statistics["all_time_peak"] = {
                    "power_w": peak_row[0],
                    "date": str(peak_row[1]) if peak_row[1] else None,
                    "at": None,
                }
            date = now.date()
            week_start = date - timedelta(days=date.weekday())
            week_rows = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (week_start.isoformat(), date.isoformat()),
            )
            if week_rows:
                week_yield_kwh = sum((row[0] or 0 for row in week_rows))
                week_consumption_kwh = sum((row[1] or 0 for row in week_rows if row[1]))
                statistics["current_week"] = {
                    "period": f"KW {date.isocalendar()[1]}",
                    "date_range": f"{week_start.strftime('%d.%m')} - {date.strftime('%d.%m')}",
                    "yield_kwh": round(week_yield_kwh, 2),
                    "consumption_kwh": (
                        round(week_consumption_kwh, 2)
                        if week_consumption_kwh > 0
                        else None
                    ),
                    "days": len(week_rows),
                    "updated_at": now.isoformat(),
                }
            month_start = date.replace(day=1)
            month_rows = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh, autarky FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (month_start.isoformat(), date.isoformat()),
            )
            if month_rows:
                month_yield_kwh = sum((row[0] or 0 for row in month_rows))
                month_consumption_kwh = sum(
                    (row[1] or 0 for row in month_rows if row[1])
                )
                autarky_values = [row[2] for row in month_rows if row[2] is not None]
                mean_autarky = (
                    sum(autarky_values) / len(autarky_values)
                    if autarky_values
                    else None
                )
                statistics["current_month"] = {
                    "period": date.strftime("%B %Y"),
                    "yield_kwh": round(month_yield_kwh, 2),
                    "consumption_kwh": (
                        round(month_consumption_kwh, 2)
                        if month_consumption_kwh > 0
                        else None
                    ),
                    "avg_autarky": round(mean_autarky, 1) if mean_autarky else None,
                    "days": len(month_rows),
                    "updated_at": now.isoformat(),
                }
            week_cutoff = (date - timedelta(days=7)).isoformat()
            recent_week_rows = await self.db.fetchall(
                "SELECT actual_kwh, accuracy FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (week_cutoff, date.isoformat()),
            )
            if recent_week_rows:
                period_yield_kwh = sum((row[0] or 0 for row in recent_week_rows))
                accuracy_values = [
                    row[1] for row in recent_week_rows if row[1] is not None
                ]
                mean_accuracy = (
                    sum(accuracy_values) / len(accuracy_values)
                    if accuracy_values
                    else None
                )
                statistics["last_7_days"] = {
                    "avg_yield_kwh": round(period_yield_kwh / len(recent_week_rows), 2),
                    "avg_accuracy": round(mean_accuracy, 1) if mean_accuracy else None,
                    "total_yield_kwh": round(period_yield_kwh, 2),
                    "calculated_at": now.isoformat(),
                }
            month_cutoff = (date - timedelta(days=30)).isoformat()
            recent_month_rows = await self.db.fetchall(
                "SELECT actual_kwh, accuracy FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (month_cutoff, date.isoformat()),
            )
            if recent_month_rows:
                period_yield_kwh = sum((row[0] or 0 for row in recent_month_rows))
                accuracy_values = [
                    row[1] for row in recent_month_rows if row[1] is not None
                ]
                mean_accuracy = (
                    sum(accuracy_values) / len(accuracy_values)
                    if accuracy_values
                    else None
                )
                statistics["last_30_days"] = {
                    "avg_yield_kwh": round(
                        period_yield_kwh / len(recent_month_rows), 2
                    ),
                    "avg_accuracy": round(mean_accuracy, 1) if mean_accuracy else None,
                    "total_yield_kwh": round(period_yield_kwh, 2),
                    "calculated_at": now.isoformat(),
                }
            year_cutoff = (date - timedelta(days=365)).isoformat()
            recent_year_rows = await self.db.fetchall(
                "SELECT actual_kwh FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (year_cutoff, date.isoformat()),
            )
            if recent_year_rows:
                period_yield_kwh = sum((row[0] or 0 for row in recent_year_rows))
                statistics["last_365_days"] = {
                    "avg_yield_kwh": round(period_yield_kwh / len(recent_year_rows), 2),
                    "total_yield_kwh": round(period_yield_kwh, 2),
                    "calculated_at": now.isoformat(),
                }
            await self.db.save_daily_statistics(statistics)
            _LOGGER.info("Statistics calculated successfully")
        except Exception as error:
            _LOGGER.error(f"Failed to calculate statistics: {error}", exc_info=True)
        if True:
            return None

    async def _calculate_weather_precision(self, now: datetime) -> None:
        """Calculate weather precision factors (compare forecast vs actual weather). @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                _LOGGER.debug("Weather precision skipped: no weather_pipeline_manager")
                return
            weather_pipeline = self.coordinator.weather_pipeline_manager
            if not weather_pipeline:
                _LOGGER.debug("Weather precision skipped: pipeline is None")
                return
            if (
                not weather_pipeline.weather_precision
                or not weather_pipeline.weather_actual_tracker
                or (not weather_pipeline.weather_corrector)
            ):
                _LOGGER.debug(
                    "Weather precision skipped: required components not initialized"
                )
                return
            date = now.strftime("%Y-%m-%d")
            _LOGGER.info(f"Calculating weather precision for {date}")
            result = await weather_pipeline.weather_precision.calculate_correction_factors_v2(
                weather_actual_tracker=weather_pipeline.weather_actual_tracker,
                date_str=date,
            )
            if result:
                _LOGGER.info(f"Weather precision calculated for {date}")
            else:
                _LOGGER.debug(
                    f"Weather precision skipped for {date} (normal if no weather data collected that day)"
                )
        except Exception as error:
            _LOGGER.error(
                f"Weather precision calculation error: {error}", exc_info=True
            )
        return None if True else None

    async def _night_cleanup_internal(self, now: datetime) -> None:
        """Night cleanup: VACUUM DISABLED to prevent data loss. @zara

        VACUUM was causing DB shrinkage and potential data loss.
        Database optimization is handled by SQLite automatically.
        """
        try:
            _LOGGER.info("Night cleanup: VACUUM disabled (DB auto-optimizes)")
        except Exception as error:
            _LOGGER.error(f"Night cleanup error: {error}", exc_info=True)
        return None if True else None

    async def _train_ml_main_model(self, now: datetime) -> None:
        """Train the AI model. @zara"""
        try:
            if not self.coordinator or not self.coordinator.ai_predictor:
                _LOGGER.debug("AI model training skipped: no predictor available")
                return
            panel_group_reader = getattr(
                self.coordinator, "panel_group_sensor_reader", None
            )
            if panel_group_reader:
                try:
                    backfilled_count = (
                        await panel_group_reader.backfill_missing_actuals_from_recorder(
                            days=30
                        )
                    )
                    if backfilled_count > 0:
                        _LOGGER.info(
                            "Backfilled %d per-group actuals from recorder before training",
                            backfilled_count,
                        )
                except Exception as error:
                    _LOGGER.warning("Recorder backfill failed: %s", error)
            ai_predictor = self.coordinator.ai_predictor
            result = await ai_predictor.train_model()
            if result.success:
                attention_note = ", attention=ON" if result.has_attention else ""
                rmse_note = f", RMSE={result.rmse:.3f}kWh" if result.rmse else ""
                _LOGGER.info(
                    f"AI model trained: R²={result.accuracy:.3f}{rmse_note}, samples={result.samples_used}, features={result.feature_count}, outputs={result.num_outputs}{attention_note}"
                )
                if self.coordinator:
                    self.coordinator.on_ai_training_complete(
                        timestamp=dt_util.now(), accuracy=result.accuracy
                    )
            elif result.reason_code == REASON_RIDGE_WARMUP:
                _LOGGER.info(
                    "AI model warm-up in progress: %d/%d valid Ridge samples",
                    result.samples_used,
                    MIN_RIDGE_SAMPLES,
                )
            elif result.error_message:
                _LOGGER.warning(f"AI model training incomplete: {result.error_message}")
            else:
                _LOGGER.debug("AI model training needs more data")
            if ai_predictor.dni_tracker:
                try:
                    await ai_predictor.dni_tracker.end_of_day_update()
                    ai_predictor.dni_tracker.start_new_day()
                except Exception as error:
                    _LOGGER.warning(f"DNI tracker finalize error: {error}")
        except Exception as error:
            _LOGGER.error(f"AI model training error: {error}", exc_info=True)
        return None if True else None

    async def _correct_snow_learning_exclusions(self, now: datetime) -> int:
        """Correct exclude_from_learning for snow-flagged hours with actual production. @zara

        When the snow tracker marks hours as covered but actual production shows panels
        were clearly producing, the exclusion was wrong. Clear it before learning runs.

        Uses production vs theoretical_max_kwh (astronomy) as evidence:
        - actual > 25% of theoretical = panels were NOT covered
        - Safe threshold: real snow coverage typically yields < 10% theoretical

        Returns number of corrected hours.
        """
        try:
            database = self.db
            date = now.date().isoformat()
            cutoff_date = (now - timedelta(days=7)).date().isoformat()
            rows = await database.fetchall(
                "SELECT hp.prediction_id, hp.target_hour, hp.actual_kwh,\n                          pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_astronomy pa ON pa.prediction_id = hp.prediction_id\n                   WHERE hp.target_date >= ?\n                     AND hp.target_date <= ?\n                     AND hp.weather_alert_type = 'snow_covered_panels'\n                     AND hp.exclude_from_learning = 1\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.actual_kwh > 0\n                     AND pa.theoretical_max_kwh > 0.05",
                (cutoff_date, date),
            )
            if not rows:
                return 0
            corrected_count = 0
            for prediction_id, hour, actual_kwh, theoretical_kwh in rows:
                exclusion_ratio = (
                    actual_kwh / theoretical_kwh if theoretical_kwh > 0 else 0
                )
                if exclusion_ratio > 0.25:
                    await database.execute(
                        "UPDATE hourly_predictions\n                           SET exclude_from_learning = 0,\n                               has_weather_alert = 0,\n                               weather_alert_type = NULL\n                           WHERE prediction_id = ?",
                        (prediction_id,),
                    )
                    corrected_count += 1
                    _LOGGER.info(
                        "Snow exclusion corrected H%02d: actual=%.3f kWh (%.0f%% of theoretical) — panels were producing",
                        hour,
                        actual_kwh,
                        exclusion_ratio * 100,
                    )
            if corrected_count > 0:
                await database.commit()
                _LOGGER.info(
                    "Corrected %d snow-excluded hours with actual production evidence",
                    corrected_count,
                )
            return corrected_count
        except Exception as error:
            _LOGGER.warning("Snow learning exclusion correction failed: %s", error)
            return 0

    async def _learn_method_performance(self, now: datetime) -> None:
        """Learn physics vs AI accuracy per condition bucket. @zara V16.0.0

        Compares physics_kwh, ai_kwh, prediction_kwh (blend) against actual_kwh
        for each hour of the day. Updates method_performance_learning table
        with smoothed MAE values and derived ai_advantage_factor.
        """
        force_learning = False
        try:
            if not self.coordinator or not self.coordinator.data_manager:
                _LOGGER.debug("Method performance learning skipped: no data manager")
                return
            database = self.db
            date = now.date().isoformat()
            rows = await database.fetchall(
                "SELECT hp.target_hour,\n                          CASE WHEN hp.adaptive_corrected = TRUE\n                               THEN hp.prediction_kwh_uncapped\n                               ELSE hp.prediction_kwh END AS blend_kwh,\n                          hp.actual_kwh,\n                          hp.physics_kwh, hp.ai_kwh, hp.ai_confidence,\n                          pw.clouds\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)",
                (date,),
            )
            if not rows:
                _LOGGER.info(
                    "Method performance learning: No data with raw values for %s", date
                )
                return
            grouped_errors = {}
            for row in rows:
                hour = row[0]
                blended_kwh = row[1]
                actual_kwh = row[2]
                physics_kwh = row[3]
                ai_kwh = row[4]
                cloud_cover = row[6] or 50
                if actual_kwh is None or actual_kwh <= 0:
                    continue
                weather_type = (
                    "clear"
                    if cloud_cover < 30
                    else "partly_cloudy" if cloud_cover < 70 else "overcast"
                )
                time_period = (
                    "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
                )
                group_key = (weather_type, time_period)
                if group_key not in grouped_errors:
                    grouped_errors[group_key] = {"physics": [], "ai": [], "blend": []}
                grouped_errors[group_key]["physics"].append(
                    abs(physics_kwh - actual_kwh)
                )
                grouped_errors[group_key]["blend"].append(abs(blended_kwh - actual_kwh))
                if ai_kwh is not None and ai_kwh > 0:
                    grouped_errors[group_key]["ai"].append(abs(ai_kwh - actual_kwh))
            if not grouped_errors:
                _LOGGER.info(
                    "Method performance learning: No valid production hours for %s",
                    date,
                )
                return
            learning_rate = 0.3
            season = get_season(now.month)
            for (weather_type, time_period), error_samples in grouped_errors.items():
                physics_mae = sum(error_samples["physics"]) / len(
                    error_samples["physics"]
                )
                blend_mae = sum(error_samples["blend"]) / len(error_samples["blend"])
                ai_mae = (
                    sum(error_samples["ai"]) / len(error_samples["ai"])
                    if error_samples["ai"]
                    else None
                )
                for seasonal_scope in (None, season):
                    previous_metrics = await database.fetchone(
                        "SELECT physics_mae, ai_mae, blend_mae, sample_count\n                           FROM method_performance_learning\n                           WHERE cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS ?",
                        (weather_type, time_period, seasonal_scope),
                    )
                    if (
                        previous_metrics
                        and previous_metrics[3]
                        and (previous_metrics[3] > 0)
                    ):
                        smoothed_physics_mae = (
                            learning_rate * physics_mae
                            + (1 - learning_rate) * previous_metrics[0]
                        )
                        smoothed_ai_mae = (
                            learning_rate * ai_mae
                            + (1 - learning_rate) * previous_metrics[1]
                            if ai_mae and previous_metrics[1]
                            else ai_mae
                        )
                        smoothed_blend_mae = (
                            learning_rate * blend_mae
                            + (1 - learning_rate) * previous_metrics[2]
                        )
                        sample_count = previous_metrics[3] + len(
                            error_samples["physics"]
                        )
                    else:
                        smoothed_physics_mae = physics_mae
                        smoothed_ai_mae = ai_mae
                        smoothed_blend_mae = blend_mae
                        sample_count = len(error_samples["physics"])
                    if (
                        smoothed_ai_mae
                        and smoothed_ai_mae > 0
                        and (smoothed_physics_mae > 0)
                    ):
                        relative_mae = smoothed_physics_mae / smoothed_ai_mae
                        correction_factor = max(0.5, min(2.0, relative_mae))
                    else:
                        correction_factor = 1.0
                    update_parameters = (
                        weather_type,
                        time_period,
                        seasonal_scope,
                        round(smoothed_physics_mae, 6),
                        round(smoothed_ai_mae, 6) if smoothed_ai_mae else None,
                        round(smoothed_blend_mae, 6),
                        round(correction_factor, 4),
                        sample_count,
                        now,
                    )
                    if seasonal_scope is None:
                        async with database.transaction():
                            await database.execute(
                                "DELETE FROM method_performance_learning\n                                   WHERE cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (weather_type, time_period),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO method_performance_learning\n                                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                    ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                update_parameters,
                                auto_commit=False,
                            )
                    else:
                        await database.execute(
                            "INSERT INTO method_performance_learning\n                               (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                                ai_advantage_factor, sample_count, last_updated)\n                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                   physics_mae = excluded.physics_mae,\n                                   ai_mae = excluded.ai_mae,\n                                   blend_mae = excluded.blend_mae,\n                                   ai_advantage_factor = excluded.ai_advantage_factor,\n                                   sample_count = excluded.sample_count,\n                                   last_updated = excluded.last_updated",
                            update_parameters,
                            auto_commit=True,
                        )
                _LOGGER.info(
                    "Method learning [%s/%s]: physics_mae=%.4f, ai_mae=%s, advantage=%.2f (%d samples)",
                    weather_type,
                    time_period,
                    smoothed_physics_mae,
                    f"{smoothed_ai_mae:.4f}" if smoothed_ai_mae else "N/A",
                    correction_factor,
                    sample_count,
                )
            _LOGGER.info("Method performance learning completed for %s", date)
            force_learning = True
        except Exception as error:
            _LOGGER.error(f"Method performance learning error: {error}", exc_info=True)
            return
        if force_learning:
            await self._calibrate_physics_from_learned_day(now)
        if True:
            return None
        else:
            return None

    async def _calibrate_physics_from_learned_day(self, now: datetime) -> None:
        """Feed the shared calibrator after method-performance learning.

        Uses the same day and row filter as method learning, and only hours
        that have stored weather. A day above the heating limit learns
        hot-water physics into the DHW-only bucket, or, without hot water,
        the measured kWh into the above-limit counter. A day below the limit
        learns unsuppressed physics and the U-factor. A calibrator failure is
        logged and does not affect the method rows.
        """
        try:
            calibrator = getattr(self.coordinator, "thermodynamics_calibrator", None)
            if not calibrator:
                _LOGGER.debug("Physics hour calibration skipped: no calibrator")
                return
            orchestrator = getattr(self.coordinator, "forecast_orchestrator", None)
            rule_strategy = getattr(orchestrator, "rule_based_strategy", None)
            if rule_strategy is None:
                _LOGGER.debug("Physics hour calibration skipped: no forecast strategy")
                return
            thermodynamics_engine = getattr(
                rule_strategy, "_thermodynamics_engine", None
            )
            if thermodynamics_engine is None:
                if hasattr(rule_strategy, "_ensure_components_initialized"):
                    await rule_strategy._ensure_components_initialized()
                    thermodynamics_engine = getattr(
                        rule_strategy, "_thermodynamics_engine", None
                    )
            if thermodynamics_engine is None:
                _LOGGER.debug(
                    "Physics hour calibration skipped: no thermodynamics engine"
                )
                return
            indoor_temperature = rule_strategy._configured_indoor_temp()
            sink_kwargs = rule_strategy._cop_sink_kwargs()
            dhw_kwargs = rule_strategy._dhw_kwargs()
            has_dhw = bool(dhw_kwargs.get("has_dhw"))
            date = now.date().isoformat()
            rows = await self.db.fetchall(
                "SELECT hp.target_hour,\n                          hp.actual_kwh,\n                          COALESCE(pwa.temperature, pwf.temperature),\n                          COALESCE(pwa.wind, pwf.wind),\n                          COALESCE(pwa.humidity, pwf.humidity)\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwa\n                       ON hp.prediction_id = pwa.prediction_id\n                       AND pwa.weather_type = 'actual'\n                   LEFT JOIN prediction_weather pwf\n                       ON hp.prediction_id = pwf.prediction_id\n                       AND pwf.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE\n                          OR hp.exclude_from_learning IS NULL)",
                (date,),
            )
            from ..thermodynamics.thermodynamics_engine import (
                learn_consumption_hour as learn_consumption_hour,
                space_heating_suppressed_by_day as space_heating_suppressed_by_day,
            )

            forecast_timezone = rule_strategy._forecast_timezone()
            forecast_day = now.date()
            parsed_hours = []
            outdoor_series = []
            for row in rows:
                parsed_hour = self._prepared_learned_hour(row)
                if not parsed_hour is not None:
                    continue
                hour, actual_kwh, outdoor_temperature, wind_speed, relative_humidity = (
                    parsed_hour
                )
                if not 0 <= hour <= 23:
                    continue
                target_datetime = datetime(
                    forecast_day.year,
                    forecast_day.month,
                    forecast_day.day,
                    hour,
                    tzinfo=forecast_timezone,
                )
                outdoor_series.append(
                    (target_datetime, outdoor_temperature, wind_speed)
                )
                parsed_hours.append(
                    (
                        hour,
                        actual_kwh,
                        outdoor_temperature,
                        wind_speed,
                        relative_humidity,
                    )
                )
            suppression_by_day = space_heating_suppressed_by_day(
                thermodynamics_engine, outdoor_series, forecast_timezone
            )
            above_heating_limit = bool(suppression_by_day.get(forecast_day, False))
            learned_factors = []
            for (
                hour,
                actual_kwh,
                outdoor_temperature,
                wind_speed,
                relative_humidity,
            ) in parsed_hours:
                try:
                    heating_limit_lifted = False
                    if above_heating_limit:
                        calibrated_suppression_by_day = space_heating_suppressed_by_day(
                            thermodynamics_engine,
                            outdoor_series,
                            forecast_timezone,
                            calibrator=calibrator,
                            has_dhw=has_dhw,
                        )
                        heating_limit_lifted = not bool(
                            calibrated_suppression_by_day.get(forecast_day, True)
                        )
                    learned_factor = await learn_consumption_hour(
                        calibrator,
                        thermodynamics_engine,
                        above_heating_limit=above_heating_limit,
                        has_dhw=has_dhw,
                        actual_kwh=actual_kwh,
                        outdoor_temp=outdoor_temperature,
                        indoor_temp=indoor_temperature,
                        wind_speed=wind_speed,
                        humidity=relative_humidity,
                        hour=hour,
                        sink_kwargs=sink_kwargs,
                        dhw_kwargs=dhw_kwargs,
                        limit_lifted=heating_limit_lifted,
                    )
                except Exception as hour_error:
                    _LOGGER.warning(
                        "Physics hour calibration skipped a row: %s", hour_error
                    )
                    continue
                if learned_factor is not None:
                    learned_factors.append(learned_factor)
            mean_learned_factor = None
            if above_heating_limit:
                if not has_dhw:
                    if learned_factors:
                        mean_learned_factor = sum(learned_factors) / len(
                            learned_factors
                        )
                        await calibrator.record_above_limit_day(mean_learned_factor)
            daily_heating_limit_lifted = False
            if above_heating_limit:
                calibrated_suppression_by_day = space_heating_suppressed_by_day(
                    thermodynamics_engine,
                    outdoor_series,
                    forecast_timezone,
                    calibrator=calibrator,
                    has_dhw=has_dhw,
                )
                daily_heating_limit_lifted = not bool(
                    calibrated_suppression_by_day.get(forecast_day, True)
                )
            await self._record_daily_consumption_ratio(
                calibrator,
                thermodynamics_engine,
                rule_strategy,
                forecast_day,
                parsed_hours,
                above_limit=above_heating_limit,
                limit_lifted=daily_heating_limit_lifted,
                indoor_temp=indoor_temperature,
                sink_kwargs=sink_kwargs,
                dhw_kwargs=dhw_kwargs,
            )
            if above_heating_limit:
                note_suppressible_day = getattr(
                    calibrator, "note_suppressible_day", None
                )
                if note_suppressible_day is not None:
                    await note_suppressible_day(has_dhw, mean_learned_factor)
        except Exception as error:
            _LOGGER.warning("Physics hour calibration failed: %s", error)
        return None if True else None

    async def _record_daily_consumption_ratio(
        self,
        calibrator: Any,
        engine: Any,
        strategy: Any,
        day: Any,
        parsed: list[tuple],
        *,
        above_limit: bool,
        limit_lifted: bool,
        indoor_temp: float,
        sink_kwargs: dict,
        dhw_kwargs: dict,
    ) -> None:
        """One day ratio at the U the forecast is already using.

        Days the heating limit still suppresses stay on the DHW evidence path.
        A missing hour rejects the day.
        """
        record_daily_ratio = getattr(calibrator, "record_daily_ratio", None)
        if not record_daily_ratio is not None or (above_limit and (not limit_lifted)):
            return
        from ..thermodynamics.thermodynamics_engine import (
            WeatherConditions as weather_conditions_class,
        )

        actual_by_hour = [None] * 24
        temperature_by_hour = [None] * 24
        wind_by_hour = [None] * 24
        prediction_by_hour = [None] * 24
        for (
            hour,
            actual_kwh,
            outdoor_temperature,
            wind_speed,
            relative_humidity,
        ) in parsed:
            if not 0 <= hour <= 23:
                continue
            actual_by_hour[hour] = actual_kwh
            temperature_by_hour[hour] = outdoor_temperature
            wind_by_hour[hour] = wind_speed
            weather_conditions = weather_conditions_class(
                outdoor_temp=outdoor_temperature,
                wind_speed=wind_speed,
                humidity=relative_humidity,
            )
            try:
                thermal_result = engine.calculate_hourly_consumption(
                    weather_conditions,
                    indoor_temp=indoor_temp,
                    hour=hour,
                    suppress_space_heating=False,
                    **sink_kwargs,
                    **dhw_kwargs,
                )
                prediction_by_hour[hour] = float(thermal_result.consumption_kwh)
            except Exception:
                prediction_by_hour[hour] = None
                continue
        try:
            heating_capacity_kw = float(getattr(strategy, "heating_capacity_kw", 0.0))
        except (TypeError, ValueError):
            heating_capacity_kw = 0.0
        try:
            await record_daily_ratio(
                day=day,
                actual_kwh_by_hour=actual_by_hour,
                physics_kwh_by_hour=prediction_by_hour,
                outdoor_c_by_hour=temperature_by_hour,
                wind_m_s_by_hour=wind_by_hour,
                heating_capacity_kw=heating_capacity_kw,
            )
        except Exception as error:
            _LOGGER.warning("Daily consumption ratio was not recorded: %s", error)
        return None if True else None

    async def migrate_daily_consumption_model(self) -> dict:
        """One-shot relearn from stored hours, then rewrite unlocked future hours.

        Too little history changes nothing and leaves the marker unset so a
        later start can try again. A set marker returns without another pass.
        """
        try:
            calibrator = getattr(self.coordinator, "thermodynamics_calibrator", None)
            if calibrator is None or not hasattr(calibrator, "run_history_migration"):
                return {"ran": False, "reason": "no_calibrator", "elapsed_s": 0.0}
            if await calibrator.migration_already_done():
                return {"ran": False, "reason": "already", "elapsed_s": 0.0}
            rows = await self.db.fetchall(
                "SELECT hp.target_date, hp.target_hour, hp.actual_kwh, hp.physics_kwh,\n                          COALESCE(pwa.temperature, pwf.temperature),\n                          COALESCE(pwa.wind, pwf.wind, 0),\n                          hp.exclude_from_learning\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwa\n                       ON hp.prediction_id = pwa.prediction_id\n                       AND pwa.weather_type = 'actual'\n                   LEFT JOIN prediction_weather pwf\n                       ON hp.prediction_id = pwf.prediction_id\n                       AND pwf.weather_type = 'forecast'\n                   WHERE hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL"
            )
            orchestrator = getattr(self.coordinator, "forecast_orchestrator", None)
            rule_strategy = getattr(orchestrator, "rule_based_strategy", None)
            try:
                heating_capacity_kw = float(
                    getattr(rule_strategy, "heating_capacity_kw", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                heating_capacity_kw = 0.0
            result = await calibrator.run_history_migration(
                list(rows or []), heating_capacity_kw=heating_capacity_kw
            )
            if result.get("ran"):
                result["rewritten_hours"] = await self.rewrite_unlocked_future_hours()
            return result
        except Exception as error:
            _LOGGER.warning("Daily consumption migration failed: %s", error)
            return {"ran": False, "reason": "error", "elapsed_s": 0.0}

    async def rewrite_unlocked_future_hours(self) -> int:
        """Rewrite future hours that are not on a locked daily forecast.

        A missing strategy or a query error leaves the stored hours unchanged.
        """
        try:
            orchestrator = getattr(self.coordinator, "forecast_orchestrator", None)
            rule_strategy = getattr(orchestrator, "rule_based_strategy", None)
            if rule_strategy is None or self.db is None:
                return 0
            from datetime import datetime as datetime_factory

            date = datetime_factory.now().date().isoformat()
            locked_forecast_rows = await self.db.fetchall(
                "SELECT forecast_date FROM daily_forecasts\n                   WHERE locked = 1 OR locked = TRUE"
            )
            locked_dates = {
                str(row[0]) for row in locked_forecast_rows or [] if row if row[0]
            }
            rows = await self.db.fetchall(
                "SELECT hp.prediction_id, hp.target_date, hp.target_hour,\n                          COALESCE(pwf.temperature, pwa.temperature),\n                          COALESCE(pwf.wind, pwa.wind, 0)\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwa\n                       ON hp.prediction_id = pwa.prediction_id\n                       AND pwa.weather_type = 'actual'\n                   LEFT JOIN prediction_weather pwf\n                       ON hp.prediction_id = pwf.prediction_id\n                       AND pwf.weather_type = 'forecast'\n                   WHERE hp.target_date >= ?",
                (date,),
            )
            from ..thermodynamics.thermodynamics_calibrator import (
                selectable_future_hours as selectable_future_hours,
            )

            selected_rows = selectable_future_hours(
                list(rows or []), locked_dates, date
            )
            if not selected_rows:
                return 0
            thermodynamics_engine = getattr(
                rule_strategy, "_thermodynamics_engine", None
            )
            calibrator = getattr(rule_strategy, "_thermodynamics_calibrator", None)
            if thermodynamics_engine is None:
                return 0
            from ..thermodynamics.thermodynamics_engine import (
                WeatherConditions as weather_conditions_class,
            )

            indoor_temperature = rule_strategy._configured_indoor_temp()
            sink_kwargs = rule_strategy._cop_sink_kwargs()
            dhw_kwargs = rule_strategy._dhw_kwargs()
            rewritten_count = 0
            for (
                prediction_id,
                forecast_date,
                hour,
                outdoor_temperature,
                wind_value,
            ) in selected_rows:
                if outdoor_temperature is None:
                    continue
                try:
                    target_hour = int(hour)
                    weather_conditions = weather_conditions_class(
                        outdoor_temp=float(outdoor_temperature),
                        wind_speed=float(wind_value or 0.0),
                        humidity=70.0,
                    )
                    result = thermodynamics_engine.calculate_hourly_consumption(
                        weather_conditions,
                        indoor_temp=indoor_temperature,
                        hour=target_hour,
                        suppress_space_heating=False,
                        **sink_kwargs,
                        **dhw_kwargs,
                    )
                    prediction_kwh = float(result.consumption_kwh)
                    if calibrator is not None:
                        correction_factor = calibrator.get_correction_factor(
                            float(outdoor_temperature),
                            float(wind_value or 0.0),
                            target_hour,
                        )
                        if (
                            correction_factor is not None
                            and float(correction_factor.confidence) > 0.3
                        ):
                            prediction_kwh *= float(correction_factor.correction_factor)
                except (TypeError, ValueError):
                    continue
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET physics_kwh = ?, prediction_kwh = ?\n                       WHERE prediction_id = ?",
                    (
                        round(prediction_kwh, 4),
                        round(max(0.0, prediction_kwh), 4),
                        prediction_id,
                    ),
                )
                rewritten_count += 1
                continue
            return rewritten_count
        except Exception as error:
            _LOGGER.warning("Future-hour rewrite skipped: %s", error)
            return 0

    @staticmethod
    def _prepared_learned_hour(
        row: Any,
    ) -> tuple[int, float, float, float, float] | None:
        """Hour, actual kWh, outdoor temperature, wind and humidity, or None."""
        try:
            hour = int(row[0])
            actual_kwh = float(row[1])
        except (TypeError, ValueError, IndexError):
            return
        temperature_value = row[2]
        if temperature_value is None:
            return
        try:
            outdoor_temperature = float(temperature_value)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(outdoor_temperature) and math.isfinite(actual_kwh)):
            return None
        wind_value = row[3]
        if wind_value is None:
            wind_speed = 3.0
        else:
            try:
                wind_speed = float(wind_value)
            except (TypeError, ValueError):
                wind_speed = 3.0
            if not math.isfinite(wind_speed):
                wind_speed = 3.0
        humidity = row[4]
        try:
            relative_humidity = 70.0 if humidity is None else float(humidity)
        except (TypeError, ValueError):
            relative_humidity = 70.0
        if not math.isfinite(relative_humidity):
            relative_humidity = 70.0
        return (hour, actual_kwh, outdoor_temperature, wind_speed, relative_humidity)

    async def _train_panel_group_efficiency(self, now: datetime) -> None:
        """Learn per-group ensemble weights (LSTM vs Ridge) per condition bucket. @zara V16.0.3

        For each panel group, computes LSTM and Ridge MAE against actual production,
        bucketed by cloud cover and hour of day. Derives inverse-MAE weights with
        EMA smoothing. Silent no-op when lstm_kwh/ridge_kwh data is not yet available.
        """
        try:
            database = self.db
            date = now.date().isoformat()
            rows = await database.fetchall(
                "SELECT ppg.group_name, hp.target_hour,\n                          ppg.lstm_kwh, ppg.ridge_kwh, ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.lstm_kwh IS NOT NULL AND ppg.ridge_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)",
                (date,),
            )
            if not rows:
                _LOGGER.debug(
                    "Ensemble group weight learning: No data with lstm/ridge/actual for %s",
                    date,
                )
                return
            grouped_errors = {}
            for row in rows:
                group_name = row[0]
                hour = row[1]
                lstm_kwh = row[2]
                ridge_kwh = row[3]
                actual_kwh = row[4]
                cloud_cover = row[5] or 50
                weather_type = (
                    "clear"
                    if cloud_cover < 30
                    else "partly_cloudy" if cloud_cover < 70 else "overcast"
                )
                time_period = (
                    "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
                )
                group_key = (group_name, weather_type, time_period)
                if group_key not in grouped_errors:
                    grouped_errors[group_key] = {"lstm": [], "ridge": []}
                grouped_errors[group_key]["lstm"].append(abs(lstm_kwh - actual_kwh))
                grouped_errors[group_key]["ridge"].append(abs(ridge_kwh - actual_kwh))
            learning_rate = 0.3
            minimum_lstm_weight = 0.2
            maximum_lstm_weight = 0.95
            season = get_season(now.month)
            updated_count = 0
            for (
                group_name,
                weather_type,
                time_period,
            ), error_samples in grouped_errors.items():
                lstm_mae = sum(error_samples["lstm"]) / len(error_samples["lstm"])
                ridge_mae = sum(error_samples["ridge"]) / len(error_samples["ridge"])
                for seasonal_scope in (None, season):
                    previous_metrics = await database.fetchone(
                        "SELECT lstm_mae, ridge_mae, sample_count\n                           FROM ensemble_group_weights\n                           WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS ?",
                        (group_name, weather_type, time_period, seasonal_scope),
                    )
                    if (
                        previous_metrics
                        and previous_metrics[2]
                        and (previous_metrics[2] > 0)
                    ):
                        smoothed_lstm_mae = (
                            learning_rate * lstm_mae
                            + (1 - learning_rate) * previous_metrics[0]
                        )
                        smoothed_ridge_mae = (
                            learning_rate * ridge_mae
                            + (1 - learning_rate) * previous_metrics[1]
                        )
                        sample_count = previous_metrics[2] + len(error_samples["lstm"])
                    else:
                        smoothed_lstm_mae = lstm_mae
                        smoothed_ridge_mae = ridge_mae
                        sample_count = len(error_samples["lstm"])
                    bounded_lstm_mae = max(0.001, smoothed_lstm_mae)
                    bounded_ridge_mae = max(0.001, smoothed_ridge_mae)
                    inverse_mae_total = 1.0 / bounded_lstm_mae + 1.0 / bounded_ridge_mae
                    raw_lstm_weight = 1.0 / bounded_lstm_mae / inverse_mae_total
                    lstm_weight = max(
                        minimum_lstm_weight, min(maximum_lstm_weight, raw_lstm_weight)
                    )
                    ridge_weight = 1.0 - lstm_weight
                    update_parameters = (
                        group_name,
                        weather_type,
                        time_period,
                        seasonal_scope,
                        round(lstm_weight, 4),
                        round(ridge_weight, 4),
                        round(smoothed_lstm_mae, 6),
                        round(smoothed_ridge_mae, 6),
                        sample_count,
                        now,
                    )
                    if seasonal_scope is None:
                        async with database.transaction():
                            await database.execute(
                                "DELETE FROM ensemble_group_weights\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (group_name, weather_type, time_period),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                update_parameters,
                                auto_commit=False,
                            )
                    else:
                        await database.execute(
                            "INSERT INTO ensemble_group_weights\n                               (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                lstm_mae, ridge_mae, sample_count, last_updated)\n                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                   lstm_weight = excluded.lstm_weight,\n                                   ridge_weight = excluded.ridge_weight,\n                                   lstm_mae = excluded.lstm_mae,\n                                   ridge_mae = excluded.ridge_mae,\n                                   sample_count = excluded.sample_count,\n                                   last_updated = excluded.last_updated",
                            update_parameters,
                            auto_commit=True,
                        )
                    updated_count += 1
                _LOGGER.info(
                    "Ensemble weight [%s/%s/%s]: lstm=%.2f ridge=%.2f (lstm_mae=%.4f, ridge_mae=%.4f, %d samples)",
                    group_name,
                    weather_type,
                    time_period,
                    lstm_weight,
                    ridge_weight,
                    smoothed_lstm_mae,
                    smoothed_ridge_mae,
                    sample_count,
                )
                continue
            _LOGGER.info(
                "Ensemble group weight learning completed for %s: %d buckets updated",
                date,
                updated_count,
            )
        except Exception as error:
            _LOGGER.error(
                f"Ensemble group weight learning error: {error}", exc_info=True
            )
        return None if True else None

    async def _learn_group_method_performance(self, now: datetime) -> None:
        """Learn per-group method accuracy (Physics vs AI vs LSTM vs Ridge). @zara V16.4.0

        For each panel group, computes MAE of each prediction method against actual
        production, bucketed by (cloud_bucket, hour_bucket, season). Determines which
        method is most accurate per group per condition. This data feeds the graduated
        safeguard to make data-driven decisions about AI vs Physics blending.

        Silent no-op when no per-group actual data is available.
        """
        try:
            database = self.db
            date = now.date().isoformat()
            rows = await database.fetchall(
                "SELECT ppg.group_name, hp.target_hour,\n                          ppg.physics_kwh, ppg.ai_kwh,\n                          ppg.lstm_kwh, ppg.ridge_kwh,\n                          ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)",
                (date,),
            )
            if not rows:
                _LOGGER.debug(
                    "Group method performance: No per-group actual data for %s", date
                )
                return
            grouped_errors = {}
            for row in rows:
                group_name = row[0]
                hour = row[1]
                physics_kwh = row[2]
                ai_kwh = row[3]
                lstm_kwh = row[4]
                ridge_kwh = row[5]
                actual_kwh = row[6]
                cloud_cover = row[7] or 50
                weather_type = (
                    "clear"
                    if cloud_cover < 30
                    else "partly_cloudy" if cloud_cover < 70 else "overcast"
                )
                time_period = (
                    "morning" if hour < 10 else "midday" if hour < 15 else "afternoon"
                )
                group_key = (group_name, weather_type, time_period)
                if group_key not in grouped_errors:
                    grouped_errors[group_key] = {
                        "physics": [],
                        "ai": [],
                        "lstm": [],
                        "ridge": [],
                        "blend": [],
                    }
                grouped_errors[group_key]["physics"].append(
                    abs(physics_kwh - actual_kwh)
                )
                if ai_kwh is not None and ai_kwh > 0:
                    grouped_errors[group_key]["ai"].append(abs(ai_kwh - actual_kwh))
                if lstm_kwh is not None and lstm_kwh > 0:
                    grouped_errors[group_key]["lstm"].append(abs(lstm_kwh - actual_kwh))
                if ridge_kwh is not None and ridge_kwh > 0:
                    grouped_errors[group_key]["ridge"].append(
                        abs(ridge_kwh - actual_kwh)
                    )
            if not grouped_errors:
                return
            learning_rate = 0.3
            season = get_season(now.month)
            updated_count = 0
            for (
                group_name,
                weather_type,
                time_period,
            ), error_samples in grouped_errors.items():
                physics_mae = sum(error_samples["physics"]) / len(
                    error_samples["physics"]
                )
                ai_mae = (
                    sum(error_samples["ai"]) / len(error_samples["ai"])
                    if error_samples["ai"]
                    else None
                )
                lstm_mae = (
                    sum(error_samples["lstm"]) / len(error_samples["lstm"])
                    if error_samples["lstm"]
                    else None
                )
                ridge_mae = (
                    sum(error_samples["ridge"]) / len(error_samples["ridge"])
                    if error_samples["ridge"]
                    else None
                )
                current_errors = {"physics": physics_mae}
                if ai_mae is not None:
                    current_errors["ai"] = ai_mae
                if lstm_mae is not None:
                    current_errors["lstm"] = lstm_mae
                if ridge_mae is not None:
                    current_errors["ridge"] = ridge_mae
                best_current_method = min(current_errors, key=current_errors.get)
                if ai_mae and ai_mae > 0 and (physics_mae > 0):
                    relative_mae = physics_mae / ai_mae
                    initial_correction_factor = max(0.5, min(2.0, relative_mae))
                else:
                    initial_correction_factor = 1.0
                for seasonal_scope in (None, season):
                    previous_metrics = await database.fetchone(
                        "SELECT physics_mae, ai_mae, lstm_mae, ridge_mae,\n                                  sample_count\n                           FROM group_method_performance\n                           WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS ?",
                        (group_name, weather_type, time_period, seasonal_scope),
                    )
                    if (
                        previous_metrics
                        and previous_metrics[4]
                        and (previous_metrics[4] > 0)
                    ):
                        smoothed_physics_mae = (
                            learning_rate * physics_mae
                            + (1 - learning_rate) * previous_metrics[0]
                        )
                        smoothed_ai_mae = (
                            learning_rate * ai_mae
                            + (1 - learning_rate) * previous_metrics[1]
                            if ai_mae and previous_metrics[1]
                            else ai_mae
                        )
                        smoothed_lstm_mae = (
                            learning_rate * lstm_mae
                            + (1 - learning_rate) * previous_metrics[2]
                            if lstm_mae and previous_metrics[2]
                            else lstm_mae
                        )
                        smoothed_ridge_mae = (
                            learning_rate * ridge_mae
                            + (1 - learning_rate) * previous_metrics[3]
                            if ridge_mae and previous_metrics[3]
                            else ridge_mae
                        )
                        sample_count = previous_metrics[4] + len(
                            error_samples["physics"]
                        )
                    else:
                        smoothed_physics_mae = physics_mae
                        smoothed_ai_mae = ai_mae
                        smoothed_lstm_mae = lstm_mae
                        smoothed_ridge_mae = ridge_mae
                        sample_count = len(error_samples["physics"])
                    smoothed_errors = {"physics": smoothed_physics_mae}
                    if smoothed_ai_mae is not None:
                        smoothed_errors["ai"] = smoothed_ai_mae
                    if smoothed_lstm_mae is not None:
                        smoothed_errors["lstm"] = smoothed_lstm_mae
                    if smoothed_ridge_mae is not None:
                        smoothed_errors["ridge"] = smoothed_ridge_mae
                    best_method = min(smoothed_errors, key=smoothed_errors.get)
                    if (
                        smoothed_ai_mae
                        and smoothed_ai_mae > 0
                        and (smoothed_physics_mae > 0)
                    ):
                        smoothed_correction_factor = max(
                            0.5, min(2.0, smoothed_physics_mae / smoothed_ai_mae)
                        )
                    else:
                        smoothed_correction_factor = 1.0
                    update_parameters = (
                        group_name,
                        weather_type,
                        time_period,
                        seasonal_scope,
                        round(smoothed_physics_mae, 6),
                        round(smoothed_ai_mae, 6) if smoothed_ai_mae else None,
                        round(smoothed_lstm_mae, 6) if smoothed_lstm_mae else None,
                        round(smoothed_ridge_mae, 6) if smoothed_ridge_mae else None,
                        None,
                        best_method,
                        round(smoothed_correction_factor, 4),
                        sample_count,
                        now,
                    )
                    if seasonal_scope is None:
                        async with database.transaction():
                            await database.execute(
                                "DELETE FROM group_method_performance\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (group_name, weather_type, time_period),
                                auto_commit=False,
                            )
                            await database.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                update_parameters,
                                auto_commit=False,
                            )
                    else:
                        await database.execute(
                            "INSERT INTO group_method_performance\n                               (group_name, cloud_bucket, hour_bucket, season,\n                                physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                best_method, ai_advantage_factor, sample_count, last_updated)\n                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                   physics_mae = excluded.physics_mae,\n                                   ai_mae = excluded.ai_mae,\n                                   lstm_mae = excluded.lstm_mae,\n                                   ridge_mae = excluded.ridge_mae,\n                                   blend_mae = excluded.blend_mae,\n                                   best_method = excluded.best_method,\n                                   ai_advantage_factor = excluded.ai_advantage_factor,\n                                   sample_count = excluded.sample_count,\n                                   last_updated = excluded.last_updated",
                            update_parameters,
                            auto_commit=True,
                        )
                    updated_count += 1
                _LOGGER.info(
                    "Group method learning [%s/%s/%s]: physics=%.4f ai=%s lstm=%s ridge=%s best=%s advantage=%.2f (%d samples)",
                    group_name,
                    weather_type,
                    time_period,
                    physics_mae,
                    f"{ai_mae:.4f}" if ai_mae else "N/A",
                    f"{lstm_mae:.4f}" if lstm_mae else "N/A",
                    f"{ridge_mae:.4f}" if ridge_mae else "N/A",
                    best_current_method,
                    initial_correction_factor,
                    sample_count,
                )
                continue
            _LOGGER.info(
                "Group method performance learning completed for %s: %d buckets updated",
                date,
                updated_count,
            )
        except Exception as error:
            _LOGGER.error(
                f"Group method performance learning error: {error}", exc_info=True
            )
        return None if True else None

    async def _run_physics_calibration(self, now: datetime) -> None:
        """Run physics engine calibration learning. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "thermodynamics_calibrator"
            ):
                _LOGGER.debug("Physics calibration skipped: no calibrator available")
                return
            calibrator = self.coordinator.thermodynamics_calibrator
            if not calibrator:
                _LOGGER.debug("Physics calibration skipped: calibrator is None")
                return
            date = now.date().isoformat()
            if hasattr(calibrator, "learn_from_day"):
                result = await calibrator.learn_from_day(date)
                if result and result.get("success"):
                    _LOGGER.info(
                        f"Physics calibration SUCCESS for {date}: Updated {result.get('groups_updated', 0)} groups"
                    )
                else:
                    _LOGGER.debug(f"Physics calibration skipped for {date}")
        except Exception as error:
            _LOGGER.error(f"Physics calibration error: {error}", exc_info=True)
        return None if True else None

    async def _run_weather_expert_learning(self, now: datetime) -> None:
        """Learn weather expert weights from today's actual observations. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                _LOGGER.debug(
                    "Weather expert learning skipped: no weather_pipeline_manager"
                )
                if True:
                    return
            weather_pipeline = self.coordinator.weather_pipeline_manager
            if not weather_pipeline:
                _LOGGER.debug("Weather expert learning skipped: pipeline is None")
                if True:
                    return
            if not weather_pipeline.weather_expert_learner:
                _LOGGER.debug(
                    "Weather expert learning skipped: learner not initialized"
                )
                if True:
                    return
            date = now.strftime("%Y-%m-%d")
            result = await weather_pipeline.weather_expert_learner.learn_from_day(date)
            if result:
                _LOGGER.info(f"Weather expert learning SUCCESS for {date}")
            else:
                skip_reason = "Learning failed or insufficient data"
                _LOGGER.debug(
                    f"Weather expert learning skipped for {date}: {skip_reason}"
                )
            try:
                prediction_rows = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh FROM hourly_predictions\n                       WHERE target_date = ?",
                    (date,),
                )
                hourly_predictions = [
                    {
                        "prediction_id": row[0],
                        "target_hour": row[1],
                        "actual_kwh": row[2],
                    }
                    for row in prediction_rows
                ]
                daily_weather = {}
                if weather_pipeline.weather_actual_tracker:
                    daily_weather = await weather_pipeline.weather_actual_tracker.get_daily_actual_weather(
                        date
                    )
                if hourly_predictions:
                    snow_learning_result = await weather_pipeline.weather_expert_learner.learn_snow_prediction_accuracy(
                        date, hourly_predictions, daily_weather
                    )
                    if snow_learning_result.get("total_snow_predicted", 0) > 0:
                        _LOGGER.info(
                            f"Snow prediction learning for {date}: {snow_learning_result.get('snow_confirmed', 0)}/{snow_learning_result.get('total_snow_predicted', 0)} confirmed ({snow_learning_result.get('accuracy', 0):.0%} accuracy)"
                        )
            except Exception as snow_learning_error:
                _LOGGER.debug(
                    f"Snow prediction learning skipped: {snow_learning_error}"
                )
        except Exception as error:
            _LOGGER.error(f"Weather expert learning error: {error}", exc_info=True)
        if True:
            return None

    async def _run_shadow_pattern_learning(self, now: datetime) -> None:
        """Learn shadow patterns from today's shadow detection data. @zara V17.0.0

        Uses ShadowPatternLearner to:
        - Identify recurring shadow patterns per hour
        - Classify fixed obstructions vs variable shadows
        - Track seasonal variations
        - V17.0.0: Per-group shadow pattern learning
        """
        try:
            from ..data.data_shadow_pattern_learning import (
                get_shadow_pattern_learner as get_shadow_pattern_learner,
            )

            shadow_learner = get_shadow_pattern_learner(self.hass, self.db)
            if not shadow_learner._initialized:
                initialized = await shadow_learner.async_init()
                if not initialized:
                    _LOGGER.warning("Shadow pattern learner initialization failed")
                    return
            date = now.strftime("%Y-%m-%d")
            result = await shadow_learner.learn_from_day(date)
            if result.success:
                _LOGGER.info(
                    f"Shadow pattern learning SUCCESS [_system_] for {date}: {result.hours_learned} hours learned, {result.patterns_updated} patterns updated, {result.fixed_obstructions_found} fixed obstructions"
                )
                learning_summary = shadow_learner.get_learning_summary()
                obstruction_hours = learning_summary.get("fixed_obstruction_hours", [])
                if obstruction_hours:
                    _LOGGER.info(
                        f"Fixed obstruction hours detected [_system_]: {obstruction_hours}"
                    )
            else:
                _LOGGER.debug(
                    f"Shadow pattern learning skipped [_system_] for {date}: {result.message}"
                )
            panel_groups = getattr(self.coordinator, "panel_groups", [])
            if panel_groups:
                learned_group_count = 0
                for panel_group in panel_groups:
                    group_name = (
                        panel_group.name
                        if hasattr(panel_group, "name")
                        else panel_group.get("name", "")
                    )
                    if not group_name:
                        continue
                    try:
                        learned_count = await shadow_learner.learn_from_day(
                            date, group_name=group_name
                        )
                        if learned_count.success:
                            learned_group_count += 1
                            _LOGGER.info(
                                f"Shadow pattern learning SUCCESS [{group_name}] for {date}: {learned_count.hours_learned} hours, {learned_count.patterns_updated} patterns, {learned_count.fixed_obstructions_found} fixed obstructions"
                            )
                        else:
                            _LOGGER.debug(
                                f"Shadow pattern learning skipped [{group_name}] for {date}: {learned_count.message}"
                            )
                    except Exception as error:
                        _LOGGER.warning(
                            f"Shadow pattern learning failed [{group_name}]: {error}"
                        )
                        continue
                if learned_group_count > 0:
                    _LOGGER.info(
                        f"Per-group shadow pattern learning: {learned_group_count}/{len(panel_groups)} groups learned"
                    )
        except Exception as error:
            _LOGGER.error(f"Shadow pattern learning error: {error}", exc_info=True)
        return None if True else None

    async def _run_drift_detection(self, now: datetime) -> None:
        """Run drift detection and metric calculation. @zara V17.0.0

        Calculates rolling metrics (MAE, RMSE, Bias, Coverage),
        bucket-specific drift, CUSUM detection, and automatic responses.
        Updates coordinator cache for drift sensor.
        """
        try:
            drift_monitor = getattr(self.coordinator, "drift_monitor", None)
            if not drift_monitor:
                _LOGGER.debug(
                    "Drift detection skipped: no drift monitor on coordinator"
                )
                return
            date = now.strftime("%Y-%m-%d")
            result = await drift_monitor.calculate_rolling_metrics(date)
            _LOGGER.info(
                "Drift detection completed for %s: %d metrics, %d events, %d scopes",
                date,
                result.get("metrics_saved", 0),
                result.get("events_created", 0),
                result.get("scopes_checked", 0),
            )
            try:
                drift_status = await drift_monitor.get_status()
                self.coordinator._drift_status_cache = drift_status
            except Exception as error:
                _LOGGER.debug("Failed to cache drift status: %s", error)
        except Exception as error:
            _LOGGER.error(f"Drift detection error: {error}", exc_info=True)
        return None if True else None

    async def _create_daily_summary(self, date: str) -> None:
        """Create daily summary using DailySummariesHandler. @zara

        Creates comprehensive daily summary including:
        - Main daily_summaries table
        - Time windows analysis (morning, midday, afternoon)
        - Frost analysis
        - Shadow analysis
        """
        try:
            rows = await self.db.fetchall(
                "SELECT hp.prediction_id, hp.target_datetime, hp.target_hour,\n                          hp.target_date, hp.prediction_kwh, hp.actual_kwh,\n                          hp.accuracy_percent, hp.error_kwh, hp.error_percent,\n                          pwa.frost_detected, pwa.frost_score, pwa.frost_confidence\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwa ON hp.prediction_id = pwa.prediction_id\n                        AND pwa.weather_type = 'actual'\n                   WHERE hp.target_date = ?\n                   ORDER BY hp.target_hour",
                (date,),
            )
            if not rows:
                _LOGGER.warning("No hourly predictions found for %s", date)
                return
            hourly_predictions = []
            for row in rows:
                prediction_data = {
                    "prediction_id": row[0],
                    "target_datetime": row[1],
                    "target_hour": row[2],
                    "target_date": row[3],
                    "prediction_kwh": row[4],
                    "actual_kwh": row[5],
                    "accuracy_percent": row[6],
                    "error_kwh": row[7],
                    "error_percent": row[8],
                    "weather_actual": (
                        {
                            "frost_detected": row[9],
                            "frost_score": row[10],
                            "frost_confidence": row[11],
                        }
                        if row[9] is not None
                        else None
                    ),
                }
                hourly_predictions.append(prediction_data)
            result = await self.daily_summaries_handler.create_daily_summary(
                date, hourly_predictions
            )
            if result:
                _LOGGER.info(
                    "Daily summary created for %s using DailySummariesHandler", date
                )
            else:
                _LOGGER.warning("DailySummariesHandler returned False for %s", date)
        except Exception as error:
            _LOGGER.error("Error creating daily summary: %s", error, exc_info=True)
        return None if True else None

    @callback
    def _adaptive_forecast_check(self, now: datetime) -> None:
        """Handle adaptive forecast check. @zara"""
        self.hass.async_create_task(self._execute_adaptive_forecast_check())

    async def _execute_adaptive_forecast_check(self) -> None:
        """Execute adaptive forecast check. @zara"""
        try:
            await self.adaptive_forecast_engine.run_midday_check()
        except Exception as error:
            _LOGGER.error("Adaptive forecast check failed: %s", error, exc_info=True)
        return None if True else None

    @callback
    async def generate_monthly_report(self, now: datetime) -> None:
        """Generate monthly system report on the 1st of each month at 07:30. @zara"""
        current_time = now if now is not None else dt_util.now()
        if current_time.day != 1:
            return
        _LOGGER.info("Generating monthly system report...")
        try:
            data_directory = self.coordinator.data_manager.data_dir
            report_generator = SystemReportGenerator(data_directory, self.db)
            result = await report_generator.generate_report()
            if result:
                _LOGGER.info("Monthly system report generated successfully")
            else:
                _LOGGER.warning("Monthly system report generation failed")
        except Exception as error:
            _LOGGER.error("Error generating monthly report: %s", error, exc_info=True)
        return None if True else None

    async def cancel_all_tasks(self) -> None:
        """Cancel all scheduled tasks. @zara"""
        self._shutdown_requested = True
        for unsubscribe in self._listeners:
            try:
                unsubscribe()
            except Exception:
                continue
        self._listeners = []
        for unsubscribe in self._dynamic_listeners:
            try:
                unsubscribe()
            except Exception:
                continue
        self._dynamic_listeners = []
        recovery_tasks = tuple(self._astronomy_recovery_tasks.values())
        self._astronomy_recovery_tasks.clear()
        for recovery_task in recovery_tasks:
            recovery_task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        await self.astronomy_provider.async_shutdown()
        _LOGGER.info("All scheduled tasks cancelled")
