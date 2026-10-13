# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Production Time Tracker for Solar Forecast ML.

Live tracks production time based on power entity state changes
with persistent state storage in database.

@author zara
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..core.core_user_messages import user_msg
from ..const import DATA_KEY_PEAK_TODAY, PEAK_TODAY_AT, PEAK_TODAY_POWER_W

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
    from ..coordinator import SolarForecastMLCoordinator
_LOGGER = logging.getLogger(__name__)


class ProductionTimeCalculator:
    """Live tracks production time based on power entity state changes. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        power_entity: Optional[str] = None,
        db_manager: Optional["DatabaseManager"] = None,
        coordinator: Optional["SolarForecastMLCoordinator"] = None,
    ):
        """Initialize the live production time tracker with persistent state. @zara"""
        self.hass = hass
        self.power_entity = power_entity
        self.db = db_manager
        self.coordinator = coordinator
        self._is_active = False
        self._start_time = None
        self._accumulated_hours = 0.0
        self._zero_power_start = None
        self._today_total_hours = 0.0
        self._end_time = None
        self._last_power_above_10w = None
        self._zero_power_streak_minutes = 0
        self.MIN_POWER_THRESHOLD_W = 10.0
        self.ZERO_POWER_THRESHOLD_W = 10.0
        self.ZERO_POWER_TIMEOUT = timedelta(minutes=5)
        self._state_listener_remove = None
        self._sot_listener_remove = None
        self._midnight_listener_remove = None
        self._periodic_save_listener_remove = None
        self._last_save_time = None
        self._needs_save = False
        self.AUTOSAVE_INTERVAL = timedelta(seconds=30)
        _LOGGER.debug(
            "ProductionTimeCalculator initialized with LOCAL time and persistence"
        )

    async def _load_persistent_state(self) -> bool:
        """Load production time state from database. @zara"""
        if not self.db:
            return False
        try:
            state_row = await self.db.fetchone(
                "SELECT date, accumulated_hours, is_active, start_time, production_time_today\n                   FROM production_time_state WHERE id = 1"
            )
            if not state_row:
                return False
            stored_date = str(state_row[0])
            today = dt_util.now().date().isoformat()
            if stored_date != today:
                _LOGGER.info(
                    "Persistent state is from %s, not loading (today is %s)",
                    stored_date,
                    today,
                )
                return False
            self._accumulated_hours = state_row[1] or 0.0
            self._is_active = bool(state_row[2])
            if self._is_active and state_row[3]:
                try:
                    start_time_text = str(state_row[3])
                    self._start_time = dt_util.parse_datetime(start_time_text)
                    if self._start_time:
                        self._start_time = dt_util.ensure_local(self._start_time)
                except Exception as error:
                    _LOGGER.warning("Could not restore start_time: %s", error)
                    self._is_active = False
                    self._start_time = None
            _LOGGER.debug(
                "Loaded production time state: accumulated=%.2fh, active=%s, start_time=%s",
                self._accumulated_hours,
                self._is_active,
                self._start_time.isoformat() if self._start_time else "None",
            )
            return True
        except Exception as error:
            _LOGGER.error("Failed to load production time state: %s", error)
            return False

    async def _save_persistent_state(self, force: bool = False) -> bool:
        """Save current production time state to database. @zara"""
        if not self.db:
            return False
        try:
            now_local = dt_util.now()
            if not force:
                if self._last_save_time:
                    time_since_save = now_local - self._last_save_time
                    if time_since_save < timedelta(seconds=10):
                        return False
            formatted_time = self.get_production_time()
            await self.db.execute(
                "INSERT INTO production_time_state\n                   (id, date, accumulated_hours, is_active, start_time,\n                    production_time_today, last_updated)\n                   VALUES (1, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       date = excluded.date,\n                       accumulated_hours = excluded.accumulated_hours,\n                       is_active = excluded.is_active,\n                       start_time = excluded.start_time,\n                       production_time_today = excluded.production_time_today,\n                       last_updated = excluded.last_updated",
                (
                    now_local.date().isoformat(),
                    round(self._accumulated_hours, 4),
                    self._is_active,
                    self._start_time.isoformat() if self._start_time else None,
                    formatted_time,
                    now_local,
                ),
            )
            self._last_save_time = now_local
        except Exception as error:
            _LOGGER.error("Failed to save persistent production time state: %s", error)
            return False
        return True

    async def _sync_to_daily_forecasts(self) -> None:
        """Sync production time state to daily_forecasts immediately after restore. @zara"""
        if not self.db:
            return
        try:
            now_local = dt_util.now()
            duration_seconds = int(self._accumulated_hours * 3600)
            if self._is_active and self._start_time:
                active_seconds = (now_local - self._start_time).total_seconds()
                duration_seconds += int(active_seconds)
            _LOGGER.info(
                "Synced production time: duration=%ds (%s)",
                duration_seconds,
                self.get_production_time(),
            )
        except Exception as error:
            _LOGGER.warning("Failed to sync production time: %s", error)
        return None if True else None

    async def start_tracking(self) -> None:
        """Start the live tracking listeners and initialize state. @zara"""
        _LOGGER.debug(
            "[PRODUCTION_TRACKER] start_tracking called with power_entity=%s",
            self.power_entity,
        )
        live_manager = self._live_state_manager()
        uses_live_state = live_manager is not None
        if not uses_live_state and (not self.power_entity):
            _LOGGER.info(user_msg("PRODUCTION_TRACKING_DISABLED"))
            return
        if uses_live_state:
            _LOGGER.debug("[PRODUCTION_TRACKER] Using SFML live-state power source")
        else:
            max_attempts = 4
            initial_delay = 5
            for attempt in range(max_attempts):
                retry_delay = initial_delay * 2**attempt
                entity_state = self.hass.states.get(self.power_entity)
                if entity_state is not None:
                    _LOGGER.info(
                        "[PRODUCTION_TRACKER] Power entity found: %s = %s",
                        self.power_entity,
                        entity_state.state,
                    )
                    break
                if attempt < max_attempts - 1:
                    log_retry = _LOGGER.info if attempt == 0 else _LOGGER.debug
                    log_retry(
                        "[PRODUCTION_TRACKER] Power entity '%s' not available yet. Retrying in %ds (attempt %d/%d)...",
                        self.power_entity,
                        retry_delay,
                        attempt + 1,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                _LOGGER.warning(
                    user_msg("SENSOR_UNAVAILABLE", entity=self.power_entity)
                )
                return
        try:
            state_restored = await self._load_persistent_state()
            if state_restored:
                _LOGGER.debug(
                    "[OK] Restored production time state: accumulated=%.2fh, active=%s",
                    self._accumulated_hours,
                    self._is_active,
                )
                await self._sync_to_daily_forecasts()
            else:
                _LOGGER.info("No persistent state found - will initialize from sensor")
            if uses_live_state:
                self._sot_listener_remove = live_manager.add_listener(
                    self._handle_live_state_power_change
                )
                _LOGGER.debug(
                    "[PRODUCTION_TRACKER] SFML live-state listener registered"
                )
            else:
                self._state_listener_remove = async_track_state_change_event(
                    self.hass, [self.power_entity], self._handle_power_change
                )
                _LOGGER.info(
                    "[PRODUCTION_TRACKER] State listener registered for %s",
                    self.power_entity,
                )
            self._midnight_listener_remove = async_track_time_change(
                self.hass, self._handle_midnight_reset, hour=0, minute=0, second=1
            )
            self._periodic_save_listener_remove = async_track_time_change(
                self.hass, self._handle_periodic_save, second=[0, 30]
            )
            if not state_restored:
                _LOGGER.info(
                    "[PRODUCTION_TRACKER] No state loaded - initializing from current sensor value"
                )
                now_local = dt_util.now()
                current_power = self._current_power_w()
                if current_power is not None:
                    _LOGGER.info(
                        "[PRODUCTION_TRACKER] Parsed power value: %.1fW", current_power
                    )
                    if current_power >= self.MIN_POWER_THRESHOLD_W:
                        self._is_active = True
                        last_changed = None
                        if not uses_live_state:
                            if self.power_entity:
                                current_state = self.hass.states.get(self.power_entity)
                                last_changed = getattr(
                                    current_state, "last_changed", None
                                )
                        self._start_time = (
                            dt_util.ensure_local(last_changed)
                            if last_changed
                            else now_local
                        )
                        self._zero_power_start = None
                        _LOGGER.info(
                            "Initial state: Production detected (%.1fW). Approximate start time (LOCAL): %s",
                            current_power,
                            self._start_time.strftime("%H:%M:%S"),
                        )
                        self._needs_save = True
                    else:
                        _LOGGER.info(
                            "Initial state: No production (%.1fW)", current_power
                        )
                else:
                    _LOGGER.info(
                        "Initial state: Power source unavailable or unknown. Starting with no production."
                    )
            else:
                current_power = self._current_power_w()
                if current_power is not None:
                    self._handle_power_value(current_power, dt_util.now())
            power_source = "sfml_live_state" if uses_live_state else self.power_entity
            _LOGGER.debug("Production time tracking started for %s", power_source)
        except Exception as error:
            _LOGGER.error(
                "Error starting production time tracking: %s", error, exc_info=True
            )
        return None if True else None

    @callback
    def _handle_power_change(self, event) -> None:
        """Callback triggered by state changes of the power entity. @zara"""
        try:
            new_state = event.data.get("new_state")
            now_local = dt_util.now()
            power_value = None
            if new_state and new_state.state not in ("unavailable", "unknown"):
                try:
                    power_value = float(new_state.state)
                except (ValueError, TypeError):
                    power_value = None
            self._handle_power_value(power_value, now_local)
        except Exception as error:
            _LOGGER.error("Error handling power state change: %s", error, exc_info=True)
        return None if True else None

    @callback
    def _handle_live_state_power_change(self) -> None:
        """Callback triggered by SFML live-state power changes."""
        try:
            self._handle_power_value(self._current_power_w(), dt_util.now())
        except Exception as error:
            _LOGGER.error(
                "Error handling SFML live-state power change: %s", error, exc_info=True
            )
        return None if True else None

    def _live_state_manager(self):
        return getattr(self.coordinator, "actual_live_state_manager", None)

    def _current_power_w(self) -> Optional[float]:
        live_manager = self._live_state_manager()
        source_state = getattr(live_manager, "state", None)
        if live_manager is not None:
            if not isinstance(source_state, dict):
                return
            power_reading = source_state.get("total_power_w")
            if power_reading is not None:
                try:
                    return float(power_reading)
                except (ValueError, TypeError):
                    return
            return
        if self.power_entity:
            source_state = self.hass.states.get(self.power_entity)
            if source_state and source_state.state not in ("unavailable", "unknown"):
                try:
                    return float(source_state.state)
                except (ValueError, TypeError):
                    return
        return None if True else None

    def _handle_power_value(
        self, power_w: Optional[float], now_local: datetime
    ) -> None:
        """Apply a power value from either legacy sensor or SFML live state."""
        if power_w is None:
            if self._is_active:
                if self._zero_power_start is None:
                    self._zero_power_start = now_local
                else:
                    zero_power_elapsed = now_local - self._zero_power_start
                    if zero_power_elapsed > self.ZERO_POWER_TIMEOUT:
                        _LOGGER.info(
                            "Timeout exceeded for sensor unavailable. Stopping production tracking."
                        )
                        self._stop_production_tracking(now_local)
            return None if True else None
        if power_w is not None and power_w > 0 and self.db:
            try:
                self.hass.async_create_task(self._update_peak_today(power_w, now_local))
            except Exception:
                pass
        if power_w >= self.MIN_POWER_THRESHOLD_W:
            if not self._is_active:
                _LOGGER.info(
                    "[PRODUCTION_TRACKER] Starting production tracking at %s (Power: %.1fW)",
                    now_local.strftime("%H:%M:%S"),
                    power_w,
                )
                self._start_production_tracking(now_local)
            elif self._zero_power_start is not None:
                self._zero_power_start = None
        elif power_w < self.ZERO_POWER_THRESHOLD_W and self._is_active:
            if self._zero_power_start is None:
                self._zero_power_start = now_local
            else:
                zero_power_elapsed = now_local - self._zero_power_start
                if zero_power_elapsed > self.ZERO_POWER_TIMEOUT:
                    _LOGGER.info(
                        "Timeout exceeded for low power. Stopping production tracking at %s",
                        now_local.strftime("%H:%M:%S"),
                    )
                    self._stop_production_tracking(now_local)
                else:
                    self._zero_power_streak_minutes = int(
                        zero_power_elapsed.total_seconds() / 60
                    )
        if power_w is not None and power_w > self.ZERO_POWER_THRESHOLD_W:
            self._last_power_above_10w = now_local
            self._zero_power_streak_minutes = 0
        self._needs_save = True

    def _sync_peak_to_coordinator(
        self,
        power_w: float,
        peak_time: Optional[str],
        date: str,
        *,
        notify: bool = True,
    ) -> None:
        """Mirror today's peak into coordinator state. @zara"""
        if not self.coordinator:
            return
        self.coordinator._peak_power_today = power_w
        self.coordinator._peak_time_today = peak_time
        self.coordinator._peak_date_today = date
        if isinstance(getattr(self.coordinator, "data", None), dict):
            coordinator_data = dict(self.coordinator.data)
            coordinator_data[DATA_KEY_PEAK_TODAY] = {
                PEAK_TODAY_POWER_W: power_w,
                PEAK_TODAY_AT: peak_time,
            }
            self.coordinator.data = coordinator_data
        if notify:
            self.coordinator.async_update_listeners()
        return None if True else None

    async def reset_daily_peak(self, reset_time: Optional[datetime] = None) -> None:
        """Reset today's peak in database and coordinator state. @zara"""
        reset_at = reset_time or dt_util.now()
        today = reset_at.date().isoformat()
        if not self.db:
            self._sync_peak_to_coordinator(0.0, None, today)
            return
        try:
            tracking_row = await self.db.fetchone(
                "SELECT date FROM daily_forecast_tracking WHERE id = 1"
            )
            tracking_date_changed = bool(
                tracking_row and tracking_row[0] and (str(tracking_row[0]) != today)
            )
            async with self.db.transaction():
                await self.db.execute(
                    "INSERT INTO production_time_state\n                       (id, date, peak_power_w, peak_power_time, last_updated)\n                       VALUES (1, ?, 0, NULL, ?)\n                       ON CONFLICT(id) DO UPDATE SET\n                           date = excluded.date,\n                           peak_power_w = 0,\n                           peak_power_time = NULL,\n                           last_updated = excluded.last_updated",
                    (today, reset_at.isoformat()),
                )
                await self.db.execute(
                    "INSERT INTO daily_forecast_tracking\n                       (id, date, peak_today_power_w, peak_today_at, last_updated)\n                       VALUES (1, ?, 0, NULL, ?)\n                       ON CONFLICT(id) DO UPDATE SET\n                           date = excluded.date,\n                           peak_today_power_w = 0,\n                           peak_today_at = NULL,\n                           last_updated = excluded.last_updated",
                    (today, reset_at.isoformat()),
                )
                if tracking_date_changed and hasattr(
                    self.db, "clear_conservative_planning_tracking"
                ):
                    await self.db.clear_conservative_planning_tracking(
                        today, reason="daily_peak_reset_date_rollover"
                    )
        except Exception as error:
            _LOGGER.warning("Could not reset daily peak: %s", error)
            return
        self._sync_peak_to_coordinator(0.0, None, today)

    async def _update_peak_today(self, power_w: float, now: datetime) -> None:
        """Update peak power for today if new peak. @zara"""
        if not self.db:
            return
        try:
            today = now.date().isoformat()
            peak_time = now.strftime("%H:%M")
            hour = now.hour
            hourly_peak_row = await self.db.fetchone(
                "SELECT kwh FROM astronomy_hourly_peaks WHERE hour = ?", (hour,)
            )
            previous_hourly_peak = hourly_peak_row[0] if hourly_peak_row else 0
            power_kw = power_w / 1000.0
            if power_kw > previous_hourly_peak:
                await self.db.execute(
                    "INSERT INTO astronomy_hourly_peaks (hour, kwh, date)\n                       VALUES (?, ?, ?)\n                       ON CONFLICT(hour) DO UPDATE SET\n                           kwh = excluded.kwh,\n                           date = excluded.date",
                    (hour, power_kw, today),
                )
            peak_row = await self.db.fetchone(
                "SELECT date, peak_power_w, peak_record_w\n                   FROM production_time_state\n                   WHERE id = 1"
            )
            tracking_row = await self.db.fetchone(
                "SELECT date FROM daily_forecast_tracking WHERE id = 1"
            )
            stored_peak_date = str(peak_row[0]) if peak_row and peak_row[0] else None
            previous_daily_peak = float(peak_row[1] or 0.0) if peak_row else 0.0
            record_peak = float(peak_row[2] or 0.0) if peak_row else 0.0
            new_daily_peak = stored_peak_date != today or power_w > previous_daily_peak
            tracking_date_changed = bool(
                tracking_row and tracking_row[0] and (str(tracking_row[0]) != today)
            )
            if new_daily_peak:
                async with self.db.transaction():
                    await self.db.execute(
                        "INSERT INTO production_time_state\n                           (id, date, peak_power_w, peak_power_time, last_updated)\n                           VALUES (1, ?, ?, ?, ?)\n                           ON CONFLICT(id) DO UPDATE SET\n                               date = excluded.date,\n                               peak_power_w = excluded.peak_power_w,\n                               peak_power_time = excluded.peak_power_time,\n                               last_updated = excluded.last_updated",
                        (today, power_w, peak_time, now.isoformat()),
                    )
                    await self.db.execute(
                        "INSERT INTO daily_forecast_tracking\n                           (id, date, peak_today_power_w, peak_today_at, last_updated)\n                           VALUES (1, ?, ?, ?, ?)\n                           ON CONFLICT(id) DO UPDATE SET\n                               date = excluded.date,\n                               peak_today_power_w = excluded.peak_today_power_w,\n                               peak_today_at = excluded.peak_today_at,\n                               last_updated = excluded.last_updated",
                        (today, power_w, peak_time, now.isoformat()),
                    )
                    if tracking_date_changed and hasattr(
                        self.db, "clear_conservative_planning_tracking"
                    ):
                        await self.db.clear_conservative_planning_tracking(
                            today, reason="peak_update_date_rollover"
                        )
                self._sync_peak_to_coordinator(power_w, peak_time, today)
            if power_w > record_peak:
                await self.db.execute(
                    "INSERT INTO production_time_state\n                       (id, date, peak_record_w, peak_record_date, peak_record_time, last_updated)\n                       VALUES (1, ?, ?, ?, ?, ?)\n                       ON CONFLICT(id) DO UPDATE SET\n                           peak_record_w = excluded.peak_record_w,\n                           peak_record_date = excluded.peak_record_date,\n                           peak_record_time = excluded.peak_record_time,\n                           last_updated = excluded.last_updated",
                    (today, power_w, today, peak_time, now.isoformat()),
                )
                await self.db.commit()
                _LOGGER.info("New all-time peak: %.1f W at %s", power_w, peak_time)
        except Exception as error:
            _LOGGER.debug("Could not update peak: %s", error)
        return None if True else None

    def _start_production_tracking(self, start_time_local: datetime) -> None:
        """Start a new production phase. @zara"""
        self._is_active = True
        self._start_time = start_time_local
        self._last_power_above_10w = start_time_local
        self._zero_power_streak_minutes = 0
        self._zero_power_start = None
        self._needs_save = True

    def _stop_production_tracking(self, stop_time_local: datetime) -> None:
        """Stop the current production phase. @zara"""
        if not self._is_active:
            return
        if self._start_time:
            active_duration = stop_time_local - self._start_time
            active_hours = active_duration.total_seconds() / 3600.0
            self._accumulated_hours += active_hours
        else:
            _LOGGER.warning(
                "Stop called but no start time recorded. Cannot calculate duration."
            )
        self._is_active = False
        self._start_time = None
        self._end_time = stop_time_local
        self._zero_power_start = None
        self._needs_save = True

    @callback
    def _handle_midnight_reset(self, now: datetime) -> None:
        """Callback for midnight reset of daily counters. @zara"""
        try:
            self._today_total_hours = self.get_production_hours()
            _LOGGER.info(
                "Midnight reset - Final production time for previous day: %.2f hours",
                self._today_total_hours,
            )
            now_local = dt_util.now()
            if self._is_active and self._start_time:
                midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                self._start_time = midnight
            self._accumulated_hours = 0.0
            self._zero_power_start = None
            self._end_time = None
            self._last_power_above_10w = None
            self._zero_power_streak_minutes = 0
            self._needs_save = True
            self.hass.async_create_task(self.reset_daily_peak(now_local))
        except Exception as error:
            _LOGGER.error("Error during midnight reset: %s", error, exc_info=True)
        return None if True else None

    def get_production_hours(self) -> float:
        """Get total production hours for today. @zara"""
        total_hours = self._accumulated_hours
        if self._is_active and self._start_time:
            now_local = dt_util.now()
            current_hours = (now_local - self._start_time).total_seconds() / 3600.0
            total_hours += current_hours
        return total_hours

    def get_production_time(self) -> str:
        """Get formatted production time string (HH:MM:SS). @zara"""
        total_hours = self.get_production_hours()
        hours = int(total_hours)
        minutes = int((total_hours - hours) * 60)
        seconds = int(((total_hours - hours) * 60 - minutes) * 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def is_currently_producing(self) -> bool:
        """Check if production is currently active. @zara"""
        return self._is_active

    @callback
    def _handle_periodic_save(self, now: datetime) -> None:
        """Callback for periodic save of production time state. @zara"""
        try:
            now_local = dt_util.now()
            if self._is_active and self._zero_power_start is not None:
                zero_power_elapsed = now_local - self._zero_power_start
                if zero_power_elapsed > self.ZERO_POWER_TIMEOUT:
                    _LOGGER.info(
                        "Periodic timeout check: %.0fs elapsed (threshold: %.0fs). Stopping production tracking.",
                        zero_power_elapsed.total_seconds(),
                        self.ZERO_POWER_TIMEOUT.total_seconds(),
                    )
                    self._stop_production_tracking(now_local)
                    self._needs_save = True
                else:
                    self._zero_power_streak_minutes = int(
                        zero_power_elapsed.total_seconds() / 60
                    )
            if self._needs_save and self.db:
                self.hass.async_create_task(self._save_state_async())
                self._needs_save = False
        except Exception as error:
            _LOGGER.error("Error during periodic save/timeout check: %s", error)
        return None if True else None

    async def _save_state_async(self) -> None:
        """Async wrapper for saving both persistent state and production tracking. @zara"""
        try:
            await self._save_persistent_state(force=True)
        except Exception as error:
            _LOGGER.error("Failed to save production state: %s", error)
        return None if True else None

    async def stop_tracking(self) -> None:
        """Stop all tracking listeners and save final state. @zara"""
        try:
            if self.db and self._needs_save:
                await self._save_state_async()
            if self._state_listener_remove:
                try:
                    self._state_listener_remove()
                except ValueError:
                    pass
                self._state_listener_remove = None
            if self._sot_listener_remove:
                try:
                    self._sot_listener_remove()
                except ValueError:
                    pass
                self._sot_listener_remove = None
            if self._midnight_listener_remove:
                try:
                    self._midnight_listener_remove()
                except ValueError:
                    pass
                self._midnight_listener_remove = None
            if self._periodic_save_listener_remove:
                try:
                    self._periodic_save_listener_remove()
                except ValueError:
                    pass
                self._periodic_save_listener_remove = None
            if self._is_active:
                now_local = dt_util.now()
                self._stop_production_tracking(now_local)
                if self.db:
                    await self._save_state_async()
            _LOGGER.info("Production time tracking stopped")
        except Exception as error:
            _LOGGER.error(
                "Error stopping production time tracking: %s", error, exc_info=True
            )
        return None if False else None

    @property
    def is_active(self) -> bool:
        """Return True if production is currently active. @zara"""
        return self._is_active

    @property
    def total_seconds(self) -> int:
        """Return total production time in seconds for today. @zara"""
        total_hours = self.get_production_hours()
        return int(total_hours * 3600)

    @property
    def start_time(self) -> Optional[datetime]:
        """Return the start time of current/last production phase (LOCAL). @zara"""
        return self._start_time

    @property
    def end_time(self) -> Optional[datetime]:
        """Return the end time of last production phase (LOCAL). @zara"""
        return self._end_time

    def get_today_total_hours(self) -> float:
        """Get the final production hours from the previous day. @zara"""
        return (
            self._today_total_hours
            if self._today_total_hours > 0
            else self.get_production_hours()
        )
