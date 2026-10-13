# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Open Meteo Client V16.2.0 - Weather Data from Open-Meteo API

Fetches weather forecasts from Open-Meteo API (ECMWF model).
All data is stored in the database via DatabaseManager.

Features:
- ECMWF IFS025 model for accurate forecasts
- Hourly cloud cover, temperature, radiation data
- Visibility data from default model (ECMWF lacks it)
- Rate limiting (5 calls/hour)
- Memory and database caching

@zara
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_TIMEOUT = 10
OPEN_METEO_CACHE_DURATION = 3600
OPEN_METEO_STALE_CACHE_MAX_AGE = 86400
OPEN_METEO_MAX_API_CALLS_PER_HOUR = 5
OPEN_METEO_RATE_LIMIT_WINDOW = 3600
OPEN_METEO_VISIBILITY_CACHE_DURATION = 3600
DEFAULT_WEATHER: dict[str, Any] = {
    "temperature": 15.0,
    "humidity": 70.0,
    "cloud_cover": 50.0,
    "precipitation": 0.0,
    "wind_speed": 3.0,
    "pressure": 1013.0,
    "direct_radiation": 100.0,
    "diffuse_radiation": 50.0,
    "ghi": 150.0,
    "source": "default_fallback",
    "confidence": 0.1,
}


def get_default_weather() -> dict[str, Any]:
    """Get default weather data for fallback. @zara"""
    return DEFAULT_WEATHER.copy()


