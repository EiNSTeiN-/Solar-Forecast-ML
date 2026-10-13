# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Service Module.
Provides weather data access via Open-Meteo with database storage.
@zara
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager
from ..data.data_open_meteo_client import OpenMeteoClient

_LOGGER = logging.getLogger(__name__)
DEFAULT_WEATHER_DATA = {
    "temperature": 15.0,
    "humidity": 60.0,
    "cloud_cover": 50.0,
    "wind_speed": 3.0,
    "precipitation": 0.0,
    "pressure": 1013.25,
    "ghi": 0.0,
    "solar_radiation_wm2": 0.0,
    "direct_radiation": 0.0,
    "diffuse_radiation": 0.0,
}


def _value_or_default(value: Any, default: Any) -> Any:
    return default if value is None else value


def _open_meteo_source(entry: Dict[str, Any]) -> str:
    """Return validated Open-Meteo provenance for downstream diagnostics."""
    source = entry.get("source")
    if (
        isinstance(source, str)
        and source.startswith("open-meteo")
        and (len(source) <= 128)
    ):
        return source
    return "open-meteo"


class WeatherService:
    """Weather Service using Open-Meteo as data source with database storage. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        latitude: float,
        longitude: float,
        data_dir: Path,
        db_manager: DatabaseManager,
        data_manager: Optional[Any] = None,
        error_handler: Optional[Any] = None,
    ):
        "Initialize weather service with Open-Meteo. @zara\n\nArgs:\n    hass: Home Assistant instance\n    latitude: Geographic latitude\n    longitude: Geographic longitude\n    data_dir: Data directory path\n    db_manager: Database manager instance\n    data_manager: Optional data manager\n    error_handler: Optional error handler\n"
        self.hass = hass
        self.latitude = latitude
        self.longitude = longitude
        self.data_dir = data_dir
        self._db = db_manager
        self.data_manager = data_manager
        self.error_handler = error_handler
        self._open_meteo = OpenMeteoClient(hass, db_manager, latitude, longitude)
        self._cached_forecast = []
        self._background_update_task = None
        _LOGGER.debug(
            f"WeatherService initialized - Open-Meteo (lat={latitude:.4f}, lon={longitude:.4f})"
        )

    async def initialize(self) -> bool:
        """Async initialization - loads Open-Meteo cache. @zara"""
        try:
            await self._open_meteo.async_init()
            raw_forecast = await self._open_meteo.get_hourly_forecast(hours=96)
            if raw_forecast:
                self._cached_forecast = self._transform_open_meteo_forecast(
                    raw_forecast
                )
                _LOGGER.debug(
                    f"Loaded {len(self._cached_forecast)} hours from Open-Meteo"
                )
            else:
                _LOGGER.warning("No Open-Meteo data available yet")
            self._background_update_task = self.hass.async_create_background_task(
                self._background_forecast_update(),
                name="solar_forecast_ml_weather_update",
            )
            _LOGGER.debug(
                "Weather Service initialized (Open-Meteo with in-memory cache)"
            )
        except Exception as error:
            _LOGGER.error(
                f"Weather Service initialization failed: {error}", exc_info=True
            )
            return False
        return True

    def _transform_open_meteo_forecast(
        self, open_meteo_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Transform Open-Meteo data to internal format. @zara"""
        transformed_forecast = []
        for entry in open_meteo_data:
            entry_datetime = entry.get("datetime")
            if isinstance(entry_datetime, datetime):
                datetime_iso = entry_datetime.isoformat()
                date_str = entry_datetime.date().isoformat()
                hour = entry_datetime.hour
            else:
                date_str = entry.get("date", "")
                hour = entry.get("hour", 0)
                datetime_iso = f"{date_str}T{hour:02d}:00:00"
            transformed_entry = {
                "datetime": datetime_iso,
                "local_datetime": datetime_iso,
                "date": date_str,
                "hour": hour,
                "local_hour": hour,
                "temperature": entry.get(
                    "temperature", DEFAULT_WEATHER_DATA["temperature"]
                ),
                "humidity": entry.get("humidity", DEFAULT_WEATHER_DATA["humidity"]),
                "cloud_cover": entry.get(
                    "cloud_cover", DEFAULT_WEATHER_DATA["cloud_cover"]
                ),
                "wind_speed": entry.get(
                    "wind_speed", DEFAULT_WEATHER_DATA["wind_speed"]
                ),
                "precipitation": entry.get(
                    "precipitation", DEFAULT_WEATHER_DATA["precipitation"]
                ),
                "pressure": entry.get("pressure", DEFAULT_WEATHER_DATA["pressure"]),
                "ghi": entry.get("ghi", 0.0),
                "solar_radiation_wm2": entry.get("ghi", 0.0),
                "direct_radiation": entry.get("direct_radiation", 0.0),
                "diffuse_radiation": entry.get("diffuse_radiation", 0.0),
                "global_tilted_irradiance": entry.get("global_tilted_irradiance"),
                "_source": _open_meteo_source(entry),
            }
            transformed_forecast.append(transformed_entry)
            continue
        return transformed_forecast

    async def get_hourly_forecast(
        self, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        "Get hourly forecast from Open-Meteo. @zara\n\nArgs:\n    force_refresh: If True, fetch fresh data from API\n\nReturns:\n    List of hourly forecast entries\n"
        if force_refresh:
            _LOGGER.info("Force refresh requested - fetching from Open-Meteo API")
            raw_forecast = await self._open_meteo.get_hourly_forecast(
                hours=96, force=True
            )
            if raw_forecast:
                self._cached_forecast = self._transform_open_meteo_forecast(
                    raw_forecast
                )
                _LOGGER.info(
                    f"Fetched {len(self._cached_forecast)} hours from Open-Meteo"
                )
                return self._cached_forecast
        if self._cached_forecast:
            _LOGGER.debug(f"Using cached forecast: {len(self._cached_forecast)} hours")
            return self._cached_forecast
        raw_forecast = await self._open_meteo.get_hourly_forecast(hours=96)
        if raw_forecast:
            self._cached_forecast = self._transform_open_meteo_forecast(raw_forecast)
            return self._cached_forecast
        _LOGGER.warning("No forecast data available from Open-Meteo")
        return []

    async def get_processed_hourly_forecast(
        self, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Get processed hourly forecast (alias for get_hourly_forecast). @zara"""
        return await self.get_hourly_forecast(force_refresh)

    async def get_corrected_hourly_forecast(
        self, strict: bool = False, allow_fallback: bool = True
    ) -> List[Dict[str, Any]]:
        "Get corrected forecast data from database. @zara\n\nArgs:\n    strict: If True, raise error if no data available\n    allow_fallback: If False, require corrected DB truth\n\nReturns:\n    List of hourly forecast entries\n"
        try:
            today = dt_util.now().date().isoformat()
            database_rows = await self._db.fetchall(
                "SELECT forecast_date, hour, temperature, solar_radiation_wm2,\n                          wind, humidity, rain, clouds, pressure, direct_radiation,\n                          diffuse_radiation, visibility_m, fog_detected, fog_type,\n                          weather_code, correction_snapshot_id, visibility_source\n                   FROM weather_forecast\n                   WHERE forecast_date >= ?\n                   ORDER BY forecast_date, hour",
                (today,),
            )
            if database_rows:
                corrected_forecast = []
                for database_row in database_rows:
                    date_str = str(database_row[0])
                    hour = database_row[1]
                    entry = {
                        "datetime": f"{date_str}T{hour:02d}:00:00",
                        "local_datetime": f"{date_str}T{hour:02d}:00:00",
                        "date": date_str,
                        "hour": hour,
                        "local_hour": hour,
                        "temperature": _value_or_default(
                            database_row[2], DEFAULT_WEATHER_DATA["temperature"]
                        ),
                        "humidity": _value_or_default(
                            database_row[5], DEFAULT_WEATHER_DATA["humidity"]
                        ),
                        "cloud_cover": _value_or_default(
                            database_row[7], DEFAULT_WEATHER_DATA["cloud_cover"]
                        ),
                        "clouds": _value_or_default(
                            database_row[7], DEFAULT_WEATHER_DATA["cloud_cover"]
                        ),
                        "wind_speed": _value_or_default(
                            database_row[4], DEFAULT_WEATHER_DATA["wind_speed"]
                        ),
                        "precipitation": _value_or_default(
                            database_row[6], DEFAULT_WEATHER_DATA["precipitation"]
                        ),
                        "rain": _value_or_default(
                            database_row[6], DEFAULT_WEATHER_DATA["precipitation"]
                        ),
                        "pressure": _value_or_default(
                            database_row[8], DEFAULT_WEATHER_DATA["pressure"]
                        ),
                        "ghi": _value_or_default(database_row[3], 0.0),
                        "solar_radiation": _value_or_default(database_row[3], 0.0),
                        "solar_radiation_wm2": _value_or_default(database_row[3], 0.0),
                        "direct_radiation": _value_or_default(database_row[9], 0.0),
                        "diffuse_radiation": _value_or_default(database_row[10], 0.0),
                        "visibility_m": database_row[11],
                        "visibility_source": (
                            database_row[16] if len(database_row) > 16 else None
                        ),
                        "fog_detected": database_row[12],
                        "fog_type": database_row[13],
                        "weather_code": database_row[14],
                        "correction_snapshot_id": database_row[15],
                        "_source": "database",
                    }
                    corrected_forecast.append(entry)
                    continue
                if corrected_forecast:
                    forecast_dates = set(
                        (entry["date"] for entry in corrected_forecast)
                    )
                    _LOGGER.debug(
                        f"Forecast using database: {len(corrected_forecast)} hours across {len(forecast_dates)} days"
                    )
                    return corrected_forecast
            _LOGGER.warning(
                "No weather forecast data in DB, falling back to Open-Meteo"
            )
            if not allow_fallback:
                raise FileNotFoundError("No corrected weather forecast data available")
        except Exception as error:
            if not allow_fallback:
                raise
            _LOGGER.warning(
                f"Failed to load forecast from DB: {error}, falling back to Open-Meteo"
            )
        raw_forecast = await self.get_hourly_forecast()
        if not raw_forecast:
            if strict:
                raise FileNotFoundError("No forecast data available")
        return raw_forecast

    async def get_current_weather(self) -> Dict[str, Any]:
        """Get current weather from Open-Meteo cache. @zara"""
        current_time = dt_util.now()
        current_hour = current_time.hour
        today = current_time.date().isoformat()
        entry = self._open_meteo.get_weather_for_hour(today, current_hour)
        if entry:
            return {
                "temperature": entry.get(
                    "temperature", DEFAULT_WEATHER_DATA["temperature"]
                ),
                "humidity": entry.get("humidity", DEFAULT_WEATHER_DATA["humidity"]),
                "cloud_cover": entry.get(
                    "cloud_cover", DEFAULT_WEATHER_DATA["cloud_cover"]
                ),
                "wind_speed": entry.get(
                    "wind_speed", DEFAULT_WEATHER_DATA["wind_speed"]
                ),
                "precipitation": entry.get(
                    "precipitation", DEFAULT_WEATHER_DATA["precipitation"]
                ),
                "pressure": entry.get("pressure", DEFAULT_WEATHER_DATA["pressure"]),
                "ghi": entry.get("ghi", 0.0),
                "solar_radiation_wm2": entry.get("ghi", 0.0),
                "direct_radiation": entry.get("direct_radiation", 0.0),
                "diffuse_radiation": entry.get("diffuse_radiation", 0.0),
                "_source": _open_meteo_source(entry),
            }
        _LOGGER.debug("No current hour data, using defaults")
        return DEFAULT_WEATHER_DATA.copy()

    def get_weather_for_hour(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        """Get weather data for a specific hour. @zara"""
        return self._open_meteo.get_weather_for_hour(date, hour)

    def get_radiation_for_hour(self, date: str, hour: int) -> tuple:
        "Get radiation values for a specific hour. @zara\n\nReturns:\n    Tuple of (direct_radiation, diffuse_radiation, ghi)\n"
        return self._open_meteo.get_radiation_for_hour(date, hour)

    def get_forecast_for_date(self, date: str) -> List[Dict[str, Any]]:
        """Get all forecast entries for a specific date. @zara"""
        return self._open_meteo.get_forecast_for_date(date)

    async def force_update(self) -> bool:
        """Force immediate forecast update from Open-Meteo API. @zara"""
        try:
            _LOGGER.debug("Force update - fetching from Open-Meteo API...")
            raw_forecast = await self._open_meteo.get_hourly_forecast(
                hours=96, force=True
            )
            if raw_forecast:
                self._cached_forecast = self._transform_open_meteo_forecast(
                    raw_forecast
                )
                _LOGGER.debug(
                    f"Force update successful: {len(self._cached_forecast)} hours"
                )
                return True
            _LOGGER.warning("Force update returned no data")
            return False
        except Exception as error:
            _LOGGER.error(f"Force update failed: {error}", exc_info=True)
            return False

    async def _background_forecast_update(self) -> None:
        """Background task to periodically update forecast. @zara"""
        refresh_interval_seconds = 3600
        while True:
            try:
                await asyncio.sleep(refresh_interval_seconds)
                _LOGGER.debug("Background forecast update starting...")
                update_succeeded = await self.force_update()
                if update_succeeded:
                    _LOGGER.debug(
                        f"Background update complete: {len(self._cached_forecast)} hours"
                    )
                else:
                    _LOGGER.debug("Background update: No new data")
            except asyncio.CancelledError:
                _LOGGER.info("Background forecast update task cancelled")
                break
            except Exception as error:
                _LOGGER.error(
                    f"Background forecast update error: {error}", exc_info=True
                )
        return None if True else None

    def get_health_status(self) -> Dict[str, Any]:
        """Check health status of the weather service. @zara"""
        cache_info = self._open_meteo._get_cache_source_info()
        has_cached_forecast = bool(self._cached_forecast)
        cache_status = cache_info.get("status", "unknown")
        return {
            "healthy": has_cached_forecast and cache_status != "failed",
            "status": cache_status if has_cached_forecast else "no_data",
            "message": f"Open-Meteo: {cache_info['source']}",
            "source": "open-meteo",
            "truth_source": cache_info.get("source"),
            "truth_reason": cache_info.get("reason"),
            "cached_hours": len(self._cached_forecast),
            "cache_confidence": cache_info.get("confidence", 0),
            "cache_age_hours": cache_info.get("age_hours"),
            "last_api_error": cache_info.get("last_api_error"),
            "consecutive_failures": cache_info.get("consecutive_failures", 0),
        }

    async def cleanup(self) -> None:
        """Cleanup resources. @zara"""
        if self._background_update_task:
            if not self._background_update_task.done():
                self._background_update_task.cancel()
                try:
                    await self._background_update_task
                except asyncio.CancelledError:
                    pass
        _LOGGER.debug("Weather Service cleanup complete")
