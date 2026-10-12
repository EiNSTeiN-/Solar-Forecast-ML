# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
DNI Tracker for maximum DNI values per hour over rolling 7-day window.
Used to calculate dni_ratio feature for ML predictions.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
TRACKED_HOURS = range(6, 21)
HISTORY_DAYS = 7


class DniTracker:
    """Track max DNI per hour over 7 days for dni_ratio feature @zara"""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize tracker with database manager @zara"""
        self.db_manager = db_manager
        self.max_dni = {hour: 0.0 for hour in TRACKED_HOURS}
        self.history = {hour: [] for hour in TRACKED_HOURS}
        self.last_updated = None
        self._loaded = False
        _LOGGER.info("DniTracker initialized")

    async def async_load(self):
        """Load tracker data from database @zara"""
        if self._loaded:
            return
        try:
            tracker_rows = await self.db_manager.fetchall(
                "SELECT hour, max_dni, last_updated FROM ai_dni_tracker"
            )
            for row in tracker_rows:
                hour = row[0]
                self.max_dni[hour] = row[1]
                self.last_updated = str(row[2])
            history_rows = await self.db_manager.fetchall(
                "SELECT hour, dni_value FROM ai_dni_history\n                   ORDER BY id DESC LIMIT ?",
                (len(TRACKED_HOURS) * HISTORY_DAYS,),
            )
            for row in history_rows:
                hour = row[0]
                if hour not in self.history:
                    self.history[hour] = []
                self.history[hour].append(row[1])
            changed = False
            for hour in TRACKED_HOURS:
                values = self.history.get(hour, [])
                if values:
                    maximum = max(values)
                    if maximum > self.max_dni.get(hour, 0.0):
                        self.max_dni[hour] = maximum
                        changed = True
            if self.last_updated:
                try:
                    last_updated_day = datetime.fromisoformat(self.last_updated).date()
                    if last_updated_day < datetime.now().date():
                        self.start_new_day()
                        changed = True
                except (ValueError, TypeError):
                    pass
            if changed:
                self.last_updated = datetime.now().isoformat()
                await self.async_save()
            maximum_valid_dni = 1200.0
            reset_hours = 0
            for hour in TRACKED_HOURS:
                if self.max_dni.get(hour, 0.0) > maximum_valid_dni:
                    self.max_dni[hour] = 0.0
                    self.history[hour] = []
                    reset_hours += 1
            if reset_hours > 0:
                _LOGGER.warning(
                    "DNI tracker self-heal: reset %d hours with unrealistic max_dni (>%.0f)",
                    reset_hours,
                    maximum_valid_dni,
                )
                self.last_updated = datetime.now().isoformat()
                await self.async_save()
                changed = False
            self._loaded = True
            _LOGGER.info("DNI tracker loaded from database")
        except Exception as error:
            _LOGGER.warning(f"Could not load DNI tracker: {error}")
        return None if True else None

    def get_max_dni(self, hour: int) -> float:
        """Get max DNI for hour over last 7 days @zara"""
        if hour not in self.max_dni:
            return 0.0
        return self.max_dni.get(hour, 0.0)

    def record_dni(self, hour: int, dni: float):
        """Record DNI value for hour during the day @zara"""
        if hour not in TRACKED_HOURS:
            return
        if dni < 0:
            return
        values = self.history.get(hour, [])
        if len(values) == 0:
            values.append(dni)
        elif dni > values[-1]:
            values[-1] = max(values[-1], dni)
        self.history[hour] = values

    async def end_of_day_update(self):
        """Finalize today's values and update max, called at 23:40 @zara"""
        for hour in TRACKED_HOURS:
            values = self.history.get(hour, [])
            if len(values) == 0 or values[-1] == 0.0:
                values.append(0.0)
            if len(values) > HISTORY_DAYS:
                values = values[-HISTORY_DAYS:]
            self.history[hour] = values
            self.max_dni[hour] = max(values) if values else 0.0
        self.last_updated = datetime.now().isoformat()
        await self.async_save()
        _LOGGER.info("DNI tracker end-of-day update complete")

    def start_new_day(self):
        """Prepare for new day by adding placeholder @zara"""
        for hour in TRACKED_HOURS:
            values = self.history.get(hour, [])
            values.append(0.0)
            if len(values) > HISTORY_DAYS:
                values = values[-HISTORY_DAYS:]
            self.history[hour] = values

    async def async_save(self):
        """Save tracker data to database @zara"""
        try:
            if not self.last_updated:
                self.last_updated = datetime.now().isoformat()
            snapshot = {
                "version": "1.0",
                "last_updated": self.last_updated,
                "max_dni": {str(hour): value for hour, value in self.max_dni.items()},
                "history": {str(hour): value for hour, value in self.history.items()},
            }
            await self.db_manager.save_dni_tracker(snapshot)
            _LOGGER.debug("DNI tracker saved to database")
        except Exception as error:
            _LOGGER.error(f"Failed to save DNI tracker: {error}")
        return None if True else None

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics for diagnostics @zara"""
        return {
            "last_updated": self.last_updated,
            "max_dni": self.max_dni.copy(),
            "history_lengths": {
                hour: len(value) for hour, value in self.history.items()
            },
        }
