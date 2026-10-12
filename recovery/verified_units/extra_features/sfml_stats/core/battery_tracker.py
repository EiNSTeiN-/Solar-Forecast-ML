# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Battery charging statistics tracker using Left Riemann Sum. @zara"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from ..const import RIEMANN_MAX_GAP_HOURS
from ..utils.time_utils import as_ha_local, ha_now

if TYPE_CHECKING:
    from ..storage.db_connection_manager import DatabaseConnectionManager
_LOGGER = logging.getLogger(__name__)
_RIEMANN_GAP_WARNING_GRACE_HOURS = 0.25


def _cap_riemann_delta_hours(delta_hours: float) -> tuple[float, bool]:
    """Return capped integration hours and whether the gap deserves a warning."""
    if delta_hours <= RIEMANN_MAX_GAP_HOURS:
        return (delta_hours, False)
    return (
        RIEMANN_MAX_GAP_HOURS,
        delta_hours > RIEMANN_MAX_GAP_HOURS + _RIEMANN_GAP_WARNING_GRACE_HOURS,
    )


class BatteryTracker:
    """Tracks battery charging statistics using Left Riemann Sum. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        db: DatabaseConnectionManager | None = None,
    ) -> None:
        # fmt: off
        """Initialize the battery tracker. @zara

Args:
    hass: Home Assistant instance
    entry_id: Config entry ID
    db: Database connection manager instance