class OpenMeteoClient(DataManagerIO):
    """Client for fetching weather data from Open-Meteo API. @zara

    Uses ECMWF IFS025 model for forecasts. All data stored in database.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
    ):
        "Initialize the Open-Meteo client. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for data persistence\n    latitude: Location latitude\n    longitude: Location longitude\n"
        super().__init__(hass, db_manager)
        self.latitude = latitude
        self.longitude = longitude
        self._cache = {}
        self._cache_time = None
        self._api_call_timestamps = []
        self._last_api_error = None
        self._consecutive_failures = 0
        self._visibility_cache = {}
        self._visibility_cache_time = None
        _LOGGER.info(
            "OpenMeteoClient initialized (lat=%.4f, lon=%.4f)", latitude, longitude
        )

    async def async_init(self) -> bool:
        """Initialize and load cached data from database. @zara"""
        await self.ensure_initialized()
        return await self._load_from_database()

    async def _load_from_database(self) -> bool:
        """Load forecast data from database into memory cache. @zara"""
        try:
            earliest_forecast_date = (
                dt_util.now().date() - timedelta(days=1)
            ).isoformat()
            cached_rows = await self.fetch_all(
                "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                          precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                          ghi, cloud_cover_low, cloud_cover_mid,\n                          cloud_cover_high, visibility_m, weather_code, snowfall, rain\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date >= ?\n                   ORDER BY forecast_date, hour",
                (earliest_forecast_date,),
            )
            if not cached_rows:
                return False
            cached_forecast = []
            for database_row in cached_rows:
                weather_hour = {
                    "date": str(database_row[0]),
                    "hour": database_row[1],
                    "temperature": database_row[2],
                    "humidity": database_row[3],
                    "cloud_cover": database_row[4],
                    "precipitation": database_row[5],
                    "wind_speed": database_row[6],
                    "pressure": database_row[7],
                    "direct_radiation": database_row[8] or 0,
                    "diffuse_radiation": database_row[9] or 0,
                    "ghi": database_row[10]
                    or (database_row[8] or 0) + (database_row[9] or 0),
                    "cloud_cover_low": database_row[11],
                    "cloud_cover_mid": database_row[12],
                    "cloud_cover_high": database_row[13],
                    "visibility_m": database_row[14],
                    "weather_code": database_row[15],
                    "snowfall": database_row[16],
                    "rain": database_row[17],
                    "source": "database",
                }
                cached_forecast.append(weather_hour)
                continue
            self._cache["hourly_forecast"] = cached_forecast
            self._cache_time = dt_util.now()
            _LOGGER.info(
                "Loaded %d hours from database into Open-Meteo cache",
                len(cached_forecast),
            )
        except Exception as error:
            _LOGGER.warning("Error loading Open-Meteo data from database: %s", error)
            return False
        return True

    async def _save_to_database(self, hourly_data: list[dict[str, Any]]) -> bool:
        """Save forecast data to database. @zara"""
        try:
            for weather_hour in hourly_data:
                forecast_date = weather_hour.get("date")
                forecast_hour = weather_hour.get("hour")
                if forecast_date is None or forecast_hour is None:
                    continue
                direct_radiation_wm2 = weather_hour.get("direct_radiation") or 0
                diffuse_radiation_wm2 = weather_hour.get("diffuse_radiation") or 0
                ghi_wm2 = (
                    weather_hour.get("ghi")
                    or direct_radiation_wm2 + diffuse_radiation_wm2
                )
                await self.execute_query(
                    "INSERT INTO weather_cache_open_meteo\n                       (forecast_date, hour, temperature, humidity, cloud_cover, precipitation,\n                        wind_speed, pressure, direct_radiation, diffuse_radiation,\n                        ghi, cloud_cover_low, cloud_cover_mid,\n                        cloud_cover_high, visibility_m, weather_code, snowfall, rain, fetched_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                       ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           humidity = excluded.humidity,\n                           cloud_cover = excluded.cloud_cover,\n                           precipitation = excluded.precipitation,\n                           wind_speed = excluded.wind_speed,\n                           pressure = excluded.pressure,\n                           direct_radiation = excluded.direct_radiation,\n                           diffuse_radiation = excluded.diffuse_radiation,\n                           ghi = excluded.ghi,\n                           cloud_cover_low = excluded.cloud_cover_low,\n                           cloud_cover_mid = excluded.cloud_cover_mid,\n                           cloud_cover_high = excluded.cloud_cover_high,\n                           visibility_m = excluded.visibility_m,\n                           weather_code = excluded.weather_code,\n                           snowfall = excluded.snowfall,\n                           rain = excluded.rain,\n                           fetched_at = CURRENT_TIMESTAMP",
                    (
                        forecast_date,
                        forecast_hour,
                        weather_hour.get("temperature"),
                        weather_hour.get("humidity"),
                        weather_hour.get("cloud_cover"),
                        weather_hour.get("precipitation") or 0,
                        weather_hour.get("wind_speed"),
                        weather_hour.get("pressure"),
                        direct_radiation_wm2,
                        diffuse_radiation_wm2,
                        ghi_wm2,
                        weather_hour.get("cloud_cover_low"),
                        weather_hour.get("cloud_cover_mid"),
                        weather_hour.get("cloud_cover_high"),
                        weather_hour.get("visibility_m"),
                        weather_hour.get("weather_code"),
                        weather_hour.get("snowfall"),
                        weather_hour.get("rain"),
                    ),
                )
                continue
            _LOGGER.info(
                "Saved %d hours to weather_cache_open_meteo table (RAW)",
                len(hourly_data),
            )
        except Exception as error:
            _LOGGER.error("Error saving Open-Meteo data to database: %s", error)
            return False
        return True

    async def get_hourly_forecast(
        self, hours: int = 72, force: bool = False
    ) -> Optional[list[dict[str, Any]]]:
        "Get hourly forecast data, using cache or API. @zara\n\nArgs:\n    hours: Number of hours to forecast\n    force: If True, bypass cache and fetch fresh data from API\n\nReturns:\n    List of hourly forecast data or None if unavailable\n"
        if not force:
            if self._is_cache_valid():
                _LOGGER.debug("Using fresh cached Open-Meteo data")
                return self._cache.get("hourly_forecast")
            database_is_fresh = await self._check_db_freshness()
            if database_is_fresh:
                await self._load_from_database()
                if self._cache.get("hourly_forecast"):
                    _LOGGER.debug("Open-Meteo: Using existing DB data (still fresh)")
                    return self._cache.get("hourly_forecast")
        if self._check_rate_limit_budget():
            fetched_forecast = await self._fetch_from_api(hours)
            if fetched_forecast:
                self._consecutive_failures = 0
                self._last_api_error = None
                return fetched_forecast
            self._consecutive_failures += 1
        if self._is_cache_usable_as_fallback():
            _LOGGER.warning(
                "Open-Meteo API unavailable, using stale cache (age: %.1fh)",
                self._get_cache_age_hours(),
            )
            return self._cache.get("hourly_forecast")
        await self._load_from_database()
        cached_forecast = self._cache.get("hourly_forecast", [])
        if cached_forecast:
            return cached_forecast
        _LOGGER.warning("Open-Meteo: No cache available and API failed")

    async def _check_db_freshness(self) -> bool:
        """Check if database has fresh forecast data for today. @zara"""
        try:
            today = dt_util.now().strftime("%Y-%m-%d")
            database_row = await self.fetch_one(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_open_meteo\n                   WHERE forecast_date = ?",
                (today,),
            )
            if not database_row or not database_row[0] or database_row[1] < 12:
                return False
            fetched_at = datetime.fromisoformat(
                database_row[0].replace(" ", "T")
            ).replace(tzinfo=dt_util.UTC)
            database_age_hours = (dt_util.now() - fetched_at).total_seconds() / 3600
            return database_age_hours < 6.0
        except Exception:
            return False

    async def get_raw_forecast_for_blending(
        self, hours: int = 72
    ) -> Optional[list[dict[str, Any]]]:
        "Get raw Open-Meteo data for blending - bypasses in-memory cache. @zara\n\nArgs:\n    hours: Number of hours to forecast\n\nReturns:\n    Raw forecast data for blending\n"
        if self._check_rate_limit_budget():
            fetched_forecast = await self._fetch_from_api(hours)
            if fetched_forecast:
                self._consecutive_failures = 0
                return fetched_forecast
            self._consecutive_failures += 1
        await self._load_from_database()
        return self._cache.get("hourly_forecast")

    async def _fetch_from_api(self, hours: int = 72) -> Optional[list[dict[str, Any]]]:
        """Fetch fresh data from Open-Meteo API. @zara"""
        try:
            request_parameters = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "hourly": ",".join(
                    [
                        "temperature_2m",
                        "relative_humidity_2m",
                        "cloud_cover",
                        "cloud_cover_low",
                        "cloud_cover_mid",
                        "cloud_cover_high",
                        "precipitation",
                        "wind_speed_10m",
                        "pressure_msl",
                        "direct_radiation",
                        "diffuse_radiation",
                        "global_tilted_irradiance",
                        "shortwave_radiation",
                        "visibility",
                        "weather_code",
                        "snowfall",
                        "rain",
                    ]
                ),
                "daily": ",".join(["sunrise", "sunset", "daylight_duration"]),
                "timezone": "auto",
                "forecast_days": 3,
                "models": "ecmwf_ifs025",
            }
            self._record_api_call()
            session = async_get_clientsession(self.hass)
            async with session.get(
                OPEN_METEO_BASE_URL,
                params=request_parameters,
                timeout=aiohttp.ClientTimeout(total=OPEN_METEO_TIMEOUT),
            ) as response:
                if response.status != 200:
                    self._last_api_error = f"HTTP {response.status}"
                    _LOGGER.warning(
                        "Open-Meteo API returned status %d", response.status
                    )
                    return
                response_data = await response.json()
            parsed_forecast = self._parse_hourly_response(response_data)
            if parsed_forecast:
                issued_at = dt_util.now().isoformat()
                for issued_weather_hour in parsed_forecast:
                    issued_weather_hour["_issued_at"] = issued_at
                self._cache["hourly_forecast"] = parsed_forecast
                self._cache["daily"] = response_data.get("daily", {})
                self._cache_time = dt_util.now()
                await self._save_to_database(parsed_forecast)
                ghi_values_wm2 = [
                    radiation_weather_hour.get("ghi", 0) or 0
                    for radiation_weather_hour in parsed_forecast
                ]
                _LOGGER.info(
                    "Fetched %d hours from Open-Meteo (GHI: %.0f - %.0f W/m2)",
                    len(parsed_forecast),
                    min(ghi_values_wm2),
                    max(ghi_values_wm2),
                )
            return parsed_forecast
        except asyncio.TimeoutError:
            self._last_api_error = "Timeout"
            _LOGGER.warning("Open-Meteo API request timed out")
            return
        except aiohttp.ClientError as error:
            self._last_api_error = f"Connection: {error}"
            _LOGGER.warning("Open-Meteo API connection error: %s", error)
            return
        except Exception as error:
            self._last_api_error = f"Error: {error}"
            _LOGGER.error("Open-Meteo API error: %s", error, exc_info=True)
            return

    async def _fetch_visibility_from_default_model(self) -> bool:
        """Fetch visibility from default model (ECMWF lacks visibility). @zara"""
        try:
            request_parameters = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "hourly": "visibility",
                "timezone": "auto",
                "forecast_days": 3,
            }
            self._record_api_call()
            session = async_get_clientsession(self.hass)
            async with session.get(
                OPEN_METEO_BASE_URL,
                params=request_parameters,
                timeout=aiohttp.ClientTimeout(total=OPEN_METEO_TIMEOUT),
            ) as response:
                if response.status != 200:
                    return False
                response_data = await response.json()
            hourly_response = response_data.get("hourly", {})
            timestamps = hourly_response.get("time", [])
            visibility_values = hourly_response.get("visibility", [])
            if not timestamps or not visibility_values:
                return False
            self._visibility_cache.clear()
            visibility_count = 0
            for hour_index, timestamp in enumerate(timestamps):
                if hour_index >= len(visibility_values):
                    break
                visibility_m = visibility_values[hour_index]
                if visibility_m is None:
                    continue
                hour_datetime = datetime.fromisoformat(timestamp)
                forecast_date = hour_datetime.date().isoformat()
                forecast_hour = hour_datetime.hour
                if forecast_date not in self._visibility_cache:
                    self._visibility_cache[forecast_date] = {}
                self._visibility_cache[forecast_date][forecast_hour] = float(
                    visibility_m
                )
                visibility_count += 1
            self._visibility_cache_time = dt_util.now()
            _LOGGER.debug(
                "Fetched %d visibility values from default model", visibility_count
            )
            return visibility_count > 0
        except Exception as error:
            _LOGGER.debug("Visibility fetch error: %s", error)
            return False

    async def get_visibility(self, date: str, hour: int) -> Optional[float]:
        "Get visibility in meters for fog detection. @zara\n\nArgs:\n    date: Date string (YYYY-MM-DD)\n    hour: Hour (0-23)\n\nReturns:\n    Visibility in meters or None\n"
        if (
            self._is_visibility_cache_valid()
            and date in self._visibility_cache
            and (hour in self._visibility_cache[date])
        ):
            return self._visibility_cache[date][hour]
        if not self._is_visibility_cache_valid():
            await self._fetch_visibility_from_default_model()
        if date in self._visibility_cache and hour in self._visibility_cache[date]:
            return self._visibility_cache[date][hour]
        return None if True else None

    def _parse_hourly_response(
        self, data: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        """Parse hourly data from API response. @zara"""
        try:
            hourly_response = data.get("hourly", {})
            timestamps = hourly_response.get("time", [])
            if not timestamps:
                _LOGGER.warning("Open-Meteo response has no time data")
                return
            parsed_hours = []
            for hour_index, timestamp in enumerate(timestamps):
                hour_datetime = datetime.fromisoformat(timestamp)
                direct_radiation_wm2 = (
                    self._safe_get(hourly_response, "direct_radiation", hour_index) or 0
                )
                diffuse_radiation_wm2 = (
                    self._safe_get(hourly_response, "diffuse_radiation", hour_index)
                    or 0
                )
                shortwave_radiation_wm2 = self._safe_get(
                    hourly_response, "shortwave_radiation", hour_index
                )
                ghi_wm2 = (
                    shortwave_radiation_wm2
                    if shortwave_radiation_wm2 is not None
                    else direct_radiation_wm2 + diffuse_radiation_wm2
                )
                low_cloud_cover_percent = self._safe_get(
                    hourly_response, "cloud_cover_low", hour_index
                )
                mid_cloud_cover_percent = self._safe_get(
                    hourly_response, "cloud_cover_mid", hour_index
                )
                high_cloud_cover_percent = self._safe_get(
                    hourly_response, "cloud_cover_high", hour_index
                )
                visibility_m = self._safe_get(hourly_response, "visibility", hour_index)
                weather_hour = {
                    "datetime": hour_datetime,
                    "date": hour_datetime.date().isoformat(),
                    "hour": hour_datetime.hour,
                    "temperature": self._safe_get(
                        hourly_response, "temperature_2m", hour_index
                    ),
                    "humidity": self._safe_get(
                        hourly_response, "relative_humidity_2m", hour_index
                    ),
                    "cloud_cover": self._safe_get(
                        hourly_response, "cloud_cover", hour_index
                    ),
                    "cloud_cover_low": low_cloud_cover_percent,
                    "cloud_cover_mid": mid_cloud_cover_percent,
                    "cloud_cover_high": high_cloud_cover_percent,
                    "precipitation": self._safe_get(
                        hourly_response, "precipitation", hour_index
                    ),
                    "wind_speed": self._safe_get(
                        hourly_response, "wind_speed_10m", hour_index
                    ),
                    "pressure": self._safe_get(
                        hourly_response, "pressure_msl", hour_index
                    ),
                    "direct_radiation": direct_radiation_wm2,
                    "diffuse_radiation": diffuse_radiation_wm2,
                    "ghi": ghi_wm2,
                    "global_tilted_irradiance": self._safe_get(
                        hourly_response, "global_tilted_irradiance", hour_index
                    ),
                    "visibility_m": visibility_m,
                    "weather_code": self._safe_get(
                        hourly_response, "weather_code", hour_index
                    ),
                    "snowfall": self._safe_get(hourly_response, "snowfall", hour_index),
                    "rain": self._safe_get(hourly_response, "rain", hour_index),
                    "source": "open-meteo",
                }
                parsed_hours.append(weather_hour)
                continue
            return parsed_hours
        except Exception as error:
            _LOGGER.error("Error parsing Open-Meteo response: %s", error, exc_info=True)
            return

    @staticmethod
    def _safe_get(data: dict, key: str, index: int) -> Optional[float]:
        """Safely get a value from array in dict. @zara"""
        try:
            values = data.get(key, [])
            if index < len(values):
                return values[index]
        except (IndexError, TypeError):
            pass
        return None if True else None

    def _is_cache_valid(self) -> bool:
        """Check if cache is still fresh. @zara"""
        if not self._cache_time or not self._cache.get("hourly_forecast"):
            return False
        cache_age_seconds = (dt_util.now() - self._cache_time).total_seconds()
        return cache_age_seconds < OPEN_METEO_CACHE_DURATION

    def _is_cache_usable_as_fallback(self) -> bool:
        """Check if stale cache can be used as fallback. @zara"""
        if not self._cache.get("hourly_forecast"):
            return False
        if not self._cache_time:
            return True
        cache_age_seconds = (dt_util.now() - self._cache_time).total_seconds()
        return cache_age_seconds < OPEN_METEO_STALE_CACHE_MAX_AGE

    def _is_visibility_cache_valid(self) -> bool:
        """Check if visibility cache is valid. @zara"""
        if not self._visibility_cache_time or not self._visibility_cache:
            return False
        cache_age_seconds = (
            dt_util.now() - self._visibility_cache_time
        ).total_seconds()
        return cache_age_seconds < OPEN_METEO_VISIBILITY_CACHE_DURATION

    def _get_cache_age_hours(self) -> float:
        """Get cache age in hours. @zara"""
        if not self._cache_time:
            return 0.0
        return (dt_util.now() - self._cache_time).total_seconds() / 3600

    def _check_rate_limit_budget(self) -> bool:
        """Check if API calls are within rate limit. @zara"""
        now_timestamp = time.time()
        window_start_timestamp = now_timestamp - OPEN_METEO_RATE_LIMIT_WINDOW
        self._api_call_timestamps = [
            api_call_timestamp
            for api_call_timestamp in self._api_call_timestamps
            if api_call_timestamp > window_start_timestamp
        ]
        if len(self._api_call_timestamps) >= OPEN_METEO_MAX_API_CALLS_PER_HOUR:
            _LOGGER.warning(
                "Open-Meteo rate limit reached (%d/h)",
                OPEN_METEO_MAX_API_CALLS_PER_HOUR,
            )
            return False
        return True

    def _record_api_call(self) -> None:
        """Record an API call timestamp. @zara"""
        self._api_call_timestamps.append(time.time())

    def get_weather_for_hour(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get weather data for specific hour from cache. @zara"""
        cached_forecast = self._cache.get("hourly_forecast", [])
        for weather_hour in cached_forecast:
            if not weather_hour.get("date") == date:
                continue
            if not weather_hour.get("hour") == hour:
                continue
            return weather_hour

    def get_radiation_for_hour(
        self, date: str, hour: int
    ) -> tuple[float, float, float]:
        "Get radiation values for specific hour. @zara\n\nReturns:\n    Tuple of (direct, diffuse, ghi) radiation values\n"
        weather_hour = self.get_weather_for_hour(date, hour)
        if weather_hour:
            direct_radiation_wm2 = weather_hour.get("direct_radiation") or 0
            diffuse_radiation_wm2 = weather_hour.get("diffuse_radiation") or 0
            ghi_wm2 = (
                weather_hour.get("ghi") or direct_radiation_wm2 + diffuse_radiation_wm2
            )
            return (direct_radiation_wm2, diffuse_radiation_wm2, ghi_wm2)
        return (0.0, 0.0, 0.0)

    def get_forecast_for_date(self, date: str) -> list[dict[str, Any]]:
        """Get all forecasts for a specific date. @zara"""
        cached_forecast = self._cache.get("hourly_forecast", [])
        return [
            weather_hour
            for weather_hour in cached_forecast
            if weather_hour.get("date") == date
        ]

    def get_daily_data(self) -> dict[str, Any]:
        """Get daily summary data. @zara"""
        return self._cache.get("daily", {})


