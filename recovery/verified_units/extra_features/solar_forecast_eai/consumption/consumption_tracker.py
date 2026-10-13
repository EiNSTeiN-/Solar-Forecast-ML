# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Production Time Tracker for Solar Forecast Energy AI.

Live tracks production time based on power entity state changes
with persistent state storage in database.

@author zara
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..core.core_user_messages import user_msg
from ..setup_state import power_watts

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
    from ..coordinator import SolarForecastEAICoordinator
_LOGGER = logging.getLogger(__name__)


class ProductionTimeCalculator:
    """Live tracks production time based on power entity state changes. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        power_entity: Optional[str] = None,
        db_manager: Optional["DatabaseManager"] = None,
        coordinator: Optional["SolarForecastEAICoordinator"] = None,
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
        self._midnight_listener_remove = None
        self._periodic_save_listener_remove = None
        self._last_save_time = None
        self._needs_save = False
        self.AUTOSAVE_INTERVAL = timedelta(seconds=30)
        self._power_unit_warned = set()
        self._power_reading_rejected = False
        _LOGGER.info(
            "ProductionTimeCalculator initialized with LOCAL time and persistence"
        )

    async def _load_persistent_state(self) -> bool:
        """Load production time state from database. @zara"""
        if not self.db:
            return False
        try:
            persistent_row = await self.db.fetchone(
                "SELECT date, accumulated_hours, is_active, start_time, production_time_today\n                   FROM production_time_state WHERE id = 1"
            )
            if not persistent_row:
                return False
            saved_date = str(persistent_row[0])
            current_date = dt_util.now().date().isoformat()
            if saved_date != current_date:
                _LOGGER.info(
                    "Persistent state is from %s, not loading (today is %s)",
                    saved_date,
                    current_date,
                )
                return False
            self._accumulated_hours = persistent_row[1] or 0.0
            self._is_active = bool(persistent_row[2])
            if self._is_active and persistent_row[3]:
                try:
                    saved_start_time = str(persistent_row[3])
                    self._start_time = dt_util.parse_datetime(saved_start_time)
                    if self._start_time:
                        self._start_time = dt_util.ensure_local(self._start_time)
                except Exception as error:
                    _LOGGER.warning("Could not restore start_time: %s", error)
                    self._is_active = False
                    self._start_time = None
            _LOGGER.info(
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
            current_time = dt_util.now()
            if not force:
                if self._last_save_time:
                    save_age = current_time - self._last_save_time
                    if save_age < timedelta(seconds=10):
                        return False
            production_time = self.get_production_time()
            await self.db.execute(
                "INSERT INTO production_time_state\n                   (id, date, accumulated_hours, is_active, start_time,\n                    production_time_today, last_updated)\n                   VALUES (1, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       date = excluded.date,\n                       accumulated_hours = excluded.accumulated_hours,\n                       is_active = excluded.is_active,\n                       start_time = excluded.start_time,\n                       production_time_today = excluded.production_time_today,\n                       last_updated = excluded.last_updated",
                (
                    current_time.date().isoformat(),
                    round(self._accumulated_hours, 4),
                    self._is_active,
                    self._start_time.isoformat() if self._start_time else None,
                    production_time,
                    current_time,
                ),
            )
            self._last_save_time = current_time
        except Exception as error:
            _LOGGER.error("Failed to save persistent production time state: %s", error)
            return False
        return True

    async def _sync_to_daily_forecasts(self) -> None:
        """Sync production time state to daily_forecasts immediately after restore. @zara"""
        if not self.db:
            return
        try:
            current_time = dt_util.now()
            production_seconds = int(self._accumulated_hours * 3600)
            if self._is_active and self._start_time:
                active_seconds = (current_time - self._start_time).total_seconds()
                production_seconds += int(active_seconds)
            _LOGGER.info(
                "Synced production time: duration=%ds (%s)",
                production_seconds,
                self.get_production_time(),
            )
        except Exception as error:
            _LOGGER.warning("Failed to sync production time: %s", error)
        return None if True else None

    async def start_tracking(self) -> None:
        """Start the live tracking listeners and initialize state. @zara"""
        _LOGGER.info(
            "[PRODUCTION_TRACKER] start_tracking called with power_entity=%s",
            self.power_entity,
        )
        if not self.power_entity:
            _LOGGER.info(user_msg("PRODUCTION_TRACKING_DISABLED"))
            return
        maximum_attempts = 4
        initial_retry_seconds = 5
        for attempt in range(maximum_attempts):
            retry_seconds = initial_retry_seconds * 2**attempt
            available_state = self.hass.states.get(self.power_entity)
            if available_state is not None:
                _LOGGER.info(
                    "[PRODUCTION_TRACKER] Power entity found: %s = %s",
                    self.power_entity,
                    available_state.state,
                )
                break
            if attempt < maximum_attempts - 1:
                retry_logger = _LOGGER.info if attempt == 0 else _LOGGER.debug
                retry_logger(
                    "[PRODUCTION_TRACKER] Power entity '%s' not available yet. Retrying in %ds (attempt %d/%d)...",
                    self.power_entity,
                    retry_seconds,
                    attempt + 1,
                    maximum_attempts,
                )
                await asyncio.sleep(retry_seconds)
                continue
            _LOGGER.warning(user_msg("SENSOR_UNAVAILABLE", entity=self.power_entity))
            return
        try:
            state_restored = await self._load_persistent_state()
            if state_restored:
                _LOGGER.info(
                    "[OK] Restored production time state: accumulated=%.2fh, active=%s",
                    self._accumulated_hours,
                    self._is_active,
                )
                await self._sync_to_daily_forecasts()
            else:
                _LOGGER.info("No persistent state found - will initialize from sensor")
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
                power_state = self.hass.states.get(self.power_entity)
                current_time = dt_util.now()
                if power_state and power_state.state not in ("unavailable", "unknown"):
                    power_w = self._reading_power_w(power_state)
                    if power_w is not None:
                        _LOGGER.info(
                            "[PRODUCTION_TRACKER] Parsed power value: %.1fW", power_w
                        )
                        if power_w >= self.MIN_POWER_THRESHOLD_W:
                            self._is_active = True
                            sensor_last_changed = getattr(
                                power_state, "last_changed", None
                            )
                            if sensor_last_changed:
                                self._start_time = dt_util.ensure_local(
                                    sensor_last_changed
                                )
                            else:
                                self._start_time = current_time
                            self._zero_power_start = None
                            _LOGGER.info(
                                "Initial state: Production detected (%.1fW). Approximate start time (LOCAL): %s",
                                power_w,
                                self._start_time.strftime("%H:%M:%S"),
                            )
                            self._needs_save = True
                        else:
                            _LOGGER.info(
                                "Initial state: No production (%.1fW)", power_w
                            )
                else:
                    _LOGGER.info(
                        "Initial state: Power sensor unavailable or unknown. Starting with no production."
                    )
            else:
                power_state = self.hass.states.get(self.power_entity)
                if power_state and power_state.state not in ("unavailable", "unknown"):
                    power_w = self._reading_power_w(power_state)
                    if power_w is not None:
                        if self._is_active and power_w <= self.ZERO_POWER_THRESHOLD_W:
                            self._zero_power_start = dt_util.now()
                        elif not self._is_active:
                            if power_w >= self.MIN_POWER_THRESHOLD_W:
                                _LOGGER.info(
                                    "[PRODUCTION_TRACKER] Restored inactive state but sensor shows %.1fW - starting production tracking now",
                                    power_w,
                                )
                                self._start_production_tracking(dt_util.now())
            _LOGGER.info("Production time tracking started for %s", self.power_entity)
        except Exception as error:
            _LOGGER.error(
                "Error starting production time tracking: %s", error, exc_info=True
            )
        return None if True else None

    def _reading_power_w(self, state: Any) -> float | None:
        """Return watts, or None when the unit is missing or not W/kW/MW."""
        state_attributes = getattr(state, "attributes", None) or {}
        parsed_power_w = power_watts(
            state.state, state_attributes.get("unit_of_measurement")
        )
        self._power_reading_rejected = parsed_power_w is None
        if parsed_power_w is None:
            warning_entity_id = self.power_entity or ""
            if warning_entity_id not in self._power_unit_warned:
                self._power_unit_warned.add(warning_entity_id)
                _LOGGER.warning(
                    "Power entity %s has no usable W/kW/MW unit; reading ignored",
                    warning_entity_id,
                )
        return parsed_power_w

    @callback
    def _handle_power_change(self, event) -> None:
        """Callback triggered by state changes of the power entity. @zara"""
        try:
            new_state = event.data.get("new_state")
            current_time = dt_util.now()
            power_w = None
            if new_state and new_state.state not in ("unavailable", "unknown"):
                power_w = self._reading_power_w(new_state)
            if power_w is None:
                if self._power_reading_rejected:
                    if True:
                        return
                if self._is_active:
                    if self._zero_power_start is None:
                        self._zero_power_start = current_time
                    else:
                        zero_power_duration = current_time - self._zero_power_start
                        if zero_power_duration > self.ZERO_POWER_TIMEOUT:
                            _LOGGER.info(
                                "Timeout exceeded for sensor unavailable. Stopping production tracking."
                            )
                            self._stop_production_tracking(current_time)
                if True:
                    return
            if power_w is not None and power_w > 0 and self.db:
                try:
                    self.hass.async_create_task(
                        self._update_peak_today(power_w, current_time)
                    )
                except Exception:
                    pass
            if power_w >= self.MIN_POWER_THRESHOLD_W:
                if not self._is_active:
                    _LOGGER.info(
                        "[PRODUCTION_TRACKER] Starting production tracking at %s (Power: %.1fW)",
                        current_time.strftime("%H:%M:%S"),
                        power_w,
                    )
                    self._start_production_tracking(current_time)
                elif self._zero_power_start is not None:
                    self._zero_power_start = None
            elif power_w < self.ZERO_POWER_THRESHOLD_W and self._is_active:
                if self._zero_power_start is None:
                    self._zero_power_start = current_time
                else:
                    zero_power_duration = current_time - self._zero_power_start
                    if zero_power_duration > self.ZERO_POWER_TIMEOUT:
                        _LOGGER.info(
                            "Timeout exceeded for low power. Stopping production tracking at %s",
                            current_time.strftime("%H:%M:%S"),
                        )
                        self._stop_production_tracking(current_time)
                    else:
                        self._zero_power_streak_minutes = int(
                            zero_power_duration.total_seconds() / 60
                        )
            if power_w is not None and power_w > self.ZERO_POWER_THRESHOLD_W:
                self._last_power_above_10w = current_time
                self._zero_power_streak_minutes = 0
            self._needs_save = True
        except Exception as error:
            _LOGGER.error("Error handling power state change: %s", error, exc_info=True)
        return None if True else None

    async def _update_peak_today(self, power_w: float, now: datetime) -> None:
        """Update peak power for today if new peak. @zara"""
        if not self.db:
            return
        try:
            peak_date = now.date().isoformat()
            peak_hour = now.hour
            peak_row = await self.db.fetchone(
                "SELECT kwh FROM astronomy_hourly_peaks WHERE hour = ?", (peak_hour,)
            )
            previous_peak_kw = peak_row[0] if peak_row else 0
            power_kw = power_w / 1000.0
            if power_kw > previous_peak_kw:
                await self.db.execute(
                    "INSERT INTO astronomy_hourly_peaks (hour, kwh, date)\n                       VALUES (?, ?, ?)\n                       ON CONFLICT(hour) DO UPDATE SET\n                           kwh = excluded.kwh,\n                           date = excluded.date",
                    (peak_hour, power_kw, peak_date),
                )
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
            phase_hours = active_duration.total_seconds() / 3600.0
            self._accumulated_hours += phase_hours
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
            current_time = dt_util.now()
            if self._is_active and self._start_time:
                local_midnight = current_time.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                self._start_time = local_midnight
            self._accumulated_hours = 0.0
            self._zero_power_start = None
            self._end_time = None
            self._last_power_above_10w = None
            self._zero_power_streak_minutes = 0
            self._needs_save = True
        except Exception as error:
            _LOGGER.error("Error during midnight reset: %s", error, exc_info=True)
        return None if True else None

    def get_production_hours(self) -> float:
        """Get total production hours for today. @zara"""
        total_hours = self._accumulated_hours
        if self._is_active and self._start_time:
            current_time = dt_util.now()
            active_hours = (current_time - self._start_time).total_seconds() / 3600.0
            total_hours += active_hours
        return total_hours

    def get_production_time(self) -> str:
        """Get formatted production time string (HH:MM:SS). @zara"""
        production_hours = self.get_production_hours()
        whole_hours = int(production_hours)
        minutes = int((production_hours - whole_hours) * 60)
        seconds = int(((production_hours - whole_hours) * 60 - minutes) * 60)
        return f"{whole_hours:02d}:{minutes:02d}:{seconds:02d}"

    def is_currently_producing(self) -> bool:
        """Check if production is currently active. @zara"""
        return self._is_active

    @callback
    def _handle_periodic_save(self, now: datetime) -> None:
        """Callback for periodic save of production time state. @zara"""
        try:
            current_time = dt_util.now()
            if self._is_active and self._zero_power_start is not None:
                zero_power_duration = current_time - self._zero_power_start
                if zero_power_duration > self.ZERO_POWER_TIMEOUT:
                    _LOGGER.info(
                        "Periodic timeout check: %.0fs elapsed (threshold: %.0fs). Stopping production tracking.",
                        zero_power_duration.total_seconds(),
                        self.ZERO_POWER_TIMEOUT.total_seconds(),
                    )
                    self._stop_production_tracking(current_time)
                    self._needs_save = True
                else:
                    self._zero_power_streak_minutes = int(
                        zero_power_duration.total_seconds() / 60
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
                current_time = dt_util.now()
                self._stop_production_tracking(current_time)
                if self.db:
                    await self._save_state_async()
            _LOGGER.info("Production time tracking stopped")
        except Exception as error:
            _LOGGER.error(
                "Error stopping production time tracking: %s", error, exc_info=True
            )
        return None if True else None

    @property
    def is_active(self) -> bool:
        """Return True if production is currently active. @zara"""
        return self._is_active

    @property
    def total_seconds(self) -> int:
        """Return total production time in seconds for today. @zara"""
        production_hours = self.get_production_hours()
        return int(production_hours * 3600)

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
