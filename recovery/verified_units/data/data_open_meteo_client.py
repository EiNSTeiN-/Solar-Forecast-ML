# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Open Meteo Client V16.2.0 - Weather Data from Open-Meteo API

Fetches weather forecasts from Open-Meteo API.
All data is stored in the database via DatabaseManager.

Features:
- ECMWF IFS025 as the durable 72-hour backbone
- ICON-D2 / ICON-EU radiation series when the API returns them
- Hourly cloud cover, temperature, radiation data
- Visibility from unsuffixed / ICON-D2 / ICON-EU / IFS chain
- Rate limiting (5 calls/hour)
- Memory and database caching

@zara
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import aiohttp
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    SNAPSHOT_KIND_PROVIDER_FETCH,
    align_provider_radiation_to_interval_start,
    forecast_lead_hours,
    open_meteo_base_source_identity,
    provider_fetch_source_rows_open_meteo,
    snapshot_target_datetime,
)
from .weather_types import resolve_open_meteo_visibility

_LOGGER = logging.getLogger(__name__)
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ECMWF_URL = "https://api.open-meteo.com/v1/ecmwf"
OPEN_METEO_DWD_ICON_URL = "https://api.open-meteo.com/v1/dwd-icon"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_TIMEOUT = 15
OPEN_METEO_MODELS_PRIMARY = "ecmwf_ifs025,icon_d2,icon_eu"
OPEN_METEO_FORECAST_DAYS = 4
OPEN_METEO_REQUIRED_FORECAST_DAYS = 3
OPEN_METEO_REQUIRED_HOURS_PER_DAY = 24
OPEN_METEO_CACHE_DURATION = 3600
OPEN_METEO_STALE_CACHE_MAX_AGE = 86400
OPEN_METEO_MAX_API_CALLS_PER_HOUR = 5
OPEN_METEO_RATE_LIMIT_WINDOW = 3600
OPEN_METEO_PRIMARY_ATTEMPTS = 3
OPEN_METEO_RETRY_BASE_DELAY = 5.0
OPEN_METEO_RETRY_MAX_DELAY = 30.0
OPEN_METEO_RETRYABLE_STATUSES = frozenset({408, 500, 502, 503, 504})
_OPEN_METEO_PROCESS_CALL_TIMESTAMPS: list[float] = []
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


def forecast_horizon_hour_counts(
    hourly_data: list[dict[str, Any]],
    start_date,
    days: int = OPEN_METEO_REQUIRED_FORECAST_DAYS,
) -> dict[str, int]:
    """Count unique forecast hours for each required local calendar day."""
    required_dates = {
        (start_date + timedelta(days=day_offset)).isoformat()
        for day_offset in range(days)
    }
    unique_hours_by_date = {date_key: set() for date_key in required_dates}
    for forecast_row in hourly_data or []:
        date_key = str(forecast_row.get("date") or "")
        raw_hour = forecast_row.get("hour")
        if date_key not in unique_hours_by_date:
            continue
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            unique_hours_by_date[date_key].add(hour)
    return {
        date_key: len(unique_hours_by_date[date_key])
        for date_key in sorted(unique_hours_by_date)
    }


def forecast_horizon_complete(
    hourly_data: list[dict[str, Any]],
    start_date,
    days: int = OPEN_METEO_REQUIRED_FORECAST_DAYS,
    minimum_hours: int = OPEN_METEO_REQUIRED_HOURS_PER_DAY,
    timezone_name: Optional[str] = None,
) -> bool:
    """Return whether every required local day has sufficient hourly coverage."""
    coverage_by_date = forecast_horizon_hour_counts(hourly_data, start_date, days)
    if len(coverage_by_date) != days:
        return False
    for day_offset in range(days):
        target_date = start_date + timedelta(days=day_offset)
        required_hours = required_forecast_hours_for_date(
            target_date, timezone_name, minimum_hours
        )
        if not coverage_by_date.get(target_date.isoformat(), 0) < required_hours:
            continue
        return False
    return True


def required_forecast_hours_for_date(
    target_date,
    timezone_name: Optional[str],
    nominal_hours: int = OPEN_METEO_REQUIRED_HOURS_PER_DAY,
) -> int:
    """Return the representable local hours for a date, including DST gaps."""
    if not timezone_name:
        return nominal_hours
    try:
        local_zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        return nominal_hours
    local_start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_zone)
    local_end = datetime.combine(
        target_date + timedelta(days=1), datetime.min.time(), tzinfo=local_zone
    )
    utc_hours = int(
        (
            local_end.astimezone(timezone.utc) - local_start.astimezone(timezone.utc)
        ).total_seconds()
        // 3600
    )
    return min(nominal_hours, max(1, utc_hours))


