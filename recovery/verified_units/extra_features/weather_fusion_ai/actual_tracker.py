# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Actual Weather Tracker - Records local sensor data for learning.

@zara
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo
from homeassistant.core import HomeAssistant
from .file_io import async_write_json

_LOGGER = logging.getLogger(__name__)


class ActualWeatherTracker:
    """Tracks actual weather from local sensors for learning."""

    FILE_RETENTION_DAYS = 730
    HISTORY_FILE = "actual_weather_history.json"

    def __init__(
        self,
        hass: HomeAssistant,
        data_dir: Path,
        sensors: Dict[str, Optional[str]],
        time_zone: str | None = None,
    ):
        'Initialize the actual weather tracker.\n\nArgs:\n    hass: Home Assistant instance\n    data_dir: Directory for data files\n    sensors: Dict mapping sensor types to entity IDs\n             e.g. {"temperature": "sensor.outdoor_temp", ...}\n'
        self.hass = hass
        self.data_dir = data_dir
        self.sensors = sensors
        try:
            self._time_zone = (
                ZoneInfo(time_zone) if time_zone else datetime.now().astimezone().tzinfo
            )
        except (ValueError, TypeError):
            self._time_zone = datetime.now().astimezone().tzinfo
        self._history_file = data_dir / self.HISTORY_FILE
        self._history = {}
        self._history_loaded = False
        self._history_lock_impl = None
        _LOGGER.info(
            "ActualWeatherTracker initialized with %d configured sensors",
            sum((sensor_value is not None for sensor_value in sensors.values())),
        )

    def _history_lock(self) -> asyncio.Lock:
        if self._history_lock_impl is None:
            self._history_lock_impl = asyncio.Lock()
        return self._history_lock_impl

    async def _load_history(self) -> bool:
        """Load history from file."""
        if self._history_loaded:
            return True
        if not self._history_file.exists():
            self._history_loaded = True
            return False
        try:
            history_payload = await asyncio.to_thread(
                lambda: json.loads(self._history_file.read_text())
            )
            self._history = history_payload.get("history", {})
            self._history_loaded = True
            _LOGGER.debug(f"Loaded {len(self._history)} days of actual weather history")
        except Exception as error:
            _LOGGER.warning(f"Error loading actual weather history: {error}")
            self._history_loaded = True
            return False
        return True

    async def _save_history(self) -> bool:
        """Save history to file with retention."""
        try:
            retention_cutoff = (
                datetime.now() - timedelta(days=self.FILE_RETENTION_DAYS)
            ).strftime("%Y-%m-%d")
            expired_dates = [
                date_key
                for date_key in self._history.keys()
                if date_key < retention_cutoff
            ]
            for date_key in expired_dates:
                del self._history[date_key]
            history_payload = {
                "version": "1.0",
                "metadata": {
                    "last_updated": datetime.now().isoformat(),
                    "retention_days": self.FILE_RETENTION_DAYS,
                },
                "history": self._history,
            }
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            await async_write_json(self._history_file, history_payload)
        except Exception as error:
            _LOGGER.warning(f"Error saving actual weather history: {error}")
            return False
        return True

    def _get_sensor_value(self, sensor_type: str) -> Optional[float]:
        """Get current value from a sensor, normalized to standard units."""
        entity_id = self.sensors.get(sensor_type)
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable", None):
            return
        try:
            sensor_value = float(state.state)
        except (ValueError, TypeError):
            return
        if sensor_type == "wind_speed":
            unit = state.attributes.get("unit_of_measurement", "").lower()
            if unit == "m/s":
                sensor_value *= 3.6
            elif unit == "mph":
                sensor_value *= 1.60934
            elif unit in ("kn", "kt", "knots"):
                sensor_value *= 1.852
            elif unit == "ft/s":
                sensor_value *= 1.09728
        if sensor_type == "wind_direction":
            sensor_value %= 360
        return sensor_value

    def _get_precipitation_details(self) -> tuple[Optional[float], str, Optional[str]]:
        """Read precipitation with explicit semantics, never guessing legacy data."""
        entity_id = self.sensors.get("rain")
        state = self.hass.states.get(entity_id) if entity_id else None
        sensor_value = self._get_sensor_value("rain")
        if sensor_value is None or state is None:
            return (sensor_value, "unknown", None)
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        state_class = state.attributes.get("state_class")
        if unit in {"in", "inch", "inches"}:
            sensor_value *= 25.4
            unit = "mm"
        elif unit in {"in/h", "in/hr"}:
            sensor_value *= 25.4
            unit = "mm/h"
        if state_class == "total_increasing" and unit == "mm":
            return (sensor_value, "counter", unit)
        if unit in {"mm/h", "mm/hr"}:
            return (sensor_value, "rate", "mm/h")
        return (sensor_value, "unknown", unit or None)

    def _current_precipitation(
        self, value: Optional[float], semantics: str, unit: Optional[str]
    ) -> Optional[float]:
        """Return a current rain value without exposing a cumulative counter."""
        if value is None:
            return
        if semantics == "rate":
            return max(0.0, value)
        if semantics == "unknown":
            return 0.0 if value == 0 else None
        if semantics != "counter":
            return
        latest_counter = None
        for day_observations in self._history.values():
            for observation in day_observations.values():
                if (
                    observation.get("precipitation_semantics") != "counter"
                    or observation.get("precipitation_unit") != unit
                ):
                    continue
                raw_counter = observation.get(
                    "precipitation_counter", observation.get("precipitation")
                )
                try:
                    observed_at = datetime.fromisoformat(
                        str(observation.get("timestamp"))
                    )
                    counter_value = float(raw_counter)
                except (TypeError, ValueError):
                    continue
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=self._time_zone)
                try:
                    if latest_counter is None or observed_at > latest_counter[0]:
                        latest_counter = (observed_at, counter_value)
                    else:
                        continue
                except TypeError:
                    continue
        if latest_counter is None:
            return
        counter_value = latest_counter[1]
        return (
            max(0.0, value - counter_value)
            if value >= counter_value
            else max(0.0, value)
        )

    async def track_current_weather(self) -> Optional[Dict[str, Any]]:
        """Track current weather from sensors and store in history."""
        await self._load_history()
        now = datetime.now(self._time_zone)
        date_key = now.strftime("%Y-%m-%d")
        hour_key = str(now.hour)
        precipitation_value, precipitation_semantics, precipitation_unit = (
            self._get_precipitation_details()
        )
        current_precipitation = self._current_precipitation(
            precipitation_value, precipitation_semantics, precipitation_unit
        )
        weather_observation = {
            "timestamp": now.isoformat(),
            "temperature": self._get_sensor_value("temperature"),
            "humidity": self._get_sensor_value("humidity"),
            "pressure": self._get_sensor_value("pressure"),
            "wind_speed": self._get_sensor_value("wind_speed"),
            "wind_direction": self._get_sensor_value("wind_direction"),
            "rain": precipitation_value,
        }
        if precipitation_semantics == "counter":
            weather_observation["precipitation_counter"] = precipitation_value
        weather_observation["precipitation"] = current_precipitation
        weather_observation["precipitation_semantics"] = precipitation_semantics
        weather_observation["precipitation_unit"] = precipitation_unit
        has_measurements = any(
            (
                field_value is not None
                for field, field_value in weather_observation.items()
                if field
                not in {
                    "timestamp",
                    "precipitation_unit",
                    "precipitation_counter",
                    "precipitation_semantics",
                }
            )
        )
        if not has_measurements:
            _LOGGER.debug("No sensor data available for tracking")
            return
        async with self._history_lock():
            if date_key not in self._history:
                self._history[date_key] = {}
            existing_observation = self._history[date_key].get(hour_key)
            if existing_observation:
                try:
                    existing_offset = datetime.fromisoformat(
                        str(existing_observation.get("timestamp"))
                    ).utcoffset()
                except (TypeError, ValueError):
                    existing_offset = now.utcoffset()
                if existing_offset != now.utcoffset():
                    hour_key = f"{now.hour}_fold{now.fold or 1}"
            self._history[date_key][hour_key] = weather_observation
            await self._save_history()
        _LOGGER.debug(
            "Tracked actual weather for %s %s:00 - temp=%.1f, hum=%s",
            date_key,
            hour_key,
            weather_observation.get("temperature") or 0,
            weather_observation.get("humidity"),
        )
        current_weather = dict(weather_observation)
        current_weather["current_precipitation"] = current_precipitation
        return current_weather

    async def get_actual_weather(
        self, date: str, hour: int
    ) -> Optional[Dict[str, Any]]:
        """Get actual weather for a specific hour from history."""
        await self._load_history()
        if date not in self._history:
            return
        return self._history[date].get(str(hour))

    async def get_actual_weather_range(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Get actual weather history for a date range."""
        await self._load_history()
        history_range = {}
        async with self._history_lock():
            for date_key in sorted(self._history.keys()):
                if start_date <= date_key <= end_date:
                    history_range[date_key] = {
                        hour_key: observation.copy()
                        for hour_key, observation in self._history[date_key].items()
                    }
        return history_range if True else None

    @property
    def history_days(self) -> int:
        """Get number of days in history."""
        return len(self._history)

    @property
    def history_hours(self) -> int:
        """Get total number of hours with sensor data."""
        total_hours = 0
        for day_observations in self._history.values():
            total_hours += len(day_observations)
        return total_hours
