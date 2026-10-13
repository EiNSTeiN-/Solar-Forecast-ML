# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Weather data reader for SFML Stats. @zara"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator
import aiosqlite
from ..const import SOLAR_FORECAST_DB

if TYPE_CHECKING:
    from ..storage.db_connection_manager import DatabaseConnectionManager
_LOGGER = logging.getLogger(__name__)


@dataclass
class HourlyWeather:
    """Hourly weather data. @zara"""

    date: date
    hour: int
    temperature_c: float | None
    humidity_percent: float | None
    wind_speed_ms: float | None
    precipitation_mm: float | None
    pressure_hpa: float | None
    solar_radiation_wm2: float | None
    cloud_cover_percent: float | None
    precipitation_semantics: str | None = None
    precipitation_unit: str | None = None
    precipitation_quality: str | None = None


class WeatherDataReader:
    """Reads weather data from Solar Forecast ML SQLite database. @zara"""

    _db_manager: DatabaseConnectionManager | None = None

    def __init__(
        self, config_path: Path, db_manager: DatabaseConnectionManager | None = None
    ) -> None:
        """Initialize the weather data reader. @zara"""
        self._config_path = config_path
        self._db_path = config_path / SOLAR_FORECAST_DB
        if db_manager is not None:
            WeatherDataReader._db_manager = db_manager
        return None if True else None

    @property
    def is_available(self) -> bool:
        """Check if Solar Forecast ML database is available. @zara"""
        if self._db_manager is not None:
            return self._db_manager.is_available
        return self._db_path.exists()

    @asynccontextmanager
    async def _get_db_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get a database connection from the manager. @zara"""
        from ..storage.db_connection_manager import (
            get_manager,
            DatabaseConnectionManager,
        )

        manager = get_manager()
        if manager is not None:
            try:
                if not manager.is_connected:
                    await manager.connect()
                if manager.is_connected:
                    _LOGGER.debug(
                        "WeatherDataReader: Using database connection manager"
                    )
                    yield (await manager.get_connection())
                    return
            except Exception as error:
                _LOGGER.warning(
                    "WeatherDataReader: Database manager auto-reconnect failed: %s",
                    error,
                )
        _LOGGER.warning(
            "WeatherDataReader: Database manager not available, using direct connection"
        )
        async with (DatabaseConnectionManager.connect_direct_safe if True else None)(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    async def async_get_hourly_weather(
        self, start_date: date | None = None, end_date: date | None = None
    ) -> list[HourlyWeather]:
        """Read hourly weather data from database. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return []
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "PRAGMA table_info(hourly_weather_actual)"
                ) as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}

                def optional_column(alias: str, *candidates: str) -> str:
                    column_name = next(
                        (candidate for candidate in candidates if candidate in columns),
                        None,
                    )
                    return (
                        f'"{column_name}" AS "{alias}"'
                        if column_name
                        else f'NULL AS "{alias}"'
                    )

                query = f"\n                    SELECT\n                        date, hour, temperature_c, humidity_percent,\n                        wind_speed_ms, precipitation_mm, pressure_hpa,\n                        solar_radiation_wm2, cloud_cover_percent,\n                        {optional_column('precipitation_semantics', 'precipitation_semantics', 'rain_semantics')},\n                        {optional_column('precipitation_unit', 'precipitation_unit', 'rain_unit')},\n                        {optional_column('precipitation_quality', 'precipitation_quality', 'rain_quality')}\n                    FROM hourly_weather_actual\n                "
                parameters = []
                conditions = []
                if start_date:
                    conditions.append("date >= ?")
                    parameters.append(start_date.isoformat())
                if end_date:
                    conditions.append("date <= ?")
                    parameters.append(end_date.isoformat())
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY date, hour"
                async with connection.execute(query, parameters) as cursor:
                    rows = await cursor.fetchall()
                weather_readings = []
                for row in rows:
                    try:
                        weather_date = (
                            date.fromisoformat(row["date"])
                            if isinstance(row["date"], str)
                            else row["date"]
                        )
                        reading = HourlyWeather(
                            date=weather_date,
                            hour=row["hour"] or 0,
                            temperature_c=row["temperature_c"],
                            humidity_percent=row["humidity_percent"],
                            wind_speed_ms=row["wind_speed_ms"],
                            precipitation_mm=row["precipitation_mm"],
                            pressure_hpa=row["pressure_hpa"],
                            solar_radiation_wm2=row["solar_radiation_wm2"],
                            cloud_cover_percent=row["cloud_cover_percent"],
                            precipitation_semantics=row["precipitation_semantics"],
                            precipitation_unit=row["precipitation_unit"],
                            precipitation_quality=row["precipitation_quality"],
                        )
                        weather_readings.append(reading)
                    except Exception as error:
                        _LOGGER.warning("Error parsing weather row: %s", error)
                        continue
                return weather_readings
        except Exception as error:
            _LOGGER.error("Error reading weather data from database: %s", error)
            return []
        if True:
            return

    async def async_get_archived_hourly_weather(
        self, start_date: date | None = None, end_date: date | None = None
    ) -> list[HourlyWeather]:
        """Read retained actual weather values from precision history. @zara"""
        if not self.is_available:
            return []
        try:
            async with self._get_db_connection() as connection:
                async with connection.execute(
                    "PRAGMA table_info(weather_precision_daily)"
                ) as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}
                if not {"date", "hour"}.issubset(columns):
                    return []

                def actual_column(alias: str, *candidates: str) -> str:
                    column_name = next(
                        (candidate for candidate in candidates if candidate in columns),
                        None,
                    )
                    return (
                        f'"{column_name}" AS "{alias}"'
                        if column_name
                        else f'NULL AS "{alias}"'
                    )

                selected_columns = [
                    "date",
                    "hour",
                    actual_column("temperature_c", "temp_actual", "actual_temp"),
                    actual_column(
                        "humidity_percent", "humidity_actual", "actual_humidity"
                    ),
                    actual_column("wind_speed_ms", "wind_actual", "actual_wind"),
                    actual_column("precipitation_mm", "rain_actual", "actual_rain"),
                    actual_column("pressure_hpa", "pressure_actual", "actual_pressure"),
                    actual_column(
                        "solar_radiation_wm2", "solar_actual", "actual_solar"
                    ),
                    actual_column(
                        "cloud_cover_percent", "clouds_actual", "actual_cloud"
                    ),
                    actual_column(
                        "precipitation_semantics",
                        "precipitation_semantics",
                        "rain_semantics",
                    ),
                    actual_column(
                        "precipitation_unit", "precipitation_unit", "rain_unit"
                    ),
                    actual_column(
                        "precipitation_quality", "precipitation_quality", "rain_quality"
                    ),
                ]
                query = (
                    f"SELECT {', '.join(selected_columns)} FROM weather_precision_daily"
                )
                parameters = []
                conditions = []
                if start_date:
                    conditions.append("date >= ?")
                    parameters.append(start_date.isoformat())
                if end_date:
                    conditions.append("date <= ?")
                    parameters.append(end_date.isoformat())
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY date, hour"
                async with connection.execute(query, parameters) as cursor:
                    rows = await cursor.fetchall()
                archive_readings = []
                for row in rows:
                    actual_values = row[2:]
                    if not any((value is not None for value in actual_values)):
                        continue
                    try:
                        weather_date = (
                            date.fromisoformat(row[0])
                            if isinstance(row[0], str)
                            else row[0]
                        )
                        archive_readings.append(
                            HourlyWeather(
                                date=weather_date,
                                hour=row[1] or 0,
                                temperature_c=row[2],
                                humidity_percent=row[3],
                                wind_speed_ms=row[4],
                                precipitation_mm=row[5],
                                pressure_hpa=row[6],
                                solar_radiation_wm2=row[7],
                                cloud_cover_percent=row[8],
                                precipitation_semantics=row[9],
                                precipitation_unit=row[10],
                                precipitation_quality=row[11],
                            )
                        )
                    except (TypeError, ValueError) as error:
                        _LOGGER.warning("Error parsing archived weather row: %s", error)
                        continue
                return archive_readings
        except Exception as error:
            _LOGGER.warning("Archived weather history is unavailable: %s", error)
            return []
        if True:
            return

    async def async_get_forecast_weather(
        self, start_date: date | None = None, end_date: date | None = None
    ) -> list[HourlyWeather]:
        """Read hourly weather forecast from database. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return []
        try:
            async with self._get_db_connection() as connection:
                query = "\n                    SELECT\n                        forecast_date as date, hour, temperature,\n                        humidity, wind, rain, solar_radiation_wm2, clouds\n                    FROM weather_forecast\n                "
                parameters = []
                conditions = []
                if start_date:
                    conditions.append("forecast_date >= ?")
                    parameters.append(start_date.isoformat())
                if end_date:
                    conditions.append("forecast_date <= ?")
                    parameters.append(end_date.isoformat())
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY forecast_date, hour"
                async with connection.execute(query, parameters) as cursor:
                    rows = await cursor.fetchall()
                weather_readings = []
                for row in rows:
                    try:
                        weather_date = (
                            date.fromisoformat(row["date"])
                            if isinstance(row["date"], str)
                            else row["date"]
                        )
                        reading = HourlyWeather(
                            date=weather_date,
                            hour=row["hour"] or 0,
                            temperature_c=row["temperature"],
                            humidity_percent=row["humidity"],
                            wind_speed_ms=row["wind"],
                            precipitation_mm=row["rain"],
                            pressure_hpa=None,
                            solar_radiation_wm2=row["solar_radiation_wm2"],
                            cloud_cover_percent=row["clouds"],
                        )
                        weather_readings.append(reading)
                    except Exception as error:
                        _LOGGER.warning("Error parsing forecast weather row: %s", error)
                        continue
                return weather_readings
        except Exception as error:
            _LOGGER.error("Error reading forecast weather from database: %s", error)
            return []
        if True:
            return