class OpenMeteoArchiveClient(DataManagerIO):
    """Client for fetching historical weather data from Open-Meteo Archive API. @zara

    Used for:
    - Bootstrap physics from history
    - Historical training data collection
    - Retroactive weather data
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
    ):
        """Initialize archive client. @zara"""
        super().__init__(hass, db_manager)
        self.latitude = latitude
        self.longitude = longitude
        self._last_error = None
        _LOGGER.info(
            "OpenMeteoArchiveClient initialized (lat=%.4f, lon=%.4f)",
            latitude,
            longitude,
        )

    async def get_historical_weather(
        self, start_date: str, end_date: str
    ) -> Optional[list[dict[str, Any]]]:
        "Fetch historical hourly weather data. @zara\n\nArgs:\n    start_date: Start date in YYYY-MM-DD format\n    end_date: End date in YYYY-MM-DD format\n\nReturns:\n    List of hourly weather data or None\n"
        try:
            request_parameters = {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "start_date": start_date,
                "end_date": end_date,
                "hourly": ",".join(
                    [
                        "temperature_2m",
                        "relative_humidity_2m",
                        "cloud_cover",
                        "precipitation",
                        "wind_speed_10m",
                        "pressure_msl",
                        "direct_radiation",
                        "diffuse_radiation",
                        "shortwave_radiation",
                        "weather_code",
                        "snowfall",
                        "rain",
                    ]
                ),
                "timezone": "auto",
            }
            _LOGGER.info(
                "Fetching historical weather from %s to %s...", start_date, end_date
            )
            session = async_get_clientsession(self.hass)
            async with session.get(
                OPEN_METEO_ARCHIVE_URL,
                params=request_parameters,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    self._last_error = f"HTTP {response.status}"
                    _LOGGER.warning("Archive API returned status %d", response.status)
                    return
                response_data = await response.json()
            historical_weather = self._parse_archive_response(response_data)
            if historical_weather:
                _LOGGER.info(
                    "Fetched %d hours of historical data (%s to %s)",
                    len(historical_weather),
                    start_date,
                    end_date,
                )
            return historical_weather
        except asyncio.TimeoutError:
            self._last_error = "Timeout"
            _LOGGER.warning("Archive API request timed out")
            return
        except aiohttp.ClientError as error:
            self._last_error = f"Connection: {error}"
            _LOGGER.warning("Archive API connection error: %s", error)
            return
        except Exception as error:
            self._last_error = f"Error: {error}"
            _LOGGER.error("Archive API error: %s", error, exc_info=True)
            return

    def _parse_archive_response(
        self, data: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        """Parse hourly response from archive API. @zara"""
        try:
            hourly_response = data.get("hourly", {})
            timestamps = hourly_response.get("time", [])
            if not timestamps:
                _LOGGER.warning("Archive response has no time data")
                return
            parsed_hours = []
            for hour_index, timestamp in enumerate(timestamps):
                hour_datetime = datetime.fromisoformat(timestamp)
                direct_radiation_wm2 = (
                    self._safe_get(hourly_response, "direct_radiation", hour_index) or 0
                )
                diffuse_radiation_wm2 = (
                    self._safe_get(hourly_response, "diffuse_radiation", hour_index)
                    or 0
                )
                shortwave_radiation_wm2 = self._safe_get(
                    hourly_response, "shortwave_radiation", hour_index
                )
                ghi_wm2 = (
                    shortwave_radiation_wm2
                    if shortwave_radiation_wm2 is not None
                    else direct_radiation_wm2 + diffuse_radiation_wm2
                )
                weather_hour = {
                    "datetime": hour_datetime,
                    "date": hour_datetime.date().isoformat(),
                    "hour": hour_datetime.hour,
                    "temperature": self._safe_get(
                        hourly_response, "temperature_2m", hour_index
                    ),
                    "humidity": self._safe_get(
                        hourly_response, "relative_humidity_2m", hour_index
                    ),
                    "cloud_cover": self._safe_get(
                        hourly_response, "cloud_cover", hour_index
                    ),
                    "precipitation": self._safe_get(
                        hourly_response, "precipitation", hour_index
                    ),
                    "wind_speed": self._safe_get(
                        hourly_response, "wind_speed_10m", hour_index
                    ),
                    "pressure": self._safe_get(
                        hourly_response, "pressure_msl", hour_index
                    ),
                    "direct_radiation": direct_radiation_wm2,
                    "diffuse_radiation": diffuse_radiation_wm2,
                    "ghi": ghi_wm2,
                    "weather_code": self._safe_get(
                        hourly_response, "weather_code", hour_index
                    ),
                    "snowfall": self._safe_get(hourly_response, "snowfall", hour_index),
                    "rain": self._safe_get(hourly_response, "rain", hour_index),
                    "source": "open-meteo-archive",
                }
                parsed_hours.append(weather_hour)
                continue
            return parsed_hours
        except Exception as error:
            _LOGGER.error("Error parsing Archive response: %s", error, exc_info=True)
            return

    @staticmethod
    def _safe_get(data: dict, key: str, index: int) -> Optional[float]:
        """Safely get a value from array in dict. @zara"""
        try:
            values = data.get(key, [])
            if index < len(values):
                return values[index]
        except (IndexError, TypeError):
            pass
        return None if True else None

    def get_last_error(self) -> Optional[str]:
        """Get last error message. @zara"""
        return self._last_error