"""
        # fmt: on
        self.hass = hass
        self._entry_id = entry_id
        self._db = db
        self._power_sensor_id = None
        self._last_power_w = 0.0
        self._last_update = None
        self._energy_today_wh = 0.0
        self._energy_week_wh = 0.0
        self._energy_month_wh = 0.0
        self._current_day = None
        self._current_week = None
        self._current_month = None
        self._unsub_state_change = None
        self._save_pending = False
        self._save_interval_seconds = 60
        self._save_timer_handle = None
        self._unloaded = False

    @property
    def current_power(self) -> float:
        """Get current battery charging power in W. @zara"""
        return self._last_power_w

    @property
    def energy_today_kwh(self) -> float:
        """Get energy charged today in kWh. @zara"""
        return round(self._energy_today_wh / 1000, 3)

    @property
    def energy_week_kwh(self) -> float:
        """Get energy charged this week in kWh. @zara"""
        return round(self._energy_week_wh / 1000, 3)

    @property
    def energy_month_kwh(self) -> float:
        """Get energy charged this month in kWh. @zara"""
        return round(self._energy_month_wh / 1000, 3)

    async def async_setup(self, power_sensor_id: str) -> None:
        """Set up the battery tracker with a power sensor. @zara"""
        self._power_sensor_id = power_sensor_id
        await self._async_load_data()
        if power_sensor_id:
            initial_state = self.hass.states.get(power_sensor_id)
            if initial_state and initial_state.state not in ("unknown", "unavailable"):
                try:
                    self._last_power_w = max(0.0, float(initial_state.state))
                    self._last_update = ha_now(self.hass)
                    _LOGGER.debug(
                        "Battery tracker initial power: %.1f W from %s",
                        self._last_power_w,
                        power_sensor_id,
                    )
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        "Invalid initial power value: %s", initial_state.state
                    )
            self._unsub_state_change = async_track_state_change_event(
                self.hass, [power_sensor_id], self._async_state_changed
            )
            _LOGGER.debug("Battery tracker set up for sensor: %s", power_sensor_id)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    async def async_unload(self) -> None:
        """Unload the battery tracker. @zara"""
        self._unloaded = True
        if self._save_timer_handle is not None:
            self._save_timer_handle.cancel()
            self._save_timer_handle = None
        if self._unsub_state_change:
            self._unsub_state_change()
            self._unsub_state_change = None
        await self._async_save_data()

    @callback
    def _async_state_changed(self, event) -> None:
        """Handle state changes of the power sensor. @zara"""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            power_w = float(new_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid power value: %s", new_state.state)
            return
        else:
            power_w = max(0.0, power_w)
            now = ha_now(self.hass)
            self._check_date_resets(now)
            if self._last_update is not None:
                delta_hours = (now - self._last_update).total_seconds() / 3600
                uncapped_hours = delta_hours
                delta_hours, warn_gap = _cap_riemann_delta_hours(delta_hours)
                if warn_gap:
                    _LOGGER.warning(
                        "Battery tracker gap detected: %.2f hours exceeds limit %.2f hours, capping interval",
                        uncapped_hours,
                        RIEMANN_MAX_GAP_HOURS,
                    )
                energy_wh = self._last_power_w * delta_hours
                self._energy_today_wh += energy_wh
                self._energy_week_wh += energy_wh
                self._energy_month_wh += energy_wh
                _LOGGER.debug(
                    "Battery energy update: +%.3f Wh (%.1f W x %.4f h)",
                    energy_wh,
                    self._last_power_w,
                    delta_hours,
                )
            self._last_power_w = power_w
            self._last_update = now
            self._schedule_save()

    def _check_date_resets(self, now: datetime) -> None:
        """Check if daily/weekly/monthly reset is needed. @zara"""
        local_now = as_ha_local(now, self.hass)
        day = local_now.day
        week = local_now.isocalendar()[1]
        month = local_now.month
        if self._current_day is not None and day != self._current_day:
            _LOGGER.info(
                "Daily reset: %.3f kWh charged yesterday", self._energy_today_wh / 1000
            )
            self._energy_today_wh = 0.0
        if self._current_week is not None and week != self._current_week:
            _LOGGER.info(
                "Weekly reset: %.3f kWh charged last week", self._energy_week_wh / 1000
            )
            self._energy_week_wh = 0.0
        if self._current_month is not None and month != self._current_month:
            _LOGGER.info(
                "Monthly reset: %.3f kWh charged last month",
                self._energy_month_wh / 1000,
            )
            self._energy_month_wh = 0.0
        self._current_day = day
        self._current_week = week
        self._current_month = month

    @callback
    def _schedule_save(self) -> None:
        """Schedule a debounced save operation. @zara"""
        if self._save_pending or self._unloaded:
            return None
        self._save_pending = True

        async def _delayed_save():
            """Perform the delayed save. @zara"""
            if self._unloaded:
                return
            await self._async_save_data()
            self._save_pending = False

        self._save_timer_handle = self.hass.loop.call_later(
            self._save_interval_seconds,
            lambda: self.hass.async_create_task(_delayed_save()),
        )

    async def _async_load_data(self) -> None:
        """Load stored statistics from database. @zara"""
        if not self._db:
            _LOGGER.debug("No database connection, skipping battery stats load")
            return
        try:
            rows = await self._db.execute_read(
                "SELECT energy_today_wh, energy_week_wh, energy_month_wh,\n                          current_day, current_week, current_month\n                   FROM GPM_battery_stats WHERE id = 1"
            )
            stored_stats = rows[0] if rows else None
        except Exception as error:
            _LOGGER.warning("Failed to load battery statistics: %s", error)
            return
        if stored_stats is None:
            _LOGGER.debug("No stored battery statistics found")
            return
        now = ha_now(self.hass)
        stored_day = stored_stats["current_day"]
        stored_week = stored_stats["current_week"]
        stored_month = stored_stats["current_month"]
        day = now.day
        week = now.isocalendar()[1]
        month = now.month
        if stored_day == day:
            self._energy_today_wh = stored_stats["energy_today_wh"] or 0.0
        else:
            self._energy_today_wh = 0.0
        if stored_week == week:
            self._energy_week_wh = stored_stats["energy_week_wh"] or 0.0
        else:
            self._energy_week_wh = 0.0
        if stored_month == month:
            self._energy_month_wh = stored_stats["energy_month_wh"] or 0.0
        else:
            self._energy_month_wh = 0.0
        self._current_day = day
        self._current_week = week
        self._current_month = month
        _LOGGER.debug(
            "Loaded battery statistics: today=%.3f kWh, week=%.3f kWh, month=%.3f kWh",
            self.energy_today_kwh,
            self.energy_week_kwh,
            self.energy_month_kwh,
        )

    async def _async_save_data(self) -> None:
        """GPM owns persistence for grid-price battery statistics."""

    def get_statistics(self) -> dict[str, Any]:
        """Get all battery statistics. @zara"""
        return {
            "battery_power": self._last_power_w,
            "battery_charged_today": self.energy_today_kwh,
            "battery_charged_week": self.energy_week_kwh,
            "battery_charged_month": self.energy_month_kwh,
        }
