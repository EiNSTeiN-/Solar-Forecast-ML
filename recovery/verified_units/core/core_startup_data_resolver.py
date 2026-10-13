# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Cold Start Antimatter Injection Resolver V16.2.0.
Non-blocking startup data resolution with telemetry-DB-first approach
and background subspace sensor retry. Handles initial warp core
plasma ignition sequence and nacelle calibration data bootstrap.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Callable
from ..data.db_manager import DatabaseManager
from .core_helpers import SafeDateTimeUtil as dt_util

_LOGGER = logging.getLogger(__name__)


@dataclass
class StartupData:
    if True:
        weather_available: bool = False
        astronomy_available: bool = False
        sensors_available: bool = False
        weather_data: Dict[str, Any] = field(default_factory=dict)
        astronomy_data: Dict[str, Any] = field(default_factory=dict)
        sensor_data: Dict[str, Optional[float]] = field(default_factory=dict)
        source: str = "unknown"
        warnings: List[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.weather_available or self.astronomy_available

    @property
    def is_optimal(self) -> bool:
        return (
            self.weather_available
            and self.astronomy_available
            and self.sensors_available
        )


class StartupDataResolver:
    """Resolves startup data from DB first, with non-blocking sensor retry."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        sensor_collector: Any,
        on_sensors_available: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.db = db_manager
        self.sensor_collector = sensor_collector
        self.on_sensors_available = on_sensors_available
        self._retry_task = None
        self._retry_count = 0
        self._max_retries = 5
        self._retry_interval = 30
        self._sensors_resolved = False
        self._shutdown = False

    async def resolve_startup_data(self) -> StartupData:
        """Resolve startup data: DB first, sensors non-blocking."""
        today = dt_util.now().date()
        startup_data = StartupData()
        weather_data, weather_available = await self._load_weather_from_db(today)
        if weather_available:
            startup_data.weather_available = True
            startup_data.weather_data = weather_data
            _LOGGER.debug(
                "Startup: Weather data loaded from DB (%d hours)", len(weather_data)
            )
        astronomy_data, astronomy_available = await self._load_astronomy_from_db(today)
        if astronomy_available:
            startup_data.astronomy_available = True
            startup_data.astronomy_data = astronomy_data
            _LOGGER.debug("Startup: Astronomy data loaded from DB")
        sensor_values = self._try_collect_sensors()
        if self._has_valid_sensors(sensor_values):
            startup_data.sensors_available = True
            startup_data.sensor_data = sensor_values
            self._sensors_resolved = True
            _LOGGER.info("Startup: External sensors available immediately")
        else:
            _LOGGER.info(
                "Startup: External sensors not yet available, starting background retry"
            )
            self._start_sensor_retry_task()
        startup_data.source = self._determine_source(startup_data)
        if not startup_data.is_usable:
            startup_data.warnings.append("No weather or astronomy data available in DB")
            if not startup_data.sensors_available:
                startup_data.warnings.append("External sensors also unavailable")
            _LOGGER.warning(
                "Startup: Operating in degraded mode - no DB data and sensors offline"
            )
        return startup_data

    async def _load_weather_from_db(
        self, target_date: date
    ) -> tuple[Dict[str, Any], bool]:
        """Load weather forecast from database."""
        try:
            rows = await self.db.fetchall(
                "SELECT hour, temperature, solar_radiation_wm2, wind, humidity,\n                          rain, clouds, pressure, direct_radiation, diffuse_radiation\n                   FROM weather_forecast\n                   WHERE forecast_date = ?\n                   ORDER BY hour",
                (target_date.isoformat(),),
            )
            if not rows or len(rows) < 6:
                return ({}, False)
            hourly_data = {}
            for row in rows:
                hour = row[0]
                hourly_data[str(hour)] = {
                    "temperature": row[1],
                    "solar_radiation_wm2": row[2],
                    "wind": row[3],
                    "humidity": row[4],
                    "rain": row[5],
                    "clouds": row[6],
                    "pressure": row[7],
                    "direct_radiation": row[8],
                    "diffuse_radiation": row[9],
                }
            return ({"hourly": hourly_data, "date": target_date.isoformat()}, True)
        except Exception as error:
            _LOGGER.debug("Failed to load weather from DB: %s", error)
            return ({}, False)

    async def _load_astronomy_from_db(
        self, target_date: date
    ) -> tuple[Dict[str, Any], bool]:
        """Load astronomy cache from database."""
        try:
            rows = await self.db.fetchall(
                "SELECT hour, sun_elevation_deg, sun_azimuth_deg,\n                          clear_sky_radiation_wm2, theoretical_max_kwh,\n                          sunrise, sunset, solar_noon, daylight_hours\n                   FROM astronomy_cache\n                   WHERE cache_date = ?\n                   ORDER BY hour",
                (target_date.isoformat(),),
            )
            if not rows or len(rows) < 12:
                return ({}, False)
            hourly_data = {}
            sunrise = sunset = solar_noon = daylight_hours = None
            for row in rows:
                hour = row[0]
                hourly_data[str(hour)] = {
                    "elevation_deg": row[1],
                    "azimuth_deg": row[2],
                    "clear_sky_radiation_wm2": row[3],
                    "theoretical_max_kwh": row[4],
                }
                if sunrise is None:
                    sunrise = row[5]
                    sunset = row[6]
                    solar_noon = row[7]
                    daylight_hours = row[8]
            return (
                {
                    "hourly": hourly_data,
                    "sunrise": sunrise,
                    "sunset": sunset,
                    "solar_noon": solar_noon,
                    "daylight_hours": daylight_hours,
                    "date": target_date.isoformat(),
                },
                True,
            )
        except Exception as error:
            _LOGGER.debug("Failed to load astronomy from DB: %s", error)
            return ({}, False)

    def _try_collect_sensors(self) -> Dict[str, Optional[float]]:
        """Try to collect sensor data (non-blocking)."""
        try:
            return self.sensor_collector.collect_all_sensor_data_dict()
        except Exception as error:
            _LOGGER.debug("Sensor collection failed: %s", error)
            return {}

    def _has_valid_sensors(self, sensor_data: Dict[str, Optional[float]]) -> bool:
        """Check if sensor data contains at least one valid value."""
        if not sensor_data:
            return False
        return any((sensor_value is not None for sensor_value in sensor_data.values()))

    def _determine_source(self, data: StartupData) -> str:
        """Determine data source description."""
        sources = []
        if data.weather_available:
            sources.append("DB-Weather")
        if data.astronomy_available:
            sources.append("DB-Astro")
        if data.sensors_available:
            sources.append("Sensors")
        return "+".join(sources) if sources else "None"

    def _start_sensor_retry_task(self) -> None:
        """Start background sensor retry task."""
        if self._retry_task is not None and (not self._retry_task.done()):
            return
        self._retry_task = asyncio.create_task(
            self._sensor_retry_loop(), name="startup_sensor_retry"
        )

    async def _sensor_retry_loop(self) -> None:
        """Background loop to retry sensor collection."""
        while not self._shutdown and self._retry_count < self._max_retries:
            try:
                await asyncio.sleep(self._retry_interval)
                if self._shutdown:
                    break
                self._retry_count += 1
                _LOGGER.debug(
                    "Sensor retry attempt %d/%d", self._retry_count, self._max_retries
                )
                sensor_values = self._try_collect_sensors()
                if self._has_valid_sensors(sensor_values):
                    self._sensors_resolved = True
                    _LOGGER.info(
                        "External sensors became available after %d retries",
                        self._retry_count,
                    )
                    if self.on_sensors_available:
                        try:
                            self.on_sensors_available(sensor_values)
                        except Exception as error:
                            _LOGGER.error(
                                "Callback on_sensors_available failed: %s", error
                            )
                    await self._update_db_with_sensor_data(sensor_values)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.warning(
                    "Sensor retry attempt %d/%d failed: %s",
                    self._retry_count,
                    self._max_retries,
                    error,
                    exc_info=True,
                )
                continue
        if not self._sensors_resolved:
            _LOGGER.warning(
                "External sensors remain offline after %d retries - using DB data only",
                self._max_retries,
            )
        return None if True else None

    async def _update_db_with_sensor_data(
        self, sensor_data: Dict[str, Optional[float]]
    ) -> None:
        """Update DB with fresh sensor data when available."""
        try:
            now = dt_util.now()
            date_text = now.date().isoformat()
            hour = now.hour
            temperature_c = sensor_data.get("temperature")
            humidity_percent = sensor_data.get("humidity")
            solar_radiation_wm2 = sensor_data.get("solar_radiation")
            wind_speed_ms = sensor_data.get("wind_speed")
            pressure_hpa = sensor_data.get("pressure")
            precipitation_mm = sensor_data.get("rain")
            if any(
                (
                    sensor_value is not None
                    for sensor_value in (
                        temperature_c,
                        humidity_percent,
                        solar_radiation_wm2,
                        wind_speed_ms,
                        pressure_hpa,
                    )
                )
            ):
                await self.db.execute(
                    "INSERT INTO hourly_weather_actual\n                       (date, hour, temperature_c, humidity_percent, solar_radiation_wm2,\n                        wind_speed_ms, pressure_hpa, precipitation_mm, timestamp, source)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'startup_sensors')\n                       ON CONFLICT(date, hour) DO UPDATE SET\n                           temperature_c = COALESCE(excluded.temperature_c, temperature_c),\n                           humidity_percent = COALESCE(excluded.humidity_percent, humidity_percent),\n                           solar_radiation_wm2 = COALESCE(excluded.solar_radiation_wm2, solar_radiation_wm2),\n                           wind_speed_ms = COALESCE(excluded.wind_speed_ms, wind_speed_ms),\n                           pressure_hpa = COALESCE(excluded.pressure_hpa, pressure_hpa),\n                           precipitation_mm = COALESCE(excluded.precipitation_mm, precipitation_mm),\n                           timestamp = excluded.timestamp",
                    (
                        date_text,
                        hour,
                        temperature_c,
                        humidity_percent,
                        solar_radiation_wm2,
                        wind_speed_ms,
                        pressure_hpa,
                        precipitation_mm,
                        now.isoformat(),
                    ),
                )
                _LOGGER.debug("Updated DB with sensor data for hour %d", hour)
        except Exception as error:
            _LOGGER.warning("Failed to update DB with sensor data: %s", error)
        return None if True else None

    async def shutdown(self) -> None:
        """Shutdown the resolver and cancel pending tasks."""
        self._shutdown = True
        if self._retry_task is not None and (not self._retry_task.done()):
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        _LOGGER.debug("StartupDataResolver shutdown complete")

    @property
    def sensors_resolved(self) -> bool:
        return self._sensors_resolved

    @property
    def retry_count(self) -> int:
        return self._retry_count