def merge_hourly_forecasts(
    preferred: list[dict[str, Any]], supplemental: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge forecast rows without replacing valid preferred fields with gaps."""
    merged_by_hour = {
        (str(forecast_row.get("date")), int(forecast_row.get("hour"))): dict(
            forecast_row
        )
        for forecast_row in supplemental or []
        if forecast_row.get("date") is not None
        if forecast_row.get("hour") is not None
    }
    for forecast_row in preferred or []:
        if forecast_row.get("date") is None or forecast_row.get("hour") is None:
            continue
        hour_key = (str(forecast_row.get("date")), int(forecast_row.get("hour")))
        merged_row = merged_by_hour.setdefault(hour_key, {})
        supplemental_source = merged_row.get("source")
        for field, value in forecast_row.items():
            if value is not None:
                merged_row[field] = value
        preferred_source = forecast_row.get("source")
        if (
            preferred_source
            and supplemental_source
            and (preferred_source != supplemental_source)
        ):
            merged_row["source"] = f"{preferred_source}+{supplemental_source}"
    return [merged_by_hour[hour_key] for hour_key in sorted(merged_by_hour)]


class OpenMeteoClient(DataManagerIO):
    """Client for fetching weather data from Open-Meteo API. @zara

    Uses ECMWF IFS025 plus ICON radiation series when available.
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
        self._api_call_timestamps = _OPEN_METEO_PROCESS_CALL_TIMESTAMPS
        self._last_api_error = None
        self._last_api_status = None
        self._last_retry_after_seconds = None
        self._consecutive_failures = 0
        self._last_forecast_status = "idle"
        self._last_forecast_source = "none"
        self._last_forecast_reason = None
        _LOGGER.debug(
            "OpenMeteoClient initialized (lat=%.4f, lon=%.4f)", latitude, longitude
        )

    async def async_init(self) -> bool:
        """Initialize and load cached data from database. @zara"""
        await self.ensure_initialized()
        return await self._load_from_database()

    async def _load_from_database(self) -> bool:
        """Load forecast data from database into memory cache. @zara"""
        try:
            today_iso = dt_util.now().date().isoformat()
            try:
                database_rows = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              fetched_at, source, ghi_icon_d2, direct_radiation_icon_d2,\n                              diffuse_radiation_icon_d2, ghi_icon_eu, direct_radiation_icon_eu,\n                              diffuse_radiation_icon_eu, ghi_source_identity, visibility_source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (today_iso,),
                )
            except Exception:
                database_rows = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              fetched_at, source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (today_iso,),
                )
            if not database_rows:
                self._set_forecast_status("failed", "database_cache", "database_empty")
                return False
            hourly_forecast = []
            latest_fetched_at = None
            for database_row in database_rows:
                forecast_row = {
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
                    "ghi_icon_d2": database_row[20] if len(database_row) > 20 else None,
                    "direct_radiation_icon_d2": (
                        database_row[21] if len(database_row) > 21 else None
                    ),
                    "diffuse_radiation_icon_d2": (
                        database_row[22] if len(database_row) > 22 else None
                    ),
                    "ghi_icon_eu": database_row[23] if len(database_row) > 23 else None,
                    "direct_radiation_icon_eu": (
                        database_row[24] if len(database_row) > 24 else None
                    ),
                    "diffuse_radiation_icon_eu": (
                        database_row[25] if len(database_row) > 25 else None
                    ),
                    "source": (
                        database_row[19]
                        if len(database_row) > 19 and database_row[19]
                        else "database"
                    ),
                    "ghi_source_identity": (
                        database_row[26]
                        if len(database_row) > 26 and database_row[26]
                        else open_meteo_base_source_identity(
                            database_row[19] if len(database_row) > 19 else None
                        )
                    ),
                    "visibility_source": (
                        database_row[27] if len(database_row) > 27 else None
                    ),
                }
                hourly_forecast.append(forecast_row)
                fetched_at = self._parse_cache_timestamp(
                    database_row[18] if len(database_row) > 18 else None
                )
                if fetched_at:
                    if latest_fetched_at is None or fetched_at > latest_fetched_at:
                        latest_fetched_at = fetched_at
            hourly_forecast = align_provider_radiation_to_interval_start(
                hourly_forecast
            )
            self._cache["hourly_forecast"] = hourly_forecast
            self._cache_time = latest_fetched_at or dt_util.now()
            if self._has_required_horizon(hourly_forecast):
                self._set_forecast_status(
                    "ok", "database_cache", "loaded_from_database"
                )
            else:
                self._set_forecast_status(
                    "degraded", "database_cache", "database_cache_incomplete_horizon"
                )
            _LOGGER.debug(
                "Loaded %d hours from database into Open-Meteo cache",
                len(hourly_forecast),
            )
            return True
        except Exception as error:
            _LOGGER.warning("Error loading Open-Meteo data from database: %s", error)
            return False

    async def _save_to_database(self, hourly_data: list[dict[str, Any]]) -> bool:
        """Save forecast data to database. @zara"""
        try:
            retrieved_at = dt_util.now()
            retrieved_at_iso = retrieved_at.isoformat()
            for forecast_row in hourly_data:
                date_key = forecast_row.get("date")
                raw_hour = forecast_row.get("hour")
                if date_key is None or raw_hour is None:
                    continue
                direct_radiation = forecast_row.get("direct_radiation")
                diffuse_radiation = forecast_row.get("diffuse_radiation")
                ghi = forecast_row.get("ghi")
                if ghi is None and (
                    direct_radiation is not None or diffuse_radiation is not None
                ):
                    ghi = (direct_radiation or 0) + (diffuse_radiation or 0)
                if ghi is not None:
                    ghi = max(0, min(1600, ghi))
                snapshot_row = dict(forecast_row)
                snapshot_row["ghi"] = ghi
                target_hour = int(raw_hour)
                async with self.db.transaction():
                    await self.db.insert_weather_radiation_snapshot(
                        snapshot_kind=SNAPSHOT_KIND_PROVIDER_FETCH,
                        retrieved_at=retrieved_at_iso,
                        provider_run_at=None,
                        target_datetime=snapshot_target_datetime(
                            str(date_key), target_hour, retrieved_at
                        ),
                        target_date=str(date_key),
                        target_hour=target_hour,
                        lead_hours=forecast_lead_hours(
                            str(date_key), target_hour, retrieved_at
                        ),
                        sources=provider_fetch_source_rows_open_meteo(snapshot_row),
                    )
                    await self.db.execute(
                        "INSERT INTO weather_cache_open_meteo\n                       (forecast_date, hour, temperature, humidity, cloud_cover, precipitation,\n                        wind_speed, pressure, direct_radiation, diffuse_radiation,\n                        ghi, cloud_cover_low, cloud_cover_mid,\n                        cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                        ghi_icon_d2, direct_radiation_icon_d2, diffuse_radiation_icon_d2,\n                        ghi_icon_eu, direct_radiation_icon_eu, diffuse_radiation_icon_eu,\n                        source, ghi_source_identity, visibility_source, fetched_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                       ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                           temperature = COALESCE(excluded.temperature, temperature),\n                           humidity = COALESCE(excluded.humidity, humidity),\n                           cloud_cover = COALESCE(excluded.cloud_cover, cloud_cover),\n                           precipitation = COALESCE(excluded.precipitation, precipitation),\n                           wind_speed = COALESCE(excluded.wind_speed, wind_speed),\n                           pressure = COALESCE(excluded.pressure, pressure),\n                           direct_radiation = COALESCE(excluded.direct_radiation, direct_radiation),\n                           diffuse_radiation = COALESCE(excluded.diffuse_radiation, diffuse_radiation),\n                           ghi = COALESCE(excluded.ghi, ghi),\n                           cloud_cover_low = COALESCE(excluded.cloud_cover_low, cloud_cover_low),\n                           cloud_cover_mid = COALESCE(excluded.cloud_cover_mid, cloud_cover_mid),\n                           cloud_cover_high = COALESCE(excluded.cloud_cover_high, cloud_cover_high),\n                           visibility_m = COALESCE(excluded.visibility_m, visibility_m),\n                           weather_code = COALESCE(excluded.weather_code, weather_code),\n                           snowfall = COALESCE(excluded.snowfall, snowfall),\n                           rain = COALESCE(excluded.rain, rain),\n                           ghi_icon_d2 = COALESCE(excluded.ghi_icon_d2, ghi_icon_d2),\n                           direct_radiation_icon_d2 = COALESCE(excluded.direct_radiation_icon_d2, direct_radiation_icon_d2),\n                           diffuse_radiation_icon_d2 = COALESCE(excluded.diffuse_radiation_icon_d2, diffuse_radiation_icon_d2),\n                           ghi_icon_eu = COALESCE(excluded.ghi_icon_eu, ghi_icon_eu),\n                           direct_radiation_icon_eu = COALESCE(excluded.direct_radiation_icon_eu, direct_radiation_icon_eu),\n                           diffuse_radiation_icon_eu = COALESCE(excluded.diffuse_radiation_icon_eu, diffuse_radiation_icon_eu),\n                           source = excluded.source,\n                           ghi_source_identity = COALESCE(excluded.ghi_source_identity, ghi_source_identity),\n                           visibility_source = COALESCE(excluded.visibility_source, visibility_source),\n                           fetched_at = CURRENT_TIMESTAMP",
                        (
                            date_key,
                            raw_hour,
                            forecast_row.get("temperature"),
                            forecast_row.get("humidity"),
                            forecast_row.get("cloud_cover"),
                            forecast_row.get("precipitation") or 0,
                            forecast_row.get("wind_speed"),
                            forecast_row.get("pressure"),
                            direct_radiation,
                            diffuse_radiation,
                            ghi,
                            forecast_row.get("cloud_cover_low"),
                            forecast_row.get("cloud_cover_mid"),
                            forecast_row.get("cloud_cover_high"),
                            forecast_row.get("visibility_m"),
                            forecast_row.get("weather_code"),
                            forecast_row.get("snowfall"),
                            forecast_row.get("rain"),
                            forecast_row.get("ghi_icon_d2"),
                            forecast_row.get("direct_radiation_icon_d2"),
                            forecast_row.get("diffuse_radiation_icon_d2"),
                            forecast_row.get("ghi_icon_eu"),
                            forecast_row.get("direct_radiation_icon_eu"),
                            forecast_row.get("diffuse_radiation_icon_eu"),
                            forecast_row.get("source") or "open-meteo",
                            (
                                forecast_row.get("ghi_source_identity")
                                or open_meteo_base_source_identity(
                                    forecast_row.get("source")
                                )
                                if ghi is not None
                                else None
                            ),
                            forecast_row.get("visibility_source"),
                        ),
                        auto_commit=False,
                    )
            _LOGGER.debug(
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
                self._set_forecast_status("ok", "memory_cache", "fresh_memory_cache")
                return self._cache.get("hourly_forecast")
            database_fresh = await self._check_db_freshness()
            if database_fresh:
                await self._load_from_database()
                if self._cache.get("hourly_forecast"):
                    _LOGGER.debug("Open-Meteo: Using existing DB data (still fresh)")
                    self._set_forecast_status(
                        "ok", "database_cache", "fresh_database_cache"
                    )
                    return self._cache.get("hourly_forecast")
        if self._check_rate_limit_budget():
            api_forecast = await self._fetch_from_api(hours)
            if api_forecast and self._has_required_horizon(api_forecast):
                self._consecutive_failures = 0
                self._last_api_error = None
                self._set_forecast_status("ok", "api", "fresh_api")
                return api_forecast
            if api_forecast:
                self._consecutive_failures += 1
                self._set_forecast_status(
                    "degraded", "api", "fresh_api_incomplete_horizon"
                )
                return api_forecast
            self._consecutive_failures += 1
        if self._is_cache_usable_as_fallback():
            _LOGGER.warning(
                "Open-Meteo API unavailable, using stale cache (age: %.1fh)",
                self._get_cache_age_hours(),
            )
            self._set_forecast_status(
                "degraded", "stale_cache", "api_unavailable_stale_cache"
            )
            return self._cache.get("hourly_forecast")
        if self._is_cache_within_stale_age():
            coverage_by_date = forecast_horizon_hour_counts(
                self._cache.get("hourly_forecast", []), dt_util.now().date()
            )
            _LOGGER.warning(
                "Open-Meteo API unavailable, using partial stale cache (coverage=%s)",
                coverage_by_date,
            )
            self._set_forecast_status(
                "degraded", "stale_cache", "api_unavailable_incomplete_horizon"
            )
            return self._cache.get("hourly_forecast")
        await self._load_from_database()
        cached_forecast = self._cache.get("hourly_forecast", [])
        if cached_forecast:
            fallback_reason = (
                "api_unavailable_database_fallback"
                if self._has_required_horizon(cached_forecast)
                else "api_unavailable_database_incomplete_horizon"
            )
            self._set_forecast_status("degraded", "database_cache", fallback_reason)
            return cached_forecast
        _LOGGER.warning("Open-Meteo: No cache available and API failed")
        self._set_forecast_status("failed", "none", "api_failed_no_cache")

    async def _check_db_freshness(self) -> bool:
        """Check if the database has a fresh, complete three-day horizon."""
        try:
            today = dt_util.now().date()
            last_required_date = (
                today + timedelta(days=OPEN_METEO_REQUIRED_FORECAST_DAYS - 1)
            ).isoformat()
            database_rows = await self.fetch_all(
                "SELECT forecast_date, COUNT(DISTINCT hour), MAX(fetched_at)\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date BETWEEN ? AND ?\n                   GROUP BY forecast_date",
                (today.isoformat(), last_required_date),
            )
            coverage_by_date = {
                str(database_row[0]): int(database_row[1] or 0)
                for database_row in database_rows or []
            }
            for day_offset in range(OPEN_METEO_REQUIRED_FORECAST_DAYS):
                target_date = today + timedelta(days=day_offset)
                required_hours = required_forecast_hours_for_date(
                    target_date, self.hass.config.time_zone
                )
                if coverage_by_date.get(target_date.isoformat(), 0) < required_hours:
                    return False
            parsed_timestamps = [
                self._parse_cache_timestamp(database_row[2])
                for database_row in database_rows
                if database_row[2]
            ]
            oldest_fetched_at = min(
                (fetched_at for fetched_at in parsed_timestamps if fetched_at),
                default=None,
            )
            if not oldest_fetched_at:
                return False
            age_hours = (dt_util.now() - oldest_fetched_at).total_seconds() / 3600
            return age_hours < 6.0
        except Exception:
            return False

    async def get_raw_forecast_for_blending(
        self, hours: int = 72
    ) -> Optional[list[dict[str, Any]]]:
        "Get raw Open-Meteo data for blending - bypasses in-memory cache. @zara\n\nArgs:\n    hours: Number of hours to forecast\n\nReturns:\n    Raw forecast data for blending\n"
        if self._check_rate_limit_budget():
            api_forecast = await self._fetch_from_api(hours)
            if api_forecast and self._has_required_horizon(api_forecast):
                self._consecutive_failures = 0
                self._set_forecast_status("ok", "api", "fresh_api_raw_blending")
                return api_forecast
            if api_forecast:
                self._consecutive_failures += 1
                self._set_forecast_status(
                    "degraded", "api", "fresh_api_raw_blending_incomplete_horizon"
                )
                return api_forecast
            self._consecutive_failures += 1
        await self._load_from_database()
        cached_forecast = self._cache.get("hourly_forecast")
        if cached_forecast:
            self._set_forecast_status(
                "degraded", "database_cache", "raw_blending_database_fallback"
            )
        return cached_forecast

    async def _fetch_from_api(self, hours: int = 72) -> Optional[list[dict[str, Any]]]:
        """Fetch a complete horizon through bounded endpoint failover."""
        del hours
        best_forecast = []
        for attempt in range(OPEN_METEO_PRIMARY_ATTEMPTS):
            forecast_rows = await self._fetch_models_from_api(
                OPEN_METEO_MODELS_PRIMARY,
                endpoint=OPEN_METEO_BASE_URL,
                source="open-meteo:multi-model",
            )
            if forecast_rows:
                best_forecast = forecast_rows
                if self._has_required_horizon(best_forecast):
                    return await self._accept_api_result(best_forecast)
                _LOGGER.warning(
                    "Open-Meteo multi-model response has incomplete horizon: %s",
                    forecast_horizon_hour_counts(best_forecast, dt_util.now().date()),
                )
                break
            if not self._last_failure_is_retryable():
                break
            if attempt + 1 >= OPEN_METEO_PRIMARY_ATTEMPTS:
                break
            retry_delay = self._retry_delay_seconds(attempt)
            _LOGGER.info(
                "Open-Meteo multi-model retry %d/%d in %.1fs",
                attempt + 2,
                OPEN_METEO_PRIMARY_ATTEMPTS,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            continue
        if self._last_api_status == 429:
            return (
                await self._accept_api_result(best_forecast) if best_forecast else None
            )
        fallback_endpoints = (
            (OPEN_METEO_ECMWF_URL, "open-meteo:ecmwf"),
            (OPEN_METEO_DWD_ICON_URL, "open-meteo:dwd-icon"),
        )
        for endpoint, source in fallback_endpoints:
            forecast_rows = await self._fetch_models_from_api(
                None, endpoint=endpoint, source=source
            )
            if not forecast_rows:
                if self._last_api_status == 429:
                    break
                continue
            best_forecast = (
                merge_hourly_forecasts(best_forecast, forecast_rows)
                if best_forecast
                else forecast_rows
            )
            if self._has_required_horizon(best_forecast):
                _LOGGER.info("Open-Meteo horizon recovered through %s", source)
                return await self._accept_api_result(best_forecast)
        if best_forecast:
            _LOGGER.warning(
                "Open-Meteo endpoint cascade produced partial horizon only: %s",
                forecast_horizon_hour_counts(best_forecast, dt_util.now().date()),
            )
            return await self._accept_api_result(best_forecast)

    async def _accept_api_result(
        self, hourly_data: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Persist one validated cascade result and update the memory cache.

        The database keeps the provider's delivered hour labels; the memory
        cache is what consumers read, so it carries interval-start radiation.
        """
        await self._save_to_database(hourly_data)
        aligned_forecast = align_provider_radiation_to_interval_start(hourly_data)
        self._cache["hourly_forecast"] = aligned_forecast
        self._cache_time = dt_util.now()
        ghi_values = [
            float(ghi_value)
            for forecast_row in aligned_forecast
            if (ghi_value := forecast_row.get("ghi")) is not None
        ]
        _LOGGER.debug(
            "Accepted %d Open-Meteo hours (GHI: %.0f - %.0f W/m2, coverage=%s)",
            len(aligned_forecast),
            min(ghi_values, default=0.0),
            max(ghi_values, default=0.0),
            forecast_horizon_hour_counts(aligned_forecast, dt_util.now().date()),
        )
        return aligned_forecast

    def _forecast_params(self, models: Optional[str]) -> dict[str, Any]:
        params = {
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
            "forecast_days": OPEN_METEO_FORECAST_DAYS,
        }
        if models:
            params["models"] = models
        return params

    async def _fetch_models_from_api(
        self, models: Optional[str], *, endpoint: str, source: str
    ) -> Optional[list[dict[str, Any]]]:
        """Fetch and parse one endpoint without mutating the persistent cache."""
        if not self._check_rate_limit_budget():
            self._last_api_status = 429
            self._last_api_error = "Local rate-limit budget exhausted"
            return
        try:
            params = self._forecast_params(models)
            self._record_api_call()
            self._last_api_status = None
            self._last_retry_after_seconds = None
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=OPEN_METEO_TIMEOUT),
                    allow_redirects=False,
                ) as response:
                    self._last_api_status = response.status
                    if response.status != 200:
                        self._last_api_error = f"HTTP {response.status}"
                        retry_after_header = response.headers.get("Retry-After")
                        if retry_after_header:
                            try:
                                self._last_retry_after_seconds = max(
                                    0.0, float(retry_after_header)
                                )
                            except (TypeError, ValueError):
                                self._last_retry_after_seconds = None
                        log_status = (
                            _LOGGER.info
                            if response.status in OPEN_METEO_RETRYABLE_STATUSES
                            else _LOGGER.warning
                        )
                        log_status(
                            "Open-Meteo API returned status %d (source=%s models=%s)",
                            response.status,
                            source,
                            models,
                        )
                        return
                    payload = await response.json()
            hourly_forecast = self._parse_hourly_response(payload, source=source)
            if hourly_forecast:
                self._cache["daily"] = payload.get("daily", {})
                ghi_values = [
                    forecast_row.get("ghi", 0) or 0 for forecast_row in hourly_forecast
                ]
                icon_d2_hours = sum(
                    (
                        1
                        for forecast_row in hourly_forecast
                        if forecast_row.get("ghi_icon_d2") is not None
                    )
                )
                _LOGGER.info(
                    "Open-Meteo refresh: %d h (source=%s models=%s, ICON-D2 %d h, GHI %.0f–%.0f W/m²)",
                    len(hourly_forecast),
                    source,
                    models,
                    icon_d2_hours,
                    min(ghi_values),
                    max(ghi_values),
                )
            return hourly_forecast
        except asyncio.TimeoutError:
            self._last_api_status = None
            self._last_api_error = "Timeout"
            _LOGGER.warning("Open-Meteo API request timed out (source=%s)", source)
            return
        except aiohttp.ClientError as error:
            self._last_api_status = None
            self._last_api_error = f"Connection: {error}"
            _LOGGER.warning("Open-Meteo API connection error (%s): %s", source, error)
            return
        except Exception as error:
            self._last_api_status = None
            self._last_api_error = f"Error: {error}"
            _LOGGER.error("Open-Meteo API error (%s): %s", source, error, exc_info=True)
            return

    def _resolve_visibility(
        self, hourly: dict[str, Any], index: int
    ) -> tuple[Optional[float], Optional[str]]:
        """Resolve visibility from unsuffixed, ICON-D2, ICON-EU, then IFS."""
        return resolve_open_meteo_visibility(
            unsuffixed=self._safe_get(hourly, "visibility", index),
            icon_d2=self._safe_get(hourly, "visibility_icon_d2", index),
            icon_eu=self._safe_get(hourly, "visibility_icon_eu", index),
            ecmwf_ifs025=self._safe_get(hourly, "visibility_ecmwf_ifs025", index),
        )

    def _parse_hourly_response(
        self, data: dict[str, Any], *, source: str = "open-meteo:multi-model"
    ) -> Optional[list[dict[str, Any]]]:
        """Parse hourly data from API response. @zara"""
        try:
            hourly = data.get("hourly", {})
            timestamps = hourly.get("time", [])
            if not timestamps:
                _LOGGER.warning("Open-Meteo response has no time data")
                return
            forecast_rows = []
            for index, timestamp_text in enumerate(timestamps):
                target_datetime = datetime.fromisoformat(timestamp_text)
                direct_radiation = self._model_value(
                    hourly, "direct_radiation", index, "ecmwf_ifs025"
                )
                diffuse_radiation = self._model_value(
                    hourly, "diffuse_radiation", index, "ecmwf_ifs025"
                )
                shortwave_radiation = self._model_value(
                    hourly, "shortwave_radiation", index, "ecmwf_ifs025"
                )
                if shortwave_radiation is not None:
                    ghi = max(0, min(1600, shortwave_radiation))
                elif direct_radiation is not None or diffuse_radiation is not None:
                    ghi = max(
                        0, min(1600, (direct_radiation or 0) + (diffuse_radiation or 0))
                    )
                else:
                    ghi = None
                icon_d2_radiation = self._model_radiation(hourly, index, "icon_d2")
                icon_eu_radiation = self._model_radiation(hourly, index, "icon_eu")
                cloud_cover_low = self._safe_get(hourly, "cloud_cover_low", index)
                if cloud_cover_low is None:
                    cloud_cover_low = self._safe_get(
                        hourly, "cloud_cover_low_ecmwf_ifs025", index
                    )
                cloud_cover_mid = self._safe_get(hourly, "cloud_cover_mid", index)
                if cloud_cover_mid is None:
                    cloud_cover_mid = self._safe_get(
                        hourly, "cloud_cover_mid_ecmwf_ifs025", index
                    )
                cloud_cover_high = self._safe_get(hourly, "cloud_cover_high", index)
                if cloud_cover_high is None:
                    cloud_cover_high = self._safe_get(
                        hourly, "cloud_cover_high_ecmwf_ifs025", index
                    )
                visibility_m, visibility_source = self._resolve_visibility(
                    hourly, index
                )
                hourly_row = {
                    "datetime": target_datetime,
                    "date": target_datetime.date().isoformat(),
                    "hour": target_datetime.hour,
                    "temperature": self._model_value(
                        hourly, "temperature_2m", index, "ecmwf_ifs025"
                    ),
                    "humidity": self._model_value(
                        hourly, "relative_humidity_2m", index, "ecmwf_ifs025"
                    ),
                    "cloud_cover": self._model_value(
                        hourly, "cloud_cover", index, "ecmwf_ifs025"
                    ),
                    "cloud_cover_low": cloud_cover_low,
                    "cloud_cover_mid": cloud_cover_mid,
                    "cloud_cover_high": cloud_cover_high,
                    "precipitation": self._model_value(
                        hourly, "precipitation", index, "ecmwf_ifs025"
                    ),
                    "wind_speed": self._model_value(
                        hourly, "wind_speed_10m", index, "ecmwf_ifs025"
                    ),
                    "pressure": self._model_value(
                        hourly, "pressure_msl", index, "ecmwf_ifs025"
                    ),
                    "direct_radiation": direct_radiation,
                    "diffuse_radiation": diffuse_radiation,
                    "ghi": ghi,
                    "ghi_source_identity": (
                        open_meteo_base_source_identity(source)
                        if ghi is not None
                        else None
                    ),
                    "ghi_icon_d2": icon_d2_radiation[0],
                    "direct_radiation_icon_d2": icon_d2_radiation[1],
                    "diffuse_radiation_icon_d2": icon_d2_radiation[2],
                    "ghi_icon_eu": icon_eu_radiation[0],
                    "direct_radiation_icon_eu": icon_eu_radiation[1],
                    "diffuse_radiation_icon_eu": icon_eu_radiation[2],
                    "global_tilted_irradiance": self._model_value(
                        hourly, "global_tilted_irradiance", index, "ecmwf_ifs025"
                    ),
                    "visibility_m": visibility_m,
                    "visibility_source": visibility_source,
                    "weather_code": self._model_value(
                        hourly, "weather_code", index, "ecmwf_ifs025"
                    ),
                    "snowfall": self._model_value(
                        hourly, "snowfall", index, "ecmwf_ifs025"
                    ),
                    "rain": self._model_value(hourly, "rain", index, "ecmwf_ifs025"),
                    "source": source,
                }
                forecast_rows.append(hourly_row)
                continue
            return forecast_rows
        except Exception as error:
            _LOGGER.error("Error parsing Open-Meteo response: %s", error, exc_info=True)
            return

    def _model_value(
        self, data: dict, key: str, index: int, model: str
    ) -> Optional[float]:
        """Read a variable, preferring the unsuffixed series then the model suffix."""
        value = self._safe_get(data, key, index)
        if value is not None:
            return value
        return self._safe_get(data, f"{key}_{model}", index)

    def _model_radiation(
        self, data: dict, index: int, model: str
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Return (ghi, dni, dhi) for one named Open-Meteo model."""
        shortwave_radiation = self._safe_get(
            data, f"shortwave_radiation_{model}", index
        )
        direct_radiation = self._safe_get(data, f"direct_radiation_{model}", index)
        diffuse_radiation = self._safe_get(data, f"diffuse_radiation_{model}", index)
        ghi = shortwave_radiation
        if ghi is None and (
            direct_radiation is not None or diffuse_radiation is not None
        ):
            ghi = (direct_radiation or 0) + (diffuse_radiation or 0)
        if ghi is not None:
            ghi = max(0, min(1600, ghi))
        return (ghi, direct_radiation, diffuse_radiation)

    @staticmethod
    def _safe_get(data: dict, key: str, index: int) -> Optional[float]:
        """Safely get a value from array in dict. @zara"""
        try:
            series = data.get(key, [])
            if index < len(series):
                return series[index]
        except (IndexError, TypeError):
            pass
        return None if True else None

    def _is_cache_valid(self) -> bool:
        """Check if cache is fresh and covers the required local horizon."""
        if not self._cache_time or not self._cache.get("hourly_forecast"):
            return False
        cache_age_seconds = (dt_util.now() - self._cache_time).total_seconds()
        return (
            cache_age_seconds < OPEN_METEO_CACHE_DURATION
            and self._has_required_horizon()
        )

    def _is_cache_usable_as_fallback(self) -> bool:
        """Check if stale cache is recent and covers the required horizon."""
        return self._is_cache_within_stale_age() and self._has_required_horizon()

    def _is_cache_within_stale_age(self) -> bool:
        """Return whether cached rows are recent enough for partial fallback."""
        if not self._cache.get("hourly_forecast") or not self._cache_time:
            return False
        cache_age_seconds = (dt_util.now() - self._cache_time).total_seconds()
        return cache_age_seconds < OPEN_METEO_STALE_CACHE_MAX_AGE

    def _has_required_horizon(
        self, hourly_data: Optional[list[dict[str, Any]]] = None
    ) -> bool:
        """Validate today through day-after-tomorrow coverage."""
        forecast_rows = (
            hourly_data
            if hourly_data is not None
            else self._cache.get("hourly_forecast", [])
        )
        return forecast_horizon_complete(
            forecast_rows,
            dt_util.now().date(),
            timezone_name=self.hass.config.time_zone,
        )

    def _last_failure_is_retryable(self) -> bool:
        """Return whether the latest network failure may succeed on retry."""
        return (
            self._last_api_status is None
            or self._last_api_status in OPEN_METEO_RETRYABLE_STATUSES
        )

    def _retry_delay_seconds(self, attempt: int) -> float:
        """Calculate bounded exponential backoff with per-installation jitter."""
        if self._last_retry_after_seconds is not None:
            return min(OPEN_METEO_RETRY_MAX_DELAY, self._last_retry_after_seconds)
        base_delay = OPEN_METEO_RETRY_BASE_DELAY * 2 ** max(0, attempt)
        return min(OPEN_METEO_RETRY_MAX_DELAY, base_delay + random.uniform(0.0, 3.0))

    def _get_cache_age_hours(self) -> float:
        """Get cache age in hours. @zara"""
        if not self._cache_time:
            return 0.0
        return (dt_util.now() - self._cache_time).total_seconds() / 3600

    def _set_forecast_status(
        self, status: str, source: str, reason: Optional[str] = None
    ) -> None:
        self._last_forecast_status = status
        self._last_forecast_source = source
        self._last_forecast_reason = reason

    @staticmethod
    def _parse_cache_timestamp(value: Any) -> Optional[datetime]:
        if not value:
            return
        try:
            parsed_timestamp = datetime.fromisoformat(str(value).replace(" ", "T"))
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
            return parsed_timestamp
        except (TypeError, ValueError):
            return

    def _get_cache_source_info(self) -> dict[str, Any]:
        """Return Open-Meteo availability and provenance diagnostics. @zara"""
        coverage_by_date = forecast_horizon_hour_counts(
            self._cache.get("hourly_forecast", []), dt_util.now().date()
        )
        return {
            "status": self._last_forecast_status,
            "source": self._last_forecast_source,
            "reason": self._last_forecast_reason,
            "age_hours": self._get_cache_age_hours() if self._cache_time else None,
            "confidence": 1.0 if self._last_forecast_status == "ok" else 0.5,
            "last_api_error": self._last_api_error,
            "last_api_status": self._last_api_status,
            "consecutive_failures": self._consecutive_failures,
            "required_horizon_complete": self._has_required_horizon(),
            "coverage_hours_by_date": coverage_by_date,
        }

    def _check_rate_limit_budget(self) -> bool:
        """Check if API calls are within rate limit. @zara"""
        now_seconds = time.time()
        window_start = now_seconds - OPEN_METEO_RATE_LIMIT_WINDOW
        self._api_call_timestamps[:] = [
            call_timestamp
            for call_timestamp in self._api_call_timestamps
            if call_timestamp > window_start
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
        for forecast_row in cached_forecast:
            if not forecast_row.get("date") == date:
                continue
            if not forecast_row.get("hour") == hour:
                continue
            return forecast_row

    def get_radiation_for_hour(
        self, date: str, hour: int
    ) -> tuple[float, float, float]:
        "Get radiation values for specific hour. @zara\n\nReturns:\n    Tuple of (direct, diffuse, ghi) radiation values\n"
        forecast_row = self.get_weather_for_hour(date, hour)
        if forecast_row:
            direct_radiation = forecast_row.get("direct_radiation") or 0
            diffuse_radiation = forecast_row.get("diffuse_radiation") or 0
            ghi = forecast_row.get("ghi") or direct_radiation + diffuse_radiation
            return (direct_radiation, diffuse_radiation, ghi)
        return (0.0, 0.0, 0.0)

    def get_forecast_for_date(self, date: str) -> list[dict[str, Any]]:
        """Get all forecasts for a specific date. @zara"""
        cached_forecast = self._cache.get("hourly_forecast", [])
        return [
            forecast_row
            for forecast_row in cached_forecast
            if forecast_row.get("date") == date
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
            params = {
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
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    OPEN_METEO_ARCHIVE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        self._last_error = f"HTTP {response.status}"
                        _LOGGER.warning(
                            "Archive API returned status %d", response.status
                        )
                        return
                    payload = await response.json()
            hourly_forecast = self._parse_archive_response(payload)
            if hourly_forecast:
                _LOGGER.info(
                    "Fetched %d hours of historical data (%s to %s)",
                    len(hourly_forecast),
                    start_date,
                    end_date,
                )
            return hourly_forecast
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
            hourly = data.get("hourly", {})
            timestamps = hourly.get("time", [])
            if not timestamps:
                _LOGGER.warning("Archive response has no time data")
                return
            forecast_rows = []
            for index, timestamp_text in enumerate(timestamps):
                target_datetime = datetime.fromisoformat(timestamp_text)
                direct_radiation = (
                    self._safe_get(hourly, "direct_radiation", index) or 0
                )
                diffuse_radiation = (
                    self._safe_get(hourly, "diffuse_radiation", index) or 0
                )
                shortwave_radiation = self._safe_get(
                    hourly, "shortwave_radiation", index
                )
                ghi = (
                    shortwave_radiation
                    if shortwave_radiation is not None
                    else direct_radiation + diffuse_radiation
                )
                ghi = max(0, min(1600, ghi))
                hourly_row = {
                    "datetime": target_datetime,
                    "date": target_datetime.date().isoformat(),
                    "hour": target_datetime.hour,
                    "temperature": self._safe_get(hourly, "temperature_2m", index),
                    "humidity": self._safe_get(hourly, "relative_humidity_2m", index),
                    "cloud_cover": self._safe_get(hourly, "cloud_cover", index),
                    "precipitation": self._safe_get(hourly, "precipitation", index),
                    "wind_speed": self._safe_get(hourly, "wind_speed_10m", index),
                    "pressure": self._safe_get(hourly, "pressure_msl", index),
                    "direct_radiation": direct_radiation,
                    "diffuse_radiation": diffuse_radiation,
                    "ghi": ghi,
                    "weather_code": self._safe_get(hourly, "weather_code", index),
                    "snowfall": self._safe_get(hourly, "snowfall", index),
                    "rain": self._safe_get(hourly, "rain", index),
                    "source": "open-meteo-archive",
                }
                forecast_rows.append(hourly_row)
                continue
            return align_provider_radiation_to_interval_start(forecast_rows)
        except Exception as error:
            _LOGGER.error("Error parsing Archive response: %s", error, exc_info=True)
            return

    @staticmethod
    def _safe_get(data: dict, key: str, index: int) -> Optional[float]:
        """Safely get a value from array in dict. @zara"""
        try:
            series = data.get(key, [])
            if index < len(series):
                return series[index]
        except (IndexError, TypeError):
            pass
        return None if True else None

    def get_last_error(self) -> Optional[str]:
        """Get last error message. @zara"""
        return self._last_error
